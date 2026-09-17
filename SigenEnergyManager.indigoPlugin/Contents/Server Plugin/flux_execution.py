"""Acknowledged Flux execution, independent of Indigo and network construction.

The caller supplies a verified Flux account, commissioned physical limits and a
household/event-aware plan under the plugin's shared state lock. This module does
not establish those facts. All ordinary writers must yield while owns_control is
true. A confirmed external supervisor owns the registers exclusively.

An SOC backstop limits energy; it is NOT a hardware timer. Communication loss can
leave a mode active beyond its software expiry. Commissioning must resolve that
limitation before live activation. No IO writes occur in the constructor.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

# Real Modbus setters include their own throttled readback. A staged command
# takes longer than ten seconds; this is a maximum observation age at commit,
# not a promise that hardware stops itself when the software lease expires.
MAX_OBSERVATION_AGE_S = 60


@dataclass(frozen=True)
class FluxTarget:
    decision_at: datetime
    decision_until: datetime
    observed_at: datetime
    expires_at: datetime
    ems_mode: int
    charge_limit_w: int
    discharge_limit_w: int
    charge_cutoff_pct: float
    discharge_cutoff_pct: float


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Aware timestamps required')
    return value.astimezone(timezone.utc)


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Finite numeric value required')
    return value


def _near(value, expected, tolerance=0.04):
    return (type(value) in (int, float) and math.isfinite(value)
            and abs(value-expected) <= tolerance)


class FluxExecutor:
    """Durable, serial step interface around existing Modbus primitives.

    A restart reconciles before accepting a new target, even with a clean journal.
    Persist a claim before start writes; retain pending ownership on any uncertain
    write/read. Disk failure blocks starts but never blocks a restorative stop.
    """

    def __init__(self, raw_driver, journal_path, *, baseline_charge_w,
                 baseline_discharge_w, baseline_charge_cutoff_pct=100.,
                 baseline_discharge_cutoff_pct=1., clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.raw = raw_driver
        self.path = Path(journal_path)
        self.last_error = ''
        self._owns = True
        self._pending = True
        self._supervisor_owned = False
        self._target = None
        self.configure_baseline(
            baseline_charge_w=baseline_charge_w,
            baseline_discharge_w=baseline_discharge_w,
            baseline_charge_cutoff_pct=baseline_charge_cutoff_pct,
            baseline_discharge_cutoff_pct=baseline_discharge_cutoff_pct)
        # Loading is diagnostic only: no persisted flag proves hardware state.
        try:
            data = json.loads(self.path.read_text())
            if data.get('version') != 1 or type(data.get('owns')) is not bool:
                raise ValueError('Invalid ownership journal')
        except (OSError, ValueError, TypeError, AttributeError):
            self.last_error = 'Ownership journal absent or invalid; reconciliation required'

    @property
    def owns_control(self):
        return self._owns or self._pending

    def rebind(self, raw_driver):
        """Replace transport without IO; require fresh baseline reconciliation.

        A new connection is not proof of the old acknowledged register state.
        The caller still identifies any active external owner on the next step.
        """
        maximum = _number(raw_driver.inverter_max_w)
        if maximum <= 0 or any(w > maximum for w in self.baseline[:2]):
            raise ValueError('Replacement driver cannot support baseline limits')
        for method in ('set_charge_limit', 'set_discharge_limit',
                       'set_charge_cutoff', 'set_discharge_cutoff',
                       'read_charge_limit', 'read_discharge_limit',
                       'read_charge_cutoff', 'read_discharge_cutoff',
                       'enable_remote_ems', 'read_remote_ems_enabled',
                       'set_remote_ems_mode', 'read_ems_mode'):
            if not callable(getattr(raw_driver, method, None)):
                raise ValueError(f'Replacement driver lacks {method}')
        self.raw = raw_driver
        self._target = None
        self._owns = self._pending = True

    def configure_baseline(self, *, baseline_charge_w, baseline_discharge_w,
                           baseline_charge_cutoff_pct=100.,
                           baseline_discharge_cutoff_pct=1.):
        """Caller supplies the current policy baseline, including storm reserve.

        Updating this description performs no writes. Before external takeover the
        caller can set the appropriate restorative floor then request step(None).
        """
        max_w = _number(self.raw.inverter_max_w)
        c, d = baseline_charge_w, baseline_discharge_w
        if any(type(v) is not int or not 0 <= v <= max_w for v in (c, d)):
            raise ValueError('Invalid baseline power')
        top, bottom = map(_number, (baseline_charge_cutoff_pct,
                                  baseline_discharge_cutoff_pct))
        if not 0 <= bottom <= top <= 100:
            raise ValueError('Invalid baseline cutoff')
        # Baselines must be exactly representable, never silently weaken a floor.
        if any(abs(v*10-round(v*10)) > 1e-7 for v in (top, bottom)):
            raise ValueError('Baseline cutoff must use 0.1% resolution')
        self.baseline = (c, d, top, bottom)

    def _save(self):
        data = {'version': 1, 'owns': self._owns, 'pending': self._pending,
                'supervisor_owned': self._supervisor_owned,
                'target_expiry': self._target.expires_at.isoformat() if self._target else None}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name+'.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _verify(self, mode, charge, discharge, top, bottom):
        d = self.raw
        actual_mode = d.read_ems_mode()
        return (d.read_remote_ems_enabled() is True
                and type(actual_mode) is int and actual_mode == mode
                and _near(d.read_charge_limit(), charge, 1)
                and _near(d.read_discharge_limit(), discharge, 1)
                and _near(d.read_charge_cutoff(), top)
                and _near(d.read_discharge_cutoff(), bottom))

    def _set_read(self, setter, reader, expected, tolerance=0.04):
        return setter(expected) is True and _near(reader(), expected, tolerance)

    def _neutralise(self):
        d = self.raw
        # Zero both directions before selecting a mode or releasing SOC limits.
        if not self._set_read(d.set_charge_limit, d.read_charge_limit, 0, 1):
            return False
        if not self._set_read(d.set_discharge_limit, d.read_discharge_limit, 0, 1):
            return False
        if d.set_remote_ems_mode(2) is not True:
            return False
        mode = d.read_ems_mode()
        return type(mode) is int and mode == 2

    def _restore(self):
        d = self.raw
        if not self._neutralise():
            return False
        charge, discharge, top, bottom = self.baseline
        for setter, reader, value, tol in (
                (d.set_charge_cutoff, d.read_charge_cutoff, top, .04),
                (d.set_discharge_cutoff, d.read_discharge_cutoff, bottom, .04),
                (d.set_charge_limit, d.read_charge_limit, charge, 1),
                (d.set_discharge_limit, d.read_discharge_limit, discharge, 1)):
            if not self._set_read(setter, reader, value, tol):
                return False
        if d.enable_remote_ems() is not True:
            return False
        return self._verify(2, charge, discharge, top, bottom)

    def _valid_target(self, target, now):
        if not isinstance(target, FluxTarget):
            raise ValueError('FluxTarget required')
        start, stop, seen, expiry = map(_utc, (target.decision_at, target.decision_until,
                                             target.observed_at, target.expires_at))
        if not (start <= now < stop <= start+timedelta(minutes=30)
                and seen <= now < expiry <= seen+timedelta(seconds=MAX_OBSERVATION_AGE_S)
                and expiry <= stop):
            raise ValueError('Expired or invalid Flux decision/observation lease')
        if type(target.ems_mode) is not int or target.ems_mode not in (2, 3, 5):
            raise ValueError('Unsupported Flux mode')
        local = now.astimezone(ZoneInfo('Europe/London'))
        if target.ems_mode == 3:
            boundary = local.replace(hour=5, minute=0, second=0, microsecond=0)
            if not 2 <= local.hour < 5 or expiry > boundary.astimezone(timezone.utc):
                raise ValueError('Grid charging outside Flux cheap window')
        if target.ems_mode == 5:
            boundary = local.replace(hour=19, minute=0, second=0, microsecond=0)
            if not 16 <= local.hour < 19 or expiry > boundary.astimezone(timezone.utc):
                raise ValueError('Discretionary export outside Flux peak window')
        max_w = _number(self.raw.inverter_max_w)
        charge, discharge = target.charge_limit_w, target.discharge_limit_w
        if any(type(v) is not int or not 0 <= v <= max_w for v in (charge, discharge)):
            raise ValueError('Invalid target power')
        # Mode 2 permits solar charging and household supply in the same policy;
        # the inverter chooses the physical direction from the current net load.
        if (target.ems_mode == 3 and discharge) or (target.ems_mode == 5 and charge):
            raise ValueError('Conflicting battery directions')
        top, bottom = map(_number, (target.charge_cutoff_pct, target.discharge_cutoff_pct))
        if not 0 <= bottom <= top <= 100:
            raise ValueError('Invalid target energy band')
        if any(abs(v*10-round(v*10)) > 1e-7 for v in (top, bottom)):
            raise ValueError('Target cutoff must use 0.1% resolution')

    def _apply(self, t):
        d = self.raw
        if not self._neutralise():
            return False
        for setter, reader, value, tol in (
                (d.set_charge_cutoff, d.read_charge_cutoff, t.charge_cutoff_pct, .04),
                (d.set_discharge_cutoff, d.read_discharge_cutoff, t.discharge_cutoff_pct, .04),
                (d.set_charge_limit, d.read_charge_limit, t.charge_limit_w, 1),
                (d.set_discharge_limit, d.read_discharge_limit, t.discharge_limit_w, 1)):
            if not self._set_read(setter, reader, value, tol):
                return False
        self._valid_target(t, _utc(self.clock()))
        if d.enable_remote_ems() is not True or d.read_ems_mode() != 2:
            return False
        self._valid_target(t, _utc(self.clock()))
        if d.set_remote_ems_mode(t.ems_mode) is not True:
            return False
        verified = self._verify(t.ems_mode, t.charge_limit_w, t.discharge_limit_w,
                                t.charge_cutoff_pct, t.discharge_cutoff_pct)
        self._valid_target(t, _utc(self.clock()))
        return verified

    def step(self, target, now, *, supervisor_owns=False, communications_ok=True):
        """Caller must hold the shared supervisor lock for the entire call."""
        now = _utc(now)
        if type(supervisor_owns) is not bool or type(communications_ok) is not bool:
            raise ValueError('Explicit boolean ownership and communications required')
        if supervisor_owns:
            self._supervisor_owned = True
            self._owns = self._pending = False
            self._target = None
            try:
                self._save()
            except OSError as exc:
                self.last_error = f'Could not record supervisor ownership: {exc}'
            return 'supervisor'
        if self._supervisor_owned:
            self._supervisor_owned = False
            self._owns = self._pending = True
        if not communications_ok:
            # Do not manufacture a stop acknowledgement from a lost connection.
            if self._owns:
                self._pending = True
            self.last_error = 'Communications unavailable; physical expiry not confirmed'
            return 'pending'
        invalid = None
        if target is not None:
            try:
                self._valid_target(target, now)
            except (ValueError, TypeError) as exc:
                invalid = str(exc)
                target = None
        try:
            if self._pending or target is None:
                if not self.owns_control:
                    self.last_error = invalid or ''
                    return 'released'
                if not self._restore():
                    raise RuntimeError('Baseline restoration not acknowledged')
                self._owns = self._pending = False
                self._target = None
                self._save()
                self.last_error = invalid or ''
                return 'released'  # new targets only on a subsequent fresh step
            previous = self._target
            self._owns = True
            self._target = target
            self._save()  # must succeed before ANY start/renewal writes
            settings = lambda t: (t.ems_mode, t.charge_limit_w, t.discharge_limit_w,
                                  t.charge_cutoff_pct, t.discharge_cutoff_pct)
            if previous is not None and settings(previous) == settings(target):
                if self._verify(*settings(target)):
                    self._valid_target(target, _utc(self.clock()))
                    self.last_error = ''
                    return 'applied'
            # SAME MODE AND ENERGY BAND, NEW POWER ONLY: adjust the limits in place.
            # A full _apply neutralises first (both limits to 0, mode 2), which is
            # right for a change of mode or cutoffs but, measured live 17-Sep-2026,
            # dropped a 4 kW peak export to zero for ~20-30 s on every tick the
            # planned power moved. Staying in the verified mode and moving only the
            # limit in the permitted direction is the same end state without the gap.
            # Falls back to the full _apply on any unacknowledged write.
            if (previous is not None and previous.ems_mode == target.ems_mode
                    and previous.charge_cutoff_pct == target.charge_cutoff_pct
                    and previous.discharge_cutoff_pct == target.discharge_cutoff_pct
                    and self._verify(previous.ems_mode, previous.charge_limit_w,
                                     previous.discharge_limit_w,
                                     previous.charge_cutoff_pct, previous.discharge_cutoff_pct)):
                d = self.raw
                if (self._set_read(d.set_charge_limit, d.read_charge_limit, target.charge_limit_w, 1)
                        and self._set_read(d.set_discharge_limit, d.read_discharge_limit,
                                           target.discharge_limit_w, 1)
                        and self._verify(*settings(target))):
                    self._valid_target(target, _utc(self.clock()))
                    self.last_error = ''
                    return 'applied'
            if not self._apply(target):
                raise RuntimeError('Flux target not acknowledged')
            self.last_error = ''
            return 'applied'
        except Exception as exc:
            self._owns = self._pending = True
            self.last_error = str(exc)
            # A failed start could have partially changed hardware. Try a stop now,
            # but retain pending ownership until a later confirmed reconciliation.
            try:
                self._restore()
            except Exception:
                pass
            return 'pending'
