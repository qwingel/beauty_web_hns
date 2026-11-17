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
No public frontend, Steam login or automatic deletion is implemented.
Private S3-compatible uploads are implemented with streamed read-back verification.
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
isolated collection overhead still requires an A/B test. The backend worker is
deployed on the game VPS; see the deployment status below.

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
and completion timestamp and is set only after successful S3 read-back verification.
Local files must stay outside static/public web directories.

## Private S3 uploads (Timeweb Cloud)

The selected destination is the private Timeweb Cloud bucket
`687cfbdb-532d-4bdc-8d35-8c096a8ddfda`, endpoint `https://s3.twcstorage.ru`,
region `ru-1`. `.env.example` contains these values and uses path-style addressing,
matching [Timeweb's boto3 example](https://github.com/timeweb-cloud/s3-examples/blob/master/python3/main.py).
The live bucket must already exist and have public access disabled. The worker
does not create buckets or change bucket policies/ACLs.

Export these settings together with the normal Django/database settings:

```powershell
$env:AC_S3_BUCKET = '687cfbdb-532d-4bdc-8d35-8c096a8ddfda'
$env:AC_S3_ENDPOINT_URL = 'https://s3.twcstorage.ru'
$env:AC_S3_REGION = 'ru-1'
$env:AC_S3_ADDRESSING_STYLE = 'path'
# Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY privately in the environment.
# Process journals, then upload due archives for this game server:
.\.venv\Scripts\python.exe manage.py process_ac_archives --upload --once
# Retry uploads without scanning the journal spool:
.\.venv\Scripts\python.exe manage.py upload_ac_archives --once
```

Omit `--once` for continuous processing. The combined worker uploads even when
no new journals arrive. Without `--upload`, journal processing remains local.
The standalone uploader accepts `--server-id` (or `AC_SERVER_ID`); without either,
it processes all servers. All workers for a server must be able to read the same
local archive paths. The systemd example enables combined processing/upload.

Credentials use boto3's standard AWS environment/profile/role chain; credentials
are never stored in PostgreSQL. Grant access to the archive prefix for
`s3:PutObject` and `s3:GetObject`. Permission to distinguish missing keys from
forbidden keys (usually `s3:ListBucket`, restricted to the relevant prefix) is
needed for new objects: `AccessDenied` is treated as a failure. No delete permission
is required. For another S3 provider, change endpoint/region/addressing style;
leave `AC_S3_ENDPOINT_URL` empty for AWS. HTTPS is required outside localhost.

Uploads have a separate durable queue: `pending` -> `uploading` -> `stored`, or
`failed` with retries from 10 seconds to one hour. PostgreSQL session locks avoid
parallel processing of the same archive; interrupted `uploading` jobs can be
reclaimed. The destination bucket, endpoint and region are bound before the
first attempt so a config change cannot silently move a retry to another store.

An existing key is read and its actual bytes are hashed. Matching content is
reused even if the local file is no longer available; conflicting content fails
without overwrite. For a new key, the local file's size/SHA-256 is checked before
an atomic single PUT with `If-None-Match: *`, followed by streamed GET verification
of the stored size and SHA-256. The verified response's ETag and optional version
ID are saved for future retrieval. ETag and user metadata are not treated as
content checksums. This read-back adds outgoing traffic equal to the archive size.

The endpoint must support conditional PUT; errors are surfaced rather than
falling back to unconditional writes. A live Timeweb smoke test on 2026-11-17
passed PUT/GET SHA-256 verification, existing-object reuse and conditional PUT
protection. Anonymous GET returned 403. The temporary test object was deleted.
The test used a temporary SSH forward through the game VPS because direct TLS
handshakes from the workstation timed out; credentials stayed on the workstation.
Single files above 5 GiB require future multipart support.
Neither local files nor source journals are removed, and no S3 lifecycle/retention
rule is installed. `stored` records successful verification at that time; it is
not a continuous audit against later external deletion or modification.

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

## VPS deployment

Deployed on `152.228.115.212` on 2026-11-17. The worker is enabled as
`beauty-ac-worker.service`, running as the dedicated `beauty-ac` system account.
The separate PostgreSQL database/role is `beauty_ac`, listening only on localhost
port 5432. The existing BeautyBans database remains on port 5433; its containers
were not restarted by this deployment.

- Application: `/opt/beauty-ac-web` (root-owned source and virtual environment).
- Producer-owned validator snapshot: `/opt/beauty-ac-producer-tools/ac_archive.py`.
  Its SHA-256 matches the source checkout's `tools/ac_archive.py`.
- Private credentials/config: `/etc/beauty-ac/worker.env` (root-owned, mode 0600).
- Local archives: `/var/lib/beauty-ac/archives` (private worker-owned state).
- Input: `/home/beauty_server/serverfiles/cstrike/addons/amxmodx/data/beauty_ac_queue`.

An ACL grants home traversal and read-only spool access, including inherited read
access for future journals. The worker cannot write producer journals. The unit
uses a 25% CPU quota, low CPU/I/O priority and a 384 MiB memory cap. These limits
bound background resource usage; no in-game A/B performance test was performed.

Before enabling continuous processing, one real 6,909,285-byte journal was run
through the full pipeline: 26,722 records, 407 bhop windows, 350 gstrafe windows,
23,467 commands and two counter summaries. Its segment archive was 1,731,224 bytes.
Record streams, field layouts, summaries, player identity and local/S3 SHA-256
were compared. There were no decisions in this capture; decision ingestion is
covered by automated tests. The capped run took about 19 seconds and peaked at
82 MiB. A second run created no duplicate rows or objects.

Continuous startup processed all four existing `.ready` journals: four complete
journals, three segments for one player, six summaries, three verified `stored`
S3 archives, zero failures. All raw sources were retained and rehashed. A service
restart preserved these counts and attempt counters. Newly created spool files
were checked for inherited read access without write permission. The active
`.open` journal was not processed. No public web service is deployed yet.

Operational checks (sudo on the VPS):

```sh
systemctl status beauty-ac-worker.service
journalctl -u beauty-ac-worker.service -n 50 --no-pager
sudo -u postgres psql beauty_ac -c 'SELECT state, count(*) FROM anticheat_journal GROUP BY state;'
sudo -u postgres psql beauty_ac -c 'SELECT upload_state, count(*) FROM anticheat_archive GROUP BY upload_state;'
```

`deploy/bootstrap_worker.py` provisions a **fresh** application/config directory
on Ubuntu, installs PostgreSQL/Python prerequisites, creates the dedicated role,
configures ACLs, applies Django migrations and installs the unit. It takes a
`git archive` release tar, the original producer validator and an external S3
credential file. It deliberately does not start the unit; verify a single closed
journal first, then enable it with `systemctl enable --now beauty-ac-worker.service`.
It is not an upgrade script. Back up the database and private environment before
future upgrades. Retention and automatic source cleanup remain unimplemented.

## Verification

Tests require a reachable PostgreSQL instance and a role allowed to create a
test database. Django creates/drops its isolated test database. The producer
checkout is required (the default test path is the sibling `beauty-server`).
Install the test extra for the Moto S3 emulator. Tests use fake credentials and
do not contact the live bucket.

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test anticheat --noinput
.\.venv\Scripts\python.exe -m unittest discover -s D:/projects/beauty-server/tools -p test_ac_archive.py -v
```

Coverage includes both movement kinds, segment isolation, float preservation,
decisions/summaries, idempotency, corruption, retry backoff, transactional rollback,
crash recovery, cross-session locking, late/pending identity, incomplete captures,
multi-server IDs, upload-state constraints and admin access. S3 tests cover
read-back verification, retry after successful PUT, corruption, conditional-write
races, version IDs, destination binding, cross-session locking and both commands.
