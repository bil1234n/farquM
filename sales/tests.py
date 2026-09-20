"""
The browser side of round 3: hand-overs, coloured note marks, the stock
keeper's dashboard, and the light-only look.

The rules themselves are tested through the API (api/tests.py) - these check
that the web pages reach the same rules and show what they should.
"""
from decimal import Decimal

from django.test import Client, TestCase

from accounts.models import User
from accounts.roles import ensure_system_roles
from core.models import Option
from inventory.models import Category, Product

from .models import Customer, Delivery, DeliveryStatus, Transaction
from .services import create_sale


class WebRound3Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()
        cls.admin = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.sales = User.objects.create_user(
            "sam", password="pw", role="SALES", manager=cls.manager
        )
        cls.keeper = User.objects.create_user(
            "kebede", password="pw", role="STOCK_KEEPER"
        )
        category = Category.objects.create(name="Blocks")
        cls.block = Product.objects.create(
            name="Hollow block", sku="HB", category=category,
            cost_price=Decimal("20.00"), selling_price=Decimal("32.00"),
            stock_quantity=5000, owner=cls.manager,
        )
        cls.slab = Product.objects.create(
            name="Paving slab", sku="PS", category=category,
            cost_price=Decimal("40.00"), selling_price=Decimal("60.00"),
            stock_quantity=500, owner=cls.manager,
        )
        cls.customer = Customer.objects.create(
            name="Abebe", phone="0911", owner=cls.sales, is_credit_approved=True
        )

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def sale(self, blocks=1000, slabs=50, user=None):
        user = user or self.sales
        cart = [
            {"product": self.block, "quantity": blocks, "unit_price": Decimal("32.00")},
            {"product": self.slab, "quantity": slabs, "unit_price": Decimal("60.00")},
        ]
        return create_sale(
            user=user,
            cart=[line for line in cart if line["quantity"]],
            amount_paid=Decimal("32.00") * blocks + Decimal("60.00") * slabs,
            payment_method="CASH",
            customer=self.customer if user == self.sales else None,
        )

    def lines(self, txn):
        return {item.product_id: item for item in txn.items.all()}


class HandOverPageTests(WebRound3Base):
    def test_the_queue_shows_waiting_goods_to_the_stock_keeper(self):
        txn = self.sale()
        page = self.client_for(self.keeper).get("/sales/deliveries/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn(txn.reference, html)
        self.assertIn("Hollow block", html)
        self.assertIn("Units in the yard", html)

    def test_part_now_the_rest_later(self):
        txn = self.sale(blocks=1000, slabs=50)
        items = self.lines(txn)
        client = self.client_for(self.keeper)

        # Trip one: 400 blocks, no slabs.
        response = client.post(f"/sales/{txn.pk}/deliver/", {
            f"qty_{items[self.block.pk].pk}": "400",
            f"qty_{items[self.slab.pk].pk}": "",
            "received_by_name": "Abebe's driver",
            "vehicle": "3-12345",
        })
        self.assertRedirects(
            response, f"/sales/{txn.pk}/#handover", fetch_redirect_response=False
        )
        txn.refresh_from_db()
        self.assertEqual(txn.delivery_status, DeliveryStatus.PARTIAL)
        items = self.lines(txn)
        self.assertEqual(items[self.block.pk].quantity_delivered, 400)
        self.assertEqual(items[self.slab.pk].quantity_delivered, 0)

        # The sale page says what is left, and offers the rest.
        html = client.get(f"/sales/{txn.pk}/").content.decode()
        self.assertIn('id="handover"', html)
        self.assertIn("Abebe&#x27;s driver", html)
        self.assertIn(f'name="qty_{items[self.block.pk].pk}"', html)

        # Trip two: everything still waiting.
        client.post(f"/sales/{txn.pk}/deliver/", {"everything": "1"})
        txn.refresh_from_db()
        self.assertEqual(txn.delivery_status, DeliveryStatus.DELIVERED)
        self.assertEqual(Delivery.objects.filter(transaction=txn).count(), 2)

    def test_more_than_is_waiting_is_refused_and_changes_nothing(self):
        txn = self.sale(blocks=100, slabs=0)
        item = txn.items.get()
        response = self.client_for(self.keeper).post(
            f"/sales/{txn.pk}/deliver/", {f"qty_{item.pk}": "150"}, follow=True
        )
        self.assertContains(response, "Only 100 of Hollow block")
        item.refresh_from_db()
        self.assertEqual(item.quantity_delivered, 0)
        self.assertFalse(Delivery.objects.exists())

    def test_cancelling_a_hand_over_puts_the_goods_back_to_waiting(self):
        txn = self.sale(blocks=100, slabs=0)
        client = self.client_for(self.admin)
        client.post(f"/sales/{txn.pk}/deliver/", {"everything": "1"})
        delivery = Delivery.objects.get()

        # No reason, no cancellation.
        client.post(f"/sales/deliveries/{delivery.pk}/void/", {"reason": " "})
        delivery.refresh_from_db()
        self.assertFalse(delivery.is_voided)

        client.post(
            f"/sales/deliveries/{delivery.pk}/void/", {"reason": "Truck broke down"}
        )
        delivery.refresh_from_db()
        txn.refresh_from_db()
        self.assertTrue(delivery.is_voided)
        self.assertEqual(txn.delivery_status, DeliveryStatus.PENDING)
        self.assertContains(client.get(f"/sales/{txn.pk}/"), "Truck broke down")

    def test_a_seller_sees_the_box_but_cannot_hand_over(self):
        txn = self.sale(blocks=10, slabs=0)
        client = self.client_for(self.sales)
        html = client.get(f"/sales/{txn.pk}/").content.decode()
        self.assertIn('id="handover"', html)
        self.assertNotIn('id="handoverForm"', html)

        response = client.post(f"/sales/{txn.pk}/deliver/", {"everything": "1"})
        self.assertRedirects(response, "/system/forbidden/", fetch_redirect_response=False)
        txn.refresh_from_db()
        self.assertEqual(txn.delivery_status, DeliveryStatus.PENDING)

    def test_a_manager_cannot_reach_another_managers_sale(self):
        other = User.objects.create_user("marta", password="pw", role="MANAGER")
        theirs = self.sale(blocks=5, slabs=0, user=other)
        response = self.client_for(self.manager).post(
            f"/sales/{theirs.pk}/deliver/", {"everything": "1"}
        )
        self.assertEqual(response.status_code, 404)

    def test_a_manager_is_not_sent_to_the_gate(self):
        """
        Out of the box a manager may not see sales, and the queue is a list of
        sales - so no menu link, no badge, no dashboard block, and the page
        itself refuses, rather than a list of buyers with a refusal behind
        every row. Production, the manager's everyday work, stays in reach.
        """
        self.sale(blocks=10, slabs=0)
        client = self.client_for(self.manager)
        self.assertRedirects(
            client.get("/sales/deliveries/"), "/system/forbidden/",
            fetch_redirect_response=False,
        )
        html = client.get("/reports/").content.decode()
        self.assertNotIn("/sales/deliveries/", html)
        self.assertNotIn("Waiting for collection", html)
        self.assertNotIn('<div class="nav-section">Sales</div>', html)
        self.assertIn("/production/runs/", html)

    def test_a_manager_who_also_sells_gets_the_gate_back(self):
        seller = User.objects.create_user(
            "mona", password="pw", role="MANAGER", extra_permissions=["sale.view"],
        )
        client = self.client_for(seller)
        self.assertEqual(client.get("/sales/deliveries/").status_code, 200)
        self.assertIn("/sales/deliveries/", client.get("/reports/").content.decode())

    def test_the_sidebar_counts_what_is_waiting(self):
        self.sale(blocks=10, slabs=0)
        html = self.client_for(self.keeper).get("/sales/deliveries/").content.decode()
        self.assertIn("Hand-overs", html)
        self.assertRegex(html, r'bi-truck"></i> Hand-overs\s*<span class="badge text-bg-info">1</span>')


class KeeperDashboardTests(WebRound3Base):
    def test_the_stock_keeper_lands_on_the_yard(self):
        self.sale(blocks=30, slabs=0)
        html = self.client_for(self.keeper).get("/reports/").content.decode()
        self.assertIn("Yard and hand-overs", html)
        self.assertIn("Waiting In The Yard", html)
        self.assertIn("Waiting for collection", html)
        self.assertIn("icon-tile", html)
        # A stock keeper handles goods, not money.
        self.assertNotIn("Month Gross Profit", html)
        self.assertNotIn("Spent this month", html)

    def test_the_owner_sees_spending_and_the_yard(self):
        html = self.client_for(self.admin).get("/reports/").content.decode()
        self.assertIn("Spent this month", html)
        self.assertIn("Profit after expenses", html)
        self.assertIn("Waiting for collection", html)

    def test_a_manager_sees_spending_but_not_profit(self):
        html = self.client_for(self.manager).get("/reports/").content.decode()
        self.assertIn("Spent this month", html)
        self.assertNotIn("Profit after expenses", html)


class NoteMarkWebTests(WebRound3Base):
    def setUp(self):
        self.good = Option.objects.get(group="NOTE_TAG", label="Good")
        self.bad = Option.objects.get(group="NOTE_TAG", label="Bad")

    def test_the_customer_form_offers_coloured_marks(self):
        html = self.client_for(self.sales).get("/sales/customers/add/").content.decode()
        self.assertIn("data-note-tag", html)
        self.assertIn('data-color="#DC2626"', html)
        self.assertIn(">Bad<", html)

    def test_a_mark_saves_and_colours_the_note(self):
        client = self.client_for(self.sales)
        response = client.post(f"/sales/customers/{self.customer.pk}/edit/", {
            "name": "Abebe", "phone": "0911", "customer_type": "REGULAR",
            "notes": "Always pays late", "note_tag": self.bad.pk,
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.note_tag, self.bad)
        html = client.get(f"/sales/customers/{self.customer.pk}/").content.decode()
        self.assertIn("note-box is-marked", html)
        self.assertIn("--mark:#DC2626", html)
        self.assertIn("Always pays late", html)

    def test_a_switched_off_mark_stays_on_the_records_that_have_it(self):
        self.customer.note_tag = self.good
        self.customer.save()
        self.good.is_active = False
        self.good.save()
        html = self.client_for(self.sales).get(
            f"/sales/customers/{self.customer.pk}/edit/"
        ).content.decode()
        self.assertIn(f'value="{self.good.pk}"', html)
        # ...but it is not offered on a new record.
        fresh = self.client_for(self.sales).get("/sales/customers/add/").content.decode()
        self.assertNotIn(f'value="{self.good.pk}"', fresh)

    def test_the_sale_screen_saves_a_mark(self):
        client = self.client_for(self.sales)
        client.post("/sales/new/", {
            "customer": self.customer.pk,
            "payment_method": "CASH",
            "amount_paid": "32.00",
            "discount_amount": "0", "tax_amount": "0",
            "notes": "Check the delivery address",
            "note_tag": self.bad.pk,
            "product_id[]": [self.block.pk],
            "quantity[]": ["1"],
            "unit_price[]": ["32.00"],
            "line_discount[]": ["0"],
        })
        txn = Transaction.objects.latest("id")
        self.assertEqual(txn.note_tag, self.bad)

    def test_a_hand_over_can_carry_a_mark(self):
        txn = self.sale(blocks=10, slabs=0)
        self.client_for(self.keeper).post(f"/sales/{txn.pk}/deliver/", {
            "everything": "1", "notes": "Two blocks chipped", "note_tag": self.bad.pk,
        })
        self.assertEqual(Delivery.objects.get().note_tag, self.bad)

    def test_a_colour_that_is_not_a_colour_is_never_written_into_the_page(self):
        # Seven characters at most in the column, which is still room for
        # something that would break out of a style attribute.
        self.bad.color = "#0;}a{b"
        self.bad.save()
        self.customer.note_tag = self.bad
        self.customer.save()
        html = self.client_for(self.sales).get(
            f"/sales/customers/{self.customer.pk}/"
        ).content.decode()
        self.assertNotIn("#0;}a{b", html)
        self.assertIn("--mark:#64748B", html)


class LightOnlyTests(WebRound3Base):
    def test_there_is_no_dark_mode_any_more(self):
        html = self.client_for(self.admin).get("/reports/").content.decode()
        self.assertIn('data-bs-theme="light"', html)
        self.assertNotIn("themeToggle", html)
        self.assertNotIn("faruq.theme", html)
        login = Client().get("/accounts/login/").content.decode()
        self.assertNotIn("themeToggle", login)
