from django.conf import settings
from django.db import models


class UnbanTicket(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "На рассмотрении"
        APPROVED = "approved", "Одобрен"
        REJECTED = "rejected", "Отклонён"

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    ban_id = models.PositiveBigIntegerField()
    subject = models.CharField(max_length=160)
    message = models.TextField()
    status = models.CharField(max_length=8, choices=Status, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_unban_tickets",
    )
    review_version = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "Тикет на разбан"
        verbose_name_plural = "Тикеты на разбан"

    def __str__(self):
        return self.subject
