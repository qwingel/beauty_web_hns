"""Load the producer-owned validator and split its validated output."""

from collections import OrderedDict
import gzip
import importlib.util
import json
import os
from pathlib import Path
import tempfile

import ijson

from .identifiers import stable_id


def load_validator(tools):
    path = Path(tools).resolve() / "ac_archive.py"
    spec = importlib.util.spec_from_file_location("beauty_ac_producer_archive", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_item(path, prefix):
    with gzip.open(path, "rb") as incoming:
        return next(ijson.items(incoming, prefix, use_float=True))


def records(path):
    with gzip.open(path, "rb") as incoming:
        yield from ijson.items(incoming, "records.item", use_float=True)


def split_archive(packed, destination, journal_id, validator):
    """Stream records through disk; never load a journal's command array in RAM."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    header = read_item(packed, "archive")
    fields = read_item(packed, "fields")
    report = read_item(packed, "report")
    results = []
    with tempfile.TemporaryDirectory(prefix="split-", dir=destination) as scratch:
        scratch = Path(scratch)
        streams = OrderedDict()
        try:
            for row in records(packed):
                segment = row["data"].get("s")
                if segment is None:
                    continue
                if segment not in streams:
                    if len(streams) >= 64:
                        _, oldest = streams.popitem(last=False)
                        oldest.close()
                    streams[segment] = (scratch / f"{segment}.ndjson").open("ab")
                streams.move_to_end(segment)
                streams[segment].write(validator.encode(row) + b"\n")
        finally:
            for outgoing in streams.values():
                outgoing.close()
        for number, state in report["segments"].items():
            segment_id = stable_id("segment", f"{journal_id}:{number}")
            archive_id = stable_id("archive", str(segment_id))
            target = destination / f"{archive_id}.json.gz"
            temporary = scratch / f"{archive_id}.part"
            with temporary.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as outgoing:
                    outgoing.write(b'{"schema":1,"journal_id":' + validator.encode(str(journal_id)))
                    outgoing.write(b',"segment_id":' + validator.encode(str(segment_id)))
                    outgoing.write(b',"archive":' + validator.encode(header))
                    outgoing.write(b',"fields":' + validator.encode(fields) + b',"records":[')
                    with (scratch / f"{number}.ndjson").open("rb") as incoming:
                        separator = b""
                        for row in incoming:
                            outgoing.write(separator + row.rstrip(b"\n"))
                            separator = b","
                    # Integrity remains journal-wide, including gaps affecting other segments.
                    integrity = {key: value for key, value in report.items() if key != "segments"}
                    outgoing.write(b'],"segment":' + validator.encode(state))
                    outgoing.write(b',"report":' + validator.encode(integrity) + b"}\n")
                raw.flush()
                os.fsync(raw.fileno())
            digest = validator.file_hash(temporary).hex()
            # Read the whole gzip stream to verify its trailer before publication.
            with gzip.open(temporary, "rb") as incoming:
                while incoming.read(1024 * 1024):
                    pass
            try:
                os.link(temporary, target)
            except FileExistsError:
                if validator.file_hash(target).hex() != digest:
                    raise ValueError(f"archive collision: {target}")
            results.append({"id": archive_id, "segment_id": segment_id, "number": int(number),
                            "path": target, "sha256": digest, "size": target.stat().st_size})
    # Persist directory entries on Linux before committing local_verified_at.
    if os.name != "nt":
        descriptor = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return header, report, results
