#!/usr/bin/env python3
"""
Technical signal engine for Egypt gold prices.
==============================================
Reads data/gold_prices.csv (produced by scraper.py) and computes classic
technical indicators on the 21K sell series (Egypt's most-traded karat),
falling back to 24K if 21K is unavailable.

Output: a score in [-1, +1] (bearish .. bullish) + human-readable detail.
Pure Python stdlib -- no new dependencies.

Usage:
    python technical.py                       # default CSV location
    python technical.py --csv path/to.csv     # explicit CSV
    python technical.py --json                # machine-readable output
"""
import argparse
import csv
import json
import math
import os
import sys

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "gold_prices.csv")

# Column candidates, in preference order (21K is Egypt's benchmark karat)
PRICE_COLUMNS = ["gold_21k_sell", "gold_24k_sell"]

# Group weights (must sum to 1.0)
W_SMA_CROSS, W_RSI, W_MOMENTUM, W_BOLLINGER = 0.30, 0.25, 0.30, 0.15

PRIMARY_SOURCE = "gold-price-today.com"


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def load_series(csv_path, prefer_primary=True, min_primary_rows=60):
    """Load (timestamp, price) pairs for the first available price column.

    Returns (column_name, [(ts_str, float), ...]).
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if len(rows) < 2:
        raise ValueError("CSV has no data rows")
    header = rows[0]
    col_idx = {name: i for i, name in enumerate(header)}
    src_idx = col_idx.get("source")
    ts_idx = col_idx.get("timestamp_utc", 0)

    chosen, series = None, []
    for col in PRICE_COLUMNS:
        if col not in col_idx:
            continue
        all_rows, primary_rows = [], []
        for r in rows[1:]:
            if len(r) <= col_idx[col] or not r[col_idx[col]].strip():
                continue
            try:
                price = float(r[col_idx[col]])
            except ValueError:
                continue
            ts = r[ts_idx] if ts_idx < len(r) else ""
            all_rows.append((ts, price))
            if src_idx is not None and src_idx < len(r) and r[src_idx] == PRIMARY_SOURCE:
                primary_rows.append((ts, price))
        use = primary_rows if (prefer_primary and len(primary_rows) >= min_primary_rows) else all_rows
        if len(use) >= 30:          # absolute minimum to say anything useful
            chosen, series = col, use
            break
    if chosen is None:
        raise ValueError("No usable price column found (need >= 30 valid rows)")
    return chosen, series


# --------------------------------------------------------------------------- #
# Indicators (stdlib only)
# --------------------------------------------------------------------------- #
def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def stdev(values, n):
    if len(values) < n:
        return None
    window = values[-n:]
    mean = sum(window) / n
    var = sum((v - mean) ** 2 for v in window) / n
    return math.sqrt(var)


def rsi(values, n=14):
    """Classic RSI over the last n changes. Returns None if not enough data."""
    if len(values) < n + 1:
        return None
    deltas = [values[i] - values[i - 1] for i in range(len(values) - n, len(values))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def pct_change(values, n):
    """% change over the last n steps (None if unavailable)."""
    if len(values) <= n or values[-n] == 0:
        return None
    return (values[-1] - values[-n]) / values[-n] * 100.0


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def compute_score(csv_path=CSV_PATH, prefer_primary=True):
    """Full technical score in [-1, +1] + indicator breakdown."""
    column, series = load_series(csv_path, prefer_primary=prefer_primary)
    values = [p for _, p in series]
    n = len(values)
    last = values[-1]

    # 1) SMA crossover (20 vs 50): direction by position, strength by % gap
    sma20, sma50 = sma(values, 20), sma(values, 50)
    sma_score, sma_detail = 0.0, "n/a (insufficient rows)"
    if sma20 is not None and sma50 is not None and sma50 != 0:
        gap = (sma20 - sma50) / sma50 * 100.0
        sma_score = clip(gap / 1.0)          # +/-1% gap -> full strength
        sma_detail = f"SMA20 {sma20:,.0f} vs SMA50 {sma50:,.0f} (gap {gap:+.2f}%)"

    # 2) RSI(14): centered at 50; fades at overbought/oversold extremes
    r = rsi(values)
    rsi_score, rsi_detail = 0.0, "n/a (insufficient rows)"
    if r is not None:
        rsi_score = clip((r - 50.0) / 30.0)  # +/-30 RSI points -> full strength
        if r >= 75:                          # overbought: momentum up but stretched
            rsi_score = min(rsi_score, 0.2)
        elif r <= 25:
            rsi_score = max(rsi_score, -0.2)
        rsi_detail = f"RSI(14) {r:.1f}"

    # 3) Momentum: 24h (24 rows) dominant, 7d (168 rows) half weight
    m24, m168 = pct_change(values, min(24, n - 1)), pct_change(values, min(168, n - 1))
    mom_score, mom_detail = 0.0, "n/a"
    if m24 is not None:
        mom_score = 0.7 * clip(m24 / 1.0)    # +/-1% in 24h -> full 24h strength
        mom_detail = f"24h {m24:+.2f}%"
        if m168 is not None:
            mom_score += 0.3 * clip(m168 / 3.0)   # +/-3% in 7d -> full 7d strength
            mom_detail += f" | 7d {m168:+.2f}%"

    # 4) Bollinger position: where is price inside the 20-row 2-sigma band
    s20, sd = sma(values, 20), stdev(values, 20)
    bol_score, bol_detail = 0.0, "n/a (insufficient rows)"
    if s20 is not None and sd and sd > 0:
        z = (last - s20) / (2.0 * sd)
        bol_score = clip(-z)                 # above band = stretched -> mild fade
        bol_detail = f"band z {z:+.2f}"

    score = (W_SMA_CROSS * sma_score + W_RSI * rsi_score +
             W_MOMENTUM * mom_score + W_BOLLINGER * bol_score)
    detail = (f"{column}: {last:,.0f} | {sma_detail} | {rsi_detail} | "
              f"{mom_detail} | Boll {bol_detail} | rows {n}")
    return {
        "group": "technical",
        "score": round(clip(score), 3),
        "weight": 1.0,
        "detail": detail,
        "indicators": {"column": column, "last": last, "rows": n,
                       "sma20": sma20, "sma50": sma50, "rsi14": r,
                       "mom_24h_pct": m24, "mom_7d_pct": m168},
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Technical score from gold_prices.csv")
    ap.add_argument("--csv", default=CSV_PATH, help="path to gold_prices.csv")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--all-sources", action="store_true",
                    help="include intl-spot-estimate rows in indicators")
    args = ap.parse_args()
    try:
        result = compute_score(args.csv, prefer_primary=not args.all_sources)
    except Exception as err:  # noqa: BLE001
        print(f"[technical] ERROR: {err}", file=sys.stderr)
        result = {"group": "technical", "score": 0.0, "weight": 0.0,
                  "detail": f"unavailable: {err}", "indicators": {}}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[technical] score {result['score']:+.3f}  ({result['detail']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
