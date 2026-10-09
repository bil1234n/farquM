"""
The Expenses and Employees pages.

The rules live in expenses.services and are tested through the API in
api/tests.py (ExpenseTests); these check that the browser reaches the same
rules, sees only what it should, and adds up the same way.
"""
import datetime as dt
import io
import tempfile
from decimal import Decimal

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from accounts.roles import ensure_system_roles
from core.models import Option

from .models import Employee, Expense
from .services import record_expense


class ExpenseWebBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()
        cls.admin = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.other_manager = User.objects.create_user("marta", password="pw", role="MANAGER")
        cls.sales = User.objects.create_user(
            "sam", password="pw", role="SALES", manager=cls.manager
        )
        cls.keeper = User.objects.create_user("kebede", password="pw", role="STOCK_KEEPER")
        cls.guard = Employee.objects.create(
            name="Tesfaye Guard", job_name="Guard", monthly_salary=Decimal("4500.00"),
        )

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def post_expense(self, user, **fields):
        data = {
            "spent_on": timezone.localdate().isoformat(),
            "amount": "1200.00",
            "payment_method": "CASH",
            "category_name": "Fuel",
            "payee": "Total station",
        }
        data.update(fields)
        return self.client_for(user).post("/expenses/new/", data)


class ExpensePageTests(ExpenseWebBase):
    def test_a_manager_records_an_expense_with_a_new_category(self):
        response = self.post_expense(self.manager, category_name="Generator fuel")
        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(expense.category_name, "Generator fuel")
        self.assertEqual(expense.owner, self.manager)
        # Typed once, in the list for everybody next time.
        self.assertTrue(
            Option.objects.filter(group="EXPENSE_CATEGORY", label="Generator fuel").exists()
        )
        page = self.client_for(self.manager).get("/expenses/").content.decode()
        self.assertIn(expense.reference, page)
        self.assertIn("Generator fuel", page)

    def test_paying_an_employee_files_it_as_wages_for_the_month(self):
        response = self.post_expense(
            self.manager, category_name="", payee="", amount="4500.00",
            employee=self.guard.pk, pay_period="2026-08",
        )
        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(expense.employee, self.guard)
        self.assertEqual(expense.category_name, "Salaries & wages")
        self.assertEqual(expense.pay_type_name, "Salary")
        self.assertEqual(expense.pay_period, dt.date(2026, 8, 1))
        self.assertEqual(expense.payee, "Tesfaye Guard")

    def test_the_pay_button_fills_in_the_usual_salary(self):
        html = self.client_for(self.manager).get(
            f"/expenses/new/?employee={self.guard.pk}"
        ).content.decode()
        # The first line arrives filled in: this person, their usual salary.
        # (The rows are drawn by the page's script from this list.)
        self.assertIn('"amount": "4500.00"', html)
        self.assertIn(f'"employee": {self.guard.pk}', html)

    def test_a_bank_payment_must_say_which_bank(self):
        response = self.post_expense(self.manager, payment_method="BANK")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose which bank or wallet")
        self.assertFalse(Expense.objects.exists())

    def test_nothing_is_dated_in_the_future(self):
        tomorrow = timezone.localdate() + dt.timedelta(days=1)
        response = self.post_expense(self.manager, spent_on=tomorrow.isoformat())
        self.assertContains(response, "future")
        self.assertFalse(Expense.objects.exists())

    def test_correcting_and_cancelling(self):
        self.post_expense(self.manager)
        expense = Expense.objects.get()
        client = self.client_for(self.manager)
        client.post(f"/expenses/{expense.pk}/edit/", {
            "spent_on": expense.spent_on.isoformat(), "amount": "1300.00",
            "payment_method": "CASH", "category_name": "Fuel",
        })
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("1300.00"))

        client.post(f"/expenses/{expense.pk}/void/", {"reason": "Entered twice"})
        expense.refresh_from_db()
        self.assertTrue(expense.is_voided)
        self.assertEqual(expense.void_reason, "Entered twice")
        # Gone from the list and the total, still there when asked for.
        page = client.get("/expenses/").content.decode()
        self.assertIn("No expenses recorded", page)
        self.assertIn(">ETB 0.00<", page.replace("\n", ""))
        voided = client.get("/expenses/?voided=1").content.decode()
        self.assertIn("Cancelled: Entered twice", voided)

    def test_each_manager_sees_only_their_own_spending(self):
        self.post_expense(self.manager, amount="100.00")
        self.post_expense(self.other_manager, amount="700.00")
        mine = Expense.objects.get(owner=self.manager)
        theirs = Expense.objects.get(owner=self.other_manager)

        html = self.client_for(self.manager).get("/expenses/").content.decode()
        self.assertIn(mine.reference, html)
        self.assertNotIn(theirs.reference, html)
        self.assertEqual(
            self.client_for(self.manager).get(f"/expenses/{theirs.pk}/edit/").status_code,
            404,
        )
        # The owner sees both.
        owner_html = self.client_for(self.admin).get("/expenses/").content.decode()
        self.assertIn(mine.reference, owner_html)
        self.assertIn(theirs.reference, owner_html)

    def test_sellers_and_the_stock_keeper_cannot_open_expenses(self):
        for user in (self.sales, self.keeper):
            with self.subTest(user=user.username):
                response = self.client_for(user).get("/expenses/")
                self.assertRedirects(
                    response, "/system/forbidden/", fetch_redirect_response=False
                )

    def test_the_month_exports_as_csv(self):
        self.post_expense(self.manager, category_name="Rent", amount="9000.00")
        response = self.client_for(self.manager).get("/expenses/export/")
        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode()
        self.assertIn("Rent", body)
        self.assertIn("9000.00", body)


class EmployeePageTests(ExpenseWebBase):
    def test_adding_somebody_with_a_new_job(self):
        response = self.client_for(self.manager).post("/expenses/employees/new/", {
            "name": "  Almaz   Kebede ", "job_name": "Forklift driver",
            "phone": "0911223344", "monthly_salary": "6000", "is_active": "on",
        })
        person = Employee.objects.get(name="Almaz Kebede")
        self.assertRedirects(
            response, f"/expenses/employees/{person.pk}/", fetch_redirect_response=False
        )
        self.assertEqual(person.job_name, "Forklift driver")
        self.assertEqual(person.created_by, self.manager)
        self.assertTrue(
            Option.objects.filter(group="EMPLOYEE_JOB", label="Forklift driver").exists()
        )

    def test_the_payroll_shows_who_has_been_paid_this_month(self):
        record_expense(user=self.manager, data={
            "amount": "3000.00", "employee": self.guard, "payment_method": "CASH",
        })
        html = self.client_for(self.manager).get("/expenses/employees/").content.decode()
        self.assertIn("Tesfaye Guard", html)
        self.assertIn("1,500.00", html)   # still to pay of 4,500
        detail = self.client_for(self.manager).get(
            f"/expenses/employees/{self.guard.pk}/"
        ).content.decode()
        self.assertIn("3,000.00", detail)

    def test_a_manager_sees_only_the_pay_they_gave(self):
        record_expense(user=self.other_manager, data={
            "amount": "999.00", "employee": self.guard, "payment_method": "CASH",
        })
        detail = self.client_for(self.manager).get(
            f"/expenses/employees/{self.guard.pk}/"
        ).content.decode()
        self.assertNotIn("999.00", detail)
        owner = self.client_for(self.admin).get(
            f"/expenses/employees/{self.guard.pk}/"
        ).content.decode()
        self.assertIn("999.00", owner)

    def test_the_sidebar_offers_expenses_to_managers_only(self):
        manager_html = self.client_for(self.manager).get("/reports/").content.decode()
        self.assertIn('href="/expenses/"', manager_html)
        self.assertIn('href="/expenses/employees/"', manager_html)
        seller_html = self.client_for(self.sales).get("/reports/").content.decode()
        self.assertNotIn('href="/expenses/"', seller_html)


class ExpenseLineTests(ExpenseWebBase):
    """One payment, several lines - each its own expense, paid together."""

    def test_running_costs_paid_together(self):
        from .services import record_expense_lines

        lines = record_expense_lines(
            user=self.manager,
            data={"payment_method": "BANK", "payment_channel_name": "CBE",
                  "payee": "Total station", "notes": "Weekly run"},
            lines=[
                {"category_name": "Fuel", "amount": "1200"},
                {"category_name": "Oil", "amount": "350.50"},
                {"category_name": "", "amount": ""},  # the empty row left behind
            ],
        )
        self.assertEqual(len(lines), 2)
        first, second = lines
        self.assertEqual(first.group_reference, first.reference)
        self.assertEqual(second.group_reference, first.reference)
        for line in lines:
            line.refresh_from_db()
            self.assertEqual(line.payment_method, "BANK")
            self.assertEqual(line.payment_channel_name, "CBE")
            self.assertEqual(line.payee, "Total station")
            self.assertEqual(line.notes, "Weekly run")
        self.assertEqual(second.category_name, "Oil")
        self.assertEqual(second.amount, Decimal("350.50"))

    def test_several_people_paid_at_once(self):
        from .services import record_expense_lines

        loader = Employee.objects.create(name="Almaz Loader", monthly_salary=Decimal("3000"))
        lines = record_expense_lines(
            user=self.manager,
            data={"pay_period": "2026-08-01"},
            lines=[
                {"employee": self.guard.pk, "amount": "4500"},
                {"employee": loader.pk, "amount": "500", "pay_type_name": "Advance"},
            ],
        )
        guard, advance = lines
        self.assertEqual((guard.payee, guard.pay_type_name), ("Tesfaye Guard", "Salary"))
        self.assertEqual((advance.payee, advance.pay_type_name), ("Almaz Loader", "Advance"))
        for line in lines:
            self.assertEqual(line.category_name, "Salaries & wages")
            self.assertEqual(line.pay_period, dt.date(2026, 8, 1))
        self.assertEqual(self.guard.paid_between(dt.date(2000, 1, 1), timezone.localdate()),
                         Decimal("4500.00"))

    def test_a_bad_line_says_which_and_saves_nothing(self):
        from .services import ExpenseError, record_expense_lines

        with self.assertRaisesMessage(ExpenseError, "Line 2: The amount must be more than zero."):
            record_expense_lines(
                user=self.manager, data={},
                lines=[
                    {"category_name": "Fuel", "amount": "100"},
                    {"category_name": "Oil", "amount": "-5"},
                ],
            )
        self.assertFalse(Expense.objects.exists())

    def test_nothing_to_record(self):
        from .services import ExpenseError, record_expense_lines

        with self.assertRaisesMessage(ExpenseError, "at least one line"):
            record_expense_lines(user=self.manager, data={}, lines=[{"amount": ""}])


class ExpenseLinesPageTests(ExpenseWebBase):
    """The browser records one payment of several lines."""

    def test_two_lines_one_payment(self):
        response = self.client_for(self.manager).post("/expenses/new/", {
            "spent_on": timezone.localdate().isoformat(),
            "payment_method": "CASH",
            "payee": "Total station",
            "line_category_name[]": ["Fuel", "Oil"],
            "line_category[]": ["", ""],
            "line_amount[]": ["1200", "300"],
            "line_employee[]": ["", ""],
        })
        self.assertEqual(response.status_code, 302, response.content[:500])
        first, second = Expense.objects.order_by("id")
        self.assertEqual((first.category_name, second.category_name), ("Fuel", "Oil"))
        self.assertEqual(second.group_reference, first.reference)
        self.assertEqual(second.payee, "Total station")

    def test_people_and_a_cost_in_one_payment(self):
        response = self.client_for(self.manager).post("/expenses/new/", {
            "spent_on": timezone.localdate().isoformat(),
            "payment_method": "CASH",
            "pay_type_name": "Salary",
            "line_category_name[]": ["", "Tea for the shift"],
            "line_category[]": ["", ""],
            "line_amount[]": ["4500", "120"],
            "line_employee[]": [str(self.guard.pk), ""],
        })
        self.assertEqual(response.status_code, 302, response.content[:500])
        pay, tea = Expense.objects.order_by("id")
        self.assertEqual(pay.employee, self.guard)
        self.assertEqual(pay.category_name, "Salaries & wages")
        self.assertEqual(pay.payee, "Tesfaye Guard")
        self.assertIsNone(tea.employee)
        self.assertEqual(tea.category_name, "Tea for the shift")

    def test_a_bad_line_keeps_what_was_typed(self):
        response = self.client_for(self.manager).post("/expenses/new/", {
            "spent_on": timezone.localdate().isoformat(),
            "payment_method": "CASH",
            "line_category_name[]": ["Fuel", ""],
            "line_category[]": ["", ""],
            "line_amount[]": ["1200", "300"],
            "line_employee[]": ["", ""],
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Line 2: Choose what the money was spent on.")
        self.assertContains(response, '"amount": "1200"')
        self.assertFalse(Expense.objects.exists())


def _png() -> bytes:
    """A real PNG, so Django's ImageField validator accepts it."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _upload(name="photo.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _png(), content_type="image/png")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="faruq-payroll-"))
class EmployeePhotoPageTests(ExpenseWebBase):
    """A face, an address and an identity card on somebody on the payroll."""

    def setUp(self):
        super().setUp()
        # A manager who may look at the payroll but no longer change it.
        self.looker = User.objects.create_user(
            "yonas", password="pw", role="MANAGER",
            denied_permissions=["employee.manage"],
        )

    def test_the_form_saves_an_address_and_both_pictures(self):
        response = self.client_for(self.manager).post(
            "/expenses/employees/new/",
            {
                "name": "Almaz Kebede",
                "job_name": "Loader",
                "phone": "0911223344",
                "address": "Kebele 08, by the water tower",
                "monthly_salary": "6000",
                "id_number": "ETH-4412",
                "is_active": "on",
                "photo": _upload("face.png"),
                "id_photo": _upload("kebele.png"),
            },
        )
        person = Employee.objects.get(name="Almaz Kebede")
        self.assertRedirects(
            response, f"/expenses/employees/{person.pk}/",
            fetch_redirect_response=False,
        )
        self.assertEqual(person.address, "Kebele 08, by the water tower")
        self.assertEqual(person.id_number, "ETH-4412")
        self.assertTrue(person.photo)
        self.assertTrue(person.id_photo)

    def test_the_page_shows_the_card_to_a_manager_and_hides_it_from_a_looker(self):
        self.guard.id_number = "ETH-1"
        self.guard.id_photo.save("card.png", ContentFile(_png()), save=True)

        allowed = self.client_for(self.manager).get(
            f"/expenses/employees/{self.guard.pk}/"
        ).content.decode()
        self.assertIn(self.guard.id_photo.url, allowed)

        refused = self.client_for(self.looker).get(
            f"/expenses/employees/{self.guard.pk}/"
        ).content.decode()
        self.assertNotIn(self.guard.id_photo.url, refused)
        self.assertIn("not allowed to open it", refused)
        # The number is working information and stays visible.
        self.assertIn("ETH-1", refused)

    def test_ticking_remove_takes_a_picture_off(self):
        self.guard.photo.save("face.png", ContentFile(_png()), save=True)
        self.client_for(self.manager).post(
            f"/expenses/employees/{self.guard.pk}/edit/",
            {
                "name": self.guard.name,
                "monthly_salary": "4500",
                "is_active": "on",
                "remove_photo": "on",
            },
        )
        self.guard.refresh_from_db()
        self.assertFalse(self.guard.photo)

    def test_a_new_picture_beats_a_forgotten_remove_tick(self):
        """
        Somebody picks a replacement and leaves the box ticked. That means
        "replace" - throwing the new file away would be the one reading
        nobody intends.
        """
        self.guard.photo.save("old.png", ContentFile(_png()), save=True)
        self.client_for(self.manager).post(
            f"/expenses/employees/{self.guard.pk}/edit/",
            {
                "name": self.guard.name,
                "monthly_salary": "4500",
                "is_active": "on",
                "remove_photo": "on",
                "photo": _upload("new.png"),
            },
        )
        self.guard.refresh_from_db()
        self.assertTrue(self.guard.photo)
        self.assertIn("new", self.guard.photo.name)

    def test_the_payroll_is_searched_by_the_number_on_the_card(self):
        Employee.objects.create(name="Almaz", id_number="ETH-4412")
        html = self.client_for(self.manager).get(
            "/expenses/employees/", {"q": "4412"}
        ).content.decode()
        self.assertIn("Almaz", html)
        self.assertNotIn("Tesfaye Guard", html)
