from types import SimpleNamespace

import groq
import httpx
import pytest

from app.errors import ErrorCode, classify_llm_error, error_body


def _req():
    return httpx.Request("POST", "https://api.groq.com")


# ── error classification ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "exc,code",
    [
        (groq.APIConnectionError(request=_req()), ErrorCode.LLM_UNAVAILABLE),
        (
            groq.RateLimitError(
                "slow down", response=httpx.Response(429, request=_req()), body=None
            ),
            ErrorCode.LLM_RATE_LIMITED,
        ),
        (
            groq.AuthenticationError(
                "bad key", response=httpx.Response(401, request=_req()), body=None
            ),
            ErrorCode.LLM_AUTH_FAILED,
        ),
        (ValueError("anything else"), ErrorCode.SQL_GENERATION_FAILED),
    ],
)
def test_classify_llm_error(exc, code):
    assert classify_llm_error(exc) == code


def test_error_body_shape():
    body = error_body(ErrorCode.LLM_UNAVAILABLE)
    assert set(body) == {"error", "error_code"}
    assert body["error_code"] == "LLM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_empty_body_returns_standard_422():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/ask", json={})
    assert r.status_code == 422
    assert r.json()["error_code"] == "INVALID_REQUEST"


# ── SQL generator agent ───────────────────────────────────────────────────────

def _agent_that_raises(exc):
    from agents.sql_generator_agent import SQLGeneratorAgent

    def boom(*a, **k):
        raise exc

    agent = SQLGeneratorAgent.__new__(
        SQLGeneratorAgent)  # skip real Groq client
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=boom))
    )
    return agent


def test_generate_connection_error_has_error_code():
    agent = _agent_that_raises(groq.APIConnectionError(request=_req()))
    result = agent.generate("How many customers?", "customers(id)")
    assert result["sql"] is None
    assert result["error_code"] == ErrorCode.LLM_UNAVAILABLE


def test_generate_rate_limit_keeps_fallback_flag():
    exc = groq.RateLimitError(
        "slow down", response=httpx.Response(429, request=_req()), body=None
    )
    agent = _agent_that_raises(exc)
    result = agent.generate("How many customers?", "customers(id)")
    assert result["rate_limited"] is True
    assert result["error_code"] == ErrorCode.LLM_RATE_LIMITED


# ── /ask endpoint ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ask_returns_error_code_on_llm_failure(monkeypatch):
    import app.main as m

    async def fake_tables(q):
        return ["customers"]

    async def fake_format(q):
        return "customers(id)"

    monkeypatch.setattr(
        m.schema_agent, "identify_relevant_tables", fake_tables)
    monkeypatch.setattr(m.schema_agent, "format_for_prompt", fake_format)
    monkeypatch.setattr(
        m.sql_agent,
        "generate",
        lambda q, s: {
            "sql": None,
            "error": "The AI service is unreachable.",
            "error_code": "LLM_UNAVAILABLE",
        },
    )
    monkeypatch.setattr(m.vector_store, "search", lambda q: None)

    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/ask", json={"question": "How many customers?"})

    body = r.json()
    assert r.status_code == 200
    assert body["answer"] is None
    assert body["error_code"] == "LLM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_empty_question_uses_standard_format():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/ask", json={"question": "   "})

    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_REQUEST"


def test_missing_api_key_is_reported_as_auth_error():
    class LocalProtocolError(Exception):
        pass

    exc = groq.APIConnectionError(request=_req())
    exc.__cause__ = LocalProtocolError("Illegal header value b'Bearer '")
    assert classify_llm_error(exc) == ErrorCode.LLM_AUTH_FAILED
