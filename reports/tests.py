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
