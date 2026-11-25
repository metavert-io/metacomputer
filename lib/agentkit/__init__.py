"""metacomputer's agent framework: what shared code knows about the coding agents it is set up for, read from their
folders (coding-agents/<name>/; coding-agents/README.md) so no shared program names one.

    registry.py      the agents, an explicit list: each is a folder coding-agents/<name>/ (its manifest, its recognition,
                     its hooks, its page, its doc-claims and tests); loads an agent's modules
    manifests.py     its manifest (coding-agents/<name>/manifest.json): its words, its command, its install options, the
                     parts of install, learn and bench it takes part in
    recognition.py   how its processes are recognised (coding-agents/<name>/recognition.json), merged for lib/owners.py and
                     written as Swift for the vault's build
    agents.sh        what bash reads (made by scripts/gen-agents.py from the manifests: don't edit by hand)

The rest of the framework is where it was: lib/agent_setup.py (install and uninstall steps, ownership of an entry and a
skill link, locks, replace-if-unchanged), lib/owners.py (whose session a process is), lib/transcripts.py (the session
readers' interface for learn), bench/bench.py (the bench harness)."""
