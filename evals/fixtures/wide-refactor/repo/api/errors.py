from fastapi import HTTPException
from fastapi.responses import JSONResponse


def to_response(exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
