import calendar
from uuid import uuid4

from django.db import models
from django.db.models import Q
from django.utils import timezone


def default_retention():
    now = timezone.now()
    year = now.year + 1
    return now.replace(year=year, day=min(now.day, calendar.monthrange(year, now.month)[1]))


class MovementKind(models.IntegerChoices):
    BHOP = 0, "Bhop"
    GSTRAFE = 1, "Gstrafe"


class Server(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.CharField(max_length=128)

    def __str__(self):
        return self.name


class Player(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    steamid = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.steamid


class Journal(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        FAILED = "failed", "Failed"
        PROCESSED = "processed", "Processed"

    id = models.UUIDField(primary_key=True, editable=False)
    server = models.ForeignKey(Server, on_delete=models.PROTECT)
    source_sha256 = models.CharField(max_length=64)
    source_path = models.TextField()
    state = models.CharField(max_length=16, choices=State, default=State.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    map_name = models.CharField(max_length=128, blank=True)
    started_at = models.DateTimeField(null=True)
    integrity = models.JSONField(null=True)
    local_path = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["server", "source_sha256"], name="ac_journal_source_unique"),
            models.CheckConstraint(condition=Q(source_sha256__regex=r"^[0-9a-f]{64}$"), name="ac_journal_hash_valid"),
            models.CheckConstraint(condition=Q(state__in=["pending", "processing", "failed", "processed"]), name="ac_journal_state_valid"),
        ]
        indexes = [models.Index(fields=["server", "next_attempt_at"], condition=~Q(state="processed"), name="ac_journal_queue")]


class Segment(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="segments")
    segment_number = models.PositiveIntegerField()
    player = models.ForeignKey(Player, null=True, on_delete=models.PROTECT, related_name="segments")
    steamid = models.CharField(max_length=64, blank=True)
    name = models.TextField(blank=True)
    name_raw_hex = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True)
    ended_at = models.DateTimeField(null=True)
    metadata = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["journal", "segment_number"], name="ac_segment_number_unique"),
            models.CheckConstraint(condition=Q(segment_number__gt=0), name="ac_segment_number_positive"),
        ]
        indexes = [models.Index(fields=["player", "-started_at"], name="ac_player_history")]


class Summary(models.Model):
    segment = models.ForeignKey(Segment, on_delete=models.PROTECT, related_name="summaries")
    kind = models.PositiveSmallIntegerField(choices=MovementKind)
    counters = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["segment", "kind"], name="ac_summary_kind_unique"),
            models.CheckConstraint(condition=Q(kind__in=[0, 1]), name="ac_summary_kind_valid"),
        ]


class Decision(models.Model):
    segment = models.ForeignKey(Segment, on_delete=models.PROTECT, related_name="decisions")
    sequence = models.PositiveBigIntegerField()
    command_id = models.BigIntegerField(null=True)
    kind = models.PositiveSmallIntegerField(choices=MovementKind)
    payload = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["segment", "sequence"], name="ac_decision_sequence_unique"),
            models.CheckConstraint(condition=Q(kind__in=[0, 1]), name="ac_decision_kind_valid"),
        ]


class Archive(models.Model):
    class UploadState(models.TextChoices):
        PENDING = "pending", "Pending"
        UPLOADING = "uploading", "Uploading"
        FAILED = "failed", "Failed"
        STORED = "stored", "Stored"

    id = models.UUIDField(primary_key=True, editable=False)
    segment = models.OneToOneField(Segment, on_delete=models.PROTECT, related_name="archive")
    format_version = models.PositiveSmallIntegerField(default=1)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    local_path = models.TextField()
    local_verified_at = models.DateTimeField(default=timezone.now)
    upload_state = models.CharField(max_length=16, choices=UploadState, default=UploadState.PENDING)
    upload_attempts = models.PositiveIntegerField(default=0)
    upload_next_attempt_at = models.DateTimeField(default=timezone.now)
    upload_error = models.TextField(blank=True)
    object_key = models.TextField(unique=True)
    storage_bucket = models.TextField(blank=True)
    storage_endpoint = models.TextField(blank=True)
    storage_region = models.CharField(max_length=128, blank=True)
    storage_version_id = models.TextField(blank=True)
    storage_etag = models.TextField(blank=True)
    stored_at = models.DateTimeField(null=True)
    retain_until = models.DateTimeField(default=default_retention)
    pinned = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(sha256__regex=r"^[0-9a-f]{64}$"), name="ac_archive_hash_valid"),
            models.CheckConstraint(condition=Q(format_version=1), name="ac_archive_format_valid"),
            models.CheckConstraint(condition=Q(size_bytes__gt=0), name="ac_archive_size_positive"),
            models.CheckConstraint(condition=Q(upload_state__in=["pending", "uploading", "failed", "stored"]), name="ac_upload_state_valid"),
            models.CheckConstraint(condition=~Q(upload_state="stored") | (Q(stored_at__isnull=False) & ~Q(storage_bucket="")), name="ac_stored_reference_required"),
        ]
        indexes = [
            models.Index(fields=["upload_next_attempt_at"], condition=Q(upload_state__in=["pending", "uploading", "failed"]), name="ac_upload_queue"),
            models.Index(fields=["retain_until"], condition=Q(pinned=False), name="ac_archive_retention"),
        ]
