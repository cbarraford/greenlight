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

final class Core: ObservableObject {
    /// 0: the main chat. n > 0: a second Greenlight Center window's own Chief (chat.py's CHAT_DIR chat-<n>, its own transcript and questions).
    let session: Int
    let transcriptFile: URL, questionsURL: URL
    init(session: Int = 0) {
        self.session = session
        transcriptFile = session == 0 ? transcript : support.appendingPathComponent("transcript-\(session).json")
        questionsURL = session == 0 ? questionsFile : support.appendingPathComponent("questions-\(session).json")
        msgs = ((try? JSONDecoder().decode([Msg.Saved].self, from: Data(contentsOf: transcriptFile)))?.map(Msg.init)) ?? msgs
        if session == 0 && ud.string(forKey: "model") == "gemini-4-neon" {
            ud.set("gemini-4-neon-medium", forKey: "model")
            msgs.append(Msg(.note, "Gemini chat now uses agy, which starts a new conversation. Your earlier Gemini CLI conversation remains available by selecting Gemini CLI."))
            try? FileManager.default.createDirectory(at: transcriptFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? JSONEncoder().encode(msgs.suffix(300).map(\.saved)).write(to: transcriptFile, options: .atomic)
        }
        questions = (try? JSONDecoder().decode([Question].self, from: Data(contentsOf: questionsURL))) ?? []
        context = ud.integer(forKey: meterKeys.context); window = ud.integer(forKey: meterKeys.window)
    }
    /// This chat's context meter in UserDefaults: the main chat keeps the old keys, a second window's chat has its own.
    var meterKeys: (context: String, window: String) { session == 0 ? ("context", "window") : ("context-\(session)", "window-\(session)") }
    var chief: String { session == 0 ? chatCmd : "CHAT_DIR=\(shellPath(greenlightHome))/chat-\(session) \(py)/chat.py" }
    var light: ([String], Bool) -> Void = { delegate.pick($0, open: $1) }  // the hexes a reply is about, lit in this chat's dashboard; true: open the lone one's card
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
    /// spoken: a voice-mode turn. It takes the tray's files (and refusals) when said; waits: files were still being read then, so it takes
    /// the tray's files when it goes, and holds until they're read.
    struct Queued { let text: String; var shown: String? = nil; var files: [ChatFile] = []; var spoken = false, waits = false; var refused: [String] = []; var note = ""; var display: String { shown ?? text } }  // note: for Chief, not shown
    @Published var files: [ChatFile] = []  // attached in the composer, sent with the next message
    @Published var fileNote = ""  // why the last file didn't attach; clears itself
    @Published var dropping = false  // files dragged over the chat
    @Published var reading = 0 { didSet { if reading == 0 { next() } } }  // files being read and checked off the main thread; Send (and a spoken turn) waits for them
    let reader = DispatchQueue(label: "greenlight.attach", qos: .userInitiated)  // one at a time: files land in the order they came
    func attach(_ urls: [URL]) { read { urls.map(chatFile(at:)) } }
    func attach(_ data: Data, name: String) { read { [chatFile(data, name: name)] } }
    private func read(_ work: @escaping () -> [(file: ChatFile?, error: String?)]) {
        reading += 1
        reader.async { [weak self] in let rs = work(); DispatchQueue.main.async { rs.forEach { self?.add($0) }; self?.reading -= 1 } }  // added before the count drops: a turn waiting on it goes then
    }
    private func add(_ r: (file: ChatFile?, error: String?)) {
        guard let f = r.file else { return refuse(r.error ?? "That file couldn't be attached.") }
        if files.count >= ChatFile.maxFiles { return refuse("\(f.name) wasn't attached: attach up to \(ChatFile.maxFiles) files at a time.") }
        if files.reduce(f.data.count, { $0 + $1.data.count }) > ChatFile.maxTotal { return refuse("\(f.name) wasn't attached: these files come to more than \(byteSize(ChatFile.maxTotal)).") }
        files.append(f)
        fileNote = ""; refused.removeAll { $0.hasPrefix("\(f.name) ") }  // it's attached now
        log(.chief, name, .status, "attached a file (\(ChatFile.kinds[f.kind]!), \(byteSize(f.data.count)))")  // never its name's folder, never its contents
    }
    /// Files that didn't attach since the last message: shown, and in voice mode told to Chief with the next spoken turn, so it says so out loud.
    var refused: [String] = []
    private func refuse(_ why: String) { refused.append(why); fileSays(why) }
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
        final class Left { var n = 0, held = 0, late = false }  // n: files still to come (the promise queue's); held: of `reading` (the main thread's)
        let left = Left(), q = OperationQueue()
        q.maxConcurrentOperationCount = 1
        left.n = ps.reduce(0) { $0 + max(1, $1.fileNames.count) }
        left.held = left.n; reading += left.n  // Send waits for them, but not forever: a promise may never be kept
        let release = { [weak self] (k: Int) in let k = min(k, left.held); left.held -= k; self?.reading -= k }
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self] in  // what hasn't come by then is dropped, and said: a turn waiting on it goes without it
            if left.held > 0 { left.late = true; self?.refuse("\(left.held == 1 ? "A dropped file" : "\(left.held) dropped files") didn't arrive in 30 seconds: drop \(left.held == 1 ? "it" : "them") again.") }
            release(left.held)
        }
        for r in ps {
            r.receivePromisedFiles(atDestination: dir, options: [:], operationQueue: q) { [weak self] url, err in
                let got = err == nil ? chatFile(at: url) : (nil, "A dropped file couldn't be received.")
                left.n -= 1
                if left.n <= 0 { try? FileManager.default.removeItem(at: dir) } else { try? FileManager.default.removeItem(at: url) }
                DispatchQueue.main.async { if !left.late { self?.add(got) }; release(1) }  // a late one would ride with something said after
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
    @Published var model = savedModel("model", fallback: "claude-opus-5-5") {
        didSet {
            ud.set(model, forKey: "model")
            if harnessOf(model) != harnessOf(oldValue) {
                msgs.append(Msg(.note, isClaude ? "Back to the Claude conversation." : "\(modelName) keeps its own conversation, separate from Claude's."))
                context = 0; window = 0; run?.meterStale = true  // the meter was the other harness's: tokens only until this one reports
            }
        }
    }
    @Published var effort = ud.string(forKey: "effort") ?? "medium" { didSet { ud.set(effort, forKey: "effort") } }
    // The agent that designs dashboard pages and puck widgets: a Claude model, or a Gemini one through agy.
    @Published var designModel = savedModel("designModel", fallback: "claude-haiku-5-5") { didSet { ud.set(designModel, forKey: "designModel") } }
    @Published var designEffort = ud.string(forKey: "designEffort") ?? "low" { didSet { ud.set(designEffort, forKey: "designEffort") } }
    /// The terminal's tab names: written by this model once a tab's output settles, unless you named it.
    @Published var termNameModel = ud.string(forKey: "termNameModel") ?? "claude-haiku-5-5" { didSet { ud.set(termNameModel, forKey: "termNameModel") } }
    @Published var termAutoName = ud.object(forKey: "termAutoName") as? Bool ?? true { didSet { ud.set(termAutoName, forKey: "termAutoName") } }
    // The agent that writes the hover card's next step (dash.py suggest), ahead of time for every puck whose state moved.
    @Published var nextModel = savedModel("nextModel", fallback: "claude-haiku-5-5") { didSet { ud.set(nextModel, forKey: "nextModel") } }
    @Published var nextEffort = ud.string(forKey: "nextEffort") ?? "low" { didSet { ud.set(nextEffort, forKey: "nextEffort") } }
    @Published var designParallel = ud.object(forKey: "designParallel") as? Int ?? 3 { didSet { ud.set(designParallel, forKey: "designParallel") } }
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
    @Published var name = ud.string(forKey: "name") ?? "Greenlight" { didSet { ud.set(name, forKey: "name") } }
    @Published var editingName = false
    @Published var hint = ""  // hover text for the footer buttons; native tooltips are slow
    @Published var voiceTest = ""  // Settings' Test of the speaking voice: what happened
    @Published var keying = false  // the next chord becomes the voice shortcut: Settings shows the recorder in place
    static let keyPrompt = "Press the new voice shortcut, with ⌃, ⌥ or ⌘ (Esc cancels)"
    /// The menu's voice-shortcut prompt is up (Settings' Change… shows its own recorder): hover hints leave it alone until a key comes.
    var prompting: Bool { keying && hint == Core.keyPrompt }
    @Published var questions: [Question] = [] {  // init reads questionsURL
        didSet {  // all waiting ones plus the last 20 answered
            let keep = questions.filter { $0.answer == nil } + questions.filter { $0.answer != nil }.suffix(20)
            try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
            try? JSONEncoder().encode(keep.sorted { $0.ts < $1.ts }).write(to: questionsURL, options: .atomic)
        }
    }
    @Published var inboxOpen = false { didSet { if !inboxOpen { inboxConfirm = false } } }
    @Published var inboxPick: UUID?  // the inbox's open row (inboxFocus), ↑↓ to move
    @Published var inboxConfirm = false  // Clear all's "Dismiss all N?" in the footer
    @Published var replyingTo: UUID?
    @Published var replyText = ""
    @Published var engine = ud.string(forKey: "engine") ?? "apple" { didSet { ud.set(engine, forKey: "engine") } }
    @Published var voiceMods = ud.object(forKey: "voiceMods") as? Int ?? (controlKey | optionKey) { didSet { ud.set(voiceMods, forKey: "voiceMods") } }
    @Published var voiceCode = ud.object(forKey: "voiceCode") as? Int ?? kVK_ANSI_V { didSet { ud.set(voiceCode, forKey: "voiceCode") } }
    @Published var voiceKey = ud.string(forKey: "voiceKey") ?? "V" { didSet { ud.set(voiceKey, forKey: "voiceKey") } }
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
        ssh("command -v python3 >/dev/null || { echo nopy; exit 3; }; test -f \(shellPath(code))/scripts/greenlight.py && echo ok", on: h, onLine: { out = $0 }) { [weak self] rc in
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

    // Settings › Autonomy: autonomy.py on the host owns it (autonomy.json); this is the last report read
    @Published var autonomy: AutonomyReport?
    @Published var autonomyNote = ""
    func loadAutonomy() {
        var out = ""
        ssh("\(py)/autonomy.py settings --json", onLine: { out += $0 }, onExit: { [weak self] code in
            guard let self else { return }
            if code == 0, let r = try? JSONDecoder().decode(AutonomyReport.self, from: Data(out.utf8)) { self.autonomy = r; self.autonomyNote = "" }
            else { self.autonomyNote = "Couldn't read autonomy settings from \(host)." }
        })
    }
    /// autonomy.py set's arguments (`["--auto", "milestone", "on"]`, `["--gate", "0.35"]`), then the report again.
    func setAutonomy(_ args: [String]) {
        ssh("\(py)/autonomy.py set \(args.map(shellQuote).joined(separator: " "))", onExit: { [weak self] code in
            if code != 0 { self?.autonomyNote = "Couldn't save that on \(host)." }
            self?.loadAutonomy()
        })
    }
    func undoAudit(_ id: String) {
        ssh("\(py)/autonomy.py undo \(shellQuote(id))", onExit: { [weak self] code in
            guard let self else { return }
            if code != 0 { self.autonomyNote = "Couldn't ask again on \(host)." }
            else {
                for i in self.questions.indices where self.questions[i].auto == id {
                    self.questions[i].auto = nil
                    self.questions[i].answer = "Reconsidering: " + (self.questions[i].answer ?? "").replacingOccurrences(of: "Auto: ", with: "")
                }
            }
            self.loadAutonomy()
        })
    }

    func setGreenlightEvery(_ m: Int) {
        greenlightEvery = m
        ud.set(m, forKey: "greenlightEvery")
        ssh("\(py)/auto.py edit greenlight " + (m == 0 ? "--disabled" : "--enabled --schedule '*/\(m) * * * *'"),
            onExit: { [weak self] code in DispatchQueue.main.async { self?.greenlightEveryNote = code == 0 ? "" : "Couldn't save the schedule on \(host)." } })
    }

    // System One (decide.py where greenlight runs): the model for its quick decisions. config.json's system_one is the truth; this is the last read.
    @Published var s1Provider = "local"
    @Published var s1Fallback = ""
    @Published var s1Key = false
    @Published var s1Loading = false
    @Published var s1Note = ""  // why the last read or save failed
    @Published var s1Test = ""  // the last Test's result
    private var s1Saves = SystemOneSaveQueue()
    private var s1Pending = 0
    private var s1Version = 0
    private var s1WriteError = false

    func loadSystemOne() {
        guard !s1Saves.busy && s1Saves.empty else { return }  // the final queued save reads the result
        systemOne("config", readConfig: true)
    }
    func setSystemOne(provider: String? = nil, fallback: String? = nil) {
        var saveFallback = fallback
        if let provider {
            let adjusted = fallbackAfterPrimaryChange(from: s1Provider, to: provider, fallback: s1Fallback)
            if fallback == nil && adjusted != s1Fallback { s1Fallback = adjusted; saveFallback = adjusted }
            s1Provider = provider
        }
        if let fallback { s1Fallback = fallback }
        s1Version += 1
        let cmd = "config" + (provider.map { " --provider \(shellQuote($0))" } ?? "") + (saveFallback.map { " --fallback \($0.isEmpty ? "none" : shellQuote($0))" } ?? "")
        if let first = s1Saves.enqueue(cmd) { saveSystemOne(first) }
    }
    private func saveSystemOne(_ cmd: String) {
        systemOne(cmd, finished: { [weak self] in
            guard let self else { return }
            if let next = self.s1Saves.finished() { self.saveSystemOne(next) }
            else { self.loadSystemOne() }
        })
    }
    /// The TypeSafe key goes to decide.py on greenlight's host (its secrets file, 0600) over stdin: never on a command line.
    func setSystemOneKey(_ v: String) { systemOne(v.isEmpty ? "key --clear" : "key", stdin: v) }
    func systemOne(_ cmd: String, stdin: String = "", readConfig: Bool = false, finished: (() -> Void)? = nil) {
        var out = ""
        let version = s1Version
        s1Pending += 1
        s1Loading = true
        ssh("\(py)/decide.py \(cmd)", stdin: stdin, onLine: { out += $0 }, onExit: { [weak self] code in
            guard let self else { return }
            self.s1Pending -= 1
            self.s1Loading = self.s1Pending > 0
            defer { finished?() }
            guard code == 0, let a = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: Any] else {
                if cmd.hasPrefix("config --") { self.s1WriteError = true }
                self.s1Note = "Couldn't reach decide.py on \(host)."; return
            }
            if readConfig && version == self.s1Version {
                if let p = a["provider"] as? String { self.s1Provider = p }
                if a.keys.contains("fallback") { self.s1Fallback = a["fallback"] as? String ?? "" }
            }
            if let k = a["key"] as? Bool { self.s1Key = k }
            if readConfig {
                self.s1Note = self.s1WriteError ? "Couldn't save a System One setting on \(host)." : ""
                self.s1WriteError = false
            } else if !cmd.hasPrefix("config --") { self.s1Note = "" }
        })
    }
    /// `decide.py test`: each model in the chain asked a question with a known answer; the result in a line.
    func testSystemOne() {
        var out = ""
        s1Test = "Asking…"
        ssh("\(py)/decide.py test", onLine: { out += $0 }, onExit: { [weak self] _ in
            let a = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: Any]
            let rows = a?["results"] as? [[String: Any]] ?? []
            self?.s1Test = a == nil ? "✗ No answer from decide.py on \(host)." : rows.isEmpty ? "Off: nothing to ask. Greenlight's own patterns decide."
                : rows.map { r in
                    let p = (r["provider"] as? String ?? "?").capitalized, ms = r["ms"] as? Int ?? 0
                    return r["ok"] as? Bool == true ? "✓ \(p) answered in \(ms) ms" : "✗ \(p): \((r["error"] as? String).map { String($0.prefix(120)) } ?? "wrong answers")"
                }.joined(separator: " · ")
        })
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
    func notificationQuestion(_ about: String, id: UUID? = nil) -> Question? {
        if let id { return waiting.first { $0.id == id && $0.ping } }
        return waiting.first { $0.text == about }
    }
    func expiredNotification(_ about: String, reply: String) {
        let message = "That notification is no longer open. Copy your reply below to resend it.\nNotification: \(about)\nReply: \(reply)"
        msgs.append(Msg(.note, message))
        if !popoverShown() { missed += 1 }
        notify(title: name, body: "That notification is no longer open. Your reply is saved in chat.", subtitle: nil, reply: false, id: "expired:\(about)")
    }
    func reply(to about: String, _ text: String, questionID: UUID? = nil, freeform: Bool = false) {
        if let questionID {
            guard let q = notificationQuestion(about, id: questionID) else {
                expiredNotification(about, reply: text)
                return
            }
            unread = max(0, unread - 1)
            answer(q, text)
            return
        }
        guard freeform else {  // older banners have no reliable ping identity, so answer from the current bell instead
            expiredNotification(about, reply: text)
            return
        }
        unread = max(0, unread - 1)
        if let q = waiting.first(where: { !$0.ping && $0.text == about }) { answer(q, text) }
        else { send("(Replying to your notification: \"\(about)\")\n\(text)", shown: text) }
    }

    /// Answers an inbox question (chip, reply field, banner reply or chat chip). Any chat answer closes the other chat questions.
    func answer(_ q: Question, _ text: String, send doSend: Bool = true) {
        for i in questions.indices where questions[i].answer == nil && (questions[i].id == q.id || (!q.ping && !questions[i].ping)) {
            questions[i].answer = text
        }
        if replyingTo == q.id { replyingTo = nil; replyText = "" }
        if doSend { send(q.ping ? "(Replying to your question: \"\(q.text)\")\n\(text)" : text, shown: text) }
        if !doSend && text == "(dismissed)" && !selftest, let d = try? JSONSerialization.data(withJSONObject: dismissal(q)) {
            ssh("\(py)/decisions.py dismiss", stdin: String(decoding: d, as: UTF8.self))  // the ✕: autonomy.py's gate counts it against its kind
        }
    }
    private func dismissal(_ q: Question, _ reason: String = "dismissed") -> [String: Any] {
        ["text": q.text, "key": q.key as Any? ?? NSNull(), "ping": q.ping, "chat": session == 0 ? "" : "chat-\(session)", "reason": reason]
    }
    /// The inbox's Clear all: dismisses every waiting question without sending. decisions.py closes them as "cleared", which the gate doesn't count.
    func clearInbox() {
        let w = waiting
        questions = questions.map { var q = $0; if q.answer == nil { q.answer = "(dismissed)" }; return q }  // one write, not one per question
        replyingTo = nil; replyText = ""; inboxConfirm = false; inboxPick = nil
        if !w.isEmpty && !selftest, let d = try? JSONSerialization.data(withJSONObject: w.map { dismissal($0, "cleared") }) {
            ssh("\(py)/decisions.py dismiss", stdin: String(decoding: d, as: UTF8.self))
        }
    }
    /// The inbox row that's open: the one picked, else the oldest.
    var inboxFocus: Question? { let w = waiting; return w.first { $0.id == inboxPick } ?? w.first }
    /// ✕ on the open row, then the next one opens in its place.
    func dismiss(_ q: Question) {
        let i = waiting.firstIndex { $0.id == q.id } ?? 0
        answer(q, "(dismissed)", send: false)
        let w = waiting; inboxPick = w.isEmpty ? nil : w[min(i, w.count - 1)].id
    }
    /// The inbox's keys (App's key monitor, while it's open and nothing is being typed): ↑↓ move, 1–9 answer, R reply, ⌫ dismiss,
    /// ⇧⌫ Clear all (Return confirms, Esc cancels). True = handled.
    func inboxKey(_ e: NSEvent) -> Bool {
        let m = e.modifierFlags.intersection([.command, .control, .option, .shift]), k = Int(e.keyCode), w = waiting
        if inboxConfirm {
            if m.isEmpty && (k == kVK_Return || k == kVK_ANSI_KeypadEnter) { clearInbox() }
            else if k == kVK_Escape { inboxConfirm = false } else { return false }
            return true
        }
        guard let q = inboxFocus, let i = w.firstIndex(where: { $0.id == q.id }) else { return false }
        if m == .shift && k == kVK_Delete { inboxConfirm = true; return true }
        guard m.isEmpty else { return false }
        switch k {
        case kVK_UpArrow, kVK_DownArrow: inboxPick = w[max(0, min(w.count - 1, i + (k == kVK_UpArrow ? -1 : 1)))].id
        case kVK_Delete, kVK_ForwardDelete: dismiss(q)
        case kVK_ANSI_R: replyText = ""; replyingTo = q.id
        default:
            guard let n = Int(e.charactersIgnoringModifiers ?? ""), n >= 1, n <= min(9, q.options.count) else { return false }
            answer(q, q.options[n - 1])
        }
        return true
    }

    /// Takes back an answer greenlight gave for you (autonomy.py undo): the question comes back to the inbox as a new ping.
    func undoAuto(_ q: Question) { if let aid = q.auto { undoAudit(aid) } }

    /// autonomy.py's anti-slop gate for a question nobody asked for (the idle check-in, the watch): may it interrupt now?
    /// Only a clear "held" stops it: a host that can't say (ssh down, a checkout without autonomy.py) lets it through.
    func gate(_ kind: String, text: String, _ go: @escaping (Bool) -> Void) {
        var out = ""
        ssh("\(py)/autonomy.py gate --kind \(shellQuote(kind)) \(shellQuote(text))", onLine: { out += $0 }, onExit: { [weak self] code in
            let held = code == 1 && out.contains("\"ok\": false")
            if held, let self { self.log(.chief, self.name, .status, "\(kind) held by the ping gate: \(out)") }
            go(!held)
        })
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
    var startingVoice = false
    func toggleVoice() {
        if voice.on || voice.starting { return }  // voice mode has the mic, or is about to
        if recording != nil { stopVoice() } else if !transcribing && !startingVoice { startVoice() }
    }

    func startVoice() {
        startingVoice = true
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] ok in DispatchQueue.main.async {
            guard let self else { return }
            self.startingVoice = false
            guard self.recording == nil, !self.transcribing else { return }
            guard ok else { return self.msgs.append(Msg(.note, "Microphone access is off: System Settings → Privacy & Security → Microphone → Greenlight.")) }
            let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1,
                                           AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false]
            guard let r = try? AVAudioRecorder(url: voiceFile, settings: settings), r.record() else {
                return self.msgs.append(Msg(.note, "Couldn't start the microphone."))
            }
            r.isMeteringEnabled = true
            self.recorder?.stop()
            self.recorder = r
            self.recording = Date()
            self.meter?.invalidate()
            self.meter = Timer.scheduledTimer(withTimeInterval: 0.06, repeats: true) { [weak self, weak r] _ in
                guard let r else { return }
                r.updateMeters()
                self?.levels = Array((self?.levels ?? []).dropFirst()) + [max(0, min(1, (r.averagePower(forChannel: 0) + 50) / 45))]
            }
        }}
    }

    func stopVoice() {
        startingVoice = false
        recorder?.stop()
        recorder = nil
        meter?.invalidate()
        meter = nil
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
        input = ""; files = []; fileNote = ""; refused = []  // the composer showed them
        if vim { vimKeys.mode = vimStart }
        let q = t.isEmpty ? nil : waiting.last(where: { !$0.ping })  // typing answers the open chat question
        if q == nil && fs.isEmpty && attached.allSatisfy(\.sent) && dwimCandidate(t) {  // a board command: done now, or Chief's if dwim.py won't
            return dwim(t, source: "chat") { [weak self] note in self?.send(note + t, shown: t) }
        }
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

    /// A turn said out loud in voice mode. It goes through the queue, in the order said, with the files waiting in the composer
    /// (dropped, pasted or picked on the stage) as a typed message does. Files still being read hold it until they land or fail
    /// (a promised file gives up after 30 s); then it takes the tray as it goes. Clear cancels it like any queued message, and so do
    /// "stop" and "bye" while it waits on a file (during an answer, "stop" stops the answer and what was said after still goes).
    func spoken(_ text: String, note: String = "") {
        if reading > 0 { queue.append(Queued(text: text, spoken: true, waits: true, note: note)) }
        else { queue.append(Queued(text: text, files: files, spoken: true, refused: refused, note: note)); files = []; refused = [] }  // a later drop is for a later turn
        if reading > 0 && run == nil && clearing == 0 { voice.wait("Reading your file", "It goes with what you said, once it's read.") }
        next()
    }
    /// The next queued message, when nothing is running: a spoken one waits for files still being read.
    func next() {
        guard run == nil, clearing == 0, let q = queue.first, !(q.waits && reading > 0) else { return }
        queue.removeFirst()
        let fs = q.waits ? files : q.files, no = q.waits ? refused : q.refused
        if q.waits { files = []; refused = [] }
        let told = no.isEmpty ? "" : "(Greenlight, not said by the user: files they tried to attach weren't: \(no.joined(separator: " ")) Tell them briefly.)\n"
        send(told + q.note + q.text, shown: q.display, files: fs, spoken: q.spoken)  // send() adds voiceNote
    }

    func send(_ text: String, shown: String? = nil, files fs: [ChatFile] = [], spoken: Bool = false) {
        active()
        if run != nil || clearing > 0 { queue.append(Queued(text: text, shown: shown, files: fs, spoken: spoken)); return }  // after Clear's `new`: it deletes attachments
        let you = [shown ?? text, fs.map { "📎 \($0.name)" }.joined(separator: "  ")].filter { !$0.isEmpty }.joined(separator: "\n")
        msgs.append(Msg(.you, you))
        var payload = voice.on ? voiceNote + text : text
        let new = attached.filter { !$0.sent }  // each docked hex goes over once; the chat session keeps it
        if !new.isEmpty {
            payload = "The user dragged these workspaces from the dashboard into the chat; this message is about them:\n"
                + new.map(\.text).joined(separator: "\n") + "\n\n" + payload
            for i in attached.indices { attached[i].sent = true }
        }
        if !focus.isEmpty && focus != focusSent {  // only when it changed: the chat session remembers the last one
            payload = "(Greenlight, not typed by the user: they were last in \(focus). \"This\", \"here\" or \"it\" likely means that.)\n" + payload
            focusSent = focus
        }
        if fs.isEmpty { let r = Run(shown ?? text); r.spoken = spoken; return start(r, chatSend, stdin: payload) }  // spoken: began() draws its instant card
        // chat.py send --attach: the message and the files as one JSON object (attach.py)
        let body = (try? JSONSerialization.data(withJSONObject: ["text": payload, "files": fs.map(\.json)])).map { String(decoding: $0, as: UTF8.self) } ?? ""
        let r = Run(you); r.files = fs; r.text = shown ?? text; r.spoken = spoken
        start(r, chatSend + " --attach", stdin: body)
    }

    // The idle check-in: at the Mac but nothing done in Greenlight (chat, a card's button, typing in a Basement terminal) for
    // checkInMins, greenlight asks the chat what to work on next, once per quiet stretch.
    // ponytail: work outside Greenlight (an editor, a browser) doesn't count; count pushes from the scan if it nags then
    @Published var checkInMins = ud.object(forKey: "checkInMins") as? Int ?? 30 { didSet { ud.set(checkInMins, forKey: "checkInMins") } }
    var lastActive = Date(), checkedIn = false
    func active() { lastActive = Date(); checkedIn = false; if session > 0 { delegate.greenlight.active() } }  // the check-in is the main chat's
    func checkIn() {
        let atMac = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!) < 120
        guard checkInMins > 0, !checkedIn, run == nil, clearing == 0, queue.isEmpty, atMac, Date().timeIntervalSince(lastActive) > Double(checkInMins * 60) else { return }
        checkedIn = true
        let activityAt = lastActive
        gate("check-in", text: "Idle check-in after \(checkInMins) minutes without Greenlight activity; ask what workspace to work on next") { [weak self] ok in
            guard let self, ok, self.checkedIn, self.lastActive == activityAt, self.checkInMins > 0,
                  Date().timeIntervalSince(self.lastActive) > Double(self.checkInMins * 60) else { return }
            guard self.run == nil, self.clearing == 0, self.queue.isEmpty else {
                self.checkedIn = false  // the timer can retry after the competing work finishes
                return
            }
            self.checkInTurn()
        }
    }
    private func checkInTurn() {
        msgs.append(Msg(.note, "Nothing done here in \(checkInMins) minutes: asking what's next."))
        let r = Run("What to work on next"); r.checkIn = true
        start(r, chatSend, stdin:
            "(Greenlight's idle check-in, not typed by the user: they're at their Mac but haven't done anything in Greenlight for \(checkInMins) minutes.) "
            + "From the board, suggest which 1–3 workspaces they should work on next, most valuable first: for each, where it is in its lifecycle, "
            + "why now, and the one next action, with its MR link. A few lines. End with an Options line naming the workspaces.")
    }

    // The watch: a workspace that newly needs the user gets the chat a quiet turn, and it asks only if it's worth asking.
    @Published var watch = ud.object(forKey: "watch") as? Bool ?? true { didSet { ud.set(watch, forKey: "watch") } }
    var focus = "", focusSent = ""  // the window you were last in, described for Chief (noteFocus)
    // The projects every window shows (one pick for all of them) as JSON, while one is up: Chief's BOARD and the watch have only
    // these. nil: every project (none picked, or no window up: you're in the menubar alone). App.pickScope.
    var scope: () -> String? = { nil }
    var chatSend: String {
        let only = scope().map { " --only \(shellQuote($0))" } ?? ""
        return "\(chief) send --model \(shellQuote(model)) --effort \(shellQuote(effort)) --name \(shellQuote(name))" + only + (verbose ? " --verbose" : "")
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
        gate("watch", text: list) { [weak self] ok in  // score the new workspaces before the watch interrupts
            guard let self else { return }
            guard ok, self.watch else {
                let current = Dictionary(uniqueKeysWithValues: ask.keys.compactMap { name in self.needs?[name].map { (name, $0) } })
                self.fresh = current.merging(self.fresh) { $1 }  // a held watch can retry if the gate's history changes
                return
            }
            let current = Dictionary(uniqueKeysWithValues: ask.keys.compactMap { name in self.needs?[name].map { (name, $0) } })
            let (visible, deferred) = inScope(current, self.pucks, self.scope())
            self.fresh = deferred.merging(self.fresh) { $1 }  // a scope change must not consume another project's news
            guard !visible.isEmpty else { return }
            guard self.run == nil, self.clearing == 0, self.queue.isEmpty else { self.fresh = visible.merging(self.fresh) { $1 }; return }
            let liveList = visible.sorted { $0.key < $1.key }.map { "- \($0.key): \($0.value)" }.joined(separator: "\n")
            let r = Run("Watching the workspaces"); r.watch = true
            self.start(r, self.chatSend, stdin:
                "(Greenlight's watch, not typed by the user: these workspaces just started needing the user.)\n\(liveList)\n"
                + "Look into them (refresh the workspaces if the board is stale). If one needs a decision or an answer only the user can give, "
                + "ask one short question about it, with its MR link and a final Options line. Otherwise reply exactly: (nothing)")
        }
    }

    func compact() {
        guard run == nil, clearing == 0, isClaude else { return }
        start(Run("Compact context"), "\(chief) compact --model \(shellQuote(model))", stdin: "")
    }

    var clearing = 0  // Clear's `chat.py new`s still running: a send waits for all of them
    func clear(keepQueue: Bool = false) {
        cancelPlans()
        for work in launchWorks.values { work.cancel() }
        launchWorks.removeAll()
        for t in toasts where t.pending { toastListeners.removeValue(forKey: t.id) }
        toasts.removeAll { $0.pending }
        if run != nil { run?.files = []; stop(); run = nil }  // discard late events from the cleared conversation
        if !keepQueue { queue = [] }
        refused = []  // about the old conversation's files
        clearing += 1
        ssh("\(chief) new", onExit: { [weak self] _ in
            guard let self else { return }
            self.clearing -= 1
            guard self.clearing == 0 else { return }
            self.next()
        })
        for i in attached.indices { attached[i].sent = false }  // the new conversation hasn't seen them
        msgs = [Msg(.note, "Context cleared. New conversation.")]
        focusSent = ""  // the new conversation hasn't heard where you are
        picks += 1; light([], false)  // nor lit anything
        if voice.on { voice.wait("Listening", "New conversation. Context cleared.") }
        questions.removeAll { !$0.ping && $0.answer == nil }  // their conversation is gone
        context = 0; window = 0; run?.meterStale = true  // the next harness reports its own window
    }

    func stop() {
        cancelPlans()
        guard let r = run, !r.stopping else { return }
        r.stopping = true
        r.status = "Stopping"; r.thinking = false
        ssh("\(chief) stop")
    }

    private func start(_ r: Run, _ remote: String, stdin: String) {
        run = r
        r.model = model
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
            let engine = j["engine"] as? String ?? "claude"
            let selected = engine == "agy" ? j["model"] as? String : engine
            r.status = "Starting \(models.first { $0.1 == selected }?.0 ?? engine.capitalized)"
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
            if !ok, let note = staleBackendNote(model: r.model, error: text, host: host, code: greenlightCode) { text = note }
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
            next()  // after Clear: its own exit sends them
            return
        }
        if !ok && r.spoken && !r.files.isEmpty {  // hands-free, the next thing said would send them again (and fail again, if refused): say so, don't resend
            fileSays("Not sent: \(r.files.map(\.name).joined(separator: ", ")). Drop them again to send them with what you say next.")
        } else if !ok && !r.files.isEmpty {  // not sent, or sent and failed: the files come back to try again, ahead of any added since, as many as fit
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
            light([], false)
            if let h = st.hexes { light(resolveHexes(h, pucks), true) }  // Chief said which: no guessing
            else { pickHexes(body, pucks, asked: asksToShow(r.question)) { [weak self] names, open in if n == self?.picks { self?.light(names, open) } } }
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
            notify(title: r.checkIn ? "\(name): what to work on next" : r.watch ? "\(name) asks" : "Done: \(r.question.prefix(60))", body: String(body.prefix(240)), subtitle: r.checkIn || r.watch ? nil : fmt(m.duration), freeform: true)
        }
        if !(ok && st.reset) { next() }  // a reset sends it once clear
    }

    // MARK: pings

    var seen: Int {  // outbox lines already shown, so pings sent while the Mac slept still arrive
        get { ud.object(forKey: "seen") as? Int ?? -1 }
        set { ud.set(newValue, forKey: "seen") }
    }

    var outboxProc: Process?
    func tailOutbox() {
        let from = seen < 0 ? "$(wc -l < \"$f\" | tr -d ' ')" : "\(seen)"
        outboxProc = ssh("f=\(shellPath(greenlightHome))/outbox.jsonl; touch \"$f\"; n=\(from); echo \"#start $n\"; tail -n +$((n+1)) -F \"$f\" 2>/dev/null",
            onLine: { [weak self] l in self?.ping(l) },
            onExit: { [weak self] _ in  // network drop, sleep, host reboot: reconnect
                self?.connected = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 15) { self?.tailOutbox() }
            })
    }

    func ping(_ l: String) {
        if l.hasPrefix("#start ") { seen = Int(l.dropFirst(7).trimmingCharacters(in: .whitespaces)) ?? 0; connected = true; return }
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
        let auto = j["auto"] as? [String: Any], aid = auto?["id"] as? String, chose = auto?["choice"] as? String
        if aid == nil, questions.contains(where: { $0.ping && $0.answer == nil && $0.text == text && $0.options == options && $0.key == key }) { return }
        var fresh = true  // a newer ping about the same workspace replaces the open one
        for i in questions.indices where questions[i].ping && questions[i].answer == nil &&
            ((key != nil && questions[i].key == key) || (key == nil && questions[i].key == nil && questions[i].text == text)) {
            questions[i].answer = "(superseded)"
            if replyingTo == questions[i].id { replyingTo = nil }
            fresh = false
        }
        var q = Question(text: text, options: options, ping: true, ts: (j["ts"] as? Double).map { Date(timeIntervalSince1970: $0) } ?? Date(), key: key,
                         ws: ws?.isEmpty == false ? ws : nil)
        if let aid, let chose {  // answered for you (an opted-in kind, sure and inert): in Answered
            q.answer = "Auto: \(chose)"; q.auto = aid
            questions.append(q)
            return notify(title: name, body: "Answered for you: \(chose). Ask again under Answered in the bell.\n\(text)", subtitle: nil, reply: false, id: key ?? "question:\(text)")
        }
        if let s = j["suggest"] as? [String: Any], let ch = s["choice"] as? String, options.contains(ch) {
            q.suggest = Suggest(choice: ch, prob: (s["prob"] as? NSNumber)?.doubleValue ?? 0, count: (s["count"] as? NSNumber)?.intValue ?? 0,
                                agree: (s["agree"] as? NSNumber)?.intValue ?? 0)
        }
        questions.append(q)
        if !popoverShown() && fresh { unread += 1; missed += 1 }
        notify(title: name, body: text, subtitle: nil, reply: true, id: key ?? "question:\(text)", questionID: q.id)
    }

    /// Same `id` (a workspace key) replaces that workspace's earlier banner in Notification Center instead of stacking.
    func notify(title: String, body: String, subtitle: String?, reply: Bool = true, id: String? = nil, questionID: UUID? = nil, freeform: Bool = false) {
        if selftest || devDir != nil { return }  // fake pings shouldn't pop banners on the real Mac, nor a copy repeat Greenlight's
        let c = UNMutableNotificationContent()
        c.title = title
        if let subtitle { c.subtitle = subtitle }
        c.body = body
        c.sound = .default
        if reply && session == 0 { c.categoryIdentifier = "greenlight" }  // a banner reply goes to the main chat: a second window's has none
        var info: [String: Any] = ["text": body]
        if let questionID { info["questionID"] = questionID.uuidString }
        if freeform { info["freeform"] = true }
        c.userInfo = info
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id ?? UUID().uuidString, content: c, trigger: nil))
    }

    // MARK: DWIM: board commands without a Chief turn (scripts/dwim.py)

    @Published var toasts: [DwimToast] = []
    var toast: DwimToast? { toasts.last }
    var latestUndoableToast: DwimToast? { toasts.last(where: { $0.undoable }) }
    var focusName: String?  // the workspace of the window you were last in (noteFocus): what "park this" means
    var launchWorks: [UUID: DispatchWorkItem] = [:]  // each accepted launch keeps its own grace and Undo
    var toastListeners: [UUID: (DwimToast?) -> Void] = [:]
    var toastExpiry = [UUID: UUID]()
    var plans = DwimPlans()
    func cancelPlans() {
        for text in plans.cancel() { msgs.append(Msg(.you, text)); msgs.append(Msg(.note, "Cancelled before applying.")) }
    }
    static let launchGrace = 5.0, toastFor = 8.0, planDeadline = 1.5
    var dwimJSON: (Any) -> String = { o in (try? JSONSerialization.data(withJSONObject: o)).map { String(decoding: $0, as: UTF8.self) } ?? "{}" }

    /// A board command: dwim.py plans it from the latest scoped snapshot (the grammar plus System One, under a deadline). Direct
    /// actions get an Undo toast and a short spoken line once applied. Anything else, or no answer within planDeadline, goes to Chief;
    /// `otherwise` gets a note for Chief when dwim.py read a board action it wouldn't do on its own (ambiguous, destructive).
    func dwim(_ text: String, source: String, onToast: ((DwimToast?) -> Void)? = nil, otherwise: @escaping (String) -> Void) {
        active()
        let request = plans.begin(text)
        let t0 = Date(), body = dwimJSON(["text": text, "source": source, "focus": focusName ?? "", "pucks": dwimPucks(dwimScopedPucks(pucks, scope()))])
        var out = "", answered = false
        let late = DispatchWorkItem { [weak self] in
            guard let self, self.plans.finish(request), !answered else { return }
            answered = true
            self.log(.greenlight, "dwim", .failed, "no plan in \(Int(Core.planDeadline * 1000)) ms: asked Chief")
            otherwise("")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + Core.planDeadline, execute: late)
        ssh("\(py)/dwim.py plan", stdin: body, onLine: { out += $0 }, onExit: { [weak self] _ in
            guard let self, self.plans.finish(request), !answered else { return }
            answered = true; late.cancel()
            let o = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: Any] ?? [:], ms = Int(Date().timeIntervalSince(t0) * 1000)
            NSLog("dwim: %@ in %d ms (dwim.py %@): %@", o["route"] as? String ?? "no answer", ms, "\(o["ms"] ?? "?")", o["why"] as? String ?? "")
            guard o["route"] as? String == "direct" else {
                let why = o["why"] as? String ?? "no answer"
                self.log(.greenlight, "dwim", .status, "to Chief in \(ms) ms: \(why)")
                let verb = o["verb"] as? String ?? ""  // a board action it read but won't do alone: Chief hears why, and asks or confirms
                return otherwise(verb.isEmpty ? "" : "(Greenlight's fast path read this as \(verb) but didn't do it: \(why). Confirm with the user, or ask which, before acting.)\n")
            }
            self.direct(o, text: text, ms: ms, onToast: onToast)
        })
    }

    private func direct(_ o: [String: Any], text: String, ms: Int, onToast: ((DwimToast?) -> Void)?) {
        let acts = o["actions"] as? [[String: Any]] ?? [], line = o["toast"] as? String ?? "", say = o["say"] as? String ?? line
        msgs.append(Msg(.you, text))  // the chat keeps the record: Chief reads the board, not this
        let pending = o["launch"] as? Bool == true
        let notice = acts.isEmpty ? say : (pending ? "Starting after \(Int(Core.launchGrace)) seconds: \(line)" : "Applying: \(text)") + dwimSkipped(o)
        msgs.append(Msg(.note, notice))
        log(.greenlight, "dwim", .status, "\(o["verb"] as? String ?? "") planned in \(ms) ms: \(notice)")
        if voice.on {
            voice.say(notice)
            if acts.isEmpty && run == nil { voice.show(["kind": "list", "eyebrow": "nothing to do", "title": line, "stat": "", "items": []]) }
        }
        if acts.isEmpty { return show(DwimToast(text: line), for: Core.toastFor, onToast: onToast) }
        if pending {  // an agent can't be un-started: it waits out the grace, and Undo cancels it
            let t = DwimToast(text: line + dwimSkipped(o), pending: true)
            show(t, for: nil, onToast: onToast)
            let w = DispatchWorkItem { [weak self] in
                guard let self else { return }
                guard self.launchWorks.removeValue(forKey: t.id) != nil else { return }  // Undo may have cancelled its grace
                var now = t; now.pending = false; self.show(now, for: nil)
                self.apply(o, text: text, id: t.id)
            }
            launchWorks[t.id] = w
            DispatchQueue.main.asyncAfter(deadline: .now() + Core.launchGrace, execute: w)
        } else {
            let t = DwimToast(text: "Applying: \(text)", applying: true)
            show(t, for: nil, onToast: onToast)
            apply(o, text: text, id: t.id)
        }
    }

    private func apply(_ o: [String: Any], text: String, id: UUID) {
        var out = ""
        let acts = o["actions"] as? [[String: Any]] ?? []
        let recovery = acts.contains { ["park", "unpark", "hold", "release"].contains($0["verb"] as? String ?? "") }
            ? UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased() : nil
        ssh("\(py)/dwim.py apply", stdin: dwimJSON(["text": text, "source": o["source"] ?? "chat", "actions": acts, "undo": recovery ?? ""]), onLine: { out += $0 }, onExit: { [weak self] code in
            guard let self else { return }
            let r = dwimApplyReply(out, failure: "\(host) didn't answer (exit \(code)); outcome unknown", recovery: recovery)
            let errs = r["errors"] as? [String] ?? [], done = r["done"] as? [String] ?? [], unchanged = r["unchanged"] as? [String] ?? [], undo = r["undo"] as? String
            let line = (done.isEmpty ? "Nothing changed" : errs.isEmpty && unchanged.isEmpty ? (o["toast"] as? String ?? "Done") : "Changed \(done.joined(separator: ", "))") + (done.isEmpty ? "" : dwimSkipped(o))
            if !done.isEmpty {
                self.msgs.append(Msg(.note, line))
                if self.voice.on {
                    self.voice.say(line)
                    if self.run == nil { self.voice.show(["kind": "list", "eyebrow": "done", "title": line, "stat": "",
                                                        "items": done.map { ["label": $0, "detail": ""] }]) }
                }
            }
            if done.isEmpty && errs.isEmpty { self.msgs.append(Msg(.note, line)); if self.voice.on { self.voice.say(line) } }
            if !errs.isEmpty {
                self.msgs.append(Msg(.note, "Didn't finish: " + errs.joined(separator: "; ")))
                if self.voice.on { self.voice.say("That didn't all work: \(errs.count == 1 ? errs[0] : "\(errs.count) failed").") }
            }
            self.log(.greenlight, "dwim", errs.isEmpty ? .action : .failed, "applied in \(r["ms"] ?? "?") ms" + (errs.isEmpty ? "" : ": " + errs.joined(separator: "; ")))
            guard var t = self.toasts.first(where: { $0.id == id }) else { return }
            t.undo = undo; t.failed = !errs.isEmpty; t.applying = false
            t.text = errs.isEmpty ? line : done.isEmpty ? "Didn't finish: \(errs.joined(separator: "; "))" : "\(line) · \(errs.count) failed"
            if t.undoing && undo != nil { t.undoing = false; self.show(t, for: nil); return self.undoToast(id) }  // Undo was pressed before the apply came back
            t.undoing = false
            self.show(t, for: Core.toastFor)
        })
    }

    func show(_ t: DwimToast, for secs: Double?, onToast: ((DwimToast?) -> Void)? = nil) {
        if let onToast { toastListeners[t.id] = onToast }
        toasts = keepingToast(toasts, t)
        toastListeners[t.id]?(t)
        let expiry = UUID(); toastExpiry[t.id] = expiry  // an update without a timer invalidates the old one too
        guard let secs else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + secs) { [weak self] in
            if self?.toastExpiry[t.id] == expiry { self?.removeToast(t.id) }
        }
    }

    private func removeToast(_ id: UUID) { toasts.removeAll { $0.id == id }; toastListeners.removeValue(forKey: id); toastExpiry.removeValue(forKey: id) }

    /// The toast's Undo (and "undo" said in voice mode): cancels a launch still in its grace, or puts the edited goals back.
    func undoToast(_ id: UUID? = nil) {
        guard let target = id ?? latestUndoableToast?.id, var t = toasts.first(where: { $0.id == target }), canBeginDwimUndo(t) else { return }
        if t.pending {
            launchWorks.removeValue(forKey: t.id)?.cancel()
            msgs.append(Msg(.note, "Cancelled: \(t.text)"))
            if voice.on { voice.say("Cancelled.") }
            removeToast(t.id)
            return show(DwimToast(text: "Cancelled"), for: 3)
        }
        guard let u = t.undo else { if t.applying { t.undoing = true; show(t, for: nil) }; return }  // still applying: undone as soon as it's in
        t.undoing = true; show(t, for: nil)
        var out = ""
        ssh("\(py)/dwim.py undo \(shellQuote(u))", onLine: { out += $0 }, onExit: { [weak self] code in
            guard let self else { return }
            guard let r = dwimUndoReply(out, Int(code)) else {
                var retry = t; retry.undoing = false; retry.failed = true
                self.show(retry, for: nil)
                self.msgs.append(Msg(.note, "Undo didn't finish. Try Undo again."))
                if self.voice.on { self.voice.say("Undo didn't finish. Try again.") }
                self.log(.greenlight, "dwim", .failed, "undo failed (exit \(code))")
                return
            }
            let back = r["undone"] as? [String] ?? [], kept = r["kept"] as? [String] ?? []
            let said = r["error"] as? String ?? (back.isEmpty ? "Nothing to undo" : "Undid \(t.text.prefix(1).lowercased() + t.text.dropFirst())")
                + (kept.isEmpty ? "" : ". Left \(kept.joined(separator: ", ")): edited since")
            self.msgs.append(Msg(.note, said))
            if self.voice.on { self.voice.say(back.isEmpty ? said : "Undone.") }
            self.log(.greenlight, "dwim", r["error"] == nil ? .action : .failed, said)
            if !kept.isEmpty {
                var retry = t; retry.undoing = false; retry.failed = true
                self.show(retry, for: nil)
                self.msgs.append(Msg(.note, "Undo the newer edit, then try this Undo again."))
            } else {
                self.removeToast(t.id)
                self.show(DwimToast(text: said), for: 4)
            }
        })
    }
}

