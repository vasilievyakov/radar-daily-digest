//
//  SignalCard.swift
//  Radar
//
//  One signal, closed and open.
//
//  Closed it is a headline, a line of summary and the three facts a reader
//  needs to decide whether to care: what kind of change, whose, and when it
//  bites. Open it shows the evidence — every fact with the sentence it was
//  taken from — because a digest that cannot be checked is a rumour with good
//  typography.
//

import AppKit
import RadarKit
import SwiftUI

struct SignalCard: View {
    let signal: Signal
    let today: CalendarDay
    let isRead: Bool
    let feedDirectory: URL
    @Binding var expanded: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if expanded { detail.padding(.top, 10) }
        }
        .padding(.vertical, 9)
        .padding(.horizontal, 11)
        .background(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(Color.primary.opacity(expanded ? 0.055 : 0.03))
        )
        .overlay(alignment: .leading) { stripe }
        .overlay(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.07), lineWidth: 1)
        )
        .contentShape(Rectangle())
        .onTapGesture { withAnimation(.easeOut(duration: 0.14)) { expanded.toggle() } }
    }

    // MARK: - Closed

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            // Unread is a dot and nothing else: the count in the menu bar has
            // already made the claim, and repeating it in colour on every row
            // would make forty rows shout at once.
            Circle()
                .fill(isRead ? Color.clear : Tone.unread)
                .frame(width: 5, height: 5)
                .padding(.top, 5)

            VStack(alignment: .leading, spacing: 3) {
                Text(signal.headline.isEmpty ? signal.summary : signal.headline)
                    .font(.system(size: 12.5, weight: signal.tier == .lead ? .semibold : .medium))
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .multilineTextAlignment(.leading)

                if let restated = subtitle {
                    Text(restated)
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .lineLimit(expanded ? nil : 2)
                        .fixedSize(horizontal: false, vertical: true)
                }

                meta
            }
            Spacer(minLength: 0)
        }
    }

    /// Two rows on purpose. Facts and badges on one line wrap into each other
    /// at this width — "31 августа, через" on one line and "13 дней" under a
    /// pill — and the reader has to reassemble the date from two fragments.
    private var meta: some View {
        VStack(alignment: .leading, spacing: 3) {
            if !metaParts.isEmpty {
                Text(metaParts.joined(separator: " · "))
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if signal.inProgress || Wording.context(signal.contextLabel) != nil {
                HStack(spacing: 5) {
                    if signal.inProgress { Badge(text: "продолжается") }
                    if let label = Wording.context(signal.contextLabel) { Badge(text: label) }
                }
            }
        }
        .padding(.top, 2)
    }

    /// The summary, unless it is the headline again.
    ///
    /// The core writes both and today they often coincide: the extractor's
    /// lead sentence becomes the headline, and the card then prints the same
    /// forty words twice, which reads as a rendering bug and costs the reader
    /// the only line of context the card had. Trimming a restatement is
    /// rendering; the fix for why they coincide belongs upstream.
    private var subtitle: String? {
        let head = Self.flatten(signal.headline)
        let body = Self.flatten(signal.summary)
        guard !body.isEmpty, !head.isEmpty else { return nil }
        if body == head || body.hasPrefix(head) || head.hasPrefix(body) { return nil }
        return signal.summary
    }

    private static func flatten(_ text: String) -> String {
        text.lowercased()
            .replacingOccurrences(of: "\u{00a0}", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
    }

    private var metaParts: [String] {
        var parts: [String] = []
        if let vendor = signal.vendor, !vendor.isEmpty { parts.append(vendor) }
        if let type = Wording.changeType(signal.changeType) { parts.append(type) }
        if let due = signal.dueDate {
            parts.append(
                "\(Wording.day(due, precision: signal.duePrecision)), "
                    + Wording.relative(due, from: today))
        }
        return parts
    }

    private var stripe: some View {
        // The core's tier, drawn as weight rather than as colour-coding: three
        // saturated stripes down a list is a legend the reader has to learn.
        RoundedRectangle(cornerRadius: 2)
            .fill(Tone.tier(signal.tier))
            .frame(width: 2.5)
            .padding(.vertical, 6)
            .padding(.leading, 1)
    }

    // MARK: - Open

    private var detail: some View {
        VStack(alignment: .leading, spacing: 9) {
            if !signal.whyItMatters.isEmpty {
                Text(signal.whyItMatters)
                    .font(.system(size: 11.5))
                    .foregroundStyle(.primary.opacity(0.85))
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let note = signal.deltaNote, !note.isEmpty {
                Text(note)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !signal.facts.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(signal.facts.enumerated()), id: \.offset) { _, fact in
                        FactRow(fact: fact)
                    }
                }
                .padding(.top, 1)
            }

            if !signal.precedents.isEmpty {
                PrecedentList(
                    note: signal.contextNote,
                    precedents: signal.precedents,
                    retrieval: signal.retrieval)
            }

            HStack(spacing: 12) {
                if let url = Links.resolve(signal.primaryURL, near: feedDirectory) {
                    LinkButton(title: "Первоисточник", url: url)
                }
                if let url = Links.resolve(signal.runLogURL, near: feedDirectory) {
                    LinkButton(title: "Лог прогона", url: url)
                }
                Spacer(minLength: 0)
                if signal.duplicatesCount > 1 {
                    Text("\(signal.duplicatesCount) источника")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.leading, 13)
    }
}

// MARK: - Fact

private struct FactRow: View {
    let fact: Fact

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 5) {
                Text(Wording.factKind(fact.kind))
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
                Text(fact.value)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.primary)
                // Verified means the quote below was found in the fetched page,
                // character for character. Unverified facts are still shown —
                // hiding them would leave the reader with a confident digest
                // and no way to see where the confidence thins out.
                Image(systemName: fact.evidenceVerified ? "checkmark.seal.fill" : "questionmark.circle")
                    .font(.system(size: 9))
                    .foregroundStyle(fact.evidenceVerified ? Tone.verified : Color.secondary)
            }
            if !fact.evidence.isEmpty {
                Text("«\(fact.evidence)»")
                    .font(.system(size: 10.5))
                    .italic()
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.leading, 2)
    }
}

// MARK: - Precedents

private struct PrecedentList: View {
    let note: String?
    let precedents: [Precedent]
    let retrieval: RetrievalReport?
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Button {
                withAnimation(.easeOut(duration: 0.12)) { open.toggle() }
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: open ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                    Text(note ?? Plural.count(precedents.count, ("прецедент", "прецедента", "прецедентов")))
                        .font(.system(size: 10.5))
                        .multilineTextAlignment(.leading)
                }
                .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)

            if open {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(precedents, id: \.statementID) { p in
                        HStack(alignment: .top, spacing: 5) {
                            Text(p.eventDate.map(Wording.day) ?? (p.eventDateRaw ?? ""))
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.tertiary)
                                .frame(width: 62, alignment: .leading)
                            Text(p.text)
                                .font(.system(size: 10.5))
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if let r = retrieval, r.totalFound > r.shown {
                        Text("Показано \(r.shown) из \(r.totalFound) найденных в корпусе.")
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                    }
                }
                .padding(.leading, 12)
            }
        }
    }
}

// MARK: - Small parts

struct Badge: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 9.5))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .background(
                Capsule().fill(Color.primary.opacity(0.07))
            )
    }
}

struct LinkButton: View {
    let title: String
    let url: URL

    var body: some View {
        Button {
            NSWorkspace.shared.open(url)
        } label: {
            HStack(spacing: 3) {
                Text(title).font(.system(size: 10.5))
                Image(systemName: "arrow.up.right").font(.system(size: 8, weight: .semibold))
            }
            .foregroundStyle(Tone.link)
        }
        .buttonStyle(.plain)
        .pointingHand()
    }
}

enum Tone {
    /// Warm, and the same warmth the pages use, so a reader moving between the
    /// window and the browser is looking at one product.
    static let accent = Color(red: 0.68, green: 0.36, blue: 0.13)
    static let link = Color(red: 0.62, green: 0.33, blue: 0.12)
    static let unread = Color(red: 0.78, green: 0.42, blue: 0.15)
    static let verified = Color(red: 0.36, green: 0.52, blue: 0.34)

    static func tier(_ t: Tier) -> Color {
        switch t {
        case .lead: return accent.opacity(0.85)
        case .standard: return Color.primary.opacity(0.28)
        case .background: return Color.primary.opacity(0.12)
        default: return Color.primary.opacity(0.2)
        }
    }
}

enum Links {
    /// `run_log_url` arrives as a filename beside the feed; a primary URL
    /// arrives absolute. Both have to open, and neither may be guessed at.
    static func resolve(_ raw: String?, near directory: URL) -> URL? {
        guard let raw, !raw.isEmpty else { return nil }
        if let url = URL(string: raw), url.scheme != nil { return url }
        return directory.appendingPathComponent(raw)
    }
}

extension View {
    func pointingHand() -> some View {
        onHover { inside in
            if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
        }
    }
}
