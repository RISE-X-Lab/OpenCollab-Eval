from __future__ import annotations

import pytest

from opencollab_eval.commands import swe_v1_prolite_config as runtime_config


def test_runtime_sync_rejects_opencollab_before_k3_capability_fix(monkeypatch):
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.4.0")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.4.0")

    with pytest.raises(RuntimeError, match="OpenCollab >=0.4.1,<0.6"):
        runtime_config._runtime_directory_sources()


def test_runtime_sync_rejects_distribution_source_version_drift(monkeypatch):
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.4.1")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.4.0")

    with pytest.raises(RuntimeError, match="source version does not match"):
        runtime_config._runtime_directory_sources()


def test_runtime_sync_accepts_opencollab_k3_capability_release(monkeypatch):
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.4.1")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.4.1")

    sources, release = runtime_config._runtime_directory_sources()

    assert release == "0.4.1"
    assert "src/opencollab" in sources


def test_runtime_sync_accepts_opencollab_050(monkeypatch):
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.5.0")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.5.0")

    sources, release = runtime_config._runtime_directory_sources()

    assert release == "0.5.0"
    assert "src/opencollab" in sources


def test_runtime_sync_rejects_unvalidated_opencollab_060(monkeypatch):
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.6.0")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.6.0")

    with pytest.raises(RuntimeError, match="OpenCollab >=0.4.1,<0.6"):
        runtime_config._runtime_directory_sources()


def test_remote_stage_binds_source_to_exact_distribution_version(monkeypatch):
    sources, _release = runtime_config._runtime_directory_sources()
    scripts = []

    def capture_install(command, **_kwargs):
        if command[0] == "ssh" and "tar -xzf" in command[-1]:
            scripts.append(command[-1])
            raise RuntimeError("captured install script")

    monkeypatch.setattr(runtime_config, "_runtime_directory_sources", lambda: (sources, "0.4.2"))
    monkeypatch.setattr(runtime_config, "run_ssh_checked", capture_install)

    with pytest.raises(RuntimeError, match="captured install script"):
        runtime_config.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo="/remote/runtime",
        )

    assert "assert opencollab.__version__==" in scripts[0]
    assert "0.4.2" in scripts[0]
    assert "startswith" not in scripts[0]
