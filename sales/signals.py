"""Auto-provision a CreditAccount the moment a Customer is created."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Customer


@receiver(post_save, sender=Customer, dispatch_uid="create_credit_account")
def ensure_credit_account(sender, instance, created, **kwargs):
    if not created:
        return
    from credit.models import CreditAccount

    CreditAccount.objects.get_or_create(customer=instance)
