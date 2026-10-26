"""Database-level protection: append-only tables (PostgreSQL triggers)."""
from django.db import DEFAULT_DB_ALIAS, connections

APPEND_ONLY_TABLES = (
    "core_auditevent",
    "clinical_encounterrevision",
    "clinical_clinicalnote",
    "pharmacy_inventorytransaction",
    "sales_payment",
)

FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION forbid_row_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'append-only table; changes are not allowed';
END;
$$ LANGUAGE plpgsql;
"""


def install_audit_guard(sender=None, using=DEFAULT_DB_ALIAS, **kwargs):
    connection = connections[using]
    if connection.vendor != "postgresql":
        return
    existing = set(connection.introspection.table_names())
    tables = [t for t in APPEND_ONLY_TABLES if t in existing]
    if not tables:
        return
    with connection.cursor() as cursor:
        cursor.execute(FUNCTION_SQL)
        if "core_auditevent" in existing:
            cursor.execute("DROP TRIGGER IF EXISTS core_auditevent_no_change ON core_auditevent")  # Phase 2 name
        for table in tables:
            cursor.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
            cursor.execute(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION forbid_row_change()"
            )
