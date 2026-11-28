import AppKit
import Carbon
import Foundation
import Security
import SwiftUI

// MARK: core, config, inbox, and keychain unit tests

/// Deterministic offline Core/Config/Models/DWIM/Projects/Keychain unit tests.
func runCoreOfflineUnitTests(check: (String, Bool) -> Void) {
    let claudeErr = "There's an issue with the selected model (gemini-4-neon-high). It may not exist or you may not have access to it. Run --model to pick a different model."
    let stale = staleBackendNote(model: "gemini-4-neon-high", error: claudeErr, host: "devbox", code: "~/.claude/skills/greenlight")
    check("stale host: a Gemini model sent to Claude Code by an old chat.py says to update the host, not that the model is missing: \(stale ?? "nil")",
          stale?.contains("Greenlight on devbox is older than this app") == true && stale?.contains("~/.claude/skills/greenlight") == true
          && staleBackendNote(model: "claude-opus-5-5", error: claudeErr, host: "devbox", code: "x") == nil
          && staleBackendNote(model: "gemini-4-neon-high", error: "quota exhausted", host: "devbox", code: "x") == nil)
    let old = Date(timeIntervalSinceNow: -2 * 86400)
    let swept = staleTestDomains([("dev.greenlight.uitest.111.plist", old), ("dev.greenlight.unittest.222.plist", Date()),
                                  ("dev.greenlight.uitest.333.plist", old), ("dev.greenlight.uitest.s0dfd05a6.plist", old), ("dev.greenlight.uitest.sbbd5e53b.plist", Date()),
                                  ("Greenlight-00c56d0114dede37.plist", old), ("Greenlight-0eeb385cf124934f.plist", Date()),
                                  ("dev.greenlight.app.plist", old), ("dev.greenlight.app.l1-multi-window.plist", old)],
                                 alive: { $0 == 333 })
    check("test defaults sweep: dead pids' suites and day-old binary and kept domains go; live, fresh, app and dev-copy ones stay: \(swept)",
          swept == ["dev.greenlight.uitest.111", "dev.greenlight.unittest.222", "dev.greenlight.uitest.s0dfd05a6", "Greenlight-00c56d0114dede37"])
    let oldSave = #"{"role":"greenlight","text":"hi","steps":[],"duration":1,"options":[],"failed":false}"#
    let loaded = (try? JSONDecoder().decode(Msg.Saved.self, from: Data(oldSave.utf8))).map(Msg.init)
    let am = Msg(.greenlight, "denied"); am.approvals = [Approval(id: "0123456789ab", tool: "Bash", what: "orca host list")]
    let back = (try? JSONEncoder().encode(am.saved)).flatMap { try? JSONDecoder().decode(Msg.Saved.self, from: $0) }.map(Msg.init)
    let dm = Msg(.greenlight, "denied"); dm.approvals = am.approvals; dm.decided = "denied"
    let backDenied = (try? JSONEncoder().encode(dm.saved)).flatMap { try? JSONDecoder().decode(Msg.Saved.self, from: $0) }.map(Msg.init)
    check("approvals: a transcript saved before them loads; pending comes back expired while denied persists across relaunch",
          loaded?.approvals.isEmpty == true && loaded?.decided == nil && back?.approvals == am.approvals && back?.decided == "expired" && backDenied?.decided == "denied")
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
    check("Chief's [open: URL] opens a forge page and isn't shown", staged("Here's the PR.\n[open: https://github.com/acme/widgets/pull/7]")
          == Staged(text: "Here's the PR.", stage: nil, open: URL(string: "https://github.com/acme/widgets/pull/7"))
          && staged("Opening [open: https://gitl").text == "Opening" && staged("Opening [open: https://gitl").open == nil  // still streaming
          && staged("[open: <https://gitlab.com/acme/widgets/-/merge_requests/3>]\n[open: https://github.com/x/y]").open == URL(string: "https://gitlab.com/acme/widgets/-/merge_requests/3"))
    check("[open: …] only sends the browser to http(s) on a forge host",
          forgeURL("http://www.github.com/a/b/issues/1") != nil && forgeURL("HTTPS://GitLab.com/a/b") != nil
          && ["javascript:alert(1)", "file:///etc/passwd", "ssh://github.com/a/b", "https://evil.com/github.com", "https://github.com.evil.com/a",
              "https://me" + "@github.com/a", "https://github.com" + "@evil.com/a", "https://github.com:8443/a", "https://gist.github.com/a", "github.com/a/b", ""]
              .allSatisfy { forgeURL($0) == nil }
          && staged("No.\n[open: https://evil.com/x]") == Staged(text: "No.", stage: nil))
    check("[open: [!3](URL)]: a Markdown link inside opens its URL and leaves nothing behind, streaming or done",
          staged("Here.\n[open: [!3](https://gitlab.com/acme/widgets/-/merge_requests/3)]\nMore.")
              == Staged(text: "Here.\n\nMore.", stage: nil, open: URL(string: "https://gitlab.com/acme/widgets/-/merge_requests/3"))
          && staged("Here. [open: [!3](https://gitl").text == "Here." && staged("Here. [open: [!3](https://gitlab.com/a/b)").text == "Here."
          && staged("[open: [x](https://evil.com/a)]").open == nil)
    check("[open: …] cut off before its ] is hidden but never opens", staged("Looking.\n[open: https://github.com/acme/widgets/pull/7") == Staged(text: "Looking.", stage: nil)
          && staged("[open: [!3](https://github.com/acme/widgets/pull/3)").open == nil && staged("[open: https://github.com/acme/widgets/pull/3 [x]").open == nil)
    let manyLinks = "Opened: [open: " + (1...12).map { "[!\($0)](https://gitlab.com/acme/widgets/-/merge_requests/\($0))" }.joined(separator: ", ") + "\nMore.", reT0 = Date()
    check("[open: …] never backtracks: a dozen links, unclosed, parse at once; a half-typed one is hidden while it streams",
          staged(manyLinks).open == nil && Date().timeIntervalSince(reT0) < 0.5 && staged("Here. [open: [").text == "Here." && staged("Here. [open: [!3").text == "Here.")
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
    check("terminal: hasSavedTermTabs detects non-empty saved layouts for launch preload",
          !hasSavedTermTabs(nil)
          && !hasSavedTermTabs(Data("not json".utf8))
          && !hasSavedTermTabs(Data(#"{"tabs":[]}"#.utf8))
          && hasSavedTermTabs(Data(#"{"tabs":[{"ws":"/w/a","tree":{"s":"mc-1"}}]}"#.utf8)))
    let mainScr = NSRect(x: 0, y: 0, width: 1440, height: 875), right = NSRect(x: 1440, y: 0, width: 2560, height: 1415)
    check("windows: a restored frame stays on a screen that's there, and comes back from one that's gone",
          fitOnScreen(NSRect(x: 1600, y: 200, width: 1280, height: 800), screens: [mainScr, right]) == NSRect(x: 1600, y: 200, width: 1280, height: 800)
          && fitOnScreen(NSRect(x: 1600, y: 200, width: 1280, height: 800), screens: [mainScr]) == NSRect(x: 80, y: 37.5, width: 1280, height: 800)
          && fitOnScreen(NSRect(x: -5000, y: 0, width: 3000, height: 2000), screens: [mainScr]) == mainScr
          && fitOnScreen(NSRect(x: 1400, y: 100, width: 800, height: 600), screens: [mainScr]) == NSRect(x: 320, y: 137.5, width: 800, height: 600)  // 40pt of its bar on screen
          && fitOnScreen(NSRect(x: 1300, y: 100, width: 800, height: 600), screens: [mainScr]) == NSRect(x: 1300, y: 100, width: 800, height: 600)
          && fitOnScreen(NSRect(x: 100, y: 870, width: 800, height: 600), screens: [mainScr]) != NSRect(x: 100, y: 870, width: 800, height: 600)  // bar above the top
          && fitOnScreen(NSRect(x: 5, y: 5, width: 10, height: 10), screens: []) == NSRect(x: 5, y: 5, width: 10, height: 10))
    check("windows: a saved frame is read as saved, not rescaled onto another screen",
          savedFrame("70 90 1300 900 0 0 1728 1084 ") == NSRect(x: 70, y: 90, width: 1300, height: 900)
          && savedFrame("-960 -578 1000 700 0 0 1728 1084") == NSRect(x: -960, y: -578, width: 1000, height: 700)
          && savedFrame(nil) == nil && savedFrame("") == nil && savedFrame("1 2 3") == nil && savedFrame("0 0 0 0 0 0 1 1") == nil)
    check("windows: a new window cascades off the one you're in, at least its minimum and inside that screen",
          cascadeFrame(NSRect(x: 100, y: 100, width: 800, height: 600), min: NSSize(width: 480, height: 520), in: mainScr) == NSRect(x: 130, y: 70, width: 800, height: 600)
          && cascadeFrame(NSRect(x: 100, y: 300, width: 900, height: 420), min: NSSize(width: 480, height: 520), in: mainScr) == NSRect(x: 130, y: 170, width: 900, height: 520)  // a view window's size: grown to L1's
          && cascadeFrame(mainScr, min: NSSize(width: 480, height: 520), in: mainScr) == mainScr  // from a full-screen one: stays on it
          && cascadeFrame(NSRect(x: 1700, y: 300, width: 800, height: 600), min: .zero, in: right) == NSRect(x: 1730, y: 270, width: 800, height: 600)  // the second monitor's
          && cascadeFrame(NSRect(x: 3700, y: 10, width: 800, height: 600), min: .zero, in: right) == NSRect(x: 3200, y: 0, width: 800, height: 600)
          && cascadeFrame(NSRect(x: 0, y: 0, width: 10, height: 10), min: NSSize(width: 20, height: 20), in: nil) == NSRect(x: 30, y: -40, width: 20, height: 20))
    check("windows: a floor switch hands the next floor the same frame, grown only to its minimum from the top-left",
          sharedFrame(NSRect(x: 100, y: 100, width: 1300, height: 900), min: NSSize(width: 801, height: 520)) == NSRect(x: 100, y: 100, width: 1300, height: 900)
          && sharedFrame(NSRect(x: 1700, y: 300, width: 900, height: 600), min: NSSize(width: 801, height: 520)) == NSRect(x: 1700, y: 300, width: 900, height: 600)  // a second monitor's: stays there
          && sharedFrame(NSRect(x: 100, y: 300, width: 600, height: 400), min: NSSize(width: 801, height: 520)) == NSRect(x: 100, y: 180, width: 801, height: 520)  // the title bar stays put
          && sharedFrame(mainScr, min: NSSize(width: 801, height: 520)) == mainScr
          && sharedFrame(NSRect(x: 1400, y: 30, width: 300, height: 160), min: NSSize(width: 801, height: 520), in: mainScr) == NSRect(x: mainScr.maxX - 801, y: mainScr.minY, width: 801, height: 520)  // grown at a corner: back on its screen
          && sharedFrame(NSRect(x: 1400, y: 30, width: 900, height: 600), min: NSSize(width: 801, height: 520), in: mainScr) == NSRect(x: 1400, y: 30, width: 900, height: 600))  // not grown: left where you put it
    check("dwim: only words dwim.py knows leave the Chief path", dwimCandidate("park the two auth ones") && dwimCandidate("Kick off CI fixes on the red ones")
          && dwimCandidate("hold #42") && dwimCandidate("don't merge #42") && dwimCandidate("unpark billing") && dwimCandidate("fix the merge conflicts on search")
          && dwimCandidate("fix for CI #42") && dwimCandidate("put #42 on ice")
          && !dwimCandidate("what's on fire?") && !dwimCandidate("merge #42") && !dwimCandidate("parking lot ideas") && !dwimCandidate("")
          && !dwimCandidate("park\nthe auth ones") && voiceCommand("Undo that.") == .undo && voiceCommand("undo #42") == nil && voiceCommand("cancel #42") == nil && voiceCommand("undo the deploy") == nil)
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
    check("config: runsScripts: workspaces, the home and @local can be asked how a turn ended; another @host can't",
          runsScripts("/w/ok") && runsScripts("") && runsScripts("@local") && runsScripts("@" + host) && (host == "devbox-other" || !runsScripts("@devbox-other")) && (host == "localhost" || !runsScripts("@localhost")))
    do {  // each host's pane states are published once: the other fetch's publish doesn't replay them
        var srv: [String: Any]? = ["trees": [["path": "/srv/a"]], "panes": ["mc-srv": ["state": "working"]]]
        var mac: [String: Any]? = ["trees": [["path": "/mac/a"]], "panes": ["mc-mac": ["state": "working"]]]
        let first = treesToPublish(server: &srv, local: &mac, merge: true, primaryHost: "devbox")
        srv = ["trees": [["path": "/srv/a"]], "panes": ["mc-srv": ["state": "done"]]]  // the next refresh: the server answers first
        let second = treesToPublish(server: &srv, local: &mac, merge: true, primaryHost: "devbox")
        let panesOf = { (o: [String: Any]?) in ((o?["panes"] as? [String: Any]) ?? [:]).keys.sorted() }
        var only: [String: Any]? = ["trees": [], "panes": ["mc-1": ["state": "done"]]], none: [String: Any]? = nil
        let single = treesToPublish(server: &only, local: &none, merge: false, primaryHost: "local")
        check("config: treesToPublish sends each host's pane states once, never the other host's stale ones; trees stay merged",
              panesOf(first) == ["mc-mac", "mc-srv"] && panesOf(second) == ["mc-srv"]
              && ((second?["panes"] as? [String: Any])?["mc-srv"] as? [String: Any])?["state"] as? String == "done"
              && ((second?["trees"] as? [[String: Any]]) ?? []).count == 2 && panesOf(single) == ["mc-1"] && only?["panes"] == nil
              && panesOf(treesToPublish(server: &srv, local: &mac, merge: true, primaryHost: "devbox")) == [])  // a failed fetch republishes none
        let t0 = Date(), tree: [String: Any] = ["trees": [["path": "/srv/a"]], "panes": ["mc-srv": ["state": "done"]]]
        var r = PaneReplay()
        r.add(["mc-srv": ["state": "working"]], now: t0); r.add(["mc-mac": ["state": "working"]], now: t0.addingTimeInterval(1))
        let both = panesOf(r.replay(tree, now: t0.addingTimeInterval(3))), old = panesOf(r.replay(tree, now: t0.addingTimeInterval(40)))
        r.start()  // the next refresh began; a page loading before it publishes gets the trees only
        let inFlight = r.replay(tree, now: t0.addingTimeInterval(41))
        r.add(["mc-mac": ["state": "working"]], now: t0.addingTimeInterval(50)); r.add(nil, now: t0.addingTimeInterval(54)); r.add([String: Any](), now: t0.addingTimeInterval(55))
        let afterEmpty = panesOf(r.replay(tree, now: t0.addingTimeInterval(56)))  // 6 s after the states came: empty publishes don't refresh them
        r.add(["mc-srv": ["state": "done"]], now: t0.addingTimeInterval(57))  // the server's answer, 7 s after the Mac's
        let lateHost = panesOf(r.replay(tree, now: t0.addingTimeInterval(57)))  // dated by the Mac's: nothing stale rides along
        check("config: PaneReplay gives a loading page both hosts' fresh states, never stale ones or the last refresh's",
              both == ["mc-mac", "mc-srv"] && old == [] && panesOf(inFlight) == [] && ((inFlight?["trees"] as? [[String: Any]]) ?? []).count == 1
              && afterEmpty == [] && lateHost == [] && r.replay(nil) == nil)
    }
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
          && harnessOf("gpt-6-luna") == "codex" && harnessOf("o4-mini") == "codex"
          && harnessOf("gpt-oss-120b-medium") == "agy" && harnessOf("agy:claude-opus-5-5-high") == "agy"
          && routeModel("agy:claude-opus-5-5-high").harness == "agy" && routeModel("agy:claude-opus-5-5-high").id == "claude-opus-5-5-high"
          && routeModel("mystery-7").harness == "claude" && routeModel("mystery-7").id == "mystery-7"
          && modelSupportsEffort("claude-opus-5-5") && modelSupportsEffort("codex") && modelSupportsEffort("gpt-6-luna")
          && modelSupportsEffort("agy") && !modelSupportsEffort("gemini-4-neon-high")
          && llmInvocation("hi", model: "claude-haiku-5-5").args.contains("--tools")
          && llmInvocation("hi", model: "gpt-6-luna", effort: "max").args.contains("model_reasoning_effort=high")
          && llmInvocation("hi", model: "gpt-6-luna").args.contains("sandbox_mode=read-only")
          && llmInvocation("hi", model: "agy:claude-sonnet-5-5-low").harness == "agy"
          && parseAgyStreamResult("{\"event\":\"result\",\"result\":{\"status\":\"SUCCESS\",\"response\":\"ok\"}}") == "ok"
          && parseAgyStreamResult("{\"event\":\"result\",\"result\":{\"status\":\"ERROR\",\"response\":\"no\"}}") == ""
          && effortName("xhigh") == "Extra high" && effortName("low") == "Low"
          && !fontChoices.isEmpty && !models.isEmpty && !accents.isEmpty)
    _ = (StatusInk.needs, StatusInk.yourMove, StatusInk.working, StatusInk.crashed, StatusInk.queued, StatusInk.done,
         StatusInk.needsInk, StatusInk.crashedInk, StatusInk.workingInk, bg, fg, card, surface, line, edge, dim, sub, bubble, accent,
         sys(size: 13), sys(size: 13, design: .monospaced), sys(size: 13, face: "Menlo"), chatFace(size: 13))
    check("config: three accents (§2.3); an old swatch migrates to the nearest, the theme's own to Signal",
          accents.map(\.id) == ["signal", "teal", "graphite"] && migratedAccent("2AC3DE") == "teal" && migratedAccent("7aa2f7") == "teal"
          && migratedAccent("BD93F9") == "teal" && migratedAccent("9ECE6A") == "signal" && migratedAccent("F5A524") == "signal" && migratedAccent(nil) == "signal")
    check("config: appearance is one setting for every window: System leaves NSApp's own, Light and Dark override it",
          App.appearances.map(\.0) == ["auto", "light", "dark"] && Floors.levels.map(\.1) == ["Center", "Basement", "Pipe"])
    ud.set("gemini-4-neon", forKey: "testSavedModel")
    check("config: savedModel migrates legacy gemini-4-neon id", savedModel("testSavedModel", fallback: "codex") == "gemini-4-neon-medium")
    do {
        // ModelManufacturer, baked-effort splitting, modelIcon, nearestEffort, and buildModelCatalog
        check("config: ModelManufacturer.of classifies Anthropic, OpenAI, Google, and Other models",
              ModelManufacturer.of("claude-opus-5-5") == .anthropic
              && ModelManufacturer.of("claude") == .anthropic
              && ModelManufacturer.of("agy:claude-opus-5-5-high") == .anthropic
              && ModelManufacturer.of("gpt-6.2-sol") == .openai
              && ModelManufacturer.of("o4-mini") == .openai
              && ModelManufacturer.of("codex") == .openai
              && ModelManufacturer.of("codex:astra-9") == .openai
              && ModelManufacturer.of("gpt-oss-120b-medium") == .openai
              && ModelManufacturer.of("gemini-4-neon-high") == .google
              && ModelManufacturer.of("agy") == .google
              && ModelManufacturer.of("gemini") == .google
              && ModelManufacturer.of("opencode") == .other
              && ModelManufacturer.of("pi") == .other
              && ModelManufacturer.of("mystery-7") == .other)

        let sp1 = splitBakedEffort(id: "gemini-4-neon-high", name: "Gemini 4 Neon (High)")
        let sp2 = splitBakedEffort(id: "gemini-4-neon-xhigh", name: "Gemini 4 Neon · Extra high")
        let sp3 = splitBakedEffort(id: "agy:claude-opus-5-5-low", name: "Claude Opus 5.5 (Low) · agy")
        let sp4 = splitBakedEffort(id: "claude-opus-5-5", name: "Opus 5.5")
        let sp5 = splitBakedEffort(id: "gemini-pro-orchestrator-neon", name: "Orchestrator (Neon)")
        check("config: splitBakedEffort collapses agy baked-effort slugs and strips effort labels",
              sp1.baseID == "gemini-4-neon" && sp1.baseName == "Gemini 4 Neon" && sp1.bakedEffort == "high"
              && sp2.baseID == "gemini-4-neon" && sp2.baseName == "Gemini 4 Neon" && sp2.bakedEffort == "xhigh"
              && sp3.baseID == "agy:claude-opus-5-5" && sp3.baseName == "Claude Opus 5.5 · agy" && sp3.bakedEffort == "low"
              && sp4.baseID == "claude-opus-5-5" && sp4.baseName == "Opus 5.5" && sp4.bakedEffort == nil
              && sp5.baseID == "gemini-pro-orchestrator-neon" && sp5.bakedEffort == nil)

        let iconSamples: [(String, String, String)] = [
            ("claude-opus-5-5", "claude", "sparkles"),
            ("claude-sonnet-5-5", "claude", "spark"),
            ("claude-haiku-5-5", "claude", "bolt"),
            ("claude-fable-5-2", "claude", "wand"),
            ("gpt-6.2-sol", "codex", "sparkles"),
            ("gpt-6-luna", "codex", "bolt"),
            ("o4-mini", "codex", "bolt"),
            ("gpt-daybreak-blue-latest", "codex", "wand"),
            ("gpt-oss-120b", "agy", "branch"),
            ("gemini-4-neon", "agy", "sparkles"),
            ("gemini-4-carbon", "agy", "spark"),
            ("gemini-3.8-flash", "agy", "bolt"),
            ("gemini-pro-orchestrator-neon", "agy", "grid"),
            ("codex", "codex", "terminal"),
            ("agy", "agy", "terminal"),
            ("opencode", "opencode", "terminal"),
            ("claude-unknown-1", "claude", "claude"),
            ("codex:custom-1", "codex", "openai"),
            ("agy:custom-1", "agy", "agy"),
        ]
        check("config: modelIcon maps every model tier and manufacturer to a valid DS icon",
              iconSamples.allSatisfy { modelIcon(for: $0.0, harness: $0.1) == $0.2 && DSIcons.all[$0.2] != nil }
              && ModelManufacturer.allCases.allSatisfy { DSIcons.all[$0.icon] != nil && !$0.name.isEmpty })

        check("config: nearestEffort matches exact, clamps down, falls back to lowest, and handles unknown levels",
              nearestEffort("high", in: ["low", "medium", "high"]) == "high"
              && nearestEffort("max", in: ["low", "medium", "high", "xhigh"]) == "xhigh"
              && nearestEffort("xhigh", in: ["low", "medium", "high"]) == "high"
              && nearestEffort("low", in: ["medium", "high"]) == "medium"
              && nearestEffort("unknown", in: ["low", "medium", "high"]) == "medium"
              && nearestEffort("high", in: ["medium"]) == "medium")

        let sampleRaw: [(String, String)] = [
            ("Opus 5.5", "claude-opus-5-5"),
            ("Haiku 5.5", "claude-haiku-5-5"),
            ("Claude Opus 5.5 (Low) · agy", "agy:claude-opus-5-5-low"),
            ("Claude Opus 5.5 (High) · agy", "agy:claude-opus-5-5-high"),
            ("GPT-6.2-Sol", "gpt-6.2-sol"),
            ("Codex default", "codex"),
            ("GPT-OSS 120B (Medium) · agy", "gpt-oss-120b-medium"),
            ("Gemini 4 Neon (High)", "gemini-4-neon-high"),
            ("Gemini 4 Neon (Low)", "gemini-4-neon-low"),
            ("Gemini 4 Neon (XHigh)", "gemini-4-neon-xhigh"),
            ("Gemini 4 Neon (Medium)", "gemini-4-neon-medium"),
            ("Gemini CLI", "gemini"),
            ("OpenCode", "opencode"),
        ]
        let fullCat = buildModelCatalog(from: sampleRaw, current: "gemini-4-neon-high")
        let writerCat = buildModelCatalog(from: sampleRaw, current: "claude-haiku-5-5") { writerHarnesses.contains(harnessOf($0)) }
        let anthropic = fullCat.first { $0.manufacturer == .anthropic }
        let openai = fullCat.first { $0.manufacturer == .openai }
        let google = fullCat.first { $0.manufacturer == .google }
        let other = fullCat.first { $0.manufacturer == .other }
        let neon = google?.choices.first { $0.id == "gemini-4-neon" }
        let agyOpus = anthropic?.choices.first { $0.id == "agy:claude-opus-5-5" }
        let oss = openai?.choices.first { $0.id == "gpt-oss-120b" }
        let gemCli = google?.choices.first { $0.id == "gemini" }
        let activeNeon = activeModelSelection(in: fullCat, modelId: "gemini-4-neon-high")
        let customCat = buildModelCatalog(from: sampleRaw, current: "mystery-custom")
        let neonMax = neon?.resolve(desiredEffort: "max")
        let neonLow = neon?.resolve(desiredEffort: "low")
        check("config: buildModelCatalog groups by manufacturer, collapses baked-effort variants, and preserves current selection",
              fullCat.map(\.manufacturer) == [.anthropic, .openai, .google, .other]
              && writerCat.map(\.manufacturer) == [.anthropic, .openai, .google]
              && anthropic?.choices.map(\.name) == ["Opus 5.5", "Opus 5.5 · agy", "Haiku 5.5"]  // most expensive first
              && agyOpus?.supportedEfforts == ["low", "high"]
              && neon?.supportedEfforts == ["low", "medium", "high", "xhigh"]
              && neon?.supportsEffortSlider == true
              && neonMax?.modelId == "gemini-4-neon-xhigh" && neonMax?.effort == "xhigh"
              && neonLow?.modelId == "gemini-4-neon-low" && neonLow?.effort == "low"
              && oss?.supportsEffortSlider == false && oss?.fixedEffort == "medium"
              && gemCli?.supportedEfforts.isEmpty == true && gemCli?.fixedEffort == nil
              && other?.choices.map(\.id) == ["opencode"]
              && activeNeon?.group.manufacturer == .google && activeNeon?.choice.id == "gemini-4-neon"
              && neon.map { currentModelEffort(for: $0, modelId: "gemini-4-neon-high", storedEffort: "low") } == "high"
              && customCat.first { $0.manufacturer == .other }?.choices.contains { $0.id == "mystery-custom" } == true)

        // Cost order: Claude by list price, other providers by codename tier, a harness's own default last.
        let costRaw: [(String, String)] = [
            ("Haiku 5.5", "claude-haiku-5-5"), ("Sonnet 5.5", "claude-sonnet-5-5"), ("Opus 5.5", "claude-opus-5-5"),
            ("Fable 5.2", "claude-fable-5-2"), ("Fable 5.1", "claude-fable-5-1"),
            ("Codex default", "codex"), ("GPT-6-Luna", "gpt-6-luna"), ("GPT-6.2-Astra", "gpt-6.2-astra"),
            ("GPT-6.2-Sol", "gpt-6.2-sol"), ("GPT-6.1-Sol", "gpt-6.1-sol"), ("GPT-OSS 120B (Medium) · agy", "gpt-oss-120b-medium"),
            ("Gemini 3.8 Flash (Medium)", "gemini-3.8-flash-medium"), ("Gemini 4 Argon (Medium)", "gemini-4-argon-medium"),
            ("Antigravity default", "agy"), ("Gemini 4 Neon (High)", "gemini-4-neon-high"),
        ]
        let costCat = buildModelCatalog(from: costRaw, current: "claude-opus-5-5")
        let ids = { (m: ModelManufacturer) in costCat.first { $0.manufacturer == m }?.choices.map(\.id) ?? [] }
        check("config: ModelSelector lists each provider's models most expensive first",
              ids(.anthropic) == ["claude-fable-5-1", "claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
              && ids(.openai) == ["gpt-6.2-sol", "gpt-6.1-sol", "gpt-6.2-astra", "gpt-6-luna", "gpt-oss-120b", "codex"]
              && ids(.google) == ["gemini-4-neon", "gemini-4-argon", "gemini-3.8-flash", "agy"]
              && modelCostRank("agy:claude-opus-5-5") == modelCostRank("claude-opus-5-5")
              && modelCostRank("claude-mystery-9") > modelCostRank("claude-haiku-5-5"))

        // Favorites: a model and its effort, toggled by the star, saved to ud, labelled with the effort.
        let favNeon = neon.map { modelFavorite(for: $0, desiredEffort: "max") }
        let favCli = gemCli.map { modelFavorite(for: $0, desiredEffort: "high") }
        let favOpus = ModelFavorite(model: "claude-opus-5-5", effort: "high")
        let once = togglingFavorite(favOpus, in: [])
        if let d = try? JSONEncoder().encode([favOpus, favNeon].compactMap { $0 }) { ud.set(d, forKey: "testModelFavorites") }
        ud.set(Data("not json".utf8), forKey: "testBadFavorites")
        check("config: favorites remember model and effort, toggle, persist, and label with the effort",
              favNeon == ModelFavorite(model: "gemini-4-neon-xhigh", effort: "xhigh")
              && favCli == ModelFavorite(model: "gemini", effort: "")
              && once == [favOpus] && togglingFavorite(favOpus, in: once).isEmpty
              && togglingFavorite(ModelFavorite(model: "claude-opus-5-5", effort: "low"), in: once).count == 2
              && savedModelFavorites("testModelFavorites") == [favOpus, ModelFavorite(model: "gemini-4-neon-xhigh", effort: "xhigh")]
              && savedModelFavorites("testBadFavorites").isEmpty && savedModelFavorites("testUnsetFavorites").isEmpty
              && modelFavoriteLabel(favOpus, in: fullCat) == "Opus 5.5 · High"
              && modelFavoriteLabel(ModelFavorite(model: "gemini-4-neon-xhigh", effort: "xhigh"), in: fullCat) == "Gemini 4 Neon · Extra high"
              && modelFavoriteLabel(ModelFavorite(model: "gemini", effort: ""), in: fullCat) == "Gemini CLI"
              && modelFavoriteLabel(ModelFavorite(model: "gemini-4-neon-xhigh", effort: "xhigh"), in: []) == "Gemini 4 Neon · Extra high")

        let selCore = Core()
        selCore.modelFavorites = [favOpus]
        let favPersisted = savedModelFavorites() == [favOpus]
        selCore.modelFavorites = []
        check("config: Core.modelFavorites persists to ud", favPersisted && savedModelFavorites().isEmpty)
        selCore.termNameEffort = "high"
        let selView = ModelSelector(c: selCore, title: "Test model", hint: "Hint", model: Binding(get: { selCore.model }, set: { selCore.model = $0 }), effort: Binding(get: { selCore.effort }, set: { selCore.effort = $0 }), fallbacks: Binding(get: { selCore.modelFallbacks }, set: { selCore.modelFallbacks = $0 }), tier: .capable)
        _ = selView
        check("config: Core.termNameEffort persists to ud and effortNotes covers every effort level",
              ud.string(forKey: "termNameEffort") == "high"
              && efforts.allSatisfy { !(effortNotes[$0] ?? "").isEmpty })

        // Per-provider model fallbacks: auto defaults per tier, custom order/dedupe, primary switch retention, CLI args, usage cap parsing.
        let fbRaw: [(String, String)] = [
            ("Opus 5.5", "claude-opus-5-5"), ("Haiku 5.5", "claude-haiku-5-5"),
            ("Gemini 4 Neon (High)", "gemini-4-neon-high"), ("Gemini 3.8 Flash (Medium)", "gemini-3.8-flash-medium"),
            ("GPT-6.2-Sol", "gpt-6.2-sol"), ("GPT-6-Luna", "gpt-6-luna"), ("Codex default", "codex"),
        ]
        let autoCapable = resolvedModelFallbacks(saved: nil, primaryModel: "claude-opus-5-5", rawOptions: fbRaw, tier: .capable)
        let autoFast = resolvedModelFallbacks(saved: nil, primaryModel: "claude-haiku-5-5", rawOptions: fbRaw, tier: .fast, allowedHarnesses: writerHarnesses)
        let customSaved = [
            ModelFallback(harness: "codex", model: "gpt-6.2-sol", effort: "high"),
            ModelFallback(harness: "agy", model: "gemini-4-neon-high", effort: "high"),
            ModelFallback(harness: "codex", model: "gpt-6-luna", effort: "low"),
        ]
        let resolvedCustom = resolvedModelFallbacks(saved: customSaved, primaryModel: "claude-opus-5-5", rawOptions: fbRaw, tier: .capable)
        let afterSwitch = updatingFallbacksAfterPrimaryChange(oldPrimary: "claude-opus-5-5", oldEffort: "high", newPrimary: "gemini-4-neon-high", saved: customSaved)
        let afterEmptySwitch = updatingFallbacksAfterPrimaryChange(oldPrimary: "claude-opus-5-5", oldEffort: "high", newPrimary: "gemini-4-neon-high", saved: [])
        let autoArgs = fallbackCLIArgs(resolvedCustom, explicit: false)
        let cliArgs = fallbackCLIArgs(resolvedCustom, explicit: true)
        let noFbArgs = fallbackCLIArgs([], explicit: true)
        let nowEpoch: TimeInterval = 2_000_000_000
        let sampleUsage: [[String: Any]] = [
            ["id": "claude", "at": nowEpoch - 20, "windows": [["label": "5h", "pct": 100.0, "resets": nowEpoch + 600]]],
            ["id": "codex", "at": nowEpoch - 20, "windows": [["label": "Week", "pct": 100.0, "resets": nowEpoch - 5]]],
            ["id": "agy", "at": nowEpoch - 20, "note": "5h and Week: Claude and GPT", "windows": [["label": "5h", "pct": 100.0, "resets": nowEpoch + 600]]],
        ]
        selCore.modelFallbacks = [ModelFallback(harness: "agy", model: "gemini-4-neon-high", effort: "high")]
        let fbPersisted = savedModelFallbacks("modelFallbacks") == selCore.modelFallbacks
        selCore.modelFallbacks = nil
        selCore.recordLocalLimit("claude", now: Date(timeIntervalSince1970: nowEpoch))
        let cappedNow = selCore.isLocallyCapped("claude", now: Date(timeIntervalSince1970: nowEpoch + 60))
        selCore.clearLocalLimit("claude")
        let uncappedAfterClear = !selCore.isLocallyCapped("claude", now: Date(timeIntervalSince1970: nowEpoch + 60))
        check("config: per-provider model fallbacks auto-default by tier, dedupe, retain previous primary, format CLI args, and detect usage caps",
              autoCapable.map(\.harness) == ["agy", "codex"]
              && autoCapable[0].model == "gemini-4-neon-high" && autoCapable[1].model == "gpt-6.2-sol"
              && autoFast.map(\.harness) == ["agy", "codex"]
              && autoFast[0].model == "gemini-3.8-flash-medium" && autoFast[1].model == "gpt-6-luna" && autoFast[1].effort == "low"
              && resolvedCustom.map(\.harness) == ["codex", "agy"]
              && afterSwitch?.first?.harness == "claude" && afterSwitch?.first?.model == "claude-opus-5-5"
              && afterEmptySwitch == []
              && autoArgs == ""
              && cliArgs == " --fallback 'gpt-6.2-sol@high' --fallback 'gemini-4-neon-high@high'"
              && noFbArgs == " --no-fallback"
              && cappedHarnessesFromUsage(sampleUsage, now: nowEpoch) == Set(["claude"])
              && isLimitOrAuthErrorText("You've hit your 5-hour limit")
              && !isLimitOrAuthErrorText("line 429: syntax error")
              && !isLimitOrAuthErrorText("all good")
              && cappedNow && uncappedAfterClear
              && fbPersisted && savedModelFallbacks("modelFallbacks") == nil)
    }
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
        func isClosed(_ h: FileHandle?) -> Bool {
            guard let h else { return false }
            do { _ = try h.readToEnd(); return false } catch { return true }
        }
        let probe = Pipe()
        try? probe.fileHandleForWriting.close()
        let openNotClosed = !isClosed(probe.fileHandleForReading)
        try? probe.fileHandleForReading.close()
        let closedIsClosed = isClosed(probe.fileHandleForReading)
        check("config: isClosed distinguishes an open EOF pipe from a closed handle (open=\(openNotClosed), closed=\(closedIsClosed))",
              openNotClosed && closedIsClosed)
        // a daemon keeps both pipes: finish() times out promptly and closes both read handles even while Process is retained
        var heldCode: Int32?
        let heldT0 = Date()
        let heldProc = ssh("sleep 2 & exit 3", on: "local", onExit: { heldCode = $0 })
        let until3 = Date() + 5
        while heldCode == nil && Date() < until3 { RunLoop.main.run(until: Date() + 0.02) }
        let heldElapsed = Date().timeIntervalSince(heldT0)
        let drainDone = heldT0 + 2.4
        while Date() < drainDone { RunLoop.main.run(until: Date() + 0.05) }  // let sleep 2 close write ends (avoids EAGAIN) while heldProc stays retained
        let heldOutClosed = isClosed((heldProc.standardOutput as? Pipe)?.fileHandleForReading)
        let heldErrClosed = isClosed((heldProc.standardError as? Pipe)?.fileHandleForReading)
        check("config: ssh() survives a held pipe, times out promptly, and closes read handles even while Process is retained (rc=\(heldCode ?? -1), secs=\(String(format: "%.2f", heldElapsed)), out=\(heldOutClosed), err=\(heldErrClosed))",
              heldCode == 3 && heldElapsed < 1.5 && heldOutClosed && heldErrClosed)
        var rc0ErrDone = 0
        for _ in 0..<10 {
            _ = ssh("printf 'stderr noise\\n' >&2; printf 'ok\\n'", on: "local", onExit: { if $0 == 0 { rc0ErrDone += 1 } })
        }
        let until4 = Date() + 5
        while rc0ErrDone < 10 && Date() < until4 { RunLoop.main.run(until: Date() + 0.02) }
        check("config: ssh() survives rc=0 with active stderr without a close race", rc0ErrDone == 10)
        let buffered = Pipe()
        var got = Data()
        let bufQ = DispatchQueue(label: "greenlight.test.pipe.buf")
        let bufReader = PipeReader(buffered.fileHandleForReading, on: bufQ) { got.append($0) }
        try? buffered.fileHandleForWriting.write(contentsOf: Data("hello".utf8))
        try? buffered.fileHandleForWriting.close()
        bufReader.finish(within: 2)
        check("config: PipeReader reads buffered data to EOF and closes the handle",
              got == Data("hello".utf8) && isClosed(buffered.fileHandleForReading))
        let heldPipe = Pipe()
        let heldQ = DispatchQueue(label: "greenlight.test.pipe.held")
        let heldReader = PipeReader(heldPipe.fileHandleForReading, on: heldQ) { _ in }
        heldReader.finish(within: 0.05)
        try? heldPipe.fileHandleForWriting.close()
        check("config: PipeReader.finish() times out and closes a held pipe without waiting for the writer",
              isClosed(heldPipe.fileHandleForReading))
    }
    // issue 128: Open in editor and a dropped or pasted file, as the app hands them on
    let vs = "com.microsoft.VSCode", zed = "dev.zed.Zed", cursor = "com.todesktop.230313mzl4w4u92"
    check("Open in editor: this Mac opens the folder in whichever editor; no folder, nothing",
          editorOpen(bid: vs, host: "", cwd: "/w/a") == .folder("/w/a") && editorOpen(bid: zed, host: "local", cwd: "/w/a") == .folder("/w/a")
          && editorOpen(bid: vs, host: "devbox", cwd: "") == nil)
    check("Open in editor: a remote host is Zed's ssh:// project, else VS Code's or Cursor's vscode-remote URL, a space in the path encoded",
          editorOpen(bid: zed, host: "devbox", cwd: "/srv/u/w/a") == .zedCLI("ssh://devbox/srv/u/w/a")
          && editorOpen(bid: vs, host: "devbox", cwd: "/srv/u/w/a") == URL(string: "vscode://vscode-remote/ssh-remote+devbox/srv/u/w/a").map(EditorOpen.url)
          && editorOpen(bid: cursor, host: "devbox", cwd: "/srv/u/my w") == URL(string: "cursor://vscode-remote/ssh-remote+devbox/srv/u/my%20w").map(EditorOpen.url))
    check("a dropped file's name: tagged, plain characters, 80 at most, never hidden or empty",
          droppedFileName("pasted.png", tag: "ab12cd34") == "ab12cd34-pasted.png"
          && droppedFileName("my file;$(rm -rf).txt", tag: "t") == "t-my_file___rm_-rf_.txt"
          && droppedFileName("../../.ssh/authorized_keys", tag: "t") == "t-file.._.._.ssh_authorized_keys"
          && droppedFileName(".env", tag: "t") == "t-file.env" && droppedFileName("", tag: "t") == "t-file"
          && droppedFileName(String(repeating: "x", count: 200), tag: "t") == "t-" + String(repeating: "x", count: 80)
          && droppedFileName("a").count == 10)
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

/// Runs the main run loop until `done()` holds or `secs` pass: a fixed short spin flakes on a loaded runner, where the
/// main-queue blocks it waits for haven't run yet. True when `done()` held.
@discardableResult
func spin(until done: () -> Bool, secs: Double = 5) -> Bool {
    let deadline = Date() + secs
    while !done() && Date() < deadline { RunLoop.main.run(until: Date() + 0.01) }
    return done()
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
    // #175: each button that did something waits for Chief's next turn; a rescan, a confirm or a preflight question doesn't
    mainC.acted("Greenlight", "rescan", 0, "", tell: false)
    mainC.acted("ws1", "delete the workspace", 0, #"{"risks":["it's pinned"]}"#)
    mainC.acted("ws1", "start a task", 0, #"{"paused":true}"#)
    check("core: acted queues what each button did for Chief, not a rescan, a confirm or a preflight question",
          mainC.doneSince == ["- merge on ws1: done", "- merge on ws1: failed: conflict"])
    check("core: actedLine names the MR and head (dash.py's, else the board row's) and cuts a long error short",
          actedLine("ws1", "merge", nil, ["ref": "#153", "sha": "0123456789abcdef"]) == "- merge on ws1 (#153, head 0123456789ab): done"
          && actedLine("ws1", "merge", "drift", ["error": "drift"], ["ref": "#9", "sha": "feedbeef"]) == "- merge on ws1 (#9, head feedbeef): failed: drift"
          && actedLine("ws2", "mark ready", "exit 1", nil) == "- mark ready on ws2: failed: exit 1"
          && actedLine("w", "merge", String(repeating: "x\n", count: 400), nil) == "- merge on w: failed: " + String(repeating: "x ", count: 150)
          && actedLine("w", "merge", nil, ["ref": "", "sha": ""]) == "- merge on w: done"
          && actedLine("n", "new workspace", nil, ["ok": true, "warn": "Setup failed (x); no agent started"]) == "- new workspace on n: done, but: Setup failed (x); no agent started"
          && mainC.made(#"{"ok":true,"name":"fix-it"}"#) == "fix-it" && mainC.made(#"{"ok":true,"path":"/srv/w/new"}"#) == "/srv/w/new"
          && mainC.made(#"{"error":"no"}"#) == nil
          && sidebarOps.filter { actedVerb[$0] == nil } == ["info", "diff"])  // a new sidebar op that changes something needs a verb
    for i in 0..<20 { mainC.acted("ws\(i)", "park", 0, "{}") }
    let capped = mainC.doneSince
    var sent: [String] = []
    responder = { r, _, _ in
        if r.contains("chat.py send") { sent.append(r); return ([#"{"e":"done","ok":\#(sent.count > 1),"text":"ok"}"#], 0) }
        return ([], 0)
    }
    mainC.send("hi")  // fails: the buttons come back for the next turn
    let kept = mainC.doneSince
    mainC.send("hi again")
    check("core: a chat turn takes the buttons as --done, once, out of the message; a failed turn gives them back",
          capped.count == Core.doneCap && capped.first == Core.doneMore && capped.last == "- park on ws19: done"
          && sent.count == 2 && sent.allSatisfy { $0.contains(" --done ") && $0.contains("park on ws19") } && kept == capped
          && mainC.doneSince.isEmpty && !mainC.msgs.contains { $0.text.contains("--done") || $0.text.contains("park on ws19") })
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

    var browsed: [URL] = []
    mainC.browse = { browsed.append($0) }
    responder = { r, _, _ in
        guard r.contains("chat.py send") else { return ([], 0) }
        return ([#"{"e":"text_reset"}"#, #"{"e":"text","delta":"Hmm.\n[open: https://github.com/acme/widgets/pull/6"}"#,  // cut off: never opens
                 #"{"e":"text_reset"}"#, #"{"e":"text","delta":"Opening it.\n[open: https://github.com/acme/widgets/pull/7]"}"#,
                 #"{"e":"text_reset"}"#, #"{"e":"text","delta":"It's open."}"#, #"{"e":"done","ok":true,"text":"It's open."}"#], 0)
    }
    mainC.send("Pop up PR 7")
    let carried = browsed, openMsg = mainC.msgs.last?.text
    responder = { r, _, _ in
        r.contains("chat.py send") ? ([#"{"e":"done","ok":false,"text":"Failed.\n[open: https://github.com/acme/widgets/pull/8]"}"#], 1) : ([], 0)
    }
    mainC.send("Pop up PR 8")
    check("core: [open: URL] opens once a reply finishes, even from a block a later one replaced; a failed reply opens nothing",
          carried == [URL(string: "https://github.com/acme/widgets/pull/7")!] && openMsg == "It's open." && browsed == carried)

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
    let splitQ = Question(text: "- CI green\n\nMerge it?", options: ["merge", "hold"], ping: false)
    mainC.questions.append(splitQ)
    mainC.reply(to: "Merge it?\n\n- CI green", "merge", questionID: splitQ.id, freeform: true)
    let splitAnswered = mainC.waiting.isEmpty && mainC.questions.first { $0.id == splitQ.id }?.answer == "merge"
    mainC.reply(to: "Merge it?\n\n- CI green", "actually hold", questionID: splitQ.id, freeform: true)  // already answered: freeform falls back to send, never expires
    check("core: freeform notification reply closes a reordered [screen] chat Question by questionID and falls back once answered",
          splitAnswered && !(mainC.msgs.last?.text.contains("no longer open") == true))
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
    // [chat: reset] clears on a later main-queue turn; until it has, a send is queued behind it (clearing > 0)
    let resetCleared = spin { mainC.clearing == 0 } && mainC.msgs.first?.text.hasPrefix("Context cleared") == true
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
          validReplyAnswered && expiredRecorded && staleNoteHit && resetCleared && errToast?.failed == true && retryMsg && queuedWhileBusy && stoppedMsg)

    // 9b. Turns that outlive the app: the "turn" id is kept until the reply, a dropped connection follows the turn again,
    // a relaunch picks it up (or notes it's gone), and quitting leaves it for the next launch
    let tid = String(repeating: "ab", count: 16)
    var follows = 0
    var followReply: (lines: [String], code: Int32) = ([], 0)
    var pendingMidTurn = false
    Core.refollowDelay = 0
    sshOverride = { remote, h, stdin, onLine, onExit in
        sshLog.append((remote, h, stdin))
        if remote.contains("chat.py send") {
            onLine(#"{"e":"turn","id":"\#(tid)"}"#)
            onLine("Last login: from the shell")  // not the turn's: follow never sends it
            onLine(#"{"e":"step","id":"s1","title":"Looking","detail":"","call":""}"#)
            pendingMidTurn = (ud.dictionary(forKey: mainC.pendingKey)?["id"] as? String) == tid
            onLine(#"{"e":"step","id":"s2","title":"Reading","detail":{"a":1}"#)  // cut off by the drop, at a "}" even
            onExit(255)  // the connection drops before the reply
        } else if remote.contains("chat.py follow") {
            follows += 1
            for l in followReply.lines { onLine(l) }
            onExit(followReply.code)
        } else { onExit(0) }
        return Process()
    }
    followReply = ([#"{"e":"step","id":"s1","title":"Looking","detail":"","call":""}"#, #"{"e":"step","id":"s2","title":"Reading","detail":"","call":""}"#,
                    #"{"e":"done","ok":true,"text":"Back."}"#], 0)
    let wasVerbose = mainC.verbose
    mainC.verbose = true; mainC.feed.clear()
    mainC.send("A long one")
    RunLoop.main.run(until: Date() + 0.1)
    let refollowed = follows == 1 && mainC.msgs.last?.text == "Back." && mainC.msgs.last?.steps.map(\.id) == ["s1", "s2"]  // s1 once, s2 whole
        && sshLog.contains { $0.remote.contains("follow '\(tid)'") || $0.remote.contains("follow \(tid)") }
        && mainC.feed.lines.filter { $0.kind == .call && $0.text.contains("Looking") }.count == 1  // the replayed step isn't logged again
    mainC.verbose = wasVerbose
    let clearedAfterReply = ud.dictionary(forKey: mainC.pendingKey) == nil && mainC.run == nil
    // a long turn through more drops than the tries: each follow brings something new, so the tries start over
    let followOverride = sshOverride
    var drops = 0
    sshOverride = { remote, h, stdin, onLine, onExit in
        sshLog.append((remote, h, stdin))
        if remote.contains("chat.py send") {
            onLine(#"{"e":"turn","id":"\#(tid)"}"#)
            onExit(255)
        } else if remote.contains("chat.py follow") {
            drops += 1
            for k in 0..<drops { onLine(#"{"e":"status","text":"step \#(k)"}"#) }  // the log so far, one line longer each time
            if drops > 6 { onLine(#"{"e":"done","ok":true,"text":"Made it."}"#); onExit(0) } else { onExit(255) }
        } else { onExit(0) }
        return Process()
    }
    mainC.send("Very long")
    RunLoop.main.run(until: Date() + 0.2)
    let triesReset = drops == 7 && mainC.msgs.last?.text == "Made it."
    sshOverride = followOverride
    followReply = ([], 255)  // the host stays away: five tries, then lost, but the id stays for the next launch
    follows = 0
    mainC.send("Another")
    RunLoop.main.run(until: Date() + 0.2)
    let gaveUp = follows == 5 && mainC.msgs.last?.text.contains("Lost the connection") == true && mainC.run == nil
        && (ud.dictionary(forKey: mainC.lostKey)?["id"] as? String) == tid
    // the next sends take over pendingKey: one fails before its "turn" (the host still away), one is refused there
    sshOverride = { _, _, _, _, onExit in onExit(255); return Process() }
    mainC.send("While away")
    RunLoop.main.run(until: Date() + 0.05)
    sshOverride = { _, _, _, onLine, onExit in
        onLine(#"{"e":"turn","id":"\#(String(repeating: "cd", count: 16))"}"#)
        onLine(#"{"e":"done","ok":false,"text":"Another message is still running in this chat."}"#)
        onExit(0)
        return Process()
    }
    mainC.send("Back again")
    RunLoop.main.run(until: Date() + 0.05)
    let lostKept = (ud.dictionary(forKey: mainC.lostKey)?["id"] as? String) == tid && mainC.run == nil
    sshOverride = followOverride
    followReply = ([#"{"e":"done","ok":true,"text":"The lost one."}"#], 0)
    mainC.resumeTurn()  // the next launch
    RunLoop.main.run(until: Date() + 0.05)
    let lostResumed = mainC.msgs.last?.text == "The lost one." && ud.dictionary(forKey: mainC.lostKey) == nil
        && ud.dictionary(forKey: mainC.pendingKey) == nil
    ud.set(["id": tid, "question": "From before", "watch": false, "checkIn": false], forKey: mainC.pendingKey)
    followReply = ([#"{"e":"done","ok":true,"text":"Resumed."}"#], 0)
    mainC.resumeTurn()
    RunLoop.main.run(until: Date() + 0.05)
    let resumed = mainC.msgs.last?.text == "Resumed." && ud.dictionary(forKey: mainC.pendingKey) == nil
    ud.set(["id": tid, "question": "Pruned", "watch": false, "checkIn": false], forKey: mainC.pendingKey)
    followReply = ([], 3)
    mainC.resumeTurn()
    let goneNoted = mainC.msgs.last?.text.contains("is gone") == true && mainC.run == nil && ud.dictionary(forKey: mainC.pendingKey) == nil
    ud.set(["id": "../../etc", "question": "Bad"], forKey: mainC.pendingKey)
    follows = 0
    mainC.resumeTurn()
    let badIdIgnored = mainC.run == nil && follows == 0 && ud.dictionary(forKey: mainC.pendingKey) == nil
    ud.removeObject(forKey: mainC.pendingKey)
    Core.quitting = true  // the app quits mid-turn: nothing is finished, the id stays for the next launch
    mainC.send("Quitting")
    let keptForRelaunch = mainC.run != nil && (ud.dictionary(forKey: mainC.pendingKey)?["id"] as? String) == tid
    Core.quitting = false
    ud.set(["id": tid], forKey: mainC.lostKey)
    mainC.clear()
    let clearForgets = ud.dictionary(forKey: mainC.pendingKey) == nil && ud.dictionary(forKey: mainC.lostKey) == nil
    Core.refollowDelay = 2.0
    check("core: turns keep their id until the reply, follow again after a drop, resume after a relaunch, and survive quitting",
          pendingMidTurn && refollowed && clearedAfterReply && triesReset && gaveUp && lostKept && lostResumed && resumed && goneNoted && badIdIgnored && keptForRelaunch && clearForgets)

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
    mainC.dockIcon = "Default"
    // Legacy gemini-4-neon migration on session 0 (uses isolated unittest UserDefaults)
    ud.set("gemini-4-neon", forKey: "model")
    let migC = Core(session: 0)
    let migrated = ud.string(forKey: "model") == "gemini-4-neon-medium" && migC.msgs.contains { $0.text.contains("agy") }
    // maxTotal (>20 MB) refusal in Core.attach
    let bigChunk = Data(repeating: 0x41, count: 4 << 20)
    mainC.files = (0..<4).map { ChatFile(name: "f\($0).png", kind: "image", data: bigChunk, thumb: nil) }
    mainC.attach(Data(repeating: 0x41, count: 4_500_000), name: "f5.png")
    RunLoop.main.run(until: Date() + 0.1)
    check("core: RulesStore round-trip, legacy gemini-4-neon migration, and helpers",
          rs.repos.contains("widgets") && rs.instructions == "Be brief" && migrated && !bigChunk.isEmpty)

    // 12. Per-project host setting, resolution, snapshot/tree merging, and project switch clearing chat context onto target host
    let hostMap = parseRepoHosts([
        "greenlight-ios": ["mode": "mac", "target": "mac", "needs_mac": true, "reason": "Xcode project"],
        "greenlight": ["mode": "auto", "target": "mac", "needs_mac": true, "reason": "macOS/iOS app"],
        "backend": ["mode": "auto", "target": "server", "needs_mac": false, "reason": ""],
        "api": ["mode": "server", "target": "server", "needs_mac": false, "reason": ""],
    ])
    let hostProjs: [[String: Any]] = [
        ["id": "pios", "name": "iOS", "repos": ["org/greenlight-ios"], "paths": ["/w/ios"]],
        ["id": "pgl", "name": "Greenlight", "repos": ["greenlight"], "paths": ["/w/gl"]],
        ["id": "papi", "name": "API", "repos": ["api", "backend"], "paths": ["/w/api"]],
    ]
    check("project host: server default for All/server repos, local for mac/auto-mac projects, mixed -> server",
          resolveProjectHost(only: nil, projList: hostProjs, repoHosts: hostMap, defaultHost: "devbox") == "devbox"
          && resolveProjectHost(only: ["pios"], projList: hostProjs, repoHosts: hostMap, defaultHost: "devbox") == "local"
          && resolveProjectHost(only: ["pgl"], projList: hostProjs, repoHosts: hostMap, defaultHost: "devbox") == "local"
          && resolveProjectHost(only: ["papi"], projList: hostProjs, repoHosts: hostMap, defaultHost: "devbox") == "devbox"
          && resolveProjectHost(only: ["pios", "pgl"], projList: hostProjs, repoHosts: hostMap, defaultHost: "devbox") == "local"
          && resolveProjectHost(only: ["pios", "papi"], projList: hostProjs, repoHosts: hostMap, defaultHost: "devbox") == "devbox"
          && resolveProjectHost(only: ["papi"], projList: hostProjs, repoHosts: hostMap, defaultHost: "local") == "local"
          && resolveRepoTargetHost("org/greenlight-ios", repoHosts: hostMap, defaultHost: "devbox") == "local"
          && resolveRepoTargetHost("api", repoHosts: hostMap, defaultHost: "devbox") == "devbox"
          && hostBadgeName("local", defaultHost: "devbox") == "Mac"
          && hostBadgeName("devbox", defaultHost: "devbox") == "devbox")

    let mergedSnap = mergeSnapshots(
        ["ts": 10, "scanned": 10, "pucks": [["name": "srv-1", "path": "/srv/1", "nativeHost": true]], "trees": [["path": "/mac/1", "nativeHost": false], ["path": "/srv/tree", "nativeHost": true]]],
        ["ts": 12, "scanned": 12, "pucks": [
            ["name": "srv-1", "path": "/srv/1", "nativeHost": false],
            ["name": "mac-1", "path": "/mac/1", "nativeHost": true],
        ], "trees": [["path": "/srv/1", "nativeHost": false]]],
        primaryHost: "devbox", secondaryHost: "local"
    )
    let mergedSnapPrimaryErr = mergeSnapshots(
        ["ts": 10, "scanned": 10, "scan_started": 8, "scan_error": ["ts": 15, "msg": "GitLab login expired", "fix": "run glab auth login, then ↻"], "pucks": [], "trees": []],
        ["ts": 12, "scanned": 12, "pucks": [], "trees": []],
        primaryHost: "devbox", secondaryHost: "local"
    )
    let mergedSnapSecondaryErr = mergeSnapshots(
        ["ts": 10, "scanned": 10, "pucks": [], "trees": []],
        ["ts": 12, "scanned": 12, "scan_error": ["ts": 18, "msg": "Orca unreachable", "fix": "check Orca is running, then ↻"], "pucks": [], "trees": []],
        primaryHost: "devbox", secondaryHost: "local"
    )
    let mergedSnapBothErr = mergeSnapshots(
        ["ts": 10, "scanned": 10, "scan_error": ["ts": 15, "msg": "GitLab login expired", "fix": ""], "pucks": [], "trees": []],
        ["ts": 12, "scanned": 12, "scan_error": ["ts": 20, "msg": "GitHub rate limited", "fix": ""], "pucks": [], "trees": []],
        primaryHost: "devbox", secondaryHost: "local"
    )
    check("mergeSnapshots preserves scan_error across dual-host merges, picks the newer error, and omits scan_started",
          (mergedSnapPrimaryErr?["scan_error"] as? [String: Any])?["msg"] as? String == "GitLab login expired"
          && mergedSnapPrimaryErr?["scan_started"] == nil
          && (mergedSnapSecondaryErr?["scan_error"] as? [String: Any])?["msg"] as? String == "Orca unreachable"
          && (mergedSnapBothErr?["scan_error"] as? [String: Any])?["msg"] as? String == "GitHub rate limited")
    let mergedPucks = (mergedSnap?["pucks"] as? [[String: Any]]) ?? []
    let mergedSnapTrees = (mergedSnap?["trees"] as? [[String: Any]]) ?? []
    let mergedTrees = mergeTreesPayload(
        ["trees": [["path": "/srv/a", "project": "greenlight", "nativeHost": true]], "panes": ["mc-srv": ["state": "done"]],
         "repoHosts": ["greenlight": ["mode": "auto", "target": "server", "needs_mac": false, "reason": ""]], "ts": 200, "scanned": 190],
        ["trees": [["path": "/srv/a", "project": "greenlight", "nativeHost": false],
                   ["path": "/mac/a", "project": "greenlight", "nativeHost": true]], "panes": ["mc-mac": ["state": "working"]],
         "repoHosts": ["greenlight": ["mode": "server", "target": "server", "needs_mac": true, "reason": "macOS/iOS app"]],
         "ts": 210, "scanned": 150, "scan_error": ["msg": "Orca unreachable", "fix": "", "ts": 205]],
        primaryHost: "devbox", secondaryHost: "local"
    )
    let combinedHost = parseRepoHosts(mergedTrees?["repoHosts"])["greenlight"]
    let mergedPanes = (mergedTrees?["panes"] as? [String: Any]) ?? [:]
    let mergedTreeList = (mergedTrees?["trees"] as? [[String: Any]]) ?? []
    let foldedMap = mergeRepoHostMaps([:], parseRepoHosts([
        "ios-app": ["mode": "server", "target": "server", "needs_mac": false, "reason": ""],
        "acme/ios-app": ["mode": "mac", "target": "mac", "needs_mac": true, "reason": "Xcode project"],
    ]))
    let preservedAutoMap = mergeRepoHostMaps([:], parseRepoHosts([
        "mac-app": ["mode": "auto", "target": "server", "needs_mac": false, "reason": ""],
        "acme/mac-app": ["mode": "server", "target": "server", "needs_mac": true, "reason": "macOS/iOS app"],
    ]))
    let downgradedMap = setRepoHostInMap(foldedMap, repo: "ios-app",
                                         info: RepoHostInfo(mode: "server", target: "server", needsMac: false, reason: ""))
    let savedOnlyHost = resolveProjectHost(
        only: ["pempty"],
        projList: [["id": "pempty", "name": "Empty", "repos": [String](), "paths": [String]()]],
        savedProjects: [["id": "pempty", "name": "Empty", "repos": ["acme/ios-app"], "dirs": [String]()]],
        repoHosts: foldedMap, defaultHost: "devbox"
    )
    let srvUsage: [[String: Any]] = [["id": "claude", "state": "ok"]], macUsage: [[String: Any]] = [["id": "codex", "state": "na"]]
    let uBoth = mergeSnapshots(["usage": srvUsage], ["usage": macUsage], primaryHost: "devbox", secondaryHost: "local")?["usage"] as? [[String: Any]]
    let uMac = mergeTreesPayload(["usage": [[String: Any]]()], ["usage": macUsage], primaryHost: "devbox", secondaryHost: "local")?["usage"] as? [[String: Any]]
    let uNone = mergeTreesPayload(["trees": [[String: Any]]()], nil, primaryHost: "devbox", secondaryHost: "local")?["usage"]
    let tmuxOf: ([String: Any]?, [String: Any]?) -> Bool? = { mergeTreesPayload($0, $1, primaryHost: "devbox", secondaryHost: "local")?["tmuxOk"] as? Bool }
    check("trees merge: tmuxOk is false when either host's tmux didn't answer, true when both did or said nothing",
          tmuxOf(["tmuxOk": true], ["tmuxOk": true]) == true && tmuxOf(["tmuxOk": false], ["tmuxOk": true]) == false
          && tmuxOf(["tmuxOk": true], ["tmuxOk": false]) == false && tmuxOf(["trees": [[String: Any]]()], nil) == true
          && tmuxOf(nil, ["tmuxOk": true]) == false)  // the server's trees not in yet: its sessions are unknown
    let uDup = mergeSnapshots(["usage": srvUsage], ["usage": srvUsage.map { var m = $0; m["state"] = "failed"; return m }],
                              primaryHost: "devbox", secondaryHost: "local")?["usage"] as? [[String: Any]]
    let rescueWin: [[String: Any]] = [["label": "5h", "pct": 20]]
    let rescueMac: [[String: Any]] = [["id": "claude", "state": "ok", "windows": rescueWin]]
    let uRescue = mergeSnapshots(["usage": [["id": "claude", "state": "failed"]]], ["usage": rescueMac],
                                 primaryHost: "devbox", secondaryHost: "local")?["usage"] as? [[String: Any]]
    let dirtyUsage: [Any] = [NSNull(), ["id": "claude", "state": "ok"] as [String: Any]]
    let uDirty = mergeSnapshots(["usage": dirtyUsage], nil, primaryHost: "devbox", secondaryHost: "local")?["usage"] as? [[String: Any]]
    check("usage meters: merged snapshots and trees carry each agent once, greenlight's host's first (Mac rescues a failed primary), tolerating bad elements",
          uBoth?.compactMap { $0["id"] as? String } == ["claude", "codex"] && uMac?.compactMap { $0["id"] as? String } == ["codex"]
          && uNone == nil && uDup?.count == 1 && uDup?.first?["state"] as? String == "ok"
          && uRescue?.count == 1 && uRescue?.first?["state"] as? String == "ok"
          && uDirty?.count == 1 && uDirty?.first?["id"] as? String == "claude")
    check("project host: nativeHost wins on paired Orca dedupe, org/repo fold is deterministic, and savedProjects resolves empty-tree projects",
          mergedPucks.count == 2
          && mergedSnapTrees.count == 1 && mergedSnapTrees[0]["path"] as? String == "/srv/tree"
          && mergedPucks.first { $0["name"] as? String == "mac-1" }?["_runHost"] as? String == "local"
          && mergedPucks.first { $0["name"] as? String == "srv-1" }?["_runHost"] as? String == "devbox"
          && mergedTreeList.count == 2
          && mergedTrees?["scanned"] as? Int == 190 && mergedTrees?["ts"] as? Int == 200  // the Basement footer's scan facts: greenlight's host's first
          && mergedTrees?["scan_error"] == nil  // all from greenlight's host: never the Mac's error beside the server's scan time
          && mergedTreeList.first { $0["path"] as? String == "/srv/a" }?["_runHost"] as? String == "devbox"
          && mergedPanes["mc-srv"] != nil && mergedPanes["mc-mac"] != nil
          && combinedHost?.mode == "auto" && combinedHost?.target == "mac" && combinedHost?.needsMac == true
          && foldedMap["ios-app"]?.target == "mac"
          && preservedAutoMap["mac-app"]?.mode == "auto" && preservedAutoMap["mac-app"]?.target == "mac" && preservedAutoMap["mac-app"]?.needsMac == true
          && downgradedMap["ios-app"]?.target == "server" && downgradedMap["acme/ios-app"]?.target == "server" && downgradedMap["acme/ios-app"]?.needsMac == true
          && savedOnlyHost == "local")

    // Verify switchProject clears docked attachments, tracks clearing per host (slow old host never blocks new host),
    // keeps serverConnected separate from local connected, and preserves DWIM toast runHost across switches.
    var oldNewFinish: ((Int32) -> Void)?
    sshOverride = { remote, h, stdin, onLine, onExit in
        sshLog.append((remote, h, stdin))
        if remote.contains("chat.py new") && h == "devbox" { oldNewFinish = onExit; return Process() }
        let (lines, code) = responder(remote, h, stdin)
        for l in lines { onLine(l) }
        onExit(code)
        return Process()
    }
    sshLog.removeAll()
    responder = { r, _, _ in
        if r.contains("chat.py send") { return ([#"{"e":"done","ok":true,"text":"Running on Mac"}"#], 0) }
        if r.contains("dwim.py undo") { return ([#"{"ok":true,"undone":["w1"],"kept":[]}"#], 0) }
        return ([], 0)
    }
    let swC = Core(session: 9)
    var undocked = [String]()
    swC.unattach = { undocked.append($0) }
    swC.attached = [Attached(id: "old-ws", label: "old-ws", color: .gray, mrs: 1, text: "old ctx", sent: true)]
    swC.serverConnected = false
    swC.syncChatHost("devbox")
    let offlineOnSrv = !swC.connected
    swC.msgs.append(Msg(.you, "old server msg"))
    swC.context = 5000; swC.window = 200000
    swC.show(DwimToast(text: "Parked w1", undo: "u-srv", runHost: "devbox"), for: nil)
    swC.switchProject(to: "local", label: "iOS")
    let newCalls = sshLog.filter { $0.remote.contains("chat.py new") }
    let switchedClean = swC.chatHost == "local" && swC.context == 0 && swC.window == 0
        && swC.attached.isEmpty && undocked == ["old-ws"]
        && swC.connected && swC.clearing == 0
        && swC.msgs.count == 1 && swC.msgs[0].text == "Switched to iOS on this Mac. New conversation."
        && newCalls.map(\.host) == ["devbox", "local"]
    sshLog.removeAll()
    swC.send("build ios app")
    let sentOnLocalWhileOldHung = sshLog.contains { $0.remote.contains("chat.py send") && $0.host == "local" }
    swC.undoToast()
    let undoHitOldHost = sshLog.contains { $0.remote.contains("dwim.py undo") && $0.host == "devbox" }
    oldNewFinish?(0)
    check("project host: switchProject clears attachments, doesn't block on old host, keeps connected accurate, and preserves DWIM undo host",
          offlineOnSrv && switchedClean && sentOnLocalWhileOldHung && undoHitOldHost)

    sshOverride = { remote, h, stdin, onLine, onExit in
        sshLog.append((remote, h, stdin))
        let (lines, code) = responder(remote, h, stdin)
        for l in lines { onLine(l) }
        onExit(code)
        return Process()
    }
    responder = { r, _, _ in
        if r.contains("task 'host-set'") { return ([#"{"repo":"widgets","mode":"mac","target":"mac","needs_mac":false,"reason":"","options":{"host":"mac"},"source":"yours"}"#], 0) }
        return ([], 0)
    }
    rs.repo = "widgets"
    rs.setHost("mac")
    check("project host: RulesStore.setHost persists mode and updates repoHostTarget",
          rs.repoHostMode == "mac" && rs.repoHostTarget == "mac" && rs.repoHosts["widgets"]?.target == "mac")

    // 13. Pane recorder tmux hooks & quoting
    let hooks = App.paneRecorderHooks()
    check("app: paneRecorderHooks emits idempotent after-new-session and client-attached pipe-pane hooks",
          hooks.count == 2
          && hooks[0].contains("after-new-session[1]")
          && hooks[1].contains("client-attached[1]")
          && hooks.allSatisfy { $0.contains("#{pane_pipe}") && $0.contains("pane_log.py") && $0.contains("pipe-pane -O") }
          && App.tmuxQuote(#"a"b\c$HOME"#) == #""a\"b\\c\$HOME""#
          && App.tmuxFormatEscape("dir#1/file#2") == "dir##1/file##2")
    let attach = delegate.paneCmd(session: "mc-agent", cwd: "/w/agent", on: "local")
    check("pane: a live agent session attaches read-write, with our tmux options, outside an inherited tmux client",
          attach.contains("unset TMUX; printf") && attach.contains("new-session -A -s 'mc-agent'")
          && attach.contains("set -g status off") && !attach.contains("attach -r"))
    check("pane: closing a tab ends its own tmux session, never an agent's it attached (keep) nor one another pane shows",
          App.closeEnds(["cmd": "close"], session: "mc-shell", held: { _ in false })
          && !App.closeEnds(["cmd": "close", "keep": true], session: "mc-agent", held: { _ in false })
          && !App.closeEnds(["cmd": "close"], session: "mc-agent", held: { _ in true })
          && !App.closeEnds(["cmd": "close"], session: nil, held: { _ in false }))
    // paneCmd's long run, for real: a login shell, then tmux where it's on PATH (else that shell), evals it byte for byte past tmux's
    // 16 KB command line, prints runMark and leaves no temp file; onto the live session again it saves nothing. Its own tmux socket,
    // no pane recorder (its hooks name the real socket and home), killed after through a login shell as the pane found tmux.
    // paneCmd puts its file in $TMPDIR, or /tmp.
    do {
        let tmp = ProcessInfo.processInfo.environment["TMPDIR"].map { $0.isEmpty ? "/tmp" : $0 } ?? "/tmp", dir = NSTemporaryDirectory() + "gl-unit-run-\(getpid())", out = dir + "/got"
        let sock = "tmux -L \(shellQuote("gl-unit-run-\(getpid())")) -f /dev/null"
        let login = { (cmd: String) -> Int32 in
            let p = Process(); p.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh")
            p.arguments = ["-lc", cmd]; p.standardInput = FileHandle.nullDevice; p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
            do { try p.run() } catch { return -1 }
            let until = Date() + 10
            while p.isRunning && Date() < until { RunLoop.main.run(until: Date() + 0.05) }
            if p.isRunning { p.terminate(); return -1 }  // a wedged tmux or rc: fail the check, don't hang the suite
            return p.terminationStatus
        }
        _ = login("\(sock) kill-server 2>/dev/null"); try? FileManager.default.removeItem(atPath: dir)  // a killed earlier run with this pid
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let prompt = String(repeating: "it's \"q\" $HOME `x` \\ #S #{session_name} → ;\n\t", count: 400)
        let run = "printf %s \(shellQuote(prompt)) > \(shellQuote(out))", mark = "\u{1b}]6973;greenlight-run\u{07}"
        let leftovers = { (try? FileManager.default.contentsOfDirectory(atPath: tmp))?.filter { $0.hasPrefix("greenlight-run.") }.count ?? 0 }
        let before = leftovers(), hasTmux = login("command -v tmux") == 0
        let full = delegate.paneCmd(session: "gl-unit-run", cwd: dir, on: "local", run: run)
        let cmd = full.replacingOccurrences(of: " \\; " + App.paneRecorderHooks().joined(separator: " \\; "), with: "").replacingOccurrences(of: App.tmux, with: sock)
        let hermetic = !cmd.contains("pane_log.py") && !cmd.contains(App.tmux) && cmd.count < full.count  // else it runs nothing
        check("pane: the long-run check's command has its own tmux socket and no pane recorder", hermetic)
        func launch(until done: @escaping (String) -> Bool) -> String {
            let p = Pty(); var shown = Data()
            p.onOutput = { shown.append($0) }
            p.start(cols: 80, rows: 24, cmd: cmd)
            let until = Date() + 15
            while !done(String(decoding: shown, as: UTF8.self)) && Date() < until { RunLoop.main.run(until: Date() + 0.05) }
            RunLoop.main.run(until: Date() + 0.5)
            p.stop(); return String(decoding: shown, as: UTF8.self)
        }
        let tmuxMark = "\u{1b}]6973;greenlight-tmux\u{07}"
        let first = hermetic ? launch { _ in FileManager.default.fileExists(atPath: out) } : "", got = try? String(contentsOfFile: out, encoding: .utf8)
        let viaTmux = first.contains(tmuxMark)
        check("pane: a long run reaches its shell whole (\(prompt.utf8.count) bytes, \(viaTmux ? "via tmux" : "no tmux")), runMark printed, no temp file left",
              hermetic && viaTmux == hasTmux && got == prompt && first.contains(mark) && leftovers() == before)
        if viaTmux {
            try? FileManager.default.removeItem(atPath: out)
            let again = launch { $0.contains(tmuxMark) }  // tmux, not this shell, has it: a broken live branch would exec the brief here
            check("pane: a run onto its live session (a reconnect that raced the mark) is marked, not run again, and saves no file",
                  again.contains(mark) && again.contains(tmuxMark) && !FileManager.default.fileExists(atPath: out) && leftovers() == before)
        }
        _ = login("\(sock) kill-server 2>/dev/null")
        check("pane: the long-run check's tmux server is gone", [1, 127].contains(login("\(sock) has-session 2>/dev/null")))  // not -1: couldn't ask
        _ = login("rm -f \"${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/gl-unit-run-\(getpid())\"")  // then its socket, which tmux leaves
        try? FileManager.default.removeItem(atPath: dir)
    }

    // A Mac-side turn that died because greenlight's scripts aren't on the Mac says so; a server drop is still a drop.
    let missing = Core.lostText("local", localBackendMissing, server: "srv")
    let gone = Core.lostText("srv", 2, server: "srv"), allLocal = Core.lostText("local", 2, server: "local")
    check("project host: a Mac without greenlight's scripts gets the fix, not \"Lost the connection to this Mac\"",
          missing.contains("aren't on this Mac") && missing.contains("srv") && !missing.contains("Lost the connection")
          && gone == "Lost the connection to srv (exit 2)." && allLocal == "Lost the connection to this Mac (exit 2).")
    check("project host: tests never prefix Mac-side commands with a backend copy", localBackendPrefix(server: "srv").isEmpty)
    // resumeOf accepts a validated ASCII agent sessionId and rejects metacharacters, leading dashes, newlines, non-ASCII, or oversized tokens.
    let okRes = delegate.resumeOf(["harness": "claude", "sessionId": "0eb62452-d4f6-4e8d-a381-64ec2d0e3137", "bin": "/opt/bin/claude"])
    let badRes = delegate.resumeOf(["harness": "claude", "sessionId": "bad;whoami", "bin": "/bad/../bin"])
    let dashRes = delegate.resumeOf(["harness": "codex", "sessionId": "--evil"])
    let nlRes = delegate.resumeOf(["harness": "agy", "sessionId": "abc\n"])
    let uniRes = delegate.resumeOf(["harness": "claude", "sessionId": "aé"])
    let bigRes = delegate.resumeOf(["harness": "claude", "sessionId": String(repeating: "a", count: 129)])
    check("resumeOf: accepts safe ASCII sessionId and rejects shell/flag/newline/unicode/oversize injection",
          okRes["harness"] == "claude" && okRes["sessionId"] == "0eb62452-d4f6-4e8d-a381-64ec2d0e3137" && okRes["bin"] == "/opt/bin/claude"
          && badRes["sessionId"] == nil && badRes["bin"] == nil && dashRes["sessionId"] == nil
          && nlRes["sessionId"] == nil && uniRes["sessionId"] == nil && bigRes["sessionId"] == nil)

    // 10. Chief approvals: lifecycle, queue hold, TTL expiry, harness switch, voiceNote, and deny persistence
    do {
        let ac = Core(session: -9099)
        defer {
            try? FileManager.default.removeItem(at: ac.transcriptFile)
            try? FileManager.default.removeItem(at: ac.questionsURL)
        }
        ac.model = "claude-opus-5-5"
        var approveCalls: [(remote: String, stdin: String)] = []
        var watchCalls = 0
        responder = { r, _, stdin in
            if r.contains("autonomy.py gate") { return ([#"{"ok": true}"#], 0) }
            if r.contains("chat.py send") && stdin.contains("Greenlight's watch") {
                watchCalls += 1
                return ([#"{"e":"done","ok":true,"text":"(nothing)"}"#], 0)
            }
            if r.contains("chat.py send") {
                approveCalls.append((r, stdin))
                if stdin.contains("trigger denial") {
                    ac.queue.append(Core.Queued(text: "queued follow-up"))
                    return ([#"{"e":"approval","items":[{"id":"0123456789ab","tool":"Bash","what":"orca host list"}]}"#,
                             #"{"e":"done","ok":true,"text":"Needs approval."}"#], 0)
                }
                return ([#"{"e":"done","ok":true,"text":"Done."}"#], 0)
            }
            return ([], 0)
        }
        // Denial arrives: holds queued message and blocks watchTurn while pending
        ac.send("trigger denial")
        let pendingMsg = ac.msgs.last
        let heldQueue = ac.hasPendingApproval && pendingMsg?.approvals.count == 1 && pendingMsg?.approvalsAt != nil && ac.queue.count == 1
        ac.watched([["name": "ws-a", "state": "working"]])
        ac.watched([["name": "ws-a", "state": "needs", "why": "ci"]])
        let watchBlocked = watchCalls == 0
        // deny() ignores non-approval messages, persists "denied", records doneSince, and drains the held queue
        let plainMsg = Msg(.greenlight, "plain"); ac.deny(plainMsg)
        let ignoredPlain = plainMsg.decided == nil
        if let pm = pendingMsg { ac.deny(pm) }
        let savedData = try? Data(contentsOf: ac.transcriptFile)
        let savedMsgs = savedData.flatMap { try? JSONDecoder().decode([Msg.Saved].self, from: $0) } ?? []
        let deniedPersisted = pendingMsg?.decided == "denied" && savedMsgs.contains { $0.decided == "denied" }
            && ac.queue.isEmpty && approveCalls.last?.remote.contains("--done") == true
            && approveCalls.last?.remote.contains("Denied: orca host list") == true

        // approve() prepends voiceNote when voice.on is true and sends --approve
        approveCalls.removeAll()
        let vm = Msg(.greenlight, "needs"); vm.approvals = [Approval(id: "fedcba987654", tool: "Bash", what: "orca host list")]; vm.approvalsAt = Date()
        ac.msgs.append(vm); ac.voice.on = true; ac.voice.isLive = false
        ac.approve(vm)
        ac.voice.on = false
        let approvedVoice = vm.decided == "approved" && approveCalls.count == 1
            && approveCalls[0].remote.contains("--approve") && approveCalls[0].stdin.hasPrefix(voiceNote)

        // Switching away from Claude expires open approvals as expired_harness and blocks approve()
        approveCalls.removeAll()
        let hm = Msg(.greenlight, "needs"); hm.approvals = [Approval(id: "111122223333", tool: "Bash", what: "orca host list")]; hm.approvalsAt = Date()
        ac.msgs.append(hm)
        ac.model = "codex"
        let harnessExpired = hm.decided == "expired_harness" && !ac.hasPendingApproval
        let nonClaudeMsg = Msg(.greenlight, "needs"); nonClaudeMsg.approvals = hm.approvals; nonClaudeMsg.approvalsAt = Date()
        ac.msgs.append(nonClaudeMsg)
        ac.approve(nonClaudeMsg)
        let blockedNonClaude = nonClaudeMsg.decided == nil && approveCalls.isEmpty
        nonClaudeMsg.decided = "expired_harness"
        ac.model = "claude-opus-5-5"

        // send() expires open approval immediately as expired_replaced
        let rm = Msg(.greenlight, "needs"); rm.approvals = [Approval(id: "444455556666", tool: "Bash", what: "orca host list")]; rm.approvalsAt = Date()
        ac.msgs.append(rm)
        ac.send("supersede it")
        let replacedExpired = rm.decided == "expired_replaced"

        // TTL expiry (1h) transitions to expired_ttl, rejects approve(), and unblocks watchTurn()
        approveCalls.removeAll()
        let tm = Msg(.greenlight, "needs"); tm.approvals = [Approval(id: "777788889999", tool: "Bash", what: "orca host list")]
        tm.approvalsAt = Date().addingTimeInterval(-(Core.approvalTTL + 5))
        ac.msgs.append(tm)
        let ttlNotPending = !ac.hasPendingApproval
        ac.approve(tm)
        let ttlExpired = tm.decided == "expired_ttl" && approveCalls.isEmpty
        ac.watched([["name": "ws-b", "state": "needs", "why": "ci"]])
        let watchUnblocked = watchCalls >= 1

        check("core: approvals hold queue, persist deny, support voiceNote, expire on harness/send/TTL, and gate watchTurn",
              heldQueue && watchBlocked && ignoredPlain && deniedPersisted && approvedVoice
              && harnessExpired && blockedNonClaude && replacedExpired && ttlNotPending && ttlExpired && watchUnblocked)
    }

    // 14. Fast terminal tab launch: direct Pty exec, SshMux socket/args, and paneLaunch (no outer login shells)
    do {
        let env = Pty.ptyEnv()
        check("pty: ptyEnv sets terminal defaults and standard PATH dirs without duplicates",
              env["TERM"] == "xterm-256color" && env["COLORTERM"] == "truecolor" && env["TERM_PROGRAM"] == "Greenlight"
              && (env["PATH"] ?? "").contains("/opt/homebrew/bin") && (env["PATH"] ?? "").contains("/usr/local/bin")
              && (env["LANG"] != nil || env["LC_ALL"] != nil || env["LC_CTYPE"] != nil))

        let p = Pty(); var out = Data()
        p.onOutput = { out.append($0) }
        p.start(cols: 80, rows: 24, exe: "/bin/echo", args: ["direct-pty-ok"])
        let until = Date() + 5
        while !String(decoding: out, as: UTF8.self).contains("direct-pty-ok") && Date() < until { RunLoop.main.run(until: Date() + 0.02) }
        p.stop()
        check("pty: start(exe:args:) runs binary directly on a PTY without a login shell",
              String(decoding: out, as: UTF8.self).contains("direct-pty-ok"))

        let longHost = String(repeating: "h", count: 60) + ".example.internal"
        let s1 = SshMux.socketPath(dir: "/tmp/gl-mux-501", host: longHost, devArgs: [], slotId: 1, pid: 99999)
        let s2 = SshMux.socketPath(dir: "/tmp/gl-mux-501", host: longHost, devArgs: [], slotId: 2, pid: 99999)
        let sDev = SshMux.socketPath(dir: "/tmp/gl-mux-501", host: longHost, devArgs: ["-o", "ProxyCommand=nc -U x"], slotId: 1, pid: 99999)
        check("sshMux: socketPath stays well under macOS 104-byte sun_path limit and isolates slots/devArgs",
              s1.utf8.count < 80 && s1 != s2 && s1 != sDev && s1.hasSuffix(".sock"))

        let mArgs = SshMux.masterArgs(host: "box", sockPath: s1, devArgs: ["-o", "ProxyCommand=nc -U x"])
        check("sshMux: masterArgs sets ControlPersist=no, BatchMode=yes, ClearAllForwardings=yes, keepalives",
              mArgs.contains("-M") && mArgs.contains("-N") && mArgs.contains(s1)
              && mArgs.contains("ControlPersist=no") && mArgs.contains("BatchMode=yes")
              && mArgs.contains("ClearAllForwardings=yes") && mArgs.contains("ServerAliveInterval=5")
              && mArgs.contains("ServerAliveCountMax=2") && Array(mArgs.suffix(2)) == ["--", "box"])

        let loc = delegate.paneLaunch(session: "mc-fast1", cwd: "/tmp", on: "local")
        check("paneLaunch: local pane runs /bin/sh -c with fastPathPrefix (no outer login shell)",
              loc.exe == "/bin/sh" && loc.args.first == "-c" && (loc.args.last ?? "").contains(App.fastPathPrefix))

        let mux = delegate.paneLaunch(session: "mc-fast2", cwd: "/w/a", on: "box", muxSocket: s1)
        check("paneLaunch: remote muxed pane runs /usr/bin/ssh directly over muxSocket with sh -c (no $SHELL -lc)",
              mux.exe == "/usr/bin/ssh" && mux.args.contains("-S") && mux.args.contains(s1)
              && mux.args.contains("ControlMaster=no") && mux.args.contains("ControlPersist=no")
              && (mux.args.last ?? "").hasPrefix("exec sh -c ") && !(mux.args.last ?? "").contains("$SHELL -lc"))

        let dirLaunch = delegate.paneLaunch(session: "mc-fast3", cwd: "/w/a", on: "box", muxSocket: nil)
        check("paneLaunch: remote direct fallback uses ControlPath=none and exec sh -c",
              dirLaunch.exe == "/usr/bin/ssh" && dirLaunch.args.contains("ControlPath=none")
              && dirLaunch.args.contains("ControlMaster=no") && (dirLaunch.args.last ?? "").hasPrefix("exec sh -c "))

        check("paneRecorderHooks: skips pipe-pane on gl-warm-* standby sessions until claimed",
              hooks.allSatisfy { $0.contains("gl-warm-*") })

        check("termBridge: hostKey normalizes local and remote hosts",
              TermBridgePool.hostKey("") == "local" && TermBridgePool.hostKey("local") == "local" && TermBridgePool.hostKey("box") == "box")
        check("termBridge: isBase64 accepts valid base64 and rejects empty strings and trailing newlines",
              TermBridgePool.isBase64("YWJjZA==") && !TermBridgePool.isBase64("")
              && !TermBridgePool.isBase64("YWJjZA==\n") && !TermBridgePool.isBase64("bad'quote"))
        let sArgs = SshMux.slaveArgs(host: "box", sockPath: s1, devArgs: ["-o", "ProxyCommand=nc -U x"], remoteCmd: "exec sh -c true")
        check("sshMux: slaveArgs keeps devArgs and ServerAlive keepalives for direct-fallback safety",
              sArgs.contains("ProxyCommand=nc -U x") && sArgs.contains("ServerAliveInterval=15") && sArgs.contains("ServerAliveCountMax=3"))
        let spec = TermBridgePool.launchSpec(for: "box")
        check("termBridge: remote launchSpec runs term_bridge.py over non-PTY SSH with keepalives",
              spec?.exe == "/usr/bin/ssh" && (spec?.args.contains("-T") ?? false)
              && (spec?.args.contains("BatchMode=yes") ?? false) && (spec?.args.contains("ClearAllForwardings=yes") ?? false)
              && (spec?.args.contains("ServerAliveInterval=5") ?? false)
              && (spec?.args.last ?? "").contains("term_bridge.py"))
    }
    mainC.clear()
}
