"""
agents/sql_generator_agent.py — Agent 2: SQL Generator Agent
--------------------------------------------------------------
Responsibilities:
  - Convert natural language question → valid PostgreSQL SELECT query
  - Handle temporal references (last year, Q1 2023, this month…)
  - SQL safety checks (SELECT / WITH only)
  - Graceful fallback when question is out of scope
  - Standardized error codes (see app/errors.py)
"""

import logging
import re
from groq import Groq
from config import GROQ_API_KEY, GROQ_MODEL
from app.errors import ErrorCode, MESSAGES, classify_llm_error

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are an expert PostgreSQL query writer.
Given a database schema and a natural-language question, write ONE valid
PostgreSQL SELECT query that answers the question.

Rules:
- Output ONLY the raw SQL — no markdown, no code fences, no explanation.
- Use table aliases for clarity on multi-table JOINs.
- For temporal references use PostgreSQL date functions:
    • "last year"   → EXTRACT(YEAR FROM sale_date) = EXTRACT(YEAR FROM NOW()) - 1
    • "this year"   → EXTRACT(YEAR FROM sale_date) = EXTRACT(YEAR FROM NOW())
    • "Q1 2023"     → sale_date BETWEEN '2023-01-01' AND '2023-03-31'
    • "this month"  → DATE_TRUNC('month', sale_date) = DATE_TRUNC('month', NOW())
- Never use DROP, INSERT, UPDATE, DELETE, TRUNCATE, or any DDL/DML.
- If the question cannot be answered from the provided schema, output exactly:
  UNSUPPORTED_QUERY
"""


def _fail(code: str, message: str | None = None, **extra) -> dict:
    """Build a standard failure result."""
    result = {
        "sql": None,
        "error": message or MESSAGES[code],
        "error_code": code,
    }
    result.update(extra)
    return result


class SQLGeneratorAgent:

    def __init__(self) -> None:
        self.client = Groq(api_key=GROQ_API_KEY)

    def generate(self, question: str, schema_text: str) -> dict:
        """
        Returns:
            {"sql": "<query>", "error": None, "error_code": None}
            or
            {"sql": None, "error": "<message>", "error_code": "<CODE>"}
            (plus "rate_limited": True when Groq returned a 429)
        """
        user_msg = (
            f"Database schema:\n{schema_text}\n\n"
            f"Question: {question}"
        )

        try:
            resp = self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=512,
            )
            raw = resp.choices[0].message.content.strip()

            # Strip accidental markdown fences
            raw = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()

            if raw.upper().startswith("UNSUPPORTED_QUERY"):
                return _fail(
                    ErrorCode.SQL_GENERATION_FAILED,
                    "This question cannot be answered from the available schema.",
                )

            # Safety gate — only SELECT / WITH allowed
            first_word = raw.split()[0].upper() if raw.split() else ""
            if first_word not in ("SELECT", "WITH"):
                return _fail(
                    ErrorCode.SQL_REJECTED,
                    f"Unsafe SQL generated (starts with '{first_word}'). Blocked.",
                )

            return {"sql": raw, "error": None, "error_code": None}

        except Exception as exc:
            code = classify_llm_error(exc)
            logger.error("SQL generation failed [%s]: %r | cause=%r",
                         code, exc, exc.__cause__)

            err_str = str(exc)
            if (code == ErrorCode.LLM_RATE_LIMITED
                    or "429" in err_str
                    or "rate_limit" in err_str.lower()):
                # Pipeline checks this flag to fall back to the vector cache
                return _fail(ErrorCode.LLM_RATE_LIMITED, "RATE_LIMIT",
                             rate_limited=True)

            return _fail(code)

    def fix(self, question: str, bad_sql: str, db_error: str) -> dict:
        """
        Called by RetrieverAgent on failure.
        Sends the broken SQL + DB error back to Groq and asks for a corrected query.
        """
        user_msg = (
            f"The following SQL query failed with this error:\n\n"
            f"SQL:\n{bad_sql}\n\n"
            f"Error:\n{db_error}\n\n"
            f"Original question: {question}\n\n"
            f"Please write a corrected PostgreSQL SELECT query."
        )
        try:
            resp = self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=512,
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
            first_word = raw.split()[0].upper() if raw.split() else ""
            if first_word not in ("SELECT", "WITH"):
                return _fail(ErrorCode.SQL_REJECTED,
                             "Fix attempt produced unsafe SQL.")
            return {"sql": raw, "error": None, "error_code": None}
        except Exception as exc:
            code = classify_llm_error(exc)
            logger.error("SQL fix attempt failed [%s]: %r | cause=%r",
                         code, exc, exc.__cause__)
            return _fail(code, f"Fix attempt failed: {MESSAGES[code]}")
