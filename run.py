from dotenv import load_dotenv

# Load .env BEFORE importing the app, since Config reads env vars at import time.
# Otherwise `python run.py` silently ignores .env (dev secret, empty API key).
load_dotenv()

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
