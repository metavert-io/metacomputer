#!/usr/bin/env python3
"""metacomputer shop: Shopify stores through the user's own Shop account (docs/payments.md).

Shopify's stores ask agents not to script their checkout forms (their /agents.md). Instead, the user signs their own Shop
account in to Shopify's MIT-licensed `shop` CLI (npm install --global @shopify/shop) with Shop's device sign-in, and Shop
Pay pays from it, within the approval and spending limits the user keeps in Shop. metacomputer adds its own approval on
top (the vault's, with a `--kind shop` entry's policy and caps) and its audit log. Metavert never touches the money or the
account: everything here runs on this Mac, between the user's Shop CLI and the user's vault.

  shop status [--json]                      is the CLI installed, and is a Shop account signed in? (never its details)
  shop login [--wait 90]                    Shop's device sign-in: prints the link and code for the user, then waits
  shop complete CHECKOUT --shop DOMAIN --instrument @FILE|- [--account NAME] [--json]
                                            complete a ready Shop checkout: the vault reads it with the CLI you pinned
                                            (secrets shop-cli set), asks, reads it again and completes it only if unchanged
  shop card NAME --spend-request ID [--wait 90]
                                            a spend request's single-use card for a non-Shopify store's card form,
                                            straight into the vault (secrets shop-card)

The CLI's answers can hold the user's addresses, email and saved cards: nothing from them is printed except fixed fields
(a status, a total, an order's label, a sign-in link and code). Completing a checkout and reading a spend request's card
are the vault's: it runs only the Shop CLI the user pinned in their own terminal (`metacomputer secrets shop-cli set`).
`status` and `login` run your own `shop` (tests: MCOMP_SHOP_CLI, a stand-in).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}\.)+[a-z]{2,24}$")
CHECKOUT_RE = re.compile(r"^[A-Za-z0-9:/._?=&-]{1,300}$")
SPEND_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$")


class ShopError(Exception):
    pass


def out(s):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def cli():
    path = os.environ.get("MCOMP_SHOP_CLI") or shutil.which("shop")
    if not path:
        raise ShopError("Shopify's shop CLI isn't installed: npm install --global @shopify/shop (MIT), then `metacomputer shop login`")
    return path


def run_shop(args, timeout=120, stdin=None):
    """(exit status, the CLI's JSON answer or {}): its own words are never printed."""
    try:
        r = subprocess.run([cli()] + args, capture_output=True, text=True, timeout=timeout, input=stdin if stdin is not None else "",
                           env=dict(os.environ, NO_COLOR="1"))
    except subprocess.TimeoutExpired:
        raise ShopError("Shop's CLI didn't answer in time")
    except OSError as e:
        raise ShopError(f"Shop's CLI couldn't run ({type(e).__name__})")
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return r.returncode, obj
    return r.returncode, {}


def error_code(ans):
    e = ans.get("error") if isinstance(ans, dict) else None
    code = e.get("code") if isinstance(e, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else None


def self_cmd():
    return os.environ.get("MCOMP_SELF") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "metacomputer")


def secrets(*args, stdin=None, timeout=400):
    r = subprocess.run([self_cmd(), "secrets", *args], capture_output=True, text=True, timeout=timeout, input=stdin or "")
    res = {}
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            res = json.loads(line)
            break
        except ValueError:
            continue
    return r.returncode, res if isinstance(res, dict) else {}, r.stderr


# --- commands -----------------------------------------------------------------------------------------------------
def cmd_status(a):
    st = {"installed": False, "signed_in": None}
    try:
        st["cli"] = cli()
        st["installed"] = True
        rc, ans = run_shop(["account"], timeout=60)
        if ans.get("ok") is True and isinstance(ans.get("data"), dict):
            st["signed_in"] = ans["data"].get("signed_in") is True
        elif error_code(ans) == "auth_required":
            st["signed_in"] = False
    except ShopError as e:
        st["reason"] = str(e)
    if a.json:
        out(json.dumps(st))
        return 0
    out("Shop CLI:   " + (st.get("cli") or "not installed (npm install --global @shopify/shop)"))
    if st["installed"]:
        out("Shop account: " + ("signed in" if st["signed_in"] else ("not signed in: metacomputer shop login" if st["signed_in"] is False else "couldn't tell")))
    return 0


def cmd_login(a):
    rc, ans = run_shop(["account", "login", "--wait", "0", "--device-name", "metacomputer"], timeout=60)
    data = ans.get("data") if isinstance(ans.get("data"), dict) else {}
    if data.get("signed_in") is True:
        out("A Shop account is signed in already.")
        return 0
    p = data.get("pending") if isinstance(data.get("pending"), dict) else {}
    link, code = p.get("verification_uri_complete") or p.get("verification_uri"), p.get("user_code")
    if not (isinstance(link, str) and link.startswith("https://") and len(link) < 400 and isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9-]{3,20}", code)):
        raise ShopError("Shop's sign-in didn't start" + (f" ({error_code(ans)})" if error_code(ans) else ""))
    out(f"Ask the user to open {link} and approve the sign-in for \"metacomputer\" on Shop's own page (code {code}).")
    out("It's their Shop account: Shop's approval and spending limits apply to every purchase, and metacomputer asks too.")
    if a.wait <= 0:
        return 0
    rc, ans = run_shop(["account", "login", "--wait", str(a.wait), "--device-name", "metacomputer"], timeout=a.wait + 60)
    data = ans.get("data") if isinstance(ans.get("data"), dict) else {}
    if data.get("signed_in") is True:
        out("Signed in.")
        return 0
    out("Not approved yet: run `metacomputer shop login --wait 90` again once they have.")
    return 7


def cmd_complete(a):
    """The vault reads the checkout with the Shop CLI you pinned, asks, reads it again and completes it only if it's
    unchanged (secrets shop-complete): this only hands the request over."""
    if not DOMAIN_RE.match(a.shop or ""):
        raise ShopError("--shop: the store's domain, like example-store.com (seller.domain in Shop's results)")
    if not CHECKOUT_RE.match(a.checkout or ""):
        raise ShopError("CHECKOUT: the checkout id Shop returned")
    if a.account and not NAME_RE.match(a.account):
        raise ShopError("--account: a shop entry's name (secrets set NAME --kind shop)")
    if a.instrument == "-":
        args, stdin = ["--instrument", "-"], sys.stdin.read(65536)
    elif a.instrument.startswith("@"):
        args, stdin = ["--instrument", os.path.abspath(a.instrument[1:])], None
    else:
        raise ShopError("--instrument @FILE or - (stdin): the exact instrument object the checkout offered, never on the command line")
    extra = ["--account", a.account] if a.account else []
    if os.environ.get("MCOMP_VM_NAME"):
        extra += ["--vm", os.environ["MCOMP_VM_NAME"]]
    rc, res, err = secrets("shop-complete", a.checkout, "--shop", a.shop, *args, *extra, stdin=stdin, timeout=700)
    out(json.dumps(res or {"ok": False, "reason": "no answer from the vault"}))
    return rc


def cmd_card(a):
    if not NAME_RE.match(a.name) or not SPEND_RE.match(a.spend_request):
        raise ShopError("usage: metacomputer shop card NAME --spend-request ID [--wait 90]")
    rc, res, err = secrets("shop-card", a.name, "--spend-request", a.spend_request, "--wait", str(a.wait))
    out(json.dumps(res or {"ok": False, "reason": "no answer from the vault"}))
    return rc


def main(argv):
    ap = argparse.ArgumentParser(prog="metacomputer shop", description="Shopify stores through your own Shop account")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("login")
    s.add_argument("--wait", type=int, default=90)
    s = sub.add_parser("complete")
    s.add_argument("checkout")
    s.add_argument("--shop", required=True)
    s.add_argument("--instrument", required=True)
    s.add_argument("--account")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("card")
    s.add_argument("name")
    s.add_argument("--spend-request", required=True)
    s.add_argument("--wait", type=int, default=90)
    a = ap.parse_args(argv)
    if getattr(a, "wait", 0) is not None and not 0 <= getattr(a, "wait", 0) <= 120:
        ap.error("--wait: 0-120 seconds")
    try:
        return {"status": cmd_status, "login": cmd_login, "complete": cmd_complete, "card": cmd_card}[a.cmd](a)
    except ShopError as e:
        sys.stderr.write(f"metacomputer shop: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
