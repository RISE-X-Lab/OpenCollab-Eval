"""NodeBB Mocha title normalization and bound target-file launcher."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
import shlex

_IMPORTED_SUITE_MARKER = re.compile(r"(?<!\S)test/[^\s:]+\.js::")


def nodebb_mocha_runtime_title(title: object) -> str:
    return " ".join(_IMPORTED_SUITE_MARKER.sub("", str(title)).split())


def nodebb_mocha_selector_title(title: object) -> str:
    return _IMPORTED_SUITE_MARKER.sub("", str(title))


def nodebb_mocha_titles_are_unambiguous(tests: object) -> bool:
    runtime_titles = [
        nodebb_mocha_runtime_title(str(item).split(" | ", 1)[1])
        for item in tests
        if " | " in str(item)
    ]
    return bool(
        all(runtime_titles)
        and len(set(runtime_titles)) == len(runtime_titles)
    )


def nodebb_mocha_target_file_command(tests: object, target_file: str) -> str:
    launcher = """import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys

tests = json.loads(open(sys.argv[1], encoding="utf-8").read())
canonical_tests = json.dumps(tests, ensure_ascii=True, separators=(",", ":"))
actual_targets_sha256 = hashlib.sha256(canonical_tests.encode("utf-8")).hexdigest()
if actual_targets_sha256 != __OPENCOLLAB_EXPECTED_TARGETS_SHA256__:
    print("Mocha target file does not match declared targets", file=sys.stderr)
    raise SystemExit(127)
imported_suite_marker = re.compile(__OPENCOLLAB_IMPORTED_SUITE_PATTERN__)
grouped = {}
runtime_titles = set()
for value in tests:
    item = str(value)
    if " | " not in item:
        continue
    test_file, title = item.split(" | ", 1)
    selector_title = imported_suite_marker.sub("", title)
    normalized_title = " ".join(selector_title.split())
    if not normalized_title or normalized_title in runtime_titles:
        print("ambiguous NodeBB Mocha titles after import normalization", file=sys.stderr)
        raise SystemExit(127)
    runtime_titles.add(normalized_title)
    grouped.setdefault(test_file, []).append(selector_title)
if not grouped:
    print("missing declared Mocha titles", file=sys.stderr)
    raise SystemExit(127)
status = 0
for test_file in sorted(grouped):
    selector = "^(?:" + "|".join(re.escape(title) for title in grouped[test_file]) + ")$"
    if pathlib.Path("./node_modules/.bin/mocha").is_file():
        command = ["./node_modules/.bin/mocha", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("yarn"):
        command = ["yarn", "test", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("npx"):
        command = ["npx", "mocha", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("pnpm"):
        command = ["pnpm", "test", "--", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("corepack"):
        command = ["corepack", "pnpm", "test", "--", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    else:
        print("No supported JS test runner found for mocha", file=sys.stderr)
        raise SystemExit(127)
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if result.returncode != 0:
        status = result.returncode
raise SystemExit(status)
""".replace(
        "__OPENCOLLAB_IMPORTED_SUITE_PATTERN__",
        repr(_IMPORTED_SUITE_MARKER.pattern),
        1,
    ).replace(
        "__OPENCOLLAB_EXPECTED_TARGETS_SHA256__",
        repr(
            hashlib.sha256(
                json.dumps(
                    [str(item) for item in tests],
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        ),
        1,
    )
    return "python3 -I -c " + shlex.quote(launcher) + " " + shlex.quote(target_file)


__all__ = [
    "nodebb_mocha_runtime_title",
    "nodebb_mocha_selector_title",
    "nodebb_mocha_target_file_command",
    "nodebb_mocha_titles_are_unambiguous",
]
