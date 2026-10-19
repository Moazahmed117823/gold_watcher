#!/usr/bin/env python3
"""
Prediction gauge orchestrator.
==============================
Runs the three signal groups (technical, consensus, news), merges them into
one bias percentage, logs every prediction, self-grades yesterday's calls,
maintains the 7-day "levels map" from verified analyst levels, and sends a
compact Telegram message only when it is worth sending (direction flip,
>=15-point move, first run of the Cairo day, or --force).

Files written (both committed by the workflow):
    data/predictions.csv     one row per run + grading columns
    data/levels_history.csv  every verified analyst level with timestamp

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

PRED_HEADER = ["timestamp_utc", "timestamp_cairo", "final_pct", "direction",
               "confidence", "technical", "consensus", "news", "price_21k",
               "global_xauusd", "local_chg_7d", "global_chg_7d", "divergence_7d",
               "price_24h_later", "predicted_correct", "graded_at_utc",
               "usdegp_official", "parallel_gap_pct", "real_premium_pct",
               "formula_dev_pct"]
LEVELS_HEADER = ["timestamp_utc", "price", "unit", "type", "title"]

def _env_weight(name, default):
    """Read a positive float weight from env; fall back to default on bad input."""
    try:
        v = float(os.environ.get(name, "").strip() or default)
        return v if v > 0 else default
    except ValueError:
        return default


# Weights are env-tunable so recalibration never needs a code change.
# Weight study (2026-10, data: 2y GC=F + USDEGP + 43d real CSV, see
# scripts/study_weights.py in the addon workspace): the source site COMPUTES
# its 21K price as global ounce x parallel-market USD/EGP (verified mean
# arithmetic error 0.005%), so the local series carries the parallel-dollar
# information that global feeds cannot see. On days when local and global
# signals disagree, local momentum predicted the next-day LOCAL move better
# (2y disagreement subset 55-58% vs 47% for global-heavy; real 43d replay
# 63% vs 41%, small n). Defaults below shift local from 40/30 to 50/20 while
# keeping the global anchor; override via env vars without touching code.
WEIGHTS = {
    "technical": _env_weight("WEIGHT_TECHNICAL", 0.50),
    "consensus": _env_weight("WEIGHT_CONSENSUS", 0.20),
    "news": _env_weight("WEIGHT_NEWS", 0.30),
}
UP_THRESHOLD, DOWN_THRESHOLD = 58, 42          # % outside this band = directional
NOTIFY_DELTA = 15.0                            # points vs previous run
ACCURACY_WINDOW = 30                           # graded rows for the accuracy stat

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
    return dict(zip(rows[0], rows[-1]))


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
def collect_signals():
    """Run all three groups; skip (don't crash) the ones that fail."""
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
    return groups


def merge(groups):
    """Weighted merge with renormalization -> (final_score, available map)."""
    num, den = 0.0, 0.0
    for g in groups:
        w = WEIGHTS.get(g["group"], 0.0) * g.get("weight", 1.0)
        if g.get("weight", 0) <= 0:
            continue
        num += w * g["score"]
        den += w
    return (num / den) if den > 0 else 0.0


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
            correct = abs(pct - 50.0) < 8.0
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
    """Rolling accuracy over the last ACCURACY_WINDOW graded rows."""
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
    return {"hits": hits, "n": len(recent), "pct": round(100.0 * hits / len(recent))}


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


def summarize_levels(current_21k):
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
            side = f" ({dist:+.1f}% from now)"
        parts.append(f"{price:,} {unit.split('/')[1]} {ltype} x{n}{side}")
    return parts


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat_id):
        print("  [telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -> skipped")
        return False
    resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text,
                               "disable_web_page_preview": True}, timeout=15)
    resp.raise_for_status()
    print("  [telegram] gauge message delivered")
    return True


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Gold prediction gauge")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, write nothing")
    ap.add_argument("--force", action="store_true", help="always send the Telegram message")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    groups = collect_signals()
    if not groups:
        print("[gauge] FATAL: all signal groups failed", file=sys.stderr)
        return 1
    final_score = merge(groups)
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

    accuracy = None if args.dry_run else grade_pending()
    if not args.dry_run:
        migrate_pred_csv()

    # current 21K price for level distances
    current_21k = None
    for g in groups:
        if g["group"] == "technical" and g.get("indicators", {}).get("column") == "gold_21k_sell":
            current_21k = g["indicators"].get("last")

    if delta is not None:
        change_txt = f"{delta:+.0f} pts since last check"
    elif args.dry_run:
        change_txt = "n/a (dry run)"
    else:
        change_txt = "first logged run - baseline saved"
    lines = [f"Gold gauge: {pct}% {direction} ({conf})",
             f"Change: {change_txt}"]
    for g in groups:
        lines.append(f"- {g['group']}: {fmt_sign(g['score'])} ({g['detail']})")

    # Local-vs-global divergence: Egyptian prices = global gold x USD/EGP x
    # local premium. A persistent spread means local market forces (merchant
    # hoarding, supply, EGP moves) are dominating. Measured + logged first;
    # it can earn a weight later once we have ~2 weeks of data.
    tech_g = next((g for g in groups if g["group"] == "technical"), None)
    cons_g = next((g for g in groups if g["group"] == "consensus"), None)
    local_7d = (tech_g or {}).get("indicators", {}).get("mom_7d_pct")
    global_7d = (cons_g or {}).get("perf_7d_pct")
    global_close = (cons_g or {}).get("close")
    divergence = None
    if isinstance(local_7d, (int, float)) and isinstance(global_7d, (int, float)):
        divergence = round(local_7d - global_7d, 3)
        lines.append(f"- local-vs-global (7d): 21K {local_7d:+.2f}% | "
                     f"XAU/USD {global_7d:+.2f}% | spread {divergence:+.2f} pts")

    # Local premium monitor: the source site bakes parallel-dollar stress,
    # making charges and scarcity premiums into its USD/EGP rate - invisible
    # inside its own numbers. Measure the invisible against external
    # references: official-FX gap, your real souq reports, formula-break
    # alarm. Fail-soft: any problem here never blocks the gauge.
    prem = None
    try:
        prem = local_premium.measure(CSV_PATH, PRED_PATH)
    except Exception as err:  # noqa: BLE001
        print(f"  [premium] monitor failed: {err}", file=sys.stderr)
    if prem:
        lines.extend(prem["lines"])
    level_parts = summarize_levels(current_21k)  # read-only, safe in dry-run too
    if level_parts:
        lines.append("Levels (7d): " + " | ".join(level_parts))
    if accuracy:
        lines.append(f"Gauge accuracy ({accuracy['n']} graded calls): {accuracy['pct']}%")
    lines.append("Bias gauge, not financial advice.")

    message = "\n".join(lines)
    print(message)
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
    prev_cairo_date = (prev.get("timestamp_cairo", "")[:10] if prev else "")
    new_day = cairo_now().strftime("%Y-%m-%d") != prev_cairo_date
    should_notify = bool(args.force or prev is None or new_day
                         or prev_dir != direction
                         or (delta is not None and abs(delta) >= NOTIFY_DELTA))
    if should_notify:
        try:
            send_telegram(message)
        except Exception as err:  # noqa: BLE001 - never fail the run on notify
            print(f"  [telegram] ERROR: {err}", file=sys.stderr)
    else:
        print("  [gauge] no significant change -> no notification")
    return 0


if __name__ == "__main__":
    sys.exit(main())
