// swift-tools-version: 6.0
//
// Two targets, and the split is the architectural argument in file form.
//
// RadarKit decodes the Signal contract and knows nothing else: no network, no
// database, no ranking, no model. Radar draws it. Neither can reach the
// pipeline — there is no dependency to reach it through — so SUR-1 and MAC-2
// hold by construction rather than by discipline.

import PackageDescription

let package = Package(
    name: "Radar",
    platforms: [.macOS(.v14)],
    targets: [
        .target(name: "RadarKit"),
        .executableTarget(name: "Radar", dependencies: ["RadarKit"]),
        // Not a test target: this machine has Command Line Tools without
        // Xcode, so neither XCTest nor swift-testing is importable. The checks
        // are an executable that exits non-zero — worth less than a real
        // harness, worth far more than trusting the decoder by eye.
        .executableTarget(
            name: "RadarChecks",
            dependencies: ["RadarKit"],
            resources: [.copy("fixtures")]
        ),
    ]
)
