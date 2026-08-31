"""NodeBB Mocha title normalization and bound target-file launcher."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
import shlex

_IMPORTED_SUITE_MARKER = re.compile(r"(?<!\S)test/[^\s:]+\.js::")
NODEBB_MOCHA_FILE_MARKER = "OPENCOLLAB_MOCHA_FILE "


def nodebb_mocha_runtime_title(title: object) -> str:
    return " ".join(_IMPORTED_SUITE_MARKER.sub("", str(title)).split())


def nodebb_mocha_selector_title(title: object) -> str:
    return _IMPORTED_SUITE_MARKER.sub("", str(title))


def nodebb_mocha_titles_are_unambiguous(
    tests: object, *, allow_cross_file_duplicates: bool = False
) -> bool:
    runtime_pairs = []
    for item in tests:
        value = str(item)
        if " | " not in value:
            continue
        test_file, title = value.split(" | ", 1)
        runtime_title = nodebb_mocha_runtime_title(title)
        runtime_pairs.append((test_file, runtime_title))
    runtime_titles = [
        pair if allow_cross_file_duplicates else pair[1] for pair in runtime_pairs
    ]
    return bool(
        all(title for _test_file, title in runtime_pairs)
        and len(set(runtime_titles)) == len(runtime_titles)
    )


def nodebb_mocha_target_file_command(tests: object, target_file: str) -> str:
    launcher = """import hashlib
import json
import os
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
    title_key = (test_file, normalized_title)
    if not normalized_title or title_key in runtime_titles:
        print("ambiguous NodeBB Mocha titles after import normalization", file=sys.stderr)
        raise SystemExit(127)
    runtime_titles.add(title_key)
    grouped.setdefault(test_file, []).append(selector_title)
if not grouped:
    print("missing declared Mocha titles", file=sys.stderr)
    raise SystemExit(127)
status = 0
for test_file in sorted(grouped):
    print("OPENCOLLAB_MOCHA_FILE " + json.dumps(test_file, ensure_ascii=True), flush=True)
    selector = "^(?:" + "|".join(re.escape(title) for title in grouped[test_file]) + ")$"
    local_mocha = pathlib.Path("./node_modules/.bin/mocha")
    if local_mocha.is_file() and os.access(local_mocha, os.X_OK):
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
    "NODEBB_MOCHA_FILE_MARKER",
    "nodebb_mocha_runtime_title",
    "nodebb_mocha_selector_title",
    "nodebb_mocha_target_file_command",
    "nodebb_mocha_titles_are_unambiguous",
]
