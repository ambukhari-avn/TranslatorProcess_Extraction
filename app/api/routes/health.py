import os
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_jobs
from app.schemas.job import Health
from app.services.job_manager import JobManager

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health, response_model_by_alias=True)
def health(jobs: Annotated[JobManager, Depends(get_jobs)]) -> Health:
    counts = jobs.counts()
    return Health(status="ok", running=counts["running"], queued=counts["queued"], max_concurrent=jobs.max_concurrent,
                  openrouter_key_configured=bool(os.environ.get("OPENROUTER_API_KEY")))
