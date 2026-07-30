#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_axle_api.py
# Description: Unit tests for axle_api.AxleAPI.get_next_event — the VPP event poller.
#              An upstream API that changes shape (null body, 204, 401, malformed
#              JSON, non-string timestamps, missing optional fields) must NEVER crash
#              the poll loop or misreport an event; it must return a clean dict or None.
#              No network — requests is mocked.
# Author:      CliveS & Claude Opus 4.8
# Date:        26-06-2026
# Version:     1.0

import os
import sys
import unittest
from unittest.mock import MagicMock

# ---- Mock requests (with real exception classes) before importing axle_api ----
class _ConnErr(Exception):
    pass


class _Timeout(Exception):
    pass


# Install the stub only when the slot is empty — an unconditional assignment
# made the combined suite ordering-dependent (whichever test file imported
# first won the sys.modules slot for every later import of requests).
if "requests" not in sys.modules:
    _req = MagicMock()
    _req.exceptions.ConnectionError = _ConnErr
    _req.exceptions.Timeout = _Timeout
    sys.modules["requests"] = _req

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import axle_api   # noqa: E402

# Raise the SAME exception classes axle_api actually catches — whichever module
# won the requests slot (the stub above, another file's stub, or real requests).
_ConnErr = axle_api.requests.exceptions.ConnectionError
_Timeout = axle_api.requests.exceptions.Timeout


def _resp(status=200, content=b'{"x":1}', json_data=None, raise_json=False):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.text = content.decode() if isinstance(content, bytes) else str(content)
    if raise_json:
        r.json.side_effect = ValueError("bad json")
    else:
        r.json.return_value = json_data
    return r


_VALID = {
    "start_time":    "2026-03-20T18:00:00+00:00",
    "end_time":      "2026-03-20T19:30:00+00:00",
    "import_export": "export",
}


class TestAxleGetNextEvent(unittest.TestCase):
    def setUp(self):
        self.api = axle_api.AxleAPI("token123")

    def _set(self, resp):
        axle_api.requests.get = MagicMock(return_value=resp)

    def test_valid_event_returns_dict(self):
        self._set(_resp(json_data=dict(_VALID)))
        ev = self.api.get_next_event()
        self.assertIsNotNone(ev)
        self.assertAlmostEqual(ev["duration_hrs"], 1.5)
        self.assertEqual(ev["import_export"], "export")
        self.assertEqual(ev["start_time"].hour, 18)

    def test_missing_optional_fields_default_none(self):
        # forecast/revenue absent on older API -> None (NOT a misreported 0).
        self._set(_resp(json_data=dict(_VALID)))
        ev = self.api.get_next_event()
        self.assertIsNone(ev["forecast_dispatch_kwh"])
        self.assertIsNone(ev["estimated_revenue_p"])

    def test_optional_fields_coerced(self):
        d = dict(_VALID, forecast_dispatch_kwh="4.2", estimated_revenue_p="350")
        self._set(_resp(json_data=d))
        ev = self.api.get_next_event()
        self.assertEqual(ev["forecast_dispatch_kwh"], 4.2)
        self.assertEqual(ev["estimated_revenue_p"], 350.0)

    def test_204_returns_none(self):
        self._set(_resp(status=204, content=b""))
        self.assertIsNone(self.api.get_next_event())

    def test_empty_body_returns_none(self):
        self._set(_resp(status=200, content=b""))
        self.assertIsNone(self.api.get_next_event())

    def test_null_json_returns_none(self):
        self._set(_resp(json_data=None, content=b"null"))
        self.assertIsNone(self.api.get_next_event())

    def test_401_returns_none(self):
        self._set(_resp(status=401))
        self.assertIsNone(self.api.get_next_event())

    def test_http_500_returns_none(self):
        self._set(_resp(status=500))
        self.assertIsNone(self.api.get_next_event())

    def test_malformed_json_returns_none(self):
        self._set(_resp(raise_json=True))
        self.assertIsNone(self.api.get_next_event())

    def test_missing_timestamps_returns_none(self):
        self._set(_resp(json_data={"import_export": "export"}))
        self.assertIsNone(self.api.get_next_event())

    def test_numeric_timestamps_returns_none(self):
        # A non-string timestamp must be handled, not raise AttributeError.
        self._set(_resp(json_data={"start_time": 123, "end_time": 456}))
        self.assertIsNone(self.api.get_next_event())

    def test_no_token_returns_none_without_request(self):
        api = axle_api.AxleAPI("")
        axle_api.requests.get = MagicMock(side_effect=AssertionError("should not call"))
        self.assertIsNone(api.get_next_event())

    def test_connection_error_returns_none(self):
        axle_api.requests.get = MagicMock(side_effect=_ConnErr("no net"))
        self.assertIsNone(self.api.get_next_event())


class TestAxleFailureIsVisible(unittest.TestCase):
    """v1.3 — a failing poll must be distinguishable from a quiet day.

    get_next_event() returns None for BOTH, so the caller cannot tell them apart
    from the return value alone. It could not, and on 30-Jul-2026 that cost six
    weeks: Axle stopped accepting this install's token some time after 15-Jun,
    every poll 401'd, and because AxleAPI logged through a logger with no handler
    attached there was not one line about it anywhere. The VPP device read
    "Standby" throughout, which is exactly what a quiet week looks like.
    """

    def setUp(self):
        self.api = axle_api.AxleAPI("token123")

    def _set(self, resp):
        axle_api.requests.get = MagicMock(return_value=resp)

    # --- healthy polls leave no error, event or not -----------------------

    def test_event_found_clears_error(self):
        self._set(_resp(json_data=dict(_VALID)))
        self.api.get_next_event()
        self.assertIsNone(self.api.last_error)

    def test_no_event_is_not_an_error(self):
        # THE distinction this whole class exists for: a quiet day is healthy.
        for label, resp in (
            ("null body",  _resp(json_data=None)),
            ("204",        _resp(status=204, content=b"")),
            ("empty body", _resp(content=b"")),
        ):
            with self.subTest(label):
                self.api.last_error = "stale"
                self._set(resp)
                self.assertIsNone(self.api.get_next_event())
                self.assertIsNone(self.api.last_error)

    # --- every failure mode records something ----------------------------

    def test_401_records_auth_failure(self):
        self._set(_resp(status=401, content=b'{"detail":"Could not validate credentials"}'))
        self.api.get_next_event()
        self.assertIsNotNone(self.api.last_error)
        self.assertIn("401", self.api.last_error)

    def test_http_error_records_status(self):
        self._set(_resp(status=503, content=b"down"))
        self.api.get_next_event()
        self.assertIn("503", self.api.last_error or "")

    def test_malformed_json_records_error(self):
        self._set(_resp(raise_json=True))
        self.api.get_next_event()
        self.assertIsNotNone(self.api.last_error)

    def test_missing_timestamps_records_error(self):
        self._set(_resp(json_data={"import_export": "export"}))
        self.api.get_next_event()
        self.assertIsNotNone(self.api.last_error)

    def test_implausible_window_records_error(self):
        self._set(_resp(json_data={
            "start_time": "2026-03-20T18:00:00+00:00",
            "end_time":   "2026-03-20T17:00:00+00:00",
        }))
        self.api.get_next_event()
        self.assertIsNotNone(self.api.last_error)

    def test_connection_error_records_error(self):
        axle_api.requests.get = MagicMock(side_effect=_ConnErr("no net"))
        self.api.get_next_event()
        self.assertIsNotNone(self.api.last_error)

    def test_timeout_records_error(self):
        axle_api.requests.get = MagicMock(side_effect=_Timeout("slow"))
        self.api.get_next_event()
        self.assertIsNotNone(self.api.last_error)

    def test_no_token_records_error(self):
        api = axle_api.AxleAPI("")
        api.get_next_event()
        self.assertIsNotNone(api.last_error)

    # --- recovery ---------------------------------------------------------

    def test_error_clears_on_next_good_poll(self):
        # A latched error would report a dead feed for ever after one blip.
        self._set(_resp(status=401, content=b"nope"))
        self.api.get_next_event()
        self.assertIsNotNone(self.api.last_error)

        self._set(_resp(json_data=dict(_VALID)))
        self.assertIsNotNone(self.api.get_next_event())
        self.assertIsNone(self.api.last_error)

    # --- logger injection -------------------------------------------------

    def test_injected_logger_receives_the_error(self):
        # The whole point: the plugin's logger, which has handlers, sees it.
        spy = MagicMock()
        api = axle_api.AxleAPI("token123", logger=spy)
        axle_api.requests.get = MagicMock(return_value=_resp(status=401, content=b"nope"))
        api.get_next_event()
        self.assertTrue(spy.error.called, "401 must reach the injected logger")

    def test_default_logger_still_works(self):
        api = axle_api.AxleAPI("token123")
        self.assertIsNotNone(api.logger)
        axle_api.requests.get = MagicMock(return_value=_resp(json_data=dict(_VALID)))
        self.assertIsNotNone(api.get_next_event())


if __name__ == "__main__":
    unittest.main(verbosity=2)
