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
  4. APPLY    - accepted decisions go to tuning.json (predictor.py
                reads it; env vars still override) and to
                data/tuning_log.csv. Everything lands as a git commit.
  5. REPORT   - the agent's own Telegram message is sent through the
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
import csv
import json
import os
import re
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
    ' "report_ar": "Arabic version of report_en, max 1800 chars"\n'
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


def call_agent(digest, retries=2):
    """Ask the LLM agent to review the digest. Returns dict | None."""
    cfg = _llm_cfg()
    if not (cfg["base"] and cfg["key"] and cfg["model"]):
        print("  [agent] LLM_* not configured -> report-only fallback")
        return None
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": SYS_PROMPT},
                     {"role": "user",
                      "content": "DATA DIGEST:\n" + json.dumps(
                          digest, ensure_ascii=False)}],
        "temperature": 0.2,
        "max_tokens": 1800,
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

    # 2) the report (agent's own, else fallback)
    if decisions and all(str(decisions.get(k, "")).strip()
                         for k in ("report_en", "report_ar")):
        en_text = str(decisions["report_en"]).strip()[:3900]
        ar_text = str(decisions["report_ar"]).strip()[:3900]
    else:
        en_text, ar_text = fallback_report(digest)
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
