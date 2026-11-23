// mcomp-vault: payment cards and purchases (SECURITY.md, "Paying for things").
//
// A card is a secret of kind `card`: its parts (number, expiry, security code, name, billing postal code) are one JSON
// value, typed by the user at their own terminal (`set NAME --kind card`) or read straight from a Shop spend request's
// card file (`shop-card`). It carries a label (what approvals, lists and the audit log call it), a pay policy (`ask`:
// every purchase asks the user, the default; `agent`: purchases within the caps don't ask) and optional caps (per
// purchase, per period, per merchant) that the unlock process enforces. A secret of kind `shop` holds no value: it's the
// policy and caps for purchases through the user's own Shop account (`metacomputer shop complete`).
//
// A purchase (`pay`) is approved once and then fills the card's parts, each at most once, into the one tab on the
// merchant's site, for a few minutes. The approval names the site the browser reports, the total the page shows (the
// page's own claim) and the card's label. Nothing here ever prints, logs or records a card's parts.
import CryptoKit
import Darwin
import Foundation

// MARK: - the card's parts

/// The parts a purchase can fill, and what each one types.
let cardParts = ["number", "exp", "exp-long", "exp-month", "exp-year", "exp-year2", "cvc", "name", "zip"]
let cardPartWords: [String: String] = [
    "number": "the card number", "exp": "the expiry as MM/YY", "exp-long": "the expiry as MM/YYYY", "exp-month": "the expiry month (MM)",
    "exp-year": "the expiry year (YYYY)", "exp-year2": "the expiry year (YY)", "cvc": "the security code", "name": "the name on the card",
    "zip": "the billing ZIP or postal code"]

/// How long an approved purchase may fill its card's parts.
let purchaseSeconds = 600
/// At most this many purchases in the hub's file (purchases-active.json) at once: past it, new purchases are refused until
/// older ones' windows pass (never an entry dropped to make room).
let activePurchasesMax = 1000
/// How long the hub keeps masking a purchase's card details after a fill ends (and after the purchase ends, when it
/// filled anything): what the fill made the page show has passed through the hub by then.
let purchaseSettleSeconds = 60

/// purchaseSeconds (test builds: MCOMP_VAULT_TEST_PURCHASE_SECONDS, for a purchase that ends during a fill).
func purchaseLifetime() -> Int {
    #if MCOMP_VAULT_TESTING
    if let s = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_PURCHASE_SECONDS"], let n = Int(s), n > 0 { return n }
    #endif
    return purchaseSeconds
}
/// How many times one part may be tried in one purchase (a fill the page refused can be tried again).
let purchasePartTries = 3
/// Payment-form sites a purchase may name (--frame-origin), and the longest site address an approval shows: with these,
/// every site that gets a card part fits, in full, in Touch ID's dialog, `requests` and the unlock terminal's question.
let payFramesMax = 3
let payOriginMax = 100
/// The most lines a purchase's question may have (none is ever dropped: a longer one isn't asked).
let payQuestionLines = 10

struct CardValue: Codable {
    var number: String          // digits only
    var exp_month: String       // "01"..."12"
    var exp_year: String        // "2030"
    var cvc: String             // 3 or 4 digits
    var name: String
    var zip: String?

    /// What one part types (nil: this card has none, like a missing postal code).
    func part(_ p: String) -> String? {
        switch p {
        case "number": return number
        case "exp": return exp_month + "/" + String(exp_year.suffix(2))
        case "exp-long": return exp_month + "/" + exp_year
        case "exp-month": return exp_month
        case "exp-year": return exp_year
        case "exp-year2": return String(exp_year.suffix(2))
        case "cvc": return cvc
        case "name": return name
        case "zip": return (zip ?? "").isEmpty ? nil : zip
        default: return nil
        }
    }

    func encoded() -> String {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        return (try? enc.encode(self)).map { String(decoding: $0, as: UTF8.self) } ?? ""
    }
}

func decodeCard(_ s: Secret) -> CardValue? {
    guard s.kind == "card" else { return nil }
    return try? JSONDecoder().decode(CardValue.self, from: Data(s.value.utf8))
}

/// The Luhn check every payment card number passes.
func luhnOK(_ digits: String) -> Bool {
    guard !digits.isEmpty, digits.allSatisfy({ $0.isASCII && $0.isNumber }) else { return false }
    var sum = 0
    for (i, ch) in digits.reversed().enumerated() {
        var d = Int(String(ch))!
        if i % 2 == 1 { d *= 2; if d > 9 { d -= 9 } }
        sum += d
    }
    return sum % 10 == 0
}

/// A card number as typed (spaces and dashes allowed): its digits, or nil when it isn't one.
/// A card number starts with 2-6 (Mastercard's 2-series, Amex, Visa, Mastercard, Discover and the like) and passes the card
/// check: the same shape the hub masks (lib/cardmask.py), so a number the vault accepts is one the hub would hide.
func cardNumber(_ typed: String) -> String? {
    let digits = typed.filter { $0 != " " && $0 != "-" }
    guard digits.count >= 12, digits.count <= 19, let f = digits.first, ("2"..."6").contains(f), luhnOK(digits) else { return nil }
    return digits
}

/// "MM/YY", "MM/YYYY", "MM-YY" or "MMYY": (month "01"-"12", year "20YY"), nil otherwise or when it's in the past.
func cardExpiry(_ typed: String, now: Date = Date()) -> (String, String)? {
    let t = typed.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: " ", with: "")
    var m = "", y = ""
    if let i = t.firstIndex(where: { $0 == "/" || $0 == "-" }) {
        m = String(t[..<i]); y = String(t[t.index(after: i)...])
    } else if t.count == 4 || t.count == 6 {
        m = String(t.prefix(2)); y = String(t.dropFirst(2))
    }
    guard (1...2).contains(m.count), m.allSatisfy({ $0.isASCII && $0.isNumber }), let mi = Int(m), (1...12).contains(mi),
          y.count == 2 || y.count == 4, y.allSatisfy({ $0.isASCII && $0.isNumber }), let yi = Int(y.count == 2 ? "20" + y : y) else { return nil }
    let cal = Calendar(identifier: .gregorian)
    let c = cal.dateComponents(in: TimeZone(identifier: "UTC")!, from: now)
    guard let cy = c.year, let cm = c.month, yi > cy || (yi == cy && mi >= cm), yi <= cy + 30 else { return nil }
    return (String(format: "%02d", mi), String(yi))
}

// MARK: - amounts

/// Minor-unit digits of a currency (ISO 4217): 2 unless it's one of these.
func currencyDigits(_ c: String) -> Int {
    if ["JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF", "PYG", "RWF", "KMF", "GNF", "VUV", "XPF", "BIF", "DJF"].contains(c) { return 0 }
    if ["BHD", "KWD", "OMR", "JOD", "TND", "LYD", "IQD"].contains(c) { return 3 }
    return 2
}

/// Currencies a page's total may name by code.
let knownCurrencies: Set<String> = ["USD", "EUR", "GBP", "CAD", "AUD", "NZD", "JPY", "CHF", "SEK", "NOK", "DKK", "INR", "HKD", "SGD",
                                    "MXN", "BRL", "PLN", "CZK", "ZAR", "KRW", "CNY", "ILS", "AED", "TRY", "HUF", "RON", "THB", "TWD"]

/// ISO 4217's codes: a page that names one of these that isn't supported above is refused, never read as another
/// currency. (Three capital letters that aren't a currency, like VAT, are just words.)
let iso4217Codes: Set<String> = Set(("AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD "
    + "CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG "
    + "HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK "
    + "MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD "
    + "SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV "
    + "WST XAF XCD XOF XPF YER ZAR ZMW ZWL").split(separator: " ").map(String.init))
let dollarCurrencies: Set<String> = ["USD", "CAD", "AUD", "NZD", "HKD", "SGD", "MXN", "TWD"]

func validCurrency(_ c: String) -> Bool { matches(c, "^[A-Z]{3}$") }

/// 1234 USD -> "USD 12.34"; with no currency, "12.34 (no currency shown)".
func formatMoney(_ minor: Int, _ currency: String?) -> String {
    let d = currency.map(currencyDigits) ?? 2
    var p = 1
    for _ in 0..<d { p *= 10 }
    let whole = minor / p, frac = minor % p
    let num = d == 0 ? "\(whole)" : "\(whole)." + String(format: "%0\(d)d", frac)
    return currency.map { "\($0) \(num)" } ?? "\(num) (no currency shown)"
}

/// An amount the user types for a cap ("50", "50.5", "1250.00"), in the currency's minor units.
func parseMajorAmount(_ s: String, currency: String) -> Int? {
    let d = currencyDigits(currency)
    let parts = s.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count <= 2, let w = parts.first, !w.isEmpty, w.count <= 9, w.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
    var frac = parts.count == 2 ? String(parts[1]) : ""
    guard frac.count <= d, frac.allSatisfy({ $0.isASCII && $0.isNumber }), !(parts.count == 2 && frac.isEmpty) else { return nil }
    while frac.count < d { frac += "0" }
    guard let v = Int(String(w) + frac), v > 0 else { return nil }
    return v
}

struct Money {
    var minor: Int
    var currency: String?
}

/// A number as a page writes it ("1,234.56", "1.234,56", "12,34", "1 200"): minor units, or nil when it can't be read
/// one way only.
func pageNumber(_ raw: String, currency: String?) -> Int? {
    let s = raw.filter { !($0 == " " || $0 == "\u{00A0}" || $0 == "\u{202F}") }
    let d = currency.map(currencyDigits) ?? 2
    var intPart = s, frac = ""
    let lastComma = s.lastIndex(of: ","), lastDot = s.lastIndex(of: ".")
    func split(at i: String.Index) { intPart = String(s[..<i]); frac = String(s[s.index(after: i)...]) }
    if let c = lastComma, let p = lastDot {
        split(at: max(c, p))                                        // both: the last one is the decimal separator
    } else if let sep = lastComma ?? lastDot {
        let tail = s.distance(from: sep, to: s.endIndex) - 1
        let count = s.filter { $0 == s[sep] }.count
        if tail == 3 && (count > 1 || d != 3) {
            if count == 1 && d == 2 && s[sep] == "." { return nil }  // "1.234": a thousands dot or a 3-digit fraction
            intPart = s; frac = ""                                  // "1,234" / "1.234.567": thousands
        } else if count == 1 && tail >= 1 && tail <= d {
            split(at: sep)                                          // "12.34" / "12,5"
        } else if count == 1 && tail == 0 {
            return nil
        } else if tail == 3 {
            intPart = s; frac = ""
        } else { return nil }
    }
    let digits = intPart.filter { $0 != "," && $0 != "." }
    guard !digits.isEmpty, digits.count <= 10, digits.allSatisfy({ $0.isASCII && $0.isNumber }),
          frac.count <= d, frac.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
    while frac.count < d { frac += "0" }
    return Int(digits + frac)
}

/// An amount at the start of `text` (after spaces, ":" or "="): (amount, why not). An explicit currency is kept as written:
/// one ISO 4217 code that isn't supported is refused, never read as another. A "$" alone is the card's caps currency when
/// that's a dollar currency, else USD; with no currency at all, the amount has none (caps then refuse it).
func amountAtStart(_ text: String, cardCurrency: String?) -> (Money?, String?) {
    let symbols = "US\\$|CA\\$|C\\$|A\\$|AU\\$|NZ\\$|HK\\$|S\\$|\\$|€|£|¥|₹"
    let pattern = "^[\\s:=\\u00A0-]{0,6}(?:(\(symbols)|[A-Z]{3}(?![A-Za-z]))\\s?)?(\\d{1,3}(?:[,.\\u00A0\\u202F ]\\d{3})+(?:[.,]\\d{1,3})?|\\d+(?:[.,]\\d{1,3})?)(?![\\d])(?:\\s?(€|[A-Z]{3}(?![A-Za-z])))?"
    guard let rx = try? NSRegularExpression(pattern: pattern) else { return (nil, "internal error") }
    let t = String(text.prefix(200))
    let ns = t as NSString
    guard let m = rx.firstMatch(in: t, range: NSRange(location: 0, length: ns.length)) else { return (nil, "no amount follows it") }
    func g(_ i: Int) -> String? { m.range(at: i).location == NSNotFound ? nil : ns.substring(with: m.range(at: i)) }
    let marker = g(1) ?? g(3)
    let num = g(2) ?? ""
    var cur: String? = nil
    switch marker {
    case "€": cur = "EUR"
    case "£": cur = "GBP"
    case "¥": cur = "JPY"
    case "₹": cur = "INR"
    case "US$": cur = "USD"
    case "CA$", "C$": cur = "CAD"
    case "A$", "AU$": cur = "AUD"
    case "NZ$": cur = "NZD"
    case "HK$": cur = "HKD"
    case "S$": cur = "SGD"
    case "$": cur = cardCurrency.map { dollarCurrencies.contains($0) ? $0 : "USD" } ?? "USD"
    case .some(let code) where code.count == 3:
        guard iso4217Codes.contains(code) else { return (nil, "no amount follows it") }     // a word, not a currency
        guard knownCurrencies.contains(code) else { return (nil, "it's in \(code), a currency this doesn't support") }
        cur = code
    default: cur = nil
    }
    if g(1) != nil, let after = g(3), after != g(1), !(g(1) == "€" && after == "EUR") {
        return (nil, "it names two currencies")
    }
    // Without a currency, only a number written with a 2-digit fraction counts: "2 items" or "3 of 4" aren't totals.
    if cur == nil && !matches(num, "[.,][0-9]{2}$") { return (nil, "no amount follows it") }
    guard let v = pageNumber(num, currency: cur), v <= 1_000_000_000 else { return (nil, "its amount can't be read one way only") }
    guard v > 0 else { return (nil, "its amount is zero") }
    return (Money(minor: v, currency: cur), nil)
}

/// Words that make a "total" a part of the price, not what's paid: right before the label ("Sub-total", "Sub total",
/// "Items total", "Tax total") or anywhere in a --total-label.
let nonPayableTotalWords: Set<String> = ["sub", "subtotal", "pre", "item", "items", "merchandise", "product", "products", "tax", "taxes",
                                         "shipping", "delivery", "discount", "discounts", "saving", "savings", "fee", "fees", "deposit",
                                         "before", "excluding", "excl", "without"]

/// Why a --total-label can't name the payable total, or nil: it must say "total" or "due" (as whole words) and nothing
/// that names a part of the price ("Subtotal", "Total savings", "Tax total").
func payableLabelProblem(_ label: String) -> String? {
    let words = label.lowercased().split(whereSeparator: { !($0.isLetter || $0.isNumber) }).map(String.init)
    guard words.contains("total") || words.contains("due") else { return "--total-label must name the payable total (say \"total\" or \"due\")" }
    if let w = words.first(where: { nonPayableTotalWords.contains($0) }) { return "--total-label names a part of the price (\"\(w)\"), not the payable total" }
    return nil
}

/// The word right before a label, past spaces and dashes ("Sub-" and "Sub " both give "sub"), lower-cased.
func wordBefore(_ before: String) -> String {
    let seps: Set<Character> = [" ", "\t", "\n", "\r", "\u{00A0}", "\u{202F}", "-", "\u{2010}", "\u{2011}", "\u{2012}", "\u{2013}", "\u{2014}", "_", "\u{00B7}"]
    var t = Substring(before)
    while let c = t.last, seps.contains(c) { t = t.dropLast() }
    var w = ""
    while let c = t.last, c.isLetter { w.insert(c, at: w.startIndex); t = t.dropLast() }
    return w.lowercased()
}

/// The order total in a page's text: (amount, why not). Text from the page is untrusted: only an amount and a currency
/// come out of it, never its words. A place counts when the label (default "Total") stands as whole words, isn't a part
/// of the price ("Sub-total", "Items total": nonPayableTotalWords right before it) and is followed directly by the
/// amount ("Total savings $1.00" isn't). Exactly one place may count: none, or several (even agreeing), is unclear and
/// refused, so the agent hands payment to the user. `texts` are rendered text (page mode: the one element's; then the
/// words before each place are read from the same text), or, with `befores`, text starting at each place the browser
/// showed the label and the words before it (human mode).
func parsePageTotal(_ texts: [String], befores: [String]? = nil, cardCurrency: String?, label: String = "Total") -> (Money?, String?) {
    if let w = payableLabelProblem(label) { return (nil, w) }
    let esc = NSRegularExpression.escapedPattern(for: label).replacingOccurrences(of: "\\ ", with: " ").replacingOccurrences(of: " ", with: "\\s+")
    guard let lrx = try? NSRegularExpression(pattern: "(?<![\\p{L}\\p{N}])" + esc + "(?![\\p{L}\\p{N}])", options: [.caseInsensitive]) else {
        return (nil, "internal error")
    }
    var found: [Money] = []
    var why: String?
    for (i, text) in texts.prefix(20).enumerated() {
        let t = String(text.prefix(relayTextMax))
        let ns = t as NSString
        for m in lrx.matches(in: t, range: NSRange(location: 0, length: ns.length)).prefix(50) {
            var before = ns.substring(to: m.range.location)
            if let bs = befores {                       // one place per text: the one it starts with
                guard m.range.location == 0, i < bs.count else { continue }
                before = bs[i]
            }
            if nonPayableTotalWords.contains(wordBefore(String(before.suffix(40)))) { continue }
            let rest = ns.substring(from: m.range.location + m.range.length)
            let (money, w) = amountAtStart(rest, cardCurrency: cardCurrency)
            if let mm = money { found.append(mm) } else if let ww = w, ww != "no amount follows it" { why = ww }
        }
    }
    if let w = why { return (nil, "after \"\(label)\": \(w)") }
    guard let first = found.first else { return (nil, "no amount follows \"\(label)\" there") }
    guard found.count == 1 else {
        let n = Set(found.map { formatMoney($0.minor, $0.currency) }).count
        return (nil, "\"\(label)\" is followed by an amount in \(found.count) places" + (n > 1 ? " (\(n) different amounts)" : "") + ": it's unclear which is the total")
    }
    return (first, nil)
}

// MARK: - caps and the spending record

struct MerchantCap: Codable {
    var origin: String
    var amount: Int             // minor units, in the caps' currency
    var period_s: Int
}

struct CardCaps: Codable {
    var currency: String
    var per_purchase: Int?
    var per_period: Int?
    var period_s: Int?
    var merchants: [MerchantCap]?

    var isEmpty: Bool { per_purchase == nil && per_period == nil && (merchants ?? []).isEmpty }

    /// One line, as list and the approval show it.
    var words: String {
        var w: [String] = []
        if let p = per_purchase { w.append("at most \(formatMoney(p, currency)) a purchase") }
        if let p = per_period, let s = period_s { w.append("\(formatMoney(p, currency)) per \(periodWords(s))") }
        for m in merchants ?? [] { w.append("\(formatMoney(m.amount, currency)) per \(periodWords(m.period_s)) at \(m.origin)") }
        return w.isEmpty ? "no caps" : w.joined(separator: ", ")
    }
}

/// One purchase that filled a card's parts, kept (in the vault) for the period caps.
struct SpendEntry: Codable {
    var card: String            // the card secret's id
    var origin: String
    var amount: Int
    var currency: String
    var t: Double               // when (Unix seconds)
    var purchase: String
    var unaccounted: Bool? = nil // spending the caps' currency can't count (Shop charged in another): blocks the caps' period
}

/// A Shop completion whose final spending isn't recorded yet: written into the vault with its reservation, before the
/// completion runs, and removed only with what Shop charged beyond it. While it's there its entry buys nothing.
struct UnresolvedSpend: Codable {
    var card: String
    var origin: String
    var purchase: String        // the reservation's tag
    var amount: Int             // what was reserved
    var currency: String
    var t: Double
}

/// One record of spend-pending.jsonl: the outcome of an unresolved completion that the vault couldn't write at the time,
/// with an HMAC-SHA256 under the vault's spend_key (which only the unlocked vault holds) over the rest.
struct PendingSpendRecord: Codable {
    var v: Int
    var purchase: String        // the unresolved completion it settles
    var card: String
    var extras: [SpendEntry]    // spending beyond the reservation (each positive), maybe none
    var t: Double
    var mac: String
}

/// The longest period any cap may name.
let maxCapPeriod = 366 * 86400

func periodWords(_ s: Int) -> String {
    if s % 86400 == 0 { return s == 86400 ? "day" : "\(s / 86400) days" }
    if s % 3600 == 0 { return s == 3600 ? "hour" : "\(s / 3600) hours" }
    return "\(s) s"
}

/// "200/30d" -> (amount in minor units, period in seconds).
func parseCapPeriod(_ s: String, currency: String) -> (Int, Int)? {
    let p = s.split(separator: "/")
    guard p.count == 2, let a = parseMajorAmount(String(p[0]), currency: currency), let d = parseDuration(String(p[1])),
          d >= 3600, d <= maxCapPeriod else { return nil }
    return (a, d)
}

/// The card options `set --kind card|shop` and `policy` share: --pay, --label, --currency and the caps.
struct CardOptions {
    var pay: String?
    var label: String?
    var caps: CardCaps?
    var clearCaps = false
    var allowFrames: [String]?      // payment-form sites an agent card may also give card parts to (no question asks)
    var singleUse = false           // the user says the number is single-use (the vault then allows one purchase)
    var merchantLocked = false      // the user says the card is locked to its --origin merchants at the bank
    var given: Bool { pay != nil || label != nil || caps != nil || clearCaps || allowFrames != nil || singleUse || merchantLocked }
}

let cardValueFlags: Set<String> = ["pay", "label", "currency", "max-purchase", "max-per", "merchant-max", "allow-frame"]
let cardBoolFlags: Set<String> = ["no-caps", "single-use", "merchant-locked", "no-frames"]

/// Approval gates disclosure, not the charge: the warning for a card that's neither single-use nor merchant-locked.
func reusableCardWarning(_ s: Secret?) -> String? {
    guard let s, s.kind == "card", s.single_use != true, s.merchant_locked != true else { return nil }
    return """
    WARNING: approval gates disclosure, not the charge. Once this reusable card's details reach a site, that site can
    charge it again, for any amount, as often as it likes; the vault can't stop that, and neither can an approval or a cap.
    Use a single-use or merchant-locked card where you can (a Shop spend request's card is both: metacomputer shop card),
    and mark yours with --single-use or --merchant-locked when it is one.
    """
}

func parseCardOptions(_ a: Args, existing: CardCaps?) throws -> CardOptions {
    var o = CardOptions()
    if let p = a.last("pay") {
        guard ["ask", "agent"].contains(p) else { throw usageError("--pay ask (every purchase asks you; the default) or agent (purchases within the caps don't ask)") }
        o.pay = p
    }
    if let l = a.last("label") {
        guard !l.isEmpty, l.count <= 60, clean(l, max: 60) == l else { throw usageError("--label: 1-60 printable characters, like \"Agent Visa\"") }
        o.label = l
    }
    o.clearCaps = a.has("no-caps")
    o.singleUse = a.has("single-use")
    o.merchantLocked = a.has("merchant-locked")
    if a.has("no-frames") { o.allowFrames = [] }
    if !a.all("allow-frame").isEmpty {
        guard !a.has("no-frames") else { throw usageError("--allow-frame or --no-frames, not both") }
        var fr: [String] = []
        for f in a.all("allow-frame") {
            guard let c = canonicalOrigin(f), c == f, c.hasPrefix("https://") else {
                throw usageError("--allow-frame: a payment form's site as https://host, like https://js.stripe.com")
            }
            if !fr.contains(c) { fr.append(c) }
        }
        guard fr.count <= 8 else { throw usageError("--allow-frame: at most 8") }
        o.allowFrames = fr
    }
    let capFlags = ["max-purchase", "max-per", "merchant-max"].filter { a.last($0) != nil }
    if a.last("currency") != nil || !capFlags.isEmpty {
        guard !o.clearCaps else { throw usageError("--no-caps clears the caps: leave out --currency, --max-purchase, --max-per and --merchant-max") }
        let cur = (a.last("currency") ?? existing?.currency ?? "USD").uppercased()
        guard validCurrency(cur) else { throw usageError("--currency: a three-letter code like USD or EUR") }
        if let ex = existing, ex.currency != cur, capFlags.count < 1 {
            throw usageError("changing the caps' currency needs the caps again in it (--max-purchase, --max-per, --merchant-max)")
        }
        var caps = (existing?.currency == cur ? existing : nil) ?? CardCaps(currency: cur)
        if let m = a.last("max-purchase") {
            guard let v = parseMajorAmount(m, currency: cur) else { throw usageError("--max-purchase: an amount in \(cur), like 50 or 49.99") }
            caps.per_purchase = v
        }
        if let m = a.last("max-per") {
            guard let (v, d) = parseCapPeriod(m, currency: cur) else {
                throw usageError("--max-per: an amount and a period, like 200/30d or 50/1d (from 1h to 366d)")
            }
            caps.per_period = v
            caps.period_s = d
        }
        var merchants = a.all("merchant-max").isEmpty ? (caps.merchants ?? []) : []
        for mm in a.all("merchant-max") {
            guard let eq = mm.firstIndex(of: "="), let o = canonicalOrigin(String(mm[..<eq])),
                  let (v, d) = parseCapPeriod(String(mm[mm.index(after: eq)...]), currency: cur) else {
                throw usageError("--merchant-max: a site and an amount per period, like https://shop.example=100/30d")
            }
            merchants.removeAll { $0.origin == o }
            merchants.append(MerchantCap(origin: o, amount: v, period_s: d))
        }
        guard merchants.count <= 32 else { throw usageError("--merchant-max: at most 32 sites") }
        caps.merchants = merchants.isEmpty ? nil : merchants
        o.caps = caps
    }
    return o
}

/// The card's parts, typed at the user's own terminal (each hidden), checked; the caller wipes nothing of a String, so
/// the bytes typed are wiped here.
func readCardFromTerminal(_ name: String) throws -> CardValue {
    var raw = try readHidden("Card number for \(name): ")
    let number = cardNumber(String(decoding: raw, as: UTF8.self))
    wipe(&raw)
    guard let n = number else { throw VaultError("that isn't a card number (12-19 digits that pass the card check)") }
    raw = try readHidden("Expiry (MM/YY): ")
    let exp = cardExpiry(String(decoding: raw, as: UTF8.self))
    wipe(&raw)
    guard let (m, y) = exp else { throw VaultError("the expiry: MM/YY (or MM/YYYY), not in the past") }
    raw = try readHidden("Security code (CVC): ")
    let cvc = String(decoding: raw, as: UTF8.self)
    wipe(&raw)
    guard (3...4).contains(cvc.count), cvc.allSatisfy({ $0.isASCII && $0.isNumber }) else { throw VaultError("the security code: 3 or 4 digits") }
    raw = try readHidden("Name on the card: ")
    let holder = String(decoding: raw, as: UTF8.self).trimmingCharacters(in: .whitespaces)
    wipe(&raw)
    guard !holder.isEmpty, holder.count <= 100, clean(holder, max: 100) == holder else { throw VaultError("the name on the card: 1-100 printable characters") }
    raw = try readHidden("Billing ZIP or postal code (Enter to leave it out): ")
    let zip = String(decoding: raw, as: UTF8.self).trimmingCharacters(in: .whitespaces)
    wipe(&raw)
    guard zip.count <= 12, matches(zip, "^[A-Za-z0-9 -]*$") else { throw VaultError("the postal code: up to 12 letters, digits, spaces or dashes") }
    return CardValue(number: n, exp_month: m, exp_year: y, cvc: cvc, name: holder, zip: zip.isEmpty ? nil : zip)
}

// MARK: - client commands

/// `pay`: start a purchase, fill a part of one, or end it (Server: opPay, opPayFill, opPayEnd).
func cmdPay(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: pay CARD --vm VM --origin https://shop (--total-selector CSS | --human [--window-id N]) [--total-label TEXT]"
        + " [--frame-origin https://pay.example ...] [--profile P] [--browser-pid PID] [--fill PART=CSS ...]"
        + " | pay --purchase KEY --vm VM --field PART [--selector CSS] | pay --purchase KEY --done   (PART: \(cardParts.joined(separator: "|")))"
    let a = try parseArgs(argv, values: ["vm", "origin", "total-selector", "total-label", "window-id", "frame-origin", "profile", "browser-pid",
                                         "fill", "purchase", "field", "selector", "request-id", "on-behalf-pid"], flags: ["human", "done", "json"],
                          repeatable: ["frame-origin", "fill"])
    // Ending a purchase takes its key and nothing else (at most --vm, which must be the purchase's own VM): --done with
    // any other word, or without --purchase, is refused, never read as a new purchase or a fill.
    if a.has("done") {
        let others = Set(a.values.keys).subtracting(["purchase", "vm"]).union(a.flags.subtracting(["done"]))
        guard let pid = a.last("purchase"), a.values["purchase"]?.count == 1, (a.values["vm"]?.count ?? 0) <= 1, others.isEmpty,
              a.positional.isEmpty, validPurchaseKey(pid), a.last("vm").map(validName) ?? true else {
            throw usageError("usage: pay --purchase KEY --done [--vm VM]   (--done ends a purchase and takes nothing else; KEY: the purchase_id pay printed)")
        }
        var req: [String: Any] = ["op": "pay_end", "purchase": pid]
        if let vm = a.last("vm") { req["vm"] = vm }
        let res: [String: Any]
        do { res = try request(paths, req, timeout: 60) } catch ClientError.locked {
            oprint(jsonLine(["ok": false, "reason": "locked", "purchase_id": pid]))
            return Exit.locked
        }
        printResult(res, ["ok", "purchase", "name", "label", "origin", "filled", "outcome", "reason"])
        return exitFor(res)
    }
    if let pid = a.last("purchase") {
        guard validPurchaseKey(pid), a.positional.isEmpty else { throw usageError(usage + "   (KEY: the purchase_id pay printed, 32 hex digits)") }
        guard let vm = a.last("vm"), validName(vm), let field = a.last("field"), cardParts.contains(field) else {
            throw usageError("usage: pay --purchase ID --vm VM --field \(cardParts.joined(separator: "|")) [--selector CSS]")
        }
        guard a.last("origin") == nil, a.last("fill") == nil, a.last("total-selector") == nil else {
            throw usageError("a purchase's site and total were fixed when it was approved: --purchase takes --field and --selector only")
        }
        var req: [String: Any] = ["op": "pay_fill", "purchase": pid, "vm": vm, "field": field, "human": a.has("human")]
        if let s = a.last("selector") {
            guard !s.isEmpty, s.count <= 300 else { throw usageError("--selector: 1-300 characters of CSS that match exactly one field") }
            req["selector"] = s
        }
        let res: [String: Any]
        do { res = try request(paths, req, timeout: 300, cancellable: true) } catch ClientError.locked {
            return lockedUse(paths, "pay_fill", ["vm": vm, "purchase_id": pid, "field": field])
        }
        printResult(res, ["ok", "purchase", "name", "label", "vm", "field", "origin", "route", "filled", "reason"])
        return exitFor(res)
    }
    guard a.positional.count == 1, validName(a.positional[0]), let vm = a.last("vm"), validName(vm), let origin = a.last("origin") else {
        throw usageError(usage)
    }
    guard canonicalOrigin(origin) == origin else { throw usageError("--origin: the merchant's site as https://host (no path), as the address bar shows it") }
    let name = a.positional[0]
    var req: [String: Any] = ["op": "pay", "name": name, "vm": vm, "origin": origin, "human": a.has("human")]
    if a.has("human") {
        guard a.last("total-selector") == nil, a.last("fill") == nil, a.last("profile") == nil, a.last("browser-pid") == nil, a.all("frame-origin").isEmpty else {
            throw usageError("--human: the focused field of the VM's Firefox (or a Chrome started normally); its total comes from the words after --total-label (default \"Total\"). No --total-selector, --fill, --profile, --browser-pid or --frame-origin")
        }
        if let l = a.last("total-label") {
            guard !l.isEmpty, l.count <= 40, clean(l, max: 40) == l else { throw usageError("--total-label: 1-40 printable characters, the words before the total on the page") }
            req["total_label"] = l
        }
        if let w = a.last("window-id") {
            guard let n = Int(w), n > 0 else { throw usageError("--window-id: the browser window's id (list_windows)") }
            req["window_id"] = n
        }
    } else {
        guard let ts = a.last("total-selector"), !ts.isEmpty, ts.count <= 300 else {
            throw usageError("--total-selector CSS: the element that shows the order total (the approval shows what it says), or --human for Firefox")
        }
        guard a.last("window-id") == nil else { throw usageError("--window-id goes with --human") }
        req["total_selector"] = ts
        if let l = a.last("total-label") {
            guard !l.isEmpty, l.count <= 40, clean(l, max: 40) == l else { throw usageError("--total-label: 1-40 printable characters, the words right before the total") }
            req["total_label"] = l
        }
        var frames: [String] = []
        for f in a.all("frame-origin") {
            guard let c = canonicalOrigin(f), c == f, c != origin else {
                throw usageError("--frame-origin: a payment form's own site, as https://host, like https://js.stripe.com (another site than --origin)")
            }
            if !frames.contains(c) { frames.append(c) }
        }
        guard frames.count <= 4 else { throw usageError("--frame-origin: at most 4") }
        if !frames.isEmpty { req["frame_origins"] = frames }
        if let p = a.last("profile") {
            guard validName(p) else { throw usageError("--profile: a driver profile name") }
            req["profile"] = p
        }
        try browserPIDOption(a, &req)
        var fills: [[String]] = []
        for f in a.all("fill") {
            guard let eq = f.firstIndex(of: "="), cardParts.contains(String(f[..<eq])) else {
                throw usageError("--fill PART=CSS, PART one of \(cardParts.joined(separator: ", ")), like --fill number='#cc-number'")
            }
            let part = String(f[..<eq]), sel = String(f[f.index(after: eq)...])
            guard !sel.isEmpty, sel.count <= 300, !fills.contains(where: { $0[0] == part }) else {
                throw usageError("--fill \(part)=: 1-300 characters of CSS, once per part")
            }
            fills.append([part, sel])
        }
        if !fills.isEmpty { req["fills"] = fills }
    }
    try useOptions(a, &req)
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 600, cancellable: true) } catch ClientError.locked {
        return lockedUse(paths, "pay", ["name": name, "vm": vm, "origin": origin])
    }
    printResult(res, ["ok", "name", "label", "vm", "origin", "frame_origins", "purchase_id", "purchase", "total", "approval", "rules", "caps", "filled",
                      "failed", "expires_in_s", "request_id", "reason", "handoff"])
    return exitFor(res)
}

/// `shop-complete CHECKOUT --vm VM --shop DOMAIN --instrument FILE|- [--account SHOP]`: the vault reads the Shop checkout
/// with the CLI the user pinned, asks (the shop entry's policy), reads it again and completes it only if it's unchanged
/// (Server: opShopComplete). The instrument is the exact object the checkout offered, from a file or stdin.
func cmdShopComplete(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: shop-complete CHECKOUT --vm VM --shop DOMAIN --instrument FILE|- [--account SHOP]"
    let a = try parseArgs(argv, values: ["vm", "shop", "instrument", "account", "request-id", "on-behalf-pid"], flags: ["json"])
    guard a.positional.count == 1, matches(a.positional[0], "^[A-Za-z0-9:/._?=&-]{1,300}$"), let vm = a.last("vm"), validName(vm),
          let shop = a.last("shop"), matches(shop, "^[a-z0-9]([a-z0-9-]{0,62}\\.)+[a-z]{2,24}$"), let src = a.last("instrument") else { throw usageError(usage) }
    if let acct = a.last("account") { guard validName(acct) else { throw usageError(usage) } }
    let raw = src == "-" ? Array(FileHandle.standardInput.readData(ofLength: 64 << 10)) : try readFileBytes(absolutePath(src), max: 64 << 10)
    guard let v = parseLexJSON(raw), case .obj = v, let text = String(bytes: raw, encoding: .utf8) else {
        throw usageError("--instrument: one JSON object, exactly as the checkout offered it (from a file or stdin)")
    }
    // As written: the vault compares it with the checkout's instruments by its exact text and passes it on unchanged.
    var req: [String: Any] = ["op": "shop_complete", "vm": vm, "checkout": a.positional[0], "shop": shop, "instrument_text": text]
    if let acct = a.last("account") { req["name"] = acct }
    try useOptions(a, &req)
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 600, cancellable: true) } catch ClientError.locked {
        return lockedUse(paths, "shop_complete", ["vm": vm, "origin": "https://" + shop])
    }
    printResult(res, ["ok", "name", "label", "origin", "total", "approval", "caps", "outcome", "charged", "verified", "spending_recorded", "warning", "order", "continue_url", "handoff", "request_id", "reason"])
    return exitFor(res)
}

/// `shop-card NAME --spend-request ID [--wait 90]`: the single-use card Shop issued for a spend request the user approved
/// in Shop, read by the unlock process straight from the file the pinned Shop CLI writes (Server: opShopCard).
func cmdShopCard(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: shop-card NAME --spend-request ID [--wait 90]   (the vault runs the Shop CLI you pinned with `shop-cli set`)"
    let a = try parseArgs(argv, values: ["spend-request", "wait", "request-id", "on-behalf-pid"], flags: ["json"])
    guard a.positional.count == 1, validName(a.positional[0]), let sr = a.last("spend-request"), matches(sr, "^[A-Za-z0-9_-]{8,80}$") else { throw usageError(usage) }
    let wait = Int(a.last("wait") ?? "90") ?? -1
    guard wait >= 0, wait <= 120 else { throw usageError("--wait: seconds to wait for your approval in Shop (0-120)") }
    var req: [String: Any] = ["op": "shop_card", "name": a.positional[0], "spend_request": sr, "wait": wait]
    try useOptions(a, &req)
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 420, cancellable: true) } catch ClientError.locked {
        return lockedUse(paths, "shop_card", ["name": a.positional[0]])
    }
    printResult(res, ["ok", "name", "label", "origin", "caps", "single_use", "reason"])
    return exitFor(res)
}

// MARK: - Shop's CLI, pinned by the user

/// The `shop` CLI the user pinned in their own terminal, everything Node can load when the vault runs it: the runtime
/// (node) and the libraries it loads that aren't the system's, the CLI's entry script and every file of its package,
/// each by its SHA-256; and the one file of the user's the CLI may use, its sign-in (`auth_file`). The vault runs
/// nothing else for Shop, and runs it confined (shopConfinementArgs): module loading held to the package, nothing of
/// your home but that file. A pin made before confinement (`confined` missing) is refused until it's made again.
struct ShopCLIPin: Codable {
    var runtime: String
    var script: String
    var root: String
    var runtime_sha: String
    var tree_sha: String
    var set: String
    var libs: [String: String]?
    var auth_file: String?
    var confined: Int?
}

/// Why a file or folder isn't safe to run from (owned by someone other than you or root, writable by others, a link
/// where a file was expected), checked for it and every folder above it; nil when it is.
func runPathProblem(_ path: String) -> String? {
    var p = path
    var first = true
    while true {
        var st = stat()
        guard lstat(p, &st) == 0 else { return "\(p) can't be read" }
        if first && (st.st_mode & S_IFMT) == S_IFLNK { return "\(p) is a link" }
        guard st.st_uid == getuid() || st.st_uid == 0 else { return "\(p) belongs to another user" }
        let dir = (st.st_mode & S_IFMT) == S_IFDIR
        // Writable by other users: refused, except a folder above it with the sticky bit (/tmp), where others can't
        // replace what's yours, and group write for admin or wheel (Homebrew's folders), whose members can be root anyway.
        let worldW = st.st_mode & 0o002 != 0 && !(dir && !first && st.st_mode & S_ISVTX != 0)
        let groupW = st.st_mode & 0o020 != 0 && st.st_gid != 80 && st.st_gid != 0
        if worldW || groupW { return "\(p) can be changed by other users (" + (dir ? "a folder" : "a file") + ")" }
        first = false
        if p == "/" { return nil }
        p = (p as NSString).deletingLastPathComponent
        if p.isEmpty { p = "/" }
    }
}

/// A hash over every regular file and link in a package folder (sorted paths, each file's SHA-256, each link's target),
/// with the folder's own checks; nil when it's too big or something in it isn't safe. A link must stay inside the
/// package (its target's own file is hashed then): one that leads out of it would let changes outside the pin in.
func packageTreeHash(_ root: String) -> (String?, String?) {
    guard let en = FileManager.default.enumerator(atPath: root), let realRoot = realPath(root) else { return (nil, "\(root) can't be read") }
    var lines: [String] = []
    var total = 0
    while let rel = en.nextObject() as? String {
        guard lines.count < 5000 else { return (nil, "the CLI's package has more than 5000 files") }
        let full = root + "/" + rel
        var st = stat()
        guard lstat(full, &st) == 0 else { return (nil, "\(full) can't be read") }
        guard st.st_uid == getuid() || st.st_uid == 0, st.st_mode & 0o002 == 0, st.st_mode & 0o020 == 0 || st.st_gid == 80 || st.st_gid == 0 else {
            return (nil, "\(full) can be changed by others")
        }
        switch st.st_mode & S_IFMT {
        case S_IFREG:
            total += Int(st.st_size)
            guard total <= 256 << 20, let b = try? readFileBytes(full, max: 256 << 20) else { return (nil, "the CLI's package is too large or unreadable") }
            lines.append("f " + rel + " " + sha256Hex(b))
        case S_IFLNK:
            let target = (try? FileManager.default.destinationOfSymbolicLink(atPath: full)) ?? "?"
            guard let real = realPath(full), real == realRoot || real.hasPrefix(realRoot + "/") else {
                return (nil, "\(full) is a link out of the package (to \(target)): the vault can't pin what it leads to")
            }
            lines.append("l " + rel + " " + target)
        case S_IFDIR: continue
        default: return (nil, "\(full) isn't a regular file")
        }
    }
    return (sha256Hex(Array(lines.sorted().joined(separator: "\n").utf8)), nil)
}

/// (libraries named by load commands, the weak ones among them, run paths) of a Mach-O program or library, for this Mac's
/// architecture (a universal file's matching slice); nil when it isn't one this reads.
func machOLoads(_ path: String) -> ([String], Set<String>, [String])? {
    guard let head = try? readFilePrefix(path, max: 4 << 20), head.count >= 32 else { return nil }
    func u32(_ b: [UInt8], _ o: Int, big: Bool = false) -> UInt32? {
        guard o >= 0, o + 4 <= b.count else { return nil }
        let v = UInt32(b[o]) | UInt32(b[o + 1]) << 8 | UInt32(b[o + 2]) << 16 | UInt32(b[o + 3]) << 24
        return big ? v.byteSwapped : v
    }
    #if arch(arm64)
    let myCPU: UInt32 = 0x0100000C
    #else
    let myCPU: UInt32 = 0x01000007
    #endif
    var b = head
    if u32(head, 0, big: true) == 0xCAFEBABE {                     // universal: find this Mac's slice
        guard let nfat = u32(head, 4, big: true), nfat > 0, nfat < 16 else { return nil }
        var off: Int? = nil
        for i in 0..<Int(nfat) {
            let e = 8 + i * 20
            if u32(head, e, big: true) == myCPU, let o = u32(head, e + 8, big: true) { off = Int(o) }
        }
        guard let o = off, let fd = Optional(open(path, O_RDONLY | O_CLOEXEC)), fd >= 0 else { return nil }
        defer { close(fd) }
        var buf = [UInt8](repeating: 0, count: 1 << 20)
        let n = buf.withUnsafeMutableBytes { pread(fd, $0.baseAddress!, 1 << 20, off_t(o)) }
        guard n > 32 else { return nil }
        b = Array(buf[0..<n])
    }
    guard u32(b, 0) == 0xFEEDFACF, u32(b, 4) == myCPU, let ncmds = u32(b, 16), let size = u32(b, 20), 32 + Int(size) <= b.count else { return nil }
    var libs: [String] = [], weak: Set<String> = [], rpaths: [String] = []
    var o = 32
    for _ in 0..<Int(ncmds) {
        guard let cmd = u32(b, o), let cmdsize = u32(b, o + 4), cmdsize >= 8, o + Int(cmdsize) <= 32 + Int(size) else { return nil }
        func str(_ at: Int) -> String? {
            guard let so = u32(b, o + at), Int(so) < Int(cmdsize) else { return nil }
            let s = b[(o + Int(so))..<(o + Int(cmdsize))].prefix { $0 != 0 }
            return String(bytes: s, encoding: .utf8)
        }
        switch cmd {
        case 0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023:       // LC_LOAD_DYLIB, _WEAK_, LC_REEXPORT_, LC_LAZY_LOAD_, _UPWARD_
            guard let s = str(8) else { return nil }
            libs.append(s)
            if cmd == 0x80000018 { weak.insert(s) }
        case 0x8000001C:                                           // LC_RPATH
            guard let s = str(8) else { return nil }
            rpaths.append(s)
        default: break
        }
        o += Int(cmdsize)
    }
    return (libs, weak, rpaths)
}

/// Every library outside the system's (/usr/lib, /System: sealed) that a program loads, and theirs, resolved the way the
/// loader finds them (@rpath, @loader_path, @executable_path), by the path it loads each from: (path: SHA-256), or why
/// not. A library someone could swap or plant (Homebrew's are yours) is part of what Node runs, so it's pinned too.
func dylibClosure(_ exe: String) -> ([String: String]?, String?) {
    let exeDir = (exe as NSString).deletingLastPathComponent
    var out: [String: String] = [:]
    var queue: [(String, [String])] = [(exe, [])]
    var visited: Set<String> = []
    func system(_ p: String) -> Bool { p.hasPrefix("/usr/lib/") || p.hasPrefix("/System/") }
    while let (file, inherited) = queue.popLast() {
        guard visited.insert(file).inserted else { continue }
        guard visited.count < 200 else { return (nil, "the runtime loads more than 200 libraries") }
        guard let (libs, weak, rp) = machOLoads(file) else { return (nil, "\(file) isn't a program or library this can read") }
        let loaderDir = (file as NSString).deletingLastPathComponent
        func expand(_ s: String) -> String {
            s.replacingOccurrences(of: "@loader_path", with: loaderDir).replacingOccurrences(of: "@executable_path", with: exeDir)
        }
        let rpaths = rp.map(expand) + inherited
        for lib in libs {
            if system(lib) { continue }
            let cands = lib.hasPrefix("@rpath/") ? rpaths.map { $0 + "/" + lib.dropFirst("@rpath/".count) } : [expand(lib)]
            guard let hit = cands.first(where: { fileExists($0) }) else {
                if weak.contains(lib) { continue }
                return (nil, "can't find \(lib), which \(file) loads")
            }
            let load = (hit as NSString).standardizingPath
            guard let real = realPath(load) else { return (nil, "\(load) can't be read") }
            if system(real) { continue }
            if out[load] == nil {
                if let w = runPathProblem(real) { return (nil, w) }
                guard let bytes = try? readFileBytes(real, max: 512 << 20) else { return (nil, "\(real) can't be read") }
                out[load] = sha256Hex(bytes)
                queue.append((real, rpaths))
            }
        }
    }
    return (out, nil)
}

/// Why the pinned CLI may not run now (moved, changed since it was pinned, not safe, pinned before confinement), or nil.
func verifyShopPin(_ pin: ShopCLIPin) -> String? {
    guard pin.confined == 1, let auth = pin.auth_file, auth.hasPrefix("/") else {
        return "Shop's CLI was pinned by an older version, before the vault ran it confined: run `metacomputer secrets shop-cli set` again, in your own terminal"
    }
    for f in [pin.runtime, pin.script] { if let w = runPathProblem(f) { return "Shop's CLI can't run: \(w)" } }
    if let w = runPathProblem(pin.root) { return "Shop's CLI can't run: \(w)" }
    guard let rb = try? readFileBytes(pin.runtime, max: 512 << 20), sha256Hex(rb) == pin.runtime_sha else {
        return "Shop's CLI can't run: its runtime (\(pin.runtime)) changed since it was pinned (run `metacomputer secrets shop-cli set` again, in your own terminal)"
    }
    let (libs, lwhy) = dylibClosure(pin.runtime)
    guard let l = libs, l == (pin.libs ?? [:]) else {
        return "Shop's CLI can't run: " + (lwhy ?? "the libraries its runtime loads changed since it was pinned (after an update, run `metacomputer secrets shop-cli set` again, in your own terminal)")
    }
    let (h, why) = packageTreeHash(pin.root)
    guard let hh = h, hh == pin.tree_sha else {
        return "Shop's CLI can't run: " + (why ?? "its files changed since it was pinned (after an update, run `metacomputer secrets shop-cli set` again, in your own terminal)")
    }
    return nil
}

/// Node's arguments that confine the pinned CLI, before its script: no native addons, and a preload (a data: URL, so no
/// file of it can be changed) that registers module hooks holding every module Node resolves or loads, in the main
/// thread and every worker, to Node's own (node:) or a file inside the pinned package; anything else stops the CLI. A
/// Node without module.registerHooks (older than 22.15) can't do this: the preload exits with status 97 and the vault
/// refuses rather than run it unconfined.
func shopConfinementArgs(root: String) -> [String] {
    let rootJSON = jsonLine(["r": (realPath(root) ?? root) + "/"])
    let js = """
    import * as mod from "node:module";
    import { fileURLToPath } from "node:url";
    const ROOT = (\(rootJSON)).r;
    if (typeof mod.registerHooks !== "function") { process.stderr.write("metacomputer-confinement: no module.registerHooks\\n"); process.exit(97); }
    const ok = (u) => typeof u === "string" && (u.startsWith("node:") || (u.startsWith("file:") && (() => { try { return fileURLToPath(u).startsWith(ROOT); } catch { return false; } })()));
    const refuse = () => { process.stderr.write("metacomputer-confinement: a module outside the pinned package\\n"); process.exit(98); };
    mod.registerHooks({
      resolve(s, c, next) { const r = next(s, c); if (!ok(r.url)) refuse(); return r; },
      load(u, c, next) { if (!ok(u)) refuse(); return next(u, c); },
    });
    """
    return ["--no-addons", "--import", "data:text/javascript;base64," + Data(js.utf8).base64EncodedString()]
}

/// macOS's sandbox-exec (root's, in /usr/bin), which runs a program under a Seatbelt profile.
func sandboxExecPath() -> String {
    #if MCOMP_VAULT_TESTING
    // Test builds: a stand-in path in the stand-in CLI's folder (tests/test_vault_payments.py: no sandbox-exec, so no run).
    if let d = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_SHOP_SPY"],
       let p = try? String(contentsOfFile: d + "/sandbox-exec", encoding: .utf8) { return p.trimmingCharacters(in: .whitespacesAndNewlines) }
    #endif
    return "/usr/bin/sandbox-exec"
}

/// A path as a Seatbelt (SBPL) string, or nil when it can't be written as one safely.
func sbplString(_ p: String) -> String? {
    guard p.hasPrefix("/"), !p.contains("\""), !p.contains("\\"),
          !p.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7F }) else { return nil }
    return "\"" + p + "\""
}

/// The Seatbelt profile the pinned Shop CLI runs under (sandbox-exec -p, so no file of it can be changed), on top of
/// Node's module hooks: everything is denied but what the CLI needs. It may read and map the pinned runtime and the
/// libraries it loads (by the paths they load from and their real paths), read the pinned package, read and write
/// its own folder and the sign-in file (with the temporary files beside it that Shop's CLI writes it through), and
/// the system's own libraries and frameworks (system.sb); look up files' metadata (its credential store checks every
/// folder above the sign-in file); and connect to one TCP port on loopback, the run's own proxy (ShopProxy, which
/// reaches public hosts on 443 only): no other address, local or not, and no Unix socket. It may run no program, not
/// even another copy of itself, and fork nothing. nil when a path can't be written into the profile.
func shopSandboxProfile(_ pin: ShopCLIPin, run: String, proxyPort: UInt16, extraRW: [String] = []) -> String? {
    guard let auth = pin.auth_file, let rt = realPath(pin.runtime), let root = realPath(pin.root), let runReal = realPath(run) else { return nil }
    let authDirGiven = (auth as NSString).deletingLastPathComponent
    let authDir = realPath(authDirGiven) ?? authDirGiven
    let authFile = authDir + "/" + (auth as NSString).lastPathComponent
    var loads = [rt, pin.runtime]
    for l in (pin.libs ?? [:]).keys.sorted() {
        loads.append(l)
        if let r = realPath(l) { loads.append(r) }
    }
    let rx = authDir.map { c -> String in c.isLetter || c.isNumber || c == "/" || c == " " || c == "_" ? String(c) : "\\" + String(c) }.joined()
    guard let rtS = sbplString(rt), let rootS = sbplString(root), let runS = sbplString(runReal), let authS = sbplString(authFile),
          let authDirS = sbplString(authDir), !rx.contains("\""), sbplString(authDir + "/x") != nil else { return nil }
    var lits: [String] = []
    for l in Array(Set(loads)).sorted() { guard let s = sbplString(l) else { return nil }; lits.append("(literal \(s))") }
    var p = """
    (version 1)
    (deny default)
    (import "system.sb")
    (allow file-read-metadata)
    (allow process-exec (literal \(rtS)))
    (allow file-read* file-map-executable \(lits.joined(separator: " ")))
    (allow file-read* (subpath \(rootS)))
    (allow file-read* file-write* (subpath \(runS)))
    (allow file-read* file-write* (literal \(authS)) (regex #"^\(rx)/\\.shop-auth-[0-9a-f]{32}\\.tmp$"))
    (allow file-read-data (literal \(authDirS)))
    (allow network-outbound (remote tcp "localhost:\(proxyPort)"))
    (allow system-socket)
    (allow sysctl-read)
    (allow signal (target self))
    (allow mach-lookup (global-name "com.apple.dnssd.service") (global-name "com.apple.SystemConfiguration.DNSConfiguration")
      (global-name "com.apple.SystemConfiguration.configd") (global-name "com.apple.trustd.agent"))
    (allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center"))

    """
    for e in extraRW { guard let s = sbplString(realPath(e) ?? e) else { return nil }; p += "(allow file-read* file-write* (subpath \(s)))\n" }
    return p
}

/// The first `name` on the PATH of this (the user's own) terminal.
func whichOnPath(_ name: String) -> String? {
    for d in (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":") {
        let f = String(d) + "/" + name
        if access(f, X_OK) == 0 { return f }
    }
    return nil
}

/// Where Shop's CLI keeps its sign-in for this terminal, as it finds it itself: SHOP_AUTH_FILE, else
/// $XDG_CONFIG_HOME/shop-node-wasm/credentials.json, else ~/.config/shop-node-wasm/credentials.json.
func shopAuthFileFromEnvironment() -> String? {
    let env = ProcessInfo.processInfo.environment
    func normal(_ p: String?) -> String? {
        guard let p, p.hasPrefix("/"), p.count > 1, (p as NSString).standardizingPath == p, !p.hasSuffix("/") else { return nil }
        return p
    }
    if let f = normal(env["SHOP_AUTH_FILE"]) { return f }
    if let x = normal(env["XDG_CONFIG_HOME"]) { return x + "/shop-node-wasm/credentials.json" }
    return normal(env["HOME"] ?? homeDirectory()).map { $0 + "/.config/shop-node-wasm/credentials.json" }
}

/// `shop-cli set [--cli PATH] [--runtime PATH] [--auth-file PATH]` (the user, in their own terminal): pin Shopify's
/// `shop` CLI, its runtime and the libraries that loads (by default `shop` and `node` from this terminal's PATH), and the
/// sign-in file it may use | `shop-cli show`.
func cmdShopCLI(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: shop-cli set [--cli PATH] [--runtime PATH] [--auth-file PATH] | shop-cli show   (your own terminal)"
    guard let sub = argv.first, ["set", "show"].contains(sub) else { throw usageError(usage) }
    let a = try parseArgs(Array(argv.dropFirst()), values: ["cli", "runtime", "auth-file"], flags: [])
    try requireTTY("shop-cli \(sub)")
    installPromptSignalHandlers()
    if sub == "show" {
        let (key, p) = try unlockKey(paths)
        key.wipeKey()
        guard let pin = p.shop_cli else { oprint("No Shop CLI pinned (metacomputer secrets shop-cli set)."); return Exit.ok }
        oprint("Shop CLI: \(pin.runtime) \(pin.script) (package \(pin.root), sign-in \(pin.auth_file ?? "-"), pinned \(pin.set))"
               + (verifyShopPin(pin).map { "; \($0)" } ?? "; unchanged since"))
        return Exit.ok
    }
    guard let cliGiven = a.last("cli") ?? whichOnPath("shop") else {
        throw VaultError("no shop CLI on this terminal's PATH: npm install --global @shopify/shop, or give --cli PATH")
    }
    guard let script = realPath(absolutePath(cliGiven)) else { throw VaultError("\(cliGiven) can't be read") }
    // The runtime: given, or from the script's first line (#!/usr/bin/env node: node on this terminal's PATH).
    var runtimeGiven = a.last("runtime")
    if runtimeGiven == nil {
        let head = String(decoding: (try? readFilePrefix(script, max: 256)) ?? [], as: UTF8.self).split(separator: "\n").first.map(String.init) ?? ""
        if head.hasPrefix("#!/usr/bin/env ") { runtimeGiven = whichOnPath(String(head.dropFirst("#!/usr/bin/env ".count)).trimmingCharacters(in: .whitespaces)) }
        else if head.hasPrefix("#!/") { runtimeGiven = String(head.dropFirst(2)).split(separator: " ").first.map(String.init) }
    }
    guard let rg = runtimeGiven, let runtime = realPath(absolutePath(rg)) else { throw VaultError("can't tell which runtime runs \(script): give --runtime PATH (node)") }
    guard (runtime as NSString).lastPathComponent.hasPrefix("node") else {
        throw VaultError("not pinned: the vault runs Shop's CLI only with Node, confined (\(runtime) isn't node): give --runtime PATH")
    }
    guard let auth = a.last("auth-file").map({ (absolutePath($0) as NSString).standardizingPath }) ?? shopAuthFileFromEnvironment() else {
        throw VaultError("can't tell where Shop's CLI keeps its sign-in: give --auth-file PATH")
    }
    // The package folder: the nearest folder above the script with a package.json (at most 4 up), else the script's own.
    var root = (script as NSString).deletingLastPathComponent
    var probe = root
    for _ in 0..<4 {
        if fileExists(probe + "/package.json") { root = probe; break }
        probe = (probe as NSString).deletingLastPathComponent
    }
    guard script.hasPrefix(root + "/") else { throw VaultError("not pinned: \(script) isn't inside its package \(root)") }
    for f in [runtime, script, root] { if let w = runPathProblem(f) { throw VaultError("not pinned: \(w)") } }
    let rb = try readFileBytes(runtime, max: 512 << 20)
    let (libs, lwhy) = dylibClosure(runtime)
    guard let l = libs else { throw VaultError("not pinned: \(lwhy ?? "the runtime's libraries can't be read")") }
    let (tree, why) = packageTreeHash(root)
    guard let t = tree else { throw VaultError("not pinned: \(why ?? "the package can't be read")") }
    let pin = ShopCLIPin(runtime: runtime, script: script, root: root, runtime_sha: sha256Hex(rb), tree_sha: t, set: isoTime(),
                         libs: l, auth_file: auth, confined: 1)
    oprint("About to pin Shop's CLI for the vault:\n  runtime  \(runtime) (and \(l.count) of its libraries)\n  CLI      \(script)\n  package  \(root)\n  sign-in  \(auth) (the only file of yours it may use)")
    let (key, _) = try unlockKey(paths)
    defer { key.wipeKey() }
    try updateVault(paths, key) { p in p.shop_cli = pin }
    audit(paths, ["op": "shop_cli", "result": "ok", "runtime": runtime, "script": script, "tree": String(t.prefix(16)), "libs": l.count], macKey: vaultAuditKey(paths, key))
    oprint("Pinned. The vault runs only this, confined: modules only from this package, no add-ons, a home of its own with only the sign-in file above, a fixed command and a clean environment. It refuses it once any of these files change (run this again after updating Node or the CLI).")
    return Exit.ok
}

/// `spend-resolve` (the user, in their own terminal): Shop completions whose outcome the vault couldn't record, which block
/// their entry until settled. No arguments: list them and say whether spend-pending.jsonl holds records the vault can't
/// verify (those block every purchase). `NAME [--extra AMOUNT]`: settle NAME's, recording AMOUNT (what Shop charged
/// beyond what was reserved, in its caps' currency; default 0) after you checked the order in Shop; its records in the
/// pending file go too. `--discard-pending`: remove spend-pending.jsonl after you checked it (nothing in it is imported).
func cmdSpendResolve(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: spend-resolve [NAME [--extra AMOUNT] | --discard-pending]   (your own terminal)"
    let a = try parseArgs(argv, values: ["extra"], flags: ["discard-pending"])
    guard a.positional.count <= 1, !(a.has("discard-pending") && !a.positional.isEmpty), a.last("extra") == nil || a.positional.count == 1 else {
        throw usageError(usage)
    }
    try requireTTY("spend-resolve")
    installPromptSignalHandlers()
    let (key, p) = try unlockKey(paths)
    defer { key.wipeKey() }
    let journal = paths.dir + "/spend-pending.jsonl"
    let names = Dictionary(p.secrets.map { (($0.value.id ?? $0.key), $0.key) }, uniquingKeysWith: { a, _ in a })
    if a.positional.isEmpty && !a.has("discard-pending") {
        let open = p.unresolved ?? []
        if open.isEmpty { oprint("No Shop completion is waiting to be settled.") }
        for m in open {
            oprint("  \(names[m.card] ?? "(a removed entry)"): a Shop completion reserved \(formatMoney(m.amount, m.currency)) at \(m.origin), \(isoTime(Date(timeIntervalSince1970: m.t))); what Shop charged isn't recorded")
        }
        if fileExists(journal) { oprint("spend-pending.jsonl is there: the unlocked vault records what in it it can verify; anything else blocks every purchase until you run `spend-resolve --discard-pending`.") }
        return Exit.ok
    }
    if a.has("discard-pending") {
        guard fileExists(journal) else { oprint("No spend-pending.jsonl."); return Exit.ok }
        scrubFile(journal)
        unlink(journal)
        audit(paths, ["op": "spend_resolve", "result": "ok", "discarded_pending": true], macKey: vaultAuditKey(paths, key))
        oprint("Removed spend-pending.jsonl. Completions still waiting to be settled (if any) block their entry until `spend-resolve NAME`.")
        return Exit.ok
    }
    let name = a.positional[0]
    guard let s = p.secrets[name] else { throw VaultError("no secret named \(name)") }
    let id = s.id ?? name
    let mine = (p.unresolved ?? []).filter { $0.card == id }
    guard !mine.isEmpty else { oprint("Nothing of \(name)'s is waiting to be settled."); return Exit.ok }
    let cur = s.caps?.currency ?? mine[0].currency
    var extra = 0
    if let x = a.last("extra") {
        guard let v = parseMajorAmount(x, currency: cur), v >= 0, v <= 1_000_000_000 else { throw usageError("--extra: an amount in \(cur), 0 or more") }
        extra = v
    }
    let now = Date().timeIntervalSince1970
    let tags = Set(mine.map { $0.purchase })
    try updateVault(paths, key) { q in
        q.unresolved = (q.unresolved ?? []).filter { $0.card != id }
        if extra > 0 {
            q.spend = (q.spend ?? []).filter { $0.t >= now - Double(maxCapPeriod) }
                + [SpendEntry(card: id, origin: mine[0].origin, amount: extra, currency: cur, t: now, purchase: "resolved:" + (mine[0].purchase))]
        }
    }
    // Its records in the pending file are settled now: they go; any other line stays.
    if let b = try? readFileBytes(journal, max: 1 << 20) {
        let keep = String(decoding: b, as: UTF8.self).split(separator: "\n").filter { l in
            guard let r = try? JSONDecoder().decode(PendingSpendRecord.self, from: Data(l.utf8)) else { return true }
            return !tags.contains(r.purchase)
        }
        if keep.isEmpty { unlink(journal) } else { try? writeFileAtomic(journal, Array((keep.joined(separator: "\n") + "\n").utf8)) }
    }
    audit(paths, ["op": "spend_resolve", "result": "ok", "name": name, "label": s.card_label ?? name, "settled": mine.count,
                  "extra": formatMoney(extra, cur), "event": "payment.resolved"], macKey: vaultAuditKey(paths, key))
    oprint("Settled \(mine.count) Shop completion(s) of \(name)" + (extra > 0 ? ", with \(formatMoney(extra, cur)) recorded beyond what was reserved." : ", with nothing beyond what was reserved."))
    return Exit.ok
}

/// What saving or changing a card's policy says (to the user, at their terminal).
func payPolicyWords(_ name: String, _ s: Secret?, saving: String) -> String {
    guard let s else { return "\(saving) \(name)." }
    let what = s.kind == "shop" ? "purchases through your Shop account" : "card \"\(s.card_label ?? name)\""
    var where_ = s.origins.isEmpty ? "" : " (only at \(s.origins.joined(separator: ", ")))"
    if s.single_use == true { where_ += ", single-use" }
    if s.merchant_locked == true { where_ += ", merchant-locked" }
    if let fr = s.allowed_frames, !fr.isEmpty { where_ += ", payment forms allowed without a question: \(fr.joined(separator: ", "))" }
    let policy = s.pay == "agent"
        ? "an agent card: purchases within its caps don't ask you" + ((s.caps?.isEmpty ?? true) ? " (it has no caps: its own limit at your bank is the only one)" : "")
        : "every purchase asks you first (Touch ID or your typed confirmation)"
    return "\(saving) \(name), \(what)\(where_): \(policy); caps: \(s.caps?.words ?? "none")."
}

let virtualCardAdvice = """
Tip: use a virtual card for your agent, one you can cap and freeze at your bank or card provider (a virtual or
single-use card number). Once a number is typed into a page in the VM, a program running there could read it; the card's
own limit is the real backstop. See docs/payments.md.
"""

// MARK: - selftest

/// The payment parts of `selftest`: card checks, amounts on pages, merchants' agent rules, RFC 9421's Ed25519 example (Web
/// Bot Auth's signatures) and an AP2 mandate's form.
func paymentSelftest(_ check: (String, Bool) -> Void) {
    check("card number: Luhn, and it starts like a card (2-6)", cardNumber("4242 4242 4242 4242") == "4242424242424242" && cardNumber("4242-4242-4242-4241") == nil
          && cardNumber("4242") == nil && cardNumber("4242424242424242x") == nil && cardNumber("2223003122003222") != nil && cardNumber("1234567812345670") == nil)
    let ref = Date(timeIntervalSince1970: 1_800_000_000)          // January 2027
    let e1 = cardExpiry("11/30", now: ref), e2 = cardExpiry("1/2031", now: ref)
    check("card expiry", e1?.0 == "11" && e1?.1 == "2030" && e2?.0 == "01" && e2?.1 == "2031" && cardExpiry("12/26", now: ref) == nil
          && cardExpiry("13/30", now: ref) == nil && cardExpiry("01/27", now: ref) != nil)
    let totals: [(String, String?, Int?, String?)] = [
        ("Total $12.34", nil, 1234, "USD"), ("Total: €1.234,56", nil, 123456, "EUR"), ("Subtotal $10.00 Tax $0.80 Total $10.80", nil, 1080, "USD"),
        ("Order total 12.34", "USD", 1234, nil), ("2 items", "USD", nil, nil), ("$10.00 or $12.00", nil, nil, nil), ("Total ¥1,200", nil, 1200, "JPY"),
        ("Total CA$5.00", "USD", 500, "CAD"), ("Total USD 1,000", nil, 100000, "USD"), ("Total $7.50", "CAD", 750, "CAD"), ("Total £3", nil, 300, "GBP"),
        ("Total 1.234", "USD", nil, nil), ("Grand total: 42.00 EUR", nil, 4200, "EUR"), ("Total $100.00 Total savings $1.00", nil, 10000, "USD"), ("Sub-total $90.00 Total $100.00", nil, 10000, "USD"),
        ("Sub total $90.00", nil, nil, nil), ("Sub\u{2011}total $90.00", nil, nil, nil), ("Total $5.00 Total $5.00", nil, nil, nil), ("Items total $9.00 Total $9.50", nil, 950, "USD"),
        ("Total BHD 4.00", "USD", nil, nil), ("Total $0.00", nil, nil, nil), ("Total $5.00 ... Total $6.00", nil, nil, nil), ("Total VAT 12.00", nil, nil, nil)]
    for (text, cur, want, wantCur) in totals {
        let (m, _) = parsePageTotal([text], cardCurrency: cur)
        check("page total '\(text)'", m?.minor == want && m?.currency == wantCur)
    }
    check("caps amounts", parseMajorAmount("50", currency: "USD") == 5000 && parseMajorAmount("49.9", currency: "USD") == 4990
          && parseMajorAmount("1200", currency: "JPY") == 1200 && parseMajorAmount("1.5", currency: "JPY") == nil && parseMajorAmount("-1", currency: "USD") == nil
          && parseCapPeriod("200/30d", currency: "USD").map { $0.0 == 20000 && $0.1 == 30 * 86400 } == true)
    let shopify = parseAgentsMD("## Checkout\nDo not work around the tools by *scripting* checkout forms or page controls.\n"
                                + "If checkout tools are unavailable, ask the buyer to continue on the checkout page. Do not fall back to scripted form fills.\nPowered by Shopify.")
    check("agents.md: a ban on scripted checkout", shopify.forbids == ["no-scripted-checkout"] && shopify.buyerContinues && shopify.shopify)
    check("agents.md: a ban on entering card details", parseAgentsMD("Agents must never enter card details for a buyer.").forbids == ["no-card-entry"])
    check("agents.md: nothing against it", parseAgentsMD("# Agents\nYou may browse and compare. Checkout is open to agents acting for a buyer.").forbids.isEmpty)
    check("UCP profile: handlers and AP2", parseUCPProfile(Array(#"{"payment":{"handlers":[{"id":"dev.shopify.shop_pay"},{"id":"com.google.pay"}]},"capabilities":["dev.ucp.shopping.ap2_mandate"]}"#.utf8))
          .map { $0.handlers == ["com.google.pay", "dev.shopify.shop_pay"] && $0.ap2 && $0.shopify } == true)
    // RFC 9421, B.2.6: the Ed25519 example (test-key-ed25519), which Web Bot Auth's signatures use.
    let der = Array(Data(base64Encoded: "MC4CAQAwBQYDK2VwBCIEIJ+DYvh6SEqVTm50DFtMDoQikTmiCqirVv9mWG9qfSnF") ?? Data())
    let base = signatureBase([("date", "Tue, 20 Apr 2021 02:07:55 GMT"), ("@method", "POST"), ("@path", "/foo"), ("@authority", "example.com"),
                              ("content-type", "application/json"), ("content-length", "18")],
                             params: "(\"date\" \"@method\" \"@path\" \"@authority\" \"content-type\" \"content-length\");created=1618884473;keyid=\"test-key-ed25519\"")
    // (CryptoKit's Ed25519 signatures are randomized, so the RFC's signature is checked with the key, not compared.)
    let rfcKey = der.count == 48 ? try? Curve25519.Signing.PrivateKey(rawRepresentation: der.suffix(32)) : nil
    let rfcSig = Data(base64Encoded: "wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw==") ?? Data()
    let mine = rfcKey.flatMap { try? $0.signature(for: Array(base.utf8)) } ?? Data()
    check("RFC 9421 B.2.6 Ed25519 signature base and key", rfcKey.map { $0.publicKey.isValidSignature(rfcSig, for: Array(base.utf8))
          && $0.publicKey.isValidSignature(mine, for: Array(base.utf8)) && !$0.publicKey.isValidSignature(rfcSig, for: Array((base + " ").utf8)) } == true)
    check("JWK thumbprint (RFC 8037 appendix A.3)", ed25519Thumbprint(base64urlDecode("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo") ?? [])
          == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k")
    // An AP2 checkout mandate: its signature checks with the key, and its disclosures hash to what the JWT names.
    let k = P256.Signing.PrivateKey()
    let jwt = "eyJhbGciOiJFUzI1NiJ9." + base64url(Array(#"{"id":"co-1","merchant":{"id":"m1","name":"Demo","website":"https://demo.example"},"totals":[{"type":"total","amount":19900}],"currency":"USD"}"#.utf8)) + ".c2ln"
    let (claim, _) = parseCheckoutJWT(jwt)
    var formOK = false
    if let c = claim, c.minor == 19900, c.currency == "USD", c.website == "https://demo.example",
       let (cm, pm, hash, _) = ap2ClosedMandates(checkoutJWT: jwt, claim: c, payeeID: "m1", payeeName: "Demo", website: "https://demo.example",
                                                  instrumentID: "card-x", instrumentLabel: "Test", key: k, ttl: 600) {
        let parts = cm.split(separator: "~").map(String.init)
        let jws = parts[0].split(separator: ".").map(String.init)
        let sigOK = jws.count == 3 && (base64urlDecode(jws[2]).flatMap { try? P256.Signing.ECDSASignature(rawRepresentation: $0) })
            .map { k.publicKey.isValidSignature($0, for: Array((jws[0] + "." + jws[1]).utf8)) } == true
        let payload = base64urlDecode(jws.count == 3 ? jws[1] : "").flatMap { parseJSONObject($0) }
        let named = ((payload?["delegate_payload"] as? [Any])?.first as? [String: Any])?["..."] as? String
        let digests = parts.dropFirst().map { base64url(Array(SHA256.hash(data: Array($0.utf8)))) }
        formOK = sigOK && named != nil && digests.contains(named!) && cm.hasSuffix("~") && pm.hasSuffix("~")
            && hash == base64url(Array(SHA256.hash(data: Array(jwt.utf8))))
    }
    check("AP2 closed mandates: ES256 signature, disclosures, checkout hash", formOK)
    let none = base64url(Array(#"{"alg":"none"}"#.utf8)) + "." + jwt.split(separator: ".")[1] + ".c2ln"
    let twice = "eyJhbGciOiJFUzI1NiJ9." + base64url(Array(#"{"id":"co-1","currency":"USD","currency":"EUR","merchant":{"id":"m1","name":"Demo","website":"https://demo.example"},"totals":[{"type":"total","amount":100}]}"#.utf8)) + ".c2ln"
    let two = "eyJhbGciOiJFUzI1NiJ9." + base64url(Array(#"{"id":"co-1","currency":"USD","merchant":{"id":"m1","name":"Demo","website":"https://demo.example"},"totals":[{"type":"total","amount":100},{"type":"total","amount":1}]}"#.utf8)) + ".c2ln"
    let anon = "eyJhbGciOiJFUzI1NiJ9." + base64url(Array(#"{"id":"co-1","currency":"USD","totals":[{"type":"total","amount":100}]}"#.utf8)) + ".c2ln"
    check("AP2 checkouts refused: alg none, a field twice, two totals, no merchant",
          parseCheckoutJWT(none).0 == nil && parseCheckoutJWT(twice).0 == nil && parseCheckoutJWT(two).0 == nil && parseCheckoutJWT(anon).0 == nil)
    func co(_ json: String) -> String { "eyJhbGciOiJFUzI1NiJ9." + base64url(Array(json.utf8)) + ".c2ln" }
    let coBase = #""id":"co-1","currency":"USD","totals":[{"type":"total","amount":100}]"#
    let nestedTwice = co(#"{"#+coBase+#","merchant":{"id":"m1","name":"Demo","website":"https://demo.example","website":"https://evil.example"}}"#)
    let totalTwice = co(#"{"id":"co-1","currency":"USD","merchant":{"id":"m1","name":"Demo","website":"https://demo.example"},"totals":[{"type":"total","amount":100,"amount":1}]}"#)
    let expired = co(#"{"#+coBase+#","exp":1000,"merchant":{"id":"m1","name":"Demo","website":"https://demo.example"}}"#)
    let other = co(#"{"#+coBase+#","aud":"someone-else","merchant":{"id":"m1","name":"Demo","website":"https://demo.example"}}"#)
    let forMe = co(#"{"#+coBase+#","aud":["kid-1"],"exp":4102444800,"merchant":{"id":"m1","name":"Demo","website":"https://demo.example"}}"#)
    let big1 = parseLexJSON(Array(#"{"q": 18446744073709551616, "f": 1.10}"#.utf8)), big2 = parseLexJSON(Array(#"{"f":1.10,"q":18446744073709551617}"#.utf8))
    let mixed = co(#"{"id":"co-1","currency":"USD","merchant":{"id":"m1","name":"Demo","website":"https://demo.example"},"totals":[{"type":"total","amount":1000,"currency":"EUR"}]}"#)
    check("AP2 checkouts refused: the payable total names another currency (EUR) than the checkout (USD)", parseCheckoutJWT(mixed).0 == nil)
    func v6(_ s: String) -> [UInt8] { var a = in6_addr(); _ = inet_pton(AF_INET6, s, &a); return withUnsafeBytes(of: a) { Array($0) } }
    let ownV4: Set<[UInt8]> = [[93, 184, 215, 14]]
    check("proxy addresses: an IPv4 address written as IPv6 (mapped, NAT64, compatible) is checked as IPv4, and this Mac's own in any form is refused",
          canonicalAddress(v6("::ffff:93.184.215.14")) == [93, 184, 215, 14] && !proxyDestinationAllowed(v6("::ffff:93.184.215.14"), own: ownV4)
          && !proxyDestinationAllowed([93, 184, 215, 14], own: ownV4) && !proxyDestinationAllowed(v6("::ffff:10.0.0.1"), own: [])
          && !proxyDestinationAllowed(v6("64:ff9b::a00:1"), own: []) && !proxyDestinationAllowed(v6("::127.0.0.1"), own: [])
          && !proxyDestinationAllowed(v6("::ffff:127.0.0.1"), own: []) && !proxyDestinationAllowed(v6("::1"), own: [])
          && !proxyDestinationAllowed(v6("fe80::1"), own: []) && !proxyDestinationAllowed(v6("fd00::1"), own: [])
          && proxyDestinationAllowed(v6("::ffff:8.8.8.8"), own: []) && proxyDestinationAllowed([8, 8, 8, 8], own: [])
          && proxyDestinationAllowed(v6("2606:4700::1"), own: []) && !proxyDestinationAllowed(v6("2606:4700::1"), own: [v6("2606:4700::1")]))
    #if MCOMP_VAULT_TESTING
    // A worker closes its socket while stop() runs, and a new socket takes its number: stop() leaves that one alone.
    var reused = false, pairsWork = false
    if let px = ShopProxy() {
        px.start()
        let cl = socket(AF_INET, SOCK_STREAM, 0)
        var a = sockaddr_in()
        a.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        a.sin_family = sa_family_t(AF_INET)
        a.sin_port = px.port.bigEndian
        a.sin_addr.s_addr = inet_addr("127.0.0.1")
        _ = withUnsafePointer(to: &a) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(cl, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        var tracked: [Int32] = []
        for _ in 0..<200 where tracked.isEmpty { tracked = px.testLiveFDs; if tracked.isEmpty { usleep(10_000) } }
        var pairs: [[Int32]] = []
        px.testBetween = {
            close(cl)                                          // the client goes; its worker closes its own socket
            for _ in 0..<300 where !px.testLiveFDs.isEmpty { usleep(10_000) }
            for _ in 0..<4 {
                var sv: [Int32] = [-1, -1]
                if socketpair(AF_UNIX, SOCK_STREAM, 0, &sv) == 0 {
                    var one: Int32 = 1
                    for f in sv { setsockopt(f, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size)) }
                    pairs.append(sv)
                }
            }
        }
        px.stop()
        reused = !tracked.isEmpty && pairs.contains { $0.contains(where: tracked.contains) }
        pairsWork = !pairs.isEmpty && pairs.allSatisfy { p in var b: UInt8 = 7; return write(p[0], &b, 1) == 1 && read(p[1], &b, 1) == 1 }
        for p in pairs { close(p[0]); close(p[1]) }
    }
    check("proxy shutdown: a socket a worker closed while stop() ran, its number now another socket's, isn't shut (sockets are held as objects, each closed once)",
          reused && pairsWork)
    #endif
    check("exact JSON: numbers by their text (two integers past 64 bits differ), keys in order, a key twice refused",
          big1.map(lexCanonical) == #"{"f":1.10,"q":18446744073709551616}"# && big2.map(lexCanonical) != big1.map(lexCanonical)
          && parseLexJSON(Array(#"{"a":1,"a":2}"#.utf8)) == nil && parseLexJSON(Array("[1,]".utf8)) == nil)
    check("AP2 checkouts refused: a field twice deeper in (merchant.website, a total's amount), expired, addressed to another key; "
          + "one addressed to this key and not expired is read", parseCheckoutJWT(nestedTwice).0 == nil && parseCheckoutJWT(totalTwice).0 == nil
          && parseCheckoutJWT(expired).0 == nil && parseCheckoutJWT(other, audience: "kid-1").0 == nil && parseCheckoutJWT(forMe, audience: "kid-1").0 != nil
          && jsonKeysUnique(Array(#"{"a":{"b":1},"c":[{"b":2},{"b":3}]}"#.utf8)) == true)
}
