#!/usr/bin/env python3
"""
Daily AI agent review (v1).
===========================
Runs once per day at END OF DAY Cairo (cron 21:00 UTC in
.github/workflows/daily-agent.yml; 21:00 UTC = 23:00 Cairo EET /
00:00 Cairo EEST). Manual run: Actions tab -> Daily AI Review ->
Run workflow, or locally: `python daily_agent.py --digest`.

This is NOT a fixed heuristic tuner. Every day an AI agent (the LLM
behind LLM_BASE_URL/LLM_API_KEY/LLM_MODEL - the same key the news
reader uses) receives a complete data digest of the project and
DECIDES what, if anything, to change to improve accuracy:

  1. DIGEST   - data health, yesterday's market, accuracy (strict /
                direction / FLAT), alarm report card, per-signal-group
                agreement with the real 24h move, accuracy trend by day,
                recent graded rows, current weights/knobs/thresholds,
                past tuning history.
  2. AGENT    - the LLM reasons over the digest and returns a JSON
                decision: analysis (EN+AR), a small set of changes,
                and a ready-to-send Telegram report (EN+AR).
  3. RAILS    - the runner validates everything and clamps:
                weights [0.10..0.60] renormalized to 1.0, max move
                0.10/group/day; ladder_confirm [0..6]; milestone_pct
                [0.3..1.0]; up_threshold [55..70] / down_threshold
                [30..45] with up >= down + 10; max 3 changes; nothing
                at all when graded calls < 15.
  3b. CODE PATCH - if the agent sees concrete evidence of a CODE or DATA
                bug (broken scraper source, wrong grading, malformed rows,
                logic error) it may request to inspect an allowlisted file,
                receive the relevant region, and return small find/replace
                patches. The runner enforces hard rails: file allowlist,
                exact-match find, size caps, an injection scan (no env
                access / subprocess / eval / unknown hosts in added code),
                py_compile + import smoke test, and automatic rollback on
                any failure. Applied patches get their own git commit and
                are logged to data/patch_log.csv.
  4. APPLY    - accepted decisions go to tuning.json (predictor.py
                reads it; env vars still override) and to
                data/tuning_log.csv. Everything lands as a git commit.
  5. REPORT   - the agent's own Telegram message PLUS a factual code-
                patch section (ground truth from the runner) is sent
                through the
                project bot (same secrets/recipients as the gauge).
                If the agent is unavailable, a template digest report
                is sent instead (report-only, never a blind change).

Flags:
  --digest   print the digest JSON and exit (used by the maintainer's
             AI assistant for interactive deep-dives)
  --no-llm   skip the agent, send the template report (plumbing test)

Exit code is always 0 so the workflow's commit step can still store
grading/tuning output.
"""
import argparse
import ast
import csv
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PRED_PATH = os.path.join(DATA_DIR, "predictions.csv")
ALARMS_PATH = os.path.join(DATA_DIR, "alarms.csv")
PRICES_PATH = os.path.join(DATA_DIR, "gold_prices.csv")
TUNING_PATH = os.path.join(BASE_DIR, "tuning.json")
TUNING_LOG_PATH = os.path.join(DATA_DIR, "tuning_log.csv")

TUNING_LOG_HEADER = ["date_utc", "n_graded", "before", "after",
                     "changes", "analysis"]
GROUPS = ("technical", "consensus", "news")
ACC_WINDOW = 30
MOVE_DEAD = 0.10       # |24h move %| below this = flat -> excluded
SCORE_DEAD = 0.05      # |group score| below this = neutral -> excluded

# ---- safety rails --------------------------------------------------------- #
W_MIN, W_MAX = 0.10, 0.60
W_MAX_MOVE = 0.10      # max weight change per group per day
KNOB_RANGES = {"ladder_confirm": (0.0, 6.0), "milestone_pct": (0.3, 1.0)}
THRESH_RANGES = {"up_threshold": (55.0, 70.0), "down_threshold": (30.0, 45.0)}
MAX_CHANGES = 3
MIN_GRADED_FOR_CHANGES = 15
DEFAULT_WEIGHTS = {"technical": 0.50, "consensus": 0.20, "news": 0.30}
DEFAULT_KNOBS = {"ladder_confirm": 2.0, "milestone_pct": 0.5}
DEFAULT_THRESHOLDS = {"up_threshold": 58, "down_threshold": 42}

# ---- code-patch rails ----------------------------------------------------- #
ALLOW_CODE_PATCH = os.environ.get("ALLOW_CODE_PATCH", "1") not in (
    "0", "false", "no")
PATCH_FILES = frozenset({
    "predictor.py", "technical.py", "consensus.py", "news_reader.py",
    "scraper.py", "real_prices.py", "llm.py"})
PATCH_MAX_BLOCKS = 3       # find/replace blocks per night
PATCH_MAX_BLOCK_LINES = 60 # max lines of find OR replace per block
PATCH_MAX_ROUNDS = 2       # inspection rounds before the final patch
PATCH_LOG_PATH = os.path.join(DATA_DIR, "patch_log.csv")
PATCH_LOG_HEADER = ["date_utc", "status", "files", "blocks", "changed_lines",
                    "commit", "why", "detail"]

SYS_PROMPT = (
    "You are the daily AI reviewer and tuner of 'gold_watcher', an Egypt "
    "gold-price watcher that logs a prediction gauge every hour and "
    "self-grades each call against the real 21K EGP price 24 hours later.\n"
    "\n"
    "YOUR JOB (once per day, end of Cairo day):\n"
    "1. Read the data digest below (accuracy, per-signal-group agreement "
    "with the real move, alarms, market, data health, current config, your "
    "own past tuning history).\n"
    "2. Reason about what is helping and what is hurting accuracy.\n"
    "3. Decide a SMALL set of changes the evidence supports. Changing less "
    "is better than changing more; if the data is noisy, change nothing and "
    "say why.\n"
    "4. Write a compact Telegram report in English and Arabic: what you "
    "found, what you changed and why (or why nothing changed).\n"
    "\n"
    "WHAT YOU MAY CHANGE (the runner clamps everything):\n"
    '- {"type": "weight", "group": "technical|consensus|news", "value": x} '
    "- each lands in [0.10..0.60], renormalized to sum 1.0; max move 0.10 "
    "per group per day.\n"
    '- {"type": "knob", "name": "ladder_confirm", "value": x} in [0..6] - '
    "anti-twitch confirm points: raise if alarms keep missing, lower only "
    "if alarms earn trust.\n"
    '- {"type": "knob", "name": "milestone_pct", "value": x} in [0.3..1.0] '
    "- sell-window sensitivity above the day's average price.\n"
    '- {"type": "threshold", "name": "up_threshold"|"down_threshold", '
    '"value": x} in [55..70]/[30..45] with up >= down + 10 - the FLAT band; '
    "HIGH-IMPACT: changes how often the gauge commits to a direction, "
    "change rarely and at most 2 points per day.\n"
    "\n"
    "HARD RULES (enforced by the runner anyway):\n"
    "- If graded calls < 15 you may NOT change anything - report only.\n"
    "- At most 3 changes per day; 0-1 is usually right.\n"
    "- Direction accuracy on UP/DOWN calls matters most; FLAT quality "
    "second; strict includes the landing window, so treat it as the harsh "
    "lens.\n"
    "- The technical group reads LOCAL 21K EGP prices and carries the "
    "parallel-dollar signal; a 5-year study set 50/20/30. A sudden drop in "
    "technical agreement can mean a market REGIME change, not a bad signal "
    "- prefer slow moves and say so in the report.\n"
    "- Do not flip-flop: check tuning_history; undo yesterday's move only "
    "with fresh evidence.\n"
    "\n"
    "GRADING RULES (for interpretation): strict = right direction with the "
    "move measured 24h later; a FLAT call counts correct only if the price "
    "moved within +/-0.1% and the gauge was near 50; direction score = "
    "UP/DOWN calls only, sign of the move.\n"
    "\n"
    "OUTPUT - reply with ONLY a JSON object, no markdown:\n"
    "{\n"
    ' "analysis_en": "<=60 words, cite the key numbers",\n'
    ' "analysis_ar": "Arabic translation of analysis_en",\n'
    ' "changes": [ ... 0..3 change objects ... ],\n'
    ' "report_en": "full Telegram message in English, plain text, section '
    "titles + blank lines, max 1800 chars\",\n"
    ' "report_ar": "Arabic version of report_en, max 1800 chars",\n'
    ' "code_review": omit or null normally. ONLY when the digest shows '
    "concrete\n"
    "   evidence of a CODE or DATA bug (broken scraper source, wrong "
    "grading,\n"
    "   malformed rows, overdue grading growing, a logic error visible in "
    "the\n"
    '   numbers) request: {"files": ["scraper.py"], "focus": "keyword or '
    'function",\n'
    '   "why": "<= 40 words citing the evidence"}. Allowed files: '
    "predictor.py,\n"
    "   technical.py, consensus.py, news_reader.py, scraper.py, "
    "real_prices.py,\n"
    "   llm.py. A normal accuracy dip or noisy market is NOT a code problem "
    "-\n"
    "   do not request a review for that.\n"
    "}\n"
    "If no changes: \"changes\": []."
)


def utcnow():
    return datetime.now(timezone.utc)


def now_utc_str():
    return utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r]


# --------------------------------------------------------------------------- #
# Digest sections
# --------------------------------------------------------------------------- #
def health_section():
    out = {}
    prices = _read_csv(PRICES_PATH)
    out["price_rows"] = len(prices)
    if prices:
        last = prices[-1]
        out["last_price_utc"] = last.get("timestamp_utc", "")
        try:
            ts = datetime.strptime(last["timestamp_utc"],
                                   "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            out["last_age_h"] = round((utcnow() - ts).total_seconds() / 3600.0, 1)
        except (ValueError, KeyError):
            out["last_age_h"] = None
        cutoff = utcnow() - timedelta(hours=24)
        out["rows_24h"] = sum(
            1 for r in prices[-60:]
            if _parse_ts(r.get("timestamp_utc")) and _parse_ts(r["timestamp_utc"]) >= cutoff)
        mix = {}
        for r in prices[-48:]:
            mix[r.get("source", "?")] = mix.get(r.get("source", "?"), 0) + 1
        out["source_mix_48"] = mix
    pred = _read_csv(PRED_PATH)
    graded, pending, overdue = 0, 0, 0
    for r in pred:
        if r.get("graded_at_utc"):
            graded += 1
            continue
        ts = _parse_ts(r.get("timestamp_utc"))
        if ts is None:
            continue
        if utcnow() - ts >= timedelta(hours=24):
            overdue += 1
        else:
            pending += 1
    out.update({"pred_rows": len(pred), "graded_n": graded,
                "pending_n": pending, "overdue_n": overdue})
    return out


def _parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def market_section():
    prices = _read_csv(PRICES_PATH)
    if not prices:
        return None
    last_day = prices[-1].get("timestamp_cairo", "")[:10]
    if not last_day:
        return None
    try:
        yday = (datetime.strptime(last_day, "%Y-%m-%d")
                - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return None
    rows = [r for r in prices if r.get("timestamp_cairo", "")[:10] == yday]
    vals = []
    for r in rows:
        try:
            vals.append((float(r["gold_21k_sell"]), r.get("source", "?")))
        except (ValueError, KeyError, TypeError):
            continue
    if not vals:
        return None
    px = [v[0] for v in vals]
    src = {}
    for _, s in vals:
        src[s] = src.get(s, 0) + 1
    ounces = []
    for r in rows:
        try:
            ounces.append(float(r["ounce_usd"]))
        except (ValueError, KeyError, TypeError):
            continue
    out = {"date": yday, "open": px[0], "close": px[-1], "low": min(px),
           "high": max(px), "checks": len(px), "sources": src,
           "chg": px[-1] - px[0],
           "chg_pct": (px[-1] - px[0]) / px[0] * 100.0 if px[0] else 0.0}
    if ounces:
        out["ounce_open"], out["ounce_close"] = ounces[0], ounces[-1]
        out["ounce_chg_pct"] = ((ounces[-1] - ounces[0]) / ounces[0] * 100.0
                                if ounces[0] else 0.0)
    return out


def accuracy_section():
    graded = [r for r in _read_csv(PRED_PATH) if r.get("graded_at_utc")]
    if not graded:
        return None

    def strict(rows):
        if not rows:
            return None
        h = sum(1 for r in rows if r.get("predicted_correct") == "1")
        return {"hits": h, "n": len(rows), "pct": round(100.0 * h / len(rows))}

    def direction(rows):
        dn = dh = 0
        for r in rows:
            if r.get("direction") not in ("UP", "DOWN"):
                continue
            try:
                called, later = float(r["price_21k"]), float(r["price_24h_later"])
            except (ValueError, KeyError, TypeError):
                continue
            if not called:
                continue
            move = (later - called) / called
            good = move > 0 if r["direction"] == "UP" else move < 0
            dn += 1
            dh += 1 if good else 0
        return {"hits": dh, "n": dn,
                "pct": round(100.0 * dh / dn)} if dn else None

    def flat_q(rows):
        flats = [r for r in rows if r.get("direction") == "FLAT"]
        if not flats:
            return None
        h = sum(1 for r in flats if r.get("predicted_correct") == "1")
        return {"hits": h, "n": len(flats), "pct": round(100.0 * h / len(flats))}

    by_day = {}
    for r in graded:
        day = r["graded_at_utc"][:10]
        d = by_day.setdefault(day, {"n": 0, "hits": 0})
        d["n"] += 1
        d["hits"] += 1 if r.get("predicted_correct") == "1" else 0
    for d in by_day.values():
        d["pct"] = round(100.0 * d["hits"] / d["n"]) if d["n"] else 0
    recent_days = dict(sorted(by_day.items())[-7:])
    return {"all": strict(graded),
            "recent": strict(graded[-ACC_WINDOW:]),
            "dir": direction(graded),
            "dir_recent": direction(graded[-ACC_WINDOW:]),
            "flat": flat_q(graded),
            "total_graded": len(graded),
            "by_grading_day": recent_days}


def alarm_section():
    rows = _read_csv(ALARMS_PATH)
    graded = [r for r in rows if str(r.get("graded", "")).strip() == "1"]
    hits = sum(1 for r in graded
               if str(r.get("verdict", "")).strip().lower() in ("hit", "beat"))
    recent = [{"utc": r.get("timestamp_utc"), "kind": r.get("kind"),
               "detail": r.get("detail"), "est_pct": r.get("est_pct"),
               "actual_pct": r.get("actual_pct"), "verdict": r.get("verdict")}
              for r in rows[-5:]]
    card = ({"hits": hits, "n": len(graded),
             "pct": round(100.0 * hits / len(graded))} if graded else None)
    return {"card": card, "recent": recent}


def group_section():
    graded = [r for r in _read_csv(PRED_PATH) if r.get("graded_at_utc")][-60:]
    if not graded:
        return None
    out = {"window_n": len(graded), "groups": {}}
    for g in GROUPS:
        n = hits = 0
        for r in graded:
            try:
                s = float(r.get(g, ""))
                called = float(r["price_21k"])
                later = float(r["price_24h_later"])
            except (ValueError, KeyError, TypeError):
                continue
            if not called:
                continue
            move_pct = (later - called) / called * 100.0
            if abs(move_pct) <= MOVE_DEAD or abs(s) < SCORE_DEAD:
                continue
            n += 1
            hits += 1 if ((move_pct > 0) == (s > 0)) else 0
        out["groups"][g] = {"n": n, "hits": hits,
                            "pct": round(100.0 * hits / n) if n else None}
    return out


def recent_rows_section(k=8):
    graded = [r for r in _read_csv(PRED_PATH) if r.get("graded_at_utc")][-k:]
    out = []
    for r in graded:
        out.append({c: r.get(c) for c in
                    ("timestamp_utc", "final_pct", "direction", "technical",
                     "consensus", "news", "price_21k", "price_24h_later",
                     "predicted_correct")})
    return out


def config_section():
    weights, knobs, thresholds = _load_tuning()
    return {"weights": weights, "knobs": knobs, "thresholds": thresholds,
            "note": "env vars override tuning.json; predictor reads it on "
                    "every run"}


def tuning_history_section(k=5):
    rows = _read_csv(TUNING_LOG_PATH)
    return rows[-k:]


# --------------------------------------------------------------------------- #
# tuning.json load/save (predictor.py reads this file on every run)
# --------------------------------------------------------------------------- #
def _load_tuning():
    weights, knobs, thresholds = (dict(DEFAULT_WEIGHTS), dict(DEFAULT_KNOBS),
                                  dict(DEFAULT_THRESHOLDS))
    try:
        with open(TUNING_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        for k, v in (data.get("weights") or {}).items():
            if k in weights and isinstance(v, (int, float)) and 0 < v <= 1:
                weights[k] = float(v)
        for k, v in (data.get("knobs") or {}).items():
            if k in knobs and isinstance(v, (int, float)):
                knobs[k] = float(v)
        for k, v in (data.get("thresholds") or {}).items():
            if k in thresholds and isinstance(v, (int, float)):
                thresholds[k] = float(v)
    except (OSError, ValueError, AttributeError):
        pass
    total = sum(weights.values()) or 1.0
    weights = {k: v / total for k, v in weights.items()}
    return weights, knobs, thresholds


def _renorm(w):
    total = sum(w.values()) or 1.0
    w = {k: v / total for k, v in w.items()}
    w = {k: round(v, 3) for k, v in w.items()}
    drift = round(1.0 - sum(w.values()), 3)
    if abs(drift) >= 0.001:
        big = max(w, key=w.get)
        w[big] = round(w[big] + drift, 3)
    return w


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------- #
# The agent brain
# --------------------------------------------------------------------------- #
def build_digest():
    hlth = acc = alarm = gperf = mkt = None
    try:
        hlth = health_section()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        mkt = market_section()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        acc = accuracy_section()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        alarm = alarm_section()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        gperf = group_section()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return {
        "as_of_utc": now_utc_str(),
        "health": hlth,
        "yesterday_market": mkt,
        "accuracy": acc,
        "alarms": alarm,
        "signals_last60": gperf,
        "recent_graded_rows": recent_rows_section(),
        "config": config_section(),
        "tuning_history": tuning_history_section(),
    }


def _llm_cfg():
    return {"base": os.environ.get("LLM_BASE_URL", "").strip().rstrip("/"),
            "key": os.environ.get("LLM_API_KEY", "").strip(),
            "model": os.environ.get("LLM_MODEL", "").strip()}


def call_agent_raw(messages, max_tokens=1800, retries=2):
    """Send a message list to the LLM agent. Returns parsed dict | None."""
    cfg = _llm_cfg()
    if not (cfg["base"] and cfg["key"] and cfg["model"]):
        return None
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {cfg['key']}"}
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(f"{cfg['base']}/chat/completions",
                                 json=payload, headers=headers, timeout=60)
            if resp.status_code == 429 and attempt < retries:
                hint = re.search(r"try again in ([\d.]+)s", resp.text)
                time.sleep(min(float(hint.group(1)) + 0.5, 20.0) if hint else 5.0)
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content or "", re.DOTALL)
            if not m:
                raise ValueError("no JSON object in agent reply")
            return json.loads(m.group(0))
        except Exception as err:  # noqa: BLE001
            print(f"  [agent] attempt {attempt}/{retries} failed: {err}",
                  file=sys.stderr)
            if attempt < retries:
                time.sleep(4.0)
    return None


def call_agent(digest, retries=2):
    """Ask the LLM agent to review the digest. Returns dict | None."""
    cfg = _llm_cfg()
    if not (cfg["base"] and cfg["key"] and cfg["model"]):
        print("  [agent] LLM_* not configured -> report-only fallback")
        return None
    return call_agent_raw(
        [{"role": "system", "content": SYS_PROMPT},
         {"role": "user",
          "content": "DATA DIGEST:\n" + json.dumps(digest, ensure_ascii=False)}],
        retries=retries)


# --------------------------------------------------------------------------- #
# Rails: validate + clamp the agent's decisions, then apply
# --------------------------------------------------------------------------- #
def validate_changes(decisions, digest, weights, knobs, thresholds):
    """Returns (new_weights, new_knobs, new_thresholds, applied, dropped_notes)."""
    applied, notes = [], []
    n_graded = 0
    if digest.get("accuracy"):
        n_graded = digest["accuracy"].get("total_graded", 0)
    raw = decisions.get("changes") if isinstance(decisions, dict) else None
    raw = raw if isinstance(raw, list) else []
    if n_graded < MIN_GRADED_FOR_CHANGES and raw:
        notes.append(f"dropped all {len(raw)} change(s): only {n_graded} graded "
                     f"calls (< {MIN_GRADED_FOR_CHANGES}) - report-only day")
        raw = []
    for c in raw[:MAX_CHANGES]:
        if not isinstance(c, dict):
            continue
        ctype = str(c.get("type", "")).strip().lower()
        try:
            if ctype == "weight":
                g = str(c.get("group", "")).strip().lower()
                if g not in GROUPS:
                    notes.append(f"dropped weight change: unknown group {g!r}")
                    continue
                v = _clamp(float(c["value"]), W_MIN, W_MAX)
                v = _clamp(v, weights[g] - W_MAX_MOVE, weights[g] + W_MAX_MOVE)
                applied.append({"type": "weight", "group": g,
                                "from": round(weights[g], 3), "to": round(v, 3)})
                weights[g] = v
            elif ctype == "knob":
                name = str(c.get("name", "")).strip().lower()
                if name not in KNOB_RANGES:
                    notes.append(f"dropped knob change: unknown {name!r}")
                    continue
                lo, hi = KNOB_RANGES[name]
                v = _clamp(float(c["value"]), lo, hi)
                applied.append({"type": "knob", "name": name,
                                "from": knobs[name], "to": round(v, 3)})
                knobs[name] = round(v, 3)
            elif ctype == "threshold":
                name = str(c.get("name", "")).strip().lower()
                if name not in THRESH_RANGES:
                    notes.append(f"dropped threshold change: unknown {name!r}")
                    continue
                lo, hi = THRESH_RANGES[name]
                v = _clamp(float(c["value"]), lo, hi)
                key = "up_threshold" if name == "up_threshold" else "down_threshold"
                applied.append({"type": "threshold", "name": key,
                                "from": thresholds[key], "to": int(v)})
                thresholds[key] = int(v)
            else:
                notes.append(f"dropped unknown change type {ctype!r}")
        except (KeyError, TypeError, ValueError) as err:
            notes.append(f"dropped malformed change {c!r}: {err}")
    if len(raw) > MAX_CHANGES:
        notes.append(f"agent tried {len(raw)} changes; kept first {MAX_CHANGES}")
    # pairing rule: up >= down + 10
    if thresholds["up_threshold"] < thresholds["down_threshold"] + 10:
        thresholds["up_threshold"] = min(
            int(thresholds["down_threshold"]) + 10,
            int(THRESH_RANGES["up_threshold"][1]))
        thresholds["down_threshold"] = max(
            int(thresholds["up_threshold"]) - 10,
            int(THRESH_RANGES["down_threshold"][0]))
        applied.append({"type": "threshold", "name": "pairing_fix",
                        "to": [thresholds["up_threshold"],
                               thresholds["down_threshold"]]})
    new_weights = _renorm(weights)
    for a in applied:                       # keep 'to' honest post-renorm
        if a.get("type") == "weight":
            a["to"] = new_weights[a["group"]]
    return new_weights, knobs, thresholds, applied, notes


def apply_tuning(new_weights, new_knobs, new_thresholds, applied, decisions,
                 digest, notes):
    old_weights, old_knobs, old_thresholds = _load_tuning()
    analysis_en = str(decisions.get("analysis_en", "")).strip() \
        if isinstance(decisions, dict) else ""
    payload = {
        "updated_utc": now_utc_str(),
        "weights": new_weights,
        "knobs": new_knobs,
        "thresholds": new_thresholds,
        "last_change_reason": analysis_en or "agent change",
    }
    with open(TUNING_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.makedirs(DATA_DIR, exist_ok=True)
    exists = os.path.exists(TUNING_LOG_PATH)
    with open(TUNING_LOG_PATH, "a", newline="", encoding="utf-8") as fh:
        wtr = csv.writer(fh)
        if not exists:
            wtr.writerow(TUNING_LOG_HEADER)
        wtr.writerow([now_utc_str(),
                      digest.get("accuracy", {}).get("total_graded", 0)
                      if digest.get("accuracy") else 0,
                      json.dumps({"weights": old_weights, "knobs": old_knobs,
                                  "thresholds": old_thresholds}),
                      json.dumps({"weights": new_weights, "knobs": new_knobs,
                                  "thresholds": new_thresholds}),
                      json.dumps(applied),
                      (analysis_en + (" | " + " | ".join(notes) if notes else ""))])
    print(f"  [apply] {len(applied)} change(s) written to tuning.json")


# --------------------------------------------------------------------------- #
# Code-patch phase: agent may inspect allowlisted files and propose
# small find/replace fixes; the runner clamps, scans, tests, and rolls back.
# --------------------------------------------------------------------------- #
CODE_SYS_PROMPT = (
    "You are the code-repair arm of the nightly gold_watcher reviewer. You "
    "may FIX code bugs that hurt accuracy or data quality. STRICT RULES:\n"
    "- You may only patch: predictor.py, technical.py, consensus.py,\n"
    "  news_reader.py, scraper.py, real_prices.py, llm.py.\n"
    "- Fixes must be SMALL and surgical: a broken selector, a wrong "
    "constant, a wrong formula, a parse bug, a guard. Never redesign, never "
    "reformat, never delete features.\n"
    "- 'find' must be copied EXACTLY from the shown file region (it must "
    "match exactly once). Keep the original indentation.\n"
    "- ADDED code must NOT: read environment variables, spawn processes, "
    "use eval/exec, encode payloads, or contact any host not already used "
    "by this repo. The runner rejects such patches automatically.\n"
    "- If the evidence is weak or the code looks correct, patch nothing - "
    "a wrong fix is worse than no fix."
)


def _repo_file(rel):
    return os.path.join(BASE_DIR, rel)


def _known_hosts():
    """Hosts already contacted somewhere in the repo's own python files."""
    hosts = set()
    for name in sorted(PATCH_FILES):
        try:
            with open(_repo_file(name), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        hosts.update(m.group(1).lower()
                     for m in re.finditer(r"https?://([A-Za-z0-9.-]+)", text))
    return hosts


def _outline(rel, cap=80):
    """Compact top-level outline (line numbers + defs/classes)."""
    try:
        with open(_repo_file(rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except Exception:  # noqa: BLE001
        return "  (outline unavailable)"
    lines = []
    for node in tree.body[:60]:
        kind = type(node).__name__
        name = getattr(node, "name", "?")
        lines.append(f"  L{getattr(node, 'lineno', 0):4d}: {kind} {name}")
    return "\n".join(lines[:cap])


def _region(rel, keyword, width=30, cap=210):
    """Numbered source lines around each case-insensitive keyword match."""
    try:
        with open(_repo_file(rel), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return "(file unreadable)"
    kw = (keyword or "").strip().lower()
    if not kw:
        return "(no keyword given)"
    hits = [i for i, ln in enumerate(lines) if kw in ln.lower()][:6]
    if not hits:
        return f"(keyword {keyword!r} not found in {rel})"
    windows, last_a, last_b = [], -1, -1
    for i in hits:
        a, b = max(0, i - width), min(len(lines), i + width + 1)
        if a <= last_b:
            windows[-1] = (last_a, b)
        else:
            windows.append((a, b))
        last_a, last_b = windows[-1]
    out, total = [], 0
    for a, b in windows:
        for j in range(a, b):
            if total >= cap:
                out.append("  ... (truncated)")
                return "\n".join(out)
            out.append(f"{j + 1:4d}| {lines[j].rstrip()}")
            total += 1
    return "\n".join(out)


def scan_patch_safety(added_text, known_hosts):
    """Violations in the ADDED lines of a patch (injection defenses)."""
    bad = []
    lowered = added_text.lower()
    for pat in ("os.environ", "getenv(", "subprocess", "eval(", "exec(",
                "__import__", "popen", "system(", "pickle", "base64",
                "socket", "chmod", "github_env", "telemetry", "webhook",
                "telegram_bot_token", "llm_api_key", "chat_id"):
        if pat in lowered:
            bad.append(f"forbidden token in added code: {pat!r}")
    for m in re.finditer(r"https?://([A-Za-z0-9.-]+)", added_text):
        host = m.group(1).lower()
        if host not in known_hosts:
            bad.append(f"added code contacts unknown host: {host}")
    return bad


def _block_check(block, known_hosts):
    """Validate one find/replace block. Returns (error, added_line_count)."""
    if not isinstance(block, dict):
        return "block is not an object", 0
    rel = str(block.get("file", "")).strip()
    if rel not in PATCH_FILES:
        return (f"file {rel!r} not in patch allowlist {sorted(PATCH_FILES)}",
                0)
    find, repl = block.get("find"), block.get("replace")
    if not isinstance(find, str) or not find.strip():
        return "missing 'find' text", 0
    if not isinstance(repl, str):
        return "missing 'replace' text", 0
    if (len(find.splitlines()) > PATCH_MAX_BLOCK_LINES
            or len(repl.splitlines()) > PATCH_MAX_BLOCK_LINES):
        return f"block too large (> {PATCH_MAX_BLOCK_LINES} lines)", 0
    bad = scan_patch_safety(repl, known_hosts)
    if bad:
        return "safety scan: " + "; ".join(bad[:3]), 0
    return "", len(repl.splitlines())


def apply_code_patch(blocks, known_hosts):
    """Apply find/replace blocks, then compile + import test.

    Returns (ok, detail, files_touched, added_lines).
    On ANY failure every file is restored byte-identical.
    """
    if not isinstance(blocks, list) or not blocks:
        return False, "no/empty patch list", [], 0
    if len(blocks) > PATCH_MAX_BLOCKS:
        return False, (f"too many blocks ({len(blocks)} > "
                       f"{PATCH_MAX_BLOCKS})"), [], 0
    added = 0
    for b in blocks:
        err, n = _block_check(b, known_hosts)
        if err:
            return False, err, [], 0
        added += n
    originals, patched = {}, {}
    for b in blocks:
        rel = b["file"]
        if rel not in originals:
            try:
                with open(_repo_file(rel), encoding="utf-8") as fh:
                    originals[rel] = fh.read()
            except OSError as err:
                return False, f"cannot read {rel}: {err}", [], 0
        find, repl = b["find"], b["replace"]
        n = originals[rel].count(find) if rel not in patched else \
            patched[rel].count(find)
        if n != 1:
            return False, (f"'find' text must match EXACTLY once in {rel} "
                           f"(matched {n})"), [], 0
        base = patched.get(rel, originals[rel])
        patched[rel] = base.replace(find, repl)
    for rel, text in patched.items():
        with open(_repo_file(rel), "w", encoding="utf-8") as fh:
            fh.write(text)
    try:
        for rel in sorted(patched):
            r = subprocess.run(
                [sys.executable, "-m", "py_compile", _repo_file(rel)],
                capture_output=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError("py_compile failed for "
                                   f"{rel}: {r.stderr.decode()[-300:]}")
        for rel in sorted(patched):
            mod = rel[:-3] if rel.endswith(".py") else rel
            r = subprocess.run(
                [sys.executable, "-c",
                 f"import importlib; importlib.import_module({mod!r})"],
                cwd=BASE_DIR, capture_output=True, timeout=90)
            if r.returncode != 0:
                raise RuntimeError(f"import test failed for {mod}: "
                                   f"{r.stderr.decode()[-300:]}")
    except Exception as err:  # noqa: BLE001
        for rel, text in originals.items():     # rollback, byte-identical
            with open(_repo_file(rel), "w", encoding="utf-8") as fh:
                fh.write(text)
        return False, f"validation failed -> rolled back: {err}", \
            sorted(patched), 0
    return True, "applied + compiled + imported OK", sorted(patched), added


def commit_code_patch(files, why):
    """Commit + push patched files. Returns (ok, detail, short_hash)."""
    msg = ("daily-agent: AI code patch - " + (why or "accuracy fix"))[:100]
    idcfg = ["-c", "user.name=github-actions[bot]",
             "-c", "user.email=41898282+github-actions[bot]@users.noreply"
                   ".github.com"]
    for _ in range(3):
        subprocess.run(["git", "pull", "--rebase", "--autostash",
                        "origin", "main"], cwd=BASE_DIR, capture_output=True)
        subprocess.run(["git", "add", *files], cwd=BASE_DIR,
                       capture_output=True)
        rc = subprocess.run(["git", *idcfg, "commit", "-m", msg],
                            cwd=BASE_DIR, capture_output=True)
        if rc.returncode != 0:
            return False, "nothing committed (" + \
                rc.stdout.decode()[-100:].strip() + ")", ""
        if subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR,
                          capture_output=True).returncode == 0:
            h = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                               cwd=BASE_DIR, capture_output=True)
            return True, "committed + pushed", h.stdout.decode().strip()
        time.sleep(8)
    return False, "push failed after retries (commit kept local)", ""


def log_patch(status, files, blocks, changed, commit, why, detail):
    os.makedirs(os.path.dirname(PATCH_LOG_PATH) or ".", exist_ok=True)
    exists = os.path.exists(PATCH_LOG_PATH)
    with open(PATCH_LOG_PATH, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if not exists:
            w.writerow(PATCH_LOG_HEADER)
        w.writerow([now_utc_str(), status, ",".join(files or []), blocks,
                    changed, commit, (why or "")[:300], (detail or "")[:300]])


def maybe_force_review(decisions):
    """FORCED TEST DRILL: inject a code-review request via environment.

    FORCE_CODE_REVIEW="file.py[,file2.py]" (must be in PATCH_FILES) and
    FORCE_FOCUS=<keyword> let you drill the whole code-patch pipeline from
    the Actions tab without waiting for a real bug. The agent still decides
    freely and every runner rail (allowlist, scan, exact-match, compile,
    import, rollback) still applies.
    """
    forced = os.environ.get("FORCE_CODE_REVIEW", "").strip()
    if not forced or not isinstance(decisions, dict):
        return decisions
    files = [f.strip() for f in forced.split(",")
             if f.strip() in PATCH_FILES]
    if not files:
        print(f"  [patch] FORCE_CODE_REVIEW={forced!r}: no allowlisted files")
        return decisions
    decisions["code_review"] = {
        "files": files,
        "focus": os.environ.get("FORCE_FOCUS", "").strip() or "def ",
        "why": "FORCED TEST DRILL (requested by the maintainer) - verify "
               "the code-patch pipeline end to end. Inspect the region and "
               "fix a real small issue if you can see one; a tiny guard or "
               "docstring is acceptable. If nothing warrants a change, "
               "return an empty patch. All runner rails still apply.",
    }
    print(f"  [patch] FORCED test drill on {files}")
    return decisions


def code_patch_section(decisions):
    """Multi-turn code review. Returns a summary dict for the report."""
    req = decisions.get("code_review") if isinstance(decisions, dict) else None
    if not req or not isinstance(req, dict):
        return None
    why = str(req.get("why", "")).strip()[:300]
    if not ALLOW_CODE_PATCH:
        return {"status": "disabled", "why_en": why,
                "detail": "code patching disabled via ALLOW_CODE_PATCH"}
    files = [f for f in (req.get("files") or [])
             if isinstance(f, str) and f in PATCH_FILES]
    if not files:
        return {"status": "rejected", "why_en": why,
                "detail": "no allowlisted files requested"}
    focus = str(req.get("focus", "")).strip() or files[0]
    rel = files[0]
    print(f"  [patch] agent requested code review: {files} focus={focus!r}")
    known = _known_hosts()
    messages = [
        {"role": "system", "content": CODE_SYS_PROMPT},
        {"role": "user", "content": (
            f"You flagged a possible code/data bug and asked to inspect:\n"
            f"files={files} focus={focus!r} evidence={why!r}\n\n"
            f"OUTLINE of {rel}:\n{_outline(rel)}\n\n"
            f"CODE around {focus!r} in {rel}:\n{_region(rel, focus)}\n\n"
            "Reply with ONLY JSON, one of:\n"
            '  {"more": {"around": "keyword"}}   <- ask for ONE more region\n'
            '  {"patch": [{"file": "...", "find": "<exact existing text>", '
            '"replace": "<new text>"}], "patch_why_en": "<=80 words", '
            '"patch_why_ar": "Arabic"}\n'
            '  {"patch": []}                     <- inspected, no fix '
            "justified")},
    ]
    reply = None
    for rnd in range(PATCH_MAX_ROUNDS):
        reply = call_agent_raw(messages, max_tokens=1400)
        if reply is None:
            break
        if (isinstance(reply.get("more"), dict)
                and rnd < PATCH_MAX_ROUNDS - 1):
            kw = str(reply["more"].get("around", "")).strip()
            messages.append({"role": "assistant",
                             "content": json.dumps(reply)})
            follow = ('Now reply with ONLY the final JSON '
                      '({"patch": [...]} or {"patch": []}).')
            messages.append({"role": "user",
                             "content": f"CODE around {kw!r} in {rel}:\n"
                                        f"{_region(rel, kw)}\n\n" + follow})
            continue
        break
    if not isinstance(reply, dict) or "patch" not in reply:
        log_patch("no-patch", [rel], 0, 0, "", why,
                  "agent inspection did not return a patch")
        return {"status": "no-patch", "files": [rel], "why_en": why,
                "detail": "agent inspection did not return a patch"}
    why_en = str(reply.get("patch_why_en", "")).strip()[:600]
    why_ar = str(reply.get("patch_why_ar", "")).strip()[:600]
    blocks = reply["patch"] if isinstance(reply["patch"], list) else []
    if not blocks:
        print("  [patch] agent inspected the code and found no bug to fix")
        log_patch("inspected-no-change", [rel], 0, 0, "", why_en, why)
        return {"status": "inspected-no-change", "files": [rel],
                "why_en": why_en or why, "why_ar": why_ar,
                "detail": "no code fix justified"}
    ok, detail, touched, changed = apply_code_patch(blocks, known)
    status = "applied" if ok else "rolled-back"
    commit = ""
    if ok:
        cok, cdet, commit = commit_code_patch(touched, why_en)
        detail += " | " + cdet
    log_patch(status, touched, len(blocks), changed, commit, why_en, detail)
    print(f"  [patch] {status}: {detail}")
    return {"status": status, "files": touched, "blocks": len(blocks),
            "changed": changed, "why_en": why_en, "why_ar": why_ar,
            "commit": commit, "detail": detail}


def _append_patch_text(text, res, lang):
    """Factual code-patch ground truth appended to the Telegram report."""
    why = res.get("why_ar") if lang == "ar" and res.get("why_ar") \
        else res.get("why_en", "")
    st = res.get("status")
    if st == "applied":
        sec = (f"\n\nCODE PATCH APPLIED\nFiles: {', '.join(res['files'])} "
               f"({res.get('blocks')} block(s), ~{res.get('changed')} lines)\n"
               f"Why: {why}\nValidated (compile + import) and committed as "
               f"{res.get('commit') or '(local commit)'} - takes effect on "
               f"the next hourly run. Revert: git revert {res.get('commit')}"
               if lang == "en" else
               f"\n\nتم تطبيق تعديل على الكود\nالملفات: "
               f"{', '.join(res['files'])} ({res.get('blocks')} مقطع، "
               f"~{res.get('changed')} سطر)\nالسبب: {why}\nتم التحقق (ترجمة "
               f"+ تشغيل) والحفظ باسم {res.get('commit')} - يسري من التشغيل "
               f"الساعي القادم. للتراجع: git revert {res.get('commit')}")
    elif st == "rolled-back":
        sec = (f"\n\nCODE PATCH ATTEMPTED - ROLLED BACK\n{res.get('detail')}"
               "\nNo code was changed."
               if lang == "en" else
               f"\n\nتمت محاولة تعديل الكود - وتم التراجع\n{res.get('detail')}"
               "\nلم يتغير أي كود.")
    elif st == "inspected-no-change":
        sec = (f"\n\nCODE REVIEWED - NO CHANGE\nWhy: {why}"
               if lang == "en" else
               f"\n\nتمت مراجعة الكود - بدون تعديل\nالسبب: {why}")
    else:
        label = {"disabled": "code patching disabled",
                 "rejected": "patch request rejected (file allowlist)",
                 "no-patch": "code inspection returned no patch",
                 "error": "code phase error"}.get(st, st)
        sec = (f"\n\nCODE PATCH: {label}\n{res.get('detail', '')}"
               if lang == "en" else
               f"\n\nتعديل الكود: {label}\n{res.get('detail', '')}")
    return (text + sec)[:3900]


# --------------------------------------------------------------------------- #
# Fallback report (agent unavailable) - pure digest, no decisions
# --------------------------------------------------------------------------- #
def fallback_report(digest):
    today = utcnow().strftime("%Y-%m-%d")
    en = [f"DAILY REVIEW - {today} (agent offline - digest only)"]
    ar = [f"المراجعة اليومية - {today} (الوكيل غير متاح - ملخص فقط)"]
    acc = digest.get("accuracy")
    if acc and acc.get("all"):
        a, r = acc["all"], acc.get("recent")
        en.append(f"\nAccuracy: strict {a['pct']}% ({a['hits']}/{a['n']})"
                  + (f" | last {r['n']}: {r['pct']}%" if r else ""))
        ar.append(f"\nالدقة: {a['pct']}% ({a['hits']}/{a['n']})"
                  + (f" | آخر {r['n']}: {r['pct']}%" if r else ""))
        if acc.get("dir"):
            d = acc["dir"]
            en.append(f"Direction UP/DOWN: {d['pct']}% ({d['hits']}/{d['n']})")
            ar.append(f"الاتجاه: {d['pct']}% ({d['hits']}/{d['n']})")
    g = digest.get("signals_last60")
    if g:
        en.append("\nSignals (agreement with real 24h move):")
        ar.append("\nالإشارات (التوافق مع الحركة الفعلية):")
        for name, st in g["groups"].items():
            pct = f"{st['pct']}%" if st["pct"] is not None else "n/a"
            en.append(f"- {name}: {pct} (n={st['n']})")
            ar.append(f"- {name}: {pct} (عدد={st['n']})")
    hlth = digest.get("health")
    if hlth:
        en.append(f"\nHealth: {hlth.get('price_rows')} price rows, last check "
                  f"{hlth.get('last_age_h')}h ago, grading {hlth.get('graded_n')} "
                  f"graded / {hlth.get('overdue_n')} overdue")
        ar.append(f"\nالحالة: {hlth.get('price_rows')} صف، آخر فحص قبل "
                  f"{hlth.get('last_age_h')} ساعة، التقييم "
                  f"{hlth.get('graded_n')} مكتمل / {hlth.get('overdue_n')} متأخر")
    en.append("\nNo changes applied today (agent unreachable - "
              "data logged for the next run).")
    ar.append("\nلا تعديلات اليوم (الوكيل غير متاح - تم تسجيل البيانات للغد).")

    def clip(lines):
        text = "\n".join(lines).strip()
        return text if len(text) <= 3900 else text[:3880] + " [...]"

    return clip(en), clip(ar)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Daily AI agent review")
    ap.add_argument("--digest", action="store_true",
                    help="print the digest JSON and exit (no agent, no send)")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the agent; send the template digest report")
    args = ap.parse_args()

    print(f"[daily-agent] start {now_utc_str()} UTC")

    # 0) grade pending predictions first so the digest sees the newest truth
    try:
        import predictor
        res = predictor.grade_pending()
        if res:
            print(f"  [grading] rolled accuracy now {res.get('pct')}% "
                  f"({res.get('hits')}/{res.get('n')})")
    except Exception as err:  # noqa: BLE001
        print(f"  [grading] skipped: {err}", file=sys.stderr)

    digest = build_digest()
    if args.digest:
        print(json.dumps(digest, indent=2, ensure_ascii=False))
        return 0

    # 1) the agent decides
    decisions = None
    if not args.no_llm:
        try:
            decisions = call_agent(digest)
            if decisions:
                print(f"  [agent] replied: "
                      f"{len(decisions.get('changes', []))} proposed change(s)")
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    applied, notes = [], []
    if decisions:
        try:
            weights, knobs, thresholds = _load_tuning()
            new_w, new_k, new_t, applied, notes = validate_changes(
                decisions, digest, weights, knobs, thresholds)
            if applied:
                apply_tuning(new_w, new_k, new_t, applied, decisions,
                             digest, notes)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            applied, notes = [], ["apply step error - nothing changed"]

    # 1b) optional code-patch phase (only if the agent requested it)
    patch_res = None
    if decisions and not args.no_llm:
        try:
            decisions = maybe_force_review(decisions)
            patch_res = code_patch_section(decisions)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            patch_res = {"status": "error",
                         "detail": "code phase crashed (nothing changed)"}

    # 2) the report (agent's own, else fallback)
    if decisions and all(str(decisions.get(k, "")).strip()
                         for k in ("report_en", "report_ar")):
        en_text = str(decisions["report_en"]).strip()[:3900]
        ar_text = str(decisions["report_ar"]).strip()[:3900]
    else:
        en_text, ar_text = fallback_report(digest)
    if patch_res:
        en_text = _append_patch_text(en_text, patch_res, "en")
        ar_text = _append_patch_text(ar_text, patch_res, "ar")
    if notes:
        en_text += "\n\nRunner notes: " + "; ".join(notes)
        ar_text += "\n\nملاحظات المنفذ: " + "; ".join(notes)
    print("\n" + "=" * 62 + "\n" + en_text + "\n" + "=" * 62)

    # 3) send through the project bot
    try:
        import predictor
        ok = predictor.send_telegram({"en": en_text, "ar": ar_text})
        print(f"  [telegram] {'delivered' if ok else 'no delivery (check secrets/recipients)'}")
    except Exception as err:  # noqa: BLE001
        print(f"  [telegram] send failed: {err}", file=sys.stderr)
    print(f"[daily-agent] done {now_utc_str()} UTC")
    return 0


if __name__ == "__main__":
    sys.exit(main())
