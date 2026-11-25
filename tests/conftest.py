"""Test fixtures. Every project here is invented (L-5): no data of a tracked project."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from pmd.config import Config
from pmd.data import db, hand
from pmd.web.app import create_app

# Tuesday 24 November 2026, 09:00 UTC.
NOW = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
ORIGIN = "http://testserver"


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "data", extra_hosts=("testserver",))


@pytest.fixture
def conn(config):
    c = db.open_store(config.db_path)
    hand.set_setting(c, "time_zone", "UTC")
    yield c
    c.close()


@pytest.fixture
def client(config, conn):
    app = create_app(config, now=lambda: NOW, spawn=lambda args: None)
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def post(client: TestClient, url: str, data: dict | None = None, **kwargs):
    """A POST as PMD's own pages send it: same origin and PMD's header."""
    headers = {"Origin": ORIGIN, "X-PMD-Request": "1", **kwargs.pop("headers", {})}
    return client.post(url, data=data or {}, headers=headers, **kwargs)
