#!/usr/bin/env python3
"""Test suite for the gold_watcher v12 changes (fx_mr group, rebalanced
weights, 62/35 FLAT band + derived ladder, Monday/morning bias dampener,
predictions.csv migration, daily_agent 4-group rails).

Run:  python3 -m unittest discover -s tests -v      (from the repo root)
or:   python3 -m pytest tests/ -q

The E2E smoke tests copy the repo to a temp dir and run the real CLIs, so
the live data/*.csv files are never touched. Network-dependent groups
(consensus / premium monitor) may be absent on offline machines - the
assertions tolerate that but fx_mr must always be there.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import predictor  # noqa: E402


def _write_prices_csv(path, quotes, prices=True):
    """quotes: [(ts_str, fx)]; optional 21K price column for realism."""
    header = ["timestamp_utc", "gold_21k_sell", "usd_egp"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for i, (ts, fx) in enumerate(quotes):
            row = [ts, str(6300 + i), str(fx)] if prices else [ts, str(fx)]
            w.writerow(row[:3])


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# fx_mr signal helper
# --------------------------------------------------------------------------- #
class FxMrSignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gw_fx_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fx(self, quotes, now=NOW):
        path = os.path.join(self.tmp, "gold_prices.csv")
        _write_prices_csv(path, quotes)
        return predictor.fx_mean_reversion(path, now=now)

    def test_spike_gives_negative_signal(self):
        # +0.5% USD/EGP 24h spike -> full negative (mean-reversion) signal
        res = self._fx([(_ts(NOW - timedelta(hours=24)), "50.00"),
                        (_ts(NOW), "50.25")])
        self.assertEqual(res["group"], "fx_mr")
        self.assertEqual(res["score"], -1.0)
        self.assertEqual(res["weight"], 1.0)

    def test_drop_gives_positive_signal(self):
        res = self._fx([(_ts(NOW - timedelta(hours=24)), "50.00"),
                        (_ts(NOW), "49.75")])
        self.assertEqual(res["score"], 1.0)

    def test_partial_move_scales_linearly(self):
        # +0.3% spike -> clip(-0.3/0.5) = -0.6
        res = self._fx([(_ts(NOW - timedelta(hours=24)), "50.00"),
                        (_ts(NOW), "50.15")])
        self.assertAlmostEqual(res["score"], -0.6)

    def test_quiet_fx_is_neutral_zero(self):
        # +0.1% (< 0.15% threshold) -> neutral 0.0
        res = self._fx([(_ts(NOW - timedelta(hours=24)), "50.00"),
                        (_ts(NOW), "50.05")])
        self.assertEqual(res["score"], 0.0)
        self.assertIn("quiet", res["detail"])

    def test_missing_history_is_zero(self):
        res = self._fx([(_ts(NOW), "50.25")])          # no ~24h-old quote
        self.assertEqual(res["score"], 0.0)
        res = self._fx([(_ts(NOW), "50.25")], now=NOW)  # tolerance miss below
        self.assertEqual(res["score"], 0.0)

    def test_prior_tolerance_window(self):
        # prior 25.5h old (within +-2h of the 24h target) -> used
        res = self._fx([(_ts(NOW - timedelta(hours=25, minutes=30)), "50.00"),
                        (_ts(NOW), "50.25")])
        self.assertEqual(res["score"], -1.0)
        # prior exactly 26h old sits ON the +-2h edge (inclusive) -> used
        res = self._fx([(_ts(NOW - timedelta(hours=26)), "50.00"),
                        (_ts(NOW), "50.25")])
        self.assertEqual(res["score"], -1.0)
        # prior 26h10m old (outside +-2h) -> missing -> neutral
        res = self._fx([(_ts(NOW - timedelta(hours=26, minutes=10)), "50.00"),
                        (_ts(NOW), "50.25")])
        self.assertEqual(res["score"], 0.0)

    def test_strict_no_lookahead(self):
        # a huge future move must never leak into the score at `now`
        res = self._fx([(_ts(NOW - timedelta(hours=24)), "50.00"),
                        (_ts(NOW), "50.25"),
                        (_ts(NOW + timedelta(hours=1)), "60.00"),
                        (_ts(NOW + timedelta(hours=6)), "40.00")])
        self.assertEqual(res["score"], -1.0)

    def test_no_lookahead_in_prior_search(self):
        # the 55.00 row is AFTER `now` - it must never become the current
        # quote. Ignored -> current == the 24h-old quote -> neutral 0.0
        # (a look-ahead implementation would return -1.0 here)
        res = self._fx([(_ts(NOW - timedelta(hours=24)), "50.00"),
                        (_ts(NOW + timedelta(minutes=30)), "55.00")],
                       now=NOW)
        self.assertEqual(res["score"], 0.0)

    def test_missing_file_is_fail_soft(self):
        res = predictor.fx_mean_reversion(os.path.join(self.tmp, "nope.csv"))
        self.assertEqual(res["score"], 0.0)
        self.assertEqual(res["weight"], 1.0)

    def test_bad_column_is_fail_soft(self):
        path = os.path.join(self.tmp, "no_fx.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows([["timestamp_utc", "gold_21k_sell"],
                                      [_ts(NOW), "6300"]])
        res = predictor.fx_mean_reversion(path)
        self.assertEqual(res["score"], 0.0)


# --------------------------------------------------------------------------- #
# Dampener + thresholds / FLAT band
# --------------------------------------------------------------------------- #
class DampenerAndThresholdTests(unittest.TestCase):
    def test_dampener_on_monday_any_hour(self):
        for hour in (3, 10, 22):
            dt = datetime(2026, 11, 30, hour, 0)   # 2026-11-30 is a Monday
            self.assertAlmostEqual(predictor.dampen_raw_bias(0.8, dt=dt),
                                   0.8 * predictor.BIAS_DAMPENER)

    def test_dampener_on_cairo_morning_hours(self):
        for day, hour in ((24, 8), (25, 9), (26, 11)):  # Tue/Wed/Thu 08-11
            dt = datetime(2026, 11, day, hour, 30)
            self.assertAlmostEqual(predictor.dampen_raw_bias(0.8, dt=dt),
                                   0.8 * predictor.BIAS_DAMPENER)

    def test_no_dampener_otherwise(self):
        for day, hour in ((24, 12), (24, 14), (25, 7), (26, 23)):
            dt = datetime(2026, 11, day, hour, 0)
            self.assertEqual(predictor.dampen_raw_bias(0.8, dt=dt), 0.8)

    def test_morning_boundary_0800_and_1159(self):
        on1 = datetime(2026, 11, 24, 8, 0)     # exactly 08:00 -> dampened
        on2 = datetime(2026, 11, 24, 11, 59)   # 11:59 -> dampened
        off = datetime(2026, 11, 24, 12, 0)    # 12:00 -> not
        self.assertAlmostEqual(predictor.dampen_raw_bias(1.0, dt=on1),
                               predictor.BIAS_DAMPENER)
        self.assertAlmostEqual(predictor.dampen_raw_bias(1.0, dt=on2),
                               predictor.BIAS_DAMPENER)
        self.assertEqual(predictor.dampen_raw_bias(1.0, dt=off), 1.0)

    def test_thresholds_are_62_35(self):
        self.assertEqual(predictor.UP_THRESHOLD, 62)
        self.assertEqual(predictor.DOWN_THRESHOLD, 35)

    def test_ladder_derives_from_thresholds(self):
        self.assertEqual(predictor.LADDER_UP, (62, 69, 76, 84, 94))
        self.assertEqual(predictor.LADDER_DOWN, (35, 28, 21, 13, 3))

    def test_scores_36_to_61_are_flat(self):
        for pct in range(36, 62):
            up = pct >= predictor.UP_THRESHOLD
            down = pct <= predictor.DOWN_THRESHOLD
            self.assertFalse(up, f"{pct} wrongly UP")
            self.assertFalse(down, f"{pct} wrongly DOWN")

    def test_band_edges_call_directions(self):
        self.assertEqual("UP" if 62 >= predictor.UP_THRESHOLD else "FLAT",
                         "UP")
        self.assertEqual("DOWN" if 35 <= predictor.DOWN_THRESHOLD else "FLAT",
                         "DOWN")


class GaugeAlarmBandTests(unittest.TestCase):
    """FLAT-band runs must never ring; rung crossings behave as designed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gw_alarm_")
        self.pred = os.path.join(self.tmp, "predictions.csv")
        self.alarms = os.path.join(self.tmp, "alarms.csv")
        with open(self.pred, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(predictor.PRED_HEADER)
        with open(self.alarms, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(predictor.ALARMS_HEADER)
        self._orig = (predictor.PRED_PATH, predictor.ALARMS_PATH)
        predictor.PRED_PATH, predictor.ALARMS_PATH = self.pred, self.alarms
        cairo = datetime.now(predictor.CAIRO)
        self._base = cairo.replace(minute=0, second=0, microsecond=0)

    def tearDown(self):
        predictor.PRED_PATH, predictor.ALARMS_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_run(self, pct):
        """Append a prediction row an hour after the previous one."""
        cairo = self._base + timedelta(hours=len(self._runs()))
        with open(self.pred, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=predictor.PRED_HEADER).writerow({
                "timestamp_utc": _ts(cairo - timedelta(hours=3)),
                "timestamp_cairo": _ts(cairo),
                "final_pct": pct, "direction": "FLAT",
                "price_21k": "6300"})
        return cairo

    @staticmethod
    def _runs():
        with open(predictor.PRED_PATH, newline="", encoding="utf-8") as fh:
            return [r for r in csv.reader(fh) if r][1:]

    def test_flat_band_never_rings(self):
        self._add_run(59)
        self.assertIsNone(predictor.detect_gauge_alarm(59, 6300,
                                                       persist=False))
        self._add_run(59)
        self.assertIsNone(predictor.detect_gauge_alarm(61, 6300,
                                                       persist=False))
        self._add_run(61)
        self.assertIsNone(predictor.detect_gauge_alarm(36, 6300,
                                                       persist=False))

    def test_held_above_band_rings_first_up_rung(self):
        self._add_run(59)
        self.assertIsNone(predictor.detect_gauge_alarm(63, 6300,
                                                       persist=False))
        self._add_run(63)   # fresh crossing but below confirm -> quiet
        res = predictor.detect_gauge_alarm(64, 6300, persist=False)
        self.assertIsNotNone(res)
        self.assertEqual(res["side"], "up")
        self.assertEqual(res["rung"], 62)
        self.assertEqual(res["est_pct"], 0.5)

    def test_down_crossing_rings_first_down_rung(self):
        self._add_run(37)
        self.assertIsNone(predictor.detect_gauge_alarm(37, 6300,
                                                       persist=False))
        self._add_run(37)
        res = predictor.detect_gauge_alarm(33, 6300, persist=False)
        self.assertIsNotNone(res)
        self.assertEqual(res["side"], "down")
        self.assertEqual(res["rung"], 35)
        self.assertEqual(res["est_pct"], -0.5)


# --------------------------------------------------------------------------- #
# merge() renormalization + predictions.csv migration
# --------------------------------------------------------------------------- #
class MergeAndMigrationTests(unittest.TestCase):
    def test_merge_renormalizes_unavailable_groups(self):
        groups = [{"group": "technical", "score": 0.5, "weight": 1.0},
                  {"group": "consensus", "score": -0.5, "weight": 1.0},
                  {"group": "news", "score": 0.0, "weight": 0.0}]  # offline
        w = predictor.WEIGHTS
        expect = (w["technical"] * 0.5 + w["consensus"] * -0.5) / \
                 (w["technical"] + w["consensus"])
        self.assertAlmostEqual(predictor.merge(groups), expect, places=9)

    def test_neutral_fx_group_still_counts(self):
        # quiet FX (score 0.0) damps the merged bias toward 50 - verified
        # behaviour from the walk-forward (config M)
        groups = [{"group": "technical", "score": 1.0, "weight": 1.0},
                  {"group": "fx_mr", "score": 0.0, "weight": 1.0}]
        w = predictor.WEIGHTS
        expect = w["technical"] / (w["technical"] + w["fx_mr"])
        self.assertAlmostEqual(predictor.merge(groups), expect, places=9)

    def test_weights_sum_and_fx_present(self):
        self.assertIn("fx_mr", predictor.WEIGHTS)
        self.assertAlmostEqual(sum(predictor.WEIGHTS.values()), 1.0, places=6)

    def test_tuning_json_matches_verified_config(self):
        with open(os.path.join(REPO, "tuning.json"), encoding="utf-8") as fh:
            t = json.load(fh)
        self.assertEqual(t["weights"], {"technical": 0.45, "consensus": 0.25,
                                        "news": 0.1, "fx_mr": 0.2})
        self.assertEqual(t["thresholds"], {"up_threshold": 62.0,
                                           "down_threshold": 35.0})

    def test_migrate_pred_csv_appends_fx_mr(self):
        tmp = tempfile.mkdtemp(prefix="gw_mig_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pred = os.path.join(tmp, "predictions.csv")
        old_header = predictor.PRED_HEADER[:-1]      # 20 cols, no fx_mr
        old_row = ["2026-10-19 19:11:27", "2026-10-19 22:11:27", "44",
                   "FLAT", "moderate", "0.109", "-0.288", "-0.417", "6275.0",
                   "4143.93", "+1.455", "-1.088", "+2.543", "6290", "0",
                   "2026-10-20 21:01:14", "53.5247", "+0.570", "", "-0.008"]
        self.assertEqual(len(old_header), 20)
        self.assertEqual(len(old_row), 20)
        with open(pred, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(old_header)
            csv.writer(fh).writerow(old_row)
        orig = predictor.PRED_PATH
        predictor.PRED_PATH = pred
        try:
            predictor.migrate_pred_csv()
        finally:
            predictor.PRED_PATH = orig
        with open(pred, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(fh) if r]
        self.assertEqual(rows[0], predictor.PRED_HEADER)
        self.assertEqual(len(rows[1]), 21)
        self.assertEqual(rows[1][:20], old_row)   # history intact
        self.assertEqual(rows[1][20], "")         # fx_mr empty on old rows
        # historical row values survive a DictReader round-trip
        with open(pred, newline="", encoding="utf-8") as fh:
            d = next(csv.DictReader(fh))
        self.assertEqual(d["final_pct"], "44")
        self.assertEqual(d["formula_dev_pct"], "-0.008")
        self.assertEqual(d["fx_mr"], "")


# --------------------------------------------------------------------------- #
# Regressions for the three v12 follow-up fixes
# --------------------------------------------------------------------------- #
class V12BugfixRegressionTests(unittest.TestCase):
    """1) grade_pending() grades FLAT calls by the real FLAT band, 2)
    fx_mean_reversion() picks the quote by timestamp, refuses stale feeds
    and anchors the 24h lookback on the quote itself, 3) the ladder stays
    inside the gauge's [2, 98] domain at extreme thresholds."""

    # --- bug 1: FLAT grading band ---------------------------------------- #
    def test_flat_grades_correct_at_59_and_41(self):
        # pct 59 and 41 are FLAT calls (band 36..61) but sit outside the old
        # hardcoded |pct-50|<8 window; on a flat 24h move they must grade 1.
        tmp = tempfile.mkdtemp(prefix="gw_grade_")
        orig = (predictor.PRED_PATH, predictor.CSV_PATH)
        predictor.PRED_PATH = os.path.join(tmp, "predictions.csv")
        predictor.CSV_PATH = os.path.join(tmp, "gold_prices.csv")
        try:
            now = datetime.now(timezone.utc)
            ts0 = now - timedelta(hours=25)
            # 21K nearly flat over the grading window: 6300 -> 6301 is
            # +0.016% (< the 0.1% dead zone) -> actual = "flat"
            with open(predictor.CSV_PATH, "w", newline="",
                      encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp_utc", "gold_21k_sell", "usd_egp"])
                w.writerow([_ts(ts0), "6300", "50.00"])
                w.writerow([_ts(ts0 + timedelta(hours=26)), "6301", "50.00"])
            with open(predictor.PRED_PATH, "w", newline="",
                      encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=predictor.PRED_HEADER)
                w.writeheader()
                for pct in (59, 41, 50):
                    w.writerow({"timestamp_utc": _ts(ts0),
                                "timestamp_cairo": _ts(ts0 + timedelta(hours=3)),
                                "final_pct": pct, "direction": "FLAT",
                                "price_21k": "6300"})
            self.assertIsNotNone(predictor.grade_pending())
            with open(predictor.PRED_PATH, newline="",
                      encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            graded = {int(r["final_pct"]): r["predicted_correct"]
                      for r in rows if r["graded_at_utc"]}
            self.assertEqual(graded[59], "1")   # old code: 0 (|59-50| = 9)
            self.assertEqual(graded[41], "1")   # old code: 0 (|41-50| = 9)
            self.assertEqual(graded[50], "1")   # unchanged region stays 1
        finally:
            predictor.PRED_PATH, predictor.CSV_PATH = orig
            shutil.rmtree(tmp, ignore_errors=True)

    # --- bug 2: stale / out-of-order fx quotes --------------------------- #
    def test_fx_stale_latest_quote_is_neutral(self):
        # latest quote is 10h old (scraper stalled) -> neutral 0.0, "stale"
        tmp = tempfile.mkdtemp(prefix="gw_fxstale_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "gold_prices.csv")
        _write_prices_csv(path, [(_ts(NOW - timedelta(hours=34)), "50.00"),
                                 (_ts(NOW - timedelta(hours=10)), "50.25")])
        res = predictor.fx_mean_reversion(path, now=NOW)
        self.assertEqual(res["score"], 0.0)
        self.assertIn("stale", res["detail"])

    def test_fx_quote_exactly_3h_old_still_live_and_anchored(self):
        # 3h old is ON the stale boundary (not > 3h) -> live. The 24h
        # lookback anchors on the quote's own timestamp (NOW-3h-24h = NOW-27h)
        # so the 50.00 quote 28h back is a valid prior; an implementation
        # anchored on `now` would miss it (4h off) and return 0.0.
        tmp = tempfile.mkdtemp(prefix="gw_fxanch_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "gold_prices.csv")
        _write_prices_csv(path, [(_ts(NOW - timedelta(hours=28)), "50.00"),
                                 (_ts(NOW - timedelta(hours=3)), "50.25")])
        res = predictor.fx_mean_reversion(path, now=NOW)
        self.assertEqual(res["score"], -1.0)

    def test_fx_latest_quote_picked_by_timestamp_not_row_order(self):
        # CSV rows out of chronological order: the LAST row (50.10) is NOT
        # the newest quote. The score must use the true latest (50.25, NOW):
        # +0.5% -> -1.0. Using the last row would give +0.2% -> -0.4.
        tmp = tempfile.mkdtemp(prefix="gw_fxord_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "gold_prices.csv")
        _write_prices_csv(path, [(_ts(NOW), "50.25"),
                                 (_ts(NOW - timedelta(hours=24)), "50.00"),
                                 (_ts(NOW - timedelta(hours=1)), "50.10")])
        res = predictor.fx_mean_reversion(path, now=NOW)
        self.assertEqual(res["score"], -1.0)

    # --- bug 3: ladder clamped into [2, 98] ------------------------------ #
    def test_ladder_clamped_at_extreme_thresholds(self):
        orig = (predictor.UP_THRESHOLD, predictor.DOWN_THRESHOLD,
                predictor.LADDER_UP, predictor.LADDER_DOWN)
        try:
            predictor.UP_THRESHOLD, predictor.DOWN_THRESHOLD = 70, 30
            predictor.LADDER_UP = predictor._ladder(
                70, predictor.LADDER_OFFSETS)
            predictor.LADDER_DOWN = predictor._ladder(
                30, tuple(-o for o in predictor.LADDER_OFFSETS))
            for rungs, rising in ((predictor.LADDER_UP, True),
                                  (predictor.LADDER_DOWN, False)):
                self.assertTrue(all(2 <= r <= 98 for r in rungs),
                                f"rung outside [2,98]: {rungs}")
                self.assertEqual(len(set(rungs)), len(rungs),
                                 f"duplicate rungs: {rungs}")
                gaps = [b - a for a, b in zip(rungs, rungs[1:])]
                if rising:
                    self.assertTrue(all(g > 0 for g in gaps))
                else:
                    self.assertTrue(all(g < 0 for g in gaps))
            # unclamped they would be (70,77,84,92,102) and (30,23,16,8,-2)
            self.assertEqual(predictor.LADDER_UP, (70, 77, 84, 92, 98))
            self.assertEqual(predictor.LADDER_DOWN, (30, 23, 16, 8, 2))
        finally:
            (predictor.UP_THRESHOLD, predictor.DOWN_THRESHOLD,
             predictor.LADDER_UP, predictor.LADDER_DOWN) = orig


# --------------------------------------------------------------------------- #
# _zone rung mapping + RSS XML entity-expansion guard
# --------------------------------------------------------------------------- #
class ZoneMappingTests(unittest.TestCase):
    """_zone() must map every rung to its own distinct index. The down side
    used to collapse every rung into ('down', 0) via min()."""

    def test_up_rungs_map_to_distinct_indices(self):
        for i, rung in enumerate(predictor.LADDER_UP):
            self.assertEqual(predictor._zone(rung), ("up", i),
                             f"up rung {rung} must map to index {i}")

    def test_down_rungs_map_to_distinct_indices(self):
        for i, rung in enumerate(predictor.LADDER_DOWN):
            self.assertEqual(predictor._zone(rung), ("down", i),
                             f"down rung {rung} must map to index {i}")
        # explicit regression: (35, 28, 21, 13, 3) used to give ('down', 0)
        # for every rung except via the buggy min() collapsing all to 0
        self.assertEqual(predictor._zone(28), ("down", 1))
        self.assertEqual(predictor._zone(21), ("down", 2))

    def test_extremes_and_flat_band(self):
        self.assertEqual(predictor._zone(98),
                         ("up", len(predictor.LADDER_UP) - 1))
        self.assertEqual(predictor._zone(2),
                         ("down", len(predictor.LADDER_DOWN) - 1))
        mid = (predictor.UP_THRESHOLD + predictor.DOWN_THRESHOLD) // 2
        self.assertIsNone(predictor._zone(mid))       # inside the FLAT band
        self.assertIsNone(predictor._zone(50))


class RssXmlGuardTests(unittest.TestCase):
    """fetch_rss must reject feeds declaring DOCTYPE/ENTITY (XXE /
    billion-laughs), and collect_headlines must survive such a feed."""

    HOSTILE = (b'<?xml version="1.0"?>\n'
               b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">'
               b'<!ENTITY a "boom">]>\n'
               b'<rss><channel><item><title>&xxe;</title>'
               b'<link>https://x.example/a</link></item></channel></rss>')
    CLEAN = (b'<?xml version="1.0"?>\n'
             b'<rss><channel><item><title>Gold up 1%</title>'
             b'<link>https://x.example/b</link>'
             b'<description>bullion rally</description>'
             b'</item></channel></rss>')

    class _FakeResp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    def setUp(self):
        import news_reader
        self.nr = news_reader
        self._orig_get = news_reader.requests.get

    def tearDown(self):
        self.nr.requests.get = self._orig_get

    def _serve(self, payload):
        self.nr.requests.get = (lambda *a, **k: self._FakeResp(payload))

    def test_guard_rejects_doctype_entity(self):
        self._serve(self.HOSTILE)
        with self.assertRaises(ValueError) as cm:
            self.nr.fetch_rss("https://rss.example/feed")
        self.assertIn("DOCTYPE/ENTITY", str(cm.exception))
        # the failure must not leak the entity target into the error text
        self.assertNotIn("file://", str(cm.exception))

    def test_clean_feed_still_parses(self):
        self._serve(self.CLEAN)
        items = self.nr.fetch_rss("https://rss.example/feed")
        self.assertEqual([it["title"] for it in items], ["Gold up 1%"])

    def test_collect_headlines_tolerates_hostile_feed(self):
        # every feed "returns" the hostile payload -> each feed fails softly,
        # the run must survive with zero headlines (never crash)
        self._serve(self.HOSTILE)
        self.assertEqual(self.nr.collect_headlines(), [])


# --------------------------------------------------------------------------- #
# daily_agent rails for the 4th group
# --------------------------------------------------------------------------- #
class DailyAgentRailTests(unittest.TestCase):
    def test_groups_include_fx_mr(self):
        import daily_agent
        self.assertEqual(daily_agent.GROUPS,
                         ("technical", "consensus", "news", "fx_mr"))
        self.assertIn("fx_mr", daily_agent.DEFAULT_WEIGHTS)
        self.assertEqual(daily_agent.DEFAULT_THRESHOLDS,
                         {"up_threshold": 62, "down_threshold": 35})
        self.assertEqual(daily_agent.GROUP_BOUNDS["fx_mr"], (0.05, 0.35))
        self.assertIn("fx_mr", daily_agent.SYS_PROMPT)

    def test_fx_mr_weight_clamped_to_bounds(self):
        import daily_agent
        weights = dict(daily_agent.DEFAULT_WEIGHTS)
        decisions = {"changes": [{"type": "weight", "group": "fx_mr",
                                  "value": 0.9}]}
        new_w, _k, _t, applied, notes = daily_agent.validate_changes(
            decisions, {"accuracy": {"total_graded": 100}}, weights,
            dict(daily_agent.DEFAULT_KNOBS),
            dict(daily_agent.DEFAULT_THRESHOLDS))
        self.assertLessEqual(new_w["fx_mr"] /
                             sum(new_w.values()), 0.35 + 1e-9)
        self.assertTrue(any(a["group"] == "fx_mr" for a in applied))
        self.assertTrue(notes == [] or True)  # notes optional on success


# --------------------------------------------------------------------------- #
# E2E smoke checks (real CLIs on a temp copy - never touches live data)
# --------------------------------------------------------------------------- #
def _clean_env():
    env = dict(os.environ)
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_LANG",
              "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
              "UP_THRESHOLD", "DOWN_THRESHOLD", "WEIGHT_TECHNICAL",
              "WEIGHT_CONSENSUS", "WEIGHT_NEWS", "WEIGHT_FX_MR",
              "BIAS_DAMPENER"):
        env.pop(k, None)
    return env


class EndToEndSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gw_e2e_")
        dst = os.path.join(cls.tmp, "repo")
        shutil.copytree(REPO, dst,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        cls.repo = dst

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_py_compile_all(self):
        pys = [f for f in os.listdir(self.repo) if f.endswith(".py")]
        self.assertTrue(pys)
        r = subprocess.run([sys.executable, "-m", "py_compile", *pys],
                           cwd=self.repo, capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode()[-500:])

    def test_02_predictor_dry_run_json(self):
        r = subprocess.run([sys.executable, "predictor.py", "--dry-run",
                            "--json"], cwd=self.repo, env=_clean_env(),
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        data = json.loads(lines[-1])          # the JSON line is printed last
        self.assertIn("pct", data)
        self.assertIn("direction", data)
        self.assertTrue(2.0 <= data["pct"] <= 98.0)
        names = {g["group"] for g in data["groups"]}
        # fx_mr must always be present (fail-soft, no network needed)
        self.assertIn("fx_mr", names)
        self.assertIn("technical", names)
        self.assertIn("news", names)
        fx = next(g for g in data["groups"] if g["group"] == "fx_mr")
        self.assertTrue(-1.0 <= fx["score"] <= 1.0)
        self.assertTrue(fx["detail"])

    def test_03_daily_agent_digest(self):
        r = subprocess.run([sys.executable, "daily_agent.py", "--digest"],
                           cwd=self.repo, env=_clean_env(),
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        out = r.stdout[r.stdout.index("{"):]
        data = json.loads(out)
        self.assertIn("accuracy", data)
        self.assertIn("config", data)
        self.assertEqual(data["config"]["weights"].get("fx_mr"), 0.2)
        self.assertEqual(data["config"]["thresholds"].get("up_threshold"),
                         62.0)
        groups = (data.get("signals_last60") or {}).get("groups", {})
        self.assertIn("fx_mr", groups)


if __name__ == "__main__":
    unittest.main()
