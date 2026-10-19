#!/usr/bin/env python3
"""
Report a REAL souq price you saw yourself.
==========================================
The site's local prices are COMPUTED (global ounce x its USD/EGP rate), so
bazaar reality - making charges, jeweler margins, scarcity premiums - never
shows up in them. Whenever you physically buy/check gold, log the real
number here. The predictor compares your reports against the site reference
at the same time and prints "real souq premium" in the gauge message.
That series is how we identify and track the variable neither of us can
name yet - with YOUR measurements.

The CSV is committed by the workflow automatically (git add data/), so the
history survives across runs.

Usage:
    python real_prices.py --21k 6350
    python real_prices.py --21k 6350 --coin 51200 --note "downtown souq"
    python real_prices.py --show          # last 10 reports
"""
import argparse
import csv
import os
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CSV_PATH = os.path.join(DATA_DIR, "real_prices.csv")
HEADER = ["timestamp_utc", "price_21k", "gold_pound_coin", "note"]


def main():
    ap = argparse.ArgumentParser(description="Log a real souq gold price (EGP)")
    ap.add_argument("--21k", dest="p21", type=float,
                    help="21K sell price per gram you were quoted (EGP)")
    ap.add_argument("--coin", type=float, default=None,
                    help="gold pound coin price (EGP), optional")
    ap.add_argument("--note", default="", help="where/which shop, optional")
    ap.add_argument("--show", action="store_true",
                    help="print the last 10 reports and exit")
    args = ap.parse_args()

    if args.show:
        if not os.path.exists(CSV_PATH):
            print("no reports logged yet")
            return 0
        with open(CSV_PATH, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(fh) if r]
        print(f"{len(rows)-1} reports on file, last 10:")
        for r in rows[-10:]:
            print("  " + " | ".join(r))
        return 0

    if not args.p21:
        ap.error("--21k is required (or use --show)")

    os.makedirs(DATA_DIR, exist_ok=True)
    new_file = not os.path.exists(CSV_PATH)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(HEADER)
        w.writerow([stamp, args.p21,
                    args.coin if args.coin else "", args.note])
    print(f"logged: 21K {args.p21:g} EGP at {stamp} UTC"
          + (f" ({args.note})" if args.note else ""))
    print("the predictor will now compute your real premium vs the site "
          "reference on its next run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
