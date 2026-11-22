from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from companies.models import PlatformOwner


class Command(BaseCommand):
    help = "Register an existing Django superuser as the offline platform owner."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)

    def handle(self, *args, **options):
        username = options["username"].strip()
        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist as exc:
            raise CommandError("The specified user does not exist.") from exc

        if not user.is_active or not user.is_superuser:
            raise CommandError("Only an active Django superuser can be registered as platform owner.")

        if PlatformOwner.objects.exclude(user=user).exists():
            raise CommandError("A platform owner is already registered. Use the controlled recovery process.")

        owner, created = PlatformOwner.objects.get_or_create(user=user)
        if not created and not owner.is_active:
            raise CommandError("The existing platform-owner profile is inactive; do not silently reactivate it.")

        self.stdout.write(self.style.SUCCESS(
            f"Platform owner {'registered' if created else 'already registered'}: {user.get_username()}"
        ))
