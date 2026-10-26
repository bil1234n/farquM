"""
Wipe ONE branch's trading data - for an admin starting that branch's books
over. Every other branch is left exactly as it was.

WHAT GOES (in the chosen branch only)
-------------------------------------
  * sales (transactions, their lines, extra charges, receipts, deliveries)
  * debts and repayments (they only exist because of sales)
  * expenses (many point at a batch or a sale's extra charge)
  * products, their stock movements, cost estimates and categories
  * production: batches, their materials and damages, stock requests
  * raw materials, their movements, and recipes
  * push-notification history of the branch

WHAT STAYS
----------
  * everything in every other branch
  * users, roles, permissions, branches, passcodes, business settings
  * customers - name, phone, photos, notes, credit limit. Their cached
    balances are reset to zero, because the debts behind them are gone.
  * employees and suppliers (people, like customers)
  * the managed pick-lists (banks, damage types, expense categories ...)
  * the audit log. It is append-only by design, and it records this wipe.

There is NO backup and no undo. The view that calls this demands the admin's
password; this module only does the deleting.

HOW THE BRANCH IS KEPT
----------------------
Every model here says how it reaches its branch in `branch_path` ("branch"
on a sale, "transaction__branch" on a sale line - see core.branching), and
each delete is filtered on exactly that path. Deletes go through
`_base_manager`, which ignores soft-delete filtering, so an archived product
in this branch goes too instead of staying behind holding a PROTECT
reference to a sale. Nothing is deleted from a model that has no
`branch_path`: better to refuse than to guess and touch another branch.
"""
from decimal import Decimal

from django.apps import apps
from django.db import transaction

from core.branching import branch_path_for

# Children before parents, so no PROTECT foreign key is ever left pointing at
# a row that is being removed. (app_label, ModelName)
WIPE_ORDER = [
    ("api", "NotificationLog"),
    # Money owed
    ("credit", "RepaymentProof"),
    ("credit", "Repayment"),
    ("credit", "DebtRecord"),
    # Money spent - before batches and extra charges, which they PROTECT
    ("expenses", "Expense"),
    # Goods leaving the yard
    ("sales", "DeliveryLine"),
    ("sales", "Delivery"),
    # Sales
    ("sales", "Receipt"),
    ("sales", "ExtraCharge"),
    ("sales", "TransactionItem"),
    ("sales", "Transaction"),
    # Production
    ("production", "ProductionRequest"),
    ("production", "ProductionDamage"),
    ("production", "ProductionMaterial"),
    ("production", "ProductionRun"),
    ("production", "MaterialMovement"),
    ("production", "RecipeItem"),
    ("production", "Recipe"),
    ("production", "RawMaterial"),
    # Stock and products
    ("inventory", "StockMovement"),
    ("inventory", "ProductCostEstimate"),
    ("inventory", "Product"),
    ("inventory", "Category"),
]


class WipeError(Exception):
    pass


def _in_branch(model, branch_id):
    """`model`'s rows that belong to `branch_id`, and nothing else."""
    path = branch_path_for(model)
    if not path:
        raise WipeError(
            f"{model.__name__} has no branch_path - refusing to delete it, "
            "because it cannot be kept to one branch."
        )
    return model._base_manager.filter(**{path: branch_id})


def wipe_branch_data(branch_id) -> dict:
    """
    Delete everything in WIPE_ORDER that belongs to `branch_id`, in one
    transaction. Other branches are not touched.

    Returns {"Model": rows_deleted, ...} for the models that had rows.
    Either all of it goes or none of it does.
    """
    if not branch_id:
        raise WipeError("No branch is selected.")

    counts = {}
    with transaction.atomic():
        for app_label, model_name in WIPE_ORDER:
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                continue  # a model renamed or removed later - skip, don't fail
            deleted, _ = _in_branch(model, branch_id).delete()
            if deleted:
                counts[model_name] = deleted

        # This branch's customers stay; what they owed does not.
        CreditAccount = apps.get_model("credit", "CreditAccount")
        _in_branch(CreditAccount, branch_id).update(
            total_credit_extended=Decimal("0"),
            total_repaid=Decimal("0"),
            outstanding_balance=Decimal("0"),
            last_purchase_date=None,
            last_payment_date=None,
        )
    return counts
