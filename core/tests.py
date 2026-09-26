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
  3. everyone shares ONE catalogue while sales, customers and debts stay
     private to whoever made them,
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
from django.test import Client, SimpleTestCase, TestCase

from accounts.models import (
    DataScope,
    RegistrationPasscode,
    RoleDefinition,
    User,
)
from accounts.registration import (
    RegistrationError,
    available_roles,
    register_user,
)
from accounts.roles import BLUEPRINTS, ensure_system_roles
from core.access import apply_user_access, build_matrix, diff_against_role
from core.permissions import (
    ALL_CODES,
    PAGE_PERMISSIONS,
    WILDCARD,
    catalog_as_dict,
    translation_pairs,
)
from core.permissions_am import AMHARIC
from reports.dashboards import profile_for
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
    """
    One catalogue, many private ledgers.

    These two halves are the whole design, and they pull in opposite
    directions, so each is asserted separately and named for the promise it
    keeps. Loosening the first must never loosen the second.
    """

    # -- The shared half ---------------------------------------------------
    def test_everyone_sees_the_whole_catalogue(self):
        """
        A product is a fact about the business, not about who typed it in.

        C1 was entered by a manager and R1 by the owner. A salesperson opening
        the till sees both, because there is one shelf.
        """
        for who in (self.sales, self.sales2, self.manager, self.admin):
            with self.subTest(user=who.username):
                visible = set(
                    scoped(Product.objects.all(), who)
                    .values_list("sku", flat=True)
                )
                self.assertEqual(
                    visible, {"C1", "R1"},
                    f"{who.username} should see the whole catalogue",
                )

    def test_a_second_manager_shares_the_first_manager_s_shelf(self):
        """
        The case that forced this change: hiring a second manager used to split
        the catalogue in two, so the same cement got entered twice under two
        owners.
        """
        other = User.objects.create_user("moses", password="pw", role="MANAGER")
        Product.objects.create(
            name="Sugar", sku="S1", selling_price=Decimal("15"), owner=other
        )
        for who in (self.manager, other, self.sales):
            with self.subTest(user=who.username):
                skus = set(
                    scoped(Product.objects.all(), who)
                    .values_list("sku", flat=True)
                )
                self.assertEqual(skus, {"C1", "R1", "S1"})

    def test_a_signed_out_or_disabled_account_still_sees_nothing(self):
        """Shared with the staff is not the same as public."""
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(
            scoped(Product.objects.all(), AnonymousUser()).count(), 0
        )
        self.sales.is_active = False
        self.sales.save(update_fields=["is_active"])
        self.assertEqual(
            scoped(Product.objects.all(), self.refresh(self.sales)).count(), 0
        )

    def test_seeing_the_shelf_is_not_permission_to_change_it(self):
        """
        The catalogue got wider; who may edit it did not move at all. This is
        the line that keeps the change safe.
        """
        self.assertTrue(self.sales.has_access("product.view"))
        for denied in ("product.edit", "product.create", "stock.restock",
                       "material.adjust", "production.reverse"):
            self.assertFalse(
                self.sales.has_access(denied),
                f"a sales user must still not hold {denied}",
            )

    # -- The private half --------------------------------------------------
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
        self.assertIn('class="stat-card', html)
        self.assertIn("today-strip", html)
        self.assertIn("sidebar-footer", html)

    def test_each_role_gets_its_own_dashboard(self):
        """
        The three built-in roles must not land on the same page.

        Asserted through the rendered HTML rather than through profile_for()
        alone, because the bug worth catching is a layout that computes the
        right profile and then renders the wrong panels anyway.
        """
        expectations = [
            # user, title, must contain, must NOT contain
            (self.admin, "Business overview", "Month Gross Profit", "Quick actions"),
            (self.manager, "Stock and team", "Needs Restocking", "Month Gross Profit"),
            (self.sales, "My sales", "Quick actions", "Stock Value (Cost)"),
        ]
        for user, title, present, absent in expectations:
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                html = client.get("/reports/").content.decode()
                self.assertIn(title, html)
                self.assertIn(present, html)
                self.assertNotIn(absent, html)

    def test_a_sales_dashboard_never_shows_anyone_elses_totals(self):
        client = Client()
        client.force_login(self.sales)
        html = client.get("/reports/").content.decode()
        # The staff league table compares people against each other. Somebody
        # scoped to their own records must never be handed one.
        self.assertNotIn("Every staff member", html)
        self.assertNotIn("My team", html)
        self.assertIn("My Customers", html)

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

    def test_the_phone_gets_the_catalogue_in_its_own_language(self):
        """
        The bug this protects: English checkboxes inside an Amharic app.

        The phone has no client-side sweep like the browser does, so the
        server has to send the wording already translated.
        """
        self.client.force_login(self.admin)
        english = self.client.get("/api/access/catalog/").json()
        amharic = self.client.get(
            "/api/access/catalog/", HTTP_ACCEPT_LANGUAGE="am-ET,am;q=0.9"
        ).json()

        def first_label(payload):
            return payload["groups"][0]["permissions"][0]["label"]

        self.assertEqual(first_label(english), "Open the dashboard")
        self.assertNotEqual(first_label(amharic), first_label(english))

        # Codes are identifiers, not words. They are what the tick boxes post
        # back, so translating one would break saving rather than reading.
        self.assertEqual(
            [p["code"] for g in english["groups"] for p in g["permissions"]],
            [p["code"] for g in amharic["groups"] for p in g["permissions"]],
        )

        # An explicit choice in the app beats the phone's system language.
        picked = self.client.get(
            "/api/access/catalog/?lang=am", HTTP_ACCEPT_LANGUAGE="en-GB,en"
        ).json()
        self.assertEqual(first_label(picked), first_label(amharic))


class TranslationCoverageTests(TestCase):
    """
    A permission added without a translation is the bug that keeps coming
    back: it works, it ships, and months later somebody notices one English
    line in the middle of an Amharic screen. Failing here is cheaper.
    """

    def test_every_catalogue_string_has_amharic(self):
        missing = [key for key, _english in translation_pairs() if key not in AMHARIC]
        self.assertEqual(
            missing, [],
            "Add these to core/permissions_am.py, then run "
            "`manage.py sync_permission_i18n`: " + ", ".join(missing),
        )

    def test_amharic_has_no_entries_for_permissions_that_vanished(self):
        known = {key for key, _english in translation_pairs()}
        stale = sorted(set(AMHARIC) - known)
        self.assertEqual(
            stale, [],
            "These translations no longer match any permission: "
            + ", ".join(stale),
        )

    def test_translation_never_changes_a_permission_code(self):
        english = catalog_as_dict("")
        amharic = catalog_as_dict("am")
        self.assertEqual(
            [p["code"] for g in english for p in g["permissions"]],
            [p["code"] for g in amharic for p in g["permissions"]],
        )

    def test_an_unknown_language_falls_back_to_english(self):
        # Not a nicety: Accept-Language arrives from the outside world and
        # can say anything at all.
        self.assertEqual(catalog_as_dict("zz"), catalog_as_dict(""))


class RegistrationPasscodeTests(AccessTestBase):
    """
    Sales registration - the feature that looked broken because it was
    switched off with nothing on screen to say so.

    The old rule was "a role is offered if an environment variable holds a
    passcode for it". PASSCODE_SALES had never been set, so Sales silently
    vanished from the form. These tests pin the replacement: an administrator
    turns a role on from Settings, and only then does it appear.
    """

    def setUp(self):
        self.client = Client()

    def open_sales(self, code="shop-sales-2026"):
        row, _ = RegistrationPasscode.objects.get_or_create(role_code="SALES")
        row.set_passcode(code)
        row.is_enabled = True
        row.save()
        return row

    def test_a_role_with_no_passcode_is_not_offered(self):
        self.assertEqual(available_roles(), [])
        html = self.client.get("/accounts/register/", follow=True).content.decode()
        self.assertNotIn('value="SALES"', html)

    def test_setting_a_passcode_puts_sales_on_the_form(self):
        self.open_sales()
        self.assertIn(("SALES", "Sales"), available_roles())
        html = self.client.get("/accounts/register/").content.decode()
        self.assertIn('value="SALES"', html)

    def test_the_passcode_is_never_stored_or_shown_in_the_clear(self):
        row = self.open_sales("shop-sales-2026")
        self.assertNotIn("shop-sales-2026", row.passcode_hash)
        self.assertTrue(row.verify("shop-sales-2026"))
        self.assertFalse(row.verify("shop-sales-2027"))

        client = Client()
        client.force_login(self.admin)
        page = client.get("/system/settings/security/").content.decode()
        self.assertNotIn("shop-sales-2026", page)

    def test_registering_as_sales_attaches_a_supervisor(self):
        self.open_sales()
        response = self.client.post(
            "/accounts/register/",
            {
                "username": "selam",
                "role": "SALES",
                "manager": self.manager.pk,
                "passcode": "shop-sales-2026",
                "password1": "Str0ngPass!42",
                "password2": "Str0ngPass!42",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        new = User.objects.get(username="selam")
        # Without a supervisor a sales account sees no products at all, which
        # looks exactly like a broken install.
        self.assertEqual(new.manager, self.manager)
        self.assertEqual(new.role, "SALES")
        self.assertIn("sale.create", new.effective_permissions)
        self.assertNotIn("product.view_cost", new.effective_permissions)

    def test_a_wrong_passcode_creates_nothing(self):
        self.open_sales()
        self.client.post(
            "/accounts/register/",
            {
                "username": "intruder",
                "role": "SALES",
                "manager": self.manager.pk,
                "passcode": "guess",
                "password1": "Str0ngPass!42",
                "password2": "Str0ngPass!42",
            },
        )
        self.assertFalse(User.objects.filter(username="intruder").exists())

    def test_a_sales_user_cannot_be_somebody_elses_supervisor(self):
        self.open_sales()
        with self.assertRaises(RegistrationError):
            register_user(
                username="chain",
                password="Str0ngPass!42",
                role="SALES",
                passcode="shop-sales-2026",
                manager=self.sales,
            )

    def test_the_security_screen_will_not_open_a_role_with_no_code(self):
        client = Client()
        client.force_login(self.admin)
        client.post(
            "/system/settings/security/",
            {"allow_self_registration": "on", "enabled_SALES": "on"},
        )
        row = RegistrationPasscode.objects.get(role_code="SALES")
        self.assertFalse(row.is_enabled)
        self.assertEqual(available_roles(), [])

    def test_clearing_a_passcode_also_closes_the_door(self):
        self.open_sales()
        client = Client()
        client.force_login(self.admin)
        client.post(
            "/system/settings/security/",
            {
                "allow_self_registration": "on",
                "clear_SALES": "on",
                "enabled_SALES": "on",
            },
        )
        row = RegistrationPasscode.objects.get(role_code="SALES")
        self.assertFalse(row.has_passcode)
        self.assertFalse(row.is_enabled)

    def test_turning_self_registration_off_closes_every_role(self):
        self.open_sales()
        client = Client()
        client.force_login(self.admin)
        client.post("/system/settings/security/", {})
        self.assertEqual(available_roles(), [])


class DashboardProfileTests(AccessTestBase):
    """The layout is chosen by permission, never by the role's name."""

    def test_each_built_in_role_lands_on_its_own_layout(self):
        self.assertEqual(profile_for(self.admin), "owner")
        self.assertEqual(profile_for(self.manager), "stock")
        self.assertEqual(profile_for(self.sales), "counter")

    def test_a_custom_role_that_restocks_gets_the_manager_layout(self):
        role = RoleDefinition.objects.create(
            code="STOCKCLERK",
            name="Stock Clerk",
            permissions=["dashboard.view", "product.view", "stock.restock"],
            data_scope=DataScope.OWN,
        )
        clerk = User.objects.create_user("cliff", password="pw", role=role.code)
        self.assertEqual(profile_for(clerk), "stock")

    def test_stripping_profit_access_changes_the_admin_layout(self):
        apply_user_access(
            user=self.admin,
            role_code="ADMIN",
            ticked=sorted(ALL_CODES - {"report.profit"}),
            editor=self.admin,
        )
        self.refresh(self.admin)
        self.assertNotEqual(profile_for(self.admin), "owner")

    def test_somebody_with_almost_nothing_still_gets_a_page(self):
        watcher = User.objects.create_user("wanda", password="pw", role="SALES")
        apply_user_access(
            user=watcher,
            role_code="SALES",
            ticked=["dashboard.view"],
            editor=self.admin,
        )
        self.refresh(watcher)
        self.assertEqual(profile_for(watcher), "viewer")
        client = Client()
        client.force_login(watcher)
        self.assertEqual(client.get("/reports/").status_code, 200)


class TransactionSafetyTests(SimpleTestCase):
    """
    Every `select_for_update()` must sit inside a transaction.

    This is a STATIC check, read off the source, and that is deliberate.
    Django only raises `TransactionManagementError` for a lock taken outside a
    transaction when the backend reports `has_select_for_update`. SQLite
    reports False, so it quietly drops the lock and never complains; PostgreSQL
    reports True and refuses the very first request.

    So this class of mistake is invisible on a developer's SQLite database and
    fatal on the deployed one - and a `TestCase` makes it worse still, because
    it wraps every test in a transaction of its own, which means even the
    Postgres suite would go green. A run-time test cannot catch this. The
    source can.

    It also happens to be the check that matters for correctness rather than
    just for not crashing: a lock has to span the read AND the write, or two
    people counting the same shelf both read the same "before" figure and the
    second one silently erases the first.
    """

    #: Apps whose service layer touches the ledgers.
    ROOT = Path(settings.BASE_DIR)

    def _locking_functions(self):
        """Yield (path, function node, decorator names, body) for each lock."""
        import ast

        for path in sorted(self.ROOT.rglob("*.py")):
            parts = path.parts
            if "__pycache__" in parts or "migrations" in parts:
                continue
            # A test that talks ABOUT locking is not itself taking a lock -
            # including this file, which would otherwise report itself.
            if path.name == "tests.py" or path.name.startswith("test_"):
                continue
            source = path.read_text(encoding="utf-8")
            if "select_for_update" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = ast.get_source_segment(source, node) or ""
                if "select_for_update" not in body:
                    continue
                decorators = [
                    ast.unparse(d) for d in node.decorator_list
                ]
                yield path.relative_to(self.ROOT), node, decorators, body

    def test_no_row_lock_is_taken_outside_a_transaction(self):
        offenders = []
        for path, node, decorators, body in self._locking_functions():
            wrapped = any("atomic" in d for d in decorators)
            # `with transaction.atomic():` inside the body counts too.
            inline = "atomic()" in body
            if not (wrapped or inline):
                offenders.append(f"{path}::{node.name} (line {node.lineno})")

        self.assertEqual(
            offenders,
            [],
            "These functions call select_for_update() without a surrounding "
            "transaction. They will work on SQLite and raise "
            "TransactionManagementError on PostgreSQL the first time a real "
            "request reaches them:\n  " + "\n  ".join(offenders),
        )

    def test_the_check_is_actually_looking_at_something(self):
        """
        A guard that silently stops finding files is worse than no guard.

        If someone moves the service layer, renames it, or the walk above
        starts matching nothing, the test above passes for the wrong reason.
        """
        found = list(self._locking_functions())
        self.assertGreaterEqual(
            len(found), 10,
            "Expected to find the ledger services that take row locks; found "
            f"{len(found)}. The source walk is probably looking in the wrong "
            "place.",
        )


class NavigationTests(SimpleTestCase):
    """
    Exactly one sidebar link is highlighted at a time.

    The old sidebar decided this inline, with substring tests against the full
    view name. It read well and was quietly wrong: "production:material_list"
    contains "product", so every page in the yard lit up Products as well as
    its own link, and "core:user_access" contains "user_" so it lit up both
    Users and Access Control.

    Substring matching on names that nest inside one another cannot be made
    safe by adding more special cases, so the rules moved into
    core.context_processors.NAV_RULES where they can be checked - by this.
    """

    def _view_names(self):
        from django.urls import get_resolver

        def walk(resolver, namespace=None):
            for pattern in resolver.url_patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from walk(pattern, pattern.namespace or namespace)
                elif pattern.name:
                    yield (namespace or ""), pattern.name

        return sorted(set(walk(get_resolver())))

    def test_no_page_highlights_two_links(self):
        """
        The rule table is matched longest-prefix-first, so a page can only ever
        resolve to one key. This asserts the table has no rule whose prefix is
        ambiguous *within its own namespace* for a real url name.
        """
        from core.context_processors import NAV_RULES

        clashes = []
        for namespace, url_name in self._view_names():
            winners = {
                key for ns, prefix, key in NAV_RULES
                if ns == namespace and url_name.startswith(prefix)
            }
            if len(winners) > 1:
                # More than one KEY can only happen when two rules of equal
                # specificity disagree, which the longest-prefix rule cannot
                # break. That is a table bug.
                lengths = {
                    len(prefix) for ns, prefix, key in NAV_RULES
                    if ns == namespace and url_name.startswith(prefix)
                }
                if len(lengths) != len(winners):
                    continue  # a longer prefix wins cleanly
                clashes.append(f"{namespace}:{url_name} -> {sorted(winners)}")

        self.assertEqual(
            clashes, [],
            "These pages match two nav rules of equal specificity:\n  "
            + "\n  ".join(clashes),
        )

    def test_every_production_page_lights_its_own_section(self):
        """The bug that started this: the yard lighting up Products."""
        from core.context_processors import nav_active

        class FakeMatch:
            def __init__(self, namespace, url_name):
                self.namespace = namespace
                self.url_name = url_name

        class FakeRequest:
            def __init__(self, match):
                self.resolver_match = match

        expected = {
            ("production", "material_list"): "materials",
            ("production", "material_detail"): "materials",
            ("production", "material_adjust"): "materials",
            ("production", "run_list"): "runs",
            ("production", "run_detail"): "runs",
            ("production", "recipe_list"): "recipes",
            ("inventory", "product_list"): "products",
            ("inventory", "low_stock"): "low_stock",
            ("core", "user_access"): "access",
            ("accounts", "user_list"): "users",
        }
        for (namespace, url_name), key in expected.items():
            with self.subTest(view=f"{namespace}:{url_name}"):
                got = nav_active(FakeRequest(FakeMatch(namespace, url_name)))
                self.assertEqual(got["NAV"], key)

    def test_the_sidebar_no_longer_decides_this_itself(self):
        """
        A regression guard with teeth: if someone puts an inline view-name test
        back into the sidebar, this fails and points at why.
        """
        sidebar = (
            Path(settings.BASE_DIR) / "templates" / "partials" / "sidebar.html"
        ).read_text(encoding="utf-8")
        body = sidebar.split("{% endcomment %}", 1)[-1]
        self.assertNotIn(
            "resolver_match", body,
            "The sidebar is testing the view name inline again. Highlighting "
            "is decided once, in core.context_processors.nav_active - see the "
            "comment at the top of the file.",
        )


class FriendlyErrorTests(TestCase):
    """
    A failure never shows a stack trace to whoever is standing at the counter.
    """

    def test_an_unexpected_error_renders_the_branded_page(self):
        from django.test import RequestFactory

        from core.middleware import FriendlyErrorMiddleware

        middleware = FriendlyErrorMiddleware(lambda r: None)
        request = RequestFactory().post("/inventory/products/1/adjust/")

        with self.assertLogs("core.middleware", level="ERROR"):
            response = middleware.process_exception(request, RuntimeError("boom"))

        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn("Something went wrong", body)
        # The two things that must never reach the screen.
        self.assertNotIn("boom", body)
        self.assertNotIn("Traceback", body)

    def test_the_api_is_left_to_return_json(self):
        """An HTML error page would break the phone's parser, not inform it."""
        from django.test import RequestFactory

        from core.middleware import FriendlyErrorMiddleware

        middleware = FriendlyErrorMiddleware(lambda r: None)
        request = RequestFactory().get("/api/materials/")
        self.assertIsNone(
            middleware.process_exception(request, RuntimeError("boom"))
        )

    def test_a_validation_error_keeps_its_own_wording(self):
        from django.core.exceptions import ValidationError

        from core.errors import describe

        self.assertEqual(
            describe(ValidationError("Enter a quantity greater than zero.")),
            "Enter a quantity greater than zero.",
        )

    def test_a_database_error_is_summarised_not_exposed(self):
        from django.db import DatabaseError

        from core.errors import describe

        with self.assertLogs("core.errors", level="ERROR"):
            text = describe(DatabaseError("relation does not exist"))
        self.assertNotIn("relation does not exist", text)
        self.assertIn("nothing was changed", text)

    def test_the_debug_page_cannot_print_the_database_password(self):
        """
        Django's own error page lists the settings and hides only names with
        PASS, KEY, SECRET or TOKEN in them. A DATABASE_URL setting would be
        printed whole - password and all - so the URL must not be a setting.
        """
        from django.views.debug import get_default_exception_reporter_filter

        shown = get_default_exception_reporter_filter().get_safe_settings()
        self.assertNotIn("DATABASE_URL", shown)
        for name, value in shown.items():
            self.assertNotIn("postgres://", str(value), name)
            self.assertNotIn("postgresql://", str(value), name)


class WebFormPickListTests(AccessTestBase):
    """
    The browser half of "every select can be added to and re-worded".

    The phone got this first. The web forms are the other front door, and a
    rule that holds on only one of them is a rule the reports cannot rely on -
    so these check the page actually carries the hooks the shared widget
    attaches to, rather than trusting that somebody remembered.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.admin)

    def _unit_widget(self, html):
        """The opening tag of whatever renders `name="unit"`."""
        match = re.search(r'<(select|input)\b[^>]*\bname="unit"[^>]*>', html)
        self.assertIsNotNone(match, "the form has no unit field at all")
        return match.group(0)

    def test_the_product_unit_select_is_a_managed_list(self):
        from django.urls import reverse

        html = self.client.get(reverse("inventory:product_create")).content.decode()
        tag = self._unit_widget(html)
        # A DROPDOWN - not merely the attributes. Taking `choices` off the
        # model field once turned this into a free-text box that still
        # carried them, and a test that only looked for the attribute passed.
        self.assertTrue(tag.startswith("<select"), tag)
        self.assertIn('data-option-group="PRODUCT_UNIT"', tag)
        self.assertIn('data-option-value="code"', tag)
        select = re.search(r'<select\b[^>]*name="unit".*?</select>', html, re.S).group(0)
        self.assertIn('<option value="PIECE" selected>Piece</option>', select,
                      "a new product starts on the model's default unit")

    def test_the_material_unit_select_is_a_managed_list(self):
        from django.urls import reverse

        html = self.client.get(reverse("production:material_create")).content.decode()
        tag = self._unit_widget(html)
        self.assertTrue(tag.startswith("<select"), tag)
        self.assertIn('data-option-group="MATERIAL_UNIT"', tag)

    def test_a_unit_in_no_list_is_refused_by_the_browser_form(self):
        """
        Open enough for a unit added a minute ago, closed enough that a typo
        does not become a unit nobody can pick again.
        """
        from django.urls import reverse

        response = self.client.post(
            reverse("inventory:product_create"),
            {
                "name": "Typo block",
                "unit": "PALLETT",
                "cost_price": "30.00",
                "selling_price": "42.00",
                "low_stock_threshold": "5",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200, "the form should re-render")
        self.assertIn("unit", response.context["form"].errors)
        self.assertFalse(Product.objects.filter(name="Typo block").exists())

    def test_a_product_saves_with_a_unit_somebody_added(self):
        """
        The end of the round trip: a unit added from either front door has to
        be accepted by the form, not met with "Select a valid choice".
        """
        from django.urls import reverse

        from core.models import Option

        Option.objects.create(
            group="PRODUCT_UNIT", label="Pallet", created_by=self.admin
        )
        code = Option.objects.get(group="PRODUCT_UNIT", label="Pallet").code
        self.assertTrue(code, "a unit needs a code to be stored by")

        response = self.client.post(
            reverse("inventory:product_create"),
            {
                "name": "Hollow block",
                "unit": code,
                "cost_price": "30.00",
                "selling_price": "42.00",
                "low_stock_threshold": "5",
                "is_active": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        saved = Product.objects.get(name="Hollow block")
        self.assertEqual(saved.unit, code)
        self.assertEqual(saved.get_unit_display(), "Pallet")


class WebWalkInSaleTests(AccessTestBase):
    """
    A buyer who is not coming back, recorded at the browser till.

    The customer book carries a credit limit and a place in the aging report,
    so filing a passer-by in it costs something: the list somebody works from
    becomes a phone directory. Their name and number belong on the sale.
    """

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.sales)

    def _post(self, **extra):
        data = {
            "product_id[]": [str(self.product.pk)],
            "quantity[]": ["2"],
            "unit_price[]": ["20.00"],
            "line_discount[]": ["0.00"],
            "payment_method": "CASH",
            "amount_paid": "40.00",
            "discount_amount": "0",
            "tax_amount": "0",
        }
        data.update(extra)
        return self.client.post("/sales/new/", data, follow=True)

    def test_a_one_off_buyer_is_named_on_the_sale_and_not_in_the_book(self):
        from sales.models import Transaction

        before = Customer.objects.count()
        response = self._post(
            walk_in_name="  Chala   Bekele ", walk_in_phone=" 0933 "
        )
        self.assertEqual(response.status_code, 200)

        txn = Transaction.objects.latest("id")
        self.assertIsNone(txn.customer)
        self.assertEqual(txn.customer_name_snapshot, "Chala Bekele")
        self.assertEqual(txn.customer_phone_snapshot, "0933")
        self.assertEqual(
            Customer.objects.count(), before,
            "a passer-by must not land in the customer book",
        )

    def test_a_one_off_buyer_cannot_be_given_a_due_date(self):
        from sales.models import Transaction

        before = Transaction.objects.count()
        response = self._post(
            walk_in_name="Chala",
            amount_paid="10.00",
            due_date="2030-01-01",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be given a due date")
        self.assertEqual(
            Transaction.objects.count(), before,
            "the sale must not be recorded when the form is refused",
        )

    def test_an_unpaid_one_off_sale_is_refused(self):
        """No account behind it means the balance is owed by nobody."""
        from sales.models import Transaction

        before = Transaction.objects.count()
        self._post(walk_in_name="Chala", amount_paid="5.00")
        self.assertEqual(Transaction.objects.count(), before)
        self.assertEqual(DebtRecord.objects.count(), 0)

    def test_a_registered_customer_wins_over_the_one_off_boxes(self):
        from sales.models import Transaction

        self._post(
            customer=str(self.customer.pk),
            walk_in_name="Somebody Else",
            walk_in_phone="0999",
        )
        txn = Transaction.objects.latest("id")
        self.assertEqual(txn.customer_id, self.customer.pk)
        self.assertEqual(txn.customer_name_snapshot, "Abebe")


class MigrationHygieneTests(SimpleTestCase):
    """
    No migration may change a table's schema AND run Python in one go.

    WHY
    ---
    Django runs each migration in one transaction, and PostgreSQL checks
    foreign keys at COMMIT. So rows a RunPython writes leave checks queued on
    their table, and any schema change Django makes afterwards in the same
    migration - an index it defers to the end, say - is refused:

        cannot CREATE INDEX "core_option" because it has pending trigger events

    That is exactly how core/0003 failed on the real database while passing
    every test here: SQLite has no such rule. Django's own documentation
    gives the fix - keep schema changes and RunPython in separate migrations -
    and this makes it a rule rather than something to remember.
    """

    #: Written before this rule, already applied on every database, and each
    #: happens not to trip it (their Python only updates columns no queued
    #: check covers). Rewriting applied history would be worse than listing
    #: them. Nothing new goes on this list - split the migration instead.
    GRANDFATHERED = {
        ("accounts", "0005_registration_passcode"),
        ("core", "0002_option"),
        ("credit", "0002_debtrecord_owner"),
        ("inventory", "0002_product_owner"),
        ("sales", "0002_owner_scoping"),
    }

    PROJECT_APPS = {
        "accounts", "api", "core", "credit", "inventory", "production",
        "reports", "sales",
    }

    @staticmethod
    def mixes(operations) -> bool:
        """Whether a migration's operations change schema AND run Python."""
        from django.db import migrations as ops

        data_ops = (ops.RunPython, ops.RunSQL)
        has_data = any(isinstance(op, data_ops) for op in operations)
        has_schema = any(
            not isinstance(op, data_ops + (ops.SeparateDatabaseAndState,))
            for op in operations
        )
        return has_data and has_schema

    def test_no_migration_mixes_schema_changes_with_python(self):
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(None, ignore_no_migrations=True)
        offenders = [
            f"{app}/{name}"
            for (app, name), migration in sorted(loader.disk_migrations.items())
            if app in self.PROJECT_APPS
            and (app, name) not in self.GRANDFATHERED
            and self.mixes(migration.operations)
        ]
        self.assertEqual(
            offenders, [],
            "These migrations change a table and run Python in the same "
            "transaction, which PostgreSQL refuses once the Python has "
            "written rows. Move the RunPython into a migration of its own.",
        )

    def test_the_rule_catches_the_shape_that_failed(self):
        """A guard is only worth having if it fails on the real mistake."""
        from django.db import migrations as ops
        from django.db import models

        add_code = ops.AddField(
            "option", "code",
            models.CharField(max_length=32, blank=True, db_index=True),
        )
        seed = ops.RunPython(ops.RunPython.noop)
        # The original core/0003: both in one migration.
        self.assertTrue(self.mixes([add_code, seed]))
        # The fix: one of each, in migrations of their own.
        self.assertFalse(self.mixes([add_code]))
        self.assertFalse(self.mixes([seed]))
