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
    var mode = "insert" { didSet { if mode != oldValue { pending = ""; onMode(mode) } } }
    var onMode: (String) -> Void = { _ in }
    var pending = ""
    var reg = ""
    var anchor = 0, cur = 0  // visual mode's fixed end and moving end

    /// One key ("\u{1b}" is Esc, "^r" is ⌃R). Returns true when vim used it, so it never reaches the field.
    func key(_ k: String, _ b: VimBuffer) -> Bool {
        let s = b.vimText, n = s.length
        if mode == "visual" {
            if n == 0 {
                mode = "normal"; anchor = 0; cur = 0; b.vimSel = NSRange(location: 0, length: 0)
                return k == "\u{1b}" || k == "v"
            }
            anchor = min(max(anchor, 0), n - 1)
            cur = min(max(cur, 0), n - 1)
        }
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
                b.vimSel = NSRange(location: clamp(b.vimText, p + 1), length: 0)
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
        let n = b.vimText.length
        guard n > 0 else { b.vimSel = NSRange(location: 0, length: 0); return }
        let a = min(max(anchor, 0), n - 1), c = min(max(cur, 0), n - 1), lo = min(a, c)
        b.vimSel = NSRange(location: lo, length: min(max(a, c) + 1, n) - lo)
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

