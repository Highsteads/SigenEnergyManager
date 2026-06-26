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
_req = MagicMock()


class _ConnErr(Exception):
    pass


class _Timeout(Exception):
    pass


_req.exceptions.ConnectionError = _ConnErr
_req.exceptions.Timeout = _Timeout
sys.modules["requests"] = _req

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import axle_api   # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
