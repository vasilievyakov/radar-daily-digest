//
//  RadarApp.swift
//  Radar
//
//  A menu bar item and the window behind it (MAC-1). No dock icon, no main
//  window, nothing to manage: the digest is a glance, and a glance should not
//  cost an application switch.
//
//  The whole client is this — a file, a decoder, a list. It cannot fetch, rank
//  or judge, and the demo argument depends on that being visibly true rather
//  than merely claimed.
//

import AppKit
import RadarKit
import SwiftUI

/// Everything the app owns, alive from launch rather than from first glance.
///
/// Wiring this to the view's `task` was wrong in a way that only shows on the
/// morning it matters: nothing loaded until the reader opened the popover, so
/// the unread count in the menu bar — the entire reason for a menu bar item —
/// stayed empty until after it was no longer needed, and a run landing while
/// the popover was closed went unannounced.
@MainActor
final class AppModel {
    static let shared = AppModel()

    let store = FeedStore(feedURL: FeedStore.defaultURL())
    let readState = ReadState()
    let notifier = Notifier()
    private var previewWindow: NSWindow?

    func start() {
        notifier.requestPermission()
        store.start { [weak self] feed in
            // A run that just landed, announced once, against a threshold the
            // reader sets and the core's score decides against (MAC-9).
            self?.notifier.announce(feed)
        }
        // The menu bar item is created either way (MAC-1), and on a machine
        // whose bar is full it is created and never drawn: macOS parks the
        // overflow left of the notch and shows nothing, with no indicator that
        // anything is hidden. A minimal AppKit status item behaves the same way
        // here, so this is the platform rather than the app — but a digest that
        // cannot be opened is not a digest. The window is the way in that always
        // works; `RADAR_WINDOW=0` turns it off for a menu bar with room.
        if ProcessInfo.processInfo.environment["RADAR_WINDOW"] != "0" { openWindow() }
    }

    /// The same view as an ordinary window.
    ///
    /// A popover under the menu bar closes the moment focus moves, which makes
    /// it impossible to screenshot, hard to point at, and awkward on a shared
    /// screen. Behind an environment variable the digest also opens as a
    /// window — same view, same store, no second code path to keep in step.
    func openWindow() {
        if let existing = previewWindow {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let hosting = NSHostingController(
            rootView: DigestView(store: store, readState: readState, notifier: notifier))
        let window = NSWindow(contentViewController: hosting)
        window.title = "Радар"
        window.styleMask = [.titled, .closable, .miniaturizable]
        window.setContentSize(NSSize(width: 420, height: 640))
        window.center()
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
        // Still an accessory: a window does not have to cost a Dock icon.
        NSApp.activate(ignoringOtherApps: true)
        previewWindow = window
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated { AppModel.shared.start() }
    }
}

@main
struct RadarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    private var model: AppModel { AppModel.shared }

    var body: some Scene {
        MenuBarExtra {
            DigestView(store: model.store, readState: model.readState, notifier: model.notifier)
        } label: {
            // The count is the whole point of a menu bar presence: the reader
            // learns whether to look without looking.
            // The count is the whole point of a menu bar presence: the reader
            // learns whether to look without looking.
            let unread = model.readState.unreadCount(in: model.store.feed?.items ?? [])
            HStack(spacing: 3) {
                Image(
                    systemName: model.store.isStale
                        ? "antenna.radiowaves.left.and.right.slash"
                        : "antenna.radiowaves.left.and.right")
                if unread > 0 { Text("\(unread)") }
            }
        }
        .menuBarExtraStyle(.window)
    }
}
