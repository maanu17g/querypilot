"""
agents/sql_generator_agent.py — Agent 2: SQL Generator Agent
--------------------------------------------------------------
Responsibilities:
  - Convert natural language question → valid PostgreSQL SELECT query
  - Handle temporal references
  - Validate generated SQL for safety
  - Graceful fallback when question is out of scope
<<<<<<< HEAD
  - Robust handling of malformed/empty model responses
=======
  - Standardized error codes (see app/errors.py)
>>>>>>> fb7432c (Standardize API error handling and response messages)
"""

import logging
import re

from groq import Groq

from config import GROQ_API_KEY, GROQ_MODEL
<<<<<<< HEAD
from agents.sql_validator import validate_sql
=======
from app.errors import ErrorCode, MESSAGES, classify_llm_error

logger = logging.getLogger(__name__)
>>>>>>> fb7432c (Standardize API error handling and response messages)


_SYSTEM_PROMPT = """You are an expert PostgreSQL query writer.
Given a database schema and a natural-language question, write ONE valid
PostgreSQL SELECT query that answers the question.

Rules:
- Output ONLY the raw SQL — no markdown, no code fences, no explanation.
- Use table aliases for clarity on multi-table JOINs.
- For temporal references use PostgreSQL date functions:
    • "last year" → EXTRACT(YEAR FROM sale_date) = EXTRACT(YEAR FROM NOW()) - 1
    • "this year" → EXTRACT(YEAR FROM sale_date) = EXTRACT(YEAR FROM NOW())
    • "Q1 2023" → sale_date BETWEEN '2023-01-01' AND '2023-03-31'
    • "this month" → DATE_TRUNC('month', sale_date) = DATE_TRUNC('month', NOW())
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
        Generate SQL from a natural-language question.

        Returns:
<<<<<<< HEAD
            {
                "sql": "<query>" | None,
                "error": None | "<message>"
            }
=======
            {"sql": "<query>", "error": None, "error_code": None}
            or
            {"sql": None, "error": "<message>", "error_code": "<CODE>"}
            (plus "rate_limited": True when Groq returned a 429)
>>>>>>> fb7432c (Standardize API error handling and response messages)
        """

        user_msg = (
            f"Database schema:\n{schema_text}\n\n"
            f"Question: {question}"
        )

        try:
            resp = self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": _SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": user_msg
                    },
                ],
                temperature=0.0,
                max_tokens=512,
            )

            # Handle malformed or empty model responses
            if (
                not getattr(resp, "choices", None)
                or not getattr(resp.choices[0], "message", None)
                or not getattr(resp.choices[0].message, "content", None)
            ):
                return {
                    "sql": None,
                    "error": "SQL generation returned an empty response."
                }

            raw = resp.choices[0].message.content.strip()

            if not raw:
                return {
                    "sql": None,
                    "error": "SQL generation returned an empty response."
                }

            # Remove accidental markdown code fences
            raw = re.sub(
                r"```[a-zA-Z]*",
                "",
                raw
            ).replace("```", "").strip()

            if not raw:
                return {
                    "sql": None,
                    "error": "SQL generation returned an empty response."
                }

            # Handle unsupported questions
            if raw.upper().startswith("UNSUPPORTED_QUERY"):
<<<<<<< HEAD
                return {
                    "sql": None,
                    "error": (
                        "This question cannot be answered "
                        "from the available schema."
                    )
                }

            # Validate generated SQL before returning it
            is_valid, validation_error = validate_sql(raw)

            if not is_valid:
                return {
                    "sql": None,
                    "error": validation_error
                }

            return {
                "sql": raw,
                "error": None
            }
=======
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
>>>>>>> fb7432c (Standardize API error handling and response messages)

        except Exception as exc:
            code = classify_llm_error(exc)
            logger.error("SQL generation failed [%s]: %r | cause=%r",
                         code, exc, exc.__cause__)

            err_str = str(exc)
<<<<<<< HEAD

            if "429" in err_str or "rate_limit" in err_str.lower():
                return {
                    "sql": None,
                    "error": "RATE_LIMIT",
                    "rate_limited": True
                }

            return {
                "sql": None,
                "error": f"SQL generation failed: {exc}"
            }
=======
            if (code == ErrorCode.LLM_RATE_LIMITED
                    or "429" in err_str
                    or "rate_limit" in err_str.lower()):
                # Pipeline checks this flag to fall back to the vector cache
                return _fail(ErrorCode.LLM_RATE_LIMITED, "RATE_LIMIT",
                             rate_limited=True)

            return _fail(code)
>>>>>>> fb7432c (Standardize API error handling and response messages)

    def fix(self, question: str, bad_sql: str, db_error: str) -> dict:
        """
        Attempt to correct SQL that failed during database execution.

        Returns:
            {
                "sql": "<corrected query>" | None,
                "error": None | "<message>"
            }
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
                    {
                        "role": "system",
                        "content": _SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": user_msg
                    },
                ],
                temperature=0.0,
                max_tokens=512,
            )

            # Handle malformed or empty model responses
            if (
                not getattr(resp, "choices", None)
                or not getattr(resp.choices[0], "message", None)
                or not getattr(resp.choices[0].message, "content", None)
            ):
                return {
                    "sql": None,
                    "error": "SQL fix returned an empty response."
                }

            raw = resp.choices[0].message.content.strip()
<<<<<<< HEAD

            if not raw:
                return {
                    "sql": None,
                    "error": "SQL fix returned an empty response."
                }

            # Remove accidental markdown code fences
            raw = re.sub(
                r"```[a-zA-Z]*",
                "",
                raw
            ).replace("```", "").strip()

            if not raw:
                return {
                    "sql": None,
                    "error": "SQL fix returned an empty response."
                }

            # Validate corrected SQL before retrying it
            is_valid, validation_error = validate_sql(raw)

            if not is_valid:
                return {
                    "sql": None,
                    "error": (
                        f"Fix attempt produced unsafe SQL: "
                        f"{validation_error}"
                    )
                }

            return {
                "sql": raw,
                "error": None
            }

        except Exception as exc:
            return {
                "sql": None,
                "error": f"Fix attempt failed: {exc}"
            }
=======
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
>>>>>>> fb7432c (Standardize API error handling and response messages)
