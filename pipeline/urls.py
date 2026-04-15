from django.urls import path

from . import views

app_name = "pipeline"

urlpatterns = [
    path("", views.index, name="index"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/data/", views.dashboard_data, name="dashboard_data"),
    path("resultados/", views.detailed_results, name="detailed_results"),
    path("export/csv/", views.export_csv, name="export_csv"),
    path("export/docx/", views.export_docx, name="export_docx"),
    path("jobs/", views.create_job, name="create_job"),
    path("jobs/<int:job_id>/status/", views.job_status, name="job_status"),
    path("jobs/<int:job_id>/cancel/", views.cancel_job, name="cancel_job"),
]
