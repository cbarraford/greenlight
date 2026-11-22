// Greenlight: menubar chat with greenlight (on this Mac or an ssh host), live activity while it works, replyable notifications.
// Chat: `chat.py send|compact|new|stop` streams JSON events (see chat.py's Norm).
// Pings: greenlight appends JSON lines to <greenlightHome>/outbox.jsonl (`greenlight.py ping`); we tail it.
// Where greenlight runs: Settings › Hosts (or `defaults write <bundle id> <key> <value>`), read at launch:
//   host       "local" (default) runs greenlight's Python here; anything else is an ssh host (GREENLIGHT_HOST overrides)
//   greenlightHome  greenlight's state dir on that host (default ~/.greenlight)
//   greenlightCode  where greenlight's *.py live there (default ~/.claude/skills/greenlight)
// A dev copy (greenlight-<workspace>, scripts/dev.py) is this app under another name with settings of its own: see devDir.
// No @State/@Observable: those are macros, and the Command Line Tools swiftc has no SwiftUI macro plugin.
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

let localDefault: String = { var b = [CChar](repeating: 0, count: 256); gethostname(&b, 255); return String(cString: b).components(separatedBy: ".")[0] }()  // "Local" in the Basement, until you rename it
let uitest = CommandLine.arguments.contains("--uitest")
let uitestSuiteName: String? = uitest ? "dev.greenlight.uitest.\(ProcessInfo.processInfo.processIdentifier)" : nil
let uitestSocketPath: String? = {
    if let i = CommandLine.arguments.firstIndex(of: "--uitest-socket"), i + 1 < CommandLine.arguments.count {
        return CommandLine.arguments[i + 1]
    }
    return ProcessInfo.processInfo.environment["GREENLIGHT_UITEST_SOCKET"]
}()
let ud: UserDefaults = uitestSuiteName.flatMap { UserDefaults(suiteName: $0) } ?? UserDefaults.standard
let host = ProcessInfo.processInfo.environment["GREENLIGHT_HOST"] ?? ud.string(forKey: "host") ?? "local"
let greenlightHome = ProcessInfo.processInfo.environment["GREENLIGHT_HOME"] ?? ud.string(forKey: "greenlightHome") ?? "~/.greenlight"
let greenlightCode = ProcessInfo.processInfo.environment["GREENLIGHT_CODE"] ?? ud.string(forKey: "greenlightCode") ?? "~/.claude/skills/greenlight"
let tmuxSocketName = ProcessInfo.processInfo.environment["GREENLIGHT_TMUX_SOCKET"] ?? (uitest ? "gl-uitest-\(ProcessInfo.processInfo.processIdentifier)" : "greenlight")
/// Quotes a filesystem path for a POSIX shell while preserving leading `~` expansion on local or remote hosts.
func shellPath(_ p: String) -> String {
    if p == "~" { return "$HOME" }
    if p.hasPrefix("~/") { return "$HOME/" + shellQuote(String(p.dropFirst(2))) }
    return shellQuote(p)
}

let py = "GREENLIGHT_HOME=\(shellPath(greenlightHome)) GREENLIGHT_TMUX_SOCKET=\(shellQuote(tmuxSocketName)) python3 \(shellPath(greenlightCode))/scripts"  // + "/greenlight.py ..."
let selftest = CommandLine.arguments.contains("--selftest") || CommandLine.arguments.contains("--attachtest")  // scratch chat session + transcript, see selfTest()
let chatCmd = (selftest ? "CHAT_DIR=\(shellPath(greenlightHome))/chat-test " : "") + "\(py)/chat.py"
let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String ?? "Greenlight"  // "greenlight-<workspace>" in a copy
/// A dev copy's folder on this Mac (Info.plist's GreenlightDev, from build.sh), nil in Greenlight itself. A copy loads its pages
/// from there and reloads one when it changes, sends its ssh through dev.py's tunnel, and posts no banners (Greenlight does).
let devDir = Bundle.main.object(forInfoDictionaryKey: "GreenlightDev") as? String
/// A dev copy's workspace ("faster-qa"): on its menubar item and in its window bar, so you can tell the copies and Greenlight apart.
let devName = devDir.map { URL(fileURLWithPath: $0).lastPathComponent }
let devTagWidth: CGFloat = devName.map { min(190, ceil(($0 as NSString).size(withAttributes: [.font: NSFont.monospacedSystemFont(ofSize: 9.5, weight: .bold)]).width) + 44) } ?? 0  // the window bar's tag: 0 in Greenlight
/// A dev copy's extra ssh options to a host dev.py has a tunnel to: the tunnel's socket, so nothing connects in the copy's
/// name and a firewall (Little Snitch) has no new app to ask about, however many copies there are. Empty otherwise.
func devSsh(_ h: String) -> [String] {
    guard let devDir else { return [] }
    let sock = URL(fileURLWithPath: devDir).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("ssh/\(h).sock").path
    return FileManager.default.fileExists(atPath: sock) ? ["-o", "ProxyCommand=/usr/bin/nc -U '\(sock)'"] : []
}
/// A bundled page (dashboard, terminal, stage): in a dev copy or --uitest, the workspace's live one.
func page(_ name: String) -> URL? {
    if let webDir = ProcessInfo.processInfo.environment["GREENLIGHT_WEB_DIR"], FileManager.default.fileExists(atPath: "\(webDir)/\(name).html") {
        return URL(fileURLWithPath: "\(webDir)/\(name).html")
    }
    if let devDir, FileManager.default.fileExists(atPath: "\(devDir)/web/\(name).html") { return URL(fileURLWithPath: "\(devDir)/web/\(name).html") }
    return Bundle.main.url(forResource: name, withExtension: "html")
}

extension WKWebView {
    /// Safely invokes a JS function path (e.g. "window.mc && mc.openPage") with JSON-serialized arguments.
    func callJS(_ fn: String, _ args: [Any] = [], done: ((Any?, Error?) -> Void)? = nil) {
        guard let data = try? JSONSerialization.data(withJSONObject: args),
              let json = String(data: data, encoding: .utf8) else { return }
        evaluateJavaScript("(() => { const f = \(fn); return typeof f === 'function' ? f(...\(json)) : undefined; })()", completionHandler: done)
    }
}

/// The process that runs a command where greenlight lives: a login shell here (so PATH has glab/gh/orca), or ssh.
/// Under --uitest on local host, use /bin/sh -c so login profile path_helper cannot reorder stub binaries on PATH.
func remoteArgv(_ remote: String, on host: String = host) -> (String, [String]) {
    if host.isEmpty || host == "local" {
        if uitest { return ("/bin/sh", ["-c", remote]) }
        return (ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh", ["-lc", remote])
    }
    return ("/usr/bin/ssh", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30"] + devSsh(host) + [host, remote])
}

/// The terminal sidebar's Hosts: ~/.ssh/config's Host names, minus wildcards, git hosts and the -ts (Tailscale) twin of a listed host.
func sshHosts() -> [String] {
    let cfg = (try? String(contentsOfFile: NSHomeDirectory() + "/.ssh/config", encoding: .utf8)) ?? ""  // ponytail: Include files aren't followed
    var names = [String]()
    for l in cfg.split(separator: "\n") {
        let w = l.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)
        guard w.first?.lowercased() == "host" else { continue }
        for n in w.dropFirst() where !names.contains(n) && !n.hasPrefix("git") && n.range(of: #"^\w[\w.-]{0,63}$"#, options: .regularExpression) != nil { names.append(n) }
    }
    return names.filter { !($0.hasSuffix("-ts") && names.contains(String($0.dropLast(3)))) }
}
/// A pane's host and folder: "@name" is a host's home (the Hosts rows), anything else a folder on GREENLIGHT_HOST.
func paneWhere(_ ws: String) -> (host: String, cwd: String)? {
    if ws.hasPrefix("@") { return ws.range(of: #"^@\w[\w.-]{0,63}$"#, options: .regularExpression) != nil ? (String(ws.dropFirst()), "") : nil }
    return ws.isEmpty || (ws.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil && !ws.contains("..")) ? (host, ws) : nil
}

/// A color scheme: four base colors; the surfaces and text greys are mixed from bg and fg.
struct Theme {
    let name: String, bg, fg, accent, bubble: NSColor
    init(_ name: String, _ bg: String, _ fg: String, _ accent: String, _ bubble: String) {
        (self.name, self.bg, self.fg, self.accent, self.bubble) = (name, hex(bg), hex(fg), hex(accent), hex(bubble))
    }
    func blend(_ f: CGFloat) -> NSColor { bg.blended(withFraction: f, of: fg) ?? fg }
    func mix(_ f: CGFloat) -> Color { Color(nsColor: blend(f)) }
    var dark: Bool { (bg.usingColorSpace(.sRGB)?.brightnessComponent ?? 0) < 0.5 }

    // The dashboard's tokens (dashboard.html :root), so the native panes and the HTML share one scale.
    static let r1: CGFloat = 6, r2: CGFloat = 10, r3: CGFloat = 14, r4: CGFloat = 20  // --r-1…4: controls, cards, sheets
    static let fs1: CGFloat = 11, fs2: CGFloat = 12, fs3: CGFloat = 13, fs4: CGFloat = 15, fs5: CGFloat = 18, fs6: CGFloat = 22  // --fs-1…6, before sys()'s size setting
    static let glyph: CGFloat = 9  // small SF Symbols in a fixed frame (xmark, chevron, check)
    static let sp1: CGFloat = 4, sp2: CGFloat = 8, sp3: CGFloat = 12, sp4: CGFloat = 16, sp5: CGFloat = 24, sp6: CGFloat = 32  // --sp-1…6, 4pt grid
    /// State colors, dark | light, as the dashboard's. Never the accent: orange always means "needs you", red "crashed".
    static func state(_ d: String, _ l: String) -> Color { Color(nsColor: hex(theme.dark ? d : l)) }
    static var needs: Color { state("FF9F0A", "F08A00") }     // --orange: needs you
    static var yourMove: Color { state("FFD60A", "E6B800") }  // --yellow
    static var working: Color { state("0A84FF", "0A7AFF") }   // --blue
    static var crashed: Color { state("FF453A", "E5392F") }   // --red
    static var queued: Color { state("8E8E93", "8E8E93") }    // --gray
    static var done: Color { state("30D158", "2BA84A") }      // --green
    static var needsInk: Color { state("FF9F0A", "A85A00") }  // --orange-ink / --red-ink: state text on a card
    static var crashedInk: Color { state("FF6B61", "C62828") }
    static var workingInk: Color { state("6FB4FF", "0062CC") }  // --blue-ink: Working text; Theme.working stays the dot and tint
}

func hex(_ s: String) -> NSColor {
    let v = Int(s, radix: 16) ?? 0
    return NSColor(srgbRed: CGFloat(v >> 16 & 255) / 255, green: CGFloat(v >> 8 & 255) / 255, blue: CGFloat(v & 255) / 255, alpha: 1)
}

/// Palettes from popular terminal and editor themes.
let themes = [
    Theme("Greenlight", "1C1C1F", "ECECEE", "F5A524", "2563EB"),
    Theme("Dracula", "282A36", "F8F8F2", "BD93F9", "6272A4"),
    Theme("Nord", "2E3440", "ECEFF4", "88C0D0", "5E81AC"),
    Theme("Tokyo Night", "1A1B26", "C0CAF5", "7AA2F7", "3D59A1"),
    Theme("Catppuccin Mocha", "1E1E2E", "CDD6F4", "CBA6F7", "585B70"),
    Theme("Gruvbox Dark", "282828", "EBDBB2", "FE8019", "458588"),
    Theme("Solarized Dark", "002B36", "EEE8D5", "B58900", "268BD2"),
    Theme("One Dark", "282C34", "ABB2BF", "61AFEF", "7E4E9E"),
    Theme("Monokai", "272822", "F8F8F2", "A6E22E", "AE2B5E"),
    Theme("Rosé Pine", "191724", "E0DEF4", "EBBCBA", "31748F"),
    Theme("Kanagawa", "1F1F28", "DCD7BA", "E6C384", "2D4F67"),
    Theme("Everforest", "2D353B", "D3C6AA", "A7C080", "3A6B67"),
    Theme("GitHub Dark", "0D1117", "E6EDF3", "2F81F7", "1F6FEB"),
    Theme("Homebrew", "000000", "28FE14", "28FE14", "0A5C0A"),
    Theme("Solarized Light", "FDF6E3", "586E75", "B58900", "268BD2"),
    Theme("Catppuccin Latte", "EFF1F5", "4C4F69", "8839EF", "1E66F5"),
    Theme("GitHub Light", "FFFFFF", "1F2328", "0969DA", "0969DA"),
    Theme("One Light", "FAFAFA", "383A42", "4078F2", "4078F2"),
    Theme("Gruvbox Light", "FBF1C7", "3C3836", "AF3A03", "076678"),
    Theme("Rosé Pine Dawn", "FAF4ED", "575279", "D7827E", "286983"),
    Theme("Tokyo Night Day", "E1E2E7", "3760BF", "2E7DE9", "2E7DE9"),
]
let accents = ["F5A524", "FF7A45", "F7768E", "FF79C6", "BD93F9", "7AA2F7", "2AC3DE", "9ECE6A"]
/// Fonts from terminal apps; only the installed ones are offered.
let fontChoices = ["System", "SF Mono", "Menlo", "Monaco", "JetBrains Mono", "Fira Code", "Cascadia Code", "Hack", "Source Code Pro",
                   "IBM Plex Mono", "Iosevka", "Berkeley Mono", "Geist Mono", "Inter", "Helvetica Neue", "Avenir Next"]
    .filter { ["System", "SF Mono"].contains($0) || NSFontManager.shared.availableFontFamilies.contains($0) }

func named(_ key: String, _ fallback: String) -> Theme { themes.first { $0.name == (ud.string(forKey: key) ?? fallback) } ?? themes[0] }
var darkTheme = named("darkTheme", "Greenlight")
var lightTheme = named("lightTheme", "GitHub Light")
/// The window bar's Auto/Light/Dark switch: every floor wears lightTheme or darkTheme; Auto follows macOS (the app's own appearance, which no window overrides).
var appearanceMode: String { ud.string(forKey: "appearance") ?? "auto" }
var isDark: Bool { appearanceMode == "auto" ? NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua : appearanceMode == "dark" }
var theme: Theme { isDark ? darkTheme : lightTheme }
var accentHex = ud.string(forKey: "accent")  // nil = the theme's own
var chatFont = ud.string(forKey: "chatFont") ?? "System"
var codeFont = ud.string(forKey: "codeFont") ?? "SF Mono"
var fontSize = ud.object(forKey: "fontSize") as? Double ?? 13

var accent: Color { Color(nsColor: accentHex.map(hex) ?? theme.accent) }
var bg: Color { Color(nsColor: theme.bg) }
var fg: Color { Color(nsColor: theme.fg) }
var card: Color { theme.mix(0.05) }
var surface: Color { theme.mix(0.07) }
var line: Color { theme.mix(0.1) }
var edge: Color { theme.mix(0.17) }
var dim: Color { theme.mix(0.6) }
var sub: Color { theme.mix(0.82) }
var bubble: Color { Color(nsColor: theme.bubble) }

/// `.system(size:)` at the chosen size; `design: .monospaced` uses the code font. Interface chrome stays in the system font.
func sys(size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default, face: String = "System") -> Font {
    let s = size * fontSize / 13, f = design == .monospaced ? codeFont : face
    if f == "System" { return .system(size: s, weight: weight, design: design) }
    if f == "SF Mono" { return .system(size: s, weight: weight, design: .monospaced) }
    return .custom(f, size: s).weight(weight)
}
/// The chosen chat font: for the conversation's own text (messages, the composer, questions), not the chrome around it.
func chatFace(size: CGFloat, weight: Font.Weight = .regular) -> Font { sys(size: size, weight: weight, face: chatFont) }
let models = [("Opus 5.5", "claude-opus-5-5"), ("Sonnet 5.5", "claude-sonnet-5-5"), ("Haiku 5.5", "claude-haiku-5-5"),
              ("Fable 5.1", "claude-fable-5-1"),
              ("Gemini 4 Neon · Low", "gemini-4-neon-low"), ("Gemini 4 Neon · Medium", "gemini-4-neon-medium"),
              ("Gemini 4 Neon · High", "gemini-4-neon-high"), ("Gemini 4 Neon · Extra high", "gemini-4-neon-xhigh"),
              ("Gemini 4 Argon · Medium", "gemini-4-argon-medium"), ("Gemini 4 Hydrogen · Medium", "gemini-4-hydrogen-medium"),
              ("Gemini 3.8 Flash · Medium", "gemini-3.8-flash-medium"),
              ("Codex", "codex"), ("OpenCode", "opencode"), ("Pi", "pi"), ("Gemini CLI", "gemini")]
/// Gemini model ids run through agy; the Gemini CLI entry still starts that harness's default model.
func harnessOf(_ model: String) -> String { model.hasPrefix("claude-") ? "claude" : model.hasPrefix("gemini-") ? "agy" : model }
func savedModel(_ key: String, fallback: String) -> String {
    let stored = ud.string(forKey: key) ?? fallback
    return stored == "gemini-4-neon" ? "gemini-4-neon-medium" : stored
}
let efforts = ["low", "medium", "high", "xhigh", "max"]
func effortName(_ e: String) -> String { e == "xhigh" ? "Extra high" : e.capitalized }  // display only; the stored value stays

// MARK: Settings: repo lifecycles, instructions, memory

/// One lifecycle stage as dash.py's lifecycle-get gives it: a built-in check, or the user's own (done: "signoff" | "label=x").
struct Stage: Identifiable, Equatable { let id = UUID(); var key: String, req: String, done: String, run = "" }  // ponytail: run (a sign-off step's Run: command) is kept, not editable here: edit the .md

/// Settings → Repositories / Memory and the chat's instructions: each read and saved with `dash.py task <op> <arg>` where greenlight runs.
final class RulesStore: ObservableObject {
    @Published var repos = [String]()
    @Published var repo = ""
    @Published var stages = [Stage]()
    @Published var options = [String: String]()
    @Published var rules = ""
    @Published var intro = ""
    @Published var source = ""
    @Published var builtins = [String: String]()
    @Published var instructions = ""
    @Published var notes = [(name: String, text: String)]()
    @Published var note = ""
    @Published var noteText = ""
    @Published var projs = [[String: Any]]()  // the Basement's projects (projects.json), for Settings → Terminal's default folders
    @Published var projNote = ""
    @Published var review = ""  // your own prompt for repo's kind (review, threads, ci, conflicts); reviewOwn false = the default
    @Published var reviewDefault = ""
    @Published var reviewOwn = false
    @Published var reviewVars = [String]()
    @Published var kind = "review"
    static let kinds = [("review", "Review"), ("threads", "Resolve threads"), ("ci", "Fix CI"), ("conflicts", "Fix conflicts")]
    @Published var autoKinds = [(kind: String, when: String, gates: [String])]()  // Auto-run: dash.py's AUTO, and this repo's {kind: gate}
    @Published var autoOn = [String: Int]()
    @Published var status = ""
    @Published var busy = false
    @Published var repoPaths = [String: String]()  // repo → its path in orca, under its name in Repositories
    // Settings' own UI state: the sidebar's search, the open project and repo tab, the add forms' drafts
    @Published var q = ""
    @Published var proj = ""
    @Published var rtab = "stages"
    @Published var adding = false
    @Published var draftRepo = ""
    @Published var draftProj = ""
    @Published var draftDir = ""
    var loaded = false

    func call(_ op: String, _ arg: String, _ stdin: String = "", _ done: @escaping ([String: Any]) -> Void) {
        busy = true
        var out = ""
        ssh("\(py)/dash.py task \(op) '\(arg)'", stdin: stdin, onLine: { out += $0 }, onExit: { [weak self] code in
            self?.busy = false
            let j = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: Any] ?? ["error": "\(host) didn't answer (exit \(code))"]
            if let e = j["error"] as? String { self?.status = e } else { done(j) }
        })
    }
    func load() {
        if loaded { return }
        loaded = true
        call("repos", "-") { j in
            let paths = ((j["repos"] as? [[String: Any]]) ?? []).compactMap { $0["path"] as? String }
            let names = paths.compactMap { $0.split(separator: "/").last.map(String.init) }
            self.repoPaths = Dictionary(zip(names, paths), uniquingKeysWith: { a, _ in a })
            self.repos = Array(Set(names + (self.repo.isEmpty ? [] : [self.repo]))).sorted()
            if self.repo.isEmpty, let r = self.repos.first { self.pick(r) }
        }
        call("instructions-get", "-") { self.instructions = $0["text"] as? String ?? "" }
        call("memory-list", "-") { self.gotNotes($0) }
    }
    func pick(_ r: String) {
        repo = r; status = ""; if !repos.contains(r) { repos = (repos + [r]).sorted() }
        call("lifecycle-get", r) { self.got($0) }; getPrompt(); call("autorun-get", r) { self.gotAuto($0) }
    }
    func gotAuto(_ j: [String: Any]) {
        autoKinds = (j["kinds"] as? [[String: Any]] ?? []).map { (kind: $0["kind"] as? String ?? "", when: $0["when"] as? String ?? "", gates: $0["gates"] as? [String] ?? []) }
        autoOn = j["on"] as? [String: Int] ?? [:]
    }
    func setAuto(_ k: String, _ gate: Int?) {  // saved at once; nil = off
        autoOn[k] = gate
        if gate != nil { ud.set(true, forKey: "autorun") }  // the app keeps the Basement's page loaded and its workspaces fresh from now on
        let s = String(decoding: (try? JSONSerialization.data(withJSONObject: autoOn)) ?? Data(), as: UTF8.self)
        call("autorun-set", repo, s) { self.gotAuto($0) }
    }
    func getPrompt() { call("prompt-get", "\(repo)/\(kind)") { self.gotReview($0) } }
    func gotReview(_ j: [String: Any]) {
        guard j["kind"] as? String == kind else { return }  // an answer for the tab you just left
        review = j["text"] as? String ?? ""; reviewDefault = j["default"] as? String ?? ""; reviewOwn = j["own"] as? Bool ?? false
        reviewVars = (j["vars"] as? [String] ?? []).map { "{\($0)}" } + ["{default}"]
    }
    func saveReview() {  // empty or Default: back to the default (dash.py deletes yours)
        let own = reviewOwn && !review.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        call("prompt-set", "\(repo)/\(kind)", own ? review : "") { self.gotReview($0); self.status = own ? "Saved the prompt." : "Using the default prompt." }
    }
    func got(_ j: [String: Any]) {
        stages = ((j["stages"] as? [[String: String]]) ?? []).map { Stage(key: $0["key"] ?? "", req: $0["req"] ?? "", done: $0["done"] ?? "", run: $0["run"] ?? "") }
        options = j["options"] as? [String: String] ?? [:]
        rules = j["rules"] as? String ?? ""; intro = j["intro"] as? String ?? ""; source = j["source"] as? String ?? ""
        builtins = j["builtins"] as? [String: String] ?? [:]
    }
    func save() {
        let doc: [String: Any] = ["stages": stages.map { ["key": $0.key, "req": $0.req, "done": $0.done, "run": $0.run] }, "options": options, "rules": rules, "intro": intro]
        let s = String(decoding: (try? JSONSerialization.data(withJSONObject: doc)) ?? Data(), as: UTF8.self)
        call("lifecycle-set", repo, s) { self.got($0); self.status = "Saved." }
    }
    func reset() { call("lifecycle-reset", repo) { self.got($0); self.status = "Back to the \(self.source) lifecycle." } }
    func saveInstructions() { call("instructions-set", "-", instructions) { self.instructions = $0["text"] as? String ?? ""; self.status = "Saved." } }
    func gotNotes(_ j: [String: Any]) {
        notes = ((j["notes"] as? [[String: String]]) ?? []).map { (name: $0["name"] ?? "", text: $0["text"] ?? "") }
        if !notes.contains(where: { $0.name == note }) { note = ""; noteText = "" }
    }
    func saveNote() { call("memory-set", note, noteText) { self.gotNotes($0); self.status = "Saved \(self.note)." } }
    func deleteNote() { let n = note; call("memory-delete", n) { self.gotNotes($0); self.status = "Deleted \(n)." } }
}

// MARK: ssh

/// Runs <remote> where greenlight lives (remoteArgv), calling onLine (main thread) per stdout line and onExit when it ends.
@discardableResult
func ssh(_ remote: String, on h: String = host, stdin: String = "", onLine: @escaping (String) -> Void = { _ in },
         onExit: @escaping (Int32) -> Void = { _ in }) -> Process {
    let p = Process()
    let (exe, args) = remoteArgv(remote, on: h)
    p.executableURL = URL(fileURLWithPath: exe)
    p.arguments = args
    let inPipe = Pipe(), out = Pipe()
    p.standardInput = inPipe
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    final class Buf { var s = Data() }
    let buf = Buf()
    out.fileHandleForReading.readabilityHandler = { h in
        let d = h.availableData
        if d.isEmpty { return }
        buf.s.append(d)
        while let nl = buf.s.firstIndex(of: 10) {
            let l = String(decoding: buf.s[buf.s.startIndex..<nl], as: UTF8.self)
            buf.s.removeSubrange(buf.s.startIndex...nl)
            DispatchQueue.main.async { onLine(l) }
        }
    }
    p.terminationHandler = { p in
        out.fileHandleForReading.readabilityHandler = nil
        let rest = out.fileHandleForReading.readDataToEndOfFile()
        DispatchQueue.main.async {
            for l in String(decoding: buf.s + rest, as: UTF8.self).split(separator: "\n") { onLine(String(l)) }
            onExit(p.terminationStatus)
        }
    }
    do { try p.run() } catch { DispatchQueue.main.async { onExit(-1) }; return p }
    let w = inPipe.fileHandleForWriting, d = Data(stdin.utf8)
    // off the main thread: a pipe holds 16 KB, and ssh reads stdin only once it's connected (attached files are MBs).
    // The throwing write: a dead ssh's EPIPE isn't a crash
    DispatchQueue.global(qos: .userInitiated).async { try? w.write(contentsOf: d); try? w.close() }
    return p
}

