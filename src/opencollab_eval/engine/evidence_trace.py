"""Small trace handle for artifacts produced by the public OpenCollab runtime."""

from __future__ import annotations

from pathlib import Path

# Which file a run actually writes its trace to. A workflow run and a team run
# are both several sessions under one run folder, but they are written by
# different parts of OpenCollab and they do not use the same name; reporting a
# path that does not exist is worse than reporting none, so these live in one
# place and the caller says which regime it ran.
ORCHESTRATION_FILENAME = "orchestration.jsonl"
TRAJECTORY_FILENAME = "trajectory.jsonl"


class EvidenceTrace:
    """Expose the expected trajectory path without owning the runtime writer."""

    def __init__(
        self,
        *,
        run_id: str,
        output_dir: str,
        filename: str = TRAJECTORY_FILENAME,
    ) -> None:
        self.path = str(Path(output_dir) / filename)
        self.run_id = run_id
        self.write_error: str | None = None

    def close(self) -> None:
        """Leave persistence ownership with the OpenCollab run."""

    def bind_artifacts(self, directory: Path, *, filename: str) -> None:
        """Point the result at the file this run's writer actually produced."""
        self.path = str(directory / filename)


__all__ = ["ORCHESTRATION_FILENAME", "TRAJECTORY_FILENAME", "EvidenceTrace"]
