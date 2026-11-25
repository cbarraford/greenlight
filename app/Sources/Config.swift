// Greenlight: menubar chat with greenlight (on this Mac or an ssh host), live activity while it works, replyable notifications.
// Chat: `chat.py send|compact|new|stop` streams JSON events (see chat.py's Norm).
// Pings: greenlight appends JSON lines to <greenlightHome>/outbox.jsonl (`greenlight.py ping`); we tail it.
// Where greenlight runs: Settings › Hosts (or `defaults write <bundle id> <key> <value>`), read at launch:
//   host       "local" (default) runs greenlight's Python here; anything else is an ssh host (GREENLIGHT_HOST overrides)
//   greenlightHome  greenlight's state dir on that host (default ~/.greenlight)
//   greenlightCode  where greenlight's *.py live there (default ~/.claude/skills/greenlight)
// A dev copy (greenlight-<workspace>, scripts/dev.py) is this app under another name with settings of its own: see devDir.
// No @State/@Observable: those are macros, and the Command Line Tools swiftc has no SwiftUI macro plugin.
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

let localDefault: String = { var b = [CChar](repeating: 0, count: 256); gethostname(&b, 255); return String(cString: b).components(separatedBy: ".")[0] }()  // "Local" in the Basement, until you rename it
let uitest = CommandLine.arguments.contains("--uitest")
let unittest = CommandLine.arguments.contains("--unittest")
/// --uitest: its own defaults, per process; GREENLIGHT_UITEST_SUITE (a dev.greenlight.uitest.* name) keeps them across a relaunch.
let uitestSuiteKept: String? = ProcessInfo.processInfo.environment["GREENLIGHT_UITEST_SUITE"].flatMap {
    uitest && $0.range(of: #"^dev\.greenlight\.uitest\.[\w-]{1,64}$"#, options: .regularExpression) != nil ? $0 : nil
}
let uitestSuiteName: String? = (uitest || unittest) ? uitestSuiteKept ?? "dev.greenlight.\(unittest ? "unittest" : "uitest").\(ProcessInfo.processInfo.processIdentifier)" : nil
/// Test defaults a killed or `exit()`ed run left behind: a dev.greenlight.(uitest|unittest).<pid> whose process is gone, or a
/// uitest.py cached binary's AppKit autosave domain (Greenlight-<key>), or a kept suite (…uitest.s<hex>) a killed session never deleted, untouched for a day.
func staleTestDomains(_ files: [(name: String, modified: Date)], alive: (Int32) -> Bool, now: Date = Date()) -> [String] {
    files.compactMap { f in
        guard f.name.hasSuffix(".plist") else { return nil }
        let d = String(f.name.dropLast(6))
        if d.range(of: #"^dev\.greenlight\.(uitest|unittest)\.\d+$"#, options: .regularExpression) != nil, let pid = Int32(d.split(separator: ".").last ?? "") {
            return alive(pid) ? nil : d
        }
        return d.range(of: #"^(Greenlight-[0-9a-f]{16}|dev\.greenlight\.uitest\.s[0-9a-f]{8})$"#, options: .regularExpression) != nil && now.timeIntervalSince(f.modified) > 86400 ? d : nil
    }
}

/// A test launch clears what earlier test launches leaked (the app's own removal only runs on a clean quit).
func sweepTestDomains() {
    let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Preferences")
    let urls = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
    let files = urls.map { (name: $0.lastPathComponent, modified: (try? $0.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? Date()) }
    for d in staleTestDomains(files, alive: { kill($0, 0) == 0 || errno == EPERM }) { dropDomain(d) }
}

/// removePersistentDomain empties a domain but leaves its plist on disk (an empty dict, one file per test run): remove both.
func dropDomain(_ d: String) {
    UserDefaults.standard.removePersistentDomain(forName: d)
    try? FileManager.default.removeItem(at: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Preferences/\(d).plist"))
}
let uitestSocketPath: String? = {
    if let i = CommandLine.arguments.firstIndex(of: "--uitest-socket"), i + 1 < CommandLine.arguments.count {
        return CommandLine.arguments[i + 1]
    }
    return ProcessInfo.processInfo.environment["GREENLIGHT_UITEST_SOCKET"]
}()
let ud: UserDefaults = uitestSuiteName.flatMap { UserDefaults(suiteName: $0) } ?? UserDefaults.standard
let host = ProcessInfo.processInfo.environment["GREENLIGHT_HOST"] ?? ud.string(forKey: "host") ?? "local"
let greenlightHome = ProcessInfo.processInfo.environment["GREENLIGHT_HOME"] ?? ud.string(forKey: "greenlightHome") ?? "~/.greenlight"
let greenlightCode = ProcessInfo.processInfo.environment["GREENLIGHT_CODE"] ?? ud.string(forKey: "greenlightCode") ?? "~/.claude/skills/greenlight"
let tmuxSocketName = ProcessInfo.processInfo.environment["GREENLIGHT_TMUX_SOCKET"] ?? (uitest ? "gl-uitest-\(ProcessInfo.processInfo.processIdentifier)" : "greenlight")
/// Quotes a filesystem path for a POSIX shell while preserving leading `~` expansion on local or remote hosts.
func shellPath(_ p: String) -> String {
    if p == "~" { return "\"$HOME\"" }
    if p.hasPrefix("~/") { return "\"$HOME\"/" + shellQuote(String(p.dropFirst(2))) }
    return shellQuote(p)
}

let py = "GREENLIGHT_HOME=\(shellPath(greenlightHome)) GREENLIGHT_TMUX_SOCKET=\(shellQuote(tmuxSocketName)) python3 \(shellPath(greenlightCode))/scripts"  // + "/greenlight.py ..."
let selftest = CommandLine.arguments.contains("--selftest") || CommandLine.arguments.contains("--attachtest") || unittest  // scratch chat session + transcript, see selfTest()
let chatCmd = (selftest ? "CHAT_DIR=\(shellPath(greenlightHome))/chat-test " : "") + "\(py)/chat.py"

/// Per-repository host configuration (`host=server|mac|auto`, default `server`) and its resolved target.
struct RepoHostInfo: Equatable {
    let mode: String       // "server" | "mac" | "auto"
    let target: String     // "server" | "mac"
    let needsMac: Bool
    let reason: String
}

func isLocalHost(_ h: String) -> Bool { h.isEmpty || h == "local" }
func hostDisplayName(_ h: String) -> String { isLocalHost(h) ? "this Mac" : h }
func hostBadgeName(_ h: String, defaultHost: String = host) -> String {
    isLocalHost(h) ? (isLocalHost(defaultHost) ? (h.isEmpty ? "local" : h) : "Mac") : h
}

/// Home and code paths for a target host. Tests stay hermetic on greenlightHome/greenlightCode; a remote server's
/// absolute Linux checkout path isn't used on this Mac when a project runs locally.
func hostPaths(for targetHost: String, defaultHost: String = host, home: String = greenlightHome, code: String = greenlightCode) -> (home: String, code: String) {
    if uitest || unittest || !isLocalHost(targetHost) || isLocalHost(defaultHost) { return (home, code) }
    let localHome = ud.string(forKey: "localGreenlightHome") ?? "~/.greenlight"
    let configured = ud.string(forKey: "localGreenlightCode") ?? ""
    if !configured.isEmpty { return (localHome, configured) }
    if FileManager.default.fileExists(atPath: (code as NSString).expandingTildeInPath + "/scripts/chat.py") { return (localHome, code) }
    return (localHome, localSkill)  // the server's path isn't on this Mac: the skill's home, which localBackendPrefix fills
}

/// Where greenlight's scripts live on this Mac when the server holds the main checkout.
let localSkill = "~/.claude/skills/greenlight"
/// Exit code of a Mac-side command whose scripts couldn't be copied from the server (Core.lost explains it).
let localBackendMissing: Int32 = 97

/// A shell prefix for a command on this Mac while the default host is a server: when the skill isn't on this Mac yet (a project
/// that needs the Mac runs its Chief and agents here, #169), copy it from the server's checkout first, once. Without it
/// `python3 …/chat.py` exits 2 and the chat reads "Lost the connection to this Mac (exit 2)". Several commands can start
/// together: one takes the lock and copies, the rest wait for it. "" when there is nothing to do.
func localBackendPrefix(server: String = host, serverCode: String = greenlightCode) -> String {
    guard !isLocalHost(server), !uitest, !unittest, !selftest, hostPaths(for: "local", defaultHost: server, code: serverCode).code == localSkill,
          !FileManager.default.fileExists(atPath: (localSkill as NSString).expandingTildeInPath + "/scripts/chat.py") else { return "" }
    let pack = "tar -C \(shellPath(serverCode)) -czf - --exclude=.git --exclude=app --exclude=memory --exclude=.venv --exclude=node_modules --exclude=__pycache__ ."
    let ssh = (["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"] + devSsh(server) + ["--", server, pack]).map(shellQuote).joined(separator: " ")
    return """
    gl_d="$HOME/.claude/skills/greenlight"; if [ ! -f "$gl_d/scripts/chat.py" ]; then \
    mkdir -p "$HOME/.claude/skills"; find "$gl_d.installing" -maxdepth 0 -mmin +5 -exec rmdir {} \\; 2>/dev/null; \
    if mkdir "$gl_d.installing" 2>/dev/null; then \
    gl_t=$(mktemp -d "$gl_d.XXXXXX") && \(ssh) | tar -xzf - -C "$gl_t" && [ -f "$gl_t/scripts/chat.py" ] && \
    { rmdir "$gl_d" 2>/dev/null; [ -e "$gl_d" ] || mv "$gl_t" "$gl_d"; }; rm -rf "$gl_t"; rmdir "$gl_d.installing"; \
    else gl_n=0; while [ -d "$gl_d.installing" ] && [ $gl_n -lt 120 ]; do sleep 1; gl_n=$((gl_n+1)); done; fi; \
    [ -f "$gl_d/scripts/chat.py" ] || { echo "greenlight: couldn't copy greenlight from \(server) to $gl_d" >&2; exit \(localBackendMissing); }; fi;\u{20}
    """
}

func pyCmd(on targetHost: String = host) -> String {
    let p = hostPaths(for: targetHost)
    return "GREENLIGHT_HOME=\(shellPath(p.home)) GREENLIGHT_TMUX_SOCKET=\(shellQuote(tmuxSocketName)) python3 \(shellPath(p.code))/scripts"
}

func chatCmdFor(session: Int, on targetHost: String = host) -> String {
    let p = hostPaths(for: targetHost), base = "\(pyCmd(on: targetHost))/chat.py"
    if session > 0 { return "CHAT_DIR=\(shellPath(p.home))/chat-\(session) \(base)" }
    return (selftest ? "CHAT_DIR=\(shellPath(p.home))/chat-test " : "") + base
}

func parseRepoHosts(_ raw: Any?) -> [String: RepoHostInfo] {
    guard let dict = raw as? [String: [String: Any]] else { return [:] }
    var out = [String: RepoHostInfo]()
    for (k, v) in dict {
        let m = (v["mode"] as? String ?? "server").lowercased()
        let mode = ["server", "mac", "auto"].contains(m) ? m : "server"
        let needs = v["needs_mac"] as? Bool ?? false
        let t = (v["target"] as? String ?? "").lowercased()
        let target = ["server", "mac"].contains(t) ? t : (mode == "mac" || (mode == "auto" && needs) ? "mac" : "server")
        out[k] = RepoHostInfo(mode: mode, target: target, needsMac: needs, reason: v["reason"] as? String ?? "")
    }
    return out
}

func lookupRepoHost(_ repo: String, in map: [String: RepoHostInfo]) -> RepoHostInfo? {
    if let info = map[repo] { return info }
    let base = String(repo.split(separator: "/").last ?? "")
    return base.isEmpty ? nil : map[base]
}

func resolveRepoTargetHost(_ repo: String, repoHosts: [String: RepoHostInfo], defaultHost: String = host) -> String {
    if isLocalHost(defaultHost) { return "local" }
    return lookupRepoHost(repo, in: repoHosts)?.target == "mac" ? "local" : defaultHost
}

private func combineHostInfo(_ existing: RepoHostInfo?, _ incoming: RepoHostInfo) -> RepoHostInfo {
    let needs = (existing?.needsMac ?? false) || incoming.needsMac
    let reason = !incoming.reason.isEmpty ? incoming.reason : (existing?.reason ?? "")
    let target = (incoming.mode == "mac" || (incoming.mode == "auto" && needs)) ? "mac" : "server"
    return RepoHostInfo(mode: incoming.mode, target: target, needsMac: needs, reason: reason)
}

/// Deterministically merges `incoming` repo host info into `base`: preserves local `needsMac` detection, applies short
/// names first, then qualified `org/repo` entries without letting a default `"server"` entry clobber an explicit mode.
func mergeRepoHostMaps(_ base: [String: RepoHostInfo], _ incoming: [String: RepoHostInfo]) -> [String: RepoHostInfo] {
    var out = base
    let shorts = incoming.keys.filter { !$0.contains("/") }.sorted()
    let qualified = incoming.keys.filter { $0.contains("/") }.sorted()
    for k in shorts {
        guard let v = incoming[k] else { continue }
        out[k] = combineHostInfo(out[k], v)
    }
    for k in qualified {
        guard let v = incoming[k] else { continue }
        let merged = combineHostInfo(out[k], v)
        out[k] = merged
        let b = String(k.split(separator: "/").last ?? "")
        if !b.isEmpty {
            if let cur = out[b] {
                if cur.mode == "server" && merged.mode != "server" {
                    out[b] = combineHostInfo(cur, merged)
                } else if merged.needsMac && !cur.needsMac {
                    let target = (cur.mode == "mac" || cur.mode == "auto") ? "mac" : "server"
                    out[b] = RepoHostInfo(mode: cur.mode, target: target, needsMac: true, reason: !cur.reason.isEmpty ? cur.reason : merged.reason)
                }
            } else {
                out[b] = merged
            }
        }
    }
    return out
}

/// Applies an explicit user host edit for `repo` (including downgrades back to `"server"`), updating `repo`, its bare
/// alias, and any matching qualified `org/<base>` entries so stale entries can't pin the old host.
func setRepoHostInMap(_ base: [String: RepoHostInfo], repo: String, info: RepoHostInfo) -> [String: RepoHostInfo] {
    var out = base
    let b = String(repo.split(separator: "/").last ?? "")
    out[repo] = combineHostInfo(out[repo], info)
    if !b.isEmpty {
        out[b] = combineHostInfo(out[b], info)
        for k in out.keys where k.hasSuffix("/" + b) {
            out[k] = combineHostInfo(out[k], info)
        }
    }
    return out
}

/// Resolves which host Chief runs on for a project selection (`only`: nil = All projects -> server default).
/// A project targets the Mac when any of its repos (from `projList` or `savedProjects`) targets "mac";
/// when every selected project targets the Mac, Chief runs locally.
func resolveProjectHost(only: [String]?, projList: [[String: Any]], savedProjects: [[String: Any]] = [], repoHosts: [String: RepoHostInfo], defaultHost: String = host) -> String {
    if isLocalHost(defaultHost) { return "local" }
    guard let only, !only.isEmpty else { return defaultHost }
    let selected: [[String]] = only.compactMap { id in
        let liveRepos = (projList.first { $0["id"] as? String == id }?["repos"] as? [String]) ?? []
        let savedRepos = (savedProjects.first { $0["id"] as? String == id }?["repos"] as? [String]) ?? []
        let union = Array(Set(liveRepos + savedRepos))
        if !union.isEmpty { return union }
        return projList.contains { $0["id"] as? String == id } || savedProjects.contains { $0["id"] as? String == id } ? [] : nil
    }
    guard !selected.isEmpty else { return defaultHost }
    let allMac = selected.allSatisfy { rs in
        !rs.isEmpty && rs.contains { lookupRepoHost($0, in: repoHosts)?.target == "mac" }
    }
    return allMac ? "local" : defaultHost
}

func switchProjectNote(project: String, host h: String) -> String {
    let whereText = hostDisplayName(h)
    return project.isEmpty ? "Switched to \(whereText). New conversation." : "Switched to \(project) on \(whereText). New conversation."
}

/// Merges dashboard snapshots from server and local Mac hosts, preferring each host's native worktrees so paired
/// Orca runtimes never let one host hijack the other's `_runHost` or overwrite its scan data.
func mergeSnapshots(_ primary: [String: Any]?, _ secondary: [String: Any]?, primaryHost: String, secondaryHost: String) -> [String: Any]? {
    guard primary != nil || secondary != nil else { return nil }
    let tag: ([[String: Any]], String) -> [[String: Any]] = { items, h in items.map { var m = $0; m["_runHost"] = h; return m } }
    let isNative: ([String: Any]) -> Bool = { $0["nativeHost"] as? Bool != false }
    func dedupe(_ a: [[String: Any]], _ b: [[String: Any]]) -> [[String: Any]] {
        let ordered = a.filter(isNative) + b.filter(isNative) + a.filter { !isNative($0) } + b.filter { !isNative($0) }
        var seen = Set<String>(), out = [[String: Any]]()
        for item in ordered {
            let k = (item["path"] as? String) ?? (item["name"] as? String) ?? UUID().uuidString
            if seen.insert(k).inserted { out.append(item) }
        }
        return out
    }
    let pucks = dedupe(tag(primary?["pucks"] as? [[String: Any]] ?? [], primaryHost),
                       tag(secondary?["pucks"] as? [[String: Any]] ?? [], secondaryHost))
    let puckPaths = Set(pucks.compactMap { $0["path"] as? String })
    let trees = dedupe(tag(primary?["trees"] as? [[String: Any]] ?? [], primaryHost),
                       tag(secondary?["trees"] as? [[String: Any]] ?? [], secondaryHost))
        .filter { ($0["path"] as? String).map { !puckPaths.contains($0) } ?? true }
    var out: [String: Any] = ["ts": max(primary?["ts"] as? Int ?? 0, secondary?["ts"] as? Int ?? 0),
                              "scanned": max(primary?["scanned"] as? Int ?? 0, secondary?["scanned"] as? Int ?? 0),
                              "pucks": pucks, "trees": trees]
    let pErr = primary?["scan_error"] as? [String: Any], sErr = secondary?["scan_error"] as? [String: Any]
    if let pErr, let sErr {
        out["scan_error"] = (sErr["ts"] as? Int ?? 0) > (pErr["ts"] as? Int ?? 0) ? sErr : pErr
    } else if let err = pErr ?? sErr {
        out["scan_error"] = err
    }
    out["usage"] = usageOf(primary, secondary)
    return out
}

/// The footer's plan meters (dash.py `usage`) from a merged pair, one per agent: greenlight's host's reading of an agent wins
/// unless it failed while the Mac has a healthy (or windowed) reading; agents only the Mac reports are added after them. nil when neither host sent any.
func usageOf(_ primary: [String: Any]?, _ secondary: [String: Any]?) -> [[String: Any]]? {
    let p = (primary?["usage"] as? [Any])?.compactMap { $0 as? [String: Any] }
    let s = (secondary?["usage"] as? [Any])?.compactMap { $0 as? [String: Any] }
    guard p != nil || s != nil else { return nil }
    let hasWin: ([String: Any]) -> Bool = { !(($0["windows"] as? [Any])?.isEmpty ?? true) }
    let better: ([String: Any], [String: Any]) -> Bool = { a, b in
        let sa = a["state"] as? String, sb = b["state"] as? String
        return (sa == "failed" && sb == "ok") || (sa == "failed" && sb == "failed" && !hasWin(a) && hasWin(b))
    }
    var sById = [String: [String: Any]]()
    for item in s ?? [] { if let id = item["id"] as? String, sById[id] == nil { sById[id] = item } }
    var seen = Set<String>(), out = [[String: Any]]()
    for item in p ?? [] {
        guard let id = item["id"] as? String, seen.insert(id).inserted else { continue }
        out.append(sById[id].map { better(item, $0) ? $0 : item } ?? item)
    }
    for item in s ?? [] {
        guard let id = item["id"] as? String, seen.insert(id).inserted else { continue }
        out.append(item)
    }
    return out
}

/// Merges `dash.py trees` payloads from server and local Mac, deduplicating by path (native host wins) and combining `repoHosts`.
func mergeTreesPayload(_ primary: [String: Any]?, _ secondary: [String: Any]?, primaryHost: String, secondaryHost: String) -> [String: Any]? {
    guard primary != nil || secondary != nil else { return nil }
    let tag: ([[String: Any]], String) -> [[String: Any]] = { items, h in items.map { var m = $0; m["_runHost"] = h; return m } }
    let isNative: ([String: Any]) -> Bool = { $0["nativeHost"] as? Bool != false }
    let pTrees = tag(primary?["trees"] as? [[String: Any]] ?? [], primaryHost)
    let sTrees = tag(secondary?["trees"] as? [[String: Any]] ?? [], secondaryHost)
    let ordered = pTrees.filter(isNative) + sTrees.filter(isNative) + pTrees.filter { !isNative($0) } + sTrees.filter { !isNative($0) }
    var seen = Set<String>(), merged = [[String: Any]]()
    for t in ordered {
        let k = (t["path"] as? String) ?? UUID().uuidString
        if seen.insert(k).inserted { merged.append(t) }
    }
    var hosts = (primary?["repoHosts"] as? [String: [String: Any]]) ?? [:]
    for (k, v) in (secondary?["repoHosts"] as? [String: [String: Any]]) ?? [:] {
        if let ex = hosts[k] {
            let mode = ex["mode"] as? String ?? v["mode"] as? String ?? "server"
            let needs = (ex["needs_mac"] as? Bool ?? false) || (v["needs_mac"] as? Bool ?? false)
            let reason = !(ex["reason"] as? String ?? "").isEmpty ? (ex["reason"] as? String ?? "") : (v["reason"] as? String ?? "")
            let target = (mode == "mac" || (mode == "auto" && needs)) ? "mac" : "server"
            hosts[k] = ["mode": mode, "target": target, "needs_mac": needs, "reason": reason]
        } else {
            hosts[k] = v
        }
    }
    var panes = (primary?["panes"] as? [String: Any]) ?? [:]
    for (k, v) in (secondary?["panes"] as? [String: Any]) ?? [:] { panes[k] = v }
    var out: [String: Any] = ["trees": merged, "panes": panes, "repoHosts": hosts]
    out["usage"] = usageOf(primary, secondary)
    let scan = primary?["scanned"] != nil ? primary : secondary  // the Basement footer's scan facts, all from one host: greenlight's, else the Mac's
    for k in ["ts", "scanned", "scan_error"] { out[k] = scan?[k] }
    return out
}
/// The trees payload to publish after one host's `dash.py trees` answered: both hosts' worktrees (merged when greenlight runs on
/// a server), but each host's pane states only once, from the fetch that read them. Each fetch publishes on its own, so without
/// this the other host's states from the last refresh ride along: a stale "working" after a turn's ping, then the fresh "done",
/// pinged the turn again (terminal.html syncPanes). Spent states are dropped from `server`/`local`.
func treesToPublish(server: inout [String: Any]?, local: inout [String: Any]?, merge: Bool, primaryHost: String) -> [String: Any]? {
    let obj = merge && local != nil ? mergeTreesPayload(server, local, primaryHost: primaryHost, secondaryHost: "local") : server ?? local
    server?["panes"] = nil; local?["panes"] = nil
    return obj
}
/// Pane states for a terminal page that just loads (`ready` replays the newest trees payload): every host's states from the current
/// refresh, and only while fresh (5 s since the first of them arrived). An older "working" replayed into a reloaded page would flip an idle pane
/// back to work, and the next fetch's "done" would ping its finished turn again. A refresh that starts drops what the last one saved.
struct PaneReplay {
    private(set) var panes = [String: Any](), at = Date.distantPast
    mutating func start() { panes = [:] }
    mutating func add(_ p: Any?, now: Date = Date()) {  // dated by the refresh's first states: the oldest that a replay carries
        guard let p = p as? [String: Any], !p.isEmpty else { return }
        if panes.isEmpty { at = now }
        panes.merge(p) { _, new in new }
    }
    func replay(_ obj: [String: Any]?, now: Date = Date()) -> [String: Any]? {
        guard var o = obj else { return nil }
        o["panes"] = !panes.isEmpty && now.timeIntervalSince(at) <= 5 ? panes : nil
        return o
    }
}
let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String ?? "Greenlight"  // "greenlight-<workspace>" in a copy
/// A dev copy's folder on this Mac (Info.plist's GreenlightDev, from build.sh), nil in Greenlight itself. A copy loads its pages
/// from there and reloads one when it changes, sends its ssh through dev.py's tunnel, and posts no banners (Greenlight does).
let devDir = Bundle.main.object(forInfoDictionaryKey: "GreenlightDev") as? String
/// A dev copy's workspace ("faster-qa"): on its menubar item and in its window bar, so you can tell the copies and Greenlight apart.
let devName = devDir.map { URL(fileURLWithPath: $0).lastPathComponent }
let devTagWidth: CGFloat = devName.map { min(190, ceil(($0 as NSString).size(withAttributes: [.font: NSFont.monospacedSystemFont(ofSize: 9.5, weight: .bold)]).width) + 44) } ?? 0  // the window bar's tag: 0 in Greenlight
/// A dev copy's extra ssh options to a host dev.py has a tunnel to: the tunnel's socket, so nothing connects in the copy's
/// name and a firewall (Little Snitch) has no new app to ask about, however many copies there are. Empty otherwise.
func devSsh(_ h: String) -> [String] {
    guard let devDir, !h.isEmpty, !h.contains("/"), !h.contains(".."), !h.hasPrefix("-") else { return [] }
    let sock = URL(fileURLWithPath: devDir).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("ssh/\(h).sock").path
    return FileManager.default.fileExists(atPath: sock) ? ["-o", "ProxyCommand=/usr/bin/nc -U \(shellQuote(sock))"] : []
}
/// A bundled page (dashboard, terminal, stage): in a dev copy or --uitest, the workspace's live one.
func page(_ name: String) -> URL? {
    if let webDir = ProcessInfo.processInfo.environment["GREENLIGHT_WEB_DIR"], FileManager.default.fileExists(atPath: "\(webDir)/\(name).html") {
        return URL(fileURLWithPath: "\(webDir)/\(name).html")
    }
    if let devDir, FileManager.default.fileExists(atPath: "\(devDir)/web/\(name).html") { return URL(fileURLWithPath: "\(devDir)/web/\(name).html") }
    return Bundle.main.url(forResource: name, withExtension: "html")
}

/// Validates a backend stdout string as a single JSON object (re-serialized cleanly), or serializes `fallback`.
func safeJSON(_ out: String, fallback: [String: Any] = [:]) -> String {
    if out.hasPrefix("{"), let d = out.data(using: .utf8),
       let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
       JSONSerialization.isValidJSONObject(obj),
       let clean = try? JSONSerialization.data(withJSONObject: obj),
       let s = String(data: clean, encoding: .utf8) { return s }
    if JSONSerialization.isValidJSONObject(fallback),
       let d = try? JSONSerialization.data(withJSONObject: fallback),
       let s = String(data: d, encoding: .utf8) { return s }
    return "{}"
}

extension WKWebView {
    /// Safely invokes a JS function path (e.g. "window.mc && mc.openPage") with JSON-serialized arguments.
    func callJS(_ fn: String, _ args: [Any] = [], done: ((Any?, Error?) -> Void)? = nil) {
        guard JSONSerialization.isValidJSONObject(args),
              let data = try? JSONSerialization.data(withJSONObject: args),
              let json = String(data: data, encoding: .utf8) else { return }
        evaluateJavaScript("(() => { const f = \(fn); return typeof f === 'function' ? f(...\(json)) : undefined; })()", completionHandler: done)
    }
}

/// The process that runs a command where greenlight lives: a login shell here (so PATH has glab/gh/orca), or ssh.
/// Under --uitest/--selftest/--attachtest on local host, use /bin/sh -c so login profile path_helper cannot reorder stub or Homebrew binaries on PATH.
func remoteArgv(_ remote: String, on host: String = host) -> (String, [String]) {
    if host.isEmpty || host == "local" {
        if uitest || selftest { return ("/bin/sh", ["-c", remote]) }
        return (ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh", ["-lc", localBackendPrefix() + remote])
    }
    return ("/usr/bin/ssh", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30"] + devSsh(host) + ["--", host, remote])
}

/// The terminal sidebar's Hosts: ~/.ssh/config's Host names, minus wildcards, git hosts and the -ts (Tailscale) twin of a listed host.
func sshHosts() -> [String] {
    let cfg = (try? String(contentsOfFile: NSHomeDirectory() + "/.ssh/config", encoding: .utf8)) ?? ""  // ponytail: Include files aren't followed
    var names = [String]()
    for l in cfg.split(separator: "\n") {
        let w = l.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)
        guard w.first?.lowercased() == "host" else { continue }
        for n in w.dropFirst() where !names.contains(n) && !n.hasPrefix("git") && n.range(of: #"^\w[\w.-]{0,63}$"#, options: .regularExpression) != nil { names.append(n) }
    }
    return names.filter { !($0.hasSuffix("-ts") && names.contains(String($0.dropLast(3)))) }
}
/// Whether a Basement tab's home runs greenlight's scripts (dash.py turn can read its pane there): a workspace or the home on
/// the scripts' host (also as @<host>, a tab saved before GREENLIGHT_HOST moved there), and @local (this Mac). Another @host has none. terminal.html's scriptsOn is the same rule.
func runsScripts(_ ws: String) -> Bool { !ws.hasPrefix("@") || ws == "@local" || ws == "@" + host }
/// A pane's host and folder: "@name" is a host's home (the Hosts rows), anything else a folder on GREENLIGHT_HOST.
func paneWhere(_ ws: String) -> (host: String, cwd: String)? {
    if ws.hasPrefix("@") { return ws.range(of: #"^@\w[\w.-]{0,63}$"#, options: .regularExpression) != nil ? (String(ws.dropFirst()), "") : nil }
    return ws.isEmpty || (ws.range(of: #"^/[\w./@+-]{1,300}$"#, options: .regularExpression) != nil && !ws.contains("..")) ? (host, ws) : nil
}

/// A color scheme: four base colors; the surfaces and text greys are mixed from bg and fg.
struct Theme {
    let name: String, bg, fg, accent, bubble: NSColor
    init(_ name: String, _ bg: String, _ fg: String, _ accent: String, _ bubble: String) {
        (self.name, self.bg, self.fg, self.accent, self.bubble) = (name, hex(bg), hex(fg), hex(accent), hex(bubble))
    }
    func blend(_ f: CGFloat) -> NSColor { bg.blended(withFraction: f, of: fg) ?? fg }
    func mix(_ f: CGFloat) -> Color { Color(nsColor: blend(f)) }
    var dark: Bool { (bg.usingColorSpace(.sRGB)?.brightnessComponent ?? 0) < 0.5 }
}

/// Small icons in a fixed frame (close, chevron, check), before iconSize().
let glyphSize: CGFloat = 9
/// Status colours (brand/DESIGN.md §2.4) in the app's appearance: one meaning each, never the accent. The solid fills are for
/// the states asking for you (needs, crashed); the rest use their text step. Queued, waiting and done are neutral.
enum StatusInk {
    static func c(_ t: DSColor) -> Color { Color(t, dark: isDark) }
    static var needs: Color { c(DS.Palette.statusNeedsSolid) }
    static var yourMove: Color { c(DS.Palette.statusYourMoveFg) }
    static var working: Color { c(DS.Palette.statusWorkingFg) }
    static var crashed: Color { c(DS.Palette.statusCrashedSolid) }
    static var queued: Color { c(DS.Palette.statusQueuedFg) }
    static var done: Color { c(DS.Palette.statusDoneFg) }
    static var needsInk: Color { c(DS.Palette.statusNeedsFg) }
    static var crashedInk: Color { c(DS.Palette.statusCrashedFg) }
    static var workingInk: Color { c(DS.Palette.statusWorkingFg) }
}

func hex(_ s: String) -> NSColor {
    let v = Int(s, radix: 16) ?? 0
    return NSColor(srgbRed: CGFloat(v >> 16 & 255) / 255, green: CGFloat(v >> 8 & 255) / 255, blue: CGFloat(v & 255) / 255, alpha: 1)
}

/// Palettes from popular terminal and editor themes.
let themes = [
    Theme("Greenlight", "1C1C1F", "ECECEE", "F5A524", "2563EB"),
    Theme("Dracula", "282A36", "F8F8F2", "BD93F9", "6272A4"),
    Theme("Nord", "2E3440", "ECEFF4", "88C0D0", "5E81AC"),
    Theme("Tokyo Night", "1A1B26", "C0CAF5", "7AA2F7", "3D59A1"),
    Theme("Catppuccin Mocha", "1E1E2E", "CDD6F4", "CBA6F7", "585B70"),
    Theme("Gruvbox Dark", "282828", "EBDBB2", "FE8019", "458588"),
    Theme("Solarized Dark", "002B36", "EEE8D5", "B58900", "268BD2"),
    Theme("One Dark", "282C34", "ABB2BF", "61AFEF", "7E4E9E"),
    Theme("Monokai", "272822", "F8F8F2", "A6E22E", "AE2B5E"),
    Theme("Rosé Pine", "191724", "E0DEF4", "EBBCBA", "31748F"),
    Theme("Kanagawa", "1F1F28", "DCD7BA", "E6C384", "2D4F67"),
    Theme("Everforest", "2D353B", "D3C6AA", "A7C080", "3A6B67"),
    Theme("GitHub Dark", "0D1117", "E6EDF3", "2F81F7", "1F6FEB"),
    Theme("Homebrew", "000000", "28FE14", "28FE14", "0A5C0A"),
    Theme("Solarized Light", "FDF6E3", "586E75", "B58900", "268BD2"),
    Theme("Catppuccin Latte", "EFF1F5", "4C4F69", "8839EF", "1E66F5"),
    Theme("GitHub Light", "FFFFFF", "1F2328", "0969DA", "0969DA"),
    Theme("One Light", "FAFAFA", "383A42", "4078F2", "4078F2"),
    Theme("Gruvbox Light", "FBF1C7", "3C3836", "AF3A03", "076678"),
    Theme("Rosé Pine Dawn", "FAF4ED", "575279", "D7827E", "286983"),
    Theme("Tokyo Night Day", "E1E2E7", "3760BF", "2E7DE9", "2E7DE9"),
]
/// The accents (brand/DESIGN.md §2.3): Signal (the default), Teal and Graphite. Each is the 12-step scale's semantic set.
struct AccentSet { let id, name: String; let solid, hover, fg, subtle, ring, onSolid: DSColor }
let accents = [
    AccentSet(id: "signal", name: "Signal", solid: DS.Palette.accentSolid, hover: DS.Palette.accentHover, fg: DS.Palette.accentFg,
              subtle: DS.Palette.accentSubtle, ring: DS.Palette.accentRing, onSolid: DS.Palette.accentOnSolid),
    AccentSet(id: "teal", name: "Teal", solid: DS.AccentTeal.solid, hover: DS.AccentTeal.hover, fg: DS.AccentTeal.fg,
              subtle: DS.AccentTeal.subtle, ring: DS.AccentTeal.ring, onSolid: DS.AccentTeal.onSolid),
    AccentSet(id: "graphite", name: "Graphite", solid: DS.AccentGraphite.solid, hover: DS.AccentGraphite.hover, fg: DS.AccentGraphite.fg,
              subtle: DS.AccentGraphite.subtle, ring: DS.AccentGraphite.ring, onSolid: DS.AccentGraphite.onSolid),
]
/// An accent from before the redesign (one of eight swatch hexes, or nil for the theme's own) to the nearest of the three
/// (§2.3): Cyan, Blue and Purple become Teal; Green, Amber, Orange, Rose, Pink and the theme's own become Signal.
func migratedAccent(_ oldHex: String?) -> String { ["2AC3DE", "7AA2F7", "BD93F9"].contains(oldHex?.uppercased() ?? "") ? "teal" : "signal" }
/// Fonts from terminal apps; only the installed ones are offered.
let fontChoices = ["System", "SF Mono", "Menlo", "Monaco", "JetBrains Mono", "Fira Code", "Cascadia Code", "Hack", "Source Code Pro",
                   "IBM Plex Mono", "Iosevka", "Berkeley Mono", "Geist Mono", "Inter", "Helvetica Neue", "Avenir Next"]
    .filter { ["System", "SF Mono"].contains($0) || NSFontManager.shared.availableFontFamilies.contains($0) }

func named(_ key: String, _ fallback: String) -> Theme { themes.first { $0.name == (ud.string(forKey: key) ?? fallback) } ?? themes[0] }
var darkTheme = named("darkTheme", "Greenlight")
var lightTheme = named("lightTheme", "GitHub Light")
/// Settings › Appearance › Appearance (§2.2): "auto" follows macOS (the default); "light" or "dark" overrides it for every
/// window, through NSApp.appearance. No window or floor forces its own.
var appearanceMode: String { ud.string(forKey: "appearance") ?? "auto" }
var appAppearance: NSAppearance? { appearanceMode == "auto" ? nil : NSAppearance(named: appearanceMode == "dark" ? .darkAqua : .aqua) }
var isDark: Bool { appearanceMode == "auto" ? NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua : appearanceMode == "dark" }
/// The terminal canvas's theme for the current appearance (§2.5: named themes style the terminal only).
var theme: Theme { isDark ? darkTheme : lightTheme }
/// The accent's id ("signal", "teal", "graphite"); a stored pre-redesign swatch migrates once.
var accentName = ud.string(forKey: "accentName").flatMap { n in accents.contains { $0.id == n } ? n : nil } ?? migratedAccent(ud.string(forKey: "accent"))
var accentSet: AccentSet { accents.first { $0.id == accentName } ?? accents[0] }
var chatFont = ud.string(forKey: "chatFont") ?? "System"
var codeFont = ud.string(forKey: "codeFont") ?? "SF Mono"
var fontSize = ud.object(forKey: "fontSize") as? Double ?? 13

// The app's native colours: the semantic tokens (§2) in the current appearance.
var accent: Color { Color(accentSet.solid, dark: isDark) }
var bg: Color { Color(DS.Palette.bgCanvas, dark: isDark) }
var fg: Color { Color(DS.Palette.fgDefault, dark: isDark) }
var card: Color { Color(DS.Palette.bgSurface, dark: isDark) }
var surface: Color { Color(DS.Palette.elementRest, dark: isDark) }
var line: Color { Color(DS.Palette.borderSubtle, dark: isDark) }
var edge: Color { Color(DS.Palette.borderDefault, dark: isDark) }
var dim: Color { Color(DS.Palette.fgMuted, dark: isDark) }
var sub: Color { Color(DS.Palette.fgMuted, dark: isDark) }
var faint: Color { Color(DS.Palette.fgFaint, dark: isDark) }
var bubble: Color { Color(DS.Palette.elementRest, dark: isDark) }

/// `.system(size:)` at the chosen size; `design: .monospaced` uses the code font. Interface chrome stays in the system font.
/// The box an icon gets beside text of `pt` points (brand/DESIGN.md §9): about a quarter more than the type size, as SF Symbols
/// sit. It follows Settings › Text size like sys(), unless `scaled` is false.
/// Greenlight's mark (brand/icon/mark*.svg, bundled by build.sh) as template images: lit (blades and light), the blades
/// alone (idle), or the light alone (to colour it separately, as the orb does). Never coloured in the menu bar.
enum GreenlightMark {
    enum Part: String { case lit = "GreenlightMark", idle = "GreenlightMarkIdle", light = "GreenlightMarkLight" }
    static func image(_ part: Part, size: CGFloat = 16) -> NSImage {
        let img = Bundle.main.url(forResource: part.rawValue, withExtension: "svg").flatMap { NSImage(contentsOf: $0) }
            ?? DSIcon.image("hex", size: size)  // a build without the marks (swiftc alone): the board's cell stands in
        img.isTemplate = true
        img.size = NSSize(width: size, height: size)
        img.accessibilityDescription = "Greenlight"
        return img
    }
}

func iconSize(_ pt: CGFloat, scaled: Bool = true) -> CGFloat { (pt * (scaled ? fontSize / 13 : 1) * 1.25).rounded() }

func sys(size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default, face: String = "System") -> Font {
    let s = size * fontSize / 13, f = design == .monospaced ? codeFont : face
    if f == "System" { return .system(size: s, weight: weight, design: design) }
    if f == "SF Mono" { return .system(size: s, weight: weight, design: .monospaced) }
    return .custom(f, size: s).weight(weight)
}
extension DSType {
    var fontWeight: Font.Weight { weight >= 700 ? .bold : weight >= 600 ? .semibold : weight >= 500 ? .medium : .regular }
}
/// A type role (brand/DESIGN.md §3) as a font at Settings › Text size; `mono` sets it in the code font, `face` in the chat font.
func ds(_ t: DSType, mono: Bool = false, face: String = "System") -> Font { sys(size: t.size, weight: t.fontWeight, design: mono ? .monospaced : .default, face: face) }
extension Color {
    /// A colour token in the given appearance.
    init(_ c: DSColor, dark: Bool) { self.init(nsColor: c.ns(dark: dark)) }
}
/// A colour token as an NSColor that follows whatever appearance it's drawn in (window backgrounds, AppKit views).
func dsDynamic(_ c: DSColor) -> NSColor {
    NSColor(name: nil) { a in c.ns(dark: a.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua) }
}
/// A colour token as "RRGGBB", for the places that still carry colours as hex strings (Look).
func dsHex(_ c: DSColor, dark: Bool) -> String { String(format: "%06X", dark ? c.dark : c.light) }
/// The chosen chat font: for the conversation's own text (messages, the composer, questions), not the chrome around it.
func chatFace(size: CGFloat, weight: Font.Weight = .regular) -> Font { sys(size: size, weight: weight, face: chatFont) }
let models = [("Opus 5.5", "claude-opus-5-5"), ("Sonnet 5.5", "claude-sonnet-5-5"), ("Haiku 5.5", "claude-haiku-5-5"),
              ("Fable 5.1", "claude-fable-5-1"),
              ("Gemini 4 Neon · Low", "gemini-4-neon-low"), ("Gemini 4 Neon · Medium", "gemini-4-neon-medium"),
              ("Gemini 4 Neon · High", "gemini-4-neon-high"), ("Gemini 4 Neon · Extra high", "gemini-4-neon-xhigh"),
              ("Gemini 4 Argon · Medium", "gemini-4-argon-medium"), ("Gemini 4 Hydrogen · Medium", "gemini-4-hydrogen-medium"),
              ("Gemini 3.8 Flash · Medium", "gemini-3.8-flash-medium"),
              ("Codex", "codex"), ("Antigravity", "agy"), ("OpenCode", "opencode"), ("Pi", "pi"), ("Gemini CLI", "gemini")]
let knownHarnesses: Set<String> = ["claude", "codex", "agy", "opencode", "pi", "gemini"]
let effortSuffixes = ["-low", "-medium", "-high", "-xhigh", "-max"]
/// Which harness runs `model` (mirrors `harness.harness_of`): claude-*, gpt-*/codex-*/o<N>, gemini-*/gpt-oss-*, <harness>:<model>, or a harness name.
func harnessOf(_ model: String) -> String {
    if knownHarnesses.contains(model) { return model }
    if let i = model.firstIndex(of: ":") {
        let head = String(model[..<i])
        if knownHarnesses.contains(head) { return head }
    }
    if model.hasPrefix("claude-") { return "claude" }
    if model.hasPrefix("gemini-") || model.hasPrefix("gpt-oss-") { return "agy" }
    if model.hasPrefix("gpt-") || model.hasPrefix("codex-") { return "codex" }
    if model.hasPrefix("o"), model.dropFirst().first?.isNumber == true { return "codex" }
    return model
}
/// (harness, model id or nil for that harness's default), matching `harness.route`.
func routeModel(_ model: String) -> (harness: String, id: String?) {
    let h = harnessOf(model)
    if !knownHarnesses.contains(h) { return ("claude", model.isEmpty ? nil : model) }
    if model == h { return (h, nil) }
    if model.hasPrefix(h + ":") {
        let rest = String(model.dropFirst(h.count + 1))
        return (h, rest.isEmpty ? nil : rest)
    }
    return (h, model == "gemini-4-neon" ? "gemini-4-neon-medium" : model)
}
/// Whether a separate Effort picker applies to `model` (Claude, Codex, and agy models whose ID doesn't bake effort in).
func modelSupportsEffort(_ model: String) -> Bool {
    let h = harnessOf(model)
    if h == "claude" || h == "codex" { return true }
    if h == "agy" { return !effortSuffixes.contains { model.hasSuffix($0) } }
    return false
}
func localHarnessBin(_ name: String) -> String {
    if let override = ud.string(forKey: "\(name)Bin"), !override.isEmpty { return override }
    let candidates = ["\(NSHomeDirectory())/.local/bin/\(name)", "\(NSHomeDirectory())/.\(name)/bin/\(name)",
                      "/opt/homebrew/bin/\(name)", "/usr/local/bin/\(name)"]
    return candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "\(NSHomeDirectory())/.local/bin/\(name)"
}
/// One toolless CLI invocation for Claude, Codex, or agy (used for tab naming and window restyling).
func llmInvocation(_ prompt: String, model: String, effort: String = "low") -> (harness: String, bin: String, args: [String], stdin: String, shell: String) {
    let routed = routeModel(model)
    let h = ["claude", "codex", "agy"].contains(routed.harness) ? routed.harness : "claude"
    let bin = localHarnessBin(h)
    if h == "codex" {
        let eff = (effort == "xhigh" || effort == "max") ? "high" : effort
        var args = ["exec", "--skip-git-repo-check", "-c", "sandbox_mode=read-only"]
        var shell = "cd /tmp && exec codex exec --skip-git-repo-check -c sandbox_mode=read-only"
        if let id = routed.id { args += ["-m", id]; shell += " -m \(shellQuote(id))" }
        args += ["-c", "model_reasoning_effort=\(eff)", "-"]
        shell += " -c model_reasoning_effort=\(shellQuote(eff)) -"
        return (h, bin, args, prompt, shell)
    }
    if h == "agy" {
        var args = ["--input-format", "stream-json", "--output-format", "stream-json"]
        var shell = "cd /tmp && exec agy --input-format stream-json --output-format stream-json"
        if let id = routed.id { args += ["--model", id]; shell += " --model \(shellQuote(id))" }
        let baked = routed.id.map { m in effortSuffixes.contains { m.hasSuffix($0) } } ?? false
        if !baked { args += ["--effort", effort]; shell += " --effort \(shellQuote(effort))" }
        let payload: [String: Any] = ["event": "user", "message": ["role": "user", "content": prompt]]
        let line = (try? JSONSerialization.data(withJSONObject: payload)).flatMap { String(data: $0, encoding: .utf8) } ?? ""
        return (h, bin, args, line + "\n", shell)
    }
    let id = routed.id ?? "claude-haiku-5-5"
    return ("claude", bin, ["-p", "--tools", "", "--no-session-persistence", "--model", id, "--effort", effort], prompt,
            "cd /tmp && exec claude -p --tools '' --no-session-persistence --model \(shellQuote(id)) --effort \(shellQuote(effort))")
}
/// Extract the final text response from `agy --output-format stream-json` output ("" on failure).
func parseAgyStreamResult(_ output: String) -> String {
    for line in output.split(separator: "\n", omittingEmptySubsequences: true).reversed() {
        guard let data = line.data(using: .utf8),
              let ev = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              ev["event"] as? String == "result",
              let res = ev["result"] as? [String: Any] else { continue }
        return res["status"] as? String == "SUCCESS" ? (res["response"] as? String ?? "") : ""
    }
    return ""
}
/// A Gemini model that came back with Claude Code's "issue with the selected model": the host's chat.py predates agy and
/// handed the id to `claude`, so the model is fine and the host's greenlight is older than this app. Says so, else nil.
func staleBackendNote(model: String, error: String, host: String, code: String) -> String? {
    guard harnessOf(model) == "agy", error.contains("issue with the selected model") else { return nil }
    let on = host.isEmpty || host == "local" ? "this Mac" : host
    return "Greenlight on \(on) is older than this app: its \(code) doesn't run Gemini models through agy, so it sent \(model) to Claude Code. "
        + "Update that checkout to main (git pull there, or scripts/sync.sh from this Mac), or pick a Claude model."
}
func savedModel(_ key: String, fallback: String) -> String {
    let stored = ud.string(forKey: key) ?? fallback
    return stored == "gemini-4-neon" ? "gemini-4-neon-medium" : stored
}
let efforts = ["low", "medium", "high", "xhigh", "max"]
func effortName(_ e: String) -> String { e == "xhigh" ? "Extra high" : e.capitalized }  // display only; the stored value stays
let effortNotes: [String: String] = [
    "low": "Fastest replies, least reasoning.",
    "medium": "Balances reasoning against response time.",
    "high": "More reasoning; replies take longer.",
    "xhigh": "Extended reasoning for hard problems; noticeably slower.",
    "max": "The most reasoning; the slowest replies.",
]
let writerHarnesses: Set<String> = ["claude", "codex", "agy"]

/// Model manufacturer / provider buckets for the reusable Settings model selector.
enum ModelManufacturer: String, CaseIterable, Equatable, Hashable, Sendable {
    case anthropic, openai, google, other

    var name: String {
        switch self {
        case .anthropic: return "Anthropic"
        case .openai: return "OpenAI"
        case .google: return "Google"
        case .other: return "Other"
        }
    }

    var icon: String {
        switch self {
        case .anthropic: return "claude"
        case .openai: return "openai"
        case .google: return "agy"
        case .other: return "terminal"
        }
    }

    /// Infers the manufacturer from a model ID (including `<harness>:<model>` pins like `agy:claude-opus-5-5-high`).
    static func of(_ modelId: String) -> ModelManufacturer {
        let h = harnessOf(modelId)
        let raw = routeModel(modelId).id ?? ""
        let low = raw.lowercased()
        if low.hasPrefix("claude-") || modelId == "claude" { return .anthropic }
        if low.hasPrefix("gpt-oss-") || low.hasPrefix("gpt-") || low.hasPrefix("codex-") || modelId == "codex" { return .openai }
        if low.hasPrefix("o"), low.dropFirst().first?.isNumber == true { return .openai }
        if low.hasPrefix("gemini-") || modelId == "agy" || modelId == "gemini" { return .google }
        switch h {
        case "claude": return .anthropic
        case "codex": return .openai
        case "agy", "gemini": return .google
        default: return .other
        }
    }
}

/// Splits an `agy` model ID whose effort is baked into the slug (e.g. `gemini-4-neon-high`, `agy:claude-opus-5-5-low`)
/// into its base model ID, cleaned display name, and effort level. Non-baked IDs return `(id, name, nil)`.
func splitBakedEffort(id: String, name: String) -> (baseID: String, baseName: String, bakedEffort: String?) {
    guard harnessOf(id) == "agy" else { return (id, name, nil) }
    for eff in efforts {
        let suffix = "-\(eff)"
        if id.hasSuffix(suffix) {
            let baseID = String(id.dropLast(suffix.count))
            let cleaned = stripEffortLabel(from: name)
            return (baseID, cleaned.isEmpty ? baseID : cleaned, eff)
        }
    }
    return (id, name, nil)
}

/// Strips trailing or parenthesised effort labels (` (High)`, ` (XHigh)`, ` · Extra high`, etc.) from a model display name.
private func stripEffortLabel(from name: String) -> String {
    var out = name
    let patterns = [
        #"\s*\((?:Low|Medium|High|XHigh|Extra high|Max)\)"#,
        #"\s*·\s*(?:Low|Medium|High|XHigh|Extra high|Max)\b"#,
    ]
    for p in patterns {
        out = out.replacingOccurrences(of: p, with: "", options: [.regularExpression, .caseInsensitive])
    }
    return out.trimmingCharacters(in: .whitespaces)
}

/// DS icon name for a model button based on its family/tier (every name is in `brand/icons`).
func modelIcon(for baseID: String, harness: String) -> String {
    if knownHarnesses.contains(baseID) { return "terminal" }
    let low = (routeModel(baseID).id ?? baseID).lowercased()
    let tokens = Set(low.split { !$0.isLetter && !$0.isNumber }.map(String.init))
    if tokens.contains("orchestrator") { return "grid" }
    if low.contains("gpt-oss") { return "branch" }
    if tokens.contains("haiku") || tokens.contains("flash") || tokens.contains("mini") || tokens.contains("luna") { return "bolt" }
    if tokens.contains("opus") || tokens.contains("neon") || tokens.contains("sol") || tokens.contains("ultra") { return "sparkles" }
    if tokens.contains("fable") || tokens.contains("daybreak") { return "wand" }
    if tokens.contains("sonnet") || tokens.contains("carbon") || tokens.contains("argon") || tokens.contains("hydrogen")
        || tokens.contains("astra") || tokens.contains("terra") { return "spark" }
    switch harness {
    case "claude": return "claude"
    case "codex": return "openai"
    case "agy": return "agy"
    default: return "terminal"
    }
}

/// Anthropic list prices (input $ per 1M tokens), used only to order the Claude models in `ModelSelector`.
let claudeInputPrice: [String: Double] = [
    "fable-5-1": 10, "fable-5": 10, "fable-5-2": 8, "opus-5": 5, "opus-4-8": 5, "opus-4-7": 5, "opus-4-6": 5,
    "opus-5-5": 4, "sonnet-4-6": 3, "sonnet-5-5": 2, "sonnet-5": 2, "haiku-4-5": 1, "haiku-5-5": 0.1,
]

/// Where a model sorts inside its provider in `ModelSelector`: lower first, so the most capable (most expensive) leads.
/// Claude models go by list price; other providers publish none here, so their codenames give a tier; a harness's
/// own default (`codex`, `agy`, `opencode`…) goes last. Equal ranks keep `harness.py models`' order (newest first).
func modelCostRank(_ id: String) -> Double {
    if knownHarnesses.contains(id) { return 1_000 }
    let low = (routeModel(id).id ?? id).lowercased()
    if low.hasPrefix("claude-"), let price = claudeInputPrice[String(low.dropFirst(7))] { return -price }
    if low.contains("gpt-oss") { return 40 }
    let tokens = Set(low.split { !$0.isLetter && !$0.isNumber }.map(String.init))
    let tiers: [(Double, Set<String>)] = [
        (10, ["fable", "opus", "sol", "neon", "daybreak", "ultra", "orchestrator", "pro"]),
        (30, ["haiku", "luna", "flash", "mini", "nano", "lite"]),
        (20, ["sonnet", "astra", "terra", "carbon", "argon", "hydrogen"]),
    ]
    return tiers.first { !$0.1.isDisjoint(with: tokens) }?.0 ?? 25
}

/// A starred model and the effort it runs at, shared by every `ModelSelector` (Core.modelFavorites).
/// `effort` is "" for a model that takes none; a baked-effort id (`gemini-4-neon-high`) carries its own as well.
struct ModelFavorite: Codable, Equatable, Hashable, Sendable {
    let model: String
    let effort: String
}

/// The favorite starring `choice` at `desiredEffort` would save: the concrete id and the effort it resolves to.
func modelFavorite(for choice: ModelChoice, desiredEffort: String) -> ModelFavorite {
    let r = choice.resolve(desiredEffort: desiredEffort)
    return ModelFavorite(model: r.modelId, effort: choice.supportedEfforts.isEmpty ? "" : r.effort)
}

/// `favs` with `f` removed if it's there, else appended (favorites keep the order you starred them in).
func togglingFavorite(_ f: ModelFavorite, in favs: [ModelFavorite]) -> [ModelFavorite] {
    favs.contains(f) ? favs.filter { $0 != f } : favs + [f]
}

/// Favorites as saved under `key` in `ud`: a JSON array of {model, effort}; [] when unset or unreadable.
func savedModelFavorites(_ key: String = "modelFavorites") -> [ModelFavorite] {
    guard let data = ud.data(forKey: key), let favs = try? JSONDecoder().decode([ModelFavorite].self, from: data) else { return [] }
    return favs.filter { !$0.model.isEmpty }
}

/// A favorite's chip label: the model's name from `catalog` (else the id) and its effort ("Opus 5.5 · High").
func modelFavoriteLabel(_ f: ModelFavorite, in catalog: [ModelGroup]) -> String {
    let known = models.first { $0.1 == f.model }?.0 ?? f.model  // not offered now: the built-in name, its effort dropped
    let name = catalog.lazy.flatMap(\.choices).first { $0.matches(modelId: f.model) }?.name
        ?? splitBakedEffort(id: f.model, name: known).baseName
    return f.effort.isEmpty ? name : "\(name) · \(effortName(f.effort))"
}

/// Picks `desired` when supported; otherwise the closest lower level in `efforts`, else the lowest supported level.
func nearestEffort(_ desired: String, in supported: [String]) -> String {
    if supported.contains(desired) { return desired }
    let ordered = efforts.filter { supported.contains($0) }
    guard !ordered.isEmpty else { return desired }
    guard let wantIdx = efforts.firstIndex(of: desired) else {
        return ordered.contains("medium") ? "medium" : ordered[0]
    }
    if let below = ordered.last(where: { (efforts.firstIndex(of: $0) ?? 0) <= wantIdx }) {
        return below
    }
    return ordered[0]
}

/// One selectable model button inside `ModelSelector`, collapsing any baked-effort variants into a single choice.
struct ModelChoice: Identifiable, Equatable, Sendable {
    let id: String
    let name: String
    let manufacturer: ModelManufacturer
    let harness: String
    let icon: String
    let bakedVariants: [String: String]
    let supportedEfforts: [String]

    var hasBakedEffort: Bool { !bakedVariants.isEmpty }
    var supportsEffortSlider: Bool { supportedEfforts.count > 1 }
    var fixedEffort: String? { supportedEfforts.count == 1 ? supportedEfforts[0] : nil }

    func matches(modelId: String) -> Bool {
        if hasBakedEffort { return bakedVariants.values.contains(modelId) }
        return id == modelId
    }

    func bakedEffort(for modelId: String) -> String? {
        bakedVariants.first { $0.value == modelId }?.key
    }

    /// Concrete `(modelId, effort)` pair when choosing this model with `desiredEffort`.
    func resolve(desiredEffort: String) -> (modelId: String, effort: String) {
        guard !supportedEfforts.isEmpty else { return (id, desiredEffort) }
        let chosen = nearestEffort(desiredEffort, in: supportedEfforts)
        if hasBakedEffort { return (bakedVariants[chosen] ?? id, chosen) }
        return (id, chosen)
    }
}

/// One manufacturer tab and its ordered model choices.
struct ModelGroup: Identifiable, Equatable, Sendable {
    var id: ModelManufacturer { manufacturer }
    let manufacturer: ModelManufacturer
    let choices: [ModelChoice]
}

/// Groups raw `(name, id)` options into Manufacturer -> Model choices, collapsing baked-effort variants
/// and always keeping `current` selectable even when offline or custom.
func buildModelCatalog(from rawOptions: [(String, String)], current: String, where keep: (String) -> Bool = { _ in true }) -> [ModelGroup] {
    var list = rawOptions.filter { keep($0.1) }
    if !current.isEmpty && !list.contains(where: { $0.1 == current }) {
        let label = models.first { $0.1 == current }?.0 ?? current
        list.append((label, current))
    }
    var order = [String]()
    var grouped = [String: (name: String, mfg: ModelManufacturer, harness: String, variants: [String: String])]()
    for (name, id) in list {
        let h = harnessOf(id)
        let mfg = ModelManufacturer.of(id)
        let (baseID, rawBaseName, baked) = splitBakedEffort(id: id, name: name)
        let displayName = (mfg == .anthropic && rawBaseName.hasPrefix("Claude ")) ? String(rawBaseName.dropFirst(7)) : rawBaseName
        if let baked {
            if grouped[baseID] == nil {
                order.append(baseID)
                grouped[baseID] = (displayName, mfg, h, [baked: id])
            } else {
                grouped[baseID]?.variants[baked] = id
            }
        } else if grouped[id] == nil {
            order.append(id)
            grouped[id] = (displayName, mfg, h, [:])
        }
    }
    var byMfg = [ModelManufacturer: [ModelChoice]]()
    for key in order {
        guard let entry = grouped[key] else { continue }
        let supported = entry.variants.isEmpty
            ? (modelSupportsEffort(key) ? efforts : [])
            : efforts.filter { entry.variants[$0] != nil }
        let choice = ModelChoice(id: key, name: entry.name, manufacturer: entry.mfg, harness: entry.harness,
                                 icon: modelIcon(for: key, harness: entry.harness),
                                 bakedVariants: entry.variants, supportedEfforts: supported)
        byMfg[entry.mfg, default: []].append(choice)
    }
    return ModelManufacturer.allCases.compactMap { mfg in
        guard let choices = byMfg[mfg], !choices.isEmpty else { return nil }
        let ranked = choices.enumerated().sorted { a, b in
            let (ra, rb) = (modelCostRank(a.element.id), modelCostRank(b.element.id))
            return ra != rb ? ra < rb : a.offset < b.offset
        }.map(\.element)
        return ModelGroup(manufacturer: mfg, choices: ranked)
    }
}

/// Finds the active `(group, choice)` matching `modelId`, falling back to the first available entry.
func activeModelSelection(in catalog: [ModelGroup], modelId: String) -> (group: ModelGroup, choice: ModelChoice)? {
    for g in catalog {
        if let ch = g.choices.first(where: { $0.matches(modelId: modelId) }) { return (g, ch) }
    }
    guard let firstGroup = catalog.first, let firstChoice = firstGroup.choices.first else { return nil }
    return (firstGroup, firstChoice)
}

/// Resolves the effort value shown on the slider for `choice` given the current `modelId` and `storedEffort`.
func currentModelEffort(for choice: ModelChoice, modelId: String, storedEffort: String) -> String {
    if let baked = choice.bakedEffort(for: modelId) { return baked }
    if choice.supportedEfforts.isEmpty { return storedEffort }
    return nearestEffort(storedEffort, in: choice.supportedEfforts)
}

// MARK: Settings: repo lifecycles, instructions, memory

/// One lifecycle stage as dash.py's lifecycle-get gives it: a built-in check, or the user's own (done: "signoff" | "label=x" | "check=x").
struct Stage: Identifiable, Equatable { let id = UUID(); var key: String, req: String, done: String, run = "", owner = "", phase = "" }

/// Settings → Repositories / Memory and the chat's instructions: each read and saved with `dash.py task <op> <arg>` where greenlight runs.
final class RulesStore: ObservableObject {
    @Published var repos = [String]()
    @Published var repo = ""
    @Published var stages = [Stage]()
    @Published var openStages = Set<String>()  // built-in stages whose long requirement is open in Settings
    @Published var options = [String: String]()
    @Published var rules = ""
    @Published var checklist = ""  // the lifecycle's ## Checklist: diff-triggered reliability checks every agent brief carries
    @Published var intro = ""
    @Published var source = ""
    @Published var builtins = [String: String]()
    @Published var instructions = ""
    @Published var notes = [(name: String, text: String)]()
    @Published var note = ""
    @Published var noteText = ""
    @Published var projs = [[String: Any]]()  // the Basement's projects (projects.json): Settings › Projects edits them
    @Published var projNote = ""
    @Published var review = ""  // your own prompt for repo's kind (review, verify, threads, ci, conflicts); reviewOwn false = the default
    @Published var reviewDefault = ""
    @Published var reviewOwn = false
    @Published var reviewVars = [String]()
    @Published var kind = "review"
    static let kinds = [("review", "Review"), ("verify", "Verify"), ("threads", "Resolve threads"), ("ci", "Fix CI"), ("conflicts", "Fix conflicts")]
    @Published var autoKinds = [(kind: String, when: String, gates: [String])]()  // Auto-run: dash.py's AUTO, and this repo's {kind: gate}
    @Published var autoOn = [String: Int]()
    @Published var repoHostMode = "server"  // Settings → Repositories: server (default) | mac | auto
    @Published var repoHostTarget = "server"
    @Published var repoNeedsMac = false
    @Published var repoNeedsMacReason = ""
    @Published var repoHosts = [String: RepoHostInfo]()
    @Published var status = ""
    @Published var busy = false
    @Published var repoPaths = [String: String]()  // repo → its path in orca, under its name in Repositories
    // Settings' own UI state: the sidebar's search, the open project and repo tab, the add forms' drafts
    @Published var q = ""
    @Published var proj = ""
    @Published var rtab = "stages"
    @Published var adding = false
    @Published var draftRepo = ""
    @Published var draftProj = ""
    @Published var draftDir = ""
    // Settings → Import (#195): which ADEs are on the host, the ones picked, folders to scan, and the last preview
    @Published var importFound = [String: Bool]()
    @Published var importOn = Set<String>()
    var importTouched = false  // the user picked sources: a later detect keeps their picks (minus any gone)
    @Published var importPaths = ""
    @Published var importPlan: [String: Any]?
    @Published var importDone = ""
    static let importSources = [("orca", "Orca"), ("t3", "T3 Code"), ("cmux", "cmux"), ("herdr", "herdr")]
    var loaded = false
    private var pending = 0
    private var hostSeqByRepo = [String: Int]()

    private func nextHostSeq(for r: String) -> Int {
        let s = (hostSeqByRepo[r] ?? 0) + 1
        hostSeqByRepo[r] = s
        return s
    }

    /// Settings → Import: the ADEs dash.py finds on the host, each picked by default.
    func importDetect() {
        status = ""
        call("import-detect", "-") { j in
            self.importFound = (j["sources"] as? [String: Bool]) ?? [:]
            let found = Set(Self.importSources.map(\.0).filter { self.importFound[$0] == true })
            let on = self.importTouched ? self.importOn.intersection(found) : found
            if on != self.importOn { self.importOn = on; self.importPlan = nil }  // Import imports only what was previewed
        }
    }
    /// Preview (apply false: changes nothing) or import what's picked; the answer is the plan either way.
    /// The request for what the pane has picked: the sources on, and the folders typed.
    var importBody: String {
        let paths = importPaths.split(whereSeparator: { $0 == "," || $0.isNewline }).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let from = Self.importSources.map(\.0).filter(importOn.contains) + (paths.isEmpty ? [] : ["folder"])
        return (try? JSONSerialization.data(withJSONObject: ["from": from, "paths": paths], options: [.sortedKeys])).flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
    }
    func importRun(apply: Bool) {
        let body = importBody
        importDone = ""; status = ""
        call(apply ? "import-apply" : "import-preview", "-", body) { j in
            // a preview answered after the picks changed is for something else: Import must only follow its own preview
            if !apply && body != self.importBody { return }
            self.importPlan = j
            let n = (j["repos"] as? [Any])?.count ?? 0, g = (j["goals"] as? [String: Any])?.count ?? 0
            if apply {  // the preview goes; a source that failed during the import itself still says so
                let srcs = (j["sources"] as? [String: Any]) ?? [:]
                let failed = srcs.compactMap { k, v in k != "folder" && (v as? [String: Any])?["error"] != nil ? k : nil }.sorted()
                let folder = (srcs["folder"] as? [String: Any])?["error"] as? String
                self.importDone = "Imported \(n) repo\(n == 1 ? "" : "s") and \(g) goal\(g == 1 ? "" : "s")."
                    + (failed.isEmpty ? "" : " Couldn't read \(failed.joined(separator: ", ")): run it again once \(failed.count == 1 ? "it's" : "they're") up.")
                    + (folder.map { " Skipped \($0.contains("; ") ? "folders" : "a folder"): \($0)." } ?? "")
                self.importPlan = nil
            }
        }
    }

    func call(_ op: String, _ arg: String, _ stdin: String = "", onError: (() -> Void)? = nil, _ done: @escaping ([String: Any]) -> Void) {
        pending += 1; busy = true
        var out = ""
        ssh("\(py)/dash.py task \(shellQuote(op)) \(shellQuote(arg))", stdin: stdin, onLine: { out += $0 }, onExit: { [weak self] code in
            guard let self else { return }
            self.pending = max(0, self.pending - 1); self.busy = self.pending > 0
            let j = (try? JSONSerialization.jsonObject(with: Data(out.utf8))) as? [String: Any] ?? ["error": "\(host) didn't answer (exit \(code))"]
            if let e = j["error"] as? String { self.status = e; onError?() } else { done(j) }
        })
    }
    private func addRepos(_ list: [[String: Any]]) {
        let paths = list.compactMap { $0["path"] as? String }
        let names = paths.compactMap { $0.split(separator: "/").last.map(String.init) }
        for (n, p) in zip(names, paths) where repoPaths[n] == nil { repoPaths[n] = p }
        repos = Array(Set(repos + names + (repo.isEmpty ? [] : [repo]))).sorted()
        if repo.isEmpty, let r = repos.first { pick(r) }
    }
    func load() {
        if loaded { return }
        loaded = true
        repoHosts = delegate.repoHosts
        call("repos", "-") { self.addRepos(($0["repos"] as? [[String: Any]]) ?? []) }
        call("repo-hosts", "-") { j in
            let parsed = parseRepoHosts(j["hosts"])
            if !parsed.isEmpty {
                self.repoHosts = mergeRepoHostMaps(self.repoHosts, parsed)
                delegate.mergeRepoHosts(parsed)
            }
        }
        if !isLocalHost(host) && !uitest && !selftest {
            var rOut = "", hOut = ""
            ssh("\(pyCmd(on: "local"))/dash.py task repos -", on: "local", onLine: { rOut += $0 }, onExit: { [weak self] code in
                guard code == 0, let j = (try? JSONSerialization.jsonObject(with: Data(rOut.utf8))) as? [String: Any] else { return }
                self?.addRepos((j["repos"] as? [[String: Any]]) ?? [])
            })
            ssh("\(pyCmd(on: "local"))/dash.py task repo-hosts -", on: "local", onLine: { hOut += $0 }, onExit: { [weak self] code in
                guard code == 0, let self, let j = (try? JSONSerialization.jsonObject(with: Data(hOut.utf8))) as? [String: Any] else { return }
                let parsed = parseRepoHosts(j["hosts"])
                guard !parsed.isEmpty else { return }
                // Keep server mode if already known; inherit local Mac-only repos and local needs_mac detection
                var combined = self.repoHosts
                for (k, v) in parsed {
                    if let ex = combined[k] {
                        let needs = ex.needsMac || v.needsMac
                        let reason = !ex.reason.isEmpty ? ex.reason : v.reason
                        let target = (ex.mode == "mac" || (ex.mode == "auto" && needs)) ? "mac" : "server"
                        combined[k] = RepoHostInfo(mode: ex.mode, target: target, needsMac: needs, reason: reason)
                    } else {
                        combined[k] = v
                    }
                }
                self.repoHosts = combined
                delegate.mergeRepoHosts(combined)
            })
        }
        call("instructions-get", "-") { self.instructions = $0["text"] as? String ?? "" }
        call("memory-list", "-") { self.gotNotes($0) }
    }
    func pick(_ r: String) {
        repo = r; status = ""; if !repos.contains(r) { repos = (repos + [r]).sorted() }
        let seq = nextHostSeq(for: r)
        if let cached = lookupRepoHost(r, in: repoHosts) ?? lookupRepoHost(r, in: delegate.repoHosts) {
            repoHostMode = cached.mode; repoHostTarget = cached.target
            repoNeedsMac = cached.needsMac; repoNeedsMacReason = cached.reason
        } else {
            repoHostMode = "server"; repoHostTarget = "server"; repoNeedsMac = false; repoNeedsMacReason = ""
        }
        call("lifecycle-get", r) { [weak self] j in
            guard let self, self.repo == r else { return }
            self.got(j, for: r, clearOnChange: false, applyHost: self.hostSeqByRepo[r] == seq)
        }
        getPrompt()
        call("autorun-get", r) { [weak self] j in if self?.repo == r { self?.gotAuto(j) } }
    }
    func applyHostInfo(_ raw: [String: Any]?, for r: String, clearOnChange: Bool) {
        let localKnown = lookupRepoHost(r, in: delegate.repoHosts)
        let optHost = (repo == r ? options["host"] : nil) ?? (raw?["options"] as? [String: String])?["host"]
        let m = (raw?["mode"] as? String ?? optHost ?? "server").lowercased()
        let mode = ["server", "mac", "auto"].contains(m) ? m : "server"
        let needs = (raw?["needs_mac"] as? Bool ?? false) || (localKnown?.needsMac ?? false)
        let reason = !(raw?["reason"] as? String ?? "").isEmpty ? (raw?["reason"] as? String ?? "") : (localKnown?.reason ?? "")
        let target = (mode == "mac" || (mode == "auto" && needs)) ? "mac" : "server"
        let info = RepoHostInfo(mode: mode, target: target, needsMac: needs, reason: reason)
        repoHosts = setRepoHostInMap(repoHosts, repo: r, info: info)
        if repo == r {
            repoHostMode = info.mode; repoHostTarget = info.target
            repoNeedsMac = info.needsMac; repoNeedsMacReason = info.reason
        }
        delegate.updateRepoHost(r, info, clearOnChange: clearOnChange)
    }
    func setHost(_ mode: String) {
        guard ["server", "mac", "auto"].contains(mode), !repo.isEmpty else { return }
        let r = repo, prevMode = repoHostMode, prevTarget = repoHostTarget, prevOpt = options["host"]
        let seq = nextHostSeq(for: r)
        repoHostMode = mode
        let needs = repoNeedsMac || (lookupRepoHost(r, in: delegate.repoHosts)?.needsMac ?? false)
        repoHostTarget = (mode == "mac" || (mode == "auto" && needs)) ? "mac" : "server"
        if mode == "server" { options["host"] = nil } else { options["host"] = mode }
        call("host-set", r, mode, onError: { [weak self] in
            guard let self, self.repo == r, self.hostSeqByRepo[r] == seq else { return }
            self.repoHostMode = prevMode; self.repoHostTarget = prevTarget; self.options["host"] = prevOpt
        }) { [weak self] j in
            guard let self else { return }
            if !isLocalHost(host) && !uitest && !selftest {
                ssh("\(pyCmd(on: "local"))/dash.py task host-set \(shellQuote(r))", on: "local", stdin: mode)
            }
            guard self.hostSeqByRepo[r] == seq else { return }
            if let opts = j["options"] as? [String: String], self.repo == r { self.options = opts }
            if let src = j["source"] as? String, self.repo == r { self.source = src }
            self.applyHostInfo(j, for: r, clearOnChange: true)
        }
    }
    func gotAuto(_ j: [String: Any]) {
        autoKinds = (j["kinds"] as? [[String: Any]] ?? []).map { (kind: $0["kind"] as? String ?? "", when: $0["when"] as? String ?? "", gates: $0["gates"] as? [String] ?? []) }
        autoOn = j["on"] as? [String: Int] ?? [:]
    }
    func setAuto(_ k: String, _ gate: Int?) {  // saved at once; nil = off
        let r = repo
        autoOn[k] = gate
        if gate != nil { ud.set(true, forKey: "autorun") }  // the app keeps the Basement's page loaded and its workspaces fresh from now on
        let s = String(decoding: (try? JSONSerialization.data(withJSONObject: autoOn)) ?? Data(), as: UTF8.self)
        call("autorun-set", r, s) { [weak self] j in if self?.repo == r { self?.gotAuto(j) } }
    }
    func getPrompt() {
        let r = repo
        call("prompt-get", "\(r)/\(kind)") { [weak self] j in if self?.repo == r { self?.gotReview(j) } }
    }
    func gotReview(_ j: [String: Any]) {
        guard j["kind"] as? String == kind else { return }  // an answer for the tab you just left
        review = j["text"] as? String ?? ""; reviewDefault = j["default"] as? String ?? ""; reviewOwn = j["own"] as? Bool ?? false
        reviewVars = (j["vars"] as? [String] ?? []).map { "{\($0)}" } + ["{default}"]
    }
    func saveReview() {  // empty or Default: back to the default (dash.py deletes yours)
        let r = repo, own = reviewOwn && !review.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        call("prompt-set", "\(r)/\(kind)", own ? review : "") { [weak self] j in if self?.repo == r { self?.gotReview(j); self?.status = own ? "Saved the prompt." : "Using the default prompt." } }
    }
    func got(_ j: [String: Any], for targetRepo: String? = nil, clearOnChange: Bool = false, applyHost: Bool = true) {
        let r = targetRepo ?? repo
        let keptHostOpt = options["host"]
        if repo == r {
            stages = ((j["stages"] as? [[String: String]]) ?? []).map {
                Stage(key: $0["key"] ?? "", req: $0["req"] ?? "", done: $0["done"] ?? "", run: $0["run"] ?? "", owner: $0["owner"] ?? "", phase: $0["phase"] ?? "")
            }
            var newOpts = j["options"] as? [String: String] ?? [:]
            if !applyHost { newOpts["host"] = keptHostOpt }
            options = newOpts
            rules = j["rules"] as? String ?? ""; checklist = j["checklist"] as? String ?? ""
            intro = j["intro"] as? String ?? ""; source = j["source"] as? String ?? ""
            builtins = j["builtins"] as? [String: String] ?? [:]
        }
        if applyHost { applyHostInfo(j["host_info"] as? [String: Any], for: r, clearOnChange: clearOnChange) }
    }
    func save() {
        for s in stages where builtins[s.key] == nil {
            if s.done.hasPrefix("label="), s.done.dropFirst(6).trimmingCharacters(in: .whitespaces).isEmpty {
                status = "Enter a label name for stage \(s.key)."; return
            }
            if s.done.hasPrefix("check="), s.done.dropFirst(6).trimmingCharacters(in: .whitespaces).isEmpty {
                status = "Enter a CI check name for stage \(s.key)."; return
            }
        }
        for (k, v) in options where v.contains("`") || v.contains("\n") || v.contains("\r") {
            status = "Option \(k) can't contain backticks or newlines."; return
        }
        let r = repo
        let doc: [String: Any] = ["stages": stages.map { ["key": $0.key, "req": $0.req, "done": $0.done, "run": $0.run, "owner": $0.owner, "phase": $0.phase] },
                                  "options": options, "rules": rules, "checklist": checklist, "intro": intro]
        let s = String(decoding: (try? JSONSerialization.data(withJSONObject: doc)) ?? Data(), as: UTF8.self)
        let seq = nextHostSeq(for: r)
        call("lifecycle-set", r, s) { [weak self] j in
            guard let self else { return }
            if !isLocalHost(host) && !uitest && !selftest {
                ssh("\(pyCmd(on: "local"))/dash.py task lifecycle-set \(shellQuote(r))", on: "local", stdin: s)
            }
            self.got(j, for: r, clearOnChange: true, applyHost: self.hostSeqByRepo[r] == seq)
            if self.repo == r { self.status = "Saved." }
        }
    }
    func reset() {
        let r = repo
        let seq = nextHostSeq(for: r)
        call("lifecycle-reset", r) { [weak self] j in
            guard let self else { return }
            if !isLocalHost(host) && !uitest && !selftest {
                ssh("\(pyCmd(on: "local"))/dash.py task lifecycle-reset \(shellQuote(r))", on: "local")
            }
            self.got(j, for: r, clearOnChange: true, applyHost: self.hostSeqByRepo[r] == seq)
            if self.repo == r { self.status = "Back to the \(self.source) lifecycle." }
        }
    }
    func saveInstructions() { call("instructions-set", "-", instructions) { self.instructions = $0["text"] as? String ?? ""; self.status = "Saved." } }
    func gotNotes(_ j: [String: Any]) {
        notes = ((j["notes"] as? [[String: String]]) ?? []).map { (name: $0["name"] ?? "", text: $0["text"] ?? "") }
        if !notes.contains(where: { $0.name == note }) { note = ""; noteText = "" }
    }
    func saveNote() { call("memory-set", note, noteText) { self.gotNotes($0); self.status = "Saved \(self.note)." } }
    func deleteNote() { let n = note; call("memory-delete", n) { self.gotNotes($0); self.status = "Deleted \(n)." } }
}

// MARK: diag

/// The app's half of scripts/diag.py's trail (~/Library/Logs/Greenlight/app.jsonl, which diag.py reads when it's on
/// this machine): structured fields only. Strings must be short tokens (command names, enums); never text, names or paths.
enum Diag {
    static let url = URL(fileURLWithPath: unittest ? (NSTemporaryDirectory() + "greenlight-unittest-\(getpid())/app.jsonl")  // never the real trail
                         : uitest ? (greenlightHome + "/diag/app.jsonl") : (NSHomeDirectory() + "/Library/Logs/Greenlight/app.jsonl"))
    static let run = String(format: "%08x", UInt32.random(in: 0...UInt32.max))
    static let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "unknown"
    static let q = DispatchQueue(label: "greenlight.diag")
    static func event(_ kind: String, _ f: [String: Any] = [:]) {
        var e: [String: Any] = ["ts": (Date().timeIntervalSince1970 * 1000).rounded() / 1000, "v": v, "comp": "app", "run": run,
                                "pid": Int(getpid()), "kind": kind]
        var dropped = 0
        for (k, x) in f {
            if let s = x as? String { if s.range(of: #"^[A-Za-z0-9_.:+-]{1,64}$"#, options: .regularExpression) != nil { e[k] = s } else { dropped += 1 } }
            else if let n = x as? NSNumber { if n.doubleValue.isFinite { e[k] = n } else { dropped += 1 } }
            else if !(x is NSNull) { dropped += 1 }
        }
        if dropped > 0 { e["dropped"] = dropped }
        let rec = e
        q.async {
            guard JSONSerialization.isValidJSONObject(rec),
                  var d = try? JSONSerialization.data(withJSONObject: rec) else { return }
            d.append(10)
            let fm = FileManager.default, path = url.path
            try? fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let n = (try? fm.attributesOfItem(atPath: path))?[.size] as? Int, n > 5 << 20 {  // ponytail: size rotation, like diag.py
                for i in stride(from: 2, through: 1, by: -1) { try? fm.removeItem(atPath: "\(path).\(i + 1)"); try? fm.moveItem(atPath: "\(path).\(i)", toPath: "\(path).\(i + 1)") }
                try? fm.moveItem(atPath: path, toPath: "\(path).1")
            }
            let fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0o600)
            guard fd >= 0 else { return }
            _ = d.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
            close(fd)
        }
    }
    /// stderr's class (diag.py's names where they match), never its text.
    static func classify(_ s: String) -> String {
        for (re, c) in [("Permission denied|publickey|Host key verification", "auth"), ("timed out|Timeout", "timeout"),
                        ("Could not resolve|Connection refused|No route to host|Network is unreachable|Connection reset|closed by remote", "network"),
                        ("command not found|No such file", "not_found"), ("rate limit|429", "rate_limit"), ("Traceback", "traceback")]
        where s.range(of: re, options: [.regularExpression, .caseInsensitive]) != nil { return c }
        return s.isEmpty ? "none" : "other"
    }
    /// "dash.task" for "…/scripts/dash.py task …": allowlisted Greenlight scripts only, never dropped user filenames.
    static func verb(_ remote: String) -> String {
        guard let r = remote.range(of: #"scripts/(dash|chat|greenlight|auto|autonomy|decide|decisions|dwim|diag|orchestrate|adopt|cleanup|lifestage|blocking|audit)\.py( [a-z][\w-]{0,20})?"#, options: .regularExpression) else { return "shell" }
        return remote[r].dropFirst(8).replacingOccurrences(of: ".py", with: "").replacingOccurrences(of: " ", with: ".")
    }
    /// A launch marker a clean quit removes: still there at the next launch means the app crashed or was killed.
    static let marker = uitest
        ? (greenlightHome + "/diag/running-\(ProcessInfo.processInfo.processIdentifier)")
        : (NSHomeDirectory() + "/Library/Logs/Greenlight/running-\(Bundle.main.bundleIdentifier ?? "app")")  // per app: dev copies relaunch on their own
    static func launched(host: String) {
        if FileManager.default.fileExists(atPath: marker) { event("prev_crash") }
        try? FileManager.default.createDirectory(atPath: (marker as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: marker, contents: Data(v.utf8))
        let os = ProcessInfo.processInfo.operatingSystemVersion
        event("app_start", ["macos": "\(os.majorVersion).\(os.minorVersion)", "host_kind": host.isEmpty || host == "local" ? "local" : "remote"])
        Thread.detachNewThread { watch() }
    }
    /// Pings the main thread every second: a reply later than 2s is a `hang`, recorded once it's back
    /// (one that never ends is a kill, which the next launch's prev_crash shows). Every 5 min a `sample` of memory and CPU.
    static func watch() {
        let t0 = Date()
        var lastCPU = cpu(), lastAt = t0, n = 0
        while true {
            let sent = Date(), back = DispatchSemaphore(value: 0)
            DispatchQueue.main.async { back.signal() }
            if back.wait(timeout: .now() + 2) == .timedOut {
                back.wait()
                event("hang", ["secs": (Date().timeIntervalSince(sent) * 10).rounded() / 10])
            }
            Thread.sleep(forTimeInterval: 1)
            n += 1
            if n % 300 == 0 {
                let c = cpu(), wall = Date().timeIntervalSince(lastAt)
                event("sample", ["rss_mb": rss() >> 20, "cpu_pct": ((c - lastCPU) / max(wall, 1) * 1000).rounded() / 10,
                                 "up_mins": Int(Date().timeIntervalSince(t0) / 60)])
                lastCPU = c; lastAt = Date()
            }
        }
    }
    static func cpu() -> Double {
        var r = rusage(); getrusage(RUSAGE_SELF, &r)
        return Double(r.ru_utime.tv_sec + r.ru_stime.tv_sec) + Double(r.ru_utime.tv_usec + r.ru_stime.tv_usec) / 1e6
    }
    static func rss() -> Int {
        var info = mach_task_basic_info()
        var count = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), $0, &count) }
        }
        return kr == KERN_SUCCESS ? Int(info.resident_size) : 0
    }
}

// MARK: ssh

/// Test seam for `ssh(...)`: when non-nil (only in `--unittest`), intercepts commands without spawning real processes.
var sshOverride: ((_ remote: String, _ host: String, _ stdin: String, _ onLine: @escaping (String) -> Void, _ onExit: @escaping (Int32) -> Void) -> Process?)?

/// Streams a pipe's read handle on a serial queue via non-blocking Darwin.read (never FileHandle's ObjC-exception
/// availableData/readDataToEndOfFile); finish() or cancel() stops reading and closes h once any in-flight read has returned.
final class PipeReader {
    private let src: DispatchSourceRead
    private let done = DispatchGroup()
    init(_ h: FileHandle, on q: DispatchQueue, onData: @escaping (Data) -> Void) {
        let fd = h.fileDescriptor
        let fl = fcntl(fd, F_GETFL)
        if fl >= 0 { _ = fcntl(fd, F_SETFL, fl | O_NONBLOCK) }
        src = DispatchSource.makeReadSource(fileDescriptor: fd, queue: q)
        done.enter()
        let g = done
        src.setEventHandler { [weak self] in
            guard let self, !self.src.isCancelled else { return }
            let want = max(Int(min(self.src.data, 65536)), 4096)
            var chunk = [UInt8](repeating: 0, count: want)
            let n = Darwin.read(fd, &chunk, chunk.count)
            if n > 0 { onData(Data(chunk[0..<n])); return }
            if n < 0 && (errno == EINTR || errno == EAGAIN) { return }
            self.src.cancel()
        }
        src.setCancelHandler {
            try? h.close()
            g.leave()
        }
        src.resume()
    }
    deinit { src.cancel() }
    /// Waits until `deadline` for EOF, then cancels and waits for any in-flight read and close() to finish.
    func finish(until deadline: DispatchTime) {
        if done.wait(timeout: deadline) == .timedOut { src.cancel() }
        done.wait()
    }
    /// Waits up to `secs` for EOF (<= 0 cancels immediately), then waits for close() to finish.
    func finish(within secs: Double = 0.3) {
        if secs <= 0 { src.cancel(); done.wait(); return }
        finish(until: .now() + secs)
    }
    /// Stops reading without blocking the caller; cancelHandler closes h on its queue.
    func cancel() { src.cancel() }
}

/// Runs <remote> where greenlight lives (remoteArgv), calling onLine (main thread) per stdout line and onExit when it ends.
@discardableResult
func ssh(_ remote: String, on h: String = host, stdin: String = "", onLine: @escaping (String) -> Void = { _ in },
         onExit: @escaping (Int32) -> Void = { _ in }) -> Process {
    if let over = sshOverride, let p = over(remote, h, stdin, onLine, onExit) { return p }
    let p = Process()
    let (exe, args) = remoteArgv(remote, on: h)
    p.executableURL = URL(fileURLWithPath: exe)
    p.arguments = args
    let inPipe = Pipe(), out = Pipe()
    p.standardInput = inPipe
    p.standardOutput = out
    final class Buf { var s = Data() }  // only mutated on serial ioQ, then read after finish()
    let ioQ = DispatchQueue(label: "greenlight.ssh.io")
    let buf = Buf(), err = Pipe(), tail = Buf(), t0 = Date()
    // stderr's last 4 KB, in memory only: classified into the diag trail, never written
    p.standardError = err
    let errReader = PipeReader(err.fileHandleForReading, on: ioQ) { d in
        tail.s.append(d)
        if tail.s.count > 4096 { tail.s = Data(tail.s.suffix(4096)) }
    }
    let outReader = PipeReader(out.fileHandleForReading, on: ioQ) { d in
        var lines = [String]()
        buf.s.append(d)
        while let nl = buf.s.firstIndex(of: 10) {
            lines.append(String(decoding: buf.s[buf.s.startIndex..<nl], as: UTF8.self))
            buf.s.removeSubrange(buf.s.startIndex...nl)
        }
        if !lines.isEmpty { DispatchQueue.main.async { for l in lines { onLine(l) } } }
    }
    p.terminationHandler = { p in
        let rc = p.terminationStatus
        if rc != 0 {  // a fast failure's stderr may not have been read yet: drain both pipes, but never wait long on a pipe a daemon kept
            let deadline = DispatchTime.now() + 0.3
            errReader.finish(until: deadline)
            Diag.event("ssh_exit", ["verb": Diag.verb(remote), "rc": Int(rc), "secs": (Date().timeIntervalSince(t0) * 1000).rounded() / 1000,
                                    "msg_class": Diag.classify(String(decoding: tail.s, as: UTF8.self))])
            outReader.finish(until: deadline)
        } else {
            Diag.event("ssh_exit", ["verb": Diag.verb(remote), "rc": 0, "secs": (Date().timeIntervalSince(t0) * 1000).rounded() / 1000])
            errReader.finish(within: 0)
            outReader.finish(within: 0.3)
        }
        let leftover = buf.s; buf.s.removeAll()
        DispatchQueue.main.async {
            for l in String(decoding: leftover, as: UTF8.self).split(separator: "\n") { onLine(String(l)) }
            onExit(rc)
        }
    }
    do { try p.run() } catch {
        outReader.cancel(); errReader.cancel()
        p.terminationHandler = nil
        try? inPipe.fileHandleForWriting.close()
        Diag.event("ssh_exit", ["verb": Diag.verb(remote), "rc": -1, "msg_class": "spawn"])
        DispatchQueue.main.async { onExit(-1) }
        return p
    }
    let w = inPipe.fileHandleForWriting, d = Data(stdin.utf8)
    // off the main thread: a pipe holds 16 KB, and ssh reads stdin only once it's connected (attached files are MBs).
    // The throwing write: a dead ssh's EPIPE isn't a crash
    DispatchQueue.global(qos: .userInitiated).async { try? w.write(contentsOf: d); try? w.close() }
    return p
}

