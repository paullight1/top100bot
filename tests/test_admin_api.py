import asyncio
from dataclasses import replace

from test_knowledge_runtime import Client, settings
from unipod_bot.runtime import Runtime
from unipod_bot.admin_api import AdminAPI

TOKEN = "test-only-admin-bridge-token-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-AFL-Actor": "web:admin-one"}


def api(tmp_path, **kwargs):
    runtime = Runtime(settings(tmp_path, **kwargs), Client())
    return AdminAPI(runtime, TOKEN)


def test_api_requires_bearer_and_authenticated_actor_and_never_exposes_token(tmp_path):
    async def run():
        app = api(tmp_path)
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {TOKEN}"}):
            status, _ = await app.handle("GET", "/v1/admin/status", {}, headers)
            assert status == 401
        status, body = await app.handle("GET", "/v1/admin/status", {}, HEADERS)
        assert status == 200 and TOKEN not in str(body)
        assert (await AdminAPI(app.runtime, "").handle("GET", "/v1/admin/status", {}, HEADERS))[0] == 503
    asyncio.run(run())


def test_text_knowledge_is_private_draft_until_explicitly_published(tmp_path):
    async def run():
        app = api(tmp_path)
        status, entry = await app.handle("POST", "/v1/admin/knowledge", {
            "title": "Mentoring", "content": "Mentoring is on Tuesdays.", "request_id": "draft-one"}, HEADERS)
        assert status == 201 and entry["status"] == "draft" and entry["audience"] == "admin_only"
        path = "/v1/admin/knowledge/" + entry["id"]
        assert not app.runtime.knowledge.active(entry["id"])
        assert (await app.handle("POST", path + "/audience", {"audience": "members"}, HEADERS))[0] == 200
        assert (await app.handle("POST", path + "/publish", {}, HEADERS))[0] == 200
        assert app.runtime.knowledge.active(entry["id"])
        _, detail = await app.handle("GET", path, {}, HEADERS)
        assert detail["created_by"] == "web:admin-one" and detail["published_by"] == "web:admin-one"
        assert (await app.handle("POST", path + "/retire", {}, HEADERS))[0] == 200
        assert not app.runtime.knowledge.active(entry["id"])
    asyncio.run(run())


def test_announcement_preview_does_not_send_and_send_is_exactly_once(tmp_path):
    async def run():
        app = api(tmp_path)
        _, draft = await app.handle("POST", "/v1/admin/announcements", {
            "title": "Test", "content": "Approved announcement", "request_id": "announcement-one"}, HEADERS)
        path = "/v1/admin/announcements/" + draft["id"]
        status, _ = await app.handle("POST", path + "/send", {}, HEADERS)
        assert status == 409 and not app.runtime.client.sent
        status, preview = await app.handle("POST", path + "/preview", {}, HEADERS)
        assert status == 200 and not app.runtime.client.sent
        payload = {"preview_token": preview["preview_token"]}
        statuses = await asyncio.gather(*(app.handle("POST", path + "/send", payload, HEADERS) for _ in range(2)))
        assert sorted(x[0] for x in statuses) == [200, 409]
        assert len(app.runtime.client.sent) == 1 and app.runtime.client.sent[0][1] == "Approved announcement"
        _, detail = await app.handle("GET", path, {}, HEADERS)
        assert detail["status"] == "sent" and detail["sent_by"] == "web:admin-one"
        assert detail["audit"]
    asyncio.run(run())


def test_announcement_cannot_change_target_reuse_another_actors_preview_or_retry_uncertain_delivery(tmp_path):
    async def run():
        app = api(tmp_path)
        _, draft = await app.handle("POST", "/v1/admin/announcements", {
            "title": "Test", "content": "Approved announcement", "request_id": "announcement-one"}, HEADERS)
        path = "/v1/admin/announcements/" + draft["id"]
        _, preview = await app.handle("POST", path + "/preview", {}, HEADERS)
        payload = {"preview_token": preview["preview_token"]}
        other = {**HEADERS, "X-AFL-Actor": "web:admin-two"}
        assert (await app.handle("POST", path + "/send", payload, other))[0] == 409
        old = app.runtime.settings
        app.runtime.settings = replace(old, test_group_jid="different@g.us", test_group_jids=("different@g.us",))
        assert (await app.handle("POST", path + "/send", payload, HEADERS))[0] == 409
        app.runtime.settings = old
        async def ambiguous_send(*args):
            raise TimeoutError("delivery may have occurred")
        app.runtime.client.send_message = ambiguous_send
        assert (await app.handle("POST", path + "/send", payload, HEADERS))[0] == 502
        assert (await app.handle("POST", path + "/send", payload, HEADERS))[0] == 409
        assert (await app.handle("GET", path, {}, HEADERS))[1]["status"] == "unconfirmed"
    asyncio.run(run())


def test_wildcard_group_and_invalid_document_are_rejected(tmp_path):
    async def run():
        app = api(tmp_path, test_group_jid="*", test_group_jids=("*",))
        assert (await app.handle("POST", "/v1/admin/announcements", {
            "title": "Bad", "content": "Not broadcast", "request_id": "bad"}, HEADERS))[0] == 400
        assert (await app.handle("POST", "/v1/admin/knowledge", {
            "title": "PDF", "pdf_base64": "not-base64", "filename": "x.pdf", "request_id": "bad"}, HEADERS))[0] == 400
    asyncio.run(run())


def test_listener_config_is_disabled_by_default_and_requires_strong_key(tmp_path, monkeypatch):
    from unipod_bot.config import load_settings
    import pytest
    monkeypatch.delenv('BOT_ADMIN_API_PORT', raising=False)
    monkeypatch.delenv('BOT_ADMIN_API_TOKEN', raising=False)
    assert load_settings(dotenv=False).bot_admin_api_port == 0
    monkeypatch.setenv('BOT_ADMIN_API_PORT', '8090')
    with pytest.raises(ValueError, match='BOT_ADMIN_API_TOKEN'):
        load_settings(dotenv=False)


def test_http_transport_enforces_auth_and_dispatches_without_exposing_credentials(tmp_path):
    import httpx
    from threading import Thread
    from unipod_bot.admin_api import make_server
    async def run():
        app = api(tmp_path)
        server = make_server(app, asyncio.get_running_loop(), '127.0.0.1', 0)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            async with httpx.AsyncClient() as client:
                url = f'http://127.0.0.1:{server.server_port}/v1/admin/status'
                assert (await client.get(url)).status_code == 401
                result = await client.get(url, headers=HEADERS)
                assert result.status_code == 200 and TOKEN not in result.text
                result = await client.post(url, content=b'{bad', headers={**HEADERS, 'Content-Type': 'application/json'})
                assert result.status_code == 400
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
    asyncio.run(run())


def test_preview_expires_and_claim_survives_restart_without_duplicate_retry(tmp_path):
    from unipod_bot.announcements import AnnouncementConflict, AnnouncementStore
    import pytest
    store = AnnouncementStore(tmp_path / 'announcements.sqlite3')
    entry = store.create(title='Review me', content='Admin-approved text', target='test@g.us', actor='web:one', request_id='one')
    preview = store.preview(entry['id'], actor='web:one', target='test@g.us', now=100)
    with pytest.raises(AnnouncementConflict):
        store.claim(entry['id'], actor='web:one', target='test@g.us', token=preview['preview_token'], now=701)
    store.claim(entry['id'], actor='web:one', target='test@g.us', token=preview['preview_token'], now=200)
    restarted = AnnouncementStore(store.path)
    with pytest.raises(AnnouncementConflict):
        restarted.claim(entry['id'], actor='web:one', target='test@g.us', token=preview['preview_token'], now=201)


def test_pdf_upload_is_extracted_into_private_unpublished_knowledge(tmp_path):
    import base64
    import fitz
    document = fitz.open()
    document.new_page().insert_text((72, 72), 'Mentoring guidance awaiting approval.')
    pdf = document.tobytes()
    document.close()
    async def run():
        app = api(tmp_path)
        status, entry = await app.handle('POST', '/v1/admin/knowledge', {
            'title': 'Uploaded guide', 'filename': '../guide.pdf', 'pdf_base64': base64.b64encode(pdf).decode(),
            'request_id': 'pdf-one'}, HEADERS)
        assert status == 201 and entry['filename'] == 'guide.pdf'
        assert 'Mentoring guidance' in entry['content']
        assert entry['status'] == 'draft' and entry['audience'] == 'admin_only'
        assert app.runtime.knowledge.search('Mentoring guidance') == []
    asyncio.run(run())


def test_private_announcement_files_and_literal_content(tmp_path):
    from unipod_bot.announcements import AnnouncementStore
    store = AnnouncementStore(tmp_path / 'announcements.sqlite3')
    literal = '\n  Approved text\n\n'
    entry = store.create(title='Title', content=literal, target='test@g.us', actor='web:admin', request_id='literal')
    assert entry['content'] == literal
    with store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE announcements SET title=?', ('Updated',))
        for suffix in ('', '-wal', '-shm'):
            path = tmp_path / ('announcements.sqlite3' + suffix)
            if path.exists():
                assert path.stat().st_mode & 0o777 == 0o600


def test_knowledge_metadata_redacts_whatsapp_identities():
    from unipod_bot.admin_api import public_knowledge
    result = public_knowledge({'created_by': '123@s.whatsapp.net', 'source_chat': '456@g.us',
        'source_message': '123@s.whatsapp.net:message', 'content': 'Literal text',
        'published_by': 'web:admin', 'audit': [{'actor': '123@lid', 'action': 'draft'}]})
    assert '123' not in str(result) and '456' not in str(result)
    assert result['published_by'] == 'web:admin' and result['content'] == 'Literal text'


def test_wildcard_target_is_not_ready(tmp_path):
    async def run():
        app = api(tmp_path, test_group_jid='*')
        _, status = await app.handle('GET', '/v1/admin/status', {}, HEADERS)
        assert status['test_group_configured'] is False
    asyncio.run(run())


def test_explicit_human_wording():
    from unipod_bot.conversation import requests_human
    for message in ('Can I talk to a human?', 'Can someone connect me to an admin?', 'I want help from a human.'):
        assert requests_human(message)
    for message in ('The admin is helpful', 'I do not want help from a human', 'Can I talk about the admin guide?'):
        assert not requests_human(message)


def test_private_notifications_never_target_groups(tmp_path):
    from neonize.utils.jid import build_jid
    from unipod_bot.jids import jid_str
    async def run():
        runtime = Runtime(settings(tmp_path), Client())
        runtime.inbox._extra['misconfigured'] = build_jid('test', 'g.us')
        assert all(not jid_str(target).endswith('@g.us') for target in runtime.inbox.targets())
        runtime.inbox.targets = lambda: [build_jid('test', 'g.us')]
        assert not await runtime._send_admin_notify('private', trigger='human_help', chat='test', message_id='test')
        assert not runtime.client.sent
    asyncio.run(run())
