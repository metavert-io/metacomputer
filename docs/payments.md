# Paying for things

Your agent can pay at a checkout in the metacomputer without the vault ever giving it your card. The card lives in the
secrets vault on your Mac (`metacomputer secrets`), and by default every purchase asks you first: Touch ID, or your name
for the card typed at the vault's terminal. The question shows every site that will get the card (the site the browser is really on,
and any payment form's own site), the total the page shows and which card. You can mark a card as an "agent card" that
pays without asking, inside caps the vault enforces. Everything runs on your Mac: Metavert runs no payment service and no
server here, and never touches your money.

**Approval gates disclosure, not the charge.** What you approve is the vault giving a card's details to a site. Once a
reusable card's number, expiry and code have reached a site, that site can charge it again, for any amount, as often as
it likes: no approval, cap or later step of the vault can take them back. For a reusable card the approval question says
so, and so does the summary Touch ID's dialog shows and a passphrase approval signs; adding one (or making it an agent
card) warns you. Where you can, use a card its issuer limits: a single-use number, or one locked to a merchant. A Shop
spend request's card is both, and capped at the amount you approve in Shop: it's the route to prefer where it's available
([below](#stores-that-arent-on-shopify-a-single-use-card-from-shop)).

**Two accepted limits**, said plainly: the vault can't hold back a charge once a card's details have reached a site
(above), and it can't bind a Shop checkout's completion to the amount you approved, since Shop's CLI completes a checkout
by its id alone; on that route Shop's own approval and your Shop spending limits are what hold, and the vault compares the
checkout before and checks what Shop reports charging after ([Shopify stores](#shopify-stores-your-own-shop-account)).

- [Prefer a single-use or merchant-locked card](#prefer-a-single-use-or-merchant-locked-card)
- [Add a card](#add-a-card-in-your-own-terminal)
- [A purchase, step by step](#a-purchase-step-by-step)
- [Approvals, agent cards and caps](#approvals-agent-cards-and-caps)
- [What the agent sees](#what-the-agent-sees)
- [Merchants' rules for agents](#merchants-rules-for-agents)
- [Shopify stores: your own Shop account](#shopify-stores-your-own-shop-account)
- [AP2 mandates](#ap2-mandates)
- [Web Bot Auth](#web-bot-auth)
- [The audit trail](#the-audit-trail)
- [Commands](#commands)

The threat model (a hostile page, a fake total, a swapped site) is in [SECURITY.md](../SECURITY.md#paying-for-things-secrets-pay).

## Prefer a single-use or merchant-locked card

Give your agent a card you can cap and freeze, ideally one its issuer limits: a single-use number, a virtual card locked
to one merchant, or a Shop spend request's card. That's a recommendation, not a rule; any card works. The reasons: a site
that has a reusable card's details can charge it again (approval gates disclosure, not the charge), and once the vault
types a number into a page in the VM, anything else running there can read it, as it could a password. The hub hides
card details in what your agent reads back from the VM, but a screenshot or a desktop recording shows a field as it
looks. The card's own limits at your bank are the backstop for anything the vault can't see, like a page that shows one
total and charges another.

## Add a card, in your own terminal

```sh
metacomputer secrets set agentvisa --kind card --label "Agent Visa"
```

It asks for the card number, the expiry (MM/YY), the security code, the name on the card and the billing ZIP or postal
code (Enter leaves it out), each with the echo off. The number must start like a card (2-6) and pass the card check, and
the expiry can't be in the past. Only you can add a card: the command needs your own terminal, and refuses one run by
Claude Code or Codex. For a reusable card it prints the warning above.

- `--origin https://shop.example` (repeatable) limits the card to those merchants.
- `--single-use` says the number is single-use (the vault then allows it one purchase and deletes it after);
  `--merchant-locked` (with `--origin`) says your issuer locks it to that merchant. These are your statements: the vault
  can't check them, and a card with neither is treated as reusable.
- `--pay agent` and the caps below can be given here or later with `policy`.
- `--project DIR` limits it to Claude Code sessions in that folder, as for other secrets.

`metacomputer secrets list` shows each card's label, policy, caps and what it spent in its period; never its number, not
even the last digits.

## A purchase, step by step

1. **The merchant's rules.** Before a checkout, the agent runs
   `metacomputer secrets merchant-rules https://shop.example` (no unlock needed): what the store publishes for agents in
   its `/agents.md` and its UCP profile. If they forbid scripted checkout or entering card details, or ask agents to have
   the buyer complete checkout, the agent stops at the review step and hands payment to you (`metacomputer handoff --why
   payment`). `pay` checks this again itself.
2. **Everything but the card.** The agent fills the cart, the address and the shipping choice as it does any form.
3. **`pay`.** At the payment step, in page-mode Chrome:

   ```sh
   metacomputer secrets pay agentvisa --origin https://shop.example --total-selector '#order-total' \
     --fill number='#cc-number' --fill exp='#cc-exp' --fill cvc='#cc-csc' --fill name='#cc-name' --fill zip='#postal'
   ```

   The vault reads the merchant's rules, reads the total from the element `--total-selector` names (in the one tab on
   that site; a person must be able to see the element: not hidden, see-through, covered or clipped away, itself or any parent (the
   browser's own hit test must land on its digits);
   its text must have the amount right after the word "Total", or after `--total-label`'s words, in exactly one place),
   checks the card's caps, then asks you. Once you allow it, it types each part into its field,
   checked in the browser first (the tab's own site, a field of the right kind), and prints a `purchase_id`: a key only
   its requester gets. The purchase belongs to the agent session that started it (or the agent's shared process: Codex's app server, an
   OpenClaw or Hermes Agent gateway); one started
   outside any agent session can't be resumed, so from a plain terminal or script name every part with `--fill`. The parts: `number`, `exp` (MM/YY), `exp-long` (MM/YYYY), `exp-month`, `exp-year`, `exp-year2`,
   `cvc`, `name`, `zip`.
4. **Fields in a payment frame** (a payment provider's card form inside the page): name its site with
   `--frame-origin https://js.stripe.com` (repeatable) and leave out `--fill`; then click into each field and fill it:
   `metacomputer secrets pay --purchase KEY --field number`. The approval names that site too. The tab itself must stay
   on the shop: the VM's helper never takes the payment form's site as the tab. A selector only searches the page
   itself, so a field inside a frame is filled by focus.
5. **In Firefox** (human mode): `metacomputer secrets pay agentvisa --origin https://shop.example --human` reads the
   site from the address bar and the total from the words after "Total" (`--total-label` for other words); then the
   agent clicks into each field and runs `pay --purchase KEY --field number --human`. Fields inside frames are refused
   there, as for passwords.
6. **The order.** The agent checks the page and places the order, then ends the purchase:
   `metacomputer secrets pay --purchase KEY --done` (exactly that: `--done` takes nothing else but `--vm` naming the
   purchase's own VM).

Payments need the hub (`MCP_HUB=on`, the default): the vault sends no card part until the hub for that VM has said it
has seen the purchase, so its masks are on first (in a private folder of the vault's, naming the hub's process and the
lock it holds). Masking is a guard against accidents with an agent that works with you, not against a malicious program
running as you: your approval of each purchase and a single-use or merchant-locked card are what limit the harm. A purchase lasts 10 minutes, fills each part at most once (a part the page refused can be tried 3 times), only into the
tab on the site you approved, and ends when the vault locks. Before each part the vault reads the total again: if it
went up since you approved, nothing more is typed (start another purchase; it asks again). A change to the card or its
policy ends purchases approved before it, even while a part is under way: the last check, with the purchase's entry in
the spending record, is under the vault's lock. A total that's unclear is refused, and the agent hands payment to you:
several places showing a total (even the same amount), none, zero, a hidden element, a currency the vault doesn't
support, or only a part of the price ("Sub-total", "Sub total", "Items total"). `--total-label` must name the payable
total: it says "total" or "due" and nothing like "sub", "savings", "tax" or "shipping". A drop-down menu
for the expiry (a `<select>`) can't be filled by the vault: hand that step to the user.

## Approvals, agent cards and caps

**By default every purchase asks you.** The question on the vault's terminal (or in macOS's Touch ID dialog, or for
`metacomputer secrets approve ID`) reads like this:

```
  Pay USD 12.34 with Agent Visa (card agentvisa) on VM metacomputer?
  Card parts go to: https://shop.example
  The page says the total is USD 12.34 (after "Total"): the page's own claim (check it in the live view, metacomputer view, if unsure).
  This approves giving the card to these sites, not the charge: a reusable card can be charged again by this site, for any amount.
  Merchant's agent rules: /agents.md published, nothing in it against card entry.
  Asked by Claude Code in ~/projects/x.
  A purchase: what you allow covers this one purchase.
  Type agentvisa to allow this one purchase. Anything else refuses:
```

With a payment form's site, the second line adds it (`https://shop.example (the shop), https://js.stripe.com (payment
forms)`). The summary Touch ID's dialog shows, `requests` lists and a passphrase approval signs reads, for the same
purchase, `pay USD 12.34 with Agent Visa (card agentvisa) at https://shop.example. Approves giving the card, not the
charge: a reusable card can be charged again by this site.`, with every payment form's site in full and, when the store's
rules can't be read, `Store's agent rules: can't tell.` A purchase whose summary can't show all of that within what the
dialog shows isn't asked at all: it names at most three payment-form sites, each address at most 100 characters. What
you allow covers that one purchase: typing `agentvisa session` or `agentvisa unlock` doesn't widen it, no grant or
unattended rule covers a card (`rule add --secret` refuses one), and presence mode (Touch ID for every approval) applies
as it does to every approval.

**An agent card pays without asking**, within its caps:

```sh
metacomputer secrets policy agentvisa --pay agent --currency USD --max-purchase 50 --max-per 200/30d \
  --merchant-max https://shop.example=100/30d
```

- `--max-purchase AMOUNT`: the most one purchase may be.
- `--max-per AMOUNT/PERIOD`: the most in any rolling period (1h to 366d), across merchants.
- `--merchant-max https://site=AMOUNT/PERIOD` (repeatable): the most at one merchant in a period.
- `--allow-frame https://pay.example` (repeatable): a payment form's site the agent card may give parts to; with no
  question asked, it refuses any other. `--no-frames` clears them.
- `--no-caps` clears the caps; `--pay ask` makes it ask again. `policy` needs your own terminal, prints the warning
  about reusable cards for an agent card, and ends the card's purchases already approved.

The caps hold for every card that has them, an asking card too: a purchase over a cap is refused before anything is
asked. A purchase counts against them from before its first part leaves the vault, and stays counted even if that fill
then fails (the part may have reached the page); a Shop checkout counts once it's approved, an AP2 mandate before it's
signed. Purchases still open hold their amount meanwhile. If the spending record can't be written, nothing is sent. A
total in another currency than the caps', with no currency, or one the vault can't read, is refused for a card with
caps. Spending the caps can't count (Shop charging in another currency than the caps') stops that card's or entry's
purchases until it ages out of the caps' period. `unlock --confirm each` makes agent cards ask too. An agent card also
refuses a store whose rules it can't read whole.

The caps check the total the page shows. A dishonest page can show a smaller total than it charges, and a site that has
a reusable card can charge it again later: that's why the card's own limits, at your bank, are the ones that hold
whatever the page says.

## What the agent sees

| | The agent |
|---|---|
| The card's number, expiry, security code, name, postal code | Never from the vault: not in `pay`'s output, `list`, the audit log or the events |
| A card number read back from the page after a fill | Masked by the hub in everything the driver sends (`[card number hidden by metacomputer]`), as text, split across values, or a JSON number; the action log and the hub's own logs keep it masked too |
| The other parts read back while a purchase is open on that VM | Masked by shape and by what names them (an expiry, the security code and postal code in a field, a JSON field or after a label that names them, `CVC:123` too, a postal code with letters anywhere) and the name by its fingerprint; meanwhile any run of 12 or more digits in that VM's results is masked, card or not |
| The same parts after the purchase ends (still on the page) | Visible, like any text on the page, except a card number |
| The page in a screenshot or a desktop recording, or a number spelled some other way | Visible: the hub can't mask pixels, and masks are patterns. Don't screenshot or record a filled card form |
| The card's label, policy, caps, what it spent | Yes (`list`) |
| The total, the site, the purchase's key, which parts were filled | Yes |

## Merchants' rules for agents

Before a card fill, the vault reads two files from the merchant's site, from your Mac:

- `/agents.md`: a store's instructions for AI agents. Shopify's stores publish one that says "Do not work around the
  tools by scripting checkout forms or page controls" and "ask the buyer to continue on the checkout page". The vault
  looks for sentences that forbid scripted checkout or entering card details, or that ask agents to have the buyer
  complete checkout; where it finds one, `pay` refuses and says to hand payment to you. A sentence it misreads errs
  toward handing payment to you.
- `/.well-known/ucp`: the store's Universal Commerce Protocol profile, for its payment handler ids (like
  `dev.shopify.shop_pay`) and whether it mentions AP2.

Only a file that's plainly an agents.md counts as rules (served as text or Markdown, read whole, plain text, with no web
page's markers anywhere in it), and only a "not found" (404 or 410) as none. A file larger than the vault reads (256
KiB), one served as a web page or anything else, one with a web page in it, another answer or no answer means "can't
tell": an agent card refuses there, and an asking card's question and its summary (Touch ID, `requests`, a passphrase
approval) say "can't tell" so you decide. A UCP profile counts only when it's served as JSON. Only rule names, payment-handler
ids and yes/no answers come out of what's read, never its words. A test page served on the VM's own loopback
(`http://localhost:8000`) isn't checked (this Mac can't reach it), which is "can't tell" too.

A store that blocks shopping agents: the agent doesn't try to get past the block; it hands the step to you.

## Shopify stores: your own Shop account

Shopify's stores ask agents not to script their checkout forms, so a card fill there is refused. Instead, your agent
checks out through **your own Shop account**, with Shopify's own command-line tool, the MIT-licensed `shop` CLI. Install
it and pin it for the vault, in your own terminal:

```sh
npm install --global @shopify/shop
metacomputer secrets shop-cli set  # pins `shop` and `node` from this terminal's PATH (or --cli PATH --runtime PATH)
metacomputer shop login            # prints a link and a code: you approve the sign-in on Shop's own page
metacomputer shop status
```

The vault runs only the CLI you pinned, never one a request names, and only confined. `shop-cli set` pins everything
Node would load: `node` and the libraries it loads that aren't the system's (Homebrew's libnode, OpenSSL, ICU ...), the
CLI's package, file by file (a link out of the package isn't pinned), and the one file of yours the CLI may use, its
sign-in (`SHOP_AUTH_FILE` from your terminal, by default `~/.config/shop-node-wasm/credentials.json`; `--auth-file PATH`
for another). Before every run the vault checks them all, unchanged and in the same places, owned by you (or root) and
not writable by other users. It runs Node under macOS's `sandbox-exec`, with a Seatbelt profile that lets it read only
the pinned runtime, its libraries, the package and the system's own libraries, read and write only a fresh folder of its
own (its home, temporary and working folder, removed afterwards) and the sign-in file, reach the network only through a
proxy the vault runs for that run (with a secret of that run's; public hosts on port 443; nothing on this Mac or its
network, no Unix socket), and
start no other program: a module, library or file planted anywhere else can't even be read. Inside that, a fixed command and
environment, no add-ons, and module hooks that stop the CLI if it resolves a module outside its package. Without
`sandbox-exec` (or with a profile that fails to apply), or with a Node older than 22.15, the vault refuses rather than
run it unconfined. After updating Node or the CLI, run
`shop-cli set` again.

Then, for a Shop checkout that's ready (`shop checkout create` …, status `ready_for_complete`):

```sh
metacomputer shop complete gid://shopify/Checkout/… --shop example-store.com --instrument @instrument.json
```

The vault reads the checkout from Shop, asks for your approval of its total at that store (the same question as a card
purchase), reads it again right before completing it, and completes it once, only if the whole checkout is the same,
compared as Shop's CLI wrote it (every number by its exact text; only fields about the read itself, like its link and
expiry, are left out), its total and currency read the same way again, the entry's policy and caps unchanged, and the
instrument one it offers. After it completes, what Shop reports charging is checked: one total, every currency the
answer names the same one and the approved one, at most the approved amount. A higher one is reported
(`completed_with_a_higher_total`, with `charged`) and the difference goes in the spending record; another currency is
reported (`completed_in_another_currency`), and so are currencies that disagree (`completed_unverified`): both stop
that entry's purchases until the spending ages out of its caps' period. An answer that doesn't say plainly what was
charged is `completed_unverified`. Each completion is settled in the vault, by its reservation's own id: until a definite
outcome is recorded its entry buys nothing, even after a lock or a restart; a timeout, an answer that doesn't say what
was charged, or a failure other than the CLI saying nothing was done (`usage` or `invalid_input`, refused before
sending; `auth_required`, refused for want of a valid sign-in) stays unsettled until you settle it (`spend-resolve NAME`, after checking the order in Shop). If the vault can't be written right then, the outcome waits
(MAC'd, in `spend-pending.jsonl`) for the next purchase or unlock (`spending_recorded: false` says so); a record there
the vault can't verify is never imported and stops every purchase until you look: `metacomputer secrets spend-resolve`
in your own terminal lists what waits, settles an entry by hand after you checked the order in Shop (`spend-resolve NAME
--extra 1.21`), or drops what can't be verified (`--discard-pending`). Shop Pay
pays from your Shop account; Shop's own approval and
spending limits (Shop app: Account, Connections, your agent) apply as well. To let Shop checkouts within caps go through
without metacomputer asking, add a shop entry in your own terminal: `metacomputer secrets set shop --kind shop --pay agent
--max-purchase 50`.

Honest limits: metacomputer can't stop an agent on your Mac from running Shopify's CLI itself, past its approval; there,
Shop's own approval and limits are what hold. The completion can't carry the approved amount or a checkout version (the
accepted limit above), so a change made by the merchant in the moment between the vault's second read and the completion
isn't stopped, only reported afterwards. And this was tested with a stand-in for the CLI (a Node script, run confined as
the real one would be), not a real Shop account.

### Stores that aren't on Shopify: a single-use card from Shop

A Shop "spend request" issues a one-time card for one purchase you approve in Shop. The agent creates it (`shop
spend-request create …`, with the store's own terms); you approve it in Shop; then:

```sh
metacomputer shop card storecard --spend-request 0198e63b-…
```

The vault's unlock process runs the pinned CLI with `--output-file` into the CLI's own folder, reads the card and removes
the file and the folder at once, so the card goes straight into the vault and the agent is never given it. It's imported only when Shop's answer
says it was issued for exactly that spend request and names the store's site, the approved limit and a supported
currency. The card is single-use (deleted when its purchase ends) and merchant-locked: limited to the store's site and
capped at the amount you approved in Shop. `secrets pay storecard …` fills it as usual, and still asks you. The agent
reports the outcome to Shop itself (`shop spend-request complete`). metacomputer takes no part in Shop's commission
programs.

## AP2 mandates

The [Agent Payments Protocol](https://github.com/google-agentic-commerce/AP2) (AP2, v0.2) lets a merchant that supports
it receive signed proof that you approved a purchase. The merchant gives the agent a signed checkout (a JWT); the vault
on your Mac, after your approval, signs two mandates with a key that never leaves the vault:

- a **Checkout Mandate** (`mandate.checkout.1`): the merchant's checkout and its hash (AP2 v0.1 called this the cart
  mandate);
- a **Payment Mandate** (`mandate.payment.1`): payee, amount, and your card by its label (never its number), bound to
  that checkout by its hash, with `risk_data` saying the merchant's signature on the checkout wasn't verified here.

Both are SD-JWTs signed with ES256, in the form AP2's own SDK makes and verifies.

```sh
metacomputer secrets ap2 init                    # your terminal: makes the key, prints its public half
metacomputer secrets ap2 key                     # the public key, any time the vault is unlocked
metacomputer secrets ap2 sign agentvisa --origin https://shop.example --checkout-jwt checkout.jwt
```

The vault signs only a checkout that names its merchant (id, name and website, which must be the site named) and its own
id, with exactly one payable total in whole minor units and one supported currency, a merchant's signing algorithm (never
`none`), no field given twice at any depth; one that says when it expires or starts (`exp`, `nbf`) must be valid when
it's read and again right before it's signed, after your approval, and one that names an audience (`aud`) must name this
vault's AP2 key id (`secrets ap2 key` shows it). It can't verify the
merchant's signature (it doesn't know the merchant's key), so only you approve a signature: an agent card refuses to
sign, and the question and its summary say the signature isn't verified. Each checkout, by its merchant and its id
(however the JWT is written or signed), is signed once.

Plainly: **almost no merchants accept AP2 yet**, and none is named here as working. What was tested: AP2's own SDK
verifies the mandates the vault signs (`tests/test_vault_payments.py`, with the SDK installed). Nobody trusts your key
until you register it with a merchant or credential provider that accepts AP2. AP2's "human not present" open mandates
(the successor of v0.1's intent mandate) aren't built here. Neither ACP nor the card networks' agent programs are part
of metacomputer: none of it makes Metavert a payment service or something you depend on.

## Web Bot Auth

Some sites tell agents apart from other bots by a signature on each request (Web Bot Auth, an IETF draft: HTTP Message
Signatures, RFC 9421, with an Ed25519 key whose public half you host). It's off unless you turn it on:

```sh
metacomputer secrets wba init --agent https://agent.your-site.example     # your terminal: a key for this install
metacomputer secrets wba directory --headers                              # what to host, while unlocked
metacomputer secrets wba on                                               # your terminal
```

Host the printed directory at `https://agent.your-site.example/.well-known/http-message-signatures-directory` with the
printed response headers (they're signed and valid for 30 days: print them again before then). Then register that
address with the sites that verify bots (each has its own way to apply); Shopify, for one, honours only keys it has
registered. Once on, the vault's own requests to merchants (their rules) carry the signature, and
`metacomputer secrets wba sign https://site/path` gives signed headers for a request of your agent's own (valid 60 s).
The VM's browsers don't sign their requests: that needs an extension in the VM image, which isn't built.

## The audit trail

Every step is in the vault's audit log and goes to its audit sinks (`secrets audit-sink`): the site, the time, the
outcome, the card's label, the total, the approval and the purchase's short id, never a card's part or a purchase's key.
Each record's `event` field names it:

| Event | When |
|---|---|
| `payment.request` | a purchase asks you |
| `payment.authorize` | you allowed it, or an agent card's policy did (`approval: policy:agent`) |
| `payment.fill` | a part was typed (`field` names which part, never its value) |
| `payment.denied` | refused, by you, a cap, the merchant's rules, or a locked vault |
| `payment.end` | the purchase ended (`done`, `expired`, `locked`, `card_changed`, or a Shop checkout's outcome) |
| `payment.mandate` | AP2 mandates were signed |

## Commands

| Who | Command |
|---|---|
| You, your terminal | `secrets set NAME --kind card [--label L] [--pay ask\|agent] [--single-use] [--merchant-locked] [caps]`, `secrets set NAME --kind shop`, `secrets policy CARD --pay … [caps] [--allow-frame URL]`, `secrets delete CARD`, `secrets shop-cli set`, `secrets ap2 init`, `secrets wba init\|on\|off` |
| You, any terminal | `secrets approve ID` (Touch ID or your login password), `secrets deny ID` |
| The agent | `secrets merchant-rules URL`, `secrets pay …`, `secrets pay --purchase KEY --field PART\|--done`, `secrets list`, `secrets ap2 key\|sign`, `secrets wba status\|directory\|sign`, `shop status\|login\|complete\|card` |
