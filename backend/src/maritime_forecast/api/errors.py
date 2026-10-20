"""Typed API errors: every failure is {"error": {"code", "message", "details"}}."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | list[Any] | None = None


class ErrorBody(BaseModel):
    error: ErrorDetail


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def dataset_not_found(dataset_id: str) -> ApiError:
    return ApiError(404, "dataset_not_found", f"unknown dataset {dataset_id!r}")


def invalid(message: str, **details: Any) -> ApiError:
    return ApiError(422, "invalid_parameter", message, details or None)


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorBody, "description": "dataset_not_found / vessel_not_found"},
    413: {"model": ErrorBody, "description": "too_many_points"},
    422: {"model": ErrorBody, "description": "invalid_parameter / window_too_large"},
}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        body = ErrorBody(error=ErrorDetail(code=exc.code, message=exc.message, details=exc.details))
        return JSONResponse(body.model_dump(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Location and message only: never echo submitted input back.
        details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        body = ErrorBody(
            error=ErrorDetail(code="invalid_parameter", message="invalid request", details=details)
        )
        return JSONResponse(body.model_dump(), status_code=422)
