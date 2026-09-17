"""
Two additions to the yard.

ProductionDamage
    A shift does not fail in one way. Splitting 'rejected: 23' into what
    actually went wrong is the only version of that number anybody can act on.

ProductionRequest
    The counter asking a named person for a stated quantity, and that person
    answering. The automatic low-stock alert says something is nearly gone; it
    does not say how many are wanted or whether anybody agreed to make them.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0001_initial"),
        ("core", "0002_option"),
        ("inventory", "0004_alter_stockmovement_movement_type"),
        ("accounts", "0005_registration_passcode"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductionDamage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "type_name",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        help_text="What it was called at the time. Survives the "
                        "option going.",
                        max_length=120,
                    ),
                ),
                (
                    "quantity",
                    models.PositiveIntegerField(
                        default=0, help_text="How many units failed this way."
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=255)),
                (
                    "damage_type",
                    models.ForeignKey(
                        blank=True,
                        help_text="An entry from the DAMAGE_TYPE list.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="production_damages",
                        to="core.option",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="damages",
                        to="production.productionrun",
                    ),
                ),
            ],
            options={
                "verbose_name": "Production damage",
                "verbose_name_plural": "Production damage",
                "ordering": ["-quantity", "id"],
            },
        ),
        migrations.CreateModel(
            name="ProductionRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "quantity",
                    models.PositiveIntegerField(
                        help_text="How many units are being asked for."
                    ),
                ),
                ("reason_name", models.CharField(blank=True, max_length=120)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("needed_by", models.DateField(blank=True, null=True)),
                (
                    "stock_at_request",
                    models.IntegerField(
                        default=0,
                        help_text="What the shelf held when this was raised.",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Waiting for an answer"),
                            ("ACCEPTED", "Accepted - will be produced"),
                            ("DECLINED", "Declined"),
                            ("FULFILLED", "Produced and added to stock"),
                            ("CANCELLED", "Cancelled by the person who asked"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=10,
                    ),
                ),
                ("responded_at", models.DateTimeField(blank=True, null=True)),
                ("response_note", models.CharField(blank=True, max_length=255)),
                (
                    "assigned_to",
                    models.ForeignKey(
                        help_text="The manager or administrator being asked.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="production_requests_received",
                        to="accounts.user",
                    ),
                ),
                (
                    "fulfilled_run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="requests_fulfilled",
                        to="production.productionrun",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="production_requests",
                        to="inventory.product",
                    ),
                ),
                (
                    "reason",
                    models.ForeignKey(
                        blank=True,
                        help_text="An entry from the PRODUCTION_REQUEST_REASON list.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="production_requests",
                        to="core.option",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="production_requests_made",
                        to="accounts.user",
                    ),
                ),
            ],
            options={
                "verbose_name": "Production request",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="productiondamage",
            index=models.Index(fields=["run"], name="damage_run_idx"),
        ),
        migrations.AddIndex(
            model_name="productiondamage",
            index=models.Index(fields=["type_name"], name="damage_type_name_idx"),
        ),
        migrations.AddConstraint(
            model_name="productiondamage",
            constraint=models.CheckConstraint(
                condition=models.Q(("quantity__gt", 0)),
                name="damage_quantity_positive",
            ),
        ),
        migrations.AddIndex(
            model_name="productionrequest",
            index=models.Index(
                fields=["assigned_to", "status", "-created_at"],
                name="request_assigned_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="productionrequest",
            index=models.Index(
                fields=["requested_by", "-created_at"], name="request_asker_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="productionrequest",
            index=models.Index(
                fields=["product", "status"], name="request_product_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="productionrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(("quantity__gt", 0)),
                name="request_quantity_positive",
            ),
        ),
    ]
