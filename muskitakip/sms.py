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
        p = clean(re.sub(r"\b(MAHALLESİNDE|MAHALLELERİNDE|MAHALLESİ|MAH\.?)\b", "", p))
        if p and p not in out:
            out.append(p)
    return out


def _time(date, hhmm):
    h, m = re.split(r"[.:]", hhmm)
    return date.replace(hour=int(h) % 24, minute=int(m))


def parse_message(text: str) -> dict | None:
    """Tek bir kesinti SMS'ini ayrıştırır; kesinti mesajı değilse None."""
    if not is_outage(text):
        return None
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
    rows = con.execute("SELECT * FROM outages WHERE source='mesaj' AND ariza_yeri=? ORDER BY tahmini_bitis DESC",
                       (msg["ariza_yeri"],)).fetchall()
    for r in rows:
        prev = datetime.fromisoformat(r["tahmini_bitis"])
        if prev <= msg["bitis"] <= prev + JOIN_WINDOW:
            return r
    return None


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
             "yeni_kesinti": 0, "mevcut_kesintiye_eklendi": 0, "anlasilamayanlar": []}
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
            con.execute("UPDATE messages SET adet=adet+1 WHERE id=?", (existing_msg["id"],))
            stats["mukerrer"] += 1
            continue

        group = find_group(con, msg)
        if group:
            oid = group["id"]
            old_end = group["tahmini_bitis"]
            new_end = _iso(max(datetime.fromisoformat(old_end), msg["bitis"]))
            if new_end != old_end:
                con.execute("UPDATE outages SET tahmini_bitis=?, updated_at=? WHERE id=?", (new_end, now, oid))
                _event(con, oid, now, "sure_uzatildi" if msg["tip"] == "devam" else "dolum_mesaji",
                       "tahmini_bitis", old_end, new_end)
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
        con.execute("INSERT INTO messages(outage_id, text, text_sha256, tip, mesaj_tarihi, ilan_bitis, received_at, "
                    "kanal, imported_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (oid, msg["text"], msg["sha"], msg["tip"], msg["tarih"], _iso(msg["bitis"]), received_at, kanal, now))
    refresh_status(con)
    return stats


def main():
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
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
