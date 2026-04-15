import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "feed_on.settings")

app = Celery("feed_on")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
