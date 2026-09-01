"""
Roles become data, and users gain per-person access overrides.

The `role` column keeps its name and its values - ADMIN and MANAGER rows carry
straight over. What changes is that the choices move out of the field and into
the RoleDefinition table, so a third role (SALES) and any number of custom
ones can exist without a code change.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_user_avatar"),
    ]

    operations = [
        migrations.CreateModel(
            name="RoleDefinition",
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
                    "code",
                    models.CharField(
                        help_text="Short uppercase identifier, e.g. CASHIER. Never changes.",
                        max_length=32,
                        unique=True,
                    ),
                ),
                ("name", models.CharField(max_length=60)),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        help_text="Shown to administrators when picking a role for someone.",
                    ),
                ),
                (
                    "permissions",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text='Permission codes from the catalogue, or ["*"] for full access.',
                    ),
                ),
                (
                    "data_scope",
                    models.CharField(
                        choices=[
                            ("OWN", "Own records only"),
                            ("TEAM", "Own records and their team's"),
                            ("ALL", "Everything in the business"),
                        ],
                        default="OWN",
                        help_text="Whose records this role may see.",
                        max_length=8,
                    ),
                ),
                (
                    "is_system",
                    models.BooleanField(
                        default=False,
                        help_text="Built-in role. Its code and system status cannot be "
                        "changed, and it cannot be deleted.",
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                (
                    "rank",
                    models.PositiveSmallIntegerField(
                        default=50,
                        help_text="Sort order. Lower is more senior - 10 Admin, "
                        "20 Manager, 30 Sales.",
                    ),
                ),
            ],
            options={
                "verbose_name": "Role",
                "verbose_name_plural": "Roles",
                "ordering": ["rank", "name"],
            },
        ),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                db_index=True,
                default="MANAGER",
                help_text="Which role's permissions this user starts from.",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="manager",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "For a sales user: whose stock they sell. They see that person's "
                    "products, while their own sales, customers and debts stay theirs."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="team",
                to="accounts.user",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="data_scope_override",
            field=models.CharField(
                blank=True,
                choices=[
                    ("OWN", "Own records only"),
                    ("TEAM", "Own records and their team's"),
                    ("ALL", "Everything in the business"),
                ],
                default="",
                help_text="Leave blank to use the role's scope.",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="extra_permissions",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Granted to this person on top of their role.",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="denied_permissions",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Taken away from this person even though their role has it.",
            ),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("CREATE", "Created"),
                    ("UPDATE", "Updated"),
                    ("DELETE", "Deleted"),
                    ("VOID", "Voided"),
                    ("LOGIN", "Logged in"),
                    ("LOGOUT", "Logged out"),
                    ("LOGIN_FAILED", "Failed login"),
                    ("PAYMENT", "Payment recorded"),
                    ("STOCK", "Stock adjusted"),
                    ("OVERRIDE", "Admin override"),
                    ("EXPORT", "Data exported"),
                    ("ACCESS", "Access changed"),
                ],
                db_index=True,
                max_length=20,
            ),
        ),
    ]
