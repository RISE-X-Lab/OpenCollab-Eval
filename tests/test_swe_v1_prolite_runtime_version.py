from __future__ import annotations

from pathlib import Path

import pytest

from opencollab_eval.commands import swe_v1_prolite_config as runtime_config


def test_runtime_sync_rejects_opencollab_before_050(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_SOURCE_ROOT", raising=False)
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.4.1")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.4.1")

    with pytest.raises(RuntimeError, match="OpenCollab >=0.5.0,<0.6"):
        runtime_config._runtime_directory_sources()


def test_runtime_sync_rejects_distribution_source_version_drift(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_SOURCE_ROOT", raising=False)
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.5.0")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.4.0")

    with pytest.raises(RuntimeError, match="source version does not match"):
        runtime_config._runtime_directory_sources()


def test_runtime_sync_uses_explicit_opencollab_source_checkout(monkeypatch, tmp_path):
    source_root = tmp_path / "OpenCollab"
    package_root = source_root / "opencollab"
    package_root.mkdir(parents=True)
    (source_root / "pyproject.toml").write_text("[project]\nname='opencollab'\n")
    (package_root / "__init__.py").write_text('__version__ = "0.5.0"\n')
    monkeypatch.setenv("OPENCOLLAB_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.5.0")
    monkeypatch.setattr(runtime_config, "verify_runtime_import_contract", lambda: None)

    sources, release = runtime_config._runtime_directory_sources()

    assert sources["src/opencollab"] == package_root.resolve()
    assert release == "0.5.0"


def test_runtime_sync_rejects_invalid_explicit_source_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_SOURCE_ROOT", str(tmp_path))

    with pytest.raises(RuntimeError, match="OpenCollab source checkout"):
        runtime_config._runtime_directory_sources()


def test_declared_opencollab_version_requires_literal_assignment(tmp_path):
    package_root = Path(tmp_path) / "opencollab"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("__version__ = build_version()\n")

    assert runtime_config._declared_opencollab_version(package_root) is None


def test_runtime_sync_rejects_opencollab_k3_capability_release(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_SOURCE_ROOT", raising=False)
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.4.1")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.4.1")

    with pytest.raises(RuntimeError, match="OpenCollab >=0.5.0,<0.6"):
        runtime_config._runtime_directory_sources()


def test_runtime_sync_accepts_opencollab_050(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_SOURCE_ROOT", raising=False)
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.5.0")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.5.0")

    sources, release = runtime_config._runtime_directory_sources()

    assert release == "0.5.0"
    assert "src/opencollab" in sources


def test_runtime_sync_rejects_unvalidated_opencollab_060(monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_SOURCE_ROOT", raising=False)
    monkeypatch.setattr(runtime_config, "version", lambda _name: "0.6.0")
    package = runtime_config.importlib.import_module("opencollab")
    monkeypatch.setattr(package, "__version__", "0.6.0")

    with pytest.raises(RuntimeError, match="OpenCollab >=0.5.0,<0.6"):
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
