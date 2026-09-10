"""
Turning an exception into something a person at the counter can read.

Two audiences, two outputs, from one event:

  * the storeman gets one sentence saying what happened and whether anything
    changed, plus a short reference if he needs to quote it;
  * the log gets the whole traceback.

The rule is that these never swap places. A stack trace on the screen tells
the person nothing they can act on while handing a passer-by the file layout,
the Python version and the installed packages - which is exactly what the
Django debug page did when a stock count crashed.
"""
import logging
import uuid

from django.core.exceptions import ValidationError
from django.db import DatabaseError

logger = logging.getLogger(__name__)

#: Errors that are the SYSTEM's fault rather than the user's. These are worth
#: a reference number and a log entry; a ValidationError is neither, because
#: "enter a quantity greater than zero" is not an incident.
INFRASTRUCTURE_ERRORS = (DatabaseError,)

#: What the storeman sees when the system, not the input, is at fault. It says
#: "nothing was changed" because that is true: every ledger write runs inside a
#: transaction, so a failure rolls the whole thing back rather than leaving the
#: cement gone and no blocks to show for it.
GENERIC_MESSAGE = (
    "That could not be saved just now, so nothing was changed. "
    "Please try again — if it keeps happening, quote reference {ref}."
)


def reference() -> str:
    """A short code the user can read aloud over a phone."""
    return uuid.uuid4().hex[:8].upper()


def describe(exc, *, context: str = "") -> str:
    """
    One sentence for the screen.

    A ValidationError already carries wording written for the person who
    triggered it, so it is passed through. Anything else is summarised, and the
    detail goes to the log under a reference.
    """
    if isinstance(exc, ValidationError):
        messages = getattr(exc, "messages", None) or [str(exc)]
        return "; ".join(str(m) for m in messages)

    ref = reference()
    logger.exception(
        "Unhandled %s%s [ref %s]",
        type(exc).__name__,
        f" during {context}" if context else "",
        ref,
    )
    return GENERIC_MESSAGE.format(ref=ref)


def is_expected(exc) -> bool:
    """True for errors caused by what was typed, not by the system."""
    return isinstance(exc, ValidationError)
