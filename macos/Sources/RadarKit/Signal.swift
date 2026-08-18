//
//  Signal.swift
//  RadarKit
//
//  The Signal contract (PRD 5.7) as a surface sees it. Decode only.
//
//  SIG-5 is enforced structurally, not by convention:
//    - unknown JSON fields are ignored, and recorded in `unknownFields`;
//    - unknown enum values survive as their raw string (see OpenEnum);
//    - decoding never throws and never traps, whatever the payload contains.
//      Every initializer below is total: a missing, null or wrongly typed
//      field degrades to a default, it does not fail the signal.
//
//  Decode only, on purpose. The offline snapshot (MAC-8) stores the original
//  payload JSON verbatim, so nothing here has to encode, and fields this
//  client does not know about survive a restart untouched.
//
//  No business logic lives here (MAC-2, SUR-2): no filtering, no ordering,
//  no scoring, no derived judgement. Parsing and nothing else.
//

import Foundation

// MARK: - Open enums

/// A closed Swift enum would throw on a value the core learns to emit later,
/// and SIG-5 forbids that. These behave like enums for known values and like
/// strings for everything else.
public protocol OpenEnum: Hashable, Sendable, Decodable, CustomStringConvertible {
    var rawValue: String { get }
    init(openRawValue: String)
    static var knownValues: [String] { get }
}

extension OpenEnum {
    public init(from decoder: any Decoder) throws {
        self.init(openRawValue: Self.scalarString(from: decoder))
    }

    /// Accepts any JSON scalar. A number where a string was promised is still
    /// something we can show, and showing it beats dropping the signal.
    static func scalarString(from decoder: any Decoder) -> String {
        guard let c = try? decoder.singleValueContainer() else { return "" }
        if let v = try? c.decode(String.self) { return v }
        if let v = try? c.decode(Int.self) { return String(v) }
        if let v = try? c.decode(Double.self) { return String(v) }
        if let v = try? c.decode(Bool.self) { return v ? "true" : "false" }
        return ""
    }

    /// False means the core is ahead of this build. The UI shows the raw value.
    public var isKnown: Bool { Self.knownValues.contains(rawValue) }
    public var description: String { rawValue }
}

public struct ChangeType: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let release = ChangeType(openRawValue: "release")
    public static let breakingChange = ChangeType(openRawValue: "breaking_change")
    public static let deprecation = ChangeType(openRawValue: "deprecation")
    public static let pricing = ChangeType(openRawValue: "pricing")
    public static let limits = ChangeType(openRawValue: "limits")
    public static let security = ChangeType(openRawValue: "security")
    public static let other = ChangeType(openRawValue: "other")

    public static let knownValues: [String] = [
        release, breakingChange, deprecation, pricing, limits, security, other,
    ].map(\.rawValue)
}

public struct FactKind: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let version = FactKind(openRawValue: "version")
    public static let effectiveDate = FactKind(openRawValue: "effective_date")
    public static let sunsetDate = FactKind(openRawValue: "sunset_date")
    public static let price = FactKind(openRawValue: "price")
    public static let limit = FactKind(openRawValue: "limit")
    public static let affectedProduct = FactKind(openRawValue: "affected_product")

    public static let knownValues: [String] = [
        version, effectiveDate, sunsetDate, price, limit, affectedProduct,
    ].map(\.rawValue)

    /// Kinds whose `value` is expected to carry a calendar date. Used only to
    /// decide whether parsing `value` as a date is worth attempting.
    public static let dateKinds: [FactKind] = [effectiveDate, sunsetDate]
}

public struct DeltaStatus: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let new = DeltaStatus(openRawValue: "new")
    public static let continuing = DeltaStatus(openRawValue: "continuing")
    public static let updated = DeltaStatus(openRawValue: "updated")
    public static let resolved = DeltaStatus(openRawValue: "resolved")

    public static let knownValues: [String] = [new, continuing, updated, resolved].map(\.rawValue)
}

public struct ContextLabel: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let notFoundInCorpus = ContextLabel(openRawValue: "not_found_in_corpus")
    public static let recurring = ContextLabel(openRawValue: "recurring")
    public static let trendMember = ContextLabel(openRawValue: "trend_member")
    public static let escalation = ContextLabel(openRawValue: "escalation")

    public static let knownValues: [String] = [
        notFoundInCorpus, recurring, trendMember, escalation,
    ].map(\.rawValue)
}

public struct Trajectory: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let emerging = Trajectory(openRawValue: "emerging")
    public static let steady = Trajectory(openRawValue: "steady")
    public static let accelerating = Trajectory(openRawValue: "accelerating")
    public static let dormant = Trajectory(openRawValue: "dormant")
    public static let closed = Trajectory(openRawValue: "closed")

    public static let knownValues: [String] = [
        emerging, steady, accelerating, dormant, closed,
    ].map(\.rawValue)
}

public struct SignalKind: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let digestItem = SignalKind(openRawValue: "digest_item")
    public static let quietDay = SignalKind(openRawValue: "quiet_day")
    public static let runFailure = SignalKind(openRawValue: "run_failure")

    public static let knownValues: [String] = [digestItem, quietDay, runFailure].map(\.rawValue)
}

public struct Tier: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let lead = Tier(openRawValue: "lead")
    public static let standard = Tier(openRawValue: "standard")
    public static let background = Tier(openRawValue: "background")

    public static let knownValues: [String] = [lead, standard, background].map(\.rawValue)
}

public struct DatePrecision: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let day = DatePrecision(openRawValue: "day")
    public static let month = DatePrecision(openRawValue: "month")
    public static let year = DatePrecision(openRawValue: "year")
    public static let inferred = DatePrecision(openRawValue: "inferred")

    public static let knownValues: [String] = [day, month, year, inferred].map(\.rawValue)
}

public struct Confidence: OpenEnum {
    public let rawValue: String
    public init(openRawValue: String) { rawValue = openRawValue }

    public static let high = Confidence(openRawValue: "high")
    public static let medium = Confidence(openRawValue: "medium")
    public static let low = Confidence(openRawValue: "low")

    public static let knownValues: [String] = [high, medium, low].map(\.rawValue)
}

// MARK: - Calendar day

/// A date without a time zone. `for_date` and `event_date` are calendar days in
/// the source, and routing them through `Date` is how "15 October" becomes
/// "14 October" for a reader west of UTC.
public struct CalendarDay: Hashable, Sendable, Comparable, CustomStringConvertible {
    public let year: Int
    public let month: Int
    public let day: Int

    public init(year: Int, month: Int, day: Int) {
        self.year = year
        self.month = month
        self.day = day
    }

    /// Accepts "2026-10-15" and the date half of "2026-10-15T09:00:00Z".
    /// Returns nil rather than guessing; callers show the raw string instead.
    public init?(iso: String) {
        let head = iso.prefix { $0 != "T" && $0 != " " }
        let parts = head.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3,
              let y = Int(parts[0]), let m = Int(parts[1]), let d = Int(parts[2]),
              (1...12).contains(m), (1...31).contains(d)
        else { return nil }
        self.init(year: y, month: m, day: d)
    }

    public var description: String { String(format: "%04d-%02d-%02d", year, month, day) }

    /// Julian day number. Calendar arithmetic without a Calendar, so "in 59
    /// days" cannot drift with the user's time zone or locale.
    public var julianDayNumber: Int {
        let a = (14 - month) / 12
        let y = year + 4800 - a
        let m = month + 12 * a - 3
        return day + (153 * m + 2) / 5 + 365 * y + y / 4 - y / 100 + y / 400 - 32045
    }

    public func days(since other: CalendarDay) -> Int { julianDayNumber - other.julianDayNumber }

    public static func today(_ now: Date = Date(), in timeZone: TimeZone = .current) -> CalendarDay {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        let c = cal.dateComponents([.year, .month, .day], from: now)
        return CalendarDay(year: c.year ?? 1970, month: c.month ?? 1, day: c.day ?? 1)
    }

    public static func < (lhs: CalendarDay, rhs: CalendarDay) -> Bool {
        (lhs.year, lhs.month, lhs.day) < (rhs.year, rhs.month, rhs.day)
    }
}

// MARK: - Timestamp parsing

enum RadarTime {
    /// Hand rolled so it cannot throw, cannot depend on a locale, and does not
    /// need a non-Sendable formatter cached in global state.
    /// A timestamp without an offset is read as UTC: the contract does not say
    /// which zone the core writes, and this assumption is written down rather
    /// than hidden.
    static func parse(_ text: String) -> Date? {
        let s = Array(text.utf8)
        func digits(_ from: Int, _ count: Int) -> Int? {
            guard from + count <= s.count else { return nil }
            var acc = 0
            for i in from..<(from + count) {
                let c = s[i]
                guard c >= 48, c <= 57 else { return nil }
                acc = acc * 10 + Int(c - 48)
            }
            return acc
        }
        guard let year = digits(0, 4), let month = digits(5, 2), let day = digits(8, 2) else {
            return nil
        }
        var hour = 0, minute = 0, second = 0, offsetSeconds = 0
        if s.count >= 19, s[10] == 84 || s[10] == 32 {  // 'T' or ' '
            hour = digits(11, 2) ?? 0
            minute = digits(14, 2) ?? 0
            second = digits(17, 2) ?? 0
            var i = 19
            if i < s.count, s[i] == 46 {  // fractional seconds, discarded
                i += 1
                while i < s.count, s[i] >= 48, s[i] <= 57 { i += 1 }
            }
            if i < s.count, s[i] == 43 || s[i] == 45 {  // '+' or '-'
                let sign = s[i] == 43 ? 1 : -1
                let oh = digits(i + 1, 2) ?? 0
                let om = digits(i + 4, 2) ?? digits(i + 3, 2) ?? 0
                offsetSeconds = sign * (oh * 3600 + om * 60)
            }
        }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(secondsFromGMT: offsetSeconds) ?? TimeZone(identifier: "UTC")!
        var c = DateComponents()
        c.year = year; c.month = month; c.day = day
        c.hour = hour; c.minute = minute; c.second = second
        return cal.date(from: c)
    }
}

// MARK: - Lenient decoding

private struct AnyKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil
    init(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

/// Consumes one element of an unkeyed container without inspecting it, so a
/// malformed array item can be skipped instead of stalling the loop.
private struct SkipOne: Decodable {
    init(from decoder: any Decoder) throws {}
}

/// Every accessor answers with a value or a default. Nothing here throws.
struct Lenient<K: CodingKey> {
    private let container: KeyedDecodingContainer<K>?

    init(_ decoder: any Decoder, keyedBy: K.Type) {
        container = try? decoder.container(keyedBy: K.self)
    }

    /// False when the payload is not a JSON object. List elements that are not
    /// objects are dropped rather than turned into blank rows on screen.
    var isObject: Bool { container != nil }

    func notAnObject(_ decoder: any Decoder) -> DecodingError {
        DecodingError.typeMismatch(
            [String: Any].self,
            DecodingError.Context(
                codingPath: decoder.codingPath, debugDescription: "expected a JSON object"))
    }

    func string(_ key: K) -> String? {
        guard let container else { return nil }
        if let v = try? container.decodeIfPresent(String.self, forKey: key) { return v }
        if let v = try? container.decodeIfPresent(Int.self, forKey: key) { return String(v) }
        if let v = try? container.decodeIfPresent(Double.self, forKey: key) { return String(v) }
        if let v = try? container.decodeIfPresent(Bool.self, forKey: key) {
            return v ? "true" : "false"
        }
        return nil
    }

    func string(_ key: K, default fallback: String) -> String { string(key) ?? fallback }

    func int(_ key: K, default fallback: Int) -> Int {
        guard let container else { return fallback }
        if let v = try? container.decodeIfPresent(Int.self, forKey: key) { return v }
        if let v = try? container.decodeIfPresent(Double.self, forKey: key) { return Int(v) }
        if let v = try? container.decodeIfPresent(String.self, forKey: key), let i = Int(v) {
            return i
        }
        return fallback
    }

    func double(_ key: K, default fallback: Double) -> Double {
        guard let container else { return fallback }
        if let v = try? container.decodeIfPresent(Double.self, forKey: key) { return v }
        if let v = try? container.decodeIfPresent(Int.self, forKey: key) { return Double(v) }
        if let v = try? container.decodeIfPresent(String.self, forKey: key), let d = Double(v) {
            return d
        }
        return fallback
    }

    func bool(_ key: K, default fallback: Bool) -> Bool {
        guard let container else { return fallback }
        if let v = try? container.decodeIfPresent(Bool.self, forKey: key) { return v }
        if let v = try? container.decodeIfPresent(Int.self, forKey: key) { return v != 0 }
        if let v = try? container.decodeIfPresent(String.self, forKey: key) {
            return ["true", "1", "yes"].contains(v.lowercased())
        }
        return fallback
    }

    func value<T: Decodable>(_ key: K, as type: T.Type) -> T? {
        guard let container else { return nil }
        return try? container.decodeIfPresent(T.self, forKey: key)
    }

    /// Element wise tolerant: one bad precedent must not cost the other four.
    func list<T: Decodable>(_ key: K, of type: T.Type) -> [T] {
        guard let container, var unkeyed = try? container.nestedUnkeyedContainer(forKey: key) else {
            return []
        }
        var out: [T] = []
        while !unkeyed.isAtEnd {
            let cursor = unkeyed.currentIndex
            if let item = try? unkeyed.decode(T.self) {
                out.append(item)
            } else {
                _ = try? unkeyed.decode(SkipOne.self)
            }
            if unkeyed.currentIndex == cursor { break }  // never spin on a stuck container
        }
        return out
    }

    func intMap(_ key: K) -> [String: Int] {
        guard let container,
              let nested = try? container.nestedContainer(keyedBy: AnyKey.self, forKey: key)
        else { return [:] }
        var out: [String: Int] = [:]
        for k in nested.allKeys {
            if let v = try? nested.decode(Int.self, forKey: k) {
                out[k.stringValue] = v
            } else if let v = try? nested.decode(Double.self, forKey: k) {
                out[k.stringValue] = Int(v)
            } else if let v = try? nested.decode(String.self, forKey: k), let i = Int(v) {
                out[k.stringValue] = i
            }
        }
        return out
    }

    func stringList(_ key: K) -> [String] {
        guard let container, var unkeyed = try? container.nestedUnkeyedContainer(forKey: key) else {
            return []
        }
        var out: [String] = []
        while !unkeyed.isAtEnd {
            let cursor = unkeyed.currentIndex
            if let v = try? unkeyed.decode(String.self) {
                out.append(v)
            } else {
                _ = try? unkeyed.decode(SkipOne.self)
            }
            if unkeyed.currentIndex == cursor { break }
        }
        return out
    }

    func intList(_ key: K) -> [Int] {
        guard let container, var unkeyed = try? container.nestedUnkeyedContainer(forKey: key) else {
            return []
        }
        var out: [Int] = []
        while !unkeyed.isAtEnd {
            let cursor = unkeyed.currentIndex
            if let v = try? unkeyed.decode(Int.self) {
                out.append(v)
            } else if let v = try? unkeyed.decode(Double.self) {
                out.append(Int(v))
            } else {
                _ = try? unkeyed.decode(SkipOne.self)
            }
            if unkeyed.currentIndex == cursor { break }
        }
        return out
    }

    /// Field names present in the payload that this build does not know about.
    static func unknownFields(_ decoder: any Decoder, known: [String]) -> [String] {
        guard let any = try? decoder.container(keyedBy: AnyKey.self) else { return [] }
        let knownSet = Set(known)
        return any.allKeys.map(\.stringValue).filter { !knownSet.contains($0) }.sorted()
    }
}

// MARK: - Fact

public struct Fact: Decodable, Hashable, Sendable {
    public let kind: FactKind
    public let value: String
    public let sourceURL: String
    /// Verbatim quote from the source. A fact without one is not published.
    public let evidence: String
    public let confidence: Confidence
    /// Set by the core, not by the model. False means the quote was not matched
    /// against the fetched source.
    public let evidenceVerified: Bool

    /// Best effort reading of `value` as a calendar day, for date kinds only.
    /// The contract does not promise a format for `value`, so this is nil
    /// whenever the string does not parse and the raw `value` is shown instead.
    /// See the contract gap list: a normalized date field belongs in the core.
    public let valueDay: CalendarDay?

    public let unknownFields: [String]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case kind, value, evidence, confidence
        case sourceURL = "source_url"
        case evidenceVerified = "evidence_verified"
    }

    public init(from decoder: any Decoder) throws {
        let r = Lenient(decoder, keyedBy: CodingKeys.self)
        guard r.isObject else { throw r.notAnObject(decoder) }
        kind = r.value(.kind, as: FactKind.self) ?? FactKind(openRawValue: "")
        value = r.string(.value, default: "")
        sourceURL = r.string(.sourceURL, default: "")
        evidence = r.string(.evidence, default: "")
        confidence = r.value(.confidence, as: Confidence.self) ?? .medium
        evidenceVerified = r.bool(.evidenceVerified, default: false)
        valueDay = FactKind.dateKinds.contains(kind) ? CalendarDay(iso: value) : nil
        unknownFields = Lenient<CodingKeys>.unknownFields(
            decoder, known: CodingKeys.allCases.map(\.rawValue))
    }
}


// MARK: - Precedent

/// Denormalized by contract (SIG-3): a signal renders whole without reaching
/// into any other store, so the client never queries the corpus.
public struct Precedent: Decodable, Hashable, Sendable {
    public let statementID: String
    public let text: String
    public let sourceURL: String
    public let eventDate: CalendarDay?
    /// Kept so an unparseable date is still shown rather than silently dropped.
    public let eventDateRaw: String?
    public let datePrecision: DatePrecision
    public let vendor: String
    public let changeType: ChangeType
    public let unknownFields: [String]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case text, vendor
        case statementID = "statement_id"
        case sourceURL = "source_url"
        case eventDate = "event_date"
        case datePrecision = "date_precision"
        case changeType = "change_type"
    }

    public init(from decoder: any Decoder) throws {
        let r = Lenient(decoder, keyedBy: CodingKeys.self)
        guard r.isObject else { throw r.notAnObject(decoder) }
        statementID = r.string(.statementID, default: "")
        text = r.string(.text, default: "")
        sourceURL = r.string(.sourceURL, default: "")
        eventDateRaw = r.string(.eventDate)
        eventDate = eventDateRaw.flatMap(CalendarDay.init(iso:))
        datePrecision = r.value(.datePrecision, as: DatePrecision.self) ?? .day
        vendor = r.string(.vendor, default: "")
        changeType = r.value(.changeType, as: ChangeType.self) ?? ChangeType(openRawValue: "")
        unknownFields = Lenient<CodingKeys>.unknownFields(
            decoder, known: CodingKeys.allCases.map(\.rawValue))
    }
}


// MARK: - Retrieval report

public struct RetrievalReport: Decodable, Hashable, Sendable {
    public let strictHits: Int
    public let relaxedHits: Int
    public let totalFound: Int
    public let shown: Int
    public let windowsDays: [Int]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case shown
        case strictHits = "strict_hits"
        case relaxedHits = "relaxed_hits"
        case totalFound = "total_found"
        case windowsDays = "windows_days"
    }

    public init(from decoder: any Decoder) throws {
        let r = Lenient(decoder, keyedBy: CodingKeys.self)
        strictHits = r.int(.strictHits, default: 0)
        relaxedHits = r.int(.relaxedHits, default: 0)
        totalFound = r.int(.totalFound, default: 0)
        shown = r.int(.shown, default: 0)
        windowsDays = r.intList(.windowsDays)
    }
}

// MARK: - Run summary

/// Facts about the run, carried on every signal because a surface may not read
/// the run tables (SUR-1). The footer naming unreachable sources is built from
/// this and nothing else.
public struct RunSummary: Decodable, Hashable, Sendable {
    public let sourcesChecked: Int
    public let sourcesFailed: [String]
    /// Reached, answered, and yielded nothing extractable. A different fault
    /// from unreachable, and named separately so the footer can say which.
    public let sourcesEmpty: [String]
    public let materialsCollected: Int
    public let materialsFiltered: Int
    public let lastSuccessDate: CalendarDay?
    public let costUSD: Double

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case sourcesChecked = "sources_checked"
        case sourcesFailed = "sources_failed"
        case sourcesEmpty = "sources_empty"
        case materialsCollected = "materials_collected"
        case materialsFiltered = "materials_filtered"
        case lastSuccessDate = "last_success_date"
        case costUSD = "cost_usd"
    }

    public init(from decoder: any Decoder) throws {
        let r = Lenient(decoder, keyedBy: CodingKeys.self)
        guard r.isObject else { throw r.notAnObject(decoder) }
        sourcesChecked = r.int(.sourcesChecked, default: 0)
        sourcesFailed = r.stringList(.sourcesFailed)
        sourcesEmpty = r.stringList(.sourcesEmpty)
        materialsCollected = r.int(.materialsCollected, default: 0)
        materialsFiltered = r.int(.materialsFiltered, default: 0)
        lastSuccessDate = r.string(.lastSuccessDate).flatMap(CalendarDay.init(iso:))
        costUSD = r.double(.costUSD, default: 0)
    }
}

// MARK: - Upcoming deadline

/// A dated obligation the core already extracted, shown when the day is quiet
/// (MAC-5). Every field comes from a verified fact, not from a fresh inference.
public struct UpcomingDeadline: Decodable, Hashable, Sendable, Identifiable {
    public var id: String { "\(whenRaw ?? "")|\(what)" }

    public let when: CalendarDay?
    public let whenRaw: String?
    public let what: String
    public let vendor: String?
    public let sourceURL: String?
    public let datePrecision: DatePrecision

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case when, what, vendor
        case sourceURL = "source_url"
        case datePrecision = "date_precision"
    }

    public init(from decoder: any Decoder) throws {
        let r = Lenient(decoder, keyedBy: CodingKeys.self)
        guard r.isObject else { throw r.notAnObject(decoder) }
        whenRaw = r.string(.when)
        when = whenRaw.flatMap(CalendarDay.init(iso:))
        what = r.string(.what, default: "")
        vendor = r.string(.vendor)
        sourceURL = r.string(.sourceURL)
        datePrecision = r.value(.datePrecision, as: DatePrecision.self) ?? .day
    }
}

// MARK: - Signal

public struct Signal: Decodable, Hashable, Sendable, Identifiable {
    public var id: String { signalID }

    public let schemaVersion: Int
    public let signalID: String
    public let runID: String
    public let signalType: SignalKind
    public let createdAt: Date?
    public let createdAtRaw: String?
    public let forDate: CalendarDay?
    public let forDateRaw: String?

    public let headline: String
    public let summary: String
    public let whyItMatters: String
    public let changeType: ChangeType?
    public let vendor: String?
    public let product: String?

    public let facts: [Fact]
    public let primaryURL: String?
    /// Not for display (voice.md 8). Kept because the contract carries it.
    public let duplicatesCount: Int

    public let deltaStatus: DeltaStatus?
    public let deltaNote: String?
    /// A storyline already told with nothing new to say. The core decides this,
    /// never the client: judging significance is exactly what SUR-2 forbids.
    public let inProgress: Bool
    public let daysTracked: Int
    public let contextLabel: ContextLabel?
    public let trendID: String?
    public let precedents: [Precedent]
    public let retrieval: RetrievalReport?
    /// The sentence above the precedent list, composed by the core. Composing
    /// it here would mean recounting the corpus, and three surfaces would word
    /// the same claim three ways.
    public let contextNote: String?

    /// The date the card leads with, chosen once by the core. Picking it from
    /// `facts` was how a card about today's news came to say "expired 649 days
    /// ago": each consumer took a different dated fact.
    public let dueDate: CalendarDay?
    public let dueDateRaw: String?
    public let duePrecision: DatePrecision

    /// Never rendered as a number (voice.md 8). Read by the local notification
    /// threshold only (MAC-9); the order stays exactly as the core set it.
    public let score: Int
    public let scoreRationale: String
    public let rank: Int
    public let tier: Tier

    public let runSummary: RunSummary?
    /// Populated on a quiet day: what the reader filed away and is now due.
    public let upcoming: [UpcomingDeadline]

    public let stats: [String: Int]
    public let failureReason: String?
    public let failureStage: String?
    public let runLogURL: String?

    /// Present in the payload, unknown to this build. Shown nowhere, available
    /// for the diagnostics footer so a schema drift is visible, not silent.
    public let unknownFields: [String]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case headline, summary, vendor, product, facts, precedents, retrieval, score, rank, tier,
            stats, upcoming
        case schemaVersion = "schema_version"
        case signalID = "signal_id"
        case runID = "run_id"
        case signalType = "signal_type"
        case createdAt = "created_at"
        case forDate = "for_date"
        case whyItMatters = "why_it_matters"
        case changeType = "change_type"
        case primaryURL = "primary_url"
        case duplicatesCount = "duplicates_count"
        case deltaStatus = "delta_status"
        case deltaNote = "delta_note"
        case daysTracked = "days_tracked"
        case contextLabel = "context_label"
        case trendID = "trend_id"
        case scoreRationale = "score_rationale"
        case inProgress = "in_progress"
        case contextNote = "context_note"
        case dueDate = "due_date"
        case duePrecision = "due_precision"
        case runSummary = "run_summary"
        case failureReason = "failure_reason"
        case failureStage = "failure_stage"
        case runLogURL = "run_log_url"
    }

    public init(from decoder: any Decoder) throws {
        let r = Lenient(decoder, keyedBy: CodingKeys.self)
        // Fact and Precedent refused a payload that is not an object; Signal
        // did not, so a bare number in the signals array decoded into a signal
        // with every field at its default — a blank card, indistinguishable on
        // screen from a real one the core wrote badly. Tolerance is for fields,
        // not for the shape.
        guard r.isObject else { throw r.notAnObject(decoder) }
        schemaVersion = r.int(.schemaVersion, default: 0)
        signalID = r.string(.signalID, default: "")
        runID = r.string(.runID, default: "")
        signalType = r.value(.signalType, as: SignalKind.self) ?? SignalKind(openRawValue: "")
        createdAtRaw = r.string(.createdAt)
        createdAt = createdAtRaw.flatMap(RadarTime.parse)
        forDateRaw = r.string(.forDate)
        forDate = forDateRaw.flatMap(CalendarDay.init(iso:))

        headline = r.string(.headline, default: "")
        summary = r.string(.summary, default: "")
        whyItMatters = r.string(.whyItMatters, default: "")
        changeType = r.value(.changeType, as: ChangeType.self)
        vendor = r.string(.vendor)
        product = r.string(.product)

        facts = r.list(.facts, of: Fact.self)
        primaryURL = r.string(.primaryURL)
        duplicatesCount = r.int(.duplicatesCount, default: 0)

        deltaStatus = r.value(.deltaStatus, as: DeltaStatus.self)
        deltaNote = r.string(.deltaNote)
        inProgress = r.bool(.inProgress, default: false)
        daysTracked = r.int(.daysTracked, default: 0)
        contextLabel = r.value(.contextLabel, as: ContextLabel.self)
        trendID = r.string(.trendID)
        precedents = r.list(.precedents, of: Precedent.self)
        retrieval = r.value(.retrieval, as: RetrievalReport.self)
        contextNote = r.string(.contextNote)

        dueDateRaw = r.string(.dueDate)
        dueDate = dueDateRaw.flatMap(CalendarDay.init(iso:))
        duePrecision = r.value(.duePrecision, as: DatePrecision.self) ?? .day

        score = r.int(.score, default: 0)
        scoreRationale = r.string(.scoreRationale, default: "")
        rank = r.int(.rank, default: 0)
        tier = r.value(.tier, as: Tier.self) ?? .standard

        runSummary = r.value(.runSummary, as: RunSummary.self)
        upcoming = r.list(.upcoming, of: UpcomingDeadline.self)

        stats = r.intMap(.stats)
        failureReason = r.string(.failureReason)
        failureStage = r.string(.failureStage)
        runLogURL = r.string(.runLogURL)

        unknownFields = Lenient<CodingKeys>.unknownFields(
            decoder, known: CodingKeys.allCases.map(\.rawValue))
    }
}


// MARK: - Entry points

extension Signal {
    /// Returns nil only when the bytes are not a JSON object at all. Any object
    /// decodes into some Signal, however incomplete.
    public static func decode(json: Data) -> Signal? {
        try? JSONDecoder().decode(Signal.self, from: json)
    }

    /// Row order from the store is the order the core ranked: preserved as is.
    /// A row that cannot be parsed is dropped, the rest of the run still shows.
    public static func decodeAll(payloads: [Data]) -> [Signal] {
        payloads.compactMap { decode(json: $0) }
    }
}
