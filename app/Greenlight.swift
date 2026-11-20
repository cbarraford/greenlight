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
import AVFoundation
import Carbon
import FoundationModels
import Security
import Speech
import SwiftUI
import UserNotifications
import WebKit

let localDefault: String = { var b = [CChar](repeating: 0, count: 256); gethostname(&b, 255); return String(cString: b).components(separatedBy: ".")[0] }()  // "Local" in the Basement, until you rename it
let host = ProcessInfo.processInfo.environment["GREENLIGHT_HOST"] ?? UserDefaults.standard.string(forKey: "host") ?? "local"
let greenlightHome = UserDefaults.standard.string(forKey: "greenlightHome") ?? "~/.greenlight"
let greenlightCode = UserDefaults.standard.string(forKey: "greenlightCode") ?? "~/.claude/skills/greenlight"
let py = "GREENLIGHT_HOME=\(greenlightHome) python3 \(greenlightCode)/scripts"  // + "/greenlight.py ..."
let selftest = CommandLine.arguments.contains("--selftest")  // scratch chat session + transcript, see selfTest()
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
              ("Fable 5.1", "claude-fable-5-1"), ("Codex", "codex"), ("OpenCode", "opencode"), ("Pi", "pi")]
/// A model id is a Claude model, or another harness's name (it runs that harness's default model).
func harnessOf(_ model: String) -> String { model.hasPrefix("claude-") ? "claude" : model }
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
    inPipe.fileHandleForWriting.write(stdin.data(using: .utf8)!)
    try? inPipe.fileHandleForWriting.close()
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

func ago(_ d: Date) -> String {
    let s = Int(Date().timeIntervalSince(d))
    return s < 60 ? "now" : s < 3600 ? "\(s / 60)m ago" : s < 86400 ? "\(s / 3600)h ago" : "\(s / 86400)d ago"
}

/// A prompt you sent: ↑↓ walk them, ⌃R fuzzy-searches them.
struct Sent: Codable { let t: String; let at: Date }

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
    var gotDone = false, checkIn = false, watch = false
    init(_ q: String) { question = q }
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
@discardableResult
func keychain(_ account: String, set value: String? = nil) -> String? {
    let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "greenlight", kSecAttrAccount as String: account]
    if let value {
        SecItemDelete(q as CFDictionary)
        if !value.isEmpty { SecItemAdd(q.merging([kSecValueData as String: Data(value.utf8)]) { $1 } as CFDictionary, nil) }
        return value
    }
    var out: AnyObject?
    guard SecItemCopyMatching(q.merging([kSecReturnData as String: true]) { $1 } as CFDictionary, &out) == errSecSuccess,
          let d = out as? Data else { return nil }
    return String(decoding: d, as: UTF8.self)
}

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
        guard let key = keychain("openai") else { return done(nil, "no OpenAI key: right-click the star → Voice → Set OpenAI key…") }
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

// MARK: voice mode: a hands-free conversation with the chat, and the stage that draws what it says

/// Chief's reply, split for voice mode: its own ```stage block (HTML, or notes for Haiku to draw), and the rest. An unclosed fence (still streaming) hides everything from it on.
struct Staged: Equatable { var text: String; var stage: String?; var end = false; var hexes: [String]? = nil; var reset = false }
func staged(_ s: String) -> Staged {
    var text = s, stage: String?, end = false, hexes: [String]?
    if let r = text.range(of: #"\[hexe?s?:([^\]\n]*)(\]|$)"#, options: [.regularExpression, .caseInsensitive]) {  // Chief's own pick; unclosed while it streams
        let inner = text[r].dropFirst().drop { $0 != ":" }.dropFirst().replacingOccurrences(of: "]", with: "")
        hexes = inner.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty && $0.lowercased() != "none" }
        text.removeSubrange(r)
    }
    if let r = text.range(of: #"\[voice: ?off\]"#, options: [.regularExpression, .caseInsensitive]) { end = true; text.removeSubrange(r) }  // Chief ends voice mode
    let reset = text.range(of: #"\[chat: ?reset\]"#, options: [.regularExpression, .caseInsensitive]).map { text.removeSubrange($0); return true } ?? false  // asked to start over
    if let r = text.range(of: #"\[stage: ?(full|normal)\]"#, options: [.regularExpression, .caseInsensitive]) { text.removeSubrange(r) }  // the old size line: no full size any more
    if let open = text.range(of: "```stage") {
        if let close = text[open.upperBound...].range(of: "```") {
            stage = String(text[open.upperBound..<close.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
            text.removeSubrange(open.lowerBound..<close.upperBound)
        } else { text = String(text[..<open.lowerBound]) }
    }
    return Staged(text: text.trimmingCharacters(in: .whitespacesAndNewlines), stage: stage, end: end, hexes: hexes, reset: reset)
}

/// Markdown off, for speaking: code blocks and bare URLs out, links to their text, emphasis/heading/quote/bullet marks gone.
func plain(_ s: String) -> String {
    var t = s.replacingOccurrences(of: "```[\\s\\S]*?```", with: "", options: .regularExpression)
    if let r = t.range(of: "```") { t = String(t[..<r.lowerBound]) }
    for (p, w) in [("\\[([^\\]]*)\\]\\([^)]*\\)", "$1"), ("`([^`]*)`", "$1"), ("https?://\\S+", ""), ("(?m)^[ \\t]*(#+|>|[-*+]|\\d+\\.)[ \\t]+", ""), ("\\*+|~~", "")] {
        t = t.replacingOccurrences(of: p, with: w, options: .regularExpression)
    }
    return t.filter { ch in  // Chief's mood emoji aren't spoken (Apple's voice reads their names); digits, # and * are "emoji" too, so keep those
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

/// A buffer's loudness, 0…1 (−50 dB…−5 dB), for the waveforms.
func level(_ b: AVAudioPCMBuffer) -> Float {
    guard let ch = b.floatChannelData, b.frameLength > 0 else { return 0 }
    var sum: Float = 0
    for i in 0..<Int(b.frameLength) { sum += ch[0][i] * ch[0][i] }
    return max(0, min(1, (20 * log10(max(sqrt(sum / Float(b.frameLength)), 1e-6)) + 50) / 45))
}

let voiceNote = "(Voice mode: the user is talking to you out loud, and your reply is spoken aloud. Answer in 1–3 short spoken sentences: "
    + "no markdown, links or lists in the spoken part. Their screen shows a stage beside your voice; a fast model draws your reply there by itself. "
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

enum Talk { case speaking, listening, thinking }
/// What every look shows, from Voice and Core.
struct Live {
    let v: Voice, c: Core
    var talk: Talk { v.speaking > 0 ? .speaking : c.run != nil ? .thinking : .listening }
    var label: String { talk == .speaking ? "Speaking" : talk == .thinking ? "Thinking" : "Listening" }
    var hint: String { talk == .speaking ? (v.vp && v.bargeIn ? "talk to interrupt" : "speaking") : talk == .thinking ? "say “stop” to cancel" : "“that’s all” ends voice mode" }
    var said: String { v.captions ? v.caption : "" }
    var heard: String { v.captions ? v.heard : "" }
    var frac: Double { c.ctxFull }
    var pct: Int { Int((frac * 100).rounded()) }
    var high: Bool { frac > 0.75 }
    var tokens: String { c.window > 0 ? "\(kfmt(c.context)) of \(kfmt(c.window))" : kfmt(c.context) }
    var name: String { c.name }
    func clear() { v.run(.reset) }
    /// The stage, inset in its card so the card's rounded corners show (the web view itself is square).
    func stage(_ l: Look, r: CGFloat = 12, pad: CGFloat = 8, border: Color? = nil) -> some View {
        StageWeb(web: v.stageWeb).padding(pad).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(RoundedRectangle(cornerRadius: r).fill(hx(l.stage)))
            .overlay(RoundedRectangle(cornerRadius: r).stroke(border ?? .clear))
    }
    /// The context readout as a button: its popover has the totals and Compact.
    func ctx<L: View>(@ViewBuilder _ label: () -> L) -> some View {
        Button { c.ctxOpen.toggle() } label: { label() }.buttonStyle(.plain).accessibilityLabel("Context, \(pct) percent used").modifier(CtxPop(c: c))
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
            }
            HStack(spacing: 10) {
                Bars(m: s.v.meter, n: 36, on: hx(l.accent), off: hx("8C959F"))
                Spacer(minLength: 4)
                s.ctx { Text("\(s.pct)% context").font(.system(size: 12, weight: .medium).monospacedDigit()).foregroundColor(s.high ? hx("9A6700") : hx(l.dim)) }
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
            HStack(spacing: 12) {
                s.ctx { Text("\(s.pct)% · \(s.tokens)").font(.system(size: 12).monospacedDigit()).foregroundColor(s.high ? hx("FF9F0A") : hx(l.dim)) }
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
        }.animation(.easeOut(duration: 0.12), value: lv).accessibilityElement().accessibilityLabel("\(s.label), context \(s.pct) percent")
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
            }.padding(.bottom, 12)
            HStack(spacing: 12) {
                s.ctx {
                    HStack(spacing: 10) {
                        Text("CONTEXT \(s.pct)%").font(.system(size: 11, weight: .heavy).monospacedDigit()).tracking(1.2)
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
            }.lineLimit(4).padding(14)
            HStack(spacing: 10) { Text(s.talk == .speaking ? s.name.lowercased() : s.talk == .listening ? "you" : "wait").foregroundColor(faint); Glyphs(m: s.v.meter, talk: s.talk) }
                .padding(.horizontal, 14).padding(.bottom, 10)
            HLine().stroke(faint, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1)
            HStack(spacing: 10) {
                s.ctx { Text("ctx [\(String(repeating: "#", count: f) + String(repeating: ".", count: 10 - f))] \(s.pct)%").foregroundColor(s.high ? hx("FFB000") : ink) }
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
            Scope(m: s.v.meter, ink: s.talk == .speaking ? red : ink, rule: hx(l.line)).frame(height: 44).padding(.top, 12)
            Rectangle().fill(ink).frame(height: 1)
            HStack(spacing: 14) {
                s.ctx { Text("Context \(Text("\(s.pct)").bold()) percent used · \(s.tokens)").font(.system(size: 12).monospacedDigit()).foregroundColor(s.high ? red : hx(l.text2)) }
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
            VStack(spacing: 1) {
                Bars(m: s.v.meter, n: 40, w: 4, gap: 4, h: 30, round: false, on: cyan, off: hx("4A6573"))
                Bars(m: s.v.meter, n: 40, w: 4, gap: 4, h: 30, round: false, on: cyan, off: hx("4A6573")).scaleEffect(x: 1, y: -1).opacity(0.35)
            }.frame(maxWidth: .infinity)
            HStack(spacing: 14) {
                s.ctx {
                    ZStack {
                        Circle().trim(from: 0, to: 0.75).stroke(hx(l.line), lineWidth: 5).rotationEffect(.degrees(135))
                        Circle().trim(from: 0, to: 0.75 * max(s.frac, 0.005)).stroke(s.high ? hx("FF9F0A") : cyan, lineWidth: 5).rotationEffect(.degrees(135))
                        VStack(spacing: 0) { Text("\(s.pct)").font(.system(size: 17, weight: .bold, design: .monospaced)); Text("CTX%").font(.system(size: 9, design: .monospaced)).foregroundColor(faint) }
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
            Breath(m: s.v.meter, talk: s.talk, l: l).frame(height: 40).padding(.top, 24)
            Text(s.label.lowercased()).font(.system(size: 12)).foregroundColor(hx(l.dim)).padding(.top, 4)
            HStack {
                s.ctx { Text("\(s.pct)%").font(.system(size: 12).monospacedDigit()).foregroundColor(s.high ? hx("FF9F0A") : hx(l.dim)) }
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
            Button(action: v.toggle) { shell(Image(systemName: "waveform").font(sys(size: Theme.fs3, weight: .semibold)), on: v.on) }
                .buttonStyle(.plain).help(v.on ? "End voice mode (or say “goodbye”)" : "Voice mode: talk with it hands-free")
                .accessibilityLabel(v.on ? "End voice mode" : "Start voice mode")
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
    var bargeIn: Bool { ud.object(forKey: "voiceBargeIn") as? Bool ?? true }
    var pause: Double { ud.object(forKey: "voicePause") as? Double ?? 1.1 }  // seconds of quiet that end your turn; hardware and rooms differ
    let engine = AVAudioEngine(), player = AVAudioPlayerNode(), synth = AVSpeechSynthesizer()
    let speechFormat = AVAudioFormat(standardFormatWithSampleRate: 22050, channels: 1)!, micFormat = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
    let rec = SFSpeechRecognizer(locale: Locale(identifier: "en-US")), lock = NSLock()
    var request: SFSpeechAudioBufferRecognitionRequest?, task: SFSpeechRecognitionTask?, endTurn: DispatchWorkItem?
    var vp = false  // echo cancellation took; without it Chief can't be interrupted (it would hear itself)
    var waitWord = "One minute"
    var ending = false  // Chief said [voice: off]: stop once it's done speaking
    var muted = false  // you cut in or said stop: the rest of this reply stays unspoken
    var said = 0, lastSpoken = "", lastHeard = "", lines = [String](), gen = 0, synthConv: AVAudioConverter?, voiceConv: AVAudioConverter?
    var drawSeq = 0, drewAt = 0, drawing = false, chiefDrew = false, shown = false
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
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            guard let self, self.on else { return }  // a headset in or out: the engine stopped itself
            do { try self.startEngine(); self.listen() } catch { self.stopAll() }
        }
    }

    func toggle() { on ? stopAll() : start() }
    func start() { start(tries: 3) }
    func start(tries: Int) {
        guard !on, core?.recording == nil else { return }  // dictation has the mic
        AVCaptureDevice.requestAccess(for: .audio) { ok in SFSpeechRecognizer.requestAuthorization { st in DispatchQueue.main.async {
            guard ok, st == .authorized else {
                self.core?.msgs.append(Msg(.note, "Voice mode needs Microphone and Speech Recognition: System Settings → Privacy & Security → Greenlight."))
                return
            }
            do { try self.startEngine() } catch {
                // Right after the permission prompts the input isn't ready yet (AVAudio -10875): try again shortly, so one click is enough
                if tries > 1 { self.engine.stop(); return DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { self.start(tries: tries - 1) } }
                self.core?.msgs.append(Msg(.note, "Voice mode couldn't start the audio: \(error.localizedDescription)"))
                return self.stopAll()
            }
            self.on = true
            self.listen()
            self.wait("Listening", "Say something, or “goodbye” to stop.")
        }}}
    }

    func startEngine() throws {
        engine.stop()
        let input = engine.inputNode
        vp = (try? input.setVoiceProcessingEnabled(true)) != nil
        if !engine.attachedNodes.contains(player) { engine.attach(player) }
        engine.connect(player, to: engine.mainMixerNode, format: speechFormat)
        let inFormat = input.outputFormat(forBus: 0)  // after voice processing, which can change it
        guard inFormat.sampleRate > 0, inFormat.channelCount > 0 else {  // no input device: installTap would raise, not throw
            throw NSError(domain: "Voice", code: 1, userInfo: [NSLocalizedDescriptionKey: "No microphone is connected."])
        }
        let conv = AVAudioConverter(from: inFormat, to: micFormat)
        if inFormat.channelCount > 1 { conv?.channelMap = [0] }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { [weak self] b, _ in
            guard let self, let conv, let m = convert(b, conv, self.micFormat) else { return }
            self.lock.lock(); let r = self.request; self.lock.unlock()
            r?.append(m)
            let l = level(m)
            DispatchQueue.main.async { self.meter.you = Array(self.meter.you.dropFirst()) + [l] }
        }
        engine.mainMixerNode.removeTap(onBus: 0)
        engine.mainMixerNode.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] b, _ in
            let l = level(b)
            DispatchQueue.main.async { guard let self else { return }; self.meter.chief = Array(self.meter.chief.dropFirst()) + [self.speaking > 0 ? l : 0] }
        }
        engine.prepare()
        try engine.start()
    }

    /// A fresh recognition request: after each of your turns, after Chief speaks (its echo is in the old one), after an error.
    func listen() {
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
            self.say(self.waitWord)
        }
    }
    func status(_ s: String) { if on && !shown { wait(waitWord, s) } }
    func live(_ r: Run) {
        guard on else { return }
        let st = staged(r.live), p = plain(st.text)
        if let h = st.stage { stage(h) }
        let (ss, to) = sentences(p, from: said)
        said = to
        ss.forEach(say)
        if !chiefDrew && !drawing && p.count - drewAt > 300 { drewAt = p.count; draw(p, last: false) }
    }
    func finished(_ st: Staged, _ body: String) {
        guard on else { return }
        if let h = st.stage { stage(h) }
        let p = plain(body) + "\n"
        sentences(p, from: min(said, p.count)).said.forEach(say)
        said = 0
        if st.end { ending = true; if speaking == 0 && lines.isEmpty { stopAll() } }
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
    func say(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard on, !muted, !t.isEmpty, t.rangeOfCharacter(from: .alphanumerics) != nil else { return }
        lastSpoken = String((lastSpoken + " " + t).suffix(600))
        lines.append(t)
        if lines.count == 1 { caption = t }
        let g = gen
        guard Speech.shared.kind != "apple" else { return apple(t, g) }
        Speech.shared.q.async { [weak self] in  // serial: lines play in order
            guard let self, self.gen == g else { return }
            guard let b = Speech.shared.pcm(t) else { return DispatchQueue.main.async { self.apple(t, g) } }
            if self.voiceConv?.inputFormat != b.format { self.voiceConv = AVAudioConverter(from: b.format, to: self.speechFormat) }
            guard let out = self.voiceConv.flatMap({ convert(b, $0, self.speechFormat, whole: true) }) else { return }
            self.play(out, end: false, g); self.play(nil, end: true, g)
        }
    }
    func apple(_ t: String, _ g: Int) {
        let u = AVSpeechUtterance(string: t)
        u.voice = Voice.best
        synth.write(u) { [weak self] buf in
            guard let self, let b = buf as? AVAudioPCMBuffer else { return }
            if b.frameLength == 0 { return self.play(nil, end: true, g) }  // the utterance is over
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
        if !self.player.isPlaying && self.engine.isRunning { self.player.play() }  // play() raises on a stopped engine
    }
    /// Stop talking (you cut in, or said stop); the turn itself goes on.
    func hush() {
        gen += 1; muted = true
        synth.stopSpeaking(at: .immediate)
        player.stop()
        speaking = 0; lines = []
    }

    func stopAll() {
        hush()
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
    }
}

/// A voice better than AVSpeechSynthesizer: Kokoro on this Mac (app/kokoro.py under uv) or ElevenLabs Flash.
/// pcm() blocks, on q; nil means Apple's voice says that line (not ready yet, no key, a failure).
final class Speech {
    static let shared = Speech()
    static let kinds = [("apple", "Apple"), ("kokoro", "Kokoro (on this Mac)"), ("eleven", "ElevenLabs Flash")]
    static let kokoroVoices = ["af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_michael", "am_fenrir", "am_puck", "bf_emma", "bm_george", "bm_fable"]
    let q = DispatchQueue(label: "speech"), fmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 24000, channels: 1, interleaved: true)!
    var kind: String { ud.string(forKey: "speech") ?? "apple" }
    var kokoroVoice: String { ud.string(forKey: "kokoroVoice") ?? "af_heart" }
    var elevenVoice: String { ud.string(forKey: "elevenVoice").flatMap { $0.isEmpty ? nil : $0 } ?? "EXAVITQu4vr4xnSDxMaL" }  // "Sarah", a premade voice
    var proc: Process?, kin: FileHandle?, kout: FileHandle?
    var ready = false  // Kokoro has loaded (the first start installs ~1 GB through uv)
    var status = ""

    func pcm(_ text: String) -> AVAudioPCMBuffer? {
        let d: Data?
        switch kind {
        case "kokoro": d = ready ? kokoro(text) : nil
        case "eleven": d = eleven(text)
        default: d = nil
        }
        return d.flatMap(buffer)
    }
    func buffer(_ d: Data) -> AVAudioPCMBuffer? {
        guard d.count > 1, let b = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(d.count / 2)) else { return nil }
        b.frameLength = b.frameCapacity
        d.withUnsafeBytes { _ = memcpy(b.int16ChannelData![0], $0.baseAddress!, Int(b.frameLength) * 2) }
        return b
    }

    func eleven(_ text: String) -> Data? {
        guard let key = keychain("elevenlabs"), var u = URLComponents(string: "https://api.elevenlabs.io/v1/text-to-speech/\(elevenVoice)") else { status = "No ElevenLabs key"; return nil }
        u.queryItems = [URLQueryItem(name: "output_format", value: "pcm_24000")]
        var r = URLRequest(url: u.url!, timeoutInterval: 15)
        r.httpMethod = "POST"
        r.setValue(key, forHTTPHeaderField: "xi-api-key")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text, "model_id": "eleven_flash_v2_5"])
        var got: Data?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: r) { d, resp, _ in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            if code == 200 { got = d; self.status = "" } else { self.status = "ElevenLabs: \(code) \(d.flatMap { String(data: $0, encoding: .utf8) }?.prefix(160) ?? "")" }
            sem.signal()
        }.resume()
        sem.wait()
        return got
    }

    func kokoro(_ text: String) -> Data? {
        guard let kin, let kout, let line = try? JSONSerialization.data(withJSONObject: ["text": text, "voice": kokoroVoice]) else { return nil }
        do {
            try kin.write(contentsOf: line + Data("\n".utf8))
            guard let n = try kout.read(upToCount: 4), n.count == 4 else { throw CocoaError(.fileReadUnknown) }
            let len = n.withUnsafeBytes { Int(UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self))) }
            return len == 0 ? nil : try kout.read(upToCount: len)
        } catch { ready = false; status = "Kokoro stopped"; proc?.terminate(); proc = nil; return nil }  // ponytail: no auto-restart; pick it again in Settings
    }

    /// Start Kokoro if it's the voice (at launch, and on picking it); a warm-up line loads the model before the first real one.
    func warm() {
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
}

final class Core: ObservableObject {
    lazy var voice: Voice = { let v = Voice(); v.core = self; return v }()
    @Published var attached: [Attached] = []
    var unattach: ((String) -> Void)?  // tells the dashboard to empty that dock slot
    @Published var msgs: [Msg] = ((try? JSONDecoder().decode([Msg.Saved].self, from: Data(contentsOf: transcript)))?.map(Msg.init))
        ?? [Msg(.note, "Ask me anything about your workspaces.")] {
        didSet {  // ponytail: rewrites the whole file per message; capped at 300 so that stays cheap
            try? FileManager.default.createDirectory(at: transcript.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? JSONEncoder().encode(msgs.suffix(300).map(\.saved)).write(to: transcript, options: .atomic)
        }
    }
    @Published var run: Run?
    @Published var queue: [String] = []
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
            }
        }
    }
    @Published var effort = UserDefaults.standard.string(forKey: "effort") ?? "medium" { didSet { UserDefaults.standard.set(effort, forKey: "effort") } }
    // The agent that designs dashboard pages and puck widgets (claude only).
    @Published var designModel = UserDefaults.standard.string(forKey: "designModel") ?? "claude-haiku-5-5" { didSet { UserDefaults.standard.set(designModel, forKey: "designModel") } }
    @Published var designEffort = UserDefaults.standard.string(forKey: "designEffort") ?? "low" { didSet { UserDefaults.standard.set(designEffort, forKey: "designEffort") } }
    /// The terminal's tab names: written by this model once a tab's output settles, unless you named it.
    @Published var termNameModel = UserDefaults.standard.string(forKey: "termNameModel") ?? "claude-haiku-5-5" { didSet { UserDefaults.standard.set(termNameModel, forKey: "termNameModel") } }
    @Published var termAutoName = UserDefaults.standard.object(forKey: "termAutoName") as? Bool ?? true { didSet { UserDefaults.standard.set(termAutoName, forKey: "termAutoName") } }
    // The agent that writes the hover card's next step (dash.py suggest), ahead of time for every puck whose state moved.
    @Published var nextModel = UserDefaults.standard.string(forKey: "nextModel") ?? "claude-haiku-5-5" { didSet { UserDefaults.standard.set(nextModel, forKey: "nextModel") } }
    @Published var nextEffort = UserDefaults.standard.string(forKey: "nextEffort") ?? "low" { didSet { UserDefaults.standard.set(nextEffort, forKey: "nextEffort") } }
    @Published var designParallel = UserDefaults.standard.object(forKey: "designParallel") as? Int ?? 3 { didSet { UserDefaults.standard.set(designParallel, forKey: "designParallel") } }
    @Published var context = UserDefaults.standard.integer(forKey: "context") { didSet { UserDefaults.standard.set(context, forKey: "context") } }
    @Published var window = UserDefaults.standard.integer(forKey: "window") { didSet { UserDefaults.standard.set(window, forKey: "window") } }
    @Published var tps = 0.0
    @Published var outTok = 0  // this run's output tokens, for the live line
    @Published var liveLine = ud.object(forKey: "liveLine") as? Bool ?? true { didSet { ud.set(liveLine, forKey: "liveLine") } }
    @Published var lastTps = 0.0
    @Published var connected = false
    @Published var unread = 0
    @Published var now = Date()
    @Published var name = UserDefaults.standard.string(forKey: "name") ?? "Greenlight" { didSet { UserDefaults.standard.set(name, forKey: "name") } }
    @Published var editingName = false
    @Published var hint = ""  // hover text for the footer buttons; native tooltips are slow
    @Published var keying = false  // the next chord becomes the voice shortcut: Settings shows the recorder in place
    @Published var questions = (try? JSONDecoder().decode([Question].self, from: Data(contentsOf: questionsFile))) ?? [] {
        didSet {  // all waiting ones plus the last 20 answered
            let keep = questions.filter { $0.answer == nil } + questions.filter { $0.answer != nil }.suffix(20)
            try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
            try? JSONEncoder().encode(keep.sorted { $0.ts < $1.ts }).write(to: questionsFile, options: .atomic)
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
        if voice.on { return }  // voice mode has the mic
        if recording != nil { stopVoice() } else if !transcribing { startVoice() }
    }

    func startVoice() {
        AVCaptureDevice.requestAccess(for: .audio) { ok in DispatchQueue.main.async {
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
        let t = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        if input.first?.isWhitespace != true { sent = [Sent(t: t, at: Date())] + sent.filter { $0.t != t }.prefix(499) }  // ignorespace
        input = ""
        if vim { vimKeys.mode = vimStart }
        if let q = waiting.last(where: { !$0.ping }) { answer(q, t) } else { send(t) }  // typing answers the open chat question
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

    func send(_ text: String, shown: String? = nil) {
        active()
        if run != nil { queue.append(text); return }
        msgs.append(Msg(.you, shown ?? text))
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
        start(Run(shown ?? text), chatSend, stdin: payload)
    }

    // The idle check-in: at the Mac but nothing done in Greenlight (chat, a card's button, typing in a Basement terminal) for
    // checkInMins, greenlight asks the chat what to work on next, once per quiet stretch.
    // ponytail: work outside Greenlight (an editor, a browser) doesn't count; count pushes from the scan if it nags then
    @Published var checkInMins = UserDefaults.standard.object(forKey: "checkInMins") as? Int ?? 30 { didSet { UserDefaults.standard.set(checkInMins, forKey: "checkInMins") } }
    var lastActive = Date(), checkedIn = false
    func active() { lastActive = Date(); checkedIn = false }
    func checkIn() {
        let atMac = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!) < 120
        guard checkInMins > 0, !checkedIn, run == nil, atMac, Date().timeIntervalSince(lastActive) > Double(checkInMins * 60) else { return }
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
    var focusPaths: [String]?  // its workspaces: Chief's BOARD has only these (nil: the window shows every project)
    var chatSend: String {
        let only = focusPaths.flatMap { try? JSONSerialization.data(withJSONObject: $0) }.flatMap { String(data: $0, encoding: .utf8) }.map { " --only \(shellQuote($0))" } ?? ""
        return "\(chatCmd) send --model \(model) --effort \(effort) --name \(shellQuote(name))" + only
    }
    var pucks: [[String: Any]] = [], picks = 0  // the latest snapshot's workspaces; picks: the newest highlight asked for
    var needs: [String: String]?, fresh = [String: String](), watchedAt = Date.distantPast
    func watched(_ pucks: [[String: Any]]) {
        let (now, new) = newNeeds(needs, pucks)
        needs = now
        fresh = fresh.merging(new) { $1 }.filter { now[$0.key] != nil }  // one that stopped needing you before the turn is dropped
        watchTurn()
    }
    func watchTurn() {  // ponytail: one turn per 5 minutes at most, everything new batched; a knob if it nags or lags
        guard watch, !fresh.isEmpty, run == nil, queue.isEmpty, Date().timeIntervalSince(watchedAt) > 300 else { return }
        watchedAt = Date()
        let list = fresh.sorted { $0.key < $1.key }.map { "- \($0.key): \($0.value)" }.joined(separator: "\n")
        fresh = [:]
        let r = Run("Watching the workspaces"); r.watch = true
        start(r, chatSend, stdin:
            "(Greenlight's watch, not typed by the user: these workspaces just started needing the user.)\n\(list)\n"
            + "Look into them (refresh the workspaces if the board is stale). If one needs a decision or an answer only the user can give, "
            + "ask one short question about it, with its MR link and a final Options line. Otherwise reply exactly: (nothing)")
    }

    func compact() {
        guard run == nil, isClaude else { return }
        start(Run("Compact context"), "\(chatCmd) compact --model \(model)", stdin: "")
    }

    func clear() {
        if run != nil { stop() }
        queue = []
        ssh("\(chatCmd) new")
        for i in attached.indices { attached[i].sent = false }  // the new conversation hasn't seen them
        msgs = [Msg(.note, "Context cleared. New conversation.")]
        focusSent = ""  // the new conversation hasn't heard where you are
        picks += 1; delegate.pick([])  // nor lit anything
        if voice.on { voice.wait("Listening", "New conversation. Context cleared.") }
        questions.removeAll { !$0.ping && $0.answer == nil }  // their conversation is gone
        context = 0
    }

    func stop() {
        guard let r = run, !r.stopping else { return }
        r.stopping = true
        r.status = "Stopping"
        ssh("\(chatCmd) stop")
    }

    private func start(_ r: Run, _ remote: String, stdin: String) {
        run = r
        tps = 0; outTok = 0
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
        case "start": r.status = "Starting \(models.first { $0.1 == j["engine"] as? String }?.0 ?? "Claude")"
        case "status": if !r.stopping { r.status = j["text"] as? String ?? r.status; voice.status(r.status) }
        case "step":
            let s = Step(id: j["id"] as? String ?? UUID().uuidString, title: j["title"] as? String ?? "", detail: j["detail"] as? String ?? "")
            r.steps.append(s); r.call = j["call"] as? String ?? ""
            if !r.stopping { r.status = s.title; voice.status(s.title) }
        case "step_done":
            if let i = r.steps.firstIndex(where: { $0.id == j["id"] as? String }) {
                r.steps[i].ok = j["ok"] as? Bool ?? true
                r.steps[i].out = j["out"] as? String ?? ""
                r.steps[i].end = Date()
            }
        case "text_reset": r.live = ""; voice.said = 0
        case "text": r.live += j["delta"] as? String ?? ""; if !r.watch && !r.checkIn { voice.live(r) }
        case "usage":
            if let c = j["context"] as? Int, c > 0 { context = c }
            if let t = j["tps"] as? Double { tps = t; if t > 0 { lastTps = t } }
            if let o = j["out"] as? Int { outTok = o }
        case "done":
            r.gotDone = true
            if let c = j["context"] as? Int, c > 0 { context = c }
            if let w = j["window"] as? Int, w > 0 { window = w }
            var text = j["text"] as? String ?? ""
            if r.question == "Compact context" { text = text.isEmpty ? "Compacted. Context is now \(kfmt(context))." : text }
            finish(r, ok: j["ok"] as? Bool ?? false, text: r.stopping ? "Stopped." : text)
        default: break
        }
    }

    private func finish(_ r: Run, ok: Bool, text: String) {
        if r.watch && (text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased().hasPrefix("(nothing)") || !ok) {  // nothing to ask: no trace
            run = nil; tps = 0
            if !queue.isEmpty { send(queue.removeFirst()) }
            return
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
            delegate.pick([])
            if let h = st.hexes { delegate.pick(resolveHexes(h, pucks)) }  // Chief said which: no guessing
            else { pickHexes(body, pucks, asked: asksToShow(r.question)) { [weak self] names in if n == self?.picks { delegate.pick(names) } } }
        }
        run = nil
        tps = 0
        if ok && st.reset { DispatchQueue.main.async { [weak self] in self?.clear() } }  // [chat: reset]: after this reply, which says so
        if !popoverShown() && !r.stopping {
            missed += 1
            notify(title: r.checkIn ? "\(name): what to work on next" : r.watch ? "\(name) asks" : "Done: \(r.question.prefix(60))", body: String(body.prefix(240)), subtitle: r.checkIn || r.watch ? nil : fmt(m.duration))
        }
        if !queue.isEmpty { send(queue.removeFirst()) }
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
        c.categoryIdentifier = "greenlight"
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
    /// Claude Code's live line, under the reply in every look: the running call, then the spinner with time, tokens and speed.
    /// ponytail: no live shell output; claude's stream only sends a tool's elapsed seconds while it runs.
    var activity: some View {
        let t = max(0, c.now.timeIntervalSince(r.start)), cur = r.steps.last.flatMap { $0.ok == nil ? $0 : nil }
        let w = r.stopping ? "Stopping…" : c.voice.waitWord.hasSuffix("…") ? c.voice.waitWord : c.voice.waitWord + "…"
        let bits = [fmt(t), c.outTok > 0 ? "↓ \(kfmt(c.outTok)) tokens" : nil, c.tps > 0 ? "\(Int(c.tps)) tok/s" : nil,
                    r.status == "Thinking" ? "thinking" : nil].compactMap { $0 }.joined(separator: " · ")
        return VStack(alignment: .leading, spacing: 2) {
            if let s = cur {
                Text("\(Text("⏺ ").foregroundColor(accent))\(r.call.isEmpty ? s.title : r.call)").foregroundColor(fg).lineLimit(1).truncationMode(.middle)
                Text("  ⎿  Running… \(fmt(c.now.timeIntervalSince(s.start)))").foregroundColor(dim)
            }
            Text("\(Text(["·", "✢", "✳", "✶", "✻", "✽"][Int(t) % 6] + " ").foregroundColor(accent))\(Text(w).foregroundColor(fg)) \(Text("(\(bits))").foregroundColor(dim))").lineLimit(1)
        }.font(sys(size: Theme.fs2, design: .monospaced)).textSelection(.enabled).accessibilityElement(children: .combine)
    }
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
                Text(stats).font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(dim).lineLimit(1)
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
                    if lk.id != "onair" { Text(stats).font((lk.id == "hud" ? code(Theme.fs1) : ui(Theme.fs2)).monospacedDigit()).tracking(lk.id == "hud" ? 1 : 0).foregroundColor(dim).lineLimit(1) }
                }
                Spacer(minLength: 4)
                stop
            }
        }
    }
    /// Orb's lead: the amber orb with two soft rings. ponytail: static; the live stage's orb is the one that moves.
    var orbMark: some View {
        ZStack {
            Circle().fill(accent.opacity(0.07)).frame(width: 60, height: 60)
            Circle().fill(accent.opacity(0.16)).frame(width: 46, height: 46)
            Circle().fill(RadialGradient(colors: [hx("FFD27A"), hx("F5A524"), hx("B4740E")], center: UnitPoint(x: 0.35, y: 0.35), startRadius: 0, endRadius: 22))
        }.frame(width: 34, height: 34).accessibilityHidden(true)
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
        let t = Text("\(mono ? Text("\(c.name.lowercased())> ").foregroundColor(dim) : Text(""))\(Text(md(staged(r.live).text)))\(cur)")
            .font(chatFace(size: fs)).textSelection(.enabled)
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
    var stats: String {
        let n = r.steps.count, t = max(0, Int(c.now.timeIntervalSince(r.start))), tps = c.tps > 0 ? " · \(Int(c.tps)) tok/s" : ""
        // ponytail: HUD's step number isn't lit apart from the rest of the line
        if lk.id == "hud" { return "STEP \(n == 0 ? "--" : String(format: "%02d", n)) · T+\(String(format: "%02d:%02d", t / 60, t % 60))\(tps.uppercased())" }
        let s = "\(n == 0 ? "No steps yet" : "Step \(n)") · \(fmt(TimeInterval(t)))\(lk.id == "conversation" ? " elapsed" : "")\(tps)"
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
            (Text("you> ").foregroundColor(dim) + t).font(chatFace(size: fs)).foregroundColor(fg).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
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
                (Text("Questions waiting").fontWeight(.semibold) + Text(" · \(w.count)").foregroundColor(dim))
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
            .onChange(of: c.settingsOpen) { rs.projs = delegate.savedProjects }
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
        }
        let status = engineStatus, ready = !status.hasPrefix("Not ready")
        box("Dictation") {
            row("Engine", status) {
                Circle().fill(ready ? Theme.done : Theme.needs).frame(width: 8, height: 8).accessibilityLabel(ready ? "Ready" : "Not ready")
                Picker("Voice engine", selection: $c.engine) { ForEach(engines, id: \.0) { Text($0.1).tag($0.0) } }.labelsHidden().frame(width: menuWidth)
            }
            if c.engine == "whisper" { dependent(off: false) { row("Whisper model") { Button("Choose model…") { delegate.pickModel() } } } }
            if c.engine == "openai" { dependent(off: false) { row("OpenAI key", "Stored in your Keychain.") { Button("Set key…") { delegate.setKey() } } } }
            row("Shortcut", "Press it in any app to dictate to the chat.") { shortcutEditor }
        }
        box("Voice mode") {
            row("Anthropic key", "For the stage: Claude Haiku 5.5 draws what \(c.name) says. Stored in your Keychain; without it the stage shows a plain list.") {
                Button("Set key…") { delegate.askKey("anthropic", "Anthropic API key", "voice mode's stage (Claude Haiku 5.5)") }
            }
            row("Speaking voice", Speech.shared.status.isEmpty ? "How \(c.name) sounds. Kokoro runs on this Mac; ElevenLabs is paid and gets each line \(c.name) says. Either falls back to Apple's voice." : Speech.shared.status) {
                Picker("Speaking voice", selection: Binding(get: { Speech.shared.kind }, set: { ud.set($0, forKey: "speech"); Speech.shared.stop(); Speech.shared.warm(); c.objectWillChange.send() })) {
                    ForEach(Speech.kinds, id: \.0) { Text($0.1).tag($0.0) }
                }.labelsHidden().frame(width: menuWidth)
            }
            if Speech.shared.kind == "kokoro" { dependent(off: false) { row("Kokoro voice", "a = American, b = British; f/m = female/male.") {
                Picker("Kokoro voice", selection: Binding(get: { Speech.shared.kokoroVoice }, set: { ud.set($0, forKey: "kokoroVoice"); c.objectWillChange.send() })) {
                    ForEach(Speech.kokoroVoices, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(width: menuWidth)
            } } }
            if Speech.shared.kind == "eleven" { dependent(off: false) {
                row("ElevenLabs key", "Stored in your Keychain.") { Button("Set key…") { delegate.askKey("elevenlabs", "ElevenLabs API key", "voice mode's speaking voice") } }
                row("ElevenLabs voice ID", "From elevenlabs.io → Voices. Empty: Sarah.") {
                    TextField("EXAVITQu4vr4xnSDxMaL", text: Binding(get: { ud.string(forKey: "elevenVoice") ?? "" }, set: { ud.set($0.trimmingCharacters(in: .whitespaces), forKey: "elevenVoice") })).frame(width: menuWidth)
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
            return keychain("openai") == nil ? "Not ready: no API key. Set one below." : "Ready · key set · model \(m)"
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
            row("Workspace pages", "Builds each workspace's page on the dashboard.") { claudeModelPicker("Workspace pages model", $c.designModel) }
            dependent(off: false) {
                row("Effort") { effortPicker("Workspace pages effort", $c.designEffort) }
                row("Rebuild", c.designParallel == 0 ? "A page builds only when you open it." : "Workspaces whose state changed rebuild in the background.") {
                    if c.designParallel > 0 {
                        Stepper("\(c.designParallel) at once", value: $c.designParallel, in: 1...10).fixedSize().accessibilityLabel("Parallel builds")
                    }
                    Picker("Rebuild pages", selection: Binding(get: { c.designParallel > 0 }, set: { c.designParallel = $0 ? max(c.designParallel, 3) : 0 })) {
                        Text("On demand").tag(false); Text("Background").tag(true)
                    }.labelsHidden().pickerStyle(.segmented).fixedSize()
                }
            }
            row("Next-step suggestions", "Suggests the next action on each hex when its state moves.") { claudeModelPicker("Suggestions model", $c.nextModel) }
            row("Idle check-in", "When you're at your Mac but haven't done anything in Greenlight for this long, the chat suggests what to work on next.") {
                Picker("Idle check-in", selection: $c.checkInMins) { Text("Off").tag(0); Text("15 min").tag(15); Text("30 min").tag(30); Text("1 hour").tag(60) }.labelsHidden().fixedSize()
            }
            row("Watch workspaces", "When a workspace starts needing you, the chat looks into it and asks you a question if one's worth asking.") {
                Toggle("Watch workspaces", isOn: $c.watch).labelsHidden().toggleStyle(.switch)
            }
            dependent(off: false) { row("Effort") { effortPicker("Suggestions effort", $c.nextEffort) } }
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
                            HStack { Spacer(minLength: 60); queued(q, i) }
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
            rule(top: false)
            footer
            }.id(c.look)
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
                meta
                VoiceButtons(v: c.voice, out: c.chatOut, pop: { c.popChat(!c.chatOut) })
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
            Text(shown).font(f).tracking(track).onTapGesture(count: 2) { c.editingName = true }.help("Double-click to rename")
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
            tag("· \(stateWord)", Theme.fs2, .semibold).foregroundColor(stateInk)
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
        let text = lk.id == "phosphor" ? Text("you> ").foregroundColor(dim) + Text(q) : lk.id == "editorial" ? Text("“\(q)”").italic() : Text(q)
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
            if c.hq != nil { histPanel }
            HStack(alignment: .bottom, spacing: Theme.sp2) {
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
                let empty = c.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty  // submit() ignores it anyway
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
            HStack(spacing: Theme.sp2) {
                if onair { modelButton }  // On Air: the model first, inverse, on the amber strip
                ViewThatFits {  // the hint gives way first when the row is tight
                    tag((c.run != nil ? "Sends after this run" : "⌃R history") + " · ↩ send", lk.id == "editorial" ? Theme.fs1 - 1 : Theme.fs1, onair ? .heavy : lk.id == "editorial" ? .semibold : .regular)
                        .foregroundColor(onair ? page : dim).lineLimit(1).fixedSize()
                    EmptyView()
                }.layoutPriority(-1)
                Spacer(minLength: 4)
                if !onair { modelButton }
                ContextButton(c: c)
                newChat
            }.font(sys(size: Theme.fs1).monospacedDigit()).controlSize(.small)
                .padding(.trailing, onair ? inset : 0).background(onair ? accent : .clear).padding(.horizontal, onair ? -inset : 0)  // full-bleed, the model flush left
        }.padding(.horizontal, inset).padding(.top, Theme.sp3).padding(.bottom, lk.id == "onair" ? 0 : Theme.sp4)
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
        return Button(action: c.clear) {
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
        let full = c.ctxFull, high = full > 0.75, pct = Int((full * 100).rounded())
        // the footer's control shape: a capsule (Orb), 6pt (Conversation), a tile (On Air), a box (HUD, Phosphor), bare (Editorial, Zen)
        let r: CGFloat = lk.id == "orb" ? 12 : lk.id == "conversation" ? Theme.r1 : 0, bare = lk.id == "editorial" || lk.id == "zen"
        let up = ["orb", "onair", "hud", "editorial"].contains(lk.id), code = lk.id == "phosphor" || lk.id == "hud"
        return Button { c.ctxOpen.toggle() } label: {
            HStack(spacing: Theme.sp1) {
                ZStack {
                    Circle().stroke(edge, lineWidth: 2)
                    Circle().trim(from: 0, to: max(full, 0.02)).stroke(high ? needs : sub, style: StrokeStyle(lineWidth: 2, lineCap: .round)).rotationEffect(.degrees(-90))
                }.frame(width: 12, height: 12)
                Text(up ? "CONTEXT \(pct)%" : lk.id == "zen" ? "context \(pct)%" : "Context \(pct)%").tracking(up ? 1.2 : 0).lineLimit(1).fixedSize()
                    .font(sys(size: Theme.fs1, weight: lk.id == "onair" ? .heavy : .regular, design: code ? .monospaced : .default).monospacedDigit())
            }.foregroundColor(high ? needsInk : lk.id == "phosphor" ? fg : sub).padding(.horizontal, bare ? 0 : Theme.sp2).frame(height: 24)
                .background(RoundedRectangle(cornerRadius: r).fill(lk.id == "onair" ? card : c.ctxOpen ? accent.opacity(0.18) : lk.id == "conversation" ? card : .clear))  // On Air sits on amber
                .overlay(RoundedRectangle(cornerRadius: r).stroke(high ? needs.opacity(0.6) : c.ctxOpen && lk.id == "onair" ? fg : bare ? .clear : line)).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("Context, \(pct) percent used").modifier(CtxPop(c: c))
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
extension Core { var ctxFull: Double { window > 0 ? min(Double(context) / Double(window), 1) : 0 } }

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
        greenlight.showDockIcon()
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
            self?.broadcast("window.mc && mc.undock(\(k)[0])")
        }
        orbMonitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] e in  // a click brings its window up: put the orbs back on top
            if let self, e.window != nil, e.window !== self.termOrb { self.raiseOrbs() }
            return e
        }
        vimMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard let self, self.capture == nil, let tv = e.window?.firstResponder as? NSTextView, tv.isDescendant(of: self.chat) else { return e }
            if self.greenlight.historyKey(e, tv) {  // ↑↓ / ⌃R history and readline keys, ahead of vim (whose ⌃R would be redo)
                guard self.greenlight.toEnd else { return nil }
                self.greenlight.toEnd = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak w = e.window] in  // caret to the end once the text lands
                    if let tv = w?.firstResponder as? NSTextView { tv.setSelectedRange(NSRange(location: (tv.string as NSString).length, length: 0)) }
                }
                return nil
            }
            guard self.greenlight.vim else { return e }
            let m = e.modifierFlags.intersection([.command, .control, .option])
            if m.contains(.command) || m.contains(.option) { return e }  // ⌘A, ⌘V… still work
            let esc = e.keyCode == kVK_Escape || (m == .control && e.charactersIgnoringModifiers == "[")
            let passthrough = [kVK_Return, kVK_ANSI_KeypadEnter, kVK_Delete, kVK_ForwardDelete, kVK_Tab,
                               kVK_LeftArrow, kVK_RightArrow, kVK_UpArrow, kVK_DownArrow, kVK_Home, kVK_End]
            if !esc && passthrough.contains(Int(e.keyCode)) { return e }  // Enter still sends
            let k = esc ? "\u{1b}" : m == .control ? "^" + (e.charactersIgnoringModifiers ?? "") : e.characters ?? ""
            return self.greenlight.vimKeys.key(k, tv) ? nil : e
        }
        floorMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in  // ⌘↑ up a floor (B2 → B1 → L1 → B2), ⌘↓ down, round the loop
            if let self, self.capture == nil, e.keyCode == kVK_Escape, self.greenlight.voice.on,  // Esc leaves live chat (sheets keep theirs)
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
        greenlight.termOpens == "greenlight" ? show() : basement()  // Settings → Terminal → App opens to
        un.getNotificationSettings { s in NSLog("greenlight notifications: authorization %d, alert %d", s.authorizationStatus.rawValue, s.alertSetting.rawValue) }
    }

    lazy var chat = NSHostingView(rootView: ChatView(c: greenlight))
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
    var webs: [WKWebView] { [fluid] + viewWindows.values.compactMap { $0.contentView as? WKWebView } }
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
        web.evaluateJavaScript(id != nil ? "JSON.stringify(window.term && term.focus && term.focus())" : "JSON.stringify(window.mc && mc.focus && mc.focus())") { [weak self] r, _ in
            guard let self else { return }
            let o = ((r as? String).flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) } as? [String: Any]) ?? [:]
            let line = focusLine(terminal: id, project: id.flatMap { self.floors[$0]?.project } ?? "", o)
            self.greenlight.focusPaths = o["paths"] as? [String]
            guard line != self.greenlight.focus else { return }
            self.greenlight.focus = line
            let key = id != nil ? ("path", o["path"] as? String) : ("name", o["page"] as? String)  // voice mode: the stage follows you
            if let k = key.1, let p = self.greenlight.pucks.first(where: { $0[key.0] as? String == k }) { self.greenlight.voice.show(workspaceSpec(p)) }
        }
    }
    func pushPicks(_ web: WKWebView) {
        guard let d = try? JSONSerialization.data(withJSONObject: picked), let j = String(data: d, encoding: .utf8) else { return }
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
        w.contentView = split
        split.layoutSubtreeIfNeeded()
        split.setPosition(split.bounds.width - 440, ofDividerAt: 0)
        split.autosaveName = "chatSplit"  // restores a dragged divider over the default
        loadFluid()
        if !w.setFrameUsingName("main") { w.center() }
        w.setFrameAutosaveName("main")
        return w
    }()

    func loadFluid() {
        let s = greenlight.fluidURL.trimmingCharacters(in: .whitespaces)
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
        ssh("\(py)/greenlight.py scan >/dev/null", onExit: { [weak self] code in
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
        greenlight.watched(pucks)
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
        if cmd == "unpick" { pick([]) }  // a click on a lit hex clears them in every window
        if cmd == "project" {  // L1's Open projects (or Clear filters: null)
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil || only!.count > 100 { return }
            setPick(only)  // one pick for every floor and window
        }
        if cmd == "window" {  // L1's Open projects, New window
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil { return }
            newTermWindow(only, from: from.window)
        }
        if cmd == "pickNew" { l1Pick(here: false) }  // ⌘⇧N on L1
        if cmd == "editProjects" {  // L1's Edit projects…: down to the Basement, where the editor lives
            wantFloor = "basement"; (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.opens('basement'); window.editProjects && editProjects()"); ride(up: false)
        }
        // A click in one window that belongs to a view in another: forward it there and bring that window up.
        if cmd == "openPage", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            fluid.evaluateJavaScript("mc.openPage(\"\(name)\")"); show()  // the hive lives in the main window
        }
        // A workspace's Basement button: down to B1, on that worktree.
        if cmd == "basement", let path = b["path"] as? String, path.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil, !path.contains("..") { toWorkspace(path) }
        if cmd == "showChat" { setChat(hidden: false) }
        if cmd == "settings" { appSettings() }  // the top bar's gear: its own window, to the front
        if cmd == "inbox" { greenlight.settingsOpen = false; greenlight.inboxOpen.toggle() }
        // An answer from a lifted workspace's page: the same as answering that question in the bell.
        if cmd == "answerPing", let id = b["id"] as? String, let text = b["text"] as? String,
           !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if let q = greenlight.waiting.first(where: { $0.id.uuidString == id }) { b["dismiss"] as? Bool == true ? greenlight.answer(q, "(dismissed)", send: false) : greenlight.answer(q, text) }
            pushedBell = ""; tick()  // re-push even when it was already answered elsewhere, so the page drops its "Sending…"
        }
        if cmd == "toggleChat" { appToggleChat() }
        if cmd == "context", let items = b["items"] as? [[String: Any]] {
            let was = Dictionary(greenlight.attached.map { ($0.id, $0.sent) }, uniquingKeysWith: { a, _ in a })
            greenlight.attached = items.compactMap { i in
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
        // A waiting agent's question (ask) and your reply to it (answer), from the Answer button or the page.
        if cmd == "ask" || cmd == "answer", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            let text = b["text"] as? String ?? ""
            if cmd == "answer" && text.isEmpty { return }
            let remote = cmd == "answer" ? "\(py)/dash.py answer '\(name)'"
                : "\(py)/dash.py ask '\(name)' --model \(shellQuote(greenlight.designModel)) --effort \(shellQuote(greenlight.designEffort))"
            var out = ""
            ssh(remote, stdin: text, onLine: { out += $0 }, onExit: { code in
                let js = out.hasPrefix("{") ? out : #"{"name":"\#(name)","error":"\#(host) didn't answer (exit \#(code))"}"#
                from.evaluateJavaScript("mc.\(cmd == "ask" ? "asked" : "answered")(\(js))")
            })
        }
        // The hover card's Next step: greenlight's next-step prompt for a workspace, and running a prompt there as a new agent.
        if cmd == "park", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil, let on = b["on"] as? Bool {
            var out = ""
            ssh("\(py)/dash.py park '\(name)' \(on ? "on" : "off")", onLine: { out += $0 }, onExit: { code in
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
            let op = cmd == "undraft" ? "ready" : cmd
            ssh("\(py)/dash.py tree \(op) '\(path)'", onLine: { out += $0 }, onExit: { code in
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
            ssh("\(py)/dash.py run '\(name)'", stdin: prompt, onLine: { out += $0 }, onExit: { code in
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
        _ = menu("File", [("New Window", "appNewWindow", "n")], target: self)
        _ = menu("Edit", [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("-", "", ""), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                          ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")])
        NSApp.windowsMenu = menu("Window", [("Minimize", "performMiniaturize:", "m"), ("Zoom", "performZoom:", ""), ("Close", "performClose:", "w"),
                                            ("-", "", ""), ("Show/Hide Chat", "appToggleChat", "C"), ("Live Chat", "appLive", "L"),
                                            ("Up to Greenlight Center", "appGoUp", "G"), ("Basement (Terminal)", "appToggleTerm", "T"), ("Jump to Terminal…", "appJumpTerm", "j"),
                                            ("Reload Dashboard", "appReload", "r")], target: self)
        NSApp.mainMenu = bar
    }
    @objc func appNewWindow() { newTermWindow(sharedOnly, from: NSApp.keyWindow.flatMap { termWins.values.contains($0) ? $0 : nil }) }  // the shared pick
    @objc func appSettings() { greenlight.settingsOpen = true; greenlight.inboxOpen = false }
    @objc func appReload() { loadFluid() }
    @objc func appToggleTerm() { toggleTerm() }
    @objc func appJumpTerm() {  // ⌘J from any window: the terminal with its jump palette open (the page catches ⌘J itself when it has focus)
        if !(termWindow.isVisible && termWindow.isKeyWindow) { toggleTerm() }
        (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.palette(true)")
    }
    var lastToggle = Date.distantPast
    @objc func appLive() {  // ⇧⌘L: live chat on (showing the chat first) or off
        if !greenlight.voice.on { setChat(hidden: false); NSApp.activate(ignoringOtherApps: true) }
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
    var l1Picked: [[String: Any]] { (l1Only ?? []).compactMap { id in projList.first { $0["id"] as? String == id } } }
    var l1Paths: [String]? { l1Only == nil ? nil : Array(Set(l1Picked.flatMap { $0["paths"] as? [String] ?? [] })).sorted() }  // sorted: the tick compares it
    func syncL1() {
        let ps = l1Picked, names = ps.compactMap { $0["name"] as? String }.joined(separator: ", ")
        l1Floor.project = l1Only == nil ? "All projects" : names.isEmpty ? "No projects" : names
        l1Floor.colors = l1Only == nil ? [] : ps.compactMap { $0["color"] as? String }
    }
    func l1Pick(here: Bool = true) {
        let list = (try? JSONSerialization.data(withJSONObject: projList)).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
        let only = (l1Only.flatMap { try? JSONSerialization.data(withJSONObject: $0) }).flatMap { String(data: $0, encoding: .utf8) } ?? "null"
        fluid.evaluateJavaScript("window.mc && mc.pickProjects && mc.pickProjects(\(list), \(only), \(here))")
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
            ssh("mkdir -p /tmp/greenlight && base64 -d > \(shellQuote(path))", on: at.host, stdin: data) { [weak web] code in
                web?.evaluateJavaScript("term.filed(\(tid), \(js(code == 0 ? path : "")))")
            }
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
                    later(reduce ? 0.22 : 0.3) { [self] in to.alphaValue = 1; if !reduce { at(0, 0.18) { s.veil = 0 } } }
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
        let repos = l1Paths.flatMap { try? JSONSerialization.data(withJSONObject: $0) }.flatMap { String(data: $0, encoding: .utf8) } ?? "null"
        if pushedRepos != repos { pushedRepos = repos; broadcast("window.mc && mc.paths && mc.paths(\(repos))") }
        if orbState.matte != (greenlight.orbStyle == "matte") { orbState.matte = greenlight.orbStyle == "matte" }
        if pushedLocal != greenlight.localShown { pushedLocal = greenlight.localShown; let js = (try? JSONEncoder().encode(pushedLocal)).flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
            termWebs.forEach { $0.evaluateJavaScript("window.term && term.localName(\(js))") } }
        if pushedSlide != greenlight.sidebarSlide { pushedSlide = greenlight.sidebarSlide; termWebs.forEach { $0.evaluateJavaScript("window.term && term.slide(\(greenlight.sidebarSlide))") } }
        if pushedMotion != greenlight.rideTime { pushedMotion = greenlight.rideTime
            (Array(termWins.values) + [window]).forEach { $0.animationBehavior = greenlight.rideTime > 0 ? .default : .none } }  // Animations off: no zoom in or out as a level comes or goes
        let chat = "\(!chatHidden),\(greenlight.missed)"
        if pushedChat != chat { pushedChat = chat; broadcast("window.mc && mc.chat(\(chat))") }
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

    @objc func setKey() { askKey("openai", "OpenAI API key", "the OpenAI Whisper engine") }
    func askKey(_ account: String, _ title: String, _ use: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = "Stored in your login Keychain. Used only for \(use). Leave empty to remove it."
        let f = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        a.accessoryView = f
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = f
        NSApp.activate(ignoringOtherApps: true)
        if a.runModal() == .alertFirstButtonReturn { keychain(account, set: f.stringValue.trimmingCharacters(in: .whitespaces)) }
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

/// `Greenlight.app/Contents/MacOS/Greenlight --selftest`: drives the actions the buttons call
/// (send, Stop, answer buttons, Compact, Codex, Clear) on a scratch session where greenlight runs, plus the menubar width.
func selfTest() {
    let d = UserDefaults.standard
    let keep = ["model", "effort", "context", "window", "name", "seen", "sent"].map { ($0, d.object(forKey: $0)) }
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
    let st = staged("Three MRs.\n```stage\n<b>hi</b>\n```\n[stage: full]\nDone."), open = staged("Here.\n```stage\n<ul><li>half")
    let hp: [[String: Any]] = [["name": "acme/mr-5339", "mr": ["ref": "!5339", "title": "pending liquidity receipts"]], ["name": "selkie"], ["name": "mr-1802"]]
    check("hexes: Chief's [hexes: …] line, stripped and resolved; the model's picks only when the reply points at them",
          staged("Merge it.\nOptions: yes | no\n[hexes: selkie, !5339, nope]").hexes == ["selkie", "!5339", "nope"]
          && staged("Merge it.\nOptions: yes | no\n[hexes: selkie]").text == "Merge it.\nOptions: yes | no"
          && staged("Nothing.\n[hexes:]").hexes == [] && staged("Hi").hexes == nil && staged("Hi [hexes: sel").text == "Hi"
          && resolveHexes(["selkie", "!5339", "mr-5339", "nope"], hp) == ["selkie", "acme/mr-5339"]
          && grounded("re-review merge request 5339", hp[0]) && grounded("the pending liquidity receipts change", hp[0])
          && grounded("I'd highlight selkie", hp[1]) && !grounded("Okay, let me know what's next.", hp[2]) && !grounded("about 18020 things", hp[2]))
    check("voice: Chief ends voice mode with [voice: off]", staged("Bye for now.\n[voice: off]") == Staged(text: "Bye for now.", stage: nil, end: true))
    check("voice: every look has its own id and themes the stage", Set(looks.map(\.id)).count == looks.count && looks.allSatisfy { $0.vars.contains("--mc-font") && $0.vars.contains("--mc-line") })
    check("voice: stage block and size cut out, an unclosed one hides the rest", st == Staged(text: "Three MRs.\n\n\nDone.", stage: "<b>hi</b>") && open.text == "Here." && open.stage == nil)
    check("voice: \(waitWords.count) ways to say one minute, none twice", waitWords.count > 300 && Set(waitWords).count == waitWords.count && waitWords.allSatisfy { $0.first!.isUppercase && $0.hasSuffix("…") })
    check("voice: mood emoji aren't spoken, numbers are", plain("😌🔍❤️\n#3 is 2 of 5.") == "\n#3 is 2 of 5.")
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
    check("focus: Chief hears which window and workspace you were in",
          focusLine(terminal: 2, project: "Platform", ["name": "mr-5339", "path": "/w/mr-5339", "mr": "!5339"]) == "a Basement terminal window showing Platform, on the tab for workspace mr-5339 (/w/mr-5339, !5339)"
          && focusLine(terminal: nil, project: "", ["page": "acme/mr-5339", "view": "overview"]) == "the dashboard, with workspace acme/mr-5339 open"
          && focusLine(terminal: nil, project: "", ["view": "list"]) == "the dashboard (list)")
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
    let c = Core()
    Thread.detachNewThread {
        onMain { c.clear(); c.questions = []; c.model = "claude-haiku-5-5"; c.effort = "low" }
        Thread.sleep(forTimeInterval: 3)

        onMain { c.send("Run this exact bash command: python3 -c \"import time; time.sleep(60)\" then say finished.") }
        wait("a live step", 90) { !(c.run?.steps.isEmpty ?? true) }
        check("live step while working: \(onMain { c.run?.steps.first?.title ?? "-" })", onMain { c.run != nil })
        check("live line names the call: \(onMain { c.run?.call ?? "-" })", onMain { c.run?.call.contains("(") ?? false })
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
if selftest { selfTest(); app.setActivationPolicy(.accessory) } else { app.delegate = delegate; app.setActivationPolicy(.regular) }
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
