import AppKit
import Darwin
import Foundation
import WebKit

/// Background `--uitest` JSON-RPC control server over an owner-only (`0600`) AF_UNIX stream socket.
/// Enables N parallel AI agents to drive, inspect, and visually screenshot isolated Greenlight instances
/// without disturbing the user's keyboard, mouse, window focus, or production state.
final class UITestServer {
    weak var app: App?
    let socketPath: String
    private var listenFD: Int32 = -1
    private var stopped = false
    private let q = DispatchQueue(label: "dev.greenlight.uitest.server", qos: .userInitiated)

    /// Injected into every WKWebView under `--uitest` to capture uncaught page errors, rejections & console.error.
    static let errorCollectorJS = """
    window.__uitestErrors = window.__uitestErrors || [];
    window.__uitestConsoleErrors = window.__uitestConsoleErrors || [];
    window.addEventListener('error', e => {
      const loc = e.filename ? ` (${e.filename}:${e.lineno || 0})` : '';
      window.__uitestErrors.push(String(e.message || e.error || 'error') + loc);
    });
    window.addEventListener('unhandledrejection', e => {
      window.__uitestErrors.push(String(e.reason || 'unhandledrejection'));
    });
    if (!window.__uitestConsoleWrapped) {
      window.__uitestConsoleWrapped = true;
      const origErr = console.error;
      console.error = function(...args) {
        try {
          window.__uitestConsoleErrors.push(args.map(a => {
            if (a instanceof Error) return a.stack || a.message;
            try { return typeof a === 'string' ? a : JSON.stringify(a); } catch { return String(a); }
          }).join(' ').slice(0, 500));
        } catch {}
        return origErr.apply(this, args);
      };
    }
    """

    /// Per-process in-memory web storage so `--uitest` instances never share `localStorage`/cookies with each other or the real app.
    private static let nonPersistentStore = WKWebsiteDataStore.nonPersistent()

    static func installErrorCollector(_ cfg: WKWebViewConfiguration) {
        guard uitest else { return }
        cfg.websiteDataStore = nonPersistentStore
        let script = WKUserScript(source: errorCollectorJS, injectionTime: .atDocumentStart, forMainFrameOnly: false)
        cfg.userContentController.addUserScript(script)
    }

    init(app: App, socketPath: String) {
        self.app = app
        self.socketPath = socketPath
    }

    func start() {
        unlink(socketPath)
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { NSLog("uitest: socket() failed: %d", errno); return }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path) - 1
        guard pathBytes.count <= maxLen else {
            NSLog("uitest: socket path too long: %@", socketPath)
            close(fd)
            return
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { buf in
            for (i, b) in pathBytes.enumerated() { buf[i] = b }
            buf[pathBytes.count] = 0
        }

        let oldMask = umask(0o077)
        let bindRes = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        umask(oldMask)
        guard bindRes == 0 else {
            NSLog("uitest: bind(%@) failed: %d", socketPath, errno)
            close(fd)
            return
        }
        chmod(socketPath, 0o600)
        guard listen(fd, 16) == 0 else {
            NSLog("uitest: listen() failed: %d", errno)
            close(fd)
            return
        }
        listenFD = fd
        q.async { [weak self] in self?.acceptLoop() }
    }

    func stop() {
        stopped = true
        if listenFD >= 0 {
            close(listenFD)
            listenFD = -1
        }
        unlink(socketPath)
    }

    private func acceptLoop() {
        while !stopped && listenFD >= 0 {
            let client = accept(listenFD, nil, nil)
            if client < 0 {
                if errno == EINTR { continue }
                break
            }
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                self?.handleClient(client)
            }
        }
    }

    private final class ReplyBox {
        private let lock = NSLock()
        private var value: [String: Any] = ["ok": false, "error": "timeout"]
        func set(_ v: [String: Any]) { lock.lock(); value = v; lock.unlock() }
        func get() -> [String: Any] { lock.lock(); defer { lock.unlock() }; return value }
    }

    private func handleClient(_ fd: Int32) {
        defer { close(fd) }
        var tv = timeval(tv_sec: 10, tv_usec: 0)
        _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        _ = setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        var data = Data()
        var buf = [UInt8](repeating: 0, count: 4096)
        let maxBytes = 1 << 20  // 1 MiB cap per JSON-RPC request
        while data.count < maxBytes {
            let n = read(fd, &buf, buf.count)
            if n <= 0 { break }
            data.append(buf, count: n)
            if data.contains(0x0A) { break }  // newline-delimited JSON request
        }
        guard data.count < maxBytes else {
            writeJSON(fd, ["ok": false, "error": "request too large"])
            return
        }
        guard !data.isEmpty,
              let line = data.split(separator: 0x0A, maxSplits: 1).first,
              let req = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any] else {
            writeJSON(fd, ["ok": false, "error": "invalid JSON request"])
            return
        }
        let sem = DispatchSemaphore(value: 0)
        let box = ReplyBox()
        DispatchQueue.main.async { [weak self] in
            guard let self, let app = self.app else {
                box.set(["ok": false, "error": "app gone"])
                sem.signal()
                return
            }
            self.dispatch(req, app: app) { out in
                box.set(out)
                sem.signal()
            }
        }
        _ = sem.wait(timeout: .now() + 30)
        writeJSON(fd, box.get())
    }

    private func writeJSON(_ fd: Int32, _ obj: [String: Any]) {
        guard var out = try? JSONSerialization.data(withJSONObject: obj) else { return }
        out.append(0x0A)
        out.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var sent = 0
            while sent < out.count {
                let n = write(fd, base.advanced(by: sent), out.count - sent)
                if n <= 0 { break }
                sent += n
            }
        }
    }

    private func webView(for target: String, app: App) -> WKWebView? {
        switch target {
        case "dashboard", "mc", "center": return app.fluid
        case "terminal", "term", "basement", "pipe": return app.termWindow.contentView as? WKWebView
        case "stage": return app.greenlight.voice.stageWeb
        default: return nil
        }
    }

    private func window(for target: String, app: App) -> NSWindow? {
        switch target {
        case "dashboard", "mc", "center": return app.window
        case "terminal", "term", "basement", "pipe": return app.termWindow
        case "settings": return app.settingsWindow
        default: return app.upstairs ? app.window : app.termWindow
        }
    }

    private func dispatch(_ req: [String: Any], app: App, done: @escaping ([String: Any]) -> Void) {
        let cmd = req["cmd"] as? String ?? ""
        switch cmd {
        case "ping":
            done([
                "ok": true,
                "pid": ProcessInfo.processInfo.processIdentifier,
                "greenlightHome": greenlightHome,
                "greenlightCode": greenlightCode,
                "tmuxSocket": tmuxSocketName,
                "uitest": uitest,
            ])

        case "state":
            collectWebHealth(app: app) { errors, consoleErrors, domNodes in
                let rssMb = (Double(Diag.rss()) / 1048576.0 * 10.0).rounded() / 10.0
                let cpuSecs = (Diag.cpu() * 1000.0).rounded() / 1000.0
                let openFds = (try? FileManager.default.contentsOfDirectory(atPath: "/dev/fd").count) ?? -1
                done([
                    "ok": true,
                    "upstairs": app.upstairs,
                    "wantFloor": app.wantFloor ?? "",
                    "termSide": app.termSide,
                    "chatShown": app.chatShown,
                    "settingsOpen": app.greenlight.settingsOpen,
                    "dashPuckCount": app.greenlight.pucks.count,
                    "coreBusy": app.greenlight.run != nil,
                    "feedCount": app.greenlight.feed.lines.count,
                    "webErrors": errors,
                    "consoleErrors": consoleErrors,
                    "metrics": [
                        "rssMb": rssMb,
                        "cpuSecs": cpuSecs,
                        "openFds": openFds,
                        "domNodes": domNodes,
                    ],
                    "windows": [
                        "dashboard": ["visible": app.window.isVisible, "width": Int(app.window.frame.width), "height": Int(app.window.frame.height)],
                        "terminal": ["visible": app.termWindow.isVisible, "width": Int(app.termWindow.frame.width), "height": Int(app.termWindow.frame.height)],
                        "settings": ["visible": app.settingsWindow.isVisible, "width": Int(app.settingsWindow.frame.width), "height": Int(app.settingsWindow.frame.height)],
                    ],
                ])
            }

        case "eval":
            let target = req["target"] as? String ?? "dashboard"
            guard let js = req["js"] as? String, let web = webView(for: target, app: app) else {
                return done(["ok": false, "error": "invalid target or missing js"])
            }
            // Wrap in JSON.stringify so any JS value (objects, arrays, primitives, undefined) crosses cleanly.
            let wrapped = "(() => { const __v = (\(js)); return __v === undefined ? null : JSON.stringify(__v); })()"
            web.evaluateJavaScript(wrapped) { val, err in
                if let err {
                    return done(["ok": false, "error": err.localizedDescription])
                }
                if let s = val as? String, let d = s.data(using: .utf8),
                   let parsed = try? JSONSerialization.jsonObject(with: d, options: .fragmentsAllowed) {
                    return done(["ok": true, "result": parsed])
                }
                done(["ok": true, "result": NSNull()])
            }

        case "action":
            let name = req["name"] as? String ?? ""
            switch name {
            case "floor":
                let to = req["to"] as? String ?? "basement"
                if to == "center" || to == "greenlight" {
                    app.show()
                } else {
                    app.wantFloor = to
                    (app.termWindow.contentView as? WKWebView)?.callJS("window.term && term.opens", [to])
                    app.basement()
                }
                done(["ok": true, "upstairs": app.upstairs, "floor": to])
            case "toggleChat":
                app.setChat(hidden: !app.chatHidden)
                done(["ok": true, "chatShown": app.chatShown])
            case "openSettings":
                app.greenlight.settingsOpen = true
                app.presentWindow(app.settingsWindow)
                done(["ok": true, "settingsOpen": true])
            case "closeSettings":
                app.greenlight.settingsOpen = false
                app.settingsWindow.orderOut(nil)
                done(["ok": true, "settingsOpen": false])
            case "inboxPing":  // --text "<question>\nOptions: a | b" [--path <workspace>]; not Core.ping, whose banner needs a bundle
                guard let text = req["text"] as? String else { return done(["ok": false, "error": "missing text"]) }
                let (said, options) = splitOptions(text), ws = req["path"] as? String
                app.greenlight.questions.append(Question(text: said, options: options, ping: true, key: ws, ws: ws.map { [$0] }))
                done(["ok": true, "waiting": app.greenlight.waiting.count])
            case "inboxConfirm":  // Clear all's question in the footer, as ⇧⌫ asks it
                app.greenlight.inboxConfirm = true
                done(["ok": true])
            case "clearInbox":
                app.greenlight.clearInbox()
                done(["ok": true, "waiting": app.greenlight.waiting.count])
            case "openInbox", "closeInbox":
                app.greenlight.inboxOpen = name == "openInbox"
                done(["ok": true, "waiting": app.greenlight.waiting.count])
            case "rescan":
                app.rescan()
                done(["ok": true])
            case "chatClear":
                app.greenlight.clear()
                done(["ok": true])
            case "sendChat":
                guard let text = req["text"] as? String, !text.isEmpty else {
                    return done(["ok": false, "error": "missing text"])
                }
                app.greenlight.send(text)
                done(["ok": true])
            case "openPalette":
                app.basement()
                (app.termWindow.contentView as? WKWebView)?.callJS("window.term && term.palette", [true])
                done(["ok": true])
            case "closePalette":
                (app.termWindow.contentView as? WKWebView)?.callJS("window.term && term.palette", [false])
                done(["ok": true])
            case "clearErrors":
                let js = "(() => { window.__uitestErrors = []; window.__uitestConsoleErrors = []; return true; })()"
                app.fluid.evaluateJavaScript(js, completionHandler: nil)  // safety-ok
                (app.termWindow.contentView as? WKWebView)?.evaluateJavaScript(js, completionHandler: nil)  // safety-ok
                done(["ok": true])
            case "openWorkspace":
                guard let path = req["path"] as? String else { return done(["ok": false, "error": "missing path"]) }
                app.toWorkspace(path)
                done(["ok": true])
            case "resize":
                let w = req["width"] as? Double ?? 1300
                let h = req["height"] as? Double ?? 900
                let target = req["window"] as? String ?? "active"
                guard let win = window(for: target, app: app) else { return done(["ok": false, "error": "no window"]) }
                var f = win.frame
                f.size = NSSize(width: w, height: h)
                win.setFrame(f, display: true)
                done(["ok": true, "width": Int(win.frame.width), "height": Int(win.frame.height)])
            case "quit":
                done(["ok": true])
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { NSApp.terminate(nil) }
            default:
                done(["ok": false, "error": "unknown action: \(name)"])
            }

        case "screenshot":
            let target = req["window"] as? String ?? "active"
            guard let path = req["path"] as? String, !path.isEmpty else {
                return done(["ok": false, "error": "missing path"])
            }
            guard let win = window(for: target, app: app) else {
                return done(["ok": false, "error": "unknown window: \(target)"])
            }
            if !win.isVisible { app.presentWindow(win) }
            win.displayIfNeeded()
            app.snap(win) { img in
                guard let img,
                      let tiff = img.tiffRepresentation,
                      let rep = NSBitmapImageRep(data: tiff),
                      let png = rep.representation(using: .png, properties: [:]) else {
                    return done(["ok": false, "error": "snapshot failed"])
                }
                do {
                    try png.write(to: URL(fileURLWithPath: path))
                    done(["ok": true, "path": path, "width": Int(img.size.width), "height": Int(img.size.height), "bytes": png.count])
                } catch {
                    done(["ok": false, "error": error.localizedDescription])
                }
            }

        default:
            done(["ok": false, "error": "unknown cmd: \(cmd)"])
        }
    }

    private func collectWebHealth(app: App, done: @escaping ([String: [String]], [String: [String]], [String: Int]) -> Void) {
        var errs = [String: [String]]()
        var consoleErrs = [String: [String]]()
        var domNodes = [String: Int]()
        let targets: [(String, WKWebView?)] = [
            ("dashboard", app.fluid),
            ("terminal", app.termWindow.contentView as? WKWebView),
        ]
        let probeJS = """
        (() => JSON.stringify({
          e: window.__uitestErrors || [],
          c: window.__uitestConsoleErrors || [],
          n: document.getElementsByTagName('*').length
        }))()
        """
        var remaining = targets.count
        for (name, web) in targets {
            guard let web else {
                errs[name] = []
                consoleErrs[name] = []
                domNodes[name] = 0
                remaining -= 1
                if remaining == 0 { done(errs, consoleErrs, domNodes) }
                continue
            }
            web.evaluateJavaScript(probeJS) { val, _ in  // safety-ok
                if let s = val as? String, let d = s.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                    errs[name] = obj["e"] as? [String] ?? []
                    consoleErrs[name] = obj["c"] as? [String] ?? []
                    domNodes[name] = obj["n"] as? Int ?? 0
                } else {
                    errs[name] = []
                    consoleErrs[name] = []
                    domNodes[name] = 0
                }
                remaining -= 1
                if remaining == 0 { done(errs, consoleErrs, domNodes) }
            }
        }
    }
}
