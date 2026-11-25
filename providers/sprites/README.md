# Fly Sprites (`sprites`)

For people: [docs/sprites.md](../../docs/sprites.md) (setup, token scopes, what keeps it apart, what it costs). This page is for
contributors: what's in this folder ([providers/README.md](../README.md) says what every provider's folder holds).

Sprites made, checkpointed and deleted through Fly's own `sprite` command (which keeps its own sign-in: no
credentials in the environment), reached through Fly's tunnel (`sprite proxy -W :22`, the `sprite` transport) through
`lib/proxywrap.py`. A Sprite sleeps by itself about 10 s after nothing is connected.

| File | What |
|---|---|
| `adapter.py` | the adapter: launches labelled by operation, every operation checked against Fly's id for the Sprite, the network policy (an allowlist of names), checkpoints, rates; it registers the `sprite` transport |
| `cli.py` | the facade of Fly's `sprite` command: byte caps on what it prints, `HOME`, `PATH` and `LANG` only, `exec --no-port-forward` |
| `manifest.py` | its capabilities; `CLI`: its transport, options (`--org`, `--sprite`), words |
| `remote.py` | its part of `lib/remote.py`: create, abort, delete, plan, the transport's checks (target, tunnel, probe), the network policy's narrowing rule, `exec`, `checkpoint`, `allow` |
| `cli.sh` | its own steps in `vm create`, `stop`, `net`, checkpoints and `reset`, `desktop-restart`, `logs` |
| `build.sh` | its own build steps: the first step through Fly's channel, the desktop as Sprites services, the baseline checkpoint |
| `guest/` | its bootstrap and the session's services (`/opt/metacomputer/sprite/`), copied into the payload as `sprite/` |
| `settings.env`, `claims.tsv` | its settings; its doc-claims rows |

**What keeps it apart**: Fly's network policy, outside the VM, is the one layer (no firewall of its own); everything of
the desktop's account runs in a session with its own mount and PID namespaces, `/.sprite` covered. **Input**: no
`/dev/uinput`, so the hub sends keys, typing and pixel clicks in the foreground. **Billing**: asleep, only its disk.
SECURITY.md ("Fly Sprites") has the details.

## Tests

`python3 tests/test_cloud_adapters.py` runs the contract every adapter passes against it (no VM, no network), checks
its manifest and that no shared code names it. Its own: `python3 tests/test_sprites.py` (a stand-in `sprite` command; no VM, no network).

## What stays shared

The hub's guards, the vault, the handoff gate (every command that changes one of its computers gates it the same
way), the network isolation and gateway rules, and the secrets flow are the framework's, not this folder's.
