import importlib.util
import os

import pytest

from opencollab_eval.engine.swe_v1_remote_pytest_controller import prolite_pytest_controller_source


def controller():
    namespace = {"__name__": "controller_test"}
    exec(prolite_pytest_controller_source(), namespace)
    return namespace


@pytest.mark.parametrize("qt5,qt6,expected", [(False, True, "PyQt6"), (True, True, None), (False, False, None)])
def test_selects_available_project_binding(tmp_path, monkeypatch, qt5, qt6, expected):
    namespace = controller()
    path = tmp_path / "qutebrowser/qt/machinery.py"
    path.parent.mkdir(parents=True)
    path.write_text('QUTE_QT_WRAPPER = "PyQt6"\n')
    monkeypatch.delenv("QUTE_QT_WRAPPER", raising=False)
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: object() if {"PyQt5": qt5, "PyQt6": qt6}[name] else None,
    )
    namespace["_configure_project_qt_wrapper"](tmp_path)
    assert os.environ.get("QUTE_QT_WRAPPER") == expected


def test_preserves_explicit_binding_and_other_projects(tmp_path, monkeypatch):
    namespace = controller()
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: pytest.fail("unexpected dependency probe"))
    monkeypatch.setenv("QUTE_QT_WRAPPER", "PyQt5")
    namespace["_configure_project_qt_wrapper"](tmp_path)
    assert os.environ["QUTE_QT_WRAPPER"] == "PyQt5"
    monkeypatch.delenv("QUTE_QT_WRAPPER")
    namespace["_configure_project_qt_wrapper"](tmp_path)
    assert "QUTE_QT_WRAPPER" not in os.environ
