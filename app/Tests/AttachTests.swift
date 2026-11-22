import AppKit
import Foundation
import SwiftUI
import WebKit

// MARK: attachment tests

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
