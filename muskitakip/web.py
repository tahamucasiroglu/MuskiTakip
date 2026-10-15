"""Kayıtları görüntüleme, filtreleme ve dışa aktarma arayüzü (Flask).

Kullanım:  python -m muskitakip.web   (üretimde waitress ile servis edilir)
"""
import gzip
import math
from datetime import datetime, timedelta
from urllib.parse import urlencode

import os

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, url_for

from . import backup, config, exports, inbox, queries, sms
from .parser import parse_iso

app = Flask(__name__)
app.secret_key = os.environ.get("MUSKI_WEB_SECRET") or os.urandom(24)
PAGE_SIZE = 200


@app.template_filter("dt")
def _dt(value, seconds=False):
    return queries.fmt_dt(value, seconds)


@app.template_filter("saat")
def _saat(value):
    return queries.fmt_hours(value)


@app.template_filter("sure")
def _sure(row):
    a, b = parse_iso(row["started_at"]), parse_iso(row["finished_at"])
    return f"{int((b - a).total_seconds())} sn" if a and b else ""


@app.template_filter("ago")
def _ago(value):
    dt = parse_iso(value) if isinstance(value, str) else value
    if not dt:
        return ""
    sec = int((datetime.now(config.TZ) - dt).total_seconds())
    if sec < 60:
        return "az önce"
    if sec < 3600:
        return f"{sec // 60} dk önce"
    if sec < 86400:
        return f"{sec // 3600} sa {sec % 3600 // 60} dk önce"
    return f"{sec // 86400} gün önce"


def status_info(con):
    last = con.execute("SELECT * FROM scrapes ORDER BY id DESC LIMIT 1").fetchone()
    last_ok = con.execute("SELECT * FROM scrapes WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
    counts = con.execute(
        "SELECT COUNT(*) toplam, SUM(durum='devam') devam, SUM(durum='tamamlandi') tamamlandi, "
        "SUM(durum='kalkti') kalkti, SUM(durum='ilan_bitti') ilan_bitti, SUM(gone_at IS NULL AND source='web') sitede FROM outages").fetchone()
    nscr = con.execute("SELECT COUNT(*) n, SUM(ok=0) hata FROM scrapes").fetchone()
    stale = True
    if last_ok:
        age = datetime.now(config.TZ) - parse_iso(last_ok["finished_at"] or last_ok["started_at"])
        stale = age > timedelta(minutes=config.SCRAPE_INTERVAL_MIN * 2 + 5)
    return {"last": last, "last_ok": last_ok, "counts": counts, "nscr": nscr, "stale": stale,
            "inbox_pending": inbox.pending_count(con),
            "interval": config.SCRAPE_INTERVAL_MIN}


@app.context_processor
def inject():
    def qs(**over):
        args = {k: v for k, v in request.args.items() if v}
        for k, v in over.items():
            if v in (None, ""):
                args.pop(k, None)
            else:
                args[k] = v
        return urlencode(args)
    return {"qs": qs, "DURUM_FILTERS": queries.DURUM_FILTERS, "KAYNAK_LABELS": queries.KAYNAK_LABELS,
            "today": datetime.now(config.TZ).strftime("%Y-%m-%d")}


@app.route("/")
def index():
    con = queries.connect()
    try:
        f = queries.get_filters(request.args)
        rows = queries.search(con, f)
        total = len(rows)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(1, request.args.get("sayfa", 1, type=int)), pages)
        shown = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        return render_template("index.html", rows=shown, total=total, page=page, pages=pages, f=f,
                               filter_text=queries.filter_summary(f), ilceler=queries.ilce_list(con),
                               mahalleler=queries.mahalle_list(con), st=status_info(con))
    finally:
        con.close()


@app.route("/kesinti/<int:oid>")
def detail(oid):
    con = queries.connect()
    try:
        row = con.execute("SELECT * FROM outages WHERE id=?", (oid,)).fetchone()
        if not row:
            abort(404)
        o = queries.enrich(dict(row))
        nbs = queries.neighborhoods_for(con, [oid]).get(oid, [])
        events = con.execute("SELECT * FROM outage_events WHERE outage_id=? ORDER BY id", (oid,)).fetchall()
        msgs = con.execute("SELECT * FROM messages WHERE outage_id=? ORDER BY id", (oid,)).fetchall()
        return render_template("detail.html", o=o, nbs=nbs, events=events, msgs=msgs, st=status_info(con))
    finally:
        con.close()


@app.route("/taramalar")
def scrapes():
    con = queries.connect()
    try:
        only_err = request.args.get("hata") == "1"
        rows = con.execute(
            "SELECT s.*, (SELECT GROUP_CONCAT(id || ':' || kind) FROM raw_snapshots r WHERE r.scrape_id=s.id) snaps "
            f"FROM scrapes s {'WHERE ok=0' if only_err else ''} ORDER BY id DESC LIMIT 500").fetchall()
        return render_template("scrapes.html", rows=rows, only_err=only_err, st=status_info(con))
    finally:
        con.close()


@app.route("/ham/<int:sid>")
def raw_snapshot(sid):
    con = queries.connect()
    try:
        r = con.execute("SELECT * FROM raw_snapshots WHERE id=?", (sid,)).fetchone()
        if not r:
            abort(404)
        body = gzip.decompress(r["content_gz"]).decode("utf-8")
        mime = "application/json" if r["kind"] == "api" else "text/plain"
        header = "" if r["kind"] == "api" else (
            f"<!-- {r['url']} | alınma: {r['fetched_at']} | sha256: {r['sha256']} -->\n")
        return Response(header + body, mimetype=mime + "; charset=utf-8")
    finally:
        con.close()


@app.route("/istatistik")
def stats():
    con = queries.connect()
    try:
        f = queries.get_filters(request.args)
        where, params = queries.build_where(f)
        by_ilce = con.execute(
            f"SELECT o.ilce, COUNT(*) n, SUM(o.durum='devam') devam, ROUND(SUM(o.tahmini_sure_saat),1) tahmini_toplam "
            f"FROM outages o{where} GROUP BY o.ilce ORDER BY n DESC", params).fetchall()
        by_mahalle = con.execute(
            f"SELECT o.ilce, n.mahalle, COUNT(*) adet, MAX(o.kesinti_tarihi) son FROM outages o "
            f"JOIN outage_neighborhoods n ON n.outage_id=o.id{where}"
            + (f" {'AND' if where else 'WHERE'} FOLD(n.mahalle) LIKE '%' || FOLD(?) || '%'" if f["mahalle"] else "")
            + " GROUP BY o.ilce, n.mahalle ORDER BY adet DESC, son DESC LIMIT 100",
            params + ([f["mahalle"]] if f["mahalle"] else [])).fetchall()
        by_month = con.execute(
            f"SELECT substr(o.kesinti_tarihi,1,7) ay, COUNT(*) n, ROUND(SUM(o.tahmini_sure_saat),1) saat "
            f"FROM outages o{where} GROUP BY ay ORDER BY ay DESC", params).fetchall()
        by_day = con.execute(
            f"SELECT substr(o.kesinti_tarihi,1,10) gun, COUNT(*) n FROM outages o{where} "
            f"GROUP BY gun ORDER BY gun DESC LIMIT 60", params).fetchall()
        return render_template("stats.html", by_ilce=by_ilce, by_mahalle=by_mahalle, by_month=by_month,
                               by_day=by_day, f=f, filter_text=queries.filter_summary(f),
                               ilceler=queries.ilce_list(con), mahalleler=queries.mahalle_list(con),
                               st=status_info(con))
    finally:
        con.close()


EXPORTS = {
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "pdf": ("application/pdf", "pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "csv": ("text/csv; charset=utf-8", "csv"),
    "json": ("application/json; charset=utf-8", "json"),
}


@app.route("/disari/<fmt>")
def export(fmt):
    if fmt not in EXPORTS:
        abort(404)
    con = queries.connect()
    try:
        f = queries.get_filters(request.args)
        rows = queries.search(con, f)
        nbs = queries.neighborhoods_for(con, [r["id"] for r in rows]) if fmt in ("xlsx", "json") else {}
    finally:
        con.close()
    if fmt == "xlsx":
        data = exports.to_xlsx(rows, nbs, f)
    elif fmt == "pdf":
        data = exports.to_pdf(rows, f)
    elif fmt == "docx":
        data = exports.to_docx(rows, f)
    elif fmt == "csv":
        data = exports.to_csv(rows)
    else:
        data = exports.to_json(rows, nbs, f)
    mime, ext = EXPORTS[fmt]
    return Response(data, mimetype=mime, headers={
        "Content-Disposition": f'attachment; filename="{exports.filename(ext)}"'})


@app.route("/yedek")
def backups():
    con = queries.connect()
    try:
        st = status_info(con)
    finally:
        con.close()
    files = [{"name": p.name, "size": p.stat().st_size,
              "mtime": datetime.fromtimestamp(p.stat().st_mtime, config.TZ)} for p in backup.list_backups()]
    db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    return render_template("backups.html", files=files, db_size=db_size, db_path=config.DB_PATH,
                           backup_dir=config.BACKUP_DIR, keep=config.BACKUP_KEEP, st=st)


@app.route("/yedek/indir")
def backup_download():
    tmp = backup.snapshot_tempfile()
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    name = f"muski-{datetime.now(config.TZ):%Y%m%d-%H%M%S}.db"
    return Response(data, mimetype="application/vnd.sqlite3",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.route("/yedek/dosya/<name>")
def backup_file(name):
    for p in backup.list_backups():
        if p.name == name:
            return send_file(p, as_attachment=True, download_name=p.name)
    abort(404)


INBOX_TABS = {"bekliyor": "Bekleyen", "eklendi": "Eklenen", "kesinti_degil": "Kesinti değil", "tumu": "Tümü"}


@app.route("/gelen-kutusu", methods=["GET", "POST"])
def inbox_page():
    con = queries.connect()
    try:
        if request.method == "POST":  # web'den ham mesaj yapıştırma
            text = (request.form.get("text") or "").strip()
            if text:
                iid, _ = inbox.save(con, text, kanal="web")
                con.commit()
                flash(f"Gelen kutusuna kaydedildi (#{iid}).")
            return redirect(url_for("inbox_page"))
        tab = request.args.get("durum", "bekliyor")
        tab = tab if tab in INBOX_TABS else "bekliyor"
        sql = "SELECT * FROM inbox" + ("" if tab == "tumu" else " WHERE durum=?") + " ORDER BY id DESC LIMIT 300"
        rows = con.execute(sql, () if tab == "tumu" else (tab,)).fetchall()
        items = [{"row": r, "pv": inbox.preview(r["text"]),
                  "ids": [int(x) for x in (r["outage_ids"] or "").split(",") if x]} for r in rows]
        counts = {r[0]: r[1] for r in con.execute("SELECT durum, COUNT(*) FROM inbox GROUP BY durum")}
        return render_template("inbox.html", items=items, tab=tab, tabs=INBOX_TABS, counts=counts,
                               st=status_info(con))
    finally:
        con.close()


@app.post("/gelen-kutusu/<int:iid>/<action>")
def inbox_action(iid, action):
    con = queries.connect()
    try:
        if action == "otomatik":
            res = inbox.auto_process(con, iid)
            if res.get("ok"):
                flash(f"#{iid} eklendi → kesinti {', '.join('#' + str(i) for i in res['outage_ids'])} "
                      f"(yeni {res['yeni_kesinti']}, mevcut kesintiye bağlanan {res['mevcut_kesintiye_eklendi']}, "
                      f"mükerrer {res['mukerrer']})")
            else:
                flash(f"#{iid} otomatik eklenemedi, 'Elle ekle' ile girebilirsin.")
        elif action == "yoksay":
            inbox.ignore(con, iid, request.form.get("note") or None)
            flash(f"#{iid} 'kesinti değil' olarak işaretlendi.")
        elif action == "geri-al":
            inbox.reopen(con, iid)
            flash(f"#{iid} tekrar bekleyenlere alındı (eklenen kesinti kayıtları silinmez).")
        else:
            abort(404)
        con.commit()
    finally:
        con.close()
    return redirect(request.referrer or url_for("inbox_page"))


MANUAL_FIELDS = ("tarih", "baslangic", "bitis", "ilce", "yer", "mahalleler", "sebep", "tur", "not")


@app.route("/mesaj/yeni", methods=["GET", "POST"])
def manual_entry():
    con = queries.connect()
    try:
        gelen = request.values.get("gelen", type=int)
        src = con.execute("SELECT * FROM inbox WHERE id=?", (gelen,)).fetchone() if gelen else None
        if request.method == "POST":
            f = {k: (request.form.get(k) or "").strip() for k in MANUAL_FIELDS}
            form = {k: v for k, v in f.items() if v}
            if form.get("tarih") and "-" in form["tarih"]:  # <input type=date> -> GG.AA.YYYY
                y, m, d = form["tarih"].split("-")
                form["tarih"] = f"{d}.{m}.{y}"
            res = sms.add_structured(con, form, kanal=f"gelen_kutusu#{gelen}" if src else "web_form",
                                     received_at=src["received_at"] if src else None,
                                     raw_text=src["text"] if src else None)
            if res["durum"] == "hata":
                flash("Kaydedilmedi: " + "; ".join(res["hatalar"]))
                return render_template("manual.html", f=f, src=src, st=status_info(con))
            if src and res["id"]:
                inbox.link_manual(con, src["id"], res["id"])
            con.commit()
            if res["durum"] == "mukerrer":
                flash("Bu kesinti zaten kayıtlı, tekrar eklenmedi.")
                return redirect(url_for("detail", oid=res["id"]) if res["id"] else url_for("inbox_page"))
            flash(f"Kesinti #{res['id']} kaydedildi.")
            return redirect(url_for("detail", oid=res["id"]))
        f = {}
        if src:  # gelen kutusundaki mesajdan ön doldurma
            pv = inbox.preview(src["text"])
            m = next((x["parsed"] for x in pv["parcalar"] if x["parsed"]), None)
            if m:
                f = {"tarih": m["tarih"], "bitis": f"{m['bitis']:%H:%M}", "yer": m["ariza_yeri"] or "",
                     "mahalleler": ", ".join(m["etkilenen"]), "sebep": m["sebep"], "tur": m["tur"].lower(),
                     "baslangic": f"{m['baslangic']:%H:%M}" if m["baslangic"] else ""}
            else:
                f = sms.parse_form(src["text"])
                d = sms._form_date(f.get("tarih", ""))
                if d:
                    f["tarih"] = d.date().isoformat()
        return render_template("manual.html", f=f, src=src, st=status_info(con))
    finally:
        con.close()


@app.route("/saglik")
def health():
    con = queries.connect()
    try:
        st = status_info(con)
    finally:
        con.close()
    return {
        "ok": not st["stale"],
        "son_tarama": dict(st["last"]) if st["last"] else None,
        "son_basarili_tarama": st["last_ok"]["finished_at"] if st["last_ok"] else None,
        "kayit": dict(st["counts"]),
    }


def main():
    from waitress import serve
    print(f"MUSKİ Takip arayüzü: http://{config.WEB_HOST}:{config.WEB_PORT}", flush=True)
    serve(app, host=config.WEB_HOST, port=config.WEB_PORT, threads=4)


if __name__ == "__main__":
    main()
