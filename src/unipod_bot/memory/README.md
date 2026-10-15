# Memory (Graphiti + FalkorDB)

Only text from the configured test group is ingested as an episode. Replies search edges, episode text,
and node summaries in the shared `GRAPHITI_GROUP_ID` partition, then Groq
answers only when Graphiti retrieval is grounded. Recent chat is prompt context
after a hit, not a substitute for retrieval. Admin DMs may still use the recent
group log when Graphiti misses.

## Episode contract

- `group_id`: shared community partition (`GRAPHITI_GROUP_ID`, default
  `top100_afl`). Graphiti rejects `:`.
- FalkorDB graph / database name is that same `group_id`.
- Episode **body**: Graphiti `actor: content` (`sender_jid: text`).
- Provenance (group JID, sender, message id, timestamp) is in
  `source_description`.
- Episode **name**: WhatsApp message id (idempotent re-ingest after reconnect).
- `source`: Graphiti `EpisodeType.message`.
- Calls run on a **background worker**, never inside the raw neonize `MessageEv`
  handler.

Enable with `GROQ_API_KEY` (entity extraction) and `OPENAI_API_KEY` (embeddings).
Replies stay on Groq. Set `GRAPHITI_ENABLED=false` to keep the bot running
without FalkorDB.
