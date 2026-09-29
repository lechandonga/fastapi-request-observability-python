"""网络/系统级故障的独立归类、安全文案与指标区分。"""

from __future__ import annotations

import socket
import ssl

from fastapi.testclient import TestClient

from app.errors import classify_exception

LEAK_MARKERS = ["10.0.0.9", "6379", "/dev/internal", "Errno", "Traceback"]


def test_classify_network_and_system_failures():
    cases = [
        ("连接被拒", ConnectionRefusedError(111, "Connection refused 10.0.0.9:6379")),
        ("连接中断", ConnectionResetError(104, "Connection reset by peer")),
        ("连接中止", ConnectionAbortedError(103, "Software caused connection abort")),
        ("管道破裂", BrokenPipeError(32, "Broken pipe")),
        ("DNS 解析失败", socket.gaierror(-2, "Name or service not known")),
        ("SSL 故障", ssl.SSLError("SSLError: [SSL] syscall on secret-host:6379")),
        ("通用系统调用失败", OSError(5, "Input/output error /dev/internal")),
    ]
    print(f"\n[输入] {len(cases)} 类网络/系统级异常实例")
    for label, exc in cases:
        et, status, message = classify_exception(exc)
        print(f"[判定] {label}: {type(exc).__name__} -> {et.value}/{status} '{message}'")
        assert et.value == "network_error"
        assert status == 502
        assert message == "网络或系统级故障"


def test_timeout_still_distinct_from_network_error():
    print("\n[输入] TimeoutError（同时是 OSError 子类）必须优先归类为 timeout_error")
    et, status, message = classify_exception(TimeoutError("read timed out 10.0.0.9:6379"))
    print(f"[判定] -> {et.value}/{status}")
    assert et.value == "timeout_error" and status == 504


def test_plain_bug_is_still_uncaught():
    print("\n[输入] 普通代码缺陷 RuntimeError 仍归类 uncaught_error")
    et, status, _ = classify_exception(RuntimeError("bug at db.query"))
    print(f"[判定] -> {et.value}/{status}")
    assert et.value == "uncaught_error" and status == 500


def test_network_error_response_is_sanitized(client: TestClient):
    print("\n[输入] GET /demo/network-error, X-Request-ID=net-http-1")
    r = client.get("/demo/network-error", headers={"X-Request-ID": "net-http-1"})
    print(f"[判定] status={r.status_code}, body={r.text}")
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["type"] == "network_error"
    assert body["error"]["message"] == "网络或系统级故障"
    assert body["error"]["detail"] is None
    assert body["request_id"] == "net-http-1"
    for marker in LEAK_MARKERS:
        assert marker not in r.text, f"响应体泄露内部信息: {marker}"

    print("[输入] GET /demo/system-error (OSError)")
    r2 = client.get("/demo/system-error", headers={"X-Request-ID": "sys-http-1"})
    assert r2.status_code == 502
    assert r2.json()["error"]["type"] == "network_error"
    assert "/dev/internal" not in r2.text


def test_network_error_distinguishable_in_metrics(client: TestClient):
    print("\n[输入] network_error x2, uncaught_error x1, timeout_error x1")
    client.get("/demo/network-error", headers={"X-Request-ID": "nm-1"})
    client.get("/demo/network-error", headers={"X-Request-ID": "nm-2"})
    client.get("/demo/uncaught", headers={"X-Request-ID": "nm-3"})
    client.get("/demo/timeout", headers={"X-Request-ID": "nm-4"})
    allm = {m.endpoint: m for m in client.app.state.registry.all_metrics()}
    print("[判定] 网络故障端点 by_error_type:",
          allm["/demo/network-error"].by_error_type)
    assert allm["/demo/network-error"].by_error_type["network_error"] == 2
    assert allm["/demo/uncaught"].by_error_type["uncaught_error"] == 1
    assert allm["/demo/timeout"].by_error_type["timeout_error"] == 1
