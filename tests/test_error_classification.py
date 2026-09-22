"""异常归类、错误信封安全性与兼容性。"""

from __future__ import annotations

from fastapi.testclient import TestClient

SECRET_MARKERS = [
    "secret", "10.0.0.7", "6379", "9527", "db.query",
    "Traceback", "RuntimeError", "File \"",
]


def test_uncaught_exception_is_classified_and_sanitized(client: TestClient):
    rid = "trace-uncaught-001"
    print(f"\n[输入] GET /demo/uncaught, X-Request-ID={rid} (抛出 RuntimeError 含敏感串)")
    r = client.get("/demo/uncaught", headers={"X-Request-ID": rid})

    print(f"[判定] status={r.status_code}, error.type={r.json()['error']['type']}")
    assert r.status_code == 500
    assert r.headers["x-request-id"] == rid, "错误响应必须回传请求标识"
    body = r.json()
    assert body["request_id"] == rid
    assert body["error"]["type"] == "uncaught_error"
    assert body["error"]["message"] == "服务内部错误"
    assert body["error"]["detail"] is None
    for marker in SECRET_MARKERS:
        assert marker not in r.text, f"响应体泄露内部信息: {marker}"


def test_business_error_hides_internal_detail(client: TestClient):
    print("\n[输入] GET /demo/business-error (BusinessError 含内部账户敏感细节)")
    r = client.get("/demo/business-error", headers={"X-Request-ID": "biz-1"})
    print(f"[判定] status={r.status_code}, error.type={r.json()['error']['type']}")
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "business_error"
    assert "9527" not in r.text and "余额" not in r.text


def test_dependency_error_is_distinguishable(client: TestClient):
    print("\n[输入] GET /demo/dependency-error (DependencyError 含内部主机:端口)")
    r = client.get("/demo/dependency-error", headers={"X-Request-ID": "dep-1"})
    print(f"[判定] status={r.status_code}, error.type={r.json()['error']['type']}")
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "dependency_error"
    assert "10.0.0.7" not in r.text and "6379" not in r.text


def test_timeout_is_distinguishable(client: TestClient):
    print("\n[输入] GET /demo/timeout (asyncio.TimeoutError)")
    r = client.get("/demo/timeout", headers={"X-Request-ID": "to-1"})
    print(f"[判定] status={r.status_code}, error.type={r.json()['error']['type']}")
    assert r.status_code == 504
    assert r.json()["error"]["type"] == "timeout_error"


def test_validation_error_returns_safe_field_info(client: TestClient):
    print("\n[输入] GET /echo 缺少必填 query 参数 msg")
    r = client.get("/echo", headers={"X-Request-ID": "val-1"})
    print(f"[判定] status={r.status_code}, body={r.json()}")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["type"] == "validation_error"
    assert body["error"]["detail"] == [{"field": "query.msg", "rule": "missing"}]


def test_not_found_envelope(client: TestClient):
    print("\n[输入] GET /does-not-exist")
    r = client.get("/does-not-exist", headers={"X-Request-ID": "nf-1"})
    print(f"[判定] status={r.status_code}, error.type={r.json()['error']['type']}")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "not_found"


def test_invalid_request_id_rejected(client: TestClient):
    print("\n[输入] GET /echo, X-Request-ID='bad id!' (含空格与感叹号)")
    r = client.get("/echo", params={"msg": "x"}, headers={"X-Request-ID": "bad id!"})
    print(f"[判定] status={r.status_code}, body={r.json()}")
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_id"
    assert r.headers.get("x-request-id") in (None, "")


def test_root_compatibility(client: TestClient):
    """既有接口对外行为保持兼容：状态码与响应体不变。"""
    print("\n[输入] GET / (兼容性检查)")
    r = client.get("/")
    print(f"[判定] status={r.status_code}, body={r.json()}")
    assert r.status_code == 200
    assert r.json() == {"Hello": "World"}


def test_error_types_recorded_in_metrics(client: TestClient, registry):
    print("\n[输入] 依次触发 uncaught/business/dependency/404/400 各一次")
    client.get("/demo/uncaught", headers={"X-Request-ID": "m-1"})
    client.get("/demo/business-error", headers={"X-Request-ID": "m-2"})
    client.get("/demo/dependency-error", headers={"X-Request-ID": "m-3"})
    client.get("/missing-path")
    client.get("/echo", headers={"X-Request-ID": "bad!!"})

    allm = {m.endpoint: m for m in registry.all_metrics()}
    print(
        "[判定] 各端点 by_error_type:",
        {ep: m.by_error_type for ep, m in allm.items()},
    )
    assert allm["/demo/uncaught"].by_error_type["uncaught_error"] == 1
    assert allm["/demo/business-error"].by_error_type["business_error"] == 1
    assert allm["/demo/dependency-error"].by_error_type["dependency_error"] == 1
    assert allm["__unmatched__"].by_error_type["not_found"] == 1
    assert allm["__invalid_request_id__"].by_error_type["invalid_request_id"] == 1
