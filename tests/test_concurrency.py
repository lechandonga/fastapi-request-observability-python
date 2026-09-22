"""高并发请求下的请求标识隔离。

使用 httpx 的 ASGITransport 在同一事件循环内并发发起大量请求，
探针端点会 await 让出事件循环（强制交错调度），是暴露 ContextVar
串扰最直接的场景。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from main import create_app
from app.metrics import MetricsRegistry

N_CONCURRENT = 64


async def _probe(transport: httpx.ASGITransport, rid: str) -> dict:
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac:
        r = await ac.get(
            "/internal/isolation-probe",
            headers={"X-Request-ID": rid},
        )
        assert r.status_code == 200, r.text
        return r.json()


def test_request_ids_isolated_under_concurrency_sync() -> None:
    reg = MetricsRegistry(capacity=10_000)
    app = create_app(metrics=reg)
    transport = httpx.ASGITransport(app=app)

    request_ids = [f"concurrent-req-{i:04d}-xy" for i in range(N_CONCURRENT)]
    print(f"\n[input] 同一事件循环并发 {N_CONCURRENT} 个请求，"
          f"每个携带唯一标识，端点内 await 10ms 强制交错")

    async def scenario() -> list[dict]:
        return await asyncio.gather(*[_probe(transport, rid) for rid in request_ids])

    results = asyncio.run(scenario())

    print(f"[判定] 返回结果数={len(results)}（期望 {N_CONCURRENT}）")
    assert len(results) == N_CONCURRENT

    seen = set()
    for rid, body in zip(request_ids, results):
        # 判定依据 1：让出前后读到的标识、以及下游传播头三者必须一致
        if not body["consistent"]:
            pytest.fail(f"标识在 await 前后不一致: rid={rid} body={body}")
        # 判定依据 2：读到的必须是自己的标识，而不是别的并发请求的
        if body["seen_before"] != rid or body["seen_after"] != rid:
            pytest.fail(f"发生跨请求串扰: 期望 {rid} 实际 {body}")
        if body["propagated"] != {"X-Request-ID": rid}:
            pytest.fail(f"下游传播头错误: {body}")
        seen.add(body["seen_after"])

    # 判定依据 3：64 个标识必须两两不同
    print(f"[判定] 唯一标识集合大小={len(seen)}（期望 {N_CONCURRENT}，无串扰）")
    assert len(seen) == N_CONCURRENT
    assert seen == set(request_ids)

    # 判定依据 4：指标按端点聚合为 64 次，状态码全 200
    stats = reg.stats("/internal/isolation-probe")
    print(f"[判定] 端点统计={stats['total']} 次, 状态码分布={stats['by_status_code']}")
    assert stats["total"] == N_CONCURRENT
    assert stats["by_status_code"] == {"200": N_CONCURRENT}
    # 判定依据 5：明细中的标识也必须是 64 个唯一值
    recent_ids = {r["request_id"] for r in reg.recent(limit=N_CONCURRENT)}
    assert recent_ids == set(request_ids)


def test_mixed_concurrent_valid_invalid_and_generated_ids() -> None:
    """并发混合：合法、非法、缺失三类标识同时出现，结果各自正确且互不影响。"""
    reg = MetricsRegistry(capacity=10_000)
    app = create_app(metrics=reg)
    transport = httpx.ASGITransport(app=app)

    async def scenario() -> list[tuple[int, str, str | None]]:
        async def one(idx: int) -> tuple[int, str, str | None]:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                if idx % 3 == 0:
                    headers = {"X-Request-ID": f"mixed-valid-{idx:03d}-zz"}
                elif idx % 3 == 1:
                    headers = {"X-Request-ID": f"bad{idx}"}  # 非法
                else:
                    headers = {}  # 缺失 -> 生成
                r = await ac.get("/echo/request-id", headers=headers)
                return r.status_code, r.headers.get("x-request-id"), r.json()["error"]["category"] if r.status_code >= 400 else None

        return await asyncio.gather(*(one(i) for i in range(30)))

    outcomes = asyncio.run(scenario())
    print(f"\n[input] 30 个并发请求混合 合法/非法/缺失 标识")
    n200 = sum(1 for s, _, _ in outcomes if s == 200)
    n400 = sum(1 for s, _, _ in outcomes if s == 400)
    print(f"[判定] 200 数量={n200}（期望 20）, 400 数量={n400}（期望 10）")
    assert n200 == 20 and n400 == 10
    resp_ids = [rid for _, rid, _ in outcomes]
    assert len(set(resp_ids)) == 30  # 每个响应的诊断标识互不相同
    for i, (status, rid, cat) in enumerate(outcomes):
        assert rid is not None and len(rid) >= 8
        if i % 3 == 1:
            assert cat == "invalid_request_id"
