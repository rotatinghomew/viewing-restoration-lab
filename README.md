# 现场掉流的观赛损失与补偿发放

节目标识与付费权益独立：同一份订单可对应不同节目。UTC 秒是算例时钟，`start_utc` 到 `end_utc` 为节目实际节目轴，用 `from_pos`/`to_pos` 表示看到的片段，左含右不含。观察范围外未知；`stalled` 明确未播放、`playing` 明确播放、`missing` 不是断流。只有播出源对应且确认的故障区间才可归因，存在合法成功替代的片段不能重复补偿。补偿规则每个节目分别生效：以证实丢失秒数达到门槛为最低条件；`critical` 是不能用普通回看替代的片段，现金积分价值在规定表中。回看有效期至少留足受补片段的播放时长；若已合法回看完成，不视为还可以发给该人的新补看。`posted` 表示按回执已发到对应权益，`unknown` 需核对不得擅发同一覆盖，`declined` 确认未发。

`playback.session` 为 `-` 时进度已在节目轴上，否则片段的两端都是会话本地秒；节目秒等于本地秒加该会话的同一个整数位移。`calibration` 给出位移允许范围，两端都含；同一时点只采用已送达的证据，且同个 `anchor` 以已送达的最新记录为准，不同锚点同时约束。`session_limits` 是节目方在采集开始时给出的有限位移范围；切源后的新会话有自己的范围。只将落在权益生效时间和节目范围内的观测计入，`missing` 的可能损失只在该会话已有观看尝试且与对应源故障重合的时段内判断。回执的 `event` 是发送标识，同标识的后续状态不等于另一笔发放；回执区间始终是发放时确定的节目轴区间。

## 数据包

`sample/` 是本地合成的业务记录，均为 UTF-8 CSV；`table-format.json` 给出各表的列顺序与标量类型，`str` 中 `-` 表示不适用，逗号分隔多值。不同表中同名业务标识用于关联，具体含义见上文。资料入口只检查表头和格式，不对业务状态作判断。

## 运行

使用 Python 3.11 标准库，不需要安装三方依赖。读取样例与运行资料检查：

```sh
python3 intake.py
python3 -m unittest discover -s tests -v
```

也可以将同格式的资料目录作为 `intake.py` 的位置参数。

---

## 客服系统

本仓库在资料检查之外提供了可持续导入后续数据、按订单/时点审计和客服查询的实现：

- `loss_service/intervals.py`：左闭右开区间、交并差。
- `loss_service/engine.py`：校准约束、相干解释枚举、损失上下界、确定/可能片段、回执、回看有效期和人工事项。
- `loss_service/store.py`：SQLite 追加式存储；重复行按内容哈希幂等忽略，支持后续 CSV/JSON 增量。
- `loss_service/web.py`：标准库 HTTP/JSON 服务和简易客服页面。
- `service.py`：命令行入口。

### 1. 导入与查询

默认状态库在 `.runtime/loss.sqlite3`。首次查询会自动导入 `sample/`：

```sh
# 增量导入一个同格式 CSV 目录
python3 service.py ingest sample

# 客服文本视图
python3 service.py order b2
python3 service.py order b2 --at 10250

# 机器可读 JSON
python3 service.py order b2 --json

# 达到上界复核门槛、未明回执或校准冲突的队列
python3 service.py queue
```

也可以指定独立数据库：

```sh
python3 service.py --db /tmp/cs.sqlite3 ingest /path/to/later-batch
python3 service.py --db /tmp/cs.sqlite3 order b2 --at 12100 --json
```

作为库使用：

```python
from loss_service import LossService, Store

with Store("state.sqlite3") as store:
    store.ingest_directory("/path/to/later-batch")
    service = LossService.from_store(store)
    report = service.order("b2", as_of=12100)
```

### 2. HTTP 客服台

```sh
python3 service.py serve --host 127.0.0.1 --port 8765
# 或 HOST=0.0.0.0 PORT=8765 python3 app.py
```

打开 <http://127.0.0.1:8765/> 可按订单查询。接口：

- `GET /api/order?order=b2[&at=10250]`
- `GET /api/queue[?at=18000]`
- `POST /api/ingest`

增量 JSON 示例：

```sh
curl -s -X POST http://127.0.0.1:8765/api/ingest \
  -H 'Content-Type: application/json' \
  -d '{"table":"playback","records":[{"id":"s12","person":"p2","program":"g11","source":"beta","from_pos":4400,"to_pos":4600,"state":"playing","known_at":20000,"session":"-"}]}'
```

## 损失口径

系统为每个 `(订单, 节目)` 独立计算，套票中的 `g12` 等其他节目不会因 `g11` 异常被连坐。跨 `local_day_boundary_utc` 不切分权益或片段；权益只按连续 UTC/节目轴和原 `order + program` 核对。

### 相干校准解释

每个命名会话选择一个满足 `session_limits`、截至 `as_of` 已送达校准锚点的整数位移。同一会话的所有片段在同一个“世界”中使用同一位移；不同会话的位移独立。系统枚举这些相干世界并做区间集合运算，因此不会把互斥解释下的两段损失相加。

报告中的 `loss`：

- `confirmed_seconds`：所有相干世界中 `stalled ∩ 对应源 outage ∩ 权益/节目范围 - playing` 后的最小秒数，即下界/证实总量。
- `definite`：在每个相干世界都确定未播放、且不能被合法 `playing` 覆盖的片段。
- `definite_named_seconds`：`definite` 的长度。它可能小于 `confirmed_seconds`；差额来自“每个解释都证明有损失，但不同解释把它放在不同位置”。
- `possible` / `upper_seconds`：上界采用保守的可归因故障天花板：某源已有观看尝试后，其已确认 outage 内、没有被 `playing` 覆盖的未知时间均可能损失。这适合决定是否发起人工复核。
- `observed_possible` / `observed_only_upper_seconds`：只按已送达的 `stalled`、`missing` 片段计算的较窄上界，便于区分“记录内可能”和“观察范围外但仍在故障区间内的保守可能”。

`missing` 永远不会进入确定损失；它只进入可能上界，并必须与同源 outage 重合。`playing`、重传重叠、切到合法源后成功播放的区间都会合并去重，不重复计损。

### 校准冲突

若同一会话最新锚点与 `session_limits` 没有共同位移，报告的 `calibration[].status` 为 `incompatible`，并在 `manual_items` 中给出：

- 无法对齐的 `session`；
- 采用的最新锚点范围；
- 受影响的 `playback_ids`。

冲突会话的确定损失不参与结论；若其中有 `playing` 片段，系统只按初始 `session_limits` 形成保守覆盖包络，任何可能被它覆盖的位置都不会被硬称为“确定净损失”。不依赖该会话的直接进度（`session=-`）和其他校准会话仍照常确认。

## 补偿、回执与更正

每个回执 `event` 聚合同标识的全部状态行，而不是把 `unknown → declined` 当成两笔发放：

- `posted`/`posted:true`：已发，区间和事件不可变，系统不会生成撤销或移动回执。
- `unknown`：其固定区间进入 `available_after_holds` 的保留/阻断集合；核对前不会建议重发同一覆盖。
- `declined`：确认未发后不再阻断，系统按当前证据重新计算可补范围。

`compensation` 中分别给出：

- `posted_or_rewatched`：扣除合法已回看和已发回执后的上下界/片段；
- `available_after_holds`：再扣除未明回执保留区间后的可核对范围；
- `ordinary_definite`：可点名的普通确定片段；
- `critical_definite`：可点名的关键确定片段；
- `replay_recommendations`：同时满足地区、节目、几何覆盖和 `until - max(as_of,start) >= 片段秒数` 的回放方案；
- `credit_recommendation`：关键片段和本场 `program.credit` 积分规则；
- `clean_unfulfillable`：片段确定，但当前没有几何覆盖或有效期不足的回放；
- `receipt_summary`：已发积分/回看、未明和已拒绝事件。

是否可发仍由运营按节目规则判断：

1. `upper_seconds >= threshold` 才进入复核；
2. 新发放要求 `confirmed_seconds >= threshold`；
3. 回执/积分必须对应 `definite` 中能点名的片段，不能只为达到秒数而为互斥的可能位置发放；
4. 普通回看必须覆盖该确定片段且有效期内留足观看时间；
5. 已被原权益 `playing`、合法 `rewatched`、已发或未明回执覆盖的部分不重复建议；
6. 若已有 `posted` 回执，校准收窄导致损失位置移动时，系统只输出 `posted_receipt_correction_gap` 人工差额事项，不会静默再发。

每个回执事件还带 `decision_snapshot`，重建该事件首次出现时点已送达的校准域、损失上下界/片段、回执区间是否能被确定片段支持，以及当时回放窗口是否足够。订单查询因此能同时看到“当时依据什么决定”和“后来证据更正后现在还能补什么/缺口在哪里”。
