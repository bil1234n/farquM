"""
The API's answer when something goes wrong that nobody planned for.

DRF's own handler turns the errors it knows about - validation, permission,
not found, throttling - into JSON. Anything else (a bug, a row the code did
not expect) escapes it, and Django answers with an HTML page instead: the
yellow debug page when DEBUG is on, a bare "Server Error (500)" when it is
off. The phone can read neither. It printed the debug page - Python paths,
installed apps, middleware, a traceback - into its error card: a screenful
of noise where one sentence belonged, and the server's insides shown to
whoever was holding the phone.

So the last resort here matches the web's (core.middleware.
FriendlyErrorMiddleware): one sentence for the person holding the phone, and
the whole traceback in the log under a short reference they can read out.

SHOW_TECHNICAL_ERRORS=True brings back Django's own page while a developer is
chasing something specific, exactly as it does for the web pages.
"""
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from core.errors import reference

logger = logging.getLogger(__name__)

#: Worded for the shop floor, not the developer. The reference is what makes
#: it useful to the developer: it finds the traceback in the log.
MESSAGE = (
    "Something went wrong on the server. Please try again - if it keeps "
    "happening, tell your administrator and quote reference {ref}."
)


def handle(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        return response
    if getattr(settings, "SHOW_TECHNICAL_ERRORS", False):
        return None  # DRF re-raises: Django's debug page, as asked

    ref = reference()
    request = context.get("request")
    logger.exception(
        "Unhandled %s on %s %s [ref %s]",
        type(exc).__name__,
        getattr(request, "method", "?"),
        getattr(request, "path", "?"),
        ref,
    )
    return Response(
        {"detail": MESSAGE.format(ref=ref), "reference": ref},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
