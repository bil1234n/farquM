"""
Shared form widgets and fields.

    ReceiptField    a file field that can actually accept more than one file -
                    shared because getting it wrong is subtle and the wrong
                    version was independently copy-pasted into three forms
    unit_choices    the editable unit lists, as select choices
    NoteTagField    the coloured mark on a note (Good / Normal / Bad ...)
"""
import re

from django import forms

from .utils import validate_receipt_file


class MultipleFileInput(forms.ClearableFileInput):
    """Opts the widget in to `<input type="file" multiple>`."""

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """
    A FileField that accepts a LIST of uploads.

    WHY THIS EXISTS
    ---------------
    Setting `allow_multiple_selected = True` on the widget alone is a trap,
    and it produced this bug:

        No file was submitted. Check the encoding type on the form.

    That message is badly misleading. The form's encoding was correct and the
    files were arriving fine. What happens is:

      1. `FileInput.value_from_datadict` checks `allow_multiple_selected`, and
         when it is True returns `files.getlist(name)` - a LIST.
      2. Plain `forms.FileField.to_python` receives that list and does
         `data.name` to read the filename.
      3. A list has no `.name`, so it raises AttributeError, which FileField
         catches and reports as its generic `'invalid'` error - and the text
         of that error happens to mention encoding.

    So the field failed the moment a file was ACTUALLY selected, and appeared
    to work whenever the input was left empty (an empty list is in
    `empty_values`, so it short-circuits). That is why it looked like an
    encoding problem: the only time it complained was when there was a file.

    The fix is the pattern from the Django docs ("Uploading multiple files"):
    keep the widget opt-in, and override `clean` to run the normal
    single-file cleaning once per uploaded file.
    """

    widget = MultipleFileInput

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean

        if isinstance(data, (list, tuple)):
            # Browsers submit one empty part for an untouched multiple input,
            # so strip the blanks before validating or a form with no
            # attachment fails for no reason the user can see.
            files = [f for f in data if f not in self.empty_values]
            if not files:
                if self.required:
                    raise forms.ValidationError(
                        self.error_messages["required"], code="required"
                    )
                return []
            return [single_clean(f, initial) for f in files]

        # A single file (or nothing) - behave exactly like FileField.
        cleaned = single_clean(data, initial)
        return [cleaned] if cleaned else []


class ReceiptField(MultipleFileField):
    """
    Multi-file field with the receipt size/type rules already attached.

    Validation lives on the field rather than in each view so that the browser
    gets a proper form error next to the input, instead of a 500 from the
    model validator firing later during save.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", "Receipt / proof of payment")
        kwargs.setdefault(
            "widget",
            MultipleFileInput(
                attrs={"multiple": True, "accept": "image/*,application/pdf"}
            ),
        )
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = super().clean(data, initial)
        for f in files:
            validate_receipt_file(f)
        return files


def unit_choices(group, builtin, current=""):
    """
    The (code, label) pairs for an editable unit list.

    WHY NOT JUST THE MODEL'S `choices`
    ----------------------------------
    Those are frozen at deploy time. "Sold by" and "Measured in" are lists the
    yard maintains itself - a jerrycan, a wheelbarrow - so a form built from
    the shipped seven would refuse a unit the app offered on a phone an hour
    earlier, with "Select a valid choice".

    `current` keeps the value a record already holds selectable even when that
    entry has since been deactivated. Without it, opening an old product to
    fix a typo would quietly move it onto a different unit on save.
    """
    from .models import Option

    rows = list(
        Option.objects.in_group(group)
        .active()
        .exclude(code="")
        .order_by("sort_order", "-use_count", "label")
        .values_list("code", "label")
    )
    if not rows:
        # Nothing seeded yet - a fresh database mid-migration, say. The
        # built-ins are a working list, not an empty dropdown.
        rows = [(code, str(label)) for code, label in builtin.choices]

    known = {code for code, _ in rows}
    current = (current or "").strip()
    if current and current not in known:
        fallback = dict(builtin.choices).get(
            current, current.replace("_", " ").title()
        )
        rows.append((current, str(fallback)))
    return rows


# ---------------------------------------------------------------------------
# Coloured note marks
# ---------------------------------------------------------------------------
# A mark is a colour with a meaning - green "Good", red "Bad" - pinned to a
# note so the reader knows what kind of note it is before reading a word of
# it. The list is the NOTE_TAG group of core.Option, editable by the people
# who use it (see api/option_views.py), so these helpers only ever READ it.
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
#: What a mark with no usable colour is painted in: slate, which reads as
#: "a mark" without claiming to mean good or bad.
DEFAULT_MARK_COLOR = "#64748B"


def mark_color(option) -> str:
    """
    The colour to paint a mark in.

    Only ever a #RRGGBB value: it is written into a style attribute, and the
    API is not the only thing that can put a row in the table.
    """
    color = (getattr(option, "color", "") or "").strip()
    return color.upper() if HEX_COLOR.match(color) else DEFAULT_MARK_COLOR


def note_tag_queryset(current=None):
    """
    The marks a form offers: every live one, plus whichever the record
    already carries even if it has since been switched off - so opening an
    old note to fix a typo does not quietly strip its colour.
    """
    from django.db.models import Q

    from .models import Option

    wanted = Q(is_active=True)
    current_id = getattr(current, "pk", current)
    if current_id:
        wanted |= Q(pk=current_id)
    return (
        Option.objects.in_group("NOTE_TAG")
        .filter(wanted)
        .order_by("sort_order", "label")
    )


class NoteTagSelect(forms.Select):
    """
    An ordinary <select> that carries each mark's colour.

    It works with no JavaScript at all. static/js/note-tags.js then draws it
    as a row of coloured chips, with a place to add a mark or change what one
    means and what colour it is - and tints the note box beside it, so the
    colour is visible while the note is being written, not only afterwards.
    """

    def __init__(self, attrs=None, note_field="notes"):
        base = {"class": "form-select", "data-note-tag": "1"}
        if note_field:
            base["data-note-field"] = note_field
        base.update(attrs or {})
        super().__init__(base)

    def create_option(self, name, value, label, selected, index, subindex=None,
                      attrs=None):
        option = super().create_option(
            name, value, label, selected, index, subindex, attrs
        )
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-color"] = mark_color(instance)
        return option


class NoteTagField(forms.ModelChoiceField):
    """The mark on a note. Optional - most notes are simply notes."""

    def __init__(self, *, current=None, note_field="notes", **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", "Mark")
        kwargs.setdefault("empty_label", "No mark")
        kwargs.setdefault(
            "help_text",
            "A colour for the note, so its meaning shows before anyone reads it.",
        )
        kwargs.setdefault("widget", NoteTagSelect(note_field=note_field))
        super().__init__(queryset=note_tag_queryset(current), **kwargs)


class NoteTagFormMixin:
    """
    For a ModelForm whose Meta.fields includes the note's mark.

    Swaps Django's default select (every NOTE_TAG row, live or not, with no
    colour) for the coloured picker, offering only live marks plus the one the
    record already has. Put it FIRST in the bases so it runs after the field
    styling has been applied.
    """

    note_tag_field = "note_tag"
    #: The text field the mark describes, so the picker can tint it.
    note_text_field = "notes"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.note_tag_field in self.fields:
            instance = getattr(self, "instance", None)
            current = getattr(instance, f"{self.note_tag_field}_id", None)
            old = self.fields[self.note_tag_field]
            self.fields[self.note_tag_field] = NoteTagField(
                current=current,
                note_field=self.note_text_field,
                label=old.label if old.label and old.label != "Note tag" else "Mark",
            )
