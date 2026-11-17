from django.contrib import admin

from .models import Archive, Decision, Journal, Player, Segment, Server, Summary


@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = ("name", "id")


class TelemetryAdmin(admin.ModelAdmin):
    """Collected evidence is inspectable but cannot be edited in the admin."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Player)
class PlayerAdmin(TelemetryAdmin):
    list_display = ("steamid", "created_at")
    search_fields = ("steamid",)


@admin.register(Journal)
class JournalAdmin(TelemetryAdmin):
    list_display = ("id", "server", "map_name", "state", "attempts", "processed_at", "source_deleted_at")
    list_filter = ("state", "server")


@admin.register(Segment)
class SegmentAdmin(TelemetryAdmin):
    list_display = ("id", "steamid", "name", "started_at", "ended_at")
    search_fields = ("steamid", "name")


@admin.register(Archive)
class ArchiveAdmin(TelemetryAdmin):
    list_display = ("id", "size_bytes", "upload_state", "retain_until", "pinned")
    list_filter = ("upload_state", "pinned")


admin.site.register(Summary, TelemetryAdmin)
admin.site.register(Decision, TelemetryAdmin)
