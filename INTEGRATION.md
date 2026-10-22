# Integration Guide — Prediction Gauge for gold_watcher

Version: **v11** (2026-10-22) — adds the **SELL WINDOW milestone** (price spike
above the day's average = local chance to sell), **fresh anti-twitch alarms**,
the **alarm report card**, **dual accuracy**, an **hourly native schedule**,
**blank-line message structure**, and **multi-recipient Telegram with an
Arabic language option**.

Time needed: ~5 minutes. All files in this folder go into your repo root
(the same level as `scraper.py`).

---

## Step 1 — Copy the files

```
technical.py       -> repo root
consensus.py       -> repo root
llm.py             -> repo root
news_reader.py     -> repo root
local_premium.py   -> repo root   (required - predictor imports it)
real_prices.py     -> repo root   (your souq-report tool)
predictor.py       -> repo root
workflow-gold-watcher.yml -> .github/workflows/  (REPLACE the content of your
                             existing workflow file - in your repo it is
                             named workflow-gold-watcher.yml; keep that name)
```

## Step 2 — requirements.txt

A ready `requirements.txt` is included in this folder — copy it to the repo
root (or, if your repo already has one, make sure it contains exactly):

```
requests>=2.31.0
beautifulsoup4>=4.12.0
openai>=1.35.0
```

## Step 3 — Secrets

You already did this: repo secret `LLM_API_KEY` = your Groq key.
Nothing else needed. The gauge runs fine even if the key is missing —
it just skips the news group.

## Step 4 — Push and watch

1. Commit + push everything.
2. Go to the Actions tab → "Egypt Gold Watcher" → Run workflow (manual test).
3. You should see the step "Run prediction gauge" print something like:

```
Gold gauge: 46% FLAT (moderate)
Change: first logged run - baseline saved
- technical: +0.12 (gold_21k_sell: 6,295 | RSI(14) 66.7 | ...)
- consensus: -0.35 (TradingView XAU/USD: overall -0.35 (sell), ..., spot 4,145.0, 7d -1.06%)
- news: +0.31 (6/6 articles read -> score +0.31, 3 verified levels)
- local-vs-global (7d): 21K +1.86% | XAU/USD -1.06% | spread +2.92 pts
- parallel-dollar: site 53.76 vs official 53.52 -> gap +0.44%, 7d ago +0.31% -> widening
- real souq premium: +0.87% vs site reference (your reports, 7d window)
Levels (7d): 6,500 gram resistance x5 (+3.3% from now) | ...
Gauge accuracy (28 graded calls): 61%
Bias gauge, not financial advice.
```

## What lands in Telegram, and when

The gauge does NOT message every run (that would be noise). It sends only when:
- the **gauge ladder** trips: the gauge climbed (or fell) onto a rung it has
  not rung today - 58 / 65 / 72 / 80 / 90 going up, 42 / 35 / 28 / 20 / 10
  going down - and the message says what 21K move that height has meant, or
- the **SELL WINDOW milestone** trips: the 21K price rises ~0.5% above the
  day's average price (see its section below), or
- it's the **first run of the Cairo day** -> the **morning report** = daily
  digest, PLUS the weekly digest on Saturdays (env `DIGEST_WEEKDAY`, 0=Mon..6=Sun)
  and the monthly digest on the 1st of each month - all inside ONE message, or
- the **direction flips** (UP <-> DOWN <-> FLAT), or
- the gauge moves **>= 15 points** since the last run, or
- you pass `--force` (add it to the workflow run command temporarily).

Every run is still logged to `data/predictions.csv` regardless of messaging.
The message is now built in SEPARATE SECTIONS with one blank line between
each subject (alarms -> report card -> digests -> Right now -> signals ->
premium -> levels/accuracy), so it is much easier to read.

## The morning report (daily + weekly + monthly digests)

The first run after midnight Cairo prepends digests of PAST performance on top
of the current gauge. Every digest is built from predictions.csv only:

```
Daily digest - 2026-10-19 (11 runs)
- gauge: open 44% -> close 52% (low 41, high 57)
- direction: FLAT 02:00 -> UP 14:00 -> FLAT 18:00
- 21K: 6,270 -> 6,295 EGP (+25, +0.40%)
- XAU/USD: 4,143.26 -> 4,150.10 (+6.84, +0.17%)
- local-vs-global: +2.65 -> +3.06 pts (max +3.06)
- parallel gap: +0.53% -> +0.51%
- grading: 6/8 calls correct

Weekly digest - 2026-10-13 -> 2026-10-19 (63 runs)      <- Saturdays only
- gauge: open 44% -> close 52% (low 38, high 64)
- direction: DOWN 10:00 -> ... -> FLAT 22:00 (5 changes)
- 21K: 6,190 -> 6,295 EGP (+105, +1.70%)
- biggest 21K day move: +0.85% on 2026-10-15
- grading: 21/34 calls correct

Monthly digest - September 2026 (260 runs)               <- 1st of month only
- gauge: open 47% -> close 52% (low 33, high 71)
- direction: FLAT 02:00 -> ... -> UP 19:00 (11 changes)
- 21K: 6,050 -> 6,295 EGP (+245, +4.05%)
- biggest 21K day move: +1.94% on 2026-09-23
- grading: 96/180 calls correct

Right now:
Gold gauge: 52% FLAT (moderate)
... (the normal gauge snapshot follows)
```

Read it stupid-simple: gauge line = where the mood started, ended and peaked;
direction = the period's path (more hops = choppier period); 21K/XAU = what
Cairo and the world actually did; local-vs-global > 1.5 pts = local-premium
episodes; parallel gap = the hidden dollar; biggest day move = the wildest
single day; grading = how past calls scored against reality.

## The gauge ladder (how high the gauge is, in EGP)

You read the gauge like a price-o-meter: every height means a (nearly)
specific 21K move. The ladder has 5 rungs up and 5 down; when the gauge
reaches a height it has NOT touched today, the very next run sends:

```
GAUGE ALARM: gauge just climbed to 72% (2 new levels today)
Levels like this have meant: 21K moves about +82 EGP (+1.3%) - usually lands
within about a day, sometimes within hours [rough estimate]
Pace check: the last 24h 21K moved +40 EGP (+0.63%) - ahead of the usual pace
for this height

Right now:
Gold gauge: 72% UP (moderate)
... (the normal gauge snapshot follows)
```

The ladder (estimates at a 6,300 EGP 21K price):

| Gauge height | Next-day 21K move it stands for | ~ EGP on 21K |
|---|---|---|
| 90% | about +2.5% | ~ +158 |
| 80% | about +1.8% | ~ +113 |
| 72% | about +1.3% | ~ +82 |
| 65% | about +0.9% | ~ +57 |
| 58% (UP line) | about +0.5% | ~ +32 |
| 42% (DOWN line) | about -0.5% | ~ -32 |
| 35% | about -0.9% | ~ -57 |
| 28% | about -1.3% | ~ -82 |
| 20% | about -1.8% | ~ -113 |
| 10% | about -2.5% | ~ -158 |

Stupid-simple rules it follows:
- FRESH: the alarm describes the run it fires in - "just climbed" means THIS
  run, not the previous one (the old one-run lag is gone)
- ANTI-TWITCH: a crossing rings instantly only when it clears the rung by 2
  points (`LADDER_CONFIRM`); a weaker crossing rings once the gauge HOLDS the
  rung for two runs in a row. Strong jumps are never delayed, fake spikes
  (60% -> 53% an hour later) never ring
- each rung rings once per Cairo day (climb 58 -> 72 in one step = one
  message about the highest rung; a re-climb tomorrow rings again)
- a FALL rings exactly like a climb, with minus numbers
- the "usually lands within about a day, sometimes within hours" wording is
  the honest window: the move is an average, not a schedule
- the [rough estimate] tag means the ladder is using its by-design numbers
  (built from the gauge's own momentum math: 24h moves of +-1%, 7-day of
  +-3%). Once your log has 3+ runs at a height, the tag switches to
  [measured from N runs in your log] and the estimate is replaced by what
  YOUR market actually did after the gauge sat at that height
- the Pace check line compares the last-24h REAL move with the usual pace for
  that height: ahead of / in line with / against - so a fast market is visible
  in the same message

Want different heights? Edit `LADDER_UP` / `LADDER_DOWN` at the top of
`predictor.py` (two one-line tuples).

## The SELL WINDOW milestone (price spike above the day's average)

The ladder watches the GAUGE; the sell window watches the PRICE itself.
Egyptian souq spikes are often short - a chance to sell before the price
falls back. The milestone rings when 21K rises about **+0.5%** above the
day's average price:

```
SELL WINDOW: 21K 6,360 is +39 EGP (+0.62%) above today's average (6,321)
Local spikes like this are often short-lived - if you were planning to sell,
this is the kind of moment.
New 7-day high.
```

Stupid-simple rules:
- baseline = today's average of the day's runs, once 3 runs exist today
  (first ~3 hours at the hourly cadence). Before that it uses YESTERDAY's
  average with outlier prices trimmed (anything >2% away from the median is
  dropped, so one weird quote cannot poison the baseline)
- rings once per Cairo day; a bigger spike later does not re-ring (no spam)
- `New 7-day high.` is appended when the price is the highest of the week
- the report card grades sell windows too: `Sell windows: 2 of 3 would have
  beat waiting 24h` (beat = the price was lower 24h later, so selling at the
  flag was the better deal)
- knobs: `MILESTONE_PCT` (default 0.5), `MILESTONE_EGP` (absolute EGP line,
  off by default), `MILESTONE_MIN_RUNS` (default 3)

## The alarm report card (does the system keep its promises?)

Every alarm (ladder + sell window) is logged to `data/alarms.csv` with the
move it promised. 21-36h later the run grades it against the real price:
- ladder alarm: **hit** = the real move reached at least HALF the promised
  move, same direction
- sell window: **beat** = selling at the flag beat holding for 24h
The message then shows `Alarm score: 3 of 5 recent alarms on target` -
you can watch the system earn (or lose) trust with your own eyes.

## Two accuracy numbers (so the strict one does not scare you)

The message now shows: `Accuracy: direction 62% (9 UP/DOWN calls) | strict
39% (18 graded calls)`. The **direction** score answers "when the gauge said
UP/DOWN, did the price go that way at all?". The **strict** score is the old
harsh grade (right direction AND landing window). Same data, two lenses.

## Who receives the message (add a friend + Arabic)

The bot can send to any number of people, each in their own language.
`TELEGRAM_CHAT_ID` now accepts a comma list where each entry is `chat_id`
or `chat_id:lang` (lang = `en`, `ar`, or `both`):

```
TELEGRAM_CHAT_ID = "111222333:ar, 444555666"
```

- person 111 gets the message in ARABIC, person 444 in English
- `444555666:both` would send both languages to one person
- entries without a tag follow `TELEGRAM_LANG` (default `en`)
- to add someone: they open your bot and press START, then find their chat id
  (send @userinfobot any message and it replies with their Id), then add
  `,<their_id>:ar` to the secret value - no code, no workflow change
- your existing single chat id keeps working exactly as before

The Arabic version is a full translation of the message structure (gauge,
alarms, sell window, digests, levels, accuracy, disclaimer). Indicator
details (SMA/RSI numbers, article counts) stay in English on purpose -
they are universal notation.

## The self-grading loop (how the % earns trust)

- Every prediction row stores `price_21k` at call time.
- 24h later, later runs grade it: actual move vs predicted direction ->
  `predicted_correct` 1/0.
- Telegram messages include rolling accuracy over the last 30 graded calls.
- After ~2-4 weeks you'll know the gauge's REAL hit rate. If it's ~50%,
  the weights need tuning or the news volume needs increasing.

## Where the news comes from (and why "+0.00" is normal sometimes)

`news_reader.py` reads 5 free RSS feeds, round-robin so every run mixes sources:

| Feed | What it gives the model |
|---|---|
| Yahoo gold futures (`GC=F`) | full article text + real prices |
| Google News EN | global headline + source only (wrapper links, bodies not fetchable) |
| Google News AR — **Egypt query** (أسعار الذهب في مصر) | Egyptian local market: EGP prices, merchant/supply stories |
| ForexLive | full article text (macro/FX context) |
| Investing.com commodities | headlines only (site blocks scraping) |

So the news group is deliberately **global + Egyptian mixed** — the LLM is
told that Egyptian local dynamics (hoarding, parallel-market dollar) can move
EGP prices opposite to global gold, and to report the LOCAL direction when
the article is a local one.

Each article's extracted prices are **verified against the text before use**
(hallucinated numbers are dropped), so `0 verified levels` simply means the
articles contained no usable price figures. If all articles are neutral you'll
see `score +0.00` — that is a legitimate "news has no direction today" signal,
and the gauge still works from technical + consensus. Expect more non-neutral
runs now that full-text feeds (Yahoo/ForexLive) are in the mix.

## The local-vs-global spread (your hoarding observation, measured)

Egyptian gold = global XAU/USD x USD/EGP x a **local premium**. When merchants
hoard stock (or EGP weakens), local 21K moves OPPOSITE to global gold — global
signals alone would then be wrong for what you actually pay.

Every run now measures this: 21K 7-day change vs XAU/USD 7-day change.

- It appears in the gauge message as `- local-vs-global (7d): ... spread ...`
- Every row of `data/predictions.csv` logs `global_xauusd`, `local_chg_7d`,
  `global_chg_7d`, `divergence_7d`
- The gauge itself already prices this in through the technical group (it
  reads LOCAL 21K EGP prices — the largest single weight)
- After ~2 weeks of logged data we can calibrate a dedicated local-premium
  weight if the spread proves persistent

### What the 5-year weight study found (2026-10)

Reconstructing the local price from your own CSV revealed the mechanics:
`21K = global ounce x parallel-market USD/EGP` — the site computes it
(verified mean arithmetic error 0.005%). Your `usd_egp` column is therefore
a **parallel-dollar series that no global feed provides**. The 7d spread you
saw (+2.19% local vs -1.14% global) was exactly this: parallel EGP +2.11%,
global gold -0.16%.

Backtests (5y daily fair-value n=1,104 + real 43d replay, see the
weight-study chart):

- On days when local and global signals DISAGREE, local momentum predicted
  the next-day LOCAL move better in EVERY window tested: 57% vs 53%
  (5y subset, n=72), 55-58% vs 47% (2y subset), 63% vs 41% (real replay,
  small n)
- Variance driver of local daily moves: gold 94% in the calm 2024-26
  window, but USD/EGP 63% across the full 5y — the 2022-24 devaluation
  era. And official-rate data understates even that: the real market
  priced the parallel dollar (gap peaked near +90% before the 2024 float),
  which only your CSV records
- EGP 1-week momentum was the single best next-day predictor (53.1%) —
  devaluations persist; your technical group already embeds it via local
  prices
- Net: local deserves the top slot, global stays as the long-horizon
  anchor. Default weights moved 40/30/30 -> **50/20/30**
  (technical/consensus/news)

Override without touching code (repo variables or workflow env):

```yaml
env:
  WEIGHT_TECHNICAL: 0.50   # local 21K composite
  WEIGHT_CONSENSUS: 0.20   # TradingView XAU/USD crowd
  WEIGHT_NEWS: 0.30        # LLM news reading
```

## The unknown local variable (measured, not guessed)

The source site COMPUTES its prices: `21K = global ounce x its USD/EGP rate`
(mean arithmetic error 0.005%). Whatever the market bakes into that rate —
parallel dollar, making charges (مصنعية), scarcity premiums, coin stress — is
invisible inside the site's own numbers. You said there is a mover we cannot
name; the gauge now measures its FOOTPRINT three ways every run:

1. **Parallel-dollar gap** — site rate vs the OFFICIAL rate (open.er-api.com,
   free). Widening gap = local stress building. Logged per run; the message
   shows the 7-day trend.
2. **Real souq premium** — when you physically buy/check gold, log it:

   ```bash
   python real_prices.py --21k 6350 --note "downtown souq"
   python real_prices.py --show        # review history
   ```

   The predictor matches each report to the site quote at the same time and
   prints the mean premium. YOUR measurements are the only source of
   bazaar reality the site cannot show.
3. **Formula-break alarm** — if 21K stops matching ounce x rate (|dev| >
   0.5%), the site itself started embedding real premiums directly: an
   early-warning that the market regime changed. Logged every run.

New columns in `predictions.csv`: `usdegp_official`, `parallel_gap_pct`,
`real_premium_pct`, `formula_dev_pct`. After a few weeks of these, the
recalibration can answer WHAT the unknown variable is (gap-driven vs
premium-driven) — and the `WEIGHT_*` env knobs adjust without code.

## Tuning knobs (env vars, optional)

| Env var | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | groq | any OpenAI-compatible endpoint (Z.ai: `https://api.z.ai/api/paas/v4`) |
| `LLM_MODEL` | `openai/gpt-oss-20b` | try `openai/gpt-oss-120b` or Z.ai GLM for stronger Arabic reading |
| `LLM_TIMEOUT` | 25 | seconds per LLM call |
| `LLM_MIN_INTERVAL` | 10 | min seconds between LLM calls — keeps Groq's free tier (8,000 tokens/MINUTE) happy; 6 articles ≈ +1 min runtime |
| `WEIGHT_TECHNICAL` | 0.50 | local 21K technical group weight (was 0.40) |
| `WEIGHT_CONSENSUS` | 0.20 | TradingView consensus weight (was 0.30) |
| `WEIGHT_NEWS` | 0.30 | news group weight (unchanged) |
| (code) `NOTIFY_DELTA` | 15 | points of change that trigger a message |
| (code) `LADDER_UP` / `LADDER_DOWN` | 58/65/72/80/90, 42/35/28/20/10 | gauge heights that ring; edit in predictor.py |
| `LADDER_CONFIRM` | 2.0 | pts a crossing must clear to ring instantly (anti-twitch); 0 = old always-instant behaviour |
| `MILESTONE_PCT` | 0.5 | SELL WINDOW: % above the day's average price that rings |
| `MILESTONE_EGP` | 0 (off) | SELL WINDOW: absolute EGP line above the baseline |
| `MILESTONE_MIN_RUNS` | 3 | runs needed today before the baseline switches to today's average |
| `TELEGRAM_LANG` | en | default language for chat ids without a `:lang` tag (en / ar / both) |
| (code) `ALARM_HIT_FRACTION` | 0.5 | fraction of the promised move that counts as an alarm "hit" |

## If you actually meant xAI "Grok" (not Groq)

One env var change: `LLM_BASE_URL: https://api.x.ai/v1`, model e.g. `grok-3-mini`.
Note: xAI has no free tier; Groq does. Everything else is identical.

## Local testing (without Telegram / without LLM)

```bash
python technical.py --csv data/gold_prices.csv      # needs only the CSV
python consensus.py                                  # live TradingView call
python news_reader.py --max-articles 2               # degrades without key
python predictor.py --dry-run                        # full gauge, writes nothing
```

Seeing `429 rate_limit_exceeded` in the logs? That is Groq's 8k-tokens/minute
cap. The reader now spaces calls ~10s apart and retries exactly when Groq
says to, so it should be rare; if you still see it, raise `LLM_MIN_INTERVAL`
to `15`.

## Data files added by the gauge (auto-committed by the workflow)

- `data/predictions.csv` — every run + grading + divergence + parallel-dollar
  gap + real souq premium columns
- `data/levels_history.csv` — every verified analyst price level
- `data/alarms.csv` — every ladder alarm and sell window, with the later
  report-card verdict (this is what feeds `Alarm score:`)
- `data/real_prices.csv` — your logged souq prices (via `real_prices.py`)

All are plain CSV; delete them anytime to reset the gauge's memory.

## Schedule: now hourly, powered by GitHub itself

The bundled workflow adds `schedule: cron '15 * * * *'` - GitHub runs the
gauge every hour at :15 UTC with NO outside service. After you push it:

1. **turn OFF your external cron service** (the one that pings
   workflow_dispatch every 2h) - otherwise you double the runs
2. check the Actions tab: you should see runs appearing at :15 every hour
   (GitHub can delay scheduled runs by a few minutes - normal)
3. one manual `Run workflow` click is still possible anytime

With hourly runs the morning report arrives closer to midnight Cairo, the
ladder and milestones react up to an hour faster, and the rough estimates
turn into measured ones about twice as fast.
