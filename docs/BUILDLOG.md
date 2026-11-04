# EDITH build log

Single source of truth for **what was built, how it was verified, what is known to be wrong, and what is next**.
Module-level status table: [ARCHITECTURE.md §13](ARCHITECTURE.md#13-implementation-status-kept-current).

Verification levels used below:
- **V0** written, not executed
- **V1** unit tests pass (fakes only: fake HTTP servers, fake models, fake clocks)
- **V2** exercised against the real external service / model
- **V3** in daily use and meeting the phase exit criterion

---

## Increments

| # | Increment | Commit | Level | Tests |
|---|---|---|---|---|
| 1 | Privacy core: sensitivity classes, pattern classifier, redactor, egress guard, hash-chained ledger, policy tiers | 79c51c6 | V1 | 38 |
| 2 | LLM gateway (ordered fallback, daily budgets, approval-bound escalation) | 79c51c6 | V1 | +9 |
| 3 | Ollama + Gemini providers, hardened HTTP helper (no redirects, no secrets in errors) | 79c51c6 | V1 | +11 |
| 4 | Reminder scheduler, recurrence, nags, deadlines; NL parser (EN/TE) with confirm-before-save; assistant | 79c51c6 | V1 | 97 total |
| 5 | Telegram channel, owner lock + pairing, kill switch, config, app runner, notifier | next commit | V1 | |
| 6 | Memory (facts with provenance/candidates), goals/tasks/progress/review, eval harness (25 cases) | next commit | V1 | |
| 7 | Read-only Gmail: OAuth (loopback + PKCE), client, local-only triage, `/mail`, `/mailadd` | next commit | V1 | |
| 8 | Web: Tavily search, Google Fact Check, fact-check pipeline with quote verification, Places + local ranking, persistent budgets | next commit | V1 | |
| 9 | Static-review fixes: secrets in email subjects no longer block the whole digest; stray loopback requests no longer abort OAuth login | next commit | V1 | **255 total** |

Increments 5–9 were written while the development shell was unavailable, then executed together for the first
time: **255 tests, all passing on the first run (2.5 s)**.

---

## Known issues and risks (open)

| ID | Severity | Issue | Plan |
|---|---|---|---|
| K2 | High | No V2 for any external integration (Telegram, Ollama, Gemini, Gmail, Tavily, Fact Check, Places). Request shapes follow vendor docs from memory | B2 on the personal Mac |
| K3 | Medium | Single process: the agent and the broker share memory, so the credential-isolation claim in ARCHITECTURE §12 is aspirational | B6 |
| K4 | Medium | Telugu UI strings are unreviewed machine-written Telugu | Native-speaker review |
| K5 | Medium | `_pending` confirmations and `/mail` suggestions live in memory: a restart loses a pending "yes/no" (safe: nothing is saved) | Acceptable for now; persist if it annoys in use |
| K6 | Low | Usage budgets roll over at 00:00 UTC (05:30 IST), not local midnight | Use user TZ in `UsageCounter._period` |
| K7 | Low | Aadhaar pattern over-matches any 12-digit number starting 2–9 (e.g. order numbers) | Deliberate (fails closed); add Verhoeff checksum to reduce false positives |
| K8 | Low | Memory retrieval is keyword overlap; `FactStore.context_block()` is not yet fed into any prompt | Wire into planner (B5) |
| K9 | Low | Places requires a billing account; EDITH's cap is a soft guard, not a billing limit | Documented; user sets a GCP budget alert |
| K10 | Info | Development laptop is an employer-managed device with a TLS-inspecting proxy; model pulls are blocked there | Real use only on the personal Mac |

---

## Backlog (prioritised)

Each item has an acceptance criterion; an item is done only when that criterion is met.

| ID | Item | Acceptance criterion | Depends on |
|---|---|---|---|
| B1 | Run and green the full suite + `python3 -m edith.evals` | Suite: done (255 pass). Remaining: eval replay shows wrong-action rate 0; commit increments 5–9 | — |
| B2 | First real run on the personal Mac (Ollama + Telegram) | Pair the bot; 10 reminders set via NL in EN and TE, all correct; `--live` eval run recorded below | B1 |
| B3 | V2 for Gmail, Tavily, Fact Check, Places with real keys | Each command returns sane output; fix any request-shape mismatch | B2 |
| B4 | Phase 1 exit: two weeks of daily reminder use | Zero missed or wrong reminders, logged | B2 |
| B5 | Planner / executor / verifier (multi-step tasks) | Durable plan state, per-step policy check, resume after crash; eval cases for 3 multi-step tasks | B1 |
| B6 | OS-level agent/broker split | Agent process holds no credentials; all network via broker socket; test that agent cannot open sockets directly | B1 |
| B7 | Proactive suggestions with a daily budget + feedback (useful / not useful / snooze) | Max N unsolicited messages/day enforced; feedback changes future behaviour | B5 |
| B8 | Per-item "escalate this email to the cloud" (design §11, decision B) | Shows exact redacted payload; sends only after approval of that hash | B3 |
| B9 | Android helper: local alarms (reminders survive laptop off), WhatsApp notification-reply drafts | Reminder fires with laptop off; WhatsApp reply only after approval in Telegram | B4 |
| B10 | Food ordering (Swiggy/Zomato): first check for official agent/MCP integrations | Decision recorded; if built, stops at cart and needs approval + UPI PIN | B9 |
| B11 | Voice notes (Telugu STT), encrypted DB at rest, backup/export | — | B4 |

---

## Live eval results

_None yet. Paste `python3 -m edith.evals --live` output here with the date, model name and host machine._
