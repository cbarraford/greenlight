// Greenlight: menubar chat with greenlight (on this Mac or an ssh host), live activity while it works, replyable notifications.
// Chat: `chat.py send|compact|new|stop` streams JSON events (see chat.py's Norm).
// Pings: greenlight appends JSON lines to <greenlightHome>/outbox.jsonl (`greenlight.py ping`); we tail it.
// Where greenlight runs: Settings › Hosts (or `defaults write <bundle id> <key> <value>`), read at launch:
//   host       "local" (default) runs greenlight's Python here; anything else is an ssh host (GREENLIGHT_HOST overrides)
//   greenlightHome  greenlight's state dir on that host (default ~/.greenlight)
//   greenlightCode  where greenlight's *.py live there (default ~/.claude/skills/greenlight)
// No @State/@Observable: those are macros, and the Command Line Tools swiftc has no SwiftUI macro plugin.
import AppKit
import Combine
import CoreAudio
import AVFoundation
import Carbon
import CryptoKit
import FoundationModels
import Network
import Security
import Speech
import SwiftUI
import UniformTypeIdentifiers
import UserNotifications
import WebKit

let localDefault: String = { var b = [CChar](repeating: 0, count: 256); gethostname(&b, 255); return String(cString: b).components(separatedBy: ".")[0] }()  // "Local" in the Basement, until you rename it
let host = ProcessInfo.processInfo.environment["GREENLIGHT_HOST"] ?? UserDefaults.standard.string(forKey: "host") ?? "local"
let greenlightHome = UserDefaults.standard.string(forKey: "greenlightHome") ?? "~/.greenlight"
let greenlightCode = UserDefaults.standard.string(forKey: "greenlightCode") ?? "~/.claude/skills/greenlight"
let py = "GREENLIGHT_HOME=\(greenlightHome) python3 \(greenlightCode)/scripts"  // + "/greenlight.py ..."
let selftest = CommandLine.arguments.contains("--selftest") || CommandLine.arguments.contains("--attachtest")  // scratch chat session + transcript, see selfTest()
let chatCmd = (selftest ? "CHAT_DIR=\(greenlightHome)/chat-test " : "") + "\(py)/chat.py"

/// The process that runs a command where greenlight lives: a login shell here (so PATH has glab/gh/orca), or ssh.
func remoteArgv(_ remote: String, on host: String = host) -> (String, [String]) {
    if host.isEmpty || host == "local" { return (ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh", ["-lc", remote]) }
    return ("/usr/bin/ssh", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30", host, remote])
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

let ud = UserDefaults.standard
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
              ("Fable 5.1", "claude-fable-5-1"), ("Gemini 4 Neon", "gemini-4-neon"),
              ("Codex", "codex"), ("OpenCode", "opencode"), ("Pi", "pi"), ("Gemini CLI", "gemini")]
/// A model id is a Claude model, a Gemini model (Gemini CLI runs it), or another harness's name (it runs that harness's default model).
func harnessOf(_ model: String) -> String { model.hasPrefix("claude-") ? "claude" : model.hasPrefix("gemini-") ? "gemini" : model }
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

// MARK: model

struct Step: Identifiable, Codable {
    let id: String
    var title: String, detail: String
    var out = ""
    var ok: Bool? = nil
    var start = Date()
    var end: Date? = nil
}

final class Msg: ObservableObject, Identifiable {
    enum Role: String, Codable { case you, greenlight, note }
    let id = UUID()
    let role: Role
    let text: String
    var steps: [Step] = []
    var duration: TimeInterval = 0
    var options: [String] = []
    var failed = false
    @Published var expanded = false
    init(_ role: Role, _ text: String) { self.role = role; self.text = text }

    struct Saved: Codable { let role: Role, text: String, steps: [Step], duration: TimeInterval, options: [String], failed: Bool }
    var saved: Saved { Saved(role: role, text: text, steps: steps, duration: duration, options: options, failed: failed) }
    convenience init(_ s: Saved) {
        self.init(s.role, s.text)
        steps = s.steps; duration = s.duration; options = s.options; failed = s.failed
    }
}

/// Something greenlight asked that waits in the bell inbox: every ping, and chat replies that end with an Options line.
struct Question: Codable, Identifiable {
    var id = UUID()
    let text: String
    var options: [String]
    let ping: Bool
    var ts = Date()
    var answer: String? = nil
    var key: String? = nil  // the workspace a ping is about; greenlight sends {"resolve": key} when it goes stale
    var ws: [String]? = nil  // workspaces it names (`greenlight.py ping --ws`): the dashboard lifts their hexes while it waits. Optional so old questions.json decodes
}

/// The transcript survives restarts; the conversation itself lives in chat.py's session file on greenlight's host.
let support = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Greenlight")
let transcript = support.appendingPathComponent(selftest ? "transcript-test.json" : "transcript.json")
let questionsFile = support.appendingPathComponent(selftest ? "questions-test.json" : "questions.json")

/// Splits a trailing "Options: a | b" line off greenlight's text.
func splitOptions(_ text: String) -> (String, [String]) {
    var lines = text.components(separatedBy: "\n")
    guard let last = lines.last?.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "`")),
          last.lowercased().hasPrefix("options:") else { return (text, []) }
    lines.removeLast()
    return (lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines),
            last.dropFirst(8).split(separator: "|").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty })
}

/// The watch's diff: workspaces (name -> why) needing the user in this snapshot, and those that weren't in the last one.
/// No last snapshot (launch) = nothing new: what already needed you isn't news.
func newNeeds(_ old: [String: String]?, _ pucks: [[String: Any]]) -> (now: [String: String], new: [String: String]) {
    var now = [String: String]()
    for p in pucks where p["state"] as? String == "needs" { if let n = p["name"] as? String { now[n] = p["why"] as? String ?? "" } }
    return (now, old.map { o in now.filter { o[$0.key] == nil } } ?? [:])
}

/// The watch's news split by the window you're in (focusScope's JSON, the same rule as dash.py's board): `ask` is about its
/// worktrees or repos, `hold` is another project's and waits until you're in a window that shows it. nil scope: every project's.
func inScope(_ fresh: [String: String], _ pucks: [[String: Any]], _ scope: String?) -> (ask: [String: String], hold: [String: String]) {
    guard let scope, let o = (try? JSONSerialization.jsonObject(with: Data(scope.utf8))) as? [String: Any] else { return (fresh, [:]) }
    let paths = o["paths"] as? [String] ?? [], repos = o["repos"] as? [String] ?? []
    let shown = Set(pucks.filter { p in  // a needing one: a same-named workspace elsewhere that's fine doesn't let another project's in
        p["state"] as? String == "needs"
            && ((p["path"] as? String).map(paths.contains) == true || (p["repo"] as? String).map { !$0.isEmpty && repos.contains($0) } == true)
    }.compactMap { $0["name"] as? String })
    return (fresh.filter { shown.contains($0.key) }, fresh.filter { !shown.contains($0.key) })
}

func ago(_ d: Date) -> String {
    let s = Int(Date().timeIntervalSince(d))
    return s < 60 ? "now" : s < 3600 ? "\(s / 60)m ago" : s < 86400 ? "\(s / 3600)h ago" : "\(s / 86400)d ago"
}

/// A prompt you sent: ↑↓ walk them, ⌃R fuzzy-searches them.
struct Sent: Codable, Equatable { let t: String; let at: Date }

/// ⌃R matching: the query's letters in order, spaces ignored; runs and word starts score higher. Tries each start, keeps the best.
func fuzzy(_ q: String, _ t: String) -> (pos: [Int], score: Int)? {
    let lq = q.lowercased().filter { !$0.isWhitespace }.map(String.init), lt = t.map { $0.lowercased() }
    guard let first = lq.first else { return nil }
    var best: (pos: [Int], score: Int)?
    for s in lt.indices where lt[s] == first {  // ponytail: O(n²) per prompt, fine for 500 short ones
        var pos: [Int] = [], score = 0, prev = -2
        for j in s..<lt.count where pos.count < lq.count && lt[j] == lq[pos.count] {
            score += (j == prev + 1 ? 5 : 1) + (j == 0 || " \n\t/-_.!:".contains(lt[j - 1]) ? 3 : 0)
            pos.append(j); prev = j
        }
        if pos.count == lq.count, score > best?.score ?? -1 { best = (pos, score) }
    }
    return best
}

/// !! is the previous prompt, !$ its last word; only as whole words, so "now!!" and !4521 stay as typed.
func expandBang(_ v: String, _ last: String) -> String {
    let re = try! NSRegularExpression(pattern: #"(?<=^|\s)!([!$])(?=\s|$)"#), word = last.split(whereSeparator: \.isWhitespace).last.map(String.init) ?? ""
    var out = v as NSString
    for m in re.matches(in: v, range: NSRange(location: 0, length: out.length)).reversed() {
        out = out.replacingCharacters(in: m.range, with: out.substring(with: m.range(at: 1)) == "!" ? last : word) as NSString
    }
    return out as String
}

final class Run: ObservableObject {
    let question: String
    let start = Date()
    @Published var status = "Connecting to \(host)"
    @Published var steps: [Step] = []
    @Published var live = ""
    @Published var stopping = false
    @Published var call = ""  // the latest tool call, Claude Code style: Bash(git status)
    @Published var thinking = false  // the status is a thinking phrase (Mulling…), not something the agent did
    @Published var plan: Plan?  // the agent's own task list, when it keeps one (TodoWrite, codex's todo list)
    var gotDone = false, checkIn = false, watch = false
    var meterStale = false  // Clear or a harness change mid-turn: its usage is the old conversation's, not the meter's
    var files: [ChatFile] = [], text = ""  // what it sent; a failed turn puts them back in the composer
    init(_ q: String) { question = q }
}

/// The agent's task list as chat.py's plan event has it: eta is seconds left at `at`, from this turn's own pace (nil without one).
struct Plan: Equatable {
    var done: Int, total: Int, title: String, eta: Double?, at: Date
}

/// Time left, coarse: never seconds (a pace from a few items isn't that precise), nothing once it's run out.
func timeLeft(_ s: Double) -> String? {
    guard s > 0 else { return nil }
    return s < 45 ? "under a minute left" : s < 90 ? "about a minute left" : s < 1200 ? "about \(Int((s / 60).rounded())) min left" : "over 20 min left"
}

/// The live byline under a run's status: the plan's progress, its time left, and what just finished (✓ or ✗ and its title).
/// lead (voice mode's stage, whose title is only a wait word): first what's happening now, the steps done when there's no plan,
/// and no time left (the stage only redraws on events).
/// Only what the agent's events say: no filler, no time left without a plan that's paced itself.
func byline(_ r: Run, now: Date, lead: Bool) -> String {
    let fin = r.steps.filter { $0.ok != nil }, failed = fin.filter { $0.ok == false }.count
    var parts = [String]()
    if lead {
        let plan = r.plan.flatMap { $0.title.isEmpty || $0.done >= $0.total ? nil : $0.title }
        parts.append(r.steps.last { $0.ok == nil }?.title ?? (r.thinking ? plan ?? "Thinking" : r.status))  // parallel calls: the newest still going
    }
    if let p = r.plan, p.total > 0 { parts.append("\(p.done) of \(p.total) tasks done") }
    else if lead && !fin.isEmpty { parts.append("\(fin.count) step\(fin.count == 1 ? "" : "s") done" + (failed > 0 ? ", \(failed) failed" : "")) }
    // the time left only where the clock redraws it (the card); the stage changes only on events, where it would go stale
    if !lead, let p = r.plan, let e = p.eta, let left = timeLeft(e - now.timeIntervalSince(p.at)) { parts.append(left) }
    if let l = fin.max(by: { ($0.end ?? $0.start) < ($1.end ?? $1.start) }), !(lead && parts.first == l.title) {
        parts.append("\(l.ok == true ? "✓" : "✗") \(l.title.count > 40 ? l.title.prefix(39) + "…" : l.title)")
    }
    return parts.filter { !$0.isEmpty }.joined(separator: " · ")
}

// MARK: verbose activity

/// One line of verbose activity (Settings › Chat & voice › Verbose activity).
struct FeedLine: Identifiable {
    enum Kind { case status, call, input, output, failed, think, said, agent, move, action }
    /// Who it's from, for the log's filter: Chief's own turn, the workspaces' agents, or Greenlight's moves and buttons.
    enum Src: String, CaseIterable { case chief = "Chief", agents = "Agents", greenlight = "Greenlight" }
    let id = UUID()
    let ts: Date, src: Src, who: String, kind: Kind
    var text: String
}

/// The verbose live log: what's actually happening, newest last. Chief's turn in full (every call's whole input and output, its
/// thinking), each workspace agent's state, tool and last words (from the 5s snapshot), and Greenlight's moves (a workspace's
/// lifecycle step moving, the dashboard's buttons). In memory only; Core fills it only while the setting is on.
final class Feed: ObservableObject {
    static let cap = 400, thinkCap = 4000
    @Published private(set) var lines: [FeedLine] = []
    @Published var only: FeedLine.Src? = nil  // the log's filter; nil: everything
    @Published var open = true  // the chat's log expanded, or just its header and newest line
    @Published var paused = false  // the log stops following the newest line, to read (no @State: see the top of the file)
    @Published var unfolded = Set<UUID>()  // long lines clicked open in full
    var shown: [FeedLine] { only.map { o in lines.filter { $0.src == o } } ?? lines }
    func add(_ src: FeedLine.Src, _ who: String, _ kind: FeedLine.Kind, _ text: String, at: Date = Date()) {
        if let l = lines.last, l.src == src, l.who == who, l.kind == kind {
            if kind == .think { lines[lines.count - 1].text = String((l.text + text).suffix(Self.thinkCap)); return }  // thinking streams in deltas: one line per stretch
            if kind == .status && l.text == text { return }
        }
        let t = kind == .think ? text : text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        lines.append(FeedLine(ts: at, src: src, who: who, kind: kind, text: t))
        if lines.count > Self.cap { lines.removeFirst(lines.count - Self.cap) }
    }
    func clear() { lines = []; unfolded = [] }
}

/// What the feed watches in one workspace from dash.py's snapshot: its agent, and its place in the lifecycle.
struct Watched: Equatable { var agent = "", type = "", tool = "", input = "", last = "", state = "", step = "", owner = "", action = "" }
func watchedOf(_ p: [String: Any]) -> Watched {
    let a = p["agent"] as? [String: Any] ?? [:], s = { (d: [String: Any], k: String) in d[k] as? String ?? "" }
    return Watched(agent: s(a, "state"), type: s(a, "type"), tool: s(a, "tool"), input: s(a, "input"), last: s(a, "last"),
                   state: s(p, "state"), step: s(p, "step"), owner: s(p, "owner"), action: s(p, "action"))
}
/// One snapshot against the last, as feed lines (src, who, kind, text). No last snapshot (launch, or the setting just turned on):
/// nothing, what's already so isn't news.
func feedDiff(_ old: [String: Watched]?, _ pucks: [[String: Any]]) -> (now: [String: Watched], lines: [(FeedLine.Src, String, FeedLine.Kind, String)]) {
    var now = [String: Watched]()
    for p in pucks { if let n = p["name"] as? String { now[n] = watchedOf(p) } }
    guard let old else { return (now, []) }
    var out = [(FeedLine.Src, String, FeedLine.Kind, String)]()
    let word = ["working": "working", "waiting": "waiting on you", "done": "finished", "idle": "idle", "none": "no agent"]
    for n in now.keys.sorted() {
        let w = now[n]!, agent = w.type.isEmpty ? "agent" : w.type
        guard let o = old[n] else {
            out.append((.greenlight, n, .move, "on the board: \(w.state), \(w.step) (waits on \(w.owner))" + (w.action.isEmpty ? "" : " · \(w.action)")))
            continue
        }
        if (o.state, o.step, o.owner) != (w.state, w.step, w.owner) {
            out.append((.greenlight, n, .move, "\(o.state) → \(w.state), \(o.step == w.step ? w.step : "\(o.step) → \(w.step)") (waits on \(w.owner))" + (w.action.isEmpty ? "" : " · \(w.action)")))
        }
        if o.agent != w.agent, !w.agent.isEmpty { out.append((.agents, n, .agent, "\(agent) \(word[w.agent] ?? w.agent)")) }
        if w.agent == "working", !w.tool.isEmpty, (o.tool, o.input) != (w.tool, w.input) { out.append((.agents, n, .call, "\(w.tool)(\(w.input))")) }
        if o.last != w.last, !w.last.isEmpty { out.append((.agents, n, .said, String(w.last.suffix(300)))) }
    }
    for n in old.keys.sorted() where now[n] == nil { out.append((.greenlight, n, .move, "off the board")) }
    return (now, out)
}

func fmt(_ t: TimeInterval) -> String {
    let s = Int(t.rounded())
    return s < 60 ? "\(s)s" : String(format: "%d:%02d", s / 60, s % 60)
}

func shellQuote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

func kfmt(_ n: Int) -> String { n >= 1_000_000 ? String(format: "%.1fM", Double(n) / 1e6) : n >= 1000 ? "\(n / 1000)k" : "\(n)" }

/// Greenlight's markdown → AttributedString, with bare URLs made clickable.
func md(_ s: String) -> AttributedString {
    let linked = s.replacingOccurrences(of: #"(?<![(<\[])(https?://[^\s)>\]]+)"#, with: "<$1>", options: .regularExpression)
    return (try? AttributedString(markdown: linked, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
}

// MARK: voice

/// Speech-to-text engines; the right-click menu's Voice submenu picks one.
let appleOnDevice = SFSpeechRecognizer()?.supportsOnDeviceRecognition ?? false  // transcribe() asks for on-device only when this holds
let engines = [("apple", appleOnDevice ? "Apple (on-device)" : "Apple (Apple servers)"), ("whisper", "Whisper (local whisper.cpp)"), ("openai", "OpenAI Whisper API")]
let voiceFile = FileManager.default.temporaryDirectory.appendingPathComponent("greenlight-voice.wav")
var recognizer: SFSpeechRecognizer?  // must outlive its task
func whisperBin() -> String { UserDefaults.standard.string(forKey: "whisperBin") ?? "/opt/homebrew/bin/whisper-cli" }
func whisperModel() -> String { UserDefaults.standard.string(forKey: "whisperModel") ?? support.appendingPathComponent("ggml-large-v3-turbo-q5_0.bin").path }

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
func forgetEnvironmentOpenAIKey() {
    envKeyQueue.async { envKeyLock.lock(); envKeyRead = (false, nil); envKeyStarted = false; envKeyLock.unlock(); DispatchQueue.main.async { (NSApp.delegate as? App)?.greenlight.objectWillChange.send() } }
}
/// The Keychain calls `keychain` makes; the selftest swaps in fakes.
struct KeychainOps {
    var copy: (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus = SecItemCopyMatching
    var update: (CFDictionary, CFDictionary) -> OSStatus = SecItemUpdate
    var add: (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus = SecItemAdd
    var delete: (CFDictionary) -> OSStatus = SecItemDelete
}
var keychainOps = KeychainOps()
private var keys: [String: String?] = [:]
private let keyLock = NSLock()

/// Transcribes a recorded file; done(text, error) runs on any thread, exactly once.
func transcribe(_ url: URL, engine: String, done: @escaping (String?, String?) -> Void) {
    let trim = { (s: String) in s.trimmingCharacters(in: .whitespacesAndNewlines) }
    switch engine {
    case "whisper":
        guard FileManager.default.isExecutableFile(atPath: whisperBin()) else {
            return done(nil, "whisper-cli not found at \(whisperBin()): `brew install whisper-cpp`, or pick another engine")
        }
        DispatchQueue.global().async {
            let p = Process(), out = Pipe()
            p.executableURL = URL(fileURLWithPath: whisperBin())
            p.arguments = ["-m", whisperModel(), "-f", url.path, "-nt", "-np"]
            p.standardOutput = out
            p.standardError = FileHandle.nullDevice
            do { try p.run() } catch { return done(nil, error.localizedDescription) }
            let text = trim(String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self))
            p.waitUntilExit()
            done(text, p.terminationStatus == 0 ? nil : "whisper-cli exited \(p.terminationStatus)")
        }
    case "openai":
        guard let key = openAIKey() else { return done(nil, "no OpenAI key: right-click the star → Voice → Set OpenAI key…, or export OPENAI_API_KEY and relaunch") }
        let b = UUID().uuidString
        var req = URLRequest(url: URL(string: "https://api.openai.com/v1/audio/transcriptions")!)
        req.httpMethod = "POST"
        req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        req.setValue("multipart/form-data; boundary=\(b)", forHTTPHeaderField: "Content-Type")
        let model = UserDefaults.standard.string(forKey: "openaiModel") ?? "whisper-1"
        var body = Data("--\(b)\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n\(model)\r\n".utf8)
        body += Data("--\(b)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n".utf8)
        body += (try? Data(contentsOf: url)) ?? Data()
        body += Data("\r\n--\(b)--\r\n".utf8)
        URLSession.shared.uploadTask(with: req, from: body) { data, _, err in
            let j = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            if let t = j?["text"] as? String { return done(trim(t), nil) }
            done(nil, err?.localizedDescription ?? (j?["error"] as? [String: Any])?["message"] as? String ?? "unexpected reply from OpenAI")
        }.resume()
    default:
        SFSpeechRecognizer.requestAuthorization { status in
            guard status == .authorized, let r = SFSpeechRecognizer(), r.isAvailable else {
                return done(nil, "speech recognition is off: System Settings → Privacy & Security → Speech Recognition")
            }
            recognizer = r
            let req = SFSpeechURLRecognitionRequest(url: url)
            req.requiresOnDeviceRecognition = r.supportsOnDeviceRecognition
            req.addsPunctuation = true
            var fired = false
            r.recognitionTask(with: req) { res, err in
                guard !fired else { return }
                if let res, res.isFinal { fired = true; done(trim(res.bestTranscription.formattedString), nil) }
                else if let err { fired = true; done(nil, err.localizedDescription) }
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
enum VoiceCmd: Equatable { case bye, stop, captions(Bool), reset }
func voiceCommand(_ heard: String) -> VoiceCmd? {
    let t = heard.lowercased().replacingOccurrences(of: "’", with: "'").replacingOccurrences(of: "[^a-z' ]", with: "", options: .regularExpression)
        .replacingOccurrences(of: #"\b(ok|okay|alright|all right|well|so|cool|great|thanks|thank you|chief|greenlight|for now|then|now)\b"#, with: " ", options: .regularExpression)
        .split(separator: " ").joined(separator: " ")
    switch t {
    case "that's all", "thats all", "that is all", "that's it", "thats it", "i'm done", "im done", "we're done", "were done", "stop listening",
         "goodbye", "good bye", "bye", "bye bye", "see you", "see ya", "see you later", "later", "talk to you later", "talk later", "catch you later",
         "good night", "goodnight", "end voice mode", "exit voice mode", "turn off voice mode", "voice off": return .bye
    case "stop", "never mind", "nevermind", "cancel", "stop that": return .stop
    case "reset", "reset context", "reset the context", "reset the chat", "clear", "clear context", "clear the context", "clear the chat",
         "start over", "start fresh", "fresh start", "new chat", "new conversation", "forget everything", "wipe it": return .reset
    case "captions on", "turn on captions", "show captions": return .captions(true)
    case "captions off", "turn off captions", "hide captions": return .captions(false)
    default: return nil
    }
}

/// While Chief speaks: you cutting in, not its own voice leaking back? 3+ of your latest words, mostly not ones it just said.
func isBargeIn(_ heard: String, _ spoken: String) -> Bool {
    let w = { (s: String) in s.lowercased().split { !$0.isLetter && $0 != "'" }.map(String.init) }
    let h = w(heard).suffix(6), said = Set(w(spoken))
    return h.count >= 3 && Double(h.filter { !said.contains($0) }.count) / Double(h.count) > 0.5
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

/// `asked`: the user asked to see some (asksToShow), so skip the "about particular workspaces?" gate; grounding still applies.
func pickHexes(_ reply: String, _ pucks: [[String: Any]], asked: Bool = false, done: @escaping ([String]) -> Void) {
    let named = namedHexes(reply, pucks)
    var seen = Set<String>()  // the same workspace twice (a stack's members, two hosts) would be a duplicate enum case
    let ws = pucks.compactMap { p -> (String, String)? in  // ponytail: the on-device context is ~4k tokens; 60 workspaces fit
        guard let n = p["name"] as? String, seen.insert(n).inserted else { return nil }
        let mr = p["mr"] as? [String: Any]
        return (n, [n, mr?["ref"] as? String, (mr?["title"] as? String).map { String($0.prefix(60)) }].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " | "))
    }.prefix(60)
    guard !ws.isEmpty, SystemLanguageModel.default.isAvailable else { return done(named) }
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
        await MainActor.run { done(out) }
    }
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
        Button { c.ctxOpen.toggle() } label: { label() }.buttonStyle(.plain).accessibilityLabel("Context, \(c.ctxSpoken)").modifier(CtxPop(c: c))
    }
    func clearButton<L: View>(@ViewBuilder _ label: () -> L) -> some View {
        Button(action: clear) { label() }.buttonStyle(.plain).help("Clear the context and start a new conversation (or say “reset”)").accessibilityLabel("Clear the context")
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
        }.frame(maxWidth: .infinity, maxHeight: .infinity).background(hx(l.page)).environment(\.colorScheme, l.dark ? .dark : .light)
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
        : ("Voice mode: talk with it hands-free", "Start voice mode")
}

/// The chat header's voice controls: voice mode on/off, and while on, the transcript and captions.
struct VoiceButtons: View, Looked {
    @ObservedObject var v: Voice
    var out = false  // the chat is in its own window
    var pop: (() -> Void)? = nil  // the chat header's pop-out (or put back), drawn as one more knob
    var body: some View {
        HStack(spacing: lk.id == "zen" || lk.id == "editorial" ? Theme.sp1 : Theme.sp2) {
            if v.on {
                knob(v.transcript ? "waveform" : "text.bubble", v.transcript ? "Show the stage" : "Show the transcript") { v.transcript.toggle() }
                knob(v.captions ? "captions.bubble.fill" : "captions.bubble", v.captions ? "Captions off" : "Captions on") { v.captions.toggle() }
            }
            // one style for live and standard chat
            knob("paintpalette", "Style: \(v.look.name), click for the next") { v.look = looks[((looks.firstIndex { $0.id == v.look.id } ?? 0) + 1) % looks.count] }
            let t = voiceButtonText(on: v.on, starting: v.starting)
            Button(action: v.toggle) { shell(Image(systemName: "waveform").font(sys(size: Theme.fs3, weight: .semibold)), on: v.on || v.starting).opacity(v.starting ? 0.6 : 1) }
                .buttonStyle(.plain).help(t.help).accessibilityLabel(t.label)
            if let pop { out ? knob("rectangle.righthalf.inset.filled", "Put back in the main window's right pane", label: "Put back in the main window", pop)
                : knob("arrow.up.forward.square", "Move to its own window (close it to bring it back)", label: "Move to its own window", pop) }
        }
    }
    func knob(_ icon: String, _ help: String, label: String? = nil, _ act: @escaping () -> Void) -> some View {
        Button(action: act) { shell(Image(systemName: icon)) }.buttonStyle(.plain).help(help).accessibilityLabel(label ?? help)
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

/// Voice mode. One audio engine: the mic, voice-processed so Chief's own voice is cancelled out of it, into on-device speech
/// recognition; Chief's speech out through a player node whose mix draws its waveform. The mic is on only while `on`.
final class Voice: NSObject, ObservableObject {
    weak var core: Core?
    @Published var on = false
    @Published var heard = ""  // your turn so far
    let meter = Meter()
    @Published var caption = ""  // the sentence Chief is saying
    @Published var look = looks.first { $0.id == ud.string(forKey: "stageStyle") } ?? looks[1] {  // default Orb
        didSet { ud.set(look.id, forKey: "stageStyle"); chatLook = look; core?.look += 1; stageWeb.appearance = NSAppearance(named: look.dark ? .darkAqua : .aqua); stageWeb.evaluateJavaScript("(s => { \(look.vars) })(document.documentElement.style)") }
    }
    @Published var captions = ud.object(forKey: "captions") as? Bool ?? true { didSet { ud.set(captions, forKey: "captions") } }
    @Published var transcript = false  // the chat instead of the stage
    @Published var speaking = 0  // speech buffers queued and not yet played
    @Published var fallback = ""  // why Apple's voice is standing in for the chosen one ("" when it isn't)
    var toldFallback = ""  // the reason the chat was last told, so each is said once
    var claimedAt = 0.0  // when this app's voice mode started, for voiceYields (another Greenlight starting later takes over)
    var bargeIn: Bool { ud.object(forKey: "voiceBargeIn") as? Bool ?? true }
    var pause: Double { ud.object(forKey: "voicePause") as? Double ?? 1.1 }  // seconds of quiet that end your turn; hardware and rooms differ
    var engine = AVAudioEngine()  // replaced by freshEngine() after a start that failed
    let player = AVAudioPlayerNode(), synth = AVSpeechSynthesizer()
    let speechFormat = AVAudioFormat(standardFormatWithSampleRate: 22050, channels: 1)!, micFormat = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
    let rec = SFSpeechRecognizer(locale: Locale(identifier: "en-US")), lock = NSLock()
    var request: SFSpeechAudioBufferRecognitionRequest?, task: SFSpeechRecognitionTask?, endTurn: DispatchWorkItem?
    var vp = false  // echo cancellation took; without it Chief can't be interrupted (it would hear itself)
    var waitWord = "One minute"
    var ending = false  // Chief said [voice: off]: stop once it's done speaking
    var muted = false  // you cut in or said stop: the rest of this reply stays unspoken
    var said = 0, lastSpoken = "", lastHeard = "", lines = [String](), gen = 0, synthConv: AVAudioConverter?, voiceConv: AVAudioConverter?
    var drawSeq = 0, drewAt = 0, drawing = false, chiefDrew = false, shown = false
    /// GPT-Live mode (Speech kind "live", fixed as voice mode starts): it listens, takes turns and speaks; Chief does the work.
    let live = LiveVoice()
    var isLive = false, liveUp = false
    var delegation: String?  // GPT-Live's latest hand-off: Chief's lines are read out under it
    var liveDrop = false  // you said stop: what it was still saying isn't played
    var quietFor = 0.0, newCaption = true, liveOut: AVAudioConverter?, toldLive = Set<String>()
    var liveBuf = [String](), awaiting: String?, liveSaid = "", liveSeq = 0  // lines not yet handed over; the cue of the last batch it hasn't begun
    static let best: AVSpeechSynthesisVoice? = {
        let en = AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix("en") }
        let rank = { (v: AVSpeechSynthesisVoice) in (v.quality.rawValue, v.language == "en-US" ? 1 : 0) }
        return en.first { $0.identifier == ud.string(forKey: "voiceName") } ?? en.max { rank($0) < rank($1) } ?? AVSpeechSynthesisVoice(language: "en-US")
    }()

    lazy var stageWeb: WKWebView = {  // no message handlers: nothing on the stage can reach the app
        let v = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        v.setValue(false, forKey: "drawsBackground")
        v.appearance = NSAppearance(named: look.dark ? .darkAqua : .aqua)  // the look's, not the system's
        if let u = Bundle.main.url(forResource: "stage", withExtension: "html") { v.loadFileURL(u, allowingReadAccessTo: u) }
        return v
    }()
    func js(_ f: String, _ args: [Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: args), let a = String(data: d, encoding: .utf8) else { return }
        stageWeb.evaluateJavaScript("(s => { \(delegate.themeVars()) \(look.vars) })(document.documentElement.style); \(f)(...\(a))")
    }
    func wait(_ title: String, _ sub: String) { js("stage.wait", [title, sub]) }
    func show(_ spec: [String: Any]) {  // a card the app drew (the workspace you switched to): newer than any drawing on its way
        guard on else { return }
        drawSeq += 1; shown = true; js("stage.spec", [spec])
    }

    override init() {
        super.init()
        // Another Greenlight (Greenlight.app beside a dev copy, which shares its shortcut) started voice mode: one listens and speaks,
        // or each hears you, asks Chief on its own and says its own answer over the other's (one in Apple's voice when its Keychain says no)
        DistributedNotificationCenter.default().addObserver(forName: voiceClaim, object: nil, queue: .main) { [weak self] n in
            guard let self, self.on, let at = n.userInfo?["at"] as? Double, let pid = n.userInfo?["pid"] as? Int,
                  voiceYields(mine: (self.claimedAt, Int(getpid())), theirs: (at, pid)) else { return }
            self.stopAll()
            self.core?.msgs.append(Msg(.note, "Voice mode moved to \(n.object as? String ?? "another Greenlight"), so this one stopped listening: two would both answer you, over each other."))
        }
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main) { [weak self] n in
            guard let self, self.on, n.object as AnyObject? === self.engine else { return }  // a headset in or out: the engine stopped itself
            self.restart()
        }
    }
    /// The engine stopped itself (a headset in or out): start it again. If it won't, voice mode stops and says so, and the next start
    /// gets a new engine. Not a new one now: play() reads the engine off the main thread while voice mode is on.
    func restart() {
        do { try startEngine(); listen() }  // listen() is a no-op on GPT-Live, whose socket stays up
        catch {
            NSLog("voice: audio restart failed: %@", "\(error)")
            core?.msgs.append(Msg(.note, "Voice mode stopped: the audio didn't restart after the devices changed (\(error.localizedDescription)). Start it again to pick it up."))
            stopAll()
            staleEngine = true
        }
    }

    func toggle() { on || starting ? stopAll() : start() }  // a press while it's still starting cancels it
    @Published var starting = false  // a start (and its retries) under way: the voice button shows it, and a press cancels it
    var startGen = 0  // stopAll() bumps it, which ends a start still under way
    var staleEngine = false  // its restart failed: the next start begins on a new engine
    func start() {
        guard !on, !starting else { return }
        starting = true; startGen += 1
        start(attempt: 0, gen: startGen)
    }
    /// Waits before each retry of a start that failed: about 3 s in all, then the error is shown.
    static let startRetries = [0.3, 0.8, 1.5]
    func start(attempt: Int, gen g: Int) {
        guard g == startGen else { return }  // cancelled
        guard !on, core?.recording == nil else { starting = false; return }  // dictation has the mic
        let live = Speech.shared.kind == "live"  // GPT-Live hears you itself: Speech Recognition isn't needed
        AVCaptureDevice.requestAccess(for: .audio) { ok in SFSpeechRecognizer.requestAuthorization { st in DispatchQueue.main.async {
            guard g == self.startGen else { return }
            guard ok, st == .authorized || live else {
                self.starting = false
                self.core?.msgs.append(Msg(.note, "Voice mode needs Microphone and Speech Recognition: System Settings → Privacy & Security → Greenlight."))
                return
            }
            self.isLive = live
            LivePreview.shared.stop()  // a voice preview would talk over voice mode
            if self.staleEngine { self.freshEngine(); self.staleEngine = false }
            do { try self.startEngine() } catch {
                // The first start often fails with AVAudio -10875 (kAudioUnitErr_FailedInitialization): the voice-processing unit
                // didn't initialize. Retrying the same engine fails the same way, because enabling voice processing again does nothing
                // and the failed unit stays. A second click worked because stopping turned voice processing off. So each retry gets a
                // new engine with its own voice-processing unit, and one click is enough.
                NSLog("voice: audio start %d failed: %@", attempt + 1, "\(error)")
                self.freshEngine()
                if attempt < Self.startRetries.count && (error as NSError).domain != "Voice" {  // not when there's no mic: no retry finds one
                    return DispatchQueue.main.asyncAfter(deadline: .now() + Self.startRetries[attempt]) { self.start(attempt: attempt + 1, gen: g) }
                }
                self.core?.msgs.append(Msg(.note, "Voice mode couldn't start the audio: \(error.localizedDescription)"))
                return self.stopAll()
            }
            if attempt > 0 { NSLog("voice: audio started on try %d", attempt + 1) }
            self.starting = false
            self.on = true
            self.fallback = ""; self.toldFallback = ""
            self.claimedAt = Date().timeIntervalSince1970
            DistributedNotificationCenter.default().postNotificationName(voiceClaim, object: Bundle.main.bundleIdentifier ?? ProcessInfo.processInfo.processName,
                                                                         userInfo: ["at": self.claimedAt, "pid": Int(getpid())], deliverImmediately: true)
            Speech.shared.warm()  // a fresh try at a voice that failed last time
            if live { return self.openLive() }
            self.listen()
            self.wait("Listening", "Say something, or “goodbye” to stop.")
        }}}
    }

    /// Drops an engine whose start failed, voice processing and all, for a new one. The player moves to the new engine.
    /// Only while voice mode is off: play() reads `engine` off the main thread, and bails out first when it's off.
    func freshEngine() {
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        engine.mainMixerNode.removeTap(onBus: 0)
        if vp { try? engine.inputNode.setVoiceProcessingEnabled(false); vp = false }
        player.stop()
        if engine.attachedNodes.contains(player) { engine.detach(player) }
        engine = AVAudioEngine()
    }

    func startEngine() throws {
        engine.stop()
        let input = engine.inputNode
        vp = (try? input.setVoiceProcessingEnabled(true)) != nil
        if !engine.attachedNodes.contains(player) { engine.attach(player) }
        try engine.link(player, to: engine.mainMixerNode, format: speechFormat)
        let inFormat = input.outputFormat(forBus: 0)  // after voice processing, which can change it
        guard inFormat.sampleRate > 0, inFormat.channelCount > 0 else {  // no input device: installTap would raise, not throw
            throw NSError(domain: "Voice", code: 1, userInfo: [NSLocalizedDescriptionKey: "No microphone is connected."])
        }
        let conv = AVAudioConverter(from: inFormat, to: micFormat), lconv = isLive ? AVAudioConverter(from: inFormat, to: live.fmt) : nil
        if inFormat.channelCount > 1 { conv?.channelMap = [0]; lconv?.channelMap = [0] }
        input.removeTap(onBus: 0)
        try input.tap(onBus: 0, bufferSize: 1024, format: inFormat) { [weak self] b, _ in
            guard let self, let conv, let m = convert(b, conv, self.micFormat) else { return }
            if let lconv { convert(b, lconv, self.live.fmt).map(self.live.mic) }  // GPT-Live: the mic goes to it, not to Apple's recognizer
            self.lock.lock(); let r = self.request; self.lock.unlock()
            r?.append(m)
            let l = level(m)
            DispatchQueue.main.async { self.meter.you = Array(self.meter.you.dropFirst()) + [l] }
        }
        engine.mainMixerNode.removeTap(onBus: 0)
        try engine.mainMixerNode.tap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] b, _ in
            let l = level(b)
            DispatchQueue.main.async { guard let self else { return }; self.meter.chief = Array(self.meter.chief.dropFirst()) + [self.speaking > 0 ? l : 0] }
        }
        engine.prepare()
        try engine.start()
    }

    /// A fresh recognition request: after each of your turns, after Chief speaks (its echo is in the old one), after an error.
    func listen() {
        guard !isLive else { return }
        task?.cancel()
        let r = SFSpeechAudioBufferRecognitionRequest()
        r.shouldReportPartialResults = true
        if rec?.supportsOnDeviceRecognition == true { r.requiresOnDeviceRecognition = true }
        lock.lock(); request = r; lock.unlock()
        heard = ""
        task = rec?.recognitionTask(with: r) { [weak self] res, err in DispatchQueue.main.async {
            guard let self, self.on, self.request === r else { return }
            if let res { self.partial(res.bestTranscription.formattedString) }
            guard err != nil || res?.isFinal == true else { return }
            if !self.heard.isEmpty { self.turn() }
            else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { if self.on && self.request === r { self.listen() } } }  // e.g. no speech yet
        }}
    }

    func partial(_ t: String) {
        if speaking > 0 {  // Chief is talking: its echo, or you cutting in
            guard vp && bargeIn && (isBargeIn(t, lastSpoken) || voiceCommand(t) != nil) else { return }
            hush()
        }
        heard = t
        endTurn?.cancel()
        let w = DispatchWorkItem { [weak self] in self?.turn() }
        endTurn = w
        DispatchQueue.main.asyncAfter(deadline: .now() + pause, execute: w)  // ponytail: silence ends your turn; a turn-taking model if it cuts you off
    }

    func turn() {
        endTurn?.cancel()
        let t = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        listen()
        guard !t.isEmpty, let c = core else { return }
        if let cmd = voiceCommand(t) { return run(cmd) }
        lastHeard = t
        caption = ""
        c.send(t)  // send() adds voiceNote
    }

    func run(_ cmd: VoiceCmd) {
        switch cmd {
        case .bye: stopAll()
        case .stop: hush(); core?.stop()
        case .captions(let b): captions = b
        case .reset: hush(); core?.clear()
        }
    }

    // Core's turn, from start to finish

    func began(_ r: Run) {
        said = 0; drewAt = 0; chiefDrew = false; shown = false; muted = false; ending = false
        waitWord = waitWords.randomElement()!
        guard on else { return }
        wait(waitWord, r.question)
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self, self.core?.run === r, self.said == 0, self.speaking == 0 else { return }
            self.say(self.waitWord, filler: true)
        }
    }
    func status(_ s: String) { if on && !shown { wait(waitWord, s) } }
    func live(_ r: Run) {
        guard on else { return }
        let st = staged(r.live), p = plain(st.text)
        if let h = st.stage { stage(h) }
        let (ss, to) = sentences(p, from: said)
        said = to
        ss.forEach { say($0) }
        if !chiefDrew && !drawing && p.count - drewAt > 300 { drewAt = p.count; draw(p, last: false) }
    }
    func finished(_ st: Staged, _ body: String) {
        guard on else { return }
        if let h = st.stage { stage(h) }
        let p = plain(body) + "\n"
        sentences(p, from: min(said, p.count)).said.forEach { say($0) }
        said = 0
        if st.end && isLive {  // its goodbye was only just handed over: stop once it has said it (or soon, if it never does)
            ending = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in if let self, self.on, self.ending { self.stopAll() } }
        } else if st.end { ending = true; if speaking == 0 && lines.isEmpty { stopAll() } }
        if !chiefDrew { draw(plain(body), last: true) }
    }
    /// The turn was stopped or failed: back to listening, saying why (unless you said stop).
    func failed(_ text: String) {
        guard on else { return }
        said = 0
        wait("Listening", plain(text))
        say(text)
    }
    /// Chief's own ```stage block: HTML as is, notes drawn by Haiku. It outranks Haiku's drawing until the next turn.
    func stage(_ h: String) {
        guard !chiefDrew else { return }
        chiefDrew = true; shown = true
        drawSeq += 1
        let j = h.replacingOccurrences(of: #"^(json|chart)\s*"#, with: "", options: .regularExpression)  // ```stage json
        if j.hasPrefix("{"), let o = try? JSONSerialization.jsonObject(with: Data(j.utf8)) as? [String: Any], o["chart"] != nil { js("stage.chart", [o]) }
        else if h.hasPrefix("<") { js("stage.html", [h]) } else { draw(h, last: true, force: true) }
    }
    func draw(_ text: String, last: Bool, force: Bool = false) {
        drawSeq += 1
        let n = drawSeq
        drawing = true
        drawSpec(lastHeard, text) { [weak self] spec in
            guard let self, n == self.drawSeq else { return }  // a newer drawing was asked for
            self.drawing = false
            guard self.on, force || !self.chiefDrew else { return }
            if let spec { self.shown = true; self.js("stage.spec", [spec]) }
            else if last { self.shown = true; self.js("stage.spec", [fallbackSpec(text)]) }  // mid-reply, a failure leaves the card up
        }
    }

    // speech

    /// One line to the player: the voice's own audio if there is one, else Apple's.
    /// A filler line (the wait word) is dropped rather than said in Apple's voice when the chosen voice can't make it: one voice per turn.
    func say(_ text: String, filler: Bool = false) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard on, !muted, !t.isEmpty, t.rangeOfCharacter(from: .alphanumerics) != nil else { return }
        lastSpoken = String((lastSpoken + " " + t).suffix(600))
        if isLive {  // GPT-Live acknowledges by itself (no filler); without a hand-off (a typed message) it can't speak, and Apple's voice doesn't stand in
            guard !filler, delegation != nil else { return }
            liveDrop = false
            liveBuf.append(t)
            return DispatchQueue.main.async { self.flushLive() }  // the lines of one burst (a finished reply) go as one
        }
        lines.append(t)
        if lines.count == 1 { caption = t }
        let g = gen
        guard Speech.shared.kind != "apple" else { return apple(t, g) }
        Speech.shared.q.async { [weak self] in  // serial: lines play in order
            guard let self, self.gen == g else { return }
            let b = Speech.shared.pcm(t)
            if self.voiceConv?.inputFormat != b?.format { self.voiceConv = b.flatMap { AVAudioConverter(from: $0.format, to: self.speechFormat) } }
            guard let b, let out = self.voiceConv.flatMap({ convert(b, $0, self.speechFormat, whole: true) }) else {
                let why = b == nil ? Speech.shared.status : "Couldn't convert the voice's audio"
                return DispatchQueue.main.async { self.fellBack(why); filler ? self.play(nil, end: true, g) : self.apple(t, g) }
            }
            DispatchQueue.main.async { self.fallback = "" }
            self.play(out, end: false, g); self.play(nil, end: true, g)
        }
    }
    /// The chosen voice couldn't say a line, so Apple's says it: why, under the stage while it speaks, and once per reason in the chat (not every line).
    func fellBack(_ why: String) {
        let name = Speech.kinds.first { $0.0 == Speech.shared.kind }?.1 ?? "The chosen voice"
        fallback = why.isEmpty ? "\(name) failed" : why
        guard fallback != toldFallback else { return }
        toldFallback = fallback
        core?.msgs.append(Msg(.note, "Speaking in Apple's voice instead of \(name): \(fallback). Settings → Chat & voice → Test checks it."))
    }
    func apple(_ t: String, _ g: Int) {
        let u = AVSpeechUtterance(string: t)
        u.voice = Voice.best
        var over = false
        synth.write(u) { [weak self] buf in
            guard let self, let b = buf as? AVAudioPCMBuffer, !over else { return }
            if b.frameLength == 0 { over = true; return self.play(nil, end: true, g) }  // the utterance is over (macOS 26 sends this twice: a second end would drop the next line's caption)
            if self.synthConv?.inputFormat != b.format { self.synthConv = AVAudioConverter(from: b.format, to: self.speechFormat) }
            self.synthConv.flatMap { convert(b, $0, self.speechFormat) }.map { self.play($0, end: false, g) }
        }
    }
    /// end: a silent frame marks the line's end in the player.
    func play(_ buf: AVAudioPCMBuffer?, end: Bool, _ g: Int) {
        guard gen == g, on else { return }
        var out = buf
        if end { out = AVAudioPCMBuffer(pcmFormat: speechFormat, frameCapacity: 1); out?.frameLength = 1 }
        guard let out else { return }
        DispatchQueue.main.async { if self.gen == g { self.speaking += 1 } }
        self.player.scheduleBuffer(out, completionCallbackType: .dataPlayedBack) { _ in DispatchQueue.main.async {
            guard self.gen == g else { return }
            self.speaking = max(0, self.speaking - 1)
            if end { if !self.lines.isEmpty { self.lines.removeFirst() }; self.caption = self.lines.first ?? self.caption }
            if self.speaking == 0 && self.lines.isEmpty && self.on { self.ending ? self.stopAll() : self.listen() }  // Chief's echo is in that request
        }}
        if !self.player.isPlaying && self.engine.isRunning { try? self.player.start() }  // play() raises on a stopped engine
    }
    /// Stop talking (you cut in, or said stop); the turn itself goes on.
    func hush() {
        gen += 1; muted = true
        if isLive { liveDrop = true; liveBuf = []; awaiting = nil }
        synth.stopSpeaking(at: .immediate)
        player.stop()
        speaking = 0; lines = []
    }

    // GPT-Live

    func openLive() {
        guard let key = openAIKey() else { return liveFailed("needs an OpenAI key: Settings → Chat & voice → OpenAI key, or export OPENAI_API_KEY and relaunch") }
        toldLive = []; newCaption = true; quietFor = 0
        wait("Connecting", "Starting a GPT-Live session…")
        live.open(key) { [weak self] e in self?.onLive(e) }
    }
    func onLive(_ e: LiveEvent) {
        guard on, isLive else { return }
        switch e {
        case .started: liveUp = true; wait("Listening", "Say something, or “goodbye” to stop.")
        case .heard(let d): heard = heard.isEmpty ? String(d.drop { $0.isWhitespace }) : heard + d
        case .said(let d):
            if let c = awaiting { liveSaid += d; if cueHeard(c, liveSaid) { awaiting = nil; flushLive() } }
            guard !liveDrop else { return }
            if newCaption { caption = ""; newCaption = false }
            caption = String(liveCaption(caption.isEmpty ? String(d.drop { $0.isWhitespace }) : caption + d).suffix(240))
        case .audio(let d): liveAudio(d)
        case .delegated(let id):  // its transcript of your turn can trail the hand-off a little
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.handOff(id) }
        case .failed(let code, let m):
            NSLog("GPT-Live: %@ %@", code, m)
            guard liveUp else { return liveFailed("refused the session: \(m)") }  // e.g. a voice it doesn't have
            if toldLive.insert(code).inserted { core?.msgs.append(Msg(.note, "GPT-Live: \(m)")) }
        case .closed(let why): liveFailed(why == "close_requested" ? "closed the session" : why == "expired" ? "ended the session (it ran out of time)" : why)
        case .other: break
        }
    }
    /// GPT-Live handed your turn off: Chief gets it (or voice mode does, for a command), and Chief's answer is read under this hand-off.
    func handOff(_ id: String) {
        guard on, isLive, let c = core else { return }
        let t = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        heard = ""; delegation = id; liveDrop = false
        guard !t.isEmpty else { return }
        if let cmd = voiceCommand(t) { return run(cmd) }
        if c.run != nil { muted = true; liveBuf = [] }  // a new request mid-answer: the rest of the old one goes unsaid (send() queues the new one)
        lastHeard = t
        c.send(t)  // send() adds voiceNote
    }
    /// Hand over the waiting lines, unless it hasn't begun the last ones (then once it has, or after 8 s).
    func flushLive() {
        guard on, isLive, awaiting == nil, !liveBuf.isEmpty, let d = delegation else { return }
        let text = liveBuf.joined(separator: " ")
        liveBuf = []; awaiting = liveCue(text); liveSaid = ""; liveSeq += 1
        live.send(liveRead(d, text))
        let n = liveSeq
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in if let self, self.liveSeq == n, self.awaiting != nil { self.awaiting = nil; self.flushLive() } }
    }
    /// Its audio streams all the time, silence between turns: play from the first loud piece until 0.8 s of quiet.
    func liveAudio(_ d: Data) {
        quietFor = audible(d) ? 0 : quietFor + Double(d.count) / 48000
        guard !liveDrop, on else { if speaking > 0 { speaking = 0; newCaption = true }; return }
        if speaking == 0 {
            guard quietFor == 0 else { return }
            speaking = 1
            let cushion = AVAudioPCMBuffer(pcmFormat: speechFormat, frameCapacity: 2646)!  // 120 ms of silence: room for network jitter
            cushion.frameLength = 2646
            player.scheduleBuffer(cushion)
        }
        if liveOut == nil { liveOut = AVAudioConverter(from: live.fmt, to: speechFormat) }
        if let b = Speech.shared.buffer(d), let c = liveOut, let out = convert(b, c, speechFormat) { player.scheduleBuffer(out) }
        if !player.isPlaying && engine.isRunning { try? player.start() }
        if quietFor > 0.8 {
            speaking = 0; newCaption = true
            if ending && awaiting == nil && liveBuf.isEmpty { stopAll() }  // it has said the goodbye, not just its own "okay"
        }
    }
    /// GPT-Live can't go on: say why and end voice mode (no other voice takes over, so two never talk at once).
    func liveFailed(_ why: String) {
        guard on else { return }
        core?.msgs.append(Msg(.note, "Voice mode stopped: GPT-Live \(why). Settings → Chat & voice → GPT-Live voice → ▶ checks it."))
        stopAll()
    }

    func stopAll() {
        startGen += 1; starting = false  // and any start still retrying
        hush()
        live.close(); liveUp = false; delegation = nil; liveOut = nil
        endTurn?.cancel()
        task?.cancel(); task = nil
        lock.lock(); request = nil; lock.unlock()
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        engine.mainMixerNode.removeTap(onBus: 0)
        if vp { try? engine.inputNode.setVoiceProcessingEnabled(false); vp = false }
        on = false; transcript = false; ending = false
        heard = ""; caption = ""
        meter.you = meter.you.map { _ in 0 }; meter.chief = meter.chief.map { _ in 0 }
        LivePreview.shared.voiceModeEnded()
    }
}

/// Voice mode's claim on the mic, posted to every Greenlight on this Mac as it starts.
let voiceClaim = Notification.Name("dev.greenlight.voice.claim")
/// Whether this app's voice mode gives way to another's claim: the later start keeps it (the pid breaks a tie), so one shortcut press
/// that starts two apps leaves exactly one on.
func voiceYields(mine: (at: Double, pid: Int), theirs: (at: Double, pid: Int)) -> Bool { theirs.pid != mine.pid && (theirs.at, theirs.pid) > (mine.at, mine.pid) }

/// A voice better than AVSpeechSynthesizer: Kokoro on this Mac (app/kokoro.py under uv) or ElevenLabs Flash.
/// pcm() blocks, on q; nil means Apple's voice says that line (not ready yet, no key, a failure).
final class Speech {
    static let shared = Speech()
    static let kinds = [("apple", "Apple"), ("kokoro", "Kokoro (on this Mac)"), ("eleven", "ElevenLabs Flash"), ("live", "OpenAI GPT-Live 1.1")]
    init() { Speech.switchToLive(ud) }
    static let kokoroVoices = ["af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_michael", "am_fenrir", "am_puck", "bf_emma", "bm_george", "bm_fable"]
    let q = DispatchQueue(label: "speech"), fmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 24000, channels: 1, interleaved: true)!
    var kind: String { ud.string(forKey: "speech") ?? "apple" }
    var kokoroVoice: String { ud.string(forKey: "kokoroVoice") ?? "af_heart" }
    var elevenVoice: String { ud.string(forKey: "elevenVoice").flatMap { $0.isEmpty ? nil : $0 } ?? "EXAVITQu4vr4xnSDxMaL" }  // "Sarah", a premade voice
    var proc: Process?, kin: FileHandle?, kout: FileHandle?
    var ready = false  // Kokoro has loaded (the first start installs ~1 GB through uv)
    var status = ""  // why the last line wasn't in the chosen voice, "" when it was; set on q
    /// A failure that won't fix itself (no key, the key refused, no such voice, no credits): later lines skip ElevenLabs,
    /// instead of each waiting on the same error, until warm() (voice mode starting, a new key, voice or engine).
    var stuck: String?

    func pcm(_ text: String) -> AVAudioPCMBuffer? { data(text).flatMap(buffer) }
    func data(_ text: String) -> Data? {
        switch kind {
        case "kokoro": if ready { return kokoro(text) }; if status.isEmpty { status = "Kokoro is still loading" }; return nil
        case "eleven": return eleven(text)
        case "live": status = "GPT-Live starts with voice mode: turn voice mode off and on"; return nil  // picked mid-conversation
        default: return nil
        }
    }
    func buffer(_ d: Data) -> AVAudioPCMBuffer? {
        guard d.count > 1, let b = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(d.count / 2)) else { return nil }
        b.frameLength = b.frameCapacity
        d.withUnsafeBytes { _ = memcpy(b.int16ChannelData![0], $0.baseAddress!, Int(b.frameLength) * 2) }
        return b
    }

    func eleven(_ text: String) -> Data? {
        if let stuck { status = stuck; return nil }
        let fail = { (why: String, hard: Bool) -> Data? in
            NSLog("ElevenLabs: %@", why)
            self.status = why; if hard { self.stuck = why }
            return nil
        }
        guard let key = keychain("elevenlabs"), !key.isEmpty else {
            return fail("No ElevenLabs key, or the Keychain didn't let Greenlight read it: Set key… again", true)
        }
        let id = elevenVoice.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        guard var u = URLComponents(string: "https://api.elevenlabs.io/v1/text-to-speech/\(id)") else { return fail("ElevenLabs voice ID “\(elevenVoice)” isn't valid", true) }
        u.queryItems = [URLQueryItem(name: "output_format", value: "pcm_24000")]
        var r = URLRequest(url: u.url!, timeoutInterval: 10)
        r.httpMethod = "POST"
        r.setValue(key, forHTTPHeaderField: "xi-api-key")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text, "model_id": "eleven_flash_v2_5"])
        var got: Data?, code = 0, body: Data?, err: Error?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: r) { d, resp, e in
            code = (resp as? HTTPURLResponse)?.statusCode ?? 0; body = d; err = e
            if code == 200, let d, d.count > 1 { got = d }
            sem.signal()
        }.resume()
        sem.wait()
        if got != nil { status = ""; return got }
        let (why, hard) = Speech.elevenWhy(code, body, err, voice: elevenVoice)
        return fail(why, hard)
    }
    /// ElevenLabs' failure in words, and whether it is hard (retrying the next line would only fail again).
    static func elevenWhy(_ code: Int, _ body: Data?, _ err: Error?, voice: String) -> (String, Bool) {
        if let e = err as? URLError {
            return (e.code == .timedOut ? "ElevenLabs didn't answer in time" : "Can't reach ElevenLabs (\(e.localizedDescription)); a firewall like Little Snitch may be blocking api.elevenlabs.io", false)
        }
        let j = body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let detail = j?["detail"] as? [String: Any]
        let said = ((detail?["message"] as? String) ?? (j?["detail"] as? String) ?? body.flatMap { String(data: $0, encoding: .utf8) } ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines).prefix(160)
        let tail = said.isEmpty ? "" : ": \(said)"
        switch code {
        case 401: return ("ElevenLabs refused the key (401\(tail))", true)
        case 402: return ("ElevenLabs needs a paid plan or more credits (402\(tail))", true)
        case 404: return ("ElevenLabs has no voice “\(voice)” (404\(tail))", true)
        case 429: return ("ElevenLabs is busy or over its rate limit (429\(tail))", false)
        case 200: return ("ElevenLabs sent no audio", false)
        case 0: return ("ElevenLabs didn't answer", false)
        default: return ("ElevenLabs failed (\(code)\(tail))", code >= 400 && code < 500)
        }
    }

    /// Settings' Test: one line in the chosen voice, played now (voice mode needn't be on). done(nil) when it played, else why.
    var tester: AVAudioPlayer?
    func test(_ line: String, _ done: @escaping (String?) -> Void) {
        q.async {
            self.stuck = nil; self.status = ""
            let d = self.data(line)
            DispatchQueue.main.async {
                guard let d else { return done(self.status.isEmpty ? "No audio" : self.status) }
                self.tester = try? AVAudioPlayer(data: wav(d, rate: 24000))
                done(self.tester?.play() == true ? nil : "Got audio but couldn't play it")
            }
        }
    }

    func kokoro(_ text: String) -> Data? {
        guard let kin, let kout, let line = try? JSONSerialization.data(withJSONObject: ["text": text, "voice": kokoroVoice]) else { return nil }
        do {
            try kin.write(contentsOf: line + Data("\n".utf8))
            guard let n = try kout.read(upToCount: 4), n.count == 4 else { throw CocoaError(.fileReadUnknown) }
            let len = n.withUnsafeBytes { Int(UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self))) }
            return len == 0 ? nil : try kout.read(upToCount: len)
        } catch {  // it died: restart it once it had been working (a failed start stays failed, no respawn loop)
            let was = ready; ready = false; status = was ? "Kokoro stopped, restarting" : "Kokoro didn't start"; proc?.terminate(); proc = nil
            if was { DispatchQueue.main.async { self.warm() } }
            return nil
        }
    }

    /// Start Kokoro if it's the voice (at launch, on picking it, and as voice mode starts); a warm-up line loads the model before the first real one.
    /// It also clears a stuck ElevenLabs failure: you may have fixed it (a new key, voice ID or plan).
    func warm() {
        q.async { if self.stuck != nil { self.stuck = nil; self.status = "" } }
        if kind == "live" { q.async { _ = openAIKey() } }
        if kind == "eleven" { q.async { _ = keychain("elevenlabs") } }  // any Keychain prompt at launch, not with the first line (which would go to Apple's voice)
        guard kind == "kokoro", proc == nil else { return }
        let uv = ["/opt/homebrew/bin/uv", "/usr/local/bin/uv", NSHomeDirectory() + "/.local/bin/uv"].first { FileManager.default.isExecutableFile(atPath: $0) }
        guard let uv, let script = Bundle.main.path(forResource: "kokoro", ofType: "py") else { status = "Kokoro needs uv (brew install uv)"; return }
        let p = Process(), i = Pipe(), o = Pipe()
        p.executableURL = URL(fileURLWithPath: uv)
        p.arguments = ["run", "-q", script]
        p.standardInput = i; p.standardOutput = o
        let log = NSHomeDirectory() + "/Library/Logs/Greenlight-kokoro.log"
        FileManager.default.createFile(atPath: log, contents: nil)
        p.standardError = FileHandle(forWritingAtPath: log)
        status = "Kokoro is loading (the first time downloads about 1 GB)"
        do { try p.run() } catch { status = "Kokoro: \(error.localizedDescription)"; return }
        proc = p; kin = i.fileHandleForWriting; kout = o.fileHandleForReading
        DispatchQueue.global().async {
            let ok = self.kokoro("Ready.") != nil
            self.q.async { self.ready = ok; self.status = ok ? "" : "Kokoro didn't start" }
        }
    }
    func stop() { proc?.terminate(); proc = nil; ready = false }

    /// Once, ElevenLabs users move to GPT-Live (what they asked for); picking ElevenLabs again afterwards sticks.
    static func switchToLive(_ d: UserDefaults) {
        guard !d.bool(forKey: "liveSwitched") else { return }
        if d.string(forKey: "speech") == "eleven" { d.set("live", forKey: "speech") }
        d.set(true, forKey: "liveSwitched")
    }
}

// MARK: GPT-Live: OpenAI's full-duplex voice model holds voice mode's conversation; Chief does the work
//
// gpt-live-1.1 (wss://api.openai.com/v1/live/sessions) isn't a text-to-speech service: it speaks only in a conversation, after it hears
// you. So in this mode it gets the mic (voice-processed, so its own voice is cancelled out), takes the turns, and hands each request
// off (a delegation); Greenlight sends your words to Chief and gives Chief's sentences back under that hand-off to be read exactly.
// Measured 2026-11-20: text with no hand-off is refused ("Missing required parameter: 'delegation_id'"), lines appended while it
// speaks queue in order, and its audio is a continuous 24 kHz stream with silence between turns (no "done speaking" event).

/// What a GPT-Live server event means to voice mode.
enum LiveEvent: Equatable { case started, heard(String), said(String), audio(Data), delegated(String), closed(String), failed(String, String), other }
func liveEvent(_ d: Data) -> LiveEvent {
    guard let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let t = j["type"] as? String else { return .other }
    switch t {
    case "session.started": return .started
    case "session.input_transcript.delta": return .heard(j["delta"] as? String ?? "")
    case "session.output_transcript.delta": return .said(j["delta"] as? String ?? "")
    case "session.output_audio.delta": return (j["delta"] as? String).flatMap { Data(base64Encoded: $0) }.map { .audio($0) } ?? .other
    case "session.delegation.created": return ((j["delegation"] as? [String: Any])?["id"] as? String).map { .delegated($0) } ?? .other
    case "session.closed": return .closed(j["reason"] as? String ?? "")
    case "error": let e = j["error"] as? [String: Any]; return .failed(e?["code"] as? String ?? "", e?["message"] as? String ?? "")
    default: return .other
    }
}
let liveRule = "You are the voice of Chief, an assistant whose answers come from a backend that can see the user's work. "
    + "Delegate every request the user makes to the backend, including small talk, goodbyes, and asking you to stop or to reset: "
    + "never answer from your own knowledge. While you wait, at most a few words of acknowledgement. "
    + "When given text in <say> tags, read it aloud exactly, word for word, with nothing added, then stop. Stop speaking when the user interrupts."
func liveStart(voice: String, instructions: String = liveRule) -> [String: Any] {
    ["type": "session.start", "session": ["model": LiveVoice.model, "instructions": instructions,
                                          "audio": ["format": ["type": "audio/pcm", "rate": 24000], "output": ["voice": voice]]]]
}
/// Chief's lines, to be read exactly under the hand-off they answer (the prompting guide's read-aloud wording, plus: "(exit 2)" was
/// read as "open parenthesis exit two close parenthesis" without the last sentence).
func liveRead(_ delegation: String, _ text: String) -> [String: Any] {
    let t = text.replacingOccurrences(of: "</say>", with: "", options: .caseInsensitive)
    return ["type": "session.instructions.append", "delegation_id": delegation,
            "content": "Say the following aloud exactly as written, word for word, without an introduction or explanation. "
                + "Punctuation and symbols are for phrasing; never read them out: <say>\(t)</say>"]
}
/// Two appends before it starts on either can lose one (measured), so the next waits until it is saying this one:
/// the first two plain words of what was sent (numbers are spoken as words, so they don't count), heard in its transcript.
func liveCue(_ text: String) -> String? {
    let w = text.lowercased().components(separatedBy: CharacterSet.letters.inverted).filter { !$0.isEmpty }
    return w.isEmpty ? nil : w.prefix(2).joined(separator: " ")
}
func cueHeard(_ cue: String, _ said: String) -> Bool {
    said.lowercased().components(separatedBy: CharacterSet.letters.inverted).filter { !$0.isEmpty }.joined(separator: " ").contains(cue)
}
/// Its transcript, as a caption: without the sound notes it adds ("[mouth noise]").
func liveCaption(_ s: String) -> String { s.replacingOccurrences(of: #"\s*\[[^\]]*\]"#, with: "", options: .regularExpression) }
/// 16-bit PCM loud enough to be speech, not the silence GPT-Live streams between turns.
func audible(_ pcm: Data) -> Bool {
    let n = pcm.count / 2
    guard n > 0 else { return false }
    let sum = pcm.withUnsafeBytes { p in (0..<n).reduce(0.0) { let v = Double(Int16(littleEndian: p.loadUnaligned(fromByteOffset: $1 * 2, as: Int16.self))); return $0 + v * v } }
    return (sum / Double(n)).squareRoot() > 150
}

// The socket is our own RFC 6455 client on an NWConnection, not URLSessionWebSocketTask. Measured 2026-11-20: api.openai.com
// upgrades over HTTP/1.1 only. Carried over HTTP/2 as an extended CONNECT (RFC 8441), the same request gets Cloudflare's
// "400 Bad Request" page (its SETTINGS lack ENABLE_CONNECT_PROTOCOL). URLSession picks the protocol itself and hides a refused
// upgrade's body. Here TLS offers only http/1.1, and a refusal's status, message and request id are read off the reply.

let liveALPN = ["http/1.1"]
/// The upgrade request: plain HTTP/1.1, the key as a bearer token.
func wsHandshake(host: String, path: String, key: String, nonce: String) -> Data {
    Data(("GET \(path) HTTP/1.1\r\nHost: \(host)\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: \(nonce)\r\n"
        + "Sec-WebSocket-Version: 13\r\nAuthorization: Bearer \(key)\r\nUser-Agent: Greenlight\r\n\r\n").utf8)
}
/// What a server must answer to `nonce` in Sec-WebSocket-Accept.
func wsAccept(_ nonce: String) -> String { Data(Insecure.SHA1.hash(data: Data((nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").utf8))).base64EncodedString() }
/// A reply's head once it has all come in (nil before): status, headers by lowercase name, and the bytes after it.
func wsReply(_ d: Data) -> (status: Int, headers: [String: String], rest: Data)? {
    guard let r = d.range(of: Data("\r\n\r\n".utf8)) else { return nil }
    let head = String(decoding: d[..<r.lowerBound], as: UTF8.self)  // never nil: a stray byte can't hide the status
    var lines = head.components(separatedBy: "\r\n"), h = [String: String]()
    let st = lines.removeFirst().split(separator: " ")
    for l in lines { if let c = l.firstIndex(of: ":") { h[l[..<c].lowercased()] = l[l.index(after: c)...].trimmingCharacters(in: .whitespaces) } }
    return (st.count > 1 ? Int(st[1]) ?? 0 : 0, h, Data(d[r.upperBound...]))
}
/// Whether a refusal's body has all come in: by its length, the last chunk, or (neither) when the server closes.
func wsBodyDone(_ h: [String: String], _ body: Data) -> Bool {
    if let n = h["content-length"].flatMap(Int.init) { return body.count >= n }
    return h["transfer-encoding"]?.lowercased() == "chunked" && unchunk(body).done
}
/// A chunked body's data, and whether its last (zero) chunk has come.
func unchunk(_ body: Data) -> (data: Data, done: Bool) {
    var out = Data(), at = body.startIndex
    while let eol = body[at...].range(of: Data("\r\n".utf8)),
          let n = Int(String(decoding: body[at..<eol.lowerBound], as: UTF8.self).split(separator: ";").first.map { $0.trimmingCharacters(in: .whitespaces) } ?? "", radix: 16) {
        if n == 0 { return (out, true) }
        guard body.distance(from: eol.upperBound, to: body.endIndex) >= n + 2 else { break }
        let end = body.index(eol.upperBound, offsetBy: n)
        out += body[eol.upperBound..<end]
        at = body.index(end, offsetBy: 2)
    }
    return (out, false)
}
/// A reply's body as sent: a chunked one without its chunk sizes (an error's JSON can span chunks).
func httpBody(_ h: [String: String], _ body: Data) -> Data { h["transfer-encoding"]?.lowercased() == "chunked" ? unchunk(body).data : body }
/// A refused upgrade's reason: OpenAI's error message (from the body, else its base64 `x-error-json` header), an HTML page's
/// title ("400 Bad Request" from Cloudflare), or the body's text. Never empty: a reply with none of these is named by its server
/// and type, so whatever answered can still be told apart (OpenAI, Cloudflare, or something in between).
func liveRefusal(_ body: Data, _ h: [String: String] = [:]) -> String {
    func message(_ t: String) -> String? {
        guard let a = t.firstIndex(of: "{"), let b = t.lastIndex(of: "}"), a < b,
              let j = try? JSONSerialization.jsonObject(with: Data(t[a...b].utf8)) as? [String: Any] else { return nil }
        let e = j["error"] as? [String: Any], m = (e?["message"] as? String ?? j["message"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let m, !m.isEmpty { return m }
        let code = [e?["code"], e?["type"]].compactMap { ($0 as? String)?.trimmingCharacters(in: .whitespaces).nilIfEmpty }.first
        return code.map { "error \($0)" }  // OpenAI's code when its message is blank
    }
    let t = String(decoding: body, as: UTF8.self)
    if let m = message(t) { return m }
    if let x = h["x-error-json"].flatMap({ Data(base64Encoded: $0) }), let m = message(String(decoding: x, as: UTF8.self)) { return m }
    let title = t.range(of: #"(?i)(?<=<title)(\s[^>]*)?>[^<]*"#, options: .regularExpression).map { t[$0].drop { $0 != ">" }.dropFirst().trimmingCharacters(in: .whitespacesAndNewlines) }
    if let title, !title.isEmpty { return title }
    let plain = title != nil ? "" : t.trimmingCharacters(in: .whitespacesAndNewlines)  // a page with a blank title: no markup
    if !plain.isEmpty { return String(plain.prefix(120)) }
    let from = [h["server"], h["content-type"]].compactMap { $0 }.joined(separator: ", ")
    return "an empty reply" + (from.isEmpty ? "" : " from \(from)")
}
/// What identifies a reply in a support request or a server log: OpenAI's request id, else Cloudflare's ray.
func liveReplyRef(_ h: [String: String]) -> String? {
    if let r = h["x-request-id"], !r.isEmpty { return "request \(r)" }
    if let r = h["cf-ray"], !r.isEmpty, r != "-" { return "cf-ray \(r)" }
    return nil
}
enum WSMessage: Equatable { case text(Data), binary(Data), ping(Data), pong, close(Int, String) }
/// One frame. A client masks its frames (RFC 6455 5.3); pass no mask for a server's.
func wsFrame(_ op: UInt8, _ payload: Data, mask: [UInt8]? = (0..<4).map { _ in UInt8.random(in: 0...255) }, fin: Bool = true) -> Data {
    var f = Data([(fin ? 0x80 : 0) | op]), n = payload.count, m: UInt8 = mask == nil ? 0 : 0x80
    if n < 126 { f.append(m | UInt8(n)) }
    else if n <= 0xFFFF { f.append(m | 126); f.append(contentsOf: [UInt8(n >> 8), UInt8(n & 0xFF)]) }
    else { f.append(m | 127); f.append(contentsOf: (0..<8).reversed().map { UInt8((n >> ($0 * 8)) & 0xFF) }) }
    guard let k = mask else { return f + payload }
    f.append(contentsOf: k)
    n = 0
    f.append(contentsOf: payload.map { b in defer { n += 1 }; return b ^ k[n & 3] })
    return f
}
/// Frames, however the bytes arrive: whole messages out (fragments joined), with pings and closes.
struct WSReader {
    private var buf = [UInt8](), frag = Data(), fragOp: UInt8 = 0
    static let limit = 16 << 20
    var broken = false  // a frame over the limit: the socket is closed
    mutating func feed(_ d: Data) -> [WSMessage] {
        buf += d
        var out = [WSMessage]()
        while !broken, buf.count >= 2 {
            let fin = buf[0] & 0x80 != 0, op = buf[0] & 0x0F, masked = buf[1] & 0x80 != 0
            var n = Int(buf[1] & 0x7F), at = 2
            if n == 126 { guard buf.count >= 4 else { break }; n = Int(buf[2]) << 8 | Int(buf[3]); at = 4 }
            else if n == 127 { guard buf.count >= 10 else { break }; n = buf[2..<10].reduce(0) { $0 << 8 | Int($1) }; at = 10 }
            if n > WSReader.limit || n < 0 { broken = true; break }
            let k = masked ? at : -1
            if masked { at += 4 }
            guard buf.count >= at + n else { break }
            var p = Data(buf[at..<at + n])
            if k >= 0 { let key = Array(buf[k..<k + 4]); p = Data(p.enumerated().map { $1 ^ key[$0 & 3] }) }
            buf.removeFirst(at + n)
            switch op {
            case 0x8: let c = p.count >= 2 ? Int(p[p.startIndex]) << 8 | Int(p[p.startIndex + 1]) : 1005
                out.append(.close(c, String(decoding: p.dropFirst(2), as: UTF8.self)))
            case 0x9: out.append(.ping(p))
            case 0xA: out.append(.pong)
            case 0x0, 0x1, 0x2:
                if op != 0 { frag = Data(); fragOp = op }
                frag += p
                if frag.count > WSReader.limit { broken = true; break }
                if fin { out.append(fragOp == 0x2 ? .binary(frag) : .text(frag)); frag = Data() }
            default: break
            }
        }
        return out
    }
}

/// One GPT-Live session over a WebSocket. Events come to `on` on the main queue; `send` and `mic` are safe from any thread.
final class LiveVoice {
    static let model = "gpt-live-1.1", host = "api.openai.com", path = "/v1/live/sessions"
    static let voices = ["marin", "cedar", "quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian", "bossa", "tempo", "beacon", "delta", "cinder",
                         "nuri", "juni", "flitz", "brise", "sillage", "nira", "harema", "shida", "noeul"]
    static var voice: String { ud.string(forKey: "liveVoice").flatMap { voices.contains($0) ? $0 : nil } ?? "marin" }
    let fmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 24000, channels: 1, interleaved: true)!
    let q = DispatchQueue(label: "live")
    /// One socket's state, touched only on q. Callbacks from a socket that isn't `sock` any more are ignored.
    private final class Sock {
        let c: NWConnection, nonce: String, key: String
        var waited: NWError?, up = false, dead = false, head = Data(), reply: (status: Int, headers: [String: String])?, body = Data(), reader = WSReader(), queued = [Data]()
        init(_ c: NWConnection, _ key: String) { self.c = c; self.key = key; nonce = Data((0..<16).map { _ in UInt8.random(in: 0...255) }).base64EncodedString() }
    }
    private var sock: Sock?
    var pending = Data(), on: ((LiveEvent) -> Void)?
    /// Where the socket goes: api.openai.com over TLS; the selftest's fake server is plain TCP on this Mac.
    var endpoint = (host: LiveVoice.host, port: UInt16(443), tls: true)

    func open(_ key: String, voice: String = LiveVoice.voice, instructions: String = liveRule, _ on: @escaping (LiveEvent) -> Void) {
        close()
        let tls = NWProtocolTLS.Options()
        for p in liveALPN { sec_protocol_options_add_tls_application_protocol(tls.securityProtocolOptions, p) }
        let e = endpoint
        let s = Sock(NWConnection(host: .init(e.host), port: .init(rawValue: e.port)!, using: e.tls ? NWParameters(tls: tls) : .tcp), key)
        q.sync { sock = s; pending = Data() }
        self.on = on
        s.c.stateUpdateHandler = { [weak self, weak s] st in if let s { self?.state(s, st) } }  // weak: the connection keeps its handler
        s.c.start(queue: q)
        send(liveStart(voice: voice, instructions: instructions))  // goes out once the upgrade is through
        q.asyncAfter(deadline: .now() + 10) { [weak self] in if let self, !s.up { self.end(s, s.waited.map { LiveVoice.why(0, "", $0) } ?? "didn't answer in time") } }
    }
    private func state(_ s: Sock, _ st: NWConnection.State) {
        switch st {
        case .ready:
            s.waited = nil  // it got through: a later timeout is the server's
            s.c.send(content: wsHandshake(host: LiveVoice.host, path: LiveVoice.path, key: s.key, nonce: s.nonce), completion: .contentProcessed { _ in })
            read(s)
        case .waiting(let e): s.waited = e  // Network retries from here (a path that's down, a refused connection); the deadline says why
        case .failed(let e): end(s, LiveVoice.why(0, "", e))
        default: break
        }
    }
    private func read(_ s: Sock) {
        s.c.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { [weak self] d, _, done, err in
            guard let self, !s.dead else { return }
            if let d, !d.isEmpty { s.up ? self.frames(s, d) : self.upgrade(s, d) }
            if s.dead { return }
            if done || err != nil {
                if !s.up, let r = s.reply { return self.refused(s, r.status, r.headers) }
                return self.end(s, err.map { LiveVoice.why(0, "", $0) } ?? (s.up ? "lost the connection" : "closed the connection before answering"))
            }
            self.read(s)
        }
    }
    /// The reply to the upgrade: 101 and the right accept opens the socket; anything else is a refusal, read to its end.
    private func upgrade(_ s: Sock, _ d: Data) {
        if s.reply != nil { s.body += d; if wsBodyDone(s.reply!.headers, s.body) { refused(s, s.reply!.status, s.reply!.headers) }; return }
        s.head += d
        guard let r = wsReply(s.head) else { if s.head.count > 64 << 10 { end(s, "sent a reply Greenlight can't read") }; return }
        s.head = Data()
        guard r.status == 101 else {
            s.reply = (r.status, r.headers); s.body = r.rest
            if wsBodyDone(r.headers, s.body) { refused(s, r.status, r.headers) }
            else { q.asyncAfter(deadline: .now() + 2) { [weak self] in self?.refused(s, r.status, r.headers) } }
            return
        }
        guard r.headers["sec-websocket-accept"] == wsAccept(s.nonce) else { return end(s, "answered the upgrade wrongly (a proxy in the way?)") }
        s.up = true
        for f in s.queued { s.c.send(content: f, completion: .contentProcessed { _ in }) }
        s.queued = []
        if !r.rest.isEmpty { frames(s, r.rest) }
    }
    private func refused(_ s: Sock, _ status: Int, _ h: [String: String]) {
        guard !s.dead else { return }
        let m = liveRefusal(httpBody(h, s.body), h), ref = liveReplyRef(h)
        // the headers that tell one server from another, for Console (not x-error-json or cookies: they can carry account details)
        let shown = ["server", "content-type", "content-length", "transfer-encoding", "connection", "via", "cf-ray", "x-request-id"]
        NSLog("GPT-Live: upgrade refused, HTTP %d, %@: %@ [%@]", status, ref ?? "no request id", m,
              shown.compactMap { k in h[k].map { "\(k): \($0)" } }.joined(separator: "; "))
        end(s, LiveVoice.why(status, m, ref: ref))
    }
    private func frames(_ s: Sock, _ d: Data) {
        for m in s.reader.feed(d) {
            switch m {
            case .text(let p), .binary(let p):
                let e = liveEvent(p)
                if e != .other { DispatchQueue.main.async { if self.q.sync(execute: { self.sock === s }) { self.on?(e) } } }
            case .ping(let p): s.c.send(content: wsFrame(0xA, p), completion: .contentProcessed { _ in })
            case .pong: break
            case .close(let c, let why):
                return end(s, c == 1000 || c == 1005 ? "closed the connection" : "closed the connection (\(c)\(why.isEmpty ? "" : ": \(why)"))",
                           last: wsFrame(0x8, Data([0x03, 0xE8])))  // the close's answer, sent before the socket goes
            }
        }
        if s.reader.broken { end(s, "sent a message over 16 MB") }
    }
    /// On q: the socket is done; voice mode hears why, unless it has moved on to another.
    private func end(_ s: Sock, _ why: String, last: Data? = nil) {
        guard !s.dead else { return }
        s.dead = true
        if let last { s.c.send(content: last, completion: .contentProcessed { _ in s.c.cancel() }) } else { s.c.cancel() }
        DispatchQueue.main.async { if self.q.sync(execute: { self.sock === s }) { self.q.sync { self.sock = nil }; self.on?(.closed(why)) } }
    }
    /// On q: a text frame, now or once the upgrade is through.
    private func write(_ s: Sock, _ text: String) {
        let f = wsFrame(0x1, Data(text.utf8))
        if s.up { s.c.send(content: f, completion: .contentProcessed { _ in }) } else { s.queued.append(f) }
    }
    func send(_ o: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: o), let s = String(data: d, encoding: .utf8) else { return }
        q.async { if let k = self.sock, !k.dead { self.write(k, s) } }
    }
    /// Mic audio, already 24 kHz 16-bit mono, sent in 100 ms pieces.
    func mic(_ b: AVAudioPCMBuffer) {
        guard let p = b.int16ChannelData, b.frameLength > 0 else { return }
        let d = Data(bytes: p[0], count: Int(b.frameLength) * 2)
        q.async {
            guard let k = self.sock, !k.dead else { return }
            self.pending += d
            guard self.pending.count >= 4800 else { return }
            let o = ["type": "session.input_audio.append", "audio": self.pending.base64EncodedString()]
            self.pending = Data()
            if let j = try? JSONSerialization.data(withJSONObject: o), let s = String(data: j, encoding: .utf8) { self.write(k, s) }
        }
    }
    func close() {
        on = nil
        q.async {
            guard let s = self.sock else { return }
            self.sock = nil
            guard !s.dead else { return }
            s.dead = true
            guard s.up else { return s.c.cancel() }
            s.c.send(content: wsFrame(0x1, Data(#"{"type":"session.close"}"#.utf8)), completion: .contentProcessed { _ in })
            s.c.send(content: wsFrame(0x8, Data([0x03, 0xE8])), completion: .contentProcessed { _ in s.c.cancel() })
        }
    }
    /// Why the socket failed, in words: an HTTP status with the server's reason, or a network error.
    /// `ref` (a request id) goes on every refusal, so a screenshot of one is enough to look it up.
    static func why(_ code: Int, _ detail: String = "", _ err: Error? = nil, ref: String? = nil) -> String {
        let d = detail.hasPrefix("\(code) ") ? String(detail.dropFirst("\(code) ".count)) : detail  // Cloudflare's title has the code
        let r = d.hasSuffix(".") ? String(d.dropLast()) : d
        let more = (r.isEmpty ? "" : ": \(r)") + (ref.map { "; \($0)" } ?? "")
        switch code {
        case 401: return "refused the OpenAI key (401\(ref.map { "; \($0)" } ?? ""))"
        case 403, 404: return "isn't available to this OpenAI key's project (\(code)\(more)): it needs access to \(model)"
        case 429: return "is over its rate or concurrent-session limit (429\(more))"
        case 0: break
        default: return "refused the connection (\(code)\(more))"
        }
        if let e = err as? NWError {
            if case .posix(let c) = e, c == .ETIMEDOUT { return "didn't answer in time" }
            if case .dns = e { return "couldn't look up api.openai.com (\(e.localizedDescription)); is this Mac online?" }
            if case .tls = e { return "couldn't make a secure connection to api.openai.com (\(e.localizedDescription)); a proxy or security tool may be intercepting it" }
            return "couldn't reach api.openai.com (\(e.localizedDescription)); a firewall like Little Snitch may be blocking it"
        }
        return err.map { "lost the connection (\($0.localizedDescription))" } ?? "lost the connection"
    }
}

// MARK: Settings' GPT-Live voice preview
//
// GPT-Live has no text-to-speech endpoint, and OpenAI's TTS has only two of its voices (marin, cedar), so a preview is a short session
// of its own. Told to speak first, it says one line with no hand-off. It speaks only while it hears something, so it hears silence.
// Measured 2026-11-20: 3 of 3 sessions said the line exactly, with the first audio ~1.9 s after opening.

let livePreviewLine = "Hello. This is how I sound."
let livePreviewRule = "You are a voice preview. As soon as the session starts, before the user says anything, say exactly: \(livePreviewLine) Then stay silent."

/// Which of a preview's audio to play, and when it's over: nothing before the first speech (GPT-Live streams silence), then all of it
/// until 0.8 s of quiet, or 8 s of it in all (it shouldn't ramble, but a preview stays short).
struct PreviewTrack {
    var spoke = false, quiet = 0.0, length = 0.0
    mutating func take(_ pcm: Data) -> (play: Bool, done: Bool) {
        let loud = audible(pcm), secs = Double(pcm.count) / 48000
        if !spoke { guard loud else { return (false, false) }; spoke = true }
        quiet = loud ? 0 : quiet + secs
        length += secs
        return (true, quiet > 0.8 || length >= 8)
    }
}

/// Whether the default output device is muted (then a preview isn't worth a session).
func outputMuted() -> Bool {
    var dev = AudioObjectID(0), size = UInt32(MemoryLayout<AudioObjectID>.size)
    var a = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &dev) == noErr else { return false }
    var m = UInt32(0); size = UInt32(MemoryLayout<UInt32>.size)
    a = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute, mScope: kAudioDevicePropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
    return AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &m) == noErr && m != 0
}

/// Picking a GPT-Live voice in Settings (or Test) says livePreviewLine in it. One preview at a time: a new pick, Stop, closing Settings,
/// changing the speaking voice or starting voice mode ends the last one. None while voice mode is on (that would talk over it) or
/// while the Mac is muted. Its engine is output-only: the mic isn't touched.
final class LivePreview: ObservableObject {
    static let shared = LivePreview()
    enum Phase: Equatable { case idle, loading(String), playing(String), played(String), skipped(String), failed(String, String) }
    @Published private(set) var phase = Phase.idle
    private(set) var said = "", gen = 0, opened = 0
    private var live: LiveVoice?, engine: AVAudioEngine?, node: AVAudioPlayerNode?, conv: AVAudioConverter?, feed: Timer?
    private(set) var scheduled = 0, played = 0
    private var track = PreviewTrack(), closed = false
    private let out = AVAudioFormat(standardFormatWithSampleRate: 22050, channels: 1)!
    var busy: Bool { if case .loading = phase { return true }; if case .playing = phase { return true }; return false }

    /// What Settings shows under the voice picker.
    var hint: String? {
        switch phase {
        case .idle: return nil
        case .loading(let v): return "Getting \(v.capitalized)’s voice…"
        case .playing(let v): return "Playing \(v.capitalized)…"
        case .played(let v): return "That was \(v.capitalized). Voice mode uses it from its next start."
        case .skipped(let why): return why
        case .failed(let v, let why): return "Couldn’t preview \(v.capitalized): \(why)"
        }
    }

    /// key is read only once a quick run through the menu settles (each pick would otherwise open a session).
    func play(_ voice: String, voiceOn: Bool, muted: Bool = outputMuted(), settle: Double = 0.35, key: @escaping () -> String? = { openAIKey() }) {
        stop()
        Speech.shared.tester?.stop()  // an earlier Test line in another voice
        if voiceOn { return phase = .skipped("\(LivePreview.voiceOnNote), so no preview (it would talk over it). \(voice.capitalized) takes over next time voice mode starts.") }
        if muted { return phase = .skipped("This Mac’s sound is muted: unmute it, then press ▶ to hear \(voice.capitalized).") }
        let g = gen
        phase = .loading(voice)
        DispatchQueue.main.asyncAfter(deadline: .now() + settle) { [weak self] in self?.begin(voice, key, g) }
    }
    func stop() {
        teardown()  // bumps gen: its timers and callbacks are void
        phase = .idle
    }
    /// Voice mode ended: its "voice mode is on" note no longer applies (a muted one still does).
    func voiceModeEnded() {
        if case .skipped(let why) = phase, why.hasPrefix(LivePreview.voiceOnNote) { phase = .idle }
    }
    /// A key was saved or removed. A new OpenAI key may fix a failure, so ▶ is worth another try, and a preview still
    /// starting on the old key is stopped (its refusal would land after the save). Removing a key fixes nothing.
    func keyChanged(_ account: String, to v: String) {
        guard account == "openai", !v.isEmpty else { return }
        switch phase { case .loading: stop(); case .failed: phase = .idle; default: break }
    }
    static let voiceOnNote = "Voice mode is on"

    private func begin(_ voice: String, _ key: () -> String?, _ g: Int) {
        guard g == gen else { return }
        guard let k = key(), !k.isEmpty else { return fail(voice, "it needs an OpenAI key (Set key… above, or export OPENAI_API_KEY and relaunch), or the Keychain didn’t let Greenlight read it") }
        guard g == gen else { return }  // stopped while the Keychain asked
        let e = AVAudioEngine(), n = AVAudioPlayerNode()
        e.attach(n)
        do { try e.link(n, to: e.mainMixerNode, format: out); try e.start(); try n.start() } catch { return fail(voice, "the audio output didn’t start (\(error.localizedDescription))") }
        engine = e; node = n; track = PreviewTrack(); said = ""; scheduled = 0; played = 0; closed = false; opened += 1
        let l = LiveVoice()
        live = l
        l.open(k, voice: voice, instructions: livePreviewRule) { [weak self] ev in self?.on(ev, voice, g) }
        let silence = AVAudioPCMBuffer(pcmFormat: l.fmt, frameCapacity: 2400)!  // zeroed: 100 ms of quiet
        silence.frameLength = 2400
        let t = Timer(timeInterval: 0.1, repeats: true) { _ in l.mic(silence) }
        RunLoop.main.add(t, forMode: .common)  // and while the voice menu is open
        feed = t
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) { [weak self] in
            if let self, self.gen == g, !self.track.spoke { self.fail(voice, "GPT-Live didn’t speak within 12 s") }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in if let self, self.gen == g { self.finish(voice) } }  // a lost playback callback, or audio that stopped without the quiet
    }
    private func on(_ e: LiveEvent, _ voice: String, _ g: Int) {
        guard g == gen else { return }
        switch e {
        case .said(let d): said += d
        case .audio(let d): audio(d, voice, g)
        case .failed(_, let m): if !track.spoke { fail(voice, "GPT-Live: \(m)") }  // e.g. "Voice session access denied." for a voice it lacks
        case .closed(let why): if !track.spoke { fail(voice, "GPT-Live \(why)") } else { closeSession(voice) }
        default: break
        }
    }
    private func audio(_ d: Data, _ voice: String, _ g: Int) {
        guard !closed else { return }
        let (play, done) = track.take(d)
        if play, let n = node, let b = Speech.shared.buffer(d) {
            if conv == nil { conv = AVAudioConverter(from: b.format, to: out) }
            if let c = conv, let o = convert(b, c, out) {
                if phase == .loading(voice) { phase = .playing(voice) }
                scheduled += 1
                n.scheduleBuffer(o, completionCallbackType: .dataPlayedBack) { [weak self] _ in DispatchQueue.main.async {
                    guard let self, self.gen == g else { return }
                    self.played += 1
                    if self.closed && self.played >= self.scheduled { self.finish(voice) }
                } }
            }
        }
        if done { closeSession(voice) }
    }
    /// The line is over: no more audio from it, and the session goes; what's queued still plays out.
    private func closeSession(_ voice: String) {
        guard !closed else { return }
        closed = true
        feed?.invalidate(); feed = nil
        live?.close(); live = nil
        if played >= scheduled { finish(voice) }
    }
    private func finish(_ voice: String) { teardown(); phase = .played(voice) }
    private func fail(_ voice: String, _ why: String) { NSLog("GPT-Live preview: %@", why); teardown(); phase = .failed(voice, why) }
    private func teardown() {
        gen += 1  // its timers and late callbacks are void, so they can't replace how it ended
        feed?.invalidate(); feed = nil
        live?.close(); live = nil
        node?.stop(); engine?.stop(); node = nil; engine = nil; conv = nil
        closed = true
    }
}

final class Core: ObservableObject {
    /// 0: the main chat. n > 0: a second Greenlight Center window's own Chief (chat.py's CHAT_DIR chat-<n>, its own transcript and questions).
    let session: Int
    let transcriptFile: URL, questionsURL: URL
    init(session: Int = 0) {
        self.session = session
        transcriptFile = session == 0 ? transcript : support.appendingPathComponent("transcript-\(session).json")
        questionsURL = session == 0 ? questionsFile : support.appendingPathComponent("questions-\(session).json")
        msgs = ((try? JSONDecoder().decode([Msg.Saved].self, from: Data(contentsOf: transcriptFile)))?.map(Msg.init)) ?? msgs
        questions = (try? JSONDecoder().decode([Question].self, from: Data(contentsOf: questionsURL))) ?? []
        context = ud.integer(forKey: meterKeys.context); window = ud.integer(forKey: meterKeys.window)
    }
    /// This chat's context meter in UserDefaults: the main chat keeps the old keys, a second window's chat has its own.
    var meterKeys: (context: String, window: String) { session == 0 ? ("context", "window") : ("context-\(session)", "window-\(session)") }
    var chief: String { session == 0 ? chatCmd : "CHAT_DIR=\(greenlightHome)/chat-\(session) \(py)/chat.py" }
    var light: ([String]) -> Void = { delegate.pick($0) }  // the hexes a reply is about, lit in this chat's dashboard
    var subs = Set<AnyCancellable>(), mirroring = false
    /// A second window's chat keeps the main one's settings, both ways: Settings (the main chat's) and this chat's own name agree.
    func follow(_ m: Core) {
        func both<T: Equatable>(_ k: ReferenceWritableKeyPath<Core, T>, _ p: KeyPath<Core, Published<T>.Publisher>) {
            self[keyPath: k] = m[keyPath: k]
            // @Published sends before the value lands: the flag, not an equality check, stops the echo
            m[keyPath: p].dropFirst().sink { [weak self] v in guard let self, !self.mirroring else { return }; self.mirroring = true; self[keyPath: k] = v; self.mirroring = false }.store(in: &subs)
            self[keyPath: p].dropFirst().sink { [weak self, weak m] v in guard let self, !self.mirroring else { return }; self.mirroring = true; m?[keyPath: k] = v; self.mirroring = false }.store(in: &subs)
        }
        both(\.model, \.$model); both(\.effort, \.$effort); both(\.name, \.$name); both(\.verbose, \.$verbose)
        both(\.liveLine, \.$liveLine); both(\.vim, \.$vim); both(\.vimStart, \.$vimStart); both(\.look, \.$look); both(\.sent, \.$sent)
        $settingsOpen.filter { $0 }.sink { [weak self, weak m] _ in m?.settingsOpen = true; DispatchQueue.main.async { self?.settingsOpen = false } }.store(in: &subs)
    }
    lazy var voice: Voice = { let v = Voice(); v.core = self; return v }()
    @Published var attached: [Attached] = []
    var unattach: ((String) -> Void)?  // tells the dashboard to empty that dock slot
    @Published var msgs: [Msg] = [Msg(.note, "Ask me anything about your workspaces.")] {  // init reads the transcript
        didSet {  // ponytail: rewrites the whole file per message; capped at 300 so that stays cheap
            try? FileManager.default.createDirectory(at: transcriptFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? JSONEncoder().encode(msgs.suffix(300).map(\.saved)).write(to: transcriptFile, options: .atomic)
        }
    }
    @Published var run: Run?
    @Published var queue: [Queued] = []
    struct Queued { let text: String; var files: [ChatFile] = [] }
    @Published var files: [ChatFile] = []  // attached in the composer, sent with the next message
    @Published var fileNote = ""  // why the last file didn't attach; clears itself
    @Published var dropping = false  // files dragged over the chat
    @Published var reading = 0  // files being read and checked off the main thread; Send waits for them
    let reader = DispatchQueue(label: "greenlight.attach", qos: .userInitiated)  // one at a time: files land in the order they came
    func attach(_ urls: [URL]) { read { urls.map(chatFile(at:)) } }
    func attach(_ data: Data, name: String) { read { [chatFile(data, name: name)] } }
    private func read(_ work: @escaping () -> [(file: ChatFile?, error: String?)]) {
        reading += 1
        reader.async { [weak self] in let rs = work(); DispatchQueue.main.async { self?.reading -= 1; rs.forEach { self?.add($0) } } }
    }
    private func add(_ r: (file: ChatFile?, error: String?)) {
        guard let f = r.file else { return fileSays(r.error ?? "That file couldn't be attached.") }
        if files.count >= ChatFile.maxFiles { return fileSays("Attach up to \(ChatFile.maxFiles) files at a time.") }
        if files.reduce(f.data.count, { $0 + $1.data.count }) > ChatFile.maxTotal { return fileSays("These files come to more than \(byteSize(ChatFile.maxTotal)): send fewer at a time.") }
        files.append(f)
        fileNote = ""
        log(.chief, name, .status, "attached a file (\(ChatFile.kinds[f.kind]!), \(byteSize(f.data.count)))")  // never its name's folder, never its contents
    }
    func fileSays(_ s: String) {
        fileNote = s
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in if self?.fileNote == s { self?.fileNote = "" } }
    }
    /// The composer's +: an open panel on the chat's window (a sheet), any file; the check says which can't go.
    func pickFiles(_ win: NSWindow?) {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = true; p.canChooseDirectories = false; p.canChooseFiles = true
        p.message = "Attach files for \(name): \(ChatFile.supported)"
        p.prompt = "Attach"
        if let win { p.beginSheetModal(for: win) { [weak self] r in if r == .OK { self?.attach(p.urls) } } }
        else { NSApp.activate(ignoringOtherApps: true); if p.runModal() == .OK { attach(p.urls) } }
    }
    /// What a drop or paste would attach: files, an app's promised files, or an image that isn't rich text's picture
    /// (a selection dragged from Pages or Word carries one: that's text, for the field).
    static func takes(_ pb: NSPasteboard) -> Bool {
        let t = Set(pb.types ?? [])
        return t.contains(.fileURL) || !t.isDisjoint(with: NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType($0) })
            || (!t.isDisjoint(with: [.png, .tiff]) && t.isDisjoint(with: [.rtf, .rtfd]))
    }
    /// Files or images dropped on the chat (or pasted): files by their URLs, else an image's data (a browser's), else files an app
    /// promises (Photos, Mail), received into a temporary folder that goes once they're read. False: nothing to attach.
    func dropped(_ pb: NSPasteboard) -> Bool {
        guard Core.takes(pb) else { return false }
        if let us = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !us.isEmpty { attach(us); return true }
        if pb.availableType(from: [.rtf, .rtfd]) == nil, let t = pb.availableType(from: [.png, .tiff]), let d = pb.data(forType: t) {
            attach(d, name: t == .png ? "Image.png" : "Image.tiff"); return true
        }
        guard let ps = pb.readObjects(forClasses: [NSFilePromiseReceiver.self]) as? [NSFilePromiseReceiver], !ps.isEmpty else { return false }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("greenlight-drop-" + UUID().uuidString)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        final class Left { var n = 0, held = 0 }  // n: files still to come (the promise queue's); held: of `reading` (the main thread's)
        let left = Left(), q = OperationQueue()
        q.maxConcurrentOperationCount = 1
        left.n = ps.reduce(0) { $0 + max(1, $1.fileNames.count) }
        left.held = left.n; reading += left.n  // Send waits for them, but not forever: a promise may never be kept
        let release = { [weak self] (k: Int) in let k = min(k, left.held); left.held -= k; self?.reading -= k }
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { release(left.held) }
        for r in ps {
            r.receivePromisedFiles(atDestination: dir, options: [:], operationQueue: q) { [weak self] url, err in
                let got = err == nil ? chatFile(at: url) : (nil, "A dropped file couldn't be received.")
                left.n -= 1
                if left.n <= 0 { try? FileManager.default.removeItem(at: dir) } else { try? FileManager.default.removeItem(at: url) }
                DispatchQueue.main.async { release(1); self?.add(got) }
            }
        }
        return true
    }
    /// ⌘V in the chat field with files or an image on the clipboard (and no text): attach them. True = taken.
    func pasteFiles() -> Bool {
        let pb = NSPasteboard.general
        if pb.availableType(from: [.fileURL]) == nil && pb.string(forType: .string) != nil { return false }  // text pastes as text
        return dropped(pb)
    }
    @Published var input = "" { didSet { if !recalling { hp = -1 } } }  // typing over a recalled prompt makes it the draft
    // ponytail: newest first in UserDefaults, 500 kept; this Mac only
    @Published var sent = (try? JSONDecoder().decode([Sent].self, from: ud.data(forKey: "sent") ?? Data())) ?? [] {
        didSet { ud.set(try? JSONEncoder().encode(sent), forKey: "sent") }
    }
    @Published var hp = -1  // ↑ position in `sent`; -1 = the draft
    @Published var hq: String?  // the ⌃R query; nil = closed
    @Published var hsel = 0
    var hsaved = "", recalling = false, toEnd = false  // toEnd: the key monitor puts the caret at the end after this key
    @Published var model = UserDefaults.standard.string(forKey: "model") ?? "claude-opus-5-5" {
        didSet {
            UserDefaults.standard.set(model, forKey: "model")
            if harnessOf(model) != harnessOf(oldValue) {
                msgs.append(Msg(.note, isClaude ? "Back to the Claude conversation." : "\(modelName) keeps its own conversation, separate from Claude's."))
                context = 0; window = 0; run?.meterStale = true  // the meter was the other harness's: tokens only until this one reports
            }
        }
    }
    @Published var effort = UserDefaults.standard.string(forKey: "effort") ?? "medium" { didSet { UserDefaults.standard.set(effort, forKey: "effort") } }
    // The agent that designs dashboard pages and puck widgets: a Claude model, or a Gemini one on Gemini CLI.
    @Published var designModel = UserDefaults.standard.string(forKey: "designModel") ?? "claude-haiku-5-5" { didSet { UserDefaults.standard.set(designModel, forKey: "designModel") } }
    @Published var designEffort = UserDefaults.standard.string(forKey: "designEffort") ?? "low" { didSet { UserDefaults.standard.set(designEffort, forKey: "designEffort") } }
    /// The terminal's tab names: written by this model once a tab's output settles, unless you named it.
    @Published var termNameModel = UserDefaults.standard.string(forKey: "termNameModel") ?? "claude-haiku-5-5" { didSet { UserDefaults.standard.set(termNameModel, forKey: "termNameModel") } }
    @Published var termAutoName = UserDefaults.standard.object(forKey: "termAutoName") as? Bool ?? true { didSet { UserDefaults.standard.set(termAutoName, forKey: "termAutoName") } }
    // The agent that writes the hover card's next step (dash.py suggest), ahead of time for every puck whose state moved.
    @Published var nextModel = UserDefaults.standard.string(forKey: "nextModel") ?? "claude-haiku-5-5" { didSet { UserDefaults.standard.set(nextModel, forKey: "nextModel") } }
    @Published var nextEffort = UserDefaults.standard.string(forKey: "nextEffort") ?? "low" { didSet { UserDefaults.standard.set(nextEffort, forKey: "nextEffort") } }
    @Published var designParallel = UserDefaults.standard.object(forKey: "designParallel") as? Int ?? 3 { didSet { UserDefaults.standard.set(designParallel, forKey: "designParallel") } }
    @Published var context = 0 { didSet { ud.set(context, forKey: meterKeys.context) } }  // init reads this chat's
    @Published var window = 0 { didSet { ud.set(window, forKey: meterKeys.window) } }  // 0: unknown, the meter shows tokens only
    @Published var tps = 0.0
    @Published var outTok = 0  // this run's output tokens, for the live line
    @Published var liveLine = ud.object(forKey: "liveLine") as? Bool ?? true { didSet { ud.set(liveLine, forKey: "liveLine") } }
    /// Verbose activity: the chat's live log (Feed). Off: the concise view, and chat.py sends no more than it used to.
    @Published var verbose = ud.bool(forKey: "verboseActivity") {
        didSet { ud.set(verbose, forKey: "verboseActivity"); if !verbose { feed.clear() }; fedFrom = nil }
    }
    let feed = Feed()
    var fedFrom: [String: Watched]?  // the last snapshot the feed saw; nil: the next one is the baseline
    func log(_ src: FeedLine.Src, _ who: String, _ kind: FeedLine.Kind, _ text: String) { if verbose { feed.add(src, who, kind, text) } }
    /// A dashboard button's command, and how it ended: its JSON's error, else the exit code.
    func acted(_ who: String, _ what: String, _ code: Int32, _ out: String) {
        let err = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) .flatMap { $0 as? [String: Any] }?["error"] as? String
        log(.greenlight, who, code == 0 && err == nil ? .action : .failed, "\(what): " + (err ?? (code == 0 ? "done" : "exit \(code)")))
    }
    /// A snapshot's agent and lifecycle changes, into the feed.
    func fed(_ pucks: [[String: Any]]) {
        let (now, lines) = feedDiff(fedFrom, pucks)
        fedFrom = now
        for l in lines { log(l.0, l.1, l.2, l.3) }
    }
    @Published var lastTps = 0.0
    @Published var connected = false
    @Published var unread = 0
    @Published var now = Date()
    @Published var name = UserDefaults.standard.string(forKey: "name") ?? "Greenlight" { didSet { UserDefaults.standard.set(name, forKey: "name") } }
    @Published var editingName = false
    @Published var hint = ""  // hover text for the footer buttons; native tooltips are slow
    @Published var voiceTest = ""  // Settings' Test of the speaking voice: what happened
    @Published var keying = false  // the next chord becomes the voice shortcut: Settings shows the recorder in place
    @Published var questions: [Question] = [] {  // init reads questionsURL
        didSet {  // all waiting ones plus the last 20 answered
            let keep = questions.filter { $0.answer == nil } + questions.filter { $0.answer != nil }.suffix(20)
            try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
            try? JSONEncoder().encode(keep.sorted { $0.ts < $1.ts }).write(to: questionsURL, options: .atomic)
        }
    }
    @Published var inboxOpen = false
    @Published var replyingTo: UUID?
    @Published var replyText = ""
    @Published var engine = UserDefaults.standard.string(forKey: "engine") ?? "apple" { didSet { UserDefaults.standard.set(engine, forKey: "engine") } }
    @Published var voiceMods = UserDefaults.standard.object(forKey: "voiceMods") as? Int ?? (controlKey | optionKey) { didSet { UserDefaults.standard.set(voiceMods, forKey: "voiceMods") } }
    @Published var voiceCode = UserDefaults.standard.object(forKey: "voiceCode") as? Int ?? kVK_ANSI_V { didSet { UserDefaults.standard.set(voiceCode, forKey: "voiceCode") } }
    @Published var voiceKey = UserDefaults.standard.string(forKey: "voiceKey") ?? "V" { didSet { UserDefaults.standard.set(voiceKey, forKey: "voiceKey") } }
    @Published var recording: Date?
    @Published var transcribing = false
    @Published var levels = [Float](repeating: 0, count: 28)  // recent mic levels 0...1, newest last
    @Published var settingsOpen = false
    @Published var settingsTab = "General"
    @Published var settingsDark = ud.bool(forKey: "settingsDark") { didSet { ud.set(settingsDark, forKey: "settingsDark") } }  // Settings' own light or dark; the rest of the app keeps its themes
    @Published var settingsRepo = ""  // the hover card's "Edit <repo>'s prompt…": Repositories opens on it
    @Published var orbsHidden = ud.bool(forKey: "orbsHidden") { didSet { ud.set(orbsHidden, forKey: "orbsHidden") } }
    @Published var termOpens = ud.string(forKey: "termOpens") ?? "pipe" { didSet { ud.set(termOpens, forKey: "termOpens") } }  // the app opens to: greenlight | basement | pipe (New windows: the Pipe)
    /// The orb's ride between floors, in seconds; 0: a cut. Was the "animations" on/off.
    @Published var rideTime = ud.object(forKey: "rideTime") as? Double ?? (ud.object(forKey: "animations") as? Bool == false ? 0 : 0.6) { didSet { ud.set(rideTime, forKey: "rideTime") } }
    @Published var localName = ud.string(forKey: "localName") ?? "" { didSet { ud.set(localName, forKey: "localName") } }  // empty: this Mac's hostname
    var localShown: String { localName.trimmingCharacters(in: .whitespaces).isEmpty ? localDefault : localName.trimmingCharacters(in: .whitespaces) }
    // Settings › Hosts: where greenlight runs. The globals host/greenlightHome/greenlightCode read these at launch, so a change asks for a relaunch.
    @Published var runHost = ud.string(forKey: "host") ?? "local" { didSet { ud.set(runHost, forKey: "host"); hostCheck = "" } }
    @Published var runHome = ud.string(forKey: "greenlightHome") ?? "" { didSet { runHome.isEmpty ? ud.removeObject(forKey: "greenlightHome") : ud.set(runHome, forKey: "greenlightHome"); hostCheck = "" } }  // empty: ~/.greenlight
    @Published var runCode = ud.string(forKey: "greenlightCode") ?? "" { didSet { runCode.isEmpty ? ud.removeObject(forKey: "greenlightCode") : ud.set(runCode, forKey: "greenlightCode"); hostCheck = "" } }  // empty: ~/.claude/skills/greenlight
    @Published var hostCheck = ""
    var hostPending: Bool { ProcessInfo.processInfo.environment["GREENLIGHT_HOST"] == nil && runHost != host
        || (runHome.isEmpty ? "~/.greenlight" : runHome) != greenlightHome || (runCode.isEmpty ? "~/.claude/skills/greenlight" : runCode) != greenlightCode }
    /// Test: ssh in (or a shell here) and look for greenlight.py and python3 at the settings' paths.
    func testHost() {
        let h = runHost, code = runCode.isEmpty ? "~/.claude/skills/greenlight" : runCode
        var out = ""
        hostCheck = "Checking \(h)…"
        ssh("command -v python3 >/dev/null || { echo nopy; exit 3; }; test -f \(code)/scripts/greenlight.py && echo ok", on: h, onLine: { out = $0 }) { [weak self] rc in
            self?.hostCheck = out == "ok" ? "✓ \(h) is ready: greenlight is at \(code)." : out == "nopy" ? "✗ \(h) has no python3."
                : rc == 255 || rc == -1 ? "✗ Can't reach \(h) over ssh (BatchMode: it needs key auth)." : "✗ \(h) has no \(code)/scripts/greenlight.py. Clone the repo there."
        }
    }
    func relaunch() {
        let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/sh"); p.arguments = ["-c", "sleep 1; open \"$0\"", Bundle.main.bundlePath]
        try? p.run(); NSApp.terminate(nil)
    }
    @Published var sidebarSlide = ud.object(forKey: "sidebarSlide") as? Double ?? 0.25 { didSet { ud.set(sidebarSlide, forKey: "sidebarSlide") } }  // seconds; 0: no slide
    @Published var orbStyle = ud.string(forKey: "orbStyle") ?? "matte" { didSet { ud.set(orbStyle, forKey: "orbStyle") } }  // the up face: "matte" or "light" (the traffic light)
    @Published var dockIcon = ud.string(forKey: "dockIcon") ?? "Orb" { didSet { ud.set(dockIcon, forKey: "dockIcon"); showDockIcon() } }  // Orb (the bundle's), MergeLight, ThreeFloors: brand/icons/*.icns
    func showDockIcon() { NSApp.applicationIconImage = dockIcon == "Orb" ? nil : Bundle.main.image(forResource: dockIcon) }  // ponytail: the running app's Dock tile only; Finder keeps the bundle's Orb
    @Published var vim = ud.bool(forKey: "vim") { didSet { ud.set(vim, forKey: "vim"); vimKeys.mode = vimStart } }
    @Published var vimStart = ud.string(forKey: "vimStart") ?? "insert" { didSet { ud.set(vimStart, forKey: "vimStart") } }
    @Published var vimMode = ud.string(forKey: "vimStart") ?? "insert"
    lazy var vimKeys: Vim = {
        let v = Vim()
        v.mode = vimMode
        v.onMode = { [weak self] in self?.vimMode = $0 }
        return v
    }()
    @Published var look = 0  // bumped on any theme/font change so every view re-renders

    func restyle(_ change: () -> Void) {
        change()
        ud.set(darkTheme.name, forKey: "darkTheme"); ud.set(lightTheme.name, forKey: "lightTheme"); ud.set(accentHex, forKey: "accent")
        ud.set(chatFont, forKey: "chatFont"); ud.set(codeFont, forKey: "codeFont"); ud.set(fontSize, forKey: "fontSize")
        look += 1
    }

    var modelName: String { models.first { $0.1 == model }?.0 ?? model }
    var recorder: AVAudioRecorder?
    var meter: Timer?
    var shortcut: String { shortcutName(voiceMods, voiceKey) }
    var popoverShown: () -> Bool = { false }  // the chat visible and in front
    @Published var chatOut = ud.bool(forKey: "chatOut") { didSet { ud.set(chatOut, forKey: "chatOut") } }  // in its own window, not the right pane
    var popChat: (Bool) -> Void = { _ in }  // true: its own window; false: back in the right pane
    var openPage: (String) -> Void = { _ in }  // show a workspace's page in the dashboard (the inbox's Open workspace)
    @Published var missed = 0  // replies and pings that landed while the chat was hidden: the dashboard toggle's badge
    @Published var greenlightEveryLoading = false  // the schedule is being read; greenlightEveryNote says why the last read or save failed
    @Published var ctxOpen = false  // the chat footer's Context popover
    var reloadFluid: () -> Void = {}
    // Empty: the bundled dashboard.html.
    @Published var customPage = false  // Settings' Source is Custom while its location field is still empty
    @Published var fluidURL = ud.string(forKey: "customPageURL") ?? "" { didSet { ud.set(fluidURL, forKey: "customPageURL") } }
    var proc: Process?
    // The greenlight automation's interval in minutes (0 = off). It lives in greenlightHome/automations.json; this is the last value read.
    @Published var greenlightEvery = ud.object(forKey: "greenlightEvery") as? Int ?? 30
    @Published var greenlightEveryNote = ""

    func loadGreenlightEvery() {
        var line = ""
        greenlightEveryLoading = true
        ssh("\(py)/auto.py show greenlight", onLine: { line += $0 }, onExit: { [weak self] code in
            let a = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
            let m = (a?["schedule"] as? String).flatMap { $0.hasPrefix("*/") ? Int($0.dropFirst(2).prefix { $0.isNumber }) : nil }
            DispatchQueue.main.async {
                guard let self else { return }
                self.greenlightEveryLoading = false
                guard code == 0, let a else { self.greenlightEveryNote = "Couldn't read the schedule from \(host)."; return }
                self.greenlightEvery = (a["enabled"] as? Bool ?? true) ? m ?? self.greenlightEvery : 0
                self.greenlightEveryNote = ""
                ud.set(self.greenlightEvery, forKey: "greenlightEvery")
            }
        })
    }

    func setGreenlightEvery(_ m: Int) {
        greenlightEvery = m
        ud.set(m, forKey: "greenlightEvery")
        ssh("\(py)/auto.py edit greenlight " + (m == 0 ? "--disabled" : "--enabled --schedule '*/\(m) * * * *'"),
            onExit: { [weak self] code in DispatchQueue.main.async { self?.greenlightEveryNote = code == 0 ? "" : "Couldn't save the schedule on \(host)." } })
    }

    var isClaude: Bool { harnessOf(model) == "claude" }
    var waiting: [Question] { questions.filter { $0.answer == nil } }
    /// The waiting pings that name workspaces, for the dashboard (mc.bell's third argument).
    var asked: [[String: Any]] {
        waiting.compactMap { q in
            guard let ws = q.ws, !ws.isEmpty else { return nil }
            return ["id": q.id.uuidString, "text": q.text, "options": q.options, "ts": q.ts.timeIntervalSince1970, "ws": ws]
        }
    }
    var askedJSON: String {
        (try? JSONSerialization.data(withJSONObject: asked, options: [.sortedKeys])).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
    }

    /// A reply typed into a notification banner.
    func reply(to about: String, _ text: String) {
        unread = max(0, unread - 1)
        if let q = waiting.first(where: { $0.text == about }) { answer(q, text) }
        else { send("(Replying to your notification: \"\(about)\")\n\(text)", shown: text) }
    }

    /// Answers an inbox question (chip, reply field, banner reply or chat chip). Any chat answer closes the other chat questions.
    func answer(_ q: Question, _ text: String, send doSend: Bool = true) {
        for i in questions.indices where questions[i].answer == nil && (questions[i].id == q.id || (!q.ping && !questions[i].ping)) {
            questions[i].answer = text
        }
        if replyingTo == q.id { replyingTo = nil; replyText = "" }
        if doSend { send(q.ping ? "(Replying to your question: \"\(q.text)\")\n\(text)" : text, shown: text) }
    }

    /// An answer chip under a message in the chat.
    func choose(_ m: Msg, _ option: String) {
        if let q = question(for: m), q.answer == nil { answer(q, option) } else { send(option) }
    }

    /// The inbox question a chat reply asked: the same text, else (the question saved in other words) the newest reply offering the same choices.
    func question(for m: Msg) -> Question? {
        guard !m.options.isEmpty else { return nil }
        if let q = questions.last(where: { !$0.ping && $0.text == m.text }) { return q }
        guard msgs.last(where: { !$0.options.isEmpty }) === m else { return nil }
        return questions.last { !$0.ping && $0.options == m.options }
    }

    /// Mic button / voice shortcut: start recording, or stop and transcribe into the input box (not sent).
    func toggleVoice() {
        if voice.on || voice.starting { return }  // voice mode has the mic, or is about to
        if recording != nil { stopVoice() } else if !transcribing { startVoice() }
    }

    func startVoice() {
        AVCaptureDevice.requestAccess(for: .audio) { [self] ok in DispatchQueue.main.async {
            guard ok else { return self.msgs.append(Msg(.note, "Microphone access is off: System Settings → Privacy & Security → Microphone → Greenlight.")) }
            let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1,
                                           AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false]
            guard let r = try? AVAudioRecorder(url: voiceFile, settings: settings), r.record() else {
                return self.msgs.append(Msg(.note, "Couldn't start the microphone."))
            }
            r.isMeteringEnabled = true
            self.recorder = r
            self.recording = Date()
            self.meter = Timer.scheduledTimer(withTimeInterval: 0.06, repeats: true) { [weak self] _ in
                r.updateMeters()
                self?.levels = Array((self?.levels ?? []).dropFirst()) + [max(0, min(1, (r.averagePower(forChannel: 0) + 50) / 45))]
            }
        }}
    }

    func stopVoice() {
        recorder?.stop()
        recorder = nil
        meter?.invalidate()
        recording = nil
        levels = levels.map { _ in 0 }
        transcribing = true
        transcribe(voiceFile, engine: engine) { text, err in DispatchQueue.main.async {
            self.transcribing = false
            if let text, !text.isEmpty { self.input += (self.input.isEmpty ? "" : " ") + text }
            else { self.msgs.append(Msg(.note, "Voice: \(err ?? "heard nothing").")) }
        }}
    }

    func doneNaming() {
        name = name.trimmingCharacters(in: .whitespaces)
        if name.isEmpty { name = "Greenlight" }
        editingName = false
    }

    func submit() {
        let t = input.trimmingCharacters(in: .whitespacesAndNewlines), fs = files
        guard !t.isEmpty || !fs.isEmpty else { return }  // files alone are a message
        if reading > 0 { return fileSays("Still reading your files: send in a moment.") }
        if !t.isEmpty && input.first?.isWhitespace != true { sent = [Sent(t: t, at: Date())] + sent.filter { $0.t != t }.prefix(499) }  // ignorespace
        input = ""; files = []; fileNote = ""
        if vim { vimKeys.mode = vimStart }
        let q = t.isEmpty ? nil : waiting.last(where: { !$0.ping })  // typing answers the open chat question
        if let q, fs.isEmpty { answer(q, t) } else { if let q { answer(q, t, send: false) }; send(t, files: fs) }
    }

    var hmatches: [(s: Sent, pos: [Int])] {
        guard let q = hq?.trimmingCharacters(in: .whitespaces), !q.isEmpty else { return sent.prefix(8).map { ($0, []) } }
        let ms: [(s: Sent, pos: [Int], score: Int, i: Int)] = sent.enumerated().compactMap { i, e in fuzzy(q, e.t).map { (e, $0.pos, $0.score, i) } }
        return ms.sorted { $0.score != $1.score ? $0.score > $1.score : $0.i < $1.i }.prefix(50).map { ($0.s, $0.pos) }
    }
    /// What ↑ walks: with a typed draft, only prompts starting with it (zsh's up-line-or-beginning-search).
    var walk: [Sent] { let pre = hp == -1 ? input : hsaved; return sent.filter { $0.t.hasPrefix(pre) && $0.t != pre } }
    func recall(_ i: Int) -> Bool {
        let w = walk
        guard i >= -1, i < w.count else { return false }
        if hp == -1 { hsaved = input }
        recalling = true; input = i == -1 ? hsaved : w[i].t; recalling = false
        hp = i; toEnd = true
        return true
    }
    func useHist(_ t: String?) { hq = nil; if let t { input = t; toEnd = true } }
    /// ↑↓ and ⌃R in the chat field, and the search field's keys while ⌃R is open. True = handled.
    func historyKey(_ e: NSEvent, _ tv: NSTextView) -> Bool {
        if editingName || inboxOpen || settingsOpen { return false }
        let m = e.modifierFlags.intersection([.command, .control, .option, .shift]), k = Int(e.keyCode), ctrl = m == .control
        if hq != nil {
            let ms = hmatches, n = ms.count
            if (m.isEmpty && k == kVK_DownArrow) || (ctrl && (k == kVK_ANSI_R || k == kVK_ANSI_N)) { hsel = n > 0 ? (hsel + 1) % n : 0 }
            else if (m.isEmpty && k == kVK_UpArrow) || (ctrl && (k == kVK_ANSI_P || k == kVK_ANSI_S)) { hsel = n > 0 ? (hsel - 1 + n) % n : 0 }
            else if m.isEmpty && [kVK_Return, kVK_ANSI_KeypadEnter, kVK_Tab].contains(k) { useHist(hsel < n ? ms[hsel].s.t : nil) }
            else if k == kVK_Escape || (ctrl && k == kVK_ANSI_G) { useHist(nil) }
            else { return false }
            return true
        }
        if ctrl && k == kVK_ANSI_R { hsel = 0; hq = ""; return true }
        let s = tv.string as NSString, r = tv.selectedRange()
        // readline keys macOS lacks. ⌃U/⌃W kill through the mark, so ⌃Y yanks them like the built-in ⌃K
        if ctrl && (k == kVK_ANSI_U || k == kVK_ANSI_W) {
            let before = s.substring(to: r.location) as NSString
            let nl = before.range(of: "\n", options: .backwards).location
            let from = k == kVK_ANSI_U ? (nl == NSNotFound ? 0 : nl + 1)
                : before.range(of: #"\S*\s*$"#, options: .regularExpression).location
            if from < r.location {
                tv.setSelectedRange(NSRange(location: from, length: 0)); tv.setMark(nil)
                tv.setSelectedRange(NSRange(location: r.location, length: 0)); tv.deleteToMark(nil)
            }
            return true
        }
        if ctrl && k == kVK_ANSI_C { if input.isEmpty && run != nil { stop() } else { input = "" }; return true }  // empty field: stop the reply
        if m == .option && k == kVK_ANSI_Period, let w = sent.first?.t.split(whereSeparator: \.isWhitespace).last {  // ⌥. : last word of the previous prompt
            tv.insertText(String(w), replacementRange: tv.selectedRange()); return true
        }
        guard m.isEmpty else { return false }
        if k == kVK_Return || k == kVK_ANSI_KeypadEnter, let last = sent.first?.t {  // like bash's histverify: see !! / !$ expanded, ↵ again sends
            let x = expandBang(input, last)
            if x != input { input = x; toEnd = true; return true }
        }
        // ↑ only from the first line, ↓ only from the last
        if k == kVK_UpArrow, !s.substring(to: r.location).contains("\n") { return recall(hp + 1) }
        if k == kVK_DownArrow, hp >= 0, !s.substring(from: NSMaxRange(r)).contains("\n") { return recall(hp - 1) }
        if k == kVK_Escape, hp >= 0, !vim { return recall(-1) }
        return false
    }

    func send(_ text: String, shown: String? = nil, files fs: [ChatFile] = []) {
        active()
        if run != nil || clearing > 0 { queue.append(Queued(text: text, files: fs)); return }  // after Clear's `new`: it deletes attachments
        let you = [shown ?? text, fs.map { "📎 \($0.name)" }.joined(separator: "  ")].filter { !$0.isEmpty }.joined(separator: "\n")
        msgs.append(Msg(.you, you))
        var payload = voice.on ? voiceNote + text : text
        let new = attached.filter { !$0.sent }  // each docked hex goes over once; the chat session keeps it
        if !new.isEmpty {
            payload = "The user dragged these workspaces from the dashboard into the chat; this message is about them:\n"
                + new.map(\.text).joined(separator: "\n") + "\n\n" + text
            for i in attached.indices { attached[i].sent = true }
        }
        if !focus.isEmpty && focus != focusSent {  // only when it changed: the chat session remembers the last one
            payload = "(Greenlight, not typed by the user: they were last in \(focus). \"This\", \"here\" or \"it\" likely means that.)\n" + payload
            focusSent = focus
        }
        if fs.isEmpty { return start(Run(shown ?? text), chatSend, stdin: payload) }
        // chat.py send --attach: the message and the files as one JSON object (attach.py)
        let body = (try? JSONSerialization.data(withJSONObject: ["text": payload, "files": fs.map(\.json)])).map { String(decoding: $0, as: UTF8.self) } ?? ""
        let r = Run(you); r.files = fs; r.text = shown ?? text
        start(r, chatSend + " --attach", stdin: body)
    }

    // The idle check-in: at the Mac but nothing done in Greenlight (chat, a card's button, typing in a Basement terminal) for
    // checkInMins, greenlight asks the chat what to work on next, once per quiet stretch.
    // ponytail: work outside Greenlight (an editor, a browser) doesn't count; count pushes from the scan if it nags then
    @Published var checkInMins = UserDefaults.standard.object(forKey: "checkInMins") as? Int ?? 30 { didSet { UserDefaults.standard.set(checkInMins, forKey: "checkInMins") } }
    var lastActive = Date(), checkedIn = false
    func active() { lastActive = Date(); checkedIn = false; if session > 0 { delegate.greenlight.active() } }  // the check-in is the main chat's
    func checkIn() {
        let atMac = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!) < 120
        guard checkInMins > 0, !checkedIn, run == nil, clearing == 0, atMac, Date().timeIntervalSince(lastActive) > Double(checkInMins * 60) else { return }
        checkedIn = true
        msgs.append(Msg(.note, "Nothing done here in \(checkInMins) minutes: asking what's next."))
        let r = Run("What to work on next"); r.checkIn = true
        start(r, chatSend, stdin:
            "(Greenlight's idle check-in, not typed by the user: they're at their Mac but haven't done anything in Greenlight for \(checkInMins) minutes.) "
            + "From the board, suggest which 1–3 workspaces they should work on next, most valuable first: for each, where it is in its lifecycle, "
            + "why now, and the one next action, with its MR link. A few lines. End with an Options line naming the workspaces.")
    }

    // The watch: a workspace that newly needs the user gets the chat a quiet turn, and it asks only if it's worth asking.
    @Published var watch = UserDefaults.standard.object(forKey: "watch") as? Bool ?? true { didSet { UserDefaults.standard.set(watch, forKey: "watch") } }
    var focus = "", focusSent = ""  // the window you were last in, described for Chief (noteFocus)
    // The projects every window shows (one pick for all of them) as JSON, while one is up: Chief's BOARD and the watch have only
    // these. nil: every project (none picked, or no window up: you're in the menubar alone). App.pickScope.
    var scope: () -> String? = { nil }
    var chatSend: String {
        let only = scope().map { " --only \(shellQuote($0))" } ?? ""
        return "\(chief) send --model \(model) --effort \(effort) --name \(shellQuote(name))" + only + (verbose ? " --verbose" : "")
    }
    var pucks: [[String: Any]] = [], picks = 0  // the latest snapshot's workspaces; picks: the newest highlight asked for
    @Published var agents: [Agent] = []  // the agents in your workspaces: the activity feed is live while one works
    var needs: [String: String]?, fresh = [String: String](), watchedAt = Date.distantPast
    func watched(_ pucks: [[String: Any]]) {
        let (now, new) = newNeeds(needs, pucks)
        needs = now
        fresh = fresh.merging(new) { $1 }.filter { now[$0.key] != nil }  // one that stopped needing you before the turn is dropped
        watchTurn()
    }
    func watchTurn() {  // ponytail: one turn per 5 minutes at most, everything new batched; a knob if it nags or lags
        let (ask, hold) = inScope(fresh, pucks, scope())  // another project's waits: the chat speaks for the windows you're in
        guard watch, !ask.isEmpty, run == nil, clearing == 0, queue.isEmpty, Date().timeIntervalSince(watchedAt) > 300 else { return }
        watchedAt = Date()
        let list = ask.sorted { $0.key < $1.key }.map { "- \($0.key): \($0.value)" }.joined(separator: "\n")
        fresh = hold
        let r = Run("Watching the workspaces"); r.watch = true
        start(r, chatSend, stdin:
            "(Greenlight's watch, not typed by the user: these workspaces just started needing the user.)\n\(list)\n"
            + "Look into them (refresh the workspaces if the board is stale). If one needs a decision or an answer only the user can give, "
            + "ask one short question about it, with its MR link and a final Options line. Otherwise reply exactly: (nothing)")
    }

    func compact() {
        guard run == nil, clearing == 0, isClaude else { return }
        start(Run("Compact context"), "\(chief) compact --model \(model)", stdin: "")
    }

    var clearing = 0  // Clear's `chat.py new`s still running: a send waits for all of them
    func clear(keepQueue: Bool = false) {
        if run != nil { run?.files = []; stop() }  // a new conversation: its files don't come back
        if !keepQueue { queue = [] }
        clearing += 1
        ssh("\(chief) new", onExit: { [weak self] _ in
            guard let self else { return }
            self.clearing -= 1
            guard self.clearing == 0 else { return }
            if self.run == nil, !self.queue.isEmpty { let q = self.queue.removeFirst(); self.send(q.text, files: q.files) }
        })
        for i in attached.indices { attached[i].sent = false }  // the new conversation hasn't seen them
        msgs = [Msg(.note, "Context cleared. New conversation.")]
        focusSent = ""  // the new conversation hasn't heard where you are
        picks += 1; light([])  // nor lit anything
        if voice.on { voice.wait("Listening", "New conversation. Context cleared.") }
        questions.removeAll { !$0.ping && $0.answer == nil }  // their conversation is gone
        context = 0; window = 0; run?.meterStale = true  // the next harness reports its own window
    }

    func stop() {
        guard let r = run, !r.stopping else { return }
        r.stopping = true
        r.status = "Stopping"; r.thinking = false
        ssh("\(chief) stop")
    }

    private func start(_ r: Run, _ remote: String, stdin: String) {
        run = r
        tps = 0; outTok = 0
        log(.chief, name, .status, r.watch ? "watch: \(r.question)" : r.checkIn ? "check-in: \(r.question)" : "asked: \(r.question)")
        if !r.watch && !r.checkIn { voice.began(r) }
        proc = ssh(remote, stdin: stdin, onLine: { [weak self] l in self?.event(l, r) }, onExit: { [weak self] code in
            guard let self, self.run === r, !r.gotDone else { return }
            self.finish(r, ok: false, text: r.stopping ? "Stopped." : "Lost the connection to \(host) (exit \(code)).")
        })
    }

    private func event(_ l: String, _ r: Run) {
        guard run === r, let d = l.data(using: .utf8),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let e = j["e"] as? String else { return }
        connected = true
        switch e {
        case "start":
            r.status = "Starting \(models.first { $0.1 == j["engine"] as? String }?.0 ?? "Claude")"
            log(.chief, name, .status, "started on \(host): \(j["engine"] as? String ?? "claude") \(j["model"] as? String ?? "")")
        case "status":
            if !r.stopping { r.status = j["text"] as? String ?? r.status; r.thinking = j["thinking"] as? Bool ?? false; voice.status(byline(r, now: Date(), lead: true)) }
            if let t = j["text"] as? String, t != r.steps.last?.title { log(.chief, name, .status, t) }  // a step's own title is its call line
        case "step":
            let s = Step(id: j["id"] as? String ?? UUID().uuidString, title: j["title"] as? String ?? "", detail: j["detail"] as? String ?? "")
            r.steps.append(s); r.call = j["call"] as? String ?? ""
            if !r.stopping { r.status = s.title; r.thinking = false; voice.status(byline(r, now: Date(), lead: true)) }
            log(.chief, name, .call, r.call.isEmpty ? [s.title, s.detail].filter { !$0.isEmpty }.joined(separator: ": ") : r.call)
            if let input = j["input"] as? String, !r.call.contains(input) { log(.chief, name, .input, input) }  // the whole call, when the line cut it short
        case "step_done":
            if let i = r.steps.lastIndex(where: { $0.id == j["id"] as? String }) {  // the newest: a rerun's codex reuses item ids
                r.steps[i].ok = j["ok"] as? Bool ?? true
                r.steps[i].out = j["out"] as? String ?? ""
                r.steps[i].end = Date()
                let out = j["full"] as? String ?? r.steps[i].out, ok = r.steps[i].ok == true
                log(.chief, name, ok ? .output : .failed, "\(ok ? "" : "failed · ")\(fmt(Date().timeIntervalSince(r.steps[i].start)))" + (out.isEmpty ? " · no output" : "\n" + out))
            }
        case "plan":
            let n = j["total"] as? Int ?? 0, k = j["done"] as? Int ?? 0, t = j["title"] as? String ?? ""
            r.plan = n > 0 ? Plan(done: k, total: n, title: t, eta: j["eta"] as? Double, at: Date()) : nil
            if !r.stopping { voice.status(byline(r, now: Date(), lead: true)) }
            if n > 0 { log(.chief, name, .status, "plan: \(k) of \(n) done" + (t.isEmpty ? "" : " · \(t)")) }
        case "think": log(.chief, name, .think, j["delta"] as? String ?? "")
        case "text_reset": r.live = ""; voice.said = 0
        case "text":  // a whole message (codex, opencode) shows but isn't spoken yet: it may be a progress note the reply restates;
            r.live += j["delta"] as? String ?? ""  // finished() speaks the reply (from said = 0) at done
            if !r.watch && !r.checkIn && j["whole"] as? Bool != true { voice.live(r) }
        case "usage":
            if let c = j["context"] as? Int, c > 0, !r.meterStale { context = c }
            if let t = j["tps"] as? Double { tps = t; if t > 0 { lastTps = t } }
            if let o = j["out"] as? Int { outTok = o }
        case "done":
            r.gotDone = true
            if let c = j["context"] as? Int, c > 0, !r.meterStale { context = c }
            let ok = j["ok"] as? Bool ?? false, w = j["window"] as? Int ?? 0
            if w > 0 || ok, !r.meterStale { window = w }  // a good turn without a window (codex unread, opencode, pi): unknown, not the last harness's
            var text = j["text"] as? String ?? ""
            if r.question == "Compact context" { text = text.isEmpty ? "Compacted. Context is now \(kfmt(context))." : text }
            finish(r, ok: ok, text: r.stopping ? "Stopped." : text)
        default: break
        }
    }

    private func finish(_ r: Run, ok: Bool, text: String) {
        log(.chief, name, ok ? .status : .failed, (ok ? "replied in \(fmt(Date().timeIntervalSince(r.start)))" : "ended: \(text)")
            + (outTok > 0 ? " · ↓ \(kfmt(outTok)) tokens" : "") + (ok && r.watch && text.lowercased().hasPrefix("(nothing)") ? " · nothing to ask" : ""))
        if r.watch && (text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased().hasPrefix("(nothing)") || !ok) {  // nothing to ask: no trace
            run = nil; tps = 0
            if !queue.isEmpty && clearing == 0 { let q = queue.removeFirst(); send(q.text, files: q.files) }  // after Clear: its own exit sends them
            return
        }
        if !ok && !r.files.isEmpty {  // not sent, or sent and failed: the files come back to try again, ahead of any added since, as many as fit
            var back = files  // files added since stay; the failed send's join them as far as they fit
            for f in r.files where back.count < ChatFile.maxFiles && back.reduce(f.data.count, { $0 + $1.data.count }) <= ChatFile.maxTotal && !back.contains(where: { $0.id == f.id }) { back.append(f) }
            files = back
            if input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { input = r.text }  // and what was said about them
            let out = r.files.filter { f in !back.contains { $0.id == f.id } }.map(\.name)
            fileSays(out.isEmpty ? "Your files are back above the field, to send again."
                     : "Not sent and no room beside the files you added: \(out.joined(separator: ", ")). Attach them again to send them.")
        }
        let st = staged(text)  // a ```stage block is for the stage, never the transcript
        let (body, options) = splitOptions(st.text.isEmpty ? "(no reply)" : st.text)
        if !r.stopping && ok { voice.finished(st, body) } else if !r.watch && !r.checkIn { voice.failed(body) }
        let m = Msg(.greenlight, body)
        m.steps = r.steps.map { var s = $0; if s.end == nil { s.end = Date() }; return s }
        m.duration = Date().timeIntervalSince(r.start)
        m.options = options
        m.failed = !ok
        msgs.append(m)
        if ok && !options.isEmpty { questions.append(Question(text: body, options: options, ping: false)) }
        if ok && !r.stopping && !st.text.isEmpty {  // light up the hexes it's about, until the next reply (cleared now, so a failed call can't leave old ones lit)
            picks += 1
            let n = picks
            light([])
            if let h = st.hexes { light(resolveHexes(h, pucks)) }  // Chief said which: no guessing
            else { pickHexes(body, pucks, asked: asksToShow(r.question)) { [weak self] names in if n == self?.picks { self?.light(names) } } }
        }
        run = nil
        tps = 0
        if ok && !r.stopping && !r.watch && !r.checkIn, let w = st.console { delegate.toConsole(w) }
        if ok && st.reset {  // [chat: reset]: after this reply, which says so; what's queued goes to the new conversation, once it's clear
            clearing += 1  // now, so nothing sent before the clear goes to the old conversation
            DispatchQueue.main.async { [weak self] in self?.clearing -= 1; self?.clear(keepQueue: true) }
        }
        if !popoverShown() && !r.stopping {
            missed += 1
            notify(title: r.checkIn ? "\(name): what to work on next" : r.watch ? "\(name) asks" : "Done: \(r.question.prefix(60))", body: String(body.prefix(240)), subtitle: r.checkIn || r.watch ? nil : fmt(m.duration))
        }
        if !queue.isEmpty && clearing == 0 && !(ok && st.reset) { let q = queue.removeFirst(); send(q.text, files: q.files) }  // a reset sends it once clear
    }

    // MARK: pings

    var seen: Int {  // outbox lines already shown, so pings sent while the Mac slept still arrive
        get { UserDefaults.standard.object(forKey: "seen") as? Int ?? -1 }
        set { UserDefaults.standard.set(newValue, forKey: "seen") }
    }

    func tailOutbox() {
        let from = seen < 0 ? "$(wc -l < $f)" : "\(seen)"
        ssh("f=\(greenlightHome)/outbox.jsonl; touch $f; n=\(from); echo \"#start $n\"; tail -n +$((n+1)) -F $f 2>/dev/null",
            onLine: { [weak self] l in self?.ping(l) },
            onExit: { [weak self] _ in  // network drop, sleep, host reboot: reconnect
                self?.connected = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 15) { self?.tailOutbox() }
            })
    }

    func ping(_ l: String) {
        if l.hasPrefix("#start ") { seen = Int(l.dropFirst(7)) ?? 0; connected = true; return }
        seen += 1
        guard let d = l.data(using: .utf8), let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
        if let k = j["resolve"] as? String {  // greenlight noticed the question no longer applies: close it quietly
            let why = j["why"] as? String ?? "no longer needed"
            for i in questions.indices where questions[i].answer == nil && questions[i].key == k {
                questions[i].answer = "(resolved: \(why))"
                if replyingTo == questions[i].id { replyingTo = nil }
            }
            return
        }
        guard let raw = j["text"] as? String else { return }
        let (text, options) = splitOptions(raw)  // pings live in the bell inbox + banner only, not the chat
        let key = j["key"] as? String
        let ws = (j["ws"] as? [String])?.filter { !$0.isEmpty }
        if questions.contains(where: { $0.answer == nil && $0.text == text }) { return }  // already in the inbox
        var fresh = true  // a newer ping about the same workspace replaces the open one
        for i in questions.indices where key != nil && questions[i].answer == nil && questions[i].key == key {
            questions[i].answer = "(superseded)"
            if replyingTo == questions[i].id { replyingTo = nil }
            fresh = false
        }
        questions.append(Question(text: text, options: options, ping: true, ts: (j["ts"] as? Double).map { Date(timeIntervalSince1970: $0) } ?? Date(), key: key,
                                  ws: ws?.isEmpty == false ? ws : nil))
        if !popoverShown() && fresh { unread += 1; missed += 1 }
        notify(title: name, body: text, subtitle: nil, reply: true, id: key)
    }

    /// Same `id` (a workspace key) replaces that workspace's earlier banner in Notification Center instead of stacking.
    func notify(title: String, body: String, subtitle: String?, reply: Bool = true, id: String? = nil) {
        if selftest { return }  // fake pings shouldn't pop banners on the real Mac
        let c = UNMutableNotificationContent()
        c.title = title
        if let subtitle { c.subtitle = subtitle }
        c.body = body
        c.sound = .default
        if session == 0 { c.categoryIdentifier = "greenlight" }  // a banner reply goes to the main chat: a second window's has none
        c.userInfo = ["text": body]
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id ?? UUID().uuidString, content: c, trigger: nil))
    }
}

// MARK: views

struct StepRow: View, Looked {
    let s: Step
    let now: Date
    let current: Bool
    var n = 0  // 1-based, for HUD's 01… and Editorial's 1…; ponytail: 0 (MsgView's list doesn't pass it) falls back to a mark
    var body: some View {
        let (glyph, ink, face, w) = mark, live = s.ok == nil
        HStack(alignment: .firstTextBaseline, spacing: Theme.sp3) {
            Text(glyph).font(face).foregroundColor(ink).frame(minWidth: w, alignment: .leading)
                .shadow(color: live && (lk.id == "orb" || lk.id == "hud") ? ink.opacity(0.7) : .clear, radius: 4).accessibilityLabel(s.ok == true ? "Done" : s.ok == false ? "Failed" : "Running")
            VStack(alignment: .leading, spacing: 3) {
                Text(s.title).font(lk.id == "editorial" ? sys(size: Theme.fs4, design: .serif) : ui(lk.id == "zen" ? 14 : Theme.fs3, mono && current ? .bold : .regular))
                    .italic(lk.id == "editorial" && current).foregroundColor(current ? fg : lk.id == "zen" ? dim : sub)
                if !s.detail.isEmpty {
                    Text(s.detail).font(sys(size: Theme.fs1, design: .monospaced)).foregroundColor(sub)
                        .lineLimit(2).padding(.horizontal, Theme.sp2).padding(.vertical, 3)
                        .background(RoundedRectangle(cornerRadius: mono || lk.id == "hud" ? 0 : Theme.r1).fill(fg.opacity(0.06)))  // reads on the card or off it
                }
                if !s.out.isEmpty { Text(s.out).font(ui(Theme.fs1)).foregroundColor(dim).lineLimit(2) }
            }
            Spacer(minLength: 4)
            if lk.id == "hud" { Text(s.ok == true ? "OK" : s.ok == false ? "ERR" : "RUN").font(sys(size: 10, design: .monospaced)).tracking(1.2).foregroundColor(ink) }
            Text(fmt((s.end ?? now).timeIntervalSince(s.start))).font((lk.id == "hud" ? sys(size: Theme.fs1, design: .monospaced) : ui(Theme.fs1)).monospacedDigit()).foregroundColor(current ? fg : dim)
        }
        .padding(.horizontal, Theme.sp3).padding(.vertical, lk.id == "zen" ? 6 : Theme.sp1)
        .textSelection(.enabled)
    }
    /// Chrome in the look's face: mono for Phosphor.
    func ui(_ size: CGFloat, _ w: Font.Weight = .regular) -> Font { sys(size: size, weight: w, design: mono ? .monospaced : .default) }
    /// The look's leading mark: glyph, ink, font, width. ponytail: steps arrive as they start, so the designs' pending row
    /// (○, NEXT, [  ]) never happens here; an unfinished step is running, a failed one gets `crashed`.
    var mark: (String, Color, Font, CGFloat) {
        let st = s.ok == true ? 0 : s.ok == nil ? 1 : 2  // done, running, failed
        switch lk.id {
        case "conversation": return (["✓", "●", "✕"][st], [done, accent, crashed][st], ui(Theme.fs3, .semibold), 14)
        case "onair": return (["DONE", "LIVE", "FAIL"][st], [accent, hx("FF6B73"), crashed][st], sys(size: 10, weight: .heavy), 34)
        case "phosphor": return (["[ok]", "[..]", "[!!]"][st], st == 2 ? crashed : fg, ui(Theme.fs3, st == 1 ? .bold : .regular), 32)
        case "editorial": return (n > 0 ? "\(n)" : ["✓", "•", "✕"][st], st == 2 ? crashed : current ? accent : fg, sys(size: Theme.fs5, weight: .semibold, design: .serif), 18)
        case "hud": return (n > 0 ? String(format: "%02d", n) : "▸", st == 2 ? crashed : accent, sys(size: Theme.fs1, design: .monospaced), 18)
        case "zen": return ("●", [dim.opacity(0.45), accent, crashed][st], sys(size: 7), 6)
        default: return ("●", st == 2 ? crashed : accent, sys(size: 10), 8)  // Orb: amber dots, the running one glowing
        }
    }
}

let feedClock: DateFormatter = { let f = DateFormatter(); f.dateFormat = "HH:mm:ss"; return f }()

/// A feed line's mark and ink, in the given look colors. Shared by the chat's log and the voice looks' tail.
func feedMark(_ k: FeedLine.Kind, accent: Color, dim: Color, failed: Color, agent: Color, move: Color) -> (String, Color) {
    switch k {
    case .status: return ("·", dim)
    case .call: return ("⏺", accent)
    case .input: return ("↳", dim)
    case .output: return ("⎿", dim)
    case .failed: return ("✕", failed)
    case .think: return ("✻", dim)
    case .said: return ("“", agent)
    case .agent: return ("●", agent)
    case .move: return ("→", move)
    case .action: return ("▸", move)
    }
}

/// Verbose activity's live log, between the conversation and the composer: a header (live dot, filter, follow, Clear, fold)
/// over the newest lines, monospaced. Folded, it's the header and the newest line. Click a long line to read all of it.
struct FeedPanel: View, Looked {
    @ObservedObject var f: Feed
    @ObservedObject var c: Core
    var body: some View {
        let lines = f.shown, live = c.run != nil || c.agents.contains { $0.state == "working" }
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: Theme.sp2) {
                Button { f.open.toggle() } label: {
                    HStack(spacing: Theme.sp1) {
                        Image(systemName: f.open ? "chevron.down" : "chevron.right").font(sys(size: Theme.glyph, weight: .bold)).frame(width: 10)
                        Circle().fill(live ? accent : dim.opacity(0.5)).frame(width: 6, height: 6).accessibilityHidden(true)
                        Text("Live log").font(sys(size: Theme.fs1, weight: .semibold))
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).foregroundColor(sub).accessibilityLabel(f.open ? "Fold the live log" : "Open the live log")
                if f.open {
                    ForEach([nil] + FeedLine.Src.allCases.map { Optional($0) }, id: \.self) { s in
                        Button { f.only = s } label: {
                            Text(s?.rawValue ?? "All").font(sys(size: Theme.fs1, weight: f.only == s ? .semibold : .regular))
                                .foregroundColor(f.only == s ? fg : dim).padding(.horizontal, 6).padding(.vertical, 2)
                                .background(RoundedRectangle(cornerRadius: Theme.r1).fill(f.only == s ? fg.opacity(0.08) : .clear))
                        }.buttonStyle(.plain).help(s == nil ? "Everything" : s == .chief ? "\(c.name)'s own turn: calls, their output, thinking" : s == .agents ? "Your workspaces' agents" : "Greenlight's moves and the dashboard's buttons")
                    }
                    Spacer(minLength: 0)
                    Button { f.paused.toggle() } label: { Image(systemName: f.paused ? "play.fill" : "pause.fill").font(sys(size: Theme.glyph)) }
                        .buttonStyle(.plain).foregroundColor(dim).help(f.paused ? "Follow the newest line" : "Stop following, to read").accessibilityLabel(f.paused ? "Follow" : "Pause following")
                } else if let l = lines.last {
                    row(l, limit: 1).frame(maxWidth: .infinity, alignment: .leading)
                } else { Spacer(minLength: 0) }
                Button("Clear") { f.clear() }.buttonStyle(.plain).font(sys(size: Theme.fs1)).foregroundColor(dim).disabled(f.lines.isEmpty)
            }.padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp1 + 2)
            if f.open {
                ScrollViewReader { sv in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 3) {
                            if lines.isEmpty {
                                Text(f.only == nil ? "Nothing yet. \(c.name)'s calls, your agents and Greenlight's moves show here as they happen." : "Nothing from \(f.only!.rawValue) yet.")
                                    .font(sys(size: Theme.fs1)).foregroundColor(dim)
                            }
                            ForEach(lines) { l in
                                row(l, limit: f.unfolded.contains(l.id) ? nil : l.kind == .think ? 3 : 6)
                                    .onTapGesture { if f.unfolded.contains(l.id) { f.unfolded.remove(l.id) } else { f.unfolded.insert(l.id) } }
                            }
                            Color.clear.frame(height: 1).id("end")
                        }.padding(.horizontal, Theme.sp3).padding(.bottom, Theme.sp2)
                    }.frame(height: 190)
                        .onAppear { sv.scrollTo("end", anchor: .bottom) }
                        .onChange(of: lines.last?.id) { if !f.paused { sv.scrollTo("end", anchor: .bottom) } }
                        .onChange(of: lines.last?.text.count) { if !f.paused { sv.scrollTo("end", anchor: .bottom) } }  // thinking grows in place
                }
            }
        }.background(fg.opacity(0.035)).textSelection(.enabled)
    }
    func row(_ l: FeedLine, limit: Int?) -> some View {
        let (m, ink) = feedMark(l.kind, accent: accent, dim: dim, failed: crashed, agent: working, move: done)
        let who = l.src == .chief ? accent : l.src == .agents ? working : done
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(feedClock.string(from: l.ts)).foregroundColor(dim.opacity(0.8))
            Text(m).foregroundColor(ink).frame(width: 12, alignment: .center)
            Text("\(Text(l.who).foregroundColor(who).bold()) \(Text(l.text).foregroundColor(l.kind == .think || l.kind == .status ? dim : l.kind == .failed ? crashed : fg).italic(l.kind == .think))")
                .lineLimit(limit).truncationMode(.tail).fixedSize(horizontal: false, vertical: true)
        }.font(sys(size: Theme.fs1, design: .monospaced)).accessibilityElement(children: .combine)
    }
}

/// Verbose activity in a voice look: the log's newest lines under the live line, one line each, in the look's colors.
struct FeedTail: View {
    @ObservedObject var f: Feed
    let n: Int, fg: Color, dim: Color, accent: Color, size: CGFloat
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if f.lines.isEmpty { Text("Live log: waiting for activity").foregroundColor(dim) }
            ForEach(f.lines.suffix(n)) { l in
                let (m, ink) = feedMark(l.kind, accent: accent, dim: dim, failed: hx("E5392F"), agent: accent, move: accent)
                Text("\(Text(m).foregroundColor(ink)) \(Text(l.who).foregroundColor(fg).bold()) \(Text(l.text.split(separator: "\n").first.map(String.init) ?? "").foregroundColor(l.kind == .think || l.kind == .status ? dim : fg))")
                    .lineLimit(1).truncationMode(.tail)
            }
        }.font(sys(size: size, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).accessibilityElement(children: .combine)
    }
}

struct RunCard: View, Looked {
    @ObservedObject var r: Run
    @ObservedObject var c: Core
    var body: some View {
        VStack(alignment: .leading, spacing: lk.id == "onair" ? 0 : Theme.sp2) {
            label
            framed(VStack(alignment: .leading, spacing: Theme.sp3) {
                head
                bar
                if !r.steps.isEmpty {
                    VStack(spacing: 0) {
                        ForEach(Array(r.steps.enumerated()), id: \.element.id) { i, s in
                            StepRow(s: s, now: c.now, current: i == r.steps.count - 1 && s.ok == nil, n: i + 1)
                        }
                    }.padding(.horizontal, -Theme.sp3)  // the rows carry their own inset (for MsgView's list); line them up with the head
                        .padding(.top, mono ? Theme.sp2 : 0)
                        .overlay(alignment: .top) { if mono { HLine().stroke(line, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1) } }
                }
            })
            if !r.live.isEmpty { liveText.padding(.top, lk.id == "onair" ? Theme.sp4 : 0) }
            if c.liveLine { activity }
        }
    }
    var activity: some View { Activity(c: c, r: r, fg: fg, dim: dim, accent: accent, size: Theme.fs2) }
    var state: String { r.stopping ? "stopping" : "working" }
    /// Chrome in the look's face: mono for Phosphor.
    func ui(_ size: CGFloat, _ w: Font.Weight = .regular) -> Font { sys(size: size, weight: w, design: mono ? .monospaced : .default) }
    func code(_ size: CGFloat, _ w: Font.Weight = .regular) -> Font { sys(size: size, weight: w, design: .monospaced) }
    func tag(_ s: String, _ fill: Color, _ ink: Color) -> some View {
        Text(s).font(sys(size: 10, weight: .heavy).monospacedDigit()).tracking(1.6).foregroundColor(ink).padding(.horizontal, 10).padding(.vertical, 4).background(fill)
    }
    /// "NAME · working" above the card.
    @ViewBuilder var label: some View {
        switch lk.id {
        case "onair": HStack(spacing: 0) { tag("\(c.name) · \(state)".uppercased(), accent, page); tag(stats, line, fg) }  // On Air's stats ride the tag
        case "phosphor": Text("\(Text("\(c.name.lowercased())@greenlight ").foregroundColor(dim))[\(state.uppercased())]").font(ui(Theme.fs3, .bold)).foregroundColor(fg)
        case "editorial": Text("\(c.name) · \(r.stopping ? "Stopping the press" : "Going to press")".uppercased()).font(sys(size: 10, weight: .semibold)).tracking(1.4).foregroundColor(accent)
        case "hud":
            HStack(spacing: Theme.sp3) { Text("\(c.name.uppercased())//RUN"); Text("▶ \(state.uppercased())").shadow(color: accent.opacity(0.55), radius: 5) }
                .font(code(Theme.fs1)).tracking(1.3).foregroundColor(accent)
        case "zen": Text("\(Text(c.name.lowercased()).foregroundColor(fg)) \(Text("· \(state)").foregroundColor(accent))").font(sys(size: Theme.fs3))
        case "conversation":
            HStack(spacing: 6) { Circle().fill(accent).frame(width: 7, height: 7).accessibilityHidden(true); Text("\(Text(c.name).fontWeight(.semibold).foregroundColor(fg)) · \(state)") }
                .font(sys(size: Theme.fs2)).foregroundColor(dim)
        default: Text("\(Text(c.name.uppercased()).foregroundColor(fg)) \(Text("· \(state.uppercased())").foregroundColor(accent))").font(sys(size: Theme.fs2, weight: .semibold)).tracking(1.7)
        }
    }
    /// The status line: a lead mark, the status and stats, Stop.
    @ViewBuilder var head: some View {
        if lk.id == "editorial" {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("IN PROGRESS").font(sys(size: Theme.fs1, weight: .bold)).tracking(1.5).foregroundColor(accent)
                Text(stats).font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(dim).lineLimit(2)
                Spacer(minLength: 4)
                stop
            }
            Text("\(r.status)\(Text("…").foregroundColor(accent))").font(sys(size: 24, weight: .semibold, design: .serif)).foregroundColor(fg).lineLimit(2)
        } else {
            if lk.id == "hud" { HStack { Text("RUN"); Spacer(); Text("LIVE").foregroundColor(accent) }.font(code(Theme.fs1)).tracking(1.5).foregroundColor(dim) }
            HStack(spacing: lk.id == "orb" ? 20 : Theme.sp3) {
                switch lk.id {
                case "orb": orbMark
                case "conversation": ProgressView().controlSize(.small)
                // ponytail: a still amber dot, not Breath: the chat shows in voice mode too (transcript), where Breath's live meter would swell it past the row
                case "zen": Circle().fill(accent).frame(width: 6, height: 6).shadow(color: accent.opacity(0.5), radius: 4).accessibilityHidden(true)
                default: EmptyView()
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(mono ? "> \(r.status.lowercased())" : r.status).lineLimit(1).foregroundColor(fg)
                        .font(lk.id == "onair" ? sys(size: Theme.fs5, weight: .heavy) : lk.id == "zen" ? sys(size: 20, weight: .light) : ui(lk.id == "conversation" ? 14 : mono ? Theme.fs3 : Theme.fs4, mono ? .bold : .semibold))
                    if lk.id != "onair" { Text(stats).font((lk.id == "hud" ? code(Theme.fs1) : ui(Theme.fs2)).monospacedDigit()).tracking(lk.id == "hud" ? 1 : 0).foregroundColor(dim).lineLimit(2) }
                }
                Spacer(minLength: 4)
                stop
            }
        }
    }
    /// Orb's lead: the amber orb with two soft rings. ponytail: static; the live stage's orb is the one that moves.
    var orbMark: some View {
        ZStack {
            Circle().fill(accent.opacity(0.07)).frame(width: 36, height: 36)  // the rings stay inside the card
            Circle().fill(accent.opacity(0.16)).frame(width: 28, height: 28)
            Circle().fill(RadialGradient(colors: [hx("FFD27A"), hx("F5A524"), hx("B4740E")], center: UnitPoint(x: 0.35, y: 0.35), startRadius: 0, endRadius: 11)).frame(width: 20, height: 20)
        }.frame(width: 36, height: 36).accessibilityHidden(true)
    }
    var stop: some View {
        let (t, ink, fill, edge, r0): (String, Color, Color, Color, CGFloat) = switch lk.id {
        case "conversation": ("Stop", crashedInk, bg, line, Theme.r1)
        case "onair": ("STOP", .white, hx("D1242F"), .clear, 0)
        case "phosphor": ("[ stop ]", fg, .clear, fg, 0)
        case "editorial": ("Stop the press", accent, .clear, .clear, 0)
        case "hud": ("[ STOP ]", fg, .clear, line, 0)
        case "zen": ("Stop", sub, .clear, .clear, 0)
        default: ("Stop", fg, .clear, line, 15)
        }
        let bare = lk.id == "editorial" || lk.id == "zen"
        return Button(action: c.stop) {
            Text(t).font(lk.id == "onair" ? sys(size: Theme.fs1, weight: .heavy) : lk.id == "hud" ? code(Theme.fs1, .bold) : lk.id == "editorial" ? sys(size: Theme.fs3, weight: .bold) : ui(Theme.fs2, lk.id == "conversation" ? .semibold : .regular))
                .tracking(lk.id == "onair" || lk.id == "hud" ? 1.5 : 0).foregroundColor(ink)
                .padding(.horizontal, bare ? 0 : mono ? Theme.sp2 : 14).padding(.vertical, bare ? 2 : 0).frame(minHeight: bare ? 24 : mono ? 26 : 30)
                .background(RoundedRectangle(cornerRadius: r0).fill(fill)).overlay(RoundedRectangle(cornerRadius: r0).stroke(edge))
                .overlay(alignment: .bottom) { if lk.id == "editorial" { Rectangle().fill(accent).frame(height: 1) } }
                .contentShape(Rectangle())
        }.buttonStyle(.plain).disabled(r.stopping).opacity(r.stopping ? 0.5 : 1).accessibilityLabel("Stop").help("Stop this run")
    }
    /// ponytail: no percentage is known, so progress stays indeterminate: the native linear bar tinted to the look, or for the
    /// block looks (On Air, Phosphor, HUD) a 3-cell window stepping along once a second off c.now (no extra timer).
    /// The designs' 62% isn't shown.
    func lit(_ i: Int) -> Bool { (i - Int(c.now.timeIntervalSince(r.start)) % 10 + 10) % 10 < 3 }
    @ViewBuilder var bar: some View {
        switch lk.id {
        case "onair": HStack(spacing: 3) { ForEach(0..<10, id: \.self) { i in Rectangle().fill(lit(i) ? accent : line).frame(height: 10) } }.accessibilityHidden(true)
        case "phosphor": Text("[" + String((0..<10).map { lit($0) ? "#" : "." }) + "]").font(ui(Theme.fs3)).foregroundColor(fg).accessibilityHidden(true)
        case "hud":
            HStack(spacing: 2) { ForEach(0..<10, id: \.self) { i in Rectangle().fill(lit(i) ? accent : accent.opacity(0.08)).frame(height: 8) } }
                .padding(2).overlay(Rectangle().stroke(line)).shadow(color: accent.opacity(0.35), radius: 4).accessibilityHidden(true)
        default:
            ProgressView().progressViewStyle(.linear).tint(lk.id == "zen" ? dim : accent).frame(height: lk.id == "zen" ? 2 : 4).clipped()
                .overlay(alignment: .bottom) { if lk.id == "editorial" { Rectangle().fill(line).frame(height: 1).offset(y: 2) } }
                .accessibilityLabel("Working")
        }
    }
    /// The card around head, bar and steps. ponytail: Conversation's is one flat card (no grey header strip, the bar inset).
    func framed<V: View>(_ v: V) -> some View {
        let open = lk.id == "editorial" || lk.id == "zen", r0: CGFloat = lk.id == "orb" ? Theme.r4 : lk.id == "conversation" ? 12 : 0
        let fill = open ? Color.clear : lk.id == "conversation" ? bg : card, edge = ["conversation", "phosphor", "hud"].contains(lk.id) ? line : .clear
        return v.shadow(color: mono ? fg.opacity(0.45) : .clear, radius: 3)  // Phosphor's glow, on the type not the box
            .padding(open ? 0 : mono ? Theme.sp3 : Theme.sp4).padding(.top, lk.id == "editorial" ? Theme.sp3 : 0)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: r0).fill(fill)).overlay(RoundedRectangle(cornerRadius: r0).stroke(edge))
            .overlay(alignment: .top) { if lk.id == "onair" || lk.id == "editorial" { Rectangle().fill(lk.id == "onair" ? accent : fg).frame(height: lk.id == "onair" ? 2 : 3) } }
            .overlay(Brackets().stroke(lk.id == "hud" ? accent : .clear, lineWidth: 2).allowsHitTesting(false).accessibilityHidden(true))
    }
    /// The reply as it streams, with the look's cursor.
    @ViewBuilder var liveText: some View {
        let cur = Text(mono ? " █" : lk.id == "zen" ? "▏" : " ▍").foregroundColor(lk.id == "onair" ? hx("D1242F") : accent)
        // the size and frame MsgView's reply has, so the text doesn't jump when the run lands
        let fs = ["orb": Theme.fs4, "hud": Theme.fs4, "editorial": Theme.fs4 + 1, "zen": Theme.fs4 + 1, "phosphor": Theme.fs3][lk.id] ?? Theme.fs3 + 1
        let rest = unmood(staged(r.live).text)
        let t = Text("\(mono ? Text("\(c.name.lowercased())> ").foregroundColor(dim) : Text(""))\(Text(md(rest)))\(cur)")
            .font(chatFace(size: fs)).textSelection(.enabled)
        Group {
            switch lk.id {
            case "conversation":
                let s = UnevenRoundedRectangle(topLeadingRadius: 4, bottomLeadingRadius: 16, bottomTrailingRadius: 16, topTrailingRadius: 16)
                t.foregroundColor(fg).padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2).background(s.fill(card)).overlay(s.stroke(line))
            case "onair":
                VStack(alignment: .leading, spacing: 0) {
                    Text(c.name.uppercased()).font(sys(size: 10, weight: .heavy)).tracking(1.6).foregroundColor(page).padding(.horizontal, Theme.sp2).padding(.vertical, 3).background(accent)
                    t.foregroundColor(page).padding(.horizontal, Theme.sp4).padding(.vertical, Theme.sp3).background(fg)
                }
            case "hud":
                VStack(alignment: .leading, spacing: 6) { Text("\(c.name.uppercased()) ▸").font(code(Theme.fs1)).tracking(1.3).foregroundColor(accent); t.foregroundColor(fg) }
                    .padding(.leading, Theme.sp3).overlay(alignment: .leading) { Rectangle().fill(accent).frame(width: 1) }
            default: t.foregroundColor(fg).shadow(color: mono ? fg.opacity(0.45) : .clear, radius: 3)
            }
        }
    }
    var stats: String {
        let n = r.steps.count, t = max(0, Int(c.now.timeIntervalSince(r.start))), tps = c.tps > 0 ? " · \(Int(c.tps)) tok/s" : ""
        let b = byline(r, now: c.now, lead: false), by = b.isEmpty ? "" : " · " + b  // the plan, time left, what just finished
        // ponytail: HUD's step number isn't lit apart from the rest of the line
        if lk.id == "hud" { return "STEP \(n == 0 ? "--" : String(format: "%02d", n)) · T+\(String(format: "%02d:%02d", t / 60, t % 60))\(by.uppercased())\(tps.uppercased())" }
        // the byline before time and speed where the line may wrap (the card is narrow, and those two are on the live line under
        // it too); last on On Air's one-line tag, so the clock isn't what's cut
        let clock = " · \(fmt(TimeInterval(t)))\(lk.id == "conversation" ? " elapsed" : "")\(tps)", step = n == 0 ? "No steps yet" : "Step \(n)"
        let s = lk.id == "onair" ? step + clock + by : step + by + clock
        return lk.id == "onair" ? s.uppercased() : mono || lk.id == "zen" ? s.lowercased() : s
    }
}

struct MsgView: View, Looked {
    @ObservedObject var m: Msg
    let c: Core
    let name: String  // passed in: MsgView doesn't observe Greenlight, which changes every second while working
    var ask: Question? = nil  // the inbox question this message asked, if any: open, or answered with which choice
    var body: some View {
        switch m.role {
        case .you: you
        case .note:
            Text(m.text).font(ui(Theme.fs1)).foregroundColor(dim).frame(maxWidth: .infinity)
        case .greenlight:
            let open = !m.options.isEmpty && ask != nil && ask?.answer == nil
            let (said, q) = open ? asked : (m.text, nil)
            VStack(alignment: .leading, spacing: Theme.sp4) {
                turn(said)  // the reply, then how the run went: the designs put it above any choices the reply ends on
                if !m.options.isEmpty { choices(open, q) }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // Each look's hand (the designs in .claude/design/chatthemes): switch on lk.id.
    // ponytail: left out of the designs: HUD's "Q-01", Phosphor's glow, On Air's bold lead paragraph, text2 on list bodies; Orb's dot is a "•" glyph.
    /// Chrome type: mono for Phosphor and HUD.
    func ui(_ size: CGFloat, _ w: Font.Weight = .regular) -> Font { sys(size: size, weight: w, design: lk.id == "phosphor" || lk.id == "hud" ? .monospaced : .default) }
    /// Chrome words in the look's case. ponytail: chrome only (and On Air/HUD chips), never what was said.
    func cased(_ s: String) -> String { ["onair", "hud"].contains(lk.id) ? s.uppercased() : ["phosphor", "zen"].contains(lk.id) ? s.lowercased() : s }
    /// Only Orb and Conversation round their corners.
    var round: Bool { lk.id == "orb" || lk.id == "conversation" }
    /// The reply's size: 15 Orb and HUD, 16 Editorial and Zen, 13 Phosphor, else 14.
    var fs: CGFloat { ["orb": Theme.fs4, "hud": Theme.fs4, "editorial": Theme.fs4 + 1, "zen": Theme.fs4 + 1, "phosphor": Theme.fs3][lk.id] ?? Theme.fs3 + 1 }
    func rule(_ c: Color) -> some View { Rectangle().fill(c).frame(width: 1).frame(maxWidth: .infinity, alignment: .leading) }

    @ViewBuilder var you: some View {
        let t = Text(m.text), f = chatFace(size: Theme.fs3 + 1)
        switch lk.id {
        case "phosphor":
            Text("\(Text("you> ").foregroundColor(dim))\(t)").font(chatFace(size: fs)).foregroundColor(fg).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        case "editorial":
            VStack(alignment: .leading, spacing: Theme.sp1) {
                Text("LETTERS · YOU ASKED").font(sys(size: 10, weight: .bold)).tracking(1.4).foregroundColor(dim)
                Text("“\(m.text)”").font(chatFace(size: Theme.fs5 + 1).italic()).foregroundColor(fg).textSelection(.enabled)
            }.frame(maxWidth: .infinity, alignment: .leading)
        case "hud":
            VStack(alignment: .leading, spacing: Theme.sp1) {
                Text("YOU ▸").font(ui(Theme.fs1)).tracking(1.3).foregroundColor(dim)
                t.font(f).foregroundColor(sub).textSelection(.enabled)
            }.padding(.leading, Theme.sp3).background(rule(line)).frame(maxWidth: .infinity, alignment: .leading)
        default:
            HStack {
                Spacer(minLength: 60)
                switch lk.id {
                case "onair":
                    VStack(alignment: .trailing, spacing: 0) {
                        Text("YOU").font(sys(size: 10, weight: .heavy)).tracking(1.6).foregroundColor(fg).padding(.horizontal, Theme.sp2).padding(.vertical, 3).background(line)
                        t.font(f).foregroundColor(fg).textSelection(.enabled).padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2).background(card)
                    }
                case "zen": t.font(f).foregroundColor(dim).multilineTextAlignment(.trailing).textSelection(.enabled)
                case "conversation":
                    t.font(f).foregroundColor(.white).textSelection(.enabled).padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2)
                        .background(UnevenRoundedRectangle(topLeadingRadius: 16, bottomLeadingRadius: 16, bottomTrailingRadius: 4, topTrailingRadius: 16).fill(accent))
                default:
                    t.font(f).foregroundColor(fg).textSelection(.enabled).padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2)
                        .background(RoundedRectangle(cornerRadius: 18).fill(card))
                }
            }
        }
    }

    /// Who's talking, what was said, how the run went.
    @ViewBuilder func turn(_ said: String) -> some View {
        let col = VStack(alignment: .leading, spacing: lk.id == "onair" ? 0 : Theme.sp2) {
            label
            if !said.isEmpty { reply(said) }
            work
        }
        switch lk.id {
        case "zen" where said.isEmpty && m.steps.isEmpty && m.duration == 0: EmptyView()  // nothing to show: no gap above the question
        case "conversation":
            HStack(alignment: .top, spacing: 10) {
                Text(String(name.prefix(1))).font(sys(size: Theme.fs2, weight: .semibold)).foregroundColor(accent).frame(width: 26, height: 26)
                    .background(Circle().fill(accent.opacity(0.12))).accessibilityLabel(name)
                col
            }
        case "phosphor": HStack(alignment: .firstTextBaseline, spacing: 0) { Text("\(name.lowercased())> ").font(chatFace(size: fs)).foregroundColor(dim); col }
        case "hud": col.padding(.leading, Theme.sp3).background(rule(accent))
        default: col
        }
    }
    @ViewBuilder var label: some View {
        switch lk.id {
        case "orb": Text(name.uppercased()).font(sys(size: Theme.fs1, weight: .semibold)).tracking(1.5).foregroundColor(dim)
        case "onair": Text(name.uppercased()).font(sys(size: 10, weight: .heavy)).tracking(1.6).foregroundColor(page).padding(.horizontal, Theme.sp2).padding(.vertical, 3).background(accent)
        case "editorial": Text("FROM THE DESK").font(sys(size: Theme.fs1, weight: .bold)).tracking(1.5).foregroundColor(accent)
        case "hud": Text("\(name.uppercased()) ▸").font(ui(Theme.fs1)).tracking(1.3).foregroundColor(accent)
        default: EmptyView()  // Conversation's avatar, Phosphor's prompt, Zen says nothing
        }
    }
    @ViewBuilder func reply(_ said: String) -> some View {
        box(unmood(said))
    }
    @ViewBuilder func box(_ said: String) -> some View {
        let t = MDText(text: said).font(chatFace(size: fs)).textSelection(.enabled)
        switch lk.id {
        case "onair":  // inverted: page ink on a text-colored card
            t.foregroundColor(m.failed ? hx("C62828") : page).padding(.horizontal, Theme.sp4).padding(.vertical, Theme.sp3).background(fg)  // failed: crashedInk's light half, the card is light
        case "conversation":
            let s = UnevenRoundedRectangle(topLeadingRadius: 4, bottomLeadingRadius: 16, bottomTrailingRadius: 16, topTrailingRadius: 16)
            t.foregroundColor(m.failed ? crashedInk : fg).padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2).background(s.fill(card)).overlay(s.stroke(line))
        default: t.foregroundColor(m.failed ? crashedInk : fg)
        }
    }

    /// The choices a reply ends on: in the look's "needs you" frame while open, a hairline once answered.
    func choices(_ open: Bool, _ q: String?) -> some View {
        let band = open && lk.id == "onair"  // ponytail: inset, not full-bleed as designed
        let chips = ForEach(m.options, id: \.self) { o in
            Button { c.choose(m, o) } label: { chip(o, open) }.buttonStyle(.plain).disabled(c.run != nil).accessibilityLabel(o)
        }
        let gap = lk.id == "zen" ? Theme.sp5 : lk.id == "editorial" ? Theme.sp4 : Theme.sp2
        let pad = lk.id == "zen" ? EdgeInsets(top: 2, leading: Theme.sp4, bottom: 2, trailing: 0)
            : open ? EdgeInsets(top: Theme.sp3, leading: Theme.sp4, bottom: Theme.sp3, trailing: Theme.sp4) : EdgeInsets(top: Theme.sp2, leading: Theme.sp2, bottom: Theme.sp2, trailing: Theme.sp2)
        return VStack(alignment: .leading, spacing: 0) {
            if band { needsTag }
            VStack(alignment: .leading, spacing: Theme.sp3) {
                if open {
                    if !band { needsTag }
                    if let q { question(q) }  // the question sits with its answers
                }
                if !open, let a = ask?.answer {  // answered: the choice made, the others folded away
                    HStack(spacing: Theme.sp2) {
                        Image(systemName: a == "(dismissed)" ? "xmark" : "checkmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(a == "(dismissed)" ? dim : done)
                        Text(cased(a == "(dismissed)" ? "Dismissed without an answer" : "You answered"))
                        if a != "(dismissed)" { Text(a).fontWeight(.semibold).foregroundColor(fg).lineLimit(2) }
                    }.font(ui(Theme.fs2)).foregroundColor(dim)
                } else {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: gap) { chips }
                        VStack(alignment: .leading, spacing: Theme.sp2) { chips }
                    }
                }
            }.padding(pad)
        }.frame(maxWidth: .infinity, alignment: .leading).background(box(open)).padding(.leading, lk.id == "conversation" ? 36 : 0)
    }
    @ViewBuilder func box(_ open: Bool) -> some View {
        let r = RoundedRectangle(cornerRadius: lk.id == "orb" ? 18 : round ? 12 : 0)
        switch (open, lk.id) {
        case (_, "zen"): rule(open ? accent : line)
        case (false, _): r.strokeBorder(line)
        case (_, "onair"): Rectangle().fill(accent)
        case (_, "phosphor"): Rectangle().fill(card).overlay(Rectangle().strokeBorder(fg, style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
        case (_, "editorial"): Rectangle().fill(card).overlay(alignment: .top) { Rectangle().fill(fg).frame(height: 3) }
        case (_, "hud"): Rectangle().fill(card).overlay(Rectangle().strokeBorder(line)).overlay(alignment: .top) { Rectangle().fill(accent).frame(height: 2) }
        default: r.fill(accent.opacity(0.06)).overlay(r.strokeBorder(accent, lineWidth: 1.5))
        }
    }
    @ViewBuilder var needsTag: some View {
        let id = lk.id
        let t = id == "hud" ? "▲ INPUT REQUIRED" : id == "phosphor" ? "-- needs your answer --" : id == "zen" ? "needs your answer" : id == "conversation" ? "Needs your answer" : "NEEDS YOUR ANSWER"
        let f = id == "zen" ? sys(size: Theme.fs2) : id == "phosphor" ? chatFace(size: fs, weight: .bold) : id == "onair" ? sys(size: Theme.fs2, weight: .heavy)
            : id == "conversation" ? sys(size: Theme.fs2, weight: .semibold) : ui(Theme.fs1, .bold)
        HStack(spacing: Theme.sp2) {
            if ["orb", "conversation", "onair"].contains(id) { Circle().fill(accent).frame(width: 7, height: 7) }
            Text(t)
        }.font(f).tracking(["orb", "editorial", "onair", "hud"].contains(id) ? 1.5 : 0).foregroundColor(id == "zen" ? dim : id == "phosphor" ? fg : accent)
            .padding(.horizontal, id == "onair" ? Theme.sp3 : 0).padding(.vertical, id == "onair" ? Theme.sp2 : 0).background(id == "onair" ? page : .clear)
    }
    @ViewBuilder func question(_ q: String) -> some View {
        let f = lk.id == "onair" ? chatFace(size: Theme.fs5 - 1, weight: .bold) : lk.id == "editorial" ? chatFace(size: Theme.fs5 + 1, weight: .semibold)
            : lk.id == "zen" ? chatFace(size: Theme.fs5, weight: .light) : chatFace(size: fs)
        let t = MDText(text: q).font(f).textSelection(.enabled).foregroundColor(lk.id == "onair" ? page : fg)
        if lk.id == "phosphor" { HStack(alignment: .firstTextBaseline, spacing: 0) { Text("? ").font(f).foregroundColor(dim); t } } else { t }
    }
    /// One choice; the first is the primary while the question's open.
    func chip(_ o: String, _ open: Bool) -> some View {
        let id = lk.id, p = open && o == m.options.first, bare = id == "editorial" || id == "zen"  // text-only choices
        let st: (ink: Color, fill: Color, edge: Color)
        switch id {
        case "conversation": st = p ? (Color.white, accent, Color.clear) : (fg, bg, line)
        case "onair": st = p ? (fg, page, Color.clear) : open ? (page, Color.clear, page) : (fg, Color.clear, line)
        case "phosphor": st = p ? (page, fg, Color.clear) : (fg, Color.clear, dim)
        case "hud": st = p ? (accent, accent.opacity(0.1), accent) : (sub, bg, line)
        case "editorial": st = (accent, Color.clear, Color.clear)
        case "zen": st = (p ? accent : sub, Color.clear, Color.clear)
        default: st = p ? (page, accent, Color.clear) : (fg, card, line)
        }
        let n = (m.options.firstIndex(of: o) ?? 0) + 1
        let t = id == "phosphor" ? "[\(n)] \(o)" : id == "hud" ? "[ \(o.uppercased()) ]" : id == "onair" ? o.uppercased() : o
        let f = id == "zen" ? sys(size: Theme.fs3 + 1) : id == "onair" ? sys(size: Theme.fs2, weight: .heavy) : ui(id == "hud" ? Theme.fs2 : Theme.fs3, p ? .semibold : .regular)
        let r = RoundedRectangle(cornerRadius: round ? 16 : 0)
        return Text(t).font(f).tracking(id == "hud" || id == "onair" ? 1.2 : 0).foregroundColor(st.ink)
            .padding(.horizontal, bare ? 0 : Theme.sp3).frame(minHeight: id == "hud" ? 34 : bare ? 26 : 32)
            .background(r.fill(st.fill)).overlay(r.strokeBorder(st.edge, lineWidth: id == "onair" ? 2 : 1))
            .overlay(alignment: .bottom) { if id == "editorial" { Rectangle().fill(accent).frame(height: p ? 2 : 1) } }
            .contentShape(r)
    }
    /// An open question's text split in two: what was said before it, and its last paragraph when that asks something.
    var asked: (String, String?) {
        let t = m.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let r = t.range(of: "\n\n", options: .backwards) else { return t.contains("?") ? ("", t) : (t, nil) }
        let last = String(t[r.upperBound...])
        return last.contains("?") ? (String(t[..<r.lowerBound]), last) : (t, nil)
    }
    @ViewBuilder var work: some View {
                if !m.steps.isEmpty || m.duration > 0 {
                    Button { m.expanded.toggle() } label: { worked.contentShape(Rectangle()) }.buttonStyle(.plain).padding(.top, lk.id == "onair" ? Theme.sp2 : 0)
                    if m.expanded && !m.steps.isEmpty {
                        VStack(spacing: 0) { ForEach(Array(m.steps.enumerated()), id: \.element.id) { i, s in StepRow(s: s, now: Date(), current: false, n: i + 1) } }
                            .padding(.vertical, Theme.sp2).background(RoundedRectangle(cornerRadius: round ? Theme.r2 : 0).fill(card)).padding(.top, lk.id == "onair" ? Theme.sp2 : 0)
                    }
                }
    }
    /// "Worked 1m 2s · 6 steps": a capsule (Orb, Conversation), a box (On Air, HUD), a line of text (Phosphor, Editorial, Zen).
    @ViewBuilder var worked: some View {
        let id = lk.id, ok = m.failed ? crashed : id == "orb" || id == "hud" ? accent : done
        switch id {
        case "phosphor":
            Text("[\(m.failed ? "err" : "ok")] \(cased(summary)) [\(m.expanded ? "-" : "+")]").font(ui(Theme.fs3)).foregroundColor(m.failed ? crashedInk : dim)
        case "zen":
            Text("\(cased(summary)) \(m.expanded ? "⌄" : "›")").font(sys(size: Theme.fs2)).foregroundColor(m.failed ? crashedInk : dim)
        case "hud":
            HStack(spacing: Theme.sp2) { Text(m.failed ? "✕ ERR" : "✓ OK").foregroundColor(ok); Text("\(cased(summary)) \(m.expanded ? "▾" : "▸")") }
                .font(ui(Theme.fs1)).tracking(0.9).foregroundColor(dim).padding(.horizontal, 10).frame(minHeight: 28).background(card).overlay(Rectangle().strokeBorder(line))
        default:
            let boxed = id != "editorial", r = RoundedRectangle(cornerRadius: round ? 15 : 0)
            HStack(spacing: Theme.sp2) {
                if boxed || m.failed { Image(systemName: m.failed ? "xmark" : "checkmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(ok) }
                Text(cased(summary))
                Image(systemName: m.expanded ? "chevron.down" : "chevron.right").font(sys(size: Theme.glyph, weight: .bold))
            }.font(id == "onair" ? sys(size: Theme.fs1, weight: .bold) : sys(size: Theme.fs2)).tracking(id == "onair" ? 1.1 : 0).foregroundColor(dim)
                .padding(.horizontal, boxed ? Theme.sp3 : 0).frame(minHeight: id == "orb" ? 30 : boxed ? 26 : 22)
                .background(r.fill(id == "conversation" ? bg : .clear)).overlay(r.strokeBorder(boxed ? line : .clear))
                .overlay(alignment: .bottom) { if !boxed { Rectangle().fill(line).frame(height: 1) } }
        }
    }
    var summary: String {
        let cmds = m.steps.filter { !$0.detail.isEmpty && $0.title != "Loaded skill" }.count
        var s = "Worked \(fmt(m.duration)) · \(m.steps.count) step\(m.steps.count == 1 ? "" : "s")"
        if cmds > 0 { s += " · \(cmds) command\(cmds == 1 ? "" : "s")" }
        return s
    }
}

/// Greenlight's markdown as blocks: paragraphs, and list items with a hanging indent (md() alone leaves "- " literal).
struct MDText: View, Looked {
    let text: String
    var body: some View {
        let bs = blocks, zen = lk.id == "zen"
        VStack(alignment: .leading, spacing: Theme.sp2) {
            ForEach(Array(bs.enumerated()), id: \.offset) { i, b in
                if let mark = b.mark {
                    HStack(alignment: .firstTextBaseline, spacing: Theme.sp2) {
                        if let t = bullet(mark, nth(bs, i)) { t.frame(minWidth: 12, alignment: .trailing) }
                        Text(md(b.text)).fixedSize(horizontal: false, vertical: true)
                    }.padding(.vertical, zen ? Theme.sp1 + 2 : 0).frame(maxWidth: zen ? .infinity : nil, alignment: .leading).overlay(alignment: .top) { if zen { Rectangle().fill(line).frame(height: 1) } }  // Zen: hairlines, no marks
                    .padding(.leading, CGFloat(b.indent) * Theme.sp4).padding(.top, b.gap ? 0 : -Theme.sp1)  // items 4pt apart, paragraphs 8
                } else {
                    Text(md(b.text)).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
    /// The list mark in the look's hand: Orb's amber dot, Phosphor's `*`, Editorial's red i. ii., HUD's 01 02, On Air's square, Zen none.
    func bullet(_ mark: String, _ n: Int) -> Text? {
        let dot = mark == "•"
        switch lk.id {
        case "zen": return nil
        case "orb": return Text(mark).foregroundColor(accent)
        case "phosphor": return Text(dot ? "*" : mark).foregroundColor(dim)
        case "editorial":  // ponytail: roman to xii, then digits
            let r = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"]
            return Text(dot ? (n <= r.count ? r[n - 1] : "\(n)") + "." : mark).bold().foregroundColor(accent)
        case "hud": return Text(dot ? String(format: "%02d", n) : mark).font(sys(size: Theme.fs2, design: .monospaced)).foregroundColor(n == 1 ? accent : dim)
        case "onair": return Text(dot ? "▪" : mark)  // in the inverted card's ink
        default: return Text(mark).foregroundColor(sub)
        }
    }
    /// Item i's place (from 1) in its list, counting back to the item that opened it.
    func nth(_ bs: [(mark: String?, indent: Int, text: String, gap: Bool)], _ i: Int) -> Int {
        var n = 0, j = i
        while j >= 0, bs[j].mark != nil, bs[j].indent >= bs[i].indent {
            if bs[j].indent == bs[i].indent { n += 1; if bs[j].gap { break } }
            j -= 1
        }
        return max(n, 1)
    }
    /// gap: a list item that starts a list (paragraph spacing above) rather than continuing one.
    var blocks: [(mark: String?, indent: Int, text: String, gap: Bool)] {
        var out: [(mark: String?, indent: Int, text: String, gap: Bool)] = [], para: [String] = [], fence = false
        func flush() { if !para.isEmpty { out.append((nil, 0, para.joined(separator: "\n"), true)); para = [] } }
        for line in text.components(separatedBy: "\n") {
            if line.trimmingCharacters(in: .whitespaces).hasPrefix("```") { fence.toggle(); para.append(line); continue }
            if fence { para.append(line); continue }
            if let r = line.range(of: #"^ *([-*+•]|\d+[.)]) +"#, options: .regularExpression) {
                let gap = !para.isEmpty || out.last?.mark == nil
                flush()
                let mark = line[r].trimmingCharacters(in: .whitespaces)
                out.append((mark.first?.isNumber == true ? mark : "•", line.prefix { $0 == " " }.count / 2, String(line[r.upperBound...]), gap))
            } else if line.trimmingCharacters(in: .whitespaces).isEmpty {
                flush()
            } else if para.isEmpty, line.hasPrefix("  "), let last = out.last, last.mark != nil {
                out[out.count - 1].text += " " + line.trimmingCharacters(in: .whitespaces)  // a wrapped item's continuation
            } else { para.append(line) }
        }
        flush()
        return out
    }
}

/// Instant hover hint shown above the footer, plus the native tooltip.
struct Tip: ViewModifier {
    @ObservedObject var c: Core
    let text: String
    func body(content: Content) -> some View {
        content.help(text).onHover { c.hint = $0 ? text : (c.hint == text ? "" : c.hint) }
    }
}

/// The bell's panel: questions waiting for an answer, newest last, then the last few answered.
struct Inbox: View {
    @ObservedObject var c: Core
    var body: some View {
        let w = c.waiting
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("\(Text("Questions waiting").fontWeight(.semibold))\(Text(" · \(w.count)").foregroundColor(dim))")
                Spacer()
                if let o = w.map(\.ts).min() { Text("oldest \(ago(o))").font(sys(size: Theme.fs1)).foregroundColor(dim) }
                Button { c.inboxOpen = false } label: {
                    Image(systemName: "xmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(dim).frame(width: 24, height: 24).contentShape(Rectangle())
                }.buttonStyle(.plain).help("Close (Esc)").accessibilityLabel("Close inbox")
            }.padding(.leading, Theme.sp4).padding(.trailing, Theme.sp2).padding(.vertical, Theme.sp2)
            Divider().overlay(line)
            if w.isEmpty {
                VStack(spacing: Theme.sp2) {
                    Image(systemName: "checkmark.circle").font(sys(size: Theme.fs6)).foregroundColor(Theme.done)
                    Text("Nothing waiting on you").fontWeight(.semibold)
                    Text("\(c.name) keeps working and lights the bell when it needs a decision.")
                        .font(sys(size: Theme.fs2)).foregroundColor(dim).multilineTextAlignment(.center)
                }.frame(maxWidth: .infinity).padding(.vertical, Theme.sp5).padding(.horizontal, Theme.sp4)
            } else {
                let rows = VStack(spacing: 0) {
                    ForEach(Array(w.enumerated()), id: \.element.id) { i, q in
                        if i > 0 { Divider().overlay(line) }
                        QuestionRow(c: c, q: q)
                    }
                }
                ViewThatFits(in: .vertical) { rows; ScrollView { rows } }
            }
            let done = c.questions.filter { $0.answer != nil }.suffix(3).reversed()
            if !done.isEmpty {
                Divider().overlay(line)
                VStack(alignment: .leading, spacing: Theme.sp2) {
                    Text("ANSWERED").font(sys(size: Theme.fs1, weight: .semibold)).foregroundColor(dim)
                    ForEach(Array(done)) { q in
                        HStack(spacing: Theme.sp2) {
                            Text(q.text).foregroundColor(dim).lineLimit(1)
                            Spacer(minLength: 8)
                            Text(q.answer ?? "").lineLimit(1).layoutPriority(1)
                        }.font(sys(size: Theme.fs2))
                    }
                }.padding(.horizontal, Theme.sp4).padding(.vertical, Theme.sp3)
            }
        }
        .background(RoundedRectangle(cornerRadius: Theme.r3).fill(card))
        .overlay(RoundedRectangle(cornerRadius: Theme.r3).stroke(edge))
        .clipShape(RoundedRectangle(cornerRadius: Theme.r3))
        .shadow(color: .black.opacity(theme.dark ? 0.5 : 0.12), radius: theme.dark ? 20 : 12, y: theme.dark ? 10 : 4)
        .accessibilityElement(children: .contain).accessibilityLabel("Questions waiting for you")
    }
}

struct QuestionRow: View {
    @ObservedObject var c: Core
    let q: Question
    var body: some View {
        let (mr, said) = label
        HStack(alignment: .top, spacing: Theme.sp2) {
            Circle().fill(Theme.needs)  // ping or chat, it waits on you
                .frame(width: 6, height: 6).padding(.top, 7)
            VStack(alignment: .leading, spacing: Theme.sp2) {  // question and choices line up with the title
            VStack(alignment: .leading, spacing: 2) {  // which task and MR, once; then where it came from and what else to do
                HStack(alignment: .firstTextBaseline, spacing: Theme.sp2) {
                    Text(about ?? q.ws.map { $0.joined(separator: ", ") } ?? (q.ping ? "\(c.name) ping" : "Your chat with \(c.name)"))
                        .font(sys(size: Theme.fs3, weight: .semibold)).foregroundColor(fg).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    if let mr { Text(mr).font(sys(size: Theme.fs2)).foregroundColor(sub) }
                    Spacer(minLength: 0)
                    Button { c.answer(q, "(dismissed)", send: false) } label: {  // drop this one, top-right, like the panel's own ✕
                        Image(systemName: "xmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(dim).frame(width: 20, height: 20).contentShape(Rectangle())
                    }.buttonStyle(.plain).help("Dismiss without answering").accessibilityLabel("Dismiss")
                }
                HStack(spacing: Theme.sp1) {
                    Text("\(q.ping ? "Ping" : "Asked in the chat") · \(ago(q.ts))")
                    if let ws = about {
                        Text("·")
                        Button("Open workspace") { c.openPage(ws) }.buttonStyle(.plain).foregroundColor(sub).help("Show \(ws) in the dashboard")
                    }
                }.font(sys(size: Theme.fs1)).foregroundColor(dim)
            }
            Text(md(said)).font(chatFace(size: Theme.fs3)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            if !q.options.isEmpty { Text("Choose to send").font(sys(size: Theme.fs1)).foregroundColor(dim) }
            let chips = Group {  // equal weight, as the chat's chips: greenlight's Options line carries no recommendation
                ForEach(q.options, id: \.self) { o in AnswerChip(title: o) { c.answer(q, o) } }
                if c.replyingTo != q.id {
                    Button(q.options.isEmpty ? "Reply" : "Other…") { c.replyText = ""; c.replyingTo = q.id }.buttonStyle(.plain).font(sys(size: Theme.fs2)).foregroundColor(sub)
                        .padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2).frame(minHeight: 30).overlay(Capsule().stroke(edge)).contentShape(Capsule())
                }
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: Theme.sp2) { chips }
                VStack(alignment: .leading, spacing: Theme.sp2) { chips }
            }
            if c.replyingTo == q.id {
                HStack(spacing: Theme.sp2) {
                    TextField("Reply to \(c.name)…", text: $c.replyText).textFieldStyle(.plain).onSubmit(send)
                    Button(action: send) {
                        Image(systemName: "arrow.up").font(sys(size: Theme.fs1, weight: .bold)).frame(width: 24, height: 24)
                            .background(RoundedRectangle(cornerRadius: Theme.r1).fill(accent)).foregroundColor(bg)
                    }.buttonStyle(.plain).accessibilityLabel("Send reply")
                }.padding(.leading, Theme.sp3).padding(.trailing, 2).padding(.vertical, 2)
                    .background(RoundedRectangle(cornerRadius: Theme.r1).fill(bg)).overlay(RoundedRectangle(cornerRadius: Theme.r1).stroke(accent))
            }
            }
        }.padding(Theme.sp3)
    }
    /// The MR a ping names as "<workspace> (!N)", and its text without that when it's only a label ("<workspace> (!N): …").
    var label: (String?, String) {
        guard let ws = about, q.text.hasPrefix(ws + " (!") else { return (nil, q.text) }
        let from = q.text.index(q.text.startIndex, offsetBy: ws.count + 2)
        guard let close = q.text[from...].firstIndex(of: ")") else { return (nil, q.text) }
        let rest = q.text[q.text.index(after: close)...]
        return (String(q.text[from..<close]), rest.hasPrefix(": ") ? String(rest.dropFirst(2)) : q.text)
    }
    func send() {
        let t = c.replyText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { c.answer(q, t) }
    }
    /// The workspace a ping is about (its key; "@…" keys are groups, so their one named workspace if any).
    var about: String? { q.key.flatMap { $0.hasPrefix("@") ? nil : $0 } ?? (q.ws?.count == 1 ? q.ws?.first : nil) }
}

/// An inbox answer: a restrained amber outline, filled on hover; choosing sends it.
struct AnswerChip: View {
    let title: String, action: () -> Void
    @StateObject private var hover = Flag()  // not @State: see the top of the file
    var body: some View {
        Button(title, action: action).buttonStyle(.plain).font(sys(size: Theme.fs2))
            .padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2).frame(minHeight: 30)
            .background(Capsule().fill(hover.on ? Theme.needs.opacity(0.14) : surface))
            .overlay(Capsule().stroke(Theme.needs.opacity(hover.on ? 0.85 : 0.5))).contentShape(Capsule()).onHover { hover.on = $0 }
    }
}
final class Flag: ObservableObject { @Published var on = false }

struct HintTab: View {
    let text: String
    var body: some View {
        let shape = UnevenRoundedRectangle(bottomLeadingRadius: Theme.r2, bottomTrailingRadius: Theme.r2)
        HStack(spacing: Theme.sp2) {
            Circle().fill(accent).frame(width: 6, height: 6)
            Text(text).font(sys(size: Theme.fs1)).foregroundColor(fg).fixedSize()
        }.padding(.horizontal, Theme.sp3).padding(.top, Theme.sp1).padding(.bottom, Theme.sp2)
            .background(shape.fill(bg)).overlay(shape.stroke(edge))
            .preferredColorScheme(theme.dark ? .dark : .light)
    }
}

/// Settings over the whole window (dashboard and chat), not just the chat pane.
/// The bell's inbox, over the dashboard under the top bar's bell: App.inboxHost.
struct InboxOverlay: View {
    @ObservedObject var c: Core
    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.clear.contentShape(Rectangle()).onTapGesture { c.inboxOpen = false }  // no scrim: the dashboard stays readable
            VStack(spacing: 0) { Inbox(c: c); Spacer(minLength: 0) }.frame(width: 420).padding(.top, 50).padding(.trailing, Theme.sp3).padding(.bottom, Theme.sp4)
            Button("") { c.inboxOpen = false }.keyboardShortcut(.cancelAction).opacity(0).frame(width: 0, height: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .foregroundColor(fg)
        .preferredColorScheme(theme.dark ? .dark : .light)
        .font(sys(size: Theme.fs3))
    }
}

/// One accent choice: a 20pt disk in a 32pt target, its name on hover, an outline on hover or focus.
struct AccentDot: View {
    @ObservedObject var c: Core
    let h: String
    var ring = fg, rim = edge  // Settings passes its own theme's
    @FocusState private var focused: Bool
    static let names = ["F5A524": "Amber", "FF7A45": "Orange", "F7768E": "Rose", "FF79C6": "Pink", "BD93F9": "Purple", "7AA2F7": "Blue", "2AC3DE": "Cyan", "9ECE6A": "Green"]
    var body: some View {
        let on = h == accentHex, name = Self.names[h] ?? "#\(h)", hover = c.hint == name  // Tip sets the hint while hovered
        Button { c.restyle { accentHex = h } } label: {
            Circle().fill(Color(nsColor: hex(h))).frame(width: 20, height: 20)
                .overlay(Circle().stroke(on ? ring : hover || focused ? rim : Color.clear, lineWidth: 2).padding(-3))
                .overlay { if on { Image(systemName: "checkmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(.black.opacity(0.7)) } }
                .frame(width: 32, height: 32).contentShape(Rectangle())
        }.buttonStyle(.plain).focused($focused)
            .modifier(Tip(c: c, text: name)).accessibilityLabel("\(name) accent (#\(h))\(on ? ", selected" : "")")
    }
}

struct SettingsWindowView: View {
    @ObservedObject var c: Core
    var body: some View { SettingsView(c: c) }
}

/// Settings: a sidebar of panes, the pane, and a footer with Done. Its own light/dark (the header's toggle), from lightTheme or darkTheme.
struct SettingsView: View {
    @ObservedObject var c: Core
    @ObservedObject var preview = LivePreview.shared
    @StateObject var rs = RulesStore()
    /// (pane, SF Symbol, words the sidebar's search also matches). This Mac's panes, then greenlight's on its host.
    static let mac = [("General", "slider.horizontal.3", "startup opens dock icon assistant name"),
                      ("Appearance", "circle.lefthalf.filled", "theme dark light accent color colour font size text dashboard source custom"),
                      ("Chat & voice", "bubble.left", "model effort dictation voice whisper engine shortcut"),
                      ("Keyboard", "keyboard", "vim shortcuts keys"),
                      ("Terminal", "terminal", "tab names basement orbs floors sidebar slide ride animation up button"),
                      ("Hosts", "server.rack", "ssh host state code relaunch connection")]
    static let gl = [("Automation", "bolt", "scan interval background agents pages suggestions"),
                     ("Projects", "folder", "folders default folder new terminals open"),
                     ("Repositories", "arrow.triangle.branch", "repo lifecycle stages rules prompts auto-run project solo merge"),
                     ("Instructions", "doc.text", "standing rules"),
                     ("Memory", "bookmark", "notes learned")]
    static let wide: Set = ["Projects", "Repositories", "Memory"]

    // Settings' own theme: these shadow the app's globals of the same names in every pane below.
    var theme: Theme { c.settingsDark ? darkTheme : lightTheme }
    var accentNS: NSColor { accentHex.map(hex) ?? theme.accent }
    var accent: Color { Color(nsColor: accentNS) }
    /// A glyph on the accent: dark on a light accent (Greenlight's amber), white on a dark one (GitHub's blue).
    var onAccent: Color {
        let c = accentNS.usingColorSpace(.sRGB) ?? accentNS
        return 0.299 * c.redComponent + 0.587 * c.greenComponent + 0.114 * c.blueComponent > 0.6 ? Color(nsColor: hex("111113")) : .white
    }
    var bg: Color { Color(nsColor: theme.bg) }
    var fg: Color { Color(nsColor: theme.fg) }
    var card: Color { theme.mix(0.04) }
    var surface: Color { theme.mix(0.07) }
    var line: Color { theme.mix(0.1) }
    var edge: Color { theme.mix(0.17) }
    var dim: Color { theme.mix(theme.dark ? 0.62 : 0.7) }
    var sub: Color { theme.mix(0.82) }
    var hostLabel: String { host.isEmpty || host == "local" ? "this Mac" : host }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: Theme.sp2) {
                Text("Settings").font(sys(size: Theme.fs3, weight: .semibold)).foregroundColor(fg)
                Text("/").font(sys(size: Theme.fs3)).foregroundColor(dim)
                Text(c.settingsTab).font(sys(size: Theme.fs3)).foregroundColor(sub)
                Spacer()
                TextField("Search settings", text: $rs.q).textFieldStyle(.roundedBorder).frame(width: 220)
                Picker("Settings theme", selection: $c.settingsDark) {
                    Image(systemName: "sun.max").accessibilityLabel("Light").tag(false)
                    Image(systemName: "moon").accessibilityLabel("Dark").tag(true)
                }.pickerStyle(.segmented).labelsHidden().fixedSize().help("Settings in light or dark").padding(.leading, Theme.sp2)
            }.padding(.leading, 80).padding(.trailing, Theme.sp4).frame(height: 52)  // the window's own buttons sit left of the title
            Divider().overlay(line)
            HStack(spacing: 0) {
                sidebar
                Divider().overlay(line)
                if Self.wide.contains(c.settingsTab) { widePane } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 22) { pane }
                            .padding(.horizontal, 40).padding(.top, 28).padding(.bottom, 40)
                            .frame(maxWidth: 720, alignment: .leading)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            Divider().overlay(line)
            HStack(spacing: Theme.sp2) {
                Image(systemName: foot.0).font(sys(size: Theme.fs2)).foregroundColor(dim)
                Text(foot.1).font(sys(size: Theme.fs2)).foregroundColor(dim)
                Spacer()
                Button("Done") { c.settingsOpen = false }.keyboardShortcut(.cancelAction).buttonStyle(.borderedProminent).controlSize(.large)
            }.padding(.leading, 20).padding(.trailing, Theme.sp4).frame(height: 60)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).background(bg).ignoresSafeArea()  // the header is the title bar
            .environment(\.colorScheme, theme.dark ? .dark : .light).tint(accent)
            .onAppear { rs.load(); rs.projs = delegate.savedProjects }
            .onChange(of: c.settingsTab) { rs.projs = delegate.savedProjects }  // the Basement's editor may have changed them
            .onChange(of: c.settingsOpen) { rs.projs = delegate.savedProjects; if !c.settingsOpen { preview.stop() } }
    }

    var foot: (String, String) {
        switch c.settingsTab {
        case "Projects": return ("internaldrive", "Projects are saved on this Mac as you change them.")
        case "Repositories": return ("server.rack", "Project and Auto-run apply right away; the lifecycle, rules and prompts are saved on \(hostLabel) when you press Save.")
        case "Instructions", "Memory": return ("server.rack", "Stored on \(hostLabel) in \(greenlightHome). Changes apply when you press Save.")
        default: return ("checkmark", "Changes on this page apply right away.")
        }
    }

    var sidebar: some View {
        let q = rs.q.trimmingCharacters(in: .whitespaces).lowercased()
        let hit = { (t: (String, String, String)) in q.isEmpty || "\(t.0) \(t.2)".lowercased().contains(q) }
        let mac = Self.mac.filter(hit), gl = Self.gl.filter(hit)
        return ScrollView {
            VStack(alignment: .leading, spacing: 2) {
                if !mac.isEmpty {
                    Text("This Mac").font(sys(size: Theme.fs1, weight: .semibold)).foregroundColor(dim).padding(.horizontal, 8).padding(.top, 6).padding(.bottom, 4)
                }
                ForEach(mac, id: \.0) { navButton($0) }
                if !gl.isEmpty {
                    HStack(spacing: 10) {  // greenlight's own panes, on its host
                        Circle().fill(Theme.done).frame(width: 8, height: 8)
                        VStack(alignment: .leading, spacing: 1) {
                            Text("Greenlight").font(sys(size: Theme.fs3, weight: .semibold)).foregroundColor(fg)
                            Text("on \(hostLabel)").font(sys(size: Theme.fs1)).foregroundColor(dim)
                        }
                        Spacer(minLength: 0)
                    }.padding(.horizontal, 10).padding(.vertical, Theme.sp2)
                        .background(RoundedRectangle(cornerRadius: Theme.r2).fill(bg)).overlay(RoundedRectangle(cornerRadius: Theme.r2).stroke(line))
                        .accessibilityElement(children: .combine).padding(.top, 14).padding(.bottom, 6)
                }
                ForEach(gl, id: \.0) { navButton($0) }
                if mac.isEmpty && gl.isEmpty { note("No settings match “\(rs.q)”.").padding(Theme.sp2) }
            }.padding(.horizontal, 10).padding(.vertical, Theme.sp3)
        }.frame(width: 228).background(card)
    }

    func navButton(_ t: (String, String, String)) -> some View {
        let on = c.settingsTab == t.0
        return Button { c.settingsTab = t.0; rs.adding = false } label: {
            HStack(spacing: 10) {
                tile(t.1, 22, on: on)
                Text(t.0).font(sys(size: Theme.fs3, weight: on ? .semibold : .regular)).foregroundColor(fg)
                Spacer(minLength: 0)
                if let n = count(t.0) { Text("\(n)").font(sys(size: Theme.fs1).monospacedDigit()).foregroundColor(dim) }
            }.padding(.horizontal, 6).frame(height: 32).contentShape(Rectangle())
                .background(RoundedRectangle(cornerRadius: 7).fill(on ? accent.opacity(0.15) : Color.clear))
        }.buttonStyle(.plain).accessibilityLabel("\(t.0) settings").accessibilityAddTraits(on ? .isSelected : [])
    }
    func count(_ t: String) -> Int? { t == "Projects" ? rs.projs.count : t == "Repositories" ? allRepos.count : t == "Memory" ? rs.notes.count : nil }

    @ViewBuilder var pane: some View {
        switch c.settingsTab {
        case "Appearance": appearance
        case "Chat & voice": chat
        case "Keyboard": keyboard
        case "Terminal": terminal
        case "Hosts": hosts
        case "Automation": automation
        case "Instructions": instructions
        default: general
        }
    }
    /// The list-and-detail panes fill the window: their own list column and scrolling.
    @ViewBuilder var widePane: some View {
        switch c.settingsTab {
        case "Projects": projects
        case "Repositories": repositories
        default: memory
        }
    }

    // MARK: the panes' parts

    /// A pane's (or nav item's) SF Symbol on a rounded tile: the accent when it's the one you're on.
    func tile(_ icon: String, _ size: CGFloat, on: Bool = true) -> some View {
        Image(systemName: icon).font(.system(size: size * 0.48, weight: .semibold)).foregroundColor(on ? onAccent : sub)
            .frame(width: size, height: size).background(RoundedRectangle(cornerRadius: size * 0.27).fill(on ? accent : surface)).accessibilityHidden(true)
    }
    static func icon(_ t: String) -> String { (mac + gl).first { $0.0 == t }?.1 ?? "gearshape" }

    func heading(_ t: String, _ lede: String?, @ViewBuilder trailing: () -> some View = { EmptyView() }) -> some View {
        HStack(alignment: .center, spacing: 14) {
            tile(Self.icon(t), 42)
            VStack(alignment: .leading, spacing: 3) {
                Text(t).font(sys(size: 20, weight: .bold)).foregroundColor(fg)
                if let lede { Text(lede).font(sys(size: Theme.fs3)).foregroundColor(sub).fixedSize(horizontal: false, vertical: true).frame(maxWidth: 560, alignment: .leading) }
            }
            Spacer(minLength: 0)
            trailing()
        }
    }

    /// Helper text: subordinate but not disabled-looking in either theme.
    func note(_ t: String) -> some View { Text(t).font(sys(size: Theme.fs2)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true) }

    /// A small accent label: "new terminals", "yours".
    func chip(_ t: String) -> some View {
        Text(t).font(sys(size: Theme.fs1, weight: .medium)).foregroundColor(accent)
            .padding(.horizontal, Theme.sp2).frame(height: 20).background(Capsule().fill(accent.opacity(0.12)))
    }

    /// A section: its title above a rounded group of rows; each row draws the hairline above it.
    func box<V: View>(_ title: String? = nil, aside: String? = nil, @ViewBuilder _ v: () -> V) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            if let title {
                HStack(alignment: .firstTextBaseline, spacing: Theme.sp2) {
                    Text(title).font(sys(size: Theme.fs2, weight: .semibold)).foregroundColor(sub)
                    Spacer(minLength: Theme.sp3)
                    if let aside { Text(aside).font(sys(size: Theme.fs1)).foregroundColor(dim).multilineTextAlignment(.trailing) }
                }.padding(.horizontal, 10)
            }
            VStack(alignment: .leading, spacing: 0) { v() }.padding(.top, -1)  // the first row's hairline tucks under the card's border
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 12).fill(card)).overlay(RoundedRectangle(cornerRadius: 12).stroke(line))
                .clipShape(RoundedRectangle(cornerRadius: 12))
        }
    }
    func ruled(_ v: some View) -> some View { v.overlay(alignment: .top) { Rectangle().fill(line).frame(height: 1) } }

    /// Label and hint on the left, the control on the right.
    func row<V: View>(_ label: String, _ hint: String? = nil, @ViewBuilder _ v: () -> V) -> some View {
        let text = VStack(alignment: .leading, spacing: 2) {
            Text(label).font(sys(size: Theme.fs3, weight: .medium)).foregroundColor(fg)
            if let hint { note(hint) }
        }
        // The label keeps at least 180pt beside the control; a control too wide for that (Effort, Accent, Scan every) moves under it.
        return ruled(ViewThatFits(in: .horizontal) {
            HStack(spacing: 20) {
                text.frame(minWidth: 180, idealWidth: 180, maxWidth: .infinity, alignment: .leading)
                HStack(spacing: Theme.sp2) { v() }.layoutPriority(1)
            }
            VStack(alignment: .leading, spacing: 10) { text.frame(maxWidth: .infinity, alignment: .leading); HStack(spacing: Theme.sp2) { v() } }
        }.padding(.horizontal, Theme.sp4).padding(.vertical, 10).frame(minHeight: 48))
    }

    func toggleRow(_ title: String, _ help: String, _ on: Binding<Bool>) -> some View {
        row(title, help) { Toggle(title, isOn: on).labelsHidden().toggleStyle(.switch) }
    }

    /// Rows that only matter while the row above is on: indented under an L, dimmed while it's off.
    func dependent<V: View>(off: Bool, @ViewBuilder _ v: () -> V) -> some View {
        VStack(alignment: .leading, spacing: 0) { v() }.padding(.leading, 22)
            .overlay(alignment: .topLeading) {
                Path { p in p.move(to: CGPoint(x: 0.5, y: 0)); p.addLine(to: CGPoint(x: 0.5, y: 24)); p.addLine(to: CGPoint(x: 12, y: 24)) }
                    .stroke(edge, lineWidth: 1).frame(width: 12, height: 24).padding(.leading, Theme.sp4 + 4).accessibilityHidden(true)
            }
            .disabled(off).opacity(off ? 0.45 : 1)
    }

    /// Projects, Repositories, Memory: a full-height list on the left (its add button at the bottom), the detail scrolling on the right.
    func split<L: View, D: View>(_ t: String, _ lede: String, add: (String, () -> Void), @ViewBuilder list: () -> L, @ViewBuilder detail: () -> D) -> some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top, spacing: 10) {
                    tile(Self.icon(t), 28)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(t).font(sys(size: Theme.fs4, weight: .bold)).foregroundColor(fg)
                        Text(lede).font(sys(size: Theme.fs1)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
                    }
                }.padding(14)
                ScrollView { VStack(spacing: 1) { list() }.padding(.horizontal, 8) }
                Divider().overlay(line)
                Button(action: add.1) { Label(add.0, systemImage: "plus").font(sys(size: Theme.fs3)) }.buttonStyle(.borderless)
                    .padding(.horizontal, 14).frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            }.frame(width: 220)
            Divider().overlay(line)
            ScrollView {
                VStack(alignment: .leading, spacing: 22) { detail() }
                    .padding(.horizontal, 24).padding(.top, 24).padding(.bottom, 32)
                    .frame(maxWidth: 760, alignment: .leading).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    func banner(_ icon: String, _ text: String, tinted: Bool = false, @ViewBuilder action: () -> some View = { EmptyView() }) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon).foregroundColor(tinted ? accent : sub)
            Text(text).font(sys(size: Theme.fs2)).foregroundColor(fg).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Theme.sp2)
            action()
        }.padding(.horizontal, 14).padding(.vertical, 10)
            .background(RoundedRectangle(cornerRadius: 9).fill(tinted ? accent.opacity(0.12) : surface))
    }

    func listItem(_ title: String, _ meta: String, on: Bool, mono: Bool = false, icon: String? = nil, tag: String? = nil, _ pick: @escaping () -> Void) -> some View {
        Button(action: pick) {
            HStack(spacing: 10) {
                if let icon { Image(systemName: icon).foregroundColor(on ? accent : sub).frame(width: 16) }
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(sys(size: Theme.fs3, weight: .semibold, design: mono ? .monospaced : .default)).foregroundColor(fg).lineLimit(1)
                    Text(meta).font(sys(size: Theme.fs1)).foregroundColor(dim).lineLimit(1)
                }
                Spacer(minLength: 0)
                if let tag { chip(tag) }
            }.padding(.horizontal, 10).padding(.vertical, Theme.sp2).contentShape(Rectangle())
                .background(RoundedRectangle(cornerRadius: 7).fill(on ? accent.opacity(0.15) : Color.clear))
        }.buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
    }

    /// A path or name in a card, with its buttons on the right.
    func pathRow<V: View>(_ icon: String, _ text: String, @ViewBuilder _ actions: () -> V) -> some View {
        ruled(HStack(spacing: 10) {
            Image(systemName: icon).font(sys(size: Theme.fs2)).foregroundColor(sub).frame(width: 16)
            Text(text).font(sys(size: Theme.fs2, design: .monospaced)).foregroundColor(fg).lineLimit(1).truncationMode(.middle)
            Spacer(minLength: Theme.sp2)
            actions()
        }.padding(.leading, Theme.sp4).padding(.trailing, Theme.sp3).frame(minHeight: 40))
    }
    func xButton(_ label: String, _ act: @escaping () -> Void) -> some View {
        Button(action: act) { Image(systemName: "xmark").font(sys(size: Theme.fs2)) }.buttonStyle(.borderless).foregroundColor(sub).accessibilityLabel(label).help(label)
    }

    /// A multi-line field.
    func editor(_ t: Binding<String>, _ label: String, height: CGFloat = 120) -> some View {
        TextEditor(text: t).font(sys(size: Theme.fs2, design: .monospaced)).scrollContentBackground(.hidden).padding(Theme.sp2)
            .frame(minHeight: height).background(RoundedRectangle(cornerRadius: Theme.r1).fill(bg))
            .overlay(RoundedRectangle(cornerRadius: Theme.r1).stroke(edge)).accessibilityLabel(label)
    }

    func saveRow(_ title: String = "Save", _ save: @escaping () -> Void, @ViewBuilder more: () -> some View = { EmptyView() }) -> some View {
        HStack(spacing: Theme.sp3) {
            more()
            if rs.busy { ProgressView().controlSize(.small) }
            note(rs.status)
            Spacer(minLength: 0)
            Button(title, action: save).keyboardShortcut("s").buttonStyle(.borderedProminent).disabled(rs.busy)
        }
    }

    /// One width for every menu picker, so values line up down the form.
    var menuWidth: CGFloat { 216 }

    static let effortNotes = ["low": "Fastest replies, least reasoning.", "medium": "Balances reasoning against response time.",
                              "high": "More reasoning; replies take longer.", "xhigh": "Extended reasoning for hard problems; noticeably slower.",
                              "max": "The most reasoning; the slowest replies."]

    /// Native segments; a menu when the five don't fit.
    func effortPicker(_ label: String, _ v: Binding<String>, off: Bool = false) -> some View {
        let p = Picker(label, selection: v) { ForEach(efforts, id: \.self) { Text(effortName($0)).tag($0) } }.labelsHidden()
        return ViewThatFits(in: .horizontal) { p.pickerStyle(.segmented).fixedSize(); p.frame(width: menuWidth) }.disabled(off).opacity(off ? 0.45 : 1)
    }
    func claudeModelPicker(_ label: String, _ v: Binding<String>) -> some View {
        Picker(label, selection: v) { ForEach(models.filter { harnessOf($0.1) == "claude" }, id: \.1) { Text($0.0).tag($0.1) } }.labelsHidden().frame(width: menuWidth)
    }
    /// The background agents dash.py runs where greenlight does: a Claude model, or a Gemini one (it runs on Gemini CLI there).
    func writerModelPicker(_ label: String, _ v: Binding<String>) -> some View {
        Picker(label, selection: v) { ForEach(models.filter { $0.1.hasPrefix("claude-") || $0.1.hasPrefix("gemini-") }, id: \.1) { Text($0.0).tag($0.1) } }.labelsHidden().frame(width: menuWidth)
    }

    // MARK: This Mac

    @ViewBuilder var general: some View {
        heading("General", "How Greenlight starts and how it shows up on this Mac.")
        box("Opens to", aside: "New windows (⌘N) always open to the Pipe.") {
            HStack(spacing: Theme.sp3) {
                ForEach([("greenlight", "Greenlight", "L1"), ("basement", "Basement", "B1"), ("pipe", "Pipe", "B2")], id: \.0) { floorCard($0.0, $0.1, $0.2) }
            }.padding(Theme.sp3)
        }
        box("Assistant") {
            row("Name", "What the chat calls itself. ⌃⌥M shows or hides it from any app.") {
                TextField("Greenlight", text: $c.name).textFieldStyle(.roundedBorder).frame(width: 220)
            }
        }
        box("Dock icon", aside: "Finder always shows the Orb.") {
            HStack(spacing: Theme.sp3) { ForEach([("Orb", "Orb"), ("MergeLight", "Merge Light"), ("ThreeFloors", "Three Floors")], id: \.0) { dockTile($0.0, $0.1) } }
                .padding(Theme.sp3)
        }
    }

    /// A floor the app opens to, with the three floors stacked and its own lit.
    func floorCard(_ id: String, _ name: String, _ floor: String) -> some View {
        let on = c.termOpens == id
        return Button { c.termOpens = id } label: {
            HStack(spacing: Theme.sp3) {
                VStack(spacing: 3) { ForEach(["L1", "B1", "B2"], id: \.self) { f in RoundedRectangle(cornerRadius: 2).fill(f == floor ? accent : edge).frame(width: 30, height: 8) } }
                VStack(alignment: .leading, spacing: 1) {
                    Text(name).font(sys(size: Theme.fs3, weight: .semibold)).foregroundColor(fg)
                    Text(floor).font(sys(size: Theme.fs1, design: .monospaced)).foregroundColor(dim)
                }
                Spacer(minLength: 0)
            }.padding(Theme.sp3).frame(maxWidth: .infinity).contentShape(Rectangle())
                .background(RoundedRectangle(cornerRadius: Theme.r2).fill(bg))
                .overlay(RoundedRectangle(cornerRadius: Theme.r2).stroke(on ? accent : edge, lineWidth: on ? 2 : 1))
        }.buttonStyle(.plain).accessibilityLabel("Open to \(name), \(floor)").accessibilityAddTraits(on ? .isSelected : [])
    }

    func dockTile(_ id: String, _ name: String) -> some View {
        let on = c.dockIcon == id
        return Button { c.dockIcon = id } label: {
            VStack(spacing: Theme.sp2) {
                Image(nsImage: Bundle.main.image(forResource: id) ?? NSApp.applicationIconImage).resizable().frame(width: 56, height: 56)
                Text(name).font(sys(size: Theme.fs2, weight: on ? .semibold : .regular)).foregroundColor(on ? fg : sub)
            }.padding(Theme.sp3).frame(maxWidth: .infinity)
                .background(RoundedRectangle(cornerRadius: Theme.r2).fill(bg))
                .overlay(RoundedRectangle(cornerRadius: Theme.r2).stroke(on ? accent : edge, lineWidth: on ? 2 : 1))
        }.buttonStyle(.plain).accessibilityLabel("\(name) Dock icon\(on ? ", selected" : "")")
    }

    @ViewBuilder var appearance: some View {
        heading("Appearance", "The switch in the window bar picks Auto (match macOS), Light or Dark for every floor; each mode wears the theme you pick here.")
        box("Floors") { HStack(spacing: Theme.sp3) { floorTheme(dark: false); floorTheme(dark: true) }.padding(Theme.sp3) }
        box("Color & text") {
            row("Accent", "Buttons, selection and focus. State colors (orange, red) never change.") { accentPicker }
            row("Chat font", "Messages, the composer and questions.") { fontPicker("Chat font", chatFont) { chatFont = $0 } }
            row("Code font", "Code blocks, logs and the terminal.") { fontPicker("Code font", codeFont) { codeFont = $0 } }
            row("Text size", "Chat, settings and terminal. The dashboard keeps its own size.") {
                Text("A").font(.system(size: 10)).foregroundColor(sub).accessibilityHidden(true)
                Slider(value: Binding(get: { fontSize }, set: { v in c.restyle { fontSize = v } }), in: 11...17, step: 1).frame(width: 140).accessibilityLabel("Text size")
                Text("A").font(.system(size: 16)).foregroundColor(sub).accessibilityHidden(true)
                Text("\(Int(fontSize)) pt").font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(sub).frame(width: 36, alignment: .trailing)
            }
        }
        let custom = c.customPage || !c.fluidURL.isEmpty
        box("Dashboard") {
            row("Source", custom ? "Your own page, on the left of the window." : "The live workspace dashboard, on the left of the window.") {
                Picker("Source", selection: Binding(get: { custom }, set: { v in
                    c.customPage = v
                    if !v && !c.fluidURL.isEmpty { c.fluidURL = ""; c.reloadFluid() }
                })) { Text("Built-in").tag(false); Text("Custom").tag(true) }.labelsHidden().pickerStyle(.segmented).fixedSize()
            }
            if custom {
                dependent(off: false) {
                    row("Location", "A file on this Mac or an http(s) URL. Return reloads it.") {
                        TextField("File on this Mac or http(s) URL", text: $c.fluidURL).textFieldStyle(.roundedBorder).onSubmit(c.reloadFluid).frame(width: 240)
                        Button("Reload", action: c.reloadFluid)
                    }
                }
            }
        }
        HStack {
            Spacer()
            Button("Reset appearance to defaults") { c.restyle { darkTheme = named("", "Greenlight"); lightTheme = named("", "GitHub Light"); accentHex = nil; chatFont = "System"; codeFont = "SF Mono"; fontSize = 13 } }
                .buttonStyle(.link).font(sys(size: Theme.fs2))
        }
    }

    /// A floor's theme: a small picture of it in that theme, and the theme menu.
    func floorTheme(dark: Bool) -> some View {
        let t = dark ? darkTheme : lightTheme, tfg = Color(nsColor: t.fg)
        return VStack(alignment: .leading, spacing: Theme.sp2) {
            VStack(alignment: .leading, spacing: 6) {
                RoundedRectangle(cornerRadius: 2).fill(tfg).frame(width: 70, height: 6)
                RoundedRectangle(cornerRadius: 2).fill(tfg.opacity(0.3)).frame(width: 120, height: 5)
                RoundedRectangle(cornerRadius: 2).fill(tfg.opacity(0.3)).frame(width: 96, height: 5)
                Capsule().fill(Color(nsColor: t.accent)).frame(width: 40, height: 12)
            }.padding(Theme.sp3).frame(maxWidth: .infinity, minHeight: 84, alignment: .topLeading)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: t.bg))).overlay(RoundedRectangle(cornerRadius: 8).stroke(edge)).accessibilityHidden(true)
            Text(dark ? "The Basement" : "Greenlight Center").font(sys(size: Theme.fs3, weight: .semibold)).foregroundColor(fg)
            note(dark ? "Terminals and their sidebar." : "Dashboard and chat.")
            Picker(dark ? "Basement theme" : "Greenlight Center theme", selection: Binding(get: { t.name }, set: { n in
                c.restyle { if let t = themes.first(where: { $0.name == n }) { if dark { darkTheme = t } else { lightTheme = t } } }
            })) { ForEach(themes.filter { $0.dark == dark }, id: \.name) { Text($0.name).tag($0.name) } }.labelsHidden()
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    var accentPicker: some View {
        let themeAccent = Button { c.restyle { accentHex = nil } } label: {
            HStack(spacing: Theme.sp2) {
                Circle().fill(Color(nsColor: lightTheme.accent)).frame(width: 12, height: 12)
                Text("Theme accent").font(sys(size: Theme.fs2, weight: accentHex == nil ? .semibold : .regular)).foregroundColor(fg)
            }.padding(.horizontal, Theme.sp3).frame(height: 28)
                .overlay(Capsule().stroke(accentHex == nil ? fg : edge))
        }.buttonStyle(.plain).accessibilityLabel("Theme accent\(accentHex == nil ? ", selected" : "")")
        let dots = HStack(spacing: 0) { ForEach(accents, id: \.self) { h in AccentDot(c: c, h: h, ring: fg, rim: edge) } }
        return ViewThatFits(in: .horizontal) {  // larger fonts or a narrow pane: the dots move under the theme accent, none drop
            HStack(spacing: Theme.sp2) { themeAccent; dots }
            VStack(alignment: .leading, spacing: Theme.sp2) { themeAccent; dots }
        }
    }

    func fontPicker(_ label: String, _ current: String, _ set: @escaping (String) -> Void) -> some View {
        Picker(label, selection: Binding(get: { current }, set: { v in c.restyle { set(v) } })) {
            ForEach(fontChoices, id: \.self) { Text($0).tag($0) }
        }.labelsHidden().frame(width: menuWidth)
    }

    @ViewBuilder var chat: some View {
        heading("Chat & voice", "The chat pane's model, and dictating into it from any app.")
        box("Model") {
            row("Chat model", "Changes apply from your next message.") {
                Picker("Model", selection: $c.model) { ForEach(models, id: \.1) { Text($0.0).tag($0.1) } }.labelsHidden().frame(width: menuWidth)
            }
            row("Effort", c.isClaude ? Self.effortNotes[c.effort] : "\(c.modelName) doesn't use an effort level.") { effortPicker("Effort", $c.effort, off: !c.isClaude) }
            toggleRow("Live activity", "Under the reply as \(c.name) works: the running tool call, time, tokens and speed, like Claude Code.", $c.liveLine)
            toggleRow("Verbose activity", "A live log above the message box (and under the live line in voice mode): \(c.name)'s every command in full with its output and thinking, what your workspaces' agents are doing, and each move Greenlight makes. Off keeps the concise view.", $c.verbose)
        }
        let status = engineStatus, ready = !status.hasPrefix("Not ready")
        box("Dictation") {
            row("Engine", status) {
                Circle().fill(ready ? Theme.done : Theme.needs).frame(width: 8, height: 8).accessibilityLabel(ready ? "Ready" : "Not ready")
                Picker("Voice engine", selection: $c.engine) { ForEach(engines, id: \.0) { Text($0.1).tag($0.0) } }.labelsHidden().frame(width: menuWidth)
            }
            if c.engine == "whisper" { dependent(off: false) { row("Whisper model") { Button("Choose model…") { delegate.pickModel() } } } }
            if c.engine == "openai" { dependent(off: false) { row("OpenAI key", openAIKeySource()) { Button("Set key…") { delegate.setKey() } } } }
            row("Shortcut", "Press it in any app to dictate to the chat.") { shortcutEditor }
        }
        box("Voice mode") {
            row("Anthropic key", "For the stage: Claude Haiku 5.5 draws what \(c.name) says. Stored in your Keychain; without it the stage shows a plain list.") {
                Button("Set key…") { delegate.askKey("anthropic", "Anthropic API key", "voice mode's stage (Claude Haiku 5.5)") }
            }
            row("Speaking voice", Speech.shared.status.isEmpty ? (Speech.shared.kind == "live" ? "OpenAI's GPT-Live 1.1 holds the conversation: it hears you, hands each request to \(c.name) and reads its answer. About $0.05 a minute while voice mode is on; if it fails, voice mode stops and says why (no Apple voice standing in)." : "How \(c.name) sounds. Kokoro runs on this Mac; ElevenLabs is paid and gets each line \(c.name) says. Either falls back to Apple's voice.") : Speech.shared.status) {
                Picker("Speaking voice", selection: Binding(get: { Speech.shared.kind }, set: { ud.set($0, forKey: "speech"); preview.stop(); Speech.shared.stop(); Speech.shared.warm(); c.voiceTest = ""; c.objectWillChange.send() })) {
                    ForEach(Speech.kinds, id: \.0) { Text($0.1).tag($0.0) }
                }.labelsHidden().frame(width: menuWidth)
            }
            if Speech.shared.kind == "kokoro" { dependent(off: false) { row("Kokoro voice", "a = American, b = British; f/m = female/male.") {
                Picker("Kokoro voice", selection: Binding(get: { Speech.shared.kokoroVoice }, set: { ud.set($0, forKey: "kokoroVoice"); c.objectWillChange.send() })) {
                    ForEach(Speech.kokoroVoices, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(width: menuWidth)
            } } }
            if Speech.shared.kind == "live" { dependent(off: false) {
                row("OpenAI key", openAIKeySource() + " The same key as the OpenAI Whisper engine.") { Button("Set key…") { delegate.setKey() } }
                row("GPT-Live voice", preview.hint ?? "Picking one says “\(livePreviewLine)” in it. Fixed for each session: a new one takes effect next time voice mode starts.") {
                    if case .loading = preview.phase { ProgressView().controlSize(.small).accessibilityLabel("Getting the voice") }
                    Button { preview.busy ? preview.stop() : preview.play(LiveVoice.voice, voiceOn: c.voice.on) } label: {
                        Image(systemName: preview.busy ? "stop.fill" : "play.fill").frame(width: 14)
                    }.help(preview.busy ? "Stop the preview" : "Hear this voice").accessibilityLabel(preview.busy ? "Stop the preview" : "Hear this voice")
                    Picker("GPT-Live voice", selection: Binding(get: { LiveVoice.voice }, set: {
                        guard $0 != LiveVoice.voice else { return }
                        ud.set($0, forKey: "liveVoice"); c.voiceTest = ""; c.objectWillChange.send()
                        preview.play($0, voiceOn: c.voice.on)
                    })) {
                        ForEach(LiveVoice.voices, id: \.self) { Text($0.capitalized).tag($0) }
                    }.labelsHidden().frame(width: menuWidth)
                }
            } }
            if Speech.shared.kind == "eleven" { dependent(off: false) {
                row("ElevenLabs key", "Stored in your Keychain.") { Button("Set key…") { delegate.askKey("elevenlabs", "ElevenLabs API key", "voice mode's speaking voice") } }
                row("ElevenLabs voice ID", "From elevenlabs.io → Voices. Empty: Sarah.") {
                    TextField("EXAVITQu4vr4xnSDxMaL", text: Binding(get: { ud.string(forKey: "elevenVoice") ?? "" }, set: { ud.set($0.trimmingCharacters(in: .whitespaces), forKey: "elevenVoice"); Speech.shared.warm() })).frame(width: menuWidth)
                }
            } }
            if Speech.shared.kind != "apple" && Speech.shared.kind != "live" { dependent(off: false) {
                row("Test the voice", c.voiceTest.isEmpty ? "Says one line now, in this voice, and shows why if it can't." : c.voiceTest) {
                    Button("Test") {
                        let name = Speech.kinds.first { $0.0 == Speech.shared.kind }?.1 ?? "this voice"
                        c.voiceTest = "Asking \(name)…"
                        Speech.shared.test("Hi, this is how \(c.name) sounds.") { why in c.voiceTest = why.map { "Failed: \($0)" } ?? "Played by \(name). Voice mode will use it." }
                    }
                }
            } }
            row("Interrupt by talking", "Talk over \(c.name) to cut it off. Off: it finishes each answer first (for speakers that echo).") {
                Toggle("Interrupt by talking", isOn: Binding(get: { c.voice.bargeIn }, set: { ud.set($0, forKey: "voiceBargeIn"); c.objectWillChange.send() })).labelsHidden().toggleStyle(.switch)
            }
        }
        HStack(spacing: Theme.sp1) {
            note("Standing instructions for Greenlight have their own page:")
            Button("Instructions") { c.settingsTab = "Instructions" }.buttonStyle(.link).font(sys(size: Theme.fs2))
        }.padding(.horizontal, 10)
    }

    /// Under Engine: what the chosen engine will actually do, and whether it's ready.
    var engineStatus: String {
        switch c.engine {
        case "whisper":
            let m = URL(fileURLWithPath: whisperModel())
            if !FileManager.default.isExecutableFile(atPath: whisperBin()) { return "Not ready: whisper-cli isn't at \(whisperBin())." }
            return FileManager.default.fileExists(atPath: m.path) ? "Ready · model \(m.lastPathComponent)" : "Not ready: model \(m.lastPathComponent) is missing. Choose a model below."
        case "openai":
            let m = UserDefaults.standard.string(forKey: "openaiModel") ?? "whisper-1"
            if settingsOpenAIKey() != nil { return "Ready · key set · model \(m)" }
            guard let env = peekEnvironmentOpenAIKey() else { return "Looking for OPENAI_API_KEY in your shell…" }
            return env == nil ? "Not ready: no API key. Set one below, or export OPENAI_API_KEY and relaunch." : "Ready · key from OPENAI_API_KEY · model \(m)"
        default:
            return appleOnDevice ? "Ready · transcribes on this Mac; audio never leaves it." : "Ready · on-device recognition isn't available here, so audio goes to Apple's servers."
        }
    }

    /// The voice shortcut's keys and Change…; while recording, an inline prompt in their place.
    @ViewBuilder var shortcutEditor: some View {
        if c.keying {
            Text("Press shortcut… Esc cancels").font(sys(size: Theme.fs2, weight: .medium)).foregroundColor(Theme.workingInk)
                .padding(.horizontal, Theme.sp2).frame(height: 22)
                .overlay(RoundedRectangle(cornerRadius: Theme.r1).stroke(Theme.working, style: StrokeStyle(lineWidth: 1, dash: [3])))
        } else {
            HStack(spacing: Theme.sp2) { keycaps(c.shortcut); Button("Change…") { delegate.recordShortcut() } }
        }
    }

    @ViewBuilder var keyboard: some View {
        heading("Keyboard", "Editing in the chat input, and every shortcut in one place.")
        box("Chat input") {
            toggleRow("Vim editing", "Esc for normal mode · i or a to insert · v to select.", $c.vim)
            dependent(off: !c.vim) {
                row("Starting mode", "The mode each new message starts in.") {
                    Picker("Starting mode", selection: $c.vimStart) { Text("Insert").tag("insert"); Text("Normal").tag("normal") }.labelsHidden().pickerStyle(.segmented).fixedSize()
                }
            }
        }
        shortcuts("In the app", [("New window", "⌘N"), ("Settings", "⌘,"), ("Show or hide chat", "⇧⌘C"), ("Live chat on or off (Esc leaves)", "⇧⌘L"),
                                 ("Show or hide terminal", "⇧⌘T"), ("Reload dashboard", "⌘R"), ("Up or down a floor", "⌘↑|⌘↓")])
        shortcuts("While typing in the chat", [("Search chat history", "⌃R"), ("Previous or next sent message", "↑|↓"), ("New line", "⇧↩")])
        shortcuts("In any app", [("Show or hide \(c.name)", "⌃⌥M"), ("Dictate to chat", c.shortcut)])
    }

    /// Two columns of shortcuts.
    func shortcuts(_ t: String, _ keys: [(String, String)]) -> some View {
        box(t) {
            let even = keys.count / 2 * 2
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 0), GridItem(.flexible(), spacing: 0)], spacing: 0) {
                ForEach(keys[..<even], id: \.0) { a, k in shortcut(a, k) }
            }
            if even < keys.count { shortcut(keys[even].0, keys[even].1) }  // an odd one out takes the full row, no empty cell
        }
    }
    func shortcut(_ a: String, _ k: String) -> some View {
        ruled(HStack(spacing: Theme.sp2) {
            Text(a).font(sys(size: Theme.fs3)).foregroundColor(sub).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Theme.sp2)
            if a == "Dictate to chat" { shortcutEditor } else { keycaps(k) }  // the voice shortcut is edited where it's listed
        }.padding(.horizontal, Theme.sp4).frame(minHeight: 40))
    }

    /// "↑|↓" is either key, as separate caps; a chord is one cap with its modifiers spaced.
    func keycaps(_ k: String) -> some View {
        HStack(spacing: Theme.sp1) {
            ForEach(Array(k.split(separator: "|").enumerated()), id: \.offset) { i, key in
                if i > 0 { Text("or").font(sys(size: Theme.fs1)).foregroundColor(dim) }
                Text(key.count > 4 ? String(key) : key.map(String.init).joined(separator: "\u{2009}")).font(sys(size: Theme.fs2, weight: .medium).monospacedDigit()).foregroundColor(fg)
                    .padding(.horizontal, Theme.sp2).frame(minWidth: 26).frame(height: 22)
                    .background(RoundedRectangle(cornerRadius: 5).fill(surface)).overlay(RoundedRectangle(cornerRadius: 5).stroke(edge))
            }
        }
    }

    @ViewBuilder var terminal: some View {
        heading("Terminal", "The Basement's tabs, and the orbs that ride between it and Greenlight Center.")
        box("Floating orbs") {
            toggleRow("Show floating orbs", "The buttons bottom right that ride between floors. Off: ⌘↑ / ⌘↓ still ride.",
                      Binding(get: { !c.orbsHidden }, set: { c.orbsHidden = !$0 }))
            dependent(off: c.orbsHidden) {
                row("Up button") {
                    Picker("Up button", selection: $c.orbStyle) { Text("Matte").tag("matte"); Text("Traffic light").tag("light") }.pickerStyle(.segmented).labelsHidden().fixedSize()
                }
            }
            row("Ride animation", "The orb's flip between floors. 0: you're just there.") { seconds("Ride animation", $c.rideTime) }
            row("Sidebar slide", "How long the Basement's sidebar takes to slide in. 0: it's just there.") { seconds("Sidebar slide", $c.sidebarSlide) }
        }
        box("Tab names") {
            toggleRow("Name tabs with AI", "Once a tab's output settles, names it from what's on screen. A name you type always wins.", $c.termAutoName)
            dependent(off: !c.termAutoName) { row("Naming model") { claudeModelPicker("Naming model", $c.termNameModel) } }
        }
    }

    /// A duration: a slider for the usual 0–2s, a field for up to 5s, and whether it's on at all.
    func seconds(_ label: String, _ v: Binding<Double>) -> some View {
        let b = Binding(get: { v.wrappedValue }, set: { v.wrappedValue = min(max($0, 0), 5) }), on = v.wrappedValue > 0
        return HStack(spacing: Theme.sp2) {
            Slider(value: Binding(get: { min(b.wrappedValue, 2) }, set: { b.wrappedValue = ($0 * 20).rounded() / 20 }), in: 0...2)  // no `step:`, which draws 41 tick marks.frame(width: 130).accessibilityLabel(label)
            TextField(label, value: b, format: .number.precision(.fractionLength(0...2))).textFieldStyle(.roundedBorder).multilineTextAlignment(.trailing).frame(width: 52)
            Text("s").foregroundColor(sub)
            Text(on ? "On" : "Off").font(sys(size: Theme.fs1, weight: .medium)).foregroundColor(on ? accent : dim)
                .frame(width: 34, height: 18).background(Capsule().fill(on ? accent.opacity(0.12) : surface))
        }
    }

    @ViewBuilder var hosts: some View {
        let ssh = sshHosts(), env = ProcessInfo.processInfo.environment["GREENLIGHT_HOST"]
        heading("Hosts", "Where Greenlight runs your agents and workspaces, and the machines the Basement opens shells on.")
        box("Greenlight runs on") {
            row("Host", c.hostCheck.isEmpty ? "Needs ssh key auth. Its workspaces, agents, timer and chat live there; this Mac only shows them." : c.hostCheck) {
                Picker("Host", selection: $c.runHost) {
                    Text("This Mac").tag("local")
                    ForEach(ssh + (c.runHost == "local" || ssh.contains(c.runHost) ? [] : [c.runHost]), id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(width: 180)
                Button("Test") { c.testHost() }.help("Test the connection")
            }
            dependent(off: false) {
                row("State folder", "On that host.") { TextField("~/.greenlight", text: $c.runHome).textFieldStyle(.roundedBorder).frame(width: 240) }
                row("Code folder", "Where greenlight.py lives on that host.") { TextField("~/.claude/skills/greenlight", text: $c.runCode).textFieldStyle(.roundedBorder).frame(width: 240) }
            }
        }
        if c.hostPending {
            banner("arrow.clockwise", "Greenlight reads the host at launch. Relaunch to apply.", tinted: true) { Button("Relaunch") { c.relaunch() }.buttonStyle(.borderedProminent) }
        }
        if let env { banner("exclamationmark.triangle", "GREENLIGHT_HOST=\(env) is set, and it wins over this setting.") }
        box("The Basement's hosts", aside: "From ~/.ssh/config") {
            row("This Mac's name", "Shown in the Basement's sidebar. Empty: its hostname.") { TextField(localDefault, text: $c.localName).textFieldStyle(.roundedBorder).frame(width: 240) }
            pathRow("laptopcomputer", c.localShown) { Text("local").font(sys(size: Theme.fs1)).foregroundColor(dim) }
            ForEach(ssh, id: \.self) { h in pathRow("server.rack", h) { if h == host { chip("runs Greenlight") } else { Text("ssh").font(sys(size: Theme.fs1)).foregroundColor(dim) } } }
            ruled(HStack {
                note("Every Host in ~/.ssh/config, minus wildcards, git hosts and -ts twins. A new one shows after a relaunch.")
                Spacer(minLength: Theme.sp3)
                Button("Edit ~/.ssh/config") { _ = try? Process.run(URL(fileURLWithPath: "/usr/bin/open"), arguments: ["-t", NSHomeDirectory() + "/.ssh/config"]) }
            }.padding(.horizontal, Theme.sp4).padding(.vertical, Theme.sp3))
        }
    }

    // MARK: Greenlight, on its host

    @ViewBuilder var automation: some View {
        heading("Automation", "What Greenlight does on \(hostLabel) between your visits: scanning workspaces, and the background agents that build the dashboard.")
        let failed = !c.greenlightEveryNote.isEmpty && !c.greenlightEveryLoading
        box("Workspace scan") {
            row("Scan every", c.greenlightEveryLoading ? "Checking \(hostLabel)…" : c.greenlightEvery == 0 ? "Off: nothing scans or starts agents on a schedule."
                : "Scans every workspace on \(hostLabel), starts agents when work is waiting, and refreshes the dashboard.") {
                if c.greenlightEveryLoading { ProgressView().controlSize(.small) }
                Picker("Scan interval", selection: Binding(get: { c.greenlightEvery }, set: c.setGreenlightEvery)) {
                    Text("Off").tag(0)
                    ForEach([10, 15, 30, 60], id: \.self) { Text($0 == 60 ? "1 hour" : "\($0) min").tag($0) }
                }.labelsHidden().pickerStyle(.segmented).fixedSize().onAppear(perform: c.loadGreenlightEvery).disabled(c.greenlightEveryLoading)
            }
            if failed {
                ruled(HStack(alignment: .firstTextBaseline, spacing: Theme.sp2) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundColor(Theme.needs)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(c.greenlightEveryNote).font(sys(size: Theme.fs2, weight: .semibold)).foregroundColor(fg)
                        note("The schedule shown is the last one this Mac read; it may be out of date.")
                    }
                    Spacer(minLength: Theme.sp3)
                    Button("Retry", action: c.loadGreenlightEvery)
                }.padding(Theme.sp4).background(Theme.needs.opacity(0.1)))
            }
        }
        box("Background agents") {
            row("Workspace pages", "Builds each workspace's page on the dashboard.") { writerModelPicker("Workspace pages model", $c.designModel) }
            dependent(off: false) {
                row("Effort") { effortPicker("Workspace pages effort", $c.designEffort, off: harnessOf(c.designModel) == "gemini") }
                row("Rebuild", c.designParallel == 0 ? "A page builds only when you open it." : "Workspaces whose state changed rebuild in the background.") {
                    if c.designParallel > 0 {
                        Stepper("\(c.designParallel) at once", value: $c.designParallel, in: 1...10).fixedSize().accessibilityLabel("Parallel builds")
                    }
                    Picker("Rebuild pages", selection: Binding(get: { c.designParallel > 0 }, set: { c.designParallel = $0 ? max(c.designParallel, 3) : 0 })) {
                        Text("On demand").tag(false); Text("Background").tag(true)
                    }.labelsHidden().pickerStyle(.segmented).fixedSize()
                }
            }
            row("Next-step suggestions", "Suggests the next action on each hex when its state moves.") { writerModelPicker("Suggestions model", $c.nextModel) }
            row("Idle check-in", "When you're at your Mac but haven't done anything in Greenlight for this long, the chat suggests what to work on next.") {
                Picker("Idle check-in", selection: $c.checkInMins) { Text("Off").tag(0); Text("15 min").tag(15); Text("30 min").tag(30); Text("1 hour").tag(60) }.labelsHidden().fixedSize()
            }
            row("Watch workspaces", "When a workspace starts needing you, the chat looks into it and asks you a question if one's worth asking.") {
                Toggle("Watch workspaces", isOn: $c.watch).labelsHidden().toggleStyle(.switch)
            }
            dependent(off: false) { row("Effort") { effortPicker("Suggestions effort", $c.nextEffort, off: harnessOf(c.nextModel) == "gemini") } }
        }
        // ponytail: auto-run is loaded per repo, so it's a link, not the design's every-repo table
        banner("bolt", "Auto-run jobs (Fix CI, Resolve threads, Fix conflicts, Review) are set per repository.") { Button("Open Repositories") { c.settingsTab = "Repositories" } }
    }

    // Projects: the Basement's projects.json on this Mac ([{id, name, repos, dirs, dir?}]), saved as you change them.
    func pid(_ p: [String: Any]) -> String { p["id"] as? String ?? "" }
    func pname(_ p: [String: Any]) -> String { p["name"] as? String ?? "Project" }
    func pstrings(_ p: [String: Any], _ k: String) -> [String] { p[k] as? [String] ?? [] }
    func projectOf(_ r: String) -> [String: Any]? { rs.projs.first { pstrings($0, "repos").contains(r) } }
    var allRepos: [String] { Array(Set(rs.repos + rs.projs.flatMap { pstrings($0, "repos") })).sorted() }

    func commitProjects(_ edit: (inout [[String: Any]]) -> Void) {
        var list = rs.projs; edit(&list)
        if delegate.saveProjects(list) { rs.projs = list; rs.projNote = "" } else { rs.projNote = "Couldn't save the projects." }
    }
    /// A repo in one project (to: "" takes it out of every project).
    func setProject(_ r: String, _ to: String) {  // ponytail: a repo the Basement put in two projects ends up in one
        commitProjects { l in for j in l.indices { l[j]["repos"] = pstrings(l[j], "repos").filter { $0 != r } + (pid(l[j]) == to ? [r] : []) } }
    }
    @discardableResult func addProject() -> String {
        let id = "p" + String(UUID().uuidString.lowercased().filter { $0.isLetter || $0.isNumber }.prefix(12))
        commitProjects { $0.append(["id": id, "name": "New project", "repos": [String](), "dirs": [String]()]) }
        rs.proj = id
        return id
    }
    func deleteProject(_ id: String) {
        let a = NSAlert(); a.messageText = "Delete \(pname(rs.projs.first { pid($0) == id } ?? [:]))?"
        a.informativeText = "Its repositories stay; they just aren't in a project. Folders and terminals aren't touched."
        a.addButton(withTitle: "Delete"); a.addButton(withTitle: "Cancel")
        guard a.runModal() == .alertFirstButtonReturn else { return }
        commitProjects { $0.removeAll { pid($0) == id } }
        rs.proj = rs.projs.first.map(pid) ?? ""
    }
    func addDir() {
        var d = rs.draftDir.trimmingCharacters(in: .whitespaces)
        if (host.isEmpty || host == "local") && (d == "~" || d.hasPrefix("~/")) { d = NSHomeDirectory() + d.dropFirst() }
        while d.count > 1 && d.hasSuffix("/") { d.removeLast() }
        guard d.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil, !d.contains("..") else {
            rs.projNote = "\(d) isn't a full path (letters, digits and ._-@+/ only)."; return
        }
        let id = rs.proj
        commitProjects { l in if let j = l.firstIndex(where: { pid($0) == id }), !pstrings(l[j], "dirs").contains(d) { l[j]["dirs"] = pstrings(l[j], "dirs") + [d] } }
        rs.draftDir = ""
    }

    @ViewBuilder var projects: some View {
        split("Projects", "Repositories and the folders they're checked out in. Each gets its own Basement window.", add: ("New project", { _ = addProject() })) {
            ForEach(rs.projs.indices, id: \.self) { i in
                let p = rs.projs[i], n = pstrings(p, "repos").count, d = pstrings(p, "dirs").count
                listItem(pname(p), "\(n) \(n == 1 ? "repo" : "repos") · \(d) \(d == 1 ? "folder" : "folders")", on: pid(p) == rs.proj, icon: "folder") { rs.proj = pid(p); rs.draftDir = ""; rs.projNote = "" }
            }
            if rs.projs.isEmpty { note("No projects yet.").padding(Theme.sp3) }
        } detail: {
            if let i = rs.projs.firstIndex(where: { pid($0) == rs.proj }) { projectDetail(i) } else { note("Pick a project, or make a new one.") }
            if !rs.projNote.isEmpty { note(rs.projNote) }
        }.onAppear { if !rs.projs.contains(where: { pid($0) == rs.proj }) { rs.proj = rs.projs.first.map(pid) ?? "" } }
    }

    @ViewBuilder func projectDetail(_ i: Int) -> some View {
        let p = rs.projs[i], id = pid(p), dirs = pstrings(p, "dirs"), repos = pstrings(p, "repos"), opensIn = p["dir"] as? String ?? dirs.first
        HStack(spacing: Theme.sp3) {
            TextField("Project name", text: Binding(get: { pname(p) }, set: { v in commitProjects { l in l[i]["name"] = String(v.prefix(80)) } }))
                .textFieldStyle(.plain).font(sys(size: 20, weight: .bold)).foregroundColor(fg)
            Button("Delete", role: .destructive) { deleteProject(id) }
        }
        box("Terminal") {
            row("New terminals open in", "Empty: the project's first folder.") {
                Picker("New terminals open in", selection: Binding(get: { p["dir"] as? String ?? "" }, set: { v in commitProjects { l in l[i]["dir"] = v.isEmpty ? nil : v } })) {
                    Text("First folder").tag("")
                    ForEach(dirs, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(maxWidth: 260)
            }
        }
        box("Folders", aside: "Full paths on \(hostLabel)") {
            ForEach(dirs, id: \.self) { d in
                pathRow("folder", d) {
                    if d == opensIn { chip("new terminals") }
                    else { Button("Open terminals here") { commitProjects { l in l[i]["dir"] = d } }.buttonStyle(.borderless).font(sys(size: Theme.fs2)) }
                    xButton("Remove \(d)") { commitProjects { l in l[i]["dirs"] = dirs.filter { $0 != d }; if l[i]["dir"] as? String == d { l[i]["dir"] = nil } } }
                }
            }
            ruled(HStack(spacing: 10) {
                TextField("/home/you/src/another-repo", text: $rs.draftDir).textFieldStyle(.roundedBorder).font(sys(size: Theme.fs2, design: .monospaced)).onSubmit(addDir)
                    .accessibilityLabel("New folder path")
                Button("Add folder", action: addDir).disabled(rs.draftDir.trimmingCharacters(in: .whitespaces).isEmpty)
            }.padding(.horizontal, Theme.sp4).padding(.vertical, 10))
        }
        box("Repositories", aside: "\(repos.count) \(repos.count == 1 ? "repo" : "repos")") {
            ForEach(repos, id: \.self) { r in
                pathRow("arrow.triangle.branch", r) {
                    Button("Open") { c.settingsTab = "Repositories"; rs.adding = false; rs.pick(r) }.buttonStyle(.borderless)
                    xButton("Remove \(r) from this project") { setProject(r, "") }
                }
            }
            if repos.isEmpty { ruled(note("No repositories in this project yet.").padding(Theme.sp4).frame(maxWidth: .infinity, alignment: .leading)) }
            ruled(HStack {
                Menu("Add a repository…") {
                    ForEach(allRepos.filter { !repos.contains($0) }, id: \.self) { r in
                        Button(r + (projectOf(r).map { " (from \(pname($0)))" } ?? "")) { setProject(r, id) }
                    }
                }.fixedSize()
                Spacer()
            }.padding(.horizontal, Theme.sp4).padding(.vertical, 10))
        }
    }

    @ViewBuilder var repositories: some View {
        split("Repositories", "Each repo's project, its lifecycle, and the prompts behind the hover card's buttons.",
              add: ("Add repository", { rs.adding = true; rs.draftRepo = ""; rs.draftProj = rs.projs.first.map(pid) ?? ""; rs.projNote = "" })) {
            ForEach(allRepos, id: \.self) { r in
                let on = !rs.adding && rs.repo == r
                listItem(r, projectOf(r).map(pname) ?? "No project", on: on, mono: true, tag: on && rs.source == "yours" ? "yours" : nil) { rs.adding = false; rs.pick(r) }
            }
            if allRepos.isEmpty { note(rs.busy ? "Loading…" : "No repositories yet.").padding(Theme.sp3) }
        } detail: {
            if rs.adding { addRepoForm } else if !rs.repo.isEmpty { repoDetail } else { note("Pick a repository.") }
        }.onAppear { takeRepo() }.onChange(of: c.settingsRepo) { takeRepo() }
    }

    func takeRepo() {  // "repo" or "repo/kind": the card's "Edit <repo>'s prompt…"
        let p = c.settingsRepo.split(separator: "/").map(String.init); if p.isEmpty { return }
        if p.count > 1, RulesStore.kinds.contains(where: { $0.0 == p[1] }) { rs.kind = p[1]; rs.rtab = "prompts" }
        rs.adding = false; rs.pick(p[0]); c.settingsRepo = ""
    }

    @ViewBuilder var addRepoForm: some View {
        box("Add a repository") {
            row("Name", "As orca knows it: the last part of its path. A remote like gitlab.com/group/repo works too.") {
                TextField("repo", text: $rs.draftRepo).textFieldStyle(.roundedBorder).font(sys(size: Theme.fs2, design: .monospaced)).frame(width: 240).onSubmit(addRepo)
            }
            row("Project") {
                Picker("Project", selection: $rs.draftProj) { ForEach(rs.projs.indices, id: \.self) { j in Text(pname(rs.projs[j])).tag(pid(rs.projs[j])) } }.labelsHidden().frame(width: 180)
                Button("New project…") { rs.draftProj = addProject(); rs.proj = rs.draftProj }
            }
        }
        if !rs.projNote.isEmpty { note(rs.projNote) }
        HStack {
            Spacer()
            Button("Cancel") { rs.adding = false }
            Button("Add repository", action: addRepo).keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
                .disabled(rs.draftRepo.trimmingCharacters(in: .whitespaces).isEmpty || rs.draftProj.isEmpty)
        }
    }
    func addRepo() {
        var name = String(rs.draftRepo.trimmingCharacters(in: .whitespaces).split(separator: "/").last ?? "")
        if name.hasSuffix(".git") { name.removeLast(4) }
        guard name.range(of: #"^[\w.-]{1,100}$"#, options: .regularExpression) != nil else { rs.projNote = "A repo name is letters, digits and ._- only."; return }  // it reaches a shell command
        guard !rs.draftProj.isEmpty else { rs.projNote = "Pick a project for it."; return }
        setProject(name, rs.draftProj)
        rs.adding = false; rs.rtab = "stages"; rs.pick(name)
    }

    @ViewBuilder var repoDetail: some View {
        let proj = projectOf(rs.repo), own = rs.source == "yours"
        VStack(alignment: .leading, spacing: 3) {
            Text(rs.repo).font(sys(size: 20, weight: .bold, design: .monospaced)).foregroundColor(fg)
            if let path = rs.repoPaths[rs.repo] { note(path) }
        }
        HStack(spacing: Theme.sp2) {  // under the name: the detail column can be ~300pt
            Picker("Project", selection: Binding(get: { proj.map(pid) ?? "" }, set: { setProject(rs.repo, $0) })) {
                Text("No project").tag("")
                ForEach(rs.projs.indices, id: \.self) { j in Text(pname(rs.projs[j])).tag(pid(rs.projs[j])) }
            }.labelsHidden().frame(maxWidth: 160).help("Opens with the project's folders in the Basement.")
            Button("New project…") { setProject(rs.repo, addProject()); c.settingsTab = "Projects" }
            Spacer(minLength: 0)
            Button("Remove", role: .destructive) { setProject(rs.repo, "") }.disabled(proj == nil).help("Take it out of its project")
        }
        if !rs.projNote.isEmpty { note(rs.projNote) }
        VStack(alignment: .leading, spacing: Theme.sp2) {
            Picker("Repository settings", selection: $rs.rtab) {
                Text("Lifecycle").tag("stages"); Text(rs.autoOn.isEmpty ? "Auto-run" : "Auto-run (\(rs.autoOn.count))").tag("auto"); Text("Rules").tag("rules"); Text("Prompts").tag("prompts")
            }.pickerStyle(.segmented).labelsHidden().fixedSize()
            if !rs.source.isEmpty { HStack(alignment: .firstTextBaseline, spacing: Theme.sp2) {
                Text(own ? "Your own lifecycle: \(greenlightHome)/lifecycles/\(rs.repo).md on \(hostLabel)." : "Using the \(rs.source) lifecycle. Saving makes it your own.")
                    .font(sys(size: Theme.fs2)).foregroundColor(own ? accent : dim).fixedSize(horizontal: false, vertical: true)
                if own { Button("Use default", action: rs.reset).buttonStyle(.link).font(sys(size: Theme.fs2)).disabled(rs.busy) }
            } }
        }
        switch rs.rtab {
        case "auto": autoRun
        case "rules":
            box("Rules", aside: "Markdown · every agent on \(rs.repo) follows these") { editor($rs.rules, "Rules for \(rs.repo)", height: 260).padding(Theme.sp3) }
            saveRow("Save", rs.save)
        case "prompts": prompts
        default: lifecycle
        }
    }

    func optBinding(_ k: String) -> Binding<Bool> {
        Binding(get: { rs.options[k] != nil }, set: { v in if v { rs.options[k] = "" } else { rs.options[k] = nil } })
    }

    @ViewBuilder var lifecycle: some View {
        box("Stages", aside: "In order. The first unmet stage is the MR's next step.") {
            ForEach(rs.stages.indices, id: \.self) { i in stageRow(i) }
            ruled(HStack {
                Menu("Add stage…") {
                    ForEach(rs.builtins.keys.sorted().filter { k in !rs.stages.contains { $0.key == k } }, id: \.self) { k in
                        Button("\(k) (built-in)") { rs.stages.insert(Stage(key: k, req: rs.builtins[k] ?? "", done: ""), at: max(0, rs.stages.count - 1)) }
                    }
                    Divider()
                    Button("Your own (sign-off or label)…") { rs.stages.insert(Stage(key: "stage\(rs.stages.count + 1)", req: "", done: "signoff"), at: max(0, rs.stages.count - 1)) }
                }.fixedSize()
                Spacer()
            }.padding(.horizontal, Theme.sp4).padding(.vertical, 10))
        }
        box("Options") {
            toggleRow("Solo", "Nobody else reviews this repo: a mergeable MR with no other approval comes to you to review and merge.", optBinding("solo"))
            toggleRow("Merge by itself", "Once every step before the merge passes (an agent's reviews and try-out, say), Greenlight merges without waiting on an approval or asking you.", optBinding("automerge"))
            toggleRow("Merge into any branch", "Greenlight may merge an MR whose target isn't the default branch.", optBinding("merge_any_base"))
        }
        saveRow("Save", rs.save)
    }

    func stageRow(_ i: Int) -> some View {
        let s = $rs.stages[i], builtin = rs.builtins[rs.stages[i].key] != nil
        return ruled(HStack(alignment: .top, spacing: Theme.sp3) {
            Text("\(i + 1)").font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(dim).frame(width: 18, alignment: .trailing).padding(.top, 5)
            VStack(alignment: .leading, spacing: Theme.sp1) {
                HStack(spacing: Theme.sp2) {
                    if builtin { Text(rs.stages[i].key).font(sys(size: Theme.fs2, weight: .semibold, design: .monospaced)).foregroundColor(fg).padding(.top, 4) }
                    else { TextField("key", text: s.key).textFieldStyle(.roundedBorder).font(sys(size: Theme.fs2, design: .monospaced)).frame(width: 180).accessibilityLabel("Stage \(i + 1) key") }
                    Text(builtin ? "Built-in" : s.wrappedValue.done.hasPrefix("label=") ? "Label" : "Your sign-off").font(sys(size: Theme.fs1, weight: .medium))
                        .foregroundColor(builtin ? sub : accent).padding(.horizontal, Theme.sp2).frame(height: 20)
                        .background(Capsule().fill(builtin ? surface : accent.opacity(0.12))).padding(.top, 2)
                }
                if builtin { note(rs.stages[i].req) }
                else {
                    TextField("Requirement", text: s.req).textFieldStyle(.roundedBorder).accessibilityLabel("Stage \(i + 1) requirement")
                    HStack(spacing: Theme.sp2) {
                        Picker("Done when", selection: Binding(get: { s.wrappedValue.done.hasPrefix("label=") ? "label" : "signoff" },
                                                               set: { s.wrappedValue.done = $0 == "label" ? "label=" : "signoff" })) {
                            Text("You sign off").tag("signoff"); Text("It has a label").tag("label")
                        }.labelsHidden().frame(width: 150)
                        if s.wrappedValue.done.hasPrefix("label=") {
                            TextField("label", text: Binding(get: { String(s.wrappedValue.done.dropFirst(6)) }, set: { s.wrappedValue.done = "label=" + $0 }))
                                .textFieldStyle(.roundedBorder).frame(width: 160).accessibilityLabel("Stage \(i + 1) label")
                        }
                    }
                }
            }
            Spacer(minLength: 0)
            HStack(spacing: 0) {
                Button { rs.stages.swapAt(i, i - 1) } label: { Image(systemName: "chevron.up") }.disabled(i == 0).accessibilityLabel("Move stage \(i + 1) up")
                Button { rs.stages.swapAt(i, i + 1) } label: { Image(systemName: "chevron.down") }.disabled(i == rs.stages.count - 1).accessibilityLabel("Move stage \(i + 1) down")
                Button { rs.stages.remove(at: i) } label: { Image(systemName: "xmark") }.disabled(rs.stages.count == 1).accessibilityLabel("Remove stage \(i + 1)")
            }.buttonStyle(.borderless).foregroundColor(sub).frame(height: 24)
        }.padding(.leading, Theme.sp3).padding(.trailing, Theme.sp3).padding(.vertical, 10))
    }

    @ViewBuilder var autoRun: some View {
        banner("bolt", "Runs in the MR's workspace in a background tab, once per head commit, one job at a time. Never on goals marked hold, parked or automation. You're notified when it finishes or needs you. Saved as you change it.")
        box {
            ForEach(rs.autoKinds, id: \.kind) { k in
                toggleRow(RulesStore.kinds.first { $0.0 == k.kind }?.1 ?? k.kind.capitalized, k.when, Binding(get: { rs.autoOn[k.kind] != nil }, set: { rs.setAuto(k.kind, $0 ? 0 : nil) }))
                if let g = rs.autoOn[k.kind], !k.gates.isEmpty {
                    dependent(off: false) {
                        row("Then") {
                            Picker("\(k.kind): then", selection: Binding(get: { g }, set: { rs.setAuto(k.kind, $0) })) { ForEach(k.gates.indices, id: \.self) { Text(k.gates[$0]).tag($0) } }
                                .pickerStyle(.segmented).labelsHidden().fixedSize()
                        }
                    }
                }
            }
            if rs.autoKinds.isEmpty { note(rs.busy ? "Loading…" : "No auto-run jobs from \(hostLabel).").padding(Theme.sp4) }
        }
    }

    @ViewBuilder var prompts: some View {
        let kindName = RulesStore.kinds.first { $0.0 == rs.kind }?.1 ?? ""
        box {
            HStack(spacing: Theme.sp3) {
                Picker("Button", selection: Binding(get: { rs.kind }, set: { rs.kind = $0; rs.getPrompt() })) { ForEach(RulesStore.kinds, id: \.0) { Text($0.1).tag($0.0) } }
                    .pickerStyle(.segmented).labelsHidden().fixedSize()
                Spacer(minLength: Theme.sp2)
                Picker("Prompt", selection: Binding(get: { rs.reviewOwn }, set: { rs.reviewOwn = $0; if $0 && rs.review.isEmpty { rs.review = "{default}\n\n" } })) {
                    Text("Your own").tag(true); Text("Default").tag(false)
                }.pickerStyle(.segmented).labelsHidden().fixedSize()
            }.padding(.horizontal, Theme.sp3).padding(.vertical, 10)
            VStack(alignment: .leading, spacing: Theme.sp2) {
                note("What the \(kindName) button hands Claude. Private to you: \(greenlightHome)/prompts/\(rs.repo)/\(rs.kind).md on \(hostLabel), never in the repo. Fills in \(rs.reviewVars.joined(separator: " ")); {default} pulls in the default.")
                if rs.reviewOwn { editor($rs.review, "\(kindName) prompt for \(rs.repo)", height: 240) }
                else {
                    ScrollView { Text(rs.reviewDefault).font(sys(size: Theme.fs2, design: .monospaced)).foregroundColor(sub).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                        .padding(Theme.sp2).frame(height: 240).background(RoundedRectangle(cornerRadius: Theme.r1).fill(surface)).overlay(RoundedRectangle(cornerRadius: Theme.r1).stroke(edge))
                }
            }.padding(Theme.sp3).overlay(alignment: .top) { Rectangle().fill(line).frame(height: 1) }
        }
        saveRow("Save prompt", rs.saveReview)
    }

    @ViewBuilder var instructions: some View {
        heading("Instructions", "Standing instructions Greenlight reads in the chat and on every run: how to talk to you, what to never do, what to always check. A repo's own rules live in Repositories › Rules.")
        box("Instructions.md", aside: "On \(hostLabel)") { editor($rs.instructions, "Instructions for Greenlight", height: 380).padding(Theme.sp3) }
        saveRow("Save instructions", rs.saveInstructions)
    }

    @ViewBuilder var memory: some View {
        split("Memory", "What Greenlight has learned, one note each. It reads them before every run.",
              add: ("New note", { rs.note = ""; rs.noteText = "---\nname: \ndescription: \nmetadata:\n  type: project\n---\n\n" })) {
            ForEach(rs.notes, id: \.name) { n in
                listItem(n.name, n.text.split(separator: "\n").first { $0.hasPrefix("description:") }.map { $0.dropFirst(12).trimmingCharacters(in: .whitespaces) } ?? "",
                         on: rs.note == n.name, mono: true) { rs.note = n.name; rs.noteText = n.text }
            }
            if rs.notes.isEmpty { note(rs.busy ? "Loading…" : "No notes yet.").padding(Theme.sp3) }
        } detail: {
            let existing = rs.notes.contains { $0.name == rs.note }
            if existing { Text(rs.note + ".md").font(sys(size: 20, weight: .bold, design: .monospaced)).foregroundColor(fg) }
            else { TextField("my-decision", text: $rs.note).textFieldStyle(.roundedBorder).frame(width: 260).accessibilityLabel("New note name") }
            box("Note", aside: "Keep the frontmatter: it's the index line.") { editor($rs.noteText, "Note text", height: 340).padding(Theme.sp3) }
            saveRow("Save note", rs.saveNote) {
                Button("Delete note", role: .destructive, action: rs.deleteNote).disabled(rs.busy || !existing)
            }.disabled(rs.note.isEmpty)
            note("Notes live in greenlight's memory folder on \(hostLabel). A note deleted here can come back from this Mac's copy on the next sync.sh.")
        }
    }
}

/// The stage while voice mode is on (and the transcript isn't asked for), else the chat.
struct StageOr<A: View, B: View>: View {
    @ObservedObject var v: Voice
    @ViewBuilder var stage: () -> A
    @ViewBuilder var chat: () -> B
    var body: some View { if v.on && !v.transcript { stage() } else { chat() } }
}

struct ChatView: View, Looked {
    @ObservedObject var c: Core
    @FocusState private var typing: Bool
    @FocusState private var searching: Bool
    var body: some View {
        VStack(spacing: 0) {
            if lk.id == "zen" {  // Zen's only rule: the context used, as a 2px hairline
                GeometryReader { g in Rectangle().fill(c.ctxFull > 0.75 ? needs : accent).frame(width: g.size.width * c.ctxFull) }.frame(height: 2).background(card).accessibilityHidden(true)
            }
            header
            rule(top: true)
            ZStack(alignment: .top) {
            Group {  // settings cover the dashboard pane: App.settingsHost
            StageOr(v: c.voice) { StageView(v: c.voice, c: c) } chat: {
            VStack(spacing: 0) {
            ScrollViewReader { sv in
                ScrollView {
                    VStack(alignment: .leading, spacing: Theme.sp4) {
                        let started = c.msgs.contains { $0.role == .you }
                        ForEach(c.msgs.filter { !(started && $0.role == .note && $0.text.hasPrefix("Ask me anything")) }) { m in MsgView(m: m, c: c, name: c.name, ask: c.question(for: m)) }
                        if let r = c.run { RunCard(r: r, c: c) }
                        ForEach(Array(c.queue.enumerated()), id: \.offset) { i, q in
                            HStack { Spacer(minLength: 60); queued([q.text, q.files.map { "📎 \($0.name)" }.joined(separator: "  ")].filter { !$0.isEmpty }.joined(separator: "\n"), i) }
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }.padding(Theme.sp4)
                }
                .onAppear { DispatchQueue.main.async { sv.scrollTo("bottom", anchor: .bottom) } }  // after layout, or it lands short
                .onChange(of: c.msgs.count) { sv.scrollTo("bottom") }
                .onChange(of: c.run?.steps.count ?? -1) { sv.scrollTo("bottom") }
                .onChange(of: c.run?.live.count ?? -1) { sv.scrollTo("bottom") }
                .onChange(of: c.queue.count) { sv.scrollTo("bottom") }
            }
            if c.verbose { rule(top: false); FeedPanel(f: c.feed, c: c) }
            rule(top: false)
            footer
            }.id(c.look)
            .overlay { DropArea(c: c) }  // files dragged anywhere on the chat, the field too
            .overlay { if c.dropping { dropHint } }
            }
            }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay {  // HUD's corner brackets on the pane; its live stage draws its own
            if lk.id == "hud" { StageOr(v: c.voice) { EmptyView() } chat: { Brackets().stroke(accent, lineWidth: 2).allowsHitTesting(false).accessibilityHidden(true) } }
        }
        .background(bg)
        .foregroundColor(fg)
        .environment(\.colorScheme, lk.dark ? .dark : .light)  // the look's, for this pane only (not the window)
        .font(lk.font == monoCSS ? sys(size: Theme.fs3, design: .monospaced) : sys(size: Theme.fs3))
    }

    // MARK: the look's chrome (header, rules, queued messages, composer)

    var busy: Bool { c.run != nil }
    var stateWord: String { c.connected ? (busy ? "working" : "idle") : "offline" }
    var stateInk: Color { !c.connected ? crashed : busy ? accent : dim }
    var inset: CGFloat { lk.id == "editorial" || lk.id == "zen" ? Theme.sp5 : Theme.sp4 }
    /// A chrome label in the look's voice: caps and tracking (Orb, On Air, HUD, Editorial), lowercase (Zen), mono (Phosphor, HUD).
    func tag(_ s: String, _ size: CGFloat = Theme.fs1, _ w: Font.Weight = .regular, caps: Bool? = nil) -> Text {
        let up = caps ?? ["orb", "onair", "hud", "editorial"].contains(lk.id), code = lk.id == "phosphor" || lk.id == "hud"
        return Text(up ? s.uppercased() : lk.id == "zen" ? s.lowercased() : s)
            .font(sys(size: size, weight: w, design: code ? .monospaced : .default).monospacedDigit()).tracking(up ? 1.4 : 0)
    }

    var header: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: Theme.sp2) {
                title
                Spacer(minLength: Theme.sp2)
                ViewThatFits { HStack(spacing: Theme.sp2) { meta }.fixedSize(); EmptyView() }.layoutPriority(-1)  // a narrow pane drops the host, never clips
                if c.session == 0 { VoiceButtons(v: c.voice, out: c.chatOut, pop: { c.popChat(!c.chatOut) }) }  // voice and its own window: the main chat's
            }.padding(.leading, inset).padding(.trailing, lk.id == "editorial" ? inset : Theme.sp3)
                .padding(.top, lk.id == "zen" ? Theme.sp4 : Theme.sp3).padding(.bottom, lk.id == "zen" ? Theme.sp1 : Theme.sp3)
            if lk.id == "editorial" { masthead }
        }
    }

    /// The name as the look prints it; double-click to rename (Return or click away saves). Only the display changes, never c.name.
    @ViewBuilder func nameView(_ shown: String, _ f: Font, track: CGFloat = 0) -> some View {
        if c.editingName {
            TextField("Name", text: $c.name, onEditingChanged: { if !$0 { c.doneNaming() } }).textFieldStyle(.plain).font(f).frame(width: 160).onSubmit(c.doneNaming)
        } else {
            Text(shown).font(f).tracking(track).lineLimit(1).onTapGesture(count: 2) { c.editingName = true }.help("Double-click to rename")
        }
    }

    /// Who and how it is: an avatar (Conversation), the orb (Orb), a tally (On Air), a prompt (Phosphor), a masthead (Editorial), a callsign (HUD), a whisper (Zen).
    @ViewBuilder var title: some View {
        let n = c.name
        switch lk.id {
        case "conversation":
            Text(n.prefix(1).uppercased()).font(sys(size: Theme.fs4 - 1, weight: .semibold)).foregroundColor(busy ? .white : accent)
                .frame(width: 30, height: 30).background(Circle().fill(busy ? accent : accent.opacity(0.14))).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                nameView(n, sys(size: Theme.fs4 - 1, weight: .semibold))
                HStack(spacing: Theme.sp1) {
                    Circle().fill(c.connected ? (busy ? accent : done) : crashed).frame(width: 7, height: 7)
                    Text("\(host) · \(stateWord)\(tps)")
                }.font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(dim)
            }
        case "onair":
            let live = c.connected && busy
            HStack(spacing: 7) {
                Circle().fill(live ? .white : c.connected ? dim : crashed).frame(width: 7, height: 7)
                tag(!c.connected ? "Off air" : live ? "On air" : "Standby", Theme.fs2, .heavy)
            }.foregroundColor(live ? .white : fg).padding(.horizontal, Theme.sp3).frame(height: 24).background(Capsule().fill(live ? hx("D1242F") : hx("3A3A40")))
            nameView((live ? "LIVE · " : "") + n.uppercased(), sys(size: Theme.fs1, weight: .heavy), track: 1.5)
                .foregroundColor(page).padding(.horizontal, 10).frame(height: 24).background(accent)
        case "phosphor":
            nameView("\(n.lowercased())@greenlight", sys(size: Theme.fs3, design: .monospaced))
            Text("~/chat").foregroundColor(dim)
        case "editorial":
            nameView("The \(n)", sys(size: Theme.fs6 + 2, weight: .bold, design: .serif).italic())
        case "hud":
            nameView("\(n.uppercased())//CHAT", sys(size: Theme.fs1, design: .monospaced), track: 1.3).foregroundColor(accent)
            tag(!c.connected ? "✕ Offline" : busy ? "▶ Working" : "◌ Idle").foregroundColor(stateInk).shadow(color: busy ? accent.opacity(0.55) : .clear, radius: 5)
        case "zen":
            nameView(n.lowercased(), sys(size: Theme.fs3))
            Text("· \(stateWord)").font(sys(size: Theme.fs3)).foregroundColor(stateInk)
        default:  // orb: an amber orb, a second ring while it works
            Circle().fill(c.connected ? AnyShapeStyle(RadialGradient(colors: [hx("FFD27A"), accent, hx("B4740E")], center: UnitPoint(x: 0.35, y: 0.35), startRadius: 0, endRadius: 9)) : AnyShapeStyle(crashed))
                .frame(width: 14, height: 14).background(Circle().fill(accent.opacity(c.connected ? 0.14 : 0)).padding(-4))
                .background(Circle().fill(accent.opacity(busy ? 0.06 : 0)).padding(-9)).padding(.horizontal, 4).accessibilityHidden(true)
            nameView(n.uppercased(), sys(size: Theme.fs2, weight: .semibold), track: 1.7)
            tag("· \(stateWord)", Theme.fs2, .semibold).foregroundColor(stateInk).lineLimit(1).fixedSize()
        }
    }

    /// Host and tok/s at the right (Conversation has them under the name, Editorial in its dateline).
    @ViewBuilder var meta: some View {
        switch lk.id {
        case "conversation", "editorial": EmptyView()
        case "phosphor":
            Text("\(host)\(tps)").foregroundColor(dim).lineLimit(1)
            Text("[\(stateWord.uppercased())]").fontWeight(busy ? .bold : .regular).foregroundColor(c.connected ? (busy ? fg : dim) : crashed)
        default: tag("\(host)\(tps)", Theme.fs1, lk.id == "onair" ? .bold : .regular, caps: lk.id == "onair" || lk.id == "hud").foregroundColor(dim).lineLimit(1)
        }
    }

    /// Editorial: a double rule, the dateline, a rule.
    var masthead: some View {
        let state = !c.connected ? "Offline" : busy ? "Going to press" : "Idle"
        return VStack(spacing: 0) {
            VStack(spacing: 2) { Rectangle().fill(fg).frame(height: 1); Rectangle().fill(fg).frame(height: 1) }
            HStack {
                tag("\(Date().formatted(.dateTime.weekday(.wide))) edition · \(state)", Theme.fs1 - 1, .semibold).foregroundColor(stateInk)
                Spacer()
                tag("\(host)\(tps)", Theme.fs1 - 1, .semibold).foregroundColor(dim)
            }.padding(.vertical, 6)
            Rectangle().fill(fg).frame(height: 1)
        }.padding(.horizontal, inset)
    }

    /// The rule between header, list and footer, in the look's hand (Editorial's masthead draws the top one; Zen has none).
    @ViewBuilder func rule(top: Bool) -> some View {
        switch lk.id {
        case "onair": Rectangle().fill(top && busy ? hx("D1242F") : line).frame(height: 2)
        case "phosphor": HLine().stroke(line, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1)
        case "editorial": if !top { Rectangle().fill(fg).frame(height: 1).padding(.horizontal, inset) }
        case "zen": EmptyView()
        case "conversation": if top { Rectangle().fill(line).frame(height: 1) }
        default: Rectangle().fill(line).frame(height: 1)
        }
    }

    /// A message waiting for the run to finish: dashed and labelled, in the look's hand.
    @ViewBuilder func queued(_ q: String, _ i: Int) -> some View {
        let dash = StrokeStyle(lineWidth: 1, dash: [4, 3]), boxed = ["orb", "conversation", "onair", "hud"].contains(lk.id)
        let text = lk.id == "phosphor" ? Text("\(Text("you> ").foregroundColor(dim))\(Text(q))") : lk.id == "editorial" ? Text("“\(q)”").italic() : Text(q)
        VStack(alignment: .trailing, spacing: lk.id == "onair" ? 0 : Theme.sp1) {
            if lk.id == "onair" { tag("You · queued", Theme.fs1 - 1, .heavy).foregroundColor(dim).padding(.horizontal, Theme.sp2).padding(.vertical, 3).overlay(Rectangle().stroke(dim, style: dash)) }
            text.font(chatFace(size: lk.id == "editorial" ? Theme.fs4 : Theme.fs3)).foregroundColor(lk.id == "conversation" ? accent : lk.id == "zen" ? dim : lk.id == "phosphor" ? fg : sub)
                .multilineTextAlignment(.trailing).padding(.horizontal, boxed ? 14 : 0).padding(.vertical, boxed ? 10 : 0)
                .background {
                    switch lk.id {
                    case "orb": RoundedRectangle(cornerRadius: 18).stroke(line, style: dash)
                    case "conversation": UnevenRoundedRectangle(topLeadingRadius: 16, bottomLeadingRadius: 16, bottomTrailingRadius: 4, topTrailingRadius: 16).stroke(accent, style: dash)
                    case "onair": Rectangle().stroke(dim, style: dash)
                    case "hud": Rectangle().stroke(line, style: dash)
                    default: EmptyView()
                    }
                }
            if lk.id != "onair" { queuedLabel(i) }
        }.padding(.top, lk.id == "editorial" ? Theme.sp2 : 0)
            .overlay(alignment: .top) { if lk.id == "editorial" { HLine().stroke(dim, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1) } }
    }
    func queuedLabel(_ i: Int) -> some View {
        let s = switch lk.id {
        case "conversation": "Queued · sends after this run"
        case "phosphor": "(queued)"
        case "editorial": "Queued for the next edition"
        case "hud": "Queued · " + String(format: "%02d", i + 1)
        default: "Queued"
        }
        return tag(s, ["editorial", "hud"].contains(lk.id) ? Theme.fs1 - 1 : lk.id == "orb" ? Theme.fs1 : Theme.fs2, lk.id == "editorial" ? .bold : .regular).foregroundColor(dim)
    }

    /// ⌃R: fuzzy search over the prompts you sent; with no query, the recent ones.
    var histPanel: some View {
        let ms = c.hmatches, q = (c.hq ?? "").trimmingCharacters(in: .whitespaces)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: Theme.sp2) {
                Image(systemName: "magnifyingglass").foregroundColor(dim).accessibilityHidden(true)
                TextField("Search prompt history", text: Binding(get: { c.hq ?? "" }, set: { c.hq = $0; c.hsel = 0 }))
                    .textFieldStyle(.plain).focused($searching).onAppear { searching = true }
                if !q.isEmpty { Text("\(ms.count) match\(ms.count == 1 ? "" : "es")").font(sys(size: Theme.fs1).monospacedDigit()).foregroundColor(dim) }
            }.padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2)
            Divider().overlay(line)
            ScrollViewReader { sv in
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        if q.isEmpty && !ms.isEmpty { Text("RECENT").font(sys(size: Theme.fs1, weight: .semibold)).foregroundColor(dim).padding(.horizontal, Theme.sp2).padding(.top, Theme.sp1) }
                        ForEach(Array(ms.enumerated()), id: \.offset) { i, m in
                            Button { c.useHist(m.s.t) } label: {
                                HStack(spacing: Theme.sp3) {
                                    Text(marked(m.s.t, m.pos)).lineLimit(1).truncationMode(.tail).foregroundColor(sub)
                                    Spacer(minLength: 4)
                                    Text(ago(m.s.at)).font(sys(size: Theme.fs1)).foregroundColor(dim)
                                }.padding(.horizontal, Theme.sp2).padding(.vertical, Theme.sp2).contentShape(Rectangle())
                                    .background(RoundedRectangle(cornerRadius: Theme.r1).fill(i == c.hsel ? accent.opacity(0.16) : .clear))
                            }.buttonStyle(.plain).id(i)
                        }
                        if ms.isEmpty { Text(c.sent.isEmpty ? "Nothing sent yet" : "No prompts match").foregroundColor(dim).padding(Theme.sp3) }
                    }.padding(Theme.sp1)
                }.frame(maxHeight: 220)
                    .onChange(of: c.hsel) { sv.scrollTo(c.hsel) }
            }
            Divider().overlay(line)
            Text("↑↓ move · ↵ put in prompt · ⌃R ⌃S older · newer · esc back to your draft").font(sys(size: Theme.fs1)).foregroundColor(dim)
                .padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2)
        }.background(RoundedRectangle(cornerRadius: Theme.r2).fill(surface)).overlay(RoundedRectangle(cornerRadius: Theme.r2).stroke(edge))
    }

    func marked(_ t: String, _ pos: [Int]) -> AttributedString {
        let at = Set(pos)
        var a = AttributedString()
        for (i, ch) in t.prefix(300).enumerated() {
            var s = AttributedString(ch.isNewline ? " " : String(ch))
            if at.contains(i) { s.foregroundColor = accent; s.font = sys(size: Theme.fs3, weight: .semibold) }
            a += s
        }
        return a
    }

    var tps: String { c.run != nil && c.tps > 0 ? " · \(Int(c.tps)) tok/s" : c.lastTps > 0 ? " · last \(Int(c.lastTps)) tok/s" : "" }

    var footer: some View {
        VStack(alignment: .leading, spacing: Theme.sp2) {
            if !c.attached.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Theme.sp2) {
                        ForEach(c.attached) { a in
                            HStack(spacing: Theme.sp1) {
                                Image(systemName: "hexagon.fill").font(sys(size: Theme.glyph)).foregroundColor(a.color)  // ponytail: every look keeps the MR's colored hexagon
                                tag(a.label, Theme.fs1, .semibold, caps: lk.id == "onair" || lk.id == "editorial").lineLimit(1)
                                Button { c.attached.removeAll { $0.id == a.id }; c.unattach?(a.id) } label: {
                                    Image(systemName: "xmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(dim).frame(width: 16, height: 16)
                                }.buttonStyle(.plain).accessibilityLabel("Remove \(a.label) from the chat")
                            }.foregroundColor(lk.id == "phosphor" ? fg : sub).padding(.leading, lk.id == "zen" ? 0 : Theme.sp2).padding(.trailing, 3).frame(height: 24)
                                .background(RoundedRectangle(cornerRadius: corner(24)).fill(["orb", "conversation", "onair", "hud"].contains(lk.id) ? card : .clear))
                                .overlay(RoundedRectangle(cornerRadius: corner(24)).stroke(lk.id == "orb" || lk.id == "zen" ? .clear : line))
                                .help(a.sent ? "Already sent to \(c.name)" : "Goes to \(c.name) with your next message")
                        }
                    }
                }
            }
            if !c.files.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Theme.sp2) { ForEach(c.files) { fileChip($0) } }
                }
            }
            if !c.fileNote.isEmpty {
                Label(c.fileNote, systemImage: "exclamationmark.circle").font(sys(size: Theme.fs1)).foregroundColor(Theme.needsInk)
                    .fixedSize(horizontal: false, vertical: true).accessibilityLabel("Not attached: \(c.fileNote)")
            }
            if c.hq != nil { histPanel }
            HStack(alignment: .bottom, spacing: Theme.sp2) {
                if c.recording == nil { attachButton }
                if let since = c.recording {
                    HStack(spacing: 2) {
                        ForEach(c.levels.indices, id: \.self) { i in
                            Capsule().fill(crashed).frame(width: 3, height: 3 + CGFloat(c.levels[i]) * 19)
                        }
                    }.frame(height: 30).accessibilityHidden(true)
                    Text("Listening… " + fmt(Date().timeIntervalSince(since))).font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(dim)
                    Spacer(minLength: 0)
                } else {
                    TextField(c.transcribing ? "Transcribing…" : c.run != nil ? "Queue a follow-up…" : c.attached.isEmpty ? greeting
                              : "Ask about \(c.attached.count == 1 && c.attached[0].mrs == 1 ? c.attached[0].label : "these \(c.attached.reduce(0) { $0 + $1.mrs }) MRs")…", text: $c.input, axis: .vertical)
                        .textFieldStyle(.plain).font(chatFace(size: bare ? Theme.fs4 : Theme.fs3, weight: lk.id == "zen" ? .light : .regular))
                        .lineLimit(2...10).padding(.vertical, Theme.sp2).frame(minHeight: 50).focused($typing)  // two lines to write in, growing to ten
                        .padding(.leading, ["phosphor", "hud", "zen"].contains(lk.id) ? 18 : 0).overlay(alignment: lk.id == "zen" ? .bottomLeading : .topLeading) { promptGlyph }
                        .onKeyPress(.return, phases: .down) { k in
                            if k.modifiers.contains(.shift) {  // newline at the cursor
                                NSApp.sendAction(#selector(NSResponder.insertNewlineIgnoringFieldEditor(_:)), to: nil, from: nil)
                            } else { c.submit() }
                            return .handled
                        }
                    if c.vim && c.vimMode != "normal" {  // normal mode shows nothing
                        Text(c.vimMode).font(sys(size: Theme.fs1).italic()).foregroundColor(dim).padding(.bottom, Theme.sp2)
                    }
                }
                let rec = c.recording != nil, pad = padSize, r = corner(pad)
                Button(action: c.toggleVoice) {
                    Group {
                        if c.transcribing { ProgressView().controlSize(.small).scaleEffect(0.7) }
                        else if lk.id == "phosphor" { Text(rec ? "[stop]" : "[mic]").padding(.horizontal, 6) }
                        else { Image(systemName: rec ? "stop.fill" : "mic.fill").font(sys(size: Theme.fs3, weight: .semibold)) }
                    }.frame(minWidth: pad).frame(height: pad)
                        .background(RoundedRectangle(cornerRadius: r).fill(rec ? crashed : lk.id == "conversation" || lk.id == "onair" ? page : .clear))  // quiet until it records
                        .overlay(RoundedRectangle(cornerRadius: r).stroke(rec || bare ? .clear : line))
                        .foregroundColor(rec ? .white : lk.id == "phosphor" ? fg : lk.id == "zen" ? dim : sub)
                }.buttonStyle(.plain).disabled(c.transcribing)
                    .modifier(Tip(c: c, text: c.recording != nil ? "Stop and transcribe (\(c.shortcut))" : "Speak to \(c.name) (\(c.shortcut))"))
                    .accessibilityLabel(c.recording != nil ? "Stop recording" : "Speak")
                let empty = (c.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && c.files.isEmpty) || c.reading > 0  // submit() ignores it anyway
                Button(action: c.submit) {
                    sendLabel(empty).frame(minWidth: pad).frame(height: pad)
                        .background(RoundedRectangle(cornerRadius: r).fill(sendFill(empty)))
                        .overlay(RoundedRectangle(cornerRadius: r).stroke(sendEdge(empty)))
                        .foregroundColor(empty ? dim.opacity(0.6) : lk.id == "hud" || lk.id == "zen" ? accent : lk.dark ? page : .white)
                }.buttonStyle(.plain).disabled(empty).accessibilityLabel("Send")
            }
            .padding(.leading, bare ? 0 : lk.id == "orb" ? Theme.sp4 : Theme.sp3).padding(.trailing, bare ? 0 : 6).padding(.vertical, bare ? 0 : 3).padding(.bottom, bare ? 6 : 0)
            .background { composerBox }
            .onChange(of: c.hq == nil) { if c.hq == nil { typing = true } }  // ⌃R closed: back to the field
            if c.hp >= 0 {
                Text("History \(c.hp + 1) of \(c.walk.count)\(c.hsaved.isEmpty ? "" : " starting “\(c.hsaved.prefix(32))”") · ↑ older · ↓ newer\(c.vim ? "" : " · esc back to your draft")").font(sys(size: Theme.fs1)).foregroundColor(dim)
            }
            let onair = lk.id == "onair"
            ViewThatFits(in: .horizontal) {  // a narrow pane drops the hint, then the model; it never clips
                footRow(hint: true, model: true)
                footRow(hint: false, model: true)
                footRow(hint: false, model: false)
            }.font(sys(size: Theme.fs1).monospacedDigit()).controlSize(.small)
                .padding(.trailing, onair ? inset : 0).background(onair ? accent : .clear).padding(.horizontal, onair ? -inset : 0)  // full-bleed, the model flush left
        }.padding(.horizontal, inset).padding(.top, Theme.sp3).padding(.bottom, lk.id == "onair" ? 0 : Theme.sp4)
    }

    /// The composer's +: attach files (the open panel). Drops and ⌘V attach too.
    var attachButton: some View {
        let pad = padSize, r = corner(pad)
        return Button { c.pickFiles(NSApp.keyWindow) } label: {
            Group {
                if lk.id == "phosphor" { Text("[+]").padding(.horizontal, 4) }
                else { Image(systemName: "plus").font(sys(size: Theme.fs3, weight: .semibold)) }
            }.frame(minWidth: pad).frame(height: pad)
                .background(RoundedRectangle(cornerRadius: r).fill(lk.id == "conversation" || lk.id == "onair" ? page : .clear))
                .overlay(RoundedRectangle(cornerRadius: r).stroke(bare ? .clear : line))
                .foregroundColor(lk.id == "phosphor" ? fg : lk.id == "zen" ? dim : sub)
        }.buttonStyle(.plain)
            .modifier(Tip(c: c, text: "Attach files: \(ChatFile.supported). Or drop them here, or paste."))
            .accessibilityLabel("Attach files")
    }
    /// A file waiting to go with the next message: its thumbnail or icon, name and size, and × to take it off.
    func fileChip(_ f: ChatFile) -> some View {
        HStack(spacing: Theme.sp1) {
            if let t = f.thumb { Image(nsImage: t).resizable().aspectRatio(contentMode: .fill).frame(width: 18, height: 18).clipShape(RoundedRectangle(cornerRadius: 3)) }
            else { Image(systemName: f.kind == "pdf" ? "doc.richtext" : "doc.text").font(sys(size: Theme.fs2)).foregroundColor(dim) }
            tag(f.name, Theme.fs1, .semibold, caps: false).lineLimit(1).truncationMode(.middle).frame(maxWidth: 150, alignment: .leading)
            tag(byteSize(f.data.count), Theme.fs1, caps: false).foregroundColor(dim).lineLimit(1).fixedSize()
            Button { c.files.removeAll { $0.id == f.id } } label: {
                Image(systemName: "xmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(dim).frame(width: 16, height: 16)
            }.buttonStyle(.plain).accessibilityLabel("Remove \(f.name)")
        }.foregroundColor(lk.id == "phosphor" ? fg : sub).padding(.leading, lk.id == "zen" ? 0 : 3).padding(.trailing, 3).frame(height: 24)
            .background(RoundedRectangle(cornerRadius: corner(24)).fill(["orb", "conversation", "onair", "hud"].contains(lk.id) ? card : .clear))
            .overlay(RoundedRectangle(cornerRadius: corner(24)).stroke(lk.id == "orb" || lk.id == "zen" ? .clear : line))
            .help("\(ChatFile.kinds[f.kind]!.capitalized), goes to \(c.name) with your next message")
            .accessibilityElement(children: .contain).accessibilityLabel("Attached \(f.name), \(byteSize(f.data.count))")
    }
    /// Files dragged over the chat: where they'll go.
    var dropHint: some View {
        RoundedRectangle(cornerRadius: corner(40) > 0 ? Theme.r3 : 0).fill(bg.opacity(0.82))
            .overlay(RoundedRectangle(cornerRadius: corner(40) > 0 ? Theme.r3 : 0).stroke(accent, style: StrokeStyle(lineWidth: 2, dash: [6, 4])))
            .overlay(VStack(spacing: Theme.sp2) {
                Image(systemName: "paperclip").font(sys(size: Theme.fs6)).foregroundColor(accent)
                Text("Drop to attach").font(sys(size: Theme.fs4, weight: .semibold))
                Text(ChatFile.supported.prefix(1).uppercased() + ChatFile.supported.dropFirst()).font(sys(size: Theme.fs1)).foregroundColor(dim)
            })
            .padding(Theme.sp3).allowsHitTesting(false).accessibilityHidden(true)
    }

    func footRow(hint: Bool, model: Bool) -> some View {
        let onair = lk.id == "onair"
        return HStack(spacing: Theme.sp2) {
            if onair && model { modelButton.fixedSize() }  // On Air: the model first, inverse, on the amber strip
            if hint {
                tag((c.run != nil ? "Sends after this run" : "⌃R history") + " · ↩ send", lk.id == "editorial" ? Theme.fs1 - 1 : Theme.fs1, onair ? .heavy : lk.id == "editorial" ? .semibold : .regular)
                    .foregroundColor(onair ? page : dim).lineLimit(1).fixedSize()
            }
            Spacer(minLength: 4)
            if !onair && model { modelButton.fixedSize() }
            ContextButton(c: c)
            newChat
        }
    }

    var bare: Bool { lk.id == "editorial" || lk.id == "zen" }  // no boxes, just type and rules
    /// Corners for the look's controls: round (Orb), 6pt (Conversation), square (the rest).
    func corner(_ h: CGFloat) -> CGFloat { lk.id == "orb" ? h / 2 : lk.id == "conversation" ? Theme.r1 : 0 }
    var padSize: CGFloat { switch lk.id { case "onair": 36; case "orb", "conversation": 34; case "hud", "editorial": 32; default: 30 } }
    var greeting: String { lk.id == "editorial" ? "Write to the desk…" : lk.id == "phosphor" ? "talk to \(c.name.lowercased())_" : "Talk to \(c.name)…" }

    /// What leads the line you type: a shell prompt (Phosphor), a caret (HUD), a dot that lights while you type (Zen).
    @ViewBuilder var promptGlyph: some View {
        switch lk.id {
        case "phosphor": Text("$").foregroundColor(typing ? fg : dim).padding(.top, Theme.sp2).accessibilityHidden(true)
        case "hud": Text(">").font(sys(size: Theme.fs2, design: .monospaced)).foregroundColor(accent).padding(.top, Theme.sp2).accessibilityHidden(true)
        case "zen": Circle().fill(typing || c.hp >= 0 ? accent : hx("4A4A52")).frame(width: 6, height: 6).padding(.bottom, 15).accessibilityHidden(true)
        default: EmptyView()
        }
    }
    /// The composer's frame: a capsule (Orb, Conversation), a square (On Air, HUD), a dashed box (Phosphor), a rule under it
    /// (Editorial, Zen). Focus lights it in the look's accent; walking the history (↑↓) thickens it.
    @ViewBuilder var composerBox: some View {
        let hot = typing || c.hp >= 0, walk: CGFloat = c.hp >= 0 ? 1 : 0
        switch lk.id {
        case "editorial", "zen":
            Rectangle().fill(hot ? accent : lk.id == "zen" ? line : fg).frame(height: (hot && lk.id == "editorial" ? 2 : 1) + walk).frame(maxHeight: .infinity, alignment: .bottom)
        case "phosphor":
            Rectangle().fill(hot ? card : .clear).overlay(Rectangle().stroke(hot ? fg : line, style: StrokeStyle(lineWidth: 1 + walk, dash: [4, 3])))
        default:
            let r: CGFloat = lk.id == "orb" ? 24 : lk.id == "conversation" ? 22 : 0
            RoundedRectangle(cornerRadius: r).fill(lk.id == "conversation" || lk.id == "hud" ? page : card)
                .overlay(RoundedRectangle(cornerRadius: r).stroke(hot ? accent : line, lineWidth: (lk.id == "onair" ? 2 : 1) + walk))
                .background(RoundedRectangle(cornerRadius: r + 3).fill(accent.opacity(lk.id == "conversation" && hot ? 0.15 : 0)).padding(-3))  // Conversation's focus ring
        }
    }
    @ViewBuilder func sendLabel(_ empty: Bool) -> some View {
        switch lk.id {
        case "phosphor": Text("[ send ]").padding(.horizontal, 6)
        case "hud": tag("[ Send ]", Theme.fs2, .bold).padding(.horizontal, Theme.sp3)
        case "editorial": Text("Send").font(sys(size: Theme.fs3, weight: .bold)).padding(.horizontal, empty ? 2 : 14)
        default: Image(systemName: "arrow.up").font(sys(size: Theme.fs3, weight: .bold))
        }
    }
    func sendFill(_ empty: Bool) -> Color {
        if empty { return lk.id == "orb" || lk.id == "onair" ? line : lk.id == "conversation" ? card : .clear }
        return lk.id == "hud" ? accent.opacity(0.1) : lk.id == "zen" ? .clear : accent
    }
    func sendEdge(_ empty: Bool) -> Color { lk.id == "hud" ? (empty ? line : accent) : lk.id == "phosphor" && empty ? line : .clear }

    /// Model and effort, as the look would print it; opens Settings.
    var modelButton: some View {
        let m = c.modelName, e = c.isClaude ? effortName(c.effort) : nil, onair = lk.id == "onair"
        let label = switch lk.id {
        case "phosphor": "--model \(m.lowercased().replacingOccurrences(of: " ", with: "-"))" + (e.map { " --effort \($0.lowercased())" } ?? "")
        case "editorial": "Set in \(m)" + (e.map { " · \($0) effort" } ?? "")
        case "hud": "Mdl \(m)" + (e.map { " · eff \($0)" } ?? "")
        case "onair": m + (e.map { " · effort \($0)" } ?? "")
        default: m + (e.map { " · \($0)" } ?? "")
        }
        return Button { c.settingsOpen = true } label: {
            HStack(spacing: Theme.sp1) {
                tag(label, Theme.fs1, onair ? .heavy : lk.id == "editorial" ? .bold : .regular).lineLimit(1).truncationMode(.middle)  // after the hint, the model gives way
                if lk.id == "conversation" || lk.id == "hud" { Image(systemName: "chevron.up.chevron.down").font(sys(size: Theme.glyph, weight: .semibold)) }
            }.foregroundColor(onair ? accent : lk.id == "phosphor" ? fg : lk.id == "zen" ? dim : sub)
                .padding(.horizontal, bare ? 0 : onair ? Theme.sp3 : Theme.sp2).frame(height: onair ? 34 : 24)
                .background(RoundedRectangle(cornerRadius: corner(24)).fill(onair ? page : lk.id == "conversation" ? card : .clear))
                .overlay(RoundedRectangle(cornerRadius: corner(24)).stroke(bare || onair ? .clear : line)).contentShape(Rectangle())
        }.buttonStyle(.plain).fixedSize(horizontal: false, vertical: true).modifier(Tip(c: c, text: "Change model and effort in Settings"))
    }

    static let newTip = "New chat — forget this conversation and start fresh"

    var newChat: some View {
        let hot = c.hint == Self.newTip
        return Button(action: { c.clear() }) {
            Image(systemName: "square.and.pencil").font(sys(size: Theme.fs2, weight: .medium)).frame(width: 26, height: 26)
                .foregroundColor(hot ? accent : lk.id == "phosphor" ? fg : sub)
                .background(RoundedRectangle(cornerRadius: corner(26)).fill(lk.id == "onair" ? card : hot ? accent.opacity(0.18) : lk.id == "conversation" ? card : .clear))  // On Air sits on amber: tile stays dark
                .overlay(RoundedRectangle(cornerRadius: corner(26)).stroke(hot ? (lk.id == "onair" ? fg : accent) : bare ? .clear : line))
        }.buttonStyle(.plain).modifier(Tip(c: c, text: Self.newTip)).accessibilityLabel("New chat")
    }
}

/// One "Context N%" control; its popover has the token totals and Compact. Neutral until the window is nearly full, then orange.
struct ContextButton: View, Looked {
    @ObservedObject var c: Core
    var body: some View {
        let full = c.ctxFull, high = full > 0.75
        // the footer's control shape: a capsule (Orb), 6pt (Conversation), a tile (On Air), a box (HUD, Phosphor), bare (Editorial, Zen)
        let r: CGFloat = lk.id == "orb" ? 12 : lk.id == "conversation" ? Theme.r1 : 0, bare = lk.id == "editorial" || lk.id == "zen"
        let up = ["orb", "onair", "hud", "editorial"].contains(lk.id), code = lk.id == "phosphor" || lk.id == "hud"
        return Button { c.ctxOpen.toggle() } label: {
            HStack(spacing: Theme.sp1) {
                ZStack {
                    Circle().stroke(edge, lineWidth: 2)
                    Circle().trim(from: 0, to: max(full, 0.02)).stroke(high ? needs : sub, style: StrokeStyle(lineWidth: 2, lineCap: .round)).rotationEffect(.degrees(-90))
                }.frame(width: 12, height: 12)
                Text(up ? "CONTEXT \(c.ctxShort)" : lk.id == "zen" ? "context \(c.ctxShort)" : "Context \(c.ctxShort)").tracking(up ? 1.2 : 0).lineLimit(1).fixedSize()
                    .font(sys(size: Theme.fs1, weight: lk.id == "onair" ? .heavy : .regular, design: code ? .monospaced : .default).monospacedDigit())
            }.foregroundColor(high ? needsInk : lk.id == "phosphor" ? fg : sub).padding(.horizontal, bare ? 0 : Theme.sp2).frame(height: 24)
                .background(RoundedRectangle(cornerRadius: r).fill(lk.id == "onair" ? card : c.ctxOpen ? accent.opacity(0.18) : lk.id == "conversation" ? card : .clear))  // On Air sits on amber
                .overlay(RoundedRectangle(cornerRadius: r).stroke(high ? needs.opacity(0.6) : c.ctxOpen && lk.id == "onair" ? fg : bare ? .clear : line)).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("Context, \(c.ctxSpoken)").modifier(CtxPop(c: c))
    }
}

/// The context popover: token totals and Compact. On the chat's Context button and each voice look's readout.
struct CtxPop: ViewModifier, Looked {
    @ObservedObject var c: Core
    func body(content: Content) -> some View {
        let can = c.run == nil && c.isClaude && c.context > 0, full = c.ctxFull, high = full > 0.75
        return content.popover(isPresented: $c.ctxOpen, arrowEdge: .top) {
                VStack(alignment: .leading, spacing: Theme.sp2) {
                    Text("Context").font(sys(size: Theme.fs3, weight: .semibold))
                    Text(c.window > 0 ? "\(kfmt(c.context)) of \(kfmt(c.window)) tokens used" : "\(kfmt(c.context)) tokens used")
                        .font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(high ? needsInk : sub)
                    if c.window > 0 { ProgressView(value: full).tint(high ? needs : sub) }
                    Button { c.compact(); c.ctxOpen = false } label: { Label("Compact", systemImage: "arrow.down.right.and.arrow.up.left") }.disabled(!can)
                    Text(!c.isClaude ? "Only Claude can compact." : c.run != nil ? "Wait for the reply to finish." : "Summarizes the chat to free context.")
                        .font(sys(size: Theme.fs1)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
                }.padding(Theme.sp3).frame(width: 240)
                    // in the look, not the window's appearance: the look's ink on the look's stage (it opens from the live looks too)
                    .font(lk.font == monoCSS ? sys(size: Theme.fs3, design: .monospaced) : sys(size: Theme.fs3)).foregroundColor(fg)
                    .environment(\.colorScheme, lk.dark ? .dark : .light).presentationBackground(bg)
            }
    }

}
extension Core {
    var ctxFull: Double { window > 0 ? min(Double(context) / Double(window), 1) : 0 }
    /// The readout: "27%", or the tokens ("35k") while the window is unknown (a harness that doesn't say, or not yet).
    var ctxShort: String { window > 0 ? "\(Int((ctxFull * 100).rounded()))%" : kfmt(context) }
    var ctxSpoken: String { window > 0 ? "\(Int((ctxFull * 100).rounded())) percent used" : "\(kfmt(context)) tokens used" }
}

// MARK: vim

/// What the vim engine edits: the input's field editor, or a plain string in `--vimtest`.
protocol VimBuffer: AnyObject {
    var vimText: NSString { get }
    var vimSel: NSRange { get set }
    func vimReplace(_ r: NSRange, _ s: String)
    func vimUndo()
    func vimRedo()
    func vimVertical(_ down: Bool)
}

extension NSTextView: VimBuffer {
    var vimText: NSString { string as NSString }
    var vimSel: NSRange {
        get { selectedRange() }
        set { setSelectedRange(newValue); scrollRangeToVisible(newValue) }
    }
    func vimReplace(_ r: NSRange, _ s: String) {
        if shouldChangeText(in: r, replacementString: s) { replaceCharacters(in: r, with: s); didChangeText() }  // keeps undo and the SwiftUI binding
    }
    func vimUndo() { undoManager?.undo() }
    func vimRedo() { undoManager?.redo() }
    func vimVertical(_ down: Bool) { down ? moveDown(nil) : moveUp(nil) }
}

/// Vim keys for the input: normal, insert and visual modes over the field's text.
/// ponytail: no counts, marks, text objects or linewise paste; add them if they're missed.
final class Vim {
    var mode = "insert" { didSet { if mode != oldValue { onMode(mode) } } }
    var onMode: (String) -> Void = { _ in }
    var pending = ""
    var reg = ""
    var anchor = 0, cur = 0  // visual mode's fixed end and moving end

    /// One key ("\u{1b}" is Esc, "^r" is ⌃R). Returns true when vim used it, so it never reaches the field.
    func key(_ k: String, _ b: VimBuffer) -> Bool {
        let s = b.vimText, n = s.length
        let p = mode == "visual" ? cur : min(b.vimSel.location, n)
        let ls = lineStart(s, p), le = lineEnd(s, p)
        if k == "\u{1b}" {
            pending = ""
            if mode == "insert" { b.vimSel = NSRange(location: p > ls ? p - 1 : p, length: 0) }
            if mode == "visual" { b.vimSel = NSRange(location: p, length: 0) }
            mode = "normal"
            return true
        }
        if mode == "insert" { return false }
        if k == "^r" { b.vimRedo(); return true }
        if k.hasPrefix("^") { return false }  // other ⌃ keys keep their Cocoa meaning

        if mode == "visual" {
            let sel = NSRange(location: min(anchor, cur), length: min(max(anchor, cur) + 1, n) - min(anchor, cur))
            switch k {
            case "v": mode = "normal"; b.vimSel = NSRange(location: p, length: 0)
            case "d", "x": apply("d", sel, b)
            case "c", "s": apply("c", sel, b)
            case "y": apply("y", sel, b)
            default:
                let seq = pending + k
                pending = ""
                if seq == "g" { pending = seq; return true }
                if let (t, _) = motion(seq, s, p) { cur = min(t, max(n - 1, 0)); select(b) }
            }
            return true
        }

        let seq = pending + k
        pending = ""
        if ["d", "c", "y", "g", "r", "dg", "cg", "yg"].contains(seq) { pending = seq; return true }
        if seq.count == 2, seq.first == "r" {
            if p < le { b.vimReplace(NSRange(location: p, length: 1), String(seq.dropFirst())); b.vimSel = NSRange(location: p, length: 0) }
            return true
        }
        if let op = seq.first, "dcy".contains(op), seq.count > 1 {
            let m = String(seq.dropFirst())
            if m == String(op) {  // dd, cc, yy: the whole line
                var r = NSRange(location: ls, length: le - ls)
                if op == "d" { if le < n { r.length += 1 } else if ls > 0 { r = NSRange(location: ls - 1, length: le - ls + 1) } }
                apply(op, r, b)
            } else if let (t, inc) = motion(op == "c" && m == "w" ? "e" : m, s, p) {  // cw acts like ce, as in vim
                let lo = min(p, t), hi = min(max(p, t) + (inc ? 1 : 0), n)
                apply(op, NSRange(location: lo, length: hi - lo), b)
            }
            return true
        }
        switch seq {
        case "i": mode = "insert"
        case "a": b.vimSel = NSRange(location: min(p + 1, le), length: 0); mode = "insert"
        case "I": b.vimSel = NSRange(location: motion("^", s, p)!.0, length: 0); mode = "insert"
        case "A": b.vimSel = NSRange(location: le, length: 0); mode = "insert"
        case "x": if p < le { apply("d", NSRange(location: p, length: 1), b) }
        case "X": if p > ls { apply("d", NSRange(location: p - 1, length: 1), b) }
        case "s": apply("c", NSRange(location: p, length: p < le ? 1 : 0), b)
        case "D": apply("d", NSRange(location: p, length: le - p), b)
        case "C": apply("c", NSRange(location: p, length: le - p), b)
        case "p", "P":
            guard !reg.isEmpty else { break }
            let at = seq == "p" && p < le ? p + 1 : p
            b.vimReplace(NSRange(location: at, length: 0), reg)
            b.vimSel = NSRange(location: at + (reg as NSString).length - 1, length: 0)
        case "u": b.vimUndo()
        case "~":
            if p < le {
                let ch = s.substring(with: NSRange(location: p, length: 1))
                b.vimReplace(NSRange(location: p, length: 1), ch == ch.uppercased() ? ch.lowercased() : ch.uppercased())
                b.vimSel = NSRange(location: clamp(s, p + 1), length: 0)
            }
        case "v": mode = "visual"; anchor = p; cur = p; select(b)
        case "j", "k": b.vimVertical(seq == "j")
        default:
            if let (t, _) = motion(seq, s, p) { b.vimSel = NSRange(location: clamp(b.vimText, t), length: 0) }
        }
        return true  // normal mode never types
    }

    /// Where a motion lands from p, and whether an operator includes that character (e, $ do; w, b don't).
    func motion(_ m: String, _ s: NSString, _ p: Int) -> (Int, Bool)? {
        let n = s.length, ls = lineStart(s, p), le = lineEnd(s, p)
        func cls(_ i: Int) -> Int {
            let c = Character(UnicodeScalar(s.character(at: i)) ?? " ")
            return c.isWhitespace ? 0 : c.isLetter || c.isNumber || c == "_" ? 1 : 2
        }
        var i = p
        switch m {
        case "h": return (max(p - 1, ls), false)
        case "l": return (min(p + 1, le), false)
        case "0": return (ls, false)
        case "$": return (le, false)
        case "^":
            i = ls
            while i < le && cls(i) == 0 { i += 1 }
            return (i, false)
        case "w":
            if i < n { let c = cls(i); while i < n && c != 0 && cls(i) == c { i += 1 } }
            while i < n && cls(i) == 0 { i += 1 }
            return (i, false)
        case "b":
            while i > 0 && cls(i - 1) == 0 { i -= 1 }
            if i > 0 { let c = cls(i - 1); while i > 0 && cls(i - 1) == c { i -= 1 } }
            return (i, false)
        case "e":
            i += 1
            while i < n && cls(i) == 0 { i += 1 }
            if i < n { let c = cls(i); while i + 1 < n && cls(i + 1) == c { i += 1 } }
            return (min(i, max(n - 1, 0)), true)
        case "gg": return (0, false)
        case "G": return (n, false)
        default: return nil
        }
    }

    func apply(_ op: Character, _ r: NSRange, _ b: VimBuffer) {
        reg = b.vimText.substring(with: r)
        if op != "y" { b.vimReplace(r, "") }
        mode = op == "c" ? "insert" : "normal"
        b.vimSel = NSRange(location: op == "c" ? r.location : clamp(b.vimText, r.location), length: 0)
    }

    func select(_ b: VimBuffer) {
        let n = b.vimText.length, lo = min(anchor, cur)
        b.vimSel = NSRange(location: lo, length: min(max(anchor, cur) + 1, n) - lo)
    }

    /// Normal mode sits on a character, never past the line's last one.
    func clamp(_ s: NSString, _ t: Int) -> Int {
        let t = min(t, s.length), ls = lineStart(s, t), le = lineEnd(s, t)
        return min(t, max(le - 1, ls))
    }

    func lineStart(_ s: NSString, _ p: Int) -> Int { s.lineRange(for: NSRange(location: min(p, s.length), length: 0)).location }
    func lineEnd(_ s: NSString, _ p: Int) -> Int {
        var end = 0, contents = 0
        s.getLineStart(nil, end: &end, contentsEnd: &contents, for: NSRange(location: min(p, s.length), length: 0))
        return contents
    }
}

// MARK: app

/// The orb's look: hovered, pressed (FloatHost reports it), and which way it goes from here.
final class OrbState: ObservableObject { @Published var hover = false; @Published var pressed = false; @Published var up = true; @Published var matte = true; var tap: () -> Void = {} }

func hc(_ s: String) -> Color { Color(nsColor: hex(s)) }
/// The orb's two faces. Up: a soft matte light-green button (or, Settings → Appearance, a lit traffic light in a beige housing). Down: the terminal button, an amber dot on it.
struct LevelFace: View {
    let up: Bool, matte: Bool
    var body: some View {
        if up && matte {
            ZStack(alignment: .topTrailing) {
                Circle().fill(RadialGradient(colors: [hc("DDF6E3"), hc("B6E7C3")], center: UnitPoint(x: 0.4, y: 0.3), startRadius: 2, endRadius: 30))
                    .overlay(Circle().strokeBorder(hc("9CD6AC").opacity(0.7), lineWidth: 1)).shadow(color: hc("1E4A2B").opacity(0.22), radius: 6, y: 3)
                Image(systemName: "chevron.up").font(.system(size: 18, weight: .semibold)).foregroundColor(hc("2E6F43")).frame(maxWidth: .infinity, maxHeight: .infinity)
                Circle().fill(hc("56E08B")).frame(width: 8, height: 8).shadow(color: hc("56E08B"), radius: 3).overlay(Circle().stroke(hc("D2F1DA"), lineWidth: 2)).padding(9)
            }
        } else if up {
            ZStack {
                Circle().fill(hc("E9DEC6")).overlay(Circle().strokeBorder(hc("C9B992"), lineWidth: 1))
                    .shadow(color: .black.opacity(0.45), radius: 3, y: 2).shadow(color: hc("3CE678").opacity(0.45), radius: 5)
                Circle().fill(hc("14130F")).padding(5)
                Circle().fill(RadialGradient(gradient: Gradient(stops: [.init(color: hc("CFFFE0"), location: 0), .init(color: hc("5DF08E"), location: 0.22),
                                                                         .init(color: hc("18C25A"), location: 0.55), .init(color: hc("0A7A35"), location: 1)]),
                                             center: UnitPoint(x: 0.5, y: 0.6), startRadius: 0, endRadius: 19))
                    .padding(8).shadow(color: hc("50F08C").opacity(0.75), radius: 4)
                Ellipse().fill(.white.opacity(0.55)).frame(width: 10, height: 5).rotationEffect(.degrees(-20)).offset(x: -7, y: -11)
            }
        } else {
            ZStack(alignment: .topTrailing) {
                Circle().fill(hc("1C1C1F")).overlay(Circle().strokeBorder(.white.opacity(0.18), lineWidth: 1)).shadow(color: .black.opacity(0.55), radius: 3, y: 2)
                Image(systemName: "terminal").font(.system(size: 20, weight: .medium)).foregroundColor(hc("ECECEE")).frame(maxWidth: .infinity, maxHeight: .infinity)
                Circle().fill(hc("FFB340")).frame(width: 8, height: 8).overlay(Circle().stroke(hc("1C1C1F"), lineWidth: 2)).padding(9)
            }
        }
    }
}

/// The window bar's floor indicator: L1 B1 B2, the level you're on lit and named; click another to go there.
final class FloorState: ObservableObject { @Published var at = "pipe"; @Published var project = ""; @Published var colors = [String]() }  // project: the window's, centered in the title bar; colors: its squares (none: All projects)
struct Floors: View {
    @ObservedObject var s: FloorState
    @ObservedObject var c: Core  // its look bumps when the mode or theme changes
    var light: Bool { _ = c.look; return !theme.dark }
    var pick: () -> Void = {}  // the project chip: a terminal window's Open projects, L1's menu
    let go: (String) -> Void
    static let levels = [("greenlight", "L1", "GREENLIGHT", "#2E6F43", "#FFFFFF", "#56E08B"), ("basement", "B1", "BASEMENT", "#FFB340", "#1C1C1F", "#FFB340"),
                         ("pipe", "B2", "PIPE", "#A8661C", "#FFF3E0", "#E08A2E")]
    var body: some View {
        HStack(spacing: 10) {
        HStack(spacing: 2) {
            ForEach(0..<3, id: \.self) { i in
                let l = Self.levels[i], on = l.0 == s.at
                Button { if !on { go(l.0) } } label: {
                    Text(on ? "\(l.1) · \(l.2)" : l.1).font(.system(size: 9.5, weight: on ? .bold : .semibold, design: .monospaced)).tracking(on ? 1.1 : 0.95)
                        .foregroundColor(on ? hexColor(l.4) : hexColor(light ? "#5F5F64" : "#9A9AA0"))
                        .padding(.horizontal, on ? 8 : 7).frame(height: 16)
                        .background(RoundedRectangle(cornerRadius: 5).fill(on ? hexColor(l.3) : .clear).shadow(color: on ? hexColor(l.5).opacity(0.45) : .clear, radius: 5))
                        .contentShape(Rectangle())
                }.buttonStyle(.plain).help(on ? "You're on \(l.1) · \(l.2.capitalized)" : "Go to \(l.1) · \(l.2.capitalized)")
                .accessibilityLabel("\(l.1) · \(l.2.capitalized)").accessibilityAddTraits(on ? .isSelected : [])
            }
        }.padding(2)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color(white: light ? 0 : 1, opacity: light ? 0.05 : 0.06))
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color(white: light ? 0 : 1, opacity: light ? 0.07 : 0.08))))
        HStack(spacing: 2) {  // Auto / Light / Dark, in the same track
            ForEach([("auto", "circle.lefthalf.filled", "Auto (match macOS)"), ("light", "sun.max", "Light"), ("dark", "moon", "Dark")], id: \.0) { m in
                let on = appearanceMode == m.0
                Button { c.restyle { ud.set(m.0, forKey: "appearance") }; (NSApp.delegate as? App)?.tick() } label: {
                    Image(systemName: m.1).font(.system(size: 10, weight: .semibold))
                        .foregroundColor(hexColor(on ? (light ? "#1D1D1F" : "#F5F5F7") : (light ? "#5F5F64" : "#9A9AA0")))
                        .frame(width: 22, height: 16)
                        .background(RoundedRectangle(cornerRadius: 5).fill(on ? Color(white: light ? 0 : 1, opacity: light ? 0.1 : 0.14) : .clear))
                        .contentShape(Rectangle())
                }.buttonStyle(.plain).help(m.2).accessibilityLabel("Appearance: \(m.2)").accessibilityAddTraits(on ? .isSelected : [])
            }
        }.padding(2)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color(white: light ? 0 : 1, opacity: light ? 0.05 : 0.06))
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color(white: light ? 0 : 1, opacity: light ? 0.07 : 0.08))))
        }.frame(width: 300, height: 28, alignment: .trailing).padding(.trailing, 10)
    }
}
/// The project chip: the window's projects, centered in the title bar like a title; a click opens the picker.
struct ProjectChip: View {
    @ObservedObject var s: FloorState
    @ObservedObject var c: Core
    var light: Bool { _ = c.look; return !theme.dark }
    let pick: () -> Void
    var body: some View {
        if !s.project.isEmpty && s.at != "pipe" {  // the Pipe is just the terminals: no project chip there
            Button(action: pick) {  // the chip (design A): its squares, the name, a chevron
                HStack(spacing: 6) {
                    if s.colors.isEmpty { Image(systemName: "square.grid.2x2.fill").font(.system(size: 9)).opacity(0.55) }
                    ForEach(Array(s.colors.prefix(4).enumerated()), id: \.offset) { RoundedRectangle(cornerRadius: 2.5).fill(hexColor($0.element)).frame(width: 9, height: 9) }
                    Text(s.project).lineLimit(1).truncationMode(.tail)
                    Image(systemName: "chevron.down").font(.system(size: 8, weight: .bold)).opacity(0.6)
                }.font(.system(size: 11.5, weight: .semibold)).foregroundColor(hexColor(light ? "#1D1D1F" : "#F5F5F7"))
                    .padding(.horizontal, 7).frame(height: 22)
                    .background(RoundedRectangle(cornerRadius: 7).fill(Color(white: light ? 0 : 1, opacity: light ? 0.05 : 0.06))
                        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color(white: light ? 0 : 1, opacity: light ? 0.07 : 0.08))))
                    .contentShape(Rectangle())
            }.buttonStyle(.plain).frame(maxWidth: 260).help("\(s.project) · Show projects…").accessibilityLabel("Projects: \(s.project)")
        }
    }
}
/// Floors in a window's title bar, top right; the project chip in its middle.
func floorBar(_ w: NSWindow, _ f: Floors) {
    let a = NSTitlebarAccessoryViewController(), h = NSHostingView(rootView: f)
    h.frame = NSRect(x: 0, y: 0, width: 310, height: 28)
    a.view = h; a.layoutAttribute = .trailing
    w.addTitlebarAccessoryViewController(a)
    guard let tb = w.standardWindowButton(.closeButton)?.superview else { return }  // the title bar itself: no accessory slot is centered
    let p = NSHostingView(rootView: ProjectChip(s: f.s, c: f.c, pick: f.pick)); p.translatesAutoresizingMaskIntoConstraints = false
    tb.addSubview(p)
    let mid = p.centerXAnchor.constraint(equalTo: tb.centerXAnchor); mid.priority = .defaultHigh  // centered, unless the floors are in the way
    NSLayoutConstraint.activate([mid, p.centerYAnchor.constraint(equalTo: tb.centerYAnchor),
                                 p.trailingAnchor.constraint(lessThanOrEqualTo: tb.trailingAnchor, constant: -(h.frame.width + 8))])  // clear of the floors (same superview: safe at creation)
}

/// The orb: in the Basement it goes up to Greenlight Center, there it comes back down; drag puts it anywhere.
struct OrbFace: View {
    @ObservedObject var s: OrbState
    var body: some View {
        LevelFace(up: s.up, matte: s.matte).frame(width: 50, height: 50).brightness(s.hover ? 0.04 : 0).scaleEffect(s.pressed ? 0.94 : 1).frame(width: 64, height: 64)
            .contentShape(Circle()).onHover { s.hover = $0 }
            .help(s.up ? "Up to Greenlight Center · ⌘⇧G\nDrag to reposition" : "Down to the Basement (terminal) · ⌘⇧T\nDrag to reposition")
            .accessibilityElement().accessibilityLabel(s.up ? "Go up to Greenlight Center" : "Go down to the Basement").accessibilityAddTraits(.isButton)
            .accessibilityAction { s.tap() }  // VoiceOver's press: the same ride as a click
    }
}

/// A window as a picture in the ride's overlay (top-left coordinates): its title band, then its content.
struct Shot { var img: NSImage?; var band: NSColor, rect: CGRect, top: CGFloat }
struct Squash { var sx = 1.0, sy = 1.0, r = 12.0, a = 1.0 }
final class RideState: ObservableObject {
    @Published var from = Squash()
    @Published var to = Squash(sx: 0.02, sy: 0.02, r: 300, a: 0)
    @Published var veil = 0.0
    @Published var tone = 0.0
    @Published var flip = 0.0
    @Published var faceUp = true
    @Published var toShot: Shot?
}
/// The ride: the window you leave shrinks into the orb, the orb flips, the other grows out of the same spot.
struct RideView: View {
    let up: Bool, from: Shot, orb: CGPoint, matte: Bool; var face = true
    @ObservedObject var s: RideState
    func win(_ w: Shot, _ q: Squash) -> some View {
        VStack(spacing: 0) {
            Color(nsColor: w.band).frame(height: w.top)
            if let i = w.img { Image(nsImage: i).resizable() } else { Color(nsColor: w.band) }
        }
        .background(Color(nsColor: w.band)).frame(width: w.rect.width, height: w.rect.height).clipShape(RoundedRectangle(cornerRadius: q.r))
        .scaleEffect(x: q.sx, y: q.sy, anchor: UnitPoint(x: (orb.x - w.rect.minX) / w.rect.width, y: (orb.y - w.rect.minY) / w.rect.height))
        .opacity(q.a).offset(x: w.rect.minX, y: w.rect.minY)
    }
    var body: some View {
        ZStack(alignment: .topLeading) {
            ZStack { hc("0A0A0C"); hc("E8E4DA").opacity(s.tone) }.opacity(s.veil)
            win(from, s.from)
            if let t = s.toShot { win(t, s.to) }
            if face { LevelFace(up: s.faceUp, matte: matte).frame(width: 50, height: 50).rotation3DEffect(.degrees(s.flip), axis: (x: 0, y: 1, z: 0)).position(orb) }  // Floating orbs off: the windows still squash and grow, with no orb between them
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// Borderless floating bits (the orb): click runs `onClick`; `draggable` ones move with the mouse and report `onMoved`.
final class FloatHost<V: View>: NSHostingView<V> {
    var onClick: () -> Void = {}, onMoved: () -> Void = {}, onPress: (Bool) -> Void = { _ in }
    var draggable = false
    var inside: () -> NSRect? = { nil }  // drags stay within this frame (the dashboard window)
    private var down = NSPoint.zero, origin = NSPoint.zero, moved = false
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func mouseDown(with e: NSEvent) { down = NSEvent.mouseLocation; origin = window?.frame.origin ?? .zero; moved = false; onPress(true) }
    override func mouseDragged(with e: NSEvent) {
        guard draggable else { return }
        let p = NSEvent.mouseLocation
        if !moved && hypot(p.x - down.x, p.y - down.y) < 3 { return }  // a click that wobbled
        moved = true
        var o = NSPoint(x: origin.x + p.x - down.x, y: origin.y + p.y - down.y)
        if let r = inside(), let s = window?.frame.size {
            o.x = min(max(o.x, r.minX), r.maxX - s.width); o.y = min(max(o.y, r.minY), r.maxY - s.height)
        }
        window?.setFrameOrigin(o)
    }
    override func mouseUp(with e: NSEvent) { onPress(false); moved ? onMoved() : onClick() }
}

/// A login shell on a pseudo-terminal, for the T orb's terminal.
final class Pty {
    private(set) var pid: pid_t = 0
    private var fd: Int32 = -1, src: DispatchSourceRead?
    var onOutput: (Data) -> Void = { _ in }
    var onExit: (Int32) -> Void = { _ in }  // the exit status: 255 is ssh's "couldn't connect / connection lost"

    /// `cmd`: run by a login shell (so PATH has Homebrew's tmux, ssh config…); nil: just the login shell.
    func start(cols: Int, rows: Int, cmd: String? = nil) {
        guard pid == 0 else { return }
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh", home = NSHomeDirectory()
        var env = ProcessInfo.processInfo.environment
        env["TERM"] = "xterm-256color"; env["COLORTERM"] = "truecolor"; env["TERM_PROGRAM"] = "Greenlight"
        // Everything the child needs is built before the fork: after it, the child only chdirs and execs.
        let argv = [strdup("-" + (shell as NSString).lastPathComponent)] + (cmd.map { [strdup("-c"), strdup($0)] } ?? []) + [nil]
        let envp = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { argv.forEach { free($0) }; envp.forEach { free($0) } }
        var ws = winsize(ws_row: UInt16(max(rows, 2)), ws_col: UInt16(max(cols, 10)), ws_xpixel: 0, ws_ypixel: 0)
        var master: Int32 = -1
        let p = forkpty(&master, nil, nil, &ws)
        if p == 0 { _ = home.withCString { chdir($0) }; execve(shell, argv, envp); _exit(127) }
        guard p > 0 else { return }
        pid = p; fd = master
        let s = DispatchSource.makeReadSource(fileDescriptor: master, queue: .main)
        s.setEventHandler { [weak self] in
            guard let self else { return }
            var buf = [UInt8](repeating: 0, count: 65536)
            let n = read(self.fd, &buf, buf.count)
            if n > 0 { self.onOutput(Data(buf[0..<n])); return }
            // EIO: the shell is gone; reap it for its status (pid cleared first, so nothing signals a reused pid)
            var st: Int32 = 0; let code = waitpid(self.pid, &st, 0) == self.pid ? (st >> 8) & 0xff : -1
            self.pid = 0; self.src?.cancel(); self.src = nil; self.fd = -1; self.onExit(code)
        }
        s.setCancelHandler { close(master) }
        s.resume(); src = s
    }
    func write(_ text: String) {
        guard fd >= 0 else { return }
        var d = Array(text.utf8)
        while !d.isEmpty { let n = Darwin.write(fd, d, d.count); if n <= 0 { break }; d.removeFirst(n) }
    }
    func resize(cols: Int, rows: Int) {
        guard fd >= 0 else { return }
        var ws = winsize(ws_row: UInt16(max(rows, 2)), ws_col: UInt16(max(cols, 10)), ws_xpixel: 0, ws_ypixel: 0)
        _ = ioctl(fd, TIOCSWINSZ, &ws)
    }
    func stop() {
        guard pid > 0 else { return }
        kill(pid, SIGHUP); var st: Int32 = 0; waitpid(pid, &st, WNOHANG)
        src?.cancel(); src = nil; fd = -1; pid = 0
    }
}

func floatPanel(_ size: NSSize) -> NSPanel {
    let w = NSPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    w.isOpaque = false
    w.backgroundColor = .clear
    w.hasShadow = true
    w.isReleasedWhenClosed = false
    w.hidesOnDeactivate = false
    return w
}


final class App: NSObject, NSApplicationDelegate, NSWindowDelegate, NSSplitViewDelegate, UNUserNotificationCenterDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    var item: NSStatusItem!
    let greenlight = Core()
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        for n in [NSWindow.didBecomeKeyNotification, NSWindow.didResignKeyNotification] {  // resign: the tab you were on as you went to the chat
            NotificationCenter.default.addObserver(forName: n, object: nil, queue: .main) { [weak self] in ($0.object as? NSWindow).map { self?.noteFocus($0) } }
        }
        greenlight.scope = { [weak self] in self?.pickScope }
        greenlight.showDockIcon()
        if greenlight.engine == "openai" { DispatchQueue.global().async { _ = openAIKey() } }  // the login shell it may run, before dictation waits on it (voice mode: Speech.warm)
        Speech.shared.warm()
        signal(SIGPIPE, SIG_IGN)  // an ssh can die between our write and its read
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.imagePosition = .imageLeading
        item.button?.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        item.button?.target = self
        item.button?.action = #selector(click)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        greenlight.popoverShown = { [weak self] in NSApp.isActive && self?.chatShown == true }
        greenlight.popChat = { [weak self] in self?.popChat(out: $0) }
        greenlight.openPage = { [weak self] name in  // as the page's own openPage message
            guard let self, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil else { return }
            self.greenlight.inboxOpen = false
            self.web(showing: "hex").evaluateJavaScript("mc.openPage(\"\(name)\")")
            if let w = self.viewWindows["hex"] { w.makeKeyAndOrderFront(nil) } else { self.show() }
        }
        greenlight.reloadFluid = { [weak self] in self?.loadFluid() }
        greenlight.unattach = { [weak self] key in
            let k = (try? JSONSerialization.data(withJSONObject: [key])).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
            self?.mainWebs.forEach { $0.evaluateJavaScript("window.mc && mc.undock(\(k)[0])") }
        }
        orbMonitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] e in  // a click brings its window up: put the orbs back on top
            if let self, e.window != nil, e.window !== self.termOrb { self.raiseOrbs() }
            return e
        }
        vimMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard let self, self.capture == nil, let tv = e.window?.firstResponder as? NSTextView,
                  let c = tv.isDescendant(of: self.chat) ? self.greenlight : self.l1s.values.first(where: { tv.isDescendant(of: $0.chat) })?.c else { return e }
            if e.modifierFlags.intersection([.command, .control, .option, .shift]) == .command, { let ch = e.charactersIgnoringModifiers?.lowercased(); return ch == "v" || (Int(e.keyCode) == kVK_ANSI_V && ch?.first?.isASCII != true) }(),  // the layout's V; a non-Latin layout's on the V key
               !c.editingName && !c.inboxOpen && !c.settingsOpen && c.replyingTo == nil, c.pasteFiles() { return nil }  // in the composer only  // ⌘V of files or an image attaches them
            if c.historyKey(e, tv) {  // ↑↓ / ⌃R history and readline keys, ahead of vim (whose ⌃R would be redo)
                guard c.toEnd else { return nil }
                c.toEnd = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak w = e.window] in  // caret to the end once the text lands
                    if let tv = w?.firstResponder as? NSTextView { tv.setSelectedRange(NSRange(location: (tv.string as NSString).length, length: 0)) }
                }
                return nil
            }
            guard c.vim else { return e }
            let m = e.modifierFlags.intersection([.command, .control, .option])
            if m.contains(.command) || m.contains(.option) { return e }  // ⌘A, ⌘V… still work
            let esc = e.keyCode == kVK_Escape || (m == .control && e.charactersIgnoringModifiers == "[")
            let passthrough = [kVK_Return, kVK_ANSI_KeypadEnter, kVK_Delete, kVK_ForwardDelete, kVK_Tab,
                               kVK_LeftArrow, kVK_RightArrow, kVK_UpArrow, kVK_DownArrow, kVK_Home, kVK_End]
            if !esc && passthrough.contains(Int(e.keyCode)) { return e }  // Enter still sends
            let k = esc ? "\u{1b}" : m == .control ? "^" + (e.charactersIgnoringModifiers ?? "") : e.characters ?? ""
            return c.vimKeys.key(k, tv) ? nil : e
        }
        floorMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in  // ⌘↑ up a floor (B2 → B1 → L1 → B2), ⌘↓ down, round the loop
            if let self, self.capture == nil, e.keyCode == kVK_Escape, self.greenlight.voice.on || self.greenlight.voice.starting,  // Esc leaves live chat, or stops it starting (sheets keep theirs)
               !self.greenlight.settingsOpen, !self.greenlight.inboxOpen { self.greenlight.voice.stopAll(); return nil }
            guard let self, self.capture == nil, e.modifierFlags.intersection([.command, .control, .option, .shift]) == .command,
                  e.keyCode == kVK_UpArrow || e.keyCode == kVK_DownArrow,
                  let w = e.window, let f = self.floorGo[ObjectIdentifier(w)] else { return e }
            let order = Floors.levels.map(\.0), n = order.count, i = order.firstIndex(of: f.0.at) ?? 0
            f.1(order[(i + (e.keyCode == kVK_UpArrow ? n - 1 : 1)) % n])
            return nil
        }
        hintSink = greenlight.$hint.receive(on: RunLoop.main).sink { [weak self] in self?.showTab($0) }
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        tick()
        if ud.bool(forKey: "autorun") { _ = termWindow }  // auto-run lives in the Basement's page: load it, hidden

        let un = UNUserNotificationCenter.current()
        un.delegate = self
        un.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        let reply = UNTextInputNotificationAction(identifier: "reply", title: "Reply", options: [], textInputButtonTitle: "Send", textInputPlaceholder: "Answer greenlight…")
        un.setNotificationCategories([UNNotificationCategory(identifier: "greenlight", actions: [reply], intentIdentifiers: [])])
        greenlight.tailOutbox()
        streamDash()
        rescan()
        hotKey()
        mainMenu()
        detached.forEach(detach)  // views the user gave their own windows last time
        reopenTermWindows()
        reopenL1s()
        greenlight.termOpens == "greenlight" ? show() : basement()  // Settings → Terminal → App opens to
        un.getNotificationSettings { s in NSLog("greenlight notifications: authorization %d, alert %d", s.authorizationStatus.rawValue, s.alertSetting.rawValue) }
    }

    lazy var chat: NSHostingView<ChatView> = { let h = NSHostingView(rootView: ChatView(c: greenlight)); h.sizingOptions = []; return h }()  // the pane sets the width, SwiftUI fits it
    lazy var settingsHost: NSHostingView<SettingsWindowView> = { let h = NSHostingView(rootView: SettingsWindowView(c: greenlight)); h.sizingOptions = []; return h }()
    /// Settings: its own window, over whichever floor you're on; closing it leaves you there
    lazy var settingsWindow: NSWindow = {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 720), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.title = "Settings"; w.titleVisibility = .hidden; w.titlebarAppearsTransparent = true
        w.isReleasedWhenClosed = false; w.minSize = NSSize(width: 560, height: 420)
        w.contentView = settingsHost; w.center(); w.setFrameAutosaveName("settings")
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: w, queue: .main) { [weak self] _ in self?.greenlight.settingsOpen = false }
        return w
    }()
    lazy var inboxHost: NSHostingView<InboxOverlay> = { let h = NSHostingView(rootView: InboxOverlay(c: greenlight)); h.sizingOptions = []; return h }()
    var settingsSub: AnyCancellable?, inboxSub: AnyCancellable?
    lazy var fluid: WKWebView = makeWeb()
    func makeWeb() -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "mc")  // dashboard.html → app: ready, page
        let v = WKWebView(frame: .zero, configuration: cfg)
        v.navigationDelegate = self
        // Black, not white, while the page repaints during a divider drag or resize.
        v.setValue(false, forKey: "drawsBackground")
        v.underPageBackgroundColor = .black
        return v
    }

    // MARK: views in their own windows (agents), so each can live on any display; the hive stays in the main window

    static let viewTitles = ["agents": "Agents"]
    var detached = ud.stringArray(forKey: "detached")?.filter { viewTitles[$0] != nil } ?? [] { didSet { ud.set(detached, forKey: "detached") } }
    var viewWindows = [String: NSWindow]()
    var mainWebs: [WKWebView] { [fluid] + viewWindows.values.compactMap { $0.contentView as? WKWebView } }  // the main window's pick, chat and lit hexes
    var webs: [WKWebView] { mainWebs + l1s.values.map(\.web) }
    func broadcast(_ js: String) { webs.forEach { $0.evaluateJavaScript(js) } }
    /// The webview that shows a view: its own window if it has one, else the main window.
    func web(showing v: String) -> WKWebView { viewWindows[v]?.contentView as? WKWebView ?? fluid }

    func detach(_ v: String) {
        guard let title = App.viewTitles[v] else { return }
        if let w = viewWindows[v] { w.makeKeyAndOrderFront(nil); return }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: v == "agents" ? 900 : 1000, height: v == "agents" ? 420 : 640),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        w.title = title
        w.backgroundColor = .black
        w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: 320, height: 160)
        w.delegate = self
        w.appearance = window.appearance
        let web = makeWeb()
        w.contentView = web
        if let u = Bundle.main.url(forResource: "dashboard", withExtension: "html") { web.loadFileURL(u, allowingReadAccessTo: u.deletingLastPathComponent()) }
        if !w.setFrameUsingName("view-" + v) { w.center() }
        w.setFrameAutosaveName("view-" + v)
        viewWindows[v] = w
        if !detached.contains(v) { detached.append(v) }
        w.makeKeyAndOrderFront(nil)
        pushMode()
    }

    /// Closing a view's window hands the view back to the main window.
    func attach(_ v: String) {
        guard let w = viewWindows.removeValue(forKey: v) else { return }
        (w.contentView as? WKWebView)?.configuration.userContentController.removeScriptMessageHandler(forName: "mc")  // it retains self
        detached.removeAll { $0 == v }
        w.orderOut(nil)
        pushMode()
    }

    func modeJSON(for web: WKWebView) -> String {
        if let v = viewWindows.first(where: { $0.value.contentView === web })?.key { return #"{"only":"\#(v)"}"# }
        return #"{"away":[\#(viewWindows.keys.map { "\"\($0)\"" }.joined(separator: ","))]}"#
    }
    var picked: [String] = []  // one Chief, one set of lit hexes: every window shows the same, a window that (re)loads gets them too
    func pick(_ names: [String]) {
        picked = names.reduce(into: []) { if !$0.contains($1) { $0.append($1) } }
        webs.forEach(pushPicks)
    }
    /// One Chief for every window: it's told which one you were last in (a terminal's tab, the dashboard's open workspace), so
    /// "this one" or "merge it" means that workspace. The chat window itself doesn't count.
    func noteFocus(_ w: NSWindow) {
        guard w !== chatWindow, let web = w.contentView as? WKWebView ?? webs.first(where: { $0.window === w }) else { return }
        let id = termWins.first { $0.value === w }?.key
        // a second Greenlight Center window tells only its own Chief; a terminal (every project) tells them all
        let cores = l1s.values.first { $0.w === w }.map { [$0.c] } ?? (id != nil ? [greenlight] + l1s.values.map(\.c) : [greenlight])
        web.evaluateJavaScript(id != nil ? "JSON.stringify(window.term && term.focusInfo && term.focusInfo())" : "JSON.stringify(window.mc && mc.focus && mc.focus())") { [weak self] r, _ in
            guard let self else { return }
            let o = ((r as? String).flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) } as? [String: Any]) ?? [:]
            let line = focusLine(terminal: id, project: id.flatMap { self.floors[$0]?.project } ?? "", o)
            guard cores.contains(where: { $0.focus != line }) else { return }
            cores.forEach { $0.focus = line }
            guard cores[0] === self.greenlight else { return }
            let key = id != nil ? ("path", o["path"] as? String) : ("name", o["page"] as? String)  // voice mode: the stage follows you
            if let k = key.1, let p = self.greenlight.pucks.first(where: { $0[key.0] as? String == k }) { self.greenlight.voice.show(workspaceSpec(p)) }
        }
    }
    func pushPicks(_ web: WKWebView) {
        guard let d = try? JSONSerialization.data(withJSONObject: l1(web)?.picked ?? picked), let j = String(data: d, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.mc && mc.pick && mc.pick(\(j))")
    }
    func pushMode() { webs.forEach { $0.evaluateJavaScript("window.mc && mc.mode(\(modeJSON(for: $0)))") } }
    lazy var window: NSWindow = {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = greenlight.name
        w.backgroundColor = .black
        w.titleVisibility = .hidden  // the floors say L1 · GREENLIGHT; the title stays for the Window menu and Mission Control
        w.isReleasedWhenClosed = false  // the close button hides it; the menubar star, Dock or ⌃⌥M bring it back
        w.minSize = NSSize(width: 480, height: 520)
        w.animationBehavior = greenlight.rideTime > 0 ? .default : .none
        l1Floor.at = "greenlight"; syncL1()
        let go: (String) -> Void = { [unowned self] f in  // down from Greenlight: the Basement or the Pipe
            wantFloor = f; (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.opens('\(f)')"); ride(up: false) }
        floorGo[ObjectIdentifier(w)] = (l1Floor, go)
        floorBar(w, Floors(s: l1Floor, c: greenlight, pick: { [unowned self] in l1Pick() }, go: go))
        let root = NSView()  // the dashboard and its overlays, left of the chat
        for v in [fluid, inboxHost] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
            NSLayoutConstraint.activate([v.leadingAnchor.constraint(equalTo: root.leadingAnchor), v.trailingAnchor.constraint(equalTo: root.trailingAnchor),
                                         v.topAnchor.constraint(equalTo: root.topAnchor), v.bottomAnchor.constraint(equalTo: root.bottomAnchor)])
        }
        inboxHost.isHidden = true
        inboxSub = greenlight.$inboxOpen.receive(on: RunLoop.main).sink { [weak self] open in
            guard let self, self.inboxHost.isHidden == open else { return }
            self.inboxHost.isHidden = !open
            if open { self.window.makeFirstResponder(self.inboxHost) }
        }
        settingsSub = greenlight.$settingsOpen.receive(on: RunLoop.main).sink { [weak self] open in
            guard let self else { return }
            if open { self.greenlight.hint = ""; self.settingsWindow.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); self.settingsWindow.makeFirstResponder(nil) }  // AppKit focuses the search field otherwise  // the gear's hover-exit never comes
            else if self.settingsWindow.isVisible { self.settingsWindow.orderOut(nil) }
        }
        split.isVertical = true
        split.dividerStyle = .thin
        split.delegate = self
        split.addArrangedSubview(root)
        split.addArrangedSubview(chatSlot)
        split.setHoldingPriority(.defaultLow, forSubviewAt: 0)  // a window resize goes to the dashboard
        split.setHoldingPriority(.defaultLow + 1, forSubviewAt: 1)
        for (v, w, pr) in [(root, 480, NSLayoutConstraint.Priority.defaultHigh + 1), (chatSlot, 320, .defaultHigh)] as [(NSView, CGFloat, NSLayoutConstraint.Priority)] {
            let k = v.widthAnchor.constraint(greaterThanOrEqualToConstant: w); k.priority = pr; k.isActive = true  // a window too narrow for both: the chat gives
        }
        w.contentView = split
        split.layoutSubtreeIfNeeded()
        split.setPosition(split.bounds.width - 440, ofDividerAt: 0)
        split.autosaveName = "chatSplit"  // restores a dragged divider over the default
        loadFluid()
        if !w.setFrameUsingName("main") { w.center() }
        w.setFrameAutosaveName("main")
        return w
    }()

    func loadFluid(_ into: WKWebView? = nil) {
        let fluid = into ?? fluid, s = greenlight.fluidURL.trimmingCharacters(in: .whitespaces)
        if s.isEmpty, let u = Bundle.main.url(forResource: "dashboard", withExtension: "html") {
            fluid.loadFileURL(u, allowingReadAccessTo: u.deletingLastPathComponent())
        } else if s.hasPrefix("http://") || s.hasPrefix("https://"), let u = URL(string: s) {
            fluid.load(URLRequest(url: u))
        } else {
            let u = URL(fileURLWithPath: (s as NSString).expandingTildeInPath)
            fluid.loadFileURL(u, allowingReadAccessTo: u.deletingLastPathComponent())
        }
    }

    // MARK: dashboard data (dash.py where greenlight runs)

    var rescanning = false
    var dashSnap = ""  // newest snapshot line, replayed when the page (re)loads

    /// A fresh greenlight scan, at launch and from the footer's ↻; the stream picks up the new last.json.
    func rescan() {
        guard !rescanning else { return }
        rescanning = true
        greenlight.log(.greenlight, "Greenlight", .action, "rescanning every workspace")
        ssh("\(py)/greenlight.py scan >/dev/null", onExit: { [weak self] code in
            self?.greenlight.acted("Greenlight", "rescan", code, "")
            DispatchQueue.main.async { self?.rescanning = false; self?.broadcast("window.mc && mc.rescanned(\(code))") }
        })
    }

    func streamDash() {
        ssh("\(py)/dash.py stream 5", onLine: { [weak self] l in
            guard let self, l.hasPrefix("{") else { return }
            self.dashSnap = l
            self.broadcast("window.mc && mc.update(\(l))")
            self.queueBuilds(l)
        }, onExit: { [weak self] _ in DispatchQueue.main.asyncAfter(deadline: .now() + 10) { self?.streamDash() } })
    }

    // Background page builds: pucks whose layout is stale, greenlight.designParallel at a time.
    var building = Set<String>()
    var tried = [String: String]()  // name -> sig last attempted; retried only once the state moves on

    func queueBuilds(_ l: String) {
        guard let d = l.data(using: .utf8), let snap = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let pucks = snap["pucks"] as? [[String: Any]] else { return }
        greenlight.pucks = pucks
        if case let a = agentsOf(pucks), a != greenlight.agents { greenlight.agents = a }
        greenlight.watched(pucks)
        greenlight.fed(pucks)
        for l in l1s.values { l.c.pucks = pucks; l.c.agents = greenlight.agents; l.c.fed(pucks) }  // the watch is the main chat's
        for p in pucks where building.count < greenlight.designParallel {
            guard let name = p["name"] as? String, let sig = p["sig"] as? String, p["built"] as? Bool == false,
                  p["state"] as? String != "done", !building.contains(name), tried[name] != sig,
                  name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil else { continue }
            building.insert(name); tried[name] = sig
            ssh("\(py)/dash.py page '\(name)' --model \(shellQuote(greenlight.designModel)) --effort \(shellQuote(greenlight.designEffort)) >/dev/null", onExit: { [weak self] _ in
                self?.building.remove(name); self?.pushBuilds()
            })
        }
        pushBuilds()
        queueSuggests(pucks)
    }

    // Background next steps: the hover card's button, written before the card opens. Only for pucks whose card shows
    // the button (agent not working or waiting) and whose snapshot has no current suggestion.
    var suggesting = Set<String>()  // in flight, background or asked for by a page; a second ask waits on the first
    var suggestTried = [String: String]()  // name -> sig last attempted; retried only once the state moves on
    let suggestParallel = 3

    func queueSuggests(_ pucks: [[String: Any]]) {
        for p in pucks where suggesting.count < suggestParallel {
            guard let name = p["name"] as? String, let sig = p["sig"] as? String, p["suggestion"] is NSNull,
                  p["state"] as? String != "done", !suggesting.contains(name), suggestTried[name] != sig,
                  !["working", "waiting"].contains((p["agent"] as? [String: Any])?["state"] as? String ?? ""),
                  name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil else { continue }
            suggestTried[name] = sig
            suggest(name)
        }
    }

    /// dash.py suggest for one workspace; every page gets the answer (mc.suggest), so an open hover card relabels at once.
    func suggest(_ name: String) {
        guard !suggesting.contains(name) else { return }
        suggesting.insert(name)
        var out = ""
        ssh("\(py)/dash.py suggest '\(name)' --model \(shellQuote(greenlight.nextModel)) --effort \(shellQuote(greenlight.nextEffort))", onLine: { out += $0 }, onExit: { [weak self] code in
            self?.suggesting.remove(name)
            let js = out.hasPrefix("{") ? out : #"{"name":"\#(name)","error":"\#(host) didn't answer (exit \#(code))"}"#
            self?.broadcast("window.mc && mc.suggest(\(js))")
            self?.termWebs.forEach { $0.evaluateJavaScript("window.term && term.suggest(\(js))") }  // the sidebar card's button
        })
    }


    var pushedDark = false, pushedTerm = ""  // the first tick pushes both
    var pushedRepos = "", pushedMono = "", pushedBell = "", pushedChat = "", pushedOrb = "", pushedMotion = 0.6, pushedSlide = 0.25, pushedLocal = localDefault
    func pushBuilds() {
        let names = (try? JSONSerialization.data(withJSONObject: building.sorted())).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
        broadcast("window.mc && mc.builds({building: \(names), parallel: \(greenlight.designParallel)})")
    }

    func userContentController(_ uc: WKUserContentController, didReceive m: WKScriptMessage) {
        if m.name == "term" { if let b = m.body as? [String: Any] { termMessage(b, from: m.webView) }; return }
        guard let b = m.body as? [String: Any], let cmd = b["cmd"] as? String, let from = m.webView else { return }
        let l = l1(from)  // a second Greenlight Center window: its own projects and chat
        if cmd == "ready", let l { l.pushedRepos = ""; l.pushedChat = ""; l.c.attached = [] }
        if cmd == "ready" {
            from.evaluateJavaScript("mc.mode(\(modeJSON(for: from)))")
            if !dashSnap.isEmpty { from.evaluateJavaScript("mc.update(\(dashSnap))") }
            pushBuilds(); pushPicks(from)
            if from === web(showing: "hex") { greenlight.attached = [] }  // a reloaded hive starts with an empty dock
            pushedMono = ""; pushedBell = ""; pushedChat = ""; pushedOrb = ""; pushedRepos = ""  // tick sends the code font, the bell and the orb's room
        }
        if cmd == "error" { NSLog("greenlight page error: %@", String(describing: b["m"] ?? "")) }
        if ["run", "merge", "undraft", "signoff", "ask", "answer", "answerPing", "park", "task", "basement", "editPrompt"].contains(cmd) { greenlight.active() }
        if cmd == "detach", let v = b["view"] as? String { detach(v) }
        if cmd == "unpick" { if let l { l.c.light([]) } else { pick([]) } }  // a click on a lit hex clears them in every window (on its chat's)
        if cmd == "project" {  // L1's Open projects (or Clear filters: null)
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil || only!.count > 100 { return }
            if let l { l.only = only; saveL1s(); syncL1() } else { setPick(only) }  // the main one: one pick for every floor and terminal window
        }
        if cmd == "window" {  // L1's Open projects, New window: another Greenlight Center, with its own Chief
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil || only!.count > 100 { return }
            newL1(only, from: from.window)
        }
        if cmd == "pickNew" { l1Pick(here: false, in: l) }  // ⌘⇧N on L1
        if cmd == "editProjects" {  // L1's Edit projects…: down to the Basement, where the editor lives
            wantFloor = "basement"; (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.opens('basement'); window.editProjects && editProjects()"); ride(up: false)
        }
        // A click in one window that belongs to a view in another: forward it there and bring that window up.
        if cmd == "openPage", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            fluid.evaluateJavaScript("mc.openPage(\"\(name)\")"); show()  // the hive lives in the main window
        }
        // A workspace's Basement button: down to B1, on that worktree.
        if cmd == "basement", let path = b["path"] as? String, path.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil, !path.contains("..") { toWorkspace(path) }
        if cmd == "showChat", l == nil { setChat(hidden: false) }  // a second window's chat is always in its pane
        if cmd == "settings" { appSettings() }  // the top bar's gear: its own window, to the front
        if cmd == "inbox" { greenlight.settingsOpen = false; if l != nil { show(); greenlight.inboxOpen = true } else { greenlight.inboxOpen.toggle() } }  // one inbox, the main window's
        // An answer from a lifted workspace's page: the same as answering that question in the bell.
        if cmd == "answerPing", let id = b["id"] as? String, let text = b["text"] as? String,
           !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if let q = greenlight.waiting.first(where: { $0.id.uuidString == id }) { b["dismiss"] as? Bool == true ? greenlight.answer(q, "(dismissed)", send: false) : greenlight.answer(q, text) }
            pushedBell = ""; tick()  // re-push even when it was already answered elsewhere, so the page drops its "Sending…"
        }
        if cmd == "toggleChat", l == nil { appToggleChat() }
        if cmd == "context", let items = b["items"] as? [[String: Any]] {
            let c = l?.c ?? greenlight
            let was = Dictionary(c.attached.map { ($0.id, $0.sent) }, uniquingKeysWith: { a, _ in a })
            c.attached = items.compactMap { i in
                guard let k = i["key"] as? String, let l = i["label"] as? String, let t = i["text"] as? String else { return nil }
                return Attached(id: k, label: l, color: hexColor(i["color"] as? String ?? ""), mrs: i["mrs"] as? Int ?? 1, text: String(t.prefix(20000)), sent: was[k] ?? false)
            }
        }
        if cmd == "rescan" { rescan() }
        // Names go into a shell command: allow only what workspace names use (dash.py checks again).
        if cmd == "page", let w = from.window { noteFocus(w) }  // a workspace opened while you're in that window
        if cmd == "page", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            let fresh = b["fresh"] as? Bool == true ? " --fresh" : ""
            var out = ""
            ssh("\(py)/dash.py page '\(name)'\(fresh) --model \(shellQuote(greenlight.designModel)) --effort \(shellQuote(greenlight.designEffort))", onLine: { out += $0 }, onExit: { code in
                let js = out.hasPrefix("{") ? out : #"{"name":"\#(name)","error":"couldn't load this page from \#(host) (exit \#(code))"}"#
                from.evaluateJavaScript("mc.page(\(js))")
            })
        }
        // The agent card's All output: a page of the workspace's agent conversation (newest, or the one before a cursor), polled while it's open.
        if cmd == "transcript", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            let before = b["before"] as? String ?? ""
            if !before.isEmpty && before.range(of: #"^[\w.-]{1,200}:\d{1,9}$"#, options: .regularExpression) == nil { return }
            var out = ""
            let bj = before.isEmpty ? "null" : "\"\(before)\""  // echoed: the page tells a failed Load earlier from a failed refresh
            ssh("\(py)/dash.py transcript '\(name)'\(before.isEmpty ? "" : " --before '\(before)'")", onLine: { out += $0 }, onExit: { code in
                let js = out.hasPrefix("{") ? out : #"{"name":"\#(name)","before":\#(bj),"error":"\#(host) didn't answer (exit \#(code))"}"#
                from.evaluateJavaScript("mc.transcript(\(js))")
            })
        }
        // A waiting agent's question (ask) and your reply to it (answer), from the Answer button or the page.
        if cmd == "ask" || cmd == "answer", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            let text = b["text"] as? String ?? ""
            if cmd == "answer" && text.isEmpty { return }
            let remote = cmd == "answer" ? "\(py)/dash.py answer '\(name)'"
                : "\(py)/dash.py ask '\(name)' --model \(shellQuote(greenlight.designModel)) --effort \(shellQuote(greenlight.designEffort))"
            var out = ""
            if cmd == "answer" { greenlight.log(.greenlight, name, .action, "answering its agent: \(text)") }
            ssh(remote, stdin: text, onLine: { out += $0 }, onExit: { code in
                if cmd == "answer" { self.greenlight.acted(name, "answer", code, out) }
                let js = out.hasPrefix("{") ? out : #"{"name":"\#(name)","error":"\#(host) didn't answer (exit \#(code))"}"#
                from.evaluateJavaScript("mc.\(cmd == "ask" ? "asked" : "answered")(\(js))")
            })
        }
        // The hover card's Next step: greenlight's next-step prompt for a workspace, and running a prompt there as a new agent.
        if cmd == "park", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil, let on = b["on"] as? Bool {
            var out = ""
            greenlight.log(.greenlight, name, .action, on ? "parking" : "unparking")
            ssh("\(py)/dash.py park '\(name)' \(on ? "on" : "off")", onLine: { out += $0 }, onExit: { code in
                self.greenlight.acted(name, on ? "park" : "unpark", code, out)
                let js = out.hasPrefix("{") ? out : #"{"name":"\#(name)","error":"\#(host) didn't answer (exit \#(code))"}"#
                from.evaluateJavaScript("mc.parked(\(js))")
            })
        }
        // New task: dash.py task repos | new <repo>; the request and its files go on stdin as JSON.
        if cmd == "task", let op = b["op"] as? String, ["repos", "new"].contains(op),
           let arg = b["arg"] as? String, arg.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            var out = ""
            ssh("\(py)/dash.py task \(op) '\(arg)'", stdin: b["text"] as? String ?? "", onLine: { out += $0 }, onExit: { code in
                let js = out.hasPrefix("{") ? out : #"{"op":"\#(op)","arg":"\#(arg)","error":"\#(host) didn't answer (exit \#(code))"}"#
                from.evaluateJavaScript("mc.tasked(\(js))")
            })
        }
        if cmd == "suggest", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            suggest(name)  // already in flight (the background queue): its answer reaches this page too
        }
        // The hex card's Merge (second click): dash.py tree merge, answered with mc.merged
        if cmd == "merge" || cmd == "undraft" || cmd == "signoff", let path = b["path"] as? String, path.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil, !path.contains("..") {
            var out = ""
            let op = cmd == "undraft" ? "ready" : cmd, who = greenlight.pucks.first { $0["path"] as? String == path }?["name"] as? String ?? path
            greenlight.log(.greenlight, who, .action, ["merge": "merging", "ready": "marking ready", "signoff": "signing off"][op] ?? op)
            ssh("\(py)/dash.py tree \(op) '\(path)'", onLine: { out += $0 }, onExit: { code in
                self.greenlight.acted(who, op, code, out)
                let js = out.hasPrefix("{") ? out : #"{"op":"\#(op)","path":"\#(path)","error":"\#(host) didn't answer (exit \#(code))"}"#
                from.evaluateJavaScript("mc.merged(\(js))")
            })
        }
        if cmd == "editPrompt", let r = b["repo"] as? String, r.range(of: #"^[\w.-]{1,100}$"#, options: .regularExpression) != nil, !r.hasPrefix(".") {
            let k = b["kind"] as? String ?? "", kind = RulesStore.kinds.contains { $0.0 == k } ? "/" + k : ""
            greenlight.settingsTab = "Repositories"; greenlight.settingsRepo = r + kind; appSettings()
        }
        if cmd == "run", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            let prompt = b["prompt"] as? String ?? ""
            if prompt.isEmpty { return }
            var out = ""
            greenlight.log(.greenlight, name, .action, "starting an agent: \(prompt)")
            ssh("\(py)/dash.py run '\(name)'", stdin: prompt, onLine: { out += $0 }, onExit: { code in
                self.greenlight.acted(name, "start agent", code, out)
                let js = out.hasPrefix("{") ? out : #"{"name":"\#(name)","error":"\#(host) didn't answer (exit \#(code))"}"#
                from.evaluateJavaScript("mc.ran(\(js))")
            })
        }
    }

    // Links in the dashboard (MRs, pipelines) open in the browser, not in the pane.
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if a.navigationType == .linkActivated, let u = a.request.url, u.scheme != "file" {
            NSWorkspace.shared.open(u); decisionHandler(.cancel)
        } else { decisionHandler(.allow) }
    }

    /// A regular app now: app, Edit and Window menus. ⌘A/C/V/X/Z in text fields route through Edit.
    func mainMenu() {
        let bar = NSMenu()
        func menu(_ title: String, _ items: [(String, String, String)], target: AnyObject? = nil) -> NSMenu {
            let m = NSMenu(title: title)
            for (t, a, k) in items {
                if t == "-" { m.addItem(.separator()); continue }
                m.addItem(withTitle: t, action: Selector(a), keyEquivalent: k).target = a.hasPrefix("app") ? target : nil
            }
            bar.addItem(withTitle: title, action: nil, keyEquivalent: "").submenu = m
            return m
        }
        _ = menu("Greenlight", [("Settings…", "appSettings", ","), ("-", "", ""),
                                 ("Hide Greenlight", "hide:", "h"), ("Quit Greenlight", "terminate:", "q")], target: self)
        _ = menu("File", [("New Window", "appNewWindow", "n"), ("New Greenlight Center Window", "appNewL1", "")], target: self)
        _ = menu("Edit", [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("-", "", ""), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                          ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")])
        NSApp.windowsMenu = menu("Window", [("Minimize", "performMiniaturize:", "m"), ("Zoom", "performZoom:", ""), ("Close", "performClose:", "w"),
                                            ("-", "", ""), ("Show/Hide Chat", "appToggleChat", "C"), ("Live Chat", "appLive", "L"),
                                            ("Up to Greenlight Center", "appGoUp", "G"), ("Basement (Terminal)", "appToggleTerm", "T"), ("Jump to Terminal…", "appJumpTerm", "j"),
                                            ("Reload Dashboard", "appReload", "r")], target: self)
        NSApp.mainMenu = bar
    }
    @objc func appNewWindow() { newTermWindow(sharedOnly, from: NSApp.keyWindow.flatMap { termWins.values.contains($0) ? $0 : nil }) }  // the shared pick
    @objc func appNewL1() { newL1(l1s.values.first { $0.w.isKeyWindow }?.only ?? sharedOnly, from: NSApp.keyWindow) }  // the projects of the one you're in
    @objc func appSettings() { greenlight.settingsOpen = true; greenlight.inboxOpen = false }
    @objc func appReload() { loadFluid(l1s.values.first { $0.w.isKeyWindow }?.web) }
    @objc func appToggleTerm() { toggleTerm() }
    @objc func appJumpTerm() {  // ⌘J from any window: the terminal with its jump palette open (the page catches ⌘J itself when it has focus)
        if !(termWindow.isVisible && termWindow.isKeyWindow) { toggleTerm() }
        (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.palette(true)")
    }
    var lastToggle = Date.distantPast
    @objc func appLive() {  // ⇧⌘L: live chat on (showing the chat first) or off
        if !greenlight.voice.on && !greenlight.voice.starting { setChat(hidden: false); NSApp.activate(ignoringOtherApps: true) }
        greenlight.voice.toggle()
    }
    @objc func appToggleChat() {  // the menu and the page can both catch one ⌘⇧C: count it once
        if Date().timeIntervalSince(lastToggle) < 0.3 { return }
        lastToggle = Date()
        let hide = chatShown && NSApp.isActive
        setChat(hidden: hide)
        if !hide { NSApp.activate(ignoringOtherApps: true) }
    }

    // MARK: the chat: the main window's right pane, or its own window (⧉ in its header; closing that window docks it back)

    var chatHidden = ud.bool(forKey: "chatPaneHidden") { didSet { ud.set(chatHidden, forKey: "chatPaneHidden") } }  // new key: the pane starts open
    let split = Split()
    /// ponytail: NSSplitView leaves its old divider drawn over the web view after a move; draw none, the pane draws its own edge.
    class Split: NSSplitView { override var dividerColor: NSColor { .clear } }
    let chatEdge = NSBox()  // the chat pane's 1px left edge in the theme's line color; tick() follows theme changes
    lazy var chatSlot: NSView = {
        let v = NSView(), edge = chatEdge
        edge.boxType = .custom; edge.titlePosition = .noTitle; edge.borderWidth = 0; edge.fillColor = theme.blend(0.1); edge.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(edge, positioned: .above, relativeTo: nil)
        NSLayoutConstraint.activate([edge.leadingAnchor.constraint(equalTo: v.leadingAnchor), edge.widthAnchor.constraint(equalToConstant: 1),
                                     edge.topAnchor.constraint(equalTo: v.topAnchor), edge.bottomAnchor.constraint(equalTo: v.bottomAnchor)])
        v.isHidden = chatHidden || greenlight.chatOut
        return v
    }()
    var chatShown: Bool { greenlight.chatOut ? chatWindow.isVisible : window.isVisible && !chatSlot.isHidden }

    func setChat(hidden: Bool) {
        chatHidden = hidden
        if !hidden { greenlight.inboxOpen = false }  // asking for the chat closes the inbox
        placeChat()
    }
    func popChat(out: Bool) {
        greenlight.chatOut = out
        if !out { chatWindow.orderOut(nil) }
        chatHidden = false
        placeChat()
        if !out { window.makeKeyAndOrderFront(nil) }  // back on the right wall: show it there
    }

    /// Puts the chat view where it belongs: the pane, or its window; hidden if `chatHidden`.
    func placeChat() {
        let home = greenlight.chatOut ? chatWindow.contentView! : chatSlot
        if chat.superview !== home {
            chat.removeFromSuperview()
            chat.translatesAutoresizingMaskIntoConstraints = false
            home.addSubview(chat, positioned: .below, relativeTo: nil)  // under the pane's edge
            NSLayoutConstraint.activate([chat.leadingAnchor.constraint(equalTo: home.leadingAnchor), chat.trailingAnchor.constraint(equalTo: home.trailingAnchor),
                                         chat.topAnchor.constraint(equalTo: home.topAnchor), chat.bottomAnchor.constraint(equalTo: home.bottomAnchor)])
        }
        let pane = !greenlight.chatOut && !chatHidden
        if chatSlot.isHidden == pane {
            chatSlot.isHidden = !pane
            split.adjustSubviews(); split.layoutSubtreeIfNeeded()
            if termOrb.isVisible { placeOrb(termOrb) }
        }
        if pane && chatSlot.frame.width < 320 { split.setPosition(split.bounds.width - 440, ofDividerAt: 0) }  // a restored collapse comes back a sliver
        if greenlight.chatOut && !chatHidden { chatWindow.makeKeyAndOrderFront(nil) } else if greenlight.chatOut { chatWindow.orderOut(nil) }
        if !chatHidden { greenlight.missed = 0 }
        tick()  // the dashboard's chat toggle
    }

    lazy var chatWindow: NSWindow = {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 680), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = greenlight.name
        w.titlebarAppearsTransparent = true
        w.backgroundColor = theme.bg
        w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: 340, height: 360)
        w.contentView = NSView()
        w.delegate = self
        if !w.setFrameUsingName("chat") { w.center() }
        w.setFrameAutosaveName("chat")
        return w
    }()

    // the dashboard keeps 480pt, the chat pane 320pt
    func splitView(_ v: NSSplitView, constrainMinCoordinate p: CGFloat, ofSubviewAt i: Int) -> CGFloat { max(p, 480) }
    func splitView(_ v: NSSplitView, constrainMaxCoordinate p: CGFloat, ofSubviewAt i: Int) -> CGFloat { min(p, v.bounds.width - 320) }

    // MARK: L1's projects: the Open projects dialog B1/B2 have (in the dashboard page), over the projects a terminal page last listed; the dashboard shows their repos
    // ponytail: until a terminal page has loaded, the list is empty and L1 shows All projects

    let l1Floor = FloorState()
    var projList = [[String: Any]]()  // id ("" No Man's Land), name, color, repos, paths (its worktrees), busy
    var sharedOnly: [String]? { ud.array(forKey: "termOnly0") as? [String] }  // the one pick every floor and window shows; nil: all projects
    func setPick(_ only: [String]?, except: WKWebView? = nil) {
        ud.set(only, forKey: "termOnly0"); syncL1()
        let j = (only.flatMap { try? JSONSerialization.data(withJSONObject: $0) }).flatMap { String(data: $0, encoding: .utf8) } ?? "null"
        for w in termWebs where w !== except { w.evaluateJavaScript("window.term && term.only(\(j))") }
    }
    var l1Only: [String]? { projList.isEmpty ? nil : sharedOnly }  // nil until a terminal page lists the projects
    var l1Picked: [[String: Any]] { picked(l1Only) }
    func picked(_ only: [String]?) -> [[String: Any]] { (only ?? []).compactMap { id in projList.first { $0["id"] as? String == id } } }
    /// The picked projects' "paths" or "repos"; nil: every project. Sorted: the tick compares it.
    func sel(_ only: [String]?, _ k: String) -> [String]? { only == nil ? nil : Array(Set(picked(only).flatMap { $0[k] as? [String] ?? [] })).sorted() }
    var l1Paths: [String]? { sel(l1Only, "paths") }
    var l1Repos: [String]? { sel(l1Only, "repos") }  // the dashboard matches them against its own fresh trees: l1Paths is only as new as the terminal page's last trees
    /// Chief's scope: the projects picked, which every window shows (a terminal's term.only, the dashboard's mc.paths), while one of
    /// them is up. Read straight from the pick, not asked of a page: nothing to wait on, and no answer from a window you've left.
    var pickScope: String? {
        windowScope(paths: l1Paths, repos: l1Repos, shown: window.isVisible || termWins.values.contains(where: \.isVisible) || viewWindows.values.contains(where: \.isVisible))
    }
    func syncL1() {
        for (f, o) in [(l1Floor, l1Only)] + l1s.values.map({ ($0.floor, projList.isEmpty ? nil : $0.only) }) {
            let ps = picked(o), names = ps.compactMap { $0["name"] as? String }.joined(separator: ", ")
            f.project = o == nil ? "All projects" : names.isEmpty ? "No projects" : names
            f.colors = o == nil ? [] : ps.compactMap { $0["color"] as? String }
        }
    }
    func l1Pick(here: Bool = true, in l: L1? = nil) {
        let list = (try? JSONSerialization.data(withJSONObject: projList)).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
        let o = l == nil ? l1Only : projList.isEmpty ? nil : l!.only
        let only = (o.flatMap { try? JSONSerialization.data(withJSONObject: $0) }).flatMap { String(data: $0, encoding: .utf8) } ?? "null"
        (l?.web ?? fluid).evaluateJavaScript("window.mc && mc.pickProjects && mc.pickProjects(\(list), \(only), \(here))")
    }

    // MARK: more Greenlight Center windows (L1's Open projects → New window): each with its own projects and its own Chief (Core(session:)),
    // remembered in l1Windows. Its close button ends that conversation, as a terminal New window's kills its shells. The main one is `window`.

    final class L1 {
        let id: Int, w: NSWindow, web: WKWebView, c: Core, chat: NSHostingView<ChatView>, split = Split(), floor = FloorState()
        var only: [String]?, picked = [String](), pushedRepos = "", pushedChat = ""
        init(_ id: Int, _ w: NSWindow, _ web: WKWebView, _ c: Core, _ only: [String]?) {
            self.id = id; self.w = w; self.web = web; self.c = c; self.only = only
            chat = NSHostingView(rootView: ChatView(c: c)); chat.sizingOptions = []
        }
    }
    var l1s = [Int: L1]()
    func l1(_ web: WKWebView?) -> L1? { l1s.values.first { $0.web === web } }
    func saveL1s() { ud.set(l1s.values.sorted { $0.id < $1.id }.map { l in l.only.map { ["id": l.id, "only": $0] as [String: Any] } ?? ["id": l.id] }, forKey: "l1Windows") }
    /// Its Chief's scope: its projects, the whole time it's open (it's a window: it closes rather than hides).
    func scope(_ l: L1) -> String? { let o = projList.isEmpty ? nil : l.only; return windowScope(paths: sel(o, "paths"), repos: sel(o, "repos"), shown: true) }

    @discardableResult
    func makeL1(_ id: Int, only: [String]?, from: NSWindow? = nil) -> L1 {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.backgroundColor = .black; w.titleVisibility = .hidden; w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: 480, height: 520); w.delegate = self; w.appearance = window.appearance
        let c = Core(session: id), l = L1(id, w, makeWeb(), c, only)
        c.follow(greenlight)
        c.scope = { [weak self, weak l] in l.flatMap { self?.scope($0) } }
        c.popoverShown = { [weak w] in NSApp.isActive && w?.isKeyWindow == true }
        c.light = { [weak self, weak l] names in
            guard let self, let l else { return }
            l.picked = names.reduce(into: []) { if !$0.contains($1) { $0.append($1) } }
            self.pushPicks(l.web)
        }
        c.unattach = { [weak l] key in
            let k = (try? JSONSerialization.data(withJSONObject: [key])).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
            l?.web.evaluateJavaScript("window.mc && mc.undock(\(k)[0])")
        }
        l.floor.at = "greenlight"
        let go: (String) -> Void = { [unowned self] f in  // ponytail: down to the Basement or the Pipe is a cut; the ride is the main window's
            wantFloor = f; (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.opens('\(f)')"); basement() }
        floorGo[ObjectIdentifier(w)] = (l.floor, go)
        floorBar(w, Floors(s: l.floor, c: greenlight, pick: { [weak self, weak l] in if let l { self?.l1Pick(in: l) } }, go: go))
        l1s[id] = l; syncL1()
        let sp = l.split
        sp.isVertical = true; sp.dividerStyle = .thin; sp.delegate = self
        sp.addArrangedSubview(l.web); sp.addArrangedSubview(l.chat)
        sp.setHoldingPriority(.defaultLow, forSubviewAt: 0); sp.setHoldingPriority(.defaultLow + 1, forSubviewAt: 1)
        for (v, wd, pr) in [(l.web, 480, NSLayoutConstraint.Priority.defaultHigh + 1), (l.chat, 320, .defaultHigh)] as [(NSView, CGFloat, NSLayoutConstraint.Priority)] {
            let k = v.widthAnchor.constraint(greaterThanOrEqualToConstant: wd); k.priority = pr; k.isActive = true
        }
        w.contentView = sp
        if !w.setFrameUsingName("main-\(id)") {
            if let f = (from ?? (window.isVisible ? window : nil))?.frame { w.setFrame(f, display: false); w.setFrameTopLeftPoint(NSPoint(x: f.minX + 30, y: f.maxY - 30)) } else { w.center() }
        }
        w.setFrameAutosaveName("main-\(id)")
        sp.layoutSubtreeIfNeeded()
        sp.setPosition(sp.bounds.width - 440, ofDividerAt: 0)
        sp.autosaveName = "chatSplit-\(id)"
        loadFluid(l.web)
        return l
    }
    func newL1(_ only: [String]?, from: NSWindow?) {
        // never a closed window's id: its chat-<n> cleanup is one ssh that may not have landed (host asleep) or may land late
        let id = max(ud.integer(forKey: "l1Next"), (l1s.keys.max() ?? 0) + 1)
        ud.set(id + 1, forKey: "l1Next")
        let l = makeL1(id, only: only, from: from)
        saveL1s()
        l.w.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func reopenL1s() {
        guard !selftest else { return }  // not your windows and their chat-<n>
        let saved = ud.array(forKey: "l1Windows") as? [[String: Any]] ?? []
        for d in saved { if let id = d["id"] as? Int, id > 0 { makeL1(id, only: d["only"] as? [String]).w.orderFront(nil) } }
        if !saved.isEmpty { _ = termWindow }  // its page lists the projects (hidden): until it has, a window shows All projects
    }
    /// The close button: its conversation goes, with its transcript and its open questions in the decision record (chat.py new).
    func closeL1(_ l: L1) {
        l.c.queue = []; l.c.proc?.terminate(); l.c.run = nil  // its turn's late result writes nothing (onExit checks run)
        ssh("\(l.c.chief) stop; \(l.c.chief) new; cd \(greenlightHome) && rm -rf chat-\(l.id)")  // cd: a home with a space fails, not rm's word split
        [l.c.transcriptFile, l.c.questionsURL].forEach { try? FileManager.default.removeItem(at: $0) }
        [l.c.meterKeys.context, l.c.meterKeys.window].forEach { ud.removeObject(forKey: $0) }  // no stale defaults (ids aren't reused)
        l.web.configuration.userContentController.removeScriptMessageHandler(forName: "mc")  // it retains self
        floorGo[ObjectIdentifier(l.w)] = nil; l.c.subs.removeAll(); l1s[l.id] = nil; saveL1s()
        l.w.setFrameAutosaveName(""); NSWindow.removeFrame(usingName: "main-\(l.id)")
        l.split.autosaveName = nil; ud.removeObject(forKey: "NSSplitView Subview Frames chatSplit-\(l.id)")
    }

    // MARK: the T orb and its terminal: login shells in tabs in their own window; the close button keeps them

    let orbState = OrbState()
    lazy var termOrb: NSPanel = {
        let p = floatPanel(NSSize(width: 64, height: 64))
        p.hasShadow = false  // the faces draw their own (and the green light its glow)
        let h = FloatHost(rootView: OrbFace(s: orbState))
        h.onPress = { [weak self] in self?.orbState.pressed = $0 }
        h.draggable = true
        h.onClick = { [weak self] in self.map { $0.ride(up: !$0.upstairs) } }
        orbState.tap = h.onClick
        h.onMoved = { [weak self] in self?.saveOrb(self?.termOrb, "termOrbOff") }
        h.inside = { [weak self] in self?.orbArea }
        p.contentView = h
        return p
    }()
    var ptys = [String: Pty]()  // one shell per tab, by "window:tab"
    /// Terminal windows by id: 0 is the orb's (every project, its close button hides it); the rest you made with
    /// New window (some projects, its close button kills its shells), remembered in termWindows.
    var termWins = [Int: NSWindow]()
    var wantFloor: String?  // the floor Greenlight's indicator asked for, if the terminal page wasn't loaded yet
    var floors = [Int: FloorState]()  // each terminal window's floor indicator
    var floorGo = [ObjectIdentifier: (FloorState, (String) -> Void)]()  // every window with floors (L1's too): where it is, how it rides; ⌘↑/⌘↓ use it
    lazy var termWindow: NSWindow = makeTermWindow(0, only: ud.array(forKey: "termOnly0") as? [String])  // the orb's window shows all projects unless you narrowed it
    var termWebs: [WKWebView] { termWins.values.compactMap { $0.contentView as? WKWebView } }
    func termId(_ web: WKWebView?) -> Int? { termWins.first { $0.value.contentView === web }?.key }
    func termState(_ wid: Int) -> URL { support.appendingPathComponent(wid == 0 ? "terminals.json" : "terminals-\(wid).json") }
    var projectsFile: URL { support.appendingPathComponent("projects.json") }
    var savedProjects: [[String: Any]] { (try? Data(contentsOf: projectsFile)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [[String: Any]] } ?? [] }
    /// The projects ([{id, name, repos, dirs, dir?}]) from the Basement's editor or Settings → Terminal: checked, saved, sent to every terminal window.
    @discardableResult
    func saveProjects(_ any: Any?, from web: WKWebView? = nil) -> Bool {
        let dir = #"^/[\w./@+-]{1,300}$"#
        guard let list = any as? [[String: Any]], list.count <= 100, list.allSatisfy({ p in
            (p["id"] as? String)?.range(of: #"^p\w{1,40}$"#, options: .regularExpression) != nil && (p["name"] as? String)?.count ?? 0 <= 80
            && (p["repos"] as? [String])?.count ?? 999 <= 500 && ((p["dirs"] as? [String]).map { $0.count <= 100 && $0.allSatisfy { $0.range(of: dir, options: .regularExpression) != nil && !$0.contains("..") } } ?? false)
            && (p["dir"] == nil || (p["dir"] as? String).map { $0.range(of: dir, options: .regularExpression) != nil && !$0.contains("..") } ?? false)  // the default folder
        }), let d = try? JSONSerialization.data(withJSONObject: list) else { return false }
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        try? d.write(to: projectsFile, options: .atomic)
        let s = String(decoding: d, as: UTF8.self)
        termWebs.filter { $0 !== web }.forEach { $0.evaluateJavaScript("term.projects(\(s))") }
        return true
    }
    func makeTermWindow(_ id: Int, only: [String]?) -> NSWindow {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 460), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Terminal"
        w.titleVisibility = .hidden
        w.titlebarAppearsTransparent = true
        w.backgroundColor = theme.bg
        w.appearance = NSAppearance(named: isDark ? .darkAqua : .aqua)  // tick keeps it in step with the switch
        w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: 300, height: 160)
        w.animationBehavior = greenlight.rideTime > 0 ? .default : .none
        w.delegate = self
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "term")
        let web = WKWebView(frame: .zero, configuration: cfg)
        web.setValue(false, forKey: "drawsBackground")
        w.contentView = web
        let name = id == 0 ? "terminal" : "terminal-\(id)"
        if !w.setFrameUsingName(name) { w.center() }
        w.setFrameAutosaveName(name)
        termWins[id] = w
        let fs = FloorState(); floors[id] = fs
        let go: (String) -> Void = { [unowned self, weak web] f in
            if f != "greenlight" { web?.evaluateJavaScript("term.opens('\(f)')") } else if id == 0 { appGoUp() } else { show() }
        }
        floorGo[ObjectIdentifier(w)] = (fs, go)
        floorBar(w, Floors(s: fs, c: greenlight, pick: { [weak web] in web?.evaluateJavaScript("window.pickProjects && pickProjects(true)") }, go: go))
        loadTerm(web, only: only)
        return w
    }
    /// New window: remembered, cascaded off the one you asked from, in front.
    func newTermWindow(_ only: [String]?, from: NSWindow?) {
        var saved = ud.array(forKey: "termWindows") as? [[String: Any]] ?? []
        let nid = (saved.compactMap { $0["id"] as? Int } + termWins.keys).max().map { $0 + 1 } ?? 1
        saved.append(["id": nid]); ud.set(saved, forKey: "termWindows"); setPick(only)  // its projects become everyone's
        let w = makeTermWindow(nid, only: only)
        if let f = from?.frame { w.setFrameTopLeftPoint(NSPoint(x: f.minX + 30, y: f.maxY - 30)) }
        w.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    /// The windows New window made, as they were at quit.
    func reopenTermWindows() {
        for d in ud.array(forKey: "termWindows") as? [[String: Any]] ?? [] {
            guard let id = d["id"] as? Int, id > 0 else { continue }
            makeTermWindow(id, only: sharedOnly).orderFront(nil)
        }
    }
    /// Red button on a window New window made: its shells' tmux sessions go, and so does the window.
    func closeTermWindow(_ wid: Int) {
        guard wid != 0, let w = termWins[wid], let web = w.contentView as? WKWebView else { return }
        web.evaluateJavaScript("term.closeAll()") { [weak self] r, _ in
            guard let self else { return }
            for p in r as? [[String: Any]] ?? [] {
                guard let s = p["session"] as? String, s.range(of: #"^mc-[\w-]{1,64}$"#, options: .regularExpression) != nil,
                      let at = paneWhere(p["ws"] as? String ?? "") else { continue }
                ssh("\(App.tmux) kill-session -t \(shellQuote("=" + s)) 2>/dev/null", on: at.host)
            }
            for k in self.ptys.keys where k.hasPrefix("\(wid):") { self.ptys.removeValue(forKey: k)?.stop() }
            self.termWins[wid] = nil; self.floors[wid] = nil; self.floorGo[ObjectIdentifier(w)] = nil; self.termBadges[wid] = nil
            ud.set((ud.array(forKey: "termWindows") as? [[String: Any]] ?? []).filter { $0["id"] as? Int != wid }, forKey: "termWindows")
            try? FileManager.default.removeItem(at: self.termState(wid))
            web.configuration.userContentController.removeScriptMessageHandler(forName: "term")  // it holds the app, not the page, but the page would linger
            w.setFrameAutosaveName(""); NSWindow.removeFrame(usingName: "terminal-\(wid)"); w.close()  // the next New window may reuse this id
        }
    }
    /// Your Ghostty look: its config's theme file, then the config's own color/font lines over it (ghostty-web theme keys).
    func ghosttyLook() -> [String: String] {
        func lines(_ path: String) -> [(String, String)] {
            ((try? String(contentsOfFile: path, encoding: .utf8)) ?? "").split(separator: "\n").compactMap { l in
                let l = l.trimmingCharacters(in: .whitespaces)
                guard !l.hasPrefix("#"), let eq = l.firstIndex(of: "=") else { return nil }
                return (l[..<eq].trimmingCharacters(in: .whitespaces), l[l.index(after: eq)...].trimmingCharacters(in: .whitespaces))
            }
        }
        let home = NSHomeDirectory(), cfg = ["\(home)/.config/ghostty/config", "\(home)/Library/Application Support/com.mitchellh.ghostty/config"].flatMap(lines)
        var all = cfg
        if var name = cfg.last(where: { $0.0 == "theme" })?.1 {
            if name.contains(":") {  // light:A,dark:B
                let parts = Dictionary(name.split(separator: ",").compactMap { p -> (String, String)? in
                    let kv = p.split(separator: ":", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }; return kv.count == 2 ? (kv[0], kv[1]) : nil }, uniquingKeysWith: { a, _ in a })
                name = parts["dark"] ?? parts.values.first ?? name
            }
            let dirs = ["\(home)/.config/ghostty/themes", "/Applications/Ghostty.app/Contents/Resources/ghostty/themes"]
            let file = dirs.lazy.compactMap { d in (try? FileManager.default.contentsOfDirectory(atPath: d))?.first { $0.lowercased() == name.lowercased() }.map { "\(d)/\($0)" } }.first
            all = (file.map(lines) ?? []) + cfg  // the config's own lines win
        }
        let ansi = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
        let keys = ["background": "background", "foreground": "foreground", "cursor-color": "cursor", "cursor-text": "cursorAccent",
                    "selection-background": "selectionBackground", "selection-foreground": "selectionForeground", "font-family": "font", "font-size": "size", "macos-option-as-alt": "optAlt"]
        var out = [String: String]()
        for (k, v) in all {
            if let key = keys[k] { out[key] = ["font", "size", "optAlt"].contains(key) ? v : (v.hasPrefix("#") ? v : "#" + v) }
            else if k == "palette", let eq = v.firstIndex(of: "="), let n = Int(v[..<eq]), n < 16 {
                let c = String(v[v.index(after: eq)...]); out[n < 8 ? ansi[n] : "bright" + ansi[n - 8].capitalized] = c.hasPrefix("#") ? c : "#" + c
            }
        }
        return out
    }
    func rgb(_ c: NSColor) -> String { let c = c.usingColorSpace(.sRGB) ?? c; return String(format: "%02X%02X%02X", Int(c.redComponent * 255), Int(c.greenComponent * 255), Int(c.blueComponent * 255)) }
    /// The terminal page's look: the screen always wears darkTheme with your Ghostty colors and font over it; the chrome follows the mode.
    func termLook() -> [String: String] {
        var look = ["background": "#" + rgb(darkTheme.bg), "foreground": "#" + rgb(darkTheme.fg), "font": codeFont, "size": "\(Int(fontSize))"]
        look.merge(ghosttyLook()) { _, g in g }  // Ghostty's colors and font win over the app's theme
        return look.merging(termMode) { _, m in m }
    }
    var termMode: [String: String] { ["mode": isDark ? "dark" : "light", "lightBg": "#" + rgb(lightTheme.bg), "lightFg": "#" + rgb(lightTheme.fg)] }
    func loadTerm(_ web: WKWebView, only: [String]?) {
        guard let u = Bundle.main.url(forResource: "terminal", withExtension: "html") else { return }
        var q = URLComponents(url: u, resolvingAgainstBaseURL: false)!
        let look = termLook()
        if let b = isDark ? look["background"] : look["lightBg"], b.count == 7 { web.window?.backgroundColor = hex(String(b.dropFirst())).blended(withFraction: isDark ? 0.22 : 0.05, of: .black) }  // the page's band follows  // the tab strip's color, so the title band is part of it
        let json = String(data: (try? JSONSerialization.data(withJSONObject: look)) ?? Data(), encoding: .utf8) ?? "{}"
        q.queryItems = [.init(name: "main", value: termWins[0]?.contentView === web ? "1" : "0"), .init(name: "look", value: json), .init(name: "home", value: host.isEmpty || host == "local" ? NSHomeDirectory() : ""), .init(name: "host", value: host), .init(name: "localname", value: greenlight.localShown), .init(name: "editor", value: editorApp()?.name ?? ""),
                        .init(name: "hosts", value: String(data: (try? JSONSerialization.data(withJSONObject: sshHosts())) ?? Data(), encoding: .utf8))]
        if let only { q.queryItems?.append(.init(name: "only", value: String(data: (try? JSONSerialization.data(withJSONObject: only)) ?? Data(), encoding: .utf8))) }
        web.loadFileURL(q.url!, allowingReadAccessTo: u.deletingLastPathComponent())
    }
    /// ⌘⇧T: down to the Basement from Greenlight; in the Basement, hides or shows the terminal.
    func toggleTerm() {
        if upstairs { return ride(up: false) }
        if termWindow.isVisible && termWindow.isKeyWindow { termWindow.orderOut(nil); hideFloaters(); return }
        basement()
    }
    /// Chief's [console: …]: a tmux session (mc-…), a worktree path or the end of one, or a tab's name; terminal.html's term.ws finds the tab.
    /// ponytail: the key window's tabs only; a tab in another Basement window isn't found
    func toConsole(_ w: String) { toWorkspace(w) }
    var wantWs: String?  // the worktree a dashboard Basement button asked for, if the terminal page wasn't loaded yet
    /// The dashboard's Basement buttons: B1 (the sidebar open) on that worktree, its latest tab there or a new shell in it.
    func toWorkspace(_ path: String) {
        wantFloor = "basement"; wantWs = path
        let p = String(data: (try? JSONSerialization.data(withJSONObject: path, options: .fragmentsAllowed)) ?? Data(), encoding: .utf8) ?? "\"\""
        (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term ? (term.opens('basement'), term.ws(\(p)), 1) : 0") { [weak self] r, _ in
            if r as? Int == 1 { self?.wantWs = nil; self?.wantFloor = nil }
        }
        upstairs ? ride(up: false) : basement()
    }
    func basement() {
        upstairs = false
        termWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.focus()")
        syncOrbs()
    }
    /// A 2–4 word tab name from a tab's recent output: claude -p on this Mac (Settings → Terminal's model, low effort); "" when it fails.
    func aiTabName(_ text: String, model: String, done: @escaping (String) -> Void) {
        claudeP("Name this terminal tab in 2 to 4 words, from what is going on in it (the project, task or tool). "
            + "Reply with only the name: no quotes, no punctuation at the end.\n\n" + String(text.suffix(6000)), model: model) { out in
            done(String(String(out.split(separator: "\n").first ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "\"'`*. ").union(.whitespaces)).prefix(40)))
        }
    }
    /// claude -p on this Mac at low effort: its stdout, "" when it fails.
    func claudeP(_ prompt: String, model: String, done: @escaping (String) -> Void) {
        DispatchQueue.global().async {
            let p = Process(), inPipe = Pipe(), out = Pipe()
            p.executableURL = URL(fileURLWithPath: UserDefaults.standard.string(forKey: "claudeBin") ?? NSHomeDirectory() + "/.local/bin/claude")
            p.arguments = ["-p", "--model", model, "--effort", "low"]
            p.currentDirectoryURL = FileManager.default.temporaryDirectory
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "\(NSHomeDirectory())/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
            p.environment = env
            p.standardInput = inPipe; p.standardOutput = out; p.standardError = FileHandle.nullDevice
            var text = ""
            if (try? p.run()) != nil {
                inPipe.fileHandleForWriting.write(Data(prompt.utf8))
                try? inPipe.fileHandleForWriting.close()
                let d = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
                if p.terminationStatus == 0 { text = String(decoding: d, as: UTF8.self) }
            }
            DispatchQueue.main.async { done(text) }
        }
    }
    /// Each pane is a tmux session on its own socket, so quitting the app only detaches: relaunch reattaches, and
    /// after a reboot `new-session -A` starts it fresh in the workspace. Options go inline: no config on either host.
    static let tmuxOpts = ["set -g status off", "set -g prefix None", "set -s escape-time 0", "set -g set-titles on",
        // the inner app's own title (Claude Code's "✳ name"); else the folder, not tmux's default (the hostname); "⌬ " when it runs codex
        "set -g set-titles-string '#{?#{m:*codex*,#{pane_current_command}},⌬ ,}#{?#{==:#{pane_title},#{host}},#{pane_current_path},#{pane_title}}'",
        "set -g default-terminal tmux-256color", "set -s 'terminal-features[90]' 'xterm-256color:RGB'", "set -g allow-passthrough on",
        "set -g history-limit 50000", "set -s 'terminal-overrides[90]' '*:smcup@:rmcup@'"]  // no alternate screen: output scrolls in the page's own scrollback
    // ponytail: the two lists are set at a fixed index, as an append (-as) grew them on every attach
    static let tmux = "tmux -L greenlight -f /dev/null"
    /// The pane's command: tmux (or a plain login shell where there's none) in the workspace, here or on GREENLIGHT_HOST over ssh.
    func paneCmd(session: String, cwd: String, on host: String) -> String {
        let inner = (cwd.isEmpty ? "cd; " : "cd \(shellQuote(cwd)) 2>/dev/null || cd; ") + "command -v tmux >/dev/null || exec $SHELL -l; exec \(App.tmux) start-server \\; "
            + App.tmuxOpts.joined(separator: " \\; ") + " \\; new-session -A -s \(shellQuote(session)) -c \"$PWD\""
        if host.isEmpty || host == "local" { return inner }
        // -q: a lost connection still shows (exit 255). ControlPath=none: each pane its own connection, as a wedged ControlMaster
        // (~/.ssh/config's ControlMaster auto) left every new pane hanging; ponytail: a full handshake per pane, fine at tab counts
        return "exec ssh -qt -o ControlPath=none -o ServerAliveInterval=30 \(shellQuote(host)) \(shellQuote("exec $SHELL -lc " + shellQuote(inner)))"
    }
    var termLoaded = false, treesProc: Process?, treesAt = Date.distantPast, termBadges: [Int: Int] = [:], tunnels: [String: Process] = [:]
    /// Open in editor's app: Zed, Cursor or VS Code, whichever is installed first (the page names its buttons after it).
    func editorApp() -> (bid: String, url: URL, name: String)? {
        [("dev.zed.Zed", "Zed"), ("com.todesktop.230313mzl4w4u92", "Cursor"), ("com.microsoft.VSCode", "VS Code")].lazy
            .compactMap { i, n in NSWorkspace.shared.urlForApplication(withBundleIdentifier: i).map { (i, $0, n) } }.first
    }
    func tunnelState(_ key: String, _ st: String) {  // a port row's tunnel: opening, open, failed, closed
        let a = String(data: (try? JSONSerialization.data(withJSONObject: [key, st])) ?? Data(), encoding: .utf8) ?? "[]"
        termWebs.forEach { $0.evaluateJavaScript("window.term && term.tunnel(...\(a))") }
    }
    func toastTerm(_ web: WKWebView?, _ text: String) {
        let d = (try? JSONSerialization.data(withJSONObject: text, options: .fragmentsAllowed)).flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
        web?.evaluateJavaScript("term.toast(\(d))")
    }
    /// The sidebar: every installed ADE's worktrees (dash.py trees), where greenlight lives.
    func pushTrees() {
        guard termLoaded, treesProc == nil else { return }
        treesAt = Date()
        var out = ""
        treesProc = ssh("\(py)/dash.py trees", onLine: { out += $0 }, onExit: { [weak self] _ in
            guard let self else { return }
            self.treesProc = nil
            if out.hasPrefix("{") { self.termWebs.forEach { $0.evaluateJavaScript("term.workspaces(\(out))") } }
        })
    }
    func termMessage(_ b: [String: Any], from web: WKWebView?) {
        guard let wid = termId(web) else { return }  // a closed window's page, still flushing
        let tid = b["id"] as? Int ?? 0, id = "\(wid):\(tid)", cols = b["cols"] as? Int ?? 80, rows = b["rows"] as? Int ?? 24
        func js(_ o: Any) -> String { String(data: (try? JSONSerialization.data(withJSONObject: o, options: .fragmentsAllowed)) ?? Data(), encoding: .utf8) ?? "null" }
        // ids that reach a shell command: a tmux session name, a worktree path 
        let session = (b["session"] as? String).flatMap { $0.range(of: #"^mc-[\w-]{1,64}$"#, options: .regularExpression) != nil ? $0 : nil }
        switch b["cmd"] as? String {
        case "ready":
            for k in ptys.keys where k.hasPrefix("\(wid):") { ptys.removeValue(forKey: k)?.stop() }  // a (re)loaded page starts with no tabs: detach, restore reattaches
            // ponytail: a reattach shows tmux's current screen only (older scrollback stays in tmux), and sessions a crash
            // orphaned before a save stay on the socket; reap ones not in the saved layout here if they pile up
            termLoaded = true
            func file(_ u: URL) -> String { (try? Data(contentsOf: u)).flatMap { try? JSONSerialization.jsonObject(with: $0) } != nil ? (try? String(contentsOf: u, encoding: .utf8)) ?? "null" : "null" }
            web?.evaluateJavaScript("term.projects(\(file(projectsFile)))")
            web?.evaluateJavaScript("term.slide(\(greenlight.sidebarSlide))")
            web?.evaluateJavaScript("term.restore(\(file(termState(wid))))")
            web?.evaluateJavaScript("term.opens('\(wid != 0 ? "pipe" : wantFloor ?? (greenlight.termOpens == "basement" ? "basement" : "pipe"))')")  // New windows: always the Pipe
            if wid == 0 { wantFloor = nil; if let p = wantWs { wantWs = nil; web?.evaluateJavaScript("term.ws(\(js(p)))") } }
            pushTrees()
        case "open":
            guard let session, let at = paneWhere(b["ws"] as? String ?? "") else { return }
            let p = Pty()
            p.onOutput = { [weak web] d in web?.evaluateJavaScript("term.feed(\(tid),'\(d.base64EncodedString())')") }
            p.onExit = { [weak web] code in web?.evaluateJavaScript("term.exited(\(tid), \(code))") }
            ptys[id] = p; p.start(cols: cols, rows: rows, cmd: paneCmd(session: session, cwd: at.cwd, on: at.host))
        case "in":
            ptys[id]?.write(b["d"] as? String ?? ""); greenlight.active()
        case "size": ptys[id]?.resize(cols: cols, rows: rows)
        case "close":  // you closed it: the session goes too (quitting the app only detaches)
            ptys.removeValue(forKey: id)?.stop()
            if let session, let at = paneWhere(b["ws"] as? String ?? "") { ssh("\(App.tmux) kill-session -t \(shellQuote("=" + session)) 2>/dev/null", on: at.host) }
        case "hide": if wid == 0 { termWindow.orderOut(nil); hideFloaters() } else { closeTermWindow(wid) }  // its last tab closed
        case "title": floors[wid]?.project = String((b["text"] as? String ?? "").prefix(80)); floors[wid]?.colors = b["colors"] as? [String] ?? []
            if let l = b["list"] as? [[String: Any]] { projList = l; syncL1() }
        case "side": floors[wid]?.at = b["open"] as? Bool ?? false ? "basement" : "pipe"; if wid == 0 { termSide = b["open"] as? Bool ?? false; syncOrbs() }  // the green light shows only while the Basement's sidebar is open
        case "projects": saveProjects(b["projects"], from: web)
        case "pickdir":  // Projects' Choose folder…: the path goes back to the page, which checks it like a pasted one
            guard let win = web?.window else { return }
            let p = NSOpenPanel(); p.canChooseDirectories = true; p.canChooseFiles = false
            p.beginSheetModal(for: win) { r in if r == .OK, let u = p.url { web?.evaluateJavaScript("term.picked(\(js(u.path)))") } }
        case "only":  // the title's Show projects: this window's set, changed in place (its shells stay)
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil || only!.count > 100 { return }
            setPick(only, except: web)  // every window and L1 follow
        case "window":  // New window: all projects (only null) or some, by id ("" is No Man's Land)
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil { return }
            newTermWindow(only, from: web?.window)
        case "look":  // Customize look: the page wrote the whole prompt; the answer goes back raw, the page checks every value
            guard let text = b["text"] as? String, text.count <= 8000 else { return }
            // ponytail: Sonnet for looks (Haiku's palettes clashed); a Settings picker if you want to choose
            claudeP(text, model: "claude-sonnet-5-5") { [weak web] out in web?.evaluateJavaScript("term.looked(\(js(String(out.prefix(4000)))))") }
        case "band":  // the tab strip's computed color, so the title band matches it
            let c = b["color"] as? String ?? "", k = c.hasPrefix("color(") ? 1.0 : 255  // WebKit computes color-mix() as color(srgb r g b), 0–1
            let n = c.split(whereSeparator: { !"0123456789.".contains($0) }).compactMap { Double($0) }
            if n.count == 3 || (n.count == 4 && n[3] >= 0.99) { web?.window?.backgroundColor = NSColor(srgbRed: n[0] / k, green: n[1] / k, blue: n[2] / k, alpha: 1) }
        case "trees": pushTrees()
        case "suggest":  // the sidebar card's next-step button: dash.py suggest (cached per state), answered with term.suggest
            if let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil { suggest(name) }
        case "tree":  // the sidebar's workspace menu: dash.py tree info|rm|goal|cfg|merge|new|diff <path>, answered with term.tree(json)
            guard let op = b["op"] as? String, ["info", "rm", "goal", "cfg", "merge", "ready", "signoff", "new", "diff"].contains(op), (b["text"] as? String)?.count ?? 0 <= 8192, let path = b["path"] as? String,
                  path.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil, !path.contains("..") else { return }
            var out = ""
            ssh("\(py)/dash.py tree \(op) '\(path)'\(b["force"] as? Bool == true ? " --force" : "")", stdin: b["text"] as? String ?? "",
                onLine: { out += $0 }, onExit: { [weak self, weak web] code in
                let js = out.hasPrefix("{") ? out : #"{"op":"\#(op)","path":"\#(path)","error":"\#(host) didn't answer (exit \#(code))"}"#
                web?.evaluateJavaScript("term.tree(\(js))")
                if op != "info" && op != "diff" { self?.pushTrees() }
            })
        case "url":  // a link in a terminal: web links only, the output isn't ours to trust
            if let u = URL(string: b["url"] as? String ?? ""), ["http", "https"].contains(u.scheme?.lowercased() ?? "") { NSWorkspace.shared.open(u) }
        case "settings":  // the hover card's "Edit <repo>'s prompt…"
            if let r = b["repo"] as? String, r.range(of: #"^[\w.-]{1,100}$"#, options: .regularExpression) != nil, !r.hasPrefix(".") {
                let k = b["kind"] as? String ?? "", kind = RulesStore.kinds.contains { $0.0 == k } ? "/" + k : ""
                greenlight.settingsTab = "Repositories"; greenlight.settingsRepo = r + kind; appSettings()
            } else { appSettings() }  // Jump › Settings
        case "up": appGoUp()  // Jump › Greenlight Center
        case "localName":  // the sidebar's Rename on this Mac
            let n = String((b["name"] as? String ?? "").trimmingCharacters(in: .whitespaces).prefix(60)); greenlight.localName = n == localDefault ? "" : n
        case "notify":  // auto-run's jobs, and an agent that finished or wants you (the page sends one per change, any window)
            if let t = b["title"] as? String, let body = b["body"] as? String { greenlight.notify(title: String(t.prefix(200)), body: String(body.prefix(500)), subtitle: nil, reply: false) }
        case "copy":
            if let s = b["text"] as? String { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(s, forType: .string) }
        case "save":
            if let st = b["state"], let d = try? JSONSerialization.data(withJSONObject: st) {
                try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
                try? d.write(to: termState(wid), options: .atomic)
            }
        case "name":
            let tab = b["tab"] as? Int ?? 0, auto = b["auto"] as? Bool ?? false
            let reply: (String) -> Void = { [weak web] n in web?.evaluateJavaScript("term.named(\(tab), \(js(n)))") }
            if auto && !greenlight.termAutoName { return reply("") }  // still answer: the page counts auto runs in flight
            aiTabName(b["text"] as? String ?? "", model: greenlight.termNameModel, done: reply)
        case "badge":  // agents waiting on you, per window: the Dock icon shows the sum
            termBadges[wid] = max(0, b["n"] as? Int ?? 0)
            let n = termBadges.values.reduce(0, +); NSApp.dockTile.badgeLabel = n > 0 ? "\(n)" : nil
        case "file":  // a dropped or pasted file: written where the pane's shell runs, its path goes back to be pasted
            guard let at = paneWhere(b["ws"] as? String ?? ""), let data = b["data"] as? String, data.count <= 14_000_000,
                  data.range(of: #"^[A-Za-z0-9+/=]*$"#, options: .regularExpression) != nil else { return }
            let name = String((b["name"] as? String ?? "").replacingOccurrences(of: #"[^\w.-]"#, with: "_", options: .regularExpression).prefix(80))
            let path = "/tmp/greenlight/\(UUID().uuidString.prefix(8))-\(name.isEmpty || name.hasPrefix(".") ? "file" + name : name)"
            // ponytail: stdin is written on the main thread, so a 10 MB drop over a slow ssh stalls the UI for its upload
            ssh("mkdir -p /tmp/greenlight && base64 -d > \(shellQuote(path))", on: at.host, stdin: data, onExit: { [weak web] code in
                web?.evaluateJavaScript("term.filed(\(tid), \(js(code == 0 ? path : "")))")
            })
        case "port":  // a dev-server port chip: localhost here, else through an ssh tunnel to the pane's host first; its state goes back to every window
            guard let port = b["port"] as? Int, (1024...65535).contains(port), let at = paneWhere(b["ws"] as? String ?? ""),
                  let u = URL(string: "http://localhost:\(port)") else { return }
            if at.host.isEmpty || at.host == "local" { NSWorkspace.shared.open(u); return }
            let key = "\(at.host):\(port)"
            if tunnels[key]?.isRunning == true { NSWorkspace.shared.open(u); return tunnelState(key, "open") }
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
            p.arguments = ["-N", "-o", "BatchMode=yes", "-o", "ControlPath=none", "-o", "ExitOnForwardFailure=yes", "-L", "\(port):localhost:\(port)", at.host]
            p.standardError = FileHandle.nullDevice
            // ControlPath=none: through a ControlMaster the forward lands on the shared master and this ssh exits at once, so it would outlive the app
            p.terminationHandler = { [weak self] q in DispatchQueue.main.async { if self?.tunnels[key] === q { self?.tunnels[key] = nil; self?.tunnelState(key, q.terminationStatus == 0 ? "closed" : "failed") } } }  // ssh exiting on its own (port taken, host down, dropped): failed; Cancel removes it first
            guard (try? p.run()) != nil else { return tunnelState(key, "failed") }
            tunnels[key] = p; tunnelState(key, "opening")
            // ponytail: 1.5s for ssh to bind; a local port already in use makes ssh exit (ExitOnForwardFailure), which reads as failed
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                guard self?.tunnels[key] === p else { return }
                if p.isRunning { NSWorkspace.shared.open(u); self?.tunnelState(key, "open") } else { self?.tunnelState(key, "failed") }
            }
        case "untunnel":  // Cancel / Close on a port row: ends that tunnel
            guard let port = b["port"] as? Int, let at = paneWhere(b["ws"] as? String ?? "") else { return }
            let key = "\(at.host):\(port)"
            if let p = tunnels.removeValue(forKey: key) { p.terminate() }
            tunnelState(key, "closed")
        case "edit":  // Open in editor: Zed, Cursor or VS Code, whichever is installed first; over ssh for a remote host
            guard let at = paneWhere(b["ws"] as? String ?? ""), !at.cwd.isEmpty else { return }
            guard let (bid, app, _) = editorApp() else { return toastTerm(web, "No Zed, Cursor or VS Code installed") }
            if at.host.isEmpty || at.host == "local" {
                NSWorkspace.shared.open([URL(fileURLWithPath: at.cwd)], withApplicationAt: app, configuration: NSWorkspace.OpenConfiguration())
            } else if bid == "dev.zed.Zed" {  // zed's cli opens ssh://host/path as a remote project
                let p = Process(); p.executableURL = app.appendingPathComponent("Contents/MacOS/cli"); p.arguments = ["ssh://\(at.host)\(at.cwd)"]; try? p.run()
            } else if let u = URL(string: "\(bid == "com.microsoft.VSCode" ? "vscode" : "cursor")://vscode-remote/ssh-remote+\(at.host)\(at.cwd)") {
                NSWorkspace.shared.open(u)
            }
        case "error": NSLog("greenlight terminal error: %@", b["text"] as? String ?? "")
        default: break
        }
    }

    /// The orbs ride with the dashboard: shown while it's up (unless Settings → Appearance turns them off), at normal
    /// window level so other apps cover them, and raised back over the dashboard whenever a click would bury them.
    /// They're child windows of the dashboard, kept inside it: they move with it, and a resize pulls them back in.
    /// One orb, two faces: in the Basement the green light (only while its sidebar is open) goes up; in Greenlight the terminal button goes down.
    func syncOrbs() {
        guard !riding else { return }
        let host = orbHost
        guard host.isVisible, host.isOnActiveSpace else { return }  // else it'd land in whatever Space is in front
        orbState.up = !upstairs
        if greenlight.orbsHidden || (!upstairs && !termSide) { hideFloaters(); return }
        if termOrb.parent !== host { hideFloaters() }
        if !termOrb.isVisible {
            if let k = keepAt {  // ponytail: clamped into the new level's area, so a spot outside it (the chat, the footer) still shifts
                let a = orbArea, w = termOrb.frame.size
                ud.set(NSStringFromPoint(NSPoint(x: a.maxX - k.x - w.width / 2, y: k.y - w.height / 2 - a.minY)), forKey: "termOrbOff")
            }
            keepAt = nil; placeOrb(termOrb); host.addChildWindow(termOrb, ordered: .above) }
    }
    var orbHost: NSWindow { upstairs ? window : termWindow }

    // MARK: Basement ↔ Greenlight Center: one level at a time, the orb rides between them

    var upstairs = false, riding = false, termSide = false, rideWin: NSPanel?, keepAt: NSPoint?
    @objc func appGoUp() { if !upstairs { ride(up: true) } else { show() } }
    func ride(up: Bool) {
        guard !riding, up != upstairs else { return }
        let from = orbHost, to = up ? window : termWindow
        guard from.isVisible, let scr = from.screen ?? NSScreen.main else { return up ? show() : basement() }
        if termOrb.isVisible { keepAt = NSPoint(x: termOrb.frame.midX, y: termOrb.frame.midY) }  // the orb stays put on screen; syncOrbs lands it there
        guard greenlight.rideTime > 0 else { if !up { from.orderOut(nil); hideFloaters() }; return up ? show() : basement() }  // Settings → Terminal → Ride animation 0: a cut
        riding = true
        let S = scr.frame, reduce = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let c = termOrb.isVisible ? NSPoint(x: termOrb.frame.midX, y: termOrb.frame.midY) : NSPoint(x: from.frame.maxX - 44, y: from.frame.minY + 44)
        let orb = CGPoint(x: c.x - S.minX, y: S.maxY - c.y)
        func shot(_ w: NSWindow, _ img: NSImage?) -> Shot {
            Shot(img: img, band: w === window ? NSColor(white: 0.93, alpha: 1) : w.backgroundColor,
                 rect: CGRect(x: w.frame.minX - S.minX, y: S.maxY - w.frame.maxY, width: w.frame.width, height: w.frame.height), top: w.frame.height - w.contentLayoutRect.height)
        }
        let k = reduce ? 1 : greenlight.rideTime / 0.6  // every beat below is written for a 0.6s ride
        func later(_ t: Double, _ f: @escaping () -> Void) { DispatchQueue.main.asyncAfter(deadline: .now() + t * k, execute: f) }
        func at(_ t: Double, _ d: Double, _ curve: (Double) -> Animation = Animation.easeInOut, _ f: @escaping () -> Void) {
            let a = curve(d * k); later(t) { withAnimation(a, f) }
        }
        snap(from) { [self] img in
            let s = RideState(); s.tone = up ? 0 : 1; s.faceUp = up
            let p = NSPanel(contentRect: S, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = false; p.ignoresMouseEvents = true; p.level = .floating
            p.contentView = NSHostingView(rootView: RideView(up: up, from: shot(from, img), orb: orb, matte: greenlight.orbStyle == "matte", face: !greenlight.orbsHidden, s: s))
            p.setFrame(S, display: true); p.orderFrontRegardless(); rideWin = p
            hideFloaters()
            termWindow.parent?.removeChildWindow(termWindow)
            // the other level comes up behind the overlay, all but invisible, so it's drawn by the time it grows
            to.alphaValue = 0.01
            DispatchQueue.main.async { [self] in from.orderOut(nil); if up { show() } else { basement() } }  // once the picture covers it
            // one smooth stroke each way, no stops: in to the orb (0.3s), the orb flips, out of it (0.3s)
            if reduce { at(0, 0.2) { s.from.a = 0; s.veil = 0.92 } } else {  // Reduce Motion: a crossfade
                at(0, 0.3, Animation.easeIn) { s.from = Squash(sx: 0.02, sy: 0.02, r: 300, a: 0) }
                at(0, 0.3) { s.veil = 0.92 }
                at(0.14, 0.09, Animation.easeIn) { s.flip = 90 }; at(0.23, 0) { s.faceUp = !up; s.flip = -90 }; at(0.24, 0.1, Animation.easeOut) { s.flip = 0 }  // a coin flip to the other face
            }
            if reduce { at(0.23, 0) { s.faceUp = !up } }
            at(0, 0.3) { s.tone = up ? 1 : 0 }  // the backdrop is the new level's color before that level grows
            DispatchQueue.main.asyncAfter(deadline: .now() + max(0.3 * k, 0.1)) { [self] in  // ponytail: a 0.1s floor so a very short ride doesn't snap the level before it's drawn
                snap(to) { [self] img in
                    s.toShot = shot(to, img)
                    if reduce { s.to = Squash(a: 0); at(0, 0.2) { s.to.a = 1; s.veil = 0 } } else {
                        at(0, 0.3, Animation.easeOut) { s.to = Squash() }
                    }
                    // the backdrop lifts only once the level is in place, so what's behind the app never shows through mid-ride
                    later(reduce ? 0.22 : 0.3) { to.alphaValue = 1; if !reduce { at(0, 0.18) { s.veil = 0 } } }
                    later(reduce ? 0.22 : 0.5) { [self] in
                        rideWin?.orderOut(nil); rideWin = nil; riding = false
                        keepAt = c; syncOrbs()  // the orb lands where you clicked it
                    }
                }
            }
        }
    }
    /// A window's content as a picture: its views as drawn, each web view's own snapshot over its spot (they draw out of process).
    func snap(_ w: NSWindow, _ done: @escaping (NSImage?) -> Void) {
        guard let v = w.contentView, v.bounds.width > 0, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) else { return done(nil) }
        v.cacheDisplay(in: v.bounds, to: rep)
        let base = NSImage(size: v.bounds.size); base.addRepresentation(rep)
        let webs = ([fluid] + termWebs).filter { $0.window === w && !$0.isHiddenOrHasHiddenAncestor }
        var parts = [(NSImage, NSRect)](), left = webs.count
        func finish() {
            done(NSImage(size: v.bounds.size, flipped: v.isFlipped) { _ in
                for (i, r) in [(base, v.bounds)] + parts { i.draw(in: r, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil) }
                return true
            })
        }
        if webs.isEmpty { return finish() }
        for web in webs {
            let r = web.convert(web.bounds, to: v)
            web.takeSnapshot(with: nil) { img, _ in if let img { parts.append((img, r)) }; left -= 1; if left == 0 { finish() } }
        }
    }
    /// The dashboard pane (left of the chat) on screen, less the page's context rail (--dock-w 64) and footer (--foot-h 32)
    /// so the orb never covers them, and a small margin.
    var orbArea: NSRect {
        if !upstairs { return termWindow.convertToScreen(termWindow.contentLayoutRect).insetBy(dx: 8, dy: 8) }
        let d = split.arrangedSubviews[0], r = window.convertToScreen(d.convert(d.bounds, to: nil))
        return NSRect(x: r.minX, y: r.minY + 32, width: r.width - 64, height: r.height - 32).insetBy(dx: 8, dy: 8)
    }
    func splitViewDidResizeSubviews(_ n: Notification) { if termOrb.isVisible { placeOrb(termOrb) } }
    /// Saved as an offset from the window's bottom-right corner, where it starts.
    func placeOrb(_ p: NSWindow) {
        let a = orbArea, s = p.frame.size
        let off = ud.string(forKey: "termOrbOff").map(NSPointFromString) ?? NSPoint(x: 8, y: 8)
        p.setFrameOrigin(NSPoint(x: min(max(a.maxX - off.x - s.width, a.minX), a.maxX - s.width), y: min(max(a.minY + off.y, a.minY), a.maxY - s.height)))
    }
    func saveOrb(_ p: NSWindow?, _ key: String) {
        guard let p else { return }
        let a = orbArea; ud.set(NSStringFromPoint(NSPoint(x: a.maxX - p.frame.maxX, y: p.frame.minY - a.minY)), forKey: key)
    }
    func windowDidResize(_ n: Notification) {
        // its parent, not orbHost: that reads the lazy termWindow, and this fires while termWindow is being made
        if termOrb.isVisible, n.object as? NSWindow === termOrb.parent { placeOrb(termOrb) }
    }
    // only when its own window is in front: ordering a child window front drags its parent over whatever you clicked (a New window)
    func raiseOrbs() { DispatchQueue.main.async { [self] in if termOrb.isVisible, let p = termOrb.parent, p === NSApp.keyWindow { termOrb.orderFront(nil) } } }

    /// The dashboard went away: its orb goes with it.
    func hideFloaters() { termOrb.parent?.removeChildWindow(termOrb); termOrb.orderOut(nil) }

    func windowShouldClose(_ w: NSWindow) -> Bool {
        if w === chatWindow { popChat(out: false); return false }
        if let wid = termWins.first(where: { $0.value === w })?.key {  // ⌘W closes the tab; the close button hides the orb's window (shells kept), closes a New window one (shells killed)
            if NSApp.currentEvent?.type == .keyDown { (w.contentView as? WKWebView)?.evaluateJavaScript("term.closeTab()") }
            else if wid == 0 { w.orderOut(nil); hideFloaters() } else { closeTermWindow(wid) }
            return false
        }
        if let v = viewWindows.first(where: { $0.value === w })?.key { attach(v); return false }
        if let l = l1s.values.first(where: { $0.w === w }) { closeL1(l) }
        if w === window { hideFloaters() }
        return true
    }
    func applicationDidBecomeActive(_ n: Notification) { if chatShown { greenlight.missed = 0 }; if window.isVisible { greenlight.unread = 0; tick(); raiseOrbs() } }

    /// Menubar icon: outline star idle, filled star + elapsed while working, outline + count for unread pings.
    func tick() {
        if !window.isVisible, tab.isVisible { greenlight.hint = "" }  // window hidden mid-hover: drop the tab
        if window.title != greenlight.name { window.title = greenlight.name }
        if pushedDark != isDark { pushedDark = isDark; greenlight.look += 1 }  // Auto: macOS flipped, so every view re-renders
        let look = NSAppearance(named: theme.dark ? .darkAqua : .aqua)  // the window bar's switch (Auto follows macOS)
        for w in termWins.values where w.appearance?.name != look?.name { w.appearance = look }
        if pushedTerm != theme.name { pushedTerm = theme.name  // the terminals' chrome follows the mode; their screens stay as they are
            let m = termMode; termWebs.forEach { $0.evaluateJavaScript("window.term && term.mode('\(m["mode"]!)', '\(m["lightBg"]!)', '\(m["lightFg"]!)')") } }
        if fluid.appearance?.name != look?.name { fluid.appearance = look }
        if window.appearance?.name != look?.name { window.appearance = look }  // else the full-screen title bar drops in white
        if chatWindow.appearance?.name != look?.name { chatWindow.appearance = look }
        if chatEdge.fillColor != theme.blend(0.1) { chatEdge.fillColor = theme.blend(0.1) }
        for w in viewWindows.values where w.appearance?.name != look?.name { w.appearance = look; w.contentView?.appearance = look }
        if greenlight.run != nil { greenlight.now = Date() }
        greenlight.checkIn()
        greenlight.watchTurn()  // one held back by a running turn or the 5-minute gap
        if termLoaded, Date().timeIntervalSince(treesAt) > (termWins.values.contains(where: \.isVisible) ? 30 : ud.bool(forKey: "autorun") ? 60 : .infinity) { pushTrees() }  // auto-run: the main window watches the workspaces even while hidden
        syncOrbs()
        let bell = "\(greenlight.waiting.count), \(greenlight.inboxOpen), \(greenlight.askedJSON)"  // re-pushed whenever a question changes, not only the count
        if pushedBell != bell { pushedBell = bell; broadcast("window.mc && mc.bell(\(bell))") }
        let orb = greenlight.orbsHidden ? "0px" : "72px"  // the shelf keeps the orb's 56pt and some air clear at its right end
        if pushedOrb != orb { pushedOrb = orb; fluid.evaluateJavaScript("document.documentElement.style.setProperty('--orb-w', '\(orb)'); window.render && render()") }
        let json = { (v: [String]?) in v.flatMap { try? JSONSerialization.data(withJSONObject: $0) }.flatMap { String(data: $0, encoding: .utf8) } ?? "null" }
        let repos = "\(json(l1Paths)), \(json(l1Repos))"
        if pushedRepos != repos { pushedRepos = repos; mainWebs.forEach { $0.evaluateJavaScript("window.mc && mc.paths && mc.paths(\(repos))") } }
        for l in l1s.values {  // the other Greenlight Center windows: their own projects, their chat always in its pane
            let o = projList.isEmpty ? nil : l.only, r = "\(json(sel(o, "paths"))), \(json(sel(o, "repos")))"
            if l.pushedRepos != r { l.pushedRepos = r; l.web.evaluateJavaScript("window.mc && mc.paths && mc.paths(\(r))") }
            if l.pushedChat.isEmpty { l.pushedChat = "true,0"; l.web.evaluateJavaScript("window.mc && mc.chat(true,0)") }
            if l.w.appearance?.name != look?.name { l.w.appearance = look; l.web.appearance = look }
            if case let t = "\(greenlight.name) · \(l.floor.project)", l.w.title != t { l.w.title = t }  // the Window menu tells them apart
            if l.c.run != nil { l.c.now = Date() }
            if l.c.run == nil, l.c.connected != greenlight.connected { l.c.connected = greenlight.connected }  // ponytail: same host, so the main chat's online light stands in until this one runs a turn
        }
        if orbState.matte != (greenlight.orbStyle == "matte") { orbState.matte = greenlight.orbStyle == "matte" }
        if pushedLocal != greenlight.localShown { pushedLocal = greenlight.localShown; let js = (try? JSONEncoder().encode(pushedLocal)).flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
            termWebs.forEach { $0.evaluateJavaScript("window.term && term.localName(\(js))") } }
        if pushedSlide != greenlight.sidebarSlide { pushedSlide = greenlight.sidebarSlide; termWebs.forEach { $0.evaluateJavaScript("window.term && term.slide(\(greenlight.sidebarSlide))") } }
        if pushedMotion != greenlight.rideTime { pushedMotion = greenlight.rideTime
            (Array(termWins.values) + [window]).forEach { $0.animationBehavior = greenlight.rideTime > 0 ? .default : .none } }  // Animations off: no zoom in or out as a level comes or goes
        let chat = "\(!chatHidden),\(greenlight.missed)"
        if pushedChat != chat { pushedChat = chat; mainWebs.forEach { $0.evaluateJavaScript("window.mc && mc.chat(\(chat))") } }
        let colors = themeVars()
        if pushedMono != codeFont + colors {  // the dashboard's logs: Settings' code font, Nerd Font icons full-size like the terminal's
            pushedMono = codeFont + colors
            let stack = #""\#(codeFont)", "Symbols Nerd Font", "Hack Nerd Font", "MesloLGS Nerd Font", "MesloLGS NF", ui-monospace, Menlo, monospace"#
            let js = String(data: (try? JSONSerialization.data(withJSONObject: [stack])) ?? Data(), encoding: .utf8) ?? "[]"
            broadcast("(s => { s.setProperty('--mono', \(js)[0]); \(colors) })(document.documentElement.style)")
        }
        if chatWindow.backgroundColor != theme.bg { chatWindow.backgroundColor = theme.bg }
        if chatWindow.title != greenlight.name { chatWindow.title = greenlight.name }
        guard let b = item.button else { return }
        // Touch the status item only on change: any menubar update keeps a full-screen window's title bar pinned down.
        let working = greenlight.run != nil
        let title = greenlight.run.map { " " + fmt(Date().timeIntervalSince($0.start)) } ?? (greenlight.unread > 0 ? " \(greenlight.unread)" : "")
        let key = "\(working) \(accent)"
        if key != starKey {
            starKey = key
            let img = NSImage(systemSymbolName: working ? "star.fill" : "star", accessibilityDescription: "Greenlight")
            img?.isTemplate = !working
            b.image = working ? img?.withSymbolConfiguration(.init(paletteColors: [NSColor(accent)])) : img
            item.length = working ? 64 : NSStatusItem.variableLength  // fixed while the timer ticks, or the menubar icons jitter each second
        }
        if b.title != title { b.title = title }
    }
    var starKey = ""

    /// The theme's colors as the dashboard's --mc-* variables; its :root falls back to its own palette without them.
    /// The default themes send none, so the dashboard looks as designed. The accent tints chrome only, never a state color.
    func themeVars() -> String {
        func css(_ c: NSColor) -> String {
            let c = c.usingColorSpace(.sRGB) ?? c
            return String(format: "#%02X%02X%02X", Int(c.redComponent * 255 + 0.5), Int(c.greenComponent * 255 + 0.5), Int(c.blueComponent * 255 + 0.5))
        }
        let own = theme.name != (theme.dark ? "Greenlight" : "GitHub Light"), a = accentHex.map(hex) ?? theme.accent
        let sa = a.usingColorSpace(.sRGB) ?? a, light = sa.redComponent * 0.299 + sa.greenComponent * 0.587 + sa.blueComponent * 0.114 > 0.6
        let vars: [(String, NSColor?)] = [("bg", theme.bg), ("surface", theme.blend(0.05)), ("raised", theme.blend(0.1)),
                                          ("text", theme.fg), ("text2", theme.blend(0.82)), ("dim", theme.blend(0.6))].map { ($0.0, own ? $0.1 : nil) }
        let acc: [(String, String?)] = own || accentHex != nil ? [("accent", css(a)), ("on-accent", light ? "#111113" : "#FFFFFF")] : [("accent", nil), ("on-accent", nil)]
        return (vars.map { ($0.0, $0.1.map(css)) } + acc)
            .map { k, v in v.map { "s.setProperty('--mc-\(k)', '\($0)');" } ?? "s.removeProperty('--mc-\(k)');" }.joined(separator: " ")
    }

    @objc func click() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            let voice = NSMenu()
            for (i, (id, title)) in engines.enumerated() {
                let e = voice.addItem(withTitle: title, action: #selector(pickEngine(_:)), keyEquivalent: "")
                e.target = self
                e.tag = i
                e.state = greenlight.engine == id ? .on : .off
            }
            voice.addItem(.separator())
            voice.addItem(withTitle: "Shortcut: \(greenlight.shortcut)  —  Change…", action: #selector(setShortcut), keyEquivalent: "").target = self
            voice.addItem(withTitle: "Choose Whisper model…", action: #selector(pickModel), keyEquivalent: "").target = self
            voice.addItem(withTitle: "Set OpenAI key…", action: #selector(setKey), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Voice", action: nil, keyEquivalent: "").submenu = voice
            menu.addItem(.separator())
            menu.addItem(withTitle: "Quit Greenlight", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            item.menu = menu
            item.button?.performClick(nil)
            item.menu = nil
        } else {
            toggle()
        }
    }


    @objc func pickEngine(_ m: NSMenuItem) { greenlight.engine = engines[m.tag].0 }

    @objc func pickModel() {
        let p = NSOpenPanel()
        p.message = "Pick a whisper.cpp ggml model (.bin)"
        p.directoryURL = URL(fileURLWithPath: whisperModel()).deletingLastPathComponent()
        NSApp.activate(ignoringOtherApps: true)
        if p.runModal() == .OK, let u = p.url { UserDefaults.standard.set(u.path, forKey: "whisperModel") }
    }

    @objc func setKey() { askKey("openai", "OpenAI API key", "the OpenAI Whisper engine and GPT-Live voice mode") }
    func askKey(_ account: String, _ title: String, _ use: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = "Stored in your login Keychain. Used only for \(use). Leave empty to remove it\(account == "openai" ? "; OPENAI_API_KEY is used then, if it’s set" : "")."
        let f = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        a.accessoryView = f
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = f
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        let v = f.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if keychain(account, set: v) == nil {
            let action = v.isEmpty ? "remove" : "save"
            greenlight.msgs.append(Msg(.note, "Couldn't \(action) the \(title) in your Keychain. Try again, or allow Greenlight if the Keychain asks."))
        } else { LivePreview.shared.keyChanged(account, to: v) }
        if account == "elevenlabs" { Speech.shared.warm(); greenlight.objectWillChange.send() }
        if account == "openai" { forgetEnvironmentOpenAIKey() }
    }

    /// The next key press with a modifier, while the window is open, becomes the voice shortcut. Esc cancels.
    var capture: Any?
    var vimMonitor: Any?, orbMonitor: Any?, floorMonitor: Any?
    @objc func setShortcut() {
        show()
        greenlight.hint = "Press the new voice shortcut, with ⌃, ⌥ or ⌘ (Esc cancels)"
        recordShortcut()
    }
    /// Settings' Change…: the recorder shows in place of the keys there, not in the chat's hint.
    func recordShortcut() {
        guard capture == nil else { return }
        greenlight.keying = true
        capture = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard let self else { return e }
            let f = e.modifierFlags
            let mods = (f.contains(.control) ? controlKey : 0) | (f.contains(.option) ? optionKey : 0)
                | (f.contains(.shift) ? shiftKey : 0) | (f.contains(.command) ? cmdKey : 0)
            if e.keyCode != kVK_Escape && mods & (controlKey | optionKey | cmdKey) == 0 { return nil }  // keep waiting
            if e.keyCode != kVK_Escape {
                greenlight.voiceMods = mods
                greenlight.voiceCode = Int(e.keyCode)
                greenlight.voiceKey = (e.charactersIgnoringModifiers ?? "?").uppercased()
                registerVoice()
            }
            greenlight.hint = ""
            greenlight.keying = false
            NSEvent.removeMonitor(capture!)
            capture = nil
            return nil
        }
    }

    /// ⌃⌥M toggles the window from anywhere; the voice shortcut (⌃⌥V by default) starts/stops dictation.
    /// Carbon hot keys need no Accessibility permission.
    func hotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, ev, _ in
            var id = EventHotKeyID()
            GetEventParameter(ev, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &id)
            DispatchQueue.main.async {
                if id.id == 2 {
                    if !delegate.window.isVisible { delegate.show() }
                    delegate.greenlight.toggleVoice()
                } else {
                    delegate.toggle()
                }
            }
            return noErr
        }, 1, &spec, nil, nil)
        var ref: EventHotKeyRef?
        RegisterEventHotKey(UInt32(kVK_ANSI_M), UInt32(controlKey | optionKey), EventHotKeyID(signature: 0x4D434846, id: 1),
                            GetApplicationEventTarget(), 0, &ref)
        registerVoice()
    }

    var voiceRef: EventHotKeyRef?
    func registerVoice() {
        if let r = voiceRef { UnregisterEventHotKey(r) }
        RegisterEventHotKey(UInt32(greenlight.voiceCode), UInt32(greenlight.voiceMods), EventHotKeyID(signature: 0x4D434846, id: 2),
                            GetApplicationEventTarget(), 0, &voiceRef)
    }

    /// Opening the app again (Dock, Spotlight, Finder, `open`) shows the window.
    func applicationWillTerminate(_ n: Notification) { tunnels.values.forEach { $0.terminate() }; Speech.shared.stop() }  // port tunnels die with the app
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { upstairs ? show() : basement(); return false }

    /// Greenlight Center, straight up (a page link, Settings, a notification): the Basement steps aside, no ride.
    func show() {
        if !upstairs, termWindow.isVisible { termWindow.orderOut(nil); hideFloaters() }
        upstairs = true
        greenlight.unread = 0
        tick()
        window.makeKeyAndOrderFront(nil)
        placeChat()
        NSApp.activate(ignoringOtherApps: true)
    }

    /// The star and ⌃⌥M: hide the window when it's in front, else bring it up.
    func toggle() {
        if NSApp.isActive && (upstairs ? window.isKeyWindow || chatWindow.isKeyWindow : termWindow.isKeyWindow) { orbHost.orderOut(nil); hideFloaters() }
        else if upstairs { show() } else { basement() }
    }

    /// Hover hint as a tab hanging off the window's bottom-right corner, so nothing inside moves.
    lazy var tab: NSPanel = {
        let w = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = true
        w.ignoresMouseEvents = true
        w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        return w
    }()
    var hintSink: AnyCancellable?

    func showTab(_ text: String) {
        guard !text.isEmpty, window.isVisible, let pw = chat.window else {
            tab.parent?.removeChildWindow(tab); tab.orderOut(nil); return
        }
        let host = NSHostingView(rootView: HintTab(text: text))
        let size = host.fittingSize
        let r = pw.convertToScreen(chat.convert(chat.bounds, to: nil))
        tab.contentView = host
        tab.setFrame(NSRect(x: r.maxX - size.width - 14, y: r.minY - size.height + 1, width: size.width, height: size.height), display: true)
        if tab.parent == nil { pw.addChildWindow(tab, ordered: .above) }  // child windows follow the window
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        DispatchQueue.main.async {
            if let t = r as? UNTextInputNotificationResponse, !t.userText.isEmpty {
                self.greenlight.reply(to: r.notification.request.content.userInfo["text"] as? String ?? "", t.userText)
            } else {
                self.greenlight.inboxOpen = !self.greenlight.waiting.isEmpty
                self.show()
            }
        }
        done()
    }
}

// MARK: self-test

/// The chat's attachments, as the selftest drives them (and `--attachtest`, alone, where no agent is logged in: a stand-in answers).
/// The composer's checks, then a real message whose files the agent must use to answer. Runs off the main thread.
func attachTest(_ c: Core, check: (String, Bool) -> Void, wait: (String, Double, () -> Bool) -> Void) {
    func onMain<T>(_ f: () -> T) -> T { DispatchQueue.main.sync(execute: f) }
    let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("greenlight-attach-\(getpid())")
    try? FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
    let red = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 64, pixelsHigh: 64, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    for x in 0..<64 { for y in 0..<64 { red.setColor(NSColor(deviceRed: 1, green: 0, blue: 0, alpha: 1), atX: x, y: y) } }
    let png = red.representation(using: .png, properties: [:])!, tiff = red.tiffRepresentation!
    try? png.write(to: tmp.appendingPathComponent("square.png"))
    try? Data("The codeword is FENNEL-4821.\n".utf8).write(to: tmp.appendingPathComponent("notes.txt"))
    try? Data([0x7F, 0x45, 0x4C, 0x46, 0, 0, 0xFF]).write(to: tmp.appendingPathComponent("tool.bin"))
    onMain {
        c.verbose = true  // so the log is on while files attach and go
        let t = chatFile(tiff, name: "shot.tiff"), bin = chatFile(at: tmp.appendingPathComponent("tool.bin")), dir = chatFile(at: tmp)
        let big = chatFile(Data(repeating: 0x61, count: (1 << 20) + 1), name: "big.log"), note = chatFile(at: tmp.appendingPathComponent("notes.txt"))
        check("attach: kinds from the bytes; TIFF goes as PNG; the name has no folder",
              fileKind(png) == "image" && fileKind(Data("%PDF-1.7".utf8)) == "pdf" && fileKind(Data("héllo".utf8)) == "text"
              && t.file?.name == "shot.png" && t.file.map { fileKind($0.data) } == "image" && note.file?.name == "notes.txt" && note.file?.kind == "text")
        check("attach: what can't go says why: \([bin.error, dir.error, big.error].compactMap { $0 })",
              bin.error == "tool.bin isn't a file the chat can use: attach \(ChatFile.supported)." && dir.error?.hasSuffix("is a folder: attach the files in it instead.") == true
              && big.error == "big.log is 1.0 MB: texts can be up to 1.0 MB." && chatFile(Data(), name: "e.txt").error == "e.txt is empty.")
        c.files = []
    }
    // files are read off the main thread: each step waits for them to land
    func read() { wait("files read", 30) { c.reading == 0 } }
    let pb = onMain { NSPasteboard(name: .init("greenlight-attachtest-\(getpid())")) }
    var byURL = false, byData = false, urlNames = [String](), dataNames = [String]()
    onMain { pb.clearContents(); pb.writeObjects([tmp.appendingPathComponent("notes.txt") as NSURL, tmp.appendingPathComponent("square.png") as NSURL]); byURL = c.dropped(pb) }
    read()
    onMain { urlNames = c.files.map(\.name); c.files = []; pb.clearContents(); pb.setData(tiff, forType: .tiff); byData = c.dropped(pb) }
    read()
    onMain {
        dataNames = c.files.map(\.name); c.files = []
        pb.clearContents(); pb.setString("just text", forType: .string)
        let plain = c.dropped(pb)
        pb.clearContents(); pb.setData(Data("{\\rtf1 hi}".utf8), forType: .rtf); pb.setData(tiff, forType: .tiff)
        check("attach: a drop brings files by URL, a browser's image by its data, and leaves text (and rich text's picture) alone: \(urlNames) \(dataNames)",
              byURL && urlNames == ["notes.txt", "square.png"] && byData && dataNames == ["Image.png"] && !plain && !c.dropped(pb) && !Core.takes(pb) && c.files.isEmpty)
        pb.releaseGlobally()
        c.attach((0..<6).map { _ in tmp.appendingPathComponent("notes.txt") })
        check("attach: Send waits while files are read", c.reading == 1 && { c.input = "x"; c.submit(); return c.input == "x" && c.run == nil }())
        c.input = ""
    }
    read()
    onMain {
        check("attach: five at a time: \(c.fileNote)", c.files.count == 5 && c.fileNote == "Attach up to 5 files at a time.")
        c.files.removeLast(4)
        c.attach([tmp.appendingPathComponent("tool.bin"), tmp.appendingPathComponent("square.png")])
    }
    read()
    let photo = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1500, pixelsHigh: 1200, bitsPerSample: 8, samplesPerPixel: 3, hasAlpha: false, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    arc4random_buf(photo.bitmapData!, photo.bytesPerRow * 1200)
    let photoPNG = photo.representation(using: .png, properties: [:])!, shrunk = chatFile(photoPNG, name: "IMG_0001.png")
    mkfifo(tmp.appendingPathComponent("pipe").path, 0o600)
    let t0 = Date(), fifo = chatFile(at: tmp.appendingPathComponent("pipe"))
    check("attach: a named pipe is refused, not read forever (\(Int(Date().timeIntervalSince(t0) * 1000)) ms)", fifo.file == nil && Date().timeIntervalSince(t0) < 2)
    check("attach: a \(byteSize(photoPNG.count)) photo shrinks to fit as JPEG (\(shrunk.file.map { "\($0.name), \(byteSize($0.data.count))" } ?? shrunk.error ?? "-"))",
          photoPNG.count > 5 << 20 && shrunk.file?.name == "IMG_0001.jpg" && (shrunk.file?.data.count ?? .max) <= 5 << 20 && shrunk.file.map { fileKind($0.data) } == "image")
    onMain {
        check("attach: a bad one doesn't stop the rest, and × removes one", c.files.map(\.name) == ["notes.txt", "square.png"] && c.files[1].thumb != nil)
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 560), styleMask: [.titled], backing: .buffered, defer: false)
        let h = NSHostingView(rootView: ChatView(c: c)); w.contentView = h
        h.layoutSubtreeIfNeeded(); RunLoop.main.run(until: Date() + 0.5)
        // Where AppKit sends a drag over the composer's field while you type in it: AppKit's own lookup (private, so only when it's there)
        func all(_ v: NSView) -> [NSView] { [v] + v.subviews.flatMap(all) }
        let sel = NSSelectorFromString("_hitTest:dragTypes:")
        if let field = all(h).compactMap({ $0 as? NSTextField }).first(where: { $0.isEditable }), let imp = class_getMethodImplementation(NSView.self, sel),
           class_getInstanceMethod(NSView.self, sel).map({ String(cString: method_getTypeEncoding($0)!) == "@32@0:8^{CGPoint=dd}16@24" }) == true {
            w.makeFirstResponder(field)
            typealias Hit = @convention(c) (AnyObject, Selector, UnsafePointer<NSPoint>, AnyObject) -> AnyObject?
            var at = field.convert(NSPoint(x: field.bounds.midX, y: field.bounds.midY), to: nil)
            let to = { (types: [NSPasteboard.PasteboardType]) in withUnsafePointer(to: &at) { unsafeBitCast(imp, to: Hit.self)(h, sel, $0, NSSet(array: types.map(\.rawValue))) } }
            let file = to([.fileURL, .init("NSFilenamesPboardType"), .init("Apple URL pasteboard type"), .string]), text = to([.string, .init("NSStringPboardType")])
            check("attach: a file dragged onto the field you're typing in attaches (to \(file.map { "\(type(of: $0))" } ?? "nothing")), text still goes in the field (to \(text.map { "\(type(of: $0))" } ?? "nothing"))",
                  file is DropCatcher && text is NSTextView && field.currentEditor() != nil)
        } else { print("skip attach: AppKit's drag lookup isn't there to ask") }
        if let dir = ProcessInfo.processInfo.environment["GREENLIGHT_SNAP"] {  // UX evidence: the composer with files, a refusal, a drag over it
            c.fileSays("tool.bin isn't a file the chat can use: attach \(ChatFile.supported).")
            for (n, drag) in [("composer", false), ("drop", true)] {
                c.dropping = drag; h.layoutSubtreeIfNeeded(); RunLoop.main.run(until: Date() + 0.5)
                if let rep = h.bitmapImageRepForCachingDisplay(in: h.bounds) { h.cacheDisplay(in: h.bounds, to: rep); try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/attach-\(n).png")) }
            }
            c.dropping = false; c.fileNote = ""
        }
        w.contentView = nil
        c.input = "What's the codeword in notes.txt, and what color is square.png? One line."
        c.submit()
        check("attach: sending empties the composer", c.files.isEmpty && c.input.isEmpty)
    }
    wait("attachments reply", 180) { c.run == nil }
    onMain {
        let you = c.msgs[c.msgs.count - 2].text, r = c.msgs.last!.text
        check("attach: the agent used both files: \(r)", r.contains("FENNEL-4821") && r.lowercased().contains("red") && !c.msgs.last!.failed)
        check("attach: the transcript names the files, never where they were: \(you)", you.hasSuffix("📎 notes.txt  📎 square.png") && !you.contains(tmp.path))
        let ours = c.feed.lines.filter { $0.kind == .status }  // greenlight's own lines; the model's thinking is its own
        check("attach: greenlight's log says a file came, never its folder or contents: \(ours.map(\.text).filter { $0.contains("attach") })",
              ours.contains { $0.text == "attached a file (text, 29 B)" } && !ours.contains { $0.text.contains(tmp.path) || $0.text.contains("The codeword is") })
        c.verbose = false
        c.input = ""; c.files = []
        c.attach([tmp.appendingPathComponent("square.png")])
    }
    wait("files read", 30) { c.reading == 0 }
    onMain { c.submit() }  // files alone are a message
    wait("files-only reply", 180) { c.run == nil }
    check("attach: files with no text still send: \(onMain { c.msgs.last!.text })", onMain { c.msgs[c.msgs.count - 2].text == "📎 square.png" && !c.msgs.last!.failed })
    let noise = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1100, pixelsHigh: 1000, bitsPerSample: 8, samplesPerPixel: 3, hasAlpha: false, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    arc4random_buf(noise.bitmapData!, noise.bytesPerRow * 1000)
    let bigPNG = noise.representation(using: .png, properties: [:])!
    try? bigPNG.write(to: tmp.appendingPathComponent("noise.png"))
    onMain { c.attach([tmp.appendingPathComponent("noise.png")]) }
    wait("files read", 30) { c.reading == 0 }
    let took = onMain { () -> Double in
        c.input = "Reply with just: got it"
        let t0 = Date(); c.submit(); return Date().timeIntervalSince(t0)
    }
    wait("big image reply", 240) { c.run == nil }
    check("attach: a \(byteSize(bigPNG.count)) image goes without holding up the app (send took \(Int(took * 1000)) ms): \(onMain { c.msgs.last!.text })",
          bigPNG.count > 3 << 20 && took < 0.5 && onMain { !c.msgs.last!.failed })
    onMain {  // where greenlight runs refuses one (as if the app's check were older): the turn fails and the files come back
        c.files = []; c.input = "look"
        c.send("look", files: [ChatFile(name: "fake.txt", kind: "text", data: Data([0x7F, 0x45, 0, 0xFF]), thumb: nil)])
    }
    wait("refusal", 60) { c.run == nil }
    check("attach: a send that fails puts its files back: \(onMain { c.msgs.last!.text })",
          onMain { c.msgs.last!.failed && c.msgs.last!.text.hasPrefix("fake.txt isn't a file the chat can use") && c.files.map(\.name) == ["fake.txt"] })
    onMain { c.files = []; c.input = "" }
    try? FileManager.default.removeItem(at: tmp)
}

/// `Greenlight --attachtest`: attachTest on a scratch chat (its CHAT_DIR, as --selftest), then exit.
func attachOnly() {
    var failed = false
    func check(_ what: String, _ ok: Bool) { print(ok ? "ok  " : "FAIL", what); fflush(stdout); failed = failed || !ok }
    func wait(_ what: String, _ secs: Double, _ ok: () -> Bool) {
        let end = Date() + secs
        while !DispatchQueue.main.sync(execute: ok) {
            if Date() > end { check("timed out: \(what)", false); return }
            Thread.sleep(forTimeInterval: 0.5)
        }
    }
    let keep = ["model", "effort", "verboseActivity", "sent"].map { ($0, ud.object(forKey: $0)) }
    let c = Core()
    Thread.detachNewThread {
        DispatchQueue.main.sync { c.clear(); c.model = "claude-haiku-5-5"; c.effort = "low" }
        Thread.sleep(forTimeInterval: 2)
        attachTest(c, check: check, wait: wait)
        DispatchQueue.main.sync { c.clear() }
        for (k, v) in keep { ud.set(v, forKey: k) }
        try? FileManager.default.removeItem(at: transcript)
        try? FileManager.default.removeItem(at: questionsFile)
        print(failed ? "FAILED" : "all ok")
        exit(failed ? 1 : 0)
    }
}


/// `Greenlight.app/Contents/MacOS/Greenlight --selftest`: drives the actions the buttons call
/// (send, Stop, answer buttons, Compact, Codex, Clear) on a scratch session where greenlight runs, plus the menubar width.
func selfTest() {
    let d = UserDefaults.standard
    let keep = ["model", "effort", "context", "window", "name", "seen", "sent", "verboseActivity"].map { ($0, d.object(forKey: $0)) }
    var failed = false
    func onMain<T>(_ f: () -> T) -> T { DispatchQueue.main.sync(execute: f) }
    func check(_ what: String, _ ok: Bool) { print(ok ? "ok  " : "FAIL", what); fflush(stdout); failed = failed || !ok }
    func wait(_ what: String, _ secs: Double, _ ok: () -> Bool) {
        let end = Date() + secs
        while !onMain(ok) {
            if Date() > end { check("timed out: \(what)", false); return }
            Thread.sleep(forTimeInterval: 0.5)
        }
    }
    let fz = fuzzy("evm test", "run the evm unit tests")
    check("!! and !$ expand as whole words only", expandBang("cat !$ again: !!", "open x/y.go") == "cat x/y.go again: open x/y.go" && expandBang("now!! !4521", "z") == "now!! !4521")
    check("fuzzy: in-order letters match, best start wins: \(fz?.pos ?? [])", fz?.pos.first == 8 && fuzzy("evm test", "open an MR") == nil && fuzzy("ETS", "unit tests") != nil)
    let w0 = newNeeds(nil, [["name": "a", "state": "needs", "why": "x"]]), w1 = newNeeds(w0.now, [["name": "a", "state": "needs"], ["name": "b", "state": "needs", "why": "ci"], ["name": "c", "state": "working"]])
    check("watch: launch isn't news, a newly-needing workspace is", w0.new.isEmpty && w1.new == ["b": "ci"] && w1.now.count == 2)
    let wp: [[String: Any]] = [["name": "ana/rsi-loop", "path": "/w/ledger/rsi-loop", "repo": "ledger", "state": "needs"],
                               ["name": "chat-scope", "path": "/w/greenlight/chat-scope", "repo": "greenlight", "state": "needs"],
                               ["name": "made-since", "path": "/w/greenlight/made-since", "repo": "greenlight", "state": "needs"],
                               ["name": "no-repo", "path": "/w/x/no-repo", "repo": "", "state": "needs"],
                               ["name": "fix-ci", "path": "/w/ledger/fix-ci", "repo": "ledger", "state": "needs"],
                               ["name": "fix-ci", "path": "/w/greenlight/fix-ci", "repo": "greenlight", "state": "working"]]
    let wn = ["ana/rsi-loop": "decide", "chat-scope": "approve", "made-since": "ci", "no-repo": "goal", "fix-ci": "ci"]
    let gl = focusScope(["paths": ["/w/greenlight/chat-scope"], "repos": ["greenlight"]]), byPath = focusScope(["paths": ["/w/x/no-repo"], "repos": [""]])
    check("watch: in the greenlight window it asks about greenlight's workspaces only, and holds ledger's for a window that shows it",
          inScope(wn, wp, gl) == (["chat-scope": "approve", "made-since": "ci"], ["ana/rsi-loop": "decide", "no-repo": "goal", "fix-ci": "ci"])
          && inScope(wn, wp, byPath) == (["no-repo": "goal"], ["ana/rsi-loop": "decide", "chat-scope": "approve", "made-since": "ci", "fix-ci": "ci"])
          && inScope(wn, wp, nil) == (wn, [:]) && inScope(wn, wp, focusScope(["paths": [], "repos": []])) == ([:], wn))
    check("watch: the scope is the projects picked while a window shows them; none picked or no window up hears every project",
          windowScope(paths: ["/w/greenlight/chat-scope"], repos: ["greenlight"], shown: true) == gl
          && windowScope(paths: ["/w/greenlight/chat-scope"], repos: ["greenlight"], shown: false) == nil
          && windowScope(paths: nil, repos: nil, shown: true) == nil && windowScope(paths: [], repos: nil, shown: true) == focusScope(["paths": [], "repos": []]))
    let st = staged("Three MRs.\n```stage\n<b>hi</b>\n```\n[stage: full]\nDone."), open = staged("Here.\n```stage\n<ul><li>half")
    let hp: [[String: Any]] = [["name": "acme/mr-5339", "mr": ["ref": "!5339", "title": "pending liquidity receipts"]], ["name": "selkie"], ["name": "mr-1802"]]
    check("hexes: Chief's [hexes: …] line, stripped and resolved; the model's picks only when the reply points at them",
          staged("Merge it.\nOptions: yes | no\n[hexes: selkie, !5339, nope]").hexes == ["selkie", "!5339", "nope"]
          && staged("Merge it.\nOptions: yes | no\n[hexes: selkie]").text == "Merge it.\nOptions: yes | no"
          && staged("Nothing.\n[hexes:]").hexes == [] && staged("Hi").hexes == nil && staged("Hi [hexes: sel").text == "Hi"
          && resolveHexes(["selkie", "!5339", "mr-5339", "nope"], hp) == ["selkie", "acme/mr-5339"]
          && grounded("re-review merge request 5339", hp[0]) && grounded("the pending liquidity receipts change", hp[0])
          && grounded("I'd highlight selkie", hp[1]) && !grounded("Okay, let me know what's next.", hp[2]) && !grounded("about 18020 things", hp[2]))
    let p0: [[String: Any]] = [["name": "a", "state": "working", "step": "ci", "owner": "agent", "action": "fix CI", "agent": ["state": "working", "type": "claude", "tool": "Bash", "input": "make", "last": "On it."]],
                               ["name": "z", "state": "done", "step": "merged", "owner": "nobody"]]
    let p1: [[String: Any]] = [["name": "a", "state": "needs", "step": "review", "owner": "me", "action": "approve it", "agent": ["state": "waiting", "type": "claude", "tool": "Bash", "input": "make", "last": "CI is green. Merge?"]],
                               ["name": "b", "state": "working", "step": "ci", "owner": "agent", "agent": ["state": "working", "type": "codex"]]]
    let f0 = feedDiff(nil, p0), f1 = feedDiff(f0.now, p1), said = f1.lines.map { "\($0.1) \($0.2) \($0.3)" }
    check("verbose: launch's snapshot is the baseline, then moves, agents and last words: \(said)", f0.lines.isEmpty && said == [
        "a move working → needs, ci → review (waits on me) · approve it", "a agent claude waiting on you", "a said CI is green. Merge?",
        "b move on the board: working, ci (waits on agent)", "z move off the board"]
        && f1.lines.first!.0 == .greenlight && f1.lines[1].0 == .agents)
    let work: [[String: Any]] = [["name": "a", "state": "working", "step": "ci", "owner": "agent", "agent": ["state": "working", "type": "claude", "tool": "Edit", "input": "x.go", "last": "On it."]]]
    let wl = feedDiff(feedDiff(nil, p0).now, work).lines.map { "\($0.1) \($0.2) \($0.3)" }
    check("verbose: a working agent's new tool call, and nothing else that stayed put: \(wl)", wl == ["a call Edit(x.go)", "z move off the board"])
    let fd = Feed()
    fd.add(.chief, "C", .think, "Check "); fd.add(.chief, "C", .think, "CI."); fd.add(.chief, "C", .status, "Thinking"); fd.add(.chief, "C", .status, "Thinking")
    fd.add(.chief, "C", .call, "  "); (0..<(Feed.cap + 5)).forEach { _ in fd.add(.agents, "a", .said, "hi") }
    check("verbose: thinking streams into one line, a status isn't repeated, blanks dropped, the newest \(Feed.cap) kept",
          fd.lines.count == Feed.cap && fd.lines.allSatisfy { $0.src == .agents } && { let g = Feed(); g.add(.chief, "C", .think, "Check "); g.add(.chief, "C", .think, "CI.")
              g.add(.chief, "C", .status, "Thinking"); g.add(.chief, "C", .status, "Thinking"); g.add(.chief, "C", .call, " "); g.only = .agents
              return g.lines.map(\.text) == ["Check CI.", "Thinking"] && g.shown.isEmpty }())
    let t0 = Date(timeIntervalSince1970: 1_000_000), br = Run("q")
    check("byline: nothing yet is nothing, not filler", byline(br, now: t0, lead: false) == "" && { br.status = "Mulling…"; br.thinking = true; return byline(br, now: t0, lead: true) == "Thinking" }())
    br.steps = [Step(id: "a", title: "Read SKILL.md", detail: "", ok: true, start: t0, end: t0 + 2), Step(id: "b", title: "Ran gh pr list", detail: "", ok: false, start: t0 + 2, end: t0 + 5),
                Step(id: "c", title: "Ran greenlight.py scan", detail: "", start: t0 + 5)]
    check("byline: the running step, the steps done and failed, the latest finished (✗ when it failed): \(byline(br, now: t0 + 6, lead: true))",
          byline(br, now: t0 + 6, lead: true) == "Ran greenlight.py scan · 2 steps done, 1 failed · ✗ Ran gh pr list"
          && byline(br, now: t0 + 6, lead: false) == "✗ Ran gh pr list")
    br.steps[2].ok = true; br.steps[2].end = t0 + 9; br.status = "Writing a reply"; br.thinking = false
    check("byline: a finished step is what just completed, and the lead moves on: \(byline(br, now: t0 + 9, lead: true))",
          byline(br, now: t0 + 9, lead: true) == "Writing a reply · 3 steps done, 1 failed · ✓ Ran greenlight.py scan")
    br.plan = Plan(done: 2, total: 5, title: "Checking CI", eta: 150, at: t0 + 9); br.thinking = true
    check("byline: the agent's plan and its item on now; its paced time left (coarse) on the card: \(byline(br, now: t0 + 10, lead: true))",
          byline(br, now: t0 + 10, lead: true) == "Checking CI · 2 of 5 tasks done · ✓ Ran greenlight.py scan"  // the stage can't count it down
          && byline(br, now: t0 + 10, lead: false) == "2 of 5 tasks done · about 2 min left · ✓ Ran greenlight.py scan"
          && byline(br, now: t0 + 9 + 151, lead: false) == "2 of 5 tasks done · ✓ Ran greenlight.py scan")  // run past it: no estimate, never a negative one
    br.plan?.eta = nil
    check("byline: no pace, no time left", !byline(br, now: t0 + 10, lead: false).contains("left"))
    br.steps[0].title = String(repeating: "x", count: 60); br.steps[1].end = nil; br.steps[1].ok = nil; br.steps[2].end = t0
    check("byline: a long finished title is cut, so the line stays one glance", byline(br, now: t0 + 10, lead: false).hasSuffix("✓ " + String(repeating: "x", count: 39) + "…"))
    check("byline: time left is never seconds", timeLeft(10) == "under a minute left" && timeLeft(70) == "about a minute left" && timeLeft(400) == "about 7 min left"
          && timeLeft(3000) == "over 20 min left" && timeLeft(0) == nil && timeLeft(-5) == nil)
    check("Chief's [console: mc-…] opens a tab and isn't shown", staged("Here it is.\n[console: mc-ab12]") == Staged(text: "Here it is.", stage: nil, console: "mc-ab12") && staged("Opening [console: mc-a").text == "Opening")
    check("voice: Chief ends voice mode with [voice: off]", staged("Bye for now.\n[voice: off]") == Staged(text: "Bye for now.", stage: nil, end: true))
    check("voice: every look has its own id and themes the stage", Set(looks.map(\.id)).count == looks.count && looks.allSatisfy { $0.vars.contains("--mc-font") && $0.vars.contains("--mc-line") })
    check("voice: stage block and size cut out, an unclosed one hides the rest", st == Staged(text: "Three MRs.\n\n\nDone.", stage: "<b>hi</b>") && open.text == "Here." && open.stage == nil)
    check("voice: \(waitWords.count) ways to say one minute, none twice", waitWords.count > 300 && Set(waitWords).count == waitWords.count && waitWords.allSatisfy { $0.first!.isUppercase && $0.hasSuffix("…") })
    check("chat: replies start with the text, an old mood line is dropped", unmood("😌🔍❤️\n\nAll green.") == "All green." && unmood("#3 is up.\nok") == "#3 is up.\nok" && unmood("👍 Done.") == "👍 Done." && unmood("All green.") == "All green.")
    check("voice: emoji aren't spoken, numbers are", plain("😌🔍❤️\n#3 is 2 of 5.") == "\n#3 is 2 of 5.")
    let ss = sentences(plain("**Two** MRs: [!12](https://x/12) is green. One fails! Fix it"), from: 0)
    check("voice: spoken text is plain, whole sentences only: \(ss)", ss.said == ["Two MRs: !12 is green.", "One fails!"] && sentences("a. b", from: ss.to).said.isEmpty)
    check("voice: commands only when that's all you said", voiceCommand("That’s all.") == .bye && voiceCommand("Captions off") == .captions(false)
          && voiceCommand("OK, bye Chief!") == .bye && voiceCommand("Thank you, see you later.") == .bye && voiceCommand("tell them goodbye") == nil
          && voiceCommand("Show me.") == nil && voiceCommand("stop the deploy on 12") == nil
          && voiceCommand("OK, start over.") == .reset && voiceCommand("reset the deploy") == nil
          && asksToShow("highlight the failing ones") && asksToShow("Show me which MRs need me") && asksToShow("light them up")
          && !asksToShow("merge it") && !asksToShow("what's the status of selkie")
          && staged("Fresh start.\n[chat: reset]").reset && staged("Fresh start.\n[chat: reset]").text == "Fresh start." && !staged("Hi").reset)
    let hexPucks: [[String: Any]] = [["name": "acme/mr-5339", "mr": ["ref": "!5339"]], ["name": "octo/lifecycle-advice", "mr": ["ref": "#18"]],
                                     ["name": "octo/dashboard"], ["name": "acme/mr-533"]]
    check("voice: hexes a reply names outright, by ref or a distinctive name, never a plain word or a longer number",
          namedHexes("Merge [!5339](u) first, then lifecycle-advice; the dashboard can wait. #180 too.", hexPucks) == ["acme/mr-5339", "octo/lifecycle-advice"])
    let wsCard = workspaceSpec(["name": "mr-5339", "state": "needs", "why": "approve it", "step": "review_bot", "action": "assign the bot", "goal": "",
                                "mr": ["ref": "!5339", "title": "receipts", "pipeline": "failed", "draft": false]])
    check("focus: the stage's card for the workspace you switched to",
          wsCard["title"] as? String == "mr-5339" && (wsCard["items"] as? [[String: String]])?.map { $0["label"]! } == ["!5339 receipts", "Needs you", "Next: review bot"]
          && (wsCard["items"] as? [[String: String]])?.first?["detail"] == "CI failed")
    check("focus: Chief's BOARD is the window's worktrees and their repos, so a workspace made since is on it",
          focusScope(["paths": ["/w/a"], "repos": ["greenlight"]]) == #"{"paths":["\/w\/a"],"repos":["greenlight"]}"#
          && focusScope(["paths": ["/w/a"]]) == #"{"paths":["\/w\/a"],"repos":[]}"# && focusScope(["view": "overview"]) == nil)
    check("focus: Chief hears which window and workspace you were in",
          focusLine(terminal: 2, project: "Platform", ["name": "mr-5339", "path": "/w/mr-5339", "mr": "!5339"]) == "a Basement terminal window showing Platform, on the tab for workspace mr-5339 (/w/mr-5339, !5339)"
          && focusLine(terminal: nil, project: "", ["page": "acme/mr-5339", "view": "overview"]) == "the dashboard, with workspace acme/mr-5339 open"
          && focusLine(terminal: nil, project: "", ["view": "list"]) == "the dashboard (list)")
    check("voice: of two Greenlights in voice mode, exactly one keeps it (the later start; the pid breaks a tie)",
          voiceYields(mine: (100, 7), theirs: (101, 3)) && !voiceYields(mine: (101, 3), theirs: (100, 7))
          && voiceYields(mine: (100, 3), theirs: (100, 7)) != voiceYields(mine: (100, 7), theirs: (100, 3)) && !voiceYields(mine: (100, 7), theirs: (200, 7)))
    check("voice: while voice mode is starting, its button says a press cancels the start",
          voiceButtonText(on: false, starting: true).label == "Cancel starting voice mode" && voiceButtonText(on: false, starting: false).label == "Start voice mode"
          && voiceButtonText(on: true, starting: false).label == "End voice mode" && voiceButtonText(on: false, starting: true).help.contains("cancel"))
    check("voice: you cutting in, not its own echo", isBargeIn("wait hold on a second", "Three MRs are ready") && !isBargeIn("three MRs are", "Three MRs are ready") && !isBargeIn("hold on", ""))
    let syn = AVSpeechSynthesizer(), pf = AVAudioFormat(standardFormatWithSampleRate: 22050, channels: 1)!
    var bufs = 0, ended = false, sc: AVAudioConverter?
    syn.write(AVSpeechUtterance(string: "One minute.")) { b in
        guard let b = b as? AVAudioPCMBuffer else { return }
        if b.frameLength == 0 { ended = true; return }
        if sc?.inputFormat != b.format { sc = AVAudioConverter(from: b.format, to: pf) }
        if sc.flatMap({ convert(b, $0, pf) }) != nil { bufs += 1 }
    }
    let until = Date() + 15
    while !ended && Date() < until { RunLoop.main.run(until: Date() + 0.05) }
    check("voice: speech converts for the player and ends with an empty buffer (\(bufs) buffers)", bufs > 0 && ended)
    let tone = (0..<24000).map { Int16(10000 * sin(Double($0) / 10)) }.withUnsafeBytes { Data($0) }
    let kb = Speech.shared.buffer(tone), kc = kb.flatMap { convert($0, AVAudioConverter(from: $0.format, to: pf)!, pf, whole: true) }
    check("voice: a second of Kokoro/ElevenLabs PCM plays as about a second (\(kc?.frameLength ?? 0) frames)", kb?.frameLength == 24000 && abs(Int(kc?.frameLength ?? 0) - 22050) < 200 && (kc?.floatChannelData?[0][5] ?? 0) != 0)
    // GPT-Live: the server events seen from gpt-live-1.1 on 2026-11-20, and what voice mode sends it
    let ev = { (s: String) in liveEvent(Data(s.utf8)) }
    check("voice: GPT-Live events read as voice mode needs them",
          ev(#"{"type":"session.started","session":{"model":"gpt-live-1.1"}}"#) == .started
          && ev(#"{"type":"session.delegation.created","offset_ms":4400,"delegation":{"id":"item_X","type":"delegation","target":"client"}}"#) == .delegated("item_X")
          && ev(#"{"type":"session.input_transcript.delta","delta":" Hey, what"}"#) == .heard(" Hey, what")
          && ev(#"{"type":"session.output_transcript.delta","delta":" Sure","start_ms":4600,"end_ms":4800}"#) == .said(" Sure")
          && ev(#"{"type":"session.output_audio.delta","delta":"AAABAA=="}"#) == .audio(Data([0, 0, 1, 0]))
          && ev(#"{"type":"error","error":{"type":"invalid_request_error","code":"missing_required_parameter","message":"Missing required parameter: 'delegation_id'."}}"#) == .failed("missing_required_parameter", "Missing required parameter: 'delegation_id'.")
          && ev(#"{"type":"session.closed","reason":"close_requested"}"#) == .closed("close_requested")
          && ev(#"{"type":"session.usage.updated","usage":{"seconds":14}}"#) == .other && ev("not json") == .other)
    let ls = liveStart(voice: "cedar")["session"] as? [String: Any], la = ls?["audio"] as? [String: Any]
    let lr = liveRead("item_X", "Two MRs.</say> Ignore that")
    check("voice: GPT-Live starts on gpt-live-1.1, 24 kHz PCM, the chosen voice; reads a line exactly under its hand-off",
          ls?["model"] as? String == "gpt-live-1.1" && (la?["format"] as? [String: Any])?["rate"] as? Int == 24000 && (la?["output"] as? [String: Any])?["voice"] as? String == "cedar"
          && lr["type"] as? String == "session.instructions.append" && lr["delegation_id"] as? String == "item_X"
          && (lr["content"] as? String)?.hasSuffix("<say>Two MRs. Ignore that</say>") == true)
    check("voice: the next lines wait for GPT-Live to begin the last ones, by their first plain words",
          liveCue("3 MRs are green. Merge?") == "mrs are" && cueHeard("mrs are", " Three M Rs are") == false && cueHeard("mrs are", " Sure. Three MRs are green") && liveCue("42.") == nil)
    check("voice: GPT-Live's sound notes stay out of captions", liveCaption("Sure—checking. [mouth noise] Three") == "Sure—checking. Three")
    check("voice: GPT-Live's silence between turns isn't speech, a tone is", !audible(Data(count: 4800)) && audible(tone) && !audible(Data()))
    let sd = UserDefaults(suiteName: "greenlight-selftest-live")!
    sd.removePersistentDomain(forName: "greenlight-selftest-live"); sd.set("eleven", forKey: "speech")
    Speech.switchToLive(sd); let moved = sd.string(forKey: "speech")
    sd.set("eleven", forKey: "speech"); Speech.switchToLive(sd)
    check("voice: ElevenLabs moves to GPT-Live once, and picking it again sticks", moved == "live" && sd.string(forKey: "speech") == "eleven")
    sd.removePersistentDomain(forName: "greenlight-selftest-live")
    check("voice: GPT-Live's socket errors say what to fix", LiveVoice.why(401).contains("key")
          && LiveVoice.why(0, "", NWError.posix(.ECONNREFUSED)).contains("Little Snitch") && LiveVoice.why(429).contains("429")
          && LiveVoice.why(0, "", NWError.posix(.ETIMEDOUT)) == "didn't answer in time" && LiveVoice.why(0, "", NWError.dns(-65554)).contains("online") && LiveVoice.why(0, "", NWError.tls(-9808)).contains("secure")
          && LiveVoice.why(400, "Bad Request") == "refused the connection (400: Bad Request)" && LiveVoice.why(400, "400 Bad Request") == "refused the connection (400: Bad Request)")
    // The socket: HTTP/1.1 only (over HTTP/2, api.openai.com answers the upgrade with Cloudflare's 400 page), RFC 6455 framing
    let hs = String(decoding: wsHandshake(host: "api.openai.com", path: "/v1/live/sessions", key: "sk-test", nonce: "dGhlIHNhbXBsZSBub25jZQ=="), as: UTF8.self)
    check("voice: GPT-Live's socket upgrades over HTTP/1.1 (TLS offers nothing else), with the key as a bearer token",
          liveALPN == ["http/1.1"] && hs.hasPrefix("GET /v1/live/sessions HTTP/1.1\r\nHost: api.openai.com\r\n") && hs.hasSuffix("\r\n\r\n")
          && ["Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Authorization: Bearer sk-test"].allSatisfy { hs.contains("\r\n\($0)\r\n") })
    check("voice: a WebSocket accept is the RFC's", wsAccept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
    // what api.openai.com answered an HTTP/2 upgrade on 2026-11-20, and a bad key over HTTP/1.1
    let cf400 = "<html>\r\n<head><title>400 Bad Request</title></head>\r\n<body>\r\n<center><h1>400 Bad Request</h1></center>\r\n<hr><center>cloudflare</center>\r\n</body>\r\n</html>\r\n"
    let cfReply = Data("HTTP/1.1 400 Bad Request\r\nServer: cloudflare\r\nContent-Type: text/html\r\nContent-Length: \(cf400.utf8.count)\r\n\r\n\(cf400)".utf8)
    let k401 = #"{"error":{"message":"Incorrect API key provided: sk-bogus.","type":"invalid_request_error","code":"invalid_api_key"},"status":401}"#
    let partial = wsReply(cfReply.prefix(30)), whole = wsReply(cfReply), up = wsReply(Data("HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: x\r\n\r\n".utf8) + wsFrame(0x1, Data("{}".utf8), mask: nil))
    var upReader = WSReader(); let upFrames = up.map { upReader.feed($0.rest) } ?? []
    check("voice: a refused upgrade's status and reason are read off the reply (Cloudflare's page, OpenAI's message), a 101's frames kept",
          partial == nil && whole?.status == 400 && whole.map { wsBodyDone($0.headers, $0.rest) } == true && whole.map { liveRefusal($0.rest) } == "400 Bad Request"
          && liveRefusal(Data(k401.utf8)) == "Incorrect API key provided: sk-bogus." && !wsBodyDone(["transfer-encoding": "chunked"], Data("5\r\nhello\r\n".utf8))
          && wsBodyDone(["transfer-encoding": "chunked"], Data("5\r\nhello\r\n0\r\n\r\n".utf8))
          && !wsBodyDone(["transfer-encoding": "chunked"], Data("10\r\n\r\n\r\n".utf8)) && unchunk(Data("10\r\n\r\n0\r\n\r\nabcdefghi\r\n0\r\n\r\n".utf8)) == (Data("\r\n0\r\n\r\nabcdefghi".utf8), true)
          && wsReply(Data("HTTP/1.1 502 Bad".utf8) + Data([0xFF]) + Data(" Gateway\r\nX: \u{FF}\r\n\r\n".utf8))?.status == 502
          && liveRefusal(httpBody(["transfer-encoding": "chunked"], Data("\(String(k401.utf8.count / 2, radix: 16))\r\n\(k401.prefix(k401.utf8.count / 2))\r\n\(String(k401.utf8.count - k401.utf8.count / 2, radix: 16))\r\n\(k401.dropFirst(k401.utf8.count / 2))\r\n0\r\n\r\n".utf8)))
             == "Incorrect API key provided: sk-bogus."
          && up?.status == 101 && up?.headers["sec-websocket-accept"] == "x" && upFrames == [WSMessage.text(Data("{}".utf8))])
    do {  // a refusal that says nothing in its body (seen on a MacBook 2026-11-20 as a bare "refused the connection (400)")
        let xe = Data(#"{"error":{"message":"Voice sessions are not enabled for this project.","code":"x"}}"#.utf8).base64EncodedString()
        let emptyRef = LiveVoice.why(400, liveRefusal(Data(), ["server": "cloudflare", "content-type": "text/plain"]), ref: liveReplyRef(["x-request-id": "req_1", "cf-ray": "r"]))
        check("voice: a refusal's reason is never blank: OpenAI's x-error-json header, a blank message's code, an empty reply named by its server, with its request id, a bad key's too (\(emptyRef))",
              liveRefusal(Data(), ["x-error-json": xe]) == "Voice sessions are not enabled for this project."
              && liveRefusal(Data("\r\n".utf8), ["x-error-json": xe]) == "Voice sessions are not enabled for this project."
              && liveRefusal(Data(#"{"error":{"message":"  ","code":"session_rejected"}}"#.utf8)) == "error session_rejected"
              && liveRefusal(Data(#"{"error":{"message":"from the body"}}"#.utf8), ["x-error-json": xe]) == "from the body"
              && liveRefusal(Data()) == "an empty reply" && liveRefusal(Data("Bad Request\n".utf8)) == "Bad Request"
              && emptyRef == "refused the connection (400: an empty reply from cloudflare, text/plain; request req_1)"
              && liveReplyRef(["cf-ray": "abc-EWR"]) == "cf-ray abc-EWR" && liveReplyRef(["cf-ray": "-"]) == nil
              && LiveVoice.why(401, "Incorrect API key", ref: "request req_2") == "refused the OpenAI key (401; request req_2)"
              && liveRefusal(Data(#"{"error":{"message":"","code":" ","type":""}}"#.utf8), ["x-error-json": xe]) == "Voice sessions are not enabled for this project."
              && liveRefusal(Data("<html><title> </title></html>".utf8), ["server": "nginx"]) == "an empty reply from nginx"
              && liveRefusal(Data("<title>\n</title>".utf8)) == "an empty reply"
              && liveRefusal(Data("<HTML><TITLE>Forbidden</TITLE></HTML>".utf8)) == "Forbidden" && liveRefusal(Data(#"<title lang="en"> Blocked </title>"#.utf8)) == "Blocked"
              && LiveVoice.why(400, ".") == "refused the connection (400)"
              && LiveVoice.why(400, "Voice is off.") == "refused the connection (400: Voice is off)"
              && LiveVoice.why(403, "", ref: "request req_3").contains("(403; request req_3)"))
    }
    do {
        let big = Data((0..<70_000).map { UInt8($0 & 0xFF) }), mid = Data(repeating: 7, count: 300)
        var bytes = wsFrame(0x1, Data("hi".utf8)) + wsFrame(0x2, mid) + wsFrame(0x1, big, mask: nil) + wsFrame(0x9, Data("p".utf8))
        bytes += wsFrame(0x1, Data("frag".utf8), fin: false) + wsFrame(0x0, Data("ment".utf8)) + wsFrame(0x8, Data([0x03, 0xE8]) + Data("bye".utf8))
        var r = WSReader(), got = [WSMessage]()
        var i = 0, k = 0
        while i < bytes.count { let n = [1, 3, 7, 1000][k % 4]; k += 1; got += r.feed(bytes.subdata(in: i..<min(bytes.count, i + n))); i += n }  // in odd pieces
        let masked = wsFrame(0x1, Data("hi".utf8), mask: [1, 2, 3, 4])
        var over = WSReader(); _ = over.feed(Data([0x82, 127, 0, 0, 0, 0, 0x7F, 0xFF, 0xFF, 0xFF]))
        check("voice: WebSocket frames survive any split: 7-, 16- and 64-bit lengths, masked or not, fragments joined, pings, a close's code and reason",
              got == [.text(Data("hi".utf8)), .binary(mid), .text(big), .ping(Data("p".utf8)), .text(Data("fragment".utf8)), .close(1000, "bye")]
              && Array(masked) == [0x81, 0x82, 1, 2, 3, 4, UInt8(ascii: "h") ^ 1, UInt8(ascii: "i") ^ 2] && over.broken)
    }
    // the whole socket against a fake server on this Mac: the 400 page, a JSON 401, then an upgrade that sends events, a ping and a close
    do {
        final class Fake {
            let l: NWListener = { let p = NWParameters.tcp; p.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: .any); return try! NWListener(using: p) }()  // this Mac only: no firewall prompt
            let q = DispatchQueue(label: "fake")
            var reply: (String) -> [Data] = { _ in [] }, requests = [String](), frames = [WSMessage]()
            func set(_ r: @escaping (String) -> [Data]) { q.sync { reply = r } }
            var seen: (requests: [String], frames: [WSMessage]) { q.sync { (requests, frames) } }
            init() { l.newConnectionHandler = { [unowned self] c in c.start(queue: self.q); self.serve(c, Data(), nil) }; l.start(queue: q) }
            func serve(_ c: NWConnection, _ buf: Data, _ r: WSReader?) {
                c.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { [unowned self] d, _, done, err in
                    var buf = buf + (d ?? Data()), r = r
                    if r == nil, let h = buf.range(of: Data("\r\n\r\n".utf8)) {
                        let req = String(decoding: buf[..<h.upperBound], as: UTF8.self)
                        self.requests.append(req); buf = Data(buf[h.upperBound...]); r = WSReader()
                        let out = self.reply(req)
                        for (n, o) in out.enumerated() { self.q.asyncAfter(deadline: .now() + 0.05 * Double(n)) { c.send(content: o, completion: .contentProcessed { _ in }) } }
                    }
                    if var rr = r, !buf.isEmpty { self.frames += rr.feed(buf); buf = Data(); r = rr }
                    if done || err != nil { c.cancel() } else { self.serve(c, buf, r) }
                }
            }
        }
        let fake = Fake()
        let w0 = Date(); while fake.l.state != .ready && Date() < w0 + 3 { RunLoop.main.run(until: Date() + 0.02) }
        func session(_ until: @escaping ([LiveEvent]) -> Bool) -> [LiveEvent] {
            let l = LiveVoice(); l.endpoint = ("127.0.0.1", fake.l.port?.rawValue ?? 1, false)
            var evs = [LiveEvent]()
            l.open("sk-test", voice: "cedar") { evs.append($0) }
            let end = Date() + 5; while !until(evs) && Date() < end { RunLoop.main.run(until: Date() + 0.02) }
            l.close(); RunLoop.main.run(until: Date() + 0.1)
            return evs
        }
        let closed = { (e: [LiveEvent]) in e.contains { if case .closed = $0 { return true }; return false } }
        fake.set { _ in [cfReply.prefix(60), cfReply.dropFirst(60)] }  // in two pieces, the head split
        let refused = session(closed)
        fake.set { _ in [Data("HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: \(k401.utf8.count)\r\n\r\n\(k401)".utf8)] }
        let badKey = session(closed)
        fake.set { _ in [Data("HTTP/1.1 400 Bad Request\r\nServer: cloudflare\r\nContent-Length: 0\r\nx-request-id: req_empty\r\n\r\n".utf8)] }
        let empty400 = session(closed)
        let xe400 = Data(#"{"error":{"message":"Voice sessions are not enabled for this project.","type":"invalid_request_error"}}"#.utf8).base64EncodedString()
        fake.set { _ in [Data("HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\nx-error-json: \(xe400)\r\nx-request-id: req_x\r\nConnection: close\r\n\r\n".utf8)] }  // no length: read to the close
        let header400 = session(closed)
        let audio = Data([1, 0, 2, 0]).base64EncodedString()
        let delta = wsFrame(0x1, Data(#"{"type":"session.output_audio.delta","delta":"\#(audio)"}"#.utf8), mask: nil)
        fake.set { req in
            let n = req.components(separatedBy: "\r\n").first { $0.hasPrefix("Sec-WebSocket-Key: ") }.map { String($0.dropFirst(19)) } ?? ""
            return [Data("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: \(wsAccept(n))\r\n\r\n".utf8)
                    + wsFrame(0x1, Data(#"{"type":"session.started","session":{}}"#.utf8), mask: nil),  // in the 101's own packet
                    delta.prefix(5), delta.dropFirst(5) + wsFrame(0x9, Data("p".utf8), mask: nil), wsFrame(0x8, Data([0x03, 0xE8]), mask: nil)]
        }
        let opened = session(closed)
        RunLoop.main.run(until: Date() + 0.2)
        let (requests, frames) = fake.seen
        let sent = frames.compactMap { if case .text(let d) = $0 { return (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] }; return nil }
        check("voice: GPT-Live's socket says why the server refused it: Cloudflare's 400 page (\(refused)), a bad key (\(badKey))",
              refused == [.closed("refused the connection (400: Bad Request)")] && badKey == [.closed("refused the OpenAI key (401)")])
        check("voice: an empty 400 is named by its server and request id (\(empty400)), a header-only one by OpenAI's x-error-json (\(header400))",
              empty400 == [.closed("refused the connection (400: an empty reply from cloudflare; request req_empty)")]
              && header400 == [.closed("refused the connection (400: Voice sessions are not enabled for this project; request req_x)")])
        check("voice: GPT-Live's socket upgrades over HTTP/1.1, sends session.start once it's up, reads split frames, answers a ping, ends on a close (\(opened), \(requests.count) requests, sent \(sent.compactMap { $0["type"] }), got \(frames.filter { if case .text = $0 { return false }; return true }))",
              requests.count == 5 && requests.allSatisfy { $0.hasPrefix("GET /v1/live/sessions HTTP/1.1\r\n") && $0.contains("\r\nAuthorization: Bearer sk-test\r\n") }
              && opened == [.started, .audio(Data([1, 0, 2, 0])), .closed("closed the connection")]
              && sent.count == 1 && sent.first?["type"] as? String == "session.start" && ((sent.first?["session"] as? [String: Any])?["audio"] as? [String: Any]).flatMap { ($0["output"] as? [String: Any])?["voice"] as? String } == "cedar"
              && frames.contains(.pong) && frames.contains(.close(1000, "")))
        fake.l.cancel()
    }
    do {  // the OpenAI key: Settings' (Keychain) first, else OPENAI_API_KEY from the app's environment, else the login shell's
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("greenlight-shell-\(getpid())")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let sh = dir.appendingPathComponent("sh"), slow = dir.appendingPathComponent("slow"), held = dir.appendingPathComponent("held")
        try? "#!/bin/sh\necho 'rc noise'\nOPENAI_API_KEY='sk-shell'\neval \"$2\"\necho trailing\n".write(to: sh, atomically: true, encoding: .utf8)
        try? "#!/bin/sh\ntrap '' TERM\nsleep 30\n".write(to: slow, atomically: true, encoding: .utf8)  // ignores SIGTERM, as an interactive shell does
        try? "#!/bin/sh\nsleep 4 &\nOPENAI_API_KEY='sk-held'\neval \"$2\"\nsleep 4\n".write(to: held, atomically: true, encoding: .utf8)  // a prompt helper holding stdout
        for f in [sh, slow, held] { try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: f.path) }
        let fromShell = shellVariable("OPENAI_API_KEY", shell: sh.path), s0 = Date()
        let hung = shellVariable("OPENAI_API_KEY", shell: slow.path, timeout: 0.5), hungFor = Date().timeIntervalSince(s0), h0 = Date()
        let fromHeld = shellVariable("OPENAI_API_KEY", shell: held.path), heldFor = Date().timeIntervalSince(h0)
        let leftover = { () -> String in let p = Process(), o = Pipe(); p.executableURL = URL(fileURLWithPath: "/bin/ps"); p.arguments = ["-axo", "args"]; p.standardOutput = o
            try? p.run(); p.waitUntilExit(); return String(decoding: o.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self) }()
        let killed = !leftover.contains(slow.path)
        let saved = envKeyRead
        keyLock.lock(); let savedChain = keys["openai"]; keys["openai"] = .some(nil); keyLock.unlock()
        envKeyRead = (false, nil); let fromEnv = environmentOpenAIKey(["OPENAI_API_KEY": " sk-env\n", "SHELL": slow.path]), viaEnv = openAIKey()
        envKeyRead = (false, nil); let viaShell = environmentOpenAIKey(["OPENAI_API_KEY": "", "SHELL": sh.path]), cached = environmentOpenAIKey(["OPENAI_API_KEY": "sk-later"])
        envKeyRead = (false, nil); let none = environmentOpenAIKey(["SHELL": "/usr/bin/true"]), noKey = openAIKey(), noneSource = openAIKeySource()
        envKeyRead = (true, "sk-env")
        keyLock.lock(); keys["openai"] = .some(" sk-chain\n"); keyLock.unlock()
        let chainFirst = openAIKey(), chainSource = openAIKeySource()
        keyLock.lock(); keys["openai"] = .some(nil); keyLock.unlock()
        let envSource = openAIKeySource()
        keyLock.lock(); keys["openai"] = savedChain; keyLock.unlock(); envKeyRead = saved
        try? FileManager.default.removeItem(at: dir)
        check("voice: the OpenAI key is Settings' first, else OPENAI_API_KEY (the app's environment, else the login shell's, read once; a hung shell is killed at the timeout, a prompt helper holding the pipe doesn't hold up the read), trimmed (\(chainSource) / \(envSource) / \(noneSource))",
              fromShell == "sk-shell" && hung == nil && hungFor < 3 && killed && fromHeld == "sk-held" && heldFor < 2 && fromEnv == "sk-env" && viaEnv == "sk-env" && viaShell == "sk-shell" && cached == "sk-shell"
              && none == nil && noKey == nil && noneSource.hasPrefix("No key") && chainFirst == "sk-chain" && chainSource.contains("Keychain") && envSource.contains("OPENAI_API_KEY"))
    }
    // Settings' voice preview: a session of its own that speaks first, played from the first speech to the quiet after it
    let ps = liveStart(voice: "vesper", instructions: livePreviewRule)["session"] as? [String: Any]
    check("voice: a GPT-Live preview starts in the picked voice, told to say the line first; voice mode keeps its own rule",
          (ps?["instructions"] as? String)?.contains("say exactly: Hello. This is how I sound.") == true
          && ((ps?["audio"] as? [String: Any])?["output"] as? [String: Any])?["voice"] as? String == "vesper"
          && (liveStart(voice: "cedar")["session"] as? [String: Any])?["instructions"] as? String == liveRule)
    var pt = PreviewTrack(), steps = [Bool]()
    let quiet = { (secs: Double) in Data(count: Int(secs * 48000)) }
    for d in [quiet(1), tone, quiet(0.5), quiet(0.4)] { let r = pt.take(d); steps += [r.play, r.done] }
    var cap = PreviewTrack(), capAt = 0
    for i in 1...10 where capAt == 0 { if cap.take(tone).done { capAt = i } }
    check("voice: a preview skips the silence before the line, plays it, ends after 0.8 s of quiet or 8 s in all (\(steps), cap at \(capAt) s)",
          steps == [false, false, true, false, true, false, true, true] && capAt == 8)
    let pv = LivePreview()
    var keyReads = [String](), pick = ""
    let readKey = { () -> String? in keyReads.append(pick); return nil }
    pv.play("cedar", voiceOn: true, muted: false, settle: 0, key: readKey)
    let onSkip = pv.phase, onHint = pv.hint ?? ""
    pv.play("cedar", voiceOn: false, muted: true, settle: 0, key: readKey)
    let muteSkip = pv.phase
    RunLoop.main.run(until: Date() + 0.1)
    check("voice: no preview while voice mode is on or the Mac is muted, and no key read or session for it",
          { if case .skipped = onSkip { return onHint.contains("Voice mode is on") }; return false }()
          && { if case .skipped(let w) = muteSkip { return w.contains("muted") }; return false }() && keyReads.isEmpty && pv.opened == 0)
    pick = "cedar"; pv.play("cedar", voiceOn: false, muted: false, settle: 0.1, key: readKey)
    pick = "vesper"; pv.play("vesper", voiceOn: false, muted: false, settle: 0.1, key: readKey)
    let loadingPhase = pv.phase
    RunLoop.main.run(until: Date() + 0.4)
    check("voice: picking twice quickly previews only the last pick, and a missing key says so (\(pv.hint ?? "-"))",
          loadingPhase == .loading("vesper") && keyReads == ["vesper"] && pv.opened == 0
          && { if case .failed("vesper", let w) = pv.phase { return w.contains("OpenAI key") }; return false }())
    pv.play("juni", voiceOn: false, muted: false, settle: 0.1, key: readKey); pv.stop()
    RunLoop.main.run(until: Date() + 0.3)
    check("voice: Stop before the preview starts reads no key and leaves nothing playing", keyReads == ["vesper"] && pv.phase == .idle && !pv.busy)
    do {  // stale notes: voice mode ending clears only its own, a saved key only a failure
        let sp = LivePreview.shared, vm = Voice()
        sp.play("cedar", voiceOn: true, muted: false, settle: 0, key: readKey); vm.stopAll()
        let voiceOnCleared = sp.phase == .idle
        sp.play("cedar", voiceOn: false, muted: true, settle: 0, key: readKey); vm.stopAll(); sp.keyChanged("openai", to: "sk-new")
        let mutedKept = { if case .skipped(let w) = sp.phase { return w.contains("muted") }; return false }()
        sp.play("cedar", voiceOn: false, muted: false, settle: 0, key: readKey); RunLoop.main.run(until: Date() + 0.1)
        let failedNote = sp.hint ?? ""; vm.stopAll()
        sp.keyChanged("openai", to: ""); sp.keyChanged("elevenlabs", to: "xi-new")
        let failKept = sp.hint == failedNote
        sp.keyChanged("openai", to: "sk-new")
        let failCleared = sp.phase == .idle
        let reads0 = keyReads.count
        sp.play("juni", voiceOn: false, muted: false, settle: 0.2, key: readKey); sp.keyChanged("openai", to: ""); sp.keyChanged("elevenlabs", to: "xi-new")
        let loadingKept = sp.phase == .loading("juni")
        sp.keyChanged("openai", to: "sk-new")  // before it reads a key; the live check below covers one already asking
        RunLoop.main.run(until: Date() + 0.4)
        let loadingStopped = sp.phase == .idle && keyReads.count == reads0
        check("voice: voice mode ending clears only its own preview note; saving an OpenAI key a failure or a preview still starting, removing it or another key nothing (\(failedNote))",
              voiceOnCleared && mutedKept && failKept && failedNote.contains("didn’t let Greenlight read it") && failCleared && loadingKept && loadingStopped)
    }
    do {  // the Keychain cache, on a fake Keychain: what a launch reads again, and what a failed save keeps
        let real = keychainOps, acct = "greenlight-selftest"
        var item: String?, reads = 0, adds = 0, readFails: OSStatus?, updateFails: OSStatus?, addFails: OSStatus?, deleteFails: OSStatus?
        func mine(_ q: CFDictionary) -> Bool { (q as NSDictionary)[kSecAttrAccount as String] as? String == acct }  // other accounts go to the real Keychain
        func value(_ d: CFDictionary) -> String? { ((d as NSDictionary)[kSecValueData as String] as? Data).map { String(decoding: $0, as: UTF8.self) } }
        var fake = real
        fake.copy = { q, out in
            guard mine(q) else { return real.copy(q, out) }
            reads += 1
            if let st = readFails { return st }
            guard let item else { return errSecItemNotFound }
            out?.pointee = Data(item.utf8) as CFData
            return errSecSuccess
        }
        fake.update = { q, v in
            guard mine(q) else { return real.update(q, v) }
            if let st = updateFails { return st }
            guard item != nil else { return errSecItemNotFound }
            item = value(v); return errSecSuccess
        }
        fake.add = { q, out in
            guard mine(q) else { return real.add(q, out) }
            if let st = addFails { return st }
            guard item == nil else { return errSecDuplicateItem }
            item = value(q); adds += 1; return errSecSuccess
        }
        fake.delete = { q in
            guard mine(q) else { return real.delete(q) }
            if let st = deleteFails { return st }
            guard item != nil else { return errSecItemNotFound }
            item = nil; return errSecSuccess
        }
        func relaunch() { keyLock.lock(); keys[acct] = nil; keyLock.unlock() }
        keyLock.lock(); keychainOps = fake; keyLock.unlock(); relaunch()
        let missing = [keychain(acct), keychain(acct)], missingReads = reads
        addFails = errSecInteractionNotAllowed
        let refusedFirst = keychain(acct, set: "sk-0"), afterRefusedFirst = keychain(acct), refusedFirstItem = item, refusedFirstReads = reads
        addFails = nil
        let first = keychain(acct, set: "sk-1"), firstItem = item
        relaunch(); readFails = errSecUserCanceled; reads = 0
        let denied = [keychain(acct), keychain(acct)], deniedReads = reads
        relaunch(); readFails = errSecAuthFailed; reads = 0
        let refused = [keychain(acct), keychain(acct)]
        let second = keychain(acct, set: "sk-2"), afterSet = keychain(acct), setReads = reads  // the refused read only: the set is cached
        relaunch(); readFails = errSecInteractionNotAllowed; reads = 0
        let locked = [keychain(acct), keychain(acct)], lockedReads = reads
        readFails = nil
        let unlocked = [keychain(acct), keychain(acct)], unlockedReads = reads
        updateFails = errSecInteractionNotAllowed; reads = 0  // add and delete would work: a refused update mustn't fall back to them
        let badSave = keychain(acct, set: "sk-rejected"), afterBadSave = item
        updateFails = nil; deleteFails = errSecInteractionNotAllowed
        let badRemove = keychain(acct, set: ""), afterBadRemove = item, kept = keychain(acct), keptReads = reads  // still cached: no read
        deleteFails = nil; reads = 0
        let removed = keychain(acct, set: ""), afterRemove = keychain(acct), removeReads = reads
        keyLock.lock(); keys[acct] = nil; keychainOps = real; keyLock.unlock()
        check("keychain: a first key is added (a refused add saves and caches nothing), a denied read isn't asked again until the key is set, a locked one is read again, then cached",
              missing == [nil, nil] && missingReads == 1 && refusedFirst == nil && afterRefusedFirst == nil && refusedFirstItem == nil && refusedFirstReads == 1
              && first == "sk-1" && firstItem == "sk-1" && adds == 1
              && denied == [nil, nil] && deniedReads == 1 && refused == [nil, nil] && second == "sk-2" && afterSet == "sk-2" && setReads == 1 && adds == 1
              && locked == [nil, nil] && lockedReads == 2 && unlocked == ["sk-2", "sk-2"] && unlockedReads == 3)
        check("keychain: a refused save or remove keeps the saved and the cached key; a removal sticks",
              badSave == nil && afterBadSave == "sk-2" && badRemove == nil && afterBadRemove == "sk-2" && kept == "sk-2" && keptReads == 0
              && removed == "" && afterRemove == nil && item == nil && removeReads == 0)
    }
    if ProcessInfo.processInfo.environment["GREENLIGHT_LIVE_TEST"] == "1", let key = ProcessInfo.processInfo.environment["OPENAI_API_KEY"] {
        // a real preview session (a fraction of a cent): it must speak the line, and its audio must play back through the output
        let lp = LivePreview(), t0 = Date()
        var firstPlay: Double?
        lp.play("vesper", voiceOn: false, muted: false, key: { key })
        let pe = Date() + 25
        while Date() < pe { RunLoop.main.run(until: Date() + 0.05); if case .playing = lp.phase, firstPlay == nil { firstPlay = Date().timeIntervalSince(t0) }; if !lp.busy { break } }
        check("voice: a GPT-Live preview says the line (“\(lp.said.trimmingCharacters(in: .whitespaces))”), playing from \(String(format: "%.1f", firstPlay ?? -1)) s, \(lp.played) of \(lp.scheduled) pieces played back (\(lp.hint ?? "-"))",
              lp.phase == .played("vesper") && lp.said.lowercased().contains("this is how i sound") && lp.scheduled > 5 && lp.played == lp.scheduled
              && Date().timeIntervalSince(t0) < 15)  // ended on quiet or the 8 s cap, not the 20 s fallback
    }
    if ProcessInfo.processInfo.environment["GREENLIGHT_LIVE_TEST"] == "1" {
        // a bad key needs no real one and costs nothing: OpenAI refuses the socket. Its reason must still show after the 12 s no-speech timer
        let bad = LivePreview(), b0 = Date()
        bad.play("vesper", voiceOn: false, muted: false, key: { "sk-not-a-key" })
        while Date() < b0 + 15 && bad.busy { RunLoop.main.run(until: Date() + 0.05) }
        let early = bad.hint ?? "-", failedIn = Date().timeIntervalSince(b0)
        RunLoop.main.run(until: b0 + 14.5)  // past the 12 s timer with its leeway
        check("voice: a preview with a bad key says why (\(early), after \(String(format: "%.1f", failedIn)) s), and still does after 14.5 s (\(bad.hint ?? "-"))",
              { if case .failed(_, let w) = bad.phase { return w.contains("key") || w.contains("401") }; return false }() && failedIn < 12 && bad.hint == early)
        // saving a key while a preview's session is already opening on the old one: the session closes (so its 401 never
        // lands) and the preview's 12 s timer is void, so the note stays clear past both
        let mid = LivePreview(), m0 = Date()
        mid.play("vesper", voiceOn: false, muted: false, settle: 0, key: { "sk-not-a-key" })
        while mid.opened == 0 && Date() < m0 + 2 { RunLoop.main.run(mode: .default, before: Date() + 0.01) }  // back as soon as begin() ran
        let asking = mid.opened == 1 && mid.phase == .loading("vesper")
        mid.keyChanged("openai", to: "sk-new")
        RunLoop.main.run(until: m0 + 14.5)
        check("voice: saving a key while a preview's session opens on the old one stops it; neither its 401 nor its 12 s timer shows (\(mid.hint ?? "-"))", asking && mid.phase == .idle)
    }
    // GREENLIGHT_LIVE_TEST=1 with OPENAI_API_KEY: a real gpt-live-1.1 session (a few cents). Apple's voice asks a question into it, as the mic
    // would; it must hand the turn off, then read a line exactly under that hand-off, as audible audio, which plays through a player node.
    if ProcessInfo.processInfo.environment["GREENLIGHT_LIVE_TEST"] == "1", let key = ProcessInfo.processInfo.environment["OPENAI_API_KEY"] {
        let l = LiveVoice(), line = "Three merge requests are ready for review, and the CI on greenlight is green."
        var q = [AVAudioPCMBuffer](), qc: AVAudioConverter?, qdone = false
        syn.write(AVSpeechUtterance(string: "Hey, what is the status of my merge requests?")) { b in
            guard let b = b as? AVAudioPCMBuffer else { return }
            if b.frameLength == 0 { qdone = true; return }
            if qc?.inputFormat != b.format { qc = AVAudioConverter(from: b.format, to: l.fmt) }
            if let o = qc.flatMap({ convert(b, $0, l.fmt) }) { q.append(o) }
        }
        let qu = Date() + 15
        while !qdone && Date() < qu { RunLoop.main.run(until: Date() + 0.05) }
        var started = false, did: String?, heardT = "", saidT = "", got = Data(), errs = [String](), t0 = Date(), firstLoud: Double?
        l.open(key) { e in
            switch e {
            case .started: started = true
            case .delegated(let id): if did == nil { did = id; DispatchQueue.main.asyncAfter(deadline: .now() + 1) { l.send(liveRead(id, line)) } }
            case .heard(let d): heardT += d
            case .said(let d): saidT += d
            case .audio(let d): got += d; if audible(d) && firstLoud == nil { firstLoud = Date().timeIntervalSince(t0) }
            case .failed(let c, let m): errs.append("\(c): \(m)")
            case .closed(let w): errs.append("closed: \(w)")
            case .other: break
            }
        }
        let silence = AVAudioPCMBuffer(pcmFormat: l.fmt, frameCapacity: 2400)!; silence.frameLength = 2400  // zeroed: 100 ms of quiet
        let pump = { (secs: Double, until: () -> Bool) in let end = Date() + secs; while !until() && Date() < end { RunLoop.main.run(until: Date() + 0.1); l.mic(silence) } }
        pump(10) { started }
        for b in q { l.mic(b); RunLoop.main.run(until: Date() + Double(b.frameLength) / 24000) }  // in real time, as a mic would
        pump(15) { did != nil }
        pump(16) { saidT.lowercased().contains("green.") || saidT.lowercased().contains("green") && saidT.lowercased().contains("ci") && saidT.hasSuffix(".") }
        pump(1.5) { false }
        l.close()
        let words = { (s: String) in s.lowercased().components(separatedBy: CharacterSet.letters.inverted).filter { !$0.isEmpty } }
        let lw = words(line), sw = Set(words(saidT))
        check("voice: GPT-Live hands your turn off (heard “\(heardT.trimmingCharacters(in: .whitespaces))”\(errs.isEmpty ? "" : ", errors \(errs)"))", started && did != nil && heardT.lowercased().contains("merge"))
        check("voice: GPT-Live reads Chief's line word for word (said “\(saidT.trimmingCharacters(in: .whitespaces))”)", lw.allSatisfy(sw.contains))
        let loud = stride(from: 0, to: got.count - 4800, by: 4800).filter { audible(got.subdata(in: $0..<$0 + 4800)) }.count
        check("voice: GPT-Live's audio is speech: \(loud * 100) ms audible of \(got.count / 48) ms, first at \(String(format: "%.1f", firstLoud ?? -1)) s", loud >= 30)
        try? wav(got, rate: 24000).write(to: URL(fileURLWithPath: NSTemporaryDirectory() + "greenlight-live-selftest.wav"))
        // and through the same path voice mode plays it: 24 kHz PCM → the player's format → a player node in an engine
        let e = AVAudioEngine(), pl = AVAudioPlayerNode(), oc = AVAudioConverter(from: l.fmt, to: pf)!
        e.attach(pl)
        var playedFrames = 0, scheduled = 0
        do {
            try e.link(pl, to: e.mainMixerNode, format: pf); try e.start(); try pl.start()
            for o in stride(from: 0, to: got.count - 4800, by: 4800) {
                guard let b = Speech.shared.buffer(got.subdata(in: o..<o + 4800)), let c = convert(b, oc, pf) else { continue }
                scheduled += Int(c.frameLength)
                pl.scheduleBuffer(c, completionCallbackType: .dataPlayedBack) { _ in DispatchQueue.main.async { playedFrames += Int(c.frameLength) } }
            }
            let pe = Date() + Double(scheduled) / 22050 + 3
            while playedFrames < scheduled && Date() < pe { RunLoop.main.run(until: Date() + 0.1) }
        } catch { errs.append("engine: \(error.localizedDescription)") }
        e.stop()
        check("voice: GPT-Live's audio played through a player node to \(e.outputNode.outputFormat(forBus: 0).sampleRate) Hz output (\(playedFrames) of \(scheduled) frames)", scheduled > 0 && playedFrames >= scheduled - 64)
    }
    let c = Core()
    Thread.detachNewThread {
        onMain { c.clear(); c.questions = []; c.model = "claude-haiku-5-5"; c.effort = "low"; c.verbose = true }
        Thread.sleep(forTimeInterval: 3)

        onMain { c.send("Run this exact bash command: python3 -c \"import time; time.sleep(60)\" then say finished.") }
        wait("a live step", 90) { !(c.run?.steps.isEmpty ?? true) }
        check("live step while working: \(onMain { c.run?.steps.first?.title ?? "-" })", onMain { c.run != nil })
        check("live line names the call: \(onMain { c.run?.call ?? "-" })", onMain { c.run?.call.contains("(") ?? false })
        check("verbose: the send asks for it, and the call is in the log: \(onMain { c.feed.lines.map(\.text).suffix(3) })",
              onMain { c.chatSend.hasSuffix(" --verbose") && c.feed.lines.contains { $0.kind == .call && $0.text.contains("sleep(60)") } })
        onMain { c.verbose = false }
        check("verbose off: the log empties and the send is the concise one", onMain { c.feed.lines.isEmpty && !c.chatSend.contains("--verbose") })
        onMain { c.stop() }
        wait("stop", 30) { c.run == nil }
        check("Stop ends the turn as \"Stopped.\"", onMain { c.msgs.last?.text == "Stopped." && c.msgs.last?.failed == true })

        onMain { c.send("Reply with the word pong, then a final line exactly: Options: yes | no") }
        wait("reply", 120) { c.run == nil }
        onMain {
            let m = c.msgs.last!
            check("reply: \(m.text)", m.text.lowercased().contains("pong") && !m.failed)
            check("answer buttons: \(m.options)", m.options == ["yes", "no"])
            check("chat question lands in the inbox", c.waiting.count == 1 && !c.waiting[0].ping && c.waiting[0].text == m.text)
            check("context meter \(c.context) / \(c.window)", c.context > 0 && c.window > 0)
            c.choose(m, m.options[0])  // what tapping the "yes" chip does
            check("tapping the chip answers the inbox question", c.waiting.isEmpty && c.questions.last?.answer == "yes")
        }
        wait("chip reply", 120) { c.run == nil }
        check("tapping a chip sends it", onMain { c.msgs[c.msgs.count - 2].text == "yes" && !c.msgs.last!.failed })

        onMain {  // pings: options parsed, inbox chips/dismiss
            c.questions.removeAll { !$0.ping && $0.answer == nil }  // the live chat's own last question, if the model asked one, isn't under test
            let n = c.msgs.count
            c.ping(#"{"ts": 1792700000, "text": "ws-a: merge !1?\nOptions: merge | hold"}"#)
            c.ping(#"{"text": "ws-b: goal for this workspace?"}"#)
            let w = c.waiting
            check("pings land in the inbox with options: \(w.map(\.options))", w.count == 2 && w[0].options == ["merge", "hold"] && w[0].text == "ws-a: merge !1?" && w[1].options.isEmpty)
            check("pings stay out of the chat", c.msgs.count == n)
            c.answer(w[0], "dismissed", send: false)
            c.answer(w[1], "(dismissed)", send: false)
            check("dismissing clears the bell without sending", c.waiting.isEmpty && c.run == nil)
            c.ping(#"{"text": "ws-c: delete it?", "key": "ws-c"}"#)
            c.ping(#"{"text": "ws-d: goal?", "key": "ws-d"}"#)
            c.ping(#"{"resolve": "ws-c", "why": "workspace removed"}"#)
            check("greenlight's resolve closes only that ping: \(c.waiting.map(\.text))", c.waiting.map(\.text) == ["ws-d: goal?"]
                  && c.questions.first { $0.key == "ws-c" }?.answer == "(resolved: workspace removed)" && c.run == nil)
            c.answer(c.waiting[0], "(dismissed)", send: false)
            check("questions persist", ((try? JSONDecoder().decode([Question].self, from: Data(contentsOf: questionsFile))) ?? []).count == c.questions.count)

            // pings that name workspaces (`greenlight.py ping --ws`): the dashboard lifts their hexes until answered, superseded or resolved
            c.ping(#"{"text": "3.22 ship?\nOptions: ship | hold", "key": "@lift", "ws": ["feat-3", "fix-1", "fix-2"]}"#)
            c.ping(#"{"text": "no workspaces here", "key": "@plain"}"#)
            check("a ping keeps the workspaces it names: \(c.waiting.map { $0.ws ?? [] })",
                  c.waiting.first { $0.key == "@lift" }?.ws == ["feat-3", "fix-1", "fix-2"] && c.waiting.first { $0.key == "@plain" }?.ws == nil)
            check("mc.bell's asked list has only pings that name workspaces: \(c.askedJSON)",
                  c.asked.count == 1 && (c.asked[0]["ws"] as? [String]) == ["feat-3", "fix-1", "fix-2"]
                  && (c.asked[0]["options"] as? [String]) == ["ship", "hold"] && (c.asked[0]["id"] as? String) == c.waiting.first { $0.key == "@lift" }?.id.uuidString)
            c.answer(c.waiting.first { $0.key == "@lift" }!, "hold", send: false)  // what answerPing does, minus the send
            check("answering it empties asked", c.asked.isEmpty && c.askedJSON == "[]")
            c.ping(#"{"text": "lift again", "key": "@lift", "ws": ["feat-3"]}"#)
            c.ping(#"{"text": "lift again, reworded", "key": "@lift", "ws": ["fix-1"]}"#)
            check("a newer ping with the same key replaces it in asked: \(c.askedJSON)", c.asked.count == 1 && (c.asked[0]["ws"] as? [String]) == ["fix-1"])
            c.ping(#"{"resolve": "@lift", "why": "shipped"}"#)
            check("greenlight's resolve empties asked", c.asked.isEmpty)
            c.answer(c.waiting.first { $0.key == "@plain" }!, "(dismissed)", send: false)
            let old = #"[{"id": "E621E1F8-C36C-495A-93FC-0C247A3E6E5F", "text": "old", "options": ["a"], "ping": true, "ts": 0, "key": "ws-x"}]"#
            let decoded = try? JSONDecoder().decode([Question].self, from: Data(old.utf8))
            check("a questions.json from before ws still loads", decoded?.count == 1 && decoded?[0].ws == nil && decoded?[0].key == "ws-x")
            let round = try? JSONDecoder().decode([Question].self, from: JSONEncoder().encode([Question(text: "t", options: [], ping: true, ws: ["a"])]))
            check("ws survives a save", round?[0].ws == ["a"])
        }

        attachTest(c, check: check, wait: wait)

        let before = onMain { c.context }
        onMain { c.compact() }
        wait("compact", 120) { c.run == nil }
        check("Compact: \(before) → \(onMain { c.context })", onMain { c.context } < before)

        onMain { c.name = "  Jarvis "; c.doneNaming() }  // what Return / click-away in the name field does
        check("rename saved: \(d.string(forKey: "name") ?? "-")", onMain { c.name == "Jarvis" && !c.editingName } && d.string(forKey: "name") == "Jarvis")
        onMain {
            c.ping(#"{"text": "MR !4321 needs your review"}"#)
            c.reply(to: "MR !4321 needs your review", "Which MR number was that about, and what is your name? One line.")
            check("banner reply answers the ping in the inbox", c.waiting.isEmpty)
        }
        wait("notification reply", 120) { c.run == nil }
        onMain {
            let r = c.msgs.last!.text.lowercased()
            check("notification reply shows as the user's text and gets an answer: \(c.msgs.last!.text)",
                  c.msgs[c.msgs.count - 2].text.hasPrefix("Which MR") && r.contains("4321") && r.contains("jarvis"))
            c.name = " "; c.doneNaming()
            check("empty name falls back to Greenlight", c.name == "Greenlight")
        }

        onMain { c.model = "codex"; c.send("Reply with just the word pong.") }
        wait("codex", 180) { c.run == nil }
        check("Codex: \(onMain { c.msgs.last!.text }) (window \(onMain { c.window }))", onMain { c.msgs.last!.text.lowercased().contains("pong") })

        onMain { c.clear() }
        Thread.sleep(forTimeInterval: 4)
        let left = Process()
        let (exe, args) = remoteArgv("ls \(greenlightHome)/chat-test")
        left.executableURL = URL(fileURLWithPath: exe)
        left.arguments = args
        let out = Pipe()
        left.standardOutput = out
        try? left.run()
        left.waitUntilExit()
        let files = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        check("Clear resets transcript, context and both sessions (left: \(files.split(separator: "\n")))",
              onMain { c.msgs.count == 1 && c.context == 0 } && !files.contains("session"))

        onMain {  // the fixed width set while working must fit the star and the longest timer
            let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            item.button?.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
            item.button?.imagePosition = .imageLeading
            item.button?.image = NSImage(systemSymbolName: "star.fill", accessibilityDescription: nil)
            item.button?.title = " 59:59"
            check("menubar timer fits the fixed 64pt (needs \(Int(item.button!.fittingSize.width)))", item.button!.fittingSize.width <= 64)
            NSStatusBar.system.removeStatusItem(item)
        }

        // voice: `say` a phrase into a 16 kHz WAV and run it through each installed engine
        let wav = FileManager.default.temporaryDirectory.appendingPathComponent("greenlight-selftest.wav")
        let say = Process()
        say.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        say.arguments = ["-o", wav.path, "--file-format=WAVE", "--data-format=LEI16@16000", "check the pipeline status"]
        try? say.run()
        say.waitUntilExit()
        for e in ["apple"] + (FileManager.default.isExecutableFile(atPath: whisperBin()) ? ["whisper"] : []) {
            let got = DispatchSemaphore(value: 0)
            var heard = ""
            transcribe(wav, engine: e) { t, err in heard = t ?? "error: \(err ?? "-")"; got.signal() }
            _ = got.wait(timeout: .now() + 120)
            check("voice \(e) heard \"\(heard)\"", heard.lowercased().contains("pipeline"))
        }

        let sem = DispatchSemaphore(value: 0)
        UNUserNotificationCenter.current().getNotificationSettings { s in
            check("notifications allowed in System Settings", s.authorizationStatus == .authorized); sem.signal()
        }
        sem.wait()

        for (k, v) in keep { d.set(v, forKey: k) }
        try? FileManager.default.removeItem(at: transcript)
        try? FileManager.default.removeItem(at: questionsFile)
        print(failed ? "FAILED" : "all ok")
        exit(failed ? 1 : 0)
    }
}

let app = NSApplication.shared
let delegate = App()
if CommandLine.arguments.contains("--vimtest") { vimTest() }
if selftest { CommandLine.arguments.contains("--attachtest") ? attachOnly() : selfTest(); app.setActivationPolicy(.accessory) } else { app.delegate = delegate; app.setActivationPolicy(.regular) }
app.run()

/// `Greenlight --vimtest`: the vim engine on a plain string, no window needed.
final class TestBuf: VimBuffer {
    var s: String
    var history: [String] = []
    var vimSel = NSRange(location: 0, length: 0)
    init(_ s: String) { self.s = s }
    var vimText: NSString { s as NSString }
    func vimReplace(_ r: NSRange, _ x: String) { history.append(s); s = (s as NSString).replacingCharacters(in: r, with: x) }
    func vimUndo() { if let h = history.popLast() { s = h } }
    func vimRedo() {}
    func vimVertical(_ down: Bool) {}
}

func vimTest() {
    var failed = false
    func run(_ text: String, _ keys: [String], _ want: String, at: Int? = nil, mode: String = "normal", start: Int = 0) {
        let b = TestBuf(text), v = Vim()
        v.mode = "normal"
        b.vimSel = NSRange(location: start, length: 0)
        for k in keys { _ = v.key(k, b) }
        let ok = b.s == want && (at == nil || b.vimSel.location == at) && v.mode == mode
        print(ok ? "ok  " : "FAIL", keys.joined(), "→", "\"\(b.s)\" @\(b.vimSel.location) \(v.mode)")
        failed = failed || !ok
    }
    let t = "hello world foo"
    run(t, ["w"], t, at: 6)
    run(t, ["e"], t, at: 4)
    run(t, ["$"], t, at: 14)
    run(t, ["b"], t, at: 6, start: 11)
    run(t, ["0"], t, at: 0, start: 9)
    run(t, ["d", "w"], "world foo", at: 0)
    run(t, ["x"], "ello world foo", at: 0)
    run(t, ["x", "u"], t)
    run(t, ["c", "w"], " world foo", at: 0, mode: "insert")
    run(t, ["c", "w", "\u{1b}"], " world foo", at: 0)
    run(t, ["d", "d"], "", at: 0)
    run(t, ["D"], "hello ", at: 5, start: 6)
    run(t, ["y", "w", "P"], "hello hello world foo")
    run(t, ["v", "e", "d"], " world foo", at: 0)
    run(t, ["r", "j"], "jello world foo", at: 0)
    run(t, ["A"], t, at: 15, mode: "insert")
    run(t, ["i", "z"], t, at: 0, mode: "insert")  // insert mode leaves typing to the field
    run(t, ["q"], t, at: 0)  // unknown keys never type in normal mode
    print(failed ? "FAILED" : "all ok")
    exit(failed ? 1 : 0)
}
