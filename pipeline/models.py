from django.db import models
from django.conf import settings
from django.utils import timezone


class ProcessingJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendente"
        RUNNING = "running", "Em processamento"
        CANCELING = "canceling", "Cancelando"
        CANCELED = "canceled", "Cancelado"
        COMPLETED = "completed", "Concluido"
        FAILED = "failed", "Falhou"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_jobs",
    )
    original_filename = models.CharField(max_length=255)
    upload = models.FileField(upload_to="uploads/%Y/%m/%d/")
    domain_name = models.CharField(max_length=100, default="geral")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    row_limit = models.PositiveIntegerField(null=True, blank=True)
    jira_created = models.PositiveIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    current_phase = models.CharField(max_length=120, blank=True)
    error_message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="pipeline_job_owner_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.status})"

    @property
    def progress_percent(self) -> int:
        if self.total_rows <= 0:
            return 0
        return min(100, round((self.processed_rows / self.total_rows) * 100))

    def mark_running(self, phase: str) -> None:
        self.status = self.Status.RUNNING
        self.current_phase = phase
        if self.started_at is None:
            self.started_at = timezone.now()
        self.save(update_fields=["status", "current_phase", "started_at", "updated_at"])

    def request_cancel(self) -> None:
        self.cancel_requested = True
        if self.status in {self.Status.PENDING, self.Status.RUNNING}:
            self.status = self.Status.CANCELING
            self.current_phase = "Cancelamento solicitado..."
        self.save(update_fields=["cancel_requested", "status", "current_phase", "updated_at"])

    def mark_canceled(self, phase: str = "Processamento cancelado") -> None:
        self.status = self.Status.CANCELED
        self.cancel_requested = True
        self.current_phase = phase
        now = timezone.now()
        self.finished_at = now
        self.canceled_at = now
        self.save(update_fields=["status", "cancel_requested", "current_phase", "finished_at", "canceled_at", "updated_at"])

    def mark_completed(self, phase: str = "Concluido") -> None:
        self.status = self.Status.COMPLETED
        self.current_phase = phase
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "current_phase", "finished_at", "updated_at"])

    def mark_failed(self, message: str) -> None:
        self.status = self.Status.FAILED
        self.current_phase = "Falha no processamento"
        self.error_message = message
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "current_phase", "error_message", "finished_at", "updated_at"])


class DomainLexicon(models.Model):
    domain_name = models.CharField(max_length=100, unique=True)
    ui_elements = models.TextField(blank=True)
    quality_attributes = models.TextField(blank=True)
    requirements = models.TextField(blank=True)
    processes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["domain_name"]

    def __str__(self) -> str:
        return self.domain_name


class FeedbackRecord(models.Model):
    class JiraStatus(models.TextChoices):
        PENDING = "pending", "Pendente"
        CREATED = "created", "Criado"
        FAILED = "failed", "Falhou"
        DRY_RUN = "dry_run", "Dry-run"

    job = models.ForeignKey(ProcessingJob, on_delete=models.CASCADE, related_name="feedbacks")
    source_id = models.CharField(max_length=120)
    text = models.TextField()
    intent = models.CharField(max_length=80, blank=True)
    ai_intent = models.CharField(max_length=40, blank=True)
    sentiment_score = models.FloatField(null=True, blank=True)
    ai_provider = models.CharField(max_length=40, blank=True)
    target_candidate = models.CharField(max_length=160, blank=True)
    ai_raw = models.JSONField(default=dict, blank=True)
    technical_target = models.CharField(max_length=160, blank=True)
    inferred_target = models.CharField(max_length=220, blank=True)
    consequence = models.CharField(max_length=80, blank=True)
    jira_payload = models.JSONField(default=dict, blank=True)
    jira_key = models.CharField(max_length=80, blank=True)
    jira_status = models.CharField(max_length=20, choices=JiraStatus.choices, default=JiraStatus.PENDING)
    processing_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["job", "source_id"], name="pipeline_fe_job_id_2b76c9_idx"),
            models.Index(fields=["jira_status"], name="pipeline_fe_jira_st_9c1f4a_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.source_id}: {self.text[:60]}"


class PipelineEvent(models.Model):
    class Level(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Aviso"
        ERROR = "error", "Erro"

    job = models.ForeignKey(ProcessingJob, on_delete=models.CASCADE, related_name="events")
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.INFO)
    message = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return self.message





