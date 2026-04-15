from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pipeline", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingjob",
            name="cancel_requested",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="canceled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="row_limit",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="processingjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pendente"),
                    ("running", "Em processamento"),
                    ("canceling", "Cancelando"),
                    ("canceled", "Cancelado"),
                    ("completed", "Concluido"),
                    ("failed", "Falhou"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
