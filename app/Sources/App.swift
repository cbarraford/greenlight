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

// MARK: app

/// The orb's look: hovered, pressed (FloatHost reports it), and which way it goes from here.
final class OrbState: ObservableObject { @Published var hover = false; @Published var pressed = false; @Published var up = true; var tap: () -> Void = {} }

/// The orb's face (brand/DESIGN.md §8.4): the mark on a 40pt bg.raised disc with elevation 2; hovered, a step higher (no
/// animation). Up (to Center) the mark is lit in the accent; down (to the Basement) it's the terminal glyph.
/// The matte, traffic-light and terminal-disc faces are retired.
struct LevelFace: View {
    let up: Bool
    var hover = false
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        let d = scheme == .dark
        Circle().fill(Color(DS.Palette.bgRaised, dark: d))
            .overlay(Circle().strokeBorder(Color(DS.Palette.borderSubtle, dark: d), lineWidth: 1))  // elevation's 1px ring
            .shadow(color: .black.opacity(d ? 0.35 : 0.06), radius: 2, y: 2)
            .shadow(color: .black.opacity(d ? 0.45 : 0.10), radius: hover ? 14 : 10, y: hover ? 6 : 4)  // elevation 2; 2+ hovered
            .overlay(up ? AnyView(ZStack {  // the mark: blades muted, its light in the accent
                    Image(nsImage: GreenlightMark.image(.idle, size: 22)).foregroundColor(Color(DS.Palette.fgMuted, dark: d))
                    Image(nsImage: GreenlightMark.image(.light, size: 22)).foregroundColor(Color(accentSet.fg, dark: d))
                }) : AnyView(Image(ds: "terminal", size: iconSize(DS.TypeRole.titleSm.size, scaled: false), medium: true)
                    .foregroundColor(Color(DS.Palette.fgDefault, dark: d))))
            .frame(width: 40, height: 40)
    }
}

/// The window bar's floor control (§10.19): a native segmented control, Center, Basement and Pipe, each with its glyph, in
/// sentence case. No floor colours, no glow: the selected segment is the system's own. On macOS 26 it sits on Liquid Glass.
final class FloorState: ObservableObject { @Published var at = "pipe"; @Published var project = ""; @Published var colors = [String]() }  // project: the window's, centered in the title bar; colors: its projects' (none: All projects)
struct Floors: View {
    @ObservedObject var s: FloorState
    @ObservedObject var c: Core  // its look bumps when the appearance changes
    var pick: () -> Void = {}  // the project chip: a terminal window's Open projects, L1's menu
    let go: (String) -> Void
    /// (floor, name, glyph): center = the mark, basement = terminal, pipe = a single pane.
    static let levels = [("greenlight", "Center", "hex"), ("basement", "Basement", "terminal"), ("pipe", "Pipe", "window")]
    var body: some View {
        let _ = c.look
        HStack(spacing: DS.Space.s8) {
            if let devName {  // a dev copy: a "Dev" tag with the debug glyph, whose workspace in its help
                Label { Text("Dev") } icon: { Image(ds: "hammer", size: iconSize(DS.TypeRole.labelSm.size, scaled: false)) }
                    .font(ds(DS.TypeRole.labelSm)).foregroundColor(dim).padding(.horizontal, DS.Space.s4).frame(height: 20)
                    .background(RoundedRectangle(cornerRadius: DS.Radius.xs).fill(surface))
                    .help("Dev copy of the \(devName) workspace").accessibilityLabel("Dev copy: \(devName)")
            }
            FloorSegments(at: s.at, go: go).fixedSize()
        }.modifier(ChromeGlass()).frame(width: 300 + devTagWidth, height: 28, alignment: .trailing).padding(.trailing, DS.Space.s8)
    }
}
/// The floors as an NSSegmentedControl: SwiftUI's segmented picker shows a title or an image, and each floor wants both.
struct FloorSegments: NSViewRepresentable {
    let at: String, go: (String) -> Void
    final class Coord: NSObject { var go: (String) -> Void = { _ in }; @objc func pick(_ c: NSSegmentedControl) { go(Floors.levels[c.selectedSegment].0) } }
    func makeCoordinator() -> Coord { Coord() }
    func makeNSView(context: Context) -> NSSegmentedControl {
        let c = NSSegmentedControl(labels: Floors.levels.map(\.1), trackingMode: .selectOne, target: context.coordinator, action: #selector(Coord.pick(_:)))
        for (i, l) in Floors.levels.enumerated() {
            c.setImage(l.0 == "greenlight" ? GreenlightMark.image(.lit) : DSIcon.image(l.2, size: 16), forSegment: i); c.setImageScaling(.scaleNone, forSegment: i)
            c.setToolTip("Go to \(l.1)", forSegment: i)
        }
        c.segmentStyle = .automatic; c.controlSize = .regular
        c.setAccessibilityLabel("Floor")
        return c
    }
    func updateNSView(_ c: NSSegmentedControl, context: Context) {
        context.coordinator.go = { f in if f != at { go(f) } }
        let i = Floors.levels.firstIndex { $0.0 == at } ?? 0
        if c.selectedSegment != i { c.selectedSegment = i }
    }
}
/// Liquid Glass for the native control layer only (§5), where the toolchain has it; plain chrome otherwise.
struct ChromeGlass: ViewModifier {
    func body(content: Content) -> some View {
        if #available(macOS 26, *) { content.glassEffect(.regular, in: Capsule()) } else { content }
    }
}
/// The project chip (§10.19): the project's monogram tag, its name in `label` and a chevron, as a quiet button centered in the
/// title bar; a click opens the picker. All projects shows the grid glyph in place of a monogram.
struct ProjectChip: View {
    @ObservedObject var s: FloorState
    @ObservedObject var c: Core
    let pick: () -> Void
    var body: some View {
        let _ = c.look
        if !s.project.isEmpty && s.at != "pipe" {  // the Pipe is just the terminals: no project chip there
            Button(action: pick) {
                HStack(spacing: DS.Space.s4) {
                    Group {
                        if s.colors.isEmpty { Image(ds: "grid", size: iconSize(DS.TypeRole.caption.size, scaled: false)) }
                        else { Text(String(s.project.prefix(1)).uppercased()).font(ds(DS.TypeRole.labelSm)) }
                    }.foregroundColor(dim).frame(width: 18, height: 18).background(RoundedRectangle(cornerRadius: DS.Radius.xs).fill(surface))
                    Text(s.project).font(ds(DS.TypeRole.label)).foregroundColor(fg).lineLimit(1).truncationMode(.tail)
                    Image(ds: "chevron-down", size: iconSize(9, scaled: false), medium: true).foregroundColor(dim)
                }.padding(.horizontal, DS.Space.s4).frame(height: DS.Layout.controlHeightSm)
                    .contentShape(Rectangle())
            }.buttonStyle(.plain).frame(maxWidth: 260).help("\(s.project) · Show projects…").accessibilityLabel("Projects: \(s.project)")
        }
    }
}
/// Floors in a window's title bar, top right; the project chip in its middle.
func floorBar(_ w: NSWindow, _ f: Floors) {
    let a = NSTitlebarAccessoryViewController(), h = NSHostingView(rootView: f)
    h.frame = NSRect(x: 0, y: 0, width: 310 + devTagWidth, height: 28)
    a.view = h; a.layoutAttribute = .trailing
    w.addTitlebarAccessoryViewController(a)
    guard let tb = w.standardWindowButton(.closeButton)?.superview else { return }  // the title bar itself: no accessory slot is centered
    let p = NSHostingView(rootView: ProjectChip(s: f.s, c: f.c, pick: f.pick)); p.translatesAutoresizingMaskIntoConstraints = false
    tb.addSubview(p)
    let mid = p.centerXAnchor.constraint(equalTo: tb.centerXAnchor); mid.priority = .defaultHigh  // centered, unless the floors are in the way
    NSLayoutConstraint.activate([mid, p.centerYAnchor.constraint(equalTo: tb.centerYAnchor),
                                 p.trailingAnchor.constraint(lessThanOrEqualTo: tb.trailingAnchor, constant: -(h.frame.width + 8))])  // clear of the floors (same superview: safe at creation)
}

/// The orb: in the Basement it goes up to Greenlight Center, there it comes back down; drag puts it anywhere.
struct OrbFace: View {
    @ObservedObject var s: OrbState
    var body: some View {
        LevelFace(up: s.up, hover: s.hover).scaleEffect(s.pressed ? 0.97 : 1).frame(width: 64, height: 64)  // the panel leaves room for the shadow
            .contentShape(Circle()).onHover { s.hover = $0 }
            .help(s.up ? "Up to Greenlight Center · ⌘⇧G\nDrag to reposition" : "Down to the Basement (terminal) · ⌘⇧T\nDrag to reposition")
            .accessibilityElement().accessibilityLabel(s.up ? "Go up to Greenlight Center" : "Go down to the Basement").accessibilityAddTraits(.isButton)
            .accessibilityAction { s.tap() }  // VoiceOver's press: the same ride as a click
    }
}

/// A window as a picture in the ride's overlay (top-left coordinates): its title band, then its content.
struct Shot { var img: NSImage?; var band: NSColor, rect: CGRect, top: CGFloat }
struct Squash { var sx = 1.0, sy = 1.0, r = 12.0, a = 1.0 }
final class RideState: ObservableObject {
    @Published var from = Squash()
    @Published var to = Squash(sx: 0.02, sy: 0.02, r: 300, a: 0)
    @Published var veil = 0.0
    @Published var tone = 0.0
    @Published var flip = 0.0
    @Published var faceUp = true
    @Published var toShot: Shot?
}
/// The ride: the window you leave shrinks into the orb, the orb flips, the other grows out of the same spot.
struct RideView: View {
    let up: Bool, from: Shot, orb: CGPoint; var face = true
    @ObservedObject var s: RideState
    func win(_ w: Shot, _ q: Squash) -> some View {
        VStack(spacing: 0) {
            Color(nsColor: w.band).frame(height: w.top)
            if let i = w.img { Image(nsImage: i).resizable() } else { Color(nsColor: w.band) }
        }
        .background(Color(nsColor: w.band)).frame(width: w.rect.width, height: w.rect.height).clipShape(RoundedRectangle(cornerRadius: q.r))
        .scaleEffect(x: q.sx, y: q.sy, anchor: UnitPoint(x: (orb.x - w.rect.minX) / w.rect.width, y: (orb.y - w.rect.minY) / w.rect.height))
        .opacity(q.a).offset(x: w.rect.minX, y: w.rect.minY)
    }
    var body: some View {
        ZStack(alignment: .topLeading) {
            ZStack { Color(DS.Palette.bgCanvas, dark: true); Color(DS.Palette.bgCanvas, dark: false).opacity(s.tone) }.opacity(s.veil)
            win(from, s.from)
            if let t = s.toShot { win(t, s.to) }
            if face { LevelFace(up: s.faceUp).rotation3DEffect(.degrees(s.flip), axis: (x: 0, y: 1, z: 0)).position(orb) }  // Floating orbs off: the windows still squash and grow, with no orb between them
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// Borderless floating bits (the orb): click runs `onClick`; `draggable` ones move with the mouse and report `onMoved`.
final class FloatHost<V: View>: NSHostingView<V> {
    var onClick: () -> Void = {}, onMoved: () -> Void = {}, onPress: (Bool) -> Void = { _ in }
    var draggable = false
    var inside: () -> NSRect? = { nil }  // drags stay within this frame (the dashboard window)
    private var down = NSPoint.zero, origin = NSPoint.zero, moved = false
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func mouseDown(with e: NSEvent) { down = NSEvent.mouseLocation; origin = window?.frame.origin ?? .zero; moved = false; onPress(true) }
    override func mouseDragged(with e: NSEvent) {
        guard draggable else { return }
        let p = NSEvent.mouseLocation
        if !moved && hypot(p.x - down.x, p.y - down.y) < 3 { return }  // a click that wobbled
        moved = true
        var o = NSPoint(x: origin.x + p.x - down.x, y: origin.y + p.y - down.y)
        if let r = inside(), let s = window?.frame.size {
            o.x = min(max(o.x, r.minX), r.maxX - s.width); o.y = min(max(o.y, r.minY), r.maxY - s.height)
        }
        window?.setFrameOrigin(o)
    }
    override func mouseUp(with e: NSEvent) { onPress(false); moved ? onMoved() : onClick() }
}

/// A login shell on a pseudo-terminal, for the T orb's terminal.
final class Pty {
    private(set) var pid: pid_t = 0
    private var fd: Int32 = -1, src: DispatchSourceRead?
    private let writeQ = DispatchQueue(label: "greenlight.pty.write")
    var onOutput: (Data) -> Void = { _ in }
    var onExit: (Int32) -> Void = { _ in }  // the exit status: 255 is ssh's "couldn't connect / connection lost"

    /// `cmd`: run by a login shell (so PATH has Homebrew's tmux, ssh config…); nil: just the login shell.
    func start(cols: Int, rows: Int, cmd: String? = nil) {
        guard pid == 0 else { return }
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh", home = NSHomeDirectory()
        var env = ProcessInfo.processInfo.environment
        env["TERM"] = "xterm-256color"; env["COLORTERM"] = "truecolor"; env["TERM_PROGRAM"] = "Greenlight"
        // Everything the child needs is built before the fork: after it, the child only chdirs and execs (async-signal-safe).
        let cHome = strdup(home), cShell = strdup(shell)
        let argv = [strdup("-" + (shell as NSString).lastPathComponent)] + (cmd.map { [strdup("-c"), strdup($0)] } ?? []) + [nil]
        let envp = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { free(cHome); free(cShell); argv.forEach { free($0) }; envp.forEach { free($0) } }
        var ws = winsize(ws_row: UInt16(min(max(rows, 2), Int(UInt16.max))), ws_col: UInt16(min(max(cols, 10), Int(UInt16.max))), ws_xpixel: 0, ws_ypixel: 0)
        var master: Int32 = -1
        let p = forkpty(&master, nil, nil, &ws)
        if p == 0 {
            if let cHome { _ = chdir(cHome) }
            if let cShell { execve(cShell, argv, envp) }
            _exit(127)
        }
        guard p > 0 else { return }
        let fl = fcntl(master, F_GETFL)
        if fl >= 0 { _ = fcntl(master, F_SETFL, fl | O_NONBLOCK) }
        pid = p; fd = master
        let s = DispatchSource.makeReadSource(fileDescriptor: master, queue: .main)
        s.setEventHandler { [weak self] in
            guard let self else { return }
            var buf = [UInt8](repeating: 0, count: 65536)
            let n = read(self.fd, &buf, buf.count)
            if n > 0 { self.onOutput(Data(buf[0..<n])); return }
            if n < 0 && (errno == EINTR || errno == EAGAIN) { return }
            // EIO/EOF: reap child off the main thread so a lingering process never blocks AppKit.
            let child = self.pid
            self.pid = 0; self.src?.cancel(); self.src = nil; self.fd = -1
            DispatchQueue.global(qos: .utility).async {
                var st: Int32 = 0
                var r: pid_t = -1
                repeat { r = waitpid(child, &st, 0) } while r < 0 && errno == EINTR
                let code: Int32 = r == child ? ((st & 0x7f) == 0 ? (st >> 8) & 0xff : 128 + (st & 0x7f)) : -1
                DispatchQueue.main.async { self.onExit(code) }
            }
        }
        s.setCancelHandler { [writeQ] in writeQ.async { close(master) } }
        s.resume(); src = s
    }
    func write(_ text: String) {
        let curFd = fd
        guard curFd >= 0 else { return }
        let bytes = Array(text.utf8)
        writeQ.async { [weak self] in
            guard let self, self.fd == curFd else { return }
            var off = 0
            while off < bytes.count {
                let n = bytes.withUnsafeBytes { ptr in Darwin.write(curFd, ptr.baseAddress!.advanced(by: off), bytes.count - off) }
                if n > 0 { off += n; continue }
                if n < 0 && errno == EINTR { continue }
                if n < 0 && errno == EAGAIN {
                    var pfd = pollfd(fd: curFd, events: Int16(POLLOUT), revents: 0)
                    if poll(&pfd, 1, 2000) > 0 && (pfd.revents & Int16(POLLOUT)) != 0 { continue }
                }
                break
            }
        }
    }
    func resize(cols: Int, rows: Int) {
        guard fd >= 0 else { return }
        var ws = winsize(ws_row: UInt16(min(max(rows, 2), Int(UInt16.max))), ws_col: UInt16(min(max(cols, 10), Int(UInt16.max))), ws_xpixel: 0, ws_ypixel: 0)
        _ = ioctl(fd, TIOCSWINSZ, &ws)
    }
    func stop() {
        let child = pid
        guard child > 0 else { return }
        pid = 0; src?.cancel(); src = nil; fd = -1
        kill(child, SIGHUP)
        DispatchQueue.global(qos: .utility).async {
            var st: Int32 = 0
            for _ in 0..<20 {
                let r = waitpid(child, &st, WNOHANG)
                if r == child || (r < 0 && errno == ECHILD) { return }
                usleep(50_000)
            }
            kill(child, SIGKILL)
            var r: pid_t = -1
            repeat { r = waitpid(child, &st, 0) } while r < 0 && errno == EINTR
        }
    }
}

func floatPanel(_ size: NSSize) -> NSPanel {
    let w = NSPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    w.isOpaque = false
    w.backgroundColor = .clear
    w.hasShadow = true
    w.isReleasedWhenClosed = false
    w.hidesOnDeactivate = false
    return w
}


/// The projects a window shows (ids, "" No Man's Land); nil is all of them, the default. Never empty: none ticked, or none of the ticked
/// still among `known` (the projects listed; nil: not listed yet, keep them all), is all again. A gone one drops out of a pick that keeps others.
func livePick(_ only: [String]?, known: [String]?) -> [String]? {
    guard let only else { return nil }
    let k = known.map { ids in only.filter(ids.contains) } ?? only
    return k.isEmpty ? nil : k
}
/// A restored window's frame, kept on a screen that's still there: a monitor unplugged since quit would leave it out of reach. It stays
/// where it was while 60pt of its title bar (half its height) is on some screen; else it keeps its size (at most that screen's) and goes to the
/// middle of `screens[0]` (the main screen's visible frame). No screens (headless): unchanged.
func fitOnScreen(_ f: NSRect, screens: [NSRect]) -> NSRect {
    guard let home = screens.first else { return f }
    let bar = NSRect(x: f.minX, y: f.maxY - 24, width: f.width, height: 24)  // its title bar
    if screens.contains(where: { let i = $0.intersection(bar); return i.width >= min(60, f.width) && i.height >= 12 }) { return f }
    let w = min(f.width, home.width), h = min(f.height, home.height)
    return NSRect(x: home.midX - w / 2, y: home.midY - h / 2, width: w, height: h)
}
/// The rect in an NSWindow frame string ("x y w h screenX screenY screenW screenH "), as saved: `setFrameUsingName` rescales it
/// onto the screen with focus when that's another monitor, so a relaunch moved a window off the laptop onto the external.
func savedFrame(_ s: String?) -> NSRect? {
    let n = (s ?? "").split(separator: " ").prefix(4).compactMap { Double($0) }
    return n.count == 4 && n[2] > 0 && n[3] > 0 ? NSRect(x: n[0], y: n[1], width: n[2], height: n[3]) : nil
}
/// A new window cascaded 30pt right and down from `f`: at least `min` (a small view window's size is too small for a whole
/// Greenlight Center), at most the screen, and inside it, so ⌘N after ⌘N doesn't walk windows off the edge. No screen: just cascaded.
func cascadeFrame(_ f: NSRect, min m: NSSize, in screen: NSRect?) -> NSRect {
    var r = NSRect(x: f.minX + 30, y: 0, width: max(f.width, m.width), height: max(f.height, m.height))
    r.origin.y = f.maxY - 30 - r.height
    guard let s = screen else { return r }
    r.size = NSSize(width: min(r.width, s.width), height: min(r.height, s.height))
    r.origin.y = f.maxY - 30 - r.height
    r.origin.x = min(max(r.minX, s.minX), s.maxX - r.width)
    r.origin.y = min(max(r.minY, s.minY), s.maxY - r.height)
    return r
}
/// The frame a floor switch hands the next window: `f`, grown to its minimum `m` from the top-left corner (a window's
/// title bar stays where it was), and when it grew, shifted back inside `screen` (`f`'s own).
func sharedFrame(_ f: NSRect, min m: NSSize, in screen: NSRect? = nil) -> NSRect {
    var r = NSRect(x: f.minX, y: f.maxY - max(f.height, m.height), width: max(f.width, m.width), height: max(f.height, m.height))
    guard let s = screen, r.size != f.size else { return r }
    r.origin.x = max(min(r.minX, s.maxX - r.width), s.minX)
    r.origin.y = max(min(r.minY, s.maxY - r.height), s.minY)
    return r
}
/// Whether a dashboard window may render snapshots yet: All-projects windows don't wait; filtered ones wait until the project list is loaded
/// so the UI doesn't flash another project's hexes before narrowing to the saved pick.
func canShowDashboard(only: [String]?, projectsLoaded: Bool) -> Bool { only == nil || projectsLoaded }
/// True when a saved terminals.json payload holds at least one tab to restore at launch.
func hasSavedTermTabs(_ data: Data?) -> Bool {
    guard let data,
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let tabs = obj["tabs"] as? [Any] else { return false }
    return !tabs.isEmpty
}
/// The title-bar project chip for a window: hidden while a saved pick is still loading, instead of flashing "All projects".
func projectChip(only: [String]?, projList: [[String: Any]], projectsLoaded: Bool) -> (text: String, colors: [String]) {
    if only != nil && !projectsLoaded { return ("", []) }
    let o = projList.isEmpty ? nil : livePick(only, known: projList.compactMap { $0["id"] as? String })
    let ps = (o ?? []).compactMap { id in projList.first { $0["id"] as? String == id } }
    let names = ps.compactMap { $0["name"] as? String }.joined(separator: ", ")
    return (o == nil ? "All projects" : names.isEmpty ? "No projects" : names, o == nil ? [] : ps.compactMap { $0["color"] as? String })
}
/// Builds L1's project list (`projList`) from `projects.json` and `dash.py trees` — matching terminal.html's merge + setTitle rules.
/// Each entry has: id ("" No Man's Land last), name, color, repos, paths, busy. `fallback`: used only if `dash.py trees` failed.
func buildProjectList(saved: [[String: Any]], trees: [[String: Any]], dark: Bool, fallback: Bool = false) -> [[String: Any]] {
    struct Proj { var id: String; var name: String; var repos: [String]; var dirs: [String] }
    let nml = "No Man's Land"
    let palette = dark
        ? ["#FF9F0A", "#4DA3FF", "#30D158", "#C597FF", "#66D4CF", "#FFD60A", "#FF6259"]
        : ["#B25000", "#0969DA", "#1A7F37", "#8250DF", "#0E7C77", "#8A6A00", "#C9372C"]
    let savedProjs: [Proj] = saved.compactMap { p in
        guard let id = p["id"] as? String, !id.isEmpty,
              let repos = p["repos"] as? [String], let dirs = p["dirs"] as? [String] else { return nil }
        return Proj(id: id, name: p["name"] as? String ?? "Project", repos: repos, dirs: dirs)
    }
    var allP = savedProjs
    for w in trees {
        guard w["main"] as? Bool == true, let repo = w["project"] as? String, !repo.isEmpty,
              let raw = (w["cfg"] as? [String: Any])?["name"] as? String else { continue }
        let name = String(raw.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        guard !name.isEmpty, name.lowercased() != nml.lowercased(),
              !savedProjs.contains(where: { $0.repos.contains(repo) }) else { continue }
        if let i = allP.firstIndex(where: { $0.name.lowercased() == name.lowercased() }) {
            if !allP[i].repos.contains(repo) { allP[i].repos.append(repo) }
        } else {
            let slug = name.lowercased().replacingOccurrences(of: #"[^a-z0-9_]+"#, with: "_", options: .regularExpression)
            allP.append(Proj(id: "p" + String(slug.prefix(40)), name: name, repos: [repo], dirs: []))
        }
    }
    func under(_ path: String, _ dir: String) -> Bool { path == dir || path.hasPrefix(dir + "/") }
    func repoIn(_ repo: String) -> Int? {
        if let i = allP.firstIndex(where: { $0.repos.contains(repo) }) { return i }
        return allP.firstIndex { p in
            trees.contains { w in
                w["main"] as? Bool == true && w["project"] as? String == repo
                    && p.dirs.contains { d in under(w["path"] as? String ?? "", d) }
            }
        }
    }
    func projIndex(_ w: [String: Any]) -> Int? {
        if let r = w["project"] as? String, !r.isEmpty, let i = repoIn(r) { return i }
        let path = w["path"] as? String ?? ""
        return allP.firstIndex { p in p.dirs.contains { d in under(path, d) } }
    }
    let indices: [Int?] = allP.indices.map { Optional($0) } + [nil]
    return indices.map { idx in
        let id = idx.map { allP[$0].id } ?? ""
        let name = idx.map { allP[$0].name } ?? nml
        let color = idx.map { palette[$0 % palette.count] } ?? "#8E8E93"
        if fallback && trees.isEmpty {
            let rs = idx.map { allP[$0].repos } ?? [], ps = idx.map { allP[$0].dirs } ?? []
            return ["id": id, "name": name, "color": color, "repos": rs, "paths": ps, "busy": 0]
        }
        let mine: ([String: Any]) -> Bool = { projIndex($0) == idx }
        var seenR = Set<String>(), rs = [String]()
        for w in trees {
            guard let r = w["project"] as? String, !r.isEmpty, !seenR.contains(r), repoIn(r) == idx else { continue }
            if trees.allSatisfy({ ($0["project"] as? String != r) || mine($0) }) { seenR.insert(r); rs.append(r) }
        }
        let paths = trees.filter(mine).compactMap { $0["path"] as? String }
        let busy = trees.filter { ($0["agent"] as? String == "working") && mine($0) }.count
        return ["id": id, "name": name, "color": color, "repos": rs, "paths": paths, "busy": busy]
    }
}
final class App: NSObject, NSApplicationDelegate, NSWindowDelegate, NSSplitViewDelegate, UNUserNotificationCenterDelegate, WKScriptMessageHandler, WKNavigationDelegate, NSMenuItemValidation {
    /// A window with chat docked: 480pt of dashboard, a 320pt chat pane and the thin divider between them.
    static let paneMinWidth: CGFloat = 480 + 320 + 1

    /// Widen `w` to at least `minW`, shifted left when that would run past its screen's right edge.
    static func widen(_ w: NSWindow, to minW: CGFloat, display: Bool) {
        guard w.frame.width < minW else { return }
        var f = w.frame
        f.size.width = minW
        if let vf = (w.screen ?? NSScreen.main)?.visibleFrame, f.maxX > vf.maxX { f.origin.x = max(vf.minX, vf.maxX - f.width) }
        w.setFrame(f, display: display)
    }

    var item: NSStatusItem?
    let greenlight = Core()
    var timer: Timer?
    var uitestServer: UITestServer?

    /// Presents a window normally, or keeps it silently in the background (`orderBack`, click-through) under `--uitest`.
    func presentWindow(_ w: NSWindow, activate: Bool = true) {
        if uitest {
            w.ignoresMouseEvents = true
            w.orderBack(nil)
        } else {
            w.makeKeyAndOrderFront(nil)
            if activate { NSApp.activate(ignoringOtherApps: true) }
        }
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { [weak self] in
            if let w = $0.object as? NSWindow, w === self?.window { _ = self?.closeShare(w) }  // Center has no delegate: its close button, as a guest
        }
        for n in [NSWindow.willEnterFullScreenNotification, NSWindow.didEnterFullScreenNotification, NSWindow.willExitFullScreenNotification, NSWindow.didExitFullScreenNotification] {  // the main window has no delegate
            NotificationCenter.default.addObserver(forName: n, object: nil, queue: .main) { [weak self] in ($0.object as? NSWindow).map { w in self?.fullScreen(n, w) } }
        }
        for n in [NSWindow.didBecomeKeyNotification, NSWindow.didResignKeyNotification] {  // resign: the tab you were on as you went to the chat
            NotificationCenter.default.addObserver(forName: n, object: nil, queue: .main) { [weak self] in ($0.object as? NSWindow).map { self?.noteFocus($0) } }
        }
        greenlight.scope = { [weak self] in self?.pickScope }
        if !uitest {
            greenlight.showDockIcon()
            if greenlight.engine == "openai" { DispatchQueue.global().async { _ = openAIKey() } }  // the login shell it may run, before dictation waits on it (voice mode: Speech.warm)
            Speech.shared.warm()
        }
        signal(SIGPIPE, SIG_IGN)  // an ssh can die between our write and its read
        Diag.launched(host: host)
        if !uitest && !selftest { offerImport() }  // a self-test must never use up the real one-time offer
        if !uitest {
            let s = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            s.button?.imagePosition = .imageLeading
            s.button?.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
            s.button?.target = self
            s.button?.action = #selector(click)
            s.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
            if let devName { s.button?.toolTip = "Dev copy of the \(devName) workspace" }
            item = s
        }
        greenlight.popoverShown = { [weak self] in NSApp.isActive && self?.chatShown == true }
        greenlight.popChat = { [weak self] in self?.popChat(out: $0) }
        greenlight.openPage = { [weak self] name in  // as the page's own openPage message
            guard let self, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil else { return }
            self.greenlight.inboxOpen = false
            self.web(showing: "hex").callJS("window.mc && mc.openPage", [name])
            if let w = self.viewWindows["hex"] { self.presentWindow(w, activate: false) } else { self.show() }
        }
        greenlight.reloadFluid = { [weak self] in self?.loadFluid() }
        greenlight.unattach = { [weak self] key in
            let k = (try? JSONSerialization.data(withJSONObject: [key])).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
            self?.mainWebs.forEach { $0.evaluateJavaScript("window.mc && mc.undock(\(k)[0])") }
        }
        orbMonitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] e in  // a click brings its window up: put the orbs back on top
            if let self, e.window != nil, e.window !== self.termOrb { self.raiseOrbs() }
            return e
        }
        vimMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            let paste = e.modifierFlags.intersection([.command, .control, .option, .shift]) == .command
                && { let ch = e.charactersIgnoringModifiers?.lowercased(); return ch == "v" || (Int(e.keyCode) == kVK_ANSI_V && ch?.first?.isASCII != true) }()  // the layout's V; a non-Latin layout's on the V key
            if let self, self.capture == nil, paste, let w = e.window, let c = self.stageUnder(w) { return c.pasteFiles() ? nil : e }  // voice mode's stage: ⌘V of files or an image attaches them
            guard let self, self.capture == nil, let tv = e.window?.firstResponder as? NSTextView,
                  let c = tv.isDescendant(of: self.chat) ? self.greenlight : self.l1s.values.first(where: { tv.isDescendant(of: $0.chat) })?.c else { return e }
            if paste, !c.editingName && !c.inboxOpen && !c.settingsOpen && c.replyingTo == nil, c.pasteFiles() { return nil }  // in the composer only  // ⌘V of files or an image attaches them
            if c.historyKey(e, tv) {  // ↑↓ / ⌃R history and readline keys, ahead of vim (whose ⌃R would be redo)
                guard c.toEnd else { return nil }
                c.toEnd = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak w = e.window] in  // caret to the end once the text lands
                    if let tv = w?.firstResponder as? NSTextView { tv.setSelectedRange(NSRange(location: (tv.string as NSString).length, length: 0)) }
                }
                return nil
            }
            guard c.vim else { return e }
            let m = e.modifierFlags.intersection([.command, .control, .option])
            if m.contains(.command) || m.contains(.option) { return e }  // ⌘A, ⌘V… still work
            let esc = e.keyCode == kVK_Escape || (m == .control && e.charactersIgnoringModifiers == "[")
            let passthrough = [kVK_Return, kVK_ANSI_KeypadEnter, kVK_Delete, kVK_ForwardDelete, kVK_Tab,
                               kVK_LeftArrow, kVK_RightArrow, kVK_UpArrow, kVK_DownArrow, kVK_Home, kVK_End]
            if !esc && passthrough.contains(Int(e.keyCode)) { return e }  // Enter still sends
            let k = esc ? "\u{1b}" : m == .control ? "^" + (e.charactersIgnoringModifiers ?? "") : e.characters ?? ""
            return c.vimKeys.key(k, tv) ? nil : e
        }
        floorMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in  // ⌘↑ up a floor (B2 → B1 → L1 → B2), ⌘↓ down, round the loop
            if let self, self.capture == nil, self.greenlight.inboxOpen, e.window === self.window, !(e.window?.firstResponder is NSTextView),
               self.greenlight.inboxKey(e) { return nil }  // the inbox's keys, unless a reply is being typed
            if let self, self.capture == nil, e.keyCode == kVK_Escape, self.greenlight.voice.on || self.greenlight.voice.starting,  // Esc leaves live chat, or stops it starting (sheets keep theirs)
               !self.greenlight.settingsOpen, !self.greenlight.inboxOpen { self.greenlight.voice.stopAll(); return nil }
            guard let self, self.capture == nil, e.modifierFlags.intersection([.command, .control, .option, .shift]) == .command,
                  e.keyCode == kVK_UpArrow || e.keyCode == kVK_DownArrow,
                  let w = e.window, let f = self.floorGo[ObjectIdentifier(w)] else { return e }
            let order = Floors.levels.map(\.0), n = order.count, i = order.firstIndex(of: f.0.at) ?? 0
            f.1(order[(i + (e.keyCode == kVK_UpArrow ? n - 1 : 1)) % n])
            return nil
        }
        hintSink = greenlight.$hint.receive(on: RunLoop.main).sink { [weak self] in guard let self else { return }; self.hint($0, from: self.greenlight) }
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        tick()
        if devDir == nil && (ud.bool(forKey: "autorun") || hasSavedTermTabs(try? Data(contentsOf: termState(0)))) { _ = termWindow }  // auto-run and restored tabs' alerts live in the Basement's page: load it, hidden. Not in a copy: it never auto-runs
        if let devDir { watchPages(devDir + "/web") }

        if !uitest {
            let un = UNUserNotificationCenter.current()
            un.delegate = self
            if devDir == nil { un.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in } }  // a copy never posts (notify), so never asks
            let reply = UNTextInputNotificationAction(identifier: "reply", title: "Reply", options: [], textInputButtonTitle: "Send", textInputPlaceholder: "Answer greenlight…")
            un.setNotificationCategories([UNNotificationCategory(identifier: "greenlight", actions: [reply], intentIdentifiers: [])])
            un.getNotificationSettings { s in NSLog("greenlight notifications: authorization %d, alert %d", s.authorizationStatus.rawValue, s.alertSetting.rawValue) }
        }
        greenlight.tailOutbox()
        pushTrees()
        streamDash()
        rescan()
        if !uitest { hotKey() }
        mainMenu()
        detached.forEach(detach)  // views the user gave their own windows last time
        reopenTermWindows()
        reopenL1s()
        if uitest {
            window.setFrame(NSRect(x: 40, y: 40, width: 1300, height: 900), display: true)
            termWindow.setFrame(NSRect(x: 40, y: 40, width: 1200, height: 800), display: true)
            basement()
            if let sock = uitestSocketPath {
                let srv = UITestServer(app: self, socketPath: sock)
                uitestServer = srv
                srv.start()
            }
        } else {
            greenlight.termOpens == "greenlight" ? show() : basement()  // Settings → Terminal → App opens to
        }
    }

    lazy var chat: NSHostingView<ChatView> = { let h = NSHostingView(rootView: ChatView(c: greenlight)); h.sizingOptions = []; return h }()  // the pane sets the width, SwiftUI fits it
    lazy var settingsHost: NSHostingView<SettingsWindowView> = { let h = NSHostingView(rootView: SettingsWindowView(c: greenlight)); h.sizingOptions = []; return h }()
    /// Settings: its own window, over whichever floor you're on; closing it leaves you there
    lazy var settingsWindow: NSWindow = {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 720), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.title = "Settings"; w.titleVisibility = .hidden; w.titlebarAppearsTransparent = true
        w.isReleasedWhenClosed = false
        let box = NSView()
        settingsHost.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(settingsHost)
        NSLayoutConstraint.activate([settingsHost.leadingAnchor.constraint(equalTo: box.leadingAnchor), settingsHost.trailingAnchor.constraint(equalTo: box.trailingAnchor),
                                     settingsHost.topAnchor.constraint(equalTo: box.topAnchor), settingsHost.bottomAnchor.constraint(equalTo: box.bottomAnchor)])
        w.contentView = box; w.minSize = NSSize(width: 620, height: 460)
        w.center(); w.setFrameAutosaveName("settings")
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: w, queue: .main) { [weak self] _ in self?.greenlight.settingsOpen = false }
        return w
    }()
    lazy var inboxHost: NSHostingView<InboxOverlay> = { let h = NSHostingView(rootView: InboxOverlay(c: greenlight)); h.sizingOptions = []; return h }()
    var settingsSub: AnyCancellable?, inboxSub: AnyCancellable?
    lazy var fluid: WKWebView = makeWeb()
    /// Web views whose page has drawn once (the dashboard's "ready", the terminal's "painted": its restored layout is up). §10.1: a
    /// window shows only after its web view first renders, so it never flashes white or black: until then it's ordered in at alpha 0,
    /// and comes up when the page says so (or after 2 s).
    var paintedWebs = Set<ObjectIdentifier>()
    var paintWaits = [ObjectIdentifier: [() -> Void]]()  // whenPainted's callers, by web view
    func revealOnPaint(_ w: NSWindow, _ web: WKWebView) {
        guard !uitest, !w.isVisible, !paintedWebs.contains(ObjectIdentifier(web)) else { return }
        w.alphaValue = 0
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self, weak w] in if let w, w !== self?.rideTo { w.alphaValue = 1 } }  // a page that never says ready still shows; a ride shows its own level at its end
    }
    func painted(_ web: WKWebView) {
        paintedWebs.insert(ObjectIdentifier(web))
        DispatchQueue.main.async { [weak self] in self?.flushPaintWaits(ObjectIdentifier(web)) }
        guard let w = web.window, w.alphaValue < 1, w !== rideTo else { return }  // the level a ride is bringing in: the ride shows it, at its end
        DispatchQueue.main.async { [weak w] in w?.alphaValue = 1 }  // the next turn of the run loop: the page's first frame is up
    }
    /// `f` once `web` has drawn (now, if it has), or after `within` seconds: a ride's picture of a cold Basement waits for it.
    func whenPainted(_ web: WKWebView?, within: Double, _ f: @escaping () -> Void) {
        guard let web, !uitest, !paintedWebs.contains(ObjectIdentifier(web)) else { return f() }
        let id = ObjectIdentifier(web)
        paintWaits[id, default: []].append(f)
        DispatchQueue.main.asyncAfter(deadline: .now() + within) { [weak self] in self?.flushPaintWaits(id) }
    }
    func flushPaintWaits(_ id: ObjectIdentifier) { (paintWaits.removeValue(forKey: id) ?? []).forEach { $0() } }
    func makeWeb() -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "mc")  // dashboard.html → app: ready, page
        UITestServer.installErrorCollector(cfg)
        let v = WKWebView(frame: .zero, configuration: cfg)
        v.navigationDelegate = self
        // The canvas, not white or black, while the page repaints during a divider drag or resize (§10.1).
        v.setValue(false, forKey: "drawsBackground")
        v.underPageBackgroundColor = dsDynamic(DS.Palette.bgCanvas)
        return v
    }

    // MARK: views in their own windows (agents), so each can live on any display; the hive stays in the main window

    static let viewTitles = ["agents": "Agents"]
    var detached = ud.stringArray(forKey: "detached")?.filter { viewTitles[$0] != nil } ?? [] { didSet { ud.set(detached, forKey: "detached") } }
    var viewWindows = [String: NSWindow]()
    var mainWebs: [WKWebView] { [fluid] + viewWindows.values.compactMap { $0.contentView as? WKWebView } }  // the main window's pick, chat and lit hexes
    var webs: [WKWebView] { mainWebs + l1s.values.map(\.web) }
    func broadcast(_ js: String) { webs.forEach { $0.evaluateJavaScript(js) } }
    /// The webview that shows a view: its own window if it has one, else the main window.
    func web(showing v: String) -> WKWebView { viewWindows[v]?.contentView as? WKWebView ?? fluid }

    func detach(_ v: String) {
        guard let title = App.viewTitles[v] else { return }
        if let w = viewWindows[v] { w.makeKeyAndOrderFront(nil); return }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: v == "agents" ? 900 : 1000, height: v == "agents" ? 420 : 640),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        w.title = title
        w.backgroundColor = dsDynamic(DS.Palette.bgCanvas)  // §10.1: the canvas, never black
        w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: 320, height: 160)
        w.delegate = self
        let web = makeWeb()
        w.contentView = web
        if let u = page("dashboard") { web.loadFileURL(u, allowingReadAccessTo: u.deletingLastPathComponent()) }
        if !w.setFrameUsingName("view-" + v) { w.center() }
        w.setFrameAutosaveName("view-" + v)
        viewWindows[v] = w
        if !detached.contains(v) { detached.append(v) }
        w.makeKeyAndOrderFront(nil)
        pushMode()
    }

    /// The chat whose voice-mode stage has the keys in window w: the stage itself (its web view) or nothing in particular
    /// (the window), never a text field or another pane (a terminal's ⌘V is its own), nor a pane that's hidden. Its sheets (Settings, the inbox) aren't the stage.
    func stageUnder(_ w: NSWindow) -> Core? {
        let r = w.firstResponder
        return ([(chat as NSView, greenlight)] + l1s.values.map { ($0.chat as NSView, $0.c) }).first { v, c in
            c.voice.on && !c.voice.transcript && !c.settingsOpen && !c.inboxOpen && v.window === w && !v.isHiddenOrHasHiddenAncestor && !v.visibleRect.isEmpty
                && (r === w || ((r as? NSView).map { $0.isDescendant(of: v) && !($0 is NSTextView) } ?? false))
        }?.1
    }
    /// Closing a view's window hands the view back to the main window.
    func attach(_ v: String) {
        guard let w = viewWindows.removeValue(forKey: v) else { return }
        if let web = w.contentView as? WKWebView {
            let id = ObjectIdentifier(web)
            readyDash.remove(id); waitingDash.remove(id)
            web.configuration.userContentController.removeScriptMessageHandler(forName: "mc")  // it retains self
        }
        detached.removeAll { $0 == v }
        w.orderOut(nil)
        pushMode()
    }

    func modeJSON(for web: WKWebView) -> String {
        if let v = viewWindows.first(where: { $0.value.contentView === web })?.key { return #"{"only":"\#(v)"}"# }
        return #"{"away":[\#(viewWindows.keys.map { "\"\($0)\"" }.joined(separator: ","))]}"#
    }
    var picked: [String] = []  // one Chief, one set of lit hexes: every window shows the same, a window that (re)loads gets them too
    var pickOpen = false  // and whether the lone lit hex's card opens (System One was sure, or the reply named it)
    func pick(_ names: [String], open: Bool = false) {
        picked = names.reduce(into: []) { if !$0.contains($1) { $0.append($1) } }
        pickOpen = open
        webs.forEach(pushPicks)
    }
    /// One Chief for every window: it's told which one you were last in (a terminal's tab, the dashboard's open workspace), so
    /// "this one" or "merge it" means that workspace. The chat window itself doesn't count.
    func noteFocus(_ w: NSWindow) {
        guard w !== chatWindow, let web = w.contentView as? WKWebView ?? webs.first(where: { $0.window === w }) else { return }
        let id = termWins.first { $0.value === w }?.key
        // a second Greenlight Center window tells only its own Chief; a terminal (every project) tells them all
        let cores = l1s.values.first { $0.w === w }.map { [$0.c] } ?? (id != nil ? [greenlight] + l1s.values.map(\.c) : [greenlight])
        web.evaluateJavaScript(id != nil ? "JSON.stringify(window.term && term.focusInfo && term.focusInfo())" : "JSON.stringify(window.mc && mc.focus && mc.focus())") { [weak self] r, _ in
            guard let self else { return }
            let o = ((r as? String).flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) } as? [String: Any]) ?? [:]
            let line = focusLine(terminal: id, project: id.flatMap { self.floors[$0]?.project } ?? "", o)
            let at = id != nil ? ("path", o["path"] as? String) : ("name", o["page"] as? String)  // DWIM's "this": that window's workspace
            let name = at.1.flatMap { k in self.greenlight.pucks.first { $0[at.0] as? String == k }?["name"] as? String }
            cores.forEach { $0.focusName = name }
            guard cores.contains(where: { $0.focus != line }) else { return }
            cores.forEach { $0.focus = line }
            guard cores[0] === self.greenlight else { return }
            let key = id != nil ? ("path", o["path"] as? String) : ("name", o["page"] as? String)  // voice mode: the stage follows you
            if let k = key.1, let p = self.greenlight.pucks.first(where: { $0[key.0] as? String == k }) { self.greenlight.voice.show(workspaceSpec(p)) }
        }
    }
    func pushPicks(_ web: WKWebView) {
        guard let d = try? JSONSerialization.data(withJSONObject: l1(web)?.picked ?? picked), let j = String(data: d, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.mc && mc.pick && mc.pick(\(j), \(l1(web)?.pickOpen ?? pickOpen))")
    }
    func pushMode() { webs.forEach { $0.evaluateJavaScript("window.mc && mc.mode(\(modeJSON(for: $0)))") } }
    lazy var window: NSWindow = {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = greenlight.name
        w.backgroundColor = dsDynamic(DS.Palette.bgCanvas)  // §10.1: the canvas, never black
        w.titleVisibility = .hidden  // the floor bar says Center; the title stays for the Window menu and Mission Control
        w.isReleasedWhenClosed = false  // the close button hides it; the menubar star, Dock or ⌃⌥M bring it back
        w.minSize = centerMinSize
        w.animationBehavior = greenlight.rideTime > 0 ? .default : .none
        l1Floor.at = "greenlight"; syncL1()
        let go: (String) -> Void = { [unowned self] f in  // down from Greenlight: the Basement or the Pipe
            wantFloor = f; (termWindow.contentView as? WKWebView)?.callJS("window.term && term.opens", [f, !termWindow.isVisible]); ride(up: false) }
        floorGo[ObjectIdentifier(w)] = (l1Floor, go)
        floorBar(w, Floors(s: l1Floor, c: greenlight, pick: { [unowned self] in l1Pick() }, go: go))
        let root = NSView()  // the dashboard and its overlays, left of the chat
        for v in [fluid, inboxHost] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
            NSLayoutConstraint.activate([v.leadingAnchor.constraint(equalTo: root.leadingAnchor), v.trailingAnchor.constraint(equalTo: root.trailingAnchor),
                                         v.topAnchor.constraint(equalTo: root.topAnchor), v.bottomAnchor.constraint(equalTo: root.bottomAnchor)])
        }
        inboxHost.isHidden = true
        inboxSub = greenlight.$inboxOpen.receive(on: RunLoop.main).sink { [weak self] open in
            guard let self, self.inboxHost.isHidden == open else { return }
            self.inboxHost.isHidden = !open
            if open { self.window.makeFirstResponder(self.inboxHost) }
        }
        settingsSub = greenlight.$settingsOpen.receive(on: RunLoop.main).sink { [weak self] open in
            guard let self else { return }
            if open { (self.hintCore ?? self.greenlight).hint = ""; self.presentWindow(self.settingsWindow); self.settingsWindow.makeFirstResponder(nil) }  // AppKit focuses the search field otherwise  // the gear's hover-exit never comes
            else if self.settingsWindow.isVisible { self.settingsWindow.orderOut(nil) }
        }
        split.isVertical = true
        split.dividerStyle = .thin
        split.delegate = self
        split.addArrangedSubview(root)
        split.addArrangedSubview(chatSlot)
        split.setHoldingPriority(.defaultLow, forSubviewAt: 0)  // a window resize goes to the dashboard
        split.setHoldingPriority(.defaultLow + 1, forSubviewAt: 1)
        for (v, w, pr) in [(root, 480, NSLayoutConstraint.Priority.defaultHigh + 1), (chatSlot, 320, .defaultHigh)] as [(NSView, CGFloat, NSLayoutConstraint.Priority)] {
            let k = v.widthAnchor.constraint(greaterThanOrEqualToConstant: w); k.priority = pr; k.isActive = true  // a window too narrow for both: the chat gives
        }
        w.contentView = split
        split.layoutSubtreeIfNeeded()
        split.setPosition(split.bounds.width - 440, ofDividerAt: 0)
        split.autosaveName = "chatSplit"  // restores a dragged divider over the default
        loadFluid()
        if w.setFrameUsingName("main") { onScreen(w) } else { w.center() }
        w.setFrameAutosaveName("main")
        return w
    }()

    /// A dev copy: reload a page when dev.py lays down a new one (the Swift takes a rebuild, and dev.py relaunches us after it).
    var pageTimes = [String: Date](), pageTimer: Timer?
    func watchPages(_ dir: String) {
        func times() -> [String: Date] {
            var t = [String: Date]()
            for f in (try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? [] where !f.hasPrefix(".") {
                t[f] = (try? FileManager.default.attributesOfItem(atPath: dir + "/" + f))?[.modificationDate] as? Date
            }
            return t
        }
        pageTimes = times()
        pageTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in
            guard let self else { return }
            let now = times(), moved = Set(now.filter { self.pageTimes[$0.key] != $0.value }.keys)
            self.pageTimes = now
            if moved.contains("dashboard.html") { self.webs.forEach { $0.reloadFromOrigin() } }
            if moved.contains("stage.html") { ([self.greenlight] + self.l1s.values.map(\.c)).filter { $0.voice.on }.forEach { $0.voice.stageWeb.reloadFromOrigin() } }
            if !moved.subtracting(["dashboard.html", "stage.html"]).isEmpty { self.termWebs.forEach { $0.reloadFromOrigin() } }  // terminal.html, or a script of its
        }
    }

    func loadFluid(_ into: WKWebView? = nil) {
        let fluid = into ?? fluid, s = greenlight.fluidURL.trimmingCharacters(in: .whitespaces)
        if s.isEmpty, let u = page("dashboard") {
            fluid.loadFileURL(u, allowingReadAccessTo: u.deletingLastPathComponent())
        } else if s.hasPrefix("http://") || s.hasPrefix("https://"), let u = URL(string: s) {
            fluid.load(URLRequest(url: u))
        } else {
            let u = URL(fileURLWithPath: (s as NSString).expandingTildeInPath)
            fluid.loadFileURL(u, allowingReadAccessTo: u.deletingLastPathComponent())
        }
    }

    // MARK: dashboard data (dash.py where greenlight runs, plus local Mac when any project runs there)

    var rescanning = false
    var dashSnapObj: [String: Any]?  // newest validated snapshot (merged across server + local Mac when needed)
    var serverSnapObj: [String: Any]?, localSnapObj: [String: Any]?
    var repoHosts = [String: RepoHostInfo]()  // per-repo host preference & resolved target ("server" | "mac")
    var snapHostByName = [String: String](), snapHostByPath = [String: String]()
    var treeHostByName = [String: String](), treeHostByPath = [String: String]()
    var cachedWsHostByPath: [String: String] = (ud.dictionary(forKey: "wsHostByPathCache") as? [String: String]) ?? [:]
    var wsHostByName = [String: String](), wsHostByPath = [String: String]()  // rebuilt from merged trees + snapshots
    var localScanned = false
    var initialServerTreesDone = false, initialLocalTreesDone = isLocalHost(host)

    var anyMacNeeded: Bool {
        !isLocalHost(host) && (repoHosts.values.contains { $0.target == "mac" }
            || isLocalHost(greenlight.chatHost) || l1s.values.contains { isLocalHost($0.c.chatHost) })
    }

    private func rebuildWorkspaceHostIndex() {
        let fresh = snapHostByPath.merging(treeHostByPath) { _, t in t }  // native trees win over snapshots
        let startupPending = !initialServerTreesDone || !initialLocalTreesDone
        let byPath = startupPending ? cachedWsHostByPath.merging(fresh) { _, f in f } : fresh
        wsHostByPath = byPath
        wsHostByName = snapHostByName.merging(treeHostByName) { _, t in t }
        if byPath != cachedWsHostByPath {
            cachedWsHostByPath = byPath
            ud.set(byPath, forKey: "wsHostByPathCache")
        }
    }

    func workspaceHost(name: String? = nil, path: String? = nil, fallback: String? = nil) -> String {
        if let path, let h = wsHostByPath[path] ?? cachedWsHostByPath[path] { return h }
        if let name, let h = wsHostByName[name] { return h }
        return fallback ?? greenlight.chatHost
    }

    func paneTarget(_ ws: String) -> (host: String, cwd: String)? {
        guard let at = paneWhere(ws) else { return nil }
        if !ws.hasPrefix("@"), !at.cwd.isEmpty, let h = wsHostByPath[at.cwd] ?? cachedWsHostByPath[at.cwd] { return (h, at.cwd) }
        return at
    }

    func mergeRepoHosts(_ incoming: [String: RepoHostInfo]) {
        repoHosts = mergeRepoHostMaps(repoHosts, incoming)
        syncHostForActiveWindows(clearOnChange: false)
        ensureLocalDashIfNeeded()
    }

    func updateRepoHost(_ repo: String, _ info: RepoHostInfo, clearOnChange: Bool = true) {
        repoHosts = setRepoHostInMap(repoHosts, repo: repo, info: info)
        syncHostForActiveWindows(clearOnChange: clearOnChange)
        ensureLocalDashIfNeeded()
    }

    private func effectiveProjList() -> [[String: Any]] {
        projList.isEmpty ? buildProjectList(saved: savedProjects, trees: [], dark: isDark, fallback: true) : projList
    }

    private func canSyncSilently(_ c: Core) -> Bool {
        !initialServerTreesDone || !initialLocalTreesDone || (c.run == nil && !c.msgs.contains { $0.role == .you })
    }

    private func syncHostForActiveWindows(clearOnChange: Bool) {
        let list = effectiveProjList(), saved = savedProjects
        let mainTarget = resolveProjectHost(only: l1Only ?? sharedOnly, projList: list, savedProjects: saved, repoHosts: repoHosts)
        if clearOnChange && mainTarget != greenlight.chatHost {
            let label = projectChip(only: sharedOnly, projList: list, projectsLoaded: true).text
            greenlight.switchProject(to: mainTarget, label: label)
        } else if !clearOnChange && canSyncSilently(greenlight) {
            greenlight.syncChatHost(mainTarget)
        }
        for l in l1s.values {
            let target = resolveProjectHost(only: live(l.only) ?? l.only, projList: list, savedProjects: saved, repoHosts: repoHosts)
            if clearOnChange && target != l.c.chatHost {
                let label = projectChip(only: l.only, projList: list, projectsLoaded: true).text
                l.c.switchProject(to: target, label: label)
            } else if !clearOnChange && canSyncSilently(l.c) {
                l.c.syncChatHost(target)
            }
        }
    }

    private func indexSnapshotHosts(_ obj: [String: Any], fallbackHost: String) {
        var byName = [String: String](), byPath = [String: String]()
        for p in obj["pucks"] as? [[String: Any]] ?? [] where p["nativeHost"] as? Bool != false {
            let h = (p["_runHost"] as? String) ?? fallbackHost
            if let n = p["name"] as? String, !n.isEmpty { byName[n] = h }
            if let path = p["path"] as? String, !path.isEmpty { byPath[path] = h }
        }
        for t in obj["trees"] as? [[String: Any]] ?? [] where t["nativeHost"] as? Bool != false {
            let h = (t["_runHost"] as? String) ?? fallbackHost
            if let path = t["path"] as? String, !path.isEmpty, byPath[path] == nil { byPath[path] = h }
            if t["main"] as? Bool != true, let n = t["name"] as? String, !n.isEmpty, byName[n] == nil { byName[n] = h }
        }
        snapHostByName = byName
        snapHostByPath = byPath
        rebuildWorkspaceHostIndex()
    }

    private func publishDashSnap() {
        let obj: [String: Any]? = (!isLocalHost(host) && localSnapObj != nil)
            ? mergeSnapshots(serverSnapObj, localSnapObj, primaryHost: host, secondaryHost: "local")
            : serverSnapObj ?? localSnapObj
        guard let obj, JSONSerialization.isValidJSONObject(obj),
              let clean = try? JSONSerialization.data(withJSONObject: obj),
              let js = String(data: clean, encoding: .utf8) else { return }
        indexSnapshotHosts(obj, fallbackHost: host)
        dashSnapObj = obj
        for w in webs where readyDash.contains(ObjectIdentifier(w)) {
            w.callJS("window.mc && mc.update", [obj])
        }
        queueBuilds(js)
    }

    /// A fresh greenlight scan, at launch and from the footer's ↻; the stream picks up the new last.json.
    func rescan() {
        guard !rescanning else { return }
        rescanning = true
        greenlight.log(.greenlight, "Greenlight", .action, "rescanning every workspace")
        ssh("\(py)/greenlight.py scan >/dev/null", onExit: { [weak self] code in
            self?.greenlight.acted("Greenlight", "rescan", code, "", tell: false)  // the board, not a workspace: nothing for Chief
            DispatchQueue.main.async { self?.rescanning = false; self?.broadcast("window.mc && mc.rescanned(\(code))") }
        })
        if anyMacNeeded && !uitest && !selftest {
            localScanned = true
            ssh("\(pyCmd(on: "local"))/greenlight.py scan >/dev/null", on: "local")
        }
    }

    var dashProc: Process?, localDashProc: Process?
    func streamDash() {
        dashProc = ssh("\(py)/dash.py stream 5", onLine: { [weak self] l in
            guard let self, l.hasPrefix("{"), let d = l.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  JSONSerialization.isValidJSONObject(obj) else { return }
            if obj["error"] != nil {
                if self.serverSnapObj == nil && self.localSnapObj == nil {
                    for w in self.webs where self.readyDash.contains(ObjectIdentifier(w)) {
                        w.callJS("window.mc && mc.update", [obj])
                    }
                }
                return
            }
            self.serverSnapObj = obj
            self.publishDashSnap()
        }, onExit: { [weak self] _ in DispatchQueue.main.asyncAfter(deadline: .now() + 10) { self?.streamDash() } })
    }

    func ensureLocalDashIfNeeded() {
        guard anyMacNeeded, localDashProc == nil, !uitest, !selftest else { return }
        if !localScanned {
            localScanned = true
            ssh("\(pyCmd(on: "local"))/greenlight.py scan >/dev/null", on: "local")
        }
        streamLocalDash()
    }

    func streamLocalDash() {
        guard !isLocalHost(host), !uitest, !selftest else { return }
        localDashProc = ssh("\(pyCmd(on: "local"))/dash.py stream 5", on: "local", onLine: { [weak self] l in
            guard let self, l.hasPrefix("{"), let d = l.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  JSONSerialization.isValidJSONObject(obj) else { return }
            if obj["error"] != nil { return }  // keep existing snapshots; local scan will populate last.json
            self.localSnapObj = obj
            self.publishDashSnap()
        }, onExit: { [weak self] _ in
            self?.localDashProc = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 10) { self?.ensureLocalDashIfNeeded() }
        })
    }

    // Background page builds: pucks whose layout is stale, greenlight.designParallel at a time.
    var building = Set<String>()
    var tried = [String: String]()  // name -> sig last attempted; retried only once the state moves on

    func queueBuilds(_ l: String) {
        guard let d = l.data(using: .utf8), let snap = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let pucks = snap["pucks"] as? [[String: Any]] else { return }
        greenlight.pucks = pucks
        if case let a = agentsOf(pucks), a != greenlight.agents { greenlight.agents = a }
        greenlight.watched(pucks)
        greenlight.fed(pucks)
        for l in l1s.values { l.c.pucks = pucks; l.c.agents = greenlight.agents; l.c.fed(pucks) }  // the watch is the main chat's
        for p in pucks where building.count < greenlight.designParallel {
            guard let name = p["name"] as? String, let sig = p["sig"] as? String, p["built"] as? Bool == false,
                  p["state"] as? String != "done", !building.contains(name), tried[name] != sig,
                  name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil else { continue }
            building.insert(name); tried[name] = sig
            let h = workspaceHost(name: name, path: p["path"] as? String, fallback: host)
            ssh("\(pyCmd(on: h))/dash.py page '\(name)' --model \(shellQuote(greenlight.designModel)) --effort \(shellQuote(greenlight.designEffort)) >/dev/null", on: h, onExit: { [weak self] _ in
                self?.building.remove(name); self?.pushBuilds()
            })
        }
        pushBuilds()
        queueSuggests(pucks)
    }

    // Background next steps: the hover card's button, written before the card opens. Only for pucks whose card shows
    // the button (agent not working or waiting) and whose snapshot has no current suggestion.
    var suggesting = Set<String>()  // in flight, background or asked for by a page; a second ask waits on the first
    var suggestTried = [String: String]()  // name -> sig last attempted; retried only once the state moves on
    let suggestParallel = 3

    func queueSuggests(_ pucks: [[String: Any]]) {
        for p in pucks where suggesting.count < suggestParallel {
            guard let name = p["name"] as? String, let sig = p["sig"] as? String, p["suggestion"] is NSNull,
                  p["state"] as? String != "done", !suggesting.contains(name), suggestTried[name] != sig,
                  !["working", "waiting"].contains((p["agent"] as? [String: Any])?["state"] as? String ?? ""),
                  name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil else { continue }
            suggestTried[name] = sig
            suggest(name)
        }
    }

    /// dash.py suggest for one workspace; every page gets the answer (mc.suggest), so an open hover card relabels at once.
    func suggest(_ name: String) {
        guard !suggesting.contains(name) else { return }
        suggesting.insert(name)
        var out = ""
        let h = workspaceHost(name: name)
        ssh("\(pyCmd(on: h))/dash.py suggest '\(name)' --model \(shellQuote(greenlight.nextModel)) --effort \(shellQuote(greenlight.nextEffort))", on: h, onLine: { out += $0 }, onExit: { [weak self] code in
            self?.suggesting.remove(name)
            let js = safeJSON(out, fallback: ["name": name, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
            self?.broadcast("window.mc && mc.suggest(\(js))")
            self?.termWebs.forEach { $0.evaluateJavaScript("window.term && term.suggest(\(js))") }  // the sidebar card's button
        })
    }


    var pushedDark = false, pushedTerm = ""  // the first tick pushes both
    var pushedRepos = "", pushedMono = "", pushedBell = "", pushedChat = "", pushedOrb = "", pushedMotion = 0.6, pushedSlide = 0.25, pushedLocal = localDefault
    func pushBuilds() {
        let names = (try? JSONSerialization.data(withJSONObject: building.sorted())).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
        broadcast("window.mc && mc.builds({building: \(names), parallel: \(greenlight.designParallel)})")
    }

    func trustedOrigin(_ web: WKWebView?, for name: String) -> Bool {
        guard let u = web?.url else { return false }
        if u.scheme == "file" { return true }
        if name == "mc" {
            let s = greenlight.fluidURL.trimmingCharacters(in: .whitespaces)
            if let cfg = URL(string: s), ["http", "https"].contains(cfg.scheme?.lowercased() ?? "") {
                return u.scheme?.lowercased() == cfg.scheme?.lowercased() && u.host?.lowercased() == cfg.host?.lowercased() && u.port == cfg.port
            }
        }
        return false
    }

    func userContentController(_ uc: WKUserContentController, didReceive m: WKScriptMessage) {
        guard m.frameInfo.isMainFrame, trustedOrigin(m.webView, for: m.name) else { return }
        if m.name == "term" {
            if let b = m.body as? [String: Any], let cmd = b["cmd"] as? String, let w = m.webView {
                if cmd == "ready" { paintedWebs.remove(ObjectIdentifier(w)) }  // a (re)loaded page: nothing of it drawn yet
                if cmd == "painted" { painted(w) }
            }
            if let b = m.body as? [String: Any] { termMessage(b, from: m.webView) }
            return
        }
        guard let b = m.body as? [String: Any], let cmd = b["cmd"] as? String, let from = m.webView else { return }
        let l = l1(from)  // a second Greenlight Center window: its own projects and chat
        if cmd == "ready", let l { l.pushedRepos = ""; l.pushedChat = ""; l.c.attached = [] }
        if cmd == "ready" {
            painted(from)
            from.evaluateJavaScript("mc.mode(\(modeJSON(for: from)))")
            let id = ObjectIdentifier(from)
            if canShowDash(for: from) {
                waitingDash.remove(id); readyDash.insert(id)
                pushPaths(to: from)
                if let snap = dashSnapObj { from.callJS("window.mc && mc.update", [snap]) }
            } else {
                readyDash.remove(id); waitingDash.insert(id)
                if treesProc == nil { pushTrees() }
            }
            pushBuilds(); pushPicks(from)
            if from === web(showing: "hex") { greenlight.attached = [] }  // a reloaded hive starts with an empty dock
            pushedMono = ""; pushedBell = ""; pushedChat = ""; pushedOrb = ""  // tick sends the code font, the bell and the orb's room
        }
        if cmd == "error" {
            NSLog("greenlight page error: %@", String(describing: b["m"] ?? ""))
            Diag.event("js_error", ["page": "dashboard", "name": b["name"] ?? NSNull(), "line": b["line"] ?? NSNull(), "col": b["col"] ?? NSNull()])
        } else if cmd != "ready" && cmd != "context" { Diag.event("action", ["cmd": cmd]) }  // the command, never its payload
        if ["run", "merge", "undraft", "signoff", "ask", "answer", "answerPing", "park", "task", "basement", "editPrompt", "steward"].contains(cmd) { greenlight.active() }
        if cmd == "detach", let v = b["view"] as? String { detach(v) }
        if cmd == "unpick" {  // a click on a lit hex clears them in every window (on its chat's)
            if let gone = b["gone"] as? [String] {  // a refresh dropped these: every window already let go of them, so just forget them, or a reload lights them again
                if let l { l.picked.removeAll(where: gone.contains) } else { picked.removeAll(where: gone.contains) }
            } else if let l { l.c.light([], false) } else { pick([]) }
        }
        if cmd == "project" {  // L1's Open projects (or Clear filters: null)
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil || only!.count > 100 { return }
            if let l {
                let oldOnly = livePick(l.only, known: nil), newOnly = livePick(only, known: nil)
                l.only = newOnly; saveL1s(); syncL1(); syncDashPaths()
                if newOnly != oldOnly {
                    let list = effectiveProjList()
                    let target = resolveProjectHost(only: live(newOnly) ?? newOnly, projList: list, savedProjects: savedProjects, repoHosts: repoHosts)
                    let label = projectChip(only: newOnly, projList: list, projectsLoaded: true).text
                    l.c.switchProject(to: target, label: label)
                    ensureLocalDashIfNeeded()
                }
            } else { setPick(only) }  // the main one: one pick for every floor and terminal window
        }
        if cmd == "window" {  // L1's Open projects, New window: another Greenlight Center, with its own Chief
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil || only!.count > 100 { return }
            newL1(only, from: from.window)
        }
        if cmd == "pickNew" { l1Pick(here: false, in: l, on: from) }  // ⌘⇧N on L1 (a detached view: there)
        if cmd == "editProjects" {  // L1's Edit projects…: down to the Basement, where the editor lives
            wantFloor = "basement"; let tw = termWindow.contentView as? WKWebView
            tw?.callJS("window.term && term.opens", ["basement", !termWindow.isVisible]); tw?.evaluateJavaScript("window.editProjects && editProjects()"); ride(up: false)  // safety-ok: a constant
        }
        // A click in one window that belongs to a view in another: forward it there and bring that window up.
        if cmd == "openPage", let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil {
            fluid.callJS("window.mc && mc.openPage", [name]); show()  // the hive lives in the main window
        }
        // A workspace's Basement button: down to B1, on that worktree.
        if cmd == "basement", let path = b["path"] as? String, path.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil, !path.contains("..") { toWorkspace(path, resume: resumeOf(b["resume"])) }
        if cmd == "showChat", l == nil { setChat(hidden: false) }  // a second window's chat is always in its pane
        if cmd == "settings" { appSettings() }  // the top bar's gear: its own window, to the front
        if cmd == "inbox" { greenlight.settingsOpen = false; if l != nil { show(); greenlight.inboxOpen = true } else { greenlight.inboxOpen.toggle() } }  // one inbox, the main window's
        // An answer from a lifted workspace's page: the same as answering that question in the bell.
        if cmd == "answerPing", let id = b["id"] as? String, let text = b["text"] as? String,
           !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if let q = greenlight.waiting.first(where: { $0.id.uuidString == id }) { b["dismiss"] as? Bool == true ? greenlight.answer(q, "(dismissed)", send: false) : greenlight.answer(q, text) }
            pushedBell = ""; tick()  // re-push even when it was already answered elsewhere, so the page drops its "Sending…"
        }
        if cmd == "toggleChat", l == nil { appToggleChat() }
        if cmd == "context", let items = b["items"] as? [[String: Any]] {
            let c = l?.c ?? greenlight
            let was = Dictionary(c.attached.map { ($0.id, $0.sent) }, uniquingKeysWith: { a, _ in a })
            c.attached = items.compactMap { i in
                guard let k = i["key"] as? String, let l = i["label"] as? String, let t = i["text"] as? String else { return nil }
                return Attached(id: k, label: l, color: hexColor(i["color"] as? String ?? ""), mrs: i["mrs"] as? Int ?? 1, text: String(t.prefix(20000)), sent: was[k] ?? false)
            }
        }
        if cmd == "rescan" { rescan() }
        // Names go into a shell command: allow only what workspace names use (dash.py checks again).
        let isWsName: (String) -> Bool = { $0.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil }
        let badNameErr = "Unsupported characters in workspace name."
        let chief = l?.c ?? greenlight  // this window's Chief hears what its buttons did (Core.acted)
        let winHost = chief.chatHost
        if cmd == "page", let w = from.window { noteFocus(w) }  // a workspace opened while you're in that window
        if cmd == "page", let name = b["name"] as? String {
            guard isWsName(name) else { from.callJS("window.mc && mc.page", [["name": name, "error": badNameErr]]); return }
            let fresh = b["fresh"] as? Bool == true ? " --fresh" : ""
            var out = ""
            let h = workspaceHost(name: name, fallback: winHost)
            ssh("\(pyCmd(on: h))/dash.py page '\(name)'\(fresh) --model \(shellQuote(greenlight.designModel)) --effort \(shellQuote(greenlight.designEffort))", on: h, onLine: { out += $0 }, onExit: { code in
                let js = safeJSON(out, fallback: ["name": name, "error": "couldn't load this page from \(hostDisplayName(h)) (exit \(code))"])
                from.evaluateJavaScript("mc.page(\(js))")
            })
        }
        // The agent card's All output: a page of the workspace's agent conversation (newest, or the one before a cursor), polled while it's open.
        if cmd == "transcript", let name = b["name"] as? String {
            let before = b["before"] as? String ?? ""
            let badBefore = !before.isEmpty && before.range(of: #"^[\w.-]{1,200}:\d{1,9}$"#, options: .regularExpression) == nil
            guard isWsName(name), !badBefore else {
                let err: [String: Any] = ["name": name, "before": before.isEmpty ? NSNull() : before, "error": badNameErr]
                from.callJS("window.mc && mc.transcript", [err])
                return
            }
            var out = ""
            let h = workspaceHost(name: name, fallback: winHost)
            ssh("\(pyCmd(on: h))/dash.py transcript '\(name)'\(before.isEmpty ? "" : " --before '\(before)'")", on: h, onLine: { out += $0 }, onExit: { code in
                let js = safeJSON(out, fallback: ["name": name, "before": before.isEmpty ? NSNull() : before, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
                from.evaluateJavaScript("mc.transcript(\(js))")
            })
        }
        // All output's extras: the sessions' tokens and cost, the files touched (logextras); a redacted copy of the log to share (logshare).
        if cmd == "logextras" || cmd == "logshare", let name = b["name"] as? String {
            let text = String((b["text"] as? String ?? "").prefix(4 << 20)), cb = cmd == "logextras" ? "mc.logextras" : "mc.logshared"
            guard isWsName(name) else { from.callJS("window.mc && \(cb)", [["name": name, "error": badNameErr]]); return }
            var out = ""
            let h = workspaceHost(name: name, fallback: winHost), req = max(0, min(b["req"] as? Int ?? 0, 1_000_000_000))  // a share's answer names its ask
            ssh("\(pyCmd(on: h))/dash.py \(cmd) '\(name)' --req \(req)", on: h, stdin: text, onLine: { out += $0 }, onExit: { code in
                let js = safeJSON(out, fallback: ["name": name, "req": req, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
                from.evaluateJavaScript("window.mc && \(cb)(\(js))")  // safety-ok: safeJSON is a JSON literal
            })
        }
        // Share log…'s Copy and Save as file…: text the page already redacted (logshare).
        if cmd == "copyText", let text = b["text"] as? String {
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString(String(text.prefix(4 << 20)), forType: .string)
        }
        if cmd == "saveText", let text = b["text"] as? String {
            let s = NSSavePanel()
            let n = (b["filename"] as? String ?? "").replacingOccurrences(of: #"[^\w.-]"#, with: "-", options: .regularExpression)
            s.nameFieldStringValue = n.isEmpty ? "agent-log.txt" : String(n.prefix(80))
            guard s.runModal() == .OK, let u = s.url else { return }
            do { try String(text.prefix(4 << 20)).write(to: u, atomically: true, encoding: .utf8); from.callJS("window.mc && mc.saved", [["ok": true]]) }
            catch { from.callJS("window.mc && mc.saved", [["ok": false, "error": error.localizedDescription]]) }
        }
        // A waiting agent's question (ask) and your reply to it (answer), from the Answer button or the page.
        if cmd == "ask" || cmd == "answer", let name = b["name"] as? String {
            let text = b["text"] as? String ?? ""
            if cmd == "answer" && text.isEmpty { return }
            guard isWsName(name) else {
                from.callJS(cmd == "ask" ? "window.mc && mc.asked" : "window.mc && mc.answered", [["name": name, "error": badNameErr]])
                return
            }
            let h = workspaceHost(name: name, fallback: winHost), pyH = pyCmd(on: h)
            let chat = b["chat"] as? Bool == true  // "Chat about this": talk the question over, don't answer it
            let remote = cmd == "answer" ? "\(pyH)/dash.py answer '\(name)'\(chat ? " --chat" : "")"
                : "\(pyH)/dash.py ask '\(name)' --model \(shellQuote(greenlight.designModel)) --effort \(shellQuote(greenlight.designEffort))"
            var out = ""
            if cmd == "answer" { greenlight.log(.greenlight, name, .action, "\(chat ? "chatting with" : "answering") its agent: \(text)") }
            ssh(remote, on: h, stdin: text, onLine: { out += $0 }, onExit: { code in
                if cmd == "answer" { chief.acted(name, "answer its agent", code, out) }
                let js = safeJSON(out, fallback: ["name": name, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
                from.evaluateJavaScript("mc.\(cmd == "ask" ? "asked" : "answered")(\(js))")
            })
        }
        // The hover card's Next step: greenlight's next-step prompt for a workspace, and running a prompt there as a new agent.
        if cmd == "park", let name = b["name"] as? String, let on = b["on"] as? Bool {
            guard isWsName(name) else { from.callJS("window.mc && mc.parked", [["name": name, "error": badNameErr]]); return }
            var out = ""
            let h = workspaceHost(name: name, fallback: winHost)
            greenlight.log(.greenlight, name, .action, on ? "parking" : "unparking")
            ssh("\(pyCmd(on: h))/dash.py park '\(name)' \(on ? "on" : "off")", on: h, onLine: { out += $0 }, onExit: { code in
                chief.acted(name, on ? "park" : "unpark", code, out)
                let js = safeJSON(out, fallback: ["name": name, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
                from.evaluateJavaScript("mc.parked(\(js))")
            })
        }
        // Steward mode & drift confirmation on a workspace card: dash.py steward on|off|confirm <name>
        if cmd == "steward", let op = b["op"] as? String, ["on", "off", "confirm"].contains(op),
           let name = b["name"] as? String {
            guard isWsName(name) else { from.callJS("window.mc && mc.steward", [["name": name, "error": badNameErr]]); return }
            var out = ""
            let h = workspaceHost(name: name, fallback: winHost)
            greenlight.log(.greenlight, name, .action, "steward \(op)")
            ssh("\(pyCmd(on: h))/dash.py steward \(op) \(shellQuote(name))", on: h, onLine: { out += $0 }, onExit: { [weak self] code in
                chief.acted(name, "steward \(op)", code, out)
                let obj = (out.data(using: .utf8).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] })
                    ?? ["op": op, "name": name, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"]
                for w in self?.webs ?? [] where self?.readyDash.contains(ObjectIdentifier(w)) == true {
                    w.callJS("window.mc && mc.steward", [obj])
                }
            })
        }
        // New task: dash.py task repos | new <repo>; runs on the repo's host (or the window's active Chief host).
        if cmd == "task", let op = b["op"] as? String, ["repos", "new"].contains(op),
           let arg = b["arg"] as? String, isWsName(arg) {
            var out = ""
            let h = op == "new" ? resolveRepoTargetHost(arg, repoHosts: repoHosts, defaultHost: winHost) : winHost
            ssh("\(pyCmd(on: h))/dash.py task \(op) '\(arg)'", on: h, stdin: b["text"] as? String ?? "", onLine: { out += $0 }, onExit: { code in
                if op == "new" { let made = chief.made(out); chief.acted(made ?? arg, made == nil ? "start a task" : "start a task in \(arg)", code, out) }
                let js = safeJSON(out, fallback: ["op": op, "arg": arg, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
                from.evaluateJavaScript("mc.tasked(\(js))")
            })
        }
        if cmd == "suggest", let name = b["name"] as? String {
            guard isWsName(name) else { from.callJS("window.mc && mc.suggest", [["name": name, "error": badNameErr]]); return }
            suggest(name)  // already in flight (the background queue): its answer reaches this page too
        }
        // The hex card's Merge (second click): dash.py tree merge, answered with mc.merged
        if cmd == "merge" || cmd == "undraft" || cmd == "signoff", let path = b["path"] as? String, path.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil, !path.contains("..") {
            var out = ""
            let op = cmd == "undraft" ? "ready" : cmd, who = greenlight.wsName(path: path)
            let h = workspaceHost(path: path, fallback: winHost)
            greenlight.log(.greenlight, who, .action, ["merge": "merging", "ready": "marking ready", "signoff": "signing off"][op] ?? op)
            ssh("\(pyCmd(on: h))/dash.py tree \(op) '\(path)'", on: h, onLine: { out += $0 }, onExit: { code in
                chief.acted(who, actedVerb[op] ?? op, code, out)
                let js = safeJSON(out, fallback: ["op": op, "path": path, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
                from.evaluateJavaScript("mc.merged(\(js))")
            })
        }
        if cmd == "editPrompt", let r = b["repo"] as? String, r.range(of: #"^[\w.-]{1,100}$"#, options: .regularExpression) != nil, !r.hasPrefix(".") {
            let k = b["kind"] as? String ?? "", kind = RulesStore.kinds.contains { $0.0 == k } ? "/" + k : ""
            greenlight.settingsTab = "Repositories"; greenlight.settingsRepo = r + kind; appSettings()
        }
        if cmd == "run", let name = b["name"] as? String {
            let prompt = b["prompt"] as? String ?? ""
            if prompt.isEmpty { return }
            guard isWsName(name) else { from.callJS("window.mc && mc.ran", [["name": name, "error": badNameErr]]); return }
            var out = ""
            let h = workspaceHost(name: name, fallback: winHost)
            greenlight.log(.greenlight, name, .action, "starting an agent: \(prompt)")
            ssh("\(pyCmd(on: h))/dash.py run '\(name)'", on: h, stdin: prompt, onLine: { out += $0 }, onExit: { code in
                chief.acted(name, "start an agent", code, out)
                let js = safeJSON(out, fallback: ["name": name, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
                from.evaluateJavaScript("mc.ran(\(js))")
            })
        }
    }

    func webView(_ w: WKWebView, didStartProvisionalNavigation _: WKNavigation!) {
        let id = ObjectIdentifier(w)
        readyDash.remove(id); waitingDash.remove(id)
    }

    // Links in the dashboard/terminal open in the browser, never navigating privileged WebViews to arbitrary URLs or file:// links.
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if a.navigationType == .linkActivated {
            if let u = a.request.url, ["http", "https"].contains(u.scheme?.lowercased() ?? "") { NSWorkspace.shared.open(u) }
            decisionHandler(.cancel); return
        }
        guard let u = a.request.url else { decisionHandler(.cancel); return }
        if u.scheme == "about" { decisionHandler(.allow); return }
        let isTerm = termWebs.contains { $0 === w }
        if u.scheme == "file" {
            let ok = isTerm ? (u.path == page("terminal")?.path)
                : (u.path == page("dashboard")?.path || (!greenlight.fluidURL.isEmpty && u.path == URL(fileURLWithPath: (greenlight.fluidURL.trimmingCharacters(in: .whitespaces) as NSString).expandingTildeInPath).path))
            decisionHandler(ok ? .allow : .cancel); return
        }
        if !isTerm, ["http", "https"].contains(u.scheme?.lowercased() ?? "") {
            let s = greenlight.fluidURL.trimmingCharacters(in: .whitespaces)
            if let cfg = URL(string: s), u.scheme?.lowercased() == cfg.scheme?.lowercased(), u.host?.lowercased() == cfg.host?.lowercased(), u.port == cfg.port {
                decisionHandler(.allow); return
            }
        }
        decisionHandler(.cancel)
    }

    /// A regular app now: app, Edit and Window menus. ⌘A/C/V/X/Z in text fields route through Edit.
    func mainMenu() {
        let bar = NSMenu()
        func menu(_ title: String, _ items: [(String, String, String)], target: AnyObject? = nil) -> NSMenu {
            let m = NSMenu(title: title)
            for (t, a, k) in items {
                if t == "-" { m.addItem(.separator()); continue }
                m.addItem(withTitle: t, action: Selector(a), keyEquivalent: k).target = a.hasPrefix("app") ? target : nil
            }
            bar.addItem(withTitle: title, action: nil, keyEquivalent: "").submenu = m
            return m
        }
        _ = menu("Greenlight", [("Settings…", "appSettings", ","), ("-", "", ""),
                                 ("Hide Greenlight", "hide:", "h"), ("Quit Greenlight", "terminate:", "q")], target: self)
        _ = menu("File", [("New Window", "appNewWindow", "n"), ("New Greenlight Center Window", "appNewL1", ""), ("Open Projects…", "appOpenProjects", "N")], target: self)
        _ = menu("Edit", [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("-", "", ""), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                          ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")])
        let look = NSMenu(title: "Appearance")  // View › Appearance: the same choice as Settings › Appearance (§10.19)
        for (i, (_, t)) in Self.appearances.enumerated() {
            let it = look.addItem(withTitle: t, action: #selector(appAppearanceMenu(_:)), keyEquivalent: ""); it.target = self; it.tag = i
        }
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Appearance", action: nil, keyEquivalent: "").submenu = look
        let fs = view.addItem(withTitle: "Enter Full Screen", action: #selector(toggleFullScreen(_:)), keyEquivalent: "f")  // in place of AppKit's, which would send a guest's to itself
        fs.keyEquivalentModifierMask = [.control, .command]; fs.target = self
        bar.addItem(withTitle: "View", action: nil, keyEquivalent: "").submenu = view
        NSApp.windowsMenu = menu("Window", [("Minimize", "performMiniaturize:", "m"), ("Zoom", "performZoom:", ""), ("Close", "performClose:", "w"),
                                            ("-", "", ""), ("Show/Hide Chat", "appToggleChat", "C"), ("Live Chat", "appLive", "L"), (micMenuTitle(muted: false), "appMuteMic", "M"),
                                            ("Up to Greenlight Center", "appGoUp", "G"), ("Basement (Terminal)", "appToggleTerm", "T"), ("Jump to Terminal…", "appJumpTerm", "j"),
                                            ("Reload Dashboard", "appReload", "r")], target: self)
        _ = menu("Help", [("Report a Bug…", "appReportBug", "")], target: self)
        NSApp.mainMenu = bar
    }
    /// The Greenlight Center window `k` is: the main one (nil L1; a view it gave its own window, or its popped-out chat, too) or another;
    /// nil for any other window.
    func l1Of(_ k: NSWindow?) -> (l: L1?, w: NSWindow)? {
        guard let k else { return nil }
        if k === window || k === chat.window || viewWindows.values.contains(k) { return (nil, k) }  // the main chat, popped out, is its too
        return l1s.values.first { $0.w === k }.map { ($0, k) }
    }
    @objc func appNewWindow() { newWindow(in: NSApp.keyWindow) }
    /// ⌘N makes another of the window you're in: in Greenlight Center, another Greenlight Center with its projects; else a terminal window.
    func newWindow(in k: NSWindow?) {
        if let c = l1Of(k) { newL1(c.l.map(\.only) ?? sharedOnly, from: c.w); return }  // an L1 on All projects (nil) stays All
        newTermWindow(sharedOnly, from: k.flatMap { termWins.values.contains($0) ? $0 : nil })  // the shared pick
    }
    @objc func appNewL1() { newL1(l1Of(NSApp.keyWindow)?.l.map(\.only) ?? sharedOnly, from: NSApp.keyWindow) }  // the projects of the one you're in
    /// ⌘⇧N: Open projects, on New window, in the window you're in. Its pages answer the key themselves; this is for the chat pane, a
    /// title bar or the Window menu. Anywhere else: Greenlight Center's.
    @objc func appOpenProjects() { openProjects(in: NSApp.keyWindow) }
    /// Greenlight Center up and key, for a dialog in it: show() leaves a popped-out chat in front (placeChat), so the main window comes after.
    func frontMain() { show(); if !window.isKeyWindow { presentWindow(window, activate: false) } }  // --uitest: ordered back, as ever
    func openProjects(in k: NSWindow?) {
        if let c = l1Of(k) {
            if c.l == nil, c.w !== window, !viewWindows.values.contains(c.w) { frontMain() }  // the popped-out chat: its dialog is the main window's, so up it comes
            l1Pick(here: false, in: c.l, on: c.w.contentView as? WKWebView); return
        }
        if let k, termWins.values.contains(k) { (k.contentView as? WKWebView)?.callJS("window.newWindowIfFree"); return }  // not over its dialog, palette or a rename
        frontMain(); l1Pick(here: false)  // Settings, the inbox…: Greenlight Center's, in front
    }
    @objc func appSettings() { greenlight.settingsOpen = true; greenlight.inboxOpen = false }

    /// First launch on the native engine (#195): another ADE on the host and no repos yet opens Settings → Import, once.
    var importProbe: RulesStore?
    func offerImport() {
        guard !ud.bool(forKey: "importOffered") else { return }
        let rs = RulesStore(); importProbe = rs
        rs.call("import-detect", "-", onError: { [weak self] in self?.importProbe = nil }) { [weak self] j in
            guard let self else { return }
            self.importProbe = nil
            let found = (j["sources"] as? [String: Bool]) ?? [:]
            guard j["native"] as? Bool == true else { return }  // Orca's own users: nothing to import into yet
            ud.set(true, forKey: "importOffered")
            guard (j["repos"] as? Int ?? 0) == 0, RulesStore.importSources.contains(where: { found[$0.0] == true }) else { return }
            self.greenlight.settingsTab = "Import"; self.appSettings()
        }
    }
    @objc func appReload() { loadFluid(l1s.values.first { $0.w.isKeyWindow }?.web) }

    /// Help › Report a Bug…: a title and description, then the full issue diag.py would file (its trail, re-checked for
    /// leaks on greenlight's host), then Save… or File on GitHub. Nothing leaves this Mac before that last click.
    @objc func appReportBug() {
        let a = NSAlert()
        a.messageText = "Report a Bug"
        a.informativeText = "The report goes on GitHub, in public. Greenlight adds its diagnostic trail: event kinds, timings and error classes, with names hashed and no messages. You'll see all of it before anything is sent. Leave names, paths and secrets out of what you write."
        let box = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 176))
        let t = NSTextField(frame: NSRect(x: 0, y: 150, width: 420, height: 24))
        t.placeholderString = "What went wrong, in a line"
        let sv = NSTextView.scrollableTextView()
        sv.frame = NSRect(x: 0, y: 0, width: 420, height: 142)
        sv.borderType = .bezelBorder
        let tv = sv.documentView as! NSTextView
        tv.isRichText = false; tv.font = .systemFont(ofSize: 12); tv.isAutomaticQuoteSubstitutionEnabled = false
        box.addSubview(t); box.addSubview(sv)
        a.accessoryView = box
        a.addButton(withTitle: "Preview…"); a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = t
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        bugReport(t.stringValue, tv.string, execute: false)
    }
    /// diag.py report on greenlight's host, with this app's own events on stdin (it checks them like its own).
    func bugReport(_ title: String, _ body: String, execute: Bool, shown: String? = nil) {
        Diag.q.async { [weak self] in
            let app = ((try? String(contentsOf: Diag.url, encoding: .utf8)) ?? "").split(separator: "\n").suffix(500)
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) }
            let input = (try? JSONSerialization.data(withJSONObject: ["title": title, "body": body, "app_events": app, "shown": shown.map { $0 as Any } ?? NSNull()])) ?? Data("{}".utf8)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                var out = ""
                _ = ssh("\(py)/diag.py report --json\(execute ? " --execute" : "")", stdin: String(decoding: input, as: UTF8.self),
                        onLine: { out += $0 }, onExit: { [weak self] code in
                    let j = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: Any] ?? [:]
                    self?.bugResult(j, code, title, body, execute)
                })
            }
        }
    }
    func bugResult(_ j: [String: Any], _ code: Int32, _ title: String, _ body: String, _ executed: Bool) {
        let a = NSAlert()
        NSApp.activate(ignoringOtherApps: true)
        guard j["ok"] as? Bool == true else {
            a.messageText = j["blocked"] != nil ? "Not sent" : "Couldn't make the report"
            a.informativeText = j["reason"] as? String ?? "diag.py on \(host.isEmpty ? "this Mac" : host) didn't answer (exit \(code)). Is greenlight synced there?"
            a.runModal(); return
        }
        if executed {
            a.messageText = j["filed"] as? Bool == true ? "Filed" : "Finish on GitHub"
            a.informativeText = j["filed"] as? Bool == true ? "Thank you. The issue is open in your browser." :
                "gh isn't set up where greenlight runs, so GitHub's form is open with your title and description. Attach the bundle you saved."
            if let u = (j["url"] as? String).flatMap(URL.init(string:)), ["http", "https"].contains(u.scheme?.lowercased() ?? "") { NSWorkspace.shared.open(u) }
            a.runModal(); return
        }
        a.messageText = "This is the whole report"
        a.informativeText = "It will be public on github.com/\(j["repo"] as? String ?? "cbarraford/greenlight"). Events that failed the privacy check are already left out."
        let sv = NSTextView.scrollableTextView()
        sv.frame = NSRect(x: 0, y: 0, width: 560, height: 320)
        let tv = sv.documentView as! NSTextView
        tv.isEditable = false; tv.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        tv.string = "\(j["title"] as? String ?? "")\n\n\(j["body"] as? String ?? "")"
        a.accessoryView = sv
        a.addButton(withTitle: "File on GitHub"); a.addButton(withTitle: "Save Bundle…"); a.addButton(withTitle: "Cancel")
        switch a.runModal() {
        case .alertFirstButtonReturn: bugReport(title, body, execute: true, shown: j["body"] as? String)  // what was shown, not a rebuild
        case .alertSecondButtonReturn:
            let s = NSSavePanel()
            s.nameFieldStringValue = "greenlight-bug-report.json"
            if s.runModal() == .OK, let u = s.url, let d = j["data"],
               let data = try? JSONSerialization.data(withJSONObject: d, options: [.prettyPrinted, .sortedKeys]) { try? data.write(to: u) }
        default: break
        }
    }
    @objc func appToggleTerm() { toggleTerm() }
    @objc func appJumpTerm() {  // ⌘J from any window: the terminal with its jump palette open (the page catches ⌘J itself when it has focus)
        if !(termWindow.isVisible && termWindow.isKeyWindow) { toggleTerm() }
        (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.palette(true)")
    }
    var lastToggle = Date.distantPast
    @objc func appLive() {  // ⇧⌘L: live chat on (showing the chat first) or off
        if !greenlight.voice.on && !greenlight.voice.starting { setChat(hidden: false); NSApp.activate(ignoringOtherApps: true) }
        greenlight.voice.toggle()
    }
    @objc func appMuteMic() {  // ⇧⌘M: voice mode's mic, muted or not. Shows the chat, whose mic knob and status say which
        guard greenlight.voice.on else { return }
        if !chatShown { setChat(hidden: false) }
        greenlight.voice.toggleMic()
    }
    /// The menu items aimed at the app: only the mic's depends on state (off while voice mode is; its title says what it does now).
    /// The appearance choices: (stored value, menu and picker title).
    static let appearances = [("auto", "System"), ("light", "Light"), ("dark", "Dark")]
    @objc func appAppearanceMenu(_ item: NSMenuItem) {
        guard Self.appearances.indices.contains(item.tag) else { return }
        greenlight.restyle { ud.set(Self.appearances[item.tag].0, forKey: "appearance") }; tick()
    }
    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(toggleFullScreen(_:)) {
            let w = fsShare.map { NSApp.keyWindow === $0.guest ? $0.host : NSApp.keyWindow } ?? NSApp.keyWindow
            item.title = w?.styleMask.contains(.fullScreen) == true ? "Exit Full Screen" : "Enter Full Screen"
            return w.map { $0.styleMask.contains(.resizable) && !$0.collectionBehavior.contains(.fullScreenAuxiliary) || $0.styleMask.contains(.fullScreen) } ?? false
        }
        if item.action == #selector(appAppearanceMenu(_:)) { item.state = Self.appearances.indices.contains(item.tag) && Self.appearances[item.tag].0 == appearanceMode ? .on : .off; return true }
        guard item.action == #selector(appMuteMic) else { return true }
        item.title = micMenuTitle(muted: greenlight.voice.micMuted)
        return greenlight.voice.on
    }
    @objc func appToggleChat() {  // the menu and the page can both catch one ⌘⇧C: count it once
        if Date().timeIntervalSince(lastToggle) < 0.3 { return }
        lastToggle = Date()
        let hide = chatShown && NSApp.isActive
        setChat(hidden: hide)
        if !hide { NSApp.activate(ignoringOtherApps: true) }
    }

    // MARK: the chat: the main window's right pane, or its own window (⧉ in its header; closing that window docks it back)

    var chatHidden = ud.bool(forKey: "chatPaneHidden") { didSet { ud.set(chatHidden, forKey: "chatPaneHidden") } }  // new key: the pane starts open
    let split = Split()
    /// ponytail: NSSplitView leaves its old divider drawn over the web view after a move; draw none, the pane draws its own edge.
    class Split: NSSplitView { override var dividerColor: NSColor { .clear } }
    let chatEdge = NSBox()  // the chat pane's 1px left edge
    lazy var chatSlot: NSView = {
        let v = NSView(), edge = chatEdge
        edge.boxType = .custom; edge.titlePosition = .noTitle; edge.borderWidth = 0; edge.fillColor = dsDynamic(DS.Palette.borderSubtle); edge.translatesAutoresizingMaskIntoConstraints = false  // §10.1: one hairline, border.subtle, following the appearance
        v.addSubview(edge, positioned: .above, relativeTo: nil)
        NSLayoutConstraint.activate([edge.leadingAnchor.constraint(equalTo: v.leadingAnchor), edge.widthAnchor.constraint(equalToConstant: 1),
                                     edge.topAnchor.constraint(equalTo: v.topAnchor), edge.bottomAnchor.constraint(equalTo: v.bottomAnchor)])
        v.isHidden = chatHidden || greenlight.chatOut
        return v
    }()
    var chatShown: Bool { greenlight.chatOut ? chatWindow.isVisible : window.isVisible && !chatSlot.isHidden }

    func setChat(hidden: Bool) {
        if !greenlight.prompting { greenlight.hint = "" }  // a hovered control going away sends no hover-exit
        chatHidden = hidden
        if !hidden { greenlight.inboxOpen = false }  // asking for the chat closes the inbox
        placeChat()
    }
    func popChat(out: Bool) {
        if !greenlight.prompting { greenlight.hint = "" }  // the pop knob moves out from under the cursor: no hover-exit comes
        greenlight.chatOut = out
        if !out { chatWindow.orderOut(nil) }
        chatHidden = false
        placeChat()
        if !out { window.makeKeyAndOrderFront(nil) }  // back on the right wall: show it there
    }

    /// Puts the chat view where it belongs: the pane, or its window; hidden if `chatHidden`.
    func placeChat() {
        let home = greenlight.chatOut ? chatWindow.contentView! : chatSlot
        if chat.superview !== home {
            chat.removeFromSuperview()
            chat.translatesAutoresizingMaskIntoConstraints = false
            home.addSubview(chat, positioned: .below, relativeTo: nil)  // under the pane's edge
            NSLayoutConstraint.activate([chat.leadingAnchor.constraint(equalTo: home.leadingAnchor), chat.trailingAnchor.constraint(equalTo: home.trailingAnchor),
                                         chat.topAnchor.constraint(equalTo: home.topAnchor), chat.bottomAnchor.constraint(equalTo: home.bottomAnchor)])
        }
        let pane = !greenlight.chatOut && !chatHidden
        window.minSize = centerMinSize; termWins[0]?.minSize = centerMinSize  // one size for every floor: the Basement's can't go below Center's
        if pane { App.widen(window, to: App.paneMinWidth, display: window.isVisible); termWins[0].map { App.widen($0, to: App.paneMinWidth, display: $0.isVisible) } }
        if chatSlot.isHidden == pane {
            chatSlot.isHidden = !pane
            split.adjustSubviews(); split.layoutSubtreeIfNeeded()
            if termOrb.isVisible { placeOrb(termOrb) }
        }
        if pane && chatSlot.frame.width < 320 { split.setPosition(max(480, split.bounds.width - 440), ofDividerAt: 0) }  // a restored collapse comes back a sliver
        if greenlight.chatOut && !chatHidden { chatWindow.makeKeyAndOrderFront(nil) } else if greenlight.chatOut { chatWindow.orderOut(nil) }
        if !chatHidden { greenlight.missed = 0 }
        tick()  // the dashboard's chat toggle
    }

    lazy var chatWindow: NSWindow = {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 680), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = greenlight.name
        w.titlebarAppearsTransparent = true
        w.backgroundColor = dsDynamic(DS.Palette.bgSurface)  // the chat pane's own surface
        w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: 340, height: 360)
        w.contentView = NSView()
        w.delegate = self
        if !w.setFrameUsingName("chat") { w.center() }
        w.setFrameAutosaveName("chat")
        return w
    }()

    // the dashboard keeps 480pt, the chat pane 320pt
    func splitView(_ v: NSSplitView, constrainMinCoordinate p: CGFloat, ofSubviewAt i: Int) -> CGFloat { max(p, 480) }
    func splitView(_ v: NSSplitView, constrainMaxCoordinate p: CGFloat, ofSubviewAt i: Int) -> CGFloat { min(p, v.bounds.width - 320) }

    // MARK: L1's projects: the Open projects dialog B1/B2 have (in the dashboard page), built at launch from projects.json + dash.py trees

    let l1Floor = FloorState()
    var projList = [[String: Any]]()  // id ("" No Man's Land), name, color, repos, paths (its worktrees), busy
    var projectsLoaded = false  // true once dash.py trees (or a terminal page with trees) has listed the projects
    var lastTreesObj: [String: Any]?  // newest valid dash.py trees payload, replayed to terminal pages and recomputed on project/theme changes
    var paneReplay = PaneReplay()  // the current refresh's pane states, every host's, for a page that loads (each publish carries only its own)
    var readyDash = Set<ObjectIdentifier>(), waitingDash = Set<ObjectIdentifier>()  // dashboard webviews that have received mc.paths vs ones waiting on projectsLoaded
    var sharedOnly: [String]? { ud.array(forKey: "termOnly0") as? [String] }  // the one pick every floor and window shows; nil: all projects
    func setPick(_ only: [String]?, except: WKWebView? = nil) {
        let oldOnly = livePick(sharedOnly, known: nil)
        let only = livePick(only, known: nil)  // none ticked is all: never stored empty
        ud.set(only, forKey: "termOnly0"); syncL1(); syncDashPaths()
        let j = (only.flatMap { try? JSONSerialization.data(withJSONObject: $0) }).flatMap { String(data: $0, encoding: .utf8) } ?? "null"
        for w in termWebs where w !== except { w.evaluateJavaScript("window.term && term.only(\(j))") }
        if only != oldOnly {
            let list = effectiveProjList()
            let target = resolveProjectHost(only: live(only) ?? only, projList: list, savedProjects: savedProjects, repoHosts: repoHosts)
            let label = projectChip(only: only, projList: list, projectsLoaded: true).text
            greenlight.switchProject(to: target, label: label)
            ensureLocalDashIfNeeded()
        }
    }
    var l1Only: [String]? { live(sharedOnly) }  // nil until projects are listed
    /// A pick as the windows show it, against the listed projects: nil (all) until listed, and for a pick none of whose
    /// projects is still there. The pick itself stays as saved, so a project that comes back (its host's trees) is shown again.
    func live(_ only: [String]?) -> [String]? { projList.isEmpty ? nil : livePick(only, known: projList.compactMap { $0["id"] as? String }) }
    var l1Picked: [[String: Any]] { picked(l1Only) }
    func picked(_ only: [String]?) -> [[String: Any]] { (only ?? []).compactMap { id in projList.first { $0["id"] as? String == id } } }
    /// The picked projects' "paths" or "repos"; nil: every project. Sorted: the tick compares it.
    func sel(_ only: [String]?, _ k: String) -> [String]? { only == nil ? nil : Array(Set(picked(only).flatMap { $0[k] as? [String] ?? [] })).sorted() }
    var l1Paths: [String]? { sel(l1Only, "paths") }
    var l1Repos: [String]? { sel(l1Only, "repos") }  // the dashboard matches them against its own fresh trees: l1Paths is only as new as the last trees
    func canShowDash(for web: WKWebView) -> Bool { canShowDashboard(only: l1(web).map(\.only) ?? sharedOnly, projectsLoaded: projectsLoaded) }
    func pushPaths(to web: WKWebView) {
        let o = l1(web).map { live($0.only) } ?? l1Only
        web.callJS("window.mc && mc.paths", [sel(o, "paths") ?? NSNull(), sel(o, "repos") ?? NSNull()])
    }
    func syncDashPaths() {
        let json = { (v: [String]?) in v.flatMap { try? JSONSerialization.data(withJSONObject: $0) }.flatMap { String(data: $0, encoding: .utf8) } ?? "null" }
        let mainKey = "\(json(l1Paths)), \(json(l1Repos))", mainChanged = pushedRepos != mainKey
        if mainChanged { pushedRepos = mainKey }
        for web in mainWebs {
            let id = ObjectIdentifier(web)
            if waitingDash.contains(id), canShowDash(for: web) {
                waitingDash.remove(id); readyDash.insert(id)
                pushPaths(to: web)
                if let snap = dashSnapObj { web.callJS("window.mc && mc.update", [snap]) }
            } else if mainChanged, readyDash.contains(id) {
                pushPaths(to: web)
            }
        }
        for l in l1s.values {
            let id = ObjectIdentifier(l.web), o = live(l.only)
            let key = "\(json(sel(o, "paths"))), \(json(sel(o, "repos")))", changed = l.pushedRepos != key
            if changed { l.pushedRepos = key }
            if waitingDash.contains(id), canShowDash(for: l.web) {
                waitingDash.remove(id); readyDash.insert(id)
                pushPaths(to: l.web)
                if let snap = dashSnapObj { l.web.callJS("window.mc && mc.update", [snap]) }
            } else if changed, readyDash.contains(id) {
                pushPaths(to: l.web)
            }
        }
    }
    func refreshProjects() {
        let trees = (lastTreesObj?["trees"] as? [[String: Any]]) ?? []
        if let rawHosts = lastTreesObj?["repoHosts"] {
            repoHosts = mergeRepoHostMaps(repoHosts, parseRepoHosts(rawHosts))
        }
        var byPath = [String: String](), byName = [String: String]()
        for t in trees where t["nativeHost"] as? Bool != false {
            if let path = t["path"] as? String, !path.isEmpty {
                let h = (t["_runHost"] as? String) ?? host
                byPath[path] = h
                if t["main"] as? Bool != true, let n = t["name"] as? String, !n.isEmpty { byName[n] = h }
            }
        }
        treeHostByPath = byPath
        treeHostByName = byName
        rebuildWorkspaceHostIndex()
        projList = buildProjectList(saved: savedProjects, trees: trees, dark: isDark, fallback: lastTreesObj == nil)
        projectsLoaded = true
        syncL1()
        syncDashPaths()
        syncHostForActiveWindows(clearOnChange: false)
        ensureLocalDashIfNeeded()
    }
    /// Chief's scope: the projects picked, which every window shows (a terminal's term.only, the dashboard's mc.paths), while one of
    /// them is up. Read straight from the pick, not asked of a page: nothing to wait on, and no answer from a window you've left.
    var pickScope: String? {
        windowScope(paths: l1Paths, repos: l1Repos, shown: window.isVisible || termWins.values.contains(where: \.isVisible) || viewWindows.values.contains(where: \.isVisible))
    }
    func l1Title(_ project: String) -> String { project.isEmpty ? greenlight.name : "\(greenlight.name) · \(project)" }
    func syncL1() {
        for (f, raw) in [(l1Floor, sharedOnly)] + l1s.values.map({ ($0.floor, $0.only) }) {
            let chip = projectChip(only: raw, projList: projList, projectsLoaded: projectsLoaded)
            f.project = chip.text
            f.colors = chip.colors
        }
    }
    /// Open projects, for L1 window `l` (nil: the main one), shown in `web` when that's one of the main one's detached views: the window you're in.
    func l1Pick(here: Bool = true, in l: L1? = nil, on web: WKWebView? = nil) {
        let shownList = projectsLoaded ? projList : buildProjectList(saved: savedProjects, trees: [], dark: isDark, fallback: true)
        let o = l == nil ? l1Only : live(l!.only)
        let view = l == nil ? web.flatMap { w in viewWindows.values.contains { $0.contentView === w } ? w : nil } : nil
        if l == nil, view == nil, !window.isVisible { frontMain() }  // the main window, closed: up again, or its dialog opens unseen
        (l?.web ?? view ?? fluid).callJS("window.mc && mc.pickProjects", [shownList, o ?? NSNull(), here])
    }

    // MARK: more Greenlight Center windows (L1's Open projects → New window): each with its own projects and its own Chief (Core(session:)),
    // remembered in l1Windows. Its close button ends that conversation, as a terminal New window's kills its shells. The main one is `window`.

    final class L1 {
        let id: Int, w: NSWindow, web: WKWebView, c: Core, chat: NSHostingView<ChatView>, split = Split(), floor = FloorState()
        var only: [String]?, picked = [String](), pickOpen = false, pushedRepos = "", pushedChat = ""
        init(_ id: Int, _ w: NSWindow, _ web: WKWebView, _ c: Core, _ only: [String]?) {
            self.id = id; self.w = w; self.web = web; self.c = c; self.only = only
            chat = NSHostingView(rootView: ChatView(c: c)); chat.sizingOptions = []
        }
    }
    var l1s = [Int: L1]()
    func l1(_ web: WKWebView?) -> L1? { l1s.values.first { $0.web === web } }
    func saveL1s() { ud.set(l1s.values.sorted { $0.id < $1.id }.map { l in l.only.map { ["id": l.id, "only": $0] as [String: Any] } ?? ["id": l.id] }, forKey: "l1Windows") }
    /// Its Chief's scope: its projects, the whole time it's open (it's a window: it closes rather than hides).
    func scope(_ l: L1) -> String? { let o = live(l.only); return windowScope(paths: sel(o, "paths"), repos: sel(o, "repos"), shown: true) }

    @discardableResult
    func makeL1(_ id: Int, only: [String]?, from: NSWindow? = nil) -> L1 {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.backgroundColor = dsDynamic(DS.Palette.bgCanvas); w.titleVisibility = .hidden; w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: App.paneMinWidth, height: 520); w.delegate = self
        let c = Core(session: id), l = L1(id, w, makeWeb(), c, only)
        c.serverConnected = greenlight.serverConnected
        c.syncChatHost(resolveProjectHost(only: live(only) ?? only, projList: effectiveProjList(), savedProjects: savedProjects, repoHosts: repoHosts))
        ensureLocalDashIfNeeded()
        c.follow(greenlight)
        c.$hint.receive(on: RunLoop.main).sink { [weak self, weak c] t in if let self, let c { self.hint(t, from: c) } }.store(in: &c.subs)  // its hover hints, by the cursor too
        c.scope = { [weak self, weak l] in l.flatMap { self?.scope($0) } }
        c.popoverShown = { [weak w] in NSApp.isActive && w?.isKeyWindow == true }
        c.light = { [weak self, weak l] names, open in
            guard let self, let l else { return }
            l.picked = names.reduce(into: []) { if !$0.contains($1) { $0.append($1) } }
            l.pickOpen = open
            self.pushPicks(l.web)
        }
        c.unattach = { [weak l] key in
            let k = (try? JSONSerialization.data(withJSONObject: [key])).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
            l?.web.evaluateJavaScript("window.mc && mc.undock(\(k)[0])")
        }
        l.floor.at = "greenlight"
        let go: (String) -> Void = { [unowned self] f in  // ponytail: down to the Basement or the Pipe is a cut; the ride is the main window's
            wantFloor = f; (termWindow.contentView as? WKWebView)?.callJS("window.term && term.opens", [f, !termWindow.isVisible]); basement(from: l1s[id]?.w) }
        floorGo[ObjectIdentifier(w)] = (l.floor, go)
        floorBar(w, Floors(s: l.floor, c: greenlight, pick: { [weak self, weak l] in if let l { self?.l1Pick(in: l) } }, go: go))
        l1s[id] = l; syncL1()
        let sp = l.split
        sp.isVertical = true; sp.dividerStyle = .thin; sp.delegate = self
        sp.addArrangedSubview(l.web); sp.addArrangedSubview(l.chat)
        sp.setHoldingPriority(.defaultLow, forSubviewAt: 0); sp.setHoldingPriority(.defaultLow + 1, forSubviewAt: 1)
        for (v, wd, pr) in [(l.web, 480, NSLayoutConstraint.Priority.defaultHigh + 1), (l.chat, 320, .defaultHigh)] as [(NSView, CGFloat, NSLayoutConstraint.Priority)] {
            let k = v.widthAnchor.constraint(greaterThanOrEqualToConstant: wd); k.priority = pr; k.isActive = true
        }
        w.contentView = sp
        let saved = savedFrame(UserDefaults.standard.string(forKey: "NSWindow Frame \(l1FrameName(id))"))  // AppKit's, never --uitest's ud
        if saved == nil {
            if let src = from ?? (window.isVisible ? window : nil) {  // cascaded off the one you asked from, on its screen
                w.setFrame(cascadeFrame(src.frame, min: w.minSize, in: (src.screen ?? NSScreen.main)?.visibleFrame), display: false)
            } else { w.center() }
        }
        w.setFrameAutosaveName(l1FrameName(id))  // applies AppKit's copy, rescaled onto the focused screen: put it back as saved
        if let f = saved { w.setFrame(f, display: false); onScreen(w) }
        App.widen(w, to: w.minSize.width, display: false)
        sp.layoutSubtreeIfNeeded()
        sp.setPosition(max(480, sp.bounds.width - 440), ofDividerAt: 0)
        if !uitest { sp.autosaveName = l1FrameName(id, "chatSplit") }  // AppKit keeps it where --uitest's ud can't remove it
        loadFluid(l.web)
        return l
    }
    /// Where AppKit keeps an L1 window's frame (and its chat divider): the app's standard defaults, which every --uitest run of one binary shares
    /// while their ids all start at 1, so a run's are its own there.
    func l1FrameName(_ id: Int, _ kind: String = "main") -> String { uitest ? "\(kind)-\(uitestSuiteName ?? "")-\(id)" : "\(kind)-\(id)" }
    /// A saved frame on a monitor that's gone: back on the main screen (multi-monitor setups unplug).
    func onScreen(_ w: NSWindow) {
        let f = fitOnScreen(w.frame, screens: NSScreen.screens.map(\.visibleFrame))
        if f != w.frame { w.setFrame(f, display: false) }
    }
    func newL1(_ only: [String]?, from: NSWindow?) {
        // never a closed window's id: its chat-<n> cleanup is one ssh that may not have landed (host asleep) or may land late
        let id = max(ud.integer(forKey: "l1Next"), (l1s.keys.max() ?? 0) + 1)
        ud.set(id + 1, forKey: "l1Next")
        let l = makeL1(id, only: only, from: from)
        saveL1s()
        revealOnPaint(l.w, l.web)
        l.w.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func reopenL1s() {
        guard !selftest else { return }  // not your windows and their chat-<n>
        let saved = ud.array(forKey: "l1Windows") as? [[String: Any]] ?? []
        for d in saved { if let id = d["id"] as? Int, id > 0 { let l = makeL1(id, only: livePick(d["only"] as? [String], known: nil)); revealOnPaint(l.w, l.web); l.w.orderFront(nil) } }
    }
    /// The close button: its conversation goes, with its transcript and its open questions in the decision record (chat.py new).
    func closeL1(_ l: L1) {
        l.c.queue = []; l.c.proc?.terminate(); l.c.run = nil  // its turn's late result writes nothing (onExit checks run)
        if !l.c.voice.idle { l.c.voice.stopAll() }
        l.c.recorder?.stop(); l.c.recorder = nil; l.c.meter?.invalidate(); l.c.meter = nil; l.c.recording = nil
        l.c.cancelPlans()
        for work in l.c.launchWorks.values { work.cancel() }
        l.c.launchWorks.removeAll(); l.c.toastListeners.removeAll()
        let h = l.c.chatHost, p = hostPaths(for: h)
        ssh("\(l.c.chief(on: h)) stop; \(l.c.chief(on: h)) new; cd \(shellPath(p.home)) && rm -rf chat-\(l.id)", on: h)
        if h != host {
            ssh("\(l.c.chief(on: host)) stop; \(l.c.chief(on: host)) new; cd \(shellPath(greenlightHome)) && rm -rf chat-\(l.id)", on: host)
        }
        [l.c.transcriptFile, l.c.questionsURL].forEach { try? FileManager.default.removeItem(at: $0) }
        [l.c.meterKeys.context, l.c.meterKeys.window].forEach { ud.removeObject(forKey: $0) }  // no stale defaults (ids aren't reused)
        let wid = ObjectIdentifier(l.web); readyDash.remove(wid); waitingDash.remove(wid); paintedWebs.remove(wid)  // a freed view's id comes back
        l.web.configuration.userContentController.removeScriptMessageHandler(forName: "mc")  // it retains self
        if hintCore === l.c { hintCore = nil; showTab("") }  // closed mid-hover: its hover-exit won't come
        dropShare(l.w); floorGo[ObjectIdentifier(l.w)] = nil; l.c.subs.removeAll(); l1s[l.id] = nil; saveL1s()
        l.w.setFrameAutosaveName(""); NSWindow.removeFrame(usingName: l1FrameName(l.id))
        l.split.autosaveName = nil; ud.removeObject(forKey: "NSSplitView Subview Frames \(l1FrameName(l.id, "chatSplit"))")
    }

    // MARK: the T orb and its terminal: login shells in tabs in their own window; the close button keeps them

    let orbState = OrbState()
    lazy var termOrb: NSPanel = {
        let p = floatPanel(NSSize(width: 64, height: 64))
        p.hasShadow = false  // the faces draw their own (and the green light its glow)
        let h = FloatHost(rootView: OrbFace(s: orbState))
        h.onPress = { [weak self] in self?.orbState.pressed = $0 }
        h.draggable = true
        h.onClick = { [weak self] in self.map { $0.ride(up: !$0.upstairs) } }
        orbState.tap = h.onClick
        h.onMoved = { [weak self] in self?.saveOrb(self?.termOrb, "termOrbOff") }
        h.inside = { [weak self] in self?.orbArea }
        p.contentView = h
        return p
    }()
    var ptys = [String: Pty]()  // one shell per tab, by "window:tab"
    var ptySession = [String: String]()  // the tmux session each of those attached ("window:tab" → mc-…)
    /// Whether a pane other than `key` (any window) still shows this session: a workspace page's Basement button can attach
    /// an agent's session that another window already has open, and closing one of them must not kill the agent in the other.
    func heldElsewhere(_ session: String, except key: (String) -> Bool) -> Bool {
        ptySession.contains { !key($0.key) && $0.value == session && ptys[$0.key] != nil }
    }
    /// Terminal windows by id: 0 is the orb's (every project, its close button hides it); the rest you made with
    /// New window (some projects, its close button kills its shells), remembered in termWindows.
    var termWins = [Int: NSWindow]()
    var wantFloor: String?  // the floor Greenlight's indicator asked for, if the terminal page wasn't loaded yet
    var floors = [Int: FloorState]()  // each terminal window's floor indicator
    var floorGo = [ObjectIdentifier: (FloorState, (String) -> Void)]()  // every window with floors (L1's too): where it is, how it rides; ⌘↑/⌘↓ use it
    lazy var termWindow: NSWindow = makeTermWindow(0, only: ud.array(forKey: "termOnly0") as? [String])  // the orb's window shows all projects unless you narrowed it
    var termWebs: [WKWebView] { termWins.values.compactMap { $0.contentView as? WKWebView } }
    func termId(_ web: WKWebView?) -> Int? { termWins.first { $0.value.contentView === web }?.key }
    func termState(_ wid: Int) -> URL { support.appendingPathComponent(wid == 0 ? "terminals.json" : "terminals-\(wid).json") }
    var projectsFile: URL { support.appendingPathComponent("projects.json") }
    var savedProjects: [[String: Any]] { (try? Data(contentsOf: projectsFile)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [[String: Any]] } ?? [] }
    /// The projects ([{id, name, repos, dirs, dir?}]) from the Basement's editor or Settings → Terminal: checked, saved, sent to every terminal window.
    @discardableResult
    func saveProjects(_ any: Any?, from web: WKWebView? = nil) -> Bool {
        let dir = #"^/[\w./@+-]{1,300}$"#
        guard let list = any as? [[String: Any]], list.count <= 100, list.allSatisfy({ p in
            (p["id"] as? String)?.range(of: #"^p\w{1,40}$"#, options: .regularExpression) != nil && (p["name"] as? String)?.count ?? 0 <= 80
            && (p["repos"] as? [String])?.count ?? 999 <= 500 && ((p["dirs"] as? [String]).map { $0.count <= 100 && $0.allSatisfy { $0.range(of: dir, options: .regularExpression) != nil && !$0.contains("..") } } ?? false)
            && (p["dir"] == nil || (p["dir"] as? String).map { $0.range(of: dir, options: .regularExpression) != nil && !$0.contains("..") } ?? false)  // the default folder
        }), JSONSerialization.isValidJSONObject(list), let d = try? JSONSerialization.data(withJSONObject: list) else { return false }
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        try? d.write(to: projectsFile, options: .atomic)
        let s = String(decoding: d, as: UTF8.self)
        termWebs.filter { $0 !== web }.forEach { $0.evaluateJavaScript("term.projects(\(s))") }
        if projectsLoaded { refreshProjects() }
        return true
    }
    func makeTermWindow(_ id: Int, only: [String]?) -> NSWindow {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 460), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Terminal"
        w.titleVisibility = .hidden
        w.titlebarAppearsTransparent = true
        w.backgroundColor = dsDynamic(DS.Palette.bgCanvas)  // the page's band takes over once it loads
        w.isReleasedWhenClosed = false
        w.minSize = id == 0 ? centerMinSize : NSSize(width: 300, height: 160)  // the orb's window shares Center's frame (issue 177), so its minimum too
        w.animationBehavior = greenlight.rideTime > 0 ? .default : .none
        w.delegate = self
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "term")
        UITestServer.installErrorCollector(cfg)
        let web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        w.contentView = web
        let name = id == 0 ? "terminal" : "terminal-\(id)"
        if !w.setFrameUsingName(name) { w.center() }
        w.setFrameAutosaveName(name)
        if id == 0, case let f = sharedFrame(w.frame, min: w.minSize, in: w.screen?.visibleFrame), f != w.frame { w.setFrame(f, display: false) }  // saved under the old 300×160 minimum
        termWins[id] = w
        let fs = FloorState(); floors[id] = fs
        let go: (String) -> Void = { [unowned self, weak web] f in
            if f != "greenlight" { web?.callJS("window.term && term.opens", [f]) } else if id == 0 { appGoUp() } else { show(from: termWins[id]) }
        }
        floorGo[ObjectIdentifier(w)] = (fs, go)
        floorBar(w, Floors(s: fs, c: greenlight, pick: { [weak web] in web?.evaluateJavaScript("window.pickProjects && pickProjects(true)") }, go: go))
        loadTerm(web, only: only)
        return w
    }
    /// New window: remembered, cascaded off the one you asked from, in front.
    func newTermWindow(_ only: [String]?, from: NSWindow?) {
        var saved = ud.array(forKey: "termWindows") as? [[String: Any]] ?? []
        let nid = (saved.compactMap { $0["id"] as? Int } + termWins.keys).max().map { $0 + 1 } ?? 1
        saved.append(["id": nid]); ud.set(saved, forKey: "termWindows"); setPick(only)  // its projects become everyone's
        let w = makeTermWindow(nid, only: only)
        if let f = from?.frame { w.setFrameTopLeftPoint(NSPoint(x: f.minX + 30, y: f.maxY - 30)) }
        if let web = w.contentView as? WKWebView { revealOnPaint(w, web) }
        w.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    /// The windows New window made, as they were at quit.
    func reopenTermWindows() {
        for d in ud.array(forKey: "termWindows") as? [[String: Any]] ?? [] {
            guard let id = d["id"] as? Int, id > 0 else { continue }
            let w = makeTermWindow(id, only: sharedOnly)
            if let web = w.contentView as? WKWebView { revealOnPaint(w, web) }
            w.orderFront(nil)
        }
    }
    /// Red button on a window New window made: its shells' tmux sessions go, and so does the window.
    func closeTermWindow(_ wid: Int) {
        guard wid != 0, let w = termWins[wid], let web = w.contentView as? WKWebView else { return }
        web.evaluateJavaScript("term.closeAll()") { [weak self] r, _ in
            guard let self else { return }
            for p in r as? [[String: Any]] ?? [] {
                guard let s = p["session"] as? String, s.range(of: #"^mc-[\w-]{1,64}$"#, options: .regularExpression) != nil,
                      !self.heldElsewhere(s, except: { $0.hasPrefix("\(wid):") }), let at = paneTarget(p["ws"] as? String ?? "") else { continue }
                ssh("\(App.tmux) kill-session -t \(shellQuote("=" + s)) 2>/dev/null", on: at.host)
            }
            for k in self.ptys.keys where k.hasPrefix("\(wid):") { self.ptys.removeValue(forKey: k)?.stop(); self.ptySession[k] = nil }
            self.dropShare(w)
            self.paintedWebs.remove(ObjectIdentifier(web)); self.termWins[wid] = nil; self.floors[wid] = nil; self.floorGo[ObjectIdentifier(w)] = nil; self.termBadges[wid] = nil
            ud.set((ud.array(forKey: "termWindows") as? [[String: Any]] ?? []).filter { $0["id"] as? Int != wid }, forKey: "termWindows")
            try? FileManager.default.removeItem(at: self.termState(wid))
            web.configuration.userContentController.removeScriptMessageHandler(forName: "term")  // it holds the app, not the page, but the page would linger
            w.setFrameAutosaveName(""); NSWindow.removeFrame(usingName: "terminal-\(wid)"); w.close()  // the next New window may reuse this id
        }
    }
    /// Your Ghostty look: its config's theme file, then the config's own color/font lines over it (ghostty-web theme keys).
    func ghosttyLook() -> [String: String] {
        func lines(_ path: String) -> [(String, String)] {
            ((try? String(contentsOfFile: path, encoding: .utf8)) ?? "").split(separator: "\n").compactMap { l in
                let l = l.trimmingCharacters(in: .whitespaces)
                guard !l.hasPrefix("#"), let eq = l.firstIndex(of: "=") else { return nil }
                return (l[..<eq].trimmingCharacters(in: .whitespaces), l[l.index(after: eq)...].trimmingCharacters(in: .whitespaces))
            }
        }
        let home = NSHomeDirectory(), cfg = ["\(home)/.config/ghostty/config", "\(home)/Library/Application Support/com.mitchellh.ghostty/config"].flatMap(lines)
        var all = cfg
        if var name = cfg.last(where: { $0.0 == "theme" })?.1 {
            if name.contains(":") {  // light:A,dark:B
                let parts = Dictionary(name.split(separator: ",").compactMap { p -> (String, String)? in
                    let kv = p.split(separator: ":", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }; return kv.count == 2 ? (kv[0], kv[1]) : nil }, uniquingKeysWith: { a, _ in a })
                name = parts["dark"] ?? parts.values.first ?? name
            }
            let dirs = ["\(home)/.config/ghostty/themes", "/Applications/Ghostty.app/Contents/Resources/ghostty/themes"]
            let file = dirs.lazy.compactMap { d in (try? FileManager.default.contentsOfDirectory(atPath: d))?.first { $0.lowercased() == name.lowercased() }.map { "\(d)/\($0)" } }.first
            all = (file.map(lines) ?? []) + cfg  // the config's own lines win
        }
        let ansi = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
        let keys = ["background": "background", "foreground": "foreground", "cursor-color": "cursor", "cursor-text": "cursorAccent",
                    "selection-background": "selectionBackground", "selection-foreground": "selectionForeground", "font-family": "font", "font-size": "size", "macos-option-as-alt": "optAlt"]
        var out = [String: String]()
        for (k, v) in all {
            if let key = keys[k] { out[key] = ["font", "size", "optAlt"].contains(key) ? v : (v.hasPrefix("#") ? v : "#" + v) }
            else if k == "palette", let eq = v.firstIndex(of: "="), let n = Int(v[..<eq]), (0..<16).contains(n) {
                let c = String(v[v.index(after: eq)...]); out[n < 8 ? ansi[n] : "bright" + ansi[n - 8].capitalized] = c.hasPrefix("#") ? c : "#" + c
            }
        }
        return out
    }
    func rgb(_ c: NSColor) -> String { let c = c.usingColorSpace(.sRGB) ?? c; return String(format: "%02X%02X%02X", Int(c.redComponent * 255), Int(c.greenComponent * 255), Int(c.blueComponent * 255)) }
    /// The terminal page's look: the screen always wears darkTheme with your Ghostty colors and font over it; the chrome follows the mode.
    func termLook() -> [String: String] {
        var look = ["background": "#" + rgb(darkTheme.bg), "foreground": "#" + rgb(darkTheme.fg), "font": codeFont, "size": "\(Int(fontSize))"]
        look.merge(ghosttyLook()) { _, g in g }  // Ghostty's colors and font win over the app's theme
        return look.merging(termMode) { _, m in m }
    }
    var termMode: [String: String] { ["mode": isDark ? "dark" : "light", "lightBg": "#" + rgb(lightTheme.bg), "lightFg": "#" + rgb(lightTheme.fg)] }
    func loadTerm(_ web: WKWebView, only: [String]?) {
        guard let u = page("terminal") else { return }
        var q = URLComponents(url: u, resolvingAgainstBaseURL: false)!
        let look = termLook(), main = termWins[0]?.contentView === web
        if let b = look["background"], b.count == 7 { web.underPageBackgroundColor = hex(String(b.dropFirst())) }  // the terminal screen's, not white, under a resize (§10.1)
        if let b = isDark ? look["background"] : look["lightBg"], b.count == 7 { web.window?.backgroundColor = hex(String(b.dropFirst())).blended(withFraction: isDark ? 0.22 : 0.05, of: .black) }  // the page's band follows  // the tab strip's color, so the title band is part of it
        let json = String(data: (try? JSONSerialization.data(withJSONObject: look)) ?? Data(), encoding: .utf8) ?? "{}"
        q.queryItems = [.init(name: "main", value: main ? "1" : "0"), .init(name: "dev", value: devName ?? ""), .init(name: "look", value: json), .init(name: "home", value: host.isEmpty || host == "local" ? NSHomeDirectory() : ""), .init(name: "host", value: host), .init(name: "localname", value: greenlight.localShown), .init(name: "editor", value: editorApp()?.name ?? ""), .init(name: "side", value: main && opensBasement ? "1" : "0"),
                        .init(name: "hosts", value: String(data: (try? JSONSerialization.data(withJSONObject: sshHosts())) ?? Data(), encoding: .utf8))]
        if let only { q.queryItems?.append(.init(name: "only", value: String(data: (try? JSONSerialization.data(withJSONObject: only)) ?? Data(), encoding: .utf8))) }
        web.loadFileURL(q.url!, allowingReadAccessTo: u.deletingLastPathComponent())
    }
    /// Whether the orb's window opens on the Basement (the sidebar out) rather than the Pipe; New windows always open on the Pipe.
    var opensBasement: Bool { (wantFloor ?? (greenlight.termOpens == "basement" ? "basement" : "pipe")) == "basement" }
    /// ⌘⇧T: down to the Basement from Greenlight; in the Basement, hides or shows the terminal.
    func toggleTerm() {
        if upstairs { return ride(up: false) }
        if termWindow.isVisible && termWindow.isKeyWindow { hideLevel(termWindow); return }
        basement()
    }
    /// Chief's [console: …]: a tmux session (mc-…), a worktree path or the end of one, or a tab's name; terminal.html's term.ws finds the tab.
    /// ponytail: the key window's tabs only; a tab in another Basement window isn't found
    func toConsole(_ w: String) { toWorkspace(w) }
    var wantWs: String?, wantResume: [String: String] = [:]  // the worktree a dashboard Basement button asked for, if the terminal page wasn't loaded yet
    /// A puck's `resume` (dash.py add_resume) as the page may get it: a harness greenlight knows, a tmux session's name and a plain absolute binary path, nothing else.
    func resumeOf(_ o: Any?) -> [String: String] {
        let d = o as? [String: Any] ?? [:]
        var out: [String: String] = [:]
        if let h = d["harness"] as? String, ["claude", "codex", "opencode", "gemini", "pi", "agy"].contains(h) { out["harness"] = h }
        if let s = d["session"] as? String, s.range(of: #"^mc-[\w-]{1,64}\z"#, options: .regularExpression) != nil { out["session"] = s }  // as open's; \z: not before a final newline
        if let b = d["bin"] as? String, b.range(of: #"^/[\w./@+-]{1,300}\z"#, options: .regularExpression) != nil, !b.contains("..") { out["bin"] = b }
        return out
    }
    /// The dashboard's Basement buttons: B1 (the sidebar open) on that worktree. resume (a workspace page's button, issue 157):
    /// its last agent's live tmux session, else that agent continuing its last session there; else its latest tab or a new shell.
    func toWorkspace(_ path: String, resume: [String: String] = [:]) {
        wantFloor = "basement"; wantWs = path; wantResume = resume
        (termWindow.contentView as? WKWebView)?.callJS("window.term && ((p, r, c) => (term.opens('basement', c), term.ws(p, r), 1))", [path, resume, !termWindow.isVisible]) { [weak self] r, _ in
            if r as? Int == 1 { self?.wantWs = nil; self?.wantResume = [:]; self?.wantFloor = nil }
        }
        upstairs ? ride(up: false) : basement()
    }
    /// The Basement in front. `from`: the window it replaces (default: Center when it's up), whose frame it takes.
    func basement(from: NSWindow? = nil) {
        hand(termWindow, from: from ?? (upstairs && window.isVisible ? window : nil))
        upstairs = false
        if let web = termWindow.contentView as? WKWebView { revealOnPaint(termWindow, web) }
        presentWindow(termWindow)
        if !uitest { (termWindow.contentView as? WKWebView)?.evaluateJavaScript("window.term && term.focus()") }
        syncOrbs()
    }
    /// A 2–4 word tab name from a tab's recent output: headless LLM call on this Mac (Settings → Terminal's model & effort); "" when it fails.
    func aiTabName(_ text: String, model: String, effort: String = "low", done: @escaping (String) -> Void) {
        claudeP("Name this terminal tab in 2 to 4 words, from what is going on in it (the project, task or tool). "
            + "Reply with only the name: no quotes, no punctuation at the end.\n\n" + String(text.suffix(6000)), model: model, effort: effort) { out in
            done(String(String(out.split(separator: "\n").first ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "\"'`*. ").union(.whitespaces)).prefix(40)))
        }
    }
    /// Headless LLM call (Claude, Codex, or agy): its stdout reply, "" when it fails.
    func claudeP(_ prompt: String, model: String, effort: String = "low", done: @escaping (String) -> Void) {
        let inv = llmInvocation(prompt, model: model, effort: effort)
        let finish: (String) -> String = { inv.harness == "agy" ? parseAgyStreamResult($0) : $0 }
        if !devSsh(host).isEmpty {  // a dev copy asks the host's CLI: one here would connect in the copy's name
            var out = ""
            ssh("exec $SHELL -lc " + shellQuote(inv.shell), stdin: inv.stdin,
                onLine: { out += $0 + "\n" }, onExit: { done($0 == 0 ? finish(out) : "") })
            return
        }
        DispatchQueue.global().async {
            let p = Process(), inPipe = Pipe(), out = Pipe()
            p.executableURL = URL(fileURLWithPath: inv.bin)
            p.arguments = inv.args
            p.currentDirectoryURL = FileManager.default.temporaryDirectory
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "\(NSHomeDirectory())/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
            p.environment = env
            p.standardInput = inPipe; p.standardOutput = out; p.standardError = FileHandle.nullDevice
            var text = ""
            if (try? p.run()) != nil {
                DispatchQueue.global(qos: .userInitiated).async {
                    try? inPipe.fileHandleForWriting.write(contentsOf: Data(inv.stdin.utf8))
                    try? inPipe.fileHandleForWriting.close()
                }
                let d = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
                if p.terminationStatus == 0 { text = finish(String(decoding: d, as: UTF8.self)) }
            }
            DispatchQueue.main.async { done(text) }
        }
    }
    /// Each pane is a tmux session on its own socket, so quitting the app only detaches: relaunch reattaches, and
    /// after a reboot `new-session -A` starts it fresh in the workspace. Options go inline: no config on either host.
    static let tmuxOpts = ["set -g status off", "set -g prefix None", "set -s escape-time 0", "set -g set-titles on",
        // the inner app's own title (Claude Code's "✳ name"); else the folder, not tmux's default (the hostname); "⌬ " for codex, "◈ " for agy
        "set -g set-titles-string '#{?#{m:*codex*,#{pane_current_command}},⌬ ,#{?#{m:*agy*,#{pane_current_command}},◈ ,}}#{?#{==:#{pane_title},#{host}},#{pane_current_path},#{pane_title}}'",
        "set -g default-terminal tmux-256color", "set -s 'terminal-features[90]' 'xterm-256color:RGB'", "set -g allow-passthrough on",
        "set -g history-limit 50000", "set -s 'terminal-overrides[90]' '*:smcup@:rmcup@'",  // no alternate screen: slow output also lands in the page's own scrollback
        // tmux draws screens, not lines: a fast burst reaches the page as its last screenful, so the history is tmux's. The page's
        // wheel sends SGR wheel events (after paneCmd's tmuxMark): copy-mode (-e: out again at the bottom), a line each; ⌃⌥⇧-wheel
        // down leaves copy-mode before a keystroke, ⌃⌥⇧-wheel up is ⌘K's clear-history. ghostty-web sends no other mouse events, so clicks stay the page's.
        "set -g mouse on", "bind -T copy-mode WheelUpPane send -X scroll-up", "bind -T copy-mode WheelDownPane send -X scroll-down",
        "bind -T copy-mode-vi WheelUpPane send -X scroll-up", "bind -T copy-mode-vi WheelDownPane send -X scroll-down",
        // ⌃⌥⇧ in the root table: copy-mode falls back to it, and an app that asked for the mouse never sees them
        "bind -n C-M-S-WheelDownPane if -F '#{pane_in_mode}' 'send -X cancel'", "bind -n C-M-S-WheelUpPane clear-history",
        // a pane keeps copy-mode across a detach (quit, reload): a reattach leaves it, as the new page doesn't know to
        "set-hook -g client-attached 'if -F \"#{pane_in_mode}\" \"send -X cancel\"'",
        // the alternate screen (less, man) without the mouse gets arrow keys, as Ghostty's alternate scroll; an app that asked for the mouse, its events
        "bind -n WheelUpPane if -F '#{||:#{pane_in_mode},#{mouse_any_flag}}' 'send -M' 'if -F \"#{alternate_on}\" \"send Up\" \"copy-mode -e\"'",
        "bind -n WheelDownPane if -F '#{||:#{pane_in_mode},#{mouse_any_flag}}' 'send -M' 'if -F \"#{alternate_on}\" \"send Down\"'"]
    // ponytail: the two lists are set at a fixed index, as an append (-as) grew them on every attach
    static var tmux: String { "tmux -L \(shellQuote(tmuxSocketName)) -f /dev/null" }
    /// Escape a string as a tmux double-quoted argument (`\`, `"`, and `$`).
    static func tmuxQuote(_ s: String) -> String {
        "\"" + s
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "$", with: "\\$") + "\""
    }
    /// Escape literal `#` as `##` for tmux commands that run status-left format expansion (`pipe-pane`).
    static func tmuxFormatEscape(_ s: String) -> String {
        s.replacingOccurrences(of: "#", with: "##")
    }
    /// Idempotent tmux hooks (`after-new-session[1]`, `client-attached[1]`) that attach `pane_log.py record`
    /// via `pipe-pane -O` when `#{pane_pipe}` is 0. Safe on remote hosts: no-ops cleanly if `pane_log.py` is absent.
    static func paneRecorderHooks() -> [String] {
        let script = tmuxFormatEscape("\(shellPath(greenlightCode))/scripts/pane_log.py")
        let home = tmuxFormatEscape(shellPath(greenlightHome))
        let sock = tmuxFormatEscape(shellQuote(tmuxSocketName))
        let pipeShell = "[ -f \(script) ] && GREENLIGHT_HOME=\(home) exec python3 \(script) record --pane #{pane_id} --socket \(sock)"
        let innerPipe = "pipe-pane -O " + tmuxQuote(pipeShell)
        let hookVal = "if -F \"#{pane_pipe}\" \"\" " + tmuxQuote(innerPipe)
        return [
            "set-hook -g 'after-new-session[1]' \(shellQuote(hookVal))",
            "set-hook -g 'client-attached[1]' \(shellQuote(hookVal))"
        ]
    }
    /// What a pane prints just before it runs tmux: the page sends tmux's wheel events (terminal.html `wheel()`) only after it.
    static let tmuxMark = "\\033]6973;greenlight-tmux\\007"
    /// The pane's command: tmux (or a plain login shell where there's none) in the workspace, here or on GREENLIGHT_HOST over ssh.
    /// `run`: a command too long to type (terminal.html `newPane`). One interactive login shell evals it, then becomes a login shell:
    /// `trap : INT` keeps a ^C (or a stray quote) from ending the pane, and nothing is left to hand the terminal back to. It's in the
    /// command once, as `r`. tmux gets it through a temp file, as it refuses a command over 16 KB; a live session (a reconnect that
    /// raced the mark) ignores it, so none then. No tmux, or no file (`>|`: noclobber), and this shell runs it. `runMark` tells the
    /// page it's handled, so a reconnect doesn't send it again. Known gap: zsh sets the trap after its rc files, so a ^C while
    /// `.zshrc` loads ends the pane (click the move again); so does a drop in the instant between the mark and tmux's session.
    static let runMark = "\\033]6973;greenlight-run\\007"
    func paneCmd(session: String, cwd: String, on host: String, run: String = "") -> String {
        let go = { (arg: String) in "exec $SHELL -lic " + shellQuote("trap : INT; eval \"\(arg)\"; exec $SHELL -l") + " greenlight " }
        let inner = (cwd.isEmpty ? "cd; " : "cd \(shellQuote(cwd)) 2>/dev/null || cd; ")
            + (run.isEmpty ? "command -v tmux >/dev/null || exec $SHELL -l; "
                : "r=\(shellQuote(run)); f=; if ! command -v tmux >/dev/null; then :; "
                + "elif \(App.tmux) has-session -t \(shellQuote("=" + session)) 2>/dev/null; then f=live; "
                + "elif f=$(mktemp -t greenlight-run.XXXXXX) && printf %s \"$r\" >| \"$f\"; then :; else [ -z \"$f\" ] || rm -f \"$f\"; f=; fi; "
                + "printf '\(App.runMark)'; [ -n \"$f\" ] || \(go("$1"))\"$r\"; ")
            + "printf '\(App.tmuxMark)'; exec \(App.tmux) start-server \\; "
            + (App.tmuxOpts + App.paneRecorderHooks()).joined(separator: " \\; ") + " \\; new-session -A -s \(shellQuote(session)) -c \"$PWD\""
            + (run.isEmpty ? "" : " " + shellQuote(go("$(cat \"$1\"; rm -f \"$1\")") + "'") + "\"$f\"" + shellQuote("'"))  // -A onto a live session: tmux ignores it
        if host.isEmpty || host == "local" { return inner }
        // -q: a lost connection still shows (exit 255). ControlPath=none: each pane its own connection, as a wedged ControlMaster
        // (~/.ssh/config's ControlMaster auto) left every new pane hanging; ponytail: a full handshake per pane, fine at tab counts
        return "exec ssh -qt -o ControlPath=none -o ServerAliveInterval=30 \((devSsh(host) + ["--", host]).map(shellQuote).joined(separator: " ")) \(shellQuote("exec $SHELL -lc " + shellQuote(inner)))"
    }
    var termLoaded = false, treesProc: Process?, localTreesProc: Process?, serverTreesObj: [String: Any]?, localTreesObj: [String: Any]?, treesAt = Date.distantPast, termBadges: [Int: Int] = [:], tunnels: [String: Process] = [:]
    /// Open in editor's app: Zed, Cursor or VS Code, whichever is installed first (the page names its buttons after it).
    func editorApp() -> (bid: String, url: URL, name: String)? {
        [("dev.zed.Zed", "Zed"), ("com.todesktop.230313mzl4w4u92", "Cursor"), ("com.microsoft.VSCode", "VS Code")].lazy
            .compactMap { i, n in NSWorkspace.shared.urlForApplication(withBundleIdentifier: i).map { (i, $0, n) } }.first
    }
    func tunnelState(_ key: String, _ st: String) {  // a port row's tunnel: opening, open, failed, closed
        let a = String(data: (try? JSONSerialization.data(withJSONObject: [key, st])) ?? Data(), encoding: .utf8) ?? "[]"
        termWebs.forEach { $0.evaluateJavaScript("window.term && term.tunnel(...\(a))") }
    }
    func toastTerm(_ web: WKWebView?, _ text: String) {
        let d = (try? JSONSerialization.data(withJSONObject: text, options: .fragmentsAllowed)).flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
        web?.evaluateJavaScript("term.toast(\(d))")
    }
    private func publishTrees() {
        let obj = treesToPublish(server: &serverTreesObj, local: &localTreesObj, merge: !isLocalHost(host), primaryHost: host)
        if let obj, JSONSerialization.isValidJSONObject(obj) {
            lastTreesObj = obj; paneReplay.add(obj["panes"])
            refreshProjects()
            termWebs.forEach { $0.callJS("window.term && term.workspaces", [obj]) }
        } else if !projectsLoaded {
            refreshProjects()  // fallback to projects.json so filtered L1 windows never hang on a failed trees fetch
        }
    }

    /// Every installed ADE's worktrees (dash.py trees), where greenlight lives (and local Mac when a remote server is configured).
    func pushTrees() {
        guard treesProc == nil else { return }
        treesAt = Date(); paneReplay.start()
        var out = ""
        treesProc = ssh("\(py)/dash.py trees", onLine: { out += $0 }, onExit: { [weak self] _ in
            guard let self else { return }
            self.treesProc = nil
            self.initialServerTreesDone = true
            if out.hasPrefix("{"), let d = out.data(using: .utf8),
               let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
               JSONSerialization.isValidJSONObject(obj) {
                self.serverTreesObj = obj
            }
            self.publishTrees()
        })
        if !isLocalHost(host) && localTreesProc == nil && !uitest && !selftest {
            var localOut = ""
            localTreesProc = ssh("\(pyCmd(on: "local"))/dash.py trees", on: "local", onLine: { localOut += $0 }, onExit: { [weak self] _ in
                guard let self else { return }
                self.localTreesProc = nil
                self.initialLocalTreesDone = true
                if localOut.hasPrefix("{"), let d = localOut.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                   JSONSerialization.isValidJSONObject(obj) {
                    self.localTreesObj = obj
                    self.publishTrees()
                }
            })
        }
    }
    func termMessage(_ b: [String: Any], from web: WKWebView?) {
        guard let wid = termId(web) else { return }  // a closed window's page, still flushing
        let tid = b["id"] as? Int ?? 0, id = "\(wid):\(tid)", cols = b["cols"] as? Int ?? 80, rows = b["rows"] as? Int ?? 24
        func js(_ o: Any) -> String { String(data: (try? JSONSerialization.data(withJSONObject: o, options: .fragmentsAllowed)) ?? Data(), encoding: .utf8) ?? "null" }
        // ids that reach a shell command: a tmux session name, a worktree path 
        let session = (b["session"] as? String).flatMap { $0.range(of: #"^mc-[\w-]{1,64}$"#, options: .regularExpression) != nil ? $0 : nil }
        if let c = b["cmd"] as? String, b["boot"] == nil, !["in", "size", "badge", "band", "title", "notify", "ready", "painted", "error", "localName", "trees", "turn"].contains(c) {
            Diag.event("action", ["cmd": c, "page": "terminal"])  // the command, never its payload; keystrokes and resizes aren't actions
        }
        switch b["cmd"] as? String {
        case "ready":
            for k in ptys.keys where k.hasPrefix("\(wid):") { ptys.removeValue(forKey: k)?.stop(); ptySession[k] = nil }  // a (re)loaded page starts with no tabs: detach, restore reattaches
            // ponytail: a reattach shows tmux's current screen only (older scrollback stays in tmux), and sessions a crash
            // orphaned before a save stay on the socket; reap ones not in the saved layout here if they pile up
            termLoaded = true
            func file(_ u: URL) -> String {  // read once; only valid JSON reaches the page
                (try? Data(contentsOf: u)).flatMap { d in (try? JSONSerialization.jsonObject(with: d)).map { _ in String(decoding: d, as: UTF8.self) } } ?? "null"
            }
            // the floor and the trees before the layout (issue 196): the sidebar is out before the first frame, and a one-project window
            // has its host and folder at restore, so its first shell doesn't wait on dash.py trees
            web?.evaluateJavaScript("term.projects(\(file(projectsFile)))")
            web?.evaluateJavaScript("term.slide(\(greenlight.sidebarSlide))")
            web?.callJS("window.term && term.opens", [wid != 0 || !opensBasement ? "pipe" : "basement"])  // New windows: always the Pipe
            if let obj = paneReplay.replay(lastTreesObj) { web?.callJS("window.term && term.workspaces", [obj]) }
            // a Basement button's worktree before the layout: restore opens it (pendingWs) in place of a first shell
            if wid == 0 { wantFloor = nil; if let p = wantWs { wantWs = nil; web?.callJS("term.ws", [p, wantResume]); wantResume = [:] } }
            web?.evaluateJavaScript("term.restore(\(file(termState(wid))))")  // safety-ok: the file only if it parses as JSON
            if lastTreesObj == nil || Date().timeIntervalSince(treesAt) > 5 { pushTrees() }
        case "painted": break  // its restored layout has drawn: userContentController reveals the window
        case "open":
            guard let session, let at = paneTarget(b["ws"] as? String ?? "") else { return }
            let p = Pty()
            p.onOutput = { [weak web] d in web?.evaluateJavaScript("term.feed(\(tid),'\(d.base64EncodedString())')") }  // safety-ok: base64 alphabet only
            p.onExit = { [weak web] code in web?.evaluateJavaScript("term.exited(\(tid), \(code))") }
            let run = b["run"] as? String ?? ""; if !run.isEmpty { greenlight.active() }  // as a typed command ("in") counts
            ptys[id] = p; ptySession[id] = session; p.start(cols: cols, rows: rows, cmd: paneCmd(session: session, cwd: at.cwd, on: at.host, run: run))
        case "in":
            ptys[id]?.write(b["d"] as? String ?? ""); greenlight.active()
        case "size": ptys[id]?.resize(cols: cols, rows: rows)
        case "close":  // you closed it: the session goes too (quitting the app only detaches)
            ptys.removeValue(forKey: id)?.stop(); ptySession[id] = nil
            if let session, !heldElsewhere(session, except: { $0 == id }), let at = paneTarget(b["ws"] as? String ?? "") { ssh("\(App.tmux) kill-session -t \(shellQuote("=" + session)) 2>/dev/null", on: at.host) }
        case "hide": if wid == 0 { if !upstairs, !closeShare(termWindow) { hideLevel(termWindow) } } else { closeTermWindow(wid) }  // its last tab closed: as its close button (upstairs it's out of sight already)
        case "title": floors[wid]?.project = String((b["text"] as? String ?? "").prefix(80)); floors[wid]?.colors = b["colors"] as? [String] ?? []
            if let l = b["list"] as? [[String: Any]] { projList = l; projectsLoaded = true; syncL1(); syncDashPaths() }
        case "side": floors[wid]?.at = b["open"] as? Bool ?? false ? "basement" : "pipe"; if wid == 0 { termSide = b["open"] as? Bool ?? false; syncOrbs() }  // the green light shows only while the Basement's sidebar is open
        case "projects": saveProjects(b["projects"], from: web)
        case "pickdir":  // Projects' Choose folder…: the path goes back to the page, which checks it like a pasted one
            guard let win = web?.window else { return }
            let p = NSOpenPanel(); p.canChooseDirectories = true; p.canChooseFiles = false
            p.beginSheetModal(for: win) { r in if r == .OK, let u = p.url { web?.evaluateJavaScript("term.picked(\(js(u.path)))") } }
        case "only":  // the title's Show projects: this window's set, changed in place (its shells stay)
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil || only!.count > 100 { return }
            setPick(only, except: web)  // every window and L1 follow
        case "window":  // New window: all projects (only null) or some, by id ("" is No Man's Land)
            let only = b["only"] as? [String]
            if b["only"] != nil, !(b["only"] is NSNull), only == nil { return }
            newTermWindow(only, from: web?.window)
        case "look":  // Customize look: the page wrote the whole prompt; the answer goes back raw, the page checks every value
            guard let text = b["text"] as? String, text.count <= 8000 else { return }
            // ponytail: Sonnet for looks (Haiku's palettes clashed); a Settings picker if you want to choose
            claudeP(text, model: "claude-sonnet-5-5") { [weak web] out in web?.evaluateJavaScript("term.looked(\(js(String(out.prefix(4000)))))") }
        case "band":  // the tab strip's computed color, so the title band matches it
            let c = b["color"] as? String ?? "", k = c.hasPrefix("color(") ? 1.0 : 255  // WebKit computes color-mix() as color(srgb r g b), 0–1
            let n = c.split(whereSeparator: { !"0123456789.".contains($0) }).compactMap { Double($0) }
            if n.count == 3 || (n.count == 4 && n[3] >= 0.99) { web?.window?.backgroundColor = NSColor(srgbRed: n[0] / k, green: n[1] / k, blue: n[2] / k, alpha: 1) }
        case "trees": pushTrees()
        case "turn":  // a Claude pane's spinner stayed gone: dash.py turn reads its screen (greenlight's agent_state), answered with term.turn
            guard let session, let ws = b["ws"] as? String, runsScripts(ws), let at = paneTarget(ws) else { return }  // @host shells: no scripts there; @local: this Mac's own
            var out = ""
            ssh("\(pyCmd(on: at.host))/dash.py turn \(shellQuote(session))", on: at.host, onLine: { out += $0 }, onExit: { [weak web] code in
                let o = out.data(using: .utf8).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? ["session": session, "error": "exit \(code)"]
                web?.callJS("window.term && term.turn", [o])
            })
        case "suggest":  // the sidebar card's next-step button: dash.py suggest (cached per state), answered with term.suggest
            if let name = b["name"] as? String, name.range(of: #"^[\w.@+/-]{1,200}$"#, options: .regularExpression) != nil { suggest(name) }
        case "tree":  // the sidebar's workspace menu: dash.py tree info|rm|goal|cfg|merge|new|diff <path>, answered with term.tree(json)
            guard let op = b["op"] as? String, sidebarOps.contains(op), (b["text"] as? String)?.count ?? 0 <= 8192, let path = b["path"] as? String,
                  path.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil, !path.contains("..") else { return }
            var out = ""
            let h = workspaceHost(path: path, fallback: greenlight.chatHost)
            ssh("\(pyCmd(on: h))/dash.py tree \(op) '\(path)'\(b["force"] as? Bool == true ? " --force" : "")", on: h, stdin: b["text"] as? String ?? "",
                onLine: { out += $0 }, onExit: { [weak self, weak web] code in
                let js = safeJSON(out, fallback: ["op": op, "path": path, "error": "\(hostDisplayName(h)) didn't answer (exit \(code))"])
                web?.evaluateJavaScript("term.tree(\(js))")
                if op != "info" && op != "diff" { self?.pushTrees() }
                if let what = actedVerb[op], let c = self?.greenlight { c.acted(op == "new" ? c.made(out) ?? c.wsName(path: path) : c.wsName(path: path), what, code, out) }
            })
        case "dwim":  // ⌘J's palette, typed with nothing to jump to: a board command (dwim.py), else a question for Chief in the chat
            guard let t = (b["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty, t.count <= 300 else { return }
            let c = greenlight
            let onToast: (DwimToast?) -> Void = { [weak web] t in  // each palette command reports its own toast to its window
                guard let t else { return }
                let d = (try? JSONSerialization.data(withJSONObject: [t.pending ? "\(t.text) in \(Int(Core.launchGrace)) s" : t.text, t.undoable ? (t.pending ? "Cancel" : "Undo") : "", t.id.uuidString])).map { String(decoding: $0, as: UTF8.self) } ?? "[]"
                web?.evaluateJavaScript("window.term && term.dwimToast && term.dwimToast(...\(d))")
            }
            let ask = { [weak self, weak web] (note: String) in
                c.send(note + t, shown: t)
                self?.setChat(hidden: false)
                self?.toastTerm(web, "Asked \(c.name): the answer is in the chat")
            }
            if dwimCandidate(t) { c.dwim(t, source: "jump", onToast: onToast, otherwise: ask) } else { ask("") }
        case "dwimUndo": greenlight.undoToast((b["id"] as? String).flatMap(UUID.init(uuidString:)))
        case "url":  // a link in a terminal: web links only, the output isn't ours to trust
            if let u = URL(string: b["url"] as? String ?? ""), ["http", "https"].contains(u.scheme?.lowercased() ?? "") { NSWorkspace.shared.open(u) }
        case "settings":  // the hover card's "Edit <repo>'s prompt…"
            if let r = b["repo"] as? String, r.range(of: #"^[\w.-]{1,100}$"#, options: .regularExpression) != nil, !r.hasPrefix(".") {
                let k = b["kind"] as? String ?? "", kind = RulesStore.kinds.contains { $0.0 == k } ? "/" + k : ""
                greenlight.settingsTab = "Repositories"; greenlight.settingsRepo = r + kind; appSettings()
            } else { appSettings() }  // Jump › Settings
        case "up": appGoUp()  // Jump › Greenlight Center
        case "localName":  // the sidebar's Rename on this Mac
            let n = String((b["name"] as? String ?? "").trimmingCharacters(in: .whitespaces).prefix(60)); greenlight.localName = n == localDefault ? "" : n
        case "notify":  // auto-run's jobs, and an agent that finished or wants you (the page sends one per change, any window)
            if let t = b["title"] as? String, let body = b["body"] as? String { greenlight.notify(title: String(t.prefix(200)), body: String(body.prefix(500)), subtitle: nil, reply: false) }
        case "copy":
            if let s = b["text"] as? String { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(s, forType: .string) }
        case "save":
            if let st = b["state"], JSONSerialization.isValidJSONObject(st), let d = try? JSONSerialization.data(withJSONObject: st) {
                try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
                try? d.write(to: termState(wid), options: .atomic)
            }
        case "name":
            let tab = b["tab"] as? Int ?? 0, auto = b["auto"] as? Bool ?? false
            let reply: (String) -> Void = { [weak web] n in web?.evaluateJavaScript("term.named(\(tab), \(js(n)))") }
            if auto && !greenlight.termAutoName { return reply("") }  // still answer: the page counts auto runs in flight
            aiTabName(b["text"] as? String ?? "", model: greenlight.termNameModel, effort: greenlight.termNameEffort, done: reply)
        case "badge":  // agents waiting on you, per window: the Dock icon shows the sum
            termBadges[wid] = max(0, b["n"] as? Int ?? 0)
            let n = termBadges.values.reduce(0, +); NSApp.dockTile.badgeLabel = n > 0 ? "\(n)" : nil
        case "file":  // a dropped or pasted file: written where the pane's shell runs (uid-private 0700 dir, 0600 file), its path goes back to be pasted
            guard let at = paneTarget(b["ws"] as? String ?? ""), let data = b["data"] as? String, data.count <= 14_000_000,
                  data.range(of: #"^[A-Za-z0-9+/=]*$"#, options: .regularExpression) != nil else { return }
            let fname = droppedFileName(b["name"] as? String ?? "")
            var writtenPath = ""
            ssh("umask 077; d=\"${XDG_RUNTIME_DIR:-/tmp}/greenlight-$(id -u)\"; [ ! -L \"$d\" ] && mkdir -p \"$d\" && chmod 700 \"$d\" && [ -O \"$d\" ] && p=\"$d/\"\(shellQuote(fname)) && base64 -d > \"$p\" && printf '%s\\n' \"$p\"",
                on: at.host, stdin: data, onLine: { writtenPath = $0 }, onExit: { [weak web] code in
                web?.callJS("window.term && term.filed", [tid, code == 0 && !writtenPath.isEmpty ? writtenPath : ""])
            })
        case "port":  // a dev-server port chip: localhost here, else through an ssh tunnel to the pane's host first; its state goes back to every window
            guard let port = b["port"] as? Int, (1024...65535).contains(port), let at = paneTarget(b["ws"] as? String ?? ""),
                  let u = URL(string: "http://localhost:\(port)") else { return }
            if at.host.isEmpty || at.host == "local" { NSWorkspace.shared.open(u); return }
            let key = "\(at.host):\(port)"
            if tunnels[key]?.isRunning == true { NSWorkspace.shared.open(u); return tunnelState(key, "open") }
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
            p.arguments = ["-N", "-o", "BatchMode=yes", "-o", "ControlPath=none", "-o", "ExitOnForwardFailure=yes", "-L", "\(port):localhost:\(port)"] + devSsh(at.host) + ["--", at.host]
            p.standardError = FileHandle.nullDevice
            // ControlPath=none: through a ControlMaster the forward lands on the shared master and this ssh exits at once, so it would outlive the app
            p.terminationHandler = { [weak self] q in DispatchQueue.main.async { if self?.tunnels[key] === q { self?.tunnels[key] = nil; self?.tunnelState(key, q.terminationStatus == 0 ? "closed" : "failed") } } }  // ssh exiting on its own (port taken, host down, dropped): failed; Cancel removes it first
            guard (try? p.run()) != nil else { return tunnelState(key, "failed") }
            tunnels[key] = p; tunnelState(key, "opening")
            // ponytail: 1.5s for ssh to bind; a local port already in use makes ssh exit (ExitOnForwardFailure), which reads as failed
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                guard self?.tunnels[key] === p else { return }
                if p.isRunning { NSWorkspace.shared.open(u); self?.tunnelState(key, "open") } else { self?.tunnelState(key, "failed") }
            }
        case "untunnel":  // Cancel / Close on a port row: ends that tunnel
            guard let port = b["port"] as? Int, let at = paneTarget(b["ws"] as? String ?? "") else { return }
            let key = "\(at.host):\(port)"
            if let p = tunnels.removeValue(forKey: key) { p.terminate() }
            tunnelState(key, "closed")
        case "edit":  // Open in editor: Zed, Cursor or VS Code, whichever is installed first; over ssh for a remote host
            guard let at = paneTarget(b["ws"] as? String ?? ""), !at.cwd.isEmpty else { return }
            guard let (bid, app, _) = editorApp() else { return toastTerm(web, "No Zed, Cursor or VS Code installed") }
            switch editorOpen(bid: bid, host: at.host, cwd: at.cwd) {
            case .folder(let dir)?: NSWorkspace.shared.open([URL(fileURLWithPath: dir)], withApplicationAt: app, configuration: NSWorkspace.OpenConfiguration())
            case .zedCLI(let arg)?:  // zed's cli opens ssh://host/path as a remote project
                let p = Process(); p.executableURL = app.appendingPathComponent("Contents/MacOS/cli"); p.arguments = [arg]; try? p.run()
            case .url(let u)?: NSWorkspace.shared.open(u)
            case nil: toastTerm(web, "Couldn't open \(at.cwd) on \(at.host)")
            }
        case "error":
            NSLog("greenlight terminal error: %@", b["text"] as? String ?? "")
            Diag.event("js_error", ["page": "terminal", "name": b["name"] ?? NSNull(), "line": b["line"] ?? NSNull(), "col": b["col"] ?? NSNull()])
        default: break
        }
    }

    /// The orbs ride with the dashboard: shown while it's up (unless Settings → Appearance turns them off), at normal
    /// window level so other apps cover them, and raised back over the dashboard whenever a click would bury them.
    /// They're child windows of the dashboard, kept inside it: they move with it, and a resize pulls them back in.
    /// One orb, two faces: in the Basement the green light (only while its sidebar is open) goes up; in Greenlight the terminal button goes down.
    func syncOrbs() {
        guard !riding else { return }
        let host = orbHost
        guard host.isVisible, host.isOnActiveSpace else { return }  // else it'd land in whatever Space is in front
        orbState.up = !upstairs
        if greenlight.orbsHidden || (!upstairs && !termSide) { hideFloaters(); return }
        if termOrb.parent !== host { hideFloaters() }
        if !termOrb.isVisible {
            if let k = keepAt {  // ponytail: clamped into the new level's area, so a spot outside it (the chat, the footer) still shifts
                let a = orbArea, w = termOrb.frame.size
                ud.set(NSStringFromPoint(NSPoint(x: a.maxX - k.x - w.width / 2, y: k.y - w.height / 2 - a.minY)), forKey: "termOrbOff")
            }
            keepAt = nil; placeOrb(termOrb); host.addChildWindow(termOrb, ordered: .above) }
    }
    var orbHost: NSWindow { upstairs ? window : termWindow }

    // MARK: One window size for every floor (issue 177): Center, the Basement and the Pipe share a frame and a Space

    /// Center's smallest frame: its dashboard, and the chat pane when it's docked. The orb's terminal window has it too.
    var centerMinSize: NSSize { NSSize(width: (!greenlight.chatOut && !chatHidden) ? App.paneMinWidth : 480, height: 520) }
    /// The full-screen window a floor switch left (`host`, kept in its Space underneath) and the floor shown over it (`guest`,
    /// a full-screen auxiliary at its frame), so every floor stays on that one Space. `frame`: the guest's to go back to.
    var fsShare: (host: NSWindow, guest: NSWindow, frame: NSRect, zoom: (AnyObject?, Selector?))?
    var preFullScreen = [ObjectIdentifier: NSRect](), fsClosing = false

    /// A floor switch from `from` to `to`: `to` takes `from`'s frame, so the size never changes. Out of a full-screen
    /// window, `to` joins that window's Space rather than making one of its own; back to it, the guest steps out again.
    func hand(_ to: NSWindow, from: NSWindow?) {
        guard let from, from !== to else { return }
        if let sh = fsShare, to === sh.guest, from !== sh.host { leaveShare(); stepAside(to) }  // a window on another monitor takes the guest back; shown again by its caller
        if let sh = fsShare, from === sh.host || from === sh.guest {
            if to === sh.guest { return }
            leaveShare()
            if to !== sh.host, !to.styleMask.contains(.fullScreen) { joinShare(to, host: sh.host) }  // one in full screen keeps its own Space
            return
        }
        guard !to.styleMask.contains(.fullScreen) else { return }  // its own Space already: macOS takes you there
        if from.styleMask.contains(.fullScreen) { return joinShare(to, host: from) }
        let f = sharedFrame(from.frame, min: to.minSize, in: from.screen?.visibleFrame)
        if f != to.frame { to.setFrame(f, display: false) }
    }
    func joinShare(_ g: NSWindow, host: NSWindow) {
        leaveShare()  // one share at a time: another monitor's full-screen window takes the guest over
        restoreFrames[ObjectIdentifier(g)] = nil  // back in before it was out of sight
        let zb = g.standardWindowButton(.zoomButton)
        fsShare = (host, g, preFullScreen[ObjectIdentifier(host)] ?? sharedFrame(g.frame, min: g.minSize), (zb?.target, zb?.action))
        g.collectionBehavior.remove([.fullScreenPrimary, .fullScreenNone])
        g.collectionBehavior.insert(.fullScreenAuxiliary)  // shown with the full-screen window, in its Space; it can't make another
        zb?.target = self; zb?.action = #selector(exitSharedFullScreen)  // its green button: out of full screen, as the host's would
        g.setFrame(host.frame, display: false)
    }
    /// The guest out of the shared Space, a normal window again once it's out of sight: a ride still has to take its
    /// picture, and whoever switched floors orders it out (`stepAside`). Its frame goes back to the floors' own.
    func leaveShare() {
        guard let sh = fsShare else { return }
        fsShare = nil
        let zb = sh.guest.standardWindowButton(.zoomButton); zb?.target = sh.zoom.0; zb?.action = sh.zoom.1
        restoreFrames[ObjectIdentifier(sh.guest)] = sh.frame
        if !sh.guest.isVisible { unguest(sh.guest) }
    }
    var restoreFrames = [ObjectIdentifier: NSRect]()  // per window: a guest left on another monitor's Space keeps its own
    func unguest(_ w: NSWindow) {
        guard let f = restoreFrames.removeValue(forKey: ObjectIdentifier(w)) else { return }
        w.collectionBehavior.remove(.fullScreenAuxiliary)
        w.setFrame(f, display: false)
    }
    @objc func exitSharedFullScreen() { fsShare?.host.toggleFullScreen(nil) }
    /// Ordering a full-screen window out would empty its Space: the host stays in it, under its guest.
    /// ⌃⌥M or ⌘⇧T out of sight. Over Center or the orb's terminal, the app hides (both levels, the Space kept): the
    /// full-screen one couldn't be ordered out alone. Over an L1 or a New window, the guest just leaves, as closing it does.
    func hideLevel(_ w: NSWindow) {
        if let sh = fsShare, w === sh.guest || w === sh.host {
            if sh.host === window || sh.host === termWindow { return NSApp.hide(nil) }
            leaveShare(); stepAside(sh.guest); hideFloaters(); return
        }
        if w.styleMask.contains(.fullScreen) { return NSApp.hide(nil) }
        w.orderOut(nil); hideFloaters()
    }
    /// Ordering a full-screen window out would empty its Space: a host stays in it, under its guest, and any other
    /// full-screen window keeps its own (a New window's floor switch up to Center leaves a full-screen Basement be).
    func stepAside(_ w: NSWindow) {
        guard w !== fsShare?.host, !w.styleMask.contains(.fullScreen) else { return }
        w.orderOut(nil); unguest(w)
    }
    /// The host leaves full screen under its guest: the guest's floor, which you were on, comes back as a window at the host's frame.
    func fullScreen(_ n: Notification.Name, _ w: NSWindow) {
        if n == NSWindow.willEnterFullScreenNotification { preFullScreen[ObjectIdentifier(w)] = w.frame; return }
        if n == NSWindow.didEnterFullScreenNotification { fsClosing = false; return }  // a close mid-entry was ignored by AppKit: it doesn't stick
        if n == NSWindow.willExitFullScreenNotification {
            if let sh = fsShare, sh.host === w {  // out of the Space before it goes
                stayOn = (sh.guest, fsClosing, w)
                leaveShare(); stepAside(sh.guest)
            }
            fsClosing = false
            return
        }
        preFullScreen[ObjectIdentifier(w)] = nil
        guard let (g, closing, host) = stayOn, host === w else { return }
        stayOn = nil
        let pair = w === window || w === termWindow  // Center and the orb's terminal are one level at a time; an L1 or a New window stays up
        if closing { if pair { w.orderOut(nil) }; return }
        g.setFrame(sharedFrame(w.frame, min: g.minSize, in: w.screen?.visibleFrame), display: false)
        presentWindow(g); if pair { w.orderOut(nil) }
        syncOrbs()
    }
    var stayOn: (w: NSWindow, closing: Bool, host: NSWindow)?
    /// The guest's close button. Over Center or the orb's terminal, that level goes too, out of full screen first so its
    /// Space goes with it; over an L1 or a New window, the guest just leaves (as it does off full screen).
    func closeShare(_ w: NSWindow) -> Bool {
        guard let sh = fsShare, sh.guest === w else { return false }
        hideFloaters()
        if (sh.host === window || sh.host === termWindow) && sh.host.styleMask.contains(.fullScreen) { fsClosing = true; sh.host.toggleFullScreen(nil) }
        else { leaveShare(); stepAside(w) }
        return true
    }
    /// A share's window closing for good (an L1, a New window), or the app quitting: the guest back to a normal window.
    func dropShare(_ w: NSWindow? = nil) {
        guard let sh = fsShare, w == nil || w === sh.host || w === sh.guest else { return }
        leaveShare(); unguest(sh.guest)
    }
    /// View › Enter Full Screen (⌃⌘F) from a guest acts on the window it's in the Space of, as its green button does:
    /// the guest itself would make a second Space.
    @objc func toggleFullScreen(_ sender: Any?) {
        if let sh = fsShare, NSApp.keyWindow === sh.guest { return sh.host.toggleFullScreen(nil) }
        NSApp.keyWindow?.toggleFullScreen(nil)
    }

    // MARK: Basement ↔ Greenlight Center: one level at a time, the orb rides between them

    var rideTo: NSWindow?  // the level a ride is bringing in (it sets that one's alpha to 1 at its end)
    var upstairs = false, riding = false, termSide = false, rideWin: NSPanel?, keepAt: NSPoint?
    @objc func appGoUp() { if !upstairs { ride(up: true) } else { show() } }
    func ride(up: Bool) {
        guard !riding, up != upstairs else { return }
        Diag.event("floor", ["to": up ? "upstairs" : "basement"])
        let from = orbHost, to = up ? window : termWindow
        guard from.isVisible, let scr = from.screen ?? NSScreen.main else { return up ? show() : basement() }
        hand(to, from: from)  // before the snapshots: the other level grows into this one's frame
        if termOrb.isVisible { keepAt = NSPoint(x: termOrb.frame.midX, y: termOrb.frame.midY) }  // the orb stays put on screen; syncOrbs lands it there
        guard greenlight.rideTime > 0 else { stepAside(from); hideFloaters(); return up ? show() : basement() }  // Settings → Terminal → Ride animation 0: a cut
        riding = true; rideTo = to
        let S = scr.frame, reduce = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let c = termOrb.isVisible ? NSPoint(x: termOrb.frame.midX, y: termOrb.frame.midY) : NSPoint(x: from.frame.maxX - 44, y: from.frame.minY + 44)
        let orb = CGPoint(x: c.x - S.minX, y: S.maxY - c.y)
        func shot(_ w: NSWindow, _ img: NSImage?) -> Shot {
            Shot(img: img, band: w === window ? DS.Palette.bgChrome.ns(dark: isDark) : w.backgroundColor,
                 rect: CGRect(x: w.frame.minX - S.minX, y: S.maxY - w.frame.maxY, width: w.frame.width, height: w.frame.height), top: w.frame.height - w.contentLayoutRect.height)
        }
        let k = reduce ? 1 : greenlight.rideTime / 0.6  // every beat below is written for a 0.6s ride
        func later(_ t: Double, _ f: @escaping () -> Void) { DispatchQueue.main.asyncAfter(deadline: .now() + t * k, execute: f) }
        func at(_ t: Double, _ d: Double, _ curve: (Double) -> Animation = Animation.easeInOut, _ f: @escaping () -> Void) {
            let a = curve(d * k); later(t) { withAnimation(a, f) }
        }
        snap(from) { [self] img in
            let s = RideState(); s.tone = up ? 0 : 1; s.faceUp = up
            let p = NSPanel(contentRect: S, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = false; p.ignoresMouseEvents = true; p.level = .floating
            p.collectionBehavior = [.fullScreenAuxiliary, .transient]  // a full-screen level's ride plays in its Space
            p.contentView = NSHostingView(rootView: RideView(up: up, from: shot(from, img), orb: orb, face: !greenlight.orbsHidden, s: s))
            p.setFrame(S, display: true); p.orderFrontRegardless(); rideWin = p
            hideFloaters()
            termWindow.parent?.removeChildWindow(termWindow)
            // the other level comes up behind the overlay, all but invisible, so it's drawn by the time it grows
            to.alphaValue = 0.01
            DispatchQueue.main.async { [self] in stepAside(from); if up { show() } else { basement() } }  // once the picture covers it
            // one smooth stroke each way, no stops: in to the orb (0.3s), the orb flips, out of it (0.3s)
            if reduce { at(0, 0.2) { s.from.a = 0; s.veil = 0.92 } } else {  // Reduce Motion: a crossfade
                at(0, 0.3, Animation.easeIn) { s.from = Squash(sx: 0.02, sy: 0.02, r: 300, a: 0) }
                at(0, 0.3) { s.veil = 0.92 }
                at(0.14, 0.09, Animation.easeIn) { s.flip = 90 }; at(0.23, 0) { s.faceUp = !up; s.flip = -90 }; at(0.24, 0.1, Animation.easeOut) { s.flip = 0 }  // a coin flip to the other face
            }
            if reduce { at(0.23, 0) { s.faceUp = !up } }
            at(0, 0.3) { s.tone = up ? 1 : 0 }  // the backdrop is the new level's color before that level grows
            DispatchQueue.main.asyncAfter(deadline: .now() + max(0.3 * k, 0.1)) { [self] in  // ponytail: a 0.1s floor so a very short ride doesn't snap the level before it's drawn
                whenPainted(to.contentView as? WKWebView, within: 1.5) { [self] in snap(to) { [self] img in
                    s.toShot = shot(to, img)
                    if reduce { s.to = Squash(a: 0); at(0, 0.2) { s.to.a = 1; s.veil = 0 } } else {
                        at(0, 0.3, Animation.easeOut) { s.to = Squash() }
                    }
                    // the backdrop lifts only once the level is in place, so what's behind the app never shows through mid-ride
                    later(reduce ? 0.22 : 0.3) { to.alphaValue = 1; if !reduce { at(0, 0.18) { s.veil = 0 } } }
                    later(reduce ? 0.22 : 0.5) { [self] in
                        rideWin?.orderOut(nil); rideWin = nil; riding = false; rideTo = nil
                        keepAt = c; syncOrbs()  // the orb lands where you clicked it
                    }
                } }
            }
        }
    }
    /// A window's content as a picture: its views as drawn, each web view's own snapshot over its spot (they draw out of process).
    func snap(_ w: NSWindow, _ done: @escaping (NSImage?) -> Void) {
        guard let v = w.contentView, v.bounds.width > 0, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) else { return done(nil) }
        v.cacheDisplay(in: v.bounds, to: rep)
        let base = NSImage(size: v.bounds.size); base.addRepresentation(rep)
        let webs = ([fluid] + termWebs).filter { $0.window === w && !$0.isHiddenOrHasHiddenAncestor }
        var parts = [(NSImage, NSRect)](), left = webs.count
        func finish() {
            let over = [inboxHost].filter { $0.window === w && !$0.isHiddenOrHasHiddenAncestor }.compactMap { o -> (NSImage, NSRect)? in  // over the web views, as on screen
                guard let rep = o.bitmapImageRepForCachingDisplay(in: o.bounds) else { return nil }
                o.cacheDisplay(in: o.bounds, to: rep); let i = NSImage(size: o.bounds.size); i.addRepresentation(rep)
                return (i, o.convert(o.bounds, to: v))
            }
            done(NSImage(size: v.bounds.size, flipped: v.isFlipped) { _ in
                for (i, r) in [(base, v.bounds)] + parts + over { i.draw(in: r, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil) }
                return true
            })
        }
        if webs.isEmpty { return finish() }
        for web in webs {
            let r = web.convert(web.bounds, to: v)
            web.takeSnapshot(with: nil) { img, _ in if let img { parts.append((img, r)) }; left -= 1; if left == 0 { finish() } }
        }
    }
    /// The dashboard pane (left of the chat) on screen, less the page's context rail (--dock-w 64) and footer (--foot-h 32)
    /// so the orb never covers them, and a small margin.
    var orbArea: NSRect {
        if !upstairs { return termWindow.convertToScreen(termWindow.contentLayoutRect).insetBy(dx: 8, dy: 8) }
        let d = split.arrangedSubviews[0], r = window.convertToScreen(d.convert(d.bounds, to: nil))
        return NSRect(x: r.minX, y: r.minY + 32, width: r.width - 64, height: r.height - 32).insetBy(dx: 8, dy: 8)
    }
    func splitViewDidResizeSubviews(_ n: Notification) { if termOrb.isVisible { placeOrb(termOrb) } }
    /// Saved as an offset from the window's bottom-right corner, where it starts.
    func placeOrb(_ p: NSWindow) {
        let a = orbArea, s = p.frame.size
        let off = ud.string(forKey: "termOrbOff").map(NSPointFromString) ?? NSPoint(x: 8, y: 8)
        p.setFrameOrigin(NSPoint(x: min(max(a.maxX - off.x - s.width, a.minX), a.maxX - s.width), y: min(max(a.minY + off.y, a.minY), a.maxY - s.height)))
    }
    func saveOrb(_ p: NSWindow?, _ key: String) {
        guard let p else { return }
        let a = orbArea; ud.set(NSStringFromPoint(NSPoint(x: a.maxX - p.frame.maxX, y: p.frame.minY - a.minY)), forKey: key)
    }
    func windowDidResize(_ n: Notification) {
        // its parent, not orbHost: that reads the lazy termWindow, and this fires while termWindow is being made
        if termOrb.isVisible, n.object as? NSWindow === termOrb.parent { placeOrb(termOrb) }
    }
    // only when its own window is in front: ordering a child window front drags its parent over whatever you clicked (a New window)
    func raiseOrbs() { DispatchQueue.main.async { [self] in if termOrb.isVisible, let p = termOrb.parent, p === NSApp.keyWindow { termOrb.orderFront(nil) } } }

    /// The dashboard went away: its orb goes with it.
    func hideFloaters() { termOrb.parent?.removeChildWindow(termOrb); termOrb.orderOut(nil) }

    func windowShouldClose(_ w: NSWindow) -> Bool {
        if w === chatWindow { popChat(out: false); return false }
        if NSApp.currentEvent?.type != .keyDown, closeShare(w) { return false }  // the didExit that follows hides the host; the guest's gone already
        if let wid = termWins.first(where: { $0.value === w })?.key {  // ⌘W closes the tab; the close button hides the orb's window (shells kept), closes a New window one (shells killed)
            if NSApp.currentEvent?.type == .keyDown { (w.contentView as? WKWebView)?.evaluateJavaScript("term.closeTab()") }
            else if wid == 0 { w.orderOut(nil); hideFloaters() } else { closeTermWindow(wid) }
            return false
        }
        if let v = viewWindows.first(where: { $0.value === w })?.key { attach(v); return false }
        if let l = l1s.values.first(where: { $0.w === w }) { closeL1(l) }
        if w === window { hideFloaters() }
        return true
    }
    func applicationDidBecomeActive(_ n: Notification) { if chatShown { greenlight.missed = 0 }; if window.isVisible { greenlight.unread = 0; tick(); raiseOrbs() } }

    /// Menubar icon: outline star idle, filled star + elapsed while working, outline + count for unread pings.
    func tick() {
        if hintStale { (hintCore ?? greenlight).hint = "" }  // a window hidden or moved mid-hover: drop the hint
        if greenlight.prompting, tab.isVisible != NSApp.isActive { NSApp.isActive ? tab.orderFront(nil) : tab.orderOut(nil) }  // the prompt waits with the recorder
        if case let t = l1Title(l1Floor.project), window.title != t { window.title = t }  // the Window menu tells the Greenlight Centers apart
        if NSApp.appearance?.name != appAppearance?.name { NSApp.appearance = appAppearance }  // Settings › Appearance: System, Light or Dark, every window
        if pushedDark != isDark { pushedDark = isDark; greenlight.look += 1 }  // the appearance flipped, so every view re-renders
        if pushedTerm != theme.name { pushedTerm = theme.name  // the terminals' chrome follows the mode; their screens stay as they are
            let m = termMode; termWebs.forEach { $0.callJS("window.term && term.mode", [m["mode"]!, m["lightBg"]!, m["lightFg"]!]) }
            if projectsLoaded { refreshProjects() } }
        if greenlight.run != nil { greenlight.now = Date() }
        greenlight.checkIn()
        greenlight.watchTurn()  // one held back by a running turn or the 5-minute gap
        if lastTreesObj == nil && Date().timeIntervalSince(treesAt) > 10 { pushTrees() }
        else if termLoaded, Date().timeIntervalSince(treesAt) > (termWins.values.contains(where: \.isVisible) ? 30 : devDir == nil ? 60 : .infinity) { pushTrees() }  // keep workspaces and Basement pane states fresh while hidden (auto-run and background-tab alerts)
        syncOrbs()
        let bell = "\(greenlight.waiting.count), \(greenlight.inboxOpen), \(greenlight.askedJSON)"  // re-pushed whenever a question changes, not only the count
        if pushedBell != bell { pushedBell = bell; broadcast("window.mc && mc.bell(\(bell))") }
        let orb = greenlight.orbsHidden ? "0px" : "72px"  // the shelf keeps the orb's 56pt and some air clear at its right end
        if pushedOrb != orb { pushedOrb = orb; fluid.callJS("((v) => { document.documentElement.style.setProperty('--orb-w', v); window.render && render(); })", [orb]) }
        syncDashPaths()
        for l in l1s.values {  // the other Greenlight Center windows: their own projects, their chat always in its pane
            if l.pushedChat.isEmpty { l.pushedChat = "true,0"; l.web.callJS("window.mc && mc.chat", [true, 0]) }
            if case let t = l1Title(l.floor.project), l.w.title != t { l.w.title = t }  // the Window menu tells them apart
            if l.c.run != nil { l.c.now = Date() }
            l.c.serverConnected = greenlight.serverConnected
            let wantConnected = isLocalHost(l.c.chatHost) ? true : greenlight.serverConnected
            if l.c.run == nil, l.c.connected != wantConnected { l.c.connected = wantConnected }
        }
        if pushedLocal != greenlight.localShown { pushedLocal = greenlight.localShown; let js = (try? JSONEncoder().encode(pushedLocal)).flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
            termWebs.forEach { $0.evaluateJavaScript("window.term && term.localName(\(js))") } }
        if pushedSlide != greenlight.sidebarSlide { pushedSlide = greenlight.sidebarSlide; termWebs.forEach { $0.evaluateJavaScript("window.term && term.slide(\(greenlight.sidebarSlide))") } }
        if pushedMotion != greenlight.rideTime { pushedMotion = greenlight.rideTime
            (Array(termWins.values) + [window]).forEach { $0.animationBehavior = greenlight.rideTime > 0 ? .default : .none } }  // Animations off: no zoom in or out as a level comes or goes
        let chat = "\(!chatHidden),\(greenlight.missed)"
        if pushedChat != chat { pushedChat = chat; mainWebs.forEach { $0.evaluateJavaScript("window.mc && mc.chat(\(chat))") } }
        let colors = themeVars()
        if pushedMono != codeFont + colors {  // the dashboard's logs: Settings' code font, Nerd Font icons full-size like the terminal's
            pushedMono = codeFont + colors
            let stack = #""\#(codeFont)", "Symbols Nerd Font", "Hack Nerd Font", "MesloLGS Nerd Font", "MesloLGS NF", ui-monospace, Menlo, monospace"#
            let js = String(data: (try? JSONSerialization.data(withJSONObject: [stack])) ?? Data(), encoding: .utf8) ?? "[]"
            broadcast("(s => { s.setProperty('--mono', \(js)[0]); \(colors) })(document.documentElement.style)")
        }
        if chatWindow.title != greenlight.name { chatWindow.title = greenlight.name }
        guard let b = item?.button else { return }
        // Touch the status item only on change: any menubar update keeps a full-screen window's title bar pinned down.
        let working = greenlight.run != nil
        let title = (devName.map { " " + ($0.count > 18 ? String($0.prefix(17)) + "…" : $0) } ?? "")  // a copy's item is named: the stars are all alike
            + (greenlight.run.map { " " + fmt(Date().timeIntervalSince($0.start)) } ?? (greenlight.unread > 0 ? " \(greenlight.unread)" : ""))
        let key = "\(working)"
        if key != starKey {
            starKey = key
            b.image = GreenlightMark.image(working ? .lit : .idle)  // the mark, a template always: the menu bar stays monochrome (§8.4)
            item?.length = working && devName == nil ? 64 : NSStatusItem.variableLength  // fixed while the timer ticks, or the menubar icons jitter each second (a copy's name doesn't fit 64)
        }
        if b.title != title { b.title = title }
    }
    var starKey = ""

    /// What a page needs to wear the app's choices (§2.3, §2.5): its data-accent, which the pages' token sets pick the accent up
    /// from, and --mc-* only for a choice that isn't the default: a terminal theme of your own (its surfaces and text) or an accent
    /// other than Signal (for the pages not yet on the tokens). The defaults send none, so every page looks as designed.
    func themeVars() -> String {
        func css(_ c: NSColor) -> String {
            let c = c.usingColorSpace(.sRGB) ?? c
            return String(format: "#%02X%02X%02X", Int(c.redComponent * 255 + 0.5), Int(c.greenComponent * 255 + 0.5), Int(c.blueComponent * 255 + 0.5))
        }
        let own = theme.name != (theme.dark ? "Greenlight" : "GitHub Light"), a = accentSet, tinted = a.id != "signal"
        let vars: [(String, String?)] = [("bg", theme.bg), ("surface", theme.blend(0.05)), ("raised", theme.blend(0.1)),
                                         ("text", theme.fg), ("text2", theme.blend(0.82)), ("dim", theme.blend(0.6))].map { ($0.0, own ? css($0.1) : nil) }
            + [("accent", tinted ? "#" + dsHex(a.solid, dark: isDark) : nil), ("on-accent", tinted ? "#" + dsHex(a.onSolid, dark: isDark) : nil)]
        return "document.documentElement.dataset.accent = '\(a.id)'; "  // one of three fixed ids
            + vars.map { k, v in v.map { "s.setProperty('--mc-\(k)', '\($0)');" } ?? "s.removeProperty('--mc-\(k)');" }.joined(separator: " ")
    }

    @objc func click() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            let voice = NSMenu()
            for (i, (id, title)) in engines.enumerated() {
                let e = voice.addItem(withTitle: title, action: #selector(pickEngine(_:)), keyEquivalent: "")
                e.target = self
                e.tag = i
                e.state = greenlight.engine == id ? .on : .off
            }
            voice.addItem(.separator())
            voice.addItem(withTitle: "Shortcut: \(greenlight.shortcut)  —  Change…", action: #selector(setShortcut), keyEquivalent: "").target = self
            voice.addItem(withTitle: "Choose Whisper model…", action: #selector(pickModel), keyEquivalent: "").target = self
            voice.addItem(withTitle: "Set OpenAI key…", action: #selector(setKey), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Voice", action: nil, keyEquivalent: "").submenu = voice
            menu.addItem(.separator())
            menu.addItem(withTitle: "Quit Greenlight", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            item?.menu = menu
            item?.button?.performClick(nil)
            item?.menu = nil
        } else {
            toggle()
        }
    }


    @objc func pickEngine(_ m: NSMenuItem) { greenlight.engine = engines[m.tag].0 }

    @objc func pickModel() {
        let p = NSOpenPanel()
        p.message = "Pick a whisper.cpp ggml model (.bin)"
        p.directoryURL = URL(fileURLWithPath: whisperModel()).deletingLastPathComponent()
        NSApp.activate(ignoringOtherApps: true)
        if p.runModal() == .OK, let u = p.url { ud.set(u.path, forKey: "whisperModel") }
    }

    /// Jev's key: stored by decide.py where greenlight runs (it reads it there), not in this Mac's Keychain.
    func setTypesafeKey() {
        let a = NSAlert()
        a.messageText = "TypeSafe API key"
        a.informativeText = "Stored by decide.py on \(host.isEmpty || host == "local" ? "this Mac" : host) (GREENLIGHT_HOME/secrets, readable only by you). Used only for Jev, System One's quick decisions. Leave empty to remove it; TYPESAFE_API_KEY is used then, if it’s set."
        let f = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        a.accessoryView = f
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = f
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        greenlight.setSystemOneKey(f.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))
    }
    @objc func setKey() { askKey("openai", "OpenAI API key", "the OpenAI Whisper engine and GPT-Live voice mode") }
    func askKey(_ account: String, _ title: String, _ use: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = "Stored in your login Keychain. Used only for \(use). Leave empty to remove it\(account == "openai" ? "; OPENAI_API_KEY is used then, if it’s set" : "")."
        let f = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        a.accessoryView = f
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = f
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        let v = f.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if keychain(account, set: v) == nil {
            let action = v.isEmpty ? "remove" : "save"
            greenlight.msgs.append(Msg(.note, "Couldn't \(action) the \(title) in your Keychain. Try again, or allow Greenlight if the Keychain asks."))
            if account == "openai" { forgetEnvironmentOpenAIKey() }
        } else if account == "openai", v.isEmpty {
            forgetEnvironmentOpenAIKey { hasEnv in
                if hasEnv { LivePreview.shared.keyChanged(account, to: "", hasFallback: true) }
            }
        } else {
            LivePreview.shared.keyChanged(account, to: v)
            if account == "openai" { forgetEnvironmentOpenAIKey() }
        }
        if account == "elevenlabs" { Speech.shared.warm(); greenlight.objectWillChange.send() }
    }

    /// The next key press with a modifier, while the window is open, becomes the voice shortcut. Esc cancels.
    var capture: Any?
    var vimMonitor: Any?, orbMonitor: Any?, floorMonitor: Any?
    @objc func setShortcut() {
        show()
        recordShortcut()
        greenlight.hint = Core.keyPrompt  // after keying is on: showTab centres it on the window
    }
    /// Settings' Change…: the recorder shows in place of the keys there, not in the chat's hint.
    func recordShortcut() {
        guard capture == nil else { return }
        greenlight.keying = true
        capture = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard let self else { return e }
            let f = e.modifierFlags
            let mods = (f.contains(.control) ? controlKey : 0) | (f.contains(.option) ? optionKey : 0)
                | (f.contains(.shift) ? shiftKey : 0) | (f.contains(.command) ? cmdKey : 0)
            if e.keyCode != kVK_Escape && mods & (controlKey | optionKey | cmdKey) == 0 { return nil }  // keep waiting
            if e.keyCode != kVK_Escape {
                greenlight.voiceMods = mods
                greenlight.voiceCode = Int(e.keyCode)
                greenlight.voiceKey = (e.charactersIgnoringModifiers ?? "?").uppercased()
                registerVoice()
            }
            greenlight.hint = ""
            greenlight.keying = false
            NSEvent.removeMonitor(capture!)
            capture = nil
            return nil
        }
    }

    /// ⌃⌥M toggles the window from anywhere; the voice shortcut (⌃⌥V by default) starts/stops dictation.
    /// Carbon hot keys need no Accessibility permission.
    func hotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, ev, _ in
            var id = EventHotKeyID()
            GetEventParameter(ev, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &id)
            DispatchQueue.main.async {
                if id.id == 2 {
                    if !delegate.window.isVisible { delegate.show() }
                    delegate.greenlight.toggleVoice()
                } else {
                    delegate.toggle()
                }
            }
            return noErr
        }, 1, &spec, nil, nil)
        var ref: EventHotKeyRef?
        RegisterEventHotKey(UInt32(kVK_ANSI_M), UInt32(controlKey | optionKey), EventHotKeyID(signature: 0x4D434846, id: 1),
                            GetApplicationEventTarget(), 0, &ref)
        registerVoice()
    }

    var voiceRef: EventHotKeyRef?
    func registerVoice() {
        if let r = voiceRef { UnregisterEventHotKey(r) }
        RegisterEventHotKey(UInt32(greenlight.voiceCode), UInt32(greenlight.voiceMods), EventHotKeyID(signature: 0x4D434846, id: 2),
                            GetApplicationEventTarget(), 0, &voiceRef)
    }

    /// Opening the app again (Dock, Spotlight, Finder, `open`) shows the window.
    func applicationWillTerminate(_ n: Notification) {
        dropShare()  // a guest's autosaved frame: the floors' own, not the screen's
        Core.quitting = true  // the chats' ssh processes end below; their turns go on and are followed at the next launch
        tunnels.values.forEach { $0.terminate() }
        dashProc?.terminate(); dashProc = nil
        localDashProc?.terminate(); localDashProc = nil
        greenlight.outboxProc?.terminate(); greenlight.outboxProc = nil
        greenlight.proc?.terminate(); greenlight.proc = nil
        treesProc?.terminate(); treesProc = nil
        localTreesProc?.terminate(); localTreesProc = nil
        l1s.values.forEach { $0.c.proc?.terminate(); if !$0.c.voice.idle { $0.c.voice.stopAll() } }
        Speech.shared.stop()
        uitestServer?.stop()
        if let suite = uitestSuiteName, uitestSuiteKept == nil { dropDomain(suite) }  // a kept one is the test's to remove
        try? FileManager.default.removeItem(atPath: Diag.marker)  // a clean quit: no prev_crash next launch
    }  // port tunnels die with the app
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { upstairs ? show() : basement(); return false }

    /// Greenlight Center, straight up (a page link, Settings, a notification): the Basement steps aside, no ride.
    /// `from`: the window you switched up from, whose frame Center takes (default: the Basement it replaces).
    func show(from: NSWindow? = nil) {
        hand(window, from: from ?? (!upstairs && termWindow.isVisible ? termWindow : nil))
        if !upstairs, termWindow.isVisible { stepAside(termWindow); hideFloaters() }
        upstairs = true
        greenlight.unread = 0
        tick()
        revealOnPaint(window, fluid)
        presentWindow(window)
        placeChat()
    }

    /// The star and ⌃⌥M: hide the window when it's in front, else bring it up.
    func toggle() {
        if NSApp.isActive && (upstairs ? window.isKeyWindow || chatWindow.isKeyWindow : termWindow.isKeyWindow) { hideLevel(orbHost) }
        else if upstairs { show() } else { basement() }
    }

    /// Hover hint as a tooltip by the cursor, at once (the native one waits ~1 s, and only while Greenlight is the front app).
    lazy var tab: NSPanel = {
        let w = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = true
        w.ignoresMouseEvents = true
        w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        w.level = .popUpMenu  // over Settings and the chat's own window, like a tooltip
        w.hidesOnDeactivate = false  // a panel hides with the app by default: hovering Greenlight from another app still hints
        return w
    }()
    var hintSink: AnyCancellable?

    /// Each window's chat has its own hover hint (its highlights read it); the cursor shows the one that spoke last. A clear counts
    /// only from that one (a late hover-exit elsewhere doesn't drop a fresh hint), and moving on drops the last one's, so its
    /// highlight goes too. While the menu's voice-shortcut prompt is up, only it shows.
    weak var hintCore: Core?
    func hint(_ text: String, from c: Core) {
        if greenlight.prompting && c !== greenlight { return }
        if text.isEmpty { if hintCore === c || hintCore == nil { showTab("") }; return }  // nil: that window has closed
        if let o = hintCore, o !== c, !o.hint.isEmpty { o.hint = "" }
        hintCore = c; showTab(text)
    }
    func showTab(_ text: String) {
        guard !text.isEmpty else { tab.orderOut(nil); return }
        let host = NSHostingView(rootView: HintTab(text: text)), size = hintSize(text)
        host.sizingOptions = []  // the frame is hintSize's (wrapped), not the view's one-line ideal
        // by the cursor; the voice-shortcut prompt (from the menu bar's menu) in the middle of the window it brought up
        let m = greenlight.prompting && window.isVisible ? NSPoint(x: window.frame.midX - size.width / 2 - 10, y: window.frame.midY) : NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(m, $0.frame, false) } ?? NSScreen.main
        tab.contentView = host
        tab.setFrame(hintFrame(size, mouse: m, screen: screen?.visibleFrame ?? .infinite), display: true)
        tab.orderFront(nil)
    }
    /// A hint whose control the cursor can't be on any more: the topmost window under the cursor isn't Greenlight's (it left,
    /// or another app or Space came to the front mid-hover). Never the voice-shortcut prompt: tick hides it while Greenlight's away.
    var hintStale: Bool {
        guard tab.isVisible, !greenlight.prompting else { return false }
        let p = NSEvent.mouseLocation
        var n = NSWindow.windowNumber(at: p, belowWindowWithWindowNumber: 0)
        if n == tab.windowNumber { n = NSWindow.windowNumber(at: p, belowWindowWithWindowNumber: n) }  // the cursor slid onto the hint: judge the window under it
        return !NSApp.windows.contains { $0 !== tab && $0.isVisible && $0.windowNumber == n }
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        DispatchQueue.main.async {
            if let t = r as? UNTextInputNotificationResponse, !t.userText.isEmpty {
                let info = r.notification.request.content.userInfo
                let qid = (info["questionID"] as? String).flatMap { UUID(uuidString: $0) }
                self.greenlight.reply(to: info["text"] as? String ?? "", t.userText, questionID: qid,
                                      freeform: info["freeform"] as? Bool ?? false)
            } else {
                self.greenlight.inboxOpen = !self.greenlight.waiting.isEmpty
                self.show()
            }
        }
        done()
    }
}

