import asyncio
from datetime import datetime

import httpx
import pytest

from unipod_bot.top100_activities import (
    Top100ActivitiesClient,
    is_activity_question,
)


def _event(**overrides):
    row = {
        "id": "evt-1",
        "slug": "afl-bootcamp",
        "title": "AFL Leadership Bootcamp",
        "summary": "Leadership and innovation workshop",
        "description": "A practical workshop for awardees",
        "start_at": "2026-11-12T09:00:00+01:00",
        "end_at": "2026-11-12T16:00:00+01:00",
        "timezone": "Africa/Lagos",
        "location": "Lagos",
        "registration_url": "https://register.example/afl",
        "tags": ["leadership", "workshop"],
        "status": "published",
        "visibility": "public",
        "programme_label": "AFL",
        "session_number": 2,
    }
    row.update(overrides)
    return row


def _client_for(payload, *, status=200, error=None):
    requests = []

    async def handler(request):
        requests.append(request)
        if error:
            raise error
        return httpx.Response(status, json=payload)

    client = Top100ActivitiesClient(
        "https://www.top100afl.com",
        transport=httpx.MockTransport(handler),
    )
    return client, requests


def test_fetches_public_event_and_normalizes_camel_case_fields():
    client, requests = _client_for([
        _event(
            start_at=None,
            startAt="2026-11-12T09:00:00+01:00",
            registration_url=None,
            registrationUrl="https://register.example/afl",
            programme_label=None,
            programmeLabel="AFL",
            session_number=None,
            sessionNumber=2,
        )
    ])

    result = asyncio.run(client.fetch_relevant("When is the AFL bootcamp?"))

    assert len(result) == 1
    assert result[0].title == "AFL Leadership Bootcamp"
    assert result[0].start_at == datetime.fromisoformat("2026-11-12T09:00:00+01:00")
    assert result[0].registration_url == "https://register.example/afl"
    assert result[0].activity_url == "https://register.example/afl"
    assert result[0].programme_label == "AFL"
    assert result[0].session_number == 2
    assert requests[0].url.path == "/api/events"
    assert requests[0].url.params.get("audience") == "awardees"
    assert requests[0].url.params.get("limit") == "100"


def test_filters_rows_unless_published_and_public():
    client, _ = _client_for([
        _event(id="draft", status="draft"),
        _event(id="private", visibility="awardee_only"),
        _event(),
    ])

    result = asyncio.run(client.fetch_relevant("AFL leadership bootcamp"))

    assert [event.id for event in result] == ["evt-1"]


def test_activity_follow_up_uses_recent_chat_for_relevance():
    client, _ = _client_for([_event()])

    result = asyncio.run(client.fetch_relevant("When does it start?", "We were discussing the AFL leadership bootcamp"))

    assert [event.slug for event in result] == ["afl-bootcamp"]
    assert is_activity_question("When does it start?", "Tell me about the AFL bootcamp")
    assert not is_activity_question("Who won the football match?")


def test_empty_payload_returns_no_activities():
    client, _ = _client_for([])

    assert asyncio.run(client.fetch_relevant("When is the AFL bootcamp?")) == []


def test_malformed_json_returns_no_activities():
    async def handler(_request):
        return httpx.Response(200, content=b"not-json")

    client = Top100ActivitiesClient(
        "https://www.top100afl.com", transport=httpx.MockTransport(handler)
    )

    assert asyncio.run(client.fetch_relevant("When is the AFL bootcamp?")) == []


def test_missing_dates_and_registration_uses_public_events_link():
    client, _ = _client_for([
        _event(start_at="not-a-date", end_at="bad", registration_url="javascript:alert(1)")
    ])

    result = asyncio.run(client.fetch_relevant("When is the AFL bootcamp?"))

    assert len(result) == 1
    assert result[0].start_at is None
    assert result[0].end_at is None
    assert result[0].registration_url is None
    assert result[0].activity_url == "https://www.top100afl.com/events"


def test_timeout_and_http_failure_return_no_activities():
    timeout_client, _ = _client_for([], error=httpx.ReadTimeout("slow"))
    assert asyncio.run(timeout_client.fetch_relevant("AFL workshop date")) == []

    status_client, _ = _client_for({"error": "unavailable"}, status=503)
    assert asyncio.run(status_client.fetch_relevant("AFL workshop date")) == []


def test_returns_at_most_five_ranked_results():
    client, _ = _client_for([
        _event(id=f"evt-{number}", slug=f"afl-workshop-{number}", title=f"AFL workshop {number}")
        for number in range(8)
    ])

    result = asyncio.run(client.fetch_relevant("AFL workshop"))

    assert len(result) == 5
