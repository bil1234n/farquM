# -*- coding: utf-8 -*-
"""
Amharic for everything the API says out loud.

THE PROBLEM THIS SOLVES
-----------------------
The phone app is translated: every label it writes itself comes from
`lib/l10n/strings_am.dart`. But a large share of the words on screen are not
written by the app at all - they arrive from the server:

    "Partially paid"                a choice label off a model
    "Damage / write-off"            the same
    "Not enough stock for 'Coke'."  a service error
    "You do not have permission     a refusal
     to void a sale."

None of those could be translated in the app without shipping a second copy of
every message and re-releasing whenever one changed. So they are translated
here, at the one place they are produced, and the app needs no change at all.

HOW IT REACHES THE CLIENT
-------------------------
`api/renderers.py` walks the outgoing JSON and passes the values that are
words - not names, not amounts - through `translate()`. The language comes from
the `Accept-Language` header the app already sends on every request (see
`ApiClient.language`), or from `?lang=`.

TWO KINDS OF ENTRY
------------------
EXACT     whole strings that never vary. A dictionary lookup.
PATTERNS  messages with a value in the middle - a product name, a quantity.
          A regex captures the pieces and the Amharic template puts them back,
          so "Not enough stock for 'Coke'." translates while 'Coke' does not.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Anything a person typed: product names, customer names, category names,
usernames, notes, block reasons. Those are data. Translating them would be
both wrong and unfixable, so the renderer never sends them through here.

ADDING A MESSAGE
----------------
Put the English exactly as the code raises it, then the Amharic. The test
`api.tests.MessageCatalogueTests` walks the source for message strings and
fails when one is missing, so this file cannot quietly fall behind.
"""
import re

# ---------------------------------------------------------------------------
# Whole strings
# ---------------------------------------------------------------------------
EXACT_AM: dict[str, str] = {
    # -- Units --------------------------------------------------------------
    "Piece": "ቁራጭ",
    "Box": "ሳጥን",
    "Carton": "ካርቶን",
    "Kilogram": "ኪሎግራም",
    "Litre": "ሊትር",
    "Meter": "ሜትር",
    "Pack": "ጥቅል",

    # -- Stock movement types ----------------------------------------------
    "Restock / Purchase in": "ዳግም ሙሌት / ግዢ ገቢ",
    "Sale": "ሽያጭ",
    "Customer return (in)": "የደንበኛ ተመላሽ (ገቢ)",
    "Return to supplier (out)": "ወደ አቅራቢ ተመላሽ (ወጪ)",
    "Manual adjustment": "በእጅ ማስተካከያ",
    "Damage / write-off": "ብልሽት / ስረዛ",
    "Reversal of voided sale": "የተሰረዘ ሽያጭ መመለሻ",
    "Opening balance": "የመክፈቻ ቀሪ",

    # -- Customer types -----------------------------------------------------
    "Walk-in": "አላፊ ደንበኛ",
    "Regular": "መደበኛ",
    "Wholesale": "ጅምላ",
    "Business": "ድርጅት",

    # -- Payment status -----------------------------------------------------
    "Paid": "ተከፍሏል",
    "Partially paid": "በከፊል ተከፍሏል",
    "Unpaid (Credit)": "አልተከፈለም (ብድር)",
    "Refunded": "ተመላሽ ተደርጓል",

    # -- Payment methods ----------------------------------------------------
    "Cash": "ጥሬ ገንዘብ",
    "Bank transfer": "የባንክ ዝውውር",
    "Mobile money": "የሞባይል ገንዘብ",
    "Cheque": "ቼክ",
    "Card": "ካርድ",
    "Credit (Pay later)": "ብድር (በኋላ ክፍያ)",
    "Mixed": "ቅይጥ",

    # -- Receipt kinds ------------------------------------------------------
    "Sale receipt": "የሽያጭ ደረሰኝ",
    "Payment proof": "የክፍያ ማስረጃ",
    "Delivery note": "የማድረሻ ሰነድ",
    "Other": "ሌላ",

    # -- Debt status --------------------------------------------------------
    "Open": "ክፍት",
    "Partially repaid": "በከፊል ተከፍሏል",
    "Settled": "ተጠናቋል",
    "Written off": "ተሰርዟል",
    "Cancelled (sale voided)": "ተሰርዟል (ሽያጩ ተሽሯል)",
    "Goods returned": "ዕቃ ተመልሷል",
    "Overdue": "ጊዜው አልፎበታል",
    "Due today": "ዛሬ የሚከፈል",

    # -- Roles and scope ----------------------------------------------------
    "Administrator": "አስተዳዳሪ",
    "Manager": "ሥራ አስኪያጅ",
    "Sales": "ሽያጭ",
    "Own records only": "የራሱን መዝገቦች ብቻ",
    "Own records and their team's": "የራሱንና የቡድኑን መዝገቦች",
    "Everything in the business": "በንግዱ ውስጥ ያለውን ሁሉ",

    # -- Audit actions ------------------------------------------------------
    "Created": "ተፈጥሯል",
    "Updated": "ተሻሽሏል",
    "Deleted": "ተሰርዟል",
    "Voided": "ተሽሯል",
    "Logged in": "ገብቷል",
    "Logged out": "ወጥቷል",
    "Failed login": "ያልተሳካ መግቢያ",
    "Payment recorded": "ክፍያ ተመዝግቧል",
    "Stock adjusted": "ክምችት ተስተካክሏል",
    "Admin override": "የአስተዳዳሪ ልዩ ውሳኔ",
    "Data exported": "መረጃ ወጥቷል",
    "Access changed": "ፈቃድ ተቀይሯል",

    # -- Notification channels and devices ----------------------------------
    "Stock": "ክምችት",
    "Credit": "ብድር",
    "General": "አጠቃላይ",
    "Android": "Android",
    "iOS": "iOS",
    "Web": "ድር",

    # -- Stock status -------------------------------------------------------
    "In stock": "በክምችት አለ",
    "Low stock": "ክምችት አነሰ",
    "Out of stock": "አልቋል",

    # -- Risk levels --------------------------------------------------------
    "Good standing": "መልካም አቋም",
    "Watch": "ክትትል",
    "At risk": "አደጋ ላይ",
    "Blocked": "ታግዷል",

    # -- Refusals -----------------------------------------------------------
    "You do not have permission to activate or deactivate accounts.":
        "መለያዎችን የማንቃት ወይም የማጥፋት ፈቃድ የለዎትም።",
    "You do not have permission to adjust stock.":
        "ክምችት የማስተካከል ፈቃድ የለዎትም።",
    "You do not have permission to archive a product.":
        "ምርት የማህደር የማድረግ ፈቃድ የለዎትም።",
    "You do not have permission to attach receipts.":
        "ደረሰኝ የማያያዝ ፈቃድ የለዎትም።",
    "You do not have permission to block or unblock credit.":
        "ብድር የማገድ ወይም የመፍታት ፈቃድ የለዎትም።",
    "You do not have permission to manage access.":
        "ፈቃድ የማስተዳደር ፈቃድ የለዎትም።",
    "You do not have permission to manage roles.":
        "ሚናዎችን የማስተዳደር ፈቃድ የለዎትም።",
    "You do not have permission to open system settings.":
        "የስርዓት ቅንብሮችን የመክፈት ፈቃድ የለዎትም።",
    "You do not have permission to receive stock.":
        "ክምችት የመቀበል ፈቃድ የለዎትም።",
    "You do not have permission to record repayments.":
        "ክፍያ የመመዝገብ ፈቃድ የለዎትም።",
    "You do not have permission to record sales.":
        "ሽያጭ የመመዝገብ ፈቃድ የለዎትም።",
    "You do not have permission to remove a receipt.":
        "ደረሰኝ የማስወገድ ፈቃድ የለዎትም።",
    "You do not have permission to reset passwords.":
        "የይለፍ ቃል የመቀየር ፈቃድ የለዎትም።",
    "You do not have permission to reverse a payment.":
        "ክፍያ የመመለስ ፈቃድ የለዎትም።",
    "You do not have permission to void a sale.":
        "ሽያጭ የመሻር ፈቃድ የለዎትም።",
    "You do not have permission to write off a debt.":
        "ዕዳ የመሰረዝ ፈቃድ የለዎትም።",
    "You do not have permission to export data.":
        "መረጃ የማውጣት ፈቃድ የለዎትም።",
    "You do not have permission to read the audit log.":
        "የኦዲት መዝገብ የማንበብ ፈቃድ የለዎትም።",
    "You do not have permission to perform this action.":
        "ይህን እርምጃ የመፈጸም ፈቃድ የለዎትም።",
    "Authentication credentials were not provided.":
        "የመግቢያ መረጃ አልቀረበም።",

    # -- Sales --------------------------------------------------------------
    "Cannot record a sale with no items.": "ያለ ዕቃ ሽያጭ መመዝገብ አይቻልም።",
    "A sale needs at least one item.": "ሽያጭ ቢያንስ አንድ ዕቃ ያስፈልገዋል።",
    "Amount paid cannot be negative.": "የተከፈለው መጠን አሉታዊ መሆን አይችልም።",
    "Sale quantity must be positive.": "የሽያጭ ብዛት አዎንታዊ መሆን አለበት።",
    "That customer is not in your customer list.":
        "ያ ደንበኛ በእርስዎ የደንበኞች ዝርዝር ውስጥ የለም።",
    "This transaction has already been voided.": "ይህ ግብይት አስቀድሞ ተሽሯል።",
    "This sale has no outstanding balance.": "ይህ ሽያጭ ቀሪ ዕዳ የለውም።",
    "You do not have access to this transaction.": "ለዚህ ግብይት ፈቃድ የለዎትም።",
    "A reason is required when voiding a transaction.":
        "ግብይትን ሲሽሩ ምክንያት ያስፈልጋል።",
    "This customer has no credit account.": "ይህ ደንበኛ የብድር መለያ የለውም።",
    "A credit sale needs a registered customer. Select or create the customer "
    "first, or collect full payment.":
        "የብድር ሽያጭ የተመዘገበ ደንበኛ ያስፈልገዋል። መጀመሪያ ደንበኛውን ይምረጡ ወይም ይፍጠሩ፣ "
        "አለበለዚያ ሙሉ ክፍያ ይሰብስቡ።",
    "You do not have permission to apply a discount. Record the sale at full "
    "price, or ask someone who can approve the discount.":
        "ቅናሽ የመስጠት ፈቃድ የለዎትም። ሽያጩን በሙሉ ዋጋ ይመዝግቡ፣ ወይም ቅናሹን ማጽደቅ የሚችል "
        "ሰው ይጠይቁ።",

    # -- Credit -------------------------------------------------------------
    "A debt cannot be opened without a customer.": "ያለ ደንበኛ ዕዳ መክፈት አይቻልም።",
    "A payment cannot be dated in the future.": "ክፍያ በወደፊት ቀን ሊመዘገብ አይችልም።",
    "A reason is required to reverse a payment.": "ክፍያ ለመመለስ ምክንያት ያስፈልጋል።",
    "A reason is required to write off a debt.": "ዕዳ ለመሰረዝ ምክንያት ያስፈልጋል።",
    "Payment amount must be greater than zero.": "የክፍያ መጠን ከዜሮ በላይ መሆን አለበት።",
    "Repayment amount must be greater than zero.": "የመክፈያ መጠን ከዜሮ በላይ መሆን አለበት።",
    "There is nothing left to settle on this debt.": "በዚህ ዕዳ ላይ የሚከፈል ነገር የለም።",
    "This debt has no outstanding balance.": "ይህ ዕዳ ቀሪ ሂሳብ የለውም።",
    "This debt was cancelled - the sale was voided.": "ይህ ዕዳ ተሰርዟል - ሽያጩ ተሽሯል።",
    "This payment has already been reversed.": "ይህ ክፍያ አስቀድሞ ተመልሷል።",
    "Only a written-off debt can be restored.": "የተሰረዘ ዕዳ ብቻ ሊመለስ ይችላል።",
    "You do not have access to this debt.": "ለዚህ ዕዳ ፈቃድ የለዎትም።",
    "You do not have access to this payment.": "ለዚህ ክፍያ ፈቃድ የለዎትም።",
    "The due date cannot be in the past.": "የመክፈያ ቀን ያለፈ መሆን አይችልም።",
    "Write-off quantity must be positive.": "የስረዛ ብዛት አዎንታዊ መሆን አለበት።",

    # -- Stock --------------------------------------------------------------
    "Restock quantity must be positive.": "የዳግም ሙሌት ብዛት አዎንታዊ መሆን አለበት።",
    "Return quantity must be positive.": "የተመላሽ ብዛት አዎንታዊ መሆን አለበት።",
    "Stock movement quantity cannot be zero.": "የክምችት እንቅስቃሴ ብዛት ዜሮ መሆን አይችልም።",

    # -- Accounts -----------------------------------------------------------
    "Incorrect username or password.": "የተጠቃሚ ስም ወይም የይለፍ ቃል ትክክል አይደለም።",
    "This account has been deactivated. Contact an administrator.":
        "ይህ መለያ ተሰናክሏል። አስተዳዳሪን ያነጋግሩ።",
    "A username is required.": "የተጠቃሚ ስም ያስፈልጋል።",
    "A phone number is required.": "የስልክ ቁጥር ያስፈልጋል።",
    "A code is required.": "ኮድ ያስፈልጋል።",
    "A role with that code already exists.": "በዚያ ኮድ ሚና አስቀድሞ አለ።",
    "That role no longer exists.": "ያ ሚና ከዚህ በኋላ የለም።",
    "That username is already taken.": "ያ የተጠቃሚ ስም አስቀድሞ ተይዟል።",
    "That person cannot be chosen as a supervisor.": "ያ ሰው እንደ አለቃ ሊመረጥ አይችልም።",
    "A user cannot report to themselves.": "ተጠቃሚ ለራሱ ሪፖርት ማድረግ አይችልም።",
    "A user cannot sell their own stock via themselves.":
        "ተጠቃሚ የራሱን ክምችት በራሱ በኩል መሸጥ አይችልም።",
    "The two passwords do not match.": "ሁለቱ የይለፍ ቃላት አይመሳሰሉም።",
    "You cannot deactivate your own account.": "የራስዎን መለያ ማሰናከል አይችሉም።",
    "Unknown role.": "ያልታወቀ ሚና።",
    "Use letters, digits and underscores only.": "ፊደላት፣ ቁጥሮችና ስመ ማጥበቂያ ብቻ ይጠቀሙ።",
    "Self-registration is currently disabled.": "የራስ ምዝገባ በአሁኑ ጊዜ ተሰናክሏል።",
    "Registration is not enabled for that role. Contact an administrator.":
        "ለዚያ ሚና ምዝገባ አልተፈቀደም። አስተዳዳሪን ያነጋግሩ።",
    "This is the only active administrator. Promote another user first.":
        "ብቸኛው ንቁ አስተዳዳሪ ይህ ነው። መጀመሪያ ሌላ ተጠቃሚ ከፍ ያድርጉ።",
    "Cannot deactivate the only remaining administrator.":
        "ብቸኛውን የቀረውን አስተዳዳሪ ማሰናከል አይቻልም።",

    # -- Common validation --------------------------------------------------
    "This field is required.": "ይህ መስክ ያስፈልጋል።",
    "This field may not be blank.": "ይህ መስክ ባዶ መሆን አይችልም።",
    "This field may not be null.": "ይህ መስክ ባዶ መሆን አይችልም።",
    "A valid number is required.": "ትክክለኛ ቁጥር ያስፈልጋል።",
    "A valid integer is required.": "ትክክለኛ ሙሉ ቁጥር ያስፈልጋል።",
    "Enter a valid email address.": "ትክክለኛ የኢሜይል አድራሻ ያስገቡ።",
    "Not found.": "አልተገኘም።",
    "Method not allowed.": "ዘዴው አልተፈቀደም።",
    "Something went wrong.": "የሆነ ችግር ተፈጥሯል።",
    "Request was throttled.": "ጥያቄው ተገድቧል።",
    "Upload a valid image. The file you uploaded was either not an image or a "
    "corrupted image.":
        "ትክክለኛ ምስል ይስቀሉ። የሰቀሉት ፋይል ምስል አይደለም ወይም የተበላሸ ነው።",
    "No file was submitted.": "ምንም ፋይል አልተላከም።",
}

#: Every language the API can answer in. English is the source, so it has no
#: table - an empty lookup means "leave the text as the code wrote it".
TABLES: dict[str, dict[str, str]] = {"am": EXACT_AM}


# ---------------------------------------------------------------------------
# Messages with a value in the middle
# ---------------------------------------------------------------------------
# Written as (english regex, amharic template). The captured groups are data -
# a product name, a quantity, an amount - and are put back untouched.
#
# Ordered most specific first: "Not enough stock ... Available: x" has to be
# tried before any looser stock pattern, or the looser one wins and eats the
# numbers.
_PATTERNS_AM: list[tuple[str, str]] = [
    (
        r"^Not enough stock for '(.+?)'\. Available: (\d+), requested: (\d+)\.$",
        "'{0}' በቂ ክምችት የለውም። ያለው፦ {1}፣ የተጠየቀው፦ {2}።",
    ),
    (
        r"^Quantity for '(.+?)' must be at least 1\.$",
        "የ'{0}' ብዛት ቢያንስ 1 መሆን አለበት።",
    ),
    (
        r"^Unit price for '(.+?)' cannot be negative\.$",
        "የ'{0}' የአንዱ ዋጋ አሉታዊ መሆን አይችልም።",
    ),
    (
        r"^Customer '(.+?)' is inactive\.$",
        "ደንበኛ '{0}' ንቁ አይደለም።",
    ),
    (
        r"^'(.+?)' is not approved for credit\. An administrator must approve "
        r"them first\.$",
        "'{0}' ለብድር አልተፈቀደም። መጀመሪያ አስተዳዳሪ ሊያጸድቀው ይገባል።",
    ),
    (
        r"^'(.+?)' is not in your product list and cannot be sold\.?$",
        "'{0}' በእርስዎ የምርት ዝርዝር ውስጥ የለም፤ ሊሸጥ አይችልም።",
    ),
    (
        r"^Credit is blocked for '(.+?)'\. Reason: (.+)$",
        "ለ'{0}' ብድር ታግዷል። ምክንያት፦ {1}",
    ),
    (
        r"^You do not have permission to sell on credit\. Collect the full "
        r"amount \((.+?)\) to complete this sale\.$",
        "በብድር የመሸጥ ፈቃድ የለዎትም። ሽያጩን ለመጨረስ ሙሉ መጠኑን ({0}) ይሰብስቡ።",
    ),
    (
        r"^A debt already exists for (.+?)\.$",
        "ለ{0} ዕዳ አስቀድሞ አለ።",
    ),
    (
        r"^(.+?) has no open debts\.$",
        "{0} ክፍት ዕዳ የለውም።",
    ),
    (
        r"^(.+?) is already fully settled\.$",
        "{0} አስቀድሞ ሙሉ በሙሉ ተከፍሏል።",
    ),
    (
        r"^Ensure this value is less than or equal to (.+?)\.$",
        "ይህ ዋጋ ከ{0} ያነሰ ወይም እኩል መሆኑን ያረጋግጡ።",
    ),
    (
        r"^Ensure this value is greater than or equal to (.+?)\.$",
        "ይህ ዋጋ ከ{0} የበለጠ ወይም እኩል መሆኑን ያረጋግጡ።",
    ),
    (
        r"^Ensure this field has no more than (\d+) characters\.$",
        "ይህ መስክ ከ{0} ቁምፊዎች እንዳይበልጥ ያድርጉ።",
    ),
    (
        r"^Expected available quantity, got (.+)$",
        "የሚገኝ ብዛት ተጠብቆ ነበር፤ የተገኘው {0}",
    ),
]

PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {
    "am": [(re.compile(rx), template) for rx, template in _PATTERNS_AM]
}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def normalise_language(raw: str) -> str:
    """
    Turn whatever arrived into a bare code we might have a table for.

    Accept-Language comes from the outside world and can say anything, so this
    never raises and never returns something that is not a short code.
    """
    if not raw:
        return ""
    first = str(raw).split(",")[0].strip().lower()
    return first.split("-")[0][:5]


def translate(text: str, lang: str) -> str:
    """
    One message in `lang`, or the English it was given.

    Never raises and never returns an empty string: a blank error message is
    worse than an English one, because the user cannot even search for it.
    """
    if not text or not isinstance(text, str):
        return text
    code = normalise_language(lang)
    if not code:
        return text

    table = TABLES.get(code)
    if table:
        # Trailing whitespace is common in messages assembled across lines.
        hit = table.get(text) or table.get(text.strip())
        if hit:
            return hit

    for pattern, template in PATTERNS.get(code, ()):
        match = pattern.match(text.strip())
        if match:
            groups = match.groups()
            try:
                return template.format(*groups)
            except (IndexError, KeyError):
                # A template and its regex that disagree about how many
                # values there are must not take down the response - the
                # English message is still useful.
                return text
    return text


def known_messages() -> set[str]:
    """Every English string this module can translate. Used by the tests."""
    return set(EXACT_AM)
