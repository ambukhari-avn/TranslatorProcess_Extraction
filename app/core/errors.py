"""One error shape for every failure: {"code": "...", "message": "..."}."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def _body(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, error: ApiError):
        return JSONResponse(status_code=error.status_code, content=_body(error.code, error.message))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, error: RequestValidationError):
        first = error.errors()[0] if error.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return JSONResponse(status_code=422, content=_body("VALIDATION_ERROR", f"{where}: {first.get('msg', 'invalid request')}".strip(": ")))

    @app.exception_handler(Exception)
    async def unexpected(_: Request, error: Exception):
        return JSONResponse(status_code=500, content=_body("INTERNAL_ERROR", "Unexpected error in the extraction service."))
