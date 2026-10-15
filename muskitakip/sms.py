"""MUSKİ'nin telefona gönderdiği su kesintisi SMS'lerini ayrıştırır ve 'mesaj' türünde kayıt olarak ekler.

Web sitesi kayıtlarından tamamen bağımsızdır (birbirleriyle eşleştirilmez).
Aynı kesinti için gelen mükerrer / "devam eden" / "tamamlanmış olup su veriliyor" mesajları tek kayıtta toplanır.

Kullanım:
    python -m muskitakip.sms dosya.txt [--eski-once] [--deneme]
Dosyadaki mesajlar telefondaki gibi en yeni üstte kabul edilir (--eski-once ile tersi).
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta

from . import config, db
from .parser import clean

DEFAULT_ILCE = os.environ.get("MUSKI_SMS_ILCE", "FETHİYE")
# "devam eden" / "dolum" mesajı, aynı arıza yerindeki önceki kayda en fazla bu kadar sonra bildirilmiş bitişle bağlanır
JOIN_WINDOW = timedelta(hours=14)

SEBEPLER = [  # (anahtar ifade, sebep, tür)
    ("YAPILACAK OLAN", "Planlı bakım onarım çalışması", "Planlı"),
    ("BAĞLANTI ÇALIŞMALARI", "Yeni hat bağlantı çalışması", "Planlı"),
    ("DEPO BAKIM ONARIM", "Depo bakım onarım çalışması", "Planlı"),
    ("BAKIM ONARIM", "Bakım onarım çalışması", "Planlı"),
    ("KAZI ÇALIŞMASI", "Kurum dışı kazı çalışmasında hat hasarı", "Plansız"),
    ("ORMAN YANGIN", "Orman yangınına tanker desteği (depo seviyesi düştü)", "Plansız"),
    ("ELEKTRİK KESİNTİSİ", "Elektrik kesintisi (depolara su iletilemedi)", "Plansız"),
    ("POMPA ARIZASI", "Depo pompa arızası", "Plansız"),
    ("VANA ARIZASI", "Şebeke hattı vana arızası", "Plansız"),
    ("İSALE HATTI", "İsale hattı arızası", "Plansız"),
    ("ANA ŞEBEKE", "Ana şebeke hattı arızası", "Plansız"),
    ("ŞEBEKE HATTI ARIZASI", "Şebeke hattı arızası", "Plansız"),
]


def tr_upper(text: str) -> str:
    return clean(text).replace("i", "İ").replace("ı", "I").upper()


def split_messages(raw: str) -> list[str]:
    """Yapıştırılmış metni tek tek SMS'lere böler (MUSKİ mesajları 'B002' imzasıyla biter)."""
    parts = re.split(r"(?<=B002)\s*", raw.strip())
    return [clean(p) for p in parts if clean(p)]


def is_outage(text: str) -> bool:
    t = tr_upper(text)
    return "SU KESİNTİSİ" in t and "FATURA" not in t and "BAŞVURU" not in t and "BASVURU" not in t


def norm_sha(text: str) -> str:
    return hashlib.sha256(tr_upper(text).encode("utf-8")).hexdigest()


def _split_names(text: str) -> list[str]:
    out = []
    for p in re.split(r",|\sVE\s", text):
        p = clean(re.sub(r"\b(?:MAHALLESİNDE|MAHALLELERİNDE|MAHALLESİ|MAH)\b\.?", "", p)).strip(" .")
        if p and p not in out:
            out.append(p)
    return out


def _time(date, hhmm):
    h, m = (int(x) for x in re.split(r"[.:]", hhmm))
    if not (0 <= m < 60 and 0 <= h <= 24) or (h == 24 and m):
        raise ValueError(f"geçersiz saat: {hhmm}")
    if h == 24:  # "24.00" -> ertesi gün 00:00
        return date.replace(hour=0, minute=0) + timedelta(days=1)
    return date.replace(hour=h, minute=m)


def parse_message(text: str) -> dict | None:
    """Tek bir kesinti SMS'ini ayrıştırır; kesinti mesajı değilse ya da okunamazsa None."""
    if not is_outage(text):
        return None
    try:
        return _parse_message(text)
    except (ValueError, OverflowError):  # 31.02 ya da 22.75 gibi geçersiz tarih/saat
        return None


def _parse_message(text: str) -> dict | None:
    t = tr_upper(text)
    t = re.sub(r"^SAYIN ABONEMİZ;?\s*", "", t)

    # Arıza yeri: "TUZLA MAHALLESİ 644 SOKAKTA" / "PATLANGIÇ MAHALLESİNDE" / "KARAÇULHA MAHALLESİ OSMAN EFENDİ CADDESİNDE"
    m = re.match(r"([A-ZÇĞİÖŞÜ ]+?) MAHALLESİ(NDE)?\s+(.*?)\s*\b(?:MEYDANA GELEN|DEVAM EDEN|KURUMUMUZ|YAPILACAK|"
                 r"YAPILAN|İMALATI|İÇME SUYU|ORMAN|MEYDANA)", t)
    yer_mahalle = clean(m.group(1)) if m else None
    sokak = None
    if m and not m.group(2) and clean(m.group(3)):
        sokak = re.sub(r"(N?DA|N?DE|TA|TE)$", "", clean(m.group(3)))  # "644 SOKAKTA" -> "644 SOKAK"
    ariza_yeri = f"{yer_mahalle} Mah." + (f" {sokak}" if sokak else "") if yer_mahalle else None

    dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s*\(([^)]*)\)", t)
    if not dm:
        return None
    gun = datetime(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)), tzinfo=config.TZ)
    zaman_notu = clean(dm.group(4))

    baslangic = None
    rm = re.search(r"(\d{1,2}[.:]\d{2})\s*-\s*(\d{1,2}[.:]\d{2})\s*SAATLERİ ARASINDA", t)
    if rm:
        baslangic = _time(gun, rm.group(1))
        bitis = _time(gun, rm.group(2))
        if bitis <= baslangic:  # gece yarısını geçen aralık (ör. 22.00-02.00)
            bitis += timedelta(days=1)
    else:
        sm = re.search(r"SAAT\s*(\d{1,2}[.:]\d{2})", t)
        if not sm:
            return None
        bitis = _time(gun, sm.group(1))

    # Etkilenen mahalleler
    am = re.search(r"(?:KADAR|ARASINDA)\s+(.+?)\s+(?:MAHALLESİNDE\s+|MAHALLELERİNDE\s+)?SU KESİNTİSİ", t)
    etkilenen = []
    if am:
        if "MAHALLE GENELİNDE" in am.group(1):
            etkilenen = [yer_mahalle] if yer_mahalle else []
        else:
            etkilenen = _split_names(am.group(1))

    if "TAMAMLANMIŞ OLUP" in t or "SU VERİLMEYE BAŞLANMIŞ" in t:
        tip = "dolum"
    elif "DEVAM EDEN" in t:
        tip = "devam"
    elif rm or "YAPILACAK" in t:
        tip = "planli"
    else:
        tip = "ilk"

    sebep, tur = "Diğer", "Plansız"
    for key, s, k in SEBEPLER:
        if key in t:
            sebep, tur = s, k
            break
    if tip == "dolum":
        sebep += " — arıza giderildi, hatlar dolarken kesinti/basınç sorunu"

    return {
        "text": clean(text), "sha": norm_sha(text), "tip": tip, "tarih": gun.date().isoformat(),
        "zaman_notu": zaman_notu, "baslangic": baslangic, "bitis": bitis,
        "ariza_yeri": ariza_yeri, "yer_mahalle": yer_mahalle, "sokak": sokak,
        "etkilenen": etkilenen, "sebep": sebep, "tur": tur,
        "basinc": "BASINÇ" in t,
    }


# --------------------------------------------------------------------------- veritabanına aktarma

def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _event(con, oid, now, event, field=None, old=None, new=None):
    con.execute("INSERT INTO outage_events(outage_id, at, event, field, old_value, new_value) VALUES (?,?,?,?,?,?)",
                (oid, now, event, field, old, new))


def _set_neighborhoods(con, oid, names):
    for n in names:
        con.execute("INSERT OR IGNORE INTO outage_neighborhoods(outage_id, mahalle) VALUES (?,?)", (oid, n))
    allnames = [r[0] for r in con.execute("SELECT mahalle FROM outage_neighborhoods WHERE outage_id=? ORDER BY rowid", (oid,))]
    con.execute("UPDATE outages SET mahalleler_text=? WHERE id=?", (", ".join(allnames), oid))


def find_group(con, msg):
    """Devam / dolum mesajını, aynı arıza yerindeki yakın tarihli mesaj kaydına bağlar."""
    if msg["tip"] not in ("devam", "dolum") or not msg["ariza_yeri"]:
        return None
    rows = con.execute("SELECT * FROM outages WHERE source='mesaj' AND ariza_yeri=? AND tahmini_bitis IS NOT NULL "
                       "ORDER BY tahmini_bitis DESC",
                       (msg["ariza_yeri"],)).fetchall()
    best = None
    for r in rows:
        prev = datetime.fromisoformat(r["tahmini_bitis"])
        gap = abs(msg["bitis"] - prev)
        if gap <= JOIN_WINDOW and (best is None or gap < best[0]):
            best = (gap, r)
    return best[1] if best else None


def find_same(con, msg):
    """Aynı tarih + kesinti yeri + bildirilen bitişe sahip mesaj kaydı (başka kanaldan girilmiş aynı kesinti)."""
    return con.execute("SELECT * FROM outages WHERE source='mesaj' AND substr(kesinti_tarihi,1,10)=? "
                       "AND COALESCE(ariza_yeri,'')=COALESCE(?,'') AND tahmini_bitis=?",
                       (msg["tarih"], msg["ariza_yeri"], _iso(msg["bitis"]))).fetchone()


def refresh_status(con, now_dt=None):
    """İlan edilen bitiş saati geçen mesaj kayıtlarını 'bitti' yapar."""
    now_dt = now_dt or datetime.now(config.TZ)
    now = _iso(now_dt)
    rows = con.execute("SELECT id, tahmini_bitis FROM outages WHERE source='mesaj' AND durum='devam'").fetchall()
    for r in rows:
        if r["tahmini_bitis"] and datetime.fromisoformat(r["tahmini_bitis"]) <= now_dt:
            con.execute("UPDATE outages SET durum='ilan_bitti', updated_at=? WHERE id=?", (now, r["id"]))
            _event(con, r["id"], now, "ilan_saati_doldu", "durum", "devam", "ilan_bitti")


def import_messages(con, texts: list[str], kanal="toplu_ice_aktarim", received_at=None) -> dict:
    """Mesajları (eskiden yeniye sıralı) ekler. Tekrar çalıştırmak güvenlidir."""
    now = db.now_iso()
    stats = {"toplam": len(texts), "kesinti_disi": 0, "anlasilamadi": 0, "mukerrer": 0,
             "yeni_kesinti": 0, "mevcut_kesintiye_eklendi": 0, "anlasilamayanlar": [], "outage_ids": []}
    batch_seen = set()
    for text in texts:
        if not is_outage(text):
            stats["kesinti_disi"] += 1
            continue
        msg = parse_message(text)
        if not msg:
            stats["anlasilamadi"] += 1
            stats["anlasilamayanlar"].append(text)
            continue
        existing_msg = con.execute("SELECT id, outage_id FROM messages WHERE text_sha256=?", (msg["sha"],)).fetchone()
        if existing_msg:
            if existing_msg["outage_id"] and existing_msg["outage_id"] not in stats["outage_ids"]:
                stats["outage_ids"].append(existing_msg["outage_id"])
            # Aynı aktarımda tekrar gelen mesaj sayılır; önceki aktarımlardan bilinen mesaj tekrar sayılmaz.
            if msg["sha"] in batch_seen:
                con.execute("UPDATE messages SET adet=adet+1 WHERE id=?", (existing_msg["id"],))
            stats["mukerrer"] += 1
            continue
        batch_seen.add(msg["sha"])

        group = find_group(con, msg)
        same = None if group else find_same(con, msg)
        if same:
            oid = same["id"]
            stats["mukerrer"] += 1
        elif group:
            oid = group["id"]
            old_end = group["tahmini_bitis"]
            new_end = _iso(msg["bitis"])  # en son gelen mesajdaki bitiş geçerlidir
            if new_end != old_end:
                con.execute("UPDATE outages SET tahmini_bitis=?, updated_at=? WHERE id=?", (new_end, now, oid))
                _event(con, oid, now, "sure_uzatildi" if msg["tip"] == "devam" else "dolum_mesaji",
                       "tahmini_bitis", old_end, new_end)
                if group["durum"] != "devam":  # yeni bitiş saati gelecekteyse refresh_status tekrar kapatmaz
                    con.execute("UPDATE outages SET durum='devam' WHERE id=?", (oid,))
                    _event(con, oid, now, "yeniden_acildi", "durum", group["durum"], "devam")
            con.execute("UPDATE outages SET sms_text = sms_text || char(10) || char(10) || ? WHERE id=?",
                        (msg["text"], oid))
            if msg["tip"] == "dolum":
                con.execute("UPDATE outages SET sebep = sebep || ' + sonrasında dolum/basınç sorunu' "
                            "WHERE id=? AND sebep NOT LIKE '%dolum%'", (oid,))
            stats["mevcut_kesintiye_eklendi"] += 1
        else:
            start = msg["baslangic"]
            kesinti_tarihi = _iso(start) if start else msg["tarih"]
            sure = round((msg["bitis"] - start).total_seconds() / 3600, 2) if start else None
            cur = con.execute(
                "INSERT INTO outages(source, ilce, kesinti_tarihi, kesinti_tarihi_raw, tahmini_sure_saat, tahmini_bitis, "
                "ilk_ilan_bitis, kesinti_turu, durum, ariza_yeri, sebep, sms_text, mahalleler_text, notes, "
                "created_at, updated_at) VALUES ('mesaj',?,?,?,?,?,?,?,'devam',?,?,?,?,?,?,?)",
                (DEFAULT_ILCE, kesinti_tarihi, f"{msg['tarih']} ({msg['zaman_notu']})", sure, _iso(msg["bitis"]),
                 _iso(msg["bitis"]), msg["tur"], msg["ariza_yeri"], msg["sebep"], msg["text"], "",
                 "Başlangıç saati SMS'te yazmıyor; tarih mesajdaki tarihtir." if not start else None, now, now))
            oid = cur.lastrowid
            _event(con, oid, now, "yeni", "kaynak", None, f"mesaj ({kanal})")
            stats["yeni_kesinti"] += 1
        _set_neighborhoods(con, oid, msg["etkilenen"])
        if oid not in stats["outage_ids"]:
            stats["outage_ids"].append(oid)
        con.execute("INSERT INTO messages(outage_id, text, text_sha256, tip, mesaj_tarihi, ilan_bitis, received_at, "
                    "kanal, imported_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (oid, msg["text"], msg["sha"], msg["tip"], msg["tarih"], _iso(msg["bitis"]), received_at, kanal, now))
    refresh_status(con)
    return stats


# --------------------------------------------------------------------------- düzenli (kalıplı) giriş

FORM_TEMPLATE = """/kesinti
tarih: 15.10.2026
baslangic: 14:00
bitis: 22:00
ilce: Fethiye
yer: Tuzla Mah. 644 Sokak
mahalleler: Babataşı, Menteşeoğlu, Tuzla
sebep: Şebeke hattı arızası
tur: plansız
not:"""

FORM_KEYS = {  # katlanmış anahtar -> alan
    "TARIH": "tarih", "GUN": "tarih",
    "BASLANGIC": "baslangic", "BASLAMA": "baslangic", "BASLANGIC SAATI": "baslangic", "SAAT": "baslangic",
    "BITIS": "bitis", "BITIS SAATI": "bitis", "KADAR": "bitis", "BITIS TARIHI": "bitis_tarihi",
    "ILCE": "ilce",
    "YER": "yer", "KESINTI YERI": "yer", "ARIZA YERI": "yer", "KONUM": "yer", "ADRES": "yer",
    "MAHALLELER": "mahalleler", "MAHALLE": "mahalleler", "ETKILENEN": "mahalleler",
    "ETKILENEN MAHALLELER": "mahalleler",
    "SEBEP": "sebep", "NEDEN": "sebep",
    "TUR": "tur", "KESINTI TURU": "tur",
    "NOT": "not", "ACIKLAMA": "not",
}


def _fold(text):
    from .queries import fold
    return fold(text)


def parse_form(text: str) -> dict:
    """'anahtar: değer' satırlarını alanlara çevirir (Türkçe karakter/büyük-küçük harf duyarsız)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*([^:=]+?)\s*[:=]\s*(.*)$", line)
        if not m:
            continue
        key = FORM_KEYS.get(_fold(m.group(1)))
        if key and clean(m.group(2)):
            out[key] = clean(m.group(2))
    return out


def _form_date(text):
    """'15.10.2026' / '15/10/2026' / '2026-10-15' -> datetime; yoksa None; geçersizse ValueError."""
    text = text or ""
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=config.TZ)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=config.TZ)
    return None


def _form_time(day, text):
    """Saat alanı; içinde tarih de yazılmışsa ('16.10.2026 02:00') o gün kullanılır."""
    text = text or ""
    d = _form_date(text)
    if d:
        day = d
        text = re.sub(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{1,2}-\d{1,2}", " ", text)
    m = re.search(r"(?<![\d.:])(\d{1,2})[.:](\d{2})(?![\d])", text)
    return _time(day, f"{m.group(1)}.{m.group(2)}") if m else None


def form_sha(form: dict) -> str:
    keys = ("tarih", "baslangic", "bitis", "bitis_tarihi", "ilce", "yer", "mahalleler")
    return norm_sha("FORM|" + "|".join(clean(form.get(k, "")) for k in keys))


def add_structured(con, form: dict, kanal="telegram_form", received_at=None, raw_text=None) -> dict:
    """Kalıplı girişten 'mesaj' türünde kesinti ekler. Dönüş: {'durum': yeni|mukerrer|hata, 'id', 'hatalar'}."""
    errors = []
    try:
        day = _form_date(form.get("tarih", ""))
    except ValueError:
        day = None
    if not day:
        errors.append("tarih eksik ya da geçersiz (örn. 15.10.2026)")
    mahalleler = _split_names(tr_upper(form.get("mahalleler", "")))
    yer = clean(form.get("yer", "")) or None
    if yer:  # SMS kayıtlarıyla aynı biçim: "TUZLA Mah. 644 SOKAK"
        yer = clean(re.sub(r"\bMAH(?:ALLESİ)?\b\.?", "Mah.", tr_upper(yer)))
    if not mahalleler and yer:
        m = re.match(r"(.+?)\s+MAH", tr_upper(yer))
        if m:
            mahalleler = [clean(m.group(1))]
    if not mahalleler:
        errors.append("mahalleler eksik")
    if errors:
        return {"durum": "hata", "id": None, "hatalar": errors}
    try:
        start = _form_time(day, form.get("baslangic"))
    except ValueError:
        return {"durum": "hata", "id": None, "hatalar": ["başlangıç saati geçersiz (örn. 14:00)"]}
    try:
        end_day = _form_date(form.get("bitis_tarihi", "")) or day
        end = _form_time(end_day, form.get("bitis"))
    except ValueError:
        return {"durum": "hata", "id": None, "hatalar": ["bitiş saati/tarihi geçersiz (örn. 22:00)"]}
    if start and end and end <= start:
        end += timedelta(days=1)
    tur_txt = _fold(form.get("tur", ""))
    tur = "Planlı" if tur_txt.startswith("PLANLI") else "Plansız"
    ilce = tr_upper(form.get("ilce", "")) or DEFAULT_ILCE
    kesinti_tarihi = _iso(start) if start else day.date().isoformat()

    raw = raw_text or "\n".join(f"{k}: {v}" for k, v in form.items())
    sha = form_sha(form)
    prev = con.execute("SELECT outage_id FROM messages WHERE text_sha256=?", (sha,)).fetchone()
    if prev:
        return {"durum": "mukerrer", "id": prev["outage_id"], "hatalar": []}
    dup = con.execute("SELECT id FROM outages WHERE source='mesaj' AND substr(kesinti_tarihi,1,10)=? "
                      "AND COALESCE(ariza_yeri,'')=COALESCE(?,'') AND COALESCE(tahmini_bitis,'')=COALESCE(?,'') "
                      "AND mahalleler_text=?",
                      (day.date().isoformat(), yer, _iso(end), ", ".join(mahalleler))).fetchone()
    if dup:
        return {"durum": "mukerrer", "id": dup["id"], "hatalar": []}

    now = db.now_iso()
    sure = round((end - start).total_seconds() / 3600, 2) if start and end else None
    cur = con.execute(
        "INSERT INTO outages(source, ilce, kesinti_tarihi, kesinti_tarihi_raw, tahmini_sure_saat, tahmini_bitis, "
        "ilk_ilan_bitis, kesinti_turu, durum, ariza_yeri, sebep, sms_text, mahalleler_text, notes, created_at, updated_at) "
        "VALUES ('mesaj',?,?,?,?,?,?,?,'devam',?,?,?,?,?,?,?)",
        (ilce, kesinti_tarihi, form.get("tarih"), sure, _iso(end), _iso(end), tur, yer, form.get("sebep"), raw, "",
         form.get("not") or (None if start else "Başlangıç saati girilmedi."), now, now))
    oid = cur.lastrowid
    _event(con, oid, now, "yeni", "kaynak", None, f"mesaj ({kanal})")
    _set_neighborhoods(con, oid, mahalleler)
    con.execute("INSERT INTO messages(outage_id, text, text_sha256, tip, mesaj_tarihi, ilan_bitis, received_at, kanal, "
                "imported_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (oid, raw, sha, "form", day.date().isoformat(), _iso(end), received_at, kanal, now))
    refresh_status(con)
    return {"durum": "yeni", "id": oid, "hatalar": []}


def main():
    from . import logsetup
    log = logsetup.setup("sms")
    ap = argparse.ArgumentParser(description="MUSKİ kesinti SMS'lerini içe aktar")
    ap.add_argument("dosya")
    ap.add_argument("--eski-once", action="store_true", help="dosyada en eski mesaj en üstte")
    ap.add_argument("--deneme", action="store_true", help="veritabanına yazma, sadece göster")
    args = ap.parse_args()
    with open(args.dosya, encoding="utf-8") as fh:
        texts = split_messages(fh.read())
    if not args.eski_once:
        texts.reverse()
    con = db.connect()
    stats = import_messages(con, texts)
    if args.deneme:
        con.rollback()
    else:
        con.commit()
    log.info("SMS içe aktarma (%s, deneme=%s): %s", args.dosya, args.deneme,
             {k: v for k, v in stats.items() if k != "anlasilamayanlar"})
    for t in stats["anlasilamayanlar"]:
        log.warning("Anlaşılamayan mesaj: %s", t[:300])
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
