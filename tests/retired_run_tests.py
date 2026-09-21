"""Whether this OpenCollab revision still has the built-in test runner.

OpenCollab retired ``run_tests`` (its `feat!: remove the built-in run_tests
tool`). Most arms here only listed the tool and were migrated to run tests
through ``bash``. One was not: ``analyst_solve`` gates a coder's claim on
*parser-backed* evidence -- the set of targets the runner itself saw pass,
read off the tool instance through OpenCollab's ``VerificationTool`` protocol.
Nothing in a shell result carries that, and inventing a replacement parser
would make the arm a different instrument than the one its recorded rows were
produced under.

So the arm is frozen rather than migrated: its code is the text that ran, and
it runs only against the revision its batch specs pin. The modules that
exercise it skip here and execute unchanged at that pin, which is also where
they are meaningful.
"""

from __future__ import annotations

from opencollab.tools import builtin_tools

try:
    builtin_tools("run_tests")
except ValueError:
    HAVE_RUN_TESTS = False
else:
    HAVE_RUN_TESTS = True

FROZEN_ARM_REASON = (
    "analyst_solve is frozen at the OpenCollab revision its batch specs pin: "
    "its F2P gate reads parser-backed evidence from the retired `run_tests` "
    "tool, and this revision has no such tool"
)
