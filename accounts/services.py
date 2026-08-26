"""Audit logging helpers. Import log_action() anywhere you mutate data."""
import logging

from core.middleware import get_current_request, get_current_user

from .models import AuditLog

logger = logging.getLogger(__name__)


def _client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_action(
    action,
    instance=None,
    description="",
    changes=None,
    user=None,
    request=None,
):
    """
    Write one immutable audit row. Never raises - logging must not be able
    to break a business transaction.
    """
    try:
        request = request or get_current_request()
        user = user or get_current_user()
        if user is not None and not getattr(user, "is_authenticated", False):
            user = None

        entry = AuditLog(
            user=user,
            action=action,
            description=description,
            changes=changes,
            ip_address=_client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:255] if request else ""),
        )
        if instance is not None:
            entry.model_name = instance.__class__.__name__
            entry.object_id = str(getattr(instance, "pk", "") or "")
            entry.object_repr = str(instance)[:255]
        entry.save()
        return entry
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to write audit log entry")
        return None


def diff_instance(old, new, fields):
    """Build a {field: {from, to}} dict for AuditLog.changes."""
    changes = {}
    for field in fields:
        before = getattr(old, field, None)
        after = getattr(new, field, None)
        if before != after:
            changes[field] = {"from": str(before), "to": str(after)}
    return changes or None
