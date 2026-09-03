# -*- coding: utf-8 -*-
"""
The one place the API's own words get translated on the way out.

WHY A RENDERER AND NOT FIFTY SERIALIZER CHANGES
-----------------------------------------------
Roughly forty fields across a dozen serializers end in `_display` or `_label`,
and every service can raise an error. Translating each at its source would mean
touching every one of them and remembering the forty-first next month.

A renderer sits at the single point every response passes through. It walks the
payload once and translates the values that are *words the server chose*,
leaving the values that are *data a person typed* exactly as they are.

WHAT GETS TRANSLATED
--------------------
  * any key ending in `_display` or `_label`, except the ones in DATA_KEYS;
  * a small explicit list of other word-valued keys (`display_status`,
    `aging_bucket`, ...);
  * every string in an error response (status >= 400), at any depth, because
    error payloads are all message and no data - DRF shapes them as
    {"field": ["This field is required."]} and the field NAMES are keys, not
    values.

WHAT NEVER DOES
---------------
`customer_display`, `owner_name`, names, references, SKUs, notes, block
reasons. Those are listed in DATA_KEYS or simply not matched. Translation is an
exact-string lookup, so even if one slipped through, a product called "Paid"
would be the only casualty - but it is worth not relying on that.

COST
----
One walk of an already-built dict per response, and a dict lookup per candidate
string. The alternative was a lookup per field per row in the serializer, which
is strictly more work.
"""
from rest_framework.renderers import JSONRenderer

from .messages import normalise_language, translate

#: Keys that LOOK like labels but hold data somebody typed.
#:
#: `customer_display` is the customer's name (or "Walk-in", which is in the
#: table anyway and translates correctly by coincidence rather than by this
#: rule). The rest are here so a future key ending in `_label` cannot start
#: mangling names by accident.
DATA_KEYS = frozenset(
    {
        "customer_display",
        "owner_name",
        "manager_name",
        "display_name",
        "product_name",
        "customer_name",
        "user_display",
        "sold_by_name",
        "performed_by_name",
        "uploaded_by_name",
        "category_name",
        "supplier_name",
        "role_name",
    }
)

#: Word-valued keys that do not end in `_display` or `_label`.
WORD_KEYS = frozenset(
    {
        "display_status",
        "aging_bucket",
        "stock_status_label",
        "risk_label",
        "scope_label",
        "data_scope_label",
        "detail",
        "message",
        "status_text",
    }
)

#: How deep to walk. A guard, not a limit anyone should hit: the deepest real
#: payload is a paginated list of sales, each with items and receipts, which is
#: five. Anything deeper is a cycle or a mistake, and walking it forever would
#: hang the request rather than fail it.
MAX_DEPTH = 12


def _should_translate(key: str) -> bool:
    if key in DATA_KEYS:
        return False
    if key in WORD_KEYS:
        return True
    return key.endswith("_display") or key.endswith("_label")


def translate_payload(data, lang: str, *, everything: bool = False, depth: int = 0):
    """
    Return `data` with its server-authored words in `lang`.

    `everything=True` treats every string as a message, which is right for an
    error response and wrong for anything else.
    """
    if depth > MAX_DEPTH:
        return data

    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if isinstance(value, str):
                if everything or _should_translate(str(key)):
                    out[key] = translate(value, lang)
                else:
                    out[key] = value
            else:
                out[key] = translate_payload(
                    value, lang, everything=everything, depth=depth + 1
                )
        return out

    if isinstance(data, (list, tuple)):
        return [
            translate(item, lang)
            if (isinstance(item, str) and everything)
            else translate_payload(item, lang, everything=everything, depth=depth + 1)
            for item in data
        ]

    return data


class TranslatingJSONRenderer(JSONRenderer):
    """JSONRenderer that speaks the language the client asked for."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        context = renderer_context or {}
        request = context.get("request")
        response = context.get("response")

        lang = ""
        if request is not None:
            # An explicit ?lang= beats the header, so a choice made inside the
            # app wins over the phone's system language.
            lang = normalise_language(
                request.GET.get("lang", "")
                or request.META.get("HTTP_ACCEPT_LANGUAGE", "")
            )

        if lang and lang != "en" and data is not None:
            failed = bool(response is not None and response.status_code >= 400)
            try:
                data = translate_payload(data, lang, everything=failed)
            except Exception:
                # Translation is a courtesy. A bug in this walk must never turn
                # a working response into a 500 - the English payload is still
                # correct and usable.
                pass

        return super().render(data, accepted_media_type, renderer_context)
