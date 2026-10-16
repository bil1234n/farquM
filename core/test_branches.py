"""
Branches - separate books for each shop or yard.

What these protect, in the owner's words: "if I select Branch 1, the full
data - the sales, the customers, the Audit, everything - is Branch 1's, and
the branches are not connected at all."

  1. Nothing of one branch is visible from another: lists, details, totals,
     the Audit, the API and the website alike - and asking for another
     branch's record by its id is "not found", not "forbidden".
  2. What is recorded lands in the branch being worked in, without any code
     having to say so.
  3. The owner switches; staff stay in the branches they are given, and an
     account given none works in the main branch, as before branches.
  4. Each branch numbers and names on its own (SKUs, phones, categories,
     pick-lists), while references stay unique across the business.
  5. A closed branch keeps its books, and its staff are refused rather than
     quietly moved into somebody else's.
  6. Outside a request - a test, the shell, a migration - nothing is
     filtered, which is how the system behaved before branches existed.
"""
from decimal import Decimal as D

from django.test import Client, TestCase

from accounts.models import AuditLog, User
from accounts.roles import ensure_system_roles
from core.branches import (
    BranchError,
    create_branch,
    set_branch_open,
    set_user_branches,
    switch_branch,
)
from core.branching import current_branch_id, using_branch
from core.models import Branch, Option
from inventory.models import Category, Product
from sales.models import Customer, Transaction
from sales.services import create_sale


class BranchTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()
        cls.main = Branch.main()
        cls.owner = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.b2 = create_branch(user=cls.owner, name="Adama yard", address="Adama")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.seller = User.objects.create_user("sam", password="pw", role="SALES")
        set_user_branches(cls.manager, [cls.b2.pk])

        # Main branch: what the business had before branches.
        cls.block = Product.objects.create(
            name="Hollow block", sku="HB-1", selling_price=D("32.00"),
            cost_price=D("20.00"), stock_quantity=500, owner=cls.owner,
        )
        cls.abebe = Customer.objects.create(
            name="Abebe", phone="0911", owner=cls.owner, is_credit_approved=True,
        )
        cls.main_sale = create_sale(
            user=cls.owner, customer=cls.abebe,
            cart=[{"product": cls.block, "quantity": 10, "unit_price": D("32.00")}],
            amount_paid=D("320.00"),
        )

        # Branch 2: built as a request in Branch 2 would build it.
        with using_branch(cls.b2):
            cls.paver = Product.objects.create(
                name="Paver", sku="PV-1", selling_price=D("15.00"),
                cost_price=D("9.00"), stock_quantity=300, owner=cls.owner,
            )
            cls.kebede = Customer.objects.create(
                name="Kebede", phone="0922", owner=cls.owner, is_credit_approved=True,
            )
            cls.b2_sale = create_sale(
                user=cls.owner, customer=cls.kebede,
                cart=[{"product": cls.paver, "quantity": 4, "unit_price": D("15.00")}],
                amount_paid=D("60.00"),
            )

    def client_for(self, user, branch=None):
        if branch is not None:
            switch_branch(user, branch.pk)
        client = Client()
        client.force_login(user)
        return client


class WhereRecordsLandTests(BranchTestBase):
    def test_everything_already_there_is_the_main_branch(self):
        self.assertEqual(self.block.branch_id, self.main.pk)
        self.assertEqual(self.abebe.branch_id, self.main.pk)
        self.assertEqual(self.main_sale.branch_id, self.main.pk)

    def test_records_made_in_a_branch_belong_to_it(self):
        self.assertEqual(self.paver.branch_id, self.b2.pk)
        self.assertEqual(self.kebede.branch_id, self.b2.pk)
        self.assertEqual(self.b2_sale.branch_id, self.b2.pk)

    def test_outside_a_request_a_sale_follows_its_customer(self):
        """The shell has no active branch; the sale still lands in Branch 2."""
        self.assertIsNone(current_branch_id())
        sale = create_sale(
            user=self.owner, customer=self.kebede,
            cart=[{"product": self.paver, "quantity": 1, "unit_price": D("15.00")}],
            amount_paid=D("15.00"),
        )
        self.assertEqual(sale.branch_id, self.b2.pk)

    def test_outside_a_request_nothing_is_filtered(self):
        self.assertEqual(Product.objects.count(), 2)
        self.assertEqual(Transaction.objects.count(), 2)

    def test_references_stay_unique_across_branches(self):
        self.assertNotEqual(self.main_sale.reference, self.b2_sale.reference)

    def test_a_sale_cannot_mix_branches(self):
        from sales.services import SaleError

        with using_branch(self.b2), self.assertRaises(SaleError):
            create_sale(
                user=self.owner, customer=None,
                cart=[{"product": self.block, "quantity": 1, "unit_price": D("32.00")}],
                amount_paid=D("32.00"),
            )


class SeparateBooksOnTheWebsiteTests(BranchTestBase):
    def test_lists_show_only_the_branch_being_worked_in(self):
        main = self.client_for(self.owner, self.main)
        page = main.get("/inventory/products/").content.decode()
        self.assertIn("Hollow block", page)
        self.assertNotIn("Paver", page)

        adama = self.client_for(self.owner, self.b2)
        page = adama.get("/inventory/products/").content.decode()
        self.assertIn("Paver", page)
        self.assertNotIn("Hollow block", page)

        customers = adama.get("/sales/customers/").content.decode()
        self.assertIn("Kebede", customers)
        self.assertNotIn("Abebe", customers)

        sales = adama.get("/sales/").content.decode()
        self.assertIn(self.b2_sale.reference, sales)
        self.assertNotIn(self.main_sale.reference, sales)

    def test_another_branchs_record_is_not_found(self):
        adama = self.client_for(self.owner, self.b2)
        self.assertEqual(adama.get(f"/inventory/products/{self.block.pk}/").status_code, 404)
        self.assertEqual(adama.get(f"/sales/{self.main_sale.pk}/").status_code, 404)
        self.assertEqual(adama.get(f"/sales/customers/{self.abebe.pk}/").status_code, 404)

    def test_switching_from_a_list_stays_on_the_list(self):
        client = self.client_for(self.owner, self.main)
        response = client.post(
            "/system/branches/switch/",
            {"branch": self.b2.pk, "next": "/inventory/products/?q=block"},
        )
        self.assertRedirects(response, "/inventory/products/", fetch_redirect_response=False)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.active_branch_id, self.b2.pk)

    def test_switching_from_one_record_goes_home(self):
        """That record is the old branch's - in the new one it does not exist."""
        client = self.client_for(self.owner, self.main)
        response = client.post(
            "/system/branches/switch/",
            {"branch": self.b2.pk, "next": f"/sales/{self.main_sale.pk}/"},
        )
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_the_switcher_lists_the_branches(self):
        page = self.client_for(self.owner, self.main).get("/inventory/products/")
        body = page.content.decode()
        self.assertIn("Switch branch", body)
        self.assertIn("Adama yard", body)

    def test_a_one_branch_business_sees_no_branch_controls(self):
        set_branch_open(self.b2, user=self.owner, is_open=False)
        body = self.client_for(self.owner).get("/inventory/products/").content.decode()
        self.assertNotIn("Switch branch", body)

    def test_a_new_product_lands_in_the_branch_being_worked_in(self):
        adama = self.client_for(self.owner, self.b2)
        adama.post("/inventory/categories/add/", {"name": "Pavers", "is_active": "on"})
        self.assertEqual(Category.objects.get(name="Pavers").branch_id, self.b2.pk)

    def test_the_activity_log_is_kept_per_branch(self):
        from accounts.models import AuditAction
        from accounts.services import log_action

        log_action(AuditAction.VOID, instance=self.main_sale, description="main thing")
        log_action(AuditAction.VOID, instance=self.b2_sale, description="adama thing")
        log_action(AuditAction.LOGIN, instance=self.owner, description="signed in")
        with using_branch(self.b2):
            seen = set(AuditLog.objects.values_list("description", flat=True))
        self.assertIn("adama thing", seen)
        self.assertIn("signed in", seen)           # the whole business's
        self.assertNotIn("main thing", seen)


class SeparateBooksOnThePhoneTests(BranchTestBase):
    def test_the_api_lists_only_this_branch(self):
        adama = self.client_for(self.owner, self.b2)
        names = [row["name"] for row in adama.get("/api/products/").json()["results"]]
        self.assertEqual(names, ["Paver"])
        refs = [row["reference"] for row in adama.get("/api/sales/").json()["results"]]
        self.assertEqual(refs, [self.b2_sale.reference])
        self.assertEqual(adama.get(f"/api/products/{self.block.pk}/").status_code, 404)

    def test_the_audit_counts_one_branch(self):
        adama = self.client_for(self.owner, self.b2)
        report = adama.get("/api/audit/?range=all").json()
        self.assertEqual(D(str(report["money_in"]["total"])), D("60.00"))
        main = self.client_for(self.owner, self.main)
        report = main.get("/api/audit/?range=all").json()
        self.assertEqual(D(str(report["money_in"]["total"])), D("320.00"))

    def test_a_sale_from_the_phone_cannot_use_another_branchs_product(self):
        adama = self.client_for(self.owner, self.b2)
        response = adama.post(
            "/api/sales/",
            {
                "items": [{"product": self.block.pk, "quantity": 1}],
                "amount_paid": "32.00",
                "payment_method": "CASH",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(Transaction.objects.count(), 2)
        # Refused for the right reason: the same request with this branch's
        # own product goes through.
        response = adama.post(
            "/api/sales/",
            {
                "items": [{"product": self.paver.pk, "quantity": 1}],
                "amount_paid": "15.00",
                "payment_method": "CASH",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Transaction.objects.latest("id").branch_id, self.b2.pk)

    def test_me_says_where_you_are_and_where_you_may_go(self):
        body = self.client_for(self.owner, self.b2).get("/api/auth/me/").json()
        self.assertEqual(body["branch"]["id"], self.b2.pk)
        self.assertEqual(
            {b["name"] for b in body["my_branches"]}, {self.main.name, "Adama yard"}
        )
        self.assertTrue(body["several_branches"])

    def test_switching_from_the_phone(self):
        client = self.client_for(self.owner, self.main)
        response = client.post(
            "/api/branches/switch/", {"branch": self.b2.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["user"]["branch"]["id"], self.b2.pk)
        # The very next request is in the new branch.
        names = [row["name"] for row in client.get("/api/products/").json()["results"]]
        self.assertEqual(names, ["Paver"])

    def test_the_token_login_works_in_the_users_branch(self):
        from rest_framework.authtoken.models import Token

        token = Token.objects.create(user=self.manager)
        client = Client(HTTP_AUTHORIZATION=f"Token {token.key}")
        names = [row["name"] for row in client.get("/api/products/").json()["results"]]
        self.assertEqual(names, ["Paver"])
        # And nothing was left switched on behind the request.
        self.assertIsNone(current_branch_id())

    def test_opening_a_branch_from_the_phone(self):
        client = self.client_for(self.owner, self.main)
        response = client.post(
            "/api/branches/", {"name": "Bishoftu"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        branch = Branch.objects.get(name="Bishoftu")
        # It starts empty, with lists of its own.
        self.assertTrue(Option.objects.filter(branch=branch).exists())
        self.assertFalse(Product.objects.filter(branch=branch).exists())

    def test_staff_cannot_open_branches(self):
        client = self.client_for(self.manager)
        response = client.post(
            "/api/branches/", {"name": "Nope"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)


class WhoWorksWhereTests(BranchTestBase):
    def test_the_owner_may_open_every_branch(self):
        self.assertEqual(set(self.owner.allowed_branch_ids), {self.main.pk, self.b2.pk})

    def test_staff_work_only_in_their_branch(self):
        self.assertEqual(self.manager.allowed_branch_ids, (self.b2.pk,))
        self.assertEqual(self.manager.current_branch_id, self.b2.pk)
        with self.assertRaises(BranchError):
            switch_branch(self.manager, self.main.pk)

        client = self.client_for(self.manager)
        response = client.post(
            "/api/branches/switch/", {"branch": self.main.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        names = [row["name"] for row in client.get("/api/products/").json()["results"]]
        self.assertEqual(names, ["Paver"])

    def test_staff_given_no_branch_work_in_the_main_one(self):
        self.assertEqual(self.seller.allowed_branch_ids, (self.main.pk,))
        self.assertEqual(self.seller.current_branch_id, self.main.pk)

    def test_closing_a_branch_refuses_its_staff_rather_than_moving_them(self):
        set_branch_open(self.b2, user=self.owner, is_open=False)
        manager = User.objects.get(pk=self.manager.pk)
        client = self.client_for(manager)
        self.assertEqual(client.get("/inventory/products/").status_code, 403)
        self.assertEqual(client.get("/api/products/").status_code, 403)

        from rest_framework.authtoken.models import Token

        token = Token.objects.create(user=manager)
        phone = Client(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(phone.get("/api/products/").status_code, 403)

    def test_the_main_branch_cannot_be_closed(self):
        with self.assertRaises(BranchError):
            set_branch_open(self.main, user=self.owner, is_open=False)

    def test_a_closed_branch_keeps_its_books(self):
        set_branch_open(self.b2, user=self.owner, is_open=False)
        self.assertTrue(Product.objects.filter(pk=self.paver.pk).exists())
        set_branch_open(self.b2, user=self.owner, is_open=True)
        with using_branch(self.b2):
            self.assertEqual(list(Product.objects.values_list("name", flat=True)), ["Paver"])

    def test_branches_are_given_on_the_user_form(self):
        client = self.client_for(self.owner, self.main)
        response = client.post(
            f"/accounts/users/{self.seller.pk}/edit/",
            {
                "username": "sam", "role": "SALES", "is_active": "on",
                "branches": [self.b2.pk],
            },
        )
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        seller = User.objects.get(pk=self.seller.pk)
        self.assertEqual(seller.allowed_branch_ids, (self.b2.pk,))


class EachBranchNamesItsOwnTests(BranchTestBase):
    def test_the_same_phone_may_be_a_customer_in_both_branches(self):
        with using_branch(self.b2):
            twin = Customer.objects.create(name="Abebe", phone="0911", owner=self.owner)
        self.assertEqual(twin.branch_id, self.b2.pk)

    def test_the_same_category_name_in_both_branches(self):
        Category.objects.create(name="Blocks")
        with using_branch(self.b2):
            Category.objects.create(name="Blocks")
        self.assertEqual(Category.objects.filter(name="Blocks").count(), 2)

    def test_a_duplicate_in_the_same_branch_is_a_sentence_not_a_crash(self):
        with using_branch(self.b2):
            Category.objects.create(name="Blocks")
        adama = self.client_for(self.owner, self.b2)
        response = adama.post("/inventory/categories/add/", {"name": "Blocks"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "There is already a category with this name.")
        api = adama.post(
            "/api/categories/", {"name": "Blocks"}, content_type="application/json"
        )
        self.assertEqual(api.status_code, 400)

    def test_every_branch_has_its_own_pick_lists(self):
        main_banks = set(
            Option.objects.filter(branch=self.main, group="BANK").values_list("label", flat=True)
        )
        b2_banks = set(
            Option.objects.filter(branch=self.b2, group="BANK").values_list("label", flat=True)
        )
        self.assertEqual(main_banks, b2_banks)   # both seeded alike

        with using_branch(self.b2):
            from core.models import resolve_option

            resolve_option("BANK", label="Adama Savings Union", user=self.owner)
        self.assertTrue(Option.objects.filter(branch=self.b2, label="Adama Savings Union").exists())
        self.assertFalse(Option.objects.filter(branch=self.main, label="Adama Savings Union").exists())

        adama = self.client_for(self.owner, self.b2)
        def labels(client):
            body = client.get("/api/options/?group=BANK").json()
            rows = body["results"] if isinstance(body, dict) else body
            return [o["label"] for o in rows]

        self.assertIn("Adama Savings Union", labels(adama))
        main = self.client_for(self.owner, self.main)
        self.assertNotIn("Adama Savings Union", labels(main))

    def test_branch_names_are_unique(self):
        with self.assertRaises(BranchError):
            create_branch(user=self.owner, name="adama YARD")


class BranchAdministrationTests(BranchTestBase):
    def test_the_owner_lists_every_branch_with_its_staff(self):
        client = self.client_for(self.owner, self.main)
        body = client.get("/api/branches/").json()
        self.assertTrue(body["can_manage"])
        rows = {row["name"]: row for row in body["results"]}
        self.assertEqual(set(rows), {self.main.name, "Adama yard"})
        # Mary is given Adama; Sam is given nothing, so he works in main.
        self.assertEqual(rows["Adama yard"]["staff_count"], 1)
        self.assertEqual(rows[self.main.name]["staff_count"], 1)

    def test_staff_list_only_where_they_may_go(self):
        body = self.client_for(self.manager).get("/api/branches/").json()
        self.assertFalse(body["can_manage"])
        self.assertEqual([row["name"] for row in body["results"]], ["Adama yard"])

    def test_closing_and_reopening_from_the_phone(self):
        client = self.client_for(self.owner, self.main)
        closed = client.patch(
            f"/api/branches/{self.b2.pk}/", {"is_active": False},
            content_type="application/json",
        )
        self.assertEqual(closed.status_code, 200, closed.content)
        self.assertFalse(closed.json()["is_active"])
        refused = client.patch(
            f"/api/branches/{self.main.pk}/", {"is_active": False},
            content_type="application/json",
        )
        self.assertEqual(refused.status_code, 400)
        reopened = client.patch(
            f"/api/branches/{self.b2.pk}/", {"is_active": True, "name": "Adama"},
            content_type="application/json",
        )
        self.assertEqual(reopened.json()["name"], "Adama")
        self.assertTrue(reopened.json()["is_active"])

    def test_giving_somebody_a_branch_from_the_phone(self):
        client = self.client_for(self.owner, self.main)
        response = client.patch(
            f"/api/users/{self.seller.pk}/", {"branches": [self.b2.pk]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["branches"], [self.b2.pk])
        self.assertEqual(response.json()["branch_names"], "Adama yard")
        self.assertEqual(User.objects.get(pk=self.seller.pk).allowed_branch_ids, (self.b2.pk,))

    def test_somebody_added_inside_a_branch_works_there(self):
        client = self.client_for(self.owner, self.b2)
        response = client.post(
            "/api/users/",
            {"username": "lemma", "password": "a-long-password", "role": "SALES"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            User.objects.get(username="lemma").allowed_branch_ids, (self.b2.pk,)
        )

    def test_the_edit_page_keeps_the_signed_in_person_in_the_menu(self):
        """The edited account used to replace `user` in the template."""
        client = self.client_for(self.owner, self.main)
        page = client.get(f"/accounts/users/{self.seller.pk}/edit/")
        self.assertEqual(page.context["user"], self.owner)
        self.assertEqual(page.context["target_user"], self.seller)

    def test_a_stock_request_goes_to_somebody_in_this_branch(self):
        from production.services import assignable_deciders

        with using_branch(self.b2):
            people = {p.username for p in assignable_deciders(self.owner)}
        # Mary works in Adama; nobody else there can make stock but the owner,
        # who is asking.
        self.assertIn("mary", people)
        with using_branch(self.main):
            people = {p.username for p in assignable_deciders(self.owner)}
        self.assertNotIn("mary", people)

    def test_notifications_are_kept_per_branch(self):
        from api.models import NotificationLog
        from api.push import notify_users

        with using_branch(self.b2):
            notify_users(self.owner, title="Low stock", body="Paver", channel="stock")
        with using_branch(self.main):
            notify_users(self.owner, title="Low stock", body="Block", channel="stock")
        notify_users(self.owner, title="Hello", body="everyone", channel="general")

        adama = self.client_for(self.owner, self.b2)
        bodies = {row["body"] for row in adama.get("/api/notifications/").json()["results"]}
        self.assertEqual(bodies, {"Paver", "everyone"})
        with using_branch(self.b2):
            entry = NotificationLog.objects.get(body="Paver")
        # Several branches: the title says which one the news is from.
        self.assertEqual(entry.title, "Adama yard · Low stock")
