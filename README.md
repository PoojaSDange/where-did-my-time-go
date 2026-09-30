# Where Did My Time Go?



**Local-first by design because browsing history is sensitive, with a hosted demo using synthetic data only.**

An AI-powered browser productivity analyzer. It answers: *where did my browsing time go, what was I doing, what was potentially wasted, what patterns appear over days and months, and what should I change?* — and lets you interrogate your own history through an AI supervisor agent.

- **Backend:** Python, FastAPI, SQLite. **Browser:** Chrome extension (Manifest V3, vanilla JS). **Website:** plain HTML/CSS/JS.
- **AI:** Gemini (one-time history classification), Groq + Qwen (live classification), Groq (daily/monthly analysis, supervisor agent).
- No React, TypeScript, LangChain or LangGraph. Every model name and limit comes from environment variables.

---

## Privacy: exactly what leaves your machine

Everything is stored in a local SQLite file. No accounts, no cloud storage. **But classification and analysis do send compact, redacted activity data to Gemini and Groq.** Read this before using real data.

| Sent to | When | What is sent |
|---|---|---|
| **Gemini** | Once, during first-run history import — only for pages the built-in rules cannot classify | Per *unique* page: a short numeric id, the **domain**, a coarse **path shape** (max 2 segments, IDs masked, e.g. `/user/{id}`), a **redacted title** (≤120 chars), total seconds, visit count |
| **Groq (Qwen)** | Live classification of new pages not already known | The same per-page fields as above |
| **Groq** | Daily analysis (after a day ends) | Per page: domain, path shape, redacted title, category, seconds, visits, first/last **local time HH:MM**, seconds flagged distracting. Plus the day's computed totals as text |
| **Groq** | Monthly analysis | Aggregate totals, weekly breakdown, top distraction domains, short AI-written daily insights |
| **Groq** | Ask page | Your question, the last 4 chat turns, and compact tool results (aggregates, domains, redacted titles) |

**Always stripped or redacted before sending:** query strings and fragments; full URLs (only domain + path shape); emails, URLs, long numbers/card-like numbers and long tokens inside titles.

**Never sent:** titles/URLs/domains of **sensitive domains** (banking, mail, health, password managers — configurable, see `SENSITIVE_DOMAINS` and the in-app *Privacy & data* panel). They are stored with a generic category, and only appear to an AI as a category + seconds under the label `[sensitive]`. Excluded domains are never tracked or stored. Incognito windows are never tracked. Your API token, IP, and raw timestamps never leave.

**Caveats you should know**
- The **domain name itself** is sent for non-sensitive sites. If a site name is private to you, add it to the sensitive list or the exclusion list.
- **Free-tier API data may be used by providers for product improvement.** If you want stronger guarantees, use paid / no-training API settings with Groq and Gemini.
- History import and analysis only send data if you set the API keys. Without keys, rules still classify known sites and all metrics still work; unknown pages simply stay "unclassified".

**Your controls:** *Delete all my data* (wipes the local DB), a per-domain exclusion list, an extra sensitive-domain list, and per-session classification corrections. All in the website's *Privacy & data* panel.

---

## How it works

```
first run                                          every day after
Chrome History file ──copy──> history_reader       Extension (MV3) ──idempotent upload──> /api/extension/activities
   → clean → estimate durations (conservative)        → normalize → split at local midnight
   → rules → cache → Gemini (unique pages only)       → override → rules → cache → pending
   → deterministic daily summaries (no LLM)           Classification worker (Groq + Qwen, token-budgeted)
   → monthly narratives (once)                        Catch-up job: analyse each finished local day once
                                                      → monthly narratives → Ask agent (tool calling)
```

- **Two kinds of data, always separate:** `history_estimated` (reconstructed from Chrome history, approximate, conservative) and `extension_measured` (live). History rows end at or before the activation timestamp — they never overlap measured data.
- **The LLM never does arithmetic.** Python/SQL computes every duration, total, percentage and aggregation; the model only interprets evidence and writes narrative. The agent's tools return pre-formatted durations.
- **Category is not waste.** `is_wasted` is decided *per session* (only social/entertainment, confident, long enough, and either in work hours or very long). Unsure → `ambiguous` and not wasted. A page's category is cached, its waste flag is not.
- **Laptop-safe scheduling.** Nothing relies on the PC being on at midnight. A catch-up job runs at startup and every ~45 min, finds finished days without an analysis, processes them oldest-first within the token budget, and resumes partial work instead of restarting.
- **Groq budget.** One shared client: RPM/TPM limiter, persisted daily token counter, `retry-after` + backoff + jitter on 429, a circuit breaker, and priorities (live > daily > monthly > Ask, with a reserved slice so Ask never dies).
- **Extension.** Service-worker safe: the open session is persisted on every change, heartbeats and flushes run on alarms, a large gap since the last heartbeat closes the session *at the last heartbeat* (dead time is never counted), uploads carry client-generated ids so retries are idempotent.

---

## Run it locally (real use)

Requires Python 3.10+ and Chrome.

```bash
cd "<YOUR_PATH>"      # your path
python -m venv .venv && .venv\Scripts\activate                 # (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env                                         # (cp on Linux/macOS) then add GROQ_API_KEY / GEMINI_API_KEY
python -m uvicorn main:app --reload
```

1. On first start the terminal prints your **API token** (also saved to `backend/data/api_token.txt`). It is only shown once.
2. Open **http://localhost:8000**. Paste the token when asked. On first run the site **lists your Chrome profiles** (name, email, folder, last used) and waits: **nothing is read or imported until you pick one** and click *Import this profile*. It then imports that profile's last ~2 months (progress bar at the top). Chrome may stay open — the history file is copied to a temp file, never read in place, and never modified. Your choice is remembered. To skip the chooser you can force a file with `CHROME_HISTORY_PATH` in `.env` (a profile folder like `...\User Data\Profile 2` also works).
3. Load the extension: `chrome://extensions` → *Developer mode* → *Load unpacked* → choose the `extension/` folder. Click its icon, paste the same token, *Save & connect*, then **Activate tracking**. Live measurement starts at that exact moment.

The server binds to `127.0.0.1` only. Every API call needs the token; CORS allows only the website origin and the extension's fixed origin; requests with any other `Origin` or `Host` are rejected.

Reset the token: `python security.py reset`.

## Demo mode

`APP_MODE=demo` seeds ~60 days of **synthetic** data (46 estimated + 14 measured), disables history reading and live ingestion, and serves the same website. Try it locally:

```bash
set APP_MODE=demo && python -m uvicorn main:app          # (export APP_MODE=demo on Linux/macOS)
```

`render.yaml` deploys it to Render. The demo SQLite lives in `/tmp` and is re-seeded when empty; there is no persistent disk. The Ask page works only if you set `GROQ_API_KEY` on that instance (rate-limited per IP).

---

## Tests

```bash
cd backend && pip install -r requirements-dev.txt && python -m pytest tests     # 96 backend tests
node extension/tests/sim.js                                                     # 11 extension simulations (mock chrome API, fake clock)
bash tests/run_frontend_tests.sh                                                # 8 page tests: jsdom against a live demo backend
bash tests/run_frontend_tests.sh frontend_local.js                              # 4 tests: profile chooser flow against a live local backend (fake profiles)
```



## Layout

```
backend/    main.py config.py database.py models.py security.py  routes/  services/  tests/
extension/  manifest.json background.js popup.html popup.js  tests/sim.js
frontend/   index|activity|insights|trends|ask.html  assets/{style,script,api,pages}.js|css
tests/      frontend_dom.js  run_frontend_tests.sh
```


Preview - https://where-did-my-time-go-demo.onrender.com
