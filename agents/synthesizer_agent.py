"""
agents/synthesizer_agent.py — Agent 4: Synthesizer Agent
----------------------------------------------------------
Responsibilities:
  - Convert raw DB result rows → clear natural-language answer
  - Optionally augment answer with relevant document context
  - Handle empty result sets gracefully
  - Keep answers concise (3–5 sentences)
"""

from groq import Groq
from agents.retriever_agent import RetrieverAgent
from config import GROQ_API_KEY, GROQ_MODEL


_SYSTEM_PROMPT = """You are a concise, helpful data analyst assistant.
Given a user's question, database query results, and optionally some
relevant document excerpts, write a clear, direct natural-language answer.

Guidelines:
- Lead with the answer — state the key finding in the first sentence.
- Use specific numbers, names, and dates from the results.
- If document context is provided and relevant, incorporate it naturally.
- If results are empty, say "No matching records were found" and briefly
  suggest why (e.g. date range, spelling, missing data).
- Maximum 5 sentences. No bullet points. No markdown.
- Do NOT repeat or mention the SQL query.
"""


class SynthesizerAgent:

    def __init__(self) -> None:
        self.client = Groq(api_key=GROQ_API_KEY)

    def synthesize(self, question: str, columns: list, rows: list,
                   doc_hits: list | None = None) -> dict:
        """
        Returns:
            {"answer": "<text>" | None, "error": None | "<message>"}
        """
        results_text = RetrieverAgent.rows_to_text(columns, rows)

        # Build doc context section if available
        doc_section = ""
        if doc_hits:
            excerpts = "\n\n".join(
                f"[From: {d['source']}]\n{d['text'][:900]}"
                for d in doc_hits[:2]
            )
            doc_section = f"\n\nRelevant document excerpts:\n{excerpts}"

        user_msg = (
            f"Question: {question}\n\n"
            f"Database results:\n{results_text}"
            f"{doc_section}"
        )

        try:
            resp = self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=350,
            )
            answer = resp.choices[0].message.content.strip()
            return {"answer": answer, "error": None}

        except Exception as exc:
            # Rate limit — build a plain answer from rows without Groq
            if "429" in str(exc) or "rate_limit" in str(exc).lower():
                fallback = _rows_to_plain_answer(question, columns, rows)
                return {"answer": fallback, "error": None, "from_cache": True}
            return {"answer": None, "error": f"Synthesis failed: {exc}"}


def _rows_to_plain_answer(question: str, columns: list, rows: list) -> str:
    """Generate a simple answer from rows when Groq is unavailable."""
    if not rows:
        return "No matching records were found for your query."
    if len(columns) == 1:
        vals = ", ".join(str(r[0]) for r in rows[:10])
        return f"Results for '{question}': {vals}."
    # Format as readable sentences
    lines = []
    for row in rows[:10]:
        pairs = ", ".join(
            f"{col}: {val}" for col, val in zip(columns, row)
            if val is not None
        )
        lines.append(pairs)
    summary = f"Found {len(rows)} result(s) for '{question}'. "
    summary += " | ".join(lines[:5])
    if len(rows) > 5:
        summary += f" ... and {len(rows) - 5} more."
    return summary
