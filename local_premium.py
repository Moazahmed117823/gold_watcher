#!/usr/bin/env python3
"""
Local premium monitor: make the 'unknown variable' measurable.
===============================================================
The source site computes 21K = global ounce x its own USD/EGP rate
(verified: mean arithmetic error 0.005%). Anything the market bakes into
that rate - the parallel dollar, scarcity premiums, making charges,
coin-demand stress - is INVISIBLE inside the site's own numbers.

This module measures the invisible against external references:

1. parallel-dollar gap : site rate vs OFFICIAL USD/EGP (open.er-api.com,
   free, no key). A widening gap = local stress the global feeds cannot see.
2. real souq premium   : prices you report yourself (data/real_prices.csv,
   via real_prices.py) vs the site reference at the same time.
   This captures premiums the site never shows (making charges, bazaar
   margins) - the literal "unknown variable" as you experience it.
3. formula-break alarm : if 21K stops matching ounce x rate (|dev| > 0.5%),
   the site itself started embedding real premiums -> early warning that
   the market regime changed.

All calls are fail-soft: no network or missing files -> empty result, the
gauge runs unchanged.

Usage (from predictor.py):
    local_premium.measure(csv_path, pred_path) -> dict
"""
import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

OFFICIAL_FX_URL = "https://open.er-api.com/v6/latest/USD"
GRAMS_PER_OUNCE = 31.1034768
FORMULA_BREAK_PCT = 0.5          # |dev| beyond this = site formula changed
REAL_REPORT_MAX_AGE_DAYS = 7     # souq reports older than this are ignored
REAL_MATCH_WINDOW_HOURS = 12     # nearest site quote to a report, ±hours


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _last_row(path):
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if len(rows) < 2:
        return None
    return dict(zip(rows[0], rows[-1]))


def official_usdegp(timeout=10):
    """Official (interbank) USD/EGP. None on any failure."""
    try:
        j = requests.get(OFFICIAL_FX_URL, timeout=timeout).json()
        v = float(j["rates"]["EGP"])
        return v if v > 0 else None
    except Exception:  # noqa: BLE001 - fail-soft by design
        return None


def real_premium(real_csv, site_csv,
                 max_age_days=REAL_REPORT_MAX_AGE_DAYS,
                 match_hours=REAL_MATCH_WINDOW_HOURS):
    """Mean premium of your reported souq prices over the site reference.

    Each report is matched to the nearest site quote within ±match_hours;
    premium = report / site_quote - 1. Returns percent (mean of reports)
    or None when no usable reports exist.
    """
    if not os.path.exists(real_csv):
        return None
    cutoff = _utcnow() - timedelta(days=max_age_days)
    reports = []
    with open(real_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                ts = _parse_ts(r["timestamp_utc"])
                price = float(r["price_21k"])
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                reports.append((ts, price))
    if not reports:
        return None
    site_quotes = []
    with open(site_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                site_quotes.append((_parse_ts(r["timestamp_utc"]),
                                    float(r["gold_21k_sell"])))
            except (KeyError, ValueError):
                continue
    site_quotes.sort()
    stamps = [q[0] for q in site_quotes]
    prems = []
    for ts, price in reports:
        best_i, best_dt = None, None
        for i, st in enumerate(stamps):
            d = abs((st - ts).total_seconds())
            if d <= match_hours * 3600 and (best_dt is None or d < best_dt):
                best_i, best_dt = i, d
        if best_i is None:
            continue
        ref = site_quotes[best_i][1]
        if ref > 0:
            prems.append((price / ref - 1.0) * 100.0)
    if not prems:
        return None
    return sum(prems) / len(prems)


def _gap_trend(pred_path, days=7):
    """parallel_gap_pct logged closest to `days` ago, or None."""
    if not os.path.exists(pred_path):
        return None
    target = _utcnow() - timedelta(days=days)
    best, best_dt = None, None
    with open(pred_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                ts = _parse_ts(r["timestamp_utc"])
                gap = float(r["parallel_gap_pct"])
            except (KeyError, ValueError):
                continue
            d = abs((ts - target).total_seconds())
            if d <= 36 * 3600 and (best_dt is None or d < best_dt):
                best, best_dt = gap, d
    return best


def measure(csv_path, pred_path=None, real_csv=None):
    """Full monitoring pass. Returns dict with message lines + raw fields.

    Keys: lines(list[str]), official, gap_pct, gap_trend(delta pts),
          real_premium_pct, formula_dev_pct, n_real_reports
    """
    if pred_path is None:
        pred_path = os.path.join(os.path.dirname(csv_path), "predictions.csv")
    if real_csv is None:
        real_csv = os.path.join(os.path.dirname(csv_path), "real_prices.csv")
    out = {"lines": [], "official": None, "gap_pct": None, "gap_trend": None,
           "real_premium_pct": None, "formula_dev_pct": None,
           "n_real_reports": 0}

    row = _last_row(csv_path)
    if not row:
        return out
    try:
        site_rate = float(row["usd_egp"])
        p21 = float(row["gold_21k_sell"])
        ounce = float(row["ounce_usd"])
    except (KeyError, TypeError, ValueError):
        return out

    # 1. formula-break detector (site starts embedding real premiums)
    implied = ounce / GRAMS_PER_OUNCE * (21.0 / 24.0) * site_rate
    if implied > 0:
        dev = (p21 / implied - 1.0) * 100.0
        out["formula_dev_pct"] = dev
        if abs(dev) > FORMULA_BREAK_PCT:
            out["lines"].append(
                f"- WARNING site formula changed: 21K deviates {dev:+.2f}% "
                f"from ounce x rate (real premium appearing in source?)")

    # 2. parallel-dollar gap vs the official rate
    off = official_usdegp()
    if off:
        gap = (site_rate / off - 1.0) * 100.0
        out["official"] = off
        out["gap_pct"] = gap
        trend = _gap_trend(pred_path)
        ttxt = ""
        if trend is not None:
            delta = gap - trend
            out["gap_trend"] = delta
            word = ("widening" if delta > 0.25 else
                    "narrowing" if delta < -0.25 else "stable")
            ttxt = f", 7d ago {trend:+.2f}% -> {word}"
        out["lines"].append(
            f"- parallel-dollar: site {site_rate:.2f} vs official {off:.2f} "
            f"-> gap {gap:+.2f}%{ttxt}")

    # 3. your real souq reports vs the site reference
    rp = real_premium(real_csv, csv_path)
    if rp is not None:
        out["real_premium_pct"] = rp
        try:
            with open(real_csv, newline="", encoding="utf-8") as fh:
                out["n_real_reports"] = sum(
                    1 for _ in csv.DictReader(fh))
        except OSError:
            pass
        out["lines"].append(
            f"- real souq premium: {rp:+.2f}% vs site reference "
            f"(your reports, {REAL_REPORT_MAX_AGE_DAYS}d window)")

    return out


if __name__ == "__main__":
    # standalone smoke test: python local_premium.py [path_to_data_dir]
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if len(sys.argv) > 1:
        base = sys.argv[1]
    res = measure(os.path.join(base, "gold_prices.csv"),
                  os.path.join(base, "predictions.csv"),
                  os.path.join(base, "real_prices.csv"))
    print("formula_dev_pct :", None if res["formula_dev_pct"] is None
          else f"{res['formula_dev_pct']:+.3f}%")
    print("official usdegp :", res["official"])
    print("parallel gap    :", None if res["gap_pct"] is None
          else f"{res['gap_pct']:+.3f}%")
    for ln in res["lines"]:
        print(ln)
