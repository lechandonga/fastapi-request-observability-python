"""入口即被挡请求：专属标识贯穿响应/日志/明细，且容量有硬上界。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

REJECTED_RE = re.compile(r"^rej-[0-9a-f]{32}$")


def test_rejected_request_has_traceable_id_in_body_and_recent(client: TestClient):
    print("\n[输入] GET / 携带非法 X-Request-ID='bad id!'")
    r = client.get("/", headers={"X-Request-ID": "bad id!"})
    print(f"[判定] status={r.status_code}, body={r.json()}")
    # 对外拒绝规则与状态码保持不变
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_id"
    # 既有规则：响应头仍然不带标识
    assert r.headers.get("x-request-id") in (None, "")
    # 新行为：响应体给出本次专属标识
    rid = r.json()["request_id"]
    assert REJECTED_RE.match(rid), f"被拒标识应形如 rej-<32hex>: {rid}"
    assert r.json()["error"]["detail"] == "request_id 包含非法字符"

    # 拿这个标识能在回溯明细里查到对应那条记录
    found = client.get(
        "/observability/recent", params={"request_id": rid}
    ).json()["records"]
    print(f"[判定] 按 {rid} 查明细命中 {len(found)} 条: "
          f"status={found[0]['status_code']}, reject_reason={found[0]['reject_reason']}")
    assert len(found) == 1
    rec = found[0]
    assert rec["request_id"] == rid
    assert rec["rejected"] is True
    assert rec["status_code"] == 400
    assert rec["reject_reason"] == "request_id 包含非法字符"


def test_rejected_records_are_separately_listable_and_not_in_normal_feed(
    client: TestClient,
):
    print("\n[输入] 2 个正常请求 + 3 个被拒请求")
    for i in range(2):
        client.get("/", headers={"X-Request-ID": f"ok-{i}"})
    for _ in range(3):
        client.get("/", headers={"X-Request-ID": "bad!!"})

    normal = client.get("/observability/recent", params={"include": "normal"}).json()
    rejected = client.get(
        "/observability/recent", params={"include": "rejected"}
    ).json()
    normal_ids = [r["request_id"] for r in normal["records"]]
    rejected_ids = [r["request_id"] for r in rejected["records"]]
    print(f"[判定] 正常feed={normal_ids}, 被拒feed={rejected_ids}")
    assert normal_ids == ["ok-1", "ok-0"], "被拒请求不得混入正常明细"
    assert len(rejected_ids) == 3 and all(REJECTED_RE.match(x) for x in rejected_ids)
    assert all(r["rejected"] for r in rejected["records"])


def test_rejected_flood_keeps_hard_capacity_and_does_not_evict_normal(
    client: TestClient,
):
    from app.config import MetricsSettings, Settings
    from app.logging_setup import configure_logging
    from app.main import build_app
    import logging

    cap_normal, cap_rejected = 20, 30
    settings = Settings(
        metrics=MetricsSettings(
            recent_records_capacity=cap_normal,
            recent_rejected_capacity=cap_rejected,
        )
    )
    configure_logging()
    app = build_app(settings)
    for name in ("observability.access", "observability.diagnostic",
                 "observability.middleware", "observability.alert", "httpx"):
        logging.getLogger(name).disabled = True
    c = TestClient(app)

    print(f"\n[输入] {cap_normal} 个正常请求后，{300} 个被拒请求（扫描器乱打）；"
          f"容量 正常={cap_normal}/被拒={cap_rejected}")
    for i in range(cap_normal):
        c.get("/", headers={"X-Request-ID": f"good-{i:03d}"})

    def flood(i: int):
        return c.get("/", headers={"X-Request-ID": f"bad {i}!"})

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(flood, range(300)))

    normal = c.get(
        "/observability/recent",
        params={"include": "normal", "limit": 200},
    ).json()
    rejected = c.get(
        "/observability/recent",
        params={"include": "rejected", "limit": 200},
    ).json()
    n_ids = [r["request_id"] for r in normal["records"]]
    r_ids = [r["request_id"] for r in rejected["records"]]
    print(f"[判定] 正常明细={len(n_ids)} 条且全部保留; 被拒明细={len(r_ids)} 条（硬上界）")
    assert len(n_ids) == cap_normal, "被扫爆不得顶掉正常请求明细"
    assert all(x.startswith("good-") for x in n_ids)
    assert len(r_ids) == cap_rejected, "被拒明细也必须有硬容量上界"
    assert normal["capacity"] == cap_normal
    assert rejected["rejected_capacity"] == cap_rejected
    reg = c.app.state.registry
    assert len(reg._recent) == cap_normal
    assert len(reg._rejected_recent) == cap_rejected
