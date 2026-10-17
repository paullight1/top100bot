import asyncio
import json
from dataclasses import replace

from test_knowledge_runtime import Client, event, settings
from unipod_bot.incoming import IncomingUserDm, classify_message
from unipod_bot.runtime import Runtime


class NoEvidenceModel:
    async def chat_completion(self, messages, **kwargs):
        return json.dumps({"answers_question": False, "conflict": False, "evidence": []})


def dm(text, mid="test"):
    return IncomingUserDm(chat_jid="member@s.whatsapp.net", sender_jid="member@s.whatsapp.net",
        message_id=mid, text=text, iso_time="", chat_obj="member@s.whatsapp.net")


def test_greeting_and_test_message_reply_even_without_model_and_never_alert_admin(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        for text in ("hello", "test", "thanks"):
            await runtime._handle_user_dm(dm(text))
        assert len(runtime.client.sent) == 3
        assert all(chat == "member@s.whatsapp.net" for chat, _ in runtime.client.sent)
        assert "help" in runtime.client.sent[0][1].lower()
        assert "receiving" in runtime.client.sent[1][1].lower()
        assert runtime.notify_count == 0
    asyncio.run(run())


def test_unanswered_question_gets_clarification_instead_of_admin_alert(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        runtime.stub_known = True
        runtime.groq = NoEvidenceModel()
        await runtime._handle_user_dm(dm("What is the mentoring fee?"))
        assert runtime.client.sent
        reply = runtime.client.sent[-1][1]
        assert "?" in reply and "fee is" not in reply.lower()
        assert runtime.notify_count == 0
    asyncio.run(run())


def test_explicit_human_help_forwards_once_and_acknowledges_in_same_chat(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        for mid in ("request-1", "request-2"):
            await runtime._handle_user_dm(dm("Please connect me to an admin", mid))
        assert runtime.notify_count == 1
        replies = [text for chat, text in runtime.client.sent if chat == "member@s.whatsapp.net"]
        assert len(replies) == 2
        assert "sent" in replies[0].lower() and "already" in replies[1].lower()
    asyncio.run(run())


def test_group_direct_address_replies_but_ordinary_group_greeting_is_quiet(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        for text in ("hello", "hello AFL bot"):
            job = classify_message(event(chat="test", sender="2348999999999", text=text), settings(tmp_path))
            await runtime._handle_group(replace(job, chat_obj="test@g.us", event_obj=None))
        assert len(runtime.client.sent) == 1
        assert runtime.client.sent[0][0] == "test@g.us"
        assert runtime.notify_count == 0
    asyncio.run(run())


def test_model_can_resolve_followup_but_cannot_supply_unverified_answer(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        seen = []
        class Model:
            async def chat_completion(self, messages, **kwargs):
                seen.append(messages)
                return json.dumps({"kind": "answer", "question": "When is mentoring?"})
        runtime.groq = Model()
        runtime.top100_activities.fetch_relevant = no_activities
        reply = await runtime._library_answer("and when?", "member: Tell me about mentoring")
        assert reply and "?" in reply
        assert "Tuesday" not in reply
        assert seen
    asyncio.run(run())


async def no_activities(*args, **kwargs):
    return []


def test_handoff_failure_is_not_reported_as_success(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path, admin_phones=()), Client())
        await runtime._handle_user_dm(dm("connect me to an admin"))
        assert "couldn't reach" in runtime.client.sent[-1][1]
        assert runtime.notify_count == 0
        assert not runtime._human_help_notified
    asyncio.run(run())


def test_private_knowledge_never_used_for_member_conversation_or_model_rewrite(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        entry = runtime.knowledge.create_draft(title="Secret mentoring fee", content="The secret fee is 9000.",
            actor="admin", source_chat="admin", source_message="private", audience="admin_only")
        runtime.knowledge.publish(entry.id, actor="admin")
        class Model:
            async def chat_completion(self, messages, **kwargs):
                assert "9000" not in json.dumps(messages)
                return json.dumps({"kind": "answer", "question": "Secret mentoring fee", "reply": "The fee is 9000."})
        runtime.groq = Model()
        runtime.top100_activities.fetch_relevant = no_activities
        answer = await runtime._library_answer("What is the mentoring fee?", "")
        assert "9000" not in answer and "verified" in answer
    asyncio.run(run())


def test_negative_or_incidental_admin_mention_does_not_escalate(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        runtime.groq = NoEvidenceModel()
        await runtime._handle_user_dm(dm("Do not contact an admin, what is the mentoring fee?"))
        assert runtime.notify_count == 0
        assert all(chat == "member@s.whatsapp.net" for chat, _ in runtime.client.sent)
    asyncio.run(run())


def test_followup_without_question_mark_routes_but_other_members_mentions_stay_quiet():
    from unipod_bot.routing import group_reply_trigger
    assert group_reply_trigger("and the registration link", (), {"123"}) == "question"
    assert group_reply_trigger("hello AFL bot", (), {"123"}) == "mention"
    assert group_reply_trigger("hello AFL bot @999", ("999",), {"123"}) is None


def test_grounded_followup_preserves_original_question_and_literal_source(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        entry = runtime.knowledge.create_draft(title="Mentoring", content="Mentoring is on Tuesdays.",
            actor="admin", source_chat="admin", source_message="public", audience="members")
        runtime.knowledge.publish(entry.id, actor="admin")
        class Model:
            async def chat_completion(self, messages, **kwargs):
                payload = json.loads(messages[-1]["content"])
                if "sources" not in payload:
                    assert "mentoring" in payload["same_chat_context"].lower()
                    return json.dumps({"kind": "answer", "question": "When is mentoring?"})
                assert payload["question"] == "and when?"
                source = payload["sources"][0]
                return json.dumps({"answers_question": True, "conflict": False,
                    "evidence": [{"source": source["id"], "quote": source["text"]}]})
        runtime.groq = Model()
        reply = await runtime._library_answer("and when?", "member: Tell me about mentoring")
        assert "Mentoring is on Tuesdays." in reply and "Source: Mentoring" in reply
    asyncio.run(run())


def test_admin_social_message_is_a_conversation_not_an_automatic_knowledge_draft(tmp_path):
    from unipod_bot.incoming import IncomingAdminDm
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        await runtime._handle_admin_dm(IncomingAdminDm(chat_jid="admin", sender_jid="2348011111111@s.whatsapp.net",
            message_id="hello", text="hello AFL bot", chat_obj="admin"))
        assert runtime.client.sent and "help" in runtime.client.sent[-1][1].lower()
        assert runtime.knowledge.list_entries() == []
    asyncio.run(run())


def test_typo_normalization_improves_search_without_replacing_original_question(tmp_path):
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        entry = runtime.knowledge.create_draft(title="Mentoring", content="Mentoring is on Tuesdays.",
            actor="admin", source_chat="admin", source_message="typo-source", audience="members")
        runtime.knowledge.publish(entry.id, actor="admin")
        class Model:
            async def chat_completion(self, messages, **kwargs):
                payload = json.loads(messages[-1]["content"])
                if "sources" not in payload:
                    return json.dumps({"kind": "answer", "question": "When is mentoring?"})
                assert payload['question'] == 'wen is mentroign'
                source = payload['sources'][0]
                return json.dumps({'answers_question': True, 'conflict': False,
                    'evidence': [{'source': source['id'], 'quote': source['text']}]})
        runtime.groq = Model()
        reply = await runtime._library_answer('wen is mentroign', '')
        assert 'Mentoring is on Tuesdays.' in reply
    asyncio.run(run())
