#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    forecast_blend.py
# Description: Calibrate and blend several weather models' solar forecasts. Pure
#              functions with no network or Indigo, so openmeteo_forecast.py owns
#              the fetching and this owns the sums.
# Author:      CliveS & Claude Opus 5.5
# Date:        30-10-2026
# Version:     1.0
#
# WHY THREE MODELS, EACH SCALED ON ITS OWN RECORD
#
# Measured 30-Oct-2026 over 158 days (24 Apr to 29 Oct), day-ahead, against actual
# generation, each model corrected only from the days before it:
#     the plugin as it ran (Met Office + bias bands)   5.41 kWh a day average error
#     ECMWF AIFS alone, scaled                         4.42
#     AIFS + ECMWF IFS + Met Office, each scaled       4.17  (95% range of the gain
#                                                           0.56 to 1.94 kWh a day)
# Uncorrected, both ECMWF models read about 7 to 8 kWh a day too high, so no ECMWF
# figure may reach the blend until it has its own scale: that is why
# calibration_factor() returns None rather than 1.0 on too few days. ICON, GFS, KNMI,
# DMI and Meteo-France did not help.
#
# Not measured: a winter. The rolling window is what follows the season.

BLEND_MODELS     = ("ukmo_seamless", "ecmwf_ifs025", "ecmwf_aifs025_single")
CAL_WINDOW_DAYS  = 45     # days of a model's record that set its scale
MIN_CAL_DAYS     = 10     # fewer than this and the model stays out of the blend
CAL_FACTOR_MIN   = 0.5
CAL_FACTOR_MAX   = 1.5
MIN_BLEND_MODELS = 2      # one model is not a blend: fall back to the bias bands
MIN_DAY_HOURS    = 23     # the clocks-forward day has 23
RECORDS_KEPT     = 120


def _usable(rec, model):
    """True when a day's record can grade this model."""
    try:
        return (float(rec.get("actual_kwh") or 0.0) > 0.0
                and float((rec.get("models") or {}).get(model) or 0.0) > 0.0)
    except (TypeError, ValueError):
        return False


def calibration_factor(records, model):
    """actual / forecast over the model's last CAL_WINDOW_DAYS, or None if too few.

    A ratio of sums, not a mean of ratios: per-day ratios are bounded below and
    not above, so their mean leans high. None, not 1.0, keeps an uncalibrated
    model out of the blend.
    """
    rows = sorted((r for r in records if _usable(r, model)),
                  key=lambda r: str(r.get("date")))[-CAL_WINDOW_DAYS:]
    if len(rows) < MIN_CAL_DAYS:
        return None
    actual   = sum(float(r["actual_kwh"]) for r in rows)
    forecast = sum(float(r["models"][model]) for r in rows)
    return max(CAL_FACTOR_MIN, min(CAL_FACTOR_MAX, actual / forecast))


def calibration_factors(records, models):
    """{model: factor} for the models that have earned one."""
    factors = {}
    for model in models:
        factor = calibration_factor(records, model)
        if factor is not None:
            factors[model] = factor
    return factors


def complete_day_totals(hourly_by_model, date_str):
    """{model: kWh} for models with no missing hour on that local date.

    A gap is left out, not read as darkness: the Met Office series stops after
    about two and a half days, and a short day would look like a dull one.
    """
    totals = {}
    for model, hourly in hourly_by_model.items():
        values = [kwh for key, kwh in hourly.items() if key.startswith(date_str)]
        if len(values) >= MIN_DAY_HOURS and None not in values:
            totals[model] = sum(values)
    return totals


def blend_day(hourly_by_model, date_str, factors):
    """Average the calibrated models hour by hour for one local date.

    Returns {"hourly": {key: kWh}, "total": kWh, "models": [...]}, or None when
    fewer than MIN_BLEND_MODELS have both a complete day and a calibration.
    """
    complete = complete_day_totals(hourly_by_model, date_str)
    used     = [m for m in hourly_by_model if m in complete and m in factors]
    if len(used) < MIN_BLEND_MODELS:
        return None
    hourly = {}
    for key in hourly_by_model[used[0]]:
        if key.startswith(date_str):
            hourly[key] = (sum(hourly_by_model[m].get(key, 0.0) * factors[m] for m in used)
                           / len(used))
    return {"hourly": hourly, "total": sum(hourly.values()), "models": used}


def merge_records(records, new_rows):
    """Merge day records by date. A live record is never replaced by a seeded one."""
    by_date = {str(r.get("date")): r for r in records}
    for row in new_rows:
        date = str(row.get("date"))
        held = by_date.get(date)
        if held and held.get("source") == "live" and row.get("source") != "live":
            continue
        by_date[date] = row
    return [by_date[d] for d in sorted(by_date)][-RECORDS_KEPT:]


def day_ahead_summary(records, window_days=7):
    """How the evening-before figure for tomorrow compared with the day itself."""
    rows = [r for r in sorted(records, key=lambda r: str(r.get("date")))
            if float(r.get("headline_kwh") or 0.0) > 0.0
            and float(r.get("actual_kwh") or 0.0) > 0.0][-window_days:]
    if not rows:
        return {"days": 0, "mean_error_kwh": 0.0, "mean_actual_kwh": 0.0}
    errors = [abs(float(r["actual_kwh"]) - float(r["headline_kwh"])) for r in rows]
    return {
        "days":            len(rows),
        "mean_error_kwh":  round(sum(errors) / len(errors), 1),
        "mean_actual_kwh": round(sum(float(r["actual_kwh"]) for r in rows) / len(rows), 1),
    }
