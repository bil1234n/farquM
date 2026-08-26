from django.urls import path

from . import views

app_name = "credit"

urlpatterns = [
    # Dashboard
    path("", views.CreditDashboardView.as_view(), name="dashboard"),
    path("aging/", views.AgingReportView.as_view(), name="aging_report"),
    # Borrowers
    path("borrowers/", views.BorrowerListView.as_view(), name="borrower_list"),
    path("borrowers/<int:pk>/", views.BorrowerDetailView.as_view(), name="borrower_detail"),
    path("borrowers/<int:pk>/pay/", views.bulk_repayment, name="bulk_repayment"),
    # Debts
    path("debts/", views.DebtListView.as_view(), name="debt_list"),
    path("debts/<int:pk>/", views.DebtDetailView.as_view(), name="debt_detail"),
    path("debts/<int:pk>/adjust/", views.DebtAdjustView.as_view(), name="debt_adjust"),
    path("debts/<int:pk>/pay/", views.repayment_create, name="repayment_create"),
    path("debts/<int:pk>/write-off/", views.debt_write_off, name="debt_write_off"),
    # Repayments
    path("repayments/<int:pk>/reverse/", views.repayment_reverse, name="repayment_reverse"),
    # Credit accounts (Admin)
    path("accounts/<int:pk>/edit/", views.CreditAccountUpdateView.as_view(), name="account_update"),
    path("accounts/<int:pk>/block/", views.account_toggle_block, name="account_block"),
]
