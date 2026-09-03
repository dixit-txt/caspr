"""HTTP shim must restore datetime so generate-report can call isoformat()."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase

from app.deliverables.service_entry import (
    parse_iso_datetime,
    restore_report_output_datetimes,
)


class ParseIsoDatetimeTests(TestCase):
    def test_parses_zulu_string(self):
        value = parse_iso_datetime("2026-09-03T06:22:49Z")
        self.assertIsInstance(value, datetime)
        self.assertEqual(value, datetime(2026, 9, 3, 6, 22, 49, tzinfo=timezone.utc))

    def test_leaves_datetime_unchanged(self):
        now = datetime.now(timezone.utc)
        self.assertIs(parse_iso_datetime(now), now)


class RestoreReportOutputDatetimesTests(TestCase):
    def test_restores_nested_generation_time(self):
        payload = restore_report_output_datetimes(
            {
                "success": True,
                "data": {
                    "s3_paths": {"pdf": "s3://bucket/report.pdf"},
                    "report_generation_time": "2026-09-03T06:22:49.123456+00:00",
                },
            }
        )
        value = payload["data"]["report_generation_time"]
        self.assertIsInstance(value, datetime)
        self.assertEqual("2026-09-03T06:22:49.123456+00:00", value.isoformat())
