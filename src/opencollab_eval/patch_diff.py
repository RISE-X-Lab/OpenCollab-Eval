"""Small, shared primitives for parsing Git patch blocks and paths."""

from __future__ import annotations

import re

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


__all__ = [
    "decode_git_c_path",
    "diff_target_path",
    "git_header_tokens",
    "split_patch_blocks",
]
