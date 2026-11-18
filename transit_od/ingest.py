"""CSV 上传摄取：解析校验、修订/冲突规则、原子批次提交。

规则实现（与需求对应）：
- 同一 event_id：仅更高修订替换旧值；同版同文重传沿用旧值；同版异文 → 冲突，整批失败；
  更低修订 → 忽略（迟到的旧重传）。
- 同卡同刻不同事件（批内或与已提交事件集冲突）→ 整批失败。
- 任何失败都整体回滚并记录失败批次，不产生半份结果。
"""
import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone

from .db import get_esv, now_iso

REQUIRED_COLS = ["card_id", "event_id", "revision", "ts_utc", "station", "direction"]
OPTIONAL_COLS = ["gate_id", "seq"]

DIR_IN = {"in", "entry", "enter", "进", "进站"}
DIR_OUT = {"out", "exit", "leave", "出", "出站"}

REVISION_RE = re.compile(r"^[0-9]+$")
MAX_FIELD_LEN = 200
DETAILS_CAP = 500  # 批次明细最多记录条数

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class BatchFormatError(Exception):
    """CSV 结构层面错误（缺列等）。"""


class BatchRejected(Exception):
    """批次内容违反规则，整批拒绝。"""


# ---------- 时间工具 ----------

def parse_ts(s):
    s = (s or "").strip()
    if not s:
        raise ValueError("UTC 时刻为空")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"UTC 时刻格式无法解析: {s!r}（应为 ISO 8601，如 2026-11-16T23:30:00Z）")
    if dt.tzinfo is None:
        raise ValueError(f"UTC 时刻必须带时区标记（Z 或 +00:00）: {s!r}")
    return dt.astimezone(timezone.utc)


def epoch_us(dt):
    d = dt - _EPOCH
    return d.days * 86_400_000_000 + d.seconds * 1_000_000 + d.microseconds


def datetime_from_us(us):
    return _EPOCH + timedelta(microseconds=us)


def iso_z(dt):
    if dt.microsecond:
        frac = f"{dt.microsecond:06d}".rstrip("0")
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + "." + frac + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_of(dt):
    return dt.strftime("%Y-%m-%d")


def days_affected(dt):
    """一个事件会影响哪些 UTC 日的结果：自身所在日 + 前一日
    （出站可回溯匹配 4 小时内的进站，行程归进站日，因此可能改变前一日的结果）。"""
    return {day_of(dt), day_of(dt - timedelta(days=1))}


# ---------- CSV 解析 ----------

def _norm_direction(s):
    v = (s or "").strip().lower()
    if v in DIR_IN:
        return "in"
    if v in DIR_OUT:
        return "out"
    raise ValueError(f"进/出站方向无法识别: {s!r}（应为 in/out）")


def _norm_revision(s):
    v = (s or "").strip()
    if not REVISION_RE.match(v):
        raise ValueError(f"修订号必须为正整数: {s!r}")
    n = int(v)
    if n < 1:
        raise ValueError(f"修订号必须为正整数: {s!r}")
    return n


def _norm_text(s, name):
    v = (s or "").strip()
    if not v:
        raise ValueError(f"{name} 为空")
    if len(v) > MAX_FIELD_LEN:
        raise ValueError(f"{name} 超长（>{MAX_FIELD_LEN} 字符）")
    return v


def _norm_gate(s):
    v = (s or "").strip()
    if not v:
        return None
    if len(v) > MAX_FIELD_LEN:
        raise ValueError(f"闸机 ID 超长（>{MAX_FIELD_LEN} 字符）")
    return v


def _norm_seq(s):
    v = (s or "").strip()
    if not v:
        return None
    if not REVISION_RE.match(v):
        raise ValueError(f"流水号必须为从 1 编起的正整数: {s!r}")
    n = int(v)
    if n < 1:
        raise ValueError(f"流水号必须为从 1 编起的正整数: {s!r}")
    return n


def parse_csv(text):
    """返回 (rows, errors)。rows 为校验通过的数据行（dict），errors 为行级错误列表。

    ``gate_id`` / ``seq`` 为可选列：旧文件没有这两列仍可上传（不计入验收）；
    某行只给其中一列时该行仍可入库，但缺少流水号即不计入验收（两者都存 NULL）。
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise BatchFormatError("CSV 为空或缺少表头")
    header_map = {}
    for h in reader.fieldnames:
        if h is not None:
            header_map[h.strip().lower()] = h
    missing = [c for c in REQUIRED_COLS if c not in header_map]
    if missing:
        raise BatchFormatError(f"CSV 缺少必需列: {', '.join(missing)}（需要 {', '.join(REQUIRED_COLS)}）")

    def cell(raw, col):
        key = header_map.get(col)
        return raw.get(key) if key is not None else None

    rows, errors = [], []
    for line_no, raw in enumerate(reader, start=2):
        try:
            ts = parse_ts(cell(raw, "ts_utc"))
            gate_id = _norm_gate(cell(raw, "gate_id"))
            seq = _norm_seq(cell(raw, "seq"))
            if gate_id is None or seq is None:
                gate_id = seq = None  # 缺少流水号 → 不计入闸机验收
            rows.append({
                "line": line_no,
                "card_id": _norm_text(cell(raw, "card_id"), "匿名卡号"),
                "event_id": _norm_text(cell(raw, "event_id"), "事件 ID"),
                "revision": _norm_revision(cell(raw, "revision")),
                "ts": ts,
                "ts_utc": iso_z(ts),
                "ts_us": epoch_us(ts),
                "station": _norm_text(cell(raw, "station"), "站点"),
                "direction": _norm_direction(cell(raw, "direction")),
                "gate_id": gate_id,
                "seq": seq,
            })
        except ValueError as e:
            errors.append(f"第 {line_no} 行: {e}")
    return rows, errors


def _core_content(row):
    """业务内容（修订号、闸机、流水号之外）。"""
    return (row["card_id"], row["ts_us"], row["station"], row["direction"])


def _core_content_db(db_row):
    return (db_row["card_id"], db_row["ts_epoch_us"], db_row["station"], db_row["direction"])


def _gate_content(row):
    """闸机验收元数据；缺流水号的记录为 None，不计入验收。"""
    return None if row["gate_id"] is None or row["seq"] is None else (row["gate_id"], row["seq"])


def _gate_content_db(db_row):
    return None if db_row["gate_id"] is None or db_row["seq"] is None else (db_row["gate_id"], db_row["seq"])


def _merge_same_core(rows, event_id):
    """同一 event_id、同一修订、业务内容相同的行合并为一行。

    闸机/流水号可缺省：只有一行带完整闸机信息则采用之；多行带互相不同的
    闸机信息则视为同版异文冲突。返回 (merged_row, conflict_reason)。
    """
    gates = {_gate_content(r) for r in rows}
    gates.discard(None)
    if len(gates) > 1:
        return None, f"事件 {event_id}: 批内同为修订 {rows[0]['revision']} 但闸机/流水号不一致"
    if gates:
        chosen = next(r for r in rows if _gate_content(r) is not None)
    else:
        chosen = rows[0]
    return chosen, None


# ---------- 批次提交 ----------

def record_failed_batch(conn, filename, uploaded_at, error, rows_total=0):
    """失败批次也落库（仅审计记录，不触碰事件集），自动提交。"""
    cur = conn.execute(
        "INSERT INTO batches(uploaded_at, filename, status, rows_total, error) VALUES(?,?,?,?,?)",
        (uploaded_at, filename, "failed", rows_total, error[:4000]),
    )
    return cur.lastrowid


def commit_batch(conn, filename, text):
    """解析并原子提交一个上传批次。返回 (batch_id, ok, error)。"""
    uploaded_at = now_iso()
    try:
        rows, parse_errors = parse_csv(text)
    except BatchFormatError as e:
        return record_failed_batch(conn, filename, uploaded_at, str(e)), False, str(e)

    rows_total = len(rows) + len(parse_errors)
    if parse_errors:
        msg = "CSV 行校验失败: " + "；".join(parse_errors[:20])
        return record_failed_batch(conn, filename, uploaded_at, msg, rows_total), False, msg
    if not rows:
        msg = "CSV 中没有数据行"
        return record_failed_batch(conn, filename, uploaded_at, msg, 0), False, msg

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1) 批内按 event_id 去重：最高修订获胜；并列最高修订业务内容异文 → 冲突。
        #    闸机/流水号可选：并列行中只有一方带完整闸机信息时采用之。
        groups = {}
        for r in rows:
            groups.setdefault(r["event_id"], []).append(r)
        winners, conflicts = [], []
        for eid, rs in groups.items():
            max_rev = max(r["revision"] for r in rs)
            tops = [r for r in rs if r["revision"] == max_rev]
            if any(_core_content(t) != _core_content(tops[0]) for t in tops[1:]):
                conflicts.append(f"事件 {eid}: 批内同为修订 {max_rev} 但内容不一致")
                continue
            merged, conflict = _merge_same_core(tops, eid)
            if conflict:
                conflicts.append(conflict)
            else:
                winners.append(merged)
        if conflicts:
            raise BatchRejected("；".join(conflicts[:20]))

        # 2) 与已提交事件集比较，决定 新增/更新/沿用/忽略/冲突
        existing = {row["event_id"]: row for row in conn.execute("SELECT * FROM events")}
        inserts, updates = [], []
        n_unchanged = n_ignored = 0
        details = []
        for r in sorted(winners, key=lambda x: x["line"]):
            old = existing.get(r["event_id"])
            if old is None:
                inserts.append(r)
                details.append({"event_id": r["event_id"], "action": "新增"})
            elif r["revision"] > old["revision"]:
                updates.append((r, old))
                details.append({"event_id": r["event_id"], "action": f"修订 {old['revision']}→{r['revision']}，替换旧值"})
            elif r["revision"] == old["revision"]:
                if _core_content(r) != _core_content_db(old):
                    conflicts.append(f"事件 {r['event_id']}: 修订 {r['revision']} 与已提交版本异文冲突")
                elif _gate_content_db(old) is None and _gate_content(r) is not None:
                    # 旧记录缺流水号，本次补录闸机/流水号：业务内容不变，仅补验收元数据
                    updates.append((r, old))
                    details.append({"event_id": r["event_id"],
                                    "action": "同版同文补录闸机/流水号，更新验收元数据"})
                elif (_gate_content(r) is not None and _gate_content_db(old) is not None
                      and _gate_content(r) != _gate_content_db(old)):
                    conflicts.append(f"事件 {r['event_id']}: 修订 {r['revision']} 的闸机/流水号与已提交版本冲突")
                else:
                    n_unchanged += 1
                    if _gate_content(r) is None and _gate_content_db(old) is not None:
                        details.append({"event_id": r["event_id"],
                                        "action": "同版同文重传（未带流水号，保留已提交闸机流水），沿用旧值"})
                    else:
                        details.append({"event_id": r["event_id"], "action": "同版同文重传，沿用旧值"})
            else:
                n_ignored += 1
                details.append({"event_id": r["event_id"], "action": f"修订 {r['revision']} 低于已提交 {old['revision']}，忽略"})
        if conflicts:
            raise BatchRejected("；".join(conflicts[:20]))

        # 3) 同卡同刻不同事件检查（基于批次应用后的状态）
        occ = {}
        for ev in existing.values():
            occ[(ev["card_id"], ev["ts_epoch_us"])] = ev["event_id"]
        for r, old in updates:
            occ.pop((old["card_id"], old["ts_epoch_us"]), None)
        collisions = []
        for r in inserts + [u[0] for u in updates]:
            key = (r["card_id"], r["ts_us"])
            other = occ.get(key)
            if other is not None and other != r["event_id"]:
                collisions.append(
                    f"卡 {r['card_id']} 在 {r['ts_utc']} 已存在事件 {other}，与本批事件 {r['event_id']} 为同卡同刻不同事件"
                )
            else:
                occ[key] = r["event_id"]
        if collisions:
            raise BatchRejected("同卡同刻不同事件: " + "；".join(collisions[:20]))

        # 3b) 同闸机同日同流水号若落在不同事件 → 整批拒绝
        gate_occ = {}
        for ev in existing.values():
            if ev["gate_id"] is not None and ev["seq"] is not None:
                gate_occ[(ev["gate_id"], day_of(datetime_from_us(ev["ts_epoch_us"])), ev["seq"])] = ev["event_id"]
        for r, old in updates:
            if old["gate_id"] is not None and old["seq"] is not None:
                gate_occ.pop((old["gate_id"], day_of(datetime_from_us(old["ts_epoch_us"])), old["seq"]), None)
        gate_conflicts = []
        for r in inserts + [u[0] for u in updates]:
            if r["gate_id"] is None or r["seq"] is None:
                continue
            key = (r["gate_id"], day_of(r["ts"]), r["seq"])
            other = gate_occ.get(key)
            if other is not None and other != r["event_id"]:
                gate_conflicts.append(
                    f"闸机 {r['gate_id']} 在 {key[1]} 的流水号 {r['seq']} 已属于事件 {other}，"
                    f"与本批事件 {r['event_id']} 不同"
                )
            else:
                gate_occ[key] = r["event_id"]
        if gate_conflicts:
            raise BatchRejected("同闸机同日同流水号不同事件: " + "；".join(gate_conflicts[:20]))

        # 4) 计算受影响日期与事件集版本（无实际变更的批次不推进版本）
        affected_days = set()
        for r in inserts:
            affected_days |= days_affected(r["ts"])
        for r, old in updates:
            affected_days |= days_affected(r["ts"])
            affected_days |= days_affected(datetime_from_us(old["ts_epoch_us"]))
        changed = bool(inserts or updates)
        esv = get_esv(conn) + 1 if changed else get_esv(conn)

        # 5) 先写批次行拿到 batch_id，再应用事件变更（同一事务，整体成败）
        cur = conn.execute(
            "INSERT INTO batches(uploaded_at, filename, status, rows_total, rows_new, rows_updated,"
            " rows_unchanged, rows_ignored, event_set_version, details) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uploaded_at, filename, "committed", rows_total, len(inserts), len(updates),
             n_unchanged, n_ignored, esv, json.dumps(details[:DETAILS_CAP], ensure_ascii=False)),
        )
        batch_id = cur.lastrowid

        for r in inserts:
            conn.execute(
                "INSERT INTO events(event_id, revision, card_id, ts_utc, ts_epoch_us, station, direction,"
                " gate_id, seq, batch_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (r["event_id"], r["revision"], r["card_id"], r["ts_utc"], r["ts_us"],
                 r["station"], r["direction"], r["gate_id"], r["seq"], batch_id, uploaded_at),
            )
        for r, old in updates:
            conn.execute(
                "UPDATE events SET revision=?, card_id=?, ts_utc=?, ts_epoch_us=?, station=?, direction=?,"
                " gate_id=?, seq=?, batch_id=?, updated_at=? WHERE event_id=?",
                (r["revision"], r["card_id"], r["ts_utc"], r["ts_us"], r["station"],
                 r["direction"], r["gate_id"], r["seq"], batch_id, uploaded_at, r["event_id"]),
            )

        if changed:
            conn.execute("UPDATE meta SET value=? WHERE key='event_set_version'", (str(esv),))
            for d in sorted(affected_days):
                conn.execute(
                    "INSERT INTO day_versions(day, version) VALUES(?, 1)"
                    " ON CONFLICT(day) DO UPDATE SET version=version+1",
                    (d,),
                )
                conn.execute("INSERT INTO batch_affected_days(batch_id, day) VALUES(?,?)", (batch_id, d))

        conn.execute("COMMIT")
        return batch_id, True, None
    except BatchRejected as e:
        conn.execute("ROLLBACK")
        msg = str(e)
        return record_failed_batch(conn, filename, uploaded_at, msg, rows_total), False, msg
    except Exception:
        conn.execute("ROLLBACK")
        raise
