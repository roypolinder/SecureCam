"""Shared fixtures. Tests never touch the real /etc or /var paths."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from securecam.config import Config, parse_config  # noqa: E402


@pytest.fixture
def raw_config(tmp_path):
    """Minimal configuration mapping pointed at a temporary directory."""
    return {
        "device": {"id": "test-cam", "name": "Test Cam"},
        "camera": {},
        "storage": {
            "base_path": str(tmp_path / "data"),
            "events_path": str(tmp_path / "data" / "events"),
            "buffer_path": str(tmp_path / "data" / "buffer"),
        },
        "mediamtx": {"config_path": str(tmp_path / "mediamtx.yml")},
        "motion": {"source": "disabled"},
        "notifications": {"enabled": False},
        "ai": {"enabled": False},
        "api": {"enabled": False},
        "health": {"state_file": str(tmp_path / "health.json")},
        "logging": {"file": ""},
    }


@pytest.fixture
def config(raw_config) -> Config:
    """Parsed configuration built from `raw_config`."""
    return parse_config(raw_config, "test-config.yaml")


@pytest.fixture
def events_dir(tmp_path):
    path = tmp_path / "data" / "events"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)
