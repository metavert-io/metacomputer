# Providers

Each place metacomputer can make a computer, besides this Mac and your own server, is a folder here. A pull request
for one provider should change that folder and little else: shared code never names a provider (a test fails when it
does), and each folder has its own reviewers (`.github/CODEOWNERS`).

The list of providers, what each has and where its docs are: [docs/providers.md](../docs/providers.md) (made from the
manifests).

## What a provider's folder holds

```
providers/<name>/
  __init__.py      empty: the folder is a package
  adapter.py       the adapter (lib/cloud/adapter.py's interface); ADAPTER = its class
  manifest.py      MANIFEST: what it has (lib/cloud/capabilities.py); CLI: what the command line needs
                   (lib/cloud/manifests.py: credentials, options, transport, words, owners)
  settings.env     its settings, as comments: what config/vms/NAME.env may say for a computer there
  README.md        its page for contributors: what's where, its credentials, what keeps it apart, what it bills
  claims.tsv       its doc-claims rows (tests/test_doc_claims.py reads every registered provider's)
  remote.py        optional: its own part of lib/remote.py (hooks, called with lib/remote.py as R)
  cli.sh           optional: its own steps in bin/metacomputer and scripts/vm-commands.sh (pv_<name>_HOOK)
  build.sh         optional: its own build steps in scripts/remote-build.sh (pv_<name>_build_HOOK)
  guest/           optional: what goes into the computer (copied into the build's payload under its target)
```

The framework is `lib/cloud/`: the interface, the capability checks, the SSH transport, the shared base for providers
that bill servers by the hour (`vps.py`), the stand-ins (`local_lume.py` this Mac, `ubuntu_vm.py` your own server and
the base of every Ubuntu VM reached over SSH, `fake.py` the tests' stand-in), and the registry.

## Adding a provider

1. Its folder, as above. Start from the closest one: a server billed by the hour is a thin driver on `lib/cloud/vps.py`
   (`hetzner`, `digitalocean`, `vultr`); one with its own lifecycle brings hooks (`sprites`).
2. One line in `lib/cloud/registry.py` (`PROVIDERS`).
3. `python3 scripts/gen-providers.py`: docs/providers.md, lib/cloud/providers.sh (what bash reads), the credential
   lines that take every provider's credentials off the environment, and CODEOWNERS follow from the manifests.
4. A line in the CHANGELOG.

The checks: `python3 tests/test_cloud_adapters.py` (the contract every adapter passes, the manifests, the generated
files being current, and no provider named in shared code), then the provider's own tests.

## Hooks

Shared code finds a provider's own behaviour by what it brings, never by its name:

- **lib/remote.py** (`hooks_for`): `provider_kw`, `create`, `abort`, `delete`, `plan`, `cloud`, `egress_check`;
  for its transport `connect_wrapper`, `tunnel`, `probe`; and `COMMANDS` it alone has (`exec`, `checkpoint`,
  `allow`). Adapter attributes say the rest (`SOFT_STOP`, `SEES_GUEST_POWEROFF`, `RESOLVER_EXCEPTION`,
  `CONSOLE_POWEROFF`, `GROUPS_IN_CONSOLE`, `REST_API`, `groups_by_record`, `SHARED_NETWORK_CHANGES`).
- **bash** (`scripts/lib.sh` `mc_pv_has` / `mc_pv_run`, from `cli.sh`): `can_ask`, `vm_create`, `vm_said`,
  `vm_billing`, `vm_delete_what`, `stop`, `stop_needs_credentials`, `stop_saying`, `net_status`, `net_egress`,
  `net_list`, `snapshot`, `desktop_restart`, `logs`; and in `build.sh`, `build_what`, `build_first_step`,
  `build_desktop`, `build_egress_ok`, `build_finish`.

Everything the guards decide stays shared and unchanged: the hub's guards, the vault, the handoff gate (every provider
command gates the same computers), the network isolation rules and the secrets flow.
