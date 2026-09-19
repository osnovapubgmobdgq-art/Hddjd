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


BOT_TOKEN = os.getenv("8763383205:AAFeRlMGdhVBEn8SBsretOjsB_dgmjMV3TM", "").strip()
ADMIN_IDS = _ids(os.getenv("5000488732", ""))
WEB_URL = os.getenv("hddjd-production.up.railway.app", "").strip().rstrip("/")
PORT = int(os.getenv("8088") or 8080)
ADMIN_PASSWORD = os.getenv("maksumtop1", "")
SECRET_KEY = os.getenv("dd", "")
CURRENCY = os.getenv("CURRENCY", "₽")
TZ_OFFSET = float(os.getenv("TZ_OFFSET") or 3)

DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "shop.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
