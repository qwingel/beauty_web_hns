from django.contrib import admin

from .models import UnbanTicket


@admin.register(UnbanTicket)
class UnbanTicketAdmin(admin.ModelAdmin):
    list_display = ("subject", "ban_id", "author", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("subject", "author__username")
    readonly_fields = ("status", "reviewed_at", "reviewed_by", "review_version")

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return (*fields, "ban_id", "author") if obj else fields
