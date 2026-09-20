"""Tutanota test command and structured OTest instrumentation."""

# ruff: noqa: E501

from __future__ import annotations

import inspect
import json
import re
import shlex


def _patch_tutanota_suite(text, targets):
    text = re.sub(
        r"(?ms)^[ \t]*// OPENCOLLAB_OSPEC_BEGIN[ \t]*$.*?^[ \t]*// OPENCOLLAB_OSPEC_END[ \t]*$\n?",
        "",
        text,
    )
    legacy = list(
        re.finditer(
            r"(?m)^(?P<i>[ \t]*)(?P<s>(?:(?:const|let)\s+[A-Za-z_$][A-Za-z0-9_$]*\s*=\s*)?o\.report\s*\([^\n]*\)\s*;?)[ \t]*$",
            text,
        )
    )

    def block(indent, lines):
        return (
            "\n".join(
                indent + line
                for line in [
                    "// OPENCOLLAB_OSPEC_BEGIN",
                    *lines,
                    "// OPENCOLLAB_OSPEC_END",
                ]
            )
            + "\n"
        )

    if len(legacy) == 1:
        match = legacy[0]
        addition = block(
            match["i"],
            [
                "const opencollabTargets = " + json.dumps(targets),
                "function opencollabSource(result) {",
                "  const stacks = Array()",
                "  if (result && result.task && result.task.error) stacks.push(result.task.error.stack)",
                "  if (result && result.error) stacks.push(result.error.stack)",
                "  if (result && Array.isArray(result.errors)) {",
                "    for (const error of result.errors) stacks.push(error && error.stack ? error.stack : error)",
                "  }",
                "  for (const stack of stacks) {",
                '    for (const line of String(stack || "").split("\\n")) {',
                r'      const match = line.match(/(?:nollup-int:\/\/\/|\/test\/)([A-Za-z0-9_./-]+\.[cm]?[jt]sx?):\d+:\d+/)',
                "      if (!match) continue",
                "      const source = match[1]",
                '      if (!source.includes("node_modules/") && !source.includes("build/") && !source.includes("bootstrapTests")) return source',
                "    }",
                "  }",
                '  return ""',
                "}",
                "const opencollabResults = results.map((result) => ({",
                '  task: typeof result.task === "string" ? result.task : result.context,',
                "  context: result.context,",
                "  source: opencollabSource(result),",
                "  pass: result.pass",
                "}))",
                'console.log("OPENCOLLAB_OSPEC_RESULTS " + JSON.stringify({targets: opencollabTargets, results: opencollabResults}))',
            ],
        )
        return text[: match.end()] + "\n" + addition + text[match.end() :]
    if legacy:
        raise ValueError("legacy ospec report call is not unique")
    runs = list(
        re.finditer(
            r"(?m)^(?P<i>[ \t]*)const (?P<var>[A-Za-z_$][A-Za-z0-9_$]*) = await o\.run\([^\n]*\)[ \t]*$",
            text,
        )
    )
    prints = list(
        re.finditer(
            r"(?m)^(?P<i>[ \t]*)o\.printReport\((?P<var>[A-Za-z_$][A-Za-z0-9_$]*)\)[ \t]*$",
            text,
        )
    )
    if (
        len(runs) != 1
        or len(prints) != 1
        or runs[0]["var"] != prints[0]["var"]
        or runs[0].end() >= prints[0].start()
    ):
        raise ValueError("cannot locate a unique OTest execution and reporting call")
    run, report = runs[0], prints[0]
    before = block(
        run["i"],
        [
            "let opencollabRootResult: any = null",
            "const opencollabO: any = o",
            "const opencollabOriginalRunSpec = opencollabO.runSpec",
            "opencollabO.runSpec = async function (...args: any[]) {",
            "  const value = await opencollabOriginalRunSpec.apply(this, args)",
            "  if (Array.isArray(args[1]) && args[1].length === 0) opencollabRootResult = value",
            "  return value",
            "}",
        ],
    )
    after = block(
        report["i"],
        [
            "opencollabO.runSpec = opencollabOriginalRunSpec",
            'if (!opencollabRootResult) throw new Error("OTest root result was not captured")',
            "const opencollabResults: any[] = []",
            "function opencollabSource(result: any) {",
            "  const stacks: any[] = []",
            "  if (result && result.task && result.task.error) stacks.push(result.task.error.stack)",
            "  if (result && result.error) stacks.push(result.error.stack)",
            "  if (result && Array.isArray(result.errors)) {",
            "    for (const error of result.errors) stacks.push(error && error.stack ? error.stack : error)",
            "  }",
            "  for (const stack of stacks) {",
            '    for (const line of String(stack || "").split("\\n")) {',
            r'      const match = line.match(/(?:nollup-int:\/\/\/|\/test\/)([A-Za-z0-9_./-]+\.[cm]?[jt]sx?):\d+:\d+/)',
            "      if (!match) continue",
            "      const source = match[1]",
            '      if (!source.includes("node_modules/") && !source.includes("build/") && !source.includes("bootstrapTests")) return source',
            "    }",
            "  }",
            '  return ""',
            "}",
            "function opencollabVisit(spec: any, parents: string[]) {",
            "  const context = parents.concat(spec.name)",
            "  for (const child of spec.specResults) opencollabVisit(child, context)",
            "  for (const result of spec.testResults) {",
            '    if (!Array.isArray(result.errors) || typeof result.skipped !== "boolean") throw new Error("Unexpected OTest result")',
            "    opencollabResults.push({task: result.name, context, source: opencollabSource(result), pass: result.errors.length === 0 && !result.skipped, skipped: result.skipped})",
            "  }",
            "}",
            "opencollabVisit(opencollabRootResult, [])",
            'console.log("OPENCOLLAB_OSPEC_RESULTS " + JSON.stringify({targets: '
            + json.dumps(targets)
            + ", results: opencollabResults}))",
        ],
    )
    return (
        text[: run.start()]
        + before
        + text[run.start() : report.end()]
        + "\n"
        + after
        + text[report.end() :]
    )


def tutanota_test_command(tests):
    patcher = "import re,json\nfrom pathlib import Path\n" + inspect.getsource(
        _patch_tutanota_suite
    )
    patcher += """
patched = []
for path in sorted(Path("test").rglob("*")):
    if not path.is_file() or path.suffix not in {".js", ".mjs", ".ts"}:
        continue
    try:
        original = path.read_text(encoding="utf-8")
        instrumented = _patch_tutanota_suite(original, __OPENCOLLAB_TARGETS__)
    except (OSError, UnicodeError, ValueError):
        continue
    patched.append((path, instrumented))
if not patched:
    raise RuntimeError("cannot locate a Tutanota OTest reporter")
for path, instrumented in patched:
    path.write_text(instrumented, encoding="utf-8")
""".replace("__OPENCOLLAB_TARGETS__", repr([str(x) for x in tests]), 1)
    legacy_script = (
        "node -e "
        + shlex.quote(
            'const scripts=require("./package.json").scripts||{}; process.exit(scripts["test:app"] ? 0 : 1)'
        )
    )
    split_script = (
        "node -e "
        + shlex.quote(
            'const scripts=require("./package.json").scripts||{}; process.exit(scripts.testapi&&scripts.testclient ? 0 : 1)'
        )
    )
    runner = "\n".join(
        [
            f"if {legacy_script}; then",
            "  npm_config_nodedir=/usr/local npm run test:app",
            f"elif {split_script}; then",
            "  npm_config_nodedir=/usr/local npm test",
            "else",
            '  echo "unsupported Tutanota test scripts" >&2',
            "  exit 127",
            "fi",
        ]
    )
    return "python3 -I -c " + shlex.quote(patcher) + " &&\n" + runner


__all__ = ["_patch_tutanota_suite", "tutanota_test_command"]
