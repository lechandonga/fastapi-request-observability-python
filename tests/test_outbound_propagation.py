"""出站传播头的构造、下游标识回收、开关与并发隔离。"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

GENERATED_RE = re.compile(r"^req-[0-9a-f]{32}$")


def test_outbound_header_carries_current_request_id_by_default(client: TestClient):
    rid = "outbound-001"
    print(f"\n[输入] GET /demo/downstream-call, X-Request-ID={rid} (默认开启出站传播)")
    r = client.get("/demo/downstream-call", headers={"X-Request-ID": rid})
    body = r.json()
    print(f"[判定] outbound_headers={body['outbound_headers']}, "
          f"captured_downstream_id={body['captured_downstream_id']}")
    assert r.status_code == 200
    assert body["outbound_headers"] == {"X-Request-ID": rid}, "出站头必须可原样带过去"
    assert body["captured_downstream_id"] == f"down-{rid}"
    assert body["all_downstream_ids"] == {"simulated-downstream": f"down-{rid}"}


def test_downstream_id_is_linked_in_recent_record(client: TestClient):
    rid = "outbound-link-1"
    print(f"\n[输入] {rid} 调下游后按标识查明细")
    client.get("/demo/downstream-call", headers={"X-Request-ID": rid})
    records = client.get(
        "/observability/recent", params={"request_id": rid}
    ).json()["records"]
    print(f"[判定] 明细 downstream_ids={records[0]['downstream_ids']}")
    assert len(records) == 1
    assert records[0]["downstream_ids"] == {"simulated-downstream": f"down-{rid}"}


def test_propagation_disabled_per_call_sends_nothing(client: TestClient):
    rid = "outbound-off-1"
    print(f"\n[输入] propagate=false（模拟调第三方不外发）, X-Request-ID={rid}")
    r = client.get(
        "/demo/downstream-call",
        params={"propagate": "false"},
        headers={"X-Request-ID": rid},
    )
    body = r.json()
    print(f"[判定] outbound_headers={body['outbound_headers']}, "
          f"captured={body['captured_downstream_id']}, 本请求标识仍在={body['request_id']}")
    assert body["outbound_headers"] == {}, "关闭后出站头必须为空"
    assert body["captured_downstream_id"] is None
    assert body["request_id"] == rid, "关闭的是出站传播，不影响本请求标识"


def test_global_propagation_switch_via_settings():
    from app.config import RequestIdSettings, Settings
    from app.logging_setup import configure_logging
    from app.main import build_app
    import logging

    settings = Settings(
        request_id=RequestIdSettings(outbound_propagation_enabled=False)
    )
    configure_logging()
    app = build_app(settings)
    for name in ("observability.access", "observability.diagnostic",
                 "observability.middleware", "observability.alert", "httpx"):
        logging.getLogger(name).disabled = True
    c = TestClient(app)
    print("\n[输入] 全局 OBS 出站传播关闭，默认调用不带 enabled 覆盖")
    r = c.get("/demo/downstream-call", headers={"X-Request-ID": "global-off"})
    print(f"[判定] outbound_headers={r.json()['outbound_headers']}（期望为空）")
    assert r.json()["outbound_headers"] == {}


def test_concurrent_outbound_headers_never_cross_talk(client: TestClient):
    n = 40
    rids = [f"chain-{i:03d}" for i in range(n)]
    print(f"\n[输入] {n} 并发，各带唯一标识调下游；断言出站头/回链标识各归各")

    def one(rid: str):
        return rid, client.get("/demo/downstream-call", headers={"X-Request-ID": rid})

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(one, rids))

    seen = set()
    for rid, r in results:
        body = r.json()
        assert r.status_code == 200
        assert body["outbound_headers"] == {"X-Request-ID": rid}
        assert body["captured_downstream_id"] == f"down-{rid}"
        seen.add(body["captured_downstream_id"])
    print(f"[判定] 回链标识集合大小={len(seen)}（期望 {n}，无串扰）")
    assert len(seen) == n


def test_asyncio_context_isolation_for_outbound_state():
    """直接在 asyncio 任务层验证 ContextVar 隔离与请求外安全降级。"""
    from app.propagation import (
        build_outbound_headers,
        record_downstream_id,
        get_downstream_ids,
    )
    from app.request_id import set_request_id, reset_request_id

    print("\n[输入] 100 个 asyncio 任务各自 set 标识 -> 取出站头 -> 登记下游标识")

    async def task(i: int) -> tuple[dict, dict]:
        rid = f"task-{i:03d}"
        token = set_request_id(rid)
        try:
            await asyncio.sleep(0)
            headers = build_outbound_headers()
            record_downstream_id(f"down-task-{i:03d}")
            await asyncio.sleep(0)
            return headers, get_downstream_ids()
        finally:
            reset_request_id(token)

    async def main():
        return await asyncio.gather(*(task(i) for i in range(100)))

    results = asyncio.run(main())
    for i, (headers, linked) in enumerate(results):
        assert headers == {"X-Request-ID": f"task-{i:03d}"}, headers
        assert linked == {"": f"down-task-{i:03d}"}, linked
    outside = build_outbound_headers()
    print(f"[判定] 100 任务头/回链全部各归各；请求上下文外调用返回 {outside}")
    assert outside == {}, "上下文外必须安全降级为空头，不抛异常"
