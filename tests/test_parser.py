from pathlib import Path

import pytest

from muskitakip import db, scraper, sms
from muskitakip.parser import parse_api, parse_duration_hours, parse_page

FIX = Path(__file__).parent / "fixtures"
HTML = (FIX / "su-kesintileri.html").read_text(encoding="utf-8")
API = (FIX / "api-home-su-kesintileri.json").read_text(encoding="utf-8")


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_parse_page():
    page = parse_page(HTML)
    assert len(page["cards"]) == 15
    assert page["summary"]["Toplam"] == 15
    c = page["cards"][0]
    assert c["region"] == "FETHİYE" and c["status"] == "devam"
    assert c["tahmini_sure_saat"] == 3
    assert "TUZLA" in c["mahalleler"]


def test_parse_api():
    api = parse_api(API)
    assert len(api["items"]) == 5
    it = api["items"][0]
    assert it["muski_id"] == 155162 and it["ays_is_emri_id"] == 5710912
    assert it["mahalleler"][0]["uavt_kodu"]


def test_duration():
    assert parse_duration_hours("3 Saat") == 3
    assert parse_duration_hours("30 Dakika") == 0.5
    assert parse_duration_hours("1 Gün") == 24


def test_process_idempotent_and_gone(con):
    def run(html, api):
        sid = con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (db.now_iso(),)).lastrowid
        return scraper.process(con, sid, html, api, db.now_iso())
    assert run(HTML, API)["new"] == 15
    r = run(HTML, API)
    assert r["new"] == 0 and r["gone"] == 0
    assert con.execute("SELECT COUNT(*) FROM outages WHERE muski_id IS NOT NULL").fetchone()[0] == 5
    # yalnızca HTML gelirse (API yok) saniyeli tarih ve ID'ler korunmalı
    run(HTML, None)
    row = con.execute("SELECT kesinti_tarihi, muski_id FROM outages WHERE muski_id=155162").fetchone()
    assert row["kesinti_tarihi"] == "2026-10-15T17:53:37+03:00"
    # bir kart kaybolunca "siteden kalktı"
    r = run(HTML.replace('class="outage-detail-card"', 'class="x"', 1), None)
    assert r["gone"] == 1
    # boş/bozuk sayfa gelirse hiçbiri kalkmış sayılmamalı
    assert run("<html></html>", None)["gone"] == 0


def test_sms_parse_and_group(con):
    raw = (Path(__file__).parent.parent / "data/sms/2026-10-15-ilk-aktarim.txt").read_text(encoding="utf-8")
    texts = sms.split_messages(raw)[::-1]
    stats = sms.import_messages(con, texts)
    assert stats["anlasilamadi"] == 0
    assert stats["yeni_kesinti"] == 55
    assert sms.import_messages(con, texts)["yeni_kesinti"] == 0  # tekrar aktarım mükerrer üretmez
    row = con.execute("SELECT * FROM outages WHERE ariza_yeri='TAŞYAKA Mah. 156 SOKAK'").fetchone()
    assert row["ilk_ilan_bitis"].startswith("2026-10-15T12:30") and row["tahmini_bitis"].startswith("2026-10-15T13:30")
    assert con.execute("SELECT COUNT(*) FROM outages WHERE source='web'").fetchone()[0] == 0


def test_sms_ignores_bills():
    assert sms.parse_message("Sayin abonemiz, FETHIYE Ilcesi TUZLA Mahallesi ... faturaniz 818.65 TL B002") is None
