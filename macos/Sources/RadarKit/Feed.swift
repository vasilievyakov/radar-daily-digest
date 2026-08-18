//
//  Feed.swift
//  RadarKit
//
//  The document the core writes and this client reads (MAC-3). An envelope —
//  which run, which day, when it was written — around signals carried verbatim.
//
//  Decoding is as total as Signal's: a truncated or malformed document yields
//  a Feed with no signals rather than an error the UI has to invent copy for.
//

import Foundation

public struct Feed: Decodable, Sendable, Hashable {
    public let feedVersion: Int
    public let generatedAt: Date?
    public let generatedAtRaw: String?
    public let runID: String?
    public let forDate: CalendarDay?
    public let forDateRaw: String?
    /// What the core said it wrote, kept apart from what actually decoded. The
    /// two differing is the one honest way to notice a dropped signal.
    public let declaredCount: Int
    public let signals: [Signal]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case signals
        case feedVersion = "feed_version"
        case generatedAt = "generated_at"
        case runID = "run_id"
        case forDate = "for_date"
        case declaredCount = "signal_count"
    }

    public init(from decoder: any Decoder) throws {
        let r = Lenient(decoder, keyedBy: CodingKeys.self)
        guard r.isObject else { throw r.notAnObject(decoder) }
        feedVersion = r.int(.feedVersion, default: 0)
        generatedAtRaw = r.string(.generatedAt)
        generatedAt = generatedAtRaw.flatMap(RadarTime.parse)
        runID = r.string(.runID)
        forDateRaw = r.string(.forDate)
        forDate = forDateRaw.flatMap(CalendarDay.init(iso:))
        declaredCount = r.int(.declaredCount, default: 0)
        signals = r.list(.signals, of: Signal.self)
    }

    /// Signals the core ranked into the body of the digest, in its order.
    public var items: [Signal] { signals.filter { $0.signalType == .digestItem } }
    public var quietDay: Signal? { signals.first { $0.signalType == .quietDay } }
    public var failure: Signal? { signals.first { $0.signalType == .runFailure } }

    /// Carried on every signal; taken from whichever one is present.
    public var runSummary: RunSummary? { signals.compactMap(\.runSummary).first }

    public var droppedOnDecode: Int { max(0, declaredCount - signals.count) }
}

// MARK: - Reading the document

public enum FeedProblem: Sendable, Hashable {
    /// The pipeline has never written here, or the path is wrong.
    case noDocument
    /// Present but unreadable: permissions, a partial write, not JSON at all.
    case unreadable(String)
}

public enum FeedRead: Sendable {
    case ok(Feed)
    case failed(FeedProblem)

    public var feed: Feed? { if case .ok(let f) = self { return f } else { return nil } }
    public var problem: FeedProblem? { if case .failed(let p) = self { return p } else { return nil } }
}

extension Feed {
    public static func read(contentsOf url: URL) -> FeedRead {
        guard FileManager.default.fileExists(atPath: url.path) else { return .failed(.noDocument) }
        guard let data = try? Data(contentsOf: url) else {
            return .failed(.unreadable("файл не читается"))
        }
        guard let feed = try? JSONDecoder().decode(Feed.self, from: data) else {
            return .failed(.unreadable("документ не разобран как JSON"))
        }
        return .ok(feed)
    }

    public static func decode(json: Data) -> Feed? {
        try? JSONDecoder().decode(Feed.self, from: json)
    }
}
