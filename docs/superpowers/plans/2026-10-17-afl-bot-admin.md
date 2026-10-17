# AFL bot admin integration implementation plan

Execution: inline using executing-plans; user has preapproved plans.

**Goal:** Administrators manage published bot knowledge and explicitly send reviewed
announcements through the existing Top100 admin interface.
**Spec:** ../specs/2026-10-17-afl-bot-admin-design.md
**Architecture:** Optional authenticated stdlib Python HTTP service dispatches onto
the bot asyncio loop; a Top100 server route applies existing admin auth and proxies
only fixed operations; a client workspace manages drafts and preview/send actions.
**Stack:** Existing Python/SQLite/neonize/Groq; Next.js/React/current admin components.

## Tasks
- [x] Bot admin domain/API: write failing lifecycle, auth and delivery-safety tests;
  implement admin_api.py and persistent announcements.py. Reuse KnowledgeLibrary
  and PDF extraction. Integrate optional listener lifecycle into main.py/config.py.
- [x] Top100 bridge: fixed operation validation plus same-origin/requireAdmin gate;
  server-only service URL/key, timeout and safe errors. Add focused route tests.
- [x] Admin workspace: knowledge draft/review/publish/retire, PDF input, announcement
  draft/preview/explicit-send and history. Add navigation; preserve unrelated edits
  using /Users/MAC/.codex/worktrees/afl-bot-admin/top100AFL.
- [x] Verify and document: run relevant tests/typechecks, review privacy boundaries,
  document private network and environment setup, commit separate bot/app changes.
  Deploy completed reply behavior. Enable admin bridge only after safe credentials
  and reachability are configured; never send a real announcement for a test.

## Execution ledger

- Conversation behavior delivered in 618c691; 359 bot tests passed before admin work.
- Bot API: auth, private drafts, publication/audience lifecycle, announcement preview,
  atomic send claim, actor/target/expiry checks, uncertain delivery and HTTP transport
  tests passed. Whole bot suite: 367 passed before final review.
- Top100 bridge/workspace implemented in isolated branch paul/afl-bot-admin; route and
  initial-render tests passed, TypeScript passed, touched production files lint clean.
- Browser fixture (no production sends) verified private draft save, reviewed publish,
  preview/checkbox send gates and simulated delivery. Mobile width 390 has no overflow.
- Ruling: activation remains disabled until shared server credential and private
  network reachability are configured. This avoids exposing private knowledge merely
  to demonstrate UI completion. Existing Top100 checkout edits remain untouched.
- Independent review findings addressed: private announcement DB/sidecars, WhatsApp
  metadata redaction, content-free ingestion log, DM-only private alerts, broader explicit
  human-help wording, exact-target readiness and literal announcement text.
- Final verification: 374 bot tests; 21 web tests; TypeScript and targeted lint clean.
- Top100 admin workspace is in draft PR #6. Private bridge activation remains pending
  operator credentials, approved network access and application rollout.
