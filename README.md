# FastAPI 请求级可观测性

每个请求都可被追踪、记录与汇总：请求关联标识贯穿日志/错误响应/内部调用，
未捕获异常先归类再对外返回，指标按端点聚合并驱动可解释的告警，
近期请求明细保留在**有硬上界**的缓冲中供回溯。

## 快速开始

```bash
uv sync --group dev          # 安装依赖（含 pytest）
uv run fastapi dev main.py   # 启动服务（或 uv run uvicorn main:app）
uv run pytest                # 运行全部测试
```

手工验证示例：

```bash
curl -i http://127.0.0.1:8000/                       # 兼容接口，响应带 X-Request-ID
curl -i -H 'X-Request-ID: caller-demo-0001' http://127.0.0.1:8000/echo/request-id
curl -i -H 'X-Request-ID: bad!' http://127.0.0.1:8000/echo/request-id   # 400 拒绝
curl -s http://127.0.0.1:8000/error/unhandled        # 500，归类为 internal_error
curl -s http://127.0.0.1:8000/internal/metrics       # 指标汇总
curl -s http://127.0.0.1:8000/internal/recent        # 近期请求明细
curl -s http://127.0.0.1:8000/internal/alerts        # 告警事件
```

## 请求关联标识（X-Request-ID）

**接收规则（确定性）：**

| 调用方提供 | 判定 | 行为 |
| --- | --- | --- |
| 未提供 / 空串 / 纯空白 | 视为未提供 | 服务端生成 `req-<uuid4 的 32 位十六进制>`（共 36 字符） |
| 合法 | 原样采用 | 不做大小写折叠或改写 |
| 非法 | **拒绝** | 返回 400 `invalid_request_id`，不做静默替换 |

合法格式：8~128 字符，首字符为字母或数字，其余字符只能是字母、数字、
`_`、`-`（正则 `^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$`）。该规则同时杜绝了
换行符等进入日志行与响应头（防日志注入）。

**传播与隔离：**

* 标识保存在 `contextvars.ContextVar` 中，`asyncio` 为每个任务复制上下文，
  并发请求之间天然隔离（由 `tests/test_concurrency.py` 验证）；
* 所有响应（含错误响应）都回写 `X-Request-ID` 头，调用方同名头会被覆盖，
  杜绝伪造与重复；
* 内部调用使用 `app.context.downstream_headers()` 把当前标识透传给下游
  （演示见 `GET /internal/downstream-call`）；
* 非法标识被拒绝时，服务端仍生成一个 `req-` 诊断标识，贯穿该次拒绝的
  日志、错误响应与指标记录，保证可排查。

## 错误分类

所有未捕获异常先归类为下表中的**可区分错误类型**，再对外返回统一结构：

```json
{ "error": { "category": "internal_error", "message": "internal server error",
             "request_id": "req-..." } }
```

| category | HTTP | 来源 |
| --- | --- | --- |
| `invalid_request_id` | 400 | 非法的 X-Request-ID |
| `validation_error` | 422 | 请求参数/体校验失败 |
| `not_found` | 404 | 路由未匹配 |
| `upstream_error` | 502 | 下游依赖失败（含 `ConnectionError`/`OSError`） |
| `timeout` | 504 | 下游超时（`TimeoutError`/`asyncio.TimeoutError`） |
| `internal_error` | 500 | 其余一切未捕获异常 |

**安全保证：** 响应体只含类别、安全文案与 `request_id`；异常类型名、
原始消息、堆栈**只写入服务端日志**（JSON 行，含同一 `request_id`），
绝不回传给调用方。`message` 字段对内部错误固定为
`internal server error`，防止 `KeyError: 'xxx'` 之类细节外泄。

## 指标与告警

**指标**按端点（路由模板）聚合：总量、按状态码、按耗时区间
（`0-50ms / 50-100ms / 100-200ms / 200-500ms / 500ms+`）、按错误类型、
服务端错误数与平均耗时。未匹配路由聚合到 `unmatched`，路由前拒绝
聚合到 `-`，避免任意路径打散指标。

**告警阈值语义**（`app.metrics.DEFAULT_RULE`）：

* 评估窗口：该端点最近 100 条请求；
* `min_samples = 10`：窗口内样本不足 10 条不评估（冷启动保护）；
* `error_rate_threshold = 0.5`：窗口内**服务端错误（状态码 ≥ 500）占比
  严格大于** 50% 触发 `error_rate` 告警（恰好等于不触发）；
* `slow_latency_ms = 200`、`slow_rate_threshold = 0.5`：窗口内
  延迟 ≥ 200ms 的请求占比严格大于 50% 触发 `latency` 告警；
* 冷却：同一端点同一类告警 60 秒内不重复产生；
* 每条告警事件包含观测值、阈值、样本量、触发请求标识与可读消息，
  通过 `GET /internal/alerts` 查询，并以 warning 日志输出。

**资源上界：** 近期明细保存在全局 FIFO 缓冲（默认容量 1000 条，
构造 `MetricsRegistry(capacity=...)` 可调），告警历史默认保留 100 条，
长期运行内存占用恒定。

## 接口一览

| 方法/路径 | 说明 |
| --- | --- |
| `GET /` | 原有接口，响应体保持 `{"Hello": "World"}` |
| `GET /echo/request-id` | 回显标识及来源 |
| `GET /error/{upstream,timeout,unhandled}` | 错误归类演示 |
| `GET /slow?ms=N` | 慢请求演示（0 ≤ N ≤ 10000） |
| `GET /internal/downstream-call` | 下游传播头演示 |
| `GET /internal/isolation-probe` | 并发隔离探针（测试用） |
| `GET /internal/metrics` | 指标汇总 |
| `GET /internal/recent?endpoint=&limit=` | 近期请求明细 |
| `GET /internal/alerts?limit=` | 告警事件 |

## 本地验证

```bash
uv run pytest            # 全部测试（并发隔离 / 异常归类 / 告警触发与不触发）
uv run pytest -s         # 打印每个用例的输入与判定依据
```

测试覆盖：并发下标识隔离（64 并发交错 + 混合合法/非法/缺失标识）、
异常归类表与安全响应、指标聚合与容量上界、告警触发/不触发/冷却/
恢复/按端点隔离等场景，结论稳定可复现。
