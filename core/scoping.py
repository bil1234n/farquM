"""
Per-manager data isolation ("ownership scoping").

THE RULE OF THIS FILE
---------------------
    ADMIN    sees every record in the system, whoever created it.
    MANAGER  sees only the records they own. Never another manager's.

Ownership is a single `owner` FK stamped on the four record types that carry
business meaning:

    inventory.Product
    sales.Customer
    sales.Transaction
    credit.DebtRecord

Everything else is scoped by following a relation back to one of those four:

    StockMovement    -> product__owner
    TransactionItem  -> transaction__owner
    Receipt          -> transaction__owner
    CreditAccount    -> customer__owner
    Repayment        -> debt__owner

Category and Supplier are deliberately NOT scoped. They are shared lookup
lists - a label, not a business record - and duplicating "Beverages" per
manager would create more confusion than it prevents.

WHY A HELPER RATHER THAN A CUSTOM MANAGER
-----------------------------------------
A default manager that silently filters is dangerous: background jobs,
reconciliation tasks and migrations legitimately need every row, and a
manager that hides rows from them corrupts totals in ways nobody notices for
months. So filtering is explicit and always driven by a request user.

FAIL CLOSED
-----------
An anonymous or unknown user gets `none()`, not everything. A row with
owner=NULL (legacy data, or a record whose owning manager was deleted) is
visible to admins only. Both are deliberate: the failure mode of this module
must be "shows too little", never "leaks another manager's books".
"""
from django.db.models import Q

# path from the model being filtered to the User who owns it
OWNER_PATHS = {
    "inventory.Product": "owner",
    "inventory.StockMovement": "product__owner",
    "sales.Customer": "owner",
    "sales.Transaction": "owner",
    "sales.TransactionItem": "transaction__owner",
    "sales.Receipt": "transaction__owner",
    "credit.DebtRecord": "owner",
    "credit.CreditAccount": "customer__owner",
    "credit.Repayment": "debt__owner",
    "credit.RepaymentProof": "repayment__debt__owner",
}


def is_admin(user) -> bool:
    """True for an authenticated, active administrator."""
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "role", None) == "ADMIN"
    )


def sees_everything(user) -> bool:
    """Admins (and Django superusers) are not scoped."""
    return is_admin(user) or bool(getattr(user, "is_superuser", False))


def owner_path_for(model) -> str:
    """Look up the ORM path from `model` to its owning User."""
    label = f"{model._meta.app_label}.{model.__name__}"
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

    path = path or owner_path_for(queryset.model)
    return queryset.filter(**{path: user.pk})


def owned_by(user):
    """
    The owner to stamp on a NEW record created by `user`.

    An admin creating a record owns it themselves. That keeps `owner` non-null
    on every new row, which in turn keeps the "fail closed" rule meaningful:
    a NULL owner can then only ever mean legacy data.
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
    if not (user and getattr(user, "is_authenticated", False)):
        return False

    owner_id = _resolve_owner_id(instance)
    return owner_id is not None and owner_id == user.pk


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

    An admin may filter by anyone. A manager only ever sees themselves, so
    offering a list of colleagues would just advertise that other people's
    data exists.
    """
    from accounts.models import User

    if sees_everything(user):
        return User.objects.active_staff()
    if user is not None and getattr(user, "is_authenticated", False):
        return User.objects.filter(pk=user.pk)
    return User.objects.none()


def owner_filter_q(user, path: str = "owner") -> Q:
    """Q object form, for when a filter must be combined with OR logic."""
    if sees_everything(user):
        return Q()
    if not (user and getattr(user, "is_authenticated", False)):
        return Q(pk__in=[])
    return Q(**{path: user.pk})
