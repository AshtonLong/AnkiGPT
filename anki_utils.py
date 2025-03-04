import genanki
import random
import re
import os
from datetime import datetime

# Define the Anki note model for Cloze cards
CLOZE_MODEL = genanki.Model(
    random.randrange(1 << 30, 1 << 31),  # Random model ID
    'AnkiGPT Cloze Model',
    fields=[
        {'name': 'Text'},
        {'name': 'Extra'},
        {'name': 'Source'},
    ],
    templates=[
        {
            'name': 'Cloze Card',
            'qfmt': '''
<div class="ankigpt-card">
    <div class="ankigpt-content">
        {{cloze:Text}}
    </div>
    {{#Extra}}
    <div class="ankigpt-extra">
        {{Extra}}
    </div>
    {{/Extra}}
</div>
''',
            'afmt': '''
<div class="ankigpt-card">
    <div class="ankigpt-content">
        {{cloze:Text}}
    </div>
    {{#Extra}}
    <div class="ankigpt-extra">
        {{Extra}}
    </div>
    {{/Extra}}
    <div class="ankigpt-source">
        Source: {{Source}}
    </div>
</div>
''',
        },
    ],
    css='''
/* AnkiGPT Card Styling - Enhanced to match site style */
.card {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    color: #dee2e6;
    border-radius: 16px;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.15), 0 1px 3px rgba(0, 0, 0, 0.1);
    padding: 28px;
    margin: 0 auto;
    overflow: hidden;
    max-width: 600px;
    text-align: left;
    line-height: 1.7;
    border: 1px solid rgba(255, 255, 255, 0.08);
    position: relative;
}

.card::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 5px;
    background: linear-gradient(90deg, #4361ee, #4cc9f0, #f72585);
    opacity: 0.8;
}

.nightMode .card {
    background: linear-gradient(135deg, #121220 0%, #14192e 100%);
    color: #dee2e6;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.25), 0 1px 3px rgba(0, 0, 0, 0.15);
}

.ankigpt-card {
    display: flex;
    flex-direction: column;
    min-height: 100%;
    position: relative;
    z-index: 1;
}

.ankigpt-content {
    font-size: 1.3rem;
    padding-bottom: 20px;
    letter-spacing: -0.01em;
    font-weight: 400;
    line-height: 1.75;
}

.ankigpt-content .cloze {
    font-weight: 600;
    color: #4361ee;
    position: relative;
    padding: 0 5px;
    margin: 0 2px;
    display: inline-block;
    border-radius: 4px;
    background-color: rgba(67, 97, 238, 0.08);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
}

.nightMode .ankigpt-content .cloze {
    color: #4cc9f0;
    background-color: rgba(76, 201, 240, 0.08);
}

.ankigpt-content .cloze-brackets {
    display: none;
}

.ankigpt-content .cloze-hidden {
    border-bottom: 2px dashed rgba(76, 201, 240, 0.6);
    padding: 0 5px;
    margin: 0 2px;
    color: transparent;
    background-color: rgba(76, 201, 240, 0.08);
    border-radius: 4px;
}

.ankigpt-extra {
    font-size: 1.05rem;
    color: #bdc3cd;
    border-top: 1px solid rgba(222, 226, 230, 0.2);
    padding-top: 20px;
    margin-top: 10px;
    font-style: italic;
    line-height: 1.6;
}

.ankigpt-source {
    margin-top: 25px;
    font-size: 0.85rem;
    color: #8c95a0;
    text-align: right;
    font-style: italic;
    border-top: 1px solid rgba(222, 226, 230, 0.2);
    padding-top: 15px;
}

/* Style for cloze hints */
.cloze-hint {
    font-size: 0.9em;
    color: #f9c74f;
    font-style: italic;
    margin-left: 8px;
    opacity: 0.9;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
}

/* Styling for images if present */
img {
    max-width: 100%;
    border-radius: 12px;
    margin: 15px 0;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
}

/* Base styling for elements */
p {
    margin-bottom: 12px;
}

strong, b {
    font-weight: 600;
    color: #f72585;
    text-shadow: 0 0 1px rgba(247, 37, 133, 0.2);
}

em, i {
    font-style: italic;
    color: #4cc9f0;
}

/* List styling */
ul, ol {
    padding-left: 25px;
    margin-bottom: 18px;
}

li {
    margin-bottom: 8px;
    position: relative;
}

ul li::marker {
    color: #4cc9f0;
}

ol li::marker {
    color: #f72585;
    font-weight: 600;
}

/* Code blocks if any */
code, pre {
    font-family: 'Fira Code', 'JetBrains Mono', 'Courier New', monospace;
    background-color: rgba(30, 30, 50, 0.7);
    border-radius: 6px;
    padding: 2px 6px;
    font-size: 0.9em;
    border: 1px solid rgba(255, 255, 255, 0.08);
    box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.2);
}

pre {
    padding: 15px;
    overflow-x: auto;
    margin: 15px 0;
    line-height: 1.5;
}

/* Table styling */
table {
    width: 100%;
    border-collapse: collapse;
    margin: 15px 0;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

th {
    background-color: rgba(67, 97, 238, 0.2);
    padding: 12px;
    border: 1px solid rgba(255, 255, 255, 0.1);
    font-weight: 600;
    color: #dee2e6;
}

td {
    padding: 10px;
    border: 1px solid rgba(255, 255, 255, 0.1);
    background-color: rgba(30, 30, 50, 0.5);
}

/* Blockquote styling */
blockquote {
    border-left: 4px solid #4361ee;
    padding: 10px 15px;
    margin: 15px 0;
    background-color: rgba(67, 97, 238, 0.08);
    border-radius: 0 8px 8px 0;
    font-style: italic;
}

/* Definition term styling */
dt {
    font-weight: 600;
    color: #4cc9f0;
    margin-top: 10px;
}

dd {
    margin-left: 20px;
    margin-bottom: 10px;
}
''',
    model_type=genanki.Model.CLOZE
)

def parse_cloze_cards(text):
    """
    Parse the generated text into individual Anki cloze cards.
    
    Args:
        text (str): The text containing cloze deletion cards
        
    Returns:
        list: List of dictionaries with card text
    """
    # Split by newlines and filter out empty lines
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    cards = []
    for line in lines:
        # Check if the line contains a cloze deletion
        if '{{c' in line and '::' in line and '}}' in line:
            cards.append({
                'text': line,
                'extra': '',
                'source': 'AnkiGPT'
            })
    
    return cards

def create_anki_deck(cards, deck_name=None):
    """
    Create an Anki deck from the parsed cards.
    
    Args:
        cards (list): List of card dictionaries
        deck_name (str, optional): Name for the deck
        
    Returns:
        genanki.Deck: The created Anki deck
    """
    if not deck_name:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        deck_name = f"AnkiGPT_Deck_{timestamp}"
    
    # Create a unique deck ID
    deck_id = random.randrange(1 << 30, 1 << 31)
    deck = genanki.Deck(deck_id, deck_name)
    
    for card in cards:
        note = genanki.Note(
            model=CLOZE_MODEL,
            fields=[card['text'], card['extra'], card['source']]
        )
        deck.add_note(note)
    
    return deck

def export_deck(deck, output_dir='.'):
    """
    Export the Anki deck to a .apkg file.
    
    Args:
        deck (genanki.Deck): The Anki deck to export
        output_dir (str): Directory to save the exported deck
        
    Returns:
        str: Path to the exported deck file
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    output_file = os.path.join(output_dir, f"{deck.name}.apkg")
    package = genanki.Package(deck)
    package.write_to_file(output_file)
    
    return output_file 