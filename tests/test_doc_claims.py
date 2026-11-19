#!/usr/bin/env python3
"""The docs' security promises, pinned to the code and settings that keep them. No VM, about a second.

    python3 tests/test_doc_claims.py

An independent review once found SECURITY.md, the README and the CHANGELOG promising more than the code did
(approval defaults, what recordings show, what the coordinate guard catches). This keeps them together:
- CLAIMS: one row per promise: the document, a phrase it must contain (a regular expression, matched with the
  document's whitespace folded to single spaces, so line breaks don't matter), and a check against the code or
  setting that keeps it. Numbers the phrase captures are compared with the code's.
- Broader checks below it: every settings table against config/defaults.env, the code's own fallbacks for the
  security settings, the gateway's refusal list both ways, the mcomp-vm flags, pinned versions and digests,
  what the docs say about recordings, the tests release.sh runs, and absolute claims no doc may make.

When a row fails, the doc or the code changed without the other. Make them agree again; if the promise got
weaker, say so in the CHANGELOG. A promise that's gone (a feature removed) loses its row in the same change.
"""
import ast
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:600]})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


# --- reading the docs and the code ---------------------------------------------------------------------------
_cache = {}


def raw(rel):
    if rel not in _cache:
        with open(os.path.join(LAB, rel), encoding="utf-8") as f:
            _cache[rel] = f.read()
    return _cache[rel]


def doc(rel):
    """The document with every run of whitespace (line breaks, list indentation) folded to one space."""
    return re.sub(r"\s+", " ", raw(rel))


def num(expr):
    """A small integer expression as the code writes it: 600_000, 32 << 20, 8 * 3600, 30 * 86400."""
    tree = ast.parse(expr.strip().replace("_", ""), mode="eval")

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            ops = {ast.LShift: lambda: a << b, ast.Mult: lambda: a * b, ast.Add: lambda: a + b, ast.Sub: lambda: a - b}
            if type(n.op) in ops:
                return ops[type(n.op)]()
        raise ValueError(f"not a simple number: {expr!r}")
    return ev(tree.body)


def swift(rel, pattern):
    """The first group of PATTERN in a Swift file (vault/...), as written."""
    m = re.search(pattern, raw(rel))
    if not m:
        raise LookupError(f"{rel}: no match for {pattern}")
    return m.group(1)


def swift_num(rel, pattern):
    return num(swift(rel, pattern))


def defaults():
    """config/defaults.env as KEY -> its literal value (quotes and comments removed, nothing expanded)."""
    out = {}
    for line in raw("config/defaults.env").splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            words = shlex.split(m.group(2), comments=True)
            out[m.group(1)] = words[0] if words else ""
    return out


D = defaults()


@contextlib.contextmanager
def clean_env():
    """No MCOMP_ settings in the environment: what the code does when nothing is set."""
    saved = dict(os.environ)
    for k in list(os.environ):
        if k.startswith("MCOMP_"):
            del os.environ[k]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


with clean_env():
    import actionlog  # noqa: E402
    import events  # noqa: E402
    import mcp_hub  # noqa: E402
    import owners  # noqa: E402
    import vm_shell  # noqa: E402
    import wirelimits  # noqa: E402

PROXY, CMDS, IMPORT, APPROVALS, CRYPTO, SERVER = (f"vault/{f}.swift" for f in
                                                  ("Proxy", "Commands", "Import", "Approvals", "Crypto", "Server"))
FILL = "provisioning/guest/rootfs/opt/metacomputer/bin/secret-fill"


def set_default():
    """`secrets set` without --approval."""
    return swift(CMDS, r'let approval = a\.last\("approval"\) \?\? "(\w+)"')


def imports_ask_each():
    """Every login, 2FA code and session the import commands write starts at `each`, none at `session`."""
    found = re.findall(r'approval: "(\w+)"', raw(IMPORT))
    return len(found) >= 3 and set(found) == {"each"}, found


def fill_types():
    """Which <input> types each field may be filled into (the VM's secret-fill)."""
    m = re.search(r"FIELD_INPUT_TYPES = (\{.*?\n\})", raw(FILL), re.S)
    return ast.literal_eval(m.group(1))


def mb(n):
    return n >> 20


# --- the table -------------------------------------------------------------------------------------------------
# (document, phrase that must be in it, check(match) -> bool or (bool, detail)). Keep rows short: one promise each.
CLAIMS = [
    # The vault's approvals (vault/Commands.swift, vault/Import.swift, vault/Approvals.swift, vault/Crypto.swift)
    ("SECURITY.md", r"the default, `--approval session`, doesn't ask once the vault is unlocked",
     lambda m: set_default() == "session"),
    ("SECURITY.md", r"secrets you add with `set` default to `--approval session` and ask only with `--approval each`",
     lambda m: set_default() == "session"),
    ("README.md", r"secrets you add with `set` don't \(`--approval session`, the default\)", lambda m: set_default() == "session"),
    ("docs/guide.md", r"a secret you add with `set` goes ahead once the vault is unlocked \(`--approval session`, the default\)",
     lambda m: set_default() == "session"),
    ("SECURITY.md", r"Imported secrets and sealed browser logins ask at every use \(`each`\)", lambda m: imports_ask_each()),
    ("README.md", r"Imported secrets then ask before every use", lambda m: imports_ask_each()),
    ("docs/guide.md", r"imported logins and saved browser logins ask every time until you relax them",
     lambda m: imports_ask_each()),
    ("docs/moving-from-spaces.md", r"Everything you import asks before every use by default \(`each`\)",
     lambda m: imports_ask_each()),
    ("SECURITY.md", r"the unlock's default `--confirm session`",
     lambda m: swift(CMDS, r'let confirm = a\.last\("confirm"\) \?\? "(\w+)"') == "session"),
    ("docs/guide.md", r"until it expires \(at most (\d+) days\)",
     lambda m: int(m[1]) * 86400 == swift_num(APPROVALS, r"let maxRuleSeconds = ([^\n]+)")),
    ("SECURITY.md", r"PBKDF2-HMAC-SHA256, ([\d,]+) rounds",
     lambda m: int(m[1].replace(",", "")) == swift_num(CRYPTO, r"let kdfIterations = ([^\n]+)")
     == swift_num(CRYPTO, r"let kdfMinIterations = ([^\n]+)")),
    ("SECURITY.md", r"The built-in list \(`vault/Approvals\.swift`\)",
     lambda m: idp_table_matches()),
    # The API proxy (vault/Proxy.swift, vault/Commands.swift, vault/Server.swift)
    ("SECURITY.md", r"holds at most (\d+) connections from one VM at once",
     lambda m: int(m[1]) == swift_num(PROXY, r"var slots = (\d+)")),
    ("SECURITY.md", r"one more waits up to (\d+) s, then gets 503",
     lambda m: int(m[1]) == swift_num(PROXY, r"var wait: TimeInterval = (\d+)")),
    ("SECURITY.md", r"at most (\d+) KB, and all of it within (\d+) seconds of the connection",
     lambda m: int(m[1]) << 10 == swift_num(PROXY, r"let requestHeadLimit = ([^\n]+)")
     and int(m[2]) == swift_num(PROXY, r"var head: TimeInterval = (\d+)")),
    ("SECURITY.md", r"only then reads the body: at most (\d+) MB, within (\d+) seconds",
     lambda m: int(m[1]) << 20 == swift_num(PROXY, r"let requestBodyLimit = ([^\n]+)")
     and int(m[2]) == swift_num(PROXY, r"let bodyDeadline: TimeInterval = (\d+)")),
    ("SECURITY.md", r"gives the whole answer at most (\d+) seconds from the moment it sends the request",
     lambda m: int(m[1]) == swift_num(PROXY, r"let defaultAnswerTimeout = (\d+)")
     and "var answer = TimeInterval(defaultAnswerTimeout)" in raw(PROXY)),
    ("SECURITY.md", r"\(or the secret's own `--answer-timeout`, (\d+) seconds to (\d+)\)",
     lambda m: (int(m[1]), int(m[2])) == (swift_num(PROXY, r"let minAnswerTimeout = (\d+)"),
                                          swift_num(PROXY, r"let maxAnswerTimeout = (\d+)"))
     and "n >= minAnswerTimeout, n <= maxAnswerTimeout" in raw(PROXY) and "n >= minAnswerTimeout, n <= maxAnswerTimeout" in raw(CMDS)),
    ("SECURITY.md", r"`--answer-timeout 30m`; (\d+) minutes by default, at most an hour",
     lambda m: int(m[1]) * 60 == swift_num(PROXY, r"let defaultAnswerTimeout = (\d+)")
     and swift_num(PROXY, r"let maxAnswerTimeout = (\d+)") == 3600),
    ("SECURITY.md", r"`metacomputer secrets token openai \[--ttl 15m\]` \(at most (\d+) hours, and never past the unlock's end\)",
     lambda m: int(m[1]) * 3600 == swift_num(PROXY, r"let tokenMaxTTL = ([^\n]+)")
     and "ttl <= tokenMaxTTL else" in raw(CMDS) and "min(ttlAsked, tokenMaxTTL, remaining())" in raw(SERVER)),
    # Renewing a token: never more than tokenMaxLifetime after issue (the longest unlock), never past the unlock
    ("SECURITY.md", r"more than \*\*(\d+) hours after the token was made\*\*, however often it's renewed \(the longest unlock",
     lambda m: int(m[1]) * 3600 == swift_num(PROXY, r"let tokenMaxLifetime = ([^\n]+)")
     == swift_num(CMDS, r"seconds <= ([0-9 *]+) else \{ throw usageError\(\"--for")
     and "let cap = min(expires, t.limit)" in raw(SERVER) and "let target = min(until, t.limit)" in raw(PROXY)),
    ("SECURITY.md", r"The new expiry is `--ttl` from now \(by default, the lifetime the token was made with, at most (\d+) hours\)",
     lambda m: int(m[1]) * 3600 == swift_num(PROXY, r"let tokenMaxTTL = ([^\n]+)")
     and "let step = min(asked ?? t.ttl, tokenMaxTTL)" in raw(SERVER)),
    # Who renews: another requester asks, even for a `session` secret; the token's own session and you at a terminal don't
    ("SECURITY.md", r"renewing a token made for another requester asks at the unlock terminal, like an `each` secret, even for a "
                    r"`session` secret",
     lambda m: 'let otherRequester = by == "other"' in raw(SERVER)
     and "if confirmEach || s.approval == \"each\" || idp || otherRequester {" in raw(SERVER)
     and 'return "same"' in raw(SERVER) and 'who.claudePID == nil && interactive && inTerminalForeground(who.clientPID)' in raw(SERVER)
     and "func inTerminalForeground" in raw("vault/Util.swift")),
    ("SECURITY.md", r"A renewal that was asked about gets the expiry its question named, never a later one",
     lambda m: "hub.renew(id: t.id, vm: t.vm, until: first)" in raw(SERVER)),
    ("SECURITY.md", r"So is `lock`: every token ends when it's accepted, before the unlock process has stopped the proxy",
     lambda m: raw(SERVER).count("hub.lockdown()          // every token ends now") == 2),
    # The answer: 120 s without a byte, whatever --answer-timeout says; the vault file checked while it streams
    ("SECURITY.md", r"The API must send its first byte, and each next one, within (\d+) seconds, whatever the limit",
     lambda m: f"plainSessionConfig(timeout: {m[1]})" in raw(PROXY) and "cfg.timeoutIntervalForResource = limit + 30" in raw(PROXY)),
    ("SECURITY.md", r"the API must still send its first byte, and each next one, within (\d+) seconds, whatever the limit",
     lambda m: f"plainSessionConfig(timeout: {m[1]})" in raw(PROXY)),
    ("SECURITY.md", r"A vault file this unlock can no longer open and verify \(a rekey, a recover, a damaged or missing file\) ends "
                    r"every token at once and locks the vault, checked in the unlock loop and while a question waits too",
     lambda m: "guard let p = try? loadPayload(paths, key) else {\n            endAll()" in raw(PROXY)
     and "guard stat(paths.vault, &st) == 0 else {\n            endAll()" in raw(PROXY)
     and raw(SERVER).count("checkVault()") >= 4 and "stopReason = \"the vault file can't be read and verified" in raw(SERVER)),
    ("SECURITY.md", r"the vault file is checked for changes four times a second while an answer streams",
     lambda m: "while stream.done.wait(timeout: .now() + 0.25) == .timedOut {\n            refreshIfVaultChanged()" in raw(PROXY)),
    ("SECURITY.md", r"the token \(`mct_` and (\d+) random bits\)",
     lambda m: int(m[1]) == 8 * swift_num(PROXY, r'"mct_" \+ base64url\(randomBytes\((\d+)\)\)')),
    ("SECURITY.md", r"\*\*127\.0\.0\.1:(\d+) inside the VM\*\* \(`SECRETS_PROXY_PORT`\)",
     lambda m: m[1] == D["SECRETS_PROXY_PORT"] == re.search(
         r"PermitListen 127\.0\.0\.1:(\d+)", raw("provisioning/guest/rootfs/etc/ssh/sshd_config.d/16-metacomputer-proxy.conf"))[1]),
    # What a recording shows of a filled login: the fill puts a password only into a password field (dots on
    # screen), and usernames and codes into ordinary text fields (visible).
    ("SECURITY.md", r"On screen and in recordings, passwords show as dots\. - Usernames and one-time codes are ordinary "
                    r"text on the page, so snapshots, screenshots and recordings show them",
     lambda m: fill_types_match()),
    ("README.md", r"Passwords show as dots on the page, but usernames and one-time codes are visible, in recordings too",
     lambda m: fill_types_match()),
    ("docs/guide.md", r"Passwords filled by `metacomputer secrets` show as dots, but usernames and codes are visible",
     lambda m: fill_types_match()),
    # Optional hardening: off by default (config/defaults.env and the code's own reading of an unset setting)
    ("SECURITY.md", r"This page covers three \*\*optional\*\* protections\. All are off by default",
     lambda m: hardening_off()),
    ("README.md", r"\*\*Opt-in,\*\* so out of the box metacomputer behaves the way Cua Driver does on its own",
     lambda m: hardening_off()),
    ("SECURITY.md", r"\| `SSH_AUTH=cert` \| A fresh key and a (\d+)-minute certificate",
     lambda m: D["SSH_CERT_TTL"] == f"{m[1]}m"),
    ("SECURITY.md", r"It's on by default \(`MCP_HUB=on`\)",
     lambda m: D["MCP_HUB"] == "on" and mcp_hub.mode({}) == "on"),
    ("SECURITY.md", r"\*\*The coordinate guard \(`COORD_GUARD=on`, the default\)\.\*\*",
     lambda m: D["COORD_GUARD"] == "on" and mcp_hub.settings_of({})["coord_guard"] == "on"),
    ("SECURITY.md", r"`VM_SHELL=auto` \(the default\) is on, except while `DRIVER_MODE=bounded`",
     lambda m: D["VM_SHELL"] == "auto" and vm_shell.shell_allowed("auto", "bounded")[0] is False
     and vm_shell.shell_allowed("auto", "standard")[0] is True),
    ("SECURITY.md", r"`SHELL_LOG=auto` \(the default\) records every run in the signed action log",
     lambda m: D["SHELL_LOG"] == "auto" and vm_shell.logging_on("auto", "on") is True
     and vm_shell.logging_on("auto", "off") is False),
    # The hub (lib/mcp_hub.py, lib/wirelimits.py, lib/owners.py)
    ("SECURITY.md", r"read with a fixed limit \((\d+) MB\) before any parsing",
     lambda m: int(m[1]) == mb(mcp_hub.MAX_UPSTREAM_MSG)),
    ("SECURITY.md", r"a VM that stops reading what the hub sends for (\d+) s",
     lambda m: int(m[1]) == mcp_hub.WRITE_TIMEOUT_S),
    ("SECURITY.md", r"JSON nested more than (\d+) levels deep", lambda m: int(m[1]) == wirelimits.MAX_JSON_DEPTH),
    ("SECURITY.md", r"a line with more than a million values", lambda m: wirelimits.MAX_JSON_NODES == 1_000_000),
    ("SECURITY.md", r"is hung up on after (\d+) in a minute",
     lambda m: int(m[1]) == mcp_hub.BAD_UPSTREAM_MAX and mcp_hub.BAD_UPSTREAM_WINDOW_S == 60),
    ("SECURITY.md", r"A client's message is limited to (\d+) MB, and at most (\d+) MB or (\d+) messages may wait",
     lambda m: (int(m[1]), int(m[2]), int(m[3])) == (mb(mcp_hub.MAX_CLIENT_MSG), mb(mcp_hub.MAX_BACKLOG_BYTES),
                                                    mcp_hub.MAX_BACKLOG_MSGS)),
    ("SECURITY.md", r"a refresh stops after (\d+) pages, ([\d,]+) tools, (\d+) MB, (\d+) s",
     lambda m: (int(m[1]), int(m[2].replace(",", "")), int(m[3]), int(m[4])) ==
     (mcp_hub.MAX_TOOL_PAGES, mcp_hub.MAX_TOOLS, mb(mcp_hub.MAX_TOOLS_BYTES), mcp_hub.REFRESH_DEADLINE_S)),
    ("SECURITY.md", r"at most (\d+) windows each and ([\d,]+) in all",
     lambda m: (int(m[1]), int(m[2].replace(",", ""))) == (mcp_hub.MAX_LAYOUT_WINDOWS, mcp_hub.MAX_LAYOUT_WINDOWS_TOTAL)),
    ("SECURITY.md", r"More than (\d+) windows open .{0,80}every pixel action is refused \(`too_many_windows`\)",
     lambda m: int(m[1]) == mcp_hub.MAX_LAYOUT_WINDOWS and '"too_many_windows"' in raw("lib/mcp_hub.py")),
    ("SECURITY.md", r"drained with a (\d+) KB memory tail and at most (\d+) MB per connection",
     lambda m: (int(m[1]) << 10, int(m[2]) << 20) == (wirelimits.STDERR_KEEP, wirelimits.STDERR_FILE_MAX)),
    ("SECURITY.md", r"Past the activity window \((\d+) min, `MCOMP_ACTIVE_S`\)",
     lambda m: int(m[1]) * 60 == owners.ACTIVE_S == mcp_hub.LEASE_MAX_S),
    ("SECURITY.md", r"\(SIGTERM, so the action log's relay can sign its end; (\d+) s later SIGKILL\)",
     lambda m: int(m[1]) == mcp_hub.TRANSPORT_GRACE_S),
    ("SECURITY.md", r"in a folder only you can open \(0700\), the socket itself 0600",
     lambda m: "os.chmod(self.paths[\"sock\"], 0o600)" in raw("lib/mcp_hub.py") and "mode=0o700" in raw("lib/mcp_hub.py")),
    # The coordinate guard: what it checks and the codes it refuses with
    ("SECURITY.md", r"\(code `(windows_changed_since_screenshot)`\)", lambda m: mcp_hub.COORD_CODE == m[1]),
    ("SECURITY.md", r"\(code `(pixels_after_window_change)`\)", lambda m: mcp_hub.BATCH_CODE == m[1]),
    ("config/defaults.env", r"COORD_GUARD=on # on: pixel input \(click x,y, drag, scroll x,y, run_steps steps with x,y\)",
     lambda m: {"click", "drag", "scroll"} <= mcp_hub.PIXEL_TOOLS),
    ("TESTING.md", r"drag, scroll and `type_text` x,y checked; `press_key` and `hotkey` with x,y \(alone and as a step\) checked",
     lambda m: {"drag", "scroll", "type_text", "press_key", "hotkey"} <= mcp_hub.PIXEL_TOOLS),
    ("SECURITY.md", r"`run_script`'s `\[x, y\]` targets and `parallel_mouse_drag`'s paths aren't checked",
     lambda m: not ({"run_script", "parallel_mouse_drag"} & mcp_hub.PIXEL_TOOLS)),
    # The event stream's limits (lib/events.py)
    ("SECURITY.md", r"at most (\d+) events per family in any (\d+) minutes",
     lambda m: (int(m[1]), int(m[2]) * 60) == (events.RATE_CAP, events.RATE_WINDOW)),
    ("docs/events.md", r"At most (\d+) events of a family in any (\d+) minutes",
     lambda m: (int(m[1]), int(m[2]) * 60) == (events.RATE_CAP, events.RATE_WINDOW)),
    ("docs/guide.md", r"\(10 per family in 10 minutes; a phone also (\d+) an hour\)",
     lambda m: (int(m[1]), 3600) in events.PHONE_LIMITS and (10, 600) in events.PHONE_LIMITS),
    # Blocked connections: the first after a quiet spell at once, the rest batched (lib/events.py)
    ("SECURITY.md", r"The first blocked connection after (\d+) quiet minutes is reported at once \(within a few seconds; at "
                    r"most (\d+) such reports in any hour\); what follows it is batched, at most once a minute, and "
                    r"repeated reports of the same destinations slow down \(up to once an hour\)",
     lambda m: (int(m[1]) * 60, (int(m[2]), 3600)) == (events.NETGW_QUIET_S, events.NETGW_FIRST_CAP)
     and events.NETGW_POLL_S <= 5 and (events.NETGW_BATCH_S, events.NETGW_MAX_S) == (60, 3600)),
    ("docs/events.md", r"When the cycle itself happens to read that first one, it's sent at once too, before the cycle's health probe",
     lambda m: 'if ev and (deliver_now or (ev.get("data") or {}).get("first") is True):' in raw("lib/events.py")
     and raw("lib/events.py").index('step("blocked connections", blocked)') < raw("lib/events.py").index('step("health", probe)')),
    ("docs/events.md", r"The first blocked connection after (\d+) minutes with none goes out at once \(the watcher looks every "
                       r"(\d+) seconds\), at most (\d+) times in any hour\. What follows it is batched: at most one event a minute",
     lambda m: (int(m[1]) * 60, int(m[2]), (int(m[3]), 3600), events.NETGW_BATCH_S)
     == (events.NETGW_QUIET_S, events.NETGW_POLL_S, events.NETGW_FIRST_CAP, 60)),
    # The shell channel (lib/vm_shell.py)
    ("docs/guide.md", r"`--timeout SECONDS` \(default (\d+), at most (\d+)\)",
     lambda m: (int(m[1]), int(m[2])) == (vm_shell.DEFAULT_TIMEOUT, vm_shell.MAX_TIMEOUT)),
    ("docs/guide.md", r"`--max-output BYTES` \(default (\d+) KB per stream, at most (\d+) MB\)",
     lambda m: (int(m[1]) << 10, int(m[2]) << 20) == (vm_shell.DEFAULT_OUTPUT, vm_shell.MAX_OUTPUT)),
    # The gateway: its refusals are config/netgw-rules.json, which the start uses
    ("docs/guide.md", r"\*\*Refused by \[config/netgw-rules\.json\]\(\.\./config/netgw-rules\.json\):\*\* (.{0,400}?)\. - \*\*Allowed:\*\* everything else",
     lambda m: gateway_matches(m[1], GUIDE_TERMS)),
    ("architecture.md", r"refused by `config/netgw-rules\.json`: (.{0,300}?) - everything else \(the public internet\): allowed",
     lambda m: gateway_matches(m[1], ARCH_TERMS)),
    ("README.md", r"The gateway on your Mac refuses connections to your Mac, your LAN, your tailnet, cloud metadata and outbound mail",
     lambda m: gateway_matches("your LAN, your tailnet, cloud metadata and outbound mail", README_TERMS, whole=False)
     and gateway_rules()["block_host_addrs"] is True),
    ("docs/guide.md", r"\*\*Always refused:\*\* your Mac's loopback and every address of its own interfaces",
     lambda m: gateway_rules()["block_host_addrs"] is True and gateway_rules()["default_accept"] is True
     and '"rules": "$LAB_ROOT/config/netgw-rules.json"' in raw("scripts/lib.sh")
     and '-check-rules "$LAB_ROOT/config/netgw-rules.json"' in raw("scripts/lib.sh")),
    # mcomp-vm leaves out host sharing: the flags the docs name are the ones scripts/lib.sh passes
    ("architecture.md", r"`lume run (--no-share-clipboard / --no-share-audio / --no-share-rosetta / --no-host-sharing)`",
     lambda m: share_flags_match(re.findall(r"--[a-z-]+", m[1]))),
    ("docs/guide.md", r"plus (`--no-share-clipboard`, `--no-share-audio`, `--no-share-rosetta` and `--no-host-sharing`), and "
                      r"metacomputer starts the VM without all three unless you turn one on",
     lambda m: share_flags_match(re.findall(r"--[a-z-]+", m[1]))
     and all(D[k] == "off" for k in ("SHARE_CLIPBOARD", "SHARE_AUDIO", "SHARE_ROSETTA"))),
    # Pinned versions and digests quoted in the docs (config/defaults.env)
    ("architecture.md", r"\| Lume \(alternative\) \| ([\d.]+) \(Developer ID, notarized; sha256 `([0-9a-f]+)…`\)",
     lambda m: m[1] == D["LUME_VERSION"] and D["LUME_TARBALL_SHA256"].startswith(m[2])),
    ("architecture.md", r"Lume ([\d.]+) \(`lume-v([\d.]+)`, `[0-9a-f]+`\) \+ one patch",
     lambda m: m[1] == m[2] == D["LUME_VERSION"]),
    ("architecture.md", r"\| cua-netgw \| built from trycua/cua `([0-9a-f]+)`", lambda m: D["CUA_SRC_COMMIT"].startswith(m[1])),
    ("architecture.md", r"\| rclone \(only for S3 volumes\) \| ([\d.]+) osx-arm64 \(sha256 `([0-9a-f]+)…`; `SHA256SUMS` signed by "
                        r"`([0-9A-F]+)…([0-9A-F]+)`\) \| `\.cache/rclone-v([\d.]+)/rclone`",
     lambda m: m[1] == m[5] == D["RCLONE_VERSION"] and D["RCLONE_SHA256"].startswith(m[2])
     and fpr(D["RCLONE_SIGNING_KEY_FPR"], m[3], m[4])),
    ("architecture.md", r"\| Cua Driver \| ([\d.]+) linux-arm64 \(sha256 `([0-9a-f]+)…`\) \| guest `/opt/cua-driver/([\d.]+)`",
     lambda m: m[1] == m[3] == D["CUA_DRIVER_VERSION"] and D["CUA_DRIVER_SHA256"].startswith(m[2])),
    ("architecture.md", r"\| Ubuntu \| 24\.04 LTS arm64 cloud image, release (\d+) \(sha256 `([0-9a-f]+)…`, SHA256SUMS signed by "
                        r"`([0-9A-F]+)…([0-9A-F]+)`\)",
     lambda m: m[1] == D["UBUNTU_RELEASE"] and D["UBUNTU_IMAGE_SHA256"].startswith(m[2])
     and fpr(D["UBUNTU_SIGNING_KEY_FPR"], m[3], m[4])),
    ("architecture.md", r"\| Firefox \| Mozilla APT repo \(arm64 \.deb; key `([0-9A-F]+)…([0-9A-F]+)`\)",
     lambda m: fpr(D["MOZILLA_KEY_FPR"], m[1], m[2])),
    ("architecture.md", r"\| Google Chrome \| Google's APT repo \(arm64 \.deb; key `([0-9A-F]+)…([0-9A-F]+)`\)",
     lambda m: fpr(D["GOOGLE_KEY_FPR"], m[1], m[2])),
    ("architecture.md", r"cua-driver ([\d.]+) mcp \(stdio", lambda m: m[1] == D["CUA_DRIVER_VERSION"]),
    ("docs/guide.md", r"\*\*Cua Driver ([\d.]+) on this desktop\*\*", lambda m: m[1] == D["CUA_DRIVER_VERSION"]),
    ("docs/guide.md", r"It's Lume ([\d.]+) plus `--no-share-clipboard`", lambda m: m[1] == D["LUME_VERSION"]),
    ("THIRD_PARTY_NOTICES.md", r"trycua/cua release `lume-v([\d.]+)`", lambda m: m[1] == D["LUME_VERSION"]),
    ("THIRD_PARTY_NOTICES.md", r"trycua/cua release `cua-driver-rs-v([\d.]+)`", lambda m: m[1] == D["CUA_DRIVER_VERSION"]),
    ("THIRD_PARTY_NOTICES.md", r"rclone/rclone release `v([\d.]+)`", lambda m: m[1] == D["RCLONE_VERSION"]),
]


def fpr(full, head, tail):
    """An abbreviated fingerprint or digest (`D2EB4462…7C81`) is the pinned one's start and end."""
    return full.upper().startswith(head.upper()) and full.upper().endswith(tail.upper()) and len(head) >= 6


def idp_table_matches():
    """SECURITY.md's identity-provider table lists exactly vault/Approvals.swift's identityProviderHosts."""
    code = re.search(r"let identityProviderHosts: \[String\] = \[(.*?)\n\]", raw(APPROVALS), re.S).group(1)
    code_hosts = set(re.findall(r'"([^"]+)"', code))
    sec = raw("SECURITY.md")
    table = sec[sec.index("| Provider | Sites |"):]
    table = table[:table.index("\n\n")]
    doc_hosts = set()
    for row in table.splitlines()[2:]:
        cells = row.split("|")
        sites = cells[2]
        if "any subdomain of" in sites:
            doc_hosts |= {"*." + h for h in re.findall(r"`([^`]+)`", sites)}
        else:
            doc_hosts |= set(re.findall(r"`([^`]+)`", sites))
    return doc_hosts == code_hosts, {"only in SECURITY.md": sorted(doc_hosts - code_hosts),
                                     "only in the code": sorted(code_hosts - doc_hosts)}


def fill_types_match():
    t = fill_types()
    visible = t["username"] | t["otp"]
    return t["password"] == {"password"} and "password" not in visible, t


def hardening_off():
    """The three optional protections are off by default, in config/defaults.env and in the code that reads
    them when nothing is set (scripts/lib.sh exports them; a client started without it reads its own defaults)."""
    with clean_env():
        log = actionlog.settings()
        sh = vm_shell.settings({})
    ok = (D["ACTION_LOG"], D["SSH_AUTH"], D["DRIVER_MODE"], D["ACTION_LOG_TEXT"]) == ("off", "key", "standard", "redact")
    ok = ok and not log["on"] and not log["keep_text"] and sh["driver_mode"] == "standard" and sh["action_log"] == "off"
    return ok, (D["ACTION_LOG"], D["SSH_AUTH"], D["DRIVER_MODE"], log, sh)


def gateway_rules():
    with open(os.path.join(LAB, "config", "netgw-rules.json")) as f:
        return json.load(f)


SMTP = frozenset({25, 465, 587})
# Words the docs use for the gateway's refusals -> the rules (net, ports) each one stands for.
GUIDE_TERMS = {
    "10/8": {("10.0.0.0/8", None)}, "172.16/12": {("172.16.0.0/12", None)}, "192.168/16": {("192.168.0.0/16", None)},
    "100.64.0.0/10": {("100.64.0.0/10", None)}, "link-local": {("169.254.0.0/16", None), ("fe80::/10", None)},
    "IPv6 ULA": {("fc00::/7", None)}, "benchmark": {("198.18.0.0/15", None)}, "reserved": {("240.0.0.0/4", None)},
    "outbound SMTP (ports 25, 465, 587)": {("0.0.0.0/0", SMTP), ("::/0", SMTP)},
}
ARCH_TERMS = dict(
    {k: GUIDE_TERMS[k] for k in ("100.64.0.0/10", "link-local", "benchmark", "reserved")},
    **{"RFC 1918": GUIDE_TERMS["10/8"] | GUIDE_TERMS["172.16/12"] | GUIDE_TERMS["192.168/16"],
       "ULA": GUIDE_TERMS["IPv6 ULA"], "outbound SMTP": GUIDE_TERMS["outbound SMTP (ports 25, 465, 587)"]})
README_TERMS = {"your LAN": ARCH_TERMS["RFC 1918"], "your tailnet": {("100.64.0.0/10", None), ("fc00::/7", None)},
                "cloud metadata": {("169.254.0.0/16", None)}, "outbound mail": ARCH_TERMS["outbound SMTP"]}


def gateway_matches(text, terms, whole=True):
    """Every refusal the text names is a refusing rule, and (whole) every refusing rule is named."""
    rules = {(r["net"], frozenset(r["ports"]) if r.get("ports") else None)
             for r in gateway_rules()["rules"] if r.get("accept") is False}
    missing_words = [w for w in terms if w not in text]
    named = set().union(*terms.values())
    not_in_rules = named - rules
    unnamed = rules - named if whole else set()
    return not (missing_words or not_in_rules or unnamed), {
        "terms not in the text": missing_words, "named but not refused by a rule": sorted(map(str, not_in_rules)),
        "refused but not named": sorted(map(str, unnamed))}


def share_flags_match(doc_flags):
    """The docs' mcomp-vm flags are exactly the ones host_sharing_args in scripts/lib.sh checks for and passes,
    one per SHARE_* setting."""
    lib = raw("scripts/lib.sh")
    body = lib[lib.index("host_sharing_args() {"):]
    body = body[:body.index("\n}\n")]
    code_flags = set(re.findall(r"--no-[a-z-]+", body))
    per_setting = dict(re.findall(r'\$\{(SHARE_[A-Z]+):-off\}" = on \] \|\| a="\$a (--no-share-[a-z]+)"', body))
    want = {"SHARE_CLIPBOARD": "--no-share-clipboard", "SHARE_AUDIO": "--no-share-audio", "SHARE_ROSETTA": "--no-share-rosetta"}
    return set(doc_flags) == code_flags and per_setting == want, (sorted(doc_flags), sorted(code_flags), per_setting)


def run_claims():
    print("each promise in the docs, against the code or setting that keeps it")
    for rel, pattern, test in CLAIMS:
        name = f"{rel}: /{pattern[:90]}{'…' if len(pattern) > 90 else ''}/"
        m = re.search(pattern, doc(rel))
        if not m:
            check(name, False, "the phrase isn't in the document any more: update the row with the doc's new wording, "
                               "or remove it if the promise was dropped on purpose")
            continue
        try:
            res = test(m)
        except Exception as e:      # a pattern in the code that moved: say which
            res = (False, f"{type(e).__name__}: {e}")
        ok, detail = res if isinstance(res, tuple) else (res, f"the doc says {m.group(0)[:200]!r}")
        check(name, bool(ok), detail)


# --- settings: the tables in the docs, and the code's fallbacks ---------------------------------------------
# The settings whose default is the doc's computed value (config/defaults.env leaves them empty).
COMPUTED = {"RUN_DIR": ".run/", "LOG_DIR": "logs/", "STORAGE_DIR": "~/.local/share/metacomputer",
            "RECORD_DIR": "recordings/manual/", "ACTION_LOG_KEY": "$SSH_DIR/action-log", "SSH_CA_KEY": "$SSH_DIR/ca",
            "LESSONS_DIR": "~/.local/share/metacomputer/lessons"}
TABLES = ("docs/commands.md", "docs/shell.md")


def settings_tables():
    """Every `| KEY | ... | Default | ...` row in docs/commands.md and docs/shell.md against config/defaults.env."""
    print("settings tables against config/defaults.env")
    rows = 0
    for rel in TABLES:
        col = None
        for line in raw(rel).splitlines():
            if not line.startswith("|"):
                col = None
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if col is None:
                col = next((i for i, c in enumerate(cells) if c == "Default"), -1)
                continue
            if col < 0 or set(cells[0]) <= {"-", " "}:
                continue
            keys = re.findall(r"`([A-Z][A-Z0-9_]+)`", cells[0])
            if not keys or not all(k in D for k in keys):
                continue
            vals = ["" if cells[col].startswith("(none)") else v for v in re.findall(r"`([^`]*)`", cells[col])] \
                or [""]
            if len(vals) == 1 and len(keys) > 1:
                vals = vals * len(keys)
            for k, v in zip(keys, vals):
                rows += 1
                want = D[k]
                if k in COMPUTED:
                    check(f"{rel}: {k}'s default ({v}) is what an empty {k} in config/defaults.env means",
                          v == COMPUTED[k] and want == "", (v, want))
                else:
                    got = v.replace("~/", "$HOME/")
                    check(f"{rel}: {k} defaults to `{v}`", got == want, f"config/defaults.env has {want!r}")
    check("the settings tables were read (rows found)", rows >= 50, rows)


SECURITY_SETTINGS = ("ACTION_LOG", "ACTION_LOG_TEXT", "SSH_AUTH", "DRIVER_MODE", "VM_SHELL", "SHELL_LOG", "COORD_GUARD",
                     "MCP_HUB", "LESSONS", "SHARE_CLIPBOARD", "SHARE_AUDIO", "SHARE_ROSETTA")
FALLBACKS = (re.compile(r"\$\{(?:MCOMP_)?([A-Z][A-Z0-9_]+):-([^}$]+)\}"),
             re.compile(r"""\.get\(\s*["'](?:MCOMP_)?([A-Z][A-Z0-9_]+)["']\s*,\s*["']([^"']+)["']\s*\)"""),
             re.compile(r"""onoff\(\s*env\.get\(\s*["']MCOMP_([A-Z][A-Z0-9_]+)["']\s*\)\s*,\s*["']([^"']+)["']\s*\)"""))


def code_fallbacks():
    """Where the code reads a security setting with a fallback (`${ACTION_LOG:-off}`, `.get("MCOMP_SSH_AUTH",
    "key")`), the fallback is config/defaults.env's value: a client that runs without the config gets the same
    protections the docs describe."""
    print("the code's fallbacks for the security settings")
    files = subprocess.run(["git", "ls-files", "lib", "bin", "scripts", "install.sh", "get.sh"], cwd=LAB,
                           capture_output=True, text=True).stdout.split()
    seen, wrong = {}, []
    for rel in files:
        try:
            text = raw(rel)
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for pat in FALLBACKS:
                for m in pat.finditer(line):
                    k, v = m.group(1), m.group(2)
                    if k in SECURITY_SETTINGS:
                        seen[k] = seen.get(k, 0) + 1
                        if v != D[k]:
                            wrong.append(f"{rel}:{n}: {k} falls back to {v!r}, config/defaults.env says {D[k]!r}")
    check("every fallback for a security setting in lib/, bin/, scripts/ and the installers is its default", not wrong, wrong)
    check("…and the scan found them (at least one for each of the hardening settings)",
          all(seen.get(k) for k in ("ACTION_LOG", "SSH_AUTH", "DRIVER_MODE", "VM_SHELL", "SHELL_LOG", "COORD_GUARD",
                                    "MCP_HUB")), seen)


# --- pins, recordings, the release script's tests, and absolutes ------------------------------------------
def pins():
    print("pinned versions")
    versions = set()
    for rel in public_docs():
        if rel == "CHANGELOG.md":
            continue
        for m in re.finditer(r"(?:Cua Driver|cua-driver|[Dd]river) v?(\d+\.\d+\.\d+)", doc(rel)):
            versions.add(tuple(int(x) for x in m.group(1).split(".")))
    pin = tuple(int(x) for x in D["CUA_DRIVER_VERSION"].split("."))
    check(f"no doc names a Cua Driver newer than the pinned {D['CUA_DRIVER_VERSION']}, and they name that one",
          versions and max(versions) == pin, sorted(versions))
    check("config/defaults.env's download names and URLs carry the pinned versions",
          D["UBUNTU_BASE_URL"].endswith(f"release-{D['UBUNTU_RELEASE']}")
          and D["CUA_DRIVER_TARBALL"] == f"cua-driver-rs-{D['CUA_DRIVER_VERSION']}-linux-arm64.tar.gz"
          and D["RCLONE_BASE_URL"].endswith(f"v{D['RCLONE_VERSION']}") and f"v{D['RCLONE_VERSION']}-" in D["RCLONE_ZIP"]
          and D["MCOMP_VM_TAG"].startswith(f"v{D['LUME_VERSION']}-mcomp."),
          {k: D[k] for k in ("UBUNTU_BASE_URL", "CUA_DRIVER_TARBALL", "RCLONE_BASE_URL", "RCLONE_ZIP", "MCOMP_VM_TAG")})


def recordings():
    """Wherever a doc says a filled password shows as dots, the same paragraph says usernames (and codes) are
    visible: a recording isn't promised to hide what a page shows."""
    print("what the docs say a recording shows")
    bad = []
    for rel in public_docs():
        for para in re.split(r"\n\s*\n", raw(rel)):
            if re.search(r"\bas dots\b", para) and not re.search(r"[Uu]sernames?\b", para):
                bad.append(f"{rel}: {' '.join(para.split())[:160]}")
    check("every 'shows as dots' also says usernames are visible", not bad, bad)


def release_suites():
    """scripts/release.sh runs exactly the no-VM suites CLAUDE.md lists."""
    print("the tests a release runs")
    text = raw("CLAUDE.md")
    bullet = text[text.index("- Tests: "):]
    bullet = bullet[:bullet.index("\n- ")]
    head, _, tail = bullet.partition("(no VM")
    listed = set(re.findall(r"\b(test_[a-z0-9_]+)", head)) - set(re.findall(r"\b(test_[a-z0-9_]+)", tail))
    rel = raw("scripts/release.sh")
    m = re.search(r"NO_VM_SUITES=\((.*?)\)", rel, re.S)
    in_script = set(re.findall(r"\b(test_[a-z0-9_]+)", m.group(1))) if m else set()
    check("scripts/release.sh's NO_VM_SUITES are CLAUDE.md's no-VM tests", listed and listed == in_script,
          {"only in CLAUDE.md": sorted(listed - in_script), "only in release.sh": sorted(in_script - listed)})
    missing = sorted(t for t in in_script if not os.path.exists(os.path.join(LAB, "tests", t + ".py")))
    check("…and each of them exists", not missing, missing)


# Absolute claims about security no doc may make: protections are described, never promised as total.
ABSOLUTES = re.compile(
    r"can(?:not|'t|’t) (?:ever )?be (?:hacked|broken into|breached|compromised|bypassed|escaped)|un-?hackable|hack-?proof|"
    r"unbreakable|impenetrable|invulnerable|bullet-?proof|fool-?proof|tamper-?proof|100 ?% (?:secure|safe|private|protected)|"
    r"(?:completely|perfectly|fully|totally|absolutely|guaranteed|provably) (?:secure|safe|unhackable)|"
    r"military[- ]grade|bank[- ]grade|zero risk|risk-free|impossible to (?:hack|breach|bypass|escape)|"
    r"never be (?:hacked|breached|compromised)", re.I)


def public_docs():
    files = subprocess.run(["git", "ls-files", "*.md", "**/*.md", "plugin/**/plugin.json", ".claude-plugin/*.json"],
                           cwd=LAB, capture_output=True, text=True).stdout.split()
    return sorted(f for f in set(files) if os.path.isfile(os.path.join(LAB, f)) and not f.startswith("tests/fixtures/"))


def absolutes():
    print("no absolute security claims")
    hits = []
    docs = public_docs()
    for rel in docs:
        for n, line in enumerate(raw(rel).splitlines(), 1):
            for m in ABSOLUTES.finditer(line):
                hits.append(f"{rel}:{n}: …{line[max(0, m.start() - 40):m.end() + 40].strip()}…")
    check(f"no doc ({len(docs)} files) says it can't be hacked, is unbreakable, 100% secure or the like", not hits, hits)
    probe = ["It can't be hacked.", "an unbreakable sandbox", "100% secure", "completely secure", "tamper-proof logs"]
    check("…and the pattern catches each of those phrasings", all(ABSOLUTES.search(p) for p in probe),
          [p for p in probe if not ABSOLUTES.search(p)])
    honest = ["tamper-evident", "it doesn't make a hostile agent harmless", "Secure Enclave", "fails closed"]
    check("…while honest wording passes", not any(ABSOLUTES.search(p) for p in honest),
          [p for p in honest if ABSOLUTES.search(p)])


def main():
    run_claims()
    settings_tables()
    code_fallbacks()
    pins()
    recordings()
    release_suites()
    absolutes()
    print(f"\n{len(CLAIMS)} promises pinned; " + (f"FAILED: {', '.join(FAILS)}" if FAILS else "all passed"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
