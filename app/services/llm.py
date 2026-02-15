import json
import time
import requests


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_HEX_CHARS = set("0123456789abcdefABCDEF")


class OpenRouterError(RuntimeError):
    def __init__(self, message, status_code=None, error_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.response_body = response_body


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
        terminal_markers = ("insufficient", "credit", "quota", "billing", "payment")
        return not any(marker in lowered for marker in terminal_markers)
    return False


def openrouter_chat(
    messages,
    model,
    api_key,
    site_url="",
    app_name="",
    temperature=0.2,
    max_retries=2,
    backoff_seconds=1.5,
    timeout_seconds=120,
):
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    headers = build_headers(api_key, site_url, app_name)
    attempts = max(0, int(max_retries)) + 1
    for attempt in range(attempts):
        try:
            response = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=timeout_seconds)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < attempts - 1:
                time.sleep(_retry_delay_seconds(attempt, backoff_seconds, None))
                continue
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc
        except requests.RequestException as exc:
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

        if response.status_code < 400:
            try:
                return response.json()
            except ValueError as exc:
                raise OpenRouterError("OpenRouter returned invalid JSON.") from exc

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
):
    prompt = (
        "Fix the JSON to match this schema: {\"cards\": [{\"type\": \"basic|cloze\", "
        "\"front\": string?, \"back\": string?, \"cloze_text\": string?, "
        "\"extra\": string?, \"tags\": [string]}]}. "
        "Return only valid JSON."
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
        temperature=0,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        timeout_seconds=timeout_seconds,
    )
    content = response["choices"][0]["message"]["content"]
    return extract_json(content)
