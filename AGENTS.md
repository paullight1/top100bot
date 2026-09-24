# Agents.md — Unipod WhatsApp Community Bot

## What this project is

A WhatsApp bot that acts as an ambient member of the Unipod innovators
community (multiple WhatsApp groups, same purpose: events, hackathons, and
related discussion). Members are aware that **all text in connected groups is
recorded** for learning and support.

The bot passively learns from group conversation (and, in later phases, shared
documentation). It answers **in the group** when it has a grounded answer from
that knowledge. It does **not** guess: if it does not know, it stays **silent
in the group** and **notifies the admin by DM** with the specific question so
the admin can add or clarify information.

Replies should be **straight to the point** (factual, no filler). Default to a
short answer. If the grounded answer cannot be explained in a few sentences,
write a longer explanation in the same chat (group or DM) — still no padding.
The bot holds useful conversations without a manually maintained system prompt.

## Locked-in tech stack — do not substitute without asking first

- WhatsApp connection: `neonize` (Python bindings over whatsmeow). NOT Baileys,
  NOT whatsapp-web.js, NOT the official WhatsApp Cloud API — evaluated and
  rejected (Cloud API's Groups feature can't join existing groups and caps at
  8 participants).
- LLM: Groq API, `gpt-oss-120b` with multi-key pool rotation (`GroqKeyPool`)
  to mitigate rate limits. NOT `compound`/`compound-beta` (tighter
  free-tier daily cap, less reliable structured output for extraction). Do not
  propose fine-tuning.
- Long-term memory: `graphiti-core` (temporal knowledge graph) with the
  FalkorDB backend. NOT Neo4j, NOT a plain vector DB as the primary mechanism.
- Graph storage: FalkorDB (Redis-based), via Docker (native ARM64).
- Text-to-Speech: Self-hosted Piper TTS (HTTP API) with persistent SHA-256 disk
  caching (`TTS_CACHE_ENABLED`) to eliminate CPU spikes and latency on repeated
  phrases.
- Document Processing: `pypdf` for in-memory PDF extraction and chunked Graphiti
  ingestion.
- Short-term memory: a simple rolling per-chat buffer (dict or SQLite) — not a
  separate framework.
- Package management: `uv`, not plain `pip` — there's a known neonize/protobuf
  version conflict with parts of graphiti-core's dependency tree. Always test
  the full combined install in an isolated venv before assuming it works.
- Python 3.11.

## Community groups & knowledge scope

- All connected groups serve the **same Unipod innovators community**; content
  overlaps (events, hackathons, announcements). Cross-group retrieval is
  intentional.
- Graphiti episodes use a **shared logical partition** for retrieval (e.g.
  `group_id = unipod:innovators`), while preserving **provenance** (actual
  WhatsApp group JID, message id, sender, timestamp) in episode content or
  metadata.
- Ingest **every group text message and PDF document** silently; replies follow
  the policy below.

## Architecture (do not restructure without discussion)

1. neonize listens to every message across all connected groups and DMs.
2. Group text messages are buffered in memory (`IngestBuffer`) and pushed into
   Graphiti in batched episodes (interval timer or max batch size) — this
   drastically cuts Groq extraction token consumption and prevents 429 limits.
3. PDF documents shared in groups are extracted, chunked, and ingested into
   Graphiti with optional language translation. Users can also request documents
   via natural language (e.g. "send me the pdf") and the bot transmits the file.
4. **Reply routing (groups):**
   - **@-mention** of the bot → evaluate whether a grounded answer exists; if
     yes, reply **in that group**; if no, **stay silent in group** and **DM
     the admin** (see Admin workflow).
   - **No @-mention** but the message is a **question** and retrieval confidence
     indicates a grounded answer → reply **in group** (same length/discipline as
     above).
   - **Voice requests:** messages starting with `/voice` synthesize the grounded
     reply via Piper TTS (with SHA-256 cache lookup) and send as an audio voice note
     (`PTT`), gracefully falling back to text on TTS error.
   - **Reminders:** members can set reminders either via slash command (e.g.
     `/reminder 10m start meeting`, `/reminder list`, `/reminder cancel <id>`) or
     plain language tagging the bot (e.g. `@bot can you set a reminder for the next 10 mins
     so that I can start my meeting`). If details are missing or `/reminder` is sent alone,
     the bot interactively prompts the user for the time and topic.
   - **Summary Skill (`/summary`):** members can request highly detailed briefings for
     specific dates or time windows (e.g. `/summary 24 sept`, `/summary today`,
     `/summary last 2 days`, `/summary week`, `/summary month`, `/summary 2 weeks`).
     If no activity was recorded for the period, the bot explicitly states so.
     Conversational questions about today (e.g. "what happened today?") receive a shorter,
     concise overview ending with a tip directing users to the `/summary` skill.
     All bot capabilities are referred to specifically as **skills**.
   - Otherwise → ingest only; **no group reply**.
   - Answers must be grounded in retrieved Graphiti facts (and short-term
     context where relevant), not invented when retrieval is empty or below
     threshold.
5. **Direct messages (DMs):** apply the same know / don't-know rule: reply in DM
   when grounded; if not known, stay silent in the user DM and **notify admin**
   with the question.
6. On a **known** reply: pull recent per-chat history (short-term) + query
   Graphiti for relevant facts (long-term, scoped to the community partition) →
   assemble prompt (incorporating roster context and topic helpers where relevant)
   → call Groq (`gpt-oss-120b`) → send via neonize → write the exchange back into
   Graphiti.

## Group reply policy (behavioral)

- **In group when answering:** default to a short, factual reply (about 1–3
  sentences) with a warm, accommodating, and eloquent tone in clear paragraphs.
  Use 1–2 relevant emojis per reply for warmth; do NOT overuse emojis or add
  hollow filler like “Great question!”. If a correct answer **requires** more
  (multi-step how-to, event rules, several facts), write the longer explanation
  in the same group with clear structure.
- **Message formatting conventions:**
  - **Numbered lists (`1. `, `2. `):** use strictly for sequential steps,
    ordered how-to procedures, chronological timelines, or ranked processes
    where step order matters.
  - **Bullet points (`• `):** use for non-sequential, parallel collections
    (multiple events, eligibility rules, resources, team matches).
  - **Hyphens (`-`):** use only for inline attribute separators (e.g. `• Hackathon - Oct 15 - Lab 3`)
    or time/date ranges (e.g. `9:00 AM - 5:00 PM`). Do NOT use hyphens as top-level
    list markers or markdown dividers.
  - **When NOT to use either:** single-fact replies, standard conversational
    answers, and confirmations must use natural sentences/paragraphs (never
    wrap a single fact inside a bullet or number).
  - **Voice mode (`/voice`):** voice notes must use fluid, conversational spoken
    English with natural spoken transitions (*"First...", "Next...", "Additionally..."*)
    and spell out ranges with "to"; never speak visual bullets, list numbers,
    or raw URLs.
- **Source attribution:** when citing information from the knowledge graph,
  mention who originally provided it. If the source is not an admin or the
  information seems like an observation rather than an official statement, note
  that it may not be fully verified and direct the user to the feedback contacts
  (`FEEDBACK_CONTACTS` env var). Forward non-admin facts used in replies to admin
  DMs for verification.
- **Topic Helpers:** when configured via `HELPERS_CONFIG`, provide relevant
  contact details of community topic leaders/helpers for specialized domains.
- **Triggers:** @-mention of the bot **or** question-like message with
  sufficient retrieval support — both paths use the same grounding rules.
- **Directed questions:** if a message @-mentions specific users (not the bot),
  the question is directed at them. The bot stays **silent** and does not answer
  on their behalf. The bot only intervenes if the addressee later replies with
  factually incorrect information.
- **Fact-correction:** if a non-question statement contains factual claims that
  contradict verified knowledge in Graphiti, the bot **may** politely and gently
  correct the misinformation (governed by `RETRIEVAL_MIN_CORRECTION_FACTS` and
  topic/user cooldowns to avoid arguing). Corrections are brief and non-confrontational.
- **When the bot does not know:** no public "I don't know" in the group; **silent
  ingest** + **admin notification** only.
- **Do not** reply to banter, subjective opinions, or obvious non-questions
  unless @-mentioned (then still apply don’t-know → silent + admin notify).

## Admin workflow & access

- **`ADMIN_PHONE` (or allowlisted JIDs) in `.env`** — admin identity is checked in
  **application code before any LLM call**, not via prompt instructions. Other
  users cannot gain admin behavior through prompt injection.
- When the bot cannot answer a **group or DM question** that qualified for a
  reply attempt (@ or question + routing), send the **admin a DM** containing at
  least: originating chat (group JID or DM), sender, message text, WhatsApp
  message id, and timestamp.
- **Daily Activity Reports:** an automated daily report is compiled and sent
  strictly to admin DMs at `DAILY_REPORT_HOUR_UTC` (never to groups), reporting
  messages ingested, questions answered vs forwarded, TTS voice notes, and Groq
  API usage.
- **Health Checks & Error Tracking:** periodic background health checks
  (`HEALTH_CHECK_INTERVAL_SECONDS`) monitor FalkorDB, Groq pool, and queue
  health. Critical alerts are sent to admin DMs with cooldown throttling
  (`CRITICAL_ALERT_COOLDOWN_SECONDS`).
- Admin developer commands (`status`, `health`, `errors`, `recent`, `ping`)
  remain **DM-only** and **allowlist-gated**.

## Reliability requirements (non-negotiable)

- Never block the asyncio event loop with a Graphiti/Groq call inside the raw
  message handler — use a queue + background workers.
- Use WhatsApp message IDs as Graphiti episode reference keys (`uuid` or
  equivalent) to avoid duplicate ingestion on reconnect.
- Wrap all Groq calls with retry + backoff + timeout (`tenacity`).
- Manage multiple Groq API keys with automatic failover on 429 rate limits, and
  clean administrative alerting when all keys are exhausted.
- Process must auto-restart on crash (systemd `Restart=always` or Docker
  `restart: unless-stopped`).
- FalkorDB must run with persistence enabled (RDB/AOF), not ephemeral.
- AWS T4g ARM64 CPU credit conservation: use native `linux/arm64` container
  builds, batched ingestion, and persistent audio caching to prevent CPU
  credit exhaustion and instance throttling.

## Explicitly out of scope for now

- Media parsing (images, video, stickers) — text, PDF documents, and voice replies only for v1.
- n8n or any external orchestration tool — everything is plain Python.
- Any browser storage, frontend UI, or web app — headless backend service only.

## Build order & feature status

1. [x] neonize connection + auth (QR/pairing); stable receive/send.
2. [x] Reply routing stubs: @-mention, question heuristic, admin notification on unknown.
3. [x] Groq integration for reply generation.
4. [x] FalkorDB + graphiti-core ingestion pipeline.
5. [x] Retrieval-gated replies, confidence threshold, grounding rules.
6. [x] Reliability & Scale:
   - Batched ingestion buffer (`IngestBuffer`)
   - Groq multi-key pool (`GroqKeyPool`)
   - Automated health checks and daily admin reports
   - PDF document ingestion and retrieval
   - Piper TTS voice notes (`/voice`) with persistent SHA-256 disk caching
   - AWS T4g Graviton ARM64 native deployment configs

If a task seems to require deviating from anything above (new library,
different DB, different LLM provider), stop and ask before proceeding.
