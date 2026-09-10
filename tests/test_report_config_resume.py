"""U5: the pause reaches the frontend, and a Confirm is the only thing that resumes it.

These exercise the router seam rather than the graph: the pause event's SSE
shape and pending record, and the Confirm endpoint's acceptance rules. The
graph-side pause and resume are covered in ``test_report_config_interrupt``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.chats import router_sessions
from app.chats.schemas import ReportConfigRequest

pytestmark = pytest.mark.unit

THREAD_ID = "0199a1e6-0000-7000-8000-000000000001"
CHAT_ID = "chat-under-test"
SESSION_ID = "session-under-test"
USER_ID = "user-under-test"

PAUSE_EVENT: dict[str, Any] = {
    "name": "report_config",
    "status": "awaiting_configuration",
    "thread_id": THREAD_ID,
    "chat_id": CHAT_ID,
    "default_tier": "study",
    "tiers": ["study", "brief"],
    "styles": ["investor"],
    "layouts": [
        {"report_tier": "study", "report_title": "UK Fintech", "markdown": "# Study"},
        {"report_tier": "brief", "report_title": "UK Fintech", "markdown": "# Brief"},
    ],
}


class _FakeRedisClient:
    """Just enough Redis for the two keys this feature touches."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.streams: dict[str, list[dict[str, str]]] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: Any = None, nx: bool = False) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key: str) -> int:
        return int(self.store.pop(key, None) is not None)

    async def exists(self, key: str) -> int:
        return int(key in self.store or key in self.streams)

    async def expire(self, key: str, ttl: Any) -> bool:
        return key in self.store or key in self.streams

    async def xadd(self, key: str, event: dict[str, str]) -> str:
        self.streams.setdefault(key, []).append(event)
        return f"{len(self.streams[key])}-0"


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list[tuple[Any, tuple, dict]] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((func, args, kwargs))


@pytest.fixture
def redis_client(monkeypatch: pytest.MonkeyPatch) -> _FakeRedisClient:
    client = _FakeRedisClient()
    monkeypatch.setattr(
        router_sessions, "redis_instance", type("_R", (), {"redis_client": client})()
    )
    # A live session for this chat: both endpoint gates read these.
    client.store[f"chat_session:{CHAT_ID}"] = f"session:{SESSION_ID}"
    client.streams[f"session:{SESSION_ID}"] = []
    return client


@pytest.fixture
def background() -> _FakeBackgroundTasks:
    return _FakeBackgroundTasks()


def _submission(**overrides: Any) -> ReportConfigRequest:
    payload: dict[str, Any] = {
        "chat_id": CHAT_ID,
        "session_id": SESSION_ID,
        "thread_id": THREAD_ID,
        "report_tier": "brief",
    }
    payload.update(overrides)
    return ReportConfigRequest(**payload)


async def _confirm(background: _FakeBackgroundTasks, **overrides: Any) -> Any:
    return await router_sessions.confirm_report_config(
        _submission(**overrides), background, USER_ID
    )


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def _sse_events(redis_client: _FakeRedisClient) -> list[dict[str, Any]]:
    return [json.loads(event["data"]) for event in redis_client.streams[f"session:{SESSION_ID}"]]


PAUSED_MESSAGE = "Write me a report on UK fintech."


async def _pause(redis_client: _FakeRedisClient) -> None:
    await router_sessions._record_report_config_pause(
        CHAT_ID, SESSION_ID, PAUSED_MESSAGE, PAUSE_EVENT
    )


# --- the pause event -------------------------------------------------------


async def test_pause_reaches_the_stream_as_its_own_sse_type(
    redis_client: _FakeRedisClient,
) -> None:
    """R2: the frontend is told the graph is waiting rather than inferring it."""
    await _pause(redis_client)

    events = _sse_events(redis_client)
    assert len(events) == 1
    assert events[0]["type"] == "report_config_required"


async def test_pause_event_carries_the_thread_and_both_tagged_layouts(
    redis_client: _FakeRedisClient,
) -> None:
    """R7: the popup gets everything it needs to render and to resume."""
    await _pause(redis_client)

    event = _sse_events(redis_client)[0]
    assert event["thread_id"] == THREAD_ID
    assert event["default_tier"] == "study"
    assert [layout["report_tier"] for layout in event["layouts"]] == ["study", "brief"]


async def test_pause_parks_the_thread_where_confirm_will_look_for_it(
    redis_client: _FakeRedisClient,
) -> None:
    """R4: the pause outlives the request that created it."""
    await _pause(redis_client)

    pending = json.loads(redis_client.store[router_sessions.report_config_pending_key(CHAT_ID)])
    assert pending["thread_id"] == THREAD_ID
    assert pending["session_id"] == SESSION_ID


# --- confirming ------------------------------------------------------------


async def test_confirm_resumes_the_paused_turn(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """R3: the submission drives the same turn on the same thread."""
    await _pause(redis_client)

    response = await _confirm(background)

    assert response.status_code == 200
    assert _body(response)["success"] is True

    func, args, _ = background.tasks[0]
    assert func is router_sessions.chat_producer
    request, user_id, thread_id, submission = args
    assert (request.chat_id, request.session_id) == (CHAT_ID, SESSION_ID)
    assert user_id == USER_ID
    assert thread_id == THREAD_ID
    assert submission["report_tier"] == "brief"


async def test_confirm_carries_the_message_that_started_the_paused_turn(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """The paused turn saved no history, so the resume is what writes the turn."""
    await _pause(redis_client)

    await _confirm(background)

    request = background.tasks[0][1][0]
    assert request.message == PAUSED_MESSAGE
    assert json.loads(redis_client.store[f"chat:{CHAT_ID}:processing_user_message"]) == {
        "type": "human",
        "content": PAUSED_MESSAGE,
    }


async def test_confirm_forwards_every_submitted_field(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """R12: the fields v1 does not act on still reach the configuration."""
    await _pause(redis_client)

    await _confirm(
        background,
        report_tier="study",
        style="investor",
        output_formats=["pdf", "pptx"],
        language="English",
        data_sources=["web"],
    )

    _, args, _ = background.tasks[0]
    assert args[3] == {
        "report_tier": "study",
        "style": "investor",
        "output_formats": ["pdf", "pptx"],
        "language": "English",
        "data_sources": ["web"],
    }


async def test_confirm_without_a_pending_pause_is_refused(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """Nothing paused, so nothing may start an unconfigured retrieval."""
    response = await _confirm(background)

    assert response.status_code == 409
    assert _body(response)["error_code"] == "NO_PENDING_REPORT_CONFIG"
    assert background.tasks == []


async def test_confirm_with_an_unknown_thread_is_refused(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """A Confirm can only resume the turn it was issued for."""
    await _pause(redis_client)

    response = await _confirm(background, thread_id="0199a1e6-0000-7000-8000-00000000dead")

    assert response.status_code == 409
    assert _body(response)["error_code"] == "STALE_REPORT_CONFIG_THREAD"
    assert background.tasks == []
    assert router_sessions.report_config_pending_key(CHAT_ID) in redis_client.store


async def test_confirm_after_the_pause_expired_is_refused(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """R4's TTL boundary: an aged-out pause is not silently resumed."""
    await _pause(redis_client)
    del redis_client.store[router_sessions.report_config_pending_key(CHAT_ID)]

    response = await _confirm(background)

    assert response.status_code == 409
    assert background.tasks == []


async def test_a_pause_is_confirmable_only_once(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """A double-submit must not resume the same thread twice."""
    await _pause(redis_client)

    first = await _confirm(background)
    second = await _confirm(background)

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(background.tasks) == 1


async def test_confirm_claims_the_turn_lock(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """The resumed turn is a turn: an ordinary chat message may not race it."""
    await _pause(redis_client)

    await _confirm(background)

    assert f"chat:{CHAT_ID}:processing_user_message" in redis_client.store


async def test_confirm_on_an_expired_session_is_refused(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    await _pause(redis_client)
    del redis_client.store[f"chat_session:{CHAT_ID}"]

    response = await _confirm(background)

    assert response.status_code == 401
    assert background.tasks == []


# --- AE4: an ordinary message while paused ---------------------------------


async def test_a_chat_message_while_paused_leaves_the_pause_standing(
    redis_client: _FakeRedisClient, background: _FakeBackgroundTasks
) -> None:
    """AE4/KD7: only a Confirm resumes; a chat message starts its own turn."""
    await _pause(redis_client)

    from app.chats.schemas import ChatRequest

    chat_response = await router_sessions.chat(
        ChatRequest(
            message="Actually, add a section on payments.", chat_id=CHAT_ID, session_id=SESSION_ID
        ),
        background,
        USER_ID,
    )

    assert chat_response.status_code == 200
    # The new turn goes through chat_producer as an ordinary turn — no thread id,
    # no submission — so it cannot resume the parked thread.
    func, args, _ = background.tasks[0]
    assert func is router_sessions.chat_producer
    assert len(args) == 2

    pending = json.loads(redis_client.store[router_sessions.report_config_pending_key(CHAT_ID)])
    assert pending["thread_id"] == THREAD_ID
