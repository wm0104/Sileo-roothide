import Foundation
import Darwin

@main
struct ArchiveHelper {
    static func main() {
        // Never touch Files-provider directories; only an inherited private socket.
        var info = stat(), peer: uid_t = 0, group: gid_t = 0
        guard CommandLine.arguments.count == 1, getuid() == 0, geteuid() == 0,
              getgid() == 0, getegid() == 0,
              fstat(3, &info) == 0, info.st_mode & S_IFMT == S_IFSOCK,
              getpeereid(3, &peer, &group) == 0, peer == 501 else { exit(70) }
        defer { close(3) }
        var phase = "request"
        do {
            let request = try ArchiveWire.receive(ArchiveRequest.self, fd: 3)
            guard ["capture", "restore-config", "restore-sources", "restore-all"].contains(request.operation) else {
                throw ConfigurationBackupError("Invalid operation")
            }
            phase = "anchors"
            // Only this trusted platform anchor resolves symlinks. All descendants no-follow.
            guard let variableRaw = jbroot_alloc("/var"), let rootRaw = jbroot_alloc("/") else {
                throw ConfigurationBackupError("Bootstrap unavailable")
            }
            defer { free(UnsafeMutablePointer(mutating: variableRaw)); free(UnsafeMutablePointer(mutating: rootRaw)) }
            guard let variable = ArchiveLocation.canonical(String(cString: variableRaw)),
                  let root = ArchiveLocation.canonical(String(cString: rootRaw)) else {
                throw ConfigurationBackupError("Bootstrap unavailable")
            }
            let engine = ConfigurationBackupEngine(library: variable + "/mobile/Library", apt: root + "/etc/apt")
            if request.operation == "capture" {
                guard request.snapshot == nil else { throw ConfigurationBackupError("Unexpected payload") }
                phase = "capture"
                var snapshot = try engine.capture()
                snapshot.manifest.digests = snapshot.files.mapValues(ConfigurationSnapshot.digest)
                try snapshot.validate()
                phase = "response"
                try ArchiveWire.send(ArchiveReply(snapshot: snapshot, failure: nil), fd: 3)
            } else {
                phase = "validate"
                guard let snapshot = request.snapshot, snapshot.manifest.digests != nil else {
                    throw ConfigurationBackupError("Missing authenticated contents")
                }
                try snapshot.validate()
                phase = "restore"
                try engine.restore(snapshot, preferences: request.operation != "restore-sources",
                                   sources: request.operation != "restore-config")
                phase = "response"
                try ArchiveWire.send(ArchiveReply(snapshot: nil, failure: nil), fd: 3)
            }
        } catch {
            // A minimal outcome only: never leak a user's path or configuration contents.
            var failure = "操作失败（\(phase)）"
            if phase == "restore" {
                let detail = (error as? ConfigurationBackupError)?.detail ?? ""
                if detail.hasPrefix("Restore partially applied") { failure = "恢复未完成，回滚或清理失败" }
                else if detail.hasPrefix("Restore completed; temporary") { failure = "恢复已应用，旧目录清理失败" }
                else { failure = "恢复失败，原目录保留或已回滚" }
            }
            try? ArchiveWire.send(ArchiveReply(snapshot: nil, failure: failure), fd: 3)
            exit(71)
        }
    }
}
