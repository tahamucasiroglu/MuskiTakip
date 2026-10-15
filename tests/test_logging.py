import logging

from muskitakip import config, logsetup


def test_logging_files_redaction_and_page(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("MUSKI_TG_TOKEN", "SAHTE-GIZLI-DEGER-SECRET")
    monkeypatch.setattr(logsetup, "_done", False)
    log = logsetup.setup("testbileşen")
    log.info("merhaba")
    log.error("token SAHTE-GIZLI-DEGER-SECRET sızmamalı")
    try:
        raise ValueError("deneme hatası")
    except ValueError:
        log.exception("yakalandı")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "logs" / "testbileşen.log").read_text(encoding="utf-8")
    assert "SECRET" not in text and "***" in text and "Traceback" in text
    recs = logsetup.read_records(tmp_path / "logs" / "testbileşen.log")
    assert [r["level"] for r in recs] == ["INFO", "ERROR", "ERROR"] and recs[-1]["extra"]
    assert len(logsetup.recent_problems(1, "ERROR")) == 2
    assert logsetup.debug_dump("x", b"abc", "png").exists()

    from muskitakip.web import app
    c = app.test_client()
    assert c.get("/loglar").status_code == 200
    assert "deneme hatası" in c.get("/loglar?dosya=testbileşen.log&seviye=ERROR").get_data(as_text=True)
    assert c.get("/loglar/indir/testbileşen.log").status_code == 200
    assert c.get("/loglar/indir/..%2Fmuski.db").status_code == 404
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
        h.close()
