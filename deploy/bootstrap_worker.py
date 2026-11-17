"""Bootstrap the dedicated AC database and worker on an Ubuntu VPS as root.

Input paths contain a git archive, the producer-owned validator and an external
S3 environment file. This installs but does not start the worker; verify one
real journal before enabling the service. No existing database is repurposed.
"""

import argparse
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import tarfile

APP = Path("/opt/beauty-ac-web")
STATE = Path("/var/lib/beauty-ac")
CONFIG = Path("/etc/beauty-ac")
TOOLS = Path("/opt/beauty-ac-producer-tools")
SPOOL = Path("/home/beauty_server/serverfiles/cstrike/addons/amxmodx/data/beauty_ac_queue")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}:
            values[key] = value.strip().strip("\"'")
    if not all(values.get(key) for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")):
        raise ValueError("external S3 credential file is incomplete")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("validator", type=Path)
    parser.add_argument("credentials", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root")
    if APP.exists() or CONFIG.exists():
        parser.error("bootstrap requires a fresh application/config directory")
    credentials = read_env(args.credentials)
    if not SPOOL.is_dir():
        parser.error("expected producer spool does not exist")
    run("apt-get", "update", "-qq")
    environment = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    run("apt-get", "install", "-y", "--no-install-recommends", "postgresql", "python3-venv", "acl", env=environment)
    run("systemctl", "enable", "--now", "postgresql")
    try:
        pwd.getpwnam("beauty-ac")
    except KeyError:
        run("useradd", "--system", "--user-group", "--home-dir", str(STATE), "--shell", "/usr/sbin/nologin", "beauty-ac")
    account = pwd.getpwnam("beauty-ac")
    APP.mkdir(mode=0o755, parents=True)
    with tarfile.open(args.release) as archive:
        archive.extractall(APP, filter="data")
    TOOLS.mkdir(mode=0o755, parents=True)
    shutil.copyfile(args.validator, TOOLS / "ac_archive.py")
    os.chmod(TOOLS / "ac_archive.py", 0o644)
    STATE.mkdir(mode=0o700, parents=True)
    os.chown(STATE, account.pw_uid, account.pw_gid)
    CONFIG.mkdir(mode=0o700, parents=True)
    run("python3", "-m", "venv", str(APP / ".venv"))
    python = str(APP / ".venv/bin/python")
    run(python, "-m", "pip", "install", str(APP))

    # Cleanup may unlink finalized files; the worker's sandbox keeps its spool read-only.
    run("setfacl", "-m", "u:beauty-ac:--x", "/home/beauty_server")
    run("setfacl", "-m", "u:beauty-ac:rwx,d:u:beauty-ac:r-x", str(SPOOL))
    for path in SPOOL.iterdir():
        if path.is_file() and path.suffix in {".ready", ".open"}:
            run("setfacl", "-m", "u:beauty-ac:r--", str(path))

    password = secrets.token_urlsafe(36)
    # Generated password contains only URL-safe characters; it never appears in argv.
    sql = f"CREATE ROLE beauty_ac LOGIN PASSWORD '{password}';\nCREATE DATABASE beauty_ac OWNER beauty_ac;\n"
    run("runuser", "-u", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", input=sql, text=True)
    values = {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(64), "DJANGO_DEBUG": "0",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "DATABASE_URL": f"postgresql://beauty_ac:{password}@127.0.0.1:5432/beauty_ac?connect_timeout=5",
        "AC_SERVER_ID": "b972127d-7d91-4dcb-8542-723deef81336", "AC_SERVER_NAME": "Beauty HNS",
        "AC_PRODUCER_TOOLS": str(TOOLS), "AC_SPOOL": str(SPOOL), "AC_OUTPUT": str(STATE / "archives"),
        "AC_S3_BUCKET": "687cfbdb-532d-4bdc-8d35-8c096a8ddfda",
        "AC_S3_ENDPOINT_URL": "https://s3.twcstorage.ru", "AC_S3_REGION": "ru-1",
        "AC_S3_ADDRESSING_STYLE": "path", **credentials,
    }
    # Double-quoted values work for systemd EnvironmentFile and shell sourcing.
    def quote(value):
        if "\n" in value or "\r" in value:
            raise ValueError("environment values must be single-line")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'

    descriptor = os.open(CONFIG / "worker.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as outgoing:
        outgoing.write("".join(f"{key}={quote(value)}\n" for key, value in values.items()))
    worker_environment = dict(os.environ, **values, PYTHONDONTWRITEBYTECODE="1")
    run("runuser", "-u", "beauty-ac", "--", python, str(APP / "manage.py"), "check", env=worker_environment, cwd=APP)
    run("runuser", "-u", "beauty-ac", "--", python, str(APP / "manage.py"), "migrate", "--noinput", env=worker_environment, cwd=APP)
    shutil.copyfile(APP / "deploy/beauty-ac-worker.service", "/etc/systemd/system/beauty-ac-worker.service")
    for unit in ("beauty-ac-cleanup.service", "beauty-ac-cleanup.timer"):
        shutil.copyfile(APP / "deploy" / unit, Path("/etc/systemd/system") / unit)
    run("systemctl", "daemon-reload")
    print("Bootstrap complete. Verify one journal and cleanup dry-run, then enable the worker and cleanup timer.")


if __name__ == "__main__":
    main()
