"""
Breakage while handing goods over.

A customer buys 100 blocks; three fail as they are lifted and two crack on
the lorry, so 105 leave the stack for 100 delivered. What these protect:

  1. the customer's figures do not move - they paid for 100 and got 100;
  2. the five extra come off the shelf as damage, referenced to the trip;
  3. cancelling a hand-over recorded by mistake puts them back, so recording
     it again on the right sale does not take the same five off twice;
  4. breakage belongs to units going out - it cannot be recorded alone.
"""
from decimal import Decimal as D

from django.test import Client, TestCase

from accounts.models import User
from accounts.roles import ensure_system_roles
from inventory.models import MovementType, Product, StockMovement

from .delivery import DeliveryError, record_delivery, void_delivery
from .models import Customer
from .services import create_sale


class BreakageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()
        cls.owner = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.keeper = User.objects.create_user("kebede", password="pw", role="STOCK_KEEPER")
        cls.block = Product.objects.create(
            name="Hollow block", sku="HB", selling_price=D("32.00"),
            cost_price=D("20.00"), stock_quantity=1000, owner=cls.manager,
        )
        cls.customer = Customer.objects.create(
            name="Abebe", phone="0911", owner=cls.owner, is_credit_approved=True,
        )

    def setUp(self):
        self.sale = create_sale(
            user=self.owner, customer=self.customer,
            cart=[{"product": self.block, "quantity": 100, "unit_price": D("32.00")}],
            amount_paid=D("3200.00"),
        )
        self.item = self.sale.items.get()
        self.block.refresh_from_db()
        # 1000 on the shelf, 100 sold: 900 left before anything breaks.
        self.assertEqual(self.block.stock_quantity, 900)

    def test_breakage_comes_off_the_shelf_not_off_the_customer(self):
        delivery = record_delivery(
            self.sale, user=self.keeper,
            lines=[{"item": self.item.pk, "quantity": 100, "broken": 5}],
        )
        self.block.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 895)      # 105 gone in all
        self.assertEqual(self.item.quantity_delivered, 100)   # the customer's 100
        self.assertEqual(delivery.lines.get().broken, 5)
        movement = StockMovement.objects.get(movement_type=MovementType.DAMAGE)
        self.assertEqual(movement.quantity_delta, -5)
        self.assertEqual(movement.reference, delivery.reference)
        self.assertEqual(movement.performed_by, self.keeper)

    def test_the_everything_button_takes_breakage_too(self):
        record_delivery(
            self.sale, user=self.keeper, everything=True,
            broken={self.item.pk: 3},
        )
        self.block.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 897)

    def test_breakage_cannot_be_recorded_on_its_own(self):
        with self.assertRaises(DeliveryError):
            record_delivery(
                self.sale, user=self.keeper,
                lines=[{"item": self.item.pk, "quantity": 0, "broken": 5}],
            )
        self.block.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 900)

    def test_a_negative_breakage_is_refused(self):
        with self.assertRaises(DeliveryError):
            record_delivery(
                self.sale, user=self.keeper,
                lines=[{"item": self.item.pk, "quantity": 10, "broken": -2}],
            )

    def test_cancelling_the_hand_over_puts_the_broken_units_back(self):
        delivery = record_delivery(
            self.sale, user=self.keeper,
            lines=[{"item": self.item.pk, "quantity": 100, "broken": 5}],
        )
        void_delivery(delivery, user=self.owner, reason="Typed on the wrong sale")
        self.block.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 900)
        self.assertEqual(self.item.quantity_delivered, 0)

    def test_the_phone_sends_breakage(self):
        client = Client()
        client.force_login(self.keeper)
        response = client.post(
            "/api/deliveries/",
            {
                "sale": self.sale.pk,
                "lines": [{"item": self.item.pk, "quantity": 100}],
                "broken": [{"item": self.item.pk, "quantity": 5}],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["delivery"]["total_broken"], 5)
        self.assertEqual(body["delivery"]["lines"][0]["broken"], 5)
        self.block.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 895)

    def test_the_sale_page_posts_breakage(self):
        client = Client()
        client.force_login(self.owner)
        response = client.post(
            f"/sales/{self.sale.pk}/deliver/",
            {f"qty_{self.item.pk}": "100", f"broken_{self.item.pk}": "5"},
        )
        self.assertEqual(response.status_code, 302)
        self.block.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 895)
