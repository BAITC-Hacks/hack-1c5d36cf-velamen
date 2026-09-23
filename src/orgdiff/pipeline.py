from pathlib import Path
from uuid import uuid4

from .config import Settings
from .extract import extract_snapshot, source_index
from .ingest import deduplicate, parse_cached
from .llm import PROMPT_VERSION, Runtime, error_ru, use_runtime
from .models import InputFile, SCHEMA_VERSION
from .storage import write_json


async def analyze(before: list[InputFile], after: list[InputFile], out: Path, *, until="extract", settings: Settings | None = None, progress=lambda e: None):
    run = Runtime(settings)
    metadata = {"id": uuid4().hex, "stopped_after": until, "pipeline_completed": False,
                "state": "running", "errors": [], "schema_version": SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION, "settings": run.settings.model_dump(mode="json"),
                "service_tier": "default"}
    with use_runtime(run):
        try:
            run.settings.require_key()
            if until not in {"extract", "compare"} or not before or not after:
                raise ValueError("Нужны оба набора документов и этап extract или compare.")
            snapshots = []
            for side, files in (("before", before), ("after", after)):
                documents = [parse_cached(file, run.settings.artifact_dir / "cache") for file in deduplicate(files)]
                snapshot = await extract_snapshot(side, documents, progress)
                snapshots.append(snapshot)
                write_json(out / f"{side}.snapshot.json", snapshot)
            result = None
            if until == "compare":
                from .compare import compare_snapshots
                result = await compare_snapshots(*snapshots)
                write_json(out / "comparison.json", result)
            # Recovery can add extracted facts to the after snapshot.
            for snapshot in snapshots:
                write_json(out / f"{snapshot.side}.snapshot.json", snapshot)
            write_json(out / "sources.json", {s.side: source_index(s) for s in snapshots})
            metadata["errors"] = [w for s in snapshots for w in s.warnings]
            if result:
                metadata["warnings"] = result.warnings
                metadata["stages"] = result.stages
            metadata["state"] = "checkpoint" if all(s.coverage_complete for s in snapshots) and not metadata["errors"] else "partial"
            if result and result.stages["compare"] == "partial":
                metadata["state"] = "partial"
            return result or snapshots
        except Exception as exc:
            metadata["state"] = "failed"
            metadata["errors"].append(error_ru(exc))
            raise
        finally:
            write_json(out / "run.json", metadata)
            write_json(out / "usage.json", dict(run.usage))
            await run.close()
