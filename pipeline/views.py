import csv
import json
from threading import Thread
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import close_old_connections
from django.db.models import Avg, Count, FloatField, Q, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import FeedbackRecord, PipelineEvent, ProcessingJob
from .services.jira import JiraConfig, criar_ticket_jira, settings_jira_config, testar_comunicacao_jira
from .services.llm import normalize_domain_name
from .services.ontology import FeedOnOntologyService, atualizar_jira_key_na_ontologia
from .services.processor import process_job
from .services.reporter import build_executive_report_docx
from .tasks import process_feedback_job

ALLOWED_SENTIMENT_FILTERS = {"all", "negative", "neutral", "positive"}
ALLOWED_CONSEQUENCE_FILTERS = {"all", "Correction", "Improvement", "Prioritization"}
NEGATIVE_THRESHOLD = -0.05
POSITIVE_THRESHOLD = 0.05


@require_GET
def landing(request: HttpRequest):
    if request.user.is_authenticated:
        recent_jobs = ProcessingJob.objects.filter(owner=request.user)[:3]
    else:
        recent_jobs = []
    return render(
        request,
        "pipeline/landing.html",
        {
            "recent_jobs": recent_jobs,
            "active_nav": "home",
        },
    )


@require_GET
@login_required
def index(request: HttpRequest):
    jobs = _user_jobs(request)[:10]
    failed_jobs_count = _user_jobs(request).filter(status=ProcessingJob.Status.FAILED).count()
    return render(
        request,
        "pipeline/index.html",
        {
            "jobs": jobs,
            "failed_jobs_count": failed_jobs_count,
            "max_upload_size_mb": settings.MAX_UPLOAD_SIZE_MB,
            "active_nav": "upload",
        },
    )


@require_POST
@login_required
def create_job(request: HttpRequest):
    upload = request.FILES.get("dataset")
    if upload is None:
        return JsonResponse({"error": "Envie um arquivo CSV ou XLSX."}, status=400)
    if not _looks_like_supported_feedback_upload(upload):
        return JsonResponse({"error": "O arquivo precisa estar no formato CSV ou XLSX."}, status=400)

    max_size = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if upload.size > max_size:
        return JsonResponse({"error": f"O arquivo excede {settings.MAX_UPLOAD_SIZE_MB} MB."}, status=400)

    row_limit, error = _parse_row_limit(request.POST.get("row_limit", ""))
    if error:
        return JsonResponse({"error": error}, status=400)

    domain_name = normalize_domain_name(request.POST.get("domain_name", ""))
    job = ProcessingJob.objects.create(
        owner=request.user,
        original_filename=upload.name,
        upload=upload,
        row_limit=row_limit,
        domain_name=domain_name,
        metadata={"domain_name": domain_name},
    )
    try:
        if settings.CELERY_TASK_ALWAYS_EAGER:
            _start_local_background_job(job.id)
            task_id = "local-thread"
        else:
            async_result = process_feedback_job.delay(job.id)
            task_id = async_result.id
    except Exception as exc:
        job.mark_failed(f"Nao foi possivel enviar o job ao Celery: {exc}")
        return JsonResponse(
            {
                "error": "Nao foi possivel iniciar o processamento. Verifique Redis/Celery ou use CELERY_TASK_ALWAYS_EAGER=true."
            },
            status=503,
        )

    job.metadata = {**job.metadata, "celery_task_id": task_id}
    job.save(update_fields=["metadata", "updated_at"])

    return JsonResponse(_job_urls(request, job), status=201)


@require_POST
@login_required
def cancel_job(request: HttpRequest, job_id: int):
    job = _get_user_job_or_404(request, job_id)
    if job.status in {ProcessingJob.Status.COMPLETED, ProcessingJob.Status.FAILED, ProcessingJob.Status.CANCELED}:
        return JsonResponse({"status": job.status, "message": "Este job ja foi finalizado."})

    job.request_cancel()
    PipelineEvent.objects.create(job=job, level=PipelineEvent.Level.WARNING, message="Cancelamento solicitado pelo usuario.")
    return JsonResponse({"status": job.status, "message": "Cancelamento solicitado."})


@require_POST
@login_required
def delete_job(request: HttpRequest, job_id: int):
    job = _get_user_job_or_404(request, job_id)
    if job.status != ProcessingJob.Status.COMPLETED:
        return JsonResponse(
            {"error": "Apenas jobs concluidos podem ser deletados."},
            status=400,
        )

    upload = job.upload
    filename = job.original_filename
    upload.delete(save=False)
    job.delete()
    return JsonResponse({"deleted": True, "job_id": job_id, "message": f"Job {job_id} ({filename}) deletado."})


@require_POST
@login_required
def clear_failed_jobs(request: HttpRequest):
    failed_jobs = list(_user_jobs(request).filter(status=ProcessingJob.Status.FAILED))
    deleted_count = 0
    for job in failed_jobs:
        upload = job.upload
        if upload:
            upload.delete(save=False)
        job.delete()
        deleted_count += 1

    return JsonResponse(
        {
            "deleted": deleted_count,
            "message": f"{deleted_count} job(s) com falha removido(s).",
        }
    )


@require_POST
@login_required
def export_selected_to_jira(request: HttpRequest, job_id: int):
    job = _get_user_job_or_404(request, job_id)
    jira_config = _jira_config_from_session(request)
    if jira_config is None:
        return JsonResponse(
            {
                "error": (
                    "Configure os dados de integracao com o Jira antes de exportar. "
                    "Use o botao 'Configurar Jira' no dashboard."
                )
            },
            status=400,
        )
    try:
        selected_ids = _selected_feedback_ids_from_request(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    selected_ids = list(dict.fromkeys(selected_ids))
    if not selected_ids:
        return JsonResponse({"error": "Selecione ao menos um feedback."}, status=400)

    records = list(FeedbackRecord.objects.filter(job=job, id__in=selected_ids).order_by("id"))
    found_ids = {record.id for record in records}
    missing_ids = [item for item in selected_ids if item not in found_ids]
    if missing_ids:
        return JsonResponse({"error": f"Feedbacks nao encontrados neste job: {missing_ids}"}, status=404)

    rows = []
    errors = []
    for record in records:
        if record.jira_key and record.jira_status == FeedbackRecord.JiraStatus.CREATED:
            rows.append(_jira_export_row(record))
            continue

        try:
            target_class = _target_class_for_jira(record)
            severity = "Correction" if record.consequence == "Correction" else "Improvement"
            jira_key = criar_ticket_jira(record.text, target_class, severity, config=jira_config)
            jira_status = (
                FeedbackRecord.JiraStatus.DRY_RUN
                if settings.JIRA_DRY_RUN or str(jira_key).startswith("DRY-RUN-")
                else FeedbackRecord.JiraStatus.CREATED
            )
            ontology_source_id = _ontology_source_id_for_record(record)

            record.jira_key = jira_key
            record.jira_status = jira_status
            record.processing_error = ""
            record.jira_payload = {
                **(record.jira_payload or {}),
                "manual_export": True,
                "dry_run": jira_status == FeedbackRecord.JiraStatus.DRY_RUN,
                "ontology_source_id": ontology_source_id,
                "target_class": target_class,
            }
            ontology_warning = _sync_jira_key_to_ontology(record, ontology_source_id, jira_key)
            if ontology_warning:
                record.jira_payload["ontology_sync_warning"] = ontology_warning
            record.save(update_fields=["jira_key", "jira_status", "processing_error", "jira_payload", "updated_at"])
            rows.append(_jira_export_row(record))
        except Exception as exc:
            record.jira_status = FeedbackRecord.JiraStatus.FAILED
            record.processing_error = _friendly_jira_error(exc, jira_config.project_key)
            record.save(update_fields=["jira_status", "processing_error", "updated_at"])
            errors.append({"id": record.id, "source_id": record.source_id, "error": record.processing_error})

    job.jira_created = FeedbackRecord.objects.filter(
        job=job,
        jira_status=FeedbackRecord.JiraStatus.CREATED,
    ).exclude(jira_key="").count()
    job.save(update_fields=["jira_created", "updated_at"])

    if errors and not rows:
        first_error = errors[0]["error"] if errors else "Falha ao exportar feedbacks para o Jira."
        return JsonResponse(
            {
                "error": f"Falha ao exportar os feedbacks selecionados: {first_error}",
                "exported": rows,
                "errors": errors,
                "jira_created": job.jira_created,
            },
            status=502,
        )

    status = 207 if errors and rows else 200
    return JsonResponse({"exported": rows, "errors": errors, "jira_created": job.jira_created}, status=status)


@require_GET
@login_required
def jira_config_status(request: HttpRequest):
    config = _jira_config_from_session(request)
    if config is None:
        env_config = settings_jira_config()
        return JsonResponse(
            {
                "configured": False,
                "defaults": {
                    "server": env_config.server,
                    "email": env_config.email,
                    "project_key": env_config.project_key,
                    "has_api_token": bool(env_config.api_token),
                },
            }
        )

    return JsonResponse(
        {
            "configured": True,
            "server": config.server,
            "email": config.email,
            "project_key": config.project_key,
            "has_api_token": bool(config.api_token),
        }
    )


@require_POST
@login_required
def save_jira_config(request: HttpRequest):
    try:
        config = _jira_config_from_payload(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    request.session["jira_config"] = {
        "server": config.server,
        "email": config.email,
        "api_token": config.api_token,
        "project_key": config.project_key,
    }
    request.session.modified = True
    return JsonResponse(
        {
            "configured": True,
            "server": config.server,
            "email": config.email,
            "project_key": config.project_key,
            "message": "Configuracao Jira salva para esta sessao.",
        }
    )


@require_POST
@login_required
def test_jira_config(request: HttpRequest):
    config = None
    try:
        config = _jira_config_from_payload(request)
        result = testar_comunicacao_jira(config)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": _friendly_jira_error(exc, config.project_key if config else None)}, status=400)

    return JsonResponse(
        {
            "ok": True,
            "message": "Comunicacao com o Jira realizada com sucesso.",
            "project_key": result["project_key"],
            "server_title": result["server_title"],
            "issue_types": result["issue_types"],
        }
    )


@require_GET
@login_required
def job_status(request: HttpRequest, job_id: int):
    job = _get_user_job_or_404(request, job_id)
    events = list(job.events.values("level", "message", "created_at", "metadata"))
    feedbacks = list(
        FeedbackRecord.objects.filter(job=job).values(
            "id",
            "source_id",
            "text",
            "intent",
            "ai_intent",
            "sentiment_score",
            "ai_provider",
            "target_candidate",
            "technical_target",
            "inferred_target",
            "consequence",
            "jira_status",
            "jira_key",
            "processing_error",
        )[:200]
    )
    for feedback in feedbacks:
        feedback["explanation"] = _feedback_explanation(feedback)

    payload = {
        "id": job.id,
        "status": job.status,
        "current_phase": job.current_phase,
        "pipeline_steps": _pipeline_steps(job),
        "csv_inspection": (job.metadata or {}).get("csv_inspection", {}),
        "domain_name": job.domain_name,
        "total_rows": job.total_rows,
        "processed_rows": job.processed_rows,
        "row_limit": job.row_limit,
        "cancel_requested": job.cancel_requested,
        "progress_percent": job.progress_percent,
        "jira_created": job.jira_created,
        "error_message": job.error_message,
        "events": events,
        "feedbacks": feedbacks,
    }
    payload.update(_job_urls(request, job))
    return JsonResponse(payload)


@require_GET
@ensure_csrf_cookie
@login_required
def dashboard(request: HttpRequest):
    jobs = list(_user_jobs(request)[:50])
    selected_job = _resolve_selected_job(request, jobs=jobs)
    sentiment_filter, consequence_filter = _parse_filters(request)
    return render(
        request,
        "pipeline/dashboard.html",
        {
            "jobs": jobs,
            "selected_job": selected_job,
            "selected_sentiment": sentiment_filter,
            "selected_consequence": consequence_filter,
            "active_nav": "dashboard",
        },
    )


@require_GET
@login_required
def dashboard_data(request: HttpRequest):
    selected_job = _resolve_selected_job(request)
    if selected_job is None:
        return JsonResponse(
            {
                "job": None,
                "cards": {"total_feedbacks": 0, "negative_percent": 0, "jira_tickets": 0},
                "charts": {
                    "consequence_distribution": {"labels": [], "data": []},
                    "top_critical_features": {"labels": [], "data": []},
                    "sentiment_by_category": {"labels": [], "data": []},
                },
                "top_critical_issues": [],
                "export_urls": {"csv": "", "docx": ""},
            }
        )

    sentiment_filter, consequence_filter = _parse_filters(request)
    feedbacks = _filtered_feedbacks(selected_job, sentiment_filter, consequence_filter)
    snapshot = _build_dashboard_snapshot(request, selected_job, feedbacks, sentiment_filter, consequence_filter)
    return JsonResponse(snapshot)


@require_GET
@login_required
def detailed_results(request: HttpRequest):
    jobs = list(_user_jobs(request)[:50])
    selected_job = _resolve_selected_job(request, jobs=jobs)
    sentiment_filter, consequence_filter = _parse_filters(request)

    page_obj = None
    rows = []
    if selected_job is not None:
        feedbacks = _filtered_feedbacks(selected_job, sentiment_filter, consequence_filter).select_related("agent").prefetch_related("targets", "consequences").order_by("-id")
        paginator = Paginator(feedbacks, 50)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
        rows = list(page_obj.object_list)

    return render(
        request,
        "pipeline/detailed_results.html",
        {
            "active_nav": "results",
            "jobs": jobs,
            "selected_job": selected_job,
            "selected_sentiment": sentiment_filter,
            "selected_consequence": consequence_filter,
            "rows": rows,
            "page_obj": page_obj,
            "base_query": _query_without_page(request),
        },
    )


@require_GET
@login_required
def export_csv(request: HttpRequest):
    job = _require_selected_job(request)
    sentiment_filter, consequence_filter = _parse_filters(request)
    feedbacks = _filtered_feedbacks(job, sentiment_filter, consequence_filter).order_by("id")
    ontology = FeedOnOntologyService()

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = f"feed-on-job-{job.id}-resultados.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(
        [
            "ID",
            "Texto Original",
            "Sentimento",
            "Alvo IA",
            "Alvo Inferido",
            "Consequencia",
            "Agente",
            "Tecnica de Elicitacao",
            "Provedor da Analise",
            "Link do Jira",
        ]
    )

    for record in feedbacks.iterator(chunk_size=500):
        writer.writerow(
            [
                record.source_id,
                record.text,
                _format_sentiment(record.sentiment_score),
                record.target_candidate,
                "|".join(f"{item.target_type}.{item.target_name}" for item in record.targets.all()) or _semantic_inferred_target(ontology, record),
                "|".join(item.consequence_type for item in record.consequences.all()) or record.consequence,
                record.agent.pseudonym if record.agent else "",
                record.elicitation_technique,
                record.ai_provider,
                _jira_issue_url(record.jira_key),
            ]
        )

    return response


@require_GET
@login_required
def export_docx(request: HttpRequest):
    job = _require_selected_job(request)
    sentiment_filter, consequence_filter = _parse_filters(request)
    feedbacks = _filtered_feedbacks(job, sentiment_filter, consequence_filter).order_by("id")

    try:
        buffer = build_executive_report_docx(job, feedbacks)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=503)

    filename = f"feed-on-job-{job.id}-relatorio-executivo.docx"
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _parse_row_limit(raw_value: str) -> tuple[int | None, str]:
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None, ""
    try:
        value = int(raw_value)
    except ValueError:
        return None, "O limite precisa ser um numero inteiro."
    if value <= 0:
        return None, "O limite precisa ser maior que zero."
    return value, ""


def _looks_like_supported_feedback_upload(upload) -> bool:
    filename = (upload.name or "").strip().lower()
    content_type = (getattr(upload, "content_type", "") or "").split(";", 1)[0].strip().lower()
    if filename.endswith((".xlsx", ".xlsm")):
        return True

    excel_content_types = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
    }
    if content_type in excel_content_types:
        return True

    allowed_content_types = {
        "text/csv",
        "application/csv",
        "text/comma-separated-values",
    }
    if filename.endswith(".csv") or content_type in allowed_content_types:
        return True

    try:
        sample = upload.read(4096)
        upload.seek(0)
    except Exception:
        return False

    if not sample:
        return False

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = sample.decode(encoding)
            break
        except UnicodeDecodeError:
            text = ""
    if not text.strip():
        return False

    try:
        dialect = csv.Sniffer().sniff(text, delimiters=",;\t|")
    except csv.Error:
        return False

    first_line = text.splitlines()[0] if text.splitlines() else ""
    return dialect.delimiter in first_line


def _job_urls(request: HttpRequest, job: ProcessingJob) -> dict:
    dashboard_url = f"{reverse('pipeline:dashboard')}?{urlencode({'job': job.id})}"
    return {
        "job_id": job.id,
        "dashboard_url": request.build_absolute_uri(dashboard_url),
        "status_url": request.build_absolute_uri(reverse("pipeline:job_status", args=[job.id])),
        "cancel_url": request.build_absolute_uri(reverse("pipeline:cancel_job", args=[job.id])),
        "delete_url": request.build_absolute_uri(reverse("pipeline:delete_job", args=[job.id])),
        "export_jira_url": request.build_absolute_uri(reverse("pipeline:export_selected_to_jira", args=[job.id])),
    }


def _start_local_background_job(job_id: int) -> None:
    def runner() -> None:
        close_old_connections()
        try:
            process_job(job_id)
        finally:
            close_old_connections()

    Thread(target=runner, daemon=True).start()


def _user_jobs(request: HttpRequest):
    return ProcessingJob.objects.filter(owner=request.user)


def _get_user_job_or_404(request: HttpRequest, job_id: int) -> ProcessingJob:
    return get_object_or_404(_user_jobs(request), pk=job_id)


def _resolve_selected_job(request: HttpRequest, jobs: list[ProcessingJob] | None = None) -> ProcessingJob | None:
    selected_job_id = request.GET.get("job")
    if selected_job_id:
        try:
            return _user_jobs(request).get(pk=int(selected_job_id))
        except (ValueError, ProcessingJob.DoesNotExist):
            pass

    if jobs is not None:
        return jobs[0] if jobs else None

    return _user_jobs(request).first()


def _require_selected_job(request: HttpRequest) -> ProcessingJob:
    selected_job = _resolve_selected_job(request)
    if selected_job is None:
        raise Http404("Nenhum job disponivel para exportacao.")
    return selected_job


def _parse_filters(request: HttpRequest) -> tuple[str, str]:
    sentiment_filter = (request.GET.get("sentiment") or "all").strip().lower()
    if sentiment_filter not in ALLOWED_SENTIMENT_FILTERS:
        sentiment_filter = "all"

    consequence_filter = (request.GET.get("consequence") or "all").strip()
    if consequence_filter not in ALLOWED_CONSEQUENCE_FILTERS:
        consequence_filter = "all"

    return sentiment_filter, consequence_filter


def _filtered_feedbacks(job: ProcessingJob, sentiment_filter: str, consequence_filter: str):
    feedbacks = FeedbackRecord.objects.filter(job=job)

    if sentiment_filter == "negative":
        feedbacks = feedbacks.filter(sentiment_score__lt=NEGATIVE_THRESHOLD)
    elif sentiment_filter == "neutral":
        feedbacks = feedbacks.filter(sentiment_score__gte=NEGATIVE_THRESHOLD, sentiment_score__lte=POSITIVE_THRESHOLD)
    elif sentiment_filter == "positive":
        feedbacks = feedbacks.filter(sentiment_score__gt=POSITIVE_THRESHOLD)

    if consequence_filter != "all":
        feedbacks = feedbacks.filter(Q(consequences__consequence_type=consequence_filter) | Q(consequence=consequence_filter)).distinct()

    return feedbacks


def _build_dashboard_snapshot(
    request: HttpRequest,
    job: ProcessingJob,
    feedbacks,
    sentiment_filter: str,
    consequence_filter: str,
) -> dict:
    total_feedbacks = feedbacks.count()
    negative_count = feedbacks.filter(sentiment_score__lt=NEGATIVE_THRESHOLD).count()
    negative_percent = round((negative_count / total_feedbacks) * 100, 1) if total_feedbacks else 0
    jira_tickets = feedbacks.filter(
        Q(jira_status=FeedbackRecord.JiraStatus.CREATED) | Q(jira_status=FeedbackRecord.JiraStatus.DRY_RUN)
    ).count()

    consequence_counts = {label: 0 for label in ("Correction", "Improvement", "Prioritization")}
    for item in feedbacks.values("consequences__consequence_type").annotate(total=Count("id", distinct=True)):
        consequence = item["consequences__consequence_type"] or ""
        if consequence in consequence_counts:
            consequence_counts[consequence] = item["total"]
    for item in feedbacks.filter(consequences__isnull=True).values("consequence").annotate(total=Count("id")):
        if item["consequence"] in consequence_counts:
            consequence_counts[item["consequence"]] += item["total"]

    if total_feedbacks:
        consequence_distribution = [round((value / total_feedbacks) * 100, 1) for value in consequence_counts.values()]
    else:
        consequence_distribution = [0, 0, 0]

    top_critical = list(
        feedbacks.filter(consequence="Correction")
        .exclude(inferred_target="")
        .values("inferred_target")
        .annotate(total=Count("id"))
        .order_by("-total", "inferred_target")[:5]
    )

    sentiment_by_category = list(
        feedbacks.exclude(sentiment_score__isnull=True)
        .exclude(inferred_target="")
        .values("inferred_target")
        .annotate(avg_sentiment=Avg("sentiment_score"), total=Count("id"))
        .order_by("-total", "inferred_target")[:6]
    )

    dashboard_rows = list(
        feedbacks.annotate(sentiment_sort=Coalesce("sentiment_score", Value(1.0), output_field=FloatField()))
        .order_by("sentiment_sort", "id")
        .values("id", "source_id", "text", "sentiment_score", "inferred_target", "consequence", "jira_key", "jira_status")[:50]
    )

    filter_query = urlencode({"job": job.id, "sentiment": sentiment_filter, "consequence": consequence_filter})
    csv_url = f"{reverse('pipeline:export_csv')}?{filter_query}"
    docx_url = f"{reverse('pipeline:export_docx')}?{filter_query}"

    return {
        "job": {
            "id": job.id,
            "status": job.status,
            "filename": job.original_filename,
            "domain_name": job.domain_name,
            "processed_rows": job.processed_rows,
            "total_rows": job.total_rows,
            "finished_at": timezone.localtime(job.finished_at).isoformat() if job.finished_at else None,
            "export_jira_url": request.build_absolute_uri(reverse("pipeline:export_selected_to_jira", args=[job.id])),
        },
        "filters": {"sentiment": sentiment_filter, "consequence": consequence_filter},
        "cards": {
            "total_feedbacks": total_feedbacks,
            "negative_percent": negative_percent,
            "jira_tickets": jira_tickets,
            "agents": job.agents.count(),
            "with_context": feedbacks.filter(context__isnull=False).count(),
            "reasoner": (job.metadata or {}).get("reasoner", {}),
        },
        "charts": {
            "consequence_distribution": {
                "labels": list(consequence_counts.keys()),
                "data": consequence_distribution,
            },
            "top_critical_features": {
                "labels": [item["inferred_target"] for item in top_critical],
                "data": [item["total"] for item in top_critical],
            },
            "sentiment_by_category": {
                "labels": [item["inferred_target"] for item in sentiment_by_category],
                "data": [round(item["avg_sentiment"] or 0, 3) for item in sentiment_by_category],
            },
        },
        "top_critical_issues": [
            {
                "id": item["id"],
                "source_id": item["source_id"],
                "text": item["text"],
                "sentiment_score": _format_sentiment(item["sentiment_score"]),
                "inferred_target": item["inferred_target"] or "-",
                "consequence": item["consequence"] or "-",
                "jira_key": item["jira_key"] or "-",
                "jira_status": item["jira_status"] or FeedbackRecord.JiraStatus.PENDING,
                "jira_url": _jira_issue_url(item["jira_key"]),
            }
            for item in dashboard_rows
        ],
        "export_urls": {
            "csv": request.build_absolute_uri(csv_url),
            "docx": request.build_absolute_uri(docx_url),
        },
    }


def _semantic_inferred_target(ontology: FeedOnOntologyService, record: FeedbackRecord) -> str:
    source_id = f"{record.source_id}_{record.id}"
    technical_target = record.technical_target or record.inferred_target or "Feature.General"

    try:
        result = ontology.interpret(
            source_id=source_id,
            text=record.text,
            intent=record.intent,
            technical_target=technical_target,
            domain_name=getattr(record.job, "domain_name", "geral"),
            create_jira_issue=False,
        )
        semantic_value = ontology.inferred_target_for(source_id)
        return semantic_value or result.inferred_target or record.inferred_target or technical_target
    except Exception:
        return record.inferred_target or technical_target


def _jira_issue_url(jira_key: str) -> str:
    if not jira_key:
        return ""
    base = (settings.JIRA_SERVER or settings.JIRA_URL or "").rstrip("/")
    if not base:
        return jira_key
    return f"{base}/browse/{jira_key}"


def _format_sentiment(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _pipeline_steps(job: ProcessingJob) -> list[dict]:
    steps = list((job.metadata or {}).get("pipeline_steps") or [])
    if steps:
        return steps
    return [
        {"key": "upload", "label": "Upload recebido", "status": "completed" if job.id else "pending", "message": ""},
        {"key": "csv_validation", "label": "Leitura e validacao do arquivo", "status": "pending", "message": ""},
        {"key": "ai_processing", "label": "Analise IA / fallback local", "status": "pending", "message": ""},
        {"key": "ontology", "label": "Instanciacao FEED-ON", "status": "pending", "message": ""},
        {"key": "reasoner", "label": "Reasoner ontologico", "status": "pending", "message": ""},
        {"key": "jira", "label": "Exportacao manual Jira", "status": "pending", "message": ""},
        {"key": "done", "label": "Finalizacao", "status": "pending", "message": ""},
    ]


def _feedback_explanation(row: dict) -> dict:
    provider = row.get("ai_provider") or "local"
    ai_intent = row.get("ai_intent") or "-"
    intent = row.get("intent") or "-"
    sentiment = _format_sentiment(row.get("sentiment_score"))
    target_candidate = row.get("target_candidate") or "-"
    technical_target = row.get("technical_target") or "-"
    inferred_target = row.get("inferred_target") or technical_target
    consequence = row.get("consequence") or "-"

    reasons = []
    if consequence == "Correction":
        reasons.append("classificado como correcao por intencao de reporte, sentimento negativo ou sinal de falha")
    elif consequence == "Improvement":
        reasons.append("classificado como melhoria por intencao de sugestao ou sentimento nao negativo")
    elif consequence == "Prioritization":
        reasons.append("classificado como priorizacao por sinal de urgencia/criticidade")
    if provider == "openai":
        reasons.append("analise semantica feita via OpenAI")
    elif provider == "csv":
        reasons.append("intencao aproveitada do proprio CSV")
    else:
        reasons.append("analise semantica feita por fallback local")
    if inferred_target and inferred_target != technical_target:
        reasons.append("alvo enriquecido pela relacao partOf da FEED-ON")

    return {
        "summary": f"{consequence}: {technical_target} -> {inferred_target}",
        "details": [
            f"Intencao FEED-ON: {intent}",
            f"Intencao IA/origem: {ai_intent}",
            f"Sentimento: {sentiment}",
            f"Alvo candidato: {target_candidate}",
            f"Alvo tecnico: {technical_target}",
            f"Alvo inferido: {inferred_target or '-'}",
            f"Fonte: {provider}",
        ],
        "reason": "; ".join(reasons) + ".",
    }


def _selected_feedback_ids_from_request(request: HttpRequest) -> list[int]:
    content_type = (request.content_type or "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Payload JSON invalido: {exc}") from exc
        raw_ids = data.get("feedbacks", data.get("feedback_ids", []))
    else:
        raw_ids = request.POST.getlist("feedbacks") or request.POST.getlist("feedback_ids")
        if not raw_ids:
            raw_ids = request.POST.get("feedbacks") or request.POST.get("feedback_ids") or []

    if isinstance(raw_ids, str):
        try:
            parsed = json.loads(raw_ids)
            raw_ids = parsed if isinstance(parsed, list) else raw_ids
        except json.JSONDecodeError:
            raw_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]

    if not isinstance(raw_ids, list):
        raise ValueError("O payload deve enviar uma lista no campo 'feedbacks'.")

    try:
        selected_ids = [int(item) for item in raw_ids]
    except (TypeError, ValueError) as exc:
        raise ValueError("Todos os itens de 'feedbacks' precisam ser IDs inteiros.") from exc

    return list(dict.fromkeys(selected_ids))


def _jira_config_from_session(request: HttpRequest) -> JiraConfig | None:
    raw = request.session.get("jira_config") or {}
    config = JiraConfig(
        server=(raw.get("server") or "").strip(),
        email=(raw.get("email") or "").strip(),
        api_token=(raw.get("api_token") or "").strip(),
        project_key=(raw.get("project_key") or "").strip().upper(),
    )
    if all([config.server, config.email, config.api_token, config.project_key]):
        return config
    return None


def _jira_config_from_payload(request: HttpRequest) -> JiraConfig:
    if (request.content_type or "").split(";", 1)[0].strip().lower() == "application/json":
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Payload JSON invalido: {exc}") from exc
    else:
        data = request.POST

    config = JiraConfig(
        server=(data.get("server") or "").strip().rstrip("/"),
        email=(data.get("email") or "").strip(),
        api_token=(data.get("api_token") or "").strip(),
        project_key=(data.get("project_key") or "").strip().upper(),
    )
    missing = []
    if not config.server:
        missing.append("URL do Jira")
    if not config.email:
        missing.append("E-mail/usuario")
    if not config.api_token:
        missing.append("API token")
    if not config.project_key:
        missing.append("Chave do projeto")
    if missing:
        raise ValueError(f"Preencha os campos obrigatorios: {', '.join(missing)}.")
    return config


def _friendly_jira_error(exc: Exception, project_key: str | None = None) -> str:
    message = str(exc)
    lowered = message.lower()
    key = project_key or settings.JIRA_PROJECT_KEY or "FEED"
    if "specify a valid issue type" in lowered or '"issuetype"' in lowered:
        return (
            "O Jira rejeitou todos os tipos tecnicos testados para criar o item no backlog. "
            "A classificacao do feedback ja esta indo apenas na descricao; ainda assim a API do Jira exige um tipo interno valido. "
            f"Confira se a conta tem permissao para criar issues no projeto {key}."
        )
    if "no project could be found" in lowered or "browse projects" in lowered or "create issues" in lowered:
        return (
            "Nao foi possivel acessar o projeto Jira configurado. "
            f"A tentativa usou a chave de projeto {key}. "
            "Abra 'Configurar Jira', confirme a chave correta e salve novamente; "
            "confira tambem se a conta autenticada tem permissao para visualizar esse projeto e criar issues."
        )
    return message


def _jira_export_row(record: FeedbackRecord) -> dict:
    return {
        "id": record.id,
        "source_id": record.source_id,
        "jira_key": record.jira_key,
        "jira_status": record.jira_status,
        "jira_url": _jira_issue_url(record.jira_key),
    }


def _target_class_for_jira(record: FeedbackRecord) -> str:
    value = record.inferred_target or record.technical_target or record.target_candidate or "Feature"
    for separator in ("_", "."):
        if separator in value:
            return value.split(separator, 1)[0] or "Feature"
    return value or "Feature"


def _sync_jira_key_to_ontology(record: FeedbackRecord, ontology_source_id: str, jira_key: str) -> str:
    try:
        atualizar_jira_key_na_ontologia(ontology_source_id, record.consequence, jira_key)
        return ""
    except Exception as exc:
        message = (
            "Ticket Jira criado, mas nao foi possivel sincronizar a chave na ontologia: "
            f"{exc}"
        )
        PipelineEvent.objects.create(
            job=record.job,
            level=PipelineEvent.Level.WARNING,
            message=message[:255],
            metadata={
                "feedback_id": record.id,
                "source_id": record.source_id,
                "ontology_source_id": ontology_source_id,
                "jira_key": jira_key,
                "error": str(exc),
            },
        )
        return str(exc)


def _ontology_source_id_for_record(record: FeedbackRecord) -> str:
    payload = record.jira_payload or {}
    ontology_source_id = payload.get("ontology_source_id")
    if ontology_source_id:
        return ontology_source_id

    ordered_ids = list(FeedbackRecord.objects.filter(job=record.job).order_by("id").values_list("id", flat=True))
    try:
        ordinal = ordered_ids.index(record.id) + 1
    except ValueError:
        ordinal = record.id
    return f"{record.source_id or 'unknown'}__job{record.job_id}__row{ordinal}"


def _chart_textual_summary(labels: list[str], values: list[float], suffix: str) -> str:
    if not labels:
        return "sem dados para o filtro aplicado."

    parts = []
    for label, value in zip(labels, values):
        if isinstance(value, float):
            formatted = f"{value:.1f}"
        else:
            formatted = str(value)
        parts.append(f"{label}: {formatted} {suffix}".strip())
    return "; ".join(parts)


def _query_without_page(request: HttpRequest) -> str:
    query = request.GET.copy()
    query.pop("page", None)
    return query.urlencode()
