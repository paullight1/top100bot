from datetime import datetime, timezone

from unipod_bot.models import IncomingMessage
from unipod_bot.routing import group_reply_trigger, should_attempt_reply
from unipod_bot.whatsapp import is_bot_mentioned, looks_like_question


def _msg(**kwargs) -> IncomingMessage:
    base = dict(
        chat_jid="120@g.us",
        sender_jid="2341@s.whatsapp.net",
        sender_user="2341",
        message_id="abc",
        text="hello",
        timestamp=datetime.now(timezone.utc),
        is_group=True,
        is_from_me=False,
        mentioned_users=(),
        raw=None,
    )
    base.update(kwargs)
    return IncomingMessage(**base)


def test_question_mark() -> None:
    assert looks_like_question("When is the hackathon?")


def test_question_wh() -> None:
    assert looks_like_question("where is the venue")
    assert not looks_like_question("the venue is campus")


def test_mention_triggers() -> None:
    bot = {"2349"}
    msg = _msg(text="help", mentioned_users=("2349",))
    assert is_bot_mentioned(msg.text, msg.mentioned_users, bot)
    assert should_attempt_reply(msg, bot)


def test_banter_no_reply() -> None:
    bot = {"2349"}
    msg = _msg(text="lol same")
    assert not should_attempt_reply(msg, bot)
    assert group_reply_trigger("lol same", (), bot) is None
    assert group_reply_trigger("when is demo day", (), bot) == "question"
    assert group_reply_trigger("help", ("2349",), bot) == "mention"


def test_leading_at_lid_still_counts_as_question() -> None:
    text = "@116003476340932 when is Hackathon"
    # When the bot is NOT the mentioned user, the question is directed at
    # someone else → suppressed (None), not "question".
    assert group_reply_trigger(text, (), set()) is None
    # When the bot IS the mentioned user → "mention"
    assert group_reply_trigger(text, (), {"116003476340932"}) == "mention"
    assert group_reply_trigger("@116003476340932 hello people", (), set()) is None
    # "Hackathon is on friday" is a factual claim → statement_check (new behavior)
    assert group_reply_trigger("Hackathon is on friday", (), set()) == "statement_check"


def test_directed_question_suppressed() -> None:
    """Questions @-mentioning someone other than the bot are suppressed."""
    bot = {"9999"}
    # @alice is not the bot — question should be suppressed
    assert group_reply_trigger("@1234 when is the event?", ("1234",), bot) is None
    # Same question without @-mention → triggers as question
    assert group_reply_trigger("when is the event?", (), bot) == "question"
    # @bot → triggers as mention
    assert group_reply_trigger("@9999 when is the event?", ("9999",), bot) == "mention"


def test_statement_check_trigger() -> None:
    """Factual claims trigger statement_check for correction pipeline."""
    bot = {"9999"}
    # Date assertion → statement_check
    assert group_reply_trigger("The event starts on 5 October", (), bot) == "statement_check"
    # Time assertion → statement_check
    assert group_reply_trigger("Registration closes at 5:00 pm", (), bot) == "statement_check"
    # Casual banter → no trigger
    assert group_reply_trigger("lol that was fun", (), bot) is None
