"""
Opening, renaming, closing and switching branches.

How branches keep their books apart is core/branching.py. This is what
people DO with branches, and the rules they do it under:

  * The main branch always exists and is always open. It is where the
    business was before there were branches, and where an account that has
    not been given a branch works. It cannot be closed.

  * A branch is never deleted. Closing one takes it out of every switcher and
    stops anybody working in it; its books stay exactly as they were, and
    opening it again brings them back.

  * A new branch starts EMPTY - no products, customers or sales - with its
    own copy of the default pick-lists (units, banks, expense headings...).
    Nothing is copied from another branch, because nothing is shared.

  * Who may move between them: whoever holds `branch.all` may open any open
    branch; everybody else works in the branches they have been given, and
    an account given none works in the main branch.
"""
from django.db import transaction

from accounts.models import AuditAction
from accounts.services import log_action

from .branching import activate_for_user
from .models import Branch
from .options import seed_branch_options


class BranchError(Exception):
    """A refusal. The message is safe to show to the person who asked."""


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())[:120]
    if not name:
        raise BranchError("Give the branch a name.")
    return name


def _ensure_free(name, *, exclude_pk=None):
    clash = Branch.objects.filter(name__iexact=name)
    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)
    if clash.exists():
        raise BranchError("There is already a branch with that name.")


def _short(value, length) -> str:
    return " ".join(str(value or "").split())[:length]


def open_branches():
    return Branch.objects.filter(is_active=True)


def has_several_branches() -> bool:
    """Whether the business runs more than one open branch."""
    return open_branches().count() > 1


@transaction.atomic
def create_branch(*, user, name, address="", phone="") -> Branch:
    """Open a new, empty branch with the default pick-lists."""
    name = _clean_name(name)
    _ensure_free(name)
    last = (
        Branch.objects.order_by("-sort_order")
        .values_list("sort_order", flat=True)
        .first()
    ) or 0
    branch = Branch.objects.create(
        name=name,
        address=_short(address, 255),
        phone=_short(phone, 40),
        sort_order=last + 10,
    )
    seed_branch_options(branch)
    log_action(
        AuditAction.CREATE,
        instance=branch,
        description=f"Opened the branch '{branch.name}'.",
        user=user,
    )
    return branch


@transaction.atomic
def update_branch(branch, *, user, name=None, address=None, phone=None) -> Branch:
    """Rename a branch, or change where it is and how to reach it."""
    changes = {}
    if name is not None:
        name = _clean_name(name)
        _ensure_free(name, exclude_pk=branch.pk)
        if name != branch.name:
            changes["name"] = {"from": branch.name, "to": name}
            branch.name = name
    if address is not None:
        address = _short(address, 255)
        if address != branch.address:
            changes["address"] = {"from": branch.address, "to": address}
            branch.address = address
    if phone is not None:
        phone = _short(phone, 40)
        if phone != branch.phone:
            changes["phone"] = {"from": branch.phone, "to": phone}
            branch.phone = phone
    if changes:
        branch.save()
        log_action(
            AuditAction.UPDATE,
            instance=branch,
            description=f"Updated the branch '{branch.name}'.",
            changes=changes,
            user=user,
        )
    return branch


@transaction.atomic
def set_branch_open(branch, *, user, is_open: bool) -> Branch:
    """Close a branch (its books stay) or open it again."""
    is_open = bool(is_open)
    if branch.is_main and not is_open:
        raise BranchError("The main branch cannot be closed.")
    if branch.is_active == is_open:
        return branch
    branch.is_active = is_open
    branch.save(update_fields=["is_active", "updated_at"])
    log_action(
        AuditAction.UPDATE,
        instance=branch,
        description=(
            f"Opened the branch '{branch.name}' again."
            if is_open
            else f"Closed the branch '{branch.name}'. Its records are kept."
        ),
        user=user,
    )
    return branch


def switch_branch(user, branch_id) -> Branch:
    """
    Move `user` into another branch.

    Remembered on the account, so the phone and the website stay in the same
    branch. The rest of the current request works in the new branch too, so
    whatever answers the switch already shows it.
    """
    try:
        branch_id = int(branch_id)
    except (TypeError, ValueError):
        raise BranchError("Choose a branch.") from None
    if branch_id not in user.allowed_branch_ids:
        raise BranchError("You do not work in that branch.")
    if user.active_branch_id != branch_id:
        user.active_branch_id = branch_id
        user.save(update_fields=["active_branch"])
    user.refresh_access()
    activate_for_user(user)
    return Branch.objects.get(pk=branch_id)


def set_user_branches(target, branch_ids) -> list[Branch]:
    """
    The branches `target` works in. Empty means the main branch only.

    Ids that are not branches are ignored rather than refused: the form only
    offers real ones, so an unknown id is a stale screen, not an attack worth
    a message.
    """
    ids = set()
    for raw in branch_ids or ():
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    chosen = list(Branch.objects.filter(pk__in=ids))
    target.branches.set(chosen)
    target.refresh_access()
    return chosen


def count_staff(branches) -> None:
    """
    Set `staff_count` on each branch: the active people who work there.

    Somebody given no branch works in the main one, so they are counted
    there. The owner and others who work in every branch are left out - they
    would be on every line and say nothing about any of them.
    """
    from accounts.models import User

    branches = list(branches)
    for branch in branches:
        branch.staff_count = 0
    for person in User.objects.filter(is_active=True).prefetch_related("branches"):
        if person.works_in_every_branch:
            continue
        given = {b.pk for b in person.branches.all()}
        for branch in branches:
            if branch.pk in given or (branch.is_main and not given):
                branch.staff_count += 1


def branch_names(user) -> str:
    """What to show for where somebody works: 'All branches' or the names."""
    if user.works_in_every_branch:
        return "All branches"
    names = [b.name for b in user.branches.filter(is_active=True)]
    if not names:
        return Branch.objects.filter(pk=Branch.main_id()).values_list(
            "name", flat=True
        ).first() or Branch.MAIN_NAME
    return ", ".join(names)
