"""
Turning a screen full of ticked boxes into stored access - and back again.

THE ONE IDEA IN THIS FILE
-------------------------
The administrator sees a grid of every permission and ticks the ones this
person should have. What gets SAVED is not that list. It is the DIFFERENCE
between that list and the role's list:

    extra_permissions  = ticked - role
    denied_permissions = role - ticked

Storing the difference rather than the absolute set is what makes roles
useful. Edit the Manager role to add "may see profit", and every manager gains
it tomorrow - including the ones whose individual pages were opened and saved
last week. If each user stored a flat copy of their permissions instead, the
role would be a one-off template that goes stale the moment anybody touches a
user, and "give all managers X" would become a job for a script.

The cost is that a permission can be held for two different reasons, and the
UI has to say which:

    inherited  ticked, from the role. Untick it to create a denial.
    granted    ticked, this person only. Untick it to remove the grant.
    denied     unticked, although the role has it. Tick it to restore.
    absent     unticked, and the role does not have it either.

`describe_permission_state()` returns exactly those four words so the template
does not have to work them out.
"""
from accounts.models import AuditAction, DataScope, RoleDefinition, User
from accounts.services import log_action
from core.permissions import (
    ALL_CODES,
    CATALOG,
    PERM_BY_CODE,
    WILDCARD,
    clean_codes,
    label_for,
)

INHERITED = "inherited"
GRANTED = "granted"
DENIED = "denied"
ABSENT = "absent"


def role_permission_set(role) -> frozenset[str]:
    """Concrete codes a role grants, wildcard expanded."""
    if role is None:
        return frozenset()
    return role.permission_set


def describe_permission_state(code: str, role_codes, extra, denied) -> str:
    in_role = code in role_codes
    if code in denied:
        return DENIED if in_role else ABSENT
    if code in extra:
        return GRANTED
    return INHERITED if in_role else ABSENT


def build_matrix(*, role, extra=(), denied=(), locked: set[str] | None = None):
    """
    The catalogue, annotated for rendering.

    Returns a list of groups, each with its permissions and their state. Both
    the web grid and the mobile grid are built from this, so the two clients
    cannot drift into showing different things.
    """
    role_codes = role_permission_set(role)
    extra = set(extra or ())
    denied = set(denied or ())
    locked = locked or set()

    groups = []
    for group in CATALOG:
        rows = []
        for perm in group.perms:
            state = describe_permission_state(perm.code, role_codes, extra, denied)
            rows.append(
                {
                    "code": perm.code,
                    "label": perm.label,
                    "help": perm.help,
                    "sensitive": perm.sensitive,
                    "state": state,
                    "checked": state in (INHERITED, GRANTED),
                    "in_role": perm.code in role_codes,
                    "locked": perm.code in locked,
                }
            )
        groups.append(
            {
                "key": group.key,
                "label": group.label,
                "icon": group.icon,
                "blurb": group.blurb,
                "permissions": rows,
                "checked_count": sum(1 for r in rows if r["checked"]),
                "total": len(rows),
            }
        )
    return groups


def diff_against_role(role, ticked) -> tuple[list[str], list[str]]:
    """
    Split a set of ticked codes into (extra, denied) relative to `role`.

    A role holding the wildcard has every permission, so nothing can be
    "extra" against it and unticking a box always produces a denial. That
    falls out of the set arithmetic below without a special case, because
    `permission_set` expands the wildcard first.
    """
    role_codes = role_permission_set(role)
    ticked = set(clean_codes(ticked))
    extra = sorted(ticked - role_codes)
    denied = sorted(role_codes - ticked)
    return extra, denied


def apply_user_access(
    *,
    user: User,
    role_code: str,
    ticked,
    manager=None,
    data_scope_override: str = "",
    editor=None,
    source: str = "the web app",
) -> dict:
    """
    Save one person's access and write an audit entry describing the change.

    Returns a summary dict so the caller can build a message without
    recomputing anything. Does NOT enforce who may call it - that is the
    view's job, and doing it in both places would mean two rules to keep in
    step.
    """
    role = RoleDefinition.objects.filter(code=role_code).first()
    before = {
        "role": user.role,
        "manager": user.manager.display_name if user.manager_id else "",
        "scope": user.data_scope,
        "permissions": sorted(user.effective_permissions),
    }

    extra, denied = diff_against_role(role, ticked)

    user.role = role_code
    user.manager = manager
    user.data_scope_override = data_scope_override or ""
    user.extra_permissions = extra
    user.denied_permissions = denied
    user.save(
        update_fields=[
            "role",
            "manager",
            "data_scope_override",
            "extra_permissions",
            "denied_permissions",
        ]
    )
    user.refresh_access()

    after_perms = sorted(user.effective_permissions)
    gained = [c for c in after_perms if c not in before["permissions"]]
    lost = [c for c in before["permissions"] if c not in after_perms]

    changes = {}
    if before["role"] != user.role:
        changes["role"] = {"from": before["role"], "to": user.role}
    if before["scope"] != user.data_scope:
        changes["data_scope"] = {"from": before["scope"], "to": user.data_scope}
    now_manager = user.manager.display_name if user.manager_id else ""
    if before["manager"] != now_manager:
        changes["manager"] = {"from": before["manager"] or "-", "to": now_manager or "-"}
    if gained:
        changes["granted"] = {"from": "", "to": ", ".join(label_for(c) for c in gained)}
    if lost:
        changes["revoked"] = {"from": ", ".join(label_for(c) for c in lost), "to": ""}

    log_action(
        AuditAction.ACCESS,
        instance=user,
        description=(
            f"Access updated for '{user.username}' from {source}: role "
            f"{before['role']} -> {user.role}, {len(gained)} permission(s) "
            f"granted, {len(lost)} revoked."
        ),
        changes=changes or None,
        user=editor,
    )

    return {
        "gained": gained,
        "lost": lost,
        "extra": extra,
        "denied": denied,
        "changed": bool(changes),
    }


def apply_role_permissions(
    *, role: RoleDefinition, ticked, editor=None, source: str = "the web app"
) -> dict:
    """Save a role's permission list and log what moved."""
    before = sorted(role.permission_set)
    codes = clean_codes(ticked)
    role.permissions = codes
    role.save()
    # permission_set is a cached_property computed before the save.
    role.__dict__.pop("permission_set", None)

    after = sorted(role.permission_set)
    gained = [c for c in after if c not in before]
    lost = [c for c in before if c not in after]

    affected = role.user_count
    log_action(
        AuditAction.ACCESS,
        instance=role,
        description=(
            f"Role '{role.name}' updated from {source}: {len(gained)} "
            f"permission(s) added, {len(lost)} removed. {affected} user(s) "
            f"affected."
        ),
        changes={
            "granted": {"from": "", "to": ", ".join(label_for(c) for c in gained)},
            "revoked": {"from": ", ".join(label_for(c) for c in lost), "to": ""},
        }
        if (gained or lost)
        else None,
        user=editor,
    )
    return {"gained": gained, "lost": lost, "affected": affected}


def reset_user_to_role(user: User, *, editor=None) -> None:
    """Clear every per-person adjustment, leaving the role's own set."""
    user.extra_permissions = []
    user.denied_permissions = []
    user.data_scope_override = ""
    user.save(
        update_fields=["extra_permissions", "denied_permissions", "data_scope_override"]
    )
    user.refresh_access()
    log_action(
        AuditAction.ACCESS,
        instance=user,
        description=(
            f"Reset '{user.username}' to the plain {user.get_role_display()} role - "
            f"all individual grants and denials removed."
        ),
        user=editor,
    )


def access_summary(user: User) -> dict:
    """Compact description of one user's access, for lists and API payloads."""
    role = user.role_definition
    held = user.effective_permissions
    return {
        "role": user.role,
        "role_name": user.get_role_display(),
        "data_scope": user.data_scope,
        "data_scope_label": user.scope_label,
        "manager": user.manager.display_name if user.manager_id else None,
        "manager_id": user.manager_id,
        "permission_count": len(held),
        "total_permissions": len(ALL_CODES),
        "extra_count": len([c for c in (user.extra_permissions or []) if c in ALL_CODES]),
        "denied_count": len([c for c in (user.denied_permissions or []) if c in ALL_CODES]),
        "is_customised": bool(
            user.extra_permissions or user.denied_permissions or user.data_scope_override
        ),
        "full_access": WILDCARD in (role.permissions if role else []),
        "permissions": sorted(held),
    }


def sensitive_grants(user: User) -> list[str]:
    """Labels of the risky permissions this user actually holds."""
    return [
        PERM_BY_CODE[c].label
        for c in sorted(user.effective_permissions)
        if c in PERM_BY_CODE and PERM_BY_CODE[c].sensitive
    ]


def scope_choices_with_help():
    return [
        (
            DataScope.OWN,
            "Own records only",
            "Sees only the sales, customers and debts they created.",
        ),
        (
            DataScope.TEAM,
            "Own records and their team's",
            "Also sees everything created by the people who report to them.",
        ),
        (
            DataScope.ALL,
            "Everything in the business",
            "No filtering at all. Reserve this for owners.",
        ),
    ]
