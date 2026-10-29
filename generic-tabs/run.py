import json, os, pathlib, shutil, subprocess, sys, uuid, signal, threading, time

root = pathlib.Path(__file__).resolve().parent
output = root / "results"
output.mkdir()
env = dict(os.environ, DEVELOPER_DIR="/Applications/Xcode_27.1.app/Contents/Developer")

def run(argv, name, timeout, check=True):
    with (output / name).open("wb") as log:
        result = subprocess.run(list(map(str, argv)), cwd=root, env=env, stdout=log,
            stderr=subprocess.STDOUT, timeout=timeout, check=False)
    if check and result.returncode: raise RuntimeError(f"{name}: exit {result.returncode}")
    return result

sim = None
recorder = None
recording_timer = None
exit_code = 1
try:
    data = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "-j"], env=env))
    device = next(d for d in data["devicetypes"] if d["name"] == "iPhone 13 mini")
    supported = [r for r in data["runtimes"] if r.get("isAvailable") and ".iOS-" in r["identifier"]
        and device["identifier"] in [t.get("identifier") if isinstance(t, dict) else t
                                    for t in r.get("supportedDeviceTypes", [])]]
    runtime = max(supported, key=lambda r: tuple(int(v) for v in r["version"].split(".")))
    sim = subprocess.check_output(["xcrun", "simctl", "create", "GenericTabRepro-" + uuid.uuid4().hex,
        device["identifier"], runtime["identifier"]], env=env, text=True).strip()
    (output / "runtime.json").write_text(json.dumps({"device": device, "runtime": runtime, "uuid": sim}, indent=2))
    if not shutil.which("xcodegen"): run(["brew", "install", "xcodegen"], "install-xcodegen.log", 180)
    run(["xcodegen", "generate"], "generate.log", 30)
    run(["xcodebuild", "build-for-testing", "-project", "GenericNativeTabs.xcodeproj", "-scheme", "GenericNativeTabs",
         "-configuration", "Debug", "-destination", "id=" + sim, "-derivedDataPath", root / "DerivedData",
         "-jobs", "1", "CODE_SIGNING_ALLOWED=NO"], "build.log", 180)
    run(["xcrun", "simctl", "boot", sim], "boot.log", 120)
    run(["xcrun", "simctl", "bootstatus", sim, "-b"], "bootstatus.log", 180)
    runs = list((root / "DerivedData/Build/Products").glob("*.xctestrun"))
    if len(runs) != 1: raise ValueError("Expected one generated xctestrun")
    result = run(["xcodebuild", "test-without-building", "-xctestrun", runs[0], "-destination", "id=" + sim,
        "-parallel-testing-enabled", "NO", "-test-timeouts-enabled", "YES",
        "-default-test-execution-time-allowance", "180", "-maximum-test-execution-time-allowance", "210",
        "-only-testing:GenericNativeTabsTests/TabReproTests/testNativeAnchorRegistrationSemantics",
        "-resultBundlePath", output / "native.xcresult"], "test.log", 240, check=False)
    exit_code = result.returncode
except Exception as error:
    (output / "error.json").write_text(json.dumps({"kind": type(error).__name__, "message": str(error)}, indent=2))
finally:
    if recording_timer: recording_timer.cancel()
    if recorder:
        if recorder.poll() is None: recorder.send_signal(signal.SIGINT)
        try: recorder.wait(timeout=10)
        except subprocess.TimeoutExpired: recorder.kill()
    result = output / "native.xcresult"
    if result.is_dir():
        for name, argv in (("summary.json", ["get", "test-results", "summary", "--path", result]),
                           ("attachments-export.log", ["export", "attachments", "--path", result, "--output-path", output / "attachments"])):
            try: run(["xcrun", "xcresulttool", *argv], name, 20, check=False)
            except Exception as error: (output / (name + ".error")).write_text(type(error).__name__)
    # Keep finalized native summary and original requested PNG/JSON attachments.
    # Full raw bundles and automatic videos are intentionally omitted from this
    # small generic experiment artifact; their SHA-256 manifest stays explicit.
    import hashlib
    omitted = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and ("native.xcresult" in path.parts or path.suffix in {".mp4", ".mov"}):
            omitted.append({"path": str(path.relative_to(output)), "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (output / "artifact-policy.json").write_text(json.dumps({
        "kind": "finalized-summary-and-original-exported-attachments",
        "raw_xcresult_retained": False, "automatic_videos_retained": False,
        "omitted_members": omitted}, indent=2))
    if result.is_dir(): shutil.rmtree(result)
    for path in output.rglob("*"):
        if path.is_file() and path.suffix in {".mp4", ".mov"}: path.unlink()
    if sim:
        for command in ("shutdown", "delete"):
            try: subprocess.run(["xcrun", "simctl", command, sim], env=env, capture_output=True, timeout=5)
            except Exception: pass
    (output / "run-exit.json").write_text(json.dumps({"test_process_exit_code": exit_code, "app_source": "generic-public-repro-only"}))
sys.exit(exit_code)
