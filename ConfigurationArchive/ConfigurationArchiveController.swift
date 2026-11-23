import UIKit
import UniformTypeIdentifiers

final class ConfigurationArchiveController: BaseSettingsViewController, UIDocumentPickerDelegate {
    private var records: [ConfigurationBackupRecord] = []
    private var busy = false
    private var folderName: String?
    private let bookmarkKey = "ConfigurationArchive.Directory"
    private let consentKey = "ConfigurationArchive.SharedRetentionIdentity"

    override func viewDidLoad() {
        super.viewDidLoad(); title = String(localizationKey: "Archive_Title"); reloadArchives()
    }
    private func directory() throws -> URL {
        guard let data = UserDefaults.standard.data(forKey: bookmarkKey) else {
            throw ConfigurationBackupError(String(localizationKey: "Archive_SelectFirst"))
        }
        var stale = false
        let url = try URL(resolvingBookmarkData: data, options: [.withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale)
        guard !stale else { throw ConfigurationBackupError(String(localizationKey: "Archive_Stale")) }
        return url
    }
    private func message(_ text: String) {
        let alert = UIAlertController(title: String(localizationKey: "Archive_Title"), message: text, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: String(localizationKey: "Archive_OK"), style: .default)); present(alert, animated: true)
    }
    private func work(_ action: @escaping () throws -> String?) {
        guard !busy else { return }
        guard let token = ArchiveActivity.shared.enter(backup: true) else {
            message(String(localizationKey: "Archive_Busy")); return
        }
        busy = true; tableView.reloadData()
        DispatchQueue.global(qos: .userInitiated).async {
            let outcome: String?
            do { outcome = try action() } catch { outcome = error.localizedDescription }
            token.end()
            DispatchQueue.main.async {
                self.busy = false; self.tableView.reloadData()
                if let outcome = outcome { self.message(outcome) }
            }
        }
    }
    private func scoped<T>(_ body: (URL) throws -> T) throws -> T {
        let url = try directory(); let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let anchor = try ArchiveLocation.open(url)
        return try withExtendedLifetime(anchor) { try body(url) }
    }
    private func reloadArchives() {
        guard UserDefaults.standard.data(forKey: bookmarkKey) != nil else { return }
        work { try self.scoped { url in let loaded = try ConfigurationBackupStorage.records(in: url); DispatchQueue.main.sync { self.records = loaded; self.folderName = url.lastPathComponent } }; return nil }
    }
    func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
        guard let url = urls.first, url.isFileURL else { return }
        work {
            let access = url.startAccessingSecurityScopedResource()
            defer { if access { url.stopAccessingSecurityScopedResource() } }
            _ = try ArchiveLocation.identity(url)
            let bookmark = try url.bookmarkData(options: [.minimalBookmark], includingResourceValuesForKeys: nil, relativeTo: nil)
            UserDefaults.standard.set(bookmark, forKey: self.bookmarkKey)
            UserDefaults.standard.removeObject(forKey: self.consentKey)
            let loaded = try ConfigurationBackupStorage.records(in: url); DispatchQueue.main.sync { self.records = loaded; self.folderName = url.lastPathComponent }
            return nil
        }
    }
    private func create() {
        // Resolve identity off-main, then bind confirmation to that exact directory.
        work {
            let identity = try self.scoped { try ArchiveLocation.identity($0) }
            DispatchQueue.main.async { self.confirmRetention(identity) }
            return nil
        }
    }
    private func confirmRetention(_ identity: String) {
        // work's completion clears busy in the same main-queue turn after this callback.
        DispatchQueue.main.async {
            let titleKey = UserDefaults.standard.string(forKey: self.consentKey) == identity ? "Archive_CreateTitle" : "Archive_SharedConfirmTitle"
            let alert = UIAlertController(title: String(localizationKey: titleKey), message: nil, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: String(localizationKey: "Archive_Cancel"), style: .cancel))
            alert.addAction(UIAlertAction(title: String(localizationKey: "Archive_Agree"), style: .destructive) { _ in
                UserDefaults.standard.set(identity, forKey: self.consentKey)
                self.capture(identity)
            })
            self.present(alert, animated: true)
        }
    }
    private func capture(_ identity: String) {
        work {
            try self.scoped { url in
                guard try ArchiveLocation.identity(url) == identity else {
                    UserDefaults.standard.removeObject(forKey: self.consentKey)
                    throw ConfigurationBackupError(String(localizationKey: "Archive_Changed"))
                }
                guard let snapshot = try ArchiveService.perform("capture") else {
                    throw ConfigurationBackupError(String(localizationKey: "Archive_NoData"))
                }
                let record = try ConfigurationBackupStorage.publish(snapshot, in: url, expectedIdentity: identity)
                let loaded = try ConfigurationBackupStorage.records(in: url); DispatchQueue.main.sync { self.records = loaded; self.folderName = url.lastPathComponent }
                return record.cleanupWarning ?? String(localizationKey: "Archive_Created")
            }
        }
    }
    private func chooseRestore(_ record: ConfigurationBackupRecord) {
        let alert = UIAlertController(title: record.displayName, message: nil, preferredStyle: .alert)
        for (label, operation) in [(String(localizationKey: "Archive_Config"), "restore-config"), (String(localizationKey: "Archive_Sources"), "restore-sources"), (String(localizationKey: "Archive_All"), "restore-all")] {
            alert.addAction(UIAlertAction(title: label, style: .default) { _ in
                let confirm = UIAlertController(title: String(format: String(localizationKey: "Archive_RestoreTitle"), label), message: nil, preferredStyle: .alert)
                confirm.addAction(UIAlertAction(title: String(localizationKey: "Archive_Cancel"), style: .cancel))
                confirm.addAction(UIAlertAction(title: String(localizationKey: "Archive_Restore"), style: .destructive) { _ in
                    self.work {
                        try self.scoped { url in
                            let saved = url.appendingPathComponent(record.url.lastPathComponent, isDirectory: true)
                            let snapshot = try ConfigurationBackupStorage.load(from: saved)
                            defer { if operation != "restore-config" { RepoManager.shared.reloadAfterArchive() } }
                            _ = try ArchiveService.perform(operation, snapshot: snapshot)
                        }
                        return String(localizationKey: "Archive_Restored")
                    }
                })
                self.present(confirm, animated: true)
            })
        }
        alert.addAction(UIAlertAction(title: String(localizationKey: "Archive_Cancel"), style: .cancel)); present(alert, animated: true)
    }
    override func numberOfSections(in tableView: UITableView) -> Int { 2 }
    override func tableView(_ tableView: UITableView, numberOfRowsInSection section: Int) -> Int { section == 0 ? 2 : records.count }
    override func tableView(_ tableView: UITableView, cellForRowAt indexPath: IndexPath) -> UITableViewCell {
        let cell = reusableCell(withStyle: .subtitle, reuseIdentifier: "ConfigurationArchive")
        cell.accessoryType = .disclosureIndicator; cell.accessoryView = nil
        cell.detailTextLabel?.text = nil
        if indexPath.section == 0 {
            cell.textLabel?.text = indexPath.row == 0 ? String(localizationKey: "Archive_Select") : (busy ? String(localizationKey: "Archive_Working") : String(localizationKey: "Archive_Create"))
            if indexPath.row == 0 { cell.detailTextLabel?.text = folderName }
        } else { cell.textLabel?.text = records[indexPath.row].displayName }
        cell.isUserInteractionEnabled = !busy; return cell
    }
    override func tableView(_ tableView: UITableView, didSelectRowAt indexPath: IndexPath) {
        tableView.deselectRow(at: indexPath, animated: true); guard !busy else { return }
        if indexPath.section == 1 { chooseRestore(records[indexPath.row]); return }
        if indexPath.row == 1 { create(); return }
        if #available(iOS 14.0, *) {
            let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder], asCopy: false)
            picker.delegate = self; picker.allowsMultipleSelection = false; present(picker, animated: true)
        } else { message(String(localizationKey: "Archive_Version")) }
    }
    override func tableView(_ tableView: UITableView, titleForHeaderInSection section: Int) -> String? { section == 0 ? String(localizationKey: "Archive_Categories") : String(localizationKey: "Archive_Records") }
}
