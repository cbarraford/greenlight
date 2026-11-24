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

// MARK: model

struct SystemOneSaveQueue {
    private var commands: [String] = []
    private(set) var busy = false
    var empty: Bool { commands.isEmpty }
    mutating func enqueue(_ command: String) -> String? {
        commands.append(command)
        return start()
    }
    mutating func finished() -> String? {
        busy = false
        return start()
    }
    private mutating func start() -> String? {
        guard !busy && !commands.isEmpty else { return nil }
        busy = true
        return commands.removeFirst()
    }
}

func fallbackAfterPrimaryChange(from old: String, to new: String, fallback: String) -> String {
    new != "local" && new == fallback ? (old == "local" ? "" : old) : fallback
}

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
    var suggest: Suggest? = nil  // autonomy.py's tier 2: the option you'd likely pick, preselected with a badge
    var auto: String? = nil  // autonomy.py's tier 1: greenlight answered it for you; the audit id Undo sends back
}

/// autonomy.py's guess at a ping's answer: the option, how likely, and how many past answers of its kind (agree: picked it).
struct Suggest: Codable { let choice: String; let prob: Double; let count: Int; let agree: Int }

/// `autonomy.py settings --json`: which kinds greenlight may answer for you, how its guesses did, and what it answered lately.
struct AutonomyReport: Decodable {
    struct Kind: Decodable { let kind: String; let eligible: Bool; let opted: Bool; let n: Int?; let right: Int?; let acc: Double? }
    struct Candidate: Decodable { let kind: String; let n: Int; let right: Int }
    struct Act: Decodable, Identifiable { let id: String; let ts: Double; let kind: String; let choice: String; let prob: Double; let text: String?; let undone: Double? }
    let auto: [String], gate: Double, kinds: [Kind], candidates: [Candidate], recent: [Act]
}

/// The transcript survives restarts; the conversation itself lives in chat.py's session file on greenlight's host.
let support: URL = {
    // --unittest gets a per-process dir: its fixed-session transcripts outlived cancelled CI runs and failed the next one
    let dir = unittest ? FileManager.default.temporaryDirectory.appendingPathComponent("greenlight-unittest-\(ProcessInfo.processInfo.processIdentifier)")
        : uitest
        ? URL(fileURLWithPath: (greenlightHome as NSString).expandingTildeInPath).appendingPathComponent("app-support")
        : FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/\(appName)")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir
}()  // a copy's own: its tabs' tmux sessions die with its tabs
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
    var spoken = false  // said out loud in voice mode: its files went with it from the stage's tray
    var model = ""  // the --model it was sent with: the picker can change mid-turn
    var open: URL?  // an [open: URL] from a reply block a later one replaced (text_reset)
    var turnId: String?  // chat.py's id for it (its "turn" event): followed again after a dropped connection or a relaunch
    var refollows = 0  // times followed again since the connection dropped
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

