//
//  Wording.swift
//  Radar
//
//  Russian labels for contract values, and nothing else. Naming a value is
//  rendering; deciding which values matter is not, and does not happen here.
//
//  Deliberately the same words the web page uses. Three surfaces wording the
//  same change type three ways teaches a reader that they are three products.
//

import Foundation
import RadarKit

enum Wording {
    static func changeType(_ t: ChangeType?) -> String? {
        guard let t else { return nil }
        switch t {
        case .release: return "релиз"
        case .breakingChange: return "ломающее изменение"
        case .deprecation: return "отключение"
        case .pricing: return "цены"
        case .limits: return "лимиты"
        case .security: return "безопасность"
        case .other: return "прочее"
        // A type this build has not learned yet still has to appear: showing
        // the raw value beats showing nothing and beats guessing (SIG-5).
        default: return t.rawValue
        }
    }

    static func factKind(_ k: FactKind) -> String {
        switch k {
        case .version: return "Версия"
        case .effectiveDate: return "Дата вступления в силу"
        case .sunsetDate: return "Дата отключения"
        case .price: return "Цена"
        case .limit: return "Лимит"
        case .affectedProduct: return "Затронутый продукт"
        default: return k.rawValue
        }
    }

    static func context(_ c: ContextLabel?) -> String? {
        guard let c else { return nil }
        switch c {
        case .recurring: return "повторяется"
        case .trendMember: return "часть тренда"
        case .escalation: return "эскалация"
        case .notFoundInCorpus: return nil  // absence of precedent is not a badge
        default: return c.rawValue
        }
    }

    static func delta(_ d: DeltaStatus?) -> String? {
        guard let d else { return nil }
        switch d {
        case .new: return "новое"
        case .continuing: return "продолжается"
        case .updated: return "обновление"
        case .resolved: return "закрыто"
        default: return d.rawValue
        }
    }

    static let months = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]

    static func day(_ d: CalendarDay) -> String {
        let month = (1...12).contains(d.month) ? months[d.month - 1] : String(d.month)
        return "\(d.day) \(month)"
    }

    /// Precision the core recorded, said out loud. A month-precision date shown
    /// as a day is the surface inventing certainty the extraction never had.
    static func day(_ d: CalendarDay, precision: DatePrecision) -> String {
        switch precision {
        case .month:
            let month = (1...12).contains(d.month) ? months[d.month - 1] : String(d.month)
            return "\(month) \(d.year)"
        case .year: return "\(d.year)"
        default: return day(d)
        }
    }

    /// "через 3 дня" / "5 дней назад" / "сегодня", against the day of the run.
    static func relative(_ target: CalendarDay, from base: CalendarDay) -> String {
        let delta = target.days(since: base)
        if delta == 0 { return "сегодня" }
        if delta == 1 { return "завтра" }
        if delta == -1 { return "вчера" }
        let n = abs(delta)
        let word = Plural.count(n, ("день", "дня", "дней"))
        return delta > 0 ? "через \(word)" : "\(word) назад"
    }
}
