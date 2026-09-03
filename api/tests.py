"""
Tests for the API surface the phone app depends on.

    python manage.py test api

WHAT THESE PROTECT
------------------
The access tests live in `core.tests` and cover the rules. These cover the
*transport* - the things that break quietly when somebody edits a serializer or
a viewset without opening the app:

  1. the server answers in the language the client asked for, and never
     translates a person's name into a status label;
  2. a photo can actually be attached to a product and a receipt to a sale,
     and removing either needs a different permission from adding it;
  3. every screen the app has a button for has an endpoint that answers, with
     the same permission the web page uses;
  4. a report never sends a cost figure to somebody who may not see one.

The last is the one worth the most: a leak there is silent, and the only sign
is a sales assistant knowing the margin on a bag of cement.
"""
import datetime as dt
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from accounts.models import RegistrationPasscode, User
from accounts.roles import ensure_system_roles
from api.messages import EXACT_AM, translate
from api.renderers import translate_payload
from credit.models import DebtRecord
from inventory.models import Category, Product
from sales.models import Customer
from sales.services import create_sale


def png_bytes() -> bytes:
    """
    A real PNG, so Django's ImageField validator accepts it.

    Built rather than checked in: a binary fixture in a test directory is one
    more thing to explain, and Pillow is already a dependency because the
    model uses ImageField.
    """
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def upload(name="photo.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, png_bytes(), content_type="image/png")


class ApiTestBase(TestCase):
    """One of each role, a shelf, a customer and a sale to hang tests on."""

    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()
        cls.admin = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.sales = User.objects.create_user(
            "sam", password="pw", role="SALES", manager=cls.manager
        )

        cls.category = Category.objects.create(name="Drinks")
        cls.product = Product.objects.create(
            name="Cola",
            sku="C1",
            category=cls.category,
            cost_price=Decimal("10.00"),
            selling_price=Decimal("15.00"),
            stock_quantity=100,
            low_stock_threshold=5,
            owner=cls.manager,
        )
        cls.customer = Customer.objects.create(
            name="Abebe", phone="0911", owner=cls.sales, is_credit_approved=True
        )
        cls.sale = create_sale(
            user=cls.sales,
            customer=cls.customer,
            cart=[{"product": cls.product, "quantity": 2,
                   "unit_price": Decimal("15.00")}],
            amount_paid=Decimal("30.00"),
            payment_method="CASH",
        )

    def as_(self, user) -> Client:
        client = Client()
        client.force_login(user)
        return client


class LanguageTests(ApiTestBase):
    """The server speaks the client's language, and only where it should."""

    def test_choice_labels_come_back_in_amharic(self):
        response = self.as_(self.sales).get(
            "/api/sales/", HTTP_ACCEPT_LANGUAGE="am"
        )
        row = response.json()["results"][0]
        self.assertEqual(row["payment_status_display"], EXACT_AM["Paid"])

    def test_a_customer_name_is_never_translated(self):
        """
        The failure this catches: a customer called "Paid" coming back as
        "ተከፍሏል". Names are data, and the renderer must not touch them.
        """
        Customer.objects.create(name="Paid", phone="0900", owner=self.sales)
        response = self.as_(self.sales).get(
            "/api/customers/", HTTP_ACCEPT_LANGUAGE="am"
        )
        names = {row["name"] for row in response.json()["results"]}
        self.assertIn("Paid", names)

    def test_errors_are_translated(self):
        response = self.as_(self.sales).post(
            "/api/sales/",
            {"items": []},
            content_type="application/json",
            HTTP_ACCEPT_LANGUAGE="am",
        )
        self.assertEqual(response.status_code, 400)
        body = response.content.decode()
        self.assertNotIn("at least one item", body)

    def test_refusals_are_translated(self):
        response = self.as_(self.sales).post(
            f"/api/products/{self.product.pk}/adjust/",
            {"kind": "DAMAGE", "quantity": 1},
            content_type="application/json",
            HTTP_ACCEPT_LANGUAGE="am",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            EXACT_AM["You do not have permission to perform this action."],
        )

    def test_english_is_left_alone(self):
        response = self.as_(self.sales).get("/api/sales/")
        self.assertEqual(
            response.json()["results"][0]["payment_status_display"], "Paid"
        )

    def test_an_unknown_language_is_not_an_error(self):
        # Accept-Language comes from the outside world and can say anything.
        response = self.as_(self.sales).get(
            "/api/sales/", HTTP_ACCEPT_LANGUAGE="zz-ZZ,zz;q=0.9"
        )
        self.assertEqual(response.status_code, 200)

    def test_the_walk_leaves_unknown_strings_alone(self):
        payload = {"detail": "Paid", "name": "Paid", "nested": [{"kind_display": "Paid"}]}
        out = translate_payload(payload, "am")
        self.assertEqual(out["detail"], EXACT_AM["Paid"])
        self.assertEqual(out["name"], "Paid")
        self.assertEqual(out["nested"][0]["kind_display"], EXACT_AM["Paid"])

    def test_a_message_with_a_value_keeps_the_value(self):
        out = translate("Customer 'Abebe Kebede' is inactive.", "am")
        self.assertIn("Abebe Kebede", out)
        self.assertNotIn("is inactive", out)


class AttachmentTests(ApiTestBase):
    """Photos: the thing the app had no way to send at all."""

    def test_a_manager_can_attach_and_remove_a_product_photo(self):
        client = self.as_(self.manager)
        response = client.post(
            f"/api/products/{self.product.pk}/photo/", {"image": upload()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_image"])
        self.assertIsNotNone(response.json()["image_url"])

        response = client.delete(f"/api/products/{self.product.pk}/photo/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["has_image"])

    def test_a_sales_user_cannot_change_a_product_photo(self):
        response = self.as_(self.sales).post(
            f"/api/products/{self.product.pk}/photo/", {"image": upload()}
        )
        self.assertEqual(response.status_code, 403)

    def test_a_photo_endpoint_refuses_something_that_is_not_an_image(self):
        response = self.as_(self.manager).post(
            f"/api/products/{self.product.pk}/photo/",
            {"image": SimpleUploadedFile("x.png", b"not an image",
                                         content_type="image/png")},
        )
        self.assertEqual(response.status_code, 400)

    def test_a_sales_user_can_attach_a_receipt_but_not_delete_one(self):
        client = self.as_(self.sales)
        response = client.post(
            f"/api/sales/{self.sale.pk}/receipt/",
            {"file": upload("slip.png"), "kind": "PAYMENT"},
        )
        self.assertEqual(response.status_code, 201)
        # The response carries the fresh list, not the prefetched one from
        # before the upload - the bug that made an attached receipt invisible.
        self.assertEqual(len(response.json()["receipts"]), 1)
        receipt_id = response.json()["receipts"][0]["id"]

        self.assertEqual(
            client.delete(
                f"/api/sales/{self.sale.pk}/receipt/{receipt_id}/"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.as_(self.admin)
            .delete(f"/api/sales/{self.sale.pk}/receipt/{receipt_id}/")
            .status_code,
            204,
        )

    def test_a_receipt_appears_on_the_sale(self):
        self.as_(self.sales).post(
            f"/api/sales/{self.sale.pk}/receipt/", {"file": upload("slip.png")}
        )
        data = self.as_(self.sales).get(f"/api/sales/{self.sale.pk}/").json()
        self.assertEqual(len(data["receipts"]), 1)
        self.assertTrue(data["receipts"][0]["is_image"])


class StockCorrectionTests(ApiTestBase):
    """Damage, returns and recounts - each behind its own permission."""

    def test_a_manager_can_write_off_damage(self):
        before = self.product.stock_quantity
        response = self.as_(self.manager).post(
            f"/api/products/{self.product.pk}/adjust/",
            {"kind": "DAMAGE", "quantity": 3, "reason": "broken"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, before - 3)

    def test_a_sales_user_cannot(self):
        self.assertEqual(
            self.as_(self.sales)
            .post(
                f"/api/products/{self.product.pk}/adjust/",
                {"kind": "DAMAGE", "quantity": 1},
                content_type="application/json",
            )
            .status_code,
            403,
        )

    def test_a_client_cannot_name_any_movement_type(self):
        """
        `kind` is restricted to DAMAGE and RETURN_IN. Letting a client ask for
        RESTOCK here would be a way to invent a delivery nobody paid for.
        """
        response = self.as_(self.manager).post(
            f"/api/products/{self.product.pk}/adjust/",
            {"kind": "RESTOCK", "quantity": 50},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_a_recount_writes_the_difference(self):
        response = self.as_(self.admin).post(
            f"/api/products/{self.product.pk}/recount/",
            {"counted_quantity": 90, "reason": "stock take"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 90)

    def test_a_recount_that_matches_changes_nothing(self):
        response = self.as_(self.admin).post(
            f"/api/products/{self.product.pk}/recount/",
            {"counted_quantity": self.product.stock_quantity},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["changed"])


class ReportTests(ApiTestBase):
    """Every figure scoped, and cost columns gated."""

    def test_the_hub_lists_only_reports_you_may_open(self):
        keys = {
            card["key"]
            for card in self.as_(self.sales).get("/api/reports/").json()["cards"]
        }
        self.assertEqual(keys, {"sales"})

        admin_keys = {
            card["key"]
            for card in self.as_(self.admin).get("/api/reports/").json()["cards"]
        }
        self.assertTrue({"sales", "profit", "inventory", "receivables"} <= admin_keys)

    def test_the_inventory_report_hides_cost_from_a_sales_user(self):
        # A sales user cannot open it at all, and a role that could would
        # still get rows with no cost key.
        self.assertEqual(
            self.as_(self.sales).get("/api/reports/inventory/").status_code, 403
        )
        rows = self.as_(self.manager).get("/api/reports/inventory/").json()["products"]
        self.assertTrue(all("cost_price" in row for row in rows))

    def test_a_sales_user_sees_only_their_own_sales_report(self):
        # The manager made no sales; the assistant made one.
        mine = self.as_(self.sales).get("/api/reports/sales/").json()
        self.assertEqual(mine["transaction_count"], 1)
        # A one-row league table is pointless, so it is not sent.
        self.assertEqual(mine["by_staff"], [])

    def test_export_needs_its_own_permission(self):
        self.assertEqual(
            self.as_(self.sales).get("/api/reports/export/sales/").status_code, 403
        )
        response = self.as_(self.admin).get("/api/reports/export/sales/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])

    def test_the_export_omits_profit_columns_for_someone_who_may_not_see_them(self):
        # The manager may export but not see margins, so the header must be
        # short. The filter is on the writer, not on the link.
        header = (
            self.as_(self.manager)
            .get("/api/reports/export/sales/")
            .content.decode()
            .splitlines()[0]
        )
        self.assertNotIn("Gross profit", header)


class AuditApiTests(ApiTestBase):
    def test_the_audit_log_needs_the_permission(self):
        self.assertEqual(self.as_(self.sales).get("/api/audit-log/").status_code, 403)
        self.assertEqual(self.as_(self.admin).get("/api/audit-log/").status_code, 200)

    def test_everybody_can_read_their_own_trail(self):
        response = self.as_(self.sales).get("/api/my-activity/")
        self.assertEqual(response.status_code, 200)
        users = {entry["user"] for entry in response.json()["entries"]}
        self.assertTrue(users <= {self.sales.display_name})

    def test_the_action_filter_labels_are_translated(self):
        response = self.as_(self.admin).get(
            "/api/audit-log/", HTTP_ACCEPT_LANGUAGE="am"
        )
        labels = {row["action_display"] for row in response.json()["actions"]}
        self.assertIn(EXACT_AM["Created"], labels)


class CreditDepthTests(ApiTestBase):
    """The credit controls the phone had no way to reach."""

    def setUp(self):
        self.credit_sale = create_sale(
            user=self.sales,
            customer=self.customer,
            cart=[{"product": self.product, "quantity": 1,
                   "unit_price": Decimal("15.00")}],
            amount_paid=Decimal("0.00"),
            payment_method="CREDIT",
        )
        self.debt = DebtRecord.objects.get(transaction=self.credit_sale)

    def test_rescheduling_needs_its_own_permission(self):
        payload = {
            "due_date": (dt.date.today() + dt.timedelta(days=14)).isoformat()
        }
        self.assertEqual(
            self.as_(self.sales)
            .post(f"/api/debts/{self.debt.pk}/reschedule/", payload,
                  content_type="application/json")
            .status_code,
            403,
        )
        self.assertEqual(
            self.as_(self.admin)
            .post(f"/api/debts/{self.debt.pk}/reschedule/", payload,
                  content_type="application/json")
            .status_code,
            200,
        )

    def test_a_due_date_cannot_move_into_the_past(self):
        response = self.as_(self.admin).post(
            f"/api/debts/{self.debt.pk}/reschedule/",
            {"due_date": (dt.date.today() - dt.timedelta(days=1)).isoformat()},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_reversing_a_payment_needs_the_permission_and_a_reason(self):
        paid = self.as_(self.sales).post(
            f"/api/debts/{self.debt.pk}/pay/",
            {"amount": "5.00", "method": "CASH"},
            content_type="application/json",
        )
        self.assertEqual(paid.status_code, 201)
        repayment_id = paid.json()["repayment"]["id"]
        path = f"/api/debts/{self.debt.pk}/repayments/{repayment_id}/reverse/"

        self.assertEqual(
            self.as_(self.sales)
            .post(path, {"reason": "duplicate"}, content_type="application/json")
            .status_code,
            403,
        )
        self.assertEqual(
            self.as_(self.admin)
            .post(path, {"reason": "duplicate"}, content_type="application/json")
            .status_code,
            200,
        )

    def test_a_block_must_say_why(self):
        client = self.as_(self.admin)
        self.assertEqual(
            client.post(
                f"/api/customers/{self.customer.pk}/block/",
                {"blocked": True},
                content_type="application/json",
            ).status_code,
            400,
        )
        self.assertEqual(
            client.post(
                f"/api/customers/{self.customer.pk}/block/",
                {"blocked": True, "reason": "cheques bouncing"},
                content_type="application/json",
            ).status_code,
            200,
        )

    def test_setting_a_credit_limit_needs_credit_limits(self):
        payload = {"credit_limit": "500.00"}
        self.assertEqual(
            self.as_(self.sales)
            .post(f"/api/customers/{self.customer.pk}/credit-limit/", payload,
                  content_type="application/json")
            .status_code,
            403,
        )
        self.assertEqual(
            self.as_(self.admin)
            .post(f"/api/customers/{self.customer.pk}/credit-limit/", payload,
                  content_type="application/json")
            .status_code,
            200,
        )


class RegistrationSecurityApiTests(ApiTestBase):
    """The passcode screen, over the API the phone uses."""

    def test_reading_never_returns_a_passcode(self):
        row, _ = RegistrationPasscode.objects.get_or_create(role_code="SALES")
        row.set_passcode("shop-sales-2026")
        row.is_enabled = True
        row.save()

        body = self.as_(self.admin).get("/api/settings/registration/").content.decode()
        self.assertNotIn("shop-sales-2026", body)
        self.assertIn("has_passcode", body)

    def test_setting_a_passcode_opens_the_role(self):
        response = self.as_(self.admin).post(
            "/api/settings/registration/",
            {
                "allow_self_registration": True,
                "roles": [
                    {"code": "SALES", "passcode": "shop-sales-2026",
                     "enabled": True}
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        open_roles = {r["code"] for r in response.json()["roles"] if r["available"]}
        self.assertIn("SALES", open_roles)

    def test_a_short_passcode_is_refused_per_role(self):
        response = self.as_(self.admin).post(
            "/api/settings/registration/",
            {"roles": [{"code": "ADMIN", "passcode": "123", "enabled": True}]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("ADMIN", response.json()["errors"])

    def test_a_manager_cannot_open_the_screen(self):
        self.assertEqual(
            self.as_(self.manager).get("/api/settings/registration/").status_code,
            403,
        )


class CatalogueApiTests(ApiTestBase):
    """Categories and suppliers: readable to sell, editable to manage."""

    def test_a_sales_user_can_read_but_not_change(self):
        client = self.as_(self.sales)
        self.assertEqual(client.get("/api/categories/").status_code, 200)
        self.assertEqual(
            client.post("/api/categories/", {"name": "Snacks"},
                        content_type="application/json").status_code,
            403,
        )

    def test_a_manager_can_manage_the_catalogue(self):
        client = self.as_(self.manager)
        self.assertEqual(
            client.post("/api/categories/", {"name": "Snacks"},
                        content_type="application/json").status_code,
            201,
        )
        self.assertEqual(
            client.post("/api/suppliers/", {"name": "Wholesaler"},
                        content_type="application/json").status_code,
            201,
        )
