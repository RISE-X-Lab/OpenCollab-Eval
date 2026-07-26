# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities through
[GitHub Security Advisories](https://github.com/RISE-X-Lab/OpenCollab-Eval/security/advisories/new).
Please include a minimal reproduction, the affected revision, and the expected
impact. The maintainers aim to acknowledge reports within 72 hours.

## Risk surface

OpenCollab-Eval can execute model-generated commands, attach to Docker, connect
to remote workers, and run benchmark tests. Use disposable workers that contain
no unrelated data. Keep credentials in files outside the repository and grant
each worker access only to the current run.

Evaluation records can contain source patches, model transcripts, task
identities, runtime paths, and provider metadata. Store them outside the source
checkout and review them before publication.

## Supported versions

OpenCollab-Eval is pre-1.0. Security fixes are maintained on `main`.
