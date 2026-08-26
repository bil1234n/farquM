"""
Self-service registration, gated by a shared passcode.

Replaces `manage.py createsuperuser` for day-to-day onboarding: a new hire
registers themselves and proves which role they are entitled to by entering
the passcode for that role.

THREAT MODEL - read this before changing anything here
------------------------------------------------------
A passcode is a SHARED secret, so this design accepts real, specific risks:

  * Anyone who learns PASSCODE_ADMIN can create an administrator account and
    then see every manager's books. It is as sensitive as the shop keys.
  * It cannot be revoked for one person. Rotating it affects everyone.
  * It is only as strong as the string in the .env file.

The mitigations below exist because of that, and removing any of them makes
the feature meaningfully weaker:

  1. Rate limiting. Passcode guessing is the obvious attack, so attempts are
     throttled per IP and the account is not created until the code is right.
  2. Constant-time comparison, so a timing side-channel cannot reveal the
     code character by character.
  3. Blank passcode disables that role entirely. An unconfigured deployment
     must not be open to strangers with an empty string.
  4. Every attempt, successful or not, lands in the audit log with its IP.
  5. The passcode is never echoed back, logged, or included in an error.
"""
import hmac
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from .models import AuditAction, Role, User
from .services import log_action

logger = logging.getLogger(__name__)

# Deliberately strict. Registration is a once-per-employee event, so a low
# ceiling costs a legitimate user nothing and costs an attacker everything.
#
# CAVEAT WORTH KNOWING: the counter lives in Django's cache. With no CACHES
# setting configured the default is LocMemCache, which is PER PROCESS. Under
# `runserver` that is one process and the limit is exact; under gunicorn with
# N workers the effective limit becomes 5*N, because each worker keeps its own
# tally. If this is ever exposed to the open internet, point CACHES at Redis
# or Memcached so the limit is shared.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60


class RegistrationError(Exception):
    """Registration refused. The message is safe to show a user."""


def _passcode_for(role: str) -> str:
    return {
        Role.ADMIN: settings.REGISTRATION_PASSCODE_ADMIN,
        Role.MANAGER: settings.REGISTRATION_PASSCODE_MANAGER,
    }.get(role, "")


def role_available(role: str) -> bool:
    """A role can only be registered for if its passcode is configured."""
    return bool(settings.REGISTRATION_ENABLED and _passcode_for(role))


def available_roles() -> list[tuple[str, str]]:
    """Role choices to offer on the registration form."""
    return [
        (value, label)
        for value, label in Role.choices
        if role_available(value)
    ]


def registration_open() -> bool:
    return bool(available_roles())


def _throttle_key(ip: str) -> str:
    return f"register:attempts:{ip or 'unknown'}"


def attempts_remaining(ip: str) -> int:
    return max(0, MAX_ATTEMPTS - cache.get(_throttle_key(ip), 0))


def _record_failure(ip: str) -> int:
    key = _throttle_key(ip)
    count = cache.get(key, 0) + 1
    # Re-setting the full timeout on every failure means sustained guessing
    # extends its own lockout rather than waiting out a fixed window.
    cache.set(key, count, LOCKOUT_SECONDS)
    return count


def clear_attempts(ip: str) -> None:
    cache.delete(_throttle_key(ip))


def check_passcode(role: str, supplied: str, *, ip: str = "", request=None) -> None:
    """
    Verify the passcode for `role`. Raises RegistrationError if wrong.

    Returns None on success - callers should treat any non-exception as a
    pass, so a future refactor cannot accidentally invert the check by
    forgetting to inspect a boolean.
    """
    if not settings.REGISTRATION_ENABLED:
        raise RegistrationError("Self-registration is currently disabled.")

    if cache.get(_throttle_key(ip), 0) >= MAX_ATTEMPTS:
        raise RegistrationError(
            "Too many incorrect passcodes from this device. "
            "Try again in 15 minutes, or ask an administrator to create the "
            "account for you."
        )

    expected = _passcode_for(role)
    if not expected:
        # Do not reveal WHICH roles are configured beyond what the form shows.
        raise RegistrationError(
            "Registration is not enabled for that role. Contact an administrator."
        )

    # hmac.compare_digest, not ==. A plain comparison returns as soon as two
    # characters differ, and that timing difference is enough to recover the
    # passcode one character at a time over many requests.
    if not hmac.compare_digest(str(supplied or ""), str(expected)):
        count = _record_failure(ip)
        log_action(
            AuditAction.LOGIN_FAILED,
            description=(
                f"Incorrect {role} registration passcode from {ip or 'unknown IP'} "
                f"(attempt {count} of {MAX_ATTEMPTS})."
            ),
            user=None,
            request=request,
        )
        remaining = max(0, MAX_ATTEMPTS - count)
        if remaining:
            raise RegistrationError(
                f"That registration passcode is not correct. "
                f"{remaining} attempt{'s' if remaining != 1 else ''} remaining."
            )
        raise RegistrationError(
            "Too many incorrect passcodes. Registration from this device is "
            "locked for 15 minutes."
        )


@transaction.atomic
def register_user(
    *,
    username: str,
    password: str,
    role: str,
    passcode: str,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    phone: str = "",
    avatar=None,
    ip: str = "",
    request=None,
) -> User:
    """
    Create a staff account after verifying the role passcode.

    The passcode is checked BEFORE the user is written, inside the same
    atomic block, so a failure cannot leave a half-created account behind.
    """
    check_passcode(role, passcode, ip=ip, request=request)

    username = (username or "").strip()
    if not username:
        raise RegistrationError("A username is required.")
    if User.objects.filter(username__iexact=username).exists():
        raise RegistrationError("That username is already taken.")

    user = User(
        username=username,
        role=role,
        first_name=(first_name or "").strip(),
        last_name=(last_name or "").strip(),
        email=(email or "").strip(),
        phone=(phone or "").strip(),
        # A Django superuser flag is NOT granted here. Role ADMIN gives full
        # access to the business; is_superuser additionally unlocks the Django
        # admin site, where the ORM is exposed raw. That stays a deliberate
        # act performed from the server.
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    user.set_password(password)
    if avatar:
        user.avatar = avatar
    user.full_clean(exclude=["password"])
    user.save()

    clear_attempts(ip)
    log_action(
        AuditAction.CREATE,
        instance=user,
        description=(
            f"New {user.get_role_display()} account '{user.username}' created via "
            f"self-registration from {ip or 'unknown IP'}."
        ),
        user=user,
        request=request,
    )
    return user
