"""闸机日流水验收：按闸机、UTC 日汇总流水号并比对登记的日终号。

验收口径：
- 只有同时带 ``gate_id`` 与 ``seq`` 的事件才计入验收；旧文件/缺流水号的记录不参与。
- 验收键 = (闸机 ID, 事件所在 UTC 日)；期望流水号为 1..登记终号。
- 已登记终号但 1..终号中存在未收到的号（或收到超出终号的号、同号重复）→ ``incomplete``。
- 有闸机流水但尚未登记终号 → ``pending``；完全没有闸机流水 → ``none``。
- 只有所有相关闸机日均登记且完整，该日验收结论才是 ``complete``。

``incomplete`` 的已登记闸机日会阻止该 UTC 日的新发布。验收结论每次由当前已提交
事件集与已提交登记记录即时计算，因此补传填缺、高修订移动闸机/日期/流水号后，
旧、新位置的验收结论都会自动重算。
"""
import json

MISSING_STATUS = "incomplete"
PENDING_STATUS = "pending"
COMPLETE_STATUS = "complete"
NONE_STATUS = "none"

ENTRY_COMPLETE = "complete"
ENTRY_UNREGISTERED = "unregistered"
ENTRY_INCOMPLETE = "incomplete"

STATUS_LABELS = {
    COMPLETE_STATUS: "完整",
    MISSING_STATUS: "缺号",
    PENDING_STATUS: "待登记终号",
    NONE_STATUS: "无闸机流水",
}

ENTRY_STATUS_LABELS = {
    ENTRY_COMPLETE: "完整",
    ENTRY_INCOMPLETE: "缺号",
    ENTRY_UNREGISTERED: "未登记终号",
}


def _canonical(obj):
    """确定性 JSON：用于发布快照与当前验收状态的逐字比较。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _event_rows(conn):
    return conn.execute(
        "SELECT event_id, gate_id, seq, substr(ts_utc, 1, 10) AS day"
        " FROM events WHERE gate_id IS NOT NULL AND seq IS NOT NULL"
    ).fetchall()


def _registration_rows(conn):
    return conn.execute(
        "SELECT gate_id, day, end_seq, registered_at FROM gate_day_registrations"
    ).fetchall()


def format_range(rng):
    """(3,5) → '3-5'；(9,9) → '9'。"""
    a, b = rng
    return str(a) if a == b else f"{a}-{b}"


def _missing_ranges(present, end_seq):
    """1..end_seq 中未收到的号的压缩区间（present 须升序且均为正整数）。"""
    missing = []
    cursor = 1
    for n in present:
        if n > end_seq:
            break
        if n > cursor:
            missing.append((cursor, n - 1))
        if n >= cursor:
            cursor = n + 1
    if cursor <= end_seq:
        missing.append((cursor, end_seq))
    return missing


def _snapshot_entry(e):
    """发布快照里保留的验收要点：不含逐号明细里的大列表，也不含登记时间元数据。"""
    return {
        "gate_id": e["gate_id"],
        "end_seq": e["end_seq"],
        "present_count": e["present_count"],
        "max_seq": e["max_seq"],
        "missing_ranges": e["missing_ranges"],
        "missing_total": e["missing_total"],
        "extra_seqs": e["extra_seqs"],
        "duplicates": e["duplicates"],
        "status": e["status"],
        "status_label": e["status_label"],
    }


def _compute_day(day, event_rows, reg_rows):
    """基于给定已提交行计算某 UTC 日的验收结论。"""
    present = {}
    for r in event_rows:
        if r["day"] == day:
            present.setdefault(r["gate_id"], []).append(int(r["seq"]))
    regs = {r["gate_id"]: r for r in reg_rows if r["day"] == day}

    entries = []
    for gate_id in sorted(set(present) | set(regs)):
        seqs = sorted(present.get(gate_id, []))
        counts = {}
        for n in seqs:
            counts[n] = counts.get(n, 0) + 1
        duplicates = sorted(n for n, c in counts.items() if c > 1)
        reg = regs.get(gate_id)
        end_seq = int(reg["end_seq"]) if reg is not None else None
        missing = []
        extra = []
        if end_seq is None:
            status = ENTRY_UNREGISTERED
        else:
            missing = _missing_ranges(sorted(counts), end_seq)
            extra = sorted(n for n in counts if n > end_seq)
            status = ENTRY_INCOMPLETE if (missing or extra or duplicates) else ENTRY_COMPLETE
        entries.append({
            "gate_id": gate_id,
            "day": day,
            "end_seq": end_seq,
            "present_count": len(seqs),
            "present_seqs": seqs,
            "max_seq": seqs[-1] if seqs else None,
            "missing_ranges": [format_range(r) for r in missing],
            "missing_total": sum(b - a + 1 for a, b in missing),
            "extra_seqs": extra,
            "duplicates": duplicates,
            "status": status,
            "status_label": ENTRY_STATUS_LABELS[status],
            "registered_at": reg["registered_at"] if reg is not None else None,
        })

    if any(e["status"] == ENTRY_INCOMPLETE for e in entries):
        status = MISSING_STATUS
    elif any(e["status"] == ENTRY_UNREGISTERED for e in entries):
        status = PENDING_STATUS
    elif entries:
        status = COMPLETE_STATUS
    else:
        status = NONE_STATUS

    blocking = status == MISSING_STATUS
    snapshot_obj = {"status": status, "entries": [_snapshot_entry(e) for e in entries]}
    return {
        "day": day,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "entries": entries,
        "blocking": blocking,
        "missing_total": sum(e["missing_total"] for e in entries),
        "snapshot": _canonical(snapshot_obj),
    }


EMPTY_SNAPSHOT = _canonical({"status": NONE_STATUS, "entries": []})


def acceptance_for_day(conn, day):
    """当前已提交状态下，某 UTC 日的闸机验收结论。"""
    begin_read(conn)
    return _compute_day(day, _event_rows(conn), _registration_rows(conn))


def acceptance_all(conn):
    """所有出现过闸机流水或终号登记的 UTC 日验收结论，日期倒序。

    调用方若需要与页面其它查询取同一已提交状态，应先 ``begin_read(conn)``（幂等）。
    """
    begin_read(conn)
    event_rows = _event_rows(conn)
    reg_rows = _registration_rows(conn)
    days = {r["day"] for r in event_rows} | {r["day"] for r in reg_rows}
    return [_compute_day(d, event_rows, reg_rows) for d in sorted(days, reverse=True)]


def begin_read(conn):
    """开启一个读事务，保证后续多次 SELECT 取自同一已提交快照。"""
    if not conn.in_transaction:
        conn.execute("BEGIN")


def normalize_snapshot(stored):
    """兼容旧库发布行没有验收快照的情况。"""
    return stored if stored and stored != "[]" else EMPTY_SNAPSHOT


def blocking_message(acceptance):
    """给页面/异常用的缺号说明。"""
    details = []
    for e in acceptance["entries"]:
        if e["status"] == ENTRY_INCOMPLETE:
            bits = []
            if e["missing_ranges"]:
                bits.append("缺号 " + ", ".join(e["missing_ranges"]))
            if e["extra_seqs"]:
                bits.append("超出终号 " + ", ".join(str(n) for n in e["extra_seqs"]))
            if e["duplicates"]:
                bits.append("重复号 " + ", ".join(str(n) for n in e["duplicates"]))
            details.append(f"闸机 {e['gate_id']}（终号 {e['end_seq']}）：" + "；".join(bits))
    return f"{acceptance['day']} 已登记闸机日存在缺号，禁止发布该日行程：" + "；".join(details)
