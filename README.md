# FastAPI 请求级可观测性

为每个 HTTP 请求提供**关联标识（Request ID）**、**结构化请求记录与指标汇总**、
以及**基于阈值的告警与有界回溯明细**。在异常与高并发下给出一致、可解释的结果，
且不向响应体泄露内部堆栈或实现细节。

## 快速开始

```bash
# 需要 Python 3.14+；依赖通过 uv 安装（见 pyproject.toml）
uv venv
uv pip install -p .venv/bin/python "fastapi[standard]>=0.141.1" pytest httpx

# 启动服务
.venv/bin/uvicorn main:app --reload
```

## 1. 请求关联标识（X-Request-ID）

### 接收与生成规则

| 入站 `X-Request-ID` | 处理规则 | 响应 |
| --- | --- | --- |
| 未提供（头缺失或纯空白） | **生成**新标识：`req-` + uuid4 的 32 位十六进制（如 `req-3f25...`，总长 36） | `200`，响应头回传生成值 |
| 合法（`^[A-Za-z0-9][A-Za-z0-9._-]*$`，长度 1–128） | **原样**使用，不重写 | `200`，响应头原样回传 |
| 非法（含空格/控制字符等、或超长） | **拒绝**，不生成替代标识 | `400 invalid_request_id`，响应头不带标识 |

> 规则全部集中在 `app/config.py:RequestIdSettings`，属确定性规则；
> uuid4 保证并发生成不碰撞。

### 传播与并发隔离

- 标识在请求入口绑定到 [`contextvars.ContextVar`](app/request_id.py)，
  请求内的日志、错误响应、路由/内部调用通过 `get_request_id()` 读到的都是同一个值；
- asyncio 为每个任务复制上下文，**并发请求之间互不串扰**（测试：`tests/test_request_id_concurrency.py`，40 并发×3 组场景）；
- 每条结构化日志都带 `request_id` 字段；错误响应信封顶层也带 `request_id`。

### 跨服务链路传播（出站）

业务代码发起下游调用前，可从 `app/propagation.py` 直接拿到可**原样携带**的请求头，
把本次请求标识透传给下游；下游响应回传它那一次的标识时回收并关联到本次请求：

```python
from app.propagation import outbound_headers, capture_downstream_id
import httpx

async with httpx.AsyncClient() as client:
    resp = await client.get("http://downstream/ping", headers=outbound_headers())
    capture_downstream_id(resp)   # 下游标识落入本请求上下文与结构化记录
```

- **默认开启**，传播头默认仍为 `X-Request-ID`（可在 `PropagationSettings` 配置）；
- 调第三方不希望外发时可关闭：环境变量 `OBS_PROPAGATION_ENABLED=false`，
  或在请求内临时 `set_propagation_scope(False)`（务必 `reset_propagation_scope` 复位）；
- 下游回传的标识写入本次 `RequestRecord.downstream_request_id`，
  日志额外带 `downstream_request_id` 字段，一条链路可回溯到两头；
- 开关与回收状态全部绑定 `ContextVar`，asyncio 按任务复制上下文，
  **并发请求各归各**（测试：`tests/test_propagation.py`，含 40 协程/30 真实 HTTP 并发隔离）。

本地验证（进程内下游，不依赖任何外部服务）：

```bash
curl -s -H 'X-Request-ID: trace-on-1' \
  http://127.0.0.1:8000/demo/downstream-call | python -m json.tool   # 发出并沿用同标识
curl -s -H 'X-Request-ID: trace-off-1' \
  'http://127.0.0.1:8000/demo/downstream-call?propagate=false' | python -m json.tool
```

### 入口被拒请求（如非法 `X-Request-ID`）

入口即被挡掉的请求不再"像没来过"：服务端为每次拒绝**生成一条本次专属标识**
（同样为 `req-<32hex>` 规则），并做到：

- 响应头 `X-Request-ID` 与错误信封顶层 `request_id` 均为该标识（原始非法值不回显）；
- 拒绝原因（空/超长/非法字符）写入响应 `detail` 与 warning 级结构化日志；
- 拿该标识可在近期回溯明细中查到记录：
  `GET /observability/recent?request_id=req-...`（记录带 `rejected=true` 与 `reject_reason`）；
- 对外拒绝规则与状态码不变：仍是 `400 invalid_request_id`。

容量隔离：被拒请求进入**独立的有界环形缓冲**（`rejected_records_capacity`，
默认 200，可用 `OBS_METRICS_REJECTED_CAPACITY` 覆盖），与正常请求明细
（默认 200）各自硬上界。扫描器乱打时既不会顶掉正常请求明细，
也不会让内存无限制增长（测试：120 并发非法请求验证容量=10 的硬上界）。

```bash
curl -i http://127.0.0.1:8000/echo?msg=hi                 # 缺失 -> 自动生成
curl ... -H 'X-Request-ID: caller-abc.1' /echo?msg=hi    # 合法 -> 原样传播
curl ... -H 'X-Request-ID: bad id!' /                    # 非法 -> 400
```

## 2. 结构化记录、错误分类与指标汇总

### 统一错误信封

所有错误（包括未捕获异常）均返回：

```json
{
  "request_id": "req-...",
  "error": { "type": "uncaught_error", "message": "服务内部错误", "detail": null }
}
```

- 内部异常文本、主机/端口、堆栈等**只写入服务端诊断日志**（JSON，含 `exc_info`），绝不进入响应体；
- `detail` 只承载安全信息（如校验字段名、非法标识原因）。

### 错误分类

| 错误类型 `error.type` | HTTP 状态码 | 触发情形 |
| --- | --- | --- |
| `invalid_request_id` | 400 | 入站标识非法 |
| `validation_error` | 422 | 请求参数/请求体校验失败（FastAPI） |
| `not_found` | 404 | 路由不存在 |
| `business_error` | 422 | 业务异常 `BusinessError` |
| `dependency_error` | 502 | 下游依赖异常 `DependencyError` |
| `timeout_error` | 504 | 超时类异常（如 `asyncio.TimeoutError`） |
| `rate_limited` | 429 | 限流 `RateLimitedError` |
| `network_error` | 502 | 网络／系统级故障：连接被拒/中断/中止、管道破裂、DNS/SSL 故障、底层系统调用失败（`ConnectionRefusedError`、`ConnectionResetError`、`OSError` 等） |
| `uncaught_error` | 500 | 其余一切未捕获异常（兜底归类，可区分、可统计） |

归类逻辑见 `app/errors.py:classify_exception`（先匹配已知基类，再匹配超时命名，
再按网络/系统级异常类型名与标准库基类识别 `network_error`，其余一律兜底为 `uncaught_error`）。
超时类（`TimeoutError` 同时也是 `OSError` 子类）优先归入 `timeout_error`，语义边界稳定。
`network_error` 对外只有固定文案"网络或系统级故障"，内部主机/端口/errno/异常原文
只进诊断日志，绝不进入响应体；指标的 `by_error_type` 可将其与 `uncaught_error`、
`dependency_error` 区分。演示路由：`/demo/network-error`、`/demo/system-error`。

### 指标维度

每次请求都会产生一条 `RequestRecord`（端点、方法、状态码、耗时、错误类型、时间戳、标识）。
按**端点**汇总（`GET /observability/metrics`）：

- `by_status`：状态码计数；`by_error_type`：错误类型计数；
- `by_latency_bucket`：耗时区间计数，区间为左闭右开
  `0-50ms / 50-100ms / 100-200ms / 200-500ms / 500-1000ms / 1000ms+`；
- `error_rate`：滚动窗口内 **5xx 占比**（4xx 不计入服务错误率）；
- `latency_p50_ms / p95_ms / p99_ms`：nearest-rank 分位数；
- `total`：当前窗口样本数；`cumulative_total`：进程累计总量。
- 未匹配路由归入固定桶 `__unmatched__`，非法标识 400 归入 `__invalid_request_id__`，
  避免以路径为标签导致的高基数问题。

## 3. 告警与有界回溯明细

### 阈值语义（默认值，可环境变量覆盖）

| 配置 | 默认 | 语义 |
| --- | --- | --- |
| `window_seconds` | `60` | 只统计最近 60s 滚动窗口内的样本 |
| `min_requests` | `5` | 窗口样本数 < 5 时**不告警**（防小样本噪声） |
| `error_rate_threshold` | `0.5` | 窗口内 5xx 占比 **`>=` 50%** 触发 `high_error_rate` |
| `latency_quantile` | `0.95` | 延迟分位（p95） |
| `latency_threshold_ms` | `500` | p95 **`>=` 500ms** 触发 `high_latency` |

- 比较一律为 **`>=`（含阈值本身）**；
- **边沿触发**：条件从"不满足"变为"满足"的当次请求产生且只产生一条 `AlertEvent`；
  条件持续越界不重复告警；当错误率/延迟回落至阈值以下，或整个统计窗口滚动过去后，
  告警态复位，下次越界可再次触发；
- 两类告警、每个端点独立判定，互不影响；
- 事件本身可解释：含端点、观测值、阈值、窗口、样本数、触发请求标识与中文说明，
  可经 `GET /observability/alerts` 查看，并写入 warning 级日志。

环境变量覆盖：`OBS_ALERT_WINDOW_SECONDS`、`OBS_ALERT_MIN_REQUESTS`、
`OBS_ALERT_ERROR_RATE_THRESHOLD`、`OBS_ALERT_LATENCY_THRESHOLD_MS`、
`OBS_METRICS_RECENT_CAPACITY`、`OBS_METRICS_REJECTED_CAPACITY`、
`OBS_PROPAGATION_ENABLED`。

### 有界回溯明细

`GET /observability/recent?limit=20` 返回**新 -> 旧**的近期请求明细，
容量由 `recent_records_capacity`（默认 **200**）硬性上界；被入口拒绝的请求
使用**独立**缓冲 `rejected_records_capacity`（默认 **200**），两类流量互不挤占。
环形缓冲满后自动淘汰最老记录；告警事件同样有 100 条上界。
因此长期运行下内存占用恒定，**不会无限制增长**（测试：300 并发灌入容量 20
的正常缓冲、120 并发非法请求灌入容量 10 的被拒缓冲）。

查询参数：

- `request_id=req-...`：按标识精确回溯（正常与被拒记录统一可查）；
- `include_rejected=false`：只看正常请求明细；
- 响应同时回传 `capacity` 与 `rejected_capacity`，便于核对硬上界。

## 可观测性查询端点

| 端点 | 说明 |
| --- | --- |
| `GET /observability/metrics[?endpoint=/echo]` | 全部或指定端点的指标汇总 |
| `GET /observability/recent?limit=20&request_id=...&include_rejected=true` | 近期请求明细（有界，新 -> 旧；可按标识查，含被拒记录） |
| `GET /observability/alerts` | 告警事件列表（有界） |

另有演示路由用于本地验证：`/demo/business-error`、`/demo/dependency-error`、
`/demo/uncaught`、`/demo/timeout`、`/demo/network-error`、`/demo/system-error`、
`/demo/downstream-call[?propagate=false]`、`/demo/slow?ms=600`。

## 本地验证方法

```bash
# 全部单测（含出站传播与并发隔离、被拒请求标识贯穿与容量隔离、
# 网络/系统级故障归类、并发标识隔离、异常归类、告警触发/不触发、容量上界）
.venv/bin/python -m pytest -q

# 查看每个用例打印的"输入/判定依据"
.venv/bin/python -m pytest -s

# 手工验证（先启动 uvicorn）
curl -s http://127.0.0.1:8000/observability/metrics | python -m json.tool
for i in $(seq 1 6); do curl -s -o /dev/null -H "X-Request-ID: load-$i" \
  http://127.0.0.1:8000/demo/uncaught; done
curl -s http://127.0.0.1:8000/observability/alerts | python -m json.tool

# 跨服务链路（进程内下游）：默认传播 vs 临时关闭
curl -s -H 'X-Request-ID: chain-1' \
  http://127.0.0.1:8000/demo/downstream-call | python -m json.tool
# 被拒请求：记下响应头标识后回溯
curl -si http://127.0.0.1:8000/ -H 'X-Request-ID: bad id!'
curl -s 'http://127.0.0.1:8000/observability/recent?request_id=req-xxxx' | python -m json.tool
# 网络/系统级故障归类
curl -si -H 'X-Request-ID: net-1' http://127.0.0.1:8000/demo/network-error
```

### 测试稳定性说明

- 告警测试通过确定性的记录注入端点喂入固定状态码/耗时/时间戳，
  不依赖真实 sleep，结论可复现；另有 40 并发真实错误风暴验证边沿告警只产生一次。
- 并发用例断言的是标识集合/状态分布等与调度无关的性质，不假设线程完成顺序。
- 全部 36 个用例可重复连跑（已验证 5 轮以上结果一致）。

## 代码结构

```
app/
  config.py              # 标识规则 / 窗口 / 阈值默认配置（支持环境变量覆盖）
  request_id.py          # 标识校验、生成、ContextVar 传播
  propagation.py         # 出站传播头构造、开关、下游标识回收（ContextVar 隔离）
  errors.py              # 错误类型、异常归类、安全错误信封
  ring_buffer.py         # 线程安全、有容量上界的环形缓冲
  metrics.py             # 滚动窗口聚合、分位数、延迟桶、边沿告警判定
  logging_setup.py       # 带 request_id 的 JSON 结构化日志
  middleware.py          # ASGI 中间件：标识接收/拒绝/传播、计时、兜底异常
  exception_handlers.py  # 404/422/业务异常统一信封
  main.py                # 应用装配、演示路由、可观测性查询端点
tests/                   # 并发隔离 / 异常归类 / 告警触发与不触发 / 容量上界
```

## 兼容性

- 原有 `GET /` 的状态码（200）与响应体（`{"Hello": "World"}`）保持不变
  （`tests/test_error_classification.py::test_root_compatibility`）。
- FastAPI 默认 404/422 状态码保持不变，仅响应体统一为错误信封格式。
