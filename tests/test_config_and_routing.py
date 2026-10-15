from pathlib import Path
from types import SimpleNamespace

from unipod_bot.admin import admin_dm_reply, event_looks_like_admin, is_admin_jid, is_admin_phone
from unipod_bot.config import Settings, load_settings, normalize_graphiti_group_id, parse_phone_list, resolve_groq_model, resolve_log_level
from unipod_bot.incoming import (
    IncomingAdminDm,
    IncomingUserDm,
    SkipReason,
    classify_message,
    datetime_from_unix,
    is_reply_group,
)
from unipod_bot.jids import is_group_jid, jid_str, phone_from_jid


def _settings(**kwargs) -> Settings:
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
        graphiti_group_id="unipod_innovators",
        admin_phones=("2348011111111", "2348022222222"),
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def test_parse_phone_list_merges_and_dedupes():
    phones = parse_phone_list(
        "2348011111111, +234 802 222 2222",
        "2348011111111",
        "2348033333333",
    )
    assert phones == ("2348011111111", "2348022222222", "2348033333333")


def test_load_settings_admins_and_debug(monkeypatch):
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("ADMIN_PHONES", "2348011111111,2348022222222")
    monkeypatch.setenv("ADMIN_PHONE", "2348033333333")
    monkeypatch.setenv("TEST_GROUP_JID", "120363test@g.us")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("HEARTBEAT_SECONDS", "90")
    monkeypatch.setenv("RETRIEVAL_MIN_SCORE", "0.25")
    monkeypatch.setenv("SHORT_TERM_MAX_MESSAGES", "30")
    settings = load_settings(dotenv=False)
    assert settings.debug is True
    assert settings.log_level == 10
    assert settings.admin_phones == ("2348011111111", "2348022222222", "2348033333333")
    assert settings.admin_phone == "2348011111111"
    assert settings.test_group_jid == "120363test@g.us"
    assert settings.heartbeat_seconds == 90.0
    assert settings.retrieval_min_score == 0.25
    assert settings.short_term_max_messages == 30


def test_load_settings_top100_api_base_url_defaults_to_public_site(monkeypatch):
    monkeypatch.delenv("TOP100_API_BASE_URL", raising=False)
    assert load_settings(dotenv=False).top100_api_base_url == "https://www.top100afl.com"


def test_load_settings_top100_api_base_url_can_be_configured(monkeypatch):
    monkeypatch.setenv("TOP100_API_BASE_URL", "https://example.test/")
    assert load_settings(dotenv=False).top100_api_base_url == "https://example.test"


def test_groq_model_alias():
    assert resolve_groq_model("gpt-oss-120b") == "openai/gpt-oss-120b"
    assert resolve_groq_model("openai/gpt-oss-120b") == "openai/gpt-oss-120b"
    assert resolve_groq_model(None) == "openai/gpt-oss-120b"


def test_graphiti_group_id_strips_colon():
    assert normalize_graphiti_group_id("unipod:innovators") == "unipod_innovators"
    assert normalize_graphiti_group_id("unipod_innovators") == "unipod_innovators"


def test_debug_false_uses_log_level():
    assert resolve_log_level(debug=False, log_level_name="WARNING") == 30
    assert resolve_log_level(debug=True, log_level_name="WARNING") == 10


def test_admin_allowlist():
    settings = _settings()
    assert is_admin_phone("2348011111111", settings)
    assert is_admin_phone("+2348022222222", settings)
    assert not is_admin_phone("2348099999999", settings)
    jid = SimpleNamespace(User="2348011111111", Server="s.whatsapp.net")
    assert is_admin_jid(jid, settings)


def test_group_jid_helpers():
    group = SimpleNamespace(User="120363abc", Server="g.us")
    dm = SimpleNamespace(User="2348011111111", Server="s.whatsapp.net")
    assert is_group_jid(group)
    assert not is_group_jid(dm)
    assert jid_str(group)
    assert phone_from_jid(dm) == "2348011111111"


def test_reply_group_gate():
    settings = _settings(test_group_jid="120363test@g.us")
    assert is_reply_group("120363test@g.us", settings)
    assert not is_reply_group("120363other@g.us", settings)
    assert is_reply_group("120363other@g.us", _settings(test_group_jid=None))


def _event(
    *,
    from_me=False,
    server="g.us",
    chat_user="120363test",
    sender="2348011111111",
    sender_server="s.whatsapp.net",
    sender_alt=None,
    mid="ABC",
):
    chat = SimpleNamespace(User=chat_user, Server=server)
    sender_jid = SimpleNamespace(User=sender, Server=sender_server)
    alt = None
    if sender_alt:
        alt = SimpleNamespace(User=sender_alt, Server="s.whatsapp.net")
    source = SimpleNamespace(
        Chat=chat,
        Sender=sender_jid,
        SenderAlt=alt,
        RecipientAlt=None,
        IsFromMe=from_me,
    )
    info = SimpleNamespace(MessageSource=source, ID=mid)
    return SimpleNamespace(Info=info, Message=None)


def test_classify_skips_own_and_empty_stranger_dm():
    settings = _settings()
    assert classify_message(_event(from_me=True), settings) is SkipReason.FROM_ME
    assert (
        classify_message(_event(server="s.whatsapp.net", sender="2348099999999"), settings)
        is SkipReason.EMPTY_DM
    )


def test_classify_member_dm_text():
    settings = _settings()
    event = _event(server="s.whatsapp.net", sender="2348099999999", chat_user="2348099999999")
    event.Message = SimpleNamespace(conversation="When is hackathon")
    classified = classify_message(event, settings)
    assert isinstance(classified, IncomingUserDm)
    assert classified.text == "When is hackathon"


def test_admin_dm_matches_lid_plus_phone_alt():
    settings = _settings()
    event = _event(
        server="lid",
        chat_user="173126205837467",
        sender="173126205837467",
        sender_server="lid",
        sender_alt="2348011111111",
        mid="DM1",
    )
    event.Message = SimpleNamespace(conversation="ping")
    assert event_looks_like_admin(event, settings)
    classified = classify_message(event, settings)
    assert isinstance(classified, IncomingAdminDm)
    assert classified.message_id == "DM1"
    assert "pong" in admin_dm_reply("ping", settings).lower()


def test_classify_ingests_all_groups_marks_reply_eligible():
    settings = _settings(test_group_jid="120363test@g.us")
    other = classify_message(_event(chat_user="120363other"), settings)
    testg = classify_message(_event(chat_user="120363test"), settings)
    assert other.is_reply_group is False
    assert other.text == ""
    assert testg.is_reply_group is True
    assert testg.message_id == "ABC"


def test_empty_admin_dm_is_skipped():
    settings = _settings()
    event = _event(
        server="s.whatsapp.net",
        chat_user="2348011111111",
        sender="2348011111111",
    )
    assert classify_message(event, settings) is SkipReason.EMPTY_DM


def test_unix_timestamp_seconds_ms_ns():
    sec = datetime_from_unix(1_000_000_000)
    ms = datetime_from_unix(1_000_000_000_000)
    ns = datetime_from_unix(1_000_000_000_000_000_000)
    assert sec.year == 2001
    assert ms.year == 2001
    assert ns.year == 2001


def test_load_settings_falkordb_database_defaults_to_group_id(monkeypatch):
    monkeypatch.delenv("FALKORDB_DATABASE", raising=False)
    monkeypatch.setenv("GRAPHITI_GROUP_ID", "unipod_innovators")
    settings = load_settings(dotenv=False)
    assert settings.falkordb_database == "unipod_innovators"
    assert settings.graphiti_group_id == "unipod_innovators"
