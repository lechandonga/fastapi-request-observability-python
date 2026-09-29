"""并发请求下请求关联标识的接收、生成、传播与隔离。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

GENERATED_RE = re.compile(r"^req-[0-9a-f]{32}$")
N_CONCURRENT = 40


def _call(client: TestClient, supplied: str | None):
    headers = {"X-Request-ID": supplied} if supplied is not None else {}
    r = client.get("/echo", params={"msg": "ping"}, headers=headers)
    return supplied, r


def test_concurrent_request_ids_are_isolated(client: TestClient):
    """N 个并发请求：各自携带唯一入站标识，响应头与响应体必须与之一致且互不串扰。"""
    supplied = [f"caller-{i:03d}.id_x" for i in range(N_CONCURRENT)]
    print(f"\n[输入] 并发数={N_CONCURRENT}, 每个请求携带唯一 X-Request-ID, 端点=/echo")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda s: _call(client, s), supplied))

    seen_header: set[str] = set()
    seen_body: set[str] = set()
    for inbound, r in results:
        header_id = r.headers.get("x-request-id")
        body_id = r.json()["request_id"]
        assert r.status_code == 200, r.text
        assert header_id == inbound, f"响应头标识未原样回传: {header_id} != {inbound}"
        assert body_id == inbound, f"内部传播标识串扰: {body_id} != {inbound}"
        seen_header.add(header_id)
        seen_body.add(body_id)

    print(f"[判定] 响应头标识集合大小={len(seen_header)}, 响应体标识集合大小={len(seen_body)}")
    assert len(seen_header) == N_CONCURRENT, "并发请求响应头标识出现重复/串扰"
    assert len(seen_body) == N_CONCURRENT, "并发请求内部传播标识出现重复/串扰"
    assert seen_header == seen_body == set(supplied), "存在跨请求标识串扰"


def test_concurrent_generated_ids_are_unique_and_isolated(client: TestClient):
    """不提供标识的并发请求：生成规则确定且互不碰撞、互不串扰。"""
    print(f"\n[输入] 并发数={N_CONCURRENT}, 均不携带 X-Request-ID")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: _call(client, None), range(N_CONCURRENT)))

    generated = set()
    for _, r in results:
        rid = r.headers["x-request-id"]
        assert GENERATED_RE.match(rid), f"生成标识不符合规则 req-<32hex>: {rid}"
        assert r.json()["request_id"] == rid, "生成标识在日志上下文/响应体间不一致"
        generated.add(rid)

    print(f"[判定] 生成标识数={len(generated)}, 规则=req-<uuid4hex32>, 碰撞数={N_CONCURRENT - len(generated)}")
    assert len(generated) == N_CONCURRENT, "并发生成标识发生碰撞"


def test_mixed_valid_invalid_missing_concurrent(client: TestClient):
    """并发混合：合法原样、非法 400 拒绝、缺失生成，三类结果各自正确。"""
    plan = (
        [("valid", f"ok-{i}") for i in range(15)]
        + [("invalid", f"bad {i}!") for i in range(15)]
        + [("missing", None) for i in range(15)]
    )
    print(f"\n[输入] 并发混合请求 合法15/非法15/缺失15, 共{len(plan)}")

    def job(item):
        kind, rid = item
        return kind, *_call(client, rid)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(job, plan))

    counts = {"valid": 0, "invalid": 0, "missing": 0}
    for kind, inbound, r in results:
        if kind == "valid":
            assert r.status_code == 200 and r.headers["x-request-id"] == inbound
            counts["valid"] += 1
        elif kind == "invalid":
            assert r.status_code == 400, r.text
            body = r.json()
            assert body["error"]["type"] == "invalid_request_id"
            # 被拒请求带本次专属标识（新生成），且与响应体一致、不回显非法原值
            rejection_id = r.headers["x-request-id"]
            assert GENERATED_RE.match(rejection_id)
            assert body["request_id"] == rejection_id
            assert rejection_id != inbound
            counts["invalid"] += 1
        else:
            assert r.status_code == 200
            rid = r.headers["x-request-id"]
            assert GENERATED_RE.match(rid)
            counts["missing"] += 1

    print(f"[判定] 合法2xx={counts['valid']}, 非法400={counts['invalid']}, 缺失已生成2xx={counts['missing']}")
    assert counts == {"valid": 15, "invalid": 15, "missing": 15}
