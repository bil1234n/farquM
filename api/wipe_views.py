"""
POST /api/system/wipe-data/ - delete all trading data. Admin only.

The phone asks twice (a warning, then the password) but the server is what
actually guards this: it checks the admin flag AND the password itself, and
wants the literal word DELETE as well, so a stray or replayed request with a
stolen token cannot empty the books.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.models import AuditAction
from accounts.services import log_action
from core.wipe import wipe_business_data

from .permissions import IsAdmin


@api_view(["POST"])
@permission_classes([IsAdmin])
def wipe_data(request):
    password = request.data.get("password") or ""
    confirm = (request.data.get("confirm") or "").strip().upper()

    if confirm != "DELETE":
        return Response(
            {"detail": "Type DELETE to confirm."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not request.user.check_password(password):
        log_action(
            AuditAction.DELETE,
            description="Tried to delete all business data - wrong password.",
            user=request.user,
            request=request,
        )
        return Response(
            {"detail": "Your password is not correct."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    counts = wipe_business_data()

    summary = ", ".join(f"{name}: {n}" for name, n in counts.items()) or "nothing"
    log_action(
        AuditAction.DELETE,
        description=(
            "Deleted ALL business data (sales, stock, products, production, "
            f"expenses, debts). Users and customers kept. Removed - {summary}."
        ),
        changes={"deleted": counts},
        user=request.user,
        request=request,
    )
    return Response({"detail": "All data deleted.", "deleted": counts})
