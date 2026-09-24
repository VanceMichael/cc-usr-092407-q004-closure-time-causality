# 机场中断影响服务

本项目提供纯后端机场中断影响服务。服务接收机场关闭、延长关闭和恢复开放事件，计算受影响的既有航班与旅客，将事件链和计算结果保存到 SQLite，并通过 HTTP 接口提供查询。

示例中的机场、航班时刻和旅客数量均为合成数据。运行期间不会请求外部航班、地图或通知服务。

## 目录

- `contracts/disruption-event.schema.json`：中断事件输入契约。
- `fixtures/airports.json`：机场时区与恢复缓冲时间。
- `fixtures/flights.json`：确定性的航班计划数据，包含跨午夜样例。
- `app/`：Python 3.12 标准库实现的业务服务。
- `tests/`：计算、校验、存储和 HTTP 集成测试。
- `scripts/docker_selftest.sh`：容器化黑盒自检入口。
- `compose.yaml`：输入校验容器、业务服务和 SQLite 持久卷。

## 启动

```bash
docker compose up -d --build --wait
curl -s http://127.0.0.1:8080/healthz
docker compose down
```

SQLite 默认位于容器内的 `/data/disruptions.db`，由 `disruption-data` 卷保存。`DB_PATH`、`HOST`、`PORT` 和 `FIXTURES_DIR` 均可通过环境变量调整。

本地运行只需要 Python 3.12：

```bash
python3 -m unittest discover -s tests
DB_PATH=./data/disruptions.db PORT=8080 python3 -m app
```

## 接口

所有请求和响应均为 JSON，错误统一使用以下结构：

```json
{"error": {"code": "unknown_airport", "message": "...", "details": {}}}
```

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/events` | 提交关闭、延长或恢复事件 |
| `GET` | `/api/v1/events/{event_id}` | 查询事件、处理状态和影响结果 |
| `GET` | `/api/v1/airports/{AIRPORT}/summary` | 查询机场影响汇总 |
| `GET` | `/api/v1/flights/affected` | 分页查询当前受影响航班 |
| `GET` | `/api/v1/chains/anomalies` | 只读列出启动审计隔离的异常事件链 |
| `GET` | `/healthz` | 检查服务和数据库健康状态 |

受影响航班查询支持 `airport`、`status`、`limit` 和 `offset` 参数。`status` 可取 `cancelled`、`delayed` 或 `pending_confirmation`。

## 事件规则

- 所有输入时间必须携带时区；先携带固定偏移解析为 UTC 瞬时，比较前再经机场 IANA 时区做一次本地往返规范化。大小比较一律在 UTC 瞬时上进行，因此夏令时重复时刻（如柏林秋季本地 02:30 出现两次）由其偏移区分，不会被错误折叠；错误明细同时给出 UTC 与机场本地时刻。
- `airport.closed` 创建事件链；未知结束时间可将 `effective_until` 设为 `null`。
- `airport.extended` 通过 `supersedes_event_id` 延长尚未结束的事件链。
- `airport.reopened` 结束事件链，机场在自身恢复缓冲时间结束后重新运行。
- 时间窗口采用左闭右开语义，恰好落在恢复时刻的航班不受影响。

### 时间因果校验

延长段与恢复事件必须满足下列因果约束，任一失败都返回 `422 validation_error`，且在单个事务内回滚——不写入事件、影响，也不推进任何重放计数；`details.errors[]` 中每条违规都带 `field` 精确指出冲突字段（并附 UTC/本地对照时刻）：

- **延长段不早于根关闭**：`extended.effective_from >= closed.effective_from`（否则 `must_not_precede_root_closure`）。
- **不跨过未说明的开放间隙**：当上一窗口有已知末端时，`extended.effective_from <= previous.effective_until`；严格大于报 `extension_leaves_uncovered_gap`。起点恰好等于上一窗口末端（端点相接）合法。
- **延长必须推后末端**：`extended.effective_until > previous.effective_until`。
- **恢复时点落在当前有效关闭范围内**：`reopened.effective_from` 不得早于根关闭（`reopen_before_chain_start`），也不得晚于当前窗口末端（`reopen_outside_closure_window`）。
- **机场缓冲结束仍在关闭范围内**：`reopened.effective_from + reopen_buffer_minutes <= 当前窗口末端`；越过报 `reopen_buffer_extends_past_closure`，恰好相接合法。
- **链不可分叉、不可接续已恢复链**：同一事件至多一个后继（`chain_already_extended`），接续 `reopened` 报 `chain_already_closed`，版本必须沿链严格递增。

### reported_at 规则（清晰且可审计）

以服务 UTC 时钟为基准，边界一律左闭：

- 领先服务时钟超过 60 秒视为时钟错误的未来事件，报 `reported_at_in_future`。
- 早于 `effective_from` 超过 14 天视为超出预报窗口，报 `reported_too_far_in_advance`。
- 晚于 `effective_from` 超过 24 小时视为迟报，报 `reported_too_late`（此类事件应走权威更正通道）。
- 沿同一事件链，后继事件的 `reported_at` 不得早于其前驱（`reported_at_out_of_order`）。

### 启动检查与异常链隔离

服务启动时对全部已持久化事件重新执行只依赖事件内容的因果审计（不依赖当前时钟，因此未来事件/迟报规则不参与重判）：

- 违规链（恢复早于根关闭、延长早于根、跨间隙、分叉、悬空引用等）写入 `chain_anomalies` 表并标记为 `quarantined`。
- 隔离链**保留原始事件与影响供只读追溯**（`GET /api/v1/events/{id}` 返回 `chain_state: "quarantined"` 与 `chain_anomaly`），但不参与机场汇总、受影响航班查询和活跃链计数，也不能再接入新事件（`chain_quarantined`）。
- 审计结论确定：同一数据库在任意时间、容器重建后重新审计得到相同的分组、原因、成员与 `detected_at`（取组内最晚 `reported_at`）。

- 同一航班分别按出发机场的计划起飞时刻和到达机场的计划到达时刻判断。
- 无法改时或所需延误超过上限的航班标记为 `cancelled`；可在上限内改时的航班标记为 `delayed`；结束时间未知时标记为 `pending_confirmation`。
- 跨午夜依据受影响机场的本地时区判定。
- `event_id` 是幂等键。相同内容重试返回原结果；相同标识携带不同内容时返回 `409 event_conflict`。
- 事件链版本必须递增，且不能继续延长已经恢复开放的事件链。

## 编译检查

```bash
python3 -m compileall -q app
```

## 验证

单元与集成测试：

```bash
python3 -m unittest discover -s tests -v
```

完整容器自检：

```bash
scripts/docker_selftest.sh
```

容器自检会从空卷构建并启动服务，验证输入校验、幂等重放、跨午夜计算、事件链变化、分页查询以及容器重建后的数据保留，结束时清理测试资源。

原始契约与夹具也可单独校验：

```bash
docker compose up -d --build scaffold
docker compose exec scaffold sh scaffold/validate_inputs.sh
```
