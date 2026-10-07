"""
app/main.py
-----------
FastAPI entrypoint.

Endpoints:
  POST /ask         - 4-agent pipeline, returns structured JSON answer
                      (supports pagination via `page` and `page_size`)
  POST /upload-doc  - ingest a PDF / text file into the document store
  GET  /docs-list   - list ingested documents
  GET  /schema      - returns full DB schema as JSON
  GET  /health      - DB connectivity check
  GET  /run-tests   - runs the API test-suite and returns the results
  GET  /            - serves the HTML frontend

All errors carry a stable `error_code` (see app/errors.py).
"""

import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import ErrorCode, MESSAGES, classify_llm_error, error_body
from database.connection import init_pool, close_pool
from agents.schema_agent import SchemaAgent
from agents.sql_generator_agent import SQLGeneratorAgent
from agents.retriever_agent import RetrieverAgent, MAX_PAGE_SIZE
from agents.synthesizer_agent import SynthesizerAgent
from agents.vector_store import VectorStore
from agents.doc_store import DocStore

logger = logging.getLogger(__name__)


# -- lifespan (startup / shutdown) ---------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()          # create asyncpg pool + ensure schema exists
    yield
    await close_pool()         # graceful shutdown


# -- app -----------------------------------------------------------------------

app = FastAPI(
    title="QueryPilot - Multi-Agent SQL Assistant",
    description="Natural language -> PostgreSQL -> human-readable answer, via a 4-agent pipeline.",
    version="1.0.0",
    lifespan=lifespan,
)


# -- global error handlers (standard format everywhere) ------------------------

@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=error_body(ErrorCode.INVALID_REQUEST,
                           jsonable_encoder(exc.errors())),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    code = (ErrorCode.INVALID_REQUEST if 400 <= exc.status_code < 500
            else ErrorCode.INTERNAL_ERROR)
    message = str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        # "detail" kept for backward compatibility with older clients/tests
        content={"error": message, "error_code": code, "detail": message},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content=error_body(ErrorCode.INTERNAL_ERROR))


# Serve frontend files at /static/*
_FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")

# -- agent singletons (shared across requests) ---------------------------------
schema_agent = SchemaAgent()
sql_agent = SQLGeneratorAgent()
retriever_agent = RetrieverAgent()
vector_store = VectorStore()
doc_store = DocStore()
synth_agent = SynthesizerAgent()


# -- models --------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str
    page: int = 1
    page_size: int = 50


class AskResponse(BaseModel):
    question:        str
    answer:          str | None
    sql_query:       str | None
    columns:         list
    rows:            list
    row_count:       int

    # Pagination information
    total_rows:      int = 0
    page:            int = 1
    page_size:       int = 50
    has_next:        bool = False

    relevant_tables: list[str]
    retried:         bool = False
    from_cache:      bool = False
    doc_context:     list = []
    error:           str | None
    error_code:      str | None = None


# -- /ask ----------------------------------------------------------------------

@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(
            status_code=400, detail="Question cannot be empty.")

    # Validate pagination
    if req.page < 1:
        raise HTTPException(
            status_code=400,
            detail="Page must be greater than or equal to 1.")

    if req.page_size < 1 or req.page_size > MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Page size must be between 1 and {MAX_PAGE_SIZE}.")

    def err(msg: str, code: str, **kwargs) -> AskResponse:
        return AskResponse(
            question=question, answer=None,
            sql_query=kwargs.get("sql"),
            columns=[], rows=[], row_count=0,
            total_rows=0, page=req.page, page_size=req.page_size,
            has_next=False,
            relevant_tables=kwargs.get("tables", []),
            from_cache=False, error=msg, error_code=code,
        )

    # Step 1 - Schema Agent (identifies relevant tables via Groq)
    try:
        relevant_tables = await schema_agent.identify_relevant_tables(question)
        schema_text = await schema_agent.format_for_prompt(question)
    except Exception as exc:
        code = classify_llm_error(exc)
        if code == ErrorCode.SQL_GENERATION_FAILED:
            code = ErrorCode.INTERNAL_ERROR      # not an LLM problem
        logger.error("Schema Agent failed [%s]: %r | cause=%r",
                     code, exc, exc.__cause__)
        msg = (MESSAGES[code] if code != ErrorCode.INTERNAL_ERROR
               else f"Schema Agent error: {exc}")
        return err(msg, code)

    # Step 2 - SQL Generator Agent
    sql_result = sql_agent.generate(question, schema_text)

    # -- Vector RAG fallback if Groq is rate-limited or SQL fails --------------
    if sql_result.get("rate_limited") or sql_result["error"]:
        cached = vector_store.search(question)
        if cached and cached.get("sql"):
            # Re-execute the cached SQL against the DB for fresh rows
            db_result = await retriever_agent.execute(
                cached["sql"], question, None,
                page=req.page, page_size=req.page_size,
            )
            if not db_result["error"]:
                cols = db_result["columns"]
                rows = db_result["rows"]
                row_count = db_result["row_count"]
                # Build a simple answer from rows without calling Groq
                answer = cached["answer"]
                return AskResponse(
                    question=question,
                    answer=answer,
                    sql_query=cached["sql"],
                    columns=cols,
                    rows=rows,
                    row_count=row_count,
                    total_rows=db_result.get("total_rows", row_count),
                    page=db_result.get("page", req.page),
                    page_size=db_result.get("page_size", req.page_size),
                    has_next=db_result.get("has_next", False),
                    relevant_tables=cached["tables"],
                    retried=False,
                    from_cache=True,
                    doc_context=[],
                    error=None,
                    error_code=None,
                )
        # No cache hit - return a friendly message with a stable error code
        code = sql_result.get("error_code") or ErrorCode.SQL_GENERATION_FAILED
        wait_msg = "Groq rate limit reached. Please wait a few minutes and try again."
        if sql_result.get("error") and sql_result["error"] != "RATE_LIMIT":
            wait_msg = sql_result["error"]
        return err(wait_msg, code, tables=relevant_tables)

    sql_query = sql_result["sql"]

    # Step 3 - Retriever Agent (validation, pagination, auto-retry via SQL Generator)
    db_result = await retriever_agent.execute(
        sql_query, question, sql_agent,
        page=req.page, page_size=req.page_size,
    )
    if db_result["error"]:
        logger.error("SQL execution failed: %s | sql=%s",
                     db_result["error"], sql_query)
        code = (ErrorCode.SQL_REJECTED if db_result.get("validation_error")
                else ErrorCode.SQL_EXECUTION_FAILED)
        return err(db_result["error"], code,
                   tables=relevant_tables,
                   sql=db_result.get("sql_used", sql_query))

    columns = db_result["columns"]
    rows = db_result["rows"]
    row_count = db_result["row_count"]
    sql_used = db_result.get("sql_used", sql_query)
    retried = db_result.get("retried", False)

    # Pagination metadata
    total_rows = db_result.get("total_rows", row_count)
    page = db_result.get("page", req.page)
    page_size = db_result.get("page_size", req.page_size)
    has_next = db_result.get("has_next", False)

    # Step 4 - Document store search (augment context if relevant docs exist)
    doc_hits = doc_store.search(question, n_results=2)
    doc_context = [{"source": d["source"], "text": d["text"][:300], "score": d["score"]}
                   for d in doc_hits]

    # Step 5 - Synthesizer Agent (with optional doc context)
    synth_result = synth_agent.synthesize(question, columns, rows, doc_hits)
    synth_from_cache = synth_result.get("from_cache", False)
    if synth_result["error"]:
        logger.error("Synthesizer failed: %s", synth_result["error"])
        return AskResponse(
            question=question, answer=None, sql_query=sql_used,
            columns=columns, rows=rows, row_count=row_count,
            total_rows=total_rows, page=page, page_size=page_size,
            has_next=has_next,
            relevant_tables=relevant_tables, retried=retried,
            from_cache=False, doc_context=doc_context,
            error=synth_result["error"],
            error_code=synth_result.get(
                "error_code") or ErrorCode.INTERNAL_ERROR,
        )

    # -- Store successful result in vector cache -------------------------------
    vector_store.store(
        question, synth_result["answer"], sql_used, relevant_tables)

    return AskResponse(
        question=question,
        answer=synth_result["answer"],
        sql_query=sql_used,
        columns=columns,
        rows=rows,
        row_count=row_count,
        total_rows=total_rows,
        page=page,
        page_size=page_size,
        has_next=has_next,
        relevant_tables=relevant_tables,
        retried=retried,
        from_cache=synth_from_cache,
        doc_context=doc_context,
        error=None,
        error_code=None,
    )


# -- /upload-doc ---------------------------------------------------------------

@app.post("/upload-doc")
async def upload_doc(file: UploadFile = File(...)):
    """Upload a PDF or text file into the document knowledge base."""
    allowed = {".pdf", ".txt", ".md"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported file type '{ext}'. Allowed: {allowed}")

    # Save to temp file, ingest, delete
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        result = doc_store.ingest_file(tmp_path, file.filename)
    finally:
        os.unlink(tmp_path)

    if result["error"]:
        raise HTTPException(status_code=422, detail=result["error"])

    return {
        "message":  f"'{file.filename}' ingested successfully.",
        "chunks":   result["chunks"],
        "total_doc_chunks": doc_store.count(),
    }


# -- /docs-list ----------------------------------------------------------------

@app.get("/docs-list")
async def list_docs():
    """List all ingested documents."""
    return {
        "documents":   doc_store.list_documents(),
        "total_chunks": doc_store.count(),
    }


# -- /schema -------------------------------------------------------------------

@app.get("/schema")
async def get_schema():
    """Return the full DB schema as structured JSON."""
    try:
        schema = await schema_agent.get_schema()
        return {"schema": schema}
    except Exception as exc:
        logger.error("Schema fetch failed: %r", exc)
        return JSONResponse(status_code=500,
                            content=error_body(ErrorCode.INTERNAL_ERROR))


# -- /health -------------------------------------------------------------------

@app.get("/health")
async def health():
    try:
        tables = await schema_agent.get_table_names()
        return {"status": "ok", "tables": tables}
    except Exception as exc:
        logger.error("Health check failed: %r", exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": str(exc),
                     **error_body(ErrorCode.INTERNAL_ERROR)},
        )


# -- /run-tests ----------------------------------------------------------------

@app.get("/run-tests")
async def run_tests():
    """Run pytest on tests/test_api.py and return structured results."""
    import subprocess
    import sys
    import re
    import asyncio

    def _run():
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_api.py", "-v", "--tb=short"],
            capture_output=True, text=True,
        )
        return result.stdout + result.stderr

    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, _run)

    # Parse individual test results
    tests = []
    for line in output.splitlines():
        m = re.match(r"tests[\\/].+::.+\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
        if m:
            status = m.group(1)
            name = line.split("::")[1].split()[0] if "::" in line else line
            tests.append({"name": name, "status": status})

    # Summary line
    summary_match = re.search(
        r"(\d+ passed)?[,\s]*(\d+ failed)?[,\s]*(\d+ error)?.*in ([\d.]+)s", output)
    summary = summary_match.group(0) if summary_match else "unknown"

    passed = sum(1 for t in tests if t["status"] == "PASSED")
    failed = sum(1 for t in tests if t["status"] in ("FAILED", "ERROR"))

    return {
        "passed":  passed,
        "failed":  failed,
        "total":   len(tests),
        "summary": summary,
        "tests":   tests,
        "log":     output,
    }


# -- / (frontend) --------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(os.path.join(_FRONTEND, "index.html"))
