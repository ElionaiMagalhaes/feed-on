from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pipeline", "0002_cancel_and_limit"),
    ]

    operations = [
        migrations.AddField(
            model_name="feedbackrecord",
            name="ai_intent",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="feedbackrecord",
            name="sentiment_score",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="feedbackrecord",
            name="ai_provider",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="feedbackrecord",
            name="ai_raw",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
