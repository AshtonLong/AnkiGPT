# AnkiGPT

AnkiGPT is a powerful tool that automatically generates high-quality Anki flashcards from your study notes or PDF files using Google's Gemini AI. Perfect for students, educators, and lifelong learners looking to optimize their study process.

## Features

- **AI-Powered Card Generation**: Convert notes or PDF files into effective Anki flashcards
- **Cloze Deletion Format**: Creates cards in the proven cloze deletion format for better retention
- **Customizable Output**: Control the number of cards and focus areas (definitions, processes, etc.)
- **Preview & Edit**: Review and modify generated cards before exporting
- **Regeneration Options**: Easily regenerate individual cards if needed
- **Direct Anki Export**: Export directly to Anki-compatible .apkg format
- **Beautiful Styling**: Pre-styled cards that enhance the learning experience

## Getting Started

### Prerequisites

- Python 3.8 or higher
- Google Gemini API key

### Installation

1. Clone the repository:
   ```
   git clone https://github.com/AshtonLong/AnkiGPT.git
   cd AnkiGPT
   ```

2. Install required dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the project root (optional):
   ```
   GEMINI_API_KEY=your_gemini_api_key_here
   ```
   Alternatively, you can input your API key through the web interface.

### Running the Application

Start the Flask development server:
```
python app.py
```

Then access the web interface at http://localhost:5000 in your browser.

## Usage Guide

1. **Enter Your API Key**:
   - Access the About page to enter your Google Gemini API key
   - This key is saved for future use

2. **Input Study Material**:
   - Paste text notes or upload a PDF file
   - Provide a name for your deck
   - Select the desired number of cards
   - Choose a focus area (balanced, definitions, relationships, processes, or examples)

3. **Generate Cards**:
   - The application will process your input and generate Anki cards
   - This process may take a few moments depending on the length of your notes

4. **Preview & Edit**:
   - Review all generated cards
   - Edit or regenerate individual cards as needed

5. **Export to Anki**:
   - Export your cards to an Anki-compatible .apkg file
   - Import the file into Anki to start studying

## Technical Details

AnkiGPT is built with:
- Flask web framework
- Google Generative AI (Gemini 2.5 Pro)
- Genanki for Anki package creation
- Modern HTML/CSS/JavaScript interface

## Customizing Card Generation

You can customize the generated cards by selecting different focus areas:
- **Balanced**: Equal emphasis on all types of information
- **Definitions**: Focus on key terms and concepts
- **Relationships**: Emphasize connections between concepts
- **Processes**: Highlight procedures and sequences
- **Examples**: Prioritize practical examples and applications (experimental setting)

## Troubleshooting

### API Key Issues
- Ensure your Google Gemini API key is valid and has sufficient quota
- The application will store your API key locally for convenience

### Rate Limiting
- If you encounter rate limit errors, wait a few minutes before trying again
- Consider using smaller chunks of text for each generation

### PDF Processing
- For large PDFs, consider splitting them into smaller sections
- Some heavily formatted PDFs may not parse optimally

## Acknowledgments

- Built with Google's Gemini AI
- Uses the Genanki library for Anki integration
- Inspired by the spaced repetition learning technique
