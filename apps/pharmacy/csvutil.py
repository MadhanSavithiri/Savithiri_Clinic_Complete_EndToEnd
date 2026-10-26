import csv

from django.http import HttpResponse

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def _safe(value):
    """Stop spreadsheet formula injection: text starting with = + - @ is shown literally."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_DANGEROUS) else text


def csv_response(filename, header, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")  # lets Excel open UTF-8 correctly
    writer = csv.writer(response)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_safe(c) for c in row])
    return response
