import os
from typing import Optional
from dotenv import load_dotenv
import requests
from pypdf import PdfReader

# Load environment variables
load_dotenv()

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Global variable to store the API key
_api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")

def set_api_key(api_key: str) -> bool:
    """Set the OpenRouter API key."""
    global _api_key
    _api_key = api_key
    return True

def initialize_gemini():
    """Compatibility stub for previous interface."""
    # No-op; retained so imports in app remain valid if referenced elsewhere
    return None

def _ensure_api_key() -> str:
    global _api_key
    if not _api_key:
        env_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
        if not env_key:
            raise ValueError("OPENROUTER_API_KEY environment variable not set")
        _api_key = env_key
    return _api_key

def _chat_completion(prompt: str, model: str) -> str:
    """Call OpenRouter Chat Completions and return text content."""
    api_key = _ensure_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Optional headers recommended by OpenRouter for rate-limiting context
        "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "http://localhost"),
        "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "AnkiGPT"),
    }
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are an expert educator who writes high-quality, self-contained Anki cloze cards in plain text."
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    resp = requests.post(OPENROUTER_API_URL, json=body, headers=headers, timeout=120)
    if resp.status_code == 429:
        # Surface rate limit clearly to caller
        raise RuntimeError("Rate limit exceeded (429) from OpenRouter")
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"OpenRouter error {resp.status_code}: {detail}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        raise RuntimeError("Unexpected OpenRouter response format")

def _extract_text_from_pdf(pdf_path: str) -> str:
    reader = PdfReader(pdf_path)
    texts = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(filter(None, texts)).strip()

def generate_anki_cards(input_content: str, card_count: str = '15-25', focus_area: str = 'balanced', is_pdf_path: bool = False, model_name: Optional[str] = None) -> str:
    """Generate Anki Cloze deletion cards using OpenRouter models.

    Args:
        input_content: The notes text OR path to a PDF file
        card_count: Target number range (advisory; model creates as many as needed)
        focus_area: 'balanced' | 'definitions' | 'relationships' | 'processes' | 'examples'
        is_pdf_path: Whether input_content is a path to a PDF
        model_name: OpenRouter model, defaults to 'openai/gpt-oss-120b'
    """
    # Default to the larger OSS model for thoroughness
    if not model_name:
        model_name = "openai/gpt-oss-120b"

    # Adjust focus based on user preference
    focus_instruction = ""
    if focus_area == 'definitions':
        focus_instruction = "Focus primarily on key terms, concepts, and definitions."
    elif focus_area == 'relationships':
        focus_instruction = "Focus primarily on relationships between concepts, cause-effect connections, and comparisons."
    elif focus_area == 'processes':
        focus_instruction = "Focus primarily on processes, sequences, steps, and methodologies."
    elif focus_area == 'examples':
        focus_instruction = "Focus primarily on examples, applications, and case studies that illustrate the concepts."

    # Extract text from PDF if needed
    notes_text = _extract_text_from_pdf(input_content) if is_pdf_path else input_content

    prompt = f"""
You are an expert educator specializing in creating high-quality Anki flashcards. Convert the following notes into comprehensive Anki Cloze deletion cards.

INSTRUCTIONS:
1. Create as many cloze deletion cards as needed to thoroughly cover ALL important concepts, definitions, relationships, and examples from the notes. Don't limit yourself to a fixed number - create as many high-quality cards as the material requires (guideline: {card_count} cards if appropriate).
2. Use the format: {{{{c1::text to be hidden}}}} for the cloze deletions.
3. Each card must be on a new line and contain ONLY ONE cloze deletion.
4. CRITICAL: Each card MUST be COMPLETELY SELF-CONTAINED with ALL necessary context. Cards will be seen in RANDOM ORDER.
5. Avoid vague references; explicitly name entities and concepts.
6. Ensure surrounding text provides sufficient context to understand what's being tested.
7. Create a mix of cards that test definitions, relationships, processes, and examples.
8. Ensure cards are factually accurate and directly based on the provided notes.
9. Use clear, concise language. Do NOT include markdown or a numbered list.
10. If there are equations, include them in LaTeX between \\( \\).
{focus_instruction}

NOTES:
{notes_text}
"""

    return _chat_completion(prompt, model=model_name)

def generate_improved_card(notes_text: str, original_card_text: str, model_name: Optional[str] = None) -> str:
    """Generate an improved version of a single cloze card."""
    if not model_name:
        # Use the smaller model for speed when regenerating a single card
        model_name = "openai/gpt-oss-20b"

    prompt = f"""
You are an expert educator specializing in creating high-quality Anki flashcards. Improve the original card by creating ONE better self-contained cloze card that tests the SAME CONCEPT.

Rules:
1. Use format {{{{c1::text}}}} for the cloze deletion.
2. Include all necessary context; no external knowledge assumed.
3. Use precise language; avoid vague references.
4. Focus on a single, clear concept from the notes.
5. Return ONLY the improved card text, no extra commentary.

ORIGINAL CARD:
{original_card_text}

NOTES:
{notes_text}
"""
    return _chat_completion(prompt, model=model_name)

