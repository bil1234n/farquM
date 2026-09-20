from django.conf import settings


def business_settings(request):
    """
    Business identity and the current user's access, on every page.

    Values come from the editable SystemSetting row and fall back to
    settings.py, so a deployment that has never opened the settings screen
    behaves exactly as it did before that screen existed.
    """
    from accounts.registration import registration_open

    from .models import SystemSetting

    conf = SystemSetting.load()
    user = getattr(request, "user", None)

    return {
        "SETTINGS": conf,
        "BUSINESS_NAME": conf.name,
        "BUSINESS_PHONE": conf.phone,
        "BUSINESS_ADDRESS": conf.address,
        "CURRENCY": conf.currency,
        # Drives the "Create an account" link on the login page. Computed here
        # rather than in the login view because LoginView is Django's, and
        # subclassing it just to add one boolean is more moving parts.
        "registration_open": registration_open(),
        # The signed-in user's effective permission codes, as a plain set.
        # Templates can then write `{% if "sale.void" in USER_PERMS %}`, which
        # is the same check the view performs - not a second, drifting copy of
        # the rule.
        "USER_PERMS": (
            user.effective_permissions
            if user is not None and getattr(user, "is_authenticated", False)
            else frozenset()
        ),
        "DEBUG": settings.DEBUG,
    }


# ---------------------------------------------------------------------------
# Which sidebar link is lit
# ---------------------------------------------------------------------------
# One rule per link, matched on (namespace, url_name) - never on a substring of
# the full view name. The substring version looked tidy and was wrong: the
# string "production:material_list" contains "product", so every page in the
# yard lit up Products as well as its own link. "core:user_access" contains
# "user_" and lit up both Users and Access Control the same way.
#
# Longest url_name prefix wins inside a namespace, so "product_list" can point
# at Products while "product_..." nothing else needs a rule. A page that
# matches nothing simply lights nothing, which is the honest answer for a
# print view or a CSV export.
NAV_RULES: tuple[tuple[str, str, str], ...] = (
    ("reports", "dashboard", "dashboard"),
    ("reports", "sales_report", "sales_report"),
    ("reports", "inventory_report", "inventory_report"),
    ("reports", "receivables_report", "receivables_report"),
    ("reports", "profit_report", "profit_report"),

    ("sales", "sale_create", "new_sale"),
    ("sales", "transaction_", "transactions"),
    ("sales", "receipt_", "transactions"),
    ("sales", "customer_", "customers"),
    ("sales", "delivery_", "deliveries"),

    ("expenses", "expense_", "expenses"),
    ("expenses", "employee_", "employees"),

    ("credit", "dashboard", "credit_dashboard"),
    ("credit", "borrower_", "borrowers"),
    ("credit", "account_", "borrowers"),
    ("credit", "debt_", "debts"),
    ("credit", "repayment_", "debts"),
    ("credit", "bulk_repayment", "debts"),
    ("credit", "aging_report", "aging"),

    ("inventory", "product_", "products"),
    ("inventory", "low_stock", "low_stock"),
    ("inventory", "stock_movements", "stock_movements"),
    ("inventory", "category_", "categories"),
    ("inventory", "supplier_", "suppliers"),

    ("production", "material_", "materials"),
    ("production", "run_", "runs"),
    ("production", "plan_api", "runs"),
    ("production", "recipe_", "recipes"),
    ("production", "request_", "requests"),

    ("accounts", "user_", "users"),
    ("accounts", "audit_log", "audit"),

    ("core", "access_list", "access"),
    ("core", "user_access", "access"),
    ("core", "role_", "settings"),
    ("core", "settings", "settings"),
    ("core", "business_settings", "settings"),
    ("core", "security_settings", "settings"),
)


def nav_active(request):
    """The key of the one sidebar link that should be highlighted."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        return {"NAV": ""}

    namespace = match.namespace or ""
    url_name = match.url_name or ""

    best_key, best_len = "", -1
    for rule_ns, prefix, key in NAV_RULES:
        if rule_ns != namespace or not url_name.startswith(prefix):
            continue
        if len(prefix) > best_len:
            best_key, best_len = key, len(prefix)
    return {"NAV": best_key}


def sidebar_badges(request):
    """
    Live counters rendered in the sidebar. Cheap COUNT queries only.

    Scoped like everything else, and skipped entirely when the user cannot
    open the page the badge points at. An unscoped count is a quiet leak: a
    user with no low stock of their own seeing "7" in the sidebar learns both
    that other people's stock exists and roughly how badly it is running down.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_active:
        return {}

    from credit.models import DebtRecord
    from inventory.models import Product
    from production.models import RawMaterial
    from sales.models import Transaction

    from core.scoping import scoped

    badges = {}
    if user.has_access("product.view"):
        badges["badge_low_stock"] = (
            scoped(Product.objects.all(), user).low_stock().count()
        )
    if user.has_access("material.view"):
        badges["badge_low_material"] = (
            scoped(RawMaterial.objects.all(), user).needs_ordering().count()
        )
    if user.has_access("credit.view"):
        badges["badge_overdue_debts"] = (
            scoped(DebtRecord.objects.all(), user).overdue().count()
        )
    # Sales with goods still in the yard - the stock keeper's to-do list, and
    # a seller's reminder of whose goods are waiting.
    if user.has_access("delivery.view"):
        badges["badge_waiting_deliveries"] = (
            scoped(Transaction.objects.awaiting_collection(), user).count()
        )
    # Requests waiting on THIS person for an answer. Not scoped by ownership
    # like the others - a request is addressed to somebody by name, and the
    # only count worth a badge is the one they personally owe an answer to.
    if user.has_access("production.approve"):
        from production.models import ProductionRequest

        badges["badge_open_requests"] = ProductionRequest.objects.pending().filter(
            assigned_to=user
        ).count()
    return badges
