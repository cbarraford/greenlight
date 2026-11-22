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
                VStack(spacing: 4) { ForEach(c.toasts) { t in row(t) } }
            }.frame(maxHeight: 160)
        }
    }
    private func row(_ t: DwimToast) -> some View {
            HStack(spacing: 10) {
                if t.pending { ProgressView().controlSize(.mini) }
                else { Image(systemName: t.failed ? "exclamationmark.triangle.fill" : "checkmark.circle.fill").font(.system(size: 12))
                    .foregroundColor(t.failed ? hx(l.dark ? "FF9F0A" : "9A6700") : hx(l.accent)) }
                Text(t.pending ? "\(t.text) in \(Int(Core.launchGrace)) s" : t.text).font(.system(size: 12, weight: .medium)).foregroundColor(hx(l.text))
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if t.undoable { action(t) }
            }.padding(.horizontal, 12).padding(.vertical, 8).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8).fill(hx(l.card))).overlay(RoundedRectangle(cornerRadius: 8).stroke(hx(l.line)))
                .accessibilityElement(children: .contain).accessibilityLabel(t.text)
                .transition(.move(edge: .bottom).combined(with: .opacity))
    }
    @ViewBuilder private func action(_ t: DwimToast) -> some View {
        let button = Button(t.undoing ? "Undoing…" : t.pending ? "Cancel" : "Undo") { c.undoToast(t.id) }.buttonStyle(.plain)
            .font(.system(size: 12, weight: .semibold)).foregroundColor(hx(l.accent)).disabled(t.undoing)
            .help(t.pending ? "Don't start it" : "Put it back (⇧⌘Z)")
        if c.latestUndoableToast?.id == t.id { button.keyboardShortcut("z", modifiers: [.command, .shift]) }
        else { button }
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
            HStack(spacing: Theme.sp2) {
                Button { f.open.toggle() } label: {
                    HStack(spacing: Theme.sp1) {
                        Image(systemName: f.open ? "chevron.down" : "chevron.right").font(sys(size: Theme.glyph, weight: .bold)).frame(width: 10)
                        Circle().fill(live ? accent : dim.opacity(0.5)).frame(width: 6, height: 6).accessibilityHidden(true)
                        Text("Live log").font(sys(size: Theme.fs1, weight: .semibold))
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).foregroundColor(sub).accessibilityLabel(f.open ? "Fold the live log" : "Open the live log")
                if f.open {
                    ForEach([nil] + FeedLine.Src.allCases.map { Optional($0) }, id: \.self) { s in
                        Button { f.only = s } label: {
                            Text(s?.rawValue ?? "All").font(sys(size: Theme.fs1, weight: f.only == s ? .semibold : .regular))
                                .foregroundColor(f.only == s ? fg : dim).padding(.horizontal, 6).padding(.vertical, 2)
                                .background(RoundedRectangle(cornerRadius: Theme.r1).fill(f.only == s ? fg.opacity(0.08) : .clear))
                        }.buttonStyle(.plain).help(s == nil ? "Everything" : s == .chief ? "\(c.name)'s own turn: calls, their output, thinking" : s == .agents ? "Your workspaces' agents" : "Greenlight's moves and the dashboard's buttons")
                    }
                    Spacer(minLength: 0)
                    Button { f.paused.toggle() } label: { Image(systemName: f.paused ? "play.fill" : "pause.fill").font(sys(size: Theme.glyph)) }
                        .buttonStyle(.plain).foregroundColor(dim).help(f.paused ? "Follow the newest line" : "Stop following, to read").accessibilityLabel(f.paused ? "Follow" : "Pause following")
                } else if let l = lines.last {
                    row(l, limit: 1).frame(maxWidth: .infinity, alignment: .leading)
                } else { Spacer(minLength: 0) }
                Button("Clear") { f.clear() }.buttonStyle(.plain).font(sys(size: Theme.fs1)).foregroundColor(dim).disabled(f.lines.isEmpty)
            }.padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp1 + 2)
            if f.open {
                ScrollViewReader { sv in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 3) {
                            if lines.isEmpty {
                                Text(f.only == nil ? "Nothing yet. \(c.name)'s calls, your agents and Greenlight's moves show here as they happen." : "Nothing from \(f.only!.rawValue) yet.")
                                    .font(sys(size: Theme.fs1)).foregroundColor(dim)
                            }
                            ForEach(lines) { l in
                                row(l, limit: f.unfolded.contains(l.id) ? nil : l.kind == .think ? 3 : 6)
                                    .onTapGesture { if f.unfolded.contains(l.id) { f.unfolded.remove(l.id) } else { f.unfolded.insert(l.id) } }
                            }
                            Color.clear.frame(height: 1).id("end")
                        }.padding(.horizontal, Theme.sp3).padding(.bottom, Theme.sp2)
                    }.frame(height: 190)
                        .onAppear { sv.scrollTo("end", anchor: .bottom) }
                        .onChange(of: lines.last?.id) { if !f.paused { sv.scrollTo("end", anchor: .bottom) } }
                        .onChange(of: lines.last?.text.count) { if !f.paused { sv.scrollTo("end", anchor: .bottom) } }  // thinking grows in place
                }
            }
        }.background(fg.opacity(0.035)).textSelection(.enabled)
    }
    func row(_ l: FeedLine, limit: Int?) -> some View {
        let (m, ink) = feedMark(l.kind, accent: accent, dim: dim, failed: crashed, agent: working, move: done)
        let who = l.src == .chief ? accent : l.src == .agents ? working : done
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(feedClock.string(from: l.ts)).foregroundColor(dim.opacity(0.8))
            Text(m).foregroundColor(ink).frame(width: 12, alignment: .center)
            Text("\(Text(l.who).foregroundColor(who).bold()) \(Text(l.text).foregroundColor(l.kind == .think || l.kind == .status ? dim : l.kind == .failed ? crashed : fg).italic(l.kind == .think))")
                .lineLimit(limit).truncationMode(.tail).fixedSize(horizontal: false, vertical: true)
        }.font(sys(size: Theme.fs1, design: .monospaced)).accessibilityElement(children: .combine)
    }
}

/// Verbose activity in a voice look: the log's newest lines under the live line, one line each, in the look's colors.
struct FeedTail: View {
    @ObservedObject var f: Feed
    let n: Int, fg: Color, dim: Color, accent: Color, size: CGFloat
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if f.lines.isEmpty { Text("Live log: waiting for activity").foregroundColor(dim) }
            ForEach(f.lines.suffix(n)) { l in
                let (m, ink) = feedMark(l.kind, accent: accent, dim: dim, failed: hx("E5392F"), agent: accent, move: accent)
                Text("\(Text(m).foregroundColor(ink)) \(Text(l.who).foregroundColor(fg).bold()) \(Text(l.text.split(separator: "\n").first.map(String.init) ?? "").foregroundColor(l.kind == .think || l.kind == .status ? dim : fg))")
                    .lineLimit(1).truncationMode(.tail)
            }
        }.font(sys(size: size, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).accessibilityElement(children: .combine)
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
    var activity: some View { Activity(c: c, r: r, fg: fg, dim: dim, accent: accent, size: Theme.fs2) }
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
                Text(stats).font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(dim).lineLimit(2)
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
                    if lk.id != "onair" { Text(stats).font((lk.id == "hud" ? code(Theme.fs1) : ui(Theme.fs2)).monospacedDigit()).tracking(lk.id == "hud" ? 1 : 0).foregroundColor(dim).lineLimit(2) }
                }
                Spacer(minLength: 4)
                stop
            }
        }
    }
    /// Orb's lead: the amber orb with two soft rings. ponytail: static; the live stage's orb is the one that moves.
    var orbMark: some View {
        ZStack {
            Circle().fill(accent.opacity(0.07)).frame(width: 36, height: 36)  // the rings stay inside the card
            Circle().fill(accent.opacity(0.16)).frame(width: 28, height: 28)
            Circle().fill(RadialGradient(colors: [hx("FFD27A"), hx("F5A524"), hx("B4740E")], center: UnitPoint(x: 0.35, y: 0.35), startRadius: 0, endRadius: 11)).frame(width: 20, height: 20)
        }.frame(width: 36, height: 36).accessibilityHidden(true)
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
        let rest = unmood(staged(r.live).text)
        let t = Text("\(mono ? Text("\(c.name.lowercased())> ").foregroundColor(dim) : Text(""))\(Text(md(rest)))\(cur)")
            .font(chatFace(size: fs)).textSelection(.enabled)
        Group {
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
    }
    var stats: String {
        let n = r.steps.count, t = max(0, Int(c.now.timeIntervalSince(r.start))), tps = c.tps > 0 ? " · \(Int(c.tps)) tok/s" : ""
        let b = byline(r, now: c.now, lead: false), by = b.isEmpty ? "" : " · " + b  // the plan, time left, what just finished
        // ponytail: HUD's step number isn't lit apart from the rest of the line
        if lk.id == "hud" { return "STEP \(n == 0 ? "--" : String(format: "%02d", n)) · T+\(String(format: "%02d:%02d", t / 60, t % 60))\(by.uppercased())\(tps.uppercased())" }
        // the byline before time and speed where the line may wrap (the card is narrow, and those two are on the live line under
        // it too); last on On Air's one-line tag, so the clock isn't what's cut
        let clock = " · \(fmt(TimeInterval(t)))\(lk.id == "conversation" ? " elapsed" : "")\(tps)", step = n == 0 ? "No steps yet" : "Step \(n)"
        let s = lk.id == "onair" ? step + clock + by : step + by + clock
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
            Text(m.text).font(ui(Theme.fs1)).foregroundColor(dim).textSelection(.enabled).frame(maxWidth: .infinity)
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
            Text("\(Text("you> ").foregroundColor(dim))\(t)").font(chatFace(size: fs)).foregroundColor(fg).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
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
        box(unmood(said))
    }
    @ViewBuilder func box(_ said: String) -> some View {
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

/// Instant hover hint by the cursor (App.showTab), in place of the native tooltip: that one waits ~1 s, shows only while
/// Greenlight is the front app, and would double this one. VoiceOver reads the text as the control's hint.
/// A click that changes what the control does (captions on, the next style) updates the hint in place; one that removes the
/// control (voice mode off) drops it, as its hover-exit never comes.
struct Tip: ViewModifier {
    @ObservedObject var c: Core
    let text: String
    func body(content: Content) -> some View {
        content.accessibilityHint(text).onHover { c.hint = $0 ? text : (c.hint == text ? "" : c.hint) }
            .onChange(of: text) { old, new in if c.hint == old { c.hint = new } }
            .onDisappear { if c.hint == text { c.hint = "" } }
    }
}
/// The context readout's hover hint: how full it is, and that a click opens the totals and Compact.
func ctxTip(_ c: Core) -> String { "Context: \(c.ctxSpoken). Click for the totals and Compact" }

/// The bell's panel, a triage list: one line per waiting question, the open one (Core.inboxFocus) expanded with its choices,
/// then the last few answered, and a footer of keys and Clear all.
struct Inbox: View {
    @ObservedObject var c: Core
    var body: some View {
        let w = c.waiting, open = c.inboxFocus?.id
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: Theme.sp2) {
                Text("Inbox").fontWeight(.semibold)
                if !w.isEmpty {
                    Text("\(w.count)").font(sys(size: Theme.fs1, weight: .bold)).foregroundColor(.black.opacity(0.8))
                        .padding(.horizontal, 6).frame(minWidth: 18, minHeight: 16).background(Capsule().fill(Theme.needs))
                        .accessibilityLabel("\(w.count) waiting")
                }
                Spacer()
                Button { c.inboxOpen = false } label: {
                    Image(systemName: "xmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(dim).frame(width: 28, height: 28).contentShape(Rectangle())
                }.buttonStyle(.plain).help("Close (Esc)").accessibilityLabel("Close inbox")
            }.padding(.leading, Theme.sp4).padding(.trailing, Theme.sp2).padding(.vertical, Theme.sp1).background(card)
            Divider().overlay(line)
            if w.isEmpty {
                HStack(alignment: .top, spacing: Theme.sp3) {
                    Image(systemName: "checkmark.circle.fill").font(sys(size: Theme.fs5)).foregroundColor(Theme.done)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Nothing waiting on you").fontWeight(.semibold)
                        Text("The bell lights up when \(c.name) needs a decision.").font(sys(size: Theme.fs2)).foregroundColor(dim)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(Theme.sp4)
            } else {
                let rows = VStack(spacing: 0) {
                    ForEach(w) { q in if q.id == open { QuestionRow(c: c, q: q) } else { InboxLine(c: c, q: q) } }
                }.padding(.vertical, Theme.sp1)
                ViewThatFits(in: .vertical) { rows; ScrollView { rows } }
            }
            // the last few answered, and every answer greenlight gave for you in the last day (each with its Undo)
            let answered = c.questions.filter { $0.answer != nil }, last = Set(answered.suffix(3).map(\.id))
            let done = answered.filter { last.contains($0.id) || ($0.auto != nil && $0.ts > Date().addingTimeInterval(-86400)) }.reversed()
            if !done.isEmpty {
                Divider().overlay(line)
                VStack(alignment: .leading, spacing: Theme.sp2) {
                    Text("ANSWERED").font(sys(size: Theme.fs1, weight: .semibold)).foregroundColor(dim)
                    ForEach(Array(done)) { q in
                        HStack(spacing: Theme.sp2) {
                            let dismissed = q.answer == "(dismissed)"
                            Image(systemName: q.auto != nil ? "sparkles" : dismissed ? "xmark" : "checkmark").font(sys(size: Theme.fs1, weight: .semibold))
                                .foregroundColor(q.auto != nil ? accent : dismissed ? dim : Theme.done).frame(width: 12)
                            Text(q.title(c.name)).foregroundColor(sub).lineLimit(1).frame(width: 92, alignment: .leading)
                            Text(dismissed ? "Dismissed" : q.answer ?? "").foregroundColor(dim).lineLimit(1).help(q.text)
                            Spacer(minLength: Theme.sp2)
                            if q.auto != nil {
                                Button("Ask again") { c.undoAuto(q) }.buttonStyle(.plain).foregroundColor(accent)
                                    .help("Greenlight will ask again. This does not reverse an action already taken.")
                            }
                        }.font(sys(size: Theme.fs2))
                    }
                }.padding(.horizontal, Theme.sp4).padding(.vertical, Theme.sp3)
            }
            if !w.isEmpty {
                Divider().overlay(line)
                HStack(spacing: Theme.sp2) {
                    if c.inboxConfirm {
                        Text("Dismiss all \(w.count) without answering?").foregroundColor(sub)
                        Spacer(minLength: Theme.sp2)
                        Button("Cancel") { c.inboxConfirm = false }.buttonStyle(.plain).foregroundColor(dim).help("Esc")
                        Button("Clear all", action: c.clearInbox).buttonStyle(.plain).fontWeight(.semibold).foregroundColor(fg)
                            .padding(.horizontal, Theme.sp3).frame(minHeight: 26).background(RoundedRectangle(cornerRadius: Theme.r1).fill(surface))
                            .overlay(RoundedRectangle(cornerRadius: Theme.r1).stroke(edge)).help("Return")
                    } else {
                        Group { kbd("↑↓"); Text("move"); kbd("⌫"); Text("dismiss"); kbd("esc"); Text("close") }.foregroundColor(dim)
                        Spacer(minLength: Theme.sp2)
                        Button { c.inboxConfirm = true } label: { HStack(spacing: 6) { Text("Clear all"); kbd("⇧⌫") } }.buttonStyle(.plain).foregroundColor(sub)
                            .help("Dismiss every waiting question without answering. Nothing is sent.")
                    }
                }.font(sys(size: Theme.fs1)).padding(.horizontal, Theme.sp4).frame(minHeight: 38).background(card)
            }
        }
        .background(RoundedRectangle(cornerRadius: Theme.r3).fill(bg))
        .overlay(RoundedRectangle(cornerRadius: Theme.r3).stroke(edge))
        .clipShape(RoundedRectangle(cornerRadius: Theme.r3))
        .shadow(color: .black.opacity(theme.dark ? 0.5 : 0.12), radius: theme.dark ? 20 : 12, y: theme.dark ? 10 : 4)
        .accessibilityElement(children: .contain).accessibilityLabel("Questions waiting for you")
    }
}

/// A key, as the inbox shows it: "1", "R", "⇧⌫".
func kbd(_ k: String) -> some View {
    Text(k).font(sys(size: 10, weight: .medium)).foregroundColor(dim).padding(.horizontal, 4).frame(minWidth: 16, minHeight: 16)
        .overlay(RoundedRectangle(cornerRadius: 4).stroke(edge)).accessibilityHidden(true)
}

/// A waiting question, folded to a line: which workspace, what it asks, how long ago. Click opens it.
struct InboxLine: View {
    @ObservedObject var c: Core
    let q: Question
    @StateObject private var hover = Flag()  // not @State: see the top of the file
    var body: some View {
        Button { c.inboxPick = q.id } label: {
            HStack(spacing: Theme.sp2) {
                Circle().fill(Theme.needs).frame(width: 6, height: 6)
                Text(q.title(c.name)).font(sys(size: Theme.fs2, weight: .semibold)).lineLimit(1).frame(width: 92, alignment: .leading)
                Text(q.label.1).font(sys(size: Theme.fs2)).foregroundColor(sub).lineLimit(1)
                Spacer(minLength: Theme.sp1)
                Text(ago(q.ts)).font(sys(size: Theme.fs1)).foregroundColor(dim)
            }.padding(.horizontal, Theme.sp4).frame(height: 32).background(hover.on ? surface : .clear).contentShape(Rectangle())
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
        VStack(alignment: .leading, spacing: Theme.sp2) {
            HStack(spacing: Theme.sp2) {
                Circle().fill(Theme.needs).frame(width: 6, height: 6)  // ping or chat, it waits on you
                Text(q.title(c.name)).font(sys(size: Theme.fs2, weight: .semibold)).lineLimit(1)
                if let mr { Text(mr).font(sys(size: Theme.fs2)).foregroundColor(sub) }
                Spacer(minLength: 0)
                Text(ago(q.ts)).font(sys(size: Theme.fs1)).foregroundColor(dim)
            }
            Text(md(said)).font(chatFace(size: Theme.fs3)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            let chips = Group {  // equal weight, unless autonomy.py's past answers make one likely (tier 2): that one is preselected
                ForEach(Array(q.options.enumerated()), id: \.element) { i, o in
                    AnswerChip(title: o, key: i < 9 ? "\(i + 1)" : nil, likely: q.suggest?.choice == o) { c.answer(q, o) }
                }
                if c.replyingTo != q.id {
                    Button { c.replyText = ""; c.replyingTo = q.id } label: { HStack(spacing: 6) { kbd("R"); Text(q.options.isEmpty ? "Reply" : "Other…") } }
                        .buttonStyle(.plain).font(sys(size: Theme.fs2)).foregroundColor(sub)
                        .padding(.horizontal, Theme.sp3).frame(minHeight: 30).overlay(Capsule().stroke(edge)).contentShape(Capsule())
                }
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: Theme.sp2) { chips }
                VStack(alignment: .leading, spacing: Theme.sp2) { chips }
            }
            if c.replyingTo == q.id {
                HStack(spacing: Theme.sp2) {
                    TextField("Reply to \(c.name)…", text: $c.replyText).textFieldStyle(.plain).onSubmit(send).focused($typing)
                        .onAppear { DispatchQueue.main.async { typing = true } }
                    Button(action: send) {
                        Image(systemName: "arrow.up").font(sys(size: Theme.fs1, weight: .bold)).frame(width: 24, height: 24)
                            .background(RoundedRectangle(cornerRadius: Theme.r1).fill(accent)).foregroundColor(bg)
                    }.buttonStyle(.plain).accessibilityLabel("Send reply")
                }.padding(.leading, Theme.sp3).padding(.trailing, 2).padding(.vertical, 2)
                    .background(RoundedRectangle(cornerRadius: Theme.r1).fill(bg)).overlay(RoundedRectangle(cornerRadius: Theme.r1).stroke(accent))
            }
            HStack(spacing: Theme.sp1) {
                if let s = q.suggest {
                    Label(likelyNote(s), systemImage: "sparkles").lineLimit(1)
                        .help("Greenlight's guess from your past answers to this sort of question. Nothing is sent until you choose.")
                    Text("·")
                }
                Text(q.ping ? "Ping" : "Asked in the chat")
                Spacer(minLength: Theme.sp2)
                if let ws = q.about {
                    Button("Open") { c.openPage(ws) }.buttonStyle(.plain).foregroundColor(sub).help("Show \(ws) in the dashboard")
                }
                Button("Dismiss") { c.dismiss(q) }.buttonStyle(.plain).foregroundColor(sub).padding(.leading, Theme.sp2)
                    .help("Dismiss without answering (⌫)")
            }.font(sys(size: Theme.fs1)).foregroundColor(dim)
        }.padding(Theme.sp3)
            .background(RoundedRectangle(cornerRadius: Theme.r2).fill(card)).overlay(RoundedRectangle(cornerRadius: Theme.r2).stroke(line))
            .padding(.horizontal, Theme.sp2).padding(.vertical, Theme.sp1)
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
        HStack(alignment: .firstTextBaseline, spacing: Theme.sp3) {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(a.choice) · \(title)").font(sys(size: Theme.fs3, weight: .medium)).foregroundColor(fg)
                Text("\(ago(Date(timeIntervalSince1970: a.ts))) · \(Int((a.prob * 100).rounded()))% sure · \(said)")
                    .font(sys(size: Theme.fs2)).foregroundColor(dim).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: Theme.sp3)
            if a.undone != nil { Text("Asked again").font(sys(size: Theme.fs1)).foregroundColor(dim) }
            else { Button("Ask me again", action: undo).help("Asks you again; it does not reverse an action already taken") }
        }.padding(.horizontal, Theme.sp4).padding(.vertical, 10)
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
                if likely { Image(systemName: "checkmark").font(sys(size: Theme.fs1, weight: .bold)) }
                Text(title)
            }
        }.buttonStyle(.plain).font(sys(size: Theme.fs2, weight: likely ? .semibold : .regular))
            .padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp2).frame(minHeight: 30)
            .background(Capsule().fill(Theme.needs.opacity(likely ? (hover.on ? 0.32 : 0.22) : hover.on ? 0.14 : 0)).background(Capsule().fill(surface)))
            .overlay(Capsule().stroke(Theme.needs.opacity(hover.on || likely ? 0.85 : 0.5))).contentShape(Capsule()).onHover { hover.on = $0 }
            .accessibilityLabel(likely ? "\(title), suggested" : title)
    }
}
final class Flag: ObservableObject { @Published var on = false }

/// The hover hint, by the cursor (App.showTab).
struct HintTab: View {
    let text: String
    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Theme.r1)
        HStack(spacing: Theme.sp2) {
            Circle().fill(accent).frame(width: 6, height: 6)
            Text(text).font(sys(size: Theme.fs1)).foregroundColor(fg).lineLimit(3).fixedSize(horizontal: false, vertical: true)
        }.padding(.horizontal, Theme.sp3).padding(.vertical, Theme.sp1 + 2)
            .background(shape.fill(bg)).overlay(shape.stroke(edge))
            .preferredColorScheme(theme.dark ? .dark : .light)
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
    @ObservedObject var preview = LivePreview.shared
    @StateObject var rs = RulesStore()
    /// (pane, SF Symbol, words the sidebar's search also matches). This Mac's panes, then greenlight's on its host.
    static let mac = [("General", "slider.horizontal.3", "startup opens dock icon assistant name"),
                      ("Appearance", "circle.lefthalf.filled", "theme dark light accent color colour font size text dashboard source custom"),
                      ("Chat & voice", "bubble.left", "model effort dictation voice whisper engine shortcut"),
                      ("Keyboard", "keyboard", "vim shortcuts keys"),
                      ("Terminal", "terminal", "tab names basement orbs floors sidebar slide ride animation up button"),
                      ("Hosts", "server.rack", "ssh host state code relaunch connection")]
    static let gl = [("Automation", "bolt", "scan interval background agents pages suggestions system one jev haiku typesafe decisions"),
                     ("Autonomy", "wand.and.stars", "confidence auto answer preselect likely calibration graduate ping gate check-in watch dismiss undo"),
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
            .onChange(of: c.settingsOpen) { rs.projs = delegate.savedProjects; if !c.settingsOpen { preview.stop() } }
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
    /// The background agents dash.py runs where greenlight does: a Claude model, or a Gemini one through agy.
    func writerModelPicker(_ label: String, _ v: Binding<String>) -> some View {
        Picker(label, selection: v) { ForEach(models.filter { $0.1.hasPrefix("claude-") || $0.1.hasPrefix("gemini-") }, id: \.1) { Text($0.0).tag($0.1) } }.labelsHidden().frame(width: menuWidth)
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
            row("Effort", c.isClaude ? Self.effortNotes[c.effort] : c.model.hasPrefix("gemini-") ? "Choose the Gemini effort in Chat model." : "\(c.modelName) doesn't use an effort level.") { effortPicker("Effort", $c.effort, off: !c.isClaude) }
            toggleRow("Live activity", "Under the reply as \(c.name) works: the running tool call, time, tokens and speed, like Claude Code.", $c.liveLine)
            toggleRow("Verbose activity", "A live log above the message box (and under the live line in voice mode): \(c.name)'s every command in full with its output and thinking, what your workspaces' agents are doing, and each move Greenlight makes. Off keeps the concise view.", $c.verbose)
        }
        let status = engineStatus, ready = !status.hasPrefix("Not ready")
        box("Dictation") {
            row("Engine", status) {
                Circle().fill(ready ? Theme.done : Theme.needs).frame(width: 8, height: 8).accessibilityLabel(ready ? "Ready" : "Not ready")
                Picker("Voice engine", selection: $c.engine) { ForEach(engines, id: \.0) { Text($0.1).tag($0.0) } }.labelsHidden().frame(width: menuWidth)
            }
            if c.engine == "whisper" { dependent(off: false) { row("Whisper model") { Button("Choose model…") { delegate.pickModel() } } } }
            if c.engine == "openai" { dependent(off: false) { row("OpenAI key", openAIKeySource()) { Button("Set key…") { delegate.setKey() } } } }
            row("Shortcut", "Press it in any app to dictate to the chat.") { shortcutEditor }
        }
        box("Voice mode") {
            row("Anthropic key", "For the stage: Claude Haiku 5.5 draws what \(c.name) says. Stored in your Keychain; without it the stage shows a plain list.") {
                Button("Set key…") { delegate.askKey("anthropic", "Anthropic API key", "voice mode's stage (Claude Haiku 5.5)") }
            }
            row("Speaking voice", Speech.shared.status.isEmpty ? (Speech.shared.kind == "live" ? "OpenAI's GPT-Live 1.1 holds the conversation: it hears you, hands each request to \(c.name) and reads its answer. About $0.05 a minute while voice mode is on; if it fails, voice mode stops and says why (no Apple voice standing in)." : "How \(c.name) sounds. Kokoro runs on this Mac; ElevenLabs is paid and gets each line \(c.name) says. Either falls back to Apple's voice.") : Speech.shared.status) {
                Picker("Speaking voice", selection: Binding(get: { Speech.shared.kind }, set: { ud.set($0, forKey: "speech"); preview.stop(); Speech.shared.stop(); Speech.shared.warm(); c.voiceTest = ""; c.objectWillChange.send() })) {
                    ForEach(Speech.kinds, id: \.0) { Text($0.1).tag($0.0) }
                }.labelsHidden().frame(width: menuWidth)
            }
            if Speech.shared.kind == "kokoro" { dependent(off: false) { row("Kokoro voice", "a = American, b = British; f/m = female/male.") {
                Picker("Kokoro voice", selection: Binding(get: { Speech.shared.kokoroVoice }, set: { ud.set($0, forKey: "kokoroVoice"); c.objectWillChange.send() })) {
                    ForEach(Speech.kokoroVoices, id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(width: menuWidth)
            } } }
            if Speech.shared.kind == "live" { dependent(off: false) {
                row("OpenAI key", openAIKeySource() + " The same key as the OpenAI Whisper engine.") { Button("Set key…") { delegate.setKey() } }
                row("GPT-Live voice", preview.hint ?? "Picking one says “\(livePreviewLine)” in it. Fixed for each session: a new one takes effect next time voice mode starts.") {
                    if case .loading = preview.phase { ProgressView().controlSize(.small).accessibilityLabel("Getting the voice") }
                    Button { preview.busy ? preview.stop() : preview.play(LiveVoice.voice, voiceOn: c.voice.on) } label: {
                        Image(systemName: preview.busy ? "stop.fill" : "play.fill").frame(width: 14)
                    }.help(preview.busy ? "Stop the preview" : "Hear this voice").accessibilityLabel(preview.busy ? "Stop the preview" : "Hear this voice")
                    Picker("GPT-Live voice", selection: Binding(get: { LiveVoice.voice }, set: {
                        guard $0 != LiveVoice.voice else { return }
                        ud.set($0, forKey: "liveVoice"); c.voiceTest = ""; c.objectWillChange.send()
                        preview.play($0, voiceOn: c.voice.on)
                    })) {
                        ForEach(LiveVoice.voices, id: \.self) { Text($0.capitalized).tag($0) }
                    }.labelsHidden().frame(width: menuWidth)
                }
            } }
            if Speech.shared.kind == "eleven" { dependent(off: false) {
                row("ElevenLabs key", "Stored in your Keychain.") { Button("Set key…") { delegate.askKey("elevenlabs", "ElevenLabs API key", "voice mode's speaking voice") } }
                row("ElevenLabs voice ID", "From elevenlabs.io → Voices. Empty: Sarah.") {
                    TextField("EXAVITQu4vr4xnSDxMaL", text: Binding(get: { ud.string(forKey: "elevenVoice") ?? "" }, set: { ud.set($0.trimmingCharacters(in: .whitespaces), forKey: "elevenVoice"); Speech.shared.warm() })).frame(width: menuWidth)
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
            Slider(value: Binding(get: { min(b.wrappedValue, 2) }, set: { b.wrappedValue = ($0 * 20).rounded() / 20 }), in: 0...2)
                .frame(width: 130).accessibilityLabel(label)  // no `step:`, which draws 41 tick marks
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
            row("Workspace pages", "Builds each workspace's page on the dashboard.") { writerModelPicker("Workspace pages model", $c.designModel) }
            dependent(off: false) {
                row("Effort") { effortPicker("Workspace pages effort", $c.designEffort, off: harnessOf(c.designModel) == "agy") }
                row("Rebuild", c.designParallel == 0 ? "A page builds only when you open it." : "Workspaces whose state changed rebuild in the background.") {
                    if c.designParallel > 0 {
                        Stepper("\(c.designParallel) at once", value: $c.designParallel, in: 1...10).fixedSize().accessibilityLabel("Parallel builds")
                    }
                    Picker("Rebuild pages", selection: Binding(get: { c.designParallel > 0 }, set: { c.designParallel = $0 ? max(c.designParallel, 3) : 0 })) {
                        Text("On demand").tag(false); Text("Background").tag(true)
                    }.labelsHidden().pickerStyle(.segmented).fixedSize()
                }
            }
            row("Next-step suggestions", "Suggests the next action on each hex when its state moves.") { writerModelPicker("Suggestions model", $c.nextModel) }
            dependent(off: false) { row("Effort") { effortPicker("Suggestions effort", $c.nextEffort, off: harnessOf(c.nextModel) == "agy") } }
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
            if !c.s1Note.isEmpty { ruled(note(c.s1Note).padding(Theme.sp4)) }
        }
        // ponytail: auto-run is loaded per repo, so it's a link, not the design's every-repo table
        banner("bolt", "Auto-run jobs (Fix CI, Resolve threads, Fix conflicts, Review) are set per repository.") { Button("Open Repositories") { c.settingsTab = "Repositories" } }
    }

    // MARK: Autonomy (autonomy.py on the host)

    static let kindTitle = ["delete-workspace": "Delete a finished workspace", "keep-draft": "Keep a PR as a draft", "merge-held": "Merge a held PR",
                            "milestone": "Set a milestone", "agent-stuck": "Unstick an agent", "signoff": "Sign off a step"]
    func kindStats(_ k: AutonomyReport.Kind) -> String {
        let past = k.n.map { n in "\(k.right ?? 0) of \(n) guesses matched your answer." } ?? "No guesses checked yet."
        return k.eligible ? past : past + " A human gate: Greenlight never answers these for you, nor preselects merge or sign-off."
    }
    @ViewBuilder var autonomyPane: some View {
        heading("Autonomy", "Greenlight guesses your answer from what you answered before. At 95% confidence, with the sort turned on here, it may answer an inactive choice such as keep draft. You can ask it to reconsider. At 65% or more, it preselects the answer in the bell. For actions or uncertain choices, it asks.")
        Group {
            if let r = c.autonomy {
                ForEach(r.candidates, id: \.kind) { cand in
                    banner("graduationcap", "\(Self.kindTitle[cand.kind] ?? cand.kind): \(cand.right) of \(cand.n) sure guesses were right. Let Greenlight answer these itself?", tinted: true) {
                        Button("Turn on") { c.setAutonomy(["--auto", cand.kind, "on"]) }.buttonStyle(.borderedProminent)
                    }
                }
                box("Answered for you", aside: "Only when 95%+ sure, and only an inactive answer") {
                    ForEach(r.kinds, id: \.kind) { k in
                        row(Self.kindTitle[k.kind] ?? k.kind, kindStats(k)) {
                            Toggle(Self.kindTitle[k.kind] ?? k.kind, isOn: Binding(get: { k.opted }, set: { c.setAutonomy(["--auto", k.kind, $0 ? "on" : "off"]) }))
                                .labelsHidden().toggleStyle(.switch).disabled(!k.eligible)
                        }
                    }
                }
                box("Questions nobody asked for") {
                    row("Ping gate", "Idle check-ins and the watch go out only while you act on them more often than you dismiss them (✕, or left unanswered a day).") {
                        Picker("Ping gate", selection: Binding(get: { r.gate }, set: { c.setAutonomy(["--gate", "\($0)"]) })) {
                            Text("Off").tag(0.0); Text("Lenient").tag(0.25); Text("Balanced").tag(0.35); Text("Strict").tag(0.5)
                        }.labelsHidden().pickerStyle(.segmented).fixedSize()
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
                HStack(spacing: Theme.sp2) { ProgressView().controlSize(.small); note("Reading \(hostLabel)…") }
            }
            if !c.autonomyNote.isEmpty {
                banner("exclamationmark.triangle", c.autonomyNote) { Button("Retry", action: c.loadAutonomy) }
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
        let mutate: ((inout [String: Any]) -> Void) -> Void = { fn in
            commitProjects { l in if let idx = l.firstIndex(where: { pid($0) == id }) { fn(&l[idx]) } }
        }
        HStack(spacing: Theme.sp3) {
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
                    else { Button("Open terminals here") { mutate { $0["dir"] = d } }.buttonStyle(.borderless).font(sys(size: Theme.fs2)) }
                    xButton("Remove \(d)") { mutate { $0["dirs"] = pstrings($0, "dirs").filter { $0 != d }; if $0["dir"] as? String == d { $0["dir"] = nil } } }
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
                Text(own ? "Your own lifecycle: \(greenlightHome)/lifecycles/\(rs.repo.replacingOccurrences(of: "/", with: "__")).md on \(hostLabel)." : "Using the \(rs.source) lifecycle. Saving makes it your own.")
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
            }.padding(.horizontal, Theme.sp4).padding(.vertical, 10))
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
                TextField("Global default", text: optText("review_bot")).textFieldStyle(.roundedBorder).font(sys(size: Theme.fs2, design: .monospaced)).frame(width: 180)
            }
            row("Milestone prefix", "Active milestone prefix for the milestone step (default Release-).") {
                TextField("Release-", text: optText("milestone")).textFieldStyle(.roundedBorder).font(sys(size: Theme.fs2, design: .monospaced)).frame(width: 180)
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
        return ruled(HStack(alignment: .top, spacing: Theme.sp3) {
            Text("\(i + 1)").font(sys(size: Theme.fs2).monospacedDigit()).foregroundColor(dim).frame(width: 18, alignment: .trailing).padding(.top, 5)
            VStack(alignment: .leading, spacing: Theme.sp1) {
                HStack(spacing: Theme.sp2) {
                    if builtin { Text(cur.key).font(sys(size: Theme.fs2, weight: .semibold, design: .monospaced)).foregroundColor(fg).padding(.top, 4) }
                    else { TextField("key", text: keyBind).textFieldStyle(.roundedBorder).font(sys(size: Theme.fs2, design: .monospaced)).frame(width: 180).accessibilityLabel("Stage \(i + 1) key") }
                    Text(badge).font(sys(size: Theme.fs1, weight: .medium))
                        .foregroundColor(builtin ? sub : accent).padding(.horizontal, Theme.sp2).frame(height: 20)
                        .background(Capsule().fill(builtin ? surface : accent.opacity(0.12))).padding(.top, 2)
                }
                if builtin { note(cur.req) }
                else {
                    TextField("Requirement", text: reqBind).textFieldStyle(.roundedBorder).accessibilityLabel("Stage \(i + 1) requirement")
                    HStack(spacing: Theme.sp2) {
                        Picker("Done when", selection: Binding(
                            get: { doneBind.wrappedValue.hasPrefix("label=") ? "label" : doneBind.wrappedValue.hasPrefix("check=") ? "check" : "signoff" },
                            set: { doneBind.wrappedValue = $0 == "label" ? "label=" : $0 == "check" ? "check=" : "signoff" })) {
                            Text("You sign off").tag("signoff"); Text("It has a label").tag("label"); Text("CI check passes").tag("check")
                        }.labelsHidden().frame(width: 150)
                        if doneBind.wrappedValue.hasPrefix("label=") {
                            TextField("label", text: Binding(get: { String(doneBind.wrappedValue.dropFirst(6)) }, set: { doneBind.wrappedValue = "label=" + $0 }))
                                .textFieldStyle(.roundedBorder).frame(width: 160).accessibilityLabel("Stage \(i + 1) label")
                        } else if doneBind.wrappedValue.hasPrefix("check=") {
                            TextField("check name", text: Binding(get: { String(doneBind.wrappedValue.dropFirst(6)) }, set: { doneBind.wrappedValue = "check=" + $0 }))
                                .textFieldStyle(.roundedBorder).frame(width: 160).accessibilityLabel("Stage \(i + 1) check")
                        } else {
                            TextField("Run command (optional)", text: runBind).textFieldStyle(.roundedBorder).font(sys(size: Theme.fs2, design: .monospaced))
                                .frame(width: 200).accessibilityLabel("Stage \(i + 1) run command")
                        }
                    }
                    HStack(spacing: Theme.sp2) {
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
                Button { if i > 0 && i < rs.stages.count { rs.stages.swapAt(i, i - 1) } } label: { Image(systemName: "chevron.up") }.disabled(i == 0).accessibilityLabel("Move stage \(i + 1) up")
                Button { if i + 1 < rs.stages.count { rs.stages.swapAt(i, i + 1) } } label: { Image(systemName: "chevron.down") }.disabled(i >= rs.stages.count - 1).accessibilityLabel("Move stage \(i + 1) down")
                Button { if i < rs.stages.count && rs.stages.count > 1 { rs.stages.remove(at: i) } } label: { Image(systemName: "xmark") }.disabled(rs.stages.count <= 1).accessibilityLabel("Remove stage \(i + 1)")
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
                            HStack { Spacer(minLength: 60); queued([q.display, q.files.map { "📎 \($0.name)" }.joined(separator: "  ")].filter { !$0.isEmpty }.joined(separator: "\n"), i) }
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
            if c.verbose { rule(top: false); FeedPanel(f: c.feed, c: c) }
            rule(top: false)
            footer
            }.id(c.look)
            .overlay { DropArea(c: c) }  // files dragged anywhere on the chat, the field too
            .overlay { if c.dropping { dropHint } }
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
                ViewThatFits { HStack(spacing: Theme.sp2) { meta }.fixedSize(); EmptyView() }.layoutPriority(-1)  // a narrow pane drops the host, never clips
                if c.session == 0 { VoiceButtons(v: c.voice, c: c) }  // voice and its own window: the main chat's
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
            Text(shown).font(f).tracking(track).lineLimit(1).onTapGesture(count: 2) { c.editingName = true }.help("Double-click to rename")
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
            tag("· \(stateWord)", Theme.fs2, .semibold).foregroundColor(stateInk).lineLimit(1).fixedSize()
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
        let text = lk.id == "phosphor" ? Text("\(Text("you> ").foregroundColor(dim))\(Text(q))") : lk.id == "editorial" ? Text("“\(q)”").italic() : Text(q)
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
            if !c.files.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Theme.sp2) { ForEach(c.files) { fileChip($0) } }
                }
            }
            DwimToastBar(c: c, l: lk).animation(.easeOut(duration: 0.15), value: c.toast)
            if !c.fileNote.isEmpty {
                Label(c.fileNote, systemImage: "exclamationmark.circle").font(sys(size: Theme.fs1)).foregroundColor(Theme.needsInk)
                    .fixedSize(horizontal: false, vertical: true).accessibilityLabel("Not attached: \(c.fileNote)")
            }
            if c.hq != nil { histPanel }
            HStack(alignment: .bottom, spacing: Theme.sp2) {
                if c.recording == nil { attachButton }
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
                let empty = (c.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && c.files.isEmpty) || c.reading > 0  // submit() ignores it anyway
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
            ViewThatFits(in: .horizontal) {  // a narrow pane drops the hint, then the model; it never clips
                footRow(hint: true, model: true)
                footRow(hint: false, model: true)
                footRow(hint: false, model: false)
            }.font(sys(size: Theme.fs1).monospacedDigit()).controlSize(.small)
                .padding(.trailing, onair ? inset : 0).background(onair ? accent : .clear).padding(.horizontal, onair ? -inset : 0)  // full-bleed, the model flush left
        }.padding(.horizontal, inset).padding(.top, Theme.sp3).padding(.bottom, lk.id == "onair" ? 0 : Theme.sp4)
    }

    /// The composer's +: attach files (the open panel). Drops and ⌘V attach too.
    var attachButton: some View {
        let pad = padSize, r = corner(pad)
        return Button { c.pickFiles(NSApp.keyWindow) } label: {
            Group {
                if lk.id == "phosphor" { Text("[+]").padding(.horizontal, 4) }
                else { Image(systemName: "plus").font(sys(size: Theme.fs3, weight: .semibold)) }
            }.frame(minWidth: pad).frame(height: pad)
                .background(RoundedRectangle(cornerRadius: r).fill(lk.id == "conversation" || lk.id == "onair" ? page : .clear))
                .overlay(RoundedRectangle(cornerRadius: r).stroke(bare ? .clear : line))
                .foregroundColor(lk.id == "phosphor" ? fg : lk.id == "zen" ? dim : sub)
        }.buttonStyle(.plain)
            .modifier(Tip(c: c, text: "Attach files: \(ChatFile.supported). Or drop them here, or paste."))
            .accessibilityLabel("Attach files")
    }
    /// A file waiting to go with the next message: its thumbnail or icon, name and size, and × to take it off.
    func fileChip(_ f: ChatFile) -> some View {
        HStack(spacing: Theme.sp1) {
            if let t = f.thumb { Image(nsImage: t).resizable().aspectRatio(contentMode: .fill).frame(width: 18, height: 18).clipShape(RoundedRectangle(cornerRadius: 3)) }
            else { Image(systemName: f.kind == "pdf" ? "doc.richtext" : "doc.text").font(sys(size: Theme.fs2)).foregroundColor(dim) }
            tag(f.name, Theme.fs1, .semibold, caps: false).lineLimit(1).truncationMode(.middle).frame(maxWidth: 150, alignment: .leading)
            tag(byteSize(f.data.count), Theme.fs1, caps: false).foregroundColor(dim).lineLimit(1).fixedSize()
            Button { c.files.removeAll { $0.id == f.id } } label: {
                Image(systemName: "xmark").font(sys(size: Theme.glyph, weight: .bold)).foregroundColor(dim).frame(width: 16, height: 16)
            }.buttonStyle(.plain).accessibilityLabel("Remove \(f.name)")
        }.foregroundColor(lk.id == "phosphor" ? fg : sub).padding(.leading, lk.id == "zen" ? 0 : 3).padding(.trailing, 3).frame(height: 24)
            .background(RoundedRectangle(cornerRadius: corner(24)).fill(["orb", "conversation", "onair", "hud"].contains(lk.id) ? card : .clear))
            .overlay(RoundedRectangle(cornerRadius: corner(24)).stroke(lk.id == "orb" || lk.id == "zen" ? .clear : line))
            .help("\(f.kindName), goes to \(c.name) with your next message")
            .accessibilityElement(children: .contain).accessibilityLabel("Attached \(f.name), \(byteSize(f.data.count))")
    }
    /// Files dragged over the chat: where they'll go.
    var dropHint: some View {
        RoundedRectangle(cornerRadius: corner(40) > 0 ? Theme.r3 : 0).fill(bg.opacity(0.82))
            .overlay(RoundedRectangle(cornerRadius: corner(40) > 0 ? Theme.r3 : 0).stroke(accent, style: StrokeStyle(lineWidth: 2, dash: [6, 4])))
            .overlay(VStack(spacing: Theme.sp2) {
                Image(systemName: "paperclip").font(sys(size: Theme.fs6)).foregroundColor(accent)
                Text("Drop to attach").font(sys(size: Theme.fs4, weight: .semibold))
                Text(ChatFile.supported.prefix(1).uppercased() + ChatFile.supported.dropFirst()).font(sys(size: Theme.fs1)).foregroundColor(dim)
            })
            .padding(Theme.sp3).allowsHitTesting(false).accessibilityHidden(true)
    }

    func footRow(hint: Bool, model: Bool) -> some View {
        let onair = lk.id == "onair"
        return HStack(spacing: Theme.sp2) {
            if onair && model { modelButton.fixedSize() }  // On Air: the model first, inverse, on the amber strip
            if hint {
                tag((c.run != nil ? "Sends after this run" : "⌃R history") + " · ↩ send", lk.id == "editorial" ? Theme.fs1 - 1 : Theme.fs1, onair ? .heavy : lk.id == "editorial" ? .semibold : .regular)
                    .foregroundColor(onair ? page : dim).lineLimit(1).fixedSize()
            }
            Spacer(minLength: 4)
            if !onair && model { modelButton.fixedSize() }
            ContextButton(c: c)
            newChat
        }
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
        return Button(action: { c.clear() }) {
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
        let full = c.ctxFull, high = full > 0.75
        // the footer's control shape: a capsule (Orb), 6pt (Conversation), a tile (On Air), a box (HUD, Phosphor), bare (Editorial, Zen)
        let r: CGFloat = lk.id == "orb" ? 12 : lk.id == "conversation" ? Theme.r1 : 0, bare = lk.id == "editorial" || lk.id == "zen"
        let up = ["orb", "onair", "hud", "editorial"].contains(lk.id), code = lk.id == "phosphor" || lk.id == "hud"
        return Button { c.ctxOpen.toggle(); c.hint = "" } label: {  // the hint would cover the popover
            HStack(spacing: Theme.sp1) {
                ZStack {
                    Circle().stroke(edge, lineWidth: 2)
                    Circle().trim(from: 0, to: max(full, 0.02)).stroke(high ? needs : sub, style: StrokeStyle(lineWidth: 2, lineCap: .round)).rotationEffect(.degrees(-90))
                }.frame(width: 12, height: 12)
                Text(up ? "CONTEXT \(c.ctxShort)" : lk.id == "zen" ? "context \(c.ctxShort)" : "Context \(c.ctxShort)").tracking(up ? 1.2 : 0).lineLimit(1).fixedSize()
                    .font(sys(size: Theme.fs1, weight: lk.id == "onair" ? .heavy : .regular, design: code ? .monospaced : .default).monospacedDigit())
            }.foregroundColor(high ? needsInk : lk.id == "phosphor" ? fg : sub).padding(.horizontal, bare ? 0 : Theme.sp2).frame(height: 24)
                .background(RoundedRectangle(cornerRadius: r).fill(lk.id == "onair" ? card : c.ctxOpen ? accent.opacity(0.18) : lk.id == "conversation" ? card : .clear))  // On Air sits on amber
                .overlay(RoundedRectangle(cornerRadius: r).stroke(high ? needs.opacity(0.6) : c.ctxOpen && lk.id == "onair" ? fg : bare ? .clear : line)).contentShape(Rectangle())
        }.buttonStyle(.plain).modifier(Tip(c: c, text: ctxTip(c))).accessibilityLabel("Context, \(c.ctxSpoken)").modifier(CtxPop(c: c))
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
extension Core {
    var ctxFull: Double { window > 0 ? min(Double(context) / Double(window), 1) : 0 }
    /// The readout: "27%", or the tokens ("35k") while the window is unknown (a harness that doesn't say, or not yet).
    var ctxShort: String { window > 0 ? "\(Int((ctxFull * 100).rounded()))%" : kfmt(context) }
    var ctxSpoken: String { window > 0 ? "\(Int((ctxFull * 100).rounded())) percent used" : "\(kfmt(context)) tokens used" }
}

