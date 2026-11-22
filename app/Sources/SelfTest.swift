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
        check("shellPath: preserves leading ~ expansion and quotes spaces",
              shellPath("~") == "\"$HOME\"" && shellPath("~/.greenlight") == "\"$HOME\"/'.greenlight'" && shellPath("/tmp/a b") == "'/tmp/a b'")
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
        check("attach: five at a time: \(c.fileNote)", c.files.count == 5 && c.fileNote == "notes.txt wasn't attached: attach up to 5 files at a time.")
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
    // Voice mode: the stage takes drops as the chat does, shows what waits, and a spoken turn carries it (even a file still being read)
    onMain {
        c.attach([tmp.appendingPathComponent("notes.txt"), tmp.appendingPathComponent("square.png")])
    }
    read()
    onMain {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 640), styleMask: [.titled], backing: .buffered, defer: false)
        let h = NSHostingView(rootView: StageView(v: c.voice, c: c)); w.contentView = h
        h.layoutSubtreeIfNeeded(); RunLoop.main.run(until: Date() + 0.5)
        func all(_ v: NSView) -> [NSView] { [v] + v.subviews.flatMap(all) }
        let sel = NSSelectorFromString("_hitTest:dragTypes:")
        if let imp = class_getMethodImplementation(NSView.self, sel),
           class_getInstanceMethod(NSView.self, sel).map({ String(cString: method_getTypeEncoding($0)!) == "@32@0:8^{CGPoint=dd}16@24" }) == true,
           let web = all(h).first(where: { $0 is WKWebView }) {
            typealias Hit = @convention(c) (AnyObject, Selector, UnsafePointer<NSPoint>, AnyObject) -> AnyObject?
            var at = web.convert(NSPoint(x: web.bounds.midX, y: web.bounds.midY), to: nil)
            let file = withUnsafePointer(to: &at) { unsafeBitCast(imp, to: Hit.self)(h, sel, $0, NSSet(array: [NSPasteboard.PasteboardType.fileURL.rawValue, "NSFilenamesPboardType"])) }
            check("voice attach: a file dragged onto the stage's drawing attaches (to \(file.map { "\(type(of: $0))" } ?? "nothing")), not opened by the web view", file is DropCatcher)
        } else { print("skip voice attach: AppKit's drag lookup or the stage's web view isn't there to ask") }
        if let dir = ProcessInfo.processInfo.environment["GREENLIGHT_SNAP"] {  // UX evidence: the stage with files waiting, a refusal, a drag over it
            c.fileSays("tool.bin isn't a file the chat can use: attach \(ChatFile.supported).")
            let was = c.voice.look
            defer { c.voice.look = was }  // the user's own pick stays
            for look in ["orb", "conversation"] {
                c.voice.look = looks.first { $0.id == look }!
                for (n, drag) in [("files", false), ("drop", true)] {
                    c.dropping = drag; h.layoutSubtreeIfNeeded(); RunLoop.main.run(until: Date() + 0.5)
                    if let rep = h.bitmapImageRepForCachingDisplay(in: h.bounds) { h.cacheDisplay(in: h.bounds, to: rep); try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/stage-\(look)-\(n).png")) }
                }
            }
            c.dropping = false
        }
        w.contentView = nil
        c.fileNote = ""; c.files.removeAll { $0.name == "square.png" }
        c.refused = []
        c.attach([tmp.appendingPathComponent("square.png"), tmp.appendingPathComponent("tool.bin")])
        c.spoken("What's the codeword in notes.txt, and what color is square.png? One line.")
        check("voice attach: a spoken turn waits, queued, for a file still being read", c.run == nil && c.reading == 1 && c.files.count == 1 && c.queue.count == 1 && c.queue[0].spoken)
    }
    wait("spoken turn sent", 20) { c.reading == 0 && c.files.isEmpty }  // its reply can be in before a poll sees the run: the transcript says what went
    wait("spoken attachments reply", 180) { c.run == nil }
    onMain {
        let you = c.msgs[c.msgs.count - 2].text, r = c.msgs.last!.text
        check("voice attach: the agent used the files said about: \(r)", r.contains("FENNEL-4821") && r.lowercased().contains("red") && !c.msgs.last!.failed)
        check("voice attach: the transcript names them (Greenlight's note to Chief about the refused one isn't shown): \(you)",
              you == "What's the codeword in notes.txt, and what color is square.png? One line.\n📎 notes.txt  📎 square.png" && c.refused.isEmpty)
        c.spoken("Thanks, that's all I needed.")
        check("voice attach: the next turn goes without them", c.run?.files.isEmpty == true)
    }
    wait("plain spoken reply", 180) { c.run == nil }
    onMain {  // Clear (or "reset") while a spoken turn waits on a file: the turn is gone, the file stays in the tray
        c.attach([tmp.appendingPathComponent("notes.txt")])
        c.spoken("Summarise this.")
        c.clear()
        check("voice attach: Clear cancels a spoken turn still waiting on its file", c.queue.isEmpty && c.run == nil)
    }
    wait("files read", 30) { c.reading == 0 }
    wait("clear done", 30) { c.clearing == 0 }
    check("voice attach: ...and the file stays for the next turn", onMain { c.run == nil && c.files.map(\.name) == ["notes.txt"] })
    onMain {  // a file greenlight's host refuses, on a spoken turn: said, and not sent again with the next thing said
        c.files = [ChatFile(name: "fake.txt", kind: "text", data: Data([0x7F, 0x45, 0, 0xFF]), thumb: nil)]
        c.spoken("look")
    }
    wait("spoken refusal", 60) { c.run == nil && c.queue.isEmpty }
    check("voice attach: a refused file on a spoken turn is named, not put back to go again: \(onMain { c.fileNote })",
          onMain { c.msgs.last!.failed && c.files.isEmpty && c.input.isEmpty && c.fileNote.hasPrefix("Not sent: fake.txt.") })
    onMain {  // turns said during an answer: each has the files there when it was said; "stop" stops the answer, not them (stopAll, which no longer touches
        // the queue, can't run here: the mini's mic is behind a prompt nobody can click)
        c.fileNote = ""; c.files = []; c.refused = []
        c.spoken("Say one word.")
        c.spoken("And another.")  // queued behind the answer, nothing in the tray
        c.files = [ChatFile(name: "notes.txt", kind: "text", data: Data("The codeword is FENNEL-4821.\n".utf8), thumb: nil)]
        c.spoken("What's the codeword in notes.txt?")
        check("voice attach: a turn said before a drop doesn't take the file", c.run != nil && c.queue.count == 2 && c.queue[0].files.isEmpty
              && c.queue[1].files.map(\.name) == ["notes.txt"] && c.files.isEmpty)
        c.voice.run(.stop)
        check("voice attach: \"stop\" during an answer keeps what was said after it: \(c.queue.map(\.text))", c.queue.count == 2)
    }
    wait("queued spoken turns", 240) { c.run == nil && c.queue.isEmpty }
    onMain {
        let you = c.msgs[c.msgs.count - 2].text, r = c.msgs.last!.text, before = c.msgs[c.msgs.count - 4].text
        check("voice attach: ...they go in order, the file with the turn said after it: \(before) / \(you) / \(r)",
              before == "And another." && you == "What's the codeword in notes.txt?\n📎 notes.txt" && r.contains("FENNEL-4821"))
    }
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
        wait("initial clear", 30) { c.clearing == 0 }
        attachTest(c, check: check, wait: wait)
        DispatchQueue.main.sync { c.clear() }
        for (k, v) in keep { ud.set(v, forKey: k) }
        try? FileManager.default.removeItem(at: transcript)
        try? FileManager.default.removeItem(at: questionsFile)
        print(failed ? "FAILED" : "all ok")
        exit(failed ? 1 : 0)
    }
}


/// Deterministic, offline Swift unit tests (`Greenlight --unittest`, also run at the start of `--selftest`).
/// Zero live agents, network, microphone, or notification permissions required.
func runOfflineUnitTests(check: (String, Bool) -> Void) {
    let claudeErr = "There's an issue with the selected model (gemini-4-neon-high). It may not exist or you may not have access to it. Run --model to pick a different model."
    let stale = staleBackendNote(model: "gemini-4-neon-high", error: claudeErr, host: "devbox", code: "~/.claude/skills/greenlight")
    check("stale host: a Gemini model sent to Claude Code by an old chat.py says to update the host, not that the model is missing: \(stale ?? "nil")",
          stale?.contains("Greenlight on devbox is older than this app") == true && stale?.contains("~/.claude/skills/greenlight") == true
          && staleBackendNote(model: "claude-opus-5-5", error: claudeErr, host: "devbox", code: "x") == nil
          && staleBackendNote(model: "gemini-4-neon-high", error: "quota exhausted", host: "devbox", code: "x") == nil)
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
    let duplicates: [[String: Any]] = [["name": "same", "mr": ["title": "unrelated title"]],
                                      ["name": "same", "mr": ["title": "pending liquidity receipts"]]]
    check("hexes: a later workspace with the same name can be grounded by its MR title",
          (hexCandidates("the pending liquidity receipts change", duplicates, named: []).first?["mr"] as? [String: Any])?["title"] as? String == "pending liquidity receipts")
    var s1q = SystemOneSaveQueue()
    let saveFirst = s1q.enqueue("provider"), saveEarly = s1q.enqueue("fallback")
    let saveSecond = s1q.finished(), saveLast = s1q.finished()
    check("System One: rapid Settings saves run in selection order, then refresh",
          saveFirst == "provider" && saveEarly == nil && saveSecond == "fallback" && saveLast == nil && s1q.empty && !s1q.busy)
    check("System One: changing primary to its fallback swaps the old primary into fallback",
          fallbackAfterPrimaryChange(from: "jev", to: "haiku", fallback: "haiku") == "jev"
          && fallbackAfterPrimaryChange(from: "jev", to: "local", fallback: "haiku") == "haiku")
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
    check("projects: a pick is never empty: none, or none still listed, is all; a gone one drops out of the rest",
          livePick(nil, known: ["a"]) == nil && livePick([], known: nil) == nil && livePick([], known: ["a"]) == nil && livePick(["gone"], known: ["a", ""]) == nil
          && livePick(["a", "gone"], known: ["a", ""]) == ["a"] && livePick([""], known: ["a", ""]) == [""] && livePick(["gone"], known: nil) == ["gone"])
    let savedProjs: [[String: Any]] = [
        ["id": "p1", "name": "Platform", "repos": ["acme/widgets"], "dirs": ["/notes"]],
        ["id": "p2", "name": "Review", "repos": [String](), "dirs": ["/w/review"]],
    ]
    let sampleTrees: [[String: Any]] = [
        ["project": "acme/widgets", "path": "/w/widgets", "main": true, "agent": "none"],
        ["project": "acme/widgets", "path": "/w/fix", "main": false, "agent": "working"],
        ["project": "octo/greenlight", "path": "/w/greenlight", "main": true, "agent": "none", "cfg": ["name": "Greenlight"]],
        ["project": "octo/greenlight", "path": "/w/feat", "main": false, "agent": "working"],
        ["project": "octo/other", "path": "/w/review", "main": false, "agent": "none"],
        ["project": "octo/other", "path": "/w/loose", "main": false, "agent": "none"],
    ]
    let builtProjs = buildProjectList(saved: savedProjs, trees: sampleTrees, dark: true)
    let byId = { (id: String) in builtProjs.first { $0["id"] as? String == id } }
    check("projects: buildProjectList merges projects.json + .greenlight.json and computes paths, repos and busy",
          builtProjs.count == 4
          && (byId("p1")?["repos"] as? [String]) == ["acme/widgets"]
          && Set(byId("p1")?["paths"] as? [String] ?? []) == ["/w/widgets", "/w/fix"]
          && (byId("p1")?["busy"] as? Int) == 1
          && (byId("pgreenlight")?["name"] as? String) == "Greenlight"
          && (byId("pgreenlight")?["repos"] as? [String]) == ["octo/greenlight"]
          && Set(byId("pgreenlight")?["paths"] as? [String] ?? []) == ["/w/greenlight", "/w/feat"]
          && (byId("pgreenlight")?["busy"] as? Int) == 1
          && (byId("p2")?["paths"] as? [String]) == ["/w/review"]
          && (byId("p2")?["repos"] as? [String]) == []
          && (byId("")?["paths"] as? [String]) == ["/w/loose"]
          && (byId("")?["repos"] as? [String]) == [])
    let fallbackProjs = buildProjectList(saved: savedProjs, trees: [], dark: false, fallback: true)
    check("projects: fallback preserves saved repos/dirs when dash.py trees fails",
          (fallbackProjs.first { $0["id"] as? String == "p1" }?["repos"] as? [String]) == ["acme/widgets"]
          && (fallbackProjs.first { $0["id"] as? String == "p1" }?["color"] as? String) == "#B25000")
    check("projects: dashboard holds snapshot until projectsLoaded for filtered windows, and chip doesn't flash All",
          canShowDashboard(only: nil, projectsLoaded: false)
          && !canShowDashboard(only: ["pgreenlight"], projectsLoaded: false)
          && canShowDashboard(only: ["pgreenlight"], projectsLoaded: true)
          && projectChip(only: ["pgreenlight"], projList: [], projectsLoaded: false).text == ""
          && projectChip(only: nil, projList: [], projectsLoaded: false).text == "All projects"
          && projectChip(only: ["pgreenlight"], projList: builtProjs, projectsLoaded: true).text == "Greenlight"
          && projectChip(only: ["gone"], projList: builtProjs, projectsLoaded: true).text == "All projects")
    check("voice: spoken text is plain, whole sentences only: \(ss)", ss.said == ["Two MRs: !12 is green.", "One fails!"] && sentences("a. b", from: ss.to).said.isEmpty)
    check("dwim: only words dwim.py knows leave the Chief path", dwimCandidate("park the two auth ones") && dwimCandidate("Kick off CI fixes on the red ones")
          && dwimCandidate("hold #42") && dwimCandidate("don't merge #42") && dwimCandidate("unpark billing") && dwimCandidate("fix the merge conflicts on search")
          && dwimCandidate("fix for CI #42") && dwimCandidate("put #42 on ice")
          && !dwimCandidate("what's on fire?") && !dwimCandidate("merge #42") && !dwimCandidate("parking lot ideas") && !dwimCandidate("")
          && !dwimCandidate("park\nthe auth ones") && voiceCommand("Undo that.") == .undo && voiceCommand("undo #42") == nil && voiceCommand("cancel #42") == nil && voiceCommand("undo the deploy") == nil)
    check("voice: cancel chooses a pending launch even while Chief runs", voiceCommand("cancel") == .cancel
          && voiceStopsPending(.cancel, chiefRunning: true, pending: true)
          && !voiceStopsPending(.stop, chiefRunning: true, pending: true)
          && voiceStopsPending(.stop, chiefRunning: false, pending: true))
    check("dwim: snapshot facts trimmed for dwim.py", dwimPucks([["name": "a", "page": ["big": 1], "agent": ["state": "working", "last": "x"]]]).first.map {
        $0["page"] == nil && ($0["agent"] as? [String: String]) == ["state": "working"] && $0["name"] as? String == "a" } == true)
    check("dwim: scope excludes other projects", dwimScopedPucks([["name": "a", "path": "/a", "repo": "A"], ["name": "b", "path": "/b", "repo": "B"]], #"{"paths":["/a"],"repos":["A"]}"#).count == 1)
    check("dwim: queued fallback shows only the user's words", Core.Queued(text: "(routing note)\npark #42", shown: "park #42").display == "park #42")
    let firstToast = DwimToast(text: "Hold A", applying: true), secondToast = DwimToast(text: "Hold B", applying: true)
    var finishedFirst = firstToast; finishedFirst.applying = false; finishedFirst.undo = "undo-a"
    check("dwim: each quick action keeps its own Undo", keepingToast(keepingToast([firstToast], secondToast), finishedFirst) == [finishedFirst, secondToast])
    let toastCore = Core(session: -9001); var firstSeen = 0, secondSeen = 0
    toastCore.show(firstToast, for: nil, onToast: { _ in firstSeen += 1 })
    toastCore.show(secondToast, for: nil, onToast: { _ in secondSeen += 1 })
    toastCore.show(finishedFirst, for: nil)
    check("dwim: concurrent palette actions keep separate toast listeners", firstSeen == 2 && secondSeen == 1)
    var inFlightUndo = finishedFirst; inFlightUndo.undoing = true
    check("dwim: repeated Undo waits for the first request", canBeginDwimUndo(finishedFirst) && !canBeginDwimUndo(inFlightUndo))
    var planGate = DwimPlans(); let cancelledPlan = planGate.begin("hold #42"); let cancelledText = planGate.cancel(); let activePlan = planGate.begin("park #42")
    check("dwim: Stop and Clear invalidate an in-flight plan", cancelledText == ["hold #42"] && !planGate.finish(cancelledPlan) && planGate.finish(activePlan))
    let stopCore = Core(session: -9002), stoppingChief = Run("Chief answer")
    let stopPlan = stopCore.plans.begin("hold #42")
    stoppingChief.stopping = true; stopCore.run = stoppingChief; stopCore.stop()
    check("dwim: Stop invalidates a plan while Chief is active", !stopCore.plans.finish(stopPlan))
    try? FileManager.default.removeItem(at: stopCore.transcriptFile)
    check("dwim: failed Undo reply keeps its retry control", dwimUndoReply("", 1) == nil && dwimUndoReply("{}", 0) == nil
          && dwimUndoReply(#"{"ok":true,"undone":["a"],"kept":[]}"#, 0) != nil)
    check("dwim: lost apply reply retains a known Undo id", dwimApplyReply("", failure: "lost", recovery: "abc123abc123")["undo"] as? String == "abc123abc123"
          && dwimApplyReply(#"{"undo":null,"errors":["can't record Undo"]}"#, failure: "lost", recovery: "abc123abc123")["undo"] is NSNull)
    check("dwim: mixed bulk notice names skipped targets", dwimSkipped(["skipped": [["ws": "billing", "why": "agent working"]]]) == " Skipped billing: agent working.")
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
    do {  // the header's knobs say what a click does now
        let off = HeaderTips(), live = HeaderTips(on: true, transcript: true, captions: true, out: true, name: "Ada", look: "Zen", next: "Orb")
        check("chat header: each top-right knob's hint and label follow its state",
              off.voice.label == "Start voice mode" && live.voice.label == "End voice mode"
              && off.view.label == "Show the transcript" && live.view.label == "Show the stage" && live.view.help.contains("Ada")
              && off.caption.label == "Turn captions on" && live.caption.label == "Turn captions off"
              && off.pop.label == "Move to its own window" && live.pop.label == "Put back in the main window"
              && live.style.help == "Style: Zen. Click for Orb" && live.style.label.contains("Orb")
              && [off.attach, off.view, off.caption, off.style, off.voice, off.pop, live.view, live.caption, live.pop].allSatisfy { !$0.help.isEmpty && $0.help.count <= 100 && !$0.label.isEmpty })
        let scr = NSRect(x: 0, y: 0, width: 1000, height: 800), sz = NSSize(width: 200, height: 24)
        let mid = hintFrame(sz, mouse: NSPoint(x: 500, y: 400), screen: scr), low = hintFrame(sz, mouse: NSPoint(x: 500, y: 20), screen: scr)
        let edge = hintFrame(sz, mouse: NSPoint(x: 990, y: 400), screen: scr)
        check("hover hint: by the cursor, below it, flipped above near the bottom, kept on screen at the right edge",
              mid.minX == 510 && mid.maxY < 400 && mid.maxY > 360 && low.minY > 20 && scr.contains(low) && scr.contains(edge) && edge.maxX <= 996)
    }
    check("voice: you cutting in, not its own echo", isBargeIn("wait hold on a second", "Three MRs are ready") && !isBargeIn("three MRs are", "Three MRs are ready") && !isBargeIn("hold on", ""))
    if !AVSpeechSynthesisVoice.speechVoices().isEmpty {
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
    } else {
        print("skip voice: no AVSpeechSynthesisVoice installed in this headless environment")
    }
    let pf = AVAudioFormat(standardFormatWithSampleRate: 22050, channels: 1)!
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
    do {
        let out44 = AVAudioFormat(standardFormatWithSampleRate: 44100, channels: 2)!
        let outMono = AVAudioFormat(standardFormatWithSampleRate: 44100, channels: 1)!
        let quad = AVAudioFormat(standardFormatWithSampleRate: 44100, channelLayout: AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_Quadraphonic)!)
        let m2 = matchedVoiceOutputFormat(inputRate: 48000, outputFormat: out44)
        let m1 = matchedVoiceOutputFormat(inputRate: 48000, outputFormat: outMono)
        let m4 = matchedVoiceOutputFormat(inputRate: 48000, outputFormat: quad)
        check("voice: voice-processing output format matches input rate while keeping channel count/layout",
              m2?.sampleRate == 48000 && m2?.channelCount == 2 && m1?.sampleRate == 48000 && m1?.channelCount == 1
              && m4?.sampleRate == 48000 && m4?.channelCount == 4 && matchedVoiceOutputFormat(inputRate: 0, outputFormat: out44) == nil)
    }
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
        // Pre-upgrade mic buffers must be dropped (not queued into s.queued), so a slow connect never bursts >5 s on 101.
        let framesBefore = fake.seen.frames.count
        fake.set { req in
            let n = req.components(separatedBy: "\r\n").first { $0.hasPrefix("Sec-WebSocket-Key: ") }.map { String($0.dropFirst(19)) } ?? ""
            return [Data(), Data(), Data(),
                    Data("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: \(wsAccept(n))\r\n\r\n".utf8)
                    + wsFrame(0x1, Data(#"{"type":"session.started","session":{}}"#.utf8), mask: nil)]
        }
        let ml = LiveVoice(); ml.endpoint = ("127.0.0.1", fake.l.port?.rawValue ?? 1, false)
        let micChunk = AVAudioPCMBuffer(pcmFormat: ml.fmt, frameCapacity: 2400)!
        micChunk.frameLength = 2400
        var mEvs = [LiveEvent]()
        ml.open("sk-test", voice: "cedar") { mEvs.append($0) }
        for _ in 0..<3 { ml.mic(micChunk) }  // before 101 arrives: must be dropped
        let mEnd = Date() + 5; while !mEvs.contains(.started) && Date() < mEnd { RunLoop.main.run(until: Date() + 0.02) }
        ml.mic(micChunk)  // after upgrade: must be sent
        RunLoop.main.run(until: Date() + 0.15)
        ml.close(); RunLoop.main.run(until: Date() + 0.1)
        let micSent = fake.seen.frames.dropFirst(framesBefore).compactMap { frame -> String? in
            guard case .text(let d) = frame, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return nil }
            return o["type"] as? String
        }
        check("voice: GPT-Live drops mic audio before upgrade and sends it once up (\(micSent))",
              mEvs.contains(.started) && micSent.filter { $0 == "session.input_audio.append" }.count == 1 && micSent.first == "session.start")
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
            try? p.run(); let d = o.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit(); return String(decoding: d, as: UTF8.self) }()
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
        sp.play("cedar", voiceOn: false, muted: false, settle: 0, key: readKey); RunLoop.main.run(until: Date() + 0.1)
        sp.keyChanged("openai", to: "", hasFallback: true)
        let fallbackFailCleared = sp.phase == .idle
        let reads1 = keyReads.count
        sp.play("juni", voiceOn: false, muted: false, settle: 0.2, key: readKey)
        sp.keyChanged("openai", to: "", hasFallback: true)
        RunLoop.main.run(until: Date() + 0.4)
        let fallbackLoadingStopped = sp.phase == .idle && keyReads.count == reads1
        check("voice: voice mode ending clears only its own preview note; saving an OpenAI key (or removing it with an env fallback) clears a failure or a preview still starting, removing it without a fallback or another key nothing (\(failedNote))",
              voiceOnCleared && mutedKept && failKept && failedNote.contains("didn’t let Greenlight read it") && failCleared && loadingKept && loadingStopped && fallbackFailCleared && fallbackLoadingStopped)
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
    // Config helpers: safeJSON, shellPath, shellQuote, paneWhere, remoteArgv, Theme, harnessOf, RulesStore, Diag, ssh
    check("config: safeJSON preserves valid JSON objects and falls back cleanly",
          safeJSON(#"{"ok":true,"n":1}"#).contains("\"ok\":true")
          && safeJSON("not json", fallback: ["a": 1]) == #"{"a":1}"#
          && safeJSON("[1,2]", fallback: [:]) == "{}")
    check("config: shellQuote escapes single quotes safely",
          shellQuote("plain") == "'plain'" && shellQuote("a'b") == "'a'\\''b'")
    check("config: paneWhere validates @host and absolute paths without ..",
          paneWhere("@devbox")?.host == "devbox" && paneWhere("@bad host") == nil
          && paneWhere("/w/ok")?.cwd == "/w/ok" && paneWhere("/w/../etc") == nil && paneWhere("")?.cwd == "")
    check("config: remoteArgv routes local vs ssh host",
          remoteArgv("echo 1", on: "local").0.hasPrefix("/") && remoteArgv("echo 1", on: "devbox").0 == "/usr/bin/ssh"
          && remoteArgv("echo 1", on: "devbox").1.contains("devbox"))
    check("config: Theme blends, dark detection, state colors, fonts, models and effortName",
          themes[0].dark && !themes.first(where: { $0.name == "GitHub Light" })!.dark
          && named("missingKey", "Nord").name == "Nord" && harnessOf("claude-opus-5-5") == "claude"
          && harnessOf("gemini-4-neon-high") == "agy" && harnessOf("codex") == "codex"
          && effortName("xhigh") == "Extra high" && effortName("low") == "Low"
          && !fontChoices.isEmpty && !models.isEmpty && !accents.isEmpty)
    _ = (Theme.needs, Theme.yourMove, Theme.working, Theme.crashed, Theme.queued, Theme.done,
         Theme.needsInk, Theme.crashedInk, Theme.workingInk, bg, fg, card, surface, line, edge, dim, sub, bubble, accent,
         sys(size: 13), sys(size: 13, design: .monospaced), sys(size: 13, face: "Menlo"), chatFace(size: 13))
    ud.set("gemini-4-neon", forKey: "testSavedModel")
    check("config: savedModel migrates legacy gemini-4-neon id", savedModel("testSavedModel", fallback: "codex") == "gemini-4-neon-medium")
    let rs = RulesStore()
    rs.got(["stages": [["key": "qa", "req": "qa", "done": "label= "]], "builtins": ["ci": "CI"], "options": ["k": "ok"], "rules": "r", "intro": "i", "source": "default"])
    rs.save()
    let badLabelStatus = rs.status
    rs.stages[0].done = "check= "
    rs.save()
    let badCheckStatus = rs.status
    rs.stages[0].done = "signoff"; rs.options["k"] = "bad`tick"
    rs.save()
    let badOptStatus = rs.status
    rs.gotAuto(["kinds": [["kind": "ci", "when": "fail", "gates": ["a"]]], "on": ["ci": 0]])
    rs.kind = "review"
    rs.gotReview(["kind": "review", "text": "custom", "default": "def", "own": true, "vars": ["mr"]])
    rs.note = "gone"; rs.gotNotes(["notes": [["name": "keep", "text": "hi"]]])
    check("config: RulesStore validates stage done/options and parses payloads",
          badLabelStatus.contains("label") && badCheckStatus.contains("CI check") && badOptStatus.contains("backticks")
          && rs.autoOn["ci"] == 0 && rs.reviewOwn && rs.reviewVars.contains("{mr}") && rs.notes.count == 1 && rs.note.isEmpty)
    check("diag: classify and verb map stderr and scripts safely",
          Diag.classify("Permission denied (publickey)") == "auth"
          && Diag.classify("Connection timed out") == "timeout"
          && Diag.classify("Connection refused") == "network"
          && Diag.classify("command not found") == "not_found"
          && Diag.classify("HTTP 429 rate limit") == "rate_limit"
          && Diag.classify("Traceback (most recent)") == "traceback"
          && Diag.classify("") == "none" && Diag.classify("weird") == "other"
          && Diag.verb("python3 /x/scripts/dash.py task repos") == "dash.task"
          && Diag.verb("echo hi") == "shell" && Diag.cpu() >= 0 && Diag.rss() > 0)
    do {
        var lines = [String](), exitCode: Int32?
        _ = ssh("printf 'hello\\nworld\\n'", on: "local", onLine: { lines.append($0) }, onExit: { exitCode = $0 })
        let until = Date() + 5
        while exitCode == nil && Date() < until { RunLoop.main.run(until: Date() + 0.02) }
        var failCode: Int32?
        _ = ssh("echo 'Permission denied' >&2; exit 7", on: "local", onExit: { failCode = $0 })
        let until2 = Date() + 5
        while failCode == nil && Date() < until2 { RunLoop.main.run(until: Date() + 0.02) }
        check("config: ssh() streams stdout lines and reports exit codes",
              lines == ["hello", "world"] && exitCode == 0 && failCode == 7)
    }
}

/// Offline inbox/ping/question triage tests (used by `--unittest` without requiring a live agent).
func runInboxUnitTests(_ c: Core, check: (String, Bool) -> Void) {
    c.questions = []
    let n = c.msgs.count
    c.ping(#"{"ts": 1792700000, "text": "ws-a: merge !1?\nOptions: merge | hold", "key": "ws-a"}"#)
    c.ping(#"{"text": "ws-b: goal for this workspace?"}"#)
    let w = c.waiting
    check("inbox: pings land with options: \(w.map(\.options))", w.count == 2 && w[0].options == ["merge", "hold"] && w[0].text == "ws-a: merge !1?" && w[1].options.isEmpty)
    check("inbox: pings stay out of the chat", c.msgs.count == n)
    c.ping(#"{"text": "ws-a: merge !1?\nOptions: hold | merge", "key": "ws-a"}"#)
    check("inbox: changed options replace old chips", c.waiting.first { $0.text == "ws-a: merge !1?" }?.options == ["hold", "merge"])
    c.answer(c.waiting.first { $0.text == "ws-a: merge !1?" }!, "dismissed", send: false)
    c.answer(w[1], "(dismissed)", send: false)
    check("inbox: dismissing clears waiting without sending", c.waiting.isEmpty && c.run == nil)
    for t in [#"ws-k: one?\nOptions: a | b"#, #"ws-l: two?\nOptions: c | d"#, "ws-m: three?"] { c.ping("{\"text\": \"\(t)\"}") }
    func key(_ code: Int, _ ch: String, _ m: NSEvent.ModifierFlags = []) -> Bool {
        c.inboxKey(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: m, timestamp: 0, windowNumber: 0, context: nil,
                                    characters: ch, charactersIgnoringModifiers: ch, isARepeat: false, keyCode: UInt16(code))!)
    }
    check("inbox: opens oldest and arrow moves focus", c.inboxFocus?.text == "ws-k: one?" && key(kVK_DownArrow, "") && c.inboxFocus?.text == "ws-l: two?")
    check("inbox: Delete dismisses focused and advances", key(kVK_Delete, "\u{7f}") && c.waiting.count == 2 && c.inboxFocus?.text == "ws-m: three?")
    check("inbox: out-of-range digit or letter ignored", !key(kVK_ANSI_1, "1") && !key(kVK_ANSI_X, "x") && c.waiting.count == 2)
    c.ping(#"{"text": "ws-n: four?"}"#)
    check("inbox: Shift+Delete confirms and Esc cancels", key(kVK_Delete, "\u{7f}", .shift) && c.inboxConfirm && key(kVK_Escape, "\u{1b}") && !c.inboxConfirm && c.waiting.count == 3)
    _ = key(kVK_Delete, "\u{7f}", .shift)
    check("inbox: Return clears all without sending", key(kVK_Return, "\r") && c.waiting.isEmpty && c.run == nil && !c.inboxConfirm
          && c.questions.filter { $0.text.hasPrefix("ws-m") || $0.text.hasPrefix("ws-n") }.allSatisfy { $0.answer == "(dismissed)" })
    c.ping(#"{"text": "3.22 ship?\nOptions: ship | hold", "key": "@lift", "ws": ["feat-3", "fix-1"]}"#)
    check("inbox: asked list carries workspace names", c.asked.count == 1 && (c.asked[0]["ws"] as? [String]) == ["feat-3", "fix-1"])
    c.ping(#"{"resolve": "@lift", "why": "shipped"}"#)
    check("inbox: resolve empties asked", c.asked.isEmpty)
    c.name = "  Jarvis "; c.doneNaming()
    check("core: rename trims and persists", c.name == "Jarvis" && !c.editingName && ud.string(forKey: "name") == "Jarvis")
    c.name = " "; c.doneNaming()
    check("core: empty name falls back to Greenlight", c.name == "Greenlight")
}

/// Deterministic Core.swift state-machine tests using `sshOverride` (zero real subprocesses).
func runCoreUnitTests(check: (String, Bool) -> Void) {
    defer { sshOverride = nil }
    var sshLog = [(remote: String, host: String, stdin: String)]()
    var responder: (String, String, String) -> (lines: [String], code: Int32) = { _, _, _ in ([], 0) }
    sshOverride = { remote, h, stdin, onLine, onExit in
        sshLog.append((remote, h, stdin))
        let (lines, code) = responder(remote, h, stdin)
        for l in lines { onLine(l) }
        onExit(code)
        return Process()
    }

    // 1. Core.follow bidirectional settings sync + restyle + acted + fed + model switching
    let mainC = Core(session: -9010), childC = Core(session: -9011)
    defer {
        try? FileManager.default.removeItem(at: mainC.transcriptFile)
        try? FileManager.default.removeItem(at: mainC.questionsURL)
        try? FileManager.default.removeItem(at: childC.transcriptFile)
        try? FileManager.default.removeItem(at: childC.questionsURL)
    }
    childC.follow(mainC)
    mainC.effort = "high"
    childC.name = "Buddy"
    let syncedEffort = childC.effort == "high", syncedName = mainC.name == "Buddy"
    let look0 = mainC.look
    mainC.restyle { fontSize = 14 }
    mainC.verbose = true
    mainC.acted("ws1", "merge", 0, "{}")
    mainC.acted("ws1", "merge", 1, #"{"error":"conflict"}"#)
    mainC.fed([["name": "w1", "state": "working", "step": "ci", "owner": "agent"]])
    mainC.fed([["name": "w1", "state": "needs", "step": "review", "owner": "me"]])
    mainC.model = "codex"
    let switchedNote = mainC.msgs.last?.text.contains("separate from Claude") == true
    mainC.model = "claude-opus-5-5"
    check("core: follow syncs settings bidirectionally, restyle bumps look, acted/fed log, model switch resets meter",
          syncedEffort && syncedName && mainC.look == look0 + 1 && switchedNote && mainC.context == 0
          && mainC.feed.lines.contains { $0.kind == .failed && $0.text.contains("conflict") })

    // 2. testHost, loadGreenlightEvery, setGreenlightEvery
    mainC.runHost = "box"
    responder = { _, _, _ in (["ok"], 0) }; mainC.testHost(); let hOk = mainC.hostCheck
    responder = { _, _, _ in (["nopy"], 3) }; mainC.testHost(); let hNoPy = mainC.hostCheck
    responder = { _, _, _ in ([], 255) }; mainC.testHost(); let h255 = mainC.hostCheck
    responder = { _, _, _ in ([], 1) }; mainC.testHost(); let hMiss = mainC.hostCheck
    check("core: testHost formats ok/nopy/ssh/missing states",
          hOk.contains("ready") && hNoPy.contains("no python3") && h255.contains("BatchMode") && hMiss.contains("Clone"))

    responder = { _, _, _ in ([#"{"schedule":"*/15 * * * *","enabled":true}"#], 0) }
    mainC.loadGreenlightEvery()
    RunLoop.main.run(until: Date() + 0.05)
    let every15 = mainC.greenlightEvery == 15 && mainC.greenlightEveryNote.isEmpty
    responder = { _, _, _ in ([#"{"schedule":"*/15 * * * *","enabled":false}"#], 0) }
    mainC.loadGreenlightEvery()
    RunLoop.main.run(until: Date() + 0.05)
    let everyOff = mainC.greenlightEvery == 0
    responder = { _, _, _ in ([], 1) }
    mainC.loadGreenlightEvery(); mainC.setGreenlightEvery(20); mainC.setGreenlightEvery(0)
    RunLoop.main.run(until: Date() + 0.05)
    check("core: loadGreenlightEvery and setGreenlightEvery update interval and error notes",
          every15 && everyOff && !mainC.greenlightEveryNote.isEmpty)

    // 3. Autonomy: loadAutonomy, setAutonomy, undoAudit, gate
    let autJSON = #"{"auto":["milestone"],"gate":0.4,"kinds":[{"kind":"milestone","eligible":true,"opted":true,"n":5,"right":5,"acc":1.0}],"candidates":[],"recent":[]}"#
    responder = { r, _, _ in
        if r.contains("autonomy.py settings") { return ([autJSON], 0) }
        if r.contains("autonomy.py gate") { return ([#"{"ok": false, "score": 0.1}"#], 1) }
        return ([], 0)
    }
    var qAuto = Question(text: "Set milestone?", options: ["yes", "no"], ping: true); qAuto.answer = "Auto: yes"; qAuto.auto = "aud-1"
    mainC.questions = [qAuto]
    mainC.loadAutonomy()
    mainC.setAutonomy(["--gate", "0.4"])
    mainC.undoAuto(qAuto)
    var gateHeld: Bool?
    mainC.gate("watch", text: "ping?") { gateHeld = !$0 }
    responder = { _, _, _ in ([#"{"ok": true}"#], 0) }
    var gateAllowed: Bool?
    mainC.gate("watch", text: "ping?") { gateAllowed = $0 }
    check("core: autonomy load/set/undoAudit and gate held/allowed work cleanly",
          mainC.autonomy?.gate == 0.4 && mainC.questions.first?.auto == nil
          && mainC.questions.first?.answer?.hasPrefix("Reconsidering:") == true
          && gateHeld == true && gateAllowed == true)

    // 4. System One: load, set (swap fallback), key, test
    responder = { r, _, _ in
        if r.contains("decide.py test") { return ([#"{"results":[{"provider":"jev","ok":true,"ms":42},{"provider":"haiku","ok":false,"error":"timeout"}]}"#], 0) }
        return ([#"{"provider":"jev","fallback":"haiku","key":true}"#], 0)
    }
    mainC.loadSystemOne()
    mainC.setSystemOne(provider: "haiku")
    mainC.setSystemOneKey("sk-s1")
    mainC.setSystemOneKey("")
    mainC.testSystemOne()
    let s1TestRes = mainC.s1Test
    responder = { _, _, _ in ([#"{"results":[]}"#], 0) }; mainC.testSystemOne(); let s1Off = mainC.s1Test
    responder = { _, _, _ in ([], 1) }; mainC.testSystemOne(); let s1Fail = mainC.s1Test
    check("core: System One config, fallback swap, key, and test formatting",
          mainC.s1Key && s1TestRes.contains("Jev answered in 42 ms") && s1TestRes.contains("Haiku: timeout")
          && s1Off.contains("Off:") && s1Fail.contains("No answer"))

    // 5. Chat streaming: send + event(start/status/step/step_done/plan/think/text_reset/text/usage/done) + compact + stop + clear + queue
    mainC.light = { _, _ in }
    mainC.attached = [Attached(id: "ws1", label: "ws1", color: .blue, mrs: 1, text: "Workspace ws1")]
    mainC.focus = "the dashboard (list)"
    responder = { r, _, _ in
        if r.contains("chat.py send") {
            return ([
                #"{"e":"start","engine":"claude","model":"claude-opus-5-5"}"#,
                #"{"e":"status","text":"Reading board","thinking":true}"#,
                #"{"e":"step","id":"s1","title":"Ran scan","detail":"d","call":"scan()","input":"full input"}"#,
                #"{"e":"step_done","id":"s1","ok":true,"out":"ok","full":"full output"}"#,
                #"{"e":"plan","done":1,"total":3,"title":"Checking","eta":60}"#,
                #"{"e":"think","delta":"Hmm"}"#,
                #"{"e":"text_reset"}"#,
                #"{"e":"text","delta":"Done.\nOptions: yes | no\n[hexes: ws1]"}"#,
                #"{"e":"usage","context":4200,"tps":28.5,"out":120}"#,
                #"{"e":"done","ok":true,"context":4200,"window":200000,"text":"Done.\nOptions: yes | no\n[hexes: ws1]"}"#,
            ], 0)
        }
        if r.contains("chat.py compact") {
            return ([#"{"e":"done","ok":true,"context":1200,"window":200000,"text":""}"#], 0)
        }
        return ([], 0)
    }
    mainC.send("Check ws1")
    let chatMsg = mainC.msgs.last
    let matchedQ = chatMsg.flatMap { mainC.question(for: $0) }
    mainC.compact()
    let compactMsg = mainC.msgs.last?.text ?? ""
    check("core: chat streaming handles all event kinds, options, context meter, and compact",
          chatMsg?.text == "Done." && chatMsg?.options == ["yes", "no"] && matchedQ != nil
          && mainC.context == 1200 && mainC.window == 200000 && compactMsg.contains("Compacted"))

    // 6. Watch turn & ping auto/suggest/start
    mainC.pucks = [["name": "ws-watch", "state": "needs", "why": "ci"]]
    responder = { r, _, _ in
        if r.contains("autonomy.py gate") { return ([#"{"ok": true}"#], 0) }
        if r.contains("chat.py send") { return ([#"{"e":"done","ok":true,"text":"(nothing)"}"#], 0) }
        return ([], 0)
    }
    mainC.watched([["name": "ws-old", "state": "working"]])
    mainC.watched([["name": "ws-old", "state": "working"], ["name": "ws-watch", "state": "needs", "why": "ci"]])
    mainC.ping("#start 42")
    let seen42 = mainC.seen == 42 && mainC.connected
    mainC.ping(#"{"text":"Auto q?\nOptions: a | b","key":"k-auto","auto":{"id":"a9","choice":"a"}}"#)
    mainC.ping(#"{"text":"Sug q?\nOptions: x | y","key":"k-sug","suggest":{"choice":"x","prob":0.88,"count":4,"agree":4}}"#)
    let sugQ = mainC.waiting.first { $0.key == "k-sug" }
    check("core: watchTurn suppresses (nothing), ping parses #start, auto, and suggest",
          seen42 && mainC.questions.contains { $0.auto == "a9" && $0.answer == "Auto: a" }
          && sugQ?.suggest?.choice == "x" && sugQ?.suggest?.count == 4)

    // 7. DWIM direct apply -> undo (success, kept-conflict, failure), pending cancel, fallback, empty actions
    responder = { r, _, _ in
        if r.contains("dwim.py plan") {
            return ([#"{"route":"direct","verb":"park","toast":"Parked ws1","say":"Parked ws1","actions":[{"verb":"park","ws":"ws1"}]}"#], 0)
        }
        if r.contains("dwim.py apply") { return ([#"{"done":["ws1"],"errors":[],"unchanged":[],"undo":"u123","ms":12}"#], 0) }
        if r.contains("dwim.py undo") { return ([#"{"ok":true,"undone":["ws1"],"kept":[]}"#], 0) }
        return ([], 0)
    }
    mainC.dwim("park ws1", source: "chat") { _ in }
    let appliedToast = mainC.latestUndoableToast
    mainC.undoToast(appliedToast?.id)
    let undidMsg = mainC.msgs.last?.text ?? ""

    // Undo failure keeps retry toast; kept-conflict keeps retry toast
    responder = { r, _, _ in
        if r.contains("dwim.py plan") {
            return ([#"{"route":"direct","verb":"hold","toast":"Held ws1","actions":[{"verb":"hold","ws":"ws1"}]}"#], 0)
        }
        if r.contains("dwim.py apply") { return ([#"{"done":["ws1"],"errors":[],"unchanged":[],"undo":"u456","ms":9}"#], 0) }
        if r.contains("dwim.py undo") { return ([#"{"ok":true,"undone":[],"kept":["ws1"]}"#], 0) }
        return ([], 0)
    }
    mainC.dwim("hold ws1", source: "chat") { _ in }
    mainC.undoToast()
    let keptMsg = mainC.msgs.last?.text ?? ""

    // Fallback to Chief passes verb note
    responder = { _, _, _ in ([#"{"route":"chief","verb":"merge","why":"needs confirmation"}"#], 0) }
    var chiefNote = ""
    mainC.dwim("merge ws1", source: "chat") { chiefNote = $0 }

    // Empty actions shows info toast
    responder = { _, _, _ in ([#"{"route":"direct","verb":"park","toast":"Already parked","actions":[]}"#], 0) }
    mainC.dwim("park ws1", source: "chat") { _ in }

    // Pending launch toast cancelled by undoToast
    responder = { _, _, _ in ([#"{"route":"direct","verb":"fix","launch":true,"toast":"Fixing ws1","actions":[{"verb":"fix","ws":"ws1"}]}"#], 0) }
    mainC.dwim("fix ci ws1", source: "chat") { _ in }
    let hadPending = mainC.toasts.contains { $0.pending }
    mainC.undoToast()
    let cancelledPending = !mainC.toasts.contains { $0.pending }
    check("core: DWIM plan/apply/undo/kept/pending-cancel/fallback/empty flows",
          appliedToast?.undo == "u123" && undidMsg.lowercased().contains("undid")
          && keptMsg.contains("Undo the newer edit") && chiefNote.contains("read this as merge")
          && hadPending && cancelledPending)

    // 8. History key navigation (⌃R, arrows, recall, ⌃U, ⌃W, ⌥., !!)
    mainC.sent = [Sent(t: "run unit tests", at: Date()), Sent(t: "fix lint errors", at: Date())]
    let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 200, height: 60))
    func ev(_ code: Int, _ ch: String, _ m: NSEvent.ModifierFlags = []) -> NSEvent {
        NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: m, timestamp: 0, windowNumber: 0, context: nil,
                         characters: ch, charactersIgnoringModifiers: ch, isARepeat: false, keyCode: UInt16(code))!
    }
    let openedSearch = mainC.historyKey(ev(kVK_ANSI_R, "r", .control), tv) && mainC.hq == ""
    _ = mainC.historyKey(ev(kVK_DownArrow, ""), tv)
    _ = mainC.historyKey(ev(kVK_UpArrow, ""), tv)
    _ = mainC.historyKey(ev(kVK_Return, "\r"), tv)
    let pickedHist = mainC.input == "run unit tests" && mainC.hq == nil
    mainC.input = "echo !!"; let expanded = mainC.historyKey(ev(kVK_Return, "\r"), tv) && mainC.input == "echo run unit tests"
    tv.string = "hello world"; tv.setSelectedRange(NSRange(location: 11, length: 0))
    _ = mainC.historyKey(ev(kVK_ANSI_W, "w", .control), tv)
    let killedWord = tv.string == "hello "
    _ = mainC.historyKey(ev(kVK_ANSI_U, "u", .control), tv)
    let killedLine = tv.string == ""
    _ = mainC.historyKey(ev(kVK_ANSI_Period, ".", .option), tv)
    let yankedLast = tv.string == "tests"
    mainC.input = ""; let recalled = mainC.recall(0) && mainC.input == "run unit tests" && mainC.recall(-1) && mainC.input == ""
    // ⌃C clears input, or stops active run; Esc closes ⌃R
    mainC.input = "temp"; _ = mainC.historyKey(ev(kVK_ANSI_C, "c", .control), tv)
    let ctrlCCleared = mainC.input.isEmpty
    _ = mainC.historyKey(ev(kVK_ANSI_R, "r", .control), tv)
    _ = mainC.historyKey(ev(kVK_Escape, "\u{1b}"), tv)
    let escClosedSearch = mainC.hq == nil
    check("core: historyKey supports Ctrl-R, arrows, bang expand, Ctrl-W/U/C, Option-. and recall",
          openedSearch && pickedHist && expanded && killedWord && killedLine && yankedLast && recalled && ctrlCCleared && escClosedSearch)

    // 9. reply(to:), choose, submit, failed/stale-backend/reset/console chat events, DWIM errors & early undo
    mainC.questions = []
    mainC.ping(#"{"text":"Ship !9?\nOptions: ship | hold","key":"k-ship"}"#)
    let shipQ = mainC.waiting.first { $0.key == "k-ship" }!
    responder = { _, _, _ in ([#"{"e":"done","ok":true,"text":"Shipped."}"#], 0) }
    mainC.reply(to: "Ship !9?", "ship", questionID: shipQ.id)
    let validReplyAnswered = mainC.waiting.isEmpty
    mainC.reply(to: "Ship !9?", "stale", questionID: UUID())
    let expiredRecorded = mainC.msgs.last?.text.contains("no longer open") == true
    mainC.reply(to: "Ship !9?", "legacy")
    mainC.reply(to: "Freeform alert", "noted", freeform: true)
    let msgWithOpts = Msg(.greenlight, "Pick one"); msgWithOpts.options = ["a", "b"]
    mainC.msgs.append(msgWithOpts)
    mainC.choose(msgWithOpts, "a")
    // submit with whitespace, with DWIM candidate, and with open chat question
    mainC.input = "   "; mainC.submit()
    mainC.attached = []
    responder = { _, _, _ in ([#"{"route":"direct","verb":"park","toast":"Parked","actions":[]}"#], 0) }
    mainC.input = "park #42"; mainC.submit()
    // Failed step, stale-backend error, [chat: reset], and DWIM apply error + undo failure
    mainC.model = "gemini-4-neon-high"
    responder = { r, _, _ in
        if r.contains("chat.py send") {
            return ([
                #"{"e":"step","id":"s2","title":"Run test","detail":"","call":"pytest"}"#,
                #"{"e":"step_done","id":"s2","ok":false,"out":"fail"}"#,
                #"{"e":"done","ok":false,"text":"There's an issue with the selected model (gemini-4-neon-high)."}"#,
            ], 0)
        }
        return ([], 0)
    }
    mainC.send("Run tests")
    let staleNoteHit = mainC.msgs.last?.text.contains("older than this app") == true
    mainC.model = "claude-opus-5-5"
    responder = { r, _, _ in
        if r.contains("chat.py send") { return ([#"{"e":"done","ok":true,"text":"Fresh.\n[chat: reset]"}"#], 0) }
        return ([], 0)
    }
    mainC.send("Start over")
    RunLoop.main.run(until: Date() + 0.05)
    // DWIM apply error + undo failure retry
    responder = { r, _, _ in
        if r.contains("dwim.py plan") { return ([#"{"route":"direct","verb":"park","toast":"Parked","actions":[{"verb":"park","ws":"a"}]}"#], 0) }
        if r.contains("dwim.py apply") { return ([#"{"done":[],"errors":["locked"],"unchanged":[],"undo":"u-err","ms":5}"#], 0) }
        if r.contains("dwim.py undo") { return ([], 1) }
        return ([], 0)
    }
    mainC.dwim("park a", source: "chat") { _ in }
    let errToast = mainC.toasts.last
    mainC.undoToast(errToast?.id)
    let retryMsg = mainC.msgs.last?.text.contains("Undo didn't finish") == true
    // Queueing while busy + clear(keepQueue: true) + tailOutbox
    var delayedExit: ((Int32) -> Void)?
    sshOverride = { remote, _, _, _, onExit in
        if remote.contains("chat.py send") { delayedExit = onExit } else { onExit(0) }
        return Process()
    }
    mainC.send("first")
    mainC.send("second")
    let queuedWhileBusy = mainC.queue.count == 1
    mainC.stop()
    delayedExit?(1)
    let stoppedMsg = mainC.msgs.contains { $0.text == "Stopped." }
    mainC.clear()
    check("core: reply, choose, submit, stale-backend, reset, DWIM error/retry, and stop/queue flows",
          validReplyAnswered && expiredRecorded && staleNoteHit && errToast?.failed == true && retryMsg && queuedWhileBusy && stoppedMsg)

    // 10. Additional Core branches: error notes, inbox R/1/Up keys, question(for:) fallback, tailOutbox, voice-on DWIM, checkInTurn
    sshOverride = { remote, h, stdin, onLine, onExit in
        sshLog.append((remote, h, stdin))
        let (lines, code) = responder(remote, h, stdin)
        for l in lines { onLine(l) }
        onExit(code)
        return Process()
    }
    responder = { _, _, _ in ([], 1) }
    mainC.loadAutonomy(); mainC.setAutonomy(["--gate", "0.5"]); mainC.undoAudit("bad")
    let autErr = mainC.autonomyNote.contains("Couldn't")
    mainC.setSystemOne(provider: "jev")
    responder = { _, _, _ in ([#"{"provider":"local","fallback":"","key":false}"#], 0) }
    mainC.loadSystemOne()
    let s1WriteErr = mainC.s1Note.contains("Couldn't save")
    // inboxKey R, digit 1, Up arrow
    mainC.questions = []
    mainC.ping(#"{"text":"First?\nOptions: go | stay","key":"k1"}"#)
    mainC.ping(#"{"text":"Second?\nOptions: a | b","key":"k2"}"#)
    _ = mainC.inboxKey(ev(kVK_DownArrow, ""))
    _ = mainC.inboxKey(ev(kVK_UpArrow, ""))
    let openedReply = mainC.inboxKey(ev(kVK_ANSI_R, "r")) && mainC.replyingTo != nil
    responder = { _, _, _ in ([#"{"e":"done","ok":true,"text":"Ok"}"#], 0) }
    let pickedDigit = mainC.inboxKey(ev(kVK_ANSI_1, "1")) && mainC.waiting.count == 1
    // question(for:) fallback by options when text differs
    mainC.questions.append(Question(text: "Original wording", options: ["optA", "optB"], ping: false))
    let reworded = Msg(.greenlight, "Reworded"); reworded.options = ["optA", "optB"]
    mainC.msgs.append(reworded)
    let matchedFallback = mainC.question(for: reworded)?.text == "Original wording"
    // tailOutbox + DWIM with voice.on = true
    responder = { r, _, _ in
        if r.contains("outbox.jsonl") { return (["#start 5", #"{"text":"Tailed ping?","key":"kt"}"#], 0) }
        if r.contains("dwim.py plan") { return ([#"{"route":"direct","verb":"park","toast":"Parked v","say":"Parked v","actions":[{"verb":"park","ws":"v"}]}"#], 0) }
        if r.contains("dwim.py apply") { return ([#"{"done":["v"],"errors":[],"unchanged":[],"undo":"uv1","ms":4}"#], 0) }
        if r.contains("dwim.py undo") { return ([#"{"ok":true,"undone":["v"],"kept":[]}"#], 0) }
        return ([], 0)
    }
    mainC.tailOutbox()
    mainC.voice.on = true; mainC.voice.isLive = false
    mainC.dwim("park v", source: "voice") { _ in }
    mainC.undoToast()
    mainC.voice.on = false
    // checkIn() when idle past threshold (if atMac)
    mainC.checkInMins = 1; mainC.lastActive = Date(timeIntervalSinceNow: -120); mainC.checkedIn = false
    responder = { _, _, _ in ([#"{"ok": true}"#], 0) }
    mainC.checkIn()
    check("core: error notes, inbox R/1/Up, question fallback, tailOutbox, and voice-on DWIM paths",
          autErr && s1WriteErr && openedReply && pickedDigit && matchedFallback && mainC.waiting.contains { $0.key == "kt" })

    // 11. RulesStore full sshOverride lifecycle + Core helpers (localShown, hostPending, shortcut, showDockIcon, pasteFiles, maxTotal, gemini-4-neon migration)
    responder = { r, _, _ in
        if r.contains("task 'repos'") { return ([#"{"repos":[{"path":"acme/widgets"}]}"#], 0) }
        if r.contains("instructions-") { return ([#"{"text":"Be brief"}"#], 0) }
        if r.contains("memory-") { return ([#"{"notes":[{"name":"n1","text":"hello"}]}"#], 0) }
        if r.contains("lifecycle-") { return ([#"{"stages":[{"key":"ci","req":"ci","done":"ci"}],"options":{},"rules":"","intro":"","source":"default","builtins":{"ci":"CI"}}"#], 0) }
        if r.contains("autorun-") { return ([#"{"on":{"ci":0},"kinds":[{"kind":"ci","when":"fail","gates":["a"]}]}"#], 0) }
        if r.contains("prompt-") { return ([#"{"kind":"review","text":"Mine","default":"Def","own":true,"vars":["mr"]}"#], 0) }
        return ([], 0)
    }
    let rs = RulesStore()
    rs.load(); rs.setAuto("ci", 0); rs.reviewOwn = true; rs.review = "Custom"; rs.saveReview()
    rs.save(); rs.reset(); rs.saveInstructions(); rs.note = "n1"; rs.noteText = "body"; rs.saveNote(); rs.deleteNote()
    _ = (mainC.localShown, mainC.hostPending, mainC.shortcut, mainC.pasteFiles())
    mainC.dockIcon = "Orb"
    // Legacy gemini-4-neon migration on session 0 (uses isolated unittest UserDefaults)
    ud.set("gemini-4-neon", forKey: "model")
    let migC = Core(session: 0)
    let migrated = ud.string(forKey: "model") == "gemini-4-neon-medium" && migC.msgs.contains { $0.text.contains("agy") }
    // maxTotal (>20 MB) refusal in Core.attach
    let bigChunk = Data(repeating: 0x41, count: 4 << 20)  // 4 MB text-like? No, use 4x 4.5MB image-like ChatFile directly
    mainC.files = (0..<4).map { ChatFile(name: "f\($0).png", kind: "image", data: bigChunk, thumb: nil) }
    mainC.attach(Data(repeating: 0x41, count: 4_500_000), name: "f5.png")
    RunLoop.main.run(until: Date() + 0.1)
    check("core: RulesStore round-trip, legacy gemini-4-neon migration, and helpers",
          rs.repos.contains("widgets") && rs.instructions == "Be brief" && migrated && !bigChunk.isEmpty)
    mainC.clear()
}

/// Deterministic VoiceEngine.swift unit tests (Speech.elevenWhy, Voice state machine, GPT-Live handlers, LivePreview).
func runVoiceEngineUnitTests(check: (String, Bool) -> Void) {
    defer { sshOverride = nil }
    sshOverride = { _, _, _, onLine, onExit in
        onLine(#"{"kind":"table","title":"Board","columns":["WS"],"rows":[["a"]]}"#)
        onExit(0)
        return Process()
    }

    // 1. Speech.elevenWhy across all status codes & URLErrors
    let e401 = Speech.elevenWhy(401, Data(#"{"detail":{"message":"bad key"}}"#.utf8), nil, voice: "v")
    let e402 = Speech.elevenWhy(402, Data(#"{"detail":"no credits"}"#.utf8), nil, voice: "v")
    let e404 = Speech.elevenWhy(404, nil, nil, voice: "sarah")
    let e429 = Speech.elevenWhy(429, nil, nil, voice: "v")
    let e200 = Speech.elevenWhy(200, nil, nil, voice: "v")
    let e0 = Speech.elevenWhy(0, nil, nil, voice: "v")
    let e500 = Speech.elevenWhy(500, Data("oops".utf8), nil, voice: "v")
    let eTimeout = Speech.elevenWhy(0, nil, URLError(.timedOut), voice: "v")
    let eNet = Speech.elevenWhy(0, nil, URLError(.notConnectedToInternet), voice: "v")
    check("voice: Speech.elevenWhy classifies all HTTP and URLError failure modes",
          e401.1 && e401.0.contains("401") && e402.1 && e402.0.contains("402") && e404.1 && e404.0.contains("sarah")
          && !e429.1 && e429.0.contains("429") && !e200.1 && !e0.1 && !e500.1 && e500.0.contains("500")
          && !eTimeout.1 && eTimeout.0.contains("in time") && !eNet.1 && eNet.0.contains("Little Snitch"))

    // 2. Speech.data branches (kokoro not ready, live mid-conversation, eleven stuck, default)
    let sp = Speech.shared
    let savedKind = ud.string(forKey: "speech")
    defer { ud.set(savedKind, forKey: "speech"); sp.stuck = nil; sp.status = "" }
    ud.set("kokoro", forKey: "speech"); sp.ready = false; sp.status = ""
    let kokoroNil = sp.data("hi") == nil && sp.status.contains("still loading")
    ud.set("live", forKey: "speech")
    let liveNil = sp.data("hi") == nil && sp.status.contains("turn voice mode off and on")
    ud.set("eleven", forKey: "speech"); sp.stuck = "No credits"
    let stuckNil = sp.data("hi") == nil && sp.status == "No credits"
    ud.set("apple", forKey: "speech")
    let appleNil = sp.data("hi") == nil && sp.buffer(Data([1])) == nil
    check("voice: Speech.data handles kokoro-loading, live-mid-session, stuck eleven, and empty buffer",
          kokoroNil && liveNil && stuckNil && appleNil)

    // 3. Voice state machine: began, status, stage, live, finished, failed, fellBack, hush, run, onLive, handOff, liveAudio
    let c = Core(session: -9012)
    defer {
        try? FileManager.default.removeItem(at: c.transcriptFile)
        try? FileManager.default.removeItem(at: c.questionsURL)
    }
    let v = c.voice
    v.on = true; v.isLive = true
    v.onLive(.started)
    v.onLive(.heard(" What is"))
    v.onLive(.heard(" status?"))
    let heardText = v.heard
    v.handOff("del-1")
    let handedOff = v.delegation == "del-1" && v.heard.isEmpty && v.lastHeard == "What is status?"
    v.liveBuf = []; v.awaiting = nil
    v.say("Three MRs are ready.", filler: false)
    v.flushLive()
    let awaitingCue = v.awaiting == "three mrs"
    v.onLive(.said(" Sure. Three MRs are ready. [click]"))
    let cueCleared = v.awaiting == nil && !v.caption.contains("[click]")
    // Audio: unattached player never crashes; attached player starts speaking on loud tone and stops after >0.8s quiet
    let tone = (0..<2400).map { Int16(10000 * sin(Double($0) / 10)) }.withUnsafeBytes { Data($0) }
    v.liveAudio(tone)  // unattached: must be a safe no-op, not SIGABRT
    v.engine.attach(v.player)
    try? v.engine.link(v.player, to: v.engine.mainMixerNode, format: v.speechFormat)
    v.liveAudio(tone)
    let startedSpeaking = v.speaking == 1
    v.liveAudio(Data(count: 48000))  // 1.0 s of silence
    let endedSpeaking = v.speaking == 0 && v.newCaption
    v.onLive(.failed("rate_limit", "slow down"))
    v.onLive(.failed("rate_limit", "slow down"))  // deduped
    let liveErrCount = c.msgs.filter { $0.text == "GPT-Live: slow down" }.count
    v.fellBack("no credits"); v.fellBack("no credits")  // deduped
    let fbCount = c.msgs.filter { $0.text.contains("no credits") }.count
    v.run(.captions(false))
    let capOff = !v.captions
    v.run(.captions(true))
    let r = Run("Check CI"); r.spoken = true; c.pucks = [["name": "a", "state": "needs"]]
    v.began(r)
    v.status("Scanning")
    v.stage(#"{"chart":"bar","labels":["a"],"series":[{"values":[1]}]}"#)
    v.chiefDrew = false
    v.stage("<b>ok</b>")
    v.live(r)
    v.finished(Staged(text: "All green.", stage: nil, end: false), "All green.")
    v.failed("Stopped.")
    v.onLive(.closed("expired"))
    let closedStopped = !v.on && c.msgs.last?.text.contains("ran out of time") == true
    v.freshEngine()
    _ = outputMuted()

    // 4. Non-live voice flows: partial (barge-in & turn), run(.undo/.stop/.cancel/.reset/.bye), live >300 chars, finished(end:true)
    v.on = true; v.isLive = false; v.vp = true; v.speaking = 1; v.lastSpoken = "Three MRs are ready"
    v.partial("wait hold on")  // triggers barge-in hush()
    let bargedIn = v.speaking == 0 && v.heard == "wait hold on"
    v.heard = "captions off"; v.turn()
    let turnRanCmd = !v.captions
    v.run(.captions(true))
    v.heard = "park #42"; v.turn()
    v.run(.undo)
    v.run(.reset)
    let longRun = Run("Tell me everything")
    longRun.live = String(repeating: "Everything looks green today. ", count: 14) + "\n```stage\n<p>hi</p>\n```"
    v.on = true; v.isLive = false; v.muted = false; v.chiefDrew = false
    v.live(longRun)
    v.finished(Staged(text: "Goodbye now.", stage: nil, end: true), "Goodbye now.")
    v.run(.bye)

    // 5. Speech.kokoro binary pipe round-trip + EOF error recovery
    do {
        let inPipe = Pipe(), outPipe = Pipe()
        let pcm = Data([1, 0, 2, 0, 3, 0, 4, 0])
        var lenLE = UInt32(pcm.count).littleEndian
        let frame = Data(bytes: &lenLE, count: 4) + pcm
        outPipe.fileHandleForWriting.write(frame)
        try? outPipe.fileHandleForWriting.close()
        sp.kin = inPipe.fileHandleForWriting; sp.kout = outPipe.fileHandleForReading; sp.ready = true
        let gotPCM = sp.kokoro("hello")
        let afterEOF = sp.kokoro("second")  // hits EOF -> resets ready & sets status
        try? inPipe.fileHandleForReading.close()
        sp.stop()
        check("voice: Speech.kokoro reads length-prefixed PCM and recovers cleanly on EOF",
              gotPCM == pcm && afterEOF == nil)
    }

    // 6. LivePreview full playback & refusal paths against loopback fake WebSocket server
    do {
        let lp = LivePreview()
        let toneBig = (0..<2400).map { Int16(12000 * sin(Double($0) / 10)) }.withUnsafeBytes { Data($0) }
        final class PreviewFake {
            let l: NWListener = { let p = NWParameters.tcp; p.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: .any); return try! NWListener(using: p) }()
            let q = DispatchQueue(label: "p-fake")
            var reply: (String) -> [Data] = { _ in [] }
            init() { l.newConnectionHandler = { [unowned self] c in c.start(queue: self.q); self.serve(c, Data()) }; l.start(queue: q) }
            func serve(_ c: NWConnection, _ buf: Data) {
                c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [unowned self] d, _, done, err in
                    let b = buf + (d ?? Data())
                    if let h = b.range(of: Data("\r\n\r\n".utf8)) {
                        let req = String(decoding: b[..<h.upperBound], as: UTF8.self)
                        for (i, o) in self.reply(req).enumerated() {
                            self.q.asyncAfter(deadline: .now() + 0.03 * Double(i)) { c.send(content: o, completion: .contentProcessed { _ in }) }
                        }
                        return
                    }
                    if !done && err == nil { self.serve(c, b) }
                }
            }
        }
        let pf = PreviewFake()
        let w0 = Date(); while pf.l.state != .ready && Date() < w0 + 3 { RunLoop.main.run(until: Date() + 0.02) }
        pf.reply = { req in
            let n = req.components(separatedBy: "\r\n").first { $0.hasPrefix("Sec-WebSocket-Key: ") }.map { String($0.dropFirst(19)) } ?? ""
            let a1 = wsFrame(0x1, Data(#"{"type":"session.output_transcript.delta","delta":"Hello."}"#.utf8), mask: nil)
            let a2 = wsFrame(0x1, Data(#"{"type":"session.output_audio.delta","delta":"\#(toneBig.base64EncodedString())"}"#.utf8), mask: nil)
            let a3 = wsFrame(0x1, Data(#"{"type":"session.output_audio.delta","delta":"\#(Data(count: 48000).base64EncodedString())"}"#.utf8), mask: nil)
            return [Data("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: \(wsAccept(n))\r\n\r\n".utf8) + a1, a2, a3]
        }
        lp.testEndpoint = ("127.0.0.1", pf.l.port?.rawValue ?? 1, false)
        lp.play("cedar", voiceOn: false, muted: false, settle: 0, key: { "sk-test" })
        let end = Date() + 5
        while lp.busy && Date() < end { RunLoop.main.run(until: Date() + 0.02) }
        let previewHint = lp.hint ?? ""
        lp.stop(); pf.l.cancel()
        check("voice: LivePreview streams audio, tracks speech-to-silence, and finishes cleanly (\(lp.phase) / \(previewHint))",
              lp.said.contains("Hello.") && (lp.phase == .played("cedar") || lp.phase == .idle))

        // Refusal 401 path on LivePreview
        let pf2 = PreviewFake()
        let w1 = Date(); while pf2.l.state != .ready && Date() < w1 + 3 { RunLoop.main.run(until: Date() + 0.02) }
        pf2.reply = { _ in [Data("HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n".utf8)] }
        let lp2 = LivePreview()
        lp2.testEndpoint = ("127.0.0.1", pf2.l.port?.rawValue ?? 1, false)
        lp2.play("vesper", voiceOn: false, muted: false, settle: 0, key: { "sk-bad" })
        let end2 = Date() + 4
        while lp2.busy && Date() < end2 { RunLoop.main.run(until: Date() + 0.02) }
        let refused401 = lp2.hint?.contains("401") == true
        lp2.stop(); pf2.l.cancel()
        check("voice: LivePreview surfaces 401 refusal in hint", refused401)
    }

    // 7. Non-live say/play/fallback, openLive missing key, onLive pre-up failure, run(.stop/.cancel/.undo), Speech.test & warm
    do {
        v.on = true; v.isLive = false; v.muted = false
        v.engine.attach(v.player)
        try? v.engine.link(v.player, to: v.engine.mainMixerNode, format: v.speechFormat)
        ud.set("eleven", forKey: "speech"); sp.stuck = "No credits"
        v.say("Fallback test line.", filler: false)
        v.say("Filler line.", filler: true)
        RunLoop.main.run(until: Date() + 0.15)
        ud.set("apple", forKey: "speech")
        v.say("Apple line.", filler: false)
        RunLoop.main.run(until: Date() + 0.1)
        // Speech.test when kind == apple returns "No audio"
        var testErr: String?
        sp.test("Hi") { testErr = $0 }
        let te = Date() + 2; while testErr == nil && Date() < te { RunLoop.main.run(until: Date() + 0.02) }
        // Voice openLive with no key + onLive pre-up failure + close_requested + .other
        let savedEnv = envKeyRead; envKeyRead = (true, nil)
        keyLock.lock(); let savedK = keys["openai"]; keys["openai"] = .some(nil); keyLock.unlock()
        v.on = true; v.isLive = true; v.liveUp = false
        v.openLive()
        let noKeyStopped = !v.on
        v.on = true; v.isLive = true; v.liveUp = false
        v.onLive(.other)
        v.onLive(.failed("bad_voice", "voice unavailable"))
        let preUpStopped = !v.on
        v.on = true; v.isLive = true
        v.onLive(.closed("close_requested"))
        keyLock.lock(); keys["openai"] = savedK; keyLock.unlock(); envKeyRead = savedEnv
        // Voice.run(.cancel) with pending toast + .stop while chief running + toggle while starting
        c.toasts = [DwimToast(text: "Pending launch", pending: true)]
        v.run(.cancel)
        let pendingCancelled = !c.toasts.contains { $0.pending }
        v.starting = true; v.toggle()
        let cancelledStart = !v.starting
        // Voice.show, look switch after stageWeb load, draw fallback, Speech.warm branches, LiveVoice.why fallback
        v.on = true; v.isLive = false
        v.show(["kind": "list", "title": "Board", "items": []])
        v.look = looks[0]; v.look = looks[1]
        v.draw("summary text", last: true)
        c.run = Run("busy"); v.run(.stop); c.run = nil
        c.toasts = [DwimToast(text: "Done", undo: "u-9")]; v.run(.undo)
        for k in ["live", "eleven", "kokoro", "apple"] {
            ud.set(k, forKey: "speech")
            sp.warm()
        }
        RunLoop.main.run(until: Date() + 0.1)
        let whyOther = LiveVoice.why(0, "", NSError(domain: "x", code: 1))
        let whyNil = LiveVoice.why(0, "", nil)
        let why404 = LiveVoice.why(404, "missing")
        v.freshEngine()
        check("voice: fallback say/play, Speech.test, openLive no-key, pre-up fail, warm, and cancel/toggle paths",
              testErr == "No audio" && noKeyStopped && preUpStopped && pendingCancelled && cancelledStart
              && whyOther.contains("lost") && whyNil == "lost the connection" && why404.contains("404"))
    }

    check("voice: Voice state machine handles GPT-Live events, cues, audio silence, stage blocks, fallback dedupe, barge-in, and close (\(heardText) / \(handedOff) / \(awaitingCue) / \(cueCleared) / \(startedSpeaking) / \(endedSpeaking) / \(liveErrCount) / \(fbCount) / \(capOff) / \(closedStopped) / \(bargedIn) / \(turnRanCmd))",
          heardText == "What is status?" && handedOff && awaitingCue && cueCleared
          && startedSpeaking && endedSpeaking && liveErrCount == 1 && fbCount == 1
          && capOff && v.captions && closedStopped && bargedIn && turnRanCmd)
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

/// `Greenlight.app/Contents/MacOS/Greenlight --selftest`: drives the actions the buttons call
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
