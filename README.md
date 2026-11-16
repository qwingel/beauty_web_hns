# Beauty website

Django website for the Beauty CS 1.6 community. Anticheat is one private app,
not the boundary of the whole website. Planned sections are player profiles and
statistics, bans, unban tickets, leaderboards, server monitoring and mix history.
Only the anticheat backend is implemented at this stage.

## Structure

- `config/`: shared Django configuration, URL routing, WSGI and ASGI entry points.
- `accounts/`: site accounts, currently standard Django login and permissions.
  The custom User model leaves room for Steam login later. Telemetry identities
  are separate from site accounts; there is no automatic account linking.
- `anticheat/`: PostgreSQL models/migrations, read-only telemetry administration,
  and the external archive worker as a Django management command.
- `deploy/`: example systemd unit for the worker.

The foundation uses Django 5.2 LTS, PostgreSQL 14+ and Python 3.10+.
No public frontend, Steam login, S3 upload or automatic deletion is implemented.
There is no separate API yet. `/admin/` uses Django login and staff/model
permissions; anonymous and ordinary site users cannot inspect anticheat data.
Telemetry is read-only in the admin. Servers can be configured by administrators.

## Producer boundary

The game plugin, journal validator and wire-format field definitions remain in
`D:/projects/beauty-server`. The worker imports `tools/ac_archive.py` from that
checkout via `AC_PRODUCER_TOOLS`; it does not duplicate the validator or field
layouts. Keep the producer checkout available and test its validator when
upgrading the worker or plugin.

Only bhop and gstrafe are collected, with 31-command windows: 15 before the event,
the event command and 15 after it. Collection has been checked on the VPS;
isolated overhead still requires an A/B test. This backend has not been deployed
to the VPS yet.

## Local setup (PowerShell)

Create a PostgreSQL database and role first, then:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:DJANGO_SECRET_KEY = 'replace-with-a-long-random-secret'
$env:DJANGO_DEBUG = '1'
$env:DATABASE_URL = 'postgresql://beauty_ac:password@127.0.0.1:5432/beauty_ac'
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

`.env.example` lists environment settings. Django does **not** load `.env` files
automatically. Pass variables through the shell or the service manager. Do not
commit credentials, journals or generated archives. Production needs HTTPS and
an appropriate `DJANGO_ALLOWED_HOSTS`; secure cookies are enabled outside debug.

## Process archives

Use a fixed, unique UUID for each game server. Keep it across restarts and deploys.
Point the worker at a local directory of finalized journals, mounted read-only
if possible. Deploy it near the spool or arrange reliable transfer of completed
`.ready` files. Transfer files under a temporary suffix and rename only after
transfer completes. Do not copy a live `.open` file into a `.ready` filename.

```powershell
$env:AC_SERVER_ID = 'b972127d-7d91-4dcb-8542-723deef81336'
$env:AC_SERVER_NAME = 'Beauty HNS'
$env:AC_PRODUCER_TOOLS = 'D:/projects/beauty-server/tools'
$env:AC_SPOOL = 'D:/ac-queue'
$env:AC_OUTPUT = 'D:/ac-archives'
.\.venv\Scripts\python.exe manage.py process_ac_archives --once
# Omit --once to watch the directory (10-second polling by default).
```

The command supports equivalent `--server-id`, `--server-name`, `--producer-tools`,
`--spool`, `--output` and `--poll-seconds` arguments. Run `migrate` explicitly before
starting workers. `--once` exits nonzero if a job fails during that pass; jobs in
retry backoff are skipped. A long-running worker logs failures and retries with
exponential backoff from 10 seconds to one hour. Connection loss terminates the
process; the service manager should restart it.

The worker:

1. Discovers only `.ready` files and registers their full SHA-256 in PostgreSQL.
2. Takes a PostgreSQL session advisory lock for each due journal. Another worker
   skips it. Locks disappear when the session dies; interrupted `processing`
   jobs can be reclaimed immediately.
3. Runs the producer-owned validator/packer. Interior corruption fails the job.
   Valid but incomplete captures retain their integrity flags and sequence gaps.
4. Streams the validated records into one immutable JSON.gz per connection
   segment, containing that segment's records, field definitions, counters and
   journal-wide integrity report. Reconnects and map changes remain separate.
   Temporary disk space is required for map packing and segment splitting.
5. Verifies gzip streams and SHA-256, publishes without overwriting existing
   files, then commits segments, summaries, decisions and archive references in
   one database transaction. Matching files left by an interrupted attempt are
   reused; conflicting/corrupt existing files fail visibly.

UUIDv5 identities are stable: player = exact authenticated SteamID; journal =
server UUID + full source SHA-256; segment = journal UUID + producer segment
number; archive = segment UUID. Pending/LAN/bot/orphan identities retain their raw
SteamID but have no shared Player row. Late `identity` records resolve the player.
Nicknames and original bytes are preserved by the producer packer.

Source journals are **never deleted**. A processed journal means its local
archives and database records were created; it does not mean S3 upload succeeded.
Archive upload states start at `pending`. `stored` requires a bucket reference
and completion timestamp, reserved for a future uploader that verifies storage.
Local files must stay outside static/public web directories.

`retain_until` initially records 12 calendar months from processing. No cleanup
runs. Extended retention for detections and pinned evidence remains undecided;
`pinned` is reserved for that later policy. The producer's spool cap still stops
collection when full, so monitor both spool and worker output capacity.

Multiple workers must share a stable source/output layout for a server and use
dedicated PostgreSQL sessions. PgBouncer transaction pooling is unsupported
because the locks belong to the session. Discovery hashes the retained spool
on every pass; increase the polling interval if that causes excess disk I/O.
The example systemd unit uses `/var/lib/beauty-ac` for output and an external
environment file. Adjust checkout/spool paths and read permissions for the service
user; the unit grants write access only to its output directory.

## Verification

Tests require a reachable PostgreSQL instance and a role allowed to create a
test database. Django creates/drops its isolated test database. The producer
checkout is required (the default test path is the sibling `beauty-server`).

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test anticheat --noinput
.\.venv\Scripts\python.exe -m unittest discover -s D:/projects/beauty-server/tools -p test_ac_archive.py -v
```

Coverage includes both movement kinds, segment isolation, float preservation,
decisions/summaries, idempotency, corruption, retry backoff, transactional rollback,
crash recovery, cross-session locking, late/pending identity, incomplete captures,
multi-server IDs, upload-state constraints and admin access.
