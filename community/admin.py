from django.contrib import admin

from .models import UnbanTicket


@admin.register(UnbanTicket)
class UnbanTicketAdmin(admin.ModelAdmin):
    list_display = ("subject", "ban_id", "author", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("subject", "author__username")
    readonly_fields = ("status",)
