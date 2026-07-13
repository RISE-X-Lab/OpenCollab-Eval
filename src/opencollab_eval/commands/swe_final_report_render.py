"""Render one validated SWE comparison model into Markdown and LaTeX."""

from __future__ import annotations

import re
from string import Template
from typing import Any

from opencollab_eval.commands.swe_final_report_model import FinalReportInputError

DEFAULT_LABELS = {
    "title": "$method_a and $method_b Final Evaluation Comparison",
    "date": "Date",
    "author": "Author",
    "final_results": "Final Results",
    "method": "Method",
    "resolved": "resolved",
    "unresolved": "unresolved",
    "technical": "technical",
    "terminal": "Terminal Coverage",
    "comparison": "Comparison",
    "common_resolved": "Common resolved",
    "only_resolved": "Resolved only by $method",
    "neither_resolved": "Resolved by neither method",
    "segmented_results": "Segmented Results",
    "range": "Range",
    "integrity": "Integrity Evidence",
    "fact_report": "Fact report",
    "audit_manifest": "Audit manifest",
    "runtime": "Runtime commits",
    "evidence_files": "Verified evidence files",
    "curated_notes": "Curated Task Notes",
    "all_tasks": "All 100 Task Verdicts",
    "task_index": "Index",
}

_FACTUAL_SUBTITLE = "SWE-bench Pro-Lite 1--100"

_TEX_REPLACEMENTS = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "%": r"\%",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _plain_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinalReportInputError(f"{label} must be a non-empty string")
    return re.sub(r"\s+", " ", value.strip())


def _label_map(model: dict[str, Any]) -> dict[str, str]:
    labels = dict(DEFAULT_LABELS)
    document = model.get("labels")
    if document is None:
        return labels
    overrides = document.get("labels") if isinstance(document, dict) else None
    if not isinstance(overrides, dict):
        raise FinalReportInputError("labels document must contain a labels object")
    unknown = set(overrides) - set(labels)
    if unknown:
        raise FinalReportInputError(f"labels document contains unknown keys: {sorted(unknown)}")
    for key, value in overrides.items():
        labels[key] = _plain_text(value, label=f"label {key}")
    return labels


def _substitute(value: str, **values: Any) -> str:
    try:
        return Template(value).substitute({key: str(item) for key, item in values.items()})
    except (KeyError, ValueError) as exc:
        raise FinalReportInputError(f"invalid report label template: {value}") from exc


def _markdown(value: Any) -> str:
    text = _plain_text(str(value), label="report text")
    for old, new in (
        ("\\", "\\\\"),
        ("`", "\\`"),
        ("*", "\\*"),
        ("#", "\\#"),
        ("-", "\\-"),
        ("+", "\\+"),
        ("!", "\\!"),
        (".", "\\."),
        ("(", "\\("),
        (")", "\\)"),
        ("_", "\\_"),
        ("[", "\\["),
        ("]", "\\]"),
        ("<", "\\<"),
        (">", "\\>"),
        ("|", "\\|"),
    ):
        text = text.replace(old, new)
    return text


def _markdown_code(value: Any) -> str:
    text = _plain_text(str(value), label="report code")
    return "<code>" + text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</code>"


def _tex(value: Any) -> str:
    text = _plain_text(str(value), label="report text")
    return "".join(_TEX_REPLACEMENTS.get(character, character) for character in text)


def _tex_breakable(value: Any, *, group: int | None = None) -> str:
    """Escape monospaced evidence while adding safe line-break opportunities."""

    text = _plain_text(str(value), label="report evidence")
    pieces: list[str] = []
    hex_like = bool(group) and re.fullmatch(r"[0-9a-f]+", text) is not None
    for position, character in enumerate(text, start=1):
        pieces.append(_TEX_REPLACEMENTS.get(character, character))
        if character in "/._-":
            pieces.append(r"\allowbreak{}")
        elif hex_like and group is not None and position % group == 0 and position != len(text):
            pieces.append(r"\allowbreak{}")
    return "".join(pieces)


def _indices(values: list[int] | tuple[int, ...]) -> str:
    return ", ".join(str(value) for value in values) or "--"


def _narrative(model: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    value = model.get("narrative")
    if value is None:
        return [], []
    if not isinstance(value, dict):
        raise FinalReportInputError("narrative document must be an object")
    overview = value.get("overview", [])
    notes = value.get("task_notes", [])
    if not isinstance(overview, list) or not all(isinstance(item, str) and item.strip() for item in overview):
        raise FinalReportInputError("narrative overview must be a list of non-empty strings")
    if not isinstance(notes, list):
        raise FinalReportInputError("narrative task_notes must be a list")
    expected = {task["index"] for task in model["tasks"]}
    verified_refs = {
        item["path"]
        for slot in ("method_a", "method_b")
        for item in model["integrity"][slot]["evidence_files"]
    }
    normalized: list[dict[str, Any]] = []
    for position, note in enumerate(notes, start=1):
        if not isinstance(note, dict):
            raise FinalReportInputError(f"narrative task note {position} is not an object")
        indices = note.get("indices")
        if (
            not isinstance(indices, list)
            or not indices
            or any(isinstance(index, bool) or not isinstance(index, int) or index not in expected for index in indices)
            or len(indices) != len(set(indices))
        ):
            raise FinalReportInputError(f"narrative task note {position} has invalid indices")
        title = _plain_text(note.get("title"), label=f"narrative task note {position} title")
        text = _plain_text(note.get("text"), label=f"narrative task note {position} text")
        refs = note.get("evidence_refs", [])
        if not isinstance(refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in refs):
            raise FinalReportInputError(f"narrative task note {position} has invalid evidence_refs")
        if any(ref not in verified_refs for ref in refs):
            raise FinalReportInputError(
                f"narrative task note {position} references evidence outside the verified audit set"
            )
        normalized.append({"indices": indices, "title": title, "text": text, "evidence_refs": refs})
    return [_plain_text(item, label="narrative overview") for item in overview], normalized


def _factual_summaries(model: dict[str, Any]) -> tuple[str, str, str, str]:
    method_a = model["methods"]["method_a"]
    method_b = model["methods"]["method_b"]
    counts = model["counts"]
    comparison = model["comparison"]
    tasks = counts["tasks"]
    final = (
        f"{method_a}: resolved={counts['method_a']['resolved']}/{tasks}, "
        f"unresolved={counts['method_a']['unresolved']}/{tasks}, technical=0; "
        f"{method_b}: resolved={counts['method_b']['resolved']}/{tasks}, "
        f"unresolved={counts['method_b']['unresolved']}/{tasks}, technical=0."
    )
    compared = (
        f"common_resolved={comparison['common_resolved_count']}; "
        f"only_{method_a}_resolved={comparison['only_method_a_resolved_count']}; "
        f"only_{method_b}_resolved={comparison['only_method_b_resolved_count']}; "
        f"neither_resolved={comparison['neither_resolved_count']}."
    )
    proof = (
        f"terminal_coverage={tasks}/{tasks}; pending=0; technical=0; "
        f"candidate_identity_bound={tasks}/{tasks}; direct_execution_proven={tasks}/{tasks}."
    )
    clean = (
        f"clean_trajectory={tasks}/{tasks}; candidate_identity_verified={tasks}/{tasks}; "
        f"network_isolated={tasks}/{tasks}; audit_source_report_binding=verified."
    )
    return final, compared, proof, clean


def render_markdown(model: dict[str, Any]) -> str:
    """Render a Markdown report solely from the comparison model."""

    labels = _label_map(model)
    method_a = model["methods"]["method_a"]
    method_b = model["methods"]["method_b"]
    method_slots = (("method_a", method_a), ("method_b", method_b))
    counts = model["counts"]
    indices = model["indices"]
    title = _substitute(labels["title"], method_a=method_a, method_b=method_b)
    summary, comparison_summary, proof_summary, clean_summary = _factual_summaries(model)
    lines = [
        f"# {_markdown(title)}",
        "",
        f"{_markdown(labels['date'])}: {_markdown(model['generated_at'])}",
        "",
        f"{_markdown(labels['author'])}: {_markdown(model['author'])}",
        "",
        f"## {_markdown(labels['final_results'])}",
        "",
        _markdown(summary),
        "",
        f"| {_markdown(labels['method'])} | {_markdown(labels['resolved'])} | "
        f"{_markdown(labels['unresolved'])} | {_markdown(labels['technical'])} | "
        f"{_markdown(labels['terminal'])} |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for slot, method in method_slots:
        value = counts[slot]
        lines.append(
            f"| {_markdown(method)} | {value['resolved']} | {value['unresolved']} | "
            f"{value['technical']} | {value['confirmed_terminal']}/{counts['tasks']} |"
        )
    lines.extend(
        [
            "",
            f"## {_markdown(labels['comparison'])}",
            "",
            _markdown(comparison_summary),
            "",
            f"{_markdown(labels['common_resolved'])}: {_indices(indices['common_resolved'])}",
            "",
            f"{_markdown(_substitute(labels['only_resolved'], method=method_a))}: "
            f"{_indices(indices['only_method_a_resolved'])}",
            "",
            f"{_markdown(_substitute(labels['only_resolved'], method=method_b))}: "
            f"{_indices(indices['only_method_b_resolved'])}",
            "",
            f"{_markdown(labels['neither_resolved'])}: {_indices(indices['neither_resolved'])}",
            "",
            f"## {_markdown(labels['segmented_results'])}",
            "",
            f"| {_markdown(labels['range'])} | {_markdown(method_a)} {_markdown(labels['resolved'])} | "
            f"{_markdown(method_a)} {_markdown(labels['unresolved'])} | "
            f"{_markdown(method_b)} {_markdown(labels['resolved'])} | "
            f"{_markdown(method_b)} {_markdown(labels['unresolved'])} |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for segment in model["segments"]:
        lines.append(
            f"| {segment['start']}--{segment['end']} | {segment['method_a']['resolved']} | "
            f"{segment['method_a']['unresolved']} | {segment['method_b']['resolved']} | "
            f"{segment['method_b']['unresolved']} |"
        )
    lines.extend(
        [
            "",
            f"## {_markdown(labels['integrity'])}",
            "",
            _markdown(proof_summary),
            "",
            _markdown(clean_summary),
        ]
    )
    for slot, method in method_slots:
        integrity = model["integrity"][slot]
        runtime = integrity["runtime"]
        lines.extend(
            [
                "",
                f"### {_markdown(method)}",
                "",
                f"{_markdown(labels['fact_report'])}: {_markdown_code(integrity['fact_report_path'])} "
                f"({_markdown_code(integrity['fact_report_sha256'])})",
                "",
                f"{_markdown(labels['audit_manifest'])}: {_markdown_code(integrity['manifest_path'])} "
                f"({_markdown_code(integrity['manifest_sha256'])})",
                "",
                f"{_markdown(labels['runtime'])}: OpenCollab {_markdown_code(runtime['opencollab_commit'])}, "
                f"OpenCollab-Eval {_markdown_code(runtime['opencollab_eval_commit'])}, "
                f"dataset {_markdown_code(runtime['dataset_sha256'])}.",
                "",
                f"{_markdown(labels['evidence_files'])}: "
                + "; ".join(
                    f"{_markdown_code(item['path'])} ({_markdown_code(item['sha256'])})"
                    for item in integrity["evidence_files"]
                ),
            ]
        )
    overview, notes = _narrative(model)
    if overview or notes:
        lines.extend(["", f"## {_markdown(labels['curated_notes'])}"])
        for paragraph in overview:
            lines.extend(["", _markdown(paragraph)])
        for note in notes:
            lines.extend(
                [
                    "",
                    f"### {_markdown(note['title'])} ({_indices(note['indices'])})",
                    "",
                    _markdown(note["text"]),
                ]
            )
            if note["evidence_refs"]:
                lines.extend(
                    [
                        "",
                        f"{_markdown(labels['evidence_files'])}: "
                        + "; ".join(_markdown_code(ref) for ref in note["evidence_refs"]),
                    ]
                )
    lines.extend(
        [
            "",
            f"## {_markdown(labels['all_tasks'])}",
            "",
            f"| {_markdown(labels['task_index'])} | {_markdown(method_a)} | {_markdown(method_b)} |",
            "| ---: | --- | --- |",
        ]
    )
    for task in model["tasks"]:
        lines.append(f"| {task['index']} | {task['method_a']} | {task['method_b']} |")
    return "\n".join(lines) + "\n"


def render_tex(model: dict[str, Any]) -> str:
    """Render a self-contained ctex report with escaped external text."""

    labels = _label_map(model)
    method_a = model["methods"]["method_a"]
    method_b = model["methods"]["method_b"]
    method_slots = (("method_a", method_a), ("method_b", method_b))
    counts = model["counts"]
    indices = model["indices"]
    title = _substitute(labels["title"], method_a=method_a, method_b=method_b)
    summary, comparison_summary, proof_summary, clean_summary = _factual_summaries(model)
    lines = [
        r"\documentclass[11pt,a4paper,fontset=fandol]{ctexart}",
        r"\usepackage[margin=2.05cm]{geometry}",
        r"\usepackage{booktabs,longtable,array,xcolor,hyperref,fancyhdr}",
        r"\hypersetup{colorlinks=true,linkcolor=blue!52!black,urlcolor=blue!52!black}",
        r"\setlength{\parindent}{0pt}",
        r"\setlength{\parskip}{0.52em}",
        r"\setlength{\headheight}{15pt}",
        r"\setlength{\emergencystretch}{3em}",
        r"\renewcommand{\arraystretch}{1.18}",
        r"\sloppy",
        r"\definecolor{passgreen}{HTML}{16733B}",
        r"\definecolor{failred}{HTML}{9C1C1C}",
        r"\newcommand{\Pass}{\textcolor{passgreen}{\textbf{resolved}}}",
        r"\newcommand{\Fail}{\textcolor{failred}{unresolved}}",
        r"\pagestyle{fancy}",
        r"\fancyhf{}",
        f"\\lhead{{{_tex(method_a)} / {_tex(method_b)}}}",
        f"\\rhead{{{_tex(model['generated_at'])}}}",
        r"\cfoot{\thepage}",
        f"\\title{{\\textbf{{{_tex(title)}}}\\\\\\large {_tex(_FACTUAL_SUBTITLE)}}}",
        f"\\author{{{_tex(model['author'])}}}",
        f"\\date{{{_tex(model['generated_at'])}}}",
        r"\begin{document}",
        r"\maketitle",
        f"\\section{{{_tex(labels['final_results'])}}}",
        _tex(summary),
        r"\begin{center}",
        r"\begin{tabular}{lrrrr}",
        r"\toprule",
        f"{_tex(labels['method'])} & {_tex(labels['resolved'])} & {_tex(labels['unresolved'])} & "
        f"{_tex(labels['technical'])} & {_tex(labels['terminal'])} \\\\",
        r"\midrule",
    ]
    for slot, method in method_slots:
        value = counts[slot]
        lines.append(
            f"{_tex(method)} & {value['resolved']} & {value['unresolved']} & {value['technical']} & "
            f"{value['confirmed_terminal']}/{counts['tasks']} \\\\"
        )
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{center}",
            f"\\section{{{_tex(labels['comparison'])}}}",
            _tex(comparison_summary),
            f"\\textbf{{{_tex(labels['common_resolved'])}:}} {_tex(_indices(indices['common_resolved']))}",
            f"\\textbf{{{_tex(_substitute(labels['only_resolved'], method=method_a))}:}} "
            f"{_tex(_indices(indices['only_method_a_resolved']))}",
            f"\\textbf{{{_tex(_substitute(labels['only_resolved'], method=method_b))}:}} "
            f"{_tex(_indices(indices['only_method_b_resolved']))}",
            f"\\textbf{{{_tex(labels['neither_resolved'])}:}} {_tex(_indices(indices['neither_resolved']))}",
            f"\\section{{{_tex(labels['segmented_results'])}}}",
            r"\begin{center}",
            r"\begin{tabular}{lrrrr}",
            r"\toprule",
            f"{_tex(labels['range'])} & {_tex(method_a)} R & {_tex(method_a)} U & "
            f"{_tex(method_b)} R & {_tex(method_b)} U \\\\",
            r"\midrule",
        ]
    )
    for segment in model["segments"]:
        lines.append(
            f"{segment['start']}--{segment['end']} & {segment['method_a']['resolved']} & "
            f"{segment['method_a']['unresolved']} & {segment['method_b']['resolved']} & "
            f"{segment['method_b']['unresolved']} \\\\"
        )
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{center}",
            f"\\section{{{_tex(labels['integrity'])}}}",
            _tex(proof_summary),
            "",
            _tex(clean_summary),
        ]
    )
    for slot, method in method_slots:
        integrity = model["integrity"][slot]
        runtime = integrity["runtime"]
        lines.extend(
            [
                f"\\subsection{{{_tex(method)}}}",
                f"\\textbf{{{_tex(labels['fact_report'])}:}}\\par",
                f"{{\\small\\ttfamily {_tex_breakable(integrity['fact_report_path'])}}}\\par",
                f"{{\\footnotesize\\ttfamily SHA-256: "
                f"{_tex_breakable(integrity['fact_report_sha256'], group=16)}}}\\par",
                f"\\textbf{{{_tex(labels['audit_manifest'])}:}}\\par",
                f"{{\\small\\ttfamily {_tex_breakable(integrity['manifest_path'])}}}\\par",
                f"{{\\footnotesize\\ttfamily SHA-256: "
                f"{_tex_breakable(integrity['manifest_sha256'], group=16)}}}\\par",
                f"\\textbf{{{_tex(labels['runtime'])}:}}\\par",
                f"OpenCollab: {{\\small\\ttfamily "
                f"{_tex_breakable(runtime['opencollab_commit'], group=16)}}}\\par",
                f"OpenCollab-Eval: {{\\small\\ttfamily "
                f"{_tex_breakable(runtime['opencollab_eval_commit'], group=16)}}}\\par",
                f"Dataset SHA-256: {{\\small\\ttfamily "
                f"{_tex_breakable(runtime['dataset_sha256'], group=16)}}}\\par",
                f"\\textbf{{{_tex(labels['evidence_files'])}:}}",
                r"\begin{itemize}",
            ]
        )
        for item in integrity["evidence_files"]:
            lines.extend(
                [
                    f"\\item {{\\small\\ttfamily {_tex_breakable(item['path'])}}}\\par",
                    f"{{\\footnotesize\\ttfamily SHA-256: "
                    f"{_tex_breakable(item['sha256'], group=16)}}}",
                ]
            )
        lines.append(r"\end{itemize}")
    overview, notes = _narrative(model)
    if overview or notes:
        lines.append(f"\\section{{{_tex(labels['curated_notes'])}}}")
        lines.extend(_tex(paragraph) for paragraph in overview)
        for note in notes:
            lines.extend(
                [
                    f"\\subsection{{{_tex(note['title'])} ({_tex(_indices(note['indices']))})}}",
                    _tex(note["text"]),
                ]
            )
            if note["evidence_refs"]:
                lines.append(f"\\textbf{{{_tex(labels['evidence_files'])}:}}")
                lines.append(r"\begin{itemize}")
                lines.extend(f"\\item \\texttt{{{_tex(ref)}}}" for ref in note["evidence_refs"])
                lines.append(r"\end{itemize}")
    lines.extend(
        [
            f"\\section{{{_tex(labels['all_tasks'])}}}",
            r"\begin{longtable}{rll}",
            r"\toprule",
            f"{_tex(labels['task_index'])} & {_tex(method_a)} & {_tex(method_b)} \\\\",
            r"\midrule",
            r"\endhead",
        ]
    )
    for task in model["tasks"]:
        value_a = r"\Pass" if task["method_a"] == "resolved" else r"\Fail"
        value_b = r"\Pass" if task["method_b"] == "resolved" else r"\Fail"
        lines.append(f"{task['index']} & {value_a} & {value_b} \\\\"
        )
    lines.extend([r"\bottomrule", r"\end{longtable}", r"\end{document}"])
    return "\n".join(lines) + "\n"


__all__ = ["DEFAULT_LABELS", "render_markdown", "render_tex"]
