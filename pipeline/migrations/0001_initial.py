import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ProcessingJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_filename", models.CharField(max_length=255)),
                ("upload", models.FileField(upload_to="uploads/%Y/%m/%d/")),
                ("status", models.CharField(choices=[("pending", "Pendente"), ("running", "Em processamento"), ("completed", "Concluido"), ("failed", "Falhou")], default="pending", max_length=20)),
                ("total_rows", models.PositiveIntegerField(default=0)),
                ("processed_rows", models.PositiveIntegerField(default=0)),
                ("jira_created", models.PositiveIntegerField(default=0)),
                ("current_phase", models.CharField(blank=True, max_length=120)),
                ("error_message", models.TextField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="FeedbackRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_id", models.CharField(max_length=120)),
                ("text", models.TextField()),
                ("intent", models.CharField(blank=True, max_length=80)),
                ("technical_target", models.CharField(blank=True, max_length=160)),
                ("inferred_target", models.CharField(blank=True, max_length=220)),
                ("consequence", models.CharField(blank=True, max_length=80)),
                ("jira_payload", models.JSONField(blank=True, default=dict)),
                ("jira_key", models.CharField(blank=True, max_length=80)),
                ("jira_status", models.CharField(choices=[("pending", "Pendente"), ("created", "Criado"), ("failed", "Falhou"), ("dry_run", "Dry-run")], default="pending", max_length=20)),
                ("processing_error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="feedbacks", to="pipeline.processingjob")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="PipelineEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("level", models.CharField(choices=[("info", "Info"), ("warning", "Aviso"), ("error", "Erro")], default="info", max_length=20)),
                ("message", models.CharField(max_length=255)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="pipeline.processingjob")),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.AddIndex(
            model_name="feedbackrecord",
            index=models.Index(fields=["job", "source_id"], name="pipeline_fe_job_id_2b76c9_idx"),
        ),
        migrations.AddIndex(
            model_name="feedbackrecord",
            index=models.Index(fields=["jira_status"], name="pipeline_fe_jira_st_9c1f4a_idx"),
        ),
    ]
