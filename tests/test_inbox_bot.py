import pytest

from muskitakip import db, inbox, sms, telegram_bot

SMS = ("SAYIN ABONEMİZ; TUZLA MAHALLESİ 644 SOKAKTA MEYDANA GELEN İÇME SUYU ŞEBEKE HATTI ARIZASI NEDENİYLE "
       "15.10.2026 (BUGÜN) SAAT 22.00'YE KADAR BABATAŞI VE TUZLA MAHALLESİNDE SU KESİNTİSİ YAŞANABİLİR. "
       "ANLAYIŞINIZ İÇİN TEŞEKKÜR EDERİZ. B002")


@pytest.fixture
def con(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(db.config, "DB_PATH", path)
    c = db.connect(path)
    yield c
    c.close()


def test_inbox_auto(con):
    iid, new = inbox.save(con, SMS, chat_id=1, message_id=10)
    assert new and inbox.save(con, SMS, chat_id=1, message_id=10) == (iid, False)
    assert inbox.pending_count(con) == 1
    res = inbox.auto_process(con, iid)
    assert res["ok"] and res["yeni_kesinti"] == 1
    row = con.execute("SELECT * FROM inbox WHERE id=?", (iid,)).fetchone()
    assert row["durum"] == "eklendi" and row["outage_ids"]
    o = con.execute("SELECT * FROM outages").fetchone()
    assert o["source"] == "mesaj" and o["ariza_yeri"] == "TUZLA Mah. 644 SOKAK"


def test_inbox_non_outage(con):
    iid, _ = inbox.save(con, "Sayin abonemiz faturaniz 818 TL B002")
    assert not inbox.auto_process(con, iid)["ok"]
    assert con.execute("SELECT durum FROM inbox").fetchone()[0] == "kesinti_degil"


def _msg(text, uid=42, mid=1, chat_type="private"):
    return {"message_id": mid, "date": 1760540000, "text": text,
            "chat": {"id": uid, "type": chat_type}, "from": {"id": uid}}


def test_bot_owner_and_modes(con, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda chat, text: sent.append((chat, text)))
    monkeypatch.setattr(telegram_bot, "CLAIM_CODE", "gizli123")
    monkeypatch.setattr(telegram_bot, "ENV_OWNER", "")
    telegram_bot.handle(con, _msg("merhaba", uid=7))
    assert "özel" in sent[-1][1] and telegram_bot.owner_id(con) is None
    telegram_bot.handle(con, _msg("/baslat yanlis", uid=7))
    assert telegram_bot.owner_id(con) is None
    telegram_bot.handle(con, _msg("/baslat gizli123", uid=42))
    assert telegram_bot.owner_id(con) == 42
    n = len(sent)
    telegram_bot.handle(con, _msg(SMS, uid=99, mid=5))  # yabancı: yok sayılır
    assert len(sent) == n and inbox.pending_count(con) == 0
    telegram_bot.handle(con, _msg(SMS, mid=6))  # düz mesaj -> gelen kutusu
    assert inbox.pending_count(con) == 1 and "Gelen kutusuna" in sent[-1][1]
    assert con.execute("SELECT COUNT(*) FROM outages").fetchone()[0] == 0
    form = "/kesinti\ntarih: 16.10.2026\nbitis: 18:00\nyer: Taşyaka Mah. 218 Sokak\nmahalleler: Taşyaka"
    telegram_bot.handle(con, _msg(form, mid=7))  # düzenli -> doğrudan kayıt
    assert "kaydedildi" in sent[-1][1]
    o = con.execute("SELECT * FROM outages").fetchone()
    assert o["ariza_yeri"] == "TAŞYAKA Mah. 218 SOKAK" and o["source"] == "mesaj"
    telegram_bot.handle(con, _msg(form, mid=8))
    assert "zaten kayıtlı" in sent[-1][1]
    for c in ("/durum", "/aktif", "/kutu", "/sablon", "/yardim", "/kesinti"):
        telegram_bot.handle(con, _msg(c, mid=100))
        assert sent[-1][1]


def test_web_inbox_and_manual(con, monkeypatch):
    from muskitakip.web import app
    c = app.test_client()
    assert c.post("/gelen-kutusu", data={"text": SMS}).status_code == 302
    assert c.get("/gelen-kutusu").status_code == 200
    iid = con.execute("SELECT id FROM inbox").fetchone()[0]
    assert c.get(f"/mesaj/yeni?gelen={iid}").status_code == 200
    r = c.post("/mesaj/yeni", data={"gelen": iid, "tarih": "2026-10-15", "bitis": "22:00", "ilce": "FETHİYE",
                                    "yer": "Tuzla Mah. 644 Sokak", "mahalleler": "Babataşı, Tuzla", "tur": "plansız"})
    assert r.status_code == 302
    assert con.execute("SELECT durum FROM inbox WHERE id=?", (iid,)).fetchone()[0] == "eklendi"
    assert c.post("/mesaj/yeni", data={"tarih": ""}).status_code == 200  # eksik alan: form tekrar gösterilir
    for fmt in ("xlsx", "pdf", "docx"):
        assert c.get(f"/disari/{fmt}?kaynak=mesaj").status_code == 200
    assert "Kesinti yeri" in c.get("/").get_data(as_text=True)
