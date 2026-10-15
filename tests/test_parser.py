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
    import re as _re
    one_less = _re.sub(r"(Toplam</div>\s*<div[^>]*>)15(</div>)", r"\g<1>14",
                       HTML.replace('class="outage-detail-card"', 'class="x"', 1))
    r = run(one_less, None)
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


def test_bad_card_and_bad_api_do_not_kill_run(con):
    sid = con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (db.now_iso(),)).lastrowid
    bad = HTML.replace("15.10.2026 09:18", "-", 1)
    errors = []
    r = scraper.process(con, sid, bad, "<html>Request blocked</html>", db.now_iso(), errors)
    assert r["new"] == 14 and r["gone"] == 0 and len(errors) == 2


def test_same_minute_different_outages_not_merged(con):
    from muskitakip.parser import parse_tr_datetime
    def card(mah):
        return {"region": "MARMARİS", "kesinti_tarihi": parse_tr_datetime("15.10.2026 13:30"),
                "kesinti_tarihi_raw": "15.10.2026 13:30", "tahmini_sure_saat": 2, "tahmini_sure_text": "2 Saat",
                "type": "plansiz", "tur_etiketi": "x", "status": "devam", "status_text": "Devam", "html": "",
                "mahalleler": [mah]}
    for _ in range(2):
        con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (db.now_iso(),))
    used = set()
    scraper.upsert(con, 1, scraper.build_record(card("BELDİBİ"), None), db.now_iso(), used)
    used = set()
    assert scraper.upsert(con, 2, scraper.build_record(card("YENİMAHALLE"), None), db.now_iso(), used) == "new"
    assert scraper.upsert(con, 2, scraper.build_record(card("BELDİBİ"), None), db.now_iso(), used) == "same"


def test_sms_24_and_overnight():
    m = sms.parse_message("TUZLA MAHALLESİNDE YAPILACAK OLAN BAKIM ONARIM ÇALIŞMASI NEDENİYLE 01.11.2026 (YARIN) "
                          "22.00 - 02.00 SAATLERİ ARASINDA MAHALLE GENELİNDE SU KESİNTİSİ YAŞANABİLİR. B002")
    assert m["bitis"] > m["baslangic"]
    m = sms.parse_message("TUZLA MAHALLESİNDE MEYDANA GELEN ŞEBEKE HATTI ARIZASI NEDENİYLE 01.11.2026 (BUGÜN) "
                          "SAAT 24.00'E KADAR MAHALLE GENELİNDE SU KESİNTİSİ YAŞANABİLİR. B002")
    assert m["bitis"].day == 2 and m["bitis"].hour == 0


def test_sms_reimport_keeps_count(con):
    t = ("TUZLA MAHALLESİNDE MEYDANA GELEN ŞEBEKE HATTI ARIZASI NEDENİYLE 01.11.2026 (BUGÜN) SAAT 14.00'E KADAR "
         "MAHALLE GENELİNDE SU KESİNTİSİ YAŞANABİLİR. B002")
    sms.import_messages(con, [t, t])
    sms.import_messages(con, [t, t])
    assert con.execute("SELECT adet FROM messages").fetchone()[0] == 2


SITE_ERROR_HTML = HTML[:HTML.index('<div class="outages-detail__grid" id="outagesGrid">')] + (
    '<div class="outages-detail__grid" id="outagesGrid"><div style="grid-column: 1 / -1"><i></i> '
    'Servis ile iletişim kurulamadığı için bilgiler şu anda alınamıyor. Lütfen daha sonra tekrar deneyiniz.</div></div>'
    '</div></section></main></body></html>')


def test_site_error_page_does_not_mark_gone(con):
    def run(html, api):
        sid = con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (db.now_iso(),)).lastrowid
        errs = []
        return scraper.process(con, sid, html, api, db.now_iso(), errs), errs, sid
    run(HTML, API)
    page = parse_page(SITE_ERROR_HTML)
    assert page["cards"] == [] and "iletişim kurulamad" in page["site_error"]
    r, errs, _ = run(SITE_ERROR_HTML, API)
    assert r["gone"] == 0 and r["site_error"] and any("hata gösteriyor" in e for e in errs)
    assert con.execute("SELECT COUNT(*) FROM outages WHERE gone_at IS NOT NULL").fetchone()[0] == 0


def test_undo_gone(con):
    sid0 = con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (db.now_iso(),)).lastrowid
    scraper.process(con, sid0, HTML, API, db.now_iso())
    sid = con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (db.now_iso(),)).lastrowid
    scraper.mark_gone(con, sid, db.now_iso(), set())
    assert scraper.undo_gone(con, sid, "test") == 15
    assert con.execute("SELECT COUNT(*) FROM outages WHERE gone_at IS NOT NULL OR durum='kalkti'").fetchone()[0] == 0


def test_partial_card_list_does_not_mark_gone(con):
    sid = con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (db.now_iso(),)).lastrowid
    scraper.process(con, sid, HTML, API, db.now_iso())
    partial = HTML
    for _ in range(5):
        partial = partial.replace('class="outage-detail-card"', 'class="x"', 1)
    sid = con.execute("INSERT INTO scrapes(started_at) VALUES (?)", (db.now_iso(),)).lastrowid
    errs = []
    assert scraper.process(con, sid, partial, None, db.now_iso(), errs)["gone"] == 0 and errs
