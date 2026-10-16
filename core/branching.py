"""
Branches - which shop's books a request reads and writes.

WHAT A BRANCH IS
----------------
A business with two yards keeps two sets of books. Branch 1 and Branch 2
have their own products and stock, their own customers and debts, their own
sales, expenses, payroll, pick-lists and Audit. Nothing is shared and nothing
is added together: choosing Branch 1 shows Branch 1, and only Branch 1.

What IS shared is what is not a book: the user accounts (an account is
given its branch - see accounts.User.allowed_branches), the roles and their
permissions, and the business-wide settings.

HOW A RECORD KNOWS ITS BRANCH
-----------------------------
Every root record - a product, a customer, a sale, an expense, a pick-list
entry - carries a `branch` column. Everything that hangs off one of those
(a sale's lines, a product's stock movements, a debt's repayments) reaches
its branch through its parent, and says how in a `branch_path` class
attribute: "branch" on a sale, "transaction__branch" on a sale line.

HOW A REQUEST IS KEPT INSIDE ITS BRANCH
---------------------------------------
Three pieces, from the outside in:

  1. `BranchMiddleware` (and, for the phone's token logins, the DRF
     authentication classes below) make the signed-in user's current branch
     the ACTIVE branch for the length of the request.

  2. Every branch-owned model's default manager returns rows of the active
     branch only (`BranchQuerySet`). That covers the queries nobody thought
     to scope - a dropdown on a form, a uniqueness check, a count on the
     dashboard - which is what makes the separation hold everywhere rather
     than wherever somebody remembered.

  3. A new row's `branch` defaults to the active branch, so a sale recorded
     in Branch 2 lands in Branch 2 without any code having to say so.

With NO branch active - a management command, a migration, a nightly job, a
test calling a service directly - managers return every row and new rows go
to the main branch, which is exactly what this system did before branches
existed. The filter is tied to a request, never to the model, so background
work that genuinely needs every row still gets every row.

WHY THE FILTER IS LAZY
----------------------
A form field or a serializer field declared on a class builds its queryset
once, when the module is imported - with no request, so no branch. If the
branch were added when the queryset was built, those querysets would list
every branch's categories forever. So `BranchQuerySet` adds the branch at
the moment the SQL is built instead, from whatever branch is active then.
The queryset object itself is never changed; each use gets a filtered copy.
"""
import contextvars
from contextlib import contextmanager

from django.db import models
from django.db.models import Q

#: The branch the current request is working in. None outside a request.
_active = contextvars.ContextVar("faruq_active_branch", default=None)

#: True between BranchMiddleware's entry and exit. The API's authentication
#: classes only activate a branch inside that window, so a view called
#: directly - a test, a shell - can never leave a branch switched on behind it.
_in_request = contextvars.ContextVar("faruq_branch_request", default=False)

#: Cached id of the main branch. Cleared whenever a branch is saved and after
#: a flush (see core.options.refill_after_flush).
_MAIN = {}

#: The "branch" of somebody whose every branch has been closed. No row has
#: it, so every query in such a request comes back empty - and the
#: middleware below refuses the request before it gets that far. It exists
#: because the alternative, None, means "no filter at all".
NO_BRANCH = 0

#: What they are told.
CLOSED_BRANCH_MESSAGE = (
    "Your branch has been closed. Ask the owner to move you to another branch."
)


def _id_of(branch):
    if branch is None:
        return None
    return getattr(branch, "pk", branch)


def current_branch_id():
    """The active branch's id, or None outside a request."""
    return _active.get()


def activate(branch):
    """Make `branch` (a Branch, an id, or None) the active one."""
    return _active.set(_id_of(branch))


def reset(token):
    _active.reset(token)


@contextmanager
def using_branch(branch):
    """Work inside one branch for the length of a `with` block."""
    token = activate(branch)
    try:
        yield
    finally:
        _active.reset(token)


def forget_main_branch():
    _MAIN.clear()


def main_branch_id():
    """
    The branch everything belonged to before there were branches, and the
    one system tasks write into. Created on demand, so a freshly flushed
    database still has somewhere to put the first row.
    """
    cached = _MAIN.get("id")
    if cached is not None:
        return cached
    try:
        from core.models import Branch

        found = Branch.main_id()
    except Exception:
        # The table may not exist yet - during the very migration that adds
        # it. Saying "no branch" there is right: nothing can be written yet.
        return None
    if found is not None:
        _MAIN["id"] = found
    return found


def branch_for_new_row():
    """Default for a new row's `branch`: the active branch, else the main one."""
    active = current_branch_id()
    return active if active else main_branch_id()


def branch_for(*records):
    """
    The branch a new record built from `records` belongs in.

    Inside a request that is simply the active branch - the records were
    looked up through branch-scoped managers, so they are in it too. Outside a
    request (a test, a shell, a data fix) nothing is active, and a sale made
    for a Branch 2 customer must still land in Branch 2 rather than in the
    main branch, so the first record that has a branch decides.
    """
    active = current_branch_id()
    if active:
        return active
    for record in records:
        if record is None:
            continue
        found = branch_of(record)
        if found is not None:
            return found
    return main_branch_id()


# ---------------------------------------------------------------------------
# Querysets and managers
# ---------------------------------------------------------------------------
def branch_path_for(model):
    """The ORM path from `model` to its Branch, or None if it has none."""
    return getattr(model, "branch_path", None)


def _branch_q(model, branch_id):
    """
    The WHERE that keeps `model` inside one branch.

    A root record tests its own column. A record that hangs off a parent -
    a sale's line, a debt's repayment - is kept to the parents that are in
    the branch, as a subquery on the parent's id rather than a join. The
    rows are the same either way; what differs is locking. PostgreSQL's
    SELECT ... FOR UPDATE locks a row in every table of the FROM clause, so
    with a join, locking a debt for a repayment would lock its customer too -
    a change in what the services lock, and a new way for two tills to
    deadlock. A subquery locks nothing.
    """
    path = branch_path_for(model)
    head, _, rest = path.partition("__")
    if not rest:
        condition = Q(**{path: branch_id})
        # A few records belong to the whole business rather than one branch -
        # a sign-in in the activity log. They show in every branch.
        if getattr(model, "branch_shared_when_blank", False):
            condition |= Q(**{f"{path}__isnull": True})
        return condition
    parent = model._meta.get_field(head).related_model
    in_branch_ids = parent._base_manager.filter(**{rest: branch_id}).values("pk")
    return Q(**{f"{head}__in": in_branch_ids})


def _scope(query, model, branch_id):
    scoped = query.clone()
    scoped.add_q(_branch_q(model, branch_id))
    scoped.branch_scope = branch_id
    return scoped


class BranchQuerySet(models.QuerySet):
    """
    A queryset that stays inside the active branch. See the module docstring
    for why the branch is added when the SQL is built rather than earlier.
    """

    @property
    def query(self):
        query = models.QuerySet.query.fget(self)
        branch_id = _active.get()
        if branch_id is None or getattr(query, "branch_scope", None) is not None:
            return query
        # A sliced or combined query cannot take another WHERE. Nothing in
        # this codebase builds one before a request starts, so in practice
        # every such query was built - and scoped - inside the request.
        if query.is_sliced or query.combinator:
            return query
        return _scope(query, self.model, branch_id)

    @query.setter
    def query(self, value):
        models.QuerySet.query.fset(self, value)


class BranchManager(models.Manager):
    """
    The default manager of a branch-owned model.

    A queryset made inside a request is scoped as it is made, so it is
    filtered once and every later use of it is plain SQL.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        branch_id = _active.get()
        if branch_id is not None and isinstance(qs, BranchQuerySet):
            qs._query = _scope(qs._query, self.model, branch_id)
        return qs


def branch_manager(queryset_class=None):
    """A branch-aware default manager built on a model's own queryset."""
    base = queryset_class or models.QuerySet
    if not issubclass(base, BranchQuerySet):
        base = type(f"Branch{base.__name__}", (BranchQuerySet, base), {})
    return BranchManager.from_queryset(base)()


def branch_field(*, null=False, related_name="+"):
    """
    The `branch` column of a root record.

    A nullable one is for records that may belong to the whole business - a
    sign-in in the activity log. Its default is the active branch and nothing
    else, so what happens outside any branch stays outside every branch.
    """
    return models.ForeignKey(
        "core.Branch",
        on_delete=models.PROTECT,
        null=null,
        blank=null,
        default=current_branch_id if null else branch_for_new_row,
        related_name=related_name,
        db_index=True,
    )


# ---------------------------------------------------------------------------
# Keeping a queryset in its branch explicitly
# ---------------------------------------------------------------------------
def in_branch(queryset):
    """
    Keep `queryset` to the active branch.

    Default managers already do this; this is for a queryset that did not
    come from one - built on `_base_manager`, say - and is called by
    core.scoping.scoped() so every scoped list is branch-scoped by
    construction. Outside a request there is no active branch and the
    queryset is returned untouched, as everywhere else.
    """
    if queryset is None:
        return queryset
    path = branch_path_for(queryset.model)
    branch_id = current_branch_id()
    if path is None or branch_id is None:
        return queryset
    if getattr(queryset.query, "branch_scope", None) == branch_id:
        return queryset
    return queryset.filter(_branch_q(queryset.model, branch_id))


def same_branch(instance) -> bool:
    """
    Whether a record belongs to the active branch - True outside a request,
    and True for a model that has no branch at all.
    """
    branch_id = current_branch_id()
    if branch_id is None or branch_path_for(type(instance)) is None:
        return True
    found = branch_of(instance)
    if found is None:
        return bool(getattr(type(instance), "branch_shared_when_blank", False))
    return found == branch_id


def branch_of(instance):
    """The branch id a record belongs to, walking its `branch_path`."""
    path = branch_path_for(type(instance))
    if path is None:
        return None
    current = instance
    parts = path.split("__")
    for part in parts[:-1]:
        current = getattr(current, part, None)
        if current is None:
            return None
    return getattr(current, f"{parts[-1]}_id", None)


# ---------------------------------------------------------------------------
# Making a request's branch the active one
# ---------------------------------------------------------------------------
class BranchMiddleware:
    """
    The signed-in user's branch is the active branch for the whole request.

    Runs after AuthenticationMiddleware, so a browser session is known here.
    A phone signs in with a token, which DRF only reads inside the view; the
    authentication classes below activate the branch there. Either way the
    value is put back when the response leaves, so nothing leaks into the
    next request served by the same thread.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        branch_id = None
        if user is not None and getattr(user, "is_authenticated", False):
            branch_id = getattr(user, "current_branch_id", None)
        token = _active.set(branch_id)
        window = _in_request.set(True)
        try:
            if branch_id == NO_BRANCH and not _open_without_a_branch(request):
                return _closed_branch_response(request)
            return self.get_response(request)
        finally:
            _in_request.reset(window)
            _active.reset(token)


def _open_without_a_branch(request) -> bool:
    """Signing out still works for somebody whose branch has closed."""
    path = request.path
    return path.rstrip("/").endswith("/logout")


def _closed_branch_response(request):
    if request.path.startswith("/api/"):
        from django.http import JsonResponse

        return JsonResponse({"detail": CLOSED_BRANCH_MESSAGE}, status=403)
    from django.shortcuts import render

    return render(request, "system/branch_closed.html", status=403)


def activate_for_user(user):
    """
    Make `user`'s branch the active one, for the rest of this request.

    Called by the API's authentication classes once a token is read, and by
    the branch switch so the page answering the switch already shows the new
    branch. Does nothing outside BranchMiddleware's window - see _in_request.
    """
    if not _in_request.get():
        return
    if user is None or not getattr(user, "is_authenticated", False):
        return
    _active.set(getattr(user, "current_branch_id", None))
    return getattr(user, "current_branch_id", None)
