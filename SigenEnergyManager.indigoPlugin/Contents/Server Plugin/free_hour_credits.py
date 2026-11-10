#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    free_hour_credits.py
# Description: What Octopus owes back for electricity used in a booked Weekend
#              Happy Hour, and whether it has been paid. Pure stdlib: claims in,
#              credits in, a verdict and the plain-English words out.
# Author:      CliveS & Claude Opus 5.5
# Date:        02-10-2026
# Version:     1.4 (10-11-2026, Claude Fable 5.2: the notes say "free hour" for a day with one hour)
#              1.3 (02-11-2026: settled hours stay settled, credits match their own hour, ledger version 3)
#
# WHY THIS EXISTS
# ---------------
# A booked free hour is free up to 16 kWh, credited "at the energy price that's
# active at the time" (Octopus's Weekend Happy Hours page, read 22-Sep-2026). The
# plugin fills the battery in that hour, so on 27-Sep-2026 the first real one was
# worth up to about GBP 7.80 across two hours. CliveS asked for the credit to be
# checked every day and shown until Octopus pays it.
#
# HOW OCTOPUS PAYS (measured 02-11-2026, every credit from 27-Sep to 1-Nov-2026)
# -----------------------------------------------------------------------------
# One Credit per hour, reasonCode WEEKEND_HAPPY_HOUR, 7 to 10 days later. Each is
# the metered kWh times the published price for that hour, VAT included as it stood,
# rounded UP to the whole penny: nine of nine exact. Octopus re-issues a credit's id
# when it bills the account (3710781562 became 3744767156), so the id cannot say
# whether a credit has been counted; its date, amount and reason can. Every other
# credit posted since the free hour is carried through for display, so a payment
# that arrives under a new name is still in front of a person.
#
# THE RULES
# ---------
# * One claim per Sunday (local date), holding that day's booked hours. Octopus
#   may pay per hour or per day; credits are summed against the day.
# * kWh comes from Octopus's own meter reading for the hour (what they bill on)
#   once it has settled, and from the inverter's grid counter until then, labelled
#   as an estimate. Capped at 16 kWh an hour, the free allowance.
# * Credits are matched only to a claim dated on or before the credit. First to the
#   oldest unpaid HOUR worth that sum within 1p, then to a claim whose outstanding sum
#   matches within 5p, else to the oldest claim with a known sum outstanding. A credit
#   is never counted twice, and one with no such claim waits for the next run rather
#   than landing on a MEASURING claim.
# * An hour whose claim has been dropped is remembered in settled_hours until it is
#   past the look-back, so a settled Sunday cannot come back as unpaid.
# * A claim still MEASURING 30 days after its last hour (CLAIM_LOOKBACK_DAYS — the
#   same horizon after which an hour is no longer recorded) is closed, and dropped
#   7 days later like a nothing-due one.
# * paid:  credited >= expected - 5p.   short: some credit, but more than 5p less.
#   late:  nothing credited 14 days after the free hour.  A short claim stays open
#   for the rest; a paid one is shown for 14 days and then dropped.
# * An absent reading is never zero (the estate's absent-state rule): no kWh at
#   all is MEASURING, not "nothing owed".

import math
from collections import Counter
from datetime import datetime, timedelta, timezone

FREE_KWH_CAP_PER_HOUR = 16.0
MATCH_TOLERANCE_P     = 5
HOUR_TOLERANCE_P      = 1
LATE_AFTER_DAYS       = 14
KEEP_PAID_DAYS        = 14
KEEP_NOTHING_DUE_DAYS = 7
CLAIM_LOOKBACK_DAYS   = 30
NOTHING_DUE_P         = 1          # under a penny owed is nothing owed

# A credit Octopus calls one of these is a free-electricity payment.
_REASONS = ("FREE_ELECTRICITY", "HAPPY_HOUR", "SAVING_SESSION", "SAVINGSESSION")
_TITLES  = ("free electricity", "happy hour", "saving session", "free hour")
# ...and these never are, whatever else they say.
_NOT_REASONS = ("POINTS_REDEEMED", "REVERSED_ACCOUNT_CHARGE")

STATE_MEASURING   = "measuring"
STATE_NOTHING_DUE = "nothing_due"
STATE_AWAITING    = "awaiting"
STATE_LATE        = "late"
STATE_SHORT       = "short"
STATE_PAID        = "paid"
_OPEN = (STATE_AWAITING, STATE_LATE, STATE_SHORT)


LEDGER_VERSION = 3


def new_ledger():
    return {"version": LEDGER_VERSION, "claims": {}, "assigned_credit_keys": [],
            "settled_hours": {}}


def credit_key(credit):
    """What identifies a credit: Octopus changes its id when it bills the account."""
    return f"{credit.get('posted')}|{int(round(_num(credit.get('amount_p')) or 0))}|{credit.get('reason') or ''}"


def upgrade(ledger):
    """Bring a stored ledger up to LEDGER_VERSION. Returns True if it changed.

    Version 2 (05-10-2026): meter readings stored before it counted one extra
    half hour per hour (the slot starting at the hour's end, which Octopus's
    consumption endpoint also returns). 27-Sep-2026 read 29.1 kWh against a real
    25.2 and reported a 93p shortfall on a credit that was right to the penny.
    Every open claim's readings are dropped so they are read again correctly;
    the inverter estimate stands in until then, as it always does.

    Version 3 (02-11-2026): counted credits are remembered by credit_key, because
    Octopus re-issues ids at billing, and dropped hours in settled_hours.
    """
    if not isinstance(ledger, dict):
        return False
    try:
        version = int(ledger.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    if version >= LEDGER_VERSION:
        return False
    if version < 2:
        for claim in (ledger.get("claims") or {}).values():
            if claim.get("closed_at"):
                continue
            for hour in (claim.get("hours") or {}).values():
                hour["meter_kwh"] = None
    ledger["assigned_credit_keys"] = [credit_key(c)
                                      for claim in (ledger.get("claims") or {}).values()
                                      for c in claim.get("credits", [])]
    ledger.setdefault("settled_hours", {})
    ledger.pop("assigned_credit_ids", None)
    ledger["version"] = LEDGER_VERSION
    return True


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None             # NaN is not a number here


# ================================================================
# Recording
# ================================================================

def record_hours(ledger, hours, now, local_date_of):
    """Add booked hours that have ENDED to their day's claim.

    hours: [{"id", "start": datetime, "end": datetime, "rate_p": float|None}]
    local_date_of: callable(datetime) -> "YYYY-MM-DD" in Europe/London.
    An hour already recorded keeps its rate, so a price looked up on the day is
    never replaced by a later day's price. A settled hour is never recorded again:
    the event stays listed long after its claim has been dropped.
    """
    settled = ledger.get("settled_hours") or {}
    for h in hours:
        start, end = h.get("start"), h.get("end")
        if start is None or end is None or end > now:
            continue
        if end < now - timedelta(days=CLAIM_LOOKBACK_DAYS):
            continue
        if str(h.get("id")) in settled:
            continue
        day = local_date_of(start)
        claim = _claim(ledger, day)
        key = str(h.get("id"))
        if key in claim["hours"]:
            if claim["hours"][key].get("rate_p") is None and _num(h.get("rate_p")) is not None:
                claim["hours"][key]["rate_p"] = _num(h.get("rate_p"))
            continue
        claim["hours"][key] = {"start": _iso(start), "end": _iso(end),
                               "rate_p": _num(h.get("rate_p")), "meter_kwh": None}


def hour_wanted(ledger, hour_id, end, now):
    """True when record_hours has a use for this hour: it is new, or it has no price."""
    key = str(hour_id)
    if key in (ledger.get("settled_hours") or {}):
        return False
    if end is None or end < now - timedelta(days=CLAIM_LOOKBACK_DAYS):
        return False
    for claim in ledger.get("claims", {}).values():
        if key in claim["hours"]:
            return claim["hours"][key].get("rate_p") is None
    return True


def _claim(ledger, day):
    return ledger.setdefault("claims", {}).setdefault(
        day, {"date": day, "hours": {}, "credits": [], "inverter_kwh": None,
              "notified": {}, "closed_at": None})


def set_meter_kwh(ledger, hour_id, kwh):
    for claim in ledger.get("claims", {}).values():
        hour = claim["hours"].get(str(hour_id))
        if hour is not None and _num(kwh) is not None:
            hour["meter_kwh"] = round(max(0.0, _num(kwh)), 3)


def add_inverter_kwh(ledger, day, kwh):
    """The inverter's grid-import estimate for a day's free hours (accumulates,
    because two adjacent hours may be two import windows). The import ends AT the
    window end, before the hourly poll has recorded the hours, so this may be the
    first thing to create the day's claim."""
    k = _num(kwh)
    if k is None:
        return
    claim = _claim(ledger, day)
    claim["inverter_kwh"] = round((claim.get("inverter_kwh") or 0.0) + max(0.0, k), 3)


def hours_needing_meter(ledger, now, settle_hours=24):
    """Hours whose meter reading should have settled and has not been read."""
    out = []
    for claim in ledger.get("claims", {}).values():
        if claim.get("closed_at"):
            continue
        for hid, h in claim["hours"].items():
            end = _parse(h.get("end"))
            if h.get("meter_kwh") is None and end and now - end >= timedelta(hours=settle_hours):
                out.append((hid, _parse(h["start"]), end))
    return out


# ================================================================
# The money
# ================================================================

def claim_energy(claim):
    """(kWh, source) — "meter" when every hour has settled, else "inverter", else
    (None, None). Each hour capped at the 16 kWh allowance."""
    hours = list(claim.get("hours", {}).values())
    if hours and all(h.get("meter_kwh") is not None for h in hours):
        return (round(sum(min(h["meter_kwh"], FREE_KWH_CAP_PER_HOUR) for h in hours), 3),
                "meter")
    inv = claim.get("inverter_kwh")
    if inv is not None and hours:
        return round(min(inv, FREE_KWH_CAP_PER_HOUR * len(hours)), 3), "inverter"
    return None, None


def hour_worth_p(hour):
    """What Octopus pays for one hour, in pence, or None without a reading and a
    price. Rounded up to the penny, as every one of its credits has been."""
    kwh, rate = _num(hour.get("meter_kwh")), _num(hour.get("rate_p"))
    if kwh is None or rate is None:
        return None
    return int(math.ceil(round(min(kwh, FREE_KWH_CAP_PER_HOUR) * rate, 6)))


def expected_pence(claim):
    """What Octopus should credit, in pence, or None when it cannot be worked out.

    Priced hour by hour at that hour's rate once the meter has settled; before
    that the day's estimate at the day's first known rate.
    """
    hours = list(claim.get("hours", {}).values())
    rates = [h.get("rate_p") for h in hours if h.get("rate_p") is not None]
    if not hours or not rates:
        return None
    if all(h.get("meter_kwh") is not None for h in hours):
        if any(h.get("rate_p") is None for h in hours):
            return None
        return sum(hour_worth_p(h) for h in hours)
    kwh, _src = claim_energy(claim)
    if kwh is None:
        return None
    return int(round(kwh * rates[0]))


def is_free_hour_credit(credit):
    if credit.get("reversed"):
        return False
    reason = str(credit.get("reason") or "").upper()
    title  = str(credit.get("title") or "").lower()
    if any(r in reason for r in _NOT_REASONS):
        return False
    return any(r in reason for r in _REASONS) or any(t in title for t in _TITLES)


def assign_credits(ledger, credits):
    """Attach new free-electricity credits to the oldest open claim they can pay.

    credits: [{"id", "posted": "YYYY-MM-DD", "amount_p": int, "title", "reason",
               "reversed": bool}]
    Returns the credits that were assigned on this call.
    """
    seen = Counter(ledger.setdefault("assigned_credit_keys", []))
    fresh = []
    for c in sorted((c for c in credits if is_free_hour_credit(c)
                     and _num(c.get("amount_p")) is not None),
                    key=lambda c: (str(c.get("posted")), str(c.get("id")))):
        key = credit_key(c)
        if seen[key] > 0:
            seen[key] -= 1                     # counted already, whatever its id is now
        else:
            fresh.append(c)
    assigned = []
    for c in fresh:
        amount = int(round(_num(c["amount_p"])))
        found = _hour_paid_by(ledger, amount, str(c.get("posted")))
        if found is not None:
            _attach(ledger, found[0], c, amount, found[1])
            assigned.append(c)
            continue
        # review 02-10-2026: a claim whose expected value is None (MEASURING) used to
        # be eligible, and since "paid in full" could never be true of it, it took
        # every later credit and left the Sunday Octopus actually paid going LATE.
        # Only claims with a known amount outstanding are candidates now; among them
        # one whose outstanding sum matches the credit (within MATCH_TOLERANCE_P) wins,
        # else the oldest. No candidate -> the credit stays unassigned and is tried
        # again on the next run, once the meter has settled.
        candidates = []
        for day in sorted(ledger.get("claims", {})):
            claim = ledger["claims"][day]
            if claim.get("closed_at") or day > str(c.get("posted")):
                continue
            exp = expected_pence(claim)
            if exp is None:
                continue                       # still measuring
            paid = sum(x["amount_p"] for x in claim["credits"])
            if paid >= exp - MATCH_TOLERANCE_P:
                continue                       # already paid in full
            candidates.append((claim, exp - paid))
        if not candidates:
            continue
        exact = [cl for cl, owed in candidates if abs(owed - amount) <= MATCH_TOLERANCE_P]
        claim = exact[0] if exact else candidates[0][0]
        _attach(ledger, claim, c, amount, None)
        assigned.append(c)
    return assigned


def _hour_paid_by(ledger, amount, posted):
    """(claim, hour id) for the oldest unpaid hour worth this credit, or None."""
    for day in sorted(ledger.get("claims", {})):
        claim = ledger["claims"][day]
        if claim.get("closed_at") or day > posted:
            continue
        taken = {x.get("hour") for x in claim["credits"]}
        for hid in sorted(claim["hours"], key=lambda k: claim["hours"][k].get("start") or ""):
            worth = hour_worth_p(claim["hours"][hid])
            if hid not in taken and worth is not None \
                    and abs(worth - amount) <= HOUR_TOLERANCE_P:
                return claim, hid
    return None


def _attach(ledger, claim, credit, amount, hour_id):
    entry = {"id": str(credit.get("id")), "posted": str(credit.get("posted")),
             "amount_p": amount, "title": credit.get("title") or "",
             "reason": credit.get("reason") or ""}
    if hour_id is not None:
        entry["hour"] = hour_id
    claim["credits"].append(entry)
    ledger["assigned_credit_keys"].append(credit_key(credit))


def status(claim, now):
    kwh, source = claim_energy(claim)
    exp  = expected_pence(claim)
    paid = sum(c["amount_p"] for c in claim.get("credits", []))
    ends = [_parse(h.get("end")) for h in claim.get("hours", {}).values()]
    ends = [e for e in ends if e]
    last_end = max(ends) if ends else None
    days = (now - last_end).days if last_end else 0
    if exp is None:
        state = STATE_MEASURING
    elif exp < NOTHING_DUE_P and not claim.get("credits"):
        state = STATE_NOTHING_DUE
    elif claim.get("credits"):
        state = STATE_PAID if paid >= exp - MATCH_TOLERANCE_P else STATE_SHORT
    else:
        state = STATE_LATE if days >= LATE_AFTER_DAYS else STATE_AWAITING
    return {"state": state, "expected_p": exp, "paid_p": paid,
            "owed_p": (max(0, exp - paid) if exp is not None else None),
            "kwh": kwh, "kwh_source": source, "days_waiting": days,
            "hours": len(claim.get("hours", {}))}


def prune(ledger, now):
    """Close and drop what no longer needs showing. Open claims are never dropped."""
    for day in list(ledger.get("claims", {})):
        claim = ledger["claims"][day]
        st = status(claim, now)
        if st["state"] in (STATE_PAID, STATE_NOTHING_DUE) and not claim.get("closed_at"):
            claim["closed_at"] = _iso(now)
        # review 02-10-2026: a claim stuck MEASURING (no meter, no estimate, or no
        # rate) was never closed and sat for ever. After CLAIM_LOOKBACK_DAYS — the
        # horizon past which record_hours stops recording an hour — give up on it.
        # A claim with no hours (made by add_inverter_kwh alone) ages from its date.
        age = st["days_waiting"]
        if not claim.get("hours"):
            try:
                age = (now.date() - datetime.strptime(day, "%Y-%m-%d").date()).days
            except ValueError:
                age = 0
        if (st["state"] == STATE_MEASURING and not claim.get("closed_at")
                and age >= CLAIM_LOOKBACK_DAYS):
            claim["closed_at"] = _iso(now)
        closed = _parse(claim.get("closed_at"))
        keep = KEEP_PAID_DAYS if st["state"] == STATE_PAID else KEEP_NOTHING_DUE_DAYS
        if closed and st["state"] not in _OPEN and now - closed > timedelta(days=keep):
            settled = ledger.setdefault("settled_hours", {})
            for hid in claim.get("hours", {}):
                settled[hid] = day
            del ledger["claims"][day]
    # An hour past the look-back cannot be recorded again, so it needs no remembering.
    forget = (now - timedelta(days=CLAIM_LOOKBACK_DAYS + 1)).strftime("%Y-%m-%d")
    settled = ledger.get("settled_hours") or {}
    for hid in [h for h, day in settled.items() if day < forget]:
        del settled[hid]


# ================================================================
# Words — plain UK English, ASCII except the pound sign
# ================================================================

POUND = "\u00a3"


def money(pence):
    p = int(round(pence))
    if abs(p) < 100:
        return f"{p}p"
    return f"{POUND}{p / 100:.2f}"


def kwh_words(kwh):
    k = round(float(kwh), 1)
    return f"{int(k)} kWh" if k == int(k) else f"{k:.1f} kWh"


def day_words(day):
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return day
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def _rate_words(claim):
    rates = sorted({round(h["rate_p"], 1) for h in claim["hours"].values()
                    if h.get("rate_p") is not None})
    if len(rates) == 1:
        return f"{rates[0]:.1f}p"
    return "the rates at the time"


def _hours_words(claim):
    """"free hour" for a day with one booked hour, "free hours" otherwise."""
    return "free hour" if len(claim.get("hours") or {}) == 1 else "free hours"


def notifications(ledger, now):
    """[(title, body)] still to send, marking each as sent. Deduped on the claim's
    date and the kind of news — never on the wording, which carries figures."""
    out = []
    for day in sorted(ledger.get("claims", {})):
        claim = ledger["claims"][day]
        st = status(claim, now)
        sent = claim.setdefault("notified", {})
        when = day_words(day)
        hrs  = _hours_words(claim)
        if st["state"] == STATE_PAID and not sent.get("paid"):
            sent["paid"] = True
            out.append((
                f"Octopus has paid for the {hrs}",
                f"Octopus has credited {money(st['paid_p'])} to the account for the free "
                f"electricity on {when}. The {hrs} used {kwh_words(st['kwh'])} at "
                f"{_rate_words(claim)}, so that is the amount we expected."))
        elif st["state"] == STATE_SHORT and sent.get("short_p") != st["paid_p"]:
            sent["short_p"] = st["paid_p"]
            out.append((
                f"Octopus paid {money(st['owed_p'])} short for the {hrs}",
                f"Octopus has credited {money(st['paid_p'])} for the free electricity on "
                f"{when}, but the {hrs} used {kwh_words(st['kwh'])} at "
                f"{_rate_words(claim)}, which comes to {money(st['expected_p'])}. That leaves "
                f"{money(st['owed_p'])} unpaid, and the Energy page will keep showing it "
                f"until the rest arrives."))
        elif st["state"] == STATE_LATE and not sent.get("late"):
            sent["late"] = True
            out.append((
                "Still waiting for the free-hour credit",
                f"It is {st['days_waiting']} days since the {hrs} on {when} and Octopus "
                f"has not credited the {money(st['expected_p'])} it owes for "
                f"{kwh_words(st['kwh'])} of electricity. It may be worth asking them."))
    return out


def _windows(claim):
    """The claim's hours as (start, end) datetimes, adjacent hours joined."""
    spans = sorted((_parse(h["start"]), _parse(h["end"])) for h in claim["hours"].values())
    out = []
    for start, end in spans:
        if out and out[-1][1] == start:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def for_display(ledger, now, other_credits=()):
    """What the dashboard shows: one row per Sunday, newest first."""
    rows = []
    for day in sorted(ledger.get("claims", {}), reverse=True):
        claim = ledger["claims"][day]
        st = status(claim, now)
        starts = sorted(_parse(h["start"]) for h in claim["hours"].values())
        ends   = sorted(_parse(h["end"]) for h in claim["hours"].values())
        others = [c for c in other_credits
                  if str(c.get("posted")) >= day and not is_free_hour_credit(c)
                  and not c.get("reversed")
                  and not any(r in str(c.get("reason") or "").upper() for r in _NOT_REASONS)]
        rows.append({
            "date": day, "state": st["state"],
            "start": _iso(starts[0]) if starts else None,
            "end": _iso(ends[-1]) if ends else None,
            "windows": [{"start": _iso(a), "end": _iso(b)} for a, b in _windows(claim)],
            "hours": st["hours"], "kwh": st["kwh"], "kwh_source": st["kwh_source"],
            "rate_p": sorted({h["rate_p"] for h in claim["hours"].values()
                              if h.get("rate_p") is not None}),
            "expected_p": st["expected_p"], "paid_p": st["paid_p"], "owed_p": st["owed_p"],
            "days_waiting": st["days_waiting"],
            "credits": list(claim.get("credits", [])),
            # Credits since the free hour that this module does not recognise as a
            # free-electricity payment, so an unfamiliar one is still visible.
            "other_credits": [{"posted": str(c.get("posted")), "amount_p": c.get("amount_p"),
                               "title": c.get("title") or "", "reason": c.get("reason") or ""}
                              for c in others][:5],
        })
    return rows
