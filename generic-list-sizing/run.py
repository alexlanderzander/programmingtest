"""Small public, generic SwiftUI sizing experiment; no private application inputs."""
import hashlib
import itertools
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "results"
OUT.mkdir(exist_ok=False)
ENV = dict(os.environ, DEVELOPER_DIR="/Applications/Xcode_27.1.app/Contents/Developer")
BUNDLE = "org.example.generic-list-sizing"
GLOBAL_DEADLINE = time.monotonic() + 720
VARIANTS = ("outerFit", "candidateFit", "combinedFit", "horizontal", "adaptive", "labeled")
PHASE_TIMINGS = []
REQUIRED = {"window", "detail", "reference", "card", "card_content", "marker", "summary", "title", "hint", "progress"}


def run(arguments, log_name, timeout, check=True):
    remaining = GLOBAL_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Global 12-minute measurement deadline exhausted")
    timeout = min(timeout, remaining)
    began = time.monotonic()
    observation = {"phase": log_name, "actual_timeout_seconds": timeout,
                   "argv": list(map(str, arguments)), "exit_code": None, "timed_out": False}
    try:
        with (OUT / log_name).open("wb") as log:
            completed = subprocess.run(list(map(str, arguments)), cwd=ROOT, env=ENV,
                                       stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        observation["exit_code"] = completed.returncode
        if check and completed.returncode:
            raise RuntimeError(f"{log_name}: exit {completed.returncode}")
        return completed
    except subprocess.TimeoutExpired:
        observation["timed_out"] = True
        raise
    finally:
        observation["elapsed_seconds"] = time.monotonic() - began
        PHASE_TIMINGS.append(observation)
        (OUT / "phase-timings.json").write_text(json.dumps(PHASE_TIMINGS, indent=2))


def read_stable_geometry(path, token):
    deadline = min(time.monotonic() + 20, GLOBAL_DEADLINE)
    previous = None
    matches = 0
    last_error = None
    while time.monotonic() < deadline:
        try:
            payload = path.read_bytes()
            value = json.loads(payload)
            if value["token"] != token or not REQUIRED.issubset(value["frames"]):
                raise ValueError("Missing current geometry")
            for key in REQUIRED:
                rectangles = value["frames"][key]
                if not rectangles or not any(len(rect) == 4 and rect[2] > 0 and rect[3] > 0 for rect in rectangles):
                    raise ValueError(f"No positive geometry: {key}")
                if key in {"window", "detail", "card", "card_content", "reference"} and len(rectangles) != 1:
                    raise ValueError(f"Ambiguous outer geometry: {key}")
            if payload == previous:
                matches += 1
            else:
                matches = 0
                previous = payload
            if matches >= 3:
                return value
        except (OSError, ValueError, KeyError, TypeError) as error:
            last_error = str(error)
        time.sleep(0.2)
    raise TimeoutError(f"No stable real geometry within the bounded interval: {last_error}")


def png_dimensions(path):
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("Expected original simulator PNG")
    return list(struct.unpack(">II", header[16:24]))


sim = None
failed = []
observations = []
started = time.monotonic()
try:
    listing = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "-j"], env=ENV, timeout=min(30, max(1, GLOBAL_DEADLINE-time.monotonic()))))
    device = next(item for item in listing["devicetypes"] if item["name"] == "iPad Pro 13-inch (M4)")
    runtimes = [item for item in listing["runtimes"] if item.get("isAvailable")
                and ".iOS-" in item["identifier"] and item["version"] == "27.0"
                and device["identifier"] in [kind.get("identifier") if isinstance(kind, dict) else kind
                                             for kind in item.get("supportedDeviceTypes", [])]]
    if not runtimes:
        raise RuntimeError("The same iPad/iOS 27.0 profile must be available; no silent runtime substitution")
    runtime = runtimes[0]
    sim = subprocess.check_output(["xcrun", "simctl", "create", "GenericListSizing-" + uuid.uuid4().hex,
                                   device["identifier"], runtime["identifier"]], env=ENV, text=True, timeout=min(30, max(1, GLOBAL_DEADLINE-time.monotonic()))).strip()
    (OUT / "runtime.json").write_text(json.dumps({"device": device, "runtime": runtime, "uuid": sim}, indent=2))
    if not shutil.which("xcodegen"):
        run(["brew", "install", "xcodegen"], "install-xcodegen.log", 180)
    run(["xcodegen", "generate"], "generate.log", 30)
    run(["xcodebuild", "build", "-project", "GenericListSizing.xcodeproj", "-scheme", "GenericListSizing",
         "-configuration", "Debug", "-destination", "id=" + sim,
         "-derivedDataPath", ROOT / "DerivedData", "-jobs", "1", "CODE_SIGNING_ALLOWED=NO"], "build.log", 180)
    run(["xcrun", "simctl", "boot", sim], "boot.log", 120)
    run(["xcrun", "simctl", "bootstatus", sim, "-b"], "bootstatus.log", 600)
    app = ROOT / "DerivedData/Build/Products/Debug-iphonesimulator/GenericListSizing.app"
    run(["xcrun", "simctl", "install", sim, app], "install.log", 180)
    container = pathlib.Path(subprocess.check_output(
        ["xcrun", "simctl", "get_app_container", sim, BUNDLE, "data"], env=ENV, text=True, timeout=min(30, max(1, GLOBAL_DEADLINE-time.monotonic()))).strip())
    geometry_file = container / "Documents/measurement.json"
    # The six cases closest to the observed failure come first so a bounded
    # partial run remains informative without claiming the full 30-case matrix.
    cases = [dict(variant=variant, decorated=True, symbol_hint=True, accessibility_size=False)
             for variant in VARIANTS]
    cases += [dict(variant=variant, decorated=decorated, symbol_hint=symbol_hint, accessibility_size=False)
              for variant, decorated, symbol_hint in itertools.product(VARIANTS, (False, True), (False, True))
              if not (decorated and symbol_hint)]
    # AX5 comparison is useful after normal-size isolation. It is a separate case,
    # and cannot turn the measured normal-size finding into a general pass.
    cases += [dict(variant=variant, decorated=True, symbol_hint=True, accessibility_size=True)
              for variant in VARIANTS]
    (OUT / "case-plan.json").write_text(json.dumps(cases, indent=2))
    for index, case in enumerate(cases):
        if GLOBAL_DEADLINE - time.monotonic() < 40:
            failed.append({"phase": "case-budget", "next_case": index, "error": "Less than 40 seconds remain; partial results retained"})
            break
        label = f"{index:02d}-{case['variant']}-decor{int(case['decorated'])}-label{int(case['symbol_hint'])}-ax{int(case['accessibility_size'])}"
        case_dir = OUT / label
        case_dir.mkdir()
        token = uuid.uuid4().hex
        run(["xcrun", "simctl", "terminate", sim, BUNDLE], label + "-terminate.log", 15, check=False)
        arguments = ["xcrun", "simctl", "launch", sim, BUNDLE,
                     "--variant", case["variant"], "--decorated", str(int(case["decorated"])),
                     "--symbol-hint", str(int(case["symbol_hint"])),
                     "--accessibility-size", str(int(case["accessibility_size"])), "--token", token]
        try:
            run(arguments, label + "-launch.log", 20)
            actual = read_stable_geometry(geometry_file, token)
            for key in ("variant", "decorated", "symbol_hint", "accessibility_size"):
                if actual[key] != case[key]:
                    raise ValueError(f"Actual {key} differs from requested case")
            (case_dir / "geometry.json").write_text(json.dumps(actual, indent=2, sort_keys=True))
            image = case_dir / "framebuffer.png"
            run(["xcrun", "simctl", "io", sim, "screenshot", "--type=png", image], label + "-screenshot.log", 20)
            after = read_stable_geometry(geometry_file, token)
            if after != actual:
                raise ValueError("Real native geometry changed across physical screenshot; retain image and fail case")
            (case_dir / "geometry-after-screenshot.json").write_text(json.dumps(after, indent=2, sort_keys=True))
            dimensions = png_dimensions(image)
            if dimensions != [2064, 2752]:
                raise ValueError(f"Expected physical iPad original, got {dimensions}")
            entry = dict(case, case_index=index, output_directory=label,
                         geometry=actual, geometry_before_after_screenshot_stable=True,
                         png_dimensions=dimensions, png_bytes=image.stat().st_size,
                         png_sha256=hashlib.sha256(image.read_bytes()).hexdigest())
            observations.append(entry)
            (case_dir / "observation.json").write_text(json.dumps(entry, indent=2))
        except Exception as error:
            failure = dict(case, case_index=index, error_type=type(error).__name__, error=str(error))
            failed.append(failure)
            (case_dir / "failure.json").write_text(json.dumps(failure, indent=2))
except Exception as error:
    failed.append({"phase": "setup", "error_type": type(error).__name__, "error": str(error)})
finally:
    if sim:
        for operation in ("shutdown", "delete"):
            try:
                subprocess.run(["xcrun", "simctl", operation, sim], env=ENV, capture_output=True, timeout=10)
            except Exception:
                pass
    summary = {"schema_version": 1, "kind": "generic-native-list-sizing-observations",
               "expected_cases": 30, "completed_cases": len(observations), "failures": failed,
               "elapsed_seconds": time.monotonic() - started,
               "compile_count": 1, "xctest_cases_executed": 0,
               "scope": "Generic Apple-framework-only app, no private application source or credentials",
               "observations": observations,
               "diagnostic_completion_passed": len(observations) == 30 and not failed,
               "layout_approval_claimed": False}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    (OUT / "source-sha256.json").write_text(json.dumps({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "App/ListSizingApp.swift", ROOT / "project.yml", ROOT / "run.py")}, indent=2))
sys.exit(0 if len(observations) == 30 and not failed else 1)
