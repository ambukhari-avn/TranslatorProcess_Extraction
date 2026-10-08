"""Settings of the extraction service, read from environment variables."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]       # the repository root: the pipeline modules and data/ live here


@dataclass(frozen=True)
class Settings:
    files_root: Path          # every input and output path a client sends must lie inside this folder
    state_dir: Path           # job state and the shared translation cache
    api_key: str              # shared secret of the backend; empty = no key check (development only)
    max_concurrent_jobs: int

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            files_root=Path(os.environ.get("EXTRACTION_FILES_ROOT", ROOT / "files")).resolve(),
            state_dir=Path(os.environ.get("EXTRACTION_STATE_DIR", ROOT / "state")).resolve(),
            api_key=os.environ.get("EXTRACTION_API_KEY", ""),
            max_concurrent_jobs=max(1, int(os.environ.get("EXTRACTION_MAX_JOBS", "2"))),
        )
