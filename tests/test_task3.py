from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from task3_pii_guardrail.guardrail import StreamingPiiGuardrail, _redact_text, redact_stream


def test_redacts_email_ssn_and_card_numbers_from_full_text():
    payload = "Contact alice@example.com or call 123-45-6789; card 4111 1111 1111 1111."
    assert _redact_text(payload) == "Contact [REDACTED] or call [REDACTED]; card [REDACTED]."


def test_handles_cross_chunk_pii_splits():
    chunks = ["Contact alic", "e@example.com and SSN 123-", "45-6789; card 4111 1111 1111 ", "1111."]
    output = "".join(redact_stream(chunks))
    assert "alice@example.com" not in output
    assert "123-45-6789" not in output
    assert "4111 1111 1111 1111" not in output
    assert "[REDACTED]" in output


def test_non_pii_text_is_preserved():
    payload = "This is a normal update without any sensitive text."
    assert _redact_text(payload) == payload


def test_memory_use_is_bounded_for_repeated_small_chunks():
    stream = StreamingPiiGuardrail(max_lookbehind=32)
    output = []
    for idx in range(200):
        output.append(stream.process_chunk(f"hello-{idx}-"))
    assert sum(len(part) for part in output) < 5000
    assert "hello" in "".join(output)


def test_streaming_response_emits_redacted_text_without_partial_sensitive_leaks():
    client = TestClient(__import__('task3_pii_guardrail.guardrail', fromlist=['create_guardrail_app']).create_guardrail_app())
    response = client.get("/stream")
    assert response.status_code == 200
    payload = response.text
    assert "alice@example.com" not in payload
    assert "123-45-6789" not in payload
    assert "4111 1111 1111 1111" not in payload
    assert "[REDACTED]" in payload
