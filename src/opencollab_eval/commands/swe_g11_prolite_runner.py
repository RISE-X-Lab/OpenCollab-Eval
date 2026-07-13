#!/usr/bin/env python3
"""G1.1 command entry for the SWE Pro-Lite runner."""

from opencollab_eval.commands.swe_v1_prolite_runner import main as run_prolite


def main() -> int:
    return int(run_prolite(prog="python -m opencollab_eval.commands.swe_g11_prolite_runner"))


if __name__ == "__main__":
    raise SystemExit(main())
