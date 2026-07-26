"""Small, shared primitives for parsing Git patch blocks and paths."""

from __future__ import annotations

import re

from .patch_paths import is_generated_runtime_artifact_path

GIT_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def decode_git_c_path(value: str) -> str:
    value = str(value or "")
    quoted = value.startswith('"')
    index = 1 if quoted else 0
    decoded = bytearray()
    while index < len(value):
        char = value[index]
        if quoted and char == '"':
            break
        if char != "\\":
            decoded.extend(char.encode("utf-8", errors="surrogatepass"))
            index += 1
            continue
        index += 1
        if index >= len(value):
            decoded.append(ord("\\"))
            break
        escaped = value[index]
        if escaped in "01234567":
            end = index
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue
        decoded.append(GIT_C_ESCAPES.get(escaped, ord(escaped)))
        index += 1
    return decoded.decode("utf-8", errors="surrogateescape")


def git_header_tokens(header: str) -> list[str]:
    text = str(header or "").strip()
    prefix = "diff --git "
    if not text.startswith(prefix):
        return []
    text = text[len(prefix) :]
    tokens = []
    index = 0
    while index < len(text) and len(tokens) < 2:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        start = index
        if text[index] == '"':
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
        else:
            while index < len(text) and not text[index].isspace():
                index += 1
        tokens.append(text[start:index])
    return tokens


def diff_target_path(header: str) -> str:
    match = re.match(r"^diff --git a/(.*) b/(.*)$", str(header or "").strip())
    if match:
        return match.group(2)
    paths = git_header_tokens(header)
    if len(paths) >= 2:
        target = decode_git_c_path(paths[1])
        if target.startswith("b/"):
            return target[2:]
    if paths:
        source = decode_git_c_path(paths[0])
        if source.startswith("a/"):
            return source[2:]
    return ""


def git_diff_endpoint(token: str, side: str) -> str:
    path = decode_git_c_path(token)
    if path == "/dev/null":
        return ""
    prefix = f"{side}/"
    if path.startswith(prefix):
        path = path[len(prefix) :]
    return path


def patch_entries(patch: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for block in split_patch_blocks(patch):
        if not block or not block[0].startswith("diff --git "):
            continue
        old_path = new_path = ""
        for line in block[1:]:
            line = line.rstrip("\r\n")
            if line.startswith("--- "):
                old_path = git_diff_endpoint(line[4:].removesuffix("\t"), "a")
            elif line.startswith("+++ "):
                new_path = git_diff_endpoint(line[4:].removesuffix("\t"), "b")
            elif line.startswith(("rename from ", "copy from ")):
                old_path = decode_git_c_path(line.split(" ", 2)[2])
            elif line.startswith(("rename to ", "copy to ")):
                new_path = decode_git_c_path(line.split(" ", 2)[2])
            elif line.startswith(("@@", "GIT binary patch")):
                break
        if not old_path and not new_path:
            match = re.match(r"^diff --git a/(.*) b/(.*)$", block[0].strip())
            if match:
                old_path, new_path = match.groups()
            else:
                tokens = git_header_tokens(block[0])
                if len(tokens) >= 2:
                    old_path = git_diff_endpoint(tokens[0], "a")
                    new_path = git_diff_endpoint(tokens[1], "b")
        if old_path or new_path:
            entries.append((old_path, new_path))
    return entries


def patch_paths(patch: str) -> list[str]:
    paths: dict[str, None] = {}
    for old_path, new_path in patch_entries(patch):
        for path in (old_path, new_path):
            if path:
                paths.setdefault(path, None)
    return list(paths)


def patch_block_target_path(block: list[str]) -> str:
    entries = patch_entries("".join(block))
    if len(entries) != 1:
        return ""
    old_path, new_path = entries[0]
    return new_path or old_path


def normalize_patch_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("/")


def split_patch_blocks(patch: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in str(patch or "").splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


EVAL_TEST_DIRECTORY_NAMES = frozenset(
    {"test", "tests", "testing", "__tests__", "spec", "specs"}
)


def is_eval_test_path(path: str) -> bool:
    normalized = normalize_patch_path(path)
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized
    if any(part in EVAL_TEST_DIRECTORY_NAMES for part in parts):
        return True
    return (
        name == "conftest.py"
        or name.endswith("_test.go")
        or name == "test.py"
        or name.startswith("test_")
        and name.endswith(".py")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def is_eval_control_path(path: str) -> bool:
    return normalize_patch_path(path).rsplit("/", 1)[-1] == "conftest.py"


def filter_patch_paths_with_evidence(
    patch: str,
    excluded_paths: set[str],
) -> tuple[str, list[str]]:
    excluded = {normalize_patch_path(path) for path in excluded_paths if path}
    if not excluded:
        return patch, []
    kept: list[str] = []
    removed: dict[str, None] = {}
    for block in split_patch_blocks(patch):
        text = "".join(block)
        entries = patch_entries(text)
        if not text.strip():
            kept.extend(block)
            continue
        if len(entries) != 1:
            raise RuntimeError("candidate patch block could not be classified safely")
        endpoints = [normalize_patch_path(path) for path in entries[0] if path]
        if any(path in excluded for path in endpoints):
            for path in endpoints:
                removed.setdefault(path, None)
        else:
            kept.extend(block)
    return "".join(kept), list(removed)


def filter_model_patch_with_evidence(patch: str) -> tuple[str, list[str]]:
    excluded = {
        path
        for path in patch_paths(patch)
        if is_eval_test_path(path) and not is_eval_control_path(path)
    }
    return filter_patch_paths_with_evidence(patch, excluded)


def filter_model_patch_for_eval(patch: str) -> str:
    filtered, _evidence = filter_model_patch_with_evidence(patch)
    return filtered


def remove_generated_artifact_blocks(
    patch: str,
    paths: set[str],
) -> tuple[str, list[str]]:
    normalized_paths = {normalize_patch_path(path) for path in paths}
    if not normalized_paths:
        return patch, []

    kept: list[str] = []
    removed: dict[str, None] = {}
    for lines in split_patch_blocks(patch):
        entries = patch_entries("".join(lines))
        if len(entries) != 1:
            raise RuntimeError("trusted patch block could not be classified safely")
        endpoints = [path for path in entries[0] if path]
        intersects = any(path in normalized_paths for path in endpoints)
        if not intersects:
            kept.extend(lines)
            continue
        if any(
            path not in normalized_paths
            or not is_generated_runtime_artifact_path(path)
            for path in endpoints
        ):
            raise RuntimeError(
                "trusted patch artifact filtering encountered a mixed-path entry"
            )
        for path in endpoints:
            removed.setdefault(path, None)
    if set(removed) != normalized_paths:
        raise RuntimeError("trusted patch artifact filtering was incomplete")
    return "".join(kept), list(removed)


__all__ = [
    "decode_git_c_path",
    "diff_target_path",
    "git_diff_endpoint",
    "git_header_tokens",
    "filter_model_patch_for_eval",
    "filter_model_patch_with_evidence",
    "filter_patch_paths_with_evidence",
    "is_eval_control_path",
    "is_eval_test_path",
    "normalize_patch_path",
    "patch_entries",
    "patch_block_target_path",
    "patch_paths",
    "remove_generated_artifact_blocks",
    "split_patch_blocks",
]
