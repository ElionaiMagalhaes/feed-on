import hashlib
import json
import platform
import subprocess
import sys
from importlib import metadata as package_metadata
from pathlib import Path

from django import get_version as django_version
from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from pipeline.models import FeedbackRecord


def initialize_manifest(job, dataset_path: Path) -> dict:
    commit, dirty = _git_state()
    stat = dataset_path.stat()
    return {
        "schema_version": "1.0",
        "application": {
            "name": "FEED-ON",
            "version": settings.APPLICATION_VERSION,
            "git_commit": commit,
            "git_dirty": dirty,
        },
        "ontology": {},
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "django_version": django_version(),
            "owlready2_version": _package_version("owlready2"),
            "openai_sdk_version": _package_version("openai"),
            "celery_version": _package_version("celery"),
            "java_version": _java_version(),
            "database_engine": settings.DATABASES["default"]["ENGINE"],
            "celery_eager": settings.CELERY_TASK_ALWAYS_EAGER,
        },
        "configuration": {
            "feedback_chunk_size": settings.FEEDBACK_CHUNK_SIZE,
            "openai_batch_size": settings.OPENAI_BATCH_SIZE,
            "hotspot_min_count": settings.FEED_ON_HOTSPOT_MIN_COUNT,
            "priority_keywords": list(settings.FEED_ON_PRIORITY_KEYWORDS),
            "reasoner_enabled": settings.FEED_ON_RUN_REASONER,
            "reasoner_name": settings.FEED_ON_REASONER,
            "reasoner_fail_fast": settings.FEED_ON_REASONER_FAIL_FAST,
            "jira_dry_run": settings.JIRA_DRY_RUN,
            "lexicon_refresh_existing": settings.FEED_ON_LEXICON_REFRESH_EXISTING,
        },
        "llm": {
            "provider": "openai" if settings.OPENAI_ENABLE_ANALYSIS else "local",
            "configured_model": settings.OPENAI_MODEL,
            "returned_models": [],
            "lexicon_prompt_version": settings.LEXICON_PROMPT_VERSION,
            "semantic_extraction_prompt_version": settings.SEMANTIC_EXTRACTION_PROMPT_VERSION,
            "canonical_target_map_version": settings.CANONICAL_TARGET_MAP_VERSION,
            "feedback_chunk_size": settings.FEEDBACK_CHUNK_SIZE,
            "api_calls": 0,
            "records_openai": 0,
            "records_local_fallback": 0,
            "records_csv": 0,
            "timeouts": 0,
            "retries": 0,
        },
        "dataset": {
            "filename": dataset_path.name,
            "sha256": _sha256_file(dataset_path),
            "size_bytes": stat.st_size,
            "received_rows": 0,
            "accepted_rows": 0,
            "ignored_rows": 0,
            "duplicate_rows": 0,
            "missing_text_rows": 0,
            "distinct_agents": 0,
            "anonymized": True,
        },
        "lexicon": {},
        "processing": {"started_at": timezone.now().isoformat(), "finished_at": "", "duration_seconds": 0.0, "chunks": 0},
        "reasoner": {},
        "jira": {"dry_run": settings.JIRA_DRY_RUN, "payloads_generated": 0, "issues_created": 0},
        "results": {},
    }


def lexicon_manifest(lexicon) -> dict:
    legacy = {
        "ui_elements": lexicon.ui_elements,
        "quality_attributes": lexicon.quality_attributes,
        "requirements": lexicon.requirements,
        "processes": lexicon.processes,
    }
    terms = list(lexicon.terms.filter(active=True).values(
        "expression", "normalized_expression", "canonical_name", "target_type", "language", "source"
    ))
    snapshot = {"domain_name": lexicon.domain_name, "legacy_fields": legacy, "normalized_terms": terms}
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**snapshot, "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "frozen": not settings.FEED_ON_LEXICON_REFRESH_EXISTING}


def finalize_manifest(job, manifest: dict) -> dict:
    feedbacks = FeedbackRecord.objects.filter(job=job)
    providers = dict(feedbacks.values_list("ai_provider").annotate(total=Count("id")))
    models = sorted({
        raw.get("model") for raw in feedbacks.values_list("ai_raw", flat=True)
        if isinstance(raw, dict) and raw.get("model")
    })
    calls = {
        (raw.get("response_id"), raw.get("model")) for raw in feedbacks.values_list("ai_raw", flat=True)
        if isinstance(raw, dict) and raw.get("response_id")
    }
    started = job.started_at or job.created_at
    finished = job.finished_at or timezone.now()
    manifest["llm"].update({
        "returned_models": models,
        "api_calls": len(calls),
        "records_openai": providers.get("openai", 0),
        "records_local_fallback": providers.get("local", 0),
        "records_csv": providers.get("csv", 0),
    })
    manifest["dataset"].update({
        "distinct_agents": job.agents.count(),
        "accepted_rows": feedbacks.count(),
    })
    manifest["processing"].update({
        "finished_at": finished.isoformat(),
        "duration_seconds": max(0.0, (finished - started).total_seconds()),
    })
    manifest["jira"].update({
        "payloads_generated": feedbacks.exclude(jira_payload={}).count(),
        "issues_created": feedbacks.filter(jira_status="created").count(),
    })
    manifest["results"] = {
        "feedbacks": feedbacks.count(),
        "targets": job.feedbacks.aggregate(total=Count("targets"))["total"] or 0,
        "consequences": job.feedbacks.aggregate(total=Count("consequences"))["total"] or 0,
        "multiple_targets": feedbacks.annotate(total=Count("targets")).filter(total__gt=1).count(),
        "multiple_consequences": feedbacks.annotate(total=Count("consequences")).filter(total__gt=1).count(),
        "contexts": feedbacks.filter(context__isnull=False).count(),
        "feature_general": feedbacks.filter(targets__target_type="Feature", targets__target_name="General").distinct().count(),
    }
    return manifest


def write_manifest(job, manifest: dict) -> Path:
    output_dir = settings.BASE_DIR / "results" / f"job_{job.id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "experimental-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return output


def write_assertion_audit(job, audit: dict) -> Path:
    output_dir = settings.BASE_DIR / "results" / f"job_{job.id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "owl-assertion-audit.json"
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return output


def _git_state() -> tuple[str, bool]:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=5, check=True).stdout.strip())
        return commit, dirty
    except Exception:
        return "", True


def _java_version() -> str:
    try:
        result = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=5, check=False)
        return (result.stderr or result.stdout).splitlines()[0].strip()
    except Exception:
        return "unavailable"


def _package_version(name: str) -> str:
    try:
        return package_metadata.version(name)
    except package_metadata.PackageNotFoundError:
        return "unavailable"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
