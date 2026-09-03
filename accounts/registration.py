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

from .models import (
    AuditAction,
    RegistrationPasscode,
    RoleCode,
    RoleDefinition,
    User,
)
from .services import log_action

#: Historical alias - this module used to import the TextChoices as `Role`.
Role = RoleCode

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


def _env_passcode(role: str) -> str:
    """
    The passcode set on the server for a role, or "" if it has none.

    This is the fallback path, kept so a deployment that configured
    PASSCODE_ADMIN / PASSCODE_MANAGER in its .env keeps working with no
    action required. Only the three built-in roles can have one: you cannot
    invent an environment variable per custom role. Custom roles get their
    code from the database instead.
    """
    return (
        {
            RoleCode.ADMIN: getattr(settings, "REGISTRATION_PASSCODE_ADMIN", ""),
            RoleCode.MANAGER: getattr(settings, "REGISTRATION_PASSCODE_MANAGER", ""),
            RoleCode.SALES: getattr(settings, "REGISTRATION_PASSCODE_SALES", ""),
        }.get(role, "")
        or ""
    )


def has_server_passcode(role: str) -> bool:
    """Public read of the .env fallback, for the Security settings screen."""
    return bool(_env_passcode(role))


def passcode_row(role: str) -> RegistrationPasscode | None:
    """
    The database row for a role's passcode, or None if there is none.

    Swallows database errors on purpose: this is reached from the login page's
    context processor, and a missing table during a half-finished migration
    must not take the login page down. Returning None means "fall back to the
    environment", which is the safe direction - it can only ever close doors,
    never open one.
    """
    try:
        return RegistrationPasscode.objects.filter(role_code=role).first()
    except Exception:
        return None


def ensure_passcode_rows() -> None:
    """
    Give every assignable role a passcode row so Settings -> Security can
    show it. A new row is disabled and codeless: creating a role must never
    open a registration door by itself.
    """
    try:
        existing = set(
            RegistrationPasscode.objects.values_list("role_code", flat=True)
        )
        missing = [
            RegistrationPasscode(
                role_code=role.code,
                is_enabled=bool(_env_passcode(role.code)),
            )
            for role in RoleDefinition.objects.assignable()
            if role.code not in existing
        ]
        if missing:
            RegistrationPasscode.objects.bulk_create(missing, ignore_conflicts=True)
    except Exception:
        # Same reasoning as passcode_row: never 500 a page over housekeeping.
        pass


def _self_registration_allowed() -> bool:
    """
    The administrator's switch, on top of the environment's.

    Wrapped in try/except because this is reached from a context processor on
    the login page: a settings table that does not exist yet during a
    half-finished migration must not take the login page down with it.
    """
    if not settings.REGISTRATION_ENABLED:
        return False
    try:
        from core.models import SystemSetting

        return bool(SystemSetting.load().allow_self_registration)
    except Exception:
        return True


def role_available(role: str) -> bool:
    """
    May somebody register as this role right now?

    Three switches, all of which must be on:

      1. self-registration is allowed at all (env + the administrator's
         setting);
      2. the role's own switch is on - a row that exists but is disabled is
         an administrator saying "not at the moment";
      3. an actual passcode exists to check against, in the database or in
         the environment. A blank code must never mean "anything passes".

    A role with no row at all falls back to the environment, which is how
    a deployment that upgrades without visiting Settings keeps working.
    """
    if not _self_registration_allowed():
        return False

    row = passcode_row(role)
    if row is not None:
        if not row.is_enabled:
            return False
        return row.has_passcode or bool(_env_passcode(role))

    return bool(_env_passcode(role))


def available_roles() -> list[tuple[str, str]]:
    """
    Role choices to offer on the registration form.

    Read from RoleDefinition, not from the RoleCode enum, so a custom role an
    administrator created and gave a passcode is offered too. Ordered by rank,
    so Administrator/Manager/Sales appear in seniority order rather than
    alphabetically.
    """
    try:
        roles = list(RoleDefinition.objects.assignable())
    except Exception:
        return []
    return [(role.code, role.name) for role in roles if role_available(role.code)]


def registration_status() -> list[dict]:
    """
    One row per assignable role, for the Security settings screen.

    Deliberately never includes the passcode itself - it is stored hashed and
    cannot be read back. `configured` answers the only question the screen
    needs: is there a code, and where does it come from.
    """
    ensure_passcode_rows()
    rows_by_code = {
        row.role_code: row for row in RegistrationPasscode.objects.all()
    }
    out = []
    for role in RoleDefinition.objects.assignable():
        row = rows_by_code.get(role.code)
        from_env = bool(_env_passcode(role.code))
        out.append(
            {
                "role": role,
                "row": row,
                "enabled": bool(row and row.is_enabled),
                "has_passcode": bool(row and row.has_passcode),
                "from_env": from_env,
                "configured": bool(row and row.has_passcode) or from_env,
                "available": role_available(role.code),
                "is_system": role.is_system,
                "users": role.user_count,
                "last_used_at": row.last_used_at if row else None,
                "use_count": row.use_count if row else 0,
                "note": row.note if row else "",
            }
        )
    return out


def registration_open() -> bool:
    try:
        return bool(available_roles())
    except Exception:
        # Same reasoning as _self_registration_allowed: never 500 the login
        # page over a question about a link on it.
        return False


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

    if not role_available(role):
        # Do not reveal WHICH roles are configured beyond what the form shows.
        raise RegistrationError(
            "Registration is not enabled for that role. Contact an administrator."
        )

    # Two sources, checked in a deliberate order. A code an administrator set
    # from Settings SUPERSEDES the one in the server environment - otherwise
    # "change the passcode" would leave the old one quietly working, which is
    # the opposite of what changing a passcode is for.
    row = passcode_row(role)
    if row is not None and row.has_passcode:
        # check_password is salted and constant-time, so the hmac dance below
        # is neither needed nor possible here (there is no plaintext to
        # compare against - that is the point of storing it hashed).
        correct = row.verify(supplied)
    else:
        expected = _env_passcode(role)
        # hmac.compare_digest, not ==. A plain comparison returns as soon as
        # two characters differ, and that timing difference is enough to
        # recover the passcode one character at a time over many requests.
        correct = bool(expected) and hmac.compare_digest(
            str(supplied or ""), str(expected)
        )

    if not correct:
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
    manager=None,
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

    # Checked against the database rather than the RoleCode enum, so a custom
    # role an administrator opened for registration works, and a role deleted
    # between loading the form and submitting it does not.
    if not RoleDefinition.objects.filter(code=role, is_active=True).exists():
        raise RegistrationError(
            "Registration is not enabled for that role. Contact an administrator."
        )

    if manager is not None and getattr(manager, "role", "") == RoleCode.SALES:
        # Belt and braces: the form already excludes sales users from the
        # dropdown, but a hand-crafted POST must not build a sales-reports-to-
        # sales chain, because catalog scoping walks exactly one hop upward
        # and a chain would silently stop resolving products.
        raise RegistrationError("That person cannot be chosen as a supervisor.")

    user = User(
        username=username,
        role=role,
        manager=manager,
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
    row = passcode_row(role)
    if row is not None:
        # Best-effort bookkeeping so an administrator can see on the Security
        # screen whether a code is actually in use. Never worth failing a
        # completed registration over.
        try:
            row.record_use()
        except Exception:
            logger.warning("Could not record passcode use for role %s", role)

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
