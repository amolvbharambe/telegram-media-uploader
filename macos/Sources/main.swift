import AppKit
import Foundation
import WebKit

// MARK: - Backend Discovery & Lifecycle Manager

final class BackendManager {
    private var serverProcess: Process?
    private(set) var baseURL: URL = URL(string: "http://127.0.0.1:8080")!
    private(set) var attachedToExternal = false

    private let appSupportDir: URL = {
        let fm = FileManager.default
        let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dir = support.appendingPathComponent("TelegramUploader", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    var dataDirectory: URL {
        if let repo = findRepoRoot(), FileManager.default.fileExists(atPath: repo.appendingPathComponent("data/db/uploader.db").path) {
            return repo.appendingPathComponent("data", isDirectory: true)
        }
        return appSupportDir.appendingPathComponent("data", isDirectory: true)
    }

    private func findRepoRoot() -> URL? {
        // Walk up dynamically from bundle path to detect dev workspace if run inside repo
        var cursor = Bundle.main.bundleURL.deletingLastPathComponent()
        for _ in 0..<5 {
            let mainPy = cursor.appendingPathComponent("app/main.py")
            if FileManager.default.fileExists(atPath: mainPy.path) {
                return cursor
            }
            let next = cursor.deletingLastPathComponent()
            if next == cursor { break }
            cursor = next
        }
        return nil
    }

    private func augmentedEnvironment(dataDir: URL) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let extraPaths = [
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/Library/Frameworks/Python.framework/Versions/Current/bin"
        ]
        let existingPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = (extraPaths + [existingPath]).joined(separator: ":")
        env["TELEGRAM_UPLOADER_DATA_DIR"] = dataDir.path
        return env
    }

    private func findPython3() -> String? {
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/usr/bin/python3"
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            return path
        }
        return nil
    }

    private func checkServerHealthy(url: URL) -> Bool {
        let statusURL = url.appendingPathComponent("api/status")
        var req = URLRequest(url: statusURL)
        req.timeoutInterval = 1.2
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200,
               let data = data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               json["auth"] != nil && json["config"] != nil {
                ok = true
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.5)
        return ok
    }

    private func isPortAvailable(_ port: UInt16) -> Bool {
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return false }
        defer { close(sock) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return bindResult == 0
    }

    func ensureStarted(onStatus: @escaping (String) -> Void, onReady: @escaping (URL) -> Void, onError: @escaping (String) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            // 1. Reuse existing backend on port 8080 if already running cleanly
            let defaultURL = URL(string: "http://127.0.0.1:8080")!
            if self.checkServerHealthy(url: defaultURL) {
                self.baseURL = defaultURL
                self.attachedToExternal = true
                DispatchQueue.main.async { onReady(defaultURL) }
                return
            }

            // 2. Locate backend code (dev workspace or bundled Resources/backend)
            let backendDir: URL
            let useLocalVenv: Bool
            if let repo = self.findRepoRoot(),
               FileManager.default.isExecutableFile(atPath: repo.appendingPathComponent("venv/bin/python3").path) {
                backendDir = repo
                useLocalVenv = true
            } else if let resPath = Bundle.main.resourcePath {
                let bundled = URL(fileURLWithPath: resPath).appendingPathComponent("backend", isDirectory: true)
                if FileManager.default.fileExists(atPath: bundled.appendingPathComponent("app/main.py").path) {
                    backendDir = bundled
                    useLocalVenv = false
                } else {
                    DispatchQueue.main.async {
                        onError("Backend resources missing inside app bundle.")
                    }
                    return
                }
            } else {
                DispatchQueue.main.async {
                    onError("Could not locate application backend.")
                }
                return
            }

            let dataDir = self.dataDirectory
            try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
            let env = self.augmentedEnvironment(dataDir: dataDir)

            // 3. Ensure Python virtualenv & dependencies exist
            let venvDir = useLocalVenv
                ? backendDir.appendingPathComponent("venv", isDirectory: true)
                : self.appSupportDir.appendingPathComponent("venv", isDirectory: true)
            let venvPython = venvDir.appendingPathComponent("bin/python3")

            if !FileManager.default.isExecutableFile(atPath: venvPython.path) {
                guard let sysPython = self.findPython3() else {
                    DispatchQueue.main.async {
                        onError("Python 3 is required to run the Telegram MTProto engine.\nInstall via: brew install python ffmpeg")
                    }
                    return
                }
                DispatchQueue.main.async { onStatus("First launch setup: creating Python environment...") }
                let mkVenv = Process()
                mkVenv.executableURL = URL(fileURLWithPath: sysPython)
                mkVenv.arguments = ["-m", "venv", venvDir.path]
                mkVenv.environment = env
                try? mkVenv.run()
                mkVenv.waitUntilExit()

                DispatchQueue.main.async { onStatus("First launch setup: installing Telethon & FastAPI engine...") }
                let pipInstall = Process()
                pipInstall.executableURL = venvPython
                pipInstall.arguments = [
                    "-m", "pip", "install", "--quiet", "-r",
                    backendDir.appendingPathComponent("requirements.txt").path
                ]
                pipInstall.environment = env
                try? pipInstall.run()
                pipInstall.waitUntilExit()

                if pipInstall.terminationStatus != 0 {
                    DispatchQueue.main.async {
                        onError("Failed to install Python dependencies into \(venvDir.path). Check internet connectivity.")
                    }
                    return
                }
            }

            // 4. Select port (prefer 8080 if free, else pick free high port)
            var chosenPort: UInt16 = 8080
            if !self.isPortAvailable(8080) {
                chosenPort = (18080...18120).first(where: { self.isPortAvailable($0) }) ?? 18080
            }

            DispatchQueue.main.async { onStatus("Starting resumable MTProto upload server on port \(chosenPort)...") }

            let proc = Process()
            proc.executableURL = venvPython
            proc.currentDirectoryURL = backendDir
            proc.arguments = [
                "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1",
                "--port", String(chosenPort),
                "--timeout-graceful-shutdown", "2"
            ]
            proc.environment = env
            let logURL = self.appSupportDir.appendingPathComponent("backend.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            if let logHandle = try? FileHandle(forWritingTo: logURL) {
                proc.standardOutput = logHandle
                proc.standardError = logHandle
            }

            do {
                try proc.run()
                self.serverProcess = proc
            } catch {
                DispatchQueue.main.async {
                    onError("Failed to launch backend process: \(error.localizedDescription)")
                }
                return
            }

            // 5. Wait up to 15s for server readiness
            let targetURL = URL(string: "http://127.0.0.1:\(chosenPort)")!
            for _ in 0..<50 {
                if !proc.isRunning {
                    DispatchQueue.main.async {
                        onError("Backend exited unexpectedly. Check log at \(logURL.path)")
                    }
                    return
                }
                if self.checkServerHealthy(url: targetURL) {
                    self.baseURL = targetURL
                    DispatchQueue.main.async { onReady(targetURL) }
                    return
                }
                Thread.sleep(forTimeInterval: 0.3)
            }

            DispatchQueue.main.async {
                onError("Timed out waiting for local upload server on port \(chosenPort).")
            }
        }
    }

    func shutdown() {
        guard let proc = serverProcess, proc.isRunning else { return }
        proc.terminate()
        let deadline = Date().addingTimeInterval(2.5)
        while proc.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.1)
        }
        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
        }
        serverProcess = nil
    }
}

// MARK: - Main Desktop Window & App Delegate

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var statusOverlay: NSView!
    private var statusLabel: NSTextField!
    private var spinner: NSProgressIndicator!
    private var retryButton: NSButton!

    private let backend = BackendManager()
    private var pollTimer: Timer?
    private var pendingConfirmAction: (() -> Void)?
    private var liveDataDirectory: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMainMenu()
        setupStatusBarItem()
        setupWindow()
        startBackendAndLoadUI()
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
        backend.shutdown()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            window.makeKeyAndOrderFront(nil)
        }
        return true
    }

    private func setupWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1200, height: 820)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Telegram Media Uploader"
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0.043, green: 0.067, blue: 0.125, alpha: 1.0)
        window.minSize = NSSize(width: 840, height: 600)
        window.setFrameAutosaveName("TelegramUploaderMainWindow")
        if !window.setFrameUsingName("TelegramUploaderMainWindow") {
            window.center()
        }

        let container = NSView(frame: window.contentView!.bounds)
        container.autoresizingMask = [.width, .height]
        window.contentView = container

        // Configure WKWebView with file drop & JS storage enabled
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        webView = WKWebView(frame: container.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        container.addSubview(webView)

        // Native startup / setup overlay
        statusOverlay = NSView(frame: container.bounds)
        statusOverlay.autoresizingMask = [.width, .height]
        statusOverlay.wantsLayer = true
        statusOverlay.layer?.backgroundColor = NSColor(red: 0.043, green: 0.067, blue: 0.125, alpha: 1.0).cgColor

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false

        let titleField = NSTextField(labelWithString: "Telegram Resumable Media Uploader")
        titleField.font = NSFont.systemFont(ofSize: 20, weight: .bold)
        titleField.textColor = .white

        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.startAnimation(nil)

        statusLabel = NSTextField(labelWithString: "Connecting to upload engine...")
        statusLabel.font = NSFont.systemFont(ofSize: 13)
        statusLabel.textColor = NSColor(white: 0.78, alpha: 1.0)
        statusLabel.alignment = .center
        statusLabel.lineBreakMode = .byWordWrapping
        statusLabel.preferredMaxLayoutWidth = 520

        retryButton = NSButton(title: "Retry Startup", target: self, action: #selector(retryStartup))
        retryButton.bezelStyle = .rounded
        retryButton.isHidden = true

        stack.addArrangedSubview(titleField)
        stack.addArrangedSubview(spinner)
        stack.addArrangedSubview(statusLabel)
        stack.addArrangedSubview(retryButton)

        statusOverlay.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: statusOverlay.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: statusOverlay.centerYAnchor)
        ])

        container.addSubview(statusOverlay)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func setupStatusBarItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "TG Uploader"
        let menu = NSMenu()
        menu.addItem(withTitle: "Show Window", action: #selector(showMainWindow), keyEquivalent: "")
        menu.addItem(withTitle: "🧹 Clear Upload Cache...", action: #selector(clearCacheFromMenu), keyEquivalent: "")
        menu.addItem(withTitle: "Open Data Folder", action: #selector(openDataFolder), keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit Telegram Uploader", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        statusItem.menu = menu
    }

    private func setupMainMenu() {
        let mainMenu = NSMenu()

        // App Menu
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Telegram Uploader", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "🧹 Clear Upload Cache...", action: #selector(clearCacheFromMenu), keyEquivalent: "")
        appMenu.addItem(withTitle: "Open Data Folder in Finder", action: #selector(openDataFolder), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Telegram Uploader", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        // File Menu
        let fileMenuItem = NSMenuItem()
        mainMenu.addItem(fileMenuItem)
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Reload Interface", action: #selector(reloadWebView), keyEquivalent: "r")
        fileMenu.addItem(NSMenuItem.separator())
        fileMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileMenuItem.submenu = fileMenu

        // Edit Menu (enables Cmd+C, Cmd+V, Cmd+A inside WebView inputs)
        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        NSApp.mainMenu = mainMenu
    }

    @objc private func retryStartup() {
        retryButton.isHidden = true
        spinner.isHidden = false
        spinner.startAnimation(nil)
        startBackendAndLoadUI()
    }

    private func startBackendAndLoadUI() {
        backend.ensureStarted(
            onStatus: { [weak self] msg in
                self?.statusLabel.stringValue = msg
            },
            onReady: { [weak self] url in
                guard let self = self else { return }
                self.webView.load(URLRequest(url: url))
                self.startStatusPolling()
            },
            onError: { [weak self] errMsg in
                self?.spinner.stopAnimation(nil)
                self?.spinner.isHidden = true
                self?.statusLabel.stringValue = errMsg
                self?.retryButton.isHidden = false
            }
        )
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        statusOverlay.isHidden = true
    }

    private func startStatusPolling() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 4.0, repeats: true) { [weak self] _ in
            self?.refreshAppStatus()
        }
        refreshAppStatus()
    }

    private func refreshAppStatus() {
        let statusURL = backend.baseURL.appendingPathComponent("api/status")
        URLSession.shared.dataTask(with: statusURL) { [weak self] data, _, _ in
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let stats = json["stats"] as? [String: Any] else { return }
            let active = stats["active_tasks"] as? Int ?? 0
            let completed = stats["completed_tasks"] as? Int ?? 0
            DispatchQueue.main.async {
                NSApp.dockTile.badgeLabel = active > 0 ? "\(active)" : nil
                self?.statusItem.button?.title = active > 0 ? "⚡ TG (\(active))" : "TG (\(completed)✓)"
            }
        }.resume()

        let cacheURL = backend.baseURL.appendingPathComponent("api/cache/stats")
        URLSession.shared.dataTask(with: cacheURL) { [weak self] data, _, _ in
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let uploadsPath = json["uploads_dir"] as? String else { return }
            let dataFolder = URL(fileURLWithPath: uploadsPath).deletingLastPathComponent()
            DispatchQueue.main.async {
                self?.liveDataDirectory = dataFolder
            }
        }.resume()
    }

    // MARK: - Actions

    @objc private func showMainWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func reloadWebView() {
        webView.reload()
    }

    @objc private func openDataFolder() {
        NSWorkspace.shared.open(liveDataDirectory ?? backend.dataDirectory)
    }

    @objc private func showAbout() {
        let alert = NSAlert()
        alert.messageText = "Telegram Media Uploader"
        alert.informativeText = "Version 1.3.0\nNative macOS Desktop Application\n\nChunked Resumable MTProto Engine with instant streamable faststart remuxing and in-group duplicate checking."
        alert.alertStyle = .informational
        alert.runModal()
    }

    @objc private func clearCacheFromMenu() {
        showMainWindow()
        webView.evaluateJavaScript("clearUploadCache();", completionHandler: nil)
    }

    // MARK: - WKUIDelegate (Native File/Folder Panels & Alert/Confirm Modals)

    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = !parameters.allowsDirectories
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.resolvesAliases = true
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "Telegram Media Uploader"
        alert.informativeText = message
        alert.alertStyle = .informational
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "Confirm Action"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Confirm")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { resp in
            completionHandler(resp == .alertFirstButtonReturn)
        }
    }
}

// MARK: - App Entrypoint

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
