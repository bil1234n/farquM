"""Thread-local access to the current request user (used by audit logging)."""
import logging
import threading

_thread_locals = threading.local()
logger = logging.getLogger(__name__)


def get_current_user():
    return getattr(_thread_locals, "user", None)


def get_current_request():
    return getattr(_thread_locals, "request", None)


def client_ip(request) -> str:
    """
    Best-effort client IP, used for audit logs and registration throttling.

    X-Forwarded-For is honoured because the app sits behind a proxy in every
    deployment that matters, and REMOTE_ADDR there is the proxy's own address -
    which would put every user in the world into a single rate-limit bucket.

    The FIRST entry in the chain is taken, since proxies append. Note that a
    client can forge that header when nothing trustworthy sits in front of the
    app, so this is good enough for throttling and forensics but must not be
    used as an authorisation input.
    """
    if request is None:
        return ""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.META.get("REMOTE_ADDR") or "")[:45]


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _thread_locals.request = request
        _thread_locals.user = getattr(request, "user", None)
        try:
            response = self.get_response(request)
        finally:
            _thread_locals.request = None
            _thread_locals.user = None
        return response


class FriendlyErrorMiddleware:
    """
    The last line of defence: no stack trace ever reaches the shop floor.

    Django only uses `handler500` when DEBUG is off, so during development an
    unexpected exception paints the yellow debug page - file paths, Python
    version, installed packages and local variables - on whatever screen
    happens to be in front of a customer. That is how a raw
    TransactionManagementError ended up being the user interface for a stock
    count that failed.

    This turns any unhandled exception into the branded error page, in both
    modes, and writes the full traceback to the log under a reference the user
    can quote. Nothing is hidden from the developer; it just stops being shown
    to the wrong person.

    Set `SHOW_TECHNICAL_ERRORS = True` (env: SHOW_TECHNICAL_ERRORS) to get the
    debug page back while chasing something specific.

    Deliberately does NOT touch:
      * `/api/` - DRF has its own exception handler that returns JSON, and an
        HTML error page would break the phone's parser rather than inform it;
      * Http404 and PermissionDenied - Django already routes those to the 404
        and 403 pages, which are the right answers.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.conf import settings
        from django.core.exceptions import PermissionDenied
        from django.http import Http404
        from django.shortcuts import render

        from core.errors import reference

        if isinstance(exception, (Http404, PermissionDenied)):
            return None
        if request.path.startswith("/api/"):
            return None
        if getattr(settings, "SHOW_TECHNICAL_ERRORS", False):
            return None

        ref = reference()
        logger.exception(
            "Unhandled %s on %s %s [ref %s]",
            type(exception).__name__, request.method, request.path, ref,
        )
        return render(request, "500.html", {"reference": ref}, status=500)
