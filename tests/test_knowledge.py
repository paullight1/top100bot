from datetime import datetime, timezone

import pytest

from unipod_bot.knowledge import KnowledgeLibrary


NOW = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)


def draft(lib, text="Mentoring is held on Tuesdays.", **kw):
    defaults = dict(title="Mentoring guide", content=text, actor="admin", source_chat="admin-dm",
                    source_message="m1", audience="members", effective_at="2026-10-16T00:00:00Z")
    defaults.update(kw)
    return lib.create_draft(**defaults)


def test_draft_is_invisible_until_published_and_persists(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    lib = KnowledgeLibrary(path)
    entry = draft(lib)
    assert lib.search("mentoring", now=NOW) == []
    lib.publish(entry.id, actor="publisher", now=NOW)
    reopened = KnowledgeLibrary(path)
    assert reopened.search("mentoring", now=NOW)[0].entry.id == entry.id
    assert reopened.get(entry.id).published_by == "publisher"
    assert len(reopened.audit(entry.id)) == 2


def test_private_draft_never_retrieved_for_members(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "kb.db")
    entry = draft(lib, audience="admin_only")
    lib.publish(entry.id, actor="admin", now=NOW)
    assert lib.search("mentoring", now=NOW) == []
    assert lib.search("mentoring", allow_admin=True, now=NOW)[0].entry.id == entry.id
    assert not lib.active(entry.id, now=NOW)


def test_scheduled_replacement_switches_at_effective_time_and_never_resurrects(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "kb.db")
    old = draft(lib)
    lib.publish(old.id, actor="admin", now=NOW)
    new = draft(lib, "Mentoring is held on Fridays.", source_message="m2", replaces=old.id,
                effective_at="2026-10-18T10:00:00Z", expires_at="2026-10-19T10:00:00Z")
    lib.publish(new.id, actor="admin", now=NOW)
    assert lib.audit(old.id)[-1]["action"] == f"superseded_by:{new.id}"
    assert lib.audit(old.id)[-1]["actor"] == "admin"
    assert lib.active(old.id, now=NOW)
    switch = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)
    assert not lib.active(old.id, now=switch)
    assert lib.active(new.id, now=switch)
    expired = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
    assert not lib.active(new.id, now=expired)
    assert not lib.active(old.id, now=expired)


def test_replay_idempotent_and_second_replacement_rejected(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "kb.db")
    old = draft(lib)
    assert draft(lib).id == old.id
    lib.publish(old.id, actor="admin", now=NOW)
    for message in ("m2", "m3"):
        entry = draft(lib, source_message=message, replaces=old.id)
        if message == "m2":
            lib.publish(entry.id, actor="admin", now=NOW)
        else:
            with pytest.raises(ValueError, match="replacement"):
                lib.publish(entry.id, actor="admin", now=NOW)
    lib.retire(old.id, actor="admin", now=NOW)
    assert not lib.active(old.id, now=NOW)


@pytest.mark.parametrize("overrides", [
    {"audience": "everyone"}, {"expires_at": "bad-date"},
    {"effective_at": "2026-10-17T00:00:00"},
    {"effective_at": "2026-10-20", "expires_at": "2026-10-19"},
    {"content": ""}, {"content": "a" * 80001}, {"source_url": "javascript:bad"},
    {"attachment": b"a" * (10 * 1024 * 1024 + 1)},
])
def test_invalid_material_is_rejected(tmp_path, overrides):
    lib = KnowledgeLibrary(tmp_path / "kb.db")
    with pytest.raises(ValueError):
        draft(lib, **overrides)


def test_lagos_dates_expiry_retire_and_audience_replacement(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "kb.db")
    entry = draft(lib, effective_at="2026-10-17", expires_at="2026-10-18")
    assert entry.effective_at == "2026-10-16T23:00:00+00:00"
    lib.publish(entry.id, actor="admin", now=NOW)
    private = draft(lib, source_message="m2", replaces=entry.id, audience="admin_only")
    with pytest.raises(ValueError, match="audience"):
        lib.publish(private.id, actor="admin", now=NOW)
    lib.retire(entry.id, actor="admin", now=NOW)
    assert lib.search("mentoring", now=NOW) == []


def test_pdf_bytes_are_only_available_when_entry_accessible(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "kb.db")
    entry = draft(lib, attachment=b"%PDF-test", filename="guide.pdf")
    assert lib.document(entry.id, now=NOW) is None
    lib.publish(entry.id, actor="admin", now=NOW)
    assert lib.document(entry.id, now=NOW) == (b"%PDF-test", "guide.pdf")
    lib.retire(entry.id, actor="admin", now=NOW)
    assert lib.document(entry.id, now=NOW) is None


def test_admin_can_classify_a_draft_but_cannot_reclassify_published_content(tmp_path):
    lib = KnowledgeLibrary(tmp_path / "kb.db")
    entry = draft(lib, audience="admin_only")
    lib.set_audience(entry.id, "members", actor="admin")
    assert lib.get(entry.id).audience == "members"
    lib.publish(entry.id, actor="admin", now=NOW)
    with pytest.raises(ValueError, match="draft"):
        lib.set_audience(entry.id, "admin_only", actor="admin")
