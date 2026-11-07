# LLM MCP Gateway Python

This workspace implements a four-part Python assessment covering MCP server tooling, security gateway enforcement, streaming PII control, and model-routing quotas.

## Project layout

- `src/task1_mcp_server/` — MCP stdio server for customer lookup and refund processing
- `src/task2_security_gateway/` — JSON-RPC gateway with bearer-token checks and downstream proxying
- `src/task3_pii_guardrail/` — streaming redaction for sensitive values in model output
- `src/task4_model_router/` — SQLite-backed tenant limiter with failover routing
- `tests/` — task-specific verification tests

## Setup

Use Python 3.12 and the project virtual environment:

```bash
cd /path/to/llm_mcp_gateway_python
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The repo includes `pytest.ini`, which adds `src` to `PYTHONPATH`, so imports like `task1_mcp_server.server` work without extra shell configuration.

## Run the full verification suite

```bash
python -m pytest -q
```

## Example usage

### 1) Task 1 MCP server

This server runs over stdio and exposes `get_customer_record` and `trigger_refund` tools.

```python
import asyncio
from task1_mcp_server.server import main

asyncio.run(main())
```

### 2) Task 2 gateway

Use `demo-admin-token` or `demo-viewer-token` in the `Authorization` header.

```bash
curl -X POST http://localhost:8000/rpc \
  -H 'Authorization: Bearer demo-admin-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### 3) Task 3 PII redaction

```python
from task3_pii_guardrail.guardrail import _redact_text

payload = "Contact alice@example.com or call 123-45-6789; card 4111 1111 1111 1111."
print(_redact_text(payload))
# Contact [REDACTED] or call [REDACTED]; card [REDACTED].
```

### 4) Task 4 model router

```bash
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"tenant-a","prompt":"Summarize the release notes."}'
```

## Verification status

Fresh check executed in this workspace:

```bash
./.venv/bin/python -m pytest -q
```

Result: `17 passed, 1 warning in 2.05s`.
