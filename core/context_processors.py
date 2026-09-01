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

    from core.scoping import scoped

    badges = {}
    if user.has_access("product.view"):
        badges["badge_low_stock"] = (
            scoped(Product.objects.all(), user).low_stock().count()
        )
    if user.has_access("credit.view"):
        badges["badge_overdue_debts"] = (
            scoped(DebtRecord.objects.all(), user).overdue().count()
        )
    return badges
