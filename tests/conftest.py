"""测试夹具：每个用例使用独立的注册表，保证互不污染、结论可复现。"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.metrics import DEFAULT_RULE, MetricsRegistry
from main import create_app


def _silence_app_loggers() -> None:
    """测试中默认静默 app 日志，避免 JSON 行刷屏；需要时可在断言中单独查看。"""
    for name in ("app", "app.request", "app.http"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)


_silence_app_loggers()


@pytest.fixture
def registry() -> MetricsRegistry:
    reg = MetricsRegistry(
        capacity=1000,
        rule=DEFAULT_RULE,
        window_size=100,
    )
    yield reg
    reg.reset()


@pytest.fixture
def client(registry: MetricsRegistry) -> Iterator[TestClient]:
    app = create_app(metrics=registry)
    # 未捕获异常必须被中间件转成 500，而不是由 TestClient 直接抛出。
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def small_registry(registry: MetricsRegistry) -> MetricsRegistry:
    """容量上界专用夹具：小容量便于验证淘汰行为。"""
    return MetricsRegistry(capacity=25, rule=DEFAULT_RULE)
