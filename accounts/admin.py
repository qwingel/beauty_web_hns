from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class SiteUserAdmin(UserAdmin):
    readonly_fields = ("steam_id",)

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_change_permission(request, obj)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is not None and request.user.is_superuser:
            return (*fieldsets, ("Steam", {"fields": ("steam_id",)}))
        return fieldsets
