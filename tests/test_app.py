# -*- coding: utf-8 -*-
"""端到端测试：上传、修订规则、配对、跨日发布、过期与重发布、原子性、重启持久化。"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transit_od import create_app  # noqa: E402
from transit_od.db import connect, get_esv  # noqa: E402

HEADER = "card_id,event_id,revision,ts_utc,station,direction\n"

# 基础批次：覆盖跨午夜行程、孤立出站、悬空进站、超 4 小时窗口
BASE = HEADER + """\
C001,E1,1,2026-11-15T22:30:00Z,Alpha,in
C001,E2,1,2026-11-16T00:10:00Z,Beta,out
C002,E3,1,2026-11-16T08:00:00Z,Alpha,in
C002,E4,1,2026-11-16T09:00:00Z,Gamma,out
C003,E5,1,2026-11-16T10:00:00Z,Delta,out
C004,E6,1,2026-11-16T11:00:00Z,Alpha,in
C004,E7,1,2026-11-16T12:30:00Z,Beta,in
C005,E8,1,2026-11-16T13:00:00Z,Alpha,in
C005,E9,1,2026-11-16T18:30:00Z,Beta,out
"""


@pytest.fixture()
def app(tmp_path):
    return create_app(str(tmp_path / "test.db"))


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, text, filename="b.csv"):
    return client.post(
        "/upload",
        data={"file": (io.BytesIO(text.encode("utf-8")), filename)},
        content_type="multipart/form-data",
    )


def q(app, sql, args=()):
    conn = connect(app.config["DB_PATH"])
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def q1(app, sql, args=()):
    rows = q(app, sql, args)
    return rows[0] if rows else None


def publish(client, day):
    return client.post(f"/days/{day}/publish")


# ---------- 上传与配对 ----------

def test_upload_pairing_and_cross_midnight_publish(app, client):
    upload(client, BASE)
    assert q1(app, "SELECT COUNT(*) n FROM events")["n"] == 9
    assert q1(app, "SELECT status FROM batches WHERE batch_id=1")["status"] == "committed"

    publish(client, "2026-11-15")
    publish(client, "2026-11-16")

    # 跨午夜行程归进站日 2026-11-15
    pub15 = q1(app, "SELECT * FROM publications WHERE day='2026-11-15'")
    assert pub15["trip_count"] == 1 and pub15["anomaly_count"] == 0
    trip = q1(app, "SELECT * FROM publication_trips WHERE pub_id=?", (pub15["pub_id"],))
    assert trip["card_id"] == "C001"
    assert trip["entry_station"] == "Alpha" and trip["exit_station"] == "Beta"
    assert trip["entry_ts"] == "2026-11-15T22:30:00Z"
    assert trip["exit_ts"] == "2026-11-16T00:10:00Z"
    assert trip["duration_min"] == 100.0

    # 2026-11-16：1 行程（C002），5 异常
    pub16 = q1(app, "SELECT * FROM publications WHERE day='2026-11-16'")
    assert pub16["trip_count"] == 1 and pub16["anomaly_count"] == 5
    anoms = q(app, "SELECT event_id, reason FROM publication_anomalies WHERE pub_id=? ORDER BY event_id",
              (pub16["pub_id"],))
    got = {a["event_id"]: a["reason"] for a in anoms}
    assert got == {
        "E5": "orphan_exit",      # 孤立出站，归自身日期
        "E6": "dangling_entry",   # 被再次进站 E7 悬空
        "E7": "dangling_entry",   # 末尾未配对进站
        "E8": "dangling_entry",   # 出站超出 4 小时窗口
        "E9": "orphan_exit",
    }
    # 跨午夜行程不应出现在出站日
    trips16 = q(app, "SELECT * FROM publication_trips WHERE pub_id=?", (pub16["pub_id"],))
    assert all(t["entry_event_id"] != "E1" for t in trips16)


def test_exactly_four_hours_matches(app, client):
    upload(client, HEADER + "C1,A1,1,2026-11-16T08:00:00Z,S1,in\nC1,A2,1,2026-11-16T12:00:00Z,S2,out\n")
    publish(client, "2026-11-16")
    pub = q1(app, "SELECT * FROM publications WHERE day='2026-11-16'")
    assert pub["trip_count"] == 1 and pub["anomaly_count"] == 0


# ---------- 修订规则 ----------

def test_same_revision_same_content_retransmit_keeps_old(app, client):
    upload(client, BASE)
    esv_before = get_esv(connect(app.config["DB_PATH"]))
    upload(client, BASE)  # 原样重传
    b = q1(app, "SELECT * FROM batches WHERE batch_id=2")
    assert b["status"] == "committed"
    assert b["rows_unchanged"] == 9 and b["rows_new"] == 0 and b["rows_updated"] == 0
    assert q1(app, "SELECT COUNT(*) n FROM events")["n"] == 9
    # 无实际变更 → 事件集版本不前进，发布不因纯重传而过期
    assert get_esv(connect(app.config["DB_PATH"])) == esv_before


def test_higher_revision_replaces_and_lower_ignored(app, client):
    upload(client, BASE)
    upload(client, HEADER + "C002,E4,2,2026-11-16T09:00:00Z,Delta,out\n")
    ev = q1(app, "SELECT * FROM events WHERE event_id='E4'")
    assert ev["revision"] == 2 and ev["station"] == "Delta"
    # 更低修订迟到 → 忽略
    upload(client, HEADER + "C002,E4,1,2026-11-16T09:00:00Z,Omega,out\n")
    ev = q1(app, "SELECT * FROM events WHERE event_id='E4'")
    assert ev["revision"] == 2 and ev["station"] == "Delta"
    b = q1(app, "SELECT * FROM batches ORDER BY batch_id DESC LIMIT 1")
    assert b["rows_ignored"] == 1


def test_same_revision_different_content_conflicts_and_fails(app, client):
    upload(client, BASE)
    upload(client, HEADER + "C002,E4,1,2026-11-16T09:00:00Z,Zeta,out\nC009,E30,1,2026-11-17T01:00:00Z,S,in\n")
    b = q1(app, "SELECT * FROM batches ORDER BY batch_id DESC LIMIT 1")
    assert b["status"] == "failed" and "异文冲突" in b["error"]
    # 原子性：整批回滚，E30 未写入，E4 保持旧值
    assert q1(app, "SELECT COUNT(*) n FROM events")["n"] == 9
    assert q1(app, "SELECT station FROM events WHERE event_id='E4'")["station"] == "Gamma"


def test_same_card_same_instant_fails_batch(app, client):
    # 批内同卡同刻不同事件
    upload(client, HEADER + "C009,E20,1,2026-11-16T10:00:00Z,Alpha,in\nC009,E21,1,2026-11-16T10:00:00Z,Beta,out\n")
    b = q1(app, "SELECT * FROM batches ORDER BY batch_id DESC LIMIT 1")
    assert b["status"] == "failed" and "同卡同刻" in b["error"]
    assert q1(app, "SELECT COUNT(*) n FROM events")["n"] == 0
    # 与已提交事件集同卡同刻
    upload(client, BASE)
    upload(client, HEADER + "C002,E99,1,2026-11-16T08:00:00Z,Alpha,in\n")
    b = q1(app, "SELECT * FROM batches ORDER BY batch_id DESC LIMIT 1")
    assert b["status"] == "failed"
    assert q1(app, "SELECT COUNT(*) n FROM events")["n"] == 9


def test_invalid_rows_fail_batch(app, client):
    upload(client, HEADER + "C1,E1,0,2026-11-16T08:00:00Z,S,in\n")  # 修订非正整数
    b = q1(app, "SELECT * FROM batches ORDER BY batch_id DESC LIMIT 1")
    assert b["status"] == "failed"
    assert q1(app, "SELECT COUNT(*) n FROM events")["n"] == 0


# ---------- 发布、过期与重发布 ----------

def test_correction_marks_stale_and_republish_keeps_old_readable(app, client):
    upload(client, BASE)
    publish(client, "2026-11-16")
    pub1 = q1(app, "SELECT * FROM publications WHERE day='2026-11-16'")
    assert "当前有效" in client.get("/days/2026-11-16").get_data(as_text=True)

    # 更正：E4 出站站点 Gamma → Delta（更高修订）
    upload(client, HEADER + "C002,E4,2,2026-11-16T09:00:00Z,Delta,out\n")
    page = client.get("/days/2026-11-16").get_data(as_text=True)
    assert "已过期" in page

    # 重新发布：新发布生效，旧发布保留可读且标过期
    publish(client, "2026-11-16")
    pub2 = q1(app, "SELECT * FROM publications WHERE day='2026-11-16' ORDER BY pub_id DESC")
    assert pub2["pub_id"] > pub1["pub_id"]
    assert q1(app, "SELECT status FROM publications WHERE pub_id=?", (pub1["pub_id"],))["status"] == "superseded"
    new_trip = q1(app, "SELECT * FROM publication_trips WHERE pub_id=?", (pub2["pub_id"],))
    assert new_trip["exit_station"] == "Delta"

    old_page = client.get(f"/publications/{pub1['pub_id']}").get_data(as_text=True)
    assert "Gamma" in old_page and "已过期" in old_page  # 旧发布保持可读并标过期
    new_page = client.get(f"/publications/{pub2['pub_id']}").get_data(as_text=True)
    assert "Delta" in new_page and "当前有效" in new_page


def test_correction_moving_event_across_days_stales_both_days(app, client):
    upload(client, BASE)
    publish(client, "2026-11-15")
    publish(client, "2026-11-16")
    # E1 进站从 11-15 22:30 改到 11-16 00:00 → 行程从 15 日移到 16 日
    upload(client, HEADER + "C001,E1,2,2026-11-16T00:00:00Z,Alpha,in\n")
    for day in ("2026-11-15", "2026-11-16"):
        assert "已过期" in client.get(f"/days/{day}").get_data(as_text=True)
    publish(client, "2026-11-15")
    publish(client, "2026-11-16")
    pub15 = q1(app, "SELECT * FROM publications WHERE day='2026-11-15' ORDER BY pub_id DESC")
    pub16 = q1(app, "SELECT * FROM publications WHERE day='2026-11-16' ORDER BY pub_id DESC")
    assert pub15["trip_count"] == 0
    assert pub16["trip_count"] == 2  # C001 与 C002 的行程都归 16 日


def test_failed_batch_does_not_stale_publication(app, client):
    upload(client, BASE)
    publish(client, "2026-11-16")
    # 失败批次（异文冲突）不应推进任何版本
    upload(client, HEADER + "C002,E4,1,2026-11-16T09:00:00Z,Zeta,out\n")
    assert "当前有效" in client.get("/days/2026-11-16").get_data(as_text=True)


def test_publish_is_consistent_snapshot(app, client):
    upload(client, BASE)
    publish(client, "2026-11-16")
    publish(client, "2026-11-16")  # 无变化再发布
    pubs = q(app, "SELECT * FROM publications WHERE day='2026-11-16' ORDER BY pub_id")
    assert len(pubs) == 2
    assert pubs[0]["status"] == "superseded" and pubs[1]["status"] == "current"
    assert pubs[0]["event_set_version"] == pubs[1]["event_set_version"]
    assert pubs[0]["trip_count"] == pubs[1]["trip_count"]


# ---------- 页面与持久化 ----------

def test_pages_render(app, client):
    upload(client, BASE)
    publish(client, "2026-11-16")
    for path in ("/days", "/days/2026-11-16", "/events", "/batches", "/batches/1", "/upload"):
        resp = client.get(path)
        assert resp.status_code == 200, path


def test_persistence_across_restart(app, client, tmp_path):
    upload(client, BASE)
    publish(client, "2026-11-16")
    db_path = app.config["DB_PATH"]
    # 模拟重启：同一数据库文件重新建应用
    app2 = create_app(db_path)
    client2 = app2.test_client()
    page = client2.get("/days/2026-11-16").get_data(as_text=True)
    assert "行程（1）" in page and "异常（5）" in page
    assert q1(app2, "SELECT COUNT(*) n FROM events")["n"] == 9
