import asyncio
import json
import math
import re
import sys
from collections.abc import Mapping
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from pydantic import BaseModel, ConfigDict, Field, field_validator

CUSTOMER_ID_PATTERN = re.compile(r"^CUST-[A-Z0-9]{5}$")
"""Customer IDs follow the assessment rule CUST-XXXXX.

We intentionally allow only uppercase letters and digits in the five-character suffix,
which keeps the ID format predictable and matches the strict validation requirement.
"""

CUSTOMER_RECORDS = {
    "CUST-AB12C": {
        "customer_id": "CUST-AB12C",
        "name": "Alice Johnson",
        "email": "alice.johnson@example.com",
        "tier": "gold",
        "status": "active",
        "last_order_total": 245.78,
    },
    "CUST-Z9Q7M": {
        "customer_id": "CUST-Z9Q7M",
        "name": "Marcus Lee",
        "email": "marcus.lee@example.com",
        "tier": "silver",
        "status": "active",
        "last_order_total": 134.22,
    },
    "CUST-4NX2P": {
        "customer_id": "CUST-4NX2P",
        "name": "Priya Patel",
        "email": "priya.patel@example.com",
        "tier": "platinum",
        "status": "inactive",
        "last_order_total": 412.0,
    },
}


class CustomerRecordArgs(BaseModel):
    model_config = ConfigDict(strict=True)

    customer_id: str = Field(..., description="Customer identifier in the CUST-XXXXX format")

    @field_validator("customer_id")
    @classmethod
    def validate_customer_id(cls, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("customer_id must be a string")
        if not CUSTOMER_ID_PATTERN.fullmatch(value):
            raise ValueError("customer_id must match CUST-XXXXX using uppercase letters and digits")
        return value


class RefundArgs(CustomerRecordArgs):
    model_config = ConfigDict(strict=True)

    amount: float = Field(..., gt=0, description="Refund amount in USD")
    reason: str = Field(..., min_length=10, description="Reason for refund")

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("amount must be a positive finite number and cannot be a boolean")
        if not isinstance(value, (int, float)):
            raise TypeError("amount must be a positive finite number")
        amount = float(value)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("amount must be a positive finite number")
        return amount

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("reason must be a string")
        if len(value.strip()) < 10:
            raise ValueError("reason must be at least 10 characters long")
        return value


def _validate_tool_arguments(model: type[BaseModel], raw_arguments: Mapping[str, Any] | None) -> BaseModel:
    if raw_arguments is None:
        raise MCPError(INVALID_PARAMS, "Missing tool arguments")
    try:
        return model.model_validate(dict(raw_arguments), by_name=False)
    except Exception as exc:  # pragma: no cover - exercised by tests at the protocol boundary
        raise MCPError(INVALID_PARAMS, "Invalid request parameters") from exc


async def _list_tools(_: Any, __: Any) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="get_customer_record",
                description="Fetch a mock customer record by customer ID.",
                inputSchema={
                    "type": "object",
                    "properties": {"customer_id": {"type": "string", "pattern": "^CUST-[A-Z0-9]{5}$"}},
                    "required": ["customer_id"],
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="trigger_refund",
                description="Process a mock refund for a known customer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string", "pattern": "^CUST-[A-Z0-9]{5}$"},
                        "amount": {"type": "number", "exclusiveMinimum": 0},
                        "reason": {"type": "string", "minLength": 10},
                    },
                    "required": ["customer_id", "amount", "reason"],
                    "additionalProperties": False,
                },
            ),
        ]
    )


async def _get_customer_record(_: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    validated = _validate_tool_arguments(CustomerRecordArgs, params.arguments)
    customer_id = validated.customer_id
    record = CUSTOMER_RECORDS.get(customer_id)
    if record is None:
        raise MCPError(INVALID_PARAMS, f"Unknown customer_id: {customer_id}")

    payload = dict(record)
    text = json.dumps({"customer_id": customer_id, "record": payload}, sort_keys=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content={"customer_id": customer_id, "record": payload},
    )


async def _trigger_refund(_: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    validated = _validate_tool_arguments(RefundArgs, params.arguments)
    customer_id = validated.customer_id
    record = CUSTOMER_RECORDS.get(customer_id)
    if record is None:
        raise MCPError(INVALID_PARAMS, f"Unknown customer_id: {customer_id}")

    refund_id = f"REF-{customer_id[-5:]}-{len(CUSTOMER_RECORDS):02d}"
    result = {
        "customer_id": customer_id,
        "refund_id": refund_id,
        "amount": float(validated.amount),
        "reason": validated.reason,
        "status": "approved",
        "currency": "USD",
        "customer_name": record["name"],
    }
    text = json.dumps(result, sort_keys=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=result,
    )


class _SafeStdioOutput:
    def __init__(self, wrapped: Any):
        self._wrapped = wrapped

    async def write(self, data: str) -> None:
        await self._wrapped.write(data)

    async def flush(self) -> None:
        await self._wrapped.flush()


class _SafeStdioInput:
    """Translate malformed lines into RFC 8259 JSON-RPC parse errors before the SDK dispatcher sees them."""

    def __init__(self, wrapped: Any, writer: _SafeStdioOutput):
        self._wrapped = wrapped
        self._writer = writer

    def __aiter__(self) -> "_SafeStdioInput":
        return self

    async def __anext__(self) -> str:
        while True:
            line = await self._wrapped.readline()
            if line == "":
                raise StopAsyncIteration
            stripped = line.strip()
            if not stripped:
                continue
            try:
                json.loads(stripped)
            except json.JSONDecodeError:
                message = json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                })
                await self._writer.write(message + "\n")
                await self._writer.flush()
                continue
            return line


server = Server(
    "customer-ops",
    version="1.0.0",
    on_list_tools=_list_tools,
    on_call_tool=lambda ctx, params: _dispatch_tool_call(ctx, params),
)


async def _dispatch_tool_call(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    name = params.name
    if name == "get_customer_record":
        return await _get_customer_record(ctx, params)
    if name == "trigger_refund":
        return await _trigger_refund(ctx, params)
    raise MCPError(-32601, f"Unknown tool: {name}")


async def main() -> None:
    safe_stdout = _SafeStdioOutput(anyio.wrap_file(sys.stdout))
    safe_stdin = _SafeStdioInput(anyio.wrap_file(sys.stdin), safe_stdout)
    async with stdio_server(stdin=safe_stdin, stdout=safe_stdout) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options(), raise_exceptions=False)


if __name__ == "__main__":
    asyncio.run(main())
