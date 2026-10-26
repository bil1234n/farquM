"""
"Delete all data" - empties the branch the admin is working in, and nothing
else. Users, customers and every other branch stay exactly as they were.
"""
from decimal import Decimal as D

from django.test import Client
from rest_framework.authtoken.models import Token

from accounts.models import AuditLog, User
from core.branching import using_branch
from core.test_branches import BranchTestBase
from credit.models import CreditAccount, DebtRecord
from expenses.models import Expense
from inventory.models import Category, Product, StockMovement
from production.models import MaterialMovement, ProductionRun, RawMaterial, Recipe, RecipeItem
from production.services import receive_material, record_production
from sales.models import Customer, Transaction
from sales.services import create_sale

URL = "/api/system/wipe-data/"
GOOD = {"password": "pw", "confirm": "DELETE"}


class WipeBranchDataTests(BranchTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Give Branch 2 a bit of everything: a credit sale (a debt), a store of
        # materials, a recipe, and a batch with labour paid as an expense.
        with using_branch(cls.b2):
            cls.b2_category = Category.objects.create(name="Pavers")
            create_sale(
                user=cls.owner, customer=cls.kebede,
                cart=[{"product": cls.paver, "quantity": 2, "unit_price": D("15.00")}],
                amount_paid=D("0.00"), payment_method="CREDIT",
            )
            cls.sand = RawMaterial.objects.create(
                name="Sand", code="SND", unit="KG", unit_cost=D("2.00"), owner=cls.owner,
            )
            receive_material(cls.sand, D("100"), user=cls.owner, unit_cost=D("2.00"))
            recipe = Recipe.objects.create(product=cls.paver, output_quantity=10)
            RecipeItem.objects.create(recipe=recipe, material=cls.sand, quantity=D("5"))
            record_production(
                product=cls.paver, quantity_produced=10,
                materials=[{"material": cls.sand, "quantity": D("5")}],
                user=cls.owner,
                expenses=[{"category_name": "Labour", "amount": "50.00"}],
            )

    def phone(self, user, branch):
        token, _ = Token.objects.get_or_create(user=user)
        return Client(
            HTTP_AUTHORIZATION=f"Token {token.key}", HTTP_X_BRANCH=str(branch.pk)
        )

    def counts(self, branch):
        b = branch.pk
        return {
            "products": Product._base_manager.filter(branch=b).count(),
            "categories": Category._base_manager.filter(branch=b).count(),
            "stock moves": StockMovement._base_manager.filter(product__branch=b).count(),
            "sales": Transaction._base_manager.filter(branch=b).count(),
            "debts": DebtRecord._base_manager.filter(customer__branch=b).count(),
            "materials": RawMaterial._base_manager.filter(branch=b).count(),
            "material moves": MaterialMovement._base_manager.filter(material__branch=b).count(),
            "batches": ProductionRun._base_manager.filter(product__branch=b).count(),
            "recipes": Recipe._base_manager.filter(product__branch=b).count(),
            "expenses": Expense._base_manager.filter(branch=b).count(),
            "customers": Customer._base_manager.filter(branch=b).count(),
        }

    def test_it_empties_this_branch_and_only_this_branch(self):
        before_b2 = self.counts(self.b2)
        before_main = self.counts(self.main)
        # The fixture really did fill Branch 2 with every kind of record.
        for name, n in before_b2.items():
            self.assertGreater(n, 0, name)
        users = User.objects.count()

        response = self.phone(self.owner, self.b2).post(
            URL, GOOD, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["branch"], "Adama yard")

        after = self.counts(self.b2)
        for name, n in after.items():
            if name == "customers":
                continue
            self.assertEqual(n, 0, f"{name} left in Branch 2")
        # Customers stay, owing nothing now.
        self.assertEqual(after["customers"], before_b2["customers"])
        account = CreditAccount._base_manager.get(customer=self.kebede)
        self.assertEqual(account.outstanding_balance, D("0"))
        # The main branch is untouched, and so are the people.
        self.assertEqual(self.counts(self.main), before_main)
        self.assertTrue(Product._base_manager.filter(pk=self.block.pk).exists())
        self.assertTrue(Transaction._base_manager.filter(pk=self.main_sale.pk).exists())
        self.assertEqual(User.objects.count(), users)
        # And the log says what happened.
        self.assertTrue(
            AuditLog._base_manager.filter(description__contains="Adama yard").exists()
        )

    def test_it_follows_the_branch_on_the_phone_not_the_account(self):
        # The account was last switched to the main branch somewhere else;
        # the phone says Branch 2, so Branch 2 is what goes.
        from core.branches import switch_branch

        switch_branch(self.owner, self.main.pk)
        before_main = self.counts(self.main)
        self.phone(self.owner, self.b2).post(URL, GOOD, content_type="application/json")
        self.assertEqual(self.counts(self.main), before_main)
        self.assertEqual(self.counts(self.b2)["products"], 0)

    def test_a_wrong_password_deletes_nothing(self):
        before = self.counts(self.b2)
        response = self.phone(self.owner, self.b2).post(
            URL, {"password": "wrong", "confirm": "DELETE"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.counts(self.b2), before)

    def test_the_confirmation_word_is_required(self):
        before = self.counts(self.b2)
        response = self.phone(self.owner, self.b2).post(
            URL, {"password": "pw"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.counts(self.b2), before)

    def test_only_an_admin_may(self):
        before = self.counts(self.b2)
        response = self.phone(self.manager, self.b2).post(
            URL, GOOD, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.counts(self.b2), before)

    def test_the_branch_can_be_used_again_afterwards(self):
        phone = self.phone(self.owner, self.b2)
        phone.post(URL, GOOD, content_type="application/json")
        response = phone.post(
            "/api/materials/", {"name": "Sand", "unit": "KG"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            RawMaterial._base_manager.get(pk=response.json()["id"]).branch_id, self.b2.pk
        )
