# EDITH: Architecture & Phased Plan

Status: draft v0.1. Single-user personal assistant first; multi-user is out of scope until it works for one person.

---

## 1. Feasibility: what is realistic, what is not

The "brain" (understanding, planning, summarising) is the easy part now. The hard parts are **access** (many of your target apps have no sanctioned API) and **trust** (not doing the wrong thing). Plan around those two.

| Capability | Feasibility | Main constraint |
|---|---|---|
| Reminders, deadlines, daily routines | Easy | Must be a deterministic scheduler, not "the LLM remembers" |
| Read / summarise / triage email (Gmail) | Easy | Gmail API OAuth. Unverified "testing" apps get refresh tokens that expire in 7 days; publish as personal/internal or re-auth |
| Calendar, tasks, goals tracking | Easy | Google Calendar API / own DB |
| Web search with citations | Easy | Search API + page fetch |
| Fact-checking news | Medium | Can show evidence and sources, cannot guarantee truth. Output "supported / contradicted / unverified" with citations, never a bare verdict |
| Restaurant / place suggestions | Easy | Google Places API (New). Paid per request, set quotas |
| Long-term memory across chats | Medium | Easy to build, hard to keep *correct* (stale facts, wrong inferences). Needs provenance and editing |
| Multi-step planning + execution | Medium | Reliability compounds: 95% per step is ~77% over 5 steps. Needs checkpoints, verification, resumability |
| **WhatsApp: replying on your personal account** | **Hard / risky** | No official API for personal accounts. The WhatsApp Business Cloud API is for business numbers only. Unofficial libraries (Baileys, whatsmeow, whatsapp-web.js) break ToS and can get the number banned. See options in 4.1 |
| **Ordering on Zomato / Swiggy / others** | **Hard / fragile** | No public consumer ordering API. Verify whether the vendors now offer official agent/MCP integrations before building anything. Fallback is UI automation (browser or Android), which is brittle and handles payments, so it comes last and always needs approval |
| Fully autonomous operation with no hallucination | **Not achievable as stated** | You can make errors rare, detectable and reversible. You cannot make them zero. The design goal is *bounded blast radius*, not perfection |

Honest summary: roughly 70% of the vision (reminders, email, search, places, memory, goals, drafting) is solidly buildable with official APIs. WhatsApp-on-personal-account and food ordering are the two high-risk items and should be isolated behind adapters so a failure there does not affect the rest.

---

## 2. Design principles (these drive every decision below)

1. **LLM proposes, code disposes.** The model never performs side effects directly. It emits structured, schema-validated *action proposals*. A deterministic policy engine decides: allow, ask the user, or deny.
2. **Anything time-based is deterministic.** Reminders, deadlines and routines live in a database and a scheduler. The model only writes to and reads from them.
3. **Every claim has provenance.** Memories, facts and search answers carry their source (message id, URL, email id, timestamp). No source means the assistant says "I don't know" or asks.
4. **Untrusted content is data, never instructions.** Emails, web pages and incoming WhatsApp messages can contain prompt injection. Content from them cannot trigger actions on its own.
5. **Least privilege, reversible by default.** Read-only first. Writes are drafts first. Irreversible or money-spending actions always require explicit approval.
6. **Everything is logged and replayable.** Full audit trail of prompts, tool calls, approvals and outcomes. This is also what makes evals possible.
7. **Abstain over guess.** Low confidence or missing info leads to a clarifying question, not an invented answer.

---

## 3. System architecture

```
                 ┌───────────────────────────────────────────────┐
  Channels       │  Telegram/WhatsApp-Business bot · Web/PWA ·   │
  (I/O)          │  Mobile push · (later) voice                  │
                 └───────────────────────┬───────────────────────┘
                                         │ normalised messages / events
                 ┌───────────────────────▼───────────────────────┐
                 │                 GATEWAY / API                 │
                 │      auth · rate limit · session routing      │
                 └───────────────────────┬───────────────────────┘
                                         │
        ┌────────────────────────────────▼────────────────────────────────┐
        │                         AGENT CORE                              │
        │                                                                 │
        │  Intent/Router ──► Planner ──► Executor ──► Verifier            │
        │        │              │           │            │                │
        │        │         Plan state   Tool calls   Check result         │
        │        │         (durable)    (proposals)  vs. intent/sources   │
        │        ▼                                                        │
        │  Context Builder ◄── Memory Service (read)                      │
        └──────────┬──────────────────────┬───────────────────────────────┘
                   │ action proposals     │ events
        ┌──────────▼──────────┐   ┌───────▼──────────────────────────────┐
        │   POLICY ENGINE     │   │  SCHEDULER / EVENT BUS               │
        │ risk tier · limits  │   │  reminders · routines · deadlines    │
        │ allow / ask / deny  │   │  goal check-ins · inbox polling      │
        └──────────┬──────────┘   └──────────────────────────────────────┘
                   │ approved only
        ┌──────────▼───────────────────────────────────────────────────┐
        │                  TOOL / CONNECTOR LAYER                      │
        │  Gmail · Calendar · Search · Places · WhatsApp adapter ·     │
        │  Food-app adapter · Browser agent · Notes/Tasks              │
        │  (each: typed schema, scopes, sandbox, idempotency key)      │
        └──────────────────────────────────────────────────────────────┘

        Cross-cutting:  Memory Service · Audit Log · Secrets Vault ·
                        Evals/Replay · Observability · Kill Switch
```

### 3.1 Agent core
- **Router**: classifies the request: chat, quick tool call, or multi-step task. Cheap/fast model.
- **Planner**: for multi-step tasks, produces an explicit plan (list of steps with preconditions, tool, expected output, risk tier). The plan is stored as data, shown to the user for medium/high-risk tasks, and can be edited.
- **Executor**: runs one step at a time via the policy engine. State is persisted after every step so a crash or restart resumes rather than repeats (idempotency keys on every side-effecting call).
- **Verifier**: after each step (and at the end) checks the result against the step's expected output and against sources. Examples: "did the email actually send?", "does the restaurant exist and is it open?", "does every claim in the summary map to a source span?" Failure leads to retry, replan, or ask the user.
- Start with a plain loop with explicit state in Postgres. Do not adopt a heavy orchestration framework until you can name a concrete limitation. Consider Temporal only if long-running workflows become painful.

### 3.2 Policy engine and safety (the core of "user control")
Every tool action is assigned a risk tier:

| Tier | Examples | Default behaviour |
|---|---|---|
| 0 Read-only, own data | read calendar, search memory | auto |
| 1 Read external | web search, fetch page, Places lookup | auto, content treated as untrusted |
| 2 Reversible write, private | create reminder, add task, save draft | auto, logged, undoable |
| 3 Communicates with others | send email, WhatsApp reply, calendar invite | **ask**, show exact content. Optionally "auto for this contact and this intent" after repeated approvals |
| 4 Spends money / irreversible | place order, pay, delete data | **always ask**, per-action cap, daily cap, no standing auto-approval |

Additional controls:
- **Prompt-injection defence**: the dangerous combination is *private data + untrusted content + a way to send data out*. Mitigations: tool outputs from tier 1 are wrapped and labelled as data; actions at tier 3+ cannot be justified solely by untrusted content (policy checks the *origin* of the instruction); outbound destinations are allow-listed; a separate "quarantined" model call summarises untrusted content without access to action tools.
- **User control surface**: approval inbox, per-connector on/off, per-contact rules, "what did you do today" log, undo for tier 2, global **kill switch** that revokes tokens and halts the scheduler, spend caps.
- **Dry-run mode** for every new connector: it proposes, nothing executes, you review the log.
- Secrets in an OS keychain or vault, never in prompts or logs. Connectors get scoped, short-lived credentials.

### 3.3 Memory service
Four separate stores, because they have different correctness needs:

1. **Working context**: current conversation plus retrieved items. Built per turn within a token budget.
2. **Episodic**: conversation summaries and events with timestamps. Summarised periodically, raw kept for audit.
3. **Semantic facts / preferences**: structured records (`likes: South Indian food`, `partner's birthday: ...`) each with `source`, `confidence`, `created_at`, `last_confirmed`. User-visible and editable. Inferred facts are stored as *candidates* and promoted only after confirmation or repeated evidence.
4. **Structured state**: tasks, deadlines, routines, goals, plans. Ordinary relational tables. **Not** vector search.

Retrieval = hybrid (keyword + embedding) + recency + importance. Stack: Postgres + pgvector, which avoids running a separate vector DB.

### 3.4 Goals, tracking and proactivity
- `Goal` → `Milestones` → `Tasks`, with target dates and metrics.
- Scheduler runs **check-ins** (daily/weekly) that compute progress from task state and calendar, not from the model's recollection. The model only phrases the result and suggests next actions.
- Proactive suggestions go through a **budget** (max N unsolicited messages/day) and a feedback signal (useful / not useful / snooze) so it learns what to stop doing.

### 3.5 Fact-checking pipeline
1. Extract atomic checkable claims from the article.
2. For each claim, run searches and retrieve from multiple independent sources (prefer primary sources and established fact-checkers).
3. Judge each claim against retrieved passages only: supported / contradicted / mixed / insufficient evidence.
4. Output claim, verdict, quoted evidence, links, and a stated confidence. Source disagreement is shown, not hidden.

### 3.6 Anti-hallucination, concretely
- Structured outputs with schema validation for every tool call and plan. Invalid output is rejected and retried.
- Retrieval-grounded answers with citation spans. A verifier step flags unsupported sentences.
- Entities (people, contacts, restaurants, dates) are **resolved against real records**, not generated. Ambiguity ("which Rahul?") means ask.
- Dates and times are normalised with explicit timezone (user timezone configured once) and echoed back for confirmation when setting reminders.
- An **eval suite** (see 6) is the actual defence. Without it, hallucination rate is a feeling, not a number.

---

## 4. Integration strategy (the risky parts)

### 4.1 WhatsApp: options, in order of preference
1. **Dedicated assistant number on WhatsApp Business Cloud API**: you chat with EDITH over WhatsApp. Official and stable. It does *not* let EDITH read or reply inside your personal chats.
2. **Telegram (or similar) as the primary channel**, with WhatsApp added later. Easiest for development.
3. **Android on-device approach** (notification listener plus accessibility / reply actions): works with your real account, never leaves the phone, but is fragile and may break with app updates.
4. **Unofficial WhatsApp Web libraries**: only if you accept the ban risk on your own number. Isolate behind an adapter. Default to *draft and ask* for every outgoing message. Do not use this on a number you cannot afford to lose.

Recommendation: start with option 2, add option 1 when stable, treat 3 or 4 as an optional, explicitly risky plugin.

### 4.2 Food ordering (Zomato, Swiggy, others)
1. Check for official partner / agent / MCP integrations first (this is changing quickly; verify current status before designing).
2. Otherwise a **browser or Android UI agent** in a sandbox, driving a logged-in session. Constraints: stop before payment, show cart, restaurant, address, total and ETA, and require approval to place. Expect regular breakage and CAPTCHAs/OTPs. Budget maintenance time.
3. Until then, the assistant can do the useful 80%: shortlist places, check reviews, open the app/deep link with the item.

### 4.3 Connector contract
Each connector implements the same interface: typed input/output schemas, declared risk tier, required scopes, idempotency key, `dry_run()`, `undo()` (if possible), and a contract test suite with recorded fixtures. MCP is a good standard for exposing connectors; use it where an MCP server already exists, wrap with the policy engine either way.

---

## 5. Tech stack (proposal, change freely)

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 (FastAPI) | Best ecosystem for agents, APIs, data |
| LLM | Anthropic Claude via API, behind a thin gateway interface | Tool use, structured output, long context. Gateway lets you swap/route models. Small model for routing, stronger for planning/verification |
| DB | Postgres + pgvector | One store for state, memory, audit log |
| Scheduler | APScheduler or a Postgres-backed job table at first; Temporal later if needed | Keep it simple |
| Queue/cache | None at first; Redis when needed | Avoid early infrastructure |
| Channel | Telegram bot first, then WhatsApp Business, plus a small PWA for approvals/dashboard | Fast iteration |
| Browser automation | Playwright in a sandbox | For the later phases |
| Hosting | Local machine / small VPS, single user | Cheap. Data stays under your control |
| Observability | Structured logs + trace per task; simple web viewer | Required for debugging agents |

---

## 6. Evaluation, the thing that makes this "robust"

Build this in Phase 0 and keep growing it:
- **Golden task set**: 50 to 200 realistic requests with expected tool calls / outcomes (e.g. "remind me to pay rent on the 1st" → correct reminder, correct timezone, correct recurrence).
- **Replay harness**: re-run logged real conversations against a new prompt/model/version and diff behaviour. Regressions block merges.
- **Safety suite**: prompt-injection emails and pages, ambiguous contacts, over-limit spends, and "send this without asking" attempts. Pass criterion: zero unapproved tier 3+ actions.
- **Metrics**: task success rate, wrong-action rate, unsupported-claim rate, approval-needed rate, user corrections per week.

---

## 7. Phased roadmap

Rough sizes assume one developer working part-time. Treat them as order-of-magnitude, not commitments.

### Phase 0: Foundations (1 to 2 weeks)
- Repo, Python project, Postgres, config, secrets handling.
- LLM gateway, structured-output wrapper, tool registry with risk tiers.
- Policy engine skeleton (allow/ask/deny), approval flow, audit log, kill switch.
- Eval harness with the first ~20 golden tasks.
- **Exit**: you can define a toy tool, have the model call it, see it blocked/approved by policy, and replay the run from the log.

### Phase 1: Core assistant: chat, reminders, routines (2 to 3 weeks)
- Telegram channel. Conversation loop with context builder.
- Deterministic scheduler: one-off and recurring reminders, daily routines, deadlines with escalation (e.g. 3 days, 1 day, morning of).
- Task list. Memory v1: summaries + structured facts with provenance, "what do you know about me" and edit/delete commands.
- **Exit**: you use it daily for reminders for two weeks with no missed or wrong reminders.

### Phase 2: Read-only world access (3 to 4 weeks)
- Gmail read (triage, summarise, extract deadlines → proposed reminders), Google Calendar read.
- Web search and page fetch with citations. News fact-check pipeline (3.5).
- Google Places recommendations using preferences from memory.
- Prompt-injection safety suite goes live here, since untrusted content is now flowing in.
- **Exit**: unsupported-claim rate below your threshold on the eval set. Email deadlines extracted with high precision.

### Phase 3: Writes with approval (3 to 4 weeks)
- Email drafts and sends, calendar event creation, WhatsApp reply drafting via the chosen route (4.1).
- Approval inbox UI (PWA). Per-contact and per-intent trust rules. Undo for tier 2.
- **Exit**: month of real use with zero unapproved outbound messages and a falling edit rate on drafts.

### Phase 4: Planning, goals, proactivity (4 to 6 weeks)
- Planner/executor/verifier with durable plan state, resume and replan.
- Goals, milestones, progress monitoring, weekly review, suggestion budget with feedback.
- Memory v2: consolidation, decay, conflict resolution, candidate-fact promotion.
- **Exit**: multi-step tasks (e.g. "plan Saturday dinner for 4, check calendar, shortlist 3, draft invites") complete correctly on the eval set at a target success rate.

### Phase 5: Commerce / app automation (open-ended, 4+ weeks)
- Food ordering through the best available route (4.2). Spend caps, mandatory confirmation screen, post-order verification.
- Other apps only one at a time, each behind its own adapter and kill switch.
- **Exit**: 20 consecutive correct orders with approval, zero wrong-item / wrong-address incidents.

### Phase 6: Hardening and polish (ongoing)
- Voice, mobile app / widgets, multi-device, backup and export, cost optimisation (caching, smaller models), privacy review, optionally multi-user.

---

## 8. Main risks and mitigations

| Risk | Mitigation |
|---|---|
| WhatsApp number ban | Official Business API channel first, personal-account access as isolated opt-in plugin |
| Food apps change UI / block automation | Last phase, adapter isolation, human-in-the-loop, fall back to deep links |
| Prompt injection via email/web | Origin-aware policy, quarantined summariser, outbound allow-list, safety eval suite |
| Silent wrong memory | Provenance, candidate vs confirmed facts, user-visible editable memory |
| Cost / latency creep | Model routing, caching, per-day budget, token accounting from Phase 0 |
| Over-notification | Suggestion budget plus feedback loop |
| Scope explosion | Phase gates with measurable exit criteria. Do not start N+1 until N's exit criteria are met |
| Privacy (email, chats go to an LLM provider) | Minimise what is sent, redact where possible, review provider data terms, local-only storage of raw data |

---

## 9. Decisions made

| # | Decision |
|---|---|
| Phone | Android (enables on-device integrations, see 10.5) |
| Primary channel | Telegram. WhatsApp Business is optional later (see 10.2) |
| WhatsApp personal-account risk | Not accepting ban risk on the personal number. If used, only via the Android notification-reply route as an opt-in plugin |
| Region | India only (Zomato, Swiggy, Google Maps India, Indian fact-checkers) |
| Languages | English + Telugu (+ romanised Telugu / English mix) |
| Host machine | MacBook Air M4, 16 GB (local models via Apple Silicon GPU) |
| Email policy | **Option B**: email is S2 and stays on the local model by default; cloud only via per-item "escalate" where the user sees the exact redacted payload and approves. Newsletters/promos may use cloud automatically. OTP/bank/medical/ID never leave. No Telugu emails expected |
| Budget | **Zero.** Free tiers only. Provider gateway must support fallback chains and rate-limit budgeting |

Still open: hosting choice (10.3), and whether you will accept a credit card on file for free-tier signups (Oracle, Google Maps Platform both ask for one).

---

## 10. Zero-cost plan (India / Android / Telugu)

NOTE: figures below are from memory and were NOT re-verified when written. Check each vendor page before relying on a limit or price.

### 10.1 LLM (the main free-tier constraint)
No free Claude API exists. Free options, all rate-limited:
- **Gemini API free tier** (Google AI Studio): best fit. Good Telugu, tool calling, structured output, audio input, Google Search grounding. Limits are small and have been cut before. **Free-tier inputs may be used by Google to improve its products**, so do not send email bodies or private chats through it without accepting that.
- **Groq**, **Cerebras**, **OpenRouter `:free` models**, **Mistral free tier**, **Cloudflare Workers AI** (daily free allowance), **GitHub Models**: fallback chain. Telugu quality is weaker on the open models.
- **Local (Ollama)** on a laptop: private and unlimited, but small models are poor at Telugu and tool use, and need RAM/GPU.
Design consequences: LLM gateway with ordered fallback; per-day request budget; cache aggressively; keep LLM out of anything deterministic (reminders, scheduling); route email/private content to a local or paid-private path later, or redact before sending.

### 10.2 WhatsApp Business (free?)
- The Cloud API has no monthly fee, Meta charges per message. User-initiated chats (you message it, it replies within 24h) are free; proactive messages outside that window (daily reminders you did not just trigger) need pre-approved templates and may be charged.
- Needs a Meta developer + business account and a number not already on WhatsApp. Meta provides a free test number that can message up to ~5 allow-listed recipients, enough for a single user, but with sandbox limitations (token handling, no production guarantees).
- Verdict: usable as a secondary chat channel; poor for free proactive reminders. Keep Telegram primary.

### 10.3 Free hosting options (need always-on for scheduler)
| Option | Pros | Cons |
|---|---|---|
| Cloudflare Workers + Cron Triggers + D1/Durable Objects (webhook design) | Truly free, always on, no server, no card | Serverless style (TypeScript natural), CPU time limits |
| Oracle Cloud Always Free ARM VM (Mumbai/Hyderabad) | Most powerful free box, runs Postgres + bot | Card verification, signup sometimes rejected, idle-reclaim policy |
| Google Cloud e2-micro | Always-free VM | Tiny, US regions only, card required |
| Old Android phone + Termux | Free, local, always with you | Battery/OS kills background apps, fragile |
| Your laptop | Simplest | Reminders missed when off |
| Render / Koyeb / Fly free tiers | Easy deploy | Sleep on idle or limited trial, unsuitable for a scheduler without keep-alive tricks |
Free databases: Neon or Supabase free Postgres (pgvector supported; Supabase pauses inactive projects), or SQLite on the host.
Backup scheduler: GitHub Actions cron (coarse, not for exact-time reminders).

### 10.4 Other free services
- Gmail API / Calendar API: free. Set the OAuth consent screen to "In production" (unverified, personal use) so refresh tokens do not expire after 7 days.
- Places: Google Places (New) has monthly free caps per SKU but requires a billing account; set budget alerts. Free fallback: OpenStreetMap (Overpass/Nominatim), Foursquare free tier. No reviews from OSM.
- Web search: Gemini Search grounding, Tavily/Brave/Google CSE free quotas, or self-hosted SearXNG.
- Fact-check: Google Fact Check Tools API (free) plus Indian fact-checkers (Factly has Telugu content, BOOM, Alt News, Vishvas News) as preferred sources.
- Speech: Telegram voice notes -> Gemini audio or Whisper (Groq free) for Telugu STT; Android built-in TTS for Telugu replies.

### 10.5 Android-specific options
- **WhatsApp via notification reply**: an on-device app reads WhatsApp notifications and replies using the standard inline-reply action. No reverse-engineered protocol, so lower ban risk than WhatsApp Web libraries, but still grey with WhatsApp's terms. Text only, no history. Flow: notification -> backend drafts -> you approve in Telegram -> phone sends reply.
- **Food apps**: check for official Swiggy/Zomato agent (MCP-style) integrations first. Otherwise on-device UI automation (accessibility service) that stops at the cart and requires approval.
- Tasker/MacroDroid for quick glue before writing a native Kotlin app.

### 10.6 Telugu requirements (add to evals from Phase 0)
- Golden set includes Telugu and romanised Telugu ("repu udayam 6 ki lepandi").
- Reminders: model outputs structured ISO time + timezone, bot echoes the parsed time back in the user's language before saving.
- Reply in the language the user wrote in.

### 10.7 (superseded by 11.5) Recommended zero-cost stack
Telegram bot -> Oracle Always Free VM (fallback: Cloudflare Workers) -> Postgres/SQLite -> Gemini free tier with Groq/OpenRouter fallback -> Gmail/Calendar APIs -> OSM or capped Places -> Android helper app later.

---

## 11. Privacy architecture (privacy is a hard requirement)

Principle: privacy comes from **deterministic controls on data flow**, not from asking an LLM to be careful. LLM "privacy agents" can be fooled by the same injected text they are protecting against, so the enforcement points are code. An LLM or NER model is used only for fuzzy detection, never as the final gate.

### 11.1 Data classes
| Class | Examples | May go to |
|---|---|---|
| S0 public | web search queries, generic chat | any provider |
| S1 personal-low | reminder text, preferences, routines | free cloud LLM only if user allows; otherwise local |
| S2 sensitive | email bodies, private messages, contacts, finance, health, location history | **local model only**, or redacted/pseudonymised snippets to cloud if user opts in |
| S3 secrets | OTPs, passwords, card numbers, Aadhaar, PAN, tokens | never sent to any LLM, never stored in memory; handled by code only |

### 11.2 The four privacy components
1. **Classifier**: tags every item with a class at ingestion, based on source (Gmail = S2 by default) plus pattern rules plus a small local model for the fuzzy cases. Unknown defaults to the stricter class.
2. **Redactor / pseudonymiser**: before any cloud call, replaces names, phone numbers, emails, addresses, account and ID numbers with placeholders (`<PERSON_1>`), keeps the mapping locally, and restores it in the reply. Regex plus an NER library (e.g. Presidio with custom Indian ID recognisers). Weaker on Telugu text, so S2 defaults to local-only.
3. **Egress guard**: every outbound network call passes through one local proxy layer. It checks destination against an allow-list and payload class against destination class (for example S2 to a free-tier provider is blocked). Blocks are logged. No connector can bypass it.
4. **Auditor**: local append-only ledger of everything that left the device (destination, class, redacted preview, hash). Weekly "what left your device" report, retention rules, one-command export and delete, kill switch.

Plus: encryption at rest (OS disk encryption plus an encrypted DB), secrets in the OS keychain, minimum OAuth scopes (`gmail.readonly`, not full access), no analytics or telemetry.

### 11.3 Honest limits
- Anything sent to a cloud LLM is out of your control, including free-tier training use. The only strong guarantee is not sending it.
- Pseudonymisation is imperfect: surrounding context can still identify someone.
- **Telegram bot chats are not end-to-end encrypted**; Telegram's servers can read them. Mitigation: never display raw S2/S3 content in chat (show summaries, or "open on device"), and move to a private Android app later.
- Local models are weaker, especially in Telugu. Expect lower quality on private email tasks.
- Verification: an egress test suite (known canary strings must never appear in outbound traffic) runs in CI, and you can inspect the ledger yourself.

### 11.4 Reminders when the laptop is off
Option A (most private): reminders sync to Android's own alarm/notification system through the helper app, so they fire offline with nothing in the cloud.
Option B: a free Cloudflare Worker cron sends a generic Telegram ping ("you have a reminder, open EDITH") with no content.

### 11.5 Revised hosting recommendation
Laptop-first: bot uses Telegram long polling (no open ports, no public server), data in an encrypted local DB, local model for S2. Cloud is optional and holds no sensitive data. If a cloud box is wanted later: Cloudflare Workers (no card) first, Oracle Always Free second.

---

## 12. Trust-domain split (refinement, inspired by the "Muse" description)

Source note: based on a user-supplied, unverified description of Meta's Muse (model + per-user Secure VM + separate "Sentinel" permission authority). Used as a reference point only.

Run EDITH as two local processes with different privileges:

| | Agent runtime (untrusted) | Broker / Sentinel (trusted) |
|---|---|---|
| Contains | LLM calls, planner, executor, sandboxed tools, Playwright in a container | Policy engine, egress guard, approvals, auditor, secrets |
| Credentials | **None** | OAuth tokens, API keys (OS keychain) |
| Network | Only through the broker's proxy | Direct, allow-listed |
| Talks via | Typed action proposals over a local socket | Returns results or denial |

Rules:
- The agent runtime can request "send this email"; the broker checks policy, gets approval, attaches credentials and performs the call. The agent never sees a token.
- The broker is plain deterministic code. No LLM makes the final allow/deny.
- Browser automation runs in a local container with no direct network egress and no access to the host filesystem beyond a scratch volume.
- Payments: final gate is the user's UPI PIN on their own phone. The agent never holds payment credentials.
- Not adopted: cloud per-user VMs (cost, privacy), many parallel subagents (free-tier rate limits).

This replaces the single "Policy Engine" box in section 3 with the broker described here. (Status: the code is organised along this line, `edith/broker/` vs `edith/agent/`, but it still runs as ONE process. The OS-level split is not done. See section 13.)

---

## 13. Implementation status (kept current)

Chronological log, verification levels, known issues and the prioritised backlog live in [BUILDLOG.md](BUILDLOG.md).

Last updated alongside the web search / fact-check / places work (after Gmail, memory, goals, evals). "Tested" means unit tests against fakes. Nothing has yet been run against a real model, a real Telegram bot, or real Gmail.

| Area | Status | Code | Notes |
|---|---|---|---|
| Data classes, classifier, redactor | Built, tested | `edith/privacy/` | Pattern-based; person names only via supplied contact names |
| Egress guard, ledger, policy tiers, kill switch | Built, tested | `edith/broker/` | Hash-chained local ledger; halted state blocks all egress except the owner chat |
| LLM gateway (fallback, daily budgets, approval-bound escalation) | Built, tested | `edith/broker/gateway.py` | |
| Ollama + Gemini providers | Built, tested vs fake HTTP servers | `edith/providers/` | Real models not yet exercised (needs the personal Mac) |
| Reminders, recurrence, nags, deadlines, scheduler | Built, tested | `edith/scheduler/` | At-least-once delivery with backoff |
| NL parsing (English / Telugu / romanised) + confirm-before-save | Built, tested | `edith/agent/` | Telugu UI strings need native review; real-model accuracy unmeasured |
| Telegram channel, owner lock, pairing, app runner | Built, tested vs fakes | `edith/channels/`, `edith/app.py` | Never run against the real Telegram API |
| Memory v1 (facts, provenance, candidates, forget) | Built, tested | `edith/memory/` | Keyword retrieval. `context_block()` exists but is not yet fed into prompts |
| Goals, tasks, progress, review, weekly review reminder | Built, tested | `edith/goals/` | Progress is computed from task state, never by a model |
| Evaluation harness | Built, tested | `edith/evals/` | `python3 -m edith.evals` (replay) and `--live` (real model, fake data) |
| Gmail: read-only client, Google OAuth (loopback + PKCE), triage, `/mail` digest, `/mailadd` | Built, tested vs fake Google servers | `edith/connectors/`, `edith/agent/mail.py`, `edith/agent/email_triage.py` | Never run against real Google. Triage runs on the LOCAL model only (email is S2); secret-bearing mail never reaches any model; the digest is shown in Telegram only after you approve that exact text. Per-item "escalate to cloud" for email is NOT built (design in 11.x remains) |
| Web search (Tavily), fact-checking, Places suggestions, persistent free-tier budgets | Built, tested vs fake servers | `edith/connectors/search.py`, `places.py`, `edith/agent/factcheck.py`, `edith/store/usage.py` | Never run against the real services; request shapes follow the vendors' docs as I know them, verify with real keys. Only typed query text leaves the machine (personal details refused, secrets denied). Fact-check verdicts need quotes verified word-for-word against the sources, otherwise they are downgraded to "insufficient". Place ranking uses your saved preferences locally; they are never sent. LLM daily budgets now persist across restarts |
| Planner / executor / verifier | Not started | | |
| Agent/broker OS-level process split | Not started | | Single process today |
| Android helper, WhatsApp route, food ordering | Not started | | Need device and accounts |
| Voice, suggestion budget, embeddings | Not started | | |

Phase mapping: Phase 0 is mostly done except the process split. Phase 1 is functionally complete; its exit criterion (two weeks of real daily use with no missed or wrong reminders) has NOT been met. The next real milestone is running it on the personal Mac and measuring with `--live`.

Deviations from the plan above: Python 3.9 standard library only (no FastAPI/Postgres yet); SQLite instead of Postgres; keyword memory retrieval instead of pgvector; single process instead of two.
