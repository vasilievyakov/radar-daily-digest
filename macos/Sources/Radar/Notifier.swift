//
//  Notifier.swift
//  Radar
//
//  MAC-9: tell the reader when something crosses their threshold.
//
//  The threshold is local and it is applied to the score the core already set.
//  Re-ranking here would make the client a second opinion on significance, and
//  two opinions is exactly the failure SUR-2 names.
//

import Foundation
import RadarKit
import UserNotifications

@MainActor
final class Notifier {
    static let thresholdKey = "notifyThreshold"
    static let defaultThreshold = 70

    private var authorized = false

    var threshold: Int {
        get {
            let stored = UserDefaults.standard.integer(forKey: Notifier.thresholdKey)
            return stored == 0 ? Notifier.defaultThreshold : stored
        }
        set { UserDefaults.standard.set(newValue, forKey: Notifier.thresholdKey) }
    }

    func requestPermission() {
        guard Bundle.main.bundleIdentifier != nil else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) {
            granted, _ in
            Task { @MainActor in self.authorized = granted }
        }
    }

    /// One notification per run, naming the loudest signal and how many others
    /// cleared the bar. Forty separate banners for one morning's run is not a
    /// notification, it is an outage of attention.
    func announce(_ feed: Feed) {
        guard authorized else { return }
        let over = feed.items.filter { $0.score >= threshold }
        guard let top = over.first else { return }

        let content = UNMutableNotificationContent()
        content.title = top.headline.isEmpty ? "Новая сводка" : top.headline
        var body = top.summary
        if over.count > 1 {
            let rest = over.count - 1
            body += body.isEmpty ? "" : "\n"
            body += "И ещё \(Plural.count(rest, ("сигнал", "сигнала", "сигналов"))) выше порога."
        }
        content.body = body
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: "run-\(feed.runID ?? UUID().uuidString)",
            content: content,
            trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
