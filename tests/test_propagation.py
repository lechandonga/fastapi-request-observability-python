"""跨服务链路：出站传播头构造、开关关闭不外发、下游标识回收与并发隔离。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.config import PropagationSettings, Settings
from app.main import build_app
from app.propagation import (
    capture_downstream_id,
    get_downstream_id,
    outbound_headers,
    propagation_enabled,
    reset_propagation_scope,
    reset_request_scope,
    set_propagation_scope,
)
from app.request_id import reset_request_id, set_request_id


def _client(propagation: PropagationSettings | None = None) -> TestClient:
    import logging

    from app.logging_setup import configure_logging

    configure_logging()
    settings = Settings(propagation=propagation) if propagation else Settings()
    app = build_app(settings)
    for name in ("observability.access", "observability.diagnostic",
                 "observability.middleware", "observability.alert", "httpx"):
        logging.getLogger(name).disabled = True
    return TestClient(app)


class _FakeResponse:
    """模拟下游响应：只提供 headers，避免依赖真实网络。"""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_outbound_headers_carry_current_request_id(client: TestClient):
    print("\n[输入] 绑定当前请求标识 chain-abc 后构造出站头")
    token = set_request_id("chain-abc")
    headers = outbound_headers({"X-Biz": "keep"})
    print(f"[判定] 出站头={headers}")
    assert headers == {"X-Request-ID": "chain-abc", "X-Biz": "keep"}
    reset_request_id(token)


def test_outbound_headers_empty_when_disabled(client: TestClient):
    print("\n[输入] 全局关闭传播后构造出站头（模拟调第三方）")
    c = _client(PropagationSettings(enabled=False))
    r = c.get("/demo/downstream-call", headers={"X-Request-ID": "off-1"})
    body = r.json()
    print(f"[判定] status={r.status_code}, 实际发出的传播头={body['sent_header']!r}")
    assert r.status_code == 200
    assert body["sent_header"] is None, "关闭时不得外发任何关联标识"

    print("[输入] 默认开启时同一路由对照")
    c2 = _client(PropagationSettings(enabled=True))
    r2 = c2.get("/demo/downstream-call", headers={"X-Request-ID": "on-1"})
    print(f"[判定] 发出的传播头={r2.json()['sent_header']!r}")
    assert r2.json()["sent_header"] == "on-1"


def test_scope_override_only_affects_current_request(client: TestClient):
    print("\n[输入] 请求内临时关闭传播（set_propagation_scope False）")
    token = set_request_id("scope-1")
    off_token = set_propagation_scope(False)
    assert outbound_headers() == {}
    assert propagation_enabled() is False
    reset_propagation_scope(off_token)
    assert outbound_headers() == {"X-Request-ID": "scope-1"}
    assert propagation_enabled() is True
    print("[判定] 关闭期间空头；reset 后恢复携带，且不影响全局默认")
    reset_request_id(token)


def test_downstream_response_id_is_captured_and_associated(client: TestClient):
    print("\n[输入] 下游响应回传它那一次的标识 down-xyz-777")
    token = set_request_id("parent-1")
    captured = capture_downstream_id(_FakeResponse({"X-Request-ID": "down-xyz-777"}))
    print(f"[判定] 回收值={captured!r}, 上下文关联下游标识={get_downstream_id()!r}")
    assert captured == "down-xyz-777"
    assert get_downstream_id() == "down-xyz-777"
    reset_request_scope()
    reset_request_id(token)

    print("[输入] 经 /demo/downstream-call 真实走一遍进程内下游并查回溯明细")
    r = client.get("/demo/downstream-call", headers={"X-Request-ID": "parent-2"})
    assert r.status_code == 200
    downstream_id = r.json()["downstream_request_id"]
    recs = client.get(
        "/observability/recent", params={"request_id": "parent-2"}
    ).json()["records"]
    print(f"[判定] 下游标识={downstream_id}, 明细关联字段={recs[0]['downstream_request_id']}")
    assert recs[0]["downstream_request_id"] == downstream_id


def test_downstream_id_isolation_under_concurrency(client: TestClient):
    n = 40
    print(f"\n[输入] {n} 个并发任务各自绑定唯一标识并回收不同下游标识")

    async def one(i: int):
        rid = f"parent-{i:03d}"
        token = set_request_id(rid)
        headers = outbound_headers()
        capture_downstream_id(_FakeResponse({"X-Request-ID": f"down-{i:03d}"}))
        await asyncio.sleep(0)
        associated = get_downstream_id()
        reset_request_scope()
        reset_request_id(token)
        return rid, headers, associated

    async def main():
        return await asyncio.gather(*(one(i) for i in range(n)))

    results = asyncio.new_event_loop().run_until_complete(main())
    pairs = {(rid, assoc) for rid, _, assoc in results}
    print(f"[判定] (本请求, 下游标识) 配对数={len(pairs)}（期望 {n}）")
    assert len(pairs) == n, "并发下下游标识发生串扰"
    for rid, headers, assoc in results:
        assert headers == {"X-Request-ID": rid}
        assert assoc == rid.replace("parent-", "down-")


def test_concurrent_downstream_calls_via_http_keep_chain_isolated(client: TestClient):
    n = 30
    print(f"\n[输入] {n} 个并发真实 HTTP 请求调用进程内下游，各带唯一标识")

    def job(i: int):
        rid = f"http-parent-{i:03d}"
        r = client.get("/demo/downstream-call", headers={"X-Request-ID": rid})
        return rid, r

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(job, range(n)))
    for rid, r in results:
        body = r.json()
        assert r.status_code == 200, r.text
        assert body["request_id"] == rid
        assert body["sent_header"] == rid
        assert body["associated_downstream_id"] == rid
    print(f"[判定] {n} 条链路的本端/出站/关联标识全部一一对应，无跨请求串扰")
