from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pipeline", "0003_ai_analysis_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="feedbackrecord",
            name="target_candidate",
            field=models.CharField(blank=True, max_length=160),
        ),
    ]
