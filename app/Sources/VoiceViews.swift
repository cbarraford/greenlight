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
    guard s.hasPrefix("#"), s.count == 7, Int(s.dropFirst(), radix: 16) != nil else { return Theme.queued }
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
struct Staged: Equatable { var text: String; var stage: String?; var end = false; var hexes: [String]? = nil; var reset = false; var console: String? = nil }
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
    let reset = text.range(of: #"\[chat: ?reset\]"#, options: [.regularExpression, .caseInsensitive]).map { text.removeSubrange($0); return true } ?? false  // asked to start over
    if let r = text.range(of: #"\[stage: ?(full|normal)\]"#, options: [.regularExpression, .caseInsensitive]) { text.removeSubrange(r) }  // the old size line: no full size any more
    if let open = text.range(of: "```stage") {
        if let close = text[open.upperBound...].range(of: "```") {
            stage = String(text[open.upperBound..<close.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
            text.removeSubrange(open.lowerBound..<close.upperBound)
        } else { text = String(text[..<open.lowerBound]) }
    }
    return Staged(text: text.trimmingCharacters(in: .whitespacesAndNewlines), stage: stage, end: end, hexes: hexes, reset: reset, console: console?.isEmpty == false ? console : nil)
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

/// The stage's drawing when Haiku can't (no key, an error, a refusal): the first sentence as the title, the rest as a list.
func fallbackSpec(_ text: String) -> [String: Any] {
    let ss = sentences(plain(text) + "\n", from: 0).said
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
    let item: [String: Any] = ["type": "object", "properties": ["label": ["type": "string"], "detail": ["type": "string"]],
                               "required": ["label", "detail"], "additionalProperties": false]
    let schema: [String: Any] = ["type": "object", "additionalProperties": false, "required": ["eyebrow", "title", "kind", "stat", "items"], "properties": [
        "title": ["type": "string"], "kind": ["type": "string", "enum": ["list", "steps", "stat", "flow", "compare", "quote"]],
        "eyebrow": ["type": "string"], "stat": ["type": "string"], "items": ["type": "array", "items": item]]]
    haiku("You draw a voice assistant's reply as one glanceable card on the user's screen while it is spoken. "
            + "title: the point in a few words. kind: list (facts), steps (an order), stat (one number matters: put it in stat, e.g. \"3 MRs\"), "
            + "flow (a pipeline or lifecycle, short labels), compare (side by side options), quote (one line to remember). "
            + "eyebrow: 1–4 lowercase words of context above the title (a workspace name, \"needs you · 2\", \"this week\") or empty; no emoji. "
            + "items: at most 6, label a few words, detail one short line or empty (for stat, items are facts: label the name, detail the value). "
            + "Use names, numbers and MR refs from the reply; never invent any. stat is empty unless kind is stat.",
          "The user said: \(heard)\n\nThe assistant's reply:\n\(reply)", schema, done: done)
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

let voiceNote = "(Voice mode: the user is talking to you out loud, and your reply is spoken aloud. Answer concisely, like a person talking: "
    + "one or two short sentences, the answer first, no preamble or recap, more only if they ask; no markdown, links or lists in the spoken part. Their screen shows a stage beside your voice; a fast model draws your reply there by itself. "
    + "To put detail there yourself (a list, steps, numbers, MR links) or when they ask you to draw or redraw something, add one ```stage fenced block: "
    + "for a chart or graph, JSON the stage draws instantly: {\"chart\": \"bar\"|\"line\"|\"pie\", \"title\": …, \"sub\": …, \"labels\": [x labels], "
    + "\"series\": [{\"name\": …, \"values\": [numbers], \"color\": green|red|gray|blue|amber|purple|teal|pink}], \"stacked\": true, \"note\": …} "
    + "(pie: one series, a slice per label; never hand-draw a chart in SVG); otherwise plain notes (drawn for you) or a self-contained HTML fragment with inline CSS or SVG and no scripts (shown as is; theme colors: "
    + "var(--text), var(--dim), var(--accent), var(--card), var(--line)). When the conversation is over (they say goodbye, thanks that's it, or ask you to stop listening "
    + "or turn voice mode off), say a short goodbye and add a line [voice: off]: voice mode ends after you finish speaking.)\n"

/// The waveforms' levels, apart from Voice so 40 updates a second redraw only the bars.
final class Meter: ObservableObject {
    @Published var you = [Float](repeating: 0, count: 48)
    @Published var chief = [Float](repeating: 0, count: 48)
}

/// Voice mode's looks, switched by the header's style button: each frames the same parts (the stage, captions, who's talking,
/// context and Clear) its own way, and the stage takes its colors and type.
struct Look { let id, name, page, stage, card, text, text2, dim, line, accent, font: String; let dark: Bool }
let sansCSS = "-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif", monoCSS = "ui-monospace,'SF Mono',Menlo,monospace", serifCSS = "ui-serif,'New York',Georgia,serif"
let looks = [
    Look(id: "conversation", name: "Conversation", page: "FFFFFF", stage: "FFFFFF", card: "F6F8FA", text: "1F2328", text2: "3D444D", dim: "59636E", line: "D1D9E0", accent: "0969DA", font: sansCSS, dark: false),
    Look(id: "orb", name: "Orb", page: "111113", stage: "1C1C1F", card: "26262A", text: "ECECEE", text2: "C9C9CF", dim: "9A9AA1", line: "34343A", accent: "F5A524", font: sansCSS, dark: true),
    Look(id: "onair", name: "On Air", page: "0B0B0C", stage: "0B0B0C", card: "1A1A1D", text: "F4F4F5", text2: "C9C9CF", dim: "A1A1AA", line: "26262A", accent: "F5A524", font: sansCSS, dark: true),
    Look(id: "phosphor", name: "Phosphor", page: "000000", stage: "000000", card: "061A04", text: "28FE14", text2: "28FE14", dim: "1DA80F", line: "1DA80F", accent: "28FE14", font: monoCSS, dark: true),
    Look(id: "editorial", name: "Editorial", page: "F7F5F0", stage: "F7F5F0", card: "EFEBE3", text: "1F2328", text2: "3D444D", dim: "59636E", line: "D5D1C7", accent: "B42318", font: serifCSS, dark: false),
    Look(id: "hud", name: "HUD", page: "0A0F14", stage: "0D151C", card: "14212B", text: "D7E3EA", text2: "B4C4CE", dim: "7F95A3", line: "1B2C36", accent: "39C5CF", font: sansCSS, dark: true),
    Look(id: "zen", name: "Zen", page: "121214", stage: "121214", card: "1C1C1F", text: "ECECEE", text2: "C9C9CF", dim: "8E8E96", line: "26262A", accent: "F5A524", font: sansCSS, dark: true),
]
extension Look {
    /// The stage page's CSS variables (stage.html reads --mc-*), set on document.documentElement.style as `s`.
    var vars: String {
        [("bg", "#" + stage), ("surface", "#" + card), ("text", "#" + text), ("text2", "#" + text2), ("dim", "#" + dim), ("line", "#" + line), ("accent", "#" + accent), ("font", font)]
            .map { "s.setProperty('--mc-\($0.0)', \"\($0.1)\");" }.joined(separator: " ") + " document.documentElement.dataset.look = '\(id)';"
    }
}
func hx(_ h: String, _ o: Double = 1) -> Color { Color(nsColor: hex(h)).opacity(o) }
/// The chat window (standard and live) wears the same look. Chat views adopt Looked: its members shadow the app-wide color
/// globals (bg, fg, accent, card…, theme) and chatFace, so their bodies read the look's tokens unchanged.
var chatLook = looks.first { $0.id == ud.string(forKey: "stageStyle") } ?? looks[1]
protocol Looked {}
extension Looked {
    var lk: Look { chatLook }
    var theme: Theme { Theme(lk.name, lk.stage, lk.text, lk.accent, lk.card) }
    var bg: Color { hx(lk.stage) }
    var page: Color { hx(lk.page) }
    var fg: Color { hx(lk.text) }
    var accent: Color { hx(lk.accent) }
    var card: Color { hx(lk.card) }
    var surface: Color { hx(lk.card) }
    var line: Color { hx(lk.line) }
    var edge: Color { hx(lk.line) }
    var dim: Color { hx(lk.dim) }
    var sub: Color { hx(lk.text2) }
    var bubble: Color { hx(lk.card) }
    var mono: Bool { lk.id == "phosphor" }
    /// State colors for the look's lightness (Theme.needs… follow the app theme, not the look).
    func state(_ d: String, _ l: String) -> Color { hx(lk.dark ? d : l) }
    var needs: Color { state("FF9F0A", "F08A00") }
    var needsInk: Color { state("FF9F0A", "A85A00") }
    var done: Color { state("30D158", "2BA84A") }
    var crashed: Color { state("FF453A", "E5392F") }
    var crashedInk: Color { state("FF6B61", "C62828") }
    var working: Color { state("0A84FF", "0A7AFF") }
    /// The conversation's text in the look's face: mono for Phosphor, serif for Editorial, else the chosen chat font.
    func chatFace(size: CGFloat, weight: Font.Weight = .regular) -> Font {
        lk.font == monoCSS ? sys(size: size, weight: weight, design: .monospaced) : lk.font == serifCSS ? sys(size: size, weight: weight, design: .serif)
            : sys(size: size, weight: weight, face: chatFont)
    }
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

enum Talk { case speaking, listening, thinking }
/// What every look shows, from Voice and Core.
struct Live {
    let v: Voice, c: Core
    var talk: Talk { v.speaking > 0 ? .speaking : c.run != nil ? .thinking : .listening }
    var label: String { talk == .speaking ? "Speaking" : talk == .thinking ? "Thinking" : "Listening" }
    var hint: String { talk == .speaking ? (!v.fallback.isEmpty && !v.isLive ? "Apple's voice: \(v.fallback)" : v.isLive || v.vp && v.bargeIn ? "talk to interrupt" : "speaking") : talk == .thinking ? "say “stop” to cancel" : "“that’s all” ends voice mode" }
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
    @ViewBuilder func activity(_ l: Look, ink: Color? = nil, accent: Color? = nil, size: CGFloat = 12) -> some View {
        if c.verbose {
            VStack(alignment: .leading, spacing: 6) {
                if c.liveLine, let r = c.run { Activity(c: c, r: r, fg: ink ?? hx(l.text), dim: hx(l.dim), accent: accent ?? hx(l.accent), size: size) }
                FeedTail(f: c.feed, n: 3, fg: ink ?? hx(l.text), dim: hx(l.dim), accent: accent ?? hx(l.accent), size: size - 1)
            }.frame(maxWidth: .infinity, alignment: .leading)
        } else if c.liveLine, let r = c.run { Activity(c: c, r: r, fg: ink ?? hx(l.text), dim: hx(l.dim), accent: accent ?? hx(l.accent), size: size).frame(maxWidth: .infinity, alignment: .leading) }
    }
    func clear() { v.run(.reset) }
    /// The stage, inset in its card so the card's rounded corners show (the web view itself is square).
    func stage(_ l: Look, r: CGFloat = 12, pad: CGFloat = 8, border: Color? = nil) -> some View {
        StageWeb(web: v.stageWeb).padding(pad).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(RoundedRectangle(cornerRadius: r).fill(hx(l.stage)))
            .overlay(RoundedRectangle(cornerRadius: r).stroke(border ?? .clear))
    }
    /// The context readout as a button: its popover has the totals and Compact.
    func ctx<L: View>(@ViewBuilder _ label: () -> L) -> some View {
        Button { c.ctxOpen.toggle(); c.hint = c.ctxOpen ? "" : ctxTip(c) } label: { label() }.buttonStyle(.plain).modifier(Tip(c: c, text: ctxTip(c))).accessibilityLabel("Context, \(c.ctxSpoken)").modifier(CtxPop(c: c))
    }
    func clearButton<L: View>(@ViewBuilder _ label: () -> L) -> some View {
        Button(action: clear) { label() }.buttonStyle(.plain).modifier(Tip(c: c, text: "Clear the context and start a new conversation (or say “reset”)")).accessibilityLabel("Clear the context")
    }
}

/// Voice mode's screen, in place of the chat, in the look the header's style button last picked.
struct StageView: View {
    @ObservedObject var v: Voice
    @ObservedObject var c: Core
    var body: some View {
        let s = Live(v: v, c: c), l = v.look
        Group {
            switch l.id {
            case "conversation": ConversationLook(s: s, l: l)
            case "onair": OnAirLook(s: s, l: l)
            case "phosphor": PhosphorLook(s: s, l: l)
            case "editorial": EditorialLook(s: s, l: l)
            case "hud": HUDLook(s: s, l: l)
            case "zen": ZenLook(s: s, l: l)
            default: OrbLook(s: s, l: l)
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) { DwimToastBar(c: c, l: l).padding(.horizontal, 12).padding(.bottom, 8); StageFiles(c: c, l: l) }
                .animation(.easeOut(duration: 0.15), value: c.toast)
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
            VStack(alignment: .leading, spacing: 6) {
                if !c.files.isEmpty || c.reading > 0 {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(c.files) { chip($0) }
                            if c.reading > 0 { HStack(spacing: 5) { ProgressView().controlSize(.mini); Text("Reading…") }.font(.system(size: 11)).foregroundColor(hx(l.dim)) }
                        }
                    }
                    Text(c.reading > 0 && c.files.isEmpty ? "Checking the file…" : "\(c.files.count == 1 ? "Goes" : "These go") to \(c.name) with what you say next")
                        .font(.system(size: 11)).foregroundColor(hx(l.dim)).lineLimit(1)
                }
                if !c.fileNote.isEmpty {
                    Label(c.fileNote, systemImage: "exclamationmark.triangle.fill").font(.system(size: 11, weight: .medium)).foregroundColor(hx(l.dark ? "FF9F0A" : "9A6700"))
                        .fixedSize(horizontal: false, vertical: true).accessibilityLabel("Not attached: \(c.fileNote)")
                }
            }.padding(.horizontal, 16).padding(.vertical, 8).frame(maxWidth: .infinity, alignment: .leading)
                .background(hx(l.card)).overlay(alignment: .top) { Rectangle().fill(hx(l.line)).frame(height: 1) }
        }
    }
    func chip(_ f: ChatFile) -> some View {
        HStack(spacing: 5) {
            if let t = f.thumb { Image(nsImage: t).resizable().aspectRatio(contentMode: .fill).frame(width: 18, height: 18).clipShape(RoundedRectangle(cornerRadius: 3)) }
            else { Image(systemName: f.kind == "pdf" ? "doc.richtext" : "doc.text").font(.system(size: 11)).foregroundColor(hx(l.dim)) }
            Text(f.name).font(.system(size: 11, weight: .semibold)).foregroundColor(hx(l.text)).lineLimit(1).truncationMode(.middle).frame(maxWidth: 150, alignment: .leading)
            Text(byteSize(f.data.count)).font(.system(size: 11)).foregroundColor(hx(l.dim)).lineLimit(1).fixedSize()
            Button { c.files.removeAll { $0.id == f.id } } label: {
                Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundColor(hx(l.dim)).frame(width: 16, height: 16)
            }.buttonStyle(.plain).accessibilityLabel("Remove \(f.name)")
        }.padding(.leading, 4).padding(.trailing, 3).frame(height: 24)
            .background(RoundedRectangle(cornerRadius: 6).fill(hx(l.stage))).overlay(RoundedRectangle(cornerRadius: 6).stroke(hx(l.line)))
            .help("\(f.kindName), goes to \(c.name) with what you say next")
            .accessibilityElement(children: .contain).accessibilityLabel("Attached \(f.name), \(byteSize(f.data.count))")
    }
}
/// Files dragged over the stage: where they'll go, in the stage's look.
struct StageDropHint: View {
    let l: Look
    var body: some View {
        RoundedRectangle(cornerRadius: 12).fill(hx(l.page).opacity(0.85))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(hx(l.accent), style: StrokeStyle(lineWidth: 2, dash: [6, 4])))
            .overlay(VStack(spacing: 8) {
                Image(systemName: "paperclip").font(.system(size: 22)).foregroundColor(hx(l.accent))
                Text("Drop to attach").font(.system(size: 15, weight: .semibold)).foregroundColor(hx(l.text))
                Text("Goes with what you say next: \(ChatFile.supported)").font(.system(size: 11)).foregroundColor(hx(l.dim)).multilineTextAlignment(.center)
            }.padding(.horizontal, 20))
            .padding(10).allowsHitTesting(false).accessibilityHidden(true)
    }
}

/// The last n levels, Chief's where it's louder (it's speaking), yours otherwise.
func levels(_ m: Meter, _ n: Int) -> [(Float, Bool)] { zip(m.chief, m.you).suffix(n).map { a, b in (max(a, b), a >= b && a > 0) } }

/// Waveform bars: Chief's in `on`, yours in `off`; flipped for a reflection.
struct Bars: View {
    @ObservedObject var m: Meter
    var n = 44, w: CGFloat = 3, gap: CGFloat = 2, h: CGFloat = 28, round = true, on: Color, off: Color
    var body: some View {
        HStack(alignment: .bottom, spacing: gap) {
            ForEach(Array(levels(m, n).enumerated()), id: \.offset) { _, x in
                RoundedRectangle(cornerRadius: round ? w / 2 : 0).fill(x.1 ? on : off).frame(width: w, height: 2 + CGFloat(x.0) * (h - 2))
            }
        }.frame(height: h, alignment: .bottom).animation(.linear(duration: 0.06), value: m.you).accessibilityHidden(true)
    }
}

struct HLine: Shape { func path(in r: CGRect) -> Path { Path { $0.move(to: CGPoint(x: 0, y: r.midY)); $0.addLine(to: CGPoint(x: r.maxX, y: r.midY)) } } }

// MARK: Conversation: GitHub Light, the turn as chat bubbles, a pill with the wave, context and Clear

struct ConversationLook: View {
    let s: Live, l: Look
    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                Text(String(s.name.prefix(1))).font(.system(size: 14, weight: .bold)).foregroundColor(.white).frame(width: 32, height: 32).background(Circle().fill(hx(l.accent)))
                VStack(alignment: .leading, spacing: 1) {
                    Text(s.name).font(.system(size: 14, weight: .semibold)).foregroundColor(hx(l.text))
                    Text("\(s.label) · \(s.hint)").font(.system(size: 12)).foregroundColor(hx(l.dim)).lineLimit(1)
                }
                Spacer()
            }
            s.stage(l, border: hx(l.line))
            VStack(spacing: 8) {
                if !s.heard.isEmpty { HStack { Spacer(minLength: 40); bubble(s.heard, fill: hx(l.accent), ink: .white) } }
                if !s.said.isEmpty { HStack { bubble(s.said, fill: hx(l.card), ink: hx(l.text)); Spacer(minLength: 40) } }
                s.activity(l).padding(.horizontal, 4)
            }
            HStack(spacing: 10) {
                Bars(m: s.v.meter, n: 36, on: hx(l.accent), off: hx("8C959F"))
                Spacer(minLength: 4)
                s.ctx { Text("\(s.c.ctxShort) context").font(.system(size: 12, weight: .medium).monospacedDigit()).foregroundColor(s.high ? hx("9A6700") : hx(l.dim)) }
                s.clearButton {
                    Image(systemName: "square.and.pencil").font(.system(size: 13, weight: .semibold)).foregroundColor(.white).frame(width: 34, height: 34).background(Circle().fill(hx(l.text)))
                }
            }.padding(.leading, 16).padding(.trailing, 5).padding(.vertical, 5)
                .background(Capsule().fill(hx(l.card))).overlay(Capsule().stroke(hx(l.line)))
            GeometryReader { g in
                ZStack(alignment: .leading) { Capsule().fill(hx(l.line)); Capsule().fill(s.high ? hx("BF8700") : hx(l.accent)).frame(width: g.size.width * s.frac) }
            }.frame(height: 3).accessibilityHidden(true)
        }.padding(16)
    }
    func bubble(_ t: String, fill: Color, ink: Color) -> some View {
        Text(t).font(.system(size: 14)).foregroundColor(ink).lineLimit(4).padding(.horizontal, 14).padding(.vertical, 9)
            .background(RoundedRectangle(cornerRadius: 16).fill(fill))
    }
}

// MARK: Orb: rings that swell with the voice, the outer one the context meter

struct OrbLook: View {
    let s: Live, l: Look
    var body: some View {
        VStack(spacing: 18) {
            Text("\(s.name) · \(s.label)".uppercased()).font(.system(size: 12, weight: .semibold)).tracking(1.6).foregroundColor(hx(l.dim))
            s.stage(l, r: 20, pad: 10, border: hx("FFFFFF", 0.08))
            Orb(m: s.v.meter, s: s, l: l).frame(width: 200, height: 200)
            if !s.said.isEmpty || !s.heard.isEmpty {
                VStack(spacing: 8) {
                    if !s.said.isEmpty { Text(s.said).font(.system(size: 20, weight: .medium)).foregroundColor(hx(l.text)) }
                    if !s.heard.isEmpty { Text("“\(s.heard)”").font(.system(size: 13)).foregroundColor(hx(l.dim)) }
                }.multilineTextAlignment(.center).lineLimit(3).padding(.horizontal, 20)
            }
            s.activity(l).padding(.horizontal, 20)
            HStack(spacing: 12) {
                s.ctx { Text(s.known ? "\(s.pct)% · \(s.tokens)" : "\(s.tokens) tokens").font(.system(size: 12).monospacedDigit()).foregroundColor(s.high ? hx("FF9F0A") : hx(l.dim)) }
                s.clearButton {
                    Label("Start over", systemImage: "arrow.counterclockwise").font(.system(size: 13, weight: .semibold)).foregroundColor(hx(l.text))
                        .padding(.horizontal, 16).frame(height: 36).background(Capsule().fill(hx(l.stage))).overlay(Capsule().stroke(hx("FFFFFF", 0.14)))
                }
            }
        }.padding(.horizontal, 20).padding(.vertical, 16)
    }
}
struct Orb: View {
    @ObservedObject var m: Meter
    let s: Live, l: Look
    var body: some View {
        let lv = CGFloat(levels(m, 1).first?.0 ?? 0), k = (s.talk == .speaking ? 1 : s.talk == .listening ? 0.8 : 0.62) * (1 + lv * 0.18)
        let core = s.talk == .speaking ? hx(l.accent) : s.talk == .listening ? hx(l.text) : hx("3A3A40")
        ZStack {
            Circle().stroke(hx("FFFFFF", 0.07), lineWidth: 3)
            Circle().trim(from: 0, to: max(s.frac, 0.005)).stroke(s.high ? hx("FF9F0A") : hx(l.accent), style: StrokeStyle(lineWidth: 3, lineCap: .round)).rotationEffect(.degrees(-90))
            Circle().stroke(hx(l.accent, 0.18)).frame(width: 174 * k, height: 174 * k)
            Circle().stroke(hx(l.accent, 0.32)).frame(width: 144 * k, height: 144 * k)
            Circle().fill(hx(l.accent, 0.16)).frame(width: 113 * k, height: 113 * k)
            Circle().fill(core).frame(width: 79 * k, height: 79 * k).shadow(color: s.talk == .thinking ? .clear : core.opacity(0.5), radius: 30)
        }.animation(.easeOut(duration: 0.12), value: lv).accessibilityElement().accessibilityLabel("\(s.label), context \(s.c.ctxSpoken)")
    }
}

// MARK: On Air: a broadcast, tally light and LED meter up top, an amber lower third, a ticker with Clear

struct OnAirLook: View {
    let s: Live, l: Look
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                HStack(spacing: 7) { Circle().fill(.white).frame(width: 8, height: 8); Text(s.talk == .speaking ? "ON AIR" : s.talk == .listening ? "LISTENING" : "STANDBY") }
                    .font(.system(size: 12, weight: .heavy)).tracking(1.4).foregroundColor(.white).padding(.horizontal, 10).padding(.vertical, 6)
                    .background(s.talk == .speaking ? hx("D1242F") : s.talk == .listening ? hx("1A7F37") : hx("3A3A40"))
                Leds(m: s.v.meter)
                Spacer()
            }.padding(.horizontal, 16).padding(.vertical, 12)
            Rectangle().fill(hx(l.line)).frame(height: 2)
            VStack(alignment: .leading, spacing: 12) {
                Text("LIVE · \(s.name.uppercased())").font(.system(size: 11, weight: .heavy)).tracking(1.5).foregroundColor(hx(l.page)).padding(.horizontal, 8).padding(.vertical, 4).background(hx(l.accent))
                s.stage(l, r: 0, pad: 0)
            }.padding(16)
            VStack(spacing: 0) {
                if !s.said.isEmpty { third("CHIEF", s.said, tag: hx(l.accent), tagInk: hx(l.page), fill: hx(l.text), ink: hx(l.page), size: 15) }
                if !s.heard.isEmpty { third("YOU", s.heard, tag: hx(l.line), tagInk: hx(l.text), fill: hx(l.card), ink: hx(l.text2), size: 13) }
                s.activity(l).padding(.horizontal, 16).padding(.top, 8)
            }.padding(.bottom, 12)
            HStack(spacing: 12) {
                s.ctx {
                    HStack(spacing: 10) {
                        Text("CONTEXT \(s.c.ctxShort)").font(.system(size: 11, weight: .heavy).monospacedDigit()).tracking(1.2)
                        HStack(spacing: 2) { ForEach(0..<10, id: \.self) { i in Rectangle().fill(i < Int((s.frac * 10).rounded()) ? hx(l.page) : hx(l.page, 0.22)).frame(width: 8, height: 16) } }
                        Text("· \(s.tokens.uppercased()) · SAY “RESET”").font(.system(size: 11, weight: .bold)).lineLimit(1)
                    }.foregroundColor(hx(l.page))
                }
                Spacer(minLength: 0)
                s.clearButton { Text("CLEAR").font(.system(size: 12, weight: .heavy)).tracking(1.6).foregroundColor(hx(l.accent)).padding(.horizontal, 18).frame(maxHeight: .infinity).background(hx(l.page)) }
            }.padding(.leading, 16).frame(height: 48).background(s.high ? hx("FF9F0A") : hx(l.accent))
        }
    }
    func third(_ who: String, _ t: String, tag: Color, tagInk: Color, fill: Color, ink: Color, size: CGFloat) -> some View {
        HStack(spacing: 0) {
            Text(who).font(.system(size: 12, weight: .heavy)).tracking(1).foregroundColor(tagInk).frame(width: 62).frame(maxHeight: .infinity).background(tag)
            Text(t).font(.system(size: size, weight: who == "CHIEF" ? .semibold : .regular)).foregroundColor(ink).lineLimit(3)
                .padding(.horizontal, 14).padding(.vertical, 9).frame(maxWidth: .infinity, alignment: .leading).background(fill)
        }.fixedSize(horizontal: false, vertical: true)
    }
}
struct Leds: View {
    @ObservedObject var m: Meter
    var body: some View {
        let lit = Int(CGFloat(levels(m, 1).first?.0 ?? 0) * 16 + 0.5)
        HStack(spacing: 2) {
            ForEach(0..<16, id: \.self) { i in Rectangle().fill(i >= lit ? hx("26262A") : i >= 13 ? hx("F85149") : i >= 9 ? hx("F5A524") : hx("3FB950")).frame(width: 6, height: 14) }
        }.accessibilityHidden(true)
    }
}

// MARK: Phosphor: a green-screen terminal, scanlines and all

struct PhosphorLook: View {
    let s: Live, l: Look
    var body: some View {
        let ink = hx(l.text), faint = hx(l.dim), f = Int((s.frac * 10).rounded())
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Text("\(s.name.lowercased())@greenlight"); Text("~/voice").foregroundColor(faint)
                Spacer(); Text("[\(s.label.uppercased())]").foregroundColor(faint)
            }.padding(.horizontal, 14).padding(.vertical, 11)
            HLine().stroke(faint, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1)
            VStack(alignment: .leading, spacing: 8) {
                Text("$ stage --live").foregroundColor(faint)
                s.stage(l, r: 0, pad: 0)
                if !s.heard.isEmpty { Text("\(Text("you> ").foregroundColor(faint))\(s.heard)") }
                if !s.said.isEmpty { Text("\(Text("\(s.name.lowercased())> ").foregroundColor(faint))\(s.said)\(s.talk == .speaking ? " █" : "")") }
                s.activity(l, accent: ink, size: 13)
            }.lineLimit(4).padding(14)
            HStack(spacing: 10) { Text(s.talk == .speaking ? s.name.lowercased() : s.talk == .listening ? "you" : "wait").foregroundColor(faint); Glyphs(m: s.v.meter, talk: s.talk) }
                .padding(.horizontal, 14).padding(.bottom, 10)
            HLine().stroke(faint, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1)
            HStack(spacing: 10) {
                s.ctx { Text("ctx [\(String(repeating: "#", count: f) + String(repeating: ".", count: 10 - f))] \(s.c.ctxShort)").foregroundColor(s.high ? hx("FFB000") : ink) }
                Text(s.tokens).foregroundColor(faint).lineLimit(1)
                Spacer()
                s.clearButton { Text("[ clear ]").fontWeight(.bold).foregroundColor(hx(l.page)).padding(.horizontal, 10).frame(height: 30).background(ink) }
            }.padding(.horizontal, 14).padding(.vertical, 10)
        }.font(.system(size: 13, design: .monospaced)).foregroundColor(ink).shadow(color: ink.opacity(0.45), radius: 3)
            .overlay(Canvas { g, size in
                for y in stride(from: 2.0, to: size.height, by: 3) { g.fill(Path(CGRect(x: 0, y: y, width: size.width, height: 1)), with: .color(.black.opacity(0.28))) }
            }.allowsHitTesting(false).accessibilityHidden(true))
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

// MARK: Editorial: a front page, serif type, an ink-line wave

struct EditorialLook: View {
    let s: Live, l: Look
    var body: some View {
        let ink = hx(l.text), red = hx(l.accent)
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Text("The \(s.name)").font(.system(size: 22, weight: .bold, design: .serif).italic())
                Spacer()
                Text(s.label.uppercased()).font(.system(size: 10, weight: .bold)).tracking(1.8).foregroundColor(hx(l.dim))
            }.padding(.top, 16).padding(.bottom, 10)
            VStack(spacing: 2) { Rectangle().fill(ink).frame(height: 1); Rectangle().fill(ink).frame(height: 1) }
            HStack { Text("\(Date().formatted(.dateTime.weekday(.wide))) edition"); Spacer(); Text("voice") }
                .font(.system(size: 10, weight: .semibold)).tracking(1.4).textCase(.uppercase).foregroundColor(hx(l.dim)).padding(.vertical, 6)
            Rectangle().fill(ink).frame(height: 1)
            Text("ON YOUR DESK").font(.system(size: 11, weight: .bold)).tracking(1.8).foregroundColor(red).padding(.top, 18).padding(.bottom, 6)
            s.stage(l, r: 0, pad: 0)
            if !s.said.isEmpty {
                Text("“\(s.said)”").font(.system(size: 19, design: .serif).italic()).lineSpacing(3).lineLimit(4).padding(.top, 16)
            }
            if !s.heard.isEmpty { Text("In reply to “\(s.heard)”").font(.system(size: 12)).foregroundColor(hx(l.dim)).lineLimit(2).padding(.top, 6) }
            s.activity(l).padding(.top, 8)
            Scope(m: s.v.meter, ink: s.talk == .speaking ? red : ink, rule: hx(l.line)).frame(height: 44).padding(.top, 12)
            Rectangle().fill(ink).frame(height: 1)
            HStack(spacing: 14) {
                s.ctx { (s.known ? Text("Context \(Text("\(s.pct)").bold()) percent used · \(s.tokens)") : Text("Context \(Text(s.tokens).bold()) tokens used")).font(.system(size: 12).monospacedDigit()).foregroundColor(s.high ? red : hx(l.text2)) }
                Spacer()
                s.clearButton { Text("Clear the page").font(.system(size: 13, weight: .bold)).foregroundColor(red).padding(.bottom, 2).overlay(Rectangle().fill(red).frame(height: 2), alignment: .bottom) }
            }.padding(.top, 12).padding(.bottom, 18)
        }.foregroundColor(ink).padding(.horizontal, 28)
    }
}
/// An oscilloscope line through the levels, tapered at both ends.
struct Scope: View {
    @ObservedObject var m: Meter
    let ink: Color, rule: Color
    var body: some View {
        let lv = levels(m, 48).map { CGFloat($0.0) }
        Canvas { g, size in
            let mid = size.height / 2, n = max(lv.count - 1, 1)
            g.stroke(Path { $0.move(to: CGPoint(x: 0, y: mid)); $0.addLine(to: CGPoint(x: size.width, y: mid)) }, with: .color(rule))
            g.stroke(Path { p in
                for (i, v) in lv.enumerated() {
                    let x = size.width * CGFloat(i) / CGFloat(n), y = mid + sin(CGFloat(i) * 0.9) * v * mid * sin(.pi * CGFloat(i) / CGFloat(n))
                    i == 0 ? p.move(to: CGPoint(x: x, y: y)) : p.addLine(to: CGPoint(x: x, y: y))
                }
            }, with: .color(ink), style: StrokeStyle(lineWidth: 1.5, lineJoin: .round))
        }.accessibilityHidden(true)
    }
}

// MARK: HUD: a heads-up display, corner brackets, mirrored bars, a dial for context

struct HUDLook: View {
    let s: Live, l: Look
    var body: some View {
        let cyan = hx(l.accent), faint = hx(l.dim), mono = Font.system(size: 11, design: .monospaced)
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Text("\(s.name.uppercased())//VOICE").foregroundColor(cyan)
                Text(s.talk == .speaking ? "▶ TX" : s.talk == .listening ? "● RX" : "◌ PROC").foregroundColor(faint)
                Spacer()
                Text(s.hint.uppercased()).foregroundColor(faint).lineLimit(1)
            }.font(mono).tracking(1.2)
            VStack(alignment: .leading, spacing: 10) {
                HStack { Text("STAGE"); Spacer(); Text("LIVE").foregroundColor(cyan) }.font(mono).tracking(1.2).foregroundColor(faint)
                s.stage(l, r: 0, pad: 0)
            }.padding(14).background(hx(l.stage)).overlay(Rectangle().stroke(hx(l.line)))
            if !s.said.isEmpty { Text("\(Text("\(s.name.uppercased()) ▸ ").font(mono).foregroundColor(cyan))\(Text(s.said).font(.system(size: 16)))").lineLimit(3) }
            if !s.heard.isEmpty { Text("\(Text("YOU ▸ ").font(mono))\(Text(s.heard).font(.system(size: 13)))").foregroundColor(faint).lineLimit(2) }
            s.activity(l)
            VStack(spacing: 1) {
                Bars(m: s.v.meter, n: 40, w: 4, gap: 4, h: 30, round: false, on: cyan, off: hx("4A6573"))
                Bars(m: s.v.meter, n: 40, w: 4, gap: 4, h: 30, round: false, on: cyan, off: hx("4A6573")).scaleEffect(x: 1, y: -1).opacity(0.35)
            }.frame(maxWidth: .infinity)
            HStack(spacing: 14) {
                s.ctx {
                    ZStack {
                        Circle().trim(from: 0, to: 0.75).stroke(hx(l.line), lineWidth: 5).rotationEffect(.degrees(135))
                        Circle().trim(from: 0, to: 0.75 * max(s.frac, 0.005)).stroke(s.high ? hx("FF9F0A") : cyan, lineWidth: 5).rotationEffect(.degrees(135))
                        VStack(spacing: 0) { Text(s.c.ctxShort.replacingOccurrences(of: "%", with: "")).font(.system(size: 17, weight: .bold, design: .monospaced)); Text(s.known ? "CTX%" : "CTX").font(.system(size: 9, design: .monospaced)).foregroundColor(faint) }
                    }.frame(width: 64, height: 64)
                }
                Text("\(s.tokens.uppercased()) TOKENS\nSAY “RESET” TO CLEAR").font(mono).tracking(0.8).foregroundColor(faint).lineSpacing(3)
                Spacer()
                s.clearButton { Text("[ CLEAR ]").font(.system(size: 12, weight: .bold, design: .monospaced)).tracking(1.6).foregroundColor(cyan).padding(.horizontal, 16).frame(height: 40).background(cyan.opacity(0.1)).overlay(Rectangle().stroke(cyan)) }
            }
        }.foregroundColor(hx(l.text)).padding(22).overlay(Brackets().stroke(cyan, lineWidth: 2).padding(8).allowsHitTesting(false))
    }
}
struct Brackets: Shape {
    func path(in r: CGRect) -> Path {
        let a: CGFloat = 16
        return Path { p in
            for (x, y, dx, dy) in [(r.minX, r.minY, a, a), (r.maxX, r.minY, -a, a), (r.minX, r.maxY, a, -a), (r.maxX, r.maxY, -a, -a)] {
                p.move(to: CGPoint(x: x, y: y + dy)); p.addLine(to: CGPoint(x: x, y: y)); p.addLine(to: CGPoint(x: x + dx, y: y))
            }
        }
    }
}

// MARK: Zen: almost nothing, one breathing dot and a hairline of context

struct ZenLook: View {
    let s: Live, l: Look
    var body: some View {
        VStack(spacing: 0) {
            GeometryReader { g in Rectangle().fill(s.high ? hx("FF9F0A") : hx(l.accent)).frame(width: g.size.width * s.frac) }.frame(height: 2).accessibilityHidden(true)
            s.stage(l, r: 0, pad: 0).padding(.horizontal, 32).padding(.top, 24)
            if !s.said.isEmpty { Text(s.said).font(.system(size: 24, weight: .light)).foregroundColor(hx(l.text)).multilineTextAlignment(.center).lineLimit(3).padding(.horizontal, 40).padding(.top, 20) }
            if !s.heard.isEmpty { Text(s.heard).font(.system(size: 13)).foregroundColor(hx(l.dim)).lineLimit(2).padding(.top, 8) }
            s.activity(l).padding(.horizontal, 40).padding(.top, 10)
            Breath(m: s.v.meter, talk: s.talk, l: l).frame(height: 40).padding(.top, 24)
            Text(s.label.lowercased()).font(.system(size: 12)).foregroundColor(hx(l.dim)).padding(.top, 4)
            HStack {
                s.ctx { Text(s.c.ctxShort).font(.system(size: 12).monospacedDigit()).foregroundColor(s.high ? hx("FF9F0A") : hx(l.dim)) }
                Spacer()
                s.clearButton { Text("Clear").font(.system(size: 13)).foregroundColor(hx(l.text2)) }
            }.padding(.horizontal, 22).padding(.top, 20).padding(.bottom, 18)
        }
    }
}
struct Breath: View {
    @ObservedObject var m: Meter
    let talk: Talk, l: Look
    var body: some View {
        let lv = CGFloat(levels(m, 1).first?.0 ?? 0)
        TimelineView(.animation(minimumInterval: 1 / 30, paused: talk == .thinking)) { t in
            let b = talk == .thinking ? 1 : 1.3 + 0.3 * sin(t.date.timeIntervalSinceReferenceDate * 2)
            Circle().fill(talk == .speaking ? hx(l.accent) : talk == .listening ? hx(l.text) : hx("4A4A52")).frame(width: 10, height: 10).scaleEffect(b + lv * 1.4)
        }.accessibilityHidden(true)
    }
}

struct StageWeb: NSViewRepresentable {
    let web: WKWebView
    func makeNSView(context: Context) -> WKWebView { web }
    func updateNSView(_ v: WKWebView, context: Context) {}
}

/// The voice button's help and accessibility label. While a start is under way (its retries take up to ~3 s) a press cancels it, so it says so.
func voiceButtonText(on: Bool, starting: Bool) -> (help: String, label: String) {
    on ? ("End voice mode (or say “goodbye”)", "End voice mode")
        : starting ? ("Starting voice mode… click to cancel", "Cancel starting voice mode")
        : ("Start voice mode: talk with it hands-free", "Start voice mode")
}

/// The chat header's knobs, top right, in the state they're in: what a click does now, for the hover hint (`help`) and VoiceOver (`label`).
struct HeaderTips {
    var on = false, starting = false, transcript = false, captions = false, out = false
    var name = "Chief", look = "Orb", next = "Conversation"
    var attach: (help: String, label: String) { ("Attach \(ChatFile.supported) to what you say next", "Attach files") }
    var view: (help: String, label: String) {
        transcript ? ("Show the stage: what \(name) draws as it talks", "Show the stage") : ("Show the transcript: the conversation as text", "Show the transcript")
    }
    var caption: (help: String, label: String) {
        captions ? ("Hide captions: the words said and heard", "Turn captions off") : ("Show captions: the words said and heard, as text", "Turn captions on")
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
        HeaderTips(on: v.on, starting: v.starting, transcript: v.transcript, captions: v.captions, out: out, name: c.name, look: v.look.name, next: nextLook.name)
    }
    var body: some View {
        let t = tips
        HStack(spacing: lk.id == "zen" || lk.id == "editorial" ? Theme.sp1 : Theme.sp2) {
            if v.on {
                if !v.transcript {  // the stage has no composer: its +. Drops on the stage and ⌘V attach too
                    knob("paperclip", t.attach) { c.pickFiles(NSApp.keyWindow) }
                }
                knob(v.transcript ? "waveform" : "text.bubble", t.view) { v.transcript.toggle() }
                knob(v.captions ? "captions.bubble.fill" : "captions.bubble", t.caption) { v.captions.toggle() }
            }
            // one style for live and standard chat
            knob("paintpalette", t.style) { v.look = nextLook }
            Button(action: v.toggle) { shell(Image(systemName: "waveform").font(sys(size: Theme.fs3, weight: .semibold)), on: v.on || v.starting).opacity(v.starting ? 0.6 : 1) }
                .buttonStyle(.plain).modifier(Tip(c: c, text: t.voice.help)).accessibilityLabel(t.voice.label)
            knob(out ? "rectangle.righthalf.inset.filled" : "arrow.up.forward.square", t.pop) { c.popChat(!out) }  // its own window, or back
        }
    }
    func knob(_ icon: String, _ t: (help: String, label: String), _ act: @escaping () -> Void) -> some View {
        Button(action: act) { shell(Image(systemName: icon)) }.buttonStyle(.plain).modifier(Tip(c: c, text: t.help)).accessibilityLabel(t.label)
    }
    /// The knob in the look: a ring (Orb), a 6pt tile (Conversation), a square (On Air, HUD), a green box (Phosphor), bare (Editorial, Zen).
    func shell<V: View>(_ icon: V, on: Bool = false) -> some View {
        let s: CGFloat = lk.id == "zen" ? 28 : lk.id == "phosphor" ? 26 : 30, r: CGFloat = lk.id == "orb" ? s / 2 : lk.id == "conversation" ? Theme.r1 : 0
        let tile = lk.id == "conversation" || lk.id == "onair", bare = lk.id == "zen" || lk.id == "editorial"
        return icon.font(sys(size: Theme.fs3)).frame(width: s, height: s).foregroundColor(on ? bg : lk.id == "phosphor" ? fg : lk.id == "zen" ? dim : sub)
            .background(RoundedRectangle(cornerRadius: r).fill(on ? accent : tile ? card : .clear))
            .overlay(RoundedRectangle(cornerRadius: r).stroke(on || bare ? .clear : line)).contentShape(Rectangle())
    }
}

