import pytest

from app.services.llm import (
    OpenRouterError,
    _retry_delay_seconds,
    _should_retry,
    extract_json,
    message_content,
)


class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"cards": []}') == {"cards": []}

    def test_json_with_surrounding_prose(self):
        text = 'Here is your JSON:\n{"cards": [{"type": "basic"}]}\nDone.'
        assert extract_json(text) == {"cards": [{"type": "basic"}]}

    def test_repairs_invalid_backslash_escape(self):
        # A stray "\ " is not a valid JSON escape; the repair pass should fix it.
        text = '{"front": "a \\ b"}'
        assert extract_json(text)["front"] == "a \\ b"

    def test_raises_on_garbage(self):
        with pytest.raises(Exception):
            extract_json("not json at all")


class TestMessageContent:
    def test_extracts_content(self):
        response = {"choices": [{"message": {"content": "hello"}}]}
        assert message_content(response) == "hello"

    def test_empty_choices_raises(self):
        with pytest.raises(OpenRouterError):
            message_content({"choices": []})

    def test_null_content_raises(self):
        response = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
        with pytest.raises(OpenRouterError):
            message_content(response)


class TestRetryLogic:
    def test_retries_5xx(self):
        assert _should_retry(503, "")

    def test_retries_transient_429(self):
        assert _should_retry(429, "rate limit exceeded")

    def test_does_not_retry_terminal_429(self):
        assert not _should_retry(429, "insufficient credits")

    def test_does_not_retry_4xx(self):
        assert not _should_retry(400, "bad request")

    def test_retry_after_header_is_respected_and_clamped(self):
        assert _retry_delay_seconds(0, 1.5, "5") == 5.0
        assert _retry_delay_seconds(0, 1.5, "9999") == 60.0

    def test_exponential_backoff(self):
        assert _retry_delay_seconds(0, 1.5, None) == 1.5
        assert _retry_delay_seconds(1, 1.5, None) == 3.0
