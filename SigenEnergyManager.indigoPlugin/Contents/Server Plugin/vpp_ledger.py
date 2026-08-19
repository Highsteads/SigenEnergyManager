#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    vpp_ledger.py
# Description: The VPP earnings ledger - what Axle has actually settled, held
#              beside what we observed ourselves, and never merged into one
#              ambiguous number.
# Author:      CliveS & Claude Opus 5
# Date:        18-08-2026
# Version:     1.0
#
# WHY THIS MODULE EXISTS
# ----------------------
# The plugin has always known what it EXPORTED - the per-event JSONL and the
# lastVpp* device states cover that in detail. What it has never known is what
# Axle PAID, and the two are not the same number. Axle settles on `flex_kwh`,
# which is the change in energy flow measured against a baseline, so a window
# the meter records as 4.00 kWh settles at 3.838. That is not a deduction to be
# chased; it is a different measurement, and the only place it exists is the
# Axle account.
#
# So there are two sources here and they stay separate:
#
#   axle   - the settled truth. Balance, transactions and the event list,
#            exactly as Axle's account page reports them. Imported wholesale;
#            never computed, never adjusted, never inferred.
#   local  - what we watched happen. One row per event we drove, taken from our
#            own snapshots, priced at the configured rate. An ESTIMATE, and
#            labelled as one everywhere it surfaces.
#
# THE RULE THAT MATTERS: an event with no Axle transaction against it is
# PENDING, not zero. Settlement runs days behind the event - the 16 Aug window
# was still unsettled two days later - so "no row yet" is the normal state for
# the newest event, and rendering it as GBP 0.00 would report a loss that never
# happened. `paid_gbp` is None until Axle says otherwise, and every consumer has
# to handle None rather than falling back to a number. A genuine zero (20 Apr
# 2026 settled at 0.000 kWh) arrives as a real transaction with credit_pence 0
# and is a different thing entirely - the ledger can tell them apart, and so
# must anything downstream.
#
# See also the standing estate rule: an absent value must never satisfy a
# comparison that a present one would.

import json
import os
import tempfile

from datetime import datetime, timezone

try:
    from london_time import to_london
except ImportError:                                     # pragma: no cover
    def to_london(dt):
        return dt


LEDGER_FILENAME = "vpp_ledger.json"
SCHEMA_VERSION  = 1

# Transaction types Axle uses. Grouped for reporting, because "what have I
# earned from grid events" and "what has landed in the account" are different
# questions and the second one includes money the battery had nothing to do
# with. The referral credit alone is GBP 25 of the lifetime total.
FLEX_EVENT_TYPES = frozenset({"flex event"})
TOP_UP_TYPES     = frozenset({"flex period", "flex period top-up"})
WITHDRAWAL_TYPES = frozenset({"withdrawal"})


# ======================================================================
# Load / save
# ======================================================================

def empty_ledger():
    """A well-formed ledger with nothing in it.

    Every reader below assumes these keys exist, so a fresh install and a
    corrupt file both come back through here rather than through None.
    """
    return {
        "schema":     SCHEMA_VERSION,
        "updated_at": None,
        "axle": {
            "fetched_at":   None,
            "balance":      None,
            "transactions": [],
            "events":       [],
        },
        "local": [],
    }


def ledger_path(data_dir):
    return os.path.join(data_dir, LEDGER_FILENAME)


def load_ledger(path):
    """Read the ledger, returning an empty one if it is missing or unreadable.

    A corrupt ledger must not stop the plugin starting, and it must not read as
    "no earnings" to anything downstream either - hence the `load_error` marker,
    which the summary carries through so the dashboard can say so out loud
    rather than showing a confident GBP 0.00.
    """
    if not path or not os.path.exists(path):
        return empty_ledger()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        led = empty_ledger()
        led["load_error"] = f"{type(exc).__name__}: {exc}"
        return led

    if not isinstance(data, dict):
        led = empty_ledger()
        led["load_error"] = "ledger file is not a JSON object"
        return led

    # Fill in anything a future/older schema left out rather than trusting the
    # shape - a half-written file should degrade to "less data", not to a
    # KeyError on the status endpoint.
    base = empty_ledger()
    base.update({k: v for k, v in data.items() if k in base or k == "load_error"})
    axle = data.get("axle") or {}
    base["axle"] = {
        "fetched_at":   axle.get("fetched_at"),
        "balance":      axle.get("balance"),
        "transactions": list(axle.get("transactions") or []),
        "events":       list(axle.get("events") or []),
    }
    base["local"] = list(data.get("local") or [])
    return base


def save_ledger(path, ledger):
    """Write the ledger atomically.

    Same reasoning as every other JSON store in this plugin: a half-written
    file read back on the next boot is worse than no file, because it looks
    like data.
    """
    ledger = dict(ledger)
    ledger["schema"]     = SCHEMA_VERSION
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    ledger.pop("load_error", None)

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".vpp_ledger-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=1, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return ledger


# ======================================================================
# Importing Axle's own numbers
# ======================================================================

def import_axle_payload(ledger, payload, fetched_at=None):
    """Merge an Axle account payload into the ledger.

    `payload` is the object Axle's account page is built from - the same shape
    whichever way it was obtained, which is the point of doing the merge here.
    Today it arrives by hand; a cookie-authenticated fetch or a widened API
    token would feed the identical dict through this one function.

    Expected keys (all optional): `balance`, `transactions`, `events`.

    Transactions are keyed on `transaction_id`, so re-importing the same
    payload is a no-op and a partial payload never truncates what is already
    held. Returns (ledger, added_count).
    """
    if not isinstance(payload, dict):
        raise ValueError("Axle payload must be a dict")

    axle     = ledger.setdefault("axle", {})
    existing = {t.get("transaction_id"): t
                for t in (axle.get("transactions") or [])
                if isinstance(t, dict) and t.get("transaction_id")}

    added = 0
    for tx in (payload.get("transactions") or []):
        if not isinstance(tx, dict):
            continue
        tid = tx.get("transaction_id")
        if not tid:
            continue
        if tid not in existing:
            added += 1
        # Later payloads win: `settlement_date` and `payment_status` are filled
        # in after the fact, so an existing row is stale by definition.
        existing[tid] = tx

    axle["transactions"] = sorted(
        existing.values(),
        key=lambda t: (t.get("start_time") or ""),
        reverse=True,
    )

    if payload.get("balance") is not None:
        axle["balance"] = payload["balance"]

    if payload.get("events"):
        by_window = {}
        for ev in (axle.get("events") or []):
            if isinstance(ev, dict):
                by_window[(ev.get("start_time"), ev.get("end_time"))] = ev
        for ev in payload["events"]:
            if isinstance(ev, dict):
                by_window[(ev.get("start_time"), ev.get("end_time"))] = ev
        axle["events"] = sorted(
            by_window.values(),
            key=lambda e: (e.get("start_time") or ""),
            reverse=True,
        )

    axle["fetched_at"] = fetched_at or datetime.now(timezone.utc).isoformat()
    return ledger, added


# ======================================================================
# What we exported INSIDE the paid window
# ======================================================================

def integrate_window_kwh(samples, duration_hrs):
    """Grid export inside the paid window, in kWh, from per-minute snapshots.

    `samples` is [(elapsed_seconds, grid_watts), ...] where grid watts are
    NEGATIVE while exporting. Returns None when there is nothing to integrate.

    WHY THIS EXISTS, and why the obvious number is the wrong one.
    The plugin's own `export_kwh` is a counter delta across the whole time it
    drove the export — and that is deliberately WIDER than the paid window: the
    driver runs from two minutes before to two minutes after, so the full hour
    is captured rather than ramped into. On an ordinary event that adds ~0.22
    kWh of tails. On 11-Aug-2026 it added FORTY-FIVE MINUTES, because the
    window never stopped (the v5.61.1 bug), and the counter read 7.05 kWh for
    an hour whose DNO cap allows 4.

    Comparing that against Axle's in-window figure puts two different
    quantities in one subtraction. Measured across every event we have, the
    counter sits 0.2-0.3 kWh above the window and the difference against Axle
    scatters; integrated strictly inside the window it is 4.00 kWh every time
    (8.01 on the two-hour event — the cap times the duration, as it must be),
    and the gap against Axle collapses to a consistent +0.162 to +0.197 kWh.
    THAT is the baseline, and it only becomes visible once both sides are
    measured over the same hour.

    Trapezoid rather than a counter read because there is no counter reading at
    the window boundary — only the snapshots, at roughly 83-second spacing. The
    curve is nearly flat (grid pinned at the export cap for the duration), so
    the approximation is worth a couple of watt-hours, and it agrees with the
    counter to within the known width of the tails.
    """
    try:
        hrs = float(duration_hrs)
    except (TypeError, ValueError):
        return None
    if hrs <= 0:
        return None

    pts = []
    for s in (samples or []):
        try:
            t, w = float(s[0]), float(s[1])
        except (TypeError, ValueError, IndexError):
            continue
        pts.append((t, w))
    if len(pts) < 2:
        return None
    pts.sort()

    end_s = hrs * 3600.0
    kwh = 0.0
    for (t0, w0), (t1, w1) in zip(pts, pts[1:]):
        a, b = max(t0, 0.0), min(t1, end_s)
        if b <= a or t1 == t0:
            continue
        # Interpolate to the clipped bounds so the lead-in and trail sit
        # outside the sum rather than being counted at their full width.
        f0 = w0 + (w1 - w0) * ((a - t0) / (t1 - t0))
        f1 = w0 + (w1 - w0) * ((b - t0) / (t1 - t0))
        # Export only. An importing minute inside the window did not earn
        # anything, and letting it subtract would flatter the total.
        exported_w = max(0.0, -(f0 + f1) / 2.0)
        kwh += exported_w * (b - a) / 3600.0 / 1000.0
    return round(kwh, 3)


# ======================================================================
# Recording what we saw ourselves
# ======================================================================

def record_local_event(ledger, start_time, end_time, export_kwh,
                       rate_per_kwh, driver=None, log_path=None,
                       window_kwh=None):
    """Upsert one locally-observed event, keyed on its window.

    Keyed on the window rather than appended, so a re-run of the summariser
    after a restart corrects the row instead of double-counting it.

    TWO figures, because they answer different questions:
      export_kwh  the counter delta over the whole time we drove the export,
                  lead-in and trail included — what actually left the house.
      window_kwh  integrated strictly inside the PAID window — the only one
                  comparable with what Axle settled.
    Where they differ by more than the ordinary couple of minutes of tails,
    the export ran outside the paid hour and earned the standard export rate
    rather than the event rate. Keeping both is what makes that visible;
    keeping only the first is what made 11-Aug look like a 3.2 kWh shortfall
    when the paid hour was in fact textbook.
    """
    key_start = _iso(start_time)
    key_end   = _iso(end_time)
    if not key_start:
        raise ValueError("record_local_event needs a start time")

    try:
        kwh = float(export_kwh)
    except (TypeError, ValueError):
        kwh = 0.0
    try:
        rate = float(rate_per_kwh)
    except (TypeError, ValueError):
        rate = 1.0

    try:
        win = round(float(window_kwh), 3) if window_kwh is not None else None
    except (TypeError, ValueError):
        win = None

    row = {
        "start_time":   key_start,
        "end_time":     key_end,
        "export_kwh":   round(kwh, 3),
        # None means "not measured", never zero — an event whose snapshots we
        # do not have must not read as an hour that exported nothing.
        "window_kwh":   win,
        "rate_per_kwh": rate,
        "estimate_gbp": round(kwh * rate, 2),
        "driver":       driver or "",
        "log_path":     log_path or "",
    }

    rows = [r for r in (ledger.get("local") or [])
            if isinstance(r, dict) and r.get("start_time") != key_start]
    rows.append(row)
    ledger["local"] = sorted(rows, key=lambda r: r.get("start_time") or "",
                             reverse=True)
    return ledger


# ======================================================================
# The display view
# ======================================================================

def summarise(ledger, now=None, recent=12):
    """Build the view the dashboard and the device states render from.

    Everything money-shaped is GBP as a float rounded to pence. Anything not
    yet known is None, never 0.0 - see the module docstring.
    """
    now    = now or datetime.now(timezone.utc)
    axle   = ledger.get("axle") or {}
    txs    = [t for t in (axle.get("transactions") or []) if isinstance(t, dict)]
    locals_ = [r for r in (ledger.get("local") or []) if isinstance(r, dict)]

    balance = axle.get("balance") or {}
    lifetime_gbp  = _pence_to_gbp(balance.get("total_earnings_pence"))
    available_gbp = _pence_to_gbp(balance.get("current_balance_pence"))
    threshold_gbp = _pence_to_gbp(balance.get("minimum_withdrawal_threshold_pence"))

    # Split the lifetime figure by where the money came from. Lumping a GBP 25
    # referral in with grid earnings flatters the battery by a wide margin.
    by_kind = {"events_gbp": 0.0, "top_ups_gbp": 0.0,
               "other_gbp": 0.0, "withdrawals_gbp": 0.0}
    for tx in txs:
        gbp  = _pence_to_gbp(tx.get("credit_pence")) or 0.0
        kind = (tx.get("transaction_type") or "").strip()
        if kind in FLEX_EVENT_TYPES:
            by_kind["events_gbp"] += gbp
        elif kind in TOP_UP_TYPES:
            by_kind["top_ups_gbp"] += gbp
        elif kind in WITHDRAWAL_TYPES:
            by_kind["withdrawals_gbp"] += gbp
        else:
            by_kind["other_gbp"] += gbp
    by_kind = {k: round(v, 2) for k, v in by_kind.items()}

    settled_by_start = {}
    for tx in txs:
        if (tx.get("transaction_type") or "").strip() in FLEX_EVENT_TYPES:
            key = _window_key(tx.get("start_time"))
            if key:
                settled_by_start[key] = tx

    # One row per event, from the union of the windows either source knows
    # about. Axle's list is authoritative for "did this event happen"; ours
    # covers a window Axle has not published yet.
    windows = {}
    for ev in (axle.get("events") or []):
        if isinstance(ev, dict):
            key = _window_key(ev.get("start_time"))
            if key:
                windows[key] = {"start": ev.get("start_time"),
                                "end":   ev.get("end_time"),
                                "settled_via": ev.get("settled_via")}
    for tx in txs:
        if (tx.get("transaction_type") or "").strip() in FLEX_EVENT_TYPES:
            key = _window_key(tx.get("start_time"))
            if key and key not in windows:
                windows[key] = {"start": tx.get("start_time"),
                                "end":   tx.get("end_time"),
                                "settled_via": None}
    for row in locals_:
        key = _window_key(row.get("start_time"))
        if key and key not in windows:
            windows[key] = {"start": row.get("start_time"),
                            "end":   row.get("end_time"),
                            "settled_via": None}

    local_by_start = {}
    for row in locals_:
        key = _window_key(row.get("start_time"))
        if key:
            local_by_start[key] = row

    events = []
    for key in sorted(windows, reverse=True):
        win = windows[key]
        tx  = settled_by_start.get(key)
        loc = local_by_start.get(key)
        paid_gbp = _pence_to_gbp(tx.get("credit_pence")) if tx else None
        paid_kwh = abs(tx["flex_kwh"]) if tx and isinstance(tx.get("flex_kwh"), (int, float)) else None
        run_kwh  = loc.get("export_kwh") if loc else None
        # The in-window figure is the ONLY one comparable with Axle's. Older
        # rows predate it being recorded and carry None; they fall back to the
        # run total, which is what they always were — never to a zero.
        our_kwh  = (loc.get("window_kwh") if loc and loc.get("window_kwh") is not None
                    else run_kwh)
        measured_in_window = bool(loc and loc.get("window_kwh") is not None)

        # Export outside the paid hour. Real, but paid at the ordinary export
        # rate rather than the event rate, so it is NOT a shortfall against
        # Axle and must not be shown in the same column as one.
        outside_kwh = None
        if measured_in_window and run_kwh is not None:
            gap = round(run_kwh - loc["window_kwh"], 3)
            # The driver deliberately runs two minutes either side, so a small
            # gap is the design working. Only flag a genuine over-run.
            outside_kwh = gap if gap > 0.5 else None

        events.append({
            "start":       win["start"],
            "end":         win["end"],
            "start_local": _local_str(win["start"], "%d %b %Y %H:%M"),
            "end_local":   _local_str(win["end"], "%H:%M"),
            # settled is a fact about whether Axle has published a figure, and
            # nothing else. A settled event may legitimately have paid nothing.
            "settled":     tx is not None,
            "paid_gbp":    paid_gbp,
            "paid_kwh":    round(paid_kwh, 3) if paid_kwh is not None else None,
            "our_kwh":     our_kwh,
            "run_kwh":     run_kwh,
            "in_window":   measured_in_window,
            "outside_kwh": outside_kwh,
            # The baseline: what Axle reckons the house would have done anyway.
            # Only meaningful when both sides cover the same hour, so it is
            # withheld rather than guessed when ours does not.
            "diff_kwh":    (round(our_kwh - paid_kwh, 3)
                            if (measured_in_window and our_kwh is not None
                                and paid_kwh is not None) else None),
            "settled_via": win.get("settled_via"),
            "driver":      (loc or {}).get("driver") or "",
        })

    pending = [e for e in events if not e["settled"]]

    # Month to date, on Axle's settled rows only. An estimate has no business
    # in a figure presented as earnings.
    month_key = to_london(now).strftime("%Y-%m") if now else None
    month_gbp = 0.0
    month_has_rows = False
    for tx in txs:
        stamp = _window_key(tx.get("start_time"))
        if stamp and month_key and stamp[:7] == month_key:
            month_gbp += _pence_to_gbp(tx.get("credit_pence")) or 0.0
            month_has_rows = True

    return {
        "lifetime_gbp":       lifetime_gbp,
        "available_gbp":      available_gbp,
        "withdraw_threshold_gbp": threshold_gbp,
        "can_withdraw":       balance.get("can_withdraw"),
        "by_kind":            by_kind,
        "events_total":       len(events),
        "events_settled":     len(events) - len(pending),
        "events_pending":     len(pending),
        # None, not 0.0, when this month has no settled rows at all - the two
        # look identical on a tile and mean opposite things in August.
        "month_to_date_gbp":  round(month_gbp, 2) if month_has_rows else None,
        "events":             events[:recent],
        "axle_fetched_at":    (ledger.get("axle") or {}).get("fetched_at"),
        "axle_fetched_local": _local_str((ledger.get("axle") or {}).get("fetched_at"),
                                         "%d/%m %H:%M"),
        "axle_age_days":      _age_days((ledger.get("axle") or {}).get("fetched_at"), now),
        "load_error":         ledger.get("load_error"),
    }


# ======================================================================
# Helpers
# ======================================================================

def _pence_to_gbp(pence):
    """Pence to GBP, preserving None. None means "not known", and it has to
    survive all the way to the renderer to stay distinguishable from zero."""
    if pence is None:
        return None
    try:
        return round(int(pence) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _iso(value):
    """Normalise a datetime or string to an ISO-8601 string, or ''."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _parse(value):
    """Parse an ISO-8601 string to an aware datetime, or None.

    Axle stamps everything +00:00. A naive value is treated as UTC rather than
    as local: guessing local here would shift a summer event by an hour, and an
    hour is enough to move an event into the wrong day on the tile.
    """
    if value is None or value == "":
        return None
    if hasattr(value, "tzinfo"):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _window_key(value):
    """A stable per-event key: the UTC minute the window opens."""
    dt = _parse(value)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M") if dt else None


def _local_str(value, fmt):
    dt = _parse(value)
    if not dt:
        return ""
    try:
        return to_london(dt).strftime(fmt)
    except Exception:
        return dt.strftime(fmt)


def _age_days(value, now):
    dt = _parse(value)
    if not dt or not now:
        return None
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return round((now - dt).total_seconds() / 86400.0, 1)
