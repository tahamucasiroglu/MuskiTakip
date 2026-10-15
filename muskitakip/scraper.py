"""MUSKİ su kesintileri sayfasını Selenium ile ziyaret edip veritabanına işler.

Kullanım:  python -m muskitakip.scraper
"""
import gzip
import hashlib
import json
import logging
import shutil
import sys
import tempfile
import time
import urllib.request

from bs4 import BeautifulSoup

from . import config, db, logsetup, sms
from .parser import (minute_key, norm_upper, parse_api, parse_iso, parse_page, tahmini_bitis)

log = logging.getLogger("muskitakip.scraper")

USER_AGENT = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/155.0.0.0 Safari/537.36")

FETCH_API_JS = """
const cb = arguments[arguments.length - 1];
fetch(arguments[0], {headers: {'Accept': 'application/json'}, cache: 'no-store'})
  .then(r => r.text().then(t => cb({status: r.status, text: t})))
  .catch(e => cb({status: 0, text: String(e)}));
"""


# --------------------------------------------------------------------------- indirme

def fetch_selenium() -> dict:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    profile = tempfile.mkdtemp(prefix="muskitakip-chrome-")
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--window-size=1366,900", "--lang=tr-TR", "--no-first-run",
                "--disable-extensions", f"--user-data-dir={profile}", f"--user-agent={USER_AGENT}"):
        opts.add_argument(arg)
    if config.CHROME_BINARY:
        opts.binary_location = config.CHROME_BINARY
    service = Service(executable_path=config.CHROMEDRIVER) if config.CHROMEDRIVER else Service()
    t0 = time.monotonic()
    driver = webdriver.Chrome(options=opts, service=service)
    log.debug("Chrome açıldı (%.1f sn): %s", time.monotonic() - t0, driver.capabilities.get("browserVersion"))
    try:
        driver.set_page_load_timeout(config.PAGE_TIMEOUT)
        driver.set_script_timeout(config.PAGE_TIMEOUT)
        t1 = time.monotonic()
        driver.get(config.PAGE_URL)
        WebDriverWait(driver, config.PAGE_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "outagesGrid")))
        html = driver.page_source
        log.info("Sayfa yüklendi: %s (%.1f sn, %d bayt, başlık=%r)", driver.current_url,
                 time.monotonic() - t1, len(html), driver.title)
        t2 = time.monotonic()
        api = driver.execute_async_script(FETCH_API_JS, config.API_URL)
        ok = bool(api and api.get("status") == 200)
        log.info("API yanıtı: HTTP %s (%.1f sn, %d bayt)", api and api.get("status"), time.monotonic() - t2,
                 len((api or {}).get("text") or ""))
        if not ok:
            log.warning("API başarısız: %s", str(api)[:500])
        return {"method": "selenium", "html": html,
                "api": api.get("text") if ok else None,
                "api_error": None if ok else f"API: {api}"[:500]}
    except Exception:
        # Sorunu sonradan görebilmek için o anki ekranı ve sayfa kaynağını sakla.
        try:
            shot = logsetup.debug_dump("selenium-hata", driver.get_screenshot_as_png(), "png")
            src = logsetup.debug_dump("selenium-hata", driver.page_source or "", "html")
            log.error("Selenium hatası; adres=%s başlık=%r ekran=%s kaynak=%s",
                      driver.current_url, driver.title, shot, src)
        except Exception:  # noqa: BLE001
            log.exception("Hata anı ekran görüntüsü alınamadı")
        raise
    finally:
        driver.quit()
        shutil.rmtree(profile, ignore_errors=True)


def _http_get(url: str, accept: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=config.PAGE_TIMEOUT) as r:
        return r.read().decode("utf-8")


def fetch_http() -> dict:
    """Selenium başarısız olursa veri kaybolmasın diye yedek yol."""
    log.warning("Selenium kullanılamadı, doğrudan HTTP ile deneniyor")
    html = _http_get(config.PAGE_URL, "text/html")
    try:
        api, err = _http_get(config.API_URL, "application/json"), None
    except Exception as e:  # noqa: BLE001
        api, err = None, f"API: {e}"
    return {"method": "http", "html": html, "api": api, "api_error": err}


def fetch() -> tuple[dict, list[str]]:
    errors = []
    for attempt in range(2):
        try:
            return fetch_selenium(), errors
        except Exception as e:  # noqa: BLE001
            log.exception("Selenium denemesi %s başarısız", attempt + 1)
            errors.append(f"selenium#{attempt + 1}: {type(e).__name__}: {e}"[:800])
            time.sleep(10)
    return fetch_http(), errors


# --------------------------------------------------------------------------- yardımcılar

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_html(html: str) -> str:
    """Sayfadan yalnızca kesinti verisini içeren bölümü al (değişmeyen menü vb. hariç)."""
    soup = BeautifulSoup(html, "html.parser")
    parts = [str(el) for el in (soup.select_one("#outagesSummary"), soup.select_one("#outagesGrid")) if el]
    return "\n".join(parts) if parts else html


def save_snapshot(con, scrape_id, kind, url, content, fetched_at) -> str:
    digest = sha256(content)
    last = con.execute("SELECT sha256 FROM raw_snapshots WHERE kind=? ORDER BY id DESC LIMIT 1",
                       (kind,)).fetchone()
    if not last or last["sha256"] != digest:
        con.execute("INSERT INTO raw_snapshots(scrape_id, kind, url, fetched_at, sha256, size, content_gz) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (scrape_id, kind, url, fetched_at, digest, len(content.encode("utf-8")),
                     gzip.compress(content.encode("utf-8"))))
    return digest


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def overlap(a, b) -> int:
    return len({norm_upper(x) for x in a} & {norm_upper(x) for x in b})


# --------------------------------------------------------------------------- kayıt birleştirme

TRACKED = ["muski_id", "ays_is_emri_id", "ilce", "kesinti_tarihi", "tahmini_sure_saat", "tahmini_sure_text",
           "kesinti_turu", "kesinti_turu_kod", "tur_etiketi", "site_durum", "site_durum_text", "api_durum",
           "is_emri_durum_id", "is_emri_durum_adi", "sahaya_gonderildi", "kesinti_yayinlansin",
           "boru_cinsi", "mahalleler_text"]


def build_record(card: dict | None, item: dict | None) -> dict:
    rec = {}
    if item:
        rec.update({k: item.get(k) for k in (
            "muski_id", "ays_is_emri_id", "ilce", "ilce_id", "ilce_uavt", "ilce_mernis", "il", "il_id",
            "tahmini_sure_saat", "kesinti_turu", "kesinti_yayinlansin", "api_durum", "is_emri_durum_id",
            "is_emri_durum_adi", "is_emri_renk", "is_emri_tamamlandi", "sahaya_gonderildi", "boru_cinsi")})
        rec["kesinti_tarihi_dt"] = item["kesinti_tarihi"]
        rec["kesinti_tarihi_raw"] = item["kesinti_tarihi_raw"]
        rec["raw_api_json"] = json.dumps(item["raw"], ensure_ascii=False)
        rec["durum"] = "tamamlandi" if item["tamamlandi"] else "devam"
    if card:
        rec["ilce"] = card["region"] or rec.get("ilce")
        if card["kesinti_tarihi"] and not rec.get("kesinti_tarihi_dt"):
            rec["kesinti_tarihi_dt"] = card["kesinti_tarihi"]
            rec["kesinti_tarihi_raw"] = card["kesinti_tarihi_raw"]
        if card["tahmini_sure_saat"] is not None or rec.get("tahmini_sure_saat") is None:
            rec["tahmini_sure_saat"] = card["tahmini_sure_saat"]
        rec["tahmini_sure_text"] = card["tahmini_sure_text"]
        rec["kesinti_turu_kod"] = card["type"] or None
        rec["tur_etiketi"] = card["tur_etiketi"]
        if not rec.get("kesinti_turu") and card["type"]:
            rec["kesinti_turu"] = {"plansiz": "Plansız", "planli": "Planlı"}.get(card["type"], card["type"])
        rec["site_durum"] = card["status"] or None
        rec["site_durum_text"] = card["status_text"]
        rec["raw_html_card"] = card["html"]
        if card["status"]:
            rec["durum"] = "tamamlandi" if card["status"] == "tamamlandi" else "devam"

    # Mahalleler: sayfadaki sıra esas, API'den gelen ID/UAVT kodları eklenir.
    api_nb = {norm_upper(m["mahalle"]): m for m in (item or {}).get("mahalleler", [])}
    nbs = []
    for name in (card or {}).get("mahalleler", []):
        m = api_nb.pop(norm_upper(name), None)
        nbs.append({"mahalle": name, "mahalle_id": m and m["mahalle_id"], "uavt_kodu": m and m["uavt_kodu"]})
    nbs.extend(api_nb.values())
    rec["neighborhoods"] = nbs
    rec["mahalleler_text"] = ", ".join(n["mahalle"] for n in nbs)

    dt = rec.get("kesinti_tarihi_dt")
    rec["kesinti_tarihi"] = iso(dt)
    rec["tahmini_bitis"] = iso(tahmini_bitis(dt, rec.get("tahmini_sure_saat")))
    return rec


def find_existing(con, rec, used_ids: set):
    if rec.get("muski_id"):
        row = con.execute("SELECT * FROM outages WHERE muski_id=?", (rec["muski_id"],)).fetchone()
        if row and row["id"] not in used_ids:
            return row
    dt = rec.get("kesinti_tarihi_dt")
    if not dt:
        return None
    rows = con.execute(
        "SELECT * FROM outages WHERE source='web' AND substr(kesinti_tarihi,1,16)=?",
        (minute_key(dt),)).fetchall()
    cands = [r for r in rows if norm_upper(r["ilce"]) == norm_upper(rec["ilce"]) and r["id"] not in used_ids
             and not (rec.get("muski_id") and r["muski_id"] and r["muski_id"] != rec["muski_id"])]
    names = [n["mahalle"] for n in rec["neighborhoods"]]
    if names:
        # Aynı ilçe+dakikada ortak mahallesi olmayan kayıt başka bir kesintidir; birleştirme.
        cands = [r for r in cands if not r["mahalleler_text"]
                 or overlap(names, r["mahalleler_text"].split(", ")) > 0]
    if not cands:
        return None
    return max(cands, key=lambda r: overlap(names, (r["mahalleler_text"] or "").split(", ")))


def add_event(con, outage_id, scrape_id, at, event, field=None, old=None, new=None):
    log.info("Kesinti #%s %s%s", outage_id, event,
             f" {field}: {str(old)[:80]!r} -> {str(new)[:80]!r}" if field else "")
    con.execute("INSERT INTO outage_events(outage_id, scrape_id, at, event, field, old_value, new_value) "
                "VALUES (?,?,?,?,?,?,?)",
                (outage_id, scrape_id, at, event, field,
                 None if old is None else str(old), None if new is None else str(new)))


def save_neighborhoods(con, outage_id, nbs):
    names = [n["mahalle"] for n in nbs]
    if names:
        con.execute(f"DELETE FROM outage_neighborhoods WHERE outage_id=? AND mahalle NOT IN "
                    f"({','.join('?' * len(names))})", (outage_id, *names))
    for n in nbs:
        con.execute(
            "INSERT INTO outage_neighborhoods(outage_id, mahalle, mahalle_id, uavt_kodu) VALUES (?,?,?,?) "
            "ON CONFLICT(outage_id, mahalle) DO UPDATE SET "
            "mahalle_id=COALESCE(excluded.mahalle_id, mahalle_id), uavt_kodu=COALESCE(excluded.uavt_kodu, uavt_kodu)",
            (outage_id, n["mahalle"], n["mahalle_id"], n["uavt_kodu"]))


COLUMNS = ["muski_id", "ays_is_emri_id", "ilce", "ilce_id", "ilce_uavt", "ilce_mernis", "il", "il_id",
           "kesinti_tarihi", "kesinti_tarihi_raw", "tahmini_sure_saat", "tahmini_sure_text", "tahmini_bitis",
           "kesinti_turu", "kesinti_turu_kod", "tur_etiketi", "site_durum", "site_durum_text", "api_durum",
           "is_emri_durum_id", "is_emri_durum_adi", "is_emri_renk", "is_emri_tamamlandi", "sahaya_gonderildi",
           "kesinti_yayinlansin", "boru_cinsi", "mahalleler_text", "raw_api_json", "raw_html_card"]


API_PRECISE = {"kesinti_tarihi", "kesinti_tarihi_raw", "tahmini_bitis"}  # tahmini_bitis aşağıda yeniden hesaplanır


def upsert(con, scrape_id, rec, now, used_ids: set) -> str:
    """Kaydı ekler/günceller. Dönüş: 'new' | 'changed' | 'same'."""
    existing = find_existing(con, rec, used_ids)
    durum = rec.get("durum") or "devam"
    if existing is None:
        vals = {c: rec.get(c) for c in COLUMNS}
        vals.update(source="web", durum=durum, first_seen_at=now, last_seen_at=now,
                    last_active_seen_at=now if durum == "devam" else None,
                    completed_seen_at=now if durum == "tamamlandi" else None,
                    created_at=now, updated_at=now)
        cols = list(vals)
        cur = con.execute(f"INSERT INTO outages({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [vals[c] for c in cols])
        oid = cur.lastrowid
        save_neighborhoods(con, oid, rec["neighborhoods"])
        add_event(con, oid, scrape_id, now, "yeni", "durum", None, durum)
        used_ids.add(oid)
        return "new"

    oid = existing["id"]
    used_ids.add(oid)
    changed = False
    updates = {"last_seen_at": now, "updated_at": now}
    for c in COLUMNS:
        new = rec.get(c)
        if new is None:
            continue  # bu turda kaynağı yoksa (ör. API'de görünmedi) eski değeri koru
        if c in API_PRECISE and not rec.get("raw_api_json") and existing["raw_api_json"]:
            continue  # API'nin saniyeli tarihini sayfadaki dakikalı tarihle ezme
        old = existing[c]
        if old != new and not (isinstance(new, float) and old is not None and float(old) == new):
            updates[c] = new
            if c in TRACKED:
                changed = True
                add_event(con, oid, scrape_id, now,
                          "id_eslesti" if c in ("muski_id", "ays_is_emri_id") and old is None else "degisti",
                          c, old, new)
    kt = parse_iso(updates.get("kesinti_tarihi", existing["kesinti_tarihi"]))
    bitis = iso(tahmini_bitis(kt, updates.get("tahmini_sure_saat", existing["tahmini_sure_saat"])))
    if bitis and bitis != existing["tahmini_bitis"]:
        updates["tahmini_bitis"] = bitis
    if existing["gone_at"]:
        updates["gone_at"] = None
        add_event(con, oid, scrape_id, now, "tekrar_gorundu", "gone_at", existing["gone_at"], None)
        changed = True
    if existing["durum"] != durum:
        updates["durum"] = durum
        add_event(con, oid, scrape_id, now, "tamamlandi" if durum == "tamamlandi" else "degisti",
                  "durum", existing["durum"], durum)
        changed = True
    if durum == "devam":
        updates["last_active_seen_at"] = now
        if existing["completed_seen_at"]:
            updates["completed_seen_at"] = None  # tekrar "devam" görünürse eski tamamlanma anı geçersiz
    elif durum == "tamamlandi" and not existing["completed_seen_at"]:
        updates["completed_seen_at"] = now
    sets = ", ".join(f"{k}=?" for k in updates)
    con.execute(f"UPDATE outages SET {sets} WHERE id=?", (*updates.values(), oid))
    save_neighborhoods(con, oid, rec["neighborhoods"])
    return "changed" if changed else "same"


def mark_gone(con, scrape_id, now, seen_ids: set) -> int:
    rows = con.execute("SELECT id, durum FROM outages WHERE source='web' AND gone_at IS NULL").fetchall()
    n = 0
    for r in rows:
        if r["id"] in seen_ids:
            continue
        new_durum = "tamamlandi" if r["durum"] == "tamamlandi" else "kalkti"
        con.execute("UPDATE outages SET gone_at=?, durum=?, updated_at=? WHERE id=?",
                    (now, new_durum, now, r["id"]))
        add_event(con, r["id"], scrape_id, now, "siteden_kalkti", "durum", r["durum"], new_durum)
        n += 1
    return n


# --------------------------------------------------------------------------- ana akış

def safe_upsert(con, scrape_id, rec, now, used_ids, errors) -> str:
    """Tek bir bozuk kayıt tüm taramayı düşürmesin (savepoint ile yalnızca o kayıt geri alınır)."""
    if not rec.get("kesinti_tarihi"):
        errors.append(f"Tarihi okunamayan kayıt atlandı: {rec.get('ilce')} {rec.get('kesinti_tarihi_raw')!r}")
        return "error"
    con.execute("SAVEPOINT rec")
    try:
        res = upsert(con, scrape_id, rec, now, used_ids)
        con.execute("RELEASE SAVEPOINT rec")
        return res
    except Exception as e:  # noqa: BLE001
        con.execute("ROLLBACK TO SAVEPOINT rec")
        con.execute("RELEASE SAVEPOINT rec")
        log.exception("Kayıt işlenemedi")
        errors.append(f"Kayıt işlenemedi ({rec.get('ilce')} {rec.get('kesinti_tarihi')}): {type(e).__name__}: {e}")
        return "error"


def process(con, scrape_id, html: str | None, api_text: str | None, now: str, errors: list | None = None) -> dict:
    errors = errors if errors is not None else []
    page = parse_page(html) if html else None
    api = None
    if api_text:
        try:
            api = parse_api(api_text)
        except (ValueError, TypeError, AttributeError) as e:
            errors.append(f"API yanıtı JSON değil / beklenmeyen biçim: {type(e).__name__}: {e}"[:500])
    grid_ok = bool(page and page["grid_found"])
    site_error = page and page["site_error"]
    if site_error:
        errors.append(f"MUSKİ sitesi kesinti listesi yerine hata gösteriyor: {site_error}")
        log.warning("MUSKİ sitesi hata gösteriyor (kesintiler 'kalktı' sayılmayacak): %s", site_error)
        logsetup.debug_dump("site-hata-mesaji", html, "html")

    items = list(api["items"]) if api else []
    used_items = set()
    used_ids: set = set()
    stats = {"new": 0, "changed": 0, "gone": 0, "error": 0}

    for card in (page["cards"] if page else []):
        cands = [i for i, it in enumerate(items) if i not in used_items
                 and norm_upper(it["ilce"]) == norm_upper(card["region"])
                 and minute_key(it["kesinti_tarihi"]) == minute_key(card["kesinti_tarihi"])]
        item = None
        log.debug("Kart %s: %s %s %s | aday API kayıtları=%s", card["index"], card["region"],
                  card["kesinti_tarihi_raw"], card["status"], [items[i]["muski_id"] for i in cands])
        if card["mahalleler"]:
            cands = [i for i in cands if not items[i]["mahalleler"]
                     or overlap(card["mahalleler"], [m["mahalle"] for m in items[i]["mahalleler"]]) > 0]
        if cands:
            best = max(cands, key=lambda i: overlap(card["mahalleler"], [m["mahalle"] for m in items[i]["mahalleler"]]))
            used_items.add(best)
            item = items[best]
            log.debug("Kart %s ↔ API kesinti %s eşleşti", card["index"], item["muski_id"])
        res = safe_upsert(con, scrape_id, build_record(card, item), now, used_ids, errors)
        if res in stats:
            stats[res] += 1

    for i, item in enumerate(items):  # API'de olup sayfada eşleşmeyenler
        if i not in used_items:
            res = safe_upsert(con, scrape_id, build_record(None, item), now, used_ids, errors)
            if res in stats:
                stats[res] += 1

    # Sayfa düzgün ayrıştırıldıysa listede olmayanlar siteden kalkmış demektir.
    if page and not grid_ok:
        log.warning("Sayfada #outagesGrid yok; siteden kalkma kontrolü atlandı (site değişmiş olabilir)")
        logsetup.debug_dump("grid-yok", html or "", "html")
    if stats["error"]:
        log.warning("%s kayıt işlenemediği için siteden kalkma kontrolü bu turda atlandı", stats["error"])
    active_in_api = [it for it in items if not it["tamamlandi"]]
    if page and grid_ok and not stats["error"] and not site_error:
        toplam = page["summary"].get("Toplam")
        if page["cards"]:
            stats["gone"] = mark_gone(con, scrape_id, now, used_ids)
        elif active_in_api:
            log.warning("Sayfa 'kesinti yok' diyor ama API'de %s devam eden kesinti var; kalkma kontrolü atlandı",
                        len(active_in_api))
        elif toplam in (0, None):
            stats["gone"] = mark_gone(con, scrape_id, now, used_ids)
        else:
            log.warning("Sayfada kart yok ama özet Toplam=%s; kalkma kontrolü atlandı", toplam)
    if page and page["summary"].get("Toplam") not in (None, len(page["cards"])):
        log.warning("Özet Toplam=%s ama %s kart ayrıştırıldı (sayfa yapısı değişmiş olabilir)",
                    page["summary"].get("Toplam"), len(page["cards"]))
        logsetup.debug_dump("kart-sayisi-uyusmuyor", html, "html")
    return {"page": page, "api": api, "site_error": site_error, **stats}


def run() -> int:
    con = db.connect()
    started = db.now_iso()
    scrape_id = con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (started,)).lastrowid
    con.commit()
    log.info("Tarama #%s başladı (veritabanı %s)", scrape_id, config.DB_PATH)
    t0 = time.monotonic()
    errors = []
    try:
        fetched, errors = fetch()
        log.info("Veri alındı (%s, %.1f sn)", fetched["method"], time.monotonic() - t0)
        now = db.now_iso()
        html_ev = evidence_html(fetched["html"]) if fetched.get("html") else None
        html_sha = save_snapshot(con, scrape_id, "html", config.PAGE_URL, html_ev, now) if html_ev else None
        api_sha = save_snapshot(con, scrape_id, "api", config.API_URL, fetched["api"], now) if fetched.get("api") else None
        if fetched.get("api_error"):
            errors.append(fetched["api_error"])
        result = process(con, scrape_id, fetched.get("html"), fetched.get("api"), now, errors)
        sms.refresh_status(con)  # mesaj kayıtlarında ilan edilen saati geçenleri "bitti" yap
        page, api = result["page"], result["api"]
        summ = page["summary"] if page else {}
        con.execute(
            "UPDATE scrapes SET finished_at=?, ok=?, method=?, error=?, html_count=?, api_count=?, new_count=?, "
            "changed_count=?, gone_count=?, site_plansiz=?, site_planli=?, site_tamamlanan=?, site_toplam=?, "
            "site_last_updated=?, html_sha256=?, api_sha256=? WHERE id=?",
            (db.now_iso(), 0 if result["site_error"] else 1, fetched["method"], "\n".join(errors) or None,
             len(page["cards"]) if page else None, len(api["items"]) if api else None,
             result["new"], result["changed"], result["gone"],
             summ.get("Plansız Kesinti"), summ.get("Planlı Kesinti"), summ.get("Tamamlanan"), summ.get("Toplam"),
             page and page["last_updated"], html_sha, api_sha, scrape_id))
        con.commit()
        log.info("Tarama #%s tamam (%s, %.1f sn): sayfa=%s api=%s yeni=%s değişen=%s kalkan=%s hatalı=%s",
                 scrape_id, fetched["method"], time.monotonic() - t0, len(page["cards"]) if page else "-",
                 len(api["items"]) if api else "-", result["new"], result["changed"], result["gone"],
                 result["error"])
        for e in errors:
            log.warning("Tarama #%s uyarısı: %s", scrape_id, e)
        return 0
    except Exception as e:  # noqa: BLE001
        con.rollback()
        log.exception("Tarama #%s başarısız", scrape_id)
        errors.append(f"{type(e).__name__}: {e}")
        con.execute("UPDATE scrapes SET finished_at=?, ok=0, error=? WHERE id=?",
                    (db.now_iso(), "\n".join(errors)[:4000], scrape_id))
        con.commit()
        return 1
    finally:
        con.close()


def undo_gone(con, scrape_id: int, reason: str) -> int:
    """Bir taramanın hatalı 'siteden kalktı' işaretlerini geri alır; düzeltme kayıt geçmişine yazılır."""
    now = db.now_iso()
    rows = con.execute("SELECT e.outage_id, e.at, e.old_value, o.gone_at, o.durum FROM outage_events e "
                       "JOIN outages o ON o.id=e.outage_id WHERE e.scrape_id=? AND e.event='siteden_kalkti'",
                       (scrape_id,)).fetchall()
    n = 0
    for r in rows:
        if r["gone_at"] != r["at"]:
            continue  # sonradan zaten değişmiş
        con.execute("UPDATE outages SET gone_at=NULL, durum=?, updated_at=? WHERE id=?",
                    (r["old_value"], now, r["outage_id"]))
        add_event(con, r["outage_id"], scrape_id, now, "duzeltme", "durum", r["durum"],
                  f"{r['old_value']} (tarama #{scrape_id} kalkma işareti geri alındı: {reason})")
        n += 1
    con.execute("UPDATE scrapes SET ok=0, gone_count=0, error=COALESCE(error || char(10), '') || ? WHERE id=?",
                (f"Düzeltme ({now}): {reason}; {n} kalkma işareti geri alındı", scrape_id))
    log.warning("Tarama #%s: %s kalkma işareti geri alındı (%s)", scrape_id, n, reason)
    return n


def main():
    logsetup.setup("scraper")
    if len(sys.argv) >= 3 and sys.argv[1] == "--geri-al-kalkma":
        con = db.connect()
        n = undo_gone(con, int(sys.argv[2]), " ".join(sys.argv[3:]) or "elle düzeltme")
        con.commit()
        print(f"{n} kayıt düzeltildi")
        return
    sys.exit(run())


if __name__ == "__main__":
    main()
