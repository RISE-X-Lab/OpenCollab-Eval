# Experiment tools

This package supports batch specifications, offline arm observations, model behavior
inspection, sample selection, and reports over recorded evaluation attempts.

Data tools load independently from runtime observers. Importing task sampling or cell
reporting leaves arm generation dependencies unloaded. The package facade loads audit
exports when callers request them.

Cell reporting separates trajectory interpretation in `cell_report_rows`, attempt
selection and statistics in `cell_report_statistics`, and text and JSON output in
`cell_report_rendering`. The original `cell_report` imports remain available.

`commands.batch_reporting.select_cell_rows` selects retries, active replacements,
and withdrawals for both the report and merged prediction export. Selection uses
recorded attempt dispositions and the declared batch relations. Selected observation
cost and recorded cost across all attempts have separate fields.

The model audit queries `opencollab.models.inspect_model_runtime` for behavior facts.
`OpenCollabSource` is a source archaeology reader that attributes those facts to
explicitly located Python source files. It uses AST inspection over the chosen
checkout. Runtime behavior queries use the public facade and perform no model requests.
Endpoint probes are explicit paid operations selected through the model audit command.

Research source material and original batch conditions are documented in the repository
[experiment collection](../../../experiment/README.md).
