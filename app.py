import os
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, jsonify
from werkzeug.utils import secure_filename
import tempfile
from dotenv import load_dotenv
import json
from flask_session import Session 

from gemini_utils import generate_anki_cards, initialize_gemini, set_api_key
from anki_utils import parse_cloze_cards, create_anki_deck, export_deck

# Load environment variables
load_dotenv()

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.urandom(24)

# Configure server-side session
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flask_session')
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
Session(app)  # Initialize Flask-Session

# Create a temporary directory for storing generated decks
TEMP_DIR = tempfile.mkdtemp()
# Ensure the temporary directory exists
os.makedirs(TEMP_DIR, exist_ok=True)

# File to store the API key
API_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api_key.json')

def save_api_key_to_file(api_key):
    """Save the API key to a file."""
    try:
        with open(API_KEY_FILE, 'w') as f:
            json.dump({'api_key': api_key}, f)
        return True
    except Exception as e:
        print(f"Error saving API key to file: {str(e)}")
        return False

def load_api_key_from_file():
    """Load the API key from a file."""
    try:
        if os.path.exists(API_KEY_FILE):
            with open(API_KEY_FILE, 'r') as f:
                data = json.load(f)
                return data.get('api_key')
    except Exception as e:
        print(f"Error loading API key from file: {str(e)}")
    return None

# Load API key at startup
api_key = load_api_key_from_file()
if api_key:
    set_api_key(api_key)

# Function to check if an error is a rate limit error
def is_rate_limit_error(error):
    """Check if the error is related to rate limiting."""
    error_str = str(error).lower()
    rate_limit_keywords = [
        'rate limit', 
        'too many requests', 
        'quota exceeded', 
        'resource exhausted',
        'ratelimit',
        '429'
    ]
    return any(keyword in error_str for keyword in rate_limit_keywords)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/about', methods=['GET'])
def about():
    # Check if we're coming from the loading page
    from_loading = False
    if 'redirect_from_loading' in session and session['redirect_from_loading']:
        from_loading = True
        # Clear the flag
        session.pop('redirect_from_loading', None)
    
    return render_template('about.html', from_loading=from_loading)

@app.route('/save_api_key', methods=['POST'])
def save_api_key():
    api_key = request.form.get('api_key', '').strip()
    
    # Check if this is a status check request
    if api_key == 'check_status_only':
        # Check both session and file for API key
        is_configured = ('gemini_api_key' in session and session['gemini_api_key']) or load_api_key_from_file() is not None
        return jsonify({
            'success': True,
            'is_update': is_configured,
            'message': 'API key status check'
        }), 200
    
    if not api_key:
        return jsonify({'success': False, 'message': 'API key cannot be empty'}), 400
    
    # Check if this is a new API key or an update
    is_update = 'gemini_api_key' in session or load_api_key_from_file() is not None
    
    # Store the API key in the session and in the file
    session['gemini_api_key'] = api_key
    save_api_key_to_file(api_key)
    
    # Set the API key for the current session
    try:
        set_api_key(api_key)
        return jsonify({
            'success': True, 
            'message': 'API key updated successfully' if is_update else 'API key saved successfully',
            'is_update': is_update
        }), 200
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error saving API key: {str(e)}'}), 500

@app.route('/loading', methods=['GET', 'POST'])
def loading():
    # For POST requests, store form data in session
    if request.method == 'POST':
        session['deck_name'] = request.form.get('deck_name', '').strip() or None
        session['focus_area'] = request.form.get('focus_area', 'balanced')
        # We no longer collect card_count from the user form
        session['card_count'] = 'all'  # Use 'all' to indicate we want to generate cards from all material
        # Store the selected AI model
        session['model_name'] = request.form.get('model_name', 'gemini-2.5-pro-preview-05-06')
        
        # Handle either text input or PDF upload
        pdf_file = request.files.get('pdf_file')
        notes_text = request.form.get('notes', '').strip()
        
        if pdf_file and pdf_file.filename:
            # Store the PDF file temporarily
            filename = secure_filename(pdf_file.filename)
            temp_path = os.path.join(TEMP_DIR, filename)
            pdf_file.save(temp_path)
            session['pdf_path'] = temp_path
            session['using_pdf'] = True
            session['notes_text'] = ''  # Clear text notes if using PDF
        elif notes_text:
            session['notes_text'] = notes_text
            session['using_pdf'] = False
            if 'pdf_path' in session:
                # Remove any previously stored PDF path
                session.pop('pdf_path', None)
        else:
            flash('Please enter some notes or upload a PDF file', 'error')
            return redirect(url_for('index'))
    
    # For both GET and POST, check if API key is set in session or file
    api_key = session.get('gemini_api_key') or load_api_key_from_file()
    if not api_key:
        # Store a flag to indicate we're coming from the loading page
        session['redirect_from_loading'] = True
        # Make sure the flag is set to True (not just present)
        session.modified = True
        flash('Please set your Gemini API key before generating cards', 'warning')
        # Add an anchor to the URL to scroll to the API key section
        return redirect(url_for('about') + '#api-key-section')
    else:
        # Ensure the API key is set in the session
        session['gemini_api_key'] = api_key
        # Set the API key for the current session
        set_api_key(api_key)
    
    # For GET requests, check if we have the necessary session data
    if request.method == 'GET' and not (('notes_text' in session and session['notes_text'].strip()) or 
                                        ('using_pdf' in session and session['using_pdf'] and 'pdf_path' in session)):
        flash('Please enter your notes or upload a PDF to generate cards', 'error')
        return redirect(url_for('index'))
    
    return render_template('loading.html')

@app.route('/generate', methods=['GET', 'POST'])
def generate():
    # If accessed directly via GET, redirect to index
    if request.method == 'GET':
        return redirect(url_for('index'))
    
    # Check for form data first, then fall back to session data
    notes_text = request.form.get('notes', '') or session.get('notes_text', '')
    using_pdf = session.get('using_pdf', False)
    pdf_path = session.get('pdf_path', None)
    deck_name = request.form.get('deck_name', '').strip() or session.get('deck_name')
    # Use 'all' as the default card_count to generate cards from all material
    card_count = request.form.get('card_count', '') or session.get('card_count', 'all')
    focus_area = request.form.get('focus_area', '') or session.get('focus_area', 'balanced')
    
    # Get the selected model name
    model_name = session.get('model_name', 'gemini-2.5-pro-preview-05-06')
    
    # Clear session data
    session.pop('notes_text', None)
    session.pop('deck_name', None)
    session.pop('card_count', None)
    session.pop('focus_area', None)
    
    # Validate input (either text or PDF must be provided)
    if not using_pdf and not notes_text.strip():
        flash('Please enter some notes or lecture content or upload a PDF', 'error')
        return redirect(url_for('index'))
    
    try:
        # Check if user has provided a custom API key (in session or file)
        api_key = session.get('gemini_api_key') or load_api_key_from_file()
        if api_key:
            # Use the custom API key
            set_api_key(api_key)
        
        # Generate Anki cards using Gemini API
        if using_pdf and pdf_path and os.path.exists(pdf_path):
            # Generate cards from PDF
            generated_text = generate_anki_cards(pdf_path, card_count=card_count, focus_area=focus_area, is_pdf_path=True, model_name=model_name)
            # Clean up the PDF file after use
            try:
                os.remove(pdf_path)
            except Exception as e:
                print(f"Warning: Could not remove temporary PDF file: {str(e)}")
            # Clear PDF data from session
            session.pop('using_pdf', None)
            session.pop('pdf_path', None)
        else:
            # Generate cards from text
            generated_text = generate_anki_cards(notes_text, card_count=card_count, focus_area=focus_area, model_name=model_name)
        
        # Parse the generated text into cards
        cards = parse_cloze_cards(generated_text)
        
        if not cards:
            flash('No valid cloze deletion cards were generated. Please try again with different notes.', 'error')
            return redirect(url_for('index'))
        
        # Set a default deck name if none provided
        if not deck_name:
            # Try to generate a meaningful name from the first few words of the notes
            words = notes_text.split()[:3]
            if len(words) > 0:
                deck_name = f"AnkiGPT_{' '.join(words)}"
            else:
                deck_name = "AnkiGPT_Deck"
        
        # For simplicity, we'll just pass the data directly to the template
        return render_template('preview.html', 
                              cards=cards, 
                              deck_name=deck_name,
                              notes_text=notes_text)
        
    except Exception as e:
        # Check if this is a rate limit error
        if is_rate_limit_error(e):
            flash('Rate limit exceeded. Please wait a moment before trying again.', 'rate-limit')
        else:
            flash(f'Error generating cards: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/regenerate_card', methods=['POST'])
def regenerate_card():
    try:
        # Get data from the request
        card_text = request.form.get('card_text', '')
        card_index = request.form.get('card_index', '')
        notes_text = request.form.get('notes_text', '')
        
        # Validate input
        if not card_text.strip() or not notes_text.strip() or not card_index:
            return jsonify({'success': False, 'message': 'Missing required parameters'}), 400
        
        # Check if API key is set
        api_key = session.get('gemini_api_key') or load_api_key_from_file()
        if not api_key:
            return jsonify({'success': False, 'message': 'API key not set'}), 400
        
        # Set the API key
        set_api_key(api_key)
        
        # Always use the Flash model for regeneration for faster response
        model_name = "gemini-2.5-flash-preview-05-20"
        
        # Generate a new card using a specialized prompt
        prompt = f"""
        You are an expert educator specializing in creating high-quality Anki flashcards. Your task is to fix and improve the original card shown below by creating a better version that addresses common issues.

        ANALYSIS OF ORIGINAL CARD:
        The original card may have one or more of these issues:
        1. Poorly placed cloze deletion (hiding text that's too obvious, too difficult, or lacks sufficient context)
        2. Assumes prior knowledge that isn't included in the card itself
        3. Contains vague references or unclear language
        4. Lacks sufficient context to understand what's being tested
        5. Tests multiple concepts in a confusing way

        INSTRUCTIONS FOR CREATING AN IMPROVED CARD:
        1. Create ONE improved cloze deletion card that tests the SAME CONCEPT as the original card.
        2. Use the format: {{{{c1::text to be hidden}}}} for the cloze deletions.
        3. Make the card COMPLETELY SELF-CONTAINED - include all necessary context within the card itself.
        4. Place the cloze deletion strategically:
           - Hide key terms, concepts, or relationships that are worth memorizing
           - Ensure the surrounding text provides clear context for what's being tested
           - Don't hide information that's too obvious or too obscure
        5. Use precise, specific language - avoid vague terms like "this," "these," or "it" without clear referents.
        6. Keep the card focused on a single, clear concept from the notes.
        7. Use clear, concise language that requires no external knowledge to understand.
        8. Ensure the card is factually accurate and directly based on the provided notes.
        
        ORIGINAL CARD TO IMPROVE:
        {card_text}
        
        NOTES (SOURCE MATERIAL):
        {notes_text}
        
        RESPONSE FORMAT:
        Return ONLY the improved card text with no additional explanation or commentary.
        """
        
        # Initialize Gemini and generate the new card
        genai_module = initialize_gemini()
        genai_module.configure(api_key=api_key)
        model = genai_module.GenerativeModel(model_name)
        response = model.generate_content(prompt)
        
        # Get the generated text and clean it up
        new_card_text = response.text.strip()
        
        # Validate that the response contains a cloze deletion
        if '{{c' not in new_card_text or '::' not in new_card_text or '}}' not in new_card_text:
            return jsonify({'success': False, 'message': 'Failed to generate a valid cloze card'}), 400
        
        return jsonify({
            'success': True,
            'card_text': new_card_text,
            'card_index': card_index
        }), 200
        
    except Exception as e:
        # Check if this is a rate limit error
        if is_rate_limit_error(e):
            return jsonify({
                'success': False, 
                'message': 'Rate limit exceeded. Please wait a moment before trying again.',
                'is_rate_limit': True
            }), 429
        return jsonify({'success': False, 'message': f'Error regenerating card: {str(e)}'}), 500

@app.route('/export', methods=['POST'])
def export():
    try:
        # Get data from the form
        cards_data = request.form.getlist('card_text')
        deck_name = request.form.get('deck_name', '').strip() or None
        
        # Convert to the format expected by create_anki_deck
        cards = [{'text': text, 'extra': '', 'source': 'AnkiGPT'} for text in cards_data if text.strip()]
        
        if not cards:
            flash('No cards to export', 'error')
            return redirect(url_for('index'))
        
        # Create and export the deck
        deck = create_anki_deck(cards, deck_name)
        
        # Ensure the temp directory exists
        os.makedirs(TEMP_DIR, exist_ok=True)
        
        # Make sure the deck name is safe for file paths
        safe_deck_name = secure_filename(deck.name)
        deck.name = safe_deck_name
        
        output_file = export_deck(deck, TEMP_DIR)
        
        # Store the deck name in session for the success page
        session['exported_deck_name'] = deck.name
        session['exported_card_count'] = len(cards)
        
        # Send the file to the user and redirect to success page
        response = send_file(output_file, as_attachment=True, download_name=f"{deck.name}.apkg")
        
        # Set a cookie to indicate successful export
        response.set_cookie('export_success', 'true', max_age=300)  # 5 minutes expiry
        
        return response
        
    except Exception as e:
        flash(f'Error exporting deck: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/success', methods=['GET'])
def success():
    # Check if the user has just exported a deck
    if request.cookies.get('export_success') == 'true':
        # Get the deck name from session
        deck_name = session.get('exported_deck_name', 'AnkiGPT Deck')
        card_count = session.get('exported_card_count', 0)
        
        # Clear the session data
        session.pop('exported_deck_name', None)
        session.pop('exported_card_count', None)
        
        return render_template('success.html', deck_name=deck_name, card_count=card_count)
    else:
        # If not coming from export, redirect to index
        return redirect(url_for('index'))

if __name__ == '__main__':
    # Create templates directory if it doesn't exist
    if not os.path.exists('templates'):
        os.makedirs('templates')
    
    # Create static directory if it doesn't exist
    if not os.path.exists('static'):
        os.makedirs('static')
    
    # Create flask_session directory if it doesn't exist
    session_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flask_session')
    if not os.path.exists(session_dir):
        os.makedirs(session_dir)
    
    # Ensure the API key file directory exists
    os.makedirs(os.path.dirname(API_KEY_FILE), exist_ok=True)
    
    app.run(debug=True)