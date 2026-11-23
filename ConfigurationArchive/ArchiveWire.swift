import Foundation
import Darwin

// Archive paths and security-scoped URLs exist only in the mobile process.
enum ArchiveLocation {
    static func canonical(_ path: String) -> String? {
        guard let pointer = realpath(path, nil) else { return nil }
        defer { free(pointer) }
        return String(cString: pointer)
    }

    // Only fixed, root-owned platform aliases may be resolved. User aliases never are.
    private static func platformPath(_ path: String) throws -> String {
        guard path.hasPrefix("/"), path == "/" || ConfigurationSnapshot.validPath(String(path.dropFirst())) else {
            throw ConfigurationBackupError("Invalid backup directory")
        }
        for (alias, target) in [("/var", "/private/var"), ("/tmp", "/private/tmp"), ("/etc", "/private/etc")] {
            if path == alias || path.hasPrefix(alias + "/") {
                var info = stat()
                if lstat(alias, &info) == 0, info.st_mode & S_IFMT == S_IFLNK {
                    guard info.st_uid == 0, canonical(alias) == target else {
                        throw ConfigurationBackupError("Unsafe platform alias")
                    }
                    return target + String(path.dropFirst(alias.count))
                }
            }
        }
        return path
    }

    private static func liveRoots() throws -> [String] {
        #if os(iOS)
        guard let variableRaw = jbroot_alloc("/var"), let rootRaw = jbroot_alloc("/") else {
            throw ConfigurationBackupError("Bootstrap unavailable")
        }
        defer { free(UnsafeMutablePointer(mutating: variableRaw)); free(UnsafeMutablePointer(mutating: rootRaw)) }
        guard let variable = canonical(String(cString: variableRaw)),
              let root = canonical(String(cString: rootRaw)) else {
            throw ConfigurationBackupError("Bootstrap unavailable")
        }
        return ConfigurationSnapshot.roots.map {
            let name = String($0.split(separator: "/").last!)
            return $0.hasPrefix("config/") ? variable + "/mobile/Library/" + name : root + "/etc/apt/" + name
        }
        #else
        return []
        #endif
    }

    static func open(_ url: URL, liveRoots suppliedRoots: [String]? = nil) throws -> ConfigurationDirectory {
        #if os(iOS)
        guard getuid() == 501, geteuid() == 501 else {
            throw ConfigurationBackupError("Backup folders require mobile")
        }
        #endif
        guard url.isFileURL else { throw ConfigurationBackupError("Local directory required") }
        let values = try url.resourceValues(forKeys: [.isUbiquitousItemKey, .volumeIsLocalKey])
        guard values.isUbiquitousItem != true, values.volumeIsLocal == true else {
            throw ConfigurationBackupError("Local directory required")
        }
        let path = try platformPath(url.path)
        // No realpath on the selected path: every component is opened O_NOFOLLOW.
        let anchor = try ConfigurationDirectory(absolutePath: path)
        let roots = try suppliedRoots ?? liveRoots()
        for root in roots {
            let lexical = try platformPath(root)
            let physical = canonical(root) ?? lexical
            for live in [lexical, physical] {
                let directory = path == "/" ? "" : path
                guard path != live, !path.hasPrefix(live + "/"), !live.hasPrefix(directory + "/") else {
                    throw ConfigurationBackupError("Backup directory overlaps live configuration")
                }
            }
        }
        return anchor
    }

    static func identity(_ directory: URL) throws -> String {
        let anchor = try open(directory)
        var info = stat()
        guard fstat(anchor.descriptor, &info) == 0 else { throw ConfigurationDirectory.failure() }
        return "\(info.st_dev):\(info.st_ino)"
    }

}

struct ArchiveRequest: Codable {
    // Only capture / restore-config / restore-sources / restore-all. No paths or commands.
    let operation: String
    let snapshot: ConfigurationSnapshot?
}

struct ArchiveReply: Codable {
    let snapshot: ConfigurationSnapshot?
    let failure: String?
}

enum ArchiveWire {
    static func send<T: Encodable>(_ value: T, fd: Int32) throws {
        let encoder = PropertyListEncoder(); encoder.outputFormat = .binary
        let data = try encoder.encode(value)
        guard data.count <= ConfigurationSnapshot.maximumTransportBytes else {
            throw ConfigurationBackupError("备份通信超过大小限制")
        }
        let result = data.withUnsafeBytes { archive_send(fd, $0.baseAddress, UInt32($0.count)) }
        guard result == 0 else { throw ConfigurationDirectory.failure() }
    }
    static func receive<T: Decodable>(_ type: T.Type, fd: Int32) throws -> T {
        var pointer: UnsafeMutableRawPointer?
        var length: UInt32 = 0
        guard archive_receive(fd, &pointer, &length) == 0, let bytes = pointer else {
            throw ConfigurationDirectory.failure()
        }
        let data = Data(bytesNoCopy: bytes, count: Int(length), deallocator: .free)
        return try PropertyListDecoder().decode(type, from: data)
    }
}
