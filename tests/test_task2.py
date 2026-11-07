import httpx
import pytest
from fastapi.testclient import TestClient

from task2_security_gateway.downstream import create_downstream_app
from task2_security_gateway.gateway import create_gateway_app


@pytest.fixture
def downstream_transport():
    app = create_downstream_app()
    transport = httpx.ASGITransport(app=app)
    return transport


@pytest.fixture
def gateway_client(monkeypatch, downstream_transport):
    downstream_calls = {"count": 0}

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=downstream_transport, timeout=0.5, *args, **kwargs)

        async def post(self, *args, **kwargs):
            downstream_calls["count"] += 1
            return await super().post(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)

    app = create_gateway_app(downstream_url="http://downstream.test")
    return TestClient(app), downstream_calls


def test_tools_list_is_forwarded_for_viewer(gateway_client):
    client, _ = gateway_client
    response = client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {}},
        headers={"Authorization": "Bearer demo-viewer-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 42
    assert payload["result"]["tools"][0]["name"] in {"echo_message", "admin_reset_cache"}


def test_admin_tool_requires_admin_role_and_does_not_hit_downstream(gateway_client):
    client, downstream_calls = gateway_client
    response = client.post(
        "/rpc",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "admin_reset_cache", "arguments": {"scope": "all"}},
        },
        headers={"Authorization": "Bearer demo-viewer-token"},
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["error"]["code"] == -32001
    assert payload["error"]["message"] == "Unauthorized Tool Call"
    assert downstream_calls["count"] == 0


def test_missing_or_malformed_payloads_are_rejected(gateway_client):
    client, _ = gateway_client
    missing_auth = client.post("/rpc", json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "echo_message"}})
    assert missing_auth.status_code in {401, 200}
    assert missing_auth.json()["error"]["code"] in {-32001, -32600, -32700}

    malformed = client.post("/rpc", data="{not valid json", headers={"Authorization": "Bearer demo-admin-token"})
    assert malformed.status_code == 200
    payload = malformed.json()
    assert payload["error"]["code"] in {-32700, -32600}


def test_downstream_timeout_is_propagated_as_jsonrpc_error(gateway_client, monkeypatch):
    client, _ = gateway_client

    async def timeout_post(*args, **kwargs):
        raise httpx.TimeoutException("downstream timeout")

    monkeypatch.setattr(httpx.AsyncClient, "post", timeout_post)

    response = client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "echo_message", "arguments": {"text": "hello"}}},
        headers={"Authorization": "Bearer demo-admin-token"},
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["error"]["code"] in {-32002, -32003}
    assert "timeout" in payload["error"]["message"].lower()
