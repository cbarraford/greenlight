import Foundation

// MARK: vim tests

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
