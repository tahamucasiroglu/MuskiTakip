"""Web arayüzü ve dışa aktarmaların ortak kullandığı filtreleme/sorgu katmanı."""
from datetime import datetime

from . import db
from .parser import clean, parse_iso

FOLD = str.maketrans({"Ş": "S", "Ğ": "G", "Ü": "U", "Ö": "O", "Ç": "C", "İ": "I", "I": "I",
                      "ş": "S", "ğ": "G", "ü": "U", "ö": "O", "ç": "C", "ı": "I", "i": "I"})


def fold(text) -> str:
    """Türkçe karakter ve büyük/küçük harf duyarsız karşılaştırma anahtarı."""
    return clean(str(text or "")).translate(FOLD).upper()


DURUM_LABELS = {
    "devam": "Devam ediyor",
    "tamamlandi": "Tamamlandı",
    "kalkti": "Siteden kalktı (bitti)",
    "ilan_bitti": "Bitti (ilan edilen saat geçti)",
}
KAYNAK_LABELS = {"web": "Web sitesi", "mesaj": "Mesaj (SMS)"}
DURUM_FILTERS = {
    "": "Tümü",
    "devam": "Devam edenler",
    "bitti": "Bitenler (tamamlandı + siteden kalktı + ilan saati geçti)",
    "tamamlandi": "Sitede 'Tamamlandı' görünenler",
    "kalkti": "Tamamlanmadan siteden kalkanlar",
    "sitede": "Şu an sitede görünenler",
}
SORTS = {
    "tarih_desc": "o.kesinti_tarihi DESC, o.id DESC",
    "tarih_asc": "o.kesinti_tarihi ASC, o.id ASC",
    "ilce": "o.ilce ASC, o.kesinti_tarihi DESC",
    "ilk_gorulme": "o.first_seen_at DESC, o.id DESC",
}


def connect():
    con = db.connect()
    con.create_function("FOLD", 1, fold, deterministic=True)
    return con


def get_filters(args) -> dict:
    """request.args benzeri bir sözlükten temizlenmiş filtreler."""
    def g(k):
        return clean(args.get(k, "") or "")
    f = {k: g(k) for k in ("ilce", "mahalle", "durum", "tur", "bas", "bit", "q", "kaynak", "sirala")}
    if f["sirala"] not in SORTS:
        f["sirala"] = "tarih_desc"
    if f["durum"] not in DURUM_FILTERS:
        f["durum"] = ""
    for k in ("bas", "bit"):
        try:
            datetime.strptime(f[k], "%Y-%m-%d")
        except ValueError:
            f[k] = ""
    return f


def build_where(f: dict):
    where, params = [], []
    if f.get("ilce"):
        where.append("FOLD(o.ilce) = FOLD(?)")
        params.append(f["ilce"])
    if f.get("mahalle"):
        where.append("EXISTS (SELECT 1 FROM outage_neighborhoods n WHERE n.outage_id=o.id "
                     "AND FOLD(n.mahalle) LIKE '%' || FOLD(?) || '%')")
        params.append(f["mahalle"])
    d = f.get("durum")
    if d == "devam":
        where.append("o.durum='devam'")
    elif d == "bitti":
        where.append("o.durum IN ('tamamlandi','kalkti','ilan_bitti')")
    elif d == "tamamlandi":
        where.append("o.durum='tamamlandi'")
    elif d == "kalkti":
        where.append("o.durum='kalkti'")
    elif d == "sitede":
        where.append("o.gone_at IS NULL AND o.source='web'")
    if f.get("tur") == "plansiz":
        where.append("(FOLD(o.kesinti_turu) LIKE '%PLANSIZ%' OR o.kesinti_turu_kod='plansiz')")
    elif f.get("tur") == "planli":
        where.append("(FOLD(o.kesinti_turu) = 'PLANLI' OR o.kesinti_turu_kod='planli')")
    if f.get("bas"):
        where.append("substr(o.kesinti_tarihi,1,10) >= ?")
        params.append(f["bas"])
    if f.get("bit"):
        where.append("substr(o.kesinti_tarihi,1,10) <= ?")
        params.append(f["bit"])
    if f.get("kaynak"):
        where.append("o.source = ?")
        params.append(f["kaynak"])
    if f.get("q"):
        q = f["q"]
        where.append("(FOLD(o.ilce) LIKE '%'||FOLD(?)||'%' OR FOLD(o.mahalleler_text) LIKE '%'||FOLD(?)||'%' "
                     "OR FOLD(o.ariza_yeri) LIKE '%'||FOLD(?)||'%' OR FOLD(o.sebep) LIKE '%'||FOLD(?)||'%' "
                     "OR CAST(o.muski_id AS TEXT)=? OR CAST(o.ays_is_emri_id AS TEXT)=? OR CAST(o.id AS TEXT)=? "
                     "OR FOLD(o.notes) LIKE '%'||FOLD(?)||'%' OR FOLD(o.sms_text) LIKE '%'||FOLD(?)||'%')")
        params += [q] * 9
    return (" WHERE " + " AND ".join(where)) if where else "", params


def search(con, f: dict, limit: int | None = None):
    where, params = build_where(f)
    sql = f"SELECT o.* FROM outages o{where} ORDER BY {SORTS[f['sirala']]}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [enrich(dict(r)) for r in con.execute(sql, params).fetchall()]


def neighborhoods_for(con, ids):
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in con.execute(f"SELECT * FROM outage_neighborhoods WHERE outage_id IN ({','.join('?' * len(chunk))}) "
                             "ORDER BY rowid", chunk):
            out.setdefault(r["outage_id"], []).append(dict(r))
    return out


def hours_between(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 2)


def enrich(o: dict) -> dict:
    """Türetilmiş alanlar: bitiş aralığı, gerçekleşen süre üst sınırı vb."""
    start = parse_iso(o.get("kesinti_tarihi"))
    completed = parse_iso(o.get("completed_seen_at"))
    gone = parse_iso(o.get("gone_at"))
    last_active = parse_iso(o.get("last_active_seen_at"))
    end_upper = completed or (gone if o.get("durum") == "kalkti" else None)
    end_lower = last_active if (last_active and end_upper and last_active < end_upper) else None
    o["durum_label"] = DURUM_LABELS.get(o.get("durum"), o.get("durum"))
    o["kaynak_label"] = KAYNAK_LABELS.get(o.get("source"), o.get("source"))
    o["uzatildi"] = bool(o.get("ilk_ilan_bitis") and o.get("tahmini_bitis") and o["ilk_ilan_bitis"] != o["tahmini_bitis"])
    o["bitis_en_gec"] = end_upper.isoformat(timespec="seconds") if end_upper else None
    o["bitis_en_erken"] = end_lower.isoformat(timespec="seconds") if end_lower else None
    o["sure_en_fazla_saat"] = hours_between(start, end_upper)
    o["sure_en_az_saat"] = hours_between(start, end_lower)
    est = o.get("tahmini_sure_saat")
    o["tahmin_asildi"] = bool(end_lower and est is not None and o["sure_en_az_saat"] > float(est))
    o["sitede"] = o.get("source") == "web" and not o.get("gone_at")
    return o


def fmt_dt(value, with_seconds=False) -> str:
    dt = parse_iso(value) if isinstance(value, str) else value
    if not dt:
        return ""
    if isinstance(value, str) and len(value) == 10:  # yalnızca tarih (SMS'te saat yok)
        return dt.strftime("%d.%m.%Y")
    return dt.strftime("%d.%m.%Y %H:%M:%S" if with_seconds else "%d.%m.%Y %H:%M")


def fmt_hours(h) -> str:
    if h is None or h == "":
        return ""
    h = float(h)
    total_min = int(round(h * 60))
    hh, mm = divmod(total_min, 60)
    if hh and mm:
        return f"{hh} sa {mm} dk"
    if hh:
        return f"{hh} sa"
    return f"{mm} dk"


def filter_summary(f: dict) -> str:
    parts = []
    if f.get("ilce"):
        parts.append(f"İlçe: {f['ilce']}")
    if f.get("mahalle"):
        parts.append(f"Mahalle: {f['mahalle']}")
    if f.get("durum"):
        parts.append(f"Durum: {DURUM_FILTERS[f['durum']]}")
    if f.get("tur"):
        parts.append("Tür: " + {"plansiz": "Plansız", "planli": "Planlı"}.get(f["tur"], f["tur"]))
    if f.get("bas") or f.get("bit"):
        parts.append(f"Tarih: {f.get('bas') or '…'} – {f.get('bit') or '…'}")
    if f.get("kaynak"):
        parts.append(f"Kayıt türü: {KAYNAK_LABELS.get(f['kaynak'], f['kaynak'])}")
    if f.get("q"):
        parts.append(f"Arama: {f['q']}")
    return "; ".join(parts) or "Filtre yok (tüm kayıtlar)"


def ilce_list(con):
    return [r[0] for r in con.execute("SELECT DISTINCT ilce FROM outages WHERE ilce<>'' ORDER BY ilce")]


def mahalle_list(con):
    return [r[0] for r in con.execute("SELECT DISTINCT mahalle FROM outage_neighborhoods ORDER BY mahalle")]
