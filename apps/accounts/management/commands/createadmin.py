import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.core.audit import log_event


class Command(BaseCommand):
    help = "Securely create a Clinic Administrator. The password is typed hidden; there is no default account."

    def add_arguments(self, parser):
        parser.add_argument("--username")
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")
        parser.add_argument("--email", default="")

    def handle(self, *args, **options):
        User = get_user_model()
        username = (options["username"] or input("Administrator username: ")).strip()
        if not username:
            raise CommandError("Username cannot be empty.")
        if User.objects.filter(username__iexact=username).exists():
            raise CommandError(f"A user named '{username}' already exists.")

        password = getpass.getpass("Password (hidden): ")
        confirm = getpass.getpass("Repeat password: ")
        if password != confirm:
            raise CommandError("Passwords did not match. Nothing was created.")

        candidate = User(username=username, first_name=options["first_name"], last_name=options["last_name"])
        try:
            validate_password(password, candidate)
        except ValidationError as exc:
            raise CommandError("Password rejected: " + " ".join(exc.messages))

        user = User.objects.create_user(
            username=username,
            email=options["email"],
            password=password,
            first_name=options["first_name"],
            last_name=options["last_name"],
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        log_event(
            user=None,
            action="user.create_admin",
            obj=user,
            summary="Administrator created via createadmin command",
        )
        self.stdout.write(self.style.SUCCESS(f"Administrator '{username}' created."))
