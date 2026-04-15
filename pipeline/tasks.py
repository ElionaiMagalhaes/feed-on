from celery import shared_task

from .services.processor import process_job


@shared_task(bind=True)
def process_feedback_job(self, job_id: int) -> dict:
    return process_job(job_id)
