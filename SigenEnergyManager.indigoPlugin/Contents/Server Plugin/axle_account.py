#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    axle_account.py
# Description: Reads the Axle account directly, using the short-lived token Axle
#              already post through the door in their own emails. No login, no
#              stored credential, and two layers so the fragile one cannot fail
#              quietly.
# Author:      CliveS & Claude Opus 5
# Date:        12-09-2026
# Version:     1.0
#
# WHY THIS MODULE EXISTS
# ----------------------
# Until 12-Sep-2026 the only automated earnings feed was the per-event
# settlement email, and on that date it was found to have stopped dead three
# weeks earlier. Axle had moved this site from `asset_readings` settlement to
# `boundary_meter`, and the boundary_meter path sends no per-event email at all.
# Two events, GBP 8.00, settled and paid at Axle and invisible here - with the
# staleness check reading a contented "0 days" throughout, because it was timed
# from our own scans rather than from anything Axle had said.
#
# THE TOKEN. Every Axle email carries `?token=<jwt>` in its account link -
# "Upcoming Grid Event", "Grid Event Finished" and the monthly summary, not just
# "Link to your account". Each is valid SEVEN DAYS and carries the site id as a
# claim, so there is nothing to configure and nothing to store. Measured over
# all 16 events on this account, 14 had a live token across their whole six-day
# settlement window; the two that did not were an April event that predates the
# mail store's reach and one gap that fell AFTER its settlement had landed.
# That is not luck - every event mints a token on the day it finishes and
# settlement arrives inside seven days, so the cover follows the need.
#
# TWO LAYERS, AND THE ORDER MATTERS
#
#   events   /vpp/site/{id}/events on api.axle.energy. Ordinary JSON, in Axle's
#            own published OpenAPI, and it carries `settled_via` - a POSITIVE
#            statement that an event has been settled. It never carries money.
#   account  the account page's own single-fetch payload, which does carry the
#            balance and every transaction. It is an internal encoding of a web
#            framework and can change without notice.
#
# The point of keeping both is that `settled_via` makes the second one's failure
# LOUD. If the account fetch breaks, `events` still says "Axle settled 5 Sept",
# we still know money is owed, and the plugin can say so. A single-layer design
# built on the account payload alone would go silent the day the encoding moved,
# which is the exact failure this module was written to end.
#
# Nothing here raises into the plugin and nothing here writes. Every fetch is a
# GET, every parse is bounded, and the account payload is refused unless it
# proves internally consistent - see `validate_account_payload`.

import base64
import email
import email.policy
import email.utils
import glob
import json
import os
import re
import time

from datetime import datetime, timedelta, timezone


API_HOST     = "https://api.axle.energy"
ACCOUNT_HOST = "https://vpp.axle.energy"

MAIL_ROOT_GLOB = "~/Library/Mail/V*"

# A token lives seven days, so a fortnight of mail is ample and keeps the walk
# cheap. Liveness is decided by the token's own `exp` claim, never by the file
# date - Apple Mail rewrites .emlx files, so mtime runs AHEAD of the message
# date and can only ever over-include here.
TOKEN_LOOKBACK_DAYS = 14

_PREFILTER   = b"axle.energy"
_RE_TOKEN    = re.compile(r"token=([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)")

HTTP_TIMEOUT = 20

# Guard rails for the decoded account payload. A settled event on this tariff is
# a few pounds; the referral credit is the largest single row ever seen at
# GBP 25. Anything wildly outside says the decode went wrong, not that Axle have
# been generous.
MAX_ROW_PENCE      = 100000      # GBP 1000
MAX_TRANSACTIONS   = 2000
BALANCE_TOLERANCE  = 0           # pence; the rows must sum EXACTLY to the total


# ======================================================================
# The token, out of Axle's own mail
# ======================================================================

def _decode_jwt_claims(token):
    """The payload segment of a JWT, without verifying the signature.

    We are not authenticating anything here - the server does that. This only
    reads `exp` and the site id so the right token can be chosen and a dead one
    skipped before a pointless request. A malformed token returns None rather
    than raising, because it arrives from a mailbox.
    """
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def iter_mail_tokens(root_glob=MAIL_ROOT_GLOB, since_days=TOKEN_LOOKBACK_DAYS, now=None):
    """Yield (token, expires_at, site_id) for every Axle account token in the
    local Mail store, newest file first is NOT guaranteed - the caller sorts.

    Never raises. An unreadable store, a malformed message or a permission
    change is skipped, exactly as the settlement-mail walk does: a mail scan
    must not be able to take the plugin down with it.
    """
    cutoff = (now if now is not None else time.time()) - since_days * 86400
    for root in sorted(glob.glob(os.path.expanduser(root_glob))):
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".emlx"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        continue
                    with open(path, "rb") as fh:
                        head = fh.read(4096)
                        if _PREFILTER not in head:
                            continue
                        raw = head + fh.read()
                except (OSError, ValueError):
                    continue
                try:
                    nl  = raw.index(b"\n")
                    msg = email.message_from_bytes(raw[nl + 1:], policy=email.policy.default)
                except Exception:
                    continue
                body = ""
                try:
                    parts = msg.walk() if msg.is_multipart() else [msg]
                    for part in parts:
                        if part.get_content_type() in ("text/html", "text/plain"):
                            try:
                                body += part.get_content()
                            except Exception:
                                continue
                except Exception:
                    continue
                for token in set(_RE_TOKEN.findall(body)):
                    claims = _decode_jwt_claims(token)
                    if not claims:
                        continue
                    exp = claims.get("exp")
                    sid = claims.get("internal_site_id")
                    if not isinstance(exp, (int, float)) or not sid:
                        continue
                    yield (token, datetime.fromtimestamp(exp, timezone.utc), str(sid))


def newest_live_token(root_glob=MAIL_ROOT_GLOB, since_days=TOKEN_LOOKBACK_DAYS,
                      now=None, iterator=None):
    """The account token with the latest expiry that has not expired yet.

    Returns (token, expires_at, site_id), or None when nothing usable is on
    disk. `iterator` is the test seam - the default walks the real Mail store.

    A token that expires within a minute is treated as dead: the request it
    would be used for has to complete, and starting one that is certain to 401
    tells us nothing except that we were slow.
    """
    when = datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)
    walk = iterator if iterator is not None else iter_mail_tokens(root_glob, since_days, now)
    best = None
    for token, expires, site_id in walk:
        if expires <= when + timedelta(minutes=1):
            continue
        if best is None or expires > best[1]:
            best = (token, expires, site_id)
    return best


# ======================================================================
# Layer 1 - the events feed. Ordinary JSON, and it carries `settled_via`.
# ======================================================================

def _http_get(url, token, timeout, getter=None):
    """One authenticated GET returning (status, text). Never raises.

    `getter` is the seam every test uses, so nothing in the suite can reach the
    network. The real one is imported lazily: `requests` is pre-installed by
    Indigo, but a module-level import would make this file unloadable in a bare
    interpreter, and the tests load it by path.
    """
    if getter is not None:
        return getter(url, token, timeout)
    try:
        import requests
        r = requests.get(url, timeout=timeout,
                         headers={"Authorization": f"Bearer {token}"})
        return (r.status_code, r.text)
    except Exception as exc:
        return (0, f"{type(exc).__name__}: {exc}")


def fetch_events(site_id, token, timeout=HTTP_TIMEOUT, getter=None):
    """Axle's own record of every event for this site.

    Returns {"events": [...], "note": ""} on success, or {"events": None,
    "note": "<why>"}. The note is never empty when events is None - a feed that
    fails silently is the thing this module exists to stop.

    Only the keys the ledger understands are carried through. Axle also return
    image and map URLs; those are of no use here and would bloat the file.
    """
    url = f"{API_HOST}/vpp/site/{site_id}/events"
    status, text = _http_get(url, token, timeout, getter)
    if status != 200:
        detail = (text or "")[:120].replace("\n", " ")
        return {"events": None,
                "note": f"the events feed answered HTTP {status} ({detail})"}
    try:
        raw = json.loads(text)
    except Exception as exc:
        return {"events": None, "note": f"the events feed returned unreadable JSON ({exc})"}
    if not isinstance(raw, list):
        return {"events": None, "note": "the events feed did not return a list"}

    events = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start_time")
        end   = item.get("end_time")
        if not start or not end:
            continue
        events.append({
            "event_id":    item.get("event_id"),
            "start_time":  start,
            "end_time":    end,
            # The whole reason this layer exists. None means Axle have not
            # settled it, which is a different thing from settled-at-zero.
            "settled_via": item.get("settled_via"),
            "opted_out_at": item.get("opted_out_at"),
        })
    return {"events": events, "note": ""}


# ======================================================================
# Layer 2 - the account payload. Carries the money; fragile by nature.
# ======================================================================

def decode_single_fetch(text):
    """Decode the account page's single-fetch payload.

    The page is React Router v7, whose single-fetch envelope is a flat JSON
    array used as a value table: an integer is an INDEX into that array, an
    object's `_N` key is itself an index giving the key's name, and negative
    integers are sentinels. So the whole document is reachable from element 0.

    THIS IS AN INTERNAL ENCODING OF SOMEBODY ELSE'S WEB FRAMEWORK. It is not a
    published interface, it carries no version, and it is entitled to change
    without warning. That is exactly why `validate_account_payload` exists and
    why layer 1 above does not depend on any of this.

    Raises ValueError on anything it cannot walk; the caller turns that into a
    note. Cycles resolve to None rather than recursing for ever.
    """
    try:
        arr = json.loads(text)
    except Exception as exc:
        raise ValueError(f"not JSON ({exc})")
    if not isinstance(arr, list) or not arr:
        raise ValueError("not a single-fetch array")

    limit = len(arr)

    def resolve(ref, seen):
        if not isinstance(ref, int) or isinstance(ref, bool):
            return ref
        if ref < 0:                      # sentinels: null/undefined/NaN/etc
            return None
        if ref >= limit or ref in seen:
            return None
        value = arr[ref]
        nxt   = seen | {ref}
        if isinstance(value, dict):
            out = {}
            for key, val in value.items():
                if key.startswith("_"):
                    try:
                        name = resolve(int(key[1:]), nxt)
                    except ValueError:
                        continue
                else:
                    name = key
                if isinstance(name, str):
                    out[name] = resolve(val, nxt)
            return out
        if isinstance(value, list):
            return [resolve(x, nxt) for x in value]
        return value

    return resolve(0, frozenset())


def _find_owner(obj, key, out, depth=0):
    """Every dict anywhere in `obj` that has `key`. Depth-bounded."""
    if depth > 40:
        return
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj)
        for value in obj.values():
            _find_owner(value, key, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _find_owner(item, key, out, depth + 1)


def validate_account_payload(payload):
    """Prove the decode is sound before a penny of it reaches the ledger.

    Returns (ok, note). The note explains the refusal and is empty on success.

    THE LOAD-BEARING CHECK IS THE LAST ONE: the transaction rows must sum
    EXACTLY to Axle's own `total_earnings_pence`. Those two numbers come from
    different parts of the payload, so an encoding change that garbles either
    one breaks the identity. It is a far better test than any amount of type
    checking, because it cannot be satisfied by a plausible-looking wrong
    answer - which is precisely how a silent decode failure would otherwise
    present.

    Skipped only when a withdrawal row exists: a withdrawal reduces the
    available balance without reducing lifetime earnings, this account has
    never had one, and a check built on a guessed sign convention is worse than
    no check. That is the same reasoning `vpp_ledger.summarise` already applies.
    """
    if not isinstance(payload, dict):
        return (False, "the decoded payload was not an object")

    balance = payload.get("balance")
    txs     = payload.get("transactions")
    if not isinstance(balance, dict):
        return (False, "no balance block in the decoded payload")
    if not isinstance(txs, list) or not txs:
        return (False, "no transactions in the decoded payload")
    if len(txs) > MAX_TRANSACTIONS:
        return (False, f"{len(txs)} transactions is implausible")

    total = balance.get("total_earnings_pence")
    if not isinstance(total, int) or isinstance(total, bool):
        return (False, "the balance block carries no whole-pence total")

    rows = 0
    for tx in txs:
        if not isinstance(tx, dict):
            return (False, "a transaction row was not an object")
        pence = tx.get("credit_pence")
        if not isinstance(pence, int) or isinstance(pence, bool):
            return (False, "a transaction row carries no whole-pence amount")
        if abs(pence) > MAX_ROW_PENCE:
            return (False, f"a transaction row of {pence}p is outside the plausible range")
        if not tx.get("start_time"):
            return (False, "a transaction row carries no start time")
        rows += pence

    if any((tx.get("transaction_type") or "").strip() == "withdrawal" for tx in txs):
        return (True, "")

    if abs(rows - total) > BALANCE_TOLERANCE:
        return (False, f"the rows sum to {rows}p but Axle's total is {total}p, "
                       f"so the payload did not decode cleanly")
    return (True, "")


def fetch_account(site_id, token, timeout=HTTP_TIMEOUT, getter=None):
    """The balance and full transaction list from the account page.

    Returns {"payload": {...}, "note": ""} or {"payload": None, "note": "<why>"}.
    The payload is in exactly the shape `vpp_ledger.import_axle_payload` takes.

    `events_joined` is deliberately NOT synthesised. Axle's page computes it in
    the browser and it is absent from this payload, so producing our own would
    be our arithmetic wearing their name - the one rule the whole axle side
    turns on. Nothing reads it.
    """
    url = (f"{ACCOUNT_HOST}/app/account/balance.data"
           f"?token={token}&siteId={site_id}")
    # The account host takes the token in the query string, as the browser does;
    # the bearer header is harmless and keeps one code path.
    status, text = _http_get(url, token, timeout, getter)
    if status != 200:
        detail = (text or "")[:120].replace("\n", " ")
        return {"payload": None,
                "note": f"the account page answered HTTP {status} ({detail})"}

    try:
        root = decode_single_fetch(text)
    except ValueError as exc:
        return {"payload": None, "note": f"the account payload could not be decoded ({exc})"}

    owners = []
    _find_owner(root, "transactions", owners)
    txs = owners[0].get("transactions") if owners else None

    owners = []
    _find_owner(root, "current_balance_pence", owners)
    balance = owners[0] if owners else None

    payload = {"balance": balance, "transactions": txs}
    ok, note = validate_account_payload(payload)
    if not ok:
        return {"payload": None, "note": note}
    return {"payload": payload, "note": ""}


# ======================================================================
# What the two layers together can say that neither can alone
# ======================================================================

def settled_without_figures(ledger):
    """Windows Axle say they have settled but for which we hold no money.

    This is the finding the whole module exists to produce, and it is a POSITIVE
    one: it rests on Axle's own `settled_via`, not on an absence. An event with
    `settled_via` set and no flex-event transaction against it is money that has
    been paid and has not reached the ledger - which is precisely the state 5
    and 7 September sat in, undetected, for a week.

    Returns a list of ISO start times, newest first.
    """
    axle = (ledger or {}).get("axle") or {}
    paid = set()
    for tx in (axle.get("transactions") or []):
        if not isinstance(tx, dict):
            continue
        if (tx.get("transaction_type") or "").strip() == "flex event":
            start = tx.get("start_time")
            if start:
                paid.add(str(start)[:16])

    missing = []
    for ev in (axle.get("events") or []):
        if not isinstance(ev, dict):
            continue
        if not ev.get("settled_via"):
            continue                       # not settled yet - correctly pending
        start = ev.get("start_time")
        if start and str(start)[:16] not in paid:
            missing.append(start)
    return sorted(missing, reverse=True)
