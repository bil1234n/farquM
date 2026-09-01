"""Shared helpers: reference generators, money math, validators."""
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value) -> Decimal:
    """Normalise any numeric input to a 2-decimal Decimal (banker-safe)."""
    if value in (None, ""):
        return ZERO
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def generate_reference(prefix: str, model, field: str = "reference") -> str:
    """
    Human-readable sequential reference, e.g. TXN-20260821-0007.

    Safe under normal load. For very high concurrency swap this for a
    PostgreSQL sequence (see docs/SCHEMA.md - "Reference generation").
    """
    today = timezone.localdate()
    stamp = today.strftime("%Y%m%d")
    base = f"{prefix}-{stamp}-"
    last = (
        model.objects.filter(**{f"{field}__startswith": base})
        .order_by(f"-{field}")
        .values_list(field, flat=True)
        .first()
    )
    seq = int(last.split("-")[-1]) + 1 if last else 1
    return f"{base}{seq:04d}"


def default_due_date(days: int | None = None) -> dt.date:
    """
    When a credit sale falls due, by default.

    Reads the administrator-editable setting first and falls back to the
    environment value. Wrapped in try/except so this keeps working during a
    migration, before the settings table exists.
    """
    if days is None:
        try:
            from core.models import SystemSetting

            days = SystemSetting.load().default_credit_due_days
        except Exception:
            days = None
    if days is None:
        days = settings.DEFAULT_CREDIT_DUE_DAYS
    return timezone.localdate() + dt.timedelta(days=int(days))


def validate_receipt_file(f):
    """Size + extension guard for uploaded receipt proof."""
    max_bytes = settings.MAX_RECEIPT_SIZE_MB * 1024 * 1024
    if f.size > max_bytes:
        raise ValidationError(
            f"File too large ({f.size / 1048576:.1f} MB). "
            f"Maximum is {settings.MAX_RECEIPT_SIZE_MB} MB."
        )
    ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    if ext not in settings.ALLOWED_RECEIPT_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file type '.{ext}'. Allowed: "
            + ", ".join(settings.ALLOWED_RECEIPT_EXTENSIONS)
        )


def receipt_upload_path(instance, filename):
    """media/receipts/2026/08/TXN-20260821-0007_receipt.jpg"""
    today = timezone.localdate()
    ref = getattr(instance, "reference_hint", None) or "misc"
    safe = filename.replace(" ", "_")
    return f"receipts/{today.year}/{today.month:02d}/{ref}_{safe}"


def avatar_upload_path(instance, filename):
    """
    media/avatars/<user-id>/<timestamp>_<filename>

    The timestamp matters: without it a re-upload keeps the same path, and
    both Cloudinary and every CDN in front of it would keep serving the OLD
    photo until their cache expired. Users read that as "the upload failed"
    and try again, repeatedly.
    """
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    safe = filename.replace(" ", "_")
    uid = getattr(instance, "pk", None) or "new"
    return f"avatars/{uid}/{stamp}_{safe}"


def validate_avatar_file(f):
    """Profile photos: images only, and smaller than a receipt."""
    max_bytes = 3 * 1024 * 1024
    if f.size > max_bytes:
        raise ValidationError(
            f"Image too large ({f.size / 1048576:.1f} MB). Maximum is 3 MB."
        )
    ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    allowed = ["jpg", "jpeg", "png", "webp"]
    if ext not in allowed:
        raise ValidationError(
            f"Unsupported image type '.{ext}'. Allowed: " + ", ".join(allowed)
        )


def percentage(part, whole) -> Decimal:
    part, whole = money(part), money(whole)
    if whole == ZERO:
        return ZERO
    return money((part / whole) * 100)
