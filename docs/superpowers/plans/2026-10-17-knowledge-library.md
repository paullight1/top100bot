# AFL Knowledge Library Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. The user's standing authorization approves plans and continuous native execution.

**Goal:** Add curated WhatsApp knowledge intake, publishing and source-constrained member answers.

**Architecture:** A persistent SQLite catalog owns content lifecycle and access. Focused command and answer modules integrate at Runtime and classify_message boundaries; Graphiti remains conversational memory.

**Tech Stack:** Existing Python 3.11, neonize, Groq, SQLite, PyMuPDF, Graphiti/FalkorDB.

**Spec:** docs/superpowers/specs/2026-10-17-knowledge-library-design.md

## Global Constraints
- No new dependencies; dedicated bot service only.
- No Top100 backend/schema/private profile changes.
- No unpaired WhatsApp session or unconfigured group scope expansion.
- Defaults: library enabled, admin_only drafts, knowledge group blank.
- Limit source content to 80,000 characters and PDF bytes to 10 MB.

## Review Focus
- Scheduled replacement must not hide the old version early or resurrect it after expiry.
- Non-admin knowledge-group messages and admin-only PDFs must never enter member context.
- A replay or concurrent replacement must not create duplicate active knowledge.
- Malformed LLM output, fabricated quotes, and a source retiring during generation must fail closed.
- Long or unreadable PDFs must not silently publish partial material.

### Task 1: Persistent library lifecycle
Files: src/unipod_bot/knowledge.py; tests/test_knowledge.py.
Interfaces: KnowledgeLibrary.create_draft/get/list_entries/publish/retire/search/active; KnowledgeEntry immutable data.
- [x] Write failing tests for publication, access, expiry, replacements, replay and restart persistence.
- [x] Run `.venv/bin/python -m pytest tests/test_knowledge.py -q` and confirm missing behavior.
- [x] Implement transactional catalog, audit, validation and bounded lexical excerpt search.
- [x] Run the focused tests; commit independently working lifecycle.

### Task 2: Intake and management
Files: knowledge_commands.py, incoming.py, config.py, runtime.py, .env.example, tests/test_knowledge_runtime.py.
Interfaces: handle_knowledge_command(library, text, actor, source_chat, source_message, attachment, filename) -> str; private feed job classification.
- [x] Write failing tests for sender/group isolation, admin draft commands, PDF lifecycle and defaults.
- [x] Add optional group config, exact sender gate, admin DM/file intake and private feed acknowledgements.
- [x] Redirect legacy ingest to drafts in library mode; feed bypasses all member logs/memory.
- [x] Verify replay handling, bad metadata, PDFs and authorization; commit.

### Task 3: Constrained retrieval and replies
Files: knowledge_answers.py, runtime.py, tests/test_knowledge_answers.py, existing legacy fixture settings.
Interfaces: grounded_answer(groq, library, question, context, activities, allow_admin) -> str|None.
- [x] Write failing tests for exact quote validation, unknown/conflict, access, expired/revoked sources, and PDF request filtering.
- [x] Retrieve only approved current candidates, validate model-selected exact evidence, attach sources, recheck active entries before returning.
- [x] Route member/admin known replies through the new boundary, disable unapproved Graphiti correction and old PDF pooling in library mode.
- [x] Verify focused and full suite; update legacy tests to opt into legacy mode where their contracts intentionally changed; commit.

### Task 4: Documentation and deployment
Files: README.md, AGENTS.md, spec and plan completion notes.
- [x] Document admin workflow, version replacement, privacy, environment defaults, and honest limitations.
- [x] Run full pytest, compile/config check, diff review; fix any regressions with focused tests.
- [ ] Push existing bot branch, inspect dedicated Dokploy compose source, redeploy, inspect service status/log metadata without revealing keys or QR.
- [ ] Report exact deployment outcome and remaining user credentials/pairing/group actions.
