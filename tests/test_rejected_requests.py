"""入口被拒请求：专属标识贯穿响应/日志/回溯，且明细容量为硬上界。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.config import MetricsSettings, Settings
from app.main import build_app
from app.logging_setup import configure_logging
import logging

REJECTION_ID_RE = re.compile(r"^req-[0-9a-f]{32}$")


def _client(normal_cap: int, rejected_cap: int) -> TestClient:
    configure_logging()
    settings = Settings(
        metrics=MetricsSettings(
            recent_records_capacity=normal_cap,
            rejected_records_capacity=rejected_cap,
        )
    )
    app = build_app(settings)
    for name in ("observability.access", "observability.diagnostic",
                 "observability.middleware", "observability.alert", "httpx"):
        logging.getLogger(name).disabled = True
    return TestClient(app)


def test_rejected_request_has_traceable_id_and_reason(client: TestClient):
    print("\n[输入] 非法标识 'bad id!'（含空格）打到 /echo")
    r = client.get("/echo", params={"msg": "x"}, headers={"X-Request-ID": "bad id!"})
    print(f"[判定] status={r.status_code}, headers.rid={r.headers.get('x-request-id')}")
    assert r.status_code == 400
    rid = r.headers["x-request-id"]
    body = r.json()
    assert REJECTION_ID_RE.match(rid), "被拒请求必须有本次专属标识"
    assert body["request_id"] == rid, "响应体与响应头标识必须对得上"
    assert body["error"]["type"] == "invalid_request_id"
    assert "bad id!" not in r.text, "原始非法值不得回显"
    assert "非法字符" in body["error"]["detail"]

    found = client.get(
        "/observability/recent", params={"request_id": rid}
    ).json()["records"]
    print(f"[判定] 按标识 {rid} 查到 {len(found)} 条被拒明细: {found[0]}")
    assert len(found) == 1
    rec = found[0]
    assert rec["request_id"] == rid
    assert rec["rejected"] is True
    assert rec["status_code"] == 400
    assert rec["endpoint"] == "__invalid_request_id__"
    assert rec["reject_reason"]


def test_rejected_records_do_not_evict_normal_records():
    print("\n[输入] 正常容量=30、被拒容量=5；先 8 个正常请求，再灌 50 个非法请求")
    c = _client(normal_cap=30, rejected_cap=5)
    for i in range(8):
        assert c.get(
            "/", headers={"X-Request-ID": f"ok-{i}"}
        ).status_code == 200

    rejected_ids = []
    for i in range(50):
        r = c.get("/", headers={"X-Request-ID": f"bad {i}!"})
        assert r.status_code == 400
        rejected_ids.append(r.headers["x-request-id"])

    normal = c.get(
        "/observability/recent", params={"include_rejected": False, "limit": 100}
    ).json()
    normal_ids = [r["request_id"] for r in normal["records"]]
    print(f"[判定] 正常明细保留 {len(normal_ids)} 条: {normal_ids}")
    assert normal_ids == [f"ok-{i}" for i in reversed(range(8))], \
        "被拒流量不得把正常请求明细顶出去"

    rejected = c.get(
        "/observability/recent",
        params={"request_id": rejected_ids[0]},
    ).json()["records"]
    print(f"[判定] 最早被拒标识可查条数={len(rejected)}（已被淘汰，期望 0）")
    assert rejected == [], "超出被拒容量的最老记录必须被淘汰"


def test_rejected_capacity_is_hard_bound_under_concurrency():
    rejected_cap = 10
    n = 120
    print(f"\n[输入] 被拒容量={rejected_cap}, {n} 个并发非法请求（模拟扫描器乱打）")
    c = _client(normal_cap=5, rejected_cap=rejected_cap)

    def job(i: int):
        return c.get("/", headers={"X-Request-ID": f"evil {i}!!"})

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(job, range(n)))

    assert all(r.status_code == 400 for r in results)
    all_records = c.app.state.registry
    rejected = c.get("/observability/recent", params={"limit": 400}).json()["records"]
    rejected = [r for r in rejected if r["rejected"]]
    print(
        f"[判定] 被拒明细={len(rejected)}, 内部缓冲={len(all_records._rejected)}, "
        f"容量={all_records.rejected_capacity()}"
    )
    assert len(rejected) == rejected_cap, "被拒明细数量必须受硬上界约束"
    assert len(all_records._rejected) == rejected_cap
    assert len({r["request_id"] for r in rejected}) == rejected_cap

    normal = c.get(
        "/observability/recent", params={"include_rejected": False, "limit": 100}
    ).json()["records"]
    # 查询端点本身会产生一条正常记录，排除它之后不应有任何正常流量
    business = [r for r in normal if r["endpoint"] != "/observability/recent"]
    print(f"[判定] 业务正常明细条数={len(business)}（扫描器流量下期望 0 混入）")
    assert business == []
