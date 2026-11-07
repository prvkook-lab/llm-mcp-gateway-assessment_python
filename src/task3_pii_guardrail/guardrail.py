from __future__ import annotations

import re
from typing import AsyncIterator, Iterable

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){15,19}(?!\d)")

PATTERN_ORDER = (EMAIL_RE, SSN_RE, CARD_RE)
MAX_LOOKBEHIND = 128


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in PATTERN_ORDER:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


class StreamingPiiGuardrail:
    """Bounded streaming redactor that holds a small tail buffer until possible PII is classified."""

    def __init__(self, max_lookbehind: int = MAX_LOOKBEHIND) -> None:
        self.pending = ""
        self.max_lookbehind = max_lookbehind

    def process_chunk(self, chunk: str) -> str:
        if not chunk:
            return ""
        combined = self.pending + chunk
        safe_length = max(0, len(combined) - self.max_lookbehind)
        ready = combined[:safe_length]
        self.pending = combined[safe_length:]
        return _redact_text(ready)

    def flush(self) -> str:
        pending = self.pending
        self.pending = ""
        return _redact_text(pending)


def redact_stream(chunks: Iterable[str]) -> Iterable[str]:
    guardrail = StreamingPiiGuardrail()
    for chunk in chunks:
        yield guardrail.process_chunk(chunk)
    tail = guardrail.flush()
    if tail:
        yield tail


async def mock_llm_stream() -> AsyncIterator[str]:
    content = [
        "Hi Alice, contact alice.smith@example.com soon. ",
        "Your SSN is 123-45-6789 and card 4111 1111 1111 1111. ",
        "Thanks for using the portal.",
    ]
    for item in content:
        yield item


app = FastAPI(title="Streaming PII Guardrail")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stream")
async def stream_response() -> StreamingResponse:
    async def generator() -> AsyncIterator[bytes]:
        async for chunk in mock_llm_stream():
            redacted = "".join(redact_stream([chunk]))
            yield redacted.encode("utf-8")

    return StreamingResponse(generator(), media_type="text/plain")


def create_guardrail_app() -> FastAPI:
    return app
