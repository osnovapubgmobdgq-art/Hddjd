import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _ids(raw: str) -> set[int]:
    result = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.add(int(part))
    return result


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
WEB_URL = os.getenv("WEB_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT") or 8080)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
CURRENCY = os.getenv("CURRENCY", "₽")
TZ_OFFSET = float(os.getenv("TZ_OFFSET") or 3)

DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "shop.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
