"""
Firebase Cloud Messaging.

Every function here is defensive: a notification failing must never break the
business transaction that triggered it. A sale that goes through but doesn't
send a push is a minor annoyance; a sale that rolls back because Firebase was
unreachable is a disaster. So everything is wrapped, logged, and swallowed.
"""
import json
import logging
import os

from django.conf import settings
from django.utils import timezone

from .models import DeviceToken, NotificationLog

logger = logging.getLogger(__name__)

_firebase_app = None
_init_attempted = False


def _get_app():
    """Lazily initialise firebase-admin. Returns None if unavailable."""
    global _firebase_app, _init_attempted

    if _firebase_app is not None:
        return _firebase_app
    if _init_attempted:
        return None

    _init_attempted = True

    if not getattr(settings, "FCM_ENABLED", False):
        logger.info("FCM disabled (FCM_ENABLED=False) - notifications will be logged only.")
        return None

    cred_path = getattr(settings, "FIREBASE_CREDENTIALS", "")
    if not cred_path:
        logger.warning("FIREBASE_CREDENTIALS is not set - push disabled.")
        return None

    full_path = cred_path if os.path.isabs(cred_path) else str(settings.BASE_DIR / cred_path)
    if not os.path.exists(full_path):
        logger.warning("Firebase credentials not found at %s - push disabled.", full_path)
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(full_path)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase initialised.")
        return _firebase_app
    except ImportError:
        logger.warning("firebase-admin is not installed. `pip install firebase-admin`")
    except Exception:
        logger.exception("Could not initialise Firebase.")
    return None


def notify_users(users, *, title, body, channel="general", data=None, log=True):
    """
    Send one message to every active device of every user given.

    `users` may be a queryset, a list, or a single user. Returns the number of
    devices that accepted the message.
    """
    from accounts.models import User

    if isinstance(users, User):
        users = [users]
    users = [u for u in users if u and u.is_active]
    if not users:
        return 0

    payload = {str(k): str(v) for k, v in (data or {}).items()}
    payload["channel"] = channel

    tokens_by_user = {}
    for user in users:
        toks = list(
            DeviceToken.objects.filter(user=user, is_active=True)
            .values_list("token", flat=True)
        )
        if toks:
            tokens_by_user[user] = toks

    entries = []
    if log:
        for user in users:
            entries.append(
                NotificationLog(
                    user=user, title=title[:160], body=body,
                    channel=channel, data=payload,
                    devices_targeted=len(tokens_by_user.get(user, [])),
                )
            )
        NotificationLog.objects.bulk_create(entries)

    app = _get_app()
    if app is None:
        logger.info("[push:not-sent] %s | %s", title, body)
        return 0

    delivered = 0
    try:
        from firebase_admin import messaging

        for user, tokens in tokens_by_user.items():
            message = messaging.MulticastMessage(
                tokens=tokens,
                notification=messaging.Notification(title=title, body=body),
                data=payload,
                android=messaging.AndroidConfig(
                    priority="high" if channel == "credit" else "normal",
                    notification=messaging.AndroidNotification(
                        channel_id=channel,
                        sound="default",
                    ),
                ),
            )
            response = messaging.send_each_for_multicast(message)
            delivered += response.success_count
            _handle_failures(tokens, response)

        if log and entries:
            for entry in entries:
                entry.was_sent = True
                entry.devices_delivered = delivered
            NotificationLog.objects.bulk_update(entries, ["was_sent", "devices_delivered"])

    except Exception as exc:
        logger.exception("Push send failed")
        if log and entries:
            for entry in entries:
                entry.error = str(exc)[:500]
            NotificationLog.objects.bulk_update(entries, ["error"])

    return delivered


def _handle_failures(tokens, response):
    """Deactivate tokens Firebase says are dead, so we stop retrying them."""
    for idx, result in enumerate(response.responses):
        if result.success:
            continue
        code = getattr(getattr(result, "exception", None), "code", "") or ""
        if "not-registered" in str(code).lower() or "invalid-argument" in str(code).lower():
            DeviceToken.objects.filter(token=tokens[idx]).update(
                is_active=False, deactivated_reason=str(code)[:120]
            )
            logger.info("Deactivated dead token %s (%s)", tokens[idx][:16], code)


# ---------------------------------------------------------------------------
# Audience helpers
# ---------------------------------------------------------------------------
def admins():
    from accounts.models import User

    return list(User.objects.admins().filter(is_active=True))


def all_staff():
    from accounts.models import User

    return list(User.objects.active_staff())


def notify_admins(**kwargs):
    return notify_users(admins(), **kwargs)


def notify_staff(**kwargs):
    return notify_users(all_staff(), **kwargs)


def notify_owner_and_admins(owner, **kwargs):
    """
    The audience for almost every business alert.

    The manager whose record it is needs to act on it; the administrators
    need oversight. Nobody else should even learn the record exists, so this
    is the correct default rather than notify_staff().

    Duplicates are removed, so an admin who owns the record is messaged once,
    not twice.
    """
    recipients = list(admins())
    if owner is not None and owner.pk not in {u.pk for u in recipients}:
        recipients.append(owner)
    return notify_users(recipients, **kwargs)


def send_test_push(user):
    """Called from the shell to prove the wiring works end to end."""
    count = notify_users(
        user,
        title="Faruq Management",
        body=f"Test notification sent {timezone.localtime():%H:%M}. Push is working.",
        channel="general",
        data={"screen": "Dashboard"},
    )
    print(f"Delivered to {count} device(s).")
    return count
