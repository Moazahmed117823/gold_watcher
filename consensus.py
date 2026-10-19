#!/usr/bin/env python3
"""
Analyst technical consensus via TradingView's public scanner endpoint.
=====================================================================
Fetches the aggregated technical rating for XAU/USD (spot gold) that
TradingView computes from standard indicators (moving averages + oscillators).
This is the same "Strong Buy .. Strong Sell" gauge retail traders watch.

Free, no API key, one GET request. Score in [-1, +1].

Usage:
    python consensus.py
    python consensus.py --json
"""
import argparse
import json
import sys

import requests

TV_URL = ("https://scanner.tradingview.com/symbol"
          "?symbol=OANDA%3AXAUUSD"
          "&fields=Recommend.All%7C1,Recommend.MA%7C1,Recommend.Other%7C1,"
          "close%7C1,Perf.W,change%7C60"
          "&no_404=true")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}


def _label(value):
    if value is None:
        return "n/a"
    if value >= 0.5:
        return "strong buy"
    if value >= 0.15:
        return "buy"
    if value > -0.15:
        return "neutral"
    if value > -0.5:
        return "sell"
    return "strong sell"


def fetch_consensus(timeout=15, retries=2):
    """Return {'group': 'consensus', 'score': float, 'weight': 1.0, 'detail': str}.

    Raises RuntimeError on failure (caller decides how to degrade).
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(TV_URL, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            overall = data.get("Recommend.All|1")
            ma = data.get("Recommend.MA|1")
            osc = data.get("Recommend.Other|1")
            close = data.get("close|1")          # global spot XAU/USD
            perf_w = data.get("Perf.W")          # global 7-day % performance
            chg60 = data.get("change|60")        # global 1h abs change (may be null)
            if overall is None:
                raise ValueError(f"unexpected payload: {data}")
            overall = float(overall)
            detail = (f"TradingView XAU/USD: overall {overall:+.2f} ({_label(overall)}), "
                      f"MAs {float(ma):+.2f} ({_label(ma)}), "
                      f"oscillators {float(osc):+.2f} ({_label(osc)})")
            if close is not None:
                detail += f", spot {float(close):,.1f}"
            if perf_w is not None:
                detail += f", 7d {float(perf_w):+.2f}%"
            return {"group": "consensus", "score": round(max(-1.0, min(1.0, overall)), 3),
                    "weight": 1.0, "detail": detail,
                    "close": close, "perf_7d_pct": perf_w, "chg_1h": chg60,
                    "raw": {"overall": overall, "ma": ma, "oscillators": osc,
                            "close": close, "perf_w": perf_w, "chg60": chg60}}
        except Exception as err:  # noqa: BLE001
            last_err = err
            print(f"  [consensus] attempt {attempt}/{retries} failed: {err}",
                  file=sys.stderr)
    raise RuntimeError(f"TradingView consensus unavailable: {last_err}")


def main():
    ap = argparse.ArgumentParser(description="TradingView XAU/USD consensus")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        result = fetch_consensus()
    except Exception as err:  # noqa: BLE001
        print(f"[consensus] ERROR: {err}", file=sys.stderr)
        result = {"group": "consensus", "score": 0.0, "weight": 0.0,
                  "detail": f"unavailable: {err}"}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[consensus] score {result['score']:+.3f}  ({result['detail']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
