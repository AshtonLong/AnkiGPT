import json
import requests


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def build_headers(api_key, site_url, app_name):
    headers = {"Authorization": f"Bearer {api_key}"}
    if site_url:
        headers["HTTP-Referer"] = site_url
    if app_name:
        headers["X-Title"] = app_name
    return headers


def openrouter_chat(messages, model, api_key, site_url="", app_name="", temperature=0.2):
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    headers = build_headers(api_key, site_url, app_name)
    response = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=120)
    response.raise_for_status()
    return response.json()


def extract_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def repair_json(raw_text, model, api_key, site_url="", app_name=""):
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
    response = openrouter_chat(messages, model, api_key, site_url, app_name, temperature=0)
    content = response["choices"][0]["message"]["content"]
    return extract_json(content)
