"""Shared request dependencies."""
import hmac
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, Header, Request

from app.core.config import Settings
from app.core.errors import ApiError
from app.services.job_manager import JobManager


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def require_api_key(settings: Annotated[Settings, Depends(get_settings)],
                    x_api_key: Annotated[Optional[str], Header()] = None) -> None:
    """The backend proves who it is with a shared secret in the X-API-Key header (skipped when no key is configured)."""
    if settings.api_key and not (x_api_key and hmac.compare_digest(x_api_key, settings.api_key)):
        raise ApiError(401, "UNAUTHORIZED", "Missing or invalid API key.")


def resolve_inside(root: Path, value: str, what: str) -> Path:
    """A path sent by a client must lie inside the files root; anything else could read or overwrite other files."""
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    if path != root and root not in path.parents:
        raise ApiError(400, "PATH_OUTSIDE_ROOT", f"{what} must be inside the configured files folder.")
    return path
