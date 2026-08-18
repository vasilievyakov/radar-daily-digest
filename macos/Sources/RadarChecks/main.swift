//
//  RadarChecks
//
//  What the client promises, checked against the document the core actually
//  writes. Fixtures cover the shapes an ordinary morning does not produce —
//  a quiet day, a failed run, a payload from a core newer than this build.
//
//  Exits non-zero on the first broken promise, and prints every one.
//

import Foundation
import RadarKit

// MARK: - A harness small enough to trust

final class Checks {
    private(set) var failures: [String] = []
    private var skipped: [String] = []
    private var passed = 0

    func check(_ name: String, _ condition: @autoclosure () -> Bool, _ detail: String = "") {
        if condition() {
            passed += 1
        } else {
            failures.append(detail.isEmpty ? name : "\(name): \(detail)")
        }
    }

    func skip(_ name: String, _ why: String) { skipped.append("\(name) — \(why)") }

    func report() -> Int32 {
        for line in skipped { print("  пропущено: \(line)") }
        for line in failures { print("  ПРОВАЛ: \(line)") }
        print("\(passed) проверок пройдено, \(failures.count) провалено, \(skipped.count) пропущено")
        return failures.isEmpty ? 0 : 1
    }
}

let c = Checks()

// MARK: - The real feed of the latest run

/// DR-7 in the client: the decoder is exercised on run output, not on a shape
/// invented to match it.
let liveURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Demo Challenge/out/signals.json")

if FileManager.default.fileExists(atPath: liveURL.path),
    case .ok(let feed) = Feed.read(contentsOf: liveURL)
{
    c.check("прогон непустой", feed.signals.count > 0)
    c.check(
        "ни один сигнал не потерян при разборе", feed.droppedOnDecode == 0,
        "объявлено \(feed.declaredCount), разобрано \(feed.signals.count)")
    c.check("дата прогона прочитана", feed.forDate != nil, feed.forDateRaw ?? "нет поля")
    c.check("идентификатор прогона на месте", feed.runID != nil)

    let items = feed.items
    if items.isEmpty {
        c.skip("карточки", "тихий день")
    } else {
        for s in items {
            c.check(
                "карточке есть что показать", !(s.headline.isEmpty && s.summary.isEmpty),
                s.signalID)
            c.check("tier известен", s.tier.isKnown, "\(s.signalID): \(s.tier.rawValue)")
            c.check("ранг проставлен ядром", s.rank > 0, s.signalID)
        }
        // Order is the core's ranking, carried through the file untouched: the
        // client re-sorting would be a second opinion on significance (SUR-2).
        c.check("порядок сохранён", items.map(\.rank) == items.map(\.rank).sorted())

        let withEvidence = items.filter { $0.facts.contains(where: \.evidenceVerified) }
        c.check(
            "у большинства карточек есть проверенная цитата",
            withEvidence.count * 2 >= items.count,
            "\(withEvidence.count) из \(items.count)")
    }

    if let summary = feed.runSummary {
        c.check("футер знает, сколько источников проверено", summary.sourcesChecked > 0)
    } else {
        c.check("run_summary доехал до поверхности", false, "поле пустое на всех сигналах")
    }

    let unknown = Set(feed.signals.flatMap(\.unknownFields))
    if !unknown.isEmpty {
        print("  ядро шлёт поля, которых этот клиент не знает: \(unknown.sorted().joined(separator: ", "))")
    }
} else {
    c.skip("живой прогон", "на этой машине нет out/signals.json")
}

// MARK: - Shapes an ordinary day does not produce

func fixture(_ name: String) -> Data? {
    guard let url = Bundle.module.url(forResource: "fixtures/\(name)", withExtension: "json")
    else { return nil }
    return try? Data(contentsOf: url)
}

if let data = fixture("quiet_day"), let s = Signal.decode(json: data) {
    c.check("тихий день опознан", s.signalType == .quietDay)
    c.check("тихий день несёт список обязательств", !s.upcoming.isEmpty)
    c.check("тихий день несёт статистику прогона", s.runSummary != nil)
} else {
    c.check("фикстура quiet_day читается", false)
}

if let data = fixture("run_failure"), let s = Signal.decode(json: data) {
    c.check("отказ опознан", s.signalType == .runFailure)
    c.check("у отказа названа причина", !(s.failureReason ?? "").isEmpty)
} else {
    c.check("фикстура run_failure читается", false)
}

if let data = fixture("digest_item"), let s = Signal.decode(json: data) {
    c.check("тип изменения разобран", s.changeType == .deprecation)
    c.check("есть проверенная цитата", s.facts.contains(where: \.evidenceVerified))
    c.check("прецеденты приехали внутри сигнала", !s.precedents.isEmpty)
    c.check("дата карточки выбрана ядром", s.dueDate != nil || s.dueDateRaw != nil)
} else {
    c.check("фикстура digest_item читается", false)
}

// MARK: - The document arriving broken

if case .failed(let problem) = Feed.read(
    contentsOf: URL(fileURLWithPath: "/nonexistent/signals.json"))
{
    c.check("отсутствие файла названо отсутствием", problem == .noDocument)
} else {
    c.check("отсутствующий файл не разбирается", false)
}

let truncated = FileManager.default.temporaryDirectory
    .appendingPathComponent("radar-check-\(UUID().uuidString).json")
try? Data(#"{"feed_version": 1, "signals": [{"signal_id": "a""#.utf8).write(to: truncated)
if case .failed(let problem) = Feed.read(contentsOf: truncated) {
    if case .unreadable = problem {
        c.check("обрезанный документ назван нечитаемым", true)
    } else {
        c.check("обрезанный документ назван нечитаемым", false, "назван отсутствующим")
    }
} else {
    c.check("обрезанный документ не выдан за сводку", false)
}
try? FileManager.default.removeItem(at: truncated)

// SIG-5: a core that learns a new value must not blank this client's screen.
if let s = Signal.decode(
    json: Data(#"{"signal_id":"x","signal_type":"digest_item","change_type":"quantum_leap"}"#.utf8))
{
    c.check("незнакомое значение выживает строкой", s.changeType?.rawValue == "quantum_leap")
    c.check("и помечено как незнакомое", !(s.changeType?.isKnown ?? true))
} else {
    c.check("сигнал с незнакомым типом разобран", false)
}

if let s = Signal.decode(json: Data(#"{"signal_id":"x","invented_by_a_later_core":42}"#.utf8)) {
    c.check("незнакомое поле записано, а не выброшено", s.unknownFields == ["invented_by_a_later_core"])
} else {
    c.check("сигнал с незнакомым полем разобран", false)
}

if let feed = Feed.decode(
    json: Data(#"{"signal_count":3,"signals":[1,"two",{"signal_id":"three"}]}"#.utf8))
{
    c.check("годный сигнал спасён из битого списка", feed.signals.count == 1)
    c.check("потеря видна, а не молчалива", feed.droppedOnDecode == 2)
} else {
    c.check("битый список сигналов не валит документ", false)
}

// The rule that put "через 1 дней" on fourteen cards.
c.check("1 день", Plural.form(1, ("день", "дня", "дней")) == "день")
c.check("2 дня", Plural.form(2, ("день", "дня", "дней")) == "дня")
c.check("5 дней", Plural.form(5, ("день", "дня", "дней")) == "дней")
c.check("11 дней", Plural.form(11, ("день", "дня", "дней")) == "дней")
c.check("21 день", Plural.form(21, ("день", "дня", "дней")) == "день")

exit(c.report())
