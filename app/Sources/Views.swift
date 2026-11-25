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

/// The Undo toast, over the chat's composer and the voice stage, in the look it sits in.
struct DwimToastBar: View {
    @ObservedObject var c: Core
    let l: Look
    var body: some View {
        if !c.toasts.isEmpty {
            ScrollView(.vertical, showsIndicators: false) {
                VStack(spacing: DS.Space.s4) { ForEach(c.toasts) { t in row(t) } }
            }.frame(maxHeight: 160)
        }
    }
    private func row(_ t: DwimToast) -> some View {
        let r = l.id == "phosphor" ? 0 : DS.Radius.md
        return HStack(spacing: DS.Space.s8) {
                if t.pending { ProgressView().controlSize(.mini) }
                else { Image(ds: t.failed ? "needs" : "done", size: iconSize(DS.TypeRole.label.size, scaled: false))
                    .foregroundColor(Color(t.failed ? DS.Palette.statusNeedsFg : DS.Palette.statusDoneFg, dark: l.dark)) }
                Text(t.pending ? "\(t.text) in \(Int(Core.launchGrace)) s" : t.text).font(ds(DS.TypeRole.label)).foregroundColor(hx(l.text))
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if t.undoable { action(t) }
            }.padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s8).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: r).fill(hx(l.card))).overlay(RoundedRectangle(cornerRadius: r).stroke(hx(l.line)))
                .accessibilityElement(children: .contain).accessibilityLabel(t.text)
                .transition(.move(edge: .bottom).combined(with: .opacity))
    }
    @ViewBuilder private func action(_ t: DwimToast) -> some View {
        let button = Button(t.undoing ? "Undoing…" : t.pending ? "Cancel" : "Undo") { c.undoToast(t.id) }.buttonStyle(.plain)
            .font(ds(DS.TypeRole.label)).foregroundColor(hx(l.link)).disabled(t.undoing)
            .help(t.pending ? "Don't start it" : "Put it back (⇧⌘Z)")
        if c.latestUndoableToast?.id == t.id { button.keyboardShortcut("z", modifiers: [.command, .shift]) }
        else { button }
    }
}

// MARK: views

/// One step of a run: its status glyph (§9.4: done, working, crashed), title, the command it ran, and its time.
/// Phosphor prints the state in brackets instead.
struct StepRow: View, Looked {
    let s: Step
    let now: Date
    let current: Bool
    var n = 0  // 1-based; unused by the two looks, kept for the callers
    var body: some View {
        let st = s.ok == true ? 0 : s.ok == nil ? 1 : 2  // done, running, failed
        HStack(alignment: .firstTextBaseline, spacing: DS.Space.s8) {
            Group {
                if mono { Text(["[ok]", "[..]", "[!!]"][st]).font(ui(DS.TypeRole.body, st == 1 ? .bold : .regular)).foregroundColor(st == 2 ? crashedInk : fg) }
                else { Image(ds: ["done", "working", "crashed"][st], size: iconSize(DS.TypeRole.body.size)).foregroundColor([dim, working, crashedInk][st]) }
            }.frame(minWidth: mono ? 32 : DS.Space.s16, alignment: .leading)
                .accessibilityLabel(s.ok == true ? "Done" : s.ok == false ? "Failed" : "Running")
            VStack(alignment: .leading, spacing: DS.Space.s2) {
                Text(s.title).font(ui(DS.TypeRole.body, mono && current ? .bold : nil)).foregroundColor(current ? fg : sub)
                if !s.detail.isEmpty {
                    Text(s.detail).font(ds(DS.TypeRole.code, mono: true)).foregroundColor(sub)
                        .lineLimit(2).padding(.horizontal, DS.Space.s4).padding(.vertical, DS.Space.s2)
                        .background(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.xs).fill(card))
                }
                if !s.out.isEmpty { Text(s.out).font(ui(DS.TypeRole.caption)).foregroundColor(dim).lineLimit(2) }
            }
            Spacer(minLength: DS.Space.s4)
            Text(fmt((s.end ?? now).timeIntervalSince(s.start))).font(ui(DS.TypeRole.caption).monospacedDigit()).foregroundColor(current ? fg : dim)
        }
        .padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s4)
        .textSelection(.enabled)
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
            HStack(spacing: DS.Space.s8) {
                Button { f.open.toggle() } label: {
                    HStack(spacing: DS.Space.s4) {
                        Image(ds: f.open ? "chevron-down" : "chevron-right", size: iconSize(9), medium: true).frame(width: 10)
                        Image(ds: live ? "working" : "queued", size: iconSize(DS.TypeRole.caption.size)).foregroundColor(live ? working : faint).accessibilityHidden(true)
                        Text("Live log").font(ui(DS.TypeRole.labelSm))
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).foregroundColor(sub).accessibilityLabel(f.open ? "Fold the live log" : "Open the live log")
                if f.open {
                    ForEach([nil] + FeedLine.Src.allCases.map { Optional($0) }, id: \.self) { s in
                        Button { f.only = s } label: {
                            Text(s?.rawValue ?? "All").font(ui(DS.TypeRole.caption, f.only == s ? .semibold : .regular))
                                .foregroundColor(f.only == s ? fg : dim).padding(.horizontal, DS.Space.s8).padding(.vertical, DS.Space.s2)
                                .background(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.xs).fill(f.only == s ? card : .clear))
                        }.buttonStyle(.plain).help(s == nil ? "Everything" : s == .chief ? "\(c.name)'s own turn: calls, their output, thinking" : s == .agents ? "Your workspaces' agents" : "Greenlight's moves and the dashboard's buttons")
                    }
                    Spacer(minLength: 0)
                    Button { f.paused.toggle() } label: { Image(ds: f.paused ? "play" : "pause", size: iconSize(9)) }
                        .buttonStyle(.plain).foregroundColor(dim).help(f.paused ? "Follow the newest line" : "Stop following, to read").accessibilityLabel(f.paused ? "Follow" : "Pause following")
                } else if let l = lines.last {
                    row(l, limit: 1).frame(maxWidth: .infinity, alignment: .leading)
                } else { Spacer(minLength: 0) }
                Button("Clear") { f.clear() }.buttonStyle(.plain).font(ui(DS.TypeRole.caption)).foregroundColor(dim).disabled(f.lines.isEmpty)
            }.padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s8)
            if f.open {
                ScrollViewReader { sv in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: DS.Space.s4) {
                            if lines.isEmpty {
                                Text(f.only == nil ? "Nothing yet. \(c.name)'s calls, your agents and Greenlight's moves show here as they happen." : "Nothing from \(f.only!.rawValue) yet.")
                                    .font(ui(DS.TypeRole.caption)).foregroundColor(dim)
                            }
                            ForEach(lines) { l in
                                row(l, limit: f.unfolded.contains(l.id) ? nil : l.kind == .think ? 3 : 6)
                                    .onTapGesture { if f.unfolded.contains(l.id) { f.unfolded.remove(l.id) } else { f.unfolded.insert(l.id) } }
                            }
                            Color.clear.frame(height: 1).id("end")
                        }.padding(.horizontal, DS.Space.s12).padding(.bottom, DS.Space.s8)
                    }.frame(height: 190)
                        .onAppear { sv.scrollTo("end", anchor: .bottom) }
                        .onChange(of: lines.last?.id) { if !f.paused { sv.scrollTo("end", anchor: .bottom) } }
                        .onChange(of: lines.last?.text.count) { if !f.paused { sv.scrollTo("end", anchor: .bottom) } }  // thinking grows in place
                }
            }
        }.background(mono ? Color.clear : Color(DS.Palette.bgSunken, dark: lk.dark)).textSelection(.enabled)
    }
    func row(_ l: FeedLine, limit: Int?) -> some View {
        let (m, ink) = feedMark(l.kind, accent: link, dim: dim, failed: crashedInk, agent: working, move: sub)
        let who = l.src == .chief ? fg : l.src == .agents ? working : sub
        return HStack(alignment: .firstTextBaseline, spacing: DS.Space.s8) {
            Text(feedClock.string(from: l.ts)).foregroundColor(faint)
            Text(m).foregroundColor(ink).frame(width: 12, alignment: .center)
            Text("\(Text(l.who).foregroundColor(who).bold()) \(Text(l.text).foregroundColor(l.kind == .think || l.kind == .status ? dim : l.kind == .failed ? crashedInk : fg).italic(l.kind == .think))")
                .lineLimit(limit).truncationMode(.tail).fixedSize(horizontal: false, vertical: true)
        }.font(ds(DS.TypeRole.caption, mono: true).monospacedDigit()).accessibilityElement(children: .combine)
    }
}

/// Verbose activity in a voice look: the log's newest lines under the live line, one line each, in the look's colors.
struct FeedTail: View {
    @ObservedObject var f: Feed
    let n: Int, fg: Color, dim: Color, accent: Color, size: CGFloat
    var body: some View {
        VStack(alignment: .leading, spacing: DS.Space.s2) {
            if f.lines.isEmpty { Text("Live log: waiting for activity").foregroundColor(dim) }
            ForEach(f.lines.suffix(n)) { l in
                let (m, ink) = feedMark(l.kind, accent: accent, dim: dim, failed: Color(DS.Palette.statusCrashedFg, dark: isDark), agent: accent, move: accent)
                Text("\(Text(m).foregroundColor(ink)) \(Text(l.who).foregroundColor(fg).bold()) \(Text(l.text.split(separator: "\n").first.map(String.init) ?? "").foregroundColor(l.kind == .think || l.kind == .status ? dim : fg))")
                    .lineLimit(1).truncationMode(.tail)
            }
        }.font(sys(size: size, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).accessibilityElement(children: .combine)
    }
}

/// The run in progress: who's working, a status line with Stop, the progress track, the steps so far, and the reply as it streams.
/// Default (§10.17): a hairline card, quiet type, the working glyph and colour; Phosphor: a dashed box, brackets and a hash bar.
struct RunCard: View, Looked {
    @ObservedObject var r: Run
    @ObservedObject var c: Core
    var body: some View {
        VStack(alignment: .leading, spacing: DS.Space.s8) {
            label
            framed(VStack(alignment: .leading, spacing: DS.Space.s12) {
                head
                bar
                if !r.steps.isEmpty {
                    VStack(spacing: 0) {
                        ForEach(Array(r.steps.enumerated()), id: \.element.id) { i, s in
                            StepRow(s: s, now: c.now, current: i == r.steps.count - 1 && s.ok == nil, n: i + 1)
                        }
                    }.padding(.horizontal, -DS.Space.s12)  // the rows carry their own inset (for MsgView's list); line them up with the head
                        .padding(.top, mono ? DS.Space.s8 : 0)
                        .overlay(alignment: .top) { if mono { HLine().stroke(line, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1) } }
                }
            })
            if !r.live.isEmpty { liveText }
            if c.liveLine { activity }
        }
    }
    var activity: some View { Activity(c: c, r: r, fg: fg, dim: dim, accent: mono ? fg : working, size: DS.TypeRole.label.size) }
    var state: String { r.stopping ? "stopping" : "working" }
    /// "Name · working" above the card, with the working glyph (Phosphor: a prompt and [working]).
    @ViewBuilder var label: some View {
        if mono { Text("\(Text("\(c.name.lowercased())@greenlight ").foregroundColor(dim))[\(state)]").font(ui(DS.TypeRole.body, .bold)).foregroundColor(fg) }
        else {
            HStack(spacing: DS.Space.s4) {
                Image(ds: "working", size: iconSize(DS.TypeRole.label.size)).foregroundColor(working).accessibilityHidden(true)
                Text("\(Text(c.name).foregroundColor(fg)) · \(state.capitalized)")
            }.font(ds(DS.TypeRole.label)).foregroundColor(dim)
        }
    }
    /// The status line: the status and stats, Stop.
    var head: some View {
        HStack(spacing: DS.Space.s12) {
            VStack(alignment: .leading, spacing: DS.Space.s2) {
                Text(mono ? "> \(r.status.lowercased())" : r.status).lineLimit(1).foregroundColor(fg).font(ui(DS.TypeRole.strong, mono ? .bold : nil))
                Text(stats).font(ui(DS.TypeRole.caption).monospacedDigit()).foregroundColor(dim).lineLimit(2)
            }
            Spacer(minLength: DS.Space.s4)
            stop
        }
    }
    /// Stop: a secondary button (§10.2), or Phosphor's [ stop ].
    var stop: some View {
        Button(action: c.stop) {
            Text(mono ? "[ stop ]" : "Stop").font(ui(DS.TypeRole.label)).foregroundColor(fg)
                .padding(.horizontal, DS.Space.s8).frame(height: DS.Layout.controlHeightSm)
                .background(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.sm).fill(mono ? Color.clear : card))
                .overlay(Rectangle().stroke(mono ? fg : .clear))
                .contentShape(Rectangle())
        }.buttonStyle(.plain).disabled(r.stopping).opacity(r.stopping ? 0.5 : 1).accessibilityLabel("Stop").help("Stop this run")
    }
    /// ponytail: no percentage is known, so progress stays indeterminate: the native linear bar in the working colour, or for
    /// Phosphor a 3-cell window stepping along once a second off c.now (no extra timer).
    func lit(_ i: Int) -> Bool { (i - Int(c.now.timeIntervalSince(r.start)) % 10 + 10) % 10 < 3 }
    @ViewBuilder var bar: some View {
        if mono { Text("[" + String((0..<10).map { lit($0) ? "#" : "." }) + "]").font(ui(DS.TypeRole.body)).foregroundColor(fg).accessibilityHidden(true) }
        else { ProgressView().progressViewStyle(.linear).tint(working).frame(height: DS.Space.s4).clipped().accessibilityLabel("Working") }
    }
    /// The card around head, bar and steps: a hairline card (radius md, 12 in), or Phosphor's dashed box.
    func framed<V: View>(_ v: V) -> some View {
        v.padding(DS.Space.s12).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.md).fill(mono ? Color.clear : bg))
            .overlay(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.md).stroke(line, style: StrokeStyle(lineWidth: 1, dash: mono ? [4, 3] : [])))
    }
    /// The reply as it streams, with the look's cursor.
    var liveText: some View {
        let cur = Text(mono ? " █" : " ▍").foregroundColor(mono ? fg : link)
        let rest = unmood(staged(r.live).shown)
        // the size and frame MsgView's reply has, so the text doesn't jump when the run lands
        return Text("\(mono ? Text("\(c.name.lowercased())> ").foregroundColor(dim) : Text(""))\(Text(md(rest)))\(cur)")
            .font(chatFace(size: DS.TypeRole.bodyCopy.size)).lineSpacing(DS.TypeRole.bodyCopy.line - DS.TypeRole.bodyCopy.size - 3).textSelection(.enabled).foregroundColor(fg)
    }
    var stats: String {
        let n = r.steps.count, t = max(0, Int(c.now.timeIntervalSince(r.start))), tps = c.tps > 0 ? " · \(Int(c.tps)) tok/s" : ""
        let b = byline(r, now: c.now, lead: false), by = b.isEmpty ? "" : " · " + b  // the plan, time left, what just finished
        // the byline before time and speed where the line may wrap (the card is narrow, and those two are on the live line under it too)
        let s = (n == 0 ? "No steps yet" : "Step \(n)") + by + " · \(fmt(TimeInterval(t)))\(tps)"
        return mono ? s.lowercased() : s
    }
}

/// One message: yours in a bubble at the right; Greenlight's full width with no bubble, then how its run went and any choices
/// it ends on (§10.17). Phosphor prints both as a shell session.
struct MsgView: View, Looked {
    @ObservedObject var m: Msg
    let c: Core
    let name: String  // passed in: MsgView doesn't observe Greenlight, which changes every second while working
    var ask: Question? = nil  // the inbox question this message asked, if any: open, or answered with which choice
    var body: some View {
        switch m.role {
        case .you: you
        case .note:
            Text(m.text).font(ui(DS.TypeRole.caption)).foregroundColor(dim).textSelection(.enabled).frame(maxWidth: .infinity)
        case .greenlight:
            let open = !m.options.isEmpty && ask != nil && ask?.answer == nil
            let (said, q) = open ? asked : (m.text, nil)
            VStack(alignment: .leading, spacing: DS.Space.s12) {
                turn(said)  // the reply, then how the run went: above any choices the reply ends on
                if !m.options.isEmpty { choices(open, q) }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Prose: body-copy (13/19) in the chat font, or Phosphor's mono.
    var prose: Font { chatFace(size: DS.TypeRole.bodyCopy.size) }
    var leading: CGFloat { DS.TypeRole.bodyCopy.line - DS.TypeRole.bodyCopy.size - 3 }  // SF's own line gap is about 3pt at 13

    @ViewBuilder var you: some View {
        if mono {
            Text("\(Text("you> ").foregroundColor(dim))\(Text(m.text).foregroundColor(youInk))").font(prose).lineSpacing(leading).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        } else {
            HStack {
                Spacer(minLength: DS.Space.s48)
                Text(m.text).font(prose).lineSpacing(leading).foregroundColor(fg).textSelection(.enabled)
                    .padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s8).background(RoundedRectangle(cornerRadius: DS.Radius.md).fill(bubble))
            }
        }
    }

    /// What was said and how the run went. Phosphor leads with the name's prompt.
    @ViewBuilder func turn(_ said: String) -> some View {
        let col = VStack(alignment: .leading, spacing: DS.Space.s8) {
            if !said.isEmpty { MDText(text: unmood(said)).font(prose).lineSpacing(leading).textSelection(.enabled).foregroundColor(m.failed ? crashedInk : fg) }
            work
        }
        if mono { HStack(alignment: .firstTextBaseline, spacing: 0) { Text("\(name.lowercased())> ").font(prose).foregroundColor(dim); col } } else { col }
    }

    /// The choices a reply ends on. Open: a needs banner (§10.21: tint, glyph, the question) with the choices as buttons, the
    /// first primary. Answered: a hairline box with the choice made.
    func choices(_ open: Bool, _ q: String?) -> some View {
        let chips = ForEach(m.options, id: \.self) { o in
            Button { c.choose(m, o) } label: { chip(o, open) }.buttonStyle(.plain).disabled(c.run != nil).accessibilityLabel(o)
        }
        return VStack(alignment: .leading, spacing: DS.Space.s12) {
            if open {
                needsTag
                if let q { question(q) }  // the question sits with its answers
            }
            if !open, let a = ask?.answer {  // answered: the choice made, the others folded away
                HStack(spacing: DS.Space.s8) {
                    Image(ds: a == "(dismissed)" ? "close" : "done", size: iconSize(DS.TypeRole.label.size)).foregroundColor(dim)
                    Text(a == "(dismissed)" ? "Dismissed without an answer" : "You answered")
                    if a != "(dismissed)" { Text(a).fontWeight(.semibold).foregroundColor(fg).lineLimit(2) }
                }.font(ui(DS.TypeRole.label)).foregroundColor(dim)
            } else {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: DS.Space.s8) { chips }
                    VStack(alignment: .leading, spacing: DS.Space.s8) { chips }
                }
            }
        }.padding(open ? DS.Space.s12 : DS.Space.s8).frame(maxWidth: .infinity, alignment: .leading).background(box(open))
    }
    @ViewBuilder func box(_ open: Bool) -> some View {
        let r = RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.md)
        if mono { r.fill(open ? card : .clear).overlay(r.strokeBorder(open ? fg : line, style: StrokeStyle(lineWidth: 1, dash: [4, 3]))) }
        else if open { r.fill(state(DS.Palette.statusNeedsBg)).overlay(r.strokeBorder(state(DS.Palette.statusNeedsBorder))) }
        else { r.strokeBorder(line) }
    }
    @ViewBuilder var needsTag: some View {
        if mono { Text("-- needs your answer --").font(chatFace(size: DS.TypeRole.body.size, weight: .bold)).foregroundColor(fg) }
        else {
            Label { Text("Needs your answer") } icon: { Image(ds: "needs", size: iconSize(DS.TypeRole.label.size)) }.font(ds(DS.TypeRole.label)).foregroundColor(needsInk)
        }
    }
    @ViewBuilder func question(_ q: String) -> some View {
        let t = MDText(text: q).font(chatFace(size: DS.TypeRole.bodyCopy.size, weight: mono ? .regular : .semibold)).lineSpacing(leading).textSelection(.enabled).foregroundColor(fg)
        if mono { HStack(alignment: .firstTextBaseline, spacing: 0) { Text("? ").font(prose).foregroundColor(dim); t } } else { t }
    }
    /// One choice; the first is the primary while the question's open (§10.2: one primary per view), the rest secondary.
    func chip(_ o: String, _ open: Bool) -> some View {
        // while a run is going the choices wait (disabled, §10.2): element.rest and fg.faint, no accent
        let off = c.run != nil, p = open && o == m.options.first && !off, n = (m.options.firstIndex(of: o) ?? 0) + 1
        let ink = off ? faint : mono ? (p ? page : fg) : p ? onAccent : fg, fill = mono ? (p ? fg : Color.clear) : p ? accent : off ? card : open ? bg : card
        let r = RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.sm)
        return Text(mono ? "[\(n)] \(o)" : o).font(ui(DS.TypeRole.label)).foregroundColor(ink)
            .padding(.horizontal, DS.Space.s12).frame(minHeight: DS.Layout.controlHeightMd)
            .background(r.fill(fill)).overlay(r.strokeBorder(mono && !p ? (off ? faint : dim) : .clear))
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
            Button { m.expanded.toggle() } label: { worked.contentShape(Rectangle()) }.buttonStyle(.plain)
            if m.expanded && !m.steps.isEmpty {
                VStack(spacing: 0) { ForEach(Array(m.steps.enumerated()), id: \.element.id) { i, s in StepRow(s: s, now: Date(), current: false, n: i + 1) } }
                    .padding(.vertical, DS.Space.s8).background(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.md).fill(card))
            }
        }
    }
    /// "Worked 1m 2s · 6 steps": a quiet line with the done or crashed glyph and a chevron; Phosphor's [ok] … [+].
    @ViewBuilder var worked: some View {
        if mono {
            Text("[\(m.failed ? "err" : "ok")] \(summary.lowercased()) [\(m.expanded ? "-" : "+")]").font(ui(DS.TypeRole.body)).foregroundColor(m.failed ? crashedInk : dim)
        } else {
            HStack(spacing: DS.Space.s4) {
                Image(ds: m.failed ? "crashed" : "done", size: iconSize(DS.TypeRole.caption.size)).foregroundColor(m.failed ? crashedInk : dim)
                Text(summary).monospacedDigit()
                Image(ds: m.expanded ? "chevron-down" : "chevron-right", size: iconSize(9), medium: true)
            }.font(ds(DS.TypeRole.caption)).foregroundColor(dim).frame(minHeight: DS.Layout.hitTargetMin)
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
        let bs = blocks
        VStack(alignment: .leading, spacing: DS.Space.s8) {
            ForEach(Array(bs.enumerated()), id: \.offset) { i, b in
                if let mark = b.mark {
                    HStack(alignment: .firstTextBaseline, spacing: DS.Space.s8) {
                        bullet(mark, nth(bs, i)).monospacedDigit().frame(minWidth: 12, alignment: .trailing)
                        Text(md(b.text)).fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, CGFloat(b.indent) * DS.Space.s16).padding(.top, b.gap ? 0 : -DS.Space.s4)  // items 4pt apart, paragraphs 8
                } else {
                    Text(md(b.text)).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
    /// The list mark: the item's own (a number, or a muted dot), Phosphor's `*`.
    func bullet(_ mark: String, _ n: Int) -> Text {
        Text(mono && mark == "•" ? "*" : mark).foregroundColor(dim)
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

/// Instant hover hint by the cursor (App.showTab), in place of the native tooltip: that one waits ~1 s, shows only while
/// Greenlight is the front app, and would double this one. VoiceOver reads the text as the control's hint.
/// A click that changes what the control does (captions on, the next style) updates the hint in place; one that removes the
/// control (voice mode off) drops it, as its hover-exit never comes.
struct Tip: ViewModifier {
    @ObservedObject var c: Core
    let text: String
    func body(content: Content) -> some View {
        content.accessibilityHint(text).onHover { on in  // writes only what changes: App.hint takes each write as this window speaking
            guard !c.prompting else { return }
            if on { c.hint = text } else if c.hint == text { c.hint = "" }
        }
            .onChange(of: text) { old, new in if c.hint == old { c.hint = new } }
            .onDisappear { if c.hint == text { c.hint = "" } }
    }
}

/// The bell's panel, a triage list: one line per waiting question, the open one (Core.inboxFocus) expanded with its choices,
/// then the last few answered, and a footer of keys and Clear all.
struct Inbox: View {
    @ObservedObject var c: Core
    var body: some View {
        let w = c.waiting, open = c.inboxFocus?.id
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: DS.Space.s8) {
                Text("Inbox").font(sys(size: DS.TypeRole.label.size, weight: .medium)).foregroundColor(dim)
                if !w.isEmpty {
                    Text("\(w.count)").font(sys(size: DS.TypeRole.caption.size, weight: .bold)).foregroundColor(.black.opacity(0.8))
                        .padding(.horizontal, 6).frame(minWidth: 18, minHeight: 16).background(Capsule().fill(StatusInk.needs))
                        .accessibilityLabel("\(w.count) waiting")
                }
                Spacer()  // no close button: Esc, the bell or a click outside closes it, as a popover
            }.padding(.horizontal, DS.Space.s16).padding(.top, DS.Space.s12).padding(.bottom, DS.Space.s4)
            if w.isEmpty {
                HStack(alignment: .top, spacing: DS.Space.s12) {
                    Image(ds: "done", size: iconSize(DS.TypeRole.title.size)).foregroundColor(dim)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("No questions waiting").fontWeight(.semibold)
                        Text("The bell lights up when \(c.name) needs a decision.").font(sys(size: DS.TypeRole.label.size)).foregroundColor(dim)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, DS.Space.s16).padding(.top, DS.Space.s8).padding(.bottom, DS.Space.s16)
            } else {
                let rows = VStack(spacing: 0) {
                    ForEach(w) { q in if q.id == open { QuestionRow(c: c, q: q) } else { InboxLine(c: c, q: q) } }
                }.padding(.vertical, DS.Space.s4)
                ViewThatFits(in: .vertical) { rows; ScrollView { rows } }
            }
            // the last few answered, and every answer greenlight gave for you in the last day (each with its Undo)
            let answered = c.questions.filter { $0.answer != nil }, last = Set(answered.suffix(3).map(\.id))
            let done = answered.filter { last.contains($0.id) || ($0.auto != nil && $0.ts > Date().addingTimeInterval(-86400)) }.reversed()
            if !done.isEmpty {
                Divider().overlay(line)
                VStack(alignment: .leading, spacing: DS.Space.s8) {
                    Text("ANSWERED").font(sys(size: DS.TypeRole.caption.size, weight: .semibold)).foregroundColor(dim)
                    ForEach(Array(done)) { q in
                        HStack(spacing: DS.Space.s8) {
                            let dismissed = q.answer == "(dismissed)"
                            Image(ds: q.auto != nil ? "sparkles" : dismissed ? "close" : "check", size: iconSize(DS.TypeRole.caption.size), medium: true)
                                .foregroundColor(q.auto != nil ? accent : dismissed ? dim : StatusInk.done).frame(width: 12)
                            Text(q.title(c.name)).foregroundColor(sub).lineLimit(1).frame(width: 92, alignment: .leading)
                            Text(dismissed ? "Dismissed" : q.answer ?? "").foregroundColor(dim).lineLimit(1).help(q.text)
                            Spacer(minLength: DS.Space.s8)
                            if q.auto != nil {
                                Button("Ask again") { c.undoAuto(q) }.buttonStyle(.plain).foregroundColor(accent)
                                    .help("Greenlight will ask again. This does not reverse an action already taken.")
                            }
                        }.font(sys(size: DS.TypeRole.label.size))
                    }
                }.padding(.horizontal, DS.Space.s16).padding(.vertical, DS.Space.s12)
            }
            if !w.isEmpty {
                Divider().overlay(line)
                HStack(spacing: DS.Space.s8) {
                    if c.inboxConfirm {
                        Text("Dismiss all \(w.count) without answering?").foregroundColor(sub)
                        Spacer(minLength: DS.Space.s8)
                        Button("Cancel") { c.inboxConfirm = false }.buttonStyle(.plain).foregroundColor(dim).help("Esc")
                        Button("Clear all", action: c.clearInbox).buttonStyle(.plain).fontWeight(.semibold).foregroundColor(fg)
                            .padding(.horizontal, DS.Space.s12).frame(minHeight: 26).background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(surface))
                            .overlay(RoundedRectangle(cornerRadius: DS.Radius.sm).stroke(edge)).help("Return")
                    } else {
                        Group { kbd("↑↓"); Text("move"); kbd("⌫"); Text("dismiss"); kbd("esc"); Text("close") }.foregroundColor(dim)
                        Spacer(minLength: DS.Space.s8)
                        Button { c.inboxConfirm = true } label: { HStack(spacing: 6) { Text("Clear all"); kbd("⇧⌫") } }.buttonStyle(.plain).foregroundColor(sub)
                            .help("Dismiss every waiting question without answering. Nothing is sent.")
                    }
                }.font(sys(size: DS.TypeRole.caption.size)).padding(.horizontal, DS.Space.s16).frame(minHeight: 38)
            }
        }
        .background(RoundedRectangle(cornerRadius: DS.Radius.lg).fill(Color(DS.Palette.bgRaised, dark: isDark)))  // bg.raised: the lightest surface in dark (§10.13)
        .overlay(RoundedRectangle(cornerRadius: DS.Radius.lg).stroke(edge))
        .clipShape(RoundedRectangle(cornerRadius: DS.Radius.lg))
        .shadow(color: .black.opacity(isDark ? 0.45 : 0.10), radius: 12, y: 6)  // elevation 2: an overlay, no scrim (§10.13)
        .accessibilityElement(children: .contain).accessibilityLabel("Questions waiting for you")
    }
}

/// A key, as the inbox shows it: "1", "R", "⇧⌫".
/// §10.4: an input's fill. Dark fields sit above their card, never below the window.
var fieldFill: Color { Color(isDark ? DS.Palette.elementRest : DS.Palette.bgSurface, dark: isDark) }

/// §10.4 for a one-line field: its own surface and edge, and the accent ring while it has the keyboard.
struct DSField: TextFieldStyle {
    func _body(configuration: TextField<Self._Label>) -> some View { Box { configuration } }
    private struct Box<V: View>: View {
        @ViewBuilder let field: () -> V
        @FocusState private var on: Bool
        var body: some View {
            field().textFieldStyle(.plain).focused($on).padding(.horizontal, DS.Space.s8).frame(height: DS.Layout.controlHeightSm)
                .background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(fieldFill))
                .overlay(RoundedRectangle(cornerRadius: DS.Radius.sm).strokeBorder(on ? Color(accentSet.ring, dark: isDark) : edge, lineWidth: on ? 2 : 1))
        }
    }
}

func kbd(_ k: String) -> some View {
    Text(k).font(sys(size: 10, weight: .medium)).foregroundColor(dim).padding(.horizontal, 4).frame(minWidth: 16, minHeight: 16)
        .overlay(RoundedRectangle(cornerRadius: DS.Radius.xs).stroke(edge)).accessibilityHidden(true)
}

/// A waiting question, folded to a line: which workspace, what it asks, how long ago. Click opens it.
struct InboxLine: View {
    @ObservedObject var c: Core
    let q: Question
    @StateObject private var hover = Flag()  // not @State: see the top of the file
    var body: some View {
        Button { c.inboxPick = q.id } label: {
            HStack(spacing: DS.Space.s8) {
                Image(ds: "needs-fill", size: 12).foregroundColor(StatusInk.needs)
                Text(q.title(c.name)).font(sys(size: DS.TypeRole.label.size, weight: .semibold)).lineLimit(1).frame(width: 92, alignment: .leading)
                Text(q.label.1).font(sys(size: DS.TypeRole.label.size)).foregroundColor(sub).lineLimit(1)
                Spacer(minLength: DS.Space.s4)
                Text(ago(q.ts)).font(sys(size: DS.TypeRole.caption.size)).foregroundColor(dim)
            }.padding(.horizontal, DS.Space.s16).frame(height: 32).background(hover.on ? surface : .clear).contentShape(Rectangle())
        }.buttonStyle(.plain).onHover { hover.on = $0 }.accessibilityLabel("\(q.title(c.name)): \(q.label.1)").accessibilityHint("Opens the question")
    }
}

/// The open question: all of it, its answers (1–9), Other… (R), and where it came from, Open and Dismiss (⌫).
struct QuestionRow: View {
    @ObservedObject var c: Core
    let q: Question
    @FocusState private var typing: Bool
    var body: some View {
        let (mr, said) = q.label
        VStack(alignment: .leading, spacing: DS.Space.s8) {
            HStack(spacing: DS.Space.s8) {
                Image(ds: "needs-fill", size: 12).foregroundColor(StatusInk.needs)  // ping or chat, it waits on you
                Text(q.title(c.name)).font(sys(size: DS.TypeRole.label.size, weight: .semibold)).lineLimit(1)
                if let mr { Text(mr).font(sys(size: DS.TypeRole.label.size)).foregroundColor(sub) }
                Spacer(minLength: 0)
                Text(ago(q.ts)).font(sys(size: DS.TypeRole.caption.size)).foregroundColor(dim)
            }
            Text(md(said)).font(chatFace(size: DS.TypeRole.body.size)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            let chips = Group {  // equal weight, unless autonomy.py's past answers make one likely (tier 2): that one is preselected
                ForEach(Array(q.options.enumerated()), id: \.element) { i, o in
                    AnswerChip(title: o, key: i < 9 ? "\(i + 1)" : nil, likely: q.suggest?.choice == o) { c.answer(q, o) }
                }
                if c.replyingTo != q.id {
                    Button { c.replyText = ""; c.replyingTo = q.id } label: { HStack(spacing: 6) { kbd("R"); Text(q.options.isEmpty ? "Reply" : "Other…") } }
                        .buttonStyle(.plain).font(sys(size: DS.TypeRole.label.size))
                        .padding(.horizontal, DS.Space.s8).frame(minHeight: DS.Layout.controlHeightMd)
                        .background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(Color(DS.Palette.elementHover, dark: isDark))).contentShape(Rectangle())
                }
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: DS.Space.s8) { chips }
                VStack(alignment: .leading, spacing: DS.Space.s8) { chips }
            }
            if c.replyingTo == q.id {
                HStack(spacing: DS.Space.s8) {
                    TextField("Reply to \(c.name)…", text: $c.replyText).textFieldStyle(.plain).onSubmit(send).focused($typing)
                        .onAppear { DispatchQueue.main.async { typing = true } }
                    Button(action: send) {
                        Image(ds: "arrow-up", size: iconSize(DS.TypeRole.caption.size), medium: true).frame(width: 24, height: 24)
                            .background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(accent)).foregroundColor(bg)
                    }.buttonStyle(.plain).accessibilityLabel("Send reply")
                }.padding(.leading, DS.Space.s12).padding(.trailing, 2).padding(.vertical, 2)
                    .background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(bg)).overlay(RoundedRectangle(cornerRadius: DS.Radius.sm).stroke(accent))
            }
            HStack(spacing: DS.Space.s4) {
                if let s = q.suggest {
                    Label { Text(likelyNote(s)) } icon: { Image(ds: "sparkles") }.lineLimit(1)
                        .help("Greenlight's guess from your past answers to this sort of question. Nothing is sent until you choose.")
                    Text("·")
                }
                Text(q.ping ? "Ping" : "Asked in the chat")
                Spacer(minLength: DS.Space.s8)
                if let ws = q.about {
                    Button("Open") { c.openPage(ws) }.buttonStyle(.plain).foregroundColor(sub).help("Show \(ws) in the dashboard")
                }
                Button("Dismiss") { c.dismiss(q) }.buttonStyle(.plain).foregroundColor(sub).padding(.leading, DS.Space.s8)
                    .help("Dismiss without answering (⌫)")
            }.font(sys(size: DS.TypeRole.caption.size)).foregroundColor(dim)
        }.padding(.horizontal, DS.Space.s16).padding(.vertical, DS.Space.s8)  // a row in the popover, not a card inside it (§10.13)
    }
    func send() {
        let t = c.replyText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { c.answer(q, t) }
    }
}

extension Question {
    /// The workspace a ping is about (its key; "@…" keys are groups, so their one named workspace if any).
    var about: String? { key.flatMap { $0.hasPrefix("@") ? nil : $0 } ?? (ws?.count == 1 ? ws?.first : nil) }
    /// What the inbox calls it: its workspace(s), else where it came from.
    func title(_ name: String) -> String { about ?? ws.map { $0.joined(separator: ", ") } ?? (ping ? "\(name) ping" : "Your chat with \(name)") }
    /// The MR a ping names as "<workspace> (!N)", and its text without that when it's only a label ("<workspace> (!N): …").
    var label: (String?, String) {
        guard let ws = about, text.hasPrefix(ws + " (!") else { return (nil, text) }
        let from = text.index(text.startIndex, offsetBy: ws.count + 2)
        guard let close = text[from...].firstIndex(of: ")") else { return (nil, text) }
        let rest = text[text.index(after: close)...]
        return (String(text[from..<close]), rest.hasPrefix(": ") ? String(rest.dropFirst(2)) : text)
    }
}

/// One answer greenlight gave for you, in Settings › Autonomy: what, when, how sure, and reconsideration.
struct AutonomyActRow: View {
    let a: AutonomyReport.Act, title: String, fg: Color, dim: Color, undo: () -> Void
    var body: some View {
        let said = a.text.map { String(splitOptions($0).0.prefix(120)) } ?? ""
        HStack(alignment: .firstTextBaseline, spacing: DS.Space.s12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(a.choice) · \(title)").font(sys(size: DS.TypeRole.body.size, weight: .medium)).foregroundColor(fg)
                Text("\(ago(Date(timeIntervalSince1970: a.ts))) · \(Int((a.prob * 100).rounded()))% sure · \(said)")
                    .font(sys(size: DS.TypeRole.label.size)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: DS.Space.s12)
            if a.undone != nil { Text("Asked again").font(sys(size: DS.TypeRole.caption.size)).foregroundColor(dim) }
            else { Button("Ask me again", action: undo).help("Asks you again; it does not reverse an action already taken") }
        }.padding(.horizontal, DS.Space.s16).padding(.vertical, 10)
    }
}

/// The badge under a preselected chip: "Likely: keep · you picked it 6 of 7 times (86%)".
func likelyNote(_ s: Suggest) -> String {
    "Likely: \(s.choice) · you picked it \(s.agree) of \(s.count) time\(s.count == 1 ? "" : "s") (\(Int((s.prob * 100).rounded()))%)"
}

/// An inbox answer: a restrained amber outline, filled on hover; choosing sends it. likely: preselected (filled, a check).
struct AnswerChip: View {
    let title: String
    var key: String?  // its number key, shown as a badge
    var likely = false
    let action: () -> Void
    @StateObject private var hover = Flag()  // not @State: see the top of the file
    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if let key { kbd(key) }
                if likely { Image(ds: "check", size: iconSize(DS.TypeRole.caption.size), medium: true) }
                Text(title)
            }
        }.buttonStyle(.plain).font(sys(size: DS.TypeRole.label.size, weight: likely ? .semibold : .regular))
            .padding(.horizontal, DS.Space.s8).frame(minHeight: DS.Layout.controlHeightMd)  // a secondary button (§10.2): the suggested one says so with a check and weight
            .background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(Color(hover.on ? DS.Palette.elementActive : DS.Palette.elementHover, dark: isDark)))  // one step up: the popover is bg.raised, which is element.rest in dark
            .contentShape(Rectangle()).onHover { hover.on = $0 }
            .accessibilityLabel(likely ? "\(title), suggested" : title)
    }
}
final class Flag: ObservableObject { @Published var on = false }

/// The hover hint, by the cursor (App.showTab).
struct HintTab: View {
    let text: String
    var body: some View {
        let shape = RoundedRectangle(cornerRadius: DS.Radius.sm)
        HStack(spacing: DS.Space.s8) {
            Circle().fill(accent).frame(width: 6, height: 6)
            Text(text).font(sys(size: DS.TypeRole.caption.size)).foregroundColor(fg).lineLimit(3).fixedSize(horizontal: false, vertical: true)
        }.padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s4 + 2)
            .background(shape.fill(bg)).overlay(shape.stroke(edge))
            .preferredColorScheme(isDark ? .dark : .light)
    }
}
/// The hint's size: its one line, or wrapped at 480 pt (a long name), so it never runs off screen.
func hintSize(_ text: String) -> NSSize {
    let s = NSHostingController(rootView: HintTab(text: text)).sizeThatFits(in: NSSize(width: 480, height: 1000))
    return NSSize(width: ceil(s.width), height: ceil(s.height))
}
/// Where the hint goes: just below and right of the cursor (clear of the arrow), as a tooltip does; above it near the
/// screen's bottom (high enough to clear most of a control the cursor is in), and shifted left near its right edge, so it's never off screen.
func hintFrame(_ size: NSSize, mouse m: NSPoint, screen v: NSRect) -> NSRect {
    var r = NSRect(x: m.x + 10, y: m.y - 22 - size.height, width: size.width, height: size.height)
    if r.minY < v.minY + 4 { r.origin.y = m.y + 24 }
    r.origin.x = max(v.minX + 4, min(r.origin.x, v.maxX - size.width - 4))
    return r
}

/// Settings over the whole window (dashboard and chat), not just the chat pane.
/// The bell's inbox, over the dashboard under the top bar's bell: App.inboxHost.
struct InboxOverlay: View {
    @ObservedObject var c: Core
    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.clear.contentShape(Rectangle()).onTapGesture { c.inboxOpen = false }  // no scrim: the dashboard stays readable
            VStack(spacing: 0) { Inbox(c: c); Spacer(minLength: 0) }.frame(width: 400).padding(.top, DS.Layout.barHeight + DS.Space.s8).padding(.trailing, DS.Space.s16).padding(.bottom, DS.Space.s16)
            Button("") { c.inboxOpen = false }.keyboardShortcut(.cancelAction).opacity(0).frame(width: 0, height: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .foregroundColor(fg)
        .preferredColorScheme(isDark ? .dark : .light)
        .font(sys(size: DS.TypeRole.body.size))
    }
}

/// One accent swatch (§10.20): the accent's solid fill, a ring and a check when it's the one you're on.
struct AccentDot: View {
    @ObservedObject var c: Core
    let a: AccentSet
    @FocusState private var focused: Bool
    var body: some View {
        let on = a.id == accentName, hover = c.hint == a.name  // Tip sets the hint while hovered
        Button { c.restyle { accentName = a.id } } label: {
            VStack(spacing: DS.Space.s4) {
                Circle().fill(Color(a.solid, dark: isDark)).frame(width: 20, height: 20)
                    .overlay(Circle().stroke(on ? fg : hover || focused ? edge : Color.clear, lineWidth: 2).padding(-3))
                    .overlay { if on { Image(ds: "check", size: iconSize(glyphSize), medium: true).foregroundColor(Color(a.onSolid, dark: isDark)) } }
                    .frame(width: 28, height: 28)
                Text(a.name).font(ds(DS.TypeRole.caption)).foregroundColor(on ? fg : dim)
            }.frame(minWidth: 56).contentShape(Rectangle())
        }.buttonStyle(.plain).focused($focused)
            .modifier(Tip(c: c, text: a.name)).accessibilityLabel("\(a.name) accent\(on ? ", selected" : "")")
    }
}

/// The system sidebar material (§5: glass and materials only in native chrome).
struct SidebarMaterial: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView { let v = NSVisualEffectView(); v.material = .sidebar; v.blendingMode = .behindWindow; v.state = .followsWindowActiveState; return v }
    func updateNSView(_ v: NSVisualEffectView, context: Context) {}
}

struct SettingsWindowView: View {
    @ObservedObject var c: Core
    var body: some View { SettingsView(c: c) }
}

/// Settings (§10.20): the system sidebar (its own material: search, then this Mac's panes and Greenlight's on its host) beside
/// the pane, a form column of grouped sections. Changes apply at once; ⌘W closes. No footer, no Done.
/// ponytail: a split by hand, not NavigationSplitView: inside the app's NSHostingView its columns couldn't be checked drawing
/// (the snapshot harness got an empty sidebar and detail), and Settings must never open blank.
struct SettingsView: View {
    @ObservedObject var c: Core
    @ObservedObject var preview = LivePreview.shared
    @StateObject var rs = RulesStore()
    /// (pane, icon name, words the sidebar's search also matches). This Mac's panes, then greenlight's on its host.
    static let mac = [("General", "sliders", "startup opens dock icon assistant name"),
                      ("Appearance", "contrast", "theme dark light system accent color colour font size text chat look phosphor dashboard source custom"),
                      ("Chat & voice", "chat", "model effort dictation voice whisper engine shortcut"),
                      ("Keyboard", "keyboard", "vim shortcuts keys"),
                      ("Terminal", "terminal", "tab names model effort basement orbs floors sidebar slide ride animation up button"),
                      ("Hosts", "server", "ssh host state code relaunch connection")]
    static let gl = [("Automation", "bolt", "scan interval background agents pages suggestions model effort system one jev haiku typesafe decisions"),
                     ("Autonomy", "wand", "confidence auto answer preselect likely calibration graduate ping gate check-in watch dismiss undo"),
                     ("Projects", "folder", "folders default folder new terminals open"),
                     ("Repositories", "branch", "repo lifecycle stages rules prompts auto-run project solo merge host mac server auto"),
                     ("Instructions", "file-text", "standing rules"),
                     ("Memory", "bookmark", "notes learned")]
    static let wide: Set = ["Projects", "Repositories", "Memory"]

    var hostLabel: String { host.isEmpty || host == "local" ? "this Mac" : host }

    var body: some View {
        let _ = c.look
        HStack(spacing: 0) {
            sidebar.frame(width: 220).background(SidebarMaterial())
            Rectangle().fill(line).frame(width: 1)  // one hairline: the sidebar owns the edge
            Group {
                if Self.wide.contains(c.settingsTab) { widePane } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: DS.Space.s24) { pane }
                            .padding(.horizontal, DS.Space.s32).padding(.top, DS.Space.s16).padding(.bottom, DS.Space.s32)  // the title is in the title bar's band
                            .frame(maxWidth: 640 + 2 * DS.Space.s32, alignment: .leading)
                            .frame(maxWidth: .infinity)
                    }
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).background(bg).ignoresSafeArea()  // the header is the title bar
        .tint(accent)
        .onAppear { rs.load(); rs.projs = delegate.savedProjects }
        .onChange(of: c.settingsTab) { rs.projs = delegate.savedProjects }  // the Basement's editor may have changed them
        .onChange(of: c.settingsOpen) { rs.projs = delegate.savedProjects; if !c.settingsOpen { preview.stop() } }
    }

    /// The system sidebar: search, then the panes; the one you're on in element.active with fg.default. No icon tiles.
    var sidebar: some View {
        let q = rs.q.trimmingCharacters(in: .whitespaces).lowercased()
        let hit = { (t: (String, String, String)) in q.isEmpty || "\(t.0) \(t.2)".lowercased().contains(q) }
        let mac = Self.mac.filter(hit), gl = Self.gl.filter(hit)
        func section(_ t: String) -> some View {
            Text(t).font(ds(DS.TypeRole.label)).foregroundColor(dim).padding(.horizontal, DS.Space.s8).padding(.top, DS.Space.s12).padding(.bottom, DS.Space.s4)
        }
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: DS.Space.s4) {  // §10.4 search: the glyph, then the field
                Image(ds: "search").foregroundColor(Color(DS.Palette.fgFaint, dark: isDark)).accessibilityHidden(true)
                TextField("Search settings", text: $rs.q).textFieldStyle(.plain).font(ds(DS.TypeRole.body))
            }.padding(.horizontal, DS.Space.s8).frame(height: DS.Layout.controlHeightMd)
                .background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(fieldFill))
                .overlay(RoundedRectangle(cornerRadius: DS.Radius.sm).stroke(edge))
                .padding(.horizontal, DS.Space.s12).padding(.top, DS.Space.s48).padding(.bottom, DS.Space.s4)  // under the window's buttons
            ScrollView {
                VStack(alignment: .leading, spacing: DS.Space.s2) {
                    if !mac.isEmpty { section("This Mac"); ForEach(mac, id: \.0) { navButton($0) } }
                    if !gl.isEmpty { section("Greenlight on \(hostLabel)"); ForEach(gl, id: \.0) { navButton($0) } }
                    if mac.isEmpty && gl.isEmpty { note("No settings match “\(rs.q)”.").padding(DS.Space.s8) }
                }.padding(.horizontal, DS.Space.s8).padding(.bottom, DS.Space.s12)
            }
        }
    }

    func navButton(_ t: (String, String, String)) -> some View {
        let on = c.settingsTab == t.0
        return Button { c.settingsTab = t.0; rs.adding = false } label: {
            HStack(spacing: DS.Space.s8) {
                Image(ds: t.1, size: iconSize(DS.TypeRole.body.size), medium: on).foregroundColor(on ? fg : dim).frame(width: 18).accessibilityHidden(true)
                Text(t.0).font(ds(DS.TypeRole.body)).foregroundColor(fg)
                Spacer(minLength: 0)
                if let n = count(t.0), n > 0 { Text("\(n)").font(ds(DS.TypeRole.caption).monospacedDigit()).foregroundColor(dim) }
            }.padding(.horizontal, DS.Space.s8).frame(height: DS.Layout.controlHeightMd).contentShape(Rectangle())
                .background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(on ? Color(DS.Palette.elementActive, dark: isDark) : Color.clear))
        }.buttonStyle(.plain)
            .accessibilityLabel("\(t.0) settings").accessibilityAddTraits(on ? .isSelected : [])
    }
    func count(_ t: String) -> Int? { t == "Projects" ? rs.projs.count : t == "Repositories" ? allRepos.count : t == "Memory" ? rs.notes.count : nil }

    var pane: AnyView {
        switch c.settingsTab {
        case "Appearance": return AnyView(appearance)
        case "Chat & voice": return AnyView(chat)
        case "Keyboard": return AnyView(keyboard)
        case "Terminal": return AnyView(terminal)
        case "Hosts": return AnyView(hosts)
        case "Automation": return AnyView(automation)
        case "Autonomy": return AnyView(autonomyPane)
        case "Instructions": return AnyView(instructions)
        default: return AnyView(general)
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

    static func icon(_ t: String) -> String { (mac + gl).first { $0.0 == t }?.1 ?? "settings" }

    /// A pane's title (`title-sm`, the only 15 on the page) and what it's for (`body`, fg.muted). No icon tile.
    func heading(_ t: String, _ lede: String?, @ViewBuilder trailing: () -> some View = { EmptyView() }) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: DS.Space.s12) {
            VStack(alignment: .leading, spacing: DS.Space.s4) {
                Text(t).font(ds(DS.TypeRole.titleSm)).foregroundColor(fg)
                if let lede { Text(lede).font(ds(DS.TypeRole.body)).foregroundColor(sub).fixedSize(horizontal: false, vertical: true).frame(maxWidth: 560, alignment: .leading) }
            }
            Spacer(minLength: 0)
            trailing()
        }
    }

    /// Helper text: `caption` in fg.muted, subordinate but not disabled-looking.
    func note(_ t: String) -> some View { Text(t).font(ds(DS.TypeRole.caption)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true) }

    /// A small accent label: "new terminals", "yours".
    func chip(_ t: String) -> some View {
        Text(t).font(ds(DS.TypeRole.labelSm)).foregroundColor(Color(accentSet.fg, dark: isDark))
            .padding(.horizontal, DS.Space.s8).frame(height: 20).background(RoundedRectangle(cornerRadius: DS.Radius.xs).fill(Color(accentSet.subtle, dark: isDark)))
    }

    /// A section (§10.20): a `strong` header with an optional `caption` under it, then a group of rows on bg.surface, radius md.
    func sectionHeader(_ title: String, _ aside: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.s2) {
            Text(title).font(ds(DS.TypeRole.strong)).foregroundColor(fg)
            if let aside { note(aside).frame(maxWidth: 420, alignment: .leading) }
        }
    }
    func box<V: View>(_ title: String? = nil, aside: String? = nil, @ViewBuilder _ v: () -> V) -> some View {
        VStack(alignment: .leading, spacing: DS.Space.s8) {
            if let title { sectionHeader(title, aside) }
            VStack(alignment: .leading, spacing: 0) { v() }.padding(.top, -1)  // the first row's hairline tucks under the card's border
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: DS.Radius.md).fill(card)).overlay(RoundedRectangle(cornerRadius: DS.Radius.md).strokeBorder(line))
                .clipShape(RoundedRectangle(cornerRadius: DS.Radius.md))
        }
    }
    func ruled(_ v: some View) -> some View { v.overlay(alignment: .top) { Rectangle().fill(line).frame(height: 1).padding(.horizontal, DS.Space.s16) } }

    /// Label (`body`) and hint (`caption`) on the left, the control on the right.
    func row<V: View>(_ label: String, _ hint: String? = nil, @ViewBuilder _ v: () -> V) -> some View {
        let text = VStack(alignment: .leading, spacing: DS.Space.s2) {
            Text(label).font(ds(DS.TypeRole.body)).foregroundColor(fg)
            if let hint { note(hint) }
        }
        // The label keeps at least 180pt beside the control; a control too wide for that (Effort, Accent, Scan every) moves under it.
        return ruled(ViewThatFits(in: .horizontal) {
            HStack(spacing: 0) {
                text.frame(minWidth: 180, idealWidth: 180, maxWidth: 420, alignment: .leading)  // one measure for every hint
                Spacer(minLength: DS.Space.s24)
                HStack(spacing: DS.Space.s8) { v() }.layoutPriority(1)
            }
            VStack(alignment: .leading, spacing: DS.Space.s8) { text.frame(maxWidth: .infinity, alignment: .leading); HStack(spacing: DS.Space.s8) { v() } }
        }.padding(.horizontal, DS.Space.s16).padding(.vertical, DS.Space.s12).frame(minHeight: 44))
    }

    func toggleRow(_ title: String, _ help: String, _ on: Binding<Bool>) -> some View {
        row(title, help) { Toggle(title, isOn: on).labelsHidden().toggleStyle(.switch) }
    }
    /// Rows that only matter while the row above is on: indented, dimmed while it's off.
    func dependent<V: View>(off: Bool, @ViewBuilder _ v: () -> V) -> some View {
        VStack(alignment: .leading, spacing: 0) { v() }.padding(.leading, DS.Space.s16)
            .disabled(off).opacity(off ? 0.45 : 1)
    }

    /// Projects, Repositories, Memory: a full-height list on the left (its add button at the bottom), the detail scrolling on the right.
    /// `empty` (icon, title, hint) while there's nothing to list: the pane is its heading and one empty state.
    @ViewBuilder func split<L: View, D: View>(_ t: String, _ lede: String, add: (String, () -> Void), empty: (String, String, String)? = nil,
                                              @ViewBuilder list: () -> L, @ViewBuilder detail: () -> D) -> some View {
        if let empty {
            VStack(alignment: .leading, spacing: 0) {
                heading(t, nil).padding(.horizontal, DS.Space.s32).padding(.top, DS.Space.s16)  // the empty state says what the lede would
                    .frame(maxWidth: 640 + 2 * DS.Space.s32, alignment: .leading).frame(maxWidth: .infinity)
                emptyDetail(empty.0, empty.1, empty.2, add: add)
                Spacer(minLength: 0)
            }
        } else { HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(t).font(ds(DS.TypeRole.titleSm)).foregroundColor(fg)
                        Text(lede).font(sys(size: DS.TypeRole.caption.size)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
                    }
                }.padding(.horizontal, 14).padding(.top, DS.Space.s16).padding(.bottom, 14)
                ScrollView { VStack(spacing: 1) { list() }.padding(.horizontal, 8) }
                Button(action: add.1) { Label { Text(add.0) } icon: { Image(ds: "plus") }.font(ds(DS.TypeRole.body)).foregroundColor(sub) }.buttonStyle(.plain)  // quiet: not the accent
                    .padding(.horizontal, 14).frame(maxWidth: .infinity, minHeight: 36, alignment: .leading)
            }.frame(width: 220)
            Divider().overlay(line)
            ScrollView {
                VStack(alignment: .leading, spacing: 22) { detail() }
                    .padding(.horizontal, 24).padding(.top, DS.Space.s16).padding(.bottom, 32)
                    .frame(maxWidth: 760, alignment: .leading).frame(maxWidth: .infinity, alignment: .leading)
            }
        } }
    }

    /// §10.21: one inline notice. Neutral for info; `failed` is the crashed glyph and tint. The text stays `fg`.
    func banner(_ icon: String, _ text: String, failed: Bool = false, @ViewBuilder action: () -> some View = { EmptyView() }) -> some View {
        HStack(spacing: DS.Space.s8) {
            Image(ds: failed ? "crashed" : icon).foregroundColor(failed ? StatusInk.crashedInk : sub).accessibilityLabel(failed ? "Failed" : "").accessibilityHidden(!failed)
            Text(text).font(ds(DS.TypeRole.body)).foregroundColor(fg).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: DS.Space.s8)
            action()
        }.padding(DS.Space.s12)
            .background(RoundedRectangle(cornerRadius: DS.Radius.md).fill(failed ? Color(DS.Palette.statusCrashedBg, dark: isDark) : surface))
    }

    func listItem(_ title: String, _ meta: String, on: Bool, icon: String? = nil, tag: String? = nil, _ pick: @escaping () -> Void) -> some View {
        Button(action: pick) {
            HStack(spacing: 10) {
                if let icon { Image(ds: icon).foregroundColor(on ? fg : sub).frame(width: 16) }
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(ds(DS.TypeRole.strong)).foregroundColor(fg).lineLimit(1).truncationMode(.middle)
                    Text(meta).font(sys(size: DS.TypeRole.caption.size)).foregroundColor(dim).lineLimit(1)
                }
                Spacer(minLength: 0)
                if let tag { chip(tag) }
            }.padding(.horizontal, 10).padding(.vertical, DS.Space.s8).contentShape(Rectangle())
                .background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(on ? Color(DS.Palette.elementActive, dark: isDark) : Color.clear))
        }.buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
    }

    /// §10.21: a detail with nothing picked: a faint icon, a line, what to do. Centred in the column.
    func emptyDetail(_ icon: String, _ title: String, _ hint: String, add: (String, () -> Void)? = nil) -> some View {
        VStack(spacing: DS.Space.s8) {
            Image(ds: icon, size: 20).foregroundColor(Color(DS.Palette.fgFaint, dark: isDark)).accessibilityHidden(true)
            Text(title).font(ds(DS.TypeRole.titleSm)).foregroundColor(fg)
            Text(hint).font(ds(DS.TypeRole.body)).foregroundColor(sub).multilineTextAlignment(.center).frame(maxWidth: 320)
            if let add { Button(add.0 + "…", action: add.1).padding(.top, DS.Space.s8) }
        }.frame(maxWidth: .infinity).padding(.top, add == nil ? 200 : 140)
    }

    /// A path or name in a card, with its buttons on the right.
    func pathRow<V: View>(_ icon: String, _ text: String, @ViewBuilder _ actions: () -> V) -> some View {
        ruled(HStack(spacing: 10) {
            Image(ds: icon, size: iconSize(DS.TypeRole.label.size)).foregroundColor(sub).frame(width: 16)
            Text(text).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).foregroundColor(fg).lineLimit(1).truncationMode(.middle)
            Spacer(minLength: DS.Space.s8)
            actions()
        }.padding(.leading, DS.Space.s16).padding(.trailing, DS.Space.s12).frame(minHeight: 40))
    }
    func xButton(_ label: String, _ act: @escaping () -> Void) -> some View {
        Button(action: act) { Image(ds: "close", size: iconSize(DS.TypeRole.label.size)) }.buttonStyle(.borderless).foregroundColor(sub).accessibilityLabel(label).help(label)
    }

    /// A multi-line field: a §10.4 well under an optional section header, never a card.
    func editor(_ t: Binding<String>, _ label: String, height: CGFloat = 120, title: String? = nil, aside: String? = nil, placeholder: String = "") -> some View {
        VStack(alignment: .leading, spacing: DS.Space.s8) {
            if let title { sectionHeader(title, aside) }
            TextEditor(text: t).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).scrollContentBackground(.hidden).padding(DS.Space.s8)
                .frame(minHeight: height).background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(fieldFill))
                .overlay(alignment: .topLeading) {
                    if t.wrappedValue.isEmpty {
                        Text(placeholder).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).foregroundColor(Color(DS.Palette.fgFaint, dark: isDark))
                            .padding(.horizontal, DS.Space.s12 + 1).padding(.vertical, DS.Space.s8).allowsHitTesting(false).accessibilityHidden(true)
                    }
                }
                .overlay(RoundedRectangle(cornerRadius: DS.Radius.sm).strokeBorder(edge)).accessibilityLabel(label)
        }
    }

    func saveRow(_ title: String = "Save", _ save: @escaping () -> Void, @ViewBuilder more: () -> some View = { EmptyView() }) -> some View {
        HStack(spacing: DS.Space.s12) {
            more()
            if rs.busy { ProgressView().controlSize(.small) }
            note(rs.status)
            Spacer(minLength: 0)
            Button(title, action: save).keyboardShortcut("s").buttonStyle(.borderedProminent).disabled(rs.busy)
        }
    }

    // MARK: This Mac

    @ViewBuilder var general: some View {
        heading("General", "How Greenlight starts and how it shows up on this Mac.")
        box {
            row("Opens to", "The floor Greenlight opens on. New windows (⌘N) always open to the Pipe.") {  // the title bar's own control
                FloorSegments(at: c.termOpens, go: { c.termOpens = $0 }).fixedSize()
            }
            row("Name", "What the chat calls itself. ⌃⌥M shows or hides it from any app.") {
                TextField("Greenlight", text: $c.name).textFieldStyle(DSField()).frame(width: 220)
            }
            row("Dock icon", "Debug is the same icon with a grey light. Dev copies start on it.") {
                HStack(spacing: DS.Space.s16) { ForEach(["Default", "Debug"], id: \.self) { dockTile($0, $0) } }
            }
        }
    }

    func dockTile(_ id: String, _ name: String) -> some View {
        let on = c.dockIcon == id
        return Button { c.dockIcon = id } label: {
            VStack(spacing: DS.Space.s4) {  // the ring is around the icon only, 2pt off it
                Image(nsImage: Bundle.main.image(forResource: id == "Debug" ? "GreenlightDebug" : "Greenlight") ?? NSApp.applicationIconImage).resizable().frame(width: 48, height: 48)
                    .padding(DS.Space.s4).overlay(RoundedRectangle(cornerRadius: DS.Radius.lg).stroke(on ? Color(accentSet.ring, dark: isDark) : Color.clear, lineWidth: 2))
                Text(name).font(ds(DS.TypeRole.caption)).foregroundColor(on ? fg : sub)
            }.contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("\(name) Dock icon\(on ? ", selected" : "")")
    }

    /// Settings › Appearance (§10.20): appearance, accent, contrast, text, the chat look and the terminal's themes, with a live
    /// preview. ponytail: the neutral-tint slider and Match terminal need the tokens generated at run time; not here yet.
    @ViewBuilder var appearance: some View {
        heading("Appearance", "How every window looks. Changes apply at once.")
        box {
            row("Appearance", "System follows macOS. The View menu has the same choice.") {
                Picker("Appearance", selection: Binding(get: { appearanceMode }, set: { m in c.restyle { ud.set(m, forKey: "appearance") }; delegate.tick() })) {
                    ForEach(App.appearances, id: \.0) { Text($0.1).tag($0.0) }
                }.labelsHidden().pickerStyle(.segmented).fixedSize()
            }
            row("Accent", "The primary button, links, focus and the selected item. Status colours never change.") {
                HStack(spacing: DS.Space.s4) { ForEach(accents, id: \.id) { AccentDot(c: c, a: $0) } }
            }
            row("Contrast", "Follows System Settings › Accessibility › Display › Increase contrast.") {
                Text(NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast ? "Increased" : "Standard").font(ds(DS.TypeRole.body)).foregroundColor(sub)
            }
            row("Text size", "Chat, settings and terminal. The dashboard keeps its own size.") {
                Text("A").font(sys(size: DS.TypeRole.caption.size)).foregroundColor(sub).accessibilityHidden(true)
                Slider(value: Binding(get: { fontSize }, set: { v in c.restyle { fontSize = v } }), in: 11...17, step: 1).frame(width: 140).accessibilityLabel("Text size")
                Text("A").font(sys(size: DS.TypeRole.title.size)).foregroundColor(sub).accessibilityHidden(true)
                Text("\(Int(fontSize)) pt").font(ds(DS.TypeRole.label).monospacedDigit()).foregroundColor(sub).frame(width: 36, alignment: .trailing)
            }
            row("Chat look", "Default follows the appearance; Phosphor is the terminal's green on near-black.") {
                Picker("Chat look", selection: Binding(get: { c.voice.look.id }, set: { c.voice.look = lookNamed($0) })) {
                    Text("Default").tag("default"); Text("Phosphor").tag("phosphor")
                }.labelsHidden().pickerStyle(.segmented).fixedSize()
            }
        }
        previewCard
        box("Fonts") {
            row("Chat font", "Messages, the composer and questions.") { fontPicker("Chat font", chatFont) { chatFont = $0 } }
            row("Code font", "Code blocks, logs and the terminal.") { fontPicker("Code font", codeFont) { codeFont = $0 } }
        }
        box("Terminal theme", aside: "The terminal canvas only") {
            row("In light", "The Basement's terminals while the app is light.") { termTheme(dark: false) }
            row("In dark", "The Basement's terminals while the app is dark.") { termTheme(dark: true) }
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
                        TextField("File on this Mac or http(s) URL", text: $c.fluidURL).textFieldStyle(DSField()).onSubmit(c.reloadFluid).frame(width: 240)
                        Button("Reload", action: c.reloadFluid)
                    }
                }
            }
        }
        HStack {
            Spacer()
            Button("Reset appearance to defaults") {
                c.restyle { darkTheme = named("", "Greenlight"); lightTheme = named("", "GitHub Light"); accentName = "signal"; chatFont = "System"; codeFont = "SF Mono"; fontSize = 13
                    ud.set("auto", forKey: "appearance") }
                delegate.tick()
            }.buttonStyle(.plain).font(ds(DS.TypeRole.label)).foregroundColor(Color(accentSet.fg, dark: isDark))
        }
    }

    /// A live preview: a chat turn, a status and the primary button, in the appearance, accent and text size you've picked.
    var previewCard: some View {
        VStack(alignment: .leading, spacing: DS.Space.s12) {
            Text("Preview").font(ds(DS.TypeRole.strong)).foregroundColor(fg)
            VStack(alignment: .leading, spacing: DS.Space.s8) {
                HStack { Spacer(minLength: DS.Space.s48)
                    Text("Which MRs need me?").font(ds(DS.TypeRole.bodyCopy, face: chatFont)).foregroundColor(fg)
                        .padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s8).background(RoundedRectangle(cornerRadius: DS.Radius.md).fill(surface)) }
                Text("!12 needs your answer; the rest are working.").font(ds(DS.TypeRole.bodyCopy, face: chatFont)).foregroundColor(fg)
                HStack(spacing: DS.Space.s8) {
                    Label { Text("Needs you") } icon: { Image(ds: "needs", size: iconSize(DS.TypeRole.label.size)) }.font(ds(DS.TypeRole.label)).foregroundColor(StatusInk.needsInk)
                    Label { Text("Working") } icon: { Image(ds: "working", size: iconSize(DS.TypeRole.label.size)) }.font(ds(DS.TypeRole.label)).foregroundColor(StatusInk.working)
                    Spacer(minLength: DS.Space.s8)
                    Text("Answer").font(ds(DS.TypeRole.label)).foregroundColor(Color(accentSet.onSolid, dark: isDark)).padding(.horizontal, DS.Space.s12)
                        .frame(height: DS.Layout.controlHeightMd).background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(accent))
                }
            }.padding(DS.Space.s16).background(RoundedRectangle(cornerRadius: DS.Radius.md).fill(card)).overlay(RoundedRectangle(cornerRadius: DS.Radius.md).stroke(line))
        }.accessibilityElement(children: .combine).accessibilityLabel("Preview")
    }

    /// The terminal canvas's theme in one appearance (§2.5): a swatch of it and the theme menu.
    func termTheme(dark: Bool) -> some View {
        let t = dark ? darkTheme : lightTheme
        return HStack(spacing: DS.Space.s8) {
            Text("ab").font(ds(DS.TypeRole.code, mono: true)).foregroundColor(Color(nsColor: t.fg)).frame(width: 32, height: 22)
                .background(RoundedRectangle(cornerRadius: DS.Radius.xs).fill(Color(nsColor: t.bg))).overlay(RoundedRectangle(cornerRadius: DS.Radius.xs).stroke(edge)).accessibilityHidden(true)
            Picker(dark ? "Dark terminal theme" : "Light terminal theme", selection: Binding(get: { t.name }, set: { n in
                c.restyle { if let t = themes.first(where: { $0.name == n }) { if dark { darkTheme = t } else { lightTheme = t } } }
            })) { ForEach(themes.filter { $0.dark == dark }, id: \.name) { Text($0.name).tag($0.name) } }.labelsHidden().fixedSize()
        }
    }

    func fontPicker(_ label: String, _ current: String, _ set: @escaping (String) -> Void) -> some View {
        Picker(label, selection: Binding(get: { current }, set: { v in c.restyle { set(v) } })) {
            ForEach(fontChoices, id: \.self) { Text($0).tag($0) }
        }.labelsHidden().fixedSize()
    }

    @ViewBuilder var chat: some View {
        heading("Chat & voice", "The chat pane's model, and dictating into it from any app.")
        box("Model") {
            ruled(ModelSelector(c: c, title: "Chat model", hint: "Changes apply from your next message.", model: $c.model, effort: $c.effort))
            toggleRow("Live activity", "Under the reply as \(c.name) works: the running tool call, time, tokens and speed, like Claude Code.", $c.liveLine)
            toggleRow("Verbose activity", "A live log above the message box: \(c.name)'s every command with its output, what your agents are doing, and each move Greenlight makes.", $c.verbose)
        }
        let status = engineStatus  // "Ready · …" or "Not ready · …": the hint says it, no dot
        box("Dictation") {
            row("Engine", status) {
                Picker("Voice engine", selection: $c.engine) { ForEach(engines, id: \.0) { Text($0.1).tag($0.0) } }.labelsHidden().fixedSize()
            }
            if c.engine == "whisper" { dependent(off: false) { row("Whisper model") { Button("Choose model…") { delegate.pickModel() } } } }
            if c.engine == "openai" { dependent(off: false) { row("OpenAI key", openAIKeySource()) { Button("Set key…") { delegate.setKey() } } } }
            row("Shortcut", "Press it in any app to dictate to the chat.") { shortcutEditor }
        }
        box("Voice mode") {
            row("Anthropic key", "Draws what \(c.name) says on the voice stage. Stored in your Keychain; without it the stage shows a plain list.") {
                Button("Set key…") { delegate.askKey("anthropic", "Anthropic API key", "voice mode's stage (Claude Haiku 5.5)") }
            }
            row("Speaking voice", Speech.shared.status.isEmpty ? (Speech.shared.kind == "live" ? "OpenAI's GPT-Live 1.1 holds the conversation: it hears you, hands each request to \(c.name) and reads its answer. About $0.05 a minute while voice mode is on; if it fails, voice mode stops and says why (no Apple voice standing in)." : "Kokoro runs on this Mac. ElevenLabs is paid and is sent each line. Either falls back to Apple's voice.") : Speech.shared.status) {
                Picker("Speaking voice", selection: Binding(get: { Speech.shared.kind }, set: { ud.set($0, forKey: "speech"); preview.stop(); Speech.shared.stop(); Speech.shared.warm(); c.voiceTest = ""; c.objectWillChange.send() })) {
                    ForEach(Speech.kinds, id: \.0) { Text($0.1).tag($0.0) }
                }.labelsHidden().fixedSize()
            }
            if Speech.shared.kind == "kokoro" { dependent(off: false) { row("Kokoro voice", "a = American, b = British; f/m = female/male.") {
                Picker("Kokoro voice", selection: Binding(get: { Speech.shared.kokoroVoice }, set: { ud.set($0, forKey: "kokoroVoice"); c.objectWillChange.send() })) {
                    ForEach(Speech.kokoroVoices, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().fixedSize()
            } } }
            if Speech.shared.kind == "live" { dependent(off: false) {
                row("OpenAI key", openAIKeySource() + " The same key as the OpenAI Whisper engine.") { Button("Set key…") { delegate.setKey() } }
                row("GPT-Live voice", preview.hint ?? "Picking one says “\(livePreviewLine)” in it. Fixed for each session: a new one takes effect next time voice mode starts.") {
                    if case .loading = preview.phase { ProgressView().controlSize(.small).accessibilityLabel("Getting the voice") }
                    Button { preview.busy ? preview.stop() : preview.play(LiveVoice.voice, voiceOn: c.voice.on) } label: {
                        Image(ds: preview.busy ? "stop" : "play").frame(width: 14)
                    }.help(preview.busy ? "Stop the preview" : "Hear this voice").accessibilityLabel(preview.busy ? "Stop the preview" : "Hear this voice")
                    Picker("GPT-Live voice", selection: Binding(get: { LiveVoice.voice }, set: {
                        guard $0 != LiveVoice.voice else { return }
                        ud.set($0, forKey: "liveVoice"); c.voiceTest = ""; c.objectWillChange.send()
                        preview.play($0, voiceOn: c.voice.on)
                    })) {
                        ForEach(LiveVoice.voices, id: \.self) { Text($0.capitalized).tag($0) }
                    }.labelsHidden().fixedSize()
                }
            } }
            if Speech.shared.kind == "eleven" { dependent(off: false) {
                row("ElevenLabs key", "Stored in your Keychain.") { Button("Set key…") { delegate.askKey("elevenlabs", "ElevenLabs API key", "voice mode's speaking voice") } }
                row("ElevenLabs voice ID", "From elevenlabs.io → Voices. Empty: Sarah.") {
                    TextField("EXAVITQu4vr4xnSDxMaL", text: Binding(get: { ud.string(forKey: "elevenVoice") ?? "" }, set: { ud.set($0.trimmingCharacters(in: .whitespaces), forKey: "elevenVoice"); Speech.shared.warm() })).frame(width: 216)
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
            row("Echo cancellation", "Keeps \(c.name)'s voice out of the mic. Off for a screen recording: with it on, macOS can turn your mic down in other apps and leave \(c.name)'s voice out of the recording. Applies when voice mode next starts.") {
                // a new engine: the old one's output is still wired for voice processing
                Toggle("Echo cancellation", isOn: Binding(get: { c.voice.echoCancel }, set: { ud.set($0, forKey: "voiceEcho"); c.voice.staleEngine = true; c.objectWillChange.send() })).labelsHidden().toggleStyle(.switch)
            }
            dependent(off: !c.voice.echoCancel) {
                row("Interrupt by talking", "Talk over \(c.name) to cut it off. Off: it finishes each answer first (for speakers that echo).") {
                    Toggle("Interrupt by talking", isOn: Binding(get: { c.voice.bargeIn }, set: { ud.set($0, forKey: "voiceBargeIn"); c.objectWillChange.send() })).labelsHidden().toggleStyle(.switch)
                }
            }
        }
        HStack(spacing: DS.Space.s4) {
            note("Standing instructions for Greenlight have their own page:")
            Button("Instructions") { c.settingsTab = "Instructions" }.buttonStyle(.link).font(sys(size: DS.TypeRole.label.size))
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
            let m = ud.string(forKey: "openaiModel") ?? "whisper-1"
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
            Text("Press shortcut… Esc cancels").font(sys(size: DS.TypeRole.label.size, weight: .medium)).foregroundColor(StatusInk.workingInk)
                .padding(.horizontal, DS.Space.s8).frame(height: 22)
                .overlay(RoundedRectangle(cornerRadius: DS.Radius.sm).stroke(StatusInk.working, style: StrokeStyle(lineWidth: 1, dash: [3])))
        } else {
            HStack(spacing: DS.Space.s8) { keycaps(c.shortcut); Button("Change…") { delegate.recordShortcut() } }
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

    /// One column: the label on the left, its keys on one right edge.
    func shortcuts(_ t: String, _ keys: [(String, String)]) -> some View {
        box(t) { ForEach(keys, id: \.0) { a, k in shortcut(a, k) } }
    }
    func shortcut(_ a: String, _ k: String) -> some View {
        ruled(HStack(spacing: DS.Space.s8) {
            Text(a).font(ds(DS.TypeRole.body)).foregroundColor(fg).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: DS.Space.s8)
            if a == "Dictate to chat" { shortcutEditor } else { keycaps(k) }  // the voice shortcut is edited where it's listed
        }.padding(.horizontal, DS.Space.s16).frame(minHeight: DS.Layout.controlHeightLg))
    }

    /// "↑|↓" is either key, as separate caps; a chord is one cap with its modifiers spaced.
    func keycaps(_ k: String) -> some View {
        HStack(spacing: DS.Space.s4) {
            ForEach(Array(k.split(separator: "|").enumerated()), id: \.offset) { i, key in
                if i > 0 { Text("or").font(ds(DS.TypeRole.caption)).foregroundColor(Color(DS.Palette.fgFaint, dark: isDark)) }
                Text(key.count > 4 ? String(key) : key.map(String.init).joined(separator: "\u{2009}")).font(ds(DS.TypeRole.labelSm)).foregroundColor(sub)
                    .padding(.horizontal, DS.Space.s4 + 2).frame(minWidth: 22).frame(height: 20)
                    .background(RoundedRectangle(cornerRadius: DS.Radius.xs).fill(surface)).overlay(RoundedRectangle(cornerRadius: DS.Radius.xs).stroke(edge))
            }
        }
    }

    @ViewBuilder var terminal: some View {
        heading("Terminal", "The Basement's tabs, and the orbs that ride between it and Greenlight Center.")
        box("Floating orbs") {
            toggleRow("Show floating orbs", "The buttons bottom right that ride between floors. Off: ⌘↑ / ⌘↓ still ride.",
                      Binding(get: { !c.orbsHidden }, set: { c.orbsHidden = !$0 }))
            row("Ride animation", "The orb's flip between floors. 0: you're just there.") { seconds("Ride animation", $c.rideTime) }
            row("Sidebar slide", "How long the Basement's sidebar takes to slide in. 0: it's just there.") { seconds("Sidebar slide", $c.sidebarSlide) }
        }
        box("Tab names") {
            toggleRow("Name tabs with AI", "Once a tab's output settles, names it from what's on screen. A name you type always wins.", $c.termAutoName)
            dependent(off: !c.termAutoName) {
                ruled(ModelSelector(c: c, title: "Naming model", model: $c.termNameModel, effort: $c.termNameEffort, allowedHarnesses: writerHarnesses))
            }
        }
    }

    /// A duration: a slider for the usual 0–2s, a field for up to 5s. 0 is off, and each row's hint says so.
    func seconds(_ label: String, _ v: Binding<Double>) -> some View {
        let b = Binding(get: { v.wrappedValue }, set: { v.wrappedValue = min(max($0, 0), 5) })
        return HStack(spacing: DS.Space.s8) {
            Slider(value: Binding(get: { min(b.wrappedValue, 2) }, set: { b.wrappedValue = ($0 * 20).rounded() / 20 }), in: 0...2)
                .frame(width: 130).accessibilityLabel(label)  // no `step:`, which draws 41 tick marks
            TextField(label, value: b, format: .number.precision(.fractionLength(0...2))).textFieldStyle(DSField()).multilineTextAlignment(.trailing).frame(width: 52)
            Text("s").foregroundColor(sub)
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
                }.labelsHidden().frame(width: 180, alignment: .trailing)
                Button("Test") { c.testHost() }.help("Test the connection")
            }
            dependent(off: false) {
                row("State folder", "On that host.") { TextField("~/.greenlight", text: $c.runHome).textFieldStyle(DSField()).frame(width: 240) }
                row("Code folder", "Where greenlight.py lives on that host.") { TextField("~/.claude/skills/greenlight", text: $c.runCode).textFieldStyle(DSField()).frame(width: 240) }
            }
        }
        if c.hostPending || env != nil {  // one notice, under the card it's about
            banner(env == nil ? "refresh" : "info", [env.map { "GREENLIGHT_HOST=\($0) is set, and it wins over this setting." },
                                                       c.hostPending ? "Greenlight reads the host at launch. Relaunch to apply." : nil].compactMap { $0 }.joined(separator: " ")) {
                if c.hostPending { Button("Relaunch") { c.relaunch() } }
            }.padding(.top, -DS.Space.s16)
        }
        box("The Basement's hosts", aside: "From ~/.ssh/config") {
            row("This Mac's name", "Shown in the Basement's sidebar. Empty: its hostname.") { TextField(localDefault, text: $c.localName).textFieldStyle(DSField()).frame(width: 240) }
            pathRow("laptop", c.localShown) { Text("local").font(sys(size: DS.TypeRole.caption.size)).foregroundColor(dim) }
            ForEach(ssh, id: \.self) { h in pathRow("server", h) { if h == host { chip("runs Greenlight") } else { Text("ssh").font(sys(size: DS.TypeRole.caption.size)).foregroundColor(dim) } } }
            ruled(HStack {
                note("Every Host in ~/.ssh/config, minus wildcards, git hosts and -ts twins. A new one shows after a relaunch.")
                Spacer(minLength: DS.Space.s12)
                Button("Edit ~/.ssh/config") { _ = try? Process.run(URL(fileURLWithPath: "/usr/bin/open"), arguments: ["-t", NSHomeDirectory() + "/.ssh/config"]) }
            }.padding(.horizontal, DS.Space.s16).padding(.vertical, DS.Space.s12))
        }
    }

    // MARK: Greenlight, on its host

    @ViewBuilder var automation: some View {
        heading("Automation", "What Greenlight does on \(hostLabel) between your visits: scanning workspaces, and the background agents that build the dashboard.")
        let failed = !c.greenlightEveryNote.isEmpty && !c.greenlightEveryLoading
        box("Workspace scan") {
            row("Scan every", c.greenlightEveryLoading ? "Checking \(hostLabel)…" : c.greenlightEvery == 0 ? "Off: nothing scans or starts agents on a schedule."
                : "Scans every workspace on \(hostLabel), starts agents when work is waiting, and refreshes the dashboard.") {
                Picker("Scan interval", selection: Binding(get: { c.greenlightEvery }, set: c.setGreenlightEvery)) {
                    Text("Off").tag(0)
                    ForEach([10, 15, 30, 60], id: \.self) { Text($0 == 60 ? "1 hour" : "\($0) min").tag($0) }
                }.labelsHidden().fixedSize().onAppear(perform: c.loadGreenlightEvery).disabled(c.greenlightEveryLoading)
            }
        }
        if failed {
            banner("crashed", c.greenlightEveryNote + " The schedule shown may be out of date.", failed: true) { Button("Retry", action: c.loadGreenlightEvery) }
                .padding(.top, -DS.Space.s16)
        }
        box("Background agents") {
            ruled(ModelSelector(c: c, title: "Workspace pages", hint: "Builds each workspace's page on the dashboard.", model: $c.designModel, effort: $c.designEffort, allowedHarnesses: writerHarnesses))
            dependent(off: false) {
                row("Rebuild", c.designParallel == 0 ? "A page builds only when you open it." : "Workspaces whose state changed rebuild in the background.") {
                    Picker("Rebuild pages", selection: Binding(get: { c.designParallel > 0 }, set: { c.designParallel = $0 ? max(c.designParallel, 3) : 0 })) {
                        Text("On demand").tag(false); Text("Background").tag(true)
                    }.labelsHidden().pickerStyle(.segmented).fixedSize()
                }
                if c.designParallel > 0 {
                    row("Pages at once") { Stepper("\(c.designParallel)", value: $c.designParallel, in: 1...10).fixedSize().accessibilityLabel("Parallel builds") }
                }
            }
            ruled(ModelSelector(c: c, title: "Next-step suggestions", hint: "Suggests the next action on each hex when its state moves.", model: $c.nextModel, effort: $c.nextEffort, allowedHarnesses: writerHarnesses))
            row("Idle check-in", "When you're at your Mac but haven't done anything in Greenlight for this long, the chat suggests what to work on next.") {
                Picker("Idle check-in", selection: $c.checkInMins) { Text("Off").tag(0); Text("15 min").tag(15); Text("30 min").tag(30); Text("1 hour").tag(60) }.labelsHidden().fixedSize()
            }
            row("Watch workspaces", "When a workspace starts needing you, the chat looks into it and asks you a question if one's worth asking.") {
                Toggle("Watch workspaces", isOn: $c.watch).labelsHidden().toggleStyle(.switch)
            }
        }
        box("System One", aside: "decide.py on \(hostLabel)") {
            row("Quick decisions", c.s1Provider == "local" ? "Off: greenlight's own patterns decide. On: a fast model reads what each agent's terminal shows (any harness), what your goals ask (hold, stay draft, park, revisit) and which hexes Chief's reply is about."
                : "Reads what each agent's terminal shows, what your goals ask and which hexes Chief's reply is about. Patterns still hold: a model only adds a hold, never takes one away.") {
                if c.s1Loading { ProgressView().controlSize(.small) }
                Picker("System One model", selection: Binding(get: { c.s1Provider }, set: { c.setSystemOne(provider: $0) })) {
                    Text("Off").tag("local"); Text("Jev").tag("jev"); Text("Haiku").tag("haiku")
                }.labelsHidden().pickerStyle(.segmented).fixedSize().onAppear(perform: c.loadSystemOne)
            }
            dependent(off: c.s1Provider == "local") {
                row("If it fails", "Asked when the first model errors or times out; after it, the patterns.") {
                    Picker("System One fallback", selection: Binding(get: { c.s1Fallback }, set: { c.setSystemOne(fallback: $0) })) {
                        Text("Patterns").tag("")
                        if c.s1Provider != "haiku" { Text("Haiku").tag("haiku") }
                        if c.s1Provider != "jev" { Text("Jev").tag("jev") }
                    }.labelsHidden().fixedSize()
                }
                row("TypeSafe key", c.s1Key ? "Stored on \(hostLabel), for Jev." : "Jev needs one: set it here, or TYPESAFE_API_KEY on \(hostLabel).") {
                    Button(c.s1Key ? "Change key…" : "Set key…") { delegate.setTypesafeKey() }
                }
                row("Test", c.s1Test.isEmpty ? "Asks each model a question with a known answer." : c.s1Test) { Button("Test") { c.testSystemOne() } }
            }
            if !c.s1Note.isEmpty { ruled(note(c.s1Note).padding(DS.Space.s16)) }
        }
        // ponytail: auto-run is loaded per repo, so it's a link, not the design's every-repo table
        banner("bolt", "Auto-run jobs (Fix CI, Resolve threads, Fix conflicts, Review) are set per repository.") { Button("Open Repositories") { c.settingsTab = "Repositories" } }
    }

    // MARK: Autonomy (autonomy.py on the host)

    static let kindTitle = ["delete-workspace": "Delete a finished workspace", "keep-draft": "Keep a PR as a draft", "merge-held": "Merge a held PR",
                            "milestone": "Set a milestone", "agent-stuck": "Unstick an agent", "signoff": "Sign off a step"]
    func kindStats(_ k: AutonomyReport.Kind) -> String? {
        let past = k.n.map { n in "\(k.right ?? 0) of \(n) guesses matched your answer." }  // none checked yet: no hint, the section says it once
        return k.eligible ? past : "A human gate: Greenlight never answers this for you, nor preselects it."
    }
    @ViewBuilder var autonomyPane: some View {
        heading("Autonomy", "Greenlight guesses your answer from what you answered before, and asks whenever it isn't sure.")
        Group {
            if let r = c.autonomy {
                ForEach(r.candidates, id: \.kind) { cand in
                    banner("graduation", "\(Self.kindTitle[cand.kind] ?? cand.kind): \(cand.right) of \(cand.n) sure guesses were right. Let Greenlight answer these itself?") {
                        Button("Turn on") { c.setAutonomy(["--auto", cand.kind, "on"]) }
                    }
                }
                box("Answered for you", aside: "At 95% sure, with the kind turned on here, it answers an inactive choice such as keep draft; you can ask it to reconsider. At 65% it preselects the answer in the bell.\(r.kinds.contains { $0.n != nil } ? "" : " No guesses checked yet.")") {
                    ForEach(r.kinds, id: \.kind) { k in
                        row(Self.kindTitle[k.kind] ?? k.kind, kindStats(k)) {
                            if k.eligible {
                                Toggle(Self.kindTitle[k.kind] ?? k.kind, isOn: Binding(get: { k.opted }, set: { c.setAutonomy(["--auto", k.kind, $0 ? "on" : "off"]) }))
                                    .labelsHidden().toggleStyle(.switch)
                            } else {
                                Label { Text("Always asks") } icon: { Image(ds: "lock") }.font(ds(DS.TypeRole.body)).foregroundColor(sub)
                            }
                        }
                    }
                }
                box("Questions nobody asked for") {
                    row("Ping gate", "Idle check-ins and the watch go out only while you act on them more often than you dismiss them (✕, or left unanswered a day).") {
                        Picker("Ping gate", selection: Binding(get: { r.gate }, set: { c.setAutonomy(["--gate", "\($0)"]) })) {
                            Text("Off").tag(0.0); Text("Lenient").tag(0.25); Text("Balanced").tag(0.35); Text("Strict").tag(0.5)
                        }.labelsHidden().fixedSize()
                    }
                }
                if !r.recent.isEmpty {
                    box("Answered for you lately") {
                        ForEach(r.recent) { a in
                            ruled(AutonomyActRow(a: a, title: Self.kindTitle[a.kind] ?? a.kind, fg: fg, dim: dim) { c.undoAudit(a.id) })
                        }
                    }
                }
            } else if c.autonomyNote.isEmpty {
                HStack(spacing: DS.Space.s8) { ProgressView().controlSize(.small); note("Reading \(hostLabel)…") }
            }
            if !c.autonomyNote.isEmpty {
                banner("alert", c.autonomyNote) { Button("Retry", action: c.loadAutonomy) }
            }
        }.onAppear(perform: c.loadAutonomy)
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
        split("Projects", "Repositories and the folders they're checked out in. Each gets its own Basement window.", add: ("New project", { _ = addProject() }),
              empty: rs.projs.isEmpty ? ("folder", "No projects yet", "A project is a set of repositories and the folders they're checked out in.") : nil) {
            ForEach(rs.projs.indices, id: \.self) { i in
                let p = rs.projs[i], n = pstrings(p, "repos").count, d = pstrings(p, "dirs").count
                listItem(pname(p), "\(n) \(n == 1 ? "repo" : "repos") · \(d) \(d == 1 ? "folder" : "folders")", on: pid(p) == rs.proj, icon: "folder") { rs.proj = pid(p); rs.draftDir = ""; rs.projNote = "" }
            }
        } detail: {
            if let i = rs.projs.firstIndex(where: { pid($0) == rs.proj }) { projectDetail(i) } else { emptyDetail("folder", "No project selected", "Pick one from the list, or make a new one under it.") }
            if !rs.projNote.isEmpty { note(rs.projNote) }
        }.onAppear { if !rs.projs.contains(where: { pid($0) == rs.proj }) { rs.proj = rs.projs.first.map(pid) ?? "" } }
    }

    @ViewBuilder func projectDetail(_ i: Int) -> some View {
        let p = rs.projs[i], id = pid(p), dirs = pstrings(p, "dirs"), repos = pstrings(p, "repos"), opensIn = p["dir"] as? String ?? dirs.first
        let mutate: ((inout [String: Any]) -> Void) -> Void = { fn in
            commitProjects { l in if let idx = l.firstIndex(where: { pid($0) == id }) { fn(&l[idx]) } }
        }
        HStack(spacing: DS.Space.s12) {
            TextField("Project name", text: Binding(get: { pname(p) }, set: { v in mutate { $0["name"] = String(v.prefix(80)) } }))
                .textFieldStyle(.plain).font(sys(size: 20, weight: .bold)).foregroundColor(fg)
            Button("Delete", role: .destructive) { deleteProject(id) }
        }
        box("Terminal") {
            row("New terminals open in", "Empty: the project's first folder.") {
                Picker("New terminals open in", selection: Binding(get: { p["dir"] as? String ?? "" }, set: { v in mutate { $0["dir"] = v.isEmpty ? nil : v } })) {
                    Text("First folder").tag("")
                    ForEach(dirs, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(maxWidth: 260)
            }
        }
        box("Folders", aside: "Full paths on \(hostLabel)") {
            ForEach(dirs, id: \.self) { d in
                pathRow("folder", d) {
                    if d == opensIn { chip("new terminals") }
                    else { Button("Open terminals here") { mutate { $0["dir"] = d } }.buttonStyle(.borderless).font(sys(size: DS.TypeRole.label.size)) }
                    xButton("Remove \(d)") { mutate { $0["dirs"] = pstrings($0, "dirs").filter { $0 != d }; if $0["dir"] as? String == d { $0["dir"] = nil } } }
                }
            }
            ruled(HStack(spacing: 10) {
                TextField("~/src/another-repo", text: $rs.draftDir).textFieldStyle(DSField()).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).onSubmit(addDir)
                    .accessibilityLabel("New folder path")
                Button("Add folder", action: addDir).disabled(rs.draftDir.trimmingCharacters(in: .whitespaces).isEmpty)
            }.padding(.horizontal, DS.Space.s16).padding(.vertical, 10))
        }
        box("Repositories", aside: "\(repos.count) \(repos.count == 1 ? "repo" : "repos")") {
            ForEach(repos, id: \.self) { r in
                pathRow("branch", r) {
                    Button("Open") { c.settingsTab = "Repositories"; rs.adding = false; rs.pick(r) }.buttonStyle(.borderless)
                    xButton("Remove \(r) from this project") { setProject(r, "") }
                }
            }
            if repos.isEmpty { ruled(note("No repositories in this project yet.").padding(DS.Space.s16).frame(maxWidth: .infinity, alignment: .leading)) }
            ruled(HStack {
                Menu("Add a repository…") {
                    ForEach(allRepos.filter { !repos.contains($0) }, id: \.self) { r in
                        Button(r + (projectOf(r).map { " (from \(pname($0)))" } ?? "")) { setProject(r, id) }
                    }
                }.fixedSize()
                Spacer()
            }.padding(.horizontal, DS.Space.s16).padding(.vertical, 10))
        }
    }

    @ViewBuilder var repositories: some View {
        split("Repositories", "Each repo's project, its lifecycle, and the prompts behind the hover card's buttons.",
              add: ("Add repository", { rs.adding = true; rs.draftRepo = ""; rs.draftProj = rs.projs.first.map(pid) ?? ""; rs.projNote = "" }),
              empty: allRepos.isEmpty && !rs.adding && !rs.busy ? ("branch", "No repositories yet", "A repository gets its own lifecycle, rules and prompts here.") : nil) {
            ForEach(allRepos, id: \.self) { r in
                let on = !rs.adding && rs.repo == r
                let hostTag = lookupRepoHost(r, in: rs.repoHosts)?.target == "mac" ? " · Mac" : ""
                listItem(r, (projectOf(r).map(pname) ?? "No project") + hostTag, on: on, tag: on && rs.source == "yours" ? "yours" : nil) { rs.adding = false; rs.pick(r) }
            }
            if allRepos.isEmpty && rs.busy { note("Loading…").padding(DS.Space.s12) }
        } detail: {
            if rs.adding { addRepoForm } else if !rs.repo.isEmpty { repoDetail } else { emptyDetail("branch", "No repository selected", "Pick one from the list, or add one under it.") }
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
                TextField("repo", text: $rs.draftRepo).textFieldStyle(DSField()).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).frame(width: 240).onSubmit(addRepo)
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
            Text(rs.repo).font(ds(DS.TypeRole.strong)).foregroundColor(fg)  // the pane's title is the only 15
            if let path = rs.repoPaths[rs.repo] { note(path) }
        }
        if !rs.projNote.isEmpty { note(rs.projNote) }
        box {
            row("Project", "Opens with the project's folders in the Basement. No project takes it out.") {
                Picker("Project", selection: Binding(get: { proj.map(pid) ?? "" }, set: {
                    if $0 == "+" { setProject(rs.repo, addProject()); c.settingsTab = "Projects" } else { setProject(rs.repo, $0) }
                })) {
                    Text("No project").tag("")
                    ForEach(rs.projs.indices, id: \.self) { j in Text(pname(rs.projs[j])).tag(pid(rs.projs[j])) }
                    Divider()
                    Text("New project…").tag("+")  // "+" is no project's id
                }.labelsHidden().fixedSize()
            }
            row("Run Chief and agents on", repoHostHelp) {
                Picker("Host", selection: Binding(get: { rs.repoHostMode }, set: { rs.setHost($0) })) {
                    Text("Server").tag("server")
                    Text("Mac").tag("mac")
                    Text("Auto").tag("auto")
                }.labelsHidden().pickerStyle(.segmented).fixedSize()
            }
        }
        VStack(alignment: .leading, spacing: DS.Space.s8) {
            Picker("Repository settings", selection: $rs.rtab) {
                Text("Lifecycle").tag("stages"); Text(rs.autoOn.isEmpty ? "Auto-run" : "Auto-run (\(rs.autoOn.count))").tag("auto"); Text("Rules").tag("rules"); Text("Prompts").tag("prompts")
            }.pickerStyle(.segmented).labelsHidden().fixedSize().frame(maxWidth: .infinity)  // centred: tabs, not a setting
            if !rs.source.isEmpty { HStack(alignment: .firstTextBaseline, spacing: DS.Space.s8) {
                Text(own ? "Your own lifecycle: \(greenlightHome)/lifecycles/\(rs.repo.replacingOccurrences(of: "/", with: "__")).md on \(hostLabel)." : "Using the \(rs.source) lifecycle. Saving makes it your own.")
                    .font(sys(size: DS.TypeRole.label.size)).foregroundColor(own ? accent : dim).fixedSize(horizontal: false, vertical: true)
                if own { Button("Use default", action: rs.reset).buttonStyle(.link).font(sys(size: DS.TypeRole.label.size)).disabled(rs.busy) }
            } }
        }
        switch rs.rtab {
        case "auto": autoRun
        case "rules":
            editor($rs.rules, "Rules for \(rs.repo)", height: 260, title: "Rules", aside: "Markdown · every agent on \(rs.repo) follows these")
            saveRow("Save", rs.save)
        case "prompts": prompts
        default: lifecycle
        }
    }

    var repoHostHelp: String {
        switch rs.repoHostMode {
        case "mac": return "Runs Chief and agents on this Mac (for local apps, Xcode, GUI tests)."
        case "auto":
            if rs.repoNeedsMac {
                return "Auto: runs on this Mac" + (rs.repoNeedsMacReason.isEmpty ? "." : " (\(rs.repoNeedsMacReason)).")
            }
            return "Auto: runs on \(hostLabel) (no Mac-only markers detected)."
        default:
            return isLocalHost(host) ? "Runs Chief and agents on this Mac (no remote server set in Hosts)." : "Runs Chief and agents on \(host) (default)."
        }
    }

    func optBinding(_ k: String) -> Binding<Bool> {
        Binding(get: { rs.options[k] != nil }, set: { v in if v { rs.options[k] = "" } else { rs.options[k] = nil } })
    }
    func optText(_ k: String) -> Binding<String> {
        Binding(get: { rs.options[k] ?? "" }, set: { v in
            let s = v.replacingOccurrences(of: "\n", with: "").replacingOccurrences(of: "\r", with: "")
            if s.isEmpty { rs.options[k] = nil } else { rs.options[k] = s }
        })
    }
    func optChoice(_ k: String) -> Binding<String> {
        Binding(get: { (rs.options[k] ?? "").lowercased() }, set: { v in
            if v.isEmpty { rs.options[k] = nil } else { rs.options[k] = v }
        })
    }
    var humanApprovalBinding: Binding<String> {
        Binding(
            get: {
                if let v = rs.options["human_approval"]?.lowercased(), v == "true" || v == "false" { return v }
                return rs.options["no_human_approval"] != nil ? "false" : ""
            },
            set: { v in
                rs.options["no_human_approval"] = nil
                if v.isEmpty { rs.options["human_approval"] = nil } else { rs.options["human_approval"] = v }
            }
        )
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
                    Button("Your own (sign-off, label or check)…") { rs.stages.insert(Stage(key: "stage\(rs.stages.count + 1)", req: "", done: "signoff"), at: max(0, rs.stages.count - 1)) }
                }.fixedSize()
                Spacer()
            }.padding(.horizontal, DS.Space.s16).padding(.vertical, 10))
        }
        box("Options") {
            toggleRow("Solo", "Nobody else reviews this repo: a mergeable MR with no other approval comes to you to review and merge.", optBinding("solo"))
            toggleRow("Merge by itself", "Once every step before the merge passes (an agent's reviews and try-out, say), Greenlight merges without waiting on an approval or asking you.", optBinding("automerge"))
            toggleRow("Merge into any branch", "Greenlight may merge an MR whose target isn't the default branch.", optBinding("merge_any_base"))
            toggleRow("Auto-rebase", "Dispatch an agent to rebase when the forge reports the branch needs a rebase with no conflicts.", optBinding("auto_rebase"))
            row("Merge method", "Overrides your global config.json merge_method for this repository.") {
                Picker("Merge method", selection: optChoice("merge_method")) {
                    Text("Default").tag(""); Text("Merge").tag("merge"); Text("Squash").tag("squash"); Text("Rebase").tag("rebase")
                }.labelsHidden().frame(width: 140)
            }
            row("Human approval", "Overrides your global require_human_approval setting for this repository.") {
                Picker("Human approval", selection: humanApprovalBinding) {
                    Text("Default").tag(""); Text("Required").tag("true"); Text("Not required").tag("false")
                }.labelsHidden().frame(width: 140)
            }
            row("Review bot", "Overrides global review_bot for this repo. Set to none to disable.") {
                TextField("Global default", text: optText("review_bot")).textFieldStyle(DSField()).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).frame(width: 180)
            }
            row("Milestone prefix", "Active milestone prefix for the milestone step (default Release-).") {
                TextField("Release-", text: optText("milestone")).textFieldStyle(DSField()).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).frame(width: 180)
            }
        }
        saveRow("Save", rs.save)
    }

    func stageRow(_ i: Int) -> some View {
        let safeStage: () -> Stage = { i < rs.stages.count ? rs.stages[i] : Stage(key: "", req: "", done: "") }
        let keyBind = Binding(get: { safeStage().key }, set: { if i < rs.stages.count { rs.stages[i].key = $0 } })
        let reqBind = Binding(get: { safeStage().req }, set: { if i < rs.stages.count { rs.stages[i].req = $0 } })
        let doneBind = Binding(get: { safeStage().done }, set: { if i < rs.stages.count { rs.stages[i].done = $0 } })
        let runBind = Binding(get: { safeStage().run }, set: { if i < rs.stages.count { rs.stages[i].run = $0 } })
        let ownerBind = Binding(get: { safeStage().owner }, set: { if i < rs.stages.count { rs.stages[i].owner = $0 } })
        let phaseBind = Binding(get: { safeStage().phase }, set: { if i < rs.stages.count { rs.stages[i].phase = $0 } })
        let cur = safeStage(), builtin = rs.builtins[cur.key] != nil
        let badge = builtin ? "Built-in" : cur.done.hasPrefix("label=") ? "Label" : cur.done.hasPrefix("check=") ? "CI check" : "Your sign-off"
        return ruled(HStack(alignment: .top, spacing: DS.Space.s12) {
            Text("\(i + 1)").font(sys(size: DS.TypeRole.label.size).monospacedDigit()).foregroundColor(dim).frame(width: 18, alignment: .trailing).padding(.top, 5)
            VStack(alignment: .leading, spacing: DS.Space.s4) {
                HStack(spacing: DS.Space.s8) {
                    if builtin { Text(cur.key).font(sys(size: DS.TypeRole.label.size, weight: .semibold, design: .monospaced)).foregroundColor(fg).padding(.top, 4) }
                    else { TextField("key", text: keyBind).textFieldStyle(DSField()).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).frame(width: 180).accessibilityLabel("Stage \(i + 1) key") }
                    Text(badge).font(sys(size: DS.TypeRole.caption.size, weight: .medium))
                        .foregroundColor(builtin ? sub : accent).padding(.horizontal, DS.Space.s8).frame(height: 20)
                        .background(RoundedRectangle(cornerRadius: DS.Radius.xs).fill(builtin ? surface : accent.opacity(0.12))).padding(.top, 2)
                }
                if builtin {  // `code` in the requirement reads as code; a long one opens on More
                    let long = cur.req.count > 160, open = rs.openStages.contains(cur.key)
                    Text((try? AttributedString(markdown: cur.req, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(cur.req))
                        .font(ds(DS.TypeRole.caption)).foregroundColor(dim).lineLimit(long && !open ? 2 : nil).fixedSize(horizontal: false, vertical: true)
                    if long {
                        Button(open ? "Less" : "More") { rs.openStages.formSymmetricDifference([cur.key]) }
                            .buttonStyle(.plain).font(ds(DS.TypeRole.caption)).foregroundColor(Color(accentSet.fg, dark: isDark)).accessibilityLabel(open ? "Show less of stage \(i + 1)" : "Show all of stage \(i + 1)")
                    }
                }
                else {
                    TextField("Requirement", text: reqBind).textFieldStyle(DSField()).accessibilityLabel("Stage \(i + 1) requirement")
                    HStack(spacing: DS.Space.s8) {
                        Picker("Done when", selection: Binding(
                            get: { doneBind.wrappedValue.hasPrefix("label=") ? "label" : doneBind.wrappedValue.hasPrefix("check=") ? "check" : "signoff" },
                            set: { doneBind.wrappedValue = $0 == "label" ? "label=" : $0 == "check" ? "check=" : "signoff" })) {
                            Text("You sign off").tag("signoff"); Text("It has a label").tag("label"); Text("CI check passes").tag("check")
                        }.labelsHidden().frame(width: 150)
                        if doneBind.wrappedValue.hasPrefix("label=") {
                            TextField("label", text: Binding(get: { String(doneBind.wrappedValue.dropFirst(6)) }, set: { doneBind.wrappedValue = "label=" + $0 }))
                                .textFieldStyle(DSField()).frame(width: 160).accessibilityLabel("Stage \(i + 1) label")
                        } else if doneBind.wrappedValue.hasPrefix("check=") {
                            TextField("check name", text: Binding(get: { String(doneBind.wrappedValue.dropFirst(6)) }, set: { doneBind.wrappedValue = "check=" + $0 }))
                                .textFieldStyle(DSField()).frame(width: 160).accessibilityLabel("Stage \(i + 1) check")
                        } else {
                            TextField("Run command (optional)", text: runBind).textFieldStyle(DSField()).font(sys(size: DS.TypeRole.label.size, design: .monospaced))
                                .frame(width: 200).accessibilityLabel("Stage \(i + 1) run command")
                        }
                    }
                    HStack(spacing: DS.Space.s8) {
                        Picker("Owner", selection: ownerBind) {
                            Text("Default owner").tag(""); Text("Agent").tag("agent"); Text("You").tag("me"); Text("Reviewers").tag("reviewers"); Text("CI").tag("ci")
                        }.labelsHidden().frame(width: 140)
                        Picker("Phase", selection: phaseBind) {
                            Text("Default phase").tag(""); Text("Define").tag("define"); Text("Build").tag("build"); Text("Verify").tag("verify"); Text("Review").tag("review"); Text("Land").tag("land")
                        }.labelsHidden().frame(width: 140)
                    }
                }
            }
            Spacer(minLength: 0)
            HStack(spacing: 0) {
                Button { if i > 0 && i < rs.stages.count { rs.stages.swapAt(i, i - 1) } } label: { Image(ds: "chevron-up").frame(width: 24, height: 24).contentShape(Rectangle()) }.disabled(i == 0).accessibilityLabel("Move stage \(i + 1) up")
                Button { if i + 1 < rs.stages.count { rs.stages.swapAt(i, i + 1) } } label: { Image(ds: "chevron-down").frame(width: 24, height: 24).contentShape(Rectangle()) }.disabled(i >= rs.stages.count - 1).accessibilityLabel("Move stage \(i + 1) down")
                Button { if i < rs.stages.count && rs.stages.count > 1 { rs.stages.remove(at: i) } } label: { Image(ds: "close").frame(width: 24, height: 24).contentShape(Rectangle()) }.disabled(rs.stages.count <= 1).padding(.leading, DS.Space.s8).accessibilityLabel("Remove stage \(i + 1)")
            }.buttonStyle(.borderless).foregroundColor(sub).frame(height: 24)
        }.padding(.leading, DS.Space.s12).padding(.trailing, DS.Space.s12).padding(.vertical, 10))
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
            if rs.autoKinds.isEmpty { note(rs.busy ? "Loading…" : "No auto-run jobs from \(hostLabel).").padding(DS.Space.s16) }
        }
    }

    @ViewBuilder var prompts: some View {
        let kindName = RulesStore.kinds.first { $0.0 == rs.kind }?.1 ?? ""
        box {
            HStack(spacing: DS.Space.s12) {
                Picker("Button", selection: Binding(get: { rs.kind }, set: { rs.kind = $0; rs.getPrompt() })) { ForEach(RulesStore.kinds, id: \.0) { Text($0.1).tag($0.0) } }
                    .pickerStyle(.segmented).labelsHidden().fixedSize()
                Spacer(minLength: DS.Space.s8)
                Picker("Prompt", selection: Binding(get: { rs.reviewOwn }, set: { rs.reviewOwn = $0; if $0 && rs.review.isEmpty { rs.review = "{default}\n\n" } })) {
                    Text("Your own").tag(true); Text("Default").tag(false)
                }.pickerStyle(.segmented).labelsHidden().fixedSize()
            }.padding(.horizontal, DS.Space.s12).padding(.vertical, 10)
            VStack(alignment: .leading, spacing: DS.Space.s8) {
                note("What the \(kindName) button hands Claude. Private to you: \(greenlightHome)/prompts/\(rs.repo)/\(rs.kind).md on \(hostLabel), never in the repo. Fills in \(rs.reviewVars.joined(separator: " ")); {default} pulls in the default.")
                if rs.reviewOwn { editor($rs.review, "\(kindName) prompt for \(rs.repo)", height: 240) }
                else {
                    ScrollView { Text(rs.reviewDefault).font(sys(size: DS.TypeRole.label.size, design: .monospaced)).foregroundColor(sub).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                        .padding(DS.Space.s8).frame(height: 240).background(RoundedRectangle(cornerRadius: DS.Radius.sm).fill(surface)).overlay(RoundedRectangle(cornerRadius: DS.Radius.sm).stroke(edge))
                }
            }.padding(DS.Space.s12).overlay(alignment: .top) { Rectangle().fill(line).frame(height: 1) }
        }
        saveRow("Save prompt", rs.saveReview)
    }

    @ViewBuilder var instructions: some View {
        heading("Instructions", "Standing instructions Greenlight reads in the chat and on every run: how to talk to you, what to never do, what to always check. A repo's own rules live in Repositories › Rules.")
        editor($rs.instructions, "Instructions for Greenlight", height: 380, placeholder: "Reply in short sentences.\nNever push to main without asking.")
        saveRow("Save instructions", rs.saveInstructions) { note("Instructions.md on \(hostLabel)") }
    }

    @ViewBuilder var memory: some View {
        split("Memory", "What Greenlight has learned, one note each. It reads them before every run.",
              add: ("New note", { rs.note = ""; rs.noteText = "---\nname: \ndescription: \nmetadata:\n  type: project\n---\n\n" }),
              empty: rs.notes.isEmpty && rs.note.isEmpty && rs.noteText.isEmpty && !rs.busy ? ("bookmark", "No notes yet", "A note is one thing Greenlight has learned. It reads them before every run.") : nil) {
            ForEach(rs.notes, id: \.name) { n in
                listItem(n.name, n.text.split(separator: "\n").first { $0.hasPrefix("description:") }.map { $0.dropFirst(12).trimmingCharacters(in: .whitespaces) } ?? "",
                         on: rs.note == n.name) { rs.note = n.name; rs.noteText = n.text }
            }
            if rs.notes.isEmpty && rs.busy { note("Loading…").padding(DS.Space.s12) }
        } detail: {
            let existing = rs.notes.contains { $0.name == rs.note }
            if rs.note.isEmpty && rs.noteText.isEmpty {
                emptyDetail("bookmark", "No note selected", "Pick one from the list, or start a new one under it.")
            } else {
            if existing { Text(rs.note + ".md").font(ds(DS.TypeRole.titleSm)).foregroundColor(fg) }
            else { TextField("my-decision", text: $rs.note).textFieldStyle(DSField()).frame(width: 260).accessibilityLabel("New note name") }
            editor($rs.noteText, "Note text", height: 340, title: "Note", aside: "Keep the frontmatter: it's the index line.")
            saveRow("Save note", rs.saveNote) {
                Button("Delete note", role: .destructive, action: rs.deleteNote).disabled(rs.busy || !existing)
            }.disabled(rs.note.isEmpty)
            note("Notes live in greenlight's memory folder on \(hostLabel). A note deleted here can come back from this Mac's copy on the next sync.sh.")
            }
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

/// The chat pane (§10.17): a header (name, state, voice knobs), the conversation, the live log, and the composer.
/// Default wears the system tokens; Phosphor the terminal's mono hand.
struct ChatView: View, Looked {
    @ObservedObject var c: Core
    @FocusState private var typing: Bool
    @FocusState private var searching: Bool
    var body: some View {
        VStack(spacing: 0) {
            header
            rule
            ZStack(alignment: .top) {
            Group {  // settings cover the dashboard pane: App.settingsHost
            StageOr(v: c.voice) { StageView(v: c.voice, c: c) } chat: {
            VStack(spacing: 0) {
            ScrollViewReader { sv in
                ScrollView {
                    VStack(alignment: .leading, spacing: DS.Space.s12) {  // 12 inside a turn, 24 between turns
                        ForEach(shown) { m in
                            MsgView(m: m, c: c, name: c.name, ask: c.question(for: m)).padding(.top, m.role == .you && m.id != shown.first?.id ? DS.Space.s12 : 0)
                        }
                        if let r = c.run { RunCard(r: r, c: c) }
                        ForEach(Array(c.queue.enumerated()), id: \.offset) { i, q in
                            HStack { Spacer(minLength: DS.Space.s48); queued([q.display, q.files.map { "📎 \($0.name)" }.joined(separator: "  ")].filter { !$0.isEmpty }.joined(separator: "\n"), i) }
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }.padding(DS.Space.s16)
                }
                .onAppear { DispatchQueue.main.async { sv.scrollTo("bottom", anchor: .bottom) } }  // after layout, or it lands short
                .onChange(of: c.msgs.count) { sv.scrollTo("bottom") }
                .onChange(of: c.run?.steps.count ?? -1) { sv.scrollTo("bottom") }
                .onChange(of: c.run?.live.count ?? -1) { sv.scrollTo("bottom") }
                .onChange(of: c.queue.count) { sv.scrollTo("bottom") }
            }.overlay { if shown.isEmpty && c.run == nil && c.queue.isEmpty { emptyState } }
            if c.verbose { rule; FeedPanel(f: c.feed, c: c) }
            footer  // the composer's own border is its edge: no rule over it
            rule
            footBar
            }.id(c.look)
            .overlay { DropArea(c: c) }  // files dragged anywhere on the chat, the field too
            .overlay { if c.dropping { dropHint } }
            }
            }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(bg)
        .foregroundColor(fg)
        .environment(\.colorScheme, lk.dark ? .dark : .light)  // the look's: Default follows the app, Phosphor is always dark
        .font(ui(DS.TypeRole.body))
        .tint(accent)
    }

    // MARK: the look's chrome (header, rules, queued messages, composer)

    var busy: Bool { c.run != nil }
    /// The conversation without the transcript's greeting note: the empty state says it.
    var shown: [Msg] { c.msgs.filter { !($0.role == .note && $0.text.hasPrefix("Ask me anything")) } }
    /// §10.21: centred, a 20 icon, a title-sm line and a body line.
    var emptyState: some View {
        VStack(spacing: DS.Space.s8) {
            Image(ds: "chat-text", size: 20).foregroundColor(faint).accessibilityHidden(true)
            Text("Ask about your workspaces").font(ui(DS.TypeRole.titleSm))
            Text("\(c.name) answers here. ⌃R searches the prompts you've sent.").foregroundColor(dim).multilineTextAlignment(.center)
        }.padding(DS.Space.s24)
    }
    var chrome: Color { mono ? bg : Color(DS.Palette.bgChrome, dark: lk.dark) }
    var stateWord: String { c.connected ? (busy ? "working" : "idle") : "offline" }
    var inset: CGFloat { DS.Space.s16 }
    /// A chrome label in the look's face: mono for Phosphor, tabular figures, sentence case (§3).
    func tag(_ s: String, _ t: DSType = DS.TypeRole.caption, _ w: Font.Weight? = nil) -> Text {
        Text(s).font(ui(t, w).monospacedDigit())
    }

    var header: some View {
        Group {
            if c.editingName {
                headerRow(showMeta: false, showState: false, fixedTitle: false)
            } else {
                ViewThatFits(in: .horizontal) {  // narrow pane drops host, then state tag, before truncating the assistant name
                    headerRow(showMeta: true, showState: true, fixedTitle: true)
                    headerRow(showMeta: false, showState: true, fixedTitle: true)
                    headerRow(showMeta: false, showState: false, fixedTitle: true)
                    headerRow(showMeta: false, showState: false, fixedTitle: false)
                }
            }
        }.padding(.leading, inset).padding(.trailing, DS.Space.s12).frame(height: DS.Layout.barHeight - 1)  // with its rule, the bar's 44: one hairline across the window
            .background(chrome)
    }

    @ViewBuilder func headerRow(showMeta: Bool, showState: Bool, fixedTitle: Bool) -> some View {
        HStack(spacing: DS.Space.s8) {
            HStack(spacing: DS.Space.s8) { title(showState: showState) }.fixedSize(horizontal: fixedTitle, vertical: false)
            if showMeta { HStack(spacing: DS.Space.s8) { meta }.fixedSize() }  // beside the name: where it runs
            Spacer(minLength: DS.Space.s8)
            HStack(spacing: DS.Space.s4) {
                newChat
                if c.session == 0 { VoiceButtons(v: c.voice, c: c) }  // voice and its own window: the main chat's
            }
        }
    }

    /// The name as the look prints it; double-click to rename (Return or click away saves). Only the display changes, never c.name.
    @ViewBuilder func nameView(_ shown: String, _ f: Font) -> some View {
        if c.editingName {
            TextField("Name", text: $c.name, onEditingChanged: { if !$0 { c.doneNaming() } }).textFieldStyle(.plain).font(f).frame(width: 160).onSubmit(c.doneNaming)
        } else {
            Text(shown).font(f).lineLimit(1).truncationMode(.tail).layoutPriority(1).onTapGesture(count: 2) { c.editingName = true }.help("Double-click to rename")
        }
    }

    /// Who and how it is: the name (strong) and its state as glyph and word (working, offline; idle says nothing in colour).
    /// Phosphor: a prompt, name@greenlight ~/chat.
    @ViewBuilder func title(showState: Bool = true) -> some View {
        let n = c.name
        if mono {
            nameView("\(n.lowercased())@greenlight", ds(DS.TypeRole.body, mono: true))
            Text("~/chat").foregroundColor(dim)
        } else {
            nameView(n, ds(DS.TypeRole.titleSm))
            if showState && (busy || !c.connected) {  // idle says nothing
                Label { Text(stateWord.capitalized) } icon: { Image(ds: c.connected ? "working" : "crashed", size: iconSize(DS.TypeRole.label.size)) }
                    .font(ds(DS.TypeRole.label)).foregroundColor(c.connected ? working : crashedInk).lineLimit(1).fixedSize()
                    .accessibilityLabel(stateWord)
            }
        }
    }

    /// Host and tok/s at the right; Phosphor adds its [state].
    @ViewBuilder var meta: some View {
        let h = hostBadgeName(c.chatHost)
        if mono {
            Text("\(h)\(tps)").foregroundColor(dim).lineLimit(1)
            Text("[\(stateWord)]").fontWeight(busy ? .bold : .regular).foregroundColor(c.connected ? (busy ? fg : dim) : crashedInk)
        } else { tag("\(h)\(tps)").foregroundColor(dim).lineLimit(1) }
    }

    /// The hairline between header, list and footer (§10.1: one side draws it); Phosphor dashes it.
    @ViewBuilder var rule: some View {
        if mono { HLine().stroke(line, style: StrokeStyle(lineWidth: 1, dash: [4, 3])).frame(height: 1) }
        else { Rectangle().fill(line).frame(height: 1) }
    }

    /// A message waiting for the run to finish: your bubble, dashed, and labelled Queued.
    @ViewBuilder func queued(_ q: String, _ i: Int) -> some View {
        let dash = StrokeStyle(lineWidth: 1, dash: [4, 3])
        let text = mono ? Text("\(Text("you> ").foregroundColor(dim))\(Text(q))") : Text(q)
        VStack(alignment: .trailing, spacing: DS.Space.s4) {
            text.font(chatFace(size: DS.TypeRole.bodyCopy.size)).foregroundColor(mono ? youInk : fg)  // full strength; the dashes and the label say queued
                .multilineTextAlignment(.trailing).padding(.horizontal, mono ? 0 : DS.Space.s12).padding(.vertical, mono ? 0 : DS.Space.s8)
                .background { if !mono { RoundedRectangle(cornerRadius: DS.Radius.md).stroke(edge, style: dash) } }
            tag(mono ? "(queued)" : "Queued · sends after this run").foregroundColor(dim)
        }
    }

    /// ⌃R: fuzzy search over the prompts you sent; with no query, the recent ones.
    var histPanel: some View {
        let ms = c.hmatches, q = (c.hq ?? "").trimmingCharacters(in: .whitespaces)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: DS.Space.s8) {
                Image(ds: "search").foregroundColor(dim).accessibilityHidden(true)
                TextField("Search prompt history", text: Binding(get: { c.hq ?? "" }, set: { c.hq = $0; c.hsel = 0 }))
                    .textFieldStyle(.plain).focused($searching).onAppear { searching = true }
                if !q.isEmpty { Text("\(ms.count) match\(ms.count == 1 ? "" : "es")").font(ui(DS.TypeRole.caption).monospacedDigit()).foregroundColor(dim) }
            }.padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s8)
            rule
            ScrollViewReader { sv in
                ScrollView {
                    VStack(alignment: .leading, spacing: DS.Space.s2) {
                        if q.isEmpty && !ms.isEmpty { Text("Recent").font(ui(DS.TypeRole.label)).foregroundColor(dim).padding(.horizontal, DS.Space.s8).padding(.top, DS.Space.s4) }
                        ForEach(Array(ms.enumerated()), id: \.offset) { i, m in
                            Button { c.useHist(m.s.t) } label: {
                                HStack(spacing: DS.Space.s12) {
                                    Text(marked(m.s.t, m.pos)).lineLimit(1).truncationMode(.tail).foregroundColor(sub)
                                    Spacer(minLength: DS.Space.s4)
                                    Text(ago(m.s.at)).font(ui(DS.TypeRole.caption).monospacedDigit()).foregroundColor(dim)
                                }.padding(.horizontal, DS.Space.s8).padding(.vertical, DS.Space.s8).contentShape(Rectangle())
                                    .background(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.sm).fill(i == c.hsel ? (mono ? card : Color(DS.Palette.accentSubtle, dark: lk.dark)) : .clear))
                            }.buttonStyle(.plain).id(i)
                        }
                        if ms.isEmpty { Text(c.sent.isEmpty ? "Nothing sent yet" : "No prompts match").foregroundColor(dim).padding(DS.Space.s12) }
                    }.padding(DS.Space.s4)
                }.frame(maxHeight: 220)
                    .onChange(of: c.hsel) { sv.scrollTo(c.hsel) }
            }
            rule
            Text("↑↓ move · ↵ put in prompt · ⌃R ⌃S older · newer · esc back to your draft").font(ui(DS.TypeRole.caption)).foregroundColor(dim)
                .padding(.horizontal, DS.Space.s12).padding(.vertical, DS.Space.s8)
        }.background(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.md).fill(mono ? page : Color(DS.Palette.bgRaised, dark: lk.dark)))
            .overlay(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.md).stroke(line))
    }

    func marked(_ t: String, _ pos: [Int]) -> AttributedString {
        let at = Set(pos)
        var a = AttributedString()
        for (i, ch) in t.prefix(300).enumerated() {
            var s = AttributedString(ch.isNewline ? " " : String(ch))
            if at.contains(i) { s.foregroundColor = fg; s.font = ui(DS.TypeRole.strong) }
            a += s
        }
        return a
    }

    var tps: String { c.run != nil && c.tps > 0 ? " · \(Int(c.tps)) tok/s" : c.lastTps > 0 ? " · last \(Int(c.lastTps)) tok/s" : "" }

    /// A file or MR waiting to go with your next message: a secondary chip (§10.5), radius sm.
    func chipBox<V: View>(_ v: V) -> some View {
        v.foregroundColor(mono ? fg : sub).padding(.leading, DS.Space.s8).padding(.trailing, DS.Space.s4).frame(height: DS.Layout.controlHeightSm)
            .background(RoundedRectangle(cornerRadius: corner).fill(card))
    }

    var footer: some View {
        VStack(alignment: .leading, spacing: DS.Space.s8) {
            if !c.attached.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: DS.Space.s8) {
                        ForEach(c.attached) { a in
                            chipBox(HStack(spacing: DS.Space.s4) {
                                Image(ds: "hex-fill", size: iconSize(9)).foregroundColor(a.color)  // the MR's state colour, as on the board
                                tag(a.label, DS.TypeRole.labelSm).lineLimit(1)
                                Button { c.attached.removeAll { $0.id == a.id }; c.unattach?(a.id) } label: {
                                    Image(ds: "close", size: iconSize(9), medium: true).foregroundColor(dim).frame(width: 16, height: 16)
                                }.buttonStyle(.plain).accessibilityLabel("Remove \(a.label) from the chat")
                            }).help(a.sent ? "Already sent to \(c.name)" : "Goes to \(c.name) with your next message")
                        }
                    }
                }
            }
            if !c.files.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: DS.Space.s8) { ForEach(c.files) { fileChip($0) } }
                }
            }
            DwimToastBar(c: c, l: lk).animation(.easeOut(duration: DS.Duration.fast), value: c.toast)
            if !c.fileNote.isEmpty {
                Label { Text(c.fileNote) } icon: { Image(ds: "needs", size: iconSize(DS.TypeRole.caption.size)) }.font(ui(DS.TypeRole.caption)).foregroundColor(needsInk)
                    .fixedSize(horizontal: false, vertical: true).accessibilityLabel("Not attached: \(c.fileNote)")
            }
            if c.hq != nil { histPanel }
            VStack(spacing: 0) {  // §10.17: the text full width, its controls in a row under it
            HStack(alignment: .bottom, spacing: DS.Space.s4) {
                if let since = c.recording {
                    HStack(spacing: DS.Space.s2) {
                        ForEach(c.levels.indices, id: \.self) { i in
                            Capsule().fill(crashed).frame(width: 2, height: 3 + CGFloat(c.levels[i]) * 19)
                        }
                    }.frame(height: 38).accessibilityHidden(true)  // two lines of text: the box keeps its height
                    Text("Listening… " + fmt(Date().timeIntervalSince(since))).font(ui(DS.TypeRole.label).monospacedDigit()).foregroundColor(dim)
                    Spacer(minLength: 0)
                } else {
                    let ph = c.transcribing ? "Transcribing…" : c.run != nil ? "Queue a follow-up…" : c.attached.isEmpty ? greeting
                        : "Ask about \(c.attached.count == 1 && c.attached[0].mrs == 1 ? c.attached[0].label : "these \(c.attached.reduce(0) { $0 + $1.mrs }) MRs")…"
                    TextField("Message", text: $c.input, prompt: Text(ph).foregroundColor(mono ? dim : faint), axis: .vertical)
                        .textFieldStyle(.plain).font(chatFace(size: DS.TypeRole.bodyCopy.size))
                        .lineLimit(2...10).padding(.vertical, DS.Space.s8).focused($typing)  // two lines to write in over the controls' row, growing to ten
                        .padding(.leading, mono ? 18 : DS.Space.s4).overlay(alignment: .topLeading) { promptGlyph }
                        .onKeyPress(.return, phases: .down) { k in
                            if k.modifiers.contains(.shift) {  // newline at the cursor
                                NSApp.sendAction(#selector(NSResponder.insertNewlineIgnoringFieldEditor(_:)), to: nil, from: nil)
                            } else { c.submit() }
                            return .handled
                        }
                    if c.vim && c.vimMode != "normal" {  // normal mode shows nothing
                        Text(c.vimMode).font(ui(DS.TypeRole.caption).italic()).foregroundColor(dim).padding(.bottom, DS.Space.s8)
                    }
                }
            }.padding(.horizontal, DS.Space.s4)
            HStack(spacing: DS.Space.s4) {
                if c.recording == nil { attachButton }
                Spacer(minLength: 0)
                let rec = c.recording != nil, pad = padSize
                Button(action: c.toggleVoice) {
                    Group {
                        if c.transcribing { ProgressView().controlSize(.small).scaleEffect(0.7) }
                        else if mono { Text(rec ? "[stop]" : "[mic]").padding(.horizontal, DS.Space.s4) }
                        else { Image(ds: rec ? "stop" : "mic", size: iconSize(DS.TypeRole.body.size)) }
                    }.frame(minWidth: pad).frame(height: pad)
                        .background(RoundedRectangle(cornerRadius: corner).fill(rec ? crashed : .clear))  // quiet until it records
                        .foregroundColor(rec ? Color(DS.Palette.statusCrashedOnSolid, dark: lk.dark) : mono ? fg : dim)
                }.buttonStyle(.plain).disabled(c.transcribing)
                    .modifier(Tip(c: c, text: c.recording != nil ? "Stop and transcribe (\(c.shortcut))" : "Speak to \(c.name) (\(c.shortcut))"))
                    .accessibilityLabel(c.recording != nil ? "Stop recording" : "Speak")
                let empty = (c.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && c.files.isEmpty) || c.reading > 0  // submit() ignores it anyway
                Button(action: c.submit) {
                    sendLabel.frame(minWidth: pad).frame(height: pad)
                        .background(RoundedRectangle(cornerRadius: corner).fill(empty ? .clear : mono ? fg : accent))
                        .overlay(RoundedRectangle(cornerRadius: corner).stroke(mono && empty ? line : .clear))
                        .foregroundColor(empty ? faint : mono ? page : onAccent)
                }.buttonStyle(.plain).disabled(empty).accessibilityLabel("Send")
            }
            }
            .padding(DS.Space.s4)
            .background { composerBox }
            .onChange(of: c.hq == nil) { if c.hq == nil { typing = true } }  // ⌃R closed: back to the field
            if c.hp >= 0 {
                Text("History \(c.hp + 1) of \(c.walk.count)\(c.hsaved.isEmpty ? "" : " starting “\(c.hsaved.prefix(32))”") · ↑ older · ↓ newer\(c.vim ? "" : " · esc back to your draft")").font(ui(DS.TypeRole.caption)).foregroundColor(dim)
            }
        }.padding(.horizontal, inset).padding(.top, DS.Space.s12).padding(.bottom, DS.Space.s12)
    }

    /// The pane's footer (§10.11): the model on the left margin, the context on the right, on the dashboard footer's line.
    var footBar: some View {
        HStack(spacing: DS.Space.s8) {
            modelButton
            Spacer(minLength: DS.Space.s4)
            ContextButton(c: c).fixedSize()
        }.font(ui(DS.TypeRole.caption).monospacedDigit()).controlSize(.small)
            .padding(.horizontal, inset - DS.Space.s8)  // the buttons' own 8: their text sits on the pane's margins
            .frame(height: DS.Layout.footerHeight - 1).background(chrome)
    }

    /// The composer's +: attach files (the open panel). Drops and ⌘V attach too.
    var attachButton: some View {
        let pad = padSize
        return Button { c.pickFiles(NSApp.keyWindow) } label: {
            Group {
                if mono { Text("[+]").padding(.horizontal, DS.Space.s4) }
                else { Image(ds: "attach", size: iconSize(DS.TypeRole.body.size)) }
            }.frame(minWidth: pad).frame(height: pad).foregroundColor(mono ? fg : dim).contentShape(Rectangle())
        }.buttonStyle(.plain)
            .modifier(Tip(c: c, text: "Attach files: \(ChatFile.supported). Or drop them here, or paste."))
            .accessibilityLabel("Attach files")
    }
    /// A file waiting to go with the next message: its thumbnail or icon, name and size, and × to take it off.
    func fileChip(_ f: ChatFile) -> some View {
        chipBox(HStack(spacing: DS.Space.s4) {
            if let t = f.thumb { Image(nsImage: t).resizable().aspectRatio(contentMode: .fill).frame(width: 18, height: 18).clipShape(RoundedRectangle(cornerRadius: DS.Radius.xs)) }
            else { Image(ds: "file-text", size: iconSize(DS.TypeRole.label.size)).foregroundColor(dim) }
            tag(f.name, DS.TypeRole.labelSm).lineLimit(1).truncationMode(.middle).frame(maxWidth: 150, alignment: .leading)
            tag(byteSize(f.data.count)).foregroundColor(dim).lineLimit(1).fixedSize()
            Button { c.files.removeAll { $0.id == f.id } } label: {
                Image(ds: "close", size: iconSize(9), medium: true).foregroundColor(dim).frame(width: 16, height: 16)
            }.buttonStyle(.plain).accessibilityLabel("Remove \(f.name)")
        })
            .help("\(f.kindName), goes to \(c.name) with your next message")
            .accessibilityElement(children: .contain).accessibilityLabel("Attached \(f.name), \(byteSize(f.data.count))")
    }
    /// Files dragged over the chat: where they'll go.
    var dropHint: some View {
        RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.lg).fill(bg.opacity(0.9))
            .overlay(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.lg).stroke(accent, style: StrokeStyle(lineWidth: 2, dash: [6, 4])))
            .overlay(VStack(spacing: DS.Space.s8) {
                Image(ds: "attach", size: iconSize(DS.TypeRole.title.size)).foregroundColor(link)
                Text("Drop to attach").font(ui(DS.TypeRole.titleSm))
                Text(ChatFile.supported.prefix(1).uppercased() + ChatFile.supported.dropFirst()).font(ui(DS.TypeRole.caption)).foregroundColor(dim)
            })
            .padding(DS.Space.s12).allowsHitTesting(false).accessibilityHidden(true)
    }

    /// Corners for the look's controls: radius sm (Default), square (Phosphor).
    var corner: CGFloat { mono ? 0 : DS.Radius.sm }
    var padSize: CGFloat { DS.Layout.controlHeightMd }
    var greeting: String { mono ? "talk to \(c.name.lowercased())_" : "Talk to \(c.name)…" }

    /// Phosphor leads the line you type with a shell prompt.
    @ViewBuilder var promptGlyph: some View {
        if mono { Text("$").foregroundColor(typing ? fg : dim).padding(.top, DS.Space.s8).accessibilityHidden(true) }
    }
    /// The composer's frame (§10.4): the input well with a 1px border, the accent ring on focus; walking the history (↑↓)
    /// thickens it. Phosphor: a dashed box.
    @ViewBuilder var composerBox: some View {
        let hot = typing || c.hp >= 0, walk: CGFloat = c.hp >= 0 ? 1 : 0
        if mono {
            Rectangle().fill(hot ? card : .clear).overlay(Rectangle().stroke(hot ? fg : line, style: StrokeStyle(lineWidth: 1 + walk, dash: [4, 3])))
        } else {
            let r = RoundedRectangle(cornerRadius: DS.Radius.md)
            r.fill(Color(lk.dark ? DS.Palette.elementRest : DS.Palette.bgSurface, dark: lk.dark))  // light: the text background, grey reads as disabled
                .overlay(r.stroke(hot ? Color(DS.Palette.accentRing, dark: lk.dark) : edge, lineWidth: 1 + walk))
        }
    }
    @ViewBuilder var sendLabel: some View {
        if mono { Text("[ send ]").padding(.horizontal, DS.Space.s4) }
        else { Image(ds: "arrow-up", size: iconSize(DS.TypeRole.body.size), medium: true) }
    }

    /// Model and effort, as a quiet button; opens Settings.
    var modelButton: some View {
        let m = c.modelName, e = c.supportsEffort ? effortName(c.effort) : nil
        let label = mono ? "--model \(m.lowercased().replacingOccurrences(of: " ", with: "-"))" + (e.map { " --effort \($0.lowercased())" } ?? "") : m + (e.map { " · \($0)" } ?? "")
        return Button { c.settingsOpen = true } label: {
            HStack(spacing: DS.Space.s4) {
                tag(label).lineLimit(1).truncationMode(.middle)  // after the hint, the model gives way
                if !mono { Image(ds: "chevron-up-down", size: iconSize(9), medium: true) }
            }.foregroundColor(mono ? fg : dim)
                .padding(.horizontal, DS.Space.s8).frame(height: DS.Layout.controlHeightSm).contentShape(Rectangle())
        }.buttonStyle(.plain).fixedSize(horizontal: false, vertical: true).modifier(Tip(c: c, text: "Change model and effort in Settings"))
    }

    static let newTip = "New chat — forget this conversation and start fresh"

    var newChat: some View {
        let hot = c.hint == Self.newTip
        return Button(action: { c.clear() }) {
            Image(ds: "compose", size: iconSize(DS.TypeRole.body.size)).frame(width: padSize, height: padSize)
                .foregroundColor(hot || mono ? fg : dim)
                .background(RoundedRectangle(cornerRadius: corner).fill(hot ? card : .clear))
        }.buttonStyle(.plain).modifier(Tip(c: c, text: Self.newTip)).accessibilityLabel("New chat")
    }
}

/// One "Context N%" control; its popover has the token totals and Compact. Neutral until the window is nearly full, then
/// status.needs with its glyph.
struct ContextButton: View, Looked {
    @ObservedObject var c: Core
    var body: some View {
        let full = c.ctxFull, high = full > 0.75
        return Button { c.ctxOpen.toggle() } label: {
            HStack(spacing: DS.Space.s4) {
                if high { Image(ds: "needs", size: iconSize(DS.TypeRole.caption.size)) }
                else {
                    ZStack {
                        Circle().stroke(line, lineWidth: 2)
                        Circle().trim(from: 0, to: max(full, 0.02)).stroke(dim, style: StrokeStyle(lineWidth: 2, lineCap: .round)).rotationEffect(.degrees(-90))
                    }.frame(width: 12, height: 12)
                }
                Text(mono ? "ctx \(c.ctxShort)" : "Context \(c.ctxShort)").lineLimit(1).fixedSize().font(ui(DS.TypeRole.caption).monospacedDigit())
            }.foregroundColor(high ? needsInk : mono ? fg : dim).padding(.horizontal, DS.Space.s8).frame(height: DS.Layout.controlHeightSm)
                .background(RoundedRectangle(cornerRadius: mono ? 0 : DS.Radius.sm).fill(c.ctxOpen ? card : .clear)).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("Context, \(c.ctxSpoken)").modifier(CtxPop(c: c))
    }
}

/// The context popover: token totals and Compact. On the chat's Context button and each voice look's readout.
struct CtxPop: ViewModifier, Looked {
    @ObservedObject var c: Core
    func body(content: Content) -> some View {
        let can = c.run == nil && c.isClaude && c.context > 0, full = c.ctxFull, high = full > 0.75
        return content.popover(isPresented: $c.ctxOpen, arrowEdge: .top) {
                VStack(alignment: .leading, spacing: DS.Space.s8) {
                    Text("Context").font(ui(DS.TypeRole.titleSm))
                    Text(c.window > 0 ? "\(kfmt(c.context)) of \(kfmt(c.window)) tokens used" : "\(kfmt(c.context)) tokens used")
                        .font(ui(DS.TypeRole.label).monospacedDigit()).foregroundColor(high ? needsInk : sub)
                    if c.window > 0 { ProgressView(value: full).tint(high ? needs : dim) }
                    Button { c.compact(); c.ctxOpen = false } label: { Label { Text("Compact") } icon: { Image(ds: "collapse") } }.disabled(!can)
                    Text(!c.isClaude ? "Only Claude can compact." : c.run != nil ? "Wait for the reply to finish." : "Summarizes the chat to free context.")
                        .font(ui(DS.TypeRole.caption)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
                }.padding(DS.Space.s12).frame(width: 240)
                    // in the look, not the window's appearance: the look's ink on the look's stage (it opens from the live looks too)
                    .font(ui(DS.TypeRole.body)).foregroundColor(fg)
                    .environment(\.colorScheme, lk.dark ? .dark : .light).presentationBackground(mono ? page : Color(DS.Palette.bgRaised, dark: lk.dark))
            }
    }

}
extension Core {
    var ctxFull: Double { window > 0 ? min(Double(context) / Double(window), 1) : 0 }
    /// The readout: "27%", or the tokens ("35k") while the window is unknown (a harness that doesn't say, or not yet).
    var ctxShort: String { window > 0 ? "\(Int((ctxFull * 100).rounded()))%" : kfmt(context) }
    var ctxSpoken: String { window > 0 ? "\(Int((ctxFull * 100).rounded())) percent used" : "\(kfmt(context)) tokens used" }
}

