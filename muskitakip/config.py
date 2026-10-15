"""Ortam değişkenleriyle değiştirilebilen ayarlar."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = os.environ.get("MUSKI_BASE_URL", "https://www.muski.gov.tr")
PAGE_URL = BASE_URL + "/su-kesintileri"
API_URL = BASE_URL + "/api/home/su-kesintileri"

DATA_DIR = Path(os.environ.get("MUSKI_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = Path(os.environ.get("MUSKI_DB", DATA_DIR / "muski.db"))
BACKUP_DIR = Path(os.environ.get("MUSKI_BACKUP_DIR", DATA_DIR / "backups"))
BACKUP_KEEP = int(os.environ.get("MUSKI_BACKUP_KEEP", "90"))

LOG_DIR = Path(os.environ.get("MUSKI_LOG_DIR", DATA_DIR / "logs"))
LOG_KEEP_DAYS = int(os.environ.get("MUSKI_LOG_KEEP_DAYS", "60"))
DEBUG_KEEP = int(os.environ.get("MUSKI_DEBUG_KEEP", "60"))

WEB_HOST =os.environ.get("MUSKI_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("MUSKI_WEB_PORT", "5090"))

# Selenium: Pi'de projeye özel chromium kullanılır (sistem chromium'una dokunulmaz).
CHROME_BINARY = os.environ.get("MUSKI_CHROME_BINARY") or None
CHROMEDRIVER = os.environ.get("MUSKI_CHROMEDRIVER") or None
PAGE_TIMEOUT = int(os.environ.get("MUSKI_PAGE_TIMEOUT", "60"))

SCRAPE_INTERVAL_MIN = int(os.environ.get("MUSKI_INTERVAL_MIN", "15"))

TZ = ZoneInfo("Europe/Istanbul")

FONT_PATHS = [
    os.environ.get("MUSKI_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
FONT_BOLD_PATHS = [
    os.environ.get("MUSKI_FONT_BOLD", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
