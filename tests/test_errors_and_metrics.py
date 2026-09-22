"""异常归类、安全错误响应、指标聚合与有界明细。"""
from __future__ import annotations

import pytest

from app.context import REQUEST_ID_HEADER
from app.errors import ErrorCategory, classify_exception
from app.metrics import LatencyBucket, MetricsRegistry, RequestRecord


# --------------------------------------------------------------------------- #
# 异常归类单元测试：打印输入与判定依据
# --------------------------------------------------------------------------- #
def test_exception_classification_table() -> None:
    import asyncio

    cases = [
        (ValueError("x"), ErrorCategory.INTERNAL_ERROR, 500),
        (TimeoutError(), ErrorCategory.TIMEOUT, 504),
        (asyncio.TimeoutError(), ErrorCategory.TIMEOUT, 504),
        (ConnectionError("down"), ErrorCategory.UPSTREAM_ERROR, 502),
        (OSError("io"), ErrorCategory.UPSTREAM_ERROR, 502),
    ]
    for exc, expect_cat, expect_status in cases:
        got = classify_exception(exc)
        print(f"\n[input] {type(exc).__name__}: {exc} -> 判定类别={got.value}")
        print(f"[判定依据] 期望={expect_cat.value}/{expect_status} 实际={got.value}")
        assert got == expect_cat


def test_error_response_never_leaks_internals(client) -> None:
    print("\n[input] GET /error/unhandled 触发 RuntimeError('simulated ...')")
    r = client.get(
        "/error/unhandled", headers={"X-Request-ID": "caller-leak-check-1"}
    )
    print(f"[判定] status={r.status_code} body={r.text}")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["category"] == "internal_error"
    assert body["error"]["message"] == "internal server error"
    # 不得回传异常类型名、堆栈、原始消息等实现细节
    assert "RuntimeError" not in r.text
    assert "simulated" not in r.text
    assert "Traceback" not in r.text
    # 但保留诊断标识
    assert body["error"]["request_id"] == "caller-leak-check-1"
    assert r.headers[REQUEST_ID_HEADER.lower()] == "caller-leak-check-1"


@pytest.mark.parametrize(  # type: ignore[misc]
    "path,expected_status,expected_category",
    [
        ("/error/upstream", 502, "upstream_error"),
        ("/error/timeout", 504, "timeout"),
        ("/error/unhandled", 500, "internal_error"),
        ("/missing-path", 404, "not_found"),
    ],
)
def test_classified_error_endpoints(
    client, path, expected_status, expected_category
) -> None:
    print(f"\n[input] GET {path} -> 期望 {expected_status}/{expected_category}")
    r = client.get(path, headers={"X-Request-ID": "caller-classify-01"})
    print(f"[判定] status={r.status_code} body={r.json()}")
    assert r.status_code == expected_status
    assert r.json()["error"]["category"] == expected_category
    assert r.json()["error"]["request_id"]


def test_validation_error_is_classified(client) -> None:
    print("\n[input] GET /slow?ms=99999（超出 ge/le 约束）-> 期望 422")
    r = client.get("/slow?ms=99999")
    print(f"[判定] status={r.status_code} body={r.json()}")
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "validation_error"


# --------------------------------------------------------------------------- #
# 指标聚合
# --------------------------------------------------------------------------- #
def test_metrics_aggregated_by_endpoint_status_and_latency(client, registry) -> None:
    print("\n[input] / 3 次, /slow?ms=10 2 次, /error/upstream 1 次")
    for _ in range(3):
        client.get("/", headers={"X-Request-ID": "metric-root-0001"})
    for _ in range(2):
        client.get("/slow?ms=10")
    client.get("/error/upstream")

    m = client.get("/internal/metrics").json()["endpoints"]
    print(f"[判定] / total={m['/']['total']} status={m['/']['by_status_code']}")
    print(f"[判定] /slow total={m['/slow']['total']} buckets={m['/slow']['by_latency_bucket']}")
    print(f"[判定] /error/upstream={m['/error/upstream']['by_error_category']}")
    assert m["/"]["total"] == 3
    assert m["/"]["by_status_code"] == {"200": 3}
    assert m["/slow"]["total"] == 2
    # 耗时桶断言不依赖墙钟：从实际记录的耗时推导期望桶，
    # 避免机器负载导致 10ms 睡眠越界造成的偶发失败。
    slow_records = [r for r in registry.recent(endpoint="/slow", limit=10)]
    assert len(slow_records) == 2
    for rec in slow_records:
        expected_bucket = LatencyBucket.for_latency(rec["latency_ms"]).value
        print(f"[判定] /slow 实测 {rec['latency_ms']}ms -> 期望桶 {expected_bucket}")
    assert sum(m["/slow"]["by_latency_bucket"].values()) == 2
    assert m["/error/upstream"]["server_error_total"] == 1
    assert m["/error/upstream"]["by_error_category"] == {"upstream_error": 1}


def test_recent_records_have_hard_capacity(small_registry) -> None:
    print(f"\n[input] 向容量={small_registry.capacity} 的注册表写入 100 条记录")
    for i in range(100):
        small_registry.record(
            RequestRecord(f"req-cap-{i:04d}", "/e", 200, 1.0, None, float(i))
        )
    items = small_registry.recent(limit=10_000)
    print(f"[判定] 保留条数={len(items)}（期望 25），最早保留={items[0]['request_id']}")
    assert len(items) == small_registry.capacity
    # FIFO 淘汰：只保留最后 25 条
    assert items[0]["request_id"] == "req-cap-0075"
    assert items[-1]["request_id"] == "req-cap-0099"
    # 再写入也不会突破上界
    small_registry.record(
        RequestRecord("req-cap-0100", "/e", 200, 1.0, None, 100.0)
    )
    assert len(small_registry.recent(limit=10_000)) == 25


def test_latency_buckets_boundaries() -> None:
    table = [
        (0.0, "0-50ms"), (49.999, "0-50ms"),
        (50.0, "50-100ms"), (99.999, "50-100ms"),
        (100.0, "100-200ms"), (199.999, "100-200ms"),
        (200.0, "200-500ms"), (499.999, "200-500ms"),
        (500.0, "500ms+"), (12345.0, "500ms+"),
    ]
    for latency, expected in table:
        got = LatencyBucket.for_latency(latency).value
        print(f"[判定] latency={latency}ms -> {got}（期望 {expected}）")
        assert got == expected
