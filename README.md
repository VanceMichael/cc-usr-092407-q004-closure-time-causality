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
| `GET` | `/healthz` | 检查服务和数据库健康状态 |

受影响航班查询支持 `airport`、`status`、`limit` 和 `offset` 参数。`status` 可取 `cancelled`、`delayed` 或 `pending_confirmation`。

## 事件规则

- 所有输入时间必须携带时区偏移（`Z` 或 `±HH:MM`），比较前统一转换为 UTC；朴素（无时区）时间戳一律拒绝。机场时区仅用于本地跨午夜判定——这同时覆盖夏令时秋季重复时刻（相同墙上时间用不同偏移表示不同瞬时）、跨午夜与左右端点。
- `airport.closed` 创建事件链；未知结束时间可将 `effective_until` 设为 `null`。
- `airport.extended` 通过 `supersedes_event_id` 延长尚未结束的事件链。
- `airport.reopened` 结束事件链，机场在自身恢复缓冲时间结束后重新运行。
- 时间窗口采用左闭右开语义，恰好落在恢复时刻的航班不受影响。

### 时间因果校验

- 延长段的 `effective_from` 不得早于根关闭起点，也不得晚于上一窗口末端（不允许跨过未说明的开放间隙）；延长段末端必须严格推后。延长起点恰好等于上一窗口末端（首尾相接）合法。
- 恢复时点不得早于根关闭起点；恢复时点加机场缓冲后的"缓冲结束"不得晚于当前有效关闭末端（缓冲结束恰好等于末端合法）。前驱为开放结束（`effective_until=null`）的链由恢复事件提供结束时点。
- 不得继续延长一条已经恢复开放的链，也不得在已隔离的异常链上追加事件。
- `reported_at` 规则（与服务器 UTC 时钟比较，1 分钟时钟漂移容差）：不得是未来时刻；不得晚于生效窗口结束；相对生效开始滞后超过 30 天拒绝；滞后超过 15 分钟宽限期的迟报允许接收，并在事件状态的 `processing.reporting` 中标注 `late` 与滞后分钟数（可审计但不隔离）。
- 同一航班分别按出发机场的计划起飞时刻和到达机场的计划到达时刻判断。
- 无法改时或所需延误超过上限的航班标记为 `cancelled`；可在上限内改时的航班标记为 `delayed`；结束时间未知时标记为 `pending_confirmation`。
- 跨午夜依据受影响机场的本地时区判定。
- `event_id` 是幂等键。相同内容重试返回原结果；相同标识携带不同内容时返回 `409 event_conflict`。
- 事件链版本必须递增；新的根关闭版本号必须大于该机场既有最大版本。

### 校验失败与异常链隔离

- 任何结构或因果校验失败都在单个事务内回滚：不写入事件、影响，也不增加重放计数。`422 validation_error` 的 `details.errors` 逐项给出冲突的 `field`、稳定 `issue` 代码与对比时刻。
- 服务启动时对 SQLite 中的既有事件链做纯数据因果审计：时间倒置等异常链被标记为隔离（`events.quarantined`，并登记 `chain_anomalies` 台账）。
- 被隔离链不参与机场汇总与受影响航班查询，但其事件与影响仍可通过 `GET /api/v1/events/{event_id}` 只读追溯（`processing.state=quarantined`，附 `anomaly` 明细）；对隔离事件的重放不增加 `replay_count`。
- 隔离判定只依赖事件数据，不依赖运行时刻状态，因此容器重建后对同一卷必然得到相同判定；纠正底层数据后重启即自动解除隔离。`/healthz` 返回 `quarantined_chains` 计数。

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
