from django.urls import path

from . import views

app_name = "pipeline"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("app/", views.index, name="index"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/data/", views.dashboard_data, name="dashboard_data"),
    path("resultados/", views.detailed_results, name="detailed_results"),
    path("export/csv/", views.export_csv, name="export_csv"),
    path("export/docx/", views.export_docx, name="export_docx"),
    path("jira/config/", views.jira_config_status, name="jira_config_status"),
    path("jira/config/save/", views.save_jira_config, name="save_jira_config"),
    path("jira/config/test/", views.test_jira_config, name="test_jira_config"),
    path("jobs/", views.create_job, name="create_job"),
    path("jobs/<int:job_id>/status/", views.job_status, name="job_status"),
    path("jobs/<int:job_id>/cancel/", views.cancel_job, name="cancel_job"),
    path("jobs/<int:job_id>/delete/", views.delete_job, name="delete_job"),
    path("jobs/<int:job_id>/export-jira/", views.export_selected_to_jira, name="export_selected_to_jira"),
]
