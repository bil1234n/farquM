"""
DRF authentication that makes the right branch the active one.

A browser session is known to BranchMiddleware before the view runs; a phone
signs in with a token, which DRF reads only inside the view. These are the
same two authentication classes the API always used, with one step added:
once the user is known, the branch this request works in becomes the active
branch for the rest of it (core.branching). BranchMiddleware puts the
previous value back when the response leaves.

WHICH BRANCH
------------
The branch the device asked for. The phone sends the branch its screen is
showing in an `X-Branch` header on every request, and that is the branch the
request reads and writes - not whatever the account was last switched to,
which a browser or a second phone signed in to the same account may have
changed in the meantime. That is what used to happen: the phone said
"Branch 2", the website had moved the account to the main branch, and the
phone listed the main branch's batches and saved its new material there.

No header (an old build of the app) means the account's own branch, as
before. A header naming a branch this person may no longer work in - closed,
or they were taken off it - is refused with 409 rather than quietly served
from another branch, except on the few endpoints the phone needs to find out
where it is now (BRANCH_FREE_VIEWS).
"""
from rest_framework import exceptions, status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication

from core.branching import (
    BRANCH_HEADER,
    BRANCH_UNAVAILABLE_MESSAGE,
    CLOSED_BRANCH_MESSAGE,
    NO_BRANCH,
    activate_for_user,
    as_branch_id,
    pin_branch,
)

#: url names (api namespace) the phone must reach even when the branch on its
#: screen is gone: who am I and where may I go, move me, sign me out. They
#: answer from the account's own branch instead of refusing.
BRANCH_FREE_VIEWS = frozenset(
    {"me", "logout", "branches", "branch_switch", "device_register"}
)


class BranchUnavailable(exceptions.APIException):
    """The phone asked for a branch this person may not work in (now)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = BRANCH_UNAVAILABLE_MESSAGE
    default_code = "branch_unavailable"

    def __init__(self):
        # A dict, so the JSON carries the code the app looks for as well as
        # the sentence it shows.
        super().__init__(
            detail={"detail": BRANCH_UNAVAILABLE_MESSAGE, "code": "branch_unavailable"}
        )


def _url_name(request) -> str:
    match = getattr(getattr(request, "_request", request), "resolver_match", None)
    return getattr(match, "url_name", "") or ""


def _pin_from_header(request, user) -> None:
    """Work in the branch the phone's screen shows, if this person may."""
    raw = request.META.get(BRANCH_HEADER)
    if raw is None or str(raw).strip() == "":
        return
    wanted = as_branch_id(raw)
    if wanted is not None and wanted in user.allowed_branch_ids:
        pin_branch(user, wanted)
        return
    if _url_name(request) in BRANCH_FREE_VIEWS:
        return  # answer from the account's own branch; the app will move
    raise BranchUnavailable()


def _activate_for(request, result):
    if result is not None:
        user = result[0]
        # Every branch this person worked in has been closed. Refused here,
        # where the phone's token is first read, rather than left to return
        # empty lists that look like a wiped business.
        if getattr(user, "current_branch_id", None) == NO_BRANCH:
            raise exceptions.PermissionDenied(CLOSED_BRANCH_MESSAGE)
        _pin_from_header(request, user)
        activate_for_user(user)
    return result


class BranchTokenAuthentication(TokenAuthentication):
    """Token sign-in (the phone), then the branch it asked for is active."""

    def authenticate(self, request):
        return _activate_for(request, super().authenticate(request))


class BranchSessionAuthentication(SessionAuthentication):
    """Browser session, then that browser's branch is active."""

    def authenticate(self, request):
        return _activate_for(request, super().authenticate(request))
