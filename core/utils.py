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


def validate_person_photo(f):
    """
    A photograph of a person or of their identity card.

    Images only - a PDF is allowed for a receipt because a bill often arrives
    as one, but nothing photographs an ID card into a PDF, and a file that
    cannot be shown beside the person's name is no use on a customer card.

    The limit is the receipt limit rather than the 3 MB used for a profile
    picture: an ID card is photographed to be READ later - a licence number,
    an expiry date - and squeezing it harder than a receipt would be the one
    picture in the system too blurred to do its job.
    """
    max_bytes = settings.MAX_RECEIPT_SIZE_MB * 1024 * 1024
    if f.size > max_bytes:
        raise ValidationError(
            f"Image too large ({f.size / 1048576:.1f} MB). "
            f"Maximum is {settings.MAX_RECEIPT_SIZE_MB} MB."
        )
    ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    allowed = ["jpg", "jpeg", "png", "webp"]
    if ext not in allowed:
        raise ValidationError(
            f"Unsupported image type '.{ext}'. Allowed: " + ", ".join(allowed)
        )


def _person_photo_path(instance, filename, kind):
    """
    media/people/<customer|employee>/<id>/<kind>/<timestamp>_<filename>

    The timestamp is not decoration: without it a replacement photo keeps the
    old path, and Cloudinary - and every CDN in front of it - goes on serving
    the picture that was just replaced. See avatar_upload_path.

    The two kinds are kept in separate folders so that a bucket listing, a
    backup, or a future "delete the ID pictures of people who have left"
    never has to guess which file is which from its name.
    """
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    safe = filename.replace(" ", "_")
    who = instance._meta.model_name
    pk = getattr(instance, "pk", None) or "new"
    return f"people/{who}/{pk}/{kind}/{stamp}_{safe}"


def person_photo_upload_path(instance, filename):
    """Where a person's own photograph is stored."""
    return _person_photo_path(instance, filename, "photo")


def id_photo_upload_path(instance, filename):
    """Where the photograph of a person's identity card is stored."""
    return _person_photo_path(instance, filename, "id")


def percentage(part, whole) -> Decimal:
    part, whole = money(part), money(whole)
    if whole == ZERO:
        return ZERO
    return money((part / whole) * 100)
