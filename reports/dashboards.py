"""
What the dashboard looks like for one particular person.

WHY THIS FILE EXISTS
--------------------
The first version of the dashboard was one fixed layout with fallbacks: slot
three showed stock value, or product count, or customer count, depending on
what the viewer was allowed to see. It never broke, but it never felt built
for anyone either - a sales assistant got the owner's layout with the
interesting numbers swapped out for consolation prizes.

So the layout is chosen instead. Four shapes:

    owner    Sees the whole business and its margins. Wants money in, money
             made, money tied up in stock, money late.
    stock    Runs the shelf. Wants what is running out, what it is worth, and
             how the people reporting to them are doing.
    counter  Sells. Wants today's takings, this month's takings, what their
             own customers still owe them, and a big button to sell again.
    viewer   Everyone else - a read-only auditor, a custom role nobody
             anticipated. Gets whatever they can actually see, in a sensible
             order, and never an empty panel.

CHOSEN BY PERMISSION, NOT BY ROLE NAME
--------------------------------------
`profile_for()` reads permissions and data scope, never `user.role`. A custom
role called "Shop Supervisor" that can restock gets the stock layout without
anybody having to add it to a list here, and an administrator who deliberately
stripped their own profit permission stops seeing profit panels. Role strings
are labels; permissions are facts.

EVERY CARD DECLARES ITS PERMISSION
----------------------------------
A card whose `needs` is not held is dropped before the template sees it, so
the template never has to ask a second time and a new card cannot leak a
figure by being added to the wrong list.
"""
from dataclasses import dataclass, field

from core.templatetags.core_extras import money as fmt_money


def plural(count: int, word: str, suffix: str = "s") -> str:
    """
    "1 sale" / "2 sales", the way Django's own `pluralize` filter would.

    Not cosmetic. The browser translates this page by matching the rendered
    sentence against a template whose "(s)" means "an optional s", so a hint
    that literally reads "2 sale(s)" matches no template and stays in English
    on an otherwise Amharic screen.
    """
    return f"{count} {word}{'' if count == 1 else suffix}"


OWNER = "owner"
STOCK = "stock"
COUNTER = "counter"
VIEWER = "viewer"


@dataclass
class Card:
    """One headline tile."""

    key: str
    label: str
    value: object
    hint: str = ""
    accent: str = "primary"
    icon: str = "bi-graph-up"
    url: str = ""          # url NAME, resolved in the template with {% url %}
    is_money: bool = True
    restricted: bool = False   # shows the "Restricted" pill
    #: Permission codes the viewer must hold for this card to be built.
    needs: tuple = ()


@dataclass
class Panel:
    """One of the larger cards below the headline row."""

    key: str
    template: str
    col: int = 6
    needs: tuple = field(default_factory=tuple)


#: How each layout describes itself, in one line, at the top of the page.
#: Worth the space: a sales user seeing smaller numbers than the owner needs
#: to know that is the design and not a bug.
BLURBS = {
    OWNER: (
        "Every sale, every debt and every product in the business, including "
        "what the stock cost and what it earns."
    ),
    STOCK: (
        "The shelf you control and the people who sell from it. Your own "
        "records plus your team's."
    ),
    COUNTER: (
        "Your counter: what you sold, what your customers owe you, and what "
        "you have collected. Nobody else's figures appear here."
    ),
    VIEWER: "The records you have been given access to.",
}

TITLES = {
    OWNER: "Business overview",
    STOCK: "Stock and team",
    COUNTER: "My sales",
    VIEWER: "Overview",
}


def profile_for(user) -> str:
    """
    Pick a layout from what this person may actually do.

    Order matters and is from the top down: an administrator can also restock
    and also sell, so the widest match has to win. A person who can do none of
    these still gets a working page rather than a blank one.
    """
    if user.data_scope == "ALL" and user.can_view_profit:
        return OWNER
    if user.has_any_access("stock.restock", "stock.adjust", "product.edit"):
        return STOCK
    if user.has_access("sale.create"):
        return COUNTER
    return VIEWER


def _keep(cards, user):
    """Drop every card whose permissions the viewer does not hold."""
    return [c for c in cards if not c.needs or user.has_access(*c.needs)]


def build_cards(user, ctx) -> list:
    """
    The headline row for this person, already filtered.

    `ctx` is the figures the view has already gathered - this function does no
    database work of its own, so adding a card can never add a query.
    """
    profile = ctx["profile"]
    month = ctx["month_stats"]
    today = ctx["today_stats"]
    receivables = ctx["receivables"]
    valuation = ctx.get("valuation") or {}
    month_profit = ctx.get("month_profit") or {}
    month_collections = ctx.get("month_collections") or {}

    if profile == OWNER:
        cards = [
            Card(
                key="month_revenue",
                label="Month Revenue",
                value=month["revenue"],
                hint=plural(month["count"], "transaction"),
                accent="primary",
                icon="bi-cash-coin",
                url="reports:sales_report",
            ),
            Card(
                key="month_profit",
                label="Month Gross Profit",
                value=month_profit.get("gross_profit", 0),
                hint=(
                    f"{month_profit.get('margin_percent', 0)}% margin "
                    f"· COGS {fmt_money(month_profit.get('cogs', 0))}"
                ),
                accent="success",
                icon="bi-graph-up-arrow",
                url="reports:profit_report",
                restricted=True,
                needs=("report.profit",),
            ),
            Card(
                key="stock_value",
                label="Stock Value (Cost)",
                value=valuation.get("cost_value", 0),
                hint=(
                    plural(valuation.get("units", 0), "unit")
                    + " · "
                    + plural(valuation.get("skus", 0), "SKU")
                ),
                accent="info",
                icon="bi-box-seam",
                url="reports:inventory_report",
                needs=("product.view_cost",),
            ),
            Card(
                key="overdue",
                label="Overdue",
                value=receivables["overdue_amount"],
                hint=f"{receivables['overdue_count']} past due date",
                accent="danger",
                icon="bi-exclamation-triangle",
                url="reports:receivables_report",
                needs=("credit.view",),
            ),
        ]

    elif profile == STOCK:
        cards = [
            Card(
                key="stock_value",
                label="Stock Value (Cost)",
                value=valuation.get("cost_value", 0),
                hint=(
                    plural(valuation.get("units", 0), "unit")
                    + " · "
                    + plural(valuation.get("skus", 0), "SKU")
                ),
                accent="primary",
                icon="bi-boxes",
                url="reports:inventory_report",
                needs=("product.view_cost",),
            ),
            Card(
                key="low_stock",
                label="Needs Restocking",
                value=ctx["low_stock_count"],
                hint=plural(ctx["product_count"], "product") + " in catalogue",
                accent="warning",
                icon="bi-arrow-repeat",
                url="inventory:low_stock",
                is_money=False,
                needs=("product.view",),
            ),
            Card(
                key="month_revenue",
                label="Month Revenue",
                value=month["revenue"],
                hint=plural(month["count"], "transaction"),
                accent="info",
                icon="bi-cash-coin",
                url="reports:sales_report",
                needs=("sale.view",),
            ),
            Card(
                key="receivables",
                label="Total Receivables",
                value=receivables["outstanding"],
                hint=plural(receivables["debt_count"], "open debt"),
                accent="danger",
                icon="bi-cash-stack",
                url="credit:debt_list",
                needs=("credit.view",),
            ),
        ]

    elif profile == COUNTER:
        cards = [
            Card(
                key="today_revenue",
                label="I Sold Today",
                value=today["revenue"],
                hint=plural(today["count"], "sale") + " recorded",
                accent="primary",
                icon="bi-bag-check",
            ),
            Card(
                key="month_revenue",
                label="I Sold This Month",
                value=month["revenue"],
                hint=plural(month["count"], "transaction"),
                accent="info",
                icon="bi-calendar-check",
                url="sales:transaction_list",
            ),
            Card(
                key="collected",
                label="Cash I Collected",
                value=month["collected"],
                hint=(
                    f"{fmt_money(month_collections.get('collected', 0))} "
                    "off older debts"
                ),
                accent="success",
                icon="bi-wallet2",
            ),
            Card(
                key="owed_to_me",
                label="Owed To Me",
                value=receivables["outstanding"],
                hint=plural(receivables["debt_count"], "open debt"),
                accent="warning",
                icon="bi-hourglass-split",
                url="credit:debt_list",
                needs=("credit.view",),
            ),
        ]

    else:  # VIEWER
        cards = [
            Card(
                key="month_revenue",
                label="Month Revenue",
                value=month["revenue"],
                hint=plural(month["count"], "transaction"),
                accent="primary",
                icon="bi-cash-coin",
                needs=("sale.view",),
            ),
            Card(
                key="receivables",
                label="Total Receivables",
                value=receivables["outstanding"],
                hint=plural(receivables["debt_count"], "open debt"),
                accent="warning",
                icon="bi-cash-stack",
                needs=("credit.view",),
            ),
            Card(
                key="products",
                label="Active Products",
                value=ctx["product_count"],
                hint=f"{ctx['low_stock_count']} need restocking",
                accent="info",
                icon="bi-box-seam",
                is_money=False,
                needs=("product.view",),
            ),
            Card(
                key="customers",
                label="Customers",
                value=ctx["customer_count"],
                hint=f"{ctx['debtor_count']} currently owe money",
                accent="success",
                icon="bi-people",
                is_money=False,
                needs=("customer.view",),
            ),
        ]

    kept = _keep(cards, user)

    # A layout can lose a card to a missing permission - a manager with no
    # cost access loses the valuation tile. Rather than leave a gap in a
    # four-across row, top the row back up from a small pool of things
    # everybody with the relevant permission can see.
    spares = [
        Card(
            key="average_sale",
            label="Average Sale",
            value=month["average_sale"],
            hint="This month",
            accent="secondary",
            icon="bi-rulers",
            needs=("sale.view",),
        ),
        Card(
            key="products",
            label="Active Products",
            value=ctx["product_count"],
            hint=f"{ctx['low_stock_count']} need restocking",
            accent="info",
            icon="bi-box-seam",
            is_money=False,
            needs=("product.view",),
        ),
        Card(
            key="customers",
            label="Customers",
            value=ctx["customer_count"],
            hint=f"{ctx['debtor_count']} currently owe money",
            accent="secondary",
            icon="bi-people",
            is_money=False,
            needs=("customer.view",),
        ),
        Card(
            key="collected_today",
            label="Collected Today",
            value=today["collected"],
            hint="Cash at the till",
            accent="success",
            icon="bi-wallet2",
            needs=("sale.view",),
        ),
    ]
    used = {c.key for c in kept}
    for spare in _keep(spares, user):
        if len(kept) >= 4:
            break
        if spare.key not in used:
            kept.append(spare)
            used.add(spare.key)

    return kept[:4]


#: The larger panels, in the order each layout wants them. The template loops
#: over this, so re-ordering a dashboard is a one-line change here rather than
#: a rearrangement of two hundred lines of HTML.
PANEL_ORDER = {
    OWNER: [
        Panel("chart", "reports/panels/chart.html", col=8, needs=("sale.view",)),
        Panel("aging", "reports/panels/aging.html", col=4, needs=("credit.view",)),
        Panel("team", "reports/panels/team.html", col=12),
        Panel("recent_sales", "reports/panels/recent_sales.html", col=7,
              needs=("sale.view",)),
        Panel("overdue", "reports/panels/overdue.html", col=5,
              needs=("credit.view",)),
        Panel("low_stock", "reports/panels/low_stock.html", col=12,
              needs=("product.view",)),
    ],
    STOCK: [
        Panel("low_stock", "reports/panels/low_stock.html", col=12,
              needs=("product.view",)),
        Panel("chart", "reports/panels/chart.html", col=8, needs=("sale.view",)),
        Panel("aging", "reports/panels/aging.html", col=4, needs=("credit.view",)),
        Panel("team", "reports/panels/team.html", col=12),
        Panel("recent_sales", "reports/panels/recent_sales.html", col=7,
              needs=("sale.view",)),
        Panel("overdue", "reports/panels/overdue.html", col=5,
              needs=("credit.view",)),
    ],
    COUNTER: [
        Panel("quick_actions", "reports/panels/quick_actions.html", col=4),
        Panel("chart", "reports/panels/chart.html", col=8, needs=("sale.view",)),
        Panel("recent_sales", "reports/panels/recent_sales.html", col=7,
              needs=("sale.view",)),
        Panel("overdue", "reports/panels/overdue.html", col=5,
              needs=("credit.view",)),
        Panel("my_customers", "reports/panels/my_customers.html", col=12,
              needs=("customer.view",)),
    ],
    VIEWER: [
        Panel("chart", "reports/panels/chart.html", col=8, needs=("sale.view",)),
        Panel("aging", "reports/panels/aging.html", col=4, needs=("credit.view",)),
        Panel("recent_sales", "reports/panels/recent_sales.html", col=12,
              needs=("sale.view",)),
    ],
}


def build_panels(user, profile: str, ctx) -> list:
    """Panels this person may see, in this layout's order."""
    panels = []
    for panel in PANEL_ORDER.get(profile, PANEL_ORDER[VIEWER]):
        if panel.needs and not user.has_access(*panel.needs):
            continue
        # The team table is the one panel that is pointless rather than
        # forbidden: somebody scoped to their own records would see a
        # one-row table containing their own total a second time.
        if panel.key == "team" and not ctx.get("by_manager"):
            continue
        panels.append(panel)
    return panels
