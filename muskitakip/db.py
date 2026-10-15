"""SQLite şeması ve bağlantı yardımcıları."""
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS scrapes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    ok              INTEGER NOT NULL DEFAULT 0,
    method          TEXT,              -- selenium / http (yedek yol)
    error           TEXT,
    html_count      INTEGER,           -- sayfadaki kart sayısı
    api_count       INTEGER,           -- JSON API'deki kayıt sayısı
    new_count       INTEGER DEFAULT 0,
    changed_count   INTEGER DEFAULT 0,
    gone_count      INTEGER DEFAULT 0,
    site_plansiz    INTEGER,           -- sayfadaki özet kutuları
    site_planli     INTEGER,
    site_tamamlanan INTEGER,
    site_toplam     INTEGER,
    site_last_updated TEXT,            -- sayfadaki "Son güncelleme" yazısı
    html_sha256     TEXT,
    api_sha256      TEXT
);

-- Ham yanıtların kanıt niteliğinde saklanması (yalnızca içerik değiştiğinde yeni kayıt).
CREATE TABLE IF NOT EXISTS raw_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_id   INTEGER NOT NULL REFERENCES scrapes(id),
    kind        TEXT NOT NULL,          -- html / api
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    content_gz  BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_kind_sha ON raw_snapshots(kind, sha256);

CREATE TABLE IF NOT EXISTS outages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL DEFAULT 'web',   -- kayıt türü: web (site, otomatik) / mesaj (SMS)
    muski_id            INTEGER UNIQUE,                -- API: id (kesinti kayıt no)
    ays_is_emri_id      INTEGER,                       -- API: aysIsEmriId (iş emri no)
    ilce                TEXT NOT NULL,
    ilce_id             INTEGER,
    ilce_uavt           INTEGER,
    ilce_mernis         INTEGER,
    il                  TEXT,
    il_id               INTEGER,
    kesinti_tarihi      TEXT NOT NULL,                 -- ISO, +03:00
    kesinti_tarihi_raw  TEXT,                          -- sayfada/API'de görünen ham değer
    tahmini_sure_saat   REAL,
    tahmini_sure_text   TEXT,
    tahmini_bitis       TEXT,                          -- kesinti_tarihi + tahmini süre
    kesinti_turu        TEXT,                          -- Plansız / Planlı
    kesinti_turu_kod    TEXT,                          -- sayfa data-type: plansiz / planli
    tur_etiketi         TEXT,                          -- sayfa rozeti: "Arıza / Plansız"
    durum               TEXT NOT NULL,                 -- devam / tamamlandi / siteden_kalkti
    site_durum          TEXT,                          -- sayfa data-status son değeri
    site_durum_text     TEXT,                          -- "Devam Ediyor" / "Tamamlandı"
    api_durum           TEXT,                          -- API: durum ("Aktif")
    is_emri_durum_id    INTEGER,
    is_emri_durum_adi   TEXT,                          -- "İşleme Alındı" / "Tamamlandı"
    is_emri_renk        TEXT,
    is_emri_tamamlandi  TEXT,
    sahaya_gonderildi   TEXT,
    kesinti_yayinlansin TEXT,
    boru_cinsi          TEXT,
    mahalleler_text     TEXT,                          -- "A, B, C" (arama/filtre için)
    first_seen_at       TEXT,                          -- sitede ilk görüldüğü tarama
    last_seen_at        TEXT,                          -- sitede son görüldüğü tarama
    last_active_seen_at TEXT,                          -- "devam ediyor" olarak son görüldüğü an
    completed_seen_at   TEXT,                          -- "tamamlandı" olarak ilk görüldüğü an
    gone_at             TEXT,                          -- siteden kalktığı ilk tarama
    ariza_yeri          TEXT,                          -- mesaj: "TUZLA Mah. 644 SOKAK" (web sitesi yazmıyor)
    sebep               TEXT,                          -- mesaj: arıza/kesinti sebebi
    ilk_ilan_bitis      TEXT,                          -- mesaj: ilk mesajda bildirilen bitiş (uzatma takibi)
    sms_text            TEXT,
    notes               TEXT,
    raw_api_json        TEXT,
    raw_html_card       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outages_tarih ON outages(kesinti_tarihi);
CREATE INDEX IF NOT EXISTS ix_outages_ilce ON outages(ilce);
CREATE INDEX IF NOT EXISTS ix_outages_durum ON outages(durum);

CREATE TABLE IF NOT EXISTS outage_neighborhoods (
    outage_id    INTEGER NOT NULL REFERENCES outages(id) ON DELETE CASCADE,
    mahalle      TEXT NOT NULL,
    mahalle_id   INTEGER,
    uavt_kodu    INTEGER,
    PRIMARY KEY (outage_id, mahalle)
);
CREATE INDEX IF NOT EXISTS ix_nb_mahalle ON outage_neighborhoods(mahalle);

CREATE TABLE IF NOT EXISTS outage_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    outage_id   INTEGER NOT NULL REFERENCES outages(id) ON DELETE CASCADE,
    scrape_id   INTEGER REFERENCES scrapes(id),
    at          TEXT NOT NULL,
    event       TEXT NOT NULL,   -- yeni / degisti / tamamlandi / siteden_kalkti / tekrar_gorundu / id_eslesti
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_outage ON outage_events(outage_id);

-- Telefona gelen SMS'ler (yalnızca su kesintisi mesajları; fatura vb. alınmaz).
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    outage_id   INTEGER REFERENCES outages(id) ON DELETE SET NULL,
    text        TEXT NOT NULL,
    text_sha256 TEXT NOT NULL UNIQUE,      -- normalize edilmiş metnin özeti (mükerrer kontrolü)
    tip         TEXT,                      -- ilk / devam / dolum (arıza bitti, su veriliyor) / planli
    mesaj_tarihi TEXT,                     -- mesajda yazan tarih
    ilan_bitis  TEXT,                      -- mesajda bildirilen bitiş zamanı
    received_at TEXT,                      -- telefona geliş zamanı (biliniyorsa)
    adet        INTEGER NOT NULL DEFAULT 1, -- aynı mesaj kaç kez geldi
    kanal       TEXT,                      -- toplu_ice_aktarim / telegram / web_form
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_outage ON messages(outage_id);
"""


def now_iso() -> str:
    return datetime.now(config.TZ).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    con.executescript(SCHEMA)
    return con
