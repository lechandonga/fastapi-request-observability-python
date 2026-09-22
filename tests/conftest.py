"""pytest 公共夹具：每个测试使用独立的应用与指标注册器。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.logging_setup import configure_logging
from app.main import build_app


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    configure_logging()
    app = build_app(settings)
    # 测试输出聚焦：断言用 print 的输入/判定依据，不混入服务 JSON 日志
    for name in ("observability.access", "observability.diagnostic",
                 "observability.middleware", "observability.alert", "httpx"):
        logging.getLogger(name).disabled = True
    with TestClient(app) as c:
        yield c


@pytest.fixture
def registry(client: TestClient):
    return client.app.state.registry
