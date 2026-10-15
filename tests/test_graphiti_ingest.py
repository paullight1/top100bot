import asyncio
from pathlib import Path
from types import SimpleNamespace

from unipod_bot.config import Settings
from unipod_bot.groq_client import retrieval_query
from unipod_bot.ingest import (
    episode_body,
    episode_name,
    episode_source_description,
    reply_episode_body,
    reply_episode_name,
    reply_source_description,
)
from unipod_bot.incoming import IncomingGroupMessage, IncomingUserDm
from unipod_bot.memory.graphiti_store import GraphitiStore
from unipod_bot.runtime import Runtime


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    async def send_message(self, to, text):
        self.sent.append((to, text))


class FakeGroq:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.text


def _settings(**kwargs) -> Settings:
    # Translate old groq_api_key= to new groq_api_keys= for backward compat
    if "groq_api_key" in kwargs:
        key = kwargs.pop("groq_api_key")
        kwargs.setdefault("groq_api_keys", (key,) if key else ())
    defaults = dict(
        whatsapp_session_path=Path("data/session.db"),
        test_group_jid="120363test@g.us",
        debug=False,
        log_level=20,
        falkordb_host="localhost",
        falkordb_port=6379,
        falkordb_database="default_db",
        falkordb_username=None,
        falkordb_password=None,
        graphiti_group_id="top100_afl",
        admin_phones=("2348011111111",),
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _group(**kwargs) -> IncomingGroupMessage:
    base = dict(
        chat_jid="120363test@g.us",
        sender_jid="2348099999999@s.whatsapp.net",
        message_id="MID1",
        text="Hackathon is monday",
        is_reply_group=True,
        iso_time="2026-09-20T05:42:39+00:00",
        mentioned_users=(),
        chat_obj=SimpleNamespace(User="120363test", Server="g.us"),
    )
    base.update(kwargs)
    return IncomingGroupMessage(**base)


def test_retrieval_query_blends_follow_up_with_recent():
    history = (
        "2541@s.whatsapp.net: Hackathon is Wednesday\n"
        "2541@s.whatsapp.net: Is this verified"
    )
    query = retrieval_query("Is this verified", history)
    assert "Hackathon is Wednesday" in query
    assert "Is this verified" in query


def test_episode_body_is_actor_content_with_provenance_in_description():
    msg = _group()
    body = episode_body(msg)
    assert body == "2348099999999@s.whatsapp.net: Hackathon is monday"
    desc = episode_source_description(msg)
    assert "source_jid=120363test@g.us" in desc
    assert "message_id=MID1" in desc
    assert episode_name("MID1") == "MID1"
    assert reply_episode_name("MID1") == "bot-MID1"
    assert reply_episode_body(msg, "Monday.") == "bot: Monday."
    assert "question=Hackathon is monday" in reply_source_description(msg)


class FakeGraphitiStore:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def ingest_message(self, msg) -> bool:
        self.names.append(msg.message_id)
        return True

    async def ingest_reply(self, msg, reply: str) -> bool:
        self.names.append(f"bot-{msg.message_id}")
        return True

    async def ingest_batch(self, messages) -> bool:
        for m in messages:
            self.names.append(m.message_id)
        return True


def test_all_groups_queued_for_graphiti_including_non_reply():
    runtime = Runtime(_settings(), FakeClient())
    runtime.store = FakeGraphitiStore()  # type: ignore[assignment]

    async def run() -> None:
        await runtime._handle_group(_group(text="Hackathon is monday"))
        await runtime._handle_group(
            _group(
                chat_jid="120363other@g.us",
                is_reply_group=False,
                message_id="MID2",
                text="when is lunch?",
            )
        )
        # Messages now go to buffer — flush buffer then process queue
        await runtime.ingest_buffer.flush_all_now()
        await runtime.flush_ingest()

    asyncio.run(run())
    assert runtime.graphiti_ingest_count == 2
    ids = sorted(runtime.store.names)  # type: ignore[attr-defined]
    assert ids == ["MID1", "MID2"]



def test_search_facts_collects_edges_episodes_and_summaries():
    store = GraphitiStore(_settings())

    class Results:
        edges = [SimpleNamespace(fact="Hackathon is Tuesday as per agreement")]
        episodes = [
            SimpleNamespace(
                content=(
                    "2541@s.whatsapp.net: Hackathon is Tuesday now as per agreement\n"
                    "whatsapp_group\nsource_jid: 120363test@g.us"
                )
            ),
            SimpleNamespace(
                content="2541@s.whatsapp.net: When is hackathon\nwhatsapp_group"
            ),
        ]
        nodes = [
            SimpleNamespace(name="2541@s.whatsapp.net", summary=""),
            SimpleNamespace(name="Top100 AFL Workshop", summary="Scheduled for Tuesday"),
        ]

    class FakeGraphiti:
        async def search_(self, query, config=None, group_ids=None):
            assert "hackathon" in query.lower()
            return Results()

    store._graphiti = FakeGraphiti()

    async def run() -> None:
        facts = await store.search_facts("When is hackathon")
        assert store.is_grounded(facts)
        assert "Hackathon is Tuesday as per agreement" in facts
        assert "2541@s.whatsapp.net: Hackathon is Tuesday now as per agreement" in facts
        assert "Top100 AFL Workshop: Scheduled for Tuesday" in facts
        assert "2541@s.whatsapp.net" not in facts
        assert not any("When is hackathon" in item for item in facts)

    asyncio.run(run())


def test_search_facts_drops_low_rerank_scores():
    store = GraphitiStore(_settings(retrieval_min_score=0.4))

    class Results:
        edges = [
            SimpleNamespace(fact="Hackathon is Tuesday"),
            SimpleNamespace(fact="Unrelated noise"),
        ]
        edge_reranker_scores = [0.9, 0.05]
        episodes = []
        episode_reranker_scores = []
        nodes = []
        node_reranker_scores = []

    class FakeGraphiti:
        async def search_(self, query, config=None, group_ids=None):
            return Results()

    store._graphiti = FakeGraphiti()

    async def run() -> None:
        facts = await store.search_facts("When is hackathon")
        assert facts == ["Hackathon is Tuesday"]

    asyncio.run(run())


class RetrievingStore:
    def __init__(self, facts: list[str]) -> None:
        self.facts = facts
        self.queries: list[str] = []

    async def search_facts(self, query: str) -> list[str]:
        self.queries.append(query)
        return list(self.facts)

    def is_grounded(self, facts: list[str]) -> bool:
        return len(facts) >= 1

    async def ingest_message(self, msg) -> bool:
        return False

    async def ingest_reply(self, msg, reply: str) -> bool:
        return False


def test_group_reply_uses_graphiti_facts_after_empty_recent():
    client = FakeClient()
    groq = FakeGroq("Tuesday.")
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.store = RetrievingStore(["Hackathon is Tuesday now as per agreement"])  # type: ignore[assignment]
    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        await runtime._handle_group(_group(chat_obj=chat, text="When is hackathon"))

    asyncio.run(run())
    assert client.sent == [(chat, "Tuesday.")]
    assert "Community facts" in groq.prompts[0]
    assert "Hackathon is Tuesday now as per agreement" in groq.prompts[0]


def test_retrieval_miss_with_prior_chat_attempts_groq():
    """When Graphiti has no facts but recent chat provides context,
    the bot now tries Groq (per AGENTS.md: short-term context is valid grounding).
    If Groq returns UNKNOWN, admin is still notified."""
    client = FakeClient()
    groq = FakeGroq("UNKNOWN")  # Groq sees context but can't answer
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.store = RetrievingStore([])  # type: ignore[assignment]
    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        await runtime._handle_group(
            _group(chat_obj=chat, text="lol same", message_id="banter")
        )
        await runtime._handle_group(_group(chat_obj=chat, text="When is hackathon"))

    asyncio.run(run())
    # Groq IS called because short-term context exists
    assert len(groq.prompts) == 1
    # But UNKNOWN response → admin notified, no group reply
    assert runtime.notify_count == 1
    assert "Unanswered Question" in client.sent[0][1]


def test_retrieval_miss_stays_silent_when_recent_is_only_the_question():
    client = FakeClient()
    groq = FakeGroq("should not be called")
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.store = RetrievingStore([])  # type: ignore[assignment]
    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        await runtime._handle_group(_group(chat_obj=chat, text="When is hackathon"))

    asyncio.run(run())
    assert groq.prompts == []
    assert runtime.notify_count == 1
    assert "Unanswered Question" in client.sent[0][1]


def test_follow_up_search_includes_recent_chat():
    client = FakeClient()
    groq = FakeGroq("Wednesday, from the latest group message.")
    store = RetrievingStore(["Hackathon is Wednesday"])
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.store = store  # type: ignore[assignment]
    chat = SimpleNamespace(User="120363test", Server="g.us")

    async def run() -> None:
        await runtime._handle_group(
            _group(chat_obj=chat, text="Hackathon is Wednesday", message_id="a")
        )
        await runtime._handle_group(
            _group(chat_obj=chat, text="Is this verified", message_id="b")
        )

    asyncio.run(run())
    assert store.queries
    assert "Hackathon is Wednesday" in store.queries[-1]
    assert "Is this verified" in store.queries[-1]
    assert client.sent[-1][1] == "Wednesday, from the latest group message."
    assert "prefer these on conflict" in groq.prompts[0]


def test_store_skips_duplicate_names():
    store = GraphitiStore(_settings())
    store._graphiti = object()  # type: ignore[assignment]
    store._seen_names.add("MID1")

    async def run() -> None:
        assert await store.ingest_message(_group()) is False

    asyncio.run(run())


def test_graphiti_enabled_requires_both_keys():
    assert _settings().graphiti_enabled is False
    assert _settings(groq_api_key="gsk", openai_api_key="sk").graphiti_enabled is True
    assert (
        _settings(groq_api_key="gsk", openai_api_key="sk", graphiti_wanted=False).graphiti_enabled
        is False
    )


def test_user_dm_grounded_reply_is_not_ingested():
    client = FakeClient()
    groq = FakeGroq("Tuesday.")
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    store = RetrievingStore(["Hackathon is Tuesday"])
    runtime.store = store  # type: ignore[assignment]
    chat = SimpleNamespace(User="2348099999999", Server="s.whatsapp.net")
    dm = IncomingUserDm(
        chat_jid="2348099999999@s.whatsapp.net",
        sender_jid="2348099999999@s.whatsapp.net",
        message_id="dm1",
        text="When is hackathon",
        iso_time="2026-09-20T07:00:00+00:00",
        chat_obj=chat,
    )

    async def run() -> None:
        await runtime._handle_user_dm(dm)
        await runtime.flush_ingest()

    asyncio.run(run())
    assert client.sent == [(chat, "Tuesday.")]
    assert runtime.notify_count == 0
    assert runtime.graphiti_ingest_count == 0
    assert "Hackathon is Tuesday" in groq.prompts[0]
    assert "When is hackathon" not in runtime.recent.format_lines(10)


def test_user_dm_retrieval_miss_notifies_admin_silently():
    client = FakeClient()
    groq = FakeGroq("should not be called")
    runtime = Runtime(_settings(groq_api_key="gsk_test"), client, groq=groq)
    runtime.store = RetrievingStore([])  # type: ignore[assignment]
    chat = SimpleNamespace(User="2348099999999", Server="s.whatsapp.net")
    dm = IncomingUserDm(
        chat_jid="2348099999999@s.whatsapp.net",
        sender_jid="2348099999999@s.whatsapp.net",
        message_id="dm2",
        text="When is lunch",
        iso_time="2026-09-20T07:00:00+00:00",
        chat_obj=chat,
    )

    async def run() -> None:
        await runtime._handle_user_dm(dm)

    asyncio.run(run())
    assert groq.prompts == []
    assert runtime.notify_count == 1
    assert client.sent[0][0] != chat
    assert "Direct message" in client.sent[0][1]
    assert "When is lunch" in client.sent[0][1]
