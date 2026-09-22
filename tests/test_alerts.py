"""告警触发与不触发两类场景（确定性、可复现）。

判定依据输入：MetricsRegistry 的窗口样本（端点/状态码/耗时/时间戳）。
通过 /test/ingest 注入确定性记录，避免真实 sleep 造成的抖动。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.config import AlertSettings, MetricsSettings, RequestIdSettings, Settings
from app.errors import ErrorType
from app.main import build_app
from app.metrics import RequestRecord
from app.logging_setup import configure_logging

import logging


def _strict_client(settings: Settings) -> TestClient:
    """min_requests=5, 错误率阈值 0.5, p95 阈值 100ms 的确定性环境。"""
    configure_logging()
    app = build_app(settings)
    for name in ("observability.access", "observability.diagnostic",
                 "observability.middleware", "observability.alert", "httpx"):
        logging.getLogger(name).disabled = True

    @app.get("/test/ingest")
    def ingest(
        endpoint: str,
        status: int,
        duration_ms: float,
        age_ms: int = 0,
    ) -> dict:
        reg = app.state.registry
        now = time.time()
        rec = RequestRecord(
            request_id=f"ingest-{now}-{endpoint}-{status}-{duration_ms}-{age_ms}",
            endpoint=endpoint,
            method="GET",
            status_code=status,
            duration_ms=duration_ms,
            error_type=ErrorType.UNCAUGHT_ERROR if status >= 500 else None,
            timestamp=now - age_ms / 1000.0,
        )
        events = reg.record(rec, now=now)
        return {"alert_kinds": [e.kind for e in events]}

    return TestClient(app)


STRICT = Settings(
    request_id=RequestIdSettings(),
    alert=AlertSettings(
        window_seconds=60,
        min_requests=5,
        error_rate_threshold=0.5,
        latency_quantile=0.95,
        latency_threshold_ms=100.0,
    ),
    metrics=MetricsSettings(recent_records_capacity=50),
)


def test_error_rate_alert_triggers_at_threshold(client: TestClient):
    """5 个样本中 3 个 5xx（错误率 0.6 >= 0.5）：触发，且仅触发一次。"""
    c = _strict_client(STRICT)
    print("\n[输入] 同端点依次 2xx,2xx,500,500,500（错误率演化 0,0,0.33,0.5,0.6）")
    plan = [200, 200, 500, 500, 500]
    fired = []
    for i, status in enumerate(plan):
        r = c.get("/test/ingest", params={"endpoint": "/a", "status": status, "duration_ms": 10})
        kinds = r.json()["alert_kinds"]
        if kinds:
            fired.append((i, kinds))
    print(f"[判定] 触发点={fired}")
    assert fired == [(4, ["high_error_rate"])], fired
    alerts = c.get("/observability/alerts").json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["kind"] == "high_error_rate"
    assert alerts[0]["sample_size"] == 5 and alerts[0]["observed_value"] == 0.6
    assert "/a" in alerts[0]["message"] and "50%" in alerts[0]["message"]


def test_error_rate_alert_does_not_trigger_below_threshold(client: TestClient):
    """5 个样本中 2 个 5xx（错误率 0.4 < 0.5）：不触发。"""
    c = _strict_client(STRICT)
    print("\n[输入] 同端点 2xx,2xx,2xx,500,500（错误率演化至 0.4）")
    fired = []
    for status in (200, 200, 200, 500, 500):
        r = c.get("/test/ingest", params={"endpoint": "/b", "status": status, "duration_ms": 10})
        fired.extend(r.json()["alert_kinds"])
    print(f"[判定] 告警种类={fired}（期望为空）")
    assert fired == []
    assert c.get("/observability/alerts").json()["count"] == 0


def test_min_requests_prevents_alert_on_small_sample(client: TestClient):
    """样本数不足 min_requests：即使全错也不告警。"""
    c = _strict_client(STRICT)
    print("\n[输入] 同端点连续 4 个 500（min_requests=5）")
    fired = []
    for _ in range(4):
        r = c.get("/test/ingest", params={"endpoint": "/c", "status": 500, "duration_ms": 10})
        fired.extend(r.json()["alert_kinds"])
    print(f"[判定] 告警种类={fired}（期望为空，样本不足）")
    assert fired == []


def test_edge_trigger_does_not_repeat_while_condition_holds(client: TestClient):
    """阈值越界后持续保持：只产生一条事件（边沿触发）。"""
    c = _strict_client(STRICT)
    print("\n[输入] 同端点 10 个连续 500（条件持续越界）")
    fired = []
    for _ in range(10):
        r = c.get("/test/ingest", params={"endpoint": "/d", "status": 500, "duration_ms": 10})
        fired.extend(r.json()["alert_kinds"])
    print(f"[判定] 全部告警种类={fired}（期望仅 1 条 high_error_rate）")
    assert fired == ["high_error_rate"]


def test_latency_alert_triggers_and_healthy_does_not(client: TestClient):
    """p95>=100ms 触发延迟告警；全快请求不触发。"""
    c = _strict_client(STRICT)
    print("\n[输入-触发组] 4 个 10ms + 1 个 150ms, p95(nearest-rank,n=5)=150ms")
    fired = []
    for dur in (10, 10, 10, 10, 150):
        r = c.get("/test/ingest", params={"endpoint": "/slow", "status": 200, "duration_ms": dur})
        fired.extend(r.json()["alert_kinds"])
    print(f"[判定] 慢端点告警={fired}")
    assert fired == ["high_latency"]

    print("[输入-不触发组] 另一健康端点 10 个 10ms")
    healthy = []
    for _ in range(10):
        r = c.get("/test/ingest", params={"endpoint": "/fast", "status": 200, "duration_ms": 10})
        healthy.extend(r.json()["alert_kinds"])
    print(f"[判定] 健康端点告警={healthy}（期望为空）")
    assert healthy == []


def test_alerts_are_scoped_per_endpoint(client: TestClient):
    """一个端点越界不影响另一个端点。"""
    c = _strict_client(STRICT)
    print("\n[输入] /bad 连续 6 个 500；/good 连续 6 个 200")
    bad, good = [], []
    for _ in range(6):
        bad += c.get("/test/ingest", params={"endpoint": "/bad", "status": 500, "duration_ms": 10}).json()["alert_kinds"]
        good += c.get("/test/ingest", params={"endpoint": "/good", "status": 200, "duration_ms": 10}).json()["alert_kinds"]
    print(f"[判定] /bad={bad}, /good={good}")
    assert bad == ["high_error_rate"] and good == []
    alerts = c.get("/observability/alerts").json()["alerts"]
    assert all(a["endpoint"] == "/bad" for a in alerts)


def test_concurrent_error_storm_triggers_single_alert(client: TestClient):
    """高并发错误风暴下结论稳定：仅一条边沿告警，且明细计数与并发数一致。"""
    c = _strict_client(STRICT)
    n = 40
    print(f"\n[输入] {n} 个并发请求打到真实 /demo/uncaught（全部 500）")

    def one(i: int):
        return c.get("/demo/uncaught", headers={"X-Request-ID": f"storm-{i:03d}"})

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(one, range(n)))
    statuses = sorted(r.status_code for r in results)
    print(f"[判定] 状态码分布 500x{statuses.count(500)}")
    assert statuses == [500] * n
    alerts = c.get("/observability/alerts").json()["alerts"]
    kinds = [a["kind"] for a in alerts]
    print(f"[判定] 告警事件={kinds}（期望恰好 1 条 high_error_rate，边沿触发）")
    assert kinds == ["high_error_rate"]
    # 边沿触发时窗口样本数至少为 min_requests；全部 40 个请求都须进入窗口统计
    assert alerts[0]["sample_size"] >= 5
    metrics = c.get("/observability/metrics", params={"endpoint": "/demo/uncaught"}).json()["metrics"]
    print(f"[判定] 触发时样本数={alerts[0]['sample_size']}, 最终窗口总数={metrics['total']}")
    assert metrics["total"] == n
    assert metrics["by_status"]["500"] == n
