import logging
from pathlib import Path

from django.conf import settings
from django.db import transaction

from pipeline.models import FeedbackRecord, PipelineEvent, ProcessingJob

from .csv_reader import count_rows, iter_feedback
from .jira import create_jira_issue
from .nlp import extract_feedback_semantics_batch
from .ontology import FeedOnOntologyService

logger = logging.getLogger(__name__)


class JobCanceled(Exception):
    pass


def process_job(job_id: int) -> dict:
    job = ProcessingJob.objects.get(pk=job_id)
    path = Path(job.upload.path)

    try:
        _ensure_not_canceled(job)
        _event(job, "AI-based Processing: lendo CSV e analisando sentimento/intencao com GPT quando configurado.")
        job.mark_running("Processando feedbacks...")
        total_rows = count_rows(path, limit=job.row_limit)
        job.total_rows = total_rows
        job.save(update_fields=["total_rows", "updated_at"])
        if job.row_limit:
            _event(job, f"Limite aplicado: processando os primeiros {job.row_limit} feedbacks.")

        ontology = FeedOnOntologyService()
        cleaned_entities = ontology.prepare_for_job(job.id)
        if cleaned_entities:
            _event(job, f"Ontologia limpa: {cleaned_entities} individuos runtime removidos antes do novo job.")
        chunk = []
        for csv_feedback in iter_feedback(path, limit=job.row_limit):
            _ensure_not_canceled(job)
            chunk.append(csv_feedback)
            if len(chunk) >= settings.FEEDBACK_CHUNK_SIZE:
                _process_chunk(job, chunk, ontology)
                chunk = []

        if chunk:
            _process_chunk(job, chunk, ontology)

        _ensure_not_canceled(job)
        _event(job, f"Task Generation: {job.processed_rows} feedbacks mapeados para JSON do Jira.")
        _send_to_jira(job)
        _ensure_not_canceled(job)
        job.mark_completed("Pipeline concluido")
        _event(job, "Pipeline concluido.")
        return {"job_id": job.id, "status": job.status, "processed_rows": job.processed_rows}
    except JobCanceled:
        job.mark_canceled()
        _event(job, "Pipeline cancelado pelo usuario.", level=PipelineEvent.Level.WARNING)
        return {"job_id": job.id, "status": job.status, "processed_rows": job.processed_rows}
    except Exception as exc:
        logger.exception("Falha no processamento do job %s", job_id)
        job.mark_failed(str(exc))
        _event(job, f"Falha no processamento: {exc}", level=PipelineEvent.Level.ERROR)
        raise


def _process_chunk(job: ProcessingJob, chunk, ontology: FeedOnOntologyService) -> None:
    _ensure_not_canceled(job)
    _event(job, f"Processando {len(chunk)} feedbacks...")
    records_to_create = []
    warnings_seen = set()

    nlp_results = extract_feedback_semantics_batch(chunk)

    for csv_feedback, nlp_result in zip(chunk, nlp_results):
        _ensure_not_canceled(job)
        semantic_source_id = _semantic_source_id(
            job=job,
            source_id=csv_feedback.source_id,
            ordinal=job.processed_rows + len(records_to_create) + 1,
        )
        ontology_result = ontology.interpret(
            source_id=semantic_source_id,
            text=csv_feedback.text,
            intent=nlp_result.intent,
            technical_target=nlp_result.technical_target,
        )
        for warning in ontology_result.warnings:
            if warning not in warnings_seen:
                warnings_seen.add(warning)
                _event(job, warning, level=PipelineEvent.Level.WARNING)

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
            inferred_target=ontology_result.inferred_target,
            consequence=ontology_result.consequence,
        )
        record._ontology_source_id = semantic_source_id
        records_to_create.append(record)

    _run_reasoner_for_chunk(job, ontology, records_to_create)
    _ensure_not_canceled(job)

    with transaction.atomic():
        FeedbackRecord.objects.bulk_create(records_to_create, batch_size=500)
        job.processed_rows += len(records_to_create)
        job.current_phase = f"Inference & Enrichment: {job.processed_rows}/{job.total_rows} feedbacks"
        job.save(update_fields=["processed_rows", "current_phase", "updated_at"])


def _run_reasoner_for_chunk(job: ProcessingJob, ontology: FeedOnOntologyService, records: list[FeedbackRecord]) -> None:
    _ensure_not_canceled(job)
    if not ontology.loaded or not settings.FEED_ON_RUN_REASONER:
        return

    job.current_phase = "Rodando Reasoner..."
    job.save(update_fields=["current_phase", "updated_at"])
    _event(job, "Semantic Interpretation (FEED-ON): instancias criadas; rodando Pellet.")
    ok, warning = ontology.run_reasoner()
    if warning:
        _event(job, warning, level=PipelineEvent.Level.WARNING)
    if not ok:
        return

    for record in records:
        ontology_source_id = getattr(record, "_ontology_source_id", record.source_id)
        inferred = ontology.inferred_target_for(ontology_source_id)
        if inferred:
            record.inferred_target = inferred
        inferred_consequence = ontology.consequence_for(ontology_source_id)
        if inferred_consequence:
            record.consequence = inferred_consequence


def _send_to_jira(job: ProcessingJob) -> None:
    pending = FeedbackRecord.objects.filter(job=job, jira_status=FeedbackRecord.JiraStatus.PENDING)
    total = pending.count()
    _event(job, f"Jira Integration: enviando {total} tarefas para o Jira...")
    job.current_phase = f"Enviando {total} tarefas para o Jira..."
    job.save(update_fields=["current_phase", "updated_at"])

    created = 0
    for record in pending.iterator(chunk_size=settings.FEEDBACK_CHUNK_SIZE):
        _ensure_not_canceled(job)
        try:
            result = create_jira_issue(record)
            record.jira_key = result.key
            record.jira_status = result.status
            record.jira_payload = result.raw
            record.save(update_fields=["jira_key", "jira_status", "jira_payload", "updated_at"])
            created += 1
        except Exception as exc:
            record.jira_status = FeedbackRecord.JiraStatus.FAILED
            record.processing_error = str(exc)
            record.save(update_fields=["jira_status", "processing_error", "updated_at"])
            _event(job, f"Falha ao criar issue para feedback {record.source_id}: {exc}", level=PipelineEvent.Level.ERROR)

    job.jira_created = created
    job.save(update_fields=["jira_created", "updated_at"])


def _ensure_not_canceled(job: ProcessingJob) -> None:
    job.refresh_from_db(fields=["cancel_requested", "status"])
    if job.cancel_requested or job.status in {ProcessingJob.Status.CANCELING, ProcessingJob.Status.CANCELED}:
        raise JobCanceled()


def _event(job: ProcessingJob, message: str, level: str = PipelineEvent.Level.INFO, **metadata) -> None:
    PipelineEvent.objects.create(job=job, level=level, message=message[:255], metadata=metadata)


def _semantic_source_id(job: ProcessingJob, source_id: str, ordinal: int) -> str:
    safe_source = source_id or "unknown"
    return f"{safe_source}__job{job.id}__row{ordinal}"






