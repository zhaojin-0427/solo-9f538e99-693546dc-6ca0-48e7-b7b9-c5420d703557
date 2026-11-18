"""页面路由：上传、闸机终号登记、按 UTC 日发布/查看、批次与事件浏览。"""
import json
import re
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from .db import connect, get_day_version, now_iso
from .gates import (STATUS_LABELS, acceptance_all, acceptance_for_day,
                    begin_read, normalize_snapshot)
from .ingest import commit_batch, record_failed_batch
from .publish import GateAcceptanceError, publish_day

bp = Blueprint("main", __name__)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEQ_RE = re.compile(r"^[0-9]+$")


def _valid_day(s):
    if not DAY_RE.match(s or ""):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return False
    return True

REASONS = {
    "dangling_entry": "进站悬空（无匹配出站）",
    "orphan_exit": "孤立出站（4 小时内无匹配进站）",
}


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def _pub_state(pub, day_version, acceptance=None):
    """发布状态：当前有效 / 已过期。

    过期 = 已被取代、底层日数据版本已前进，或发布时的闸机验收快照与当前已提交
    状态不一致（例如终号登记后出现缺号）。过期发布保持可读，但不再显示为可供数。
    """
    if pub["status"] != "current":
        return "已过期"
    if pub["day_version"] < day_version:
        return "已过期"
    if acceptance is not None:
        stored = normalize_snapshot(pub["gate_acceptance"] if "gate_acceptance" in pub.keys() else None)
        if stored != acceptance["snapshot"]:
            return "已过期"
    return "当前有效"


def _pub_rows(conn, pub_id):
    trips = conn.execute(
        "SELECT * FROM publication_trips WHERE pub_id=? ORDER BY entry_ts, card_id", (pub_id,)
    ).fetchall()
    anomalies = conn.execute(
        "SELECT * FROM publication_anomalies WHERE pub_id=? ORDER BY ts_utc, card_id", (pub_id,)
    ).fetchall()
    return trips, anomalies


def _parse_acceptance_snapshot(pub):
    """发布时保存的闸机验收快照 → dict；无法解析时为空快照。"""
    raw = pub["gate_acceptance"] if "gate_acceptance" in pub.keys() else None
    try:
        return json.loads(raw) if raw else {"status": "none", "entries": []}
    except (TypeError, ValueError):
        return {"status": "none", "entries": []}


@bp.route("/")
def index():
    return redirect(url_for("main.days"))


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        f = request.files.get("file")
        if f is None or not f.filename:
            flash("请选择要上传的 CSV 文件", "err")
            return redirect(url_for("main.upload"))
        raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            msg = "文件不是有效的 UTF-8 编码"
            bid = record_failed_batch(get_db(), f.filename, now_iso(), msg)
            flash(f"上传失败：{msg}", "err")
            return redirect(url_for("main.batch_detail", batch_id=bid))
        batch_id, ok, err = commit_batch(get_db(), f.filename, text)
        if ok:
            flash(f"批次 #{batch_id} 已提交", "ok")
        else:
            flash(f"批次 #{batch_id} 失败：{err[:300]}", "err")
        return redirect(url_for("main.batch_detail", batch_id=batch_id))
    return render_template("upload.html")


@bp.route("/batches")
def batches():
    rows = get_db().execute("SELECT * FROM batches ORDER BY batch_id DESC LIMIT 500").fetchall()
    return render_template("batches.html", rows=rows)


@bp.route("/batches/<int:batch_id>")
def batch_detail(batch_id):
    conn = get_db()
    b = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if b is None:
        abort(404)
    details = json.loads(b["details"]) if b["details"] else []
    days = [r["day"] for r in conn.execute(
        "SELECT day FROM batch_affected_days WHERE batch_id=? ORDER BY day", (batch_id,))]
    return render_template("batch_detail.html", b=b, details=details, days=days)


@bp.route("/events")
def events():
    rows = get_db().execute(
        "SELECT * FROM events ORDER BY card_id, ts_epoch_us, event_id LIMIT 2000").fetchall()
    return render_template("events.html", rows=rows)


@bp.route("/gates")
def gates():
    conn = get_db()
    begin_read(conn)
    acceptance = acceptance_all(conn)
    return render_template("gates.html", rows=acceptance)


@bp.route("/gates/register", methods=["POST"])
def gate_register():
    gate_id = (request.form.get("gate_id") or "").strip()
    day = (request.form.get("day") or "").strip()
    end_seq_raw = (request.form.get("end_seq") or "").strip()
    if not gate_id:
        flash("请填写闸机 ID", "err")
        return redirect(url_for("main.gates"))
    if not _valid_day(day):
        flash("日期须为有效的 YYYY-MM-DD（UTC 日）", "err")
        return redirect(url_for("main.gates"))
    if not SEQ_RE.match(end_seq_raw) or int(end_seq_raw) < 1:
        flash("终号须为从 1 编起的正整数", "err")
        return redirect(url_for("main.gates", day=day))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO gate_day_registrations(gate_id, day, end_seq, registered_at) VALUES(?,?,?,?)"
            " ON CONFLICT(gate_id, day) DO UPDATE SET end_seq=excluded.end_seq,"
            " registered_at=excluded.registered_at",
            (gate_id, day, int(end_seq_raw), now_iso()),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    acc = acceptance_for_day(conn, day)
    if acc["blocking"]:
        flash(f"已登记 {gate_id} @ {day} 终号 {end_seq_raw}：该日存在缺号，禁止发布。", "err")
    else:
        flash(f"已登记 {gate_id} @ {day} 终号 {end_seq_raw}：验收{acc['status_label']}。", "ok")
    return redirect(url_for("main.gates", day=day))


@bp.route("/days")
def days():
    conn = get_db()
    begin_read(conn)
    ev_counts = {r["day"]: r["n"] for r in conn.execute(
        "SELECT substr(ts_utc,1,10) AS day, COUNT(*) AS n FROM events GROUP BY day")}
    latest = {}
    for r in conn.execute(
        "SELECT p.* FROM publications p"
        " JOIN (SELECT day, MAX(pub_id) AS m FROM publications GROUP BY day) t ON t.m = p.pub_id"
    ):
        latest[r["day"]] = r
    dvs = {r["day"]: r["version"] for r in conn.execute("SELECT day, version FROM day_versions")}
    gate_by_day = {a["day"]: a for a in acceptance_all(conn)}
    all_days = sorted(set(ev_counts) | set(latest) | set(gate_by_day), reverse=True)
    rows = []
    for d in all_days:
        pub = latest.get(d)
        acc = gate_by_day.get(d) or acceptance_for_day(conn, d)
        state = "未发布" if pub is None else _pub_state(pub, dvs.get(d, 0), acc)
        rows.append({"day": d, "events": ev_counts.get(d, 0), "pub": pub, "state": state,
                     "gate": acc})
    return render_template("days.html", rows=rows)


@bp.route("/days/<day>")
def day_detail(day):
    if not DAY_RE.match(day):
        abort(404)
    conn = get_db()
    begin_read(conn)
    pubs = conn.execute(
        "SELECT * FROM publications WHERE day=? ORDER BY pub_id DESC", (day,)).fetchall()
    dv = get_day_version(conn, day)
    acc = acceptance_for_day(conn, day)
    ev_count = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE substr(ts_utc,1,10)=?", (day,)).fetchone()["n"]
    latest = pubs[0] if pubs else None
    trips, anomalies = ([], [])
    if latest:
        trips, anomalies = _pub_rows(conn, latest["pub_id"])
    pub_list = [(p, _pub_state(p, dv, acc)) for p in pubs]
    return render_template(
        "day_detail.html", day=day, pubs=pub_list, latest=latest,
        latest_state=(_pub_state(latest, dv, acc) if latest else None),
        trips=trips, anomalies=anomalies, ev_count=ev_count, reasons=REASONS,
        gate=acc, gate_labels=STATUS_LABELS)


@bp.route("/days/<day>/publish", methods=["POST"])
def publish(day):
    if not DAY_RE.match(day):
        abort(404)
    conn = get_db()
    try:
        pub_id = publish_day(conn, day)
    except GateAcceptanceError as e:
        flash(str(e), "err")
        return redirect(url_for("main.day_detail", day=day))
    flash(f"已发布 {day}：发布号 #{pub_id}（基于同一已提交事件集快照）", "ok")
    return redirect(url_for("main.day_detail", day=day))


@bp.route("/publications/<int:pub_id>")
def publication(pub_id):
    conn = get_db()
    begin_read(conn)
    pub = conn.execute("SELECT * FROM publications WHERE pub_id=?", (pub_id,)).fetchone()
    if pub is None:
        abort(404)
    dv = get_day_version(conn, pub["day"])
    acc = acceptance_for_day(conn, pub["day"])
    trips, anomalies = _pub_rows(conn, pub_id)
    return render_template("publication.html", pub=pub, state=_pub_state(pub, dv, acc),
                           trips=trips, anomalies=anomalies, reasons=REASONS,
                           gate_snapshot=_parse_acceptance_snapshot(pub),
                           gate_labels=STATUS_LABELS)
