import json
import os
from typing import Optional

API_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api_key.json')

def save_api_key_to_file(api_key: str, path: str = API_KEY_FILE) -> bool:
    """Save the API key to a file."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'api_key': api_key}, f)
        return True
    except Exception as e:
        print(f"Error saving API key to file: {e}")
        return False

def load_api_key_from_file(path: str = API_KEY_FILE) -> Optional[str]:
    """Load the API key from a file."""
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                data = json.load(f)
                return data.get('api_key')
    except Exception as e:
        print(f"Error loading API key from file: {e}")
    return None
