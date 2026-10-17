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
- [ ] Bot admin domain/API: write failing lifecycle, auth and delivery-safety tests;
  implement admin_api.py and persistent announcements.py. Reuse KnowledgeLibrary
  and PDF extraction. Integrate optional listener lifecycle into main.py/config.py.
- [ ] Top100 bridge: fixed operation validation plus same-origin/requireAdmin gate;
  server-only service URL/key, timeout and safe errors. Add focused route tests.
- [ ] Admin workspace: knowledge draft/review/publish/retire, PDF input, announcement
  draft/preview/explicit-send and history. Add navigation; preserve unrelated edits
  using /Users/MAC/.codex/worktrees/afl-bot-admin/top100AFL.
- [ ] Verify and document: run relevant tests/typechecks, review privacy boundaries,
  document private network and environment setup, commit separate bot/app changes.
  Deploy completed reply behavior. Enable admin bridge only after safe credentials
  and reachability are configured; never send a real announcement for a test.
