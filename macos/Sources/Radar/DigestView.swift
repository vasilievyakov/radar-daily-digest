//
//  DigestView.swift
//  Radar
//
//  The window behind the menu bar icon: a day's signals, in the order the core
//  ranked them.
//
//  Three days the digest could have and all three are drawn here — a day with
//  news, a quiet day (MAC-5), a run that did not finish. The third is the one
//  usually left as a blank screen, and a blank screen is indistinguishable
//  from silence, which is a lie about the morning.
//

import AppKit
import RadarKit
import SwiftUI

struct DigestView: View {
    let store: FeedStore
    let readState: ReadState
    let notifier: Notifier

    /// How many cards stand open before the rest folds away. Capacity is a
    /// property of the channel, not of the news: the same run fills a page in
    /// the browser and a narrow window here.
    private let openCards = 6

    @State private var expanded: Set<String> = []
    @State private var showingRest = false

    private var feed: Feed? { store.feed }
    private var today: CalendarDay { feed?.forDate ?? CalendarDay.today() }
    private var items: [Signal] { feed?.items ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if store.isStale { staleBanner }
            header
            Divider().opacity(0.5)
            content
            Divider().opacity(0.5)
            footer
        }
        .frame(width: 420)
        // The lead card arrives open, as it does on the page: the reader sees
        // one piece of evidence without asking for it, and learns that every
        // other card has the same underneath.
        .task(id: feed?.runID) {
            if let lead = items.first { expanded = [lead.signalID] }
        }
        .background(.background)
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(feed?.forDate.map(Wording.day) ?? "Сводка")
                    .font(.system(size: 15, weight: .semibold))
                Text(countLine)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Menu {
                Button("Отметить всё прочитанным") { readState.markAllRead(items) }
                Button("Обновить") { store.reload() }
                Divider()
                if let dir = feed.map({ _ in store.feedURL.deletingLastPathComponent() }) {
                    Button("Открыть сводку в браузере") {
                        NSWorkspace.shared.open(dir.appendingPathComponent("digest.html"))
                    }
                    Button("Открыть лог прогона") {
                        NSWorkspace.shared.open(dir.appendingPathComponent("run-log.html"))
                    }
                }
                Divider()
                Text("Источник: \(store.feedURL.path)")
                Button("Выбрать файл сводки…") { chooseFeed() }
                Divider()
                Button("Выйти") { NSApplication.shared.terminate(nil) }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .frame(width: 24)
        }
        .padding(.horizontal, 14)
        .padding(.top, 12)
        .padding(.bottom, 10)
    }

    private var countLine: String {
        guard let feed else { return "источник не найден" }
        var line = Plural.count(feed.items.count, ("сигнал", "сигнала", "сигналов"))
        let unread = readState.unreadCount(in: feed.items)
        if unread > 0 {
            line += ", \(unread) \(Plural.form(unread, ("новый", "новых", "новых")))"
        }
        if let at = feed.generatedAt {
            let f = DateFormatter()
            f.dateFormat = "HH:mm"
            line += " · прогон \(f.string(from: at))"
        }
        return line
    }

    private var staleBanner: some View {
        HStack(spacing: 6) {
            Image(systemName: "clock.arrow.circlepath").font(.system(size: 10))
            Text(staleText)
                .font(.system(size: 10.5))
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .foregroundStyle(Tone.accent)
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background(Tone.accent.opacity(0.10))
    }

    private var staleText: String {
        // Says which of the two failures this is. "Данные устарели" over an
        // empty screen leaves the reader unable to tell a quiet morning from a
        // scheduler that never fired.
        if store.origin == .snapshot {
            switch store.problem {
            case .noDocument: return "Сводки на месте нет. Показано последнее, что приходило."
            case .unreadable(let why): return "Сводка не читается: \(why). Показано последнее известное."
            case nil: return "Показано последнее известное."
            }
        }
        if let day = store.feed?.forDate {
            return "Данные за \(Wording.day(day)). Сегодняшний прогон ещё не приходил."
        }
        return "Дата прогона неизвестна."
    }

    // MARK: - Body

    @ViewBuilder private var content: some View {
        if let failure = feed?.failure {
            FailureView(signal: failure, feedDirectory: store.feedURL.deletingLastPathComponent())
        } else if items.isEmpty, let quiet = feed?.quietDay {
            QuietDayView(signal: quiet, today: today)
        } else if items.isEmpty {
            emptyState
        } else {
            list
        }
    }

    private var list: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 5) {
                ForEach(items.prefix(openCards), id: \.signalID) { card($0) }

                if items.count > openCards {
                    Button {
                        withAnimation(.easeOut(duration: 0.15)) { showingRest.toggle() }
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: showingRest ? "chevron.up" : "chevron.down")
                                .font(.system(size: 8, weight: .semibold))
                            Text(
                                showingRest
                                    ? "Свернуть"
                                    : "Ещё \(Plural.count(items.count - openCards, ("сигнал", "сигнала", "сигналов")))"
                            )
                            .font(.system(size: 11))
                            Spacer()
                        }
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 6)
                        .padding(.horizontal, 4)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)

                    if showingRest {
                        ForEach(items.dropFirst(openCards), id: \.signalID) { card($0) }
                    }
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .frame(maxHeight: 460)
    }

    private func card(_ signal: Signal) -> some View {
        SignalCard(
            signal: signal,
            today: today,
            isRead: readState.isRead(signal),
            feedDirectory: store.feedURL.deletingLastPathComponent(),
            expanded: Binding(
                get: { expanded.contains(signal.signalID) },
                set: { open in
                    if open {
                        expanded.insert(signal.signalID)
                        // Opening a card is the only evidence this client has
                        // that it was read. Arriving in the list is not.
                        readState.markRead(signal)
                    } else {
                        expanded.remove(signal.signalID)
                    }
                }))
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Сигналов нет")
                .font(.system(size: 12, weight: .medium))
            Text("Прогон не записал ни одного сигнала за этот день.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
    }

    // MARK: - Footer

    private var footer: some View {
        HStack(spacing: 8) {
            if let s = feed?.runSummary {
                Text(summaryLine(s))
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 0)
            if let dir = feed.map({ _ in store.feedURL.deletingLastPathComponent() }) {
                LinkButton(title: "Сводка", url: dir.appendingPathComponent("digest.html"))
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    private func summaryLine(_ s: RunSummary) -> String {
        var parts = [
            "\(s.sourcesChecked) источников",
            "\(s.materialsCollected) материалов",
        ]
        if s.materialsFiltered > 0 { parts.append("\(s.materialsFiltered) отсеяно") }
        if !s.sourcesFailed.isEmpty {
            parts.append("не ответили: \(s.sourcesFailed.prefix(2).joined(separator: ", "))")
        }
        if s.costUSD > 0 { parts.append(String(format: "$%.2f", s.costUSD)) }
        return parts.joined(separator: " · ")
    }

    private func chooseFeed() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.json]
        panel.canChooseDirectories = false
        panel.directoryURL = store.feedURL.deletingLastPathComponent()
        if panel.runModal() == .OK, let url = panel.url { store.point(at: url) }
    }
}

// MARK: - Quiet day

/// SUR-4 and MAC-5: a quiet day is a result, and it is reported as one. The
/// run still says what it checked, and what the reader has coming.
private struct QuietDayView: View {
    let signal: Signal
    let today: CalendarDay

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(signal.headline.isEmpty ? "Сегодня ничего важного" : signal.headline)
                    .font(.system(size: 13, weight: .semibold))
                if !signal.summary.isEmpty {
                    Text(signal.summary)
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if !signal.upcoming.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Впереди")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.tertiary)
                    ForEach(signal.upcoming) { deadline in
                        HStack(alignment: .top, spacing: 6) {
                            Text(deadline.when.map { Wording.day($0, precision: deadline.datePrecision) } ?? "")
                                .font(.system(size: 10.5, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .frame(width: 76, alignment: .leading)
                            Text(deadline.what)
                                .font(.system(size: 11))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
    }
}

// MARK: - Failure

private struct FailureView: View {
    let signal: Signal
    let feedDirectory: URL

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(Tone.accent)
                    .font(.system(size: 11))
                Text(signal.headline.isEmpty ? "Прогон не завершился" : signal.headline)
                    .font(.system(size: 13, weight: .semibold))
            }
            if let reason = signal.failureReason, !reason.isEmpty {
                Text(reason)
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let stage = signal.failureStage, !stage.isEmpty {
                Text("Стадия: \(stage)")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.tertiary)
            }
            if let url = Links.resolve(signal.runLogURL, near: feedDirectory) {
                LinkButton(title: "Лог прогона", url: url)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
    }
}
