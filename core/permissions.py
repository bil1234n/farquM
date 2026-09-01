"""
THE PERMISSION CATALOGUE - the single source of truth for "who may do what".

HOW THE ACCESS SYSTEM FITS TOGETHER
-----------------------------------
    core/permissions.py   <- this file. Declares every permission that exists.
    accounts.RoleDefinition
                          <- a named bundle of those codes + a data scope.
                             ADMIN / MANAGER / SALES ship as system roles;
                             an administrator may add more.
    accounts.User         <- points at one role, and may carry per-person
                             `extra_permissions` (granted on top) and
                             `denied_permissions` (taken away).
    core/mixins.py        <- enforces it on web views.
    api/permissions.py    <- enforces it on the API.
    core/scoping.py       <- separate axis: WHOSE records you may see.

    effective = (role.permissions | user.extra) - user.denied
    ...unless the set contains WILDCARD, which means "everything".

WHY A HAND-WRITTEN CATALOGUE RATHER THAN django.contrib.auth PERMISSIONS
------------------------------------------------------------------------
Django's built-in permissions are per-model CRUD (add/change/delete/view
product). Real shop rules are not shaped like that. "May sell on credit",
"may see cost prices", "may overwrite a stock count" and "may reverse a
payment" are all operations on the same two models, and they carry wildly
different amounts of trust. Modelling them as model CRUD would either collapse
distinctions that matter or produce permissions nobody can interpret.

So the catalogue is written in the language of the business, and the checkbox
grid an administrator sees is generated straight from it. Add a Perm here and
it appears in the UI, in the API, and in the role editor - with no other
change.

RULES FOR ADDING A PERMISSION
-----------------------------
1. The code is `<area>.<verb>` in lower snake case. Never rename one: codes
   are stored in the database on every role and user.
2. Write `label` as something an administrator ticking a box would say out
   loud. It is UI text, not a symbol.
3. Mark `sensitive=True` when getting it wrong loses money or hides evidence -
   voids, write-offs, payment reversals, cost prices, permission granting.
   The UI tints those rows and warns before a bulk grant.
4. Enforce it somewhere. An unenforced permission is a lie told in a checkbox.
"""
from dataclasses import dataclass, field

# A role holding this holds everything, now and in future. Only the ADMIN role
# should have it: a wildcard means new permissions are granted automatically
# the day they are invented, which is right for the owner and wrong for
# everyone else.
WILDCARD = "*"


@dataclass(frozen=True)
class Perm:
    code: str
    label: str
    help: str = ""
    #: Money, evidence, or access itself. Rendered with a warning tint.
    sensitive: bool = False


@dataclass(frozen=True)
class PermGroup:
    key: str
    label: str
    icon: str
    blurb: str
    perms: tuple[Perm, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
CATALOG: tuple[PermGroup, ...] = (
    PermGroup(
        key="overview",
        label="Overview",
        icon="bi-grid-1x2",
        blurb="The landing page everyone sees after signing in.",
        perms=(
            Perm("dashboard.view", "Open the dashboard",
                 "Without this the user lands on their profile page instead."),
        ),
    ),
    PermGroup(
        key="catalog",
        label="Products & Stock",
        icon="bi-box-seam",
        blurb="The shelf: what exists, what it costs, and how much is left.",
        perms=(
            Perm("product.view", "See the product list",
                 "Needed to sell anything - the till lists products."),
            Perm("product.create", "Add new products"),
            Perm("product.edit", "Edit product details and prices"),
            Perm("product.archive", "Archive (soft-delete) a product",
                 "History is kept; the product stops appearing at the till.",
                 sensitive=True),
            Perm("product.view_cost", "See cost prices and stock value",
                 "What you paid, not what you charge. Hidden from a sales "
                 "user so a customer glancing at the screen cannot see it.",
                 sensitive=True),
            Perm("stock.view_movements", "See the stock movement history"),
            Perm("stock.restock", "Receive new stock",
                 "Adds units and records the delivery."),
            Perm("stock.adjust", "Record damage and customer returns"),
            Perm("stock.recount", "Overwrite a counted stock quantity",
                 "A stock-take that sets the number outright. This is how "
                 "shrinkage gets hidden, so keep it narrow.",
                 sensitive=True),
            Perm("catalog.manage", "Manage categories and suppliers"),
        ),
    ),
    PermGroup(
        key="sales",
        label="Sales",
        icon="bi-cart",
        blurb="The till, and everything that has already gone through it.",
        perms=(
            Perm("sale.view", "See sales and transaction history"),
            Perm("sale.create", "Record a sale"),
            Perm("sale.credit", "Sell on credit (leave a balance owing)",
                 "Turns a sale into a debt the customer owes. The debt is "
                 "recorded against whoever made the sale."),
            Perm("sale.discount", "Apply a discount to a sale",
                 "A discount is money off the top - grant it deliberately.",
                 sensitive=True),
            Perm("sale.void", "Void a sale",
                 "Reverses stock and cancels any linked debt. Rewrites the "
                 "day's takings.",
                 sensitive=True),
            Perm("sale.receipt.add", "Attach receipts and proof of payment"),
            Perm("sale.receipt.delete", "Delete an attached receipt",
                 "Removing proof of a payment. Kept with the people "
                 "accountable for the books.",
                 sensitive=True),
        ),
    ),
    PermGroup(
        key="customers",
        label="Customers",
        icon="bi-people",
        blurb="The customer book. Each user builds their own.",
        perms=(
            Perm("customer.view", "See the customer list"),
            Perm("customer.create", "Register a new customer"),
            Perm("customer.edit", "Edit customer details"),
        ),
    ),
    PermGroup(
        key="credit",
        label="Credit & Borrowers",
        icon="bi-cash-stack",
        blurb="Who owes what, how late they are, and who may forgive it.",
        perms=(
            Perm("credit.view", "See borrowers, debts and the aging report"),
            Perm("credit.collect", "Record a repayment"),
            Perm("credit.reschedule", "Change a debt's due date"),
            Perm("credit.limits", "Set credit limits and block borrowers",
                 "Decides how deep a customer may go. A financial control.",
                 sensitive=True),
            Perm("credit.write_off", "Write off a debt as uncollectable",
                 "Money the business stops chasing.",
                 sensitive=True),
            Perm("credit.reverse_payment", "Reverse a recorded payment",
                 "Un-does a receipt. The other way cash goes missing on paper.",
                 sensitive=True),
        ),
    ),
    PermGroup(
        key="reports",
        label="Reports",
        icon="bi-graph-up",
        blurb="Read-only summaries. Splitting these is how you let someone "
              "run the shop without showing them the margins.",
        perms=(
            Perm("report.sales", "Sales report"),
            Perm("report.inventory", "Inventory report"),
            Perm("report.receivables", "Receivables report"),
            Perm("report.profit", "Profit & margins",
                 "Cost of goods, gross profit, margin percentages. The most "
                 "commercially sensitive screen in the system.",
                 sensitive=True),
            Perm("report.export", "Export reports to CSV",
                 "A file that leaves the building. Cost and profit columns "
                 "are only written for someone who may see them."),
        ),
    ),
    PermGroup(
        key="admin",
        label="Administration",
        icon="bi-shield-lock",
        blurb="Control of the system itself. Grant sparingly.",
        perms=(
            Perm("user.view", "See the staff list"),
            Perm("user.create", "Add staff accounts"),
            Perm("user.edit", "Edit staff details"),
            Perm("user.deactivate", "Activate and deactivate accounts",
                 sensitive=True),
            Perm("user.reset_password", "Reset another user's password",
                 "Also signs that user out of every device.",
                 sensitive=True),
            Perm("user.permissions", "Grant and revoke access",
                 "Whoever holds this can give themselves anything else. "
                 "Treat it as equivalent to full control.",
                 sensitive=True),
            Perm("role.manage", "Create and edit roles",
                 "Changes what a whole group of people can do at once.",
                 sensitive=True),
            Perm("settings.view", "Open system settings"),
            Perm("settings.edit", "Change system settings",
                 sensitive=True),
            Perm("audit.view", "Read the audit log",
                 "Everyone's actions, not just their own."),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Flat lookups, built once at import
# ---------------------------------------------------------------------------
ALL_PERMS: tuple[Perm, ...] = tuple(p for g in CATALOG for p in g.perms)
ALL_CODES: frozenset[str] = frozenset(p.code for p in ALL_PERMS)
PERM_BY_CODE: dict[str, Perm] = {p.code: p for p in ALL_PERMS}
GROUP_BY_CODE: dict[str, PermGroup] = {
    p.code: g for g in CATALOG for p in g.perms
}
SENSITIVE_CODES: frozenset[str] = frozenset(
    p.code for p in ALL_PERMS if p.sensitive
)


def label_for(code: str) -> str:
    """Human label for a code, falling back to the code itself."""
    if code == WILDCARD:
        return "Full access to everything"
    perm = PERM_BY_CODE.get(code)
    return perm.label if perm else code


def clean_codes(codes) -> list[str]:
    """
    Keep only codes this build knows about, de-duplicated, in catalogue order.

    Stored permission lists outlive the code that wrote them: a role saved by
    an older deployment can name a permission that has since been removed.
    Filtering on the way out means a stale code is inert rather than a
    KeyError halfway through rendering a page, and re-saving quietly drops it.
    """
    if not codes:
        return []
    wanted = {str(c).strip() for c in codes if str(c).strip()}
    if WILDCARD in wanted:
        return [WILDCARD]
    return [p.code for p in ALL_PERMS if p.code in wanted]


def expand(codes) -> frozenset[str]:
    """Resolve a stored list into the concrete set it grants."""
    codes = set(codes or ())
    if WILDCARD in codes:
        return ALL_CODES
    return frozenset(codes & ALL_CODES)


# ---------------------------------------------------------------------------
# Page map - what each screen requires
# ---------------------------------------------------------------------------
# Used by the sidebar to hide links the user cannot follow, and by
# core.mixins as the default requirement for a view that does not name one.
#
# Hiding a link is a courtesy, not a control. Every entry here is also
# enforced in the view itself; a user who types the URL still gets stopped.
PAGE_PERMISSIONS: dict[str, str] = {
    "reports:dashboard": "dashboard.view",
    "sales:sale_create": "sale.create",
    "sales:transaction_list": "sale.view",
    "sales:transaction_detail": "sale.view",
    "sales:customer_list": "customer.view",
    "sales:customer_detail": "customer.view",
    "credit:dashboard": "credit.view",
    "credit:borrower_list": "credit.view",
    "credit:debt_list": "credit.view",
    "credit:aging_report": "credit.view",
    "inventory:product_list": "product.view",
    "inventory:low_stock": "product.view",
    "inventory:stock_movements": "stock.view_movements",
    "inventory:category_list": "catalog.manage",
    "inventory:supplier_list": "catalog.manage",
    "reports:sales_report": "report.sales",
    "reports:inventory_report": "report.inventory",
    "reports:receivables_report": "report.receivables",
    "reports:profit_report": "report.profit",
    "accounts:user_list": "user.view",
    "accounts:audit_log": "audit.view",
    "core:settings": "settings.view",
    "core:access_list": "user.permissions",
    "core:role_list": "role.manage",
}


def catalog_as_dict() -> list[dict]:
    """JSON-serialisable catalogue, for the API and the mobile matrix UI."""
    return [
        {
            "key": g.key,
            "label": g.label,
            "icon": g.icon,
            "blurb": g.blurb,
            "permissions": [
                {
                    "code": p.code,
                    "label": p.label,
                    "help": p.help,
                    "sensitive": p.sensitive,
                }
                for p in g.perms
            ],
        }
        for g in CATALOG
    ]
