# Public-test evidence owned by Eval

OpenCollab removed its built-in test tool in PR #95. Ordinary Single runs and
YAML team examples use the current OC built-ins and Bash for native commands.
Research workflows that require parser-backed exact-target evidence use
`evaluation_tools("run_tests")` from this package. The model-facing name remains
stable so those workflows retain their existing verification semantics.

The pytest, Go and Django parsers were moved from OpenCollab commit
`d5d5a6d12b` immediately before the removal, under the same MulanPSL-2.0 license.
Their regression tests now live with this implementation. Runtime integration
uses the public structural tool interface and contains no OC private imports.

Model roles require process isolation and cannot override the runner or add
flags. Mechanical verification can select its already approved command under
its existing isolation checks. `verified_targets` records only the latest
parser-backed passing targets; failed overlapping runs invalidate older proof.
`verification_records` retains the actual command, runner, target, exit code and
verification outcome for candidate comparison. Empty, collection-only, skipped,
truncated and mismatched-target output cannot produce passing evidence.

Runner discovery and execution explicitly enter the environment's declared
workspace, including when container shell initialization selects another
directory. Absolute pytest paths are made relative to that same workspace
before execution and proof matching, with their full paths and selectors
preserved. Relative and absolute aliases invalidate the same previous proof
after a failed rerun. Verification records retain the originally requested
target, test command, and workspace. The workspace prefix remains separate from
the command identity so independent candidate workspaces can compare the same
test command.

Headless tool schemas expose only usable options. Disabled runner overrides
and extra flags remain rejected if supplied programmatically. An empty runner
value requests automatic detection, just like an omitted runner.
