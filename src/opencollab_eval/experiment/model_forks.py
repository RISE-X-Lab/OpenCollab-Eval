"""Every place OpenCollab or OpenCollab-Eval branches on a model identifier.

The executable form of "what does this instrument do for a model it has never
seen". Each fork below is a lookup keyed on the model name: a table row, a
family prefix, a substring, or a regex. Every one of them has a branch for
*not matching*, and that branch is silent -- it returns a constant instead of
raising. Running only ``deepseek-v4-flash``, which has a row in every table,
means none of those branches ever executed, so a code freeze proves the
instrument is reproducible and proves nothing about whether it is right for the
next model. Switching to ``qwen3.8-flash`` and ``gpt-5.6-luna`` on 2026-09-06
hit twelve of these in one day.

``arm_registry`` guards the other axis: inputs that differ *between arms* of one
batch. Its fifteen factors hold ``sampling_temperature`` and nothing else from
this family, because a fork on the model name is identical on every arm and so
is invisible to a cross-arm comparison. It is only visible against the *model*.

Two ways of reading a value, and the difference matters:

``method="source"``
    Parsed out of the file with ``ast``. This is the only way to tell an exact
    row from a fallback: at runtime ``context_window=None`` for a model with no
    row is indistinguishable from a row that says ``None``. It is also where
    ``path:line`` comes from.
``method="runtime"``
    OpenCollab's own function, executed in a child process. Eval may not import
    ``opencollab.adapters`` (``tests/test_boundaries.py``), and re-implementing
    the lookups here would create a second copy that can drift -- which is the
    defect this module exists to find. So the rules are re-implemented only to
    *attribute* a value to a source line, and every attribution is then checked
    against what OpenCollab actually returns. A disagreement is a hard error:
    it means this module's model of that code is stale and its table cannot be
    trusted.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import opencollab

import opencollab_eval
from opencollab_eval import usage as eval_usage
from opencollab_eval.experiment.model_fork_runtime import (
    DEFAULT_OVERFLOW_SAMPLES,
    DEFAULT_RETRY_SAMPLES,
    ErrorSample,
    ForkResolutionError,
    opencollab_runtime,
)

__all__ = [
    "DEFAULT_OVERFLOW_SAMPLES",
    "DEFAULT_RETRY_SAMPLES",
    "EXACT",
    "FALLBACK",
    "FAMILY",
    "GATE_CROSS_REPO",
    "GATE_FALLBACK",
    "ROW_DEFAULT",
    "RULE",
    "ErrorSample",
    "Finding",
    "ForkResolutionError",
    "OpenCollabSource",
    "resolve",
]

# How a value was reached.
EXACT = "exact"  # a row keyed on this exact model id (or a dated spelling of it)
FAMILY = "family"  # a family/substring table matched; no row for this model
ROW_DEFAULT = "row-default"  # the model has a row, but this field is not in it
RULE = "rule"  # a regex or predicate branch; no table row is involved
DERIVED = "derived"  # computed from another resolved quantity
FALLBACK = "fallback"  # nothing matched; a module constant was used

# Which gate a row can fail. ``None`` means the row is reported but never fails.
GATE_FALLBACK = "fallback"
GATE_CROSS_REPO = "cross-repo"

OPENCOLLAB_PACKAGE = Path(opencollab.__file__).resolve().parent
EVAL_PACKAGE = Path(opencollab_eval.__file__).resolve().parent


@dataclass(frozen=True)
class Finding:
    """One resolved quantity, where it came from, and how it got there."""

    quantity: str
    side: str  # "opencollab" | "eval" | "cross-repo"
    source: str  # "path:line"
    value: Any
    hit: str
    method: str  # "source" | "runtime"
    fallback_value: Any = None  # what the default is, when ``hit`` is a default
    gate: str | None = None
    note: str = ""


#: Repository roots, so a source reference reads ``OpenCollab/...`` rather than
#: an absolute path nobody can paste into a review comment. Eval is installed
#: from ``src/``, so its repository root is one level above its package parent.
_REPOSITORY_ROOTS = (OPENCOLLAB_PACKAGE.parent, EVAL_PACKAGE.parent.parent)


def _display(path: Path) -> str:
    """``OpenCollab/opencollab/adapters/llm/types.py``, not an 80-column path."""
    for root in _REPOSITORY_ROOTS:
        try:
            return f"{root.name}/{path.resolve().relative_to(root).as_posix()}"
        except ValueError:
            continue
    return str(path)


def _ref(path: Path, line: int) -> str:
    return f"{_display(path)}:{line}"


def _parse(path: Path) -> ast.Module:
    if not path.is_file():
        raise ForkResolutionError(f"expected to read a model fork out of {path}, which does not exist")
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_assign(tree: ast.Module, name: str) -> ast.stmt:
    for node in tree.body:
        target: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            return node
    raise ForkResolutionError(f"{name} is no longer a module-level assignment")


def _assigned_value(tree: ast.Module, name: str) -> ast.expr:
    node = _module_assign(tree, name)
    value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
    if value is None:
        raise ForkResolutionError(f"{name} is declared without a value")
    return value


def _literal_dict(node: ast.expr, name: str) -> dict[str, tuple[Any, int]]:
    if not isinstance(node, ast.Dict):
        raise ForkResolutionError(f"{name} is no longer a dict literal")
    out: dict[str, tuple[Any, int]] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            raise ForkResolutionError(f"{name} has a non-literal key")
        out[key.value] = (ast.literal_eval(value), value.lineno)
    return out


def _string_tuple(node: ast.expr, name: str) -> tuple[tuple[str, int], ...]:
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        raise ForkResolutionError(f"{name} is no longer a literal sequence")
    return tuple((ast.literal_eval(item), item.lineno) for item in node.elts)


class OpenCollabSource:
    """The model-keyed tables and rules, read out of an OpenCollab checkout."""

    def __init__(self, package_root: Path | None = None) -> None:
        self.root = (package_root or OPENCOLLAB_PACKAGE).resolve()
        self.types_path = self.root / "adapters" / "llm" / "types.py"
        self.pipeline_path = self.root / "application" / "shaping" / "pipeline.py"
        self.provider_path = self.root / "adapters" / "llm" / "openai_provider.py"
        self.errors_path = self.root / "adapters" / "llm" / "errors.py"
        self.retry_path = self.root / "adapters" / "llm" / "retry.py"
        self.ledger_path = self.root / "adapters" / "llm" / "usage_ledger.py"
        self._types = _parse(self.types_path)

    # -- opencollab/adapters/llm/types.py ---------------------------------
    def capability_defaults(self) -> dict[str, tuple[Any, int]]:
        """``ModelCapabilities``' dataclass fields, in declaration order."""
        for node in self._types.body:
            if isinstance(node, ast.ClassDef) and node.name == "ModelCapabilities":
                fields: dict[str, tuple[Any, int]] = {}
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        if stmt.value is None:
                            raise ForkResolutionError(f"ModelCapabilities.{stmt.target.id} has no default")
                        fields[stmt.target.id] = (ast.literal_eval(stmt.value), stmt.value.lineno)
                if not fields:
                    raise ForkResolutionError("ModelCapabilities declares no fields")
                return fields
        raise ForkResolutionError("ModelCapabilities is no longer a class in types.py")

    def exact_capabilities(self) -> dict[str, dict[str, tuple[Any, int]]]:
        """``_EXACT_MODEL_CAPABILITIES`` as ``{model: {field: (value, line)}}``.

        Only the keywords a row actually spells out. A field a row leaves out is
        a ``row-default``, and telling the two apart is the whole point.
        """
        table = _assigned_value(self._types, "_EXACT_MODEL_CAPABILITIES")
        if not isinstance(table, ast.Dict):
            raise ForkResolutionError("_EXACT_MODEL_CAPABILITIES is no longer a dict literal")
        rows: dict[str, dict[str, tuple[Any, int]]] = {}
        for key, value in zip(table.keys, table.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                raise ForkResolutionError("_EXACT_MODEL_CAPABILITIES has a non-literal key")
            if not isinstance(value, ast.Call):
                raise ForkResolutionError(f"{key.value}: expected a ModelCapabilities(...) call")
            row: dict[str, tuple[Any, int]] = {}
            for keyword in value.keywords:
                if keyword.arg is None:
                    raise ForkResolutionError(f"{key.value}: ModelCapabilities(**kwargs) cannot be read statically")
                row[keyword.arg] = (ast.literal_eval(keyword.value), keyword.value.lineno)
            rows[key.value] = row
        return rows

    def family_windows(self) -> dict[str, tuple[int, int]]:
        return _literal_dict(_assigned_value(self._types, "MODEL_CONTEXT_WINDOWS"), "MODEL_CONTEXT_WINDOWS")

    def bounded_families(self) -> frozenset[str]:
        node = _assigned_value(self._types, "_BOUNDED_CAPABILITY_FAMILIES")
        if isinstance(node, ast.Call) and node.args:
            return frozenset(ast.literal_eval(node.args[0]))
        return frozenset(ast.literal_eval(node))

    # -- the other four files ---------------------------------------------
    def line_of(self, path: Path, name: str) -> int:
        return _module_assign(_parse(path), name).lineno

    def overflow_fragments(self) -> tuple[tuple[str, int], ...]:
        tree = _parse(self.errors_path)
        return _string_tuple(_assigned_value(tree, "_OVERFLOW_MESSAGE_FRAGMENTS"), "_OVERFLOW_MESSAGE_FRAGMENTS")

    def reasoning_regex(self) -> tuple[str, int]:
        tree = _parse(self.provider_path)
        node = _assigned_value(tree, "_OPENAI_REASONING_MODEL_RE")
        if not (isinstance(node, ast.Call) and node.args):
            raise ForkResolutionError("_OPENAI_REASONING_MODEL_RE is no longer re.compile(<literal>)")
        return ast.literal_eval(node.args[0]), node.lineno


# ---------------------------------------------------------------------------
# The lookup rules, re-implemented for attribution only (see module docstring).
# ---------------------------------------------------------------------------


def _leaf(model: str) -> str:
    return model.strip().lower().rsplit("/", 1)[-1]


def _canonical(model: str, exact: dict[str, Any]) -> str:
    """``types._canonical_model_id``: a row id, or a dated spelling of one."""
    import re

    leaf = _leaf(model)
    if leaf in exact:
        return leaf
    for known in exact:
        if re.fullmatch(rf"{re.escape(known)}-\d{{4}}(?:-\d{{2}}){{0,2}}", leaf):
            return known
    return leaf


def _matches_family(model: str, family: str, exact: dict[str, Any], bounded: frozenset[str]) -> bool:
    """``types.model_matches_family``."""
    leaf = _leaf(model)
    normalized_family = family.strip().lower()
    if normalized_family in bounded:
        return _canonical(leaf, exact) == normalized_family
    return leaf == normalized_family or leaf.startswith(f"{normalized_family}-")


# ---------------------------------------------------------------------------
# Runtime probe: OpenCollab's own answers, executed out of process.
# ---------------------------------------------------------------------------

def eval_context_window_source(model: str) -> tuple[Any, str, str]:
    """Eval's recorded window, plus how its own three rules reached it."""
    import re

    tree = _parse(Path(eval_usage.__file__))
    exact = _literal_dict(_assigned_value(tree, "EXACT_MODEL_CONTEXT_WINDOWS"), "EXACT_MODEL_CONTEXT_WINDOWS")
    family = _literal_dict(_assigned_value(tree, "MODEL_CONTEXT_WINDOWS"), "MODEL_CONTEXT_WINDOWS")
    path = Path(eval_usage.__file__)
    leaf = _leaf(model)
    if leaf in exact:
        return exact[leaf][0], EXACT, _ref(path, exact[leaf][1])
    for key, (window, line) in exact.items():
        if re.fullmatch(rf"{re.escape(key)}-\d{{4}}(?:-\d{{2}}){{0,2}}", leaf):
            return window, EXACT, _ref(path, line)
    # usage.py:103 matches a family by *substring*, not by prefix: "gpt-4" hits
    # anything containing it. Recorded as FAMILY so it can never read as exact.
    for key, (window, line) in family.items():
        if key in leaf:
            return window, FAMILY, _ref(path, line)
    return None, FALLBACK, _ref(path, _module_assign(tree, "MODEL_CONTEXT_WINDOWS").lineno)


def shared_table_disagreements(source: OpenCollabSource) -> dict[str, tuple[Any, Any]]:
    """Rows both repositories carry, where the two copies say different things.

    A row Eval omits is fine -- Eval only needs the models it records. A row
    both carry with different values is the recorded window disagreeing with the
    enforced one, for whichever model runs next.
    """
    tree = _parse(Path(eval_usage.__file__))
    eval_exact = {k: v[0] for k, v in _literal_dict(
        _assigned_value(tree, "EXACT_MODEL_CONTEXT_WINDOWS"), "EXACT_MODEL_CONTEXT_WINDOWS"
    ).items()}
    eval_family = {k: v[0] for k, v in _literal_dict(
        _assigned_value(tree, "MODEL_CONTEXT_WINDOWS"), "MODEL_CONTEXT_WINDOWS"
    ).items()}
    oc_exact = {
        model: row["context_window"][0]
        for model, row in source.exact_capabilities().items()
        if "context_window" in row
    }
    oc_family = {k: v[0] for k, v in source.family_windows().items()}
    out: dict[str, tuple[Any, Any]] = {}
    for name, theirs, ours in (("exact", oc_exact, eval_exact), ("family", oc_family, eval_family)):
        for key in sorted(set(theirs) & set(ours)):
            if theirs[key] != ours[key]:
                out[f"{name}:{key}"] = (theirs[key], ours[key])
    return out


# ---------------------------------------------------------------------------
# Resolution: one row per fork, for one model.
# ---------------------------------------------------------------------------


def _capability_findings(
    model: str,
    source: OpenCollabSource,
    runtime: dict[str, Any],
) -> list[Finding]:
    exact = source.exact_capabilities()
    defaults = source.capability_defaults()
    families = source.family_windows()
    bounded = source.bounded_families()
    types_path = source.types_path
    canonical = _canonical(model, exact)
    row = exact.get(canonical)

    findings: list[Finding] = []
    for field_name, (default_value, default_line) in defaults.items():
        if row is not None and field_name in row:
            value, line = row[field_name]
            findings.append(
                Finding(
                    quantity=f"capabilities.{field_name}",
                    side="opencollab",
                    source=_ref(types_path, line),
                    value=value,
                    hit=EXACT,
                    method="source",
                    gate=GATE_FALLBACK,
                    note=f"_EXACT_MODEL_CAPABILITIES[{canonical!r}]",
                )
            )
        elif row is not None:
            findings.append(
                Finding(
                    quantity=f"capabilities.{field_name}",
                    side="opencollab",
                    source=_ref(types_path, default_line),
                    value=default_value,
                    hit=ROW_DEFAULT,
                    method="source",
                    fallback_value=default_value,
                    gate=None,
                    note=f"row {canonical!r} exists but does not set this field",
                )
            )
        elif field_name == "context_window":
            window = None
            line = _module_assign(source._types, "MODEL_CONTEXT_WINDOWS").lineno
            hit = FALLBACK
            note = "no row, and no family matched"
            for family, (candidate, family_line) in families.items():
                if _matches_family(model, family, exact, bounded):
                    window, line, hit = candidate, family_line, FAMILY
                    note = f"MODEL_CONTEXT_WINDOWS[{family!r}] -- a family, not this model"
                    break
            findings.append(
                Finding(
                    quantity="capabilities.context_window",
                    side="opencollab",
                    source=_ref(types_path, line),
                    value=window,
                    hit=hit,
                    method="source",
                    fallback_value=window if hit == FAMILY else None,
                    gate=GATE_FALLBACK,
                    note=note,
                )
            )
        else:
            # No row: ``model_capabilities`` builds a fresh ModelCapabilities.
            # Two of its fields are decided by a regex on the model *name*
            # (``_is_responses_reasoning_family`` and its sampling twin), which
            # is a rule rather than a default -- a gateway alias such as
            # ``gpt-5.6-luna`` is classified as an OpenAI GPT-5 by its spelling
            # alone. The rest are dataclass defaults nobody chose for this
            # model. ``capabilities.row`` below carries the fact that no row
            # exists, so these two do not have to overstate their own case.
            value = runtime["capabilities"][field_name]
            by_regex = field_name in {"supports_responses_reasoning", "supports_responses_sampling"}
            findings.append(
                Finding(
                    quantity=f"capabilities.{field_name}",
                    side="opencollab",
                    source=_ref(types_path, default_line),
                    value=value,
                    hit=RULE if by_regex else FALLBACK,
                    method="source",
                    fallback_value=default_value,
                    gate=None if by_regex else GATE_FALLBACK,
                    note=(
                        "decided by a regex on the model name, not by a row"
                        if by_regex else "no row for this model"
                    ),
                )
            )

    findings.insert(
        0,
        Finding(
            quantity="capabilities.row",
            side="opencollab",
            source=_ref(types_path, _module_assign(source._types, "_EXACT_MODEL_CAPABILITIES").lineno),
            value=canonical if row is not None else None,
            hit=EXACT if row is not None else FALLBACK,
            method="source",
            fallback_value=None,
            gate=GATE_FALLBACK,
            note=(
                f"_EXACT_MODEL_CAPABILITIES has a row for {canonical!r}"
                if row is not None
                else "no row: every capability below is whatever an unlisted model gets"
            ),
        ),
    )
    return findings


def _check_runtime_agreement(findings: list[Finding], runtime: dict[str, Any]) -> None:
    """Positive control: what this module attributes must be what OpenCollab does.

    Without it a rule that drifted here would produce a table that is confident
    and wrong, which is worse than no table. Any disagreement stops the audit.
    """
    disagreements = []
    for finding in findings:
        if finding.side != "opencollab" or not finding.quantity.startswith("capabilities."):
            continue
        field_name = finding.quantity.split(".", 1)[1]
        if field_name == "row":
            continue
        if field_name in runtime["capabilities"] and runtime["capabilities"][field_name] != finding.value:
            disagreements.append(f"{finding.quantity}: source says {finding.value!r}, runtime says "
                                 f"{runtime['capabilities'][field_name]!r}")
    missing = set(runtime["capabilities"]) - {
        f.quantity.split(".", 1)[1] for f in findings if f.quantity.startswith("capabilities.")
    } - {"row"}
    if missing:
        disagreements.append(f"ModelCapabilities gained field(s) this reader does not resolve: {sorted(missing)}")
    if disagreements:
        raise ForkResolutionError(
            "this module's copy of OpenCollab's lookup rules no longer matches OpenCollab:\n  "
            + "\n  ".join(disagreements)
        )


def resolve(
    model: str,
    *,
    source: OpenCollabSource | None = None,
    overflow_samples: tuple[ErrorSample, ...] = DEFAULT_OVERFLOW_SAMPLES,
    retry_samples: tuple[ErrorSample, ...] = DEFAULT_RETRY_SAMPLES,
) -> list[Finding]:
    """Every model-keyed fork, resolved for ``model``, in reading order."""
    source = source or OpenCollabSource()
    runtime = opencollab_runtime(model, overflow_samples=overflow_samples, retry_samples=retry_samples)

    findings = _capability_findings(model, source, runtime)
    if Path(runtime["types_file"]).resolve() == source.types_path.resolve():
        _check_runtime_agreement(findings, runtime)

    # The window used for the cross-repository comparison is the one attributed
    # to a source line, not the one the child process returned. The two are the
    # same whenever the audited checkout is the imported one -- which
    # ``_check_runtime_agreement`` has just asserted -- and when a caller points
    # this at another checkout, the file it was pointed at is the thing being
    # compared.
    window = next(
        f.value for f in findings
        if f.side == "opencollab" and f.quantity == "capabilities.context_window"
    )
    scaled = runtime["history"]["from"] == "context_window"
    trigger_line = source.line_of(source.pipeline_path, "DEFAULT_HISTORY_TRIGGER_TOKENS")
    target_line = source.line_of(source.pipeline_path, "DEFAULT_HISTORY_TARGET_TOKENS")
    for name, line in (("trigger", trigger_line), ("target", target_line)):
        findings.append(
            Finding(
                quantity=f"history.{name}",
                side="opencollab",
                source=_ref(source.pipeline_path, line),
                value=runtime["history"][name],
                hit=DERIVED if scaled else FALLBACK,
                method="runtime",
                fallback_value=None if scaled else runtime["history"][name],
                gate=GATE_FALLBACK,
                note=(
                    f"scaled from a {window:,}-token window" if scaled and isinstance(window, int)
                    else "fixed default: this model has no window to scale from"
                ),
            )
        )

    regex, regex_line = source.reasoning_regex()
    reasoning = runtime["reasoning_request_fields"]
    findings.append(
        Finding(
            quantity="request.reasoning_fields",
            side="opencollab",
            source=_ref(source.provider_path, regex_line),
            value=reasoning,
            hit=RULE,
            method="runtime",
            note=f"_OPENAI_REASONING_MODEL_RE = {regex!r}",
        )
    )
    for quantity, key, note in (
        ("request.max_output_token_field", "max_output_token_field", "which field carries the output ceiling"),
        ("request.sends_temperature", "sends_temperature", "a reasoning model is sent no temperature at all"),
        ("request.sends_top_p", "sends_top_p", "a reasoning model is sent no top_p at all"),
    ):
        findings.append(
            Finding(
                quantity=quantity,
                side="opencollab",
                source=_ref(source.provider_path, regex_line),
                value=runtime[key],
                hit=RULE,
                method="runtime",
                note=note,
            )
        )
    forced_ok = runtime["capabilities"]["supports_forced_tool_choice"]
    findings.append(
        Finding(
            quantity="request.forced_tool_choice_downgraded",
            side="opencollab",
            source=_ref(source.provider_path, regex_line),
            value=not forced_ok,
            hit=DERIVED,
            method="runtime",
            note=(
                "a required/named tool_choice is silently rewritten to 'auto'"
                if not forced_ok else "required/named tool_choice is sent as asked"
            ),
        )
    )
    findings.append(
        Finding(
            quantity="pricing.mode",
            side="opencollab",
            source=_ref(source.ledger_path, source.line_of(source.ledger_path, "DEFAULT_GLM52_INPUT_USD_PER_MTOK")),
            value=runtime["pricing_mode"],
            hit=EXACT if runtime["pricing_mode"] != "unset" else FALLBACK,
            method="runtime",
            fallback_value="unset",
            gate=None,
            note="'unset' prices every token at 0.00 USD unless the env carries a rate",
        )
    )

    fragments_line = source.line_of(source.errors_path, "_OVERFLOW_MESSAGE_FRAGMENTS")
    for sample in overflow_samples:
        findings.append(
            Finding(
                quantity=f"overflow.{sample.label}",
                side="opencollab",
                source=_ref(source.errors_path, fragments_line),
                value=runtime["overflow"][sample.label],
                hit=RULE,
                method="runtime",
                note=sample.message[:70],
            )
        )
    status_line = source.line_of(source.retry_path, "RETRYABLE_STATUS_CODES")
    for sample in retry_samples:
        findings.append(
            Finding(
                quantity=f"retry.{sample.label}",
                side="opencollab",
                source=_ref(source.retry_path, status_line),
                value=runtime["retry"][sample.label],
                hit=RULE,
                method="runtime",
                note=f"{sample.class_name}, status={sample.status}",
            )
        )

    eval_window, eval_hit, eval_ref = eval_context_window_source(model)
    if eval_window != eval_usage.model_context_window(model):
        raise ForkResolutionError(
            f"this module's copy of Eval's lookup rule disagrees with usage.model_context_window: "
            f"{eval_window!r} vs {eval_usage.model_context_window(model)!r}"
        )
    findings.append(
        Finding(
            quantity="capabilities.context_window",
            side="eval",
            source=eval_ref,
            value=eval_window,
            hit=eval_hit,
            method="source",
            gate=GATE_FALLBACK,
            note="the window written into metrics.jsonl as what the run had",
        )
    )
    eval_mode = eval_usage.pricing_for_model(model)["mode"]
    findings.append(
        Finding(
            quantity="pricing.mode",
            side="eval",
            source=_ref(Path(eval_usage.__file__), _module_assign(
                _parse(Path(eval_usage.__file__)), "DEFAULT_GLM52_INPUT_USD_PER_MTOK").lineno),
            value=eval_mode,
            hit=EXACT if eval_mode != "unset" else FALLBACK,
            method="runtime",
            fallback_value="unset",
            gate=None,
            note="Eval matches 'glm' as a substring; OpenCollab matches the glm-5.2 family",
        )
    )

    for quantity, ours, theirs in (
        ("capabilities.context_window", window, eval_window),
        ("pricing.mode", runtime["pricing_mode"], eval_mode),
    ):
        agree = ours == theirs
        findings.append(
            Finding(
                quantity=quantity,
                side="cross-repo",
                source="OpenCollab vs OpenCollab-Eval",
                value=f"oc={ours!r} eval={theirs!r}",
                hit="agree" if agree else "DISAGREE",
                method="source",
                gate=GATE_CROSS_REPO,
                note="" if agree else "the recorded value is not the value the runtime enforced",
            )
        )
    drift = shared_table_disagreements(source)
    findings.append(
        Finding(
            quantity="tables.shared_rows",
            side="cross-repo",
            source="OpenCollab vs OpenCollab-Eval",
            value=f"{len(drift)} disagreeing row(s)" + (f": {drift}" if drift else ""),
            hit="agree" if not drift else "DISAGREE",
            method="source",
            gate=GATE_CROSS_REPO,
            note="rows both repositories carry; a row only one carries is fine",
        )
    )
    return findings
