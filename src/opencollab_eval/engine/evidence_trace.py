"""Small trace handle for artifacts produced by the public OpenCollab runtime."""

from __future__ import annotations

from pathlib import Path


class EvidenceTrace:
    """Expose the expected trajectory path without owning the runtime writer."""

    def __init__(
        self,
        *,
        run_id: str,
        output_dir: str,
        filename: str = "trajectory.jsonl",
    ) -> None:
        self.path = str(Path(output_dir) / filename)
        self.run_id = run_id
        self.write_error: str | None = None

    def close(self) -> None:
        """Leave persistence ownership with the OpenCollab run."""

    def bind_artifacts(self, directory: Path, *, workflow: bool) -> None:
        """Point the result at the artifact writer selected for this run."""
        filename = "orchestration.jsonl" if workflow else "trajectory.jsonl"
        self.path = str(directory / filename)


__all__ = ["EvidenceTrace"]
