"""The recognition cases that lib/owners.py (tests/test_session_guard.py) and the vault (tests/test_vault_ops.py, its
test-agent-kind) are both checked against, ready to use: each agent's own (coding-agents/<name>/tests/recognition_cases.json,
in the registry's order) after the shared ones (fixtures/agent_argv.json: the interpreters' rules, and what can't be
told), with `@ROOT@` replaced by a folder of the test's, where the files and links a case names are made (empty files;
a link and the file it points to) and its working folder too.

Each case: "name" the kernel's name for the process, "argv" its exact arguments (null: they can't be read), "exe" the
realpath of its executable (absent: not known), "cwd" its working folder (absent: @ROOT@; null: it can't be read),
"files" and "links" what exists, and "agent" what it is (null: no agent).

    cases = agent_argv_cases.load(root)            # the cases, with their files, links and folders in root
    path = agent_argv_cases.write(root)            # the same, as a JSON file in root (for the vault)
"""
import json
import os

TESTS = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(TESTS, "fixtures", "agent_argv.json")


def fixtures():
    """The case files: the shared one, then each registered agent's that has one."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(TESTS), "lib"))
    from agentkit import registry
    own = [os.path.join(registry.folder(n), "tests", "recognition_cases.json") for n in registry.AGENTS]
    return [FIXTURE] + [f for f in own if os.path.isfile(f)]


def load(root):
    def sub(v):
        return v.replace("@ROOT@", root) if isinstance(v, str) else v
    cases = [c for f in fixtures() for c in json.load(open(f))]
    for c in cases:
        if c.get("argv") is not None:
            c["argv"] = [sub(a) for a in c["argv"]]
        c["exe"] = sub(c.get("exe"))
        c["cwd"] = sub(c["cwd"]) if "cwd" in c else root
        if c["cwd"]:
            os.makedirs(c["cwd"], exist_ok=True)
        for f in c.pop("files", None) or ():
            f = sub(f)
            os.makedirs(os.path.dirname(f), exist_ok=True)
            if not os.path.exists(f):
                open(f, "w").close()
        for link, target in (c.pop("links", None) or {}).items():
            link, target = sub(link), sub(target)
            for p in (link, target):
                os.makedirs(os.path.dirname(p), exist_ok=True)
            if not os.path.exists(target):
                open(target, "w").close()
            if not os.path.lexists(link):
                os.symlink(target, link)
    return cases


def table(owners, c, pid=7):
    """A test process table of one process for case c (lib/owners.py ProcTable, with what's known of it)."""
    return owners.ProcTable({pid: (1, c["name"])}, argvs={pid: c["argv"]}, exes={pid: c["exe"]}, cwds={pid: c["cwd"]})


def write(root):
    path = os.path.join(root, "agent_argv.json")
    with open(path, "w") as f:
        json.dump(load(root), f)
    return path
