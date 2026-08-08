# Hardware-validation handoff

Run these steps manually on the lab host before changing an active service.
They are intentionally not performed by the repository-only preflight.

The project model service and any disposable pilot may use only physical GPUs 0
and 1. For the two workers, set worker 0 to `CUDA_VISIBLE_DEVICES=0` and worker 1
to `CUDA_VISIBLE_DEVICES=1` (one physical GPU exposed per worker). GPUs 2-7 are
out of scope: do not inspect their workloads beyond the read-only ownership
inventory, expose them to a worker, stop their processes, or otherwise touch
them.

1. Record `nvidia-smi --query-gpu=index,name,uuid,memory.total,driver_version,compute_cap --format=csv`.
   Confirm every selected GPU has compute capability at least 7.5, the general
   floor documented by vLLM. This floor alone does not prove Qwen3.5 will fit or
   run correctly.
2. Record `nvidia-smi` process ownership and all active listeners. Do not stop or
   reuse an unrelated process, GPU, or port.
3. Inventory CUDA components from `requirements.lock`. The selected PyTorch
   wheel is CUDA 12.9, while vLLM's resolved dependencies also include CUDA 13.x
   tooling. Compare the installed driver with NVIDIA's compatibility tables for
   the complete resolved stack and confirm it in the live pilot. If evidence is
   missing, stop; do not infer compatibility from the local toolkit, the
   `nvidia-smi` "CUDA Version" label, or another host.
4. With `CUDA_VISIBLE_DEVICES=` run `preflight.py` against the exact cached
   snapshot. Archive its JSON output (`--json`) with the environment lock digest.
5. In a project-owned maintenance window, expose only the intended GPU(s), start
   a disposable vLLM instance on an unused loopback port with the exact revision,
   `--language-model-only`, service context limit `MAX_MODEL_LEN=65536`, maximum
   generated output `MAX_OUTPUT_TOKENS=4096`, and conservative memory
   utilization. The first value is the model context-window cap; the second is
   the per-response generation cap. Preserve the startup log and inspect every
   kernel or out-of-memory failure.
6. Verify `/v1/models`, then send one non-measured minimal request. Record vLLM,
   PyTorch, CUDA-runtime, driver, GPU, model revision, dtype, tensor-parallel
   size, and peak memory. This live pilot—not the static checker—is the evidence
   for hardware compatibility.
7. Stop the disposable instance and verify its PID and listener are gone. Only
   after the pilot is green should the active experiment configuration point to
   this isolated environment's `vllm` executable.

The cached model revision must be
`c202236235762e1c871ad0ccb60c8ee5ba337b9a`. A matching architecture string or
directory name is identity evidence, not proof that the weight files are
complete; retain the cache/snapshot integrity evidence used by the lab operator.
