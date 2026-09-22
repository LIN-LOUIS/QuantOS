"""Release version has one project authority and validated consumers."""

from __future__ import annotations

from importlib.metadata import version as installed_version
import json
from pathlib import Path

from fastapi.testclient import TestClient

from quantos import __version__
from quantos._compat import tomllib
from quantos.api import create_app
from quantos.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def test_supported_toml_reader_parses_project_metadata():
    parsed = tomllib.loads('[project]\nname = "quantos"\n')

    assert parsed["project"]["name"] == "quantos"


def test_project_runtime_and_frontend_versions_are_aligned(tmp_path):
    project_version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    frontend_version = json.loads(
        (ROOT / "web" / "package.json").read_text(encoding="utf-8")
    )["version"]

    assert project_version == "0.3.1"
    assert installed_version("quantos") == project_version
    assert __version__ == project_version
    assert frontend_version == project_version
    assert TestClient(create_app(settings=Settings.from_project_root(tmp_path))).get(
        "/v1/health"
    ).json()["quantos_version"] == project_version
