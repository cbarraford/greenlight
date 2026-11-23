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
    let bigChunk = Data(repeating: 0x41, count: 4 << 20)
    mainC.files = (0..<4).map { ChatFile(name: "f\($0).png", kind: "image", data: bigChunk, thumb: nil) }
    mainC.attach(Data(repeating: 0x41, count: 4_500_000), name: "f5.png")
    RunLoop.main.run(until: Date() + 0.1)
    check("core: RulesStore round-trip, legacy gemini-4-neon migration, and helpers",
          rs.repos.contains("widgets") && rs.instructions == "Be brief" && migrated && !bigChunk.isEmpty)
    mainC.clear()
}
