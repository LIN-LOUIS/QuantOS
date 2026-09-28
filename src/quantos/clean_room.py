"""Fail-closed clean-room acceptance for a public QuantOS snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import tarfile
import time
from typing import Iterable, Sequence

from quantos._compat import tomllib


MANIFEST_SCHEMA = "quantos-clean-room-acceptance-v1"
HISTORY_POLICY = "content-only-export-preserve-public-history-no-private-ancestry"
REQUIRED_FILES = (
    ".dockerignore", "Dockerfile", "LICENSE", "README.md", "pyproject.toml",
    "web/package.json", "web/package-lock.json", "src/quantos/__init__.py",
    "src/quantos/api/app.py", "src/quantos/api/protection.py",
    "public_export_manifest.txt", "PUBLIC_EXPORT_METADATA.json",
)
_FORBIDDEN_ANYWHERE_DIRS = {
    ".git", ".venv", "node_modules", "dist", "coverage", ".vite",
    ".pytest_cache", "__pycache__", "研发日报",
}
_FORBIDDEN_ROOT_DIRS = {"data", "reports", "credentials", "secrets", "build"}
_RUNTIME_SUFFIXES = (
    ".parquet", ".duckdb", ".duckdb.wal", ".sqlite", ".sqlite3", ".pyc",
)
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[opusr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(
        rb"\b(?:TUSHARE_TOKEN|TAVILY_API_KEY|DEEPSEEK_API_KEY|EXA_API_KEY)"
        rb"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]{24,}[\"']?",
        re.IGNORECASE,
    ),
)


class AcceptanceError(RuntimeError):
    """A safe stage-specific release-gate failure."""

    def __init__(self, stage: str, reason: str, remediation: str) -> None:
        self.stage = stage
        self.reason = reason
        self.remediation = remediation
        super().__init__(f"{stage}: {reason}. Remediation: {remediation}")


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AcceptanceManifest:
    schema_version: str
    project_version: str
    release_snapshot_id: str
    public_parent_identifier: str | None
    acceptance_result: str
    python_test_count: int
    frontend_test_count: int
    clean_room: str
    history_policy: str
    gates: tuple[Gate, ...]

    def to_bytes(self) -> bytes:
        payload = asdict(self)
        return (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")


def create_tracked_snapshot(repository: Path, destination: Path) -> None:
    """Extract ``HEAD`` through git archive, never copying working-tree extras."""

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    archive = destination.parent / "tracked-source.tar"
    run_checked(
        ["git", "archive", "--format=tar", "--output", str(archive), "HEAD"],
        stage="SOURCE_SNAPSHOT", cwd=repository,
        remediation="Commit the intended release files and retry from a valid Git checkout.",
    )
    try:
        with tarfile.open(archive, "r") as value:
            _safe_extract(value, destination)
    finally:
        archive.unlink(missing_ok=True)


def validate_required_files(root: Path, required: Iterable[str] = REQUIRED_FILES) -> None:
    missing = sorted(name for name in required if not (root / name).is_file())
    if missing:
        raise AcceptanceError(
            "REQUIRED_FILES", "missing required release files: " + ", ".join(missing),
            "Restore the audited release file set before running acceptance.",
        )


def runtime_artifact_candidates(root: Path) -> tuple[str, ...]:
    candidates: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        parts = relative.parts
        if (
            any(part in _FORBIDDEN_ANYWHERE_DIRS for part in parts)
            or (parts and parts[0] in _FORBIDDEN_ROOT_DIRS)
        ):
            candidates.append(relative.as_posix())
            continue
        lowered = relative.as_posix().lower()
        if path.is_file() and (
            path.name == ".env" or path.name.startswith(".env.")
            or lowered.endswith(_RUNTIME_SUFFIXES)
        ):
            candidates.append(relative.as_posix())
    return tuple(dict.fromkeys(candidates))


def secret_candidates(root: Path) -> tuple[str, ...]:
    """Return paths containing credential-like values, never their contents."""

    found: list[str] = []
    for path in sorted(value for value in root.rglob("*") if value.is_file()):
        try:
            content = path.read_bytes()
        except OSError as error:
            raise AcceptanceError(
                "SECRET_SCAN", f"could not read {path.relative_to(root).as_posix()}",
                "Make every public export file readable and retry.",
            ) from error
        if b"\0" in content[:4096]:
            continue
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            found.append(path.relative_to(root).as_posix())
    return tuple(found)


def stage_public_export(source: Path, destination: Path, allowlist: Path) -> tuple[str, ...]:
    entries = _allowlist_entries(allowlist)
    destination.mkdir(parents=True, exist_ok=False)
    for name in entries:
        origin = source / name
        if not origin.is_file():
            raise AcceptanceError(
                "PUBLIC_EXPORT", f"allowlisted file is missing: {name}",
                "Update the export allowlist and source together.",
            )
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
    actual = tuple(sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*") if path.is_file()
    ))
    if actual != entries:
        raise AcceptanceError(
            "EXPORT_DIFF", "staged export differs from the intended public surface",
            "Review public_export_manifest.txt and regenerate the staging tree.",
        )
    return entries


def snapshot_identifier(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(value for value in root.rglob("*") if value.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "public-snapshot-sha256:" + digest.hexdigest()


def project_version(root: Path) -> str:
    value = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(value["project"]["version"])


def validate_version_consistency(root: Path) -> str:
    version = project_version(root)
    frontend = json.loads((root / "web/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "web/package-lock.json").read_text(encoding="utf-8"))
    metadata = json.loads(
        (root / "PUBLIC_EXPORT_METADATA.json").read_text(encoding="utf-8")
    )
    observed = {
        "frontend": frontend.get("version"),
        "frontend_lock": lock.get("version"),
        "frontend_lock_root": lock.get("packages", {}).get("", {}).get("version"),
        "public_metadata": metadata.get("project_version"),
    }
    mismatches = sorted(name for name, value in observed.items() if value != version)
    if mismatches:
        raise AcceptanceError(
            "VERSION_CONSISTENCY", "version mismatch in: " + ", ".join(mismatches),
            "Align every release version surface with pyproject.toml.",
        )
    return version


def public_parent(root: Path) -> str | None:
    payload = json.loads((root / "PUBLIC_EXPORT_METADATA.json").read_text("utf-8"))
    value = payload.get("public_parent_commit")
    return str(value) if value else None


def make_manifest(
    *, root: Path, snapshot_id: str, python_tests: int, frontend_tests: int,
    gates: Sequence[Gate],
) -> AcceptanceManifest:
    result = "PASS" if gates and all(gate.status == "PASS" for gate in gates) else "FAIL"
    return AcceptanceManifest(
        schema_version=MANIFEST_SCHEMA,
        project_version=project_version(root),
        release_snapshot_id=snapshot_id,
        public_parent_identifier=public_parent(root),
        acceptance_result=result,
        python_test_count=python_tests,
        frontend_test_count=frontend_tests,
        clean_room=result,
        history_policy=HISTORY_POLICY,
        gates=tuple(gates),
    )


def require_pass(manifest: AcceptanceManifest) -> None:
    if manifest.acceptance_result != "PASS" or manifest.clean_room != "PASS":
        failed = ", ".join(g.name for g in manifest.gates if g.status != "PASS")
        raise AcceptanceError(
            "FINAL_ACCEPTANCE", f"mandatory gates did not pass: {failed or 'unknown'}",
            "Resolve every failed gate; partial acceptance cannot be exported.",
        )


def run_checked(
    command: Sequence[str], *, stage: str, cwd: Path,
    remediation: str, environment: dict[str, str] | None = None,
    output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command), cwd=cwd, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AcceptanceError(stage, "command could not complete", remediation) from error
    if output is not None:
        output.write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        raise AcceptanceError(stage, f"command exited with status {result.returncode}", remediation)
    return result


def parse_pytest_count(output: str) -> int:
    matches = re.findall(r"(?:^|\s)(\d+) passed", output)
    if not matches:
        raise AcceptanceError("PYTHON_TESTS", "pytest pass count was not found",
                              "Inspect the Python test log and rerun the gate.")
    return int(matches[-1])


def parse_frontend_count(output: str) -> int:
    matches = re.findall(r"Tests\s+(\d+) passed", output)
    if not matches:
        matches = re.findall(r"(?:^|\s)(\d+) passed", output)
    if not matches:
        raise AcceptanceError("FRONTEND_TESTS", "frontend pass count was not found",
                              "Inspect the frontend test log and rerun the gate.")
    return int(matches[-1])


def available_port() -> int:
    with socket.socket() as value:
        value.bind(("127.0.0.1", 0))
        return int(value.getsockname()[1])


def docker_smoke(
    *, source: Path, image: str, container: str, snapshot_id: str,
    no_cache: bool, logs: Path,
) -> dict[str, str]:
    build = ["docker", "build"]
    if no_cache:
        build.append("--no-cache")
    build.extend([
        "--build-arg", f"QUANTOS_BUILD_COMMIT={snapshot_id}",
        "--tag", image, ".",
    ])
    run_checked(build, stage="DOCKER_BUILD", cwd=source,
                remediation="Confirm Docker is running and the clean export builds.",
                output=logs / "docker-build.log")
    started = False
    try:
        run_checked([
            "docker", "run", "--detach", "--name", container,
            "--label", "org.quantos.clean-room=true", "--network", "none",
            image,
        ], stage="CONTAINER_START", cwd=source,
            remediation="Inspect the container log and Public Preview command.",
            output=logs / "container-start.log")
        started = True
        _wait_container_health(container)
        uid = run_checked(
            ["docker", "exec", container, "id", "-u"], stage="NON_ROOT_RUNTIME",
            cwd=source, remediation="Run the image with its non-root quantos user.",
        ).stdout.strip()
        if uid == "0":
            raise AcceptanceError("NON_ROOT_RUNTIME", "container runs as root",
                                  "Restore USER quantos in the runtime image.")
        inventory = run_checked(
            ["docker", "exec", container, "find", "/app", "-type", "f", "-print"],
            stage="IMAGE_CONTENT_SCAN", cwd=source,
            remediation="Remove credentials and runtime data from the runtime image.",
        ).stdout.splitlines()
        environment = run_checked(
            ["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}",
             container],
            stage="IMAGE_ENV_SCAN", cwd=source,
            remediation="Remove provider credentials from the image configuration.",
        ).stdout.splitlines()
        validate_container_inventory(inventory, environment)
        payload = _container_http_acceptance(container, source)
        if payload.get("runtime_mode") != "DEMO":
            raise AcceptanceError("RUNTIME_MODE", "runtime mode is not DEMO",
                                  "Start Public Preview with --demo.")
        if payload.get("deployment_mode") != "public_preview":
            raise AcceptanceError("DEPLOYMENT_MODE", "deployment mode is not public_preview",
                                  "Start with --public-preview.")
        if payload.get("build_commit") != snapshot_id:
            raise AcceptanceError("BUILD_IDENTITY", "runtime snapshot identifier differs",
                                  "Inject the accepted snapshot ID during docker build.")
        return {"runtime_user": uid, **payload}
    finally:
        if started:
            subprocess.run(["docker", "rm", "--force", container], cwd=source,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def validate_container_inventory(paths: Iterable[str], environment: Iterable[str]) -> None:
    forbidden_paths = []
    for raw in paths:
        relative = raw.removeprefix("/app/")
        pure = PurePosixPath(relative)
        lowered = relative.lower()
        allowed_workspace_asset = pure.parts[:2] == ("web", "dist")
        if (
            any(
                part in _FORBIDDEN_ANYWHERE_DIRS
                and not (part == "dist" and allowed_workspace_asset)
                for part in pure.parts
            )
            or (pure.parts and pure.parts[0] in _FORBIDDEN_ROOT_DIRS)
            or pure.name == ".env" or pure.name.startswith(".env.")
            or lowered.endswith(_RUNTIME_SUFFIXES)
        ):
            forbidden_paths.append(raw)
    forbidden_keys = []
    for item in environment:
        key = item.partition("=")[0].upper()
        if any(marker in key for marker in (
            "TUSHARE", "TAVILY", "DEEPSEEK", "EXA_API", "PASSWORD", "SECRET", "TOKEN",
        )):
            forbidden_keys.append(key)
    if forbidden_paths or forbidden_keys:
        raise AcceptanceError(
            "IMAGE_SECURITY", (
                f"runtime image contains {len(forbidden_paths)} forbidden path(s) "
                f"and {len(forbidden_keys)} credential environment key(s)"
            ),
            "Remove credentials and user/runtime data from the image build.",
        )


def _container_http_acceptance(container: str, cwd: Path) -> dict[str, str]:
    script = r'''
import json, urllib.error, urllib.request
def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=5) as r:
        assert r.status == 200
        return json.load(r) if r.headers.get_content_type() == "application/json" else r.read().decode()
def post(path, payload):
    request=urllib.request.Request("http://127.0.0.1:8000"+path,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=10) as r: return json.load(r)
health=get("/v1/health")
assert "QuantOS" in get("/")
for path in ("/docs","/redoc","/openapi.json"):
    try: urllib.request.urlopen("http://127.0.0.1:8000"+path,timeout=5); raise AssertionError(path)
    except urllib.error.HTTPError as error: assert error.code == 404
status=get("/v1/status")
ask=post("/v1/ask",{"symbol":"600519.SH","question":"最近一个交易日表现怎么样？","as_of_time":"2026-09-18T18:00:00+08:00","no_research":True})
trace=get("/v1/traces/"+ask["trace_id"])
analytics=post("/v1/analytics/query",{"metrics":["close"],"dimensions":["trading_date"],"entities":["600519.SH"],"time_range":{"start":"2026-08-20","end":"2026-09-18"},"filters":[],"sort":[{"field":"trading_date","direction":"ASC"}],"limit":20,"as_of_time":"2026-09-19T12:00:00+08:00"})
assert analytics["rows"] and analytics["provenance"]
reports=get("/v1/reports"); assert reports["items"]
report=get("/v1/reports/"+reports["items"][0]["report_id"])
replays=get("/v1/replay/campaigns"); assert replays["items"]
campaign_id=replays["items"][0]["campaign_id"]
replay=get("/v1/replay/campaigns/"+campaign_id)
failures=get("/v1/replay/campaigns/"+campaign_id+"/failures")
assert status and trace and report and replay and failures is not None
print(json.dumps(health,sort_keys=True))
'''
    result = run_checked(
        ["docker", "exec", container, "python", "-c", script],
        stage="E2E_DEMO", cwd=cwd,
        remediation="Inspect Public Preview logs and the synthetic fixture surface.",
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _wait_container_health(container: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            text=True, capture_output=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "healthy":
            return
        if result.returncode == 0 and result.stdout.strip() == "unhealthy":
            break
        time.sleep(0.5)
    raise AcceptanceError("CONTAINER_HEALTH", "container did not become healthy",
                          "Inspect docker logs and /v1/health startup behavior.")


def _allowlist_entries(path: Path) -> tuple[str, ...]:
    entries = tuple(sorted({
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }))
    for value in entries:
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts:
            raise AcceptanceError("PUBLIC_EXPORT", f"unsafe allowlist path: {value}",
                                  "Use repository-relative paths only.")
    return entries


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    for member in archive.getmembers():
        pure = PurePosixPath(member.name)
        if pure.is_absolute() or ".." in pure.parts or member.issym() or member.islnk():
            raise AcceptanceError("SOURCE_SNAPSHOT", "unsafe archive entry",
                                  "Audit tracked paths before release acceptance.")
    archive.extractall(destination)
