# Unipod WhatsApp community bot

Headless Python service for the Unipod innovators community. It connects to
WhatsApp with **neonize**, records **all group text** the bot can see, and (in
later build steps) answers only when the answer is grounded. Unknown questions
stay silent in the group; allowlisted **admins** are notified by DM.

Members must be told that **all text in connected groups is recorded**.

This document covers **local testing** and **VPS/Docker deployment**.

## What works today (build order step 6)

| Feature | Status |
| ------- | ------ |
| QR / session pairing | Working |
| Log every group text | Working |
| Admin DMs (`ping`, `status`, `recent`) | Working |
| Member DMs | Groq when Graphiti/recent chat is grounded; otherwise silent + admin notify. Private DMs are **not** ingested. |
| Question / @-mention in the reply group | Groq from **Graphiti facts + recent chat**; unknown stays silent (+ admin DM unless you asked) |
| Graphiti ingest + retrieval | Group text → FalkorDB episodes (`actor: content` + provenance in `source_description`); replies search with Graphiti hybrid + cross-encoder; low scores can be dropped |
| Reliability | Background queues, Groq/Graphiti retries, `event=heartbeat`, Compose `restart: unless-stopped`, systemd `Restart=always` |
| Groq model id | `openai/gpt-oss-120b` |

## Locked stack

| Layer | Choice |
| ----- | ------ |
| WhatsApp | `neonize==0.4.3.post0` |
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
| `TEST_GROUP_JID` | Optional `…@g.us`. **Ingest still logs all groups.** Replies (step 2+) only in this group when set. |
| `LOG_LEVEL` | `INFO` (default) or `WARNING` |
| `DEBUG` | `true` / `false`. When true: DEBUG logs, file:line, skip reasons (`from_me`, `not_group`) |
| `ADMIN_PHONES` | Comma-separated E.164 digits, no `+`. Multiple admins. |
| `ADMIN_PHONE` | Single number; merged into the same allowlist |
| `FALKORDB_*` | Used from step 4. Compose sets `FALKORDB_HOST=falkordb` for the bot container. |
| `GRAPHITI_GROUP_ID` | Shared partition, default `unipod_innovators` (no colon) |
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

On Ubuntu with `uv run unipod-bot` (not Docker for the bot), keep `FALKORDB_HOST=localhost`. Need ~2 GB extra RAM; skip FalkorDB on a 2 GB t4g or set `GRAPHITI_ENABLED=false`.

3. Restart the bot. Look for `event=graphiti_ready` then after a group text `event=graphiti_ingested name=<whatsapp-id>`.
4. Duplicate reconnects log `event=graphiti_skip_duplicate`.

After ingest, a question in the reply group searches Graphiti (edge facts + episode text). Look for `event=graphiti_retrieved count=N`. If Graphiti is connected and count is 0, the bot stays silent (`event=retrieval_miss`) even if this process already logged other lines. Recent chat is used to **search** and to **word** a grounded answer, not to skip retrieval. Redeploy this code, then ask `When is hackathon` without restating the date — the Tuesday episode from earlier should come back as a community fact.

If FalkorDB or keys are missing, the bot still runs WhatsApp + Groq (`event=graphiti_disabled` or `event=graphiti_start_failed`). Look for `event=heartbeat` about once a minute (`worker_queue`, `ingest_queue`, Graphiti/Groq flags).

Admin commands: `ping`, `status`, `help`, `recent` (last group lines this process), `stub known` (placeholder in-group reply to test send), `stub unknown` (default: silent + notify).

In the reply group (`TEST_GROUP_JID`, or every group if unset): a **question** or **@-mention** does **not** get a public “I don’t know”. The bot DMs each admin with chat, sender, message id, timestamp, and text. Banter is ingested only. Set `TEST_GROUP_JID` before testing so community groups do not fan out notifies.

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
uv run unipod-bot --check
uv run pytest -q
```

`--check` must print `event=check_ok` and must **not** connect to WhatsApp.

### Pair and watch ingestion

1. Put a **dedicated** WhatsApp number on the phone you will pair (not your personal daily driver if you can avoid it).
2. Add that number to **all community groups** you want recorded, plus one **test group** for future replies.
3. In `.env` leave `TEST_GROUP_JID` empty for the first messages, or fill it after you copy a JID from logs.
4. Set `ADMIN_PHONES` to your number(s).
5. Optional: `DEBUG=true` while testing.

```powershell
uv run unipod-bot
```

Scan the terminal QR: **WhatsApp → Settings → Linked devices → Link a device**.

Send a text in **any** connected group. You should see:

```text
event=group_message ingest=yes reply_eligible=true|false group=...@g.us sender=... id=... text=...
```

- `ingest=yes` on every group = recording works.
- `reply_eligible=false` on community groups when `TEST_GROUP_JID` is set to the test group only.
- Second process start should **not** show a QR (session file under `data/`).

Then in the **test group**, send a question (`when is the hackathon?`). The group should stay silent. Your admin DM should get an “Unknown question” notice. Logs: `event=admin_notified`. Send `recent` in the admin DM to see logged lines.

Banter (`lol`) should only produce `event=group_message`, no admin DM.

Ctrl+C stops the process. `DEBUG=true` also logs `event=message_skipped reason=from_me|empty_dm`.

Copy the test group JID into `.env`:

```env
TEST_GROUP_JID=120363xxxxxxxx@g.us
DEBUG=false
```

Restart the bot and confirm community groups still ingest, with `reply_eligible=false`.

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
docker compose exec bot uv run --no-dev unipod-bot --check
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
uv run unipod-bot --check
```

`--check` must print `event=check_ok`. Confirm `admin_count` and `test_group` look right.

FalkorDB user/password: **leave empty** (`FALKORDB_USERNAME=` / `FALKORDB_PASSWORD=`). Unused until step 4.

### First QR pair (foreground)

Add the bot number to the groups. Then:

```bash
cd ~/Whatsapp_bot
uv run unipod-bot
```

Scan the QR: **WhatsApp → Settings → Linked devices → Link a device**.

Success looks like:

- `event=paired user=…`
- `event=connected`
- `event=group_message ingest=yes …` after someone posts

`Got 515`, websocket EOF, history-sync 400, duplicate contacts, and occasional `no sender key` on first join are **normal**. Ignore unless **new** texts never appear as `event=group_message`.

`reply_eligible=false` means the chat is not `TEST_GROUP_JID`. Ingest still runs. Send text in the test group to see `reply_eligible=true`.

Restart with `uv run unipod-bot` should **not** show a QR (`data/session.db` is the session).

### Keep it running: tmux (not Ctrl+B in a normal SSH shell)

If you run `uv run unipod-bot` in plain SSH, **closing the window or Ctrl+C stops the bot**. `^B` on screen means you are **not** in tmux.

Stop a foreground bot: **Ctrl+C**. If SSH is stuck, another session: `pkill -f unipod-bot`.

Then:

```bash
sudo apt-get install -y tmux
cd ~/Whatsapp_bot
tmux new -s bot
```

Green bar at the bottom = you are in tmux. Then start the bot:

```bash
uv run unipod-bot
```

Detach (bot keeps running): **Ctrl+B**, **release**, then **D**.  
PuTTY: **Ctrl+B**, `:`, type `detach`, Enter.

```bash
tmux ls                 # expect: bot: 1 windows
tmux attach -t bot      # watch logs again
```

`tmux new -s bot` then immediate `[detached]` creates an **empty** session. Attach and run `uv run unipod-bot` inside it, or:

```bash
tmux kill-session -t bot
cd ~/Whatsapp_bot
tmux new -s bot
uv run unipod-bot
```

### Debug logs (monitoring)

In `.env`:

```env
DEBUG=true
LOG_LEVEL=INFO
```

`DEBUG=true` forces DEBUG (skip reasons `from_me` / `empty_dm`, file:line). Restart the process after changing `.env` (Ctrl+C in tmux, then `uv run unipod-bot` again).

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
git clone <YOUR_REPO_URL> /opt/unipod-bot
cd /opt/unipod-bot
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

**Option A (recommended):** Pair on a laptop with `uv run unipod-bot`, stop the process, copy `data/session.db` (and `-wal`/`-shm` if present) to the VPS:

```bash
mkdir -p /opt/unipod-bot/data
# from your PC, after pairing:
scp data/session.db user@vps:/opt/unipod-bot/data/session.db
```

If you use named volume `bot_data`, copy into the volume after first `compose up`, or switch the bot volume to a bind mount:

```yaml
volumes:
  - /opt/unipod-bot/data:/app/data
```

**Option B:** `docker compose up bot` with `tty: true`, `docker compose logs -f bot`, scan QR on the phone. Re-pair if the QR times out.

### 5. Start

```bash
cd /opt/unipod-bot
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
cd /opt/unipod-bot
git pull
docker compose up -d --build
docker compose exec bot uv run --no-dev unipod-bot --check
```

Session volume is unchanged across rebuilds.

### 8. Crash restart

Compose already sets `restart: unless-stopped` on `bot` and `falkordb`. After a reboot:

```bash
docker compose -f /opt/unipod-bot/docker-compose.yml up -d
```

Optional systemd unit:

```ini
[Unit]
Description=Unipod WhatsApp bot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/unipod-bot
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now unipod-bot.service
```

## Reply policy (later steps)

- In-group on **@-mention** or a **question** with grounded retrieval.
- Default: short. Longer only when the answer cannot fit in a few sentences.
- Unknown: **no group reply**; every admin in `ADMIN_PHONES` gets a DM (step 2).
- While `TEST_GROUP_JID` is set, **do not reply** outside that group (ingest still runs everywhere).

PDF ingest is **not** in v1.

## Troubleshooting

| Symptom | Action |
| ------- | ------ |
| QR every start | Session path not persisted; check volume / `data/session.db` |
| No group logs | Bot not in the group; or you are logging DMs (`DEBUG` skip `from_me` / `empty_dm`) |
| FFmpeg error | Install FFmpeg locally or rebuild the image |
| `uv sync` protobuf error | Known neonize vs graphiti-core conflict — keep both packages; do not drop either |
| FalkorDB connection (later) | In Docker use host `falkordb`, not `localhost` |

## Tests

```powershell
uv sync --group dev
uv run pytest -q
uv run unipod-bot --check
```
