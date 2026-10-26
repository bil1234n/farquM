"""
Each phone and each browser works in the branch on its own screen.

The bug this guards against, as the owner met it: the phone's Account page
said "Branch 2", but the account had been switched to the main branch from
somewhere else (the website, or a second phone signed in to the same
account). The phone then listed the main branch's production batches, and a
raw material added "in Branch 2" was saved into the main branch's store.

The branch used to be one value on the account, shared by every device. Now:

  1. The phone sends the branch its screen shows (`X-Branch`) on every
     request, and that branch is the one read and written.
  2. A browser keeps the branch it chose in its own session.
  3. Neither is moved by the other. The account's remembered branch is only
     where a device that says nothing starts.
  4. A branch the person may no longer work in is refused (409), never
     quietly swapped for another one - except on the few endpoints the phone
     needs to find out where it is now.
  5. Every API answer says which branch served it.
"""
from decimal import Decimal as D

from django.test import Client
from rest_framework.authtoken.models import Token

from core.branches import set_branch_open, set_user_branches, switch_branch
from core.branching import current_branch_id, using_branch
from core.models import Branch
from core.test_branches import BranchTestBase
from production.models import RawMaterial


class EachDeviceKeepsItsOwnBranchTests(BranchTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.main_cement = RawMaterial.objects.create(
            name="Main cement", code="MCEM", unit="KG", owner=cls.owner,
        )
        with using_branch(cls.b2):
            cls.b2_sand = RawMaterial.objects.create(
                name="Adama sand", code="ASND", unit="KG", owner=cls.owner,
            )

    def phone(self, user, branch=None) -> Client:
        """The app: a token, and the branch its screen shows."""
        token, _ = Token.objects.get_or_create(user=user)
        headers = {"HTTP_AUTHORIZATION": f"Token {token.key}"}
        if branch is not None:
            headers["HTTP_X_BRANCH"] = str(getattr(branch, "pk", branch))
        return Client(**headers)

    def browser(self, user, branch) -> Client:
        """The website: a session that switched to `branch`."""
        client = Client()
        client.force_login(user)
        response = client.post("/system/branches/switch/", {"branch": branch.pk})
        self.assertEqual(response.status_code, 302)
        return client

    @staticmethod
    def names(response):
        return sorted(row["name"] for row in response.json()["results"])

    # -- The bug ---------------------------------------------------------------
    def test_the_phone_stays_in_its_branch_when_the_website_switches(self):
        phone = self.phone(self.owner, self.b2)
        # The same account moves to the main branch on the website...
        self.browser(self.owner, self.main)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.active_branch_id, self.main.pk)

        # ...and the phone, still showing Branch 2, still works in Branch 2.
        self.assertEqual(self.names(phone.get("/api/products/")), ["Paver"])
        self.assertEqual(self.names(phone.get("/api/materials/")), ["Adama sand"])

        response = phone.post(
            "/api/materials/",
            {"name": "Gravel", "unit": "KG", "quantity_in_stock": "10"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        made = RawMaterial._base_manager.get(pk=response.json()["id"])
        self.assertEqual(made.branch_id, self.b2.pk)

    def test_production_totals_follow_the_phone(self):
        main_phone = self.phone(self.owner, self.main)
        adama_phone = self.phone(self.owner, self.b2)
        main = main_phone.get("/api/production/summary/").json()
        adama = adama_phone.get("/api/production/summary/").json()
        self.assertEqual(main_phone.get("/api/production/").status_code, 200)
        self.assertEqual(
            self.names(main_phone.get("/api/materials/")), ["Main cement"]
        )
        self.assertEqual(
            self.names(adama_phone.get("/api/materials/")), ["Adama sand"]
        )
        self.assertIsInstance(main, dict)
        self.assertIsInstance(adama, dict)

    def test_two_phones_on_one_account_work_in_two_branches(self):
        adama = self.phone(self.owner, self.b2)
        head_office = self.phone(self.owner, self.main)
        self.assertEqual(self.names(adama.get("/api/products/")), ["Paver"])
        self.assertEqual(self.names(head_office.get("/api/products/")), ["Hollow block"])
        self.assertEqual(adama.get(f"/api/products/{self.block.pk}/").status_code, 404)

    def test_two_browsers_on_one_account_work_in_two_branches(self):
        adama = self.browser(self.owner, self.b2)
        head_office = self.browser(self.owner, self.main)
        page = adama.get("/inventory/products/").content.decode()
        self.assertIn("Paver", page)
        self.assertNotIn("Hollow block", page)
        page = head_office.get("/inventory/products/").content.decode()
        self.assertIn("Hollow block", page)
        self.assertNotIn("Paver", page)

    # -- Saying where you are ----------------------------------------------------
    def test_me_reports_the_branch_on_the_phones_screen(self):
        switch_branch(self.owner, self.main.pk)  # the account says main
        body = self.phone(self.owner, self.b2).get("/api/auth/me/").json()
        self.assertEqual(body["branch"]["id"], self.b2.pk)

    def test_every_answer_says_which_branch_served_it(self):
        response = self.phone(self.owner, self.b2).get("/api/products/")
        self.assertEqual(response["X-Branch"], str(self.b2.pk))
        response = self.phone(self.manager).get("/api/products/")
        self.assertEqual(response["X-Branch"], str(self.b2.pk))
        # Nothing is left switched on behind the request.
        self.assertIsNone(current_branch_id())

    def test_switching_on_the_phone_answers_in_the_new_branch(self):
        phone = self.phone(self.owner, self.main)
        response = phone.post(
            "/api/branches/switch/", {"branch": self.b2.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["user"]["branch"]["id"], self.b2.pk)
        self.assertEqual(response["X-Branch"], str(self.b2.pk))
        # The account remembers it, for a device that says nothing.
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.active_branch_id, self.b2.pk)

    def test_an_old_app_without_the_header_uses_the_accounts_branch(self):
        switch_branch(self.owner, self.b2.pk)
        self.assertEqual(self.names(self.phone(self.owner).get("/api/products/")), ["Paver"])

    # -- A branch the person may not work in ---------------------------------------
    def test_a_branch_you_do_not_work_in_is_refused_not_swapped(self):
        phone = self.phone(self.manager, self.main)  # Mary works in Adama only
        response = phone.get("/api/products/")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "branch_unavailable")

        before = RawMaterial._base_manager.count()
        response = phone.post(
            "/api/materials/", {"name": "Nope", "unit": "KG"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(RawMaterial._base_manager.count(), before)

        # Where am I, then? Answered - from the account's own branch.
        me = phone.get("/api/auth/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["branch"]["id"], self.b2.pk)

    def test_a_branch_closed_under_the_phone_is_refused(self):
        bishoftu_staff = self.manager
        from core.branches import create_branch

        bishoftu = create_branch(user=self.owner, name="Bishoftu")
        set_user_branches(bishoftu_staff, [self.b2.pk, bishoftu.pk])
        phone = self.phone(bishoftu_staff, bishoftu)
        self.assertEqual(phone.get("/api/products/").status_code, 200)

        set_branch_open(bishoftu, user=self.owner, is_open=False)
        self.assertEqual(phone.get("/api/products/").status_code, 409)
        # The phone can still find out where to go, and move there.
        self.assertEqual(phone.get("/api/branches/").status_code, 200)
        moved = phone.post(
            "/api/branches/switch/", {"branch": self.b2.pk},
            content_type="application/json",
        )
        self.assertEqual(moved.status_code, 200, moved.content)

    def test_garbage_in_the_header_is_refused(self):
        phone = self.phone(self.owner, "not-a-branch")
        self.assertEqual(phone.get("/api/products/").status_code, 409)

    def test_a_browser_whose_branch_closed_falls_back(self):
        from core.branches import create_branch

        bishoftu = create_branch(user=self.owner, name="Bishoftu")
        browser = self.browser(self.owner, bishoftu)
        set_branch_open(bishoftu, user=self.owner, is_open=False)
        switch_branch(self.owner, self.main.pk)
        page = browser.get("/inventory/products/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Hollow block", page.content.decode())

    def test_a_sale_from_the_phone_lands_in_the_phones_branch(self):
        switch_branch(self.owner, self.main.pk)
        phone = self.phone(self.owner, self.b2)
        response = phone.post(
            "/api/sales/",
            {
                "items": [{"product": self.paver.pk, "quantity": 1}],
                "amount_paid": "15.00",
                "payment_method": "CASH",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        from sales.models import Transaction

        sale = Transaction._base_manager.get(pk=response.json()["id"])
        self.assertEqual(sale.branch_id, self.b2.pk)
        self.assertEqual(Branch.objects.get(pk=sale.branch_id).name, "Adama yard")
        self.assertEqual(D(str(response.json()["total_amount"])), D("15.00"))
