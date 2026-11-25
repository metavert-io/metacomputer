# Oracle Cloud's own steps in metacomputer's commands (bin/metacomputer, scripts/vm-commands.sh): each pv_oci_HOOK runs
# where the shared code finds the hook (scripts/lib.sh mc_pv_has / mc_pv_run) instead of its own step, in the same
# shell, with that command's variables and functions. Sourced (scripts/lib.sh mc_pv_load), never run.

# vm create --on oci (scripts/vm-commands.sh vm_create: its variables).
pv_oci_vm_create() {
  [ -z "$host$huser$hport" ] || die "--host, --user and --port are for your own server; Oracle Cloud gives the computer its address"
  [ -z "$region" ] || die "--region: an Oracle Cloud computer is made in your credentials' region (OCI_REGION)"
  size="${size:-arm-2x12}"
  a="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import cloud; oci = cloud.registry.module("oci")
try: oci.size_of(sys.argv[2])
except Exception as e: print(e); sys.exit(1)' "$LAB_ROOT/lib" "$size")" || die "$a"
  case "${idle:-15}" in off|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) ;; *) die "--idle-stop: minutes (1-999), or off" ;; esac
  rlines=("SIZE=$size" "IDLE_STOP=${idle:-15}")
}
pv_oci_vm_billing() {
  # Something billed: the rate and what it keeps, before anything is made (providers/oci/adapter.py rates).
  a="$(MCOMP_VM="$name" remote_py rates --vm "$name" 2>/dev/null || true)"
  printf '%s\n' "Oracle Cloud: ${a:-rates unknown}" "PERSIST=disk: its disk keeps everything while it's stopped. It powers itself off after ${idle:-15} minutes unused (IDLE_STOP), which saves nothing at Oracle: a billed size is billed as running until it's stopped there (metacomputer --vm $name stop); stopped, only its disk is billed (docs/remote.md, What a stopped computer costs)." >&2
}
pv_oci_vm_delete_what() { printf '%s\n' "  its instance on $(remote_where) and the instance's disk (everything on it), its security group, and metacomputer's network there once no computer of yours uses it"; }

# stop (bin/metacomputer do_stop_remote): what it says, and what it needs.
pv_oci_stop_needs_credentials() {
  die "stopping $VM_NAME needs $(remote_where)'s credentials in this command's environment (docs/remote.md). Left alone, it powers itself off after ${IDLE_STOP:-15} minutes unused, but $(remote_where) counts it as running (and bills a billed size) until it's stopped there"
}
pv_oci_stop_saying() { say "stopping $VM_NAME (the guest shuts down, then $(remote_where) stops it; its disk is kept. If it doesn't answer, this can take up to 15 minutes: --force powers it off at once)"; }
