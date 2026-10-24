#!/usr/bin/env python3
"""
Self-service Telegram onboarding (v11.1).
=========================================
Lets anyone subscribe to the gold reports WITHOUT the maintainer touching
GitHub. Runs inside every hourly predictor run (fail-soft: any problem here
never blocks the gauge).

How it works
------------
Each hourly run calls poll_once():
  1. getUpdates picks up everything pending since the last run.
  2. Commands are handled:
       /start   -> save the chat + send the welcome + language buttons
       /en      -> reports in English
       /ar      -> reports in Arabic
       /stop    -> unsubscribe (row kept, active=0)
       /latest  -> the newest gauge reading, in their language
       any other text -> a short bilingual hint + the buttons
     The language buttons are inline keyboards (callback "lang:en"/"lang:ar").
  3. A final getUpdates(offset=last_id+1) confirms the batch so nothing is
     reprocessed next hour.

Subscribers live in data/subscribers.csv (committed by the hourly workflow
next to predictions.csv):
    chat_id,lang,name,joined_utc,active

predictor._recipients() merges the active rows with the TELEGRAM_CHAT_ID
env entries (env entries win on language conflicts), so reports, digests and
the daily agent's EN/AR message all reach subscribers automatically.

Notes
-----
- Never raise past poll_once()'s callers: everything is wrapped fail-soft.
- Do NOT set a webhook on this bot (BotFather or API): getUpdates and
  webhooks are mutually exclusive; a webhook would silently break this.
- A chat id in the repo file is harmless without the bot token, which stays
  in GitHub secrets.
"""
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SUBS_PATH = os.path.join(DATA_DIR, "subscribers.csv")
PRED_PATH = os.path.join(DATA_DIR, "predictions.csv")
SUBS_HEADER = ["chat_id", "lang", "name", "joined_utc", "active"]
LANGS = ("en", "ar")
MAX_SUBS = 200        # sanity cap on active self-service subscribers

WELCOME_KEYBOARD = {"inline_keyboard": [[
    {"text": "\U0001F1EC\U0001F1E7 English", "callback_data": "lang:en"},
    {"text": "\U0001F1EA\U0001F1EC \u0627\u0644\u0639\u0631\u0628\u064A\u0629",
     "callback_data": "lang:ar"}]]}

TXT = {
    "welcome_en": ("👋 Welcome to Gold Watcher!\n\n"
                   "Every hour I send the Egyptian gold gauge: a 0-100 meter "
                   "showing where the 21K price is heading next, plus alarms "
                   "when the gauge or the price moves hard.\n\n"
                   "Pick your language:"),
    "welcome_ar": ("👋 أهلًا بك في Gold Watcher!\n\n"
                   "أرسل لك مؤشر الذهب المصري كل ساعة: مقياس من 0 إلى 100 "
                   "يوضح اتجاه سعر عيار 21 التالي، مع تنبيهات عند تحرك "
                   "المؤشر أو السعر بقوة.\n\n"
                   "اختر لغتك:"),
    "picked_en": ("✅ Done! Reports will arrive in English - the next one "
                  "lands within the hour.\n\n/ar anytime for Arabic | "
                  "/stop to leave"),
    "picked_ar": ("✅ تم! ستصلك التقارير بالعربية - التقرير القادم خلال "
                  "ساعة كحد أقصى.\n\nاكتب /en للإنجليزية في أي وقت | "
                  "/stop لإلغاء الاشتراك"),
    "bye": ("👋 Unsubscribed - send /start anytime to return.\n\n"
            "👋 تم إلغاء الاشتراك - أرسل /start في أي وقت للعودة."),
    "hint": ("I only understand these commands:\n"
             "/start - subscribe\n"
             "/en - reports in English\n"
             "/ar - التقارير بالعربية\n"
             "/stop - unsubscribe\n"
             "/latest - latest gauge reading\n\n"
             "اختر لغتك / Pick your language:"),
    "full": ("Sorry - the subscriber list is full right now.\n\n"
             "عذرًا - قائمة المشتركين ممتلئة حاليًا."),
    "nodata_en": "No readings yet - the next report will have one.",
    "nodata_ar": "لا توجد قراءات بعد - التقرير القادم سيحتوي واحدة.",
}
DIR_AR = {"UP": "صاعد", "DOWN": "هابط", "FLAT": "مسطح"}
CONF_AR = {"strong": "قوي", "moderate": "متوسط", "low": "ضعيف"}


# --------------------------------------------------------------------------- #
# subscribers.csv
# --------------------------------------------------------------------------- #
def _load_subs():
    if not os.path.exists(SUBS_PATH):
        return []
    try:
        with open(SUBS_PATH, newline="", encoding="utf-8") as fh:
            return [r for r in csv.DictReader(fh) if r.get("chat_id")]
    except OSError:
        return []


def _save_subs(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SUBS_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUBS_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in SUBS_HEADER})


def _clean_name(name):
    return re.sub(r"[,\r\n\t]+", " ", str(name or "")).strip()[:40]


def _upsert(chat_id, lang=None, name="", active=1):
    """Add or update one subscriber row. Returns False when the list is full."""
    rows = _load_subs()
    for r in rows:
        if r.get("chat_id") == chat_id:
            if lang:
                r["lang"] = lang
            if name:
                r["name"] = _clean_name(name)
            r["active"] = str(active)
            _save_subs(rows)
            return True
    if sum(1 for r in rows if r.get("active") == "1") >= MAX_SUBS:
        return False
    rows.append({"chat_id": chat_id,
                 "lang": lang or "en",
                 "name": _clean_name(name),
                 "joined_utc": datetime.now(timezone.utc)
                 .strftime("%Y-%m-%d %H:%M:%S"),
                 "active": str(active)})
    _save_subs(rows)
    return True


def merge_recipients(env_list):
    """env entries first, then active subscribers not already present.

    env_list entries win on chat-id conflicts (the maintainer's explicit
    config beats the file).
    """
    out = list(env_list or [])
    seen = {cid for cid, _ in out}
    for r in _load_subs():
        cid = (r.get("chat_id") or "").strip()
        if not cid or cid in seen or (r.get("active") or "").strip() != "1":
            continue
        lang = (r.get("lang") or "en").strip().lower()
        out.append((cid, lang if lang in ("en", "ar", "both") else "en"))
        seen.add(cid)
    return out


# --------------------------------------------------------------------------- #
# Telegram plumbing
# --------------------------------------------------------------------------- #
def _api(token, method, payload=None, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if payload is not None:
        resp = requests.post(url, json=payload, timeout=15)
    else:
        resp = requests.get(url, params=params or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _send(token, chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text,
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = keyboard
    _api(token, "sendMessage", payload=payload)
    return True


# --------------------------------------------------------------------------- #
# /latest: newest gauge reading straight from predictions.csv
# --------------------------------------------------------------------------- #
def _latest_line(lang):
    try:
        with open(PRED_PATH, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("final_pct")]
        if not rows:
            return TXT["nodata_ar" if lang == "ar" else "nodata_en"]
        r = rows[-1]
        pct = float(r["final_pct"])
        direction = (r.get("direction") or "?").strip()
        conf = (r.get("confidence") or "low").strip().lower()
        when = (r.get("timestamp_cairo") or "")[:16]
        price = (r.get("price_21k") or "").strip()
        try:
            price_txt = f" | 21K: {float(price):,.0f} EGP"
        except ValueError:
            price_txt = ""
        if lang == "ar":
            return (f"آخر قراءة ({when} القاهرة): {pct:.0f}% - "
                    f"{DIR_AR.get(direction, direction)} "
                    f"({CONF_AR.get(conf, conf)}){price_txt}")
        return (f"Latest gauge ({when} Cairo): {pct:.0f}% - {direction} "
                f"({conf}){price_txt}")
    except Exception:  # noqa: BLE001 - cosmetic command, never fail loudly
        return TXT["nodata_ar" if lang == "ar" else "nodata_en"]


# --------------------------------------------------------------------------- #
# Update handling
# --------------------------------------------------------------------------- #
def _sub_lang(chat_id, default="en"):
    for r in _load_subs():
        if r.get("chat_id") == chat_id:
            lang = (r.get("lang") or "").strip().lower()
            return lang if lang in ("en", "ar") else default
    return default


def handle_update(update, token):
    """One Telegram update -> state change + reply. Returns True if handled."""
    cb = update.get("callback_query")
    if cb:
        data = (cb.get("data") or "").strip()
        chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
        if not chat_id:
            return False
        if data in ("lang:en", "lang:ar"):
            lang = data.split(":", 1)[1]
            if not _upsert(chat_id, lang=lang,
                           name=(cb.get("from") or {}).get("first_name", "")):
                _send(token, chat_id, TXT["full"])
                return True
            _api(token, "answerCallbackQuery",
                 payload={"callback_query_id": cb.get("id"),
                          "text": "✅ English" if lang == "en" else "✅ تم"})
            _send(token, chat_id, TXT[f"picked_{lang}"])
            return True
        return False

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return False
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not chat_id:
        return False
    frm = msg.get("from") or {}
    name = frm.get("first_name", "")
    text = (msg.get("text") or "").strip().lower()

    if text.startswith("/start"):
        cur = _sub_lang(chat_id)
        if not _upsert(chat_id, lang=cur, name=name, active=1):
            _send(token, chat_id, TXT["full"])
            return True
        _send(token, chat_id, TXT["welcome_en"], WELCOME_KEYBOARD)
        return True
    if text.startswith("/en"):
        if not _upsert(chat_id, lang="en", name=name, active=1):
            _send(token, chat_id, TXT["full"])
            return True
        _send(token, chat_id, TXT["picked_en"])
        return True
    if text.startswith("/ar"):
        if not _upsert(chat_id, lang="ar", name=name, active=1):
            _send(token, chat_id, TXT["full"])
            return True
        _send(token, chat_id, TXT["picked_ar"])
        return True
    if text.startswith("/stop"):
        _upsert(chat_id, name=name, active=0)
        _send(token, chat_id, TXT["bye"])
        return True
    if text.startswith("/latest"):
        _send(token, chat_id, _latest_line(_sub_lang(chat_id)))
        return True
    _send(token, chat_id, TXT["hint"], WELCOME_KEYBOARD)
    return True


def poll_once():
    """Process pending updates. Returns how many were handled. Fail-soft."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("  [bot] TELEGRAM_BOT_TOKEN not set -> onboarding skipped")
        return 0
    updates = _api(token, "getUpdates", params={
        "timeout": 0,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }).get("result", [])
    handled = 0
    for u in updates:
        try:
            if handle_update(u, token):
                handled += 1
        except Exception as err:  # noqa: BLE001 - one bad update != stop all
            print(f"  [bot] update {u.get('update_id')} failed: {err}",
                  file=sys.stderr)
    if updates:  # confirm the batch so it is not reprocessed next hour
        try:
            _api(token, "getUpdates", params={
                "offset": updates[-1]["update_id"] + 1, "timeout": 0})
        except Exception as err:  # noqa: BLE001
            print(f"  [bot] confirm call failed (harmless): {err}",
                  file=sys.stderr)
    return handled
