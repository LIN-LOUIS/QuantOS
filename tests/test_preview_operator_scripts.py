"""Public Preview operator scripts stay bounded to their named container."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = tuple(ROOT / "scripts" / f"preview-{name}.sh" for name in ("build", "up", "down", "status", "logs"))


def _fake_tools(tmp_path: Path, *, container_exists: bool = False) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> \"$FAKE_DOCKER_LOG\"\n"
        "if [[ \"$1 $2\" == \"container inspect\" && \"${3:-}\" != --format ]]; then [[ \"${FAKE_CONTAINER_EXISTS:-0}\" == 1 ]]; exit; fi\n"
        "if [[ \"$1 $2\" == \"container inspect\" && \"${3:-}\" == --format ]]; then echo \"${FAKE_OWNER:-true}\"; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for item in bin_dir.iterdir():
        item.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_CONTAINER_EXISTS": "1" if container_exists else "0",
        "QUANTOS_PREVIEW_PORT": "18080",
    })
    return env, log


def test_operator_scripts_are_valid_and_never_use_broad_container_cleanup():
    for script in SCRIPTS:
        assert script.exists()
        assert os.access(script, os.X_OK)
        subprocess.run(["bash", "-n", str(script)], check=True)
        source = script.read_text(encoding="utf-8")
        assert "pkill" not in source
        assert "killall" not in source
        assert "docker ps" not in source
        assert "408-ai-tutor" not in source


def test_preview_up_binds_loopback_by_default_and_lan_only_when_explicit(tmp_path):
    env, log = _fake_tools(tmp_path)
    local = subprocess.run([str(ROOT / "scripts" / "preview-up.sh")], env=env, text=True, capture_output=True, check=True)
    assert "Mode: LOCAL" in local.stdout
    assert "URL: http://127.0.0.1:18080" in local.stdout
    assert "-p 127.0.0.1:18080:8000" in log.read_text(encoding="utf-8")

    log.write_text("", encoding="utf-8")
    lan = subprocess.run([str(ROOT / "scripts" / "preview-up.sh"), "--lan"], env=env, text=True, capture_output=True, check=True)
    assert "Mode: LAN" in lan.stdout
    assert "WARNING: LAN mode exposes" in lan.stdout
    assert "-p 0.0.0.0:18080:8000" in log.read_text(encoding="utf-8")


def test_preview_build_injects_explicit_clean_room_identity(tmp_path):
    env, log = _fake_tools(tmp_path)
    expected = "public-snapshot-sha256:" + "a" * 64
    env["QUANTOS_BUILD_COMMIT"] = expected
    subprocess.run([str(ROOT / "scripts" / "preview-build.sh")], env=env, check=True, capture_output=True, text=True)
    recorded = log.read_text(encoding="utf-8")
    assert f"--build-arg QUANTOS_BUILD_COMMIT={expected}" in recorded
    assert "--tag quantos-public-preview:local" in recorded


def test_preview_build_fails_clearly_without_git_or_explicit_identity(tmp_path):
    env, _log = _fake_tools(tmp_path)
    exported = tmp_path / "exported"
    (exported / "scripts").mkdir(parents=True)
    script = exported / "scripts" / "preview-build.sh"
    script.write_bytes((ROOT / "scripts" / "preview-build.sh").read_bytes())
    script.chmod(0o755)

    result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

    assert result.returncode == 1
    assert "Set QUANTOS_BUILD_COMMIT" in result.stderr


def test_preview_down_removes_only_the_labeled_preview_container(tmp_path):
    env, log = _fake_tools(tmp_path, container_exists=True)
    subprocess.run([str(ROOT / "scripts" / "preview-down.sh")], env=env, check=True, capture_output=True, text=True)
    commands = log.read_text(encoding="utf-8").splitlines()
    assert "rm -f quantos-public-preview" in commands
    assert all("408-ai-tutor" not in command for command in commands)

    log.write_text("", encoding="utf-8")
    env["FAKE_OWNER"] = "false"
    rejected = subprocess.run([str(ROOT / "scripts" / "preview-down.sh")], env=env, capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "not owned by the QuantOS Preview operator" in rejected.stderr
    assert not any(line.startswith("rm ") for line in log.read_text(encoding="utf-8").splitlines())
