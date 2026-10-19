#!/usr/bin/env python3
"""
Gold news reader: RSS -> article text -> LLM extraction -> verified levels.
==========================================================================
Pipeline per run:
  1. Pull latest headlines from RSS feeds (Yahoo gold futures, Google News
     EN + AR, ForexLive, Investing.com)
  2. Filter to gold-relevant items, round-robin across feeds, keep the N most useful
  3. Fetch article bodies, extract readable text
  4. LLM extracts {bias, confidence, levels[]} per article  (llm.py adapter)
  5. VERIFICATION PASS: every extracted price must literally appear in the
     article text (comma/no-comma variants) -- hallucinated numbers are dropped
  6. Aggregate into a news score in [-1, +1] + verified levels list

Graceful degradation: no LLM key, network failure, or zero articles ->
score 0.0, used_llm=False. The gauge still works (technical + consensus).

Usage:
    python news_reader.py --dry-run --max-articles 3
"""
import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

import llm

YAHOO_GC_RSS = ("https://feeds.finance.yahoo.com/rss/2.0/headline?s=GC=F"
                "&region=US&lang=en-US")     # gold futures: DIRECT article links
GOOGLE_NEWS_EN = ("https://news.google.com/rss/search?q=gold+price+forecast+OR+"
                  "xauusd+OR+%22gold+rally%22&hl=en-US&gl=US&ceid=US:en")
GOOGLE_NEWS_AR = ("https://news.google.com/rss/search?q=%D8%A3%D8%B3%D8%B9%D8%A7%D8%B1"
                  "+%D8%A7%D9%84%D8%B0%D9%87%D8%A8+%D9%81%D9%8A+%D9%85%D8%B5%D8%B1"
                  "&hl=ar&gl=EG&ceid=EG:ar")   # "أسعار الذهب في مصر" (Egypt focus)
FOREXLIVE_RSS = "https://www.forexlive.com/feed/news"
INVESTING_RSS = "https://www.investing.com/rss/news_11.rss"

RSS_FEEDS = [("yahoo-gold-futures", YAHOO_GC_RSS),
             ("google-news-en", GOOGLE_NEWS_EN),
             ("google-news-ar", GOOGLE_NEWS_AR),
             ("forexlive", FOREXLIVE_RSS),
             ("investing-commodities", INVESTING_RSS)]

# Feeds whose links never yield a fetchable article body (news.google.com
# links are JS redirect wrappers; investing.com returns 403). Their RSS text
# is used as-is, so no request is wasted and no 403 noise appears.
WRAPPER_FEEDS = ("google-news", "investing")

GOLD_KEYWORDS = re.compile(
    r"gold|xau|bullion|fed|dollar|([\u0600-\u06FF]*\u0630\u0647\u0628)", re.IGNORECASE)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en,ar;q=0.8",
}

BIAS_TO_SIGN = {"up": 1.0, "down": -1.0, "neutral": 0.0}


# --------------------------------------------------------------------------- #
# RSS + article fetching
# --------------------------------------------------------------------------- #
def fetch_rss(url, timeout=15):
    """Minimal RSS 2.0 parser (stdlib ET). Returns list of item dicts."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()
        if title:
            items.append({"title": title, "link": link, "description": desc})
    return items


def collect_headlines(max_per_feed=12):
    """Pull + filter headlines from all feeds. Tolerates individual feed failure."""
    seen, out = set(), []
    for name, url in RSS_FEEDS:
        try:
            for it in fetch_rss(url):
                key = it["title"][:80].lower()
                if key in seen:
                    continue
                if not GOLD_KEYWORDS.search(it["title"] + " " + it["description"]):
                    continue
                seen.add(key)
                out.append({**it, "feed": name})
                if len([o for o in out if o["feed"] == name]) >= max_per_feed:
                    break
        except Exception as err:  # noqa: BLE001 - one dead feed must not kill the run
            print(f"  [news] feed {name} failed: {err}", file=sys.stderr)
    # Round-robin interleave so the top N articles span every feed instead
    # of clustering into whichever feed sorts first alphabetically.
    buckets = {}
    for it in out:
        buckets.setdefault(it["feed"], []).append(it)
    out = []
    while any(buckets.values()):
        for name, _ in RSS_FEEDS:
            if buckets.get(name):
                out.append(buckets[name].pop(0))
    return out


def fetch_article_text(url, timeout=15, max_chars=3000):
    """Fetch an article page and return readable text (BeautifulSoup).

    Prefers <p> paragraphs (skips nav menus, icon-font glyph walls and
    cookie banners); falls back to whole-page text if too little is found.
    """
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    paras = []
    for p in soup.find_all("p"):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(t) >= 40:                       # skip buttons, links, captions
            paras.append(t)
    text = " ".join(paras)
    if len(text) < 200:
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return text[:max_chars]


# --------------------------------------------------------------------------- #
# Verification pass (anti-hallucination)
# --------------------------------------------------------------------------- #
def _normalize(text):
    return (text or "").lower().replace(",", "").replace("'", "")


def verify_level(price, unit, body):
    """True only if the number literally appears in the article text.

    Accepts integer and one-decimal variants; commas are stripped on both
    sides before matching. EGP/USD sanity ranges applied per unit.
    """
    try:
        price_f = float(price)
    except (TypeError, ValueError):
        return False
    ranges = {
        "USD/ounce": (500, 20000), "EGP/gram": (500, 50000),
        "USD/gram": (10, 700), "EGP/coin": (10000, 300000),
    }
    lo, hi = ranges.get(unit or "", (0, 1e9))
    if not (lo <= price_f <= hi):
        return False
    nb = _normalize(body)
    candidates = [str(int(round(price_f)))]
    if abs(price_f - round(price_f)) > 1e-9:
        candidates.append(f"{price_f:.1f}")
        candidates.append(f"{price_f:.2f}")
    return any(c.replace(",", "") in nb for c in candidates)


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def read_news(max_articles=4, fetch_bodies=True):
    """Returns {'group': 'news', 'score', 'weight', 'detail', 'levels', 'used_llm'}."""
    result = {"group": "news", "score": 0.0, "weight": 1.0,
              "detail": "news unavailable", "levels": [], "used_llm": False}
    if not llm.llm_available():
        result["weight"] = 0.0          # unavailable -> excluded from merge
        result["detail"] = "no LLM configured -> news group skipped"
        print(f"  [news] {result['detail']}", file=sys.stderr)
        return result

    headlines = collect_headlines()
    if not headlines:
        result["weight"] = 0.0
        result["detail"] = "no gold headlines found"
        return result
    headlines = headlines[:max_articles]

    scores, verified_levels, used = [], [], 0
    for item in headlines:
        title = item["title"]
        try:
            # Google News links are redirect wrappers; the description often
            # carries enough context, so use title+desc as the minimum corpus.
            is_wrapper = item["feed"].startswith(WRAPPER_FEEDS)
            body = item["description"]
            if fetch_bodies and not is_wrapper and item["link"].startswith("http"):
                try:
                    body = (fetch_article_text(item["link"]) or "") or body
                except Exception as err:  # noqa: BLE001
                    print(f"  [news] body fetch failed, using RSS text: {err}",
                          file=sys.stderr)
            corpus = f"{title}. {body}"
            extracted = llm.read_article(title, corpus)
            if extracted is None:
                continue
            used += 1
            bias = str(extracted.get("bias", "neutral")).lower()
            conf = max(0.0, min(100.0, float(extracted.get("confidence", 0) or 0)))
            scores.append(BIAS_TO_SIGN.get(bias, 0.0) * conf / 100.0)
            for lvl in extracted.get("levels", []) or []:
                price, unit, ltype = lvl.get("price"), lvl.get("unit"), lvl.get("type")
                if verify_level(price, unit, corpus):
                    verified_levels.append({"price": float(price), "unit": unit,
                                            "type": str(ltype or "target").lower(),
                                            "title": title[:60]})
                else:
                    print(f"  [news] dropped unverifiable level {price} {unit}",
                          file=sys.stderr)
        except Exception as err:  # noqa: BLE001 - per-article isolation
            print(f"  [news] article '{title[:40]}' failed: {err}", file=sys.stderr)

    if used == 0:
        result["weight"] = 0.0          # LLM ran but produced nothing usable
        result["detail"] = f"{len(headlines)} headlines fetched but LLM extraction failed"
        return result

    result["score"] = round(sum(scores) / len(scores), 3) if scores else 0.0
    result["levels"] = verified_levels
    result["used_llm"] = True
    result["detail"] = (f"{used}/{len(headlines)} articles read -> "
                        f"score {result['score']:+.2f}, {len(verified_levels)} verified levels")
    return result


def main():
    ap = argparse.ArgumentParser(description="News-based signal (LLM)")
    ap.add_argument("--max-articles", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="identical to normal run; "
                    "never writes anything (kept for scraper.py parity)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        result = read_news(max_articles=args.max_articles)
    except Exception as err:  # noqa: BLE001
        print(f"[news] ERROR: {err}", file=sys.stderr)
        result = {"group": "news", "score": 0.0, "weight": 0.0,
                  "detail": f"unavailable: {err}", "levels": [], "used_llm": False}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[news] score {result['score']:+.3f}  ({result['detail']})")
        for lvl in result.get("levels", []):
            print(f"       level: {lvl['price']:,.0f} {lvl['unit']} {lvl['type']}"
                  f"  <- '{lvl['title']}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
