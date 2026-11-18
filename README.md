# 闸机刷卡记录 OD 行程发布平台

把闸机补传/改错的刷卡记录整理成 OD 分析用行程：Flask + SQLite 网页应用，
支持 CSV 上传（含修订与冲突规则）、按卡配对生成行程、按 UTC 日发布/查看行程与异常、
过期发布保留可读。

## 快速启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

打开 http://localhost:5000 。SQLite 数据库保存在命名卷 `transit_data`（容器内 `/data/app.db`），
容器重启/重建后数据与发布仍可查看。

### 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py          # 默认 http://0.0.0.0:5000 ，库文件 ./data/app.db
```

环境变量：`APP_DB`（SQLite 路径）、`PORT`、`SECRET_KEY`。

### 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## CSV 格式

表头必需列（不区分大小写，多余列忽略）：

```csv
card_id,event_id,revision,ts_utc,station,direction
C001,E1,1,2026-11-15T22:30:00Z,Alpha,in
```

- `card_id`：匿名卡号；`event_id`：事件 ID；`station`：站点名。
- `revision`：正整数修订号。
- `ts_utc`：ISO 8601 UTC 时刻（须带 `Z` 或 `+00:00`）。
- `direction`：`in`/`out`（兼容 `entry`/`exit`、`进`/`出`）。
- 文件须为 UTF-8（兼容 BOM），上传上限 16MB。

示例文件见 `sample_data/`：`batch1_base.csv`（正常+跨午夜+异常）、
`batch2_correction.csv`（更正+同版同文重传）、`batch3_conflict.csv`（同版异文冲突，失败）、
`batch4_same_instant.csv`（同卡同刻，失败）、`batch5_stale_revision.csv`（低修订忽略）。

## 规则实现

### 摄取（每个上传文件 = 一个原子批次）

| 情形 | 处理 |
|---|---|
| 新 event_id | 插入 |
| 同 event_id，更高修订 | 替换旧值 |
| 同 event_id，同版同文 | 沿用旧值（无操作，批次记为"沿用"） |
| 同 event_id，同版异文 | 冲突 → **整批失败** |
| 同 event_id，更低修订 | 忽略（迟到的旧重传） |
| 同卡同刻不同事件（批内或与已提交事件集） | **整批失败** |
| CSV 结构/行校验错误 | **整批失败** |

批次在单个 `BEGIN IMMEDIATE` 事务内校验并提交；任何失败整体回滚并记录失败批次（含原因），
事件集不会出现半份结果。无实际变更的批次不推进事件集版本。

### 配对（每卡按时刻排序）

- 再次进站 → 前次未配对进站**悬空**（异常）。
- 出站仅匹配最近一次 **4 小时内**（含恰 4 小时）的进站，1:1 消耗。
- 其余事件列为异常：悬空进站 `dangling_entry`、孤立出站 `orphan_exit`。

### 按 UTC 日发布

- 行程归**进站日**（跨午夜行程归进站所在日）；孤立出站归**自身日期**；悬空进站归进站自身日期。
- 发布为不可变快照：在单个事务内读取已提交事件集 → 配对 → 写入
  `publications` / `publication_trips` / `publication_anomalies`，与上传互斥，
  因此每份发布都基于同一已提交事件集（发布记录 `event_set_version` 佐证）。
- 补传/更正使某日数据变化后，该日最新发布标记为**已过期**（旧发布保持可读）；
  重新发布后旧发布置为 superseded，仍可查看快照。
- 过期判定：批次提交时按受影响日期（事件所在日及其前一日，因 4 小时配对窗口）
  推进 `day_versions`；发布的 `day_version` 落后于当前值即标过期。

## 页面

- **日期与发布** `/days`：各 UTC 日事件数、最新发布、状态（未发布/当前有效/已过期）、发布按钮。
- **日视图** `/days/<day>`：最新发布的行程与异常表、过期提示、发布历史。
- **发布快照** `/publications/<id>`：任意历史发布内容（过期标黄条），保持可读。
- **上传 CSV** `/upload`；**批次记录** `/batches`（含失败原因与逐事件明细）；**已提交事件** `/events`。

## 设计取舍（已知边界）

- **过期判定为保守近似**：批次会影响"事件日及其前一日"。若更正未真正改变某日结果，
  该日发布也可能被标过期（安全方向，重新发布即可），不会漏标。
- **同版异文冲突即整批失败**：需求只对"同卡同刻"明示整批失败；冲突同样按失败处理，
  以满足"不得出现半份结果"，需人工厘清后重传。
- 配对窗口边界含恰好 4 小时；同一进站只参与一次配对（1:1 消耗）。
- 容器内使用 Flask 自带开发服务器（单进程、SQLite 串行写满足本场景）；
  如需更高并发可换 gunicorn 单 worker。
