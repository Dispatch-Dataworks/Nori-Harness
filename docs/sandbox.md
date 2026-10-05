# Code sandbox

`run_sandboxed_command` lets Nori (and any sub-agent with **can run code** ticked) run `python3` against a copy of one working-folder folder, in a separate container that cannot hurt anything. It is **off by default**: an admin turns it on under *Settings → Code sandbox*.

## How it is isolated

The isolation is the container, not the model's good behavior.

| | Nori container | Sandbox container |
|---|---|---|
| Network | normal | **none** (`network_mode: none`) — no DNS, no sockets out |
| Volumes | data, prompts, static | **none** except `sandbox-sock` (the socket) |
| Secrets (`.env`, `secret.key`, DB) | yes | **no** — no `env_file`, no data volume |
| Root filesystem | writable | read-only; scratch is a 256 MB tmpfs |
| Capabilities | default | all dropped except what the runner needs to drop each run to an unprivileged uid |
| Limits | — | 768 MB memory, 128 pids, 1 CPU; per run: CPU time, 512 MB address space, 64 processes, 50 MB per file, 256 open files |

Nori talks to the runner over a unix socket in the shared volume (length-prefixed JSON). The runner never touches Nori's files and Nori never mounts anything into the sandbox: Nori **sends a copy** of the chosen folder and gets back stdout, stderr, the exit status and a manifest of what the run created, changed or deleted.

Each run: a fresh directory, files owned by an unprivileged uid (`53417`, deliberately unusual — Linux counts process limits per uid across the whole host), no supplementary groups, own session, scrubbed environment, hard wall-clock kill of the process group, then every process of that uid is killed and reaped and the directory is deleted. One run at a time.

## What a run can do to your files

Nothing directly. Results come back to Nori, which saves them through the same write path every model-driven write uses (`workfiles.write_bytes`):

- a file Nori created earlier is updated in place;
- a file **anyone else** placed is never overwritten — the new version is saved beside it as `name.v2.ext` (then `v3`, …);
- a **deletion** inside the sandbox is reported and never applied;
- a sub-agent's **write folder** applies: it can only run code inside it;
- a sub-agent **without `file_write`** can compute and print, but nothing is saved;
- the usual per-user quotas apply.

So there is no "confirm before it modifies your files" step to forget: overwriting or deleting a user-owned file is not possible.

## What can run

Only `python3` (an argv, not a shell string — no pipes, redirects or `&&`), standard library only — there is no network to install anything. Default time limit 30 s, maximum 120 s (`NORI_SANDBOX_MAX_TIMEOUT_S`; the runner's own ceiling is 300 s). The whole `cwd` folder is copied in, capped at 300 files / 20 MB (5 MB per file), so give a narrow `cwd`.

## Audit

Every call, including refused ones, is a row in `sandbox_runs`: when, who (chat or sub-agent job), the command, where, the outcome, exit code, duration, what was saved and under what name, what the run deleted (not applied). The last 25 are listed on the Code sandbox tab.

## Enabling it

1. `docker compose up -d` (builds and starts the `sandbox` service; it idles at ~15 MB).
2. *Settings → Code sandbox* — tick the box. The page shows whether the container is reachable.
3. For sub-agents: tick **can run code** on the roster entry (it also needs a nonzero tool-call limit, and **can write files** if its output should be saved).

## Honest limits

- A container is not a hardware boundary: a kernel escape would defeat it. The design makes one unattractive rather than impossible — nothing valuable is inside, nothing sensitive is reachable, and it has no network to exfiltrate over.
- Code can burn its CPU/memory/time budget; the limits bound the damage and a stuck run is killed.
- Output files are text or binary the model chose to write; treat them like any other file Nori creates.
- Changing the allowed interpreters (`SANDBOX_ALLOWED` on the sandbox service, `NORI_SANDBOX_ALLOWED` on Nori) widens what can run — keep it to interpreters with no way to open a shell by default.

Tests: `tests/test_nori_sandbox.py` (Nori's side, runs anywhere) and `tests/test_nori_sandbox_runner.py` (hostile code against the real runner; run inside the container as described in its docstring).
