"""MUSKİ su kesintileri sayfası (HTML) ve ana sayfa JSON API'si için ayrıştırıcılar."""
import json
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from . import config


def clean(text) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def norm_upper(text: str) -> str:
    """Türkçe büyük harf normalizasyonu (eşleştirme için)."""
    t = clean(text).replace("i", "İ").replace("ı", "I")
    return t.upper()


def parse_tr_datetime(text: str):
    """'15.10.2026 17:53' -> datetime (+03:00)."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", text or "")
    if not m:
        return None
    d, mo, y, h, mi, s = m.groups()
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=config.TZ)


def parse_iso(text: str):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt.astimezone(config.TZ)


def parse_duration_hours(text: str):
    """'3 Saat' -> 3.0, '30 Dakika' -> 0.5, '1 Gün' -> 24.0. Anlaşılamazsa None."""
    t = clean(text).lower().replace(",", ".")
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(gün|gun|saat|sa|dakika|dk)", t):
        found = True
        n = float(num)
        if unit.startswith("g"):
            total += n * 24
        elif unit in ("saat", "sa"):
            total += n
        else:
            total += n / 60
    if found:
        return total
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", t)
    return float(m.group(1)) if m else None


def split_neighborhoods(text: str) -> list[str]:
    out = []
    for part in clean(text).split(","):
        name = re.sub(r"\s+(Mah\.?|Mahallesi|MAH\.?)$", "", clean(part), flags=re.I).strip()
        if name and name not in out:
            out.append(name)
    return out


def minute_key(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else ""


# --------------------------------------------------------------------------- HTML

def parse_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    summary = {}
    box = soup.select_one("#outagesSummary") or soup.select_one('[data-part="summary"]')
    if box:
        for div in box.find_all("div"):
            kids = div.find_all("div", recursive=False)
            if len(kids) == 2 and not kids[0].find("div"):
                label = clean(kids[0].get_text())
                val = clean(kids[1].get_text())
                if val.isdigit():
                    summary[label] = int(val)

    last_updated = soup.select_one("#lastUpdated")
    correlation = soup.select_one("[data-correlation-id]")

    cards = []
    for idx, card in enumerate(soup.select(".outage-detail-card")):
        cards.append(parse_card(card, idx))

    # Kart yoksa listede ne yazdığına bak: MUSKİ servisi çöktüğünde "Servis ile iletişim kurulamadığı için
    # bilgiler şu anda alınamıyor" yazıyor ve özet 0 gösteriyor. Bu "kesinti kalmadı" demek DEĞİLDİR.
    grid = soup.select_one("#outagesGrid") or soup.select_one('[data-part="grid"]')
    grid_message = clean(grid.get_text(" ")) if grid is not None and not cards else ""
    site_error = None
    if grid is not None and not cards:
        legit_empty = re.search(r"BULUNMU|BULUNMAMAKTA|KESİNTİ YOK|AKTİF .*YOK", norm_upper(grid_message))
        if not legit_empty:
            site_error = grid_message or "Kesinti listesi boş geldi (açıklama yok)"

    return {
        "summary": summary,
        "last_updated": clean(last_updated.get_text()) if last_updated else None,
        "correlation_id": correlation.get("data-correlation-id") if correlation else None,
        "regions": [clean(o.get_text()) for o in soup.select("#regionFilter option") if o.get("value")],
        "cards": cards,
        "grid_found": grid is not None,
        "grid_message": grid_message,
        "site_error": site_error,
    }


def parse_card(card, idx: int) -> dict:
    header = card.select_one(".outage-detail-card__header h3")
    pill = card.select_one(".status-pill")
    details = {}
    for item in card.select(".detail-item"):
        label = item.select_one(".detail-label")
        value = item.select_one(".detail-value")
        if label is None or value is None:
            continue
        details[clean(label.get_text())] = value

    def text_of(key):
        v = details.get(key)
        return clean(v.get_text()) if v is not None else None

    tarih_raw = text_of("Kesinti Tarihi")
    sure_raw = text_of("Tahmini Süre")
    mah_raw = text_of("Etkilenen Mahalleler")
    tur_raw = text_of("Kesinti Türü")
    sure_el = details.get("Tahmini Süre")
    tur_el = details.get("Kesinti Türü")
    badge = sure_el.select_one(".duration-badge") if sure_el is not None else None
    tbadge = tur_el.select_one(".type-badge") if tur_el is not None else None

    # Bilinmeyen/yeni eklenmiş alanlar da kaybolmasın.
    extra = {k: clean(v.get_text()) for k, v in details.items()
             if k not in ("Kesinti Tarihi", "Tahmini Süre", "Etkilenen Mahalleler", "Kesinti Türü")}

    return {
        "index": idx,
        "attrs": {k: (" ".join(v) if isinstance(v, list) else v) for k, v in card.attrs.items()},
        "region": clean(card.get("data-region") or (header.get_text() if header else "")),
        "status": clean(card.get("data-status")).lower(),
        "type": clean(card.get("data-type")).lower(),
        "status_text": clean(pill.get_text()) if pill else None,
        "status_class": " ".join(pill.get("class", [])) if pill else None,
        "kesinti_tarihi_raw": tarih_raw,
        "kesinti_tarihi": parse_tr_datetime(tarih_raw),
        "tahmini_sure_text": sure_raw,
        "tahmini_sure_saat": parse_duration_hours(sure_raw) if sure_raw else None,
        "duration_class": " ".join(badge.get("class", [])) if badge else None,
        "mahalleler_raw": mah_raw,
        "mahalleler": split_neighborhoods(mah_raw or ""),
        "tur_etiketi": tur_raw,
        "type_class": " ".join(tbadge.get("class", [])) if tbadge else None,
        "extra": extra,
        "html": str(card),
    }


# --------------------------------------------------------------------------- JSON API

def parse_api(text: str) -> dict:
    payload = json.loads(text)
    items = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for raw in items or []:
        out.append(parse_api_item(raw))
    return {
        "is_success": payload.get("isSuccess") if isinstance(payload, dict) else None,
        "from_cache": payload.get("fromCache") if isinstance(payload, dict) else None,
        "message": payload.get("message") if isinstance(payload, dict) else None,
        "items": out,
    }


def parse_api_item(raw: dict) -> dict:
    adresler = raw.get("aysSuKesintisiAdresDtoList") or []
    ilce = {}
    mahalleler = []
    for adr in adresler:
        dto = adr.get("absIlceDto") or {}
        if dto and not ilce:
            ilce = dto
        for m in adr.get("absMahalleDtoList") or []:
            name = clean(m.get("mahalleAdi"))
            if name and name not in [x["mahalle"] for x in mahalleler]:
                mahalleler.append({"mahalle": name, "mahalle_id": m.get("id"), "uavt_kodu": m.get("uavtKodu")})
    durum = raw.get("aysIsEmriDurumDto") or {}
    boru = raw.get("boruCinsi")
    if isinstance(boru, list):
        boru = ", ".join(str(b) for b in boru)
    tamam = str(durum.get("tamamlandi") or "").lower() in ("evet", "true") or \
        str(durum.get("sabitAdi") or "").upper() == "TAMAMLANDI"
    return {
        "muski_id": raw.get("id"),
        "ays_is_emri_id": raw.get("aysIsEmriId"),
        "kesinti_tarihi_raw": raw.get("kesintiTarihi"),
        "kesinti_tarihi": parse_iso(raw.get("kesintiTarihi")),
        "tahmini_sure_saat": raw.get("suKesintisiSuresi"),
        "kesinti_turu": clean(raw.get("kesintiTuru")) or None,
        "kesinti_yayinlansin": raw.get("kesintiYayinlansin"),
        "api_durum": raw.get("durum"),
        "is_emri_durum_id": durum.get("id"),
        "is_emri_durum_adi": durum.get("adi"),
        "is_emri_renk": durum.get("renk"),
        "is_emri_tamamlandi": durum.get("tamamlandi"),
        "sahaya_gonderildi": durum.get("sahayaGonderildi"),
        "tamamlandi": tamam,
        "boru_cinsi": boru,
        "ilce": clean(ilce.get("adi")),
        "ilce_id": ilce.get("id"),
        "ilce_uavt": ilce.get("uavtKodu"),
        "ilce_mernis": ilce.get("mernisKodu"),
        "il": clean(ilce.get("absIlAdi")) or None,
        "il_id": ilce.get("absIlId"),
        "mahalleler": mahalleler,
        "raw": raw,
    }


def tahmini_bitis(start, hours):
    if not start or hours in (None, ""):
        return None
    try:
        return start + timedelta(hours=float(hours))
    except (TypeError, ValueError):
        return None
