# Egypt Gold Watcher

A zero-server, zero-cost gold price watcher that checks **live Egyptian gold prices every 30 minutes**, keeps a full price history in CSV, and sends you a **Telegram alert only when a price changes**. It runs entirely on **GitHub Actions** free tier.

## What you get

| Karat | Tracked price |
|-------|---------------|
| 24K, 21K, 18K, 14K | Sell + buy price per gram in EGP |
| Gold pound coin (الجنيه الذهب) | Sell price in EGP |
| Ounce | USD spot price |

Prices come from [gold-price-today.com/egypt](https://www.gold-price-today.com/egypt/) (Cairo jeweler prices). If that site is ever down or changes its layout, the script automatically falls back to the international spot price (api.gold-api.com XAU/USD) converted to EGP at the live USD/EGP rate, and marks those rows as `intl-spot-estimate` in the CSV.

## Project files

```
egypt-gold-watcher/
├── .github/
│   └── workflows/
│       └── gold-watcher.yml    # GitHub Actions: runs every 30 min, commits data
├── data/                       # gold_prices.csv appears here after the first run
├── scraper.py                  # the watcher itself (single-file Python)
├── requirements.txt            # requests + beautifulsoup4
├── .gitignore
└── README.md
```

## Setup — step by step

### Step 1 — Create the repository

1. Log in to [github.com](https://github.com) and click **New repository**.
2. Name it something like `egypt-gold-watcher`, keep it **Private** if you prefer (Actions work on free private repos).
3. Click **Create repository**. Do not add a README yet.

### Step 2 — Upload the project files

Either push with git from your machine:

```bash
git clone https://github.com/YOUR_USERNAME/egypt-gold-watcher.git
# copy scraper.py, requirements.txt, .gitignore, .github/ and README.md inside
git add . && git commit -m "gold watcher" && git push
```

...or simply drag-and-drop the files on the repo web page (**Add file → Upload files**). Make sure the hidden `.github` folder is uploaded too — it contains the workflow.

### Step 3 — Do a manual test run

1. In your repo, open the **Actions** tab. If prompted, click **I understand my workflows, go ahead and enable them**.
2. Select **Egypt Gold Watcher** in the left sidebar.
3. Click **Run workflow → Run workflow** (that is the `workflow_dispatch` trigger).
4. After ~30 seconds, open the run. In the **Fetch gold prices** step you should see today's karat prices printed, and `data/gold_prices.csv` should appear in the repo (committed by the bot).

### Step 4 — (Optional) Set up Telegram alerts

1. In Telegram, message **@BotFather** → send `/newbot` → choose a name and a username. BotFather replies with a **token** like `123456789:AAH-xxxxxxxx`.
2. Start a chat with your new bot (press **Start**). This is required or the bot cannot message you.
3. Get your chat ID: open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser, send any message to your bot, refresh the page, and find `"chat":{"id": 123456789, ...}`. (If you created a group and added the bot, use the group's `-100...` id.)
4. In your repo: **Settings → Secrets and variables → Actions → New repository secret**, and add two secrets:
   - `TELEGRAM_BOT_TOKEN` = the token from BotFather
   - `TELEGRAM_CHAT_ID` = your chat id
5. Test it: `Actions → Egypt Gold Watcher → Run workflow`. The first run always sends one message ("no history yet"), and after that you only get a message when a price actually moves.

No secrets set? That is fine — the watcher still records prices to the CSV and prints them in the Actions log; it just skips messaging.

## How the schedule works (important to know)

- The cron `*/30 * * * *` is in **UTC**. Every 30 minutes UTC = every 30 minutes Cairo time, so no conversion is needed.
- GitHub's scheduler is *best effort*: on the free tier runs can be **delayed by a few minutes** (more at busy times like the top of the hour). This is normal — it is not your bug.
- Scheduled workflows on repos with **no activity for 60 days are auto-disabled**. GitHub emails you about it; just re-enable, or push anything. Since this workflow commits data automatically, the repo stays active on its own.
- `concurrency` is set so overlapping runs queue instead of racing on the CSV.

## The data

`data/gold_prices.csv` — one row per 30-minute check:

```csv
timestamp_utc,timestamp_cairo,source,gold_24k_sell,gold_24k_buy,gold_21k_sell,gold_21k_buy,gold_18k_sell,gold_18k_buy,gold_14k_sell,gold_14k_buy,gold_pound_coin,ounce_usd,usd_egp
2026-09-06 13:30:02,2026-09-06 16:30:02,gold-price-today.com,7235,7175,6330,6280,5425,5385,4220,4185,50640,4428.95,50.8
```

- `source` is `gold-price-today.com` for real jeweler prices, or `intl-spot-estimate` for the fallback (pure spot conversion — treat those rows as an approximation, real Egyptian prices sit slightly below spot math).
- Every row is a git commit, so even if the CSV is ever broken you can recover any past state from history.

## Customization

**Change the frequency** — edit `.github/workflows/gold-watcher.yml`:
```yaml
- cron: '*/30 * * * *'   # every 30 min
- cron: '0 * * * *'      # hourly
- cron: '0 8,20 * * *'   # 08:00 and 20:00 UTC (11:00 / 23:00 Cairo)
```

**Get a message on every check, not only on changes** — in `scraper.py`, change:
```python
should_notify = bool(diffs) or args.force_notify or prev_row is None
```
to:
```python
should_notify = True
```

**Alert only on big moves** — replace `should_notify = bool(diffs) ...` with a threshold, e.g. 5 EGP:
```python
should_notify = any(abs(new - old) >= 5 for old, new in diffs.values())
```

**Run locally** (works anywhere Python 3.9+ is installed):
```bash
pip install -r requirements.txt
python scraper.py --dry-run          # just fetch and print
python scraper.py --force-notify     # also test your Telegram bot
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Actions tab shows nothing | Workflow file must be at `.github/workflows/gold-watcher.yml` on the default branch. |
| Run fails at "Install dependencies" | Check `requirements.txt` was uploaded next to the workflow. |
| Run fails at "Fetch gold prices" | Open the log — the site may be down or restructured. The fallback usually still records spot estimates; if both fail the run is marked red intentionally so you notice. |
| Scrape layout changed | Open `https://www.gold-price-today.com/egypt/`, view the page source, and compare the `<table>` markup with what `scrape_egypt_site()` expects (labels like `عيار 21`). |
| Telegram message never arrives | Message the bot once (press Start), re-check the token/chat-id secrets, and check `getUpdates` returns your chat id. |
| CSV has duplicate timestamps | Two runs overlapped before `concurrency` was added, or you ran `workflow_dispatch` during a scheduled run. Harmless. |
| Scheduled runs stopped | 60-day auto-disable (see above) — re-enable in the Actions tab. |
