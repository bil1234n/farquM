"""
Branches, from the phone.

    GET   /api/branches/              the branches this person may switch to;
                                      with branch.manage, every branch
    POST  /api/branches/              open a new branch          (branch.manage)
    PATCH /api/branches/<id>/         rename, re-address, close or reopen
                                                                 (branch.manage)
    POST  /api/branches/switch/       work in another branch     (signed in)

The switch is remembered on the account, so the phone and the website are in
the same branch; the answer carries the refreshed profile, so the app can
redraw itself in the new branch without a second request. The rules live in
core/branches.py - this module only translates them to HTTP.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.branches import (
    BranchError,
    count_staff,
    create_branch,
    set_branch_open,
    switch_branch,
    update_branch,
)
from core.models import Branch

from .serializers import UserSerializer


def _row(branch, *, manage=False) -> dict:
    row = {
        "id": branch.pk,
        "name": branch.name,
        "is_main": branch.is_main,
        "is_active": branch.is_active,
    }
    if manage:
        row.update(
            {
                "address": branch.address,
                "phone": branch.phone,
                "staff_count": getattr(branch, "staff_count", None),
                "created_at": branch.created_at,
            }
        )
    return row


def _refused(exc) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def branches(request):
    user = request.user
    manage = user.has_access("branch.manage")

    if request.method == "POST":
        if not manage:
            return Response(
                {"detail": "You do not have permission to manage branches."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            branch = create_branch(
                user=user,
                name=request.data.get("name"),
                address=request.data.get("address") or "",
                phone=request.data.get("phone") or "",
            )
        except BranchError as exc:
            return _refused(exc)
        return Response(_row(branch, manage=True), status=status.HTTP_201_CREATED)

    if manage:
        rows = list(Branch.objects.all())
        count_staff(rows)
    else:
        rows = user.allowed_branches()
    return Response(
        {
            "current": user.current_branch_id,
            "can_manage": manage,
            "results": [_row(b, manage=manage) for b in rows],
        }
    )


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def branch_detail(request, pk):
    if not request.user.has_access("branch.manage"):
        return Response(
            {"detail": "You do not have permission to manage branches."},
            status=status.HTTP_403_FORBIDDEN,
        )
    branch = Branch.objects.filter(pk=pk).first()
    if branch is None:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    data = request.data
    try:
        update_branch(
            branch,
            user=request.user,
            name=data.get("name") if "name" in data else None,
            address=data.get("address") if "address" in data else None,
            phone=data.get("phone") if "phone" in data else None,
        )
        if "is_active" in data:
            raw = data.get("is_active")
            is_open = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
            set_branch_open(branch, user=request.user, is_open=is_open)
    except BranchError as exc:
        return _refused(exc)
    count_staff([branch])
    return Response(_row(branch, manage=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def branch_switch(request):
    try:
        branch = switch_branch(request.user, request.data.get("branch"))
    except BranchError as exc:
        return _refused(exc)
    return Response(
        {
            "branch": _row(branch),
            "user": UserSerializer(request.user, context={"request": request}).data,
        }
    )
