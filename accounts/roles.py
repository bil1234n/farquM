"""
The three built-in roles, and how to (re)install them.

These blueprints are the DEFAULTS, not the law. Once seeded, a role lives in
the database and an administrator may edit it - that is the whole point of
making roles data. `ensure_system_roles()` therefore creates what is missing
and leaves what already exists alone, so a deploy never silently undoes a
customer's own tuning.

`reset_to_blueprint()` is the deliberate escape hatch for "put Sales back the
way it shipped", triggered by a button in the role editor rather than by a
migration.

WHY THESE THREE
---------------
ADMIN    The owner. Full access, sees the whole business. There is always at
         least one, and the system refuses to let the last one be demoted or
         deactivated.

MANAGER  Runs the shelf. Owns products and stock, buys them in, prices them,
         counts them. Sells too. Sees their own records plus those of the
         sales staff assigned to them, so they can supervise without being
         shown another manager's books.

SALES    Sells what the manager stocked. Cannot add, edit, price or count
         products, and never sees a cost price. Can register their own
         customers, sell on credit, and collect what they are owed - and only
         ever sees their own sales, customers and debts, so "how much I make"
         is a question about their own numbers.

The gaps are intentional. A sales user has no `sale.discount` and no
`sale.void`: giving away margin and erasing a sale are the two easiest ways to
lose money at a counter, so they start with an administrator and are handed
out per person from Access Control when someone has earned them.
"""
from core.permissions import WILDCARD


# code -> blueprint
BLUEPRINTS: dict[str, dict] = {
    "ADMIN": {
        "name": "Administrator",
        "rank": 10,
        "data_scope": "ALL",
        "description": (
            "The owner. Full control of products, sales, credit, staff and "
            "settings, and the only role that sees every record in the "
            "business."
        ),
        "permissions": [WILDCARD],
    },
    "MANAGER": {
        "name": "Manager",
        "rank": 20,
        "data_scope": "TEAM",
        "description": (
            "Controls the products and the stock. Buys in, prices, counts and "
            "sells. Sees their own records plus those of the sales staff "
            "assigned to them - but not profit margins, staff accounts or "
            "system settings."
        ),
        "permissions": [
            "dashboard.view",
            "product.view",
            "product.create",
            "product.edit",
            "product.archive",
            "product.view_cost",
            "stock.view_movements",
            "stock.restock",
            "stock.adjust",
            "stock.recount",
            "catalog.manage",
            "sale.view",
            "sale.create",
            "sale.credit",
            "sale.discount",
            "sale.receipt.add",
            "customer.view",
            "customer.create",
            "customer.edit",
            "credit.view",
            "credit.collect",
            "credit.reschedule",
            "report.sales",
            "report.inventory",
            "report.receivables",
            "report.export",
        ],
    },
    "SALES": {
        "name": "Sales",
        "rank": 30,
        "data_scope": "OWN",
        "description": (
            "Sells the stock their manager holds. Registers their own "
            "customers, can sell on credit under their own name, and collects "
            "repayments. Sees only their own sales, customers and debts, and "
            "never a cost price."
        ),
        "permissions": [
            "dashboard.view",
            "product.view",
            "sale.view",
            "sale.create",
            "sale.credit",
            "sale.receipt.add",
            "customer.view",
            "customer.create",
            "customer.edit",
            "credit.view",
            "credit.collect",
            "report.sales",
        ],
    },
}


def ensure_system_roles(role_model=None) -> dict:
    """
    Create any missing built-in role. Never overwrites an existing one.

    `role_model` lets a data migration pass in its historical model instead of
    the live one - importing the real class inside a migration would break the
    moment the model changes shape again.
    """
    if role_model is None:
        from .models import RoleDefinition as role_model

    created = {}
    for code, spec in BLUEPRINTS.items():
        obj, was_created = role_model.objects.get_or_create(
            code=code,
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "permissions": list(spec["permissions"]),
                "data_scope": spec["data_scope"],
                "rank": spec["rank"],
                "is_system": True,
                "is_active": True,
            },
        )
        # A role that exists but has drifted off "system" would become
        # deletable, and deleting ADMIN is unrecoverable from the UI.
        if not was_created and not obj.is_system:
            obj.is_system = True
            obj.save(update_fields=["is_system"])
        created[code] = was_created
    return created


def reset_to_blueprint(role) -> bool:
    """Restore one built-in role to its shipped permissions. Returns success."""
    spec = BLUEPRINTS.get(role.code)
    if not spec:
        return False
    role.name = spec["name"]
    role.description = spec["description"]
    role.permissions = list(spec["permissions"])
    role.data_scope = spec["data_scope"]
    role.rank = spec["rank"]
    role.is_system = True
    role.is_active = True
    role.save()
    return True
