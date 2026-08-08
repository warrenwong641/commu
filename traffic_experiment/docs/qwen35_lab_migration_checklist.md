# Qwen3.5 lab-migration staging checklist

This checklist prepares a future Qwen3.5 lab migration without changing the
active ignored `server.lab.env`. The tracked staging example is intentionally
credential-free and non-runnable. Static validation does not prove that the lab
machine, CUDA stack, model cache, executables, GPUs, or listeners are ready.

## Phase A: cloud/static checks (safe to run now)

- [ ] Start from the reviewed repository commit on a dedicated branch.
- [ ] Keep `traffic_experiment/environments/qwen35/`, active env files, model
      caches, credentials, packet captures, run directories, and bundles
      unchanged.
- [ ] Review `server.qwen35.staging.env.example`. It must retain:
  - model and served name `Qwen/Qwen3.5-9B`;
  - revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`;
  - exactly GPUs `0,1`, two workers, and ports `8000`/`8001`;
  - tensor parallel size 1, context limit 65536, and both output limits 4096;
  - a loopback-only vLLM host;
  - explicit staging placeholders or absolute staged paths for `VLLM_BIN` and
    `RUNNER_PYTHON`.
- [ ] Run the non-executing validator from the repository root:

  ```bash
  python3 traffic_experiment/scripts/validate_qwen35_staging_config.py \
    traffic_experiment/server.qwen35.staging.env.example
  ```

- [ ] Confirm the validator prints `QWEN35_STAGING_CONFIG_OK`.
- [ ] Run its focused tests:

  ```bash
  python3 -m unittest -q traffic_experiment.tests.test_qwen35_staging_config
  ```

- [ ] Review the staged diff and secret-scan it before transfer or publication.

Stop after Phase A in a cloud workspace. Do not install environments, resolve
the placeholders to guessed live paths, start services, make model requests, or
copy values into the ignored active env file from here.

## Phase B: later lab-server runtime validation (not performed now)

Perform this phase interactively on the intended lab server after its owner has
approved the migration window.

- [ ] Record existing GPU processes, listener ownership, service ownership,
      installed driver/CUDA versions, free storage, and the current active vLLM
      configuration before making changes.
- [ ] Stage a lab-compatible vLLM environment and runner environment without
      overwriting an existing environment. Record their exact absolute
      executable paths and package versions.
- [ ] Replace only the two staging placeholders in a temporary copy, rerun the
      static validator, and independently verify both paths exist and are
      executable. Static validation intentionally does not access the paths.
- [ ] Verify the pinned model revision is available locally and record its
      cache/source digest. Do not download the model during packet capture.
- [ ] Verify physical GPU IDs `0` and `1` are the intended free devices and that
      ports `8000` and `8001` have no unrelated listeners.
- [ ] Deliberately transfer the reviewed values into the ignored
      `server.lab.env`; keep credentials out and load any required credential
      only into the invoking process environment.
- [ ] Run `scripts/17_lab_preflight.sh` with the explicit active env path and
      archive its machine audit. This is the runtime gate; the staging validator
      is not a replacement for it.
- [ ] Start vLLM only after preflight inputs and ownership are reviewed. Verify
      one worker per GPU, loopback listeners on ports 8000/8001, the exact served
      model name/revision, context limit, and output ceilings.
- [ ] Perform unmeasured health/warm-up validation, then the documented protocol
      pilots and admission checks before any measured matrix.
- [ ] After validation or failure, stop only recorded project-owned resources,
      verify listener closure, and preserve logs/audits outside version control.

The full runtime sequence, network lifecycle, protocol pilots, and teardown are
defined in `docs/lab_server_migration.md`.
