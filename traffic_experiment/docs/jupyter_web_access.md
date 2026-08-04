# Secure browser access to the lab server

JupyterLab can provide an AutoDL-like browser, file editor, notebooks, and
terminal. It does not itself bypass a VPN or firewall. A separate reachable
HTTPS ingress path is required.

Because an authenticated Jupyter terminal can execute arbitrary commands as its
Unix user, obtain lab/IT approval before publishing it outside the research
network.

## Architecture

```text
browser
  -> HTTPS identity gateway
  -> outbound tunnel or approved public reverse proxy
  -> 127.0.0.1:8888 JupyterLab
  -> experiment files and terminals
```

Jupyter never binds to `0.0.0.0`. It listens only on loopback and retains its
random token as a second authentication layer.

## Install the localhost service

Run as the non-root lab account that owns the experiment:

```bash
cd /srv/commu/traffic_experiment
bash scripts/20_setup_jupyter_web.sh
bash scripts/21_check_jupyter_web.sh
```

To choose another file root or port:

```bash
JUPYTER_ROOT_DIR=/srv/commu JUPYTER_PORT=8888 \
  bash scripts/20_setup_jupyter_web.sh
```

The installer creates a dedicated virtual environment, a 256-bit token, and a
systemd user service. Ask an administrator to enable user lingering if the
service must remain active after logout:

```bash
sudo loginctl enable-linger <LAB-USER>
```

Do not run the notebook service as root.

## Recommended: named outbound tunnel

Use this when the server has outbound Internet access but no approved inbound
port. A named Cloudflare Tunnel needs:

1. Institutional approval.
2. A Cloudflare account and domain.
3. A Cloudflare Access policy restricted to the researcher's institutional
   identity.
4. Outbound connectivity to Cloudflare, normally including port 7844.

Install `cloudflared` using Cloudflare's current package instructions. Create a
named tunnel, copy `web_control/cloudflared-config.yml.example`, replace the
tunnel UUID, credential path, and hostname, and validate it:

```bash
cloudflared tunnel ingress validate
cloudflared tunnel run <TUNNEL-NAME-OR-UUID>
```

After testing, install the tunnel as a service using Cloudflare's supported
service workflow. Do not place a tunnel token in Git, notebooks, shell history,
or a world-readable systemd unit.

A quick `trycloudflare.com` tunnel is acceptable only for a short connectivity
test. It is not the final deployment: its hostname is temporary and it lacks the
named tunnel's stable identity policy.

## Alternative: direct Caddy HTTPS

Use `web_control/Caddyfile.jupyter.example` only when all of the following are
true:

- IT assigns a DNS hostname to the lab server.
- inbound TCP 80/443 is explicitly permitted;
- the server is reachable without the conflicting VPN route;
- the firewall exposes Caddy, not port 8888.

Run this as a separate Caddy service/configuration. Its admin endpoint is 2020,
so it does not collide with the experiment proxy. Keep experimental ports 8443,
8444, 8543, and 8544 separate.

## Interaction with traffic experiments

The controlled packet capture occurs on `llmhost0`, whereas browser/tunnel
traffic uses the physical interface and loopback. It therefore is not counted in
the experimental PCAP. It can still consume CPU, disk, GPU, or physical network
capacity, so during final measurements:

- use Jupyter only to monitor logs and progress;
- do not upload/download files;
- do not run additional notebook kernels or GPU work;
- record that the management service was active;
- preferably stop idle kernels before each measurement block.

If absolute resource isolation is needed, keep Jupyter on a separate management
node and control the inference host through the institution's approved internal
route.

## Troubleshooting

- If `127.0.0.1:8888` works but the public hostname does not, the problem is the
  tunnel, DNS, identity policy, or outbound firewall—not Jupyter.
- If the page loads but kernels disconnect, inspect WebSocket proxying and the
  browser console.
- If the service stops after logout, enable user lingering.
- If both VPN and non-VPN routes fail, ask IT whether the lab server permits
  outbound tunnels. Do not circumvent an institutional network policy.
