#!/usr/bin/env python3
"""
Egypt Gold Watcher
==================
Scrapes live gold prices in Egypt (EGP per gram, by karat) and appends them
to a CSV price history. Designed to run on GitHub Actions every 30 minutes.

Primary source : https://www.gold-price-today.com/egypt/
                 (Cairo jeweler prices: 24k / 21k / 18k / 14k + gold pound coin)
Fallback source: api.gold-api.com (XAU/USD spot) + open.er-api.com (USD->EGP)
                 -> produces an *estimate* when the primary site is unreachable.

Telegram alert (primary channel): set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
as env vars (GitHub repo secrets) - free, 2-minute setup via @BotFather.

WhatsApp alerts (optional):
  - Green API (free developer tier, recommended): set GREENAPI_INSTANCE_ID,
    GREENAPI_API_TOKEN and WHATSAPP_PHONE. Link your WhatsApp by scanning a QR
    code once at green-api.com.
  - CallMeBot: also supported, but currently full for new signups.

Target-price alerting: set TARGET_21K (env) or pass --target-21k N to get an
alert whenever the 21K gram price CROSSES that fixed EGP level (up or down).
0 (or unset) disables the target alert - prices are still recorded.

Usage:
    python scraper.py                    # normal run (record + target alert)
    python scraper.py --dry-run          # fetch and print, write nothing
    python scraper.py --target-21k 6350  # override TARGET_21K for this run
    python scraper.py --force-notify     # send an alert even without a crossing
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
EGYPT_URL = "https://www.gold-price-today.com/egypt/"
GOLD_API_URL = "https://api.gold-api.com/price/XAU"
FX_API_URL = "https://open.er-api.com/v6/latest/USD"

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gold_prices.csv")
CSV_HEADER = [
    "timestamp_utc", "timestamp_cairo", "source",
    "gold_24k_sell", "gold_24k_buy",
    "gold_21k_sell", "gold_21k_buy",
    "gold_18k_sell", "gold_18k_buy",
    "gold_14k_sell", "gold_14k_buy",
    "gold_pound_coin", "ounce_usd", "usd_egp",
]

# Karat label -> CSV column prefix (Arabic labels used on the site)
KARAT_LABELS = {"24": "gold_24k", "21": "gold_21k", "18": "gold_18k", "14": "gold_14k"}
COIN_LABEL = "الجنيه الذهب"  # Egyptian gold pound coin

GRAMS_PER_OUNCE = 31.1034768

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ar,en;q=0.8",
}

WHATSAPP_PHONE = re.sub(r"\D", "", os.environ.get("WHATSAPP_PHONE", ""))  # digits only
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "").strip()
GREENAPI_INSTANCE_ID = os.environ.get("GREENAPI_INSTANCE_ID", "").strip()
GREENAPI_API_TOKEN = os.environ.get("GREENAPI_API_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def cairo_now():
    """Current time in Cairo (Africa/Cairo). Falls back to UTC+3 on any tz issue."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Africa/Cairo"))
    except Exception:
        return datetime.now(timezone.utc).fromtimestamp(
            time.time() + 3 * 3600, tz=timezone.utc)


def fetch(url, session, timeout=20, retries=3, as_json=False):
    """GET with retries and small backoff. Raises on final failure."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except Exception as err:  # noqa: BLE001 - network calls can fail many ways
            last_err = err
            print(f"  [fetch] attempt {attempt}/{retries} failed for {url}: {err}")
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def to_int(text):
    """'7,235 جنيه' -> 7235 (int) or None."""
    m = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    if not m:
        return None
    num = m.group(0).replace(",", "")
    try:
        val = float(num)
        return int(val) if val == int(val) else round(val, 2)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Primary scraper: gold-price-today.com/egypt
# --------------------------------------------------------------------------- #
def scrape_egypt_site(session):
    """Parse the main Egypt price table. Returns a flat price dict."""
    html = fetch(EGYPT_URL, session)
    soup = BeautifulSoup(html, "html.parser")

    # The page's first <table> holds today's prices:
    # columns: Karat | Sell price | Buy price
    table = soup.find("table")
    if table is None:
        raise ValueError("No <table> found on the Egypt page (layout may have changed)")

    prices = {}
    for tr in table.find_all("tr"):
        cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        label = cells[0]
        if "زكاة" in label:          # skip the zakat-threshold row (contains 'عيار 24' too)
            continue
        karat_m = re.fullmatch(r"عيار\s*(\d{2})", label)
        if karat_m and karat_m.group(1) in KARAT_LABELS:
            prefix = KARAT_LABELS[karat_m.group(1)]
            prices[f"{prefix}_sell"] = to_int(cells[1])
            prices[f"{prefix}_buy"] = to_int(cells[2]) if len(cells) > 2 else None
        elif COIN_LABEL in label:
            prices["gold_pound_coin"] = to_int(cells[1])
            prices["gold_pound_coin_buy"] = to_int(cells[2]) if len(cells) > 2 else None
        elif "دولار الصاغة" in label:   # jeweler USD->EGP rate, e.g. '50.80 جنيه مقابل ...'
            prices["usd_egp"] = to_int(cells[1])

    # Also grab the USD ounce price if present ('الأونصة بالدولار')
    for tr in table.find_all("tr"):
        row_text = tr.get_text(" ", strip=True)
        if "الأونصة بالدولار" in row_text:
            prices["ounce_usd"] = to_int(row_text.split("الأونصة بالدولار")[-1])
            break

    missing = [k for k in ("gold_24k_sell", "gold_21k_sell", "gold_18k_sell", "gold_14k_sell") if k not in prices]
    if missing:
        raise ValueError(f"Primary scrape incomplete, missing {missing}. Got: {prices}")

    print(f"  [source] gold-price-today.com -> {prices}")
    return {"source": "gold-price-today.com", **prices}


# --------------------------------------------------------------------------- #
# Fallback: international spot XAU/USD converted to EGP (estimate)
# --------------------------------------------------------------------------- #
def scrape_fallback(session):
    """XAU spot in USD/oz + USD->EGP rate -> estimated EGP prices per gram."""
    xau = fetch(GOLD_API_URL, session, as_json=True)
    ounce_usd = float(xau["price"])                      # USD per troy ounce
    fx = fetch(FX_API_URL, session, as_json=True)
    usd_egp = float(fx["rates"]["EGP"])                  # EGP per USD

    egp_per_gram_24k = ounce_usd / GRAMS_PER_OUNCE * usd_egp
    prices = {
        "source": "intl-spot-estimate",
        "ounce_usd": round(ounce_usd, 2),
        "usd_egp": round(usd_egp, 4),
    }
    for karat, prefix in (("24", "gold_24k"), ("21", "gold_21k"),
                          ("18", "gold_18k"), ("14", "gold_14k")):
        est = egp_per_gram_24k * (int(karat) / 24.0)
        prices[f"{prefix}_sell"] = round(est)
        prices[f"{prefix}_buy"] = None
    print(f"  [source] fallback -> {prices}")
    return prices


def get_prices(session):
    """Try primary scraper, then fallback. Raises if both fail."""
    try:
        return scrape_egypt_site(session)
    except Exception as err:  # noqa: BLE001
        print(f"  [warn] primary source failed: {err}")
        print("  [warn] switching to international-spot fallback")
        return scrape_fallback(session)


# --------------------------------------------------------------------------- #
# CSV history
# --------------------------------------------------------------------------- #
def read_last_row(path):
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if r]
        if len(rows) < 2:
            return None
        return dict(zip(CSV_HEADER, rows[-1]))


def append_row(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(CSV_HEADER)
        writer.writerow([row.get(col, "") for col in CSV_HEADER])


# --------------------------------------------------------------------------- #
# Alert message + channels (Telegram primary, WhatsApp optional)
# --------------------------------------------------------------------------- #
TRACKED = ["gold_24k_sell", "gold_21k_sell", "gold_18k_sell", "gold_14k_sell", "gold_pound_coin"]

FIELD_NAMES = {
    "gold_24k_sell": "24K/gram", "gold_21k_sell": "21K/gram",
    "gold_18k_sell": "18K/gram", "gold_14k_sell": "14K/gram",
    "gold_pound_coin": "Gold pound",
}


def fmt(n):
    return f"{int(n):,}" if n not in (None, "") else "n/a"


def build_message(current, target_hit, prev_row, target_21k):
    """Compose the alert text. target_hit = 21K crossed the target price."""
    t = current["timestamp_cairo"]
    head = "Egypt Gold Price Alert" if target_hit else "Egypt Gold Price Update"
    lines = [f"{head} - {t}", f"Source: {current['source']}"]
    if target_21k > 0:
        lines.append(f"21K target: {target_21k:g} EGP")
    lines.append("")
    for field in TRACKED:
        val = current.get(field)
        if val in (None, ""):
            continue
        if field == "gold_21k_sell" and target_hit:
            if target_hit.get("old") is not None:
                lines.append(f"{FIELD_NAMES[field]}: {fmt(target_hit['old'])} -> "
                             f"{fmt(target_hit['new'])} EGP "
                             f"({target_hit['dir']} - crossed your target) [TRIGGERED]")
            else:
                lines.append(f"{FIELD_NAMES[field]}: {fmt(val)} EGP "
                             "[already at/above your target]")
        else:
            lines.append(f"{FIELD_NAMES[field]}: {fmt(val)} EGP")
    if target_hit:
        lines.append("")
        lines.append("21K crossed your target price - that is why you got this message.")
    elif prev_row:
        lines.append("")
        lines.append("No target crossing since the last check (forced notification).")
    return "\n".join(lines)


def send_telegram(text):
    """Primary channel - free and reliable. Bot via @BotFather."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("  [telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -> skipped")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "disable_web_page_preview": True}
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    print("  [telegram] message delivered")
    return True


def send_whatsapp_greenapi(text):
    """WhatsApp via Green API free developer tier (alternative to CallMeBot).
    One-time setup: free account at green-api.com -> create an instance ->
    scan the QR code with WhatsApp -> copy the instance id and API token."""
    if not (GREENAPI_INSTANCE_ID and GREENAPI_API_TOKEN and WHATSAPP_PHONE):
        print("  [whatsapp/green-api] GREENAPI_INSTANCE_ID / GREENAPI_API_TOKEN / "
              "WHATSAPP_PHONE not set -> skipped")
        return False
    url = (f"https://api.green-api.com/waInstance{GREENAPI_INSTANCE_ID}"
           f"/sendMessage/{GREENAPI_API_TOKEN}")
    resp = requests.post(url, json={"chatId": f"{WHATSAPP_PHONE}@c.us",
                                    "message": text}, timeout=20)
    resp.raise_for_status()
    print("  [whatsapp/green-api] message handed to Green API")
    return True


def send_whatsapp_callmebot(text):
    """WhatsApp via CallMeBot - kept for when it reopens for new signups
    (it is currently full). One-time setup at callmebot.com/whatsapp.php."""
    if not (WHATSAPP_PHONE and CALLMEBOT_APIKEY):
        print("  [whatsapp/callmebot] WHATSAPP_PHONE / CALLMEBOT_APIKEY not set -> skipped")
        return False
    resp = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": WHATSAPP_PHONE, "text": text, "apikey": CALLMEBOT_APIKEY},
        timeout=20,
    )
    resp.raise_for_status()
    snippet = resp.text[:150].strip()
    if "error" in snippet.lower() or "invalid" in snippet.lower():
        raise RuntimeError(f"CallMeBot replied with an error: {snippet}")
    print("  [whatsapp/callmebot] message handed to CallMeBot")
    return True


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="Egypt gold price watcher")
    parser.add_argument("--dry-run", action="store_true", help="do not write CSV")
    parser.add_argument("--force-notify", action="store_true",
                        help="send an alert even if nothing crossed the threshold")
    parser.add_argument("--target-21k", type=float, default=None,
                        help="alert when the 21K price crosses this fixed EGP level "
                             "(overrides TARGET_21K env)")
    args = parser.parse_args()

    raw_target = args.target_21k
    if raw_target is None:
        raw_target = os.environ.get("TARGET_21K", "")
    try:
        target_21k = float(raw_target) if str(raw_target).strip() else 0.0
    except ValueError:
        print(f"  [warn] invalid TARGET_21K={raw_target!r} -> target alert disabled")
        target_21k = 0.0

    session = requests.Session()

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    now_cairo = cairo_now().strftime("%Y-%m-%d %H:%M:%S")

    prices = get_prices(session)
    current = {"timestamp_utc": now_utc, "timestamp_cairo": now_cairo, **prices}

    prev_row = None if args.dry_run else read_last_row(CSV_PATH)

    # 21K target-price alert: fire when the 21K price crosses the target level.
    target_hit = None
    if target_21k > 0 and current.get("gold_21k_sell") is not None:
        cur = float(current["gold_21k_sell"])
        if prev_row is None:
            if cur >= target_21k:
                target_hit = {"old": None, "new": cur, "dir": "UP"}
        else:
            prev_val = prev_row.get("gold_21k_sell")
            if prev_val not in (None, ""):
                prev_val = float(prev_val)
                if (prev_val < target_21k) != (cur < target_21k):
                    target_hit = {"old": prev_val, "new": cur,
                                  "dir": "UP" if cur > prev_val else "DOWN"}

    print("\n=== Egypt gold prices ===")
    print(f"  Time (Cairo) : {now_cairo}")
    print(f"  Source       : {current['source']}")
    if target_21k > 0:
        print(f"  21K target   : {target_21k:g} EGP")
    for field in TRACKED:
        if current.get(field) not in (None, ""):
            print(f"  {field:<16}: {fmt(current[field])} EGP")
    print(f"  Target crossed: {'yes (' + target_hit['dir'] + ')' if target_hit else 'no'}")

    if not args.dry_run:
        append_row(CSV_PATH, current)
        print(f"  Appended row to {CSV_PATH}")

    should_notify = bool(target_hit) or args.force_notify or prev_row is None
    if should_notify:
        msg = build_message(current, target_hit, prev_row, target_21k)
        print("\n--- Alert message ---")
        print(msg)
        if not args.dry_run:
            channels = []
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                channels.append(("telegram", send_telegram))
            if GREENAPI_INSTANCE_ID and GREENAPI_API_TOKEN and WHATSAPP_PHONE:
                channels.append(("whatsapp/green-api", send_whatsapp_greenapi))
            if WHATSAPP_PHONE and CALLMEBOT_APIKEY:
                channels.append(("whatsapp/callmebot", send_whatsapp_callmebot))
            if not channels:
                print("  [alerts] no messaging channel configured -> skipped")
            for channel, sender in channels:
                try:
                    sender(msg)
                except Exception as err:  # noqa: BLE001 - never fail the run on notify
                    print(f"  [{channel}] ERROR: {err}")
    else:
        print("  21K did not cross the target -> no notification")

    if not current.get("gold_24k_sell"):
        print("FATAL: could not obtain any 24k price", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
