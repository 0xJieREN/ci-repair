# LCA local check replay

External dataset: [JetBrains-Research/lca-ci-builds-repair](https://huggingface.co/datasets/JetBrains-Research/lca-ci-builds-repair),
revision `ebf12dad7a97c3c0cdc38705403d8bc8ddde47dc`, default/test split.
No dataset rows or upstream source are vendored here. The importer downloads
and verifies the pinned Parquet, then uses reviewed recipes for IDs 24 and 107.
Upstream license files stay in each complete source snapshot. The dataset card
states that source repositories use permissive licenses.

See [selection rationale and measurement limits](../../docs/benchmark-selection-2026-09-22.md).
This is a **selected-check local replay**, not the official LCA full-workflow
benchmark. There is no hidden oracle and no false-PASS estimate.

```bash
# macOS: ensure VM shutdown even on failure. On Linux omit the Colima lines.
trap 'colima stop' EXIT
colima start --cpu 2 --memory 4 --disk 20
docker build -t ci-repair-lca:local benchmarks/lca

# Admission + correct/no-op controls, both runners; no paid model calls.
uv run --with pyarrow==25.0.1 python -m ci_repair.lca \
  --output runs/lca-controls --controls --steps 3

# New output directory; explicit paid budget. Only run with authorized credentials.
uv run --with pyarrow==25.0.1 python -m ci_repair.lca \
  --output runs/lca-model --model deepseek/deepseek-flash --env-file .env \
  --steps 15 --max-total-calls 60
```

Every model batch rechecks both original failures and both reference fixes
before any paid call. `scores.json` records selected-check success and agent
exit status separately. Raw logs, reference diffs, admission records and all
trajectories remain in the ignored output directory. Inspect `baseline.json`
when admission fails; missing tools or mismatched diagnostics are environment
errors, not repair failures. Recipes never execute commands parsed from data.

HTTPX checker versions come from its pinned `requirements.txt`; dependencies
were resolved with `uv pip compile --python-version 3.10 --exclude-newer
2023-12-28 --generate-hashes`. cloud-init's Ruff 0.0.285 is stated in its archived
CI log. Python 3.10.21/Linux ARM64 is our local environment, not a recreation of
every original OS/Python matrix combination. Dockerfile base/tool images are
pinned by digest; the final image ID is recorded in each experiment. Apt package
repositories are not an immutable snapshot, so rebuilds may produce a new ID.

The [first measured pilot](../../docs/experiments/2026-09-22-lca-deepseek.md)
completed four trials using 41 of 60 authorized model calls. Keep its
selected-check results separate from official benchmark scores.
