"""Veritabanının tutarlı kopyasını alır (SQLite backup API) ve eski yedekleri temizler.

Kullanım:  python -m muskitakip.backup
"""
import gzip
import logging
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import config, db

log = logging.getLogger("muskitakip.backup")


def snapshot(dest: Path) -> Path:
    """Çalışan veritabanının o anki tutarlı kopyasını dest'e yazar."""
    src = db.connect()
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.execute("PRAGMA journal_mode = DELETE")
        out.close()
    finally:
        src.close()
    return dest


def snapshot_tempfile() -> Path:
    fd, name = tempfile.mkstemp(prefix="muski-", suffix=".db")
    import os
    os.close(fd)
    return snapshot(Path(name))


def make_backup() -> Path:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(config.TZ).strftime("%Y%m%d-%H%M%S")
    tmp = snapshot_tempfile()
    dest = config.BACKUP_DIR / f"muski-{stamp}.db.gz"
    try:
        with open(tmp, "rb") as fi, gzip.open(dest, "wb") as fo:
            shutil.copyfileobj(fi, fo)
    finally:
        tmp.unlink(missing_ok=True)
    prune()
    return dest


def list_backups():
    if not config.BACKUP_DIR.exists():
        return []
    return sorted(config.BACKUP_DIR.glob("muski-*.db.gz"), reverse=True)


def prune():
    for old in list_backups()[config.BACKUP_KEEP:]:
        old.unlink(missing_ok=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    path = make_backup()
    log.info("Yedek alındı: %s (%d bayt)", path, path.stat().st_size)


if __name__ == "__main__":
    main()
