"""The extraction service: translates PDF and Word files on request of the backend.

    uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

Run it from the repository root (the pipeline reads data/ relative to it) and keep it off the public network:
only the backend should reach it, with the X-API-Key header."""
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from app.api.routes import health, jobs
from app.core.config import ROOT, Settings
from app.core.errors import register_error_handlers
from app.services.job_manager import JobManager, Runner
from app.services.translator import run_translation

log = logging.getLogger("extraction")


def create_app(settings: Optional[Settings] = None, runner: Runner = run_translation) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.files_root.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    manager = JobManager(settings.state_dir, settings.cache_dir, settings.max_concurrent_jobs, runner)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        os.chdir(ROOT)                                   # the pipeline reads data/ relative to the repository root
        log.info("Recovered %d earlier job(s)", manager.recover())
        if not settings.api_key:
            log.warning("EXTRACTION_API_KEY is not set: the API accepts any caller. Set it outside development.")
        yield
        manager.shutdown()

    app = FastAPI(title="Extraction service", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.jobs = settings, manager
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(jobs.router)
    return app

