from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

DEFAULT_TOKENS_PER_MINUTE = 50_000


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_ms: int = 60000) -> None:
        super().__init__("Rate limit exceeded")
        self.retry_after_ms = retry_after_ms


@dataclass(slots=True)
class ProviderResult:
    status_code: int
    body: dict[str, Any]


class TenantRateLimiter:
    def __init__(self, db_path: str = "/tmp/llm_gateway_rate_limits.sqlite3", limit_per_minute: int = DEFAULT_TOKENS_PER_MINUTE) -> None:
        self.db_path = db_path
        self.limit_per_minute = limit_per_minute
        self._tenant_locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, int] = {}

    def _tenant_lock(self, tenant_key: str) -> asyncio.Lock:
        if tenant_key not in self._tenant_locks:
            self._tenant_locks[tenant_key] = asyncio.Lock()
        return self._tenant_locks[tenant_key]

    async def ensure_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    tenant_key TEXT NOT NULL,
                    bucket_start INTEGER NOT NULL,
                    tokens INTEGER NOT NULL,
                    PRIMARY KEY (tenant_key, bucket_start)
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_usage_tenant_bucket ON usage (tenant_key, bucket_start)")
            await db.commit()

    async def reserve(self, tenant_key: str, tokens: int = 1) -> None:
        if tokens <= 0:
            return
        self._inflight[tenant_key] = self._inflight.get(tenant_key, 0) + 1
        try:
            now = int(time.time())
            window_start = now - 60
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("DELETE FROM usage WHERE tenant_key = ? AND bucket_start < ?", (tenant_key, window_start))
                cursor = await db.execute(
                    "SELECT bucket_start, tokens FROM usage WHERE tenant_key = ? ORDER BY bucket_start DESC",
                    (tenant_key,),
                )
                rows = await cursor.fetchall()
                current_total = sum(row[1] for row in rows)
                if current_total + tokens > self.limit_per_minute and (tokens > current_total or (current_total == 0 and tokens > self.limit_per_minute)):
                    await db.rollback()
                    raise RateLimitExceeded(retry_after_ms=60000)
                await db.execute(
                    "INSERT INTO usage (tenant_key, bucket_start, tokens) VALUES (?, ?, ?) ON CONFLICT(tenant_key, bucket_start) DO UPDATE SET tokens = usage.tokens + excluded.tokens",
                    (tenant_key, now, tokens),
                )
                await db.commit()
        finally:
            remaining = self._inflight.get(tenant_key, 1) - 1
            if remaining <= 0:
                self._inflight.pop(tenant_key, None)
            else:
                self._inflight[tenant_key] = remaining


def _sanitize_error(code: str, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}}


class MockProvider:
    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url

    async def call(self, prompt: str) -> ProviderResult:
        async with httpx.AsyncClient(timeout=0.5) as client:
            response = await client.post(self.base_url, json={"prompt": prompt})
            if response.status_code == 429:
                raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)
            response.raise_for_status()
            return ProviderResult(response.status_code, response.json())


async def _gateway_request(tenant_key: str, prompt: str, db_path: str, primary_url: str, backup_url: str) -> JSONResponse:
    limiter = TenantRateLimiter(db_path=db_path)
    await limiter.ensure_db()
    try:
        await limiter.reserve(tenant_key, max(1, len(prompt) // 10))
    except RateLimitExceeded:
        return JSONResponse(content=_sanitize_error("rate_limited", "Rate limit exceeded"), status_code=429)

    primary = MockProvider("primary", primary_url)
    backup = MockProvider("backup", backup_url)
    primary_name = getattr(primary, "name", "primary")
    backup_name = getattr(backup, "name", "backup")
    try:
        result = await primary.call(prompt)
        return JSONResponse(content={"provider": primary_name, "result": result.body}, status_code=200)
    except (httpx.TimeoutException, httpx.HTTPStatusError):
        try:
            result = await backup.call(prompt)
            return JSONResponse(content={"provider": backup_name, "result": result.body}, status_code=200)
        except Exception:
            return JSONResponse(content=_sanitize_error("provider_unavailable", "Model unavailable"), status_code=503)
    except Exception:
        return JSONResponse(content=_sanitize_error("provider_error", "Model request failed"), status_code=500)


app = FastAPI(title="Model Router")


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(content=_sanitize_error("invalid_request", "Invalid JSON payload"), status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse(content=_sanitize_error("invalid_request", "Invalid payload"), status_code=400)

    tenant_key = payload.get("tenant")
    if tenant_key is None:
        tenant_key = request.headers.get("X-Tenant-Key")
    if not isinstance(tenant_key, str) or not tenant_key.strip():
        return JSONResponse(content=_sanitize_error("invalid_request", "Invalid tenant"), status_code=400)

    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return JSONResponse(content=_sanitize_error("invalid_request", "Invalid prompt"), status_code=400)

    db_path = "/tmp/llm_gateway_rate_limits.sqlite3"
    return await _gateway_request(tenant_key.strip(), prompt, db_path, "http://primary.mock/v1/chat", "http://backup.mock/v1/chat")


def create_router_app() -> FastAPI:
    return app
