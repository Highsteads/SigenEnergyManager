#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    axle_email.py
# Description: Parse an Axle "Recent grid event - results are in!" settlement
#              email into the payload shape vpp_ledger.import_axle_payload takes.
# Author:      CliveS & Claude Opus 5
# Date:        05-09-2026 23:40
# Version:     1.0
#
# WHY THIS EXISTS
# ---------------
# Axle's earnings API is documented but every path needs an ORGANISATIONAL
# bearer token; a consumer token gets HTTP 403 (ha-axle-vpp issue #19), and four
# independent integrations all stop at the events endpoint. The account page
# cannot be fetched either - it is React Router v7 (so no ?_data= loader URL)
# behind magic-link-only sign-in, so there is no storable credential.
#
# The SETTLEMENT EMAIL is the channel that is already working. Axle send one per
# event, it carries the settled kWh and the money, and it arrives DAYS before the
# account page catches up - which is why the ledger's newest Axle row has always
# been a hand-typed `email-...` row and why import_axle_payload dedupes on the
# window rather than the transaction id.
#
# WHAT IT DELIBERATELY DOES NOT PARSE
# -----------------------------------
# The event's clock times. In the specimen (16-Aug-2026, captured 05-Sep-2026)
# "Event 20:00 -> 21:00" is drawn INSIDE the chart image, not written in the body
# text, so no text parser can reach it. It does not need to: the plugin drove the
# window itself and already holds it, in local ledger rows and in the per-event
# JSONL filenames. So the email supplies the MONEY and the plugin supplies the
# WINDOW, which is both more robust than OCR and impossible to get subtly wrong.
#
# THE BODY IS UNTRUSTED INPUT
# ---------------------------
# Anyone can send mail to the address this is fed from, and a parsed figure lands
# in the earnings ledger. So the sender is checked, the subject is checked, and
# both figures are range-bounded. A duplicate is harmless by construction -
# import_axle_payload is keyed on the window and re-importing is a no-op.

import email
import email.policy
import email.utils
import glob
import os
import re
import time
from datetime import timedelta

# Bounds on what a single domestic flex event can plausibly settle. The site's
# export cap is 4 kW and the longest event seen is two hours, so 20 kWh is
# already generous; the bound exists to stop a malformed or hostile message
# writing a nonsense figure into the earnings record, not to second-guess Axle.
MAX_EVENT_KWH = 20.0
MAX_EVENT_GBP = 100.0

SENDER_DOMAIN  = "axle.energy"
SUBJECT_TOKENS = ("grid event", "results are in")

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

# "you exported 3.87 kWh during the grid event", and the ZERO-EXPORT variant
# "your battery exported *-0.00* *kWh*" - a different template with asterisks
# round the figure, a minus sign, and no Earned tile at all (specimen
# 20-Apr-2026). Both shapes matter: Axle record a nil event as a real
# transaction of 0 kWh and 0 pence, so it belongs in the ledger like any other.
# The sign is absorbed with abs() rather than matched away, so a future template
# writing a real export as negative still reads correctly.
_RE_KWH = re.compile(r"exported[\s*]*(-?[0-9]+(?:\.[0-9]+)?)[\s*]*kWh", re.I)

# "3.87 kWh @ £1.00/kWh" - the rate line under the Earned tile. Preferred over
# the headline "£3.87" because it carries the rate too, which is what lets us
# cross-check the two figures against each other.
_RE_RATE_LINE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*kWh\s*@\s*(?:GBP|£)\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*kWh", re.I)

# THERE IS DELIBERATELY NO LOOSE "find a pound sign" FALLBACK, and there must
# never be one. Axle's low-export template ("This is less than we hoped") states
# the kWh and carries NO money for the event at all - the only pound figure in
# the whole body is the boilerplate "we're still guaranteeing *min GBP 10/month*
# earnings". A bare money regex picks that up: measured on the real 21-May-2026
# mail, it filed GBP 10.00 for an event Axle settled at 4p, an overstatement of
# 250x. The rate line is the ONLY construction that ties a figure to THIS event.
#
# The one permitted fallback is anchored to the phrase "You earned", which
# appears solely in the success template and solely about this event, so it
# cannot reach the monthly-guarantee boilerplate.
_RE_EARNED = re.compile(r"you\s+earned\s*(?:GBP|£)\s*([0-9]+(?:\.[0-9]+)?)", re.I)

# "on Sun 16th August" - note there is NO YEAR in the body, which is why the
# year has to come from the message's own timestamp.
_RE_DATE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+"
    r"([0-9]{1,2})\s*(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)",
    re.I)

_RE_TAG      = re.compile(r"<[^>]+>")
_RE_WS       = re.compile(r"\s+")
_ENTITIES    = {"&pound;": "£", "&#163;": "£", "&#xa3;": "£",
                "&nbsp;": " ", "&#160;": " ", "&amp;": "&", "&#38;": "&",
                "&rarr;": "->", "&#8594;": "->", "&mdash;": "-", "&ndash;": "-"}


def to_text(body):
    """Flatten an HTML or plain-text mail body to searchable text.

    Entities first, then tags, then whitespace. Doing tags first would weld
    `<b>3.87</b>kWh` into `3.87kWh` with no separator, so a space is left behind
    in place of every tag.
    """
    if not body:
        return ""
    text = str(body)
    for ent, ch in _ENTITIES.items():
        text = text.replace(ent, ch).replace(ent.upper(), ch)
    text = _RE_TAG.sub(" ", text)
    return _RE_WS.sub(" ", text).strip()


def is_settlement_email(sender, subject):
    """True when this message is an Axle per-event settlement mail.

    Both halves are required. The subject alone is guessable by anyone, and the
    figures parsed here are written into an earnings record.
    """
    if not sender or SENDER_DOMAIN not in str(sender).lower():
        return False
    subj = (subject or "").lower()
    return all(tok in subj for tok in SUBJECT_TOKENS)


def event_date_from_body(text, received_at):
    """The event's calendar date, from a body that never states the year.

    The year comes from the message's own timestamp. A settlement mail arrives
    days AFTER its event (three days, in the specimen), so a parsed date landing
    in the future means the event belongs to the previous year - the 31st of
    December settled on the 2nd of January. Without that, one event a year is
    filed twelve months out.
    """
    m = _RE_DATE.search(text or "")
    if not m or not received_at:
        return None
    day   = int(m.group(1))
    month = _MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        guess = received_at.date().replace(month=month, day=day)
    except ValueError:
        return None      # e.g. 31st February in a malformed mail
    if guess > received_at.date() + timedelta(days=1):
        try:
            guess = guess.replace(year=guess.year - 1)
        except ValueError:
            return None  # 29th February in a non-leap previous year
    return guess


def parse_settlement_email(sender, subject, body, received_at, window_lookup=None):
    """Turn one settlement email into an import_axle_payload payload.

    window_lookup(date) -> (start_iso, end_iso) or None. The plugin passes a
    lookup over its OWN ledger rows, because it drove the window and already
    knows it to the second; the email only draws it inside a chart image.

    Returns {"payload": dict|None, "note": str}. `payload` is None whenever the
    message is not a settlement mail or could not be trusted, and `note` says
    which - never a silent skip, because a settlement that quietly fails to land
    is the exact failure this whole feed exists to end.
    """
    if not is_settlement_email(sender, subject):
        return {"payload": None, "note": "not an Axle settlement email"}

    text = to_text(body)

    m_kwh = _RE_KWH.search(text)
    if not m_kwh:
        return {"payload": None, "note": "no exported kWh figure in the body"}
    kwh = abs(float(m_kwh.group(1)))     # the nil template writes it as -0.00

    rate = None
    m_rate = _RE_RATE_LINE.search(text)
    if m_rate:
        gbp  = float(m_rate.group(1)) * float(m_rate.group(2))
        rate = float(m_rate.group(2))
    elif _RE_EARNED.search(text):
        gbp = float(_RE_EARNED.search(text).group(1))
    elif kwh == 0.0:
        # The nil-export mail carries no Earned tile because there is nothing to
        # earn. Zero exported IS zero earned - a fact, not an estimate - and Axle
        # record it as a real transaction, so it belongs in the ledger.
        gbp = 0.0
    else:
        # The low-export template: a real settlement whose money the email never
        # states. We could multiply by the configured rate, and we deliberately
        # do not - "Axle's figures, imported verbatim and never computed here" is
        # the rule the whole axle side turns on, and a computed number presented
        # as theirs is the worse error. Name the figure so it can be entered.
        return {"payload": None,
                "note": f"Axle settled {kwh} kWh but their low-export template states no "
                        f"amount, so the money cannot be read from the mail - enter it by "
                        f"hand if you want this event recorded"}

    if not (0.0 <= kwh <= MAX_EVENT_KWH):
        return {"payload": None, "note": f"exported figure {kwh} kWh is outside the plausible range"}
    if not (0.0 <= gbp <= MAX_EVENT_GBP):
        return {"payload": None, "note": f"earned figure GBP {gbp} is outside the plausible range"}


    # Cross-check the two independent figures. They come from different parts of
    # the mail, so a disagreement means the template moved and the parse can no
    # longer be trusted - better to refuse than to bank a number we misread.
    if rate:
        implied = kwh * rate
        if abs(implied - gbp) > 0.02:
            return {"payload": None,
                    "note": f"the kWh and money figures disagree ({kwh} x {rate} is not {gbp})"}

    event_date = event_date_from_body(text, received_at)
    if event_date is None:
        return {"payload": None, "note": "no event date in the body"}

    window = window_lookup(event_date) if window_lookup else None
    if not window:
        return {"payload": None,
                "note": f"settled {kwh} kWh for {event_date.isoformat()} but this plugin has "
                        f"no record of driving a window that day, so the row cannot be keyed"}
    start_iso, end_iso = window

    # `email-<start>` matches the id convention the hand-typed rows already use,
    # so the window-keyed supersede in import_axle_payload replaces this row with
    # Axle's authentic one when the account page eventually catches up.
    stamp = str(start_iso)[:16]
    return {
        "payload": {"transactions": [{
            "transaction_id":   f"email-{stamp}",
            "transaction_type": "flex event",
            "start_time":       start_iso,
            "end_time":         end_iso,
            "settlement_date":  None,
            # Negative: an export, matching every row Axle themselves send.
            "flex_kwh":         -round(kwh, 3),
            "credit_pence":     int(round(gbp * 100)),
        }]},
        "note": "",
    }


# ======================================================================
# Reading them straight out of the local Mail store
# ======================================================================
#
# There is no IMAP account to log into and no forwarding rule to set up: Apple
# Mail has already downloaded these messages under the user's own account, and
# an Indigo plugin host can read them (verified 05-Sep-2026 from a live host —
# ~/Library/Mail is behind Full Disk Access and the host has it).
#
# So the "fetch" is a file read. No credential passes through the plugin, which
# is the whole reason to prefer this over any mailbox login.
#
# .emlx is a byte count, a newline, the RFC822 message, then an Apple plist.
# Splitting at the first newline and parsing the rest as a message is all it takes.

MAIL_ROOT_GLOB = "~/Library/Mail/V*"
SCAN_WINDOW_DAYS = 45          # a settlement lands ~3 days after its event
_PREFILTER = (b"noreply@axle.energy", b"results are in")


def mail_roots(root_glob=MAIL_ROOT_GLOB):
    """Every local Mail store directory. The V-number changes with macOS, so it
    is globbed rather than pinned — a version bump must not silently stop this."""
    return sorted(glob.glob(os.path.expanduser(root_glob)))


def iter_settlement_messages(root_glob=MAIL_ROOT_GLOB, since_days=SCAN_WINDOW_DAYS, now=None):
    """Yield (sender, subject, body, received_at) for each settlement mail found.

    Two cheap filters before anything is parsed, because the store here holds
    16,000 messages and this runs on the plugin's only thread: an mtime window,
    then a 4 KB prefilter on the raw bytes. A full walk measured 1.64 s.

    Never raises. An unreadable store, a malformed message, a permission change
    — each is skipped, because a mail scan must not be able to take the plugin
    down with it.
    """
    cutoff = (now or time.time()) - since_days * 86400
    for root in mail_roots(root_glob):
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
                        if not all(tok in head for tok in _PREFILTER):
                            continue
                        raw = head + fh.read()
                except (OSError, ValueError):
                    continue
                try:
                    nl = raw.index(b"\n")
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
                try:
                    received = email.utils.parsedate_to_datetime(msg.get("Date"))
                except Exception:
                    received = None
                yield (str(msg.get("From") or ""), str(msg.get("Subject") or ""), body, received)
