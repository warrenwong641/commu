# Server-side zero-transport scaffold

Run this directory from the project checkout on the GPU server (for example,
`/path/to/commu/server_side_zero_transport`). All experiment components run
there and use loopback. This scaffold never starts, restarts, signals, or
kills vLLM.

## Safety contract

- Set `ALLOWED_GPU_IDS` to exactly two explicit GPU indices or UUIDs.
- Set `VLLM_PIDS` to every PID belonging to the already-running vLLM service
  (frontend and workers). Do not guess them.
- `verify_existing_vllm.sh` only reads `/proc` and `nvidia-smi`; it sends no
  HTTP/model request.
- Verification fails unless the union of GPUs used by `VLLM_PIDS` is exactly
  the two allowed GPUs. A listed PID found on any other GPU also fails.
- Client, gateway, and analysis processes are launched with
  `CUDA_VISIBLE_DEVICES=`. Only their recorded child PIDs are cleaned up.
- The vLLM URL and all experiment listeners must be loopback addresses.
- Verification evidence never records full process command lines because they
  may contain credentials.

## Use

```bash
cp config.env.example config.env
# Edit config.env with two device IDs, all existing vLLM PIDs, URL, and ports.

./verify_existing_vllm.sh ./config.env
# Inspect the printed and saved nvidia-smi/process evidence. No request is sent.

read -rsp "Existing vLLM API key: " VLLM_API_KEY
echo
export VLLM_API_KEY
./run.sh ./config.env
unset VLLM_API_KEY
```

`run.sh` re-verifies immediately before starting anything. Each run is stored
under `runs/<UTC timestamp>-<random>/`. Ports must be explicitly selected and
unused. The sample client sends one request only after the verification gate
passes. Stop with Ctrl-C; cleanup verifies the gateway executable, command,
process start identity, and listener closure before clearing its recorded state.

The gateway supports ordinary HTTP loopback vLLM endpoints. It does not change
the model service. TLS/HTTP3 or public port 443 are intentionally out of scope.

The client forwards the API key in memory. It is not written to the config,
request artifact, verification evidence, or logs.
