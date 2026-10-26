"""
Wipe the business's trading data - for an admin starting the books over.

WHAT GOES
---------
Every branch's:
  * sales (transactions, their lines, extra charges, receipts, deliveries)
  * debts and repayments (they only exist because of sales)
  * expenses (many point at a batch or a sale's extra charge)
  * products, their stock movements, cost estimates and categories
  * production: batches, their materials and damages, stock requests
  * raw materials, their movements, and recipes
  * push-notification history (it points at sales and debts that are gone)

WHAT STAYS
----------
  * users, roles, permissions, branches, passcodes, business settings
  * customers - name, phone, photos, notes, credit limit. Their cached
    balances are reset to zero, because the debts behind them are gone.
  * employees and suppliers (people, like customers)
  * the managed pick-lists (banks, damage types, expense categories ...)
  * the audit log. It is append-only by design, and it records this wipe.

There is NO backup and no undo. The view that calls this demands the admin's
password; this module only does the deleting.

Deletes go through each model's `_base_manager`, which ignores the branch
scoping and soft-delete filtering of the default managers - otherwise only
the current branch's live rows would go, and an archived product would stay
behind holding a PROTECT reference to a sale.
"""
from decimal import Decimal

from django.apps import apps
from django.db import transaction

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


def wipe_business_data() -> dict:
    """
    Delete everything in WIPE_ORDER across every branch, in one transaction.

    Returns {"Model": rows_deleted, ...} for the models that had rows.
    Either all of it goes or none of it does.
    """
    counts = {}
    with transaction.atomic():
        for app_label, model_name in WIPE_ORDER:
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                continue  # a model renamed or removed later - skip, don't fail
            deleted, _ = model._base_manager.all().delete()
            if deleted:
                counts[model_name] = deleted

        # Customers stay; what they owed does not.
        CreditAccount = apps.get_model("credit", "CreditAccount")
        CreditAccount._base_manager.all().update(
            total_credit_extended=Decimal("0"),
            total_repaid=Decimal("0"),
            outstanding_balance=Decimal("0"),
            last_purchase_date=None,
            last_payment_date=None,
        )
    return counts
