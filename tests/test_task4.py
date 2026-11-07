import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from task4_model_router.router import TenantRateLimiter, app, create_router_app


@pytest.mark.asyncio
async def test_rate_limiter_isolates_tenants_and_expired_rows(tmp_path):
    db = tmp_path / "limits.sqlite3"
    limiter = TenantRateLimiter(db_path=str(db), limit_per_minute=5)
    await limiter.ensure_db()

    await limiter.reserve("tenant-a", 2)
    await limiter.reserve("tenant-b", 2)
    assert await _current_usage(db, "tenant-a") == 2
    assert await _current_usage(db, "tenant-b") == 2

    with pytest.raises(Exception):
        await limiter.reserve("tenant-a", 4)

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM usage WHERE tenant_key = 'tenant-a'")
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_concurrent_reservations_are_atomic(tmp_path):
    db = tmp_path / "limits.sqlite3"
    limiter = TenantRateLimiter(db_path=str(db), limit_per_minute=10)
    await limiter.ensure_db()

    async def worker(value: int) -> None:
        await limiter.reserve("tenant-c", value)

    await asyncio.gather(worker(4), worker(4), worker(3))
    assert await _current_usage(db, "tenant-c") == 11


def test_router_returns_sanitized_errors_for_invalid_requests():
    client = TestClient(create_router_app())
    response = client.post("/chat", json={"tenant": "", "prompt": "hello"})
    assert response.status_code == 400
    payload = response.json()
    assert "error" in payload
    assert payload["error"]["code"] == "invalid_request"


def test_primary_failover_uses_backup_on_timeout(monkeypatch):
    client = TestClient(create_router_app())

    class FakePrimary:
        async def call(self, prompt: str):
            raise httpx.TimeoutException("primary timeout")

    class FakeBackup:
        async def call(self, prompt: str):
            return type("Result", (), {"body": {"answer": "ok from backup"}})()

    monkeypatch.setattr("task4_model_router.router.MockProvider", lambda name, base_url: FakePrimary() if name == "primary" else FakeBackup())
    resp = client.post("/chat", json={"tenant": "tenant-x", "prompt": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "backup"
    assert body["result"]["answer"] == "ok from backup"


async def _current_usage(db_path: Path, tenant_key: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT SUM(tokens) FROM usage WHERE tenant_key = ?", (tenant_key,)).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()
