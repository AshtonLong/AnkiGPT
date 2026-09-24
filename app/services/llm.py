"""Thin OpenRouter client: chat completions (with structured outputs, tools and vision),
embeddings, a tool-call loop for agentic phases, and JSON extraction/repair helpers.

Everything above this module (the pipeline) speaks in *roles* and never touches HTTP.
"""

import base64
import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
_HEX_CHARS = set("0123456789abcdefABCDEF")

# A 429 mentioning one of these is a spend/quota wall, not throttling: retrying it
# only burns time. Shared with the pipeline, which uses it to abort a run early.
TERMINAL_ERROR_MARKERS = ("insufficient", "credit", "quota", "billing", "payment")


class OpenRouterError(RuntimeError):
    def __init__(self, message, status_code=None, error_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.response_body = response_body


def is_terminal_error(exc):
    """A failure that will recur on every call, so there's no point continuing."""
    if isinstance(exc, OpenRouterError):
        if exc.status_code in (401, 403):
            return True
        detail = (exc.response_body or "").lower()
        if exc.status_code == 429 and any(m in detail for m in TERMINAL_ERROR_MARKERS):
            return True
    if isinstance(exc, RuntimeError) and "OPENROUTER_API_KEY" in str(exc):
        return True
    return False


def json_schema_format(name, schema):
    """Wrap a JSON schema as a strict `response_format` for structured outputs."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


# Strict JSON-schema response format for card writers. Supported models constrain
# decoding to this shape, which removes nearly all fragile JSON parsing/repair. Strict
# mode requires every property to be listed in "required" and nullable types instead of
# optional keys.
CARD_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "enum": ["basic", "cloze"]},
        "front": {"type": ["string", "null"]},
        "back": {"type": ["string", "null"]},
        "cloze_text": {"type": ["string", "null"]},
        "extra": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "source_quote": {"type": ["string", "null"]},
    },
    "required": ["type", "front", "back", "cloze_text", "extra", "tags", "source_quote"],
}

CARD_RESPONSE_FORMAT = json_schema_format(
    "anki_cards",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {"cards": {"type": "array", "items": CARD_ITEM_SCHEMA}},
        "required": ["cards"],
    },
)


def message_content(response):
    """Pull assistant text out of a chat-completions response, with clear errors.

    Guards against empty `choices` and null `content` (content filtering, length cutoffs),
    which would otherwise raise an opaque KeyError/TypeError deep in the pipeline.
    """
    try:
        choices = response["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError("OpenRouter returned no message content.") from exc
    if content is None:
        finish = finish_reason(response) or "unknown"
        raise OpenRouterError(f"OpenRouter returned empty content (finish_reason: {finish}).")
    if isinstance(content, list):
        # Some providers return content parts; concatenate the text ones.
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return content


def finish_reason(response):
    try:
        return response["choices"][0].get("finish_reason") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def assistant_message(response):
    try:
        return response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError("OpenRouter returned no assistant message.") from exc


def usage_of(response):
    usage = response.get("usage") if isinstance(response, dict) else None
    return usage if isinstance(usage, dict) else {}


def image_part(image_bytes, mime="image/png", detail="auto"):
    """Build a vision content part from raw image bytes (data URL, no upload needed)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}


def text_part(text):
    return {"type": "text", "text": text}


def build_headers(api_key, site_url, app_name):
    headers = {"Authorization": f"Bearer {api_key}"}
    if site_url:
        headers["HTTP-Referer"] = site_url
    if app_name:
        headers["X-Title"] = app_name
    return headers


def _parse_error_response(response):
    detail = ""
    error_code = None
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("detail") or "").strip()
                error_code = err.get("code")
            if not detail:
                detail = str(data.get("message") or data.get("detail") or "").strip()
        if not detail:
            detail = json.dumps(data)[:500]
    except ValueError:
        detail = (response.text or "").strip()[:500]
    return detail, error_code


def _retry_delay_seconds(attempt, backoff_seconds, retry_after_header):
    if retry_after_header:
        try:
            value = float(retry_after_header)
            if value >= 0:
                return min(value, 60.0)
        except ValueError:
            pass
    return min(backoff_seconds * (2**attempt), 60.0)


def _should_retry(status_code, detail):
    if status_code in {500, 502, 503, 504}:
        return True
    if status_code == 429:
        lowered = (detail or "").lower()
        return not any(marker in lowered for marker in TERMINAL_ERROR_MARKERS)
    return False


def _post_with_retries(url, payload, headers, max_retries, backoff_seconds, timeout_seconds):
    attempts = max(0, int(max_retries)) + 1
    for attempt in range(attempts):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < attempts - 1:
                time.sleep(_retry_delay_seconds(attempt, backoff_seconds, None))
                continue
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc
        except requests.RequestException as exc:
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

        if response.status_code < 400:
            try:
                data = response.json()
            except ValueError as exc:
                raise OpenRouterError("OpenRouter returned invalid JSON.") from exc
            # OpenRouter can return a 200 with an error envelope (e.g. provider errors).
            if isinstance(data, dict) and data.get("error") and not data.get("choices"):
                err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
                code = err.get("code")
                detail = str(err.get("message") or "")
                try:
                    status_code = int(code)
                except (TypeError, ValueError):
                    status_code = None
                if status_code and _should_retry(status_code, detail) and attempt < attempts - 1:
                    time.sleep(_retry_delay_seconds(attempt, backoff_seconds, None))
                    continue
                raise OpenRouterError(
                    f"OpenRouter error {code or ''}: {detail}".strip(),
                    status_code=status_code, error_code=code, response_body=detail,
                )
            return data

        detail, error_code = _parse_error_response(response)
        status_code = response.status_code
        parts = [f"OpenRouter error {status_code}"]
        if error_code:
            parts.append(f"({error_code})")
        if detail:
            parts.append(f": {detail}")
        message = " ".join(parts)
        if _should_retry(status_code, detail) and attempt < attempts - 1:
            retry_after = response.headers.get("Retry-After")
            time.sleep(_retry_delay_seconds(attempt, backoff_seconds, retry_after))
            continue
        raise OpenRouterError(message, status_code=status_code, error_code=error_code, response_body=detail)

    raise OpenRouterError("OpenRouter request failed after retries.")


def openrouter_chat(
    messages,
    model,
    api_key,
    site_url="",
    app_name="",
    temperature=None,
    max_retries=2,
    backoff_seconds=1.5,
    timeout_seconds=120,
    response_format=None,
    max_tokens=None,
    tools=None,
    tool_choice=None,
    reasoning_effort=None,
    seed=None,
):
    """One chat-completions call. `temperature` is only sent when given — reasoning
    models (including GPT-6 Luna) reject it."""
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": model,
        "messages": messages,
        # Ask OpenRouter to report token usage and cost so LLMRun.cost_estimate is populated.
        "usage": {"include": True},
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if response_format is not None:
        payload["response_format"] = response_format
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    if seed is not None:
        payload["seed"] = seed
    headers = build_headers(api_key, site_url, app_name)
    return _post_with_retries(OPENROUTER_URL, payload, headers, max_retries, backoff_seconds, timeout_seconds)


def openrouter_embeddings(
    texts,
    model,
    api_key,
    site_url="",
    app_name="",
    max_retries=2,
    backoff_seconds=1.5,
    timeout_seconds=120,
):
    """Embed a list of strings. Returns a list of float vectors in input order."""
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    if not texts:
        return []
    payload = {"model": model, "input": list(texts)}
    headers = build_headers(api_key, site_url, app_name)
    data = _post_with_retries(
        OPENROUTER_EMBEDDINGS_URL, payload, headers, max_retries, backoff_seconds, timeout_seconds
    )
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != len(texts):
        raise OpenRouterError("OpenRouter returned a malformed embeddings response.")
    ordered = sorted(items, key=lambda it: it.get("index", 0))
    return [it.get("embedding") or [] for it in ordered]


def tool_spec(name, description, parameters):
    """OpenAI-style function tool definition."""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def run_tool_loop(chat, messages, tools, handlers, max_turns=12, on_turn=None):
    """Drive an agentic loop: call the model, execute any tool calls it makes, feed the
    results back, repeat until it stops calling tools (or `max_turns` is hit).

    `chat(messages, tools)` performs one model call and returns the raw response.
    `handlers` maps tool name -> callable(args_dict) -> JSON-serialisable result. A
    handler may raise StopIteration to end the loop after its result is recorded.
    Returns (final_assistant_text, transcript_messages, turns_used, stopped_by_tool).
    """
    transcript = list(messages)
    stopped = False
    turns = 0
    final_text = ""
    for turns in range(1, max_turns + 1):
        response = chat(transcript, tools)
        msg = assistant_message(response)
        if on_turn:
            on_turn(response, msg)
        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        transcript.append(
            {"role": "assistant", "content": content or "", "tool_calls": tool_calls or None}
            if tool_calls
            else {"role": "assistant", "content": content or ""}
        )
        if not tool_calls:
            final_text = content or ""
            break
        for call in tool_calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}
                result = {"error": f"Could not parse arguments for {name}: {raw_args[:200]}"}
            else:
                handler = handlers.get(name)
                if handler is None:
                    result = {"error": f"Unknown tool {name}"}
                else:
                    try:
                        result = handler(args)
                    except StopIteration as stop:
                        result = stop.value if stop.value is not None else {"ok": True}
                        stopped = True
                    except Exception as exc:  # A bad tool call shouldn't kill the plan.
                        logger.warning("Tool %s failed: %s", name, exc)
                        result = {"error": str(exc)[:500]}
            transcript.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        if stopped:
            break
    return final_text, transcript, turns, stopped


def _sanitize_json_string_escapes(text):
    # Repair common LLM JSON mistakes: invalid backslash escapes and raw control chars in strings.
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        if ch == '"':
            out.append(ch)
            in_string = False
            i += 1
            continue

        if ch == "\\":
            if i + 1 >= n:
                out.append("\\\\")
                i += 1
                continue
            nxt = text[i + 1]
            if nxt in {'"', "\\", "/", "b", "f", "n", "r", "t"}:
                out.append("\\")
                out.append(nxt)
                i += 2
                continue
            if nxt == "u" and i + 5 < n and all(c in _HEX_CHARS for c in text[i + 2 : i + 6]):
                out.append("\\")
                out.append("u")
                out.append(text[i + 2 : i + 6])
                i += 6
                continue
            out.append("\\\\")
            i += 1
            continue

        if ch == "\n":
            out.append("\\n")
            i += 1
            continue
        if ch == "\r":
            out.append("\\r")
            i += 1
            continue
        if ch == "\t":
            out.append("\\t")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _json_load_with_repair(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = _sanitize_json_string_escapes(text)
        if repaired == text:
            raise
        return json.loads(repaired)


def extract_json(text):
    text = (text or "").strip()
    # Strip a ```json fence if the model added one despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return _json_load_with_repair(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return _json_load_with_repair(text[start : end + 1])
        raise


def repair_json(
    raw_text,
    model,
    api_key,
    site_url="",
    app_name="",
    max_retries=2,
    backoff_seconds=1.5,
    timeout_seconds=120,
    schema_hint=None,
):
    prompt = (
        "Fix the JSON to match this schema: "
        + (
            schema_hint
            or '{"cards": [{"type": "basic|cloze", "front": string?, "back": string?, '
            '"cloze_text": string?, "extra": string?, "tags": [string], "source_quote": string?}]}'
        )
        + ". Return only valid JSON."
    )
    messages = [
        {"role": "system", "content": "You fix invalid JSON outputs."},
        {"role": "user", "content": prompt + "\n\nInvalid JSON:\n" + raw_text},
    ]
    response = openrouter_chat(
        messages,
        model,
        api_key,
        site_url,
        app_name,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        timeout_seconds=timeout_seconds,
    )
    return extract_json(message_content(response))
