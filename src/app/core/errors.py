"""HTTP exception handlers and the shared error response model.

Moved from ``app.core.errors.py``. ``ErrorResponse`` came from
``app.auth.schemas.py``: it is declared in the ``responses={}`` block
of routes in every one of the 13 contexts, so it belongs with the error
machinery rather than inside any single context's schemas.

The RFC 9457 Problem Details contract (R-ERR-2) is a separate, deferred piece
of work — see spec §5. This module currently preserves the pre-migration
``{"success": false, "error": ...}`` envelope exactly.
"""

from pydantic import BaseModel

from app.core.logging import setup_logging

# The validation handler moved here from main.py, where it used that module's
# logger. It needs its own.
logger = setup_logging(__file__)


class ErrorResponse(BaseModel):
    success: bool = False
    error: str


from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions"""
    return JSONResponse(
        status_code=exc.status_code, content={"success": False, "error": exc.detail}
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation exceptions"""
    return JSONResponse(
        status_code=422, content={"success": False, "error": "Invalid request data"}
    )


# Moved verbatim from the pre-migration main.py. Named `validation_error_handler`
# to avoid colliding with the pre-existing `validation_exception_handler` above,
# which is the older, unregistered variant.
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """
    Global exception handler for validation errors.
    Formats the error response in a consistent way.
    """
    errors = exc.errors()
    error_messages = []

    for error in errors:
        # Extract field and error message
        field = ".".join(str(loc) for loc in error["loc"][1:]) if len(error["loc"]) > 1 else ""
        message = error["msg"]

        # Check for email validation errors
        if "email" in field.lower() and "not a valid email" in message.lower():
            logger.error(f"Invalid email format: {error}")
            return JSONResponse(
                status_code=422, content={"success": False, "error": "Invalid email format"}
            )

        error_messages.append(f"{field}: {message}" if field else message)

    # General validation error
    logger.error(f"Validation error: {error_messages}")
    return JSONResponse(
        status_code=422,
        content={"success": False, "error": "Validation error: " + "; ".join(error_messages)},
    )
