"""近期请求明细回溯：有界容量、顺序、并发不越界。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.config import MetricsSettings, Settings
from app.main import build_app
from app.logging_setup import configure_logging
import logging


def _client_with_capacity(capacity: int) -> TestClient:
    settings = Settings(metrics=MetricsSettings(recent_records_capacity=capacity))
    configure_logging()
    app = build_app(settings)
    for name in ("observability.access", "observability.diagnostic",
                 "observability.middleware", "observability.alert", "httpx"):
        logging.getLogger(name).disabled = True
    return TestClient(app)


def test_recent_is_newest_first_and_limit_respected(client: TestClient):
    print("\n[输入] 顺序请求 6 次，查询 /observability/recent?limit=3")
    rids = [f"seq-{i}" for i in range(6)]
    for rid in rids:
        client.get("/echo", params={"msg": "x"}, headers={"X-Request-ID": rid})
    body = client.get("/observability/recent", params={"limit": 3}).json()
    returned = [r["request_id"] for r in body["records"]]
    print(f"[判定] 返回={returned}, capacity={body['capacity']}")
    assert body["returned"] == 3
    assert returned == ["seq-5", "seq-4", "seq-3"], "明细须为 新->旧 顺序"


def test_recent_capacity_is_hard_bound_under_concurrency():
    capacity = 20
    c = _client_with_capacity(capacity)
    n = 300
    print(f"\n[输入] 容量上界={capacity}, {n} 个并发请求（15x 超发）")
    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(
            lambda i: c.get("/", headers={"X-Request-ID": f"cap-{i:04d}"}),
            range(n),
        ))
    body = c.get("/observability/recent", params={"limit": 200}).json()
    ids = [r["request_id"] for r in body["records"]]
    print(f"[判定] 返回条数={len(ids)}, capacity={body['capacity']}, 最新={ids[0]}")
    assert len(ids) == capacity, "明细数量不得超过容量上界"
    assert body["capacity"] == capacity
    assert len(set(ids)) == capacity, "缓冲内标识应互不相同（无重复写入）"
    # 并发下完成顺序不等于提交顺序：最新一条由服务端顺序保证为最后完成项，
    # 其余保留项只需确认全部来自本批请求且无越界。
    assert ids[0].startswith("cap-")
    assert all(0 <= int(x.split("-")[1]) < n for x in ids)
    # registry 内部内存结构同样不越界
    assert len(c.app.state.registry._recent) == capacity


def test_recent_contains_diagnostic_fields_without_stack(client: TestClient):
    """明细保留可供排查的诊断字段，但不含堆栈。"""
    print("\n[输入] 触发 1 次未捕获异常后查询明细")
    client.get("/demo/uncaught", headers={"X-Request-ID": "diag-1"})
    body = client.get("/observability/recent", params={"limit": 1}).json()
    rec = body["records"][0]
    print(f"[判定] 首条明细键={sorted(rec.keys())}, error_type={rec['error_type']}")
    assert rec["request_id"] == "diag-1"
    assert rec["endpoint"] == "/demo/uncaught"
    assert rec["status_code"] == 500
    assert rec["error_type"] == "uncaught_error"
    assert rec["duration_ms"] >= 0
    assert "Traceback" not in str(rec) and "secret" not in str(rec)
