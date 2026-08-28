#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    economics.py
# Description: Whole-house cost settlement and the daily / yesterday / period / calendar
#              economics summaries. Lifted out of plugin.py, and Indigo-free by design.
# Author:      CliveS & Claude Opus 5
# Date:        25-08-2026
# Version:     1.0

"""Money, worked out from the daily history.

Everything here reads `daily_history.json` and the Octopus ledger and returns numbers. It
touches no Indigo API and imports nothing from `plugin`, which is what keeps it testable
without a server and keeps the import graph one-way — `plugin` imports this, never the
reverse.

The clock is injected rather than read. Four of these derive week, month and year cutoffs
from "now", so a caller pinning the clock is the only way to test them without the answers
changing daily and breaking outright at a month boundary.

Written for a plugin that drives a real battery: every method has to survive a history file
that is sparse, partly settled, or absent altogether. Rows gain fields over time, and at the
time of writing 82 of 152 real rows carried settled costs and only 65 carried standing
rates, so an absent field is the normal case rather than an edge one.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone   # noqa: F401  (used by moved code)

import london_time

DEFAULT_EXPORT_RATE_P = 12.0


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


class Economics:
    """Cost settlement and the economics summaries, over a daily-history file.

    Constructed with what it needs rather than reaching for it: the data directory,
    the Octopus client, a logger, and a callable returning the current London-aware
    datetime. The default clock is `london_time.london_now`; the plugin passes a
    late-bound lambda so its own `_london_now` stays the single source of "now".
    """

    COST_SETTLE_WINDOW_DAYS = 14   # settle whole-house cost for the last N days
    COST_SETTLE_MIN_SLOTS   = 46   # require a (near-)complete electricity day

    def __init__(self, data_dir, octopus=None, logger=None, now_fn=None):
        self.data_dir  = data_dir
        self.octopus   = octopus
        self.logger    = logger or logging.getLogger("SigenEnergyManager.Economics")
        self._now_fn   = now_fn or london_time.london_now
        # Cache for the parsed history, invalidated on mtime. Moved with the code
        # that uses it; it was two attributes on the plugin before.
        self._wh_hist_cache = None
        self._wh_hist_mtime = None

    # ---- clock ------------------------------------------------------------
    def _now(self):
        """Current time as an aware Europe/London datetime."""
        return self._now_fn()

    def _today(self):
        """Today's date in Europe/London."""
        return self._now_fn().date()

    # ---- moved verbatim from plugin.Plugin --------------------------------

    @staticmethod
    def _compute_daily_economics(home_kwh, import_kwh, export_kwh,
                                 import_rate_p, export_rate_p):
        """Return the economics dict for a single day's energy figures.

        All four kWh values are non-negative floats.  import_rate_p and
        export_rate_p are pence per kWh (positive).  Either rate being None
        produces a None-result dict so the caller can render "—".
        """
        if import_rate_p is None or export_rate_p is None:
            return {
                "import_rate_p":      import_rate_p,
                "export_rate_p":      export_rate_p,
                "import_cost_gbp":    None,
                "export_revenue_gbp": None,
                "no_solar_cost_gbp":  None,
                "net_today_gbp":      None,
                "solar_benefit_gbp":  None,
            }
        import_cost_p   = import_kwh * import_rate_p
        export_rev_p    = export_kwh * export_rate_p
        no_solar_cost_p = home_kwh   * import_rate_p
        net_p           = export_rev_p - import_cost_p
        benefit_p       = no_solar_cost_p - import_cost_p + export_rev_p
        def _gbp(p):
            return round(p / 100.0, 2)
        return {
            "import_rate_p":      round(float(import_rate_p), 2),
            "export_rate_p":      round(float(export_rate_p), 2),
            "import_cost_gbp":    _gbp(import_cost_p),
            "export_revenue_gbp": _gbp(export_rev_p),
            "no_solar_cost_gbp":  _gbp(no_solar_cost_p),
            "net_today_gbp":      _gbp(net_p),
            "solar_benefit_gbp":  _gbp(benefit_p),
        }

    @staticmethod
    def _row_standing_p(rec, fallback_p):
        """Electricity standing charge in pence for one history row.

        Prefers the value frozen on the day; falls back to the current ledger;
        returns 0.0 when neither is known so a missing rate can only ever
        understate, never invent a charge.
        """
        v = rec.get("elec_standing_p_day")
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
        try:
            return float(fallback_p) if fallback_p is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _wh_build_card(import_kwh, export_kwh, elec_unit_p, export_rate_p,
                       elec_standing_p, gas_kwh, gas_unit_p, gas_standing_p,
                       provisional, gas_estimated):
        """Assemble a whole-house card dict from kWh + rates (None-safe).

        When the electricity unit rate is unknown but import is non-zero the
        bill cannot be known either, so bill/net/covered come back None and the
        page renders "—". Returning 0.00 for the unit cost — as this did — left
        the standing charge as the whole bill, which nearly always beat the
        export revenue and painted a green "Covered" badge over a bill nobody
        had actually worked out.
        """
        rate_missing = (elec_unit_p is None and (import_kwh or 0.0) > 0.0)
        eu = (import_kwh * elec_unit_p / 100.0) if (import_kwh is not None and elec_unit_p is not None) else 0.0
        es = (elec_standing_p / 100.0) if elec_standing_p is not None else 0.0
        gu = (gas_kwh * gas_unit_p / 100.0) if (gas_kwh is not None and gas_unit_p is not None) else 0.0
        gs = (gas_standing_p / 100.0) if gas_standing_p is not None else 0.0
        bill = eu + es + gu + gs
        # `is None` not truthiness — a genuine 0p export rate is a real rate,
        # and the old test silently swapped it for 12p.
        er   = export_rate_p if export_rate_p is not None else DEFAULT_EXPORT_RATE_P
        exp  = (export_kwh or 0.0) * er / 100.0
        return {
            # The kWh these costs were computed FROM, carried alongside them so
            # a consumer cannot pair a live cost with a stale volume.
            "import_kwh":            import_kwh,
            "export_kwh":            export_kwh,
            "gas_kwh":               gas_kwh,
            "electric_unit_gbp":     None if rate_missing else round(eu, 2),
            "electric_standing_gbp": round(es, 2),
            "electric_gbp":          None if rate_missing else round(eu + es, 2),
            "gas_unit_gbp":          round(gu, 2),
            "gas_standing_gbp":      round(gs, 2),
            "gas_gbp":               round(gu + gs, 2),
            "bill_gbp":              None if rate_missing else round(bill, 2),
            "export_gbp":            round(exp, 2),
            "net_gbp":               None if rate_missing else round(exp - bill, 2),
            "covered":               None if rate_missing else bool(exp >= bill),
            "rate_missing":          rate_missing,
            "provisional":           provisional,
            "gas_estimated":         gas_estimated,
        }

    @staticmethod
    def _wh_card_from_row(rec):
        """Build the whole-house card dict from a cost-settled daily_history row."""
        if not rec or not rec.get("cost_settled"):
            return None
        eu = rec.get("elec_unit_cost_gbp") or 0.0
        es = rec.get("elec_standing_gbp")  or 0.0
        gu = rec.get("gas_unit_cost_gbp")  or 0.0
        gs = rec.get("gas_standing_gbp")   or 0.0
        return {
            # The kWh the settle step actually PRICED — import_kwh_octo, not
            # grid_import_kwh.  Publishing any other figure beside these costs
            # would let the two contradict each other, which is the whole
            # reason the kWh are carried on the card at all.
            "import_kwh":            rec.get("import_kwh_octo"),
            "export_kwh":            rec.get("grid_export_kwh"),
            "gas_kwh":               rec.get("gas_kwh"),
            "electric_unit_gbp":     round(eu, 2),
            "electric_standing_gbp": round(es, 2),
            "electric_gbp":          round(eu + es, 2),
            "gas_unit_gbp":          round(gu, 2),
            "gas_standing_gbp":      round(gs, 2),
            "gas_gbp":               round(gu + gs, 2),
            "bill_gbp":              rec.get("whole_house_bill_gbp"),
            "export_gbp":            rec.get("export_revenue_gbp"),
            "net_gbp":               rec.get("wh_net_gbp"),
            "covered":               rec.get("covered"),
            "provisional":           False,
            "gas_estimated":         False,
        }

    @staticmethod
    def _wh_provisional_from_row(rec, elec_standing_p, gas_unit_p,
                                 gas_standing_p, gas_est_kwh, fin_elec_unit_p,
                                 has_gas=True):
        """Provisional card for a not-yet-settled recent day, from the row's
        Sigen-measured import/export (complete at midnight) plus an estimated gas
        figure.  Electric and export are accurate; only gas is an estimate until
        Octopus settles the day, at which point the settled row takes over."""
        def _f(v, d=None):
            try:
                return float(v) if v is not None else d
            except (TypeError, ValueError):
                return d
        imp_kwh       = _f(rec.get("grid_import_kwh"), 0.0)
        exp_kwh       = _f(rec.get("grid_export_kwh"), 0.0)
        elec_unit_p   = _f(rec.get("rate_today_p"), fin_elec_unit_p)
        export_rate_p = _f(rec.get("export_rate_p"), DEFAULT_EXPORT_RATE_P)
        es_p          = _f(rec.get("elec_standing_p_day"), elec_standing_p)
        gu_p          = _f(rec.get("gas_unit_p_day"), gas_unit_p)
        gs_p          = _f(rec.get("gas_standing_p_day"), gas_standing_p)
        return Economics._wh_build_card(imp_kwh, exp_kwh, elec_unit_p, export_rate_p,
                                   es_p, gas_est_kwh, gu_p, gs_p,
                                   provisional=True,
                                   gas_estimated=(gas_est_kwh is not None and has_gas))

    def _current_elec_standing_p(self):
        """Today's electricity standing charge in pence/day, or None.

        Used as the fallback for history rows written before per-day rate
        capture (elec_standing_p_day), which is 87 of the first 121 rows here.
        Mirrors the _day_rate fallback in _settle_whole_house_costs.
        """
        try:
            fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception:
            return None
        if not fin or not fin.get("elec"):
            return None
        try:
            v = fin["elec"].get("standing_p")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _yesterday_economics(self, export_rate_p, fallback_import_rate_p):
        """Read yesterday's totals from daily_history.json and compute economics.

        Returns (economics_dict, date_str) or (None-econ-dict, "").
        Uses the rate_today_p recorded in yesterday's history entry; falls
        back to today's live rate if that field is empty (older entries
        pre-rate-recording).
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            return self._compute_daily_economics(0, 0, 0, None, None), ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return self._compute_daily_economics(0, 0, 0, None, None), ""
        if not records:
            return self._compute_daily_economics(0, 0, 0, None, None), ""
        # Look up YESTERDAY by date, not records[-1] — after a missed-midnight restart
        # (Mac asleep over midnight) the last row may be the day before, mislabelling
        # the "yesterday" card. Fall back to the most recent row if the date is absent.
        today_local = self._today()
        y_str   = (today_local - timedelta(days=1)).strftime("%Y-%m-%d")
        by_date = {r.get("date"): r for r in records if r.get("date")}
        yest = by_date.get(y_str) or records[-1]
        date_str = yest.get("date", "")
        rate_p   = None
        try:
            r = float(yest.get("rate_today_p") or 0.0)
            if r > 0:
                rate_p = r
        except (TypeError, ValueError):
            pass
        if rate_p is None:
            rate_p = fallback_import_rate_p   # may also be None
        # Use yesterday's own saved export rate if present (v5.7+);
        # fall back to today's live export rate for older records.
        yest_export_p = export_rate_p
        try:
            er = float(yest.get("export_rate_p") or 0.0)
            if er > 0:
                yest_export_p = er
        except (TypeError, ValueError):
            pass
        return (
            self._compute_daily_economics(
                home_kwh      = float(yest.get("home_kwh",       0.0) or 0.0),
                import_kwh    = float(yest.get("grid_import_kwh", 0.0) or 0.0),
                export_kwh    = float(yest.get("grid_export_kwh", 0.0) or 0.0),
                import_rate_p = rate_p,
                export_rate_p = yest_export_p,
            ),
            date_str,
        )

    def _whole_house_summary(self, import_rate_p, export_rate_p,
                             today_import_kwh=0.0, today_export_kwh=0.0):
        """Whole-house cost block for /api/status.

        Returns today (provisional), yesterday (settled where available),
        month-to-date net, days self-funded, account balance and the 30-day
        bill-vs-export series.  Fields are None when data isn't available yet.
        """
        out = {
            "today": None, "yesterday": None, "yesterday_date": "",
            "day_before": None, "day_before_date": "",
            "month": None, "self_funded": None, "balance_gbp": None,
            "series30": [],
        }
        try:
            fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception:
            fin = None

        elec_standing_p = gas_unit_p = gas_standing_p = None
        fin_elec_unit_p = None
        if fin:
            out["balance_gbp"] = fin.get("balance_gbp")
            if fin.get("elec"):
                elec_standing_p = fin["elec"].get("standing_p")
                fin_elec_unit_p = fin["elec"].get("unit_p")
            if fin.get("gas"):
                gas_unit_p     = fin["gas"].get("unit_p")
                gas_standing_p = fin["gas"].get("standing_p")

        # /api/status hits this every ~5s but daily_history.json only changes at
        # midnight / on a settle, so cache the parse keyed by the file's mtime.
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        if (getattr(self, "_wh_hist_mtime", None) == mtime
                and getattr(self, "_wh_hist_cache", None) is not None):
            records = self._wh_hist_cache
        else:
            records = []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    records = json.load(f)
            except (OSError, ValueError):
                records = []
            self._wh_hist_cache = records
            self._wh_hist_mtime = mtime
        by_date  = {r.get("date"): r for r in records if r.get("date")}
        settled  = [r for r in records if r.get("cost_settled")]

        now_local  = self._now()
        today      = now_local.date()
        month_pref = today.strftime("%Y-%m")

        # ---- Gas estimate: most recent settled gas_kwh within the last 7 days,
        # used for any day whose gas hasn't settled yet (today + recent days). ----
        gas_est_kwh = None
        gas_cutoff = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        for r in sorted(settled, key=lambda x: x.get("date", ""), reverse=True):
            if r.get("date", "") < gas_cutoff:
                break
            if r.get("gas_kwh") is not None:
                try:
                    gas_est_kwh = float(r["gas_kwh"])
                    break
                except (TypeError, ValueError):
                    continue

        # Resolved before _day_card so the yesterday / day-before cards apply the
        # same gas test the today card does — without it an electricity-only user
        # got an "(est)" tag on a £0.00 gas line.
        has_gas = bool(self.octopus and self.octopus.gas_mprn and self.octopus.gas_serial)

        def _day_card(date_str):
            """Settled row if frozen, else a provisional card from the row's
            Sigen-measured import/export (complete at midnight) + estimated gas —
            so yesterday shows accurate electric + export while gas still settles."""
            rec = by_date.get(date_str)
            settled_card = self._wh_card_from_row(rec)
            if settled_card:
                return settled_card
            if rec is None:
                return None
            return self._wh_provisional_from_row(
                rec, elec_standing_p, gas_unit_p, gas_standing_p,
                gas_est_kwh, fin_elec_unit_p, has_gas=has_gas)

        # ---- Yesterday + day before ----
        y_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        out["yesterday_date"] = y_str
        out["yesterday"]      = _day_card(y_str)
        d2_str = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        out["day_before_date"] = d2_str
        out["day_before"]      = _day_card(d2_str)

        # ---- Today (provisional, from live Sigen totals) ----
        # Gas rates are only required when a gas meter exists — an electricity-only
        # user still gets a today card (the None-safe card treats gas cost as 0).
        has_gas = bool(self.octopus and self.octopus.gas_mprn and self.octopus.gas_serial)
        elec_unit_p_today = import_rate_p if import_rate_p is not None else fin_elec_unit_p
        if elec_standing_p is not None and (not has_gas or
                (gas_standing_p is not None and gas_unit_p is not None)):
            try:
                imp_kwh = float(today_import_kwh or 0.0)
                exp_kwh = float(today_export_kwh or 0.0)
            except (TypeError, ValueError):
                imp_kwh = exp_kwh = 0.0
            out["today"] = self._wh_build_card(
                imp_kwh, exp_kwh, elec_unit_p_today, export_rate_p,
                elec_standing_p, gas_est_kwh, gas_unit_p, gas_standing_p,
                provisional=True, gas_estimated=(gas_est_kwh is not None and has_gas))

        # ---- Month to date (settled rows this month) ----
        m_rows = [r for r in settled if (r.get("month") == month_pref
                                         or str(r.get("date", "")).startswith(month_pref))]
        if m_rows:
            bill_sum = sum(float(r.get("whole_house_bill_gbp") or 0.0) for r in m_rows)
            exp_sum  = sum(float(r.get("export_revenue_gbp")   or 0.0) for r in m_rows)
            covered_days = sum(1 for r in m_rows if r.get("covered"))
            def _kwh_sum(key):
                total = 0.0
                for r in m_rows:
                    try:
                        total += float(r.get(key) or 0.0)
                    except (TypeError, ValueError):
                        continue
                return round(total, 3)

            out["month"] = {
                "bill_gbp":   round(bill_sum, 2),
                "export_gbp": round(exp_sum, 2),
                "net_gbp":    round(exp_sum - bill_sum, 2),
                "in_credit":  bool(exp_sum >= bill_sum),
                "days":       len(m_rows),
                # Same rows, same keys the settle step priced — so the month's
                # kWh and its £ are always the same set of days.
                "import_kwh": _kwh_sum("import_kwh_octo"),
                "export_kwh": _kwh_sum("grid_export_kwh"),
                "gas_kwh":    _kwh_sum("gas_kwh"),
            }
            out["self_funded"] = {
                "covered_days": covered_days,
                "settled_days": len(m_rows),
            }

        # ---- 30-day bill-vs-export series ----
        recent = sorted(settled, key=lambda x: x.get("date", ""))[-30:]
        out["series30"] = [
            {
                "date":   r.get("date"),
                "bill":   round(float(r.get("whole_house_bill_gbp") or 0.0), 2),
                "export": round(float(r.get("export_revenue_gbp")   or 0.0), 2),
            }
            for r in recent
        ]
        return out

    def _period_economics_summary(self, export_rate_p, fallback_import_rate_p):
        """Roll up weekly / monthly / yearly economics from daily_history.json.

        Each window returns:
          {"days": N, "import_total_gbp", "export_total_gbp",
           "no_solar_total_gbp", "net_total_gbp", "benefit_total_gbp",
           "benefit_avg_gbp", "no_solar_avg_gbp", "net_avg_gbp",
           "import_avg_gbp", "export_avg_gbp"}

        Each daily record uses its own saved `rate_today_p` (the import rate
        on that day) — Tracker rates change daily so historical days mustn't
        all be valued at today's rate.  Export rate is assumed flat 12p
        across history (Octopus Outgoing 12p has been live since 26-Mar-2026,
        which is when this system started exporting).
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            records = []
        today = self._today()

        fallback_standing_p = self._current_elec_standing_p()

        def aggregate(subset):
            if not subset:
                return {
                    "days": 0,
                    "import_total_gbp":   None, "export_total_gbp":   None,
                    "no_solar_total_gbp": None, "net_total_gbp":      None,
                    "benefit_total_gbp":  None,
                    "elec_whole_house_total_gbp": None,
                    "elec_whole_house_avg_gbp":   None,
                    "import_avg_gbp":     None, "export_avg_gbp":     None,
                    "no_solar_avg_gbp":   None, "net_avg_gbp":        None,
                    "benefit_avg_gbp":    None,
                }
            total_imp_p = total_exp_p = 0.0
            total_no_solar_p = total_net_p = total_benefit_p = 0.0
            total_elec_wh_gbp = 0.0   # whole-house electric (unit + standing)
            # kWh over the SAME rows the money is computed from — a day
            # skipped for want of a rate must not contribute volume either.
            total_imp_kwh = total_exp_kwh = total_gas_kwh = 0.0
            counted = 0
            for r in subset:
                home  = float(r.get("home_kwh",        0) or 0)
                imp_k = float(r.get("grid_import_kwh", 0) or 0)
                exp_k = float(r.get("grid_export_kwh", 0) or 0)
                try:
                    ir = float(r.get("rate_today_p") or 0)
                except (TypeError, ValueError):
                    ir = 0
                if ir <= 0 and fallback_import_rate_p is not None:
                    ir = fallback_import_rate_p
                if ir <= 0:
                    continue   # skip days with no rate info
                # Prefer per-record export rate (saved from v5.7 onwards);
                # older records fall back to the live/current export rate.
                try:
                    er = float(r.get("export_rate_p") or 0)
                except (TypeError, ValueError):
                    er = 0
                if er <= 0:
                    er = export_rate_p
                # The day's standing charge, needed twice below: a grid-only
                # home pays exactly the same one, and the whole-house electric
                # column claims to include it.
                st_p = self._row_standing_p(r, fallback_standing_p)

                imp_p       = imp_k * ir
                exp_p       = exp_k * er
                no_sol_unit = home  * ir
                net         = exp_p - imp_p
                # Standing cancels out of the benefit — (no_sol + st) - (imp + st)
                # + exp — so it is computed from the unit-only figures and does
                # NOT change when the standing charge is reported below.
                benefit     = no_sol_unit - imp_p + exp_p
                total_imp_p      += imp_p
                total_exp_p      += exp_p
                # Grid-only counterfactual carries the standing charge: the same
                # meter, the same daily charge, just no solar behind it. Without
                # it the column sat ~£0.62/day under the truth while the elec
                # bill beside it included the charge.
                total_no_solar_p += no_sol_unit + st_p
                total_net_p      += net
                total_benefit_p  += benefit
                # Whole-house electric, ALWAYS unit + standing so the column
                # matches its header and the row reads as an identity:
                #   solar benefit = grid-only - elec bill + export earned
                # Settled rows carry frozen bill-exact figures; unsettled rows
                # (Octopus settles ~a day in arrears, so a 7-day window nearly
                # always holds one) previously contributed unit only.
                if r.get("elec_unit_cost_gbp") is not None:
                    total_elec_wh_gbp += (float(r.get("elec_unit_cost_gbp") or 0)
                                          + float(r.get("elec_standing_gbp") or 0))
                else:
                    total_elec_wh_gbp += (imp_p + st_p) / 100.0
                total_imp_kwh += imp_k
                total_exp_kwh += exp_k
                try:
                    total_gas_kwh += float(r.get("gas_kwh") or 0.0)
                except (TypeError, ValueError):
                    pass
                counted += 1
            if counted == 0:
                return aggregate([])   # all rate-less, treat as empty
            def _g(p):  return round(p / 100.0,           2)
            def _ga(p): return round(p / 100.0 / counted, 2)
            return {
                "days":                counted,
                "import_total_gbp":    _g(total_imp_p),
                "export_total_gbp":    _g(total_exp_p),
                "no_solar_total_gbp":  _g(total_no_solar_p),
                "net_total_gbp":       _g(total_net_p),
                "benefit_total_gbp":   _g(total_benefit_p),
                "elec_whole_house_total_gbp": round(total_elec_wh_gbp, 2),
                "elec_whole_house_avg_gbp":   round(total_elec_wh_gbp / counted, 2),
                "import_avg_gbp":      _ga(total_imp_p),
                "export_avg_gbp":      _ga(total_exp_p),
                "no_solar_avg_gbp":    _ga(total_no_solar_p),
                "net_avg_gbp":         _ga(total_net_p),
                "benefit_avg_gbp":     _ga(total_benefit_p),
                # kWh over the same counted rows, so a consumer can never
                # pair this window's money with a different set of days.
                "import_kwh":          round(total_imp_kwh, 3),
                "export_kwh":          round(total_exp_kwh, 3),
                "gas_kwh":             round(total_gas_kwh, 3),
            }

        # Window selectors
        wk_cutoff   = (today - timedelta(days=7)).isoformat()
        yr_cutoff   = (today - timedelta(days=365)).isoformat()
        month_pref  = today.strftime("%Y-%m")
        week_recs   = [r for r in records if (r.get("date") or "") >= wk_cutoff]
        month_recs  = [r for r in records if (r.get("date") or "").startswith(month_pref)]
        year_recs   = [r for r in records if (r.get("date") or "") >= yr_cutoff]

        return {
            "week":  aggregate(week_recs),
            "month": aggregate(month_recs),
            "year":  aggregate(year_recs),
        }

    def _calendar_months_summary(self, export_rate_p, fallback_import_rate_p,
                                  year=None):
        """Per-month economics for Jan-Dec of `year` (default: current year).

        Returns a list of 12 dicts in calendar order, each containing the
        same shape as _period_economics_summary's aggregate (days, totals,
        averages).  Months with no records report days=0 and None values
        so the dashboard can render "—".
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            records = []
        now_local = self._now()
        if year is None:
            year = now_local.year

        # Group records by calendar month string ("YYYY-MM")
        by_month = {f"{year:04d}-{m:02d}": [] for m in range(1, 13)}
        for r in records:
            d = r.get("date") or ""
            m_key = d[:7]
            if m_key in by_month:
                by_month[m_key].append(r)

        fallback_standing_p = self._current_elec_standing_p()

        # Reuse the aggregate maths from the period summary
        def aggregate(subset):
            if not subset:
                return {
                    "days": 0,
                    "import_total_gbp":   None, "export_total_gbp":   None,
                    "no_solar_total_gbp": None, "net_total_gbp":      None,
                    "benefit_total_gbp":  None,
                    "elec_whole_house_total_gbp": None,
                    "elec_whole_house_avg_gbp":   None,
                    "import_avg_gbp":     None, "export_avg_gbp":     None,
                    "no_solar_avg_gbp":   None, "net_avg_gbp":        None,
                    "benefit_avg_gbp":    None,
                }
            total_imp_p = total_exp_p = 0.0
            total_no_solar_p = total_net_p = total_benefit_p = 0.0
            total_elec_wh_gbp = 0.0   # whole-house electric (unit + standing)
            counted = 0
            for r in subset:
                home  = float(r.get("home_kwh",        0) or 0)
                imp_k = float(r.get("grid_import_kwh", 0) or 0)
                exp_k = float(r.get("grid_export_kwh", 0) or 0)
                try:
                    ir = float(r.get("rate_today_p") or 0)
                except (TypeError, ValueError):
                    ir = 0
                if ir <= 0 and fallback_import_rate_p is not None:
                    ir = fallback_import_rate_p
                if ir <= 0:
                    continue
                # Prefer per-record export rate (saved from v5.7 onwards);
                # older records fall back to the live/current export rate —
                # matches _period_economics_summary so history isn't re-valued
                # at today's rate.
                try:
                    er = float(r.get("export_rate_p") or 0)
                except (TypeError, ValueError):
                    er = 0
                if er <= 0:
                    er = export_rate_p
                # Same basis as _period_economics_summary — see the comments
                # there for why standing sits in grid-only and in the elec bill
                # but never in the benefit.
                st_p = self._row_standing_p(r, fallback_standing_p)

                imp_p       = imp_k * ir
                exp_p       = exp_k * er
                no_sol_unit = home  * ir
                net         = exp_p - imp_p
                benefit     = no_sol_unit - imp_p + exp_p
                total_imp_p      += imp_p
                total_exp_p      += exp_p
                total_no_solar_p += no_sol_unit + st_p
                total_net_p      += net
                total_benefit_p  += benefit
                if r.get("elec_unit_cost_gbp") is not None:
                    total_elec_wh_gbp += (float(r.get("elec_unit_cost_gbp") or 0)
                                          + float(r.get("elec_standing_gbp") or 0))
                else:
                    total_elec_wh_gbp += (imp_p + st_p) / 100.0
                counted += 1
            if counted == 0:
                return aggregate([])
            def _g(p):  return round(p / 100.0,           2)
            def _ga(p): return round(p / 100.0 / counted, 2)
            return {
                "days":                counted,
                "import_total_gbp":    _g(total_imp_p),
                "export_total_gbp":    _g(total_exp_p),
                "no_solar_total_gbp":  _g(total_no_solar_p),
                "net_total_gbp":       _g(total_net_p),
                "benefit_total_gbp":   _g(total_benefit_p),
                "elec_whole_house_total_gbp": round(total_elec_wh_gbp, 2),
                "elec_whole_house_avg_gbp":   round(total_elec_wh_gbp / counted, 2),
                "import_avg_gbp":      _ga(total_imp_p),
                "export_avg_gbp":      _ga(total_exp_p),
                "no_solar_avg_gbp":    _ga(total_no_solar_p),
                "net_avg_gbp":         _ga(total_net_p),
                "benefit_avg_gbp":     _ga(total_benefit_p),
            }

        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        result = []
        for m in range(1, 13):
            key = f"{year:04d}-{m:02d}"
            entry = aggregate(by_month[key])
            entry["month_key"] = key
            entry["month_name"] = month_names[m - 1]
            entry["partial"]   = (m == now_local.month and year == now_local.year)
            result.append(entry)
        return {"year": year, "months": result}

    def _settle_whole_house_costs(self):
        """Backfill settled whole-house cost fields into daily_history.json rows.

        For each recent day not yet cost-settled, fetch Octopus settled grid-import
        and gas consumption, value them at the rate in force on that day (the saved
        rate_today_p for elec unit, and the standing + gas rates saved on the day
        in daily_history, falling back to the current Kraken ledger only for older
        rows), compute the whole-house bill, and freeze the row.  Gas is the
        gating signal — a day only settles once Octopus has its gas data.  Export
        revenue uses the Sigen-measured daily export (final at midnight, ~0.02%
        accurate) so it needs no settlement wait.
        """
        if not self.octopus:
            return
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return
        if not records:
            return

        try:
            fin = self.octopus.get_account_financials()
        except Exception as exc:
            self.logger.debug(f"[CostSettle] financials fetch failed: {exc}")
            return
        has_gas_meter = bool(self.octopus.gas_mprn and self.octopus.gas_serial)
        if not fin or not fin.get("elec") or (has_gas_meter and not fin.get("gas")):
            self.logger.debug("[CostSettle] No ledger rates yet — skipping this cycle")
            return
        elec_standing_p = fin["elec"].get("standing_p")
        fin_elec_unit_p = fin["elec"].get("unit_p")
        gas_unit_p      = (fin.get("gas") or {}).get("unit_p")     if has_gas_meter else 0.0
        gas_standing_p  = (fin.get("gas") or {}).get("standing_p") if has_gas_meter else 0.0
        fin_export_p    = (fin.get("export") or {}).get("unit_p")
        # Electricity rates are always required; gas rates only when a gas meter exists.
        if elec_standing_p is None or (has_gas_meter and None in (gas_unit_p, gas_standing_p)):
            self.logger.debug("[CostSettle] Ledger missing standing/gas rates — skipping")
            return

        by_date = {r.get("date"): r for r in records if r.get("date")}
        today   = self._today()

        settled_n = 0
        for offset in range(1, self.COST_SETTLE_WINDOW_DAYS + 1):
            date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            rec = by_date.get(date_str)
            if rec is None or rec.get("cost_settled"):
                continue
            try:
                imp = self.octopus.get_import_kwh_for_date(date_str)
                gas = self.octopus.get_gas_kwh_for_date(date_str)
            except Exception as exc:
                self.logger.debug(f"[CostSettle] consumption fetch error {date_str}: {exc}")
                continue
            # Import (electricity) must be present and is the DAY-COMPLETENESS signal:
            # a smart electricity meter always reports half-hourly, so 46+ of 48 slots
            # means the day is whole. Octopus settles ~a day in arrears, so the most
            # recent day often has only the first hour or two — freezing that would lock
            # in a near-zero bill permanently (cost_settled). Wait until the day is whole.
            if not imp or imp.get("kwh") is None:
                continue
            if imp.get("slots", 0) < self.COST_SETTLE_MIN_SLOTS:
                continue

            # Gas: gate on FULL-DAY COVERAGE, not a 46-slot half-hourly count.
            # Many SMETS1 / daily-read gas meters report ONE reading per day (slots=1),
            # which can never reach 46 — gating on slots strands those users on an
            # estimate forever. But presence alone is NOT enough either: gas settles
            # slower than electricity and can arrive PARTIALLY — on 03-07-2026 the
            # 1 Jul row froze at 0.034 kWh (£0.00) off a single 00:00-00:30 slot.
            # The `complete` flag (readings reach the end of the local day) is true
            # for a whole half-hourly day AND for a daily meter's single 24h reading,
            # but false for a partial day. A user with no gas meter settles on
            # electricity alone.
            if has_gas_meter:
                if not gas or gas.get("kwh") is None:
                    continue   # gas meter configured but not settled yet — wait
                if not gas.get("complete", False):
                    self.logger.debug(
                        f"[CostSettle] gas for {date_str} only partially settled "
                        f"({gas.get('slots', 0)} slot(s), coverage short of day end) — waiting")
                    continue
                try:
                    gas_kwh = float(gas["kwh"])
                except (TypeError, ValueError):
                    self.logger.debug(f"[CostSettle] non-numeric gas kWh for {date_str}; skipping")
                    continue
                gas_m3 = gas.get("m3")
            else:
                gas_kwh = 0.0
                gas_m3  = None

            # Elec unit rate that applied on this day (saved at midnight); fall
            # back to the current ledger rate only if the row never captured one.
            try:
                elec_unit_p = float(rec.get("rate_today_p"))
            except (TypeError, ValueError):
                elec_unit_p = fin_elec_unit_p
            if elec_unit_p is None:
                continue
            try:
                export_rate_p = float(rec.get("export_rate_p"))
            except (TypeError, ValueError):
                export_rate_p = fin_export_p if fin_export_p is not None else DEFAULT_EXPORT_RATE_P

            try:
                import_kwh = float(imp["kwh"])
            except (TypeError, ValueError):
                self.logger.debug(f"[CostSettle] non-numeric import kWh for {date_str}; skipping")
                continue
            try:
                export_kwh = float(rec.get("grid_export_kwh", 0.0))
            except (TypeError, ValueError):
                export_kwh = 0.0

            # Standing charges + gas unit rate: prefer the value saved on the
            # day (tariff-change-proof); fall back to the current ledger only for
            # older / backfilled rows that predate per-day capture.
            def _day_rate(key, fallback):
                v = rec.get(key)
                try:
                    return float(v) if v is not None else fallback
                except (TypeError, ValueError):
                    return fallback
            day_elec_standing_p = _day_rate("elec_standing_p_day", elec_standing_p)
            day_gas_unit_p      = _day_rate("gas_unit_p_day",      gas_unit_p)
            day_gas_standing_p  = _day_rate("gas_standing_p_day",  gas_standing_p)

            elec_unit_cost = import_kwh * elec_unit_p / 100.0
            elec_standing  = day_elec_standing_p / 100.0
            gas_unit_cost  = gas_kwh * day_gas_unit_p / 100.0
            gas_standing   = day_gas_standing_p / 100.0
            bill           = elec_unit_cost + elec_standing + gas_unit_cost + gas_standing
            export_rev     = export_kwh * export_rate_p / 100.0
            net            = export_rev - bill

            rec.update({
                "import_kwh_octo":      round(import_kwh, 3),
                "gas_m3":               round(gas_m3, 3) if gas_m3 is not None else None,
                "gas_kwh":              round(gas_kwh, 3),
                "elec_unit_cost_gbp":   round(elec_unit_cost, 2),
                "elec_standing_gbp":    round(elec_standing, 2),
                "gas_unit_cost_gbp":    round(gas_unit_cost, 2),
                "gas_standing_gbp":     round(gas_standing, 2),
                "whole_house_bill_gbp": round(bill, 2),
                "export_revenue_gbp":   round(export_rev, 2),
                "wh_net_gbp":           round(net, 2),
                "covered":              bool(export_rev >= bill),
                "cost_settled":         True,
            })
            settled_n += 1

        if settled_n:
            try:
                _atomic_write_json(path, records)
                self.logger.info(f"[CostSettle] Settled whole-house cost for {settled_n} day(s)")
            except Exception as e:
                self.logger.error(f"[CostSettle] Cannot write daily history: {e}")
