#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    london_time.py
# Description: The ONE Europe/London implementation for this plugin. Every
#              module converts local time through here and nowhere else.
# Author:      CliveS & Claude Opus 5
# Date:        05-08-2026
# Version:     1.0
#
# WHY THIS MODULE EXISTS
# ----------------------
# Every local-time answer in this plugin is a decision input: tariff windows,
# the midnight rollover, dawn and dusk, the VPP daytime/dark mode choice. Get
# one an hour out and the plugin buys electricity at the wrong time, or runs the
# export mode that curtails the solar.
#
# The bug this prevents has now been found THREE times, and every time the cause
# was the same: several hand-rolled copies of one conversion, some written
# defensively and some not.
#   v5.22.1 (27-May-2026)  octopus_api — fixed there, nowhere else.
#   v5.55.3 (30-Jul-2026)  battery_manager — five copies found, three with a
#                          stdlib tier and two without; the two without returned
#                          the UTC value UNCHANGED when pytz was missing, an
#                          answer quietly one hour out for the eight months of
#                          BST. Unified onto one implementation — but only
#                          within that module.
#   v5.56.0 (05-Aug-2026)  plugin.py still had FIFTEEN, and openmeteo_forecast
#                          four. The v5.55.3 note explaining the danger was
#                          filed in plugin.py, which was one of the files that
#                          had not been fixed.
# So the implementation now lives BELOW every module that needs it, and there is
# no longer anywhere sensible to put a sixth copy.
#
# STDLIB zoneinfo IS PREFERRED over pytz, deliberately:
#   * it ships with Python from 3.9, so it cannot go missing when a
#     Contents/Packages rebuild fails — and that has happened on this install
#     more than once (ClaudeBridge's lost __init__.py, the paho 1.6.1 -> 2.1.0
#     purge);
#   * it has no .localize() requirement, and attaching a pytz zone with a bare
#     replace(tzinfo=...) yields LMT — for London that is -00:01, a silently
#     wrong answer by 60 seconds.
# pytz is kept as a second source only for completeness.
#
# NOTHING HERE FALLS BACK TO UTC. A caller that cannot convert is told so and
# decides what to do about it. "Carry on in UTC" is the failure this file exists
# to make impossible.

from datetime import datetime, timezone


def london_tz():
    """Return a Europe/London tzinfo, or None if no tz database is reachable.

    None means "cannot convert", and callers must degrade explicitly — it must
    never be confused with "the answer is UTC". On any supported Python this
    returns a real zone, so the None path is effectively unreachable; it exists
    so a broken interpreter fails visibly at the call site rather than here.
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/London")
    except Exception:
        try:
            import pytz
            return pytz.timezone("Europe/London")
        except Exception:
            return None


def london_localise(naive_dt, prefer_dst=False):
    """Attach Europe/London to a NAIVE datetime that is already local wall-clock.

    THE reason this is a shared function: pytz and zoneinfo need different calls
    for this, and only this, operation. pytz requires `tz.localize(naive)`;
    zoneinfo requires `naive.replace(tzinfo=tz)`. Using zoneinfo's form on a
    pytz zone gives LMT, and that is exactly the kind of detail a copied block
    gets wrong.

    `prefer_dst` resolves the AMBIGUOUS hour on the October fallback Sunday,
    when 01:00-02:00 local happens twice. The two libraries spell that
    differently too — pytz takes `is_dst=`, zoneinfo takes `fold=`, and there is
    no `is_dst` keyword in zoneinfo at all, so a half-migrated copy breaks the
    once-a-year path:

        prefer_dst=False (default)  the SECOND, GMT occurrence
                                    pytz is_dst=False  ==  zoneinfo fold=1
        prefer_dst=True             the FIRST, BST occurrence
                                    pytz is_dst=True   ==  zoneinfo fold=0

    The default matches pytz's own default and what openmeteo_forecast has
    always asked for explicitly. Before this module the two branches DISAGREED
    on that hour — battery_manager's zoneinfo path took the first occurrence
    while its pytz path took the second, so the answer depended on which library
    happened to be installed.

    A NONEXISTENT local time (the March spring-forward gap) is not defined here
    and the two libraries genuinely differ on it; nothing in this plugin feeds
    one in, because a gap time never appears on a local wall clock.

    Returns None when no zone is available, so a caller cannot mistake an
    unconverted value for a converted one.
    """
    tz = london_tz()
    if tz is None:
        return None
    localize = getattr(tz, "localize", None)          # pytz only
    if localize is not None:
        return localize(naive_dt, is_dst=prefer_dst)
    return naive_dt.replace(tzinfo=tz, fold=0 if prefer_dst else 1)


def to_london(dt):
    """Convert an AWARE datetime to Europe/London.

    A naive datetime is returned unchanged — callers that hold naive values
    treat them as already-local, and silently stamping a zone on one would
    invent information. Returns dt unchanged if no zone is available.
    """
    if dt is None or dt.tzinfo is None:
        return dt
    tz = london_tz()
    return dt.astimezone(tz) if tz is not None else dt


def london_now():
    """Current time as an AWARE Europe/London datetime, or None if unavailable.

    None rather than a naive local clock: on a UTC-hosted server those differ by
    an hour for eight months of the year, and a caller that cannot tell which it
    got is the whole problem.
    """
    tz = london_tz()
    if tz is None:
        return None
    return datetime.now(timezone.utc).astimezone(tz)
