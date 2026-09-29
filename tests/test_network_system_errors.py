"""网络／系统级故障的错误分类：语义贴切、可统计、不泄露内部细节。"""

from __future__ import annotations

import socket

from fastapi.testclient import TestClient

from app.errors import ErrorType, classify_exception

SENSITIVE_MARKERS = ["10.0.0.7", "10.0.0.9", "6379", "errno", "Connection refused",
                     "Connection reset", "socket.read"]


def test_connection_refused_is_network_system_error(client: TestClient):
    rid = "net-refused-1"
    print(f"\n[输入] GET /demo/network-error (ConnectionRefusedError 含主机:端口) X-Request-ID={rid}")
    r = client.get("/demo/network-error", headers={"X-Request-ID": rid})
    print(f"[判定] status={r.status_code}, type={r.json()['error']['type']}")
    assert r.status_code == 502
    assert r.headers["x-request-id"] == rid
    assert r.json()["error"]["type"] == "network_system_error"
    assert r.json()["error"]["message"] == "网络或系统错误"
    assert r.json()["error"]["detail"] is None
    for marker in SENSITIVE_MARKERS:
        assert marker not in r.text, f"响应体泄露内部细节: {marker}"


def test_connection_reset_is_network_system_error(client: TestClient):
    rid = "net-reset-1"
    print(f"\n[输入] GET /demo/system-error (ConnectionResetError) X-Request-ID={rid}")
    r = client.get("/demo/system-error", headers={"X-Request-ID": rid})
    print(f"[判定] status={r.status_code}, type={r.json()['error']['type']}")
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "network_system_error"
    assert "443" not in r.text


def test_classify_standard_network_exceptions_stably():
    cases = [
        ConnectionRefusedError(111, "refused 10.0.0.7:6379"),
        ConnectionResetError(104, "reset"),
        ConnectionAbortedError(103, "aborted"),
        BrokenPipeError(32, "broken pipe"),
        socket.gaierror(-2, "Name or service not known secret.internal"),
    ]
    print(f"\n[输入] 标准库网络/系统级异常 {len(cases)} 个: "
          f"{[type(e).__name__ for e in cases]}")
    for exc in cases:
        et, status, message = classify_exception(exc)
        print(f"[判定] {type(exc).__name__} -> {et.value}/{status}/{message}")
        assert et is ErrorType.NETWORK_SYSTEM_ERROR
        assert status == 502
        assert message == "网络或系统错误"


def test_network_classification_does_not_swallow_other_categories():
    print("\n[输入] 超时/文件类OSError/普通未知异常，验证归类边界不被打乱")
    timeout_et, _, _ = classify_exception(TimeoutError())
    file_et, file_status, _ = classify_exception(FileNotFoundError("secret /etc/x"))
    runtime_et, runtime_status, _ = classify_exception(RuntimeError("secret boom"))
    print(f"[判定] TimeoutError->{timeout_et.value}, "
          f"FileNotFoundError->{file_et.value}/{file_status}, "
          f"RuntimeError->{runtime_et.value}/{runtime_status}")
    assert timeout_et is ErrorType.TIMEOUT_ERROR
    # 文件类 OSError 不属于网络/系统级故障，仍走兜底
    assert file_et is ErrorType.UNCAUGHT_ERROR and file_status == 500
    assert runtime_et is ErrorType.UNCAUGHT_ERROR and runtime_status == 500


def test_network_error_distinguished_in_metrics(client: TestClient, registry):
    print("\n[输入] 各打一次 network-error / uncaught / timeout，验证汇总可区分")
    client.get("/demo/network-error", headers={"X-Request-ID": "m-net"})
    client.get("/demo/system-error", headers={"X-Request-ID": "m-sys"})
    client.get("/demo/uncaught", headers={"X-Request-ID": "m-unc"})
    client.get("/demo/timeout", headers={"X-Request-ID": "m-to"})

    allm = {m.endpoint: m for m in registry.all_metrics()}
    net = allm["/demo/network-error"].by_error_type
    sys_ = allm["/demo/system-error"].by_error_type
    unc = allm["/demo/uncaught"].by_error_type
    to = allm["/demo/timeout"].by_error_type
    print(f"[判定] network={net}, system={sys_}, uncaught={unc}, timeout={to}")
    assert net == {"network_system_error": 1}
    assert sys_ == {"network_system_error": 1}
    assert unc == {"uncaught_error": 1}
    assert to == {"timeout_error": 1}
    # 502 计入 5xx 服务错误率，语义与既有指标一致
    assert allm["/demo/network-error"].by_status == {502: 1}
