#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    house_energy_cost.py
# Description: A running record of what the house's own energy cost: grid energy
#              at the import rate in force when it was used, battery energy at
#              what its charge cost, and solar free. Published as two lifetime
#              totals (kWh and pounds) so another plugin can price anything that
#              ran between two moments: cost = change in pounds / change in kWh.
# Author:      CliveS & Claude Opus 5.5
# Date:        28-11-2026
# Version:     1.0
#
# Why: Appliance Monitor priced every washing machine cycle at the grid import
# rate in force when the cycle ended (25.6p), although the house ran on battery
# energy bought at 15.5p in the Flux cheap window. The appliance's own plug knows
# how much it used, not where the energy came from. This ledger knows the second.
#
# How each step is split (powers in watts, grid + = import, battery + = charging):
#   grid import feeds the battery first (a charge in the cheap window), then the
#   house. The battery's discharge feeds what of the house the grid did not, and
#   any discharge beyond that went to the grid (a Flux peak sale) and is not a
#   house cost. Solar covers the rest of the house, at no cost.
# The battery is a pool: a charge from the grid adds its cost at the rate in
# force, a charge from solar adds energy at no cost, and a discharge takes
# energy at the pool's average price. After each step the pool's energy is set
# to what the battery's state of charge says (keeping the average price), so
# drift in the power readings never builds up, and the energy lost in charging
# is carried in the price of what is left.
#
# Pure: no indigo, no I/O. plugin.py feeds it readings and saves its state.

MAX_STEP_S      = 300.0   # a longer gap between readings is not integrated
MIN_POOL_KWH    = 0.05    # below this the pool has no meaningful average price
STATE_VERSION   = 1


def _num(value, default=None):
    """A finite float, or default."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


class HouseCostLedger:
    """Lifetime totals of the house's energy and what it cost.

    house_kwh   lifetime kWh the house used (while a price was known)
    house_cost_p lifetime pence that energy cost
    pool_kwh    energy now in the battery, kWh
    pool_cost_p what that energy cost, pence
    """

    def __init__(self, state=None):
        s = state if isinstance(state, dict) else {}
        if s.get("version") != STATE_VERSION:
            s = {}
        self.house_kwh    = max(0.0, _num(s.get("house_kwh"), 0.0))
        self.house_cost_p = max(0.0, _num(s.get("house_cost_p"), 0.0))
        self.pool_kwh     = _num(s.get("pool_kwh"), None)
        self.pool_cost_p  = _num(s.get("pool_cost_p"), None)
        self.last_ts      = None     # never restored: a gap across a restart is not integrated
        self.last_rate_p  = _num(s.get("last_rate_p"), None)
        if self.pool_kwh is None or self.pool_cost_p is None or self.pool_kwh < 0 or self.pool_cost_p < 0:
            self.pool_kwh, self.pool_cost_p = None, None

    # ------------------------------------------------------------------ state
    def to_state(self):
        return {
            "version":      STATE_VERSION,
            "house_kwh":    round(self.house_kwh, 6),
            "house_cost_p": round(self.house_cost_p, 6),
            "pool_kwh":     None if self.pool_kwh is None else round(self.pool_kwh, 6),
            "pool_cost_p":  None if self.pool_cost_p is None else round(self.pool_cost_p, 6),
            "last_rate_p":  self.last_rate_p,
        }

    def battery_price_p(self):
        """Average pence per kWh of the energy now in the battery, or None."""
        if self.pool_kwh is None or self.pool_kwh < MIN_POOL_KWH:
            return None
        return self.pool_cost_p / self.pool_kwh

    # ------------------------------------------------------------------- step
    def step(self, now, grid_w, batt_w, home_w, soc_pct, capacity_kwh, rate_p,
             seed_price_p=None):
        """Feed one set of readings taken at `now` (epoch seconds).

        rate_p is the import price in force (pence/kWh), or None when unknown:
        the last known price is then used, and with none ever known the step
        moves the clock only, so the totals never count energy they cannot price.
        seed_price_p prices the battery's contents the first time it is seen;
        without one the rate in force is used.
        Returns True when the step was integrated.
        """
        now     = _num(now, None)
        grid_w  = _num(grid_w, None)
        batt_w  = _num(batt_w, None)
        home_w  = _num(home_w, None)
        soc     = _num(soc_pct, None)
        cap     = _num(capacity_kwh, None)
        rate    = _num(rate_p, None)
        if rate is not None and rate < 0:
            rate = None   # a negative import price is not something this house has
        if rate is not None:
            self.last_rate_p = rate
        else:
            rate = self.last_rate_p
        if now is None or grid_w is None or batt_w is None or home_w is None:
            return False

        target_kwh = None
        if soc is not None and cap is not None and cap > 0 and 0.0 <= soc <= 100.0:
            target_kwh = cap * soc / 100.0

        if self.pool_kwh is None and target_kwh is not None:
            seed = _num(seed_price_p, None)
            if seed is None or seed < 0:
                seed = rate
            if seed is not None:
                self.pool_kwh    = target_kwh
                self.pool_cost_p = target_kwh * seed

        prev, self.last_ts = self.last_ts, now
        if prev is None or rate is None:
            return False
        dt = now - prev
        if dt <= 0 or dt > MAX_STEP_S:
            return False

        h       = dt / 3600.0 / 1000.0            # kWh per watt over this step
        imp     = max(grid_w, 0.0) * h
        charge  = max(batt_w, 0.0) * h
        dis     = max(-batt_w, 0.0) * h
        home    = max(home_w, 0.0) * h

        grid_to_batt = min(charge, imp)
        grid_to_home = min(home, imp - grid_to_batt)
        batt_to_home = min(dis, home - grid_to_home)

        avg = self.battery_price_p()
        batt_price = avg if avg is not None else rate
        self.house_kwh    += home
        self.house_cost_p += grid_to_home * rate + batt_to_home * batt_price

        if self.pool_kwh is not None:
            avg_before = batt_price
            self.pool_kwh    += charge - dis
            self.pool_cost_p += grid_to_batt * rate - dis * avg_before
            if self.pool_kwh < 0 or self.pool_cost_p < 0:
                self.pool_kwh    = max(self.pool_kwh, 0.0)
                self.pool_cost_p = max(self.pool_cost_p, 0.0)
            if target_kwh is not None:
                price = self.battery_price_p()
                if price is None:
                    # Empty, or as good as: what is left of it carries the price
                    # of whatever charged it last, or the rate in force.
                    price = (grid_to_batt * rate / charge) if charge > 0 else rate
                self.pool_kwh    = target_kwh
                self.pool_cost_p = target_kwh * price
        return True
