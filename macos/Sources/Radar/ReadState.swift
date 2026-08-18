//
//  ReadState.swift
//  Radar
//
//  What this reader has already seen. Local, and staying local (MAC-7): the
//  core is one pipeline serving three surfaces, and a per-person reading habit
//  is not a fact about the world it should be storing.
//

import Foundation
import Observation
import RadarKit

@MainActor
@Observable
final class ReadState {
    private var read: Set<String>
    private let key = "readSignalIDs"
    /// Ten runs of forty signals is four hundred identifiers; a bound keeps the
    /// list from growing for the lifetime of the install.
    private let limit = 2000

    init() {
        read = Set(UserDefaults.standard.stringArray(forKey: key) ?? [])
    }

    func isRead(_ signal: Signal) -> Bool { read.contains(signal.signalID) }

    func markRead(_ signal: Signal) {
        guard !read.contains(signal.signalID) else { return }
        read.insert(signal.signalID)
        persist()
    }

    func markAllRead(_ signals: [Signal]) {
        let before = read.count
        read.formUnion(signals.map(\.signalID))
        if read.count != before { persist() }
    }

    func unreadCount(in signals: [Signal]) -> Int {
        signals.reduce(0) { $0 + (read.contains($1.signalID) ? 0 : 1) }
    }

    private func persist() {
        if read.count > limit { read = Set(read.suffix(limit)) }
        UserDefaults.standard.set(Array(read), forKey: key)
    }
}
