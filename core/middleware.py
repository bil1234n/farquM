"""Thread-local access to the current request user (used by audit logging)."""
import threading

_thread_locals = threading.local()


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
