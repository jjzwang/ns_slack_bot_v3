# =============================================================================
# Pillar extraction — characterization test
# =============================================================================
# Loads a canonical transcript fixture and asserts extract_pillars produces
# the expected 4-pillar JSON when the Haiku call returns a known payload.
# The Anthropic SDK is stubbed at the function that returns the cached client
# so the real network is never touched.

import json
from pathlib import Path
from typing import Any

import pytest

import reviewer

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(self._response_text)


class _FakeClient:
    def __init__(self, response_text: str) -> None:
        self.messages = _FakeMessages(response_text)


@pytest.fixture
def credit_limit_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "transcript_credit_limit.json").read_text())


def test_extract_pillars_credit_limit_fixture(monkeypatch, credit_limit_fixture):
    fake_client = _FakeClient(credit_limit_fixture["mocked_claude_response"])
    monkeypatch.setattr(reviewer, "_get_extraction_client", lambda _: fake_client)

    result = reviewer.extract_pillars(
        credit_limit_fixture["history"],
        api_key="sk-ant-test",
    )

    assert result.success is True
    assert result.error is None
    assert result.pillars == credit_limit_fixture["expected_pillars"]
    # Sanity: Haiku model name should be the one configured in config.py.
    assert len(fake_client.messages.calls) == 1
    assert fake_client.messages.calls[0]["model"].startswith("claude-haiku")


def test_extract_pillars_handles_malformed_response(monkeypatch):
    # When the LLM returns junk, extraction returns the empty-pillar baseline
    # rather than crashing. This is the "graceful degradation" contract that
    # _run_interview_turn relies on to keep the conversation moving.
    fake_client = _FakeClient("not json at all, sorry")
    monkeypatch.setattr(reviewer, "_get_extraction_client", lambda _: fake_client)

    result = reviewer.extract_pillars(
        [{"role": "user", "content": "hi"}],
        api_key="sk-ant-test",
    )

    # success=True because the API call succeeded; pillars are all None
    # because the parser couldn't make sense of the response.
    assert result.success is True
    assert all(v is None for v in result.pillars.values())
