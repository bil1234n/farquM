"""
DRF authentication that makes the signed-in user's branch the active one.

A browser session is known to BranchMiddleware before the view runs; a phone
signs in with a token, which DRF reads only inside the view. These are the
same two authentication classes the API always used, with one step added:
once the user is known, their branch becomes the active branch for the rest
of the request (core.branching). BranchMiddleware puts the previous value
back when the response leaves.
"""
from rest_framework import exceptions
from rest_framework.authentication import SessionAuthentication, TokenAuthentication

from core.branching import CLOSED_BRANCH_MESSAGE, NO_BRANCH, activate_for_user


def _activate_for(result):
    if result is not None:
        user = result[0]
        # Every branch this person worked in has been closed. Refused here,
        # where the phone's token is first read, rather than left to return
        # empty lists that look like a wiped business.
        if getattr(user, "current_branch_id", None) == NO_BRANCH:
            raise exceptions.PermissionDenied(CLOSED_BRANCH_MESSAGE)
        activate_for_user(user)
    return result


class BranchTokenAuthentication(TokenAuthentication):
    """Token sign-in (the phone), then that user's branch is active."""

    def authenticate(self, request):
        return _activate_for(super().authenticate(request))


class BranchSessionAuthentication(SessionAuthentication):
    """Browser session, then that user's branch is active."""

    def authenticate(self, request):
        return _activate_for(super().authenticate(request))
