"""
Tests for the Audit (reports/audit.py, the web page and the API).

    python manage.py test reports

A small block yard, built through the real services so every ledger row is
what production would write:

    bought      1,000 kg cement at 18 and 10 m3 sand at 900      27,000
    made        60 blocks from 50 kg cement and 0.18 m3 sand      1,062
                + labour 300 and electricity 60 paid on the batch   360
                -> 23.70 a block
    paid        rent 1,000 and a wage of 2,000                     3,000
    sold        40 blocks for cash at 32                          1,280
                10 blocks at 32, 100 down, the rest on credit       320
    collected   120 of the 220 owed

Each test names the figure it protects. The ones about money in and profit
matter most: they are the ones an owner checks against his own notebook.
"""
import datetime as dt
from decimal import Decimal as D

from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import AuditLog, User
from accounts.roles import ensure_system_roles
from credit.services import record_repayment
from expenses.models import Employee
from expenses.services import record_expense
from inventory.models import Product, ProductCostEstimate
from production import services as yard
from production.models import RawMaterial
from sales.models import Customer
from sales.services import create_sale

from .audit import (
    Period,
    build_report,
    buckets,
    resolve_period,
    running_candles,
    set_your_cost,
)


class AuditTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()
        cls.owner = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.rival = User.objects.create_user("rob", password="pw", role="MANAGER")
        cls.sales = User.objects.create_user(
            "sam", password="pw", role="SALES", manager=cls.manager
        )
        cls.keeper = User.objects.create_user("kebede", password="pw", role="STOCK_KEEPER")

        cls.block = Product.objects.create(
            name="Hollow Block 20cm", sku="HB20", selling_price=D("32.00"),
            cost_price=D("0.00"), stock_quantity=0, owner=cls.manager,
        )
        cls.cement = RawMaterial.objects.create(
            name="Cement", code="CEM", unit="KG", unit_cost=D("18.00"), owner=cls.manager,
        )
        cls.sand = RawMaterial.objects.create(
            name="Sand", code="SAND", unit="M3", unit_cost=D("900.00"), owner=cls.manager,
        )
        cls.customer = Customer.objects.create(
            name="Abebe", phone="0911", owner=cls.sales, is_credit_approved=True,
        )
        cls.worker = Employee.objects.create(name="Tesfaye", monthly_salary=D("2000.00"))

    def setUp(self):
        from core.models import forget_labels

        # Unit names are cached per process; start from the database's.
        forget_labels()
        yard.receive_material(self.cement, D("1000"), user=self.manager, unit_cost=D("18.00"))
        yard.receive_material(self.sand, D("10"), user=self.manager, unit_cost=D("900.00"))
        self.run = self.make_batch()
        record_expense(user=self.manager, data={"amount": "1000", "category_name": "Rent"})
        record_expense(user=self.manager, data={"amount": "2000", "employee": self.worker.pk})
        self.block.refresh_from_db()
        create_sale(
            user=self.sales, customer=self.customer,
            cart=[{"product": self.block, "quantity": 40, "unit_price": D("32.00")}],
            amount_paid=D("1280.00"), payment_method="CASH",
        )
        self.credit_sale = create_sale(
            user=self.sales, customer=self.customer,
            cart=[{"product": self.block, "quantity": 10, "unit_price": D("32.00")}],
            amount_paid=D("100.00"), payment_method="CASH",
        )
        record_repayment(debt=self.credit_sale.debt_record, amount=D("120.00"), user=self.sales)
        self.block.refresh_from_db()

    def make_batch(self, produced=60, labour="300", produced_on=None):
        return yard.record_production(
            product=self.block,
            quantity_produced=produced,
            materials=[
                {"material": self.cement, "quantity": D("50")},
                {"material": self.sand, "quantity": D("0.180")},
            ],
            user=self.manager,
            produced_on=produced_on,
            expenses=[
                {"category_name": "Labour", "amount": labour},
                {"category_name": "Electricity", "amount": "60"},
            ],
        )

    def report(self, user=None, key="30d", **kw):
        user = user or self.owner
        return build_report(user, resolve_period(key, user=user), **kw)

    def row(self, report, product=None):
        product = product or self.block
        return next(r for r in report["costing"] if r["id"] == product.pk)

    def as_(self, user):
        client = Client()
        client.force_login(user)
        return client


class MoneyTests(AuditTestBase):
    def test_money_out_is_purchases_and_every_expense(self):
        out = self.report()["money_out"]
        self.assertEqual(out["materials"], D("27000.00"))
        # Rent 1,000 + wage 2,000 + the batch's labour 300 and electricity 60.
        self.assertEqual(out["expenses"], D("3360.00"))
        self.assertEqual(out["wages"], D("2000.00"))
        self.assertEqual(out["running"], D("1360.00"))
        self.assertEqual(out["in_batches"], D("360.00"))
        self.assertEqual(out["total"], D("30360.00"))
        self.assertEqual(sum(k["amount"] for k in out["kinds"]), out["total"])

    def test_cement_is_spent_when_bought_not_again_when_used(self):
        # The batch used 1,062 of materials; buying them was the money out.
        self.assertEqual(self.report()["money_out"]["materials"], D("27000.00"))
        self.assertEqual(self.report()["production"]["materials_used"], D("1062.00"))

    def test_money_in_counts_each_payment_once(self):
        came = self.report()["money_in"]
        # The credit sale's amount_paid is now 220 (100 down + 120 later);
        # counting it as it stands would count the 120 twice.
        self.assertEqual(came["at_till"], D("1380.00"))
        self.assertEqual(came["repaid"], D("120.00"))
        self.assertEqual(came["total"], D("1500.00"))

    def test_profit_after_all_costs_matches_the_profit_report(self):
        pr = self.report()["profit"]
        self.assertEqual(pr["revenue"], D("1600.00"))
        self.assertEqual(pr["cost_of_sold"], D("1185.00"))  # 50 x 23.70
        # Running costs leave out the 360 already inside each block's cost.
        self.assertEqual(pr["running_costs"], D("3000.00"))
        self.assertEqual(pr["profit"], D("-2585.00"))

        today = timezone.localdate()
        api = self.as_(self.owner).get(
            "/api/reports/profit/",
            {"date_from": (today - dt.timedelta(days=29)).isoformat(),
             "date_to": today.isoformat()},
        ).json()
        self.assertEqual(D(api["net_profit"]), pr["profit"])

    def test_what_is_on_hand(self):
        held = self.report()["holdings"]
        # 950 kg cement at 18, 9.82 m3 sand at 900.
        self.assertEqual(held["materials_value"], D("25938.00"))
        # 10 blocks left, at what a batch said they cost.
        self.assertEqual(held["products_value"], D("237.00"))
        self.assertEqual(held["products_retail"], D("320.00"))
        self.assertEqual(held["owed"], D("100.00"))

    def test_a_voided_sale_and_a_reversed_batch_count_for_nothing(self):
        from sales.services import void_transaction

        void_transaction(self.credit_sale, user=self.owner, reason="Entered twice")
        pr = self.report()["profit"]
        self.assertEqual(pr["revenue"], D("1280.00"))

        extra = self.make_batch(produced=30)
        yard.reverse_production(extra, user=self.owner, reason="Wrong mix")
        self.assertEqual(self.row(self.report())["produced"], 60)


class CandleTests(AuditTestBase):
    def test_each_cash_candle_opens_where_the_last_closed(self):
        report = self.report()
        candles = report["cash_candles"]
        self.assertEqual(len(candles), 30)
        for before, after in zip(candles, candles[1:]):
            self.assertEqual(after["open"], before["close"])
        net = report["money_in"]["total"] - report["money_out"]["total"]
        self.assertEqual(candles[-1]["close"], net)
        for c in candles:
            self.assertLessEqual(c["low"], min(c["open"], c["close"]))
            self.assertGreaterEqual(c["high"], max(c["open"], c["close"]))

    def test_the_wick_follows_the_order_money_moved(self):
        period = Period(dt.date(2026, 1, 1), dt.date(2026, 1, 1), "custom")
        morning = dt.datetime(2026, 1, 1, 9)
        events = [
            (morning, D("-500")),
            (morning.replace(hour=11), D("800")),
            (morning.replace(hour=15), D("-100")),
        ]
        (c,) = running_candles(events, period)
        self.assertEqual((c["open"], c["low"], c["high"], c["close"]),
                         (D("0.00"), D("-500.00"), D("300.00"), D("200.00")))
        self.assertEqual((c["money_in"], c["money_out"]), (D("800.00"), D("600.00")))

    def test_cost_candles_show_every_batch(self):
        today = timezone.localdate()
        self.make_batch(labour="900", produced_on=today)  # dearer: 33.70 a block
        report = self.report(product_id=self.block.pk)
        drawn = [c for c in report["cost_candles"] if c["open"] is not None]
        self.assertEqual(len(drawn), 1)  # both batches fell on one day
        (c,) = drawn
        self.assertEqual(c["count"], 2)
        self.assertEqual((c["open"], c["close"]), (D("23.70"), D("33.70")))
        self.assertEqual((c["low"], c["high"]), (D("23.70"), D("33.70")))

        row = self.row(report)
        self.assertEqual(row["batch_trend"], D("42.2"))
        self.assertIn("cost_rising", [i["code"] for i in report["insights"]])

    def test_buckets_cover_the_period_exactly(self):
        p = Period(dt.date(2026, 1, 15), dt.date(2026, 3, 10), "custom")
        self.assertEqual(p.bucket, "week")
        spans = buckets(p)
        self.assertEqual(spans[0][0], p.start)
        self.assertEqual(spans[-1][1], p.end)
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertEqual(start, end + dt.timedelta(days=1))
        year = Period(dt.date(2025, 1, 1), dt.date(2025, 12, 31), "custom")
        self.assertEqual(year.bucket, "month")
        self.assertEqual(len(buckets(year)), 12)


class CostingTests(AuditTestBase):
    def test_the_suggestion_adds_a_share_of_running_costs(self):
        row = self.row(self.report())
        self.assertEqual(row["materials_per_unit"], D("17.70"))
        self.assertEqual(row["extras_per_unit"], D("6.00"))
        self.assertEqual(row["base_per_unit"], D("23.70"))
        # The only product, so it carries all 3,000 of running costs, spread
        # over the larger of 60 made and 50 sold.
        self.assertEqual(row["running_per_unit"], D("50.00"))
        self.assertEqual(row["suggested"], D("73.70"))
        self.assertTrue(row["below_cost"])
        self.assertEqual(row["cost_basis"], "suggested")

    def test_running_costs_are_shared_by_the_work_each_product_was(self):
        slab = Product.objects.create(
            name="Paving Slab", sku="PS6", selling_price=D("60.00"),
            cost_price=D("40.00"), stock_quantity=20, owner=self.manager,
        )
        create_sale(
            user=self.sales, customer=self.customer,
            cart=[{"product": slab, "quantity": 20, "unit_price": D("60.00")}],
            amount_paid=D("1200.00"), payment_method="CASH",
        )
        report = self.report()
        block, slab_row = self.row(report), self.row(report, slab)
        # Work: blocks 60 x 23.70 = 1,422; slabs 20 x 40 = 800.
        shared = block["running_per_unit"] * 60 + slab_row["running_per_unit"] * 20
        # Each share is rounded to the cent, so the total may be a few
        # cents either side of the 3,000 it was cut from.
        self.assertAlmostEqual(float(shared), 3000.0, delta=0.5)
        self.assertEqual(slab_row["base_source"], "system")
        self.assertGreater(slab_row["running_per_unit"], block["running_per_unit"])

    def test_your_cost_drives_the_margins(self):
        set_your_cost(self.block, "30.00", user=self.owner, note="Wages and power")
        row = self.row(self.report())
        self.assertEqual(row["your_cost"], D("30.00"))
        self.assertEqual(row["your_cost_note"], "Wages and power")
        self.assertEqual(row["margin_at_your_cost"], D("6.3"))
        # 50 sold for 1,600 against 50 x 30.
        self.assertEqual(row["profit_at_your_cost"], D("100.00"))
        self.assertFalse(row["below_cost"])
        self.assertEqual(row["cost_basis"], "yours")
        # On-hand stock is valued at the owner's figure too.
        self.assertEqual(self.report()["holdings"]["products_value"], D("300.00"))

    def test_your_cost_is_kept_with_its_history_and_logged(self):
        set_your_cost(self.block, "30", user=self.owner)
        set_your_cost(self.block, "31.5", user=self.owner)
        set_your_cost(self.block, None, user=self.owner)
        history = list(ProductCostEstimate.objects.filter(product=self.block))
        self.assertEqual([h.cost for h in history], [None, D("31.50"), D("30.00")])
        self.assertEqual(history[0].previous, D("31.50"))
        self.assertEqual(history[1].system_cost, D("23.70"))
        self.block.refresh_from_db()
        self.assertIsNone(self.block.audit_cost)
        self.assertTrue(
            AuditLog.objects.filter(description__contains="Changed the cost of one").exists()
        )
        with self.assertRaises(PermissionError):
            history[0].delete()

    def test_your_cost_never_touches_the_cost_price(self):
        """The profit report takes running costs off separately - folding
        them into cost_price as well would count them twice."""
        set_your_cost(self.block, "70", user=self.owner)
        self.block.refresh_from_db()
        self.assertEqual(self.block.cost_price, D("23.70"))

    def test_findings(self):
        codes = [i["code"] for i in self.report()["insights"]]
        self.assertIn("below_cost", codes)
        self.assertIn("costs_missing", codes)
        self.assertIn("loss", codes)
        self.assertEqual(self.report()["insights"][0]["level"], "critical")

        set_your_cost(self.block, "30", user=self.owner)
        codes = [i["code"] for i in self.report()["insights"]]
        self.assertNotIn("costs_missing", codes)
        # 30 against a suggested 73.70.
        self.assertIn("estimate_low", codes)
        # At 30 the blocks made 100; the books say -2,585.
        self.assertIn("estimates_too_low", codes)

    def test_a_cost_that_is_not_a_number_is_refused(self):
        from .audit import CostError

        for bad in ("abc", "-1", "99999999999"):
            with self.assertRaises(CostError):
                set_your_cost(self.block, bad, user=self.owner)


class PeriodTests(TestCase):
    def test_ranges(self):
        today = dt.date(2026, 10, 3)
        p = resolve_period("30d", today=today)
        self.assertEqual((p.start, p.end, p.days, p.bucket), (dt.date(2026, 9, 4), today, 30, "day"))
        self.assertEqual(resolve_period("90d", today=today).bucket, "week")
        self.assertEqual(resolve_period("365d", today=today).bucket, "month")
        self.assertEqual(resolve_period("nonsense", today=today).key, "90d")

    def test_custom_dates(self):
        today = dt.date(2026, 10, 3)
        p = resolve_period(date_from="2026-09-20", date_to="2026-09-01", today=today)
        self.assertEqual((p.start, p.end, p.key), (dt.date(2026, 9, 1), dt.date(2026, 9, 20), "custom"))
        future = resolve_period(date_from="2026-09-01", date_to="2027-01-01", today=today)
        self.assertEqual(future.end, today)
        p = resolve_period(date_from="2026-09-01", today=today)
        self.assertEqual(p.end, today)

    def test_since_the_start_begins_at_the_first_record(self):
        ensure_system_roles()
        owner = User.objects.create_user("owner", password="pw", role="ADMIN")
        today = timezone.localdate()
        self.assertEqual(resolve_period("all", user=owner).start, today)
        record_expense(user=owner, data={
            "amount": "50", "category_name": "Fuel",
            "spent_on": (today - dt.timedelta(days=40)).isoformat(),
        })
        p = resolve_period("all", user=owner)
        self.assertEqual(p.start, today - dt.timedelta(days=40))
        self.assertEqual(p.bucket, "week")

    def test_the_previous_period_is_the_same_length(self):
        p = Period(dt.date(2026, 9, 4), dt.date(2026, 10, 3), "30d")
        prev = p.previous()
        self.assertEqual((prev.start, prev.end, prev.days), (dt.date(2026, 8, 5), dt.date(2026, 9, 3), 30))


class AccessTests(AuditTestBase):
    def test_the_owner_and_the_manager_see_it(self):
        for user in (self.owner, self.manager):
            self.assertEqual(self.as_(user).get("/api/audit/").status_code, 200)
            self.assertEqual(self.as_(user).get("/reports/audit/").status_code, 200)

    def test_sales_and_the_stock_keeper_do_not(self):
        for user in (self.sales, self.keeper):
            self.assertEqual(self.as_(user).get("/api/audit/").status_code, 403)
            self.assertNotEqual(self.as_(user).get("/reports/audit/").status_code, 200)

    def test_only_the_owner_sets_a_cost(self):
        url = f"/api/audit/products/{self.block.pk}/cost/"
        self.assertEqual(
            self.as_(self.manager).post(url, {"cost": "30"}, content_type="application/json").status_code,
            403,
        )
        r = self.as_(self.owner).post(url, {"cost": "30", "note": "x"}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["your_cost"], "30.00")
        self.assertEqual(r.json()["history"][0]["cost"], "30.00")

        bad = self.as_(self.owner).post(url, {"cost": "-5"}, content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("below zero", bad.json()["cost"][0])

        cleared = self.as_(self.owner).post(url, {"cost": None}, content_type="application/json")
        self.assertIsNone(cleared.json()["your_cost"])

        # The web form refuses a manager the same way (and says so).
        self.as_(self.manager).post(f"/reports/audit/cost/{self.block.pk}/", {"cost": "31"})
        self.block.refresh_from_db()
        self.assertIsNone(self.block.audit_cost)

        web = self.as_(self.owner).post(
            f"/reports/audit/cost/{self.block.pk}/", {"cost": "31", "range": "30d"}
        )
        self.assertEqual(web.status_code, 302)
        self.assertIn("product=", web["Location"])
        self.block.refresh_from_db()
        self.assertEqual(self.block.audit_cost, D("31.00"))

    def test_the_web_page_shows_the_form_to_the_owner_only(self):
        page = self.as_(self.owner).get("/reports/audit/?range=30d").content.decode()
        self.assertIn("audit-cost-form", page)
        self.assertIn('class="audit-chart"', page)
        self.assertIn('class="audit-donut"', page)
        page = self.as_(self.manager).get("/reports/audit/?range=30d").content.decode()
        self.assertNotIn("audit-cost-form", page)

    def test_a_manager_sees_the_shared_stock_but_only_his_teams_expenses(self):
        record_expense(user=self.rival, data={"amount": "777", "category_name": "Fuel"})
        mine = self.report(self.manager)["money_out"]["expenses"]
        everything = self.report(self.owner)["money_out"]["expenses"]
        self.assertEqual(everything - mine, D("777.00"))
        self.assertEqual(
            self.report(self.manager)["holdings"]["materials_value"],
            self.report(self.owner)["holdings"]["materials_value"],
        )


class ApiShapeTests(AuditTestBase):
    def test_numbers_are_strings_and_dates_iso(self):
        data = self.as_(self.owner).get("/api/audit/?range=30d").json()
        self.assertEqual(data["period"]["key"], "30d")
        self.assertEqual(data["money_in"]["total"], "1500.00")
        dt.date.fromisoformat(data["period"]["start"])
        self.assertTrue(data["can_set_cost"])
        self.assertEqual(len(data["cash_candles"]), 30)
        self.assertIn("insights", data)
        self.assertFalse(self.as_(self.manager).get("/api/audit/").json()["can_set_cost"])

    def test_units_arrive_in_the_readers_language(self):
        data = self.as_(self.owner).get("/api/audit/?range=30d", HTTP_ACCEPT_LANGUAGE="am").json()
        row = next(r for r in data["costing"] if r["id"] == self.block.pk)
        self.assertEqual(row["unit_display"], "ቁራጭ")
        # A product's name is data and stays as it was typed.
        self.assertEqual(row["name"], "Hollow Block 20cm")

    def test_the_cost_candles_follow_the_product_asked_for(self):
        data = self.as_(self.owner).get(f"/api/audit/?range=30d&product={self.block.pk}").json()
        self.assertEqual(data["cost_product_id"], self.block.pk)
        data = self.as_(self.owner).get("/api/audit/?range=30d&product=99999").json()
        self.assertEqual(data["cost_product_id"], self.block.pk)


# ---------------------------------------------------------------------------
# Money out: what was deleted with its goods still in it is not money spent
# ---------------------------------------------------------------------------
class DeletedStockTests(AuditTestBase):
    """
    An owner tried things out with a raw material that turned out to be a
    mistake - received a big delivery, then deleted the material. The Audit
    went on counting the delivery as money spent: 400,000 he never spent.
    """

    def money_out(self):
        return self.report()["money_out"]

    def test_a_deleted_material_still_holding_its_delivery_is_not_counted(self):
        before = self.money_out()
        test = RawMaterial.objects.create(name="Test cement", code="TST", unit="KG",
                                          unit_cost=D("4000.00"), owner=self.manager)
        yard.receive_material(test, D("100"), user=self.manager, unit_cost=D("4000.00"))
        self.assertEqual(self.money_out()["materials"], before["materials"] + D("400000.00"))

        test.soft_delete(user=self.owner)
        after = self.money_out()
        self.assertEqual(after["materials"], before["materials"])
        self.assertEqual(after["total"], before["total"])
        self.assertEqual(after["not_counted"], D("400000.00"))
        self.assertEqual(after["not_counted_count"], 1)
        # The cash candles agree with the card.
        report = self.report()
        self.assertEqual(
            sum((c["money_out"] for c in report["cash_candles"]), D("0")), after["total"]
        )

    def test_what_was_used_before_the_delete_still_counts(self):
        before = self.money_out()["materials"]
        old = RawMaterial.objects.create(name="Old sand", code="OLD", unit="M3",
                                         unit_cost=D("100.00"), owner=self.manager)
        yard.receive_material(old, D("10"), user=self.manager, unit_cost=D("100.00"))
        yard.receive_material(old, D("5"), user=self.manager, unit_cost=D("120.00"))
        yard.waste_material(old, D("8"), user=self.manager, reason="Washed away")
        old.soft_delete(user=self.owner)
        # 7 were left: the newest delivery (5 at 120) and 2 of the first.
        # What was used - 8 at 100 - was really bought.
        out = self.money_out()
        self.assertEqual(out["materials"], before + D("800.00"))
        self.assertEqual(out["not_counted"], D("800.00"))  # 5 x 120 + 2 x 100

    def test_a_material_used_up_and_then_deleted_counts_in_full(self):
        before = self.money_out()["materials"]
        gone = RawMaterial.objects.create(name="Pigment", code="PIG", unit="KG",
                                          unit_cost=D("50.00"), owner=self.manager)
        yard.receive_material(gone, D("4"), user=self.manager, unit_cost=D("50.00"))
        yard.waste_material(gone, D("4"), user=self.manager)
        gone.soft_delete(user=self.owner)
        self.assertEqual(self.money_out()["materials"], before + D("200.00"))
        self.assertEqual(self.money_out()["not_counted"], D("0.00"))

    def test_a_deleted_product_still_holding_its_restock_is_not_counted(self):
        from inventory.services import restock

        before = self.money_out()["total"]
        trial = Product.objects.create(name="Trial pavers", sku="TP1", selling_price=D("10"),
                                       cost_price=D("6.00"), owner=self.manager)
        restock(trial, 50, user=self.manager, unit_cost=D("6.00"))
        self.assertEqual(self.money_out()["total"], before + D("300.00"))
        trial.soft_delete(user=self.owner)
        self.assertEqual(self.money_out()["total"], before)

    def test_a_switched_off_material_still_counts_on_hand(self):
        before = self.report()["holdings"]["materials_value"]
        RawMaterial.objects.filter(pk=self.sand.pk).update(is_active=False)
        self.assertEqual(self.report()["holdings"]["materials_value"], before)

    def test_goods_taken_back_for_a_debt_are_not_money_in(self):
        before = self.report()["money_in"]["total"]
        record_repayment(debt=self.credit_sale.debt_record, amount=D("50.00"),
                         user=self.sales, method="GOODS_RETURN")
        self.assertEqual(self.report()["money_in"]["total"], before)


# ---------------------------------------------------------------------------
# The owner's cost on the product pages
# ---------------------------------------------------------------------------
class OwnersCostOnProductsTests(AuditTestBase):
    def setUp(self):
        super().setUp()
        set_your_cost(self.block, "30", user=self.owner)
        self.block.refresh_from_db()

    def test_profit_margin_and_stock_value_use_it(self):
        # 10 blocks on the shelf, sold at 32, the owner says they cost 30.
        self.assertEqual(self.block.cost_price, D("23.70"))
        self.assertEqual(self.block.profit_per_unit, D("2.00"))
        self.assertEqual(self.block.margin_percent, D("6.25"))
        self.assertEqual(self.block.stock_value, D("300.00"))

    def test_the_stock_is_valued_the_same_everywhere(self):
        from reports.selectors import inventory_valuation

        self.assertEqual(inventory_valuation(user=self.owner)["cost_value"], D("300.00"))
        self.assertEqual(self.report()["holdings"]["products_value"], D("300.00"))
        from django.db.models import Sum

        # As the products list adds it up.
        total = (
            Product.objects.alive().filter(pk=self.block.pk)
            .with_stock_value().aggregate(t=Sum("stock_value"))["t"]
        )
        self.assertEqual(total, D("300.00"))

    def test_the_api_sends_both_costs(self):
        data = self.as_(self.owner).get(f"/api/products/{self.block.pk}/").json()
        self.assertEqual(data["your_cost"], "30.00")
        self.assertEqual(data["cost_used"], "30.00")
        self.assertEqual(data["cost_price"], "23.70")
        self.assertEqual(data["profit_per_unit"], "2.00")
        self.assertEqual(data["stock_value"], "300.00")
        # Nobody who may not see costs is told the owner's either.
        hidden = self.as_(self.sales).get(f"/api/products/{self.block.pk}/").json()
        self.assertNotIn("your_cost", hidden)
        self.assertNotIn("cost_used", hidden)
        # And it cannot be set through the product: that is the Audit's job.
        self.as_(self.owner).patch(
            f"/api/products/{self.block.pk}/", {"your_cost": "1.00"}, content_type="application/json"
        )
        self.block.refresh_from_db()
        self.assertEqual(self.block.audit_cost, D("30.00"))

    def test_without_it_the_cost_price_is_used(self):
        set_your_cost(self.block, None, user=self.owner)
        data = self.as_(self.owner).get(f"/api/products/{self.block.pk}/").json()
        self.assertIsNone(data["your_cost"])
        self.assertEqual(data["cost_used"], "23.70")
        self.assertEqual(data["profit_per_unit"], "8.30")

    def test_the_web_page_says_which_cost_it_is(self):
        page = self.as_(self.owner).get(f"/inventory/products/{self.block.pk}/").content.decode()
        self.assertIn("Your cost", page)
        self.assertIn("Batch cost", page)

    def test_a_sale_still_records_the_batch_cost(self):
        """The profit report takes running costs off separately; the owner's
        figure already has them in it, so a sale must not copy it."""
        sale = create_sale(
            user=self.sales, customer=self.customer,
            cart=[{"product": self.block, "quantity": 1, "unit_price": D("32.00")}],
            amount_paid=D("32.00"), payment_method="CASH",
        )
        self.assertEqual(sale.items.get().unit_cost, D("23.70"))


# ---------------------------------------------------------------------------
# The four cards, opened up
# ---------------------------------------------------------------------------
class DetailTests(AuditTestBase):
    def detail(self, kind, user=None, key="30d"):
        from .audit_detail import build_detail

        user = user or self.owner
        return build_detail(user, resolve_period(key, user=user), kind)

    def test_each_page_adds_up_to_its_card(self):
        report = self.report()
        self.assertEqual(self.detail("money-out")["total"], report["money_out"]["total"])
        self.assertEqual(self.detail("money-in")["total"], report["money_in"]["total"])
        self.assertEqual(self.detail("profit")["total"], report["profit"]["profit"])
        self.assertEqual(self.detail("on-hand")["total"], report["holdings"]["total"])

    def test_money_out_lists_every_payment(self):
        d = self.detail("money-out")
        # Two deliveries; rent, a wage, and the batch's labour and electricity.
        self.assertEqual(d["items_count"], 6)
        self.assertEqual(sum((D(str(i["amount"])) for i in d["items"]), D("0")), d["total"])
        self.assertEqual(sum((r["total"] for r in d["series"]), D("0")), d["total"])
        self.assertEqual([m["name"] for m in d["by_material"]], ["Cement", "Sand"])
        self.assertEqual(d["by_material"][0]["amount"], D("18000.00"))
        types = {i["type"] for i in d["items"]}
        self.assertEqual(types, {"delivery", "expense", "wage"})
        wage = next(i for i in d["items"] if i["type"] == "wage")
        self.assertEqual(wage["party"], "Tesfaye")
        self.assertEqual(wage["by"], "mary")
        self.assertEqual(d["not_counted"]["count"], 0)

    def test_money_in_lists_every_payment(self):
        d = self.detail("money-in")
        self.assertEqual(d["items_count"], 3)  # two sales and a repayment
        self.assertEqual(d["figures"]["at_till"], D("1380.00"))
        self.assertEqual(d["figures"]["repaid"], D("120.00"))
        self.assertEqual(d["figures"]["on_credit"], D("220.00"))
        self.assertEqual(sum((r["total"] for r in d["series"]), D("0")), d["total"])
        self.assertEqual(d["by_person"][0]["name"], "sam")
        self.assertEqual(d["by_person"][0]["amount"], D("1500.00"))

    def test_the_profit_bars_add_up_to_the_profit(self):
        d = self.detail("profit")
        self.assertEqual(sum((r["total"] for r in d["series"]), D("0")), d["total"])
        self.assertEqual(sum((r["revenue"] for r in d["series"]), D("0")), D("1600.00"))
        self.assertEqual([s["amount"] for s in d["steps"]],
                         [D("1600.00"), D("-1185.00"), D("-3000.00"), D("-2585.00")])
        row = d["products"][0]
        self.assertEqual((row["sold"], row["revenue"], row["recorded_cost"]),
                         (50, D("1600.00"), D("1185.00")))
        self.assertEqual([c["label"] for c in d["running_categories"]],
                         ["Salaries & wages", "Rent"])

    def test_on_hand_lists_what_is_held_and_owed(self):
        d = self.detail("on-hand")
        self.assertEqual(d["figures"]["owed"], D("100.00"))
        self.assertEqual(len(d["debts"]), 1)
        self.assertEqual(d["debts"][0]["balance"], D("100.00"))
        self.assertEqual(d["debts"][0]["customer"], "Abebe")
        self.assertEqual({m["name"] for m in d["materials"]}, {"Cement", "Sand"})

    def test_the_api(self):
        owner = self.as_(self.owner)
        for kind in ("money-out", "money-in", "profit", "on-hand"):
            r = owner.get(f"/api/audit/detail/{kind}/?range=30d")
            self.assertEqual(r.status_code, 200, kind)
            self.assertIsInstance(r.json()["total"], str)
        data = owner.get("/api/audit/detail/money-out/?range=30d").json()
        self.assertEqual(data["total"], "30360.00")
        self.assertEqual(len(data["series"]), 30)
        dt.datetime.fromisoformat(data["items"][0]["at"])
        self.assertEqual(owner.get("/api/audit/detail/nonsense/").status_code, 404)
        self.assertEqual(self.as_(self.manager).get("/api/audit/detail/profit/").status_code, 200)
        for user in (self.sales, self.keeper):
            self.assertEqual(self.as_(user).get("/api/audit/detail/profit/").status_code, 403)

    def test_the_web_pages(self):
        owner = self.as_(self.owner)
        for kind in ("money-out", "money-in", "profit", "on-hand"):
            page = owner.get(f"/reports/audit/{kind}/?range=30d")
            self.assertEqual(page.status_code, 200, kind)
        page = owner.get("/reports/audit/money-out/?range=30d").content.decode()
        self.assertIn('class="audit-chart"', page)
        self.assertIn("Every payment out", page)
        self.assertIn("Tesfaye", page)
        self.assertEqual(owner.get("/reports/audit/nonsense/").status_code, 404)
        self.assertNotEqual(self.as_(self.sales).get("/reports/audit/profit/").status_code, 200)
        # The Audit's cards lead here, for the same period.
        audit = owner.get("/reports/audit/?range=90d").content.decode()
        self.assertIn('href="/reports/audit/money-out/?range=90d"', audit)

    def test_deleted_deliveries_are_listed_apart(self):
        test = RawMaterial.objects.create(name="Test block mix", code="TBM", unit="KG",
                                          unit_cost=D("10.00"), owner=self.manager)
        yard.receive_material(test, D("7"), user=self.manager, unit_cost=D("10.00"))
        test.soft_delete(user=self.owner)
        d = self.detail("money-out")
        self.assertEqual(d["not_counted"]["count"], 1)
        self.assertEqual(d["not_counted"]["items"][0]["title"], "Test block mix")
        self.assertEqual(d["not_counted"]["amount"], D("70.00"))
        page = self.as_(self.owner).get("/reports/audit/money-out/?range=30d").content.decode()
        self.assertIn("not-counted", page)


# ---------------------------------------------------------------------------
# Profit at the owner's own cost
# ---------------------------------------------------------------------------
class ProfitAtYourCostTests(AuditTestBase):
    """
    The owner said one block really costs 30, everything included. Profit is
    then sales less 30 a block - not the batch's 23.70 - and the running
    costs his 30 already holds are not taken off a second time.
    """

    def setUp(self):
        super().setUp()
        set_your_cost(self.block, "30", user=self.owner)

    def test_the_audit_profit_uses_it(self):
        pr = self.report()["profit"]
        self.assertEqual(pr["revenue"], D("1600.00"))
        self.assertEqual(pr["cost_of_sold"], D("1500.00"))  # 50 x 30
        # Every block sold carries his cost, so the 3,000 of rent and wages
        # are inside it already.
        self.assertEqual(pr["running_included"], D("3000.00"))
        self.assertEqual(pr["running_costs"], D("0.00"))
        self.assertEqual(pr["profit"], D("100.00"))
        self.assertEqual(pr["margin"], D("6.3"))
        # The books, for comparison, are unchanged.
        self.assertEqual(pr["profit_recorded"], D("-2585.00"))

    def test_the_findings_compare_his_costs_with_the_books(self):
        codes = [i["code"] for i in self.report()["insights"]]
        self.assertIn("estimates_too_low", codes)
        self.assertNotIn("loss", codes)

    def test_the_dashboard_and_the_profit_report_agree(self):
        owner = self.as_(self.owner)
        financials = owner.get("/api/dashboard/").json()["financials"]
        self.assertEqual(D(financials["month_gross_profit"]), D("100.00"))
        today = timezone.localdate()
        report = owner.get(
            "/api/reports/profit/",
            {"date_from": (today - dt.timedelta(days=29)).isoformat(),
             "date_to": today.isoformat()},
        ).json()
        self.assertEqual(D(report["cogs"]), D("1500.00"))
        self.assertEqual(D(report["net_profit"]), D("100.00"))
        self.assertEqual(D(report["expenses_in_your_costs"]), D("3000.00"))
        self.assertEqual(D(report["net_profit_recorded"]), D("-2585.00"))
        self.assertEqual(D(report["net_profit"]), self.report()["profit"]["profit"])

    def test_each_sale_too(self):
        self.credit_sale.refresh_from_db()
        self.assertEqual(self.credit_sale.total_cost, D("300.00"))
        self.assertEqual(self.credit_sale.gross_profit, D("20.00"))
        # What the sale recorded stays as it was.
        self.assertEqual(self.credit_sale.items.get().unit_cost, D("23.70"))

    def test_the_profit_page_adds_up(self):
        from .audit_detail import build_detail

        d = build_detail(self.owner, resolve_period("30d", user=self.owner), "profit")
        self.assertEqual(d["total"], D("100.00"))
        self.assertEqual(sum((r["total"] for r in d["series"]), D("0")), D("100.00"))
        row = d["products"][0]
        self.assertEqual((row["cost"], row["profit"], row["at_your_cost"]),
                         (D("1500.00"), D("100.00"), True))
        self.assertEqual(d["figures"]["profit_recorded"], D("-2585.00"))

    def test_only_the_share_his_costs_cover_is_left_in(self):
        """A second product without his cost brings in half the sales, so
        half the running costs are still taken off."""
        slab = Product.objects.create(name="Slab", sku="SL1", selling_price=D("1600.00"),
                                      cost_price=D("1000.00"), stock_quantity=0,
                                      owner=self.manager)
        from inventory.services import restock

        restock(slab, 1, user=self.manager, unit_cost=D("1000.00"))
        create_sale(
            user=self.sales, customer=self.customer,
            cart=[{"product": slab, "quantity": 1, "unit_price": D("1600.00")}],
            amount_paid=D("1600.00"), payment_method="CASH",
        )
        pr = self.report()["profit"]
        self.assertEqual(pr["revenue"], D("3200.00"))
        self.assertEqual(pr["cost_of_sold"], D("2500.00"))  # 1,500 + 1,000
        self.assertEqual(pr["running_included"], D("1500.00"))
        self.assertEqual(pr["running_costs"], D("1500.00"))
        self.assertEqual(pr["profit"], D("-800.00"))  # 3,200 - 2,500 - 1,500

    def test_without_his_cost_nothing_changes(self):
        set_your_cost(self.block, None, user=self.owner)
        pr = self.report()["profit"]
        self.assertEqual(pr["profit"], D("-2585.00"))
        self.assertEqual(pr["running_included"], D("0.00"))


# ---------------------------------------------------------------------------
# Putting right a delivery that was entered wrong
# ---------------------------------------------------------------------------
class CorrectionTests(AuditTestBase):
    """
    The owner's case: two deliveries of 48 bags were typed in by mistake,
    and the store was counted afterwards, so the stock is already right -
    only the money is wrong.
    """

    def setUp(self):
        super().setUp()
        self.wrong = yard.receive_material(self.cement, D("48"), user=self.manager,
                                           unit_cost=D("18.00"))
        # Somebody counted the store afterwards: the 48 were never there.
        yard.recount_material(self.cement, self.cement.quantity_in_stock - D("48"),
                              user=self.manager)
        self.cement.refresh_from_db()

    def money_out(self):
        return self.report()["money_out"]

    def fix(self, user=None, **data):
        return self.as_(user or self.owner).post(
            f"/api/audit/corrections/material/{self.wrong.pk}/", data,
            content_type="application/json",
        )

    def test_a_delivery_that_never_happened_stops_counting(self):
        self.assertEqual(self.money_out()["materials"], D("27864.00"))  # 27,000 + 48 x 18
        r = self.fix(quantity="0", note="Typed by mistake")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.money_out()["materials"], D("27000.00"))
        # The store was right already and is left alone.
        stock = self.cement.quantity_in_stock
        self.cement.refresh_from_db()
        self.assertEqual(self.cement.quantity_in_stock, stock)
        # What was typed is still on the row, and the change is in the log.
        self.wrong.refresh_from_db()
        self.assertEqual(self.wrong.quantity_delta, D("48"))
        self.assertEqual(self.wrong.corrected_quantity, D("0"))
        self.assertEqual(self.wrong.correction_note, "Typed by mistake")
        self.assertTrue(AuditLog.objects.filter(description__contains="never happened").exists())

    def test_the_line_stays_listed_marked_cancelled(self):
        from .audit_detail import build_detail

        self.fix(quantity="0")
        d = build_detail(self.owner, resolve_period("30d", user=self.owner), "money-out")
        line = next(i for i in d["items"] if i["id"] == self.wrong.pk and i["type"] == "delivery")
        self.assertTrue(line["cancelled"])
        self.assertEqual(line["amount"], D("0.00"))
        self.assertEqual(line["recorded_quantity"], D("48"))
        self.assertEqual(line["corrected_by"], "owner")

    def test_a_wrong_quantity_and_price(self):
        self.fix(quantity="4.8", unit_cost="20")
        self.assertEqual(self.money_out()["materials"], D("27096.00"))  # 27,000 + 4.8 x 20
        self.wrong.refresh_from_db()
        self.assertEqual(self.wrong.counted_unit_cost, D("20.00"))

    def test_changing_the_store_too(self):
        stock = self.cement.quantity_in_stock
        # A second wrong delivery nobody has counted away yet.
        other = yard.receive_material(self.cement, D("10"), user=self.manager,
                                      unit_cost=D("18.00"))
        r = self.as_(self.owner).post(
            f"/api/audit/corrections/material/{other.pk}/",
            {"quantity": "4", "change_stock": True}, content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.cement.refresh_from_db()
        self.assertEqual(self.cement.quantity_in_stock, stock + D("4"))

    def test_the_store_is_never_taken_below_zero(self):
        yard.recount_material(self.cement, D("5"), user=self.manager)
        r = self.fix(quantity="0", change_stock=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("counted", r.json()["detail"])
        self.wrong.refresh_from_db()
        self.assertIsNone(self.wrong.corrected_at)

    def test_putting_it_back_clears_the_correction(self):
        self.fix(quantity="0")
        self.fix(quantity="48", unit_cost="18.00")
        self.wrong.refresh_from_db()
        self.assertIsNone(self.wrong.corrected_at)
        self.assertEqual(self.money_out()["materials"], D("27864.00"))

    def test_the_latest_price_is_put_right_on_the_material(self):
        self.fix(quantity="48", unit_cost="19.50")
        self.cement.refresh_from_db()
        self.assertEqual(self.cement.unit_cost, D("19.50"))

    def test_who_may_correct(self):
        self.assertEqual(self.fix(user=self.manager, quantity="0").status_code, 200)
        for user in (self.sales, self.keeper):
            self.assertEqual(self.fix(user=user, quantity="1").status_code, 403)
        self.assertEqual(self.fix(quantity="-1").status_code, 400)
        self.assertEqual(self.fix(quantity="lots").status_code, 400)
        r = self.as_(self.owner).post(
            "/api/audit/corrections/material/999999/", {"quantity": "1"},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        # Only deliveries: a sale's stock movement cannot be "corrected" here.
        from inventory.models import StockMovement

        sale_row = StockMovement.objects.filter(movement_type="SALE").first()
        r = self.as_(self.owner).post(
            f"/api/audit/corrections/product/{sale_row.pk}/", {"quantity": "1"},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_a_restock_can_be_corrected_too(self):
        from inventory.services import restock

        before = self.money_out()["total"]
        row = restock(self.block, 20, user=self.manager, unit_cost=D("25.00"))
        self.assertEqual(self.money_out()["total"], before + D("500.00"))
        r = self.as_(self.owner).post(
            f"/api/audit/corrections/product/{row.pk}/", {"quantity": "2"},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.money_out()["total"], before + D("50.00"))
        self.assertEqual(
            self.as_(self.owner).post(
                f"/api/audit/corrections/product/{row.pk}/", {"quantity": "2.5"},
                content_type="application/json",
            ).status_code,
            400,
        )


class SearchTests(AuditTestBase):
    def lines(self, kind="money-out", **filters):
        from .audit_detail import build_detail

        return build_detail(self.owner, resolve_period("30d", user=self.owner), kind,
                            filters=filters)

    def test_words_in_any_order_and_case(self):
        d = self.lines(q="CEMENT")
        self.assertEqual([i["title"] for i in d["items"]], ["Cement"])
        self.assertEqual(d["items_matching"], 1)
        self.assertEqual(d["matching_total"], D("18000.00"))
        self.assertTrue(d["searching"])
        # The totals still cover the whole period.
        self.assertEqual(d["total"], D("30360.00"))

    def test_by_type_person_and_amount(self):
        self.assertEqual(self.lines(type="wages")["items_matching"], 1)
        self.assertEqual(self.lines(type="deliveries")["items_matching"], 2)
        self.assertEqual(self.lines(by="mary")["items_matching"], 6)
        self.assertEqual(self.lines(by="nobody")["items_matching"], 0)
        self.assertEqual(self.lines(min="1000")["items_matching"], 4)
        self.assertEqual(self.lines(min="1000", max="2000")["items_matching"], 2)
        self.assertIn("mary", self.lines()["people"])

    def test_sorting(self):
        amounts = [i["amount"] for i in self.lines(sort="largest")["items"]]
        self.assertEqual(amounts, sorted(amounts, reverse=True))
        amounts = [i["amount"] for i in self.lines(sort="smallest")["items"]]
        self.assertEqual(amounts, sorted(amounts))

    def test_money_in_can_be_searched_too(self):
        d = self.lines("money-in", type="repayments")
        self.assertEqual(d["items_matching"], 1)
        self.assertEqual(d["items"][0]["amount"], D("120.00"))

    def test_through_the_api(self):
        data = self.as_(self.owner).get(
            "/api/audit/detail/money-out/?range=30d&q=sand&sort=oldest"
        ).json()
        self.assertEqual(data["items_matching"], 1)
        self.assertEqual(data["filters"]["sort"], "oldest")
        self.assertEqual(data["items"][0]["source"], "material")
        self.assertTrue(data["items"][0]["can_correct"])
        self.assertIn("in_store", data["items"][0])


class CorrectionPageTests(CorrectionTests):
    def test_the_web_page_corrects_and_goes_back(self):
        owner = self.as_(self.owner)
        url = f"/reports/audit/correct/material/{self.wrong.pk}/"
        page = owner.get(url + "?next=/reports/audit/money-out/%3Frange%3D30d")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Quantity really delivered")
        r = owner.post(url, {"never": "1", "next": "/reports/audit/money-out/?range=30d"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/reports/audit/money-out/?range=30d")
        self.wrong.refresh_from_db()
        self.assertEqual(self.wrong.corrected_quantity, D("0"))
        # Anywhere else is not somewhere to be sent back to.
        r = owner.post(url, {"quantity": "48", "unit_cost": "18", "next": "https://evil.example/"})
        self.assertTrue(r["Location"].startswith("/reports/audit/money-out/"))
        # A bad figure shows the form again with the reason.
        r = owner.post(url, {"quantity": "-3"})
        self.assertContains(r, "below zero")

    def test_the_list_offers_correct_and_search(self):
        page = self.as_(self.owner).get("/reports/audit/money-out/?range=30d&q=cement").content.decode()
        self.assertIn(f"/reports/audit/correct/material/{self.wrong.pk}/", page)
        self.assertIn('name="q" value="cement"', page)
        self.assertNotIn("Rent", page.split('id="payments"')[1])
        # The sales assistant cannot open the page at all.
        self.assertNotEqual(
            self.as_(self.sales).get(f"/reports/audit/correct/material/{self.wrong.pk}/").status_code, 200
        )
