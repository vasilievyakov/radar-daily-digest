//
//  FeedStore.swift
//  Radar
//
//  Everything this client knows about the world: one file on disk, watched.
//
//  It does not rank, filter by significance, call a model or open the store —
//  MAC-2 — and the way to keep that true under pressure is to give the client
//  no way to do any of it. The only input is a JSON document; the only output
//  is what the window draws.
//

import Foundation
import Observation
import RadarKit

@MainActor
@Observable
final class FeedStore {
    enum Origin: Sendable {
        /// Read from the document the pipeline writes.
        case live
        /// The document was unavailable and this is the last one we saw.
        /// MAC-8: last known signals, marked as such — never a blank screen.
        case snapshot
    }

    private(set) var feed: Feed?
    private(set) var origin: Origin = .live
    private(set) var problem: FeedProblem?
    private(set) var lastRead: Date?

    private(set) var feedURL: URL
    private var watcher: DispatchSourceFileSystemObject?
    private var poller: Task<Void, Never>?
    private let snapshotURL: URL
    private var onNewRun: ((Feed) -> Void)?

    init(feedURL: URL) {
        self.feedURL = feedURL
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let dir = support.appendingPathComponent("Radar", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        snapshotURL = dir.appendingPathComponent("last-feed.json")
    }

    func start(onNewRun: @escaping (Feed) -> Void) {
        self.onNewRun = onNewRun
        reload()
        watchDirectory()
        // A backstop under the watcher: editors, network volumes and atomic
        // renames all confound file events in their own way, and a digest that
        // silently stops updating is worse than one that updates a minute late.
        poller = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(20))
                self?.reload()
            }
        }
    }

    func point(at url: URL) {
        feedURL = url
        UserDefaults.standard.set(url.path, forKey: FeedStore.pathKey)
        watcher?.cancel()
        watcher = nil
        reload()
        watchDirectory()
    }

    // MARK: - Reading

    func reload() {
        let previousRun = feed?.runID
        switch Feed.read(contentsOf: feedURL) {
        case .ok(let fresh):
            feed = fresh
            origin = .live
            problem = nil
            lastRead = Date()
            try? Data(contentsOf: feedURL).write(to: snapshotURL, options: .atomic)
            if fresh.runID != previousRun, previousRun != nil { onNewRun?(fresh) }
        case .failed(let why):
            problem = why
            // Falling back is not hiding the failure: `origin` says the screen
            // is a memory, and the header says it out loud.
            if let data = try? Data(contentsOf: snapshotURL), let kept = Feed.decode(json: data) {
                feed = kept
                origin = .snapshot
            }
        }
    }

    /// The day the shown feed is about, against today. A run that did not
    /// happen leaves yesterday's document in place, readable and wrong.
    var isStale: Bool {
        if origin == .snapshot { return true }
        guard let day = feed?.forDate else { return true }
        return day < CalendarDay.today()
    }

    // MARK: - Watching

    private func watchDirectory() {
        // The directory, not the file: the pipeline writes to a temporary name
        // and renames it into place, which detaches any descriptor held on the
        // old inode. Watching the directory survives that.
        let dir = feedURL.deletingLastPathComponent()
        let fd = open(dir.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd, eventMask: [.write, .rename, .delete], queue: .global())
        source.setEventHandler { [weak self] in
            Task { @MainActor in self?.reload() }
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        watcher = source
    }

    // MARK: - Where the document lives

    static let pathKey = "feedPath"

    static func defaultURL() -> URL {
        if let override = ProcessInfo.processInfo.environment["RADAR_FEED"], !override.isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        if let saved = UserDefaults.standard.string(forKey: pathKey), !saved.isEmpty {
            return URL(fileURLWithPath: saved)
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent("Demo Challenge/out/signals.json")
    }
}
