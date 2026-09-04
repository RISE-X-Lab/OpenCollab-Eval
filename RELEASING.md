# Releasing OpenCollab-Eval

OpenCollab-Eval releases are paired with a verified OpenCollab release. PyPI publishing is outside the current release process.

## Release invariants

- Release a clean commit already present on `main`.
- Keep the package version, annotated tag, changelog, and wheel metadata aligned.
- Bind CI to the immutable commit behind the compatible OpenCollab tag.
- Verify every GitHub check, test, and artifact against the exact release SHA.
- Push only the intended tag ref and never move a published tag.
- Use a signed annotated tag. When signing or tag protection is unavailable, record the maintainer's explicit waiver in the GitHub Release before completion.

## Verify the candidate

Record the exact OpenCollab and OpenCollab-Eval commits. Confirm that the OpenCollab commit is the peeled target of its published tag and that both worktrees are clean.

```bash
git fetch origin main
git switch main
git pull --ff-only origin main
release_sha="$(git rev-parse HEAD)"
test "$release_sha" = "$(git rev-parse origin/main)"

python -m pip install -e ../OpenCollab
python -m pip install -e '.[dev,swebench]'
ruff check .
pytest -q
```

Wait for the Python matrix, deterministic SWE end-to-end job, hygiene, title, and security checks on `release_sha`.

## Build the paired artifacts

Build the OpenCollab wheel from its published tag, then build the evaluator source distribution and wheel. Run the installed-wheel contract against those exact artifacts.

```bash
set -euo pipefail
release_version=0.5.1
opencollab_release_tag=v0.5.0
opencollab_release_sha=963585611ad2a1d0c1fc7f4ba0043af5a3d860bb
artifact_root="$(mktemp -d -t "opencollab-eval-${release_version}.XXXXXX")"
mkdir -p "$artifact_root/opencollab" "$artifact_root/sdist" "$artifact_root/wheel" "$artifact_root/assets"

test "$(git -C ../OpenCollab rev-parse "${opencollab_release_tag}^{}")" = "$opencollab_release_sha"
test "$(git -C ../OpenCollab rev-parse HEAD)" = "$opencollab_release_sha"
test -z "$(git -C ../OpenCollab status --porcelain)"
uv build --wheel --no-sources --out-dir "$artifact_root/opencollab" ../OpenCollab
uv build --sdist --no-sources --out-dir "$artifact_root/sdist"
sdists=("$artifact_root"/sdist/*.tar.gz)
test "${#sdists[@]}" -eq 1
uv build --wheel --no-sources "${sdists[0]}" --out-dir "$artifact_root/wheel"
uvx --from twine==6.2.0 twine check "${sdists[0]}" "$artifact_root"/wheel/*.whl
scripts/verify_wheel_contract.sh "$artifact_root"/opencollab/*.whl "$artifact_root"/wheel/*.whl
cp "$artifact_root"/sdist/*.tar.gz "$artifact_root"/wheel/*.whl "$artifact_root/assets/"
(
  cd "$artifact_root/assets"
  sha256sum ./*.tar.gz ./*.whl > SHA256SUMS
)
```

Install the evaluator wheel in a new Python 3.12 environment. Verify `opencollab_eval.__version__`, `oc-eval --version`, and `oc-eval --help` from outside the source checkout.

## Tag and publish

Create the annotated `vX.Y.Z` tag on `release_sha`, verify its peeled target, and push only that ref. Publish a GitHub prerelease while the project remains Alpha. Attach the evaluator source distribution, wheel, and `SHA256SUMS`.

After publication, download the assets into a new directory, verify their hashes, repeat the installed-wheel probe, and confirm that an anonymous clone of the tag reaches `release_sha`.
