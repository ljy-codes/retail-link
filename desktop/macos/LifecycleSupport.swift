import Foundation
import CryptoKit
import Darwin

enum LifecycleFailure: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let value) = self { return value }; return nil }
}

/// Filesystem operations shared by the native UI and isolated fixture tests.
enum LifecycleFiles {
    static let applicationID = "com.retaillink.desktop.mac"
    static let removableData = ["history", "history_deleted", "daily.sqlite3", "daily.sqlite3-wal", "daily.sqlite3-shm",
        "preferences.json", "import_templates.json", "dataset_templates.json", "format_baselines.json",
        "desktop_onboarding.json", "desktop-install.json", "logs", "operations/state.json",
        "writeback/settings.json", "writeback/templates.json"]

    static func exists(_ url: URL) -> Bool {
        var info = stat()
        return lstat(url.path, &info) == 0
    }

    static func rejectLinks(_ url: URL) throws {
        var part = url.standardizedFileURL
        while part.path != "/" {
            var info = stat()
            if lstat(part.path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFLNK {
                // Foundation standardizes /private/var to /var on macOS. These
                // root-owned system aliases do not make a selected directory unsafe.
                let systemAliases = ["/var": "private/var", "/tmp": "private/tmp", "/etc": "private/etc"]
                let link = try FileManager.default.destinationOfSymbolicLink(atPath: part.path)
                if let systemTarget = systemAliases[part.path], link == systemTarget || link == "/" + systemTarget {
                    part.deleteLastPathComponent(); continue
                }
                throw LifecycleFailure.message("请选择实际目录，不通过符号链接安装或清理：\(part.path)")
            }
            part.deleteLastPathComponent()
        }
    }

    static func bundleInfo(_ url: URL) throws -> [String: Any] {
        guard url.pathExtension == "app", let data = try? Data(contentsOf: url.appendingPathComponent("Contents/Info.plist")),
            let value = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
            value["CFBundleIdentifier"] as? String == applicationID else {
            throw LifecycleFailure.message("目标不是可识别的零售通 App；不会覆盖此位置。")
        }
        return value
    }

    static func digest(_ url: URL) throws -> String {
        let fm = FileManager.default
        let attributes = try fm.attributesOfItem(atPath: url.path)
        var paths = [""]
        if attributes[.type] as? FileAttributeType == .typeDirectory,
            let iterator = fm.enumerator(atPath: url.path) { paths += iterator.allObjects.compactMap { $0 as? String } }
        var hash = SHA256()
        for relative in paths.sorted() {
            let entry = relative.isEmpty ? url : url.appendingPathComponent(relative)
            let type = try fm.attributesOfItem(atPath: entry.path)[.type] as? FileAttributeType
            hash.update(data: Data("\(relative.utf8.count):\(relative):".utf8))
            if type == .typeSymbolicLink {
                hash.update(data: Data(("L:" + (try fm.destinationOfSymbolicLink(atPath: entry.path))).utf8))
            } else if type == .typeRegular {
                let handle = try FileHandle(forReadingFrom: entry)
                defer { try? handle.close() }
                var contents = SHA256()
                while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty { contents.update(data: data) }
                hash.update(data: Data(contents.finalize()))
            } else { hash.update(data: Data("D".utf8)) }
        }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func verifySignature(_ url: URL) throws {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
        task.arguments = ["--verify", "--deep", "--strict", url.path]
        let output = Pipe(); task.standardOutput = output; task.standardError = output
        try task.run()
        let detail = output.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0 else {
            throw LifecycleFailure.message("App 完整性校验失败，未安装。\n" + String(decoding: detail, as: UTF8.self))
        }
    }

    static func install(source: URL, directory: URL, expectedTarget: String?, verify: Bool = true, isRunning: ((URL) -> Bool)? = nil) throws -> (target: URL, backup: URL?) {
        let fm = FileManager.default
        let source = source.standardizedFileURL
        let directory = directory.standardizedFileURL
        try rejectLinks(directory)
        guard (try? directory.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true,
            fm.isWritableFile(atPath: directory.path) else { throw LifecycleFailure.message("所选目录不可写，请选择个人 Applications 或其他可写目录。") }
        let target = directory.appendingPathComponent("零售通.app", isDirectory: true)
        guard target != source, !directory.path.hasPrefix(source.path + "/") else {
            throw LifecycleFailure.message("当前 App 已在这个位置。升级请从新版磁盘映像打开，再选择原安装目录。")
        }
        _ = try bundleInfo(source)
        if isRunning?(target) == true { throw LifecycleFailure.message("目标 App 正在运行，请先退出旧版。") }
        try rejectLinks(target)
        if exists(target) {
            _ = try bundleInfo(target)
            guard let expected = expectedTarget, try digest(target) == expected else {
                throw LifecycleFailure.message("目标 App 在确认后已变化，请重新选择并确认。")
            }
        } else if expectedTarget != nil { throw LifecycleFailure.message("目标 App 在确认后已移动，请重新选择。") }
        let stage = directory.appendingPathComponent(".零售通-安装中-\(UUID().uuidString).app")
        let backup = exists(target) ? directory.appendingPathComponent("零售通-升级前-\(UUID().uuidString.prefix(8)).app") : nil
        defer { try? fm.removeItem(at: stage) }
        try fm.copyItem(at: source, to: stage)
        let stagedDigest = try digest(stage)
        guard try digest(source) == stagedDigest else { throw LifecycleFailure.message("复制校验失败，原 App 保持不变。") }
        if verify { try verifySignature(stage) }
        if isRunning?(target) == true { throw LifecycleFailure.message("目标 App 在复制期间已启动，升级已停止。") }
        if let backup = backup {
            guard try digest(target) == expectedTarget else { throw LifecycleFailure.message("复制期间原 App 已变化，已停止升级。") }
            try fm.moveItem(at: target, to: backup)
            guard try digest(backup) == expectedTarget else {
                if !exists(target) { try? fm.moveItem(at: backup, to: target) }
                throw LifecycleFailure.message("目标变化，已停止升级并保留旧 App。")
            }
        }
        var published = false
        do {
            guard renamex_np(stage.path, target.path, UInt32(RENAME_EXCL)) == 0 else {
                throw LifecycleFailure.message("安装位置出现同名项目或无法写入；已有文件不会覆盖。\n\(String(cString: strerror(errno)))")
            }
            published = true
            if verify { try verifySignature(target) }
            return (target, backup)
        } catch {
            // Only our exact staged contents may be removed during recovery.
            if published, exists(target), (try? digest(target)) == stagedDigest { try? fm.removeItem(at: target) }
            if let backup = backup, !exists(target) { try? fm.moveItem(at: backup, to: target) }
            throw error
        }
    }

    static func cleanupPlan(bundle: URL, support: URL, library: URL, complete: Bool) throws -> [URL] {
        try rejectLinks(bundle)
        guard complete else { return [bundle] }
        var urls = [bundle]
        for name in removableData {
            let url = support.appendingPathComponent(name)
            if exists(url) { try rejectLinks(url); urls.append(url) }
        }
        for name in ["Caches/\(applicationID)", "Logs/零售通", "WebKit/\(applicationID)",
            "HTTPStorages/\(applicationID)", "Preferences/\(applicationID).plist", "Saved Application State/\(applicationID).savedState"] {
            let url = library.appendingPathComponent(name)
            if exists(url) { try rejectLinks(url); urls.append(url) }
        }
        return urls
    }

    /// The source remains caller-owned. Even cross-volume copies are fully
    /// flushed and verified in a private sibling before exclusive publication.
    /// allowRename=false explicitly exercises copying; direct source moves are
    /// intentionally disabled so all callers have one source-ownership contract.
    static func publishDownload(source: URL, destination: URL, allowRename: Bool = true,
        beforePublish: (() throws -> Void)? = nil) throws {
        let fm = FileManager.default
        let directory = destination.deletingLastPathComponent()
        try rejectLinks(directory)
        guard !exists(destination) else { throw LifecycleFailure.message("保存位置已有同名项目，请使用新文件名；已有文件保持不变。") }
        guard try fm.attributesOfItem(atPath: source.path)[.type] as? FileAttributeType == .typeRegular else {
            throw LifecycleFailure.message("下载暂存文件不是普通文件，未发布。")
        }
        let sourceDigest = try digest(source)
        let stage = directory.appendingPathComponent(".retail-link-publish-\(UUID().uuidString).part")
        let fd = open(stage.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw LifecycleFailure.message("无法创建同目录暂存文件，未发布。\n\(String(cString: strerror(errno)))") }
        var created = stat(); fstat(fd, &created)
        defer {
            var current = stat()
            if lstat(stage.path, &current) == 0, current.st_dev == created.st_dev, current.st_ino == created.st_ino { unlink(stage.path) }
        }
        let writer = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        do {
            let reader = try FileHandle(forReadingFrom: source)
            defer { try? reader.close() }
            while let data = try reader.read(upToCount: 1_048_576), !data.isEmpty { try writer.write(contentsOf: data) }
            try writer.synchronize()
            try writer.close()
            guard try digest(stage) == sourceDigest, try digest(source) == sourceDigest else {
                throw LifecycleFailure.message("复制期间下载文件发生变化，未发布。")
            }
            try beforePublish?()
            var current = stat()
            guard lstat(stage.path, &current) == 0, current.st_dev == created.st_dev, current.st_ino == created.st_ino,
                try digest(stage) == sourceDigest else { throw LifecycleFailure.message("暂存文件在发布前发生变化，未发布。") }
            guard renamex_np(stage.path, destination.path, UInt32(RENAME_EXCL)) == 0 else {
                let code = errno
                if code == EEXIST { throw LifecycleFailure.message("保存期间出现同名项目，已有文件保持不变；请换名重试。") }
                throw LifecycleFailure.message("此目录无法完成排他原子发布，已停止保存且未写入最终文件。请换用支持此操作的磁盘。\n\(String(cString: strerror(code)))")
            }
            guard try digest(destination) == sourceDigest else {
                throw LifecycleFailure.message("发布后的文件被外部修改，请保留文件并核对；未覆盖其他文件。")
            }
            let directoryFD = open(directory.path, O_RDONLY)
            if directoryFD >= 0 { _ = fsync(directoryFD); close(directoryFD) }
        } catch {
            try? writer.close()
            throw error
        }
    }
}
