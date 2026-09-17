# External official-scoring adapters

**English** | [简体中文](zh-CN/scoring-adapters.md)

The Pro-Lite runner supports an external registry of evaluator adapters. Pass
the absolute registry path on the worker with `--scoring-adapter-registry`.
The parallel runner accepts the same option and forwards it to preflight and
each task process. `swe_eval_run` forwards this option to the parallel runner.
An eval-only queue can supply it in `runner_args`.

```bash
oc-eval swe-v1-prolite \
  --runner-transport local \
  --scoring-adapter-registry "$WORKER_SCORING_REGISTRY" \
  ...
```

`OPENCOLLAB_EVAL_SCORING_ADAPTER_REGISTRY` supplies the host configuration
default. The explicit CLI option takes precedence. The controller sends the
path in the worker configuration for both local and SSH transports. The path
refers to a file already installed on the worker. Source packaging includes
the generic loader and scoring entrypoints. Benchmark registry files and
adapter modules stay in the external directory maintained by the evaluator.

An installed runtime can also contain `engine/scoring_adapters/registry.json`.
The loader uses that relative path when an explicit path and host default are
absent. An unconfigured public runtime preserves the original scoring row.
An explicit unreadable registry, malformed registry, or invalid matching
adapter raises an error before generation and scoring in the task loop.

The task loop creates a separate scoring copy before candidate isolation.
Generation receives the original dataset row. Scoring prepares plans and
evaluation identities from the scoring copy. The direct `eval_for_task` and
`eval_for_task_once` entrypoints use the same preparation function, so the
adapter executes once when these entrypoints are chained. An in-memory dict
subclass retains the receipt separately from dataset fields.

An adapter declaring `SUPPORTS_CANDIDATE_PATCH=True` receives an optional
`candidate_patch` keyword from the verified prediction's evaluation patch.
Early task preparation defers this adapter until generation readiness is
verified. Retry planning and direct scoring reuse that same verified prediction
and metric snapshot. The scoring copy is completed in place once, while
generation retains the original row. Existing adapters keep their one-argument
call. The sealed gold patch remains part of the judge data.

The registry uses the existing `opencollab.scoring_adapter_registry.v1` format.

```json
{
  "schema": "opencollab.scoring_adapter_registry.v1",
  "entries": [
    {
      "instance_id": "example-instance",
      "adapter_id": "example-public-interface-v1",
      "module": "example_adapter.py",
      "allowed_changed_fields": ["test_patch"]
    }
  ]
}
```

Each module exports `INSTANCE_IDS`, `ADAPTER_ID`, and
`adapt(instance) -> (adapted_instance, receipt)`. The loader passes a deep copy
to the adapter. It verifies the module identity, the exact set of allowed
changes, and the original instance identity. The receipt retains the existing
isolation declarations `solver_input_unchanged=true` and
`gold_production_code_added=false`, with `adapter_id` and `changed_fields`
matching the registry entry. The evaluator operator supplies the adapter's
functional validation and keeps it beside that external module.

Task reports include `scoring_adapter`. Executed official reports and summaries
include the same receipt, and the official input directory saves
`scoring_adapter_receipt.json`. The receipt records the registry path, module
path, adapter ID, declared changes, and adapter-supplied provenance.
The existing `original_instance_sha256` and `adapted_instance_sha256` receipt
fields describe the two copies using the existing serialization format.

For a direct installed runner, supply `scoring_adapter_registry` in the config
passed to `swe_v1_remote_runner.install_into`. For standalone preparation, call
`swe_eval_scoring_adapters.prepare_scoring_row(row, registry_path)`.

Historical metadata may contain a canonical `instance_id` together with an
anonymous `task_id` in the `solver-<32 lowercase hexadecimal characters>`
namespace. The shared task parser uses the canonical instance identity and
retains the original dictionary. Other conflicting benchmark aliases continue
to reject pairing, and record and patch identity checks retain their existing
behavior.
