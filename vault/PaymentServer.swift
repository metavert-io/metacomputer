// mcomp-vault: purchases, served by the unlock process (Payments.swift; SECURITY.md, "Paying for things").
//
// `pay` starts one: the merchant's published agent rules (a ban, or rules it can't read whole for an agent card, stops it,
// and the agent hands payment to the user), the total the page shows (read in the tab on the merchant's site from a shown
// element, right after its label, reduced to an amount and a currency), the card's caps (what it has spent in each
// period, plus the purchases still open), then the user's approval (Touch ID or the typed name, for this purchase only)
// unless the card is an agent card. The approval names every site that will get a card part (the shop, and any payment
// form's frame) and binds them. Then, for a few minutes, the card's parts go one at a time into the tab on that same site,
// each part at most once: the total is read again, the tab checked, the purchase counted in the spending record (before
// any part leaves the vault, never undone), then the part is typed (the fill account's secret-fill in page mode, held to
// the shop as the tab's own site; the desktop account's human-mode script in Firefox). Every step is in the audit log,
// never a part's value. Approval gates disclosure, not the charge: once a reusable card reaches a site, that site can
// charge it again (SECURITY.md).
import CryptoKit
import Darwin
import Foundation

func newAckID() -> String { randomBytes(8).map { String(format: "%02x", $0) }.joined() }

/// Shop CLI error codes that mean nothing was done: its own refusals of the arguments before sending (usage,
/// invalid_input), or Shop refusing the request for want of a valid sign-in (auth_required). UnlockServer.shopNothingSent.
let shopNothingSentCodes: Set<String> = ["usage", "invalid_input", "auth_required"]

/// An approved purchase: in this process's memory only, gone when it ends, expires or the vault locks.
final class Purchase {
    let key: String                 // the capability the requester gives back (32 hex digits); never recorded
    let id: String                  // what's shown and recorded: the first 8 hex digits of the key's SHA-256
    let name: String
    let cardID: String
    let gen: String                 // the card's generation at approval: a new value or policy ends the purchase
    let label: String
    let vm: String
    let origin: String
    let frameOrigins: [String]
    let human: Bool
    let profile: String?
    let browserPID: Int?
    let windowID: Int?
    let money: Money?
    let approval: String
    let requestID: String?
    let claudePID: pid_t?           // whom it's bound to: the agent session that started it,
    let claudeStart: Double?
    let agentName: String?
    let hostPID: pid_t?             // or the shared agent process it came from (Codex's app server, an OpenClaw or Hermes
                                    // Agent gateway); with neither it can't be resumed (ownPurchase)
    let hostStart: Double?
    let route: String               // card | shop | ap2
    let singleUse: Bool
    var totalSelector: String?      // where the page shows the total (page mode), read again before each part
    var totalLabel = "Total"        // the words right before it
    let created = Date()
    let expires: Date
    var filled: [String] = []
    var tries: [String: Int] = [:]
    var counted = false             // in the spending record: from before the first part left the vault
    var masks: [String: Any] = [:]  // what the hub masks while it's open (shapes and a fingerprint, never a value)
    var ackID = newAckID()          // what the hub says it has seen (hubAckProblem): a new one for each fill's masks
    var maskUntil = Date.distantPast  // the hub masks it until then even after it ends: each fill and a settle margin after
    var reservation: String?        // a Shop completion's own reservation id: what settles it, and nothing else

    init(name: String, s: Secret, vm: String, origin: String, frameOrigins: [String], human: Bool, profile: String?,
         browserPID: Int?, windowID: Int?, money: Money?, approval: String, requestID: String?, who: Requester, route: String, expires: Date) {
        key = randomBytes(16).map { String(format: "%02x", $0) }.joined()
        id = String(sha256Hex(Array(key.utf8)).prefix(8))
        self.name = name
        self.cardID = s.id ?? name
        self.gen = secretGen(s)
        self.label = s.card_label ?? name
        self.vm = vm
        self.origin = origin
        self.frameOrigins = frameOrigins
        self.human = human
        self.profile = profile
        self.browserPID = browserPID
        self.windowID = windowID
        self.money = money
        self.approval = approval
        self.requestID = requestID
        self.claudePID = who.claudePID
        self.claudeStart = who.claudeStart
        self.agentName = who.agent
        self.hostPID = who.claudePID == nil ? who.hostPID : nil
        self.hostStart = who.claudePID == nil ? who.hostStart : nil
        self.route = route
        self.singleUse = s.single_use == true
        self.expires = expires
    }

    var total: String { money.map { formatMoney($0.minor, $0.currency) } ?? "unknown" }
}

/// What authorizing a purchase decided.
struct PurchaseAuth {
    let ok: Bool
    let how: String
    let requestID: String?
    let why: String
}

/// The most entries the spending record keeps within the longest period a cap can have; past it, purchases are refused
/// (nothing that still counts is ever dropped).
func ledgerLimit() -> Int {
    #if MCOMP_VAULT_TESTING
    if let s = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_LEDGER_MAX"], let n = Int(s), n > 0 { return n }
    #endif
    return 20000
}

/// A purchase's key, as a requester gives it back: 32 hex digits.
func validPurchaseKey(_ s: String) -> Bool { matches(s, "^[0-9a-f]{32}$") }

extension UnlockServer {
    // MARK: results

    /// Like finish(): the answer for the client and one audit record (op, result, the card's label, never a value), a line
    /// on the unlock terminal, the sinks flushed.
    func payFinish(_ op: String, _ fields: [String: Any], _ res: [String: Any], _ who: Requester, approval: String,
                   requestID: String? = nil) -> [String: Any] {
        var out = fields
        for k in ["ok", "status", "reason", "route"] { if let v = res[k] { out[k] = v } }
        if let id = requestID { out["request_id"] = id }
        let left = requesterFD >= 0 && requesterGone()
        if left && out["status"] as? String != "ok" {
            out["ok"] = false
            out["status"] = "cancelled"
            out["reason"] = requesterLeftReason
        }
        if out["ok"] == nil { out["ok"] = false }
        out = (cleanAll(out) as? [String: Any]) ?? fields
        let status = out["status"] as? String ?? "error"
        var rec = out
        rec["ok"] = nil
        rec["status"] = nil
        rec["handoff"] = nil
        if left { rec["requester_left"] = true }
        rec["op"] = op
        rec["result"] = status
        rec["approval"] = approval
        rec["requester"] = who.record
        if status == "ok" {
            rec["event"] = op == "pay_fill" ? "payment.fill" : (op == "ap2_sign" ? "payment.mandate" : nil)
        } else if status == "denied" || status == "locked" {
            rec["event"] = "payment.denied"
        }
        record(rec)
        var line = "          -> \(status)"
        if let o = out["origin"] as? String { line += ": \(o)" }
        if let t = out["total"] as? String { line += ", \(t)" }
        if let r = out["reason"] as? String { line += " (\(r))" }
        say(line)
        flushSinks(force: true)
        return out
    }

    // MARK: caps and the spending record

    /// Entries the spending record holds within the longest period a cap can have.
    func ledgerCount(_ p: Payload) -> Int {
        let since = Date().timeIntervalSince1970 - Double(maxCapPeriod)
        return (p.spend ?? []).filter { $0.t >= since }.count
    }

    /// Why a purchase of `money` at `origin` is over one of the card's caps (counting what it spent in each period and
    /// the purchases still open), or nil.
    func capsProblem(_ s: Secret, _ p: Payload, name: String, origin: String, money: Money?, excluding: String? = nil) -> String? {
        if ledgerCount(p) + purchases.values.filter({ !$0.counted && $0.key != excluding }).count >= ledgerLimit() {
            return "the spending record is full (\(ledgerLimit()) purchases within a year): no purchase is allowed until older ones age out"
        }
        if let why = pendingSpendProblem(s, name: name, p) { return why }
        guard let caps = s.caps, !caps.isEmpty else { return nil }
        let label = s.card_label ?? name
        guard let m = money else { return "the total couldn't be read, and \(label) has caps" }
        guard m.currency == caps.currency else {
            return "the total is in \(m.currency ?? "no currency the page shows"), and \(label)'s caps are in \(caps.currency): they can't be checked"
        }
        if let pp = caps.per_purchase, m.minor > pp {
            return "\(formatMoney(m.minor, m.currency)) is over \(label)'s cap of \(formatMoney(pp, caps.currency)) a purchase"
        }
        let now = Date().timeIntervalSince1970
        let id = s.id ?? name
        func used(_ period: Int, _ site: String?) -> Int {
            let past = (p.spend ?? []).filter { $0.card == id && $0.currency == caps.currency && $0.t >= now - Double(period) && (site == nil || $0.origin == site) }
                .reduce(0) { $0 + $1.amount }
            let open = purchases.values.filter { $0.cardID == id && !$0.counted && $0.key != excluding && $0.money?.currency == caps.currency && (site == nil || $0.origin == site) }
                .reduce(0) { $0 + ($1.money?.minor ?? 0) }
            return past + open
        }
        // Spending this vault knows of but couldn't count in the caps' currency (Shop charged in another): the caps can't
        // be checked until it ages out of the longest period.
        let longest = max(caps.period_s ?? 0, (caps.merchants ?? []).map { $0.period_s }.max() ?? 0)
        if let odd = (p.spend ?? []).first(where: { $0.card == id && $0.unaccounted == true && $0.t >= now - Double(longest) }) {
            return "\(label) has spending in \(odd.currency) its caps (in \(caps.currency)) can't count: no purchase until it ages out of the caps' period"
        }
        if let per = caps.per_period, let ps = caps.period_s {
            let u = used(ps, nil)
            if u + m.minor > per {
                return "\(formatMoney(m.minor, m.currency)) would take \(label) over its cap of \(formatMoney(per, caps.currency)) per \(periodWords(ps)) (\(formatMoney(u, caps.currency)) used or held by open purchases)"
            }
        }
        for mc in caps.merchants ?? [] where mc.origin == origin {
            let u = used(mc.period_s, origin)
            if u + m.minor > mc.amount {
                return "\(formatMoney(m.minor, m.currency)) would take \(label) over its cap of \(formatMoney(mc.amount, caps.currency)) per \(periodWords(mc.period_s)) at \(origin) (\(formatMoney(u, caps.currency)) used or held)"
            }
        }
        return nil
    }

    /// The purchase goes in the spending record before any part of the card leaves the vault, and stays there: a fill that
    /// fails afterwards may still have handed a part over. A single-use card is spent from then on. nil when it's recorded
    /// (or was already); otherwise why not, and nothing may be sent. Only entries older than the longest period a cap can
    /// have are dropped.
    func reserveSpend(_ pu: Purchase, tag: String? = nil) -> String? {
        // The card (or shop entry) and its policy as they are now, read under the vault's lock with the reservation: a
        // policy changed since the approval, a single-use card spent meanwhile, or caps it would now exceed refuse here.
        func stillAuthorized(_ p: Payload) -> String? {
            guard let s = p.secrets[pu.name], (s.id ?? pu.name) == pu.cardID, secretGen(s) == pu.gen else {
                return "\(pu.name) or its policy changed after the purchase was approved: nothing was sent; start another"
            }
            if !pu.counted, pu.singleUse, s.spent == true { return "\(pu.name) is a single-use card and was spent meanwhile" }
            if !pu.counted, let why = capsProblem(s, p, name: pu.name, origin: pu.origin, money: pu.money, excluding: pu.key) { return why + "; nothing was sent" }
            return nil
        }
        if pu.counted {
            guard let p = try? load() else { return "cannot read the vault; nothing was sent" }
            return stillAuthorized(p)
        }
        let now = Date().timeIntervalSince1970
        var refused: String?
        do {
            try updateVault(paths, key) { p in
                if let why = stillAuthorized(p) { refused = why; throw VaultError(why) }
                var l = (p.spend ?? []).filter { $0.t >= now - Double(maxCapPeriod) }
                guard l.count < ledgerLimit() else { throw VaultError("the spending record is full (\(ledgerLimit()) purchases within a year)") }
                l.append(SpendEntry(card: pu.cardID, origin: pu.origin, amount: pu.money?.minor ?? 0, currency: pu.money?.currency ?? "-", t: now,
                                    purchase: tag ?? pu.id))
                // A Shop completion is settled only once what Shop charged is known and recorded: until then its entry
                // buys nothing, and that holds across a lock or a restart (it's in the vault, sealed, from now on).
                if pu.route == "shop" {
                    // Its own id, fresh and sealed with it: only a settlement naming exactly this reservation ends it.
                    let rid = "res:" + randomBytes(16).map { String(format: "%02x", $0) }.joined()
                    pu.reservation = rid
                    p.unresolved = (p.unresolved ?? []) + [UnresolvedSpend(card: pu.cardID, origin: pu.origin, purchase: rid,
                                                                           amount: pu.money?.minor ?? 0, currency: pu.money?.currency ?? "-", t: now)]
                    if p.spend_key == nil { p.spend_key = Data(randomBytes(32)).base64EncodedString() }
                }
                p.spend = l
                if pu.singleUse, let s = p.secrets[pu.name], (s.id ?? pu.name) == pu.cardID { p.secrets[pu.name]?.spent = true }
            }
        } catch let e as VaultError {
            return refused ?? "the purchase couldn't be recorded (\(e.message)); nothing was sent"
        } catch {
            return "the purchase couldn't be recorded in the vault; nothing was sent"
        }
        pu.counted = true
        return nil
    }

    /// Spending found after the fact (Shop charged more than approved, or in another currency): recorded too, so the caps
    /// count it. `unaccounted` marks spending the caps' currency can't count (it then blocks the caps' period). nil when
    /// recorded, else why not (it's said, not hidden).
    var pendingSpendPath: String { paths.dir + "/spend-pending.jsonl" }

    /// The MAC on a pending record: HMAC-SHA256 under the vault's spend_key over the record with an empty mac, its keys sorted.
    func pendingMAC(_ r: PendingSpendRecord, key k: [UInt8]) -> String {
        var c = r
        c.mac = ""
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        return hmacSHA256Hex(key: k, data: Array((try? enc.encode(c)) ?? Data()))
    }

    /// spend-pending.jsonl as the vault reads it, against the vault's own state: records that settle an unresolved
    /// completion (their MAC checks out under spend_key, and they fit it: the same card, each extra positive and at most
    /// 1e9, a currency code, times from that completion to now), and every other line, which counts as damaged. A record
    /// whose completion is settled already is dropped quietly (it changes nothing). nil file: (nothing, nothing).
    func pendingJournal(_ p: Payload) -> ([PendingSpendRecord], [String]) {
        guard fileExists(pendingSpendPath) else { return ([], []) }
        guard let b = try? readFileBytes(pendingSpendPath, max: 1 << 20) else { return ([], ["(the file can't be read)"]) }
        let k = p.spend_key.flatMap { Data(base64Encoded: $0) }.map { Array($0) }
        let now = Date().timeIntervalSince1970
        var good: [PendingSpendRecord] = [], bad: [String] = []
        for l in String(decoding: b, as: UTF8.self).split(separator: "\n", omittingEmptySubsequences: true) {
            guard let r = try? JSONDecoder().decode(PendingSpendRecord.self, from: Data(l.utf8)), let key = k, r.v == 1,
                  r.mac.count == 64, constantTimeEqual(r.mac, pendingMAC(r, key: key)) else { bad.append(String(l.prefix(200))); continue }
            guard let m = (p.unresolved ?? []).first(where: { $0.purchase == r.purchase }) else { continue }      // settled already
            let inTime = { (t: Double) in t >= m.t - 60 && t <= now + 300 }
            guard m.card == r.card, inTime(r.t), r.extras.count <= 4, r.extras.allSatisfy({ e in
                e.card == r.card && e.amount > 0 && e.amount <= 1_000_000_000 && matches(e.currency, "^[A-Z]{3}(/[A-Z]{3}){0,3}$")
                    && e.purchase.hasPrefix(r.purchase) && inTime(e.t) }) else { bad.append(String(l.prefix(200))); continue }
            good.append(r)
        }
        return (good, bad)
    }

    /// Settles a Shop completion: what it charged beyond its reservation (extras, each positive; none when it charged what
    /// was approved, or nobody can tell) goes in the spending record and its unresolved marker goes, in one write. When the
    /// vault can't be written, the outcome is kept, MAC'd, in memory and in spend-pending.jsonl for the next purchase or
    /// unlock to record; the marker, sealed in the vault since the reservation, keeps the entry blocked meanwhile, across a
    /// lock or a restart. nil when recorded, else what to tell the user.
    func settleCompletion(_ pu: Purchase, extras: [SpendEntry]) -> String? {
        guard pu.cardID != "shop-default", let tag = pu.reservation else { return nil }
        let now = Date().timeIntervalSince1970
        do {
            try updateVault(paths, key) { p in
                p.unresolved = (p.unresolved ?? []).filter { $0.purchase != tag }
                p.spend = (p.spend ?? []).filter { $0.t >= now - Double(maxCapPeriod) } + extras
            }
            return nil
        } catch {}
        var rec = PendingSpendRecord(v: 1, purchase: tag, card: pu.cardID, extras: extras, t: now, mac: "")
        if let p = try? load(), let k = p.spend_key.flatMap({ Data(base64Encoded: $0) }) { rec.mac = pendingMAC(rec, key: Array(k)) }
        pendingSettle.append(rec)
        var kept = false
        if !rec.mac.isEmpty, let line = try? JSONEncoder().encode(rec) {
            let fd = open(pendingSpendPath, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW, 0o600)
            if fd >= 0 { kept = writeAll(fd, Array(line) + [0x0A]) && fsync(fd) == 0; close(fd) }
        }
        say("          WARNING: what Shop charged couldn't be recorded in the vault yet" + (kept ? " (kept in spend-pending.jsonl)" : "") + ": \(pu.label) buys nothing until it is")
        return "couldn't be recorded in the vault yet, so \(pu.label)'s purchases are refused until it is (the vault retries with every purchase and unlock; `metacomputer secrets spend-resolve \(pu.name)` in your own terminal settles it by hand)"
    }

    /// Records what's waiting (this process's settlements, and verified records in spend-pending.jsonl), once each; the file
    /// keeps only lines the vault couldn't verify, which block every purchase until the user checks them
    /// (`spend-resolve --discard-pending`), and are reported when first seen.
    func reconcilePendingSpend() {
        guard let p = try? load() else { return }
        let (good, bad) = pendingJournal(p)
        let open = Set((p.unresolved ?? []).map { $0.purchase })
        var apply: [PendingSpendRecord] = []
        for r in pendingSettle + good where open.contains(r.purchase) && !apply.contains(where: { $0.purchase == r.purchase }) { apply.append(r) }
        if !apply.isEmpty {
            let now = Date().timeIntervalSince1970
            do {
                try updateVault(paths, key) { q in
                    for r in apply where (q.unresolved ?? []).contains(where: { $0.purchase == r.purchase }) {
                        q.unresolved = (q.unresolved ?? []).filter { $0.purchase != r.purchase }
                        q.spend = (q.spend ?? []).filter { $0.t >= now - Double(maxCapPeriod) } + r.extras
                    }
                }
            } catch { return }
        }
        pendingSettle.removeAll { r in apply.contains { $0.purchase == r.purchase } || !open.contains(r.purchase) }
        if bad.isEmpty {
            if fileExists(pendingSpendPath) { unlink(pendingSpendPath) }
        } else {
            let sig = sha256Hex(Array(bad.joined(separator: "\n").utf8))
            if sig != pendingBadSeen {
                pendingBadSeen = sig
                record(["op": "spend_pending", "result": "rejected", "lines": bad.count, "event": "payment.pending_rejected"])
                say("          WARNING: spend-pending.jsonl holds \(bad.count) record(s) the vault can't verify: no purchase until you check (`metacomputer secrets spend-resolve`, your own terminal)")
            }
            let keep = Array((bad.joined(separator: "\n") + "\n").utf8)
            if good.contains(where: { r in apply.contains { $0.purchase == r.purchase } }) { try? writeFileAtomic(pendingSpendPath, keep) }
        }
    }

    /// Why a card or shop entry can't be used now: a completion of its own that isn't settled (sealed in the vault), or a
    /// spend-pending.jsonl the vault can't verify (it blocks every purchase). nil when neither.
    func pendingSpendProblem(_ s: Secret, name: String, _ p: Payload) -> String? {
        let id = s.id ?? name
        if let m = (p.unresolved ?? []).first(where: { $0.card == id }) {
            return "a Shop completion with \(s.card_label ?? name) (reserved \(formatMoney(m.amount, m.currency))) isn't settled yet (what Shop charged isn't recorded): no purchase with it until it is (the vault retries with every purchase; `metacomputer secrets spend-resolve \(name)` in your own terminal settles it by hand)"
        }
        if !pendingJournal(p).1.isEmpty {
            return "spend-pending.jsonl holds records the vault can't verify, so no purchase is allowed until you check it (`metacomputer secrets spend-resolve`, your own terminal)"
        }
        return nil
    }

    // MARK: what the hub masks while a purchase is open

    /// Shapes and a fingerprint of a card's parts for the hub (lib/cardmask.py): the security code's length, the postal
    /// code's shape, whether an expiry is in play, and a salted SHA-256 of the name (which confirms a guess, never reveals
    /// it). Never a part itself.
    func maskHints(_ card: CardValue) -> [String: Any] {
        var h: [String: Any] = ["cvc_len": card.cvc.count, "exp": true]
        if let z = card.zip, !z.isEmpty {
            h["zip_shape"] = String(z.map { $0.isNumber ? "9" : ($0.isLetter ? "A" : ($0 == " " ? " " : "-")) })
        }
        // The name's words as the hub's masker reads them (lib/cardmask.py, _WORD): letters, with apostrophes and hyphens
        // inside a word; punctuation and initials' dots between words don't count ("J. Example" is "j example").
        let ns = card.name as NSString
        let words = (try? NSRegularExpression(pattern: "[^\\W\\d_]+(?:['\u{2019}-][^\\W\\d_]+)*"))?
            .matches(in: card.name, range: NSRange(location: 0, length: ns.length)).map { ns.substring(with: $0.range).lowercased() } ?? []
        let norm = words.joined(separator: " ")
        if !norm.isEmpty {
            let salt = randomBytes(16).map { String(format: "%02x", $0) }.joined()
            h["name"] = ["salt": salt, "sha256": sha256Hex(Array((salt + norm).utf8)), "words": norm.split(separator: " ").count]
        }
        return h
    }

    /// The purchases whose card details the hub masks: <vault folder>/purchases-active.json (0600). An open purchase counts
    /// until it expires; any purchase also until its maskUntil: through each fill and a settle margin after it, and after it
    /// ends (endedPurchases), so what a fill made the page show has passed through the hub. Removed when none counts.
    /// At unlock: an earlier unlock's entries (ended or locked since, or a crash) whose window hasn't passed are kept, and
    /// written with every later version of the file until it passes: a new unlock never drops or removes them.
    /// Every entry still in its window, however many; a file that's there but can't be read stops new purchases and is
    /// never rewritten (the hub masks for every shape while it can't read it), rather than lose what it holds.
    func importActivePurchases() {
        let path = paths.dir + "/purchases-active.json"
        guard access(path, F_OK) == 0 else { return }
        guard let b = try? readFileBytes(path, max: 64 << 20), let o = parseJSONObject(b), let list = o["purchases"] as? [Any] else {
            activeFileProblem = "the hub's masking file (purchases-active.json in the vault folder) can't be read, so it isn't "
                + "rewritten and no purchase starts: once no purchase can still be in its window (an hour after the last), remove "
                + "it and unlock again"
            return
        }
        let now = Date().timeIntervalSince1970
        inheritedMasks = list.compactMap { $0 as? [String: Any] }.filter { (($0["until"] as? NSNumber)?.doubleValue ?? 0) > now }   // all of them
    }

    /// Why a new purchase can't start now: the hub's file can't be read, or holds activePurchasesMax purchases still in
    /// their window (none is dropped to make room).
    func activeFileCapacityProblem() -> String? {
        if let why = activeFileProblem { return why }
        let now = Date()
        let n = inheritedMasks.filter { (($0["until"] as? NSNumber)?.doubleValue ?? 0) > now.timeIntervalSince1970 }.count
            + endedPurchases.filter { $0.maskUntil > now }.count + purchases.values.filter { $0.route == "card" }.count
        guard n < activePurchasesMax else {
            return "the hub's masking file already holds \(n) purchases still in their window (at most \(activePurchasesMax)): "
                + "start this one once the oldest have passed (a minute after their last fill)"
        }
        return nil
    }

    @discardableResult
    func writeActivePurchases() -> Bool {
        if activeFileProblem != nil { return false }           // never over what can't be read
        let path = paths.dir + "/purchases-active.json"
        let now = Date()
        endedPurchases.removeAll { $0.maskUntil <= now }
        inheritedMasks.removeAll { (($0["until"] as? NSNumber)?.doubleValue ?? 0) <= now.timeIntervalSince1970 }
        let open = purchases.values.filter { $0.route == "card" && now < max($0.expires, $0.maskUntil) }
        let all = open.map { ($0, max($0.expires, $0.maskUntil)) } + endedPurchases.map { ($0, $0.maskUntil) }
        guard !all.isEmpty || !inheritedMasks.isEmpty else { unlink(path); return true }
        let list: [[String: Any]] = inheritedMasks + all.map { pu, until in
            var d = pu.masks
            d["vm"] = pu.vm
            d["ack"] = pu.ackID
            d["until"] = Int(until.timeIntervalSince1970.rounded(.up))
            d["filled"] = pu.filled
            return d
        }
        return (try? writeFileAtomic(path, Array(jsonLine(["v": 1, "purchases": list]).utf8) + [0x0A])) != nil
    }

    // MARK: authorizing

    /// The card's caps, then its policy: an agent card's purchase within its caps doesn't ask (unless this unlock asks
    /// for every use); anything else asks the user, for this one purchase (never a grant or an unattended rule). The
    /// question and the summary that Touch ID's dialog and a passphrase approval sign name every site that gets a part.
    func authorizePurchase(_ s: Secret, _ p: Payload, name: String, origin: String, frames: [String] = [], money: Money?, vm: String,
                           who: Requester, route: String, lines: [String], notes: [String] = [], proposedID: String?) -> PurchaseAuth {
        let label = s.card_label ?? name
        if let why = capsProblem(s, p, name: name, origin: origin, money: money) { return PurchaseAuth(ok: false, how: "-", requestID: nil, why: why) }
        let total = money.map { formatMoney($0.minor, $0.currency) } ?? "unknown"
        let what = s.kind == "shop" ? "through your Shop account (\(label))" : "with \(label) (card \(name))"
        // What Touch ID's dialog, `requests` and a passphrase approval show and sign: every site that gets a part, in
        // full, then what approving means and what's uncertain. If it can't all be shown, nothing is asked.
        var summary = "pay \(total) \(what) at \(origin)" + (frames.isEmpty ? "" : "; card parts also to \(frames.joined(separator: ", "))") + "."
        if route == "card" && s.single_use != true && s.merchant_locked != true {
            summary += " Approves giving the card, not the charge: a reusable card can be charged again by " + (frames.isEmpty ? "this site." : "these sites.")
        }
        for n in notes { summary += " " + n + "." }
        let dialogPrefix = "approve request 00000000, \(scopeWords(.use, name: name)): "
        guard dialogPrefix.count + summary.count <= maxConfirmText, clean(summary, max: maxConfirmText) == summary else {
            return PurchaseAuth(ok: false, how: "-", requestID: nil,
                                why: "the approval can't show every site that would get the card, in full (\(summary.count) characters): use fewer or shorter payment-form sites, a shorter label, or hand payment to the user")
        }
        if s.pay == "agent" && !confirmEach {
            var rec: [String: Any] = ["op": "authorize", "result": "ok", "use": "pay", "name": name, "label": label, "vm": vm, "origin": origin,
                                      "total": total, "approval": "policy:agent", "route": route, "requester": who.record, "event": "payment.authorize"]
            if !frames.isEmpty { rec["frame_origins"] = frames }
            if let c = s.caps, !c.isEmpty { rec["caps"] = c.words }
            record(rec)
            say("          an agent card: allowed by its policy (\(s.caps?.words ?? "no caps"))")
            return PurchaseAuth(ok: true, how: "policy:agent", requestID: nil, why: "")
        }
        let qlines = ["  Pay \(total) \(what) on VM \(vm)?",
                      "  " + (s.kind == "shop" ? "The store" : "Card parts go to") + ": \(origin)" + (frames.isEmpty ? "" : " (the shop), \(frames.joined(separator: ", ")) (payment forms)")]
            + lines + ["  Asked by \(who.describe)."]
        guard qlines.count <= payQuestionLines, qlines.allSatisfy({ $0.count <= 500 }) else {
            return PurchaseAuth(ok: false, how: "-", requestID: nil, why: "the question can't show everything in full: hand payment to the user")
        }
        let o = approve("pay", name, qlines.joined(separator: "\n"), who, vm: vm, site: origin, use: "pay", summary: summary,
                        proposedID: proposedID, singleUse: true)
        return PurchaseAuth(ok: o.yes, how: o.how, requestID: o.requestID, why: o.why)
    }

    // MARK: reading the total

    /// The page's total, from the tab on `origin`: in page mode the shown element `selector` names (its rendered text, held
    /// to the shop as the tab's own site); in human mode the browser's text after each place `label` is. (money, why not)
    func readTotal(_ t: GuestTarget, origin: String, human: Bool, selector: String?, label: String, profile: String?, browserPID: Int?,
                   windowID: Int?, currency: String?) -> (Money?, String?) {
        let to = TimeInterval(min(60, remaining()))
        if human {
            let (o, texts, befores, why) = runner.humanTotal(t, origins: [origin], label: label, windowID: windowID, timeout: to)
            guard o == origin, let tx = texts, let bs = befores else { return (nil, why ?? "the page isn't on \(origin)") }
            return parsePageTotal(tx, befores: bs, cardCurrency: currency, label: label)
        }
        var rq: [String: Any] = ["mode": "read", "what": "total", "origins": [origin], "top_origins": [origin], "selector": selector ?? "", "field": "text"]
        if let pr = profile { rq["profile"] = pr }
        if let bp = browserPID { rq["browser_pid"] = bp }
        let read: GuestRead
        do { read = try runner.read(t, request: rq, timeout: to) } catch let e as VaultError { return (nil, e.message) } catch {
            return (nil, "reading the page failed")
        }
        guard read.ok, read.origin == origin else { return (nil, read.reason ?? "the tab isn't on \(origin)") }
        guard read.topChecked, read.visible != nil else {
            return (nil, "this VM's fill helper is older than purchases need (it can't hold the tab to the shop or tell a shown total): rebuild the VM")
        }
        guard read.visible == true else { return (nil, "the element --total-selector names isn't shown on the page") }
        return parsePageTotal([read.text], cardCurrency: currency, label: label)
    }

    // MARK: pay: start a purchase

    func opPay(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        var fields = echo(req, ["name", "vm", "origin"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, let origin = req["origin"] as? String,
              validName(name), validName(vm), canonicalOrigin(origin) == origin else {
            return payFinish("pay", fields, failed("bad request"), who, approval: "-")
        }
        let human = req["human"] as? Bool == true
        let totalSel = req["total_selector"] as? String
        let totalLabel = (req["total_label"] as? String) ?? "Total"
        let frames = (req["frame_origins"] as? [Any] ?? []).compactMap { $0 as? String }
        let profile = req["profile"] as? String
        let browserPID = (req["browser_pid"] as? NSNumber)?.intValue
        let windowID = (req["window_id"] as? NSNumber)?.intValue
        let fills = (req["fills"] as? [Any] ?? []).compactMap { $0 as? [String] }
        guard frames.count <= payFramesMax, Set(frames).count == frames.count, frames.allSatisfy({ canonicalOrigin($0) == $0 && $0 != origin && $0.hasPrefix("https://") }), profile.map(validName) ?? true,
              fills.count <= cardParts.count, fills.allSatisfy({ $0.count == 2 && cardParts.contains($0[0]) && !$0[1].isEmpty && $0[1].count <= 300 }),
              !totalLabel.isEmpty, totalLabel.count <= 40, clean(totalLabel, max: 40) == totalLabel, windowID.map({ $0 > 0 }) ?? true,
              human ? (totalSel == nil && fills.isEmpty && frames.isEmpty && profile == nil && browserPID == nil)
                    : (totalSel.map { !$0.isEmpty && $0.count <= 300 } ?? false) && windowID == nil else {
            return payFinish("pay", fields, failed("bad request"), who, approval: "-")
        }
        if let w = payableLabelProblem(totalLabel) { return payFinish("pay", fields, denied(w), who, approval: "-") }
        reconcilePendingSpend()
        if let long = ([origin] + frames).first(where: { $0.count > payOriginMax }) {
            return payFinish("pay", fields, denied("\(long) is longer than an approval can show in full (\(payOriginMax) characters): hand payment to the user"), who, approval: "-")
        }
        if let why = browserBindingProblem(req, profile: profile, who) { return payFinish("pay", fields, denied(why), who, approval: "-") }
        if let why = activeFileCapacityProblem() { return payFinish("pay", fields, denied(why), who, approval: "-") }
        if who.claudePID == nil && who.hostPID == nil && (human || fills.isEmpty) {
            // Nothing to bind a later part to: such a purchase would be anyone's who learns its key.
            return payFinish("pay", fields, denied("this request doesn't come from an agent session (or an agent's shared process, like Codex's app server), so a purchase it starts can't be resumed: name every part with --fill in this pay call, or run it from the agent's session"), who, approval: "-")
        }
        if human { fields["mode"] = "human" }
        if !frames.isEmpty { fields["frame_origins"] = frames }
        say("\(clockTime())  pay with \(name) at \(origin) on VM \(vm)" + (human ? ", human-mode browser" : "")
            + (frames.isEmpty ? "" : ", card fields in frames from \(frames.joined(separator: ", "))") + ", asked by \(who.describe)")
        let p: Payload
        do { p = try load() } catch let e as VaultError {
            return payFinish("pay", fields, e.code == Exit.locked ? lockedResult(e.message) : failed(e.message), who, approval: "-")
        } catch { return payFinish("pay", fields, failed("cannot read the vault"), who, approval: "-") }
        guard let s = p.secrets[name] else { return payFinish("pay", fields, failed("no card named \(name) (metacomputer secrets list)"), who, approval: "-") }
        guard s.kind == "card", let card = decodeCard(s) else {
            return payFinish("pay", fields, denied("\(name) is a \(s.kind) secret, not a card" + (s.kind == "shop" ? ": purchases through your Shop account go through `metacomputer shop complete`" : "")), who, approval: "-")
        }
        fields["label"] = s.card_label ?? name
        if s.single_use == true && (s.spent == true || purchases.values.contains { $0.cardID == (s.id ?? name) }) {
            return payFinish("pay", fields, denied("\(name) is a single-use card and its one purchase has started already"), who, approval: "-")
        }
        if !s.origins.isEmpty && !s.origins.contains(origin) {
            return payFinish("pay", fields, denied("\(name) pays only at \(s.origins.joined(separator: ", ")), not \(origin)"), who, approval: "-")
        }
        let agent = s.pay == "agent" && !confirmEach
        if agent, let bad = frames.first(where: { !(s.allowed_frames ?? []).contains($0) }) {
            return payFinish("pay", fields, denied("\(name) is an agent card and nobody is asked, so it gives card parts to a payment form's site only when you allowed it: \(bad) isn't (metacomputer secrets policy \(name) --allow-frame URL, in your own terminal)"), who, approval: "-")
        }
        if let why = projectProblem(name, s, who) { return payFinish("pay", fields, denied(why), who, approval: "-") }
        for (part, _) in fills.map({ ($0[0], $0[1]) }) where card.part(part) == nil {
            return payFinish("pay", fields, denied("\(name) has no \(cardPartWords[part] ?? part) stored; leave out --fill \(part)"), who, approval: "-")
        }
        guard let t = target(p, vm) else { return payFinish("pay", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), who, approval: "-") }
        if let why = requiredSinkProblem("pay", fields, who) { return payFinish("pay", fields, denied(why), who, approval: "-") }
        if let problem = runner.listenerProblem(t) {
            return payFinish("pay", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: "-")
        }

        // 1. The merchant's own rules for agents, read from this Mac.
        let rules = merchantRules(origin, wba: p.wba)
        fields["rules"] = rules.record
        say("          merchant's agent rules: \(rules.summary)")
        if rules.forbidden {
            fields["handoff"] = "metacomputer handoff --why payment --say \"Please enter the payment details and place the order\""
            return payFinish("pay", fields, denied("\(origin): \(rules.summary). Stop at the review step and hand payment to the user (metacomputer handoff --why payment)"
                                                   + (rules.shopify ? "; or check out through their own Shop account (metacomputer shop)" : "")), who, approval: "-")
        }
        if !rules.known && agent {
            fields["handoff"] = "metacomputer handoff --why payment --say \"Please enter the payment details and place the order\""
            return payFinish("pay", fields, denied("\(origin)'s agent rules: \(rules.summary), and an agent card doesn't ask you: refused. Hand payment to the user"), who, approval: "-")
        }
        if requesterGone() { return payFinish("pay", fields, ["ok": false, "status": "cancelled", "reason": requesterLeftReason], who, approval: "-") }

        // 2. The total, as the page shows it in the tab on that site (the page's own claim).
        let cur = s.caps?.currency
        let (moneyOpt, mwhy) = readTotal(t, origin: origin, human: human, selector: totalSel, label: totalLabel, profile: profile,
                                         browserPID: browserPID, windowID: windowID, currency: cur)
        guard let money = moneyOpt else {
            fields["handoff"] = "metacomputer handoff --why payment --say \"Please check the total and pay\""
            return payFinish("pay", fields, denied("the total couldn't be read: \(mwhy ?? "unclear"). When it's unclear, hand payment to the user"), who, approval: "-")
        }
        let total = formatMoney(money.minor, money.currency)
        fields["total"] = total
        say("          the page says the total is \(total)")

        // 3. Caps and the card's policy (the user's approval, by default).
        var lines = ["  The page says the total is \(total) (after \"\(totalLabel)\"): the page's own claim (check it in the live view, metacomputer view, if unsure)."]
        if s.single_use != true && s.merchant_locked != true {
            lines.append("  This approves giving the card to these sites, not the charge: a reusable card can be charged again by this site, for any amount.")
        }
        lines.append("  Merchant's agent rules: \(rules.summary)." + (s.caps.map { "  Caps: \($0.words)." } ?? ""))
        let auth = authorizePurchase(s, p, name: name, origin: origin, frames: frames, money: money, vm: vm, who: who, route: "card", lines: lines,
                                     notes: rules.known ? [] : ["Store's agent rules: can't tell"], proposedID: req["request_id"] as? String)
        if let c = s.caps, !c.isEmpty { fields["caps"] = c.words }
        guard auth.ok else { return payFinish("pay", fields, denied(auth.why), who, approval: auth.how, requestID: auth.requestID) }
        guard Date() < expires, receivedSignal == 0 else {
            return payFinish("pay", fields, lockedResult("the vault locked before the purchase started"), who, approval: auth.how, requestID: auth.requestID)
        }
        // The card and its policy as they are now: a change while it asked (a new value, a tighter policy) ends it here.
        guard let p2 = try? load(), let s2 = p2.secrets[name], (s2.id ?? name) == (s.id ?? name), secretGen(s2) == secretGen(s) else {
            return payFinish("pay", fields, denied("\(name) or its policy changed while it asked: start another purchase"), who, approval: auth.how, requestID: auth.requestID)
        }
        if let why = capsProblem(s2, p2, name: name, origin: origin, money: money) {
            return payFinish("pay", fields, denied(why), who, approval: auth.how, requestID: auth.requestID)
        }
        let pu = Purchase(name: name, s: s2, vm: vm, origin: origin, frameOrigins: frames, human: human, profile: profile, browserPID: browserPID,
                          windowID: windowID, money: money, approval: auth.how, requestID: auth.requestID, who: who, route: "card",
                          expires: min(Date().addingTimeInterval(TimeInterval(purchaseLifetime())), expires))
        pu.totalSelector = human ? nil : totalSel
        pu.totalLabel = totalLabel
        pu.masks = maskHints(card)
        purchases[pu.key] = pu
        writeActivePurchases()
        fields["purchase"] = pu.id
        fields["approval"] = auth.how
        fields["expires_in_s"] = Int(pu.expires.timeIntervalSinceNow)
        var started = payFinish("pay", fields, ["ok": true, "status": "ok"], who, approval: auth.how, requestID: auth.requestID)
        started["purchase_id"] = pu.key                 // the capability goes to the requester only, never to a record
        guard !fills.isEmpty else { return started }
        // 4. The parts named with --fill, in order, into the tab on that site; it stops at the first that fails.
        var out = started
        var failedPart: [String: Any]? = nil
        for f in fills {
            let r = payPartFill(pu, part: f[0], selector: f[1], who)
            if r["ok"] as? Bool != true {
                failedPart = ["field": f[0], "reason": r["reason"] ?? "it failed"]
                out["ok"] = false
                out["status"] = r["status"] ?? "error"
                out["reason"] = "\(f[0]): \(r["reason"] as? String ?? "it failed")"
                break
            }
        }
        out["filled"] = pu.filled
        if let fp = failedPart { out["failed"] = fp }
        return out
    }

    // MARK: pay --purchase KEY --field PART

    /// Why the page's total is now over the one approved (or can't be read again), or nil.
    func totalRaised(_ pu: Purchase, _ t: GuestTarget) -> String? {
        guard let approved = pu.money else { return nil }
        let (m, why) = readTotal(t, origin: pu.origin, human: pu.human, selector: pu.totalSelector, label: pu.totalLabel, profile: pu.profile,
                                 browserPID: pu.browserPID, windowID: pu.windowID, currency: approved.currency)
        guard let now = m else { return "the total couldn't be read again before this part (\(why ?? "unclear")); nothing was typed" }
        if now.currency != approved.currency || now.minor > approved.minor {
            return "the page's total is now \(formatMoney(now.minor, now.currency)), over the \(pu.total) you approved: nothing was typed. Start another purchase (it asks again)"
        }
        return nil
    }

    /// One part into the tab on the purchase's site: the card and its policy still the approved ones, the total read
    /// again, the tab checked without the value (the shop as the tab's own site, a frame only from the sites approved),
    /// the purchase in the spending record, then the part. Each part once.
    func payPartFill(_ pu: Purchase, part: String, selector: String?, _ who: Requester) -> [String: Any] {
        var fields: [String: Any] = ["purchase": pu.id, "name": pu.name, "label": pu.label, "vm": pu.vm, "field": part]
        if pu.human { fields["mode"] = "human" }
        guard Date() < pu.expires else { return payFinish("pay_fill", fields, denied("purchase \(pu.id) has expired: start another (it asks again)"), who, approval: pu.approval) }
        guard !pu.filled.contains(part) else {
            return payFinish("pay_fill", fields, denied("\(part) was filled already in this purchase (each part goes in once; start another purchase to fill it again)"), who, approval: pu.approval)
        }
        guard (pu.tries[part] ?? 0) < purchasePartTries else {
            return payFinish("pay_fill", fields, denied("\(part) was tried \(purchasePartTries) times in this purchase: hand payment to the user"), who, approval: pu.approval)
        }
        pu.tries[part, default: 0] += 1
        let p: Payload
        do { p = try load() } catch { return payFinish("pay_fill", fields, failed("cannot read the vault"), who, approval: pu.approval) }
        guard let s = p.secrets[pu.name], s.kind == "card", (s.id ?? pu.name) == pu.cardID, secretGen(s) == pu.gen, let card = decodeCard(s) else {
            purchases[pu.key] = nil
            endPurchase(pu, outcome: "card_changed", who)
            return payFinish("pay_fill", fields, denied("the card, or its policy, changed after the purchase was approved: this purchase has ended; start another"), who, approval: pu.approval)
        }
        guard let v = card.part(part) else { return payFinish("pay_fill", fields, denied("\(pu.name) has no \(cardPartWords[part] ?? part) stored"), who, approval: pu.approval) }
        guard let t = target(p, pu.vm) else { return payFinish("pay_fill", fields, failed("VM \(pu.vm) isn't bound to the vault"), who, approval: pu.approval) }
        if let problem = runner.listenerProblem(t) {
            return payFinish("pay_fill", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: pu.approval)
        }
        say("\(clockTime())  purchase \(pu.id): \(cardPartWords[part] ?? part) into \(pu.origin) on VM \(pu.vm)" + (selector.map { ", selector '\(promptText($0, max: 80))'" } ?? ", the focused field"))
        // The total again: a page whose total went up since you approved gets nothing more (a lower one is fine).
        if let why = totalRaised(pu, t) {
            return payFinish("pay_fill", fields, denied(why), who, approval: pu.approval)
        }
        // Page mode: the tab itself must be on the shop (top_origins); a payment form's site is allowed only for a frame.
        var base: [String: Any] = pu.human ? ["field": "card", "origins": [pu.origin]]
            : ["field": "text", "origins": [pu.origin] + pu.frameOrigins, "top_origins": [pu.origin]]
        if let pr = pu.profile { base["profile"] = pr }
        if let bp = pu.browserPID { base["browser_pid"] = bp }
        if let w = pu.windowID { base["window_id"] = w }
        if let sel = selector { base["selector"] = sel }
        let to = TimeInterval(min(60, remaining()))
        // A part's bytes go to ssh only while the purchase is still open: checked last, after every slow step on the way
        // (the provider's check of a computer elsewhere included), right before they're written (Guest.ssh's release).
        let release: () -> String? = { [self] in
            if Date() >= pu.expires { return "purchase \(pu.id) expired before its part was sent: nothing was typed; start another (it asks again)" }
            if Date() >= expires || receivedSignal != 0 { return "the vault locked before the value was sent: nothing was typed" }
            return nil
        }
        func guest(_ rq: [String: Any], _ value: [UInt8]?) -> [String: Any] {
            do {
                let rel = value == nil ? nil : release
                return pu.human ? try runner.human(t, request: rq, value: value, timeout: to, minWithhold: 6, release: rel)
                                : try runner.fill(t, request: rq, value: value, timeout: to, minWithhold: 6, release: rel)
            } catch let e as VaultError { return e.code == Exit.denied ? denied(e.message) : failed(e.message) } catch { return failed("the fill failed") }
        }
        var chk = base
        chk["mode"] = "check"
        let c = guest(chk, nil)
        guard c["ok"] as? Bool == true else { return payFinish("pay_fill", fields, c, who, approval: pu.approval) }
        guard c["origin"] as? String == pu.origin else {
            return payFinish("pay_fill", fields, denied("the tab isn't on \(pu.origin) (the site this purchase was approved for); nothing was typed"), who, approval: pu.approval)
        }
        if !pu.human && c["top_checked"] as? Bool != true {
            return payFinish("pay_fill", fields, denied("this VM's fill helper is older than purchases need (it can't hold the tab to the shop): rebuild the VM; nothing was typed"), who, approval: pu.approval)
        }
        guard Date() < expires, receivedSignal == 0, !requesterGone() else {
            return payFinish("pay_fill", fields, lockedResult("the vault locked (or the request was withdrawn) before the value was sent"), who, approval: pu.approval)
        }
        // The hub's masks cover the whole fill and a settle margin after it, whenever the purchase ends meanwhile: the ack
        // wait, the fill's own time limit and the margin. A new ack id for this window: the hub for that VM must say it
        // has seen it (so its masks are on, for this long) before anything of the card leaves.
        pu.maskUntil = max(pu.maskUntil, Date().addingTimeInterval(5 + to + TimeInterval(purchaseSettleSeconds)))
        pu.ackID = newAckID()
        guard writeActivePurchases() else {
            return payFinish("pay_fill", fields, denied("the hub's masks for this fill couldn't be written (purchases-active.json in the vault folder): nothing was typed"), who, approval: pu.approval)
        }
        // However this attempt ends (sent, refused, failed), the window then runs a settle margin from that moment.
        defer {
            pu.maskUntil = Date().addingTimeInterval(TimeInterval(purchaseSettleSeconds))
            writeActivePurchases()
        }
        if let why = hubAckProblem(pu) {
            return payFinish("pay_fill", fields, denied(why), who, approval: pu.approval)
        }
        // The purchase's own time again, right before anything leaves (the checks above take a while).
        guard Date() < pu.expires else {
            return payFinish("pay_fill", fields, denied("purchase \(pu.id) expired while its page was checked: nothing was typed; start another (it asks again)"), who, approval: pu.approval)
        }
        // Counted before anything leaves the vault, and never undone: a fill that fails may still have handed it over.
        if let why = reserveSpend(pu) {
            return payFinish("pay_fill", fields, denied(why), who, approval: pu.approval)
        }
        var value = Array(v.utf8)
        defer { wipe(&value) }
        var rq = base
        rq["mode"] = "fill"
        var r = guest(rq, value)
        if r["ok"] as? Bool == true && (r["origin"] as? String != pu.origin || (!pu.human && r["top_checked"] as? Bool != true)) {
            r = failed("the VM reported another site than \(pu.origin) after typing; check the page")
        }
        if r["ok"] as? Bool == true { pu.filled.append(part) }
        fields["origin"] = pu.origin
        var res = r
        res["chars"] = nil                       // a card number's length says too much
        var out = payFinish("pay_fill", fields, res, who, approval: pu.approval)
        out["filled"] = pu.filled
        return out
    }

    /// Why a part may not leave yet: the hub for the purchase's VM hasn't said it has seen this purchase, within 5 s. The
    /// hub reads the open purchases fresh for every result; this makes sure it's running and has read this one before the
    /// card can show up in anything it passes on. What it says (hubAckCheck) has to come from the hub: the 0700 ack folder
    /// of this user's, a private file naming the purchase's ack id, a process of this user's that started when it says, and
    /// the hub's lock (RUN_DIR/hub/hub.pid), held now and naming that same process. Another process running as this user
    /// could still forge all of it (SECURITY.md, "Accepted limits"): this is against mistakes and stale state, not that.
    func hubAckProblem(_ pu: Purchase) -> String? {
        let safeVM = String(pu.vm.map { $0.isLetter || $0.isNumber || "._-".contains($0) ? $0 : "_" })
        let dir = paths.dir + "/hub-acks"
        let deadline = Date().addingTimeInterval(5)
        var why = "no word from it"
        while true {
            guard let w = hubAckCheck(dir: dir, path: dir + "/" + safeVM + ".json", pu) else { return nil }
            why = w
            if Date() >= deadline || requesterGone() { break }
            usleep(50_000)
        }
        return "the hub for VM \(pu.vm) hasn't confirmed it hides card details for this purchase (\(why); payments need the hub: MCP_HUB=on, and it running): nothing was typed"
    }

    /// nil when the ack for `pu` holds (hubAckProblem), else what's wrong with it.
    func hubAckCheck(dir: String, path: String, _ pu: Purchase) -> String? {
        guard privatePath(dir, folder: true) else { return "its ack folder isn't a private folder of this user's" }
        guard privatePath(path, folder: false), let b = try? readFileBytes(path, max: 64 << 10), let o = parseJSONObject(b) else { return "no word from it" }
        guard o["vm"] as? String == pu.vm, let seen = o["seen"] as? [Any], seen.contains(where: { $0 as? String == pu.ackID }) else {
            return "it hasn't said it has seen this purchase"
        }
        guard let pid = (o["pid"] as? NSNumber)?.int32Value, pid > 1, let st = o["start"] as? [Any], st.count == 2,
              let s0 = (st[0] as? NSNumber)?.uint64Value, let s1 = (st[1] as? NSNumber)?.uint64Value,
              let lock = o["lock"] as? String, lock.hasPrefix("/") else { return "its word doesn't name its process and lock" }
        guard let p = procStartUID(pid), p.uid == getuid(), p.sec == s0, p.usec == s1 else { return "the process it names isn't running (or is another one now)" }
        guard privatePath((lock as NSString).deletingLastPathComponent, folder: true), privatePath(lock, folder: false) else {
            return "its lock isn't a private file of this user's"
        }
        let fd = open(lock, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        guard fd >= 0 else { return "its lock can't be read" }
        defer { close(fd) }
        if flock(fd, LOCK_SH | LOCK_NB) == 0 {
            _ = flock(fd, LOCK_UN)
            return "nothing holds its lock"
        }
        guard errno == EWOULDBLOCK else { return "its lock can't be checked" }
        var buf = [UInt8](repeating: 0, count: 4096)
        let n = read(fd, &buf, buf.count)
        guard n > 0, let rec = parseJSONObject(Array(buf[0..<n])), (rec["pid"] as? NSNumber)?.int32Value == pid,
              let rs = rec["start"] as? [Any], rs.count == 2, (rs[0] as? NSNumber)?.uint64Value == s0, (rs[1] as? NSNumber)?.uint64Value == s1 else {
            return "the hub's lock names another process"
        }
        return nil
    }

    /// The purchase a requester names by its key, if it's theirs: a purchase started from a Claude Code (or Codex) session
    /// belongs to that session; any other is held by its key alone, which only its requester was given.
    /// The open purchase a request's key names, if the requester is whom it's bound to: the agent session that started
    /// it, or the shared agent process it came from (Codex's app server, an OpenClaw or Hermes Agent gateway: their
    /// conversations aren't told apart). One started outside both can't be resumed (its own pay call filled what it
    /// named); the key still ends it (`ending`).
    func ownPurchase(_ req: [String: Any], _ who: Requester, ending: Bool = false) -> (Purchase?, String?) {
        guard let key = req["purchase"] as? String, validPurchaseKey(key) else { return (nil, "bad request: --purchase takes the purchase_id pay printed (32 hex digits)") }
        guard let pu = purchases[key] else { return (nil, "no such purchase is open (it ended, expired or the vault locked since): start another") }
        if let cp = pu.claudePID {
            guard who.claudePID == cp && abs((who.claudeStart ?? -1) - (pu.claudeStart ?? -2)) < 0.001 else {
                return (nil, "purchase \(pu.id) belongs to another \(pu.agentName ?? "agent") session")
            }
        } else if let hp = pu.hostPID {
            guard who.claudePID == nil, who.hostPID == hp, abs((who.hostStart ?? -1) - (pu.hostStart ?? -2)) < 0.001 else {
                return (nil, "purchase \(pu.id) belongs to another \(pu.agentName ?? "agent process")")
            }
        } else if !ending {
            return (nil, "purchase \(pu.id) was started outside an agent session, so it can't be resumed: only its own pay call fills it (--fill)")
        }
        return (pu, nil)
    }

    func opPayFill(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        var fields = echo(req, ["vm", "field"])
        guard let vm = req["vm"] as? String, validName(vm), let part = req["field"] as? String, cardParts.contains(part),
              (req["selector"] as? String).map({ !$0.isEmpty && $0.count <= 300 }) ?? (req["selector"] == nil) else {
            return payFinish("pay_fill", fields, failed("bad request"), who, approval: "-")
        }
        let (puOpt, why) = ownPurchase(req, who)
        guard let pu = puOpt else { return payFinish("pay_fill", fields, denied(why ?? "no such purchase"), who, approval: "-") }
        fields["purchase"] = pu.id
        fields["name"] = pu.name
        fields["label"] = pu.label
        guard vm == pu.vm else { return payFinish("pay_fill", fields, denied("purchase \(pu.id) is for VM \(pu.vm)"), who, approval: pu.approval) }
        let human = req["human"] as? Bool == true
        guard human == pu.human else {
            return payFinish("pay_fill", fields, denied("purchase \(pu.id) was started for " + (pu.human ? "a human-mode browser: add --human" : "page-mode Chrome: leave out --human")), who, approval: pu.approval)
        }
        if pu.human && req["selector"] != nil {
            return payFinish("pay_fill", fields, failed("bad request: a human-mode fill types into the focused field, without --selector"), who, approval: pu.approval)
        }
        return payPartFill(pu, part: part, selector: req["selector"] as? String, who)
    }

    // MARK: ending one

    /// The purchase ends: its record (the parts it filled, whether it counts against the caps); a single-use card that
    /// gave out any part is deleted from the vault.
    @discardableResult
    func endPurchase(_ pu: Purchase, outcome: String, _ who: Requester?) -> [String: Any] {
        purchases[pu.key] = nil
        // A card purchase that tried any part stays masked a settle margin longer (and through any fill's window).
        if pu.route == "card" && (pu.counted || !pu.tries.isEmpty) && !endedPurchases.contains(where: { $0 === pu }) {
            pu.maskUntil = max(pu.maskUntil, Date().addingTimeInterval(TimeInterval(purchaseSettleSeconds)))
            endedPurchases.append(pu)
        }
        writeActivePurchases()
        var rec: [String: Any] = ["op": "pay_end", "result": "ok", "purchase": pu.id, "name": pu.name, "label": pu.label, "vm": pu.vm,
                                  "origin": pu.origin, "total": pu.total, "outcome": outcome, "filled": pu.filled, "counted": pu.counted,
                                  "route": pu.route, "event": "payment.end"]
        if let w = who { rec["requester"] = w.record }
        if pu.singleUse && pu.counted {
            do {
                try updateVault(paths, key) { p in
                    if let s = p.secrets[pu.name], (s.id ?? pu.name) == pu.cardID { p.secrets[pu.name] = nil }
                }
                rec["single_use_deleted"] = true
            } catch { say("          (couldn't delete the single-use card \(pu.name))") }
        }
        record(rec)
        say("\(clockTime())  purchase \(pu.id) (\(pu.label) at \(pu.origin)) ended: \(outcome)" + (pu.filled.isEmpty ? "" : ", filled \(pu.filled.joined(separator: ", "))")
            + ((rec["single_use_deleted"] as? Bool) == true ? "; the single-use card is deleted" : ""))
        var out = rec
        out["ok"] = true
        out["status"] = "ok"
        out["op"] = nil
        out["event"] = nil
        out["result"] = nil
        out["requester"] = nil
        return out
    }

    func opPayEnd(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        let (puOpt, why) = ownPurchase(req, who, ending: true)
        guard let pu = puOpt else { return denied(why ?? "no such purchase") }
        // --vm, when given, must be the purchase's own VM (`pay --purchase KEY --done --vm VM`; it takes nothing else).
        if let vm = req["vm"] as? String, vm != pu.vm {
            return denied("purchase \(pu.id) is on VM \(pu.vm), not \(clean(vm, max: 64)): nothing was ended")
        }
        let r = endPurchase(pu, outcome: "done", who)
        flushSinks(force: true)
        return r
    }

    /// Purchases past their time end (from the main loop and while a question waits).
    func purchaseSweep() {
        for pu in purchases.values where Date() >= pu.expires { endPurchase(pu, outcome: "expired", nil) }
        let now = Date()
        if endedPurchases.contains(where: { $0.maskUntil <= now })
            || inheritedMasks.contains(where: { (($0["until"] as? NSNumber)?.doubleValue ?? 0) <= now.timeIntervalSince1970 }) {
            writeActivePurchases()
        }
    }

    /// The vault is locking: every open purchase ends.
    func purchaseLockdown() {
        for pu in purchases.values { endPurchase(pu, outcome: "locked", nil) }
        writeActivePurchases()              // what's still masked stays listed (its time is in it); nothing else
    }

    // MARK: Shop's CLI: only the one the user set, checked, with a fixed command and a clean environment

    /// Run the pinned `shop` CLI, confined: (exit status, its JSON answer, why not). The runtime, the libraries it loads
    /// and every file of the package must still be the ones `shop-cli set` pinned. It runs in a fresh folder of the
    /// vault's own (its home, its temporary folder and its working folder, removed afterwards) with a fixed command and
    /// environment: PATH=/usr/bin:/bin, no NODE_OPTIONS, NODE_PATH or npm settings, OpenSSL's configuration off, and of
    /// your home only the sign-in file the user pinned (SHOP_AUTH_FILE). Node's module hooks hold every module it loads to
    /// the package (shopConfinementArgs); `prepare` puts files in the folder first (the output file's name, say).
    func runShop(_ args: [String], stdin: [UInt8] = [], timeout: TimeInterval, prepare: ((String) -> [String])? = nil,
                 collect: ((String) -> Void)? = nil) -> (Int32, [String: Any]?, String?) {
        guard let p = try? load() else { return (-1, nil, "cannot read the vault") }
        guard let pin = p.shop_cli else {
            return (-1, nil, "Shop's CLI isn't set up for the vault: the user runs `metacomputer secrets shop-cli set` in their own terminal")
        }
        if let why = verifyShopPin(pin) { return (-1, nil, why) }
        let run = paths.dir + "/shop-run-" + randomBytes(8).map { String(format: "%02x", $0) }.joined()
        do { try ensurePrivateDir(run) } catch { return (-1, nil, "cannot make a folder for Shop's CLI") }
        defer { removeTree(run) }
        let extra = prepare?(run) ?? []
        var env = ["PATH=/usr/bin:/bin", "HOME=\(run)", "TMPDIR=\(run)/", "LANG=C.UTF-8", "NO_COLOR=1", "OPENSSL_CONF=/dev/null",
                   "SHOP_AUTH_FILE=\(pin.auth_file ?? "")"]
        // The OS confines it (a Seatbelt profile, sandbox-exec), and Node's module hooks besides; without the sandbox it
        // doesn't run at all.
        let sbx = sandboxExecPath()
        var sbxOK = access(sbx, X_OK) == 0
        #if !MCOMP_VAULT_TESTING
        sbxOK = sbxOK && sbx == "/usr/bin/sandbox-exec" && runPathProblem(sbx) == nil
        #endif
        guard sbxOK else {
            return (-1, nil, "Shop's CLI wasn't run: macOS's sandbox-exec isn't available here, and the vault doesn't run it unconfined")
        }
        var spy: [String] = []
        var testAllow = Set<String>()
        var testOwn = Set<[UInt8]>()
        #if MCOMP_VAULT_TESTING
        if let d = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_SHOP_SPY"] {
            spy = [d]                                                         // the stand-in CLI's records
            if let a = try? String(contentsOfFile: d + "/proxy-allow", encoding: .utf8) {            // a stand-in merchant on loopback
                testAllow = Set(a.split(whereSeparator: { $0.isWhitespace }).map(String.init))
            }
            if let o = try? String(contentsOfFile: d + "/proxy-own", encoding: .utf8) {               // stand-in own addresses
                for w in o.split(whereSeparator: { $0.isWhitespace }) {
                    var a4 = in_addr(), a6 = in6_addr()
                    if inet_pton(AF_INET, String(w), &a4) == 1 { testOwn.insert(withUnsafeBytes(of: a4) { Array($0) }) }
                    else if inet_pton(AF_INET6, String(w), &a6) == 1 { testOwn.insert(withUnsafeBytes(of: a6) { Array($0) }) }
                }
            }
        }
        #endif
        // Its only network: a proxy of this run's own that reaches public hosts on 443, nothing local.
        guard let proxy = ShopProxy(testAllow: testAllow, testOwn: testOwn) else { return (-1, nil, "Shop's CLI wasn't run: its network proxy couldn't be opened") }
        proxy.start()
        defer { proxy.stop() }
        env += ["HTTPS_PROXY=\(proxy.url)", "HTTP_PROXY=\(proxy.url)"]
        guard var profile = shopSandboxProfile(pin, run: run, proxyPort: proxy.port, extraRW: spy) else {
            return (-1, nil, "Shop's CLI wasn't run: a pinned path can't be named in the sandbox's profile (a quote or a control character in it)")
        }
        #if MCOMP_VAULT_TESTING
        if let d = spy.first, fileExists(d + "/sandbox-break") { profile += "(this is not a rule" }      // a profile that fails to apply
        #endif
        let r = runProcess(sbx, ["-p", profile, pin.runtime] + shopConfinementArgs(root: pin.root) + [pin.script] + args + extra, env: env,
                           stdin: stdin, timeout: timeout, maxStdout: 1 << 20, cwd: run)
        collect?(run)
        if r.cancelled { return (-1, nil, requesterLeftReason) }
        if r.timedOut { return (-1, nil, "Shop's CLI didn't answer in time") }
        let err = String(decoding: r.stderr.suffix(4096), as: UTF8.self)
        if r.status != 0 && String(decoding: r.stderr.prefix(64), as: UTF8.self).hasPrefix("sandbox-exec:") {
            return (-1, nil, "Shop's CLI wasn't run: its sandbox couldn't be applied, and the vault doesn't run it unconfined")
        }
        if r.status == 97 && err.contains("metacomputer-confinement: no module.registerHooks") {
            return (-1, nil, "Shop's CLI wasn't run: its Node can't be confined (it needs module.registerHooks, Node 22.15 or newer); update Node, then run `metacomputer secrets shop-cli set` again, in your own terminal")
        }
        if r.status == 98 && err.contains("metacomputer-confinement: a module outside the pinned package") {
            return (-1, nil, "Shop's CLI tried to load code from outside its pinned package, and was stopped: nothing it would have done happened. Check the package (and any package.json or node_modules above it)")
        }
        let last = String(decoding: r.stdout, as: UTF8.self).split(separator: "\n").last.map { Array($0.utf8) } ?? []
        lastShopRaw = last
        return (r.status, parseJSONObject(last), nil)
    }

    // MARK: shop-card: a Shop spend request's single-use card, straight into the vault

    func opShopCard(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        var fields = echo(req, ["name"])
        guard let name = req["name"] as? String, validName(name), let sr = req["spend_request"] as? String, matches(sr, "^[A-Za-z0-9_-]{8,80}$"),
              let wait = (req["wait"] as? NSNumber)?.intValue, wait >= 0, wait <= 120, req["shop_cli"] == nil, req["path"] == nil else {
            return payFinish("shop_card", fields, failed("bad request"), who, approval: "-")
        }
        fields["spend_request"] = sr
        let p: Payload
        do { p = try load() } catch { return payFinish("shop_card", fields, failed("cannot read the vault"), who, approval: "-") }
        guard p.secrets[name] == nil else { return payFinish("shop_card", fields, denied("a secret named \(name) exists already: choose another name"), who, approval: "-") }
        // Into the CLI's own folder (the vault's, removed afterwards): the CLI writes the card there (0600, never over a
        // file) and this process reads it before the folder goes. Its standard output names the file instead of the card.
        say("\(clockTime())  shop-card \(name): Shop's single-use card for spend request \(sr), waiting up to \(wait) s for your approval in Shop; asked by \(who.describe)")
        var cardBytes: [UInt8]? = nil
        var file = ""
        let (status, answer, why) = runShop(["spend-request", "get", sr, "--include", "card", "--wait", String(wait)], timeout: TimeInterval(wait + 60),
                                            prepare: { dir in file = dir + "/card.json"; return ["--output-file", file] },
                                            collect: { _ in cardBytes = try? readFileBytes(file, max: 64 << 10); scrubFile(file) })
        defer { if var b = cardBytes { wipe(&b) } }
        if let why { return payFinish("shop_card", fields, why == requesterLeftReason ? ["ok": false, "status": "cancelled", "reason": why] : denied(why), who, approval: "-") }
        // Its own words aren't passed on: only fields of fixed shapes from its JSON answer.
        let data = answer?["data"] as? [String: Any] ?? [:]
        guard status == 0, answer?["ok"] as? Bool == true, let named = data["credential_output_file"] as? String, named == file || realPath(named) == realPath(file) else {
            let code = ((answer?["error"] as? [String: Any])?["code"] as? String).flatMap { matches($0, "^[a-z_]{1,40}$") ? $0 : nil }
            return payFinish("shop_card", fields, failed("Shop's CLI gave no card" + (code.map { " (\($0))" } ?? "")
                                                       + ": is the spend request approved in Shop? (`shop spend-request get \(sr)` shows its status)"), who, approval: "-")
        }
        // Every restriction the card comes with must be there, and match: the request, its state, the store, the limit.
        guard data["status"] as? String == "consumed" else { return payFinish("shop_card", fields, failed("Shop says the spend request isn't consumed (approved and issued): nothing imported"), who, approval: "-") }
        var siteOK: (String) -> Bool = { $0.hasPrefix("https://") }
        #if MCOMP_VAULT_TESTING
        siteOK = { $0.hasPrefix("https://") || isLoopbackOrigin($0) }      // test builds: a stand-in store on this Mac
        #endif
        guard let origin = (data["business_url"] as? String).flatMap(canonicalOriginOfURL), siteOK(origin) else {
            return payFinish("shop_card", fields, failed("Shop's answer names no store site (business_url): nothing imported"), who, approval: "-")
        }
        guard let limN = data["approval_limit"] as? NSNumber, CFGetTypeID(limN) != CFBooleanGetTypeID(), limN.doubleValue == limN.doubleValue.rounded(),
              limN.intValue > 0, limN.intValue <= 1_000_000_000, let cur = (data["currency"] as? String)?.uppercased(), knownCurrencies.contains(cur) else {
            return payFinish("shop_card", fields, failed("Shop's answer has no approved limit and supported currency: nothing imported"), who, approval: "-")
        }
        guard let bytes = cardBytes, let doc = parseJSONObject(bytes), doc["spend_request_id"] as? String == sr,
              let c = doc["card"] as? [String: Any], let cred = c["credential"] as? [String: Any], cred["type"] as? String == "pan",
              let number = (cred["number"] as? String).flatMap(cardNumber),
              let em = cred["expiry_month"].map({ "\($0)" }), let ey = cred["expiry_year"].map({ "\($0)" }), let (m, y) = cardExpiry(em + "/" + ey),
              let cvv = cred["cvv"] as? String, (3...4).contains(cvv.count), cvv.allSatisfy({ $0.isASCII && $0.isNumber }) else {
            return payFinish("shop_card", fields, failed("Shop's card file isn't for this spend request, or isn't in the form this reads (a card with a number, expiry and security code): nothing imported"), who, approval: "-")
        }
        let holder = (cred["name"] as? String).flatMap { $0.count <= 100 && clean($0, max: 100) == $0 ? $0 : nil } ?? ""
        let zip = ((c["billing_address"] as? [String: Any])?["postal_code"] as? String).flatMap { $0.count <= 12 && matches($0, "^[A-Za-z0-9 -]*$") ? $0 : nil }
        let card = CardValue(number: number, exp_month: m, exp_year: y, cvc: cvv, name: holder, zip: zip)
        let caps = CardCaps(currency: cur, per_purchase: limN.intValue)
        let business = (data["business_name"] as? String).flatMap { $0.count <= 40 && clean($0, max: 40) == $0 ? $0 : nil }
        let label = "Shop single-use card" + (business.map { " for \($0)" } ?? "")
        do {
            try updateVault(paths, key) { p in
                guard p.secrets[name] == nil else { throw VaultError("a secret named \(name) exists already") }
                var s = Secret(kind: "card", value: card.encoded(), username: nil, origins: [origin], approval: "each",
                               created: isoTime(), rotated: nil, last_used: nil, uses: 0, projects: nil, api_host: nil, api_header: nil,
                               api_path_prefix: nil, gen: randomBytes(8).map { String(format: "%02x", $0) }.joined(), id: newSecretID())
                s.card_label = label
                s.caps = caps
                s.single_use = true
                s.merchant_locked = true
                s.source = "shop-spend-request"
                p.secrets[name] = s
            }
        } catch let e as VaultError { return payFinish("shop_card", fields, failed(e.message), who, approval: "-") } catch {
            return payFinish("shop_card", fields, failed("cannot write the vault"), who, approval: "-")
        }
        fields["label"] = label
        fields["single_use"] = true
        fields["origin"] = origin
        fields["caps"] = caps.words
        return payFinish("shop_card", fields, ["ok": true, "status": "ok"], who, approval: "-")
    }

    // MARK: shop-complete: a Shop checkout, bound to the checkout that was approved

    /// What an approval of a Shop checkout is bound to: the whole checkout as Shop's CLI wrote it (its id, version,
    /// status, currency, totals, line items and every other field; numbers by their exact text, never converted), except
    /// fields that only describe the read itself or the buyer's link. A different answer, or one that can't be read that
    /// way, never matches.
    func checkoutFingerprint(raw: [UInt8]) -> String {
        guard let v = parseLexJSON(raw), case .obj(let top) = v, let data = top.first(where: { $0.0 == "data" })?.1, case .obj(let fields) = data else {
            return "unreadable:" + randomBytes(16).map { String(format: "%02x", $0) }.joined()
        }
        let drop: Set<String> = ["continue_url", "expires_at", "updated_at", "fetched_at", "request_id", "trace_id", "messages"]
        return sha256Hex(Array(lexCanonical(.obj(fields.filter { !drop.contains($0.0) })).utf8))
    }

    /// A Shop checkout's terms, checked the same way at every read: ready to complete, exactly one total (type total) in
    /// whole minor units, a supported currency. (money, why not)
    func checkoutTerms(_ co: [String: Any]) -> (Money?, String?) {
        guard co["status"] as? String == "ready_for_complete" else { return (nil, "the checkout isn't ready to complete") }
        let totals = (co["totals"] as? [Any] ?? []).compactMap { $0 as? [String: Any] }.filter { $0["type"] as? String == "total" }
        guard totals.count == 1, let an = totals[0]["amount"] as? NSNumber, CFGetTypeID(an) != CFBooleanGetTypeID(), !CFNumberIsFloatType(an),
              an.int64Value > 0, an.int64Value <= 1_000_000_000, let cur = (co["currency"] as? String)?.uppercased(), knownCurrencies.contains(cur) else {
            return (nil, "the checkout has no single total in whole minor units of a supported currency")
        }
        return (Money(minor: Int(an.int64Value), currency: cur), nil)
    }

    func opShopComplete(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        var fields = echo(req, ["vm"])
        fields["route"] = "shop"
        guard let vm = req["vm"] as? String, validName(vm), let checkout = req["checkout"] as? String, matches(checkout, "^[A-Za-z0-9:/._?=&-]{1,300}$"),
              let domain = req["shop"] as? String, matches(domain, "^[a-z0-9]([a-z0-9-]{0,62}\\.)+[a-z]{2,24}$"),
              let instText = req["instrument_text"] as? String, instText.utf8.count <= 64 << 10,
              let instLex = parseLexJSON(Array(instText.utf8)), case .obj = instLex, (req["name"] as? String).map(validName) ?? true else {
            return payFinish("pay", fields, failed("bad request"), who, approval: "-")
        }
        let origin = "https://" + domain
        fields["origin"] = origin
        fields["checkout"] = "checkout:" + String(sha256Hex(Array(checkout.utf8)).prefix(16))
        reconcilePendingSpend()
        let p: Payload
        do { p = try load() } catch { return payFinish("pay", fields, failed("cannot read the vault"), who, approval: "-") }
        // The shop entry named, else the first one, else the defaults: every purchase asks, no caps.
        var name = req["name"] as? String ?? ""
        var s: Secret
        if !name.isEmpty {
            guard let x = p.secrets[name], x.kind == "shop" else { return payFinish("pay", fields, denied("\(name) isn't a shop entry (secrets set NAME --kind shop)"), who, approval: "-") }
            s = x
        } else if let first = p.secrets.keys.sorted().first(where: { p.secrets[$0]?.kind == "shop" }) {
            name = first
            s = p.secrets[first]!
        } else {
            name = "shop"
            s = Secret(kind: "shop", value: "", username: nil, origins: [], approval: "each", created: isoTime(), rotated: nil, last_used: nil, uses: 0,
                       projects: nil, api_host: nil, api_header: nil, api_path_prefix: nil, id: "shop-default")
            s.card_label = "your Shop account"
        }
        fields["name"] = name
        fields["label"] = s.card_label ?? name
        if let why = projectProblem(name, s, who) { return payFinish("pay", fields, denied(why), who, approval: "-") }
        if let why = pendingSpendProblem(s, name: name, p) { return payFinish("pay", fields, denied(why), who, approval: "-") }
        // 1. The checkout as Shop has it: ready, exactly one total, the instrument one it offers.
        func readCheckout() -> ([String: Any]?, String?, [UInt8]) {
            let (st, ans, why) = runShop(["checkout", "get", checkout, "--shop", domain], timeout: 120)
            if let why { return (nil, why, []) }
            guard st == 0, let co = ans?["data"] as? [String: Any], co["id"] as? String == checkout else { return (nil, "Shop didn't return this checkout", []) }
            return (co, nil, lastShopRaw)
        }
        let (coOpt, cwhy, raw1) = readCheckout()
        guard let co = coOpt else { return payFinish("pay", fields, denied(cwhy ?? "no checkout"), who, approval: "-") }
        guard co["status"] as? String == "ready_for_complete" else {
            fields["handoff"] = "metacomputer handoff --why payment --say \"Please finish the checkout at the link\""
            if let u = co["continue_url"] as? String, u.hasPrefix("https://"), u.count < 2048 { fields["continue_url"] = u }
            return payFinish("pay", fields, denied("the checkout isn't ready to complete: give the user its link"), who, approval: "-")
        }
        let (moneyOpt, twhy) = checkoutTerms(co)
        guard let money = moneyOpt, let cur = money.currency else {
            return payFinish("pay", fields, denied("\(twhy ?? "unclear"): give the user its link"), who, approval: "-")
        }
        // The instrument is one the checkout offers, compared as written (numbers by their text, like the checkout itself).
        let wanted = lexCanonical(instLex)
        func offers(_ raw: [UInt8]) -> Bool {
            guard let v = parseLexJSON(raw), case .obj(let top) = v, case .obj(let data)? = top.first(where: { $0.0 == "data" })?.1,
                  case .obj(let pay)? = data.first(where: { $0.0 == "payment" })?.1,
                  case .arr(let list)? = pay.first(where: { $0.0 == "instruments" })?.1 else { return false }
            return list.contains { lexCanonical($0) == wanted }
        }
        guard offers(raw1) else {
            return payFinish("pay", fields, denied("that instrument isn't one this checkout offers (pass one of its payment.instruments exactly)"), who, approval: "-")
        }
        let fp = checkoutFingerprint(raw: raw1)
        let total = formatMoney(money.minor, cur)
        fields["total"] = total
        say("\(clockTime())  a Shop checkout at \(origin) for \(total), asked by \(who.describe)")
        // 2. metacomputer's approval (the shop entry's policy and caps).
        let auth = authorizePurchase(s, p, name: name, origin: origin, money: money, vm: vm, who: who, route: "shop",
                                     lines: ["  The total is \(total), as Shop's checkout gives it; the checkout is completed only if it's still exactly this one.",
                                             "  Shop Pay pays from your own Shop account; Shop's approval and spending limits apply too."],
                                     proposedID: req["request_id"] as? String)
        if let c = s.caps, !c.isEmpty { fields["caps"] = c.words }
        guard auth.ok else { return payFinish("pay", fields, denied(auth.why), who, approval: auth.how, requestID: auth.requestID) }
        fields["approval"] = auth.how
        // 3. The same checkout, read again right before it's completed: anything different is refused.
        // Checked on its own as well as compared: the same terms, read the same way, and the instrument still offered.
        let (co2, why2, raw2) = readCheckout()
        let m2 = co2.flatMap { checkoutTerms($0).0 }
        guard let again = co2, checkoutFingerprint(raw: raw2) == fp, m2?.minor == money.minor, m2?.currency == cur, offers(raw2) else {
            return payFinish("pay", fields, denied("the checkout changed (or couldn't be read again) after it was approved: nothing was completed. \(why2 ?? "")"), who, approval: auth.how, requestID: auth.requestID)
        }
        let pu = Purchase(name: name, s: s, vm: vm, origin: origin, frameOrigins: [], human: false, profile: nil, browserPID: nil, windowID: nil,
                          money: money, approval: auth.how, requestID: auth.requestID, who: who, route: "shop", expires: Date())
        if s.id != "shop-default", let why = reserveSpend(pu, tag: fields["checkout"] as? String ?? "shop") {
            return payFinish("pay", fields, denied(why), who, approval: auth.how, requestID: auth.requestID)
        }
        // 4. Completed once, the instrument on the CLI's standard input.
        let (st3, ans3, why3) = runShop(["checkout", "complete", checkout, "--shop", domain, "--instrument", "-"], stdin: Array(instText.utf8), timeout: 180)
        let d3 = ans3?["data"] as? [String: Any] ?? [:]
        let order = d3["order"] as? [String: Any]
        var outcome = why3 == nil && st3 == 0 && d3["status"] as? String == "completed" && order != nil ? "completed"
            : (why3 == nil && shopNothingSent(ans3) ? "failed" : "not_confirmed")
        if let l = order?["label"] as? String, l.count <= 40, clean(l, max: 40) == l { fields["order"] = l }
        // What Shop says it charged: one total, its currency. The completion itself can't carry the approved amount (Shop's
        // own approval and spending limits are what hold it), so this is checked afterwards: anything else is reported as
        // unverified, and spending over the approved amount goes in the spending record too.
        var warning: String?
        var extras: [SpendEntry] = []          // spending beyond the reservation, recorded as the completion is settled
        let tagBase = pu.reservation ?? (fields["checkout"] as? String ?? "shop")   // extras name the reservation they settle
        func extra(_ amount: Int, _ currency: String, _ suffix: String, unaccounted: Bool) {
            var e = SpendEntry(card: pu.cardID, origin: pu.origin, amount: amount, currency: currency, t: Date().timeIntervalSince1970, purchase: tagBase + suffix)
            if unaccounted { e.unaccounted = true }
            extras.append(e)
        }
        if outcome == "completed" {
            let t3 = (d3["totals"] as? [Any] ?? []).compactMap { $0 as? [String: Any] }.filter { $0["type"] as? String == "total" }
            let a3 = t3.count == 1 ? (t3[0]["amount"] as? NSNumber).flatMap { CFGetTypeID($0) != CFBooleanGetTypeID() && !CFNumberIsFloatType($0) && $0.int64Value > 0 && $0.int64Value <= 1_000_000_000 ? Int($0.int64Value) : nil } : nil
            // Every currency the answer names (its own, the total's) must be the same one: two never verify, whichever
            // comes first.
            let named = [d3["currency"], t3.first?["currency"]].compactMap { $0 }
            let declared = named.compactMap { ($0 as? String)?.uppercased() }
            let c3: String? = declared.count == named.count && !declared.isEmpty && Set(declared).count == 1 ? declared[0] : nil
            fields["verified"] = false
            if let a = a3, let c = c3, iso4217Codes.contains(c) {
                fields["charged"] = formatMoney(a, c)
                if c != cur {
                    outcome = "completed_in_another_currency"
                    warning = "Shop reports a charge of \(formatMoney(a, c)), not in the approved \(cur)"
                    extra(a, c, ":charged", unaccounted: true)
                } else if a > money.minor {
                    outcome = "completed_with_a_higher_total"
                    warning = "Shop reports a charge of \(formatMoney(a, c)), over the \(total) approved"
                    extra(a - money.minor, c, ":extra", unaccounted: false)
                } else {
                    fields["verified"] = true
                }
            } else if Set(declared).count > 1 || declared.count != named.count {
                outcome = "completed_unverified"
                warning = "Shop's answer names conflicting currencies for what it charged (\(Set(declared).sorted().joined(separator: ", ")))"
            } else {
                outcome = "completed_unverified"
                warning = "Shop's answer doesn't say plainly what it charged (one total and its currency)"
            }
        }
        // Settled now, whatever the outcome: the reservation stays counted, anything beyond it is added, and the entry's
        // marker goes. If the vault can't be written, the entry stays blocked (durably) until it is.
        // A definite outcome only: Shop completed it and said plainly what it charged, or said plainly it failed. A timeout,
        // an answer that can't be read or one that doesn't say what was charged settles nothing: the entry stays blocked
        // until a definite outcome is recorded or the user settles it (spend-resolve, after checking the order in Shop).
        let definite = ["completed", "completed_with_a_higher_total", "completed_in_another_currency", "failed"].contains(outcome)
        if s.id != "shop-default" {
            if !definite {
                fields["settled"] = false
                warning = (warning.map { $0 + "; " } ?? "") + "what Shop charged isn't known, so \(s.card_label ?? name) buys nothing until you settle it (`metacomputer secrets spend-resolve \(name)`, your own terminal, after checking the order in Shop)"
            } else if let nr = settleCompletion(pu, extras: extras) {
                fields["spending_recorded"] = false
                warning = (warning.map { $0 + "; " } ?? "") + "the outcome " + nr
            }
        }
        fields["outcome"] = outcome
        if let w = warning {
            fields["warning"] = w + ": check the order in Shop and tell the user"
            say("          WARNING: \(w)")
        }
        let res: [String: Any] = outcome.hasPrefix("completed") ? ["ok": true, "status": "ok"] : ["ok": false, "status": "error", "reason": "Shop's checkout outcome: \(outcome); never complete it again: read it with `shop checkout get` and tell the user"]
        return payFinish("pay", fields, res, who, approval: auth.how, requestID: auth.requestID)
    }

    /// Shop's CLI said plainly that nothing was done: its own refusal before sending (a usage or input error) or Shop's
    /// refusal for want of a valid sign-in, as `{"ok": false, "error": {"code": ...}}` with no data. Only that settles a
    /// completion as failed.
    /// Any other failure (a transport error, an upstream or merchant error, a checkout that isn't completed, a bare
    /// `{"ok": false}`, a code it doesn't know) leaves what was charged unknown: the entry stays blocked (spend-resolve).
    func shopNothingSent(_ ans: [String: Any]?) -> Bool {
        guard let a = ans, let ok = a["ok"] as? NSNumber, CFGetTypeID(ok) == CFBooleanGetTypeID(), !ok.boolValue, a["data"] == nil,
              let e = a["error"] as? [String: Any], let code = e["code"] as? String else { return false }
        return shopNothingSentCodes.contains(code)
    }

    // MARK: AP2

    func opAP2Key() -> [String: Any] {
        guard let p = try? load() else { return failed("cannot read the vault") }
        guard let k = ap2SigningKey(p) else { return failed("no AP2 key yet: the user makes one with `metacomputer secrets ap2 init`, in their own terminal") }
        var out = ap2PublicJWK(k.publicKey)
        out["ok"] = true
        out["status"] = "ok"
        return out
    }

    func opAP2Sign(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        var fields = echo(req, ["name", "vm", "origin"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, let origin = req["origin"] as? String, validName(name), validName(vm),
              canonicalOrigin(origin) == origin, let jwt = req["checkout_jwt"] as? String, let ttl = (req["ttl_s"] as? NSNumber)?.intValue, ttl >= 60, ttl <= 3600 else {
            return payFinish("ap2_sign", fields, failed("bad request"), who, approval: "-")
        }
        say("\(clockTime())  AP2 mandates for a checkout at \(origin) with \(name), asked by \(who.describe)")
        reconcilePendingSpend()
        let p: Payload
        do { p = try load() } catch { return payFinish("ap2_sign", fields, failed("cannot read the vault"), who, approval: "-") }
        guard let s = p.secrets[name], s.kind == "card" else { return payFinish("ap2_sign", fields, failed("no card named \(name)"), who, approval: "-") }
        fields["label"] = s.card_label ?? name
        guard let key = ap2SigningKey(p) else {
            return payFinish("ap2_sign", fields, failed("no AP2 key yet: the user makes one with `metacomputer secrets ap2 init`, in their own terminal"), who, approval: "-")
        }
        let (claimOpt, why) = parseCheckoutJWT(jwt, audience: ap2PublicJWK(key.publicKey)["kid"] as? String)
        guard let claim = claimOpt else { return payFinish("ap2_sign", fields, denied(why ?? "the checkout JWT can't be read"), who, approval: "-") }
        guard claim.website == origin else {
            return payFinish("ap2_sign", fields, denied("the checkout names the merchant site \(claim.website), not \(origin): refused"), who, approval: "-")
        }
        // The merchant's signature on the checkout can't be verified here (no merchant key is known), so only a person
        // approves one: an agent card, which nobody would see, refuses. The mandate says it wasn't verified (risk_data).
        if s.pay == "agent" && !confirmEach {
            return payFinish("ap2_sign", fields, denied("\(name) is an agent card: it signs AP2 mandates only for a checkout whose merchant signature is verified, which this vault can't do yet. Use a card that asks"), who, approval: "-")
        }
        if !s.origins.isEmpty && !s.origins.contains(origin) {
            return payFinish("ap2_sign", fields, denied("\(name) pays only at \(s.origins.joined(separator: ", ")), not \(origin)"), who, approval: "-")
        }
        if s.single_use == true { return payFinish("ap2_sign", fields, denied("\(name) is a single-use card for a card form, not for AP2"), who, approval: "-") }
        if let why = projectProblem(name, s, who) { return payFinish("ap2_sign", fields, denied(why), who, approval: "-") }
        if let why = requiredSinkProblem("ap2_sign", fields, who) { return payFinish("ap2_sign", fields, denied(why), who, approval: "-") }
        // One signature per checkout, by what it is (the merchant, its site, the checkout's id), not by how it's written.
        let tag = "ap2:" + String(sha256Hex(Array((claim.merchantID + "\u{0}" + claim.website + "\u{0}" + claim.checkoutID).utf8)).prefix(16))
        if (p.spend ?? []).contains(where: { $0.purchase == tag }) {
            return payFinish("ap2_sign", fields, denied("mandates for this checkout were signed already: one checkout, one signature"), who, approval: "-")
        }
        let money = Money(minor: claim.minor, currency: claim.currency)
        let total = formatMoney(claim.minor, claim.currency)
        fields["total"] = total
        let auth = authorizePurchase(s, p, name: name, origin: origin, money: money, vm: vm, who: who, route: "ap2",
                                     lines: ["  Sign AP2 checkout and payment mandates for \(total) to \(claim.merchantName) (checkout \(clean(claim.checkoutID, max: 60))): the merchant's own claim; its signature isn't verified here, and the mandate says so."],
                                     notes: ["AP2 mandates; the merchant's signature on the checkout isn't verified"], proposedID: req["request_id"] as? String)
        guard auth.ok else { return payFinish("ap2_sign", fields, denied(auth.why), who, approval: auth.how, requestID: auth.requestID) }
        guard let p2 = try? load(), let s2 = p2.secrets[name], secretGen(s2) == secretGen(s) else {
            return payFinish("ap2_sign", fields, denied("\(name) or its policy changed while it asked"), who, approval: auth.how, requestID: auth.requestID)
        }
        // Still valid now (exp, nbf), after however long the approval took.
        if parseCheckoutJWT(jwt, audience: ap2PublicJWK(key.publicKey)["kid"] as? String, now: Date()).0 == nil {
            return payFinish("ap2_sign", fields, denied("the checkout is no longer valid (it expired while it asked): nothing was signed"), who, approval: auth.how, requestID: auth.requestID)
        }
        // Counted against the caps before it's signed: a signed mandate authorizes the payment.
        let pu = Purchase(name: name, s: s, vm: vm, origin: origin, frameOrigins: [], human: false, profile: nil, browserPID: nil, windowID: nil,
                          money: money, approval: auth.how, requestID: auth.requestID, who: who, route: "ap2", expires: Date())
        if let why = reserveSpend(pu, tag: tag) { return payFinish("ap2_sign", fields, denied(why), who, approval: auth.how, requestID: auth.requestID) }
        guard let (cm, pm, h2, kid) = ap2ClosedMandates(checkoutJWT: jwt, claim: claim, payeeID: claim.merchantID, payeeName: claim.merchantName,
                                                         website: origin, instrumentID: "card-" + (s.id ?? name), instrumentLabel: s.card_label ?? name,
                                                         key: key, ttl: ttl, approvedBy: "user") else {
            return payFinish("ap2_sign", fields, failed("signing failed"), who, approval: auth.how, requestID: auth.requestID)
        }
        fields["checkout_hash"] = h2
        fields["kid"] = kid
        fields["approval"] = auth.how
        fields["checkout_signature_verified"] = false
        fields["expires"] = isoTime(Date().addingTimeInterval(TimeInterval(ttl)))
        var out = payFinish("ap2_sign", fields, ["ok": true, "status": "ok"], who, approval: auth.how, requestID: auth.requestID)
        if out["ok"] as? Bool == true {
            out["checkout_mandate"] = cm
            out["payment_mandate"] = pm
        }
        return out
    }

    // MARK: Web Bot Auth (while unlocked)

    func opWBA(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard let p = try? load() else { return failed("cannot read the vault") }
        guard let k = p.wba, let raw = Data(base64Encoded: k.key), let sk = try? Curve25519.Signing.PrivateKey(rawRepresentation: raw) else {
            return (req["what"] as? String) == "status" ? ["ok": true, "status": "ok", "state": "none"]
                : failed("no Web Bot Auth key yet: the user makes one with `metacomputer secrets wba init --agent https://their.site`, in their own terminal")
        }
        let kid = ed25519Thumbprint(Array(sk.publicKey.rawRepresentation))
        switch req["what"] as? String {
        case "status":
            return ["ok": true, "status": "ok", "state": k.on ? "on" : "off", "keyid": kid, "agent": k.agent]
        case "directory":
            var out: [String: Any] = ["ok": true, "status": "ok", "keyid": kid, "agent": k.agent,
                                      "url": k.agent + "/.well-known/http-message-signatures-directory", "directory": wbaDirectory(k) ?? [:]]
            if req["headers"] as? Bool == true, let days = (req["days"] as? NSNumber)?.intValue, (1...90).contains(days),
               let u = URL(string: k.agent), let h = u.host {
                var hdr = wbaSignatureHeaders(k, authority: h + (u.port.map { ":\($0)" } ?? ""), ttl: days * 86400, tag: "http-message-signatures-directory") ?? [:]
                hdr["Content-Type"] = "application/http-message-signatures-directory+json"
                out["headers"] = hdr
                out["valid_days"] = days
            }
            return out
        case "sign":
            guard k.on else { return denied("Web Bot Auth is off: the user turns it on with `metacomputer secrets wba on`, in their own terminal") }
            guard let o = req["origin"] as? String, canonicalOrigin(o) == o, let h = wbaSignatureHeaders(k, authority: originAuthority(o)) else { return failed("bad request") }
            record(["op": "wba_sign", "result": "ok", "origin": o, "keyid": kid, "requester": who.record])
            return ["ok": true, "status": "ok", "origin": o, "keyid": kid, "headers": h, "valid_s": 60]
        default:
            return failed("bad request")
        }
    }
}

/// A file Shop's CLI wrote with a card in it: overwritten with zeros, then removed.
func scrubFile(_ path: String) {
    var st = stat()
    guard lstat(path, &st) == 0 else { return }
    if (st.st_mode & S_IFMT) == S_IFREG {
        let fd = open(path, O_WRONLY | O_NOFOLLOW | O_CLOEXEC)
        if fd >= 0 {
            let zeros = [UInt8](repeating: 0, count: Int(min(st.st_size, 1 << 20)))
            _ = writeAll(fd, zeros)
            _ = fsync(fd)
            close(fd)
        }
    }
    unlink(path)
}

// MARK: - Shop's network: a CONNECT proxy to public hosts on 443 only

/// Whether IPv4 bytes (network order) are a public unicast address: never "this network", private, shared (CGNAT),
/// loopback, link-local, IETF protocol, documentation, benchmarking, multicast or reserved ranges.
func publicV4(_ b: [UInt8]) -> Bool {
    guard b.count == 4 else { return false }
    switch (b[0], b[1], b[2]) {
    case (0, _, _), (10, _, _), (127, _, _): return false
    case (100, let x, _) where x & 0xC0 == 64: return false
    case (169, 254, _): return false
    case (172, let x, _) where x & 0xF0 == 16: return false
    case (192, 168, _), (192, 0, 0), (192, 0, 2), (198, 51, 100), (203, 0, 113): return false
    case (198, let x, _) where x & 0xFE == 18: return false
    case (let a, _, _) where a >= 224: return false
    default: return true
    }
}

/// Whether IPv6 bytes are a public unicast address: never unspecified, loopback, link-local, unique-local, multicast,
/// documentation, discard, or an IPv4 address in IPv6 form (mapped, NAT64) that isn't public itself.
func publicV6(_ b: [UInt8]) -> Bool {
    guard b.count == 16 else { return false }
    if b.allSatisfy({ $0 == 0 }) || (b[0..<15].allSatisfy({ $0 == 0 }) && b[15] == 1) { return false }
    if b[0] == 0xFE && b[1] & 0xC0 == 0x80 { return false }           // fe80::/10
    if b[0] & 0xFE == 0xFC { return false }                            // fc00::/7
    if b[0] == 0xFF { return false }                                   // ff00::/8
    if b[0] == 0x20 && b[1] == 0x01 && b[2] == 0x0D && b[3] == 0xB8 { return false }     // 2001:db8::/32
    if b[0] == 0x01 && b[1..<8].allSatisfy({ $0 == 0 }) { return false }                  // 100::/64
    if b[0..<10].allSatisfy({ $0 == 0 }) && b[10] == 0xFF && b[11] == 0xFF { return publicV4(Array(b[12..<16])) }   // ::ffff:a.b.c.d
    if b[0] == 0x00 && b[1] == 0x64 && b[2] == 0xFF && b[3] == 0x9B { return publicV4(Array(b[12..<16])) }        // 64:ff9b::/96
    if b[0..<12].allSatisfy({ $0 == 0 }) { return false }              // ::a.b.c.d (deprecated compatible form)
    return true
}

/// An address in one form for every check: an IPv4 address written as IPv6 (mapped ::ffff:a.b.c.d, NAT64 64:ff9b::a.b.c.d,
/// or the old compatible ::a.b.c.d) becomes its four IPv4 bytes; anything else stays as it is.
func canonicalAddress(_ b: [UInt8]) -> [UInt8] {
    guard b.count == 16 else { return b }
    if b[0..<10].allSatisfy({ $0 == 0 }) && b[10] == 0xFF && b[11] == 0xFF { return Array(b[12..<16]) }
    if b[0] == 0x00 && b[1] == 0x64 && b[2] == 0xFF && b[3] == 0x9B && b[4..<12].allSatisfy({ $0 == 0 }) { return Array(b[12..<16]) }
    if b[0..<12].allSatisfy({ $0 == 0 }) && !(b[12..<15].allSatisfy({ $0 == 0 }) && b[15] <= 1) { return Array(b[12..<16]) }
    return b
}

/// Whether the proxy may connect to an address: public (publicV4/publicV6, on its canonical form) and not one of this
/// Mac's own (compared in canonical form too, so ::ffff:<an own IPv4> is this Mac).
func proxyDestinationAllowed(_ bytes: [UInt8], own: Set<[UInt8]>) -> Bool {
    let c = canonicalAddress(bytes)
    guard c.count == 4 ? publicV4(c) : publicV6(c) else { return false }
    return !own.contains(c)
}

/// This Mac's own addresses (every interface's), as bytes.
func ownAddresses() -> Set<[UInt8]> {
    var out = Set<[UInt8]>()
    var ifa: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&ifa) == 0, let first = ifa else { return out }
    defer { freeifaddrs(ifa) }
    var p: UnsafeMutablePointer<ifaddrs>? = first
    while let cur = p {
        if let sa = cur.pointee.ifa_addr { if let b = addressBytes(sa) { out.insert(canonicalAddress(b)) } }
        p = cur.pointee.ifa_next
    }
    return out
}

/// An address's bytes (4 for IPv4, 16 for IPv6), or nil for another family.
func addressBytes(_ sa: UnsafePointer<sockaddr>) -> [UInt8]? {
    switch Int32(sa.pointee.sa_family) {
    case AF_INET:
        return sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { s in withUnsafeBytes(of: s.pointee.sin_addr) { Array($0) } }
    case AF_INET6:
        return sa.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) { s in withUnsafeBytes(of: s.pointee.sin6_addr) { Array($0) } }
    default: return nil
    }
}

/// One of the proxy's sockets, held as an object: stop() shuts it and its worker closes it, each under its lock, so a
/// shutdown never reaches a descriptor number the socket no longer owns (one closed and handed to something else).
final class ProxySocket {
    let fd: Int32
    private let lock = NSLock()
    private var closed = false
    init(_ fd: Int32) { self.fd = fd }
    /// Wakes whatever waits on it (poll returns): only while it's still open.
    func shut() {
        lock.lock()
        defer { lock.unlock() }
        if !closed { shutdown(fd, SHUT_RDWR) }
    }
    /// Closed once, whoever asks first.
    func close() {
        lock.lock()
        defer { lock.unlock() }
        if !closed {
            closed = true
            _ = Darwin.close(fd)
        }
    }
    /// `body` with the descriptor while it's open (under the lock, so it can't be closed meanwhile); nil once closed.
    func withOpen<T>(_ body: (Int32) -> T) -> T? {
        lock.lock()
        defer { lock.unlock() }
        return closed ? nil : body(fd)
    }
}

/// The only network the sandbox lets the pinned Shop CLI reach: a CONNECT proxy on 127.0.0.1 (a port of its own, for one
/// run), given to the CLI as HTTPS_PROXY with a secret of that run's in its userinfo, which Node sends as
/// Proxy-Authorization (Shop's CLI uses Node's proxy support for every request); a request without it gets 407, so
/// nothing else on this Mac can use it. It tunnels only to port 443 of public addresses: it resolves a name itself,
/// refuses loopback, private, link-local, shared and other special ranges and this Mac's own addresses, whatever form
/// they're written in (canonicalAddress), and connects to the address it checked. Nothing but CONNECT is served. At most
/// 8 connections at once and 256 MiB each way per connection; every read and write has a deadline (10 s for the request,
/// 60 s without progress after); stop() shuts every socket it has, so no worker outlives the run. `testAllow`: test
/// builds only, host:port pairs it also allows (a stand-in merchant); `testOwn`: test builds, addresses treated as own.
final class ShopProxy {
    let listener: ProxySocket
    let port: UInt16
    let token = randomBytes(24).map { String(format: "%02x", $0) }.joined()
    let testAllow: Set<String>
    private let lock = NSLock()
    private var stopped = false
    private var live = [ObjectIdentifier: ProxySocket]()
    private var active = 0
    #if MCOMP_VAULT_TESTING
    var testBetween: (() -> Void)?            // selftest: runs in stop() after it has taken the sockets, before it shuts them
    var testLiveFDs: [Int32] { lock.lock(); defer { lock.unlock() }; return live.values.map { $0.fd } }
    #endif
    private let own: Set<[UInt8]>
    static let maxConnections = 8
    static let maxBytes = 256 << 20

    var url: String { "http://mc:\(token)@127.0.0.1:\(port)" }

    init?(testAllow: Set<String> = [], testOwn: Set<[UInt8]> = []) {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, socklen_t(MemoryLayout<Int32>.size))
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
        var a = sockaddr_in()
        a.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        a.sin_family = sa_family_t(AF_INET)
        a.sin_port = 0
        a.sin_addr.s_addr = inet_addr("127.0.0.1")
        let ok = withUnsafePointer(to: &a) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } } == 0
        guard ok, listen(fd, 16) == 0 else { close(fd); return nil }
        var got = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &got) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) } }
        listener = ProxySocket(fd)
        port = UInt16(bigEndian: got.sin_port)
        self.testAllow = testAllow
        self.own = ownAddresses().union(testOwn.map(canonicalAddress))
    }

    var isStopped: Bool { lock.lock(); defer { lock.unlock() }; return stopped }

    /// Registers a socket so stop() can shut it (refused once stopped; a client also past the connection cap).
    private func track(_ sock: ProxySocket, client: Bool) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if stopped || (client && active >= ShopProxy.maxConnections) { return false }
        if client { active += 1 }
        live[ObjectIdentifier(sock)] = sock
        return true
    }

    /// Forgets a socket and closes it (once: ProxySocket.close).
    private func untrack(_ sock: ProxySocket, client: Bool) {
        lock.lock()
        if client { active -= 1 }
        live[ObjectIdentifier(sock)] = nil
        lock.unlock()
        sock.close()
    }

    func start() {
        Thread.detachNewThread { [self] in
            while !isStopped {
                // The listener is used only while it's open, under its lock: stop() closes it in between, never during.
                guard let c = listener.withOpen({ fd -> Int32 in
                    var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
                    guard poll(&pfd, 1, 200) > 0 else { return -1 }
                    return accept(fd, nil, nil)
                }) else { break }
                guard c >= 0 else { continue }
                prepare(c)
                let sock = ProxySocket(c)
                guard track(sock, client: true) else {
                    _ = put(c, Array("HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n".utf8)[...], wait: 500)
                    sock.close()
                    continue
                }
                Thread.detachNewThread { [self] in handle(sock) }
            }
        }
    }

    /// Ends the run's network: the listener and every connection (each worker's poll returns at once and it ends). Each
    /// socket is shut through its object, so one a worker has already closed is left alone, whatever now has its number.
    func stop() {
        lock.lock()
        stopped = true
        let all = Array(live.values)
        lock.unlock()
        #if MCOMP_VAULT_TESTING
        testBetween?()
        #endif
        for sock in all { sock.shut() }
        listener.close()
    }

    private func prepare(_ fd: Int32) {
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
        _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
    }

    /// All of `bytes` to a non-blocking socket, each wait for room at most `wait` seconds; false on error, stop or timeout.
    private func put(_ fd: Int32, _ bytes: ArraySlice<UInt8>, wait: Int32 = 60_000) -> Bool {
        var off = bytes.startIndex
        while off < bytes.endIndex {
            if isStopped { return false }
            let n = bytes[off...].withUnsafeBytes { write(fd, $0.baseAddress!, $0.count) }
            if n > 0 { off += n; continue }
            guard n < 0, errno == EAGAIN || errno == EINTR else { return false }
            var pfd = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
            guard poll(&pfd, 1, wait) > 0, pfd.revents & Int16(POLLOUT) != 0 else { return false }
        }
        return true
    }

    private func handle(_ sock: ProxySocket) {
        defer { untrack(sock, client: true) }
        let c = sock.fd                               // this worker's own: only it closes it (untrack, at the end)
        // The request: one CONNECT line and its headers, within 8 KiB and 10 s.
        var head = [UInt8]()
        var buf = [UInt8](repeating: 0, count: 4096)
        let deadline = Date().addingTimeInterval(10)
        var end: Int? = nil
        while end == nil, head.count < 8192, Date() < deadline, !isStopped {
            var pfd = pollfd(fd: c, events: Int16(POLLIN), revents: 0)
            guard poll(&pfd, 1, 500) >= 0 else { return }
            if pfd.revents == 0 { continue }
            let n = read(c, &buf, buf.count)
            if n < 0 && (errno == EAGAIN || errno == EINTR) { continue }
            guard n > 0 else { return }
            head += buf[0..<n]
            if let r = head.firstRange(of: [0x0D, 0x0A, 0x0D, 0x0A]) { end = r.upperBound }
        }
        func refuse(_ code: String, _ extra: String = "") { _ = put(c, Array("HTTP/1.1 \(code)\r\n\(extra)Content-Length: 0\r\nConnection: close\r\n\r\n".utf8)[...], wait: 2_000) }
        guard let e = end else { refuse("400 Bad Request"); return }
        let lines = String(decoding: head[0..<e], as: UTF8.self).components(separatedBy: "\r\n")
        // This run's secret, as Node sends the proxy URL's userinfo.
        let want = "Basic " + Data("mc:\(token)".utf8).base64EncodedString()
        let given = lines.dropFirst().first { $0.lowercased().hasPrefix("proxy-authorization:") }
            .map { String($0.dropFirst("proxy-authorization:".count)).trimmingCharacters(in: .whitespaces) } ?? ""
        guard constantTimeEqual(given, want) else { refuse("407 Proxy Authentication Required", "Proxy-Authenticate: Basic realm=\"metacomputer\"\r\n"); return }
        let parts = (lines.first ?? "").split(separator: " ")
        guard parts.count == 3, parts[0] == "CONNECT", parts[2].hasPrefix("HTTP/1.") else { refuse("405 Method Not Allowed"); return }
        let target = String(parts[1])
        guard let colon = target.lastIndex(of: ":"), let port = Int(target[target.index(after: colon)...]), (1...65535).contains(port) else {
            refuse("400 Bad Request"); return
        }
        var host = String(target[..<colon])
        if host.hasPrefix("[") && host.hasSuffix("]") { host = String(host.dropFirst().dropLast()) }
        guard !host.isEmpty, host.count <= 253, host.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || ".-:".contains($0)) }) else {
            refuse("400 Bad Request"); return
        }
        let testAllowed = testAllow.contains(target)
        guard port == 443 || testAllowed else { refuse("403 Forbidden"); return }
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        hints.ai_protocol = IPPROTO_TCP
        var res: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, String(port), &hints, &res) == 0, let first = res else { refuse("502 Bad Gateway"); return }
        defer { freeaddrinfo(res) }
        var s: Int32 = -1
        var upstream: ProxySocket?
        var checked = false
        var ai: UnsafeMutablePointer<addrinfo>? = first
        while let a = ai, s < 0, !isStopped {
            ai = a.pointee.ai_next
            guard let sa = a.pointee.ai_addr, let bytes = addressBytes(sa) else { continue }
            guard testAllowed || proxyDestinationAllowed(bytes, own: own) else { continue }
            checked = true
            let fd = socket(a.pointee.ai_family, SOCK_STREAM, 0)
            guard fd >= 0 else { continue }
            prepare(fd)
            let up = ProxySocket(fd)
            guard track(up, client: false) else { up.close(); return }
            var ok = connect(fd, sa, a.pointee.ai_addrlen) == 0
            if !ok && errno == EINPROGRESS {
                var pfd = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
                var err: Int32 = 0
                var len = socklen_t(MemoryLayout<Int32>.size)
                ok = poll(&pfd, 1, 10_000) > 0 && getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) == 0 && err == 0
            }
            if ok { upstream = up; s = fd } else { untrack(up, client: false) }
        }
        guard s >= 0, let upSock = upstream else { refuse(checked ? "502 Bad Gateway" : "403 Forbidden"); return }
        defer { untrack(upSock, client: false) }
        guard put(c, Array("HTTP/1.1 200 Connection established\r\n\r\n".utf8)[...]) else { return }
        if e < head.count { guard put(s, head[e...]) else { return } }
        // Both ways until either side is done, 60 s pass with nothing, a side has sent its 256 MiB, or the run ends.
        var fds = [pollfd(fd: c, events: Int16(POLLIN), revents: 0), pollfd(fd: s, events: Int16(POLLIN), revents: 0)]
        var big = [UInt8](repeating: 0, count: 65536)
        var moved = [0, 0]
        while !isStopped {
            fds[0].revents = 0
            fds[1].revents = 0
            guard poll(&fds, 2, 60_000) > 0 else { return }
            for (i, f) in fds.enumerated() where f.revents != 0 {
                let n = read(f.fd, &big, big.count)
                if n < 0 && (errno == EAGAIN || errno == EINTR) { continue }
                guard n > 0 else { return }
                moved[i] += n
                guard moved[i] <= ShopProxy.maxBytes, put(i == 0 ? s : c, big[0..<n]) else { return }
            }
        }
    }
}
