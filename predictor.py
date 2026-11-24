#!/usr/bin/env python3
"""
Prediction gauge orchestrator (v12).
==============================
Runs the four signal groups (technical, consensus, news, fx_mr), merges them
into one bias percentage, logs every prediction, self-grades yesterday's
calls, maintains the 7-day "levels map" from verified analyst levels, and
sends a compact Telegram message only when it is worth sending (gauge-ladder
alarm, sell-window milestone, direction flip, >=15-point move, first run of
the Cairo day, or --force).
Morning runs also prepend daily/weekly/monthly digests of past performance.

v12 additions (verified by a 60/40 walk-forward on the graded log, see
tuning.json / data/tuning_log.csv):
  - fx_mr group: 24h USD/EGP mean-reversion signal read from gold_prices.csv
    (the source site computes its 21K price from the parallel USD/EGP rate,
    so FX spikes partially revert and drag the local price along).
  - Rebalanced weights 45/25/10/20 (technical/consensus/news/fx_mr) and a
    stricter FLAT band (62/35) after the walk-forward.
  - Raw-bias dampener (x0.65) during Monday and Cairo 08:00-11:59, the two
    windows whose calls historically underperform.
  - Ladder rungs now derive from the thresholds (they sit a fixed offset
    beyond the FLAT band edge), so the FLAT band never rings alarms.

v11 additions:
  - SELL WINDOW milestone: flags when the 21K price rises a certain amount
    above the day's average price (yesterday's outlier-trimmed average during
    the first hours of the Cairo day) - the classic local "chance to sell".
  - Fresh alarms: the ladder alarm describes the run it fires in (no lag).
  - Anti-twitch: a weak crossing must clear its rung by LADDER_CONFIRM pts
    (or hold for 2 runs) before it rings; strong jumps ring instantly.
  - Alarm report card: past alarms are graded (data/alarms.csv) and scored.
  - Dual accuracy: direction-only accuracy shown next to the strict one.
  - Honest window wording + pace note (24h speed vs the usual pace).
  - Multi-recipient Telegram with per-recipient language (English/Arabic).
  - Self-service subscribers: anyone can /start the bot, pick English or
    Arabic, and get every report - the maintainer touches nothing
    (telegram_bot.py; data/subscribers.csv).

Files written (all committed by the workflow):
    data/predictions.csv     one row per run + grading columns
    data/levels_history.csv  every verified analyst level with timestamp
    data/alarms.csv          every alarm raised, with its later report card

Designed to run AFTER scraper.py in the same workflow so gold_prices.csv
is fresh. Never raises on partial failures: missing groups are skipped and
weights renormalized. Exits 0 unless everything failed.

Usage:
    python predictor.py                  # normal run
    python predictor.py --dry-run        # compute + print, write nothing
    python predictor.py --force          # send Telegram even if unchanged
    python predictor.py --json           # machine-readable output
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

import consensus
import local_premium
import news_reader
import technical

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "data", "gold_prices.csv")
PRED_PATH = os.path.join(BASE_DIR, "data", "predictions.csv")
LEVELS_PATH = os.path.join(BASE_DIR, "data", "levels_history.csv")
ALARMS_PATH = os.path.join(BASE_DIR, "data", "alarms.csv")
TUNING_PATH = os.path.join(BASE_DIR, "tuning.json")   # written by daily_agent.py

PRED_HEADER = ["timestamp_utc", "timestamp_cairo", "final_pct", "direction",
               "confidence", "technical", "consensus", "news", "price_21k",
               "global_xauusd", "local_chg_7d", "global_chg_7d", "divergence_7d",
               "price_24h_later", "predicted_correct", "graded_at_utc",
               "usdegp_official", "parallel_gap_pct", "real_premium_pct",
               "formula_dev_pct", "fx_mr"]  # fx_mr LAST: migrate_pred_csv pads
               # old rows by appending the new columns at the end
LEVELS_HEADER = ["timestamp_utc", "price", "unit", "type", "title"]
ALARMS_HEADER = ["timestamp_utc", "timestamp_cairo", "kind", "detail",
                 "est_pct", "price", "graded", "actual_pct", "verdict"]

def _env_weight(name, default):
    """Read a positive float weight from env; fall back to default on bad input."""
    try:
        v = float(os.environ.get(name, "").strip() or default)
        return v if v > 0 else default
    except ValueError:
        return default


def _tuned(section, key, default):
    """Read a value from tuning.json (written by daily_agent.py).

    Returns default when the file/section/key is missing or malformed.
    Callers keep env-var priority by using this as their _env_* default.
    """
    try:
        with open(TUNING_PATH, encoding="utf-8") as fh:
            v = (json.load(fh).get(section) or {}).get(key)
        if isinstance(v, (int, float)):
            return float(v)
    except (OSError, ValueError, AttributeError):
        pass
    return default


# Weights are env-tunable so recalibration never needs a code change.
# Since 2026-10-24 the daily AI agent (daily_agent.py) can also adjust
# them, via tuning.json - priority: env var > tuning.json > defaults.
# Weight rebalance (2026-11-24, verified by a read-only walk-forward on all
# 805 graded rows, 60/40 chronological IS/OOS split, no look-ahead): the
# source site COMPUTES its 21K price as global ounce x parallel-market
# USD/EGP, so the local technical series carries the parallel-dollar
# information global feeds cannot see. Per-group agreement with the real
# non-flat 24h move: technical 61.5% vs consensus 55.2% vs news 53.7%
# overall, and in the OOS window technical 71.7% vs consensus 59.0%. The
# rebalanced set (45/25/10) plus the fx_mr group (0.20) improved OOS strict
# accuracy 66.1% -> 70.2%, OOS direction accuracy 82.6% -> 94.3% and OOS
# alarm precision 70.4% -> 77.8% vs the then-current tuning. Defaults below
# match tuning.json; override via env vars without touching code.
def _weight(key, env_name, default):
    """Weight priority: env var (explicitly set) > tuning.json > default."""
    env = os.environ.get(env_name, "").strip()
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    return _tuned("weights", key, default)


WEIGHTS = {
    "technical": _weight("technical", "WEIGHT_TECHNICAL", 0.45),
    "consensus": _weight("consensus", "WEIGHT_CONSENSUS", 0.25),
    "news": _weight("news", "WEIGHT_NEWS", 0.10),
    "fx_mr": _weight("fx_mr", "WEIGHT_FX_MR", 0.20),
}


def _threshold(key, env_name, default, lo, hi):
    env = os.environ.get(env_name, "").strip()
    if env:
        try:
            return int(_env_float(env_name, default))
        except ValueError:
            pass
    return int(_clamp(_tuned("thresholds", key, default), lo, hi))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


UP_THRESHOLD = _threshold("up_threshold", "UP_THRESHOLD", 62, 55, 70)
DOWN_THRESHOLD = _threshold("down_threshold", "DOWN_THRESHOLD", 35, 30, 45)
if UP_THRESHOLD < DOWN_THRESHOLD + 10:   # sanity: keep a real FLAT band
    UP_THRESHOLD, DOWN_THRESHOLD = 62, 35
NOTIFY_DELTA = 15.0                            # points vs previous run
ACCURACY_WINDOW = 30                           # graded rows for the accuracy stat


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


DIGEST_WEEKDAY = _env_int("DIGEST_WEEKDAY", 5)  # weekly digest day, 0=Mon..6=Sun; 5=Saturday

# Anti-twitch: a crossing must clear its rung by this many points to ring
# instantly; a weaker crossing waits until the gauge HOLDS the rung for a
# second run. 0 = every crossing rings instantly (old behaviour).
LADDER_CONFIRM = _env_float("LADDER_CONFIRM",
                            _tuned("knobs", "ladder_confirm", 2.0))

# SELL WINDOW milestone: rings when 21K rises this far above the day's
# average price (percent, or absolute EGP line; 0 disables a knob).
MILESTONE_PCT = _env_float("MILESTONE_PCT",
                           _tuned("knobs", "milestone_pct", 0.5))
MILESTONE_EGP = _env_float("MILESTONE_EGP", 0.0)
# Until this many runs exist today (3 runs = first ~3 hours at hourly
# cadence), the baseline is yesterday's average with outliers trimmed.
MILESTONE_MIN_RUNS = _env_int("MILESTONE_MIN_RUNS", 3)

# Default message language for recipients without an explicit ":lang" tag.
TELEGRAM_DEFAULT_LANG = (os.environ.get("TELEGRAM_LANG", "en").strip().lower()
                         or "en")

# Gauge ladder: the gauge read as a price-o-meter. Each height the gauge
# climbs to (or falls to) is translated into the 21K move that height has
# meant. Rungs sit a fixed offset BEYOND the FLAT band edge (so the band
# itself never rings: with the default band 35-62 the first rungs are 62/35),
# follow the thresholds automatically, and each rings once per Cairo day on
# the run it crosses (fresh), guarded by the anti-twitch confirmation rule
# (LADDER_CONFIRM).
LADDER_OFFSETS = (0, 7, 14, 22, 32)   # points beyond the FLAT band edge


def _ladder(base, offsets, lo=2, hi=98):
    """Rungs derived from the band edge, clamped into the gauge's own
    [2, 98] domain (UP_THRESHOLD=70 must not produce a 102 rung, 30 not a
    -2 rung) and kept strictly monotonic + unique even after clamping."""
    return tuple(dict.fromkeys(min(hi, max(lo, base + o)) for o in offsets))


LADDER_UP = _ladder(UP_THRESHOLD, LADDER_OFFSETS)               # e.g. 62..94
LADDER_DOWN = _ladder(DOWN_THRESHOLD,
                      tuple(-o for o in LADDER_OFFSETS))        # e.g. 35..3
# Rough by-design meaning of each height (next-day 21K move, %) until your
# own log has enough samples (>=3) for the measured answer to take over.
# LADDER_EST_PCT[i] pairs with LADDER_OFFSETS[i] (signed per side).
LADDER_EST_PCT = (0.5, 0.9, 1.3, 1.8, 2.5)
# An alarm counts as "on target" in the report card when the real move
# reached at least this fraction of the promised move, same direction.
ALARM_HIT_FRACTION = 0.5

try:  # Cairo time for the daily-digest logic (fallback UTC+3, Egypt DST-aware)
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:  # noqa: BLE001
    CAIRO = timezone(timedelta(hours=3))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def cairo_now():
    return datetime.now(CAIRO)


def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def read_last_row(path):
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if len(rows) < 2:
        return None
    return dict(zip(rows[0], rows[-1], strict=False))


def append_row(path, header, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(header)
        writer.writerow([row.get(col, "") for col in header])


def migrate_pred_csv():
    """Pad an older predictions.csv if the header gained columns (safe no-op)."""
    if not os.path.exists(PRED_PATH):
        return
    with open(PRED_PATH, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if not rows or len(rows[0]) >= len(PRED_HEADER):
        return
    width = len(PRED_HEADER)
    rows = ([rows[0] + PRED_HEADER[len(rows[0]):]] +
            [r + [""] * (width - len(r)) for r in rows[1:]])
    with open(PRED_PATH, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def fmt_sign(x):
    return f"{x:+.2f}"


# --------------------------------------------------------------------------- #
# Signal groups
# --------------------------------------------------------------------------- #
FX_MR_MIN_CHG = 0.15     # % 24h USD/EGP move that counts as a real impulse
FX_MR_SATURATION = 0.5   # % move that saturates the signal at +/-1
FX_MR_TOL_HOURS = 2.0    # tolerance when locating the quote ~24h ago
FX_MR_STALE_HOURS = 3.0  # latest quote older than this = dead scraper feed


def fx_mean_reversion(csv_path=CSV_PATH, now=None):
    """fx_mr group: 24h USD/EGP mean-reversion signal in [-1, +1].

    The source site computes its 21K price from the (parallel-market)
    USD/EGP rate, so an FX spike partially mean-reverts and drags the local
    gold price with it. Score = clip(-chg_24h / 0.5) when the 24h USD/EGP
    move is >= 0.15%, else 0.0 (quiet FX = neutral, still weighted - a
    verified-neutral vote, matching the walk-forward).

    Strict no look-ahead: only quotes with timestamp <= `now` are read, and
    the "24h ago" quote is the nearest one within +/-2h of (current quote
    timestamp - 24h), so the delta is always a true ~24h window.
    The current quote is the one with the LATEST timestamp (not the last CSV
    row - row order is not guaranteed). If that quote is more than
    FX_MR_STALE_HOURS old, the feed is considered stale -> neutral 0.0.
    Fail-soft: missing or stale history -> score 0.0 (never raises).
    """
    result = {"group": "fx_mr", "score": 0.0, "weight": 1.0,
              "detail": "USD/EGP history unavailable -> neutral"}
    if not os.path.exists(csv_path):
        return result
    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(fh) if r]
        header = {name: i for i, name in enumerate(rows[0])}
        ts_i, fx_i = header.get("timestamp_utc", 0), header.get("usd_egp")
        if fx_i is None:
            return result
        now = now or datetime.now(timezone.utc)
        quotes = []
        for r in rows[1:]:
            try:
                ts = datetime.strptime(r[ts_i], "%Y-%m-%d %H:%M:%S") \
                    .replace(tzinfo=timezone.utc)
                fx = float(r[fx_i])
            except (ValueError, IndexError, TypeError):
                continue
            if ts <= now and fx > 0:
                quotes.append((ts, fx))
        if not quotes:
            return result
        cur_ts, cur = max(quotes, key=lambda x: x[0])
        age_h = (now - cur_ts).total_seconds() / 3600.0
        if age_h > FX_MR_STALE_HOURS:
            result["detail"] = (f"stale USD/EGP quote ({age_h:.1f}h old, "
                                f"scraper stalled?) -> neutral")
            return result
        target = cur_ts - timedelta(hours=24)
        prior, best_d = None, None
        for ts, fx in quotes:               # quotes are <= now by construction
            d = abs((ts - target).total_seconds())
            if d <= FX_MR_TOL_HOURS * 3600 and (best_d is None or d < best_d):
                prior, best_d = fx, d
        if prior is None:
            result["detail"] = "no USD/EGP quote ~24h ago (+/-2h) -> neutral"
            return result
        chg = (cur - prior) / prior * 100.0
        if abs(chg) < FX_MR_MIN_CHG:
            result["detail"] = (f"USD/EGP {cur:.2f} 24h {chg:+.2f}% (quiet) "
                                "-> neutral")
            return result
        score = max(-1.0, min(1.0, -chg / FX_MR_SATURATION))
        result["score"] = round(score, 3)
        result["detail"] = (f"USD/EGP {cur:.2f} 24h {chg:+.2f}% -> "
                            f"mean-reversion {score:+.2f}")
        return result
    except Exception as err:  # noqa: BLE001 - fx is fail-soft by design
        result["detail"] = f"fx_mr unavailable: {err}"
        return result


def collect_signals():
    """Run all four groups; skip (don't crash) the ones that fail."""
    groups = []
    try:
        groups.append(technical.compute_score(CSV_PATH))
    except Exception as err:  # noqa: BLE001
        print(f"  [gauge] technical group failed: {err}", file=sys.stderr)
    try:
        groups.append(consensus.fetch_consensus())
    except Exception as err:  # noqa: BLE001
        print(f"  [gauge] consensus group failed: {err}", file=sys.stderr)
    try:
        groups.append(news_reader.read_news(max_articles=6))
    except Exception as err:  # noqa: BLE001
        print(f"  [gauge] news group failed: {err}", file=sys.stderr)
    try:
        groups.append(fx_mean_reversion(CSV_PATH))
    except Exception as err:  # noqa: BLE001
        print(f"  [gauge] fx_mr group failed: {err}", file=sys.stderr)
    return groups


def merge(groups):
    """Weighted merge with renormalization -> (final_score, available map).

    Groups that are unavailable (absent, or weight <= 0) are renormalized
    away; a present group with score 0.0 (e.g. quiet FX) still counts.
    """
    num, den = 0.0, 0.0
    for g in groups:
        w = WEIGHTS.get(g["group"], 0.0) * g.get("weight", 1.0)
        if g.get("weight", 0) <= 0:
            continue
        num += w * g["score"]
        den += w
    return (num / den) if den > 0 else 0.0


# Raw-bias dampener: Monday and Cairo 08:00-11:59 runs historically
# underperform (strict accuracy 47.0% / 51.5% vs 59.2% otherwise, all 805
# graded rows), so their raw merged bias is scaled toward neutral. Verified
# in the same walk-forward as the weights (OOS direction 91.5% -> 92.5%,
# IS direction 66.9% -> 69.5%, alarm precision up in both windows).
BIAS_DAMPENER = _env_float("BIAS_DAMPENER", 0.65)


def dampen_raw_bias(bias, dt=None):
    """Scale the raw merged bias during weak windows; 1.0 disables."""
    if BIAS_DAMPENER >= 1.0:
        return bias
    dt = dt or cairo_now()
    if dt.strftime("%a") == "Mon" or 8 <= dt.hour < 12:
        return bias * BIAS_DAMPENER
    return bias


def confidence_label(groups):
    """Agreement heuristic: same sign everywhere -> strong, spread -> low."""
    scores = [g["score"] for g in groups if g.get("weight", 0) > 0]
    if len(scores) < 2:
        return "low"
    same_sign = all(s >= 0 for s in scores) or all(s <= 0 for s in scores)
    spread = max(scores) - min(scores)
    if same_sign and spread < 0.6:
        return "strong"
    if spread < 0.9:
        return "moderate"
    return "low"


# --------------------------------------------------------------------------- #
# Grading: compare old predictions with what actually happened 24h later
# --------------------------------------------------------------------------- #
def _main_price_series():
    """(timestamps, 21k sell prices) from the primary CSV, for grading."""
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}
    ts_i, p21_i = idx.get("timestamp_utc", 0), idx.get("gold_21k_sell")
    out = []
    for r in rows[1:]:
        try:
            ts = datetime.strptime(r[ts_i], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            price = float(r[p21_i])
            out.append((ts, price))
        except (ValueError, IndexError, TypeError):
            continue
    return out


def grade_pending():
    """Fill price_24h_later / predicted_correct for rows older than 24h."""
    if not (os.path.exists(PRED_PATH) and os.path.exists(CSV_PATH)):
        return None
    series = _main_price_series()
    if len(series) < 2:
        return None
    with open(PRED_PATH, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return None
    header = rows[0]
    col = {name: i for i, name in enumerate(header)}
    changed, now = False, datetime.now(timezone.utc)
    for r in rows[1:]:
        if col["graded_at_utc"] < len(r) and r[col["graded_at_utc"]]:
            continue
        try:
            ts = datetime.strptime(r[col["timestamp_utc"]],
                                   "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
        if now - ts < timedelta(hours=24):
            continue
        target = ts + timedelta(hours=24)
        future = next((p for t, p in series if t >= target), None)
        if future is None:
            continue
        try:
            called = float(r[col["price_21k"]])
            pct = float(r[col["final_pct"]])
        except (ValueError, IndexError):
            continue
        move_pct = (future - called) / called * 100.0
        predicted_up = pct > 50.0
        actual = "up" if move_pct > 0.1 else ("down" if move_pct < -0.1 else "flat")
        if actual == "flat":
            # A FLAT call means the gauge landed inside the FLAT band; grade
            # it by the same band the gauge used (not a hardcoded +/-8 window
            # around 50), so 36..42 and 58..61 also grade correct on flat days.
            correct = DOWN_THRESHOLD < pct < UP_THRESHOLD
        else:
            correct = (move_pct > 0) == predicted_up
        r[col["price_24h_later"]] = f"{future:.0f}"
        r[col["predicted_correct"]] = "1" if correct else "0"
        r[col["graded_at_utc"]] = now_utc_str()
        changed = True
    if changed:
        with open(PRED_PATH, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
    return _accuracy()


def _accuracy():
    """Rolling accuracy over the last ACCURACY_WINDOW graded rows.

    Returns both scores:
      strict    - the existing rule (direction AND landing window matter)
      direction - among UP/DOWN calls only: did the price move that way at all
    """
    if not os.path.exists(PRED_PATH):
        return None
    with open(PRED_PATH, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return None
    col = {name: i for i, name in enumerate(rows[0])}
    graded = [r for r in rows[1:] if col["graded_at_utc"] < len(r) and r[col["graded_at_utc"]]]
    recent = graded[-ACCURACY_WINDOW:]
    if not recent:
        return None
    hits = sum(1 for r in recent if r[col["predicted_correct"]] == "1")
    out = {"hits": hits, "n": len(recent), "pct": round(100.0 * hits / len(recent))}
    # direction-only score over non-FLAT calls (price move vs called side)
    d_hits = d_n = 0
    for r in recent:
        direction = col["direction"] < len(r) and r[col["direction"]]
        if direction not in ("UP", "DOWN"):
            continue
        try:
            called = float(r[col["price_21k"]])
            later = float(r[col["price_24h_later"]])
        except (ValueError, KeyError, TypeError):
            continue
        if not called:
            continue
        move = (later - called) / called
        good = move > 0 if direction == "UP" else move < 0
        d_n += 1
        d_hits += 1 if good else 0
    if d_n:
        out["dir_hits"], out["dir_n"] = d_hits, d_n
        out["dir_pct"] = round(100.0 * d_hits / d_n)
    return out


# --------------------------------------------------------------------------- #
# Levels map (7-day aggregation of verified levels)
# --------------------------------------------------------------------------- #
def update_levels(levels):
    if not levels:
        return
    for lvl in levels:
        append_row(LEVELS_PATH, LEVELS_HEADER, {
            "timestamp_utc": now_utc_str(), "price": lvl["price"],
            "unit": lvl["unit"], "type": lvl["type"], "title": lvl["title"]})


def summarize_levels(current_21k, lang="en"):
    """Most-cited levels over the last 7 days, per (unit, type)."""
    if not os.path.exists(LEVELS_PATH):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    with open(LEVELS_PATH, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if len(rows) < 2:
        return []
    col = {name: i for i, name in enumerate(rows[0])}
    counts = {}
    for r in rows[1:]:
        if len(r) <= max(col.values()) or r[col["timestamp_utc"]] < cutoff:
            continue
        try:
            key = (r[col["unit"]], r[col["type"]], round(float(r[col["price"]])))
        except (ValueError, KeyError):
            continue
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
    parts = []
    for (unit, ltype, price), n in ranked:
        side = ""
        if unit == "EGP/gram" and current_21k:
            dist = (price - current_21k) / current_21k * 100.0
            side = " " + T(lang, "lvl_fromnow", d=dist)
        unit_word = (T(lang, "lvl_gram") if "gram" in unit
                     else T(lang, "lvl_ounce") if "ounce" in unit
                     else unit.split("/")[1])
        type_word = (T(lang, "lvl_" + ltype) if ltype in ("target", "support", "resistance")
                     else ltype)
        parts.append(f"{price:,} {unit_word} {type_word} x{n}{side}")
    return parts


# --------------------------------------------------------------------------- #
# Telegram (multi-recipient, per-recipient language)
# --------------------------------------------------------------------------- #
# TELEGRAM_CHAT_ID accepts a comma list. Each entry is "chat_id" or
# "chat_id:lang" where lang is en / ar / both. Entries without a tag use
# TELEGRAM_LANG (default en).  Example: "111222333:ar, 444555666"
def _recipients():
    raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        cid, _, tag = part.partition(":")
        lang = (tag or TELEGRAM_DEFAULT_LANG).strip().lower()
        if lang not in ("en", "ar", "both"):
            lang = "en"
        cid = cid.strip()
        if cid:
            out.append((cid, lang))
    # self-service subscribers (v11.1): merge data/subscribers.csv rows;
    # env entries keep priority on chat-id conflicts. Fail-soft: onboarding
    # problems can never break the report pipeline.
    try:
        import telegram_bot
        out = telegram_bot.merge_recipients(out)
    except Exception as err:  # noqa: BLE001
        print(f"  [telegram] subscriber merge skipped: {err}", file=sys.stderr)
    return out


def send_telegram(messages):
    """messages: {"en": text, "ar": text} - each recipient gets its language.

    Never raises past the caller's fail-soft wrapper; returns True if any
    delivery succeeded.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    recips = _recipients()
    if not (token and recips):
        print("  [telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -> skipped")
        return False
    ok = False
    for cid, lang in recips:
        for lg in ((lang,) if lang != "both" else ("en", "ar")):
            text = messages.get(lg) or messages.get("en")
            if not text:
                continue
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": cid, "text": text,
                          "disable_web_page_preview": True}, timeout=15)
                resp.raise_for_status()
                print(f"  [telegram] delivered to {cid} ({lg})")
                ok = True
            except Exception as err:  # noqa: BLE001 - one chat failing = others still get it
                print(f"  [telegram] delivery to {cid} ({lg}) failed: {err}",
                      file=sys.stderr)
    return ok


# --------------------------------------------------------------------------- #
# Language: every message string via T(lang, key, **kw); missing -> English
# --------------------------------------------------------------------------- #
TRANSLATIONS = {
    "ladder_up": {"en": "GAUGE ALARM: gauge just climbed to {gauge:.0f}% ({levels})",
                  "ar": "تنبيه المؤشر: المؤشر صعد الآن إلى {gauge:.0f}% ({levels})"},
    "ladder_down": {"en": "GAUGE ALARM: gauge just fell to {gauge:.0f}% ({levels})",
                    "ar": "تنبيه المؤشر: المؤشر هبط الآن إلى {gauge:.0f}% ({levels})"},
    "levels_new_one": {"en": "1 new level today", "ar": "مستوى جديد واحد اليوم"},
    "levels_new_many": {"en": "{n} new levels today", "ar": "{n} مستويات جديدة اليوم"},
    "meaning": {"en": "Levels like this have meant: 21K moves about {egp} ({pct:+.1f}%) "
                      "- usually lands within about a day, sometimes within hours [{src}]",
                "ar": "قراءات كهذه كانت تعني تاريخيًا: 21ك يتحرك نحو {egp} ({pct:+.1f}%) "
                      "— يصل عادة خلال يوم تقريبًا، وأحيانًا خلال ساعات [{src}]"},
    "src_rough": {"en": "rough estimate", "ar": "تقدير مبدئي"},
    "src_measured": {"en": "measured from {n} runs in your log",
                     "ar": "محسوب من {n} تشغيل في سجلك"},
    "pace": {"en": "Pace check: the last 24h 21K moved {egp} ({pct:+.2f}%) - {word} "
                   "the usual pace for this height",
             "ar": "فحص السرعة: آخر 24 ساعة تحرك 21ك بمقدار {egp} ({pct:+.2f}%) — {word} "
                   "الوتيرة المعتادة لهذا الارتفاع"},
    "pace_ahead": {"en": "ahead of", "ar": "أسرع من"},
    "pace_line": {"en": "in line with", "ar": "في حدود"},
    "pace_against": {"en": "against", "ar": "عكس"},
    "sell_title": {"en": "SELL WINDOW: 21K {price:,.0f} is {delta:+,.0f} EGP "
                         "({pct:+.2f}%) above {src}",
                   "ar": "فرصة بيع: سعر 21ك {price:,.0f} أعلى من {src} "
                         "بمقدار {delta:+,.0f} جنيه ({pct:+.2f}%)"},
    "sell_src_today": {"en": "today's average", "ar": "متوسط اليوم"},
    "sell_src_yday": {"en": "yesterday's average (outliers trimmed)",
                      "ar": "متوسط الأمس (بعد استبعاد القيم الشاذة)"},
    "sell_body": {"en": "Local spikes like this are often short-lived - if you were "
                        "planning to sell, this is the kind of moment.",
                  "ar": "قفزات كهذه غالبًا لا تدوم طويلًا — إذا كنت تنوي البيع "
                        "فهذا من لحظاتها المناسبة."},
    "sell_high": {"en": "New 7-day high.", "ar": "أعلى سعر في 7 أيام."},
    "report_alarms": {"en": "Alarm score: {hits} of {n} recent alarms on target",
                      "ar": "تقييم التنبيهات: {hits} من {n} من التنبيهات الحديثة على الهدف"},
    "report_sell": {"en": "Sell windows: {hits} of {n} would have beat waiting 24h",
                    "ar": "نوافذ البيع: {hits} من {n} كانت ستتفوق على الانتظار 24 ساعة"},
    "digest_daily": {"en": "Daily digest - {date} ({n} runs)",
                     "ar": "ملخص اليوم - {date} ({n} تشغيل)"},
    "digest_weekly": {"en": "Weekly digest - {a} -> {b} ({n} runs)",
                      "ar": "ملخص الأسبوع - {a} إلى {b} ({n} تشغيل)"},
    "digest_monthly": {"en": "Monthly digest - {month} ({n} runs)",
                       "ar": "ملخص الشهر - {month} ({n} تشغيل)"},
    "dg_gauge": {"en": "- gauge: open {a:.0f}% -> close {b:.0f}% (low {l:.0f}, high {h:.0f})",
                 "ar": "- المؤشر: افتتاح {a:.0f}% → إغلاق {b:.0f}% (أدنى {l:.0f}، أعلى {h:.0f})"},
    "dg_direction": {"en": "- direction: {path}", "ar": "- الاتجاه: {path}"},
    "dg_direction_all": {"en": "- direction: {d} all period",
                         "ar": "- الاتجاه: {d} طوال الفترة"},
    "dg_direction_n": {"en": "- direction: {a} -> ... -> {b} ({k} changes)",
                       "ar": "- الاتجاه: {a} → ... → {b} ({k} تغييرات)"},
    "dg_21k": {"en": "- 21K: {a:,.0f} -> {b:,.0f} EGP ({d:+,.0f}, {p:+.2f}%)",
               "ar": "- 21ك: {a:,.0f} → {b:,.0f} جنيه ({d:+,.0f}، {p:+.2f}%)"},
    "dg_xau": {"en": "- XAU/USD: {a:,.2f} -> {b:,.2f} ({d:+,.2f}, {p:+.2f}%)",
               "ar": "- الأوقية العالمية: {a:,.2f} → {b:,.2f} ({d:+,.2f}، {p:+.2f}%)"},
    "dg_div": {"en": "- local-vs-global: {a:+.2f} -> {b:+.2f} pts (max {m:+.2f})",
               "ar": "- المحلي مقابل العالمي: {a:+.2f} → {b:+.2f} نقطة (الأقصى {m:+.2f})"},
    "dg_gap": {"en": "- parallel gap: {a:+.2f}% -> {b:+.2f}%",
               "ar": "- فجوة الدولار الموازي: {a:+.2f}% → {b:+.2f}%"},
    "dg_bigday": {"en": "- biggest 21K day move: {pct:+.2f}% on {d}",
                  "ar": "- أكبر حركة يومية لـ21ك: {pct:+.2f}% في {d}"},
    "dg_grading": {"en": "- grading: {ok}/{n} calls correct",
                   "ar": "- التقييم: {ok}/{n} من التوقعات صحيحة"},
    "rightnow": {"en": "Right now:", "ar": "الآن:"},
    "gauge_line": {"en": "Gold gauge: {pct}% {direction} ({conf})",
                   "ar": "مؤشر الذهب: {pct}% {direction} (ثقة {conf})"},
    "change_line": {"en": "Change: {delta} pts since last check",
                    "ar": "التغير: {delta} نقطة منذ آخر فحص"},
    "change_dry": {"en": "n/a (dry run)", "ar": "غير متاح (تجربة)"},
    "change_first": {"en": "first logged run - baseline saved",
                     "ar": "أول تسجيل — تم حفظ نقطة البداية"},
    "sig_technical": {"en": "- technical: {detail}", "ar": "- الفني: {detail}"},
    "sig_consensus": {"en": "- consensus: {detail}", "ar": "- الإجماع العالمي: {detail}"},
    "sig_news": {"en": "- news: {detail}", "ar": "- الأخبار: {detail}"},
    "sig_fx_mr": {"en": "- fx (USD/EGP 24h mean-reversion): {detail}",
                  "ar": "- الدولار (ارتداد USD/EGP خلال 24 ساعة): {detail}"},
    "div_line": {"en": "- local-vs-global (7d): 21K {a:+.2f}% | XAU/USD {b:+.2f}% | spread {s:+.2f} pts",
                  "ar": "- المحلي مقابل العالمي (7 أيام): 21ك {a:+.2f}% | الأوقية {b:+.2f}% | الفارق {s:+.2f} نقطة"},
    "dir_UP": {"en": "UP", "ar": "صاعد"},
    "dir_DOWN": {"en": "DOWN", "ar": "هابط"},
    "dir_FLAT": {"en": "FLAT", "ar": "مستقر"},
    "conf_strong": {"en": "strong", "ar": "قوية"},
    "conf_moderate": {"en": "moderate", "ar": "متوسطة"},
    "conf_low": {"en": "low", "ar": "ضعيفة"},
    "levels_head": {"en": "Levels (7d): {parts}", "ar": "المستويات (7 أيام): {parts}"},
    "lvl_target": {"en": "target", "ar": "هدف"},
    "lvl_support": {"en": "support", "ar": "دعم"},
    "lvl_resistance": {"en": "resistance", "ar": "مقاومة"},
    "lvl_gram": {"en": "gram", "ar": "جرام"},
    "lvl_ounce": {"en": "ounce", "ar": "أوقية"},
    "lvl_fromnow": {"en": "({d:+.1f}% from now)", "ar": "({d:+.1f}% من الآن)"},
    "accuracy_both": {"en": "Accuracy: direction {d}% ({dn} UP/DOWN calls) | "
                            "strict {s}% ({n} graded calls)",
                      "ar": "الدقة: الاتجاه {d}% ({dn} توقع صاعد/هابط) | "
                            "الصارمة {s}% ({n} توقع مُقيَّم)"},
    "accuracy_strict": {"en": "Gauge accuracy ({n} graded calls): {pct}%",
                        "ar": "دقة المؤشر ({n} توقع مُقيَّم): {pct}%"},
    "bias": {"en": "Bias gauge, not financial advice.",
             "ar": "مؤشر استرشادي وليس نصيحة استثمارية."},
}


def T(lang, key, **kw):
    """Translate one message string; falls back to English, then the key."""
    entry = TRANSLATIONS.get(key) or {}
    tmpl = entry.get(lang) or entry.get("en") or key
    try:
        return tmpl.format(**kw)
    except (KeyError, IndexError, ValueError):
        return tmpl


def _dir_word(lang, direction):
    return T(lang, "dir_" + str(direction)) if direction else "?"


def _conf_word(lang, conf):
    key = "conf_" + str(conf) if conf in ("strong", "moderate", "low") else "conf_low"
    return T(lang, key)


# --------------------------------------------------------------------------- #
# Daily / weekly / monthly digests
# --------------------------------------------------------------------------- #
def _load_pred_rows():
    """Read predictions.csv -> (column map, data rows); (None, None) if empty."""
    if not os.path.exists(PRED_PATH):
        return None, None
    with open(PRED_PATH, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if len(rows) < 2:
        return None, None
    return {name: i for i, name in enumerate(rows[0])}, rows[1:]


def _val(r, col, name):
    i = col.get(name)
    return r[i] if i is not None and i < len(r) and r[i] != "" else None


def _num(r, col, name):
    try:
        return float(_val(r, col, name))
    except (TypeError, ValueError):
        return None


def _digest_core(day, col, title, lang="en"):
    """Shared digest body over a list of prediction rows -> lines."""
    out = [title]

    def series(name):
        return [x for x in (_num(r, col, name) for r in day) if x is not None]

    g = series("final_pct")
    if g:
        out.append(T(lang, "dg_gauge", a=g[0], b=g[-1], l=min(g), h=max(g)))
    path, last_d = [], None
    for r in day:
        d = _val(r, col, "direction")
        t = (_val(r, col, "timestamp_cairo") or "")[11:16]
        if d and d != last_d:
            path.append(f"{d} {t}" if t else str(d))
            last_d = d
    if len(path) > 6:  # long windows: compress the hops
        out.append(T(lang, "dg_direction_n", a=path[0], b=path[-1], k=len(path) - 1))
    elif len(path) > 1:
        out.append(T(lang, "dg_direction", path=" -> ".join(path)))
    elif path:
        out.append(T(lang, "dg_direction_all", d=path[0]))
    p = series("price_21k")
    if p:
        out.append(T(lang, "dg_21k", a=p[0], b=p[-1], d=p[-1] - p[0],
                     p=(p[-1] - p[0]) / p[0] * 100))
    x = series("global_xauusd")
    if x:
        out.append(T(lang, "dg_xau", a=x[0], b=x[-1], d=x[-1] - x[0],
                     p=(x[-1] - x[0]) / x[0] * 100))
    d7 = series("divergence_7d")
    if d7:
        out.append(T(lang, "dg_div", a=d7[0], b=d7[-1], m=max(d7, key=abs)))
    gp = series("parallel_gap_pct")
    if gp:
        out.append(T(lang, "dg_gap", a=gp[0], b=gp[-1]))
    return out


def _grading_line(col, rows, want, lang="en"):
    """want = set of UTC-date prefixes (or a single string) for graded rows."""
    prefixes = {want} if isinstance(want, str) else want
    graded = [r for r in rows
              if (_val(r, col, "graded_at_utc") or "")[:10] in prefixes]
    if graded:
        ok = sum(1 for r in graded if _val(r, col, "predicted_correct") == "1")
        return T(lang, "dg_grading", ok=ok, n=len(graded))
    return None


def _day_move_line(day, col, lang="en"):
    """Biggest single-Cairo-day 21K move within the window (weekly/monthly)."""
    per = {}
    for r in day:
        d = (_val(r, col, "timestamp_cairo") or "")[:10]
        v = _num(r, col, "price_21k")
        if d and v is not None:
            per.setdefault(d, []).append(v)
    moves = []
    for d, vals in per.items():
        if len(vals) >= 2 and vals[0]:
            pct = (vals[-1] - vals[0]) / vals[0] * 100
            moves.append((abs(pct), pct, d))
    if not moves:
        return None
    _, pct, d = max(moves)
    return T(lang, "dg_bigday", pct=pct, d=d)


def _filter_day(rows, col, dates):
    dates = {dates} if isinstance(dates, str) else dates
    return [r for r in rows
            if (_val(r, col, "timestamp_cairo") or "")[:10] in dates]


def build_daily_digest(lang="en"):
    """Yesterday's runs (Cairo)."""
    col, rows = _load_pred_rows()
    if not rows:
        return None
    yest = (cairo_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    day = _filter_day(rows, col, yest)
    if not day:
        return None
    out = _digest_core(day, col,
                       T(lang, "digest_daily", date=yest, n=len(day)), lang)
    gl = _grading_line(col, rows, yest, lang)
    if gl:
        out.append(gl)
    return out


def build_weekly_digest(lang="en"):
    """Rolling last-7-Cairo-days window ending yesterday."""
    col, rows = _load_pred_rows()
    if not rows:
        return None
    dates = {(cairo_now() - timedelta(days=k)).strftime("%Y-%m-%d")
             for k in range(1, 8)}
    day = _filter_day(rows, col, dates)
    if not day:
        return None
    out = _digest_core(day, col,
                       T(lang, "digest_weekly", a=min(dates), b=max(dates),
                         n=len(day)), lang)
    mv = _day_move_line(day, col, lang)
    if mv:
        out.append(mv)
    gl = _grading_line(col, rows, dates, lang)
    if gl:
        out.append(gl)
    return out


def build_monthly_digest(lang="en"):
    """The previous calendar Cairo month."""
    col, rows = _load_pred_rows()
    if not rows:
        return None
    first_this = cairo_now().strftime("%Y-%m-01")
    prev_end = datetime.strptime(first_this, "%Y-%m-%d") - timedelta(days=1)
    ym = prev_end.strftime("%Y-%m")
    day = [r for r in rows
           if (_val(r, col, "timestamp_cairo") or "").startswith(ym)]
    if not day:
        return None
    out = _digest_core(day, col,
                       T(lang, "digest_monthly",
                         month=prev_end.strftime("%B %Y"), n=len(day)), lang)
    mv = _day_move_line(day, col, lang)
    if mv:
        out.append(mv)
    graded = [r for r in rows
              if (_val(r, col, "graded_at_utc") or "").startswith(ym)]
    if graded:
        ok = sum(1 for r in graded if _val(r, col, "predicted_correct") == "1")
        out.append(T(lang, "dg_grading", ok=ok, n=len(graded)))
    return out


# --------------------------------------------------------------------------- #
# Gauge ladder alarm (gauge height translated into 21K moves)
# --------------------------------------------------------------------------- #
def _ladder_points(col, rows):
    """[(dt, gauge, price)] sorted by time; rows without gauge dropped."""
    pts = []
    for r in rows:
        ts = _val(r, col, "timestamp_cairo")
        g = _num(r, col, "final_pct")
        p = _num(r, col, "price_21k")
        if not ts or g is None:
            continue
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                pts.append((datetime.strptime(ts, fmt), g, p))
                break
            except ValueError:
                continue
    pts.sort(key=lambda x: x[0])
    return pts


def _zone(gauge):
    """(side, rung_index) for the extreme rung the gauge has reached.

    Index (not the absolute rung value) keys the measured stats, so the
    buckets stay stable when the thresholds - and with them the rungs -
    change over time. None = inside the FLAT band.
    """
    if gauge >= LADDER_UP[0]:
        return "up", max(i for i, r in enumerate(LADDER_UP) if r <= gauge)
    if gauge <= LADDER_DOWN[0]:
        return "down", max(i for i, r in enumerate(LADDER_DOWN) if r >= gauge)
    return None


def _gauge_move_stats(pts):
    """Gauge zone -> (avg next-day 21K move %, samples) from the own log.

    Zone = (side, rung index): the highest up-rung reached (or lowest
    down-rung). 'Next-day move' = the 21K price 21-36h later vs now - the
    same reality check the self-grading loop uses.
    """
    buckets = {}
    for ts, g, p in pts:
        if p is None:
            continue
        nxt = next(((t2, p2) for t2, _, p2 in pts
                    if t2 > ts and p2 is not None
                    and 21.0 <= (t2 - ts).total_seconds() / 3600.0 <= 36.0),
                   None)
        if not nxt:
            continue
        move = (nxt[1] - p) / p * 100.0
        zone = _zone(g)
        if zone:
            buckets.setdefault(zone, []).append(move)
    return {z: (sum(v) / len(v), len(v)) for z, v in buckets.items()}


def _rung_est(side, idx):
    """Signed promised move (%) for a ladder zone, before measuring."""
    return LADDER_EST_PCT[idx] * (1 if side == "up" else -1)


def _ladder_meaning(side, idx, stats, price, lang="en"):
    """(pct, egp, source): what a ladder height has meant for 21K next day."""
    zone = (side, idx)
    if zone in stats and stats[zone][1] >= 3:
        pct, n = stats[zone]
        src = T(lang, "src_measured", n=n)
    else:
        pct = _rung_est(side, idx)
        src = T(lang, "src_rough")
    egp = pct / 100.0 * price if price else None
    return pct, egp, src


# --------------------------------------------------------------------------- #
# Alarms log (data/alarms.csv): every alarm + its later report card
# --------------------------------------------------------------------------- #
def _load_alarms():
    if not os.path.exists(ALARMS_PATH):
        return []
    with open(ALARMS_PATH, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if len(rows) < 2:
        return []
    col = {name: i for i, name in enumerate(rows[0])}
    return [{k: (r[i] if i < len(r) else "") for k, i in col.items()}
            for r in rows[1:]]


def _alarms_rung_today(kind):
    """Details of `kind` alarms already raised today (Cairo) - for dedup."""
    today = cairo_now().strftime("%Y-%m-%d")
    return {a.get("detail") for a in _load_alarms()
            if a.get("kind") == kind
            and (a.get("timestamp_cairo") or "")[:10] == today}


def _append_alarm(kind, detail, est_pct, price, persist=True):
    """Log one alarm. persist=False in dry-run (preview only, no file)."""
    if not persist:
        return
    append_row(ALARMS_PATH, ALARMS_HEADER, {
        "timestamp_utc": now_utc_str(),
        "timestamp_cairo": cairo_now().strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind, "detail": detail,
        "est_pct": (f"{est_pct:+.2f}" if est_pct is not None else ""),
        "price": (f"{price:.0f}" if price else ""),
        "graded": "", "actual_pct": "", "verdict": ""})


def _grade_alarms():
    """Fill verdicts on alarms old enough to judge (21-36h later price).

    gauge alarms:  hit = the real move reached >= half the promised move,
                   same direction (ALARM_HIT_FRACTION).
    milestones:    beat = selling at the flag beat holding for 24h
                   (price afterwards fell back below the flagged price).
    """
    if not os.path.exists(ALARMS_PATH):
        return
    series = _main_price_series() if os.path.exists(CSV_PATH) else []
    if len(series) < 2:
        return
    with open(ALARMS_PATH, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return
    col = {name: i for i, name in enumerate(rows[0])}
    now = datetime.now(timezone.utc)
    changed = False
    for r in rows[1:]:
        if col["graded"] < len(r) and r[col["graded"]]:
            continue
        try:
            ts = datetime.strptime(r[col["timestamp_utc"]],
                                   "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            price = float(r[col["price"]])
        except (ValueError, IndexError, TypeError):
            continue
        age_h = (now - ts).total_seconds() / 3600.0
        if age_h < 21:
            continue
        future = next((p for t, p in series
                       if 21.0 <= (t - ts).total_seconds() / 3600.0 <= 36.0), None)
        if future is None:
            if age_h > 48:  # window passed with no price -> close it out
                r[col["graded"]] = "1"
                r[col["verdict"]] = "n/a"
                changed = True
            continue
        try:
            est = float(r[col["est_pct"]]) if r[col["est_pct"]] else None
        except ValueError:
            est = None
        actual = (future - price) / price * 100.0
        kind = r[col["kind"]]
        if kind == "milestone":
            verdict = "beat" if actual < 0 else "held"
        elif est is None or abs(est) < 1e-9:
            verdict = "n/a"
        else:
            need = ALARM_HIT_FRACTION * est
            verdict = "hit" if (actual >= need if est > 0 else actual <= need) else "miss"
        r[col["graded"]] = "1"
        r[col["actual_pct"]] = f"{actual:+.2f}"
        r[col["verdict"]] = verdict
        changed = True
    if changed:
        with open(ALARMS_PATH, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)


def alarm_report_lines(lang="en"):
    """Report card over the last 5 graded alarms of each kind."""
    alarms = [a for a in _load_alarms() if a.get("graded") == "1"]
    out = []
    ga = [a for a in alarms if a.get("kind") == "gauge"][-5:]
    if ga:
        hits = sum(1 for a in ga if a.get("verdict") == "hit")
        out.append(T(lang, "report_alarms", hits=hits, n=len(ga)))
    ms = [a for a in alarms if a.get("kind") == "milestone"][-5:]
    if ms:
        beats = sum(1 for a in ms if a.get("verdict") == "beat")
        out.append(T(lang, "report_sell", hits=beats, n=len(ms)))
    return out


# --------------------------------------------------------------------------- #
# Gauge ladder alarm (fresh: the current run counts; anti-twitch confirmed)
# --------------------------------------------------------------------------- #
def _nearest_price(series, target, tol_hours=6.0):
    """Price logged nearest to `target` time (±tol) from [(dt, price)] or None."""
    best = None
    for ts, price in series:
        if price is None:
            continue
        d = abs((ts - target).total_seconds())
        if d <= tol_hours * 3600 and (best is None or d < best[0]):
            best = (d, price)
    return best[1] if best else None


def _pace_note(pts, current_price, est_pct, lang="en"):
    """Speed-aware line: what the last 24h actually did vs the usual pace."""
    if current_price is None or len(pts) < 2:
        return None
    base = _nearest_price([(t, p) for t, _, p in pts],
                          pts[-1][0] - timedelta(hours=24), 6.0)
    if not base:
        return None
    pct = (current_price - base) / base * 100.0
    if est_pct and (pct > 0) != (est_pct > 0):
        word = "pace_against"
    elif abs(pct) > 1.5 * abs(est_pct or 0.0):
        word = "pace_ahead"
    else:
        word = "pace_line"
    return T(lang, "pace", egp=f"{current_price - base:+,.0f} EGP", pct=pct,
             word=T(lang, word))


def detect_gauge_alarm(current_gauge, current_price, persist=True):
    """Ring when the gauge reaches a rung not yet rung today (Cairo).

    FRESH: the CURRENT run's gauge participates directly, so the alarm
    describes the run it fires in (no more one-run lag).
    ANTI-TWITCH: a crossing rings instantly only when it clears the rung by
    LADDER_CONFIRM pts; a weaker crossing rings once the gauge has held the
    rung for two consecutive runs. 0 = every crossing rings instantly.
    Dedup lives in data/alarms.csv (survives restarts, once per rung/day).
    Returns a dict for the renderer, or None when quiet.
    """
    col, rows = _load_pred_rows()
    pts = _ladder_points(col, rows) if rows else []
    prev_g = pts[-1][1] if pts else None
    prev_prev_g = pts[-2][1] if len(pts) > 1 else None
    rung_today = _alarms_rung_today("gauge")

    def rings_up(r):
        if f"up:{r}" in rung_today or current_gauge < r:
            return False
        if prev_g is None:
            return False
        if prev_g < r:  # fresh crossing this run
            return LADDER_CONFIRM <= 0 or current_gauge >= r + LADDER_CONFIRM
        # held: crossed earlier (unconfirmed), now a second run above it
        return current_gauge >= r and (prev_prev_g is None or prev_prev_g < r)

    def rings_down(r):
        if f"down:{r}" in rung_today or current_gauge > r:
            return False
        if prev_g is None:
            return False
        if prev_g > r:  # fresh crossing this run
            return LADDER_CONFIRM <= 0 or current_gauge <= r - LADDER_CONFIRM
        return current_gauge <= r and (prev_prev_g is None or prev_prev_g > r)

    new_up = [r for r in LADDER_UP if rings_up(r)]
    new_down = [r for r in LADDER_DOWN if rings_down(r)]
    if not new_up and not new_down:
        return None

    if new_up:
        side, rung = "up", max(new_up)
    else:
        side, rung = "down", min(new_down)
    idx = (LADDER_UP.index(rung) if side == "up"
           else LADDER_DOWN.index(rung))
    est_pct, egp, src = _ladder_meaning(side, idx, _gauge_move_stats(pts),
                                        current_price)
    for r in (new_up if new_up else new_down):
        r_idx = (LADDER_UP.index(r) if side == "up"
                 else LADDER_DOWN.index(r))
        _append_alarm("gauge", f"{side}:{r}", _rung_est(side, r_idx),
                      current_price, persist)
    res = {"side": side, "rung": rung, "levels": len(new_up or new_down),
           "gauge": current_gauge, "est_pct": est_pct, "egp": egp, "src": src}
    pace = _pace_note(pts, current_price, est_pct)
    if pace:
        res["pace"] = pace
    return res


def render_alarm(res, lang="en"):
    """Alarm dict -> message lines (localized)."""
    if not res:
        return []
    levels_txt = (T(lang, "levels_new_one") if res["levels"] == 1
                  else T(lang, "levels_new_many", n=res["levels"]))
    lines = [T(lang, "ladder_up" if res["side"] == "up" else "ladder_down",
               gauge=res["gauge"], levels=levels_txt)]
    if res.get("egp") is not None:
        lines.append(T(lang, "meaning", egp=f"{res['egp']:+,.0f} EGP",
                       pct=res["est_pct"], src=res["src"]))
    if res.get("pace"):
        lines.append(res["pace"])
    return lines


# --------------------------------------------------------------------------- #
# SELL WINDOW milestone: 21K far above the day's average = local sell chance
# --------------------------------------------------------------------------- #
def _trimmed_mean(vals, tol=0.02):
    """Mean after dropping prices that sit >tol away from the median."""
    if not vals:
        return None
    med = sorted(vals)[len(vals) // 2]
    keep = [v for v in vals if med and abs(v - med) / med <= tol]
    use = keep or vals
    return sum(use) / len(use)


def detect_milestone(current_price, persist=True):
    """SELL WINDOW when 21K rises MILESTONE_PCT (or MILESTONE_EGP) above the
    day's average price. Baseline = today's mean once MILESTONE_MIN_RUNS runs
    exist today; before that, yesterday's outlier-trimmed mean. Rings once
    per Cairo day. Returns a dict for the renderer, or None."""
    if current_price is None:
        return None
    if "milestone" in _alarms_rung_today("milestone"):
        return None
    col, rows = _load_pred_rows()

    def day_prices(d):
        if not rows:
            return []
        return [v for r in rows
                if (_val(r, col, "timestamp_cairo") or "")[:10] == d
                for v in [_num(r, col, "price_21k")] if v is not None]

    today = cairo_now().strftime("%Y-%m-%d")
    yest = (cairo_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    todays = day_prices(today)
    if len(todays) >= MILESTONE_MIN_RUNS:
        baseline, src_key = sum(todays) / len(todays), "sell_src_today"
    else:
        baseline = _trimmed_mean(day_prices(yest))
        src_key = "sell_src_yday"
        if baseline is None:
            if not todays:
                return None
            baseline, src_key = sum(todays) / len(todays), "sell_src_today"
    delta = current_price - baseline
    pct = delta / baseline * 100.0 if baseline else 0.0
    if not (pct >= MILESTONE_PCT or (MILESTONE_EGP > 0 and delta >= MILESTONE_EGP)):
        return None
    high7 = False
    try:
        series = _main_price_series()
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        past = [p for t, p in series if t >= cutoff]
        high7 = bool(past) and current_price > max(past)
    except Exception:  # noqa: BLE001 - the high note is cosmetic
        pass
    _append_alarm("milestone", "milestone", round(pct, 2), current_price, persist)
    return {"price": current_price, "baseline": baseline, "delta": delta,
            "pct": pct, "src_key": src_key, "high7": high7}


def render_milestone(res, lang="en"):
    """Milestone dict -> message lines (localized)."""
    if not res:
        return []
    lines = [T(lang, "sell_title", price=res["price"], delta=res["delta"],
               pct=res["pct"], src=T(lang, res["src_key"])),
             T(lang, "sell_body")]
    if res.get("high7"):
        lines.append(T(lang, "sell_high"))
    return lines


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Gold prediction gauge")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, write nothing")
    ap.add_argument("--force", action="store_true", help="always send the Telegram message")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # ---- self-service onboarding: /start, /en, /ar, /stop, /latest ----
    if not args.dry_run:
        try:
            import telegram_bot
            n = telegram_bot.poll_once()
            if n:
                print(f"  [bot] {n} onboarding update(s) processed")
        except Exception as err:  # noqa: BLE001 - never block the gauge
            print(f"  [bot] poll skipped: {err}", file=sys.stderr)

    groups = collect_signals()
    if not groups:
        print("[gauge] FATAL: all signal groups failed", file=sys.stderr)
        return 1
    final_score = merge(groups)
    final_score = dampen_raw_bias(final_score)
    pct = round(max(2.0, min(98.0, 50.0 + 50.0 * final_score)))
    direction = "UP" if pct >= UP_THRESHOLD else ("DOWN" if pct <= DOWN_THRESHOLD else "FLAT")
    conf = confidence_label(groups)

    prev = None if args.dry_run else read_last_row(PRED_PATH)
    delta = None
    if prev and prev.get("final_pct"):
        try:
            delta = pct - float(prev["final_pct"])
        except ValueError:
            delta = None
    prev_cairo_date = (prev.get("timestamp_cairo", "")[:10] if prev else "")
    new_day = cairo_now().strftime("%Y-%m-%d") != prev_cairo_date

    accuracy = None if args.dry_run else grade_pending()
    if not args.dry_run:
        migrate_pred_csv()
        try:
            _grade_alarms()  # report card: grade alarms now 21-36h old
        except Exception as err:  # noqa: BLE001
            print(f"  [alarms] grading failed: {err}", file=sys.stderr)

    # current 21K price for level distances / alarms
    current_21k = None
    for g in groups:
        if g["group"] == "technical" and g.get("indicators", {}).get("column") == "gold_21k_sell":
            current_21k = g["indicators"].get("last")

    # ---- alarms: FRESH (current run counts) + anti-twitch + milestone ----
    try:
        alarm_res = detect_gauge_alarm(pct, current_21k, persist=not args.dry_run)
    except Exception as err:  # noqa: BLE001
        print(f"  [ladder] check failed: {err}", file=sys.stderr)
        alarm_res = None
    try:
        mile_res = detect_milestone(current_21k, persist=not args.dry_run)
    except Exception as err:  # noqa: BLE001
        print(f"  [milestone] check failed: {err}", file=sys.stderr)
        mile_res = None

    # Local-vs-global divergence: Egyptian prices = global gold x USD/EGP x
    # local premium. A persistent spread means local market forces dominate.
    tech_g = next((g for g in groups if g["group"] == "technical"), None)
    cons_g = next((g for g in groups if g["group"] == "consensus"), None)
    local_7d = (tech_g or {}).get("indicators", {}).get("mom_7d_pct")
    global_7d = (cons_g or {}).get("perf_7d_pct")
    global_close = (cons_g or {}).get("close")
    divergence = None
    if isinstance(local_7d, (int, float)) and isinstance(global_7d, (int, float)):
        divergence = round(local_7d - global_7d, 3)

    # Local premium monitor: measures the parallel-dollar stress the source
    # site bakes into its own USD/EGP rate. Fail-soft: never blocks the gauge.
    prem = None
    try:
        prem = local_premium.measure(CSV_PATH, PRED_PATH)  # EN; CSV columns too
    except Exception as err:  # noqa: BLE001
        print(f"  [premium] monitor failed: {err}", file=sys.stderr)

    def _safe(fn):
        try:
            return fn()
        except Exception as err:  # noqa: BLE001
            print(f"  [digest] {getattr(fn, '__name__', 'digest')} skipped: {err}",
                  file=sys.stderr)
            return None

    def _prem_lines(lang):
        """Premium monitor lines in a language (EN reused from the first call)."""
        if not prem:
            return []
        if lang == "en":
            return prem.get("lines", [])
        try:
            return local_premium.measure(CSV_PATH, PRED_PATH, lang=lang).get("lines", [])
        except Exception:  # noqa: BLE001
            return prem.get("lines", [])

    # ---- which languages do the recipients need? ----
    need_langs = set()
    for _, lang in _recipients():
        need_langs |= {"en", "ar"} if lang == "both" else {lang}
    if args.dry_run or not need_langs:
        need_langs = {"en"}

    def _compose(lang):
        """The full message for one language: blocks joined by a blank line."""
        blocks = []
        if alarm_res:
            blocks.append(render_alarm(alarm_res, lang))
        if mile_res:
            blocks.append(render_milestone(mile_res, lang))
        if not args.dry_run:
            rep = alarm_report_lines(lang)
            if rep:
                blocks.append(rep)
        if args.dry_run or new_day:
            parts = [_safe(lambda: build_daily_digest(lang))]
            if cairo_now().weekday() == DIGEST_WEEKDAY:
                parts.append(_safe(lambda: build_weekly_digest(lang)))
            if cairo_now().day == 1:
                parts.append(_safe(lambda: build_monthly_digest(lang)))
            for part in parts:
                if part:
                    blocks.append(part)
        if delta is not None:
            change_txt = T(lang, "change_line", delta=f"{delta:+.0f}")
        elif args.dry_run:
            change_txt = T(lang, "change_dry")
        else:
            change_txt = T(lang, "change_first")
        blocks.append([T(lang, "rightnow"),
                       T(lang, "gauge_line", pct=pct,
                         direction=_dir_word(lang, direction),
                         conf=_conf_word(lang, conf)),
                       change_txt])
        sig = [T(lang, "sig_" + g["group"], detail=g["detail"])
               for g in groups
               if g["group"] in ("technical", "consensus", "news", "fx_mr")]
        if divergence is not None:
            sig.append(T(lang, "div_line", a=local_7d, b=global_7d, s=divergence))
        if sig:
            blocks.append(sig)
        pl = _prem_lines(lang)
        if pl:
            blocks.append(pl)
        tail = []
        level_parts = summarize_levels(current_21k, lang)  # read-only, dry-run safe
        if level_parts:
            tail.append(T(lang, "levels_head", parts=" | ".join(level_parts)))
        if accuracy:
            if accuracy.get("dir_n"):
                tail.append(T(lang, "accuracy_both", d=accuracy["dir_pct"],
                              dn=accuracy["dir_n"], s=accuracy["pct"],
                              n=accuracy["n"]))
            else:
                tail.append(T(lang, "accuracy_strict", n=accuracy["n"],
                              pct=accuracy["pct"]))
        tail.append(T(lang, "bias"))
        blocks.append(tail)
        return "\n\n".join("\n".join(b) for b in blocks if b)

    messages = {lang: _compose(lang) for lang in sorted(need_langs)}
    print(messages.get("en") or next(iter(messages.values()), ""))
    if args.dry_run and not alarm_res:
        print("  [ladder] quiet - no new rung today", file=sys.stderr)
    if args.dry_run and not mile_res:
        print("  [milestone] quiet - price not above the day baseline", file=sys.stderr)
    if args.json:
        print(json.dumps({"pct": pct, "direction": direction, "confidence": conf,
                          "groups": groups, "accuracy": accuracy}, ensure_ascii=False))

    if args.dry_run:
        return 0

    # ---- persistence ----
    append_row(PRED_PATH, PRED_HEADER, {
        "timestamp_utc": now_utc_str(),
        "timestamp_cairo": cairo_now().strftime("%Y-%m-%d %H:%M:%S"),
        "final_pct": pct, "direction": direction, "confidence": conf,
        "technical": next((f"{g['score']:.3f}" for g in groups if g["group"] == "technical"), ""),
        "consensus": next((f"{g['score']:.3f}" for g in groups if g["group"] == "consensus"), ""),
        "news": next((f"{g['score']:.3f}" for g in groups if g["group"] == "news"), ""),
        "fx_mr": next((f"{g['score']:.3f}" for g in groups if g["group"] == "fx_mr"), ""),
        "price_21k": current_21k if current_21k else "",
        "global_xauusd": (f"{global_close:.2f}" if isinstance(global_close, (int, float)) else ""),
        "local_chg_7d": (f"{local_7d:+.3f}" if isinstance(local_7d, (int, float)) else ""),
        "global_chg_7d": (f"{global_7d:+.3f}" if isinstance(global_7d, (int, float)) else ""),
        "divergence_7d": (f"{divergence:+.3f}" if divergence is not None else ""),
        "usdegp_official": (f"{prem['official']:.4f}"
                            if prem and prem.get("official") else ""),
        "parallel_gap_pct": (f"{prem['gap_pct']:+.3f}"
                             if prem and prem.get("gap_pct") is not None else ""),
        "real_premium_pct": (f"{prem['real_premium_pct']:+.3f}"
                             if prem and prem.get("real_premium_pct") is not None else ""),
        "formula_dev_pct": (f"{prem['formula_dev_pct']:+.3f}"
                            if prem and prem.get("formula_dev_pct") is not None else ""),
    })
    for g in groups:
        if g["group"] == "news":
            update_levels(g.get("levels", []))

    # ---- notify decision: only when it is worth a message ----
    prev_dir = prev.get("direction") if prev else None
    should_notify = bool(args.force or prev is None or new_day
                         or prev_dir != direction
                         or (delta is not None and abs(delta) >= NOTIFY_DELTA)
                         or alarm_res or mile_res)
    if should_notify:
        try:
            send_telegram(messages)
        except Exception as err:  # noqa: BLE001 - never fail the run on notify
            print(f"  [telegram] ERROR: {err}", file=sys.stderr)
    else:
        print("  [gauge] no significant change -> no notification")
    return 0


if __name__ == "__main__":
    sys.exit(main())
