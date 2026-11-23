"""The shared argument cases (fixtures/agent_argv.json) that lib/owners.py (tests/test_session_guard.py) and the vault
(tests/test_vault_ops.py, its test-agent-kind) are both checked against, ready to use: `@ROOT@` replaced by a folder
of the test's, with the links a case names made there (a link and the file it points to), so a case can check that an
argument is read by what it resolves to.

    cases = agent_argv_cases.load(root)            # the cases, with their files and links in root
    path = agent_argv_cases.write(root)            # the same, as a JSON file in root (for the vault)
"""
import json
import os

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "agent_argv.json")


def load(root):
    def sub(v):
        return v.replace("@ROOT@", root) if isinstance(v, str) else v
    cases = json.load(open(FIXTURE))
    for c in cases:
        if c.get("argv") is not None:
            c["argv"] = [sub(a) for a in c["argv"]]
        for link, target in (c.pop("links", None) or {}).items():
            link, target = sub(link), sub(target)
            for p in (link, target):
                os.makedirs(os.path.dirname(p), exist_ok=True)
            if not os.path.exists(target):
                open(target, "w").close()
            if not os.path.lexists(link):
                os.symlink(target, link)
    return cases


def write(root):
    path = os.path.join(root, "agent_argv.json")
    with open(path, "w") as f:
        json.dump(load(root), f)
    return path
