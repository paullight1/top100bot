import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from unipod_bot.knowledge import KnowledgeLibrary
from unipod_bot.knowledge_answers import grounded_answer


def publish(lib, text="Mentoring is held on Tuesdays.", audience="members", mid="m1"):
    e = lib.create_draft(title="Mentoring guide", content=text, actor="admin", source_chat="admin",
        source_message=mid, audience=audience)
    return lib.publish(e.id, actor="admin")


class Model:
    def __init__(self, quote="Mentoring is held on Tuesdays.", *, malformed=False, conflict=False, callback=None):
        self.quote, self.malformed, self.conflict = quote, malformed, conflict
        self.callback, self.prompts = callback, []

    async def chat_completion(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        self.prompts.append(payload)
        if self.callback:
            self.callback()
        if self.malformed:
            return "It is on Friday, trust me."
        return json.dumps({"answers_question": True, "conflict": self.conflict,
            "evidence": [{"source": payload["sources"][0]["id"], "quote": self.quote}]})


def test_reply_uses_verbatim_approved_evidence_with_source(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "knowledge.db")
    entry = publish(lib)
    model = Model()
    reply = asyncio.run(grounded_answer(model, lib, question="When is mentoring?"))
    assert "Mentoring is held on Tuesdays." in reply
    assert f"{entry.id}" in reply and "Mentoring guide" in reply
    assert "Friday" not in reply


@pytest.mark.parametrize("model", [Model("Mentoring is held on Fridays."), Model(malformed=True), Model(conflict=True)])
def test_unsupported_or_conflicting_model_response_is_withheld(tmp_path, model):
    lib = KnowledgeLibrary(tmp_path / "knowledge.db")
    publish(lib)
    assert asyncio.run(grounded_answer(model, lib, question="When is mentoring?")) is None


def test_private_or_draft_source_never_reaches_member_model_prompt(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "knowledge.db")
    publish(lib, text="The mentoring administrator password is confidential.", audience="admin_only")
    model = Model()
    assert asyncio.run(grounded_answer(model, lib, question="What is the mentoring password?")) is None
    assert not model.prompts


def test_retired_source_during_generation_is_not_sent(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "knowledge.db")
    entry = publish(lib)
    model = Model(callback=lambda: lib.retire(entry.id, actor="admin"))
    assert asyncio.run(grounded_answer(model, lib, question="When is mentoring?")) is None


def test_context_never_becomes_authoritative_evidence(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "knowledge.db")
    assert asyncio.run(grounded_answer(Model(), lib, question="When does it start?",
        context="A member says the mentoring event is on Friday.")) is None


def test_public_activity_source_retains_only_actual_link(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "knowledge.db")
    model = Model("Workshop starts on 12 November.")
    reply = asyncio.run(grounded_answer(model, lib, question="When is the workshop?",
        activities=[("Workshop", "Workshop starts on 12 November.", "https://example.org/register")]))
    assert "https://example.org/register" in reply
    assert "Workshop starts on 12 November." in reply


def test_admin_answer_can_read_private_knowledge(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "knowledge.db")
    publish(lib, audience="admin_only")
    assert asyncio.run(grounded_answer(Model(), lib, question="When is mentoring?", allow_admin=True))


def test_selecting_fragment_that_omits_negation_is_rejected(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "knowledge.db")
    publish(lib, text="Mentoring applicants are not eligible for travel funding.")
    model = Model("eligible for travel funding.")
    assert asyncio.run(grounded_answer(model, lib, question="Is mentoring travel funded?")) is None
