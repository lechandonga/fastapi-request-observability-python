"""请求关联标识：接收、生成、拒绝、回写与下游传播规则。"""
from __future__ import annotations


def test_caller_supplied_valid_id_is_adopted_and_echoed(client) -> None:
    rid = "caller-0001-abcd"
    print(f"\n[input] X-Request-ID={rid!r} -> 期望原样采用并回写")
    r = client.get("/echo/request-id", headers={"X-Request-ID": rid})
    print(f"[判定] status={r.status_code} body={r.json()} resp-header={r.headers.get('x-request-id')!r}")
    assert r.status_code == 200
    assert r.json()["x-request-id"] == rid
    assert r.json()["received"] is True
    assert r.headers["x-request-id"] == rid


def test_missing_id_is_generated_with_fixed_format(client) -> None:
    print("\n[input] 无 X-Request-ID 头 -> 期望按 req-<uuid4hex> 生成")
    r = client.get("/echo/request-id")
    body = r.json()
    rid = body["x-request-id"]
    print(f"[判定] status={r.status_code} generated={rid!r} received={body['received']}")
    assert r.status_code == 200
    assert rid.startswith("req-")
    assert len(rid) == 4 + 32
    assert all(ch in "0123456789abcdef" for ch in rid[4:])
    assert body["received"] is False
    assert r.headers["x-request-id"] == rid


def test_generated_ids_are_unique_across_requests(client) -> None:
    print("\n[input] 连续 50 个无标识请求 -> 期望 50 个互不相同的生成标识")
    ids = {client.get("/echo/request-id").json()["x-request-id"] for _ in range(50)}
    print(f"[判定] 唯一标识数={len(ids)}（期望 50）")
    assert len(ids) == 50


def test_invalid_id_is_rejected_with_400_and_safe_body(client) -> None:
    # 注意：空串/纯空白按"未提供"处理（生成新标识），见上一条规则；
    # 这里只覆盖"提供了但格式非法"的输入。
    for bad in ["bad", "has space", "bad!char", "1-newline\ninjected", "x" * 129]:
        print(f"\n[input] X-Request-ID={bad!r} -> 期望 400 invalid_request_id")
        r = client.get("/echo/request-id", headers={"X-Request-ID": bad})
        print(f"[判定] status={r.status_code} body={r.json()}")
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["category"] == "invalid_request_id"
        # 拒绝响应仍带诊断标识（服务端新生成），便于关联日志排查
        assert body["error"]["request_id"].startswith("req-")
        assert r.headers["x-request-id"].startswith("req-")
        # 非法输入原样不得进入响应体（防注入/防混淆）
        assert bad.strip() not in body["error"]["request_id"]


def test_blank_id_is_treated_as_missing_and_generated(client) -> None:
    print("\n[input] X-Request-ID 为空串或纯空白 -> 按未提供处理并生成")
    for blank in ["", "   "]:
        r = client.get("/echo/request-id", headers={"X-Request-ID": blank})
        print(f"[判定] input={blank!r} status={r.status_code} body={r.json()}")
        assert r.status_code == 200
        assert r.json()["x-request-id"].startswith("req-")
        assert r.json()["received"] is False


def test_request_id_propagates_to_internal_downstream_headers(client) -> None:
    rid = "caller-propagate-9"
    print(f"\n[input] X-Request-ID={rid!r} -> 期望内部调用头携带同一标识")
    r = client.get("/internal/downstream-call", headers={"X-Request-ID": rid})
    print(f"[判定] status={r.status_code} body={r.json()}")
    assert r.status_code == 200
    assert r.json()["propagated_headers"] == {"X-Request-ID": rid}
    assert r.json()["request_id"] == rid


def test_root_endpoint_remains_compatible(client) -> None:
    print('\n[input] GET / （带标识）-> 期望响应体仍为 {"Hello": "World"}')
    r = client.get("/", headers={"X-Request-ID": "caller-root-0001"})
    print(f"[判定] status={r.status_code} body={r.json()}")
    assert r.status_code == 200
    assert r.json() == {"Hello": "World"}
    assert r.headers["x-request-id"] == "caller-root-0001"
