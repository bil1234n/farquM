"""
The Expenses and Employees pages.

The rules live in expenses.services and are tested through the API in
api/tests.py (ExpenseTests); these check that the browser reaches the same
rules, sees only what it should, and adds up the same way.
"""
import datetime as dt
from decimal import Decimal

from django.test import Client, TestCase
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
        self.assertIn('value="4500.00"', html)
        self.assertIn(f'<option value="{self.guard.pk}" selected>', html)

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
