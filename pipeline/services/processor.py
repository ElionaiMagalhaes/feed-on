import logging
from pathlib import Path
import hashlib

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from pipeline.models import (DomainLexicon, FeedbackAgent, FeedbackConsequence, FeedbackContext,
                             FeedbackRecord, FeedbackTarget, PipelineEvent, ProcessingJob)

from .csv_reader import inspect_csv, iter_feedback
from .llm import (
    CATEGORY_FIELDS,
    generate_domain_keywords,
    keywords_from_storage,
    keywords_to_storage,
    merge_keyword_lists,
    normalize_domain_name,
)
from .nlp import extract_feedback_semantics_batch
from .ontology import FeedOnOntologyService
from .semantics import derive_consequences, normalize_text, resolve_targets
from .experiment import (finalize_manifest, initialize_manifest, lexicon_manifest,
                         write_assertion_audit, write_manifest)

logger = logging.getLogger(__name__)


class JobCanceled(Exception):
    pass


PIPELINE_STEPS = (
    ("upload", "Upload recebido"),
    ("csv_validation", "Leitura e validacao do arquivo"),
    ("ai_processing", "Analise IA / fallback local"),
    ("ontology", "Instanciacao FEED-ON"),
    ("reasoner", "Reasoner ontologico"),
    ("jira", "Exportacao manual Jira"),
    ("done", "Finalizacao"),
)


def process_job(job_id: int) -> dict:
    job = ProcessingJob.objects.get(pk=job_id)
    path = Path(job.upload.path)
    manifest = initialize_manifest(job, path)

    try:
        _ensure_not_canceled(job)
        _initialize_steps(job)
        _start_step(job, "upload", "Arquivo recebido e job criado.")
        _complete_step(job, "upload", "Upload pronto para processamento.")
        _start_step(job, "csv_validation", "Lendo arquivo e contando feedbacks validos.")
        job.mark_running("Processando feedbacks...")
        csv_inspection = inspect_csv(path, limit=job.row_limit)
        total_rows = csv_inspection.valid_rows
        job.total_rows = total_rows
        manifest["dataset"].update({
            "received_rows": csv_inspection.total_rows,
            "accepted_rows": csv_inspection.valid_rows,
            "ignored_rows": csv_inspection.empty_rows + csv_inspection.missing_text_rows,
            "missing_text_rows": csv_inspection.missing_text_rows,
        })
        job.metadata = {**(job.metadata or {}), "manifest": manifest, "csv_inspection": _csv_inspection_payload(csv_inspection)}
        job.save(update_fields=["total_rows", "metadata", "updated_at"])
        _complete_step(job, "csv_validation", f"{total_rows} feedbacks validos detectados.", total=total_rows)
        for warning in csv_inspection.warnings:
            _event(job, warning, level=PipelineEvent.Level.WARNING)
        if job.row_limit:
            _event(job, f"Limite aplicado: processando os primeiros {job.row_limit} feedbacks.")

        domain_name = normalize_domain_name(job.domain_name)
        if job.domain_name != domain_name:
            job.domain_name = domain_name
            job.save(update_fields=["domain_name", "updated_at"])
        lexicon = _prepare_domain_lexicon(job, domain_name)
        manifest["lexicon"] = lexicon_manifest(lexicon)

        _start_step(job, "ontology", "Carregando ontologia FEED-ON.")
        ontology = FeedOnOntologyService()
        cleaned_entities = ontology.prepare_for_job(job.id)
        ontology_metrics = ontology.metrics()
        manifest["ontology"] = {**ontology_metrics, "individuals_before": ontology_metrics.get("individuals", 0)}
        job.metadata = {**(job.metadata or {}), "manifest": manifest, "ontology": manifest["ontology"]}
        job.save(update_fields=["metadata", "updated_at"])
        if cleaned_entities:
            _event(job, f"Ontologia limpa: {cleaned_entities} individuos runtime removidos antes do novo job.")
        _update_step(job, "ontology", status="running", message="Ontologia pronta para instanciar feedbacks.")

        chunk = []
        _start_step(job, "ai_processing", "Analisando feedbacks com IA ou fallback local.", total=total_rows)
        for csv_feedback in iter_feedback(path, limit=job.row_limit):
            _ensure_not_canceled(job)
            chunk.append(csv_feedback)
            if len(chunk) >= settings.FEEDBACK_CHUNK_SIZE:
                _process_chunk(job, chunk, ontology, lexicon)
                chunk = []

        if chunk:
            _process_chunk(job, chunk, ontology, lexicon)

        _ensure_not_canceled(job)
        _run_reasoner_for_job(job, ontology)
        _save_instantiated_ontology(job, ontology, manifest)
        _complete_step(job, "ontology", f"{job.processed_rows} feedbacks instanciados.", processed=job.processed_rows, total=job.total_rows)
        _complete_step(job, "ai_processing", f"{job.processed_rows} feedbacks analisados.", processed=job.processed_rows, total=job.total_rows)
        _complete_step(job, "reasoner", "Inferencias ontologicas concluidas.", processed=job.processed_rows, total=job.total_rows)
        _event(job, f"Task Generation: {job.processed_rows} feedbacks prontos para exportacao manual ao Jira.")
        _complete_step(
            job,
            "jira",
            "Exportacao Jira aguardando selecao manual no dashboard.",
            processed=0,
            total=job.processed_rows,
        )
        _ensure_not_canceled(job)
        job.mark_completed("Pipeline concluido")
        _complete_step(job, "done", "Pipeline concluido.")
        job.refresh_from_db()
        manifest["reasoner"] = (job.metadata or {}).get("reasoner", {})
        manifest["ontology"] = (job.metadata or {}).get("ontology", manifest["ontology"])
        manifest["processing"]["chunks"] = (job.processed_rows + settings.FEEDBACK_CHUNK_SIZE - 1) // settings.FEEDBACK_CHUNK_SIZE
        manifest = finalize_manifest(job, manifest)
        manifest_path = write_manifest(job, manifest)
        job.metadata = {**(job.metadata or {}), "manifest": manifest, "manifest_path": str(manifest_path)}
        job.save(update_fields=["metadata", "updated_at"])
        _event(job, "Pipeline concluido.")
        return {"job_id": job.id, "status": job.status, "processed_rows": job.processed_rows}
    except JobCanceled:
        job.mark_canceled()
        _mark_running_step(job, "canceled", "Pipeline cancelado pelo usuario.")
        _event(job, "Pipeline cancelado pelo usuario.", level=PipelineEvent.Level.WARNING)
        return {"job_id": job.id, "status": job.status, "processed_rows": job.processed_rows}
    except Exception as exc:
        logger.exception("Falha no processamento do job %s", job_id)
        job.mark_failed(str(exc))
        _mark_running_step(job, "error", str(exc))
        _event(job, f"Falha no processamento: {exc}", level=PipelineEvent.Level.ERROR)
        raise


def _process_chunk(job: ProcessingJob, chunk, ontology: FeedOnOntologyService, lexicon: DomainLexicon) -> None:
    _ensure_not_canceled(job)
    _event(job, f"Processando {len(chunk)} feedbacks...")
    records_to_create = []
    warnings_seen = set()

    nlp_results = extract_feedback_semantics_batch(chunk)
    _update_step(
        job,
        "ai_processing",
        status="running",
        processed=job.processed_rows + len(chunk),
        total=job.total_rows,
        message=f"{job.processed_rows + len(chunk)}/{job.total_rows} feedbacks analisados.",
    )

    ontology_warnings = 0
    target_frequencies = _target_frequencies(job)
    for csv_feedback, nlp_result in zip(chunk, nlp_results):
        _ensure_not_canceled(job)
        semantic_source_id = _semantic_source_id(
            job=job,
            source_id=csv_feedback.source_id,
            ordinal=job.processed_rows + len(records_to_create) + 1,
        )
        typed_candidate = f"{nlp_result.target_type}.{nlp_result.target_candidate}"
        targets = resolve_targets(csv_feedback.target, typed_candidate, csv_feedback.text, lexicon)
        for target in targets:
            key = f"{target.target_type}.{target.target_name}"
            target_frequencies[key] = target_frequencies.get(key, 0) + 1
        consequences = derive_consequences(
            nlp_result.ai_intent, nlp_result.intent, nlp_result.sentiment_score, csv_feedback.text,
            targets, target_frequencies, settings.FEED_ON_HOTSPOT_MIN_COUNT, settings.FEED_ON_PRIORITY_KEYWORDS,
        )
        primary_target = targets[0]
        primary_consequence = consequences[0]
        agent = _agent_for(job, csv_feedback.agent_identifier, csv_feedback.agent_role)
        elicitation = "ExplicitFeedbackElicitationTechnique"
        ontology_result = ontology.interpret(
            source_id=semantic_source_id,
            text=csv_feedback.text,
            intent=nlp_result.intent,
            technical_target=f"{primary_target.target_type}.{primary_target.target_name}",
            sentiment_score=nlp_result.sentiment_score,
            ai_provider=nlp_result.ai_provider,
            elicitation_technique=elicitation,
            agent_pseudonym=agent.pseudonym if agent else "",
            agent_role_type=agent.role_type if agent else "",
            resolved_targets=targets,
            derived_consequences=consequences,
            domain_name=job.domain_name,
        )
        for warning in ontology_result.warnings:
            if warning not in warnings_seen:
                warnings_seen.add(warning)
                _event(job, warning, level=PipelineEvent.Level.WARNING)
            ontology_warnings += 1

        record = FeedbackRecord(
            job=job,
            source_id=csv_feedback.source_id,
            text=csv_feedback.text,
            intent=nlp_result.intent,
            ai_intent=nlp_result.ai_intent,
            sentiment_score=nlp_result.sentiment_score,
            ai_provider=nlp_result.ai_provider,
            target_candidate=nlp_result.target_candidate,
            ai_raw=nlp_result.ai_raw or {},
            technical_target=nlp_result.technical_target,
            inferred_target=f"{primary_target.target_type}.{primary_target.target_name}",
            consequence=primary_consequence.consequence_type,
            agent=agent,
            elicitation_technique=elicitation,
        )
        record._resolved_targets = targets
        record._derived_consequences = consequences
        record._context_data = csv_feedback.context or {}
        record.jira_payload = {"ontology_source_id": semantic_source_id}
        record._ontology_source_id = semantic_source_id
        record._ontology_warnings = ontology_result.warnings
        records_to_create.append(record)

    _ensure_not_canceled(job)

    with transaction.atomic():
        FeedbackRecord.objects.bulk_create(records_to_create, batch_size=500)
        FeedbackTarget.objects.bulk_create([
            FeedbackTarget(feedback=record, target_type=item.target_type, target_name=item.target_name,
                           matched_expression=item.matched_expression, source=item.source, confidence=item.confidence,
                           is_primary=index == 0)
            for record in records_to_create for index, item in enumerate(record._resolved_targets)
        ])
        FeedbackConsequence.objects.bulk_create([
            FeedbackConsequence(feedback=record, consequence_type=item.consequence_type,
                                derivation_rule=item.derivation_rule, confidence=item.confidence, is_primary=index == 0)
            for record in records_to_create for index, item in enumerate(record._derived_consequences)
        ])
        for record in records_to_create:
            if record._context_data:
                _create_context(record, record._context_data)
        job.processed_rows += len(records_to_create)
        job.current_phase = f"Inference & Enrichment: {job.processed_rows}/{job.total_rows} feedbacks"
        job.save(update_fields=["processed_rows", "current_phase", "updated_at"])
        _update_step(
            job,
            "ontology",
            status="running",
            processed=job.processed_rows,
            total=job.total_rows,
            message=f"{job.processed_rows}/{job.total_rows} feedbacks instanciados. Avisos: {ontology_warnings}.",
        )


def _prepare_domain_lexicon(job: ProcessingJob, domain_name: str) -> DomainLexicon:
    lexicon = DomainLexicon.objects.filter(domain_name=domain_name).first()
    should_generate = lexicon is None or settings.FEED_ON_LEXICON_REFRESH_EXISTING
    generated = generate_domain_keywords(domain_name) if should_generate else {}

    if lexicon is None:
        lexicon = DomainLexicon(domain_name=domain_name)
        action = "criado"
    else:
        action = "reutilizado"

    if generated:
        action = "enriquecido" if lexicon.pk else "criado"
        for category, field_name in CATEGORY_FIELDS.items():
            existing_terms = keywords_from_storage(getattr(lexicon, field_name, ""))
            merged_terms = merge_keyword_lists(existing_terms, generated.get(category, []))
            setattr(lexicon, field_name, keywords_to_storage(merged_terms))
        lexicon.save()

    if not lexicon.pk:
        lexicon.save()

    job.metadata = {
        **(job.metadata or {}),
        "domain_name": domain_name,
        "domain_lexicon_id": lexicon.id,
        "domain_lexicon_action": action,
    }
    job.save(update_fields=["metadata", "updated_at"])
    _event(job, f"Lexico de dominio '{domain_name}' {action}.")
    return lexicon


def _run_reasoner_for_job(job: ProcessingJob, ontology: FeedOnOntologyService) -> None:
    _ensure_not_canceled(job)
    if not ontology.loaded or not settings.FEED_ON_RUN_REASONER:
        _complete_step(job, "reasoner", "Reasoner pulado; usando inferencia deterministica.")
        return

    _start_step(job, "reasoner", "Rodando Pellet para enriquecer inferencias.", total=job.total_rows)
    job.current_phase = "Rodando Reasoner..."
    job.save(update_fields=["current_phase", "updated_at"])
    _event(job, "Semantic Interpretation (FEED-ON): instancias criadas; rodando Pellet.")
    reasoner_started = timezone.now()
    ok, warning = ontology.run_reasoner()
    reasoner_finished = timezone.now()
    audit = ontology.assertion_audit or {
        "scope": "job_runtime_subgraph", "assertions_before": 0, "assertions_after": 0,
        "direct_assertions": 0, "inferred_assertions": 0, "removed_assertions": 0,
        "direct_by_kind": {}, "inferred_by_kind": {}, "removed_by_kind": {},
        "direct": [], "inferred": [], "removed": [],
    }
    audit_path = write_assertion_audit(job, audit)
    job.metadata = {**(job.metadata or {}), "reasoner": {
        "enabled": True, "name": "Pellet", "started_at": reasoner_started.isoformat(),
        "finished_at": reasoner_finished.isoformat(), "duration_seconds": (reasoner_finished - reasoner_started).total_seconds(),
        "success": ok, "consistent": ok, "error": "" if ok else (warning or "reasoner failed"), "warnings": [warning] if warning else [],
        "assertions_before": audit["assertions_before"], "assertions_after": audit["assertions_after"],
        "direct_assertions": audit["direct_assertions"], "inferred_assertions": audit["inferred_assertions"],
        "removed_assertions": audit["removed_assertions"], "direct_by_kind": audit["direct_by_kind"],
        "inferred_by_kind": audit["inferred_by_kind"], "removed_by_kind": audit["removed_by_kind"],
        "assertion_audit_path": str(audit_path), "assertion_scope": audit["scope"],
    }}
    job.save(update_fields=["metadata", "updated_at"])
    if warning:
        _event(job, warning, level=PipelineEvent.Level.WARNING)
    if not ok:
        _complete_step(job, "reasoner", warning or "Reasoner falhou; resultados deterministicos mantidos.")
        if settings.FEED_ON_REASONER_FAIL_FAST:
            raise RuntimeError(warning or "Pellet reasoner failed")
        return

    records = list(FeedbackRecord.objects.filter(job=job))
    for record in records:
        ontology_source_id = (record.jira_payload or {}).get("ontology_source_id", record.source_id)
        inferred = ontology.inferred_target_for(ontology_source_id)
        if inferred:
            record.inferred_target = inferred
        inferred_consequence = ontology.consequence_for(ontology_source_id)
        if inferred_consequence:
            record.consequence = inferred_consequence
    FeedbackRecord.objects.bulk_update(records, ["inferred_target", "consequence"], batch_size=500)


def _save_instantiated_ontology(job, ontology, manifest) -> None:
    try:
        output_path = ontology.save()
        if output_path:
            metrics = ontology.metrics()
            ontology_metadata = {
                **(job.metadata or {}).get("ontology", {}),
                "instantiated_path": str(output_path),
                "individuals_after": metrics.get("individuals", 0),
            }
            manifest["ontology"] = ontology_metadata
            job.metadata = {**(job.metadata or {}), "ontology": ontology_metadata}
            job.save(update_fields=["metadata", "updated_at"])
    except Exception as exc:
        _event(job, f"Ontologia instanciada, mas nao foi possivel salvar o OWL: {exc}", level=PipelineEvent.Level.WARNING)


def _agent_for(job, identifier, role):
    if not (identifier or "").strip():
        return None
    source_hash = hashlib.sha256(f"{settings.FEED_ON_AGENT_HASH_SALT}:{normalize_text(identifier)}".encode()).hexdigest()
    existing = FeedbackAgent.objects.filter(job=job, source_hash=source_hash).first()
    if existing:
        return existing
    role_value = normalize_text(role)
    role_type = "InternalAgent" if role_value in {"internal", "interno", "employee", "funcionario"} else "ExternalAgent" if role_value in {"external", "externo", "customer", "cliente"} else ""
    return FeedbackAgent.objects.create(job=job, source_hash=source_hash, pseudonym=f"Agent_{job.agents.count() + 1:03d}", role_type=role_type, role_source="csv" if role_type else "")


def _target_frequencies(job):
    frequencies = {}
    for target in FeedbackTarget.objects.filter(feedback__job=job).values("target_type", "target_name"):
        key = f"{target['target_type']}.{target['target_name']}"
        frequencies[key] = frequencies.get(key, 0) + 1
    return frequencies


def _create_context(record, data):
    from django.utils.dateparse import parse_datetime
    values = dict(data)
    raw_timestamp = values.pop("timestamp", "")
    timestamp = parse_datetime(raw_timestamp) if raw_timestamp else None
    known = {key: values.pop(key, "") for key in ("device", "browser", "operating_system", "screen", "module", "environment", "source_channel")}
    FeedbackContext.objects.create(feedback=record, timestamp=timestamp, metadata=values, **known)


def _ensure_not_canceled(job: ProcessingJob) -> None:
    job.refresh_from_db(fields=["cancel_requested", "status"])
    if job.cancel_requested or job.status in {ProcessingJob.Status.CANCELING, ProcessingJob.Status.CANCELED}:
        raise JobCanceled()


def _event(job: ProcessingJob, message: str, level: str = PipelineEvent.Level.INFO, **metadata) -> None:
    PipelineEvent.objects.create(job=job, level=level, message=message[:255], metadata=metadata)


def _csv_inspection_payload(inspection) -> dict:
    return {
        "total_rows": inspection.total_rows,
        "valid_rows": inspection.valid_rows,
        "empty_rows": inspection.empty_rows,
        "missing_text_rows": inspection.missing_text_rows,
        "fieldnames": list(inspection.fieldnames),
        "text_column": inspection.text_column,
        "id_column": inspection.id_column,
        "target_column": inspection.target_column,
        "intent_column": inspection.intent_column,
        "delimiter": inspection.delimiter,
        "file_format": getattr(inspection, "file_format", "csv"),
        "sheet_name": getattr(inspection, "sheet_name", ""),
        "warnings": list(inspection.warnings),
    }


def _initialize_steps(job: ProcessingJob) -> None:
    job.metadata = {**(job.metadata or {}), "pipeline_steps": [_blank_step(key, label) for key, label in PIPELINE_STEPS]}
    job.save(update_fields=["metadata", "updated_at"])


def _start_step(job: ProcessingJob, key: str, message: str = "", total: int | None = None) -> None:
    _update_step(job, key, status="running", message=message, total=total, started=True)


def _complete_step(
    job: ProcessingJob,
    key: str,
    message: str = "",
    processed: int | None = None,
    total: int | None = None,
) -> None:
    _update_step(job, key, status="completed", message=message, processed=processed, total=total, finished=True)


def _mark_running_step(job: ProcessingJob, status: str, message: str) -> None:
    steps = _steps_from_job(job)
    now = timezone.now().isoformat()
    for step in steps:
        if step.get("status") == "running":
            step["status"] = status
            step["message"] = message[:180]
            step["finished_at"] = now
    job.metadata = {**(job.metadata or {}), "pipeline_steps": steps}
    job.save(update_fields=["metadata", "updated_at"])


def _update_step(
    job: ProcessingJob,
    key: str,
    *,
    status: str | None = None,
    message: str | None = None,
    processed: int | None = None,
    total: int | None = None,
    started: bool = False,
    finished: bool = False,
) -> None:
    steps = _steps_from_job(job)
    now = timezone.now()
    for step in steps:
        if step.get("key") != key:
            continue
        if status:
            step["status"] = status
        if message is not None:
            step["message"] = message[:180]
        if processed is not None:
            step["processed"] = max(0, int(processed))
        if total is not None:
            step["total"] = max(0, int(total))
        if started and not step.get("started_at"):
            step["started_at"] = now.isoformat()
        if finished:
            step["finished_at"] = now.isoformat()
            step["duration_seconds"] = _duration_seconds(step.get("started_at"), now)
        break

    job.metadata = {**(job.metadata or {}), "pipeline_steps": steps}
    job.save(update_fields=["metadata", "updated_at"])


def _steps_from_job(job: ProcessingJob) -> list[dict]:
    existing = list((job.metadata or {}).get("pipeline_steps") or [])
    if existing:
        return existing
    return [_blank_step(key, label) for key, label in PIPELINE_STEPS]


def _blank_step(key: str, label: str) -> dict:
    return {
        "key": key,
        "label": label,
        "status": "pending",
        "message": "",
        "processed": 0,
        "total": 0,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
    }


def _duration_seconds(started_at: str | None, finished_at) -> float | None:
    if not started_at:
        return None
    try:
        started = timezone.datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return round((finished_at - started).total_seconds(), 2)


def _semantic_source_id(job: ProcessingJob, source_id: str, ordinal: int) -> str:
    safe_source = source_id or "unknown"
    return f"{safe_source}__job{job.id}__row{ordinal}"






