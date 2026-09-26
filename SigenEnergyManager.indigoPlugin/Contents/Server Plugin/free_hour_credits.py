#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    free_hour_credits.py
# Description: What Octopus owes back for electricity used in a booked Weekend
#              Happy Hour, and whether it has been paid. Pure stdlib: claims in,
#              credits in, a verdict and the plain-English words out.
# Author:      CliveS & Claude Opus 5.5
# Date:        26-09-2026
# Version:     1.0
#
# WHY THIS EXISTS
# ---------------
# A booked free hour is free up to 16 kWh, credited "at the energy price that's
# active at the time" (Octopus's Weekend Happy Hours page, read 22-Sep-2026). The
# plugin fills the battery in that hour, so on 27-Sep-2026 the first real one was
# worth up to about GBP 7.80 across two hours. CliveS asked for the credit to be
# checked every day and shown until Octopus pays it.
#
# WHAT IS NOT KNOWN YET, AND HOW THIS COPES
# -----------------------------------------
# No free hour on this account has yet been paid in a form anyone has seen. The
# only earlier one (16-Aug-2026) used almost no grid power (2.3 kWh imported in
# the whole month) and left no credit line. Older Octopus free-electricity
# sessions were paid as a Credit with reasonCode FREE_ELECTRICITY_REWARD (Sept and
# Oct 2025 on this account), so that is the first thing looked for, with looser
# title and reason matches beside it. Every other credit posted since the free
# hour is carried through for display, so a payment that arrives under a name
# this module does not recognise is still in front of a person rather than
# silently ignored. If Octopus turns out to pay by not charging the units on the
# bill instead, no credit will ever appear and the claim will go LATE after two
# weeks, which is the signal to change the matcher.
#
# THE RULES
# ---------
# * One claim per Sunday (local date), holding that day's booked hours. Octopus
#   may pay per hour or per day; credits are summed against the day.
# * kWh comes from Octopus's own meter reading for the hour (what they bill on)
#   once it has settled, and from the inverter's grid counter until then, labelled
#   as an estimate. Capped at 16 kWh an hour, the free allowance.
# * Credits are matched oldest claim first, and only to a claim dated on or before
#   the credit. A credit is never counted twice.
# * paid:  credited >= expected - 5p.   short: some credit, but more than 5p less.
#   late:  nothing credited 14 days after the free hour.  A short claim stays open
#   for the rest; a paid one is shown for 14 days and then dropped.
# * An absent reading is never zero (the estate's absent-state rule): no kWh at
#   all is MEASURING, not "nothing owed".

from datetime import datetime, timedelta, timezone

FREE_KWH_CAP_PER_HOUR = 16.0
MATCH_TOLERANCE_P     = 5
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


def new_ledger():
    return {"version": 1, "claims": {}, "assigned_credit_ids": []}


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
    never replaced by a later day's price.
    """
    for h in hours:
        start, end = h.get("start"), h.get("end")
        if start is None or end is None or end > now:
            continue
        if end < now - timedelta(days=CLAIM_LOOKBACK_DAYS):
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
        return int(round(sum(min(h["meter_kwh"], FREE_KWH_CAP_PER_HOUR) * h["rate_p"]
                             for h in hours)))
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
    seen = set(ledger.setdefault("assigned_credit_ids", []))
    fresh = sorted((c for c in credits if str(c.get("id")) not in seen
                    and is_free_hour_credit(c) and _num(c.get("amount_p")) is not None),
                   key=lambda c: (str(c.get("posted")), str(c.get("id"))))
    assigned = []
    for c in fresh:
        for day in sorted(ledger.get("claims", {})):
            claim = ledger["claims"][day]
            if claim.get("closed_at") or day > str(c.get("posted")):
                continue
            exp = expected_pence(claim)
            paid = sum(x["amount_p"] for x in claim["credits"])
            if exp is not None and paid >= exp - MATCH_TOLERANCE_P:
                continue                       # already paid in full
            claim["credits"].append({"id": str(c.get("id")), "posted": str(c.get("posted")),
                                     "amount_p": int(round(_num(c["amount_p"]))),
                                     "title": c.get("title") or "",
                                     "reason": c.get("reason") or ""})
            ledger["assigned_credit_ids"].append(str(c.get("id")))
            assigned.append(c)
            break
    return assigned


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
        closed = _parse(claim.get("closed_at"))
        keep = KEEP_PAID_DAYS if st["state"] == STATE_PAID else KEEP_NOTHING_DUE_DAYS
        if closed and st["state"] not in _OPEN and now - closed > timedelta(days=keep):
            del ledger["claims"][day]


# ================================================================
# Words — plain UK English, ASCII except the pound sign
# ================================================================

POUND = "£"


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


def notifications(ledger, now):
    """[(title, body)] still to send, marking each as sent. Deduped on the claim's
    date and the kind of news — never on the wording, which carries figures."""
    out = []
    for day in sorted(ledger.get("claims", {})):
        claim = ledger["claims"][day]
        st = status(claim, now)
        sent = claim.setdefault("notified", {})
        when = day_words(day)
        if st["state"] == STATE_PAID and not sent.get("paid"):
            sent["paid"] = True
            out.append((
                "Octopus has paid for the free hours",
                f"Octopus has credited {money(st['paid_p'])} to the account for the free "
                f"electricity on {when}. The free hours used {kwh_words(st['kwh'])} at "
                f"{_rate_words(claim)}, so that is the amount we expected."))
        elif st["state"] == STATE_SHORT and sent.get("short_p") != st["paid_p"]:
            sent["short_p"] = st["paid_p"]
            out.append((
                f"Octopus paid {money(st['owed_p'])} short for the free hours",
                f"Octopus has credited {money(st['paid_p'])} for the free electricity on "
                f"{when}, but the free hours used {kwh_words(st['kwh'])} at "
                f"{_rate_words(claim)}, which comes to {money(st['expected_p'])}. That leaves "
                f"{money(st['owed_p'])} unpaid, and the Energy page will keep showing it "
                f"until the rest arrives."))
        elif st["state"] == STATE_LATE and not sent.get("late"):
            sent["late"] = True
            out.append((
                "Still waiting for the free-hour credit",
                f"It is {st['days_waiting']} days since the free hours on {when} and Octopus "
                f"has not credited the {money(st['expected_p'])} it owes for "
                f"{kwh_words(st['kwh'])} of electricity. It may be worth asking them."))
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
