# Conversational replies and quieter admin notifications

The bot now answers direct greetings, thanks, test messages and capability questions
without requiring a source lookup. Mention it, reply to it, or address it as “AFL
bot” in the configured test group. Plain group banter and questions addressed to
someone else stay quiet. Short follow-ups such as “and the registration link” use
the same chat to resolve the topic.

Official facts still come only from currently accessible published knowledge or
current public Top100 activities. The model may resolve references and typos; it
cannot write an unsupported official answer. When evidence is missing, the bot asks
for an activity name, official link or clarification rather than silently stopping.

Routine unanswered questions no longer send admin DMs. Members can explicitly ask
“please connect me to an admin”; the bot forwards that request and acknowledges it
only when sending succeeds. Repeated requests by the same person in the same chat
are limited to one forwarding per 30 minutes per process (reset on restart).
Critical service alerts and the existing daily report still operate. No new
environment variables are needed. This section supersedes legacy unknown/silent
forwarding descriptions later in this file.

# Curated AFL knowledge library

The default mode now uses **published knowledge and public Top100 activities** for official answers. Ordinary group conversation helps resolve follow-up questions but does not automatically become official knowledge. The older Graphiti reply workflow described later in this file applies only when `KNOWLEDGE_LIBRARY_ENABLED=false`.

## Set up the knowledge feed

Pair WhatsApp yourself, configure your existing `ADMIN_PHONE`/`ADMIN_PHONES` (international digits) or `ADMIN_JIDS`, and retain `TEST_GROUP_JID` for member testing. Add these settings to the **bot service only** if overriding defaults:

```dotenv
KNOWLEDGE_LIBRARY_ENABLED=true
KNOWLEDGE_GROUP_JID=
KNOWLEDGE_DB_PATH=
```

An empty knowledge-group JID disables the group feed; authorized admin DMs still work. To use a private knowledge group, set its exact `...@g.us` JID. It can be separate from the member test group. Only senders on the bot's configured admin allowlist can contribute, even if WhatsApp grants other people group-admin status. Notify contributors that submissions are retained. Feed text/PDFs are stored as drafts, excluded from member conversation memory, and acknowledged by private DM to the submitting admin.

The blank DB path automatically places `knowledge.sqlite3` beside `WHATSAPP_SESSION_PATH`. In Docker that is `/app/data/knowledge.sqlite3` on the existing `bot_data` volume. This stores entries, PDF attachments and audit history across restarts. Back up the volume; redeployment alone is not a backup.

Drafting, reviewing and publishing require no model key. Answering requires `GROQ_API_KEY`. `OPENAI_API_KEY` is needed for Graphiti conversation memory, but **is not required for the curated library itself**. Both services keep their existing roles; nothing is added to Top100's database.

## Submit and publish

In an authorized admin DM, send a message in this form (replace the example with real, confirmed information):

```text
knowledge draft Mentoring guide | audience=members | effective=2026-11-01 | expires=2027-01-01
Mentoring sessions are held on the first Tuesday of each month.
Contact the programme office for scheduling assistance.
```

The bot returns an entry ID, metadata and a content preview. Review a long entry using `knowledge show ENTRY_ID 2` for page two and subsequent pages. Then send:

```text
knowledge publish ENTRY_ID
```

Plain admin-feed messages and PDFs also create drafts. They default to **admin_only**, and remain invisible to members even after publication. To classify a draft for member-facing answers, review it, send `knowledge audience ENTRY_ID members`, review the updated preview, then publish it. Use `knowledge list` and `knowledge list 2` to find entries, and `knowledge help` for command syntax.

Readable PDFs up to 10 MB are accepted in admin DMs/the configured feed; scanned PDFs need selectable text before submission. There is an 80,000-character content limit. Oversize/unreadable documents are rejected rather than silently truncated. A PDF caption can contain the `knowledge draft Title | audience=members | ...` header. Its original extracted text is retained; the bot does not generate a summary and silently publish that instead.

Knowledge submissions and document attachments posted in the member test group are excluded from bot memory in this mode. An admin receives a private instruction to resubmit in a DM/feed. If voice replies are enabled, approved excerpts retain their conditions and are accompanied by the original text and source links.

Legacy `ingest <text>` now creates a private draft. `ingest on` enables automatic drafts of non-question admin statements; `ingest off` disables that convenience. Neither command automatically publishes anything. Publishing is an explicit admin action; review the full content and audience before using it.

## Replace, expire or withdraw

Publish a new draft with `replaces=OLD_ENTRY_ID` to replace an existing published entry. The audience must match. A future effective date leaves the old version active until the scheduled switch. Only one successor can be published against a predecessor. Content cannot be edited after publication; the original remains available for audit. A new PDF version should be resubmitted with a caption specifying `replaces`.

`knowledge retire ENTRY_ID` withdraws an entry immediately. Expiry is automatic at the configured instant. Retired, expired and superseded entries cannot support replies or PDF downloads. Previous versions do not automatically come back when a successor expires/is retired. Dates such as `2026-11-01` mean midnight Lagos; precise ISO timestamps must include a timezone, for example `2026-11-01T09:00:00+01:00`.

## Answer behavior and limits

The bot retrieves only current published member entries for member groups/DMs. Admin-only entries are available in authenticated admin DMs, never in a group even when an admin asks. Public activity records remain read-only and are fetched when relevant, with the existing short API cache.

Groq selects exact excerpts; application code validates source IDs, literal text and sentence/field boundaries, then adds source titles, entry IDs and approved links. It rechecks access/lifecycle after the model call. Fabricated quotes, malformed output, an explicit source conflict, or missing evidence produce no factual reply. Existing silent-group/admin-notification behavior applies. Automatic Graphiti fact corrections are disabled in library mode. `/summary` describes chat history and reminders perform scheduling; these remain separate conversational skills, not official knowledge sources.

Document requests apply the same audience/lifecycle rules. If more than one accessible document exists, the bot offers a bounded list; use `document ENTRY_ID` to request one. It never sends the last unapproved/private PDF from an in-memory pool.

This reduces invented facts; it does not guarantee that an approved document is correct or that model-selected excerpts answer every question correctly. Retrieval uses keyword matches, so wording differences may cause an abstention. The publishing admin must resolve contradictory guides and assign expiry dates to temporary announcements. Keep the member test group restricted while reviewing real answers.

---

# Existing deployment and legacy reference

# Top100 AFL WhatsApp assistant

Headless Python service for Top100 Africa Future Leaders. It connects to
WhatsApp with **neonize**, uses recent conversation and Top100's public
activities to answer relevant questions, and keeps community memory in
Graphiti/FalkorDB. Group processing is disabled until `TEST_GROUP_JID` is set;
messages outside that allowlist are dropped before logging or ingestion.

The bot records every text message in its configured group. Tell members before
adding it to any group beyond the isolated test group.

This document covers local setup, **Dokploy deployment**, and direct Docker operations.

## What works today (build order step 6)

| Feature | Status |
| ------- | ------ |
| QR / session pairing | Working |
| Log allowlisted test-group text | Working |
| Admin DMs (`ping`, `status`, `recent`) | Working |
| Member DMs | Groq when Graphiti/recent chat is grounded; otherwise silent + admin notify. Private DMs are **not** ingested. |
| Question / @-mention in the test group | Groq from **public Top100 activity records + Graphiti facts + recent chat**; unknown stays silent (+ admin DM unless you asked) |
| Graphiti ingest + retrieval | Group text → FalkorDB episodes (`actor: content` + provenance in `source_description`); replies search with Graphiti hybrid + cross-encoder; low scores can be dropped |
| Top100 activities | Read-only public `GET /api/events` lookup for relevant activity questions; no Supabase key or private profile data |
| Reliability | Background queues, Groq/Graphiti retries, `event=heartbeat`, Compose `restart: unless-stopped`, systemd `Restart=always` |
| Groq model id | `openai/gpt-oss-120b` |

## Locked stack

| Layer | Choice |
| ----- | ------ |
| WhatsApp | `neonize==0.5.2` |
| LLM | Groq `openai/gpt-oss-120b` (same model as `gpt-oss-120b`) |
| Memory | `graphiti-core[falkordb]==0.30.2` + FalkorDB |
| Python | 3.11 via **uv** |
| Process | Docker Compose `restart: unless-stopped` |

Do not substitute libraries named in [AGENTS.md](AGENTS.md).

## Environment

Copy [`.env.example`](.env.example) to `.env`. Never commit `.env` or `data/session.db`.

| Variable | Purpose |
| -------- | ------- |
| `WHATSAPP_SESSION_PATH` | SQLite session (pair once, reuse) |
| `TEST_GROUP_JID` | Required `…@g.us` to process any group. Only the configured test group is read; unset means group processing is disabled. |
| `TOP100_API_BASE_URL` | Public activity API base URL; default `https://www.top100afl.com` |
| `LOG_LEVEL` | `INFO` (default) or `WARNING` |
| `DEBUG` | `true` / `false`. When true: DEBUG logs, file:line, skip reasons (`from_me`, `not_group`) |
| `ADMIN_PHONES` | Comma-separated E.164 digits, no `+`. Multiple admins. |
| `ADMIN_PHONE` | Single number; merged into the same allowlist |
| `FALKORDB_*` | Used from step 4. Compose sets `FALKORDB_HOST=falkordb` for the bot container. |
| `GRAPHITI_GROUP_ID` | Graph partition, default `top100_afl` |
| `GROQ_API_KEY` | Groq replies + Graphiti extraction |
| `OPENAI_API_KEY` | Graphiti embeddings/rerank only (required for step 4 ingest) |
| `GRAPHITI_ENABLED` | `true` (default) starts Graphiti when both keys are set |
| `HEARTBEAT_SECONDS` | Interval for `event=heartbeat` (minimum 10s) |
| `RETRIEVAL_MIN_FACTS` | Minimum Graphiti hits before a reply is treated as grounded |
| `RETRIEVAL_MIN_SCORE` | Drop Graphiti **cross-encoder** hits below this score (`0` keeps all ranked hits) |
| `QUEUE_MAXSIZE` | Cap on the WhatsApp worker and Graphiti ingest queues |

Admin DMs: allowlisted numbers (`ADMIN_PHONES` / `ADMIN_JIDS`) get commands plus grounded answers. WhatsApp may send the chat as `@lid`; the bot also checks `SenderAlt` / phone JID so LIDs still match. Member DMs use the same know / don't-know rule and are **not** written into Graphiti.

`status` reports `graphiti=on:retrieval` when FalkorDB ingest + search is live.

### Step 4–5 — FalkorDB + Graphiti ingest and retrieval

1. Set `GROQ_API_KEY`, `OPENAI_API_KEY`, `GROQ_MODEL=openai/gpt-oss-120b`.
2. Start FalkorDB with persistence (AOF + RDB via `REDIS_ARGS` in compose):

```powershell
docker compose up -d falkordb
```

On Ubuntu with `uv run top100-afl-bot` (not Docker for the bot), keep `FALKORDB_HOST=localhost`. Need ~2 GB extra RAM; skip FalkorDB on a 2 GB t4g or set `GRAPHITI_ENABLED=false`.

3. Restart the bot. Look for `event=graphiti_ready` then after a group text `event=graphiti_ingested name=<whatsapp-id>`.
4. Duplicate reconnects log `event=graphiti_skip_duplicate`.

After ingest, a question in the test group searches Graphiti (edge facts + episode text). Look for `event=graphiti_retrieved count=N`. Top100 activity facts are fetched separately when relevant. If Graphiti is connected and count is 0, the bot can still answer from a current matching public activity record; without either source or supporting recent context it stays silent (`event=retrieval_miss`).

If FalkorDB or keys are missing, the bot still runs WhatsApp + Groq (`event=graphiti_disabled` or `event=graphiti_start_failed`). Look for `event=heartbeat` about once a minute (`worker_queue`, `ingest_queue`, Graphiti/Groq flags).

Admin commands: `ping`, `status`, `help`, `recent` (last group lines this process), `stub known` (placeholder in-group reply to test send), `stub unknown` (default: silent + notify).

In the configured test group, a **question** or **@-mention** does **not** get a public “I don’t know”. The bot DMs each admin with chat, sender, message id, timestamp, and text. Banter is ingested only. If `TEST_GROUP_JID` is empty, all group messages are ignored.

## How to test locally (recommended before VPS)

### Prerequisites (Windows)

1. Python **3.11**
2. [uv](https://docs.astral.sh/uv/)
3. **FFmpeg** on `PATH` (`winget install FFmpeg`)
4. Docker Desktop (for FalkorDB / later full stack)

### Install and unit tests

```powershell
cd C:\Users\HP\Documents\Whatsapp_bot
uv sync --group dev
copy .env.example .env
uv run top100-afl-bot --check
uv run pytest -q
```

`--check` must print `event=check_ok` and must **not** connect to WhatsApp.

### Pair and watch ingestion

1. Put a **dedicated** WhatsApp number on the phone you will pair (not your personal daily driver if you can avoid it).
2. Add the linked number to the **test group only**. Do not add it to other groups during initial setup.
3. If the group JID is unknown, pair while `TEST_GROUP_JID` is blank. The service reports `event=group_ignored ... group=<jid>` without message text; set `TEST_GROUP_JID` to that JID and restart before testing replies.
4. Set `ADMIN_PHONES` to your number(s).
5. Optional: `DEBUG=true` while testing.

```powershell
uv run top100-afl-bot
```

Scan the terminal QR: **WhatsApp → Settings → Linked devices → Link a device**.

Send a text in the configured test group. You should see:

```text
event=group_message ingest=yes reply_eligible=true|false group=...@g.us sender=... id=... text=...
```

- `ingest=yes` on the test group = recording works.
- `event=group_ignored` for groups outside the configured allowlist or while it is blank. Ignored messages are not logged or ingested.
- Second process start should **not** show a QR (session file under `data/`).

Then in the **test group**, send a question (`when is the hackathon?`). The group should stay silent. Your admin DM should get an “Unknown question” notice. Logs: `event=admin_notified`. Send `recent` in the admin DM to see logged lines.

Banter (`lol`) should only produce `event=group_message`, no admin DM.

Ctrl+C stops the process. `DEBUG=true` also logs `event=message_skipped reason=from_me|empty_dm`.

Copy the test group JID into `.env`:

```env
TEST_GROUP_JID=120363xxxxxxxx@g.us
DEBUG=false
```

Restart the bot and confirm group events are ignored until `TEST_GROUP_JID` matches the test group.

## Dokploy deployment

Create a **new Dokploy project/service** named `AFL WhatsApp Bot` from this
repository's `paul/top100-afl-bot` branch. Deploy the existing Compose stack in
that project, with its own persistent `bot_data` and `falkordb_data` volumes.
Keep the FalkorDB port private; do not edit the existing Top100 project or its
services. Set the non-secret `TOP100_API_BASE_URL` to
`https://www.top100afl.com`. Leave `TEST_GROUP_JID` empty until the user pairs
the WhatsApp number and configures the test group; with it empty, the service
logs only ignored group JIDs and does not retain group messages. Add the bot
only to the test group, tell its members that text is recorded, and enter any
missing model/admin credentials directly into this new Dokploy service.

The QR scan and group JID configuration are user steps. A running container
without those steps is not a paired or operational WhatsApp bot.

## Docker Compose (bot + FalkorDB)

FalkorDB persistence: named volume mounted at `/data` and `/var/lib/falkordb/data`, AOF + RDB via `REDIS_ARGS` (`appendonly` + `--save`). Bot and DB use `restart: unless-stopped`. Logs go to stdout; Docker rotates json-file logs (10 MB × 5).

**Pairing:** QR in Docker is awkward. Prefer pairing **locally**, then copy `data/session.db` onto the server (or into the `bot_data` volume). If you pair on the VPS, attach a TTY and follow logs for the QR.

```powershell
docker compose up -d --build
docker compose logs -f bot
```

Useful commands:

```powershell
docker compose logs -f --tail=200 bot
docker compose exec bot uv run --no-dev top100-afl-bot --check
docker compose restart bot
docker compose ps
```

FalkorDB smoke test from the host (if `redis-cli` is installed):

```powershell
redis-cli -p 6379 ping
```

## Ubuntu EC2 (t4g / ARM, 2 GB) — pair and keep it running

This is the path we used: **no Docker until the WhatsApp session exists**. t4g is **arm64**. Do not start FalkorDB on 2 GB RAM.

Security group: **SSH only**. WhatsApp is outbound; do not open port 6379.

### CPU Credit Conservation (Standard / Non-Unlimited t4g)

On standard t4g instances without "Unlimited CPU Credits", CPU consumption that exceeds baseline (e.g. 20% on `t4g.small`) burns through your CPU credit balance. Once credits hit zero, AWS throttles CPU to baseline, causing noticeable lag.

The bot includes built-in safeguards to preserve CPU credits:
1. **TTS Audio Caching (SHA-256)**: `TTS_CACHE_ENABLED=true` caches synthesized voice responses in `data/tts_cache/`. Repeated phrases or frequent queries are served directly from disk with **0 CPU load** and **<10ms latency**.
2. **Batched Ingestion**: Messages are batched (`INGEST_BATCH_MAX_MESSAGES=15`, interval 30 mins) so Graphiti does not trigger CPU and token spikes on every incoming message.
3. **Native ARM64 Execution**: Always run native `linux/arm64` binaries and Docker containers to eliminate emulation overhead.

### Swap (required on 2 GB)

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile    # required before swapon
sudo swapon /swapfile
swapon --show
free -h
grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

If `swapon` says `read swap header failed`, you skipped `mkswap`.

### Packages, FFmpeg, uv

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev ffmpeg git curl ca-certificates tmux
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
echo 'source "$HOME/.local/bin/env"' >> ~/.bashrc
uv --version
```

Prefer the install script, not `snap install astral-uv`.

### Install the app and check config

Repo on the instance (clone or scp). `.env` already filled.

```bash
cd ~/Whatsapp_bot
uv python pin 3.11
uv sync
uv run top100-afl-bot --check
```

`--check` must print `event=check_ok`. Confirm `admin_count` and `test_group` look right.

FalkorDB user/password: **leave empty** (`FALKORDB_USERNAME=` / `FALKORDB_PASSWORD=`). Unused until step 4.

### First QR pair (foreground)

Add the bot number to the groups. Then:

```bash
cd ~/Whatsapp_bot
uv run top100-afl-bot
```

Scan the QR: **WhatsApp → Settings → Linked devices → Link a device**.

Success looks like:

- `event=paired user=…`
- `event=connected`
- `event=group_message ingest=yes …` after someone posts

`Got 515`, websocket EOF, history-sync 400, duplicate contacts, and occasional `no sender key` on first join are **normal**. Ignore unless **new** texts never appear as `event=group_message`.

`event=group_ignored` means the chat is not `TEST_GROUP_JID` or the setting is blank; the message body is not logged or ingested. Set the test-group JID and restart before testing replies.

Restart with `uv run top100-afl-bot` should **not** show a QR (`data/session.db` is the session).

### Keep it running: tmux (not Ctrl+B in a normal SSH shell)

If you run `uv run top100-afl-bot` in plain SSH, **closing the window or Ctrl+C stops the bot**. `^B` on screen means you are **not** in tmux.

Stop a foreground bot: **Ctrl+C**. If SSH is stuck, another session: `pkill -f top100-afl-bot`.

Then:

```bash
sudo apt-get install -y tmux
cd ~/Whatsapp_bot
tmux new -s bot
```

Green bar at the bottom = you are in tmux. Then start the bot:

```bash
uv run top100-afl-bot
```

Detach (bot keeps running): **Ctrl+B**, **release**, then **D**.  
PuTTY: **Ctrl+B**, `:`, type `detach`, Enter.

```bash
tmux ls                 # expect: bot: 1 windows
tmux attach -t bot      # watch logs again
```

`tmux new -s bot` then immediate `[detached]` creates an **empty** session. Attach and run `uv run top100-afl-bot` inside it, or:

```bash
tmux kill-session -t bot
cd ~/Whatsapp_bot
tmux new -s bot
uv run top100-afl-bot
```

### Debug logs (monitoring)

In `.env`:

```env
DEBUG=true
LOG_LEVEL=INFO
```

`DEBUG=true` forces DEBUG (skip reasons `from_me` / `empty_dm`, file:line). Restart the process after changing `.env` (Ctrl+C in tmux, then `uv run top100-afl-bot` again).

To review with a developer: `tmux attach -t bot`, copy a slice of log (especially `event=connected`, `event=group_message`, errors). Detach with Ctrl+B, D.

Turn debug off when you want quieter logs: `DEBUG=false`, restart.

## Deploy on a VPS (Linux) — Docker later

Use Ubuntu 22.04/24.04, a small dedicated number, and a non-root sudo user. On t4g, finish the **uv + tmux** pairing section first; do not `docker compose up` FalkorDB on 2 GB until Graphiti is needed.

### 1. Server baseline

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git ufw
sudo ufw allow OpenSSH
sudo ufw enable
```

Do **not** expose FalkorDB (`6379`) on the public internet. The compose file binds 6379 on the host; on a VPS either remove the `ports:` mapping for `falkordb` (recommended: only the `bot` network can reach it) or firewall it.

Safer compose change for production: delete the `falkordb.ports` section so Redis is internal-only. Keep `FALKORDB_HOST=falkordb` for the bot service.

### 2. Install Docker Engine

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
# log out and back in
docker --version
docker compose version
```

### 3. Clone and configure

```bash
git clone <YOUR_REPO_URL> /opt/top100-afl-bot
cd /opt/top100-afl-bot
cp .env.example .env
nano .env
```

Set at least:

```env
ADMIN_PHONES=2348xxxxxxxxx,2348yyyyyyyyy
TEST_GROUP_JID=120363xxxxxxxx@g.us
DEBUG=false
LOG_LEVEL=INFO
WHATSAPP_SESSION_PATH=/app/data/session.db
```

### 4. Session file

**Option A (recommended):** Pair on a laptop with `uv run top100-afl-bot`, stop the process, copy `data/session.db` (and `-wal`/`-shm` if present) to the VPS:

```bash
mkdir -p /opt/top100-afl-bot/data
# from your PC, after pairing:
scp data/session.db user@vps:/opt/top100-afl-bot/data/session.db
```

If you use named volume `bot_data`, copy into the volume after first `compose up`, or switch the bot volume to a bind mount:

```yaml
volumes:
  - /opt/top100-afl-bot/data:/app/data
```

**Option B:** `docker compose up bot` with `tty: true`, `docker compose logs -f bot`, scan QR on the phone. Re-pair if the QR times out.

### 5. Start

```bash
cd /opt/top100-afl-bot
docker compose up -d --build
docker compose logs -f bot
```

Confirm `event=connected` then send a group message and look for `event=group_message`.

### 6. Logging and debug

Production:

```env
DEBUG=false
LOG_LEVEL=INFO
```

Incident / developer:

```env
DEBUG=true
```

```bash
docker compose up -d bot
docker compose logs -f --tail=300 bot
```

Persist logs off-box later with a log shipper if you need it; stdout + Docker json-file is enough for v1.

### 7. Updates

```bash
cd /opt/top100-afl-bot
git pull
docker compose up -d --build
docker compose exec bot uv run --no-dev top100-afl-bot --check
```

Session volume is unchanged across rebuilds.

### 8. Crash restart

Compose already sets `restart: unless-stopped` on `bot` and `falkordb`. After a reboot:

```bash
docker compose -f /opt/top100-afl-bot/docker-compose.yml up -d
```

Optional systemd unit:

```ini
[Unit]
Description=Top100 AFL WhatsApp bot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/top100-afl-bot
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now top100-afl-bot.service
```

## Reply policy (later steps)

- In-group on **@-mention** or a **question** with grounded retrieval.
- Default: short. Longer only when the answer cannot fit in a few sentences.
- Unknown: **no group reply**; every admin in `ADMIN_PHONES` gets a DM (step 2).
- Group processing and replies are limited to `TEST_GROUP_JID`; with no test-group JID, all groups are ignored. The bot does not fetch member rosters or private profiles.

PDF ingest is **not** in v1.

## Troubleshooting

| Symptom | Action |
| ------- | ------ |
| QR every start | Session path not persisted; check volume / `data/session.db` |
| Business app says “Check your connection” after QR | Use the pinned Neonize 0.5.2 or newer tested release. Its WhatsMeow core handles `companion_reg_refresh`, which the previous 0.4.3.post0 pin ignored. Pairing still needs a live scan to confirm success. |
| Logs say `Login event: timeout` while the container stays running | The displayed QR is expired. Restart the bot container to begin another pairing window; keep the persistent session volume. |
| No group logs | Bot not in the group; or you are logging DMs (`DEBUG` skip `from_me` / `empty_dm`) |
| FFmpeg error | Install FFmpeg locally or rebuild the image |
| `uv sync` protobuf error | Known neonize vs graphiti-core conflict — keep both packages; do not drop either |
| FalkorDB connection (later) | In Docker use host `falkordb`, not `localhost` |

## Tests

```powershell
uv sync --group dev
uv run pytest -q
uv run top100-afl-bot --check
```
