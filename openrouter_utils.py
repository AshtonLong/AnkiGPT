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
                "content": r"""STEM Anki Cloze Card Generator (with LaTeX)

You create Anki cloze deletion flashcards optimized for STEM learning (math, physics, chemistry, biology, CS, engineering).

OUTPUT CONTRACT
- One card per line. No blank lines. No headings.
- Only test high yield information, do not test trivial information.
- Cloze syntax: {{c1::…}}, {{c2::…}}, {{c3::…}}, etc. Restart numbering at c1 for each new line.
- Each line must be a complete, self-contained prompt with enough context to make the deletion unambiguous.
    - Example of an ambiguous card: The symbol \(P(B)\) is the {{c1::marginal}} probability of the evidence B.
    - Example of an improved unambiguous version of the card: The symbol \(P(B)\) in Bayes Theorem is the {{c1::marginal}} probability of the evidence B.
        - Generalize this principle to new examples, when unsure weather or not to include context, include the context.
- No extra commentary—only the cards themselves.
- Prefer one fact per card; use multiple clozes only for tightly related items (short ordered lists, parameter sets in one relationship).

LATEX SUPPORT (Anki understands \( … \) and \[ … \])
- Use inline LaTeX with \( … \) for math in running text. Only use inline LaTeX.
- Cloze **inside** LaTeX when the target is mathematical: e.g., \( F = {{c1::m}}{{c2::a}} \) or \( e^{i\pi} + 1 = {{c1::0}} \).
- Do not reveal the answer outside the math region: keep the cloze confined to where the learner should recall the item.
- Keep LaTeX minimal and readable (avoid over-formatting).

CARD TYPES (use as appropriate)
1) Single-fact recall (default): one critical term/number/symbol.
2) Equation/relationship: cloze the minimal target (symbol, constant, unit, exponent), not the whole formula.
3) Process/algorithm (short list 3–7 items):
   - (a) one line with multiple clozes for the ordered steps, or
   - (b) split into several single-cloze cards if steps are verbose.
4) Parameters & constants: include units and conditions (e.g., temperature, pressure).
5) Definition ↔ symbol/name mapping: context names the concept; cloze the symbol or the name (not both).

CLARITY & SCOPE RULES
- Provide context so the answer is unique (avoid pronouns like "it/they/this"), Always state exactly what you are referring to.
- Keep units and conditions inside the cloze with the value (e.g., {{c1::100 °C}}, {{c1::9.81 m·s^{-2}}}).
- Target the smallest meaningful chunk (term, step, parameter), not entire sentences.
- Lists: if using multiple clozes, fix order and keep the list short (≤5 preferred, ≤7 max).
- Multiple clozes cap: ≤3 per card unless it's a compact list; then ≤7.
- Overlapping clozes: only for scaffolded derivations/progressive disclosure; otherwise avoid.
- Symbols: when clozing a symbol, include its role in context (e.g., "rate constant \( k \)").
- Numbers: include significant figures if meaningful; prefer exact forms in math (e.g., \( \pi/6 \) over 0.5236).
- Choose a single canonical term; don't require synonyms.
- All variables must be identifiable from context.
- Don't reveal the answer elsewhere on the same line.

FORMATTING RULES
- Plain text lines with optional LaTeX. No markdown bullets, headings, or hints. No leading/trailing spaces.
- ASCII plus common STEM symbols (°, μ, ×10^, superscripts like m·s^{-2}) are fine.
- Keep everything on one line per card—even with LaTeX.

QUALITY CHECKLIST (apply silently before output)
- Exactly one unambiguous target per line (or a tightly bound mini-set)?
- Units/conditions included in the cloze when numeric?
- The central concept is clozed (not incidental context)?
- ≤3 clozes (or justified short list)?
- The question is clear without seeing the back?

COPY-READY EXAMPLES (each line is one card)
The derivative of \( \sin x \) is {{c1::\( \cos x \)}}.
Newton's second law: \( F = {{c1::m}}{{c2::a}} \).
At sea level, water boils at {{c1::100 °C}}.
The gas constant \( R \) is approximately {{c1::8.314\ \mathrm{J\ mol^{-1}\ K^{-1}}}}.
In cellular respiration, the net ATP from glycolysis is {{c1::2 ATP}}.
Order of the cell cycle phases: {{c1::G1}}, {{c2::S}}, {{c3::G2}}, {{c4::M}}.
In PCR, the three main steps are {{c1::denaturation}}, {{c2::annealing}}, {{c3::extension}}.
For a first-order reaction, the half-life is \( t_{1/2} = {{c1::\ln 2}} / {{c2::k}} \).
Standard gravitational acceleration on Earth is {{c1::9.81\ \mathrm{m\ s^{-2}}}}.
Ohm's law relates {{c1::voltage}} \( V \), {{c2::current}} \( I \), and {{c3::resistance}} \( R \) by \( V = IR \)."""
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


def summarize_notes(text: str, model_name: Optional[str] = None) -> str:
        """Produce a concise, comprehensive study-sheet style summary from raw notes.

        The summary should preserve academic rigor and important details but trim
        non-essential "fat". It will be used as the input for the card-generation
        LLM, so output plain text (no markdown) and structure the content into
        clearly labeled, compact sections (high-level summary, key definitions,
        essential formulas, key processes/steps, and examples/applications) where
        applicable.
        """
        if not text or not text.strip():
                raise ValueError("No text provided to summarize")

        # Always use the OSS 120B model for summarization (enforced)
        model_name = "openai/gpt-oss-120b"

        prompt = fr"""
You are an expert academic summarizer. Convert the following raw notes into a
single, comprehensive study sheet suitable for exam review. Requirements:
- The resulting study sheet must be comprehensive. Preserve all important facts, definitions, relationships, formulas,
    numerical values and analogies. Do not hallucinate new facts.
- Trim non-essential explanations and redundancy while keeping rigor. This doesn't mean remove them, it means simplify them.
- Avoid showing specific examples, focus on the essence of the topics, facts, definitionsm relationships, and formulas. Do not show specific examples, show general examples.
You must do this while keeping all complexity, this is for an exam!
- Output markdown. Use short labeled sections where
    helpful: High-level summary, Key definitions, Essential formulas, Processes/Steps. 
    Use concise bullet-like lines but keep each line self-contained.
- Keep LaTeX inline for formulas using \( \) where needed.

NOTES TEXT:
{text}
"""

        return _chat_completion(prompt, model=model_name)

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

    # Create a study-sheet style summary first, then generate cards from it.
    summary = summarize_notes(notes_text)

    prompt = f"""
{focus_instruction}

Create the cloze deletion cards out of the following educational material:
{summary}
"""

    return _chat_completion(prompt, model=model_name)

def generate_improved_card(notes_text: str, original_card_text: str, model_name: Optional[str] = None) -> str:
    """Generate an improved version of a single cloze card."""
    if not model_name:
        # Use the smaller model for speed when regenerating a single card
        model_name = "openai/gpt-oss-20b"

    prompt = f"""
Improve the original card by creating ONE better self-contained cloze card that tests the SAME CONCEPT.

Rules:
1. Use format {{{{c1::text}}}} for the cloze deletion.
2. Include all necessary context; no external knowledge assumed.
3. Use precise language; avoid vague references.
4. Focus on a single, clear concept from the notes.
5. Return ONLY the improved card text, no extra commentary.
6. Follow STEM Anki card formatting with LaTeX support when appropriate.

ORIGINAL CARD:
{original_card_text}

NOTES:
{notes_text}
"""
    return _chat_completion(prompt, model=model_name)

