from django.conf import settings


def business_settings(request):
    return {
        "BUSINESS_NAME": settings.BUSINESS_NAME,
        "BUSINESS_PHONE": settings.BUSINESS_PHONE,
        "BUSINESS_ADDRESS": settings.BUSINESS_ADDRESS,
        "CURRENCY": settings.CURRENCY_SYMBOL,
    }


def sidebar_badges(request):
    """
    Live counters rendered in the sidebar. Cheap COUNT queries only.

    Scoped like everything else. An unscoped count is a quiet leak: a manager
    with no low stock of their own seeing "7" in the sidebar learns both that
    other people's stock exists and roughly how badly it is running down.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}

    from credit.models import DebtRecord
    from inventory.models import Product

    from core.scoping import scoped

    return {
        "badge_low_stock": scoped(Product.objects.all(), user).low_stock().count(),
        "badge_overdue_debts": scoped(DebtRecord.objects.all(), user)
        .overdue()
        .count(),
    }
