# Privacy: what metacomputer keeps and sends

Last updated: 22 November 2026.

metacomputer is made by Metavert LLC, and [Metavert's privacy policy](https://www.metavert.io/privacy-policy) applies
to it: its "Self-hosted software" section covers software you install and run yourself, like this. This page is the
product documentation that section refers to. It covers this repository, its Claude Code plugin, its other agent packages and the
setup it adds to Codex, OpenClaw, Hermes Agent, Gemini CLI and Antigravity CLI. metacomputer runs on your Mac. **Metavert runs no server for it and receives nothing from it**: there is no
account, no telemetry, no analytics and no crash reporting. What leaves your Mac goes where you, your coding agent
or the tasks you give it send it, as described below.

## What stays on your Mac

- **The virtual computer** and everything in it: files, browser profiles, the sites you sign in to there.
- **Records metacomputer keeps:** recordings and screenshots, the action log, task folders, the event spool, its
  settings, which computer each coding-agent session uses (process ids, VM names and settings paths, removed once the
  session ends), and the lessons `metacomputer learn` proposes. `learn` reads your Claude Code, Codex, Gemini CLI, Antigravity CLI,
  OpenClaw and Hermes Agent sessions on your Mac (files, and OpenClaw's and Hermes Agent's databases, only read) and keeps only structured fields (classes of mistakes, counts, tool and app names), never their text.
- **The secrets vault:** the passwords, one-time-code seeds, API keys and payment cards you add, encrypted on your Mac,
  with a record of what each card spent (for its caps). The vault
  types a value into a page in the VM, or uses a key for a request the VM makes through its proxy; it never gives a
  value to your coding agent, and values never appear in its output or logs.

## What your coding agent sends to its model provider

metacomputer's tools return what your agent asks for: screenshots of the VM, accessibility and page text, command
output and the contents of files in the VM. Your agent (Claude Code, Codex, OpenClaw, Hermes Agent, Gemini CLI or Antigravity CLI) sends those to its model
provider (Anthropic, OpenAI, Google, or whichever provider you chose for OpenClaw or Hermes Agent) as part of the
conversation, under that provider's terms and privacy policy. So don't put
anything in the VM that you wouldn't show your agent's provider. A page can show a username or a one-time code in
plain sight, and then a screenshot or a recording holds it too.

## Network connections metacomputer makes

**To install and build** (from your Mac), each only to fetch software:
- GitHub: this repository; mcomp-vm's source and the Swift packages it builds with; Cua's source and releases
  (Cua Driver, and Lume if you choose Cua's own runner).
- Go's module proxy, for the two small Go tools the build compiles.
- Homebrew, only if you agree to install it or a package it lacks (Homebrew's own analytics follow your Homebrew
  settings).
- Ubuntu's cloud images and keyserver, and, from the VM while it's built, Ubuntu's package archive, Mozilla's
  package repository for Firefox and, if you choose Chrome, Google's.
- rclone's downloads, for S3-backed folders.
- For a computer elsewhere ([docs/remote.md](docs/remote.md)): Cua Driver's Linux build for its architecture from
  GitHub (to your Mac, then over SSH to the computer), and, from the computer while it's set up, the same package
  repositories as the VM on your Mac.

The programs metacomputer installs are pinned to a version or commit and checked against a checksum or signature.
Ubuntu, Firefox and Chrome packages come signed from their own repositories, current when the VM is built.

**To check for updates:** `metacomputer status` asks GitHub for this repository's release tags at most once a day
(`UPDATE_CHECK=off` turns it off), and `metacomputer update` downloads the release from GitHub.

**The VM itself** reaches the public internet through a gateway on your Mac, so it goes wherever your agent's tasks
take it. The gateway refuses connections to your Mac, your local network, your tailnet, cloud metadata services and
outbound mail. Telemetry is turned off where the parts have it: Cua Driver's (on by default upstream) in the VM,
Lume's on your Mac, Firefox's telemetry and studies, and Chrome's usage reports. The browsers otherwise contact
their makers' services as they normally do, such as safe-browsing checks.

**A computer elsewhere** (your own server, or one Oracle Cloud, Hetzner Cloud, DigitalOcean or Vultr makes; only
when you make one): your Mac reaches it over SSH, and everything in it (its screen, its files, what's typed into it)
is on that server or the provider's hardware, under your agreement with them. For a provider, `lib/remote.py` calls
its API with the credentials you hand it, to make, start, stop and delete the computer and what it needs there (its
network or firewall, and, at Hetzner, DigitalOcean and Vultr, the public half of the computer's own SSH key for its
first boot); nothing else is sent to the provider. The computer reaches the internet itself (no gateway on your Mac):
its own firewall refuses cloud metadata services and private and link-local addresses (on Oracle Cloud, Hetzner
Cloud and DigitalOcean, the provider's firewall also lets out only public addresses; Vultr's filters only what comes
in), and `EGRESS=off` turns its internet off, package updates included.

**Only when you set them up:**
- Event sinks and the vault's audit sinks: events go to the webhook, OpenTelemetry Collector, syslog server or ntfy
  topic you add. Events never carry typed text, tokens or URL queries ([docs/events.md](docs/events.md)).
- The vault's API proxy: when a program in the VM uses a token from `metacomputer secrets token`, the proxy on your
  Mac forwards its requests to the API provider you bound that key to, with your key.
- Claude Code inside the VM (`metacomputer claude`): the VM downloads Claude Code from Anthropic, and it talks to
  Anthropic through that proxy, with its nonessential traffic turned off.
- S3-backed folders: rclone on your Mac keeps a folder in step with the bucket you name.
- Paying for things ([docs/payments.md](docs/payments.md)): before a card purchase, the vault on your Mac reads the
  store's `/agents.md` and `/.well-known/ucp` from the store's own site (no cookies; with a Web Bot Auth signature only
  if you turned that on). `metacomputer shop` runs Shopify's `shop` CLI on your Mac, which talks to Shop with the
  account you signed in (completing a checkout and reading a spend request's card, the vault runs the copy you pinned
  with `secrets shop-cli set`, in a macOS sandbox: of your files it reads only that CLI's sign-in file). Payments themselves go between the store, its payment provider and your card or Shop account;
  metacomputer and Metavert take no part in them.

## What Metavert receives

Nothing, unless you contact us: a GitHub issue, a pull request or a private vulnerability report (see
[SECURITY.md](SECURITY.md#reporting-a-vulnerability)). Those are handled on GitHub, under
[GitHub's privacy statement](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement).

## Children

metacomputer is a tool for developers and isn't directed to children under 18.

## Changes

Changes to this page are made in this file, so its history on GitHub shows every version, and the
[CHANGELOG](CHANGELOG.md) notes them.

## Contact

Email [privacy@metavert.io](mailto:privacy@metavert.io) or open an issue at
[github.com/metavert-io/metacomputer/issues](https://github.com/metavert-io/metacomputer/issues). For anything
security-related, use the private report described in [SECURITY.md](SECURITY.md#reporting-a-vulnerability).
