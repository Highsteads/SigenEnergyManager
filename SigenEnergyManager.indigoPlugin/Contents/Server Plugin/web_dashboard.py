#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    web_dashboard.py
# Description: Lightweight HTTP server serving the live Sigenergy outage view and
#              the JSON data API. Runs on port 8179, loopback by default.
#              Started from plugin.startup(), stopped on plugin.shutdown().
# Author:      CliveS & Claude Opus 5
# Date:        24-08-2026
# Version:     1.6 (page moved out to dashboard.html; Chart.js bundle and the
#              /chart.js route dropped with the charts. The seven JSON endpoints
#              are unchanged - the Dashboards plugin proxies all of them.)
# 1.5 - loopback bind by default + bearer/query/cookie token auth; refuses to
#       start on a network interface without a token
# 1.4 — same-origin CORS, real HTTP error codes, JS null-safety
# 1.3 — NaN/Infinity-safe JSON (one non-finite float no longer breaks the whole live
#       update); calendar view state (_calCurrentYear/_calYearsLoaded) hoisted to
#       <script> scope so the selected year tab survives the 5s refresh; Back link
#       host-relative (was a hardcoded LAN IP, Clive-only).

import hmac
import http.server
import ipaddress
import json
import logging
import math
import os
import socketserver
import threading

DASHBOARD_PORT = 8179

# Bind address. Loopback is the default and the Dashboards plugin proxies to it
# server-side from 127.0.0.1, so the energy and cost pages are unaffected by it.
# Widening this to every interface puts the whole API on the LAN, which is why
# the server refuses to do that without a token (see WebDashboard.start).
DASHBOARD_BIND_LOOPBACK = "127.0.0.1"
DASHBOARD_BIND_ALL      = ""

# Name of the cookie that carries the token once a browser has presented it in
# a query string, so the 5s poll does not have to repeat the token every time.
AUTH_COOKIE = "sigen_dash"

def _json_safe(obj):
    """Recursively replace NaN/Infinity floats with None so the emitted JSON is
    valid. Default json.dumps writes bare NaN/Infinity tokens, which the browser's
    JSON.parse rejects — one non-finite float would take down the whole live update."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj

# ============================================================
# ============================================================
# Dashboard page
#
# The page lives in dashboard.html beside this module rather than inside a
# Python string. It spent its first five versions as an 1,800-line triple-quoted
# literal, which meant no linter, no editor highlighting and no `node --check`
# ever looked at it — an unclosed brace in that JS was a blank page nobody could
# find. Loaded once at import, like the Chart.js bundle above.
#
# If the file is missing the server still answers, with a plain page that says
# so. Serving nothing at all would look identical to the plugin being down,
# and this page exists precisely for the moments when everything else is.
# ============================================================
_FALLBACK_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Sigenergy Monitor</title></head>
<body style="font-family:-apple-system,sans-serif;padding:2rem;line-height:1.5">
<h1>Sigenergy Monitor</h1>
<p>The dashboard page (<code>dashboard.html</code>) is missing from the plugin
bundle, so only this placeholder can be shown.</p>
<p>The data API is unaffected — <a href="/api/status">/api/status</a> still
returns the live figures, and the Dashboards plugin reads it directly.</p>
</body></html>
"""

DASHBOARD_HTML = _FALLBACK_HTML
try:
    _here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    with open(os.path.join(_here, "dashboard.html"), encoding="utf-8") as _f:
        DASHBOARD_HTML = _f.read()
except OSError as _exc:
    logging.getLogger("Sigenergy").warning(
        f"[Web] dashboard.html could not be read ({_exc}) - serving the "
        f"placeholder page. The data API is unaffected."
    )

_DASHBOARD_BYTES = DASHBOARD_HTML.encode("utf-8")


# ============================================================
# HTTP server
# ============================================================

class _DashboardTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Non-blocking threaded TCP server with address reuse."""
    allow_reuse_address = True
    daemon_threads      = True

    def handle_error(self, request, client_address):
        """Swallow routine client-disconnect exceptions (tab closed mid-write,
        phone locked during the 5s poll) instead of socketserver's default
        full traceback to stderr; anything else still gets the traceback."""
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            logging.getLogger("Sigenergy").debug(
                f"[Web] Client {client_address} disconnected: {exc}")
            return
        super().handle_error(request, client_address)


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Request handler for the Sigenergy web dashboard."""

    # Set by WebDashboard.start() before the server thread launches.
    _plugin_ref = None

    # Shared secret required from any client that is not on the loopback
    # interface. Empty means the server is loopback-only, where a token would
    # add nothing — anything able to reach 127.0.0.1 already runs as this user.
    _auth_token = ""

    # Per-connection socket timeout: reaps dead/half-open connections so a
    # slowloris-style client can't pin ThreadingMixIn threads indefinitely.
    timeout = 30

    # Keep-alive: Content-Length is always sent (_send), so the browser's 5s
    # poll can reuse one TCP connection instead of a fresh handshake per poll.
    protocol_version = "HTTP/1.1"

    def _query(self):
        return self.path.split("?", 1)[1] if "?" in self.path else ""

    # ---------------------------------------------------------------- #
    # Authentication
    #
    # Until v1.5 this server bound every interface and asked for nothing, so
    # anyone on the LAN could read the house's energy, tariff and VPP data. On
    # the LAN that was a known trade. Behind a tunnel it would be a hole, and a
    # tunnel is exactly where this is heading.
    # ---------------------------------------------------------------- #

    def _client_is_loopback(self):
        """True when the request came from this machine.

        The Dashboards plugin proxies to 127.0.0.1 server-side, so its requests
        land here as loopback and need no token.
        """
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except (ValueError, IndexError):
            return False

    def _presented_token(self):
        """Return whatever token the client offered, from any of the four places.

        Header first (what a script uses), then the query string (what a person
        pastes into a browser once), then the cookie the query string sets.
        """
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip()

        header = self.headers.get("X-Auth-Token", "")
        if header:
            return header.strip()

        for pair in self._query().split("&"):
            if pair.startswith("token="):
                from urllib.parse import unquote
                return unquote(pair.split("=", 1)[1])

        for crumb in self.headers.get("Cookie", "").split(";"):
            name, _, value = crumb.strip().partition("=")
            if name == AUTH_COOKIE:
                return value

        return ""

    def _authorised(self):
        """True when this request may proceed.

        No token configured means loopback-only, which start() has already
        enforced. Otherwise compare in constant time.
        """
        if not self._auth_token:
            return True
        if self._client_is_loopback():
            return True
        return hmac.compare_digest(self._presented_token(), self._auth_token)

    def _send_unauthorised(self):
        body = (b'{"error":"unauthorised - append ?token=... or send '
                b'an Authorization: Bearer header"}')
        self._send(401, "application/json", body)

    def _int_param(self, name, default, lo, hi):
        for kv in self._query().split("&"):
            if kv.startswith(name + "="):
                try:
                    return max(lo, min(hi, int(kv.split("=", 1)[1])))
                except ValueError:
                    return default
        return default

    def _send_api(self, producer):
        """Run a data producer and send JSON with a meaningful HTTP STATUS, so the
        browser consumers' `!r.ok` guards actually fire on failure. Errors used to
        be returned as HTTP 200 with an {error} body, which those guards never caught
        (they then tried to render the error object as data)."""
        if self._plugin_ref is None:
            self._send(503, "application/json", b'{"error":"plugin not ready"}')
            return
        # Produce the body FIRST, then send exactly once: a client that
        # disconnects mid-write raises BrokenPipeError after headers are out,
        # and attempting a second (500) response on the dead socket only
        # raises again into socketserver's handle_error.
        try:
            code, body = 200, json.dumps(_json_safe(producer())).encode()
        except Exception as exc:
            code, body = 500, json.dumps({"error": str(exc)}).encode()
        try:
            self._send(code, "application/json", body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return   # client went away mid-write — nothing useful to do

    def do_GET(self):
        if not self._authorised():
            self._send_unauthorised()
            return

        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            # A browser that authenticated with ?token= gets a cookie, so the
            # page's own 5s poll carries the token without it sitting in every
            # URL (and in the browser history, and in any referrer).
            headers = None
            if self._auth_token and not self._client_is_loopback():
                headers = [("Set-Cookie",
                            f"{AUTH_COOKIE}={self._auth_token}; Path=/; "
                            f"HttpOnly; SameSite=Strict; Max-Age=31536000")]
            self._send(200, "text/html; charset=utf-8", _DASHBOARD_BYTES, headers)

        elif path == "/api/status":
            self._send_api(lambda: self._plugin_ref.get_dashboard_data())

        elif path == "/api/history":
            # Half-hourly slots for the last N hours (default 24h, max 168h).
            hours = self._int_param("hours", 24, 1, 168)
            self._send_api(lambda: self._plugin_ref.get_dashboard_history(hours=hours))

        elif path == "/api/calendar":
            # Calendar-months summary for a specific year. Validate the param to a
            # plain 4-digit year (URL-decoded) before passing it through, rather than
            # forwarding arbitrary query text to the plugin.
            import re as _re
            from urllib.parse import unquote as _unquote
            year = ""
            for kv in self._query().split("&"):
                if kv.startswith("year="):
                    year = _unquote(kv.split("=", 1)[1])
            if not _re.fullmatch(r"\d{4}", year or ""):
                year = ""
            self._send_api(lambda: self._plugin_ref.get_dashboard_calendar(year))

        elif path == "/api/years":
            self._send_api(lambda: self._plugin_ref.get_dashboard_years())

        elif path == "/api/daily":
            # Upper bound is deliberately >365. The dashboards' week-on-week card
            # compares against the same week LAST year, probing day offsets 364-370
            # back from the newest record — a 365 cap returns at most one of those
            # seven days, so the year column could never unlock however long the
            # history grew. 800 leaves room for a two-year comparison later.
            days = self._int_param("days", 30, 1, 800)
            self._send_api(lambda: self._plugin_ref.get_dashboard_daily(days=days))

        elif path == "/api/export-sync":
            self._send_api(lambda: self._plugin_ref.get_dashboard_export_sync())

        elif path == "/api/vpp":
            # The earnings ledger plus the next announced window. Separate from
            # /api/status because it carries the per-event list, which is far
            # bigger than anything a 30-second status poll should be dragging
            # around.
            self._send_api(lambda: self._plugin_ref.get_dashboard_vpp())

        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, content_type, body, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or ()):
            self.send_header(name, value)
        # Same-origin only: the dashboard page and its /api endpoints are served from
        # this same host:port (the Dashboards plugin iframes it), so no cross-origin
        # header is needed. The previous wildcard `*` let ANY browser page read this
        # server's energy/cost/SOC data — note the bind exposure is documented in the
        # README; this drops the browser-CORS surface to same-origin.
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass   # suppress access log noise in Indigo event log


# ============================================================
# Public interface
# ============================================================

class WebDashboard:
    """Manages the lifecycle of the HTTP dashboard server thread."""

    def __init__(self, plugin, port=DASHBOARD_PORT,
                 bind_host=DASHBOARD_BIND_LOOPBACK, auth_token=""):
        self._plugin    = plugin
        self._port      = port
        self._bind_host = bind_host
        self._token     = (auth_token or "").strip()
        self._server    = None
        self._thread    = None

    def _is_loopback_bind(self):
        """True when this will only ever accept connections from this machine."""
        if not self._bind_host:
            return False          # "" means every interface
        try:
            return ipaddress.ip_address(self._bind_host).is_loopback
        except ValueError:
            return self._bind_host == "localhost"

    def start(self):
        """Start the server, refusing to open an unauthenticated network port.

        Binding every interface with no token is how this server spent its first
        five versions, and it put the whole energy API on the LAN. It is now a
        hard refusal rather than a warning: a dashboard that fails to start is
        noticed and fixed, whereas a warning in a busy log is not.
        """
        log = logging.getLogger("Sigenergy")

        if not self._is_loopback_bind() and not self._token:
            log.error(
                "[Web] Dashboard NOT started: it is set to listen on every "
                "interface but no access token is configured, which would put "
                "the energy API on the LAN unauthenticated. Set a token in the "
                "plugin config, or set the bind address back to loopback."
            )
            return

        _DashboardHandler._plugin_ref = self._plugin
        _DashboardHandler._auth_token = self._token
        self._server = _DashboardTCPServer((self._bind_host, self._port),
                                           _DashboardHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="SigenWebDash",
            daemon=True,
        )
        self._thread.start()

        if self._is_loopback_bind():
            log.info(f"[Web] Dashboard started on 127.0.0.1:{self._port} "
                     f"(this machine only)")
        else:
            log.info(f"[Web] Dashboard started on port {self._port}, all "
                     f"interfaces, token required")

    def stop(self):
        # Re-arm the 503 'plugin not ready' guard so late requests during (or
        # after) a stop/start cycle can't hit a half-stopped plugin.
        _DashboardHandler._plugin_ref = None
        _DashboardHandler._auth_token = ""
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
