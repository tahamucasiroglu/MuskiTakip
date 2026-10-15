"""Telegram botu (@TahaMuskiBot). Yalnızca sahibinden gelen mesajları kabul eder.

İki giriş yolu:
  1) Düz mesaj  : SMS'i olduğu gibi yapıştır -> Gelen Kutusu'na kaydedilir (sonra web'de ayıklanır).
  2) Düzenli    : /kesinti + 'anahtar: değer' satırları -> doğrudan 'mesaj' türünde kesinti kaydı.

İlk kurulumda sahip, Pi'deki gizli ayar dosyasındaki tek kullanımlık kodla belirlenir: /baslat <kod>
Kullanım:  python -m muskitakip.telegram_bot
"""
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import config, db, inbox, logsetup, sms

log = logging.getLogger("muskitakip.telegram")

TOKEN = os.environ.get("MUSKI_TG_TOKEN", "")
CLAIM_CODE = os.environ.get("MUSKI_TG_CLAIM_CODE", "")
ENV_OWNER = os.environ.get("MUSKI_TG_OWNER_ID", "")
WEB_URL = os.environ.get("MUSKI_WEB_PUBLIC_URL", f"http://localhost:{config.WEB_PORT}")

HELP = f"""💧 MUSKİ Kesinti Takip botu

1️⃣ Düz mesaj: MUSKİ'den gelen SMS'i kopyalayıp buraya yapıştır. Ayrıştırılmadan Gelen Kutusu'na kaydedilir; sonra web'de ayıklanır:
{WEB_URL}/gelen-kutusu

2️⃣ Düzenli mesaj: /kesinti yazıp alttaki satırları doldur, doğrudan kesinti kaydı olur. Şablon için: /sablon

Diğer komutlar:
/durum – son site ziyareti, aktif kesintiler, bekleyen mesajlar
/aktif – sitede devam eden kesintiler
/kutu – gelen kutusundaki son mesajlar
/yardim – bu mesaj"""

COMMANDS = [("kesinti", "Düzenli kesinti girişi (şablonlu)"), ("sablon", "Düzenli giriş şablonu"),
            ("durum", "Son site ziyareti ve özet"), ("aktif", "Sitede devam eden kesintiler"),
            ("kutu", "Gelen kutusundaki son mesajlar"), ("yardim", "Yardım")]


def api(method, http_timeout=30, **params):
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}",
                                 data=json.dumps(params).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # Telegram hata açıklamasını loga taşı (ör. 409 Conflict, 401)
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Telegram {method} HTTP {e.code}: {body}") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def send(chat_id, text):
    for i in range(0, len(text), 4000):  # Telegram mesaj sınırı 4096
        api("sendMessage", chat_id=chat_id, text=text[i:i + 4000], disable_web_page_preview=True)


def get_setting(con, key):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))


def owner_id(con):
    v = ENV_OWNER or get_setting(con, "tg_owner_id")
    return int(v) if v else None


def fmt(value):
    from .queries import fmt_dt
    return fmt_dt(value)


# --------------------------------------------------------------------------- komutlar

def cmd_durum(con):
    last = con.execute("SELECT * FROM scrapes WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
    c = con.execute("SELECT SUM(source='web' AND durum='devam') web_devam, SUM(source='web' AND gone_at IS NULL) sitede, "
                    "SUM(source='web') web, SUM(source='mesaj') mesaj FROM outages").fetchone()
    lines = ["📊 Durum"]
    if last:
        lines.append(f"Son başarılı site ziyareti: {fmt(last['finished_at'])} (sayfada {last['html_count']} kart)")
    lines += [f"Sitede devam eden: {c['web_devam'] or 0} · şu an sitede: {c['sitede'] or 0}",
              f"Toplam kayıt: web {c['web'] or 0}, mesaj {c['mesaj'] or 0}",
              f"Gelen kutusunda bekleyen: {inbox.pending_count(con)}", WEB_URL]
    return "\n".join(lines)


def cmd_aktif(con):
    rows = con.execute("SELECT * FROM outages WHERE source='web' AND durum='devam' ORDER BY kesinti_tarihi DESC").fetchall()
    if not rows:
        return "Sitede devam eden kesinti yok. ✅"
    out = [f"🚱 Devam eden kesintiler ({len(rows)})"]
    for r in rows:
        out.append(f"• {r['ilce']} – {fmt(r['kesinti_tarihi'])} (tahmini bitiş {fmt(r['tahmini_bitis'])})\n  {r['mahalleler_text']}")
    return "\n".join(out)


def cmd_kutu(con):
    rows = con.execute("SELECT id, durum, received_at, substr(replace(text, char(10), ' '), 1, 90) t FROM inbox "
                       "ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        return "Gelen kutusu boş."
    icon = {"bekliyor": "⏳", "eklendi": "✅", "kesinti_degil": "🚫"}
    return "📥 Son mesajlar\n" + "\n".join(f"{icon.get(r['durum'], '')} #{r['id']} {fmt(r['received_at'])}: {r['t']}"
                                         for r in rows) + f"\n\n{WEB_URL}/gelen-kutusu"


def cmd_kesinti(con, text, received_at):
    body = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
    form = sms.parse_form(body)
    if not form:
        return "Düzenli giriş için şablonu kopyalayıp doldur:\n\n" + sms.FORM_TEMPLATE
    res = sms.add_structured(con, form, kanal="telegram_form", received_at=received_at, raw_text=text)
    if res["durum"] == "hata":
        return "⚠️ Kaydedilmedi: " + "; ".join(res["hatalar"]) + "\n\nŞablon:\n" + sms.FORM_TEMPLATE
    if res["durum"] == "mukerrer":
        return "ℹ️ Bu kesinti zaten kayıtlı" + (f" (#{res['id']})" if res["id"] else "") + ", tekrar eklenmedi."
    o = con.execute("SELECT * FROM outages WHERE id=?", (res["id"],)).fetchone()
    return (f"✅ Kesinti #{o['id']} kaydedildi (mesaj)\n{o['ilce']} · {fmt(o['kesinti_tarihi'])}"
            f"{' → ' + fmt(o['tahmini_bitis']) if o['tahmini_bitis'] else ''}\n"
            f"Kesinti yeri: {o['ariza_yeri'] or '-'}\nMahalleler: {o['mahalleler_text']}\n{WEB_URL}/kesinti/{o['id']}")


def plain_message(con, msg, text, received_at):
    iid, new = inbox.save(con, text, "telegram", received_at, msg["chat"]["id"], msg["message_id"])
    con.commit()  # ham mesaj, sonraki adımlarda hata olsa bile kaybolmasın
    if not new:
        return None
    p = inbox.preview(text)
    lines = [f"📥 Gelen kutusuna kaydedildi (#{iid})."]
    parsed = [x["parsed"] for x in p["parcalar"] if x["parsed"]]
    if parsed:
        for m in parsed[:5]:
            lines.append(f"• Otomatik okunabiliyor: {m['ariza_yeri'] or '?'} · {m['tarih']} · bitiş "
                         f"{m['bitis']:%H:%M} · {', '.join(m['etkilenen'])}")
    elif not any(x["kesinti"] for x in p["parcalar"]):
        lines.append("• Su kesintisi mesajına benzemiyor (ayıklarken 'kesinti değil' diyebilirsin).")
    else:
        lines.append("• Otomatik okunamadı; web'de elle ekleyebilirsin.")
    lines.append(f"Ayıklamak için: {WEB_URL}/gelen-kutusu")
    return "\n".join(lines)


def handle(con, msg):
    chat, user = msg.get("chat", {}), msg.get("from", {})
    text = (msg.get("text") or msg.get("caption") or "").strip()
    log.info("Mesaj alındı: id=%s kullanıcı=%s sohbet=%s/%s uzunluk=%s başı=%r", msg.get("message_id"),
             user.get("id"), chat.get("id"), chat.get("type"), len(text), text[:60])
    received_at = datetime.fromtimestamp(msg.get("date", time.time()), config.TZ).isoformat(timespec="seconds")
    owner = owner_id(con)

    if owner is None:
        if CLAIM_CODE and text.split()[:2] == ["/baslat", CLAIM_CODE] and chat.get("type") == "private":
            set_setting(con, "tg_owner_id", user["id"])
            con.commit()
            log.info("Bot sahibi ayarlandı: %s", user["id"])
            return send(chat["id"], "✅ Bu bot artık yalnızca sana yanıt verecek.\n\n" + HELP)
        log.warning("Sahip yokken gelen mesaj, kullanıcı %s", user.get("id"))
        return send(chat["id"], "Bu bot özeldir. Kurulum kodunu göndermek için: /baslat <kod>")
    if user.get("id") != owner or chat.get("type") != "private":
        log.warning("Yetkisiz kullanıcıdan mesaj yok sayıldı: %s", user.get("id"))
        return None

    if not text:
        return send(chat["id"], "Yalnızca metin mesajları kaydedilebiliyor.")
    cmd = text.split(None, 1)[0].split("@")[0].lower()
    if cmd in ("/start", "/yardim", "/help", "/baslat"):
        reply = HELP
    elif cmd == "/sablon":
        reply = sms.FORM_TEMPLATE
    elif cmd == "/kesinti":
        reply = cmd_kesinti(con, text, received_at)
    elif cmd == "/durum":
        reply = cmd_durum(con)
    elif cmd == "/aktif":
        reply = cmd_aktif(con)
    elif cmd == "/kutu":
        reply = cmd_kutu(con)
    elif cmd.startswith("/"):
        reply = "Bilinmeyen komut.\n\n" + HELP
    else:
        reply = plain_message(con, msg, text, received_at)
    con.commit()
    log.info("Yanıt (%s): %r", cmd if cmd.startswith("/") else "düz mesaj", (reply or "")[:120])
    if reply:
        send(chat["id"], reply)


def run():
    if not TOKEN:
        log.error("MUSKI_TG_TOKEN tanımlı değil; bot çalışmıyor.")
        return 1
    con = db.connect()
    try:
        api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except Exception:  # noqa: BLE001
        log.exception("Komut menüsü ayarlanamadı")
    me = api("getMe")
    log.info("Bot başladı: @%s, sahip=%s", me.get("username"), owner_id(con))
    backoff = 5
    while True:
        try:
            offset = int(get_setting(con, "tg_offset") or 0)
            # long polling: Telegram 50 sn bekletir, HTTP zaman aşımı bundan uzun olmalı
            updates = api("getUpdates", http_timeout=70, offset=offset, limit=50, timeout=50,
                          allowed_updates=["message"])
            for upd in updates:
                try:
                    if "message" in upd:
                        handle(con, upd["message"])
                except sqlite3.OperationalError:
                    # geçici (ör. "database is locked"): offset ilerletilmez, mesaj bir sonraki turda tekrar alınır
                    con.rollback()
                    log.exception("Veritabanı geçici hatası, mesaj tekrar denenecek: %s", upd.get("update_id"))
                    raise
                except Exception:  # noqa: BLE001
                    con.rollback()
                    log.exception("Mesaj işlenemedi: %s", upd.get("update_id"))
                    try:
                        m = upd.get("message") or {}
                        if m.get("from", {}).get("id") == owner_id(con):
                            send(m["chat"]["id"], "⚠️ Bu mesaj işlenemedi. Web'deki Gelen Kutusu'na elle yapıştırabilirsin.")
                    except Exception:  # noqa: BLE001
                        log.exception("Hata bildirimi gönderilemedi")
                set_setting(con, "tg_offset", upd["update_id"] + 1)
                con.commit()
            backoff = 5
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("Telegram bağlantı hatası: %s (%ss sonra tekrar)", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        except Exception:  # noqa: BLE001
            log.exception("Beklenmeyen hata (%ss sonra tekrar)", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def main():
    logsetup.setup("telegram")
    sys.exit(run())


if __name__ == "__main__":
    main()
