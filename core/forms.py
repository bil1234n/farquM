"""
Shared form widgets and fields.

Currently one thing lives here: a file field that can actually accept more
than one file. It is shared because getting it wrong is subtle and the wrong
version was independently copy-pasted into three forms.
"""
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
