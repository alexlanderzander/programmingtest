#!/usr/bin/env python3
"""Generic launcher for a private job on a public, ephemeral runner.

Only this launcher and its workflow belong in the public repository. Job input,
child output and result archives use an authenticated private broker. No child
output is copied to the public job console, summaries, artifacts or cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
MAX_EXPANDED_SOURCE = 256 * 1024 ** 2
MAX_RESULT = 256 * 1024 ** 2
MAX_RESULT_INPUT = 512 * 1024 ** 2
CHUNK_SIZE = 4 * 1024 ** 2
MAX_PARTS = 64
MAX_FILES = 20_000
POST_RETRY_SECONDS = 90
HEX = re.compile(r"[a-f0-9]{64}")
ENTRYPOINT = "ci-entrypoint.py"


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


def private_environment(source: Path, output: Path, parent=None) -> dict:
    environment = dict(os.environ if parent is None else parent)
    for name in list(environment):
        if (name.startswith("ACTIONS_") or name in {
                "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT",
                "GITHUB_ENV", "GITHUB_PATH", "RUNNER_DEBUG", "PRIVATE_BROKER_URL",
                "PRIVATE_JOB_ID", "PRIVATE_SOURCE_SHA256"}):
            environment.pop(name, None)
    environment.update(RESULTS_DIR=str(output), GITHUB_WORKSPACE=str(source),
                       PRIVATE_SOURCE_DIR=str(source))
    return environment


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


def execute_private(source: Path, output: Path, timeout_seconds: int, *, parent_environment=None) -> int:
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (output / "entrypoint.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, str(source / ENTRYPOINT)], cwd=source,
            env=private_environment(source, output, parent_environment), stdout=log,
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
            code = execute_private(source, output, timeout_seconds)
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
    args = parser.parse_args(argv)
    try:
        def cancelled(_number, _frame):
            raise PrivateJobError("Job cancelled")
        signal.signal(signal.SIGTERM, cancelled)
        # Keep a ten-minute nominal margin in the sixty-minute workflow for
        # source pinning, packing and authenticated private-result transfer.
        if not 1 <= args.timeout_seconds <= 3000:
            raise PrivateJobError("Invalid job timeout")
        temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        code = run(Broker(args.broker, args.job), args.source_sha256, args.timeout_seconds, temporary)
    except BaseException:
        print("Private job could not complete. Inspect its private result channel.")
        return 1
    print("Private job " + ("passed" if code == 0 else "failed") + ". Reference: " + args.job)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
