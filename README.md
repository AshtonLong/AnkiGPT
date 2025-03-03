# AnkiGPT

![AnkiGPT Logo](https://img.icons8.com/fluency/96/brain.png)

A beautiful, modern web application that uses AI to automatically generate Anki flashcards from your notes and study materials.

## 🚀 Features

- **AI-Powered Card Generation**: Automatically create high-quality Anki cloze deletion cards from your notes
- **Customizable Output**: Control the number and types of cards generated
- **Modern UI**: Beautiful, responsive interface with animations and modern design elements
- **Easy Export**: One-click export to Anki-compatible .apkg files
- **Card Preview & Editing**: Review and edit generated cards before exporting

## 📸 Screenshots

![AnkiGPT Screenshot](https://images.unsplash.com/photo-1456513080510-7bf3a84b82f8?ixlib=rb-4.0.3&ixid=MnwxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8&auto=format&fit=crop&w=1000&q=80)

## 🛠️ Technologies Used

- **Backend**: Flask (Python)
- **Frontend**: HTML, CSS, JavaScript
- **UI Framework**: Bootstrap 5
- **Animations**: AOS (Animate On Scroll)
- **Icons**: Font Awesome
- **AI**: Google Gemini API
- **Anki Integration**: genanki library

## 🚀 Getting Started

### Prerequisites

- Python 3.7+
- Google Gemini API key

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/ashtonlong/ankigpt.git
   cd ankigpt
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the application:
   ```bash
   python app.py
   ```

5. Open your browser and navigate to `http://localhost:5000`

## ❓ Frequently Asked Questions

### What is AnkiGPT?
AnkiGPT is a web application that leverages AI to automatically generate Anki flashcards from your notes and study materials, saving you time and effort in creating effective study materials.

### How does AnkiGPT work?
AnkiGPT uses Google Gemini AI to analyze your text input and intelligently create cloze deletion cards that highlight key concepts. The AI identifies important information and creates optimal cards for memorization.

### Is AnkiGPT free to use?
AnkiGPT requires a Google Gemini API key, which as of right now offers generous rate limits on the Flash series of models. So yes, as of right now this app can be used for free.

### What types of content work best with AnkiGPT?
AnkiGPT works best with clearly structured content like lecture notes, textbook chapters, and study guides. It's particularly effective for subjects that require memorization of key facts, definitions, and concepts.

### Can I edit the cards before exporting to Anki?
Yes! AnkiGPT provides a preview and editing interface so you can review and modify all generated cards before exporting them to an Anki-compatible file.

### How do I import the generated cards into Anki?
After generating and reviewing your cards, you can export them as an .apkg file with one click. Then, simply open Anki and double click the .apkg file to import it.

## 👨‍💻 Author

**Ashton Long**

- GitHub: [@ashtonlong](https://github.com/ashtonlong)

## 🙏 Acknowledgements

- [Anki](https://apps.ankiweb.net/) for the amazing spaced repetition software
- [Google Gemini](https://deepmind.google/technologies/gemini/) for the powerful AI capabilities
- [Bootstrap](https://getbootstrap.com/) for the responsive UI framework
- [Font Awesome](https://fontawesome.com/) for the beautiful icons
- [AOS](https://michalsnik.github.io/aos/) for the scroll animations 