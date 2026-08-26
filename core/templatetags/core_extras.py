"""Template filters used across the UI."""
from decimal import Decimal, InvalidOperation

from django import template
from django.conf import settings
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def money(value, with_symbol=True):
    """Format a number as currency: 12345.5 -> 'ETB 12,345.50'."""
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return value
    formatted = f"{amount:,.2f}"
    if with_symbol:
        return f"{settings.CURRENCY_SYMBOL} {formatted}"
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
