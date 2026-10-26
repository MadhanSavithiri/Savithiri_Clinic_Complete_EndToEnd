from django.core.management import call_command
from django.test import TestCase


class MigrationsUpToDateTests(TestCase):
    def test_no_model_changes_are_missing_a_migration(self):
        # Fails if someone changes a model and forgets `manage.py makemigrations`.
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)
