import json
from pathlib import Path

# --- Config Paths ---
BLACKBOARD_BASE = "https://blackboard.umbc.edu"
SCRIPT_DIR = Path(__file__).parent.parent.resolve()
SESSION_DIR = SCRIPT_DIR / ".session"
OUTPUT_BASE = SCRIPT_DIR / "output"
CONFIG_FILE = SCRIPT_DIR / "config.json"

# Fallback courses if config.json doesn't exist
DEFAULT_COURSES = {
    "_100001_1": "CIS 101 Introduction to Computing",
}

LOGIN_INDICATORS = ["login", "sso", "accounts.google", "webauth", "duosecurity", "idp/profile"]


def load_config() -> dict:
    """Load the full config.json."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            print("⚠️  Warning: config.json is corrupt. Using empty config.")
    return {}

def load_courses() -> dict[str, str]:
    """Load courses from config.json, falling back to DEFAULT_COURSES."""
    data = load_config()
    if "courses" in data:
        return data["courses"]
    elif data and not any(k in data for k in ["courses", "auto_login"]):
        # Legacy support where config.json was just the courses dictionary
        return data
        
    return DEFAULT_COURSES


def save_courses(courses: dict[str, str]):
    """Save courses to config.json."""
    if not courses:
        return
    # Merge with existing
    data = load_config()
    existing = data.get("courses", {})
    if not existing and data and not any(k in data for k in ["courses", "auto_login"]):
        existing = data 
    
    existing.update(courses)
    data["courses"] = existing
    
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
