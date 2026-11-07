from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request


app = FastAPI(title="Mock downstream MCP server")


def _jsonrpc_response(request_id: Any, result: Any | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


@app.post("/rpc")
async def handle_rpc(request: Request) -> dict[str, Any]:
    payload = await request.json()
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if method == "tools/list":
        return _jsonrpc_response(
            request_id,
            {
                "tools": [
                    {"name": "echo_message", "description": "Echo back a message"},
                    {"name": "admin_reset_cache", "description": "Reset cached data"},
                ]
            },
        )

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name == "echo_message":
            text = arguments.get("text", "")
            return _jsonrpc_response(request_id, {"content": [{"type": "text", "text": text}]})

        if name == "admin_reset_cache":
            return _jsonrpc_response(request_id, {"status": "reset"})

        return _jsonrpc_response(request_id, error={"code": -32601, "message": "Method not found"})

    return _jsonrpc_response(request_id, error={"code": -32601, "message": "Method not found"})


def create_downstream_app() -> FastAPI:
    return app
