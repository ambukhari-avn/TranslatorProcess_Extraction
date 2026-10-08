"""Job endpoints the backend calls: start a translation, read its progress, list jobs, cancel."""
from dataclasses import asdict
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Response

from app.api.deps import get_jobs, get_settings, require_api_key, resolve_inside
from app.core.config import Settings
from app.core.errors import ApiError
from app.schemas.job import JobCreate, JobState, JobStatus
from app.services.job_manager import Job, JobManager

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])
ALLOWED = (".pdf", ".docx")


def _status(job: Job) -> JobStatus:
    return JobStatus.model_validate(asdict(job))


def _get(jobs: JobManager, job_id: str) -> Job:
    job = jobs.get(job_id)
    if job is None:
        raise ApiError(404, "JOB_NOT_FOUND", f"No job with id {job_id}.")
    return job


@router.post("", response_model=JobStatus, response_model_by_alias=True, status_code=202)
def start_job(body: JobCreate, response: Response, jobs: Annotated[JobManager, Depends(get_jobs)],
              settings: Annotated[Settings, Depends(get_settings)]) -> JobStatus:
    """Queue a translation. Sending the same jobId again returns that job instead of starting a second run."""
    source = resolve_inside(settings.files_root, body.input_path, "inputPath")
    target = resolve_inside(settings.files_root, body.output_dir, "outputDir")
    if source.suffix.lower() not in ALLOWED:
        raise ApiError(400, "UNSUPPORTED_FILE_TYPE", "Only .pdf and .docx files can be translated.")
    if not source.is_file():
        raise ApiError(400, "INPUT_NOT_FOUND", "The input file does not exist.")
    existing = jobs.get(body.job_id)
    if existing and existing.input_path != str(source):
        raise ApiError(409, "JOB_EXISTS", "A job with this id exists for a different file.")
    job, created = jobs.submit(body.job_id, str(source), str(target), body.options.model_dump())
    if not created:
        response.status_code = 200
    return _status(job)


@router.get("", response_model=list[JobStatus], response_model_by_alias=True)
def list_jobs(jobs: Annotated[JobManager, Depends(get_jobs)], status: Optional[JobState] = None) -> list:
    return [_status(j) for j in jobs.list(status.value if status else None)]


@router.get("/{job_id}", response_model=JobStatus, response_model_by_alias=True)
def get_job(job_id: str, jobs: Annotated[JobManager, Depends(get_jobs)]) -> JobStatus:
    return _status(_get(jobs, job_id))


@router.post("/{job_id}/cancel", response_model=JobStatus, response_model_by_alias=True)
def cancel_job(job_id: str, jobs: Annotated[JobManager, Depends(get_jobs)]) -> JobStatus:
    _get(jobs, job_id)
    job, accepted = jobs.cancel(job_id)
    if not accepted:
        raise ApiError(409, "JOB_FINISHED", f"The job already finished ({job.status}).")
    return _status(job)
