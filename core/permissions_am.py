"""
Amharic for the permission catalogue.

WHY IT LIVES IN ITS OWN FILE
----------------------------
The catalogue in core/permissions.py is read by people adding a permission,
and doubling every entry with a second language would bury the structure. It
is also read by a translator, who should not have to pick their way around
dataclasses to find a sentence.

WHY NOT DJANGO'S gettext
------------------------
These strings have to reach three places: server-rendered HTML, the browser's
own client-side translator (static/js/i18n.js, which swaps text after the page
loads), and the phone app, which asks the API for the catalogue and renders it
itself. gettext solves the first and neither of the others. A plain dictionary
serves all three: the API sends whichever language was asked for, and
`manage.py sync_permission_i18n` writes the same pairs into the JSON files the
browser fetches.

KEY SHAPE
---------
    perm.<code>.label      the checkbox caption
    perm.<code>.help       the grey sentence under it
    group.<key>.label      the section heading
    group.<key>.blurb      the sentence under the heading

A missing key falls back to English. That is deliberate: a permission added
today and translated next week must still be usable, and an untranslated
checkbox is a smaller problem than a blank one.
"""

#: Amharic. Keys follow the shape described above.
AMHARIC: dict[str, str] = {
    # -- Groups -------------------------------------------------------------
    "group.overview.label": "አጠቃላይ እይታ",
    "group.overview.blurb": "ሁሉም ሰው ከገባ በኋላ የሚያየው መነሻ ገጽ።",
    "group.catalog.label": "ምርቶች እና ክምችት",
    "group.catalog.blurb": "መደርደሪያው፦ ምን እንዳለ፣ ስንት እንደተገዛ እና ስንት እንደቀረ።",
    "group.sales.label": "ሽያጭ",
    "group.sales.blurb": "ካዝናው፣ እና በእሱ በኩል ያለፈው ሁሉ።",
    "group.customers.label": "ደንበኞች",
    "group.customers.blurb": "የደንበኞች መዝገብ። እያንዳንዱ ተጠቃሚ የራሱን ይገነባል።",
    "group.credit.label": "ብድር እና ተበዳሪዎች",
    "group.credit.blurb": "ማን ምን ዕዳ እንዳለበት፣ ምን ያህል እንደዘገየ፣ እና ማን ሊሰርዘው እንደሚችል።",
    "group.reports.label": "ሪፖርቶች",
    "group.reports.blurb": (
        "ለንባብ ብቻ የሆኑ ማጠቃለያዎች። እነዚህን መለያየት ማለት አንድ ሰው ትርፉን ሳያይ "
        "ሱቁን እንዲያስተዳድር መፍቀድ ነው።"
    ),
    "group.admin.label": "አስተዳደር",
    "group.admin.blurb": "የስርዓቱ ራሱ ቁጥጥር። በጥንቃቄ ብቻ ይስጡ።",

    # -- Overview -----------------------------------------------------------
    "perm.dashboard.view.label": "ዳሽቦርዱን መክፈት",
    "perm.dashboard.view.help": "ይህ ከሌለ ተጠቃሚው በምትኩ ወደ መገለጫ ገጹ ይሄዳል።",

    # -- Products & stock ---------------------------------------------------
    "perm.product.view.label": "የምርት ዝርዝሩን ማየት",
    "perm.product.view.help": "ማንኛውንም ነገር ለመሸጥ ያስፈልጋል — ካዝናው ምርቶችን ይዘረዝራል።",
    "perm.product.create.label": "አዲስ ምርቶች መጨመር",
    "perm.product.edit.label": "የምርት ዝርዝሮችንና ዋጋዎችን ማረም",
    "perm.product.archive.label": "ምርትን ማህደር ማድረግ (ለስላሳ ስረዛ)",
    "perm.product.archive.help": "ታሪኩ ይቀመጣል፤ ምርቱ ግን በካዝናው ላይ መታየቱ ይቆማል።",
    "perm.product.view_cost.label": "የግዢ ዋጋንና የክምችት ዋጋ ማየት",
    "perm.product.view_cost.help": (
        "የከፈሉት እንጂ የሚያስከፍሉት አይደለም። ደንበኛ ስክሪኑን ቢያይ እንዳይታይ ከሽያጭ "
        "ተጠቃሚ ተደብቋል።"
    ),
    "perm.stock.view_movements.label": "የክምችት እንቅስቃሴ ታሪክ ማየት",
    "perm.stock.restock.label": "አዲስ ክምችት መቀበል",
    "perm.stock.restock.help": "ብዛት ይጨምራል እና አቅርቦቱን ይመዘግባል።",
    "perm.stock.adjust.label": "ብልሽትንና የደንበኛ ተመላሽ መመዝገብ",
    "perm.stock.recount.label": "የተቆጠረውን የክምችት ብዛት በሌላ መተካት",
    "perm.stock.recount.help": (
        "ቁጥሩን በቀጥታ የሚያስቀምጥ ቆጠራ። ጉድለት የሚደበቀው በዚህ መንገድ ስለሆነ "
        "ለጥቂት ሰዎች ብቻ ይስጡ።"
    ),
    "perm.catalog.manage.label": "ምድቦችንና አቅራቢዎችን ማስተዳደር",

    # -- Sales --------------------------------------------------------------
    "perm.sale.view.label": "ሽያጮችንና የግብይት ታሪክ ማየት",
    "perm.sale.create.label": "ሽያጭ መመዝገብ",
    "perm.sale.credit.label": "በብድር መሸጥ (ቀሪ ዕዳ መተው)",
    "perm.sale.credit.help": (
        "ሽያጩን ደንበኛው የሚከፍለው ዕዳ ያደርገዋል። ዕዳው የሸጠው ሰው ስም ላይ ይመዘገባል።"
    ),
    "perm.sale.discount.label": "በሽያጭ ላይ ቅናሽ መስጠት",
    "perm.sale.discount.help": "ቅናሽ ማለት ቀጥታ ገንዘብ መቀነስ ነው — በጥንቃቄ ይስጡ።",
    "perm.sale.void.label": "ሽያጭን መሰረዝ",
    "perm.sale.void.help": (
        "ክምችቱን ይመልሳል እና የተያያዘውን ዕዳ ይሰርዛል። የቀኑን ገቢ ይለውጣል።"
    ),
    "perm.sale.receipt.add.label": "ደረሰኞችንና የክፍያ ማስረጃ ማያያዝ",
    "perm.sale.receipt.delete.label": "የተያያዘ ደረሰኝ መሰረዝ",
    "perm.sale.receipt.delete.help": (
        "የክፍያ ማስረጃን ማጥፋት ነው። ለመዝገቡ ተጠያቂ ለሆኑ ሰዎች ብቻ ይሰጣል።"
    ),

    # -- Customers ----------------------------------------------------------
    "perm.customer.view.label": "የደንበኞችን ዝርዝር ማየት",
    "perm.customer.create.label": "አዲስ ደንበኛ መመዝገብ",
    "perm.customer.edit.label": "የደንበኛ መረጃ ማረም",

    # -- Credit -------------------------------------------------------------
    "perm.credit.view.label": "ተበዳሪዎችን፣ ዕዳዎችንና የዕድሜ ሪፖርት ማየት",
    "perm.credit.collect.label": "ክፍያ መመዝገብ",
    "perm.credit.reschedule.label": "የዕዳ መክፈያ ቀን መቀየር",
    "perm.credit.limits.label": "የብድር ጣሪያ ማስቀመጥና ተበዳሪ ማገድ",
    "perm.credit.limits.help": (
        "ደንበኛ እስከ ምን ድረስ መበደር እንደሚችል ይወስናል። የገንዘብ ቁጥጥር ነው።"
    ),
    "perm.credit.write_off.label": "ዕዳን የማይሰበሰብ ብሎ መሰረዝ",
    "perm.credit.write_off.help": "ንግዱ መከታተል የሚያቆመው ገንዘብ ነው።",
    "perm.credit.reverse_payment.label": "የተመዘገበ ክፍያን መመለስ",
    "perm.credit.reverse_payment.help": (
        "ደረሰኙን ይሽራል። በወረቀት ላይ ገንዘብ የሚጠፋበት ሌላኛው መንገድ ነው።"
    ),

    # -- Reports ------------------------------------------------------------
    "perm.report.sales.label": "የሽያጭ ሪፖርት",
    "perm.report.inventory.label": "የክምችት ሪፖርት",
    "perm.report.receivables.label": "የሚሰበሰብ ገንዘብ ሪፖርት",
    "perm.report.profit.label": "ትርፍና የትርፍ ህዳግ",
    "perm.report.profit.help": (
        "የሸቀጥ ወጪ፣ ጠቅላላ ትርፍ፣ የትርፍ መቶኛ። በስርዓቱ ውስጥ በንግድ በኩል እጅግ "
        "ሚስጥራዊው ገጽ ነው።"
    ),
    "perm.report.export.label": "ሪፖርቶችን ወደ CSV ማውጣት",
    "perm.report.export.help": (
        "ከድርጅቱ የሚወጣ ፋይል ነው። የወጪና የትርፍ አምዶች የሚጻፉት ማየት ለሚፈቀድለት "
        "ሰው ብቻ ነው።"
    ),

    # -- Administration -----------------------------------------------------
    "perm.user.view.label": "የሠራተኞችን ዝርዝር ማየት",
    "perm.user.create.label": "የሠራተኛ መለያ መጨመር",
    "perm.user.edit.label": "የሠራተኛ መረጃ ማረም",
    "perm.user.deactivate.label": "መለያዎችን ማንቃትና ማጥፋት",
    "perm.user.reset_password.label": "የሌላ ተጠቃሚን የይለፍ ቃል መቀየር",
    "perm.user.reset_password.help": "ያ ተጠቃሚ ከሁሉም መሣሪያዎች እንዲወጣም ያደርጋል።",
    "perm.user.permissions.label": "ፈቃድ መስጠትና መንሳት",
    "perm.user.permissions.help": (
        "ይህ ያለው ሰው ለራሱ ማንኛውንም ሌላ ፈቃድ መስጠት ይችላል። እንደ ሙሉ ቁጥጥር ይቁጠሩት።"
    ),
    "perm.role.manage.label": "ሚናዎችን መፍጠርና ማረም",
    "perm.role.manage.help": "የአንድን ቡድን ሰዎች ሙሉ ፈቃድ በአንድ ጊዜ ይለውጣል።",
    "perm.settings.view.label": "የስርዓት ቅንብሮችን መክፈት",
    "perm.settings.edit.label": "የስርዓት ቅንብሮችን መቀየር",
    "perm.audit.view.label": "የኦዲት መዝገብን ማንበብ",
    "perm.audit.view.help": "የሁሉንም ሰው እንቅስቃሴ እንጂ የራሱን ብቻ አይደለም።",

    # -- Odds and ends ------------------------------------------------------
    "perm.wildcard.label": "ሁሉንም ነገር ሙሉ ፈቃድ",
}

#: Every language the catalogue can be served in. English is not listed
#: because it is the source: an empty table means "use what is written in
#: permissions.py", which is exactly the right fallback.
TRANSLATIONS: dict[str, dict[str, str]] = {
    "am": AMHARIC,
}


def translate(key: str, lang: str, default: str) -> str:
    """
    One catalogue string in `lang`, falling back to the English `default`.

    Never raises and never returns an empty string: a checkbox with no caption
    is worse than a checkbox in the wrong language.
    """
    if not lang:
        return default
    table = TRANSLATIONS.get(lang.lower().split("-")[0])
    if not table:
        return default
    return table.get(key) or default
