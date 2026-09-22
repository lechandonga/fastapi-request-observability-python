"""告警触发与不触发两类场景，结论必须稳定可复现。

为避免依赖真实睡眠与墙钟，这里直接向 :class:`MetricsRegistry` 写入
时间戳可控的记录，阈值语义因此可精确验证：

* 错误率/慢请求率 **严格大于** 阈值才触发（恰好等于不触发）；
* 样本数不足 ``min_samples`` 不触发（冷启动保护）；
* 同一端点同一类告警 60s 冷却，避免风暴；
* 冷却过后再次超阈会重新触发；
* 不同端点互不影响。
"""
from __future__ import annotations

from app.errors import ErrorCategory
from app.metrics import AlertRule, MetricsRegistry, RequestRecord

RULE = AlertRule(
    name="test",
    min_samples=10,
    error_rate_threshold=0.5,
    slow_rate_threshold=0.5,
    slow_latency_ms=200.0,
)


def _err(i: int, endpoint: str = "/e") -> RequestRecord:
    return RequestRecord(
        f"req-err-{i:03d}", endpoint, 500, 5.0,
        ErrorCategory.INTERNAL_ERROR, float(i),
    )


def _ok(i: int, latency: float = 5.0, endpoint: str = "/e") -> RequestRecord:
    return RequestRecord(
        f"req-ok-{i:03d}", endpoint, 200, latency, None, float(i)
    )


def test_no_alert_when_below_min_samples() -> None:
    reg = MetricsRegistry(capacity=1000, rule=RULE)
    print("\n[input] 仅 9 条样本，全部 500")
    for i in range(9):
        assert reg.record(_err(i)) == []
    print("[判定依据] min_samples=10，样本不足不产生任何告警")
    assert reg.alerts() == []


def test_no_alert_when_rate_equals_threshold() -> None:
    reg = MetricsRegistry(capacity=1000, rule=RULE)
    print("\n[input] 10 条样本中 5 条 500 -> 错误率=0.50，阈值=0.50")
    for i in range(5):
        reg.record(_err(i))
    for i in range(5, 10):
        reg.record(_ok(i))
    print("[判定依据] 严格大于才触发，0.50 == 0.50 不触发")
    assert reg.alerts() == []


def test_alert_triggered_when_error_rate_exceeds_threshold() -> None:
    reg = MetricsRegistry(capacity=1000, rule=RULE)
    print("\n[input] 10 条样本中 6 条 500 -> 错误率=0.60 > 0.50")
    for i in range(6):
        reg.record(_err(i))
    events = []
    for i in range(6, 10):
        events.extend(reg.record(_ok(i)))
    alerts = reg.alerts()
    print(f"[判定] 告警事件={alerts}")
    assert len(alerts) == 1
    a = alerts[0]
    assert a["kind"] == "error_rate"
    assert a["observed"] == 0.6
    assert a["threshold"] == 0.5
    assert a["sample_size"] == 10
    assert a["endpoint"] == "/e"
    # 可解释：消息中同时给出观测值、窗口大小与阈值
    assert "60%" in a["message"] and "50%" in a["message"]


def test_latency_alert_trigger_and_non_trigger() -> None:
    # 不触发：10 条中 5 条慢（恰好 50%）
    reg = MetricsRegistry(capacity=1000, rule=RULE)
    print("\n[input A] 10 条中 5 条 latency=200ms（慢请求率 0.50 == 阈值）")
    for i in range(5):
        reg.record(_ok(i, latency=200.0))
    for i in range(5, 10):
        reg.record(_ok(i, latency=10.0))
    print("[判定依据 A] 严格大于阈值才触发 -> 无延迟告警")
    assert [a for a in reg.alerts() if a["kind"] == "latency"] == []

    # 触发：10 条中 6 条慢（60% > 50%）
    reg2 = MetricsRegistry(capacity=1000, rule=RULE)
    print("\n[input B] 10 条中 6 条 latency=200ms（慢请求率 0.60 > 0.50）")
    for i in range(6):
        reg2.record(_ok(i, latency=200.0))
    for i in range(6, 10):
        reg2.record(_ok(i, latency=10.0))
    alerts = [a for a in reg2.alerts() if a["kind"] == "latency"]
    print(f"[判定 B] 延迟告警={alerts}")
    assert len(alerts) == 1
    assert alerts[0]["observed"] == 0.6
    assert "200" in alerts[0]["message"]


def test_alert_cooldown_suppresses_repeats() -> None:
    reg = MetricsRegistry(capacity=1000, rule=RULE)
    print("\n[input] 持续写入 500（时间戳每秒递增），错误率始终 >50%")
    for i in range(30):
        reg.record(_err(i))  # timestamp = i（秒）
    alerts = reg.alerts()
    print(f"[判定] 30 个超阈样本只产生 {len(alerts)} 条告警（60s 冷却）")
    # 第 10 条样本时首次告警；下一次可能告警要等到 t>=10+60=70
    assert len(alerts) == 1
    assert alerts[0]["sample_size"] == 10


def test_alert_re_fires_after_cooldown() -> None:
    reg = MetricsRegistry(capacity=1000, rule=RULE)
    print("\n[input] 超阈样本时间戳跨越 60s 冷却边界")
    events = []
    for i in range(10):
        events.extend(reg.record(_err(i)))  # t=0..9 首次告警
    assert len(reg.alerts()) == 1
    for i in range(70, 75):  # t=70+，已过冷却
        events.extend(reg.record(_err(i)))
    alerts = reg.alerts()
    print(f"[判定] 冷却后再次超阈，总告警数={len(alerts)}（期望 2）")
    assert len(alerts) == 2


def test_alerts_are_scoped_per_endpoint() -> None:
    reg = MetricsRegistry(capacity=1000, rule=RULE)
    print("\n[input] /a 错误率 60%，/b 全部成功")
    for i in range(6):
        reg.record(_err(i, endpoint="/a"))
    for i in range(6, 10):
        reg.record(_ok(i, endpoint="/a"))
    for i in range(20):
        reg.record(_ok(100 + i, endpoint="/b"))
    alerts = reg.alerts()
    print(f"[判定] 告警端点={[a['endpoint'] for a in alerts]}（只应含 /a）")
    assert len(alerts) == 1 and alerts[0]["endpoint"] == "/a"


def test_recovery_sliding_window_clears_condition() -> None:
    """错误率恢复后不再产生新告警（滑动窗口被成功请求稀释）。"""
    reg = MetricsRegistry(capacity=1000, rule=RULE, window_size=10)
    print("\n[input] 先 6 错误+4 成功触发告警，随后 10 条全部成功")
    for i in range(6):
        reg.record(_err(i))
    for i in range(6, 10):
        reg.record(_ok(i))
    assert len(reg.alerts()) == 1
    for i in range(10, 20):
        assert reg.record(_ok(i)) == []
    print("[判定依据] 最近 10 条全成功，错误率=0，无新增告警")
    assert len(reg.alerts()) == 1


def test_alert_history_is_bounded() -> None:
    # 用极短冷却制造大量告警，验证告警历史同样有上界
    import app.metrics as metrics_mod

    original = metrics_mod.ALERT_COOLDOWN_SECONDS
    metrics_mod.ALERT_COOLDOWN_SECONDS = 0.0
    try:
        reg = MetricsRegistry(
            capacity=10_000, rule=RULE, window_size=10, alert_history=5
        )
        print("\n[input] 关闭冷却后制造 >5 条告警，alert_history=5")
        for batch in range(10):
            for i in range(10):
                rec = RequestRecord(
                    f"r{batch}-{i}", "/e", 500, 1.0,
                    ErrorCategory.INTERNAL_ERROR,
                    float(batch * 100 + i),  # 时间戳间隔足够大
                )
                reg.record(rec)
        alerts = reg.alerts(limit=100)
        print(f"[判定] 保留告警数={len(alerts)}（硬上界 5）")
        assert len(alerts) == 5
    finally:
        metrics_mod.ALERT_COOLDOWN_SECONDS = original
