"""Gap-aware, concurrency-safe document numbering.

Call next_number() INSIDE the same transaction.atomic() block that saves the document.
If that transaction rolls back, the counter rolls back too, so numbers are not wasted.
The sequence row is locked (SELECT ... FOR UPDATE) so two workstations can never get the same number.
"""
from collections import namedtuple

from django.db import IntegrityError, transaction

from .models import DocType, DocumentSequence

Number = namedtuple("Number", "value formatted")

# doc_type -> (default prefix, zero padding). Editable per sequence in the admin.
DEFAULT_FORMATS = {
    DocType.PATIENT: ("P", 6),
    DocType.APPOINTMENT: ("AP", 6),
    DocType.TOKEN: ("T", 3),
    DocType.PRESCRIPTION: ("RX", 6),
    DocType.CONSULT_RECEIPT: ("RC", 6),
    DocType.PHARMACY_INVOICE: ("PH", 6),
    DocType.PURCHASE_INVOICE: ("PU", 6),
    DocType.COUNTER_SALE: ("CS", 6),
    DocType.CREDIT_NOTE: ("CN", 6),
}


def next_number(doc_type, period=""):
    doc_type = str(doc_type)
    with transaction.atomic():
        seq = DocumentSequence.objects.select_for_update().filter(doc_type=doc_type, period=period).first()
        if seq is None:
            prefix, padding = DEFAULT_FORMATS.get(doc_type, ("", 6))
            try:
                with transaction.atomic():  # savepoint: survive a creation race
                    seq = DocumentSequence.objects.create(
                        doc_type=doc_type, period=period, prefix=prefix, padding=padding
                    )
            except IntegrityError:
                seq = DocumentSequence.objects.select_for_update().get(doc_type=doc_type, period=period)
        value = seq.next_value
        seq.next_value = value + 1
        seq.save(update_fields=["next_value", "updated_at"])

    parts = [p for p in (seq.prefix, period) if p]
    formatted = "-".join(parts + [f"{value:0{seq.padding}d}"])
    return Number(value, formatted)
