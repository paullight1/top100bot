"""Graphiti + FalkorDB long-term memory (ingest + retrieval-gated search).

Episode contract:
- group_id: shared community partition (GRAPHITI_GROUP_ID). Colons are stripped;
  Graphiti rejects them, so the default is top100_afl.
- FalkorDB database name matches group_id so Graphiti does not clone a second graph.
- Episode name: WhatsApp message id (idempotent re-ingest on reconnect).
- body: Graphiti EpisodeType.message format (`actor: content`).
- provenance: WhatsApp group JID, sender, message id, timestamp in source_description.
- uuid: Graphiti 0.30 looks up uuid rather than assigning it on create, so the
  WhatsApp id is stored as episode name (equivalent reference key).

Ingest runs on the worker task, never inside the neonize MessageEv handler.
Search (edges + episode text) is used for retrieval-gated replies.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from unipod_bot.config import Settings, normalize_graphiti_group_id
from unipod_bot.groq_client import is_question_fact
from unipod_bot.groq_gate import groq_ingest
from unipod_bot.ingest import (
    EpisodeSource,
    batched_episode_body,
    batched_episode_name,
    batched_source_description,
    episode_body,
    episode_name,
    episode_source_description,
    parse_iso_time,
    reply_episode_body,
    reply_episode_name,
    reply_source_description,
)

logger = logging.getLogger("unipod_bot")


def _retry_ingest(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return getattr(exc, "status_code", None) in {408, 409, 429, 500, 502, 503, 504}


COMMUNITY_EXTRACTION_INSTRUCTIONS = (
    "Extract key entities and relationships for the Top100 AFL test-group conversation:\n"
    "- Events & Hackathons: Extract event names, dates, times, deadlines, rules, and themes. "
    "Use edge 'HAS_DEADLINE' or 'OCCURS_ON' to link events to their dates/deadlines.\n"
    "- People & Roles: Extract member handles (@phone or name), roles, and skills. "
    "Use edge 'LOOKING_FOR' to link team seekers to their desired roles or skills. "
    "Link team seekers to events with 'PARTICIPATES_IN'.\n"
    "- Resources & Documents: Extract shared documents, PDFs, guides, links, and code repositories. "
    "Link senders to resources with 'SHARED_RESOURCE' and link resources to events with 'RESOURCES_FOR'.\n"
    "- Announcements & Policies: Extract official announcements, contacts, and rules.\n"
    "Keep entity names concise and canonical (e.g. '@254XXXXXXXXX' for users, full event name for events)."
)


class GraphitiStore:
    def __init__(self, settings: Settings, pool: "GroqKeyPool | None" = None) -> None:
        self._settings = settings
        self._group_id = normalize_graphiti_group_id(settings.graphiti_group_id)
        self._graphiti = None
        self._seen_names: set[str] = set()
        # Key pool for rotation (can be None if using single key for backwards compat)
        from unipod_bot.groq_pool import GroqKeyPool
        if pool is not None:
            self._pool = pool
        elif settings.groq_api_keys:
            self._pool = GroqKeyPool(list(settings.groq_api_keys))
        else:
            self._pool = None

    async def start(self) -> None:
        os.environ.setdefault("SEMAPHORE_LIMIT", str(self._settings.semaphore_limit))

        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig, ModelSize
        from graphiti_core.llm_client.errors import RateLimitError as GraphitiRateLimit
        from graphiti_core.llm_client.groq_client import DEFAULT_MAX_TOKENS as GROQ_MAX_TOKENS
        from graphiti_core.llm_client.groq_client import GroqClient
        from graphiti_core.prompts.models import Message
        from groq.types.chat import ChatCompletionMessageParam
        from pydantic import BaseModel
        import groq as groq_sdk
        import json

        # Capture pool reference for use in the inner class
        _pool = self._pool

        class ExtractGroqClient(GroqClient):
            """Groq GPT-OSS: max_completion_tokens + low reasoning (JSON extraction).

            Uses the key pool for rotation on 429 rate limits.
            """

            def _extract_retry_after(self, exc: BaseException) -> float:
                resp = getattr(exc, "response", None)
                if resp is not None:
                    headers = getattr(resp, "headers", {})
                    raw = headers.get("retry-after")
                    if raw:
                        try:
                            return float(raw)
                        except (ValueError, TypeError):
                            pass
                return 60.0

            async def _generate_response(
                self,
                messages: list[Message],
                response_model: type[BaseModel] | None = None,
                max_tokens: int = GROQ_MAX_TOKENS,
                model_size: ModelSize = ModelSize.medium,
            ) -> dict:
                msgs: list[ChatCompletionMessageParam] = []
                for m in messages:
                    if m.role == "user":
                        msgs.append({"role": "user", "content": m.content})
                    elif m.role == "system":
                        msgs.append({"role": "system", "content": m.content})

                # Short-circuit if pool is paused (all keys exhausted for the day)
                if _pool is not None and _pool.is_paused:
                    raise GraphitiRateLimit("Groq pool paused until daily reset.")

                # Inner loop: try each available key immediately on 429.
                max_attempts = _pool.total if _pool is not None else 1
                for _attempt in range(max_attempts):
                    if _pool is not None:
                        key = _pool.get_key()
                        from groq import AsyncGroq as _AsyncGroq
                        client = _AsyncGroq(
                            api_key=key,
                            max_retries=0,
                            timeout=self._settings.groq_timeout_seconds
                            if hasattr(self, "_settings")
                            else 45.0,
                        )
                    else:
                        key = None
                        client = self.client

                    model_name = (self.model or "openai/gpt-oss-120b").lower()
                    is_reasoning = any(
                        x in model_name for x in ("oss", "r1", "o1", "o3", "reasoning")
                    )
                    call_kwargs: dict[str, Any] = {
                        "model": self.model or "openai/gpt-oss-120b",
                        "messages": msgs,
                        "temperature": self.temperature,
                        "max_completion_tokens": max_tokens or self.max_tokens,
                        "response_format": {"type": "json_object"},
                    }
                    if is_reasoning:
                        call_kwargs["reasoning_effort"] = "low"

                    try:
                        async with groq_ingest():
                            response = await client.chat.completions.create(**call_kwargs)
                        result = response.choices[0].message.content or ""
                        return json.loads(result)
                    except groq_sdk.RateLimitError as e:
                        if key is not None and _pool is not None:
                            retry_after = self._extract_retry_after(e)
                            _pool.mark_rate_limited(key, retry_after)
                            logger.warning(
                                "event=graphiti_groq_429 key=%s… retry_after=%.0f rotating_immediately",
                                key[:8],
                                retry_after,
                            )
                            continue  # immediately try next key
                        raise GraphitiRateLimit from e

                # All keys exhausted — park pool until daily reset
                if _pool is not None:
                    _pool.pause_until_reset()
                raise GraphitiRateLimit("All Groq API keys hit rate limits. Pool paused until midnight UTC.")

        if (
            self._settings.falkordb_database
            and self._settings.falkordb_database != self._group_id
        ):
            logger.info(
                "event=graphiti_database_alias configured=%s using_group_id=%s",
                self._settings.falkordb_database,
                self._group_id,
            )

        driver = FalkorDriver(
            host=self._settings.falkordb_host,
            port=self._settings.falkordb_port,
            username=self._settings.falkordb_username,
            password=self._settings.falkordb_password,
            database=self._group_id,
        )
        llm = ExtractGroqClient(
            config=LLMConfig(
                api_key=self._settings.groq_api_key or "pool-managed",
                model=self._settings.groq_model,
                small_model=self._settings.groq_model,
            )
        )
        llm._settings = self._settings
        try:
            from groq import AsyncGroq

            llm.client = AsyncGroq(
                api_key=self._settings.groq_api_key or (self._pool.peek_key() if self._pool else ""),
                max_retries=0,
                timeout=self._settings.groq_timeout_seconds,
            )
        except Exception:
            logger.exception("event=graphiti_groq_client_wrap_failed")
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=self._settings.openai_api_key,
                embedding_model=self._settings.openai_embedding_model,
            )
        )
        reranker = OpenAIRerankerClient(
            config=LLMConfig(
                api_key=self._settings.openai_api_key,
                model="gpt-4o-mini",
            )
        )
        self._graphiti = Graphiti(
            graph_driver=driver,
            llm_client=llm,
            embedder=embedder,
            cross_encoder=reranker,
            max_coroutines=self._settings.semaphore_limit,
        )
        await self._graphiti.build_indices_and_constraints()
        logger.info(
            "event=graphiti_ready host=%s:%s db=%s group_id=%s",
            self._settings.falkordb_host,
            self._settings.falkordb_port,
            self._group_id,
            self._group_id,
        )

    async def close(self) -> None:
        if self._graphiti is not None:
            await self._graphiti.close()
            self._graphiti = None

    async def ingest_message(self, msg: EpisodeSource) -> bool:
        name = episode_name(msg.message_id)
        if self._graphiti is None or not (msg.text or "").strip() or not name:
            return False
        if await self._already_ingested(name):
            logger.info("event=graphiti_skip_duplicate name=%s", name)
            return False
        await self._add_episode(
            name=name,
            body=episode_body(msg),
            source_description=episode_source_description(msg),
            reference_time=parse_iso_time(msg.iso_time),
        )
        self._seen_names.add(name)
        logger.info("event=graphiti_ingested name=%s chat=%s", name, msg.chat_jid)
        return True

    async def ingest_reply(self, msg: EpisodeSource, reply: str) -> bool:
        name = reply_episode_name(msg.message_id)
        if self._graphiti is None or not (reply or "").strip():
            return False
        if await self._already_ingested(name):
            logger.info("event=graphiti_skip_duplicate name=%s", name)
            return False
        await self._add_episode(
            name=name,
            body=reply_episode_body(msg, reply),
            source_description=reply_source_description(msg),
            reference_time=datetime.now(timezone.utc),
        )
        self._seen_names.add(name)
        logger.info("event=graphiti_ingested_reply name=%s chat=%s", name, msg.chat_jid)
        return True

    # Max messages per sub-batch to stay under Groq's 8,000 TPM limit.
    # Entity extraction prompts with schema + deduplication candidates overhead easily exceed 8,000 tokens when batch is > 5.
    _SUB_BATCH_SIZE: int = 5

    async def ingest_batch(self, messages: list) -> bool:
        """Ingest a batch of buffered messages as Graphiti episodes.

        Large batches are split into sub-batches to avoid exceeding the
        Groq model's token-per-minute limit (8,000 TPM on free tier).
        Each sub-batch becomes one ``add_episode()`` call (≈4 Groq calls).
        """
        if self._graphiti is None or not messages:
            return False

        # Filter out already-ingested messages
        fresh = [m for m in messages if m.message_id not in self._seen_names]
        if not fresh:
            logger.info(
                "event=graphiti_batch_skip_all_duplicates count=%s",
                len(messages),
            )
            return False

        # Split into sub-batches to avoid 413 Payload Too Large
        ingested = 0
        for i in range(0, len(fresh), self._SUB_BATCH_SIZE):
            chunk = fresh[i : i + self._SUB_BATCH_SIZE]
            name = batched_episode_name(chunk[0].message_id, len(chunk))
            body = batched_episode_body(chunk)
            source_desc = batched_source_description(chunk)
            ref_time = parse_iso_time(chunk[0].iso_time)

            await self._add_episode(
                name=name,
                body=body,
                source_description=source_desc,
                reference_time=ref_time,
            )

            # Mark individual message IDs as ingested
            for m in chunk:
                self._seen_names.add(m.message_id)
            ingested += len(chunk)

        logger.info(
            "event=graphiti_batch_ingested sub_batches=%s messages=%s groups=%s",
            (len(fresh) + self._SUB_BATCH_SIZE - 1) // self._SUB_BATCH_SIZE,
            ingested,
            len(set(m.chat_jid for m in fresh)),
        )
        return True

    async def search_facts(self, query: str) -> list[str]:
        """Community facts from Graphiti edges, episodes, and node summaries.

        Facts include sender attribution where available, formatted as:
        '[from @sender_jid] fact text' so the LLM can cite sources.
        """
        if self._graphiti is None or not query.strip():
            return []
        results = await self._search(query.strip())
        min_score = self._settings.retrieval_min_score
        facts: list[str] = []
        seen: set[str] = set()

        def _extract_sender(source_desc: str) -> str:
            """Extract sender= value from episode source_description."""
            if not source_desc:
                return ""
            for part in source_desc.split():
                if part.startswith("sender="):
                    val = part.split("=", 1)[1]
                    phone = "".join(ch for ch in val.split("@")[0] if ch.isdigit())
                    return phone or val
            return ""

        def _extract_source_group(source_desc: str) -> str:
            """Extract source_jid= value from episode source_description."""
            if not source_desc:
                return ""
            for part in source_desc.split():
                if part.startswith("source_jid="):
                    return part.split("=", 1)[1]
            return ""

        def add(text: str, sender: str = "", source_group: str = "") -> None:
            cleaned = " ".join((text or "").split())
            key = cleaned.lower()
            if not cleaned or key in seen:
                return
            seen.add(key)
            tag_parts: list[str] = []
            if sender:
                s_tag = sender if sender.startswith("@") else f"@{sender}"
                tag_parts.append(s_tag)
            if source_group:
                tag_parts.append(f"in group {source_group}")
            if tag_parts:
                facts.append(f"[from {' '.join(tag_parts)}] {cleaned}")
            else:
                facts.append(cleaned)

        def passes(scores: list[float] | None, index: int) -> bool:
            if not scores or index >= len(scores):
                return True
            return float(scores[index]) >= min_score

        edges = list(getattr(results, "edges", None) or [])
        edge_scores = list(getattr(results, "edge_reranker_scores", None) or [])
        for i, edge in enumerate(edges):
            if not passes(edge_scores, i):
                continue
            fact = str(getattr(edge, "fact", "") or "")
            if not is_question_fact(fact):
                # Edges don't carry source_description directly; add without sender
                add(fact)

        episodes = list(getattr(results, "episodes", None) or [])
        episode_scores = list(getattr(results, "episode_reranker_scores", None) or [])
        for i, episode in enumerate(episodes):
            if not passes(episode_scores, i):
                continue
            content = (getattr(episode, "content", None) or "").strip()
            if not content:
                continue
            first = content.split("\n", 1)[0].strip()
            if is_question_fact(first):
                continue
            source_desc = getattr(episode, "source_description", "") or ""
            sender = _extract_sender(source_desc)
            source_group = _extract_source_group(source_desc)
            add(first, sender=sender, source_group=source_group)

        nodes = list(getattr(results, "nodes", None) or [])
        node_scores = list(getattr(results, "node_reranker_scores", None) or [])
        for i, node in enumerate(nodes):
            if not passes(node_scores, i):
                continue
            name = (getattr(node, "name", None) or "").strip()
            summary = (getattr(node, "summary", None) or "").strip()
            if summary:
                add(f"{name}: {summary}" if name else summary)
            elif name and "@" not in name:
                add(name)
        limit = getattr(self._settings, "retrieval_num_results", 15)
        return facts[:limit]

    @retry(
        retry=retry_if_exception(_retry_ingest),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _search(self, query: str):
        from graphiti_core.search.search_config_recipes import (
            COMBINED_HYBRID_SEARCH_RRF,
        )

        assert self._graphiti is not None
        timeout = max(self._settings.graphiti_search_timeout_seconds, 5.0)
        return await asyncio.wait_for(
            self._graphiti.search_(
                query,
                config=COMBINED_HYBRID_SEARCH_RRF,
                group_ids=[self._group_id],
            ),
            timeout=timeout,
        )

    def is_grounded(self, facts: list[str]) -> bool:
        return len(facts) >= self._settings.retrieval_min_facts

    async def _already_ingested(self, name: str) -> bool:
        if name in self._seen_names:
            return True
        if self._graphiti is None:
            return False
        try:
            result = await self._graphiti.driver.execute_query(
                "MATCH (e:Episodic {name: $name, group_id: $group_id}) "
                "RETURN e.uuid AS uuid LIMIT 1",
                name=name,
                group_id=self._group_id,
            )
        except Exception:
            logger.exception("event=graphiti_duplicate_check_failed name=%s", name)
            return False
        records = result[0] if result else None
        exists = bool(records)
        if exists:
            self._seen_names.add(name)
        return exists

    @retry(
        retry=retry_if_exception(_retry_ingest),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _add_episode(
        self,
        *,
        name: str,
        body: str,
        source_description: str,
        reference_time: datetime,
    ) -> None:
        from graphiti_core.nodes import EpisodeType

        assert self._graphiti is not None
        ts = (
            reference_time
            if reference_time.tzinfo
            else reference_time.replace(tzinfo=timezone.utc)
        )
        await self._graphiti.add_episode(
            name=name,
            episode_body=body,
            source_description=source_description,
            reference_time=ts,
            source=EpisodeType.message,
            group_id=self._group_id,
            custom_extraction_instructions=COMMUNITY_EXTRACTION_INSTRUCTIONS,
        )

    async def get_episodes_in_range(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> list[str]:
        """Retrieve episodic texts from FalkorDB that fall within the specified time range."""
        if self._graphiti is None:
            return []
        start_iso = start_time.isoformat()
        end_iso = end_time.isoformat()
        try:
            result = await self._graphiti.driver.execute_query(
                "MATCH (e:Episodic {group_id: $group_id}) "
                "WHERE e.valid_at >= $start_iso AND e.valid_at <= $end_iso "
                "RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content "
                "ORDER BY e.valid_at ASC",
                group_id=self._group_id,
                start_iso=start_iso,
                end_iso=end_iso,
            )
            records = result[0] if result else []
            return [str(r["content"]) for r in records if r and r.get("content")]
        except Exception:
            logger.exception("event=graphiti_get_episodes_in_range_failed")
            return []

