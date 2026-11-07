import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
SERVER_ENV = os.environ.copy()
SERVER_ENV["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + SERVER_ENV.get("PYTHONPATH", "")


def _open_client_session():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import asyncio; from task1_mcp_server.server import main; asyncio.run(main())"],
        env=SERVER_ENV,
        cwd=str(ROOT),
    )
    return stdio_client(params)


@pytest.mark.asyncio
async def test_server_lists_tools_and_returns_customer_record():
    async with _open_client_session() as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tool_names = {tool.name for tool in (await session.list_tools()).tools}
            assert {"get_customer_record", "trigger_refund"}.issubset(tool_names)

            record = await session.call_tool("get_customer_record", {"customer_id": "CUST-AB12C"})
            text = record.content[0].text
            assert "AB12C" in text
            assert "alice" in text.lower()

            refund = await session.call_tool(
                "trigger_refund",
                {
                    "customer_id": "CUST-AB12C",
                    "amount": 49.99,
                    "reason": "Duplicate charge on the monthly plan.",
                },
            )
            payload = refund.structured_content
            assert payload["status"] == "approved"
            assert payload["customer_id"] == "CUST-AB12C"


@pytest.mark.asyncio
async def test_invalid_tool_parameters_return_invalid_params():
    async with _open_client_session() as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            with pytest.raises(Exception):
                await session.call_tool(
                    "trigger_refund",
                    {
                        "customer_id": "INVALID",
                        "amount": 19.99,
                        "reason": "too short",
                    },
                )


def test_malformed_jsonrpc_request_returns_parse_or_invalid_request_error():
    proc = subprocess.run(
        [sys.executable, "-c", "import asyncio; from task1_mcp_server.server import main; asyncio.run(main())"],
        input='{"jsonrpc": "2.0", "method": "tools/call", "params": }\n',
        text=True,
        capture_output=True,
        cwd=str(ROOT),
        env=SERVER_ENV,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip()
    response = json.loads(proc.stdout.strip().splitlines()[-1])
    assert response["jsonrpc"] == "2.0"
    assert response["error"]["code"] in {-32700, -32600, -32602}


def test_stdout_only_contains_jsonrpc_and_stderr_is_empty():
    proc = subprocess.run(
        [sys.executable, "-c", "import asyncio; from task1_mcp_server.server import main; asyncio.run(main())"],
        input='{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}\n',
        text=True,
        capture_output=True,
        cwd=str(ROOT),
        env=SERVER_ENV,
    )
    assert proc.returncode == 0
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines
    for line in lines:
        payload = json.loads(line)
        assert payload["jsonrpc"] == "2.0"
    assert proc.stderr == ""
