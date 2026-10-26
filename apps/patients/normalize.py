import re
import unicodedata


def normalize_name(name):
    """Case/spacing/punctuation-insensitive form used for searching and duplicate detection."""
    text = unicodedata.normalize("NFKC", name or "").casefold()
    text = re.sub(r"[.,]", " ", text)
    return " ".join(text.split())


def normalize_phone(raw):
    """Return a 10-digit number, '' for blank, or None if it cannot be a valid number."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None
