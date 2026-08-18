//
//  Language.swift
//  RadarKit
//
//  Grammar the client needs to render numbers, kept beside the contract rather
//  than inside a view.
//
//  The core learned this lesson first: the plural rule lived in the email
//  channel, the digest could not reach it, and fourteen cards went out saying
//  "через 1 дней". A rule that only one surface can see is a rule the others
//  will get wrong.
//

import Foundation

public enum Plural {
    /// The forms Russian needs and English does not: "1 день", "2 дня",
    /// "5 дней". The digest carried "через 1 дней" on fourteen cards because
    /// this rule lived in one channel and the core could not reach it.
    public static func form(_ n: Int, _ forms: (String, String, String)) -> String {
        let n = abs(n) % 100
        if (11...14).contains(n) { return forms.2 }
        switch n % 10 {
        case 1: return forms.0
        case 2, 3, 4: return forms.1
        default: return forms.2
        }
    }

    public static func count(_ n: Int, _ forms: (String, String, String)) -> String {
        "\(n) \(form(n, forms))"
    }
}
