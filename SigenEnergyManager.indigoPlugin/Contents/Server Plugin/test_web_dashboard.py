#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_web_dashboard.py
# Description: Unit tests for the web dashboard's access control (v1.5). Until then
#              the server bound every interface and asked for nothing, so anyone on
#              the LAN could read the house's energy, tariff and VPP data. These
#              tests pin the three rules that replaced that: loopback callers are
#              trusted (the Dashboards plugin proxies from 127.0.0.1), everyone else
#              needs the token, and the server refuses to open a network port at all
#              without one. No sockets are opened for the token tests — the predicate
#              is exercised directly, because a test running on this machine is a
#              loopback client and would pass whatever the token said.
# Author:      CliveS & Claude Opus 5
# Date:        24-08-2026
# Version:     1.0

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import web_dashboard   # noqa: E402

TOKEN = "s3cret-token-value"
LAN   = "192.168.1.77"


class _Headers(dict):
    """Minimal stand-in for the email.message.Message that http.server supplies."""

    def get(self, name, default=""):
        for key, value in self.items():
            if key.lower() == name.lower():
                return value
        return default


def make_handler(client_ip, token=TOKEN, path="/api/status", headers=None):
    """Build a handler without running BaseHTTPRequestHandler.__init__.

    The real __init__ starts parsing a socket, which is not what is under test —
    the authorisation predicate is.
    """
    handler = object.__new__(web_dashboard._DashboardHandler)
    handler.client_address = (client_ip, 51000)
    handler.path = path
    handler.headers = _Headers(headers or {})
    handler._auth_token = token
    return handler


class TestClientIsLoopback(unittest.TestCase):

    def test_ipv4_loopback(self):
        self.assertTrue(make_handler("127.0.0.1")._client_is_loopback())

    def test_ipv6_loopback(self):
        self.assertTrue(make_handler("::1")._client_is_loopback())

    def test_ipv4_mapped_loopback(self):
        self.assertTrue(make_handler("::ffff:127.0.0.1")._client_is_loopback())

    def test_lan_address_is_not_loopback(self):
        self.assertFalse(make_handler(LAN)._client_is_loopback())

    def test_garbage_address_is_not_loopback(self):
        """An unparseable peer must fail closed, not open."""
        self.assertFalse(make_handler("not-an-ip")._client_is_loopback())


class TestTokenExtraction(unittest.TestCase):

    def test_bearer_header(self):
        h = make_handler(LAN, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(h._presented_token(), TOKEN)

    def test_x_auth_token_header(self):
        h = make_handler(LAN, headers={"X-Auth-Token": TOKEN})
        self.assertEqual(h._presented_token(), TOKEN)

    def test_query_string(self):
        h = make_handler(LAN, path=f"/api/status?token={TOKEN}&hours=24")
        self.assertEqual(h._presented_token(), TOKEN)

    def test_query_string_is_url_decoded(self):
        h = make_handler(LAN, path="/api/status?token=a%2Bb%2Fc")
        self.assertEqual(h._presented_token(), "a+b/c")

    def test_cookie(self):
        h = make_handler(LAN, headers={"Cookie": f"other=1; {web_dashboard.AUTH_COOKIE}={TOKEN}"})
        self.assertEqual(h._presented_token(), TOKEN)

    def test_nothing_presented(self):
        self.assertEqual(make_handler(LAN)._presented_token(), "")

    def test_basic_auth_header_is_not_mistaken_for_a_bearer(self):
        h = make_handler(LAN, headers={"Authorization": "Basic dXNlcjpwYXNz"})
        self.assertEqual(h._presented_token(), "")


class TestAuthorised(unittest.TestCase):

    def test_loopback_needs_no_token(self):
        """The Dashboards plugin proxies from 127.0.0.1 and sends no token."""
        self.assertTrue(make_handler("127.0.0.1")._authorised())

    def test_lan_without_token_is_refused(self):
        self.assertFalse(make_handler(LAN)._authorised())

    def test_lan_with_correct_token_is_allowed(self):
        h = make_handler(LAN, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertTrue(h._authorised())

    def test_lan_with_wrong_token_is_refused(self):
        h = make_handler(LAN, headers={"Authorization": "Bearer wrong"})
        self.assertFalse(h._authorised())

    def test_lan_with_token_prefix_is_refused(self):
        """A prefix of the real token must not pass."""
        h = make_handler(LAN, headers={"Authorization": f"Bearer {TOKEN[:-1]}"})
        self.assertFalse(h._authorised())

    def test_empty_token_config_means_loopback_only_so_anything_passes(self):
        """No token configured is only ever paired with a loopback bind, which
        start() enforces. The predicate itself must not then reject callers."""
        self.assertTrue(make_handler(LAN, token="")._authorised())

    def test_empty_presented_token_never_matches_a_configured_one(self):
        """The absent-value trap: '' must not satisfy a configured token."""
        h = make_handler(LAN, headers={"Cookie": f"{web_dashboard.AUTH_COOKIE}="})
        self.assertFalse(h._authorised())


class TestBindRefusal(unittest.TestCase):
    """start() must not open an unauthenticated network port."""

    def tearDown(self):
        if getattr(self, "dash", None):
            self.dash.stop()
        web_dashboard._DashboardHandler._auth_token = ""

    def test_all_interfaces_without_token_refuses_to_start(self):
        self.dash = web_dashboard.WebDashboard(
            plugin=None, port=18179,
            bind_host=web_dashboard.DASHBOARD_BIND_ALL, auth_token="")
        self.dash.start()
        self.assertIsNone(self.dash._server,
                          "server started on every interface with no token")

    def test_all_interfaces_with_token_starts(self):
        self.dash = web_dashboard.WebDashboard(
            plugin=None, port=18180,
            bind_host=web_dashboard.DASHBOARD_BIND_ALL, auth_token=TOKEN)
        self.dash.start()
        self.assertIsNotNone(self.dash._server)
        self.assertEqual(web_dashboard._DashboardHandler._auth_token, TOKEN)

    def test_loopback_without_token_starts(self):
        self.dash = web_dashboard.WebDashboard(
            plugin=None, port=18181,
            bind_host=web_dashboard.DASHBOARD_BIND_LOOPBACK, auth_token="")
        self.dash.start()
        self.assertIsNotNone(self.dash._server)

    def test_default_bind_is_loopback(self):
        """The default must be the safe one — a fresh install is closed."""
        dash = web_dashboard.WebDashboard(plugin=None, port=18182)
        self.assertTrue(dash._is_loopback_bind())

    def test_stop_clears_the_shared_token(self):
        """The handler class attribute is global; a stale token must not survive."""
        self.dash = web_dashboard.WebDashboard(
            plugin=None, port=18183,
            bind_host=web_dashboard.DASHBOARD_BIND_LOOPBACK, auth_token=TOKEN)
        self.dash.start()
        self.dash.stop()
        self.dash = None
        self.assertEqual(web_dashboard._DashboardHandler._auth_token, "")


class TestLiveLoopbackServer(unittest.TestCase):
    """One end-to-end pass over a real socket, to prove the gate is actually wired
    into do_GET rather than merely present on the class."""

    def test_lan_request_over_the_wire_gets_401(self):
        import http.client
        dash = web_dashboard.WebDashboard(
            plugin=None, port=18184,
            bind_host=web_dashboard.DASHBOARD_BIND_LOOPBACK, auth_token=TOKEN)
        dash.start()
        try:
            # Force the handler to believe the peer is on the LAN, so the
            # loopback exemption does not mask the gate.
            original = web_dashboard._DashboardHandler._client_is_loopback
            web_dashboard._DashboardHandler._client_is_loopback = lambda self: False
            try:
                conn = http.client.HTTPConnection("127.0.0.1", 18184, timeout=5)
                conn.request("GET", "/api/status")
                self.assertEqual(conn.getresponse().status, 401)
                conn.close()

                conn = http.client.HTTPConnection("127.0.0.1", 18184, timeout=5)
                conn.request("GET", "/api/status",
                             headers={"Authorization": f"Bearer {TOKEN}"})
                # 503 is the "plugin not ready" path — the point is that it is
                # NOT a 401, so the token was accepted.
                self.assertNotEqual(conn.getresponse().status, 401)
                conn.close()
            finally:
                web_dashboard._DashboardHandler._client_is_loopback = original
        finally:
            dash.stop()


class _StubPlugin:
    """Answers every producer the dashboard routes call, with the right shape."""
    def get_dashboard_data(self):      return {"battery": {"soc_pct": 50}}
    def get_dashboard_history(self, hours=24): return {"slots": []}
    def get_dashboard_daily(self, days=30):    return {"records": []}
    def get_dashboard_export_sync(self):       return {"rows": [], "summary": {}}
    def get_dashboard_calendar(self, year):    return {"months": [], "year": year}
    def get_dashboard_years(self):     return {"years": [2026]}
    def get_dashboard_vpp(self):       return {"events": []}


class TestApiSurface(unittest.TestCase):
    """The seven JSON endpoints the Dashboards plugin proxies to.

    v5.76.0 stripped this server's own page back to an outage view and moved the
    charts to the Dashboards energy page. The PAGE shrank; the API must not. If
    any of these stops answering, the Energy and Cost pages lose data silently —
    the proxy returns the error as data and the cards just go blank.

    The list is deliberately hardcoded rather than derived from the handler, so
    deleting a route makes this fail instead of quietly agreeing with itself.
    """

    # Mirrors Dashboards' _SIGEN_ALLOWED_PATHS.
    REQUIRED = ["/api/status", "/api/history", "/api/daily", "/api/export-sync",
                "/api/years", "/api/calendar", "/api/vpp"]

    @classmethod
    def setUpClass(cls):
        cls.port = 18190
        cls.dash = web_dashboard.WebDashboard(
            plugin=_StubPlugin(), port=cls.port,
            bind_host=web_dashboard.DASHBOARD_BIND_LOOPBACK, auth_token="")
        cls.dash.start()

    @classmethod
    def tearDownClass(cls):
        cls.dash.stop()

    def _get(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body

    def test_every_proxied_endpoint_answers(self):
        for path in self.REQUIRED:
            with self.subTest(path=path):
                status, body = self._get(path)
                self.assertEqual(status, 200, f"{path} returned {status}")
                json.loads(body)   # must be valid JSON, not an HTML error page

    def test_page_is_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Sigenergy", body)

    def test_page_came_from_disk_not_the_placeholder(self):
        """A missing dashboard.html degrades to a placeholder. Catch that in CI
        rather than discovering it during a power cut."""
        self.assertNotEqual(web_dashboard.DASHBOARD_HTML, web_dashboard._FALLBACK_HTML,
                            "dashboard.html did not load - serving the placeholder")

    def test_unknown_path_404s(self):
        status, _ = self._get("/api/nonsense")
        self.assertEqual(status, 404)

    def test_chart_js_route_is_gone(self):
        """The 200 KB Chart.js bundle left with the charts."""
        status, _ = self._get("/chart.js")
        self.assertEqual(status, 404)


class TestPageContent(unittest.TestCase):
    """What the outage page must and must not contain."""

    @classmethod
    def setUpClass(cls):
        cls.html = web_dashboard.DASHBOARD_HTML

    def test_keeps_the_outage_essentials(self):
        for needle in ["Live Power Flow", "Battery State", "Live Power",
                       "Manager Decision", "flow-svg", "soc-ring"]:
            with self.subTest(needle=needle):
                self.assertIn(needle, self.html)

    def test_duplicated_cards_are_gone(self):
        """These live on the Dashboards energy and cost pages. Two copies of a
        chart is two things to keep in step, and one of them always rots."""
        for needle in ["chart-soc", "chart-energy", "chart-daily", "cal-tbody",
                       "period-tbody", "exp-sync-tbody", "fc-svg", "tar-rate"]:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.html)

    def test_no_reference_to_the_removed_chart_bundle(self):
        self.assertNotIn("/chart.js", self.html)

    def test_page_has_no_unresolved_element_lookups(self):
        """Every getElementById literal must match an id that exists in the page.

        The three legitimate exceptions are built at runtime.
        """
        import re
        ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', self.html))
        allowed_dynamic = {"help-tip", "dot-"}
        missing = {m for m in re.findall(r"getElementById\(\s*['\"]([a-zA-Z0-9_-]+)['\"]", self.html)
                   if m not in ids and m not in allowed_dynamic}
        self.assertEqual(missing, set(), f"page references ids that do not exist: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
