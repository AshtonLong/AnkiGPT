import os
import google.generativeai as genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Global variable to store the API key
_api_key = os.environ.get("GEMINI_API_KEY")

def set_api_key(api_key):
    """Set the Gemini API key."""
    global _api_key
    _api_key = api_key
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

def generate_anki_cards(input_content, card_count='15-25', focus_area='balanced', is_pdf_path=False):
    """
    Generate Anki Cloze deletion cards from notes or PDF using Gemini API.
    
    Args:
        input_content (str): The notes text OR path to a PDF file
        card_count (str): Number of cards to generate (e.g., '10-15', '20-30')
        focus_area (str): Focus area for cards ('balanced', 'definitions', 'relationships', 'processes', 'examples')
        is_pdf_path (bool): Whether input_content is a path to a PDF file
        
    Returns:
        str: Generated Anki cards in Cloze deletion format
    """
    genai_module = initialize_gemini()
    genai_module.configure(api_key=_api_key)
    model = genai_module.GenerativeModel("gemini-2.5-pro-exp-03-25")
    
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
    
    # Create the prompt for text input
    prompt_template = f"""
    You are an expert educator specializing in creating high-quality Anki flashcards. Your task is to convert the following notes into comprehensive Anki Cloze deletion cards.

    INSTRUCTIONS:
    1. Create as many cloze deletion cards as needed to thoroughly cover ALL important concepts, definitions, relationships, and examples from the notes. Don't limit yourself to a fixed number - create as many high-quality cards as the material requires.
    2. Use the format: {{{{c1::text to be hidden}}}} for the cloze deletions.
    3. Each card should be on a new line.
    4. CRITICAL: Each card MUST be COMPLETELY SELF-CONTAINED with ALL necessary context. Students will see these cards in RANDOM ORDER, so each card must make perfect sense on its own without requiring knowledge from other cards or external sources.
    5. Include ALL relevant context within each card - never assume the student remembers information from other cards or external sources.
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
    15. Do NOT enumerate the cards, i.e. 1. text, 2. text, 3. text, etc.
    16. If there are math equations, always include them in the card in LaTeX format using the \\(\\) syntax. For example:
        - "The formula for the area of a circle is \\(A = \\pi r^2\\), where \\(r\\) is the radius of the circle."
        - "According to Einstein's mass-energy equivalence, \\(E = mc^2\\), where \\(E\\) represents {{{{c1::energy}}}}, \\(m\\) represents mass, and \\(c\\) represents the speed of light."\
    17. You are forbidden from using * or ** in the cards. This is NOT markdown. This does not bold text.
    {focus_instruction}
    """
    
    # If input is a PDF file
    if is_pdf_path:
        # Open the PDF file for the model to process
        with open(input_content, "rb") as f:
            pdf_data = f.read()
        
        # Create the PDF prompt with the same instructions
        prompt = prompt_template + "\n\nPlease analyze the attached PDF and create Anki Cloze deletion cards based on its content."
        
        # Generate content with the PDF
        response = model.generate_content([
            prompt,
            {
                "mime_type": "application/pdf",
                "data": pdf_data
            }
        ])
    else:
        # For text input, use the original approach
        prompt = prompt_template + f"\n\nNOTES:\n{input_content}"
        response = model.generate_content(prompt)
    
    return response.text 