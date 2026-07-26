from __future__ import annotations

from swe_v1_prolite_runner_test_support import Path, _remote_namespace


def test_eval_output_publisher_rejects_oversize_and_write_failure(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    original_atomic_write = namespace["atomic_write_bytes"]
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "f2p.log").write_bytes(b"12345")
    monkeypatch.setitem(namespace, "MAX_TEST_EVIDENCE_BYTES", 4)

    errors = namespace["publish_eval_output_artifacts"](source, output, ["f2p.log"])

    assert errors == ["publish:f2p.log:RecordInputLimitError"]
    assert not (output / "f2p.log").exists()

    monkeypatch.setitem(
        namespace,
        "atomic_write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("NFS write failed")),
    )
    monkeypatch.setitem(namespace, "MAX_TEST_EVIDENCE_BYTES", 64)
    errors = namespace["publish_eval_output_artifacts"](source, output, ["f2p.log"])
    assert errors == ["publish:f2p.log:OSError"]

    monkeypatch.setitem(namespace, "atomic_write_bytes", original_atomic_write)
    original_chmod = Path.chmod

    def fail_output_chmod(path, mode):
        if path == output / "f2p.log":
            raise OSError("NFS chmod failed")
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", fail_output_chmod)
    errors = namespace["publish_eval_output_artifacts"](source, output, ["f2p.log"])
    assert errors == ["publish:f2p.log:OSError"]


def test_temporary_output_cleanup_failure_is_reported(tmp_path):
    namespace = _remote_namespace(tmp_path)

    class BrokenTemporaryOutput:
        def cleanup(self):
            raise PermissionError("root-owned child")

    assert namespace["cleanup_temporary_output"](BrokenTemporaryOutput()) == [
        "cleanup:temporary_output:PermissionError"
    ]
