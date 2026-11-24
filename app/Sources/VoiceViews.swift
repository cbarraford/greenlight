import AppKit
import AVFoundation
import Carbon
import Combine
import CoreAudio
import CryptoKit
import Network
import Security
import Speech
import SwiftUI
import UniformTypeIdentifiers
import UserNotifications
import WebKit
#if canImport(FoundationModels)
import FoundationModels
#endif

// MARK: voice

/// Speech-to-text engines; the right-click menu's Voice submenu picks one.
let appleOnDevice = SFSpeechRecognizer()?.supportsOnDeviceRecognition ?? false  // transcribe() asks for on-device only when this holds
let engines = [("apple", appleOnDevice ? "Apple (on-device)" : "Apple (Apple servers)"), ("whisper", "Whisper (local whisper.cpp)"), ("openai", "OpenAI Whisper API")]
let voiceFile = FileManager.default.temporaryDirectory.appendingPathComponent("greenlight-voice.wav")
var recognizer: SFSpeechRecognizer?  // must outlive its task
func whisperBin() -> String { ud.string(forKey: "whisperBin") ?? "/opt/homebrew/bin/whisper-cli" }
func whisperModel() -> String { ud.string(forKey: "whisperModel") ?? support.deletingLastPathComponent().appendingPathComponent("Greenlight/ggml-large-v3-turbo-q5_0.bin").path }  // one model, copies included

/// Reads (or with `set`, stores) a secret in the login Keychain under service "greenlight".
/// Reads are cached for the launch: Settings asks on every redraw, voice mode every sentence, and each Keychain read can ask your password.
/// A missing key and a read you denied stay cached (so a Deny isn't asked again a moment later) until you set the key again;
/// a locked Keychain that couldn't ask, or any other error, is read again next time.
@discardableResult
func keychain(_ account: String, set value: String? = nil) -> String? {
    keyLock.lock(); defer { keyLock.unlock() }
    let sec = keychainOps
    let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "greenlight", kSecAttrAccount as String: account]
    if let value {
        if value.isEmpty {
            let st = sec.delete(q as CFDictionary)
            guard st == errSecSuccess || st == errSecItemNotFound else {
                NSLog("keychain delete %@: %d", account, st)
                return nil
            }
            keys[account] = .some(nil)
            return value
        }
        let data = Data(value.utf8)
        var st = sec.update(q as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if st == errSecItemNotFound {
            st = sec.add(q.merging([kSecValueData as String: data]) { $1 } as CFDictionary, nil)
            if st == errSecDuplicateItem { st = sec.update(q as CFDictionary, [kSecValueData as String: data] as CFDictionary) }
        }
        guard st == errSecSuccess else {
            NSLog("keychain save %@: %d", account, st)
            return nil
        }
        keys[account] = .some(value)
        return value
    }
    if let k = keys[account] { return k }
    var out: CFTypeRef?
    let st = sec.copy(q.merging([kSecReturnData as String: true]) { $1 } as CFDictionary, &out)
    if st == errSecSuccess, let data = out as? Data {
        let value = String(decoding: data, as: UTF8.self)
        keys[account] = .some(value)
        return value
    }
    if st != errSecItemNotFound { NSLog("keychain read %@: %d", account, st) }
    if st == errSecItemNotFound || st == errSecUserCanceled || st == errSecAuthFailed { keys[account] = .some(nil) }
    return nil
}
/// The OpenAI key Greenlight uses: the one set in Settings (Keychain) first, else OPENAI_API_KEY from the environment.
/// It may wait on the login shell (see environmentOpenAIKey); Settings' text doesn't (openAIKeySource).
func openAIKey() -> String? { settingsOpenAIKey() ?? environmentOpenAIKey() }
/// The key set in Settings, trimmed; nil if there's none (or only whitespace).
func settingsOpenAIKey() -> String? { keychain("openai")?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty }
/// Where openAIKey() finds its key, for Settings. Never waits: before the shell has answered, it says it's looking.
func openAIKeySource() -> String {
    if settingsOpenAIKey() != nil { return "Stored in your Keychain." }
    guard let env = peekEnvironmentOpenAIKey() else { return "Looking for OPENAI_API_KEY in your shell…" }
    return env == nil ? "No key: set one here, or export OPENAI_API_KEY in your shell and relaunch Greenlight."
        : "Using OPENAI_API_KEY; a key set here comes first."
}
/// OPENAI_API_KEY from this app's environment, else your login shell's: an app opened from the Finder or the Dock gets launchd's
/// environment, without your shell's exports. Read once a launch on envKeyQueue, so a caller that needs the key waits for the one
/// read and Settings (peek) never does. The selftest sets envKeyRead.
var envKeyRead: (done: Bool, key: String?) = (false, nil)
private let envKeyLock = NSLock(), envKeyQueue = DispatchQueue(label: "env-key")
private var envKeyStarted = false  // peek started the read: Settings redraws ask once, not on every render
func environmentOpenAIKey(_ env: [String: String] = ProcessInfo.processInfo.environment) -> String? { envKeyQueue.sync { readEnvironmentOpenAIKey(env) } }
/// On envKeyQueue: the stored answer, else the read.
private func readEnvironmentOpenAIKey(_ env: [String: String]) -> String? {
    envKeyLock.lock(); let r = envKeyRead; envKeyLock.unlock()
    if r.done { return r.key }
    var k = env["OPENAI_API_KEY"]?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty
    if k == nil { k = shellVariable("OPENAI_API_KEY", shell: env["SHELL"] ?? loginShell())?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty }
    envKeyLock.lock(); envKeyRead = (true, k); envKeyLock.unlock()
    return k
}
/// The read's result without waiting: .some(key or nil) once read; before that nil, and the read starts (Settings redraws when it's in).
func peekEnvironmentOpenAIKey() -> String?? {
    envKeyLock.lock(); let r = envKeyRead, start = !r.done && !envKeyStarted; envKeyStarted = true; envKeyLock.unlock()
    if r.done { return .some(r.key) }
    guard start else { return nil }
    envKeyQueue.async { _ = readEnvironmentOpenAIKey(ProcessInfo.processInfo.environment); DispatchQueue.main.async { (NSApp.delegate as? App)?.greenlight.objectWillChange.send() } }
    return nil
}
/// Your account's login shell (an app launched by launchd may have no SHELL).
func loginShell() -> String { getpwuid(getuid())?.pointee.pw_shell.map { String(cString: $0) }.flatMap { $0.isEmpty ? nil : $0 } ?? "/bin/zsh" }
extension String { var nilIfEmpty: String? { isEmpty ? nil : self } }
/// A variable as your interactive login shell sets it (~/.zprofile and ~/.zshrc), or nil. Whatever the rc files print is skipped:
/// the value sits between markers made for this read, and reading stops at the end one (a prompt's background helper may hold the
/// pipe open for good). A shell that hasn't answered in `timeout` s is killed (an interactive shell ignores SIGTERM); helpers it
/// started are left, as a closed Terminal tab leaves them (Process can't give the shell a group of its own to kill).
func shellVariable(_ name: String, shell: String, timeout: Double = 5) -> String? {
    let p = Process(), out = Pipe(), lock = NSLock(), got = DispatchSemaphore(value: 0), tag = UUID().uuidString
    let open = "<<\(tag)>>", close = "<</\(tag)>>"
    p.executableURL = URL(fileURLWithPath: shell)
    p.arguments = ["-ilc", "printf '\\n\(open)%s\(close)' \"$\(name)\""]
    p.standardInput = FileHandle.nullDevice; p.standardOutput = out; p.standardError = FileHandle.nullDevice
    var data = Data(), over = false
    out.fileHandleForReading.readabilityHandler = { h in
        let d = h.availableData
        lock.lock(); defer { lock.unlock() }
        guard !over else { return }
        data += d
        let done = d.isEmpty || data.range(of: Data(close.utf8)) != nil  // EOF, or the value is in
        if !done && data.count > 1 << 20 { data = data.suffix(64 << 10) }  // a noisy rc: keep the tail (the open marker is in it, or still to come)
        if done { over = true; h.readabilityHandler = nil; got.signal() }
    }
    defer { out.fileHandleForReading.readabilityHandler = nil }  // the handle closes once released: never under a running handler
    do { try p.run() } catch { return nil }
    let answered = got.wait(timeout: .now() + timeout) == .success
    if p.isRunning { kill(p.processIdentifier, SIGKILL) }  // done with it either way: the value is read, or it never came
    lock.lock(); over = true; let t = String(decoding: data, as: UTF8.self); lock.unlock()
    guard answered, let a = t.range(of: open, options: .backwards), let b = t.range(of: close, range: a.upperBound..<t.endIndex) else { return nil }
    return String(t[a.upperBound..<b.lowerBound]).nilIfEmpty
}
/// Read OPENAI_API_KEY again on next use: you may have exported one since launch (Set key… does this). Settings redraws to start it.
/// When `done` is provided, re-reads immediately on `envKeyQueue` and calls `done(hasKey)` on the main queue.
func forgetEnvironmentOpenAIKey(_ done: (@Sendable (Bool) -> Void)? = nil) {
    envKeyQueue.async {
        envKeyLock.lock(); envKeyRead = (false, nil); envKeyStarted = done != nil; envKeyLock.unlock()
        let found = done != nil ? readEnvironmentOpenAIKey(ProcessInfo.processInfo.environment) != nil : false
        DispatchQueue.main.async {
            (NSApp.delegate as? App)?.greenlight.objectWillChange.send()
            done?(found)
        }
    }
}
/// The Keychain calls `keychain` makes; the selftest swaps in fakes.
struct KeychainOps {
    var copy: (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus = SecItemCopyMatching
    var update: (CFDictionary, CFDictionary) -> OSStatus = SecItemUpdate
    var add: (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus = SecItemAdd
    var delete: (CFDictionary) -> OSStatus = SecItemDelete
}
var keychainOps = KeychainOps()
var keys: [String: String?] = [:]
let keyLock = NSLock()

/// Transcribes a recorded file; done(text, error) runs on any thread, exactly once, and removes the recording file afterwards.
func transcribe(_ url: URL, engine: String, done: @escaping (String?, String?) -> Void) {
    let trim = { (s: String) in s.trimmingCharacters(in: .whitespacesAndNewlines) }
    let finish: (String?, String?) -> Void = { text, err in
        try? FileManager.default.removeItem(at: url)
        done(text, err)
    }
    switch engine {
    case "whisper":
        guard FileManager.default.isExecutableFile(atPath: whisperBin()) else {
            return finish(nil, "whisper-cli not found at \(whisperBin()): `brew install whisper-cpp`, or pick another engine")
        }
        DispatchQueue.global().async {
            let p = Process(), out = Pipe()
            p.executableURL = URL(fileURLWithPath: whisperBin())
            p.arguments = ["-m", whisperModel(), "-f", url.path, "-nt", "-np"]
            p.standardOutput = out
            p.standardError = FileHandle.nullDevice
            do { try p.run() } catch { return finish(nil, error.localizedDescription) }
            let text = trim(String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self))
            p.waitUntilExit()
            finish(text, p.terminationStatus == 0 ? nil : "whisper-cli exited \(p.terminationStatus)")
        }
    case "openai":
        DispatchQueue.global(qos: .userInitiated).async {
            guard let key = openAIKey() else { return finish(nil, "no OpenAI key: right-click the star → Voice → Set OpenAI key…, or export OPENAI_API_KEY and relaunch") }
            let b = UUID().uuidString
            var req = URLRequest(url: URL(string: "https://api.openai.com/v1/audio/transcriptions")!)
            req.httpMethod = "POST"
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
            req.setValue("multipart/form-data; boundary=\(b)", forHTTPHeaderField: "Content-Type")
            let model = ud.string(forKey: "openaiModel") ?? "whisper-1"
            var body = Data("--\(b)\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n\(model)\r\n".utf8)
            body += Data("--\(b)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n".utf8)
            body += (try? Data(contentsOf: url)) ?? Data()
            body += Data("\r\n--\(b)--\r\n".utf8)
            URLSession.shared.uploadTask(with: req, from: body) { data, _, err in
                let j = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                if let t = j?["text"] as? String { return finish(trim(t), nil) }
                finish(nil, err?.localizedDescription ?? (j?["error"] as? [String: Any])?["message"] as? String ?? "unexpected reply from OpenAI")
            }.resume()
        }
    default:
        SFSpeechRecognizer.requestAuthorization { status in
            DispatchQueue.main.async {
                guard status == .authorized, let r = SFSpeechRecognizer(), r.isAvailable else {
                    return finish(nil, "speech recognition is off: System Settings → Privacy & Security → Speech Recognition")
                }
                recognizer = r
                let req = SFSpeechURLRecognitionRequest(url: url)
                req.requiresOnDeviceRecognition = r.supportsOnDeviceRecognition
                req.addsPunctuation = true
                var fired = false
                r.recognitionTask(with: req) { res, err in
                    DispatchQueue.main.async {
                        guard !fired else { return }
                        if let res, res.isFinal { fired = true; _ = r; finish(trim(res.bestTranscription.formattedString), nil) }
                        else if let err { fired = true; _ = r; finish(nil, err.localizedDescription) }
                    }
                }
            }
        }
    }
}

/// "#RRGGBB" from the dashboard's theme; gray if it isn't one.
func hexColor(_ s: String) -> Color {
    guard s.hasPrefix("#"), s.count == 7, Int(s.dropFirst(), radix: 16) != nil else { return StatusInk.queued }
    return Color(nsColor: hex(String(s.dropFirst())))
}

/// "⌃⌥V" for the saved voice shortcut.
func shortcutName(_ mods: Int, _ key: String) -> String {
    (mods & controlKey != 0 ? "⌃" : "") + (mods & optionKey != 0 ? "⌥" : "") + (mods & shiftKey != 0 ? "⇧" : "") + (mods & cmdKey != 0 ? "⌘" : "") + key
}

/// A hex or stack dragged from the dashboard onto the chat's edge: its MRs go to greenlight with the next message.
struct Attached: Identifiable {
    let id: String, label: String, color: Color, mrs: Int, text: String
    var sent = false
}

/// A file attached in the chat's composer (its +, a drop, ⌘V), checked as chat.py's attach.py checks it again where greenlight runs.
/// Only its own name and its bytes go there: the folder it was in on this Mac is never sent, shown or logged.
struct ChatFile: Identifiable {
    let id = UUID()
    let name: String, kind: String, data: Data, thumb: NSImage?
    static let limits = ["image": 5 << 20, "pdf": 10 << 20, "text": 1 << 20], maxFiles = 5, maxTotal = 20 << 20
    static let kinds = ["image": "image", "pdf": "PDF", "text": "text"]
    static let supported = "images (PNG, JPEG, GIF, WebP), PDFs and text files"
    var json: [String: String] { ["name": name, "data": data.base64EncodedString()] }
    var kindName: String { let k = ChatFile.kinds[kind] ?? kind; return k.prefix(1).uppercased() + k.dropFirst() }  // "Image", "PDF", "Text"
}
func byteSize(_ n: Int) -> String { n < 1024 ? "\(n) B" : n < 1 << 20 ? "\(Int((Double(n) / 1024).rounded())) KB" : String(format: "%.1f MB", Double(n) / Double(1 << 20)) }
/// image | pdf | text, from the bytes (never the name), as attach.kind.
func fileKind(_ d: Data) -> String? {
    let b = [UInt8](d.prefix(12)), has = { (p: [UInt8]) in b.starts(with: p) }
    if has([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) || has([0xFF, 0xD8, 0xFF]) || has(Array("GIF87a".utf8)) || has(Array("GIF89a".utf8))
        || (has(Array("RIFF".utf8)) && b.count == 12 && Array(b[8..<12]) == Array("WEBP".utf8)) { return "image" }
    if has(Array("%PDF-".utf8)) { return "pdf" }
    return !d.contains(0) && String(data: d, encoding: .utf8) != nil ? "text" : nil
}
/// A file to attach, or why it can't be. Another image format (HEIC, TIFF), or an image over 5 MB (a phone photo, a big screenshot),
/// goes as a PNG when that fits, else a JPEG at most 2576 px on its long side. Slow for big images: call it off the main thread.
func chatFile(_ data: Data, name raw: String) -> (file: ChatFile?, error: String?) {
    let last = raw.split(separator: "/").last.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } ?? "", name = last.isEmpty ? "file" : last
    if data.isEmpty { return (nil, "\(name) is empty.") }
    var d = data, n = name, kind = fileKind(data)
    let limit = ChatFile.limits["image"]!
    if kind == nil || (kind == "image" && d.count > limit), let img = NSImage(data: data), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let stem = (name as NSString).deletingPathExtension, full = NSBitmapImageRep(cgImage: cg)
        if kind == nil, let png = full.representation(using: .png, properties: [:]), png.count <= limit { (d, n, kind) = (png, stem + ".png", "image") }
        else {
            let k = min(1, 2576 / CGFloat(max(cg.width, cg.height))), w = max(1, Int(CGFloat(cg.width) * k)), h = max(1, Int(CGFloat(cg.height) * k))
            if let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                          colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0), let ctx = NSGraphicsContext(bitmapImageRep: rep) {
                NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = ctx
                ctx.cgContext.interpolationQuality = .high
                ctx.cgContext.setFillColor(CGColor(gray: 1, alpha: 1)); ctx.cgContext.fill(CGRect(x: 0, y: 0, width: w, height: h))  // JPEG has no alpha: white, not black
                ctx.cgContext.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
                NSGraphicsContext.restoreGraphicsState()
                for q in [0.85, 0.7, 0.5] as [CGFloat] {
                    if let jpg = rep.representation(using: .jpeg, properties: [.compressionFactor: q]), jpg.count <= limit { (d, n, kind) = (jpg, stem + ".jpg", "image"); break }
                }
            }
        }
    }
    guard let k = kind else { return (nil, "\(name) isn't a file the chat can use: attach \(ChatFile.supported).") }
    if d.count > ChatFile.limits[k]! { return (nil, "\(name) is \(byteSize(d.count)): \(ChatFile.kinds[k]!)s can be up to \(byteSize(ChatFile.limits[k]!)).") }
    let thumb = k == "image" ? NSImage(data: d).map { t -> NSImage in t.size = NSSize(width: 36, height: 36 * t.size.height / max(t.size.width, 1)); return t } : nil
    return (ChatFile(name: n, kind: k, data: d, thumb: thumb), nil)
}
/// A file on disk (a symlink's target), read only up to the biggest file the chat takes: a 3 GB video is refused unread.
func chatFile(at url: URL) -> (file: ChatFile?, error: String?) {
    let name = url.lastPathComponent, real = url.resolvingSymlinksInPath(), v = try? real.resourceValues(forKeys: [.isDirectoryKey, .isRegularFileKey])
    if v?.isDirectory == true { return (nil, "\(name) is a folder: attach the files in it instead.") }
    guard let v else { return (nil, "\(name) couldn't be read.") }
    if v.isRegularFile != true { return (nil, "\(name) isn't a file the chat can use: attach \(ChatFile.supported).") }  // a pipe or a device would never finish reading
    let cap = ChatFile.limits["image"]! * 8  // a photo that big still shrinks to fit; a bigger file won't
    guard let fh = try? FileHandle(forReadingFrom: real) else { return (nil, "\(name) couldn't be read.") }
    defer { try? fh.close() }
    guard let d = try? fh.read(upToCount: cap + 1) ?? Data() else { return (nil, "\(name) couldn't be read.") }
    if d.count > cap { return (nil, "\(name) is over \(byteSize(cap)): too big to attach.") }
    return chatFile(d, name: name)
}
/// The chat's drop target, over the whole pane: files and images dragged anywhere on it, the text field too, attach. It takes
/// only those types, so dragged text still lands in the field, and clicks pass through it. (A SwiftUI onDrop can't: AppKit gives a
/// file drag over the field to the field's editor first, which would type the file's path.)
final class DropCatcher: NSView {
    weak var c: Core?
    static let types: [NSPasteboard.PasteboardType] = [.fileURL, .tiff, .png] + NSFilePromiseReceiver.readableDraggedTypes.map { .init($0) }
    override init(frame: NSRect) { super.init(frame: frame); registerForDraggedTypes(Self.types) }
    required init?(coder: NSCoder) { nil }
    override func hitTest(_ p: NSPoint) -> NSView? { nil }
    override func draggingEntered(_ s: NSDraggingInfo) -> NSDragOperation {
        guard Core.takes(s.draggingPasteboard) else { return [] }  // rich text's picture: not ours to take
        c?.dropping = true; return .copy
    }
    override func draggingExited(_ s: NSDraggingInfo?) { c?.dropping = false }
    override func draggingEnded(_ s: NSDraggingInfo) { c?.dropping = false }
    override func performDragOperation(_ s: NSDraggingInfo) -> Bool { c?.dropping = false; return c?.dropped(s.draggingPasteboard) ?? false }
}
struct DropArea: NSViewRepresentable {
    let c: Core
    func makeNSView(context: Context) -> DropCatcher { let v = DropCatcher(); v.c = c; return v }
    func updateNSView(_ v: DropCatcher, context: Context) { v.c = c }
}

// MARK: voice mode: a hands-free conversation with the chat, and the stage that draws what it says

/// Chief's reply, split for voice mode: its own ```stage block (HTML, or notes for Haiku to draw), and the rest. An unclosed fence (still streaming) hides everything from it on.
/// The rest splits again at a `[screen]` line: `text` before it is what's spoken (the gist), `screen` after it is the detail that is
/// only shown (on the stage, and in the transcript under the gist), never read out.
struct Staged: Equatable {
    var text: String; var stage: String?; var end = false; var hexes: [String]? = nil; var reset = false; var console: String? = nil; var open: URL? = nil; var screen: String? = nil
    /// What the transcript shows: the gist, then the detail.
    var shown: String { [text, screen ?? ""].filter { !$0.isEmpty }.joined(separator: "\n\n") }
    /// The finished reply's parts with the Options lines off, whichever part Chief ended with one: what's shown, the gist,
    /// the detail alone, and the options. A gist that asks the options' question goes last in what's shown, over its buttons.
    var parts: (shown: String, gist: String, detail: String?, options: [String]) {
        let (gist, o1) = splitOptions(text), (more, o2) = splitOptions(screen ?? ""), options = o1 + o2
        let order = !options.isEmpty && gist.contains("?") ? [more, gist] : [gist, more]
        return (order.filter { !$0.isEmpty }.joined(separator: "\n\n"), gist, more.isEmpty ? nil : more, options)
    }
}
/// Every reply splits, voice mode or not: a session that ends voice mode may keep writing the marker, and only the line goes.
func staged(_ s: String) -> Staged {
    var text = s, stage: String?, end = false, hexes: [String]?
    if let r = text.range(of: #"\[hexe?s?:([^\]\n]*)(\]|$)"#, options: [.regularExpression, .caseInsensitive]) {  // Chief's own pick; unclosed while it streams
        let inner = text[r].dropFirst().drop { $0 != ":" }.dropFirst().replacingOccurrences(of: "]", with: "")
        hexes = inner.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty && $0.lowercased() != "none" }
        text.removeSubrange(r)
    }
    if let r = text.range(of: #"\[voice: ?off\]"#, options: [.regularExpression, .caseInsensitive]) { end = true; text.removeSubrange(r) }  // Chief ends voice mode
    var console: String?
    if let r = text.range(of: #"\[console:([^\]\n]*)(\]|$)"#, options: [.regularExpression, .caseInsensitive]) {  // Chief brings up a terminal tab
        console = text[r].dropFirst(9).replacingOccurrences(of: "]", with: "").trimmingCharacters(in: .whitespaces)
        text.removeSubrange(r)
    }
    var link: URL?
    while let r = text.range(of: #"\[open:(?:[^\[\]\n]|\[[^\]\n]*+(?:\]|$))*+(?:\]|$)"#, options: [.regularExpression, .caseInsensitive]) {  // Chief opens a PR/MR/issue in the browser: the first allowed one; every one is hidden. One [!3] level inside; possessive, so a stray one can't backtrack
        let closed = text[r].range(of: #"^\[open:(?:[^\[\]\n]|\[[^\]\n]*+\])*+\]$"#, options: [.regularExpression, .caseInsensitive]) != nil  // its own ], not a nested one's
        if link == nil, closed { link = forgeURL(String(text[r].dropFirst(6).dropLast())) }  // closed only: a cut-off one never opens
        text.removeSubrange(r)
    }
    let reset = text.range(of: #"\[chat: ?reset\]"#, options: [.regularExpression, .caseInsensitive]).map { text.removeSubrange($0); return true } ?? false  // asked to start over
    if let r = text.range(of: #"\[stage: ?(full|normal)\]"#, options: [.regularExpression, .caseInsensitive]) { text.removeSubrange(r) }  // the old size line: no full size any more
    while let open = text.range(of: #"```[ \t]*stage(?![A-Za-z0-9])"#, options: [.regularExpression, .caseInsensitive]) {  // every one, ```Stage too: never for the transcript
        guard let close = text[open.upperBound...].range(of: "```") else { text = String(text[..<open.lowerBound]); break }
        if stage == nil { stage = String(text[open.upperBound..<close.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines) }  // the first is drawn
        text.removeSubrange(open.lowerBound..<close.upperBound)
    }
    var screen: String?
    if let r = screenMark(text) {  // the gist is spoken; what follows is only shown
        screen = String(text[r.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
        text = String(text[..<r.lowerBound])
    }
    return Staged(text: text.trimmingCharacters(in: .whitespacesAndNewlines), stage: stage, end: end, hexes: hexes, reset: reset,
                  console: console?.isEmpty == false ? console : nil, open: link, screen: screen?.isEmpty == false ? screen : nil)
}

/// The forges Greenlight tracks (greenlight.py's forge_project): the only hosts Chief's [open: …] may send the browser to.
let forgeHosts: Set<String> = ["github.com", "gitlab.com"]
/// Chief's [open: URL] (or [open: [!3](URL)]) as a URL the browser may open: http(s) on a forge host (or its www.), no user:password@, no odd port; else nil.
func forgeURL(_ s: String) -> URL? {
    var t = s.trimmingCharacters(in: .whitespaces)
    t = t.replacingOccurrences(of: #"^\[[^\]]*\]\(([^)]*)\)$"#, with: "$1", options: .regularExpression)  // [!3](url): Chief's Markdown link
    t = t.trimmingCharacters(in: CharacterSet(charactersIn: "<>`\"' "))
    guard let c = URLComponents(string: t), let scheme = c.scheme?.lowercased(), ["http", "https"].contains(scheme),
          var host = c.host?.lowercased(), c.user == nil, c.password == nil, c.port == nil else { return nil }
    if host.hasPrefix("www.") { host.removeFirst(4) }
    return forgeHosts.contains(host) ? c.url : nil
}

/// The first `[screen]` marker: at the start of a line (the detail may follow on it) or at the end of one (the gist before it),
/// bold or a colon around it allowed (backticks only when alone on its line), and outside a code fence, so prose quoting it isn't split.
func screenMark(_ s: String) -> Range<String.Index>? {
    var from = s.startIndex
    let mark = #"[*_]*\[screen\][*_]*:?[*_]*"#
    while let r = s.range(of: #"(?m)^[ \t]*"# + mark + #"[ \t]*|[ \t]+"# + mark + #"[ \t]*$|^[ \t]*`\[screen\]:?`:?[ \t]*$"#, options: [.regularExpression, .caseInsensitive], range: from..<s.endIndex) {
        if s[..<r.lowerBound].components(separatedBy: "```").count % 2 == 1 { return r }  // an even number of fences before it: outside
        from = r.upperBound
    }
    return nil
}

/// A spoken sentence made easy to listen to: a commit hash isn't read out character by character (the screen keeps it).
/// A hash is a whole word of 7 to 40 lowercase hex with a digit and a letter, not a number like 1e10000. In brackets it goes;
/// after "commit" the word stays; anywhere else it's said as "a commit", so the sentence still reads.
func forEar(_ s: String) -> String {
    let hash = #"(?<![\w#.\-/])(?!\d+e\d+(?![\w\-]))(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w\-/]|\.\w|\.\.)"#
    return s.replacingOccurrences(of: #"[ \t]*\([ \t]*"# + hash + #"[ \t]*\)"#, with: "", options: .regularExpression)
        .replacingOccurrences(of: #"\b((?i:commits?))[ \t]+"# + hash, with: "$1", options: .regularExpression)
        .replacingOccurrences(of: hash + #"[ \t]+(?=(?i:commits?)\b)"#, with: "", options: .regularExpression)
        .replacingOccurrences(of: hash, with: "a commit", options: .regularExpression)
        .trimmingCharacters(in: .whitespaces)
}

/// The gist as it streams, without an Options line (its buttons, never said). Removing it whole keeps `said` valid: the line
/// is always removed, from its first characters on.
func spokenSoFar(_ gist: String) -> String {
    plain(gist).replacingOccurrences(of: #"(?im)^[ \t]*options:.*$"#, with: "", options: .regularExpression)
}

/// Markdown off, for speaking: code blocks and bare URLs out, links to their text, emphasis/heading/quote/bullet marks gone.
/// The reply without a leading emoji-only line: Chief used to open with a three-emoji mood line, and older transcripts
/// (and a resumed session imitating them) still carry one. A reply that just starts with an emoji keeps it.
func unmood(_ s: String) -> String {
    let (first, rest) = s.firstIndex(of: "\n").map { (s[..<$0], String(s[s.index(after: $0)...])) } ?? (s[...], "")
    let m = first.trimmingCharacters(in: .whitespaces)
    let emoji = !m.isEmpty && m.allSatisfy { ch in ch.isWhitespace || (ch.unicodeScalars.first.map { $0.properties.isEmojiPresentation || ($0.properties.isEmoji && ch.unicodeScalars.count > 1) } ?? false) }
    return emoji ? rest.drop { $0.isNewline }.description : s
}

func plain(_ s: String) -> String {
    var t = s.replacingOccurrences(of: "```[\\s\\S]*?```", with: "", options: .regularExpression)
    if let r = t.range(of: "```") { t = String(t[..<r.lowerBound]) }
    for (p, w) in [("\\[([^\\]]*)\\]\\([^)]*\\)", "$1"), ("`([^`]*)`", "$1"), ("https?://\\S+", ""), ("(?m)^[ \\t]*(#+|>|[-*+]|\\d+\\.)[ \\t]+", ""), ("\\*+|~~", "")] {
        t = t.replacingOccurrences(of: p, with: w, options: .regularExpression)
    }
    return t.filter { ch in  // emoji aren't spoken (Apple's voice reads their names); digits, # and * are "emoji" too, so keep those
        guard let u = ch.unicodeScalars.first, u.properties.isEmoji else { return true }
        return !(u.properties.isEmojiPresentation || ch.unicodeScalars.count > 1)
    }
}

/// The complete sentences in s past `from` (a character offset): ended by . ! ? and a space, or by a line break.
func sentences(_ s: String, from: Int) -> (said: [String], to: Int) {
    let cs = Array(s)
    var out = [String](), start = min(max(from, 0), cs.count), i = start
    while i < cs.count {
        if cs[i] == "\n" || (".!?".contains(cs[i]) && i + 1 < cs.count && cs[i + 1].isWhitespace) {
            let t = String(cs[start...i]).trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { out.append(t) }
            start = i + 1
        }
        i += 1
    }
    return (out, start)
}

/// What voice mode shows and says while Chief works, one per turn (it used to always be "One minute").
let waitWords = """
Pondering Mulling Musing Ruminating Cogitating Deliberating Contemplating Considering Reflecting Brooding Chewing Noodling
Puzzling Figuring Reckoning Calculating Computing Crunching Tallying Weighing Sifting Sorting Combing Rummaging Digging
Excavating Unearthing Spelunking Delving Probing Scouting Exploring Surveying Investigating Sleuthing Snooping Detecting Tracking
Tracing Hunting Foraging Fishing Angling Trawling Dredging Mining Prospecting Panning Scavenging Gathering Collecting Harvesting
Gleaning Brewing Steeping Simmering Stewing Marinating Percolating Fermenting Distilling Infusing Whisking Stirring Kneading
Baking Roasting Toasting Sautéing Braising Caramelizing Seasoning Garnishing Plating Cooking Concocting Mixing Blending Churning
Folding Proofing Tinkering Fiddling Twiddling Jiggling Wiggling Wrangling Juggling Shuffling Rearranging Untangling Unraveling
Unknotting Unpacking Unscrambling Decoding Deciphering Decrypting Translating Parsing Reading Skimming Scanning Studying Cramming
Researching Reviewing Assembling Building Constructing Crafting Forging Smithing Welding Soldering Wiring Rigging Engineering
Architecting Drafting Sketching Doodling Drawing Painting Sculpting Whittling Carving Chiseling Polishing Buffing Sanding Honing
Sharpening Tuning Calibrating Adjusting Tweaking Finessing Massaging Smoothing Ironing Tidying Arranging Organizing Filing
Indexing Cataloging Labeling Stacking Packing Loading Booting Spinning Revving Warming Charging Powering Humming Buzzing Whirring
Clicking Ticking Beeping Booping Bleeping Blipping Pinging Polling Querying Fetching Retrieving Summoning Conjuring Divining
Scrying Channeling Manifesting Enchanting Spellcasting Alchemizing Transmuting Hatching Incubating Germinating Sprouting
Blossoming Cultivating Gardening Pruning Weeding Watering Composting Nesting Burrowing Tunneling Navigating Charting Mapping
Plotting Scheming Strategizing Planning Orchestrating Conducting Composing Harmonizing Improvising Riffing Jamming Drumming
Strumming Whistling Daydreaming Wondering Imagining Envisioning Visualizing Dreaming Hypothesizing Theorizing Philosophizing
Speculating Extrapolating Synthesizing Analyzing Diagnosing Triangulating Deducing Inferring Concluding Resolving Untwisting
Clarifying Condensing Summarizing Paraphrasing Editing Proofreading Revising Rewriting Wordsmithing Scribbling Jotting Noting
Annotating Highlighting Cross-referencing Double-checking Fact-checking Verifying Validating Testing Debugging Refactoring
Optimizing Compiling Linting Diffing Merging Rebasing Committing Shipping Deploying Herding Shepherding Corralling Lassoing
Wrestling Taming Coaxing Nudging Cajoling Persuading Negotiating Haggling Bartering Bargaining Galloping Trotting Moseying
Sauntering Meandering Wandering Pacing Tiptoeing Scurrying Scampering Hustling Bustling Zooming Whooshing Zipping Accelerating
Gearing Caffeinating Stretching Limbering Meditating Breathing Centering Focusing Concentrating Zeroing Homing Locking Aligning
Syncing Buffering Rendering Pixelating Vectorizing Quantizing Tokenizing Embedding Inferencing Reasoning Thinking Processing
Working Grinding Plugging Chugging Trundling Puttering Pottering Dawdling Lollygagging Dillydallying Shilly-shallying
Flibbertigibbeting Bamboozling Befuddling Discombobulating Recombobulating Kerfuffling Hullabalooing Gallivanting Frolicking
Cavorting Moonwalking Breakdancing Boogieing Shimmying Twirling Pirouetting Somersaulting Cartwheeling Snorkeling Scuba-diving
Sailing Rowing Paddling Surfing Skating Gliding Soaring Hovering Orbiting Launching Rocketing Teleporting Time-traveling Warping
Beaming Abacusing Slide-ruling Hmm-ing Umm-ing Head-scratching Chin-stroking Pipe-puffing Monocle-polishing Tea-sipping
Coffee-brewing Lightbulbing Eureka-ing Aha-ing Puzzle-solving Rubik's-cubing Sudoku-ing Crosswording Chess-playing Checkmating
Domino-toppling Jigsawing
""".split(whereSeparator: \.isWhitespace).map { String($0) + "…" }

/// What voice mode does itself instead of sending to Chief: only when that's all you said (give or take "ok", "thanks", "chief").
enum VoiceCmd: Equatable { case bye, stop, cancel, captions(Bool), reset, undo }
func voiceCommand(_ heard: String) -> VoiceCmd? {
    let t = heard.lowercased().replacingOccurrences(of: "’", with: "'").replacingOccurrences(of: "[^a-z0-9' ]", with: "", options: .regularExpression)
        .replacingOccurrences(of: #"\b(ok|okay|alright|all right|well|so|cool|great|thanks|thank you|chief|greenlight|for now|then|now)\b"#, with: " ", options: .regularExpression)
        .split(separator: " ").joined(separator: " ")
    switch t {
    case "that's all", "thats all", "that is all", "that's it", "thats it", "i'm done", "im done", "we're done", "were done", "stop listening",
         "goodbye", "good bye", "bye", "bye bye", "see you", "see ya", "see you later", "later", "talk to you later", "talk later", "catch you later",
         "good night", "goodnight", "end voice mode", "exit voice mode", "turn off voice mode", "voice off": return .bye
    case "stop", "stop that": return .stop
    case "never mind", "nevermind", "cancel": return .cancel
    case "reset", "reset context", "reset the context", "reset the chat", "clear", "clear context", "clear the context", "clear the chat",
         "start over", "start fresh", "fresh start", "new chat", "new conversation", "forget everything", "wipe it": return .reset
    case "undo", "undo that", "undo it", "take that back", "put it back", "put them back": return .undo
    case "captions on", "turn on captions", "show captions": return .captions(true)
    case "captions off", "turn off captions", "hide captions": return .captions(false)
    default: return nil
    }
}
func voiceStopsPending(_ cmd: VoiceCmd, chiefRunning: Bool, pending: Bool) -> Bool {
    pending && (cmd == .cancel || !chiefRunning)
}

/// While Chief speaks: you cutting in, not its own voice leaking back? 3+ of your latest words, mostly not ones it just said.
func isBargeIn(_ heard: String, _ spoken: String) -> Bool {
    let w = { (s: String) in s.lowercased().split { !$0.isLetter && $0 != "'" }.map(String.init) }
    let h = w(heard).suffix(6), said = Set(w(spoken))
    return h.count >= 3 && Double(h.filter { !said.contains($0) }.count) / Double(h.count) > 0.5
}

/// DWIM (scripts/dwim.py): a board command said, typed in the chat or typed in ⌘J's palette ("park the two auth ones", "hold #42",
/// "kick off CI fixes on the red ones") is done in well under a second, without a Chief turn. This is only the cheap gate on the
/// words: a verb dwim.py knows. dwim.py decides the rest (the route, the workspaces, the guards) and anything it won't do goes to Chief.
let dwimVerbs = try! NSRegularExpression(pattern: #"\b(?:un-?park|park|shelve|snooze|un-?shelve|unsnooze|un-?hold|hold|freeze|do ?n[o'’]?t merge|do not merge|"#
    + #"release (?:the )?hold|lift (?:the )?hold|remove (?:the )?hold|clear (?:the )?hold|ok to merge|rebase|put (?:\w+ ){0,4}aside|bring (?:\w+ ){0,4}back|"#
    + #"fix(?:es|ing)? (?:the |its |their )?(?:ci|build|builds|pipelines?|tests?|red|(?:merge )?conflicts?)|fix (?:for )?ci|ci fix(?:es)?|get (?:\w+ ){0,3}green|put .{1,80} on ice|"#
    + #"(?:address|answer|fix|resolve|handle)(?:es)? (?:the |its |their )?(?:review |open )?(?:threads|comments|feedback|review))\b"#, options: [.caseInsensitive])
func dwimCandidate(_ t: String) -> Bool {
    let s = t.trimmingCharacters(in: .whitespacesAndNewlines)
    return !s.isEmpty && s.count <= 300 && !s.contains("\n") && dwimVerbs.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
}
/// The snapshot facts dwim.py reads, trimmed: the stage card and the router want names, goals, MRs, moves and lifecycle, not pages.
func dwimPucks(_ pucks: [[String: Any]]) -> [[String: Any]] {
    pucks.map { p in
        var o = p.filter { ["name", "path", "project", "repo", "goal", "goal_live", "state", "step", "steps", "step_index", "owner", "hidden", "action", "agent", "mr", "life", "moves", "branch"].contains($0.key) }
        if let ag = p["agent"] as? [String: Any] { o["agent"] = ["state": ag["state"] ?? ""] }
        return o
    }
}
func dwimScopedPucks(_ pucks: [[String: Any]], _ scope: String?) -> [[String: Any]] {
    guard let scope else { return pucks }
    guard let o = (try? JSONSerialization.jsonObject(with: Data(scope.utf8))) as? [String: Any] else { return [] }
    let paths = o["paths"] as? [String] ?? [], repos = o["repos"] as? [String] ?? []
    return pucks.filter { p in
        (p["path"] as? String).map(paths.contains) == true ||
            (p["repo"] as? String).map { !$0.isEmpty && repos.contains($0) } == true
    }
}
/// The Undo toast: a direct board action just done (Undo puts the goals back), or an agent launch waiting out its grace (Undo cancels it).
struct DwimToast: Identifiable, Equatable {
    let id = UUID()
    var text: String
    var undo: String?  // dwim.py's undo id, once applied
    var pending = false  // a launch that hasn't started: Undo cancels it
    var applying = false  // goal edits on their way: Undo waits for their undo id
    var undoing = false, failed = false
    var runHost: String = host
    var undoable: Bool { pending || applying || undo != nil }
}
func keepingToast(_ toasts: [DwimToast], _ t: DwimToast) -> [DwimToast] {
    var out = toasts
    if let i = out.firstIndex(where: { $0.id == t.id }) { out[i] = t } else { out.append(t) }
    return out
}
func canBeginDwimUndo(_ t: DwimToast) -> Bool { !t.undoing }
struct DwimPlans {
    private var pending = [UUID: String]()
    mutating func begin(_ text: String) -> UUID { let id = UUID(); pending[id] = text; return id }
    mutating func finish(_ id: UUID) -> Bool { pending.removeValue(forKey: id) != nil }
    mutating func cancel() -> [String] { let texts = Array(pending.values); pending.removeAll(); return texts }
}
func dwimUndoReply(_ out: String, _ code: Int) -> [String: Any]? {
    guard code == 0, let r = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: Any] else { return nil }
    if r["error"] is String { return r }
    return r["ok"] as? Bool == true && r["undone"] is [String] && r["kept"] is [String] ? r : nil
}
func dwimApplyReply(_ out: String, failure: String, recovery: String?) -> [String: Any] {
    if let r = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: Any] { return r }
    return ["errors": [failure], "undo": recovery.map { $0 as Any } ?? NSNull()]
}
func dwimSkipped(_ plan: [String: Any]) -> String {
    let skipped = (plan["skipped"] as? [[String: String]] ?? []).compactMap { s -> String? in
        guard let name = s["ws"], let why = s["why"] else { return nil }
        return "\(name): \(why)"
    }
    return skipped.isEmpty ? "" : " Skipped " + skipped.joined(separator: "; ") + "."
}

/// A stage block's JSON, past the fence's own label (```stage json, ```Stage JSON, ```stage-card). nil when it isn't JSON:
/// notes that open with a link, timestamp, bullet or placeholder ("[!12](…) green", "[10:30] standup", "Plan {draft}") aren't.
func stageJSON(_ s: String) -> String? {
    let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
    let start = #"^(?:[._:-]?[A-Za-z][A-Za-z0-9_-]*)?\s*(?=\{\s*(?:\}|\z|["']|[A-Za-z_]\w*\s*:)|\[\s*(?:\z|\]\s*\z|["'\[{]|(?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|(?:true|false|null)\b)\s*(?:,|\]\s*\z|\z)))"#
    return t.range(of: start, options: .regularExpression).map { String(t[$0.upperBound...]) }
}

/// A stage block's HTML/SVG fragment, past an optional ```stage html/svg label. nil for plain notes ("Note:\n<foo>").
func stageHTML(_ s: String) -> String? {
    let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
    if t.hasPrefix("<") { return t }
    return t.range(of: #"^(?i)[_.:-]?(?:html|svg)\s+(?=<[A-Za-z!?])"#, options: .regularExpression).map { String(t[$0.upperBound...]) }
}

/// True when a parsed JSON stage object declares a chart (non-empty string, never null/false/empty).
func isStageChart(_ o: [String: Any]) -> Bool {
    (o["chart"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
}

/// A markdown link whose target is a URL or a path, for its text: [!12](https://…). Not fns[0](x).
let mdLink = #"\[([^\]]*)\]\((?:https?://|mailto:|\.{0,2}/|#|[\w-]+(?:\.[\w-]+)+/)[^)]*\)"#
/// Markdown a stage line drops: links to their text, code ticks, and emphasis only in real pairs (**kwargs, x ** 2 stay).
let mdMarks: [(String, String)] = [(mdLink, "$1"), ("`([^`\n]*)`", "$1"),
    (#"(?<![\w*])\*\*(?=\S)([^*\n]*?\S)\*\*(?![\w*])"#, "$1"), (#"(?<![\w~])~~(?=\S)([^~\n]*?\S)~~(?![\w~])"#, "$1")]
func unmark(_ s: String) -> String { mdMarks.reduce(s) { $0.replacingOccurrences(of: $1.0, with: $1.1, options: .regularExpression) } }

/// One line of a card as the stage shows it (text only): links to their text, code and emphasis marks off, one line;
/// true/false as yes/no; a list's values joined. `unnumber` drops a leading "1." or "2)" (the steps card counts for itself).
func cardText(_ v: Any?, unnumber: Bool = false) -> String {
    var t: String
    if let n = v as? NSNumber { t = CFGetTypeID(n) == CFBooleanGetTypeID() ? (n.boolValue ? "yes" : "no") : "\(n)" }
    else if let s = v as? String { t = s } else { t = v.map { jsonLines($0).map { cardText($0) }.filter { !$0.isEmpty }.joined(separator: ", ") } ?? "" }
    t = unmark(t)
    for (p, w) in [(#"\s+"#, " ")] + (unnumber ? [(#"^\s*\d+[.)]\s+"#, "")] : []) {
        t = t.replacingOccurrences(of: p, with: w, options: .regularExpression)
    }
    return t.trimmingCharacters(in: .whitespaces)
}

/// A card item from a line or an object: label, detail and tone by their usual names, any other values kept in the detail.
func cardItem(_ v: Any, unnumber: Bool) -> [String: String]? {
    guard let d = v as? [String: Any] else { let l = cardText(v, unnumber: unnumber); return l.isEmpty ? nil : ["label": l, "detail": "", "tone": ""] }
    let named = ["label", "title", "name", "mr", "ref", "id", "ws", "workspace", "path"].first { !cardText(d[$0]).isEmpty }  // else the first by key
    let said = ["detail", "value", "text"].first { !cardText(d[$0]).isEmpty }
    var rest = d.keys.filter { $0 != "tone" && $0 != named && $0 != said }.sorted().compactMap { k -> String? in
        let t = cardText(d[k])
        guard !t.isEmpty else { return nil }
        return d[k] is NSNumber ? "\(k): \(t)" : t
    }
    let strip = { (s: String) in unnumber ? s.replacingOccurrences(of: #"^\s*\d+[.)]\s+"#, with: "", options: .regularExpression).trimmingCharacters(in: .whitespaces) : s }
    var label = strip(cardText(named.flatMap { d[$0] }))
    if label.isEmpty, !rest.isEmpty { label = strip(rest.removeFirst()) }
    var detail = ([cardText(said.flatMap { d[$0] })] + rest).filter { !$0.isEmpty }.joined(separator: " · ")
    if label.isEmpty { (label, detail) = (strip(detail), "") }
    let tone = d["tone"] as? String ?? ""
    return label.isEmpty ? nil : ["label": label, "detail": detail, "tone": ["needs", "bad", "now", "ok"].contains(tone) ? tone : ""]
}

/// A native template kind (stage.html's TEMPLATES) that has the fields stage.html reads; else the block is read as a card.
func templateDraws(_ o: [String: Any]) -> Bool {
    let all = { (k: String, ok: (Any) -> Bool) in (o[k] as? [Any]).map { !$0.isEmpty && $0.allSatisfy(ok) } ?? false }
    let has = { (v: Any, k: String) in (v as? [String: Any]).map { !cardText($0[k]).isEmpty } ?? false }
    switch o["kind"] as? String {
    case "table": return all("rows") { ($0 as? [Any]).map { !$0.isEmpty } ?? false }
    case "timeline": return all("items") { has($0, "label") }
    case "lifecycle": return all("stages") { has($0, "label") }
    case "diff":
        let okF = all("files") { has($0, "path") }, okL = all("lines") { ($0 as? [String: Any])?["s"] is String }
        let emptyOrNil = { (v: Any?) in v == nil || v is NSNull || (v as? [Any])?.isEmpty == true }
        return (okF || okL) && (emptyOrNil(o["files"]) || okF) && (emptyOrNil(o["lines"]) || okL)
    default: return false
    }
}

/// A template's strings as the stage shows them: markdown marks off (unmark), all the way down, spacing kept. A diff's code
/// (its lines and file paths) stays exactly as written.
func cardClean(_ v: Any) -> Any {
    if let s = v as? String { return unmark(s) }
    if let a = v as? [Any] { return a.map(cardClean) }
    guard let d = v as? [String: Any] else { return v }
    return Dictionary(uniqueKeysWithValues: d.map { k, x in (k, d["kind"] as? String == "diff" && (k == "lines" || k == "files") ? x : cardClean(x)) })
}

/// Chief's ```stage JSON that isn't a chart: a card ({title, items, eyebrow, kind, stat}, items as lines or objects, under items,
/// steps, notes, list or lines, or just a list; plain values beside the title become lines too), drawn as stage.spec at once,
/// never handed to Haiku or its fallback as raw text. A native template with its facts goes through whole. nil when it isn't
/// one, or holds a shape a card can't show (a table without its kind, a list given as text): Haiku draws that.
func stageCard(_ s: String) -> [String: Any]? {
    guard let j = stageJSON(s), let any = try? JSONSerialization.jsonObject(with: Data(j.utf8)) else { return nil }
    let o = (any as? [String: Any]) ?? (any as? [Any]).map { ["items": $0] } ?? [:]
    guard !isStageChart(o) else { return nil }
    if templateDraws(o) { return cardClean(o) as? [String: Any] }
    let head = ["title", "heading", "kind", "eyebrow", "stat", "tone"]
    let listKey = ["items", "steps", "notes", "list", "lines"].first { o[$0] != nil && !(o[$0] is NSNull) }
    let noChart = { (k: String) in k == "chart" && ((o[k] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true
        || (o[k] as? NSNumber).map { CFGetTypeID($0) == CFBooleanGetTypeID() && !$0.boolValue } == true) }
    let rest = o.keys.filter { !head.contains($0) && $0 != listKey && !(o[$0] is NSNull) && !noChart($0) }.sorted()
    let list: [Any]? = listKey.map { o[$0] as? [Any] } ?? []  // nil: a list given as text or an object
    guard rest.allSatisfy({ o[$0] is String || o[$0] is NSNumber }), let raw = list else { return nil }
    let numbered = !raw.isEmpty && raw.allSatisfy { ($0 as? String)?.range(of: #"^\s*\d+[.)]\s"#, options: .regularExpression) != nil }
    let given = o["kind"] as? String ?? ""
    let kind = ["list", "steps", "stat", "flow", "compare", "quote"].contains(given) ? given : (numbered || (listKey == "steps" && !raw.isEmpty)) ? "steps" : "list"
    let prose = ["sub", "subtitle", "note", "summary", "body", "text", "detail"]  // said, not named: the value is the line
    let extra = rest.compactMap { k -> [String: String]? in
        let v = cardText(o[k])
        return v.isEmpty ? nil : prose.contains(k) ? ["label": v, "detail": "", "tone": ""] : ["label": k, "detail": v, "tone": ""]
    }
    let items = Array((raw.compactMap { cardItem($0, unnumber: kind == "steps") } + extra).prefix(8))
    let title = [o["title"], o["heading"]].map { cardText($0) }.first { !$0.isEmpty } ?? ""
    guard !(title.isEmpty && items.isEmpty) else { return nil }
    return ["title": title, "kind": kind, "eyebrow": cardText(o["eyebrow"]), "stat": cardText(o["stat"]), "items": items]
}

/// Stage-schema enum values (kind/tone/chart) that aren't user facts when falling back to plain lines.
func isStageMeta(_ key: String, _ v: Any) -> Bool {
    guard let s = v as? String else { return false }
    switch key {
    case "kind": return ["list", "steps", "stat", "flow", "compare", "quote", "table", "timeline", "diff", "lifecycle"].contains(s)
    case "tone": return ["needs", "bad", "now", "ok"].contains(s)
    case "chart": return ["bar", "line", "pie"].contains(s)
    default: return false
    }
}

/// Every string and number in a JSON value, in order (an object's title/heading first): what a block the stage can't draw still says.
func jsonLines(_ v: Any) -> [String] {
    if let s = v as? String { return [s] }
    if let n = v as? NSNumber { return [cardText(n)] }
    if let a = v as? [Any] { return a.flatMap(jsonLines) }
    if let d = v as? [String: Any] {
        let rank = { (k: String) in k == "title" ? 0 : k == "heading" ? 1 : 2 }
        return d.keys.sorted { (rank($0), $0) < (rank($1), $1) }.flatMap { isStageMeta($0, d[$0]!) ? [] : jsonLines(d[$0]!) }
    }
    return []
}

/// Single-pass unescape for a broken JSON string token: keeps \\n literal as \n, turns \n/\t/\r into spaces, decodes \uXXXX.
func unescapeJSON(_ s: String) -> String {
    var out = "", i = s.startIndex
    let hex4 = { (from: String.Index) -> (UInt32, String.Index)? in
        guard let end = s.index(from, offsetBy: 4, limitedBy: s.endIndex), let v = UInt32(s[from..<end], radix: 16) else { return nil }
        return (v, end)
    }
    while i < s.endIndex {
        let ch = s[i], next = s.index(after: i)
        guard ch == "\\", next < s.endIndex else { out.append(ch); i = next; continue }
        let esc = s[next], after = s.index(after: next)
        switch esc {
        case "\"", "'", "\\", "/": out.append(esc); i = after
        case "n", "t", "r", "b", "f": out.append(" "); i = after
        case "u":
            if let (u1, e1) = hex4(after) {
                if (0xD800...0xDBFF).contains(u1), e1 < s.endIndex, s[e1...].hasPrefix("\\u"),
                   let (u2, e2) = hex4(s.index(e1, offsetBy: 2)), (0xDC00...0xDFFF).contains(u2),
                   let sc = UnicodeScalar(0x10000 + ((u1 - 0xD800) << 10) + (u2 - 0xDC00)) {
                    out.append(Character(sc)); i = e2; continue
                }
                if let sc = UnicodeScalar(u1) { out.append(Character(sc)); i = e1; continue }
            }
            out.append("\\u"); i = after
        default: out.append("\\"); out.append(esc); i = after
        }
    }
    return out
}

/// The stage's drawing when Haiku can't (no key, an error, a refusal): the first sentence as the title, the rest as a list.
/// JSON (a ```stage block Haiku couldn't draw) is read as a card, else its values as lines, its quoted values when it doesn't
/// parse: never the raw JSON.
func fallbackSpec(_ text: String) -> [String: Any] {
    if let c = stageCard(text) { return c }
    let ss: [String]
    if let j = stageJSON(text) {
        let quoted = { () -> [String] in  // malformed: quoted values left to right, title/heading first, metadata enum values out
            let re = try! NSRegularExpression(pattern: #"(["'])((?:(?!\1)[^\\]|\\.)*)\1(\s*:)?|([A-Za-z_]\w*)\s*:"#)
            var titled: String?, vals = [String](), key: (name: String, end: String.Index)?
            for m in re.matches(in: j, range: NSRange(j.startIndex..., in: j)) {
                guard let full = Range(m.range, in: j) else { continue }
                if m.range(at: 3).location != NSNotFound, let r = Range(m.range(at: 2), in: j) { key = (unescapeJSON(String(j[r])), full.upperBound); continue }
                if m.range(at: 4).location != NSNotFound, let r = Range(m.range(at: 4), in: j) { key = (String(j[r]), full.upperBound); continue }
                guard let r = Range(m.range(at: 2), in: j) else { continue }
                let val = unescapeJSON(String(j[r]))
                let direct = key.flatMap { j[$0.end..<full.lowerBound].allSatisfy(\.isWhitespace) ? $0.name : nil }
                key = nil
                if let direct, isStageMeta(direct, val) { continue }
                if titled == nil, let direct, direct == "title" || direct == "heading", !cardText(val).isEmpty { titled = val } else { vals.append(val) }
            }
            return (titled.map { [$0] } ?? []) + vals
        }
        ss = ((try? JSONSerialization.jsonObject(with: Data(j.utf8))).map(jsonLines) ?? quoted()).map { cardText($0) }.filter { !$0.isEmpty }
    } else { ss = sentences(plain(text) + "\n", from: 0).said }  // JSON with nothing to say draws blank, never its source
    return ["title": ss.first ?? "", "kind": "list", "eyebrow": "", "stat": "", "items": ss.dropFirst().prefix(6).map { ["label": $0, "detail": ""] }]
}

/// One Claude Haiku 5.5 call (no thinking, low effort, structured output), for the quick decisions around Chief's replies.
/// nil when there's no key, or on an error, a refusal or a cut-off reply: the caller keeps what's on screen.
func haiku(_ system: String, _ user: String, _ schema: [String: Any], done: @escaping ([String: Any]?) -> Void) {
    guard let key = keychain("anthropic"), !key.isEmpty else { return done(nil) }
    var q = URLRequest(url: URL(string: "https://api.anthropic.com/v1/messages")!)
    q.httpMethod = "POST"
    q.timeoutInterval = 20
    for (k, v) in ["x-api-key": key, "anthropic-version": "2026-10-21", "content-type": "application/json"] { q.setValue(v, forHTTPHeaderField: k) }
    let body: [String: Any] = [
        "model": "claude-haiku-5-5", "max_tokens": 600, "thinking": ["type": "disabled"],
        "output_config": ["effort": "low", "format": ["type": "json_schema", "schema": schema]],
        "system": system, "messages": [["role": "user", "content": user]]]
    q.httpBody = try? JSONSerialization.data(withJSONObject: body)
    URLSession.shared.dataTask(with: q) { d, resp, err in
        let j = d.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let text = (j?["content"] as? [[String: Any]])?.first { $0["type"] as? String == "text" }?["text"] as? String
        if j?["stop_reason"] as? String != "end_turn" {  // a 400 shouldn't look like "no key": Console.app, process Greenlight
            NSLog("haiku: %@ %@", "\((resp as? HTTPURLResponse)?.statusCode ?? 0)", err?.localizedDescription ?? d.flatMap { String(data: $0.prefix(400), encoding: .utf8) } ?? "")
        }
        let out = j?["stop_reason"] as? String == "end_turn" ? text.flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] } : nil
        DispatchQueue.main.async { done(out) }
    }.resume()
}

/// Haiku draws what Chief is saying as a card for stage.spec.
func drawSpec(_ heard: String, _ reply: String, done: @escaping ([String: Any]?) -> Void) {
    let item: [String: Any] = ["type": "object", "properties": ["label": ["type": "string"], "detail": ["type": "string"],
                                                                "tone": ["type": "string", "enum": ["", "needs", "bad", "now", "ok"]]],
                               "required": ["label", "detail", "tone"], "additionalProperties": false]
    let schema: [String: Any] = ["type": "object", "additionalProperties": false, "required": ["eyebrow", "title", "kind", "stat", "items"], "properties": [
        "title": ["type": "string"], "kind": ["type": "string", "enum": ["list", "steps", "stat", "flow", "compare", "quote"]],
        "eyebrow": ["type": "string"], "stat": ["type": "string"], "items": ["type": "array", "items": item]]]
    haiku("You draw a voice assistant's reply as one glanceable card on the user's screen while it is spoken. "
            + "title: the point in a few words. kind: list (facts), steps (an order), stat (one number matters: put it in stat, e.g. \"3 MRs\"), "
            + "flow (a pipeline or lifecycle, short labels), compare (side by side options), quote (one line to remember). "
            + "eyebrow: 1–4 lowercase words of context above the title (a workspace name, \"needs you · 2\", \"this week\") or empty; no emoji. "
            + "items: at most 6, label a few words, detail one short line or empty (for stat, items are facts: label the name, detail the value). "
            + "tone: needs (waits on the user), bad (failed or crashed), now (in progress), ok (done), else empty; the card shows it as a glyph. "
            + "Use names, numbers and MR refs from the reply; never invent any. stat is empty unless kind is stat.",
          "The user said: \(heard)\n\nThe assistant's reply:\n\(reply)", schema, done: done)
}

// MARK: voice mode's status line (#176): what Chief is doing, on the stage's strip, while it says nothing. Never spoken.

/// Seconds of a running turn with nothing said before the strip gets a fresh line (and again after each line).
let statusQuiet = 20.0  // plus Haiku's second or two: a line about every 20-25 s
/// Words a status line keeps at most: it's a glance, not a report.
let statusWords = 12

/// What Chief has been doing, newest last, for the status line's model: its last few steps' titles (never their details: a shell
/// step's is the whole command line, with whatever tokens, hosts or paths it holds) and whether each is still going, its plan, and
/// a thinking phrase. Not the connecting status either (a host name). Empty when there's nothing to go on.
func statusActivity(_ r: Run) -> String {
    var out = r.steps.suffix(6).map { s in "- \(s.ok == nil ? "running" : s.ok == true ? "done" : "failed") · \(String(s.title.prefix(120)))" }
    if let p = r.plan, p.total > 0 { out.append("Plan: \(p.done) of \(p.total) done" + (p.title.isEmpty ? "" : ", now: \(p.title)")) }
    let now = r.status.trimmingCharacters(in: .whitespacesAndNewlines)
    if r.thinking, !now.isEmpty { out.append("Status: \(String(now.prefix(120)))") }
    return out.joined(separator: "\n")
}

/// A status line fit for the strip: one sentence, at most `statusWords` words, markdown and line breaks off, a full stop at the end.
/// nil when it's empty or says what a line this turn already said (case, spacing and the final mark aside): the strip keeps that one.
func statusLine(_ raw: String, shown: [String]) -> String? {
    var t = plain(raw).replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
    if let end = t.range(of: #"[.!?](\s|$)"#, options: .regularExpression) { t = String(t[..<end.lowerBound]) }  // the first sentence
    let words = t.split(separator: " ")
    t = words.prefix(statusWords).joined(separator: " ").trimmingCharacters(in: CharacterSet(charactersIn: " ,;:-–—…."))
    guard t.rangeOfCharacter(from: .alphanumerics) != nil else { return nil }
    t += words.count > statusWords ? "…" : "."
    let key = { (s: String) in s.lowercased().filter { $0.isLetter || $0.isNumber } }
    return shown.contains { key($0) == key(t) } ? nil : t
}

/// The reply's [screen] detail is on its way: the block streaming now has one and no step has started since its text (else it was
/// commentary before more work, and the quiet that follows still gets lines).
func screenStreaming(_ r: Run) -> Bool {
    staged(r.live).screen != nil && !(r.steps.last.map { $0.start > r.textAt } ?? false)
}

/// The strip's line when the model can't write one (no key, an error): the newest step still going ("Ran gh pr list" as "Running
/// gh pr list"), else the newest one as it's titled; before any step, the turn's own status (a thinking phrase).
func statusFallback(_ r: Run, shown: [String]) -> String? {
    if let s = r.steps.last(where: { $0.ok == nil }) {
        return statusLine(s.title.hasPrefix("Ran ") ? "Running " + s.title.dropFirst(4) : s.title, shown: shown)
    }
    return statusLine(r.steps.last?.title ?? r.status, shown: shown)
}

/// Haiku writes the strip's line from Chief's activity: one short present-tense sentence, nothing it doesn't see, no repeat.
func statusSpec(_ heard: String, _ activity: String, shown: [String], done: @escaping (String?) -> Void) {
    let schema: [String: Any] = ["type": "object", "additionalProperties": false, "required": ["line"], "properties": ["line": ["type": "string"]]]
    haiku("A voice assistant is working on the user's request and hasn't spoken for a while. Write the one status line their screen "
            + "shows meanwhile: what it is doing right now, one sentence in the present tense, under \(statusWords) words, no first person, "
            + "e.g. \"Checking PR 153's CI.\" or \"Reading the auth workspace's last test run.\" Name the newest thing still running; "
            + "when it's the same work as before, say how it's going instead (\"Still waiting on the test run.\"). "
            + "Use only names, numbers and refs from the activity; never invent any. No markdown, no emoji, no commands or paths.",
          (heard.isEmpty ? "" : "The user said: \(heard)\n\n") + "Its activity, newest last:\n\(activity)"
            + (shown.isEmpty ? "" : "\n\nLines already shown (say something new):\n" + shown.map { "- " + $0 }.joined(separator: "\n")),
          schema) { o in done(o?["line"] as? String) }
}

/// The stage's card for one workspace: its MR, where it is and what's next, from the dashboard's snapshot.
func workspaceSpec(_ p: [String: Any]) -> [String: Any] {
    let s = { (k: String) in p[k] as? String ?? "" }, mr = p["mr"] as? [String: Any] ?? [:]
    let label = ["needs": "Needs you", "crashed": "Crashed", "raised": "Your move", "working": "Working", "queued": "Queued", "quiet": "Waiting on others", "done": "Done"]
    let items: [[String: String]] = [
        ["label": [mr["ref"] as? String, mr["title"] as? String].compactMap { $0 }.joined(separator: " "),
         "detail": [mr["draft"] as? Bool == true ? "draft" : nil, (mr["pipeline"] as? String).map { "CI \($0)" }, mr["conflicts"] as? Bool == true ? "conflicts" : nil].compactMap { $0 }.joined(separator: " · ")],
        ["label": label[s("state")] ?? s("state"), "detail": s("why")],
        ["label": "Next: " + s("step").replacingOccurrences(of: "_", with: " "), "detail": s("action")],
        ["label": "Goal", "detail": s("goal")],
    ].filter { !($0["label"]!.isEmpty || $0["label"] == "Goal" && $0["detail"]!.isEmpty) }
    return ["title": s("name"), "kind": "list", "eyebrow": (label[s("state")] ?? s("state")).lowercased(), "stat": "", "items": items]
}

/// The window you were last in, in words for Chief: a terminal's project and tab, or the dashboard's open workspace.
func focusLine(terminal id: Int?, project: String, _ o: [String: Any]) -> String {
    let s = { (k: String) in (o[k] as? String).map { String($0.prefix(200)) } ?? "" }
    if id != nil {
        let tab = s("name").isEmpty ? "" : ", on the tab for workspace \(s("name")) (\(s("path"))\(s("mr").isEmpty ? "" : ", \(s("mr"))"))"
        return "a Basement terminal window" + (project.isEmpty ? "" : " showing \(project)") + tab
    }
    return s("page").isEmpty ? "the dashboard (\(s("view")))" : "the dashboard, with workspace \(s("page")) open"
}

/// The window's scope for Chief's BOARD (`chat.py send --only`), from a page's focus(): its worktrees' paths and their repos, so a
/// workspace created after the window listed them (`ade worktree create`) is on the board too. nil: the window shows every project.
func focusScope(_ o: [String: Any]) -> String? {
    guard let paths = o["paths"] as? [String] else { return nil }
    let scope: [String: [String]] = ["paths": paths, "repos": o["repos"] as? [String] ?? []]
    return (try? JSONSerialization.data(withJSONObject: scope, options: .sortedKeys)).flatMap { String(data: $0, encoding: .utf8) }
}

/// Chief's scope from the projects picked: their worktrees and repos while a window showing them is up. nil: every project (none
/// picked, or no window up: you're in the menubar alone).
func windowScope(paths: [String]?, repos: [String]?, shown: Bool) -> String? {
    guard shown, let paths else { return nil }
    return focusScope(["paths": paths, "repos": repos ?? []])
}

/// Chief's [hexes: …] names to the board's: exact, the short name (after a "/"), or an MR/PR ref; unknown ones dropped.
func resolveHexes(_ said: [String], _ pucks: [[String: Any]]) -> [String] {
    let names = pucks.compactMap { $0["name"] as? String }
    return said.compactMap { w in
        let k = w.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "`*\"' "))
        return names.first { $0.lowercased() == k } ?? names.first { $0.lowercased().split(separator: "/").last.map(String.init) == k }
            ?? pucks.first { ((($0["mr"] as? [String: Any])?["ref"] as? String)?.lowercased()) == k }?["name"] as? String
    }.reduce(into: []) { if !$0.contains($1) { $0.append($1) } }
}

/// The model's pick counts only when the reply points at it: its name, its MR number, or two of its title's longer words.
/// ponytail: without this the small model lights the top of the list for small talk
func grounded(_ reply: String, _ p: [String: Any]) -> Bool {
    let low = reply.lowercased(), mr = p["mr"] as? [String: Any] ?? [:]
    let name = String((p["name"] as? String ?? "").lowercased().split(separator: "/").last ?? "")
    let num = (mr["ref"] as? String).map { String($0.drop { !$0.isNumber }) } ?? ""
    let has = { (k: String) in low.range(of: "(?<![\\w])" + NSRegularExpression.escapedPattern(for: k) + "(?![\\w])", options: .regularExpression) != nil }
    let words = (mr["title"] as? String ?? "").lowercased().split { !$0.isLetter }.map(String.init).filter { $0.count >= 5 }
    return !name.isEmpty && has(name) || !num.isEmpty && has(num) || Set(words.filter(has)).count >= 2
}

/// Workspaces a reply names outright: its MR/PR ref (!5339, #18) or a distinctive name (mr-5339, lifecycle-advice; not a plain word).
func namedHexes(_ reply: String, _ pucks: [[String: Any]]) -> [String] {
    let low = reply.lowercased()
    let has = { (k: String) in low.range(of: "(?<![\\w!#-])" + NSRegularExpression.escapedPattern(for: k.lowercased()) + "(?![\\w-])", options: .regularExpression) != nil }
    return pucks.compactMap { p in
        guard let n = p["name"] as? String else { return nil }
        let short = String(n.split(separator: "/").last ?? Substring(n))
        let ref = (p["mr"] as? [String: Any])?["ref"] as? String
        return (ref.map(has) ?? false) || (short.count >= 5 && short.contains { !$0.isLetter } && has(short)) ? n : nil
    }
}

/// The decision model for the dashboard: which workspaces (hexes) Chief's reply is about. The ones it names outright, plus what
/// Apple's on-device model picks (local, no key; only real names, an enum of them). Without Apple Intelligence: the named ones.
/// The user asked to see workspaces ("highlight…", "show me…", "light up…", "point at…", "which ones…"): the picker assumes yes.
func asksToShow(_ s: String) -> Bool {
    s.range(of: #"\b(highlight|light (them |it |those |these )?up|show me|show (them|those|these|which)|point (me )?(at|to|out)|which (ones?|workspaces?|hex(es)?|mrs?|prs?)|where (is|are)|find)\b"#,
            options: [.regularExpression, .caseInsensitive]) != nil
}

/// How sure System One must be that a reply is about a workspace before its hex lights, and before its card opens by itself.
let lightAt = 0.70, openAt = 0.85

func hexCandidates(_ reply: String, _ pucks: [[String: Any]], named: [String]) -> [[String: Any]] {
    var seen = Set(named)
    return Array(pucks.filter { p in
        guard let n = p["name"] as? String, !seen.contains(n), grounded(reply, p) else { return false }
        seen.insert(n)
        return true
    }.prefix(12))
}

/// The hexes a reply is about, and whether the dashboard should open the one lit hex's card (`open`: it's that sure).
/// The ones it names outright are certain. The others it points at (grounded: a name, an MR number, title words) go to System One
/// (`decide.py batch` where greenlight runs, one yes/no each): P > 0.70 lights, a lone hex at P > 0.85 opens. With no System One model
/// set up there, Apple's on-device model picks as before, and only a hex named outright opens.
/// `asked`: the user asked to see some (asksToShow): the questions say so, and the on-device picker skips its "particular ones?" gate.
func pickHexes(_ reply: String, _ pucks: [[String: Any]], asked: Bool = false, done: @escaping ([String], Bool) -> Void) {
    let named = namedHexes(reply, pucks)
    let cands = hexCandidates(reply, pucks, named: named)  // ponytail: 12 at most; more is about the board
    guard !cands.isEmpty else { return done(named, named.count == 1) }
    var qs: [String: Any] = [:]
    for (i, p) in cands.enumerated() {
        let mr = p["mr"] as? [String: Any]
        let what = [p["name"] as? String, mr?["ref"] as? String, (mr?["title"] as? String).map { String($0.prefix(80)) }].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " | ")
        qs["h\(i)"] = ["type": "bool", "question": "Is the reply about this workspace in particular (it names, recommends, asks about or acts on it): \(what)?",
                       "context": "An assistant's reply to the user, about the user's coding workspaces." + (asked ? " The user asked to be shown workspaces." : "")
                         + " A reply about all of them in general is about none in particular."]
    }
    guard let d = try? JSONSerialization.data(withJSONObject: ["state": String(reply.prefix(4000)), "questions": qs]) else { return done(named, named.count == 1) }
    var out = ""
    ssh("\(py)/decide.py batch", stdin: String(decoding: d, as: UTF8.self), onLine: { out += $0 }, onExit: { code in
        let a = code == 0 ? (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: [String: Any]] : nil
        guard let a, a.values.contains(where: { $0["provider"] as? String != "local" }) else {  // no model there (or no answer): on-device
            return applePick(reply, pucks, asked: asked, named: named) { names in done(names, names.count == 1 && named.contains(names[0])) }
        }
        let probs = cands.enumerated().compactMap { i, p -> (String, Double)? in
            guard let n = p["name"] as? String, let pr = a["h\(i)"]?["prob"] as? Double, pr > lightAt else { return nil }
            return (n, pr)
        }
        let lit = named + probs.map(\.0)
        let sure = lit.count == 1 && (named.count == 1 || probs.first.map { $0.1 > openAt } == true)
        NSLog("pick: %@", probs.map { "\($0.0)=\($0.1)" }.joined(separator: " "))
        done(lit, sure)
    })
}

/// Apple's on-device model's picks (local, no key; only real names, an enum of them), grounded, after the named ones.
/// Without Apple Intelligence: the named ones.
func applePick(_ reply: String, _ pucks: [[String: Any]], asked: Bool, named: [String], done: @escaping ([String]) -> Void) {
    #if canImport(FoundationModels)
    guard #available(macOS 26.0, *) else { return done(named) }
    var seen = Set<String>()  // the same workspace twice (a stack's members, two hosts) would be a duplicate enum case
    let ws = pucks.compactMap { p -> (String, String)? in  // ponytail: the on-device context is ~4k tokens; 60 workspaces fit
        guard let n = p["name"] as? String, seen.insert(n).inserted else { return nil }
        let mr = p["mr"] as? [String: Any]
        return (n, [n, mr?["ref"] as? String, (mr?["title"] as? String).map { String($0.prefix(60)) }].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " | "))
    }.prefix(60)
    guard !ws.isEmpty, SystemLanguageModel.default.isAvailable else { return done(named) }
    nonisolated(unsafe) let finish = done
    Task {
        var picked: [String] = []
        do {
            let root = DynamicGenerationSchema(name: "Pick", properties: [  // "specific" first: without it the small model picks every one for small talk
                .init(name: "specific", description: "does the reply name or point at particular workspaces?", schema: DynamicGenerationSchema(type: Bool.self)),
                .init(name: "hexes", description: "workspaces the reply is about; empty when none",
                schema: DynamicGenerationSchema(arrayOf: DynamicGenerationSchema(name: "Workspace", anyOf: ws.map(\.0)), maximumElements: 8))])
            let session = LanguageModelSession(instructions: "You decide which of the user's workspaces an assistant's reply is about, to highlight them "
                + "on the user's dashboard: the ones it names, recommends, asks about or acts on (by name, branch, MR/PR number or title). "
                + "Return an empty list when the reply is about none of them, or about all of them in general. Never guess.")
            let r = try await session.respond(to: "Workspaces (name | MR | title):\n" + ws.map(\.1).joined(separator: "\n") + "\n\nThe reply:\n" + String(reply.prefix(1500)),
                                              schema: try GenerationSchema(root: root, dependencies: []), options: GenerationOptions(temperature: 0))
            picked = try asked || r.content.value(Bool.self, forProperty: "specific") ? r.content.value([String].self, forProperty: "hexes") : []
            if picked.count > 3 && picked.count >= ws.count { picked = [] }  // all of them is about none in particular
        } catch { NSLog("pick: %@", "\(error)") }
        let ok = Set(picked.filter { n in pucks.contains { $0["name"] as? String == n && grounded(reply, $0) } })
        let out = (named + picked.filter(ok.contains)).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        await MainActor.run { finish(out) }
    }
    #else
    done(named)
    #endif
}

// macOS 27 deprecated AVAudio's connect, installTap and play, which raise on failure, for ones that throw: these call the new ones there.
// ponytail: compiler(>=6.4) stands in for "the macOS 27 SDK" (they came together); an older SDK has only the old calls
extension AVAudioEngine {
    func link(_ a: AVAudioNode, to b: AVAudioNode, format f: AVAudioFormat?) throws {
        #if compiler(>=6.4)
        if #available(macOS 27, *) { try connectNode(a, to: b, format: f) } else { connect(a, to: b, format: f) }
        #else
        connect(a, to: b, format: f)
        #endif
    }
}
extension AVAudioNode {
    func tap(onBus bus: AVAudioNodeBus, bufferSize n: AVAudioFrameCount, format f: AVAudioFormat?, block: @escaping AVAudioNodeTapBlock) throws {
        #if compiler(>=6.4)
        if #available(macOS 27, *) { try __installTap(onBus: bus, bufferSize: n, format: f, error: (), block: block) }  // (): the NSError** the importer turned into throws
        else { installTap(onBus: bus, bufferSize: n, format: f, block: block) }
        #else
        installTap(onBus: bus, bufferSize: n, format: f, block: block)
        #endif
    }
}
extension AVAudioPlayerNode {
    func start() throws {
        #if compiler(>=6.4)
        if #available(macOS 27, *) { try playAudio() } else { play() }
        #else
        play()
        #endif
    }
}

/// One buffer in another format (rate, channels, sample type): the mic to 16 kHz mono for recognition, speech to the player's.
/// whole: b is all there is (a line from Kokoro/ElevenLabs), so flush the resampler's tail instead of holding it for the next call.
func convert(_ b: AVAudioPCMBuffer, _ c: AVAudioConverter, _ to: AVAudioFormat, whole: Bool = false) -> AVAudioPCMBuffer? {
    defer { if whole { c.reset() } }
    guard let out = AVAudioPCMBuffer(pcmFormat: to, frameCapacity: AVAudioFrameCount(Double(b.frameLength) * to.sampleRate / b.format.sampleRate) + 32) else { return nil }
    var fed = false, err: NSError?
    c.convert(to: out, error: &err) { _, status in
        if fed { status.pointee = whole ? .endOfStream : .noDataNow; return nil }
        fed = true; status.pointee = .haveData; return b
    }
    return err == nil && out.frameLength > 0 ? out : nil
}

/// 16-bit mono PCM as a WAV file, for AVAudioPlayer (Settings' voice test).
func wav(_ pcm: Data, rate: UInt32) -> Data {
    var d = Data()
    func put<T: FixedWidthInteger>(_ v: T) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
    d += Data("RIFF".utf8); put(UInt32(36 + pcm.count)); d += Data("WAVEfmt ".utf8)
    put(UInt32(16)); put(UInt16(1)); put(UInt16(1)); put(rate); put(rate * 2); put(UInt16(2)); put(UInt16(16))
    d += Data("data".utf8); put(UInt32(pcm.count)); d += pcm
    return d
}

/// A buffer's loudness, 0…1 (−50 dB…−5 dB), for the waveforms.
func level(_ b: AVAudioPCMBuffer) -> Float {
    guard let ch = b.floatChannelData, b.frameLength > 0 else { return 0 }
    var sum: Float = 0
    for i in 0..<Int(b.frameLength) { sum += ch[0][i] * ch[0][i] }
    return max(0, min(1, (20 * log10(max(sqrt(sum / Float(b.frameLength)), 1e-6)) + 50) / 45))
}

let voiceNote = "(Voice mode: the user is talking to you out loud. Your reply has two parts. First the spoken part, read aloud: the gist, "
    + "one or two short sentences, the answer first, no preamble or recap, like a person talking; no markdown, links or lists. Write it for the ear: "
    + "never read out commit hashes, MR or PR numbers, IDs, branch names, file paths, URLs or symbols; say \"the latest commit\" or \"the three reviews on the API\" "
    + "or name them by what they do; short sentences, plain words. Then a line [screen] and the full explanation as text: "
    + "whenever the answer names workspaces, MRs, numbers, steps or reasons, those go there, each MR as a markdown link with what it needs. "
    + "It's shown on their screen and in the transcript under the gist, never spoken, so the two needn't match, and it's exempt from the "
    + "few-short-lines rule (lists and bold are fine there; still no tables or headings). Put [screen] alone on its line, and an Options line last, after the text. "
    + "Leave [screen] out only when the gist says it all (a yes, a done, a single fact). Their screen shows a stage beside your voice: "
    + "the [screen] text, or else a card a fast model draws from what you said. "
    + "To draw it yourself instead, a chart or a picture, or when they ask you to draw or redraw something, add one ```stage fenced block (it outranks [screen] on the stage): "
    + "for a chart or graph, JSON the stage draws instantly: {\"chart\": \"bar\"|\"line\"|\"pie\", \"title\": …, \"sub\": …, \"labels\": [x labels], "
    + "\"series\": [{\"name\": …, \"values\": [numbers], \"color\": green|red|gray|blue|amber|purple|teal|pink}], \"stacked\": true, \"note\": …} "
    + "(pie: one series, a slice per label; never hand-draw a chart in SVG); for a card, JSON {\"title\": …, \"items\": [short lines]}; otherwise plain notes (drawn for you) or a self-contained HTML fragment with inline CSS or SVG and no scripts (shown as is; theme colors: "
    + "var(--text), var(--dim), var(--accent), var(--card), var(--line)). When the conversation is over (they say goodbye, thanks that's it, or ask you to stop listening "
    + "or turn voice mode off), say a short goodbye and add a line [voice: off]: voice mode ends after you finish speaking.)\n"

/// The waveforms' levels, apart from Voice so 40 updates a second redraw only the bars.
final class Meter: ObservableObject {
    @Published var you = [Float](repeating: 0, count: 48)
    @Published var chief = [Float](repeating: 0, count: 48)
}

/// The chat's and voice mode's looks (brand/DESIGN.md §10.17): token sets on the same components, switched by the header's
/// style button. Default wears the system tokens in the current appearance; Phosphor is a mono, green-on-near-black homage to
/// the terminal, built from the neutral and Signal scales. `you` inks what you said (the captions' heard line, your turns in the
/// transcript), apart from the look's text, which inks what Chief says. `link` is the accent as text; `accent` is its fill.
struct Look { let id, name, page, stage, card, text, text2, dim, line, accent, link, you, font: String; let dark: Bool }
let sansCSS = "-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif", monoCSS = "ui-monospace,'SF Mono',Menlo,monospace"
/// Default: the chat pane is bg.surface on the canvas, bubbles element.rest, hairlines border.subtle (§2, §10.17).
func defaultLook(dark d: Bool) -> Look {
    let p = DS.Palette.self
    return Look(id: "default", name: "Default", page: dsHex(p.bgCanvas, dark: d), stage: dsHex(p.bgSurface, dark: d), card: dsHex(p.elementRest, dark: d),
                text: dsHex(p.fgDefault, dark: d), text2: dsHex(p.fgMuted, dark: d), dim: dsHex(p.fgMuted, dark: d), line: dsHex(p.borderSubtle, dark: d),
                accent: dsHex(accentSet.solid, dark: d), link: dsHex(accentSet.fg, dark: d), you: dsHex(DS.Palette.accentFg, dark: d), font: sansCSS, dark: d)
}
/// Phosphor: always the dark scales. Signal's text step on the darkest neutral, its subtle step for wells, its solid for rules.
let phosphorLook: Look = {
    let p = DS.Palette.self
    return Look(id: "phosphor", name: "Phosphor", page: dsHex(p.bgCanvas, dark: true), stage: dsHex(p.bgCanvas, dark: true), card: dsHex(p.accentSubtle, dark: true),
                text: dsHex(p.accentFg, dark: true), text2: dsHex(p.accentFg, dark: true), dim: dsHex(p.accentHover, dark: true), line: dsHex(p.accentSolid, dark: true),
                accent: dsHex(p.accentFg, dark: true), link: dsHex(p.accentFg, dark: true), you: dsHex(p.fgDefault, dark: true), font: monoCSS, dark: true)
}()
/// The looks the style button cycles through. The five parked looks (Orb, Conversation, On Air, Editorial, HUD, Zen) are gone
/// from the pickers; a stored choice of one opens as Default.
var looks: [Look] { [defaultLook(dark: isDark), phosphorLook] }
/// A look by its stored id, in today's appearance: anything but Phosphor is Default.
func lookNamed(_ id: String?) -> Look { id == "phosphor" ? phosphorLook : defaultLook(dark: isDark) }
extension Look {
    /// What the stage page needs to wear the look: its data-look (stage.html's token overrides). Colours come from the tokens
    /// and the appearance, and a theme of your own from themeVars().
    var vars: String { "document.documentElement.dataset.look = '\(id == "phosphor" ? "phosphor" : "default")';" }
}
func hx(_ h: String, _ o: Double = 1) -> Color { Color(nsColor: hex(h)).opacity(o) }
/// WCAG contrast ratio of two hex colors (1…21): the captions keep your words at 4.5 or more on the look's stage.
func contrast(_ a: String, _ b: String) -> Double {
    func lum(_ h: String) -> Double {
        let c = hex(h), ch = [c.redComponent, c.greenComponent, c.blueComponent].map { v -> Double in let v = Double(v); return v <= 0.04045 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    }
    let x = lum(a), y = lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)
}
/// A hex color's hue in degrees and its saturation (0…1).
func hueSat(_ h: String) -> (hue: Double, sat: Double) { let c = hex(h); return (Double(c.hueComponent) * 360, Double(c.saturationComponent)) }
/// The chat look's id ("default" or "phosphor"); Voice.look sets it.
var chatLookId = ud.string(forKey: "stageStyle") == "phosphor" ? "phosphor" : "default"
/// The chat window (standard and live) wears the same look. Chat views adopt Looked: its members shadow the app-wide color
/// globals (bg, fg, accent, card…, theme) and chatFace, so their bodies read the look's tokens unchanged.
var chatLook: Look { lookNamed(chatLookId) }
protocol Looked {}
extension Looked {
    var lk: Look { chatLook }
    var bg: Color { hx(lk.stage) }
    var page: Color { hx(lk.page) }
    var fg: Color { hx(lk.text) }
    var accent: Color { hx(lk.accent) }
    var link: Color { hx(lk.link) }
    var onAccent: Color { mono ? page : Color(accentSet.onSolid, dark: lk.dark) }
    var card: Color { hx(lk.card) }
    var surface: Color { hx(lk.card) }
    var line: Color { hx(lk.line) }
    var edge: Color { mono ? line : Color(DS.Palette.borderDefault, dark: lk.dark) }
    var dim: Color { hx(lk.dim) }
    var faint: Color { mono ? dim : Color(DS.Palette.fgFaint, dark: lk.dark) }
    var sub: Color { hx(lk.text2) }
    var youInk: Color { hx(lk.you) }  // what you said, apart from Chief's words in fg
    var bubble: Color { hx(lk.card) }
    var mono: Bool { lk.id == "phosphor" }
    /// Status colours (§2.4), one meaning each, in the look's appearance: never the accent.
    func state(_ c: DSColor) -> Color { Color(c, dark: lk.dark) }
    var needs: Color { state(DS.Palette.statusNeedsSolid) }
    var needsInk: Color { state(DS.Palette.statusNeedsFg) }
    var done: Color { state(DS.Palette.statusDoneFg) }
    var crashed: Color { state(DS.Palette.statusCrashedSolid) }
    var crashedInk: Color { state(DS.Palette.statusCrashedFg) }
    var working: Color { state(DS.Palette.statusWorkingFg) }
    /// The conversation's text in the look's face: mono for Phosphor, else the chosen chat font.
    func chatFace(size: CGFloat, weight: Font.Weight = .regular) -> Font {
        mono ? sys(size: size, weight: weight, design: .monospaced) : sys(size: size, weight: weight, face: chatFont)
    }
    /// A type role in the look's face for chrome: mono for Phosphor.
    func ui(_ t: DSType, _ w: Font.Weight? = nil) -> Font { sys(size: t.size, weight: w ?? t.fontWeight, design: mono ? .monospaced : .default) }
}

/// Claude Code's live line, under the reply in the chat and in every voice look: the running call, then the spinner with time, tokens and speed.
/// ponytail: no live shell output; claude's stream only sends a tool's elapsed seconds while it runs.
struct Activity: View {
    @ObservedObject var c: Core
    @ObservedObject var r: Run
    let fg: Color, dim: Color, accent: Color, size: CGFloat
    var body: some View {
        let t = max(0, c.now.timeIntervalSince(r.start)), cur = r.steps.last.flatMap { $0.ok == nil ? $0 : nil }
        let w = r.stopping ? "Stopping…" : c.voice.waitWord.hasSuffix("…") ? c.voice.waitWord : c.voice.waitWord + "…"
        let bits = [fmt(t), c.outTok > 0 ? "↓ \(kfmt(c.outTok)) tokens" : nil, c.tps > 0 ? "\(Int(c.tps)) tok/s" : nil,
                    r.thinking ? "thinking" : nil].compactMap { $0 }.joined(separator: " · ")
        VStack(alignment: .leading, spacing: 2) {
            if let s = cur {
                Text("\(Text("⏺ ").foregroundColor(accent))\(r.call.isEmpty ? s.title : r.call)").foregroundColor(fg).lineLimit(1).truncationMode(.middle)
                Text("  ⎿  Running… \(fmt(c.now.timeIntervalSince(s.start)))").foregroundColor(dim)
            }
            Text("\(Text(["·", "✢", "✳", "✶", "✻", "✽"][Int(t) % 6] + " ").foregroundColor(accent))\(Text(w).foregroundColor(fg)) \(Text("(\(bits))").foregroundColor(dim))").lineLimit(1)
        }.font(sys(size: size, design: .monospaced)).textSelection(.enabled).accessibilityElement(children: .combine)
    }
}

/// A workspace's agent (Claude Code, Codex…) that's working or asking you, from dash.py's snapshot (finished ones aren't counted); id is the worktree's path.
struct Agent: Equatable, Identifiable { let id: String, name: String, state: String, type: String }
func agentsOf(_ pucks: [[String: Any]]) -> [Agent] {
    pucks.compactMap { p in
        guard let a = p["agent"] as? [String: Any], let st = a["state"] as? String, ["working", "waiting"].contains(st), let path = p["path"] as? String else { return nil }
        return Agent(id: path, name: p["name"] as? String ?? path, state: st, type: a["type"] as? String ?? "")
    }.sorted { ($0.state == "waiting" ? 0 : 1, $0.name) < ($1.state == "waiting" ? 0 : 1, $1.name) }
}

enum Talk { case speaking, listening, thinking, muted }
/// Who has the floor: Chief speaking or thinking comes first; with neither, you're heard unless you muted the mic.
func voiceTalk(speaking: Bool, thinking: Bool, micMuted: Bool) -> Talk { speaking ? .speaking : thinking ? .thinking : micMuted ? .muted : .listening }
/// The status every look shows: muted is said even while Chief speaks or thinks, since some looks show only this.
func voiceTalkLabel(_ t: Talk, micMuted: Bool = false) -> String {
    switch t {
    case .speaking: return micMuted ? "Speaking · muted" : "Speaking"
    case .thinking: return micMuted ? "Thinking · muted" : "Thinking"
    case .muted: return "Muted"
    case .listening: return "Listening"
    }
}
/// Window ▸ the mic's item, by what choosing it does now.
func micMenuTitle(muted: Bool) -> String { muted ? "Unmute Microphone" : "Mute Microphone" }
/// Every look's hint line while the mic is muted, whoever has the floor.
let mutedHint = "mic muted · ⇧⌘M unmutes"
/// Whether talking over the voice cuts it off: only with echo cancellation (else the mic hears the voice itself), and GPT-Live
/// takes turns on its own whatever "Interrupt by talking" says.
func voiceInterruptible(live: Bool, vp: Bool, bargeIn: Bool) -> Bool { vp && (live || bargeIn) }
/// What every look shows, from Voice and Core.
struct Live {
    let v: Voice, c: Core
    var talk: Talk { voiceTalk(speaking: v.speaking > 0, thinking: c.run != nil, micMuted: v.micMuted) }
    var label: String { voiceTalkLabel(talk, micMuted: v.micMuted) }
    var hint: String { v.micMuted ? mutedHint : talk == .speaking ? (!v.fallback.isEmpty && !v.isLive ? "Apple's voice: \(v.fallback)" : voiceInterruptible(live: v.isLive, vp: v.vp, bargeIn: v.bargeIn) ? "talk to interrupt" : "speaking") : talk == .thinking ? "say “stop” to cancel" : "say “goodbye” to end voice mode" }
    var said: String { v.captions ? v.caption : "" }
    var heard: String { v.captions ? v.heard : "" }
    var frac: Double { c.ctxFull }
    var pct: Int { Int((frac * 100).rounded()) }
    var known: Bool { c.window > 0 }  // else tokens only: an unknown window is no "0%"
    var high: Bool { frac > 0.75 }
    var tokens: String { c.window > 0 ? "\(kfmt(c.context)) of \(kfmt(c.window))" : kfmt(c.context) }
    var name: String { c.name }
    /// The live line, in the look's colors, while Chief works (off in Settings → Chat & voice).
    /// With verbose activity on, the live log's newest lines follow it.
    @ViewBuilder func activity(_ l: Look, ink: Color? = nil, accent: Color? = nil, size: CGFloat = DS.TypeRole.label.size) -> some View {
        if c.verbose {
            VStack(alignment: .leading, spacing: 6) {
                if c.liveLine, let r = c.run { Activity(c: c, r: r, fg: ink ?? hx(l.text), dim: hx(l.dim), accent: accent ?? hx(l.accent), size: size) }
                FeedTail(f: c.feed, n: 3, fg: ink ?? hx(l.text), dim: hx(l.dim), accent: accent ?? hx(l.accent), size: size - 1)
            }.frame(maxWidth: .infinity, alignment: .leading)
        } else if c.liveLine, let r = c.run { Activity(c: c, r: r, fg: ink ?? hx(l.text), dim: hx(l.dim), accent: accent ?? hx(l.accent), size: size).frame(maxWidth: .infinity, alignment: .leading) }
    }
    func clear() { v.run(.reset) }
    /// The stage, inset in its card so the card's rounded corners show (the web view itself is square).
    func stage(_ l: Look, r: CGFloat = DS.Radius.md, pad: CGFloat = DS.Space.s8, border: Color? = nil) -> some View {
        StageWeb(web: v.stageWeb).padding(pad).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(RoundedRectangle(cornerRadius: r).fill(hx(l.stage)))
            .overlay(RoundedRectangle(cornerRadius: r).stroke(border ?? .clear))
    }
    /// The context readout as a button: its popover has the totals and Compact.
    func ctx<L: View>(@ViewBuilder _ label: () -> L) -> some View {
        Button { c.ctxOpen.toggle() } label: { label() }.buttonStyle(.plain).accessibilityLabel("Context, \(c.ctxSpoken)").modifier(CtxPop(c: c))
    }
    func clearButton<L: View>(@ViewBuilder _ label: () -> L) -> some View {
        Button(action: clear) { label() }.buttonStyle(.plain).help("Clear the context and start a new conversation (or say “reset”)").accessibilityLabel("Clear the context")
    }
}

/// Voice mode's screen, in place of the chat, in the look the header's style button last picked (§10.18).
struct StageView: View {
    @ObservedObject var v: Voice
    @ObservedObject var c: Core
    var body: some View {
        let s = Live(v: v, c: c), l = lookNamed(v.look.id)  // Default follows today's appearance
        Group {
            if l.id == "phosphor" { PhosphorLook(s: s, l: l) } else { DefaultLook(s: s, l: l) }
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) { DwimToastBar(c: c, l: l).padding(.horizontal, DS.Space.s12).padding(.bottom, DS.Space.s8); StageFiles(c: c, l: l) }
                .animation(.easeOut(duration: DS.Duration.fast), value: c.toast)
        }
        .background(hx(l.page))
        .overlay { DropArea(c: c) }  // files dragged anywhere on the stage go with what you say next, as on the chat
        .overlay { if c.dropping { StageDropHint(l: l) } }
        .environment(\.colorScheme, l.dark ? .dark : .light)
    }
}

/// Voice mode's files: the ones waiting to go with what you say next (dropped on the stage, pasted, or picked with the header's
/// paperclip), each with × to take it off; why the last one couldn't attach; and that some are still being read. Nothing when there are none.
struct StageFiles: View {
    @ObservedObject var c: Core
    let l: Look
    var body: some View {
        if !c.files.isEmpty || !c.fileNote.isEmpty || c.reading > 0 {
            VStack(alignment: .leading, spacing: DS.Space.s8) {
                if !c.files.isEmpty || c.reading > 0 {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: DS.Space.s8) {
                            ForEach(c.files) { chip($0) }
                            if c.reading > 0 { HStack(spacing: DS.Space.s4) { ProgressView().controlSize(.mini); Text("Reading…") }.font(ds(DS.TypeRole.caption)).foregroundColor(hx(l.dim)) }
                        }
                    }
                    Text(c.reading > 0 && c.files.isEmpty ? "Checking the file…" : "\(c.files.count == 1 ? "Goes" : "These go") to \(c.name) with what you say next")
                        .font(ds(DS.TypeRole.caption)).foregroundColor(hx(l.dim)).lineLimit(1)
                }
                if !c.fileNote.isEmpty {
                    Label { Text(c.fileNote) } icon: { Image(ds: "needs", size: iconSize(DS.TypeRole.caption.size)) }.font(ds(DS.TypeRole.labelSm))
                        .foregroundColor(Color(DS.Palette.statusNeedsFg, dark: l.dark))
                        .fixedSize(horizontal: false, vertical: true).accessibilityLabel("Not attached: \(c.fileNote)")
                }
            }.padding(.horizontal, DS.Space.s16).padding(.vertical, DS.Space.s8).frame(maxWidth: .infinity, alignment: .leading)
                .background(hx(l.stage)).overlay(alignment: .top) { Rectangle().fill(hx(l.line)).frame(height: 1) }
        }
    }
    func chip(_ f: ChatFile) -> some View {
        HStack(spacing: DS.Space.s4) {
            if let t = f.thumb { Image(nsImage: t).resizable().aspectRatio(contentMode: .fill).frame(width: 18, height: 18).clipShape(RoundedRectangle(cornerRadius: DS.Radius.xs)) }
            else { Image(ds: "file-text", size: iconSize(DS.TypeRole.caption.size, scaled: false)).foregroundColor(hx(l.dim)) }
            Text(f.name).font(ds(DS.TypeRole.labelSm)).foregroundColor(hx(l.text)).lineLimit(1).truncationMode(.middle).frame(maxWidth: 150, alignment: .leading)
            Text(byteSize(f.data.count)).font(ds(DS.TypeRole.caption).monospacedDigit()).foregroundColor(hx(l.dim)).lineLimit(1).fixedSize()
            Button { c.files.removeAll { $0.id == f.id } } label: {
                Image(ds: "close", size: iconSize(9, scaled: false), medium: true).foregroundColor(hx(l.dim)).frame(width: 16, height: 16)
            }.buttonStyle(.plain).accessibilityLabel("Remove \(f.name)")
        }.padding(.leading, DS.Space.s4).padding(.trailing, DS.Space.s4).frame(height: DS.Layout.controlHeightSm)
            .background(RoundedRectangle(cornerRadius: l.id == "phosphor" ? 0 : DS.Radius.sm).fill(hx(l.card)))
            .help("\(f.kindName), goes to \(c.name) with what you say next")
            .accessibilityElement(children: .contain).accessibilityLabel("Attached \(f.name), \(byteSize(f.data.count))")
    }
}
/// Files dragged over the stage: where they'll go, in the stage's look.
struct StageDropHint: View {
    let l: Look
    var body: some View {
        let r = l.id == "phosphor" ? 0 : DS.Radius.lg
        RoundedRectangle(cornerRadius: r).fill(hx(l.page).opacity(0.9))
            .overlay(RoundedRectangle(cornerRadius: r).stroke(hx(l.accent), style: StrokeStyle(lineWidth: 2, dash: [6, 4])))
            .overlay(VStack(spacing: DS.Space.s8) {
                Image(ds: "attach", size: iconSize(DS.TypeRole.title.size, scaled: false)).foregroundColor(hx(l.link))
                Text("Drop to attach").font(ds(DS.TypeRole.titleSm)).foregroundColor(hx(l.text))
                Text("Goes with what you say next: \(ChatFile.supported)").font(ds(DS.TypeRole.caption)).foregroundColor(hx(l.dim)).multilineTextAlignment(.center)
            }.padding(.horizontal, DS.Space.s24))
            .padding(DS.Space.s12).allowsHitTesting(false).accessibilityHidden(true)
    }
}

/// The last n levels, Chief's where it's louder (it's speaking), yours otherwise.
func levels(_ m: Meter, _ n: Int) -> [(Float, Bool)] { zip(m.chief, m.you).suffix(n).map { a, b in (max(a, b), a >= b && a > 0) } }

/// Waveform bars: Chief's in `on`, yours in `off`.
struct Bars: View {
    @ObservedObject var m: Meter
    var n = 44, w: CGFloat = 2, gap: CGFloat = 2, h: CGFloat = 24, on: Color, off: Color
    @Environment(\.accessibilityReduceMotion) private var still
    var body: some View {
        HStack(alignment: .center, spacing: gap) {
            ForEach(Array(levels(m, n).enumerated()), id: \.offset) { _, x in
                Capsule().fill(x.1 ? on : off).frame(width: w, height: 2 + CGFloat(x.0) * (h - 2))
            }
        }.frame(height: h).animation(still ? nil : .linear(duration: 0.06), value: m.you).accessibilityHidden(true)
    }
}

struct HLine: Shape { func path(in r: CGRect) -> Path { Path { $0.move(to: CGPoint(x: 0, y: r.midY)); $0.addLine(to: CGPoint(x: r.maxX, y: r.midY)) } } }

/// Who has the floor, as a glyph: the meter's wave while either of you speaks, the working ring while Chief thinks.
func talkGlyph(_ t: Talk) -> String { t == .thinking ? "working" : t == .muted ? "mic-off" : t == .listening ? "mic" : "waveform" }

// MARK: Default: the system tokens. A header (name, who has the floor), the stage card, the captions, and a bar with the wave,
// the context used and Start over.

struct DefaultLook: View {
    let s: Live, l: Look
    var body: some View {
        let muted = hx(l.dim)
        VStack(spacing: DS.Space.s12) {
            HStack(spacing: DS.Space.s8) {
                Text(s.name).font(ds(DS.TypeRole.strong)).foregroundColor(hx(l.text))
                Label { Text(voiceTalkLabel(s.talk, micMuted: s.v.micMuted)) } icon: { Image(ds: talkGlyph(s.talk), size: iconSize(DS.TypeRole.label.size)) }
                    .font(ds(DS.TypeRole.label)).foregroundColor(s.talk == .thinking ? Color(DS.Palette.statusWorkingFg, dark: l.dark) : muted)
                Spacer(minLength: DS.Space.s8)
                Text(s.hint).font(ds(DS.TypeRole.caption)).foregroundColor(muted).lineLimit(1)
            }
            s.stage(l, r: DS.Radius.md, pad: DS.Space.s8, border: hx(l.line))
            if !s.said.isEmpty || !s.heard.isEmpty {
                VStack(alignment: .leading, spacing: DS.Space.s8) {
                    if !s.heard.isEmpty {
                        HStack { Spacer(minLength: DS.Space.s48)
                            Text(s.heard).font(ds(DS.TypeRole.bodyCopy, face: chatFont)).foregroundColor(hx(l.text)).lineLimit(4)
                                .padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s8).background(RoundedRectangle(cornerRadius: DS.Radius.md).fill(hx(l.card))) }
                    }
                    if !s.said.isEmpty { Text(s.said).font(ds(DS.TypeRole.bodyCopy, face: chatFont)).foregroundColor(hx(l.text)).lineLimit(4).frame(maxWidth: .infinity, alignment: .leading) }
                }
            }
            s.activity(l)
            HStack(spacing: DS.Space.s12) {
                Bars(m: s.v.meter, n: 40, on: hx(l.accent), off: Color(DS.Palette.borderDefault, dark: l.dark))
                Spacer(minLength: DS.Space.s4)
                if s.c.context > 0 { s.ctx {
                    Label { Text(s.known ? "\(s.pct)% context" : "\(s.tokens) tokens") } icon: { if s.high { Image(ds: "needs", size: iconSize(DS.TypeRole.label.size)) } }
                        .font(ds(DS.TypeRole.label).monospacedDigit()).foregroundColor(s.high ? Color(DS.Palette.statusNeedsFg, dark: l.dark) : muted)
                } }  // nothing to say until the conversation has used some
                s.clearButton {
                    Label { Text("Start over") } icon: { Image(ds: "compose", size: iconSize(DS.TypeRole.label.size)) }.font(ds(DS.TypeRole.label)).foregroundColor(hx(l.text))
                        .padding(.horizontal, DS.Space.s8).frame(height: DS.Layout.controlHeightMd).background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(hx(l.card)))
                }
            }
        }.padding(DS.Space.s16)
    }
}

// MARK: Phosphor: the terminal's spirit, mono and green on near-black. Shell prompts and brackets, dashed rules, no glow.

struct PhosphorLook: View {
    let s: Live, l: Look
    var body: some View {
        let ink = hx(l.text), faint = hx(l.dim), f = Int((s.frac * 10).rounded())
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: DS.Space.s8) {
                Text("\(s.name.lowercased())@greenlight"); Text("~/voice").foregroundColor(faint)
                Spacer(); Text("[\((s.v.micMuted ? "Muted" : s.label).lowercased())]").foregroundColor(faint)  // short: the line is full; "wait" below says thinking
            }.padding(.horizontal, DS.Space.s16).padding(.vertical, DS.Space.s12)
            HLine().stroke(faint, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1)
            VStack(alignment: .leading, spacing: DS.Space.s8) {
                Text("$ stage --live").foregroundColor(faint)
                s.stage(l, r: 0, pad: 0)
                if !s.heard.isEmpty { Text("\(Text("you> ").foregroundColor(faint))\(Text(s.heard).foregroundColor(hx(l.you)))") }
                if !s.said.isEmpty { Text("\(Text("\(s.name.lowercased())> ").foregroundColor(faint))\(s.said)\(s.talk == .speaking ? " █" : "")") }
                s.activity(l, accent: ink, size: DS.TypeRole.code.size)
            }.lineLimit(4).padding(DS.Space.s16)
            HStack(spacing: DS.Space.s12) { Text(s.talk == .speaking ? s.name.lowercased() : s.talk == .listening ? "you" : s.talk == .muted ? "muted" : "wait").foregroundColor(faint); Glyphs(m: s.v.meter, talk: s.talk) }
                .padding(.horizontal, DS.Space.s16).padding(.bottom, DS.Space.s12)
            HLine().stroke(faint, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1)
            HStack(spacing: DS.Space.s12) {
                s.ctx { Text("ctx [\(String(repeating: "#", count: f) + String(repeating: ".", count: 10 - f))] \(s.c.ctxShort)\(s.high ? " !" : "")")
                    .foregroundColor(s.high ? Color(DS.Palette.statusNeedsFg, dark: true) : ink) }
                Text(s.tokens).foregroundColor(faint).lineLimit(1)
                Spacer()
                s.clearButton { Text("[ clear ]").fontWeight(.semibold).foregroundColor(hx(l.page)).padding(.horizontal, DS.Space.s8).frame(height: DS.Layout.controlHeightMd).background(ink) }
            }.padding(.horizontal, DS.Space.s16).padding(.vertical, DS.Space.s12)
        }.font(ds(DS.TypeRole.code, mono: true)).foregroundColor(ink)
    }
}
struct Glyphs: View {
    @ObservedObject var m: Meter
    let talk: Talk
    var body: some View {
        let g = Array("▁▂▃▄▅▆▇█")
        Text(talk == .thinking ? "·   ·   ·   ·   ·   ·" : String(levels(m, 36).map { g[min(7, Int($0.0 * 7 + 0.5))] })).lineLimit(1).accessibilityHidden(true)
    }
}

struct StageWeb: NSViewRepresentable {
    let web: WKWebView
    func makeNSView(context: Context) -> WKWebView { web }
    func updateNSView(_ v: WKWebView, context: Context) {}
}

/// The muted mic knob's fill: status.needs (you have to unmute to be heard), never the accent the voice button lights in.
func mutedFill(_ l: Look) -> String { dsHex(DS.Palette.statusNeedsSolid, dark: l.dark) }

/// The voice button's help and accessibility label. While a start is under way (its retries take up to ~3 s) a press cancels it, so it says so.
func voiceButtonText(on: Bool, starting: Bool) -> (help: String, label: String) {
    on ? ("End voice mode (or say “goodbye”)", "End voice mode")
        : starting ? ("Starting voice mode… click to cancel", "Cancel starting voice mode")
        : ("Start voice mode: talk with it hands-free", "Start voice mode")
}

/// The chat header's knobs, top right, in the state they're in: what a click does now, for the hover hint (`help`) and VoiceOver (`label`).
struct HeaderTips {
    var on = false, starting = false, transcript = false, captions = false, out = false, micMuted = false
    var name = "Chief", look = "Default", next = "Phosphor"
    var attach: (help: String, label: String) { ("Attach \(ChatFile.supported) to what you say next", "Attach files") }
    var view: (help: String, label: String) {
        transcript ? ("Show the stage: what \(name) draws as it talks", "Show the stage") : ("Show the transcript: the conversation as text", "Show the transcript")
    }
    var caption: (help: String, label: String) {
        captions ? ("Hide captions: the words said and heard", "Turn captions off") : ("Show captions: the words said and heard, as text", "Turn captions on")
    }
    var mic: (help: String, label: String) {
        micMuted ? ("Unmute the mic: \(name) hears you again (⇧⌘M)", "Unmute the microphone")
            : ("Mute the mic: nothing you say is heard or sent until you unmute (⇧⌘M)", "Mute the microphone")
    }
    var style: (help: String, label: String) { ("Style: \(look). Click for \(next)", "Chat style: \(look). Switch to \(next)") }
    var voice: (help: String, label: String) { voiceButtonText(on: on, starting: starting) }
    var pop: (help: String, label: String) {
        out ? ("Put the chat back in the main window's right pane", "Put back in the main window")
            : ("Pop the chat out into its own window (close it to bring it back)", "Move to its own window")
    }
}

/// The chat header's voice controls: voice mode on/off, and while on, the transcript and captions. Each hints on hover (Tip):
/// at once, under the pane, even while Greenlight isn't the front app (the native tooltip waits for that).
struct VoiceButtons: View, Looked {
    @ObservedObject var v: Voice
    @ObservedObject var c: Core
    var out: Bool { c.chatOut }  // the chat is in its own window
    var nextLook: Look { looks[((looks.firstIndex { $0.id == v.look.id } ?? 0) + 1) % looks.count] }
    var tips: HeaderTips {
        HeaderTips(on: v.on, starting: v.starting, transcript: v.transcript, captions: v.captions, out: out, micMuted: v.micMuted, name: c.name, look: v.look.name, next: nextLook.name)
    }
    var body: some View {
        let t = tips
        HStack(spacing: DS.Space.s4) {
            if v.on {
                if !v.transcript {  // the stage has no composer: its +. Drops on the stage and ⌘V attach too
                    knob("attach", t.attach) { c.pickFiles(NSApp.keyWindow) }
                }
                knob(v.transcript ? "waveform" : "chat-text", t.view) { v.transcript.toggle() }
                knob(v.captions ? "captions-fill" : "captions", t.caption) { v.captions.toggle() }
                Button(action: v.toggleMic) {  // muted: filled in a warning color the look's accent (the voice button's) isn't
                    shell(Image(ds: v.micMuted ? "mic-off" : "mic"), on: v.micMuted, fill: v.micMuted ? hx(mutedFill(lk)) : nil)
                }.buttonStyle(.plain).modifier(Tip(c: c, text: t.mic.help)).accessibilityLabel(t.mic.label)
            }
            // one style for live and standard chat
            knob("palette", t.style) { v.look = nextLook }
            Button(action: v.toggle) { shell(Image(ds: "waveform", size: iconSize(DS.TypeRole.body.size), medium: true), on: v.on || v.starting).opacity(v.starting ? 0.6 : 1) }
                .buttonStyle(.plain).modifier(Tip(c: c, text: t.voice.help)).accessibilityLabel(t.voice.label)
            knob(out ? "panel" : "external", t.pop) { c.popChat(!out) }  // its own window, or back
        }
    }
    func knob(_ icon: String, _ t: (help: String, label: String), _ act: @escaping () -> Void) -> some View {
        Button(action: act) { shell(Image(ds: icon)) }.buttonStyle(.plain).modifier(Tip(c: c, text: t.help)).accessibilityLabel(t.label)
    }
    /// The knob: a quiet icon button (§10.2), 28 square; lit, the accent's solid (or status.needs for the muted mic). Phosphor: square.
    func shell<V: View>(_ icon: V, on: Bool = false, fill: Color? = nil) -> some View {
        let s = DS.Layout.controlHeightMd, r: CGFloat = mono ? 0 : DS.Radius.sm
        let lit = fill == nil ? onAccent : Color(DS.Palette.statusNeedsOnSolid, dark: lk.dark)
        return icon.font(ds(DS.TypeRole.body)).frame(width: s, height: s).foregroundColor(on ? lit : mono ? fg : dim)
            .background(RoundedRectangle(cornerRadius: r).fill(on ? fill ?? accent : .clear))
            .overlay(RoundedRectangle(cornerRadius: r).stroke(mono && !on ? line : .clear)).contentShape(Rectangle())
    }
}
