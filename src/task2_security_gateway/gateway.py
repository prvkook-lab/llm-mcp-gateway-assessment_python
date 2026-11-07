from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROLE_TOKENS = {
    "demo-admin-token": "admin",
    "demo-viewer-token": "viewer",
}


def _jsonrpc_error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    return payload


def create_gateway_app(downstream_url: str = "http://localhost:8001/rpc") -> FastAPI:
    app = FastAPI(title="MCP security gateway")

    async def _forward_request(raw_payload: dict[str, Any], request_id: Any) -> JSONResponse:
        parsed = urlsplit(downstream_url)
        path = parsed.path or "/rpc"
        if path == "/":
            path = "/rpc"
        url = urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=raw_payload, timeout=2.0)
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException:
            return JSONResponse(content=_jsonrpc_error(request_id, -32003, "Downstream timeout"), status_code=200)
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            return JSONResponse(content=_jsonrpc_error(request_id, -32002, "Downstream error"), status_code=200)
        return JSONResponse(content=body, status_code=200)

    @app.post("/rpc")
    async def proxy(request: Request) -> JSONResponse:
        try:
            body = await request.body()
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(content=_jsonrpc_error(None, -32700, "Parse error"), status_code=200)

        if not isinstance(payload, dict):
            return JSONResponse(content=_jsonrpc_error(None, -32600, "Invalid Request"), status_code=200)

        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        if not isinstance(params, dict):
            return JSONResponse(content=_jsonrpc_error(request_id, -32602, "Invalid request parameters"), status_code=200)

        auth = request.headers.get("Authorization")
        if auth is None or not auth.startswith("Bearer "):
            return JSONResponse(
                content=_jsonrpc_error(request_id, -32001, "Unauthorized Tool Call"),
                status_code=200,
            )

        token = auth.split(" ", 1)[1].strip()
        role = ROLE_TOKENS.get(token)
        if role is None:
            return JSONResponse(
                content=_jsonrpc_error(request_id, -32001, "Unauthorized Tool Call"),
                status_code=200,
            )

        if method == "tools/list":
            return await _forward_request(payload, request_id)

        if method == "tools/call":
            tool_name = params.get("name")
            if not isinstance(tool_name, str):
                return JSONResponse(content=_jsonrpc_error(request_id, -32602, "Invalid request parameters"), status_code=200)
            if tool_name.startswith("admin_") and role != "admin":
                return JSONResponse(content=_jsonrpc_error(request_id, -32001, "Unauthorized Tool Call"), status_code=200)
            return await _forward_request(payload, request_id)

        return JSONResponse(content=_jsonrpc_error(request_id, -32601, "Method not found"), status_code=200)

    return app
