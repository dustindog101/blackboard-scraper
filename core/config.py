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

DEFAULT_BLANK_CONFIG = {
    "courses": {},
    "auto_login": {
        "username": "",
        "password": ""
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "admin_chat_id": None
    }
}

LOGIN_INDICATORS = ["login", "sso", "accounts.google", "webauth", "duosecurity", "idp/profile"]


def ensure_config_exists(notify: bool = True) -> tuple[bool, dict]:
    """
    Ensure config.json exists. If missing, create a blank config and optionally notify.
    Returns (created: bool, config: dict).
    """
    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(DEFAULT_BLANK_CONFIG, indent=2))
        if notify:
            print(f"⚠️ No login detected. Blank config created at: {CONFIG_FILE.name}")
        return True, dict(DEFAULT_BLANK_CONFIG)
    return False, load_config()


def has_auto_login_credentials(config: dict | None = None) -> bool:
    """Check whether valid auto_login credentials (username and password) exist."""
    cfg = config if config is not None else load_config()
    auto = cfg.get("auto_login", {})
    if not isinstance(auto, dict):
        return False
    usr = str(auto.get("username") or "").strip()
    pwd = str(auto.get("password") or "").strip()
    return bool(usr and pwd)


def save_auto_login_credentials(username: str, password: str) -> None:
    """Save or update auto_login credentials in config.json."""
    data = load_config()
    if not data:
        data = json.loads(json.dumps(DEFAULT_BLANK_CONFIG))
    if "auto_login" not in data or not isinstance(data["auto_login"], dict):
        data["auto_login"] = {}
    data["auto_login"]["username"] = username.strip()
    data["auto_login"]["password"] = password
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    print("   💾 Saved credentials to config.json for future logins.")


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


def save_courses(courses: dict[str, str], overwrite: bool = False):
    """Save courses to config.json with optional overwrite."""
    if not courses:
        return
    data = load_config()
    if overwrite:
        data["courses"] = courses
    else:
        existing = data.get("courses", {})
        if not existing and data and not any(k in data for k in ["courses", "auto_login"]):
            existing = data
        existing.update(courses)
        data["courses"] = existing

    CONFIG_FILE.write_text(json.dumps(data, indent=2))


