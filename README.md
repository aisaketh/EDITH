# EDITH

A privacy-first personal assistant. Runs on your own machine, talks to you over Telegram, keeps your data local.
Design and roadmap: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status

Implemented: reminders, deadlines, recurring routines, nags; English and Telugu input with confirmation before
saving; owner-locked Telegram bot; kill switch; audit ledger; memory (facts you control); goals, tasks, progress
and weekly review; read-only Gmail digest; web search, fact-checking and place suggestions; an evaluation
harness. Not built yet: multi-step planning, Android helper, WhatsApp replies, food ordering. Detailed status table: section 13 of [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Nothing has been run against a real model or the real Telegram API yet; do that first (see below).

## Run the tests

    python3 -m unittest discover -s tests -t .

No dependencies are needed (standard library only, Python 3.9+).

## Run the bot

1. In Telegram, talk to **@BotFather**, send `/newbot`, and copy the token.
2. Optional, for natural-language reminders (without a model, only `/commands` work):
   - Install [Ollama](https://ollama.com) and `ollama pull qwen3:8b`, then start it with `OLLAMA_NO_CLOUD=1 ollama serve`.
   - and/or get a free Gemini API key from Google AI Studio.
3. Start EDITH:

        export TELEGRAM_BOT_TOKEN='123456:ABC...'
        export EDITH_LOCAL_MODEL='qwen3:8b'            # optional: local model (private)
        export EDITH_GEMINI_MODEL='<model name>'       # optional: free cloud model, also needs GEMINI_API_KEY
        python3 -m edith

4. The terminal prints a one-time code. Send `/pair <code>` to your bot from your own Telegram account.
   Only that account is ever served; everyone else is ignored.

`python3 -m edith --check` prints the configuration summary (no secrets) and exits.

## Gmail (optional, read-only)

EDITH can summarise your recent mail and suggest deadlines. It can read mail; it cannot send, change or delete it
(scope `gmail.readonly`). Email is treated as sensitive: it is summarised **only by your local model**, mail
containing codes/IDs/account numbers is never sent to any model, and the digest is shown in Telegram only after
you approve that exact text (Telegram chats are not end-to-end encrypted).

One-time setup:
1. Google Cloud Console: create a project, enable the **Gmail API**, configure the OAuth consent screen
   (set publishing status to **In production**; as an unverified personal app you will see a warning, but refresh
   tokens then do not expire after 7 days as they do in "Testing"), create an OAuth client of type **Desktop app**.
2. `export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=...`
3. `python3 -m edith gmail-login` and follow the browser prompt. The refresh token is stored in `~/.edith/google_token.json` (mode 0600).
4. Needs a local model (`EDITH_LOCAL_MODEL`). In the bot: `/mail` or `/mail from:bank newer_than:7d`, then `yes`; `/mailadd N` turns a suggested deadline into a real one.

## Web: search, fact-check, places (optional)

- `/search <query>` needs `TAVILY_API_KEY` (free tier at tavily.com).
- `/factcheck <claim or headline>` uses web search plus Google's Fact Check Tools (`GOOGLE_API_KEY`, enable the
  "Fact Check Tools API"). A model judges the evidence, and code then verifies every quote it cites word for word;
  an answer it can't back with verified quotes is downgraded to "insufficient". Single-source answers are capped at
  low confidence and conflicts with published fact-checks (Factly, BOOM, Alt News...) are shown, not hidden.
  It gives evidence to weigh, never certainty.
- `/food <what and where>` (e.g. `/food biryani near Banjara Hills`) uses Google Places (New) with the same
  `GOOGLE_API_KEY` (enable "Places API (New)"; billing must be on, but a monthly cap is enforced:
  `EDITH_PLACES_MONTHLY`, default 100, and you should also set a budget alert in Google Cloud). Your saved
  preferences (`/remember I am vegetarian`) re-rank results **on this machine**; they are never sent.
- Only the typed query leaves your machine. Queries with a phone number or email are refused; secrets are denied.
  Free-tier usage is capped (`EDITH_SEARCH_MONTHLY`=300, `EDITH_FACTCHECK_DAILY`=100) and remembered across restarts.

## Measure how well it understands you

    python3 -m edith.evals                # replays recorded model replies through the validators (offline)
    python3 -m edith.evals --live         # sends the case texts to your configured model(s); fake data only
    python3 -m edith.evals --live --allow-cloud   # also let the free cloud model see those fake texts

The number to watch is the **wrong-action rate** (must be 0): a proposal that differs from what was asked,
or one produced where none is allowed (secrets, past dates). Cases live in `edith/evals/cases.json`
(English, Telugu script, romanised Telugu, ambiguity, secrets, prompt injection). Add your own phrasing there.

## Commands

    /list  /show N  /done N  /cancel N  /snooze N 30m
    /remind YYYY-MM-DD HH:MM text      /deadline YYYY-MM-DD HH:MM title
    /remember <fact>  /facts  /forget N  /confirm N         your memory: visible, editable, deletable
    /goal <title> by YYYY-MM-DD  /goals  /closegoal N [abandon]
    /task [goal N] <title> [by YYYY-MM-DD]  /tasks  /task done N
    /review                      progress summary (computed from your tasks, not by a model)
    /review weekly               schedule that summary every Sunday 6 PM
    /mail [gmail search]  /mailadd N       read-only email digest (see Gmail section)
    /search <query>  /factcheck <claim>  /food <what and where>      (see Web section)
    /halt   stop everything (survives restarts)       /resume

Or just write: "remind me to pay rent tomorrow at 9am" / "repu udayam 6 ki lepandi".

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | required |
| `EDITH_HOME` | data directory, default `~/.edith` (mode 0700) |
| `EDITH_LOCAL_MODEL` | Ollama model name |
| `EDITH_GEMINI_MODEL`, `GEMINI_API_KEY` | free-tier cloud model |
| `EDITH_GEMINI_DAILY` | self-imposed daily request budget, default 100 |
| `EDITH_ALLOW_S1_CLOUD=1` | let low-sensitivity personal text (reminders) go to the cloud model without asking |
| `EDITH_TZ` | timezone, default `Asia/Kolkata` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Gmail (read-only) |
| `TAVILY_API_KEY` | web search |
| `GOOGLE_API_KEY` | Fact Check Tools + Places (New) |
| `EDITH_SEARCH_MONTHLY`, `EDITH_FACTCHECK_DAILY`, `EDITH_PLACES_MONTHLY` | free-tier caps (300 / 100 / 100) |

## Privacy notes

- Email-class data never goes to a cloud model without your per-item approval; secrets (OTP, card, PAN, Aadhaar,
  passwords) never leave the code that holds them. See section 11 of the architecture doc.
- Telegram bot chats are not end-to-end encrypted. Don't put secrets in them.
- Logs contain metadata only, never message text. The audit ledger stores only redacted previews and hashes.
- Don't run this on an employer-managed device with personal data.
