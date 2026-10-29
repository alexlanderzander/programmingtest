#!/usr/bin/env python3
"""Generic launcher for a private job on a public, ephemeral runner.

Only this launcher and its workflow belong in the public repository. Job input,
child output and result archives use an authenticated private broker. No child
output is copied to the public job console, summaries, artifacts or cache.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


AUDIENCE = "private-native-ci"
USER_AGENT = "NativeCI/1.0"
MAX_SOURCE = 25 * 1024 ** 2
MAX_INPUT_ARTIFACT = 128 * 1024 ** 2
MAX_EXPANDED_SOURCE = 256 * 1024 ** 2
MAX_RESULT = 256 * 1024 ** 2
MAX_RESULT_INPUT = 512 * 1024 ** 2
CHUNK_SIZE = 4 * 1024 ** 2
MAX_PARTS = 64
MAX_FILES = 20_000
POST_RETRY_SECONDS = 90
HEX = re.compile(r"[a-f0-9]{64}")
ENTRYPOINT = "ci-entrypoint.py"
ARTIFACT_DESCRIPTOR = "private-artifact-input.json"
ARTIFACT_TRANSPORT = "private-artifact-transport.json"
ARTIFACT_IDENTITY = "private-artifact-identity.json"
MAX_TRANSPORT_DESCRIPTOR = 64 * 1024
MAX_ARTIFACT_IDENTITY = 8 * 1024 ** 2
MAX_EXPANDED_ARTIFACT = 320 * 1024 ** 2
MAX_ARTIFACT_FILES = 12_000
RETAINED_MEMBER = "native/native-audit-app.zip"
PUBLIC_CLAIMS = ("iss", "aud", "iat", "exp", "nbf", "repository_id", "repository", "ref",
                 "sub", "workflow_ref", "workflow_sha", "run_id", "run_attempt", "actor_id",
                 "event_name")
PUBLIC_JOSE_FIELDS = ("alg", "typ", "kid")
PUBLIC_WORKER_ERRORS = {
    "invalid_json", "body_too_large", "invalid_token", "unauthorized", "identity_unavailable",
    "unknown_signing_key", "run_not_allowed", "job_pending", "not_found",
    "invalid_job_configuration", "job_expired", "source_pending", "source_part_length_mismatch",
    "source_part_integrity_mismatch", "invalid_part", "missing_body", "job_already_completed",
    "part_already_stored", "part_length_mismatch", "part_upload_failed", "part_digest_mismatch",
    "invalid_manifest", "parts_not_ready", "already_completed", "results_pending",
    "method_not_allowed", "job_expiring", "request_failed", "internal_error",
}


class PrivateJobError(Exception):
    """Errors are intentionally not rendered on the public console."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def remaining_timeout(deadline, maximum):
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PrivateJobError("Private transfer deadline")
    return min(maximum, remaining)


def public_diagnostic_value(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, (int, float)) and math.isfinite(value) and abs(value) < 10 ** 16:
        return value
    if isinstance(value, list) and len(value) <= 8 and all(isinstance(item, str) for item in value):
        return [item[:512] for item in value]
    return "unsupported-value"


def public_token_fields(token: str, segment: int, fields: tuple[str, ...]) -> dict:
    parts = token.split(".")
    if len(parts) != 3 or len(parts[segment]) > 48 * 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[segment]):
        raise PrivateJobError("Invalid diagnostic identity")
    payload = base64.urlsafe_b64decode(parts[segment] + "=" * (-len(parts[segment]) % 4))
    if len(payload) > 32 * 1024:
        raise PrivateJobError("Diagnostic identity limit")
    values = json.loads(payload)
    if not isinstance(values, dict):
        raise PrivateJobError("Invalid diagnostic identity")
    return {name: {"type": type(values[name]).__name__, "value": public_diagnostic_value(values[name])}
            if name in values else {"type": "missing", "value": None} for name in fields}


def public_claims(token: str) -> dict:
    """Decode only diagnostic identity fields, never token/header/signature.

    This is not signature verification; the broker remains the auth verifier.
    JSON encoding prevents workflow-command or newline injection into logs.
    """
    return public_token_fields(token, 1, PUBLIC_CLAIMS)


def diagnostic_failure(stage: str, error: BaseException) -> dict:
    if isinstance(error, urllib.error.HTTPError):
        kind = "HTTPError"
    elif isinstance(error, urllib.error.URLError):
        kind = "URLError"
    elif isinstance(error, TimeoutError):
        kind = "TimeoutError"
    elif isinstance(error, (ValueError, json.JSONDecodeError, PrivateJobError)):
        kind = "InvalidContext"
    else:
        kind = "UnexpectedError"
    result = {"stage": stage, "type": kind}
    if isinstance(error, urllib.error.HTTPError) and 100 <= error.code <= 599:
        result["http_status"] = error.code
    return result


def public_token_endpoint(broker):
    try:
        endpoint = getattr(broker, "environment", {}).get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
        parsed = urllib.parse.urlsplit(endpoint)
        host = parsed.hostname or ""
        if parsed.scheme in {"https", "http"} and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
            return {"scheme": parsed.scheme, "host": host}
    except BaseException:
        pass
    return None


def token_failure(broker, error):
    value = diagnostic_failure("oidc-token", error)
    endpoint = public_token_endpoint(broker)
    if endpoint is not None:
        value["endpoint"] = endpoint
    print(json.dumps(value, sort_keys=True))


def public_worker_error(error):
    code = "unavailable"
    reported = (error.headers or {}).get("X-Private-CI-Error")
    if reported in PUBLIC_WORKER_ERRORS:
        return reported
    try:
        payload = error.read(2048)
        value = json.loads(payload).get("error")
        if value in PUBLIC_WORKER_ERRORS:
            code = value
    except BaseException:
        pass
    return code


def verify_access(broker, *, wait_seconds=180) -> int:
    """Public-safe auth preflight. Never fetch, execute or upload source."""
    try:
        deadline = time.monotonic() + wait_seconds
        token = broker.token(deadline=deadline if wait_seconds else None)
    except BaseException as error:
        token_failure(broker, error)
        return 1
    try:
        claims = public_claims(token)
        jose = public_token_fields(token, 0, PUBLIC_JOSE_FIELDS)
    except BaseException as error:
        print(json.dumps(diagnostic_failure("oidc-claims", error), sort_keys=True))
        return 1
    print(json.dumps({"stage": "oidc-claims", "claims": claims, "jose_header": jose}, sort_keys=True, ensure_ascii=True))
    while True:
        request = urllib.request.Request(broker.base + "/source", method="HEAD",
            headers={"Authorization": "Bearer " + token, "User-Agent": USER_AGENT})
        try:
            with broker.opener.open(request, timeout=remaining_timeout(deadline, 30) if wait_seconds else 30) as response:
                status = response.status
                # Never consume source bytes, including on a bad upstream.
            print(json.dumps({"stage": "source-head", "http_status": status}, sort_keys=True))
            return 0 if status == 200 else 1
        except urllib.error.HTTPError as error:
            result = diagnostic_failure("source-head", error)
            result["worker_error"] = public_worker_error(error)
            remaining = deadline - time.monotonic()
            if error.code not in {403, 409, 503} or remaining <= 0:
                print(json.dumps(result, sort_keys=True))
                return 1
        except BaseException as error:
            print(json.dumps(diagnostic_failure("source-head", error), sort_keys=True))
            return 1
        time.sleep(min(3, remaining))
        if time.monotonic() >= deadline:
            print(json.dumps(result, sort_keys=True))
            return 1
        try:
            token = broker.token(deadline=deadline)
        except BaseException as error:
            token_failure(broker, error)
            return 1


def verify_source(broker, expected_sha256: str, temporary_root: Path) -> int:
    """Read authenticated source privately to verify the transfer, never unpack it."""
    count = 0
    try:
        with tempfile.TemporaryDirectory(prefix="private-source-verification-", dir=temporary_root) as name:
            directory = Path(name)
            directory.chmod(0o700)
            path = directory / "source.zip"
            path.touch(mode=0o600, exist_ok=False)
            try:
                broker.download(path, expected_sha256)
                count = path.stat().st_size
            except BaseException:
                count = path.stat().st_size if path.is_file() else 0
                raise
    except BaseException as error:
        # Only a fixed diagnostic enum is public. Never render exception text,
        # filenames, archive contents, response bodies, headers or token values.
        known = {"Invalid source size": "invalid_source_size",
                 "Source identity mismatch": "source_identity_mismatch",
                 "Source integrity failure": "source_integrity_mismatch",
                 "Source size limit": "source_size_limit",
                 "Private source unavailable": "source_unavailable"}
        code = known.get(str(error), "source_verification_failed") if isinstance(error, PrivateJobError) else "source_verification_failed"
        print(json.dumps({"stage": "source-integrity", "source_hash_matches": False,
                          "byte_count": count, "error": code}, sort_keys=True))
        return 1
    print(json.dumps({"stage": "source-integrity", "source_hash_matches": True,
                      "byte_count": count}, sort_keys=True))
    return 0


def private_environment(source: Path, output: Path, parent=None, *, input_artifact=None) -> dict:
    environment = dict(os.environ if parent is None else parent)
    for name in list(environment):
        if (name.startswith("ACTIONS_") or name in {
                "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT",
                "GITHUB_ENV", "GITHUB_PATH", "RUNNER_DEBUG", "PRIVATE_BROKER_URL",
                "PRIVATE_JOB_ID", "PRIVATE_SOURCE_SHA256", "PRIVATE_INPUT_ARTIFACT"}):
            environment.pop(name, None)
    environment.update(RESULTS_DIR=str(output), GITHUB_WORKSPACE=str(source),
                       PRIVATE_SOURCE_DIR=str(source))
    if input_artifact is not None:
        environment["PRIVATE_INPUT_ARTIFACT"] = str(input_artifact)
    return environment


def artifact_descriptor(value) -> dict:
    if (not isinstance(value, dict) or set(value) != {"schema_version", "size", "sha256"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["size"]) is not int or not 0 < value["size"] <= MAX_INPUT_ARTIFACT
            or not isinstance(value["sha256"], str) or not HEX.fullmatch(value["sha256"])):
        raise PrivateJobError("Invalid private artifact descriptor")
    return dict(value)


def read_artifact_descriptor(source: Path):
    path = source / ARTIFACT_DESCRIPTOR
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 4096:
        raise PrivateJobError("Invalid private artifact descriptor file")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise PrivateJobError("Duplicate private artifact descriptor field")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as error:
        raise PrivateJobError("Malformed private artifact descriptor") from error
    return artifact_descriptor(value)


def _unique_private_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise PrivateJobError("Duplicate private transport field")
            value[key] = item
        return value

    def constant(_value):
        raise PrivateJobError("Nonfinite private transport field")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as error:
        raise PrivateJobError("Malformed private transport metadata") from error


def _sized_hash(value, maximum):
    return (isinstance(value, dict) and type(value.get("size")) is int
            and 0 < value["size"] <= maximum and isinstance(value.get("sha256"), str)
            and HEX.fullmatch(value["sha256"]) is not None)


def retained_artifact_transport(value):
    """Validate a frozen, explicit retained-result envelope without a URL."""
    if (not isinstance(value, dict) or set(value) != {
            "schema_version", "backend", "producer_job_id", "outer", "nested", "identity"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["backend"] != "r2-retained-result" or not isinstance(value["producer_job_id"], str)):
        raise PrivateJobError("Invalid retained artifact transport")
    try:
        if str(uuid.UUID(value["producer_job_id"])) != value["producer_job_id"]:
            raise ValueError()
    except ValueError:
        raise PrivateJobError("Invalid retained artifact producer") from None
    outer, nested, identity = value["outer"], value["nested"], value["identity"]
    if (not _sized_hash(outer, MAX_RESULT) or set(outer) != {"size", "sha256", "parts"}
            or not _sized_hash(nested, MAX_INPUT_ARTIFACT) or set(nested) != {"member", "size", "sha256"}
            or nested["member"] != RETAINED_MEMBER or not _sized_hash(identity, MAX_ARTIFACT_IDENTITY)
            or set(identity) != {"size", "sha256"} or not isinstance(outer["parts"], list)
            or not 1 <= len(outer["parts"]) <= MAX_PARTS):
        raise PrivateJobError("Invalid retained artifact bounds or member")
    total = 0
    for index, part in enumerate(outer["parts"]):
        if (not _sized_hash(part, 4 * 1024 ** 2)
                or set(part) != {"index", "size", "sha256", "object_key"}
                or type(part["index"]) is not int or part["index"] != index
                or part["object_key"] != f'native-ci-results/v1/jobs/{value["producer_job_id"]}/result/{index}/{part["sha256"]}'):
            raise PrivateJobError("Invalid retained artifact part")
        total += part["size"]
    if total != outer["size"]:
        raise PrivateJobError("Retained artifact part lengths differ")
    return value


def _bounded_source_file(source, name, maximum):
    path = source / name
    if (path.is_symlink() or not path.is_file() or path.resolve().parent != source.resolve()
            or not stat.S_ISREG(path.stat().st_mode) or not 0 < path.stat().st_size <= maximum):
        raise PrivateJobError("Missing or unbounded pinned transport metadata")
    return path


def read_artifact_transport(source: Path):
    path = source / ARTIFACT_TRANSPORT
    if not path.exists() and not path.is_symlink():
        return None
    path = _bounded_source_file(source, ARTIFACT_TRANSPORT, MAX_TRANSPORT_DESCRIPTOR)
    value = retained_artifact_transport(_unique_private_json(path.read_bytes()))
    inner = read_artifact_descriptor(source)
    if inner != {"schema_version": 1, "size": value["nested"]["size"], "sha256": value["nested"]["sha256"]}:
        raise PrivateJobError("Pinned inner descriptor differs from retained transport")
    identity = _bounded_source_file(source, ARTIFACT_IDENTITY, MAX_ARTIFACT_IDENTITY)
    raw = identity.read_bytes()
    if len(raw) != value["identity"]["size"] or hashlib.sha256(raw).hexdigest() != value["identity"]["sha256"]:
        raise PrivateJobError("Pinned original identity differs from retained transport")
    _artifact_inventory(_unique_private_json(raw), value)
    return value


def _artifact_relative(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or any(ord(char) < 32 for char in name)):
        raise PrivateJobError("Unsafe private artifact member")
    path = PurePosixPath(name)
    if (path.is_absolute() or str(path) != name or any(part in {".", ".."} for part in path.parts)
            or any(part in {".git", ".build", "DerivedData", "SourcePackages"} for part in path.parts)
            or any(part.endswith((".app", ".appex")) for part in path.parts)
            or path.suffix.lower() in {".p8", ".p12", ".pfx", ".pem", ".key", ".mobileprovision",
                                       ".swift", ".m", ".mm", ".c", ".cc", ".cpp", ".h", ".hpp", ".xcconfig"}
            or path.name in {"DevSecrets.swift", ".env"}):
        raise PrivateJobError("Unsupported private artifact member")
    return path


def _artifact_inventory(identity, transport):
    if (not isinstance(identity, dict) or type(identity.get("schema_version")) is not int
            or identity["schema_version"] != 1 or identity.get("private") is not True
            or identity.get("app_directory") != "app" or identity.get("archive_sha256") != transport["nested"]["sha256"]
            or type(identity.get("archive_bytes")) is not int or identity["archive_bytes"] != transport["nested"]["size"]
            or not isinstance(identity.get("files"), dict) or not 1 <= len(identity["files"]) <= MAX_ARTIFACT_FILES):
        raise PrivateJobError("Pinned private artifact identity differs")
    total, folded = 0, set()
    for name, record in identity["files"].items():
        _artifact_relative(name)
        normal = unicodedata.normalize("NFC", name).casefold()
        if (normal in folded or not isinstance(record, dict) or set(record) != {"bytes", "sha256", "mode"}
                or type(record["bytes"]) is not int or record["bytes"] < 0
                or type(record["mode"]) is not int or record["mode"] not in {0o644, 0o755}
                or not isinstance(record["sha256"], str) or not HEX.fullmatch(record["sha256"])):
            raise PrivateJobError("Invalid private artifact inventory")
        folded.add(normal)
        total += record["bytes"]
    if total > MAX_EXPANDED_ARTIFACT:
        raise PrivateJobError("Private artifact inventory expansion limit")
    if any('/'.join(name.split('/')[:index]) in folded
           for name in folded for index in range(1, len(name.split('/')))):
        raise PrivateJobError("Private artifact file/directory collision")
    return identity["files"]


def extract_retained_artifact(outer_path, output, transport, identity_path):
    """Extract one pinned opaque ZIP, verify every app member, never expand app."""
    transport = retained_artifact_transport(transport)
    if output.exists() or output.is_symlink() or output.parent.is_symlink():
        raise PrivateJobError("Private artifact output already exists")
    for path, maximum in ((outer_path, MAX_RESULT), (identity_path, MAX_ARTIFACT_IDENTITY)):
        if (path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode)
                or not 0 < path.stat().st_size <= maximum):
            raise PrivateJobError("Retained artifact input is not a bounded regular file")
    outer_digest = hashlib.sha256()
    with outer_path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            outer_digest.update(data)
    if outer_path.stat().st_size != transport["outer"]["size"] or outer_digest.hexdigest() != transport["outer"]["sha256"]:
        raise PrivateJobError("Retained outer archive integrity differs")
    raw_identity = identity_path.read_bytes()
    if len(raw_identity) != transport["identity"]["size"] or hashlib.sha256(raw_identity).hexdigest() != transport["identity"]["sha256"]:
        raise PrivateJobError("Pinned original identity integrity differs")
    inventory = _artifact_inventory(_unique_private_json(raw_identity), transport)
    created = False
    try:
        with zipfile.ZipFile(outer_path) as outer:
            entries, seen = outer.infolist(), {}
            if not 1 <= len(entries) <= MAX_FILES or sum(item.file_size for item in entries) > MAX_RESULT_INPUT:
                raise PrivateJobError("Retained outer archive expansion limit")
            for entry in entries:
                if ":" in entry.filename or any(ord(char) < 32 for char in entry.filename):
                    raise PrivateJobError("Unsafe retained outer archive path")
                name = source_name(entry.filename)
                normal = unicodedata.normalize("NFC", name).casefold()
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (normal in seen or entry.is_dir() or entry.flag_bits & 1
                        or kind not in {0, stat.S_IFREG} or entry.file_size < 0):
                    raise PrivateJobError("Unsafe retained outer archive member")
                seen[normal] = entry
            if any('/'.join(name.split('/')[:index]) in seen
                   for name in seen for index in range(1, len(name.split('/')))):
                raise PrivateJobError("Retained outer file/directory collision")
            member = outer.getinfo(RETAINED_MEMBER)
            if member.file_size != transport["nested"]["size"] or outer.testzip() is not None:
                raise PrivateJobError("Retained outer CRC or nested length differs")
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as target, outer.open(member) as source:
                for data in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(data)
                    if size > transport["nested"]["size"]:
                        raise PrivateJobError("Nested private artifact size limit")
                    target.write(data)
                    digest.update(data)
            if size != transport["nested"]["size"] or digest.hexdigest() != transport["nested"]["sha256"]:
                raise PrivateJobError("Nested private artifact integrity differs")
        with zipfile.ZipFile(output) as inner:
            entries = inner.infolist()
            if (len(entries) != len(inventory) or len({item.filename for item in entries}) != len(entries)
                    or {item.filename for item in entries} != {"app/" + name for name in inventory}):
                raise PrivateJobError("Private artifact member inventory differs")
            for entry in entries:
                relative = entry.filename[4:]
                _artifact_relative(relative)
                record, mode = inventory[relative], entry.external_attr >> 16
                if (entry.is_dir() or entry.flag_bits & 1 or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                        or (mode & 0o777) != record["mode"] or entry.file_size != record["bytes"]):
                    raise PrivateJobError("Private artifact member type or mode differs")
                digest, size = hashlib.sha256(), 0
                with inner.open(entry) as source:
                    for data in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(data)
                        if size > record["bytes"]:
                            raise PrivateJobError("Private artifact member exceeds pinned bytes")
                        digest.update(data)
                if size != record["bytes"] or digest.hexdigest() != record["sha256"]:
                    raise PrivateJobError("Private artifact member integrity differs")
    except BaseException as error:
        if created:
            output.unlink(missing_ok=True)
        if isinstance(error, (PrivateJobError, KeyboardInterrupt, SystemExit)):
            raise
        raise PrivateJobError("Retained private artifact archive is invalid") from error


def source_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise PrivateJobError("Invalid source path")
    raw_parts = name.rstrip("/").split("/")
    path = PurePosixPath(name)
    if (path.is_absolute() or any(part in {"", ".", ".."} for part in raw_parts)
            or ":" in raw_parts[0]):
        raise PrivateJobError("Unsafe source path")
    return "/".join(raw_parts)


def extract_source(archive: Path, destination: Path) -> None:
    """Check the complete central directory before writing source files."""
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if not entries or len(entries) > MAX_FILES:
            raise PrivateJobError("Invalid source file count")
        checked = []
        names = {}
        total = 0
        for entry in entries:
            relative = source_name(entry.filename)
            folded = unicodedata.normalize("NFC", relative).casefold()
            if folded in names:
                raise PrivateJobError("Duplicate source path")
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            directory = entry.is_dir()
            if (entry.flag_bits & 1 or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or (directory and kind == stat.S_IFREG)
                    or (not directory and kind == stat.S_IFDIR)):
                raise PrivateJobError("Unsupported source entry")
            total += entry.file_size
            if entry.file_size < 0 or total > MAX_EXPANDED_SOURCE:
                raise PrivateJobError("Source expansion limit")
            names[folded] = directory
            checked.append((entry, relative, mode, directory))
        for folded in names:
            parts = folded.split("/")
            if any(names.get("/".join(parts[:index])) is False
                   for index in range(1, len(parts))):
                raise PrivateJobError("Source file/directory collision")
        entrypoint = next((item for item in checked if item[1] == ENTRYPOINT), None)
        if entrypoint is None or entrypoint[3]:
            raise PrivateJobError("Missing private entrypoint")
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for entry, relative, mode, directory in checked:
            target = destination.joinpath(*PurePosixPath(relative).parts)
            if directory:
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with source.open(entry) as input_file, target.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, 1024 * 1024)
            if target.stat().st_size != entry.file_size:
                raise PrivateJobError("Source length mismatch")
            target.chmod(0o700 if mode & 0o111 else 0o600)


class Broker:
    def __init__(self, origin: str, job: str, environment=None, opener=None):
        parsed = urllib.parse.urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise PrivateJobError("Invalid broker origin")
        try:
            if str(uuid.UUID(job)) != job:
                raise ValueError()
        except ValueError:
            raise PrivateJobError("Invalid job identifier") from None
        self.base = origin.rstrip("/") + "/jobs/" + job
        self.environment = os.environ if environment is None else environment
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def token(self, *, deadline=None) -> str:
        endpoint = self.environment.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
        credential = self.environment.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.password or parsed.fragment or not credential):
            raise PrivateJobError("OIDC context unavailable")
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query)
                 if key != "audience"]
        query.append(("audience", AUDIENCE))
        url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + credential,
                                                       "User-Agent": USER_AGENT})
        with self.opener.open(request, timeout=remaining_timeout(deadline, 30)) as response:
            payload = response.read(64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            raise PrivateJobError("Invalid OIDC response")
        value = json.loads(payload).get("value")
        if not isinstance(value, str) or not 100 <= len(value) <= 32 * 1024:
            raise PrivateJobError("Invalid OIDC token")
        return value

    def request(self, suffix: str, *, method="GET", data=None, headers=None, deadline=None):
        fields = dict(headers or {})
        if any(not isinstance(name, str) or name.lower() in {
                "authorization", "user-agent", "content-length", "host"} for name in fields):
            raise PrivateJobError("Reserved transfer header")
        fields.update(Authorization="Bearer " + self.token(deadline=deadline), **{"User-Agent": USER_AGENT})
        if data is not None:
            fields["Content-Length"] = str(len(data))
        request = urllib.request.Request(self.base + suffix, data=data, headers=fields, method=method)
        return self.opener.open(request, timeout=remaining_timeout(deadline, 60))

    def download(self, output: Path, expected_sha256: str, *, wait_seconds=180):
        if not HEX.fullmatch(expected_sha256):
            raise PrivateJobError("Invalid source fingerprint")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                with self.request("/source") as response, output.open("wb") as target:
                    announced = response.headers.get("Content-Length")
                    if announced is None or not 0 < int(announced) <= MAX_SOURCE:
                        raise PrivateJobError("Invalid source size")
                    if response.headers.get("X-Content-Sha256") != expected_sha256:
                        raise PrivateJobError("Source identity mismatch")
                    size, digest = 0, hashlib.sha256()
                    while block := response.read(1024 * 1024):
                        size += len(block)
                        if size > MAX_SOURCE:
                            raise PrivateJobError("Source size limit")
                        target.write(block)
                        digest.update(block)
                    if size != int(announced) or digest.hexdigest() != expected_sha256:
                        raise PrivateJobError("Source integrity failure")
                output.chmod(0o600)
                return
            except urllib.error.HTTPError as error:
                # A newly pinned job/config may take time to reach the edge.
                if error.code not in {403, 404, 409, 429, 500, 502, 503, 504} or time.monotonic() >= deadline:
                    raise PrivateJobError("Private source unavailable") from None
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise PrivateJobError("Private source unavailable") from None
            time.sleep(min(3, max(0, deadline - time.monotonic())))

    def download_artifact(self, output: Path, descriptor: dict, *, wait_seconds=90):
        """Transfer opaque private bytes; only the private native job may unpack them."""
        expected = artifact_descriptor(descriptor)
        if output.exists() or output.is_symlink():
            raise PrivateJobError("Private artifact output already exists")
        deadline = time.monotonic() + wait_seconds
        while True:
            created = False
            try:
                with self.request("/artifact", deadline=deadline if wait_seconds else None) as response:
                    announced = response.headers.get("Content-Length", "")
                    if (response.status != 200 or not re.fullmatch(r"[1-9][0-9]{0,8}", announced)
                            or int(announced) != expected["size"]):
                        raise PrivateJobError("Private artifact size mismatch")
                    if response.headers.get("X-Content-Sha256") != expected["sha256"]:
                        raise PrivateJobError("Private artifact identity mismatch")
                    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                    size, digest = 0, hashlib.sha256()
                    with os.fdopen(fd, "wb") as target:
                        while block := response.read(min(1024 * 1024, expected["size"] - size + 1)):
                            size += len(block)
                            if size > expected["size"]:
                                raise PrivateJobError("Private artifact size limit")
                            target.write(block)
                            digest.update(block)
                        if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                            raise PrivateJobError("Private artifact integrity failure")
                return
            except BaseException as error:
                if created:
                    output.unlink(missing_ok=True)
                retry = isinstance(error, urllib.error.URLError)
                if isinstance(error, urllib.error.HTTPError):
                    retry = error.code in {404, 409, 429, 500, 502, 503, 504}
                if not retry or time.monotonic() >= deadline:
                    raise PrivateJobError("Private artifact unavailable or invalid") from None
            time.sleep(min(3, max(0, deadline - time.monotonic())))

    def download_retained_artifact(self, output: Path, descriptor: dict, *, wait_seconds=90):
        """Fetch only this consumer's authorized, pinned retained outer ZIP."""
        expected = retained_artifact_transport(descriptor)["outer"]
        if output.exists() or output.is_symlink():
            raise PrivateJobError("Private retained input already exists")
        deadline = time.monotonic() + wait_seconds
        while True:
            created = False
            try:
                with self.request("/artifact", deadline=deadline if wait_seconds else None) as response:
                    announced = response.headers.get("Content-Length", "")
                    if (response.status != 200 or not re.fullmatch(r"[1-9][0-9]{0,8}", announced)
                            or int(announced) != expected["size"]
                            or response.headers.get("X-Content-Sha256") != expected["sha256"]):
                        raise PrivateJobError("Retained artifact response identity differs")
                    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                    whole = hashlib.sha256()
                    with os.fdopen(fd, "wb") as target:
                        for part in expected["parts"]:
                            remaining, digest = part["size"], hashlib.sha256()
                            while remaining:
                                data = response.read(min(65536, remaining))
                                if not data or len(data) > remaining:
                                    raise PrivateJobError("Retained artifact part length differs")
                                remaining -= len(data)
                                target.write(data)
                                digest.update(data)
                                whole.update(data)
                            if digest.hexdigest() != part["sha256"]:
                                raise PrivateJobError("Retained artifact part integrity differs")
                        if response.read(1) or whole.hexdigest() != expected["sha256"]:
                            raise PrivateJobError("Retained artifact archive integrity differs")
                return
            except BaseException as error:
                if created:
                    output.unlink(missing_ok=True)
                retry = isinstance(error, urllib.error.URLError)
                if isinstance(error, urllib.error.HTTPError):
                    retry = error.code in {404, 409, 429, 500, 502, 503, 504}
                if not retry or time.monotonic() >= deadline:
                    raise PrivateJobError("Private retained artifact unavailable or invalid") from None
            time.sleep(min(3, max(0, deadline - time.monotonic())))

    def upload(self, archive: Path, exit_code: int, status: str) -> None:
        if not 0 < archive.stat().st_size <= MAX_RESULT:
            raise PrivateJobError("Result archive size limit")
        archive_hash = hashlib.sha256()
        parts = []
        with archive.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                index = len(parts)
                if index >= MAX_PARTS:
                    raise PrivateJobError("Result chunk limit")
                digest = hashlib.sha256(chunk).hexdigest()
                self._post("/result/" + str(index), chunk,
                           {"Content-Type": "application/octet-stream", "X-Chunk-Sha256": digest},
                           success_statuses=(200, 201))
                archive_hash.update(chunk)
                parts.append({"index": index, "size": len(chunk), "sha256": digest})
        manifest = {"status": status, "exit_code": exit_code, "parts": parts,
                    "total_bytes": archive.stat().st_size, "archive_sha256": archive_hash.hexdigest()}
        self._post("/complete", json.dumps(manifest, separators=(",", ":")).encode(),
                   {"Content-Type": "application/json"})

    def _post(self, suffix, payload, headers, *, success_statuses=(200,)):
        # Result parts may reach another edge after its negative KV cache has
        # been filled. Keep the identical write idempotent while allowing that
        # documented propagation window; never retry authentication failures.
        deadline = time.monotonic() + POST_RETRY_SECONDS
        delay = 1
        while True:
            try:
                with self.request(suffix, method="POST", data=payload, headers=headers,
                                  deadline=deadline) as response:
                    if response.status not in success_statuses:
                        raise PrivateJobError("Private result transfer failed")
                    response.read(64 * 1024)
                return
            except urllib.error.HTTPError as error:
                if error.code not in {409, 429, 500, 502, 503, 504}:
                    raise PrivateJobError("Private result transfer failed") from None
            except urllib.error.URLError:
                pass
            remaining = remaining_timeout(deadline, delay)
            time.sleep(remaining)
            delay = min(5, delay * 2)


def execute_private(source: Path, output: Path, timeout_seconds: int, *, parent_environment=None,
                    input_artifact=None) -> int:
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (output / "entrypoint.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, str(source / ENTRYPOINT)], cwd=source,
            env=private_environment(source, output, parent_environment, input_artifact=input_artifact), stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            terminate_private(process)
            code = 124
        except BaseException:
            terminate_private(process)
            raise
    return max(0, min(255, code if code >= 0 else 128 - code))


def terminate_private(process) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def result_archive(output: Path, destination: Path) -> None:
    """Only explicit private result files; never follow links to runner data."""
    files, total = [], 0
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise PrivateJobError("Unsupported result link")
        if path.is_dir():
            continue
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise PrivateJobError("Unsupported result file")
        total += path.stat().st_size
        if total > MAX_RESULT_INPUT or len(files) >= MAX_FILES:
            raise PrivateJobError("Private result limit")
        files.append(path)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=4, allowZip64=True) as archive:
        for path in files:
            archive.write(path, path.relative_to(output).as_posix())
            if destination.stat().st_size > MAX_RESULT:
                raise PrivateJobError("Private result archive limit")
    destination.chmod(0o600)
    if not 0 < destination.stat().st_size <= MAX_RESULT:
        raise PrivateJobError("Private result archive limit")


def run(broker: Broker, expected_sha256: str, timeout_seconds: int, temporary_root: Path) -> int:
    code, status = 1, "error"
    with tempfile.TemporaryDirectory(prefix="private-native-job-", dir=temporary_root) as name:
        workspace = Path(name)
        workspace.chmod(0o700)
        output = workspace / "results"
        output.mkdir(mode=0o700)
        try:
            archive = workspace / "source.zip"
            broker.download(archive, expected_sha256)
            source = workspace / "source"
            extract_source(archive, source)
            transport = read_artifact_transport(source)
            descriptor = read_artifact_descriptor(source)
            artifact = None
            if transport is not None:
                outer = workspace / "private-retained-input.zip"
                artifact = workspace / "private-input.zip"
                broker.download_retained_artifact(outer, transport)
                extract_retained_artifact(outer, artifact, transport, source / ARTIFACT_IDENTITY)
            elif descriptor is not None:
                artifact = workspace / "private-input.zip"
                broker.download_artifact(artifact, descriptor)
            code = execute_private(source, output, timeout_seconds, input_artifact=artifact)
            status = "pass" if code == 0 else "fail"
        except BaseException:
            # The private log is the only place an error may contain job data.
            import traceback
            (output / "launcher-error.log").write_text(traceback.format_exc())
            code, status = 1, "error"
        archive = workspace / "results.zip"
        result_archive(output, archive)
        broker.upload(archive, code, status)
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run an authenticated private job without public job output.")
    parser.add_argument("--broker", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3000)
    parser.add_argument("--verify-access", action="store_true",
                        help="Print allowlisted public OIDC identity fields and HEAD status; no private job execution.")
    parser.add_argument("--verify-source", action="store_true",
                        help="After successful access preflight, privately verify source bytes without unpacking or executing.")
    args = parser.parse_args(argv)
    try:
        def cancelled(_number, _frame):
            raise PrivateJobError("Job cancelled")
        signal.signal(signal.SIGTERM, cancelled)
        # Keep a ten-minute nominal margin in the sixty-minute workflow for
        # source pinning, packing and authenticated private-result transfer.
        if not 1 <= args.timeout_seconds <= 3000:
            raise PrivateJobError("Invalid job timeout")
        if args.verify_source and not args.verify_access:
            print(json.dumps({"stage": "preflight-context", "type": "InvalidContext"}))
            return 1
        transfer = Broker(args.broker, args.job)
        if args.verify_access:
            code = verify_access(transfer)
            if code or not args.verify_source:
                return code
            temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
            return verify_source(transfer, args.source_sha256, temporary)
        temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        code = run(transfer, args.source_sha256, args.timeout_seconds, temporary)
    except BaseException:
        if args.verify_access:
            # Arguments contain only public launcher configuration, but avoid
            # rendering exception messages or URLs even for invalid context.
            print(json.dumps({"stage": "preflight-context", "type": "InvalidContext"}))
            return 1
        print("Private job could not complete. Inspect its private result channel.")
        return 1
    print("Private job " + ("passed" if code == 0 else "failed") + ". Reference: " + args.job)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
