"""
Data isolation ("ownership scoping") - WHOSE records a user may see.

This is the second half of the access system. core/permissions.py answers
"what may this person do"; this file answers "to which rows". Both have to
pass. A sales user with `sale.view` still only ever sees their own sales.

THE THREE SCOPES
----------------
    ALL   Sees every record in the business, whoever created it. Admin.
    TEAM  Sees their own records, plus those of everyone who reports to them
          (User.manager). Manager.
    OWN   Sees only their own records. Sales.

THE ONE EXCEPTION THAT MAKES THE SALES ROLE WORK
------------------------------------------------
A sales user owns no stock - the manager does. If products were scoped the
same way as sales, a sales assistant would open the till and find an empty
catalogue, which is not a shop.

So there are two families of record, scoped differently:

    LEDGER   sales, customers, debts, repayments, receipts
             "things I did". Strictly OWN for a sales user - their takings,
             their customers, their debts. This is what makes a credit sale
             genuinely "under his obligation", and what lets the app answer
             "how much have I made" with a number that means something.

    SHARED   products, stock, raw materials, recipes, production runs
             "what the business has". ONE catalogue, visible to every signed-in
             member of staff, whoever entered it. A product added by the owner
             is on the same shelf as one added by a manager, because there is
             only one shelf - the business sells hollow blocks whoever typed
             them in, and a second salesperson must not open the till to find
             half a catalogue.

             Who may CHANGE any of it stays a permission question, not a
             scoping one: `product.edit`, `material.adjust` and
             `production.reverse` are not in the Sales role, so a salesperson
             sees the whole shelf and can alter none of it.

WHY SHARED AND NOT "MY MANAGER'S"
---------------------------------
This used to be scoped to the user's own manager, which quietly split the
business in two the moment a second manager was hired: each one stocked a
catalogue the other could not see, the same physical product got entered
twice, and stock figures for one cement bay lived under two owners. The shelf
is a fact about the business, not about who happened to type it.

WHY A HELPER RATHER THAN A CUSTOM MANAGER
-----------------------------------------
A default manager that silently filters is dangerous: background jobs,
reconciliation tasks and migrations legitimately need every row, and a manager
that hides rows from them corrupts totals in ways nobody notices for months.
So filtering is explicit and always driven by a request user.

FAIL CLOSED
-----------
An anonymous or unknown user gets `none()`, not everything. A row with
owner=NULL (legacy data, or a record whose owning user was deleted) is visible
to full-scope users only. Both are deliberate: the failure mode of this module
must be "shows too little", never "leaks another person's books".
"""
from django.db.models import Q

# path from the model being filtered to the User who owns it
OWNER_PATHS = {
    "inventory.Product": "owner",
    "inventory.StockMovement": "product__owner",
    "production.RawMaterial": "owner",
    "production.MaterialMovement": "material__owner",
    "production.Recipe": "product__owner",
    "production.RecipeItem": "recipe__product__owner",
    "production.ProductionRun": "owner",
    "production.ProductionMaterial": "run__owner",
    "sales.Customer": "owner",
    "sales.Transaction": "owner",
    "sales.TransactionItem": "transaction__owner",
    "sales.Receipt": "transaction__owner",
    "credit.DebtRecord": "owner",
    "credit.CreditAccount": "customer__owner",
    "credit.Repayment": "debt__owner",
    "credit.RepaymentProof": "repayment__debt__owner",
}

#: Models that follow the SHARED rule (see the module docstring). Everything
#: else in OWNER_PATHS is a LEDGER record and stays owner-filtered.
CATALOG_LABELS = frozenset(
    {
        "inventory.Product",
        # Movements are shared because the product is. Showing someone a stock
        # figure of 60 while hiding the rows that add up to 60 makes the ledger
        # look broken to the person reading it.
        "inventory.StockMovement",
        # The plant follows the shelf. Production consumes shared materials and
        # produces shared stock, so hiding the batch that moved them would
        # leave a colleague watching numbers change for no visible reason.
        "production.RawMaterial",
        "production.MaterialMovement",
        "production.Recipe",
        "production.RecipeItem",
        "production.ProductionRun",
        "production.ProductionMaterial",
    }
)

# Category and Supplier are deliberately absent from OWNER_PATHS. They are
# shared lookup lists - a label, not a business record - and duplicating
# "Beverages" per manager would create more confusion than it prevents.


def _label(model) -> str:
    return f"{model._meta.app_label}.{model.__name__}"


def _usable(user) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
    )


def is_admin(user) -> bool:
    """True for an authenticated, active user with full control."""
    return bool(_usable(user) and getattr(user, "is_admin", False))


def sees_everything(user) -> bool:
    """True when no owner filter should be applied at all."""
    if bool(getattr(user, "is_superuser", False)) and _usable(user):
        return True
    return bool(_usable(user) and getattr(user, "data_scope", "OWN") == "ALL")


def ledger_owner_ids(user) -> set[int] | None:
    """
    Whose sales / customers / debts this user may see.

    Returns None for "everything, no filter". Returns a set of user IDs
    otherwise - never an empty set for a valid user, since everyone can always
    see their own work.
    """
    if sees_everything(user):
        return None
    if not _usable(user):
        return set()

    ids = {user.pk}
    if getattr(user, "data_scope", "OWN") == "TEAM":
        ids |= set(getattr(user, "team_ids", ()) or ())
    return ids


def catalog_owner_ids(user) -> set[int] | None:
    """
    Whose products, stock and materials this user may see: everyone's.

    Returns None - "apply no owner filter" - for any signed-in, active member
    of staff. There is one catalogue for the business, so the answer does not
    depend on who is asking.

    Still fails closed: an anonymous or deactivated account gets the empty set
    and therefore no rows at all. "Shared with the staff" is not "public".
    """
    if not _usable(user):
        return set()
    return None


def visible_owner_ids(user, model_or_label) -> set[int] | None:
    """Dispatch to the right rule for the model being filtered."""
    label = (
        model_or_label
        if isinstance(model_or_label, str)
        else _label(model_or_label)
    )
    if label in CATALOG_LABELS:
        return catalog_owner_ids(user)
    return ledger_owner_ids(user)


def owner_path_for(model) -> str:
    """Look up the ORM path from `model` to its owning User."""
    label = _label(model)
    try:
        return OWNER_PATHS[label]
    except KeyError as exc:  # pragma: no cover - programmer error
        raise KeyError(
            f"{label} has no entry in core.scoping.OWNER_PATHS. Add one before "
            f"scoping it, or the queryset will silently return everything."
        ) from exc


def scoped(queryset, user, path: str | None = None):
    """
    Restrict `queryset` to what `user` is allowed to see.

        qs = scoped(Product.objects.alive(), request.user)

    Pass `path` explicitly when the queryset is built from a model that is not
    in OWNER_PATHS but can still reach an owner (rare).
    """
    if queryset is None:
        return queryset

    if not (user is not None and getattr(user, "is_authenticated", False)):
        return queryset.none()

    if sees_everything(user):
        return queryset

    if not getattr(user, "is_active", False):
        return queryset.none()

    owner_ids = visible_owner_ids(user, queryset.model)
    # None and set() are BOTH falsy and mean opposite things: None is "no
    # filter, show everything", set() is "show nothing". Test for None first or
    # the shared catalogue disappears for every non-admin in the business.
    if owner_ids is None:
        return queryset
    if not owner_ids:
        return queryset.none()

    path = path or owner_path_for(queryset.model)
    # __in rather than a plain equality test, because TEAM scope and the
    # manager's-shelf rule both resolve to several owners.
    return queryset.filter(**{f"{path}__in": sorted(owner_ids)})


def owned_by(user):
    """
    The owner to stamp on a NEW record created by `user`.

    Always the creator, never their manager. A sales user's sale, customer and
    debt belong to that sales user - that is what makes the debt theirs to
    collect and their figures theirs to be measured by. The manager still sees
    it, through TEAM scope, without owning it.
    """
    return user if getattr(user, "is_authenticated", False) else None


def stamp_owner(instance, user):
    """Set instance.owner if the model has one and it is not already set."""
    if hasattr(instance, "owner_id") and instance.owner_id is None:
        instance.owner = owned_by(user)
    return instance


def can_touch(instance, user) -> bool:
    """
    Object-level check: may `user` read or modify this specific record?

    Used as a second line of defence on detail endpoints, where an ID arrives
    straight from the client and a scoped queryset was not used.
    """
    if sees_everything(user):
        return True
    if not _usable(user):
        return False

    visible = visible_owner_ids(user, type(instance))
    # None means "no owner filter applies to this model" - a shared catalogue
    # row. Answer before looking at the owner at all, so that a product with
    # owner=NULL (legacy rows, or one whose creator was deleted) is still
    # restockable. Otherwise a user sees it in the list and is told it is "not
    # in your product list" the moment they touch it.
    if visible is None:
        return True

    owner_id = _resolve_owner_id(instance)
    if owner_id is None:
        return False
    return owner_id in visible


def _resolve_owner_id(instance):
    """Walk the OWNER_PATHS relation on a concrete instance."""
    try:
        path = owner_path_for(type(instance))
    except KeyError:
        return getattr(instance, "owner_id", None)

    parts = path.split("__")
    current = instance
    for part in parts[:-1]:
        current = getattr(current, part, None)
        if current is None:
            return None
    return getattr(current, f"{parts[-1]}_id", None)


def visible_users(user):
    """
    Which staff members' names may appear in a filter dropdown.

    A full-scope user may filter by anyone. A manager sees themselves and
    their team. Everyone else only ever sees themselves, so offering a list of
    colleagues would just advertise that other people's data exists.
    """
    from accounts.models import User

    if sees_everything(user):
        return User.objects.active_staff()
    ids = ledger_owner_ids(user)
    if not ids:
        return User.objects.none()
    return User.objects.filter(pk__in=sorted(ids))


def owner_filter_q(user, path: str = "owner", model_label: str | None = None) -> Q:
    """Q object form, for when a filter must be combined with OR logic."""
    if sees_everything(user):
        return Q()
    ids = (
        visible_owner_ids(user, model_label)
        if model_label
        else ledger_owner_ids(user)
    )
    # As in scoped(): None is "no filter", set() is "nothing". An empty Q()
    # matches every row, which is exactly right for a shared catalogue and
    # exactly wrong for a locked-out account - so the order matters.
    if ids is None:
        return Q()
    if not ids:
        return Q(pk__in=[])
    return Q(**{f"{path}__in": sorted(ids)})
