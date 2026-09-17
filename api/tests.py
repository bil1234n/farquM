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
from core.models import Option
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


class ProductFormTests(ApiTestBase):
    """
    The new-product form on the phone, in the shape the phone actually sends
    it: no SKU, a photo as a separate multipart request afterwards.
    """

    def test_a_product_can_be_created_without_a_sku(self):
        response = self.as_(self.manager).post(
            "/api/products/",
            {
                "name": "Wooden Bench Seat",
                "selling_price": "250.00",
                "cost_price": "180.00",
                "low_stock_threshold": 3,
                "unit": "PIECE",
                "is_active": True,
                "barcode": None,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        # Initials of the first three words, then a per-owner sequence.
        self.assertEqual(response.json()["sku"], "WBS-00001")

    def test_a_blank_sku_string_is_also_generated(self):
        """The phone sends '' when the box was touched and then cleared."""
        response = self.as_(self.manager).post(
            "/api/products/",
            {"name": "Iron Gate", "selling_price": "900.00", "sku": ""},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["sku"].startswith("IG-"))

    def test_a_typed_sku_is_kept(self):
        response = self.as_(self.manager).post(
            "/api/products/",
            {"name": "Cement", "selling_price": "800.00", "sku": "CEM-1"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["sku"], "CEM-1")

    def test_a_duplicate_sku_is_a_sentence_not_a_500(self):
        response = self.as_(self.manager).post(
            "/api/products/",
            {"name": "Another Cola", "selling_price": "16.00", "sku": "c1"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("sku", response.json())

    def test_the_duplicate_message_is_translated(self):
        response = self.as_(self.manager).post(
            "/api/products/",
            {"name": "Another Cola", "selling_price": "16.00", "sku": "C1"},
            content_type="application/json",
            HTTP_ACCEPT_LANGUAGE="am",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("You already have", response.content.decode())

    def test_a_sku_already_in_use_is_refused_whoever_owns_it(self):
        """
        One catalogue, one set of SKUs. C1 belongs to the manager, but an admin
        reusing it would put two different products on the same shelf under one
        code - and a code that identifies two things identifies neither.
        """
        response = self.as_(self.admin).post(
            "/api/products/",
            {"name": "Admin Cola", "selling_price": "16.00", "sku": "C1"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("already used", response.content.decode())

    def test_a_generated_sku_never_collides_with_someone_elses(self):
        """The generator scans the whole catalogue, not just the creator's."""
        first = self.as_(self.manager).post(
            "/api/products/",
            {"name": "Green Tea", "selling_price": "12.00"},
            content_type="application/json",
        ).json()["sku"]
        second = self.as_(self.admin).post(
            "/api/products/",
            {"name": "Green Tea", "selling_price": "12.00"},
            content_type="application/json",
        ).json()["sku"]
        self.assertNotEqual(first, second)

    def test_editing_without_a_sku_keeps_the_existing_one(self):
        response = self.as_(self.manager).patch(
            f"/api/products/{self.product.pk}/",
            {"name": "Cola 500ml", "sku": ""},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.sku, "C1")
        self.assertEqual(self.product.name, "Cola 500ml")

    def test_the_unit_can_be_set_from_the_app(self):
        response = self.as_(self.manager).patch(
            f"/api/products/{self.product.pk}/",
            {"unit": "CARTON"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["unit"], "CARTON")

    def test_the_create_then_photo_sequence_the_app_uses(self):
        created = self.as_(self.manager).post(
            "/api/products/",
            {"name": "Steel Door", "selling_price": "3200.00"},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        pk = created.json()["id"]
        self.assertFalse(created.json()["has_image"])

        photo = self.as_(self.manager).post(
            f"/api/products/{pk}/photo/", {"image": upload()}
        )
        self.assertEqual(photo.status_code, 200, photo.content)
        self.assertTrue(photo.json()["has_image"])
        self.assertTrue(photo.json()["image_url"])

    def test_the_detail_endpoint_answers_with_everything_the_screen_reads(self):
        """
        The product page renders straight from this payload. A key going
        missing here is the difference between a full record and a blank one.
        """
        response = self.as_(self.manager).get(f"/api/products/{self.product.pk}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in (
            "id", "name", "sku", "barcode", "description",
            "unit", "unit_display", "selling_price",
            "stock_quantity", "low_stock_threshold",
            "stock_status", "stock_status_label", "is_active",
            "image_url", "has_image", "category_name",
        ):
            self.assertIn(key, body, f"the app reads '{key}' off this payload")

    def test_the_movement_list_answers_for_a_product_with_no_history(self):
        fresh = Product.objects.create(
            name="Quiet Item", sku="Q1", selling_price=Decimal("5.00"),
            cost_price=Decimal("2.00"), owner=self.manager,
        )
        response = self.as_(self.manager).get(
            f"/api/products/{fresh.pk}/movements/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


class AttachmentFlowTests(ApiTestBase):
    """
    The two sequences the app performs when someone photographs a slip.

    Both post multipart to endpoints whose other fields are JSON, which is
    exactly the combination that breaks silently when a parser list is
    tightened somewhere else.
    """

    def _open_debt(self):
        credit_sale = create_sale(
            user=self.sales,
            customer=self.customer,
            cart=[{"product": self.product, "quantity": 1,
                   "unit_price": Decimal("15.00")}],
            amount_paid=Decimal("0.00"),
            payment_method="CREDIT",
        )
        return DebtRecord.objects.get(transaction=credit_sale)

    def test_a_sale_can_be_photographed_immediately_after_it_is_rung_up(self):
        """The new-sale form: create, then attach, as two requests."""
        client = self.as_(self.sales)
        created = client.post(
            "/api/sales/",
            {
                "items": [{"product": self.product.pk, "quantity": 1,
                           "unit_price": "15.00"}],
                "amount_paid": "15.00",
                "payment_method": "CASH",
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        sale_id = created.json()["id"]

        attached = client.post(
            f"/api/sales/{sale_id}/receipt/",
            {"file": upload("slip.png"), "kind": "SALE"},
        )
        self.assertEqual(attached.status_code, 201, attached.content)

        detail = client.get(f"/api/sales/{sale_id}/").json()
        self.assertEqual(len(detail["receipts"]), 1)
        self.assertTrue(detail["receipts"][0]["file_url"])

    def test_a_repayment_carries_its_proof_photo(self):
        debt = self._open_debt()
        response = self.as_(self.sales).post(
            f"/api/debts/{debt.pk}/pay/",
            {"amount": "5.00", "method": "CASH", "proof": upload("proof.png")},
        )
        self.assertEqual(response.status_code, 201, response.content)
        debt.refresh_from_db()
        self.assertEqual(debt.amount_repaid, Decimal("5.00"))

        # The slip is filed against the repayment, and the URL comes back in
        # the same response - which is what the debt page in the app reads to
        # show the thumbnail.
        proofs = response.json()["repayment"]["proofs"]
        self.assertEqual(len(proofs), 1, response.content)
        self.assertTrue(proofs[0]["file_url"])

        listed = self.as_(self.sales).get(f"/api/debts/{debt.pk}/repayments/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()[0]["proofs"]), 1, listed.content)

    def test_a_repayment_without_a_photo_still_works(self):
        """The field is optional, and staying optional is the point."""
        debt = self._open_debt()
        response = self.as_(self.sales).post(
            f"/api/debts/{debt.pk}/pay/",
            {"amount": "5.00", "method": "CASH"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_a_sales_user_without_collect_cannot_record_a_payment(self):
        debt = self._open_debt()
        self.sales.denied_permissions = ["credit.collect"]
        self.sales.save(update_fields=["denied_permissions"])
        response = self.as_(self.sales).post(
            f"/api/debts/{debt.pk}/pay/",
            {"amount": "5.00", "proof": upload("proof.png")},
        )
        self.assertEqual(response.status_code, 403, response.content)


class DashboardShapeTests(ApiTestBase):
    """
    The home screen is built from what the viewer may actually see.

    The web dashboard has always filtered its cards by permission. The phone
    took the same payload and drew a fixed layout, so a manager who never
    touches the till opened the app to "Month revenue: 0", "Owed to you: 0"
    and an empty best-sellers table - figures that were not zero, but not
    theirs to see at all.

    Gating the PAYLOAD rather than the screen is what makes that durable: a
    key that is absent cannot be rendered by a later edit, and the phone has
    nothing to hide.
    """

    def _dash(self, user):
        response = self.as_(user).get("/api/dashboard/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_a_manager_is_sent_the_shelf_and_the_plant_only(self):
        body = self._dash(self.manager)

        for key in ("inventory", "low_stock", "production"):
            self.assertIn(key, body, f"a manager should be sent {key}")

        for key in ("today", "week", "month", "trend", "recent_sales",
                    "top_products", "by_manager", "receivables",
                    "overdue_debts", "due_soon", "customers", "my_customers"):
            self.assertNotIn(
                key, body,
                f"'{key}' is somebody else's business and should not be sent "
                f"to a manager who holds no sale.view or credit.view",
            )

    def test_an_administrator_is_sent_everything(self):
        body = self._dash(self.admin)
        for key in ("today", "month", "trend", "recent_sales", "top_products",
                    "receivables", "overdue_debts", "inventory", "low_stock",
                    "customers", "my_customers", "production"):
            self.assertIn(key, body, f"an administrator should be sent {key}")

    def test_a_sales_user_is_sent_takings_but_not_the_plant(self):
        body = self._dash(self.sales)
        self.assertIn("today", body)
        self.assertIn("recent_sales", body)
        # No material.view or production.view in the Sales role.
        self.assertNotIn("production", body)

    def test_the_manager_still_gets_the_stock_layout(self):
        """
        Narrowing the role must not drop them into the fallback layout, which
        is the generic one for a role nobody anticipated.
        """
        self.assertEqual(self._dash(self.manager)["profile"], "stock")

    def test_rows_carry_the_id_needed_to_open_them(self):
        """
        A row naming a product or a person is a dead end without its id. The
        dashboard links every row it draws, so the payload has to say what
        each one points at.
        """
        body = self._dash(self.admin)
        self.assertTrue(body["top_products"], "expected the seeded sale here")
        for row in body["top_products"]:
            self.assertIsNotNone(row.get("id"), "top product row needs an id")
        for row in body.get("by_manager", []):
            self.assertIn("id", row, "staff row needs an id")
        for row in body["recent_sales"]:
            self.assertIn("id", row)
        for row in body["my_customers"]:
            self.assertIn("id", row)

    def test_the_plant_block_reports_the_month(self):
        from decimal import Decimal as D

        from production import services
        from production.models import RawMaterial

        cement = RawMaterial.objects.create(
            name="Cement", code="CEM", unit="KG", unit_cost=D("18"),
            reorder_level=D("100"), owner=self.manager,
        )
        services.receive_material(cement, D("500"), user=self.manager)
        services.record_production(
            product=self.product, quantity_produced=40, quantity_rejected=2,
            materials=[{"material": cement, "quantity": D("50")}],
            user=self.manager,
        )

        plant = self._dash(self.manager)["production"]
        self.assertEqual(plant["material_count"], 1)
        self.assertEqual(plant["runs_this_month"], 1)
        self.assertEqual(plant["produced_this_month"], 40)
        self.assertEqual(plant["rejected_this_month"], 2)


class ManagerRoleTests(ApiTestBase):
    """
    What a Manager is, now: the shelf and the plant, and nothing at the till.
    """

    STOCK_AND_PLANT = (
        "product.view", "product.create", "product.edit", "product.view_cost",
        "stock.restock", "stock.adjust", "stock.recount",
        "stock.view_movements", "catalog.manage",
        "material.view", "material.create", "material.edit",
        "material.receive", "material.adjust",
        "recipe.manage", "production.view", "production.create",
        "report.inventory",
    )

    NOT_THEIRS = (
        "sale.view", "sale.create", "sale.credit", "sale.discount",
        "customer.view", "customer.create", "customer.edit",
        "credit.view", "credit.collect", "credit.reschedule",
        "report.sales", "report.receivables", "report.profit",
        "user.view", "settings.view",
    )

    def test_a_manager_runs_the_shelf_and_the_plant(self):
        for code in self.STOCK_AND_PLANT:
            with self.subTest(permission=code):
                self.assertTrue(
                    self.manager.has_access(code),
                    f"a manager should hold {code}",
                )

    def test_a_manager_does_not_work_the_till(self):
        for code in self.NOT_THEIRS:
            with self.subTest(permission=code):
                self.assertFalse(
                    self.manager.has_access(code),
                    f"a manager should NOT hold {code} by default",
                )

    def test_the_sales_pages_refuse_them(self):
        """Hiding the link is a courtesy; the refusal is the control."""
        client = self.as_(self.manager)
        for url in ("/api/sales/", "/api/customers/", "/api/credit/overview/"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403)

    def test_the_stock_and_yard_pages_still_let_them_in(self):
        client = self.as_(self.manager)
        for url in ("/api/products/", "/api/materials/", "/api/production/"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_one_manager_can_still_be_given_the_till(self):
        """
        The narrowing is a DEFAULT, not a wall. A shop where the manager also
        serves customers grants it per person, and the role stays clean.
        """
        self.manager.extra_permissions = ["sale.view", "sale.create"]
        self.manager.save(update_fields=["extra_permissions"])
        self.manager.refresh_from_db()
        self.manager.refresh_access()

        self.assertTrue(self.manager.has_access("sale.create"))
        self.assertEqual(
            self.as_(self.manager).get("/api/sales/").status_code, 200
        )
        # And the payload follows the permission, not the role name.
        self.assertIn("today", self.as_(self.manager).get(
            "/api/dashboard/").json())


class SaleDebtLinkTests(ApiTestBase):
    """
    A credit sale says it opened a debt, so it has to be able to open it.

    The app showed "This sale opened a debt · View debt" and the button popped
    back to the home screen, because the sale payload never carried the debt's
    id - there was nothing to navigate to, so the button did the only thing it
    could. Sending the id is what turns that from a label into a link.
    """

    def _credit_sale(self):
        from decimal import Decimal as D
        return create_sale(
            user=self.sales,
            customer=self.customer,
            cart=[{"product": self.product, "quantity": 2,
                   "unit_price": D("15.00")}],
            amount_paid=D("10.00"),          # leaves 20.00 outstanding
            payment_method="CASH",
        )

    def test_a_credit_sale_carries_the_id_of_the_debt_it_opened(self):
        from credit.models import DebtRecord

        sale = self._credit_sale()
        body = self.as_(self.sales).get(f"/api/sales/{sale.pk}/").json()

        debt = DebtRecord.objects.get(transaction=sale)
        self.assertEqual(body["debt_id"], debt.pk)

        # And that id opens the debt, which is the whole point.
        detail = self.as_(self.sales).get(f"/api/debts/{debt.pk}/")
        self.assertEqual(detail.status_code, 200, detail.content)

    def test_a_sale_paid_in_full_carries_no_debt(self):
        # self.sale from the base class was paid in full.
        body = self.as_(self.sales).get(f"/api/sales/{self.sale.pk}/").json()
        self.assertIsNone(body["debt_id"])

    def test_the_list_view_carries_it_too(self):
        """The sales list links straight to a debt without opening the sale."""
        self._credit_sale()
        rows = self.as_(self.sales).get("/api/sales/").json()["results"]
        self.assertTrue(any(r["debt_id"] is not None for r in rows))

    def test_listing_sales_does_not_cost_a_query_per_row(self):
        """
        debt_record is a reverse one-to-one, so serializing it WITHOUT
        select_related fires an extra SELECT for every sale on the page.

        Asserted as "the count does not grow with the rows" rather than against
        a fixed number: the absolute figure depends on auth, permissions and
        pagination and would need editing every time any of those changed,
        which is how a query-count test ends up deleted. The shape of the bug
        is growth, so growth is what this measures.
        """
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        client = self.as_(self.sales)

        self._credit_sale()
        with CaptureQueriesContext(connection) as few:
            client.get("/api/sales/")

        for _ in range(5):
            self._credit_sale()
        with CaptureQueriesContext(connection) as many:
            client.get("/api/sales/")

        self.assertEqual(
            len(many), len(few),
            f"{len(few)} queries for 2 sales but {len(many)} for 7 - the debt "
            f"link is being fetched one row at a time. Add 'debt_record' to "
            f"select_related on the transaction queryset.",
        )


class DebtLookupFromSaleTests(ApiTestBase):
    """
    Finding a sale's debt without relying on the sale payload.

    The app is not always talking to a server built from the same commit -
    this one runs against a deployed backend while the source sits on a
    laptop. So "View debt" resolves in two steps: use `debt_id` when the sale
    carries it, and otherwise ask the debts endpoint. Both paths are tested
    because the app will meet both servers.
    """

    def _credit_sale(self):
        from decimal import Decimal as D
        return create_sale(
            user=self.sales,
            customer=self.customer,
            cart=[{"product": self.product, "quantity": 2,
                   "unit_price": D("15.00")}],
            amount_paid=D("10.00"),
            payment_method="CASH",
        )

    def test_the_debts_endpoint_can_be_filtered_to_one_sale(self):
        from credit.models import DebtRecord

        first = self._credit_sale()
        self._credit_sale()          # a second, so a filter has work to do

        rows = self.as_(self.sales).get(
            f"/api/debts/?transaction={first.pk}"
        ).json()["results"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transaction"], first.pk)
        self.assertEqual(
            rows[0]["id"], DebtRecord.objects.get(transaction=first).pk
        )

    def test_every_debt_row_names_the_sale_that_opened_it(self):
        """
        The fallback path matches on this field, so it has to be present on
        the unfiltered list too - that is what an older server returns.
        """
        sale = self._credit_sale()
        rows = self.as_(self.sales).get("/api/debts/").json()["results"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("transaction", row)
        self.assertTrue(any(r["transaction"] == sale.pk for r in rows))

    def test_an_unknown_filter_value_returns_nothing_not_everything(self):
        """
        A filter that silently fails open would hand the app somebody else's
        debt as "the debt for this sale".
        """
        self._credit_sale()
        rows = self.as_(self.sales).get(
            "/api/debts/?transaction=999999"
        ).json()["results"]
        self.assertEqual(rows, [])

    def test_the_filter_cannot_reach_another_persons_debt(self):
        """Filtering narrows the scoped queryset; it never widens it."""
        from decimal import Decimal as D

        other_seller = User.objects.create_user(
            "sara", password="pw", role="SALES", manager=self.manager
        )
        other_customer = Customer.objects.create(
            name="Bekele", phone="0922", owner=other_seller,
            is_credit_approved=True,
        )
        theirs = create_sale(
            user=other_seller,
            customer=other_customer,
            cart=[{"product": self.product, "quantity": 1,
                   "unit_price": D("15.00")}],
            amount_paid=D("0.00"),
            payment_method="CASH",
        )

        rows = self.as_(self.sales).get(
            f"/api/debts/?transaction={theirs.pk}"
        ).json()["results"]
        self.assertEqual(
            rows, [],
            "a seller must not reach a colleague's debt by guessing a sale id",
        )


class OptionListTests(ApiTestBase):
    """
    The managed pick-lists: a select anyone can add to, and take back from.

    The point of the endpoint being open to any signed-in user is that a clerk
    is never blocked by a list somebody else forgot to maintain. Gating it
    recreates the free typing it replaces - "dashn" in the notes field.
    """

    def test_a_seller_gets_the_seeded_bank_list(self):
        rows = self.as_(self.sales).get("/api/options/?group=BANK").json()
        labels = [row["label"] for row in rows]
        self.assertIn("Commercial Bank of Ethiopia (CBE)", labels)
        self.assertIn("Dashen Bank", labels)

    def test_a_missing_group_returns_nothing_rather_than_everything(self):
        """A forgotten parameter must fail loudly, not load every list."""
        rows = self.as_(self.sales).get("/api/options/").json()
        self.assertEqual(rows, [])

    def test_a_seller_can_add_one_and_take_it_back(self):
        # A name deliberately NOT in the shipped list, so this exercises the
        # "somebody typed a new one" path rather than reviving a seeded row.
        client = self.as_(self.sales)
        created = client.post(
            "/api/options/",
            {"group": "BANK", "label": "Kifiya Microfinance"},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201)
        row = created.json()
        self.assertTrue(row["can_remove"], "you must be able to undo your own typo")

        labels = [
            o["label"]
            for o in client.get("/api/options/?group=BANK").json()
        ]
        self.assertIn("Kifiya Microfinance", labels)

        self.assertEqual(
            client.delete(f"/api/options/{row['id']}/").status_code, 204
        )
        labels = [
            o["label"]
            for o in client.get("/api/options/?group=BANK").json()
        ]
        self.assertNotIn("Kifiya Microfinance", labels)

    def test_re_adding_a_shipped_name_returns_the_shipped_row(self):
        """
        Somebody typing 'Siinqee Bank' when it is already in the list must get
        that row, not a second one - and must not then be able to delete the
        shared entry just because they were the one who typed it.
        """
        response = self.as_(self.sales).post(
            "/api/options/",
            {"group": "BANK", "label": "Siinqee Bank"},
            content_type="application/json",
        )
        row = response.json()
        self.assertTrue(row["is_seeded"])
        self.assertFalse(row["can_remove"])
        self.assertEqual(
            Option.objects.in_group("BANK")
            .filter(label__iexact="Siinqee Bank")
            .count(),
            1,
        )

    def test_adding_a_label_that_exists_returns_it_rather_than_refusing(self):
        """
        A dead end in the form somebody is standing in is worse than a
        duplicate, and case must not create a second bank.
        """
        client = self.as_(self.sales)
        again = client.post(
            "/api/options/",
            {"group": "BANK", "label": "dashen bank"},
            content_type="application/json",
        )
        self.assertIn(again.status_code, (200, 201))
        self.assertEqual(again.json()["label"], "Dashen Bank")

    def test_a_seller_cannot_remove_somebody_elses_entry(self):
        seeded = self.as_(self.admin).get("/api/options/?group=BANK").json()[0]
        response = self.as_(self.sales).delete(f"/api/options/{seeded['id']}/")
        self.assertEqual(response.status_code, 403)

    def test_an_unknown_list_is_refused(self):
        response = self.as_(self.sales).post(
            "/api/options/",
            {"group": "NOT_A_LIST", "label": "x"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class SalePaymentChannelTests(ApiTestBase):
    """'BANK' on its own cannot be reconciled against anything."""

    def _cart(self):
        return [{"product": self.product.pk, "quantity": 1,
                 "unit_price": "15.00"}]

    def test_a_transfer_must_name_a_bank(self):
        response = self.as_(self.sales).post(
            "/api/sales/",
            {"items": self._cart(), "amount_paid": "15.00",
             "payment_method": "BANK"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_the_bank_is_stored_and_read_back(self):
        client = self.as_(self.sales)
        bank = [
            o for o in client.get("/api/options/?group=BANK").json()
            if o["label"] == "Dashen Bank"
        ][0]

        sale = client.post(
            "/api/sales/",
            {"items": self._cart(), "amount_paid": "15.00",
             "payment_method": "BANK", "payment_channel": bank["id"],
             "payment_reference": "TX99213"},
            content_type="application/json",
        ).json()

        self.assertEqual(sale["payment_channel_name"], "Dashen Bank")
        self.assertEqual(sale["payment_reference"], "TX99213")
        self.assertIn("Dashen Bank", sale["payment_display"])
        self.assertIn("TX99213", sale["payment_display"])

    def test_a_bank_typed_in_at_the_counter_joins_the_list(self):
        client = self.as_(self.sales)
        sale = client.post(
            "/api/sales/",
            {"items": self._cart(), "amount_paid": "15.00",
             "payment_method": "MOBILE",
             "payment_channel_name": "Kacha"},
            content_type="application/json",
        ).json()

        self.assertEqual(sale["payment_channel_name"], "Kacha")
        labels = [
            o["label"]
            for o in client.get("/api/options/?group=MOBILE_MONEY").json()
        ]
        self.assertIn("Kacha", labels)

    def test_cash_names_nothing_even_if_a_bank_is_sent(self):
        """A stale value in a client's form must not invent a transfer."""
        client = self.as_(self.sales)
        bank = client.get("/api/options/?group=BANK").json()[0]
        sale = client.post(
            "/api/sales/",
            {"items": self._cart(), "amount_paid": "15.00",
             "payment_method": "CASH", "payment_channel": bank["id"]},
            content_type="application/json",
        ).json()
        self.assertEqual(sale["payment_channel_name"], "")


class StockRequestTests(ApiTestBase):
    """
    The counter asking the yard for more stock.

    The automatic low-stock alert says something is nearly gone. It does not
    say how many are wanted, who is waiting, or whether anybody agreed - so it
    gets swiped away and the seller finds out at the counter. This is the half
    that survives that.
    """

    def _ask(self, client=None, **overrides):
        payload = {
            "product": self.product.pk,
            "quantity": 200,
            "assigned_to": self.manager.pk,
            "reason_name": "Stock is low",
            "note": "Two lorries booked for Friday",
        }
        payload.update(overrides)
        return (client or self.as_(self.sales)).post(
            "/api/production-requests/",
            payload,
            content_type="application/json",
        )

    def test_only_people_who_can_make_things_may_be_asked(self):
        rows = self.as_(self.sales).get(
            "/api/production-requests/deciders/"
        ).json()
        names = [row["name"] for row in rows]
        self.assertIn(self.manager.display_name, names)
        self.assertNotIn(
            self.sales.display_name, names,
            "a seller who cannot record a batch must not appear as an answer",
        )

    def test_a_seller_asks_and_the_manager_sees_it(self):
        created = self._ask()
        self.assertEqual(created.status_code, 201)
        row = created.json()
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["quantity"], 200)
        # Snapshotted, because by the time it is read the shelf has moved.
        self.assertEqual(row["stock_at_request"], self.product.stock_quantity)

        incoming = self.as_(self.manager).get(
            "/api/production-requests/?box=incoming"
        ).json()["results"]
        self.assertEqual(len(incoming), 1)
        self.assertTrue(incoming[0]["can_respond"])
        self.assertFalse(incoming[0]["can_cancel"])

    def test_the_asker_can_cancel_but_not_answer_their_own_request(self):
        request_id = self._ask().json()["id"]
        mine = self.as_(self.sales).get(
            "/api/production-requests/?box=sent"
        ).json()["results"][0]
        self.assertTrue(mine["can_cancel"])
        self.assertFalse(mine["can_respond"])

        cancelled = self.as_(self.sales).post(
            f"/api/production-requests/{request_id}/cancel/",
            {}, content_type="application/json",
        )
        self.assertEqual(cancelled.json()["status"], "CANCELLED")

    def test_the_manager_accepts_and_the_seller_is_told(self):
        request_id = self._ask().json()["id"]
        answered = self.as_(self.manager).post(
            f"/api/production-requests/{request_id}/respond/",
            {"accept": True, "note": "Pouring tomorrow"},
            content_type="application/json",
        ).json()
        self.assertEqual(answered["status"], "ACCEPTED")
        self.assertEqual(answered["response_note"], "Pouring tomorrow")

        from api.models import NotificationLog

        self.assertTrue(
            NotificationLog.objects.filter(user=self.sales).exists(),
            "the person who asked has to be told either way",
        )

    def test_a_second_open_request_for_the_same_thing_is_refused(self):
        self.assertEqual(self._ask().status_code, 201)
        second = self._ask()
        self.assertEqual(second.status_code, 400)
        self.assertIn("already", second.json()["detail"].lower())

    def test_a_request_is_private_to_the_two_people_in_it(self):
        self._ask()
        other = User.objects.create_user(
            "zed", password="pw", role="SALES", manager=self.manager
        )
        rows = self.as_(other).get("/api/production-requests/").json()["results"]
        self.assertEqual(
            rows, [],
            "a request is a conversation between two named people",
        )

    def test_recording_the_batch_closes_the_request(self):
        from decimal import Decimal as D

        request_id = self._ask().json()["id"]
        self.as_(self.manager).post(
            f"/api/production-requests/{request_id}/respond/",
            {"accept": True}, content_type="application/json",
        )

        from production.models import RawMaterial

        cement = RawMaterial.objects.create(
            name="Cement", code="CEM", unit="BAG",
            quantity_in_stock=D("500"), unit_cost=D("18.00"),
            owner=self.manager,
        )
        run = self.as_(self.manager).post(
            "/api/production/",
            {
                "product": self.product.pk,
                "quantity_produced": 200,
                "materials": [{"material": cement.pk, "quantity": "50.000"}],
                "damages": [{"type_name": "Cracked", "quantity": 5}],
                "fulfils": [request_id],
            },
            content_type="application/json",
        )
        self.assertEqual(run.status_code, 201)
        # The itemised lines ARE the rejected figure.
        self.assertEqual(run.json()["quantity_rejected"], 5)

        closed = self.as_(self.sales).get(
            "/api/production-requests/?box=sent"
        ).json()["results"][0]
        self.assertEqual(closed["status"], "FULFILLED")
        self.assertIsNotNone(closed["fulfilled_run"])
