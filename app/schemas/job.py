"""Request and response models of the job endpoints (JSON uses camelCase)."""
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class JobState(str, Enum):
    QUEUED = "Queued"
    RUNNING = "Running"
    DONE = "Done"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class JobOptions(CamelModel):
    language: Literal["ru", "kk"] = "ru"
    verify: bool = True                       # second-model verification and repair pass


class JobCreate(CamelModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    input_path: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    options: JobOptions = JobOptions()


class JobFiles(CamelModel):
    output: Optional[str] = None             # the translated file
    review: Optional[str] = None             # review document with flagged rows
    report: Optional[str] = None             # verification report (JSON)
    log: Optional[str] = None                # model call log (JSON)


class JobStatus(CamelModel):
    job_id: str
    status: JobState
    progress: int = 0
    stage: Optional[str] = None
    critical: int = 0
    moderate: int = 0
    document_score: Optional[float] = None
    review_required: bool = False
    error: Optional[str] = None
    files: JobFiles = JobFiles()
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class Health(CamelModel):
    status: str
    running: int
    queued: int
    max_concurrent: int
    openrouter_key_configured: bool
