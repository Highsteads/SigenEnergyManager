#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin.py
# Description: SigenEnergyManager - self-sufficiency battery management for
#              Sigenergy solar/battery systems. Replaces SigenergySolar v3.1.
#              Core philosophy: never import from grid unless battery cannot
#              reach next-day solar at minimum SOC. Export to prevent 100% cap.
# Author:      CliveS & Claude Fable 5 (5.67.0); Claude Opus 5 (5.68-5.69, 5.71.1,
#              5.72.0, 5.75.0, 5.78.0-5.78.1); Claude Sonnet 5 (5.80.0); Claude Opus 5 (5.80.1, 5.81.0-5.88.0);
#              Claude Fable 5.1 (5.89.0-5.90.2); Claude Opus 5 (5.91.0-5.99.2); Claude Sonnet 5 (5.99.3);
#              Claude Fable 5.1 (5.100.0-5.101.0); Claude Opus 5 (5.102.0, 5.103.0, 5.104.0-5.108.0);
#              Claude Opus 5 & ChatGPT Astra/Codex (5.109.0 — native Flux controller)
#              Claude Opus 5 (5.109.1 — Flux floors on the backup reserve, site import cap)
#              Claude Opus 5 (5.109.2 — Saving Sessions for other regions stay silent)
#              Claude Opus 5 (5.109.3 — daytime solar paced to be full by the 4pm Flux peak)
#              Claude Opus 5 (5.109.4 — Saving Session Pushover in plain English)
#              Claude Opus 5 (5.109.5 — on Flux, bank to 100% once the day cannot clip)
#              Claude Opus 5 (5.110.0 — Flux import priced by band; every tier published)
#              Claude Opus 5 (5.110.1 — the Flux floor stops shouting its re-asserts)
#              Claude Opus 5 (5.110.2 — the day's first bank-first verdict survives a restart)
#              Claude Opus 5 (5.110.3 — the Flux plan note stops repeating itself)
#              Claude Opus 5 (5.110.4 — and stops again, now the watt figure is out of the key)
#              Claude Opus 5 (5.111.0 — no pacing at all once the day cannot clip)
#              Claude Opus 5 (5.111.1 — the bank-first log stops claiming a release that never happened)
#              Claude Opus 5 (5.111.3 — the bank-first line quotes the forecast it was actually classified on)
#              Claude Opus 5 (5.111.4 — the data directory refuses a path that is not one)
# Date:        20-09-2026
# Version:     5.111.4
#
# CHANGELOG: docs/plugin-changelog.md
#   The full technical history used to live here and had reached 2,002 lines - 17.4% of
#   this file - which cost every reader 2,000 lines of scrolling to reach the imports.
#   Moved out 25-Aug-2026. Nothing was lost: the AST of this module is byte-identical
#   either side of the move, because only comments were removed.
#   Add new entries to the TOP of that file, as they were added here.
#

import indigo
import json
import os
import math
import sqlite3
import sys
import threading
import time
import copy
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# Secrets (from master IndigoSecrets.py - never committed to git)
# ============================================================

sys.path.insert(0, os.getcwd())
sys.path.insert(0, "/Library/Application Support/Perceptive Automation")
# Master credentials file: IndigoSecrets.py at
# /Library/Application Support/Perceptive Automation/IndigoSecrets.py
# (renamed from `IndigoSecrets.py` on 10-May-2026 — see feedback_secrets_module_shadowing.md
# for why: avoid colliding with Python's stdlib `secrets` module).
# Per-key try/except so a missing single key does not blank the others.
try:
    from IndigoSecrets import OCTOPUS_API_KEY
except ImportError:
    OCTOPUS_API_KEY = ""
try:
    from IndigoSecrets import OCTOPUS_ACCOUNT
except ImportError:
    OCTOPUS_ACCOUNT = ""
try:
    from IndigoSecrets import OCTOPUS_MPAN
except ImportError:
    OCTOPUS_MPAN = ""
try:
    from IndigoSecrets import OCTOPUS_SERIAL
except ImportError:
    OCTOPUS_SERIAL = ""
try:
    from IndigoSecrets import OCTOPUS_EXPORT_MPAN
except ImportError:
    OCTOPUS_EXPORT_MPAN = ""
try:
    from IndigoSecrets import OCTOPUS_EXPORT_SERIAL
except ImportError:
    OCTOPUS_EXPORT_SERIAL = ""
try:
    from IndigoSecrets import OCTOPUS_GAS_MPRN
except ImportError:
    OCTOPUS_GAS_MPRN = ""
try:
    from IndigoSecrets import OCTOPUS_GAS_SERIAL
except ImportError:
    OCTOPUS_GAS_SERIAL = ""
try:
    from IndigoSecrets import AXLE_API_KEY
except ImportError:
    AXLE_API_KEY = ""
try:
    from IndigoSecrets import PUSHOVER_USER_TOKEN
except ImportError:
    PUSHOVER_USER_TOKEN = ""
try:
    from IndigoSecrets import POWERCUT_EMAIL
except ImportError:
    POWERCUT_EMAIL = ""
try:
    from IndigoSecrets import SIGENERGY_IP
except ImportError:
    SIGENERGY_IP = ""
try:
    from IndigoSecrets import DASHBOARD_HOST
except ImportError:
    DASHBOARD_HOST = ""
try:
    from IndigoSecrets import SIGEN_DASHBOARD_TOKEN
except ImportError:
    SIGEN_DASHBOARD_TOKEN = ""
# Site coordinates — IndigoSecrets first, PluginConfig fallback, Big Ben default.
# Names match the existing IndigoSecrets convention (LATITUDE / LONGITUDE).
try:
    from IndigoSecrets import LATITUDE as SITE_LATITUDE
except ImportError:
    SITE_LATITUDE = None
try:
    from IndigoSecrets import LONGITUDE as SITE_LONGITUDE
except ImportError:
    SITE_LONGITUDE = None
# Unified Dashboards plugin: menuOpenDashboard now points at the Dashboards
# hub rather than Sigenergy's internal mini-dashboard on WEB_DASHBOARD_PORT.
try:
    from IndigoSecrets import INDIGO_URL
except ImportError:
    INDIGO_URL = ""
try:
    from IndigoSecrets import INDIGO_API_KEY
except ImportError:
    INDIGO_API_KEY = ""
try:
    from IndigoSecrets import CLAUDEBRIDGE_BEARER_TOKEN
except ImportError:
    CLAUDEBRIDGE_BEARER_TOKEN = ""

try:
    from plugin_utils import log_startup_banner
except ImportError:
    log_startup_banner = None
try:
    from plugin_utils import install_timestamp_filter
except ImportError:
    install_timestamp_filter = None
try:
    # The correct checkbox-pref reader: Indigo re-serialises a saved checkbox as
    # the STRING "false", and bare bool("false") is True. NB most of this file
    # still uses bare bool() on prefs (exportEnabled, axleEnabled, …) — a real
    # latent trap, but an estate-wide sweep of its own, not this change's job.
    from plugin_utils import as_bool as _as_bool
except ImportError:                                     # pragma: no cover
    def _as_bool(value, default=False):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "yes", "on", "1"):
                return True
            if v in ("false", "no", "off", "0", ""):
                return False
        return default

# Plugin modules
from sigenergy_modbus import SigenergyModbus, ENERGY_BLOCK_KEYS
# v5.89.0: the daily figures derive from lifetime counters anchored at local
# midnight. See docs/daily-energy-revamp.md for why the accumulate-and-reset
# model went (it froze homeDailyKwh on yesterday's total for two days).
from daily_energy import (DailyEnergy, readings_from_data, recovery_from_data,
                          local_midnight_epoch, KEYS as ENERGY_KEYS)
from openmeteo_forecast import OpenMeteoForecast
from octopus_api      import (OctopusAPI, TARIFF_TRACKER, TARIFF_FLEXIBLE, TARIFF_AGILE,
                              TARIFF_FLUX, TARIFF_GO, TARIFF_IGO, TARIFF_IFLUX,
                              GAS_KWH_PER_M3)
from octopus_api      import SAVING_SESSION_TURN_DOWN, SAVING_SESSION_HAPPY_HOUR
from octopus_api      import octopoints_to_pence as _points_to_pence

# ── Weekend Happy Hour scheme (v5.107.0) ─────────────────────────────────────
# All three are VENDOR FACTS read off Octopus's own pages on 15-09-2026 and all
# three will rot. Re-read the pages rather than trusting these; they are
# constants and not prefs because a change on Octopus's side is a release, not
# something an owner should have to discover and type in.
#
#   octopus.energy/saving-sessions/weekend-happy-hours/
#   octopus.energy/help-and-faqs/articles/how-to-earn-weekend-happy-hours/
#
# The end date is EXCLUSIVE here even though Octopus write "use them all by 1st
# November", and 1 November 2026 is itself a Sunday. The wording is ambiguous
# ("until 1st November" elsewhere on the same site) and the two readings fail in
# opposite directions: assume it counts and be wrong, and a token is earned that
# can never be spent — which is the exact thing this guard exists to prevent.
# Assume it does not and be wrong, and the worst case is one cautious refusal.
HAPPY_HOUR_SCHEME_END        = date(2026, 11, 1)
# "You can use up to two per weekend."
HAPPY_HOUR_MAX_PER_WEEKEND   = 2
# "Results from your Power Down sessions will take about three working days to
# update, after which they'll count towards your Weekend Happy Hour tally." Three
# working days is five calendar days in the worst case (a Thursday session lands
# the following Tuesday), and a booking also has to be made before the weekend —
# "bookings open towards the end of the week" — so this is deliberately the
# pessimistic reading. A token that arrives too late to book is not a token.
HAPPY_HOUR_SETTLEMENT_DAYS   = 5
# The ONE Europe/London implementation. Imported, never re-declared: copies are
# what put two sites an hour out in the first place, and there is no version of
# "just this once" that does not end up as copy number six.
from london_time import (
    london_tz       as _london_tz,
    london_localise as _london_localise,
    to_london       as _to_london,
)
from battery_manager  import (
    BatteryManager, ManagerSnapshot, TariffData,
    ACTION_SELF_CONSUMPTION, ACTION_START_IMPORT, ACTION_STOP_IMPORT,
    ACTION_SCHEDULE_IMPORT, ACTION_START_EXPORT, ACTION_STOP_EXPORT,
    ACTION_VPP_EXPORT, ACTION_SAVING_SESSION, ACTION_HAPPY_HOUR_IMPORT,
    ACTION_SOLAR_OVERFLOW, FLOOD_PREV_SOC_THRESHOLD_PCT, FLOOD_PREV_TARGET_PCT,
    FLOOD_PREV_FORECAST_MULT,
    pv_tracking_factor as _pv_tracking_factor,
    need_for_weekday as _need_for_weekday,
    BANK_FIRST_HOLDING, BANK_FIRST_RELEASED, BANK_FIRST_NOT_ASKED,
    SOLAR_OVERFLOW_TARGET_SOC_PCT, SOLAR_OVERFLOW_MIN_END_SOC_PCT,
    SOLAR_OVERFLOW_SHADOW_TARGET_SOC_PCT,
    SOLAR_OVERFLOW_CAP_DEADBAND_W,
    SOLAR_OVERFLOW_BANK_FIRST_MAX_KWH, SOLAR_OVERFLOW_BANK_FIRST_SOC_PCT,
    SOLAR_OVERFLOW_BANK_FIRST_SOC_MAX, SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX,
    BANK_FIRST_PROMOTE_MARGIN_KWH,
)
from axle_api      import AxleAPI
from storm_watch   import check_storm_level
from economics     import Economics
from web_dashboard import (WebDashboard, DASHBOARD_BIND_ALL,
                           DASHBOARD_BIND_LOOPBACK)
import vpp_ledger as _vpp_ledger
import axle_email as _axle_email
import axle_account as _axle_account

# ── Octopus Flux (draft, off by default) ─────────────────────────────────────
# Two modules, imported together and guarded together. flux_strategy decides;
# flux_execution carries the decision out and owns the durable claim. Neither is
# reachable unless the owner has ticked BOTH the feature switch and the
# commissioning gate, so a missing module can only ever mean "the feature stays
# off" — never a plugin that will not start.
try:
    import flux_strategy as _flux_strategy
    from flux_execution import (FluxExecutor as _FluxExecutor, FluxTarget as _FluxTarget,
                                MAX_OBSERVATION_AGE_S as _FLUX_MAX_OBSERVATION_AGE_S)
    FLUX_AVAILABLE = True
except Exception as _flux_import_exc:       # noqa: BLE001 — a broken half must not stop the plugin
    _flux_strategy  = None
    _FluxExecutor   = None
    _FluxTarget     = None
    _FLUX_MAX_OBSERVATION_AGE_S = 60
    FLUX_AVAILABLE  = False
    FLUX_IMPORT_ERROR = repr(_flux_import_exc)
else:
    FLUX_IMPORT_ERROR = ""

# ============================================================
# Constants
# ============================================================

# PLUGIN_VERSION is read dynamically from Info.plist by Indigo and passed to
# Plugin.__init__ as `plugin_version` (exposed as self.pluginVersion).  Do NOT
# add a hardcoded version constant here — Info.plist is the single source of truth.
PLUGIN_NAME        = "Sigenergy Manager"
WEB_DASHBOARD_PORT = 8179

# Maps the raw decision action (snake_case) to the camelCase token written to
# the batteryManager "currentMode" List-enum state. Indigo derives one
# BoolTrueFalse sub-state per token (currentMode.solarOverflow etc.) for
# per-mode triggering. Tokens MUST match the <Option value=> entries in
# Devices.xml. currentAction keeps the friendly display string separately.
ACTION_MODE_TOKEN = {
    ACTION_SELF_CONSUMPTION: "selfConsumption",
    ACTION_SOLAR_OVERFLOW:   "solarOverflow",
    ACTION_START_IMPORT:     "startImport",
    ACTION_SCHEDULE_IMPORT:  "scheduleImport",
    ACTION_STOP_IMPORT:      "stopImport",
    ACTION_START_EXPORT:     "startExport",
    ACTION_STOP_EXPORT:      "stopExport",
    ACTION_VPP_EXPORT:       "vppExport",     # dedicated enum token (currentMode.vppExport trigger)
    ACTION_SAVING_SESSION:   "savingSession",
    ACTION_HAPPY_HOUR_IMPORT: "happyHourImport",
}

# Minimum inverter readings required per half-hourly slot before we trust the
# accumulated average over the default profile.
# CAUTION (v5.104.0): the original comment here read "5 readings = ~5 days of
# data in that time-slot (one reading per day during that 30-min window)". That
# stopped being true when the poll got faster. MODBUS_POLL_INTERVAL is 10 s and
# the measured rate is ~147 readings per slot per DAY (counted off the dated
# tarballs in data_backup/, 30-Aug to 13-Sep-2026), so five readings is about
# one minute of one day. The guard is now only a "has this slot ever been fed"
# check, which is all the LIFETIME accumulator needs. The rolling window below
# counts DAYS instead, because a window seeded from a single day would swing the
# whole plan to whatever yesterday happened to be.
HOME_PROFILE_MIN_READINGS = 5

# --- Rolling consumption window (v5.104.0) -------------------------------
# The lifetime accumulator never forgot anything: 442,295 readings banked since
# go-live in March 2026, no decay, no window. Measured on 13-Sep-2026 it was
# accurate to ~1%, but only because the house's decline had been gradual — a
# real step change would have taken ~63 days to be HALF absorbed, and that
# horizon lengthened by a day for every day that passed. The window fixes the
# mechanism rather than the number.
#
# 63 days, not 60, is deliberate: it is exactly NINE WEEKS, so the window holds
# nine of every weekday and the blended profile carries no day-of-week
# composition bias. A flat 60 would hold nine of some days and eight of others,
# which tilts the blend by roughly 0.04 kWh given the 2.6 kWh Saturday/Sunday
# spread measured here. Small, and free to avoid.
PROFILE_WINDOW_DAYS     = 63    # 9 whole weeks
PROFILE_MIN_WINDOW_DAYS = 14    # before the window outranks the lifetime mean

# --- Away mode (v5.78.0) -------------------------------------------------
# An empty house draws a completely different load, and the occupied profile
# cannot represent it.  Measured off the Octopus import meter across the 45-day
# Oct-Nov 2025 absence (pre-battery, so import WAS house load): a flat 507 W,
# 12.16 kWh/day, varying only 1.2x from trough to peak.  Two consequences, and
# they are why away days get their own accumulators rather than a fudge factor:
#   1. A cumulative mean will not move far enough in six weeks to notice, so
#      the occupied profile would keep predicting a full house all trip.
#   2. Those six weeks would otherwise pollute the occupied profile for months
#      afterwards.
AWAY_PROFILE_MIN_READINGS = 5
AWAY_VARIABLE_DEFAULT     = "Away"
AWAY_DAILY_KWH_DEFAULT    = 12.0
AWAY_TRUTHY               = ("true", "1", "yes", "on", "away")

# Polling intervals (seconds).
# MODBUS_POLL_INTERVAL is the default fallback when no value is in pluginPrefs;
# the actual interval used at runtime is `self.modbus_poll_s`, set in
# _init_modules from pluginPrefs.pollInterval.  Wiring up the pref was added
# in v5.9 — previously the constant was used everywhere and the PluginConfig
# `pollInterval` field had no effect.
MODBUS_POLL_INTERVAL      = 10
MANAGER_EVAL_INTERVAL     = 60    # evaluation cadence — independent of poll cadence
FORECAST_FETCH_INTERVAL   = 1800  # 30 minutes (Open-Meteo: 10,000 calls/day free)
OCTOPUS_RATES_INTERVAL    = 1800  # 30 minutes
OCTOPUS_PROFILE_INTERVAL  = 86400 # 24 hours
COST_SETTLE_INTERVAL      = 21600 # 6 hours - backfill settled whole-house costs into daily_history
VPP_POLL_NORMAL_INTERVAL  = 600   # 10 minutes
VPP_POLL_ACTIVE_INTERVAL  = 60    # 1 minute (near/during event)

# --- Octopus Flux supervisor (draft) -------------------------------------
# There is deliberately NO re-assert interval. An earlier draft skipped the
# executor for 60 s when the command had not changed, to spare the throttled
# bus — and a skip longer than the observation lease leaves the executor holding
# a decision whose reading has expired, with nothing on the inverter enforcing
# it. The target is renewed every tick from a fresh observation instead; the
# executor's renewal path is read-back only when the command is unchanged, which
# is what makes that affordable.
# Take a fresh SOC if the cached observation is older than six seconds, leaving
# the rest of the executor's observation budget for throttled, verified IO.
FLUX_OBSERVATION_MAX_AGE_S = 6.0
# This is observation freshness at commit, not a physical hardware watchdog.
FLUX_OBSERVATION_LEASE_S   = _FLUX_MAX_OBSERVATION_AGE_S
# After being pre-empted by a manual action, a VPP window, a storm or anything
# else that outranks Flux, the supervisor stays out for at least this long AND
# until the pre-empting condition has been clear for FLUX_RECLAIM_TICKS
# consecutive ticks. One tick of a flag flickering clear must not be enough to
# take the inverter back off somebody who is still using it.
# How old the ACCOUNT evidence may be. The agreements come from the account
# endpoint and change perhaps twice a year, so six hours is generous — but it
# must have been fetched, and recently. Public prices refreshing is not evidence
# that the house is still on the tariff those prices belong to.
FLUX_ACCOUNT_EVIDENCE_MAX_AGE_S = 6 * 3600
FLUX_PREEMPT_COOLDOWN_S    = 300
FLUX_RECLAIM_TICKS         = 3
# There is deliberately no give-up timeout for an unconfirmed claim. An earlier
# draft had one; it worked by telling the executor an external supervisor had
# taken over, which is a lie when nothing has. See _flux_note_pending.
# Backstop grace past the stored window end before the MANAGER force-ends an
# over-running VPP export (v5.62.0). The primary path stops at end+2min on a
# 60s poll, so 15 min leaves it ample room to do its job first; anything still
# exporting a quarter of an hour late is a fault, not a late poll.
VPP_OVERRUN_GRACE_MINS    = 15
# How often to check the Axle EARNINGS LEDGER is still being fed. Hours, not
# minutes: settlement runs days behind an event and the final revenue lands at
# the end of the following month, so a fast check would only re-log the same
# staleness. The check itself is local file work — no network, no Modbus.
VPP_LEDGER_CHECK_INTERVAL = 21600  # 6 hours
# How often to look in the local Mail store for new Axle settlement mail. Same
# reasoning as the ledger check: a settlement lands days after its event, so
# hours is ample, and the walk costs ~1.6 s of the plugin's only thread.
AXLE_MAIL_SCAN_INTERVAL   = 21600  # 6 hours
AXLE_ACCOUNT_POLL_INTERVAL = 21600 # 6 hours — settlement moves in days, not minutes
# How far our own in-window measurement may sit from what Axle settled before it
# is treated as a fault rather than meter difference.
#
# MEASURED 06-09-2026 over the seven events Axle have settled and we drove:
# every single one reads HIGH by 0.127 to 0.198 kWh, mean 0.172 - our inverter's
# CT against the DCC settlement meter, consistently in the same direction.
# Crucially the offset is ABSOLUTE and does not scale: the 8.0 kWh event of
# 12-Aug diverges by +0.189, indistinguishable from the 4 kWh ones. So this is a
# flat threshold, NOT a percentage - a proportional gate would have been fitted
# to a pattern the data does not show. 1.0 kWh is about five times the largest
# divergence ever observed, which is the margin a gate needs over its noise.
# Re-measure if events much larger than 8 kWh ever appear.
SETTLEMENT_DIVERGENCE_KWH = 1.0
# Only look this far back. Older events pre-date self-drive and have no
# measurement of ours to compare against, so scanning them would report the
# absence of a record as a fault.
SETTLEMENT_CHECK_DAYS     = 45
# The TIMING gate, and it is a fraction rather than a flat kWh on purpose.
# MEASURED over the eight driven events on disk: a healthy event lands 92.5% to
# 97.0% of what it drove inside the paid hour (the shortfall is the driver's
# deliberate two minutes either side). The one real failure in the ledger, the
# 11-Aug-2026 over-run, landed 56.7% inside. A flat kWh gate cannot work here:
# a 30-minute event is worth at most 2 kWh at the DNO cap, so any threshold
# clearing the noise would swallow the event whole.
SETTLEMENT_INSIDE_FRACTION = 0.85
# ...paired with an absolute floor, so a tiny event's noisy ratio cannot fire.
# Largest healthy gap observed is 0.323 kWh, giving about 2.5x margin.
SETTLEMENT_OUTSIDE_KWH     = 0.8
ACCUMULATOR_SAVE_INTERVAL = 300   # 5 minutes
# v5.89.0 — the energy tripwire. The identity residual and the derived-vs-device
# house gap each warn once per day above max(absolute, fraction * throughput).
ENERGY_BALANCE_ABS_KWH    = 0.5
ENERGY_BALANCE_FRACTION   = 0.03
ENERGY_HOUSE_FRACTION     = 0.05
# How long the midnight task waits for a post-midnight lifetime read to replace
# the provisional anchor before recording the day anyway.
MIDNIGHT_ANCHOR_WAIT_S    = 600
# v5.90.0 — intraday PV tracking (Stage 3). A minute counts towards the ratio only
# while the inverter could take all the PV: at the export cap, or on a full
# battery no longer charging, it is turning PV away and the measured figure
# under-reads potential.
PV_TRACKING_EXPORT_CAP_FRACTION = 0.95
PV_TRACKING_RECORD_ROWS         = 2000   # intraday_pv_tracking.json ring, ~80 days of hours
# v5.90.0 — the weekend uplift is MEASURED from daily_history.json (was a hard-coded
# 1.30; measured here 1.10 over 26 weekends). Default until there is enough history.
# Four day types, not two. Measured here over the 90 days to 13-Sep-2026:
# Saturday 24.72 kWh, Monday 22.09, Sunday 22.05, against a Tue-Fri mean of
# 20.76. A single "weekend" figure of 22.9 split the 2.6 kWh Saturday/Sunday
# gap down the middle, and a single "weekday" figure buried a Monday that runs
# 1.3 kWh above the rest of the working week.
#
# Monday is not noise: the uplift measures 1.061 / 1.064 / 1.065 / 1.070 over
# 63 / 90 / 120 / all days of history, and the difference from Tue-Fri grows
# more significant with every extra sample (t = 2.18, 2.89, 3.48, 3.59). A
# ratio that barely moves as the window widens is a real effect, which is also
# what licenses the longer uplift window below.
#
# These defaults are starting points for a house with no history yet, not this
# one's measured values; `_measured_day_uplifts()` replaces them as soon as
# there is enough data.
MONDAY_UPLIFT_DEFAULT           = 1.06
SATURDAY_UPLIFT_DEFAULT         = 1.15
SUNDAY_UPLIFT_DEFAULT           = 1.05

# The uplifts get their OWN, longer window — 18 whole weeks against the
# profile's 9. They are measuring different things and want different spans.
# The profile is a LEVEL and has to be current, so it tracks the house as it is
# now. An uplift is a RATIO between day types, it reflects the household's
# weekly rhythm rather than the season, and the measurement above shows it
# barely moves as the window widens. Splitting a day out of a bucket costs
# sample size — at 63 days there are only 9 Mondays and the mean carries a
# standard error of +/-0.53 kWh, against +/-0.22 for the 36 Tue-Fri days — so a
# ratio estimated on the short window would trade a 1.3 kWh systematic error
# for a needlessly noisy one. 18 weeks roughly halves that noise and, because
# the ratio is stable, costs nothing in responsiveness.
DAY_UPLIFT_WINDOW_DAYS          = 126   # 18 whole weeks
DAY_UPLIFT_MIN_BASE_DAYS        = 20    # Tue-Fri days needed for a reference at all
DAY_UPLIFT_MIN_DAYS             = 6     # of that particular day, before its own uplift is used
STORM_WATCH_INTERVAL = 7200  # 2 hours
# Octopus announces a new Saving Session at most a few times a day (usually the evening
# before), so hourly is ample — no reason to poll it on the 30-min Octopus-rates cadence.
SAVING_SESSIONS_INTERVAL = 3600  # 1 hour
SAVING_SESSIONS_SOON_INTERVAL = 600   # 10 min once a session is imminent
SAVING_SESSIONS_SOON_HOURS    = 2.0   # how far ahead counts as imminent


def _away_seed_profile(daily_kwh):
    """Flat 48-slot half-hourly profile (kWh per slot) for an empty house.

    Flat because the measurement says flat — 1.2x trough to peak over 45 days
    is noise, not a shape.  Used until enough real away readings accumulate,
    and as the permanent fallback for any slot that never fills.

    Guarded like every other config coercion here: a blank, non-numeric or
    absurd value falls back to the documented default rather than raising in
    the middle of a manager evaluation.
    """
    try:
        total = float(daily_kwh)
    except (TypeError, ValueError):
        total = AWAY_DAILY_KWH_DEFAULT
    if not (0.0 < total <= 100.0):
        total = AWAY_DAILY_KWH_DEFAULT
    return [round(total / 48.0, 4)] * 48


PUSHOVER_BODY_CAP = 1024


def _fit_pushover_body(essential, optional, tail, cap=PUSHOVER_BODY_CAP):
    """Join three groups of lines into a Pushover body that fits the 1024 cap.

    Pushover's free tier truncates SILENTLY above 1024 characters, so a body
    that grows past it loses its end mid-sentence with nothing to say so. The
    VPP summary walked into exactly that when its headline was rewritten into
    plain English (v5.91.0): 1062 characters, and the casualty would have been
    the last line of the Ask Claude prompt.

    Three groups, because they are not equally worth keeping:
      essential  the money -- what was sold, and what it is worth.
      optional   diagnostics, every one of which is in the JSONL anyway.
      tail       the Ask Claude block, which is what makes the file actionable.
    Only OPTIONAL lines are shed, from the bottom up, and the message SAYS how
    many went rather than going quietly shorter. Shedding to nothing and still
    not fitting is possible in principle, so the result is truncated as a last
    resort with an explicit marker -- never a silent cut.
    """
    def build(keep, dropped):
        lines = list(essential) + list(optional[:keep])
        if dropped:
            lines.append(f"({dropped} more detail line"
                         f"{'' if dropped == 1 else 's'} left out to fit.)")
        return "\n".join(lines + list(tail))

    n = len(optional)
    for keep in range(n, -1, -1):
        body = build(keep, n - keep)
        if len(body) <= cap:
            return body
    body = build(0, n)
    return body[:cap - 12].rstrip() + "\n[truncated]"


def _as_float(value, fallback):
    """Coerce a config value to float, returning fallback on blank/None/non-numeric.
    Config textfields come back as strings (blank after a dialog save), so float() must
    be guarded everywhere a pref reaches the battery-evaluate maths. The fallback is
    coerced too: several callers pass a string default ('35.04', '94'), which must never
    leak unconverted into arithmetic (e.g. `_as_float(blank, '94') / 100.0` → TypeError)."""
    try:
        if value not in (None, ""):
            return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(fallback)
    except (TypeError, ValueError):
        return fallback


def _as_int(value, fallback):
    """Coerce a config value to int, returning fallback on blank/None/non-numeric.
    The fallback is coerced too so a string default can't leak into arithmetic."""
    try:
        if value not in (None, ""):
            return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(fallback)
    except (TypeError, ValueError):
        return fallback


def _need_scales(mon_uplift, sat_uplift, sun_uplift):
    """(tuefri_scale, monday_scale, saturday_scale, sunday_scale) for a profile P.

    P is the blended mean over ALL days, so the four figures must be chosen to
    put the WEEK back where P says it is:
        4*wd + wd*um + wd*us + wd*uu = 7   ->   wd = 7 / (4 + um + us + uu)
    with monday = wd * um, saturday = wd * us, sunday = wd * uu.

    Tue-Fri is the reference bucket and therefore carries no uplift of its own.
    That is what makes the three ratios independent of each other: pulling
    Monday out cannot move Saturday, because neither is measured against a mean
    the other belongs to. v5.105.0 — v5.104.0 was the same algebra over three
    buckets, and v5.90.0 over two.
    """
    def _clamp(u):
        try:
            v = float(u)
        except (TypeError, ValueError):
            v = 1.0
        return max(0.5, min(2.0, v))
    um, us, uu = _clamp(mon_uplift), _clamp(sat_uplift), _clamp(sun_uplift)
    wd = 7.0 / (4.0 + um + us + uu)
    return round(wd, 4), round(wd * um, 4), round(wd * us, 4), round(wd * uu, 4)


def _num_state(key, value, dp):
    """Build a numeric device-state dict that stores the real number (so Indigo's
    history charts it) with a `decimalPlaces` hint so the device UI renders it
    cleanly (e.g. '99.6', not the raw float '99.59999999999999'). We deliberately
    do NOT set an explicit uiValue — that would make the history log a separate
    `<state>_ui` text column. decimalPlaces alone is the idiomatic Indigo pattern
    (matches the first-party Ecowitt etc.). Coercion is guarded."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    return {"key": key, "value": round(v, dp), "decimalPlaces": dp}


def _atomic_write_json(path, data, indent=2):
    """Write JSON atomically: serialise to a sibling temp file, fsync, then
    os.replace() over the target.  A crash mid-write can never truncate the
    destination (matters for the never-pruned daily_history.json)."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def analyse_pack_balance(avg_c, max_c, min_c, packs=4, outlier_c=2.0):
    """Infer how the battery packs differ, from the three aggregates the
    inverter actually publishes. Pure — the test seam.

    WHY THIS EXISTS: this inverter's Modbus exposes NO per-pack registers
    (probed register by register 13-08-2026 across the inverter and plant
    spaces — every battery figure is an average or a max/min across
    clusters). But max, min AND mean over N identical packs still bound the
    distribution: the other N-2 packs must average
        (mean*N - max - min) / (N - 2)
    so whichever end sits furthest from that middle is the odd one out. With
    four packs that is enough to say "one is running hot" — which the plant
    average alone can never show, because it is exactly what an average
    hides.

    Returns None when the inputs cannot support the inference, rather than a
    confident answer built on nothing:
      * any figure missing
      * fewer than 3 packs (nothing to be an outlier FROM)
      * a middle that falls outside [min, max] — arithmetically impossible,
        so the mean is not the mean of these packs and every conclusion
        drawn from it would be fiction

    verdict: "even" | "one_hot" | "one_cold". Only claims an outlier when
    that end is at least outlier_c away from the middle AND at least twice
    as far as the other end — one warm pack in a tight group is not news.
    """
    try:
        avg = float(avg_c); hi = float(max_c); lo = float(min_c)
        n = int(packs)
    except (TypeError, ValueError):
        return None
    if n < 3 or hi < lo:
        return None
    middle = (avg * n - hi - lo) / (n - 2)
    if middle < lo - 0.05 or middle > hi + 0.05:
        return None                      # the aggregates disagree — say nothing
    hot_gap = hi - middle
    cold_gap = middle - lo
    verdict = "even"
    if hot_gap >= outlier_c and hot_gap >= cold_gap * 2:
        verdict = "one_hot"
    elif cold_gap >= outlier_c and cold_gap >= hot_gap * 2:
        verdict = "one_cold"
    return {
        "spread_c":   round(hi - lo, 1),
        "middle_c":   round(middle, 1),
        "hot_gap_c":  round(hot_gap, 1),
        "cold_gap_c": round(cold_gap, 1),
        "verdict":    verdict,
        "packs":      n,
    }


def _parse_pv_string_labels(raw, count):
    """Parse the pvStringLabels pref into exactly `count` label dicts.

    Format: comma-separated names, each optionally carrying that string's kWp
    after a colon — "South:4.275, East:4.275, West:2.85, NE:2.85". Blank or
    short input pads with PV1..PVn, so the dashboards always get one label per
    reported string and an unnamed install reads PV1-PV4 rather than nothing.
    kWp is optional because the string→roof mapping is unknown until a clear
    day names the curves; a junk kWp becomes None, never a dropped label.
    Pure — the test seam."""
    out = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, _, kwp_txt = part.partition(":")
            name = name.strip()
            try:
                kwp = float(kwp_txt.strip())
            except (TypeError, ValueError):
                kwp = None
        else:
            name, kwp = part, None
        out.append({"label": (name or f"PV{len(out) + 1}")[:20], "kwp": kwp})
    while len(out) < count:
        out.append({"label": f"PV{len(out) + 1}", "kwp": None})
    return out[:count]


ENERGY_VAR_INTERVAL  = 1800  # 30 minutes — write running totals to Indigo variables

# Storm-level hierarchy (mirrors storm_watch._LEVELS)
STORM_LEVELS = ["none", "yellow", "amber", "red"]

# Storm reserve SOC — the minimum the battery is held at during a warning so the
# house can ride out a power cut. CliveS's decision (26-Jun-2026): a FLAT 50% for
# ALL levels (yellow/amber/red). The overnight resilience-buffer import tops the
# battery up to this floor ONLY when below it and ONLY on a flat-rate tariff —
# above 50% there is no storm-driven grid charging (a 50% reserve is enough
# resilience; charging higher from the grid wastes money and self-sufficiency).
# Both constants are deliberately equal — kept separate only so a future user
# could re-differentiate severity without restructuring.
STORM_SOC_YELLOW = 50.0
STORM_SOC_AMBER  = 50.0

# Consecutive failed MeteoAlarm polls tolerated while HOLDING a previous storm
# level before decaying to "none" (poll cadence ~2h → 12 ≈ 24h; any real Met
# Office warning has expired by then). A failed poll is "unknown", never
# all-clear — see _check_storm_watch.
STORM_POLL_FAIL_LIMIT = 12

# Storm export-suppression release point. The storm override holds export off so
# the battery banks kWh ahead of a possible power cut — but ONLY while it is still
# filling toward the reserve. At/above this SOC the reserve is effectively in, and
# continuing to suppress export banks nothing extra: it just rams the battery to
# 100% (charging takes priority over export in self-consumption), which then clips
# every watt of PV above the 4 kW DNO export cap. Releasing here lets Solar Overflow
# take over — it throttles the charge and pushes the surplus out at the full export
# cap, so the battery creeps up slowly with headroom to spare instead of slamming
# full and curtailing. Mirrors POWER_CUT_LOCKOUT_SOC_FLOOR, which solves the
# identical "near-full battery clips solar" problem for the post-cut lockout.
STORM_EXPORT_RELEASE_PCT = 85.0

# Power cut lockout: suppress export for this many hours after grid is restored
POWER_CUT_LOCKOUT_HOURS = 4.0
# During that window, hold the export ban ONLY while SOC is below this floor.
# Above it, let export resume so flood-prevention can shed surplus — otherwise a
# near-full battery (e.g. 92%) under good solar would hit 100% and clip generation
# we could have exported. Below the floor there is no flood risk, so the
# precaution holds.
POWER_CUT_LOCKOUT_SOC_FLOOR = 85.0

# v5.50.0 — second, STRICTLY-ADDITIONAL release condition for the lockout (see
# _solar_refill_releases_lockout). A flat SOC floor cannot tell a January night
# from a July morning: on 20-Jul-2026 a 109-SECOND grid blip at 05:25 armed the
# lockout at SOC 75.6%, and export stayed off until 07:36 while the battery
# climbed to the 85% floor. Those ~3.3 kWh were all exportable under the 4 kW DNO
# cap at the time, and banking them instead cost us the afternoon headroom that
# would otherwise have absorbed the above-cap peak — so the lockout converted
# exportable morning kWh into afternoon curtailment. The overnight optimiser had
# already computed the day's actual power-cut resilience minimum as 10%.
#
# Never release on the strength of a forecast below this SOC, however good the day
# looks — a nearly-empty battery is not a resilience reserve, and the whole point
# of the lockout is that the grid has just proved itself unreliable.
POWER_CUT_LOCKOUT_MIN_SOC_PCT = 50.0
# Today's remaining surplus must exceed the gap-to-floor by this factor before we
# trust it. remaining_solar_kwh is already bias-corrected (battery_manager applies
# snapshot.bias_factor), so this is belt-and-braces on a calibrated figure rather
# than a hedge against raw forecast optimism.
POWER_CUT_LOCKOUT_REFILL_MARGIN = 1.25


def _export_locked_out(within_window, soc_pct, soc_floor):
    """Pure decision: should export be suppressed by the post-power-cut lockout?

    Suppress while inside the post-restore window AND the battery is below the
    SOC floor. At/above the floor, allow export (flood-prevention protects the
    solar). An unknown SOC fails safe (suppress) — the lockout must never
    fail-open. Returns True = export suppressed.
    """
    if not within_window:
        return False
    if soc_pct is None:
        return True
    return soc_pct < soc_floor


PV_EXPECTED_MIN_W = 200


def _forecast_peak_w_for_window(hourly, start_local, end_local):
    """Peak FORECAST watts across the local hours a window touches.

    `hourly` is the raw {"YYYY-MM-DD HH:MM:SS": watts} p50 bucket dict. Returns
    None when the window's hours are simply not in the forecast - unknown is a
    third answer here and must not be mistaken for zero.

    Deliberately the RAW p50 rather than the bias-corrected figure: raw runs
    high on this site (overall factor 0.883), and over-stating the expectation
    makes the guard below LESS willing to excuse a zero, which is the safe
    direction for a check whose whole job is catching a shut-down MPPT.
    """
    if not hourly or start_local is None or end_local is None:
        return None
    try:
        first = start_local.replace(minute=0, second=0, microsecond=0)
        last  = end_local.replace(minute=0, second=0, microsecond=0)
    except AttributeError:
        return None

    peak, seen = 0.0, False
    for slot, watts in hourly.items():
        try:
            slot_dt = datetime.strptime(str(slot), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if first.tzinfo is not None:
            slot_dt = _london_localise(slot_dt) or slot_dt
        try:
            if not (first <= slot_dt <= last):
                continue
        except TypeError:
            continue
        seen = True
        try:
            peak = max(peak, float(watts))
        except (TypeError, ValueError):
            continue
    return peak if seen else None


def _solar_refill_releases_lockout(is_daytime, soc_pct, min_soc_pct, battery_kwh,
                                   floor_kwh, remaining_solar_kwh, home_to_dusk_kwh,
                                   margin=POWER_CUT_LOCKOUT_REFILL_MARGIN):
    """Pure decision: does today's own solar make the lockout reserve self-refilling?

    Returns True when the day's remaining generation (net of house load to dusk)
    comfortably covers the gap between where the battery is now and the lockout
    SOC floor. In that case holding export off banks NOTHING the sun wasn't going
    to deliver anyway — it just risks driving the battery to 100% and clipping
    every watt above the DNO export cap. This is a release-EARLY condition only:
    the caller ORs it with the flat SOC floor, so it can never hold export off for
    longer than the floor alone would.

    Deliberately expressed in kWh rather than as an SOC percentage: an SOC-space
    formulation like `projected_dusk_pct >= floor_pct * margin` is unsatisfiable
    whenever `floor_pct * margin > 100` (85 * 1.25 = 106), because the projection
    is capped at the battery's physical capacity.

    Fail-safe in the same direction as _export_locked_out — night, unknown SOC, or
    a battery below min_soc_pct all return False, which HOLDS the lockout.

    NOT used by the storm override, which solves the same clipping problem via
    STORM_EXPORT_RELEASE_PCT. That is deliberate: a storm forecast means the solar
    may not arrive at all, so releasing export because a forecast looks good is
    exactly the wrong call there. Do not "restore symmetry" between the two.
    """
    if not is_daytime or soc_pct is None:
        return False
    if soc_pct < min_soc_pct:
        return False
    needed = max(0.0, floor_kwh - battery_kwh)
    if needed <= 0.0:
        return True          # already at/above the floor — nothing left to bank
    surplus = max(0.0, remaining_solar_kwh - home_to_dusk_kwh)
    return surplus >= needed * margin


def _backup_runtime_hours(soc_pct, floor_pct, capacity_kwh, home_w):
    """Pure: hours the battery could carry the house at the load it is drawing now.

    Usable energy is everything above the inverter's discharge cutoff — below that
    it stops, so counting it would overstate the backup. Returns None when the
    answer is unknowable rather than guessing: no SOC reading, no house load, a
    zero or negative load (the meter dropping out mid-outage), or a nonsense
    capacity. A caller that gets None should say nothing about runtime.

    This is a snapshot at the CURRENT load, not a forecast — the house will draw
    differently over the next few hours, and on a bright day the panels keep the
    battery topped up. It is the same simple figure the dashboard shows, which
    matters: two places quoting the same number should compute it the same way.
    """
    if soc_pct is None or home_w is None:
        return None
    try:
        soc      = float(soc_pct)
        floor    = float(floor_pct)
        capacity = float(capacity_kwh)
        load_w   = float(home_w)
    except (TypeError, ValueError):
        return None
    if capacity <= 0.0 or load_w <= 0.0:
        return None
    usable_kwh = max(0.0, soc - floor) / 100.0 * capacity
    return usable_kwh / (load_w / 1000.0)


def _format_runtime(hours):
    """Pure: turn a backup-runtime figure into something readable on a phone.

    Minutes under the hour, one decimal up to half a day, whole hours to two
    days, then days. Capped at "10+ days" because past that the number is
    meaningless — the load will have changed many times over.
    """
    if hours is None:
        return None
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return None
    if h <= 0.0:
        return None
    if h >= 240.0:
        return "10+ days"
    if h < 1.0:
        return f"{h * 60:.0f} minutes"
    if h < 12.0:
        return f"{h:.1f} hours"
    if h < 48.0:
        return f"{h:.0f} hours"
    return f"{h / 24:.1f} days"


def _lockout_message(export_enabled, lockout_end_local, soc_floor_pct, outlook=None):
    """Pure: one plain sentence on what the export lockout is doing after a restore.

    Must name BOTH ways the lockout ends, or an early resume on the solar rule
    reads as a fault — which is exactly what happened on 26-Jul-2026, when export
    restarted at 74% after a message promising 85%. Returns None when there is
    nothing to say, so the caller can leave the paragraph out entirely.

    `outlook` is the answer to "will today's solar refill the reserve?" worked out
    at send time — a dict of {releases, surplus_kwh, needed_kwh, is_daytime} — or
    None when it could not be worked out. Given one, the message SAYS which way it
    has gone and shows the two figures, rather than leaving the reader to wonder
    which of the two rules will apply to them. It is still a forecast, so it is
    phrased as what the plugin thinks now and re-checks every minute — never as a
    promise. Without an outlook it falls back to naming both rules and no more.
    """
    if not export_enabled:
        return "Export is switched off in the plugin settings, so nothing is being held back."
    if not lockout_end_local:
        return None

    head = f"Export is held off until {lockout_end_local} as a precaution."
    if not outlook:
        return (f"{head} It restarts early if the battery reaches {soc_floor_pct:.0f}%, "
                f"or if today's solar can refill that reserve on its own.")

    surplus = outlook.get("surplus_kwh")
    needed  = outlook.get("needed_kwh")
    figures = ""
    if surplus is not None and needed is not None:
        figures = f" ({surplus:.1f} kWh spare against the {needed:.1f} kWh needed)"

    if outlook.get("releases"):
        return (f"{head} But today's solar covers that reserve on its own{figures}, so "
                f"export should restart within the minute. It re-checks every minute.")
    if not outlook.get("is_daytime"):
        return (f"{head} There is no solar left today to refill the reserve any sooner, "
                f"so it stands until the battery reaches {soc_floor_pct:.0f}% or the "
                f"window ends.")
    return (f"{head} Today's solar does not cover that reserve yet{figures}, so it stands "
            f"until the battery reaches {soc_floor_pct:.0f}%, the forecast improves, or "
            f"the window ends. It re-checks every minute.")


# VPP state machine values
VPP_IDLE         = "idle"
VPP_ANNOUNCED    = "announced"
VPP_PRE_CHARGING = "pre_charging"
VPP_ACTIVE       = "active"

# Axle VPP SOC calculation constants (from SigenergySolar)
VPP_DISCHARGE_EFFICIENCY  = 0.97
BATTERY_CAPACITY_KWH      = 35.04

# Fallback export rate, pence per kWh, used only when no live or per-day rate is
# known (Octopus Outgoing flat 12p, live here since 26-Mar-2026). It was written
# out as a bare 12.0 in nine places, so a tariff change meant finding all nine.
DEFAULT_EXPORT_RATE_P = 12.0


def _serialise_vpp_event(event):
    """Serialise an Axle VPP event dict to JSON-safe primitives.

    datetimes -> ISO-8601 strings; the `raw` API blob is dropped (not needed to
    resume a window). Returns None for a falsy event. Pure — unit-tested.
    """
    if not event:
        return None

    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v

    return {
        "start_time":            _iso(event.get("start_time")),
        "end_time":              _iso(event.get("end_time")),
        "import_export":         event.get("import_export", "export"),
        "duration_hrs":          event.get("duration_hrs"),
        "forecast_dispatch_kwh": event.get("forecast_dispatch_kwh"),
        "estimated_revenue_p":   event.get("estimated_revenue_p"),
    }


def _deserialise_vpp_event(d):
    """Inverse of _serialise_vpp_event — ISO strings back to tz-aware UTC datetimes.

    Returns None when start/end are missing or unparseable, so a corrupt payload
    can never resurrect a dangling VPP state. Pure — unit-tested.
    """
    if not d:
        return None
    try:
        start = datetime.fromisoformat(d["start_time"])
        end   = datetime.fromisoformat(d["end_time"])
    except (KeyError, TypeError, ValueError):
        return None
    # The VPP state machine works in UTC end-to-end — normalise both ends.
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end   = end.astimezone(timezone.utc)
    return {
        "start_time":            start,
        "end_time":              end,
        "import_export":         d.get("import_export", "export"),
        "duration_hrs":          d.get("duration_hrs")
                                 or (end - start).total_seconds() / 3600.0,
        "forecast_dispatch_kwh": d.get("forecast_dispatch_kwh"),
        "estimated_revenue_p":   d.get("estimated_revenue_p"),
    }


def _vpp_resume_decision(vpp_state, event, now, tail_minutes=2):
    """Decide how to handle a persisted VPP window found on restart.

    Returns:
        "idle"   — nothing to resume (state IDLE or no usable event)
        "ended"  — the window closed while the plugin was down; clean up hardware
        "resume" — the window is still open; restore the state machine

    Pure decision function (no side effects) so the restart contract is
    unit-tested without Indigo or hardware.
    """
    if vpp_state == VPP_IDLE or not event:
        return "idle"
    end_time = event.get("end_time")
    if end_time is None:
        return "idle"
    if now >= end_time + timedelta(minutes=tail_minutes):
        return "ended"
    return "resume"


def _vpp_export_anchor_after_midnight(old_start_kwh, pre_reset_total_kwh):
    """New vpp_export_start_kwh when the daily export counter resets mid-window.

    The window's export figure is (grid_export_daily_kwh - start anchor). The
    midnight rollover zeroes the counter, so a window spanning midnight would
    settle NEGATIVE — logged, written to the JSONL, pushed to the device states
    and the Pushover as e.g. "-3.90 kWh exported" (v5.57.0; latent since v5.28.0,
    never hit because Axle has only ever sent within-day windows).

    Re-basing the anchor to (old_start - pre_reset_total) keeps the delta exact:
    the export banked before midnight is (pre_reset_total - old_start), and the
    new day's counter T then yields T - (old_start - pre_reset_total)
    = (pre_reset_total - old_start) + T. Pure so the arithmetic is unit-tested.
    """
    return old_start_kwh - pre_reset_total_kwh


# ============================================================
# Plugin log file (daily rotation, 14-day retention)
# ============================================================

_plugin_log_fh   = None
_plugin_log_date = None

def _ensure_plugin_log(data_dir):
    """Open (or rotate) the daily plugin log file in data_dir/logs/."""
    global _plugin_log_fh, _plugin_log_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _plugin_log_date == today and _plugin_log_fh is not None:
        return  # already open for today
    # Close previous file if open
    if _plugin_log_fh is not None:
        try:
            _plugin_log_fh.close()
        except Exception:
            pass
    log_dir  = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{today}.log")
    try:
        _plugin_log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    except Exception:
        _plugin_log_fh = None
    _plugin_log_date = today
    # Purge log files older than 14 days
    cutoff = datetime.now() - timedelta(days=14)
    try:
        for fname in os.listdir(log_dir):
            if fname.endswith(".log") and len(fname) == 14:
                try:
                    fdate = datetime.strptime(fname[:10], "%Y-%m-%d")
                    if fdate < cutoff:
                        os.remove(os.path.join(log_dir, fname))
                except (ValueError, OSError):
                    pass
    except OSError:
        pass


import logging


_LOG_LEVELS = {
    "DEBUG":   logging.DEBUG,
    "INFO":    logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR":   logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _lvl(level):
    """Map a level NAME to a Python logging int.

    indigo.server.log(level=...) wants an int. A STRING is silently ignored
    and the line logs as plain Info, which hid every WARNING and ERROR raised
    through log() until this was corrected (21-07-2026).
    """
    if isinstance(level, int):
        return level
    return _LOG_LEVELS.get(str(level).upper(), logging.INFO)


def _write_plugin_log(ts, message, level):
    """Append one line to the plugin's own daily log file. Best-effort."""
    if _plugin_log_fh is None:
        return
    try:
        _plugin_log_fh.write(f"{ts} [{level:<7}] {message}\n")
        _plugin_log_fh.flush()
    except Exception:
        pass


def log(message, level="INFO"):
    """Custom log function — writes to Indigo event log and daily plugin log file."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    indigo.server.log(f"[{ts}] {message}", level=_lvl(level))
    _write_plugin_log(ts, message, level)


def vpp_log(message, level="INFO", event=False):
    """VPP narration: the plugin's OWN log file, and NOT the Indigo event log.

    There is a lot of it and there is going to be more - a line for every
    announcement, pre-charge step, mode change, ledger write and summary, once
    per event, for ever. CliveS asked for it out of the event log on 06-Sep-2026
    and he was right: the event log is the estate's dashboard, not this plugin's
    diary. Nothing is lost. Every line still lands in the plugin's own daily
    file, which is where you go when you want the narrative.

    FAULTS ARE THE EXCEPTION and still reach the event log, because that is the
    only place Log_Error_Watch.py can see them, and a VPP failure nothing
    surfaces is exactly the silence the ledger and API guards exist to end.
    Making that decision here, once, is the point of the wrapper - the
    alternative was 69 separate judgements at the call sites, which is 69
    chances to put a fault somewhere nobody reads.

    `event=True` forces one INFO line to the event log anyway. It exists for
    RECOVERY messages: their warning half went to the event log, so sending the
    all-clear only to the plugin's file would leave the event log's last word on
    the subject an error, which reads as still broken. Marking them this way -
    rather than calling log() directly - keeps the rule absolute, so the guard
    in test_plugin can demand that EVERY [VPP] line goes through here.
    """
    if event or _lvl(level) >= logging.WARNING:
        log(message, level)
        return
    _write_plugin_log(datetime.now().strftime("%H:%M:%S.%f")[:-3], message, level)


# Latch so a missing tz database is reported ONCE, not on every 60-second
# evaluate. A plain module global is unreliable inside plugin callbacks, so the
# house idiom is a mutable container.
_TZ_WARN_STATE = {"warned": False}


def _warn_no_tzdb():
    """Report — loudly, once — that Europe/London could not be resolved.

    Every local-time answer in this module is a decision input: tariff windows,
    the midnight rollover, dawn/dusk, the VPP daytime/dark mode choice. The old
    code met a missing tz database with `except: carry on in UTC`, which is an
    answer an hour wrong for the eight months of BST with nothing logged. Degrade
    visibly instead. In practice unreachable — zoneinfo is stdlib from 3.9, so
    this survives a Contents/Packages wipe that would take pytz with it.
    """
    if _TZ_WARN_STATE["warned"]:
        return
    _TZ_WARN_STATE["warned"] = True
    log("No Europe/London time zone available (neither zoneinfo nor pytz) — "
        "local times are falling back to UTC and will be an hour out during "
        "BST. Tariff windows, midnight rollover and VPP daytime/dark detection "
        "are all affected.", level="ERROR")


def _london_now():
    """Current time as an AWARE Europe/London datetime.

    The single entry point for "what is the local time now" in this module.
    """
    tz = _london_tz()
    if tz is None:
        _warn_no_tzdb()
        return datetime.now(timezone.utc)
    return datetime.now(timezone.utc).astimezone(tz)


def _london_today():
    """Today's date in Europe/London."""
    return _london_now().date()


def _tou_band_now(tou, now_local=None):
    """Which band a time-of-use tariff is in right now: "cheap" | "peak" | "standard".

    Compared in LOCAL wall time, because that is what the windows are quoted in —
    octopus_api derives `cheap_start`/`cheap_end` from the published rates and
    stores them as local "HH:MM". Comparing a UTC clock against them would be an
    hour out for the eight months of BST, which is the mistake the Go window
    carried from v5.47.0 to v5.60.0.

    A window that wraps past midnight (Go's 00:30-05:30 does not, but iFlux's
    19:00-16:00 does) is handled by the wrap branch rather than a naive
    start <= t < end.
    """
    if not tou:
        return "standard"
    local = now_local or _london_now()
    hhmm  = local.strftime("%H:%M")

    def _inside(start, end):
        if not start or not end or start == end:
            return False
        return (start <= hhmm < end) if start < end else (hhmm >= start or hhmm < end)

    if _inside(tou.get("peak_start"), tou.get("peak_end")):
        return "peak"
    if _inside(tou.get("cheap_start"), tou.get("cheap_end")):
        return "cheap"
    return "standard"


def _tou_rate_now_p(tou, now_local=None):
    """The published price a time-of-use tariff charges right now, or None.

    None when that band has no published price — absent, not zero and not
    another band's price.
    """
    band = _tou_band_now(tou, now_local)
    return (tou or {}).get({"peak": "peak_p", "cheap": "cheap_p"}.get(band, "standard_p"))


def _local_time(dt, fmt="%H:%M"):
    """Format a UTC-aware datetime in Europe/London local time (BST/GMT).

    All datetimes from the Axle API and VPP state machine are UTC-aware.
    Displaying them without conversion shows UTC, which is 1 hour behind
    BST during British Summer Time (late March — late October).

    A naive datetime is formatted as-is: callers holding naive values treat them
    as already-local, and stamping a zone on one would invent information.
    """
    if _london_tz() is None:
        _warn_no_tzdb()
        return dt.strftime(fmt)
    return _to_london(dt).strftime(fmt)


def _local_today_str():
    """Return today's date (YYYY-MM-DD) in Europe/London, matching _check_midnight.

    The midnight rollover and the accumulator save both stamp today_date in
    Europe/London. The initialiser and the on-disk accumulator restore must use
    the SAME basis — a naive datetime.now() on a UTC-hosted server is the previous
    day for the first hour after local midnight in BST, which would make the loader
    reject (and silently drop) a fresh day's accumulators on a restart in that window.
    """
    return _london_today().strftime("%Y-%m-%d")


def _snapshot_in_window(rec, event, slack_mins=15):
    """True if a JSONL snapshot belongs to `event`'s window.

    Pure and defensive — used by the post-event summariser so one window's
    readings can never be attributed to another. The driver runs T-2min to
    end+2min, so the bound is the window plus a generous `slack_mins` either
    side: a legitimate lead/trail sample is always kept, while a snapshot from
    an entirely different day (11-Aug-2026: elapsed -1288 min) is rejected.

    An UNKNOWN elapsed or duration returns True — dropping a reading we cannot
    place would silently shrink the summary, which is the worse error.
    """
    try:
        elapsed = float(rec.get("event_elapsed_secs"))
    except (TypeError, ValueError):
        return True
    try:
        duration_hrs = float((event or {}).get("duration_hrs") or 0.0)
    except (TypeError, ValueError):
        duration_hrs = 0.0
    if duration_hrs <= 0:
        return True
    slack = slack_mins * 60.0
    return -slack <= elapsed <= duration_hrs * 3600.0 + slack


def _clock_words(dt):
    """6pm, 6:30pm, midday, midnight: a time as a person says it."""
    if dt.hour == 12 and dt.minute == 0:
        return "midday"
    if dt.hour == 0 and dt.minute == 0:
        return "midnight"
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}{suffix}" if dt.minute == 0 else f"{hour}:{dt.minute:02d}{suffix}"


def _session_day_words(start_local, now_utc):
    """today / tonight / tomorrow / on Saturday 20 September."""
    try:
        today = now_utc.astimezone(start_local.tzinfo).date() if start_local.tzinfo else now_utc.date()
        days = (start_local.date() - today).days
    except Exception:                                    # noqa: BLE001
        days = None
    if days == 0:
        return "tonight" if start_local.hour >= 17 else "today"
    if days == 1:
        return "tomorrow"
    return f"on {start_local.strftime('%A')} {start_local.day} {start_local.strftime('%B')}"


def _session_span_words(start_local, end_local):
    if end_local is None:
        return f"at {_clock_words(start_local)}"
    return f"from {_clock_words(start_local)} to {_clock_words(end_local)}"


def _ascii_plain(text):
    """Pushover bodies are ASCII (standing rule): swap the usual offenders."""
    for bad, good in (("\u2014", ", "), ("\u2013", " to "), ("\u2019", "'"),
                      ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'),
                      ("\u00a3", "GBP "), ("\u00b0", " degrees")):
        text = text.replace(bad, good)
    return text.encode("ascii", "ignore").decode("ascii")


# How long the manager waits after startup for the first tariff before planning
# with the Tracker fallback (v5.109.1).
TARIFF_WAIT_S = 300


class _FluxRawDriver:
    """The exact surface flux_execution asks a raw driver for, and nothing else.

    A thin named adapter rather than handing the executor `self.modbus` directly,
    for three reasons:

      * `inverter_max_w` is a plugin PREFERENCE, not a register. The executor
        validates every power figure against it, so it has to come from somewhere,
        and reading it from the driver keeps the executor free of plugin state.
      * `set_charge_limit(watts, quiet=False)` logs and read-back-verifies by
        default. The executor does its own read-back on every write, so this
        adapter passes quiet=True and lets the executor be the one that decides
        what "confirmed" means. One verifier, not two.
      * it is the seam the tests drive. Everything below here is a real Modbus
        transaction; everything above it can be exercised on a fake.

    Every method coerces to a real bool, because the executor tests `is True` and
    a driver that returns a truthy non-bool would be read as a failed write.
    """

    def __init__(self, modbus, prefs):
        self._modbus = modbus
        self._prefs  = prefs

    @property
    def inverter_max_w(self):
        return int(_as_float(self._prefs.get("inverterMaxKw"), 10.0) * 1000)

    # --- writes -------------------------------------------------------
    def set_charge_limit(self, watts):
        return bool(self._modbus.set_charge_limit(int(watts), quiet=True))

    def set_discharge_limit(self, watts):
        return bool(self._modbus.set_discharge_limit(int(watts)))

    def set_charge_cutoff(self, pct):
        return bool(self._modbus.set_charge_cutoff(float(pct)))

    def set_discharge_cutoff(self, pct):
        # THE FLOOR GOES ON THE BACKUP RESERVE (40046), NOT THE CUTOFF (40048).
        # Both stop a grid-tied discharge (hardware-verified 17-Sep-2026, forced
        # export included), but only 40048 still applies off-grid. There is no
        # hardware watchdog, so whatever floor Flux last wrote survives a lost
        # connection — on 40048 that would lock the house out of its own battery
        # in a power cut that followed; on 40046 the battery stays fully usable.
        return bool(self._modbus.set_backup_soc(float(pct)))

    def set_remote_ems_mode(self, mode):
        return bool(self._modbus.set_remote_ems_mode(int(mode)))

    def enable_remote_ems(self):
        return bool(self._modbus.enable_remote_ems())

    # --- reads --------------------------------------------------------
    def read_ems_mode(self):
        return self._modbus.read_ems_mode()

    def read_charge_limit(self):
        return self._modbus.read_charge_limit()

    def read_discharge_limit(self):
        return self._modbus.read_discharge_limit()

    def read_charge_cutoff(self):
        return self._modbus.read_charge_cutoff()

    def read_discharge_cutoff(self):
        return self._modbus.read_backup_soc()

    def read_remote_ems_enabled(self):
        """Register 40029 — is Remote EMS switched on at all.

        The executor checks this as well as the mode, and it is worth having:
        mode 0x03 written while Remote EMS is DISABLED reads back perfectly and
        does nothing, which is the shape of failure that looks like a working
        charge in every log line until the SOC does not move.
        """
        return self._modbus.read_remote_ems_enabled()


class Plugin(indigo.PluginBase):
    """SigenEnergyManager Indigo Plugin.

    Manages a Sigenergy solar/battery system with self-sufficiency as the
    primary goal. Only imports from grid if battery cannot reach next day's
    solar generation window at the configured minimum SOC. Exports to
    prevent 100% battery cap during peak solar generation.
    """

    # ================================================================
    # Plugin Lifecycle
    # ================================================================

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)

        self.timestamp_enabled = bool(plugin_prefs.get("timestampEnabled", True))
        if install_timestamp_filter:
            self._ts_filter = install_timestamp_filter(self, enabled=self.timestamp_enabled)
        else:
            self._ts_filter = None

        # Startup banner moved to showPluginInfo on demand (revised 25-May-2026 per Jay).

        self.debug = plugin_prefs.get("showDebugInfo", False)

        # Data directory for cache files
        self.data_dir = self._get_data_dir()

        # Initialise module instances (configured properly in startup())
        self.modbus   = None
        self.forecast  = None
        self.octopus  = None
        self.manager  = BatteryManager()
        # Export + gas MPAN/serial — populated by _init_modules
        self.export_mpan   = ""
        self.export_serial = ""
        self.gas_mprn      = ""
        self.gas_serial    = ""
        self.axle     = None

        # Indigo trigger registry — populated by triggerStartProcessing/Stop.
        # Maps trigger.id -> trigger object so _trigger_event can find triggers
        # whose pluginTypeId matches the event being fired.  Without this
        # registry, indigo.trigger.execute() has no trigger object to fire.
        self.event_triggers = {}

        # Latest data from each module
        self.latest_inverter_data = {}
        self.latest_forecast_data = {}
        self.latest_rates_data    = {}
        self.latest_decision      = None

        # Web dashboard server (started in startup, stopped in shutdown)
        self.web_dashboard        = None

        # Reentrant lock for self.store. Indigo's runConcurrentThread runs in a
        # background thread while action callbacks, menu items, deviceUpdated and
        # device-config submissions all run on the main thread.  Without a lock,
        # a composite read-modify-write on store (e.g. _act_on_decision flipping
        # flags while an action callback is also writing) can race.  RLock so the
        # tick can call _act_on_decision which can call _trigger_event etc.
        self._state_lock = threading.RLock()

        # Poll timers
        self.store                   = {}   # mutable state dict (replaces globals)
        self.store["last_modbus"]    = 0.0
        self.store["last_manager"]   = 0.0
        self.store["last_forecast"]   = 0.0
        self.store["last_octopus"]   = 0.0
        self.store["last_profile"]   = 0.0
        self.store["last_vpp"]            = 0.0
        self.store["last_acc_save"]       = 0.0
        self.store["last_cost_settle"]    = 0.0
        self.store["last_manager_action"] = ""
        self.store["last_overflow_cap_w"] = 0

        # Daily energy accumulators (kWh, reset at midnight)
        self.store["pv_daily_kwh"]              = 0.0
        self.store["grid_import_daily_kwh"]     = 0.0
        self.store["grid_export_daily_kwh"]     = 0.0
        self.store["home_daily_kwh"]            = 0.0
        self.store["peak_soc"]                  = 0.0
        self.store["min_soc"]                   = 100.0
        self.store["peak_pv_w"]                 = 0
        self.store["peak_pv_time"]              = ""
        self.store["today_date"]                = _local_today_str()
        # Lifetime total anchors for daily delta computation (set on first Modbus read)
        self.store["pv_lifetime_start_kwh"]     = None
        self.store["import_lifetime_start_kwh"] = None
        self.store["export_lifetime_start_kwh"] = None
        # v5.89.0: the six daily figures are DERIVED (daily_energy.py). The store
        # keys above and below are a read-only PROJECTION of that object, refreshed
        # on every Modbus observe and kept so its sixty-odd consumers need no change.
        # Nothing may write to them except _project_daily_energy.
        self.store["battery_charge_daily_kwh"]    = 0.0
        self.store["battery_discharge_daily_kwh"] = 0.0
        self.store["energy_balance_kwh"]          = 0.0
        self.store["energy_day_partial"]          = False
        self.store["energy_reconcile_warned"]     = ""
        self.store["energy_yesterday_projection"] = None
        self.daily_energy = DailyEnergy()
        # v5.90.0: intraday PV tracking + measured need (Stage 3 of the revamp).
        self.store["pv_track_date"]         = ""
        self.store["pv_track_actual_kwh"]   = 0.0
        self.store["pv_track_forecast_kwh"] = 0.0
        self.store["pv_track_last_epoch"]   = 0.0
        self.store["pv_track_last_pv_kwh"]  = None
        self.store["pv_track_clipped_min"]  = 0.0
        self.store["pv_track_factor"]       = 1.0
        self.store["pv_track_ratio"]        = None
        self.store["pv_track_last_hour"]    = None
        self.store["need_today_kwh"]        = None
        self.store["day_uplifts"]           = None   # [saturday, sunday] vs the Mon-Fri mean
        self.store["day_uplift_date"]       = ""

        # VPP state machine
        self.store["vpp_state"]            = VPP_IDLE
        self.store["vpp_active"]           = False
        self.store["vpp_event"]            = None
        self.store["vpp_pre_charge_soc"]   = 0.0
        self.store["vpp_export_start_kwh"]  = 0.0   # grid_export_daily_kwh at event start
        self.store["vpp_last_export_kwh"]   = 0.0   # export kWh during last completed event
        self.store["vpp_charge_stopped"]    = False # True once pre-charge import has ended
        self.store["vpp_10min_warning_sent"] = False # T-10min warning latched
        self.store["vpp_last_snapshot_at"]   = 0.0    # time.time() of last detailed snapshot log

        # Scheduled import state
        self.store["import_active"]          = False
        self.store["import_scheduled_time"]  = None
        self.store["import_target_soc"]      = 0.0

        # Export state (export limit is set once at startup; no dynamic tracking needed)
        self.store["export_active"]   = False

        # Storm watch state
        self.store["last_storm_watch"]    = 0.0    # time.time() of last poll
        self.store["last_energy_var"]     = 0.0    # time.time() of last variable write
        self._energy_var_ids: dict        = {}     # cached variable IDs by name

        # Saving Sessions — Phase 1 (notify-only, no dispatch changes)
        self.store["last_saving_sessions"]      = 0.0   # time.time() of last poll
        self.store["saving_sessions_notified"]  = []    # event ids already Pushover'd
        self.store["saving_sessions_windows"]   = []    # joined windows, cached for the manager
        # EVERY announced session, joined or not, for DISPLAY only. The windows
        # cache above is what the manager drives from, so it is deliberately
        # filtered to joined turn-downs and happy hours — which makes it useless
        # for telling anyone that a session exists and has NOT been opted into,
        # the one case a human most needs to see. Nothing drives off this list.
        self.store["saving_sessions_upcoming"]  = []
        # Event codes Octopus PERMANENTLY refused to let us join (full, ineligible).
        # A transient failure is deliberately NOT recorded, so it retries next poll;
        # only a refusal retrying cannot fix earns a place here, which is what stops
        # an hourly re-attempt for the life of the plugin.
        self.store["saving_sessions_join_refused"] = []
        self.store["saving_sessions_not_our_region"] = []
        # Warn-once latch for the token-budget refusal, so a session we decline for
        # the whole week says so once rather than hourly. Not persisted on purpose:
        # it is chatter suppression, not state, and one repeat after a restart is a
        # fair price for not carrying another key.
        self.store["saving_sessions_budget_logged"] = ""
        # Happy Hour token balance, reported verbatim by the API. None means NOT
        # REPORTED, which is deliberately distinct from 0 — a missing balance must
        # never be shown as "you have none".
        self.store["happy_hour_tokens"]         = None
        self.store["saving_session_export_active"] = False
        self.store["happy_hour_import_active"]  = False
        self.store["happy_hour_anchor_kwh"]     = None   # grid-import counter at window entry
        self.store["happy_hour_free_kwh"]       = 0.0    # free kWh banked in the last window

        # Half-hourly SQLite logging — delta anchors (reset each write)
        self.store["hh_anchor_pv_kwh"]     = None  # cumulative PV at last slot boundary
        self.store["hh_anchor_import_kwh"] = None  # cumulative import at last slot boundary
        self.store["hh_anchor_export_kwh"] = None  # cumulative export at last slot boundary
        self.store["hh_anchor_home_kwh"]   = None  # cumulative home at last slot boundary
        self.store["hh_anchor_soc_pct"]    = None  # SOC at start of current slot
        self.store["storm_level"]         = "none" # current storm level
        # Defaults; _load_accumulators() restores the persisted values (accumulators.json
        # is the plugin's reliable cross-restart store — written every poll) so an
        # already-active warning does not re-send its Pushover on every plugin restart.
        self.store["storm_alerted_level"] = "none" # level at which alert was last sent
        self.store["storm_export_suppressed"] = False  # is export held off right now?

        # Power cut detection
        self.store["grid_status_prev"] = "On-grid"  # previous poll's gridStatus
        # Rolling log of grid-status transitions (most recent 100 entries).
        # Each entry is a human-readable string with timestamp + transition +
        # duration of the outage when it ends.  Surfaced via menuShowPowerCutLog
        # and the web dashboard /api/status.
        self.store["power_cut_events"]      = []
        self.store["power_cut_started_at"]  = None

        # Solar overflow state (daytime charge-cap export)
        self.store["solar_overflow_active"]      = False
        self.store["solar_overflow_charge_cap_w"] = 0
        # When the cap was last released (UTC datetime), for the manager's v3.10
        # re-engage dwell. None = not released this run, which the manager treats as
        # "not blocked" — a fresh start must never be locked out of exporting.
        self.store["solar_overflow_released_at"]  = None
        # Log-only 95% pacing counterfactual — never read by a control path.
        self.store["shadow_95_export_foregone_kwh"] = 0.0
        self.store["shadow_95_samples"]             = 0
        self.store["shadow_skip_reason"]            = ""

        # ── Bank-first export hold (v5.79.0) ────────────────────────────────
        # A one-way day latch: set only from a COMPLETE forecast, and only ever to
        # True. A forecast oscillating around the threshold can therefore tip the
        # day into "small" but can never tip it back out, which is the safe
        # direction — the cost of banking a day that turns out big is a few kWh of
        # export, the cost of releasing a day that turns out small is the whole
        # point of the feature.
        self.store["bank_first_small_latched"]  = False
        self.store["bank_first_first_class_kwh"]   = None
        self.store["bank_first_first_class_small"] = None
        self.store["bank_first_first_class_local"] = ""
        self.store["bank_first_promoted_local"]    = ""
        # v5.111.3: the forecast the latch IN FORCE was set from, and when. Not the
        # same as the first classification of the day — a day that starts big and is
        # demoted has a first classification that never governed anything.
        self.store["bank_first_latched_small_kwh"]   = None
        self.store["bank_first_latched_small_local"] = ""
        self.store["bank_first_latch_date"]     = ""
        # Daily measurement. Counted in manager ticks, which are one a minute.
        self.store["bank_first_blocked_samples"]   = 0
        self.store["bank_first_withheld_kwh"]      = 0.0
        self.store["bank_first_first_block_local"] = ""
        self.store["bank_first_released_local"]    = ""
        self.store["bank_first_logged_date"]       = ""
        self.store["bank_first_release_logged"]    = False
        # Measured, not forecast. clip_boundary_minutes is the only honest detector
        # of the cost this feature could impose: minutes in which export was pinned
        # at the DNO cap while the battery had stopped taking anything. An
        # "SOC >= 99.5" counter would read zero on exactly the days it exists to
        # catch, because a BMS taper stops the charge well before 100%.
        self.store["bank_first_minutes_soc_ge_95"]  = 0
        self.store["bank_first_minutes_soc_ge_99"]  = 0
        self.store["bank_first_clip_boundary_min"]  = 0
        self.store["bank_first_arm_minutes"]        = 0
        self.store["bank_first_first_arm_local"]    = ""
        self.store["bank_first_peak_surplus_kw"]    = 0.0

        # Set when a VPP hand-back write was not confirmed, so the 10s tick
        # re-asserts the safe baseline instead of waiting for the 60s manager
        # cycle (see _retry_vpp_handback). Deliberately NOT persisted: a restart
        # runs the stuck-mode recovery, which returns the inverter to
        # self-consumption anyway, so a stale True would be noise.
        self.store["vpp_handback_pending"] = False

        # --- Octopus Flux supervisor (draft) ---------------------------------
        # NONE of this is persisted. The durable claim on the hardware belongs to
        # flux_execution's journal, which is the one record that survives a
        # restart; a second copy of ownership in the accumulators file could only
        # ever disagree with it. Everything here is this process's own view.
        self.flux_executor            = None
        self.store["flux_decision"]      = None   # last FluxDecision object
        self.store["flux_applied_key"]   = None   # control_key of what is on the inverter
        self.store["flux_last_step"]     = 0.0    # when step() last ran
        self.store["flux_last_result"]   = ""     # applied / released / pending / supervisor
        self.store["flux_owner_reason"]  = ""     # who outranks us right now
        self.store["flux_preempted_at"]  = 0.0
        self.store["flux_clear_ticks"]   = 0
        self.store["flux_pending_since"] = 0.0
        self.store["flux_status"]        = "off"
        self.store["flux_note_logged"]   = ""

        # Ledger-freshness bookkeeping. last_ledger_check starts at 0.0 so the
        # first tick after a restart checks immediately rather than waiting out
        # a whole interval — a stale ledger discovered six hours late is six
        # hours of the silence this exists to end.
        self.store["last_ledger_check"]  = 0.0
        self.store["last_mail_scan"]     = 0.0
        # Windows already reported as diverging, so a re-scan does not re-warn.
        # Deliberately NOT persisted: a restart re-surfaces an unresolved
        # divergence once, which is the right way round for something that costs
        # money and has not been dealt with.
        self.store["settlement_warned"]  = set()
        self.store["mail_scan_problem"]  = ""
        self.store["vpp_ledger_stale"]   = ""
        self.store["vpp_ledger_logged"]  = 0.0

        # Flood prevention state (overnight pre-drain).
        # Persisted to pluginPrefs so a mid-pre-drain plugin restart doesn't leave
        # the inverter cutoff register raised but the store flag empty — which
        # would let _verify_ems_registers() reset the hardware floor and break
        # the in-progress drain. Rehydrated in startup() below.
        self.store["flood_prev_target_soc"] = None  # set when pre-drain export is active

        # Import charge-cutoff backstop (hardware ceiling while a grid import is
        # active — see _set_import_cutoff). Deliberately NOT rehydrated at startup:
        # import_active does not survive a restart (the stuck-mode recovery returns
        # the inverter to self-consumption) and _init_modules parks 40047 at 100%.
        self.store["import_charge_cutoff_pct"] = None

        # Consumption profile (48 slots)
        self.store["consumption_profile"] = []

        # Long-lived home-load profile accumulators (persist across days; never reset at midnight)
        # Built from real homePowerWatts inverter readings, one reading per Modbus poll.
        # v5.104.0: these are now the FALLBACK and the long-run reference. The
        # figure the plugin plans against comes from home_profile_days below.
        self.store["home_profile_watts_sum"] = [0.0] * 48
        self.store["home_profile_count"]     = [0]   * 48

        # Rolling window: {"YYYY-MM-DD": {"sum": [48 floats], "count": [48 ints]}},
        # pruned to PROFILE_WINDOW_DAYS whenever a new day opens. Occupied days
        # only — see _accumulate_home_profile for why the away profile keeps the
        # lifetime accumulator instead.
        self.store["home_profile_days"]      = {}

        # Away-load profile — the same accumulators again, fed only on days the
        # house is empty, so the two never contaminate each other.
        self.store["away_profile_watts_sum"] = [0.0] * 48
        self.store["away_profile_count"]     = [0]   * 48
        # Last known away state.  False, not None: an unreadable variable must
        # read as OCCUPIED (see _is_away for why that is the safe direction).
        self.store["away_active"]            = False
        self.store["away_warned"]            = False

        self._load_accumulators()
        self._load_home_profile()   # restore accumulated inverter profile from disk

    def startup(self):
        _ensure_plugin_log(self.data_dir)
        log(f"{PLUGIN_NAME} v{self.pluginVersion} starting")

        # ── Pref migrations ──────────────────────────────────────────────────
        # v3.0: raise dawnSocTarget minimum to 15% so there is a real buffer
        # above the 10% health floor on poor solar days.  Direct file edits are
        # overwritten by Indigo on shutdown, so we correct the value here and
        # let Indigo persist it naturally.
        _dawn_target = _as_float(self.pluginPrefs.get("dawnSocTarget"), "10")
        if _dawn_target < 15.0:
            self.pluginPrefs["dawnSocTarget"] = "15"
            log(
                f"[Migration] dawnSocTarget raised from {_dawn_target:.0f}% to 15% "
                f"(minimum recommended to buffer above 10% health floor)"
            )
        # v5.104.0: the single `weekendKwh` override becomes one per weekend day.
        # Carried over here rather than left to the fallback in
        # _build_manager_snapshot, because the field is gone from PluginConfig.xml
        # and Indigo writes back only what the dialog holds — so the first time
        # the user opened Configure and saved, a stored override would have been
        # dropped and the fallback would have had nothing to read.
        # v5.105.0: `weekdayKwh` now means Tue-Fri, and Monday has its own field.
        # An existing weekday override is carried onto Monday so the upgrade does
        # not quietly hand Monday back to auto-calibration.
        if not self.pluginPrefs.get("mondayKwh"):
            _wd = self.pluginPrefs.get("weekdayKwh")
            if _wd not in (None, "") and abs(_as_float(_wd, 22.0) - 22.0) > 1.0:
                self.pluginPrefs["mondayKwh"] = str(_wd)
                log(f"[Migration] weekdayKwh {_wd} carried over to mondayKwh — Monday is "
                    f"its own figure now, so set it apart in Configure if your Monday differs")

        if not self.pluginPrefs.get("saturdayKwh") and not self.pluginPrefs.get("sundayKwh"):
            _legacy = self.pluginPrefs.get("weekendKwh")
            # Only a real override is worth carrying. The stored default means the
            # user never touched it, auto-calibration owns the figure either way,
            # and copying it across would only produce a startup line telling
            # someone to reconsider a setting they have never set.
            if _legacy not in (None, "") and abs(_as_float(_legacy, 30.0) - 30.0) > 1.0:
                self.pluginPrefs["saturdayKwh"] = str(_legacy)
                self.pluginPrefs["sundayKwh"]   = str(_legacy)
                log(f"[Migration] weekendKwh {_legacy} carried over to both "
                    f"saturdayKwh and sundayKwh — set them apart in Configure if "
                    f"your Saturday and Sunday differ (they usually do)")

        # (The v5.9 pollInterval 60/120 -> 10s migration was DELETED in v5.43:
        # it had no one-shot gate, so a user deliberately choosing the
        # still-offered 60s/120s ConfigUI options got silently reverted to 10s
        # on every restart, forever. Anyone on the pre-5.9 legacy default has
        # long since been migrated — ~30 versions have passed.)

        self._init_modules()
        self._init_timeseries_db()
        if self.forecast:
            self.forecast.load_correction_factor()
            # v5.106.0: reconstruct any accuracy record whose actual PV was lost to
            # the inverter's midnight counter reset (see _check_midnight_impl). The
            # settled total is in daily_history.json, so the pairing can be redone
            # exactly. One-shot in effect — a repaired record is never zero again.
            try:
                self.forecast.repair_zero_actuals(self._settled_pv_for_date)
            except Exception as exc:
                self.logger.warning(f"[Forecast] Accuracy repair skipped: {exc}")
        # Pre-populate latest_forecast_data from disk cache so the first manager
        # evaluation has forecast data available (disk cache was loaded in
        # OpenMeteoForecast.__init__; this propagates it into plugin.py's dict).
        self._refresh_forecast()
        self.store["last_forecast"] = time.time()

        # Rehydrate flood prevention target SOC from pluginPrefs.
        # If the plugin restarts mid-pre-drain, the inverter still has its
        # discharge cutoff register raised to this target — without rehydrating
        # we'd lose the flag and _verify_ems_registers() would lower the cutoff
        # back to the health floor mid-drain.
        # Clear any stale persisted import charge-cutoff: the backstop does not
        # survive a restart (register parked at 100% by _init_modules above) so a
        # leftover pref would make _verify_ems_registers expect the wrong value.
        if self.pluginPrefs.get("importChargeCutoffPct", ""):
            self._set_import_cutoff(None)

        _flood_prev_str = self.pluginPrefs.get("floodPrevTargetSoc", "")
        if _flood_prev_str:
            try:
                _flood_prev_val = float(_flood_prev_str)
                if _flood_prev_val > 0:
                    self.store["flood_prev_target_soc"] = _flood_prev_val
                    self.store["export_active"]         = True
                    log(
                        f"[Manager] Flood prevention rehydrated from prefs: "
                        f"target {_flood_prev_val:.0f}% — pre-drain still in progress"
                    )
            except (ValueError, TypeError):
                pass

        # Resume an in-progress Axle VPP window if the plugin restarted mid-event.
        # The window was restored from accumulators.json by _load_accumulators;
        # this makes the time-based resume/cleanup decision now modbus is up.
        self._rehydrate_vpp_state()

        # Set initial state images for all devices that already exist
        # (deviceStartComm handles newly created devices; this handles existing ones on reload)
        for dev in indigo.devices.iter("self"):
            self._set_device_initial_state(dev)
        log(f"{PLUGIN_NAME} ready")

        try:
            self.web_dashboard = WebDashboard(
                self, port=WEB_DASHBOARD_PORT,
                bind_host=self._resolve_dashboard_bind(),
                auth_token=self._resolve_dashboard_token())
            self.web_dashboard.start()
            log(f"[Web] Dashboard at {self._dashboard_url()}")
        except Exception as exc:
            log(f"[Web] Dashboard failed to start: {exc}", level="ERROR")

        # Subscribe to variable changes so an external automation (or the
        # Indigo client UI) can toggle pause via the `sigen_manager_paused`
        # variable.  Creating the variable on startup means users discover the
        # control without having to read the docs.
        try:
            indigo.variables.subscribeToChanges()
            self._ensure_pause_variable()
        except Exception as exc:
            log(f"[Startup] Variable subscription failed: {exc}", level="ERROR")

        # Write the shared site_config.json so the companion optimiser script
        # always uses the same numbers as the plugin (no constant drift).
        self._write_site_config()

        # One line naming the bank-first setting. A deliberate exception to the
        # quiet-boot convention: v5.79.0 changes when the system exports, and a
        # behaviour change that arrives silently on upgrade is not acceptable.
        self._log_bank_first_setting()
        self._log_tariff_override_setting()
        self._log_saving_session_auto_join_setting()
        self._log_axle_account_setting()

        # Auto-update check (best-effort, daily-cached, fully silent on failure).
        try:
            self._check_for_update()
        except Exception as exc:
            self.logger.debug(f"[Update] check error: {exc}")

    def _write_site_config(self):
        """Write a shared site_config.json that companion scripts can read.

        The optimiser script (Python Scripts/openmeteo_battery_optimiser.py)
        used to hardcode duplicates of every site constant — a maintenance
        hazard that caused FLOOD_PREV_FORECAST_MULT to drift (script had 4.0
        while the plugin had 3.0).  Now the plugin is the single source of
        truth: every plugin start, and every PluginConfig save, writes this
        JSON to a stable path.  The script reads it with fallbacks to its
        existing constants in case the file is missing.

        Written to the Python Scripts folder — same place the optimiser
        already reads openmeteo_forecast.json from, so no extra plumbing.
        """
        prefs = self.pluginPrefs
        try:
            capacity   = _as_float(prefs.get("batteryCapacityKwh"), "35.04")
        except (TypeError, ValueError):
            capacity = 35.04
        try:
            efficiency = _as_float(prefs.get("batteryEfficiency"), "94") / 100.0
        except (TypeError, ValueError):
            efficiency = 0.94
        try:
            inv_kw     = _as_float(prefs.get("inverterMaxKw"), "10.0")
        except (TypeError, ValueError):
            inv_kw     = 10.0
        try:
            export_kw  = _as_float(prefs.get("maxExportKw"), "4.0")
        except (TypeError, ValueError):
            export_kw  = 4.0
        try:
            dawn_pct   = self._dawn_target_pct()
        except (TypeError, ValueError):
            dawn_pct   = 10.0
        try:
            winter_pct = _as_float(prefs.get("winterBufferPct"), "20")
        except (TypeError, ValueError):
            winter_pct = 20.0
        try:
            health_pct = _as_float(prefs.get("batteryHealthCutoff"), "1")
        except (TypeError, ValueError):
            health_pct = 1.0
        # Site coordinates — IndigoSecrets first, PluginConfig next, None last.
        # No built-in default: a fresh install must explicitly configure a
        # location (in IndigoSecrets.py or the PluginConfig fields). If neither
        # is set the forecast feature logs an ERROR and skips itself.  The
        # site_config publish below writes None into the JSON in that case.
        def _coord(secrets_value, prefs_key):
            if secrets_value is not None:
                try:
                    return float(secrets_value)
                except (TypeError, ValueError):
                    return None
            raw = (prefs.get(prefs_key) or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        site_lat = _coord(SITE_LATITUDE,  "siteLatitude")
        site_lon = _coord(SITE_LONGITUDE, "siteLongitude")

        # Export rate — best-effort lookup from latest_rates_data; default 12p.
        try:
            export_rate_p = float(self.latest_rates_data.get("export_rate_p", 0.0))
            if export_rate_p <= 0:
                export_rate_p = DEFAULT_EXPORT_RATE_P
        except (TypeError, ValueError):
            export_rate_p = DEFAULT_EXPORT_RATE_P

        # v5.15: publish the auto-calibrated consumption profile so the
        # optimiser script uses the SAME real-inverter-derived numbers the
        # plugin uses, instead of its older Octopus-grid-import-only file.
        # The Octopus profile under-counts true home consumption because it
        # only sees what the grid imports — everything covered by solar or
        # battery during the day is invisible to the smart meter.  For a
        # solar+battery house this dragged the script's daily total down
        # to ~11 kWh when actual usage is ~22 kWh, causing the script to
        # promise overnight exports the plugin then declined (confirmed
        # 12/13-May-2026 incident).
        profile_48 = self.store.get("consumption_profile", []) or []
        consumption_block = None
        if len(profile_48) == 48:
            # Aggregate half-hourly slots into hourly: hour H = slot 2H + slot 2H+1
            # v5.90.1: publish the SAME figures the manager uses. The profile sum is a
            # blend over all days; _need_scales() splits it into weekday and weekend
            # so the week still averages the profile. v5.90.0 published the raw sum
            # as the weekday and sum x uplift as the weekend, so the optimiser's
            # need figure sat 0.9 kWh above the plugin's own in the same message.
            if hasattr(self, "store"):
                _mon_u, _sat_u, _sun_u = self._measured_day_uplifts()
            else:
                _mon_u = MONDAY_UPLIFT_DEFAULT
                _sat_u, _sun_u = SATURDAY_UPLIFT_DEFAULT, SUNDAY_UPLIFT_DEFAULT
            _tf_scale, _mon_scale, _sat_scale, _sun_scale = _need_scales(
                _mon_u, _sat_u, _sun_u)
            _total = sum(profile_48)

            def _hourly(scale):
                return {
                    str(h): round((profile_48[2 * h] + profile_48[2 * h + 1]) * scale, 4)
                    for h in range(24)
                }

            def _blend(a, b, wa=1.0, wb=1.0):
                return {k: round((a[k] * wa + b[k] * wb) / (wa + wb), 4) for k in a}

            # The four real buckets are monday / tuefri / saturday / sunday.
            # `weekday` and `weekend` are KEPT as the Mon-Fri and Sat-Sun blends
            # they used to mean, because an older copy of
            # openmeteo_battery_optimiser.py asks for them by name and a config
            # file that silently lost the key it reads would send that script to
            # its Octopus-grid-only fallback — which under-counts a solar house
            # by about half, and warns about it in a log nobody reads at 20:00.
            # Publishing them as BLENDS rather than as one of the buckets keeps
            # such a reader unbiased over the week instead of merely working.
            hourly_tf  = _hourly(_tf_scale)
            hourly_mon = _hourly(_mon_scale)
            hourly_sat = _hourly(_sat_scale)
            hourly_sun = _hourly(_sun_scale)
            hourly_wd  = _blend(hourly_mon, hourly_tf, 1.0, 4.0)   # Mon-Fri, legacy
            hourly_we  = _blend(hourly_sat, hourly_sun)            # Sat-Sun, legacy
            consumption_block = {
                "source":            "sigen_inverter_48slot",
                "daily_kwh_tuefri":   round(_total * _tf_scale,  2),
                "daily_kwh_monday":   round(_total * _mon_scale, 2),
                "daily_kwh_saturday": round(_total * _sat_scale, 2),
                "daily_kwh_sunday":   round(_total * _sun_scale, 2),
                "daily_kwh_weekday":  round(_total * (_mon_scale + 4 * _tf_scale) / 5.0, 2),
                "daily_kwh_weekend":  round(_total * (_sat_scale + _sun_scale) / 2.0, 2),
                "monday_multiplier":   round(_mon_u, 3),
                "saturday_multiplier": round(_sat_u, 3),
                "sunday_multiplier":   round(_sun_u, 3),
                "weekend_multiplier":  round((_sat_u + _sun_u) / 2.0, 3),
                "window_days":         PROFILE_WINDOW_DAYS,
                "uplift_window_days":  DAY_UPLIFT_WINDOW_DAYS,
                "hourly_kwh": {
                    "tuefri":   hourly_tf,
                    "monday":   hourly_mon,
                    "saturday": hourly_sat,
                    "sunday":   hourly_sun,
                    "weekday":  hourly_wd,
                    "weekend":  hourly_we,
                },
            }

        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": f"SigenEnergyManager v{self.pluginVersion}",
            "battery": {
                "capacity_kwh":       capacity,
                "efficiency":         efficiency,
                "health_cutoff_pct":  health_pct,
            },
            "inverter": {
                "max_kw":         inv_kw,
                "max_export_kw":  export_kw,
            },
            "tariff": {
                "export_rate_p": export_rate_p,
            },
            "resilience": {
                "summer_pct":           dawn_pct,
                "winter_pct":           winter_pct,
                "power_cut_buffer_kwh": 3.5,
            },
            "flood_prevention": {
                "soc_threshold_pct": FLOOD_PREV_SOC_THRESHOLD_PCT,
                "target_pct":        FLOOD_PREV_TARGET_PCT,
                "forecast_mult":     FLOOD_PREV_FORECAST_MULT,
            },
            "site": {
                "latitude":  site_lat,
                "longitude": site_lon,
                "timezone":  "Europe/London",
            },
        }
        # Only include the consumption block when we have a real 48-slot
        # profile so script callers can distinguish "plugin published" from
        # "fall back to local file".
        if consumption_block is not None:
            data["consumption"] = consumption_block
        path = ("/Library/Application Support/Perceptive Automation/"
                "Python Scripts/sigen_site_config.json")
        try:
            # Atomic — the optimiser script reads this file and must never
            # see a half-written copy.
            _atomic_write_json(path, data)
            self.logger.debug(f"[SiteConfig] Wrote shared config to {path}")
        except OSError as exc:
            self.logger.warning(f"[SiteConfig] Cannot write {path}: {exc}")

    def _ensure_pause_variable(self):
        """Create `sigen_manager_paused` in the Sigenergy folder if it doesn't
        already exist.  The current paused state is mirrored in.  Indigo
        variables are always strings — "true" / "false" lower-case."""
        if "sigen_manager_paused" in indigo.variables:
            # Seed the in-memory flag from the variable so a restart-while-paused
            # stays paused (the _evaluate_manager gate self-heals the device label).
            var = indigo.variables["sigen_manager_paused"]
            self.store["manager_paused"] = (
                str(var.value).strip().lower() in ("true", "1", "yes", "on", "paused")
            )
            if self.store["manager_paused"]:
                log("[Setup] Battery manager is PAUSED at startup "
                    "(sigen_manager_paused=true) — not driving the inverter until resumed.")
            return
        folder_id = self._sigenergy_folder_id()
        try:
            indigo.variable.create(
                "sigen_manager_paused",
                value="false",
                folder=folder_id,
            )
            self.store["manager_paused"] = False
            log("[Setup] Created variable 'sigen_manager_paused' — set to 'true' "
                "to pause the battery manager from anywhere in Indigo.")
        except Exception as exc:
            log(f"[Setup] Cannot create pause variable: {exc}", level="WARNING")

    def variableUpdated(self, orig_var, new_var):
        """Indigo callback when any subscribed variable changes value.

        Picks up `sigen_manager_paused` toggles and mirrors them into
        self.store["manager_paused"].  Plain string compare; accepts the usual
        truthy strings ('true', '1', 'yes', 'on').
        """
        super().variableUpdated(orig_var, new_var)
        if new_var.name != "sigen_manager_paused":
            return
        with self._state_lock:
            new_value = str(new_var.value).strip().lower()
            paused = new_value in ("true", "1", "yes", "on", "paused")
            if self.store.get("manager_paused", False) != paused:
                self._set_manager_paused(
                    paused, f"sigen_manager_paused={new_var.value!r}"
                )

    def shutdown(self):
        log(f"{PLUGIN_NAME} shutting down")
        # Give the inverter up BEFORE anything else stops. The executor's journal
        # survives this process, so a claim left standing is a claim the next
        # start has to reconcile against hardware it cannot see the history of —
        # and the set_self_consumption below would otherwise be a write made over
        # the top of a live claim.
        try:
            with self._state_lock:
                self._flux_release("the plugin is shutting down")
        except Exception as exc:
            log(f"[Flux] Release on shutdown failed: {exc!r}", level="WARNING")
        if self.web_dashboard:
            try:
                self.web_dashboard.stop()
                log("[Web] Dashboard stopped")
            except Exception as exc:
                log(f"[Web] Dashboard stop error: {exc}", level="ERROR")
        global _plugin_log_fh
        if _plugin_log_fh is not None:
            try:
                _plugin_log_fh.close()
            except Exception:
                pass
            _plugin_log_fh = None
        if self.modbus and self.modbus.connected:
            # Return to self-consumption on shutdown
            try:
                self.modbus.set_self_consumption()
            except Exception:
                pass
            self.modbus.disconnect()
        self._save_accumulators()

    # ────────────────────────────────────────────────────────────────────────
    # Mac sleep / wake
    # ────────────────────────────────────────────────────────────────────────
    # Defensive: an unattended Mac could sleep for hours. If the inverter is
    # in a forced mode (force-charge, night-export) when sleep happens, that
    # mode would persist until wake — overcharging or over-discharging the
    # battery. Return to self-consumption (the safe baseline) on sleep, then
    # let the manager re-evaluate normally on wake. Modbus reconnects lazily
    # on next poll cycle; we only need to restart the web dashboard.
    def prepare_to_sleep(self):
        log("Mac going to sleep — returning inverter to self-consumption and stopping dashboard")
        # Same reasoning as shutdown, and more urgent: a sleeping Mac stops
        # calling step() altogether, and nothing in the executor is a hardware
        # timer. A Flux mode left running into a four-hour sleep would run for
        # four hours.
        try:
            with self._state_lock:
                self._flux_release("the Mac is going to sleep")
        except Exception as exc:
            log(f"[Flux] Release before sleep failed: {exc!r}", level="WARNING")
        if self.web_dashboard:
            try:
                self.web_dashboard.stop()
            except Exception as exc:
                log(f"[Web] Dashboard stop error on sleep: {exc}", level="ERROR")
        # Return to the safe self-consumption baseline AND release any raised
        # discharge-cutoff floor / VPP engagement (see _disengage_to_safe_baseline),
        # so an unattended sleep can't strand the battery above a raised SOC and
        # force grid import. The manager re-evaluates and re-applies on wake.
        self._disengage_to_safe_baseline("Sleep")
        if self.modbus and self.modbus.connected:
            try:
                self.modbus.disconnect()
            except Exception:
                pass
        self._save_accumulators()
        super().prepare_to_sleep()
    prepareToSleep = prepare_to_sleep

    def wake_up(self):
        log("Mac woke — restarting dashboard; modbus reconnects on next poll, manager re-evaluates within 60s")
        super().wake_up()
        try:
            self.web_dashboard = WebDashboard(
                self, port=WEB_DASHBOARD_PORT,
                bind_host=self._resolve_dashboard_bind(),
                auth_token=self._resolve_dashboard_token())
            self.web_dashboard.start()
            log(f"[Web] Dashboard at {self._dashboard_url()}")
        except Exception as exc:
            log(f"[Web] Dashboard restart on wake failed: {exc}", level="ERROR")
        # Force a fresh modbus poll on the next tick (not wait the full
        # interval). This makes the system feel responsive after sleep.
        self.store["last_modbus"] = 0.0
        self.store["last_manager"] = 0.0
    wakeUp = wake_up

    # ------------------------------------------------------------------ #
    # Web dashboard data provider                                          #
    # ------------------------------------------------------------------ #

    def get_dashboard_data(self):
        """Return a dict of live system data for the web dashboard /api/status."""
        try:
            # v5.45.0: millisecond snapshot under the lock, then the whole
            # payload builds lock-free from consistent local copies — handler
            # threads used to read store live (torn composite reads
            # possible around the midnight counter reset).
            with self._state_lock:
                store  = dict(self.store)
                inv    = self.latest_inverter_data  or {}
                fcast  = self.latest_forecast_data  or {}
                rates  = self.latest_rates_data     or {}
                dec    = self.latest_decision

            tariff_info = rates.get("tariff_info", {})
            active_today_p, active_tomorrow_p = self._rates_for_tariff(
                tariff_info.get("tariff_key", TARIFF_TRACKER), rates)

            pv_w   = int(inv.get("pvPowerWatts",     0))
            bat_w  = int(inv.get("batteryPowerWatts", 0))
            grid_w = int(inv.get("gridPowerWatts",    0))
            home_w = int(inv.get("homePowerWatts",    0))
            soc    = float(inv.get("batterySoc",      0.0))

            # Hourly forecast: {hour_label: kWh}
            # Scaled by today's band factor so the bars (and their per-hour kWh
            # tooltips) sum to the corrected headline forecast rather than the raw
            # model total — same shape-preserving scaling _write_optimiser_file
            # applies to the optimiser JSON. The cached _hourly_p50_* buckets stay
            # raw: bands are recomputed nightly, so the cache must hold the
            # model's own numbers.
            raw_hourly = fcast.get("_hourly_p50_today", {})
            try:
                hour_factor = float(fcast.get("biasFactorToday", 1.0) or 1.0)
            except (TypeError, ValueError):
                hour_factor = 1.0
            hourly = {}
            for key in sorted(raw_hourly.keys()):
                wh = raw_hourly[key]
                try:
                    hour = int(str(key).split(" ")[1].split(":")[0])
                except (IndexError, ValueError):
                    continue
                hourly[f"{hour:02d}:00"] = round(wh / 1000.0 * hour_factor, 2)

            # Self-sufficiency
            home_kwh   = store.get("home_daily_kwh", 0.0)
            import_kwh = store.get("grid_import_daily_kwh", 0.0)
            if home_kwh > 0:
                self_suff = round(max(0.0, (home_kwh - import_kwh) / home_kwh * 100.0), 1)
            else:
                self_suff = 100.0

            # Tomorrow revenue estimate: surplus solar × export rate.
            # Surplus is the optimistic forecast minus expected daily need
            # (auto-calibrated from inverter data, see _build_manager_snapshot).
            # Export rate defaults to 12p (Octopus Outgoing flat) — overridden if
            # the rates_data feed publishes a different export rate.
            tomorrow_solar = float(fcast.get("correctedTomorrowKwh", 0.0))
            profile        = store.get("consumption_profile", []) or []
            tomorrow_need  = sum(profile) if len(profile) == 48 else 22.0
            tomorrow_surplus = max(0.0, tomorrow_solar - tomorrow_need)
            export_rate_p  = DEFAULT_EXPORT_RATE_P
            try:
                rates_export = float(rates.get("export_rate_p", 0.0))
                if rates_export > 0:
                    export_rate_p = rates_export
            except (TypeError, ValueError):
                pass
            # On a banded export tariff the surplus is midday sun, so it is valued
            # at the band covering noon tomorrow, not whichever band is live now
            # (at 17:00 that was the 27.7p peak, overstating it nearly threefold).
            surplus_rate_p = export_rate_p
            if self._export_tariff_is_banded():
                try:
                    _noon = _london_localise(datetime.combine(
                        _london_today() + timedelta(days=1), datetime.min.time())
                        .replace(hour=12))
                    _band = (self._flux_export_band_p(_noon.astimezone(timezone.utc))
                             if _noon is not None else None)
                    if _band is not None:
                        surplus_rate_p = _band
                except Exception:                       # noqa: BLE001
                    pass
            tomorrow_revenue_gbp = round(tomorrow_surplus * surplus_rate_p / 100.0, 2)

            # ---- Today's economics ----
            # All four numbers — actual import cost, export revenue, what the
            # home would have cost on grid alone, and the net financial benefit
            # of having solar today.  None if no import rate is known yet.
            #
            # Prefer the tariffMonitor device state (always populated from the
            # Octopus refresh cycle) over latest_rates_data which is empty for
            # ~30 minutes after a plugin restart.
            import_rate_p = None
            try:
                r = float(active_today_p or 0.0)
                if r > 0:
                    import_rate_p = r
            except (TypeError, ValueError):
                pass
            if import_rate_p is None:
                tariff_dev = self._find_device("tariffMonitor")
                if tariff_dev:
                    try:
                        r = float(tariff_dev.states.get("rateToday", "") or 0.0)
                        if r > 0:
                            import_rate_p = r
                    except (TypeError, ValueError):
                        pass
            if import_rate_p is None:
                # Final fallback: the elec_unit_rate_p Indigo variable, which
                # is written every 30 minutes by _write_energy_summary_variables
                # and persists across plugin restarts (unlike device states,
                # which deviceStartComm clears to "Initialising").
                try:
                    if "elec_unit_rate_p" in indigo.variables:
                        r = float(indigo.variables["elec_unit_rate_p"].value or 0.0)
                        if r > 0:
                            import_rate_p = r
                except (TypeError, ValueError, KeyError):
                    pass

            # v5.110.0: on a banded tariff TODAY's kWh are valued at the bands
            # they moved in. The rates above are the band live NOW, which is the
            # right thing to display and the wrong thing to multiply a whole
            # day's kWh by.
            import_rate_p, today_export_rate_p = self._live_rates_today_p(
                import_rate_p, export_rate_p)
            today_econ = self._compute_daily_economics(
                home_kwh      = home_kwh,
                import_kwh    = import_kwh,
                export_kwh    = float(store.get("grid_export_daily_kwh", 0.0)),
                import_rate_p = import_rate_p,
                export_rate_p = today_export_rate_p,
            )
            yesterday_econ, yesterday_date = self._yesterday_economics(
                export_rate_p          = export_rate_p,
                fallback_import_rate_p = import_rate_p,
            )
            periods = self._period_economics_summary(
                export_rate_p          = export_rate_p,
                fallback_import_rate_p = import_rate_p,
            )
            calendar_months = self._calendar_months_summary(
                export_rate_p          = export_rate_p,
                fallback_import_rate_p = import_rate_p,
            )
            # Isolated so a fault in the (newer, network-touching) whole-house
            # block can never blank the rest of /api/status.
            try:
                whole_house = self._whole_house_summary(
                    import_rate_p = import_rate_p,
                    export_rate_p = today_export_rate_p,
                )
            except Exception as exc:
                self.logger.debug(f"[WholeHouse] summary failed: {exc}")
                whole_house = None
            economics = {
                "today":           today_econ,
                "yesterday":       yesterday_econ,
                "yesterday_date":  yesterday_date,
                "periods":         periods,
                "calendar_months": calendar_months,
                "whole_house":     whole_house,
            }

            # Publish the EFFECTIVE storm release point, not the raw pref —
            # _apply_storm_override clamps it to the active reserve target
            # (max(pref, STORM_SOC_*)), so a pref below the reserve would show
            # a release percentage the override never honours during a storm.
            storm_level_now   = store.get("storm_level", "none")
            storm_release_now = self._storm_export_release_pct()
            if storm_level_now in ("amber", "red"):
                storm_release_now = max(storm_release_now, STORM_SOC_AMBER)
            elif storm_level_now == "yellow":
                storm_release_now = max(storm_release_now, STORM_SOC_YELLOW)

            return {
                "timestamp":  datetime.now().strftime("%H:%M:%S"),
                "battery": {
                    "soc_pct":  round(soc, 1),
                    "power_w":  bat_w,
                    # Published so dashboards can turn SOC into kWh without
                    # hardcoding this system's pack size — energy.html carried
                    # its own 35.04 literal, which is wrong for anyone else
                    # running the plugin.
                    # v5.70.0: read the PREF, not the module constant. Every
                    # control path (_calculate_24h_balance, the dawn reserve,
                    # flood prevention) uses the pref with the constant only
                    # as a fallback, so publishing the constant here meant the
                    # dashboards would have silently disagreed with the battery
                    # logic the moment the pref was changed — and it is about
                    # to be, since the rated capacity is 36.16 kWh and the
                    # measured SOC-to-kWh relationship is ~35.6, not 35.04.
                    "capacity_kwh": _as_float(
                        self.pluginPrefs.get("batteryCapacityKwh"), BATTERY_CAPACITY_KWH),
                    # The inverter's own nameplate (register 30548 / plant
                    # 30083), for comparison against the configured figure.
                    "rated_capacity_kwh": inv.get("ratedCapacityKwh"),
                    # v5.68.0 — what CAN be said about the individual packs.
                    # The inverter publishes no per-pack registers, but the
                    # three aggregates below bound the distribution, and
                    # analyse_pack_balance turns them into a verdict. None
                    # when the figures cannot support one.
                    "temp_c":       inv.get("batteryTempC"),
                    "temp_max_c":   inv.get("batteryMaxTempC"),
                    "temp_min_c":   inv.get("batteryMinTempC"),
                    "cell_v":       inv.get("batteryCellVoltage"),
                    "soh_pct":      inv.get("batterySoh"),
                    "pack_count":   self._pack_count(),
                    "pack_balance": analyse_pack_balance(
                        inv.get("batteryTempC"), inv.get("batteryMaxTempC"),
                        inv.get("batteryMinTempC"), self._pack_count()),
                },
                "grid_quality": {
                    # A grid event is ultimately a frequency problem, so this
                    # is the quantity the whole VPP scheme exists to defend.
                    "frequency_hz": inv.get("gridFrequencyHz"),
                    # v5.71.0 — the REAL measured voltage (register 31011), not
                    # the 230 V nameplate. UK statutory ceiling is 253.0 V and
                    # an inverter must curtail above it, so a high reading here
                    # costs export revenue and the cause is the DNO's network.
                    "voltage_v":     inv.get("gridVoltageV"),
                    "current_a":     inv.get("gridCurrentA"),
                    "statutory_max_v": 253.0,
                    "statutory_min_v": 216.2,
                },
                "inverter_health": {
                    # v5.69.0 — the inverter's own diagnostics, named from the
                    # official V2.7 protocol.
                    "pcs_temp_c":     inv.get("pcsInternalTempC"),
                    "insulation_mohm": inv.get("insulationResistanceMohm"),
                    # An alarm word we cannot decode is still worth reporting
                    # as raised-or-clear; inventing a description for an
                    # unknown code would be worse than saying "something is".
                    "alarm": (None if inv.get("alarm1Raw") is None
                              else ("clear" if int(inv["alarm1Raw"]) == 0 else "raised")),
                },
                "solar": {
                    "power_w":        pv_w,
                    "today_kwh":      round(fcast.get("correctedTodayKwh",     0.0), 1),
                    "tomorrow_kwh":   round(fcast.get("correctedTomorrowKwh",  0.0), 1),
                    "bias_factor":    round(fcast.get("biasFactor",            1.0), 3),
                    "remaining_kwh":  round(fcast.get("remainingTodayKwh",     0.0), 1),
                    "tomorrow_surplus_kwh":  round(tomorrow_surplus, 1),
                    "tomorrow_revenue_gbp":  tomorrow_revenue_gbp,
                    "export_rate_p":         round(export_rate_p, 2),
                    "actual_today_kwh":      round(store.get("pv_daily_kwh", 0.0), 2),
                    "peak_w":                store.get("peak_pv_w", 0),
                    "peak_time":             store.get("peak_pv_time", ""),
                    "lifetime_kwh":          inv.get("pvLifetimeKwh"),
                    "total_kwp":             self._total_kwp(),
                    # v5.67.0: live per-PV-string readings. [] when the
                    # inverter doesn't report them — the dashboards hide the
                    # strip rather than drawing invented zeros.
                    "strings":               self._pv_strings_status(inv),
                },
                "grid": {
                    "power_w": grid_w,
                    "status":  inv.get("gridStatus", "On-grid"),
                },
                "home": {
                    "load_w": home_w,
                },
                "decision": {
                    "action":        dec.action         if dec else "unknown",
                    "reason":        dec.reason         if dec else "",
                    "dawn_viable":   dec.dawn_viable     if dec else True,
                    "soc_at_dawn_kwh": round(dec.soc_at_dawn_kwh if dec else 0.0, 1),
                },
                "tariff": {
                    "name":         tariff_info.get("display_name", "Unknown"),
                    "product_code": tariff_info.get("product_code", ""),
                    "today_p":      active_today_p,
                    "tomorrow_p":   active_tomorrow_p,
                    # v5.110.0: every band on each side with the local times it
                    # runs, so a page can show the whole tariff rather than one
                    # number. Empty tiers mean a flat side: use today_p /
                    # export_rate_p as before.
                    **self._tariff_sides_payload(export_rate_p),
                },
                "today_summary": {
                    "pv_kwh":     round(store.get("pv_daily_kwh",          0.0), 2),
                    "import_kwh": round(import_kwh,                                   2),
                    "export_kwh": round(store.get("grid_export_daily_kwh", 0.0), 2),
                    "home_kwh":   round(home_kwh,                                     2),
                    "peak_soc":   round(store.get("peak_soc",            0.0), 1),
                    "min_soc":    round(store.get("min_soc",           100.0), 1),
                    "self_suff":  self_suff,
                    # v5.89.0
                    "battery_charge_kwh":    round(store.get("battery_charge_daily_kwh", 0.0), 2),
                    "battery_discharge_kwh": round(store.get("battery_discharge_daily_kwh", 0.0), 2),
                    "balance_kwh":           round(store.get("energy_balance_kwh", 0.0), 2),
                    "partial":               bool(store.get("energy_day_partial", False)),
                },
                "vpp": {
                    "state":     store.get("vpp_state",  "idle"),
                    "active":    store.get("vpp_active", False),
                    # Was hardcoded "" from the day this block was written, so
                    # every consumer that appends it produced a dangling
                    # "VPP event announced:" with the one useful fact — WHEN —
                    # missing. Live-spotted 30-07-2026 on the phone.
                    "event_str": self._vpp_event_str(),
                    # The same window in parts, so a page can count down to it
                    # without parsing that sentence. None = nothing announced.
                    "next_event": self._vpp_next_event_info(),
                    # Feed health travels WITH the answer. "No event announced"
                    # and "the feed has been dead for six weeks" look identical
                    # otherwise, and that is exactly how a revoked token hid
                    # from 15-Jun to 30-Jul-2026.
                    "api_status": store.get("vpp_api_error") or "OK",
                    "earnings":   self._vpp_earnings_brief(),
                },
                "storm": {
                    "level": storm_level_now,
                    "export_suppressed": store.get("storm_export_suppressed", False),
                    "export_release_pct": storm_release_now,
                },
                "power_cut": {
                    "events":  (store.get("power_cut_events", []) or [])[-10:],
                    "ongoing": store.get("power_cut_started_at") is not None,
                    "lockout_active": store.get("power_cut_lockout_active", False),
                    "export_suppressed": store.get("power_cut_export_suppressed", False),
                    "lockout_soc_floor": self._power_cut_lockout_soc_floor(),
                    "lockout_min_soc": self._power_cut_lockout_min_soc(),
                    # True when export is running mid-lockout because today's solar
                    # refills the reserve on its own — lets the Lockout chip explain
                    # itself rather than looking like a bug.
                    "solar_release_active": store.get("power_cut_solar_release", False),
                },
                "forecast_accuracy": (
                    self.forecast.get_accuracy_summary(window_days=7)
                    if self.forecast else
                    {"days": 0, "mape_pct": 0.0, "mean_factor": 1.0,
                     "over_count": 0, "under_count": 0}
                ),
                "economics": economics,
                "flags": {
                    "export_active":         store.get("export_active",         False),
                    "solar_overflow_active": store.get("solar_overflow_active", False),
                    "import_active":         store.get("import_active",         False),
                    # Octopus session state. Added 03-Sep-2026 because it was
                    # NOT observable from anywhere: the window cache lives only
                    # in memory, so half an hour before a live session there was
                    # no way to answer "is this armed?" without waiting to see
                    # whether it fired. A feature you cannot check before it runs
                    # can only be verified after it has already failed.
                    "saving_session_export_active":
                        store.get("saving_session_export_active", False),
                    "happy_hour_import_active":
                        store.get("happy_hour_import_active", False),
                    # Live connection state — latest_inverter_data is kept at
                    # last-known-good on failure, so bool(inv) could never go
                    # false again after the first successful poll.
                    "modbus_connected":      bool(self.modbus and self.modbus.connected),
                },
                "octopus_sessions": {
                    # The cached windows, so an armed session is visible BEFORE
                    # its start rather than inferred afterwards from whether the
                    # battery moved. Direction is included because it decides
                    # which way the window drives: TURN_DOWN exports,
                    # WEEKEND_HAPPY_HOUR imports, and anything else drives
                    # nothing at all.
                    "windows":    store.get("saving_sessions_windows") or [],
                    "next_start": store.get("saving_sessions_next_start"),
                    # Everything announced, joined or not, for display. A reader
                    # that shows only `windows` cannot say "there is a session
                    # tonight and you are NOT in it", because an un-joined
                    # session is absent from that list entirely — the absent-state
                    # trap, where silence reads as all-clear.
                    "upcoming":   store.get("saving_sessions_upcoming") or [],
                },
                "hourly_forecast": hourly,
            }
        except Exception as exc:
            return {"error": str(exc), "timestamp": datetime.now().strftime("%H:%M:%S")}

    @property
    def _econ(self):
        """The economics engine, built on first use and rebuilt if its inputs change.

        A property rather than something wired in _init_modules because the test
        harness builds a Plugin with __new__ and sets data_dir and octopus
        afterwards. The clock is passed as a late-bound lambda so patching this
        module's _london_now still reaches it.
        """
        octo = getattr(self, "octopus", None)
        econ = getattr(self, "_economics_impl", None)
        data_dir = getattr(self, "data_dir", None)
        if econ is None or econ.data_dir != data_dir or econ.octopus is not octo:
            econ = Economics(getattr(self, "data_dir", None),
                             octopus=octo, logger=self.logger,
                             now_fn=lambda: _london_now())
            self._economics_impl = econ
        return econ

    @staticmethod
    def _compute_daily_economics(home_kwh, import_kwh, export_kwh,
                                 import_rate_p, export_rate_p):
        """Delegates to economics.Economics — see that module."""
        return Economics._compute_daily_economics(
            home_kwh, import_kwh, export_kwh, import_rate_p, export_rate_p)

    def _settle_whole_house_costs(self):
        """Delegates to economics.Economics — see that module."""
        return self._econ._settle_whole_house_costs()

    def _total_kwp(self):
        """Sum of configured PV array peak power (kWp) — for the solar yield figure."""
        try:
            if self.forecast and getattr(self.forecast, "arrays", None):
                return round(sum(float(a.get("kwp", 0)) for a in self.forecast.arrays), 2)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _wh_card_from_row(rec):
        """Delegates to economics.Economics — see that module."""
        return Economics._wh_card_from_row(rec)

    @staticmethod
    def _wh_build_card(import_kwh, export_kwh, elec_unit_p, export_rate_p,
                       elec_standing_p, gas_kwh, gas_unit_p, gas_standing_p,
                       provisional, gas_estimated):
        """Delegates to economics.Economics — see that module."""
        return Economics._wh_build_card(
            import_kwh, export_kwh, elec_unit_p, export_rate_p, elec_standing_p,
            gas_kwh, gas_unit_p, gas_standing_p, provisional, gas_estimated)

    @staticmethod
    def _wh_provisional_from_row(rec, elec_standing_p, gas_unit_p,
                                 gas_standing_p, gas_est_kwh, fin_elec_unit_p,
                                 has_gas=True):
        """Delegates to economics.Economics — see that module.

        Static: it needs no plugin state, so it must not require a data_dir the
        caller may not have set. A test builds a bare Plugin to call exactly this.
        """
        return Economics._wh_provisional_from_row(
            rec, elec_standing_p, gas_unit_p, gas_standing_p, gas_est_kwh,
            fin_elec_unit_p, has_gas)

    def _whole_house_summary(self, import_rate_p, export_rate_p):
        """Delegates to economics.Economics — see that module.

        Today's running counters live in self.store and are passed in; the
        economics module owns no plugin state.
        """
        return self._econ._whole_house_summary(
            import_rate_p, export_rate_p,
            today_import_kwh=self.store.get("grid_import_daily_kwh", 0.0),
            today_export_kwh=self.store.get("grid_export_daily_kwh", 0.0))

    def _current_elec_standing_p(self):
        """Delegates to economics.Economics — see that module."""
        return self._econ._current_elec_standing_p()

    @staticmethod
    def _row_standing_p(rec, fallback_p):
        """Delegates to economics.Economics — see that module."""
        return Economics._row_standing_p(rec, fallback_p)

    def _period_economics_summary(self, export_rate_p, fallback_import_rate_p):
        """Delegates to economics.Economics — see that module."""
        return self._econ._period_economics_summary(export_rate_p, fallback_import_rate_p)

    def _calendar_months_summary(self, export_rate_p, fallback_import_rate_p,
                                 year=None):
        """Delegates to economics.Economics — see that module."""
        return self._econ._calendar_months_summary(
            export_rate_p, fallback_import_rate_p, year)

    def _yesterday_economics(self, export_rate_p, fallback_import_rate_p):
        """Delegates to economics.Economics — see that module."""
        return self._econ._yesterday_economics(export_rate_p, fallback_import_rate_p)

    def get_dashboard_calendar(self, year):
        """Return calendar_months summary for a specific year.

        Backs the /api/calendar?year=YYYY endpoint so the dashboard's year
        selector can fetch historical years on demand instead of dumping
        every year into every /api/status response.
        """
        try:
            yi = int(year)
        except (TypeError, ValueError):
            yi = datetime.now().year
        # Resolve export rate same as in get_dashboard_data
        export_rate_p = DEFAULT_EXPORT_RATE_P
        try:
            rates_export = float((self.latest_rates_data or {}).get("export_rate_p", 0.0))
            if rates_export > 0:
                export_rate_p = rates_export
        except (TypeError, ValueError):
            pass
        # Best-effort current import rate for fallback
        fallback_rate = None
        try:
            t = (self.latest_rates_data or {}).get("tracker", {}) or {}
            r = float(t.get("today_p") or 0.0)
            if r > 0:
                fallback_rate = r
        except (TypeError, ValueError):
            pass
        if fallback_rate is None:
            try:
                if "elec_unit_rate_p" in indigo.variables:
                    r = float(indigo.variables["elec_unit_rate_p"].value or 0.0)
                    if r > 0:
                        fallback_rate = r
            except (TypeError, ValueError, KeyError):
                pass
        return self._calendar_months_summary(
            export_rate_p          = export_rate_p,
            fallback_import_rate_p = fallback_rate,
            year                   = yi,
        )

    def get_dashboard_years(self):
        """Return the sorted list of years that have at least one daily record."""
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            return {"years": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return {"years": []}
        years = sorted({(r.get("date") or "")[:4] for r in records
                        if (r.get("date") or "")[:4].isdigit()})
        return {"years": years}

    def get_dashboard_history(self, hours=24):
        """Return half-hourly slots for the last N hours from the SQLite store.

        Used by the dashboard's /api/history endpoint to plot SOC and energy
        flows over time.  Returns a JSON-serialisable dict:
            {"hours": N, "slots": [{"t","soc_start","soc_end","pv_kwh",
                                    "import_kwh","export_kwh","home_kwh",
                                    "action"}, ...]}
        """
        db_path = os.path.join(self.data_dir, "energy_timeseries.db")
        if not os.path.exists(db_path):
            return {"hours": hours, "slots": []}
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        con = None
        slots = []
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            cur = con.execute(
                """SELECT slot_start, slot_end,
                          battery_soc_start_pct, battery_soc_end_pct,
                          pv_kwh, grid_import_kwh, grid_export_kwh, home_kwh,
                          manager_action
                     FROM halfhourly
                    WHERE slot_end >= ?
                 ORDER BY slot_end ASC""",
                (cutoff,),
            )
            for row in cur.fetchall():
                slots.append({
                    "t":          row[1],
                    "soc_start":  row[2],
                    "soc_end":    row[3],
                    "pv_kwh":     row[4],
                    "import_kwh": row[5],
                    "export_kwh": row[6],
                    "home_kwh":   row[7],
                    "action":     row[8] or "",
                })
        except sqlite3.Error as exc:
            self.logger.debug(f"[Dashboard] history query failed: {exc}")
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
        return {"hours": hours, "slots": slots}

    def get_dashboard_daily(self, days=30):
        """Return per-day totals for the last N days from daily_history.json.

        Used by /api/daily for the longer-range bar charts.
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            return {"days": days, "records": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return {"days": days, "records": []}
        return {"days": days, "records": records[-days:]}

    # ================================================================
    # Export sync check (v5.19) — Sigenergy vs Octopus settled exports
    # ================================================================
    #
    # Octopus settles export half-hourly readings over ~24-48h, so we only
    # compare days that are at least settle_days (default 3) old. Results
    # are cached on self.store so repeated calls (dashboard polling, action
    # callbacks, etc.) don't hammer the Octopus API.
    EXPORT_SYNC_WINDOW_DAYS    = 7
    EXPORT_SYNC_SETTLE_DAYS    = 3
    COST_SETTLE_WINDOW_DAYS    = 14  # settle whole-house cost for the last N days — wide enough
                                     # that a day whose gas/import settles late (Octopus can lag
                                     # several days, esp. daily-read gas) is not permanently missed
    COST_SETTLE_MIN_SLOTS      = 46  # require a (near-)complete electricity day (48 half-hours)
    EXPORT_SYNC_TOLERANCE_PCT  = 5.0
    EXPORT_SYNC_MIN_DAY_KWH    = 0.5    # below this daily total the % is noise, skip drift check
    EXPORT_SYNC_CACHE_TTL      = 6 * 3600   # 6 h — re-check four times/day at most

    def _compute_export_sync(self, force=False):
        """Compare Sigenergy daily export vs Octopus settled export.

        Window: last EXPORT_SYNC_WINDOW_DAYS settled days
                (skipping the most recent EXPORT_SYNC_SETTLE_DAYS, which
                Octopus may not have published yet).

        Returns a dict:
            {
              "computed_at":  ISO8601 UTC timestamp,
              "window_days":  int,
              "settle_days":  int,
              "tolerance_pct": float,
              "rows": [
                {"date", "sigen_kwh", "octopus_kwh", "diff_kwh",
                 "diff_pct", "status", "slots"}, ...
              ],
              "summary": {
                "days_compared":  int,
                "days_unsettled": int,
                "avg_diff_pct":   float | None,
                "worst": {"date", "diff_pct"} | None,
                "all_within_tolerance": bool,
              },
              "available": bool,    # False if no octopus client or no export MPAN
              "reason":   str,      # populated when available=False
            }
        """
        # Hot cache — skip if recent
        cached = self.store.get("export_sync_cache")
        if not force and cached:
            try:
                age = time.time() - float(cached.get("_cached_at", 0))
                if age < self.EXPORT_SYNC_CACHE_TTL:
                    return cached["data"]
            except (TypeError, ValueError):
                pass

        result = {
            "computed_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window_days":   self.EXPORT_SYNC_WINDOW_DAYS,
            "settle_days":   self.EXPORT_SYNC_SETTLE_DAYS,
            "tolerance_pct": self.EXPORT_SYNC_TOLERANCE_PCT,
            "rows":          [],
            "summary":       {
                "days_compared":  0,
                "days_unsettled": 0,
                "avg_diff_pct":   None,
                "worst":          None,
                "all_within_tolerance": True,
            },
            "available":     True,
            "reason":        "",
        }

        if not self.octopus or not self.octopus.api_key:
            result["available"] = False
            result["reason"]    = "Octopus API key not configured"
            return result
        if not self.export_mpan or not self.export_serial:
            result["available"] = False
            result["reason"]    = (
                "Export MPAN/serial not configured — set OCTOPUS_EXPORT_MPAN "
                "and OCTOPUS_EXPORT_SERIAL in IndigoSecrets.py, or fill in "
                "PluginConfig"
            )
            return result

        # Load daily_history once and index by date
        path = os.path.join(self.data_dir, "daily_history.json")
        history_by_date = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for rec in json.load(f):
                    d = rec.get("date")
                    if d:
                        history_by_date[d] = rec
        except (OSError, ValueError):
            pass

        # Use Europe/London for "today" (matches _check_midnight)
        today      = _london_now()
        today_date = today.date()

        diffs_for_avg = []
        worst_row     = None

        # Iterate D-(settle_days) back through D-(settle_days+window-1), oldest first
        oldest_offset = self.EXPORT_SYNC_SETTLE_DAYS + self.EXPORT_SYNC_WINDOW_DAYS - 1
        for offset in range(oldest_offset, self.EXPORT_SYNC_SETTLE_DAYS - 1, -1):
            day = today_date - timedelta(days=offset)
            date_str = day.strftime("%Y-%m-%d")

            sigen_rec = history_by_date.get(date_str)
            sigen_kwh = None
            if sigen_rec is not None:
                try:
                    sigen_kwh = float(sigen_rec.get("grid_export_kwh", 0.0))
                except (TypeError, ValueError):
                    sigen_kwh = None

            try:
                octo = self.octopus.get_export_kwh_for_date(
                    date_str, self.export_mpan, self.export_serial
                )
            except Exception as exc:
                self.logger.debug(f"[ExportSync] Octopus fetch error for {date_str}: {exc}")
                octo = None

            row = {
                "date":        date_str,
                "sigen_kwh":   round(sigen_kwh, 2) if sigen_kwh is not None else None,
                "octopus_kwh": None,
                "diff_kwh":    None,
                "diff_pct":    None,
                "slots":       0,
                "status":      "unsettled",
            }

            if octo is None:
                row["status"] = "fetch_error"
            else:
                row["slots"] = int(octo.get("slots", 0))
                if octo.get("kwh") is None:
                    row["status"] = "unsettled"
                else:
                    row["octopus_kwh"] = round(float(octo["kwh"]), 2)
                    if sigen_kwh is not None:
                        diff = sigen_kwh - row["octopus_kwh"]
                        row["diff_kwh"] = round(diff, 2)
                        # Use the LARGER of the two as the denominator —
                        # protects against tiny Sigenergy values inflating %.
                        denom = max(sigen_kwh, row["octopus_kwh"], 0.001)
                        pct = (diff / denom) * 100.0 if denom > 0 else 0.0
                        row["diff_pct"] = round(pct, 1)
                        # Near-zero export days (winter, rain) — a 0.05 kWh
                        # delta on a 0.07 kWh day reads as 71% but is
                        # operationally meaningless. Force-ok and exclude
                        # from the average / worst summary stats.
                        if denom < self.EXPORT_SYNC_MIN_DAY_KWH:
                            row["status"] = "ok"
                        else:
                            row["status"] = (
                                "ok" if abs(pct) <= self.EXPORT_SYNC_TOLERANCE_PCT
                                else "drift"
                            )
                            diffs_for_avg.append(pct)
                            if worst_row is None or abs(pct) > abs(worst_row["diff_pct"]):
                                worst_row = row
                    else:
                        row["status"] = "no_sigen_record"

            result["rows"].append(row)

        # Summary aggregates
        compared    = [r for r in result["rows"] if r["status"] in ("ok", "drift")]
        unsettled   = [r for r in result["rows"] if r["status"] == "unsettled"]
        result["summary"]["days_compared"]  = len(compared)
        result["summary"]["days_unsettled"] = len(unsettled)
        if diffs_for_avg:
            result["summary"]["avg_diff_pct"] = round(
                sum(diffs_for_avg) / len(diffs_for_avg), 2
            )
        if worst_row is not None:
            result["summary"]["worst"] = {
                "date":     worst_row["date"],
                "diff_pct": worst_row["diff_pct"],
            }
        result["summary"]["all_within_tolerance"] = all(
            r["status"] == "ok" for r in compared
        )

        # Cache
        self.store["export_sync_cache"] = {
            "_cached_at": time.time(),
            "data":       result,
        }
        return result

    def _log_export_sync_summary(self):
        """Emit one INFO line summarising the export-sync window.

        Called from _check_midnight (so it runs once a day) and skipped
        silently if the feature isn't available.
        """
        try:
            data = self._compute_export_sync(force=True)
        except Exception as exc:
            self.logger.error(f"[ExportSync] Summary computation failed: {exc}")
            return
        if not data.get("available", False):
            return  # disabled by config — stay quiet
        s = data["summary"]
        if s["days_compared"] == 0:
            return  # nothing to say yet (cold start, no settled history)
        worst_str = ""
        if s["worst"]:
            worst_str = (
                f"  worst: {s['worst']['date']} "
                f"{s['worst']['diff_pct']:+.1f}%"
            )
        avg_str = (
            f"{s['avg_diff_pct']:+.1f}%"
            if s["avg_diff_pct"] is not None else "n/a"
        )
        flag = "" if s["all_within_tolerance"] else "  [DRIFT >5%]"
        log(
            f"[ExportSync] {s['days_compared']}d avg diff {avg_str}"
            f"{worst_str}{flag}"
        )

    def get_dashboard_export_sync(self):
        """Public accessor used by the /api/export-sync dashboard endpoint."""
        try:
            return self._compute_export_sync(force=False)
        except Exception as exc:
            return {"available": False, "reason": f"compute failed: {exc}"}

    def deviceStartComm(self, dev):
        dev.stateListOrDisplayStateIdChanged()
        dev = indigo.devices[dev.id]   # re-fetch: state list changed, local object is stale
        try:
            self._set_device_initial_state(dev)
        except Exception as e:
            self.logger.error(f"deviceStartComm error for {dev.name}: {e}")

    def deviceStopComm(self, dev):
        pass

    @staticmethod
    def didDeviceCommPropertyChange(oldDevice, newDevice):
        """Suppress unnecessary deviceStopComm/deviceStartComm cycles.

        Devices in this plugin are created and managed internally from Modbus
        polling, Open-Meteo forecast updates and snapshot writes; there are no
        user-editable pluginProps that justify a comm restart. Returning False
        prevents Indigo from cycling comm on every internal
        replacePluginPropsOnServer write.
        """
        return False

    def _set_device_initial_state(self, dev):
        """Write placeholder states and state image for a device on startup."""
        type_id = dev.deviceTypeId

        if type_id == "sigenergyInverter":
            # Only the CONNECTION states are seeded. batterySoc and the power
            # states are real charted numbers — writing 0s here injected a
            # phantom 0% SOC sample into SQL history and fired any user
            # SOC-threshold trigger on every restart. The states simply hold
            # their last real values until the first Modbus poll (~5-10s).
            dev.updateStatesOnServer([
                {"key": "modbusConnected",   "value": "False"},
                {"key": "lastUpdate",        "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

        elif type_id == "batteryManager":
            dev.updateStatesOnServer([
                {"key": "managerStatus", "value": "Initialising"},
                {"key": "currentAction", "value": "self_consumption"},
                # currentMode (List enum) is intentionally NOT seeded here:
                # the state-list registration kicked off by
                # stateListOrDisplayStateIdChanged() is async and hasn't
                # completed at deviceStartComm time, so an immediate write
                # logs a spurious "state key not defined" ERROR. Indigo
                # registers the enum with its first Option (selfConsumption)
                # as the default, and the first evaluate tick (<=5s) writes
                # the real mode — so nothing is lost.
                {"key": "currentReason", "value": "Starting up"},
                # -1 = NOT REPORTED. Seeded here rather than left to the first
                # tick because a freshly created Integer state materialises as
                # 0, and 0 is a REAL balance meaning "you have no tokens" — a
                # claim the API has not made yet at this point in startup. The
                # gap is only seconds, but it is seconds of the state asserting
                # something false, which is the whole failure mode this -1
                # convention exists to prevent.
                {"key": "happyHourTokens", "value": -1},
                {"key": "dawnViable",    "value": ""},
                {"key": "socAtDawn",     "value": ""},
                {"key": "lastUpdate",    "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "solarForecast":
            dev.updateStatesOnServer([
                {"key": "todayKwh",       "value": "0.0"},
                {"key": "tomorrowKwh",    "value": "0.0"},
                {"key": "forecastStatus", "value": "Initialising"},
                {"key": "lastUpdate",     "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "tariffMonitor":
            dev.updateStatesOnServer([
                {"key": "tariffActive", "value": "Initialising"},
                {"key": "rateToday",    "value": ""},
                {"key": "rateTomorrow", "value": ""},
                {"key": "lastUpdate",   "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "axleVppMonitor":
            dev.updateStatesOnServer([
                {"key": "vppStatus",        "value": "Standby"},
                {"key": "vppState",         "value": "idle"},
                {"key": "vppLastExportKwh", "value": "0.00"},
                {"key": "lastUpdate",       "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

    def runConcurrentThread(self):
        """Main 10-second polling loop.

        A failure in any single tick task must not kill the whole polling loop —
        one bad modbus read / forecast parse / VPP poll should be logged and
        retried on the next tick, not take the manager offline until a plugin
        restart. StopThread still propagates so shutdown stays clean. (Since
        v5.45.0 the tick does not hold _state_lock itself — each stage locks
        only its merge section — so no lock is ever held across the retry.)
        """
        try:
            while True:
                now = time.time()
                try:
                    self._tick(now)
                except self.StopThread:
                    raise
                except Exception:
                    self.logger.exception(
                        "[Tick] Unhandled error in poll tick — continuing to next tick"
                    )
                # Sleep the REMAINDER of the interval, not the interval on TOP of
                # the work. The tick was doing its work and then sleeping the full
                # interval, so the real period was always (work + interval) and the
                # advertised "5 seconds" never happened: measured 43 s before the
                # v1.13 read tiering and 12 s after, against a 5 s setting.
                # Per-task interval checks inside _tick gate everything else, so
                # waking earlier costs nothing but a few float comparisons — and a
                # long interval is unaffected, because there the work is short and
                # the sleep is still nearly the whole interval.
                self.sleep(self._tick_sleep_seconds(
                    getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL),
                    time.time() - now))
        except self.StopThread:
            pass

    # ================================================================
    # Main Poll Tick
    # ================================================================

    @staticmethod
    def _tick_sleep_seconds(poll_s, tick_took, base=10.0, floor=0.2):
        """Seconds to sleep after a tick that took `tick_took` to run.

        The remainder of the interval, so a poll starts every `poll_s` — or as
        soon as the last one finishes when the read is slower than that, which
        it is here (the Modbus read is 8 throttled transactions, ~7 s). The
        floor is belt-and-braces: work can only exceed the budget when it was
        genuinely slow, so a hot loop is not reachable, but 0.2 s costs nothing
        and removes the class of bug entirely.
        """
        return max(floor, min(poll_s, base) - max(0.0, tick_took))

    @staticmethod
    def _accum_interval_h(elapsed_s, poll_s):
        """Hours to attribute to one accounting pass, from MEASURED elapsed time.

        The fallback watt-integrators used the CONFIGURED poll interval, which
        is not how often a pass actually happens — 43 s of real cycle against a
        5 s setting meant they under-counted eightfold whenever the inverter's
        own daily registers were unavailable and the fallback ran. Clamped so a
        reconnect after a long outage cannot dump one huge slab into a daily
        total, and floored at zero so a clock step backwards cannot subtract.
        """
        return min(max(elapsed_s, 0.0), max(60.0, poll_s * 3.0)) / 3600.0

    def _tick(self, now):
        """Called every 10 seconds. Dispatches all timed tasks.

        v5.45.0 LOCKING MODEL: the tick no longer holds _state_lock for its
        whole duration (it used to stall every action callback and dashboard
        request behind a ~16-20s Modbus cycle or a slow HTTP fetch). Instead:
          - network stages (_poll_modbus, _refresh_forecast,
            _refresh_octopus_rates, _poll_vpp, _check_storm_watch,
            _settle_whole_house_costs) run their I/O UNLOCKED and take the
            lock only to merge results into the store;
          - control stages (_evaluate_manager incl. verify+act,
            _check_midnight, _check_scheduled_import) are SELF-LOCKING and run
            entirely under the lock — hardware decisions must stay serialised
            with the locked action callbacks;
          - the last_* stamps below are scalar dict ops with the tick as the
            only regular writer — safe without the lock.
        Contract pinned by test_concurrency.py.
        """
        # Date rotation only needs checking every ~hour (Phase 4C).  Skipping
        # the per-tick filesystem stat avoids 8,640 redundant calls/day.
        if now - self.store.get("last_log_check", 0.0) >= 3600.0:
            _ensure_plugin_log(self.data_dir)
            self.store["last_log_check"] = now

        # 1. Modbus poll. Stamp BEFORE the call: the outage back-off inside
        # _apply_modbus_result pushes last_modbus into the future, and
        # stamping afterwards would clobber it (which is exactly what made
        # the v5.43.0 back-off silently ineffective — caught in the v5.45.0
        # locking review, pinned by test_outage_backoff_survives_the_tick).
        if now - self.store["last_modbus"] >= getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL):
            self.store["last_modbus"] = now
            self._poll_modbus()

        # 2. Solar forecast (before manager so decision always has fresh data)
        if now - self.store["last_forecast"] >= FORECAST_FETCH_INTERVAL:
            self._refresh_forecast()
            self.store["last_forecast"] = now

        # 2b. Octopus Flux supervisor (draft, off by default). Runs on EVERY tick
        # and BEFORE the manager, deliberately:
        #   * before, so that within one tick Flux has already claimed or let go
        #     by the time the manager looks at the inverter — the manager must
        #     never act on registers a claim is about to change, and must never
        #     have its own act pass fight a live claim;
        #   * every tick rather than on the manager's 60 s cadence, because
        #     letting go has to be prompt. Taking control is rate-limited inside
        #     the supervisor's own stand-down rules; giving it up is not.
        # Costs nothing when the feature is off: one preference read and a return.
        self._flux_supervisor_tick()

        # 3. Battery manager evaluation (every 60s — matches Modbus poll frequency)
        if now - self.store["last_manager"] >= MANAGER_EVAL_INTERVAL:
            self._evaluate_manager()
            self.store["last_manager"] = now

        # 4. Octopus rates
        if now - self.store["last_octopus"] >= OCTOPUS_RATES_INTERVAL:
            self._refresh_octopus_rates()
            self.store["last_octopus"] = now

        # 5. Consumption profile (daily)
        if now - self.store["last_profile"] >= OCTOPUS_PROFILE_INTERVAL:
            self._refresh_consumption_profile()
            self.store["last_profile"] = now

        # 5b. Whole-house cost settle (every 6h — backfill settled gas/import/cost)
        if now - self.store.get("last_cost_settle", 0.0) >= COST_SETTLE_INTERVAL:
            self._settle_whole_house_costs()
            self.store["last_cost_settle"] = now

        # 6. VPP polling (adaptive)
        vpp_interval = self._vpp_poll_interval()
        if now - self.store["last_vpp"] >= vpp_interval:
            self._poll_vpp()
            self.store["last_vpp"] = now

        # 7. Accumulator save
        if now - self.store["last_acc_save"] >= ACCUMULATOR_SAVE_INTERVAL:
            self._save_accumulators()
            self.store["last_acc_save"] = now

        # 8. Daily midnight tasks
        self._check_midnight()

        # 9. Check scheduled import
        self._check_scheduled_import()

        # 10. Storm watch (every 2 hours)
        if now - self.store["last_storm_watch"] >= STORM_WATCH_INTERVAL:
            self._check_storm_watch()
            self.store["last_storm_watch"] = now

        # 10b. Saving Sessions — notify on newly-announced events, and refresh the
        # joined-window cache. Hourly normally; every 10 min once a known session is
        # within SAVING_SESSIONS_SOON_HOURS, because OPTING IN is a thing the owner
        # does in the Octopus app minutes before the window and we would otherwise
        # not see it until after the session had started.
        if now - self.store["last_saving_sessions"] >= self._saving_sessions_interval():
            self._check_saving_sessions()
            self.store["last_saving_sessions"] = now

        # 11. Write energy summary to Indigo variables + SQLite (every 30 min)
        if now - self.store["last_energy_var"] >= ENERGY_VAR_INTERVAL:
            self._log_halfhourly_to_db()
            self._write_energy_summary_variables()
            self.store["last_energy_var"] = now

        # 12. Unconfirmed VPP hand-back — re-assert on the 10s tick (v5.64)
        self._retry_vpp_handback()

        # 12a. Axle settlement mail (v5.95.0). Runs BEFORE the freshness check
        # below, so an import found here clears the staleness warning on the same
        # pass rather than leaving the log contradicting itself for six hours.
        if now - self.store.get("last_mail_scan", 0.0) >= AXLE_MAIL_SCAN_INTERVAL:
            try:
                self._scan_axle_mail()
            except Exception as exc:
                vpp_log(f"[VPP] Axle mail scan failed: {exc}", "WARNING")
            self.store["last_mail_scan"] = now

        # 12a-ii. Axle account read (v5.103.0). AFTER the mail scan, so on a
        # tick where both run the account's own figures are the last word -
        # they are Axle's authentic rows, where the mail gives a row we built
        # from prose. Both are window-keyed, so the order only decides which
        # wins a tie, and it should be this one.
        if now - self.store.get("last_account_poll", 0.0) >= AXLE_ACCOUNT_POLL_INTERVAL:
            try:
                self._poll_axle_account()
            except Exception as exc:
                vpp_log(f"[VPP] Axle account read failed: {exc}", "WARNING")
            self.store["last_account_poll"] = now

        # 12b. Earnings-ledger freshness (v5.92.0). Local file work only.
        if now - self.store.get("last_ledger_check", 0.0) >= VPP_LEDGER_CHECK_INTERVAL:
            self._check_ledger_freshness()
            self.store["last_ledger_check"] = now

        # 13. Happy Hour overrun backstop — independent of the primary end path
        self._check_happy_hour_overrun()

    def _retry_vpp_handback(self):
        """Re-assert Self Consumption after a VPP hand-back that was never confirmed.

        Bounds the exposure at one 10s tick instead of the 60s manager
        cycle. Deliberately small and self-terminating:

          * it only ever writes the SAFE baseline (0x02, no limits), which is what
            the manager would ask for in this state anyway, so a spurious run
            costs nothing;
          * it clears the flag the moment a write is confirmed, so it cannot spin;
          * it is gated on the state machine being genuinely IDLE with no import
            or export engaged, so it can never fight a new window. _vpp_transition
            clears the flag on any engagement as well — belt and braces, because
            re-asserting 0x02 during a live export would cost a paid window.

        Not a general retry framework. Predbat's #4477 ended by DELETING the
        machinery its own earlier rounds had added; the lesson taken here is to
        confirm the one write that matters and stop there.
        """
        if not self.store.get("vpp_handback_pending"):
            return
        if self.store.get("vpp_state", VPP_IDLE) != VPP_IDLE:
            self.store["vpp_handback_pending"] = False
            return
        if self.store.get("export_active") or self.store.get("import_active"):
            self.store["vpp_handback_pending"] = False
            return
        if not (self.modbus and self.modbus.connected):
            return          # nothing to do until the socket is back
        if self.modbus.set_self_consumption():
            self.store["vpp_handback_pending"] = False
            vpp_log("[VPP] Hand-back to Self Consumption confirmed on retry — "
                "inverter is back on the safe baseline.")

    # ================================================================
    # Modbus Polling
    # ================================================================

    def _poll_modbus(self):
        """Read all inverter registers and update sigenergyInverter device states.

        v5.45.0: the throttled read cycle (~16-20s) runs WITHOUT _state_lock —
        only the merge of the result takes it. A locked callback commanding a
        write mid-cycle interleaves safely (the modbus client serialises each
        transaction internally).
        """
        if not self.modbus:
            return

        data = self.modbus.read_all()   # NETWORK — unlocked
        with self._state_lock:
            self._apply_modbus_result(data)

    def _apply_modbus_result(self, data):
        """Merge a read_all() result into store/devices. Caller holds the lock."""
        if data is None:
            # Track consecutive failures: one WARNING at the transition (the
            # modbus layer's per-cycle lines cover detail), then widen the
            # effective poll gap so a long outage doesn't re-run a failed
            # cycle every few seconds (60s after 3 fails, 300s after 10).
            fails = self.store.get("modbus_consecutive_failures", 0) + 1
            self.store["modbus_consecutive_failures"] = fails
            if fails == 1:
                log("[Modbus] Inverter poll failed — holding last-known-good "
                    "snapshot, backing off while offline", level="WARNING")
            if fails >= 10:
                backoff = 300
            elif fails >= 3:
                backoff = 60
            else:
                backoff = 0
            if backoff:
                self.store["last_modbus"] = (
                    time.time() + backoff
                    - getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL))
            self._update_inverter_device_offline()
            return

        if self.store.get("modbus_consecutive_failures", 0) > 0:
            log(f"[Modbus] Inverter poll recovered after "
                f"{self.store['modbus_consecutive_failures']} failed attempt(s)")
        self.store["modbus_consecutive_failures"] = 0
        data["_read_at"] = time.time()   # staleness stamp for _evaluate_manager
        self.latest_inverter_data = data

        # Detect grid-status transitions in either direction so the power-cut
        # log captures both the start and end of every outage.  Key the edges on a
        # GENUINE off-grid status (register 30009 = 1/2 → "Off-grid …") and on an
        # in-progress-outage flag, NOT on "any value that isn't On-grid": a transient
        # unmapped read ("Unknown (N)") must not fire a false outage notification, and
        # an Unknown→On-grid blip must not fire a false "restored".
        new_grid_status  = data.get("gridStatus", "On-grid")
        in_outage        = self.store.get("power_cut_started_at") is not None
        is_off_grid      = new_grid_status.startswith("Off-grid")
        now_utc          = datetime.now(timezone.utc)
        if is_off_grid and not in_outage:
            # Outage started
            self.store["power_cut_started_at"] = now_utc
            event_str = (
                f"{now_utc.strftime('%Y-%m-%d %H:%M')} UTC — grid LOST "
                f"({new_grid_status})"
            )
            self.store["power_cut_events"].append(event_str)
            # Cap log at 100 entries to bound memory
            if len(self.store["power_cut_events"]) > 100:
                self.store["power_cut_events"] = self.store["power_cut_events"][-100:]
            log(f"[PowerCut] Grid lost — entering {new_grid_status} mode",
                level="WARNING")
            self._save_accumulators()   # crash-safe copy — a restart mid-outage must not re-alert
            self._send_power_cut_notification("lost", detail=new_grid_status)
        elif in_outage and new_grid_status == "On-grid":
            # Outage ended (we were genuinely off-grid and the grid is now back)
            started = self.store.get("power_cut_started_at")
            duration_str = ""
            if started:
                seconds = (now_utc - started).total_seconds()
                if seconds < 120:
                    duration_str = f" (duration {seconds:.0f}s)"
                elif seconds < 3600:
                    duration_str = f" (duration {seconds/60:.1f} min)"
                else:
                    duration_str = f" (duration {seconds/3600:.1f} h)"
            event_str = (
                f"{now_utc.strftime('%Y-%m-%d %H:%M')} UTC — grid RESTORED"
                f"{duration_str}"
            )
            self.store["power_cut_events"].append(event_str)
            if len(self.store["power_cut_events"]) > 100:
                self.store["power_cut_events"] = self.store["power_cut_events"][-100:]
            self.store["power_cut_started_at"]  = None
            self.store["power_restored_time"]   = now_utc
            # Only arm the export lockout if export is actually enabled — otherwise
            # there is nothing to lock out and _resolve_export_lockout would fire a
            # phantom 'lockout cleared' on its next pass.
            export_pref = bool(self.pluginPrefs.get("exportEnabled", False))
            if export_pref:
                self.store["power_cut_lockout_active"] = True
                self.pluginPrefs["powerRestoredTime"]  = now_utc.isoformat()
                self._save_accumulators()   # crash-safe copy — prefs alone only persist on graceful shutdown
                # Name BOTH release rules. Since v5.50.0 the lockout also lifts
                # when the day's own solar can refill the reserve unaided, so a
                # message quoting only the SOC floor makes an early resume look
                # like a fault (live: 26-Jul-2026, export resumed at 74%).
                log(
                    f"[PowerCut] Grid restored after outage — export locked for "
                    f"{POWER_CUT_LOCKOUT_HOURS:.0f} hours as precaution (export resumes "
                    f"early if SOC reaches {self._power_cut_lockout_soc_floor():.0f}%, or "
                    f"if today's solar can refill that reserve on its own)",
                    level="WARNING",
                )
                # Record what the solar rule thinks RIGHT NOW, with its figures.
                # On 26-Jul-2026 export sat suppressed for 20 minutes after the
                # restore and nothing in the log said what was being judged, so
                # the only honest answer afterwards was "we cannot tell". One
                # line here makes the next one answerable.
                outlook = self._solar_refill_outlook(
                    _as_float(data.get("batterySoc"), None))
                if outlook:
                    log(f"[PowerCut] Solar-refill outlook at restore: "
                        f"{'RELEASES' if outlook['releases'] else 'holds'} — "
                        f"{outlook['surplus_kwh']:.1f} kWh spare vs "
                        f"{outlook['needed_kwh']:.1f} kWh needed, "
                        f"daytime={outlook['is_daytime']}")
                self._trigger_event("powerCutLockoutStarted")
            else:
                log("[PowerCut] Grid restored after outage (export disabled — "
                    "no export lockout needed)", level="WARNING")
                self._save_accumulators()   # clear the persisted outage marker (export branch saved above)
            self._send_power_cut_notification("restored", detail=duration_str)
        self.store["grid_status_prev"] = new_grid_status

        # Update daily energy accumulators
        self._observe_energy_counters(data)

        # Accumulate home load into persistent half-hourly profile
        self._refresh_away_state()
        self._accumulate_home_profile(max(0.0, float(data.get("homePowerWatts", 0))))

        # Update device states
        self._update_inverter_device(data)

    def _observe_energy_counters(self, data):
        """Feed this cycle's lifetime counters to DailyEnergy and project the day's
        figures into the store.

        v5.89.0 replaced the accumulate-and-reset model. Each daily figure is
        `latest - anchor` on a plant LIFETIME counter (pv 30088, load 30094,
        ESS charge/discharge 30200/30204, grid import/export 30216/30220),
        anchored at Europe/London midnight. The inverter's own daily counters
        (30092, 30566, 30572) are read but never trusted as the figure: they run
        on the inverter's clock and are served only on the cycle they were read.
        They RECOVER a missing anchor (anchor = lifetime - device_daily) and
        cross-check the derived house figure. Design: docs/daily-energy-revamp.md.
        """
        de = getattr(self, "daily_energy", None)
        if de is None:
            return
        today    = _local_today_str()
        readings = readings_from_data(data)
        recovery = recovery_from_data(data)
        fresh    = data.get("_energyReadAt") is not None
        read_at  = float(data.get("_energyReadAt") or data.get("_read_at") or time.time())
        try:
            soc = float(data.get("batterySoc"))
        except (TypeError, ValueError):
            soc = None
        if de.today_date is not None and today != de.today_date:
            # Local midnight has passed since the last observe. Keep the day just
            # ended as the projection stood (the fallback for any key the anchors
            # cannot settle), and force a fresh read of every lifetime block on the
            # next cycle so the new day's anchor is a post-midnight boundary reading
            # rather than a cached one — the cache is what froze 4/5-Sep-2026.
            self.store["energy_yesterday_projection"] = self._energy_projection_snapshot(de.today_date)
            mb = getattr(self, "modbus", None)
            if mb is not None:
                try:
                    mb.mark_slow_read_due(*ENERGY_BLOCK_KEYS)
                except Exception as exc:
                    self.logger.debug(f"[Energy] could not mark the energy blocks due: {exc}")
        de.observe(readings, read_at, today, soc_pct=soc, recovery=recovery, fresh=fresh)
        if de.last_backwards:
            log(f"[Energy] Lifetime counter went BACKWARDS for {', '.join(de.last_backwards)} "
                f"— the plant re-based it; re-anchored from here, so today's figure for it "
                f"restarts at zero", level="WARNING")
        self._project_daily_energy(data)
        self._reconcile_daily_energy(data, today)
        self._track_energy_extremes(data)

    def _project_daily_energy(self, data=None):
        """Write DailyEnergy's figures into the legacy store keys (read-only projection)."""
        de = getattr(self, "daily_energy", None)
        if de is None:
            return
        t = de.today()
        v = t["values"]
        data = data or {}
        # House: its own lifetime counter; failing that (a firmware without
        # 30094) the identity, which the other five counters give exactly — the
        # plant computes its house figure the same way.
        home = v["home"]
        if home is None and None not in (v["pv"], v["gridImport"], v["gridExport"],
                                         v["batteryCharge"], v["batteryDischarge"]):
            home = round(max(0.0, v["pv"] + v["gridImport"] + v["batteryDischarge"]
                                  - v["gridExport"] - v["batteryCharge"]), 2)
        # Battery flow: lifetime-anchored; failing that (no 30200/30204) the
        # inverter's own daily counters, and only from a cycle that read them.
        chg = v["batteryCharge"]
        dis = v["batteryDischarge"]
        if chg is None and data.get("batteryDailyChargeKwh") is not None:
            chg = float(data["batteryDailyChargeKwh"])
        if dis is None and data.get("batteryDailyDischargeKwh") is not None:
            dis = float(data["batteryDailyDischargeKwh"])
        for store_key, value in (
            ("pv_daily_kwh",                v["pv"]),
            ("grid_import_daily_kwh",       v["gridImport"]),
            ("grid_export_daily_kwh",       v["gridExport"]),
            ("home_daily_kwh",              home),
            ("battery_charge_daily_kwh",    chg),
            ("battery_discharge_daily_kwh", dis),
        ):
            if value is not None:                 # absent keeps the last projection
                self.store[store_key] = float(value)
        self.store["energy_day_partial"] = bool(t["partial"])
        residual = de.residual()
        if residual is not None:
            self.store["energy_balance_kwh"] = residual

    def _energy_projection_snapshot(self, date_str=None):
        """The projection as a plain dict, stamped with the day it describes."""
        de = getattr(self, "daily_energy", None)
        t  = de.today() if de is not None else {"sources": {}, "partial": False}
        return {
            "date":             date_str or self.store.get("today_date"),
            "pv":               float(self.store.get("pv_daily_kwh", 0.0) or 0.0),
            "gridImport":       float(self.store.get("grid_import_daily_kwh", 0.0) or 0.0),
            "gridExport":       float(self.store.get("grid_export_daily_kwh", 0.0) or 0.0),
            "home":             float(self.store.get("home_daily_kwh", 0.0) or 0.0),
            "batteryCharge":    float(self.store.get("battery_charge_daily_kwh", 0.0) or 0.0),
            "batteryDischarge": float(self.store.get("battery_discharge_daily_kwh", 0.0) or 0.0),
            "balance":          float(self.store.get("energy_balance_kwh", 0.0) or 0.0),
            "partial":          bool(self.store.get("energy_day_partial", False)),
            "sources":          dict(t.get("sources") or {}),
        }

    def _settled_pv_for_date(self, date_str):
        """Settled PV kWh for one past day, read from daily_history.json.

        v5.106.0, for repair_zero_actuals. The daily record is written from
        _energy_day_totals, i.e. the difference of two boundary anchors, so it is
        the figure the accuracy record should have been paired with all along.

        The file is read ONCE per call sequence and held on the instance: the
        repair asks for up to 365 dates, and re-reading a never-pruned history
        file for each of them would turn a startup task into a stall.

        Returns None when the day is absent or unusable, never 0.0 — a zero here
        is indistinguishable from the bug being repaired.
        """
        index = getattr(self, "_settled_pv_index", None)
        if index is None:
            index = {}
            path  = os.path.join(self.data_dir, "daily_history.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for rec in json.load(f) or []:
                        day = rec.get("date")
                        pv  = rec.get("pv_kwh")
                        # A partial day was never anchored on both boundaries, so
                        # its PV is a projection. Grading a forecast against a
                        # guess is the failure this whole repair exists to undo.
                        if day and pv is not None and not rec.get("energy_partial"):
                            index[str(day)] = float(pv)
            except (OSError, ValueError, TypeError) as exc:
                self.logger.debug(f"[Forecast] Cannot index daily history: {exc}")
            self._settled_pv_index = index
        value = index.get(str(date_str))
        return value if (value is not None and value > 0.0) else None

    def _energy_day_totals(self, date_str):
        """The six figures for `date_str`, for the daily record.

        A COMPLETED day is anchor[next day] - anchor[day] — two boundary readings,
        exact by construction, immune to the order the midnight tasks ran in.
        Any key the anchors cannot settle falls back to the projection as it
        stood when that day ended, which is what v5.88 recorded for every key.
        """
        snap = self.store.get("energy_yesterday_projection")
        base = (dict(snap) if isinstance(snap, dict) and snap.get("date") == date_str
                else self._energy_projection_snapshot(date_str))
        de = getattr(self, "daily_energy", None)
        c  = de.completed(date_str) if de is not None else None
        if c is None:
            return base
        v = c["values"]
        for key in ENERGY_KEYS:
            if v[key] is not None:
                base[key] = v[key]
        if v["home"] is None and None not in (v["pv"], v["gridImport"], v["gridExport"],
                                              v["batteryCharge"], v["batteryDischarge"]):
            base["home"] = round(max(0.0, v["pv"] + v["gridImport"] + v["batteryDischarge"]
                                          - v["gridExport"] - v["batteryCharge"]), 2)
        base["sources"] = dict(c["sources"])
        base["partial"] = bool(c["partial"])
        if None not in (v["pv"], v["gridImport"], v["gridExport"], v["batteryCharge"],
                        v["batteryDischarge"]) and base.get("home") is not None:
            base["balance"] = round(v["pv"] + v["gridImport"] + v["batteryDischarge"]
                                    - v["gridExport"] - v["batteryCharge"] - base["home"], 2)
        return base

    def _reconcile_daily_energy(self, data, today):
        """The tripwire: the identity must close and the derived house figure must
        agree with the inverter's own daily counter. Each check warns once per day
        and re-arms when it clears — the 4-Sep-2026 fault (a 10.5 kWh
        disagreement) sat unnoticed for two days because nothing looked.
        """
        de = getattr(self, "daily_energy", None)
        if de is None:
            return
        warned = str(self.store.get("energy_reconcile_warned") or "")
        flags  = set(w for w in warned.split(",") if w.startswith(today + ":"))
        v = de.today()["values"]
        checks = []
        residual = de.residual()
        if residual is not None:
            through = v["pv"] + v["gridImport"] + v["batteryDischarge"]
            checks.append((
                "balance",
                abs(residual) > max(ENERGY_BALANCE_ABS_KWH, ENERGY_BALANCE_FRACTION * through),
                f"energy balance off by {residual:+.2f} kWh so far today (pv {v['pv']:.2f} "
                f"+ import {v['gridImport']:.2f} + discharge {v['batteryDischarge']:.2f} "
                f"- export {v['gridExport']:.2f} - charge {v['batteryCharge']:.2f} - house "
                f"{v['home']:.2f}) — a midnight anchor is wrong; the figures come right "
                f"again at the next midnight"))
        device_home = (data or {}).get("homeDailyDirectKwh")
        derived     = self.store.get("home_daily_kwh")
        if device_home is not None and derived is not None and v["home"] is not None:
            gap = float(derived) - float(device_home)
            checks.append((
                "house",
                abs(gap) > max(ENERGY_BALANCE_ABS_KWH, ENERGY_HOUSE_FRACTION * float(device_home)),
                f"derived house use {float(derived):.2f} kWh disagrees with the inverter's "
                f"own daily counter {float(device_home):.2f} kWh by {gap:+.2f} kWh — the "
                f"house anchor is wrong"))
        for name, bad, message in checks:
            flag = f"{today}:{name}"
            if bad and flag not in flags:
                log(f"[Energy] {message}", level="WARNING")
                flags.add(flag)
            elif not bad and flag in flags:
                log(f"[Energy] {name} check back within bounds")
                flags.discard(flag)
        self.store["energy_reconcile_warned"] = ",".join(sorted(flags))

    def _track_energy_extremes(self, data):
        """Peak/low SOC and peak PV for the day — unchanged from v5.88, split out."""
        # --- SOC peak/low tracking ---
        soc = data.get("batterySoc", 0.0)
        if soc > self.store["peak_soc"]:
            self.store["peak_soc"] = soc
        if soc < self.store["min_soc"]:
            self.store["min_soc"] = soc

        # --- Peak PV tracking (max generation today + the time it hit) ---
        try:
            pv_w = int(data.get("pvPowerWatts", 0) or 0)
        except (TypeError, ValueError):
            pv_w = 0
        if pv_w > self.store.get("peak_pv_w", 0):
            self.store["peak_pv_w"]    = pv_w
            self.store["peak_pv_time"] = datetime.now().strftime("%H:%M")

    # ================================================================
    # Manager Evaluation
    # ================================================================

    def _evaluate_manager(self):
        # v5.45.0: the ENTIRE evaluate/verify/act path runs under the lock —
        # a register-verify racing an action callback (e.g. Pause) could
        # otherwise re-assert a stale mode. Control trades a few locked
        # seconds per minute for serialisation; the bulk I/O lives elsewhere.
        with self._state_lock:
            return self._evaluate_manager_impl()

    def _evaluate_manager_impl(self):
        """Run the battery manager decision engine and act on the result.

        Orchestrates:
          1. resolve power-cut lockout state (and toggle lockout-cleared event)
          2. compute VPP energy reserve
          3. build the immutable ManagerSnapshot
          4. apply seasonal + storm overrides
          5. evaluate the manager
          6. log on action change / heartbeat
          7. verify persistent inverter registers
          8. act on the decision
          9. push device state
        """
        if not self.latest_inverter_data:
            return

        # Staleness guard: during a Modbus outage the last-known-good snapshot
        # is deliberately kept for display, but the MANAGER must not act on a
        # frozen SOC for hours — at reconnect the first writes would be based
        # on arbitrarily old data. Hold evaluation once the snapshot exceeds
        # ~3 poll intervals + 60s.
        read_at = self.latest_inverter_data.get("_read_at", 0.0)
        if read_at:
            poll_s  = getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL)
            max_age = 3 * poll_s + 60
            age     = time.time() - read_at
            if age > max_age:
                dev = self._find_device("batteryManager")
                if dev and dev.states.get("managerStatus") != "Modbus offline — holding":
                    log(f"[Manager] Inverter data {age:.0f}s stale — holding "
                        f"evaluation until Modbus recovers", level="WARNING")
                    dev.updateStateOnServer("managerStatus",
                                            value="Modbus offline — holding")
                return

        # Manager paused (Pause action or sigen_manager_paused variable): stay
        # completely hands-off the inverter.  Skipping evaluate/verify/act here is
        # what makes pause a real control rather than a cosmetic label.  The
        # inverter was returned to self-consumption on the pause transition (see
        # _set_manager_paused); self-heal the device label so a restart-while-
        # paused shows "Paused".
        if self.store.get("manager_paused", False):
            dev = self._find_device("batteryManager")
            if dev and dev.states.get("managerStatus") != "Paused":
                dev.updateStateOnServer("managerStatus", value="Paused")
            return

        # 0. VPP OVER-RUN BACKSTOP (v5.62.0). An active window is ended by exactly
        #    ONE path — _poll_vpp -> _apply_vpp_event — and the manager below will
        #    re-drive ACTION_VPP_EXPORT every tick for as long as `vpp_active` is
        #    True, on nothing but that boolean. It never looks at the clock. So
        #    ANY failure that stops the poll reaching its end test leaves 4 kW
        #    pouring out of the battery against a 1% discharge floor with nothing
        #    to stop it: `axleEnabled` unticked mid-window, the token cleared so
        #    `self.axle` is None, a raise before the ACTIVE branch, or the VPP tick
        #    task dying while the manager lives. v5.61.1 fixed the specific cause
        #    that fired on 11-Aug-2026 (reading the NEXT event's end_time), but the
        #    single point of failure remained — so this is the second, independent
        #    guard: manager cadence, no network, no prefs, no Axle.
        #    It can only ever act in a state that is ALREADY wrong (past our own
        #    stored end), so it cannot cut a legitimate window short: the primary
        #    path stops at end+2min and this waits VPP_OVERRUN_GRACE beyond that.
        self._check_vpp_overrun()

        soc_pct = self.latest_inverter_data.get("batterySoc", 0.0)

        # 1. Power cut lockout (SOC- and forecast-aware — see _resolve_export_lockout).
        #    The solar-refill release needs the energy balance, which normally isn't
        #    built until step 3, so inside a lockout window we build a provisional
        #    snapshot first purely to obtain it. Both calls are pure (no side
        #    effects, no disk I/O) and safe to repeat, and a lockout window is a
        #    rare state — outside one, normal ticks pay nothing for this.
        lockout_balance = None
        if self._power_cut_window_active():
            try:
                provisional = self._build_manager_snapshot(
                    soc_pct, bool(self.pluginPrefs.get("exportEnabled", False)), 0.0,
                )
                lockout_balance = self.manager._calculate_24h_balance(provisional)
            except Exception as exc:
                # Never let the release calculation break the lockout itself —
                # balance=None simply falls back to the flat SOC floor.
                log(f"[PowerCut] Could not build balance for the solar-refill "
                    f"release ({exc!r}) — falling back to the SOC floor",
                    level="WARNING")
        export_enabled = self._resolve_export_lockout(soc_pct, lockout_balance)

        # 2. VPP reserve
        vpp_reserved_kwh = self._compute_vpp_reserved_kwh()

        # 3. Build snapshot
        self._update_pv_tracking()          # v5.90.0: the day's own evidence, first
        snapshot = self._build_manager_snapshot(
            soc_pct, export_enabled, vpp_reserved_kwh,
        )

        # 4. Seasonal + storm overrides (mutates snapshot)
        self._apply_seasonal_override(snapshot)
        self._apply_storm_override(snapshot, soc_pct)

        # 5. Evaluate
        decision = self.manager.evaluate(snapshot)
        # v5.90.0: today's need as the balance saw it, for the device state.
        try:
            self.store["need_today_kwh"] = self.manager._calculate_24h_balance(snapshot).need_24h_kwh
        except Exception as exc:
            self.logger.debug(f"[Manager] need_today_kwh not recorded: {exc}")
        self.latest_decision = decision
        self._record_solar_overflow_shadow(snapshot, decision)
        self._record_bank_first_metrics(snapshot, decision, soc_pct)

        # 6. Log if action changed or heartbeat
        self._log_manager_decision(decision, snapshot, soc_pct)
        self._note_import_hold(decision)

        # 7 + 8. Verify and act — UNLESS the Flux supervisor is holding the
        # inverter. This is the whole of the "no overlay that fights the old
        # manager" rule, and it is one branch on purpose.
        #
        # Both of the passes below would fight a live Flux claim, in different
        # ways and within a minute of each other:
        #   * _verify_ems_registers reads back the mode and the two power limits,
        #     compares them against what the STORE flags imply, and "corrects"
        #     anything else. A Flux charge (0x03 with a sized limit) looks exactly
        #     like drift to it, because no store flag says otherwise;
        #   * _act_on_decision's anti-oscillation hold would keep re-driving its
        #     own idea of the import, and its SELF_CONSUMPTION branch would issue
        #     a hand-back nobody asked for.
        # So while Flux owns the registers the manager still EVALUATES — the
        # decision, the device state, the shadow metrics and the logs all carry on
        # — and simply does not touch the hardware. The moment Flux lets go, the
        # next evaluation acts as it always did, including the stuck-mode recovery
        # that puts a strange mode back to self consumption.
        if self._flux_owns_control():
            # Somebody may have taken priority since the supervisor last looked —
            # a VPP window opening, a storm landing, an import starting. Release
            # HERE, inside the same locked evaluation, rather than leaving it to
            # the next supervisor tick: a paid export window must not wait a tick
            # plus a manager cycle (up to 70 seconds of it) for Flux to notice.
            owner = self._flux_other_owner()
            if owner:
                self._flux_release(f"pre-empted by {owner}", preempted=True)
            if self._flux_owns_control():
                self.store["manager_hands_off_reason"] = (
                    "the Flux supervisor holds the inverter")
                self._update_manager_device(decision, snapshot)
                self._publish_flood_preview(snapshot, decision)
                return
        self.store["manager_hands_off_reason"] = ""
        self._verify_ems_registers()
        # 8a. NO TARIFF YET. Straight after a restart the first manager tick runs
        #    before the first Octopus refresh, and _build_tariff_data then falls
        #    back to Tracker — so on Flux (or Go, or Agile) the first plan of every
        #    restart was a flat-rate plan (seen live 17-Sep-2026: "tariff=tracker"
        #    at 11:23, 11:25 and 12:50, each a restart). Everything above still runs (Flux pre-emption,
        #    the device state); only the hardware ACTION waits for the tariff, up to
        #    TARIFF_WAIT_S; after that, carry on as before rather than never
        #    managing the battery because Octopus is down.
        if not (self.latest_rates_data or {}).get("tariff_info"):
            waited_since = self.store.get("tariff_wait_since")
            if waited_since is None:
                self.store["tariff_wait_since"] = waited_since = time.time()
                log("[Manager] Waiting for the first Octopus tariff refresh before "
                    "acting, so the first plan after a restart uses the real tariff.")
            if time.time() - waited_since < TARIFF_WAIT_S:
                self._update_manager_device(decision, snapshot)
                return
        else:
            self.store["tariff_wait_since"] = None

        self._act_on_decision(decision)

        # 9. Push device state
        self._update_manager_device(decision, snapshot)

        # 10. Publish the flood-export gate preview for the openmeteo advisory so it
        #     reports the SAME gate the manager acts on (single source of truth — stops
        #     the advisory re-deriving and drifting; see the 23/24-Jun-2026 case).
        self._publish_flood_preview(snapshot, decision)

    def _note_import_hold(self, decision):
        """Say, once a day, that tomorrow needed a grid import and none could be priced.

        v5.100.0. The planner now HOLDS on self-consumption when it has no Agile
        rates to plan from, when the published slots do not cover a charge, or when
        the tariff is unrecognised — instead of importing 10 kW blind. A hold that
        looks exactly like "24h sufficient" is the one way that change could hide a
        broken Octopus connection until the bill, so: a WARNING in the event log
        (Log_Error_Watch collects it) and one Pushover a day at normal priority.
        """
        if not getattr(decision, "import_held", False):
            return
        today = _london_today().isoformat()
        if self.store.get("import_hold_noted_date") == today:
            return
        self.store["import_hold_noted_date"] = today
        log(f"[Manager] Grid import HELD — {decision.reason}", level="WARNING")
        kwh = float(getattr(decision, "import_kwh", 0.0) or 0.0)
        why = str(getattr(decision, "import_held_why", "") or "no price could be found")
        if str(getattr(decision, "import_purpose", "") or "") == "reserve":
            # v5.101.0: the power-cut reserve on Agile is bought through the same
            # planner, so it can be held for the same reasons — but it is a
            # different thing to tell someone about.
            title = "Battery reserve top-up held"
            body  = (
                f"The battery is on course to be below its power-cut reserve by dawn, "
                f"about {kwh:.0f} kWh short, but the plugin could not price a grid "
                f"top-up: {why}. Until it can, the battery keeps what it has and the "
                f"house draws from the grid as it needs to. Check the Octopus "
                f"connection and the plugin log."
            )
        else:
            title = "Battery import held tonight"
            body  = (
                f"Tomorrow needs about {kwh:.0f} kWh more than the battery and the solar "
                f"forecast can supply, but the plugin could not price a grid import: "
                f"{why}. Until it can, the house draws from the grid as it needs to, "
                f"which costs the daytime rate instead of a cheaper overnight one. "
                f"Check the Octopus connection and the plugin log."
            )
        self._send_pushover(title, body)

    def _record_bank_first_metrics(self, snapshot, decision, soc_pct):
        """Measure what the bank-first hold did today. Never touches control.

        Three jobs, all of them measurement:
          1. Classify the day SMALL from the latest COMPLETE forecast FOR TODAY, and
             keep that classification in step with the forecast for as long as one
             keeps arriving — in EITHER direction. A partial or failed fetch reads
             low, and reading low is exactly when you want to keep the kWh, so a
             degraded reading changes nothing; but a good one always wins, however
             many came before it. (05-Sep-2026: this used to arm SMALL once and never
             clear — a day whose first post-midnight fetch read 31.0 kWh stayed
             "small" all day even after later fetches revised it to 46.0 and
             48.4 kWh, and cost 101 minutes of clipped solar on the 46.0 kWh day.)
          2. Count the hold: samples, minutes, and the export actually withheld,
             obtained by re-running the gate against a copy of the snapshot with the
             feature switched off. The 0.0-means-off contract is what makes that
             counterfactual a one-scalar change rather than a second code path.
          3. Count the MEASURED cost. clip_boundary_minutes is the number that decides
             whether this feature is free: minutes with export pinned at the DNO cap
             and the battery no longer taking anything, which is the state in which PV
             is being thrown away. It reads the meter, not the forecast, and it fires
             whether the cause is a full battery or a BMS taper.

        Wrapped whole: analysis must be invisible to the control path.
        """
        try:
            store = self.store
            # Establish every counter up front. __init__ seeds them, but an
            # accumulators.json written by a version that predates this feature
            # restores a partial store, and a counter that only materialises when its
            # event fires reads as "never happened" and as "not measured" in exactly
            # the same way. They must be distinguishable.
            for _key, _default in (
                ("bank_first_blocked_samples",   0),
                ("bank_first_withheld_kwh",      0.0),
                ("bank_first_first_block_local", ""),
                ("bank_first_released_local",    ""),
                ("bank_first_minutes_soc_ge_95", 0),
                ("bank_first_minutes_soc_ge_99", 0),
                ("bank_first_clip_boundary_min", 0),
                ("bank_first_arm_minutes",       0),
                ("bank_first_first_arm_local",   ""),
                ("bank_first_peak_surplus_kw",   0.0),
                ("bank_first_small_latched",     False),
                ("bank_first_first_class_kwh",   None),
                ("bank_first_first_class_small", None),
                ("bank_first_first_class_local", ""),
                ("bank_first_promoted_local",    ""),
                ("bank_first_latched_small_kwh",   None),
                ("bank_first_latched_small_local", ""),
            ):
                store.setdefault(_key, _default)

            local_now = _to_london(snapshot.now)
            today_str = local_now.strftime("%Y-%m-%d")

            # ── 1. the day classification ───────────────────────────────────
            if store.get("bank_first_latch_date") != today_str:
                store["bank_first_latch_date"]    = today_str
                store["bank_first_small_latched"] = False
                # The day's own first verdict, cleared with the latch it describes.
                # Leaving these behind would file yesterday's classification against
                # today, which is the shape of bug this block already carries a
                # paragraph about.
                store["bank_first_first_class_kwh"]   = None
                store["bank_first_first_class_small"] = None
                store["bank_first_first_class_local"] = ""
                store["bank_first_promoted_local"]    = ""
                store["bank_first_latched_small_kwh"]   = None
                store["bank_first_latched_small_local"] = ""
            max_kwh = min(float(snapshot.solar_overflow_bank_first_max_kwh or 0.0),
                          SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX)
            status  = str(self.latest_forecast_data.get("forecastStatus", ""))
            raw_kwh = float(snapshot.raw_today_kwh or 0.0)
            # The forecast must be FOR today. Between local midnight and the first
            # fetch of the new day, latest_forecast_data still holds YESTERDAY's
            # totals while today_str has already rolled — checked via fc_date below.
            fc_date = str(self.latest_forecast_data.get("forecastDate", ""))
            # Re-derive the classification from the freshest complete, correctly-dated
            # fetch every time one lands — never freeze on the first. This USED to be
            # a one-way arm-to-SMALL-only latch (comment above this block, before
            # 05-Sep-2026, called that "the safe direction"), and it was not: the
            # overnight fetch for 04-Sep read 31.0 kWh, armed the day SMALL, and never
            # cleared when the forecast was later revised to 46.0 kWh — a genuinely
            # big day held for its whole length, 101 minutes of it spent clipping
            # solar at the DNO cap with the battery already full. A partial, failed,
            # or wrong-day fetch leaves the stored value exactly where it was — that
            # is what stops one bad reading deciding the afternoon — but any COMPLETE
            # fetch for today, in either direction, updates it.
            #
            # v5.106.0 — AND THE SYMMETRY INTRODUCED THE MIRROR FAULT. Promotion
            # now needs a MARGIN; demotion still does not.
            #
            # 14-Sep-2026: the day armed SMALL at 08:00 on a raw 37.4 kWh, the
            # 08:50 fetch read 40.7, that cleared the 40.0 threshold by 0.7, the
            # hold released at 08:52 after 47 minutes and 0.001 kWh withheld, and
            # 14.9 kWh went to the grid between 09:30 and 15:00 while the battery
            # was capped at ~1.1 kW. The day delivered 36.43 kWh — below even the
            # pre-jump figure — and peaked at 90.3% against a 95% target.
            #
            # A threshold with no margin is being asked to resolve a difference
            # smaller than the signal's own noise. MEASURED from six days of
            # plugin logs (10-15 Sep 2026, 06:00-15:00): the raw forecast wanders
            # a median 5.7 kWh across the morning, max 9.0, and its largest single
            # upward step is a median 3.0 kWh, max 7.4. So 0.7 kWh is noise, and
            # ONLY a margin can tell it from a revision. Same lesson as the one
            # that moved this gate off a 1.0 kWh margin in the first place.
            #
            # One-sided, because the errors are: holding export on a day that
            # turns out big costs a little late export, and on this estate
            # clip_boundary_minutes has been 0 on every held day. Releasing it on
            # a day that turns out small costs 13-18p on every kWh sold and bought
            # back. Demotion therefore stays immediate — the cheap direction needs
            # no evidence — and 04-Sep's genuine 31.0 -> 46.0 revision still
            # promotes, because 46.0 clears 40.0 + 5.0.
            if (max_kwh > 0.0 and status.upper().startswith("OK")
                    and fc_date == today_str and raw_kwh > 0.0):
                was_small = bool(store.get("bank_first_small_latched", False))
                promote_at = max_kwh + BANK_FIRST_PROMOTE_MARGIN_KWH
                if was_small:
                    # Already holding: only a forecast clear of the noise releases it.
                    still_small = raw_kwh < promote_at
                else:
                    still_small = raw_kwh < max_kwh
                if was_small and not still_small:
                    store["bank_first_promoted_local"] = local_now.strftime("%H:%M")
                    log(f"[BankFirst] Day reclassified as big — forecast {raw_kwh:.1f} kWh "
                        f"clears the {max_kwh:.0f} kWh threshold by more than the "
                        f"{BANK_FIRST_PROMOTE_MARGIN_KWH:.0f} kWh the forecast normally "
                        f"wanders in a morning, so daytime export is released")
                # v5.106.0: keep the FIRST classification of the day and the forecast
                # it was made from. The daily record used to publish only the latch as
                # it stood at 23:59 plus latest_forecast_data's todayKwh at that same
                # moment, under the names `classified_small` and `classified_from_kwh`
                # — so a day classified small at 08:00 on 37.4 kWh and promoted at
                # 08:52 was filed as "classified_small: false, classified_from_kwh:
                # 41.3", which is neither the verdict that governed the morning nor
                # the number it was reached from. 14-Sep-2026 read exactly that way.
                # v5.111.3: record the forecast the latch now in force was set
                # from. The opening log line used to print the LIVE forecast beside
                # the threshold, and on 20-09-2026 that read "today's forecast 40.5
                # kWh is below the 40.0 kWh cap-saturation threshold" — a sentence a
                # reader can see is false. The day had been classified small at 00:05
                # on 39.9 kWh and the forecast had drifted up by the time the hold
                # engaged at 08:00. Captured on the TRANSITION, where `still_small`
                # was reached through `raw_kwh < max_kwh`, so "below the threshold" is
                # true of this figure by construction rather than by luck.
                if still_small and not was_small:
                    store["bank_first_latched_small_kwh"]   = round(raw_kwh, 2)
                    store["bank_first_latched_small_local"] = local_now.strftime("%H:%M")
                if store.get("bank_first_first_class_kwh") is None:
                    store["bank_first_first_class_kwh"]   = round(raw_kwh, 2)
                    store["bank_first_first_class_small"] = still_small
                    store["bank_first_first_class_local"] = local_now.strftime("%H:%M")
                store["bank_first_small_latched"] = still_small

            # ── 2. the hold ─────────────────────────────────────────────────
            if getattr(decision, "bank_first_holding", False):
                store["bank_first_blocked_samples"] = int(
                    store.get("bank_first_blocked_samples", 0)) + 1
                if not store.get("bank_first_first_block_local"):
                    store["bank_first_first_block_local"] = local_now.strftime("%H:%M")
                # What the legacy gate would have exported this minute. Same balance,
                # same physics, one scalar different — so any divergence is the gate
                # under test and nothing else.
                legacy = copy.copy(snapshot)
                legacy.solar_overflow_bank_first_max_kwh = 0.0
                legacy.bank_first_small_latched          = False
                shadow = self.manager._check_solar_overflow(
                    legacy, self.manager._calculate_24h_balance(legacy))
                if shadow is not None:
                    store["bank_first_withheld_kwh"] = float(
                        store.get("bank_first_withheld_kwh", 0.0)
                    ) + float(shadow.export_kw) * MANAGER_EVAL_INTERVAL / 3600.0
            # v5.111.1: stamp a RELEASE only when the gate was actually consulted and
            # let the day through. "Not holding" used to be enough, and it covers the
            # case where an earlier gate refused and bank-first never ran — which on
            # 19-09-2026 stamped released_local as 08:31 at SOC 36% against a 95%
            # gate, on a day that reached 79% and exported nothing before 16:00. The
            # four-week review reads this field.
            elif (int(store.get("bank_first_blocked_samples", 0)) > 0
                  and not store.get("bank_first_released_local")
                  and getattr(decision, "bank_first_state", BANK_FIRST_NOT_ASKED)
                      == BANK_FIRST_RELEASED):
                store["bank_first_released_local"] = local_now.strftime("%H:%M")

            # ── 3. the measured cost ────────────────────────────────────────
            inv        = self.latest_inverter_data or {}
            grid_w     = float(inv.get("gridPowerWatts", 0.0) or 0.0)
            batt_w     = float(inv.get("batteryPowerWatts", 0.0) or 0.0)
            max_exp_w  = float(snapshot.max_export_kw or 0.0) * 1000.0
            surplus_w  = max(0.0, float(snapshot.pv_watts) - float(snapshot.house_load_watts))

            if soc_pct >= 95.0:
                store["bank_first_minutes_soc_ge_95"] = int(
                    store.get("bank_first_minutes_soc_ge_95", 0)) + 1
            if soc_pct >= 99.0:
                store["bank_first_minutes_soc_ge_99"] = int(
                    store.get("bank_first_minutes_soc_ge_99", 0)) + 1

            # Export pinned at the cap AND the battery no longer absorbing AND the
            # battery high enough that it is the battery, not the house, that stopped
            # taking it. All three, or a windy evening reads as clipping.
            if (max_exp_w > 0.0 and -grid_w >= max_exp_w - 100.0
                    and batt_w < 200.0 and soc_pct >= 97.0):
                store["bank_first_clip_boundary_min"] = int(
                    store.get("bank_first_clip_boundary_min", 0)) + 1

            # Arming: minutes in which the MEASURED surplus could actually fill the
            # export cable. Recorded now, consulted by a later stage — a day that has
            # never armed has never demonstrated that exporting early protects
            # anything.
            if max_exp_w > 0.0 and surplus_w >= max_exp_w:
                store["bank_first_arm_minutes"] = int(
                    store.get("bank_first_arm_minutes", 0)) + 1
                if not store.get("bank_first_first_arm_local"):
                    store["bank_first_first_arm_local"] = local_now.strftime("%H:%M")

            if surplus_w / 1000.0 > float(store.get("bank_first_peak_surplus_kw", 0.0)):
                store["bank_first_peak_surplus_kw"] = round(surplus_w / 1000.0, 2)

        except Exception as exc:
            self.logger.debug(f"[BankFirst] metrics sample skipped: {exc!r}")

    def _shadow_target_pct(self):
        """The SOC target the pacing shadow compares the LIVE target against.

        v5.106.0. Defaults to SOLAR_OVERFLOW_SHADOW_TARGET_SOC_PCT and is only
        meaningful when it differs from the live target — see
        _record_solar_overflow_shadow for why that is now checked rather than
        assumed.
        """
        return _as_float(self.pluginPrefs.get("solarOverflowShadowTargetSoc"),
                         SOLAR_OVERFLOW_SHADOW_TARGET_SOC_PCT)

    def _record_solar_overflow_shadow(self, snapshot, live_decision):
        """Accumulate an alternative-target pacing estimate without changing control.

        The physics, sufficiency and DNO gates are deliberately identical to the
        live decision.  Only the charge pacing target changes.  The delta is an
        estimate of export foregone / charge retained during this one-minute
        evaluation interval; it is not an assertion about end-of-day SOC or PV
        curtailment, both of which depend on later weather and inverter behaviour.

        v5.106.0 — THIS RAN ZERO TIMES BETWEEN 31-Aug AND 15-Sep-2026. It was
        written as a fixed 90-versus-95 experiment and guarded with
        `abs(live_target - 90.0) > 0.01: return`, which was the right instinct
        (never label a different comparison 90/95) with no handling of the
        obvious consequence: the experiment it guarded was the argument for
        moving the live target to 95, so the moment that argument WON, the guard
        fired on every tick for ever. `samples` sat at 0 on every daily record
        from 1-Sep onward and nothing said why, while the block went on
        reporting the hardcoded literals `live_target_pct: 90.0` /
        `shadow_target_pct: 95.0` — a comparison that was not being made,
        described with numbers that were no longer true.

        So the pair is now read, not assumed, and the skip reason is recorded.
        A shadow equal to the live target is not an experiment and is reported
        as such rather than silently producing zeroes.
        """
        self.store["shadow_skip_reason"] = ""
        if not bool(self.pluginPrefs.get("solarOverflowShadowEnabled", True)):
            self.store["shadow_skip_reason"] = "disabled in Configure"
            return
        if snapshot.storm_active or live_decision.action != ACTION_SOLAR_OVERFLOW:
            return          # not a skip worth reporting — most ticks are not overflow
        live_pct   = float(snapshot.solar_overflow_target_pct)
        shadow_pct = float(self._shadow_target_pct())
        if abs(live_pct - shadow_pct) <= 0.01:
            # Not a fault, but it must not read as one. Without this the block is
            # indistinguishable from the bug above: zero samples, no explanation.
            self.store["shadow_skip_reason"] = (
                f"shadow target {shadow_pct:.0f}% equals the live target — "
                "nothing to compare")
            return
        try:
            balance = self.manager._calculate_24h_balance(snapshot)
            shadow_snapshot = copy.copy(snapshot)
            shadow_snapshot.solar_overflow_target_pct = shadow_pct
            shadow = self.manager._check_solar_overflow(shadow_snapshot, balance)
            if shadow is None:
                self.store["shadow_skip_reason"] = "shadow pacing returned no decision"
                return
            # Sign convention, stated because it inverted when the live target
            # moved: the LOWER SOC target is the one that exports more, so the
            # foregone export is always (looser export - stricter export). Taking
            # `live - shadow` was only correct while live was the looser of the
            # two, and would have clamped silently to 0.0 for ever once it wasn't.
            live_kw   = float(live_decision.export_kw)
            shadow_kw = float(shadow.export_kw)
            looser_kw, stricter_kw = ((live_kw, shadow_kw) if live_pct < shadow_pct
                                      else (shadow_kw, live_kw))
            export_delta_kw = max(0.0, looser_kw - stricter_kw)
            self.store["shadow_95_export_foregone_kwh"] += (
                export_delta_kw * MANAGER_EVAL_INTERVAL / 3600.0
            )
            self.store["shadow_95_samples"] += 1
        except Exception as exc:
            # Analysis must be invisible to the control path if an unexpected
            # forecast/input shape occurs.
            self.store["shadow_skip_reason"] = f"sample raised {exc!r}"
            self.logger.debug(f"[Shadow] pacing sample skipped: {exc!r}")

    def _publish_flood_preview(self, snapshot, decision):
        """Write sigen_flood_preview.json so openmeteo_battery_optimiser.py can report
        the flood-export gate verbatim instead of re-deriving it.  Best-effort and
        side-effect-free with respect to control — any failure is logged and swallowed
        so it can never disrupt the manager tick.  Published every tick (incl. daytime)
        because the preview is forward-looking: would_fire answers "would the gate drain
        tonight given the current forecast and SOC", which is exactly what the advisory's
        20:00 (still-daylight) run needs."""
        try:
            preview = self.manager.compute_flood_preview(snapshot)
        except Exception as exc:
            self.logger.debug(f"[FloodPreview] compute failed: {exc!r}")
            return
        try:
            today_export_kwh = float(
                self.latest_inverter_data.get("gridDailyExportKwh", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            today_export_kwh = 0.0
        preview["today_export_kwh"] = round(today_export_kwh, 2)
        preview["export_active"]    = bool(getattr(snapshot, "export_active", False))
        preview["decision_action"]  = decision.action
        preview["generated_at"]     = datetime.now(timezone.utc).isoformat()
        preview["plugin_version"]   = self.pluginVersion
        path = ("/Library/Application Support/Perceptive Automation/"
                "Python Scripts/sigen_flood_preview.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(preview, f, indent=2)
            os.replace(tmp, path)   # atomic — readers never see a half-written file
        except OSError as exc:
            self.logger.warning(f"[FloodPreview] Cannot write {path}: {exc}")

    def _power_cut_lockout_soc_floor(self):
        """SOC floor (%) above which export resumes during the post-cut lockout.
        Reads the powerCutLockoutSocFloor pref, guarded, defaulting to the
        POWER_CUT_LOCKOUT_SOC_FLOOR constant."""
        return _as_float(self.pluginPrefs.get("powerCutLockoutSocFloor"),
                         POWER_CUT_LOCKOUT_SOC_FLOOR)

    def _power_cut_lockout_min_soc(self):
        """SOC floor (%) below which the solar-refill release never applies.
        Reads the powerCutLockoutMinSocPct pref, guarded, defaulting to the
        POWER_CUT_LOCKOUT_MIN_SOC_PCT constant."""
        return _as_float(self.pluginPrefs.get("powerCutLockoutMinSocPct"),
                         POWER_CUT_LOCKOUT_MIN_SOC_PCT)

    def _storm_export_release_pct(self):
        """SOC (%) at/above which the storm override stops suppressing export.
        Reads the stormExportReleasePct pref, guarded, defaulting to the
        STORM_EXPORT_RELEASE_PCT constant. Never allowed below the active storm
        reserve target — the caller clamps with max(release, override_soc)."""
        return _as_float(self.pluginPrefs.get("stormExportReleasePct"),
                         STORM_EXPORT_RELEASE_PCT)

    def _power_cut_window_active(self):
        """True while inside the POWER_CUT_LOCKOUT_HOURS window after a restore.

        The window is purely time-based (decoupled from the export pref) so the
        cleared-event fires on real expiry, not immediately when export happens to
        be disabled.

        Defensive parsing: a hand-edited or corrupt pluginPrefs value (naive ISO,
        garbage string, or wrong type) must NEVER fail-open and let export resume
        during the lockout window. On parse failure we clear the bad value (so it
        doesn't block forever) and resume normal operation.

        Extracted from _resolve_export_lockout in v5.50.0 so _evaluate_manager_impl
        can cheaply ask "are we in a lockout?" before deciding whether the extra
        energy-balance calculation for the solar-refill release is worth doing.
        """
        prt_str = self.pluginPrefs.get("powerRestoredTime", "")
        if not prt_str:
            return False
        try:
            power_restored = datetime.fromisoformat(prt_str)
            if power_restored.tzinfo is None:
                power_restored = power_restored.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - power_restored).total_seconds() / 3600.0
            return hours_since < POWER_CUT_LOCKOUT_HOURS
        except (ValueError, TypeError, AttributeError) as exc:
            log(
                f"[PowerCut] Bad powerRestoredTime in prefs ({exc!r}) — "
                f"clearing and resuming normal operation",
                level="WARNING",
            )
            self.pluginPrefs["powerRestoredTime"] = ""
            return False

    def _resolve_export_lockout(self, soc_pct=None, balance=None):
        """Apply the post-power-cut export lockout (returns export_enabled bool).

        For POWER_CUT_LOCKOUT_HOURS after the grid is restored, export is held
        off as a precaution — BUT only while SOC is below
        POWER_CUT_LOCKOUT_SOC_FLOOR. At/above the floor export resumes so
        flood-prevention can shed surplus and we don't clip solar by letting a
        near-full battery hit 100%. The `power_cut_lockout_active` store flag
        tracks the time WINDOW (so the cleared-event fires once on expiry, not
        when SOC merely crosses the floor); `power_cut_export_suppressed` tracks
        whether export is actually held off right now.

        v5.50.0 adds a SECOND, strictly-additional release condition: pass the
        current SufficiencyBalance as `balance` and export also resumes once
        today's own remaining solar comfortably refills the battery to the floor
        by itself (see _solar_refill_releases_lockout). Omitting `balance` — as
        every pre-5.50 caller did — reproduces the old behaviour exactly.

        Side effects: updates the store flags and fires the
        `powerCutLockoutCleared` trigger event when the window expires.
        """
        export_pref   = bool(self.pluginPrefs.get("exportEnabled", False))
        within_window = self._power_cut_window_active()

        # SOC floor: within the window, suppress export only while SOC is low AND
        # export is actually enabled (nothing to suppress otherwise).
        soc_val = None
        try:
            if soc_pct is not None:
                soc_val = float(soc_pct)
        except (TypeError, ValueError):
            soc_val = None
        soc_floor  = self._power_cut_lockout_soc_floor()
        locked_out = _export_locked_out(within_window, soc_val, soc_floor)

        # Solar-refill release. Only consulted while the SOC floor would otherwise
        # hold export off, and only when the caller supplied a balance — so it can
        # release EARLIER than the floor but never hold longer.
        solar_release = False
        if locked_out and balance is not None:
            cap_kwh = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), 35.04)
            solar_release = _solar_refill_releases_lockout(
                is_daytime          = bool(getattr(balance, "is_daytime", False)),
                soc_pct             = soc_val,
                min_soc_pct         = self._power_cut_lockout_min_soc(),
                battery_kwh         = float(getattr(balance, "battery_kwh", 0.0)),
                floor_kwh           = soc_floor / 100.0 * cap_kwh,
                remaining_solar_kwh = float(getattr(balance, "remaining_solar_kwh", 0.0)),
                home_to_dusk_kwh    = float(getattr(balance, "remaining_home_to_dusk_kwh", 0.0)),
            )

        suppressed     = export_pref and locked_out and not solar_release
        export_enabled = export_pref and not suppressed
        self.store["power_cut_solar_release"] = bool(within_window and solar_release)

        # One-shot INFO when export first resumes mid-window, naming WHICH rule
        # released it — this line is what explains the behaviour in the event log
        # months later. soc_val is formatted defensively: export_pref=False makes
        # suppressed False regardless of SOC, so this can be reached with an
        # unknown SOC.
        prev_suppressed = bool(self.store.get("power_cut_export_suppressed", False))
        if within_window and prev_suppressed and not suppressed:
            soc_str = f"{soc_val:.0f}%" if soc_val is not None else "unknown"
            if solar_release:
                surplus = (float(getattr(balance, "remaining_solar_kwh", 0.0))
                           - float(getattr(balance, "remaining_home_to_dusk_kwh", 0.0)))
                log(
                    f"[PowerCut] SOC {soc_str} — today's solar refills the "
                    f"{soc_floor:.0f}% reserve on its own ({surplus:.1f} kWh surplus "
                    f"to dusk), so export re-enabled during lockout to protect solar",
                )
            else:
                log(
                    f"[PowerCut] SOC {soc_str} ≥ {soc_floor:.0f}% "
                    f"floor — export re-enabled during lockout to protect solar",
                )
        self.store["power_cut_export_suppressed"] = suppressed

        # Detect lockout-window-cleared transition for the Indigo trigger event.
        # Tied to the WINDOW (not the SOC override) so it fires once on expiry.
        prev_lockout = bool(self.store.get("power_cut_lockout_active", False))
        if prev_lockout and not within_window:
            log("[PowerCut] Export lockout cleared — normal operation resumed")
            self.store["power_cut_lockout_active"] = False
            self._trigger_event("powerCutLockoutCleared")
        else:
            self.store["power_cut_lockout_active"] = within_window

        return export_enabled

    def _compute_vpp_reserved_kwh(self):
        """Pre-compute kWh that must be reserved for an upcoming VPP event."""
        vpp_state = self.store.get("vpp_state", VPP_IDLE)
        vpp_event = self.store.get("vpp_event") or {}
        if vpp_state in (VPP_ANNOUNCED, VPP_PRE_CHARGING) and vpp_event:
            max_export_kw = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)
            duration_hrs  = vpp_event.get("duration_hrs", 1.0)
            return max_export_kw * duration_hrs / VPP_DISCHARGE_EFFICIENCY
        return 0.0

    def _compute_vpp_export_by_date(self):
        """Return (today_kwh, tomorrow_kwh) — Axle export expected on each local date.

        Used by flood prevention to subtract VPP export from refill-day capacity.
        Future-only: an ACTIVE event's elapsed portion is not counted (SOC already
        reflects it). Pro-rated for events that span local midnight.

        Counts only ANNOUNCED / PRE_CHARGING / ACTIVE states — COOLING_OFF means
        the export is already complete and reflected in SOC.
        """
        vpp_state = self.store.get("vpp_state", VPP_IDLE)
        vpp_event = self.store.get("vpp_event") or {}
        if not vpp_event or vpp_state not in (VPP_ANNOUNCED, VPP_PRE_CHARGING, VPP_ACTIVE):
            return (0.0, 0.0)

        start = vpp_event.get("start_time")
        end   = vpp_event.get("end_time")
        if start is None or end is None:
            return (0.0, 0.0)

        max_export_kw = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)

        now_local   = _london_now()
        start_local = _to_london(start)   # naive values are returned unchanged
        end_local   = _to_london(end)

        # Future-only: clip to "from now"
        effective_start = max(start_local, now_local)
        if effective_start >= end_local:
            return (0.0, 0.0)

        today_date    = now_local.date()
        tomorrow_date = today_date + timedelta(days=1)

        today_hours    = self._vpp_overlap_hours(effective_start, end_local, today_date)
        tomorrow_hours = self._vpp_overlap_hours(effective_start, end_local, tomorrow_date)

        return (
            round(today_hours    * max_export_kw, 3),
            round(tomorrow_hours * max_export_kw, 3),
        )

    @staticmethod
    def _vpp_overlap_hours(start_dt, end_dt, date_obj):
        """Return overlap hours between [start_dt, end_dt] and local date date_obj.

        start_dt and end_dt are local-time datetimes (tz-aware if pytz available).
        Returns 0.0 if no overlap, otherwise the duration of the overlap in hours.
        """
        day_start = start_dt.replace(
            year=date_obj.year, month=date_obj.month, day=date_obj.day,
            hour=0, minute=0, second=0, microsecond=0,
        )
        day_end = day_start + timedelta(days=1)
        overlap_start = max(start_dt, day_start)
        overlap_end   = min(end_dt,   day_end)
        if overlap_end <= overlap_start:
            return 0.0
        return (overlap_end - overlap_start).total_seconds() / 3600.0

    # ------------------------------------------------------------------
    # v5.90.0 — intraday PV tracking + measured need (Stage 3 of the revamp,
    # docs/daily-energy-revamp.md). The manager evaluated every 60 s already;
    # what was missing was anything MEASURED about today in its inputs.
    # ------------------------------------------------------------------

    def _pv_unclipped(self, data):
        """True when the inverter is free to take all the PV — the only time a
        shortfall against the forecast says anything about the WEATHER.

        At the export cap, or on a full battery that is no longer charging, the
        inverter is turning PV away and the measured figure under-reads potential.
        Learning from those minutes would talk the plugin out of exporting on the
        very days it most needs to (a low ratio -> less remaining solar -> the
        physics gate releases -> the battery charges -> it clips again).
        """
        data = data or {}
        try:
            export_w = max(0.0, -float(data.get("gridPowerWatts", 0) or 0))
            soc      = float(data.get("batterySoc", 0.0) or 0.0)
            batt_w   = float(data.get("batteryPowerWatts", 0) or 0)
        except (TypeError, ValueError):
            return True
        max_export_w = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0) * 1000.0
        if max_export_w > 0 and export_w >= PV_TRACKING_EXPORT_CAP_FRACTION * max_export_w:
            return False
        if soc >= 99.0 and batt_w <= 100.0:
            return False
        return True

    def _forecast_kwh_between(self, t0, t1):
        """Bias-corrected forecast energy for the interval [t0, t1] (epoch seconds),
        integrated bucket by bucket from today's hourly p50 in local time.

        Robust to a forecast refresh mid-day: each minute takes whatever bucket
        is current for it, rather than differencing two cumulative sums that a
        refresh would move under us.
        """
        fc     = self.latest_forecast_data or {}
        hourly = fc.get("_hourly_p50_today") or {}
        if not hourly or t1 <= t0:
            return 0.0
        try:
            factor = float(fc.get("biasFactorToday") or fc.get("biasFactor", 1.0) or 1.0)
        except (TypeError, ValueError):
            factor = 1.0
        a = _to_london(datetime.fromtimestamp(float(t0), tz=timezone.utc)).replace(tzinfo=None)
        b = _to_london(datetime.fromtimestamp(float(t1), tz=timezone.utc)).replace(tzinfo=None)
        total_wh = 0.0
        for key, wh in hourly.items():
            try:
                start = datetime.strptime(str(key), "%Y-%m-%d %H:%M:%S")
                wh    = float(wh)
            except (TypeError, ValueError):
                continue
            end     = start + timedelta(hours=1)
            overlap = (min(b, end) - max(a, start)).total_seconds()
            if overlap > 0:
                total_wh += wh * overlap / 3600.0
        return total_wh / 1000.0 * factor

    def _update_pv_tracking(self):
        """Advance the two accumulators the tracking factor is made from — measured
        PV and the forecast for the SAME minutes — counting only minutes when the
        inverter could take all the PV. Called once per manager evaluate (60 s).
        Resets at local midnight; persisted so a restart keeps the morning.
        """
        today = _local_today_str()
        if self.store.get("pv_track_date") != today:
            for key, value in (("pv_track_actual_kwh", 0.0), ("pv_track_forecast_kwh", 0.0),
                               ("pv_track_clipped_min", 0.0), ("pv_track_last_epoch", 0.0),
                               ("pv_track_last_pv_kwh", None), ("pv_track_factor", 1.0),
                               ("pv_track_ratio", None), ("pv_track_last_hour", None)):
                self.store[key] = value
            self.store["pv_track_date"] = today
        now     = time.time()
        pv_now  = self.store.get("pv_daily_kwh")
        partial = bool(self.store.get("energy_day_partial", False))
        de      = getattr(self, "daily_energy", None)
        if de is not None:
            try:
                partial = partial or de.today()["sources"].get("pv") in ("late", "absent")
            except Exception:
                pass
        last_t  = float(self.store.get("pv_track_last_epoch") or 0.0)
        last_pv = self.store.get("pv_track_last_pv_kwh")
        if pv_now is not None and last_t > 0.0 and now > last_t and last_pv is not None:
            d_pv = float(pv_now) - float(last_pv)
            if d_pv < 0.0:                       # projection re-anchored: count from here
                d_pv = 0.0
            d_fc = self._forecast_kwh_between(last_t, now)
            if self._pv_unclipped(self.latest_inverter_data):
                self.store["pv_track_actual_kwh"]   = (
                    float(self.store.get("pv_track_actual_kwh") or 0.0) + d_pv)
                self.store["pv_track_forecast_kwh"] = (
                    float(self.store.get("pv_track_forecast_kwh") or 0.0) + d_fc)
            else:
                self.store["pv_track_clipped_min"] = (
                    float(self.store.get("pv_track_clipped_min") or 0.0) + (now - last_t) / 60.0)
        self.store["pv_track_last_epoch"]  = now
        self.store["pv_track_last_pv_kwh"] = pv_now
        if partial:
            factor, ratio = 1.0, None
        else:
            factor, ratio = _pv_tracking_factor(self.store.get("pv_track_actual_kwh", 0.0),
                                                self.store.get("pv_track_forecast_kwh", 0.0))
        self.store["pv_track_factor"] = factor
        self.store["pv_track_ratio"]  = ratio
        self._record_pv_tracking_hour(today)

    def _record_pv_tracking_hour(self, today):
        """One line per local hour into intraday_pv_tracking.json — the data the
        damping constants will be tuned from. Best-effort; never raises."""
        try:
            hour = _london_now().hour
        except Exception:
            return
        if self.store.get("pv_track_last_hour") == hour:
            return
        self.store["pv_track_last_hour"] = hour
        try:
            path = os.path.join(self.data_dir, "intraday_pv_tracking.json")
            rows = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    rows = json.load(fh) or []
            rows.append({
                "date":         today,
                "hour":         hour,
                "actual_kwh":   round(float(self.store.get("pv_track_actual_kwh") or 0.0), 3),
                "forecast_kwh": round(float(self.store.get("pv_track_forecast_kwh") or 0.0), 3),
                "ratio":        self.store.get("pv_track_ratio"),
                "factor":       self.store.get("pv_track_factor"),
                "clipped_min":  round(float(self.store.get("pv_track_clipped_min") or 0.0), 1),
                "pv_today_kwh": self.store.get("pv_daily_kwh"),
            })
            _atomic_write_json(path, rows[-PV_TRACKING_RECORD_ROWS:])
        except Exception as exc:
            self.logger.debug(f"[Tracking] recorder skipped: {exc}")

    def _measured_day_uplifts(self):
        """(monday, saturday, sunday) uplifts against the Tue-Fri mean of daily house use.

        Measured from daily_history.json over DAY_UPLIFT_WINDOW_DAYS and cached
        per local day.

        **Tue-Fri is the reference and is deliberately not one of the returned
        ratios.** Measuring each special day against a mean it is itself part of
        makes the ratios move each other — pull Monday out and every other
        figure shifts, for no reason to do with that day. Against a clean base
        the three are independent.

        v5.105.0 added Monday. Over the 90 days to 13-Sep-2026 it ran 22.09 kWh
        against a Tue-Fri mean of 20.76, and that 1.3 kWh sat inside a single
        weekday figure. It is a real effect rather than a thin sample: the ratio
        measures 1.061 / 1.064 / 1.065 / 1.070 over 63 / 90 / 120 / all days.
        v5.104.0 split Saturday from Sunday for the same reason.

        Each day falls back to its own default with too little history, and each
        is clamped to [0.9, 1.5] independently — a quiet Sunday must not be able
        to drag Saturday's figure with it, which is exactly what the single
        blended weekend uplift did. Days flagged partial (a missed midnight
        boundary) are left out.
        """
        today = _local_today_str()
        if (self.store.get("day_uplift_date") == today
                and self.store.get("day_uplifts")):
            return tuple(self.store["day_uplifts"])

        mon_u = MONDAY_UPLIFT_DEFAULT
        sat_u = SATURDAY_UPLIFT_DEFAULT
        sun_u = SUNDAY_UPLIFT_DEFAULT
        base, mon, sat, sun = [], [], [], []
        try:
            path = os.path.join(self.data_dir, "daily_history.json")
            with open(path, "r", encoding="utf-8") as fh:
                records = json.load(fh) or []
            cutoff = (datetime.strptime(today, "%Y-%m-%d")
                      - timedelta(days=DAY_UPLIFT_WINDOW_DAYS)).strftime("%Y-%m-%d")
            for r in records:
                d = str(r.get("date") or "")
                if d < cutoff or d >= today or r.get("energy_partial"):
                    continue
                try:
                    h = float(r.get("home_kwh") or 0.0)
                except (TypeError, ValueError):
                    continue
                if h < 2.0:
                    continue
                try:
                    dow = datetime.strptime(d, "%Y-%m-%d").weekday()
                except ValueError:
                    continue
                if dow == 0:
                    mon.append(h)
                elif dow == 5:
                    sat.append(h)
                elif dow == 6:
                    sun.append(h)
                else:
                    base.append(h)

            if len(base) >= DAY_UPLIFT_MIN_BASE_DAYS:
                m_base = sum(base) / len(base)
                if m_base > 0.0:
                    for samples, name in ((mon, "mon"), (sat, "sat"), (sun, "sun")):
                        if len(samples) >= DAY_UPLIFT_MIN_DAYS:
                            u = max(0.9, min(1.5, (sum(samples) / len(samples)) / m_base))
                            if name == "mon":
                                mon_u = u
                            elif name == "sat":
                                sat_u = u
                            else:
                                sun_u = u
        except Exception as exc:
            self.logger.debug(f"[Profile] day uplifts not measured: {exc}")

        mon_u, sat_u, sun_u = round(mon_u, 3), round(sat_u, 3), round(sun_u, 3)
        if list(self.store.get("day_uplifts") or []) != [mon_u, sat_u, sun_u]:
            if base and (mon or sat or sun):
                m_base = sum(base) / len(base)
                log(f"[Profile] Day uplifts measured over {DAY_UPLIFT_WINDOW_DAYS} days — "
                    f"Tue-Fri mean {m_base:.1f} kWh from {len(base)} days, "
                    f"Monday x{mon_u:.2f} ({len(mon)} days), "
                    f"Saturday x{sat_u:.2f} ({len(sat)} days), "
                    f"Sunday x{sun_u:.2f} ({len(sun)} days)")
            else:
                log(f"[Profile] Day uplifts are the defaults — Monday x{mon_u:.2f}, "
                    f"Saturday x{sat_u:.2f}, Sunday x{sun_u:.2f} (not enough history yet)")
        self.store["day_uplifts"]     = [mon_u, sat_u, sun_u]
        self.store["day_uplift_date"] = today
        return mon_u, sat_u, sun_u

    def _build_manager_snapshot(self, soc_pct, export_enabled, vpp_reserved_kwh):
        """Construct the immutable snapshot passed to manager.evaluate()."""
        prefs = self.pluginPrefs

        # Auto-calibrated daily consumption — once the 48-slot profile has
        # enough real readings, its total daily kWh is a better estimate than
        # the static weekday/weekend prefs (which were originally hand-tuned).
        # Prefs still win when they're set to a non-default value, so the user
        # can override the auto-derived figure if they want.  Detection of
        # "user set a custom value" is approximate — any value differing from
        # the documented default by > 1 kWh is treated as user-intent.
        vpp_today_kwh, vpp_tomorrow_kwh = self._compute_vpp_export_by_date()

        profile      = self.store.get("consumption_profile", []) or []
        live_daily   = sum(profile) if len(profile) == 48 else 0.0
        # v5.104.0: `weekendKwh` is the FALLBACK for the two new per-day prefs, so
        # a user who had overridden the weekend keeps that override on both days
        # rather than silently reverting to auto-calibration on upgrade.
        _weekend_pref = _as_float(prefs.get("weekendKwh"), 30.0)
        weekday_pref  = _as_float(prefs.get("weekdayKwh"),  22.0)
        # v5.105.0: Monday falls back to the Tue-Fri pref, so a user who had
        # overridden the weekday figure keeps that override on Monday too.
        monday_pref   = _as_float(prefs.get("mondayKwh"),   weekday_pref)
        saturday_pref = _as_float(prefs.get("saturdayKwh"), _weekend_pref)
        sunday_pref   = _as_float(prefs.get("sundayKwh"),   _weekend_pref)
        if live_daily >= 5.0:    # plausibility floor — ignore wildly low partial profiles
            weekday_user_override  = abs(weekday_pref  - 22.0) > 1.0
            monday_user_override   = abs(monday_pref   - 22.0) > 1.0
            saturday_user_override = abs(saturday_pref - 30.0) > 1.0
            sunday_user_override   = abs(sunday_pref   - 30.0) > 1.0
            # v5.90.0: the uplift is MEASURED (was a hard-coded 1.30 — 1.30 charged
            # ~28 kWh against every Saturday when the measured mean was 23.4).
            # v5.104.0 / v5.105.0: measured PER DAY TYPE against a Tue-Fri base.
            # The profile sum is a blend over all days, so the four figures are
            # scaled to put the WEEK back where the profile says it is — see
            # _need_scales(). v5.78.0's rule stands: no uplift while the house is
            # empty, because an uplift models people at home on a Saturday and
            # there are none.
            if self.store.get("away_active"):
                mon_uplift = sat_uplift = sun_uplift = 1.0
            else:
                mon_uplift, sat_uplift, sun_uplift = self._measured_day_uplifts()
            wd_scale, mon_scale, sat_scale, sun_scale = _need_scales(
                mon_uplift, sat_uplift, sun_uplift)
            if not weekday_user_override:
                weekday_pref  = round(live_daily * wd_scale,  1)
            if not monday_user_override:
                monday_pref   = round(live_daily * mon_scale, 1)
            if not saturday_user_override:
                saturday_pref = round(live_daily * sat_scale, 1)
            if not sunday_user_override:
                sunday_pref   = round(live_daily * sun_scale, 1)

        # Octopus Saving Session — a JOINED window live right now, from the cache the
        # hourly poll leaves behind (never a network call on the manager cycle).
        _ss_window = self._saving_session_window()
        _hh_window = self._happy_hour_window()

        return ManagerSnapshot(
            current_soc_pct    = soc_pct,
            capacity_kwh       = _as_float(prefs.get("batteryCapacityKwh"), 35.04),
            saving_session_active = _ss_window is not None,
            saving_session_hours  = float((_ss_window or {}).get("hours", 1.0)),
            saving_session_points_kwh = float((_ss_window or {}).get("points", 0) or 0),
            happy_hour_active     = _hh_window is not None,
            happy_hour_hours      = float((_hh_window or {}).get("hours", 1.0)),
            efficiency         = _as_float(prefs.get("batteryEfficiency"), 94) / 100.0,
            dawn_target_pct    = self._dawn_target_pct(),                      # v4.0: retained for VPP/storm
            health_cutoff_pct  = _as_float(prefs.get("batteryHealthCutoff"), 1),
            export_enabled     = export_enabled,
            max_export_kw      = _as_float(prefs.get("maxExportKw"), 4.0),
            inverter_max_kw    = _as_float(prefs.get("inverterMaxKw"), 10.0),
            export_rate_p      = _as_float((self.latest_rates_data or {}).get("export_rate_p"), DEFAULT_EXPORT_RATE_P),
            weekday_kwh        = weekday_pref,
            monday_kwh         = monday_pref,
            saturday_kwh       = saturday_pref,
            sunday_kwh         = sunday_pref,
            pv_watts                = int(self.latest_inverter_data.get("pvPowerWatts", 0)),
            house_load_watts        = int(self.latest_inverter_data.get("homePowerWatts", 0)),
            export_active           = self.store["export_active"],
            corrected_today_kwh     = float(self.latest_forecast_data.get("correctedTodayKwh", 0.0)),
            corrected_tomorrow_kwh  = float(self.latest_forecast_data.get("correctedTomorrowKwh", 0.0)),
            tariff                  = self._build_tariff_data(),
            forecast_p50            = self.latest_forecast_data.get("_hourly_p50_today", {}),
            dawn_times              = self.latest_forecast_data.get("_dawn_times", {}),
            consumption_profile     = self.store.get("consumption_profile", []),
            now                     = datetime.now(timezone.utc),
            bias_factor                 = float(self.latest_forecast_data.get("biasFactor", 1.0)),
            # v5.65.0: the control path's own factor. Falls back to biasFactor and
            # then 1.0, so a forecast payload predating the bands still behaves as
            # it did rather than silently dropping the correction altogether.
            bias_factor_today           = float(
                self.latest_forecast_data.get("biasFactorToday")
                or self.latest_forecast_data.get("biasFactor", 1.0) or 1.0),
            vpp_active                  = self.store["vpp_active"],
            vpp_reserved_kwh            = vpp_reserved_kwh,
            vpp_today_kwh               = vpp_today_kwh,
            vpp_tomorrow_kwh            = vpp_tomorrow_kwh,
            solar_overflow_active       = self.store["solar_overflow_active"],
            solar_overflow_charge_cap   = self.store["solar_overflow_charge_cap_w"],
            solar_overflow_released_at  = self.store.get("solar_overflow_released_at"),
            solar_overflow_target_pct   = _as_float(prefs.get("solarOverflowTargetSoc"),
                                                    SOLAR_OVERFLOW_TARGET_SOC_PCT),
            solar_overflow_min_end_pct  = _as_float(prefs.get("solarOverflowMinEndSoc"),
                                                    SOLAR_OVERFLOW_MIN_END_SOC_PCT),
            # RAW todayKwh, never correctedTodayKwh: the 40 kWh threshold is
            # calibrated against the raw model total, and the bias band factor
            # reaches ~1.2 around 30 kWh, so feeding the corrected figure to a
            # raw-calibrated gate would move it by a fifth without anyone noticing.
            raw_today_kwh               = _as_float(
                self.latest_forecast_data.get("todayKwh"), 0.0),
            bank_first_small_latched    = bool(
                self.store.get("bank_first_small_latched", False)),
            solar_overflow_bank_first_max_kwh = _as_float(
                prefs.get("solarOverflowBankFirstMaxKwh"),
                SOLAR_OVERFLOW_BANK_FIRST_MAX_KWH),
            solar_overflow_bank_first_soc     = _as_float(
                prefs.get("solarOverflowBankFirstSoc"),
                SOLAR_OVERFLOW_BANK_FIRST_SOC_PCT),
            # storm_active is set by _apply_storm_override AFTER the snapshot is built
            # (it already mutates dawn_target_pct/export_enabled there) — see step 4.
            flood_prev_target_soc       = float(self.store.get("flood_prev_target_soc") or 0.0),
            # v5.90.0 — what has actually happened today (Stage 3). home_today_kwh is
            # None until the daily-energy object has seen a reading today, so a
            # fresh start cannot present 0.0 kWh used as a measurement.
            pv_tracking_factor          = float(self.store.get("pv_track_factor", 1.0) or 1.0),
            pv_tracking_ratio           = self.store.get("pv_track_ratio"),
            home_today_kwh              = self._home_today_measured_kwh(),
            home_today_partial          = bool(self.store.get("energy_day_partial", False)),
        )

    def _home_today_measured_kwh(self):
        """Today's measured house use, or None when nothing has been observed today."""
        de = getattr(self, "daily_energy", None)
        if de is None or de.today_date != _local_today_str() or not de.latest:
            return None
        try:
            return float(self.store.get("home_daily_kwh"))
        except (TypeError, ValueError):
            return None

    def _apply_seasonal_override(self, snapshot):
        """Raise resilience floor in winter months (Oct–Mar) — longer nights."""
        local_month = _london_now().month

        applied = None
        if local_month in (10, 11, 12, 1, 2, 3):
            winter_buf = _as_float(self.pluginPrefs.get("winterBufferPct"), 20)
            if winter_buf > snapshot.dawn_target_pct:
                snapshot.dawn_target_pct = winter_buf
                applied = winter_buf
        # Log only on a state change — this runs every 60s evaluate, so an
        # unconditional log spammed the event log all winter.
        if applied != self.store.get("seasonal_override_applied"):
            if applied is not None:
                log(f"[Seasonal] Winter buffer active (month {local_month}): "
                    f"resilience floor raised to {applied:.0f}%")
            elif self.store.get("seasonal_override_applied") is not None:
                log("[Seasonal] Winter buffer no longer active — back to summer floor")
            self.store["seasonal_override_applied"] = applied

    def _apply_storm_override(self, snapshot, soc_pct):
        """Raise dawn target and suppress exports during active storm warnings.

        Export is suppressed ONLY while the battery is still filling toward the
        storm reserve (SOC below the release point). At/above the release point
        the reserve is banked, so holding export off achieves nothing for
        resilience and merely rams the battery to 100% (charge takes priority
        over export in self-consumption), clipping all PV above the DNO export
        cap. Releasing lets Solar Overflow resume — throttling the charge and
        pushing surplus to grid so the battery creeps up with headroom rather
        than slamming full and curtailing. Mirrors the post-cut lockout floor.
        """
        storm_level = self.store.get("storm_level", "none")
        if storm_level in ("amber", "red"):
            override_soc = STORM_SOC_AMBER
        elif storm_level == "yellow":
            override_soc = STORM_SOC_YELLOW
        else:
            self.store["storm_override_logged_level"] = None
            self.store["storm_export_suppressed"]     = False
            snapshot.storm_active                     = False
            return

        snapshot.dawn_target_pct = max(snapshot.dawn_target_pct, override_soc)
        # v5.51.0: restore the 100% solar-overflow charge target for the duration. A
        # storm is the one time a genuinely full battery is worth clipping for. The
        # pacing stays lazy even then — if the day's own solar reaches 100% unaided,
        # required_charge stays small and nothing is force-charged out of export, which
        # is CliveS's explicit constraint: don't ram the battery full ahead of a storm
        # when the sun was going to do it anyway.
        snapshot.storm_active = True

        # Release point can never sit below the active reserve target — below the
        # target we genuinely want max charge (overflow off) to fill fast for a cut.
        try:
            soc_val = float(soc_pct)
        except (TypeError, ValueError):
            soc_val = 0.0   # unknown SOC — fail safe: keep export suppressed
        release_pct = max(self._storm_export_release_pct(), override_soc)
        suppress    = soc_val < release_pct
        if suppress:
            snapshot.export_enabled = False

        # Log on storm-level change OR when the release point first lets export
        # resume mid-storm, so the event log explains why export is running while
        # a warning is active. Both are state changes, not a 60s heartbeat.
        prev_suppressed = bool(self.store.get("storm_export_suppressed", False))
        level_changed   = self.store.get("storm_override_logged_level") != storm_level
        if level_changed:
            log(
                f"[Storm] Storm override active (level={storm_level}): dawn target "
                f"raised to {snapshot.dawn_target_pct:.0f}%, export "
                f"{'suppressed' if suppress else f'allowed (SOC {soc_val:.0f}% >= {release_pct:.0f}% release)'}"
            )
            self.store["storm_override_logged_level"] = storm_level
        elif prev_suppressed and not suppress:
            log(
                f"[Storm] SOC {soc_val:.0f}% >= {release_pct:.0f}% release point — "
                f"export re-enabled during storm to protect solar (reserve already banked)"
            )
        self.store["storm_export_suppressed"] = suppress

    def _log_bank_first(self, decision, snapshot, soc_pct):
        """One line when the hold starts, one when it lifts. At most two a day.

        Without the second line, "banking correctly" and "the gate is stuck" look
        exactly the same in the log.
        """
        try:
            store     = self.store
            local_now = _to_london(snapshot.now)
            today_str = local_now.strftime("%Y-%m-%d")
            if store.get("bank_first_logged_date") != today_str:
                store["bank_first_logged_date"]    = ""
                store["bank_first_release_logged"] = False

            # One read, two lines. v5.111.1 moved both the opening and the closing
            # line onto the three-state field so they cannot disagree about what the
            # gate did; `holding` is now derived rather than read separately.
            state   = getattr(decision, "bank_first_state", BANK_FIRST_NOT_ASKED)
            holding = (state == BANK_FIRST_HOLDING)
            gate    = float(getattr(decision, "bank_first_gate_pct", 0.0) or 0.0)
            gate    = min(SOLAR_OVERFLOW_BANK_FIRST_SOC_MAX, max(0.0, gate))

            if holding and store.get("bank_first_logged_date") != today_str:
                store["bank_first_logged_date"] = today_str
                max_kwh = min(float(snapshot.solar_overflow_bank_first_max_kwh or 0.0),
                              SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX)
                # v5.111.3: quote the forecast the day was CLASSIFIED on, not the
                # one showing now — see the setter in _record_bank_first_metrics. A
                # missing figure prints no figure: an absent number is a worse read
                # than no number only when the reader cannot tell which they have.
                latched_kwh   = store.get("bank_first_latched_small_kwh")
                latched_local = str(store.get("bank_first_latched_small_local") or "")
                if latched_kwh is None:
                    fc_clause = "today's forecast is below the"
                else:
                    fc_clause = (f"today's forecast was {float(latched_kwh):.1f} kWh when "
                                 f"the day was classified small"
                                 f"{f' at {latched_local}' if latched_local else ''}, "
                                 f"below the")
                log(f"[Manager] Banking first — daytime export held until SOC reaches "
                    f"{gate:.0f}%. SOC {soc_pct:.1f}%, {fc_clause} "
                    f"{max_kwh:.1f} kWh cap-saturation threshold, so nothing is gained "
                    f"by selling early — the surplus is still there this afternoon.")

            # v5.111.1: `not holding` is not evidence of a release — see the
            # BANK_FIRST_* constants in battery_manager.py for the 19-09-2026 line
            # that claimed the hold was satisfied at 36% SOC because the physics gate
            # had refused one tick after the hold engaged. Read the state instead, so
            # this fires once, on a gate that genuinely ran and let the day through.
            if (state == BANK_FIRST_RELEASED
                    and store.get("bank_first_logged_date") == today_str
                    and not store.get("bank_first_release_logged")
                    and int(store.get("bank_first_blocked_samples", 0)) > 0):
                store["bank_first_release_logged"] = True
                held_min  = int(store.get("bank_first_blocked_samples", 0))
                withheld  = float(store.get("bank_first_withheld_kwh", 0.0))
                peak_kw   = float(store.get("bank_first_peak_surplus_kw", 0.0))
                log(f"[Manager] Bank-first satisfied at {local_now.strftime('%H:%M')} — "
                    f"SOC {soc_pct:.1f}%, export handed back to the overflow gate. "
                    f"Held {held_min // 60}h{held_min % 60:02d}m, {withheld:.1f} kWh not "
                    f"exported, peak surplus {peak_kw:.1f} kW "
                    f"(cap {float(snapshot.max_export_kw or 0.0):.1f} kW).")
        except Exception as exc:
            self.logger.debug(f"[BankFirst] log skipped: {exc!r}")

    def _log_manager_decision(self, decision, snapshot, soc_pct):
        """Log manager decisions only on action change — no periodic heartbeat."""
        last_action    = self.store.get("last_manager_action", "")
        action_changed = decision.action != last_action

        # Bank-first gets its own matched pair, ABOVE the action-change gate. It has
        # to: the action does not change when the hold bites — the manager was
        # already sitting on self_consumption overnight — so every path below returns
        # early and the hold would be completely silent. An absence always reads as a
        # fault, and a page that withholds something must say so.
        self._log_bank_first(decision, snapshot, soc_pct)

        if decision.action == ACTION_SOLAR_OVERFLOW:
            if not action_changed:
                return
            log(
                f"[Manager] SOC={soc_pct:.1f}%  PV={snapshot.pv_watts}W  "
                f"Action=solar_overflow"
            )
            if self.debug:
                for line in decision.reason.split("\n"):
                    indigo.server.log(f"  {line}")
        else:
            if not action_changed:
                return
            log(
                f"[Manager] SOC={soc_pct:.1f}%  PV={snapshot.pv_watts}W  "
                f"Action={decision.action}  {decision.reason}"
            )

        # v5.22.0 — Decision audit trail (plan-object pattern from
        # battery_manager.py v3.5).  Logged on action change so the WHY of the
        # new action is visible without re-running with debug on.  Silently
        # skipped when audit_trail is empty (e.g. unit-test Decisions
        # constructed directly without going through evaluate()).
        audit_trail = getattr(decision, "audit_trail", None) or []
        if audit_trail:
            log("[Manager] === DECISION AUDIT ===")
            tag_width = max((len(t) for t, _ in audit_trail), default=10)
            for tag, msg in audit_trail:
                log(f"[Manager]   [{tag:<{tag_width}}]  {msg}")
            log("[Manager] ======================")

        self.store["last_manager_action"] = decision.action

    # ================================================================
    # Storm Watch
    # ================================================================

    def _resolve_pushover_user(self):
        """Return the Pushover user/group key, preferring IndigoSecrets.py over PluginConfig.

        Returns "" and logs an ERROR (once per call) if neither source is set.
        Callers should treat "" as "skip alert".
        """
        token = PUSHOVER_USER_TOKEN or self.pluginPrefs.get("pushoverUserToken", "")
        if not token:
            log(
                "[Pushover] No user/group key configured. Set PUSHOVER_USER_TOKEN in "
                "IndigoSecrets.py or fill in 'Pushover user/group key' under Plugins -> "
                "Sigenergy Manager -> Configure. Alert skipped.",
                level="ERROR",
            )
        return token

    def _resolve_dashboard_host(self):
        """Resolve the host shown in the dashboard URL log line.

        Order of precedence: DASHBOARD_HOST in IndigoSecrets.py, then dashboardHost
        in PluginConfig, then auto-detect via socket.  Auto-detect is the silent
        default — no warning needed, since the dashboard binds to all interfaces
        regardless and the URL is purely a convenience log line.
        """
        host = DASHBOARD_HOST or self.pluginPrefs.get("dashboardHost", "")
        if host:
            return host
        try:
            import socket
            # UDP connect to a public address (no packets sent) — tells the OS
            # which local interface IP would route outbound traffic.
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                host = s.getsockname()[0]
            finally:
                s.close()
        except Exception as exc:
            log(f"[Web] Could not auto-detect LAN IP ({exc}); falling back to localhost", level="WARNING")
            host = "localhost"
        return host

    def _dawn_target_pct(self):
        """The summer resilience floor, as a percent.

        One reader for one rule. Four call sites used to coerce this pref
        independently, every one of them falling back to 10 — a value the plugin
        itself refuses: `validatePrefsConfigUi` rejects anything under 15 at save
        time, and the startup migration raises a stored value below 15. So the
        only state that could ever have produced a 10 was a pref that had never
        been written, and 10 is exactly the health floor this buffer exists to
        sit above.

        The migration's own read is deliberately NOT routed through here — it has
        to see the raw pre-migration value to know whether to raise it.
        """
        return _as_float(self.pluginPrefs.get("dawnSocTarget"), 15)

    def _resolve_dashboard_bind(self):
        """Return the address the dashboard should bind to.

        Loopback is the default and the right answer for almost everyone: the
        Dashboards plugin proxies to 127.0.0.1 server-side, so the energy and
        cost pages keep working, and nothing is exposed to the LAN. Widening it
        is deliberate, and requires a token.
        """
        if self.pluginPrefs.get("dashboardBind", "loopback") == "all":
            return DASHBOARD_BIND_ALL
        return DASHBOARD_BIND_LOOPBACK

    def _resolve_dashboard_token(self):
        """Return the dashboard access token, generating and storing one if needed.

        IndigoSecrets first, then PluginConfig, then a generated token kept in a
        0600 file beside the plugin's other data. The file rather than the prefs
        because pluginPrefs are only flushed on a graceful shutdown, and a token
        that vanishes after a crash locks the user out of their own dashboard.
        """
        token = (SIGEN_DASHBOARD_TOKEN
                 or self.pluginPrefs.get("dashboardToken", "")).strip()
        if token:
            return token

        path = os.path.join(self.data_dir, "dashboard_token.txt")
        try:
            with open(path, encoding="utf-8") as handle:
                token = handle.read().strip()
            if token:
                return token
        except OSError:
            pass

        # No token anywhere — mint one. Done even for a loopback bind so that
        # widening the bind later is a one-field change rather than a puzzle.
        import secrets
        token = secrets.token_urlsafe(24)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(token)
            os.chmod(path, 0o600)
        except OSError as exc:
            log(f"[Web] Could not save the dashboard token to {path}: {exc}",
                level="WARNING")
        return token

    def _dashboard_url(self):
        """The URL that actually works right now, token included where needed."""
        if self._resolve_dashboard_bind() == DASHBOARD_BIND_LOOPBACK:
            return f"http://127.0.0.1:{WEB_DASHBOARD_PORT}/"
        host = self._resolve_dashboard_host()
        return (f"http://{host}:{WEB_DASHBOARD_PORT}/"
                f"?token={self._resolve_dashboard_token()}")

    def _is_in_quiet_hours(self):
        """True if the current local time falls within the configured Pushover
        quiet-hours window.  Window may straddle midnight (e.g. 22:00 → 07:00).
        Blank/invalid config returns False so alerts always send."""
        start_str = (self.pluginPrefs.get("pushoverQuietStart") or "").strip()
        end_str   = (self.pluginPrefs.get("pushoverQuietEnd")   or "").strip()
        if not start_str or not end_str:
            return False
        try:
            sh, sm = [int(x) for x in start_str.split(":")]
            eh, em = [int(x) for x in end_str.split(":")]
        except (ValueError, AttributeError):
            return False
        now       = _london_now()
        now_min   = now.hour * 60 + now.minute
        start_min = sh * 60 + sm
        end_min   = eh * 60 + em
        if start_min <= end_min:
            return start_min <= now_min < end_min
        # straddles midnight
        return now_min >= start_min or now_min < end_min

    def _send_pushover(self, title, message, priority="0"):
        """Send a Pushover notification. Called from the main plugin thread.

        Honours the configured sound (pluginPrefs.pushoverSound, default
        'vibrate') and quiet-hours window.  HIGH-priority alerts (priority>=1)
        always send regardless of quiet hours so storm amber/red and VPP
        release failures are never silenced.
        """
        user = self._resolve_pushover_user()
        if not user:
            return
        try:
            priority_int = int(priority)
        except (TypeError, ValueError):
            priority_int = 0
        if priority_int < 1 and self._is_in_quiet_hours():
            log(f"[Pushover] suppressed (quiet hours): {title}")
            return
        sound = self.pluginPrefs.get("pushoverSound", "vibrate") or "vibrate"
        try:
            pushover = indigo.server.getPlugin("io.thechad.indigoplugin.pushover")
            if pushover and pushover.isEnabled():
                pushover.executeAction("send", props={
                    "msgTitle":    title,
                    "msgBody":     message,
                    "msgUser":     user,
                    "msgPriority": str(priority),
                    "msgSound":    sound,
                })
                log(f"[Pushover] sent: {title}")
            else:
                log("[Pushover] plugin not enabled — alert not sent", level="ERROR")
        except Exception as exc:
            log(f"[Pushover] send failed: {exc}", level="ERROR")

    def _resolve_powercut_email(self):
        """Return the power-cut email recipient, preferring IndigoSecrets.py
        (POWERCUT_EMAIL) over the powerCutEmailRecipient PluginConfig field.
        Returns '' when neither is set (email is then skipped)."""
        return (POWERCUT_EMAIL or self.pluginPrefs.get("powerCutEmailRecipient", "") or "").strip()

    def _power_cut_lockout_end_local(self):
        """Local clock time the export lockout expires, or "" if none is armed.

        Read from the SAME pluginPrefs["powerRestoredTime"] the lockout window
        itself uses, so the time quoted in the alert cannot drift from the time
        export actually resumes. The caller arms the lockout (and writes that
        pref) before sending the restore alert, so it is already there.
        """
        prt_str = self.pluginPrefs.get("powerRestoredTime", "")
        if not prt_str:
            return ""
        try:
            restored = datetime.fromisoformat(prt_str)
            if restored.tzinfo is None:
                restored = restored.replace(tzinfo=timezone.utc)
            return _local_time(restored + timedelta(hours=POWER_CUT_LOCKOUT_HOURS))
        except (ValueError, TypeError, AttributeError):
            return ""

    def _solar_refill_outlook(self, soc_pct):
        """Will today's solar refill the lockout reserve on its own? Answer it NOW.

        Returns {releases, surplus_kwh, needed_kwh, is_daytime} or None when the
        question cannot be answered (no SOC, no forecast yet, anything raising).
        None is a real answer here — the caller then falls back to naming both
        rules without claiming to know which will apply.

        Built the SAME way _evaluate_manager_impl builds it inside a lockout
        window — provisional snapshot, then the manager's own 24h balance — so
        the alert and the decision cannot disagree. Both calls are pure and hold
        no lock of their own; _state_lock is an RLock, so re-entering is safe.

        The two figures are worth returning even when the answer is no: they are
        what makes "not yet" checkable rather than a bare refusal, and they are
        the numbers that were missing from the log on 26-Jul-2026 when export sat
        suppressed for 20 minutes with no way to see what the plugin was judging.
        """
        if soc_pct is None:
            return None
        try:
            provisional = self._build_manager_snapshot(
                soc_pct, bool(self.pluginPrefs.get("exportEnabled", False)), 0.0,
            )
            balance = self.manager._calculate_24h_balance(provisional)
        except Exception as exc:
            log(f"[PowerCut] Could not work out the solar-refill outlook ({exc!r})",
                level="WARNING")
            return None

        cap_kwh     = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), BATTERY_CAPACITY_KWH)
        floor_kwh   = self._power_cut_lockout_soc_floor() / 100.0 * cap_kwh
        battery_kwh = float(getattr(balance, "battery_kwh", 0.0))
        is_daytime  = bool(getattr(balance, "is_daytime", False))
        surplus     = max(0.0, float(getattr(balance, "remaining_solar_kwh", 0.0))
                          - float(getattr(balance, "remaining_home_to_dusk_kwh", 0.0)))
        # Quote the margin-inflated figure, because that is the bar actually used.
        needed      = max(0.0, floor_kwh - battery_kwh) * POWER_CUT_LOCKOUT_REFILL_MARGIN

        return {
            "releases": _solar_refill_releases_lockout(
                is_daytime          = is_daytime,
                soc_pct             = soc_pct,
                min_soc_pct         = self._power_cut_lockout_min_soc(),
                battery_kwh         = battery_kwh,
                floor_kwh           = floor_kwh,
                remaining_solar_kwh = float(getattr(balance, "remaining_solar_kwh", 0.0)),
                home_to_dusk_kwh    = float(getattr(balance, "remaining_home_to_dusk_kwh", 0.0)),
            ),
            "surplus_kwh": surplus,
            "needed_kwh":  needed,
            "is_daytime":  is_daytime,
        }

    def _power_cut_status_lines(self, kind):
        """Build the detail paragraphs for a power-cut alert. Returns a list.

        Each paragraph is independent and is simply omitted when the readings
        behind it are missing, so a dropped Modbus register costs one line rather
        than the whole message.
        """
        lines = []
        data  = getattr(self, "latest_inverter_data", None) or {}

        soc      = _as_float(data.get("batterySoc"), None)
        home_w   = _as_float(data.get("homePowerWatts"), None)
        capacity = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), BATTERY_CAPACITY_KWH)
        floor    = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)

        # Battery + load + how long that lasts.
        if soc is not None:
            stored = soc / 100.0 * capacity
            bits   = [f"Battery {soc:.0f}% ({stored:.1f} kWh stored)"]
            if home_w is not None:
                bits.append(f"the house is drawing {home_w:.0f} W")
            runtime = _format_runtime(
                _backup_runtime_hours(soc, floor, capacity, home_w))
            if runtime:
                # Say plainly that this is at the load right now — the figure
                # moves the moment anything switches on, and on a sunny day the
                # panels stretch it a long way further.
                if kind == "lost":
                    bits.append(f"enough to carry it for about {runtime} at that load")
                else:
                    bits.append(f"about {runtime} of backup at that load "
                                f"if the power goes again")
            lines.append(", ".join(bits) + ".")

        # What export is doing now the grid is back, and which way the solar rule
        # is currently pointing. Worked out here rather than left as a conditional
        # — the reader wants to know whether it applies to them today.
        if kind != "lost":
            lockout = _lockout_message(
                export_enabled    = bool(self.pluginPrefs.get("exportEnabled", False)),
                lockout_end_local = self._power_cut_lockout_end_local(),
                soc_floor_pct     = self._power_cut_lockout_soc_floor(),
                outlook           = self._solar_refill_outlook(soc),
            )
            if lockout:
                lines.append(lockout)
        return lines

    def _send_power_cut_notification(self, kind, detail=""):
        """Alert on a grid-status transition via Pushover + email.

        kind     — "lost" when mains power drops (house islands onto the battery)
                   or "restored" when it returns.
        detail   — the off-grid mode string on loss (e.g. "Off-grid (auto)"), or
                   the pre-formatted duration suffix on restoration (e.g.
                   " (duration 83s)") as built by the caller.

        Both channels carry the SAME full body: battery level, what the house is
        drawing, how long the battery would carry it, and — on a restore — what
        the export lockout is doing and both ways out of it. These are the two
        messages read on a phone during an outage, so everything needed to judge
        the situation goes in them rather than in a log nobody opens at the time.

        Every figure is optional. A line whose numbers are missing is left out
        rather than printed as a dash or a zero, so a partial Modbus read can
        never dress a guess up as a reading.

        Pushover is sent at normal priority so it honours the configured quiet
        hours. Both sends are best-effort: a failure (most likely when the outage
        also took the broadband down) is logged and swallowed so the poll loop
        never dies — the alert then lands once connectivity returns.
        """
        if not self.pluginPrefs.get("powerCutNotify", True):
            return

        local_now = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
        if kind == "lost":
            title = "Power cut — grid lost"
            head  = (f"Mains power lost at {local_now}. The house is now running "
                     f"on the Sigenergy battery ({detail}).")
        else:
            title = "Power restored"
            head  = f"Mains power restored at {local_now}{detail}."

        paragraphs = [head]
        try:
            paragraphs.extend(self._power_cut_status_lines(kind))
        except Exception as exc:
            # The headline is the part that matters. Never let a missing reading
            # or a bad pref stop the alert going out.
            log(f"[PowerCut] Could not build notification detail: {exc!r}",
                level="WARNING")
        message = "\n\n".join(paragraphs)

        # Pushover — normal priority, so quiet hours are respected.
        try:
            self._send_pushover(title, message, priority="0")
        except Exception as exc:
            log(f"[PowerCut] Pushover notify failed: {exc}", level="WARNING")

        # Email via the Email+ SMTP device (best-effort — needs connectivity).
        recipient = self._resolve_powercut_email()
        if recipient:
            try:
                indigo.server.sendEmailTo(recipient, subject=title, body=message)
                log(f"[PowerCut] Email sent to {recipient}")
            except Exception as exc:
                log(f"[PowerCut] Email to {recipient} failed: {exc}", level="WARNING")
        else:
            log("[PowerCut] No email recipient configured — email step skipped")

    def _check_storm_watch(self):
        """
        Poll the MeteoAlarm CAP feed for incoming wind/storm risk covering the
        configured site.  Updates self.store['storm_level'] and sends a Pushover
        alert when the level escalates.  Sends an all-clear when it drops to 'none'.
        """
        loc_name = self.pluginPrefs.get("siteLocationName") or "your area"
        site_lat = (SITE_LATITUDE if SITE_LATITUDE is not None
                    else _as_float(self.pluginPrefs.get("siteLatitude"), None))
        site_lon = (SITE_LONGITUDE if SITE_LONGITUDE is not None
                    else _as_float(self.pluginPrefs.get("siteLongitude"), None))
        try:
            if site_lat is not None and site_lon is not None:
                new_level, reason = check_storm_level(site_lat, site_lon, loc_name)
            else:
                # No configured coordinates — fall back to storm_watch's defaults.
                new_level, reason = check_storm_level(location_name=loc_name)
        except Exception as exc:
            log(f"[Storm] check_storm_level() raised: {exc}", level="WARNING")
            return

        with self._state_lock:   # v5.45.0: poll unlocked, merge locked
            self._apply_storm_result(new_level, reason)

    def _apply_storm_result(self, new_level, reason):
        """Merge a MeteoAlarm poll result. Caller holds the lock."""
        # Read here, not from the caller. The v5.45.0 locking restructure split
        # this method out of _check_storm_watch and left loc_name behind in it,
        # so the yellow-escalation and all-clear alerts — the two bodies that
        # quote it — raised NameError instead of sending. Worse than the missing
        # alert: storm_alerted_level is only written AFTER a successful send, so
        # a failed all-clear left it stuck at the old level and every later
        # storm at or below that level was then judged "already alerted" and
        # stayed silent. Latent since 02-Jul-2026 — no storm has hit since.
        loc_name      = self.pluginPrefs.get("siteLocationName") or "your area"
        prev_level    = self.store.get("storm_level", "none")
        alerted_level = self.store.get("storm_alerted_level", "none")

        if new_level is None:
            # Poll FAILED (feed unreachable / XML unparseable / schema drift) —
            # that is "unknown", not all-clear. Hold the previous level so a flaky
            # internet connection mid-storm (the likeliest time for one) cannot
            # drop an active storm reserve and send a false "risk has passed"
            # push. After STORM_POLL_FAIL_LIMIT consecutive failures decay to
            # "none" with its own notification — any real warning would have
            # expired by then, and holding a stale reserve forever against a
            # permanently-changed feed would be its own failure mode.
            fails = self.store.get("storm_poll_failures", 0) + 1
            self.store["storm_poll_failures"] = fails
            if prev_level != "none" and fails >= STORM_POLL_FAIL_LIMIT:
                log(
                    f"[Storm] Feed unavailable for {fails} consecutive polls — "
                    f"releasing the held '{prev_level}' storm reserve (any real "
                    f"warning has likely expired). Last error: {reason}",
                    level="WARNING",
                )
                self.store["storm_level"] = "none"
                if alerted_level != "none":
                    self._send_pushover(
                        "Storm Watch - Feed Lost",
                        f"MeteoAlarm has been unreachable for ~24 hours; the held "
                        f"'{prev_level}' storm reserve has been released. Check "
                        f"the Met Office directly if storms are still forecast.",
                        priority="0",
                    )
                    self.store["storm_alerted_level"] = "none"
                    self._save_accumulators()
            else:
                held = (f" — holding level '{prev_level}'"
                        if prev_level != "none" else "")
                log(f"[Storm] Poll failed ({fails} consecutive): {reason}{held}",
                    level="WARNING")
            return

        self.store["storm_poll_failures"] = 0
        self.store["storm_level"] = new_level

        # Log-on-change only — the 2-hourly no-change poll was producing ~24
        # heartbeat lines/day.  The reason line goes through log() (not raw
        # indigo.server.log) so it keeps the timestamp/file-mirror convention.
        if new_level != prev_level:
            log(f"[Storm] Level={new_level}  prev={prev_level}")
            log(f"[Storm]   {reason}")
        elif new_level != "none":
            # Active warning — keep the reason visible on each poll so an
            # in-progress storm still leaves a trail, without the 'none' noise.
            log(f"[Storm] Level={new_level}  {reason}")

        # Severity indices for comparison
        new_idx     = STORM_LEVELS.index(new_level)    if new_level    in STORM_LEVELS else 0
        alerted_idx = STORM_LEVELS.index(alerted_level) if alerted_level in STORM_LEVELS else 0

        # Escalation: new level is more severe than the last alert sent
        if new_idx > alerted_idx:
            if new_level == "yellow":
                title = "Storm Watch - Yellow"
                body  = (
                    f"A yellow-level wind risk is forecast for {loc_name}. "
                    f"Battery held at a {STORM_SOC_YELLOW:.0f}% minimum reserve "
                    f"(no grid charging above that); export held off until the "
                    f"battery is nearly full, as a precaution.\n\n{reason}"
                )
                priority = "0"
            elif new_level == "amber":
                title = "Storm Warning - Amber"
                body  = (
                    f"An amber-level wind/storm warning is active for your area. "
                    f"Battery held at a {STORM_SOC_AMBER:.0f}% minimum reserve "
                    f"(no grid charging above that); export held off until the "
                    f"battery is nearly full, against power cuts.\n\n{reason}"
                )
                priority = "1"   # high priority
            else:  # red
                title = "Storm Warning - RED"
                body  = (
                    f"A RED storm warning is active for your area. Power cut risk "
                    f"is high. Battery held at a {STORM_SOC_AMBER:.0f}% minimum "
                    f"reserve (no grid charging above that); export held off until "
                    f"the battery is nearly full.\n\n{reason}"
                )
                priority = "1"   # high priority
            self._send_pushover(title, body, priority)
            self.store["storm_alerted_level"] = new_level
            self._save_accumulators()   # persist NOW so a restart can't re-send this alert

        # De-escalation: level dropped back to none after an alert was sent
        elif new_level == "none" and alerted_idx > 0:
            self._send_pushover(
                "Storm Watch Cleared",
                f"Storm/wind risk has passed for {loc_name}. "
                "Normal battery management and export schedule resumed.",
                priority="0",
            )
            self.store["storm_alerted_level"] = "none"
            self._save_accumulators()

    def _happy_hour_alert_body(self, when, duration_h, event):
        """What to say about a Weekend Happy Hour, given what the reader can do about it.

        A Happy Hour is an hour of FREE electricity, earned by successful turn-downs and
        then BOOKED on the Octopus site — Octopus offers four slots per Sunday and only
        the one actually booked comes back joined. Three facts decide the message, and
        all three come from the API rather than from us:

          token_balance   how many tokens the account holds (reported VERBATIM — the
                          accrual rule is not derivable, see octopus_api)
          capacity        whether the slot can still be booked at all
          joined          whether THIS slot is the one already booked

        The rule this exists to enforce: never tell someone to act on something they
        cannot act on. On 03-Sep-2026 four pushes went out saying "opt in to earn" for
        four slots against a balance that could not pay for one — advice that cost the
        reader four interruptions and bought nothing.
        """
        tokens   = self.store.get("happy_hour_tokens")
        need     = self._happy_hour_tokens_required()
        capacity = event.get("capacity")
        joined   = bool(event.get("joined"))

        body = (f"Octopus Weekend Happy Hour: {when}"
                + (f" ({duration_h:.1f}h)" if duration_h else "")
                + " — free electricity for the hour."
                + self._happy_hour_expiry_note())

        if joined:
            enabled = _as_bool(self.pluginPrefs.get("happyHourImport"), False)
            return body + ("  BOOKED. The battery will grid-charge through it."
                           if enabled else
                           "  BOOKED — but Happy Hour import is switched off, so the "
                           "battery will not charge for it. Tick it in the plugin config.")

        if capacity and capacity != "AVAILABLE":
            return body + f"  This slot is {capacity.replace('_', ' ').lower()}."

        # Not booked, and bookable as far as capacity goes. What is left is the token
        # count — and this is REPORTED, never used as a verdict.
        #
        # MEASURED 03-Sep-2026: the API said tokenBalance=1 while the Octopus app showed
        # CliveS 0, and the schema describes the field plainly as "the account's Weekend
        # Happy Hours token balance". A documented field can still disagree with the
        # vendor's own UI, and it is the UI that decides what the account may actually
        # do. So the plugin ATTRIBUTES the number rather than asserting it, and never
        # says "you cannot book this" — a wrong refusal would talk him out of a free
        # hour he was entitled to, which is a far worse failure than a redundant nudge.
        if tokens is None:
            return body + "  Book it in the Octopus app if you have the tokens."
        if need and tokens < need:
            return (body + f"  Octopus's API reports {tokens} of the {need} tokens a "
                    "booking needs — check the app, which is the authority, and book it "
                    "there if it lets you. Each successful Power Down earns towards one.")
        return (body + f"  Octopus's API reports {tokens} tokens — book it in the app "
                "and the battery will charge itself free for that hour.")

    @staticmethod
    def happy_hour_slots_left(from_day,
                              scheme_end=None,
                              max_per_weekend=None):
        """How many Happy Hours could still be USED, counting from `from_day` on.

        v5.107.0. Pure and static so it can be tested without a plugin, an Indigo
        or a clock. Counts weekend DAYS in [from_day, scheme_end), groups them into
        distinct weekends, and allows `max_per_weekend` in each.

        A Saturday and the Sunday after it are ONE weekend for this purpose, and the
        grouping is by the Saturday's date because that says so plainly. (An ISO
        week number would in fact give the same answer — ISO weeks run Monday to
        Sunday, so a Saturday and the next day always share one. A comment here
        previously claimed otherwise; a mutation swapping the two proved it wrong,
        which is the sort of thing mutation testing is actually for.)
        """
        end   = scheme_end or HAPPY_HOUR_SCHEME_END
        per   = HAPPY_HOUR_MAX_PER_WEEKEND if max_per_weekend is None else max_per_weekend
        # Belt and braces: the `while day < end` below already yields 0 for a
        # from_day at or past the end, so this is a cheap exit and not a
        # separate rule. A mutation relaxing it to `>` changes nothing.
        if from_day >= end or per <= 0:
            return 0
        weekends = set()
        day = from_day
        while day < end:
            if day.weekday() >= 5:                       # Saturday or Sunday
                # Name the weekend by its Saturday, so Sat and the next Sun agree.
                weekends.add(day - timedelta(days=day.weekday() - 5))
            day += timedelta(days=1)
        return len(weekends) * per

    def _happy_hour_token_verdict(self, session_start_local, now_local=None):
        """(worth_joining, reason) for a session starting on `session_start_local`.

        v5.107.0, and the reason CliveS asked for it, 15-09-2026:

            "I intend to leave the free hour slots until we have a low solar day as
             topping up when solar is good defeats the object, it takes 2 tokens for
             1 happy hour and these need to be used by 1 november so can you make
             sure i do not have a saving session if the number of tokens would be
             too many to use before the 1 november"

        A token that cannot become an hour he will use is not a benefit, and the
        session that earns it spends battery and exports units at a net loss. So the
        guard is on JOINING: refuse before committing the account, because the join
        is one-way.

        Three ways a token can be worthless, checked in order of certainty:

        1. The scheme has ended. Nothing to weigh.
        2. This session settles too late to reach a remaining weekend. Octopus take
           about three working days to score a session and bookings open late in the
           week, so a session in the last days of October earns nothing usable.
        3. He already holds enough tokens for every hour he expects to use.

        (3) is the one that needs his judgement rather than arithmetic. The slot
        COUNT is derivable — seven weekends at two hours each is fourteen — but he
        has said he will only spend an hour on a low-solar weekend day, and how many
        of those there will be between now and November is weather, not a rule. So
        `happyHourUsableHours` lets him state the realistic figure, and the derived
        maximum is the default, which makes this leg inert until he sets one. It is
        capped at the derived maximum either way: he cannot use more hours than
        exist, whatever he types.
        """
        today  = now_local or date.today()
        need   = self._happy_hour_tokens_required()
        tokens = self.store.get("happy_hour_tokens")

        if today >= HAPPY_HOUR_SCHEME_END:
            return False, ("the Weekend Happy Hour scheme ended on "
                           f"{HAPPY_HOUR_SCHEME_END.strftime('%-d %B %Y')}, so a token "
                           "earned now buys nothing")

        credited = session_start_local + timedelta(days=HAPPY_HOUR_SETTLEMENT_DAYS)
        if self.happy_hour_slots_left(credited) <= 0:
            return False, (f"Octopus take about {HAPPY_HOUR_SETTLEMENT_DAYS} days to score "
                           "a session, so this one's token would arrive too late to book "
                           "a weekend hour before the scheme ends")

        # A token cost of 0 means the accrual rule is switched off; there is then no
        # arithmetic to do and no grounds to refuse.
        if need <= 0 or tokens is None:
            return True, ""

        derived = self.happy_hour_slots_left(today)
        stated  = _as_float(self.pluginPrefs.get("happyHourUsableHours"), 0.0)
        budget  = int(min(derived, stated)) if stated > 0 else derived
        if tokens >= budget * need:
            hours_held = int(tokens // need)
            return False, (f"you already hold {tokens} token{'' if tokens == 1 else 's'} "
                           f"({hours_held} free hour{'' if hours_held == 1 else 's'}) and "
                           f"only {budget} hour{'' if budget == 1 else 's'} can still be "
                           "used before the scheme ends, so another one would be wasted")
        return True, ""

    @staticmethod
    def _happy_hour_expiry_note(today=None):
        """" Use them by X or lose them", while that is still true and not before.

        v5.106.1. Octopus's own Weekend Happy Hours page (read 15-Sep-2026) says the
        scheme runs "until 1st November" and that you must "use them all by 1st
        November, or they'll disappear". A banked hour is not banked indefinitely, and
        a token earned in late October may have nowhere to go — which is worth knowing
        when the whole point of running a Power Down is the hour it pays for.

        Dated deliberately, so it stops saying itself once the date has passed rather
        than nagging for ever about a promotion that has ended. THE DATE IS A VENDOR
        PROMOTION AND WILL ROT: re-read the page rather than trusting this constant
        past it, and if Octopus extend the scheme this is the one line to change.
        """
        end = date(2026, 11, 1)
        now = today or date.today()
        if now >= end:
            return ""       # the scheme has ended, or been extended and nobody looked
        days = (end - now).days
        return ("  Use it by 1 November or it is lost"
                + (f" — {days} day{'' if days == 1 else 's'} left." if days <= 45 else "."))

    def _happy_hour_tokens_required(self):
        """How many tokens booking a Happy Hour costs.

        NOT available from the API and NOT derivable from the account history — measured
        03-Sep-2026, 24 successful turn-downs and one booking leave a balance of 1, which
        fits no simple arithmetic. So this is Octopus's published figure, held as a pref
        so a change on their side is one field to edit rather than a release. 0 disables
        the "not enough tokens" wording entirely.
        """
        try:
            return max(0, int(self.pluginPrefs.get("happyHourTokensRequired", 2)))
        except (TypeError, ValueError):
            return 2

    def _auto_join_saving_sessions(self, data, now_utc):
        """Opt in to announced TURN_DOWN sessions, if the owner asked us to.

        Gated on the `savingSessionAutoJoin` checkbox and OFF by default, like every
        other feature here that reaches outside the house. Joining is a commitment
        made on the owner's behalf against their energy account, and it is ONE-WAY:
        the Kraken schema has joinSavingSessionsEvent and no counterpart to leave
        (introspected 10-Sep-2026). It is defensible only because failing a session
        carries no penalty — this account has 11 fails against 24 successes and lost
        nothing by them.

        TURN_DOWN only, and that is the same guard the export branch and the window
        cache already carry. A TURN_UP session wants MORE consumption, so opting in
        commits to something the battery cannot help with; a WEEKEND_HAPPY_HOUR is
        not an opt-in at all but a BOOKING that spends a scarce token, and spending
        one automatically is not a decision to take out of the owner's hands. Both
        arrive in this same feed. An absent or unrecognised direction is never joined.

        Mutates `data` in place on success so the alert below and the window cache
        both see the new state immediately. That writes through to the API layer's
        30-minute cache, which is deliberate: the account really did change, and a
        cached "not joined" would otherwise outlive the fact.
        """
        if not _as_bool(self.pluginPrefs.get("savingSessionAutoJoin"), False):
            return
        if not self.octopus:
            return

        refused = {str(x) for x in (self.store.get("saving_sessions_join_refused") or [])}
        joined_any = False

        for event in data.get("events") or []:
            code     = event.get("code")
            start_at = event.get("start_at")
            if not code or start_at is None:
                continue
            if event.get("joined"):
                continue                      # already in — nothing to do
            if not self._saving_session_for_us(event):
                continue                      # not open to our region; say nothing
            if event.get("direction") != SAVING_SESSION_TURN_DOWN:
                continue                      # see the docstring: turn-downs only
            if start_at <= now_utc:
                continue                      # started or past; joining earns nothing
            if str(code) in refused:
                continue                      # Octopus already said no, permanently
            # A full event cannot be joined, and trying earns a refusal that would
            # then be remembered as permanent. Skip it and say so instead.
            if event.get("capacity") == "FULL":
                log(f"[SavingSessions] {code} is full, so it cannot be joined.",
                    level="WARNING")
                refused.add(str(code))
                continue

            # v5.107.0: would the token this earns ever be usable? A Power Down is
            # run for the Weekend Happy Hour it pays towards, and an hour that
            # cannot be spent before the scheme ends is worth nothing — while the
            # session itself spends battery and sells units at a net loss. Checked
            # BEFORE the join because the join is one-way.
            #
            # Deliberately NOT added to `refused`: that set is for permanent
            # refusals by Octopus, and this verdict changes as tokens are spent and
            # as the calendar moves. Re-asked every poll instead.
            _tz    = _london_tz()
            _start = (start_at.astimezone(_tz) if _tz else start_at).date()
            # `now_utc` is the caller's clock, already passed in. Reading date.today()
            # here instead would make every test of this guard depend on the real
            # calendar and start failing of its own accord on 1 November 2026.
            _today = (now_utc.astimezone(_tz) if _tz else now_utc).date()
            _worth, _why = self._happy_hour_token_verdict(_start, now_local=_today)
            if not _worth:
                if self.store.get("saving_sessions_budget_logged") != str(code):
                    self.store["saving_sessions_budget_logged"] = str(code)
                    log(f"[SavingSessions] Not joining {code} — {_why}.")
                continue

            try:
                result = self.octopus.join_saving_session_event(code)
            except Exception as exc:                                # noqa: BLE001
                log(f"[SavingSessions] joining {code} raised: {exc}", level="WARNING")
                continue

            if result.get("ok"):
                event["joined"] = True
                joined_any = True
                if not result.get("already"):
                    tz    = _london_tz()
                    start = start_at.astimezone(tz) if tz else start_at
                    log(f"[SavingSessions] Opted in to {code} "
                        f"({start.strftime('%a %d %b, %H:%M')}, "
                        f"about {_points_to_pence(event.get('reward_per_kwh_points', 0)):.0f}p/kWh "
                        f"bonus) automatically.")
            elif result.get("permanent"):
                refused.add(str(code))
                if "region" in str(result.get("reason", "")).lower():
                    # Octopus says it is not for us, although the event's own region
                    # list said it was. Remember it so nothing announces it, and say
                    # once that the two disagree — the region numbering may be wrong.
                    not_ours = [str(x) for x in (self.store.get("saving_sessions_not_our_region") or [])]
                    if str(code) not in not_ours:
                        self.store["saving_sessions_not_our_region"] = (not_ours + [str(code)])[-200:]
                    log(f"[SavingSessions] {code} is not open to this account's region, "
                        f"although its region list suggested it was — check the Octopus "
                        f"region setting: {result.get('reason')}", level="WARNING")
                else:
                    log(f"[SavingSessions] Could not opt in to {code} and will not try "
                        f"again: {result.get('reason')}", level="WARNING")
            else:
                # Transient — a network blip, a rate limit, an expired token. NOT
                # recorded, so the next poll tries again.
                log(f"[SavingSessions] Opting in to {code} failed, will retry: "
                    f"{result.get('reason')}", level="WARNING")

        if refused != {str(x) for x in (self.store.get("saving_sessions_join_refused") or [])}:
            self.store["saving_sessions_join_refused"] = list(refused)[-200:]
            self._save_accumulators()
        if joined_any and not _as_bool(self.pluginPrefs.get("savingSessionExport"), False):
            # Worth one line: opted in is not the same as being driven, and the two
            # are separate checkboxes on purpose. Without this the owner can be
            # joined to a session nothing will act on and have no way to know.
            log("[SavingSessions] Opted in, but 'drive the battery' is switched off, "
                "so the export will not be driven for it.", level="WARNING")

    def _saving_session_for_us(self, event):
        """False only when we KNOW this session is not open to this account's region.

        CliveS, 17-Sep-2026: "if it does not concern us then we don't need to be
        told." Octopus run sessions for a subset of regions and the feed carries all
        of them; on 17-Sep a 6pm session for regions 8-12 produced a "NOT OPTED IN,
        join it" push, a warning, and an amber Dashboards chip for something this
        account could not join. Known when the event lists its regions and ours is
        not among them, or when Octopus has already refused a join on region grounds.
        Unknown (no region list, region not configured) counts as ours, so a feed
        change can never silence a real session.
        """
        code = str(event.get("code") or "")
        if code and code in {str(x) for x in (self.store.get("saving_sessions_not_our_region") or [])}:
            return False
        regions = event.get("target_regions")
        ours = getattr(self.octopus, "saving_session_region_id", None) if self.octopus else None
        if not regions or ours is None:
            return True
        return ours in regions

    def _check_saving_sessions(self):
        """Notify on newly-announced Octopus Saving Sessions events.

        Phase 1 — visibility only, no dispatch change. Octopus pays extra export
        during a session at the Saving Sessions incentive rate ON TOP OF the normal
        Outgoing export rate (confirmed against the account 03-Sep-2026: a session
        with 7.99 kWh exported earned both the normal ~96p export payment AND a
        120-point/15p Octopoints bonus for the 7.27 kWh above baseline). This just
        surfaces the announcement so CliveS can glance at the battery — it does not
        try to hold back or time discharge, which would need to arbitrate against
        the Axle VPP and bank-first export hold (deliberately deferred, see
        SigenEnergyManager CLAUDE.md).

        Cheap by design: hourly poll, one Pushover per event ever (deduped via
        saving_sessions_notified, persisted so a restart can't re-send), silent
        when nothing new. A failed poll is silent too — get_saving_sessions()
        serves the last good value or None, and this simply tries again next hour.
        """
        if not self.octopus:
            return
        try:
            data = self.octopus.get_saving_sessions()
        except Exception as exc:
            log(f"[SavingSessions] get_saving_sessions() raised: {exc}", level="WARNING")
            return
        if not data:
            return   # fetch failed (get_saving_sessions has already warned) or no account
        # Carry the balance forward on every successful fetch. Written before the
        # has_joined early-return below, because the token count is just as true for an
        # account that has not joined the campaign.
        self.store["happy_hour_tokens"] = data.get("token_balance")
        if not data.get("has_joined"):
            # A REAL answer now, not the "couldn't tell" case — that returns None above and
            # warns from the API layer. Say it once per plugin start: an account that has
            # not joined the campaign earns nothing from a session, and silence about that
            # is indistinguishable from the feature being broken (which it was, in v5.80.0).
            if not self.store.get("saving_sessions_unjoined_logged"):
                self.store["saving_sessions_unjoined_logged"] = True
                log("[SavingSessions] This Octopus account has not joined the Saving "
                    "Sessions campaign, so no session alerts will fire. Join it in the "
                    "Octopus app under Octoplus if you want them.")
            return

        now_utc  = datetime.now(timezone.utc)
        # Opt in BEFORE the alert loop below, and before the window cache is built
        # at the end of this method. Both orderings matter:
        #   * the Pushover would otherwise tell the owner to "join it in the Octopus
        #     app" for a session this method has just joined them to, which is advice
        #     that is not merely useless but wrong;
        #   * the manager's window cache admits joined events only, so building it
        #     first would leave a session we just joined un-armed until the NEXT poll
        #     — up to an hour, and the session may not last that long.
        self._auto_join_saving_sessions(data, now_utc)
        # Normalised to str on BOTH sides. GraphQL's ID type is specified to
        # serialise as a STRING, and this payload happens to return an int — so
        # comparing the raw value against a persisted set is one API tweak away
        # from a dedupe that never matches and re-announces every hour. Also
        # migrates a set persisted as ints by an earlier version.
        notified = {str(x) for x in (self.store.get("saving_sessions_notified") or [])}
        new_ids  = []

        for event in data.get("events") or []:
            event_id = str(event.get("id")) if event.get("id") is not None else None
            start_at = event.get("start_at")
            if not event_id or start_at is None or event_id in notified:
                continue
            if not self._saving_session_for_us(event):
                continue                      # another region's session: not our news
            if start_at <= now_utc:
                # Already started or in the past — either we've seen it before (and
                # it's in `notified`) or the plugin only just started watching after
                # it began. Either way a push now would be pointless or late.
                continue

            tz          = _london_tz()
            start_local = start_at.astimezone(tz) if tz else start_at
            end_at      = event.get("end_at")
            end_local   = (end_at.astimezone(tz) if (end_at and tz) else end_at)
            points      = event.get("reward_per_kwh_points", 0)
            duration_h  = ((end_at - start_at).total_seconds() / 3600.0) if end_at else None

            when = start_local.strftime("%a %d %b, %H:%M")
            if end_local:
                when += f"-{end_local.strftime('%H:%M')}"
            # Being JOINED is the difference between earning and not earning.
            # MEASURED 03-Sep-2026: campaign membership does NOT enrol you in each
            # event — this account read hasJoinedCampaign=True while every one of
            # the 17 sessions since 16-Aug was un-joined, so they were missed
            # silently. An alert that omits this reads as "you're covered", which
            # is the opposite of the truth, so it leads the message when it matters.
            joined = bool(event.get("joined"))
            direction = event.get("direction") or "UNKNOWN"
            turn_down = direction == SAVING_SESSION_TURN_DOWN
            happy_hour = direction == SAVING_SESSION_HAPPY_HOUR

            if happy_hour:
                # A Happy Hour is BOOKED, not opted into, and booking costs tokens.
                # Telling someone to "opt in to earn" on a slot they cannot book is
                # noise — it happened on 03-Sep-2026, four pushes for four slots
                # against a balance that could not pay for one. So the message says
                # what the reader can actually DO, and nothing else.
                body = _ascii_plain(self._happy_hour_alert_body(when, duration_h, event))
                title = ("Octopus Happy Hour booked" if joined
                         else "Octopus Happy Hour available")
            else:
                # v5.106.1 — THE POINTS ARE NOT THE POINT. CliveS, 15-Sep-2026:
                # "The reason i export at an Octopus Saving Session is it gives me
                # a 1 hour free token, 2 needed, for an hour of free electricity on
                # a Saturday or Sunday, the export amount I earn is not the reason
                # for the export, it is secondary."
                #
                # Octopus agree: "if you manage to use less electricity than you'd
                # normally use in two Power Down sessions, you'll earn one Weekend
                # Happy Hour" (octopus.energy help article, read 15-Sep-2026). So a
                # session is worth HALF A FREE HOUR, and the money is change. v5.106.0
                # led on the pence and closed by comparing it unfavourably with an
                # Axle event, which is true about the cash and reads as "not worth
                # bothering with" about the thing that actually matters.
                #
                # It also means SUCCEEDING matters more than exporting a lot: a
                # session that misses the baseline earns no token at all, however
                # many units went out.
                # v5.109.4 — PLAIN ENGLISH, AND ONLY ASK FOR WHAT HE CAN DO. CliveS,
                # 17-Sep-2026: the 12:50 push said "NOT OPTED IN — join it in the
                # Octopus app" about a session Octopus had just refused to let this
                # account join. The standing notification rules also want sentences,
                # people's times ("6pm"), no em-dashes and ASCII only.
                bonus_p  = _points_to_pence(points)
                need     = self._happy_hour_tokens_required()
                tokens   = self.store.get("happy_hour_tokens")
                auto_on  = _as_bool(self.pluginPrefs.get("savingSessionAutoJoin"), False)
                code     = str(event.get("code") or "")
                refused  = code and code in {str(x) for x in
                                             (self.store.get("saving_sessions_join_refused") or [])}
                skip_why = ""
                if turn_down and not joined and auto_on:
                    _sd = (start_local.date() if hasattr(start_local, "date") else start_local)
                    _ok, _why = self._happy_hour_token_verdict(_sd)
                    skip_why = "" if _ok else _why
                if not joined and (refused or event.get("capacity") == "FULL"):
                    # Cannot be joined at all, so it does not concern us: no push.
                    log(f"[SavingSessions] {code or event_id} ({when}) cannot be joined "
                        f"by this account, so it is not announced.")
                    new_ids.append(event_id)
                    continue
                day   = _session_day_words(start_local, now_utc)
                span  = _session_span_words(start_local, end_local)
                parts = [f"Octopus have announced a Saving Session {day} {span}."]
                if not turn_down:
                    if direction == "TURN_UP":
                        parts.append("It is a Power Up session, which wants you to use "
                                     "more rather than less, so the battery is not run "
                                     "for it.")
                    else:
                        parts.append("Octopus did not say which way this session runs, "
                                     "so the battery is not run for it.")
                    title = f"Saving Session {day}: not one for the battery"
                elif joined:
                    if _as_bool(self.pluginPrefs.get("savingSessionExport"), False):
                        parts.append("You are in it, and the battery will export for it.")
                    else:
                        parts.append("You are in it, but exporting for sessions is "
                                     "switched off in the plugin, so the battery will not "
                                     "be run for it.")
                    title = f"Saving Session {day}: you are in"
                elif skip_why:
                    parts.append(f"The plugin has not joined it, because {skip_why}.")
                    title = f"Saving Session {day}: not worth joining"
                elif auto_on:
                    parts.append("The plugin could not join it just now and will try "
                                 "again within the hour.")
                    title = f"Saving Session {day}: joining shortly"
                else:
                    parts.append("You are not in it yet. Join it in the Octopus app, or it "
                                 "earns nothing and the battery will not be run for it.")
                    title = f"Saving Session {day}: join it in the Octopus app"
                if turn_down and not skip_why:
                    parts.append("Beating your usual use in two of these earns a free hour "
                                 "of electricity at the weekend, so winning it matters more "
                                 "than how much goes out.")
                    # Only say where he stands if the API actually told us.
                    if tokens is not None and need > 0:
                        plural = "" if tokens == 1 else "s"
                        if tokens + 1 >= need:
                            parts.append(f"Octopus report {tokens} token{plural}, so "
                                         "winning this one should unlock a free hour.")
                        else:
                            parts.append(f"Octopus report {tokens} token{plural} and an "
                                         f"hour costs {need}, so this one is a step "
                                         "towards the next.")
                    parts.append(f"The Octopoints are change on top, about {bonus_p:.0f}p "
                                 "a unit added to what the export itself earns.")
                body = _ascii_plain(" ".join(parts))
            self._send_pushover(title, body, priority="0")
            log(f"[SavingSessions] New event {event.get('code') or event_id}: {when}, "
                f"{direction}, {points} pts/kWh (~{_points_to_pence(points):.1f}p/kWh), "
                f"opted in: {'YES' if joined else 'NO'}"
                + ("" if turn_down else " — battery NOT driven (not a turn-down)"),
                level="WARNING" if title.endswith("join it in the Octopus app") else "INFO")
            new_ids.append(event_id)

        # Cache the JOINED upcoming/live windows for the manager cycle. The manager
        # runs every 60s and must never do network I/O (one slow call there stalls
        # every action callback behind it), so the hourly poll leaves it a small
        # plain-data list and _saving_session_window() reads only that.
        upcoming = sorted(e["start_at"] for e in (data.get("events") or [])
                          if e.get("start_at") and e["start_at"] > now_utc
                          and self._saving_session_for_us(e))
        # Deliberately NOT filtered on `joined` — the whole point is to be polling
        # often enough to notice an opt-in made shortly before the window.
        self.store["saving_sessions_next_start"] = (
            upcoming[0].isoformat() if upcoming else "")

        horizon = now_utc + timedelta(days=2)
        self.store["saving_sessions_windows"] = [
            {"id": e.get("id"),
             "start": e["start_at"].isoformat(),
             "end":   e["end_at"].isoformat(),
             "points": e.get("reward_per_kwh_points", 0),
             "direction": e.get("direction")}
            for e in (data.get("events") or [])
            if e.get("joined") and e.get("end_at") and e["end_at"] > now_utc
            and e["start_at"] < horizon
            # ONLY a turn-down is earned by exporting. A TURN_UP session wants MORE
            # consumption and a WEEKEND_HAPPY_HOUR is free power you want to USE —
            # exporting the battery into either is backwards, and both arrive in
            # THIS SAME feed (12 of 62 events on this account are happy hours). An
            # absent or unrecognised direction is never treated as a turn-down.
            # Same class as the Axle import/export guard added in v5.57.0.
            # Both drivable directions are cached WITH their direction; each reader
            # filters to the one it drives. One cache means the freshness logic
            # cannot diverge between the two features.
            and e.get("direction") in (SAVING_SESSION_TURN_DOWN, SAVING_SESSION_HAPPY_HOUR)
        ]

        # The DISPLAY list: no joined filter and no direction filter, because a
        # session you are not in and a Power Up you cannot help with are both
        # things a person needs told about. A longer horizon than the manager's
        # two days so a weekend Happy Hour shows when it is announced.
        display_horizon = now_utc + timedelta(days=8)
        self.store["saving_sessions_upcoming"] = [
            {"id":        e.get("id"),
             "start":     e["start_at"].isoformat(),
             "end":       e["end_at"].isoformat(),
             "points":    e.get("reward_per_kwh_points", 0),
             "direction": e.get("direction"),
             "joined":    bool(e.get("joined")),
             "capacity":  e.get("capacity")}
            for e in (data.get("events") or [])
            if e.get("start_at") and e.get("end_at") and e["end_at"] > now_utc
            and e["start_at"] < display_horizon
            # ...but not another region's session: that is not news for this house.
            and self._saving_session_for_us(e)
        ]

        if new_ids:
            notified.update(new_ids)
            # Cap it — this only needs to remember enough to dedupe recent
            # announcements, not the account's whole history.
            self.store["saving_sessions_notified"] = list(notified)[-200:]
            self._save_accumulators()   # persist now so a restart can't re-send these

    def _saving_sessions_interval(self):
        """Poll cadence: hourly, or every 10 min once a session is imminent.

        Mirrors _vpp_poll_interval. The reason it matters is OPT-IN: joining a
        session is a tap in the Octopus app that the owner may make minutes before
        the window opens, and the `joined` flag is what gates the export. On the
        flat hourly cadence an opt-in at 17:45 would not be seen until after an
        18:00 session had started. Reads only the cached next-start, never the
        network.
        """
        nxt = self.store.get("saving_sessions_next_start")
        if not nxt:
            return SAVING_SESSIONS_INTERVAL
        try:
            start = datetime.fromisoformat(nxt)
        except (TypeError, ValueError):
            return SAVING_SESSIONS_INTERVAL
        hours = (start - datetime.now(timezone.utc)).total_seconds() / 3600.0
        if 0 <= hours <= SAVING_SESSIONS_SOON_HOURS:
            return SAVING_SESSIONS_SOON_INTERVAL
        return SAVING_SESSIONS_INTERVAL

    def _end_happy_hour_import(self, why):
        """Stop a Happy Hour import and record what it banked.

        Latches on the FLAG, not the clock, so this is equally the path for the
        window ending, the battery reaching target, and the overrun backstop.
        The hand-back is CONFIRMED — an unconfirmed write is what left a VPP
        window exporting past its end (v5.64.0) — and an unconfirmed one here
        would leave the house BUYING electricity after the free hour, which is
        the more expensive direction to get wrong.
        """
        anchor = self.store.get("happy_hour_anchor_kwh")
        banked = None
        if anchor is not None:
            banked = max(0.0, float(self.store.get("grid_import_daily_kwh", 0.0)) - float(anchor))
            self.store["happy_hour_free_kwh"] = round(banked, 2)
        self.store["happy_hour_import_active"] = False
        self.store["happy_hour_anchor_kwh"]    = None
        self.store["import_active"]            = False
        self.store["import_target_soc"]        = 0.0

        if self.modbus and self.modbus.connected:
            if not self.modbus.set_self_consumption():
                log("[Manager] Happy Hour hand-back to Self Consumption was NOT "
                    "confirmed — retrying on the next tick", level="WARNING")
                self.store["vpp_handback_pending"] = True
            self._restore_import_cutoff()
        else:
            log("[Manager] Happy Hour ended with the inverter unreachable — the "
                "hand-back will be re-asserted when it returns", level="WARNING")
            self.store["vpp_handback_pending"] = True

        banked_str = f"{banked:.2f} kWh banked free" if banked is not None else "amount unknown"
        log(f"[Manager] Happy Hour import ended ({why}) — {banked_str}")
        self._save_accumulators()

    def _check_happy_hour_overrun(self):
        """Second, independent guard on an over-running Happy Hour import.

        Mirrors _check_vpp_overrun (v5.62.0), and for the same reason: the
        primary end path is one path, and one path ending a window is one path
        too few. Sharing no dependency with it — no Octopus call, no prefs, just
        the stored window end — is the whole point. Importing past a free hour
        means BUYING at 25p, so the exposure here is real money.
        """
        if not self.store.get("happy_hour_import_active"):
            return
        try:
            w = self._happy_hour_window()
            if w is not None:
                return                      # still inside the booked window
            # No live window but the flag is set: either it ended and the primary
            # path missed it, or the pref was switched off mid-window. Either way,
            # stop importing.
            log("[Manager] Happy Hour import still running with no live window — "
                "force-ending it", level="WARNING")
            self._end_happy_hour_import("overrun backstop: no live window")
        except Exception as exc:                                # noqa: BLE001
            log(f"[Manager] Happy Hour overrun check failed: {exc}", level="WARNING")

    def _window_of_direction(self, direction, pref, now_utc=None):
        """The live cached window of one direction, or None. Pure cache read.

        Shared by both features so the freshness and malformed-row handling can
        only ever be written once. `pref` is the checkbox that gates driving the
        battery for that direction — read via as_bool, because Indigo stores a
        saved checkbox as the STRING "false" and bare bool() calls that True.
        """
        if not _as_bool(self.pluginPrefs.get(pref), False):
            return None
        now_utc = now_utc or datetime.now(timezone.utc)
        for w in self.store.get("saving_sessions_windows") or []:
            if w.get("direction") != direction:
                continue
            try:
                start = datetime.fromisoformat(w["start"])
                end   = datetime.fromisoformat(w["end"])
            except (KeyError, TypeError, ValueError):
                continue        # malformed row — skip it, never guess a window
            if start <= now_utc < end:
                hours = max(0.0, (end - start).total_seconds() / 3600.0)
                return dict(w, hours=hours)
        return None

    def _happy_hour_window(self, now_utc=None):
        """A BOOKED Octopus Weekend Happy Hour live right now, or None.

        Booked is the whole point: Octopus offers four 1-hour slots each Sunday
        and only the one the owner reserved comes back joined — the cache admits
        joined events only, so the three unbooked siblings never reach here.
        """
        return self._window_of_direction(
            SAVING_SESSION_HAPPY_HOUR, "happyHourImport", now_utc)

    def _saving_session_window(self, now_utc=None):
        """The JOINED Saving Session window live right now, or None.

        Pure read of the cache `_check_saving_sessions` leaves behind — no network,
        no Octopus call, safe to run on the 60s manager cycle.

        Returns {"id", "start", "end", "points", "hours"} or None. Gated on the
        `savingSessionExport` pref: this drives the battery, so it is OFF unless
        the owner turned it on.
        """
        return self._window_of_direction(
            SAVING_SESSION_TURN_DOWN, "savingSessionExport", now_utc)

    @staticmethod
    def _rate_str(value):
        """A rate for a device state: blank when there isn't one, never "None".

        `str(d.get(k, ""))` returns the STRING "None" whenever the key exists
        holding None — which is every unmonitored rate — and a control page then
        displays the word None where a price should be. Blank is what the other
        fields on this device already use for absent, so absent reads as absent.
        """
        return "" if value is None else str(value)

    @staticmethod
    def _rates_for_tariff(tariff_key, rates):
        """(today_p, tomorrow_p) for the ACTIVE tariff — the one owner of that choice.

        Extracted in v5.98.1. _build_tariff_data already selected per tariff and
        carried a comment warning that falling through to the Tracker branch
        "would display a price from a tariff we are not on" — and the status log
        line 40 lines away did exactly that, reading monitored["tracker"] whatever
        the tariff was. Caught the first time the Agile override was switched on:
        it printed "today: Nonep" because the Tracker bucket is not filled when
        Tracker is not the active tariff. One intent, one function.
        """
        if tariff_key == TARIFF_FLEXIBLE:
            return rates.get(TARIFF_FLEXIBLE, {}).get("today_p"), None
        if tariff_key == TARIFF_AGILE:
            # Agile has 48 prices a day, so "today's rate" is the slot in force right
            # now (octopus_api fills it). There is no single tomorrow rate.
            return rates.get(TARIFF_AGILE, {}).get("today_p"), None
        if tariff_key in (TARIFF_GO, TARIFF_IGO, TARIFF_FLUX, TARIFF_IFLUX):
            # A TIME-OF-USE tariff has no single "today's rate" either — it has
            # bands. These buckets hold cheap_p / standard_p / peak_p and the
            # local window strings, and NONE of them is called today_p, so the
            # Tracker fall-through below returned None for every one: live on
            # Flux the status line and the tariff device both read "Nonep".
            # The rate in force NOW is the honest answer, the same choice Agile
            # already makes. No tomorrow figure: the bands repeat daily and a
            # second number would only invite the question of which band it is.
            return _tou_rate_now_p(rates.get(tariff_key, {})), None
        tracker = rates.get(TARIFF_TRACKER, {})
        return tracker.get("today_p"), tracker.get("tomorrow_p")

    @staticmethod
    def _tariff_line_changed(tariff_key, today_rate, last_key, last_rate):
        """Should the status line speak? Lifted out in v5.98.1 so a test can drive it.

        On Agile the price moves every half hour BY DESIGN, so gating on it would
        put 48 lines a day in the event log. The tariff key alone gates it there;
        the live slot price is on the device and the dashboard. Left inline in
        _refresh_rates this was reachable only by a structural assertion, and a
        mutation walked straight through it.
        """
        if tariff_key != last_key:
            return True
        if tariff_key == TARIFF_AGILE:
            return False
        return today_rate != last_rate

    def _build_tariff_data(self):
        """Build a TariffData object from the latest Octopus rates."""
        rates       = self.latest_rates_data
        tariff_info = rates.get("tariff_info", {})
        tariff_key  = tariff_info.get("tariff_key", TARIFF_TRACKER)

        tou = rates.get(tariff_key, {})          # cheap window data for Go/Flux/iGo/iFlux
        today_rate_p, tomorrow_rate_p = self._rates_for_tariff(tariff_key, rates)

        return TariffData(
            tariff_key      = tariff_key,
            today_rate_p    = today_rate_p,
            tomorrow_rate_p = tomorrow_rate_p,
            cheap_start     = tou.get("cheap_start"),
            cheap_end       = tou.get("cheap_end"),
            cheap_rate_p    = tou.get("cheap_p"),
            agile_slots     = rates.get("agile_slots", []),
        )

    def _act_on_decision(self, decision):
        """Translate a Decision into Modbus writes."""
        if not self.modbus:
            return

        action      = decision.action
        prev_import = self.store["import_active"]
        prev_export = self.store["export_active"]

        # ── Retract a stale scheduled import (v5.65.0) ──────────────────────
        # ACTION_SCHEDULE_IMPORT armed a stored time that NOTHING ever cleared
        # except the firing itself. The manager re-emits SCHEDULE_IMPORT on every
        # tick while it still wants the import, so any other action means it has
        # changed its mind — but the stored time survived, fired regardless with
        # no fresh import_needed check, and the anti-oscillation guard in the
        # SELF_CONSUMPTION branch then HELD the unwanted import until the stored
        # target SOC was reached. On Tracker the midnight-deferral path can arm
        # this ~8 hours ahead, which is a long time for the forecast to improve.
        # Cost when it fires: 10 kW of grid import nobody wanted, against the
        # self-sufficiency KPI, with one INFO line to show for it.
        #
        # Deliberately does NOT retract while an import is actually running —
        # that is the STOP_IMPORT branch's job, and clearing here would strand
        # the in-flight import's bookkeeping.
        if (action != ACTION_SCHEDULE_IMPORT
                and not prev_import
                and self.store.get("import_scheduled_time") is not None):
            log(f"[Manager] Scheduled import retracted — manager now wants "
                f"'{action}' instead of the import it had queued for "
                f"{_local_time(self.store['import_scheduled_time'])}")
            self.store["import_scheduled_time"]   = None
            self.store["import_scheduled_logged"] = False

        if action == ACTION_START_IMPORT:
            if not prev_import:
                log(f"[Manager] Starting grid import: {decision.reason}")
                power_w = min(decision.power_watts or 10000,
                              int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000))
                # Hardware backstop: charge cutoff a little above the software
                # target so a plugin crash / Modbus outage mid-import cannot
                # grid-charge unbounded toward 100% (the export path has the
                # symmetric set_discharge_cutoff floor). +3% headroom keeps the
                # software SOC compare as the primary stop.
                cutoff = min((decision.target_soc_pct or 100.0) + 3.0, 100.0)
                if self.modbus.force_charge(power_w, cutoff_soc=cutoff):
                    self.store["import_active"]     = True
                    self.store["import_target_soc"] = decision.target_soc_pct
                    self.store["export_active"]     = False
                    self.store["had_import_today"]  = True   # daily history flag
                    self._set_import_cutoff(cutoff)
                    self._trigger_event("emergencyImportTriggered")

        # There is deliberately NO ACTION_STOP_IMPORT branch. battery_manager has not
        # returned that action since the v4.0 sufficiency model, and the import
        # teardown lives in the SELF_CONSUMPTION branch below (target reached) and
        # the unconditional target check at the end of this method. A dead copy sat
        # here until v5.100.0 and read as the place imports were ended (10-09-2026
        # review: "the teardown sits in a branch nothing can reach").

        elif action == ACTION_SCHEDULE_IMPORT:
            # v5.100.0: never arm while an import is RUNNING. The planner's window
            # used to exclude the slot in progress, so mid-import it re-emitted
            # SCHEDULE for the NEXT half-hour; stored, that time outlived the import
            # (nothing clears it on completion) and _check_scheduled_import fired a
            # second charge seconds after the first reached target. The running
            # import is the plan; the manager re-plans the moment it ends.
            if not prev_import:
                # Store the scheduled time - checked in _check_scheduled_import
                self.store["import_scheduled_time"] = decision.scheduled_time
                self.store["import_target_soc"]     = decision.target_soc_pct
                if self.store.get("import_scheduled_logged") != str(decision.scheduled_time):
                    log(f"[Manager] Import scheduled: {decision.reason}")
                    self.store["import_scheduled_logged"] = str(decision.scheduled_time)

        elif action == ACTION_START_EXPORT:
            # Idempotent: only call night_export if not already exporting
            if not prev_import and not prev_export:
                log(f"[Manager] Starting night export: {decision.reason}")
                inv_max_w = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
                # Flood prevention uses DNO export cap (decision.power_watts).
                # Legacy night export falls back to full inverter capacity (inv_max_w).
                export_w = decision.power_watts if decision.power_watts > 0 else inv_max_w
                if self.modbus.night_export(export_w):
                    # Set hardware floor so battery stops automatically at target SOC.
                    # Plugin resets this cutoff on return to self-consumption.
                    if decision.target_soc_pct > 0:
                        # Never below the policy floor. Flood prevention drains the
                        # battery to make room for tomorrow's sun, which is worth
                        # money; the reserve is there for a power cut, which is
                        # not about money. Read the floor BEFORE recording the
                        # flood target, or the target would be compared with
                        # itself.
                        floor  = self._policy_discharge_floor_pct()
                        target = max(float(decision.target_soc_pct), floor)
                        self.modbus.set_discharge_cutoff(target)
                        self._set_flood_prev_target(target)
                        log(f"[Manager] Discharge cutoff set to {target:.0f}% "
                            f"(flood prevention floor)")
                        self._trigger_event("floodPreventionStarted")
                    self.store["export_active"]      = True
                    self.store["export_count_today"] = (
                        self.store.get("export_count_today", 0) + 1
                    )
                    self._trigger_event("exportStarted")

        elif action == ACTION_VPP_EXPORT:
            # VPP event window: self-drive the export, re-evaluated each tick by
            # _drive_vpp_export() which picks bank-surplus (mode 0x02 + charge cap,
            # daytime with ample PV) or discharge (0x05/0x06) from live PV vs the
            # export target. The discharge floor was set at pre-charge (next-day
            # reserve) — leave it. Axle settle on the meter so this counts
            # identically; their own dispatch is ignored.
            if not prev_export:
                log(f"[Manager] VPP export — {decision.reason}")
            self._drive_vpp_export()

        elif action == ACTION_SAVING_SESSION:
            # Octopus Saving Session: drive the export exactly the way a VPP window
            # is driven — same proven path, same live PV vs export-target choice
            # between banking surplus and discharging. The manager re-decides every
            # cycle, so if the dawn projection turns against us mid-window the very
            # next decision drops out of this branch and the export stands down.
            if not self.store.get("saving_session_export_active"):
                log(f"[Manager] Saving Session export — {decision.reason}")
                self.store["saving_session_export_active"] = True
                # Claim the driver's sub-mode state, exactly as _vpp_transition does
                # on entry to VPP_ACTIVE. Without this a session inherits whatever
                # sub-mode the LAST driven window left behind, and _drive_vpp_export
                # only writes the mode register on a sub-mode CHANGE — so a session
                # following another session (both "discharge") would write NOTHING
                # and "export" in whichever mode the previous hand-back left, which
                # is 0x02 self-consumption. No export, no error, no log line.
                # A VPP window is unaffected either way: _end_vpp_export and
                # _vpp_transition(VPP_ACTIVE) both already reset these.
                self.store["vpp_export_submode"]    = None
                self.store["vpp_bank_charge_cap_w"] = -1
            self._drive_vpp_export()

        elif action == ACTION_HAPPY_HOUR_IMPORT:
            # FREE electricity for one booked hour: grid-charge at inverter max.
            # Reuses force_charge (mode 0x03 + charge limit + the hardware charge
            # cutoff backstop) — the same proven path ACTION_START_IMPORT drives,
            # so a crash mid-window cannot leave it charging unbounded.
            if not self.store.get("happy_hour_import_active"):
                power_w = min(int(decision.power_watts or 10000),
                              int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000))
                target  = decision.target_soc_pct or 93.0
                cutoff  = min(float(target) + 3.0, 100.0)
                if self.modbus and self.modbus.connected and \
                        self.modbus.force_charge(power_w, cutoff_soc=cutoff):
                    # Anchor the free-kWh measurement on the cumulative import
                    # counter, captured ONCE at entry. A delta from one anchor is
                    # the only way a restart mid-window cannot double-count.
                    self.store["happy_hour_import_active"] = True
                    self.store["happy_hour_anchor_kwh"] = self.store.get(
                        "grid_import_daily_kwh", 0.0)
                    self.store["import_active"] = True
                    self.store["import_target_soc"] = float(target)
                    self._set_import_cutoff(cutoff)
                    self._save_accumulators()   # persist the anchor immediately
                    log(f"[Manager] Happy Hour import — {decision.reason}")
                else:
                    log("[Manager] Happy Hour import could not be started "
                        "(inverter unreachable or the charge command was refused)",
                        level="WARNING")

        elif action == ACTION_STOP_EXPORT:
            if prev_export:
                log("[Manager] Stopping night export - returning to self-consumption")
                self.modbus.set_self_consumption()
                # Clean up flood prevention cutoff if it was active
                flood_target = self.store.get("flood_prev_target_soc")
                if flood_target:
                    self._set_flood_prev_target(None)
                    # The policy floor, NOT the bare health floor: while the Flux
                    # strategy is armed it owns a reserve that must survive an
                    # export ending. _set_flood_prev_target first, so the floor is
                    # computed without the target being cleared.
                    floor = self._write_policy_floors()
                    log(f"[Manager] Discharge cutoff reset to {floor:.0f}% (policy floor)")
                    self._trigger_event("floodPreventionStopped")
                self.store["export_active"] = False
                self._trigger_event("exportStopped")

        elif action == ACTION_SOLAR_OVERFLOW:
            # Daytime charge cap: stay in mode 0x02, reduce HOLD_ESS_MAX_CHARGE.
            # PV keeps generating at full power; surplus flows to grid.
            cap_w     = decision.power_watts
            export_kw = decision.export_kw
            prev_cap  = self.store["solar_overflow_charge_cap_w"]

            if not self.store["solar_overflow_active"]:
                # First entry: ensure self-consumption mode, then apply cap.
                # set_self_consumption() resets charge limit to inv_max_w so we
                # must call set_charge_limit() immediately after.
                log(
                    f"[Manager] Solar overflow starting: target export {export_kw:.2f} kW, "
                    f"charge cap {cap_w}W"
                )
                indigo.server.log("  PV surplus flowing to grid")
                self.modbus.set_self_consumption()
                # If flood prevention was running overnight and dawn broke before
                # the target SOC was reached, reset the discharge cutoff register
                # now so it does not act as a hidden floor during daytime operation.
                flood_target = self.store.get("flood_prev_target_soc")
                if flood_target:
                    self._set_flood_prev_target(None)
                    floor = self._write_policy_floors()
                    log(f"[Manager] Discharge cutoff reset to {floor:.0f}% "
                        f"(flood prevention interrupted at dawn)")
                    self._trigger_event("floodPreventionStopped")
                self.modbus.set_charge_limit(cap_w, quiet=True)
                if prev_import:
                    # v5.100.0: an import interrupted by dawn kept its raised charge
                    # cutoff (40047) — this path cleared the flag without restoring
                    # the register, and _verify_ems_registers then re-asserted the
                    # import target as the PV charge ceiling for the rest of the day.
                    self._restore_import_cutoff()
                self.store["solar_overflow_active"]       = True
                self.store["solar_overflow_charge_cap_w"] = cap_w
                self.store["export_active"]               = False
                self.store["import_active"]               = False
            elif abs(prev_cap - cap_w) > SOLAR_OVERFLOW_CAP_DEADBAND_W:
                # Cap has shifted by more than deadband — update inverter register silently.
                # No log here: Indigo shows all indigo.server.log() calls regardless of
                # level= so any per-cap-change line floods the event log. The 15-min
                # heartbeat summary already reflects the current cap in its reason string.
                self.modbus.set_charge_limit(cap_w, quiet=True)
                self.store["solar_overflow_charge_cap_w"] = cap_w
            # else: cap within deadband — idempotent, no Modbus writes

        elif action == ACTION_SELF_CONSUMPTION:
            # Saving Session stand-down. The window ending is only ONE of the ways to
            # get here — the manager also drops out of the branch mid-window if the
            # dawn projection turns against us, and that must stand the export down
            # just as promptly. Latch on the flag, not on the clock.
            if self.store.get("happy_hour_import_active"):
                self._end_happy_hour_import("window ended or battery reached target")

            if self.store.get("saving_session_export_active"):
                self.store["saving_session_export_active"] = False
                log("[Manager] Saving Session export ended — returning to self-consumption")
                if self.modbus and self.modbus.connected:
                    if not self.modbus.set_self_consumption():
                        # Confirm, never assume: an unconfirmed hand-back is what left a
                        # VPP window exporting past its end (v5.64.0). Same lesson here.
                        log("[Manager] Saving Session hand-back to Self Consumption was "
                            "NOT confirmed — retrying on the next tick", level="WARNING")
                        self.store["vpp_handback_pending"] = True

            # Determine if inverter is currently in a non-self-consumption mode.
            # Check store flags first; fall back to actual emsWorkMode from inverter data
            # so a restart (which resets all flags to False) can still recover a stuck mode.
            ems_mode_str   = self.latest_inverter_data.get("emsWorkMode", "")
            inverter_stuck = ems_mode_str in ("Discharge ESS First", "Charge Grid First")

            if prev_import:
                # Only cancel an active import if the target SOC has been reached.
                # Without this guard the manager oscillates: one tick of importing
                # nudges SOC above the viability floor → next evaluate() returns
                # SELF_CONSUMPTION → import cancelled → SOC drops → repeat.
                current_soc = self.latest_inverter_data.get("batterySoc", 0.0)
                target_soc  = self.store.get("import_target_soc", 0.0)
                if current_soc >= target_soc:
                    log(f"[Manager] Import complete ({current_soc:.1f}% >= {target_soc:.0f}%) - returning to self-consumption")
                    self.modbus.set_self_consumption()
                    self._restore_import_cutoff()
                    self.store["import_active"]     = False
                    self.store["import_target_soc"] = 0.0
                else:
                    # self.logger.debug respects Indigo's debug-logging toggle; the
                    # module log() helper passes a string level to indigo.server.log
                    # (which wants a logging int), so "DEBUG" there prints regardless.
                    self.logger.debug(
                        f"[Manager] Holding import - SOC {current_soc:.1f}% / "
                        f"target {target_soc:.0f}%")
            elif prev_export:
                # Flood prevention complete, export disabled, or other export end
                flood_target = self.store.get("flood_prev_target_soc")
                current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)
                if flood_target:
                    log(f"[Manager] Flood prevention complete "
                        f"(SOC {current_soc:.1f}% reached {flood_target:.0f}% target) "
                        f"— returning to self-consumption")
                else:
                    log("[Manager] Export disabled — returning to self-consumption")
                self.modbus.set_self_consumption()
                if flood_target:
                    self._set_flood_prev_target(None)
                    floor = self._write_policy_floors()
                    log(f"[Manager] Discharge cutoff reset to {floor:.0f}% (policy floor)")
                    self._trigger_event("floodPreventionStopped")
                self.store["export_active"] = False
            elif self.store.get("solar_overflow_active"):
                # SOC dropped below release threshold — restore full charge rate
                log("[Manager] Solar overflow released — restoring full charge limit")
                self.modbus.set_self_consumption()   # resets charge limit to inv_max_w
                self.store["solar_overflow_active"]       = False
                self.store["solar_overflow_charge_cap_w"] = 0
                # Starts the manager's re-engage dwell (battery_manager v3.10).
                self.store["solar_overflow_released_at"]  = datetime.now(timezone.utc)
            elif inverter_stuck:
                # Inverter is in wrong mode (e.g. stuck in 0x06 after restart cleared store flags)
                log(f"[Manager] Inverter stuck in '{ems_mode_str}' — forcing self-consumption",
                    level="WARNING")
                self.modbus.set_self_consumption()

        # Check if active import has reached target SOC
        if self.store["import_active"]:
            current_soc = self.latest_inverter_data.get("batterySoc", 0.0)
            target_soc  = self.store["import_target_soc"]
            if current_soc >= target_soc:
                log(f"[Manager] Import target SOC {target_soc:.0f}% reached - stopping")
                self.modbus.set_self_consumption()
                self._restore_import_cutoff()
                self.store["import_active"]      = False
                self.store["import_target_soc"]  = 0.0

    def _set_flood_prev_target(self, target_soc_pct):
        """Set or clear flood-prevention target SOC, persisted to pluginPrefs.

        Persistence is critical: if the plugin restarts mid-pre-drain, the
        inverter's HOLD_ESS_DISCHARGE_CUTOFF register is still raised to the
        target. Without a persisted flag, startup() would lose the state and
        _verify_ems_registers() would reset the cutoff to the health floor —
        breaking the in-progress drain. Pass None or 0 to clear.
        """
        if target_soc_pct:
            self.store["flood_prev_target_soc"]    = target_soc_pct
            self.pluginPrefs["floodPrevTargetSoc"] = str(target_soc_pct)
        else:
            self.store["flood_prev_target_soc"]    = None
            self.pluginPrefs["floodPrevTargetSoc"] = ""
        # Crash-safe copy — runtime pluginPrefs writes only persist on a
        # graceful shutdown, and losing this flag mid-drain lets the verify
        # pass lower the raised hardware cutoff (see docstring).
        self._save_accumulators()

    def _set_import_cutoff(self, cutoff_pct):
        """Record (and persist) the hardware charge-cutoff backstop raised for an
        active grid import, so _verify_ems_registers maintains it instead of
        restoring 100%. Pass None (or >= 100) to clear. Mirrors
        _set_flood_prev_target — same pluginPrefs persistence caveats apply.
        """
        if cutoff_pct and float(cutoff_pct) < 100.0:
            self.store["import_charge_cutoff_pct"]    = float(cutoff_pct)
            self.pluginPrefs["importChargeCutoffPct"] = str(cutoff_pct)
        else:
            self.store["import_charge_cutoff_pct"]    = None
            self.pluginPrefs["importChargeCutoffPct"] = ""

    def _restore_import_cutoff(self):
        """Restore the hardware charge cutoff to 100% when a grid import ends.
        Best-effort — _verify_ems_registers self-heals a failed write next cycle."""
        if self.store.get("import_charge_cutoff_pct"):
            if self.modbus and self.modbus.connected:
                self.modbus.set_charge_cutoff(100.0)
            self._set_import_cutoff(None)

    def _driven_export_owns_registers(self):
        """True while a self-driven export window owns the mode and limit registers.

        TWO things drive the inverter through _drive_vpp_export — an Axle VPP window
        and an Octopus Saving Session — and both must be able to hold a mode the
        verify loop would otherwise "correct". Gating on vpp_state alone saw only
        the first. A Saving Session leaves vpp_state IDLE with export_active True,
        so _verify_ems_registers expected 0x06 and would have overwritten a bank
        (0x02) or daytime-discharge (0x05 + charge 0) window inside 60 seconds, then
        put the charge cap back to inverter max — and _drive_vpp_export only writes
        the mode on a sub-mode CHANGE, so nothing would ever have healed it. A
        silently unpaid window, with one WARNING line to show for it.

        Live near-miss 07-Sep-2026: the session survived only because a stale
        vpp_is_daytime picked night_export (0x06), the one sub-mode verify agrees
        with. The persisted flag was left True by that evening's VPP window, so the
        NEXT session would have picked 0x05 and been fought. Fixed here and in
        _export_is_daylight together — either alone still loses the window.

        VPP_PRE_CHARGING stays in the list: pre-charge is a grid IMPORT that owns
        the same registers. The export re-assert itself is additionally gated on
        export_active, which pre-charge does not set.
        """
        if self.store.get("vpp_state", VPP_IDLE) in (VPP_PRE_CHARGING, VPP_ACTIVE):
            return True
        return bool(self.store.get("saving_session_export_active"))

    def _verify_ems_registers(self):
        """Read back HOLD_ESS_MAX_DISCHARGE and HOLD_ESS_MAX_CHARGE and correct if wrong.

        These registers persist on the inverter across mode changes. A previous
        force_discharge() or force_charge() call can leave a stale limit that
        caps battery output in self-consumption mode. This runs every manager
        evaluation cycle as a self-healing guard.

        THE CADENCE IS 60 SECONDS, not the "~15 min" this docstring claimed until
        v5.91.0. MANAGER_EVAL_INTERVAL has been 60 since the manager was split
        from the Modbus poll, and a live count over the 05-Sep-2026 VPP window
        found 55 runs in the hour. The stale figure was not harmless: it was the
        stated basis for sizing a failed hand-back at "~1.25 kWh" in
        _end_vpp_export, a figure 15x too large.

        Expected values:
          - export_active: discharge limit = inverter max (night_export uses export limit register,
                           not discharge register, to cap grid flow; battery must be free to supply
                           house load + grid simultaneously)
          - import_active: charge limit = inverter max (full import power), discharge = inverter max
          - otherwise:     both limits = inverter max (unrestricted self-consumption)
        """
        if not self.modbus or not self.modbus.connected:
            return

        inv_max_w = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)

        # Always expect inverter max — night_export() uses HOLD_GRID_MAX_EXPORT_LIMIT
        # (not the discharge register) to constrain grid flow.
        expected_discharge_w = inv_max_w

        # During solar overflow the charge limit is intentionally reduced.
        # Use the stored cap as the expected value so verify() doesn't fight it.
        if self.store.get("solar_overflow_active"):
            expected_charge_w = self.store.get("solar_overflow_charge_cap_w", inv_max_w)
        else:
            expected_charge_w = inv_max_w

        # --- EMS mode ---
        # Skip while a self-driven export (VPP window OR Saving Session) owns the
        # inverter mode, so the verify loop cannot overwrite the driver's 0x02/0x05/
        # 0x06 with our self-consumption expectation. Normal verification resumes the
        # moment the window ends. See _driven_export_owns_registers for why the old
        # vpp_state-only test was not enough.
        mode_names = {0x02: "Self Consumption", 0x03: "Charge Grid First",
                      0x05: "Discharge PV First", 0x06: "Discharge ESS First"}
        _export_driven = self._driven_export_owns_registers()
        if not _export_driven:
            # Determine what mode the inverter should be in based on store flags.
            # After a restart all flags are False, so expected_mode = 0x02 (Self Consumption).
            # If the inverter is stuck in 0x06 (Discharge ESS First) from overnight export
            # this will catch and correct it on the next manager tick.
            if self.store.get("export_active"):
                expected_mode = 0x06  # Discharge ESS First
            elif self.store.get("import_active"):
                expected_mode = 0x03  # Charge Grid First
            else:
                expected_mode = 0x02  # Max Self Consumption

            actual_mode = self.modbus.read_ems_mode()
            if actual_mode is not None and actual_mode != expected_mode:
                log(
                    f"[Verify] EMS mode mismatch: inverter={mode_names.get(actual_mode, actual_mode)} "
                    f"expected={mode_names.get(expected_mode, expected_mode)} — correcting",
                    level="WARNING",
                )
                self.modbus.set_remote_ems_mode(expected_mode)
        elif self.store.get("export_active"):
            # v5.91.0: extracted so _log_vpp_snapshot can run the SAME checks
            # on the registers it has already read, instead of only filing them.
            self._verify_vpp_export_registers()

        # --- Discharge limit and charge limit ---
        # Skip while a self-driven export owns these registers (night_export sets the
        # discharge limit to inverter max; daytime_export pins the charge limit to 0).
        # A stray write here once caused a brief 2kW grid import (10-Apr-2026) when the
        # solar_overflow charge cap was written back mid-window — and writing the charge
        # limit back to inverter max under a Saving Session's 0x05 would resurrect the
        # v5.29.0 missed-dispatch failure, which is the other half of this gate.
        if not _export_driven:
            actual_discharge_w = self.modbus.read_discharge_limit()
            if actual_discharge_w is not None:
                if abs(actual_discharge_w - expected_discharge_w) > 200:
                    log(
                        f"[Verify] Discharge limit mismatch: inverter={actual_discharge_w}W "
                        f"expected={expected_discharge_w}W — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_discharge_limit(expected_discharge_w)

            actual_charge_w = self.modbus.read_charge_limit()
            if actual_charge_w is not None:
                if abs(actual_charge_w - expected_charge_w) > 200:
                    log(
                        f"[Verify] Charge limit mismatch: inverter={actual_charge_w}W "
                        f"expected={expected_charge_w}W — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_charge_limit(expected_charge_w)

        # --- Discharge cutoff (health floor, register 40048) ---
        # This register physically stops battery discharge. It is only written by
        # VPP code, so on systems without VPP activity it drifts to whatever the
        # inverter factory default is (typically 5%). Verify every cycle so the
        # hardware floor always matches the plugin's batteryHealthCutoff preference.
        # Skip if VPP has temporarily raised the cutoff — the VPP state machine owns it.
        # Skip if flood prevention has temporarily raised the cutoff — it owns it too.
        if not self.store.get("vpp_cutoff_raised") and not self.store.get("flood_prev_target_soc"):
            # ONE owner for this register. This used to read batteryHealthCutoff
            # directly, so any floor another policy had raised was pulled back to
            # 1% within a minute of that policy handing over.
            expected_cutoff_pct = self._absolute_cutoff_pct()
            actual_cutoff_pct   = self.modbus.read_discharge_cutoff()
            if actual_cutoff_pct is not None:
                if abs(actual_cutoff_pct - expected_cutoff_pct) > 0.5:
                    log(
                        f"[Verify] Discharge cutoff mismatch: inverter={actual_cutoff_pct:.1f}% "
                        f"expected={expected_cutoff_pct:.1f}% — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_discharge_cutoff(expected_cutoff_pct)

        # --- Backup reserve (the economic floor, register 40046) ---
        # Owned only while Flux is armed; see _absolute_cutoff_pct for why the
        # reserve lives here rather than on 40048. Not gated on the VPP flag: the
        # VPP floor is on 40048, and this value already excludes an event that is
        # being dispatched.
        if self._owns_backup_reserve():
            expected_backup = self._policy_discharge_floor_pct()
            actual_backup   = self.modbus.read_backup_soc()
            if actual_backup is not None and abs(actual_backup - expected_backup) > 0.5:
                log(f"[Verify] Backup reserve mismatch: inverter={actual_backup:.1f}% "
                    f"expected={expected_backup:.1f}% — correcting", level="WARNING")
                self.modbus.set_backup_soc(expected_backup)

        # --- Whole-site grid import cap (registers 40040-41) ---
        # Asserted only once the site limit is verified. Enforced by the inverter at
        # the meter, so a load that switches on between Flux decisions trims the
        # battery charge at once instead of waiting for the next plan.
        if self._flux_armed() and _as_bool(self.pluginPrefs.get("fluxSiteImportVerified", False)):
            limit_w = int(_as_float(self.pluginPrefs.get("fluxSiteImportLimitKw"), 0.0) * 1000)
            if limit_w > 0:
                actual_w = self.modbus.read_grid_import_limit()
                if actual_w is not None and abs(actual_w - limit_w) > 100:
                    log(f"[Verify] Grid import cap mismatch: inverter={actual_w} W "
                        f"expected={limit_w} W — correcting", level="WARNING")
                    self.modbus.set_grid_import_limit(limit_w)

        # --- Charge cutoff (import backstop, register 40047) ---
        # Raised only while a grid import is active (hardware ceiling in case the
        # plugin dies mid-import); expected 100% at all other times. VPP never
        # writes this register, so no VPP-state gating is needed.
        expected_charge_cutoff = self.store.get("import_charge_cutoff_pct") or 100.0
        actual_charge_cutoff   = self.modbus.read_charge_cutoff()
        if actual_charge_cutoff is not None:
            if abs(actual_charge_cutoff - expected_charge_cutoff) > 0.5:
                log(
                    f"[Verify] Charge cutoff mismatch: inverter={actual_charge_cutoff:.1f}% "
                    f"expected={expected_charge_cutoff:.1f}% — correcting",
                    level="WARNING",
                )
                self.modbus.set_charge_cutoff(expected_charge_cutoff)

    def _verify_vpp_export_registers(self, ems_mode=None, charge_w=None,
                                     discharge_w=None):
        """Check the self-driven export's registers, and re-assert any that drifted.

        Called from TWO places, deliberately, and they interleave (measured
        05-Sep-2026 over a live window: verify then snapshot 39-48s later, then
        verify ~20s after that, so the mode is actually checked every 20-45s
        rather than every 60):
          - _verify_ems_registers(), on the 60s manager cycle, reading fresh.
          - _log_vpp_snapshot(), on the 60s VPP cycle, passing the three
            registers it has JUST read for the JSONL record.

        The second caller is why the arguments exist. That snapshot read the
        mode, the charge limit and the discharge limit every minute of every
        window and did nothing with them but write them to a file — so a drift
        landing in a snapshot sat there, recorded and unacted on, until verify's
        next pass. Passing them in closes that gap for FREE: no extra Modbus
        transaction, and at the 1000ms throttle every avoided read is worth
        having during a window.

        Any argument left None is read here, so the no-argument call behaves
        exactly as the inlined block did before v5.91.0.

        ONE set of rules, in one place. Previously the expectations lived only
        inside _verify_ems_registers and the snapshot had no opinion at all;
        two copies would have been free to disagree about which registers may
        be written mid-window, and that question has an expensive wrong answer
        (see the 10-Apr-2026 note below).
        """
        if not self.modbus or not self.modbus.connected:
            return
        # The state guard lives HERE rather than at the call sites. It used to be
        # the elif condition in _verify_ems_registers; now that a second caller
        # exists, a check that RE-ASSERTS an export mode must be unable to fire
        # outside a live export window whoever calls it. It covers a Saving Session
        # as well as a VPP window — a driven export that nothing verifies is exactly
        # the gap this function was written to close.
        if not self._driven_export_owns_registers():
            return
        if not self.store.get("export_active"):
            return

        inv_max_w  = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
        mode_names = {0x02: "Self Consumption", 0x03: "Charge Grid First",
                      0x05: "Discharge PV First", 0x06: "Discharge ESS First"}

        # During the self-driven window the export mode is written once at
        # VPP_ACTIVE entry and the manager's ACTION_VPP_EXPORT guard does not
        # re-write it. Maintain the chosen mode (0x05 daytime / 0x06 dark)
        # here so a transient drift self-heals. Mode register, PLUS the bank
        # sub-mode's own charge cap (see below) — and nothing else. A stray
        # write of any OTHER limit once caused a brief 2 kW grid import
        # (10-Apr-2026), which is why the general limit block in
        # _verify_ems_registers still skips the whole window.
        expected_mode = self.store.get("vpp_export_mode", 0x06)
        actual_mode   = ems_mode if ems_mode is not None else self.modbus.read_ems_mode()
        if actual_mode is not None and actual_mode != expected_mode:
            log(
                f"[Verify] VPP export mode drift: inverter={mode_names.get(actual_mode, actual_mode)} "
                f"expected={mode_names.get(expected_mode, expected_mode)} — re-asserting",
                level="WARNING",
            )
            self.modbus.set_remote_ems_mode(expected_mode)

        # ONE exception to "mode register only": the bank sub-mode's charge
        # cap. In bank (0x02) the cap IS the export mechanism — it is what
        # stops the inverter soaking the PV surplus into the battery instead
        # of sending it to the grid. If it drifted up to inverter max the
        # export would fall to ~0 and NOTHING above would notice, because the
        # mode register would still read a correct 0x02. _drive_vpp_export
        # only rewrites the cap when the surplus moves by >300 W, so a drift
        # could stand for the rest of the window — a silently unpaid event.
        #
        # This does not reopen the 10-Apr-2026 incident: that was the
        # solar-overflow cap being written back over a VPP window. Here the
        # expected value is the VPP driver's own cap, and the check only runs
        # while bank is the live sub-mode. The 0x05/0x06 limits stay untouched
        # (they are static for the window, and the snapshots record them).
        if self.store.get("vpp_export_submode") == "bank":
            expected_cap_w = self.store.get("vpp_bank_charge_cap_w", -1)
            if expected_cap_w is not None and expected_cap_w >= 0:
                actual_cap_w = charge_w if charge_w is not None else self.modbus.read_charge_limit()
                if actual_cap_w is not None and abs(actual_cap_w - expected_cap_w) > 300:
                    log(
                        f"[Verify] VPP bank charge-cap drift: inverter={actual_cap_w}W "
                        f"expected={expected_cap_w}W — re-asserting (export would "
                        f"otherwise be banked instead of sold)",
                        level="WARNING",
                    )
                    self.modbus.set_charge_limit(expected_cap_w, quiet=True)

        # And the DISCHARGE sub-mode's own registers (v5.57.0). Same argument
        # as the bank cap, in the other sub-mode:
        #   - daytime_export pins charge=0, because in 0x05 an OPEN charge
        #     limit lets high PV charge the battery INSTEAD of exporting —
        #     the v5.29.0 missed-dispatch failure. A failed or externally
        #     reverted write resurrects it with the mode register reading a
        #     perfectly correct 0x05, so nothing above notices. Dark windows
        #     never pin charge (PV is zero, the register is irrelevant), so
        #     only the daytime latch asserts it.
        #   - both discharge modes wrote discharge=inv_max, and the
        #     docstring above has promised "export_active: discharge limit
        #     = inverter max" since v5.16 while this branch skipped the
        #     whole window. A stale lower cap (a failed night_export write,
        #     or anything external) throttles the paid export for the
        #     remaining window with nothing to heal it.
        # Neither write can cause a grid import — a charge cap of 0 blocks
        # charging, and the discharge limit is the register the drive
        # itself wrote — so the 10-Apr-2026 rule is not reopened.
        elif self.store.get("vpp_export_submode") == "discharge":
            actual_dis_w = discharge_w if discharge_w is not None else self.modbus.read_discharge_limit()
            if actual_dis_w is not None and abs(actual_dis_w - inv_max_w) > 200:
                log(
                    f"[Verify] VPP discharge-limit drift: inverter={actual_dis_w}W "
                    f"expected={inv_max_w}W — re-asserting (a low cap throttles "
                    f"the paid export)",
                    level="WARNING",
                )
                self.modbus.set_discharge_limit(inv_max_w)
            if self.store.get("vpp_is_daytime"):
                actual_chg_w = charge_w if charge_w is not None else self.modbus.read_charge_limit()
                if actual_chg_w is not None and actual_chg_w > 300:
                    log(
                        f"[Verify] VPP daytime-discharge charge cap drift: "
                        f"inverter={actual_chg_w}W expected=0W — re-asserting "
                        f"(open charge limit in 0x05 banks PV instead of "
                        f"exporting it — the v5.29.0 failure)",
                        level="WARNING",
                    )
                    self.modbus.set_charge_limit(0, quiet=True)

    def _check_scheduled_import(self):
        # v5.45.0: commands hardware from store state — runs under the lock.
        with self._state_lock:
            return self._check_scheduled_import_impl()

    def _check_scheduled_import_impl(self):
        """Check if a scheduled import time has arrived."""
        scheduled = self.store.get("import_scheduled_time")
        if scheduled is None:
            return

        # v5.65.0: this runs from the tick, OUTSIDE the paused gate in
        # _evaluate_manager_impl — so a pause used to stop the manager deciding
        # while leaving it free to fire a queued 10 kW import. _disengage_to_safe_baseline
        # now cancels the schedule when pause is set, and this is the second lock
        # on the same door: whatever route set the flag, a paused manager drives
        # nothing. Left armed rather than cleared, so resuming keeps the schedule
        # the manager will re-emit anyway if it still wants it.
        if self.store.get("manager_paused", False):
            if not self.store.get("import_scheduled_paused_logged"):
                self.store["import_scheduled_paused_logged"] = True
                log("[Manager] Scheduled import held — manager is paused")
            return
        self.store["import_scheduled_paused_logged"] = False

        # v5.71.1: the same door, for a VPP window. This runs from the TICK, so it
        # fires on the clock alone — while the manager's own override (which returns
        # ACTION_VPP_EXPORT and would retract the schedule) only re-evaluates on the
        # 60-second cycle, and that retraction is skipped once an import is running.
        # A schedule armed for a time inside a window therefore fired mid-export:
        # Charge Grid First at up to inverterMaxKw, buying at the import rate through
        # the very hour we are being paid a premium to sell, with _verify_ems_registers
        # deliberately standing down for the duration so nothing corrected it.
        # Predbat reached the same conclusion from the planning side in #4520 — an
        # export event makes importing expensive by exactly the premium it forfeits.
        #
        # HELD, not cancelled: the battery still wants that charge, so the schedule
        # stays armed and fires once the window closes. PRE_CHARGING is included
        # because the plugin is already grid-charging to its own target there, and a
        # second charge command with its own cutoff would fight it. ANNOUNCED is NOT
        # included — that can be hours ahead, and charging before a window is the
        # arbitrage this plugin exists to do.
        _vpp_state = self.store.get("vpp_state", VPP_IDLE)
        if _vpp_state in (VPP_PRE_CHARGING, VPP_ACTIVE):
            if not self.store.get("import_scheduled_vpp_logged"):
                self.store["import_scheduled_vpp_logged"] = True
                log(f"[Manager] Scheduled import held — VPP window {_vpp_state} "
                    f"(importing now would forfeit the export premium). It will "
                    f"fire once the window closes.")
            return
        self.store["import_scheduled_vpp_logged"] = False

        now_utc = datetime.now(timezone.utc)
        # Normalise scheduled time to UTC if naive. A naive value here is local
        # wall-clock, so it needs localising (pytz) or stamping (zoneinfo) — the
        # one operation the two libraries spell differently, hence the shared
        # helper. It returns None only if no tz database exists at all; skip the
        # tick loudly rather than start an import at the wrong hour.
        if scheduled.tzinfo is None:
            localised = _london_localise(scheduled)
            if localised is None:
                _warn_no_tzdb()
                return
            scheduled = localised.astimezone(timezone.utc)

        if now_utc >= scheduled:
            log("[Manager] Scheduled import window reached - starting import")
            # This path writes to the inverter straight from the tick, outside
            # the manager's evaluate, so it needs its own pre-emption: Flux lets
            # go before the charge command, not after it.
            self._flux_preempt("a scheduled grid import")
            # v5.65.0: `(target_soc or 100.0)` turned a target of 0.0 into a 100%
            # charge cutoff — and 0.0 is exactly what an intervening completed
            # import leaves behind. The backstop meant to STOP a runaway import
            # became permission for one. A missing/zero target is now the same
            # conservative default the .get() already uses, not "charge to full".
            target_soc = self.store.get("import_target_soc") or 12.0
            cutoff     = min(target_soc + 3.0, 100.0)
            # Power follows the configured inverter rating, as the START_IMPORT
            # branch already does — 10000 was right only on a 10 kW machine.
            power_w = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
            if self.modbus and self.modbus.force_charge(power_w, cutoff_soc=cutoff):
                self.store["import_active"]      = True
                self.store["import_target_soc"]  = target_soc
                self.store["import_scheduled_time"] = None
                self.store["had_import_today"]   = True   # daily history flag
                self._set_import_cutoff(cutoff)
                self._trigger_event("emergencyImportTriggered")

    # ================================================================
    # Solar Forecast Refresh
    # ================================================================

    def _refresh_forecast(self, force=False):
        """Fetch updated solar forecast from Open-Meteo."""
        if not self.forecast:
            return

        data = self.forecast.fetch_forecast(force=force)
        self.latest_forecast_data = data

        self._update_forecast_device(data)

        status   = data.get("forecastStatus", "")
        # A SUCCESS stamp, separate from store["last_forecast"], which records
        # when the task last RAN. A forecast that has been failing for six hours
        # reads as fresh on the attempt stamp, and the Flux planner buys on the
        # strength of this forecast — so it asks this one.
        #
        # It stamps the GENERATION that came back, not the poll: the buckets must
        # be present AND carry today's local date. A cached payload from
        # yesterday satisfies "not empty" perfectly well, and dating it today is
        # how a stale forecast would look current for ever.
        try:
            _today_key = _london_today().strftime("%Y-%m-%d")
            _buckets   = (data or {}).get("_hourly_p50_today") or {}
            _dated_today = any(str(k).startswith(_today_key) for k in _buckets)
        except Exception:                                   # noqa: BLE001
            _dated_today = False
        if data and "No data" not in status and _dated_today:
            generated_at = getattr(self.forecast, "_cached_time", None)
            if (type(generated_at) in (int, float) and math.isfinite(generated_at)
                    and 0 < generated_at <= time.time()):
                self.store["forecast_ok_at"] = generated_at
        tmrw_kwh = data.get("correctedTomorrowKwh", 0.0)

        if "No data" in status:
            log(f"[Solar] WARNING: forecast unavailable ({status}) — night export condition 3 will block", level="WARNING")
        elif tmrw_kwh == 0.0:
            log(f"[Solar] WARNING: tomorrow forecast is 0.0 kWh (status: {status!r}) — night export condition 3 will block", level="WARNING")
        elif self.debug:
            log(
                f"[Solar] Today: {data.get('correctedTodayKwh', 0):.1f} kWh "
                f"(raw {data.get('todayKwh', 0):.1f}, bias {data.get('biasFactor', 1):.3f}), "
                f"Tomorrow: {tmrw_kwh:.1f} kWh"
            )

    # ================================================================
    # Octopus Refresh
    # ================================================================

    def _refresh_octopus_rates(self, force=False):
        """Fetch current tariff rates from Octopus API."""
        if not self.octopus:
            return

        try:
            tariff_info   = self.octopus.get_current_tariff(force=force)
            monitored     = self.octopus.get_all_monitored_rates(force=force)

            # Keep today's Agile slots while on Tracker for the log-only
            # same-consumption comparison. This is one cached public-rates
            # request, not a tariff change and not an input to current control.
            if bool(self.pluginPrefs.get("solarOverflowShadowEnabled", True)):
                try:
                    monitored["shadow_agile_slots"] = self.octopus.get_agile_rates(
                        _london_today(), force=force)
                except Exception as exc:
                    monitored["shadow_agile_slots"] = []
                    self.logger.debug(f"[Shadow] Agile comparison rates unavailable: {exc!r}")

            self.latest_rates_data = {
                "tariff_info": tariff_info,
                **monitored,
            }
            # The paired Flux schedules. Fetched when the controller is armed —
            # it cannot price a trade without them — AND whenever the ACCOUNT is
            # detected as Flux, even with the controller switched off, because
            # REPORTING needs them too: the export rate written to the dashboard
            # and into every daily history record comes from this schedule, and
            # without it a live Flux house would go on being told its exports
            # earned the old flat Outgoing rate.
            _detected = str((tariff_info or {}).get("detected_key")
                            or (tariff_info or {}).get("tariff_key") or "")
            if self._flux_enabled() or _detected == TARIFF_FLUX:
                self._refresh_flux_rates(force=force)

            # THE EXPORT RATE, PUBLISHED — and published AFTER the schedules it
            # reads. Nothing ever wrote this key, so every consumer (dashboard,
            # manager snapshot, VPP revenue, daily history) fell back to the flat
            # 12p Outgoing rate: correct while the account was on Outgoing, wrong
            # the day it moved to paired Flux. Published BEFORE the fetch, as it
            # first was, the first refresh after every restart still quoted 12p,
            # because the bands it needs had not arrived yet.
            self.latest_rates_data["export_rate_p"] = self._export_rate_now_p()

            self._update_tariff_device(tariff_info, monitored)
            self._write_tariff_schedule_variables(tariff_info, monitored)

            # Log on first fetch or when tariff / rate changes; also in debug mode
            tariff_key = tariff_info.get("tariff_key", "?")
            today_rate, tomorrow_rate = self._rates_for_tariff(tariff_key, monitored)
            with self._state_lock:   # v5.45.0: fetch unlocked, store merge locked
                _changed = self._tariff_line_changed(
                    tariff_key, today_rate,
                    self.store.get("_last_tariff_key"),
                    self.store.get("_last_tariff_rate"))
                if _changed:
                    self.store["_last_tariff_key"]  = tariff_key
                    self.store["_last_tariff_rate"] = today_rate
            if _changed or self.debug:
                _now  = f"{today_rate:.3f}p" if today_rate is not None else "not published"
                if tariff_key == TARIFF_AGILE:
                    _slots = len(monitored.get("agile_slots", []))
                    _tail  = f"now {_now} (this half-hour), {_slots} slots held"
                else:
                    _tmrw  = (f"{tomorrow_rate:.3f}p" if tomorrow_rate is not None
                              else "not published yet")
                    _tail  = f"today {_now}, tomorrow {_tmrw}"
                log(
                    f"[Octopus] Tariff: {tariff_info.get('display_name', tariff_key)} "
                    f"({tariff_info.get('product_code', '?')}) — {_tail}"
                )

        except Exception as e:
            log(f"[Octopus] Rate refresh error: {e}", level="ERROR")

    def _write_tariff_schedule_variables(self, tariff_info, monitored):
        """Write the rate + slot-JSON Indigo variables consumed by the openmeteo
        battery optimiser, so the plugin is the single source of truth and the
        standalone octopus_tracker_rate.py script can be retired.

        elec_rates_*_json carry the raw Octopus slots [{valid_from, valid_to,
        value_inc_vat}, ...]. Tomorrow's slots aren't published until ~16:00, so the
        tomorrow variable is only overwritten when slots are available — stale is
        softer than empty (the optimiser refuses to plan on an empty list).
        """
        if not self.octopus:
            return
        try:
            sched   = self.octopus.get_active_rate_schedule()
            tracker = (monitored or {}).get("tracker", {}) or {}
            folder  = self._sigenergy_folder_id()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            today_slots    = sched.get("today_slots") or []
            tomorrow_slots = sched.get("tomorrow_slots") or []
            today_p        = tracker.get("today_p")
            tomorrow_p     = tracker.get("tomorrow_p")

            writes = []
            if today_slots:
                writes.append(("elec_rates_today_json", json.dumps(today_slots)))
            if tomorrow_slots:
                writes.append(("elec_rates_tomorrow_json", json.dumps(tomorrow_slots)))
            # today_p None = fetch failure (today's rate always exists once
            # published) — keep the last good numeric value rather than
            # destroying it with a string that breaks float() consumers;
            # tracker_fetch_status carries the failure signal. tomorrow_p None
            # is NORMAL before ~16:00, and "pending" is the truthful value
            # (yesterday's tomorrow-rate would be actively wrong after midnight).
            on_tracker = str((tariff_info or {}).get("tariff_key") or "") == TARIFF_TRACKER
            if on_tracker:
                if today_p is not None:
                    writes.append(("tracker_rate_today", f"{today_p:.2f}"))
                writes.append(("tracker_rate_tomorrow",
                               f"{tomorrow_p:.2f}" if tomorrow_p is not None else "pending"))
                writes.append(("tracker_fetch_status", "OK"))
            else:
                # v5.110.0: off Tracker these two held the last Tracker price for
                # ever (26.21p on the first Flux day), and the morning brief read
                # it out as today's price. "n/a" is not a number, so every float()
                # consumer now sees no price rather than a wrong one.
                writes.append(("tracker_rate_today", "n/a"))
                writes.append(("tracker_rate_tomorrow", "n/a"))
                writes.append(("tracker_fetch_status", "not on Tracker"))
            if sched.get("product_code"):
                writes.append(("tracker_product_code", sched["product_code"]))
            if tariff_info and tariff_info.get("display_name"):
                writes.append(("tracker_product_name", tariff_info["display_name"]))
            writes.append(("tracker_last_updated", now_str))
            # The billed export schedule for today, same slot shape as
            # elec_rates_today_json, when the export side is banded.
            if self._export_tariff_is_banded():
                bounds = self._day_bounds_utc(_local_today_str())
                if bounds is not None:
                    exp_today = []
                    for slot in (self.store.get("flux_export_slots") or []):
                        try:
                            s = datetime.fromisoformat(str(slot["valid_from"]).replace("Z", "+00:00"))
                            e = datetime.fromisoformat(str(slot["valid_to"]).replace("Z", "+00:00"))
                        except (KeyError, TypeError, ValueError):
                            continue
                        if e > bounds[0] and s < bounds[1]:
                            exp_today.append(slot)
                    if exp_today:
                        writes.append(("export_rates_today_json", json.dumps(exp_today)))

            for name, value in writes:
                var_id = self._ensure_var(name, folder)
                if var_id:
                    indigo.variable.updateValue(var_id, value)
            self.logger.debug(
                f"[Octopus] Tariff vars written: today {len(today_slots)} slot(s), "
                f"tomorrow {len(tomorrow_slots)} slot(s)"
            )
        except Exception as exc:
            log(f"[Octopus] Tariff-schedule variable write failed: {exc}", level="WARNING")

    def _is_away(self):
        """True when the configured Indigo variable says the house is empty.

        FAILS TOWARDS OCCUPIED, deliberately.  The two errors are not
        symmetrical: believing the house is empty when it is not under-imports
        and leaves the battery short on a winter evening, while believing it is
        occupied when it is empty merely buys a little more than needed and the
        SOC guard caps that anyway.  So a missing variable, a missing config, a
        junk value or an exception all return False rather than propagating an
        unknown.  Same reasoning as the absent-state rule applied to a device.
        """
        prefs = getattr(self, "pluginPrefs", None) or {}
        if not prefs.get("awayEnabled", False):
            return False
        name = str(prefs.get("awayVariable") or AWAY_VARIABLE_DEFAULT).strip()
        if not name:
            return False
        try:
            if name not in indigo.variables:
                # Warn ONCE.  This runs every Modbus poll, so an unconditional
                # warning would be ~1400 identical lines a day.
                if not self.store.get("away_warned"):
                    self.store["away_warned"] = True
                    log(f"[Away] Variable '{name}' does not exist — treating the "
                        f"house as occupied. Set the correct name in Configure, "
                        f"or untick Away mode.", level="WARNING")
                return False
            self.store["away_warned"] = False
            return str(indigo.variables[name].value).strip().lower() in AWAY_TRUTHY
        except Exception as exc:                                   # noqa: BLE001
            if not self.store.get("away_warned"):
                self.store["away_warned"] = True
                log(f"[Away] Could not read variable '{name}' "
                    f"({type(exc).__name__}: {exc}) — treating the house as occupied.",
                    level="WARNING")
            return False

    def _refresh_away_state(self):
        """Re-read the away flag, and on a change swap which profile is live.

        Called from the Modbus merge, i.e. once per poll, immediately before the
        reading is accumulated — so a reading is never filed against the wrong
        profile across a transition.
        """
        now_away = self._is_away()
        was_away = bool(self.store.get("away_active", False))
        if now_away != was_away:
            self.store["away_active"] = now_away
            counts   = self.store["away_profile_count" if now_away else "home_profile_count"]
            real     = sum(1 for c in counts if c >= (AWAY_PROFILE_MIN_READINGS
                                                      if now_away else HOME_PROFILE_MIN_READINGS))
            log(f"[Away] House is now {'EMPTY' if now_away else 'OCCUPIED'} — "
                f"switching to the {'away' if now_away else 'occupied'} consumption "
                f"profile ({real}/48 slots from real data)")
            # Rebuild immediately: the manager may evaluate before the next
            # scheduled refresh, and it must not plan tonight against the
            # profile for the house it is no longer in.
            self._refresh_consumption_profile()
        return now_away

    def _accumulate_home_profile(self, home_watts):
        """Accumulate one inverter home-load reading into the 48-slot half-hourly profile.

        Called every Modbus poll (~60s).  Readings are averaged per 30-min slot over
        many days, giving a robust consumption profile that reflects actual house load
        rather than the Octopus import meter (which shows only ~0.7 kWh/day on a
        near self-sufficient system instead of the real ~12-15 kWh/day load).
        """
        now  = datetime.now()
        slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
        slot = max(0, min(47, slot))
        # v5.78.0: file the reading against whichever house this is. The away
        # state is refreshed immediately before this call, so a transition
        # cannot land a full-house reading in the empty-house profile.
        prefix = "away" if self.store.get("away_active") else "home"
        self.store[f"{prefix}_profile_watts_sum"][slot] += home_watts
        self.store[f"{prefix}_profile_count"][slot]     += 1

        # v5.104.0: the same reading again, filed under its own day, so the
        # planning profile can be a rolling window instead of a mean that never
        # forgets anything.
        #
        # Occupied days ONLY. The away profile deliberately banks rare data —
        # this house was empty for six weeks in the whole of 2025 — so a window
        # would throw away the last trip long before the next one, and with a
        # minimum of PROFILE_MIN_WINDOW_DAYS it could never engage for a
        # fortnight away. An empty house has no drift to track; that is the
        # whole thing a window is for.
        if prefix == "away":
            return
        days = self.store.setdefault("home_profile_days", {})
        key  = now.strftime("%Y-%m-%d")
        day  = days.get(key)
        if day is None:
            day = {"sum": [0.0] * 48, "count": [0] * 48}
            days[key] = day
            self._prune_profile_days()
        day["sum"][slot]   += home_watts
        day["count"][slot] += 1

    def _prune_profile_days(self):
        """Drop day buckets that have fallen out of the rolling window.

        Pruned BY DATE, not by keeping the newest N buckets. A week-long outage
        should leave a week-shaped hole that expires on schedule, not reach an
        extra week further back to make the count up — stale readings are the
        one thing the window exists to shed. The cost is that an outage briefly
        unbalances the day-of-week mix the 9-week span is chosen to keep even.
        Called when a new day opens, so once a day rather than every poll.
        """
        days = self.store.setdefault("home_profile_days", {})
        if not days:
            return
        cutoff = (datetime.now() - timedelta(days=PROFILE_WINDOW_DAYS)).strftime("%Y-%m-%d")
        stale  = [d for d in days if d < cutoff]
        for d in stale:
            del days[d]
        if stale:
            self.logger.debug(
                f"[Profile] Rolling window dropped {len(stale)} day(s) older than "
                f"{cutoff}; {len(days)} day(s) retained"
            )

    def _windowed_profile_totals(self):
        """(watts_sum[48], reading_count[48], day_count[48]) over the rolling window.

        day_count is what decides whether a slot is trustworthy, NOT the reading
        count: at a 10 s poll one day alone puts ~147 readings in every slot, so
        a reading-count threshold would let a single day's data speak for the
        whole window. See the note on HOME_PROFILE_MIN_READINGS.
        """
        sums   = [0.0] * 48
        counts = [0]   * 48
        days   = [0]   * 48
        for bucket in self.store.get("home_profile_days", {}).values():
            b_sum, b_cnt = bucket.get("sum") or [], bucket.get("count") or []
            if len(b_sum) != 48 or len(b_cnt) != 48:
                continue
            for i in range(48):
                if b_cnt[i]:
                    sums[i]   += b_sum[i]
                    counts[i] += b_cnt[i]
                    days[i]   += 1
        return sums, counts, days

    def _refresh_consumption_profile(self, force=False):
        # v5.45.0: reads the profile accumulators + writes the store — locked
        # (no network; the accumulators are fed by the locked modbus merge).
        with self._state_lock:
            return self._refresh_consumption_profile_impl(force=force)

    def _refresh_consumption_profile_impl(self, force=False):
        """Rebuild 48-slot consumption profile from accumulated inverter readings.

        Each slot (0=00:00, 1=00:30 … 47=23:30) holds the average homePowerWatts
        seen during that half-hour across all polling days.  Slots with fewer than
        HOME_PROFILE_MIN_READINGS readings fall back to the OctopusAPI default
        (UK typical ~12 kWh/day shape) so the first day still works correctly.

        Profile values are kWh per half-hourly slot (watts × 0.5 h / 1000).
        """
        try:
            # v5.78.0: two profiles, one live. The fallback differs too — an
            # empty house must fall back to the flat away seed, never to the
            # UK-typical occupied shape, or the first days of a trip plan
            # against an evening peak that is not going to happen.
            away = bool(self.store.get("away_active"))
            if away:
                default   = _away_seed_profile(
                    _as_float(self.pluginPrefs.get("awayDailyKwh"), AWAY_DAILY_KWH_DEFAULT))
                watts_sum = self.store["away_profile_watts_sum"]
                counts    = self.store["away_profile_count"]
                min_reads = AWAY_PROFILE_MIN_READINGS
            else:
                default   = OctopusAPI._default_consumption_profile()
                watts_sum = self.store["home_profile_watts_sum"]
                counts    = self.store["home_profile_count"]
                min_reads = HOME_PROFILE_MIN_READINGS
            # v5.104.0: three sources per slot, in order — the rolling window
            # first, the lifetime accumulator second, the default shape last.
            # The lifetime figure is kept as the middle rung on purpose: it is
            # what makes the upgrade seamless. Until the window has
            # PROFILE_MIN_WINDOW_DAYS of its own the old number serves, so the
            # plan does not jump on the day this ships and does not swing to
            # whatever the last 24 hours happened to look like.
            if away:
                win_sum, win_cnt, win_days = [0.0] * 48, [0] * 48, [0] * 48
            else:
                win_sum, win_cnt, win_days = self._windowed_profile_totals()

            profile     = []
            window_slots = 0
            real_slots   = 0
            for i in range(48):
                if win_days[i] >= PROFILE_MIN_WINDOW_DAYS and win_cnt[i] > 0:
                    profile.append(round((win_sum[i] / win_cnt[i]) * 0.5 / 1000.0, 4))
                    window_slots += 1
                    real_slots   += 1
                elif counts[i] >= min_reads:
                    profile.append(round((watts_sum[i] / counts[i]) * 0.5 / 1000.0, 4))
                    real_slots += 1
                else:
                    profile.append(default[i])

            self.store["consumption_profile"] = profile
            # Freshness for consumers that plan against it (the Flux supervisor
            # refuses a profile older than a week — it is rebuilt daily, so an old
            # one means the rebuild has been failing).
            self.store["profile_built_at"] = time.time()
            daily_kwh    = sum(profile)
            retained_days = len(self.store.get("home_profile_days", {}))
            if away:
                source = "the away accumulator (no window — an empty house has no drift to track)"
            elif window_slots == 48:
                source = f"the rolling {PROFILE_WINDOW_DAYS}-day window ({retained_days} days held)"
            elif window_slots:
                source = (f"{window_slots}/48 slots from the rolling window "
                          f"({retained_days} days held), the rest from the lifetime mean")
            else:
                source = (f"the lifetime mean — the window has {retained_days} day(s), "
                          f"and needs {PROFILE_MIN_WINDOW_DAYS} before it is trusted")
            log(
                f"[Profile] {'Away' if away else 'Occupied'} consumption profile "
                f"updated from inverter data — daily: {daily_kwh:.1f} kWh  "
                f"({real_slots}/48 slots from real data, "
                f"{48 - real_slots} using default) — {source}"
            )
            # v5.15: republish sigen_site_config.json so the optimiser
            # script picks up the freshly-calibrated profile on its next run.
            # Wrapped in its own try so a write failure here doesn't mask
            # the profile-refresh success.
            try:
                self._write_site_config()
            except Exception as exc:
                self.logger.warning(
                    f"[Profile] Could not republish site_config after refresh: {exc}"
                )
        except Exception as e:
            log(f"[Profile] Refresh error: {e}", level="ERROR")

    def _save_home_profile(self):
        """Persist home-load profile accumulators to disk (home_load_profile.json).

        Written every 5 minutes (via _save_accumulators) and on plugin shutdown.
        The file survives across restarts and day-rollover; it is never deleted.
        """
        path = os.path.join(self.data_dir, "home_load_profile.json")
        data = {
            "watts_sum": self.store["home_profile_watts_sum"],
            "count":     self.store["home_profile_count"],
            # v5.78.0. New keys, so a file written by <=5.77.3 simply lacks them
            # and the away accumulators start empty — no migration needed.
            "away_watts_sum": self.store["away_profile_watts_sum"],
            "away_count":     self.store["away_profile_count"],
            # v5.104.0. A new key, so a file written by <= 5.103.x simply lacks
            # it, the window starts empty and the lifetime mean above carries
            # the plan until it fills — no migration, no discontinuity.
            # Sums are rounded to 1 dp purely to keep the file readable; they
            # run to millions of watt-readings and the last decimal is noise.
            "days": {
                d: {"sum": [round(v, 1) for v in b["sum"]], "count": list(b["count"])}
                for d, b in self.store.get("home_profile_days", {}).items()
            },
            "saved_at":  datetime.now().isoformat(),
        }
        try:
            # Atomic — written every 5 minutes; a crash mid-write must not
            # truncate the profile and silently reset it to defaults.
            _atomic_write_json(path, data)
        except Exception as e:
            self.logger.warning(f"Cannot save home profile: {e}")

    def _load_home_profile(self):
        """Restore home-load profile accumulators from disk on startup.

        If no file exists (fresh install) the in-memory defaults of all-zeros
        remain, and the first HOME_PROFILE_MIN_READINGS days fall back to the
        OctopusAPI default shape.
        """
        path = os.path.join(self.data_dir, "home_load_profile.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            watts_sum = data.get("watts_sum", [])
            counts    = data.get("count", [])
            away_sum  = data.get("away_watts_sum", [])
            away_cnt  = data.get("away_count", [])
            days      = data.get("days") or {}
            restored  = {}
            for d, b in days.items():
                try:
                    b_sum = [float(v) for v in (b.get("sum") or [])]
                    b_cnt = [int(v)   for v in (b.get("count") or [])]
                except (TypeError, ValueError):
                    continue
                if len(b_sum) == 48 and len(b_cnt) == 48:
                    restored[str(d)] = {"sum": b_sum, "count": b_cnt}
            self.store["home_profile_days"] = restored
            if restored:
                self._prune_profile_days()
            if len(away_sum) == 48 and len(away_cnt) == 48:
                self.store["away_profile_watts_sum"] = [float(v) for v in away_sum]
                self.store["away_profile_count"]     = [int(v)   for v in away_cnt]
            if len(watts_sum) == 48 and len(counts) == 48:
                self.store["home_profile_watts_sum"] = [float(v) for v in watts_sum]
                self.store["home_profile_count"]     = [int(v)   for v in counts]
                # Seed the away flag BEFORE the rebuild, or a restart during a
                # trip comes back up on the occupied profile until the next poll.
                self.store["away_active"] = self._is_away()
                # Immediately build consumption_profile from restored data
                self._refresh_consumption_profile()
                real_slots = sum(1 for c in counts if c >= HOME_PROFILE_MIN_READINGS)
                away_real  = sum(1 for c in away_cnt if c >= AWAY_PROFILE_MIN_READINGS)
                self.logger.info(
                    f"Home load profile restored — {real_slots}/48 slots from real "
                    f"data ({away_real}/48 away), rolling window holds "
                    f"{len(self.store['home_profile_days'])}/{PROFILE_WINDOW_DAYS} days"
                    + ("  [house is currently EMPTY]" if self.store["away_active"] else "")
                )
        except Exception as e:
            self.logger.warning(f"Cannot load home profile: {e}")

    # ================================================================
    # VPP State Machine
    # ================================================================

    def _vpp_poll_interval(self):
        """Return adaptive VPP poll interval.

        ACTIVE polls at 60s so the self-driven export window is tracked closely.
        """
        state = self.store["vpp_state"]
        event = self.store["vpp_event"]

        if state == VPP_ACTIVE:
            return VPP_POLL_ACTIVE_INTERVAL

        if event and state in (VPP_ANNOUNCED, VPP_PRE_CHARGING):
            start = event.get("start_time")
            if start:
                hours_away = (start - datetime.now(timezone.utc)).total_seconds() / 3600.0
                if hours_away <= 2.0:
                    return VPP_POLL_ACTIVE_INTERVAL

        return VPP_POLL_NORMAL_INTERVAL

    def _record_vpp_api_status(self, error):
        """Record the outcome of the latest Axle poll and surface a lasting failure.

        get_next_event() returns None for BOTH "no event scheduled" and a hard
        failure, so an unhealthy feed looks exactly like a quiet week. Axle
        revoked this install's token some time after 15-Jun-2026 and the plugin
        polled a dead endpoint for six weeks in complete silence — the VPP page
        simply read "Standby" throughout. This is what makes that state visible.

        Logs on the first occurrence of a given failure and hourly thereafter, so
        a sustained outage costs one line an hour rather than one every 10 min.
        """
        prev = self.store.get("vpp_api_error")
        self.store["vpp_api_error"] = error

        if error:
            self.store["vpp_api_fails"] = self.store.get("vpp_api_fails", 0) + 1
            now = time.time()
            if error != prev or now - self.store.get("vpp_api_logged", 0.0) >= 3600.0:
                self.store["vpp_api_logged"] = now
                vpp_log(f"[VPP] Axle poll failing - {error} "
                    f"(consecutive failures: {self.store['vpp_api_fails']})", "ERROR")
        else:
            if prev:
                vpp_log(f"[VPP] Axle poll recovered after {self.store.get('vpp_api_fails', 0)} "
                    f"failure(s) - API reachable again")
            self.store["vpp_api_fails"]  = 0
            self.store["vpp_api_logged"] = 0.0
            self.store["vpp_api_last_ok"] = time.time()


    def _record_vpp_ledger_status(self, problem):
        """Surface an earnings ledger that has stopped being fed.

        The exact twin of _record_vpp_api_status above, for the other Axle feed.
        That one exists because a dead events endpoint "looks exactly like a
        quiet week" — this install polled a revoked token for six weeks in
        silence. The LEDGER had no such guard at all: `axle_age_days` has been
        computed on every summary since the ledger shipped and rendered by
        nothing, and the only warning lived inside a menu item the user has to
        fire by hand. On 05-Sep-2026 the Axle side was 17.2 days old and not one
        thing on this system had said so.

        A ledger that is merely OLD is not a fault — Axle settle days behind an
        event, and a quiet fortnight with no events is normal. What is a fault is
        nobody being able to tell the difference between "no events lately" and
        "the feed stopped". So the message names the age and says what to do.

        Logs on the first occurrence and hourly thereafter, and logs an explicit
        recovery line when it clears — silence on recovery would leave the last
        word on the subject being an error.
        """
        prev = self.store.get("vpp_ledger_stale") or ""
        self.store["vpp_ledger_stale"] = problem or ""

        if problem:
            # ONCE, on change. Not hourly: nothing here can fix itself without a
            # human, so a repeat is a nag rather than news - it fired five times
            # in one hour on 05-Sep-2026 and would have done so for ever. The
            # current state is on the device as ledgerStatus for anyone looking.
            if problem != prev:
                vpp_log(f"[VPP] Earnings ledger not being fed - {problem}. Import the "
                    f"latest figures with Plugins > SigenEnergyManager > Import Axle "
                    f"Ledger, or check the Axle account page.", "WARNING")
        else:
            if prev:
                vpp_log("[VPP] Earnings ledger is being fed again - fresh figures "
                        "imported.", event=True)
            self.store["vpp_ledger_logged"] = 0.0

        # Push the change to the device the moment it happens, rather than waiting
        # for the next VPP poll. _update_vpp_device runs from _poll_vpp at tick
        # stage 6 and this check is stage 12b, so on the tick that first finds the
        # ledger stale the device has ALREADY been written with the old verdict —
        # and the next VPP poll is up to ten minutes away while idle. That left the
        # log saying "not being fed" and the control page saying "being fed" at the
        # same moment, which is worse than either alone. Same stage-ordering trap as
        # the midnight rollover reading state a later stage owns.
        # Only on a CHANGE, so this costs nothing on the overwhelming majority of
        # checks, and guarded so a device write can never break status recording.
        if (problem or "") != prev:
            try:
                self._update_vpp_device()
            except Exception as exc:
                self.logger.debug(f"[VPP] Ledger status device refresh skipped: {exc}")

    def _check_ledger_freshness(self):
        """Compare the ledger's age against the threshold and report through
        _record_vpp_ledger_status. Pure local work — the summary is served from
        an mtime-keyed cache, so this costs nothing on a quiet tick.

        NEVER IMPORTED is reported distinctly from OLD. An empty ledger has an
        age of None, and treating that as fresh would be the absent-state fault
        this estate keeps meeting: a feed that has never delivered once would
        read healthier than one a day late.
        """
        if not self.pluginPrefs.get("axleEnabled", False):
            self._record_vpp_ledger_status("")   # not an Axle user; nothing to feed
            return

        try:
            summary = self._vpp_ledger_summary() or {}
        except Exception as exc:
            self.logger.debug(f"[VPP] Ledger freshness check skipped: {exc}")
            return

        # The verdict itself lives in vpp_ledger.ledger_problem, so a test can
        # drive it directly - see the note there. This method's remaining job is
        # to fetch the summary, read the pref and report.
        limit_days = _as_float(self.pluginPrefs.get("ledgerStaleDays"), 7.0)
        self._record_vpp_ledger_status(_vpp_ledger.ledger_problem(summary, limit_days))

    def _merge_axle_payload(self, payload):
        """Merge one Axle account payload into the ledger. Returns added count.

        Lifted out of importAxleLedger in v5.92.0 so there is ONE merge path.
        The plugin's own comment beside the ledger has always said Axle's rows
        "arrive through ONE importer", but the load/merge/save/invalidate
        sequence lived inline in a menu callback — so any automated feed would
        have had to copy it, including the cache reset that three dashboard
        pages depend on. Now the feed is a call, not a second copy.

        REFUSES a ledger that failed to load. save_ledger drops the load_error
        marker and rewrites the file wholesale, so merging over an unparseable
        ledger would destroy both the evidence and whatever was still in it.
        """
        path   = self._vpp_ledger_path()
        ledger = _vpp_ledger.load_ledger(path)
        if ledger.get("load_error"):
            raise RuntimeError(
                f"refusing to merge over a ledger that did not parse "
                f"({ledger['load_error']}) - fix or move {path} first")
        ledger, added = _vpp_ledger.import_axle_payload(ledger, payload)
        _vpp_ledger.save_ledger(path, ledger)
        self._vpp_ledger_cache = None
        # A successful merge is exactly what clears a staleness warning, and
        # waiting up to six hours for the next tick to notice would leave the
        # log contradicting the screen.
        self._check_ledger_freshness()
        # New settlement figures are exactly when a divergence becomes knowable.
        try:
            self._check_settlement_divergence()
        except Exception as exc:
            self.logger.debug(f"[VPP] Settlement divergence check skipped: {exc}")
        return added


    def _poll_axle_account(self):
        """Read the Axle account directly, in two layers.

        OFF BY DEFAULT, like the mail scan and for the same reason: this is a
        published plugin and it reads the user's own Mail store to find the
        sign-in link Axle put there. Nothing is stored and nothing is typed -
        the link expires on its own after seven days, and an event always sends
        a fresh one on the day it finishes.

        THE TWO LAYERS ARE DELIBERATELY NOT ONE CALL. `events` is ordinary JSON
        from Axle's own published API and tells us WHICH events are settled;
        the account payload carries the money but rides on a web framework's
        internal encoding. Fetching events FIRST and merging it regardless
        means that when the money fetch breaks - and one day it will - the
        ledger still knows a settled event exists, `_check_unsettled_money`
        still speaks, and the failure is loud instead of silent.
        """
        if not _as_bool(self.pluginPrefs.get("axleReadAccount"), False):
            return
        if not _as_bool(self.pluginPrefs.get("axleEnabled"), False):
            return

        found = _axle_account.newest_live_token()
        if not found:
            self._note_account_problem(
                "no live Axle sign-in link was found in Apple Mail on this Mac - they "
                "arrive with every Axle email and last a week, so opening your account "
                "page from any of their emails will supply a fresh one")
            return
        token, expires, site_id = found

        events = _axle_account.fetch_events(site_id, token)
        if events["events"] is None:
            self._note_account_problem(events["note"])
            return

        # Layer 1 lands on its own, before the fragile half is even attempted.
        try:
            self._merge_axle_payload({"events": events["events"]})
        except Exception as exc:
            self._note_account_problem(f"the event list could not be filed ({exc})")
            return
        self._note_account_problem("")
        vpp_log(f"[VPP] Axle account read: {len(events['events'])} event(s), "
                f"sign-in link good until {_local_time(expires, '%d/%m %H:%M')}.")

        # Layer 2. A failure here is reported and survivable - layer 1 has
        # already told the ledger which events Axle consider settled.
        account = _axle_account.fetch_account(site_id, token)
        if account["payload"] is None:
            self._note_money_problem(account["note"])
        else:
            try:
                added = self._merge_axle_payload(account["payload"])
                self._note_money_problem("")
                if added:
                    vpp_log(f"[VPP] Axle account read brought in {added} new "
                            f"settlement figure(s).", event=True)
            except Exception as exc:
                self._note_money_problem(f"the account figures could not be filed ({exc})")

        try:
            self._check_unsettled_money()
        except Exception as exc:
            self.logger.debug(f"[VPP] Unsettled-money check skipped: {exc}")

    def _note_account_problem(self, problem):
        """Report a failing account read, once and then on change only."""
        prev = self.store.get("axle_account_problem") or ""
        self.store["axle_account_problem"] = problem or ""
        if problem:
            if problem != prev:
                vpp_log(f"[VPP] Axle account could not be read - {problem}.", "WARNING")
        elif prev:
            vpp_log("[VPP] Axle account is readable again.", event=True)

    def _note_money_problem(self, problem):
        """Report a failing MONEY read separately from a failing account read.

        Separate because the remedies differ and lumping them would hide the
        interesting case: the event list working while the figures do not means
        the account payload's encoding has moved, which is a plugin fix, not
        anything the user can do.
        """
        prev = self.store.get("axle_money_problem") or ""
        self.store["axle_money_problem"] = problem or ""
        if problem:
            if problem != prev:
                vpp_log(f"[VPP] Axle settled figures could not be read - {problem}. "
                        f"Which events have been settled is still known, so nothing "
                        f"is lost track of.", "WARNING")
        elif prev:
            vpp_log("[VPP] Axle settled figures are readable again.", event=True)

    def _check_unsettled_money(self):
        """Warn when Axle say an event is settled but we hold no figure for it.

        THE POINT OF THIS CHECK IS THAT IT IS POSITIVE. It rests on Axle's own
        `settled_via`, so it is not inferring a fault from an absence - it is
        repeating back something Axle have stated. That is what makes it able
        to catch the 12-Sep-2026 fault, where the feed was dead, every staleness
        measure read healthy, and the only evidence anywhere was Axle saying
        "settled" about two events we had no money for.

        Latched per window, so a figure that genuinely never arrives says so
        once rather than every six hours.
        """
        ledger  = _vpp_ledger.load_ledger(self._vpp_ledger_path())
        if ledger.get("load_error"):
            return
        missing = _axle_account.settled_without_figures(ledger)
        seen    = self.store.setdefault("vpp_settled_unpaid_seen", set())

        for start in missing:
            if start in seen:
                continue
            seen.add(start)
            # settled_without_figures returns ISO strings; _local_time wants a
            # datetime, and an unparseable one must not stop the warning.
            try:
                when = _local_time(datetime.fromisoformat(start), "%d %b %Y %H:%M")
            except (TypeError, ValueError):
                when = str(start)
            vpp_log(f"[VPP] Axle have settled the {when} event but no payment figure has "
                    f"reached the ledger, so the earnings shown are short. Open your Axle "
                    f"account page to refresh the sign-in link, or import the figures by "
                    f"hand.", "WARNING")

        # Drop anything that has since been paid, so a later recurrence warns again.
        for start in list(seen):
            if start not in missing:
                seen.discard(start)

    def _scan_axle_mail(self):
        """Import any Axle settlement mail sitting in the local Mail store.

        OFF BY DEFAULT, and it must stay that way. This plugin is published, and
        reading someone's personal mail store is not a thing to start doing
        because they upgraded. The pref says plainly what it reads.

        There is no mailbox login here and no credential anywhere: Apple Mail has
        already downloaded the messages under the user's own account, and the
        plugin host can read them. That is the whole reason to prefer this over
        an IMAP account — nothing to store, nothing to rotate, nothing to leak.

        Re-importing is free: the merge is keyed on the event window, so the same
        message parsed a hundred times still yields one row. That is deliberate,
        because it means no bookkeeping of what has already been seen can drift.
        """
        if not _as_bool(self.pluginPrefs.get("axleScanMail"), False):
            return
        if not _as_bool(self.pluginPrefs.get("axleEnabled"), False):
            return

        roots = _axle_email.mail_roots()
        if not roots:
            self._note_mail_scan_problem(
                "no Apple Mail store found on this Mac, so there is nothing to read")
            return

        seen = imported = refused = 0
        for sender, subject, body, received in _axle_email.iter_settlement_messages():
            seen += 1
            note = self._ingest_settlement_email(sender, subject, body, received)
            if note:
                refused += 1
            else:
                imported += 1

        if seen == 0:
            self._note_mail_scan_problem(
                "no Axle settlement emails found in the Mail store - check Mail is "
                "still downloading them, and that this Mac holds the account they go to")
        else:
            self._note_mail_scan_problem("")
            self.logger.debug(f"[VPP] Mail scan: {seen} settlement message(s), "
                              f"{imported} usable, {refused} not.")

    def _note_mail_scan_problem(self, problem):
        """Report a mail scan that is finding nothing, once and then hourly.

        A scan that silently returns nothing is indistinguishable from a quiet
        month of no events - the same trap the events feed and the ledger have
        each been caught by, so it gets the same treatment.
        """
        prev = self.store.get("mail_scan_problem") or ""
        self.store["mail_scan_problem"] = problem or ""
        if problem:
            if problem != prev:      # once, on change - see _record_vpp_ledger_status
                vpp_log(f"[VPP] Axle mail scan found nothing - {problem}.", "WARNING")
        else:
            if prev:
                vpp_log("[VPP] Axle mail scan is finding settlement emails again.",
                        event=True)
            self.store["mail_scan_logged"] = 0.0

    def _check_settlement_divergence(self):
        """Warn when a paid window did not go the way we think it did.

        WHY. On 21-May-2026 a BST/UTC mix-up drove the export a full hour late.
        Axle settled 0.04 kWh; the only reason anything was earned is that the
        driver starts two minutes before the window opens, so a sliver landed
        inside it. Nothing compared what we drove against what was paid, so it
        surfaced weeks later when a human read the settlement email.

        THE FIRST VERSION OF THIS CHECK COULD NOT HAVE CAUGHT THAT, and the
        ledger proves it. It compared our in-window export against Axle's
        settled figure - but both are integrated over the SAME hour, so when the
        export runs at the wrong time both fall together and agree. Run against
        the real 11-Aug-2026 over-run, where the window failed to stop and
        3.052 kWh went outside the paid hour, that comparison returns 0.198 kWh
        - dead centre of the healthy band. A meter-agreement check wearing a
        timing check's name, and worse than nothing because silence reads as
        confidence.

        The question that actually matters is: DID WHAT WE DROVE LAND INSIDE THE
        PAID HOUR? That is run_kwh against window_kwh, both of which the ledger
        has held all along. Measured over the eight driven events on disk:

            healthy   92.5% to 97.0% inside, gap 0.226 to 0.323 kWh
            11-Aug    56.7% inside, gap 3.052 kWh

        The gap is the driver's deliberate two minutes either side, so it is
        roughly constant while the fraction is scale-free - which matters,
        because a 30-minute event is worth at most 2 kWh and any flat kWh gate
        big enough to clear the noise would swallow the whole event. Both
        conditions must hold: a real gap AND a poor fraction. Either alone
        false-alarms, on a tiny event and on a long clean one respectively.

        Four distinct faults, each said in its own words rather than lumped:
          1. we exported outside the paid window (the 21-May fault);
          2. Axle's meter and ours disagree about the same hour;
          3. Axle settled an event we have no measurement of at all;
          4. we drove a window but cannot tell how much landed inside it.
        """
        try:
            path   = self._vpp_ledger_path()
            ledger = _vpp_ledger.load_ledger(path)
            # NOT _vpp_ledger_summary(): that caps at the 12 newest events while
            # this looks back 45 days, so a busy spell would push an unsettled
            # event off the list before its figure arrived.
            summary = _vpp_ledger.summarise(ledger, recent=500)
        except Exception as exc:
            self.logger.debug(f"[VPP] Settlement check skipped: {exc}")
            return

        warned = self.store.setdefault("settlement_warned", set())
        before = len(warned)
        cutoff = datetime.now(timezone.utc) - timedelta(days=SETTLEMENT_CHECK_DAYS)

        for ev in (summary.get("events") or []):
            key = str(ev.get("start"))[:16]
            if not key or key in warned:
                continue
            # A hand-typed drop-file row can carry a naive timestamp. Comparing
            # naive to aware raises, and the caller swallows it to DEBUG - so one
            # bad row would silently stop the whole guard. Treat naive as UTC,
            # which is what vpp_ledger's own parser does.
            try:
                start = datetime.fromisoformat(str(ev.get("start")))
            except (TypeError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start < cutoff:
                continue

            run   = ev.get("run_kwh")
            ours  = ev.get("our_kwh")
            paid  = ev.get("paid_kwh")
            diff  = ev.get("diff_kwh")
            when  = ev.get("start_local") or _local_time(start, "%d/%m %H:%M")

            # 1. THE TIMING FAULT. Scale-free, so a short event is covered.
            if run and ev.get("in_window") and ours is not None:
                gap = run - ours
                inside = (ours / run) if run > 0 else 1.0
                if gap >= SETTLEMENT_OUTSIDE_KWH and inside < SETTLEMENT_INSIDE_FRACTION:
                    warned.add(key)
                    vpp_log(
                        f"[VPP] Most of an export missed its paid hour. On {when} we sent "
                        f"out {run:.2f} kWh altogether but only {ours:.2f} kWh of it landed "
                        f"inside the window Axle pay for - {inside * 100:.0f}%, where a normal "
                        f"event is above 92%. The other {gap:.2f} kWh earned the ordinary "
                        f"export rate instead. That usually means the export ran at the wrong "
                        f"time rather than anything being broken on the battery.", "WARNING")
                    continue

            # 2. THE METERS DISAGREE about the same hour. Different fault, and
            #    the flat threshold is right here: the offset is absolute and
            #    does not scale (0.127 to 0.198 kWh across 4 to 8 kWh events).
            if diff is not None and abs(diff) >= SETTLEMENT_DIVERGENCE_KWH:
                warned.add(key)
                short = "less" if diff > 0 else "more"
                vpp_log(
                    f"[VPP] Axle settled a different amount from the one we measured. On "
                    f"{when} we recorded {ours:.2f} kWh inside the paid hour and they "
                    f"settled {paid:.2f} kWh, {abs(diff):.2f} kWh {short}. The seven events "
                    f"before this agreed to within 0.2 kWh, so a gap this size is worth "
                    f"querying with them.", "WARNING")
                continue

            # 3. THEY PAID FOR A WINDOW WE HAVE NO MEASUREMENT OF. No floor on
            #    the amount: a settled row means the event was real, and the
            #    21-May row Axle settled was 0.036 kWh - the very shape a floor
            #    would have muted.
            if ev.get("settled") and paid is not None and ours is None:
                warned.add(key)
                vpp_log(
                    f"[VPP] Axle settled an event this plugin never recorded driving. They "
                    f"paid for {paid:.2f} kWh on {when} and there is no measurement of ours "
                    f"for that window at all, so the export may have been missed altogether "
                    f"or the plugin may have been stopped at the time.", "WARNING")
                continue

            # 4. WE DROVE IT BUT CANNOT CHECK IT. Snapshots lost or too few to
            #    integrate, so our_kwh fell back to the run total and neither
            #    check above can speak. Absent is not agreement.
            if run and not ev.get("in_window"):
                warned.add(key)
                vpp_log(
                    f"[VPP] Cannot tell whether the {when} export landed in its paid hour. "
                    f"We drove {run:.2f} kWh but the per-minute record for that window is "
                    f"missing or too short to add up, so there is nothing to check Axle's "
                    f"figure against.", "WARNING")

        # Persist AT ONCE when the latch grows rather than waiting out the
        # five-minute accumulator tick. A warning raised and then lost to a
        # restart in the gap is a warning raised twice, which is the whole fault
        # being fixed here. Cheap (one small atomic write) and only on a change.
        if len(warned) != before:
            try:
                self._save_accumulators()
            except Exception as exc:
                self.logger.debug(f"[VPP] Latch save skipped: {exc}")

    def _window_for_event_date(self, event_date):
        """(start_iso, end_iso) for the VPP window this plugin drove on a date.

        The settlement email never states the event's clock times in text - in
        the specimen they are drawn inside the chart image, where no parser can
        reach them. It does not matter: we DROVE the window, so we hold it to the
        second in our own ledger rows. The email brings the money, this brings
        the window.

        Matches on the LOCAL calendar date, because that is the date the email
        names ("Sun 16th August"), while the rows are stored in UTC - a summer
        evening event at 19:00 UTC is the 16th in both, but a late one is not.
        """
        try:
            ledger = _vpp_ledger.load_ledger(self._vpp_ledger_path())
        except Exception as exc:
            self.logger.debug(f"[VPP] Window lookup failed: {exc}")
            return None
        # Our own rows first - we drove those windows. Then Axle's own event
        # list, which covers events from BEFORE self-drive shipped and any we
        # did not drive at all (the plugin down, or an event we never saw).
        # Without the second source, a settlement for an event we missed can
        # never be filed, and the money would arrive with nothing recording it.
        rows = list(ledger.get("local") or [])
        rows += [e for e in ((ledger.get("axle") or {}).get("events") or [])
                 if isinstance(e, dict)]
        for row in rows:
            start = row.get("start_time")
            end   = row.get("end_time")
            if not start or not end:
                continue
            try:
                local = _to_london(datetime.fromisoformat(str(start)))
            except (TypeError, ValueError):
                continue
            if local.date() == event_date:
                return (str(start), str(end))
        return None

    def _ledger_has_figure_for(self, start_iso):
        """Does the ledger already hold Axle's own settled figure for this window?

        The question a refusal has to ask before it complains. A message we
        cannot parse matters only when nothing else has supplied the money —
        once the account import has, there is nothing for anyone to do and
        nothing to say.

        A genuine 0p row counts as a figure: Axle record a nil event as a real
        transaction, and treating it as missing would nag for ever about an
        event that settled correctly at nothing.
        """
        if not start_iso:
            return False
        try:
            ledger = _vpp_ledger.load_ledger(self._vpp_ledger_path())
            if ledger.get("load_error"):
                return False        # unknown is not "already held"
            key = str(start_iso)[:16]
            for tx in (ledger.get("axle") or {}).get("transactions") or []:
                if not isinstance(tx, dict):
                    continue
                if (tx.get("transaction_type") or "").strip() != "flex event":
                    continue
                if str(tx.get("start_time") or "")[:16] == key:
                    return True
        except Exception as exc:
            self.logger.debug(f"[VPP] Ledger check skipped: {exc}")
        return False

    def _ingest_settlement_email(self, sender, subject, body, received_at):
        """Parse one Axle settlement email into the ledger. Returns a note.

        Every outcome is logged. A settlement that quietly fails to land is the
        exact failure the ledger-staleness guard exists to end, so "we could not
        use this message" must never be silent either.
        """
        result = _axle_email.parse_settlement_email(
            sender, subject, body, received_at,
            window_lookup=self._window_for_event_date)
        payload, note = result.get("payload"), result.get("note") or ""
        window = result.get("window")

        if payload is None:
            start = window[0] if window else None
            if not note or "not an Axle settlement email" in note:
                self.logger.debug(f"[VPP] Settlement email ignored - {note}")
            elif start and self._ledger_has_figure_for(start):
                # NOTHING TO DO. The mail scan re-reads every message every six
                # hours by design, so a message we can never parse was warning
                # on every pass - 42 times in the week to 12-Sep-2026 - about the
                # 21-May event, whose authentic figure (0.036 kWh, 4p) Axle's own
                # account had already supplied. "Enter it by hand" was not merely
                # noise, it was wrong: the row was there. Suppressed on the
                # EVIDENCE OF SUCCESS (a settled figure for that window), never on
                # the identity of the message, so a window that later loses its
                # figure starts warning again.
                self.logger.debug(
                    f"[VPP] Settlement email unreadable but already settled - {note}")
            else:
                # Genuinely missing. Say it ONCE per window rather than every
                # scan: the fault is real but static, and an amber line every six
                # hours only teaches the eye to skim them.
                seen = self.store.setdefault("vpp_email_refusal_seen", set())
                key  = start or note
                if key not in seen:
                    seen.add(key)
                    vpp_log(f"[VPP] Settlement email not imported - {note}"
                            + (" - enter it by hand if you want this event recorded."
                               if start else "."), "WARNING")
                else:
                    self.logger.debug(f"[VPP] Settlement email still not importable - {note}")
            return note

        # A window that has just been filed is no longer an outstanding refusal.
        if window and window[0]:
            self.store.setdefault("vpp_email_refusal_seen", set()).discard(window[0])

        try:
            added = self._merge_axle_payload(payload)
        except Exception as exc:
            vpp_log(f"[VPP] Settlement email could not be filed: {exc}", "ERROR")
            return str(exc)

        tx = payload["transactions"][0]
        # _local_time takes a datetime; the row carries an ISO string.
        try:
            when = _local_time(datetime.fromisoformat(tx["start_time"]), "%d/%m %H:%M")
        except (TypeError, ValueError):
            when = str(tx["start_time"])[:16]
        vpp_log(f"[VPP] Settlement email imported - Axle settled "
            f"{abs(tx['flex_kwh']):.2f} kWh at {when} for GBP "
            f"{tx['credit_pence'] / 100:.2f}"
            + ("." if added else ", which we already held."))
        return ""

    def actionIngestAxleSettlementEmail(self, action, dev=None):
        """Action: read the newest message off an Email+ IMAP device and file it.

        The message is read from the device's OWN states rather than passed in
        through action props. Cross-plugin prop serialisation drops keys (the
        Email+ sendEmail bug, 09-Apr-2026), and a settlement figure arriving
        blank would be filed as a real one.
        """
        props = getattr(action, "props", {}) or {}
        try:
            dev_id = int(props.get("emailDeviceId") or 0)
        except (TypeError, ValueError):
            dev_id = 0
        if not dev_id or dev_id not in indigo.devices:
            vpp_log("[VPP] Import Axle Settlement Email: no Email+ device chosen. Open the "
                "action and pick the IMAP device carrying the message.", "ERROR")
            return

        email_dev = indigo.devices[dev_id]
        if not email_dev.enabled:
            vpp_log(f"[VPP] Import Axle Settlement Email: '{email_dev.name}' has its "
                f"communication disabled, so its states are frozen at whatever they "
                f"held when it was switched off. Enable it first.", "ERROR")
            return

        st = email_dev.states
        received = datetime.now(timezone.utc)
        raw_date = st.get("messageDate") or ""
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(str(raw_date).strip()[:31], fmt)
                received = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                break
            except (ValueError, TypeError):
                continue

        self._ingest_settlement_email(st.get("messageFrom", ""), st.get("messageSubject", ""),
                                      st.get("messageText", ""), received)

    def _poll_vpp(self):
        """Poll Axle API and advance VPP state machine."""
        if not self.pluginPrefs.get("axleEnabled", False):
            return
        if not self.axle:
            self._record_vpp_api_status("No API token configured")
            return
        # While the manager is paused the VPP state machine must not advance — pause
        # already stood down any active window and disengaged the inverter, so a
        # pre-charge (force-charge + raised cutoff) or a new window starting here
        # would drive the hardware behind a 'Paused' label. Resume re-detects an
        # in-progress Axle window via the late-detection path.
        if self.store.get("manager_paused", False):
            return

        event = self.axle.get_next_event()   # NETWORK — unlocked (v5.45.0)
        self._record_vpp_api_status(self.axle.last_error)
        with self._state_lock:
            self._apply_vpp_event(event)

    def _check_vpp_overrun(self):
        """Force-end a VPP window that is running long past its own end time.

        The SECOND, independent guard on the export (v5.62.0). See the call site
        in _evaluate_manager_impl for why one is not enough: the manager re-drives
        ACTION_VPP_EXPORT from the `vpp_active` boolean alone and never consults
        the clock, so every route that stops _poll_vpp reaching its end test leaves
        the export running for ever.

        Deliberately conservative:
          * acts ONLY past our own STORED end + VPP_OVERRUN_GRACE_MINS, so it can
            never truncate a live window — and it uses the stored event for the
            same reason v5.61.1 does, never anything an API just handed back;
          * an unparseable or missing end time does NOTHING (a guess here would be
            worse than the fault it guards);
          * ends through _end_vpp_export, the same path the poll uses, so the
            summary, the JSONL and the state machine all land exactly as normal;
          * logs at WARNING naming the overshoot, because reaching this line at
            all means the primary path failed and that is worth knowing.
        """
        try:
            if self.store.get("vpp_state") != VPP_ACTIVE:
                return
            stored = self.store.get("vpp_event") or {}
            end_time = stored.get("end_time")
            if end_time is None:
                return
            now = datetime.now(timezone.utc)
            deadline = end_time + timedelta(minutes=VPP_OVERRUN_GRACE_MINS)
            if now < deadline:
                return
            overshoot = (now - end_time).total_seconds() / 60.0
            vpp_log(f"[VPP] BACKSTOP — window ended {_local_time(end_time)} but the export "
                f"is still running {overshoot:.0f} min later. The Axle poll has not "
                f"closed it, so the manager is force-ending it now. Check why "
                f"_poll_vpp stopped advancing the state machine.", level="WARNING")
            self._end_vpp_export(now, stored)
        except Exception as exc:
            # Never let the backstop break the evaluate it is protecting.
            vpp_log(f"[VPP] Over-run backstop failed: {exc}", level="ERROR")

    def _apply_vpp_event(self, event):
        """Advance the VPP state machine for a fetched event. Caller holds the lock."""
        now           = datetime.now(timezone.utc)
        current_state = self.store["vpp_state"]

        # Direction guard (v5.57.0 — closes the item v5.28.0 recorded as "noted
        # only"). Everything below self-drives an EXPORT: announce, pre-charge,
        # raise the cutoff, then night_export/daytime_export for the window.
        # Axle's API carries an import_export field and has only ever sent
        # "export", but nothing here checked it — an IMPORT event would have
        # been driven as a full export, pushing 4 kW OUT through the very window
        # the grid wants energy IN, draining the battery for a dispatch that
        # settles against us. Treat a non-export event exactly like "no event":
        # the None branch already stands down any pre-window state cleanly, and
        # an ACTIVE window keeps driving to its own stored end (our stored
        # window is always an export one — a non-export event can never reach
        # ACTIVE). Warn ONCE per event (latched on its start time — the poll
        # repeats every 10 minutes, potentially for hours of lead time).
        if event is not None:
            direction = str(event.get("import_export") or "export").strip().lower()
            if direction != "export":
                start_key = str(event.get("start_time"))
                if self.store.get("vpp_direction_warned") != start_key:
                    self.store["vpp_direction_warned"] = start_key
                    vpp_log(f"[VPP] Axle announced a '{direction}' event "
                        f"({_local_time(event['start_time'])}-"
                        f"{_local_time(event['end_time'])}) — this plugin only "
                        f"self-drives EXPORT windows, so it will NOT be driven. "
                        f"If Axle have started sending import events, that needs "
                        f"building, not assuming.", level="WARNING")
                event = None

        if event is None:
            # Axle API returns None when no event is scheduled or the event has ended

            if current_state == VPP_ACTIVE:
                # We self-drive on our OWN stored window — the API returning None
                # usually just means the event ended, but only stop once we are past
                # our stored end_time + 2-min tail, so a transient API blip mid-event
                # cannot cut the export short.
                stored   = self.store.get("vpp_event") or {}
                end_time = stored.get("end_time")
                if end_time is not None and now < end_time + timedelta(minutes=2):
                    self._log_vpp_snapshot(stored)
                    self._update_vpp_device()
                    return
                self._end_vpp_export(now, stored)

            elif current_state != VPP_IDLE:
                # Event cancelled / disappeared before the window opened — stand down.
                vpp_log("[VPP] Event cancelled/disappeared - restoring self-consumption")
                self._restore_discharge_cutoff()
                if self.store.get("export_active") and self.modbus:
                    self.modbus.set_self_consumption()
                self.store["export_active"]      = False
                self.store["vpp_charge_stopped"] = False
                self._vpp_transition(VPP_IDLE)
                self.store["vpp_active"] = False

            self._update_vpp_device()
            return

        # Event is scheduled
        start_time     = event["start_time"]
        end_time       = event["end_time"]
        hours_to_start = (start_time - now).total_seconds() / 3600.0

        if current_state == VPP_IDLE and hours_to_start > 0:
            self.store["vpp_event"] = event
            self.store["vpp_10min_warning_sent"]  = False  # latched per event
            # Discharge cutoff is NOT raised here — it is raised at pre-charge time
            # (30 min before event). Raising it at announcement (up to 24h early)
            # locks the battery below the floor if SOC is low, causing unnecessary
            # grid imports. Solar will always restore SOC before the event; if not,
            # Axle's own firmware will decline to dispatch.
            self._vpp_transition(VPP_ANNOUNCED)
            self._trigger_event("vppAnnounced")
            vpp_log(
                f"[VPP] Event announced: {_local_time(start_time)} - "
                f"{_local_time(end_time)} BST ({event['duration_hrs']:.1f}h)"
            )
            # Persist every field Axle's API returned to per-event JSONL file
            # under <data_dir>/vpp_events/. Read after the event with eg
            #   jq -c 'select(.type=="announcement")' <file>
            # to inspect the dispatch metadata.
            self._write_vpp_event_header(event)

        elif current_state == VPP_IDLE and hours_to_start <= 0 and now < end_time:
            # Axle published the event late — it's already under way.
            # Skip straight to ACTIVE; Axle's firmware already has control.
            mins_late = int(-hours_to_start * 60)
            vpp_log(
                f"[VPP] Late detection: event already active {_local_time(start_time)} - "
                f"{_local_time(end_time)} BST (Axle published {mins_late} min late) — "
                f"entering ACTIVE, self-driving export"
            )
            self.store["vpp_event"] = event
            self.store["vpp_export_start_kwh"] = self.store["grid_export_daily_kwh"]
            # The announced path writes the JSONL header at announcement; this path
            # never announced, so write it here — otherwise the event's snapshot
            # file opens with no announcement record and the post-event analysis
            # loses the dispatch metadata (v5.57.0).
            self._write_vpp_event_header(event)
            # Pre-charge was skipped, so set the discharge cutoff here too — otherwise
            # vpp_cutoff_raised stays False and _verify_ems_registers would reset the
            # cutoff to the health floor mid-window, letting a late-detected NIGHT event
            # over-discharge below the dawn reserve.
            self._set_vpp_discharge_cutoff(event, is_daytime=self._event_is_daytime(start_time))
            self._vpp_transition(VPP_ACTIVE, preempted=True)
            self.store["vpp_active"] = True
            self._trigger_event("vppStarted")

        elif current_state == VPP_ANNOUNCED:
            # Single T-10min warning instead of the old minute-by-minute spam.
            # Fires once when we cross 10 min; latched via vpp_10min_warning_sent.
            if hours_to_start <= 10.0 / 60.0 and not self.store.get("vpp_10min_warning_sent"):
                vpp_log(f"[VPP] Event in 10 minutes — Axle handover at T-5min, "
                    f"event window {_local_time(start_time)}-{_local_time(end_time)}")
                self.store["vpp_10min_warning_sent"] = True
            # Existing pre-charge trigger at T-30min (unchanged)
            if hours_to_start <= 0.5:
                self._start_vpp_precharge(event)

        elif current_state == VPP_PRE_CHARGING:
            required_soc       = self.store["vpp_pre_charge_soc"]
            current_soc        = self.latest_inverter_data.get("batterySoc", 0.0)
            charge_stopped     = self.store.get("vpp_charge_stopped",   False)
            soc_ready          = current_soc >= required_soc

            # Step 1: stop charging once SOC target is reached (fire once only).
            # Guard against fighting Axle: if 40031 already reads 0x06
            # (Discharge ESS first) then Axle is mid-dispatch and we must
            # not overwrite it with 0x02. Battery is already not charging
            # in that case, so the "stop charging" intent is satisfied.
            if soc_ready and not charge_stopped:
                cur_mode = self.modbus.read_ems_mode() if self.modbus else None
                if cur_mode == 0x06:
                    self.store["vpp_charge_stopped"] = True
                    vpp_log(f"[VPP] Pre-charge complete — SOC {current_soc:.0f}% >= "
                        f"{required_soc:.0f}% target. Axle already dispatching "
                        f"(40031=0x06) — leaving inverter under Axle control.")
                elif self.modbus:
                    self.modbus.set_self_consumption()
                    self.store["vpp_charge_stopped"] = True
                    vpp_log(f"[VPP] Pre-charge complete — SOC {current_soc:.0f}% >= "
                        f"{required_soc:.0f}% target. Holding in Self Consumption.")

            # Start the self-driven export 2 min BEFORE the window opens, so we are
            # already exporting by the time Axle's meter window begins (v5.28).
            if hours_to_start <= 2.0 / 60.0:
                self.store["vpp_export_start_kwh"]  = self.store["grid_export_daily_kwh"]
                self.store["vpp_charge_stopped"]    = False
                self._vpp_transition(VPP_ACTIVE)
                self.store["vpp_active"] = True
                self._trigger_event("vppStarted")
                vpp_log("[VPP] T-2min — self-driving export for the window "
                    "(ignoring Axle dispatch; meter-settled).")

        elif current_state == VPP_ACTIVE:
            # THE END IS JUDGED AGAINST OUR OWN STORED WINDOW, NEVER THE EVENT THE
            # API JUST HANDED BACK. Axle publish the NEXT event within a minute of
            # one finishing, so `event` here can be TOMORROW's window while we are
            # still driving tonight's — and reading its end_time made the stop test
            # `now >= <tomorrow> + 2min`, which is false for a further ~21 hours.
            # Live-hit 11-Aug-2026: tonight's 19:30-20:30 window did not stop at
            # 20:32; at 20:31 the API returned the 12-Aug event and the plugin kept
            # exporting 4 kW from the battery, with a 1% discharge floor and nothing
            # to halt it before the pack was flat. Snapshots went into the NEXT
            # event's file at elapsed -1288 min, which is how it was spotted.
            # The `event is None` branch above has always used the stored window and
            # says why; this branch simply never got the same care.
            stored     = self.store.get("vpp_event") or event
            stored_end = stored.get("end_time") or end_time
            # Snapshot against the stored window too, so the readings land in the
            # running event's file with a sane elapsed figure.
            self._log_vpp_snapshot(stored)
            if now >= stored_end + timedelta(minutes=2):
                # Our timer drives the stop (+2-min tail past the window). We do not
                # wait for Axle to release anything — we never handed it over.
                # A future event returned above is picked up on the next poll, once
                # this transition has put us back in IDLE.
                self._end_vpp_export(now, stored)

        self._update_vpp_device()

    def _vpp_event_log_path(self, event):
        """Return per-event JSONL file path under <data_dir>/vpp_events/.

        Filename derived from event start_time, e.g. 2026-05-15_0800.jsonl.
        Used both at announcement (to write the event header) and during
        VPP_ACTIVE (to append per-minute snapshots). Designed so the file
        can be read after the event to see exactly what Axle did with the
        inverter — without flooding the Indigo Event Log.
        """
        try:
            start = event.get("start_time")
            if hasattr(start, "strftime"):
                stamp = start.strftime("%Y-%m-%d_%H%M")
            else:
                stamp = str(start).replace(":", "")[:13]
        except Exception:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
        dir_path = os.path.join(self.data_dir, "vpp_events")
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception:
            pass
        return os.path.join(dir_path, f"{stamp}.jsonl")

    def _write_vpp_event_header(self, event):
        """Write the announcement record (every field Axle returned) to the
        per-event JSONL file. Called once when the event transitions to
        VPP_ANNOUNCED."""
        import json as _json
        path = self._vpp_event_log_path(event)
        record = {"type": "announcement",
                  "logged_at": datetime.now(timezone.utc).isoformat()}
        try:
            for k, v in event.items():
                if hasattr(v, "isoformat"):
                    v = v.isoformat()
                try:
                    _json.dumps(v)
                    record[k] = v
                except (TypeError, ValueError):
                    record[k] = str(v)
        except Exception:
            pass
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(record) + "\n")
            vpp_log(f"[VPP] Event log: {path}")
        except Exception as exc:
            vpp_log(f"[VPP] Could not write event header: {exc}", level="WARNING")

    def _vpp_driver(self, ems_mode):
        """Who is actually driving the inverter, judged from the live register.

        The old field was `"self" if store["export_active"] else "axle"` — it
        mirrored our own intent flag, so it could only ever read "self" once we
        started driving and "axle" before. It never looked at the hardware, and
        therefore could not answer the one question it existed to answer.

        Compare the LIVE mode register against the mode we last wrote instead.
        While we hold Remote EMS nothing else should be able to move it, so a
        mode we did not write means something external did — the signal that
        Axle's cloud dispatch has reached the inverter (or that a stray write
        has). Returns:
          "self"             — register matches the mode we wrote
          "external"         — register holds a mode we did not write
          "self (unverified)" — Modbus read failed, or no mode written yet
          "idle"             — not driving an export right now
        """
        if not self.store.get("export_active"):
            return "idle"
        expected = self.store.get("vpp_export_mode")
        if ems_mode is None or expected is None:
            return "self (unverified)"
        return "self" if ems_mode == expected else "external"

    def _log_vpp_snapshot(self, event):
        """Append one per-minute snapshot of inverter state to the per-event
        JSONL file. Captures everything we'd need to reconstruct what Axle
        did: power flows, EMS mode + limits, SOC. NOT logged to the Indigo
        Event Log — that stays clean.
        """
        import json as _json
        now_ts = time.time()
        if now_ts - self.store.get("vpp_last_snapshot_at", 0) < 55:
            return
        self.store["vpp_last_snapshot_at"] = now_ts

        ems_mode = chg_lim = dis_lim = None
        try:
            inv = self.latest_inverter_data or {}
            ems_mode = self.modbus.read_ems_mode()        if self.modbus and self.modbus.connected else None
            chg_lim  = self.modbus.read_charge_limit()    if self.modbus and self.modbus.connected else None
            dis_lim  = self.modbus.read_discharge_limit() if self.modbus and self.modbus.connected else None

            try:
                start = event["start_time"]
                now_dt = datetime.now(timezone.utc)
                elapsed = (now_dt - start).total_seconds()
            except Exception:
                elapsed = None

            ems_mode_names = {0x00: "PCS Remote Control", 0x01: "Standby",
                              0x02: "Max Self Consumption", 0x03: "Charge Grid First",
                              0x04: "Charge PV First", 0x05: "Discharge PV First",
                              0x06: "Discharge ESS First", 0x07: "Reserved", 0x08: "V2G"}
            record = {
                "type":               "snapshot",
                "logged_at":          datetime.now(timezone.utc).isoformat(),
                "event_elapsed_secs": elapsed,
                "soc_pct":            inv.get("batterySoc"),
                "pv_w":               inv.get("pvPowerWatts"),
                "battery_w":          inv.get("batteryPowerWatts"),
                "home_w":             inv.get("homePowerWatts"),
                "grid_w":             inv.get("gridPowerWatts"),
                "ems_work_mode":      inv.get("emsWorkMode"),
                "ems_mode_register":  ems_mode,
                "ems_mode_name":      ems_mode_names.get(ems_mode) if ems_mode is not None else None,
                "driver":             self._vpp_driver(ems_mode),
                "charge_limit_w":     chg_lim,
                "discharge_limit_w":  dis_lim,
                "grid_status":        inv.get("gridStatus"),
                "battery_temp_c":     inv.get("batteryTempC"),
                "battery_cell_v":     inv.get("batteryCellVoltage"),
                "plant_state":        inv.get("plantRunningState"),
            }
            path = self._vpp_event_log_path(event)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(record) + "\n")
        except Exception:
            pass   # snapshot is best-effort, never raise during active event

        # v5.91.0: ACT on the three registers just read, do not merely file them.
        # Until now this method read the mode, the charge limit and the discharge
        # limit every minute of every window and did nothing but write them to
        # JSONL — so a drift arriving between manager cycles sat in the file,
        # recorded and uncorrected, until verify's next pass. Reusing the values
        # costs no extra Modbus transaction, and the two callers interleave
        # (measured 05-Sep-2026: 20-45s apart), so the effective check rate
        # roughly doubles for free.
        # Separate try: a fault in the check must not cost us the snapshot that
        # was the whole point of the read, and vice versa.
        try:
            self._verify_vpp_export_registers(ems_mode, chg_lim, dis_lim)
        except Exception as exc:
            self.logger.debug(f"[VPP] Snapshot-side register check skipped: {exc}")

    def _summarise_vpp_event(self, event):
        """Parse the per-event JSONL file we just closed, compute summary
        stats, write them to the axleVppMonitor device states, and fire a
        Pushover with the headline numbers plus a pre-formed Claude prompt
        the user can paste straight into Claude Code.

        Called once, at the VPP_ACTIVE -> COOLING_OFF transition.  Best-effort:
        never raise — any failure here must not interfere with the cool-off
        state machine.
        """
        import json as _json
        try:
            path = self._vpp_event_log_path(event)
        except Exception:
            path = ""

        # Parse all snapshot records out of the JSONL file
        snapshots   = []
        ended       = None
        foreign     = 0     # snapshots belonging to a different window (see below)
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = _json.loads(line)
                        except Exception:
                            continue
                        rtype = rec.get("type")
                        if rtype == "snapshot":
                            # Only snapshots belonging to THIS window (v5.63.0).
                            # A file can hold foreign snapshots: on 11-Aug-2026 an
                            # over-running window wrote 31 of them into the NEXT
                            # event's file at elapsed -1288 min, because the driver
                            # was logging against the event the API had just
                            # returned. v5.61.1 stopped that at source, but the
                            # summariser had no defence of its own and would have
                            # mixed a previous night's readings into the next
                            # event's peak grid export, min PV and mode list — a
                            # confidently wrong report is worse than a missing one.
                            # The bound is deliberately loose: the driver runs
                            # T-2min to end+2min, so anything inside +/-15 min of
                            # the window is legitimate and kept.
                            if _snapshot_in_window(rec, event):
                                snapshots.append(rec)
                            else:
                                foreign += 1
                        elif rtype == "announcement":
                            pass  # announcement records are not surfaced here
                        elif rtype == "event_ended":
                            ended = rec
            except Exception as exc:
                vpp_log(f"[VPP] Could not parse event log {path}: {exc}",
                    level="WARNING")
        if foreign:
            # Never silent: a file holding another window's readings means
            # something upstream filed them wrongly, and that is worth knowing.
            vpp_log(f"[VPP] Ignored {foreign} snapshot(s) in {os.path.basename(path)} "
                f"that fall outside this event's window — they belong to a "
                f"different event and would have skewed the summary.",
                level="WARNING")

        def _to_float(v, default=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        # ----- core summary stats ----------------------------------------
        export_kwh   = _to_float((ended or {}).get("export_kwh", 0.0))
        pv_watts     = [_to_float(s.get("pv_w"))      for s in snapshots]
        bat_watts    = [_to_float(s.get("battery_w")) for s in snapshots]
        grid_watts   = [_to_float(s.get("grid_w"))    for s in snapshots]
        ems_strings  = [s.get("ems_work_mode")        for s in snapshots
                        if s.get("ems_work_mode")]

        min_pv_w           = int(min(pv_watts))                  if pv_watts   else 0
        max_pv_w           = int(max(pv_watts))                  if pv_watts   else 0
        # battery discharging = negative watts; "max discharge" is most negative magnitude
        max_bat_dis_w      = int(-min(bat_watts))                if bat_watts  else 0
        # grid exporting = negative watts; "peak export" is most negative magnitude
        peak_grid_export_w = int(-min(grid_watts))               if grid_watts else 0

        # Was the driver ever something other than us? Any snapshot reading
        # "external" means the live mode register held a mode we did not write.
        drivers_seen  = {s.get("driver") for s in snapshots if s.get("driver")}
        external_seen = "external" in drivers_seen
        driver_str    = ", ".join(sorted(drivers_seen)) if drivers_seen else "(unknown)"

        # Did our export mode curtail PV?  Mode 0x06 makes the battery do all the
        # discharge and, with the grid held at the DNO cap, the MPPT shuts down —
        # the 15-Jun-2026 fault that mode 0x05 (daytime) exists to avoid.
        #
        # THIS QUESTION ONLY EXISTS IN DAYLIGHT.  The old test was a bare
        # `min_pv_w > 100`, so every dark window reported "PV collapsed" — an
        # alarm about the sun having set.  Live case 05-Aug-2026, a 21:00-22:00
        # BST window: 45 snapshots at 0 W PV, a clean 4.23 kWh export, and a
        # Pushover claiming PV had collapsed.  Gate on the daylight flag latched
        # at VPP_ACTIVE entry, falling back to a fresh solar-window check if the
        # store has been cleared (a restart between event end and summary).
        #
        # AND THE VERDICT READS THE PEAK, NOT THE MINIMUM (v5.66.0).  v5.56.0
        # fixed the fully-dark window; this covers the DAYTIME window whose PV
        # touches zero part-way through.  The first two-hour event (12-Aug-2026,
        # 18:00-20:00 BST) hit it: PV ran at ~1.45 kW, peaked at 1757 W, then
        # collapsed to 0 W from 18:58 and stayed there — so the minimum was 0
        # and a textbook 8.26 kWh window was reported "curtailed".
        #
        # NOT SUNSET, AND NOT US.  Sunset that day was 20:47:47 — 48 min AFTER
        # the window closed, 1h50m after PV hit zero.  The sun was well up
        # throughout; a partial solar eclipse and cloud took the irradiance
        # (a 551 W -> 1757 W recovery inside five minutes is cloud, not an
        # astronomical curve).  Curtailment was IMPOSSIBLE anyway: in 0x05 with
        # charge pinned at 0, PV can only be curtailed above house + export cap
        # (~4.99 kW that evening) and it peaked at 1.76 kW.
        #
        # So a zero MINIMUM says nothing — PV can reach zero mid-window for
        # eclipse, cloud, or simply a window that outlasts the daylight later in
        # the year.  Only a peak that never lifts means the MPPT was shut down.
        daytime = self.store.get("vpp_is_daytime")
        if daytime is None:
            try:
                daytime = self._event_is_daytime(event.get("start_time"))
            except Exception:
                daytime = False
        daytime = bool(daytime)

        # AND A DAYLIGHT WINDOW AT THE EDGE OF THE SOLAR DAY EXPECTS NOTHING
        # ANYWAY (v5.73.0). The daytime gate brackets by dawn and dusk, so the
        # last forty minutes before sunset count as daytime - and a 14.25 kWp
        # array at five degrees of elevation makes essentially nothing, so a
        # zero peak there is the sun, not a shut-down MPPT.
        # Live-hit on the 16-Aug-2026 window (20:00-21:00 BST, sunset 20:40):
        # PV read 0 W across all 48 snapshots and the report said "curtailed",
        # while the forecast for that hour was 153 W falling to zero. It had
        # predicted the zero. Curtailment was IMPOSSIBLE too - in 0x05 with
        # charge pinned at 0 it needs PV above house + export cap, about 5 kW.
        # So the verdict now asks what was EXPECTED. A negligible forecast
        # excuses a zero; a substantial one still condemns it, which is the
        # 15-Jun-2026 failure this check exists for (a mid-morning window whose
        # forecast ran to kilowatts while mode 0x06 held the MPPT down).
        # An UNKNOWN forecast changes nothing - it must not quietly excuse a
        # real fault, so the older, noisier verdict stands in that case.
        expected_pv_w = self._forecast_pv_peak_w(event)

        if not daytime:
            pv_status = "n/a (dark window)"
        elif pv_watts and max_pv_w > 100:
            pv_status = "ran"
        elif expected_pv_w is not None and expected_pv_w < PV_EXPECTED_MIN_W:
            pv_status = "n/a (no PV expected)"
        else:
            pv_status = "curtailed"

        # The boolean is the "nothing went wrong with PV" flag a trigger wants,
        # so a dark window is True — there was no PV to lose.  pv_status carries
        # the detail that a boolean cannot.
        pv_survived = pv_status != "curtailed"

        # rough PV produced over the window: avg watts * hours
        try:
            duration_hrs = float(event.get("duration_hrs", 0.0))
        except Exception:
            duration_hrs = 0.0
        if pv_watts:
            avg_pv_w = sum(pv_watts) / len(pv_watts)
            pv_kwh   = (avg_pv_w * duration_hrs) / 1000.0
        else:
            pv_kwh = 0.0

        ems_modes_seen = sorted({m for m in ems_strings if m})
        ems_modes_str  = ", ".join(ems_modes_seen) if ems_modes_seen else "(unknown)"

        # ----- write summary to axleVppMonitor states --------------------
        try:
            dev = self._find_device("axleVppMonitor")
            if dev:
                event_date = ""
                try:
                    st = event.get("start_time")
                    if hasattr(st, "strftime"):
                        event_date = _local_time(st, "%Y-%m-%d %H:%M")
                    else:
                        event_date = str(st)
                except Exception:
                    pass
                dev.updateStatesOnServer([
                    {"key": "lastVppDate",                 "value": event_date},
                    {"key": "lastVppExportKwh",            "value": round(export_kwh, 2)},
                    {"key": "lastVppPvKwh",                "value": round(pv_kwh,     2)},
                    {"key": "lastVppMinPvW",               "value": min_pv_w},
                    {"key": "lastVppMaxPvW",               "value": max_pv_w},
                    {"key": "lastVppMaxBatteryDischargeW", "value": max_bat_dis_w},
                    {"key": "lastVppPeakGridExportW",      "value": peak_grid_export_w},
                    {"key": "lastVppPvSurvived",           "value": pv_survived},
                    {"key": "lastVppPvStatus",             "value": pv_status},
                    {"key": "lastVppEmsModes",             "value": ems_modes_str},
                    {"key": "lastVppDriver",               "value": driver_str},
                    {"key": "lastVppLogPath",              "value": path or ""},
                ])
        except Exception as exc:
            vpp_log(f"[VPP] Could not update axleVppMonitor summary states: {exc}",
                level="WARNING")

        # Add our own figures for this window to the ledger — BOTH of them.
        # `export_kwh` is the counter delta across the whole driven run,
        # lead-in and trail included; the integrated figure covers only the
        # paid window. They differ by a couple of minutes of tails normally,
        # and by three quarters of an hour when a window fails to stop (11-Aug
        # read 7.05 kWh for an hour whose cap allows 4). Only the second is
        # comparable with what Axle settles.
        window_kwh = None
        try:
            window_kwh = _vpp_ledger.integrate_window_kwh(
                [(_to_float(s.get("event_elapsed_secs")), _to_float(s.get("grid_w")))
                 for s in snapshots],
                event.get("duration_hrs"))
        except Exception as exc:
            vpp_log(f"[VPP] Could not integrate in-window export: {exc}", level="WARNING")

        self._record_vpp_ledger_event(event, export_kwh,
                                      driver=driver_str, log_path=path or "",
                                      window_kwh=window_kwh)

        # ----- Pushover: headline numbers + pre-formed Claude prompt -----
        # The prompt used to ask what AXLE did — wording left over from the
        # observe-and-hand-over model that v5.28.0 replaced with self-drive in
        # June. We drive every window ourselves now and ignore Axle's dispatch,
        # so those questions had a false premise baked in and sent the reader
        # looking for behaviour that was never going to be in the file.
        window_str = "daytime" if daytime else "dark"
        # THE HEADLINE IS THE PAID HOUR, NOT THE WHOLE RUN (v5.91.0).
        # export_kwh is the counter delta across everything we drove, and the
        # driver deliberately runs two minutes either side of the window, so it
        # overstates what Axle settles by ~0.23 kWh on a textbook event. It was
        # the title, the first body line AND the event-log one-liner, so all
        # three read about 6% high while window_kwh -- the figure that is
        # actually comparable, computed a few lines above and already stored in
        # the ledger -- appeared nowhere the reader would see it.
        # Axle's own settlements settle the argument: over the seven events they
        # have settled, window_kwh is 0.17 kWh from their number and export_kwh
        # is 0.82 kWh from it (measured 05-Sep-2026).
        rate_per_kwh = _as_float(self.pluginPrefs.get("axleVppRatePerKwh"), 1.00)
        outside_kwh  = (export_kwh - window_kwh) if window_kwh is not None else None

        if window_kwh is not None:
            title = f"VPP window done - {window_kwh:.2f} kWh sold"
            essential = [
                f"Sold inside the paid hour: {window_kwh:.2f} kWh, worth about "
                f"GBP {window_kwh * rate_per_kwh:.2f} at {rate_per_kwh * 100:.0f}p a unit.",
            ]
            # Only worth a sentence when there is something to explain. Below a
            # tenth of a unit the tails are rounding, and saying so every time
            # would train the reader to skip the line that matters when a window
            # genuinely over-runs.
            if outside_kwh is not None and outside_kwh >= 0.1:
                essential.append(
                    f"We sent out {export_kwh:.2f} kWh altogether. The extra "
                    f"{outside_kwh:.2f} kWh went either side of the window, so it "
                    f"earns the ordinary export rate rather than the event rate."
                )
        else:
            # Never present the run total as though it were the settled figure.
            title = f"VPP window done - {export_kwh:.2f} kWh exported"
            essential = [
                f"We sent out {export_kwh:.2f} kWh while driving the export. The "
                f"paid-hour figure could not be measured this time, so treat that "
                f"as the whole run and not as what Axle will settle.",
            ]

        # A window that something else was driving is not a detail -- it is the
        # headline. Essential, so it can never be one of the lines shed to fit.
        if external_seen:
            essential.append("WARNING: something outside the plugin moved the "
                             "inverter mode during the window.")

        # Diagnostics. Every one of these is in the JSONL as well, which is what
        # makes them the right lines to drop when the body will not fit.
        optional = [
            f"Solar: {pv_kwh:.2f} kWh ({pv_status}), peaking at {max_pv_w} W.",
            f"The battery peaked at {max_bat_dis_w} W going out and the grid at "
            f"{peak_grid_export_w} W.",
            f"Inverter mode through the {window_str} window: {ems_modes_str} "
            f"(driven by {driver_str}).",
        ]
        tail = [
            "",
            "-- Ask Claude --",
            "Read the latest VPP JSONL file at",
            f"  {path}",
            "This was a SELF-DRIVEN export (we hold Remote EMS over Modbus and",
            "ignore Axle's dispatch; Axle settles on the meter). Tell me:",
            f"  1. Did we hold the export target for the full window ({window_str})?",
            "  2. What EMS mode + register values did we use, and were they",
            "     the right ones for the conditions?",
            "  3. Did anything external move the mode register mid-window?",
            "  4. Any recommended changes to _drive_vpp_export /",
            "     _verify_ems_registers.",
        ]
        body = _fit_pushover_body(essential, optional, tail)
        try:
            self._send_pushover(title, body, priority="0")
        except Exception as exc:
            vpp_log(f"[VPP] Pushover summary send failed: {exc}", level="WARNING")

        # Also log the headline to the Indigo Event Log so the user has a
        # single grep-able line for each event.  No JSONL spam, just the
        # one-liner.
        _paid_str = (f"{window_kwh:.2f} kWh sold in the paid hour "
                     f"({export_kwh:.2f} kWh over the whole run)"
                     if window_kwh is not None
                     else f"{export_kwh:.2f} kWh exported (paid hour not measured)")
        vpp_log(f"[VPP] Summary: {_paid_str}, "
            f"PV {pv_kwh:.2f} kWh ({pv_status}), "
            f"peak grid export {peak_grid_export_w} W, "
            f"{window_str} window, driver {driver_str}, "
            f"EMS modes: {ems_modes_str}. Log: {path}")
        if external_seen:
            vpp_log("[VPP] External control was detected during the window — the mode "
                "register held a value the plugin did not write. Check the JSONL "
                "snapshots for when it changed.", level="WARNING")

    def _start_vpp_precharge(self, event):
        """Assess SOC 30 min before VPP event; raise discharge cutoff; no grid import.

        The discharge cutoff is raised here (not at announcement) so it only
        applies close to the event — avoiding unnecessary battery lockout hours
        in advance. If SOC is below the required level, we do NOT import from
        grid: solar will have charged the battery throughout the day, and if
        energy is still short, Axle's own firmware will decline to dispatch
        rather than us importing at cost to cover their export.
        """
        duration_hrs    = event.get("duration_hrs", 1.0)
        cap_kwh         = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), BATTERY_CAPACITY_KWH)
        if cap_kwh <= 0:
            cap_kwh = BATTERY_CAPACITY_KWH   # guard the (required_kwh / cap_kwh) divisions below
        max_export_kw   = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)
        dawn_target_pct = self._dawn_target_pct()

        # For daytime events solar will recharge during/after the event, so we
        # only need to hold the export energy itself.  For night events we must
        # also hold the dawn reserve so the battery survives until morning.
        is_daytime  = self._event_is_daytime(event.get("start_time"))
        export_kwh  = max_export_kw * duration_hrs / VPP_DISCHARGE_EFFICIENCY
        if is_daytime:
            required_kwh = export_kwh
            required_soc = min(100.0, (required_kwh / cap_kwh) * 100.0)
            dawn_kwh     = 0.0
        else:
            dawn_kwh     = cap_kwh * dawn_target_pct / 100.0
            required_kwh = export_kwh + dawn_kwh
            required_soc = min(100.0, (required_kwh / cap_kwh) * 100.0)
            required_soc = max(required_soc, dawn_target_pct)

        if self._flux_armed():
            # Match the floor that will actually be installed below. In this
            # path the event is still ANNOUNCED; it may spend its own allocation.
            floor_pct = max(dawn_target_pct if not is_daytime else 0.0,
                            self._policy_discharge_floor_pct(dispatch_event=event))
            dawn_kwh = cap_kwh * floor_pct / 100.0
            required_kwh = export_kwh + dawn_kwh
            required_soc = min(100.0, required_kwh / cap_kwh * 100.0)

        # Current battery level
        current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)
        current_kwh  = cap_kwh * current_soc / 100.0

        self.store["vpp_pre_charge_soc"] = required_soc

        # Set discharge cutoff now (30 min before) — not at announcement time
        self._set_vpp_discharge_cutoff(event, is_daytime)

        if current_kwh >= required_kwh:
            if self._flux_armed():
                vpp_log(f"[VPP] SOC sufficient ({current_soc:.0f}%) for "
                        f"{export_kwh:.1f} kWh export plus {dawn_kwh:.1f} kWh "
                        f"protected reserve and later commitments")
            elif is_daytime:
                vpp_log(
                    f"[VPP] SOC sufficient ({current_soc:.0f}%, {current_kwh:.1f} kWh) for "
                    f"{duration_hrs:.1f}h export ({export_kwh:.1f} kWh) — daytime, solar will recharge"
                )
            else:
                vpp_log(
                    f"[VPP] SOC sufficient ({current_soc:.0f}%, {current_kwh:.1f} kWh) for "
                    f"{duration_hrs:.1f}h export ({export_kwh:.1f} kWh) + dawn reserve "
                    f"({dawn_kwh:.1f} kWh)"
                )
        else:
            shortfall = required_kwh - current_kwh
            vpp_log(
                f"[VPP] SOC low ({current_soc:.0f}%, shortfall {shortfall:.1f} kWh) — "
                f"proceeding without grid import; Axle will assess at dispatch time",
                level="WARNING",
            )
            self._alert_vpp_shortfall(
                event, current_soc, current_kwh, required_kwh, shortfall, is_daytime
            )

        self._vpp_transition(VPP_PRE_CHARGING, preempted=True)

    def _alert_vpp_shortfall(self, event, current_soc, current_kwh,
                             required_kwh, shortfall, is_daytime):
        """Pushover a heads-up when pre-charge finds the battery short for the window.

        Until v5.58.0 a shortfall produced ONE log line and nothing else, so the
        first anyone knew of an under-delivered window was the settlement figure
        days later. That mattered little while events arrived with 18-24 h of
        notice and a manual opt-in; it matters now that Axle opt us in by default
        (08-Aug-2026) and short-notice events give as little as 2 h, leaving far
        less room for solar to top the battery up before the window opens.

        Deliberately priority 0, so quiet hours CAN suppress it: there is nothing
        to be done about it at 03:00 — pre-charge never imports by design (see
        _start_vpp_precharge) — and being woken to be told the export will be
        smaller helps nobody. The log line above is the durable record.

        Wrapped whole: this is an advisory on the pre-charge path, and a failure
        to describe the shortfall must never stop the window being driven.
        """
        try:
            window = self._vpp_event_str() or "the next window"
            floor = ("health floor (daytime — solar will recharge)" if is_daytime
                     else "dawn reserve (night event)")
            if self._flux_armed():
                floor = "Flux reserve and later event allocations"
            body = (
                f"Battery short for the {window} VPP window.\n\n"
                f"SOC {current_soc:.0f}% ({current_kwh:.1f} kWh) against "
                f"{required_kwh:.1f} kWh needed — short by {shortfall:.1f} kWh.\n\n"
                f"The window will still be driven; export simply stops when the "
                f"battery reaches its {floor}. No grid import is used to cover it."
            )
            self._send_pushover("Sigen VPP — battery low for event", body, priority="0")
        except Exception as exc:
            vpp_log(f"[VPP] shortfall alert failed: {exc}", level="ERROR")

    def _set_vpp_discharge_cutoff(self, event, is_daytime=False):
        """Set discharge cutoff at pre-charge time (30 min before event).

        Daytime events (solar forecast available during/after event):
          Use the health floor (1%) — the battery can discharge freely because
          solar will recharge it during the day.  No need to hold a dawn reserve.

        Night events (before dawn or after dusk, no solar recharge coming):
          Use the dawn target (15%) — ensures the battery can survive overnight
          until the next morning's solar even after the event has dispatched.

        Called from _start_vpp_precharge() with the is_daytime flag already
        determined, NOT at announcement time.
        """
        self._flux_preempt("Axle pre-charge cutoff")
        health_floor    = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)
        dawn_target_pct = self._dawn_target_pct()

        if is_daytime:
            floor_pct = health_floor
            reason    = "daytime event — solar will recharge"
        else:
            floor_pct = dawn_target_pct
            reason    = "night event — protecting dawn floor"

        # Never below the absolute cutoff. The Flux reserve and every OTHER
        # commitment go on the backup reserve instead, computed with this event
        # excluded so the dispatch can spend its own allocation.
        floor_pct = max(floor_pct, health_floor, self._absolute_cutoff_pct())

        if self.modbus:
            self.modbus.set_discharge_cutoff(floor_pct)
            if self._owns_backup_reserve():
                self.modbus.set_backup_soc(
                    self._policy_discharge_floor_pct(dispatch_event=event))
            self.store["vpp_cutoff_raised"] = True   # prevents verify() fighting the VPP floor
            vpp_log(f"[VPP] Discharge cutoff set to {floor_pct:.0f}% ({reason})")

    def _event_is_daytime(self, event_start):
        """Return True if event_start falls within the solar generation window.

        Uses _dawn_times (first PV slot above threshold) and the hourly forecast
        (last non-zero slot) to bracket the solar window for the event's date.
        Returns False if event_start is None or solar data is unavailable —
        night-event behaviour is the safe fallback.
        """
        if event_start is None:
            return False

        fcast      = self.latest_forecast_data or {}
        dawn_times = fcast.get("_dawn_times", {})

        # Convert event start to local (London) time for date lookup. An hour's
        # error here flips a dusk-edge window to "daytime" and runs mode 0x05
        # when it should run 0x06 (or the reverse — the 15-Jun-2026 PV
        # curtailment), so this must not degrade silently.
        if _london_tz() is None:
            _warn_no_tzdb()
        event_local = _to_london(event_start)

        event_date_str = event_local.strftime("%Y-%m-%d")

        # Dawn: first PV-generating slot on the event's date
        dawn = dawn_times.get(event_date_str)
        if dawn is None:
            return False   # no solar expected that day

        # Dusk: last slot with non-zero generation on the event's date
        # Check both today and tomorrow hourly dicts
        dusk = None
        for hourly_key in ("_hourly_p50_today", "_hourly_p50_tomorrow"):
            hourly = fcast.get(hourly_key, {})
            for slot_str in sorted(hourly.keys(), reverse=True):
                if slot_str.startswith(event_date_str) and hourly[slot_str] > 0:
                    try:
                        dt_naive = datetime.strptime(slot_str, "%Y-%m-%d %H:%M:%S")
                        # Forecast slots are local wall-clock, so localise (not
                        # stamp) — see _london_localise on why that distinction
                        # cannot be hand-rolled per site.
                        dusk = _london_localise(dt_naive) or dt_naive
                    except Exception:
                        pass
                    break
            if dusk is not None:
                break

        if dusk is None:
            return False   # can't determine dusk — treat as night

        # Compare event_start against the solar window
        try:
            return dawn <= event_start <= dusk
        except TypeError:
            # Mixed tz-aware / naive — strip timezone for comparison
            def _naive(dt):
                return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt
            return _naive(dawn) <= _naive(event_start) <= _naive(dusk)

    def _forecast_pv_peak_w(self, event):
        """Peak forecast watts over an event's window, or None if unknown.

        Wraps the pure `_forecast_peak_w_for_window` with this plugin's
        forecast store and the local-time conversion. Wrapped whole: an
        advisory figure must never cost the summary that carries the export.
        """
        try:
            def _iso_dt(v):
                if v is None:
                    return None
                if isinstance(v, datetime):
                    dt = v
                else:
                    try:
                        dt = datetime.fromisoformat(str(v))
                    except (TypeError, ValueError):
                        return None
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

            start = _iso_dt(event.get("start_time")) if event else None
            end   = _iso_dt(event.get("end_time"))   if event else None
            if start is None:
                return None
            start_local = _to_london(start)
            end_local   = _to_london(end) if end else start_local

            fcast = self.latest_forecast_data or {}
            peak  = None
            for key in ("_hourly_p50_today", "_hourly_p50_tomorrow"):
                got = _forecast_peak_w_for_window(fcast.get(key) or {},
                                                  start_local, end_local)
                if got is not None:
                    peak = got if peak is None else max(peak, got)
            return peak
        except Exception:
            return None

    def _restore_discharge_cutoff(self):
        """Restore discharge cutoff to the health floor after VPP."""
        if self.modbus:
            floor = self._write_policy_floors()
            self.store["vpp_cutoff_raised"] = False   # allow verify() to manage cutoff again
            vpp_log(f"[VPP] Discharge cutoff restored to {floor:.0f}% (policy floor)")

    def _disengage_to_safe_baseline(self, reason):
        """Return the inverter to the safe self-consumption baseline AND release any
        raised discharge-cutoff floor (flood-prevention / storm / VPP), standing
        down any in-flight VPP export.

        Used by both prepare_to_sleep() and pause.  set_self_consumption() clears
        the forced MODE and the charge/discharge LIMIT registers, but NOT the
        discharge-cutoff SOC register — so a cutoff left raised (e.g. a 30% flood
        pre-drain target, or a VPP pre-charge floor) would lock the battery above
        that SOC for the whole hands-off period and force grid import, defeating the
        self-sufficiency KPI under a 'safe' label.  This helper resets the cutoff to
        the health floor and clears the raised-floor flags so the manager re-evaluates
        cleanly on wake/resume.  Best-effort; never raises (a hands-off transition
        must always complete).
        """
        # 1. Stand down any VPP engagement first.  For an ACTIVE window _end_vpp_export
        #    already restores the cutoff + records the wrap-up; earlier states just drop
        #    back to IDLE so a later resume/wake re-detects the window if it's still open.
        vpp_state = self.store.get("vpp_state", VPP_IDLE)
        if vpp_state == VPP_ACTIVE:
            try:
                self._end_vpp_export(datetime.now(timezone.utc),
                                     self.store.get("vpp_event"))
            except Exception as exc:
                log(f"[{reason}] VPP stand-down failed: {exc}", level="WARNING")
        elif vpp_state != VPP_IDLE:
            self.store["vpp_event"]  = None
            self.store["vpp_active"] = False
            self._vpp_transition(VPP_IDLE)
        # 2. Safe baseline mode + reset the discharge cutoff to the health floor.
        if self.modbus and self.modbus.connected:
            try:
                self.modbus.set_self_consumption()
            except Exception as exc:
                log(f"[{reason}] set_self_consumption failed: {exc}", level="WARNING")
            try:
                # Clear the raised floors FIRST: releasing them is the whole point
                # of this path, and reading the policy floor while they are still
                # set would hand back the very floor being released.
                self._set_flood_prev_target(None)
                self.store["vpp_cutoff_raised"] = False
                floor = self._write_policy_floors()
                log(f"[{reason}] Discharge cutoff reset to {floor:.0f}% (policy floor)")
            except Exception as exc:
                log(f"[{reason}] discharge-cutoff reset failed: {exc}", level="WARNING")
            try:
                if self.store.get("import_charge_cutoff_pct"):
                    self.modbus.set_charge_cutoff(100.0)
                    log(f"[{reason}] Charge cutoff restored to 100% (import backstop released)")
            except Exception as exc:
                log(f"[{reason}] charge-cutoff reset failed: {exc}", level="WARNING")
        # 3. Clear the raised-floor flags so _verify_ems_registers manages the cutoff
        #    again on the next evaluate (it defers while flood/VPP floors are flagged).
        self._set_flood_prev_target(None)
        self._set_import_cutoff(None)
        self.store["vpp_cutoff_raised"] = False
        self.store["import_active"]     = False
        self.store["export_active"]     = False
        # 4. Cancel any QUEUED import (v5.65.0). _check_scheduled_import runs on
        #    the tick regardless of pause — it sits outside the paused gate — so a
        #    schedule armed before the pause fired anyway: a full-power grid import
        #    at 00:05 with the device reading "Paused", left in Charge Grid First
        #    with a raised charge cutoff for the rest of the hands-off period.
        #    Pause and sleep both mean "stop driving the inverter", and that has to
        #    include the drive we had queued.
        if self.store.get("import_scheduled_time") is not None:
            log(f"[{reason}] Cancelled the queued grid import that was due at "
                f"{_local_time(self.store['import_scheduled_time'])}")
            self.store["import_scheduled_time"]   = None
            self.store["import_scheduled_logged"] = False

    def _end_vpp_export(self, now, event):
        """Stop the self-driven VPP export at window end (+2-min tail).

        Restores Self Consumption + the health-floor discharge cutoff and returns
        the state machine straight to IDLE — no COOLING_OFF / Axle-release wait,
        because we never handed control to Axle. Records the wrap-up + summary.
        """
        event = event or {}
        vpp_export = (self.store["grid_export_daily_kwh"]
                      - self.store.get("vpp_export_start_kwh", 0.0))
        self.store["vpp_last_export_kwh"] = round(vpp_export, 2)

        # Hand the inverter back, and CONFIRM it landed. set_self_consumption()
        # returns False when the write was rejected, clamped, or the socket was
        # already dead — and until v5.64 that answer was thrown away, so the state
        # machine went IDLE claiming the export had stopped while the inverter was
        # still sitting in 0x05/0x06 selling the battery. The only backstop was
        # _verify_ems_registers, which runs on the 60-SECOND manager cycle (this
        # comment said "~15 MINUTE" until v5.91.0 — MANAGER_EVAL_INTERVAL is 60,
        # and 55 runs were counted in the 05-Sep-2026 window), so a
        # failed hand-back drained up to ~0.08 kWh to the grid outside the paid
        # window — unpaid, silent (one generic Modbus ERROR line, nothing
        # VPP-tagged), and straight against the self-sufficiency KPI.
        released = False
        if self.modbus and self.modbus.connected:
            released = bool(self.modbus.set_self_consumption())
            if not released:
                # One immediate retry: the commonest cause is a dropped socket,
                # and the failed write already flags the connection for reconnect.
                released = bool(self.modbus.set_self_consumption())
        if not released:
            vpp_log("[VPP] Hand-back to Self Consumption NOT confirmed at window end — "
                "the inverter may still be exporting. Re-asserting every tick until "
                "it lands.", level="WARNING")
        self.store["vpp_handback_pending"] = not released
        self._restore_discharge_cutoff()

        self.store["export_active"]        = False
        self.store["vpp_active"]           = False
        self.store["vpp_export_submode"]   = None
        self.store["vpp_bank_charge_cap_w"] = -1
        # The transition persists accumulators: include completion in that snapshot.
        self.store["had_vpp_today"] = True
        self._vpp_transition(VPP_IDLE)
        self.store["vpp_event"]     = None
        self._trigger_event("vppEnded")

        # Wrap-up record in the per-event JSONL file (best-effort)
        try:
            import json as _json
            path = self._vpp_event_log_path(event)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps({
                    "type":       "event_ended",
                    "logged_at":  now.isoformat(),
                    "export_kwh": round(vpp_export, 3),
                }) + "\n")
        except Exception:
            pass

        vpp_log(f"[VPP] >>> EVENT COMPLETE <<<  self-driven export ~{vpp_export:.2f} kWh. "
            f"Restored to Self Consumption.")

        # Post-event summary -> axleVppMonitor states + Pushover (best-effort)
        try:
            self._summarise_vpp_event(event)
        except Exception as _exc:
            vpp_log(f"[VPP] Summary step failed: {_exc}", level="WARNING")

    def _vpp_transition(self, new_state, preempted=False):
        """Transition VPP state machine to a new state."""
        old_state = self.store["vpp_state"]
        # Flux lets go BEFORE the state machine's own writes, not after: a paid
        # window taking the inverter must not find a Flux claim still standing,
        # and a release discovered a tick later would restore the baseline over
        # the top of the window's mode.
        # ANNOUNCED is not a driving state — it writes nothing, and _flux_other_owner
        # deliberately does not treat it as an owner, because the cheap window is
        # where the energy for the dispatch gets bought. Pre-empting here would
        # stand Flux down at announcement and undo that fix from the other side.
        if (not preempted and new_state not in (VPP_IDLE, VPP_ANNOUNCED)
                and old_state != new_state):
            self._flux_preempt(f"an Axle VPP window ({new_state})")
        self.store["vpp_state"] = new_state
        if self.debug:
            vpp_log(f"[VPP] State: {old_state} -> {new_state}")

        # Any engagement cancels a pending hand-back retry — re-asserting the
        # 0x02 baseline during a live window would kill a paid export (v5.64).
        if new_state != VPP_IDLE:
            self.store["vpp_handback_pending"] = False

        # On entry to VPP_ACTIVE we self-drive the export (v5.28). Axle settle on
        # the meter reading so exporting it ourselves counts identically — and
        # their cloud dispatch proved unreliable (no-show 10-Jun-2026; Axle
        # acknowledged a SigEnergy-API fault that may not be fixed before the next
        # event). _drive_vpp_export() does the actual driving here for a prompt
        # start at T-2min, and the manager's ACTION_VPP_EXPORT override re-runs it
        # each tick. The discharge floor (next-day reserve) was set at pre-charge.
        if new_state == VPP_ACTIVE:
            if self.store.get("solar_overflow_active"):
                self.store["solar_overflow_active"]       = False
                self.store["solar_overflow_charge_cap_w"] = 0
                # Same dwell as an ordinary release: a daylight VPP window ending
                # should not hand straight back to a marginal overflow decision.
                self.store["solar_overflow_released_at"]  = datetime.now(timezone.utc)

            # Latch whether this is a daylight window (set once at entry). The live
            # sub-mode (bank-surplus vs discharge) is then re-decided every tick by
            # _drive_vpp_export from real PV/home; vpp_export_mode tracks the actual
            # mode register (0x02/0x05/0x06) for the verify loop.
            event = self.store.get("vpp_event") or {}
            self.store["vpp_is_daytime"]       = self._event_is_daytime(event.get("start_time"))
            self.store["vpp_export_submode"]   = None    # force a mode log on first drive
            self.store["vpp_bank_charge_cap_w"] = -1
            vpp_log("[VPP] >>> VPP WINDOW ACTIVE <<<  self-driving export "
                f"({'daytime' if self.store['vpp_is_daytime'] else 'dark'} window, "
                "DNO-capped; Axle dispatch ignored).")
            self._drive_vpp_export()

        # Persist the new state immediately (crash-safe, atomic) so a restart or
        # crash mid-window resumes without relying on the Axle API still
        # returning the active event. accumulators.json is the authoritative
        # cross-restart store (pluginPrefs only flush on a graceful shutdown).
        self._save_accumulators()

    def _rehydrate_vpp_state(self):
        """Resume an in-progress Axle VPP window after a plugin restart.

        The VPP state machine is otherwise re-driven purely from the Axle API on
        each poll, so a restart mid-window relies on Axle still returning the
        active event. If its endpoint drops the event once the window is live,
        the rest of the window would be silently missed — Predbat issue #3051's
        failure mode. The window is persisted to accumulators.json on every
        transition and restored into the store by _load_accumulators; here, with
        modbus available, we make the time-based decision.

        Best-effort — never blocks startup.
        """
        state = self.store.get("vpp_state", VPP_IDLE)
        event = self.store.get("vpp_event")
        now   = datetime.now(timezone.utc)
        decision = _vpp_resume_decision(state, event, now)

        if decision == "idle":
            return

        if decision == "ended":
            # Window finished while the plugin was down. The discharge-cutoff
            # register may still be raised in hardware (it survives a plugin
            # restart) — reset it to the health floor and drop back to Self
            # Consumption so the manager evaluates cleanly.
            vpp_log(f"[VPP] Persisted window ended during downtime "
                f"(ended {_local_time(event.get('end_time'))}) — cleaning up")
            try:
                if self.modbus:
                    self.modbus.set_self_consumption()
                self._restore_discharge_cutoff()
            except Exception as exc:
                vpp_log(f"[VPP] Restart cleanup failed: {exc}", level="WARNING")
            self.store["vpp_event"]         = None
            self.store["vpp_active"]        = False
            self.store["export_active"]     = False
            self.store["vpp_cutoff_raised"] = False
            self._vpp_transition(VPP_IDLE)   # re-persists, clearing the window
            return

        # decision == "resume" — the window is still open. Restore the state
        # machine only; NO hardware re-issue here. The discharge-cutoff register
        # persisted across the restart, and for an ACTIVE window the manager's
        # ACTION_VPP_EXPORT override re-drives the export on its next tick.
        # Keeping vpp_cutoff_raised set stops _verify_ems_registers lowering the
        # floor before then.
        self.store["vpp_active"] = (state == VPP_ACTIVE)
        vpp_log(f"[VPP] Resuming persisted '{state}' window after restart — "
            f"{_local_time(event.get('start_time'))}-{_local_time(event.get('end_time'))} "
            f"(Axle API not required; cutoff-raised="
            f"{self.store.get('vpp_cutoff_raised', False)})")

    def _export_is_daylight(self, now_utc=None):
        """Is there sun on the roof RIGHT NOW? Picks 0x05 (PV-first) over 0x06 (ESS-first).

        READ AT DRIVE TIME, NEVER LATCHED. _drive_vpp_export used to read
        store["vpp_is_daytime"], which ONLY the VPP state machine ever writes
        (_vpp_transition, on entry to VPP_ACTIVE). Every other caller of the driver
        — today the Octopus Saving Session — therefore steered on a flag left behind
        by the last VPP window, which can be days old and is not about now at all.

        Live cost, 07-Sep-2026: a Saving Session ran 18:00-19:00 BST with the flag
        stale-False from a plugin restart, so the driver chose night_export (0x06).
        PV read 932 W at 17:00:45 UTC and 0 W at 17:01:06 — twenty-one seconds after
        the mode commit — and stayed at zero for the next 57 minutes, with sunset
        still 1h42m away. Roughly 0.3-0.5 kWh of free solar thrown away and taken
        out of the battery instead (estimated from the 932 W measured at the commit
        decaying to the 25-66 W measured an hour later under 0x05; the uncurtailed
        case was never run, so it cannot be measured exactly).

        UNKNOWN RESOLVES TO DAYLIGHT, and the asymmetry is the whole argument. The
        two modes are not equal-and-opposite: daytime_export's own docstring records
        that at PV == 0 mode 0x05 "behaves exactly like night_export", so guessing
        daylight in the dark costs nothing whatever, while guessing dark in daylight
        curtails the array to zero for the length of the window. So a missing
        forecast must not fall through to 0x06. (_event_is_daytime keeps its own
        night-is-safe fallback: its other callers ask a different question — what
        the discharge floor should be, and whether a zero-PV window deserves a
        "curtailed" verdict — where an unwarranted daylight answer is the costly one.)

        The latched store flag is left alone: the post-window summary wants
        "was this a daylight window", decided once, and that is a fair question.
        """
        fcast = self.latest_forecast_data or {}
        if not fcast.get("_dawn_times") and not fcast.get("_hourly_p50_today"):
            return True   # no forecast to reason from — assume sun, the cheap error
        try:
            return bool(self._event_is_daytime(now_utc or datetime.now(timezone.utc)))
        except Exception as exc:
            self.logger.debug(f"[VPP] Daylight check failed ({exc}) — assuming daylight")
            return True

    def _drive_vpp_export(self, now_utc=None):
        """Self-drive the export, re-evaluated each manager tick while a window runs.

        Pre-empts Flux first. A Saving Session reaches here with vpp_state still
        IDLE, so the state-machine hook alone would not cover it.

        Driven by BOTH the VPP state machine (ACTION_VPP_EXPORT) and an Octopus
        Saving Session (ACTION_SAVING_SESSION) — the name is historical.

        Two sub-modes, chosen from live PV vs the export target:

        - "bank"  (daytime, PV surplus >= target): stay in Max Self Consumption
          (mode 0x02) and CAP the battery charge to (surplus - target). The
          inverter then exports the target to the grid (held at the DNO cap) and
          banks only the PV above the target — full paid export AND the battery
          charges, with no PV curtailment. This is the same mechanism as Solar
          Overflow (proven on hardware 15-Jun-2026: PV 5.75 kW -> home 0.61 +
          export 4.00 + battery charge 1.13).

        - "discharge" (dark window, or PV surplus < target): issue a discharge
          command so the battery tops the export up to the target — daytime uses
          mode 0x05 (PV-first) with charge pinned to 0; dark uses 0x06 (ESS-first).

        Hysteresis (HYST_W) around the crossover stops the mode flapping when PV
        hovers at the target. Modbus is only written on a sub-mode change or when
        the bank charge cap shifts beyond a deadband, so re-running every tick is
        cheap. The grid export is held at the DNO cap by the inverter's
        commissioned export limit in every sub-mode.
        """
        self._flux_preempt("a driven export window")
        if not self.modbus:
            return

        first      = not self.store.get("export_active")
        inv_max_w  = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
        target_w   = int(_as_float(self.pluginPrefs.get("maxExportKw"), 4.0) * 1000)
        daytime    = self._export_is_daylight(now_utc)

        inv       = self.latest_inverter_data or {}
        pv_w      = int(inv.get("pvPowerWatts", 0))
        home_w    = int(inv.get("homePowerWatts", 0))
        surplus_w = max(0, pv_w - home_w)

        # Guarantee the full export: the instant surplus < target, use discharge so
        # the battery tops the grid up to the target (bank mode / self-consumption
        # will NOT discharge to export, so it would fall short). Only ENTER bank when
        # surplus is comfortably above target (>= target + HYST) — that one-sided
        # margin stops a brief PV spike flapping us into 0x02. Once banking, hold it
        # down to target (in the [target, target+HYST) band bank still exports the
        # full target, just banks a little less), then drop to discharge below target.
        HYST_W   = 400
        prev_sub = self.store.get("vpp_export_submode")
        if not daytime:
            sub = "discharge"
        elif surplus_w < target_w:
            sub = "discharge"
        elif surplus_w >= target_w + HYST_W:
            sub = "bank"
        else:
            sub = prev_sub or "discharge"   # band [target, target+HYST): hold prev

        if sub == "bank":
            charge_cap_w = max(0, surplus_w - target_w)
            if prev_sub != "bank":
                # Switch into Max Self Consumption (resets limits to inv_max), then
                # apply the charge cap below. vpp_export_mode tracks 0x02 for verify.
                self.modbus.set_self_consumption()
                self.store["vpp_export_mode"]       = 0x02
                self.store["vpp_bank_charge_cap_w"] = -1   # force the cap write below
                vpp_log(f"[VPP] Bank-surplus export — PV {pv_w}W, home {home_w}W, surplus "
                    f"{surplus_w}W >= target {target_w}W: mode 0x02, charge cap "
                    f"{charge_cap_w}W (export {target_w}W from PV, bank the rest).")
            prev_cap = self.store.get("vpp_bank_charge_cap_w", -1)
            if abs(prev_cap - charge_cap_w) > 300:
                self.modbus.set_charge_limit(charge_cap_w, quiet=True)
                self.store["vpp_bank_charge_cap_w"] = charge_cap_w
        else:
            if prev_sub != "discharge":
                if daytime:
                    self.modbus.daytime_export(inv_max_w)   # mode 0x05 + charge 0
                    self.store["vpp_export_mode"] = 0x05
                else:
                    self.modbus.night_export(inv_max_w)     # mode 0x06
                    self.store["vpp_export_mode"] = 0x06
                self.store["vpp_bank_charge_cap_w"] = -1
                vpp_log(f"[VPP] Discharge export — PV {pv_w}W, home {home_w}W, surplus "
                    f"{surplus_w}W < target {target_w}W: mode "
                    f"{'0x05 PV-first' if daytime else '0x06 ESS-first'}, battery tops "
                    f"the export up to {target_w}W.")

        self.store["vpp_export_submode"] = sub
        if first:
            self.store["export_active"] = True
            self._trigger_event("exportStarted")

    # ================================================================
    # Midnight Tasks
    # ================================================================

    def _check_midnight(self):
        # v5.45.0: accumulator rollover is a compound store mutation — locked.
        with self._state_lock:
            return self._check_midnight_impl()

    def _check_midnight_impl(self):
        """Run once-daily tasks at local (Europe/London) midnight.

        Naive datetime.now() returns server-local time which may not match
        Europe/London if the host runs UTC, causing accumulators to roll over
        on the wrong calendar day around BST/UTC boundaries.
        """
        today = _local_today_str()   # Europe/London (shared with init + accumulator restore)
        if today == self.store["today_date"]:
            return  # Not yet midnight

        # v5.89.0: give the first post-midnight lifetime read a few minutes to
        # replace the provisional anchor, so the record written below is the
        # difference of two true boundary readings. Bounded — a Modbus outage
        # at midnight must not hold the day's record hostage.
        de = getattr(self, "daily_energy", None)
        if de is not None and de.today_date == today and de.today().get("provisional"):
            midnight = local_midnight_epoch(today)
            if midnight is not None and 0.0 <= time.time() - midnight < MIDNIGHT_ANCHOR_WAIT_S:
                return

        # New day
        yesterday = self.store["today_date"]
        log(f"Midnight: recording daily history for {yesterday}")

        # Write the bias-accuracy record for yesterday (pass the date explicitly —
        # datetime.now() inside record_accuracy would give the new day). The
        # morning baseline it consumes was captured by the forecast module itself
        # on yesterday's FIRST complete fetch (day-ahead), and record_accuracy
        # skips if the baseline's date doesn't match — no capture call here
        # (capturing at midnight fed it a same-day hindcast, v5.43 fix).
        #
        # v5.106.0: the ACTUAL comes from the settled boundary totals, NEVER from
        # store["pv_daily_kwh"]. That store key mirrors the inverter's own daily
        # accumulator, which the inverter resets at its midnight — and this task
        # runs at 00:00:0x, after a poll has already copied the reset zero in. So
        # every record written here read 0.0 kWh: 9 of the 10 days to 14-Sep-2026. _compute_correction_bands filters
        # `0.1 < factor`, so the zeros never poisoned the bands — they STARVED
        # them, which is worse for being invisible. The 40 kWh band was still
        # fitted on twelve July/August samples on 15-Sep, returning x1.05 on a
        # month measuring 0.888, so the correction pushed a falling forecast UP.
        # _energy_day_totals is anchor[next day] - anchor[day], settled and exact.
        if self.forecast:
            _acc_pv = None
            try:
                _acc_pv = self._energy_day_totals(yesterday).get("pv")
            except Exception as exc:
                self.logger.warning(
                    f"[Forecast] Settled PV for {yesterday} unavailable ({exc}) — "
                    "skipping the accuracy record rather than writing a wrong one")
            # A missing or zero total is not evidence of a dark day. Skipping keeps
            # the bands on real samples; recording a zero is what broke this.
            if _acc_pv is not None and float(_acc_pv) > 0.0:
                self.forecast.record_accuracy(float(_acc_pv), date_str=yesterday)
            else:
                log(f"[Forecast] No settled PV total for {yesterday} — accuracy "
                    "record skipped (the bias bands keep their existing samples)",
                    level="WARNING")

        # Write daily history — every record kept, no cap since v5.7
        self._write_daily_history(yesterday)

        # Forecast accuracy: log the rolling 7-day summary so trends are
        # visible without having to read the JSON file.
        try:
            summary = self.forecast.get_accuracy_summary(window_days=7) if self.forecast else {"days": 0}
            if summary["days"] > 0:
                log(
                    f"[Forecast] 7-day accuracy: MAPE {summary['mape_pct']:.1f}%  "
                    f"mean factor {summary['mean_factor']:.2f}  "
                    f"(over: {summary['over_count']}, under: {summary['under_count']})"
                )
        except Exception as exc:
            self.logger.debug(f"Accuracy summary skipped: {exc}")

        # Weekly data-directory backup — once a week on Monday's midnight task.
        # Cheap insurance against accumulator/SoH/daily_history corruption.
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            if today_dt.weekday() == 0:
                self._backup_data_dir(today)
        except (ValueError, OSError) as exc:
            self.logger.debug(f"[Backup] Skipped: {exc}")

        # Battery State-of-Health snapshot — once a week, on Monday's midnight task.
        # LFP cells typically lose ~0.3%/year of capacity in normal cycling; anything
        # faster is worth flagging.  Cheap to compute and stored as a small JSON ring
        # buffer in data_dir.
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            if today_dt.weekday() == 0:   # Monday
                self._record_battery_soh_snapshot(today)
        except (ValueError, OSError) as exc:
            self.logger.debug(f"SoH snapshot skipped: {exc}")

        # Daily export-sync check: compare Sigenergy daily export totals against
        # what Octopus has settled (D-3 to D-9). One-line INFO summary, silent
        # if export MPAN isn't configured. Full results are visible on the
        # web dashboard's Export Sync card.
        try:
            self._log_export_sync_summary()
        except Exception as exc:
            self.logger.debug(f"[ExportSync] Skipped: {exc}")

        # Write final daily totals to Indigo variables before reset
        self._write_energy_summary_variables()
        self.store["last_energy_var"] = time.time()

        # A VPP window spanning midnight must keep its export delta intact
        # across the counter reset below, or the settlement figure goes
        # negative (see _vpp_export_anchor_after_midnight). Must run BEFORE
        # the reset — it needs the pre-reset total.
        if self.store.get("vpp_state", VPP_IDLE) == VPP_ACTIVE:
            _pre_total = self.store.get("grid_export_daily_kwh", 0.0)
            _old_start = self.store.get("vpp_export_start_kwh", 0.0)
            self.store["vpp_export_start_kwh"] = _vpp_export_anchor_after_midnight(
                _old_start, _pre_total)
            vpp_log(f"[VPP] Window spans midnight — export anchor re-based "
                f"({max(0.0, _pre_total - _old_start):.2f} kWh banked before the rollover)")

        # New day. v5.89.0: the daily figures derive from lifetime counters anchored
        # at this boundary (daily_energy.py) — nothing is zeroed by hand. The
        # projection is refreshed from the object, which already rolled over on
        # the first observe of the new day (or does so here if Modbus was out).
        self.store["peak_soc"]                  = 0.0
        self.store["min_soc"]                   = 100.0
        self.store["peak_pv_w"]                 = 0
        self.store["peak_pv_time"]              = ""
        self.store["today_date"]                = today
        self.store["energy_reconcile_warned"]   = ""
        # Legacy anchors (pre-5.89 accumulators file) — cleared so a downgrade
        # cannot read yesterday's boundary as today's.
        self.store["pv_lifetime_start_kwh"]     = None
        self.store["import_lifetime_start_kwh"] = None
        self.store["export_lifetime_start_kwh"] = None
        if de is not None:
            if de.today_date != today:
                if self.store.get("energy_yesterday_projection") is None:
                    self.store["energy_yesterday_projection"] = self._energy_projection_snapshot(yesterday)
                de.rollover(today)
                mb = getattr(self, "modbus", None)
                if mb is not None:
                    try:
                        mb.mark_slow_read_due(*ENERGY_BLOCK_KEYS)
                    except Exception as exc:
                        self.logger.debug(f"[Energy] could not mark the energy blocks due: {exc}")
            self._project_daily_energy()
        else:
            for _k in ("pv_daily_kwh", "grid_import_daily_kwh", "grid_export_daily_kwh",
                       "home_daily_kwh", "battery_charge_daily_kwh", "battery_discharge_daily_kwh",
                       "energy_balance_kwh"):
                self.store[_k] = 0.0
            self.store["energy_day_partial"] = False

        self._save_accumulators()

        # Rewrite the *_today_* variable set now the accumulators are zeroed —
        # without this the just-ended day's full totals sat in the today
        # variables for up to 30 minutes while yesterday was already inside
        # the month roll-up (a today+month double-count window).
        self._write_energy_summary_variables()
        self.store["last_energy_var"] = time.time()

    def _shadow_tariff_baseline(self, date_str):
        """Price the observed imports at Tracker and Agile, never reschedule them.

        A true Agile result needs a dispatch simulation (and future prices known
        when decisions were made). This intentionally answers the narrower,
        auditable question: what would the exact imported half-hours have cost
        at each tariff? Missing price coverage yields None rather than a made-up
        total. Evening means slots ending at or after 16:00 local time.
        """
        result = {
            "evening_import_kwh": 0.0, "priced_import_kwh": 0.0,
            "tracker_cost_gbp": None, "agile_cost_gbp": None,
            "priced_slots": 0, "missing_price_slots": 0,
        }
        db_path = os.path.join(self.data_dir, "energy_timeseries.db")
        if not os.path.exists(db_path):
            return result
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            rows = con.execute(
                """SELECT slot_end, grid_import_kwh, tracker_price_p, agile_price_p
                     FROM halfhourly WHERE substr(slot_end, 1, 10) = ?
                     ORDER BY slot_end""", (date_str,)).fetchall()
            con.close()
        except sqlite3.Error as exc:
            self.logger.debug(f"[Shadow] Cannot read tariff baseline: {exc}")
            return result

        tracker_cost = agile_cost = 0.0
        any_import = False
        complete = True
        for slot_end, import_kwh, tracker_p, agile_p in rows:
            try:
                imported = max(0.0, float(import_kwh or 0.0))
            except (TypeError, ValueError):
                continue
            if imported <= 0:
                continue
            any_import = True
            try:
                if int(str(slot_end)[11:13]) >= 16:
                    result["evening_import_kwh"] += imported
            except (TypeError, ValueError):
                pass
            try:
                tracker = float(tracker_p)
                agile = float(agile_p)
            except (TypeError, ValueError):
                complete = False
                result["missing_price_slots"] += 1
                continue
            result["priced_import_kwh"] += imported
            result["priced_slots"] += 1
            tracker_cost += imported * tracker / 100.0
            agile_cost += imported * agile / 100.0
        result["evening_import_kwh"] = round(result["evening_import_kwh"], 3)
        result["priced_import_kwh"] = round(result["priced_import_kwh"], 3)
        if any_import and complete:
            result["tracker_cost_gbp"] = round(tracker_cost, 4)
            result["agile_cost_gbp"] = round(agile_cost, 4)
        return result

    def _write_daily_history(self, date_str):
        """Append today's totals to the all-time daily history.

        Persists both the import rate (`rate_today_p`) and the export rate
        (`export_rate_p`) on every record so that historical economics roll-ups
        use the exact rate that was in effect on each day rather than today's
        live rate.  Without this, a future export-rate change would
        retroactively re-value every past day at the new rate.
        """
        # Capture the export rate as it stood on this day.  Falls back to
        # 12p (Octopus Outgoing flat from 26-Mar-2026) if the live feed
        # hasn't published a value yet.
        try:
            export_rate_p = float(
                (self.latest_rates_data or {}).get("export_rate_p", 0.0)
            )
            if export_rate_p <= 0:
                export_rate_p = DEFAULT_EXPORT_RATE_P
        except (TypeError, ValueError):
            export_rate_p = DEFAULT_EXPORT_RATE_P

        # On a BANDED export tariff one daily number is only meaningful weighted
        # by WHEN the kWh went out — the same day's export is worth two and a
        # half times as much at 17:00 as at 03:00.
        export_rate_p, export_rate_basis = self._export_rate_and_basis(
            date_str, export_rate_p)
        import_rate_p, import_rate_basis = self._import_rate_and_basis(
            date_str, (self.latest_rates_data or {}).get("tracker", {}).get("today_p"))

        # Capture the standing charges + gas unit rate in force on this day, so
        # the whole-house settle values each frozen day at its OWN rates rather
        # than whatever the ledger reads when the settle pass later runs.  A
        # tariff or price-cap change must never retroactively re-value past days.
        day_elec_standing_p = day_gas_unit_p = day_gas_standing_p = None
        try:
            _fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception:
            _fin = None
        if _fin:
            if _fin.get("elec"):
                day_elec_standing_p = _fin["elec"].get("standing_p")
            if _fin.get("gas"):
                day_gas_unit_p     = _fin["gas"].get("unit_p")
                day_gas_standing_p = _fin["gas"].get("standing_p")

        shadow_tariff = self._shadow_tariff_baseline(date_str)
        try:
            end_soc_pct = round(float(self.latest_inverter_data.get("batterySoc")), 1)
        except (TypeError, ValueError):
            end_soc_pct = None
        shadow_export = round(float(self.store.get("shadow_95_export_foregone_kwh", 0.0)), 3)
        shadow_samples = int(self.store.get("shadow_95_samples", 0) or 0)
        _shadow_live_pct = _as_float(self.pluginPrefs.get("solarOverflowTargetSoc"),
                                     SOLAR_OVERFLOW_TARGET_SOC_PCT)
        _shadow_pct      = self._shadow_target_pct()
        _bf_threshold = min(_as_float(self.pluginPrefs.get("solarOverflowBankFirstMaxKwh"),
                                      SOLAR_OVERFLOW_BANK_FIRST_MAX_KWH),
                            SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX)
        _bf_gate      = min(SOLAR_OVERFLOW_BANK_FIRST_SOC_MAX,
                            max(0.0, _as_float(self.pluginPrefs.get("solarOverflowBankFirstSoc"),
                                               SOLAR_OVERFLOW_BANK_FIRST_SOC_PCT)))

        totals = self._energy_day_totals(date_str)
        record = {
            "date":                 date_str,
            "month":                date_str[:7],
            "pv_kwh":               round(totals["pv"], 2),
            "pv_forecast_kwh":      round(self.latest_forecast_data.get("todayKwh", 0.0), 2),
            "grid_import_kwh":      round(totals["gridImport"], 2),
            "grid_export_kwh":      round(totals["gridExport"], 2),
            "home_kwh":             round(totals["home"], 2),
            "battery_charge_kwh":   round(totals["batteryCharge"], 2),
            "battery_discharge_kwh": round(totals["batteryDischarge"], 2),
            # v5.89.0 audit fields: the identity residual, whether any figure was
            # anchored late (a boundary the plugin missed), and how each was anchored.
            "energy_balance_kwh":   round(float(totals.get("balance", 0.0)), 2),
            "energy_partial":       bool(totals.get("partial", False)),
            "energy_sources":       dict(totals.get("sources") or {}),
            "peak_soc":   round(self.store["peak_soc"], 1),
            "min_soc":    round(self.store["min_soc"], 1),
            "tariff":     self.latest_rates_data.get("tariff_info", {}).get("tariff_key", "?"),
            # v5.110.0: on a banded import tariff this is the day's imports
            # weighted by band, as export_rate_p already was. Tracker days keep
            # the day's single published rate, exactly as before.
            "rate_today_p":   import_rate_p,
            "import_rate_basis": import_rate_basis,
            "export_rate_p":  round(export_rate_p, 4),
            # How that figure was arrived at: "weighted" means the day's own
            # half-hourly exports were priced against the published bands,
            # "flat" means one price applied all day. A reader of this history
            # can tell an exact figure from an approximate one.
            "export_rate_basis": export_rate_basis,
            "elec_standing_p_day": day_elec_standing_p,
            "gas_unit_p_day":      day_gas_unit_p,
            "gas_standing_p_day":  day_gas_standing_p,
            "import_events": 1 if self.store.get("had_import_today", False) else 0,
            "export_events": self.store.get("export_count_today", 0),
            "vpp_event":  self.store.get("had_vpp_today", False),
            "solar_overflow_shadow": {
                "enabled": bool(self.pluginPrefs.get("solarOverflowShadowEnabled", True)),
                # v5.106.0: READ, never literals. These were hardcoded 90.0/95.0 and
                # went on being written after the live target moved to 95 on
                # 31-Aug-2026, so every record for a fortnight named a comparison
                # that was not happening. A record about a setting has to read the
                # setting.
                "live_target_pct":   round(_shadow_live_pct, 1),
                "shadow_target_pct": round(_shadow_pct, 1),
                "samples": shadow_samples,
                # Positive = export the LOWER of the two targets would have made and
                # the higher one withholds. Direction is derived from the pair above,
                # not fixed, so it stays true whichever way the targets sit.
                "estimated_export_foregone_kwh": shadow_export,
                # Why a zero-sample day was zero. Without it a dead experiment and a
                # day with no overflow at all look exactly alike.
                "skipped_reason": str(self.store.get("shadow_skip_reason") or ""),
                "observed_end_soc_pct": end_soc_pct,
                "observed_evening_import_kwh": shadow_tariff["evening_import_kwh"],
                "tariff_baseline": shadow_tariff,
            },
            # Separate from solar_overflow_shadow above, deliberately: that block is a
            # pacing experiment between two SOC targets, and folding a second question
            # into it would make both reports lie.
            "bank_first": {
                "mode":                  "live",
                "threshold_kwh":         _bf_threshold,
                "gate_soc_pct":          _bf_gate,
                # BOTH of these describe 23:59, which is why they are named for it
                # now. They are the last state of the latch, not the verdict that
                # governed the day — see _record_bank_first_metrics.
                "classified_small":          bool(self.store.get("bank_first_small_latched", False)),
                "classified_from_kwh":       round(self.latest_forecast_data.get("todayKwh", 0.0), 2),
                # The verdict that actually governed the morning, and the forecast it
                # was reached from. v5.106.0 — absent on rows written before it.
                "first_classified_small":    self.store.get("bank_first_first_class_small"),
                "first_classified_from_kwh": self.store.get("bank_first_first_class_kwh"),
                "first_classified_local":    self.store.get("bank_first_first_class_local") or None,
                "promoted_local":            self.store.get("bank_first_promoted_local") or None,
                "forecast_status":       str(self.latest_forecast_data.get("forecastStatus", "")),
                "blocked_samples":       int(self.store.get("bank_first_blocked_samples", 0)),
                "first_block_local":     self.store.get("bank_first_first_block_local") or None,
                "released_local":        self.store.get("bank_first_released_local") or None,
                "minutes_held":          int(self.store.get("bank_first_blocked_samples", 0)),
                "export_withheld_kwh":   round(float(self.store.get("bank_first_withheld_kwh", 0.0)), 3),
                "peak_soc_pct":          round(self.store["peak_soc"], 1),
                "minutes_soc_ge_95":     int(self.store.get("bank_first_minutes_soc_ge_95", 0)),
                "minutes_soc_ge_99":     int(self.store.get("bank_first_minutes_soc_ge_99", 0)),
                # The number that decides whether this feature is free. Zero on every
                # small day means banking cost nothing measurable.
                "clip_boundary_minutes": int(self.store.get("bank_first_clip_boundary_min", 0)),
                "arm_minutes":           int(self.store.get("bank_first_arm_minutes", 0)),
                "first_arm_local":       self.store.get("bank_first_first_arm_local") or None,
                "measured_peak_surplus_kw": round(float(self.store.get("bank_first_peak_surplus_kw", 0.0)), 2),
            },
        }

        path    = os.path.join(self.data_dir, "daily_history.json")
        records = []
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    records = json.load(f)
        except Exception:
            pass

        records.append(record)
        # Retention: keep every record indefinitely (v5.7).  Each record is
        # ~280 bytes; even 50 years of daily data is < 6 MB JSON.  The user
        # explicitly asked to never lose history.  No pruning.

        try:
            _atomic_write_json(path, records)
        except Exception as e:
            log(f"Cannot write daily history: {e}", level="ERROR")

        # Reset daily counters
        self.store["had_import_today"]   = False
        self.store["export_count_today"] = 0
        self.store["had_vpp_today"]      = False
        self.store["shadow_95_export_foregone_kwh"] = 0.0
        self.store["shadow_95_samples"]             = 0
        self.store["shadow_skip_reason"]            = ""
        # Bank-first daily counters. The LATCH is not reset here — it is keyed on the
        # local date inside _record_bank_first_metrics, so it clears itself on the
        # first tick of the new day whether or not midnight recording ran.
        self.store["bank_first_blocked_samples"]   = 0
        self.store["bank_first_withheld_kwh"]      = 0.0
        self.store["bank_first_first_block_local"] = ""
        self.store["bank_first_released_local"]    = ""
        self.store["bank_first_release_logged"]    = False
        self.store["bank_first_minutes_soc_ge_95"] = 0
        self.store["bank_first_minutes_soc_ge_99"] = 0
        self.store["bank_first_clip_boundary_min"] = 0
        self.store["bank_first_arm_minutes"]       = 0
        self.store["bank_first_first_arm_local"]   = ""
        self.store["bank_first_peak_surplus_kw"]   = 0.0

    # ================================================================
    # Data directory backup (weekly, Monday midnight)
    # ================================================================

    def _backup_data_dir(self, date_str):
        """Write a tar.gz of the small JSON / SQLite files in the data dir.

        Cheap protection against accumulator / SoH / daily_history corruption.
        Backups go to a sibling 'data_backup/' folder; only the 8 most recent
        are kept (~2 months of weekly snapshots).
        """
        import tarfile
        files_to_backup = [
            "accumulators.json",
            "daily_history.json",
            "home_load_profile.json",
            "soh_history.json",
            "forecast_accuracy.json",
            "energy_timeseries.db",
            "openmeteo_combined_cache.json",
        ]
        backup_dir = os.path.join(self.data_dir, "data_backup")
        try:
            os.makedirs(backup_dir, exist_ok=True)
        except OSError as exc:
            self.logger.warning(f"[Backup] Cannot create backup dir: {exc}")
            return

        tar_name = f"sigen_data_{date_str}.tar.gz"
        tar_path = os.path.join(backup_dir, tar_name)
        added = 0
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                for fname in files_to_backup:
                    fpath = os.path.join(self.data_dir, fname)
                    if os.path.exists(fpath):
                        tar.add(fpath, arcname=fname)
                        added += 1
            size_kb = os.path.getsize(tar_path) / 1024.0
            log(f"[Backup] Wrote {tar_name} ({added} files, {size_kb:.1f} KB)")
        except OSError as exc:
            self.logger.warning(f"[Backup] Failed: {exc}")
            return

        # Retention: keep the 8 most recent tarballs.  Sort by filename which
        # embeds an ISO date, so lexicographic sort = chronological.
        try:
            existing = sorted(
                fn for fn in os.listdir(backup_dir)
                if fn.startswith("sigen_data_") and fn.endswith(".tar.gz")
            )
            for fn in existing[:-8]:
                try:
                    os.remove(os.path.join(backup_dir, fn))
                    self.logger.debug(f"[Backup] Pruned old backup {fn}")
                except OSError:
                    pass
        except OSError:
            pass

    # ================================================================
    # Auto-update notifier (GitHub releases)
    # ================================================================

    def _check_for_update(self):
        """Hit the GitHub releases API once per day and log if a newer plugin
        version is available.  Best-effort — silent if offline or rate-limited.

        Stores the last-check timestamp in pluginPrefs so a plugin restart
        doesn't re-query within 24h.  Compares the GitHub tag's leading
        version digits (so 'v5.2' / 'v5.2.0' / '5.2' all parse equivalently).
        """
        last_check_str = self.pluginPrefs.get("lastUpdateCheck", "")
        try:
            last_check = datetime.fromisoformat(last_check_str)
        except (ValueError, TypeError):
            last_check = datetime.min
        if (datetime.now() - last_check).total_seconds() < 86400:
            return   # already checked within the last 24 hours

        import urllib.request
        import urllib.error

        url = ("https://api.github.com/repos/Highsteads/SigenEnergyManager/"
               "releases/latest")
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"SigenEnergyManager/{self.pluginVersion}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            self.logger.debug(f"[Update] Check failed (silent): {exc}")
            return

        latest_raw = (payload.get("tag_name") or payload.get("name") or "").strip()
        if not latest_raw:
            return

        # Strip a leading 'v' / 'V' and any non-version suffix
        clean = latest_raw.lstrip("vV").split(" ", 1)[0]

        def _parse(s):
            try:
                return tuple(int(x) for x in s.split(".")[:3])
            except (ValueError, AttributeError):
                return ()

        latest_tuple  = _parse(clean)
        current_tuple = _parse(self.pluginVersion)
        if not latest_tuple or not current_tuple:
            return

        if latest_tuple > current_tuple:
            log(
                f"[Update] New plugin version available: {latest_raw} "
                f"(running {self.pluginVersion}). "
                f"See {payload.get('html_url', 'https://github.com/Highsteads/SigenEnergyManager/releases')}"
            )
        else:
            self.logger.debug(
                f"[Update] Plugin is up to date ({self.pluginVersion})"
            )

        self.pluginPrefs["lastUpdateCheck"] = datetime.now().isoformat()

    # ================================================================
    # Battery State-of-Health tracking
    # ================================================================

    def _record_battery_soh_snapshot(self, date_str):
        """Append a weekly SoH snapshot to soh_history.json (data_dir).

        Logs a WARNING if SoH has dropped by more than 2 percentage points in
        the last year, or more than 1 point in the last 4 weeks — either
        indicates faster-than-expected degradation for LFP chemistry and is
        worth investigating (cell imbalance, BMS calibration drift, etc.).
        """
        inv = self.latest_inverter_data or {}
        soh = float(inv.get("batterySoh", 0.0) or 0.0)
        if soh <= 0.0:
            self.logger.debug("[SoH] No batterySoh reading yet — snapshot skipped")
            return

        path = os.path.join(self.data_dir, "soh_history.json")
        records = []
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    records = json.load(f)
        except (OSError, ValueError):
            records = []

        records.append({"date": date_str, "soh": round(soh, 2)})
        # Keep ~5 years of weekly snapshots (260 entries)
        if len(records) > 260:
            records = records[-260:]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f)
        except OSError as exc:
            self.logger.warning(f"[SoH] Cannot persist soh_history.json: {exc}")

        # Degradation checks — compare against 4-weeks and 52-weeks ago.
        def _lookup(n_weeks_ago):
            if len(records) < n_weeks_ago + 1:
                return None
            return records[-(n_weeks_ago + 1)].get("soh")

        prev_month = _lookup(4)
        prev_year  = _lookup(52)
        log(f"[SoH] Weekly snapshot: {soh:.1f}% "
            f"(4w ago: {prev_month if prev_month is not None else 'n/a'},  "
            f"52w ago: {prev_year if prev_year is not None else 'n/a'})")
        if prev_month is not None and (prev_month - soh) > 1.0:
            log(
                f"[SoH] WARNING: capacity dropped {prev_month - soh:.1f}% in 4 weeks. "
                f"LFP norm is < 0.1%/4 weeks — investigate BMS / cell balance.",
                level="WARNING",
            )
        if prev_year is not None and (prev_year - soh) > 2.0:
            log(
                f"[SoH] WARNING: capacity dropped {prev_year - soh:.1f}% in 52 weeks. "
                f"Expected LFP degradation is ~0.3%/year — consider raising with installer.",
                level="WARNING",
            )

    # ================================================================
    # Trigger Events
    # ================================================================

    def triggerStartProcessing(self, trigger):
        """Indigo lifecycle: called when a trigger configured against this
        plugin is enabled.  Store the trigger object so _trigger_event() can
        fire it later via indigo.trigger.execute().
        """
        self.event_triggers[trigger.id] = trigger
        if self.debug:
            log(f"[Trigger] Registered: id={trigger.id} type={trigger.pluginTypeId}")

    def triggerStopProcessing(self, trigger):
        """Indigo lifecycle: called when a trigger is disabled or deleted."""
        self.event_triggers.pop(trigger.id, None)
        if self.debug:
            log(f"[Trigger] Unregistered: id={trigger.id} type={trigger.pluginTypeId}")

    def _trigger_event(self, event_id):
        """Fire all registered Indigo triggers whose pluginTypeId matches event_id.

        indigo.trigger.execute() requires a trigger object (NOT a string event ID),
        and indigo.server.fireEvent() does not exist.  We iterate self.event_triggers
        (populated by triggerStartProcessing) and fire every matching one.
        """
        fired = 0
        for trigger in self.event_triggers.values():
            if trigger.pluginTypeId == event_id:
                try:
                    indigo.trigger.execute(trigger)
                    fired += 1
                except Exception as e:
                    log(f"[Trigger] execute failed for {event_id} (id={trigger.id}): {e}", level="ERROR")
        if fired == 0 and self.debug:
            # Not an error — user may simply have no triggers wired to this event.
            log(f"[Trigger] No triggers configured for event '{event_id}'")

    # ================================================================
    # Device State Updates
    # ================================================================

    def _pack_count(self):
        """How many battery modules the stack holds.

        ASK THE HARDWARE FIRST: register 31024 is the PACK/BCU count in the
        official protocol, so the inverter simply tells us (4 here). That
        beats the old arithmetic, which divided capacity by an assumed
        8.76 kWh module — right for a SigenStor, a guess for anything else.
        Order: explicit pref -> the register -> the division -> 0.

        Returns 0 when it cannot be worked out, and the pack-balance
        inference then simply doesn't run, which is the right answer for a
        system whose shape we do not know."""
        prefs = getattr(self, "pluginPrefs", None) or {}
        override = _as_int(prefs.get("batteryPackCount"), 0)
        if override > 0:
            return override
        reported = _as_int((getattr(self, "latest_inverter_data", None) or {}).get("packCount"), 0)
        if 0 < reported <= 16:          # the protocol's own sanity bound
            return reported
        module = _as_float(prefs.get("batteryModuleKwh"), 8.76)
        if module <= 0:
            return 0
        n = int(round(BATTERY_CAPACITY_KWH / module))
        # Only trust a clean division — a capacity that is not a whole number
        # of modules means the module size is wrong for this stack.
        if n < 1 or abs(n * module - BATTERY_CAPACITY_KWH) > 0.5:
            return 0
        return n

    def _pv_strings_status(self, inv):
        """Per-string list for /api/status: [{n, label, v, a, w, kwp?}, ...].

        [] when the inverter reports none — consumers hide rather than invent.
        kwp rides along only when the pvStringLabels pref supplies it, so the
        dashboards can scale each string's bar to its own capacity once the
        string→roof mapping is named."""
        strings = (inv or {}).get("pvStrings") or []
        labels = _parse_pv_string_labels(
            self.pluginPrefs.get("pvStringLabels", ""), len(strings))
        out = []
        for i, s in enumerate(strings):
            entry = {"n": i + 1, "label": labels[i]["label"],
                     "v": s.get("v"), "a": s.get("a"), "w": s.get("w")}
            if labels[i]["kwp"]:
                entry["kwp"] = labels[i]["kwp"]
            out.append(entry)
        return out

    def _update_inverter_device(self, data):
        """Push Modbus data to sigenergyInverter device."""
        dev = self._find_device("sigenergyInverter")
        if not dev:
            return
        # Numeric states are written as real numbers (not str()) so Indigo's
        # built-in history records them as chartable columns. Coercion is guarded
        # via _as_int/_as_float so a missing/odd Modbus value can never crash the
        # state write. Categorical states (emsWorkMode, gridStatus, etc.) stay str.
        states = [
            {"key": "emsWorkMode",              "value": str(data.get("emsWorkMode", ""))},
            {"key": "gridSensorConnected",      "value": str(data.get("gridSensorConnected", False))},
            {"key": "gridPowerWatts",           "value": _as_int(data.get("gridPowerWatts"), 0)},
            {"key": "gridStatus",               "value": str(data.get("gridStatus", ""))},
            # 0 only on a GENUINE off-grid status — an unmapped "Unknown (N)" read must
            # not chart a false power cut (mirrors the _poll_modbus edge detection).
            {"key": "gridOnline",               "value": 0 if str(data.get("gridStatus", "")).startswith("Off-grid") else 1},
            _num_state("batterySoc",               _as_float(data.get("batterySoc"), 0.0),            1),
            {"key": "pvPowerWatts",             "value": _as_int(data.get("pvPowerWatts"), 0)},
            {"key": "batteryPowerWatts",        "value": _as_int(data.get("batteryPowerWatts"), 0)},
            {"key": "homePowerWatts",           "value": _as_int(data.get("homePowerWatts"), 0)},
            {"key": "plantRunningState",        "value": str(data.get("plantRunningState", ""))},
            _num_state("dischargeCutoffSoc",       _as_float(data.get("dischargeCutoffSoc"), 0.0),    1),
            _num_state("batterySoh",               _as_float(data.get("batterySoh"), 0.0),            1),
            _num_state("batteryTempC",             _as_float(data.get("batteryTempC"), 0.0),          1),
            _num_state("batteryCellVoltage",       _as_float(data.get("batteryCellVoltage"), 0.0),    3),
            _num_state("batteryMaxTempC",          _as_float(data.get("batteryMaxTempC"), 0.0),       1),
            _num_state("batteryMinTempC",          _as_float(data.get("batteryMinTempC"), 0.0),       1),
            # v5.89.0: from the projection, never from data — the inverter's daily
            # registers are present only on the cycle they were read, and a 0.0
            # written on the other cycles would chart as a real reading.
            _num_state("batteryDailyChargeKwh",    self.store.get("battery_charge_daily_kwh", 0.0),    2),
            _num_state("batteryDailyDischargeKwh", self.store.get("battery_discharge_daily_kwh", 0.0), 2),
            _num_state("pvDailyKwh",               self.store["pv_daily_kwh"],          2),
            _num_state("gridDailyImportKwh",       self.store["grid_import_daily_kwh"], 2),
            _num_state("gridDailyExportKwh",       self.store["grid_export_daily_kwh"], 2),
            _num_state("homeDailyKwh",             self.store["home_daily_kwh"],        2),
            _num_state("energyBalanceKwh",         self.store.get("energy_balance_kwh", 0.0), 2),
            {"key": "modbusConnected",          "value": "True"},
            {"key": "lastUpdate",               "value": data.get("lastUpdate", "")},
        ]
        # Per-PV-string readings (v5.67.0). Written only for strings the
        # inverter actually reported — an unreported string's states stay at
        # their last value rather than being stamped with a fabricated 0
        # (states are only meaningful alongside a present pvStrings key, and
        # a transient block failure must not chart a phantom string dropout).
        for i, s in enumerate((data.get("pvStrings") or [])[:4], start=1):
            states.append(_num_state(f"pv{i}Volts", s.get("v"), 1))
            states.append(_num_state(f"pv{i}Amps",  s.get("a"), 2))
            states.append(_num_state(f"pv{i}Watts", s.get("w"), 0))
        # Grid frequency (v5.68.0) — same rule: only when actually read.
        if data.get("gridFrequencyHz") is not None:
            states.append(_num_state("gridFrequencyHz",
                                     _as_float(data.get("gridFrequencyHz"), 0.0), 2))
        # Inverter self-diagnostics (v5.69.0), named from the official V2.7
        # protocol. Each written only when actually read.
        if data.get("pcsInternalTempC") is not None:
            states.append(_num_state("pcsInternalTempC",
                                     _as_float(data.get("pcsInternalTempC"), 0.0), 1))
        if data.get("insulationResistanceMohm") is not None:
            states.append(_num_state("insulationResistanceMohm",
                                     _as_float(data.get("insulationResistanceMohm"), 0.0), 3))
        # Grid voltage (v5.71.0). Numeric so the SQL Logger charts it — the
        # useful question is not "what is it now" but "how often does it touch
        # 253 V", which is a week of history, not a reading.
        if data.get("gridVoltageV") is not None:
            states.append(_num_state("gridVoltageV",
                                     _as_float(data.get("gridVoltageV"), 0.0), 2))
        if data.get("gridCurrentA") is not None:
            states.append(_num_state("gridCurrentA",
                                     _as_float(data.get("gridCurrentA"), 0.0), 2))
        if data.get("alarm1Raw") is not None:
            # The raw word decodes via Appendix 2, which we do NOT carry — so
            # report the honest binary fact (something is raised / nothing is)
            # rather than inventing a description for a code we cannot name.
            states.append({"key": "inverterAlarm",
                           "value": "clear" if int(data["alarm1Raw"]) == 0 else "raised"})
            states.append(_num_state("inverterAlarmRaw",
                                     _as_float(data.get("alarm1Raw"), 0.0), 0))
        # Pack balance (v5.68.0). Charting the SPREAD is the point: a single
        # reading says little, but a spread that widens week on week is a
        # cooling problem or a pack going off, and nothing else in the system
        # would show it. Written only when the inference actually held.
        # Wrapped whole: this is the write that carries SOC, and an advisory
        # figure must never be able to cost it (the v5.58.0 rule — an extra
        # on the drive path never breaks the thing it rides on).
        try:
            bal = analyse_pack_balance(data.get("batteryTempC"),
                                       data.get("batteryMaxTempC"),
                                       data.get("batteryMinTempC"),
                                       self._pack_count())
        except Exception as exc:
            self.logger.debug(f"pack-balance skipped: {exc}")
            bal = None
        if bal:
            states.append(_num_state("packTempSpreadC", bal["spread_c"], 1))
            states.append({"key": "packBalance", "value": bal["verdict"]})
        dev.updateStatesOnServer(states)
        dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

    def _update_inverter_device_offline(self):
        """Mark inverter device as offline."""
        dev = self._find_device("sigenergyInverter")
        if dev:
            dev.updateStateOnServer("modbusConnected", value="False")
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def _update_manager_device(self, decision, snapshot):
        """Push battery manager decision state to batteryManager device."""
        dev = self._find_device("batteryManager")
        if not dev:
            return

        scheduled_str = ""
        if decision.scheduled_time:
            scheduled_str = decision.scheduled_time.strftime("%H:%M")

        action_display = {
            ACTION_SELF_CONSUMPTION: "Self Consumption",
            ACTION_SOLAR_OVERFLOW:   "Solar Overflow Export",
            ACTION_START_IMPORT:     "Grid Import Active",
            ACTION_STOP_IMPORT:      "Import Stopping",
            ACTION_SCHEDULE_IMPORT:  "Import Scheduled",
            ACTION_START_EXPORT:     "Night Export Active",
            ACTION_STOP_EXPORT:      "Export Stopping",
            ACTION_VPP_EXPORT:       "VPP Export Active",
            ACTION_SAVING_SESSION:   "Saving Session Export",
            ACTION_HAPPY_HOUR_IMPORT: "Happy Hour Import",
        }.get(decision.action, decision.action)

        # Flood prevention visibility (v4.5)
        flood_target_pct  = self.store.get("flood_prev_target_soc")
        flood_active      = bool(flood_target_pct)

        # Power cut lockout visibility (v4.5)
        lockout_active    = bool(self.store.get("power_cut_lockout_active", False))
        lockout_remain_min = ""
        if lockout_active:
            prt = self.store.get("power_restored_time")
            if prt and isinstance(prt, datetime):
                # Re-promote to UTC if naive
                _prt = prt if prt.tzinfo else prt.replace(tzinfo=timezone.utc)
                _hours_since = (datetime.now(timezone.utc) - _prt).total_seconds() / 3600.0
                _hours_left  = max(0.0, POWER_CUT_LOCKOUT_HOURS - _hours_since)
                lockout_remain_min = str(int(round(_hours_left * 60)))

        # v5.14: Compute tomorrow's solar/need using the *same* logic
        # battery_manager._calculate_24h_balance() uses (lines 405-406 of
        # battery_manager.py) so external scripts that read these states
        # can never disagree with the plugin's flood-prevention gate.
        # The 12-May-2026 incident: optimiser script said "export will
        # happen" (40 kWh vs 11 kWh = 3.6x) but plugin saw 63 kWh vs 22 kWh
        # = 2.81x, just under the 3.0x gate -> no export ran.
        try:
            _local_now = snapshot.now.astimezone(ZoneInfo("Europe/London"))
        except Exception:
            _local_now = snapshot.now
        _tomorrow_weekday  = (_local_now.date() + timedelta(days=1)).weekday()
        _tomorrow_need_kwh = _need_for_weekday(snapshot, _tomorrow_weekday)
        _tomorrow_solar_kwh = snapshot.corrected_tomorrow_kwh

        # currentAction and currentMode deliberately keep their existing tokens — seven
        # HTML sites and a List enum key off those strings, and a hold is not a new
        # action, it is self-consumption with a reason. The reason is where it shows.
        _bank_first_holding = bool(getattr(decision, "bank_first_holding", False))
        # Publish the CONFIGURED gate whether or not the hold is currently biting.
        # The decision only carries a gate while it is holding, and publishing that
        # 0.0 the rest of the time would put a number on the device that means
        # nothing — which a reader will take to mean the gate is zero. Read the pref
        # instead, and show a real level all day.
        _bank_first_gate    = min(SOLAR_OVERFLOW_BANK_FIRST_SOC_MAX,
                                  max(0.0, _as_float(
                                      self.pluginPrefs.get("solarOverflowBankFirstSoc"),
                                      SOLAR_OVERFLOW_BANK_FIRST_SOC_PCT)))
        _reason_display     = (
            f"Banking first to {_bank_first_gate:.0f}% — daytime export held | {decision.reason}"
            if _bank_first_holding else decision.reason
        )

        states = [
            {"key": "managerStatus",       "value": "Running" if not self.store["vpp_active"] else "VPP Active"},
            {"key": "currentAction",       "value": action_display},
            # currentMode (List enum) appended below, guarded — see note before
            # the updateStatesOnServer call.
            {"key": "currentReason",       "value": _reason_display[:255]},
            {"key": "dawnViable",          "value": str(decision.dawn_viable)},
            {"key": "socAtDawn",           "value": str(round(decision.soc_at_dawn_kwh, 2))},
            {"key": "importActive",        "value": str(self.store["import_active"])},
            {"key": "importScheduled",     "value": str(bool(self.store["import_scheduled_time"]))},
            {"key": "importScheduledTime", "value": scheduled_str},
            {"key": "importKwh",           "value": str(round(decision.import_kwh, 2))},
            {"key": "exportActive",        "value": str(self.store["export_active"])},
            {"key": "exportKw",            "value": str(round(decision.export_kw, 1))},
            {"key": "floodPrevActive",     "value": str(flood_active)},
            {"key": "floodPrevTarget",     "value": str(flood_target_pct) if flood_active else ""},
            {"key": "powerCutLockoutActive",        "value": str(lockout_active)},
            {"key": "powerCutLockoutRemainingMin",  "value": lockout_remain_min},
            {"key": "tariffActive",        "value": snapshot.tariff.tariff_key},
            {"key": "rateToday",           "value": str(snapshot.tariff.today_rate_p or "")},
            {"key": "rateTomorrow",        "value": str(snapshot.tariff.tomorrow_rate_p or "")},
            {"key": "tomorrowSolarKwh",    "value": round(_tomorrow_solar_kwh, 2)},
            {"key": "tomorrowNeedKwh",     "value": round(_tomorrow_need_kwh, 1)},
            # v5.90.0
            {"key": "needTodayKwh",        "value": round(float(self.store.get("need_today_kwh") or 0.0), 1)},
            {"key": "pvTrackingPct",       "value": int(round(100.0 * float(self.store.get("pv_track_ratio") or 1.0)))},
            {"key": "lastUpdate",          "value": datetime.now().strftime("%H:%M:%S")},
        ]
        # currentMode is a List-enum registered ASYNCHRONOUSLY after the
        # stateListOrDisplayStateIdChanged() call in deviceStartComm. On the
        # very first evaluate tick right after a plugin restart it may not be
        # registered yet, and writing it then logs a spurious red
        #   device "Battery Manager" state key currentMode not defined
        # ERROR (the rest of the batch still applies, so it's harmless — but it
        # breaks the "no alarming red lines for expected conditions" rule). Only
        # include it once the state actually exists; the next tick (<=60s) writes
        # the real mode, so nothing is lost.
        if "currentMode" in dev.states:
            states.append({"key": "currentMode",
                           "value": ACTION_MODE_TOKEN.get(decision.action, "selfConsumption")})
        # v5.78.0. Same first-tick guard as currentMode above — a state added in
        # this version is not registered until stateListOrDisplayStateIdChanged
        # has run, and writing it early logs a red line for nothing.
        if "happyHourActive" in dev.states:
            states.append({"key": "happyHourActive",
                           "value": str(bool(self.store.get("happy_hour_import_active")))})
        if "happyHourFreeKwhLast" in dev.states:
            states.append({"key": "happyHourFreeKwhLast",
                           "value": round(float(self.store.get("happy_hour_free_kwh", 0.0) or 0.0), 2)})
        # -1 stands for "not reported yet", because an Integer state cannot hold
        # None and 0 is a real balance that means something quite different. The
        # distinction matters: 0 tokens is "earn some", unknown is "ask Octopus".
        if "happyHourTokens" in dev.states:
            _tok = self.store.get("happy_hour_tokens")
            states.append({"key": "happyHourTokens",
                           "value": int(_tok) if _tok is not None else -1})
        if "awayMode" in dev.states:
            states.append({"key": "awayMode",
                           "value": str(bool(self.store.get("away_active")))})
        # v5.79.0. Same first-tick guard again — these three did not exist before
        # this version, so on the tick straight after the upgrade they are not yet
        # registered.
        if "bankFirstActive" in dev.states:
            states.append({"key": "bankFirstActive", "value": str(_bank_first_holding)})
        if "bankFirstTargetSoc" in dev.states:
            states.append({"key": "bankFirstTargetSoc",
                           "value": str(round(_bank_first_gate, 1))})
        if "bankFirstForecastKwh" in dev.states:
            states.append({"key": "bankFirstForecastKwh",
                           "value": str(round(float(snapshot.raw_today_kwh or 0.0), 1))})
        dev.updateStatesOnServer(states)

    def _update_forecast_device(self, data):
        """Push Open-Meteo forecast to solarForecast device."""
        dev = self._find_device("solarForecast")
        if not dev:
            return
        states = [
            {"key": "todayKwh",             "value": str(data.get("todayKwh", 0.0))},
            {"key": "tomorrowKwh",          "value": str(data.get("tomorrowKwh", 0.0))},
            {"key": "correctedTodayKwh",    "value": str(data.get("correctedTodayKwh", 0.0))},
            {"key": "correctedTomorrowKwh", "value": str(data.get("correctedTomorrowKwh", 0.0))},
            {"key": "biasFactor",           "value": str(data.get("biasFactor", 1.0))},
            {"key": "remainingTodayKwh",    "value": str(data.get("remainingTodayKwh", 0.0))},
            {"key": "currentHourWatts",     "value": str(data.get("currentHourWatts", 0))},
            {"key": "nextHourWatts",        "value": str(data.get("nextHourWatts", 0))},
            {"key": "forecastStatus",       "value": str(data.get("forecastStatus", ""))},
            {"key": "lastUpdate",           "value": data.get("lastUpdate", "")},
        ]
        dev.updateStatesOnServer(states)

    def _sigenergy_folder_id(self) -> int:
        """Return the Sigenergy variable folder ID, creating the folder if it
        doesn't exist yet (fresh installs would otherwise dump 25+ plugin
        variables at the root of the Variables window).  Falls back to 0
        (root) only if the create itself fails.

        Result is cached on the instance after the first successful lookup so
        the half-hourly variable-write path doesn't re-scan every folder.
        The cache is invalidated when the folder cannot be resolved and again
        on plugin restart (instance is fresh).
        """
        cached = getattr(self, "_sigen_folder_id_cache", None)
        if cached is not None:
            return cached
        result = 0
        try:
            for fid in indigo.variables.folders:
                f = indigo.variables.folders[fid]
                if f.name.lower() in ("sigenergy", "sigen energy", "sigen"):
                    result = f.id
                    break
        except Exception:
            result = 0
        if not result:
            # No existing folder — create it so plugin variables stay grouped.
            try:
                folder = indigo.variables.folder.create("Sigenergy")
                result = folder.id
                log(f"[Energy Vars] Created 'Sigenergy' variable folder (id={result})")
            except Exception as exc:
                self.logger.warning(f"[Energy Vars] Could not create 'Sigenergy' folder: {exc}")
                result = 0
        # Only cache successful hits — a 0 result means the create failed and
        # we want a future call to retry rather than pin variables to root.
        if result:
            self._sigen_folder_id_cache = result
        return result

    def _ensure_var(self, name: str, folder_id: int) -> int:
        """
        Return the Indigo variable ID for `name`, creating it in `folder_id`
        if it does not already exist. Caches the result in self._energy_var_ids.
        """
        if name in self._energy_var_ids:
            return self._energy_var_ids[name]
        # Look up by name
        try:
            v = indigo.variables[name]
            self._energy_var_ids[name] = v.id
            return v.id
        except KeyError:
            pass
        # Create it
        try:
            v = indigo.variable.create(name, value="", folder=folder_id)
            self._energy_var_ids[name] = v.id
            log(f"[Energy Vars] Created variable '{name}' (id={v.id})")
            return v.id
        except Exception as exc:
            log(f"[Energy Vars] Could not create '{name}': {exc}", level="WARNING")
            return 0

    def _init_timeseries_db(self):
        """Create energy_timeseries.db in data_dir if it does not already exist."""
        db_path = os.path.join(self.data_dir, "energy_timeseries.db")
        con = None
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            con.execute("""
                CREATE TABLE IF NOT EXISTS halfhourly (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_start       TEXT    NOT NULL UNIQUE,
                    slot_end         TEXT    NOT NULL,
                    grid_import_kwh  REAL    NOT NULL DEFAULT 0.0,
                    grid_export_kwh  REAL    NOT NULL DEFAULT 0.0,
                    pv_kwh           REAL    NOT NULL DEFAULT 0.0,
                    home_kwh         REAL    NOT NULL DEFAULT 0.0,
                    battery_soc_start_pct REAL,
                    battery_soc_end_pct   REAL,
                    battery_net_kwh  REAL,
                    tracker_price_p  REAL,
                    agile_price_p    REAL,
                    manager_action   TEXT,
                    battery_charge_kwh    REAL,
                    battery_discharge_kwh REAL
                )
            """)
            # Existing installations have the older table. SQLite only gained
            # ADD COLUMN support suitable for this small migration long ago;
            # inspect first so startup remains idempotent.
            columns = {row[1] for row in con.execute("PRAGMA table_info(halfhourly)")}
            for _col in ("agile_price_p", "battery_charge_kwh", "battery_discharge_kwh"):
                if _col not in columns:
                    con.execute(f"ALTER TABLE halfhourly ADD COLUMN {_col} REAL")
            con.commit()
            log(f"[Timeseries] DB ready: {db_path}")
        except sqlite3.Error as exc:
            log(f"[Timeseries] DB init failed: {exc}", level="ERROR")
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    def _log_halfhourly_to_db(self):
        # v5.45.0: reads/advances store anchors — locked (local sqlite, fast).
        with self._state_lock:
            return self._log_halfhourly_to_db_impl()

    def _log_halfhourly_to_db_impl(self):
        """Append one half-hourly slot to energy_timeseries.db.

        Computes energy deltas since the last write. On the very first call
        (anchors are None) the deltas would span an unknown period so the
        row is skipped and anchors are seeded for the next call instead.
        """
        db_path = os.path.join(self.data_dir, "energy_timeseries.db")
        if not os.path.exists(db_path):
            return

        now_dt   = datetime.now()
        slot_end = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
        slot_start_dt = now_dt - timedelta(seconds=ENERGY_VAR_INTERVAL)
        slot_start = slot_start_dt.strftime("%Y-%m-%dT%H:%M:%S")

        # v5.89.0: deltas of the LIFETIME counters between two writes. The old
        # form subtracted the daily store and clamped at zero, which threw away
        # the pre-midnight part of the slot spanning midnight every night —
        # winter_import_forecast.py had already noticed "44-47 of 48 slots".
        de   = getattr(self, "daily_energy", None)
        snap = de.lifetime_snapshot() if de is not None else {}
        inv_data  = self.latest_inverter_data or {}
        cur_soc   = float(inv_data.get("batterySoc", 0.0))
        cap_kwh   = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), "35.04")
        need = ("pv", "gridImport", "gridExport", "home")
        if any(k not in snap for k in need):
            return                                   # no complete reading yet — next slot
        anchor     = self.store.get("hh_anchor_lifetime")
        anchor_soc = self.store.get("hh_anchor_soc_pct")

        # Seed anchors on first call (or an upgrade from the pre-5.89 daily-store
        # anchors) — skip writing this slot, its period is unknown.
        if not isinstance(anchor, dict) or any(k not in anchor for k in need):
            self.store["hh_anchor_lifetime"] = dict(snap)
            self.store["hh_anchor_soc_pct"]  = cur_soc
            return

        def _delta(key):
            if key not in snap or key not in anchor:
                return None
            # max() guards a meter reset only; lifetime counters never go down.
            return round(max(0.0, snap[key] - anchor[key]), 4)

        delta_pv     = _delta("pv")
        delta_import = _delta("gridImport")
        delta_export = _delta("gridExport")
        delta_home   = _delta("home")
        delta_charge = _delta("batteryCharge")
        delta_dischg = _delta("batteryDischarge")
        battery_net  = round((cur_soc - (anchor_soc if anchor_soc is not None else cur_soc))
                             * cap_kwh / 100.0, 4)

        # Tracker price from tariff monitor device state
        tracker_p = None
        try:
            tariff_dev = self._find_device("tariffMonitor")
            if tariff_dev:
                rate_str = tariff_dev.states.get("rateToday", "")
                if rate_str:
                    tracker_p = float(rate_str)
        except Exception:
            pass

        # Agile's published half-hourly rate for this same observed slot. It is
        # gathered even while Tracker is active solely for the shadow tariff
        # baseline; it never feeds the manager unless Agile is actually active.
        agile_p = None
        now_utc = datetime.now(timezone.utc)
        try:
            for start, rate in reversed(self.latest_rates_data.get("shadow_agile_slots", [])):
                if start <= now_utc < start + timedelta(minutes=30):
                    agile_p = float(rate)
                    break
        except (TypeError, ValueError):
            agile_p = None

        action = ""
        if self.latest_decision:
            action = str(self.latest_decision.action)

        con = None
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            con.execute(
                """INSERT OR IGNORE INTO halfhourly
                   (slot_start, slot_end,
                    grid_import_kwh, grid_export_kwh, pv_kwh, home_kwh,
                    battery_soc_start_pct, battery_soc_end_pct, battery_net_kwh,
                    tracker_price_p, agile_price_p, manager_action,
                    battery_charge_kwh, battery_discharge_kwh)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (slot_start, slot_end,
                 delta_import, delta_export, delta_pv, delta_home,
                 anchor_soc, cur_soc, battery_net,
                 tracker_p, agile_p, action,
                 delta_charge, delta_dischg)
            )
            con.commit()
        except sqlite3.Error as exc:
            log(f"[Timeseries] Write failed: {exc}", level="ERROR")
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

        # Advance anchors
        self.store["hh_anchor_lifetime"] = dict(snap)
        self.store["hh_anchor_soc_pct"]  = cur_soc

    def _write_energy_summary_variables(self):
        """
        Write today's running energy totals and battery decision to Indigo variables
        in the Sigenergy folder. Called every 30 min and at midnight.
        Variables created automatically if they do not exist.
        """
        try:
            folder_id = self._sigenergy_folder_id()
            with self._state_lock:   # v5.45.0: snapshot the inputs, write unlocked
                pv     = round(self.store.get("pv_daily_kwh",          0.0), 2)
                imp    = round(self.store.get("grid_import_daily_kwh",  0.0), 2)
                exp    = round(self.store.get("grid_export_daily_kwh",  0.0), 2)
                home   = round(self.store.get("home_daily_kwh",         0.0), 2)
                peak   = round(self.store.get("peak_soc",               0.0), 1)
                minsoc = round(self.store.get("min_soc",              100.0), 1)
            # Self-sufficiency = share of home load NOT met by grid import. On a
            # zero-load day (home=0) nothing was needed from the grid, so that's 100%
            # — matches the dashboard's get_dashboard_data computation (was 0.0 here,
            # an inconsistency between the Indigo variable and the dashboard).
            sself  = (round(max(0.0, (1 - imp / home) * 100), 1)
                      if home > 0 else 100.0)

            decision = self.latest_decision
            action   = str(decision.action)  if decision else ""
            reason   = str(decision.reason)  if decision else ""

            updates = [
                ("sigen_today_pv_kwh",       str(pv)),
                ("sigen_today_import_kwh",   str(imp)),
                ("sigen_today_export_kwh",   str(exp)),
                ("sigen_today_home_kwh",     str(home)),
                ("sigen_today_self_suff_pct", str(sself)),
                ("sigen_today_peak_soc",     str(peak)),
                ("sigen_today_min_soc",      str(minsoc)),
                ("sigen_decision_action",    action),
                ("sigen_decision_reason",    reason),
            ]
            for var_name, value in updates:
                var_id = self._ensure_var(var_name, folder_id)
                if var_id:
                    try:
                        indigo.variable.updateValue(var_id, value)
                    except Exception as exc:
                        log(f"[Energy Vars] Update failed '{var_name}': {exc}",
                            level="WARNING")
            # v5.41: republish the Octopus cost/rate variables from the bill-exact
            # ledger + economics (they had no active writer and went stale).
            self._write_cost_variables(folder_id)
        except Exception as exc:
            log(f"[Energy Vars] _write_energy_summary_variables failed: {exc}",
                level="WARNING")

    def _cost_vars_economics(self):
        """Minimal economics for _write_cost_variables: whole-house today +
        month periods ONLY. get_dashboard_data() builds the entire dashboard
        payload (including three parses of the ever-growing daily_history.json)
        and was being invoked inside the _state_lock-holding tick every ~30
        minutes just for these two blocks (v5.43).
        Rate resolution mirrors get_dashboard_data's three-tier fallback.
        """
        rates   = self.latest_rates_data or {}
        tracker = rates.get("tracker", {})
        export_rate_p = DEFAULT_EXPORT_RATE_P
        try:
            rates_export = float(rates.get("export_rate_p", 0.0))
            if rates_export > 0:
                export_rate_p = rates_export
        except (TypeError, ValueError):
            pass
        import_rate_p = None
        try:
            r = float(tracker.get("today_p") or 0.0)
            if r > 0:
                import_rate_p = r
        except (TypeError, ValueError):
            pass
        if import_rate_p is None:
            tariff_dev = self._find_device("tariffMonitor")
            if tariff_dev:
                try:
                    r = float(tariff_dev.states.get("rateToday", "") or 0.0)
                    if r > 0:
                        import_rate_p = r
                except (TypeError, ValueError):
                    pass
        if import_rate_p is None:
            try:
                if "elec_unit_rate_p" in indigo.variables:
                    r = float(indigo.variables["elec_unit_rate_p"].value or 0.0)
                    if r > 0:
                        import_rate_p = r
            except (TypeError, ValueError, KeyError):
                pass
        # v5.110.0: the tracker bucket is empty on Flux, so price TODAY by band.
        if import_rate_p is None:
            import_rate_p = self._flux_import_band_p() if self._import_tariff_is_banded() else None
        import_rate_p, today_export_rate_p = self._live_rates_today_p(
            import_rate_p, export_rate_p)
        periods = self._period_economics_summary(
            export_rate_p          = export_rate_p,
            fallback_import_rate_p = import_rate_p,
        )
        try:
            whole_house = self._whole_house_summary(
                import_rate_p = import_rate_p,
                export_rate_p = today_export_rate_p,
            )
        except Exception as exc:
            self.logger.debug(f"[WholeHouse] summary failed: {exc}")
            whole_house = None
        return {"whole_house": whole_house, "periods": periods}

    def _write_cost_variables(self, folder_id):
        """v5.41: republish the orphaned Octopus cost/rate variables.

        elec_*/gas_*/export_*/account_balance lost their writer when the old
        Octopus consumption script was retired, so they froze (elec_unit_rate_p
        weeks behind the live Tracker rate, account_balance_gbp stuck at 0).
        weekly_home_digest.py and get_dashboard_data's import-rate fallback both
        read elec_unit_rate_p, so keeping it live here makes this the single
        source of truth. Rates + balance come from get_account_financials (the
        Kraken ledger — already cached, no duplicate fetch); today/month costs
        from the live economics (no recompute → no drift). Fully guarded so a
        Kraken/economics hiccup leaves the values in place rather than blanking
        them, and never disturbs the sigen_* writes that ran before it.
        """
        updates = []
        # ---- bill-exact rates + balance (Kraken ledger, cached ~30 min) ----
        try:
            fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception as exc:
            fin = None
            log(f"[Cost Vars] financials fetch failed: {exc}", level="WARNING")
        if fin:
            elec = dict(fin.get("elec") or {})
            gas  = fin.get("gas") or {}
            exp  = dict(fin.get("export") or {})
            # v5.110.0: a banded tariff's "unit rate" is the band in force NOW.
            # The ledger is cached for half an hour, so read the band at write
            # time rather than trusting the price captured at fetch time.
            if self._import_tariff_is_banded():
                _band = self._flux_import_band_p()
                if _band is not None:
                    elec["unit_p"] = _band
            if self._export_tariff_is_banded():
                _band = self._flux_export_band_p()
                if _band is not None:
                    exp["unit_p"] = _band
            # The three *_tariff_name variables had no writer since the Octopus
            # script was retired, so they still named Tracker and Outgoing.
            for _var, _side in (("elec_tariff_name", elec), ("export_tariff_name", exp),
                                ("gas_tariff_name", gas)):
                if _side.get("display_name"):
                    updates.append((_var, str(_side["display_name"])))
            if elec.get("unit_p") is not None:
                updates.append(("elec_unit_rate_p", f"{float(elec['unit_p']):.4f}"))
            if elec.get("standing_p") is not None:
                updates.append(("elec_standing_charge_p", f"{float(elec['standing_p']):.4f}"))
            if gas.get("unit_p") is not None:
                updates.append(("gas_unit_rate_p", f"{float(gas['unit_p']):.4f}"))
            if gas.get("standing_p") is not None:
                updates.append(("gas_standing_charge_p", f"{float(gas['standing_p']):.4f}"))
            if exp.get("unit_p") is not None:
                rp = f"{float(exp['unit_p']):.4f}"
                updates.append(("export_rate_p", rp))
                updates.append(("export_rate",   rp))   # legacy alias kept for any old consumer (digest now reads export_rate_p)
            if fin.get("balance_gbp") is not None:
                updates.append(("account_balance_gbp", f"{float(fin['balance_gbp']):.2f}"))
        # ---- today/month costs from the live economics (single source, no recompute) ----
        try:
            econ = self._cost_vars_economics() or {}
            whole = econ.get("whole_house") or {}
            wh   = whole.get("today") or {}
            yday = whole.get("yesterday") or {}
            mon  = (econ.get("periods") or {}).get("month") or {}

            def _add(name, val):
                if val is not None:
                    try:
                        updates.append((name, f"{float(val):.2f}"))
                    except (TypeError, ValueError):
                        pass

            def _add_kwh(name, val):
                # 3 dp to match the kWh convention these variables have always
                # used; costs stay at 2. A None means "not known yet" and must
                # leave the variable alone rather than writing a confident 0.
                if val is not None:
                    try:
                        updates.append((name, f"{float(val):.3f}"))
                    except (TypeError, ValueError):
                        pass
            _add("elec_today_cost_gbp",       wh.get("electric_gbp"))
            _add("gas_today_cost_gbp",        wh.get("gas_gbp"))
            _add("export_today_revenue_gbp",  wh.get("export_gbp"))
            _add("combined_today_actual_gbp", wh.get("bill_gbp"))
            # ---- the kWh behind those costs (v5.61.0) --------------------
            # These nine variables have existed since the Octopus-script era
            # and have had NO writer since it was retired (12-Apr-2026), so
            # they sat frozen beside live £ figures — export_today_kwh read
            # 0.000 next to an export_today_revenue_gbp of £2.12. They are
            # published from the SAME card the money comes from, so the pair
            # can never contradict each other again.
            _add_kwh("elec_today_kwh",       wh.get("import_kwh"))
            _add_kwh("gas_today_kwh",        wh.get("gas_kwh"))
            _add_kwh("export_today_kwh",     wh.get("export_kwh"))
            _add_kwh("elec_yesterday_kwh",   yday.get("import_kwh"))
            _add_kwh("gas_yesterday_kwh",    yday.get("gas_kwh"))
            _add_kwh("export_yesterday_kwh", yday.get("export_kwh"))
            # Companion flag: the today card's gas component is an ESTIMATE
            # (most recent settled day's kWh at current rates) until Octopus
            # settles the day — surface that to variable consumers, who can't
            # see the API payload's provisional/gas_estimated flags.
            if wh.get("provisional") is not None:
                updates.append(("combined_today_is_provisional",
                                "true" if wh.get("provisional") else "false"))
            if mon.get("days") == 0:
                # A successful history read with no rows yet this month: the
                # month genuinely starts at zero. (Rows are written at the
                # FOLLOWING midnight, so the whole 1st of each month has none —
                # skipping here kept the closed month's totals showing all day.)
                updates.append(("elec_month_cost_gbp",     "0.00"))
                updates.append(("export_month_revenue_gbp", "0.00"))
                updates.append(("elec_month_kwh",   "0.000"))
                updates.append(("gas_month_kwh",    "0.000"))
                updates.append(("export_month_kwh", "0.000"))
            else:
                # Whole-house basis (unit + standing from settled rows) so the
                # month figure matches elec_today_cost_gbp's basis; falls back
                # to the unit-only aggregate for pre-settle installs.
                _add("elec_month_cost_gbp",
                     mon.get("elec_whole_house_total_gbp")
                     if mon.get("elec_whole_house_total_gbp") is not None
                     else mon.get("import_total_gbp"))
                _add("export_month_revenue_gbp", mon.get("export_total_gbp"))
                _add_kwh("elec_month_kwh",   mon.get("import_kwh"))
                _add_kwh("gas_month_kwh",    mon.get("gas_kwh"))
                _add_kwh("export_month_kwh", mon.get("export_kwh"))
        except Exception as exc:
            log(f"[Cost Vars] economics read failed: {exc}", level="WARNING")
        # ---- write ----
        if not updates:
            return
        for name, value in updates:
            vid = self._ensure_var(name, folder_id)
            if vid:
                try:
                    indigo.variable.updateValue(vid, value)
                except Exception as exc:
                    log(f"[Cost Vars] update '{name}': {exc}", level="WARNING")
        elec_rate = next((v for n, v in updates if n == "elec_unit_rate_p"), "—")
        # Log-on-change only: on Tracker the rate changes once a day, so the
        # ~48 identical heartbeat lines this produced were pure noise. The
        # routine confirmation drops to debug (Indigo's toggle, not log()).
        if elec_rate != self.store.get("cost_vars_last_logged_rate"):
            self.store["cost_vars_last_logged_rate"] = elec_rate
            log(f"[Cost Vars] published {len(updates)} Octopus cost/rate variables "
                f"(elec unit rate {elec_rate}p)")
        else:
            self.logger.debug(
                f"[Cost Vars] published {len(updates)} Octopus cost/rate variables "
                f"(elec unit rate {elec_rate}p)")

    def _update_tariff_device(self, tariff_info, monitored):
        """Push Octopus tariff data to tariffMonitor device."""
        dev = self._find_device("tariffMonitor")
        if not dev:
            return

        active_key = tariff_info.get("tariff_key", TARIFF_TRACKER)
        tracker    = monitored.get("tracker",  {})
        go         = monitored.get("go",       {})
        flux       = monitored.get("flux",     {})
        flexible   = monitored.get("flexible", {})

        # v5.98.2: the THIRD site that read the Tracker bucket whatever the tariff.
        # Caught on the live Agile rehearsal, where this device showed rateToday
        # "None" while the Battery Manager beside it correctly showed 18.543.
        active_rate_today, active_rate_tomorrow = self._rates_for_tariff(
            active_key, monitored)

        states = [
            {"key": "tariffActive",        "value": tariff_info.get("display_name", "")},
            {"key": "rateToday",           "value": self._rate_str(active_rate_today)},
            {"key": "rateTomorrow",        "value": self._rate_str(active_rate_tomorrow)},
            {"key": "trackerRateToday",    "value": self._rate_str(tracker.get("today_p"))},
            {"key": "trackerRateTomorrow", "value": self._rate_str(tracker.get("tomorrow_p"))},
            {"key": "goOffPeakRate",       "value": self._rate_str(go.get("cheap_p"))},
            {"key": "goStandardRate",      "value": self._rate_str(go.get("standard_p"))},
            {"key": "goPeakRate",          "value": self._rate_str(go.get("peak_p"))},
            {"key": "fluxOffPeakRate",     "value": self._rate_str(flux.get("cheap_p"))},
            {"key": "fluxStandardRate",    "value": self._rate_str(flux.get("standard_p"))},
            {"key": "fluxPeakRate",        "value": self._rate_str(flux.get("peak_p"))},
            {"key": "flexibleRate",        "value": self._rate_str(flexible.get("today_p"))},
            {"key": "lastUpdate",          "value": datetime.now().strftime("%H:%M:%S")},
        ]
        dev.updateStatesOnServer(states)

    def _vpp_event_str(self):
        """The announced window as "19:00-20:00", or "" when there is none.

        Consumers append this to a sentence ("VPP event announced: …"), so an
        empty string must read as "nothing to add" rather than "unknown" — the
        callers already guard on falsiness. A window on another day is prefixed
        with its date; today's stays short, because that is the common case and
        the whole point is a glanceable line on a phone.

        Times go through _local_time so they match the device states and the
        log, all of which are Europe/London — the stored event is UTC-aware and
        printing that raw would read an hour early through BST.
        """
        event = self.store.get("vpp_event") or {}
        start = event.get("start_time")
        end   = event.get("end_time")
        if not start or not end:
            return ""
        try:
            window = f"{_local_time(start)}-{_local_time(end)}"
            return window if _local_time(start, "%Y-%m-%d") == _local_today_str() \
                else f"{_local_time(start, '%d/%m')} {window}"
        except Exception:
            # A malformed stored event must not take the whole status payload
            # down — every caller treats "" as "say nothing".
            return ""

    # ================================================================
    # VPP earnings ledger
    # ================================================================
    #
    # What we EXPORTED and what Axle PAID are different numbers, and only the
    # second one is earnings. Axle settles on `flex_kwh` — the change against a
    # baseline — so a window our own snapshots record as 4.23 kWh settles at
    # 3.838. The ledger holds both, side by side, and never averages them into
    # a single figure that means neither.
    #
    # Axle's rows arrive through ONE importer (`importAxleLedger`) reading a
    # drop file. Today that file is written by hand from the account page;
    # a cookie-authenticated fetch or a widened API token would write the same
    # file, and nothing downstream would need to change.

    def _vpp_ledger_path(self):
        return _vpp_ledger.ledger_path(self.data_dir)

    def _vpp_ledger_summary(self):
        """The ledger view, re-read when the file changes.

        Three dashboard pages poll /api/status, so this is on a hot path. The
        cache is keyed on mtime rather than a timer: an import must show up
        immediately, and nothing else writes the file.
        """
        path = self._vpp_ledger_path()
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        cached = getattr(self, "_vpp_ledger_cache", None)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            summary = _vpp_ledger.summarise(_vpp_ledger.load_ledger(path))
        except Exception as exc:
            # A broken ledger must not take the status endpoint down with it,
            # and must not read as "no earnings" either.
            vpp_log(f"[VPP] Ledger summary failed: {exc}", level="WARNING")
            summary = {"load_error": f"{type(exc).__name__}: {exc}"}
        self._vpp_ledger_cache = (mtime, summary)
        return summary

    def _record_vpp_ledger_event(self, event, export_kwh, driver="", log_path="",
                                 window_kwh=None):
        """Add what we observed for one finished event to the ledger.

        Keyed on the window, so re-running the summariser corrects the row
        rather than counting the event twice.
        """
        try:
            start = (event or {}).get("start_time")
            end   = (event or {}).get("end_time")
            if not start:
                return
            path   = self._vpp_ledger_path()
            rate   = _as_float(self.pluginPrefs.get("axleVppRatePerKwh"), 1.00)
            ledger = _vpp_ledger.load_ledger(path)
            _vpp_ledger.record_local_event(ledger, start, end, export_kwh, rate,
                                           driver=driver, log_path=log_path,
                                           window_kwh=window_kwh)
            _vpp_ledger.save_ledger(path, ledger)
            self._vpp_ledger_cache = None
            vpp_log(f"[VPP] Ledger: recorded {round(float(export_kwh), 2)} kWh for "
                f"{_local_time(start, '%d/%m %H:%M')} (our figure — Axle settles later)")
        except Exception as exc:
            vpp_log(f"[VPP] Could not record event in ledger: {exc}", level="WARNING")

    def importAxleLedger(self, valuesDict=None, typeId=None):
        """Menu: merge <data_dir>/vpp_axle_import.json into the ledger.

        A drop file rather than a paste box, because the payload is a few
        kilobytes of JSON and an Indigo textfield is the wrong shape for it.
        The file is the seam: anything that can write it can feed the ledger.

        Take the whole `routes/account/index` loader object from the Axle
        account page, or just its `balance`, `transactions` and `events` keys —
        both are accepted.
        """
        if log_startup_banner:
            log_startup_banner(self.pluginId, self.pluginDisplayName, self.pluginVersion)

        drop = os.path.join(self.data_dir, "vpp_axle_import.json")
        if not os.path.exists(drop):
            vpp_log(f"[VPP] No import file found. Save the Axle account JSON to:\n"
                f"      {drop}\n"
                f"      then run this menu item again.", level="WARNING")
            return

        try:
            with open(drop, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            vpp_log(f"[VPP] Import file could not be read: {exc}", level="ERROR")
            return

        # Accept the whole loader object as well as the trimmed form — pulling
        # three keys out by hand is exactly where a paste goes wrong.
        if "transactions" not in payload and isinstance(payload.get("data"), dict):
            payload = payload["data"]

        try:
            added = self._merge_axle_payload(payload)
        except Exception as exc:
            vpp_log(f"[VPP] Import failed: {exc}", level="ERROR")
            return

        summary = self._vpp_ledger_summary()
        life    = summary.get("lifetime_gbp")
        vpp_log(f"[VPP] Ledger import complete — {added} new transaction(s). "
            f"Lifetime {'£%.2f' % life if life is not None else 'unknown'}, "
            f"{summary.get('events_settled', 0)} settled, "
            f"{summary.get('events_pending', 0)} pending.")

        # Rename rather than delete: if the merge was wrong, the source is
        # still on disk to look at.
        try:
            os.replace(drop, drop + ".imported")
        except OSError:
            pass
        self._update_vpp_device()

    def _vpp_earnings_brief(self):
        """The few ledger figures small enough to ride along with /api/status.

        Deliberately not the event list — that is what /api/vpp is for. Keys
        are None rather than 0 when unknown, and consumers must render them as
        such.
        """
        s = self._vpp_ledger_summary()
        return {
            "lifetime_gbp":      s.get("lifetime_gbp"),
            "available_gbp":     s.get("available_gbp"),
            "month_to_date_gbp": s.get("month_to_date_gbp"),
            "events_gbp":        (s.get("by_kind") or {}).get("events_gbp"),
            "events_pending":    s.get("events_pending"),
            "can_withdraw":      s.get("can_withdraw"),
            "age_days":          s.get("axle_age_days"),
            # Non-None when Axle's stored headline is older than the rows
            # beneath it - see vpp_ledger.summarise.
            "balance_behind_gbp": s.get("balance_behind_gbp"),
        }

    def get_dashboard_vpp(self):
        """The /api/vpp payload: the ledger plus whatever is coming next."""
        summary = dict(self._vpp_ledger_summary())
        summary["next_event"] = self._vpp_next_event_info()
        summary["state"]      = self.store.get("vpp_state", "idle")
        summary["active"]     = self.store.get("vpp_active", False)
        summary["rate_per_kwh"] = _as_float(self.pluginPrefs.get("axleVppRatePerKwh"), 1.00)
        return summary

    def _vpp_next_event_info(self):
        """The announced window in machine-readable form, or None.

        `_vpp_event_str` gives a phrase to append to a sentence; this gives the
        parts, so a page can render a countdown without parsing English. None
        means "nothing announced", which is a real answer and not an error —
        read it alongside `apiStatus`, because a dead feed also has nothing to
        announce.
        """
        event = self.store.get("vpp_event") or {}
        start = event.get("start_time")
        end   = event.get("end_time")
        if not start or not end:
            return None
        try:
            now = datetime.now(timezone.utc)
            duration_hrs = event.get("duration_hrs") or 0.0
            max_export_kw = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)
            rate = _as_float(self.pluginPrefs.get("axleVppRatePerKwh"), 1.00)
            return {
                "start_utc":    start.isoformat() if hasattr(start, "isoformat") else str(start),
                "end_utc":      end.isoformat() if hasattr(end, "isoformat") else str(end),
                "start_local":  _local_time(start, "%d %b %H:%M"),
                "end_local":    _local_time(end, "%H:%M"),
                "duration_hrs": round(duration_hrs, 2),
                "seconds_until_start": int((start - now).total_seconds()),
                "seconds_until_end":   int((end - now).total_seconds()),
                "import_export": event.get("import_export", "export"),
                # An ESTIMATE at the DNO cap, not a promise. Axle's own forecast
                # is used when it sends one.
                "estimate_gbp": round(max_export_kw * duration_hrs * rate, 2),
                "axle_forecast_kwh": event.get("forecast_dispatch_kwh"),
                "axle_estimate_gbp": (round(event["estimated_revenue_p"] / 100.0, 2)
                                      if event.get("estimated_revenue_p") is not None else None),
            }
        except Exception as exc:
            vpp_log(f"[VPP] Could not build next-event info: {exc}", level="WARNING")
            return None

    def _update_vpp_device(self):
        """Push VPP state to axleVppMonitor device."""
        dev = self._find_device("axleVppMonitor")
        if not dev:
            return

        event         = self.store.get("vpp_event") or {}
        start_str     = ""
        end_str       = ""
        duration_hrs  = 0.0

        if event.get("start_time"):
            start_str    = _local_time(event["start_time"], "%H:%M %d/%m")
            end_str      = _local_time(event["end_time"])
            duration_hrs = event.get("duration_hrs", 0.0)

        max_export_kw = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)
        vpp_rate      = _as_float(self.pluginPrefs.get("axleVppRatePerKwh"), 1.00)  # GBP/kWh, configurable (default £1)
        earnings_est  = round(max_export_kw * duration_hrs * vpp_rate, 2)

        # Feed health. Without this the device reads a calm "Standby" whether the
        # API is healthy and quiet or rejecting every call (see _record_vpp_api_status).
        api_error = self.store.get("vpp_api_error")
        last_ok   = self.store.get("vpp_api_last_ok")
        last_ok_s = datetime.fromtimestamp(last_ok).strftime("%H:%M %d/%m") if last_ok else "never"

        states = [
            {"key": "apiStatus",         "value": api_error or "OK"},
            {"key": "apiLastOk",         "value": last_ok_s},
            {"key": "vppStatus",         "value": "Active" if self.store["vpp_active"] else "Standby"},
            {"key": "vppState",          "value": self.store["vpp_state"]},
            {"key": "eventStartTime",    "value": start_str},
            {"key": "eventEndTime",      "value": end_str},
            {"key": "preChargeRequired", "value": str(self.store["vpp_pre_charge_soc"])},
            {"key": "estimatedEarnings", "value": str(earnings_est)},
            {"key": "vppLastExportKwh",  "value": str(round(self.store.get("vpp_last_export_kwh", 0.0), 2))},
            {"key": "lastUpdate",        "value": datetime.now().strftime("%H:%M:%S")},
        ]

        # Settled earnings, from the ledger. These are Axle's numbers, not
        # ours. A figure Axle has not published yet reads "pending" — never
        # 0.00, which on a control page is indistinguishable from "you earned
        # nothing" and would be wrong for several days after every event.
        try:
            summary  = self._vpp_ledger_summary()
            life     = summary.get("lifetime_gbp")
            avail    = summary.get("available_gbp")
            month    = summary.get("month_to_date_gbp")
            by_kind  = summary.get("by_kind") or {}
            pending  = summary.get("events_pending")
            states += [
                {"key": "lifetimeEarnings",  "value": "unknown" if life  is None else f"{life:.2f}"},
                {"key": "availableBalance",  "value": "unknown" if avail is None else f"{avail:.2f}"},
                {"key": "monthToDate",       "value": "pending" if month is None else f"{month:.2f}"},
                {"key": "eventEarningsTotal","value": f"{by_kind.get('events_gbp', 0.0):.2f}"},
                {"key": "eventsSettled",     "value": int(summary.get("events_settled", 0) or 0)},
                {"key": "eventsPending",     "value": int(pending or 0)},
                {"key": "ledgerUpdated",     "value": summary.get("axle_fetched_local") or "never"},
                # The age and the health beside the date. A bare "imported 19/08"
                # reads as calmly as "imported this morning" on a control page,
                # which is how 17 days went unremarked.
                {"key": "ledgerAgeDays",     "value": (int(round(summary["axle_age_days"]))
                                                       if summary.get("axle_age_days") is not None else -1)},
                {"key": "ledgerStatus",      "value": (self.store.get("vpp_ledger_stale")
                                                       or "being fed")},
            ]
        except Exception as exc:
            vpp_log(f"[VPP] Could not add ledger states: {exc}", level="WARNING")

        dev.updateStatesOnServer(states)

    # ================================================================
    # Action dialog pre-population (Indigo getActionConfigUiValues)
    # ================================================================

    def getActionConfigUiValues(self, plugin_props, type_id, dev_id):
        """Pre-fill action dialogs with live values where useful.

        For Force Grid Import: defaults the target SOC to current SOC + 20%
        (clamped to 95%), so the slider opens at a sensible "top-up by ~20%"
        position rather than a static 80%.

        For Force Grid Export: defaults the kW field to the configured DNO
        export limit (4 kW by default) rather than a hardcoded 4.0.

        Any user-saved values in plugin_props take precedence — we only
        write a key when the user hasn't already set it.
        """
        values = indigo.Dict(plugin_props) if plugin_props else indigo.Dict()
        errors = indigo.Dict()
        try:
            inv = self.latest_inverter_data or {}
            soc = float(inv.get("batterySoc", 0.0))
            if type_id == "forceGridImport":
                if not values.get("targetSocPct"):
                    suggested = int(min(95, max(20, round(soc + 20))))
                    values["targetSocPct"] = str(suggested)
                if not values.get("powerKw"):
                    values["powerKw"] = str(
                        _as_float(self.pluginPrefs.get("inverterMaxKw"), "10.0")
                    )
            elif type_id == "forceExport":
                # powerKw is the discharge HEADROOM (grid export is DNO-capped) —
                # default it to the inverter max so the dialog opens at full export.
                if not values.get("powerKw"):
                    values["powerKw"] = str(
                        _as_float(self.pluginPrefs.get("inverterMaxKw"), "10.0")
                    )
        except Exception as exc:
            self.logger.debug(f"getActionConfigUiValues fallback: {exc}")
        return (values, errors)

    # ================================================================
    # Octopus Flux supervisor (DRAFT — off by default)
    # ================================================================
    #
    #   flux_strategy.plan()  decides (pure, stdlib, no clock of its own)
    #   this section          builds its inputs from live state, decides WHO owns
    #                         the inverter, leases a decision to the executor
    #   flux_execution        holds the durable claim, writes and verifies
    #
    # TWO RULES ABOVE ALL OTHERS:
    #
    #   1. While Flux owns the inverter, the existing manager's verify and act
    #      passes do not run. Two owners writing the same four registers a minute
    #      apart is an argument, not a supervisor.
    #   2. The hardware discharge floor has ONE owner across the whole day —
    #      _policy_discharge_floor_pct. Flux releasing the inverter must not mean
    #      releasing the reserve, and every writer of register 40048 reads that
    #      one function.

    def _flux_enabled(self):
        """True only when both switches are on and both modules loaded.

        `fluxEnabled` is "I want this"; `fluxCommissioned` is "the behaviour in
        the activation checklist has been proven on this inverter". A feature
        switch alone would let an untested hand-back reach a live battery.
        """
        if not FLUX_AVAILABLE:
            return False
        return (_as_bool(self.pluginPrefs.get("fluxEnabled", False))
                and _as_bool(self.pluginPrefs.get("fluxCommissioned", False)))

    def _flux_peak_now(self):
        """True while the Flux strategy is armed and the 16:00-19:00 peak is open."""
        if not self._flux_armed():
            return False
        try:
            return bool(_flux_strategy.in_window(
                datetime.now(timezone.utc), _london_tz(),
                _flux_strategy.FLUX_PEAK_START, _flux_strategy.FLUX_PEAK_END))
        except Exception:                                    # noqa: BLE001
            return False

    def _flux_armed(self):
        """True when the strategy is on, whether or not it holds the inverter.

        The reserve floor applies all day once armed — including the hours Flux
        deliberately hands back — so this, not _flux_enabled, is what the policy
        floor asks.
        """
        return self._flux_enabled()

    def _flux_journal_path(self):
        return os.path.join(self.data_dir, "flux_claim.json")

    # ---- the one owner of the hardware discharge floor --------------------

    def _policy_discharge_floor_pct(self, dispatch_event=None):
        """The lowest SOC policy lets a GRID-TIED discharge reach, whoever is driving.

        Since 17-Sep-2026 this is written to the BACKUP RESERVE (40046), not the
        absolute cutoff (40048): see _absolute_cutoff_pct. Every writer reads
        this: the manager's verify pass, the export teardown, the flood-prevention
        reset, the VPP floor and restore, the pause/sleep disengage, and the Flux
        release baseline. Before this
        existed each of them wrote `batteryHealthCutoff` directly, so the moment
        Flux handed back, the verify pass pulled the hardware floor to 1% and the
        agreed 20% reserve lasted less than a minute.

        Highest of: the health floor, a live flood-prevention target, and — only
        while the Flux strategy is armed — the Flux reserve plus whatever energy
        is already promised to a grid event. Storm is NOT here: it raises a
        software dawn target, never this register, and a storm floor written to
        hardware is one nothing would ever lower.
        """
        health = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)

        # THE ONE CASE WHERE THIS FLOOR GOES DOWN RATHER THAN UP.
        #
        # Register 40048 is the GLOBAL discharge cutoff: the inverter honours it
        # off-grid as well as on, so a 20% floor written for economic reasons
        # locks a fifth of the pack away during the very power cut the reserve
        # exists for. The house would go dark with 7 kWh in the battery.
        #
        # Backup reserve is 40046's job — a separate register, set to 20% here —
        # and that one is about WITHHOLDING energy while the grid is up. This one
        # is an absolute limit, so during a verified outage it drops to the health
        # floor and the emergency is allowed to spend the reserve. Nothing
        # economic may raise it back: neither a flood-prevention pre-drain target
        # nor the Flux reserve is worth anything in a blackout.
        if self._grid_outage_active():
            return round(max(0.0, min(100.0, health)), 1)

        floor = health
        flood = self.store.get("flood_prev_target_soc")
        if flood:
            floor = max(floor, float(flood))
        if self._flux_armed():
            floor = max(floor, self._flux_reserve_floor_pct(dispatch_event=dispatch_event))
        return round(max(0.0, min(100.0, floor)), 1)

    def _absolute_cutoff_pct(self):
        """What register 40048 holds: the health floor, or a flood pre-drain target.

        40048 applies OFF-grid as well as on, and nothing can release it while the
        host is out of touch with the inverter. So no economic reserve lives here
        any more — the Flux reserve and event commitments go on the backup reserve
        (40046), which stops grid-tied discharge just the same but steps aside in
        a power cut. VPP's own dispatch floor is set separately by the VPP code.
        """
        health = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)
        if self._grid_outage_active():
            return round(max(0.0, min(100.0, health)), 1)
        floor = health
        flood = self.store.get("flood_prev_target_soc")
        if flood:
            floor = max(floor, float(flood))
        return round(max(0.0, min(100.0, floor)), 1)

    def _owns_backup_reserve(self):
        """The plugin writes 40046 only while the Flux strategy is armed.

        An install that never arms Flux keeps whatever backup reserve its
        installer set in the app. During a verified outage the register does not
        apply, so it is left alone and is still correct when the grid returns.
        """
        return self._flux_armed() and not self._grid_outage_active()

    def _write_policy_floors(self, dispatch_event=None):
        """Write both floors: 40048 = absolute cutoff, 40046 = policy reserve.

        Returns the 40048 value written, for the caller's log line.
        """
        cutoff = self._absolute_cutoff_pct()
        self.modbus.set_discharge_cutoff(cutoff)
        if self._owns_backup_reserve():
            self.modbus.set_backup_soc(
                self._policy_discharge_floor_pct(dispatch_event=dispatch_event))
        return cutoff

    def _grid_outage_active(self):
        """True only during a FRESH, VERIFIED grid outage. Two sources must agree.

        `power_cut_started_at` is set by _apply_modbus_result on a genuine
        `Off-grid` status from register 30009 — deliberately not on "any value
        that is not On-grid", so an unmapped `Unknown (N)` read cannot fire it.
        That flag alone is not enough here: it survives a poll loop that has died,
        and this answer lowers a safety floor. So the LIVE reading must still say
        off-grid, and it must be recent enough to be a reading rather than a
        memory.
        """
        if self.store.get("power_cut_started_at") is None:
            return False
        data = self.latest_inverter_data or {}
        if not str(data.get("gridStatus", "")).startswith("Off-grid"):
            return False
        read_at = float(data.get("_read_at") or 0.0)
        if not read_at:
            return False
        poll_s = getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL)
        return (time.time() - read_at) <= (3 * poll_s + 60)

    def _flux_reserve_floor_pct(self, dispatch_event=None):
        """Flux's own floor: the flat reserve plus committed event energy.

        Committed energy is included because a promised kWh is not spare — but
        household demand is NOT, because the house must be able to eat its own
        evening: see flux_strategy.household_floor_pct.
        """
        reserve = _as_float(self.pluginPrefs.get("fluxReservePct"),
                            _flux_strategy.DEFAULT_RESERVE_PCT)
        capacity = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), 35.04)
        eff      = max(0.01, _as_float(self.pluginPrefs.get("batteryEfficiency"), 94.0) / 100.0)
        try:
            now   = datetime.now(timezone.utc)
            until = _flux_strategy.next_cheap_start(now, _london_tz())
            # THE DISPATCH BEING SERVED IS NOT RESERVED AGAINST ITSELF. The VPP
            # code sets this register once, at PRE_CHARGING — thirty minutes
            # before the window — and does not touch it again. So a floor that
            # includes the event's own allocation at that moment blocks the whole
            # dispatch, and nothing later corrects it. Excluded in both phases;
            # every OTHER commitment still counts, because those really are
            # energy that must survive this window.
            dispatched = self._flux_dispatch_event_ids()
            # Pre-charge writes the cutoff BEFORE changing ANNOUNCED to
            # PRE_CHARGING. The caller explicitly identifies that dispatch.
            if dispatch_event is not None:
                dispatched.add(str(dispatch_event.get("id") or "axle"))
            commitments = tuple(c for c in self._flux_commitments() if c.kind == "export")
            serving = tuple(c for c in commitments if c.event_id in dispatched)
            # Union(all) minus union(serving) leaves only energy not supplied by
            # this dispatch, including a later tail of an overlapping session.
            committed = max(0.0,
                _flux_strategy.commitment_energy_kwh(commitments, now, until)
                - _flux_strategy.commitment_energy_kwh(serving, now, until))
        except Exception as exc:                        # noqa: BLE001
            self.logger.debug(f"[Flux] commitment floor unavailable: {exc!r}")
            committed = 0.0
        if committed > 0 and capacity > 0:
            reserve += (committed / (eff ** 0.5)) / capacity * 100.0
        return round(min(100.0, max(0.0, reserve)), 1)

    def _flux_dispatch_event_ids(self):
        """Event ids whose energy is being spent RIGHT NOW by their own owner.

        An owner driving a window manages that window's energy itself, including
        the floor it sets for it. Reserving the same kWh again on top is how a
        reservation comes to block the dispatch it was made for.

        Pre-charge counts: it is the VPP code filling the battery for exactly
        this event, and it is also the only moment the VPP writes the discharge
        cutoff.
        """
        ids = set()
        if self.store.get("vpp_state", VPP_IDLE) in (VPP_PRE_CHARGING, VPP_ACTIVE):
            event = self.store.get("vpp_event") or {}
            ids.add(str(event.get("id") or "axle"))
        if self.store.get("saving_session_export_active"):
            window = self._saving_session_window() or {}
            ids.add(str(window.get("id") or "session"))
        return ids

    def _flux_planner_floor_pct(self):
        """The floor the PLANNER may rely on. Adds storm and power-cut policy.

        Storm belongs here and not in the hardware floor: holding more back than
        needed is always safe in a plan, and a storm stands Flux down anyway.
        """
        floor = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)
        flood = self.store.get("flood_prev_target_soc")
        if flood:
            floor = max(floor, float(flood))
        storm_level = str(self.store.get("storm_level", "none") or "none")
        if storm_level in ("amber", "red"):
            floor = max(floor, STORM_SOC_AMBER)
        elif storm_level == "yellow":
            floor = max(floor, STORM_SOC_YELLOW)
        if self._power_cut_window_active():
            floor = max(floor, POWER_CUT_LOCKOUT_MIN_SOC_PCT)
        return round(min(100.0, max(0.0, floor)), 1)

    def _flux_baseline(self):
        """What the executor restores on release.

        The discharge floor is the policy floor, so a release does not drop the
        reserve. The charge ceiling follows any import in flight, so a hand-back
        mid-import does not cap it short of its target.
        """
        inv_max_w = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
        ceiling   = self.store.get("import_charge_cutoff_pct") or 100.0
        return {
            "baseline_charge_w":             int(inv_max_w),
            "baseline_discharge_w":          int(inv_max_w),
            "baseline_charge_cutoff_pct":    round(min(100.0, float(ceiling)), 1),
            "baseline_discharge_cutoff_pct": self._policy_discharge_floor_pct(),
        }

    # ---- executor lifecycle ----------------------------------------------

    def _flux_recovery_pending(self):
        """True when a durable claim may still be on the inverter.

        Either this process holds one, or a journal from a previous run is on
        disk. Deliberately cheap and side-effect free: it is read on the startup
        path, before anything has been reconciled, and it must not itself write
        or build anything. A missing journal and no executor is the ordinary
        case for every install that has never armed Flux, and answers False.
        """
        ex = getattr(self, "flux_executor", None)
        if ex is not None and ex.owns_control:
            return True
        if not FLUX_AVAILABLE:
            return False
        try:
            return os.path.exists(self._flux_journal_path())
        except Exception:                              # noqa: BLE001
            return False

    def _flux_rebind_driver(self):
        """Point an existing executor at the CURRENT Modbus driver.

        `_init_modules` replaces `self.modbus` on every preference save, so an
        executor built earlier keeps an adapter wrapping the disconnected old
        driver. The claim is durable and survives the swap; the socket does not.
        Re-binding preserves the claim and the obligation to reconcile it, which
        rebuilding the executor would not — a new one starts from the journal
        and would have to re-derive what this one already knows.
        """
        ex = getattr(self, "flux_executor", None)
        if ex is None or self.modbus is None or not FLUX_AVAILABLE:
            return
        try:
            adapter = _FluxRawDriver(self.modbus, self.pluginPrefs)
            # Prove the adapter answers before handing it over. The constructor
            # only stores two references, so a broken one fails at the first
            # WRITE otherwise — which is the worst moment to find out.
            int(adapter.inverter_max_w)
            ex.rebind(adapter)
            log("[Flux] The inverter connection was rebuilt; the outstanding claim "
                "has been re-bound to the new one and still needs reconciling.")
        except Exception as exc:                       # noqa: BLE001
            log(f"[Flux] Could not re-bind the claim to the new inverter "
                f"connection ({exc!r}) — dropping the executor so the next tick "
                f"rebuilds it from the journal.", level="WARNING")
            self.flux_executor = None

    def _flux_ensure_executor(self, allow_create=True):
        """The executor, built lazily, with its baseline kept current.

        `allow_create=False` is used by the disabled path: it will adopt an
        executor that already exists but will not build one for trading.
        """
        if self.modbus is None or not FLUX_AVAILABLE:
            return None
        if getattr(self, "flux_executor", None) is None:
            if not allow_create:
                return None
            try:
                self.flux_executor = _FluxExecutor(
                    _FluxRawDriver(self.modbus, self.pluginPrefs),
                    self._flux_journal_path(),
                    **self._flux_baseline())
                log("[Flux] Supervisor armed. It takes control only inside the Flux "
                    "windows, and only when every input it needs is present and fresh.")
            except Exception as exc:
                log(f"[Flux] Supervisor could not be armed: {exc!r}. The strategy "
                    f"stays off and the existing manager keeps the inverter.",
                    level="ERROR")
                self.flux_executor = None
                return None
        else:
            try:
                self.flux_executor.configure_baseline(**self._flux_baseline())
            except Exception as exc:
                self.logger.debug(f"[Flux] baseline refresh refused: {exc!r}")
        return self.flux_executor

    def _flux_owns_control(self):
        """True while the executor holds the claim. Asked of the executor, never
        of a local flag — it is the thing that knows whether a write landed."""
        ex = getattr(self, "flux_executor", None)
        return bool(ex is not None and ex.owns_control)

    # ---- priority ---------------------------------------------------------

    def _flux_other_owner(self):
        """Name the owner that outranks Flux, or "". All of them write the same
        four registers, and all of them are older and worth more than a trade."""
        if self._grid_outage_active():
            # First, and above everything. The supervisor tick runs before the
            # manager in _tick, so releasing here is what puts the claim down
            # BEFORE the manager's verify pass writes the lowered floor.
            return "the grid is down and the house is running on the battery"
        if self.store.get("manager_paused", False):
            return "the manager is paused"
        if self.store.get("flux_manual_preempt"):
            return str(self.store["flux_manual_preempt"])
        # ANNOUNCED is not an owner. An announced window is a commitment — the
        # planner reserves its energy and the cheap window is where that energy
        # gets bought. Standing Flux down at announcement would block the very
        # charge that funds the dispatch. Ownership starts when the VPP code
        # begins writing, which is pre-charge.
        _vpp = self.store.get("vpp_state", VPP_IDLE)
        if _vpp not in (VPP_IDLE, VPP_ANNOUNCED):
            return f"an Axle VPP window ({_vpp})"
        if self.store.get("saving_session_export_active"):
            return "an Octopus Saving Session"
        if self.store.get("happy_hour_import_active"):
            return "a Happy Hour free import"
        if self.store.get("import_active"):
            return "the manager has a grid import in flight"
        if self.store.get("export_active"):
            return "the manager has an export in flight"
        if self.store.get("solar_overflow_active") and not self._flux_peak_now():
            # NOT an owner inside the peak window (v5.109.5, found live 17-Sep-2026:
            # an overflow export started at 14:24 held Flux off for the whole
            # 16:00 peak, silently, with the battery at 96%). The Flux export mode
            # sends PV first anyway, so standing overflow down in the peak loses
            # nothing and lets the battery's spare energy sell at the peak price.
            return "solar overflow is running"
        if self.store.get("flood_prev_target_soc"):
            return "flood prevention is holding a discharge floor"
        if self._power_cut_window_active():
            return "a power-cut lockout is in force"
        if str(self.store.get("storm_level", "") or "").lower() not in ("", "none", "clear"):
            return f"a storm warning ({self.store.get('storm_level')})"
        return ""

    # ---- event commitments ------------------------------------------------

    def _flux_commitments(self):
        """Energy already promised to somebody else, as EventCommitments.

        An owner flag says "not now"; a commitment says "this many kWh, between
        these times". Flux needs both — a window announced for 18:00-19:00 has to
        be reserved from the moment it is announced, not when it starts, or the
        peak-window export sells the very kWh that window was going to need.

        Two sources, both read from caches the polls leave behind (no network on
        this path):
          * Axle — the VPP state machine's own event, while it is announced,
            pre-charging or active. Sized the way _compute_vpp_reserved_kwh sizes
            it: the DNO cap for the duration, because that is what the driver
            exports;
          * Octopus — joined Saving Sessions of the TURN_DOWN direction, which are
            the only ones earned by exporting, plus Happy Hour, which is an
            IMPORT commitment (free power, so leave room for it rather than
            energy).

        Event rewards are carried but never mixed with tariff prices: Axle pays
        for dispatch and Octopus pays points, and neither is the Flux export
        rate. Physical export is counted once; ordinary tariff revenue and a
        separately earned event payment are separate ledger entries.
        """
        if _flux_strategy is None:
            return ()
        out = []
        now = datetime.now(timezone.utc)
        export_kw = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)

        vpp_state = self.store.get("vpp_state", VPP_IDLE)
        event     = self.store.get("vpp_event") or {}
        if vpp_state in (VPP_ANNOUNCED, VPP_PRE_CHARGING, VPP_ACTIVE) and event:
            start = event.get("start_time")
            end   = event.get("end_time")
            direction = str(event.get("import_export", "export") or "export").lower()
            if start and end and end > now and direction == "export":
                hours = max(0.0, (end - start).total_seconds() / 3600.0)
                out.append(_flux_strategy.EventCommitment(
                    source="axle", kind="export", start=start, end=end,
                    energy_kwh=round(export_kw * hours, 3),
                    announced_at=self.store.get("vpp_announced_at"),
                    reward_p_per_kwh=None,
                    event_id=str(event.get("id") or "axle")))

        for window in (self.store.get("saving_sessions_windows") or []):
            try:
                start = datetime.fromisoformat(str(window["start"]))
                end   = datetime.fromisoformat(str(window["end"]))
            except (KeyError, TypeError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if end <= now:
                continue
            hours = max(0.0, (end - start).total_seconds() / 3600.0)
            direction = window.get("direction")
            if direction == SAVING_SESSION_TURN_DOWN:
                out.append(_flux_strategy.EventCommitment(
                    source="octopus", kind="export", start=start, end=end,
                    energy_kwh=round(export_kw * hours, 3),
                    reward_p_per_kwh=None,
                    event_id=str(window.get("id") or "session")))
            elif direction == SAVING_SESSION_HAPPY_HOUR:
                # Free import: what it needs is EMPTY battery, not full.
                inv_kw = _as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0)
                out.append(_flux_strategy.EventCommitment(
                    source="octopus", kind="import", start=start, end=end,
                    energy_kwh=round(inv_kw * hours, 3),
                    event_id=str(window.get("id") or "happyhour")))
        return tuple(out)

    # ---- tariff ------------------------------------------------------------

    def _flux_tariff_verified(self):
        """(ok, why_not). Proof, from the account, fetched recently. Fails closed.

        Flux is a PAIRED tariff — the cheap overnight import is paid for by the
        peak export price — so an import agreement alone proves half of it.

        THE EVIDENCE MUST BE FRESH IN ITS OWN RIGHT. Both agreements come from a
        single account fetch carrying its own timestamp, because the earlier
        version could be kept alive indefinitely by something else succeeding:
        the rate refresh held the last good agreement when the account lookup
        returned nothing, and then stamped the PUBLIC prices as fresh. A house
        that had left Flux would have gone on trading it, on evidence nothing had
        re-checked since the day it was gathered.

        `get_current_tariff` is deliberately not consulted: it falls back to the
        last agreement in the list when none is current, which is right for a
        display and is not proof. A tariff override proves nothing either way.
        """
        evidence = self.store.get("flux_account_evidence") or {}
        if not evidence:
            return False, ("the account's agreements have not been read, so neither "
                           "side of the paired Flux tariff is proven")
        fetched_at = evidence.get("fetched_at")
        age = (time.time() - fetched_at) if (type(fetched_at) in (int, float)
                and math.isfinite(fetched_at) and fetched_at > 0) else None
        if age is None or age < 0 or age > FLUX_ACCOUNT_EVIDENCE_MAX_AGE_S:
            return False, ("the account's agreements have not been re-read recently "
                           "enough to prove what the house is on today")
        for side in ("import", "export"):
            valid_to = evidence.get(f"{side}_valid_to")
            if valid_to:
                try:
                    end = datetime.fromisoformat(str(valid_to).replace("Z", "+00:00"))
                    if end.tzinfo is None or end.timestamp() <= time.time():
                        return False, f"the account's {side} agreement has expired"
                except (TypeError, ValueError):
                    return False, f"the account's {side} agreement expiry is unreadable"
        imp = evidence.get("import") or {}
        exp = evidence.get("export") or {}
        if str(imp.get("tariff_key") or "") != TARIFF_FLUX:
            return False, (f"the account's import agreement is "
                           f"'{imp.get('tariff_key') or 'unreadable'}', not Flux")
        if str(exp.get("tariff_key") or "") != TARIFF_FLUX:
            return False, (f"the account's export agreement is "
                           f"'{exp.get('tariff_key') or 'unreadable'}', not the paired "
                           f"Flux export tariff")
        return True, ""

    # ---- export pricing (paired Flux is banded, not a flat 12p) -----------

    def _flux_import_band_p(self, when_utc=None):
        """The published Flux IMPORT price for a given moment, or None."""
        return self._flux_band_p("flux_import_slots", when_utc)

    def _flux_band_p(self, slots_key, when_utc=None):
        """The published price on one side of the pair for a moment, or None."""
        spans = self._flux_rate_spans(slots_key)
        when = when_utc or datetime.now(timezone.utc)
        for span in spans:
            if span.start <= when < span.end:
                return round(float(span.p), 4)
        return None

    # Tier names by rank, cheapest first, for a day with that many distinct prices.
    _TIER_LABELS = {1: ("Standard",), 2: ("Off-peak", "Standard"),
                    3: ("Off-peak", "Day", "Peak")}

    def _band_tiers(self, side, date_str=None, now_utc=None):
        """One side's distinct prices for a local day, cheapest first, with the
        local windows each runs in. [] unless the whole day is published.

        [{"label": "Peak", "p": 34.0999, "windows": [{"start": "16:00",
          "end": "19:00", "current": False}], "current": False}, ...]

        A band running over midnight is shown as one window ("19:00" to "02:00")
        rather than two halves of the same band at either end of the page.
        """
        if not self._tariff_is_banded(side):
            return []
        date_str = date_str or _local_today_str()
        bounds = self._day_bounds_utc(date_str)
        spans  = self._flux_rate_spans(self._BAND_SIDES[side][0])
        if bounds is None or not spans:
            return []
        a, b = bounds
        pieces = sorted((max(s.start, a), min(s.end, b), round(float(s.p), 4))
                        for s in spans if s.end > a and s.start < b)
        # Must tile the day exactly: a gap would show an invented window.
        cursor = a
        for start, end, _ in pieces:
            if start > cursor:
                return []
            cursor = max(cursor, end)
        if cursor < b:
            return []
        merged = []
        for start, end, p in pieces:
            if merged and merged[-1][2] == p and merged[-1][1] >= start:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end), p)
            else:
                merged.append((start, end, p))
        now = now_utc or datetime.now(timezone.utc)
        windows = [{"start": s, "end": e, "p": p, "current": s <= now < e}
                   for s, e, p in merged]
        if len(windows) > 1 and windows[0]["p"] == windows[-1]["p"]:
            first = windows.pop(0)
            windows[-1] = {"start": windows[-1]["start"], "end": first["end"],
                           "p": first["p"],
                           "current": windows[-1]["current"] or first["current"]}
        prices = sorted({w["p"] for w in windows})
        labels = self._TIER_LABELS.get(len(prices)) or tuple(
            f"Band {i + 1}" for i in range(len(prices)))
        tiers = []
        for price, label in zip(prices, labels):
            own = [w for w in windows if w["p"] == price]
            own.sort(key=lambda w: _to_london(w["start"]).strftime("%H:%M"))
            tiers.append({
                "label":   label,
                "p":       price,
                "current": any(w["current"] for w in own),
                "windows": [{"start":   _to_london(w["start"]).strftime("%H:%M"),
                             "end":     _to_london(w["end"]).strftime("%H:%M"),
                             "current": w["current"]} for w in own],
            })
        return tiers

    def _tariff_sides_payload(self, export_rate_now_p):
        """The import and export tariffs, each with its bands, for the dashboards.

        Names come from the account's own agreements (the Kraken ledger), so the
        page says "Octopus Flux Import", not a product code, and never a tariff
        the house has left.
        """
        try:
            fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception:                               # noqa: BLE001
            fin = None
        fin = fin or {}
        evidence = self.store.get("flux_account_evidence") or {}
        out = {}
        for side, fin_key, now_fn in (("import", "elec", self._flux_import_band_p),
                                      ("export", "export", self._flux_export_band_p)):
            ledger = fin.get(fin_key) or {}
            agreed = evidence.get(side) or {}
            tiers  = self._band_tiers(side)
            now_p  = now_fn() if tiers else None
            if side == "export" and not tiers:
                now_p = round(float(export_rate_now_p), 4) if export_rate_now_p else None
            out[f"{side}_side"] = {
                "name":         ledger.get("display_name") or "",
                "tariff_code":  ledger.get("tariff_code") or agreed.get("tariff_code") or "",
                "banded":       bool(tiers),
                "now_p":        now_p,
                "tiers":        tiers,
                "valid_from":   evidence.get(f"{side}_valid_from") or "",
            }
        return out

    def _live_rates_today_p(self, import_fallback_p, export_fallback_p):
        """(import p, export p) to value TODAY's kWh so far.

        On a banded side that is today's own kWh weighted by band, not the band
        in force at the moment somebody looks. A flat side returns its fallback
        untouched.
        """
        today = _local_today_str()
        imp, _ = self._import_rate_and_basis(today, import_fallback_p)
        exp, _ = self._export_rate_and_basis(today, export_fallback_p)
        return imp, exp

    def _flux_export_band_p(self, when_utc=None):
        """The published Flux EXPORT price for a given moment, or None.

        Read from the same account-verified schedule the planner trades on, so
        the price reported to the dashboards and written into the daily history
        is the price the account is actually paid.
        """
        if _flux_strategy is None:
            return None
        spans = self._flux_rate_spans("flux_export_slots")
        if not spans:
            return None
        when = when_utc or datetime.now(timezone.utc)
        for span in spans:
            if span.start <= when < span.end:
                return round(float(span.p), 4)
        return None

    # ---- banded (time-of-use) pricing, both sides -----------------------
    # Paired Flux bills BOTH directions by band, so a day's import is only priced
    # right when each kWh is valued at the band it was bought in. Until v5.110.0
    # only the export side was weighted; import went on being valued at whatever
    # band happened to be in force when the page was loaded, so the same morning's
    # import cost 14.6p at 03:00, 24.4p at noon and 34.1p during the peak.
    _BAND_SIDES = {
        "import": ("flux_import_slots", "grid_import_kwh", "import_valid_from"),
        "export": ("flux_export_slots", "grid_export_kwh", "export_valid_from"),
    }

    def _tariff_is_banded(self, side):
        """True when the account's agreement on `side` charges more than one price."""
        evidence = self.store.get("flux_account_evidence") or {}
        key = str((evidence.get(side) or {}).get("tariff_key") or "")
        return key in (TARIFF_FLUX, "iflux")

    def _import_tariff_is_banded(self):
        """True when the account's import agreement charges more than one price."""
        return self._tariff_is_banded("import")

    def _export_tariff_is_banded(self):
        """True when the account's export agreement pays more than one price.

        Flux and Intelligent Flux do. Outgoing at a flat 12p does not, which is
        why a single number was right for three years and stopped being right
        the day the export agreement changed.
        """
        return self._tariff_is_banded("export")

    def _export_rate_now_p(self):
        """The export price in force right now. ONE owner for that question.

        Order: the account's banded export schedule, then whatever the rate feed
        published, then the flat Outgoing default. The default is the last
        resort and no longer the silent answer — `export_rate_p` was never
        populated anywhere, so every consumer had been quoting 12p regardless of
        what the account was on.
        """
        if self._export_tariff_is_banded():
            band = self._flux_export_band_p()
            if band is not None:
                return band
        try:
            published = float((self.latest_rates_data or {}).get("export_rate_p", 0.0))
            if published > 0:
                return published
        except (TypeError, ValueError):
            pass
        return DEFAULT_EXPORT_RATE_P

    def _import_rate_and_basis(self, date_str, fallback_p):
        """(rate, basis) for one day's imports. Mirror of _export_rate_and_basis."""
        return self._banded_rate_and_basis("import", date_str, fallback_p)

    def _export_rate_and_basis(self, date_str, fallback_p):
        """(rate, basis) for one day's exports. ONE owner for that decision.

        `basis` is "weighted" when the day's own half-hourly exports were priced
        against the published bands, "flat" when a single price genuinely applied
        all day, and **"estimated"** when the tariff IS banded but the weighting
        could not be done — no series, a gap in the bands, or a day before the
        agreement began. That third label matters: calling an unweighted figure
        "flat" on a banded tariff reads as exact, and it is a guess.
        """
        return self._banded_rate_and_basis("export", date_str, fallback_p)

    def _banded_rate_and_basis(self, side, date_str, fallback_p):
        """(rate, basis) for one side of one day.

        Beyond the three labels above, a banded day on which nothing flowed in
        that direction is "time-average": the day's bands averaged by the hours
        each ran. The kWh are zero so the money is unaffected, but a history row
        with no rate at all reads to every consumer as "rate missing".
        """
        try:
            weighted, reason = self._banded_rate_for_day(side, date_str)
        except Exception as exc:                        # noqa: BLE001
            self.logger.debug(f"[Economics] {side} weighting skipped: {exc!r}")
            weighted, reason = None, "error"
        if weighted is not None:
            return weighted, "weighted"
        if not self._tariff_is_banded(side):
            return fallback_p, "flat"
        if reason == "nothing flowed":
            mean = self._band_day_mean_p(side, date_str)
            if mean is not None:
                return mean, "time-average"
        return fallback_p, "estimated"

    def _agreement_started(self, side):
        """When the account's agreement on `side` began, as an aware UTC instant.

        None when unknown — and unknown is not "always". A day before the
        agreement started was billed on the previous tariff.
        """
        raw = str((self.store.get("flux_account_evidence") or {}).get(
            self._BAND_SIDES[side][2]) or "")
        if not raw:
            return None
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if started.tzinfo is None:
            return None
        return started.astimezone(timezone.utc)

    def _export_agreement_started(self):
        """When the account's export agreement began, or None."""
        return self._agreement_started("export")

    def _day_bounds_utc(self, date_str):
        """[start, end) of a local day as aware UTC instants, or None."""
        try:
            day = datetime.strptime(str(date_str), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None
        a = _london_localise(datetime(day.year, day.month, day.day))
        nxt = day + timedelta(days=1)
        b = _london_localise(datetime(nxt.year, nxt.month, nxt.day))
        if a is None or b is None:
            return None
        return a.astimezone(timezone.utc), b.astimezone(timezone.utc)

    def _day_is_before_agreement(self, side, date_str):
        """True when the day began before the banded agreement did."""
        started = self._agreement_started(side)
        bounds  = self._day_bounds_utc(date_str)
        return bool(started is not None and bounds is not None and bounds[0] < started)

    def _band_day_mean_p(self, side, date_str):
        """The day's bands averaged by how long each ran, or None if not covered."""
        if not self._tariff_is_banded(side) or self._day_is_before_agreement(side, date_str):
            return None
        spans  = self._flux_rate_spans(self._BAND_SIDES[side][0])
        bounds = self._day_bounds_utc(date_str)
        if not spans or bounds is None:
            return None
        priced = self._export_price_over(bounds[0], bounds[1], spans)
        return round(priced, 4) if priced is not None else None

    def _export_rate_for_day_p(self, date_str):
        """A day's exports valued at the bands they were sold in, or None."""
        return self._banded_rate_for_day("export", date_str)[0]

    def _import_rate_for_day_p(self, date_str):
        """A day's imports valued at the bands they were bought in, or None."""
        return self._banded_rate_for_day("import", date_str)[0]

    def _banded_rate_for_day(self, side, date_str):
        """(weighted pence, reason). The pence are None whenever the answer would
        not be a measurement, and `reason` says which refusal it was.

        On a banded tariff one daily number is only meaningful weighted by WHEN
        the kWh moved: the same day's export is worth roughly six times as much
        at 17:00 as at 03:00, and import costs more than twice as much.

        REFUSES, rather than approximating, whenever:
          * the tariff on that side is not banded — the caller's flat rate is exact;
          * the day started before the agreement did. The switch to Flux is a
            date: 16 September was billed Tracker and Outgoing and must stay so;
          * any slot that moved energy is not FULLY covered by published bands.
            Averaging the covered subset and calling it weighted is the failure
            this guard exists for;
          * nothing moved in that direction ("nothing flowed").

        Each slot is apportioned across every band it overlaps rather than priced
        at the band its start falls in. The rows are written by a 30-minute tick,
        not on the half hour, so a slot beginning at 15:58 straddles the 16:00
        peak boundary and the two parts are worth very different money.

        `slot_start`/`slot_end` are naive LOCAL wall time (`datetime.now()` in
        _log_halfhourly_to_db_impl), so they are localised, not stamped as UTC.
        """
        if not self._tariff_is_banded(side):
            return None, "not banded"
        slots_key, column, _ = self._BAND_SIDES[side]
        spans = self._flux_rate_spans(slots_key)
        if not spans:
            return None, "no bands"
        if self._day_bounds_utc(date_str) is None:
            return None, "bad date"
        if self._day_is_before_agreement(side, date_str):
            return None, "before agreement"
        db_path = os.path.join(self.data_dir, "energy_timeseries.db")
        if not os.path.exists(db_path):
            return None, "no series"
        con = None
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            # `column` comes from the fixed _BAND_SIDES table, never from input.
            rows = con.execute(
                f"""SELECT slot_start, slot_end, {column}
                      FROM halfhourly
                     WHERE slot_start >= ? AND slot_start < ?
                       AND {column} > 0""",
                (f"{date_str}T00:00:00", f"{date_str}T23:59:59"),
            ).fetchall()
        except Exception as exc:                        # noqa: BLE001
            self.logger.debug(f"[Flux] {side} weighting unavailable: {exc!r}")
            return None, "no series"
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

        total_kwh = 0.0
        total_p   = 0.0
        for slot_start, slot_end, kwh in rows:
            try:
                value = float(kwh)
                a = _london_localise(datetime.strptime(str(slot_start),
                                                       "%Y-%m-%dT%H:%M:%S"))
                b = _london_localise(datetime.strptime(str(slot_end),
                                                       "%Y-%m-%dT%H:%M:%S"))
            except (TypeError, ValueError):
                return None, "gap"   # an unreadable row is a gap, not a nothing
            if a is None or b is None:
                return None, "gap"
            a = a.astimezone(timezone.utc)
            b = b.astimezone(timezone.utc)
            if value <= 0 or b <= a:
                continue
            priced = self._export_price_over(a, b, spans)
            if priced is None:
                return None, "gap"   # PARTIAL COVERAGE — refuse the whole day
            total_kwh += value
            total_p   += value * priced
        if total_kwh <= 0:
            return None, "nothing flowed"
        return round(total_p / total_kwh, 4), "weighted"

    @staticmethod
    def _export_price_over(a, b, spans):
        """Time-weighted export price across [a, b), or None if not fully covered.

        None is the important half: a slot only partly inside a published
        schedule cannot be priced, and pricing the part that is covered would
        report an average of a different period from the one asked about.
        """
        total_seconds = (b - a).total_seconds()
        if total_seconds <= 0:
            return None
        covered = 0.0
        value   = 0.0
        for span in spans:
            start = max(a, span.start)
            end   = min(b, span.end)
            overlap = (end - start).total_seconds()
            if overlap > 0:
                covered += overlap
                value   += float(span.p) * overlap
        # A second of slack for boundary arithmetic; anything more is a real gap.
        if covered < total_seconds - 1.0:
            return None
        return value / covered

    def _flux_rate_spans(self, key):
        """Published spans for one side of the pair, from the store.

        The store, not latest_rates_data, because _refresh_octopus_rates rebuilds
        that dict wholesale every cycle and a Flux fetch that failed on a good
        cycle would silently drop the last known schedule.
        """
        spans = []
        for slot in (self.store.get(key) or []):
            try:
                start = datetime.fromisoformat(str(slot["valid_from"]).replace("Z", "+00:00"))
                raw_to = slot.get("valid_to")
                if not raw_to:
                    continue            # open-ended: a flat tariff, not Flux
                end = datetime.fromisoformat(str(raw_to).replace("Z", "+00:00"))
                p   = float(slot["value_inc_vat"])
            except (KeyError, TypeError, ValueError):
                continue
            spans.append(_flux_strategy.RateSpan(start=start, end=end, p=p))
        return spans

    def _refresh_flux_rates(self, force=False):
        """Fetch the paired Flux import and export schedules, and the ACCOUNT's
        export agreement.

        Today and tomorrow both, because every decision looks forward to the next
        02:00 and the planner requires contiguous price coverage to that point.
        Last-good data and its timestamp are only replaced on success.

        The export side is new: nothing in this plugin has ever fetched an export
        tariff, because `export_rate_p` has always fallen back to the flat 12p
        Outgoing rate. On Flux that would price every sale wrongly.
        """
        if not self.octopus or _flux_strategy is None:
            return
        try:
            # FAIL CLOSED. A failed account read CLEARS the evidence rather than
            # leaving the last one standing: keeping stale proof while stamping
            # fresh public prices is exactly how a house that had left Flux would
            # go on trading it.
            evidence = self.octopus.get_account_agreements(force=force)
            if not evidence:
                self.store["flux_account_evidence"] = {}
                self.store["flux_rates_problem"] = (
                    "the account's agreements could not be read, so nothing is proven")
                return
            self.store["flux_account_evidence"] = evidence
            imp_agreement = evidence.get("import") or {}
            exp_agreement = evidence.get("export") or {}
            # THE BILLED PRODUCT, never a probe. `_probe_product_by_prefix`
            # returns the most recently LAUNCHED matching product, which on a
            # re-versioned tariff is not the one this house is on — so the rates
            # would come from a product nobody is billed for. No probe and no
            # fallback may arm trading.
            imp_code   = imp_agreement.get("product_code")
            exp_code   = exp_agreement.get("product_code")
            imp_tariff = imp_agreement.get("tariff_code")
            exp_tariff = exp_agreement.get("tariff_code")
            if not (imp_code and exp_code and imp_tariff and exp_tariff):
                self.store["flux_rates_problem"] = (
                    "the account's own Flux import and export products could not both "
                    "be identified, and no probed product may stand in for them")
                return
            # YESTERDAY, today and tomorrow. Yesterday is not optional: the
            # daily history is written just after midnight for the day that has
            # just ENDED, and without its bands that write cannot be weighted —
            # it would silently fall back to a flat rate every single night.
            today = _london_today()
            imp, exp = [], []
            for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
                imp.extend(self.octopus._fetch_rate_schedule(imp_code, imp_tariff, day) or [])
                exp.extend(self.octopus._fetch_rate_schedule(exp_code, exp_tariff, day) or [])
            if not imp or not exp:
                self.store["flux_rates_problem"] = "the Flux rate schedules came back empty"
                return
            self.store["flux_import_slots"]  = imp
            self.store["flux_export_slots"]  = exp
            self.store["flux_rates_at"]      = time.time()
            self.store["flux_rates_problem"] = ""
        except Exception as exc:                        # noqa: BLE001
            self.store["flux_rates_problem"] = f"{type(exc).__name__}: {exc}"
            self.logger.debug(f"[Flux] rate refresh failed: {exc!r}")

    # ---- observation -------------------------------------------------------

    def _flux_observation(self):
        """(flows_dict, observed_at) fresh enough to lease, or None.

        The executor refuses an observation older than ten seconds and this
        plugin's tiered read takes about seven on a ten-second poll, so the
        cached snapshot is routinely too old. Rather than surrender control every
        other tick, one direct read is taken when the cache has aged out. It also
        gives the house and PV figures the site-headroom sums need at the same
        instant as the SOC, which a cached snapshot cannot promise.
        """
        cached_at = float((self.latest_inverter_data or {}).get("_read_at") or 0.0)
        data      = self.latest_inverter_data or {}
        now       = time.time()
        keys      = ("batterySoc", "pvPowerWatts", "homePowerWatts", "gridPowerWatts")
        if cached_at and all(data.get(k) is not None for k in keys) \
                and 0 <= (now - cached_at) <= FLUX_OBSERVATION_MAX_AGE_S:
            return {k: float(data[k]) for k in keys}, cached_at
        if not (self.modbus and self.modbus.connected):
            return None
        fresh = self.modbus.read_power_flows()
        if not fresh:
            return None
        return ({"batterySoc":     float(fresh["batterySoc"]),
                 "pvPowerWatts":   float(fresh["pvPowerWatts"]),
                 "homePowerWatts": float(fresh["homePowerWatts"]),
                 "gridPowerWatts": float(fresh["gridPowerWatts"])},
                time.time())

    def _flux_inputs(self, observed, observed_at):
        """Everything plan() is allowed to see."""
        tz    = _london_tz()
        prefs = self.pluginPrefs
        now   = datetime.now(timezone.utc)
        verified, _why = self._flux_tariff_verified()

        bands = None
        try:
            bands = _flux_strategy.derive_bands(
                self._flux_rate_spans("flux_import_slots"),
                self._flux_rate_spans("flux_export_slots"), tz, now)
        except Exception as exc:                        # noqa: BLE001
            self.logger.debug(f"[Flux] band derivation failed: {exc!r}")

        house = None
        try:
            house = _flux_strategy.HalfHourProfile(
                self.store.get("consumption_profile", []), tz)
        except (ValueError, TypeError) as exc:
            self.logger.debug(f"[Flux] no usable consumption profile: {exc}")

        pv = None
        fc = self.latest_forecast_data or {}
        try:
            buckets = {}
            buckets.update(fc.get("_hourly_p50_today") or {})
            buckets.update(fc.get("_hourly_p50_tomorrow") or {})
            if buckets:
                # PER-DAY factors. The forecast publishes one for today and one
                # for tomorrow and they differ — today has measured generation
                # behind it, tomorrow has none — so a single factor biases the
                # overnight charge by whatever the two disagree by.
                today_local = _london_today()
                pv = _flux_strategy.HourlyPvForecast(
                    buckets, tz,
                    bias=float(fc.get("biasFactor", 1.0) or 1.0),
                    bias_by_date={
                        today_local: float(fc.get("biasFactorToday")
                                           or fc.get("biasFactor", 1.0) or 1.0),
                        today_local + timedelta(days=1):
                            float(fc.get("biasFactorTomorrow")
                                  or fc.get("biasFactor", 1.0) or 1.0),
                    })
        except (ValueError, TypeError) as exc:
            self.logger.debug(f"[Flux] no usable solar forecast: {exc}")

        inv_max_w = int(_as_float(prefs.get("inverterMaxKw"), 10.0) * 1000)
        import_limit_w = int(_as_float(prefs.get("fluxSiteImportLimitKw"),
                                       _as_float(prefs.get("inverterMaxKw"), 10.0)) * 1000)

        site = _flux_strategy.FluxSite(
            capacity_kwh          = _as_float(prefs.get("batteryCapacityKwh"), 35.04),
            charge_power_w        = inv_max_w,
            discharge_power_w     = inv_max_w,
            export_limit_w        = int(_as_float(prefs.get("maxExportKw"), 4.0) * 1000),
            import_limit_w        = import_limit_w,
            import_limit_verified = _as_bool(prefs.get("fluxSiteImportVerified", False)),
            efficiency            = _as_float(prefs.get("batteryEfficiency"), 94.0) / 100.0,
            wear_p_per_kwh        = _as_float(prefs.get("fluxWearPencePerKwh"),
                                              _flux_strategy.DEFAULT_WEAR_P_PER_KWH),
            reserve_pct           = _as_float(prefs.get("fluxReservePct"),
                                              _flux_strategy.DEFAULT_RESERVE_PCT),
            policy_floor_pct      = self._flux_planner_floor_pct(),
            max_charge_soc_pct    = _as_float(prefs.get("fluxMaxChargeSocPct"), 100.0),
        )

        # AGES COME FROM SUCCESSFUL FETCHES, not from when a task last ran.
        # `last_forecast` is stamped whether the fetch worked or not, so a
        # forecast that has been failing for six hours would read as fresh.
        rates_at    = float(self.store.get("flux_rates_at") or 0.0)
        forecast_at = float(self.store.get("forecast_ok_at") or 0.0)
        profile_at  = float(self.store.get("profile_built_at") or 0.0)
        age = (lambda stamp: (time.time() - stamp) if stamp else None)

        flows = _flux_strategy.FluxFlows(
            pv_w      = observed["pvPowerWatts"],
            house_w   = observed["homePowerWatts"],
            grid_w    = observed["gridPowerWatts"],
        )
        return _flux_strategy.FluxInputs(
            now             = now,
            local_tz        = tz,
            bands           = bands,
            site            = site,
            soc_pct         = observed["batterySoc"],
            house           = house,
            pv              = pv,
            flows           = flows,
            commitments     = self._flux_commitments(),
            tariff_verified = verified,
            commissioned    = _as_bool(prefs.get("fluxCommissioned", False)),
            enabled         = _as_bool(prefs.get("fluxEnabled", False)),
            rates_age_s     = age(rates_at),
            forecast_age_s  = age(forecast_at),
            profile_age_s   = age(profile_at),
            telemetry_age_s = max(0.0, time.time() - observed_at),
            flows_age_s     = max(0.0, time.time() - observed_at),
        )

    def _flux_target(self, decision, observed_at):
        """A leased FluxTarget, or None.

        TWO LEASES. The DECISION lease (decision_at .. decision_until) says how
        long this PLAN may speak for the plant; `decision_at` is when the plan was
        made, taken from the decision itself, so a renewal cannot become a
        re-stamp of an expired plan. The OBSERVATION lease (observed_at ..
        expires_at) says how long the reading behind it can be trusted, and that
        is seconds.
        """
        if decision is None or not decision.owns or decision.decision_until is None:
            return None
        now      = datetime.now(timezone.utc)
        made_at  = decision.decision_at or now
        observed = datetime.fromtimestamp(observed_at, tz=timezone.utc)
        until    = decision.decision_until
        if until <= now or made_at > now:
            return None
        if (until - made_at) > timedelta(minutes=30) or (now - made_at) > timedelta(minutes=30):
            return None                 # an expired plan is re-planned, never re-stamped
        expires = min(observed + timedelta(seconds=FLUX_OBSERVATION_LEASE_S), until)
        if expires <= now or observed > now:
            return None
        try:
            return _FluxTarget(
                decision_at          = made_at,
                decision_until       = until,
                observed_at          = observed,
                expires_at           = expires,
                ems_mode             = int(decision.ems_mode),
                charge_limit_w       = int(decision.charge_limit_w),
                discharge_limit_w    = int(decision.discharge_limit_w),
                charge_cutoff_pct    = round(float(decision.charge_cutoff_pct), 1),
                discharge_cutoff_pct = round(float(decision.discharge_cutoff_pct), 1),
            )
        except (TypeError, ValueError) as exc:
            log(f"[Flux] Could not build a target from the decision ({exc}) — "
                f"standing down this cycle", level="WARNING")
            return None

    # ---- release and pre-emption ------------------------------------------

    def _flux_release(self, reason, preempted=False):
        """Hand the inverter back to the baseline and stop owning it.

        `preempted` starts the stand-down period AND changes what an unconfirmed
        hand-back means. At a window end, nobody else wants the registers, so the
        claim is kept and retried. Under pre-emption, retrying a baseline restore
        would put mode 2 over the top of whatever the new owner has just written,
        so the claim is given up outright — which is honest, because somebody
        else really has taken over.
        """
        ex = getattr(self, "flux_executor", None)
        if ex is None:
            return ""
        result = ""
        try:
            if ex.owns_control:
                try:
                    ex.configure_baseline(**self._flux_baseline())
                except Exception as exc:
                    self.logger.debug(f"[Flux] baseline refresh before release: {exc!r}")
                result = ex.step(None, datetime.now(timezone.utc),
                                 communications_ok=bool(self.modbus and self.modbus.connected))
                if result == "released":
                    log(f"[Flux] Control handed back — {reason}. The inverter is on the "
                        f"baseline the manager expects, with the discharge floor at "
                        f"{self._policy_discharge_floor_pct():.0f}%.")
                elif preempted:
                    yielded = ex.step(None, datetime.now(timezone.utc),
                                      supervisor_owns=True,
                                      communications_ok=bool(self.modbus and self.modbus.connected))
                    log(f"[Flux] Hand-back to the baseline was not confirmed "
                        f"({result}; {ex.last_error or 'no detail'}), so the claim has "
                        f"been given up outright — {reason} owns the inverter now and "
                        f"the manager's verify pass will settle the limits.",
                        level="WARNING")
                    result = yielded
                else:
                    log(f"[Flux] Hand-back is not yet confirmed ({result}; "
                        f"{ex.last_error or 'no detail'}) — the supervisor keeps the "
                        f"claim and retries.", level="WARNING")
        except Exception as exc:                        # noqa: BLE001
            log(f"[Flux] Hand-back raised {exc!r} — keeping the claim and retrying",
                level="ERROR")
            result = "pending"
        self.store["flux_applied_key"] = None
        self.store["flux_last_result"] = result
        if preempted:
            self.store["flux_preempted_at"] = time.time()
            self.store["flux_clear_ticks"]  = 0
        return result

    def _flux_preempt(self, reason):
        """Called by anything about to write to the inverter itself, BEFORE it writes.

        Returns True when Flux no longer holds the claim. A False return is worth
        logging by the caller but must never block it: the other owner outranks
        Flux by definition, and a paid window does not wait.

        Cheap and safe when Flux is off.
        """
        self.store["flux_manual_preempt"] = reason
        ex = getattr(self, "flux_executor", None)
        if ex is None or not ex.owns_control:
            self.store["flux_preempted_at"] = time.time()
            self.store["flux_clear_ticks"]  = 0
            return True
        self._flux_release(f"pre-empted by {reason}", preempted=True)
        if self._flux_owns_control():
            log(f"[Flux] {reason} is taking the inverter, but the Flux claim could "
                f"not be relinquished ({ex.last_error or 'no detail'}). Proceeding — "
                f"the new owner's own writes stand.", level="WARNING")
            return False
        return True

    def _flux_clear_preempt(self):
        if self.store.get("flux_manual_preempt"):
            self.store["flux_manual_preempt"] = ""

    def _flux_may_claim(self):
        """True when the stand-down has expired and the coast has been clear for
        several consecutive ticks. One clear reading is not evidence."""
        since = time.time() - float(self.store.get("flux_preempted_at") or 0.0)
        if since < FLUX_PREEMPT_COOLDOWN_S:
            return False
        return int(self.store.get("flux_clear_ticks") or 0) >= FLUX_RECLAIM_TICKS

    # ---- the tick ----------------------------------------------------------

    def _flux_supervisor_tick(self):
        """The whole Flux control path, once per plugin tick. Self-locking."""
        if not self._flux_enabled():
            with self._state_lock:
                self._flux_recover_while_disabled()
            return
        with self._state_lock:
            try:
                self._flux_supervisor_step()
            except Exception:
                self.logger.exception(
                    "[Flux] Supervisor raised — releasing control and continuing")
                try:
                    self._flux_release("the supervisor hit an error", preempted=True)
                except Exception:
                    pass

    def _flux_recover_while_disabled(self):
        """Reconcile a claim left behind, without arming any trading.

        Two ways to get here with a claim outstanding: the feature switched off
        mid-window, or a restart finding a journal from a previous run. Neither
        may simply drop the executor — the claim is durable, the hardware may
        still be in a Flux mode, and discarding the object loses the only thing
        that knows to reconcile. So the executor is ADOPTED (built if a journal
        exists), released, and only let go once the release is confirmed.

        No target is ever offered on this path, so nothing here can trade.
        """
        ex = getattr(self, "flux_executor", None)
        if ex is None:
            if not FLUX_AVAILABLE or self.modbus is None:
                return
            try:
                if not os.path.exists(self._flux_journal_path()):
                    self.store["flux_status"] = "off"
                    return
            except Exception:
                return
            ex = self._flux_ensure_executor(allow_create=True)
            if ex is None:
                return
            log("[Flux] A claim journal was found while the strategy is switched "
                "off — reconciling the inverter back to the baseline before "
                "standing down.", level="WARNING")
        owner = self._flux_other_owner()
        if owner:
            # Keep the executor AND journal throughout the external owner's
            # tenure, including later ticks when owns_control is already false.
            self.store["flux_last_result"] = ex.step(
                None, datetime.now(timezone.utc), supervisor_owns=True,
                communications_ok=bool(self.modbus and self.modbus.connected))
            self.store["flux_status"] = f"switched off; {owner} owns the inverter"
            return
        # Always step after the owner leaves: its exclusive ownership does not
        # prove the baseline. The real executor reconciles supervisor handback
        # even though owns_control was false while that owner was active.
        try:
            ex.configure_baseline(**self._flux_baseline())
            result = ex.step(None, datetime.now(timezone.utc),
                             communications_ok=bool(self.modbus and self.modbus.connected))
        except Exception as exc:
            self.logger.warning(f"[Flux] Disabled recovery still pending: {exc}")
            result = "pending"
        self.store["flux_last_result"] = result
        if result != "released":
            self.store["flux_status"] = "switched off, but the hand-back is not confirmed — retrying"
            return
        self.flux_executor = None
        self.store["flux_status"] = "off"
        try:
            os.remove(self._flux_journal_path())
        except OSError:
            pass

    def _flux_supervisor_step(self):
        """One pass. Caller holds _state_lock."""
        ex = self._flux_ensure_executor()
        if ex is None:
            self.store["flux_status"] = "unavailable"
            return

        comms = bool(self.modbus and self.modbus.connected)

        # A manual pre-emption is sticky for the stand-down and then lets go. The
        # STATE it leaves behind (an import in flight, a paused manager) keeps its
        # own entry below for as long as it is really true.
        if (self.store.get("flux_manual_preempt")
                and time.time() - float(self.store.get("flux_preempted_at") or 0.0)
                >= FLUX_PREEMPT_COOLDOWN_S):
            self._flux_clear_preempt()

        owner = self._flux_other_owner()
        if owner != (self.store.get("flux_owner_reason") or ""):
            # Say so when Flux starts or stops standing aside. Silence here is how
            # a whole peak window went by on 17-Sep-2026 with nobody knowing why.
            if owner:
                log(f"[Flux] Standing aside: {owner}.")
            elif self.store.get("flux_owner_reason"):
                log(f"[Flux] No longer standing aside ({self.store.get('flux_owner_reason')} has ended).")
        self.store["flux_owner_reason"] = owner
        if owner:
            # NEVER a baseline restore here. By the time an owner is VISIBLE to
            # this pass it has already written its own mode and limits — every
            # path that takes the inverter calls _flux_preempt first, which is
            # where the clean, ordered hand-back happens. Restoring now would put
            # mode 2 over the top of a live paid export, which is the exact
            # failure the ordered pre-emption exists to prevent. So the claim is
            # given up with no writes at all, and the new owner plus the
            # manager's verify pass settle the registers.
            self.store["flux_clear_ticks"]  = 0
            self.store["flux_last_result"]  = ex.step(
                None, datetime.now(timezone.utc),
                supervisor_owns=True, communications_ok=comms)
            self.store["flux_preempted_at"] = time.time()
            self.store["flux_applied_key"]  = None
            self.store["flux_status"] = f"standing down — {owner}"
            return
        self.store["flux_clear_ticks"] = int(self.store.get("flux_clear_ticks") or 0) + 1

        if not comms:
            # The executor keeps the claim and reports pending: a lost connection
            # is not an acknowledgement that anything stopped, and nothing here
            # is a hardware timer.
            self.store["flux_last_result"] = ex.step(
                None, datetime.now(timezone.utc), communications_ok=False)
            self.store["flux_status"] = "inverter unreachable — claim held, not confirmed"
            return

        observation = self._flux_observation()
        if observation is None:
            if ex.owns_control:
                self._flux_release("the inverter readings could not be refreshed")
            self.store["flux_status"] = "no usable inverter reading"
            return
        observed, observed_at = observation

        inputs   = self._flux_inputs(observed, observed_at)
        decision = _flux_strategy.plan(inputs)
        self.store["flux_decision"] = decision
        self.store["flux_status"]   = _flux_strategy.describe(decision)
        self._flux_log_decision(decision)

        # A late announcement must re-plan at once rather than ride out the
        # applied command: the reservation it creates changes what is spare.
        signature = _flux_strategy.commitment_signature(inputs.commitments)
        if signature != self.store.get("flux_commitment_signature"):
            if self.store.get("flux_commitment_signature") is not None:
                log(f"[Flux] Grid-event commitments changed — re-planning. "
                    f"{len(inputs.commitments)} now stand.")
            self.store["flux_commitment_signature"] = signature
            self.store["flux_applied_key"] = None

        if not decision.owns:
            if ex.owns_control:
                self._flux_release(decision.reason)
            return

        if not ex.owns_control and not self._flux_may_claim():
            self.store["flux_status"] = (
                "waiting out the stand-down period after being pre-empted")
            return

        # RENEWED EVERY TICK FROM A FRESH OBSERVATION. A skip longer than the
        # observation lease would leave the executor holding a decision whose
        # reading had expired, and nothing on the inverter enforces that lease.
        # The executor's renewal path is read-back only when the command has not
        # changed, which is what makes this affordable.
        target = self._flux_target(decision, observed_at)
        if target is None:
            if ex.owns_control:
                self._flux_release("the decision could not be leased")
            return

        result = ex.step(target, datetime.now(timezone.utc), communications_ok=True)
        self.store["flux_last_step"]   = time.time()
        self.store["flux_last_result"] = result
        if result == "applied":
            self.store["flux_applied_key"]   = decision.control_key()
            self.store["flux_pending_since"] = 0.0
        else:
            self.store["flux_applied_key"] = None
            if result == "pending":
                self._flux_note_pending(ex, decision)
            else:
                self.store["flux_pending_since"] = 0.0

    def _flux_note_pending(self, ex, decision):
        """Record an unacknowledged command and keep retrying it.

        There is deliberately NO timeout that declares somebody else has taken
        over: `supervisor_owns` means another owner really has the registers, and
        saying so because a clock ran out would be a lie told to the one component
        that must not be lied to. So the claim stands and the release is retried
        every tick, with one ERROR at the start and one an hour after that, until
        it is confirmed or a real owner pre-empts it.
        """
        started = float(self.store.get("flux_pending_since") or 0.0)
        now     = time.time()
        if not started:
            self.store["flux_pending_since"] = now
            self.store["flux_pending_logged"] = now
            # THE WINDOW ENDING IS NOT A FAULT. A renewal issued in the last seconds
            # of the window is refused by the executor, because its lease may not
            # outlive the window — which is correct, and it is what happens at 19:00
            # and 05:00 every day. Logging that as a WARNING about an inverter that
            # "did not acknowledge" reads as a failure (live, 17-Sep-2026 19:00:33)
            # and would teach the reader to ignore the message that matters.
            until = getattr(decision, "decision_until", None)
            if until is not None and until <= datetime.now(timezone.utc):
                log(f"[Flux] The {decision.mode} window ended while the last command "
                    f"was still in flight, so it was not renewed. The inverter goes "
                    f"back to the manager on the next tick.")
                return
            log(f"[Flux] The inverter did not acknowledge the {decision.mode} "
                f"command ({ex.last_error or 'no detail'}). The supervisor keeps the "
                f"claim and retries; the ordinary manager stays hands-off while a "
                f"claim is outstanding.", level="WARNING")
            return
        if now - float(self.store.get("flux_pending_logged") or 0.0) < 3600:
            return
        self.store["flux_pending_logged"] = now
        log(f"[Flux] The inverter has not acknowledged a Flux command for "
            f"{int((now - started) // 60)} minutes ({ex.last_error or 'no detail'}). "
            f"The claim is still outstanding and the ordinary manager is hands-off. "
            f"Switch the Flux strategy off in the plugin configuration to force a "
            f"reconciled hand-back.", level="ERROR")

    def _flux_log_decision(self, decision):
        """One line when the plan changes, and never the same line twice.

        The key comes from flux_strategy.note_key(), NOT from the message: the
        reason text carries a figure that moves every tick, so keying on the
        prose meant the guard never fired. See note_key() for the detail.
        """
        key = _flux_strategy.note_key(decision)
        if self.store.get("flux_note_logged") == key:
            return
        self.store["flux_note_logged"] = key
        log(f"[Flux] {_flux_strategy.describe(decision)}")

    # ================================================================
    # Indigo Action Callbacks
    # ================================================================

    def actionForceGridImport(self, action):
        """Action: Force immediate grid import. Clamps to the bounds the ConfigUI
        labels promise (power 0..inverterMaxKw, target SOC 10..100%)."""
        with self._state_lock:
            # v5.71.1: same rule as the queued import — a grid charge inside a paid
            # export window forfeits the premium, and it also sets export_active
            # False beneath a state machine that is still driving the export, so the
            # store would then disagree with the inverter. Refused rather than
            # silently held, because this one has a person behind it who deserves an
            # answer. Pause the manager first if the window really must be overridden.
            _vpp_state = self.store.get("vpp_state", VPP_IDLE)
            if _vpp_state in (VPP_PRE_CHARGING, VPP_ACTIVE):
                log(f"[Action] Force grid import REFUSED — a VPP window is "
                    f"{_vpp_state} and importing now would forfeit the export "
                    f"premium. Pause the manager first if you need to override it.",
                    level="WARNING")
                return
            # A person has asked for this, so Flux lets go BEFORE the write, not
            # after. See _flux_preempt: discovering the manual action on the next
            # supervisor pass would mean reading back registers this action had
            # just set and "correcting" them.
            self._flux_preempt("a Force Grid Import action")
            props      = action.props
            inv_max_kw = _as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0)
            power_kw   = min(max(0.0, _as_float(props.get("powerKw"), inv_max_kw)), inv_max_kw)
            target_soc = min(max(10.0, _as_float(props.get("targetSocPct"), 80.0)), 100.0)
            log(f"[Action] Force grid import: {power_kw:.1f}kW to {target_soc:.0f}% SOC")
            cutoff = min(target_soc + 3.0, 100.0)
            if self.modbus and self.modbus.force_charge(int(power_kw * 1000),
                                                        cutoff_soc=cutoff):
                self.store["import_active"]     = True
                self.store["import_target_soc"] = target_soc
                self.store["export_active"]     = False
                self._set_import_cutoff(cutoff)

    def actionForceExport(self, action):
        """Action: Force immediate grid export at the DNO cap (test).

        night_export sets the battery DISCHARGE limit; grid export itself is capped
        automatically by the inverter's commissioned DNO limit (no grid-export
        setpoint is written). So the powerKw field sets the discharge HEADROOM —
        default = inverter max, which exports the full DNO allocation; a lower value
        limits how hard the battery can discharge (grid export then = headroom minus
        house load). The field used to be ignored entirely (always inverter max)."""
        with self._state_lock:
            self._flux_preempt("a Force Grid Export action")
            inv_max_kw  = _as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0)
            power_kw    = min(max(0.0, _as_float(action.props.get("powerKw"), inv_max_kw)),
                              inv_max_kw)
            discharge_w = int(power_kw * 1000)
            log(f"[Action] Force export: discharge limit {power_kw:.1f}kW "
                f"(grid export auto-capped at the DNO limit)")
            if self.modbus and self.modbus.night_export(discharge_w):
                if self.store.get("import_active"):
                    self._restore_import_cutoff()          # v5.100.0
                self.store["export_active"]  = True
                self.store["import_active"]  = False

    def actionForceDaytimeExport(self, action):
        """Action: Force PV-first grid export (mode 0x05) for hardware validation.

        Lets us confirm the inverter's real 0x05 behaviour before trusting it in
        an unattended VPP window: with PV above the DNO cap the grid should hold
        ~4 kW sourced from PV (battery flat / charging from surplus); below the
        cap the battery should top the export up to 4 kW. Watch home_status /
        the inverter device states after firing. Reverts via Set Self-Consumption
        (or the manager's next tick) — pause the manager first to hold it.
        """
        with self._state_lock:
            self._flux_preempt("a Force Daytime Export action")
            inv_max_w = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
            log("[Action] Force daytime export: mode 0x05 (Discharge PV First) — test")
            if self.modbus and self.modbus.daytime_export(inv_max_w):
                self.store["export_active"] = True
                self.store["import_active"] = False

    def actionForceVppExportTest(self, action):
        """Action: exercise the live VPP export driver (auto bank vs discharge) for testing.

        Forces a daytime VPP context and runs _drive_vpp_export() once against the
        current live PV/home, so the inverter picks the same sub-mode it would in a
        real daytime event: bank-surplus (mode 0x02 + charge cap) when PV exceeds the
        export target, or discharge (0x05) when it doesn't. Pause the manager first to
        hold it; restore with Set Self-Consumption then Resume Battery Manager.
        """
        with self._state_lock:
            self._flux_preempt("a Force VPP Export Drive action")
            self.store["vpp_is_daytime"]        = True
            self.store["vpp_export_submode"]    = None
            self.store["vpp_bank_charge_cap_w"] = -1
            log("[Action] Force VPP export drive (test) — daytime context; live PV "
                "decides bank vs discharge")
            self._drive_vpp_export()

    def actionSetSelfConsumption(self, action):
        """Action: Return to self-consumption mode."""
        with self._state_lock:
            self._flux_preempt("a Set Self-Consumption action")
            log("[Action] Set self-consumption mode")
            if self.modbus:
                self.modbus.set_self_consumption()
                if self.store.get("import_active"):
                    self._restore_import_cutoff()          # v5.100.0
                self.store["import_active"] = False
                self.store["export_active"] = False

    def actionReturnToLocalEms(self, action):
        """Action: Disable Remote EMS and return to local inverter control."""
        with self._state_lock:
            # Remote EMS is about to be DISABLED, which takes the registers the
            # executor holds its claim over out from under it. Let go first.
            self._flux_preempt("a Return to Local EMS action")
            log("[Action] Return to local EMS control")
            if self.modbus:
                self.modbus.return_to_local()
                if self.store.get("import_active"):
                    self._restore_import_cutoff()          # v5.100.0
                self.store["import_active"] = False
                self.store["export_active"] = False

    def _set_manager_paused(self, paused, source):
        """Single entry point for pause/resume (Pause/Resume actions + the
        sigen_manager_paused variable).  Caller must hold self._state_lock.

        On PAUSE we hand the inverter back to self-consumption — the same safe
        baseline prepare_to_sleep() uses — so a latched force-charge or
        force-export cannot keep importing/exporting under a "Paused" label
        (the self-sufficiency KPI must never lose to a dead control).  The
        manager evaluate is then gated in _evaluate_manager while paused.
        On RESUME we force an immediate re-evaluation on the next tick.
        """
        was_paused = self.store.get("manager_paused", False)
        # Pause means hands off the inverter, and that has to include the Flux
        # supervisor — otherwise "Paused" would be a label on a plugin still
        # trading. Released BEFORE the hand-back write below, and before the
        # pause flag can be read by anything else.
        if paused and not was_paused:
            self._flux_preempt(f"the manager being paused ({source})")
        self.store["manager_paused"] = paused
        dev = self._find_device("batteryManager")
        if dev:
            dev.updateStateOnServer(
                "managerStatus", value="Paused" if paused else "Running"
            )
        # Mirror into the sigen_manager_paused variable — it is the ONLY thing
        # startup seeds the paused flag from, so an action-initiated pause that
        # never wrote it silently resumed inverter control after any restart
        # (and the variable disagreed with the device label throughout). The
        # variableUpdated value-compare guard makes this converge, not loop.
        try:
            var_id = self._ensure_var("sigen_manager_paused",
                                      self._sigenergy_folder_id())
            if var_id:
                indigo.variable.updateValue(var_id,
                                            "true" if paused else "false")
        except Exception as exc:
            log(f"[Pause] Could not mirror pause state to variable: {exc}",
                level="WARNING")
        if paused:
            # Hand the inverter back to the safe baseline AND release any raised
            # discharge-cutoff floor (flood-prev / storm / VPP) + stand down any
            # in-flight VPP export — the same disengage prepare_to_sleep() uses.
            # Without the cutoff release a 'Paused' manager could leave the battery
            # locked above a raised SOC and silently force grid import for the whole
            # pause. import_active/export_active are cleared inside the helper.
            if self.modbus and self.modbus.connected:
                log(f"[Pause] Manager paused ({source}) — inverter returned to "
                    f"self-consumption and raised floors released; holding hands-off "
                    f"until resumed.")
            else:
                log(f"[Pause] Manager paused ({source}) — modbus offline, inverter "
                    f"left as-is; raised-floor flags cleared.")
            self._disengage_to_safe_baseline("Pause")
        elif was_paused:
            # Resume: re-evaluate now rather than waiting up to MANAGER_EVAL_INTERVAL.
            self.store["last_manager"] = 0.0
            # Resuming is the explicit end of the manual hold. The cooldown still
            # runs (see _flux_may_claim) — the person gets their hands back on the
            # inverter immediately, and Flux has to wait its turn.
            self._flux_clear_preempt()
            log(f"[Pause] Manager resumed ({source}) — re-evaluating immediately.")

    def actionPauseManager(self, action):
        """Action: Pause battery manager (hands the inverter back to
        self-consumption, then stops the manager acting — see
        _set_manager_paused)."""
        with self._state_lock:
            self._set_manager_paused(True, "Pause action")

    def actionResumeManager(self, action):
        """Action: Resume battery manager."""
        with self._state_lock:
            self._set_manager_paused(False, "Resume action")

    def actionRefreshForecast(self, action):
        """Action: Manual solar forecast refresh (Open-Meteo)."""
        # v5.45.0: unlocked — the fetch runs outside the lock by design and
        # the stamp write is a scalar dict op.
        log("[Action] Manual solar forecast refresh")
        self._refresh_forecast(force=True)
        self.store["last_forecast"] = time.time()

    def actionRefreshOctopus(self, action):
        """Action: Manual Octopus rates refresh."""
        # v5.45.0: unlocked — see actionRefreshForecast.
        log("[Action] Manual Octopus rates refresh")
        self._refresh_octopus_rates(force=True)
        self.store["last_octopus"] = time.time()

    # ================================================================
    # Indigo Menu Callbacks
    # ================================================================

    def menuRefreshAll(self):
        """Menu: Force refresh solar forecast + Octopus + re-evaluate manager.

        Runs under _state_lock like every sibling action callback — unlocked it
        raced the background tick, running the manager (store mutation + Modbus
        writes) concurrently on two threads.
        """
        log("[Menu] Refresh All: fetching solar forecast, Octopus and re-evaluating...")
        # v5.45.0: no outer lock — the fetches run unlocked by design and
        # _evaluate_manager is self-locking, so holding the lock here would
        # just stall callbacks behind two HTTP fetches for no protection.
        self._refresh_forecast(force=True)
        self.store["last_forecast"] = time.time()
        self._refresh_octopus_rates(force=True)
        self.store["last_octopus"] = time.time()
        self._evaluate_manager()
        self.store["last_manager"] = time.time()
        log("[Menu] Refresh All complete")
        return True

    def menuShowStatus(self):
        """Menu: Log current manager status to event log."""
        from datetime import datetime as _dt
        now_str  = _dt.now().strftime("%H:%M:%S")
        capacity = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), 35.04)

        inv   = self._find_device("sigenergyInverter")
        mgr   = self._find_device("batteryManager")
        fcast = self._find_device("solarForecast")
        tarif = self._find_device("tariffMonitor")

        log("[Status] ======= Live Status: " + now_str + " =======")

        # --- Battery ---
        if inv:
            soc_pct  = float(inv.states.get("batterySoc", 0))
            soc_kwh  = soc_pct / 100.0 * capacity
            batt_w   = int(inv.states.get("batteryPowerWatts", 0))
            modbus   = inv.states.get("modbusConnected", "False")
            if batt_w > 50:
                batt_str = f"Charging {batt_w}W"
            elif batt_w < -50:
                batt_str = f"Discharging {abs(batt_w)}W"
            else:
                batt_str = "Idle"
            log(f"[Status] Battery:  {soc_pct:.1f}% SOC  |  {soc_kwh:.1f} kWh stored  |  {batt_str}"
                f"  |  Modbus: {'OK' if modbus == 'True' else 'OFFLINE'}")
        else:
            log("[Status] Battery:  No inverter device found", level="WARNING")

        # --- Solar & Grid & Home ---
        # Forecast reads depend only on fcast — hoisted out of the `if inv:`
        # block so the Tomorrow line at the bottom can't hit an unassigned
        # fcst_tmrw when a forecast device exists but no inverter does.
        # Use remainingTodayKwh (now -> dusk), NOT correctedTodayKwh
        # (whole-day forecast) — same double-count bug as the Today
        # Energy Summary, fixed here at the same time.
        fcst_remain  = fcast.states.get("remainingTodayKwh", "?") if fcast else "?"
        fcst_tmrw    = fcast.states.get("correctedTomorrowKwh", "?") if fcast else "?"
        if inv:
            pv_w    = int(inv.states.get("pvPowerWatts", 0))
            grid_w  = int(inv.states.get("gridPowerWatts", 0))
            home_w  = int(inv.states.get("homePowerWatts", 0))
            ems     = inv.states.get("emsWorkMode", "Unknown")
            grid_str = f"Exporting {abs(grid_w)}W" if grid_w < -50 else (
                       f"Importing {grid_w}W" if grid_w > 50 else "Idle (grid)")
            pv_today   = self.store.get("pv_daily_kwh", 0.0)
            imp_today  = self.store.get("grid_import_daily_kwh", 0.0)
            exp_today  = self.store.get("grid_export_daily_kwh", 0.0)
            home_today = self.store.get("home_daily_kwh", 0.0)
            try:
                remain_val          = float(fcst_remain)
                fcst_expected_total = round(pv_today + max(0.0, remain_val), 1)
                fcst_remain_str     = f"{remain_val:.1f}"
            except (ValueError, TypeError):
                fcst_expected_total = "?"
                fcst_remain_str     = str(fcst_remain)
            log(f"[Status] Solar:    {pv_w}W now  |  {pv_today:.2f} kWh today"
                f"  |  {fcst_remain_str} kWh forecast remaining  |  {fcst_expected_total} kWh expected total")
            log(f"[Status] Grid:     {grid_str}  |  Import today {imp_today:.2f} kWh"
                f"  |  Export today {exp_today:.2f} kWh")
            log(f"[Status] Home:     {home_w}W now  |  {home_today:.2f} kWh today"
                f"  |  EMS mode: {ems}")

        # --- Tariff ---
        if tarif:
            t_name = tarif.states.get("tariffActive", "?")
            t_rate = tarif.states.get("rateToday", "?")
            t_tmrw = tarif.states.get("rateTomorrow", "?")
            log(f"[Status] Tariff:   {t_name}  |  Today {t_rate}p/kWh  |  Tomorrow {t_tmrw}p/kWh")

        # --- Manager decision ---
        if mgr:
            action   = mgr.states.get("currentAction", "unknown")
            reason   = mgr.states.get("currentReason", "")
            viable   = mgr.states.get("dawnViable", "?")
            soc_dawn = mgr.states.get("socAtDawn", "?")
            sched    = mgr.states.get("importScheduledTime", "")
            log(f"[Status] Manager:  {action}  |  {reason}")
            log(f"[Status] Dawn:     Viable={viable}  |  SOC at dawn {soc_dawn} kWh"
                + (f"  |  Import scheduled {sched}" if sched else ""))

        # --- Tomorrow ---
        if fcast:
            log(f"[Status] Tomorrow: {fcst_tmrw} kWh solar forecast")

        log("[Status] =============================================")
        return True

    def menuShowDailyHistory(self):
        """Menu: Log last 7 days from daily_history.json."""
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            log("[History] No daily_history.json found yet", level="WARNING")
            return True

        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception as e:
            log(f"[History] Cannot read daily_history.json: {e}", level="ERROR")
            return True

        recent = records[-7:] if len(records) >= 7 else records
        log(f"[History] Last {len(recent)} days:")
        for r in reversed(recent):
            import_flag = " IMPORT" if r.get("import_events", 0) > 0 else ""
            export_flag = f" exports={r['export_events']}" if r.get("export_events", 0) > 0 else ""
            vpp_flag    = " VPP" if r.get("vpp_event") else ""
            log(
                f"  {r['date']}  PV={r.get('pv_kwh', 0):.1f}kWh "
                f"(fcst={r.get('pv_forecast_kwh', 0):.1f}) "
                f"Import={r.get('grid_import_kwh', 0):.2f} "
                f"Export={r.get('grid_export_kwh', 0):.2f} "
                f"Home={r.get('home_kwh', 0):.1f} "
                f"SOC {r.get('min_soc', 0):.0f}-{r.get('peak_soc', 0):.0f}%"
                f"{import_flag}{export_flag}{vpp_flag}"
            )
        return True

    def menuShowSolarOverflowShadow(self):
        """Log recent log-only SOC-target pacing and tariff-baseline evidence.

        v5.106.0: the header and every row name the targets the record itself
        carries. The old wording said "90% live vs 95%" whatever the file held,
        which read as a live experiment for the fortnight the experiment was
        dead.
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError) as exc:
            log(f"[Shadow] No completed-day comparison available: {exc}", level="WARNING")
            return True

        rows = [r for r in records if r.get("solar_overflow_shadow")][-21:]
        if not rows:
            log("[Shadow] No completed-day comparison available yet — first row is written at midnight")
            return True
        log(f"[Shadow] SOC-target pacing / Tracker vs Agile baseline ({len(rows)} days)")
        for row in reversed(rows):
            s = row["solar_overflow_shadow"]
            tariff = s.get("tariff_baseline", {})
            export = float(s.get("estimated_export_foregone_kwh", 0.0) or 0.0)
            soc = s.get("observed_end_soc_pct")
            tracker = tariff.get("tracker_cost_gbp")
            agile = tariff.get("agile_cost_gbp")
            costs = (f"Tracker £{tracker:.2f} / Agile £{agile:.2f}"
                     if tracker is not None and agile is not None else
                     f"tariff coverage incomplete ({tariff.get('missing_price_slots', 0)} import slots)")
            samples = int(s.get("samples", 0) or 0)
            live_pct   = s.get("live_target_pct")
            shadow_pct = s.get("shadow_target_pct")
            pair = (f"{live_pct:g}% live vs {shadow_pct:g}%"
                    if live_pct is not None and shadow_pct is not None else "targets not recorded")
            # A zero-sample day must say which kind of zero it is. Rows written
            # before v5.106.0 carry no reason and are marked as such rather than
            # reported as a clean nothing-to-report.
            if samples:
                verdict = (f"the stricter target withheld ~{export:.2f}kWh of export")
            else:
                reason = str(s.get("skipped_reason") or "")
                verdict = ("no comparison ran — " + reason if reason else
                           "no comparison ran (no reason recorded — pre-v5.106.0 row)")
            log(
                f"[Shadow] {row.get('date', '?')}: {pair}, samples={samples}; "
                f"{verdict}; end SOC={soc if soc is not None else '?'}%; "
                f"evening import={s.get('observed_evening_import_kwh', 0):.2f}kWh; {costs}"
            )
        log("[Shadow] Tariff figures hold import timing constant; they are not an Agile battery-dispatch forecast.")
        return True

    def menuShowBankFirstReport(self):
        """Log the last 21 days of the bank-first record.

        clip_boundary_minutes is the column that matters: zero on every small day
        means holding export back cost nothing measurable. Anything else means the
        threshold is too high and should come down.
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError) as exc:
            log(f"[BankFirst] No completed-day record available: {exc}", level="WARNING")
            return True

        rows = [r for r in records if r.get("bank_first")][-21:]
        if not rows:
            log("[BankFirst] No completed-day record yet — the first row is written at midnight")
            return True

        log(f"[BankFirst] Daytime export hold, last {len(rows)} days")
        log("[BankFirst] date        fcst  small  held   withheld  peakSOC  >=95m  >=99m  clip  arm")
        for row in reversed(rows):
            b = row["bank_first"]
            held = int(b.get("minutes_held", 0))
            # v5.106.0: the FIRST classification and the forecast it was made from,
            # falling back to the 23:59 pair on rows written before those existed.
            # The midnight values describe the end of the day, so a day held all
            # morning and promoted at tea time printed as "no" and read as a day
            # the hold never touched.
            _first_small = b.get("first_classified_small")
            _small = _first_small if _first_small is not None else b.get("classified_small")
            _fcst  = b.get("first_classified_from_kwh")
            if _fcst is None:
                _fcst = b.get("classified_from_kwh", 0.0)
            log(f"[BankFirst] {row.get('date', '?'):<10} "
                f"{float(_fcst or 0.0):5.1f}  "
                f"{('yes' + ('*' if b.get('promoted_local') else '')) if _small else 'no ':<5} "
                f"{held // 60}h{held % 60:02d}m  "
                f"{float(b.get('export_withheld_kwh', 0.0)):7.2f}  "
                f"{float(b.get('peak_soc_pct', 0.0)):6.1f}  "
                f"{int(b.get('minutes_soc_ge_95', 0)):5d}  "
                f"{int(b.get('minutes_soc_ge_99', 0)):5d}  "
                f"{int(b.get('clip_boundary_minutes', 0)):4d}  "
                f"{int(b.get('arm_minutes', 0)):3d}")
        _clip = sum(int(r["bank_first"].get("clip_boundary_minutes", 0)) for r in rows)
        _held = sum(float(r["bank_first"].get("export_withheld_kwh", 0.0)) for r in rows)
        if _clip == 0:
            log(f"[BankFirst] No clip-boundary minutes across {len(rows)} days — holding "
                f"export back cost nothing measurable. {_held:.1f} kWh delayed, not lost.")
        else:
            log(f"[BankFirst] {_clip} clip-boundary minutes across {len(rows)} days — solar "
                f"was being thrown away with the battery full. Lower the kWh threshold.",
                level="WARNING")
        _promoted = [r["date"] for r in rows if r["bank_first"].get("promoted_local")]
        if _promoted:
            log(f"[BankFirst] Held then released as the forecast rose on {len(_promoted)} "
                f"day(s) ({', '.join(_promoted)}) — marked * above. Since v5.106.0 that "
                f"needs the forecast to clear the threshold by "
                f"{BANK_FIRST_PROMOTE_MARGIN_KWH:.0f} kWh, not by any amount.")
        return True

    def menuShowTariffRates(self):
        """Menu: Log current Tracker/Go/Flux rates from cached Octopus data."""
        rates = self.latest_rates_data
        if not rates:
            log("[Tariff] No rates cached yet — run Refresh All first", level="WARNING")
            return True

        tariff_info = rates.get("tariff_info", {})
        tracker     = rates.get("tracker", {})
        go          = rates.get("go", {})
        flux        = rates.get("flux", {})

        flexible = rates.get("flexible", {})

        log(f"[Tariff] Active tariff: {tariff_info.get('display_name', '?')} "
            f"({tariff_info.get('tariff_key', '?')})")

        # Flexible Octopus (flat rate — no TOU windows)
        if flexible.get("today_p") is not None:
            log(f"[Tariff] Flexible: {flexible['today_p']:.2f}p/kWh (flat rate, no cheap window)")

        # Tracker (shown if available, e.g. when user is on Tracker or monitoring it)
        today_p    = tracker.get("today_p")
        tomorrow_p = tracker.get("tomorrow_p")
        if today_p is not None:
            log(f"[Tariff] Tracker today: {today_p:.2f}p/kWh" +
                (f"  tomorrow: {tomorrow_p:.2f}p/kWh" if tomorrow_p is not None else
                 "  tomorrow: not yet published"))
        else:
            log("[Tariff] Tracker: not available (suspended or not active)")

        if go:
            log(f"[Tariff] Go: off-peak={go.get('cheap_p', '?')}p "
                f"({go.get('cheap_start', '?')}-{go.get('cheap_end', '?')}) "
                f"standard={go.get('standard_p', '?')}p")
        if flux:
            log(f"[Tariff] Flux: off-peak={flux.get('cheap_p', '?')}p "
                f"({flux.get('cheap_start', '?')}-{flux.get('cheap_end', '?')}) "
                f"peak={flux.get('peak_p', '?')}p "
                f"standard={flux.get('standard_p', '?')}p")
        return True

    def menuShowSavingSessions(self):
        """Menu: what Octopus is offering, and whether we are actually in it.

        REPORT ONLY. It forces a fresh poll, and joining happens only as a side
        effect of that poll when the owner has ticked automatic opt-in. There is
        deliberately no "join now" button here: one decision, one owner. A status
        menu that quietly commits the account to a session would be a second route
        to the same irreversible act, and the two would drift.
        """
        if log_startup_banner:
            log_startup_banner(self.pluginId, self.pluginDisplayName, self.pluginVersion)
        if not self.octopus:
            log("[SavingSessions] No Octopus account is configured.", level="WARNING")
            return
        auto = _as_bool(self.pluginPrefs.get("savingSessionAutoJoin"), False)
        drive = _as_bool(self.pluginPrefs.get("savingSessionExport"), False)
        log(f"[SavingSessions] Automatic opt-in: {'ON' if auto else 'off'}   "
            f"Drive the battery: {'ON' if drive else 'off'}")
        try:
            data = self.octopus.get_saving_sessions(force=True)
        except Exception as exc:                                    # noqa: BLE001
            log(f"[SavingSessions] Fetch failed: {exc}", level="WARNING")
            return
        if not data:
            log("[SavingSessions] Could not read the account — see the warnings above.",
                level="WARNING")
            return

        tokens = data.get("token_balance")
        log(f"[SavingSessions] Campaign joined: "
            f"{'YES' if data.get('has_joined') else 'NO'}   "
            f"Happy Hour tokens: {'not reported' if tokens is None else tokens}")

        now_utc = datetime.now(timezone.utc)
        tz      = _london_tz()
        upcoming = [e for e in (data.get("events") or [])
                    if e.get("end_at") and e["end_at"] > now_utc]
        if not upcoming:
            log("[SavingSessions] No sessions announced.")
            return
        for e in upcoming:
            start = e["start_at"].astimezone(tz) if tz else e["start_at"]
            end   = e["end_at"].astimezone(tz)   if tz else e["end_at"]
            log(f"[SavingSessions]   {start.strftime('%a %d %b %H:%M')}"
                f"-{end.strftime('%H:%M')}  "
                f"{(e.get('direction') or 'UNKNOWN').replace('_', ' ').title():<18} "
                f"{e.get('reward_per_kwh_points', 0)} pts/kWh "
                f"(~{_points_to_pence(e.get('reward_per_kwh_points', 0)):.1f}p/kWh)  "
                f"{'OPTED IN' if e.get('joined') else 'not opted in'}"
                + (f"  [{e['capacity']}]" if e.get("capacity") else ""))
        refused = self.store.get("saving_sessions_join_refused") or []
        if refused:
            log(f"[SavingSessions] Previously refused and not retried: "
                f"{', '.join(str(x) for x in refused)}")

    def menuShowVppStatus(self):
        """Menu: Log current VPP state and next event details."""
        state   = self.store.get("vpp_state", "idle")
        active  = self.store.get("vpp_active", False)
        event   = self.store.get("vpp_event") or {}
        vpp_log(f"[VPP] State={state} Active={'YES' if active else 'no'}")
        if event:
            start = event.get("start_time")
            end   = event.get("end_time")
            start_s = _local_time(start, "%H:%M %d/%m") if start else "?"
            end_s   = _local_time(end)                  if end   else "?"
            vpp_log(f"[VPP] Next event: {start_s} - {end_s} BST "
                f"({event.get('duration_hrs', 0):.1f}h) "
                f"precharge={self.store.get('vpp_pre_charge_soc', 0):.0f}%")
        else:
            vpp_log("[VPP] No event scheduled")
        if not self.axle:
            vpp_log("[VPP] Axle API not configured", level="WARNING")
        return True

    def menuShowVppExport(self):
        """Menu: Log VPP export summary for today."""
        axle_enabled = self.pluginPrefs.get("axleEnabled", False)
        state        = self.store.get("vpp_state", "idle")
        active       = self.store.get("vpp_active", False)
        last_export  = self.store.get("vpp_last_export_kwh", 0.0)
        had_vpp      = self.store.get("had_vpp_today", False) or active

        vpp_log("[VPP] ============ VPP Export Summary ============")
        vpp_log(f"[VPP] Axle enabled:        {'YES' if axle_enabled else 'NO'}")
        vpp_log(f"[VPP] Axle token:          {'configured' if self.axle else 'not set'}")
        vpp_log(f"[VPP] Current state:       {state}")
        vpp_log(f"[VPP] Event active:        {'YES - Axle in control' if active else 'No'}")

        if active:
            ongoing = (self.store["grid_export_daily_kwh"]
                       - self.store.get("vpp_export_start_kwh", 0.0))
            vpp_log(f"[VPP] Export so far:       {ongoing:.2f} kWh  (event in progress)")
        elif had_vpp:
            vpp_log(f"[VPP] Last event export:   {last_export:.2f} kWh")
        else:
            vpp_log("[VPP] Last event export:   No VPP event recorded today")

        dev = self._find_device("axleVppMonitor")
        if dev:
            start = dev.states.get("eventStartTime", "")
            end   = dev.states.get("eventEndTime", "")
            earn  = dev.states.get("estimatedEarnings", "")
            chg   = dev.states.get("preChargeRequired", "0")
            if start:
                vpp_log(f"[VPP] Event window:        {start} - {end}")
                vpp_log(f"[VPP] Est. earnings:       GBP {earn}")
                if float(chg) > 0:
                    vpp_log(f"[VPP] Pre-charge target:   {chg}% SOC")
                else:
                    vpp_log("[VPP] Pre-charge:          Not needed (SOC sufficient)")

        vpp_log("[VPP] =============================================")
        return True

    def menuShowVppEarnings(self):
        """Menu: print the earnings ledger — what Axle has settled, beside ours.

        Deliberately shows BOTH kWh columns. They differ by about 0.4 kWh on a
        clean event, because Axle settles on the change against a baseline
        rather than on raw export, and seeing the pair is the only way to tell
        an ordinary baseline deduction from a window that genuinely went wrong
        (11-Aug over-ran and shows a 3.2 kWh gap).
        """
        s = self._vpp_ledger_summary()

        if s.get("load_error"):
            vpp_log(f"[VPP] Ledger could not be read: {s['load_error']}", level="ERROR")
            return True

        def money(v, unknown="unknown"):
            return unknown if v is None else f"GBP {v:.2f}"

        by_kind = s.get("by_kind") or {}
        vpp_log("[VPP] ============ EARNINGS LEDGER ============")
        vpp_log(f"[VPP] Lifetime earnings:   {money(s.get('lifetime_gbp'))}")
        vpp_log(f"[VPP] Available to draw:   {money(s.get('available_gbp'))}"
            f"{'  (withdrawable now)' if s.get('can_withdraw') else ''}")
        vpp_log(f"[VPP] This month:          {money(s.get('month_to_date_gbp'), 'nothing settled yet')}")
        vpp_log("[VPP] ---- where the money came from ----")
        vpp_log(f"[VPP]   Grid events:       GBP {by_kind.get('events_gbp', 0.0):.2f}")
        vpp_log(f"[VPP]   Monthly top-ups:   GBP {by_kind.get('top_ups_gbp', 0.0):.2f}")
        vpp_log(f"[VPP]   Bonuses/referrals: GBP {by_kind.get('other_gbp', 0.0):.2f}")
        vpp_log(f"[VPP] Events: {s.get('events_settled', 0)} settled, "
            f"{s.get('events_pending', 0)} awaiting settlement")

        # A headline that disagrees with its own rows is worse than no
        # headline, so say which is which rather than quietly picking one.
        behind = s.get("balance_behind_gbp")
        if behind:
            vpp_log(f"[VPP] NB the balance above is Axle's last reported figure and is "
                f"GBP {abs(behind):.2f} {'behind' if behind > 0 else 'ahead of'} the "
                f"transactions listed here (they total "
                f"GBP {s.get('rows_total_gbp', 0.0):.2f}). Import the account page "
                f"to refresh it.", level="WARNING")

        age = s.get("axle_age_days")
        vpp_log(f"[VPP] Axle data imported:  {s.get('axle_fetched_local') or 'never'}"
            f"{f'  ({age:.0f} days old)' if age is not None and age >= 1 else ''}")
        if age is not None and age >= 14:
            vpp_log("[VPP] Ledger is over a fortnight old — re-import from the Axle "
                "account page to pick up newly settled events.", level="WARNING")

        vpp_log("[VPP] ---- recent events (paid / ours) ----")
        for e in s.get("events", []):
            if e["settled"]:
                paid = f"GBP {e['paid_gbp']:.2f} / {e['paid_kwh']:.3f} kWh"
            else:
                # Never "GBP 0.00" — settlement runs days behind the event.
                paid = "awaiting settlement"
            ours = f"{e['our_kwh']:.2f} kWh" if e["our_kwh"] is not None else "not logged"
            diff = f"  diff {e['diff_kwh']:+.3f} kWh" if e["diff_kwh"] is not None else ""
            vpp_log(f"[VPP]   {e['start_local']}-{e['end_local']}  {paid:<28} ours {ours}{diff}")

        next_ev = self._vpp_next_event_info()
        if next_ev:
            hrs = next_ev["seconds_until_start"] / 3600.0
            when = f"in {hrs:.1f}h" if hrs > 0 else "running now"
            vpp_log(f"[VPP] Next event:          {next_ev['start_local']}-{next_ev['end_local']} ({when})")
        else:
            api = self.store.get("vpp_api_error")
            vpp_log(f"[VPP] Next event:          none announced"
                f"{f'  — WARNING, Axle feed is failing: {api}' if api else ''}")
        vpp_log("[VPP] =========================================")
        return True

    def menuShowTodaySummary(self):
        """Menu: Log a human-readable summary of today's energy data."""
        today   = datetime.now().strftime("%d-%b-%Y")
        pv      = self.store.get("pv_daily_kwh", 0.0)
        imp     = self.store.get("grid_import_daily_kwh", 0.0)
        exp     = self.store.get("grid_export_daily_kwh", 0.0)
        home    = self.store.get("home_daily_kwh", 0.0)
        peak    = self.store.get("peak_soc", 0.0)
        low     = self.store.get("min_soc", 100.0)
        vpp_exp = self.store.get("vpp_last_export_kwh", 0.0)
        had_vpp = self.store.get("had_vpp_today", False) or self.store.get("vpp_active", False)

        inv         = self._find_device("sigenergyInverter")
        current_soc = float(inv.states.get("batterySoc", 0)) if inv else 0.0
        ems_mode    = inv.states.get("emsWorkMode", "Unknown") if inv else "Unknown"
        pv_now      = inv.states.get("pvPowerWatts", "0") if inv else "0"
        grid_now    = inv.states.get("gridPowerWatts", "0") if inv else "0"

        mgr      = self._find_device("batteryManager")
        action   = mgr.states.get("currentAction", "Unknown") if mgr else "Unknown"
        reason   = mgr.states.get("currentReason", "") if mgr else ""
        viable   = mgr.states.get("dawnViable", "?") if mgr else "?"
        soc_dawn = mgr.states.get("socAtDawn", "?") if mgr else "?"

        fcast         = self._find_device("solarForecast")
        # correctedTodayKwh is the WHOLE-day bias-corrected forecast.  For the
        # "remaining today" line we need remainingTodayKwh which the forecast
        # module computes as the sum from now -> dusk.  Using
        # correctedTodayKwh here caused a real double-count bug surfaced on
        # 12-May-2026 (87.8 kWh expected total for a 14.25 kWp array — physically
        # impossible).  fcst_today retained for compatibility but not used in
        # the summary line below.
        fcst_today    = fcast.states.get("correctedTodayKwh",  "?") if fcast else "?"  # noqa: F841 — retained for compatibility (see comment above)
        fcst_remain   = fcast.states.get("remainingTodayKwh", "?") if fcast else "?"
        fcst_tmrw     = fcast.states.get("correctedTomorrowKwh", "?") if fcast else "?"

        tariff  = self._find_device("tariffMonitor")
        t_name  = tariff.states.get("tariffActive", "?") if tariff else "?"
        t_rate  = tariff.states.get("rateToday", "") if tariff else ""
        t_tmrw  = tariff.states.get("rateTomorrow", "") if tariff else ""

        import_note = "  (self-sufficient - no grid draw)" if imp < 0.05 else ""
        export_note = f"  (VPP contribution: {vpp_exp:.2f} kWh)" if had_vpp and vpp_exp > 0 else ""
        rate_str    = (f" at {t_rate}p/kWh" if t_rate else "")
        tmrw_str    = (f"  |  tomorrow: {t_tmrw}p" if t_tmrw else "  |  tomorrow: TBD")

        log(f"[Today] ======= Energy Summary: {today} =======")
        try:
            remaining_val  = float(fcst_remain)
            expected_total = round(pv + max(0.0, remaining_val), 1)
            remain_str     = f"{remaining_val:.1f}"
        except (ValueError, TypeError):
            expected_total = "?"
            remain_str     = str(fcst_remain)
        log(f"[Today] Solar generation:    {pv:.2f} kWh  (+{remain_str} kWh remaining = {expected_total} kWh expected total)")
        log(f"[Today] Home consumption:    {home:.2f} kWh")
        log(f"[Today] Grid import:         {imp:.2f} kWh{import_note}")
        log(f"[Today] Grid export:         {exp:.2f} kWh{export_note}")
        log(f"[Today] Battery SOC now:     {current_soc:.0f}%  "
            f"(peak {peak:.0f}%,  low {low:.0f}%)")
        log(f"[Today] EMS mode:            {ems_mode}")
        log(f"[Today] Manager action:      {action}")
        if reason:
            log(f"[Today] Reason:              {reason}")
        log(f"[Today] Dawn viability:      {viable}  |  SOC at dawn: {soc_dawn} kWh")
        log(f"[Today] Tariff:              {t_name}{rate_str}{tmrw_str}")
        log(f"[Today] Tomorrow forecast:   {fcst_tmrw} kWh solar expected")
        log(f"[Today] Live:                PV {pv_now} W  |  Grid {grid_now} W")
        if had_vpp:
            vpp_state = self.store.get("vpp_state", "idle")
            if self.store.get("vpp_active"):
                ongoing = exp - self.store.get("vpp_export_start_kwh", 0.0)
                log(f"[Today] VPP:                 ACTIVE ({ongoing:.2f} kWh exported so far)")
            else:
                log(f"[Today] VPP:                 Completed  ({vpp_exp:.2f} kWh exported)  "
                    f"state: {vpp_state}")
        log("[Today] =============================================")
        return True

    def menuToggleDebug(self):
        """Menu: Toggle debug logging on/off."""
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        state = "ENABLED" if self.debug else "disabled"
        log(f"[Menu] Debug logging {state}")
        return True

    def menuSelfTest(self):
        """Menu: Run a quick health check across every subsystem.

        Logs a single block summarising secrets resolution, Modbus connect,
        Octopus auth, Open-Meteo reachability and Axle auth.  Each line is
        an explicit OK/FAIL so configuration problems surface without log
        digging.  Read-only — no side effects beyond a fresh forecast/rates
        fetch and a Modbus connect check.
        """
        # Estate convention: every diagnostic menu dumps the full plugin-info
        # banner first, so one log block carries environment AND results
        # (same extras as showPluginInfo — one source of truth).
        if log_startup_banner:
            log_startup_banner(
                self.pluginId, self.pluginDisplayName, self.pluginVersion,
                extras=[("Timestamps in Log:", "ON" if self.timestamp_enabled else "OFF")])
        log("=" * 56)
        log("Plugin self-test")
        log("=" * 56)

        # Secrets
        secrets_status = []
        for name, value in (
            ("OCTOPUS_API_KEY",       OCTOPUS_API_KEY),
            ("OCTOPUS_ACCOUNT",       OCTOPUS_ACCOUNT),
            ("OCTOPUS_MPAN",          OCTOPUS_MPAN),
            ("OCTOPUS_SERIAL",        OCTOPUS_SERIAL),
            ("OCTOPUS_EXPORT_MPAN",   OCTOPUS_EXPORT_MPAN),
            ("OCTOPUS_EXPORT_SERIAL", OCTOPUS_EXPORT_SERIAL),
            ("OCTOPUS_GAS_MPRN",      OCTOPUS_GAS_MPRN),
            ("OCTOPUS_GAS_SERIAL",    OCTOPUS_GAS_SERIAL),
            ("AXLE_API_KEY",          AXLE_API_KEY),
            ("PUSHOVER_USER_TOKEN",   PUSHOVER_USER_TOKEN),
            ("SIGENERGY_IP",        SIGENERGY_IP),
            ("LATITUDE",            SITE_LATITUDE),
            ("LONGITUDE",           SITE_LONGITUDE),
        ):
            secrets_status.append(
                f"  {name:<22}: {'SET' if value not in (None, '') else 'missing (using PluginConfig or default)'}"
            )
        log("Secrets (from IndigoSecrets.py):")
        for line in secrets_status:
            log(line)

        # Modbus
        log("Modbus inverter:")
        if not self.modbus:
            log("  FAIL — Modbus not initialised (no inverter IP configured)",
                level="ERROR")
        else:
            try:
                # Under _state_lock: connect() closes and replaces the shared
                # socket, which must not interleave with an in-flight tick read.
                with self._state_lock:
                    ok = bool(self.modbus.connected) or self.modbus.connect()
                if ok and self.modbus.connected:
                    inv = self.latest_inverter_data or {}
                    soc = inv.get("batterySoc", "unknown")
                    log(f"  OK — connected, SOC {soc}%")
                else:
                    log("  FAIL — cannot connect to inverter", level="ERROR")
            except Exception as exc:
                log(f"  FAIL — {exc}", level="ERROR")

        # Octopus
        log("Octopus API:")
        if not self.octopus or not self.octopus.api_key:
            log("  SKIP — no Octopus API key configured")
        else:
            try:
                t = self.octopus.get_current_tariff(force=False)
                if t:
                    log(f"  OK — tariff {t.get('display_name', 'unknown')} "
                        f"({t.get('tariff_code', '')})")
                else:
                    log("  FAIL — could not detect tariff (auth or network)",
                        level="ERROR")
            except Exception as exc:
                log(f"  FAIL — {exc}", level="ERROR")

        # Open-Meteo
        log("Open-Meteo forecast:")
        try:
            data = self.latest_forecast_data
            if data and data.get("correctedTomorrowKwh") is not None:
                log(f"  OK — tomorrow {data.get('correctedTomorrowKwh', 0):.1f} kWh "
                    f"(cache age {data.get('cache_age_hours', 0):.1f}h)")
            else:
                log("  WARN — no forecast cached yet — try Menu > Refresh All Data Now",
                    level="WARNING")
        except Exception as exc:
            log(f"  FAIL — {exc}", level="ERROR")

        # Axle
        log("Axle VPP:")
        if not self.axle:
            log("  SKIP — Axle integration disabled or no API key")
        else:
            try:
                event = self.axle.get_next_event()
                if event:
                    start = _local_time(event["start_time"], "%H:%M %d/%m")
                    log(f"  OK — next event {start} ({event.get('import_export')})")
                else:
                    log("  OK — connected, no event scheduled")
            except Exception as exc:
                log(f"  FAIL — {exc}", level="ERROR")

        # Pushover
        log("Pushover plugin:")
        try:
            po = indigo.server.getPlugin("io.thechad.indigoplugin.pushover")
            if po and po.isEnabled():
                user = self._resolve_pushover_user()
                log(f"  OK — plugin enabled, user token "
                    f"{'set' if user else 'NOT SET'}")
            else:
                log("  WARN — Pushover plugin not enabled (storm/VPP alerts skipped)",
                    level="WARNING")
        except Exception as exc:
            log(f"  WARN — {exc}", level="WARNING")

        log("=" * 56)
        log("Self-test complete.")
        return True

    def menuOpenDashboard(self):
        """Menu: Open the unified Dashboards hub in the default browser.

        Routes to the Dashboards plugin's index.html via IWS rather than the
        legacy internal mini-dashboard on WEB_DASHBOARD_PORT. The internal
        dashboard server still runs (Sigen-only Sankey/charts) but the menu
        now lands on the unified hub, which links to Sigen / Heating / etc.
        Falls back to the legacy URL if INDIGO_URL is not configured.

        Note: this opens the browser on the Indigo SERVER. If the Indigo
        client is running on a different Mac, the dashboard will appear on
        the server's screen, not the client's. The URL (without api-key) is
        also logged so it can be clicked from the event log on any client.
        """
        if INDIGO_URL:
            base = INDIGO_URL.rstrip("/")
            url_log = f"{base}/com.clives.indigoplugin.dashboards/static/pages/index.html"
            api_key = INDIGO_API_KEY or CLAUDEBRIDGE_BEARER_TOKEN
            url_open = f"{url_log}?api-key={api_key}" if api_key else url_log
        else:
            # The internal dashboard binds to loopback unless widened, so ask
            # for the URL that actually works rather than assembling one that
            # looks right and refuses the connection.
            url_open = self._dashboard_url()
            url_log  = url_open.split("?", 1)[0]   # never log the token
        log(f"[Menu] Dashboards: {url_log}")
        try:
            import webbrowser
            opened = webbrowser.open(url_open, new=2)
            if not opened:
                log("[Menu] Could not auto-open browser — open the URL above manually",
                    level="WARNING")
        except Exception as exc:
            log(f"[Menu] Browser launch failed ({exc}) — open the URL above manually",
                level="WARNING")
        return True

    def menuShowDashboardAccess(self):
        """Menu: report where the internal dashboard is listening and how to reach it.

        The token is logged here deliberately. It is the only way to get it onto
        a phone or a tablet, the log is local, and a token nobody can find is a
        dashboard nobody can open.
        """
        bind = self._resolve_dashboard_bind()
        if bind == DASHBOARD_BIND_LOOPBACK:
            log(f"[Web] Dashboard is listening on 127.0.0.1:{WEB_DASHBOARD_PORT} "
                f"— this machine only.")
            log(f"[Web] Open it here: http://127.0.0.1:{WEB_DASHBOARD_PORT}/")
            log("[Web] The Dashboards plugin's energy and cost pages reach it "
                "through a server-side proxy, so they work either way.")
            log("[Web] To reach it from another device, set 'Dashboard access' to "
                "'Whole network' in this plugin's config. A token is already "
                "generated and will be required.")
        else:
            log(f"[Web] Dashboard is listening on ALL interfaces, port "
                f"{WEB_DASHBOARD_PORT}, and requires a token.")
            log(f"[Web] Open it here: {self._dashboard_url()}")
            log("[Web] That link sets a cookie on first use, so the token only "
                "needs pasting once per browser.")
        return True

    def menuShowPowerCutLog(self):
        """Menu: Show the last 20 grid-status transitions from the in-memory log."""
        events = self.store.get("power_cut_events", []) or []
        if not events:
            log("[PowerCut] No grid outages have been observed since plugin start.")
            return True
        log("=" * 56)
        log("Power cut log — last 20 grid-status transitions")
        log("=" * 56)
        for ev in events[-20:]:
            log(f"  {ev}")
        log("=" * 56)
        return True

    # ================================================================
    # Plugin Preferences Callback
    # ================================================================

    def validatePrefsConfigUi(self, values_dict):
        """Enforce the dawnSocTarget >= 15% floor AT SAVE TIME so the user sees
        the constraint in the dialog — previously a saved 10-14% was silently
        bumped back to 15 by a startup migration on the next restart, with the
        XML default (10) contradicting the enforced minimum the whole time."""
        errors = indigo.Dict()
        raw = str(values_dict.get("dawnSocTarget", "")).strip()
        if raw:
            try:
                if float(raw) < 15.0:
                    errors["dawnSocTarget"] = (
                        "Minimum 15% — keeps a real buffer above the battery "
                        "health floor on poor solar days.")
            except ValueError:
                errors["dawnSocTarget"] = "Enter a number (percent, 15-100)."
        # Cheap sanity checks on the most damaging numeric fields — a typo here
        # otherwise only surfaces later as a silent _as_float/_as_int fallback.
        raw = str(values_dict.get("modbusPort", "")).strip()
        if raw:
            try:
                if not (1 <= int(raw) <= 65535):
                    errors["modbusPort"] = "Enter a TCP port between 1 and 65535 (Sigenergy default is 502)."
            except ValueError:
                errors["modbusPort"] = "Enter a whole number (Sigenergy default is 502)."
        raw = str(values_dict.get("inverterMaxKw", "")).strip()
        if raw:
            try:
                if not (0.5 <= float(raw) <= 100.0):
                    errors["inverterMaxKw"] = "Enter the inverter rating in kW (0.5-100, e.g. 10.0)."
            except ValueError:
                errors["inverterMaxKw"] = "Enter a number (kW, e.g. 10.0)."
        raw = str(values_dict.get("batteryCapacityKwh", "")).strip()
        if raw:
            try:
                if not (1.0 <= float(raw) <= 1000.0):
                    errors["batteryCapacityKwh"] = "Enter the battery capacity in kWh (1-1000, e.g. 35.04)."
            except ValueError:
                errors["batteryCapacityKwh"] = "Enter a number (kWh, e.g. 35.04)."
        if errors:
            return (False, values_dict, errors)
        return (True, values_dict)

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if user_cancelled:
            return
        self.debug = values_dict.get("showDebugInfo", False)
        log("[Prefs] Plugin preferences updated - reinitialising modules")
        # Under _state_lock: _init_modules disconnects and REPLACES self.modbus
        # while the background tick may be mid-read on the old client. The RLock
        # means the dialog close simply waits for the current tick to finish.
        with self._state_lock:
            self._init_modules()
            # Re-publish the shared site config so the optimiser script picks up
            # any tariff / battery / inverter changes immediately rather than
            # waiting for the next plugin restart.
            self._write_site_config()
        self._log_bank_first_setting()
        self._log_tariff_override_setting()
        self._log_saving_session_auto_join_setting()
        # Force the next tick to poll Octopus rather than waiting out the remaining
        # hour. Ticking "opt in automatically" at 18:55 for a 19:00 session has to
        # act now, and a settings save is exactly when the owner expects it to.
        self.store["last_saving_sessions"] = 0.0
        # Same reasoning for the Axle account read: ticking the box is the moment
        # the owner expects it to look, and waiting out the rest of a six-hour
        # window would leave a feature that appears to do nothing for most of an
        # evening. Costs one extra read of a page we already read six-hourly.
        self.store["last_account_poll"] = 0.0
        self._log_axle_account_setting()

    def _log_axle_account_setting(self):
        """Say whether the plugin is reading the Axle account directly.

        At every start and every save, for the same reason the tariff override and
        the saving-session auto-join announce themselves: this one reaches OUTSIDE
        the house and reads the owner's mail to find its sign-in link. A capability
        like that being on should never be something you have to open a dialog to
        discover.
        """
        try:
            if not _as_bool(self.pluginPrefs.get("axleReadAccount"), False):
                return
            # event=True: an INFO line that must reach the EVENT log. The v5.96.0
            # rule sends ordinary VPP narration to the plugin's own file because
            # it grows per event and CliveS does not want it; this fires only at a
            # start or a save, and an armed capability that reads the owner's mail
            # is precisely the thing he should not have to go looking for. It is
            # NOT a warning — an armed state working as configured is not a fault.
            vpp_log("[VPP] Reading the Axle account directly is ON - the plugin borrows "
                    "the seven-day sign-in link from Axle's own emails; nothing is stored.",
                    event=True)
        except Exception as exc:
            self.logger.debug(f"[VPP] Axle account setting log skipped: {exc}")

    def _log_saving_session_auto_join_setting(self):
        """Say whether the plugin will opt in on the owner's behalf. Start and every save.

        Auto-join commits his account to something, and it is one-way. An armed
        state that reaches outside the house has to be visible in the log without
        anyone going looking for it — the same reason the tariff override announces
        itself. Silence would leave "it joined that on my behalf" indistinguishable
        from "I must have tapped it in the app and forgotten".
        """
        try:
            if not _as_bool(self.pluginPrefs.get("savingSessionAutoJoin"), False):
                return
            log("[SavingSessions] Automatic opt-in is ON — announced Power Down "
                "sessions will be joined for you. Power Ups and Happy Hours are "
                "never joined automatically.")
        except Exception as exc:                                    # noqa: BLE001
            self.logger.debug(f"[SavingSessions] setting announcement skipped: {exc!r}")

    def _log_tariff_override_setting(self):
        """Say loudly, at startup and on every prefs save, that a tariff is being forced.

        An override that is quietly left on is worse than no override at all: the
        planner would keep making decisions for a tariff the house is not on, and
        nothing in the log would say so. Announcing it on every prefs save and every
        start is what makes the armed state observable.
        """
        try:
            forced = (self.pluginPrefs.get("tariffOverride") or "").strip().lower()
            if forced in ("", "auto", "none"):
                return
            log(f"[Octopus] TARIFF OVERRIDE IS ON — the plugin is planning as though the "
                f"house were on '{forced}'. This is a rehearsal setting: it does not "
                f"change what Octopus bills, and it should be set back to Automatic once "
                f"the real tariff matches. Plugins -> Sigenergy Manager -> Configure.",
                level="WARNING")
        except Exception as exc:
            self.logger.debug(f"[Octopus] override announcement skipped: {exc!r}")

    def _log_bank_first_setting(self):
        """Say what the export hold is set to, at startup and on every prefs save.

        A behaviour change that arrives silently on upgrade is not acceptable; an
        announced one is. It also puts the kill switch in the same line the owner
        reads when he wonders why nothing exported this morning.
        """
        try:
            max_kwh = _as_float(self.pluginPrefs.get("solarOverflowBankFirstMaxKwh"),
                                SOLAR_OVERFLOW_BANK_FIRST_MAX_KWH)
            if max_kwh <= 0.0:
                log("[Manager] Bank-first export hold: OFF (threshold 0) — daytime export "
                    "follows the forecast gate alone.")
                return
            gate = min(SOLAR_OVERFLOW_BANK_FIRST_SOC_MAX,
                       max(0.0, _as_float(self.pluginPrefs.get("solarOverflowBankFirstSoc"),
                                          SOLAR_OVERFLOW_BANK_FIRST_SOC_PCT)))
            clamped = min(max_kwh, SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX)
            log(f"[Manager] Bank-first export hold: ON — daytime export waits for "
                f"{gate:.0f}% SOC on days forecast below {clamped:.1f} kWh "
                f"(set the threshold to 0 to disable).")
            if max_kwh > SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX:
                log(f"[Manager] Bank-first threshold {max_kwh:.1f} kWh is above the "
                    f"{SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX:.0f} kWh clamp and has been "
                    f"capped there.", level="WARNING")
            elif clamped >= 65.0:
                log(f"[Manager] Bank-first threshold {clamped:.1f} kWh is higher than any "
                    f"day this system has ever produced, so export will be held on "
                    f"essentially every day. Intended?", level="WARNING")
        except Exception as exc:
            self.logger.debug(f"[BankFirst] setting announcement skipped: {exc!r}")

    # ================================================================
    # Initialisation Helpers
    # ================================================================

    def _init_modules(self):
        """Initialise all module instances from current preferences."""
        prefs = self.pluginPrefs

        # Resolve credentials: IndigoSecrets.py wins over PluginConfig
        api_key    = OCTOPUS_API_KEY or prefs.get("octopusApiKey", "")
        account_id = OCTOPUS_ACCOUNT or prefs.get("octopusAccount", "")
        mpan       = OCTOPUS_MPAN    or prefs.get("octopusMpan", "")
        serial     = OCTOPUS_SERIAL  or prefs.get("octopusSerial", "")
        region     = prefs.get("octopusRegion", "F")
        # Export MPAN/serial (Octopus Outgoing) — used by v5.19 export-sync check.
        # Optional: if blank, the export-sync feature is silently disabled.
        self.export_mpan   = OCTOPUS_EXPORT_MPAN   or prefs.get("octopusExportMpan", "")
        self.export_serial = OCTOPUS_EXPORT_SERIAL or prefs.get("octopusExportSerial", "")
        self.gas_mprn      = OCTOPUS_GAS_MPRN       or prefs.get("octopusGasMprn", "")
        self.gas_serial    = OCTOPUS_GAS_SERIAL     or prefs.get("octopusGasSerial", "")
        # Gas m3->kWh calorific factor (override on the bill's exact figure).
        try:
            gas_kwh_per_m3 = float(prefs.get("gasKwhPerM3", "") or 0.0)
        except (TypeError, ValueError):
            gas_kwh_per_m3 = 0.0
        if gas_kwh_per_m3 <= 0:
            gas_kwh_per_m3 = GAS_KWH_PER_M3

        axle_key   = AXLE_API_KEY or prefs.get("axleApiKey", "")

        # Solar forecast (Open-Meteo — all 4 arrays, no API key needed).
        # Initialised before Modbus so that downstream callers (startup, etc.)
        # always have a forecast object available even if the Modbus IP is not
        # yet configured.
        # Site coordinates: IndigoSecrets.py first (LATITUDE / LONGITUDE),
        # then PluginConfig (siteLatitude / siteLongitude), then None — there
        # is NO built-in default.  If both are unset the forecast feature is
        # skipped with a clear ERROR.  Array specs remain in source for v5 —
        # a per-array UI is on the v6 roadmap.
        # Uses the module-level _as_float (handles a None fallback correctly).
        site_lat = SITE_LATITUDE  if SITE_LATITUDE  is not None else _as_float(prefs.get("siteLatitude"),  None)
        site_lon = SITE_LONGITUDE if SITE_LONGITUDE is not None else _as_float(prefs.get("siteLongitude"), None)
        # Optional per-array JSON override.  Strict shape check — every entry
        # must declare all 5 required keys (name, tilt, azimuth, kwp, shade)
        # with the right types.  Bad JSON falls back to the module default
        # ARRAYS and is logged at ERROR level so the user notices.
        arrays_override = None
        arrays_raw = (prefs.get("siteArraysJson") or "").strip()
        if arrays_raw:
            try:
                parsed = json.loads(arrays_raw)
                if not isinstance(parsed, list) or not parsed:
                    raise ValueError("expected a non-empty JSON list")
                cleaned = []
                for i, entry in enumerate(parsed):
                    if not isinstance(entry, dict):
                        raise ValueError(f"array #{i+1} is not an object")
                    for key in ("name", "tilt", "azimuth", "kwp", "shade"):
                        if key not in entry:
                            raise ValueError(f"array #{i+1} missing '{key}'")
                    cleaned.append({
                        "name":    str(entry["name"]),
                        "tilt":    float(entry["tilt"]),
                        "azimuth": float(entry["azimuth"]),
                        "kwp":     float(entry["kwp"]),
                        "shade":   float(entry["shade"]),
                    })
                arrays_override = cleaned
                log(f"[Config] Loaded {len(cleaned)} PV array(s) from PluginConfig "
                    f"siteArraysJson override.")
            except (ValueError, TypeError) as exc:
                log(
                    f"[Config] siteArraysJson parse failed ({exc}) — falling back "
                    f"to built-in default ARRAYS. Fix the JSON in PluginConfig.",
                    level="ERROR",
                )
        if site_lat is None or site_lon is None:
            log(
                "[Config] No site coordinates configured. Set LATITUDE / "
                "LONGITUDE in IndigoSecrets.py OR fill in siteLatitude / "
                "siteLongitude under Plugins → Sigenergy Manager → Configure. "
                "Solar forecast feature is disabled until both are set.",
                level="ERROR",
            )
            self.forecast = None
        else:
            self.forecast = OpenMeteoForecast(
                data_dir=self.data_dir,
                logger=self.logger,
                latitude=site_lat,
                longitude=site_lon,
                arrays=arrays_override,
            )

        # Octopus
        self.octopus = OctopusAPI(
            api_key=api_key,
            account_id=account_id,
            mpan=mpan,
            serial=serial,
            region=region,
            data_dir=self.data_dir,
            logger=self.logger,
            gas_mprn=self.gas_mprn,
            gas_serial=self.gas_serial,
            export_mpan=self.export_mpan,
            export_serial=self.export_serial,
            gas_kwh_per_m3=gas_kwh_per_m3,
            gas_unit=(self.pluginPrefs.get("gasMeterUnit") or "m3"),
            tariff_override=(self.pluginPrefs.get("tariffOverride") or ""),
        )

        # Axle VPP. Pass our own logger — AxleAPI's private fallback logger has no
        # handler, so its errors go nowhere (a revoked token hid for six weeks).
        self.axle = AxleAPI(api_token=axle_key, logger=self.logger) if axle_key else None

        # Inverter IP: IndigoSecrets.py wins over PluginConfig.  If neither is set,
        # log an ERROR and skip Modbus init only — the rest of the plugin can
        # still run for development/diagnostic use.
        inv_ip     = SIGENERGY_IP or prefs.get("inverterIp", "")
        if not inv_ip:
            log(
                "[Config] No inverter IP configured. Set SIGENERGY_IP in IndigoSecrets.py "
                "or fill in 'Inverter IP address' under Plugins -> Sigenergy Manager -> "
                "Configure. Modbus connection will not start until this is set.",
                level="ERROR",
            )
            log(
                f"[Init] Modbus=NOT CONFIGURED, "
                f"Octopus={'OK' if api_key else 'not configured'}, "
                f"Solar=Open-Meteo (4 arrays), "
                f"Axle={'OK' if axle_key else 'disabled'}"
            )
            return
        inv_port   = _as_int(prefs.get("modbusPort"), 502)
        # Modbus poll interval — read from pluginPrefs (v5.9), clamped to a
        # safe range. Sigenergy spec allows ~1s; 5s gives plenty of headroom.
        try:
            self.modbus_poll_s = max(5, min(600, int(prefs.get("pollInterval",
                                                               MODBUS_POLL_INTERVAL))))
        except (TypeError, ValueError):
            self.modbus_poll_s = MODBUS_POLL_INTERVAL
        log(f"[Init] Modbus poll interval: {self.modbus_poll_s}s")
        plant_addr = _as_int(prefs.get("plantAddress"), 247)
        inv_addr   = _as_int(prefs.get("inverterSlaveId"), 1)

        # Modbus
        if self.modbus:
            self.modbus.disconnect()
        # Pass self.sleep so the Modbus 1-second throttle between reads can be
        # interrupted by StopThread during plugin shutdown. Without this,
        # read_all() can block the plugin thread for ~16s and Indigo hard-kills
        # the plugin if it doesn't respond to StopThread within ~10s.
        self.modbus = SigenergyModbus(
            ip=inv_ip, port=inv_port,
            plant_address=plant_addr, inverter_address=inv_addr,
            logger=self.logger,
            sleep_func=self.sleep,
            # v5.65.0: the modbus layer now holds the rated power itself, so a mode
            # method can no longer reset the limits to a hardcoded 10000 W — right
            # on this 10 kW inverter, a silent discharge cap on any other. Re-set
            # on every prefs save (closedPrefsConfigUi) so a corrected pref takes
            # effect without a restart.
            inverter_max_w=int(_as_float(prefs.get("inverterMaxKw"), 10.0) * 1000),
        )
        # An executor built against the PREVIOUS driver is now holding a dead
        # socket. Re-bind it to the new one before anything else can use it — a
        # preference change mid-claim would otherwise strand the claim on a
        # disconnected driver, and every write and read-back through it would
        # fail while the claim itself stayed outstanding.
        self._flux_rebind_driver()

        # Startup Modbus initialisations — connect once for all startup writes.
        # HOLD_ESS_MAX_DISCHARGE (40034) persists across mode changes on the inverter.
        # A previous force_discharge() call may have left a low limit that caps battery
        # output even in self-consumption mode. Always reset to full inverter capacity.
        #
        # SKIPPED ENTIRELY WHILE A FLUX CLAIM IS OUTSTANDING. These three writes
        # are the legacy "clear anything stale" pass, and after a crash they are
        # exactly wrong: a Flux charge left the inverter in mode 3 with a charge
        # cutoff at, say, 80%, and that cutoff is the only thing stopping it
        # charging to 100% unattended. Lifting it to 100% and both limits to
        # maximum — before the journal has even been read — removes the backstop
        # from a mode that is still running. Reconciliation happens first, on the
        # first tick; these writes resume on the next restart, by which time
        # there is no claim to protect.
        if self._flux_recovery_pending():
            log("[Flux] A claim journal is outstanding, so the usual startup reset "
                "of the charge and discharge limits has been SKIPPED — lifting the "
                "charge cutoff now would remove the backstop from a mode that may "
                "still be running. The supervisor reconciles on the first tick.",
                level="WARNING")
            self.modbus.connect()
        elif self.modbus.connect():
            inverter_max_w = int(_as_float(prefs.get("inverterMaxKw"), 10.0) * 1000)
            self.modbus.set_discharge_limit(inverter_max_w)   # clear any stale discharge cap
            self.modbus.set_charge_limit(inverter_max_w)      # clear any stale charge cap
            self.modbus.set_charge_cutoff(100.0)              # ensure unrestricted charging
            if prefs.get("exportEnabled", False):
                dno_startup_w = int(_as_float(prefs.get("maxExportKw"), 4.0) * 1000)
                self.modbus.set_export_limit(dno_startup_w)

        log(
            f"[Init] Modbus={inv_ip}:{inv_port}, "
            f"Octopus={'OK' if api_key else 'not configured'}, "
            f"Solar=Open-Meteo (4 arrays), "
            f"Axle={'OK' if axle_key else 'disabled'}"
        )

    def _get_data_dir(self):
        """Return plugin data directory path (create if needed).

        v5.111.4: the install path is CHECKED before anything is created. It used
        to be joined and made unconditionally, so anything other than a real path
        produced a real directory named after it — and `makedirs` is perfectly happy
        to do that.

        Found 20-Sep-2026 inside the live bundle: a tree at
        `Contents/Server Plugin/MagicMock/mock.getInstallFolderPath()/<object id>/
        Preferences/Plugins/com.clives.indigoplugin.sigenergy-energy-manager`, four
        deep, one per test run, dated 12-Sep and still there eight days later. A
        suite had built a Plugin for real against a mocked `indigo`, this method ran,
        and the stringified mock became a folder inside the plugin people install.
        No test reaches `__init__` today, which is why it stopped happening rather
        than because anything prevented it.

        Failing here is strictly better than succeeding into a wrong directory: a
        plugin whose data directory is nonsense loses its accumulators, its logs and
        its VPP ledger silently, and the only sign is that yesterday never happened.
        """
        data_dir = indigo.server.getInstallFolderPath()
        if not isinstance(data_dir, str) or not data_dir.strip():
            raise RuntimeError(
                "indigo.server.getInstallFolderPath() returned "
                f"{type(data_dir).__name__} {data_dir!r}, not a path. Refusing to "
                "create a data directory from it."
            )
        data_dir = os.path.join(data_dir, "Preferences", "Plugins",
                                "com.clives.indigoplugin.sigenergy-energy-manager")
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        return data_dir

    def _find_device(self, type_id):
        """Find the first enabled device of a given typeId."""
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId == type_id and dev.enabled:
                return dev
        return None

    # ================================================================
    # Accumulator Persistence
    # ================================================================

    def _save_accumulators(self):
        """Save daily accumulators to disk (survives plugin reload).

        Best-effort throughout — called from control paths (flood-target and
        lockout changes) that must never fail because a save couldn't run.
        """
        try:
            path = os.path.join(self.data_dir, "accumulators.json")
        except Exception as e:
            self.logger.warning(f"Cannot save accumulators: {e}")
            return
        # v5.45.0: build + write under the lock so the saved snapshot is
        # internally consistent (RLock — locked callers nest fine; the
        # atomic file write is local and fast).
        with self._state_lock:
            self._save_accumulators_locked(path)

    def _save_accumulators_locked(self, path):
        """Build + write the accumulators payload. Caller holds the lock."""
        data = {
            "pv_daily_kwh":              self.store["pv_daily_kwh"],
            "grid_import_daily_kwh":     self.store["grid_import_daily_kwh"],
            "grid_export_daily_kwh":     self.store["grid_export_daily_kwh"],
            "home_daily_kwh":            self.store["home_daily_kwh"],
            "peak_soc":                  self.store["peak_soc"],
            "min_soc":                   self.store["min_soc"],
            "peak_pv_w":                 self.store.get("peak_pv_w", 0),
            "peak_pv_time":              self.store.get("peak_pv_time", ""),
            "today_date":                self.store["today_date"],
            # Daily event flags must survive a same-day restart after dispatch.
            "had_vpp_today":             bool(self.store.get("had_vpp_today", False)),
            "had_import_today":          bool(self.store.get("had_import_today", False)),
            "pv_lifetime_start_kwh":     self.store["pv_lifetime_start_kwh"],
            "import_lifetime_start_kwh": self.store["import_lifetime_start_kwh"],
            "export_lifetime_start_kwh": self.store["export_lifetime_start_kwh"],
            "battery_charge_daily_kwh":    self.store.get("battery_charge_daily_kwh", 0.0),
            "battery_discharge_daily_kwh": self.store.get("battery_discharge_daily_kwh", 0.0),
            "energy_balance_kwh":          self.store.get("energy_balance_kwh", 0.0),
            "energy_day_partial":          bool(self.store.get("energy_day_partial", False)),
            "energy_reconcile_warned":     self.store.get("energy_reconcile_warned", ""),
            "energy_yesterday_projection": self.store.get("energy_yesterday_projection"),
            # v5.90.0: the intraday tracking accumulators (the morning survives a restart)
            "pv_track_date":               self.store.get("pv_track_date", ""),
            "pv_track_actual_kwh":         self.store.get("pv_track_actual_kwh", 0.0),
            "pv_track_forecast_kwh":       self.store.get("pv_track_forecast_kwh", 0.0),
            "pv_track_clipped_min":        self.store.get("pv_track_clipped_min", 0.0),
            "pv_track_last_hour":          self.store.get("pv_track_last_hour"),
            # v5.89.0: the lifetime anchors the daily figures derive from. Carries
            # its own dates, so it is restored whatever day the plugin starts on.
            "daily_energy":                (self.daily_energy.to_dict()
                                            if getattr(self, "daily_energy", None) is not None
                                            else None),
            # Log-only 90% vs 95% daytime pacing counterfactual. Persist it so a
            # restart cannot turn a partial day into an apparently clean day.
            "shadow_95_export_foregone_kwh": self.store.get("shadow_95_export_foregone_kwh", 0.0),
            "shadow_95_samples":             self.store.get("shadow_95_samples", 0),
            # Bank-first day state. Persisted so a restart cannot turn a held morning
            # into an apparently clean day, and so the day's classification survives —
            # otherwise a lunchtime restart re-reads a forecast that has already
            # drifted and can reclassify a day that was settled hours ago.
            "bank_first_small_latched":      self.store.get("bank_first_small_latched", False),
            "bank_first_latch_date":         self.store.get("bank_first_latch_date", ""),
            "bank_first_blocked_samples":    self.store.get("bank_first_blocked_samples", 0),
            "bank_first_withheld_kwh":       self.store.get("bank_first_withheld_kwh", 0.0),
            "bank_first_first_block_local":  self.store.get("bank_first_first_block_local", ""),
            "bank_first_released_local":     self.store.get("bank_first_released_local", ""),
            "bank_first_logged_date":        self.store.get("bank_first_logged_date", ""),
            "bank_first_release_logged":     self.store.get("bank_first_release_logged", False),
            "bank_first_minutes_soc_ge_95":  self.store.get("bank_first_minutes_soc_ge_95", 0),
            "bank_first_minutes_soc_ge_99":  self.store.get("bank_first_minutes_soc_ge_99", 0),
            "bank_first_clip_boundary_min":  self.store.get("bank_first_clip_boundary_min", 0),
            "bank_first_arm_minutes":        self.store.get("bank_first_arm_minutes", 0),
            "bank_first_first_arm_local":    self.store.get("bank_first_first_arm_local", ""),
            "bank_first_peak_surplus_kw":    self.store.get("bank_first_peak_surplus_kw", 0.0),
            # v5.110.2: these four were added to the seed, the reset, the setter AND
            # the restore by v5.106.0, but never here — so every restart lost them and
            # the next classification re-stamped them with the CURRENT time and the
            # CURRENT forecast. That is precisely the fault v5.106.0 was written to
            # remove, reintroduced one dict down. 15-Sep-2026 filed its first
            # classification at 15:50 and 17-Sep at 19:03, both of them simply the
            # first evaluation after a restart.
            "bank_first_first_class_kwh":    self.store.get("bank_first_first_class_kwh"),
            "bank_first_first_class_small":  self.store.get("bank_first_first_class_small"),
            "bank_first_first_class_local":  self.store.get("bank_first_first_class_local", ""),
            "bank_first_promoted_local":     self.store.get("bank_first_promoted_local", ""),
            # v5.111.3, and listed here FIRST for the reason the paragraph above
            # gives: without these two a restart loses the classified forecast and
            # the opening line falls back to printing no figure at all.
            "bank_first_latched_small_kwh":   self.store.get("bank_first_latched_small_kwh"),
            "bank_first_latched_small_local": self.store.get("bank_first_latched_small_local", ""),
            # Storm state is NOT day-specific (a warning can span midnight) — persist it
            # so a restart during an active warning doesn't re-send the Pushover.
            "storm_alerted_level":       self.store.get("storm_alerted_level", "none"),
            "storm_level":               self.store.get("storm_level", "none"),
            # Saving Sessions notified-event ids — not day-specific, persisted so a
            # restart between the announcement and the session can't re-send the push.
            "saving_sessions_notified":  list(self.store.get("saving_sessions_notified") or [])[-200:],
            # Permanently-refused joins, persisted for the same reason: a restart
            # must not turn a settled "Octopus said no" back into an hourly retry.
            "saving_sessions_join_refused":
                list(self.store.get("saving_sessions_join_refused") or [])[-200:],
            "saving_sessions_not_our_region":
                list(self.store.get("saving_sessions_not_our_region") or [])[-200:],
            # WARN-ONCE LATCHES. Persisted for exactly the reason above, and it
            # is not a nicety: an in-memory latch is a warn-once-PER-RESTART
            # latch, which on a plugin restarted several times in a working day
            # is not "once" at all. Measured 12-Sep-2026: the settlement
            # divergence warning had fired 18 times over 6 days, every one about
            # the SAME 11-Aug event — a fault already found and fixed in v5.61.1,
            # about a window a month old that nobody can now do anything about.
            # A warning that cannot be acted on and cannot be cleared only
            # teaches the reader to skim warnings.
            "settlement_warned":
                list(self.store.get("settlement_warned") or [])[-200:],
            "vpp_settled_unpaid_seen":
                list(self.store.get("vpp_settled_unpaid_seen") or [])[-200:],
            "vpp_email_refusal_seen":
                list(self.store.get("vpp_email_refusal_seen") or [])[-200:],
            # Happy Hour: the anchor is the ONLY way the free-kWh figure survives a
            # restart mid-window without double-counting, so it is persisted on entry.
            "happy_hour_import_active":  bool(self.store.get("happy_hour_import_active")),
            "happy_hour_anchor_kwh":     self.store.get("happy_hour_anchor_kwh"),
            "happy_hour_free_kwh":       self.store.get("happy_hour_free_kwh", 0.0),
            # Restart-critical control state. These also live in pluginPrefs,
            # but runtime pref writes only reach .indiPref on a GRACEFUL
            # shutdown — a crash or hard-kill (plausible in exactly the
            # power-cut scenario the lockout exists for) lost both. This file
            # is written on every change, so it is the authoritative copy.
            "flood_prev_target_soc":     self.store.get("flood_prev_target_soc") or 0,
            "power_restored_time":       self.pluginPrefs.get("powerRestoredTime", ""),
            # Outage state is NOT day-specific either — persist it so a plugin
            # restart mid-outage doesn't re-send the 'grid LOST' alert or
            # under-report the RESTORED duration.
            "power_cut_started_at":      (self.store["power_cut_started_at"].isoformat()
                                          if self.store.get("power_cut_started_at") else ""),
            "power_cut_events":          list(self.store.get("power_cut_events", [])),
            # Axle VPP window — restart-critical control state. A window can span
            # midnight, so it is restored regardless of day. Written on every
            # transition (see _vpp_transition) so a restart or crash mid-window
            # resumes WITHOUT relying on Axle still returning the active event —
            # the endpoint may drop it once live (Predbat issue #3051).
            "vpp_state":                 self.store.get("vpp_state", VPP_IDLE),
            "vpp_event":                 (_serialise_vpp_event(self.store.get("vpp_event"))
                                          if self.store.get("vpp_state", VPP_IDLE) != VPP_IDLE
                                          else None),
            "vpp_pre_charge_soc":        self.store.get("vpp_pre_charge_soc", 0.0),
            "vpp_export_start_kwh":      self.store.get("vpp_export_start_kwh", 0.0),
            "vpp_charge_stopped":        self.store.get("vpp_charge_stopped", False),
            "vpp_cutoff_raised":         self.store.get("vpp_cutoff_raised", False),
            "vpp_is_daytime":            self.store.get("vpp_is_daytime", False),
            # The live export mode (0x02 bank / 0x05 / 0x06). Without it, the
            # first verify after a mid-window restart expects the 0x06 default
            # and "corrects" a daytime window's register to it — verify runs
            # BEFORE the act step whose _drive_vpp_export would re-decide, so
            # the spurious write lands first and costs a real mode-switch
            # settle (~26 s of degraded export, measured 15-Jun-2026). 0 =
            # not driving.
            "vpp_export_mode":           self.store.get("vpp_export_mode", 0),
            "vpp_export_active":         (self.store.get("export_active", False)
                                          if self.store.get("vpp_state", VPP_IDLE) != VPP_IDLE
                                          else False),
        }
        try:
            # Atomic — rewritten every 5 minutes; a crash/power blip mid-write
            # must not truncate the file and silently reset the day's totals.
            _atomic_write_json(path, data)
        except Exception as e:
            self.logger.warning(f"Cannot save accumulators: {e}")
        self._save_home_profile()

    def _load_accumulators(self):
        """Load daily accumulators from disk on startup."""
        path = os.path.join(self._get_data_dir(), "accumulators.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Storm state is restored regardless of day (a warning can span midnight),
            # so a restart during an active warning won't re-send the Pushover.
            if data.get("storm_alerted_level"):
                self.store["storm_alerted_level"] = data.get("storm_alerted_level")
            if data.get("storm_level"):
                self.store["storm_level"] = data.get("storm_level")
            if data.get("saving_sessions_notified"):
                self.store["saving_sessions_notified"] = list(data["saving_sessions_notified"])[-200:]
            if data.get("saving_sessions_join_refused"):
                self.store["saving_sessions_join_refused"] = \
                    list(data["saving_sessions_join_refused"])[-200:]
            if data.get("saving_sessions_not_our_region"):
                self.store["saving_sessions_not_our_region"] = \
                    list(data["saving_sessions_not_our_region"])[-200:]
            # The warn-once latches come back as SETS, because that is what the
            # three checks that read them expect; JSON can only carry a list, so
            # the conversion has to happen on the way in. Restored WITHOUT
            # re-warning: a fault already reported once has already been reported.
            for _k in ("settlement_warned", "vpp_settled_unpaid_seen",
                       "vpp_email_refusal_seen"):
                if data.get(_k):
                    self.store[_k] = {str(x) for x in list(data[_k])[-200:]}
            # Restore a Happy Hour that was mid-window when we stopped. The overrun
            # backstop then ends it on the first tick if the window has since closed.
            if data.get("happy_hour_import_active"):
                self.store["happy_hour_import_active"] = True
                self.store["happy_hour_anchor_kwh"] = data.get("happy_hour_anchor_kwh")
            if data.get("happy_hour_free_kwh") is not None:
                self.store["happy_hour_free_kwh"] = data.get("happy_hour_free_kwh", 0.0)
            # Restart-critical control state, restored regardless of day.
            # pluginPrefs copies (written by the same paths) are kept as a
            # fallback for installs upgrading from before v5.43 — startup()'s
            # pref rehydration runs after this and only fills gaps.
            _flood = data.get("flood_prev_target_soc")
            if _flood:
                try:
                    self.store["flood_prev_target_soc"] = float(_flood)
                    self.store["export_active"]         = True
                    self.logger.debug(
                        f"Flood pre-drain target {float(_flood):.0f}% restored "
                        f"from accumulators (crash-safe copy)")
                except (TypeError, ValueError):
                    pass
            # Outage state, restored regardless of day — a restart mid-outage
            # must not re-send the 'grid LOST' alert or reset the duration.
            _pcs = data.get("power_cut_started_at")
            if _pcs:
                try:
                    self.store["power_cut_started_at"] = datetime.fromisoformat(_pcs)
                    self.logger.debug(
                        f"Outage-in-progress marker {_pcs} restored from accumulators")
                except (TypeError, ValueError):
                    pass
            if data.get("power_cut_events"):
                self.store["power_cut_events"] = list(data["power_cut_events"])[-100:]
            # Axle VPP window, restored regardless of day (a window can span
            # midnight). Only the raw fields are restored here — the time-based
            # resume/cleanup decision is deferred to _rehydrate_vpp_state() in
            # startup(), where modbus is available for any hardware cleanup.
            _vpp_state = data.get("vpp_state", VPP_IDLE)
            if _vpp_state and _vpp_state != VPP_IDLE:
                _vpp_event = _deserialise_vpp_event(data.get("vpp_event"))
                if _vpp_event is not None:
                    self.store["vpp_state"]            = _vpp_state
                    self.store["vpp_event"]            = _vpp_event
                    self.store["vpp_active"]           = (_vpp_state == VPP_ACTIVE)
                    self.store["vpp_pre_charge_soc"]   = data.get("vpp_pre_charge_soc", 0.0)
                    self.store["vpp_export_start_kwh"] = data.get("vpp_export_start_kwh", 0.0)
                    self.store["vpp_charge_stopped"]   = bool(data.get("vpp_charge_stopped", False))
                    self.store["vpp_cutoff_raised"]    = bool(data.get("vpp_cutoff_raised", False))
                    self.store["vpp_is_daytime"]       = bool(data.get("vpp_is_daytime", False))
                    # Restore the live export mode so the first verify holds the
                    # window's REAL mode rather than the 0x06 default (see the
                    # save-side comment). Absent (pre-5.57 file, or not
                    # driving) leaves the default in place.
                    try:
                        _mode = int(data.get("vpp_export_mode", 0))
                    except (TypeError, ValueError):
                        _mode = 0
                    if _mode:
                        self.store["vpp_export_mode"] = _mode
                    if data.get("vpp_export_active"):
                        self.store["export_active"] = True
                    self.logger.debug(
                        f"VPP {_vpp_state} window restored from accumulators (crash-safe copy)")
            _prt = data.get("power_restored_time")
            if _prt and not self.pluginPrefs.get("powerRestoredTime", ""):
                # _resolve_export_lockout reads the PREF — reseed it so the
                # 4h lockout survives a non-graceful stop. It self-expires.
                self.pluginPrefs["powerRestoredTime"]  = _prt
                self.store["power_cut_lockout_active"] = True
            # v5.89.0: the anchor object, restored regardless of day — it carries
            # its own dates and prunes what is stale itself.
            _de = data.get("daily_energy")
            if isinstance(_de, dict):
                self.daily_energy = DailyEnergy.from_dict(_de)
            if isinstance(data.get("energy_yesterday_projection"), dict):
                self.store["energy_yesterday_projection"] = data["energy_yesterday_projection"]
            today = _local_today_str()   # Europe/London, matches the save/midnight basis
            if data.get("today_date") == today:
                # Same day — restore accumulators and lifetime anchors
                for event_flag in ("had_vpp_today", "had_import_today"):
                    self.store[event_flag] = bool(data.get(event_flag, False))
                self.store["pv_daily_kwh"]              = data.get("pv_daily_kwh", 0.0)
                self.store["grid_import_daily_kwh"]     = data.get("grid_import_daily_kwh", 0.0)
                self.store["grid_export_daily_kwh"]     = data.get("grid_export_daily_kwh", 0.0)
                self.store["home_daily_kwh"]            = data.get("home_daily_kwh", 0.0)
                self.store["peak_soc"]                  = data.get("peak_soc", 0.0)
                self.store["min_soc"]                   = data.get("min_soc", 100.0)
                self.store["peak_pv_w"]                 = data.get("peak_pv_w", 0)
                self.store["peak_pv_time"]              = data.get("peak_pv_time", "")
                self.store["today_date"]                = today
                # Restore lifetime anchors so delta computation continues correctly
                self.store["pv_lifetime_start_kwh"]     = data.get("pv_lifetime_start_kwh")
                self.store["import_lifetime_start_kwh"] = data.get("import_lifetime_start_kwh")
                self.store["export_lifetime_start_kwh"] = data.get("export_lifetime_start_kwh")
                self.store["shadow_95_export_foregone_kwh"] = data.get("shadow_95_export_foregone_kwh", 0.0)
                self.store["shadow_95_samples"]             = data.get("shadow_95_samples", 0)
                for _ek in ("battery_charge_daily_kwh", "battery_discharge_daily_kwh",
                            "energy_balance_kwh", "energy_day_partial", "energy_reconcile_warned",
                            "pv_track_date", "pv_track_actual_kwh", "pv_track_forecast_kwh",
                            "pv_track_clipped_min", "pv_track_last_hour"):
                    if _ek in data:
                        self.store[_ek] = data[_ek]
                # v5.89.0 upgrade path: a pre-5.89 file has no daily_energy block, but
                # its lifetime anchors ARE today's midnight boundary for three keys.
                # The other three recover from the inverter's own daily counters on
                # the first read (daily_energy.observe, recovery=).
                if not isinstance(_de, dict) and getattr(self, "daily_energy", None) is not None:
                    _seeded = self.daily_energy.migrate_legacy(
                        today, data.get("pv_lifetime_start_kwh"),
                        data.get("import_lifetime_start_kwh"), data.get("export_lifetime_start_kwh"))
                    if _seeded:
                        log(f"[Energy] Midnight anchors for {', '.join(_seeded)} carried over from "
                            f"the pre-5.89 accumulators; house and battery flow recover from the "
                            f"inverter's own daily counters on the first read")
                for _bf_key, _bf_default in (
                    ("bank_first_small_latched",     False),
                    ("bank_first_first_class_kwh",   None),
                    ("bank_first_first_class_small", None),
                    ("bank_first_first_class_local", ""),
                    ("bank_first_promoted_local",    ""),
                    ("bank_first_latched_small_kwh",   None),
                    ("bank_first_latched_small_local", ""),
                    ("bank_first_latch_date",        ""),
                    ("bank_first_blocked_samples",   0),
                    ("bank_first_withheld_kwh",      0.0),
                    ("bank_first_first_block_local", ""),
                    ("bank_first_released_local",    ""),
                    ("bank_first_logged_date",       ""),
                    ("bank_first_release_logged",    False),
                    ("bank_first_minutes_soc_ge_95", 0),
                    ("bank_first_minutes_soc_ge_99", 0),
                    ("bank_first_clip_boundary_min", 0),
                    ("bank_first_arm_minutes",       0),
                    ("bank_first_first_arm_local",   ""),
                    ("bank_first_peak_surplus_kw",   0.0),
                ):
                    self.store[_bf_key] = data.get(_bf_key, _bf_default)
                self.logger.debug("Restored daily accumulators from disk")
        except Exception as e:
            self.logger.warning(f"Cannot load accumulators: {e}")

    # -------------------------------------------------------------------------
    # Menu handlers
    # -------------------------------------------------------------------------

    def showPluginInfo(self, valuesDict=None, typeId=None):
        extras = [("Timestamps in Log:", "ON" if self.timestamp_enabled else "OFF")]
        if log_startup_banner:
            log_startup_banner(self.pluginId, self.pluginDisplayName, self.pluginVersion, extras=extras)
        else:
            indigo.server.log(f"{self.pluginDisplayName} v{self.pluginVersion}")
            for label, value in extras:
                indigo.server.log(f"  {label} {value}")

    def menuToggleTimestamps(self, valuesDict=None, typeId=None):
        self.timestamp_enabled = not self.timestamp_enabled
        self.pluginPrefs["timestampEnabled"] = self.timestamp_enabled
        if self._ts_filter:
            self._ts_filter.enabled = self.timestamp_enabled
        state = "ON" if self.timestamp_enabled else "OFF"
        indigo.server.log(f"[{self.pluginDisplayName}] Timestamps in Log -> {state}")
        return True
