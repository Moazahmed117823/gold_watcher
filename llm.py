#!/usr/bin/env python3
"""
Provider-agnostic LLM adapter (OpenAI protocol).
================================================
One file knows which provider is in use; every caller just gets
read_article() -> dict | None.

Configured entirely via environment variables:
    LLM_BASE_URL   e.g. https://api.groq.com/openai/v1        (Groq, free)
                       https://api.z.ai/api/paas/v4           (Z.ai GLM, free tier)
                       https://openrouter.ai/api/v1           (OpenRouter)
    LLM_API_KEY    the secret (repo secret LLM_API_KEY)
    LLM_MODEL      e.g. openai/gpt-oss-20b / llama-3.3-70b-versatile / glm-4.7-flash
    LLM_TIMEOUT    optional, seconds (default 25)
    LLM_MIN_INTERVAL  optional, min seconds between requests (default 10).
                      Groq free tier allows only 8,000 tokens/MINUTE and one
                      article costs ~1.3k tokens -> calls must be spaced out.

If any variable is missing, or the openai package is not installed, the
adapter reports unavailable and callers degrade gracefully (gauge works
without the news group).
"""
import json
import os
import re
import sys
import time

SYSTEM_PROMPT = (
    "You extract market-direction information from financial news about GOLD. "
    "Respond with ONLY a JSON object, no markdown, no extra text, using exactly this schema:\n"
    '{"bias": "up"|"down"|"neutral", '
    '"confidence": <integer 0-100>, '
    '"levels": [{"price": <number>, "unit": "USD/ounce"|"EGP/gram"|"USD/gram"|"EGP/coin", '
    '"type": "resistance"|"support"|"target"}], '
    '"summary": "<= 12 words"}\n'
    "Rules:\n"
    "- bias is what the TEXT argues for gold's near-term direction, not your opinion.\n"
    "- The text may be a headline only. Headlines can carry direction (e.g. 'gold "
    "rallies', 'XAU/USD plunges', 'eyes a rebound', Arabic 'يرتفع'/'يتراجع'): if the "
    "headline states a direction, use it with confidence<=60. If it only reports past "
    "prices with no direction, bias=neutral.\n"
    "- Only include levels whose NUMBERS literally appear in the text. If none, levels=[].\n"
    "- Choose the unit that matches the number's context in the text. When unsure, omit.\n"
    "- If the text is not about gold markets, or gives no direction: bias=neutral, confidence<=20.\n"
    "- For EGYPTIAN local-market articles (Egypt, EGP prices, merchants, hoarding, "
    "supply): local market dynamics and the parallel-market dollar can push local EGP "
    "prices OPPOSITE to global gold. Report the direction the text argues for LOCAL "
    "EGP prices, and include EGP/gram or EGP/coin levels whenever those numbers appear.\n"
    "- If unsure about anything, prefer neutral. Never invent numbers."
)


def _cfg():
    return {
        "base_url": os.environ.get("LLM_BASE_URL", "").strip(),
        "api_key": os.environ.get("LLM_API_KEY", "").strip(),
        "model": os.environ.get("LLM_MODEL", "").strip(),
        "timeout": float(os.environ.get("LLM_TIMEOUT", "25")),
    }


_last_call_ts = 0.0


def _throttle():
    """Space out requests so free-tier TPM limits (Groq: 8k tokens/min) hold."""
    global _last_call_ts
    interval = float(os.environ.get("LLM_MIN_INTERVAL", "10"))
    wait = interval - (time.time() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


def llm_available():
    cfg = _cfg()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        return False
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def _extract_json(text):
    """Robust JSON extraction: direct parse, then first {...} block."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def read_article(title, body, retries=2):
    """Ask the LLM to extract {bias, confidence, levels, summary} from a text.

    Returns dict on success, None on any failure (never raises).
    """
    cfg = _cfg()
    if not llm_available():
        return None
    from openai import OpenAI  # imported lazily; llm_available() already checked

    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"],
                    timeout=cfg["timeout"], max_retries=0)
    content = f"{title}\n\n{body[:3000]}"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            _throttle()
            resp = client.chat.completions.create(
                model=cfg["model"],
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": content}],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            parsed = _extract_json(resp.choices[0].message.content)
            if parsed is None:
                raise ValueError("could not parse JSON from LLM reply")
            return parsed
        except Exception as err:  # noqa: BLE001 - degrade, never crash the gauge
            last_err = err
            print(f"  [llm] attempt {attempt}/{retries} failed: {err}", file=sys.stderr)
            if attempt < retries:
                # 429 replies carry "Please try again in X s" -- honour exactly
                # that (capped), otherwise a short generic pause.
                hint = re.search(r"try again in ([\d.]+)s", str(err))
                time.sleep(min(float(hint.group(1)) + 0.5, 20.0) if hint else 2.0)
    # One retry without response_format (some providers reject the field)
    try:
        _throttle()
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": content}],
            temperature=0.1,
        )
        return _extract_json(resp.choices[0].message.content)
    except Exception as err:  # noqa: BLE001
        print(f"  [llm] giving up: {err} (last structured error: {last_err})",
              file=sys.stderr)
        return None


if __name__ == "__main__":
    print(f"[llm] available: {llm_available()}  cfg: "
          f"{ {k: (v if k not in ('api_key',) else '***') for k, v in _cfg().items()} }")
