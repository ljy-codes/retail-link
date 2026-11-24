import AppKit
import WebKit
import Darwin

private let applicationName = "零售通"
private let applicationVersion = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "0.2.1"
private let bundleIdentifier = "com.retaillink.desktop.mac"
private let applicationStart = ProcessInfo.processInfo.systemUptime

private final class ActionButton: NSButton {
    var perform: (() -> Void)?
    convenience init(_ title: String, handler: @escaping () -> Void) {
        self.init(title: title, target: nil, action: nil)
        perform = handler; target = self; action = #selector(invoke)
        bezelStyle = .rounded
    }
    @objc private func invoke() { perform?() }
}
private struct LifecycleAction {
    let title: String
    var closes = true
    var destructive = false
    let handler: () -> Void
}

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
    private var startupStarted = applicationStart
    private var startupStage = "准备本地数据"
    private var frontendReady = false
    private var lifecyclePanel: NSPanel?
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
        if let path = Bundle.main.resourceURL?.appendingPathComponent("AppIcon.png").path,
            let icon = NSImage(contentsOfFile: path) { NSApp.applicationIconImage = icon }
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
            writeLifecycleLog("process_started", extra: ["actualBundle": Bundle.main.bundleURL.path])
            recordActualInstallation()
            installMenus()
            makeWindow()
            if testRoot != nil && CommandLine.arguments.contains("--lifecycle-preview") {
                showLoading("界面验收预览", detail: "隔离测试模式；本地业务服务未启动。")
                DispatchQueue.main.async { [weak self] in self?.showOnboarding() }
            } else { startBackend() }
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
        let setup = NSMenuItem(title: "安装与升级…", action: #selector(beginInstallation), keyEquivalent: "")
        setup.target = self; appMenu.addItem(setup)
        let guide = NSMenuItem(title: "使用与数据说明…", action: #selector(showOnboarding), keyEquivalent: "")
        guide.target = self; appMenu.addItem(guide)
        let diagnostics = NSMenuItem(title: "显示启动诊断", action: #selector(showDiagnostics), keyEquivalent: "")
        diagnostics.target = self; appMenu.addItem(diagnostics)
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

    private var nativeLogDirectory: URL {
        let library = testRoot?.appendingPathComponent("Library", isDirectory: true)
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library", isDirectory: true)
        return library.appendingPathComponent("Logs/零售通", isDirectory: true)
    }

    private func writeLifecycleLog(_ event: String, extra: [String: Any] = [:]) {
        do {
            try fileManager.createDirectory(at: nativeLogDirectory, withIntermediateDirectories: true)
            var value: [String: Any] = ["event": event, "version": applicationVersion, "pid": getpid(),
                "time": ISO8601DateFormatter().string(from: Date()),
                "elapsedSeconds": ProcessInfo.processInfo.systemUptime - startupStarted,
                "os": ProcessInfo.processInfo.operatingSystemVersionString]
            extra.forEach { value[$0.key] = $0.value }
            var data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
            data.append(0x0a)
            let url = nativeLogDirectory.appendingPathComponent("startup.jsonl")
            if !fileManager.fileExists(atPath: url.path) { fileManager.createFile(atPath: url.path, contents: nil) }
            let handle = try FileHandle(forWritingTo: url)
            try handle.seekToEnd(); try handle.write(contentsOf: data); try handle.close()
        } catch { NSLog("零售通诊断日志未写入：%@", error.localizedDescription) }
    }

    private func recordActualInstallation() {
        let value: [String: Any] = ["bundleIdentifier": bundleIdentifier, "version": applicationVersion,
            "actualBundle": Bundle.main.bundleURL.standardizedFileURL.path,
            "updatedAt": ISO8601DateFormatter().string(from: Date())]
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: supportDirectory.appendingPathComponent("desktop-install.json"), options: .atomic)
        }
    }

    @objc private func showDiagnostics() {
        NSWorkspace.shared.activateFileViewerSelecting([nativeLogDirectory.appendingPathComponent("startup.jsonl")])
    }

    private func showLifecyclePanel(title: String, subtitle: String, body: String,
        accessory: NSView? = nil, actions: [LifecycleAction]) {
        guard !panelActive else { NSSound.beep(); return }
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 680, height: 600),
            styleMask: [.titled], backing: .buffered, defer: false)
        panel.title = "零售通 · \(applicationVersion)"
        let view = NSView(); panel.contentView = view
        let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading
        stack.spacing = 15; stack.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(stack)
        let brand = NSStackView(); brand.orientation = .horizontal; brand.spacing = 14
        let image = NSImageView(image: NSApp.applicationIconImage); image.imageScaling = .scaleProportionallyUpOrDown
        image.translatesAutoresizingMaskIntoConstraints = false
        image.widthAnchor.constraint(equalToConstant: 52).isActive = true
        image.heightAnchor.constraint(equalToConstant: 52).isActive = true
        let heading = NSTextField(labelWithString: title); heading.font = .systemFont(ofSize: 25, weight: .semibold)
        let brandText = NSStackView(); brandText.orientation = .vertical; brandText.alignment = .leading; brandText.spacing = 3
        let versionLabel = NSTextField(labelWithString: "零售通 · v\(applicationVersion) · Mac")
        versionLabel.font = .systemFont(ofSize: 12, weight: .medium); versionLabel.textColor = .systemRed
        brandText.addArrangedSubview(heading); brandText.addArrangedSubview(versionLabel)
        brand.addArrangedSubview(image); brand.addArrangedSubview(brandText); stack.addArrangedSubview(brand)
        let intro = NSTextField(wrappingLabelWithString: subtitle)
        intro.font = .systemFont(ofSize: 14); intro.textColor = .secondaryLabelColor; stack.addArrangedSubview(intro)
        let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
        let text = NSTextView(); text.isEditable = false; text.isSelectable = true
        text.font = .systemFont(ofSize: 13)
        let paragraph = NSMutableParagraphStyle(); paragraph.lineBreakMode = .byCharWrapping; paragraph.lineSpacing = 3
        text.defaultParagraphStyle = paragraph; text.string = body
        text.textContainerInset = NSSize(width: 14, height: 14)
        text.isVerticallyResizable = true; text.isHorizontallyResizable = false
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = false
        text.textContainer?.containerSize = NSSize(width: 568, height: CGFloat.greatestFiniteMagnitude)
        text.textContainer?.lineBreakMode = .byCharWrapping
        scroll.documentView = text; stack.addArrangedSubview(scroll)
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.heightAnchor.constraint(equalToConstant: accessory == nil ? 300 : 245).isActive = true
        scroll.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        text.frame = NSRect(x: 0, y: 0, width: 602, height: 300)
        if let accessory = accessory { stack.addArrangedSubview(accessory) }
        let buttons = NSStackView(); buttons.orientation = .horizontal; buttons.spacing = 10
        for item in actions {
            let button = ActionButton(item.title) { [weak self, weak panel] in
                guard let self = self else { return }
                if item.closes, let panel = panel {
                    self.window.endSheet(panel); panel.orderOut(nil); self.lifecyclePanel = nil; self.panelActive = false
                }
                item.handler()
            }
            if item.destructive { button.contentTintColor = .systemRed }
            buttons.addArrangedSubview(button)
        }
        stack.addArrangedSubview(buttons)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 30),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -30),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 24),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -22)])
        panelActive = true; lifecyclePanel = panel; window.beginSheet(panel)
    }

    @objc private func showOnboarding() {
        let actual = Bundle.main.bundleURL.standardizedFileURL.path
        showLifecyclePanel(title: "欢迎使用零售通", subtitle: "把销售、库存和经营任务连在一起。文件在本机处理，核对后再作判断。",
            body: "开始使用\n1. 选择经营工作簿。\n2. 核对商品、仓库、日期和数据问题。\n3. 查看分析，预览后导出结果。\n\n当前应用：\n\(actual)\n\n本地数据：\n\(supportDirectory.path)\n\n默认导出：\n\(exportDirectory.path)\n\n原始 Excel 默认只读；在设置中开启源写回后，每次仍需预览并确认。历史、导入文件和已生成文件与 App 分开保存。\n\n当前业务数据通过本机文件接入；本地服务只监听本机地址，不自动上传工作簿、不定时抓取线上数据。主动打开外部链接时交给系统浏览器。\n\n安装或升级：可选择任意可写的实际目录；升级从新版 App 选择原安装目录，旧 App 保留为恢复副本。",
            actions: [LifecycleAction(title: "选择安装位置", handler: { [weak self] in self?.beginInstallation() }),
                LifecycleAction(title: "打开数据目录", closes: false, handler: { [weak self] in self?.showDataDirectory() }),
                LifecycleAction(title: "开始使用", handler: { [weak self] in
                    guard let self = self else { return }
                    try? Data("{\"version\":2}".utf8).write(to: self.supportDirectory.appendingPathComponent("desktop_onboarding.json"), options: .atomic)
                })])
    }

    @objc private func beginInstallation() {
        guard !panelActive, !uninstalling, !terminating else { NSSound.beep(); return }
        let chooser = NSOpenPanel(); chooser.title = "选择零售通安装位置"
        chooser.message = "选择实际可写的文件夹。升级请选择旧版所在文件夹；运行中的目标 App 不会被替换。"
        chooser.canChooseFiles = false; chooser.canChooseDirectories = true; chooser.canCreateDirectories = true
        chooser.directoryURL = fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Applications")
        panelActive = true
        chooser.beginSheetModal(for: window) { [weak self] response in
            guard let self = self else { return }; self.panelActive = false
            guard response == .OK, let directory = chooser.url else { return }
            let target = directory.appendingPathComponent("零售通.app")
            do {
                try LifecycleFiles.rejectLinks(target)
                let expected = LifecycleFiles.exists(target) ? try LifecycleFiles.digest(target) : nil
                if expected != nil { _ = try LifecycleFiles.bundleInfo(target) }
                self.showLifecyclePanel(title: expected == nil ? "确认安装位置" : "确认升级位置",
                    subtitle: "复制通过校验后才启用新版；应用数据与原始文件保持原位。",
                    body: "版本：\(applicationVersion)\n\n当前来源：\(Bundle.main.bundleURL.path)\n\n实际目标：\(target.path)\n\n\(expected == nil ? "将新建 App，不覆盖已有文件。" : "旧 App 将保留在同目录的“零售通-升级前-…”恢复副本中，不自动删除。")\n\n安装完成后可以立即启动、打开安装目录或完成。Mac 不创建 Windows 开始菜单或未经选择的桌面快捷方式。",
                    actions: [LifecycleAction(title: "返回选择", handler: { [weak self] in self?.beginInstallation() }),
                        LifecycleAction(title: "确认安装", handler: { [weak self] in self?.performInstallation(directory: directory, expected: expected) }),
                        LifecycleAction(title: "取消", handler: {})])
            } catch { self.showNativeNotice("无法选择此位置", detail: error.localizedDescription) }
        }
    }

    private func performInstallation(directory: URL, expected: String?) {
        showLoading("正在复制并校验 App…", detail: "原始文件与本地历史保留。请等待安装结果。")
        let source = Bundle.main.bundleURL
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let target = directory.appendingPathComponent("零售通.app").standardizedFileURL
                guard !NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).contains(where: {
                    $0.bundleURL?.standardizedFileURL == target
                }) else { throw LifecycleFailure.message("目标 App 正在运行。请退出目标旧版，从新版磁盘映像重新安装。") }
                let result = try LifecycleFiles.install(source: source, directory: directory, expectedTarget: expected, isRunning: { candidate in
                    NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).contains { $0.bundleURL?.standardizedFileURL == candidate }
                })
                DispatchQueue.main.async {
                    self.writeLifecycleLog("installation_complete", extra: ["target": result.target.path, "backup": result.backup?.path ?? ""])
                    self.restoreWorkspace()
                    self.showLifecyclePanel(title: "安装完成", subtitle: "零售通 \(applicationVersion) · 本地经营工作空间",
                        body: "实际安装路径：\n\(result.target.path)\n\n恢复副本：\n\(result.backup?.path ?? "首次安装，无旧版副本")\n\n下一步：选择经营文件 → 核对数据 → 查看并导出。\n\n本地数据：\(self.supportDirectory.path)\n原始表和生成文件保持原位。",
                        actions: [LifecycleAction(title: "立即启动", handler: { [weak self] in self?.launchInstalled(result.target) }),
                            LifecycleAction(title: "打开安装目录", closes: false, handler: { NSWorkspace.shared.activateFileViewerSelecting([result.target]) }),
                            LifecycleAction(title: "完成", handler: {})])
                }
            } catch {
                DispatchQueue.main.async {
                    self.restoreWorkspace()
                    self.writeLifecycleLog("installation_failed", extra: ["reason": error.localizedDescription])
                    self.showLifecyclePanel(title: "安装未完成", subtitle: "本地数据与用户文件未清理。",
                        body: error.localizedDescription + "\n\n请选择有写入权限且空间充足的位置。旧版恢复副本如已产生将保留在目标目录。",
                        actions: [LifecycleAction(title: "重新选择", handler: { [weak self] in self?.beginInstallation() }), LifecycleAction(title: "关闭", handler: {})])
                }
            }
        }
    }

    private func launchInstalled(_ url: URL) {
        quiesceBackend { [weak self] in
            guard let self = self else { return }
            if self.lockDescriptor >= 0 { flock(self.lockDescriptor, LOCK_UN); close(self.lockDescriptor); self.lockDescriptor = -1 }
            let configuration = NSWorkspace.OpenConfiguration(); configuration.createsNewApplicationInstance = true
            if let root = self.testRoot { configuration.environment = ["RETAIL_LINK_TEST_ROOT": root.path] }
            NSWorkspace.shared.openApplication(at: url, configuration: configuration) { _, error in
                DispatchQueue.main.async {
                    if let error = error {
                        let lockURL = self.supportDirectory.appendingPathComponent("desktop.lock")
                        self.lockDescriptor = open(lockURL.path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
                        if self.lockDescriptor >= 0 && flock(self.lockDescriptor, LOCK_EX | LOCK_NB) == 0 { self.startBackend() }
                        self.showNativeNotice("无法启动已安装的 App", detail: error.localizedDescription)
                    } else { NSApp.terminate(nil) }
                }
            }
        }
    }

    private func restoreWorkspace() {
        if frontendReady { loadingView.isHidden = true; webView.isHidden = false }
        else { showLoading("本地工作空间", detail: "可从菜单重新启动工作空间。", failed: true) }
    }

    @objc private func beginUninstall() {
        guard !panelActive, !uninstalling, !terminating else { NSSound.beep(); return }
        let bundle = Bundle.main.bundleURL.standardizedFileURL
        let readOnly = (try? bundle.resourceValues(forKeys: [.volumeIsReadOnlyKey]))?.volumeIsReadOnly == true
        guard !readOnly, fileManager.isWritableFile(atPath: bundle.deletingLastPathComponent().path) else {
            showNativeNotice("当前位置不能卸载", detail: "请从已安装的 App 操作。磁盘映像中的 App 只需推出磁盘，不会清理用户数据。"); return
        }
        guard downloads.isEmpty else { showNativeNotice("请先完成下载", detail: "完成或取消下载后再卸载。"); return }
        let options = NSStackView(); options.orientation = .vertical; options.alignment = .leading; options.spacing = 10
        let ordinary = ActionButton("普通卸载 · 保留配置、历史和日志（默认）", handler: {})
        let complete = ActionButton("彻底卸载 · 应用专属数据也移入废纸篓", handler: {})
        ordinary.setButtonType(.radio); complete.setButtonType(.radio)
        ordinary.perform = { [weak ordinary, weak complete] in ordinary?.state = .on; complete?.state = .off }
        complete.perform = { [weak ordinary, weak complete] in ordinary?.state = .off; complete?.state = .on }
        ordinary.state = .on
        options.addArrangedSubview(ordinary); options.addArrangedSubview(complete)
        showLifecyclePanel(title: "选择卸载方式", subtitle: "用户原始资料、导入文件副本和生成文件始终保留。",
            body: "当前应用：\n\(bundle.path)\n\n普通卸载仅移走当前 App。\n彻底卸载另清理明确列出的应用配置、历史、缓存和日志；未知文件及文件备份保留。\n\n下一步会列出本次实际范围，确认后才开始。移入废纸篓的项目可以恢复。",
            accessory: options, actions: [LifecycleAction(title: "下一步", handler: { [weak self] in self?.confirmUninstall(bundle: bundle, complete: complete.state == .on) }),
                LifecycleAction(title: "取消", handler: {})])
    }

    private func uninstallTargets(bundle: URL, complete: Bool) throws -> [URL] {
        let library = testRoot?.appendingPathComponent("Library", isDirectory: true)
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library", isDirectory: true)
        return try LifecycleFiles.cleanupPlan(bundle: bundle, support: supportDirectory, library: library, complete: complete)
    }

    private func confirmUninstall(bundle: URL, complete: Bool) {
        do {
            let urls = try uninstallTargets(bundle: bundle, complete: complete)
            showLifecyclePanel(title: complete ? "确认彻底卸载" : "确认普通卸载", subtitle: "请核对下面的实际范围。此步骤仍可取消。",
                body: "将移入废纸篓：\n" + urls.map { $0.path }.joined(separator: "\n") + "\n\n始终保留：\n原始资料及导入文件\n\(importDirectory.path)\n\(exportDirectory.path)\n应用数据目录中的 exports 文件备份、writeback/backups 原件备份、writeback/operations 恢复记录与未在清理名单内的文件\n\n本次方式：\(complete ? "彻底卸载" : "普通卸载")",
                actions: [LifecycleAction(title: "取消", handler: {}), LifecycleAction(title: "确认卸载", destructive: complete, handler: { [weak self] in
                    self?.performUninstall(urls: urls, complete: complete)
                })])
        } catch { showNativeNotice("无法准备卸载", detail: error.localizedDescription) }
    }

    private func performUninstall(urls: [URL], complete: Bool) {
        uninstalling = true
        showLoading("正在卸载零售通…", detail: "正在停止本地服务；完成后会显示实际结果。")
        quiesceBackend { [weak self] in
            guard let self = self else { return }
            NSWorkspace.shared.recycle(urls) { [weak self] recovered, error in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    let moved = recovered
                    let failed = urls.filter { LifecycleFiles.exists($0) }
                    let mode = complete ? "彻底卸载" : "普通卸载"
                    let movedLines: [String] = moved.map { entry in entry.key.path + " → " + entry.value.path }
                    let removedText = movedLines.sorted().joined(separator: "\n")
                    let failedText = failed.isEmpty ? "无" : failed.map { $0.path }.joined(separator: "\n")
                    var summary = "方式：" + mode + "\n\n已移入废纸篓：\n" + removedText
                    summary += "\n\n未清理项：\n" + failedText
                    summary += "\n\n已保留：原始资料、导入文件、生成文件、写回备份及恢复记录和未知文件。\n"
                    summary += error?.localizedDescription ?? ""
                    let reportDirectory = self.exportDirectory.deletingLastPathComponent().appendingPathComponent("卸载记录")
                    let report = reportDirectory.appendingPathComponent("卸载结果-\(UUID().uuidString.prefix(8)).txt")
                    var reportStatus = "结果记录：" + report.path
                    do {
                        try self.fileManager.createDirectory(at: reportDirectory, withIntermediateDirectories: true)
                        try Data(summary.utf8).write(to: report, options: .atomic)
                    } catch { reportStatus = "结果记录未能保存：" + error.localizedDescription + "\n请复制本页结果留存。" }
                    self.showLifecyclePanel(title: failed.isEmpty && error == nil ? "卸载完成" : "卸载尚未全部完成",
                        subtitle: "核对结果后退出；已移入废纸篓的项目可恢复。", body: summary + "\n\n" + reportStatus,
                        actions: [LifecycleAction(title: "打开结果记录", closes: false, handler: { NSWorkspace.shared.activateFileViewerSelecting([report]) }),
                            LifecycleAction(title: "退出", handler: { NSApp.terminate(nil) })])
                }
            }
        }
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
        startupStarted = ProcessInfo.processInfo.systemUptime
        frontendReady = false; startupStage = "准备运行环境与本地数据"
        writeLifecycleLog("startup_attempt")
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
            startupStage = "启动本地服务"
            writeLifecycleLog("backend_started", extra: ["backendPid": process.processIdentifier])
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
        guard generation == attempt, let child = backend, !frontendReady else { return }
        let elapsed = ProcessInfo.processInfo.systemUptime - startupStarted
        detailLabel.stringValue = "阶段：\(startupStage) · 已用 \(String(format: "%.1f", elapsed)) 秒\n" +
            (elapsed > 8 ? "仍在启动，正在等待当前阶段完成；可从帮助菜单查看诊断。" : "数据在本机处理。")
        if backendURL == nil, let data = try? Data(contentsOf: file),
            let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
            let raw = value["url"] as? String, let url = URL(string: raw),
            url.scheme == "http", url.host == "127.0.0.1", let port = url.port, (1...65535).contains(port),
            url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
            url.path.isEmpty || url.path == "/",
            let pid = value["pid"] as? NSNumber, pid.int32Value == child.processIdentifier,
            let version = value["version"] as? String, version == applicationVersion {
            backendURL = url; startupStage = "打开工作台并准备数据"
            writeLifecycleLog("backend_ready")
            statusLabel.stringValue = "正在打开经营工作台…"
            webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
        }
        if Date() > startupDeadline {
            writeLifecycleLog("startup_timeout", extra: ["stage": startupStage, "guardSeconds": 40])
            let detail = "阶段：\(startupStage)。总启动等待已超时，请重试或查看启动诊断。\n" + String(backendOutput.suffix(1600))
            quiesceBackend { [weak self] in self?.showLoading("本地工作空间尚未就绪", detail: detail, failed: true) }
        }
    }

    private func markFrontendReady() {
        guard !frontendReady, !terminating, !uninstalling else { return }
        frontendReady = true; startupStage = "工作台可操作"
        startupTimer?.invalidate(); startupTimer = nil
        writeLifecycleLog("frontend_ready", extra: ["processToReadySeconds": ProcessInfo.processInfo.systemUptime - applicationStart])
        progress.stopAnimation(nil); loadingView.isHidden = true; webView.isHidden = false
        window.makeFirstResponder(webView)
        let guide = supportDirectory.appendingPathComponent("desktop_onboarding.json")
        if !fileManager.fileExists(atPath: guide.path) { DispatchQueue.main.async { [weak self] in self?.showOnboarding() } }
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
        startupStage = "准备工作台数据与操作入口"
        writeLifecycleLog("page_loaded")
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
            let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        if action == "frontendReady" { markFrontendReady(); return }
        guard let identifier = body["id"] else { return }
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
        do { try LifecycleFiles.publishDownload(source: pending.staging, destination: pending.destination) }
        catch { showDownloadError(error.localizedDescription) }
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

@main
struct RetailLinkEntry {
    static func main() {
        let application = NSApplication.shared
        let applicationDelegate = RetailLinkApplication()
        application.delegate = applicationDelegate
        application.run()
    }
}
