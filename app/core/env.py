"""Reads the repository's .env file into the environment, so the service needs no manual setup per terminal."""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_env_file(path: Path = ENV_FILE) -> int:
    """Sets every KEY=value line of the file that is not already set in the environment; returns how many it set.
    A value that is already in the environment wins, so a real environment variable still overrides the file."""
    if not path.is_file():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
