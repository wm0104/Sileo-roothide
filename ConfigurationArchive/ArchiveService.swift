import Foundation
import Darwin

// Tokens may end on a different queue (source refresh's completion is on main).
final class ArchiveActivity {
    static let shared = ArchiveActivity()
    private let lock = NSLock()
    private var ordinary = 0
    private var exclusive = false
    final class Token {
        private var finish: (() -> Void)?
        private let lock = NSLock()
        init(_ finish: @escaping () -> Void) { self.finish = finish }
        func end() { lock.lock(); let action = finish; finish = nil; lock.unlock(); action?() }
        deinit { end() }
    }
    func enter(backup: Bool = false) -> Token? {
        lock.lock(); defer { lock.unlock() }
        guard !exclusive, !backup || ordinary == 0 else { return nil }
        if backup { exclusive = true } else { ordinary += 1 }
        return Token { [self] in
            lock.lock(); defer { lock.unlock() }
            if backup { exclusive = false } else { ordinary -= 1 }
        }
    }
}

enum ArchiveService {
    static func perform(_ operation: String, snapshot: ConfigurationSnapshot? = nil) throws -> ConfigurationSnapshot? {
        #if targetEnvironment(macCatalyst)
        throw ConfigurationBackupError("此备份功能仅用于 iOS roothide")
        #else
        let helper = Bundle.main.bundleURL.appendingPathComponent("sileo-archive").path
        var fd: Int32 = -1, child: Int32 = 0
        guard archive_launch(helper, &fd, &child) == 0 else {
            throw ConfigurationBackupError("无法启动备份服务（启动/权限交接，errno \(errno)）")
        }
        var completed = false
        defer { if !completed { _ = archive_finish(fd, child, 1) } }
        try ArchiveWire.send(ArchiveRequest(operation: operation, snapshot: snapshot), fd: fd)
        let reply = try ArchiveWire.receive(ArchiveReply.self, fd: fd)
        let status = archive_finish(fd, child, 0); completed = true
        if let failure = reply.failure { throw ConfigurationBackupError(failure) }
        guard status == 0 else { throw ConfigurationBackupError("备份服务未正常结束") }
        return reply.snapshot
        #endif
    }
}
