"""Connects a job to the translation pipeline (imported lazily, so the API starts without loading OCR and PDF libraries)."""
from app.services.job_manager import Job


def run_translation(job: Job, report, cache_dir: str) -> dict:
    """Translate job.input_path into job.output_dir and report progress; raises when the run is cancelled or stopped."""
    import pipeline
    from config import PipelineConfig

    config = PipelineConfig(target_language=job.options.get("language", "ru"),
                            verify_translations=bool(job.options.get("verify", True)))

    if not config.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set on the extraction service.")

    def on_progress(stage: str, percent: int) -> None:
        report(stage, percent)
        if job.cancel_requested:
            raise pipeline.JobCancelled()

    return pipeline.translate_file(job.input_path, job.output_dir, on_progress, config, cache_dir=cache_dir)
