"""Extract bounded Go module additions from an evaluated candidate patch."""

from __future__ import annotations

import re

from opencollab_eval.patch_diff import patch_block_target_path, split_patch_blocks

_MODULE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~+/\-]*\Z")
_VERSION_RE = re.compile(r"v[^\s]+\Z")
_REQUIRE_LINE_RE = re.compile(
    r"\s*(?:(?:require)\s+)?(?P<module>[A-Za-z0-9][A-Za-z0-9._~+/\-]*)"
    r"\s+(?P<version>v[^\s]+)(?:\s+//\s*indirect)?\s*\Z"
)


def candidate_added_go_modules(model_patch: str) -> list[dict[str, str]]:
    """Return exact module versions added to go.mod require declarations."""
    added: list[tuple[str, str]] = []
    prior: set[tuple[str, str]] = set()
    for block in split_patch_blocks(str(model_patch or "")):
        if patch_block_target_path(block) != "go.mod":
            continue
        old_section = ""
        new_section = ""
        for raw_line in block:
            if raw_line.startswith(("+++", "---", "@@")) or not raw_line:
                continue
            marker = raw_line[0]
            if marker not in {" ", "+", "-"}:
                continue
            content = raw_line[1:].strip()
            directive = re.fullmatch(r"(require|exclude|replace|retract)\s*\(", content)
            if directive:
                if marker in {" ", "-"}:
                    old_section = directive.group(1)
                if marker in {" ", "+"}:
                    new_section = directive.group(1)
                continue
            if content == ")":
                if marker in {" ", "-"}:
                    old_section = ""
                if marker in {" ", "+"}:
                    new_section = ""
                continue
            match = _REQUIRE_LINE_RE.fullmatch(content)
            section = new_section if marker == "+" else old_section
            if match is None or section not in {"", "require"}:
                continue
            if section == "" and not content.startswith("require "):
                continue
            item = (match.group("module"), match.group("version"))
            if marker == "+" and item not in added:
                added.append(item)
            elif marker in {" ", "-"}:
                prior.add(item)
    modules = [
        {"module": module, "version": version}
        for module, version in added
        if (module, version) not in prior
    ]
    if len(modules) > 256 or sum(
        len(item["module"].encode()) + len(item["version"].encode()) for item in modules
    ) > 64 * 1024:
        return []
    return modules


def valid_candidate_added_go_modules(value: object) -> bool:
    """Validate the bounded proof representation used by persisted test plans."""
    if not isinstance(value, list) or not value or len(value) > 256:
        return False
    normalized: list[tuple[str, str]] = []
    byte_count = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"module", "version"}:
            return False
        module = item.get("module")
        version = item.get("version")
        if (
            not isinstance(module, str)
            or _MODULE_RE.fullmatch(module) is None
            or not isinstance(version, str)
            or _VERSION_RE.fullmatch(version) is None
        ):
            return False
        normalized.append((module, version))
        byte_count += len(module.encode()) + len(version.encode())
    return len(set(normalized)) == len(normalized) and byte_count <= 64 * 1024


__all__ = ["candidate_added_go_modules", "valid_candidate_added_go_modules"]
