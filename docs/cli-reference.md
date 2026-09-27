# CLI reference

**English** | [简体中文](zh-CN/cli-reference.md)

Run `oc-eval --help` for installed options. `oc-eval inspect` reads the dataset
with an evaluator-owned identity key. `oc-eval run TASKS_JSONL` generates
candidates and reports submission eligibility, accepting optional
`--workflow module:function`. `oc-eval score` forwards arguments to the installed
SWE-bench scoring wrapper. Use `oc-eval score --help` for prediction and report
options.

For OpenCollab 0.8 canonical Duo, select
`--workflow opencollab.builtin_workflows:duo`. `--agent-profile base` uses the
Base mapping to `single2`; explicit `--agent-profile single2` selects the same
profile. The compatibility profile names `single` and `default` also resolve
through Base. The supplied profile is resolved and recorded by the evaluator.

Campaigns, G22 presets, rejudging queues, provider proxies, runtime packaging,
and comparison report presentation use `oc-exp` from OpenCollabExp.
