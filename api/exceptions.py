from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import Error
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError


def missing(_: Request, exc: KeyError) -> JSONResponse:
    return JSONResponse({"detail": "Research resource not found"}, status_code=404)


def invalid(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse({"detail": "Invalid research operation"}, status_code=422)


def unavailable(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": "Local research database unavailable"}, status_code=503)


def invalid_data(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse({"detail": "Invalid stored research data"}, status_code=500)


def register_exception_handlers(app: FastAPI) -> None:
    app.exception_handler(KeyError)(missing)
    app.exception_handler(ValueError)(invalid)
    app.exception_handler(Error)(unavailable)
    app.exception_handler(SQLAlchemyError)(unavailable)
    app.exception_handler(ValidationError)(invalid_data)
