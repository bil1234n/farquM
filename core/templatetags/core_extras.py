"""Template filters used across the UI."""
from decimal import Decimal, InvalidOperation

from django import template
from django.conf import settings
from django.core.cache import cache
from django.utils.safestring import mark_safe

register = template.Library()

CURRENCY_CACHE_KEY = "faruq.currency_symbol"
CURRENCY_CACHE_SECONDS = 300


def currency_symbol() -> str:
    """
    The symbol to print in front of money.

    Cached for five minutes because `money` is called dozens of times per
    page: a settings lookup per amount would turn one report into a hundred
    identical queries. `SystemSettingForm` clears this key on save, so an
    administrator sees their change immediately rather than in five minutes.
    """
    symbol = cache.get(CURRENCY_CACHE_KEY)
    if symbol:
        return symbol
    try:
        from core.models import SystemSetting

        symbol = SystemSetting.load().currency
    except Exception:
        symbol = getattr(settings, "CURRENCY_SYMBOL", "ETB")
    cache.set(CURRENCY_CACHE_KEY, symbol, CURRENCY_CACHE_SECONDS)
    return symbol


@register.filter
def money(value, with_symbol=True):
    """Format a number as currency: 12345.5 -> 'ETB 12,345.50'."""
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return value
    formatted = f"{amount:,.2f}"
    if with_symbol:
        return f"{currency_symbol()} {formatted}"
    return formatted


@register.filter
def plain_money(value):
    return money(value, with_symbol=False)


@register.filter
def subtract(value, arg):
    try:
        return Decimal(str(value)) - Decimal(str(arg))
    except (InvalidOperation, TypeError, ValueError):
        return 0


@register.filter
def percent_of(part, whole):
    try:
        part, whole = Decimal(str(part)), Decimal(str(whole))
        if whole == 0:
            return 0
        return round(part / whole * 100, 1)
    except (InvalidOperation, TypeError, ValueError):
        return 0


@register.filter
def status_badge(obj):
    """Render a coloured pill from an object exposing status_class/display."""
    css = getattr(obj, "status_class", "secondary")
    label = getattr(obj, "display_status", None) or getattr(obj, "status", "")
    return mark_safe(f'<span class="badge text-bg-{css}">{label}</span>')


@register.simple_tag(takes_context=True)
def query_replace(context, **kwargs):
    """Preserve existing GET params while changing one (used by pagination)."""
    query = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = value
    return query.urlencode()


@register.filter
def field_type(field):
    return field.field.widget.__class__.__name__


@register.filter
def initials(user):
    name = getattr(user, "display_name", "") or getattr(user, "username", "?")
    parts = [p for p in name.split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
@register.filter(name="can")
def can(user, codes):
    """
    Permission check inside a template.

        {% if user|can:"sale.void" %} ... {% endif %}
        {% if user|can:"credit.write_off,credit.reverse_payment" %}  (ANY of)

    Comma-separated codes mean "any of these", because that is what a template
    almost always wants: show the section if there is anything in it to show.
    Requiring all of several permissions to render one block is rare enough to
    be worth spelling out with nested ifs.

    Falls back to False for AnonymousUser rather than raising - a template that
    500s when someone is logged out is a template that breaks the login page.
    """
    wanted = [c.strip() for c in str(codes).split(",") if c.strip()]
    if not wanted:
        return False
    checker = getattr(user, "has_access", None)
    if checker is None:
        return False
    return checker(*wanted, require_all=False)


@register.filter(name="can_all")
def can_all(user, codes):
    """Same as `can`, but every listed code is required."""
    wanted = [c.strip() for c in str(codes).split(",") if c.strip()]
    if not wanted:
        return False
    checker = getattr(user, "has_access", None)
    if checker is None:
        return False
    return checker(*wanted, require_all=True)


@register.filter
def perm_label(code):
    """Human label for a permission code."""
    from core.permissions import label_for

    return label_for(code)
