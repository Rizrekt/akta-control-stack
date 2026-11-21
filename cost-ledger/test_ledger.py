#!/usr/bin/env python3
"""
Tes invarian untuk ledger.py (stdlib unittest, tanpa dependensi).

Jalankan:
    cd cost-ledger && python3 -m unittest test_ledger -v

Semua timestamp dibangun deterministik dari datetime UTC — tes tidak
bergantung pada jam sistem, sesuai kontrak "waktu di-inject" ledger.
"""
import json
import math
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ledger as L  # noqa: E402


def ts(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


DAY1 = ts(2026, 11, 21, 10)          # 2026-11-21 10:00 UTC
DAY1_LATE = ts(2026, 11, 21, 23, 30)  # 2026-11-21 23:30 UTC
DAY2 = ts(2026, 11, 22, 9)           # 2026-11-22 09:00 UTC


def _mem(**kw):
    return L.CostLedger(conn=sqlite3.connect(":memory:"), **kw)


class RecordValidationTest(unittest.TestCase):
    def test_record_round_trip(self):
        with _mem() as led:
            rid = led.record(ts=DAY1, project="demo-project", lane="search-api",
                             category="api", amount=0.25, currency="USD",
                             note="deep search edisi 4")
            self.assertEqual(rid, 1)
            rows = led.entries()
            self.assertEqual(len(rows), 1)
            e = rows[0]
            self.assertEqual((e.project, e.lane, e.category), ("demo-project", "search-api", "api"))
            self.assertEqual(e.amount, 0.25)
            self.assertEqual(e.currency, "USD")
            self.assertEqual(e.note, "deep search edisi 4")

    def test_ts_is_required_no_default(self):
        with _mem() as led:
            with self.assertRaises(TypeError):
                led.record(project="p", lane="l", category="api", amount=1.0)  # tanpa ts

    def test_ts_type_and_range(self):
        with _mem() as led:
            with self.assertRaises(TypeError):
                led.record(ts="2026-11-21", project="p", lane="l", category="api", amount=1.0)
            with self.assertRaises(ValueError):
                led.record(ts=-5, project="p", lane="l", category="api", amount=1.0)
            with self.assertRaises(ValueError):
                led.record(ts=math.nan, project="p", lane="l", category="api", amount=1.0)
            with self.assertRaises(ValueError):
                led.record(ts=math.inf, project="p", lane="l", category="api", amount=1.0)

    def test_category_must_be_known(self):
        with _mem() as led:
            with self.assertRaises(ValueError):
                led.record(ts=DAY1, project="p", lane="l", category="tokens", amount=1.0)
            for cat in ("api", "gas", "infra"):
                led.record(ts=DAY1, project="p", lane="l", category=cat, amount=1.0)
            self.assertEqual(len(led.entries()), 3)

    def test_project_and_lane_non_empty(self):
        with _mem() as led:
            with self.assertRaises(ValueError):
                led.record(ts=DAY1, project="  ", lane="l", category="api", amount=1.0)
            with self.assertRaises(ValueError):
                led.record(ts=DAY1, project="p", lane="", category="api", amount=1.0)

    def test_amount_must_be_finite_number(self):
        with _mem() as led:
            with self.assertRaises(ValueError):
                led.record(ts=DAY1, project="p", lane="l", category="api", amount=math.nan)
            with self.assertRaises(ValueError):
                led.record(ts=DAY1, project="p", lane="l", category="api", amount=math.inf)
            with self.assertRaises(TypeError):
                led.record(ts=DAY1, project="p", lane="l", category="api", amount="5")

    def test_currency_normalized_upper(self):
        with _mem() as led:
            led.record(ts=DAY1, project="p", lane="bsc", category="gas",
                       amount=0.001, currency="bnb")
            self.assertEqual(led.entries()[0].currency, "BNB")

    def test_negative_amount_is_correction(self):
        with _mem() as led:
            led.record(ts=DAY1, project="p", lane="l", category="api", amount=10.0)
            led.record(ts=DAY1, project="p", lane="l", category="api", amount=-4.0,
                       note="refund sebagian")
            self.assertEqual(led.summary().totals_by_currency["USD"], 6.0)


class SummaryTest(unittest.TestCase):
    def _seed(self, led):
        led.record(ts=DAY1, project="demo-project", lane="search-api", category="api", amount=1.50)
        led.record(ts=DAY1, project="demo-project", lane="llm-router", category="api", amount=0.50)
        led.record(ts=DAY1, project="pxio", lane="bsc", category="gas", amount=0.02)
        led.record(ts=DAY1, project="pxio", lane="bsc", category="gas",
                   amount=0.0004, currency="BNB")
        led.record(ts=DAY2, project="demo-project", lane="search-api", category="api", amount=2.00)
        led.record(ts=DAY2, project="infra", lane="publicnode", category="infra", amount=5.00)

    def test_totals_never_mix_currencies(self):
        with _mem() as led:
            self._seed(led)
            s = led.summary()
            self.assertEqual(s.entries, 6)
            self.assertAlmostEqual(s.totals_by_currency["USD"], 9.02, places=6)
            self.assertAlmostEqual(s.totals_by_currency["BNB"], 0.0004, places=6)

    def test_by_project_lane_category_day(self):
        with _mem() as led:
            self._seed(led)
            s = led.summary()
            self.assertAlmostEqual(s.by_project["demo-project"]["USD"], 4.00, places=6)
            self.assertAlmostEqual(s.by_lane["search-api"]["USD"], 3.50, places=6)
            self.assertAlmostEqual(s.by_category["gas"]["USD"], 0.02, places=6)
            self.assertAlmostEqual(s.by_category["gas"]["BNB"], 0.0004, places=6)
            self.assertAlmostEqual(s.by_day["2026-11-21"]["USD"], 2.02, places=6)
            self.assertAlmostEqual(s.by_day["2026-11-22"]["USD"], 7.00, places=6)

    def test_window_and_project_filters(self):
        with _mem() as led:
            self._seed(led)
            s = led.summary(since=DAY2)
            self.assertEqual(s.entries, 2)
            s2 = led.summary(project="demo-project")
            self.assertEqual(s2.entries, 3)
            self.assertAlmostEqual(s2.totals_by_currency["USD"], 4.00, places=6)
            s3 = led.summary(until=DAY2)
            self.assertEqual(s3.entries, 4)

    def test_daily_total(self):
        with _mem() as led:
            self._seed(led)
            self.assertAlmostEqual(led.daily_total("2026-11-21"), 2.02, places=6)
            self.assertAlmostEqual(
                led.daily_total("2026-11-21", project="pxio", currency="BNB"),
                0.0004, places=6)
            self.assertEqual(led.daily_total("2026-11-23"), 0.0)

    def test_report_text(self):
        with _mem() as led:
            self._seed(led)
            r = led.summary().report()
            self.assertIn("cost ledger", r)
            self.assertIn("demo-project", r)
            self.assertIn("BNB", r)

    def test_tz_offset_shifts_day_bucket(self):
        # 2026-11-21 18:00 UTC = 2026-11-22 01:00 WIB
        with _mem(tz_offset_s=7 * 3600) as led:
            led.record(ts=ts(2026, 11, 21, 18), project="p", lane="l",
                       category="api", amount=3.0)
            self.assertAlmostEqual(led.daily_total("2026-11-22"), 3.0, places=6)
            self.assertEqual(led.daily_total("2026-11-21"), 0.0)
        with _mem() as led:  # offset 0 = UTC
            led.record(ts=ts(2026, 11, 21, 18), project="p", lane="l",
                       category="api", amount=3.0)
            self.assertAlmostEqual(led.daily_total("2026-11-21"), 3.0, places=6)


class DailyAlertTest(unittest.TestCase):
    def test_threshold_set_list_remove(self):
        with _mem() as led:
            led.set_daily_threshold(10.0, currency="USD")
            led.set_daily_threshold(3.0, currency="USD", scope="demo-project")
            rows = led.daily_thresholds()
            self.assertIn(("*", "USD", 10.0), rows)
            self.assertIn(("demo-project", "USD", 3.0), rows)
            led.remove_daily_threshold(currency="USD", scope="demo-project")
            self.assertNotIn(("demo-project", "USD", 3.0), led.daily_thresholds())
            with self.assertRaises(ValueError):
                led.set_daily_threshold(0)
            with self.assertRaises(ValueError):
                led.set_daily_threshold(-1)

    def test_no_alert_under_threshold(self):
        with _mem() as led:
            led.set_daily_threshold(10.0)
            led.record(ts=DAY1, project="p", lane="l", category="api", amount=9.99)
            self.assertEqual(led.check_daily_alerts(now_ts=DAY1_LATE), [])

    def test_alert_fires_once_per_day_then_dedup(self):
        with _mem() as led:
            led.set_daily_threshold(10.0)
            led.record(ts=DAY1, project="p", lane="l", category="api", amount=6.0)
            led.record(ts=DAY1, project="p", lane="l", category="gas", amount=5.0)
            fired = led.check_daily_alerts(now_ts=DAY1_LATE)
            self.assertEqual(len(fired), 1)
            self.assertIn("2026-11-21", fired[0])
            self.assertIn("11.0000 USD", fired[0])
            # pemeriksaan ulang hari yang sama: tidak mengulang
            self.assertEqual(led.check_daily_alerts(now_ts=DAY1_LATE), [])
            hist = led.alerts_fired("2026-11-21")
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["scope"], "*")
            self.assertAlmostEqual(hist[0]["total_at_fire"], 11.0, places=6)

    def test_alert_can_fire_again_next_day(self):
        with _mem() as led:
            led.set_daily_threshold(10.0)
            led.record(ts=DAY1, project="p", lane="l", category="api", amount=11.0)
            self.assertEqual(len(led.check_daily_alerts(now_ts=DAY1_LATE)), 1)
            led.record(ts=DAY2, project="p", lane="l", category="api", amount=12.0)
            fired = led.check_daily_alerts(now_ts=DAY2)
            self.assertEqual(len(fired), 1)
            self.assertIn("2026-11-22", fired[0])

    def test_project_scope_alert_and_isolation(self):
        with _mem() as led:
            led.set_daily_threshold(3.0, scope="demo-project")
            led.record(ts=DAY1, project="lain", lane="l", category="api", amount=99.0)
            self.assertEqual(led.check_daily_alerts(now_ts=DAY1_LATE), [])
            led.record(ts=DAY1, project="demo-project", lane="l", category="api", amount=3.50)
            fired = led.check_daily_alerts(now_ts=DAY1_LATE)
            self.assertEqual(len(fired), 1)
            self.assertIn("demo-project", fired[0])

    def test_global_scope_aggregates_projects(self):
        with _mem() as led:
            led.set_daily_threshold(10.0)  # global
            led.record(ts=DAY1, project="a", lane="l", category="api", amount=6.0)
            led.record(ts=DAY1, project="b", lane="l", category="api", amount=6.0)
            fired = led.check_daily_alerts(now_ts=DAY1_LATE)
            self.assertEqual(len(fired), 1)
            self.assertIn("SEMUA PROYEK", fired[0])

    def test_threshold_currency_isolated(self):
        with _mem() as led:
            led.set_daily_threshold(0.001, currency="BNB")
            led.record(ts=DAY1, project="p", lane="bsc", category="gas",
                       amount=500.0, currency="USD")
            self.assertEqual(led.check_daily_alerts(now_ts=DAY1_LATE), [])
            led.record(ts=DAY1, project="p", lane="bsc", category="gas",
                       amount=0.002, currency="BNB")
            self.assertEqual(len(led.check_daily_alerts(now_ts=DAY1_LATE)), 1)


class PersistenceAndExportTest(unittest.TestCase):
    def test_entries_and_alert_state_persist_across_instances(self):
        with tempfile.TemporaryDirectory() as td:
            db = pathlib.Path(td) / "ledger.db"
            led = L.CostLedger(db_path=db)
            led.set_daily_threshold(5.0)
            led.record(ts=DAY1, project="p", lane="l", category="api", amount=7.0)
            self.assertEqual(len(led.check_daily_alerts(now_ts=DAY1_LATE)), 1)
            led.close()
            # instance baru di berkas yang sama: entri utuh, alert tidak refire
            led2 = L.CostLedger(db_path=db)
            self.assertEqual(len(led2.entries()), 1)
            self.assertEqual(led2.check_daily_alerts(now_ts=DAY1_LATE), [])
            led2.close()

    def test_export_jsonl(self):
        with _mem() as led, tempfile.TemporaryDirectory() as td:
            led.record(ts=DAY1, project="demo-project", lane="search-api",
                       category="api", amount=0.25, note="deep search")
            led.record(ts=DAY2, project="pxio", lane="bsc", category="gas",
                       amount=0.0004, currency="BNB")
            out = pathlib.Path(td) / "out.jsonl"
            n = led.export_jsonl(out)
            self.assertEqual(n, 2)
            lines = out.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first["project"], "demo-project")
            self.assertEqual(first["day"], "2026-11-21")
            second = json.loads(lines[1])
            self.assertEqual(second["currency"], "BNB")


if __name__ == "__main__":
    unittest.main()
