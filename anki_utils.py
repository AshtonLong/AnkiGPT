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
            'qfmt': '{{cloze:Text}}<br><br>{{Extra}}',
            'afmt': '{{cloze:Text}}<br><br>{{Extra}}<hr id="source">Source: {{Source}}',
        },
    ],
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