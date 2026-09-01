"""
Tests for the access system.

    python manage.py test core

WHAT THESE ARE FOR
------------------
Permission bugs are quiet. Nothing crashes when a sales assistant can suddenly
see cost prices, or when a manager's discount silently stops working - it just
becomes somebody's word against the screen weeks later. So the things worth
testing here are the ones that fail silently:

  1. the three shipped role matrices are what the blueprints say,
  2. a per-person grant or denial beats the role, and does not leak to a
     colleague who shares that role,
  3. a sales user sees their MANAGER's products but only their OWN sales,
  4. the sale service refuses credit and discounts without the permission,
     including when the request bypasses the form entirely,
  5. an administrator cannot strip their own way back into Access Control.

Each test names the rule it protects. If one fails, the message should tell
you which promise the system just stopped keeping.
"""
import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.template.loader import get_template
from django.test import Client, TestCase

from accounts.models import DataScope, RoleDefinition, User
from accounts.roles import BLUEPRINTS, ensure_system_roles
from core.access import apply_user_access, build_matrix, diff_against_role
from core.permissions import ALL_CODES, PAGE_PERMISSIONS, WILDCARD
from core.scoping import scoped
from credit.models import DebtRecord
from inventory.models import Category, Product
from sales.models import Customer
from sales.services import SaleError, create_sale


class AccessTestBase(TestCase):
    """One of each role, plus a shelf to sell from."""

    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()

        cls.admin = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.sales = User.objects.create_user(
            "sam", password="pw", role="SALES", manager=cls.manager
        )
        cls.sales2 = User.objects.create_user(
            "sara", password="pw", role="SALES", manager=cls.manager
        )

        cls.category = Category.objects.create(name="Drinks")
        cls.product = Product.objects.create(
            name="Cola", sku="C1", category=cls.category,
            cost_price=Decimal("10.00"), selling_price=Decimal("20.00"),
            stock_quantity=100, owner=cls.manager,
        )
        cls.admin_product = Product.objects.create(
            name="Rice", sku="R1", category=cls.category,
            cost_price=Decimal("50.00"), selling_price=Decimal("80.00"),
            stock_quantity=50, owner=cls.admin,
        )
        cls.customer = Customer.objects.create(
            name="Abebe", phone="0911", owner=cls.sales, is_credit_approved=True
        )
        cls.other_customer = Customer.objects.create(
            name="Bekele", phone="0922", owner=cls.sales2, is_credit_approved=True
        )

    def refresh(self, user):
        user.refresh_from_db()
        user.refresh_access()
        return user


class RoleDefaultsTests(AccessTestBase):
    def test_admin_holds_everything(self):
        self.assertTrue(
            self.admin.has_access("report.profit", "sale.void", "user.permissions")
        )
        self.assertEqual(self.admin.effective_permissions, ALL_CODES)

    def test_manager_controls_products_and_stock(self):
        self.assertTrue(
            self.manager.has_access(
                "product.create", "product.edit", "stock.restock",
                "stock.recount", "product.view_cost",
            )
        )

    def test_manager_cannot_see_profit_or_manage_staff(self):
        # The whole reason cost and profit are two permissions: a manager
        # buying stock needs the first and has no business with the second.
        self.assertTrue(self.manager.can_view_costs)
        self.assertFalse(self.manager.can_view_profit)
        self.assertFalse(self.manager.has_access("user.view"))
        self.assertFalse(self.manager.has_access("sale.void"))

    def test_sales_sells_but_does_not_stock(self):
        self.assertTrue(
            self.sales.has_access(
                "product.view", "sale.create", "sale.credit",
                "credit.collect", "customer.create",
            )
        )
        for denied in ("product.edit", "product.view_cost", "sale.discount",
                       "stock.restock", "report.profit"):
            self.assertFalse(
                self.sales.has_access(denied),
                f"a plain Sales user should not hold {denied}",
            )

    def test_scopes(self):
        self.assertEqual(self.admin.data_scope, DataScope.ALL)
        self.assertEqual(self.manager.data_scope, DataScope.TEAM)
        self.assertEqual(self.sales.data_scope, DataScope.OWN)

    def test_blueprints_only_name_real_permissions(self):
        for code, spec in BLUEPRINTS.items():
            for perm in spec["permissions"]:
                self.assertTrue(
                    perm == WILDCARD or perm in ALL_CODES,
                    f"role {code} names a permission that does not exist: {perm}",
                )


class PerPersonOverrideTests(AccessTestBase):
    def test_grant_and_denial_beat_the_role(self):
        role = RoleDefinition.objects.get(code="SALES")
        ticked = set(role.permission_set) | {"sale.discount"}
        ticked.discard("sale.credit")

        extra, denied = diff_against_role(role, ticked)
        self.assertEqual(extra, ["sale.discount"])
        self.assertEqual(denied, ["sale.credit"])

        apply_user_access(
            user=self.sales, role_code="SALES", ticked=ticked,
            manager=self.manager, editor=self.admin,
        )
        self.refresh(self.sales)
        self.assertTrue(self.sales.has_access("sale.discount"))
        self.assertFalse(self.sales.has_access("sale.credit"))

    def test_an_override_does_not_leak_to_a_colleague(self):
        apply_user_access(
            user=self.sales, role_code="SALES",
            ticked=set(RoleDefinition.objects.get(code="SALES").permission_set)
            | {"sale.discount"},
            manager=self.manager, editor=self.admin,
        )
        self.refresh(self.sales2)
        self.assertFalse(
            self.sales2.has_access("sale.discount"),
            "one person's grant must not follow their role to everyone else",
        )

    def test_matrix_labels_each_row_correctly(self):
        role = RoleDefinition.objects.get(code="SALES")
        matrix = build_matrix(
            role=role, extra=["sale.discount"], denied=["sale.credit"]
        )
        states = {
            row["code"]: row["state"]
            for group in matrix for row in group["permissions"]
        }
        self.assertEqual(states["sale.discount"], "granted")
        self.assertEqual(states["sale.credit"], "denied")
        self.assertEqual(states["sale.create"], "inherited")
        self.assertEqual(states["report.profit"], "absent")

    def test_editing_a_role_moves_everyone_who_holds_it(self):
        role = RoleDefinition.objects.get(code="SALES")
        role.permissions = list(role.permissions) + ["report.inventory"]
        role.save()
        self.refresh(self.sales)
        self.refresh(self.sales2)
        self.assertTrue(self.sales.has_access("report.inventory"))
        self.assertTrue(self.sales2.has_access("report.inventory"))

    def test_a_denial_survives_the_role_gaining_it(self):
        # The point of storing differences rather than a flat copy: an
        # exception somebody made deliberately is not undone by a later role
        # edit that happens to grant the same thing.
        apply_user_access(
            user=self.sales, role_code="SALES",
            ticked=set(RoleDefinition.objects.get(code="SALES").permission_set)
            - {"credit.collect"},
            manager=self.manager, editor=self.admin,
        )
        role = RoleDefinition.objects.get(code="SALES")
        role.permissions = list(set(role.permissions) | {"credit.collect"})
        role.save()

        self.refresh(self.sales)
        self.assertFalse(self.sales.has_access("credit.collect"))
        self.refresh(self.sales2)
        self.assertTrue(self.sales2.has_access("credit.collect"))


class ScopingTests(AccessTestBase):
    def test_sales_sees_the_managers_shelf(self):
        visible = set(
            scoped(Product.objects.all(), self.sales).values_list("sku", flat=True)
        )
        self.assertEqual(visible, {"C1"}, "a sales user sells their manager's stock")

    def test_sales_does_not_see_another_managers_shelf(self):
        visible = set(
            scoped(Product.objects.all(), self.sales).values_list("sku", flat=True)
        )
        self.assertNotIn("R1", visible)

    def test_ledger_stays_private(self):
        mine = set(
            scoped(Customer.objects.all(), self.sales).values_list("name", flat=True)
        )
        self.assertEqual(mine, {"Abebe"})

    def test_manager_sees_the_whole_team(self):
        theirs = set(
            scoped(Customer.objects.all(), self.manager).values_list("name", flat=True)
        )
        self.assertEqual(theirs, {"Abebe", "Bekele"})

    def test_admin_sees_everything(self):
        self.assertEqual(scoped(Product.objects.all(), self.admin).count(), 2)


class SaleServiceTests(AccessTestBase):
    def cart(self, qty=1, discount="0.00"):
        return [{
            "product": self.product,
            "quantity": qty,
            "unit_price": Decimal("20.00"),
            "line_discount": Decimal(discount),
        }]

    def test_sales_user_sells_the_managers_stock(self):
        txn = create_sale(
            user=self.sales, cart=self.cart(2),
            customer=self.customer, amount_paid=Decimal("40.00"),
        )
        self.product.refresh_from_db()
        self.assertEqual(txn.owner_id, self.sales.pk, "the sale belongs to the seller")
        self.assertEqual(self.product.stock_quantity, 98,
                         "stock comes off the manager's product")

    def test_credit_refused_without_the_permission(self):
        apply_user_access(
            user=self.sales, role_code="SALES",
            ticked=set(RoleDefinition.objects.get(code="SALES").permission_set)
            - {"sale.credit"},
            manager=self.manager, editor=self.admin,
        )
        self.refresh(self.sales)
        with self.assertRaises(SaleError) as ctx:
            create_sale(user=self.sales, cart=self.cart(),
                        customer=self.customer, amount_paid=Decimal("0.00"))
        self.assertIn("permission to sell on credit", str(ctx.exception))

    def test_discount_refused_without_the_permission(self):
        # Enforced in the service, not just the form - a hand-crafted request
        # that skipped the form would otherwise give money away.
        with self.assertRaises(SaleError) as ctx:
            create_sale(user=self.sales, cart=self.cart(discount="5.00"),
                        customer=self.customer, amount_paid=Decimal("15.00"))
        self.assertIn("permission to apply a discount", str(ctx.exception))

    def test_debt_belongs_to_the_seller_and_the_manager_can_see_it(self):
        txn = create_sale(user=self.sales, cart=self.cart(),
                          customer=self.customer, amount_paid=Decimal("0.00"))
        debt = DebtRecord.objects.get(transaction=txn)
        self.assertEqual(debt.owner_id, self.sales.pk,
                         "a credit sale is under the seller's own obligation")
        self.assertTrue(
            scoped(DebtRecord.objects.all(), self.manager).filter(pk=debt.pk).exists()
        )
        self.assertFalse(
            scoped(DebtRecord.objects.all(), self.sales2).filter(pk=debt.pk).exists()
        )


class AccessScreenTests(AccessTestBase):
    """The Settings hub, as the browser sees it."""

    def setUp(self):
        self.client = Client()

    def test_manager_cannot_reach_access_control(self):
        # Refusal lands on the 403 page, which answers with a 403 status - the
        # page is a refusal, not a normal page that happens to say "no".
        self.client.force_login(self.manager)
        response = self.client.get("/system/access/", follow=True)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.redirect_chain[-1][0], "/system/forbidden/")

    def test_admin_can_edit_someone(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/system/access/{self.sales.pk}/",
            {
                "role": "SALES",
                "manager": self.manager.pk,
                "data_scope_override": "",
                "perm": ["dashboard.view", "sale.view", "sale.create",
                         "product.view", "sale.discount"],
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.refresh(self.sales)
        self.assertTrue(self.sales.has_access("sale.discount"))
        self.assertFalse(self.sales.has_access("credit.collect"))

    def test_admin_cannot_lock_themselves_out(self):
        # The one mistake that could not be undone from inside the app.
        self.client.force_login(self.admin)
        self.client.post(
            f"/system/access/{self.admin.pk}/",
            {"role": "ADMIN", "manager": "", "data_scope_override": "",
             "perm": ["dashboard.view"]},
            follow=True,
        )
        self.refresh(self.admin)
        self.assertTrue(
            self.admin.has_access("user.permissions", "settings.view"),
            "an administrator must keep the permissions that reach this screen",
        )

    def test_last_administrator_cannot_be_demoted(self):
        self.client.force_login(self.admin)
        self.client.post(
            f"/system/access/{self.admin.pk}/",
            {"role": "MANAGER", "manager": "", "data_scope_override": "",
             "perm": ["dashboard.view"]},
            follow=True,
        )
        self.refresh(self.admin)
        self.assertEqual(self.admin.role, "ADMIN")

    def test_sidebar_hides_what_the_user_cannot_open(self):
        self.client.force_login(self.sales)
        html = self.client.get("/reports/").content.decode()
        self.assertNotIn("/system/settings/", html)
        self.assertNotIn("/inventory/categories/", html)
        self.assertIn("/sales/new/", html)


class TemplateHygieneTests(TestCase):
    """
    Guards against a whole class of silent template bug.

    Django's hash-style comment is SINGLE-LINE ONLY. Spread one over several
    lines and it stops being a comment: the text renders into the page. In the
    sidebar that landed the prose as the first child of the flex container that
    lays out the whole app, so every screen shifted sideways and the content
    scrolled off to the right - a wall of explanation where the dashboard
    should have been, on every page at once.

    Nothing raised. Nothing logged. It just looked broken. So it gets a test.
    """

    def template_files(self):
        for directory in settings.TEMPLATES[0]["DIRS"]:
            yield from sorted(Path(directory).rglob("*.html"))

    def test_no_multiline_hash_comments(self):
        offenders = []
        for path in self.template_files():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                for match in re.finditer(r"\{#", line):
                    if "#}" not in line[match.end():]:
                        offenders.append(f"{path.name}:{number}")
        self.assertEqual(
            offenders, [],
            "a {# comment #} must open and close on ONE line - use "
            "{% comment %}...{% endcomment %} for anything longer:\n  "
            + "\n  ".join(offenders),
        )

    def test_every_template_compiles(self):
        for path in self.template_files():
            for directory in settings.TEMPLATES[0]["DIRS"]:
                try:
                    name = str(path.relative_to(directory)).replace("\\", "/")
                except ValueError:
                    continue
                with self.subTest(template=name):
                    get_template(name)


class RenderedOutputTests(AccessTestBase):
    """Every page a user can open renders finished HTML, not template source."""

    #: Pages that need no URL arguments, plus the ones the sidebar links to.
    #: Derived from PAGE_PERMISSIONS rather than typed out, so a route that is
    #: renamed or added cannot quietly fall out of this test - a hard-coded
    #: list would just 404 and, if the test tolerated that, prove nothing.
    EXTRA_PAGES = [
        "/accounts/profile/",
        "/inventory/suppliers/",
        "/system/settings/business/",
        "/system/roles/new/",
    ]

    def pages(self):
        from django.urls import NoReverseMatch, reverse

        seen = []
        for name in PAGE_PERMISSIONS:
            try:
                seen.append(reverse(name))
            except NoReverseMatch:
                # A detail route that needs an object id - covered separately.
                continue
        return seen + self.EXTRA_PAGES

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.admin)

    def test_no_template_source_leaks_into_any_page(self):
        for path in self.pages():
            with self.subTest(page=path):
                response = self.client.get(path, follow=True)
                # An administrator holds every permission, so anything other
                # than 200 here is a broken route, not a refusal. Skipping it
                # is how a typo'd path sits in a test suite proving nothing.
                self.assertEqual(
                    response.status_code, 200,
                    f"{path} did not render for an administrator",
                )
                html = response.content.decode()
                for marker in ("{#", "#}", "{%", "%}", "{{"):
                    self.assertNotIn(
                        marker, html,
                        f"{path} leaked raw template syntax ({marker}) into the page",
                    )

    def test_the_dashboard_actually_renders_its_content(self):
        # The failure this catches did not 500 - it returned 200 with the
        # layout wrecked, which is why "the page loaded" is not enough.
        html = self.client.get("/reports/").content.decode()
        self.assertIn("Today's Revenue", html)
        self.assertIn('class="stat-card', html)
        self.assertIn("sidebar-footer", html)

    def test_the_access_grid_actually_renders(self):
        html = self.client.get(
            f"/system/access/{self.sales.pk}/"
        ).content.decode()
        self.assertIn("perm-matrix", html)
        self.assertIn('name="perm"', html)


class AccessApiTests(AccessTestBase):
    """The same rules, over the API the phone uses."""

    def setUp(self):
        self.client = Client()

    def test_me_carries_the_permission_codes(self):
        self.client.force_login(self.sales)
        data = self.client.get("/api/auth/me/").json()
        self.assertIn("codes", data["permissions"])
        self.assertIn("sale.create", data["permissions"]["codes"])
        self.assertNotIn("product.view_cost", data["permissions"]["codes"])
        self.assertEqual(data["permissions"]["manager"], self.manager.display_name)

    def test_cost_and_profit_are_stripped_per_permission(self):
        self.client.force_login(self.manager)
        row = self.client.get("/api/products/").json()["results"][0]
        self.assertIn("cost_price", row, "a manager buys the stock")
        self.assertNotIn("margin_percent", row, "and is not shown the margin")

        self.client.force_login(self.sales)
        row = self.client.get("/api/products/").json()["results"][0]
        self.assertNotIn("cost_price", row)

    def test_access_endpoint_is_restricted(self):
        self.client.force_login(self.sales)
        self.assertEqual(
            self.client.get(f"/api/users/{self.sales2.pk}/access/").status_code, 403
        )

    def test_admin_can_set_access_over_the_api(self):
        self.client.force_login(self.admin)
        response = self.client.put(
            f"/api/users/{self.sales.pk}/access/",
            data={
                "role": "SALES",
                "manager": self.manager.pk,
                "permissions": ["dashboard.view", "sale.view", "credit.collect",
                                "not.a.real.permission"],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.refresh(self.sales)
        self.assertTrue(self.sales.has_access("credit.collect"))
        self.assertNotIn("not.a.real.permission", self.sales.effective_permissions)

    def test_roles_endpoint_needs_role_manage(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get("/api/roles/").status_code, 403)
        self.client.force_login(self.admin)
        codes = {r["code"] for r in self.client.get("/api/roles/").json()}
        self.assertTrue({"ADMIN", "MANAGER", "SALES"} <= codes)
