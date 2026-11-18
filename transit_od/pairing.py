"""按卡配对进出站事件，生成行程与异常。

规则（与需求对应）：
- 每卡按时刻排序处理；
- 再次进站 → 前次未配对进站悬空（异常）；
- 出站仅匹配最近一次 4 小时内的进站（含恰好 4 小时），匹配成功即消耗该进站（1:1 配对）；
- 其余事件（悬空进站、孤立出站）列为异常。
"""
from datetime import timedelta

WINDOW = timedelta(hours=4)

REASON_DANGLING_ENTRY = "dangling_entry"  # 进站悬空：无匹配出站
REASON_ORPHAN_EXIT = "orphan_exit"        # 孤立出站：4 小时内无匹配进站


def pair_card_events(evs):
    """单卡事件（已按 (ts, event_id) 排序，且同卡时刻互异）→ (trips, anomalies)。

    trips: [(entry_ev, exit_ev), ...]；anomalies: [(ev, reason), ...]
    """
    trips, anomalies = [], []
    open_entry = None
    for ev in evs:
        if ev["direction"] == "in":
            if open_entry is not None:
                anomalies.append((open_entry, REASON_DANGLING_ENTRY))
            open_entry = ev
        else:  # out
            if open_entry is not None and ev["ts"] - open_entry["ts"] <= WINDOW:
                trips.append((open_entry, ev))
                open_entry = None
            else:
                anomalies.append((ev, REASON_ORPHAN_EXIT))
    if open_entry is not None:
        anomalies.append((open_entry, REASON_DANGLING_ENTRY))
    return trips, anomalies


def build_results(events):
    """对整批事件（多卡）配对。events: dict 列表，需含 card_id/ts/event_id/direction。"""
    by_card = {}
    for ev in events:
        by_card.setdefault(ev["card_id"], []).append(ev)
    trips, anomalies = [], []
    for card_id in sorted(by_card):
        evs = sorted(by_card[card_id], key=lambda e: (e["ts"], e["event_id"]))
        t, a = pair_card_events(evs)
        trips.extend(t)
        anomalies.extend(a)
    trips.sort(key=lambda p: (p[0]["ts"], p[0]["event_id"]))
    anomalies.sort(key=lambda p: (p[0]["ts"], p[0]["event_id"]))
    return trips, anomalies
