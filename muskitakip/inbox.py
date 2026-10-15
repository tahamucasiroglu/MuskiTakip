"""Gelen kutusu: Telegram'dan 'düz' gönderilen ham mesajlar. Ayıklanana kadar kesinti kaydı oluşturmaz.

Kullanım (Pi'de, elle ayıklamak için):
    python -m muskitakip.inbox liste
    python -m muskitakip.inbox otomatik [ID ...]     # ID verilmezse bekleyen tümü
    python -m muskitakip.inbox yoksay ID [not]
"""
import json
import sys

from . import db, sms


def save(con, text, kanal="telegram", received_at=None, chat_id=None, message_id=None) -> tuple[int, bool]:
    """Ham mesajı kaydeder. Dönüş: (id, yeni_mi)."""
    if chat_id is not None and message_id is not None:
        row = con.execute("SELECT id FROM inbox WHERE tg_chat_id=? AND tg_message_id=?", (chat_id, message_id)).fetchone()
        if row:
            return row["id"], False
    cur = con.execute("INSERT INTO inbox(text, kanal, received_at, tg_chat_id, tg_message_id) VALUES (?,?,?,?,?)",
                      (text, kanal, received_at or db.now_iso(), chat_id, message_id))
    return cur.lastrowid, True


def preview(text: str) -> dict:
    """Mesajın otomatik ayrıştırılıp ayrıştırılamayacağını, kayda yazmadan söyler."""
    parts = sms.split_messages(text) or [text]
    out = []
    for p in parts:
        if not sms.is_outage(p):
            out.append({"text": p, "kesinti": False, "parsed": None})
        else:
            out.append({"text": p, "kesinti": True, "parsed": sms.parse_message(p)})
    return {"parcalar": out,
            "otomatik_eklenebilir": any(x["parsed"] for x in out),
            "hepsi_anlasildi": all(x["parsed"] or not x["kesinti"] for x in out)}


def auto_process(con, inbox_id: int) -> dict:
    row = con.execute("SELECT * FROM inbox WHERE id=?", (inbox_id,)).fetchone()
    if not row:
        return {"ok": False, "hata": "kayıt yok"}
    texts = sms.split_messages(row["text"]) or [row["text"]]
    stats = sms.import_messages(con, texts, kanal=f"gelen_kutusu#{inbox_id}", received_at=row["received_at"])
    ids = stats["outage_ids"]
    if ids:
        _close(con, inbox_id, "eklendi", ids,
               None if not stats["anlasilamadi"] else f"{stats['anlasilamadi']} parça anlaşılamadı")
    elif stats["kesinti_disi"] == len(texts):
        _close(con, inbox_id, "kesinti_degil", [], "otomatik: kesinti mesajı değil")
    return {"ok": bool(ids), **{k: v for k, v in stats.items() if k != "anlasilamayanlar"}}


def link_manual(con, inbox_id: int, outage_id: int):
    _close(con, inbox_id, "eklendi", [outage_id], "elle eklendi")


def ignore(con, inbox_id: int, note=None):
    _close(con, inbox_id, "kesinti_degil", [], note)


def reopen(con, inbox_id: int):
    con.execute("UPDATE inbox SET durum='bekliyor', processed_at=NULL WHERE id=?", (inbox_id,))


def _close(con, inbox_id, durum, ids, note):
    con.execute("UPDATE inbox SET durum=?, outage_ids=?, note=?, processed_at=? WHERE id=?",
                (durum, ",".join(str(i) for i in ids) or None, note, db.now_iso(), inbox_id))


def pending_count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM inbox WHERE durum='bekliyor'").fetchone()[0]


def main():
    con = db.connect()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "liste"
    if cmd == "liste":
        for r in con.execute("SELECT id, received_at, durum, substr(replace(text, char(10), ' '), 1, 110) t "
                             "FROM inbox ORDER BY id DESC LIMIT 100"):
            print(f"#{r['id']} {r['received_at']} [{r['durum']}] {r['t']}")
    elif cmd == "otomatik":
        ids = [int(x) for x in sys.argv[2:]] or [r[0] for r in con.execute(
            "SELECT id FROM inbox WHERE durum='bekliyor' ORDER BY id")]
        for i in ids:
            print(i, json.dumps(auto_process(con, i), ensure_ascii=False))
        con.commit()
    elif cmd == "yoksay":
        ignore(con, int(sys.argv[2]), " ".join(sys.argv[3:]) or None)
        con.commit()
    else:
        print(__doc__)
    con.close()


if __name__ == "__main__":
    main()
