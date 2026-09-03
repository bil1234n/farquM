"""
The Settings hub: system configuration, roles, and per-person access.

This is what replaced the link to Django's raw admin site in the sidebar.
The Django admin is a database editor - it will happily let someone set a
stock quantity without writing a movement, or delete a user and orphan their
sales. Everything here goes through the same services and audit logging as
the rest of the app.

WHO CAN REACH WHAT
------------------
    settings.view      the hub and the business page (read)
    settings.edit      saving the business page
    user.permissions   Access Control - editing what a person may do
    role.manage        creating and editing roles

`user.permissions` is the keys to the building: whoever holds it can grant
themselves anything else. The self-lockout guards below exist because the one
mistake that cannot be undone from inside the app is an administrator removing
their own access to this screen.
"""
from django.contrib import messages
from django.core.cache import cache
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import (
    AuditAction,
    RegistrationPasscode,
    RoleCode,
    RoleDefinition,
    User,
)
from accounts.registration import (
    MAX_ATTEMPTS,
    available_roles,
    ensure_passcode_rows,
    has_server_passcode,
    registration_status,
)
from accounts.roles import BLUEPRINTS, reset_to_blueprint
from accounts.services import log_action

from .access import (
    access_summary,
    apply_role_permissions,
    apply_user_access,
    build_matrix,
    reset_user_to_role,
    scope_choices_with_help,
    sensitive_grants,
)
from .mixins import require
from .models import SystemSetting
from .permissions import ALL_CODES, CATALOG, SENSITIVE_CODES
from .settings_forms import RoleForm, SystemSettingForm, UserAccessForm
from .templatetags.core_extras import CURRENCY_CACHE_KEY

#: Permissions an administrator may not strip from themselves.
#:
#: Without this, one careless save locks the last administrator out of the only
#: screen that could undo it, and the fix becomes a Django shell on the server.
#: Rendered as ticked-and-disabled with an explanation, not silently forced.
SELF_LOCKED = frozenset({"user.permissions", "user.view", "settings.view"})

#: Minimum length for a registration passcode.
#:
#: Short by password standards on purpose - this is a code read aloud to a new
#: hire, not something typed daily - but long enough that the 5-attempts-per-
#: 15-minutes throttle makes guessing hopeless.
MIN_PASSCODE_LENGTH = 6

#: Codes that are the same as no code at all. Not a security control, a
#: courtesy: it stops the obvious accident rather than a determined mistake.
OBVIOUS_PASSCODES = frozenset(
    {
        "123456",
        "1234567",
        "12345678",
        "password",
        "passcode",
        "admin123",
        "000000",
        "111111",
        "abcdef",
        "qwerty",
    }
)


# ---------------------------------------------------------------------------
# Error pages
# ---------------------------------------------------------------------------
def forbidden(request):
    return render(request, "403.html", status=403)


def handler403(request, exception=None):
    return render(request, "403.html", status=403)


def handler404(request, exception=None):
    return render(request, "404.html", status=404)


def handler500(request):
    return render(request, "500.html", status=500)


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
def settings_hub(request):
    blocked = require(
        request, "settings.view",
        message="You do not have permission to open system settings.",
    )
    if blocked:
        return blocked

    roles = list(RoleDefinition.objects.assignable())
    counts = {
        row["role"]: row["n"]
        for row in User.objects.filter(is_active=True)
        .values("role")
        .annotate(n=Count("id"))
    }
    for role in roles:
        role.active_users = counts.get(role.code, 0)

    customised = User.objects.filter(is_active=True).exclude(
        Q(extra_permissions=[]) & Q(denied_permissions=[]) & Q(data_scope_override="")
    )

    return render(
        request,
        "system/hub.html",
        {
            "conf": SystemSetting.load(),
            "roles": roles,
            "role_count": len(roles),
            "user_count": User.objects.filter(is_active=True).count(),
            "inactive_count": User.objects.filter(is_active=False).count(),
            "permission_count": len(ALL_CODES),
            "sensitive_count": len(SENSITIVE_CODES),
            "customised_count": customised.count(),
            "customised": customised.select_related("manager")[:6],
            "group_count": len(CATALOG),
            # Names of the roles somebody could register as right now. Shown
            # on the hub because "self-registration: on" alone never answered
            # the question people actually have, which is "on for whom?".
            "open_roles": [
                name for _code, name in available_roles()
            ],
            "active_tab": "hub",
        },
    )


def business_settings(request):
    blocked = require(request, "settings.view")
    if blocked:
        return blocked

    conf = SystemSetting.load()
    can_edit = request.user.has_access("settings.edit")
    form = SystemSettingForm(instance=conf)

    if request.method == "POST":
        if not can_edit:
            messages.error(request, "You may view these settings but not change them.")
            return redirect("core:business_settings")
        form = SystemSettingForm(request.POST, instance=conf)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            # The `money` filter caches the symbol; without this an
            # administrator changes the currency and nothing appears to happen.
            cache.delete(CURRENCY_CACHE_KEY)
            log_action(
                AuditAction.UPDATE,
                instance=obj,
                description="Updated business settings.",
                changes={
                    field: {"from": "", "to": str(form.cleaned_data.get(field))}
                    for field in form.changed_data
                }
                or None,
            )
            messages.success(request, "Settings saved.")
            return redirect("core:business_settings")

    return render(
        request,
        "system/business.html",
        {"form": form, "conf": conf, "can_edit": can_edit, "active_tab": "business"},
    )


# ---------------------------------------------------------------------------
# Security: who may sign themselves up, and with which code
# ---------------------------------------------------------------------------
def _apply_passcode_changes(request, conf) -> dict:
    """
    Save the Security screen. Returns {role_code: error message}.

    Written by hand rather than as a Django formset because the number of rows
    is the number of roles, which an administrator can change - and because
    each row has three controls (a code, a switch, a clear button) whose
    interactions are the interesting part:

      * clearing a code also switches the role off, so an administrator cannot
        leave a door marked open with no lock on it;
      * switching a role on without a code is refused rather than silently
        ignored, because "I ticked it and nothing happened" is the bug report
        this whole screen exists to prevent;
      * an empty code box means "leave it alone", not "erase it" - the code
        cannot be read back to pre-fill the box, so a blank box is the normal
        state of a role that already has one.
    """
    errors: dict[str, str] = {}
    changes: list[str] = []

    allow = "allow_self_registration" in request.POST
    if conf.allow_self_registration != allow:
        conf.allow_self_registration = allow
        conf.updated_by = request.user
        conf.save()
        changes.append(
            "self-registration turned " + ("on" if allow else "off")
        )

    ensure_passcode_rows()

    for role in RoleDefinition.objects.assignable():
        code = role.code
        row, _ = RegistrationPasscode.objects.get_or_create(role_code=code)
        raw = (request.POST.get(f"passcode_{code}") or "").strip()
        clearing = f"clear_{code}" in request.POST
        wanted_on = f"enabled_{code}" in request.POST

        if clearing:
            row.set_passcode("")  # also switches the role off
            changes.append(f"cleared the {role.name} passcode")
            wanted_on = False
        elif raw:
            if len(raw) < MIN_PASSCODE_LENGTH:
                errors[code] = (
                    f"Use at least {MIN_PASSCODE_LENGTH} characters."
                )
                continue
            if raw.lower() in OBVIOUS_PASSCODES:
                errors[code] = (
                    "That code is one of the first things anyone would try. "
                    "Pick something else."
                )
                continue
            row.set_passcode(raw)
            changes.append(f"set a new {role.name} passcode")

        if wanted_on and not (row.has_passcode or has_server_passcode(code)):
            errors[code] = (
                "Set a passcode first - a role cannot be opened for "
                "registration without one."
            )
            wanted_on = False

        if row.is_enabled != wanted_on:
            row.is_enabled = wanted_on
            changes.append(
                f"{role.name} registration turned " + ("on" if wanted_on else "off")
            )

        note = (request.POST.get(f"note_{code}") or "").strip()[:120]
        if note != (row.note or ""):
            row.note = note

        row.updated_by = request.user
        row.save()

    if changes:
        # The description names what changed but NEVER the code itself. An
        # audit log is read by more people than the settings screen is.
        log_action(
            AuditAction.UPDATE,
            instance=conf,
            description="Registration security: " + "; ".join(changes) + ".",
        )
        messages.success(request, "Registration settings saved.")
    elif not errors:
        messages.info(request, "Nothing changed.")

    return errors


def security_settings(request):
    blocked = require(
        request, "settings.view",
        message="You do not have permission to open system settings.",
    )
    if blocked:
        return blocked

    conf = SystemSetting.load()
    can_edit = request.user.has_access("settings.edit")
    errors: dict[str, str] = {}

    if request.method == "POST":
        if not can_edit:
            messages.error(request, "You may view these settings but not change them.")
            return redirect("core:security_settings")
        errors = _apply_passcode_changes(request, conf)
        if not errors:
            return redirect("core:security_settings")

    rows = registration_status()
    for row in rows:
        # Attached to the row rather than passed as a separate dict, because
        # Django templates cannot subscript a dict by a variable key.
        row["error"] = errors.get(row["role"].code, "")

    return render(
        request,
        "system/security.html",
        {
            "conf": conf,
            "rows": rows,
            "can_edit": can_edit,
            "open_count": sum(1 for r in rows if r["available"]),
            "min_length": MIN_PASSCODE_LENGTH,
            "max_attempts": MAX_ATTEMPTS,
            "active_tab": "security",
        },
    )


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def access_list(request):
    blocked = require(
        request, "user.permissions",
        message="You do not have permission to manage access.",
    )
    if blocked:
        return blocked

    users = User.objects.select_related("manager").order_by(
        "-is_active", "role", "username"
    )
    q = request.GET.get("q", "").strip()
    role = request.GET.get("role", "").strip()
    state = request.GET.get("status", "").strip()

    if q:
        users = users.filter(
            Q(username__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
        )
    if role:
        users = users.filter(role=role)
    if state == "active":
        users = users.filter(is_active=True)
    elif state == "inactive":
        users = users.filter(is_active=False)
    elif state == "custom":
        users = users.exclude(
            Q(extra_permissions=[]) & Q(denied_permissions=[]) & Q(data_scope_override="")
        )

    rows = [{"user": u, "access": access_summary(u)} for u in users]

    return render(
        request,
        "system/access_list.html",
        {
            "rows": rows,
            "roles": RoleDefinition.objects.assignable(),
            "q": q,
            "selected_role": role,
            "selected_status": state,
            "total_permissions": len(ALL_CODES),
            "active_tab": "access",
        },
    )


def _guard_target(request, target):
    """
    Refuse edits that would break the system or exceed the editor's standing.

    Two rules, both learned the hard way in systems like this one:

      * Only a Django superuser may edit another superuser. Otherwise an
        administrator can quietly demote the account that exists precisely to
        recover from an administrator's mistake.
      * The last active administrator cannot be edited into something else.
        The role form catches the role change; this catches the rest of the
        page reaching the same end.
    """
    if target.is_superuser and not request.user.is_superuser:
        return (
            "That account is a system superuser and can only be changed from "
            "the server."
        )
    return None


def user_access(request, pk):
    blocked = require(
        request, "user.permissions",
        message="You do not have permission to manage access.",
    )
    if blocked:
        return blocked

    target = get_object_or_404(User.objects.select_related("manager"), pk=pk)
    problem = _guard_target(request, target)
    if problem:
        messages.error(request, problem)
        return redirect("core:access_list")

    editing_self = target.pk == request.user.pk
    locked = SELF_LOCKED if editing_self else set()
    form = UserAccessForm(instance=target, editor=request.user)

    if request.method == "POST":
        form = UserAccessForm(request.POST, instance=target, editor=request.user)
        ticked = set(request.POST.getlist("perm"))
        if editing_self:
            # Ticked back on rather than rejected: the administrator's intent
            # was almost certainly to change something else on the page, and
            # failing the whole save over it would be obstructive.
            ticked |= locked

        if form.is_valid():
            role_code = form.cleaned_data["role"]
            if (
                target.role == RoleCode.ADMIN
                and role_code != RoleCode.ADMIN
                and not User.objects.admins()
                .filter(is_active=True)
                .exclude(pk=target.pk)
                .exists()
            ):
                messages.error(
                    request,
                    "This is the only active administrator. Promote someone "
                    "else before changing this account.",
                )
                return redirect("core:user_access", pk=target.pk)

            result = apply_user_access(
                user=target,
                role_code=role_code,
                ticked=ticked,
                manager=form.cleaned_data.get("manager"),
                data_scope_override=form.cleaned_data.get("data_scope_override") or "",
                editor=request.user,
            )
            if result["changed"]:
                messages.success(
                    request,
                    f"Access updated for {target.display_name}: "
                    f"{len(result['gained'])} granted, {len(result['lost'])} revoked.",
                )
                if editing_self:
                    messages.info(
                        request,
                        "You changed your own access. Some menu items may "
                        "appear or disappear on the next page.",
                    )
            else:
                messages.info(request, "Nothing changed.")
            return redirect("core:user_access", pk=target.pk)

    role = (
        RoleDefinition.objects.filter(code=form.data.get("role")).first()
        if request.method == "POST"
        else target.role_definition
    ) or target.role_definition

    matrix = build_matrix(
        role=role,
        extra=target.extra_permissions,
        denied=target.denied_permissions,
        locked=locked,
    )

    return render(
        request,
        "system/access_form.html",
        {
            "target_user": target,
            "form": form,
            "matrix": matrix,
            "access": access_summary(target),
            "sensitive": sensitive_grants(target),
            "scopes": scope_choices_with_help(),
            "editing_self": editing_self,
            "locked": sorted(locked),
            "roles": RoleDefinition.objects.assignable(),
            "total_permissions": len(ALL_CODES),
            "active_tab": "access",
        },
    )


@require_POST
def user_access_reset(request, pk):
    blocked = require(request, "user.permissions")
    if blocked:
        return blocked

    target = get_object_or_404(User, pk=pk)
    problem = _guard_target(request, target)
    if problem:
        messages.error(request, problem)
        return redirect("core:access_list")

    if target.pk == request.user.pk:
        messages.error(
            request,
            "Reset someone else's access, not your own - a reset can remove "
            "the very permission you are using right now.",
        )
        return redirect("core:user_access", pk=target.pk)

    reset_user_to_role(target, editor=request.user)
    messages.success(
        request,
        f"{target.display_name} is back to a plain {target.get_role_display()}.",
    )
    return redirect("core:user_access", pk=target.pk)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def role_list(request):
    blocked = require(
        request, "role.manage",
        message="You do not have permission to manage roles.",
    )
    if blocked:
        return blocked

    roles = list(RoleDefinition.objects.all())
    for role in roles:
        role.active_users = User.objects.filter(role=role.code, is_active=True).count()

    return render(
        request,
        "system/role_list.html",
        {
            "roles": roles,
            "total_permissions": len(ALL_CODES),
            "active_tab": "roles",
        },
    )


def role_form(request, pk=None):
    blocked = require(request, "role.manage")
    if blocked:
        return blocked

    role = get_object_or_404(RoleDefinition, pk=pk) if pk else None
    form = RoleForm(instance=role)

    if request.method == "POST":
        form = RoleForm(request.POST, instance=role)
        ticked = request.POST.getlist("perm")
        if form.is_valid():
            obj = form.save(commit=False)
            if role is None:
                obj.is_system = False
            elif role.is_system:
                # Nothing on the form can turn a built-in role into a
                # deletable one; keeping this here means a hand-crafted POST
                # cannot either.
                obj.is_system = True
                obj.is_active = True
            # Saved with its existing permissions, then handed to the service
            # which swaps them in and logs the difference. Setting them here
            # too would make that log read "nothing changed".
            obj.save()
            result = apply_role_permissions(
                role=obj, ticked=ticked, editor=request.user
            )
            if role is None:
                log_action(
                    AuditAction.CREATE,
                    instance=obj,
                    description=f"Created role '{obj.name}' ({obj.code}).",
                )
                messages.success(
                    request,
                    f"Role '{obj.name}' created with {len(obj.permission_set)} "
                    f"permission(s). Assign it from Users & Roles.",
                )
            else:
                messages.success(
                    request,
                    f"Role '{obj.name}' saved. {result['affected']} user(s) "
                    f"are affected.",
                )
            return redirect("core:role_update", pk=obj.pk)

    matrix = build_matrix(role=role if role else None)
    if request.method == "POST":
        # Re-render the grid with what was submitted, not with what is stored,
        # so a validation error does not silently discard every tick.
        ticked = set(request.POST.getlist("perm"))
        for group in matrix:
            for row in group["permissions"]:
                row["checked"] = row["code"] in ticked
                row["state"] = "granted" if row["checked"] else "absent"

    return render(
        request,
        "system/role_form.html",
        {
            "form": form,
            "role": role,
            "matrix": matrix,
            "is_new": role is None,
            "can_reset": bool(role and role.code in BLUEPRINTS),
            "user_count": role.user_count if role else 0,
            "total_permissions": len(ALL_CODES),
            "active_tab": "roles",
        },
    )


@require_POST
def role_delete(request, pk):
    blocked = require(request, "role.manage")
    if blocked:
        return blocked

    role = get_object_or_404(RoleDefinition, pk=pk)
    if not role.can_be_deleted:
        messages.error(
            request,
            f"'{role.name}' cannot be deleted - it is "
            + ("a built-in role." if role.is_system
               else f"still assigned to {role.user_count} user(s)."),
        )
        return redirect("core:role_update", pk=role.pk)

    name = role.name
    log_action(
        AuditAction.DELETE,
        instance=role,
        description=f"Deleted role '{name}' ({role.code}).",
    )
    role.delete()
    messages.success(request, f"Role '{name}' deleted.")
    return redirect("core:role_list")


@require_POST
def role_reset(request, pk):
    blocked = require(request, "role.manage")
    if blocked:
        return blocked

    role = get_object_or_404(RoleDefinition, pk=pk)
    if reset_to_blueprint(role):
        log_action(
            AuditAction.ACCESS,
            instance=role,
            description=f"Reset role '{role.name}' to its shipped defaults.",
        )
        messages.success(request, f"'{role.name}' restored to its defaults.")
    else:
        messages.error(request, "Only built-in roles have defaults to restore.")
    return redirect("core:role_update", pk=role.pk)
