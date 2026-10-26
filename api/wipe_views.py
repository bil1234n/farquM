"""
POST /api/system/wipe-data/ - delete the CURRENT branch's trading data.
Admin only. Other branches are not touched.

The phone asks twice (a warning, then the password) but the server is what
actually guards this: it checks the admin flag AND the password itself, and
wants the literal word DELETE as well, so a stray or replayed request with a
stolen token cannot empty the books.

The branch is the one the admin is working in right now (the same one every
other screen is showing) - never one named in the request body, so the phone
cannot be talked into wiping a branch the admin is not looking at.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.models import AuditAction
from accounts.services import log_action
from core.branching import current_branch_id
from core.models import Branch
from core.wipe import WipeError, wipe_branch_data

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
            description="Tried to delete all branch data - wrong password.",
            user=request.user,
            request=request,
        )
        return Response(
            {"detail": "Your password is not correct."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    branch_id = current_branch_id() or getattr(
        request.user, "current_branch_id", None
    )
    branch = Branch.objects.filter(pk=branch_id).first() if branch_id else None
    if branch is None:
        return Response(
            {"detail": "No branch is selected."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        counts = wipe_branch_data(branch.pk)
    except WipeError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    summary = ", ".join(f"{name}: {n}" for name, n in counts.items()) or "nothing"
    log_action(
        AuditAction.DELETE,
        instance=branch,
        description=(
            f"Deleted ALL data of branch '{branch}' (sales, stock, products, "
            "production, expenses, debts). Users, customers and other "
            f"branches kept. Removed - {summary}."
        ),
        changes={"branch": str(branch), "deleted": counts},
        user=request.user,
        request=request,
    )
    return Response(
        {"detail": "All data deleted.", "branch": str(branch), "deleted": counts}
    )
