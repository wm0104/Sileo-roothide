import Foundation
import Darwin

@main
struct ArchiveFixtures {
    static func rejects(_ block: () throws -> Void) {
        do { try block(); fatalError("Expected rejection") } catch { }
    }
    static func main() throws {
        let fm = FileManager.default
        let base = fm.temporaryDirectory.appendingPathComponent("sileo-native-" + UUID().uuidString)
        try fm.createDirectory(at: base, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: base) }
        // Canonical physical temp anchor; macOS /var itself is a platform symlink.
        let physical = URL(fileURLWithPath: ArchiveLocation.canonical(base.path)!)
        let library = physical.appendingPathComponent("Library")
        let apt = physical.appendingPathComponent("apt")
        let output = physical.appendingPathComponent("archives")
        for url in [library.appendingPathComponent("Preferences"), apt.appendingPathComponent("sources.list.d"), output] {
            try fm.createDirectory(at: url, withIntermediateDirectories: true)
        }
        let preference = library.appendingPathComponent("Preferences/test.plist")
        try Data("fixture-private".utf8).write(to: preference)
        try Data("deb https://fixture.invalid ./".utf8).write(to: apt.appendingPathComponent("sources.list.d/fixture.list"))
        let engine = ConfigurationBackupEngine(library: library.path, apt: apt.path)
        var snapshot = try engine.capture()
        snapshot.manifest.digests = snapshot.files.mapValues(ConfigurationSnapshot.digest)
        try snapshot.validate()
        let fixtureRoots = ConfigurationSnapshot.roots.enumerated().map { index, _ in
            physical.appendingPathComponent("live-" + String(index)).path
        }
        for root in fixtureRoots {
            let inside = URL(fileURLWithPath: root).appendingPathComponent("inside")
            try fm.createDirectory(at: inside, withIntermediateDirectories: true)
            rejects { _ = try ArchiveLocation.open(inside, liveRoots: fixtureRoots) }
            rejects { _ = try ArchiveLocation.open(URL(fileURLWithPath: root), liveRoots: fixtureRoots) }
        }
        rejects { _ = try ArchiveLocation.open(physical, liveRoots: fixtureRoots) }
        _ = try ArchiveLocation.open(output, liveRoots: fixtureRoots)
        let boundary = physical.appendingPathComponent("live-0-other")
        try fm.createDirectory(at: boundary, withIntermediateDirectories: true)
        _ = try ArchiveLocation.open(boundary, liveRoots: fixtureRoots)
        let alias = physical.appendingPathComponent("user-alias")
        try fm.createSymbolicLink(at: alias, withDestinationURL: output)
        rejects { _ = try ArchiveLocation.open(alias, liveRoots: fixtureRoots) }
        let throughAlias = alias.appendingPathComponent("child")
        try fm.createDirectory(at: output.appendingPathComponent("child"), withIntermediateDirectories: true)
        rejects { _ = try ArchiveLocation.open(throughAlias, liveRoots: fixtureRoots) }
        if physical.path.hasPrefix("/private/var/") {
            let platformAlias = URL(fileURLWithPath: "/var/" + String(output.path.dropFirst("/private/var/".count)))
            let physicalFD = try ArchiveLocation.open(output, liveRoots: fixtureRoots)
            let aliasFD = try ArchiveLocation.open(platformAlias, liveRoots: fixtureRoots)
            var first = stat(), second = stat()
            assert(fstat(physicalFD.descriptor, &first) == 0 && fstat(aliasFD.descriptor, &second) == 0)
            assert(ConfigurationDirectory.sameNode(first, second))
        }
        let identity = try ArchiveLocation.identity(output)
        let iris = try IrisinReferenceStorage.publish(snapshot, in: output, allowPruning: false)
        let fromIris = try ConfigurationBackupStorage.load(from: iris.url)
        assert(fromIris.files == snapshot.files)
        assert(fromIris.manifest.metadata == snapshot.manifest.metadata)
        let sileo = try ConfigurationBackupStorage.publish(fromIris, in: output, expectedIdentity: identity)
        let fromSileo = try IrisinReferenceStorage.load(from: sileo.url)
        assert(fromSileo.files == snapshot.files)
        assert(fromSileo.manifest.metadata == snapshot.manifest.metadata)
        assert(sileo.url != iris.url && sileo.url.lastPathComponent.hasSuffix("_2"))
        // Named but foreign, renamed and corrupt directories must survive shared cleanup.
        let renamed = output.appendingPathComponent("renamed")
        try fm.copyItem(at: iris.url, to: renamed)
        let foreign = output.appendingPathComponent("配置备份_01-01")
        try fm.copyItem(at: iris.url, to: foreign)
        let manifestURL = foreign.appendingPathComponent("manifest.json")
        var manifest = try JSONDecoder().decode(ConfigurationSnapshot.Manifest.self, from: Data(contentsOf: manifestURL))
        manifest.producer = "foreign.example"; manifest.archiveName = foreign.lastPathComponent
        try JSONEncoder().encode(manifest).write(to: manifestURL)
        let corrupt = output.appendingPathComponent("配置备份_01-02")
        try fm.createDirectory(at: corrupt, withIntermediateDirectories: true)
        try Data("broken".utf8).write(to: corrupt.appendingPathComponent("manifest.json"))
        _ = try ConfigurationBackupStorage.publish(snapshot, in: output, expectedIdentity: identity)
        assert(!fm.fileExists(atPath: iris.url.path))
        assert(fm.fileExists(atPath: sileo.url.path))
        for url in [renamed, foreign, corrupt] { assert(fm.fileExists(atPath: url.path)) }
        rejects { _ = try ConfigurationBackupStorage.publish(snapshot, in: output, expectedIdentity: "wrong") }
        var bad = snapshot
        bad.files["config/Preferences/test.plist"] = Data("tampered".utf8)
        rejects { try bad.validate() }
        bad = snapshot; bad.manifest.metadata["config/Preferences"]!.mode = 0o4777
        rejects { try bad.validate() }
        assert(!ConfigurationSnapshot.validPath("config/Preferences/../escape"))
        let link = library.appendingPathComponent("Preferences/link")
        try fm.createSymbolicLink(atPath: link.path, withDestinationPath: preference.path)
        rejects { _ = try engine.capture() }; try fm.removeItem(at: link)
        try fm.linkItem(at: preference, to: link)
        rejects { _ = try engine.capture() }; try fm.removeItem(at: link)
        // Ordinary failure after one swap must restore the original selected trees.
        try Data("current".utf8).write(to: preference)
        engine.afterPublish = { _ in throw ConfigurationBackupError("fixture injected failure") }
        rejects { try engine.restore(snapshot, preferences: true, sources: true) }
        let rolledBack = try Data(contentsOf: preference)
        assert(rolledBack == Data("current".utf8))
        engine.afterPublish = nil
        let extra = library.appendingPathComponent("Preferences/extra")
        try Data("extra".utf8).write(to: extra)
        try engine.restore(snapshot, preferences: true, sources: false)
        let restored = try Data(contentsOf: preference)
        assert(restored == Data("fixture-private".utf8))
        assert(!fm.fileExists(atPath: extra.path))
        let remaining = try fm.contentsOfDirectory(atPath: library.path)
        assert(remaining.allSatisfy { !$0.hasPrefix(".sileo-config-") })
        // The gate spans asynchronous lifetimes, permits ordinary nesting and excludes capture.
        let normal = ArchiveActivity.shared.enter()!
        assert(ArchiveActivity.shared.enter(backup: true) == nil)
        normal.end()
        let exclusive = ArchiveActivity.shared.enter(backup: true)!
        assert(ArchiveActivity.shared.enter() == nil)
        exclusive.end()
        print("PASS native archive fixtures: cross-producer storage, retention, links, digests, rollback, replacement, activity gate")
    }
}
