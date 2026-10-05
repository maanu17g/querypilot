import groq


class ErrorCode:
    INVALID_REQUEST = "INVALID_REQUEST"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
    LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
    SQL_GENERATION_FAILED = "SQL_GENERATION_FAILED"
    SQL_REJECTED = "SQL_REJECTED"
    SQL_EXECUTION_FAILED = "SQL_EXECUTION_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


MESSAGES = {
    ErrorCode.INVALID_REQUEST: "The request was invalid. Check the question field.",
    ErrorCode.LLM_UNAVAILABLE: "The AI service is unreachable right now. Please try again shortly.",
    ErrorCode.LLM_RATE_LIMITED: "The AI service is rate limited. Please try again in a moment.",
    ErrorCode.LLM_AUTH_FAILED: "The AI service rejected the API key. Check server configuration.",
    ErrorCode.SQL_GENERATION_FAILED: "Could not generate a SQL query for this question.",
    ErrorCode.SQL_REJECTED: "The generated SQL was blocked by the safety checks.",
    ErrorCode.SQL_EXECUTION_FAILED: "The query could not be executed against the database.",
    ErrorCode.INTERNAL_ERROR: "Something went wrong on the server.",
}


def classify_llm_error(exc: Exception) -> str:
    if isinstance(exc, groq.RateLimitError):
        return ErrorCode.LLM_RATE_LIMITED
    if isinstance(exc, groq.AuthenticationError):
        return ErrorCode.LLM_AUTH_FAILED
    if isinstance(exc, (groq.APIConnectionError, groq.APITimeoutError)):
        # An empty/missing API key makes the SDK fail while building the
        # request ("Illegal header value b'Bearer '"), which looks like a
        # connection error but is really a configuration problem.
        if "Illegal header value" in str(exc.__cause__):
            return ErrorCode.LLM_AUTH_FAILED
        return ErrorCode.LLM_UNAVAILABLE
    return ErrorCode.SQL_GENERATION_FAILED


def error_body(code: str, details=None) -> dict:
    body = {"error": MESSAGES[code], "error_code": code}
    if details is not None:
        body["details"] = details
    return body
