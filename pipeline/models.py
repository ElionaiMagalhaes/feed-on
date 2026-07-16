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


class DomainLexiconTerm(models.Model):
    lexicon = models.ForeignKey(DomainLexicon, related_name="terms", on_delete=models.CASCADE)
    expression = models.CharField(max_length=220)
    normalized_expression = models.CharField(max_length=220)
    canonical_name = models.CharField(max_length=220)
    target_type = models.CharField(max_length=80)
    language = models.CharField(max_length=20, default="pt-BR")
    source = models.CharField(max_length=80, default="domain_lexicon")
    active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-active", "-normalized_expression"]
        constraints = [
            models.UniqueConstraint(fields=["lexicon", "normalized_expression", "target_type"], name="pipeline_unique_lexicon_term"),
        ]


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
    agent = models.ForeignKey("FeedbackAgent", related_name="feedbacks", on_delete=models.SET_NULL, null=True, blank=True)
    elicitation_technique = models.CharField(max_length=80, blank=True)
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

    @property
    def analysis_provider(self) -> str:
        return self.ai_provider


class FeedbackAgent(models.Model):
    job = models.ForeignKey(ProcessingJob, related_name="agents", on_delete=models.CASCADE)
    pseudonym = models.CharField(max_length=120)
    source_hash = models.CharField(max_length=128)
    role_type = models.CharField(max_length=80, blank=True)
    role_source = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "source_hash"], name="pipeline_unique_job_agent")]


class FeedbackTarget(models.Model):
    feedback = models.ForeignKey(FeedbackRecord, related_name="targets", on_delete=models.CASCADE)
    target_type = models.CharField(max_length=80)
    target_name = models.CharField(max_length=220)
    matched_expression = models.CharField(max_length=220, blank=True)
    source = models.CharField(max_length=80, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    is_primary = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["feedback", "target_type", "target_name"], name="pipeline_unique_feedback_target"),
            models.UniqueConstraint(fields=["feedback"], condition=models.Q(is_primary=True), name="pipeline_one_primary_target"),
        ]


class FeedbackConsequence(models.Model):
    feedback = models.ForeignKey(FeedbackRecord, related_name="consequences", on_delete=models.CASCADE)
    consequence_type = models.CharField(max_length=80)
    derivation_rule = models.CharField(max_length=160, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    is_primary = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["feedback", "consequence_type"], name="pipeline_unique_feedback_consequence"),
            models.UniqueConstraint(fields=["feedback"], condition=models.Q(is_primary=True), name="pipeline_one_primary_consequence"),
        ]


class FeedbackContext(models.Model):
    feedback = models.OneToOneField(FeedbackRecord, related_name="context", on_delete=models.CASCADE)
    timestamp = models.DateTimeField(null=True, blank=True)
    device = models.CharField(max_length=120, blank=True)
    browser = models.CharField(max_length=120, blank=True)
    operating_system = models.CharField(max_length=120, blank=True)
    screen = models.CharField(max_length=160, blank=True)
    module = models.CharField(max_length=160, blank=True)
    environment = models.CharField(max_length=80, blank=True)
    source_channel = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)


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





