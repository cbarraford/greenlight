import AppKit
import AVFoundation
import Carbon
import Foundation
import UserNotifications

// MARK: test runners (--unittest, --selftest)

/// Runs all deterministic offline unit checks (called by both `--unittest` and `--selftest`).
func runOfflineUnitTests(check: (String, Bool) -> Void) {
    runCoreOfflineUnitTests(check: check)
    runVoiceOfflineUnitTests(check: check)
}

/// `Greenlight --unittest [--filter <substr>]`: fast, deterministic Swift unit test suite for local & CI.
func unitTest() {
    let t0 = Date()
    let filter: String? = {
        if let i = CommandLine.arguments.firstIndex(of: "--filter"), i + 1 < CommandLine.arguments.count {
            return CommandLine.arguments[i + 1].lowercased()
        }
        return nil
    }()
    var total = 0, failed = 0
    func check(_ what: String, _ ok: Bool) {
        if let f = filter, !what.lowercased().contains(f) { return }
        total += 1
        if !ok { failed += 1 }
        print(ok ? "ok  " : "FAIL", what)
        fflush(stdout)
    }
    runOfflineUnitTests(check: check)
    let c = Core(session: -9003)
    runInboxUnitTests(c, check: check)
    try? FileManager.default.removeItem(at: c.transcriptFile)
    try? FileManager.default.removeItem(at: c.questionsURL)
    runCoreUnitTests(check: check)
    runVoiceEngineUnitTests(check: check)
    try? FileManager.default.removeItem(at: transcript)
    try? FileManager.default.removeItem(at: questionsFile)
    if let suite = uitestSuiteName { ud.removePersistentDomain(forName: suite) }
    let elapsed = String(format: "%.2f", Date().timeIntervalSince(t0))
    if failed > 0 {
        print("FAILED \(failed)/\(total) Swift unit checks (\(elapsed)s)")
        exit(1)
    }
    print("ok  \(total)/\(total) Swift unit checks passed (\(elapsed)s)")
    exit(0)
}

/// `Greenlight --selftest`: drives the actions the buttons call
/// (send, Stop, answer buttons, Compact, Codex, Clear) on a scratch session where greenlight runs, plus the menubar width.
func selfTest() {
    let d = ud
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
    runOfflineUnitTests(check: check)
    let syn = AVSpeechSynthesizer(), pf = AVAudioFormat(standardFormatWithSampleRate: 22050, channels: 1)!
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
            c.ping(#"{"ts": 1792700000, "text": "ws-a: merge !1?\nOptions: merge | hold", "key": "ws-a"}"#)
            c.ping(#"{"text": "ws-b: goal for this workspace?"}"#)
            let w = c.waiting
            check("pings land in the inbox with options: \(w.map(\.options))", w.count == 2 && w[0].options == ["merge", "hold"] && w[0].text == "ws-a: merge !1?" && w[1].options.isEmpty)
            check("pings stay out of the chat", c.msgs.count == n)
            c.ping(#"{"text": "ws-a: merge !1?\nOptions: hold | merge", "key": "ws-a"}"#)
            check("changed options replace old inbox chips", c.waiting.first { $0.text == "ws-a: merge !1?" }?.options == ["hold", "merge"])
            c.answer(c.waiting.first { $0.text == "ws-a: merge !1?" }!, "dismissed", send: false)
            c.answer(w[1], "(dismissed)", send: false)
            check("dismissing clears the bell without sending", c.waiting.isEmpty && c.run == nil)
            for t in [#"ws-k: one?\nOptions: a | b"#, #"ws-l: two?\nOptions: c | d"#, "ws-m: three?"] { c.ping("{\"text\": \"\(t)\"}") }
            func key(_ code: Int, _ ch: String, _ m: NSEvent.ModifierFlags = []) -> Bool {
                c.inboxKey(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: m, timestamp: 0, windowNumber: 0, context: nil,
                                            characters: ch, charactersIgnoringModifiers: ch, isARepeat: false, keyCode: UInt16(code))!)
            }
            check("the inbox opens the oldest; ↓ moves down", c.inboxFocus?.text == "ws-k: one?" && key(kVK_DownArrow, "") && c.inboxFocus?.text == "ws-l: two?")
            check("⌫ dismisses the open one and opens the next", key(kVK_Delete, "\u{7f}") && c.waiting.count == 2 && c.inboxFocus?.text == "ws-m: three?")
            check("a digit past its choices, or a letter, isn't taken", !key(kVK_ANSI_1, "1") && !key(kVK_ANSI_X, "x") && c.waiting.count == 2)
            c.ping(#"{"text": "ws-n: four?"}"#)
            check("⇧⌫ asks first; Esc cancels", key(kVK_Delete, "\u{7f}", .shift) && c.inboxConfirm && key(kVK_Escape, "\u{1b}") && !c.inboxConfirm && c.waiting.count == 3)
            _ = key(kVK_Delete, "\u{7f}", .shift)
            check("Return clears all without sending, answered history kept", key(kVK_Return, "\r") && c.waiting.isEmpty && c.run == nil && !c.inboxConfirm
                  && c.questions.filter { $0.text.hasPrefix("ws-m") || $0.text.hasPrefix("ws-n") }.allSatisfy { $0.answer == "(dismissed)" })
            c.ping(#"{"text": "ws-e: choose?\nOptions: old | later"}"#)
            c.ping(#"{"text": "ws-e: choose?\nOptions: new | later"}"#)
            check("unkeyed changed options replace old chips", c.waiting.filter { $0.text == "ws-e: choose?" }.map(\.options) == [["new", "later"]])
            c.answer(c.waiting.first { $0.text == "ws-e: choose?" }!, "(dismissed)", send: false)
            let chatQuestion = Question(text: "ws-f: choose?", options: ["yes", "no"], ping: false)
            c.questions.append(chatQuestion)
            c.ping(#"{"text": "ws-f: choose?\nOptions: later | now"}"#)
            check("an unkeyed ping cannot replace a chat question", c.questions.first { $0.id == chatQuestion.id }?.answer == nil
                  && c.waiting.contains { $0.ping && $0.text == "ws-f: choose?" })
            let pingQuestion = c.waiting.first { $0.ping && $0.text == "ws-f: choose?" }!
            check("a banner reply targets its ping despite same-text chat", c.notificationQuestion("ws-f: choose?", id: pingQuestion.id)?.id == pingQuestion.id)
            let beforeExpired = c.msgs.count
            let unreadBeforeExpired = c.unread
            let missedBeforeExpired = c.missed, hiddenChat = !c.popoverShown()
            c.reply(to: "ws-f: choose?", "stale choice", questionID: UUID())
            check("an expired banner reply never reaches the agent and keeps its context and typed text", c.run == nil && c.msgs.count == beforeExpired + 1
                  && c.msgs.last?.text.contains("Notification: ws-f: choose?") == true
                  && c.msgs.last?.text.contains("Reply: stale choice") == true && c.unread == unreadBeforeExpired
                  && c.missed == missedBeforeExpired + (hiddenChat ? 1 : 0))
            c.reply(to: "ws-f: choose?", "legacy choice")
            check("a legacy banner without an ID keeps the reply for resending", c.run == nil && c.msgs.last?.text.contains("legacy choice") == true)
            c.answer(chatQuestion, "(dismissed)", send: false)
            c.answer(pingQuestion, "(dismissed)", send: false)
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
            c.reply(to: "MR !4321 needs your review", "Which MR number was that about, and what is your name? One line.",
                    questionID: c.waiting.first { $0.text == "MR !4321 needs your review" }?.id)
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
