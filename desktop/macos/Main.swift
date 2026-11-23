import AppKit
import WebKit
import Darwin

private let applicationName = "零售通"
private let applicationVersion = "0.1.5"
private let bundleIdentifier = "com.retaillink.desktop.mac"

private struct PendingDownload {
    let download: WKDownload
    let directory: URL
    let staging: URL
    let destination: URL
}

final class RetailLinkApplication: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var loadingView: NSView!
    private var progress: NSProgressIndicator!
    private var statusLabel: NSTextField!
    private var detailLabel: NSTextField!
    private var retryButton: NSButton!
    private var quitButton: NSButton!
    private var backend: Process?
    private var outputPipe: Pipe?
    private var parentStdinWriter: FileHandle?
    private var startupTimer: Timer?
    private var startupDeadline = Date()
    private var launchDirectory: URL?
    private var backendURL: URL?
    private var backendOutput = ""
    private var generation = UUID()
    private var terminating = false
    private var panelActive = false
    private var stoppingBackend = false
    private var stopCallbacks: [() -> Void] = []
    private var uninstalling = false
    private var lockDescriptor: Int32 = -1
    private var downloads: [ObjectIdentifier: PendingDownload] = [:]
    private let fileManager = FileManager.default
    private var testRoot: URL? {
        guard let path = ProcessInfo.processInfo.environment["RETAIL_LINK_TEST_ROOT"], !path.isEmpty else { return nil }
        return URL(fileURLWithPath: (path as NSString).expandingTildeInPath, isDirectory: true).standardizedFileURL
    }
    private var supportDirectory: URL {
        if let root = testRoot { return root.appendingPathComponent("ApplicationSupport/零售通", isDirectory: true) }
        return fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(applicationName, isDirectory: true)
    }
    private var importDirectory: URL { supportDirectory.appendingPathComponent("导入文件", isDirectory: true) }
    private var exportDirectory: URL {
        if let root = testRoot { return root.appendingPathComponent("Documents/零售通/生成文件", isDirectory: true) }
        return fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(applicationName, isDirectory: true)
            .appendingPathComponent("生成文件", isDirectory: true)
    }
    private var temporaryDirectory: URL {
        if let root = testRoot { return root.appendingPathComponent("Temporary", isDirectory: true) }
        return fileManager.temporaryDirectory
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        do {
            try fileManager.createDirectory(at: supportDirectory, withIntermediateDirectories: true)
            let lockURL = supportDirectory.appendingPathComponent("desktop.lock")
            lockDescriptor = open(lockURL.path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
            guard lockDescriptor >= 0 else { throw nativeError("无法创建应用实例锁，请检查应用数据目录权限。") }
            if flock(lockDescriptor, LOCK_EX | LOCK_NB) != 0 {
                let existing = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
                    .first { $0.processIdentifier != getpid() }
                existing?.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
                NSApp.terminate(nil)
                return
            }
            installMenus()
            makeWindow()
            startBackend()
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        } catch {
            let alert = NSAlert()
            alert.messageText = "无法启动\(applicationName)"
            alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "退出")
            alert.runModal()
            NSApp.terminate(nil)
        }
    }

    private func nativeError(_ description: String) -> NSError {
        NSError(domain: bundleIdentifier, code: 1, userInfo: [NSLocalizedDescriptionKey: description])
    }

    private func installMenus() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: applicationName)
        let about = NSMenuItem(title: "关于\(applicationName)", action: #selector(showAbout), keyEquivalent: "")
        about.target = self
        appMenu.addItem(about)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏\(applicationName)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "显示全部", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let uninstall = NSMenuItem(title: "卸载零售通…", action: #selector(beginUninstall), keyEquivalent: "")
        uninstall.target = self
        appMenu.addItem(uninstall)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出\(applicationName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        for (title, selector, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"),
            ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        editItem.submenu = edit
        menu.addItem(editItem)
        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "窗口")
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowMenu.addItem(.separator())
        windowMenu.addItem(withTitle: "前置全部窗口", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: "")
        windowItem.submenu = windowMenu
        menu.addItem(windowItem)
        NSApp.windowsMenu = windowMenu
        let helpItem = NSMenuItem()
        let helpMenu = NSMenu(title: "帮助")
        let data = NSMenuItem(title: "显示本地数据位置", action: #selector(showDataDirectory), keyEquivalent: "")
        data.target = self
        helpMenu.addItem(data)
        helpItem.submenu = helpMenu
        menu.addItem(helpItem)
        NSApp.mainMenu = menu
    }

    private func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 850),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = applicationName
        window.minSize = NSSize(width: 880, height: 620)
        window.center()
        if testRoot == nil { window.setFrameAutosaveName("RetailLinkMainWindow") }
        window.delegate = self
        window.isReleasedWhenClosed = false
        let content = NSView()
        window.contentView = content
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = testRoot == nil ? .default() : .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        configuration.userContentController.add(self, name: "retailLinkDesktop")
        configuration.userContentController.addUserScript(WKUserScript(
            source: "window.__RETAIL_LINK_DESKTOP__ = Object.freeze({platform:'mac',version:'\(applicationVersion)'});",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.isHidden = true
        fill(webView, in: content)
        loadingView = NSView()
        fill(loadingView, in: content)
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        loadingView.addSubview(stack)
        let title = NSTextField(labelWithString: applicationName)
        title.font = .systemFont(ofSize: 30, weight: .semibold)
        stack.addArrangedSubview(title)
        progress = NSProgressIndicator()
        progress.style = .spinning
        progress.controlSize = .regular
        progress.isIndeterminate = true
        stack.addArrangedSubview(progress)
        statusLabel = NSTextField(labelWithString: "正在启动本地工作空间…")
        statusLabel.font = .systemFont(ofSize: 16, weight: .medium)
        stack.addArrangedSubview(statusLabel)
        detailLabel = NSTextField(wrappingLabelWithString: "数据在本机处理，请稍候。")
        detailLabel.alignment = .center
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 8
        stack.addArrangedSubview(detailLabel)
        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 12
        retryButton = NSButton(title: "重新启动", target: self, action: #selector(retryStartup))
        retryButton.bezelStyle = .rounded
        quitButton = NSButton(title: "退出", target: NSApp, action: #selector(NSApplication.terminate(_:)))
        quitButton.bezelStyle = .rounded
        buttons.addArrangedSubview(retryButton)
        buttons.addArrangedSubview(quitButton)
        stack.addArrangedSubview(buttons)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: loadingView.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: loadingView.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualToConstant: 640),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: loadingView.leadingAnchor, constant: 40),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: loadingView.trailingAnchor, constant: -40),
            detailLabel.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
    }

    private func fill(_ view: NSView, in parent: NSView) {
        view.translatesAutoresizingMaskIntoConstraints = false
        parent.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: parent.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: parent.trailingAnchor),
            view.topAnchor.constraint(equalTo: parent.topAnchor),
            view.bottomAnchor.constraint(equalTo: parent.bottomAnchor)
        ])
    }

    @objc private func showAbout() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: applicationName,
            .applicationVersion: applicationVersion, .version: "Mac 试用版",
            .credits: NSAttributedString(string: "本地经营工作空间 · 数据保存在本机")])
    }

    @objc private func showDataDirectory() {
        NSWorkspace.shared.activateFileViewerSelecting([supportDirectory])
    }

    @objc private func beginUninstall() {
        guard !panelActive, !uninstalling, !terminating else { NSSound.beep(); return }
        let bundle = Bundle.main.bundleURL.standardizedFileURL
        let readOnly = (try? bundle.resourceValues(forKeys: [.volumeIsReadOnlyKey]))?.volumeIsReadOnly == true
        guard bundle.pathExtension == "app", !readOnly,
            fileManager.isWritableFile(atPath: bundle.deletingLastPathComponent().path),
            fileManager.isWritableFile(atPath: bundle.path) else {
            showNativeNotice("请先正常安装 App", detail: "当前应用位于只读磁盘映像或不可写位置。请先将零售通复制到“应用程序”或个人 Applications 文件夹，再从已安装的 App 中卸载。")
            return
        }
        guard downloads.isEmpty else {
            showNativeNotice("请先完成下载", detail: "完成或取消正在进行的下载后，再卸载零售通。")
            return
        }
        let choice = NSAlert()
        choice.alertStyle = .warning
        choice.messageText = "卸载零售通"
        choice.informativeText = "普通卸载：只将 App 移入废纸篓，保留配置、历史和日志。\n\n彻底卸载：同时将应用专属配置、历史、缓存和日志移入废纸篓。\n\n用户原始文件、导入文件和生成文件始终保留。废纸篓中的项目可恢复。"
        choice.addButton(withTitle: "普通卸载")
        choice.addButton(withTitle: "彻底卸载")
        choice.addButton(withTitle: "取消")
        panelActive = true
        choice.beginSheetModal(for: window) { [weak self] result in
            guard let self = self else { return }
            self.panelActive = false
            guard result == .alertFirstButtonReturn || result == .alertSecondButtonReturn else { return }
            self.confirmUninstall(bundle: bundle, complete: result == .alertSecondButtonReturn)
        }
    }

    private func confirmUninstall(bundle: URL, complete: Bool) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = complete ? "确认彻底卸载？" : "确认普通卸载？"
        let mode = complete ? "App、应用配置、历史记录、缓存和日志将移入废纸篓。" : "仅 App 移入废纸篓；配置、历史和日志保留。"
        alert.informativeText = "\(mode)\n\n应用位置：\(bundle.path)\n\n用户原始文件、“导入文件”目录和 Documents 中的生成文件不会移动或删除。卸载完成后应用退出。"
        alert.addButton(withTitle: "确认卸载")
        alert.addButton(withTitle: "取消")
        panelActive = true
        alert.beginSheetModal(for: window) { [weak self] result in
            guard let self = self else { return }
            self.panelActive = false
            guard result == .alertFirstButtonReturn else { return }
            self.uninstalling = true
            self.showLoading("正在卸载零售通…", detail: "正在结束本地服务，源文件与生成文件保持原位。")
            self.quiesceBackend { [weak self] in
                guard let self = self else { return }
                do {
                    let urls = try self.uninstallTargets(bundle: bundle, complete: complete)
                    NSWorkspace.shared.recycle(urls) { [weak self] _, error in
                        DispatchQueue.main.async {
                            guard let self = self else { return }
                            if let error = error {
                                self.uninstalling = false
                                self.showLoading("卸载尚未全部完成", detail: "\(error.localizedDescription)\n已经移入废纸篓的项目可恢复，用户源文件和生成文件未被清理。", failed: true)
                            } else { NSApp.terminate(nil) }
                        }
                    }
                } catch {
                    self.uninstalling = false
                    self.showLoading("卸载未完成", detail: error.localizedDescription, failed: true)
                }
            }
        }
    }

    private func uninstallTargets(bundle: URL, complete: Bool) throws -> [URL] {
        guard complete else { return [bundle] }
        var urls = [bundle]
        // Imported source files can be the user's only remaining copy. Even a
        // full uninstall preserves the import directory and all user outputs.
        if fileManager.fileExists(atPath: supportDirectory.path) {
            let children = try fileManager.contentsOfDirectory(at: supportDirectory,
                includingPropertiesForKeys: nil, options: [])
            urls += children.filter { !["导入文件", "原始资料", "生成文件"].contains($0.lastPathComponent) }
        }
        let library = testRoot?.appendingPathComponent("Library", isDirectory: true)
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library", isDirectory: true)
        for path in ["Caches/\(bundleIdentifier)", "Logs/\(applicationName)", "WebKit/\(bundleIdentifier)",
            "HTTPStorages/\(bundleIdentifier)", "Preferences/\(bundleIdentifier).plist",
            "Saved Application State/\(bundleIdentifier).savedState"] {
            let url = library.appendingPathComponent(path)
            if fileManager.fileExists(atPath: url.path) { urls.append(url) }
        }
        return urls
    }

    private func showNativeNotice(_ title: String, detail: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = detail
        alert.addButton(withTitle: "确定")
        alert.beginSheetModal(for: window, completionHandler: nil)
    }

    @objc private func retryStartup() { startBackend() }

    private func showLoading(_ message: String, detail: String, failed: Bool = false) {
        loadingView.isHidden = false
        webView.isHidden = true
        statusLabel.stringValue = message
        detailLabel.stringValue = detail
        retryButton.isHidden = !failed
        quitButton.isHidden = !failed
        if failed { progress.stopAnimation(nil) } else { progress.startAnimation(nil) }
    }

    private func startBackend() {
        guard !terminating, !uninstalling else { return }
        showLoading("正在启动本地工作空间…", detail: "正在准备运行环境和本地数据。")
        quiesceBackend { [weak self] in self?.launchBackend() }
    }

    private func launchBackend() {
        guard !terminating, !uninstalling else { return }
        generation = UUID()
        let attempt = generation
        backendOutput = ""
        backendURL = nil
        showLoading("正在启动本地工作空间…", detail: "正在准备运行环境和本地数据。")
        do {
            guard let resources = Bundle.main.resourceURL else { throw nativeError("应用资源缺失，请重新复制完整的 App。") }
            let python = resources.appendingPathComponent("runtime/bin/python3")
            let server = resources.appendingPathComponent("app/server.py")
            guard fileManager.isExecutableFile(atPath: python.path), fileManager.fileExists(atPath: server.path) else {
                throw nativeError("内置运行环境不完整，请重新复制完整的 App。")
            }
            try fileManager.createDirectory(at: importDirectory, withIntermediateDirectories: true)
            try fileManager.createDirectory(at: exportDirectory, withIntermediateDirectories: true)
            let temporary = temporaryDirectory.appendingPathComponent("retail-link-\(UUID().uuidString)", isDirectory: true)
            try fileManager.createDirectory(at: temporary, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
            launchDirectory = temporary
            let readyFile = temporary.appendingPathComponent("backend-ready.json")
            let process = Process()
            process.executableURL = python
            process.currentDirectoryURL = resources.appendingPathComponent("app", isDirectory: true)
            process.arguments = ["-B", server.path, "--desktop", "--parent-stdin", "--port", "0",
                "--data-dir", supportDirectory.path, "--original-dir", importDirectory.path,
                "--export-dir", exportDirectory.path, "--ready-file", readyFile.path]
            var environment = ProcessInfo.processInfo.environment
            environment.removeValue(forKey: "PYTHONHOME")
            environment.removeValue(forKey: "PYTHONPATH")
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONNOUSERSITE"] = "1"
            environment["PYTHONUNBUFFERED"] = "1"
            process.environment = environment
            let lifeline = Pipe()
            parentStdinWriter = lifeline.fileHandleForWriting
            let reader = lifeline.fileHandleForReading
            defer { reader.closeFile() }
            // Only fd 0 reaches Python. The parent's write end must close on
            // exec so a forcibly terminated App produces EOF in the child.
            let flags = fcntl(lifeline.fileHandleForWriting.fileDescriptor, F_GETFD)
            guard flags >= 0,
                fcntl(lifeline.fileHandleForWriting.fileDescriptor, F_SETFD, flags | FD_CLOEXEC) != -1 else {
                throw nativeError("无法建立本地服务退出通道，请重新启动。")
            }
            process.standardInput = reader
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            outputPipe = pipe
            pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                guard !data.isEmpty else { handle.readabilityHandler = nil; return }
                let text = String(decoding: data, as: UTF8.self)
                DispatchQueue.main.async {
                    guard let self = self, self.generation == attempt else { return }
                    self.backendOutput += text
                    if self.backendOutput.count > 16000 { self.backendOutput = String(self.backendOutput.suffix(16000)) }
                }
            }
            process.terminationHandler = { [weak self] child in
                DispatchQueue.main.async {
                    guard let self = self, !self.terminating, self.generation == attempt else { return }
                    self.startupTimer?.invalidate()
                    self.startupTimer = nil
                    self.backendURL = nil
                    self.webView.stopLoading()
                    let detail = self.backendOutput.isEmpty ? "本地服务已退出（状态 \(child.terminationStatus)），可以重新启动。" : String(self.backendOutput.suffix(1800))
                    self.showLoading("本地服务已停止", detail: detail, failed: true)
                }
            }
            backend = process
            try process.run()
            startupDeadline = Date().addingTimeInterval(40)
            startupTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in
                self?.checkReadiness(file: readyFile, attempt: attempt)
            }
        } catch {
            let detail = error.localizedDescription
            quiesceBackend { [weak self] in self?.showLoading("启动未完成", detail: detail, failed: true) }
        }
    }

    private func checkReadiness(file: URL, attempt: UUID) {
        guard generation == attempt, let child = backend else { return }
        if let data = try? Data(contentsOf: file),
            let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
            let raw = value["url"] as? String, let url = URL(string: raw),
            url.scheme == "http", url.host == "127.0.0.1", let port = url.port, (1...65535).contains(port),
            url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
            url.path.isEmpty || url.path == "/",
            let pid = value["pid"] as? NSNumber, pid.int32Value == child.processIdentifier,
            let version = value["version"] as? String, version == applicationVersion {
            startupTimer?.invalidate()
            startupTimer = nil
            backendURL = url
            statusLabel.stringValue = "正在打开经营工作台…"
            webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
        } else if Date() > startupDeadline {
            let detail = backendOutput.isEmpty ? "启动超时，请重试。若仍无法启动，请检查完整 App 是否已复制到本机。" : String(backendOutput.suffix(1800))
            quiesceBackend { [weak self] in self?.showLoading("本地工作空间尚未就绪", detail: detail, failed: true) }
        }
    }

    private func quiesceBackend(completion: @escaping () -> Void) {
        stopCallbacks.append(completion)
        guard !stoppingBackend else { return }
        stoppingBackend = true
        generation = UUID()
        startupTimer?.invalidate()
        startupTimer = nil
        backendURL = nil
        parentStdinWriter?.closeFile()
        parentStdinWriter = nil
        guard let process = backend, process.isRunning else { finishStoppingBackend(); return }
        process.terminationHandler = nil
        process.terminate()
        DispatchQueue.global(qos: .utility).async {
            let deadline = Date().addingTimeInterval(2)
            while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.04) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            process.waitUntilExit()
            DispatchQueue.main.async { [weak self] in self?.finishStoppingBackend() }
        }
    }

    private func finishStoppingBackend() {
        backend = nil
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        outputPipe = nil
        if let directory = launchDirectory { try? fileManager.removeItem(at: directory) }
        launchDirectory = nil
        stoppingBackend = false
        let callbacks = stopCallbacks
        stopCallbacks.removeAll()
        for callback in callbacks { callback() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window?.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !terminating else { return .terminateLater }
        terminating = true
        if backend?.isRunning != true && !stoppingBackend { return .terminateNow }
        quiesceBackend { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        terminating = true
        startupTimer?.invalidate()
        startupTimer = nil
        finishStoppingBackend()
        for pending in downloads.values {
            pending.download.cancel { _ in }
            try? fileManager.removeItem(at: pending.directory)
        }
        downloads.removeAll()
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "retailLinkDesktop")
        if lockDescriptor >= 0 { flock(lockDescriptor, LOCK_UN); close(lockDescriptor); lockDescriptor = -1 }
    }

    private func isLocal(_ url: URL?) -> Bool {
        guard let url = url, let endpoint = backendURL else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == endpoint.port
            && url.user == nil && url.password == nil
    }

    private func isLocalDownload(_ url: URL?) -> Bool {
        guard let url = url else { return false }
        if isLocal(url) { return true }
        if url.scheme == "blob" { return isLocal(URL(string: String(url.absoluteString.dropFirst(5)))) }
        return false
    }

    private func trusted(_ frame: WKFrameInfo) -> Bool {
        guard let endpoint = backendURL else { return false }
        return frame.isMainFrame && frame.securityOrigin.protocol == "http"
            && frame.securityOrigin.host == "127.0.0.1" && frame.securityOrigin.port == endpoint.port
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard isLocal(webView.url) else { return }
        progress.stopAnimation(nil)
        loadingView.isHidden = true
        webView.isHidden = false
        window.makeFirstResponder(webView)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        showLoading("工作台暂时无法打开", detail: error.localizedDescription, failed: true)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        showLoading("工作台暂时无法打开", detail: error.localizedDescription, failed: true)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        showLoading("页面已停止", detail: "可以重新启动工作空间。已保存的历史数据仍在本机。", failed: true)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        if navigationAction.shouldPerformDownload && isLocalDownload(url) {
            decisionHandler(.download)
        } else if isLocal(url) {
            decisionHandler(.allow)
        } else if isLocalDownload(url) {
            decisionHandler(.download)
        } else {
            decisionHandler(.cancel)
            if navigationAction.navigationType == .linkActivated, let url = url,
                ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard isLocalDownload(navigationResponse.response.url) else { decisionHandler(.cancel); return }
        let attachment = (navigationResponse.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition")?.lowercased().contains("attachment") == true
        decisionHandler(attachment || !navigationResponse.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocalDownload(url) { webView.load(navigationAction.request) }
            else if navigationAction.navigationType == .linkActivated, ["http", "https"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
        }
        return nil
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        guard trusted(frame), !panelActive else { completionHandler(nil); return }
        panelActive = true
        let panel = NSOpenPanel()
        panel.title = "选择经营数据"
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { [weak self] result in
            self?.panelActive = false
            completionHandler(result == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        guard trusted(frame), !panelActive else { completionHandler(); return }
        panelActive = true
        let alert = NSAlert()
        alert.messageText = applicationName
        alert.informativeText = message
        alert.addButton(withTitle: "确定")
        alert.beginSheetModal(for: window) { [weak self] _ in self?.panelActive = false; completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard trusted(frame), !panelActive else { completionHandler(false); return }
        panelActive = true
        let alert = NSAlert()
        alert.messageText = "请确认操作"
        alert.informativeText = message
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { [weak self] response in
            self?.panelActive = false
            completionHandler(response == .alertFirstButtonReturn)
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "retailLinkDesktop", trusted(message.frameInfo),
            let body = message.body as? [String: Any], let identifier = body["id"],
            let action = body["action"] as? String else { return }
        let validIdentifier = (identifier as? String).map { !$0.isEmpty && $0.count <= 128 } ?? (identifier is NSNumber)
        guard validIdentifier else { return }
        if action == "showInFolder" {
            let path = (body["path"] as? String) ?? (body["initialPath"] as? String) ?? ""
            let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
            guard !path.isEmpty, fileManager.fileExists(atPath: url.path) else {
                resolve(identifier, ["error": "文件或文件夹不存在。"]); return
            }
            NSWorkspace.shared.activateFileViewerSelecting([url])
            resolve(identifier, ["path": url.path])
            return
        }
        guard ["chooseDirectory", "chooseFile", "saveFile"].contains(action) else {
            resolve(identifier, ["error": "不支持此系统操作。"]); return
        }
        guard !panelActive else { resolve(identifier, ["error": "请先完成当前文件选择。"]); return }
        panelActive = true
        let panel: NSSavePanel
        if action == "saveFile" {
            panel = NSSavePanel()
            panel.title = "选择保存位置"
            panel.nameFieldStringValue = safeFilename(body["filename"] as? String ?? "零售通.xlsx")
            panel.canCreateDirectories = true
        } else {
            let openPanel = NSOpenPanel()
            openPanel.title = action == "chooseDirectory" ? "选择文件夹" : "选择文件"
            openPanel.canChooseDirectories = action == "chooseDirectory"
            openPanel.canChooseFiles = action == "chooseFile"
            openPanel.allowsMultipleSelection = false
            panel = openPanel
        }
        if let path = body["initialPath"] as? String, !path.isEmpty {
            let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
            var directory: ObjCBool = false
            if fileManager.fileExists(atPath: url.path, isDirectory: &directory) {
                panel.directoryURL = directory.boolValue ? url : url.deletingLastPathComponent()
            }
        }
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self = self else { return }
            self.panelActive = false
            if result == .OK, let url = panel.url { self.resolve(identifier, ["path": url.path]) }
            else { self.resolve(identifier, ["cancelled": true]) }
        }
    }

    private func resolve(_ identifier: Any, _ result: [String: Any]) {
        guard isLocal(webView.url), let data = try? JSONSerialization.data(withJSONObject: [identifier, result] as [Any]),
            let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.RetailLinkDesktop && window.RetailLinkDesktop.resolve.apply(window.RetailLinkDesktop, \(json));", completionHandler: nil)
    }

    private func safeFilename(_ filename: String) -> String {
        let name = URL(fileURLWithPath: filename).lastPathComponent.replacingOccurrences(of: ":", with: "-")
        return name.isEmpty || name == "." || name == ".." ? "零售通下载.xlsx" : String(name.prefix(180))
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
        suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        guard !panelActive, response.url == nil || isLocalDownload(response.url) else { completionHandler(nil); return }
        panelActive = true
        let panel = NSSavePanel()
        panel.title = "下载文件"
        panel.message = "请选择新文件名保存；本下载入口不覆盖已有文件。"
        panel.canCreateDirectories = true
        panel.directoryURL = exportDirectory
        var name = safeFilename(suggestedFilename)
        if fileManager.fileExists(atPath: exportDirectory.appendingPathComponent(name).path) {
            let url = URL(fileURLWithPath: name)
            name = url.deletingPathExtension().lastPathComponent + "-下载-" + String(UUID().uuidString.prefix(6))
                + (url.pathExtension.isEmpty ? "" : "." + url.pathExtension)
        }
        panel.nameFieldStringValue = name
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self = self else { completionHandler(nil); return }
            self.panelActive = false
            guard result == .OK, let destination = panel.url else { completionHandler(nil); return }
            guard !self.fileManager.fileExists(atPath: destination.path) else {
                completionHandler(nil)
                self.showDownloadError("目标文件已存在，请重新下载并选择新文件名。原文件保持不变。")
                return
            }
            do {
                let folder = destination.deletingLastPathComponent()
                    .appendingPathComponent(".retail-link-download-\(UUID().uuidString)", isDirectory: true)
                try self.fileManager.createDirectory(at: folder, withIntermediateDirectories: false,
                    attributes: [.posixPermissions: 0o700])
                let staging = folder.appendingPathComponent("download.part")
                self.downloads[ObjectIdentifier(download)] = PendingDownload(download: download,
                    directory: folder, staging: staging, destination: destination)
                completionHandler(staging)
            } catch { completionHandler(nil); self.showDownloadError(error.localizedDescription) }
        }
    }

    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest, decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        decisionHandler(isLocal(request.url) ? .allow : .cancel)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let pending = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
        defer { try? fileManager.removeItem(at: pending.directory) }
        // Both files are on the same filesystem. link() publishes without a
        // replace race: another application's newly created file is preserved.
        if link(pending.staging.path, pending.destination.path) != 0 {
            showDownloadError("无法保存下载文件（\(String(cString: strerror(errno)))）。请选择其他位置或新文件名。已有文件保持不变。")
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let pending = downloads.removeValue(forKey: ObjectIdentifier(download)) {
            try? fileManager.removeItem(at: pending.directory)
        }
        if (error as NSError).code != NSURLErrorCancelled { showDownloadError(error.localizedDescription) }
    }

    private func showDownloadError(_ detail: String) {
        guard !terminating else { return }
        let alert = NSAlert()
        alert.messageText = "下载未完成"
        alert.informativeText = detail
        alert.addButton(withTitle: "确定")
        alert.beginSheetModal(for: window, completionHandler: nil)
    }
}

let application = NSApplication.shared
let applicationDelegate = RetailLinkApplication()
application.delegate = applicationDelegate
application.run()
