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


# ---------------------------------------------------------------------------
# Coloured note marks
# ---------------------------------------------------------------------------
@register.simple_tag
def note_mark(tag, compact=False):
    """
    A note's mark as a coloured pill: {% note_mark txn.note_tag %}

    `compact` draws the dot alone - for a table row where the pill would crowd
    the reference out - with the meaning kept in the tooltip.
    """
    if not tag:
        return ""
    from django.utils.html import format_html

    from core.forms import mark_color

    color = mark_color(tag)
    if compact:
        return format_html(
            '<span class="note-dot-only" style="--mark:{}" title="{}" '
            'aria-label="{}"></span>',
            color, tag.label, tag.label,
        )
    return format_html(
        '<span class="note-mark" style="--mark:{}"><span class="note-dot"></span>{}</span>',
        color, tag.label,
    )


@register.inclusion_tag("partials/note_block.html")
def note_block(text, tag=None, title="Notes"):
    """
    A note, boxed in its mark's colour, with the mark's meaning on top.

        {% note_block txn.notes txn.note_tag %}

    Renders nothing for a record with neither a note nor a mark.
    """
    from core.forms import mark_color

    return {
        "text": text or "",
        "tag": tag,
        "color": mark_color(tag) if tag else "",
        "title": title,
    }


@register.simple_tag
def note_tag_select(name="note_tag", value=None, note_field="notes"):
    """
    The coloured mark picker for a hand-written form:

        {% note_tag_select "note_tag" %}

    Hand-written forms (the hand-over box on a sale, say) post plain fields
    rather than going through a Django form; this gives them the same picker
    the model forms get from core.forms.NoteTagField.
    """
    from core.forms import NoteTagField

    current = getattr(value, "pk", value) or None
    field = NoteTagField(current=current, note_field=note_field)
    return field.widget.render(name, current, attrs={"id": f"id_{name}"})
