"""Ortak loglama: her bileşen kendi dosyasına (günlük döner) + stdout'a (journald) yazar.

Dosyalar: <MUSKI_LOG_DIR>/<bileşen>.log  (ör. scraper.log, web.log, telegram.log)
Seviye  : MUSKI_LOG_LEVEL (varsayılan INFO; eşleştirme ayrıntıları için DEBUG)
Hata anında Selenium ekran görüntüsü/sayfa kaynağı: <MUSKI_LOG_DIR>/debug/
"""
import logging
import logging.handlers
import os
import re
import sys
import time
from pathlib import Path

from . import config

FORMAT = "%(asctime)s %(levelname)-7s [%(process)d] %(name)s: %(message)s"
_done = False


class RedactFormatter(logging.Formatter):
    """Gizli değerlerin (Telegram token vb.) loga — traceback dahil — yazılmasını engeller."""

    def __init__(self, fmt):
        super().__init__(fmt)
        self.secrets = [s for s in (os.environ.get("MUSKI_TG_TOKEN"), os.environ.get("MUSKI_TG_CLAIM_CODE")) if s]

    def format(self, record):
        # Mesajdaki satır sonları kaçışlanır: URL vb. ile sahte log satırı eklenemesin (traceback etkilenmez).
        msg = record.getMessage()
        if "\n" in msg or "\r" in msg:
            record.msg, record.args = msg.replace("\r", "\\r").replace("\n", "\\n"), None
        text = super().format(record)
        for s in self.secrets:
            text = text.replace(s, "***")
        return text


def setup(component: str) -> logging.Logger:
    """Bileşenin loglamasını kurar (birden çok çağrılırsa ilk çağrı geçerlidir)."""
    global _done
    log = logging.getLogger(f"muskitakip.{component}")
    if _done:
        return log
    _done = True
    level = getattr(logging, os.environ.get("MUSKI_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = RedactFormatter(FORMAT)

    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    root.addHandler(out)
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(config.LOG_DIR / f"{component}.log", when="midnight",
                                                       backupCount=config.LOG_KEEP_DAYS, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:  # log klasörü yazılamıyorsa en azından journald'a yazmaya devam et
        root.warning("Log dosyası açılamadı (%s): %s", config.LOG_DIR, e)

    for noisy in ("selenium", "urllib3", "PIL", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.captureWarnings(True)

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        logging.getLogger(f"muskitakip.{component}").critical("Yakalanmamış hata", exc_info=(exc_type, exc, tb))
    sys.excepthook = excepthook
    return log


def debug_dump(name: str, content: bytes | str, ext: str) -> Path | None:
    """Hata ayıklama dosyası kaydeder (son MUSKI_DEBUG_KEEP dosya tutulur)."""
    try:
        d = config.LOG_DIR / "debug"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.{ext}"
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        files = sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[config.DEBUG_KEEP:]:
            old.unlink(missing_ok=True)
        return path
    except OSError:
        logging.getLogger("muskitakip").exception("Debug dosyası yazılamadı")
        return None


# --------------------------------------------------------------------------- okuma (web "Loglar" sayfası için)

LINE_START = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+)\s+\[(\d+)\] ([\w.]+): ?(.*)$")
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def log_files():
    """Log klasöründeki dosyalar (yeni → eski)."""
    if not config.LOG_DIR.exists():
        return []
    files = [p for p in config.LOG_DIR.iterdir() if p.is_file() and ".log" in p.name]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def read_records(path: Path, max_bytes: int = 2_000_000) -> list[dict]:
    """Dosyanın son kısmını kayıtlara böler (traceback satırları kendi kaydına eklenir)."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()  # yarım satırı at
        text = fh.read().decode("utf-8", errors="replace")
    records = []
    for line in text.splitlines():
        m = LINE_START.match(line)
        if m:
            records.append({"time": m.group(1), "level": m.group(2), "pid": m.group(3), "name": m.group(4),
                            "msg": m.group(5), "extra": [], "file": path.name})
        elif records:
            records[-1]["extra"].append(line)
    return records


def recent_problems(hours: int = 24, min_level: str = "WARNING") -> list[dict]:
    """Bütün bileşenlerin (dönmüş dosyalar dahil) zaman aralığındaki uyarı/hataları (yeni → eski)."""
    cutoff = time.time() - hours * 3600
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff))
    lvl = LEVELS.index(min_level) if min_level in LEVELS else LEVELS.index("WARNING")
    out = []
    for p in log_files():
        try:
            if p.stat().st_mtime < cutoff:
                continue  # aralıktan önce kapanmış dosya
            records = read_records(p)
        except OSError:  # okunurken döndürülmüş/silinmiş olabilir
            continue
        for r in records:
            if r["time"] >= since and r["level"] in LEVELS and LEVELS.index(r["level"]) >= lvl:
                out.append(r)
    return sorted(out, key=lambda r: r["time"], reverse=True)
