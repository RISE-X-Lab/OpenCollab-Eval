"""Pure command construction for Python test targets."""

import shlex


def normalize_python_test_target(target):
    target = str(target)
    if "[" in target and not target.endswith("]"):
        return target.split("[", 1)[0]
    return target


def python_test_command(targets, max_args=40, max_chars=12000):
    batches = []
    current = []
    current_chars = 0
    for target in targets:
        quoted = shlex.quote(target)
        if current and (len(current) >= max_args or current_chars + len(quoted) + 1 > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(target)
        current_chars += len(quoted) + 1
    if current:
        batches.append(current)
    commands = ["python3 -m pytest -vv " + " ".join(shlex.quote(target) for target in batch) for batch in batches]
    return " && ".join(commands)
