import os
import json
from pathlib import Path
from typing import Dict, Any

BASE_DIR = Path(__file__).resolve().parent.parent
_env_data_dir = os.getenv("TELEGRAM_UPLOADER_DATA_DIR")
DATA_DIR = Path(_env_data_dir).expanduser().resolve() if _env_data_dir else (BASE_DIR / "data")
DB_DIR = DATA_DIR / "db"
UPLOADS_DIR = DATA_DIR / "uploads"
THUMBS_DIR = DATA_DIR / "thumbs"
SESSIONS_DIR = DATA_DIR / "sessions"
CONFIG_FILE = DATA_DIR / "config.json"

for directory in [DATA_DIR, DB_DIR, UPLOADS_DIR, THUMBS_DIR, SESSIONS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_id": DEFAULT_API_ID,
    "api_hash": DEFAULT_API_HASH,
    "bot_token": None,
    "phone": None,
    "auth_mode": "bot",  # "bot" or "user"
    "chunk_size_kb": 512,  # 128, 256, 512
    "max_concurrent_chunks": 3,
    "max_retries": 10,
    "retry_base_delay": 2.0,
    "retry_max_delay": 60.0,
    "auto_resume_on_startup": True,
    "auto_delete_local_on_finish": False,
    "host": "0.0.0.0",
    "port": 8080
}

def load_config() -> Dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                cfg.update(saved)
        except Exception as e:
            print(f"Warning: Failed to load config from {CONFIG_FILE}: {e}")
    
    # Environment variables override config file if present
    env_api_id = os.getenv("TELEGRAM_API_ID")
    if env_api_id:
        try:
            cfg["api_id"] = int(env_api_id)
        except ValueError:
            pass
    if os.getenv("TELEGRAM_API_HASH"):
        cfg["api_hash"] = os.getenv("TELEGRAM_API_HASH")
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        cfg["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN")
    if os.getenv("TELEGRAM_PHONE"):
        cfg["phone"] = os.getenv("TELEGRAM_PHONE")

    return cfg

def save_config(new_config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    cfg.update(new_config)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg

DB_PATH = DB_DIR / "uploader.db"
