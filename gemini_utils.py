import os
import google.generativeai as genai
from google.generativeai import types
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Global variable to store the API key
_api_key = os.environ.get("GEMINI_API_KEY")

def set_api_key(api_key):
    """Set the Gemini API key."""
    global _api_key
    _api_key = api_key
    # Configure the API with the new key
    genai.configure(api_key=_api_key)
    return True

def initialize_gemini():
    """Initialize the Gemini API client."""
    global _api_key
    if not _api_key:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        _api_key = api_key
    
    return genai

def generate_anki_cards(notes_text, card_count='15-25', focus_area='balanced'):
    """
    Generate Anki Cloze deletion cards from notes using Gemini API.
    
    Args:
        notes_text (str): The notes or lecture content to process
        card_count (str): Number of cards to generate (e.g., '10-15', '20-30')
        focus_area (str): Focus area for cards ('balanced', 'definitions', 'relationships', 'processes', 'examples')
        
    Returns:
        str: Generated Anki cards in Cloze deletion format
    """
    genai_module = initialize_gemini()
    genai_module.configure(api_key=_api_key)
    model = genai_module.GenerativeModel("gemini-2.0-pro-exp-02-05")
    
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
    
    prompt = f"""
    You are an expert educator specializing in creating high-quality Anki flashcards. Your task is to convert the following notes into comprehensive Anki Cloze deletion cards.

    INSTRUCTIONS:
    1. Create {card_count} cloze deletion cards that thoroughly cover ALL important concepts, definitions, relationships, and examples from the notes.
    2. Use the format: {{{{c1::text to be hidden}}}} for the cloze deletions.
    3. Each card should be on a new line.
    4. CRITICAL: Each card MUST be COMPLETELY SELF-CONTAINED with ALL necessary context. Students will see these cards in RANDOM ORDER, so each card must make perfect sense on its own without requiring knowledge from other cards or external sources.
    5. Include ALL relevant context within each card - never assume the student remembers information from other cards.
    6. Avoid vague references like "this process" or "these components" - always explicitly name what you're referring to.
    7. For each cloze deletion, ensure the surrounding text provides sufficient context to understand what's being tested.
    8. Create a mix of cards that test:
       - Key definitions (e.g., "A {{{{c1::mitochondrion}}}} is the powerhouse of the cell that produces ATP through cellular respiration")
       - Important relationships (e.g., "In the endocrine system, {{{{c1::increased insulin}}}} leads to decreased blood glucose levels by facilitating glucose uptake into cells")
       - Processes and sequences (e.g., "During DNA replication, the enzyme {{{{c1::helicase}}}} unwinds the double helix before DNA polymerase begins synthesis of the new strand")
       - Examples that illustrate concepts (e.g., "{{{{c1::The French Revolution (1789-1799)}}}} exemplifies social upheaval driven by class inequality, where the bourgeoisie overthrew the aristocracy")
    9. For complex topics, create multiple cards that approach the concept from different angles, ensuring each card stands alone.
    10. Avoid creating cards that are too simple or test trivial information.
    11. Ensure cards are factually accurate and directly based on the provided notes.
    12. For numerical data or specific facts, always include the complete information in the card.
    13. Use clear, concise language that a student can understand without additional explanation.
    14. When referencing a concept that appears in multiple cards, always reintroduce it with sufficient context.
    {focus_instruction}

    NOTES:
    {notes_text}
    """
    
    response = model.generate_content(prompt)
    
    return response.text 