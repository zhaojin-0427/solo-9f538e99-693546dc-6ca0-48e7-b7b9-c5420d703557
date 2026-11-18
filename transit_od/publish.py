"""按 UTC 日发布行程与异常。

发布语义：
- 发布在单个 BEGIN IMMEDIATE 事务内完成：读取已提交事件集 → 配对 → 写入快照，
  与上传互斥（SQLite 写锁），因此每份发布都基于同一已提交事件集，不会出现半份结果。
- 跨午夜行程归进站日；孤立出站归自身日期；悬空进站归进站自身日期。
- 重新发布时旧发布置为 superseded，内容保留可读；若发布后该日数据版本前进，
  页面将其标记为"已过期"。
"""
from .db import get_day_version, get_esv, now_iso
from .ingest import day_of, datetime_from_us
from .pairing import build_results


def _load_events(conn):
    rows = conn.execute("SELECT * FROM events").fetchall()
    return [
        {
            "event_id": r["event_id"],
            "card_id": r["card_id"],
            "ts": datetime_from_us(r["ts_epoch_us"]),
            "ts_utc": r["ts_utc"],
            "station": r["station"],
            "direction": r["direction"],
        }
        for r in rows
    ]


def publish_day(conn, day):
    """发布指定 UTC 日，返回 pub_id。整个操作是单个写事务。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        events = _load_events(conn)
        trips, anomalies = build_results(events)
        day_trips = [(e, x) for e, x in trips if day_of(e["ts"]) == day]
        day_anoms = [(ev, reason) for ev, reason in anomalies if day_of(ev["ts"]) == day]

        esv = get_esv(conn)
        dv = get_day_version(conn, day)

        conn.execute("UPDATE publications SET status='superseded' WHERE day=? AND status='current'", (day,))
        cur = conn.execute(
            "INSERT INTO publications(day, created_at, event_set_version, day_version, status, trip_count, anomaly_count)"
            " VALUES(?,?,?,?,?,?,?)",
            (day, now_iso(), esv, dv, "current", len(day_trips), len(day_anoms)),
        )
        pub_id = cur.lastrowid
        for e, x in day_trips:
            duration_min = round((x["ts"] - e["ts"]).total_seconds() / 60.0, 1)
            conn.execute(
                "INSERT INTO publication_trips(pub_id, card_id, entry_event_id, exit_event_id,"
                " entry_station, exit_station, entry_ts, exit_ts, duration_min) VALUES(?,?,?,?,?,?,?,?,?)",
                (pub_id, e["card_id"], e["event_id"], x["event_id"], e["station"],
                 x["station"], e["ts_utc"], x["ts_utc"], duration_min),
            )
        for ev, reason in day_anoms:
            conn.execute(
                "INSERT INTO publication_anomalies(pub_id, card_id, event_id, ts_utc, station, direction, reason)"
                " VALUES(?,?,?,?,?,?,?)",
                (pub_id, ev["card_id"], ev["event_id"], ev["ts_utc"], ev["station"], ev["direction"], reason),
            )
        conn.execute("COMMIT")
        return pub_id
    except Exception:
        conn.execute("ROLLBACK")
        raise
