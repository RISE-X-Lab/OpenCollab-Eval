"""Parse and remove Gitlink deletion blocks after trusted tree verification."""

from __future__ import annotations

import re
from typing import Any

from opencollab_eval.patch_diff import decode_git_c_path, diff_target_path, split_patch_blocks

_OBJECT_ID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
_INDEX_RE = re.compile(rf"^index (?P<old>{_OBJECT_ID})\.\.(?P<new>0{{40}}|0{{64}})$")
_SUBPROJECT_RE = re.compile(rf"^-Subproject commit (?P<old>{_OBJECT_ID})$")
_LS_TREE_OBJECT_RE = re.compile(rf"{_OBJECT_ID}\Z")


def _marker_path(line: str, marker: str) -> str:
    value = str(line or "").rstrip("\r\n")
    prefix = marker + " "
    if not value.startswith(prefix):
        return ""
    token = value[len(prefix) :]
    if token == "/dev/null":
        return token
    decoded = decode_git_c_path(token)
    expected = "a/" if marker == "---" else "b/"
    return decoded[len(expected) :] if decoded.startswith(expected) else ""


def gitlink_deletion_candidates(patch: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for block_index, block in enumerate(split_patch_blocks(patch)):
        if not block or not any(line.rstrip("\r\n") == "deleted file mode 160000" for line in block):
            continue
        path = diff_target_path(block[0])
        indexes = [
            match
            for line in block
            if (match := _INDEX_RE.fullmatch(line.rstrip("\r\n"))) is not None
        ]
        old_paths = [_marker_path(line, "---") for line in block if line.startswith("--- ")]
        new_paths = [_marker_path(line, "+++") for line in block if line.startswith("+++ ")]
        commits = [
            match.group("old").lower()
            for line in block
            if (match := _SUBPROJECT_RE.fullmatch(line.rstrip("\r\n"))) is not None
        ]
        if (
            not path
            or "\x00" in path
            or len(indexes) != 1
            or old_paths != [path]
            or new_paths != ["/dev/null"]
            or commits != [indexes[0].group("old").lower()]
        ):
            continue
        candidates.append(
            {
                "block_index": block_index,
                "path": path,
                "old_oid": indexes[0].group("old").lower(),
            }
        )
    return candidates


def parse_ls_tree_entries(output: str) -> dict[str, dict[str, str]]:
    text = str(output or "")
    if text and not text.endswith("\0"):
        raise ValueError("unterminated git ls-tree output")
    entries: dict[str, dict[str, str]] = {}
    for record in text.split("\0"):
        if not record:
            continue
        metadata, separator, path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or not path or path in entries:
            raise ValueError("malformed or duplicate git ls-tree record")
        mode, object_type, object_id = fields
        if re.fullmatch(r"[0-7]{6}", mode) is None or _LS_TREE_OBJECT_RE.fullmatch(object_id) is None:
            raise ValueError("invalid git ls-tree metadata")
        entries[path] = {
            "base_mode": mode,
            "base_type": object_type,
            "base_oid": object_id.lower(),
        }
    return entries


def filter_verified_gitlink_deletions(
    patch: str,
    verified: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    by_block = {
        int(item["block_index"]): item
        for item in verified
        if item.get("probe_status") == "verified"
    }
    kept: list[str] = []
    evidence: list[dict[str, Any]] = []
    for block_index, block in enumerate(split_patch_blocks(patch)):
        item = by_block.get(block_index)
        if item is None:
            kept.extend(block)
            continue
        evidence.append(
            {
                "path": item["path"],
                "reason": "missing_snapshot_gitlink",
                "old_oid": item["old_oid"],
                "base_oid": item["base_oid"],
                "probe_status": item["probe_status"],
            }
        )
    return "".join(kept), evidence


__all__ = [
    "filter_verified_gitlink_deletions",
    "gitlink_deletion_candidates",
    "parse_ls_tree_entries",
]
