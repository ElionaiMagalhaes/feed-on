from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0005_processingjob_owner_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="processingjob",
            name="domain_name",
            field=models.CharField(default="geral", max_length=100),
        ),
        migrations.CreateModel(
            name="DomainLexicon",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain_name", models.CharField(max_length=100, unique=True)),
                ("ui_elements", models.TextField(blank=True)),
                ("quality_attributes", models.TextField(blank=True)),
                ("requirements", models.TextField(blank=True)),
                ("processes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["domain_name"],
            },
        ),
    ]
