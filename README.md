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

```bash
curl -i http://127.0.0.1:8000/echo?msg=hi                 # 缺失 -> 自动生成
curl ... -H 'X-Request-ID: caller-abc.1' /echo?msg=hi    # 合法 -> 原样传播
curl ... -H 'X-Request-ID: bad id!' /                    # 非法 -> 400
```

### 跨服务链路（出站传播，默认开启）

业务代码在发起对下游的调用之前，可直接拿到一份**可原样带过去**的请求头：

```python
from app.propagation import build_outbound_headers, capture_downstream_response

async def call_downstream(http_client):
    headers = build_outbound_headers()          # -> {"X-Request-ID": 本次标识}
    resp = await http_client.get(downstream_url, headers=headers)
    # 下游若在响应头 X-Downstream-Request-ID 回它那一次的标识，回收并关联到本次请求
    capture_downstream_response(resp.headers, downstream="order-service")
```

- 默认**开启**；全局可关（调第三方时不外发）：环境变量
  `OBS_OUTBOUND_PROPAGATION_ENABLED=false`，或构造应用时传入
  `RequestIdSettings(outbound_propagation_enabled=False)`；
  单次调用也可用 `build_outbound_headers(enabled=False)` 临时覆盖；
- 关闭或在请求上下文外调用时返回 `{}`，不抛异常；
- 下游回传的标识存于同一 `ContextVar`，写入访问日志的 `downstream_ids` 字段，
  并随本次请求落进回溯明细的 `downstream_ids`（下游名 -> 标识）；
  asyncio 每任务复制上下文，**并发请求各归各，互不串扰**；
- 下游响应头名可用 `RequestIdSettings.downstream_id_header_name` 配置
  （默认 `X-Downstream-Request-ID`）。

本地验证（不依赖任何外部服务，路由内置了一个模拟下游）：

```bash
curl -s -H 'X-Request-ID: chain-1' \
  http://127.0.0.1:8000/demo/downstream-call | python -m json.tool
# outbound_headers 携带 chain-1，captured_downstream_id=down-chain-1
curl -s 'http://127.0.0.1:8000/demo/downstream-call?propagate=false'  # 本次不外发
curl -s 'http://127.0.0.1:8000/observability/recent?request_id=chain-1' | python -m json.tool
```

### 入口被挡请求（非法标识 400）也可追溯

非法标识请求原来对外的拒绝规则与状态码**完全不变**（`400`，
`error.type=invalid_request_id`，响应头仍不带 `X-Request-ID`），但现在会：

- 生成一条本次专属标识（`rej-` + 32 位十六进制），放进**响应体**
  `request_id` 字段；拒绝原因在 `error.detail`；
- 该标识同样绑定上下文写入结构化日志（`rejected_request_id` 字段）；
- 记录进入**独立的有界缓冲**，拿标识可在近期明细里对账：
  `GET /observability/recent?request_id=rej-xxxx`，记录含
  `rejected=true` 与 `reject_reason`；也可用 `?include=rejected` 只看被挡明细。

被扫爆时容量安全：被挡请求与正常请求使用**两个相互独立**的环形缓冲，
容量各自为硬上界（默认各 200，被挡容量可经
`OBS_METRICS_RECENT_REJECTED_CAPACITY` 调整）。被挡洪峰不会顶掉正常请求
明细、不会撑爆容量，内存占用恒定。

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
| `network_system_error` | 502 | 网络/系统级故障：连接被拒/中断、DNS、底层系统调用失败等 |
| `uncaught_error` | 500 | 其余一切未捕获异常（兜底归类，可区分、可统计） |

归类逻辑见 `app/errors.py:classify_exception`（先匹配已知基类，再匹配超时命名，
其余一律兜底为 `uncaught_error`）。

`network_system_error` 的识别边界（稳定、可复现）：

- 命中内置连接类异常基类 `ConnectionError`：`ConnectionRefusedError`
  （连接被拒）、`ConnectionResetError`/`ConnectionAbortedError`（连接中断）、
  `BrokenPipeError` 等；
- 或命中标转库/异步库的具体连接类异常名：`socket.gaierror`/`SSLError`、
  http.client 的 `RemoteDisconnected`、urllib3 的 `NewConnectionError`/
  `MaxRetryError`、asyncio 的 `ClientConnectorError`/`ServerDisconnectedError` 等；
- **超时不归入本类**（仍是 `timeout_error`/504）；普通文件类 `OSError`
  （如 `FileNotFoundError`）与其它未知异常仍兜底为 `uncaught_error`/500，
  避免把本地代码问题误判成依赖故障；
- 对外仍是固定安全文案"网络或系统错误"，`detail=null`：主机、端口、
  errno、异常原文一律不进响应体，只进服务端诊断日志；
  归类结果进入访问日志与 `by_error_type`/`by_status`（502 计入 5xx 错误率）。

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
`OBS_METRICS_RECENT_CAPACITY`、`OBS_METRICS_RECENT_REJECTED_CAPACITY`、
`OBS_OUTBOUND_PROPAGATION_ENABLED`。

### 有界回溯明细

`GET /observability/recent?limit=20` 返回**新 -> 旧**的近期请求明细，
容量由 `recent_records_capacity`（默认 **200**）硬性上界：
环形缓冲满后自动淘汰最老记录；告警事件同样有 100 条上界。
因此长期运行下内存占用恒定，**不会无限制增长**（测试：300 并发灌入容量 20 的缓冲）。
入口被挡请求使用独立缓冲 `recent_rejected_capacity`（默认 **200**，
`OBS_METRICS_RECENT_REJECTED_CAPACITY` 覆盖），洪峰不影响正常明细。

## 可观测性查询端点

| 端点 | 说明 |
| --- | --- |
| `GET /observability/metrics[?endpoint=/echo]` | 全部或指定端点的指标汇总 |
| `GET /observability/recent?limit=20&include=normal&request_id=` | 近期明细（有界，新 -> 旧）；`include` 取 `normal`/`rejected`/`all`；`request_id` 跨缓冲按标识对账 |
| `GET /observability/alerts` | 告警事件列表（有界） |

另有演示路由用于本地验证：`/demo/business-error`、`/demo/dependency-error`、
`/demo/uncaught`、`/demo/timeout`、`/demo/slow?ms=600`、
`/demo/downstream-call[?propagate=false]`（跨服务链路，内置模拟下游）、
`/demo/network-error`（连接被拒）、`/demo/system-error`（连接中断）。

```bash
# 被拒请求追踪：400 响应体 request_id 即 rej- 标识，用它对账
curl -s -H 'X-Request-ID: bad id!' http://127.0.0.1:8000/
curl -s 'http://127.0.0.1:8000/observability/recent?include=rejected' | python -m json.tool

# 网络/系统级故障归类（502 network_system_error，响应体无主机端口）
curl -s http://127.0.0.1:8000/demo/network-error | python -m json.tool
```

## 本地验证方法

```bash
# 全部单测（含并发标识隔离、异常归类、告警触发/不触发、容量上界）
.venv/bin/python -m pytest -q

# 查看每个用例打印的"输入/判定依据"
.venv/bin/python -m pytest -s

# 手工验证（先启动 uvicorn）
curl -s http://127.0.0.1:8000/observability/metrics | python -m json.tool
for i in $(seq 1 6); do curl -s -o /dev/null -H "X-Request-ID: load-$i" \
  http://127.0.0.1:8000/demo/uncaught; done
curl -s http://127.0.0.1:8000/observability/alerts | python -m json.tool
```

### 测试稳定性说明

- 告警测试通过确定性的记录注入端点喂入固定状态码/耗时/时间戳，
  不依赖真实 sleep，结论可复现；另有 40 并发真实错误风暴验证边沿告警只产生一次。
- 并发用例断言的是标识集合/状态分布等与调度无关的性质，不假设线程完成顺序。
- 全部 36 个用例（含出站传播构造/并发隔离/开关关闭、被拒请求标识贯穿与
  容量洪峰、网络/系统级故障分类）可重复连跑（多轮验证结果一致）。

## 代码结构

```
app/
  config.py              # 标识规则 / 窗口 / 阈值默认配置（支持环境变量覆盖）
  request_id.py          # 标识校验、生成、ContextVar 传播
  propagation.py         # 出站传播头构造、下游标识回收（ContextVar 隔离）
  errors.py              # 错误类型、异常归类、安全错误信封
  ring_buffer.py         # 线程安全、有容量上界的环形缓冲
  metrics.py             # 滚动窗口聚合、分位数、延迟桶、边沿告警判定
  logging_setup.py       # 带 request_id 的 JSON 结构化日志
  middleware.py          # ASGI 中间件：标识接收/拒绝/传播、被拒追踪、计时、兜底异常
  exception_handlers.py  # 404/422/业务异常统一信封
  main.py                # 应用装配、演示路由、可观测性查询端点
tests/                   # 并发隔离 / 出站传播 / 被拒追踪 / 网络故障归类 /
                         # 异常归类 / 告警触发与不触发 / 容量上界
```

## 兼容性

- 原有 `GET /` 的状态码（200）与响应体（`{"Hello": "World"}`）保持不变
  （`tests/test_error_classification.py::test_root_compatibility`）。
- FastAPI 默认 404/422 状态码保持不变，仅响应体统一为错误信封格式。
