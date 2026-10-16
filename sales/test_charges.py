"""
Extra charges on a sale: the lorry to the site, the loaders, packaging.

What these protect, in the order a shopkeeper would notice them going wrong:

  1. the customer is charged - paid now or left on their debt - and an extra
     can be what pushes a sale past a credit limit;
  2. an extra the seller put on the debt cannot be swallowed by the amount
     paid at the counter;
  3. what the business paid out for it ("We paid out") is an expense, filed
     under one heading, and cancelled with the sale;
  4. that pay-out is taken off profit exactly once - it is a cost of the
     sale, not a running cost - so the Audit neither overstates nor
     double-counts it;
  5. paying money out is still its own permission, and a seller never sees
     what the lorry cost.
"""
from decimal import Decimal as D

from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import User
from accounts.roles import ensure_system_roles
from core.models import Option
from expenses.models import Expense
from expenses.services import record_expense
from inventory.models import Product

from .models import Customer, ExtraCharge, Transaction
from .services import PAID_OUT_CATEGORY, SaleError, create_sale, void_transaction


class ExtraChargeTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()
        cls.owner = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.sales = User.objects.create_user(
            "sam", password="pw", role="SALES", manager=cls.manager
        )
        cls.block = Product.objects.create(
            name="Hollow block", sku="HB", selling_price=D("32.00"),
            cost_price=D("20.00"), stock_quantity=5000, owner=cls.manager,
        )
        cls.customer = Customer.objects.create(
            name="Abebe", phone="0911", owner=cls.sales, is_credit_approved=True,
        )

    def sell(self, *, user=None, blocks=100, paid=None, extras=None, customer=True):
        """100 blocks at 32, paid in full unless `paid` says otherwise."""
        user = user or self.owner
        goods = D("32.00") * blocks
        collected = sum(
            D(str(e.get("amount") or 0)) for e in (extras or []) if not e.get("on_debt")
        )
        return create_sale(
            user=user,
            customer=self.customer if customer else None,
            cart=[{"product": self.block, "quantity": blocks, "unit_price": D("32.00")}],
            amount_paid=goods + collected if paid is None else D(str(paid)),
            extra_charges=extras,
        )

    @staticmethod
    def lorry(amount="500.00", cost="400.00", **extra):
        line = {"label": "Transport", "amount": amount, "cost_amount": cost}
        line.update(extra)
        return line


class ChargedToTheCustomerTests(ExtraChargeTestBase):
    def test_an_extra_is_part_of_the_total_and_can_be_paid_now(self):
        txn = self.sell(extras=[self.lorry()])
        self.assertEqual(txn.subtotal, D("3200.00"))
        self.assertEqual(txn.extra_charge_amount, D("500.00"))
        self.assertEqual(txn.extra_charge_label, "Transport")
        self.assertFalse(txn.extra_charge_on_debt)
        self.assertEqual(txn.total_amount, D("3700.00"))
        self.assertEqual(txn.balance_due, D("0.00"))
        self.assertFalse(hasattr(txn, "debt_record") and txn.debt_record)

    def test_several_extras_each_keep_their_own_line(self):
        txn = self.sell(extras=[
            self.lorry(cost=""),
            {"label": "Loading worker", "amount": "150.00"},
        ])
        self.assertEqual(
            list(txn.extra_charges.values_list("label", "amount")),
            [("Transport", D("500.00")), ("Loading worker", D("150.00"))],
        )
        self.assertEqual(txn.extra_charge_label, "Transport, Loading worker")
        self.assertEqual(txn.total_amount, D("3850.00"))

    def test_an_extra_put_on_debt_goes_on_the_debt(self):
        txn = self.sell(paid="3200.00", extras=[self.lorry(on_debt=True)])
        self.assertTrue(txn.extra_charge_on_debt)
        self.assertTrue(txn.extra_charges.get().on_debt)
        self.assertEqual(txn.balance_due, D("500.00"))
        self.assertEqual(txn.debt_record.principal, D("500.00"))

    def test_an_extra_on_debt_cannot_be_collected_at_the_counter(self):
        """The seller said it goes on the debt; paying it now contradicts that."""
        with self.assertRaises(SaleError):
            self.sell(paid="3700.00", extras=[self.lorry(on_debt=True)])
        self.assertFalse(Transaction.objects.exists())

    def test_an_extra_can_push_a_sale_past_the_credit_limit(self):
        """
        Checked before a single unit leaves the shelf: finding it out after
        the stock moved would be finding it out too late.
        """
        account = self.customer.credit_account
        account.credit_limit = D("300.00")
        account.save()
        with self.assertRaises(SaleError):
            self.sell(paid="3200.00", extras=[self.lorry(on_debt=True)])
        self.block.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 5000)

    def test_a_walk_in_must_pay_the_extra_as_well(self):
        with self.assertRaises(SaleError):
            self.sell(paid="3200.00", extras=[self.lorry()], customer=False)

    def test_an_extra_must_say_what_it_is_for(self):
        with self.assertRaises(SaleError):
            self.sell(extras=[{"amount": "500.00"}])

    def test_an_empty_row_left_by_the_add_button_is_ignored(self):
        txn = self.sell(extras=[{"label": "", "amount": "", "cost_amount": ""}])
        self.assertEqual(txn.extra_charges.count(), 0)
        self.assertEqual(txn.total_amount, D("3200.00"))

    def test_a_negative_extra_is_refused(self):
        with self.assertRaises(SaleError):
            self.sell(extras=[self.lorry(amount="-50")])


class PaidOutTests(ExtraChargeTestBase):
    def test_the_pay_out_becomes_an_expense(self):
        txn = self.sell(extras=[self.lorry()])
        charge = txn.extra_charges.get()
        self.assertEqual(charge.cost_amount, D("400.00"))
        self.assertEqual(charge.profit, D("100.00"))
        expense = Expense.objects.get(extra_charge=charge)
        self.assertEqual(expense.amount, D("400.00"))
        self.assertEqual(expense.category_name, PAID_OUT_CATEGORY)
        self.assertEqual(expense.spent_on, timezone.localdate())
        self.assertEqual(expense.branch_id, txn.branch_id)
        self.assertIn("Transport", expense.notes)
        self.assertIn(txn.reference, expense.notes)
        self.assertEqual(txn.extra_charge_cost, D("400.00"))

    def test_every_pay_out_is_filed_under_one_heading(self):
        self.sell(extras=[self.lorry()])
        self.sell(extras=[{"label": "Loading worker", "amount": "150", "cost_amount": "100"}])
        self.assertEqual(
            Option.objects.filter(
                group="EXPENSE_CATEGORY", label=PAID_OUT_CATEGORY
            ).count(),
            1,
        )
        self.assertEqual(
            Expense.objects.filter(category_name=PAID_OUT_CATEGORY).count(), 2
        )

    def test_no_pay_out_no_expense(self):
        """The yard's own lorry and its own men: the whole charge is income."""
        txn = self.sell(extras=[self.lorry(cost="")])
        self.assertFalse(Expense.objects.filter(extra_charge__transaction=txn).exists())
        self.assertEqual(txn.extra_charge_cost, D("0.00"))

    def test_a_pay_out_with_nothing_charged_is_refused(self):
        """That is a plain expense; the customer's bill is not the place for it."""
        with self.assertRaises(SaleError):
            self.sell(extras=[self.lorry(amount="0", cost="400.00")])
        self.assertFalse(Expense.objects.exists())

    def test_paying_money_out_needs_its_own_permission(self):
        self.assertFalse(self.sales.has_access("expense.record"))
        with self.assertRaises(SaleError):
            self.sell(user=self.sales, extras=[self.lorry()])
        # The extra itself is fine: the customer is billed.
        txn = self.sell(user=self.sales, extras=[self.lorry(cost="")])
        self.assertEqual(txn.extra_charge_amount, D("500.00"))

    def test_voiding_the_sale_cancels_the_pay_out(self):
        txn = self.sell(extras=[self.lorry()])
        void_transaction(txn, user=self.owner, reason="Entered twice")
        expense = Expense.objects.get(extra_charge__transaction=txn)
        self.assertTrue(expense.is_voided)
        self.assertIn(txn.reference, expense.void_reason)


class ProfitTests(ExtraChargeTestBase):
    """The pay-out is a cost of the sale - taken off once, and only once."""

    def test_the_sale_profit_takes_the_pay_out_off(self):
        txn = self.sell(extras=[self.lorry()])
        # 3200 of blocks at a cost of 2000, plus 500 charged for a lorry that
        # cost 400: 3700 - 2000 - 400.
        self.assertEqual(txn.total_cost, D("2400.00"))
        self.assertEqual(txn.gross_profit, D("1300.00"))
        annotated = Transaction.objects.with_profit().get(pk=txn.pk)
        self.assertEqual(annotated.profit, D("1300.00"))

    def test_an_extra_with_no_pay_out_is_all_profit(self):
        txn = self.sell(extras=[self.lorry(cost="")])
        self.assertEqual(txn.gross_profit, D("1700.00"))

    def test_the_audit_takes_it_off_once(self):
        from reports.audit import Period, profit

        record_expense(user=self.owner, data={"amount": "300", "category_name": "Rent"})
        self.sell(extras=[self.lorry()])
        today = timezone.localdate()
        pr = profit(self.owner, Period(today, today, "custom"))
        self.assertEqual(pr["revenue"], D("3700.00"))
        self.assertEqual(pr["cost_of_sold"], D("2400.00"))
        self.assertEqual(pr["service_cost"], D("400.00"))
        # Rent is the only running cost; the lorry is not counted again.
        self.assertEqual(pr["running_total"], D("300.00"))
        self.assertEqual(pr["profit"], D("1000.00"))

    def test_money_out_still_shows_the_lorry(self):
        from reports.audit import Period, _out_items

        txn = self.sell(extras=[self.lorry()])
        today = timezone.localdate()
        items, _ = _out_items(self.owner, Period(today, today, "custom"))
        rows = [i for i in items if i.get("on_sale") == txn.reference]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], D("400.00"))


class ExtraChargeApiTests(ExtraChargeTestBase):
    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def post_sale(self, user, **body):
        payload = {
            "items": [{"product": self.block.pk, "quantity": 100, "unit_price": "32.00"}],
            "customer": self.customer.pk,
            "amount_paid": "3200.00",
        }
        payload.update(body)
        return self.client_for(user).post(
            "/api/sales/", payload, content_type="application/json"
        )

    def test_the_phone_sends_an_extra_with_what_was_paid_out(self):
        response = self.post_sale(
            self.owner, extra_charges=[self.lorry(on_debt=True)]
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["extra_charge_amount"], "500.00")
        self.assertEqual(body["extra_charge_cost"], "400.00")
        self.assertEqual(body["balance_due"], "500.00")
        line = body["extra_charges"][0]
        self.assertEqual(line["label"], "Transport")
        self.assertTrue(line["on_debt"])
        self.assertEqual(line["cost_amount"], "400.00")
        self.assertEqual(line["profit"], "100.00")
        self.assertTrue(
            Expense.objects.filter(extra_charge__transaction_id=body["id"]).exists()
        )

    def test_an_older_app_still_sends_the_single_extra(self):
        response = self.post_sale(
            self.owner,
            extra_charge_amount="500.00",
            extra_charge_label="Transport",
            extra_charge_on_debt=True,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(ExtraCharge.objects.get().amount, D("500.00"))
        self.assertEqual(response.json()["balance_due"], "500.00")

    def test_a_pay_out_alone_is_explained_not_dropped(self):
        response = self.post_sale(
            self.owner, extra_charges=[self.lorry(amount="0")]
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Transaction.objects.exists())

    def test_a_seller_never_sees_what_the_lorry_cost(self):
        txn = self.sell(extras=[self.lorry()])
        txn.owner = self.sales
        txn.save(update_fields=["owner"])
        body = self.client_for(self.sales).get(f"/api/sales/{txn.pk}/").json()
        line = body["extra_charges"][0]
        self.assertEqual(line["amount"], "500.00")
        self.assertNotIn("cost_amount", line)
        self.assertNotIn("profit", line)
        self.assertNotIn("extra_charge_cost", body)


class ExtraChargeWebTests(ExtraChargeTestBase):
    def post_counter(self, user, **extra):
        client = Client()
        client.force_login(user)
        data = {
            "product_id[]": [str(self.block.pk)],
            "quantity[]": ["100"],
            "unit_price[]": ["32.00"],
            "customer": str(self.customer.pk),
            "payment_method": "CASH",
            "amount_paid": "3700.00",
            "discount_amount": "0",
            "tax_amount": "0",
            "extra_label[]": ["Transport"],
            "extra_amount[]": ["500.00"],
            "extra_pay[]": ["NOW"],
        }
        data.update(extra)
        return client.post("/sales/new/", data)

    def test_the_counter_page_posts_an_extra_with_its_pay_out(self):
        response = self.post_counter(self.owner, **{"extra_cost[]": ["400.00"]})
        self.assertEqual(response.status_code, 302, response.content[:300])
        charge = ExtraCharge.objects.get()
        self.assertEqual(charge.amount, D("500.00"))
        self.assertEqual(charge.cost_amount, D("400.00"))
        self.assertEqual(charge.transaction.total_amount, D("3700.00"))
        self.assertEqual(charge.transaction.extra_charge_cost, D("400.00"))

    def test_a_seller_cannot_slip_a_pay_out_in(self):
        """The box is not shown to them, and a crafted one is not read."""
        response = self.post_counter(self.sales, **{"extra_cost[]": ["400.00"]})
        self.assertEqual(response.status_code, 302, response.content[:300])
        self.assertEqual(ExtraCharge.objects.get().cost_amount, D("0.00"))
        self.assertFalse(Expense.objects.exists())

    def test_the_counter_page_offers_the_pay_out_box_to_the_owner(self):
        client = Client()
        client.force_login(self.owner)
        html = client.get("/sales/new/").content.decode()
        self.assertIn('id="extraBox"', html)
        self.assertIn("We paid out", html)

    def test_a_seller_is_not_offered_the_pay_out_box(self):
        client = Client()
        client.force_login(self.sales)
        html = client.get("/sales/new/").content.decode()
        self.assertIn('id="extraBox"', html)
        self.assertNotIn("We paid out", html)

    def test_the_sale_page_shows_the_pay_out_to_the_owner_only(self):
        txn = self.sell(extras=[self.lorry()])
        owner = Client()
        owner.force_login(self.owner)
        html = owner.get(f"/sales/{txn.pk}/").content.decode()
        self.assertIn("Transport", html)
        self.assertIn("We paid out", html)

        txn.owner = self.sales
        txn.save(update_fields=["owner"])
        seller = Client()
        seller.force_login(self.sales)
        html = seller.get(f"/sales/{txn.pk}/").content.decode()
        self.assertIn("Transport", html)
        self.assertNotIn("We paid out", html)
