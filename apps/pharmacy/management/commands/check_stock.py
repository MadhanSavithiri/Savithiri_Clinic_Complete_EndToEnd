from django.core.management.base import BaseCommand, CommandError

from apps.pharmacy.stock import reconcile


class Command(BaseCommand):
    help = "Verify that every batch quantity equals the sum of its stock-ledger rows. Run after restores and weekly."

    def handle(self, *args, **options):
        bad = reconcile()
        if bad:
            for row in bad:
                self.stderr.write(f"MISMATCH batch {row['id']} ({row['batch_number']}): on hand {row['quantity_on_hand']}, ledger {row['ledger_sum']}")
            raise CommandError(f"{len(bad)} batch(es) do not match the ledger.")
        self.stdout.write(self.style.SUCCESS("Stock ledger and batch quantities agree."))
