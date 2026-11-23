import AppKit
import AVFoundation
import Carbon
import Combine
import CoreAudio
import CryptoKit
import Network
import os
import Security
import Speech
import SwiftUI
import UniformTypeIdentifiers
import UserNotifications
import WebKit

/// Voice mode. One audio engine: the mic, voice-processed so Chief's own voice is cancelled out of it, into on-device speech
/// recognition; Chief's speech out through a player node whose mix draws its waveform. The mic is on only while `on`.
final class Voice: NSObject, ObservableObject {
    weak var core: Core?
    @Published var on = false
    @Published var heard = ""  // your turn so far
    let meter = Meter()
    @Published var caption = ""  // the sentence Chief is saying
    @Published var look = looks.first { $0.id == ud.string(forKey: "stageStyle") } ?? looks[1] {  // default Orb
        didSet {
            ud.set(look.id, forKey: "stageStyle"); chatLook = look; core?.look += 1
            if stageWebLoaded {
                stageWeb.appearance = NSAppearance(named: look.dark ? .darkAqua : .aqua)
                stageWeb.evaluateJavaScript("(s => { \(look.vars) })(document.documentElement.style)")  // safety-ok: hardcoded Theme look.vars
            }
        }
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
    var inputTapped = false, mixerTapped = false  // avoid lazily opening inputNode/mainMixerNode in stopAll/freshEngine when never tapped
    var waitWord = "One minute"
    var ending = false  // Chief said [voice: off]: stop once it's done speaking
    var muted = false  // you cut in or said stop: the rest of this reply stays unspoken
    /// You muted the mic (the header's mic button, ⇧⌘M): nothing it hears reaches Apple's recognizer or GPT-Live, and the turn you
    /// were in the middle of is dropped. The engine keeps running so Chief can still speak (and its echo cancellation stays set up),
    /// so macOS still shows the mic in use. Each voice mode starts unmuted.
    @Published private(set) var micMuted = false
    private var micOff = false  // micMuted for the mic's tap on the audio thread, under `lock`
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

    private final class StageNav: NSObject, WKNavigationDelegate, WKUIDelegate {
        let stageURL: URL?
        var ready = false, queued: String?
        init(_ u: URL?) { stageURL = u }
        func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
            ready = true
            if let q = queued { queued = nil; w.evaluateJavaScript(q) }  // safety-ok: queued only by Voice.js() with JSONSerialization args
        }
        func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let u = a.request.url else { decisionHandler(.cancel); return }
            let ok = u.scheme == "about" || (u.scheme == "file" && u.path == stageURL?.path)
            decisionHandler(ok ? .allow : .cancel)
        }
        func webView(_ w: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? { nil }
    }
    private var stageNav: StageNav?
    private var stageWebLoaded = false
    lazy var stageWeb: WKWebView = {  // no message handlers: nothing on the stage can reach the app
        stageWebLoaded = true
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .nonPersistent()
        let u = page("stage")
        let nav = StageNav(u); stageNav = nav
        let v = WKWebView(frame: .zero, configuration: cfg)
        v.navigationDelegate = nav; v.uiDelegate = nav
        v.setValue(false, forKey: "drawsBackground")
        v.appearance = NSAppearance(named: look.dark ? .darkAqua : .aqua)  // the look's, not the system's
        if let u { v.loadFileURL(u, allowingReadAccessTo: u) }
        return v
    }()
    func js(_ f: String, _ args: [Any]) {
        guard JSONSerialization.isValidJSONObject(args),
              let d = try? JSONSerialization.data(withJSONObject: args), let a = String(data: d, encoding: .utf8) else { return }
        let code = "(s => { \(delegate.themeVars()) \(look.vars) })(document.documentElement.style); \(f)(...\(a))"
        _ = stageWeb
        if stageNav?.ready == true { stageWeb.evaluateJavaScript(code) } else { stageNav?.queued = code }  // safety-ok: JSONSerialization args + hardcoded look.vars
    }
    func wait(_ title: String, _ sub: String) { js("stage.wait", [title, sub]) }
    func show(_ spec: [String: Any]) {  // a card the app drew (the workspace you switched to): newer than any drawing on its way
        guard on else { return }
        drawSeq += 1; shown = true; js("stage.spec", [spec])
    }

    private var claimObs: NSObjectProtocol?, audioObs: NSObjectProtocol?
    override init() {
        super.init()
        // Another Greenlight (Greenlight.app beside a dev copy, which shares its shortcut) started voice mode: one listens and speaks,
        // or each hears you, asks Chief on its own and says its own answer over the other's (one in Apple's voice when its Keychain says no)
        claimObs = DistributedNotificationCenter.default().addObserver(forName: voiceClaim, object: nil, queue: .main) { [weak self] n in
            guard let self, self.on, let at = n.userInfo?["at"] as? Double, let pid = n.userInfo?["pid"] as? Int,
                  voiceYields(mine: (self.claimedAt, Int(getpid())), theirs: (at, pid)) else { return }
            self.stopAll()
            self.core?.msgs.append(Msg(.note, "Voice mode moved to \(n.object as? String ?? "another Greenlight"), so this one stopped listening: two would both answer you, over each other."))
        }
        audioObs = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main) { [weak self] n in
            guard let self, self.on, n.object as AnyObject? === self.engine else { return }  // a headset in or out: the engine stopped itself
            self.restart()
        }
    }
    deinit {
        if let claimObs { DistributedNotificationCenter.default().removeObserver(claimObs) }
        if let audioObs { NotificationCenter.default.removeObserver(audioObs) }
    }
    /// The engine stopped itself (a headset in or out): start it again. If it won't, voice mode stops and says so, and the next start
    /// gets a new engine.
    func restart() {
        hush()
        do { try startEngine(); listen() }  // listen() is a no-op on GPT-Live, whose socket stays up
        catch {
            liveLog.error("voice: audio restart failed: \(String(describing: error), privacy: .public)")
            core?.msgs.append(Msg(.note, "Voice mode stopped: the audio didn't restart after the devices changed (\(error.localizedDescription)). Start it again to pick it up."))
            stopAll()
            staleEngine = true
        }
    }

    func toggle() { Diag.event("voice", ["on": !(on || starting)]); on || starting ? stopAll() : start() }  // a press while it's still starting cancels it
    @Published var starting = false  // a start (and its retries) under way: the voice button shows it, and a press cancels it
    var startGen = 0  // stopAll() bumps it, which ends a start still under way
    var staleEngine = false  // its restart failed: the next start begins on a new engine
    func start() {
        guard !on, !starting else { return }
        starting = true; startGen += 1
        core?.refused = []  // refusals from before voice mode were shown in the composer: Chief isn't told them now
        start(attempt: 0, gen: startGen)
    }
    /// Waits before each retry of a start that failed: about 3 s in all, then the error is shown.
    static let startRetries = [0.3, 0.8, 1.5]
    func start(attempt: Int, gen g: Int) {
        guard g == startGen else { return }  // cancelled
        guard !on, core?.recording == nil else { starting = false; return }  // dictation has the mic
        let live = Speech.shared.kind == "live"  // GPT-Live hears you itself: Speech Recognition isn't needed
        let finishAuth: (Bool, Bool) -> Void = { ok, speechOk in DispatchQueue.main.async {
            guard g == self.startGen, !self.on, self.core?.recording == nil else { self.starting = false; return }
            guard ok, speechOk else {
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
                liveLog.error("voice: audio start \(attempt + 1, privacy: .public) failed: \(String(describing: error), privacy: .public)")
                self.freshEngine()
                if attempt < Self.startRetries.count && (error as NSError).domain != "Voice" {  // not when there's no mic: no retry finds one
                    return DispatchQueue.main.asyncAfter(deadline: .now() + Self.startRetries[attempt]) { self.start(attempt: attempt + 1, gen: g) }
                }
                self.core?.msgs.append(Msg(.note, "Voice mode couldn't start the audio: \(error.localizedDescription)"))
                return self.stopAll()
            }
            if attempt > 0 { liveLog.info("voice: audio started on try \(attempt + 1, privacy: .public)") }
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
        }}
        AVCaptureDevice.requestAccess(for: .audio) { ok in
            if live { finishAuth(ok, true) } else { SFSpeechRecognizer.requestAuthorization { st in finishAuth(ok, st == .authorized) } }
        }
    }

    /// Drops an engine whose start failed, voice processing and all, for a new one. The player moves to the new engine.
    /// Only while voice mode is off: play() reads `engine` off the main thread, and bails out first when it's off.
    func freshEngine() {
        engine.stop()
        if inputTapped { engine.inputNode.removeTap(onBus: 0); inputTapped = false }
        if mixerTapped { engine.mainMixerNode.removeTap(onBus: 0); mixerTapped = false }
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
        engine.mainMixerNode.removeTap(onBus: 0)
        // AUVoiceProcessor (vpio) fails with -10875 ("client-side input and output formats do not match") when input and output
        // devices run at different sample rates (e.g. 48 kHz built-in mic + 44.1 kHz headphones) because mainMixerNode's
        // implicit connection to outputNode stays at the output device's rate. Match mixer->output to the input rate.
        if vp, let outFmt = matchedVoiceOutputFormat(inputRate: inFormat.sampleRate, outputFormat: engine.outputNode.inputFormat(forBus: 0)) {
            try engine.link(engine.mainMixerNode, to: engine.outputNode, format: outFmt)
        }
        let conv = AVAudioConverter(from: inFormat, to: micFormat), lconv = isLive ? AVAudioConverter(from: inFormat, to: live.fmt) : nil
        if inFormat.channelCount > 1 { conv?.channelMap = [0]; lconv?.channelMap = [0] }
        input.removeTap(onBus: 0)
        try input.tap(onBus: 0, bufferSize: 1024, format: inFormat) { [weak self] b, _ in
            guard let self else { return }
            self.lock.lock(); let r = self.request, off = self.micOff; self.lock.unlock()
            guard !off, let conv, let m = convert(b, conv, self.micFormat) else { return }  // muted: the audio goes nowhere
            if let lconv { convert(b, lconv, self.live.fmt).map(self.live.mic) }  // GPT-Live: the mic goes to it, not to Apple's recognizer
            r?.append(m)
            let l = level(m)
            DispatchQueue.main.async { self.meter.you = Array(self.meter.you.dropFirst()) + [l] }
        }
        inputTapped = true
        try engine.mainMixerNode.tap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] b, _ in
            let l = level(b)
            DispatchQueue.main.async { guard let self else { return }; self.meter.chief = Array(self.meter.chief.dropFirst()) + [self.speaking > 0 ? l : 0] }
        }
        mixerTapped = true
        engine.prepare()
        try engine.start()
    }

    /// A fresh recognition request: after each of your turns, after Chief speaks (its echo is in the old one), after an error.
    func listen() {
        guard !isLive, !micMuted else { return }  // muted: unmuting listens again
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
        if c.files.isEmpty && c.refused.isEmpty && c.reading == 0 && dwimCandidate(t) { return c.dwim(t, source: "voice") { note in c.spoken(t, note: note) } }  // files go to Chief
        c.spoken(t)
    }
    /// The instant card (dwim.py stage): a table, timeline, diff or lifecycle drawn from the snapshot while Chief thinks. It only
    /// replaces the wait card: anything drawn since it was asked for (Chief's own, Haiku's) wins, and it never cancels Haiku's drawing.
    func instant(_ heard: String) {
        guard on, let c = core, !c.pucks.isEmpty else { return }
        let seq = drawSeq, t0 = Date()
        let body = (try? JSONSerialization.data(withJSONObject: ["text": heard, "focus": c.focusName ?? "", "pucks": dwimPucks(dwimScopedPucks(c.pucks, c.scope()))])).map { String(decoding: $0, as: UTF8.self) } ?? "{}"
        var out = ""
        ssh("\(py)/dwim.py stage", stdin: body, onLine: { out += $0 }, onExit: { [weak self] _ in
            guard let self, self.on, seq == self.drawSeq, !self.shown, !self.chiefDrew,
                  let o = try? JSONSerialization.jsonObject(with: Data(out.utf8)) as? [String: Any], o["kind"] is String else { return }
            self.shown = true; self.js("stage.spec", [o])
            NSLog("dwim: stage %@ in %d ms (dwim.py %@ ms)", o["kind"] as? String ?? "", Int(Date().timeIntervalSince(t0) * 1000), "\(o["ms"] ?? "?")")
        })
    }

    func run(_ cmd: VoiceCmd) {
        switch cmd {
        case .bye: if core?.run == nil { core?.queue.removeAll { $0.spoken } }; stopAll()  // a turn still waiting on its file doesn't go
        case .undo: if let t = core?.latestUndoableToast { core?.undoToast(t.id) } else { core?.spoken("undo") }  // nothing of ours to undo: Chief's
        case .stop, .cancel: core?.cancelPlans()  // a plan may be pending while Chief has a different turn running
            if voiceStopsPending(cmd, chiefRunning: core?.run != nil, pending: core?.toasts.contains(where: { $0.pending }) == true),
               let pending = core?.toasts.last(where: { $0.pending }) { return core!.undoToast(pending.id) }
            if core?.run != nil { hush(); core?.stop(); return }
            hush(); if core?.run == nil { core?.queue.removeAll { $0.spoken } }; core?.stop()  // the answer, or a turn waiting on its file; never what was said during the answer
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
        if r.spoken { instant(r.question) }
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
    /// end: a silent frame marks the line's end in the player. Always touches AVAudioEngine/player on the main thread.
    func play(_ buf: AVAudioPCMBuffer?, end: Bool, _ g: Int) {
        let work = {
            guard self.gen == g, self.on, self.engine.attachedNodes.contains(self.player) else { return }
            var out = buf
            if end { out = AVAudioPCMBuffer(pcmFormat: self.speechFormat, frameCapacity: 1); out?.frameLength = 1 }
            guard let out else { return }
            self.speaking += 1
            self.player.scheduleBuffer(out, completionCallbackType: .dataPlayedBack) { _ in DispatchQueue.main.async {
                guard self.gen == g else { return }
                self.speaking = max(0, self.speaking - 1)
                if end { if !self.lines.isEmpty { self.lines.removeFirst() }; self.caption = self.lines.first ?? self.caption }
                if self.speaking == 0 && self.lines.isEmpty && self.on { self.ending ? self.stopAll() : self.listen() }  // Chief's echo is in that request
            }}
            if !self.player.isPlaying && self.engine.isRunning { try? self.player.start() }  // play() raises on a stopped engine
        }
        if Thread.isMainThread { work() } else { DispatchQueue.main.async(execute: work) }
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
            let g = startGen
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in if let self, self.startGen == g { self.handOff(id) } }
        case .failed(let code, let m):
            liveLog.error("GPT-Live: \(code, privacy: .public) \(m, privacy: .public)")
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
        if c.run != nil { muted = true; liveBuf = [] }  // a new request mid-answer: the rest of the old one goes unsaid (spoken() queues the new one)
        lastHeard = t
        c.spoken(t)
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
        guard !liveDrop, on, engine.attachedNodes.contains(player) else { if speaking > 0 { speaking = 0; newCaption = true }; return }
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

    func toggleMic() { setMicMuted(!micMuted) }
    /// Mutes or unmutes the mic, only while voice mode is on (unmuting always works). Muting cuts the tap off first, then drops the
    /// recognition request and the half-heard turn with it, so nothing said while muted is transcribed or sent.
    func setMicMuted(_ m: Bool) {
        guard m != micMuted, on || !m else { return }
        lock.lock(); micOff = m; if m { request = nil }; lock.unlock()
        micMuted = m
        Diag.event("voice", ["mic_muted": m])
        if m {
            endTurn?.cancel(); task?.cancel(); task = nil
            heard = ""
            live.dropMic()
            meter.you = meter.you.map { _ in 0 }
        } else if on { listen() }
    }

    /// Never started (or long since stopped): nothing for stopAll to stop. It touches engine.inputNode, which on a Mac that hasn't
    /// granted the microphone asks for it (or, with nobody to answer, blocks): closing a window that never used voice mustn't.
    var idle: Bool { !on && !starting && !ending && !vp && !engine.isRunning }
    func stopAll() {
        startGen += 1; starting = false  // and any start still retrying
        hush()
        live.close(); liveUp = false; delegation = nil; liveOut = nil
        endTurn?.cancel()
        task?.cancel(); task = nil
        lock.lock(); request = nil; micOff = false; lock.unlock()
        micMuted = false
        engine.stop()
        if inputTapped { engine.inputNode.removeTap(onBus: 0); inputTapped = false }
        if mixerTapped { engine.mainMixerNode.removeTap(onBus: 0); mixerTapped = false }
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

/// Output format for `mainMixerNode -> outputNode` under voice processing: matches `inputRate` while preserving the output
/// channel count/layout, so AUVoiceProcessor doesn't fail with -10875 on mixed-rate devices (e.g. 48 kHz mic + 44.1 kHz headphones).
func matchedVoiceOutputFormat(inputRate: Double, outputFormat: AVAudioFormat) -> AVAudioFormat? {
    guard inputRate > 0, outputFormat.channelCount > 0 else { return nil }
    if outputFormat.channelCount <= 2 {
        return AVAudioFormat(standardFormatWithSampleRate: inputRate, channels: outputFormat.channelCount)
    }
    if let layout = outputFormat.channelLayout {
        return AVAudioFormat(standardFormatWithSampleRate: inputRate, channelLayout: layout)
    }
    return AVAudioFormat(standardFormatWithSampleRate: inputRate, channels: 2)
}

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
            let d = self.data(line), errStatus = self.status
            DispatchQueue.main.async {
                guard let d else { return done(errStatus.isEmpty ? "No audio" : errStatus) }
                self.tester = try? AVAudioPlayer(data: wav(d, rate: 24000))
                done(self.tester?.play() == true ? nil : "Got audio but couldn't play it")
            }
        }
    }

    private func kokoroRead(_ text: String, kin: FileHandle, kout: FileHandle, voice: String) throws -> Data? {
        guard let line = try? JSONSerialization.data(withJSONObject: ["text": text, "voice": voice]) else { return nil }
        try kin.write(contentsOf: line + Data("\n".utf8))
        guard let n = try kout.read(upToCount: 4), n.count == 4 else { throw CocoaError(.fileReadUnknown) }
        let len = n.withUnsafeBytes { Int(UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self))) }
        return len == 0 ? nil : try kout.read(upToCount: len)
    }

    func kokoro(_ text: String) -> Data? {
        guard let kin, let kout else { return nil }
        let p = proc
        do {
            return try kokoroRead(text, kin: kin, kout: kout, voice: kokoroVoice)
        } catch {  // it died: restart it once it had been working (a failed start stays failed, no respawn loop)
            let was = ready
            if proc === p { ready = false; status = was ? "Kokoro stopped, restarting" : "Kokoro didn't start"; proc?.terminate(); proc = nil; self.kin = nil; self.kout = nil }
            if was { warm() }
            return nil
        }
    }

    /// Start Kokoro if it's the voice (at launch, on picking it, and as voice mode starts); a warm-up line loads the model before the first real one.
    /// It also clears a stuck ElevenLabs failure: you may have fixed it (a new key, voice ID or plan).
    func warm() {
        q.async {
            if self.stuck != nil { self.stuck = nil; self.status = "" }
            let k = self.kind
            if k == "live" { _ = openAIKey(); return }
            if k == "eleven" { _ = keychain("elevenlabs"); return }  // any Keychain prompt at launch, not with the first line
            guard k == "kokoro", self.proc == nil else { return }
            let uv = ["/opt/homebrew/bin/uv", "/usr/local/bin/uv", NSHomeDirectory() + "/.local/bin/uv"].first { FileManager.default.isExecutableFile(atPath: $0) }
            guard let uv, let script = Bundle.main.path(forResource: "kokoro", ofType: "py") else { self.status = "Kokoro needs uv (brew install uv)"; return }
            let p = Process(), i = Pipe(), o = Pipe()
            p.executableURL = URL(fileURLWithPath: uv)
            p.arguments = ["run", "-q", script]
            p.standardInput = i; p.standardOutput = o
            let log = NSHomeDirectory() + "/Library/Logs/Greenlight-kokoro.log"
            FileManager.default.createFile(atPath: log, contents: nil)
            p.standardError = FileHandle(forWritingAtPath: log)
            self.status = "Kokoro is loading (the first time downloads about 1 GB)"
            do { try p.run() } catch { self.status = "Kokoro: \(error.localizedDescription)"; return }
            let wIn = i.fileHandleForWriting, wOut = o.fileHandleForReading, v = self.kokoroVoice
            self.proc = p; self.kin = wIn; self.kout = wOut
            DispatchQueue.global().async {
                let ok = (try? self.kokoroRead("Ready.", kin: wIn, kout: wOut, voice: v)) != nil
                self.q.async {
                    guard self.proc === p else { return }
                    self.ready = ok
                    self.status = ok ? "" : "Kokoro didn't start"
                    if !ok { self.proc?.terminate(); self.proc = nil; self.kin = nil; self.kout = nil }
                }
            }
        }
    }
    func stop() {
        q.async { self.proc?.terminate(); self.proc = nil; self.kin = nil; self.kout = nil; self.ready = false }
    }

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
    let clean = { (s: String) in s.replacingOccurrences(of: #"[\r\n\0]"#, with: "", options: .regularExpression) }
    return Data(("GET \(clean(path)) HTTP/1.1\r\nHost: \(clean(host))\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: \(clean(nonce))\r\n"
        + "Sec-WebSocket-Version: 13\r\nAuthorization: Bearer \(clean(key))\r\nUser-Agent: Greenlight\r\n\r\n").utf8)
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
        guard n > 0, n <= 64 << 10, body.distance(from: eol.upperBound, to: body.endIndex) - 2 >= n else { break }
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

private let liveLog = Logger(subsystem: "dev.greenlight.app", category: "live")

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
        let c: NWConnection, nonce: String, key: String, gen: Int
        var waited: NWError?, up = false, dead = false, head = Data(), reply: (status: Int, headers: [String: String])?, body = Data(), reader = WSReader(), queued = [Data]()
        init(_ c: NWConnection, _ key: String, _ gen: Int) { self.c = c; self.key = key; self.gen = gen; nonce = Data((0..<16).map { _ in UInt8.random(in: 0...255) }).base64EncodedString() }
    }
    private let genLock = NSLock()
    private var liveGen = 0
    private func isCurrent(_ g: Int) -> Bool { genLock.lock(); defer { genLock.unlock() }; return liveGen == g }
    private var sock: Sock?
    var pending = Data(), on: ((LiveEvent) -> Void)?
    /// Where the socket goes: api.openai.com over TLS; the selftest's fake server is plain TCP on this Mac.
    var endpoint = (host: LiveVoice.host, port: UInt16(443), tls: true)

    func open(_ key: String, voice: String = LiveVoice.voice, instructions: String = liveRule, _ on: @escaping (LiveEvent) -> Void) {
        close()
        let tls = NWProtocolTLS.Options()
        for p in liveALPN { sec_protocol_options_add_tls_application_protocol(tls.securityProtocolOptions, p) }
        let e = endpoint
        genLock.lock(); liveGen += 1; let g = liveGen; genLock.unlock()
        let s = Sock(NWConnection(host: .init(e.host), port: .init(rawValue: e.port)!, using: e.tls ? NWParameters(tls: tls) : .tcp), key, g)
        q.async { self.sock = s; self.pending = Data() }
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
        if s.reply != nil {
            if s.body.count < 64 << 10 { s.body += d.prefix((64 << 10) - s.body.count) }
            if s.body.count >= 64 << 10 || wsBodyDone(s.reply!.headers, s.body) { refused(s, s.reply!.status, s.reply!.headers) }
            return
        }
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
        let hdrs = shown.compactMap { k in h[k].map { "\(k): \($0)" } }.joined(separator: "; ")
        let refText = ref ?? "no request id"
        liveLog.error("GPT-Live: upgrade refused, HTTP \(status, privacy: .public), \(refText, privacy: .public): \(m, privacy: .public) [\(hdrs, privacy: .public)]")
        end(s, LiveVoice.why(status, m, ref: ref))
    }
    private func frames(_ s: Sock, _ d: Data) {
        for m in s.reader.feed(d) {
            switch m {
            case .text(let p), .binary(let p):
                let e = liveEvent(p)
                if e != .other { DispatchQueue.main.async { if self.isCurrent(s.gen) { self.on?(e) } } }
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
        if sock === s { sock = nil }
        if let last { s.c.send(content: last, completion: .contentProcessed { _ in s.c.cancel() }) } else { s.c.cancel() }
        DispatchQueue.main.async { if self.isCurrent(s.gen) { self.on?(.closed(why)) } }
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
    /// Mic audio, already 24 kHz 16-bit mono, sent in 100 ms pieces once the WebSocket upgrade is up.
    /// Dropped before upgrade so a slow TCP/TLS connect never flushes a >5 s burst that trips input_audio_rate_limit_exceeded.
    func mic(_ b: AVAudioPCMBuffer) {
        guard let p = b.int16ChannelData, b.frameLength > 0 else { return }
        let d = Data(bytes: p[0], count: Int(b.frameLength) * 2)
        q.async {
            guard let k = self.sock, !k.dead, k.up else { self.pending = Data(); return }
            self.pending += d
            guard self.pending.count >= 4800 else { return }
            let o = ["type": "session.input_audio.append", "audio": self.pending.base64EncodedString()]
            self.pending = Data()
            if let j = try? JSONSerialization.data(withJSONObject: o), let s = String(data: j, encoding: .utf8) { self.write(k, s) }
        }
    }
    /// Mute: the bit of mic audio not yet sent is never sent.
    func dropMic() { q.async { self.pending = Data() } }
    func close() {
        genLock.lock(); liveGen += 1; genLock.unlock()
        on = nil
        q.async {
            guard let s = self.sock else { return }
            self.sock = nil
            guard !s.dead else { return }
            s.dead = true
            guard s.up else { return s.c.cancel() }
            s.c.send(content: wsFrame(0x1, Data(#"{"type":"session.close"}"#.utf8)), completion: .contentProcessed { _ in })
            s.c.send(content: wsFrame(0x8, Data([0x03, 0xE8])), completion: .contentProcessed { _ in s.c.cancel() })
            self.q.asyncAfter(deadline: .now() + 2) { s.c.cancel() }
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
    /// Optional loopback endpoint override for offline `--unittest` preview tests.
    var testEndpoint: (host: String, port: UInt16, tls: Bool)?
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
    /// A key was saved or removed. A new OpenAI key (or removing the Keychain override when OPENAI_API_KEY is set)
    /// may fix a failure, so ▶ is worth another try, and a preview still starting on the old key is stopped (its refusal
    /// would land after the save). Removing a key with no fallback fixes nothing.
    func keyChanged(_ account: String, to v: String, hasFallback: Bool = false) {
        guard account == "openai", !v.isEmpty || hasFallback else { return }
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
        if let ep = testEndpoint { l.endpoint = ep }
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
    private func fail(_ voice: String, _ why: String) { liveLog.error("GPT-Live preview: \(why, privacy: .public)"); teardown(); phase = .failed(voice, why) }
    private func teardown() {
        gen += 1  // its timers and late callbacks are void, so they can't replace how it ended
        feed?.invalidate(); feed = nil
        live?.close(); live = nil
        node?.stop(); engine?.stop(); node = nil; engine = nil; conv = nil
        closed = true
    }
}

