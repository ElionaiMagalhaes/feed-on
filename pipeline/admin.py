from django.contrib import admin

from .models import FeedbackRecord, PipelineEvent, ProcessingJob


class PipelineEventInline(admin.TabularInline):
    model = PipelineEvent
    extra = 0
    readonly_fields = ("level", "message", "created_at", "metadata")


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "original_filename", "status", "total_rows", "processed_rows", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("original_filename",)
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")
    inlines = [PipelineEventInline]


@admin.register(FeedbackRecord)
class FeedbackRecordAdmin(admin.ModelAdmin):
    list_display = ("source_id", "job", "intent", "ai_intent", "sentiment_score", "target_candidate", "technical_target", "inferred_target", "consequence", "jira_key")
    list_filter = ("intent", "ai_intent", "ai_provider", "consequence", "jira_status")
    search_fields = ("source_id", "text", "jira_key", "inferred_target")


@admin.register(PipelineEvent)
class PipelineEventAdmin(admin.ModelAdmin):
    list_display = ("job", "level", "message", "created_at")
    list_filter = ("level", "created_at")
    search_fields = ("message",)


