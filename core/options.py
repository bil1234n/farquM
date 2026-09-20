"""
THE PICK-LIST REGISTRY - every "choose one, or add your own" list in the app.

WHY THIS EXISTS
---------------
Half the fields in a yard system are the same handful of answers typed again
every single day: which bank the transfer came from, what was wrong with the
blocks that broke, why the count did not match. Typed free-hand they arrive as
"Dashen", "dashen bank", "Dashn" and "DB" - four rows that are one bank, and a
report that can never add them up.

Modelling each one as its own table is the other mistake: eight near-identical
models, eight admin screens, eight migrations every time somebody wants a new
list. So there is one table (core.Option), partitioned by `group`, and this
file is the catalogue of groups.

RULES FOR ADDING A GROUP
------------------------
1. `key` is stored in the database on every row. Never rename one.
2. `defaults` are seeded by migration. They are a starting point, not a fixed
   list - anybody may add to them from inside the dropdown itself.
3. Anything that references an Option must ALSO snapshot its label, and use
   on_delete=SET_NULL. An option deleted a year from now must not rewrite what
   last year's sale said.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OptionGroup:
    #: Stored on every row. Never change it.
    key: str
    #: What the dropdown's label says.
    label: str
    #: Wording for the "add a new one" row inside the select.
    add_label: str = "Add new"
    help: str = ""
    #: Seeded once by migration. Either a plain label, or a (code, label)
    #: pair when something stores the CODE rather than the wording - a unit,
    #: for instance, where every product row already holds "PIECE".
    defaults: tuple = field(default_factory=tuple)

    @property
    def is_coded(self) -> bool:
        """
        Whether rows elsewhere store this group's code instead of its label.

        A coded group can be RENAMED safely - "Piece" becoming "Each" leaves
        every product still pointing at PIECE. An uncoded one is referenced by
        id with a snapshot of the wording, which is the right shape for a bank
        or a damage type.
        """
        return any(isinstance(d, tuple) for d in self.defaults)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
GROUPS: tuple[OptionGroup, ...] = (
    OptionGroup(
        key="DAMAGE_TYPE",
        label="Damage type",
        add_label="Add a damage type",
        help="What went wrong with the units that failed.",
        defaults=(
            "Broken",
            "Cracked",
            "Chipped edge",
            "Under-cured / weak",
            "Wrong size",
            "Out of shape",
            "Colour defect",
            "Broken while handling",
            "Broken in transport",
            "Rain damaged",
        ),
    ),
    OptionGroup(
        key="BANK",
        label="Bank",
        add_label="Add a bank",
        help="Which bank the transfer or cheque went through.",
        defaults=(
            "Commercial Bank of Ethiopia (CBE)",
            "Awash Bank",
            "Dashen Bank",
            "Bank of Abyssinia",
            "Cooperative Bank of Oromia",
            "Wegagen Bank",
            "Hibret Bank (United)",
            "Nib International Bank",
            "Oromia Bank",
            "Lion International Bank",
            "Zemen Bank",
            "Bunna Bank",
            "Berhan Bank",
            "Abay Bank",
            "Enat Bank",
            "Addis International Bank",
            "Debub Global Bank",
            "Amhara Bank",
            "Tsehay Bank",
            "Ahadu Bank",
            "Siinqee Bank",
            "Shabelle Bank",
            "Hijra Bank",
            "ZamZam Bank",
            "Rammis Bank",
            "Goh Betoch Bank",
            "Gadaa Bank",
            "Sidama Bank",
            "Tsedey Bank",
            "Omo Bank",
            "Development Bank of Ethiopia",
        ),
    ),
    OptionGroup(
        key="MOBILE_MONEY",
        label="Mobile money",
        add_label="Add a provider",
        help="Which wallet the money came from.",
        defaults=(
            "telebirr",
            "M-PESA",
            "CBE Birr",
            "Amole",
            "HelloCash",
            "Awash Birr",
            "E-Birr",
        ),
    ),
    OptionGroup(
        key="STOCK_ADJUST_REASON",
        label="Reason",
        add_label="Add a reason",
        help="Why the shelf does not match the card.",
        defaults=(
            "Broken in the yard",
            "Broken while loading",
            "Lost",
            "Stolen",
            "Returned by customer",
            "Stock-take correction",
            "Sample / give-away",
        ),
    ),
    OptionGroup(
        key="MATERIAL_WASTE_REASON",
        label="Reason",
        add_label="Add a reason",
        help="Why material left the store without making anything.",
        defaults=(
            "Bag set solid",
            "Spilled",
            "Rain damaged",
            "Wrong mix - thrown away",
            "Returned to supplier",
            "Stock-take correction",
        ),
    ),
    OptionGroup(
        key="PRODUCT_UNIT",
        label="Sold by",
        add_label="Add a unit",
        help="How a finished product is counted and sold.",
        defaults=(
            ("PIECE", "Piece"),
            ("BOX", "Box"),
            ("CARTON", "Carton"),
            ("KG", "Kilogram"),
            ("LITRE", "Litre"),
            ("METER", "Meter"),
            ("PACK", "Pack"),
        ),
    ),
    OptionGroup(
        key="MATERIAL_UNIT",
        label="Measured in",
        add_label="Add a unit",
        help="How a raw material is weighed or counted in the store.",
        # Deliberately not the same list as PRODUCT_UNIT: nobody sells a cubic
        # metre of hollow blocks, and nobody buys cement by the "carton".
        # Sharing one list would put wrong options in both dropdowns.
        defaults=(
            ("KG", "Kilogram"),
            ("TONNE", "Tonne"),
            ("BAG", "Bag"),
            ("M3", "Cubic metre"),
            ("LITRE", "Litre"),
            ("PIECE", "Piece"),
            ("METER", "Metre"),
            ("ROLL", "Roll"),
        ),
    ),
    OptionGroup(
        key="PRODUCTION_REQUEST_REASON",
        label="Reason",
        add_label="Add a reason",
        help="Why more of this product is needed.",
        defaults=(
            "Stock is low",
            "Out of stock",
            "Customer order waiting",
            "Large order coming",
            "Regular top-up",
        ),
    ),
)

GROUP_BY_KEY: dict[str, OptionGroup] = {g.key: g for g in GROUPS}
GROUP_KEYS: tuple[str, ...] = tuple(g.key for g in GROUPS)
#: For a model field's `choices=`. Keeps the admin readable without making the
#: column a hard enum - a group added in a later build must not invalidate
#: rows written by this one.
GROUP_CHOICES = [(g.key, g.label) for g in GROUPS]


def group_for(key: str) -> OptionGroup | None:
    return GROUP_BY_KEY.get((key or "").strip().upper())


def is_known(key: str) -> bool:
    return group_for(key) is not None


def seed_pairs() -> list[tuple[str, str, str, int]]:
    """(group, code, label, sort_order) for every default, for the migration."""
    rows: list[tuple[str, str, str, int]] = []
    for group in GROUPS:
        for index, entry in enumerate(group.defaults):
            code, label = entry if isinstance(entry, tuple) else ("", entry)
            rows.append((group.key, code, label, (index + 1) * 10))
    return rows


#: Groups whose code is written into another table's column.
CODED_GROUPS: frozenset[str] = frozenset(g.key for g in GROUPS if g.is_coded)


def is_coded(key: str) -> bool:
    return (key or "").strip().upper() in CODED_GROUPS


# ---------------------------------------------------------------------------
# Which list a payment method draws from
# ---------------------------------------------------------------------------
#: A bank transfer and a cheque both name a bank; mobile money names a wallet;
#: cash and credit name nothing. Kept here rather than in sales so the two
#: clients and the web form all read one answer.
PAYMENT_CHANNEL_GROUPS: dict[str, str] = {
    "BANK": "BANK",
    "CHEQUE": "BANK",
    "MOBILE": "MOBILE_MONEY",
}


def channel_group_for_method(method: str) -> str:
    """The Option group a payment method picks from, or "" for none."""
    return PAYMENT_CHANNEL_GROUPS.get((method or "").strip().upper(), "")
