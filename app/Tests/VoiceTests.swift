import AVFoundation
import CoreAudio
import Foundation
import Network
import Speech

// MARK: voice & GPT-Live unit tests

/// Deterministic offline voice, audio, WebSocket, and GPT-Live protocol tests.
func runVoiceOfflineUnitTests(check: (String, Bool) -> Void) {
    runTranscriptTests(check: check)
    check("voice: the trail names a stage draw and its card kind by token, a made-up kind as other (issue 208)",
          stageDraw("stage.text") == "text" && stageDraw("stage.wait") == "wait" && stageDraw("alert") == "other"
          && stageCardKind("stage.spec", ["kind": "table"]) == "table" && stageCardKind("stage.spec", [String: Any]()) == "list"
          && stageCardKind("stage.spec", ["kind": "Fix login for ACME"]) == "other" && stageCardKind("stage.chart", ["chart": "pie"]) == "pie"
          && stageCardKind("stage.chart", ["chart": "table"]) == "other" && stageCardKind("stage.spec", ["kind": "pie"]) == "other"
          && stageCardKind("stage.text", ["lead": "x"]) == nil && stageCardKind("stage.wait", "One minute") == nil)
    let st = staged("Three MRs.\n```stage\n<b>hi</b>\n```\n[stage: full]\nDone."), open = staged("Here.\n```stage\n<ul><li>half")
    do {  // the stage's status line (issue 176): one short sentence, never a repeat; the activity Haiku reads; the local fallback
        check("voice: a status line is one sentence of at most 12 words, markdown off, with a full stop",
              statusLine("Checking **PR 153**'s CI. Then the logs.", shown: []) == "Checking PR 153's CI."
              && statusLine("one two three four five six seven eight nine ten eleven twelve thirteen", shown: [])
                == "one two three four five six seven eight nine ten eleven twelve…"
              && statusLine("Reading `chat.py`\nnow", shown: []) == "Reading chat.py now." && statusLine("Mulling…", shown: []) == "Mulling."
              && statusLine("  ", shown: []) == nil && statusLine("…", shown: []) == nil)
        check("voice: a status line that says a shown one again (case, spacing, final mark aside) isn't shown",
              statusLine("checking PR 153's CI", shown: ["Checking PR 153's CI."]) == nil
              && statusLine("Checking PR 154's CI.", shown: ["Checking PR 153's CI."]) == "Checking PR 154's CI.")
        let r = Run("why is CI red on 153?")
        r.steps = [Step(id: "1", title: "glab ci status", detail: "!153", ok: true), Step(id: "2", title: "Reading the job log", detail: "")]
        r.status = "Reading the job log"
        let act = statusActivity(r)
        check("voice: the status line's activity is the newest steps' titles (never a command line), each running/done/failed; no host",
              act == "- done · glab ci status\n- running · Reading the job log" && statusActivity(Run("q")) == "")
        r.plan = Plan(done: 1, total: 3, title: "Fix the test", eta: nil, at: Date()); r.status = "Mulling…"; r.thinking = true
        check("voice: the activity adds the plan and a thinking phrase", statusActivity(r).hasSuffix("\nPlan: 1 of 3 done, now: Fix the test\nStatus: Mulling…"))
        check("voice: without the model, the strip shows the newest running step, else the newest, else the status; never a repeat",
              statusFallback(r, shown: []) == "Reading the job log." && statusFallback(r, shown: ["Reading the job log."]) == nil
              && statusFallback(Run("q"), shown: [])?.hasPrefix("Connecting") == true)
        let ran = Run("q"); ran.steps = [Step(id: "1", title: "Ran gh pr list", detail: "gh pr list -R a/b")]
        check("voice: a running step's \"Ran …\" title reads as \"Running …\" on the strip (#180 review)", statusFallback(ran, shown: []) == "Running gh pr list.")
    }
    check("voice: Chief ends voice mode with [voice: off]", staged("Bye for now.\n[voice: off]") == Staged(text: "Bye for now.", stage: nil, end: true))
    check("voice: contrast is WCAG's ratio", abs(contrast("FFFFFF", "000000") - 21) < 0.01 && abs(contrast("777777", "777777") - 1) < 0.01 && abs(contrast("0969DA", "FFFFFF") - 5.2) < 0.1)
    let bothLooks = [defaultLook(dark: false), defaultLook(dark: true), phosphorLook]
    check("voice: every look inks your words apart from Chief's (a hue, or colour against neutral), legible on its stage", bothLooks.allSatisfy { l in
        let y = hueSat(l.you), t = hueSat(l.text), d = abs(y.hue - t.hue)
        return max(y.sat, t.sat) >= 0.25 && (min(y.sat, t.sat) < 0.15 || min(d, 360 - d) >= 45) && contrast(l.you, l.stage) >= 4.5 && contrast(l.you, l.page) >= 4.5 && l.you != l.text
    })
    check("voice: two looks, Default and Phosphor; a parked look's stored id opens as Default; the stage gets only data-look",
          looks.map(\.id) == ["default", "phosphor"] && ["orb", "zen", "hud", "conversation", nil].allSatisfy { lookNamed($0).id == "default" }
          && lookNamed("phosphor").id == "phosphor" && looks.allSatisfy { $0.vars.contains("dataset.look") && !$0.vars.contains("--mc-") })
    check("voice: Phosphor stays dark; Default follows the appearance", phosphorLook.dark && !defaultLook(dark: false).dark && defaultLook(dark: true).dark
          && bothLooks.allSatisfy { contrast($0.text, $0.stage) >= 7 && contrast($0.dim, $0.stage) >= 4.5 })

    check("voice: stage block and size cut out, an unclosed one hides the rest", st == Staged(text: "Three MRs.\n\n\nDone.", stage: "<b>hi</b>") && open.text == "Here." && open.stage == nil)
    let issued = "{\"title\":\"Issue filed\",\"items\":[\"[greenlight #148](https://github.com/x/y/issues/148)\",\"Open a URL, http(s) only\"]}"
    let card = stageCard(issued), numbered = stageCard("json\n{\"title\":\"Work on first\",\"items\":[\"1. merge !5415\",\"2. clear the draft\"]}")
    check("voice: Chief's {title, items} stage JSON is a card, drawn as is: links to their text, numbered lines as steps (#149)",
          card?["title"] as? String == "Issue filed" && card?["kind"] as? String == "list"
          && (card?["items"] as? [[String: String]])?.map { $0["label"]! } == ["greenlight #148", "Open a URL, http(s) only"]
          && numbered?["kind"] as? String == "steps" && (numbered?["items"] as? [[String: String]])?.first?["label"] == "merge !5415")
    let objs = stageCard("{\"title\":\"!12\",\"kind\":\"compare\",\"items\":[{\"label\":\"CI\",\"detail\":\"green\",\"tone\":\"ok\"},{\"name\":\"Owner\",\"value\":3},{\"tone\":\"x\"}]}")
    check("voice: a card's object items keep label, detail and a known tone; empty ones drop",
          objs?["kind"] as? String == "compare" && (objs?["items"] as? [[String: String]]) == [["label": "CI", "detail": "green", "tone": "ok"], ["label": "Owner", "detail": "3", "tone": ""]])
    check("voice: a chart, HTML, notes or empty JSON aren't cards; a native template with its facts goes through whole, links to their text",
          stageCard("{\"chart\":\"bar\",\"title\":\"x\"}") == nil && stageCard("<b>hi</b>") == nil && stageCard("Three MRs are green.") == nil
          && stageCard("{}") == nil && stageCard("{\"title\": ") == nil
          && (stageCard("{\"kind\":\"table\",\"title\":\"[x](https://u)\",\"rows\":[[\"[!1](https://u)\", 2]]}")?["rows"] as? [[Any]])?.first?.first as? String == "!1"
          && stageCard("{\"kind\":\"table\",\"title\":\"[x](https://u)\",\"rows\":[[\"a\"]]}")?["title"] as? String == "x"
          && (stageCard("[\"one\", \"two\"]")?["items"] as? [[String: String]])?.count == 2)
    let labels = { (s: String) in (stageCard(s)?["items"] as? [[String: String]])?.map { $0["label"]! + ($0["detail"]!.isEmpty ? "" : "=" + $0["detail"]!) } }
    check("voice: a template without its facts is a card, not an empty table (#153 review)",
          labels("{\"kind\":\"table\",\"title\":\"Results\",\"items\":[\"One\"]}") == ["One"] && stageCard("{\"kind\":\"timeline\",\"items\":[\"a\"]}")?["kind"] as? String == "list")
    check("voice: a shape a card can't show goes to Haiku whole, not a bare title (#153 review)",
          ["{\"title\":\"MRs\",\"rows\":[[\"!1\",\"green\"]]}", "{\"title\":\"F\",\"items\":\"a, b\"}", "{\"title\":\"M\",\"items\":{\"!12\":\"green\"}}",
           "{\"title\":\"MRs\",\"columns\":[\"MR\",\"CI\"],\"rows\":[[\"!1\",\"green\"]]}", "{\"title\":\"audit\",\"findings\":[\"x\"]}"].allSatisfy { stageCard($0) == nil })
    check("voice: plain values beside the title, other item keys and true/false all make it onto the card (#153 review)",
          labels("{\"title\":\"Status\",\"CI\":\"green\",\"sub\":\"backend\",\"Draft\":false}") == ["CI=green", "Draft=no", "backend"]
          && labels("{\"title\":\"t\",\"items\":[{\"ref\":\"!12\",\"status\":\"merged\"},{\"label\":\"Mergeable\",\"value\":true,\"why\":\"CI green\"}]}") == ["!12=merged", "Mergeable=yes · CI green"]
          && labels("{\"title\":\"audit\",\"notes\":[\"1. divide by zero\",\"Likely recovered\"]}") == ["1. divide by zero", "Likely recovered"])
    check("voice: a card's lines keep their meaning: 1) counts as steps, a list keeps its numbers, > and + and URLs stay (#153 review)",
          labels("{\"items\":[\"1) merge\",\"2) rebase\"]}") == ["merge", "rebase"] && stageCard("{\"items\":[\"1) merge\"]}")?["kind"] as? String == "steps"
          && labels("{\"kind\":\"list\",\"items\":[\"1. a\",\"2. b\"]}") == ["1. a", "2. b"]
          && labels("{\"items\":[\"> 5 minutes\",\"+ 2 added\",\"https://x.test/1\"]}") == ["> 5 minutes", "+ 2 added", "https://x.test/1"]
          && labels("{\"items\":[\"\",\" \",\"a\",\"b\",\"c\",\"d\",\"e\",\"f\",\"g\",\"h\",\"i\"]}")?.count == 8)
    check("voice: the fence's own label, any case, before the JSON (#153 review)",
          ["JSON\n{\"title\":\"x\"}", "-json {\"title\":\"x\"}", "card\n{\"title\":\"x\"}", "json{\"title\":\"x\"}"].allSatisfy { stageCard($0)?["title"] as? String == "x" })
    let raw = { (spec: [String: Any]) in ([spec["title"] as? String ?? ""] + ((spec["items"] as? [[String: String]]) ?? []).map { $0["label"]! }).contains { $0.hasPrefix("{") || $0.hasPrefix("[") || $0.contains("\":") } }
    let fb = fallbackSpec("{\"heading_x\":\"Held\",\"meta\":{\"a\":\"CI green\",\"b\":\"no conflicts\"}}")
    let broken = fallbackSpec("JSON\n{\"title\": \"Work on first\", \"items\": [\"merge !5415\", \"say \\\"go\\\"\", 'x'")  // cut off, single quotes: won't parse
    check("voice: the fallback never puts raw JSON on the stage: a card, else its values (title first), else a broken block's quoted values",
          fallbackSpec(issued)["title"] as? String == "Issue filed" && !raw(fb) && fb["title"] as? String == "Held"
          && (fb["items"] as? [[String: String]])?.map { $0["label"]! } == ["CI green", "no conflicts"]
          && fallbackSpec("{\"summary\":\"Held\",\"title\":\"T\",\"more\":{\"a\":\"x\"}}")["title"] as? String == "T"
          && !raw(broken) && broken["title"] as? String == "Work on first" && (broken["items"] as? [[String: String]])?.map { $0["label"]! } == ["merge !5415", "say \"go\"", "x"]
          && raw(["title": "{\"title\":\"x\"}"]) && fallbackSpec("All green. Two MRs wait.")["title"] as? String == "All green.")
    let two = staged("A.\n```stage\n{\"title\":\"x\"}\n```\nB.\n```Stage\n{\"title\":\"y\"}\n```\nC.")
    check("voice: every ```stage block is cut out of the transcript, ```Stage too; the first is drawn (#153 review)",
          two.text == "A.\n\nB.\n\nC." && two.stage == "{\"title\":\"x\"}" && staged("Done.\n``` stage\n{}\n```").stage == "{}"
          && staged("Run:\n```stagehand\nx\n```").stage == nil && staged("```stage-json\n{}\n```").stage == "-json\n{}")
    let diff = stageCard("{\"kind\":\"diff\",\"files\":[{\"path\":\"app/__init__.py\",\"add\":1}],\"lines\":[{\"t\":\"+\",\"s\":\"    f(**kw)\"}]}")
    check("voice: a template keeps its code and paths as written; only links turn to text (#153 review 2)",
          ((diff?["files"] as? [[String: Any]])?.first?["path"] as? String) == "app/__init__.py"
          && ((diff?["lines"] as? [[String: Any]])?.first?["s"] as? String) == "    f(**kw)")
    check("voice: an item's MR or ref is its label, whatever else it holds; a named one keeps them in the detail (#153 review 2)",
          labels("{\"items\":[{\"mr\":\"!12\",\"author\":\"sam\"}]}") == ["!12=sam"] && labels("{\"items\":[{\"label\":\"CI\",\"mr\":\"!12\"}]}") == ["CI=!12"])
    check("voice: notes that open with a link or a checkbox aren't JSON, and their fallback reads them (#153 review 2)",
          ["[!12](https://x.test) green", "- [ ] rebase", "Blocked [waiting on CI]", "MRs\n[!12](https://x.test)"].allSatisfy { stageJSON($0) == nil }
          && ["[\"a\"]", "[]", "{}", "[{\"a\":1}]", "json [1, 2]"].allSatisfy { stageJSON($0) != nil }
          && fallbackSpec("- [!12](https://x.test) green\n- [!13](https://x.test) draft")["title"] as? String == "!12 green")
    check("voice: ```stage_json is a stage fence too (#153 review 2)", staged("Ok.\n```stage_json\n{\"title\":\"x\"}\n```").text == "Ok."
          && staged("Ok.\n```stage_json\n{\"title\":\"x\"}\n```").stage == "_json\n{\"title\":\"x\"}" && stageCard("_json\n{\"title\":\"x\"}")?["title"] as? String == "x")
    check("voice: a card line keeps code and identifiers: __init__, **kwargs, fns[0](x); a link to a URL is its text (#153 review 2)",
          labels("{\"items\":[\"fix scripts/__init__.py\",\"pass **kwargs\",\"fns[0](x)\",\"**bold** [!12](https://x.test/12)\"]}")
          == ["fix scripts/__init__.py", "pass **kwargs", "fns[0](x)", "bold !12"])
    check("voice: broken JSON's fallback has its strings, not the punctuation between them (#153 review 2)",
          fallbackSpec("{\"title\": \"Work\", \"items\": [\"a\", \"b")["title"] as? String == "Work"
          && (fallbackSpec("{\"title\": \"Work\", \"items\": [\"a\", \"b")["items"] as? [[String: String]])?.map { $0["label"]! } == ["a"])
    check("voice: JSON that starts with true/false/null or single quotes is JSON; one with nothing to say draws blank, never its source (#153 review 3)",
          ["[true, false]", "[null]", "{'title': 'Work'}", "[ 'a' ]"].allSatisfy { stageJSON($0) != nil } && stageJSON("[tasks] two left") == nil
          && labels("[true, false]") == ["yes", "no"] && fallbackSpec("{'title': 'Work', 'items': ['a', 'it\\'s']}")["title"] as? String == "Work"
          && (fallbackSpec("{'title': 'Work', 'items': ['a', 'it\\'s']}")["items"] as? [[String: String]])?.map { $0["label"]! } == ["a", "it's"]
          && ["{}", "{\"title\":", "{\"items\":[{\"a\":null}]}", "[]"].allSatisfy { fallbackSpec($0)["title"] as? String == "" && !raw(fallbackSpec($0)) }
          && fallbackSpec("{\"items\": [1, 2,")["title"] as? String == "")
    check("voice: only real emphasis loses its marks: **kwargs and **opts, f(**a) g(**b), x ** 2 stay (#153 review 3)",
          labels("{\"items\":[\"forward **kwargs and **opts\",\"f(**a) calls g(**b)\",\"x ** 2 + y ** 2\",\"a **real** b ~~old~~\"]}")
          == ["forward **kwargs and **opts", "f(**a) calls g(**b)", "x ** 2 + y ** 2", "a real b old"])
    let table = stageCard("{\"kind\":\"table\",\"title\":\"CI\",\"rows\":[[\"`main` **failed**\", \"[doc](./README.md)\"]]}")
    check("voice: a table or timeline drops markdown marks too; relative links read as text; ```stage.json is a label (#153 review 3)",
          ((table?["rows"] as? [[Any]])?.first as? [String]) == ["main failed", "doc"] && labels("{\"items\":[\"[MR](gitlab.test/x/1)\"]}") == ["MR"]
          && stageCard(".json {\"title\":\"x\"}")?["title"] as? String == "x" && stageCard(":json\n{\"title\":\"x\"}")?["title"] as? String == "x")
    check("voice: broken JSON's escapes read as text; unquoted keys still give their values (#153 review 3)",
          (fallbackSpec("{\"title\": \"a\\nb\\/c\", \"x\": [1")["title"] as? String) == "a b/c"
          && fallbackSpec("{title: \"x\", items: [\"a\"]}")["title"] as? String == "x")
    check("voice: notes with brackets, timestamps, bullets or {placeholders} stay notes in fallback, never blank or garbled (#153 review 4)",
          ["[10:30] standup\n- review !12", "[12 MRs](https://x) waiting", "[1] Merge !5415\n[2] Rebase !5416",
           "- [148](https://github.com/x/y/issues/148) open", "[2/3] Checks passing", "[2026-11-24] Deploy finished",
           "[-] skipped", "- [-] cancelled", "Note: {branch} is ready", "Plan {draft} first",
           "[2 MRs] don't merge, it's red", "Today: [2 green, 1 red]"].allSatisfy { stageJSON($0) == nil }
          && fallbackSpec("[10:30] standup\n- review !12")["title"] as? String == "[10:30] standup"
          && fallbackSpec("[2 MRs] don't merge, it's red")["title"] as? String == "[2 MRs] don't merge, it's red"
          && fallbackSpec("Plan {draft} first")["title"] as? String == "Plan {draft} first"
          && fallbackSpec("- [148](https://github.com/x/y/issues/148) open")["title"] as? String == "148 open")
    check("voice: native templates require the fields stage.html reads; malformed ones fall back cleanly (#153 review 4)",
          stageCard("{\"kind\":\"timeline\",\"title\":\"Release\",\"items\":[{\"name\":\"Ship\",\"date\":\"Monday\"}]}")?["kind"] as? String == "list"
          && labels("{\"kind\":\"timeline\",\"title\":\"Release\",\"items\":[{\"name\":\"Ship\",\"date\":\"Monday\"}]}") == ["Ship=Monday"]
          && stageCard("{\"kind\":\"lifecycle\",\"title\":\"!12\",\"stages\":[{\"name\":\"Build\",\"status\":\"done\"}]}") == nil
          && stageCard("{\"kind\":\"diff\",\"title\":\"Changes\",\"files\":[{\"path\":\"a.swift\",\"add\":1}],\"lines\":[\"+foo\",\"-bar\"]}") == nil
          && stageCard("{\"kind\":\"diff\",\"files\":[{\"name\":\"a.swift\",\"added\":5}]}") == nil
          && stageCard("{\"kind\":\"table\",\"title\":\"CI\",\"rows\":[[]]}") == nil
          && stageCard("{\"kind\":\"diff\",\"files\":[{\"path\":\"a.swift\",\"add\":1}],\"lines\":[]}")?["kind"] as? String == "diff"
          && stageCard("{\"kind\":\"timeline\",\"title\":\"Lately\",\"items\":[{\"at\":\"10:00\",\"label\":\"Merged !12\"}]}")?["kind"] as? String == "timeline")
    check("voice: JSON null and falsy chart don't break cards or chart routing; numeric rest fields keep their keys (#153 review 4)",
          !isStageChart(["chart": NSNull()]) && !isStageChart(["chart": ""]) && !isStageChart(["chart": false]) && isStageChart(["chart": "bar"])
          && stageCard("{\"chart\":null,\"title\":\"Status\",\"items\":[\"All green\"]}")?["title"] as? String == "Status"
          && labels("{\"chart\":false,\"title\":\"Status\",\"items\":[\"All green\"]}") == ["All green"]
          && stageCard("{\"title\":\"Deploys\",\"chart\":{\"type\":\"bar\",\"values\":[1,2]}}") == nil
          && stageCard("{\"title\":\"x\",\"items\":[\"a\"],\"note\":null}")?["title"] as? String == "x"
          && labels("{\"title\":\"Status\",\"items\":null,\"notes\":[\"Merge !12\"]}") == ["Merge !12"]
          && stageCard("{\"title\":null,\"heading\":\"Status\",\"items\":[\"a\"]}")?["title"] as? String == "Status"
          && labels("{\"items\":[{\"title\":null,\"ws\":\"auth-fix\",\"author\":\"alice\"}]}") == ["auth-fix=alice"]
          && labels("{\"items\":[{\"path\":\"app/a.py\",\"add\":1,\"del\":2}]}") == ["app/a.py=add: 1 · del: 2"])
    check("voice: steps key makes a steps card and object items lose leading step numbers; labelled HTML fences strip their tag (#153 review 4)",
          stageCard("{\"title\":\"Plan\",\"steps\":[\"Rebase\",\"Merge\"]}")?["kind"] as? String == "steps"
          && stageCard("{\"title\":\"Plan\",\"kind\":\"list\",\"steps\":[\"Rebase\",\"Merge\"]}")?["kind"] as? String == "list"
          && labels("{\"title\":\"Plan\",\"kind\":\"steps\",\"items\":[{\"label\":\"1. Rebase\",\"detail\":\"main\"}]}") == ["Rebase=main"]
          && stageHTML("html\n<div>hi</div>") == "<div>hi</div>" && stageHTML("-svg\n<svg/>") == "<svg/>"
          && stageHTML("svg\n<?xml version=\"1.0\"?><svg/>") == "<?xml version=\"1.0\"?><svg/>"
          && stageHTML("Note:\n<something>") == nil && stageHTML("html\n< 50ms") == nil)
    check("voice: broken JSON unescapes in one pass (\\n vs \\\\n, \\uXXXX); fallback skips kind/tone/chart metadata and puts title first (#153 review 4)",
          (fallbackSpec("{\"path\": \"C:\\\\new\\\\test\", \"x\": [1")["title"] as? String) == "C:\\new\\test"
          && (fallbackSpec("{\"title\": \"one\\u2014two\", \"x\": [1")["title"] as? String) == "one\u{2014}two"
          && fallbackSpec("{\"kind\":\"steps\",\"title\":\"Work on first\",\"items\":[\"merge !5415\",\"rebase")["title"] as? String == "Work on first"
          && fallbackSpec("{\"chart\":\"bar\",\"title\":\"Deploys\",\"labels\":[\"Mon\",\"Tue")["title"] as? String == "Deploys"
          && (fallbackSpec("{\"title\":\"MRs\",\"kind\":\"table\",\"rows\":[]}")["items"] as? [[String: String]])?.isEmpty == true
          && (fallbackSpec("{\"title\":\"Audit\",\"kind\":\"list\",\"findings\":[{\"label\":\"Bug\",\"tone\":\"bad\"}]}")["items"] as? [[String: String]])?.map { $0["label"]! } == ["Bug"])
    let split = staged("Two reviews need you.\n[screen]\n- **!4834** at abc1234\n```stage\n<b>x</b>\n```"), streaming = staged("Two reviews need you.\n[scr")
    check("voice: a [screen] line splits the spoken gist from the shown detail; the transcript gets both, a stage block still comes out",
          split.text == "Two reviews need you." && split.screen == "- **!4834** at abc1234" && split.stage == "<b>x</b>"
          && split.shown == "Two reviews need you.\n\n- **!4834** at abc1234" && staged("All green.").screen == nil && staged("All green.").shown == "All green."
          && staged("Gist.\n[SCREEN]\n").screen == nil && streaming.screen == nil && sentences(plain(streaming.text), from: 0).said == ["Two reviews need you."])
    let quoted = "Add a line `[screen]` then the detail.\n```\n[screen]\n```\nDone.", twice = staged("Gist.\n [screen] \nOne.\n[screen]\nTwo.")
    let prose = ["Always separate them with `[screen]`", "Put details after `[screen]`\nand keep the gist short.", "`[screen]` splits gist from detail.", "Example:\n```[screen]\nfoo\n```"]
    check("voice: only a [screen] at a line's start and outside a code fence splits; a second one stays in the detail",
          staged(quoted).screen == nil && staged(quoted).text == quoted && twice.text == "Gist." && twice.screen == "One.\n[screen]\nTwo."
          && prose.allSatisfy { staged($0).screen == nil && staged($0).text == $0 })
    let loose = ["Gist.\n**[screen]**\n- a", "Gist.\n[Screen]:\n- a", "Gist.\n[screen] - a", "Gist.\n**[screen]:**\n- a", "Gist. [screen]\n- a", "Gist.\n\n`[screen]`\n- a"].map { staged($0) }
    check("voice: a bold, colon, same-line or line-ending [screen] still splits, so the detail is never read out",
          loose.allSatisfy { $0.text == "Gist." && $0.screen == "- a" })
    let opts = staged("Merge it?\nOptions: merge | hold\n[screen]\n- CI green").parts, after = staged("Merge it?\n[screen]\n- CI green\nOptions: merge | hold").parts
    let bare = staged("[screen]\n- CI green").parts, plainReply = staged("All green.\nOptions: a | b").parts
    check("voice: the Options line comes off whichever part ends with it; the question goes last, over its buttons",
          opts.options == ["merge", "hold"] && opts.shown == "- CI green\n\nMerge it?" && opts.gist == "Merge it?" && opts.detail == "- CI green"
          && after.options == ["merge", "hold"] && after.detail == "- CI green" && after.shown == "- CI green\n\nMerge it?"
          && bare.gist == "" && bare.shown == "- CI green" && bare.detail == "- CI green"
          && plainReply.shown == "All green." && plainReply.gist == "All green." && plainReply.detail == nil && plainReply.options == ["a", "b"] && staged("Gist.\n[screen]\n- a").parts.shown == "Gist.\n\n- a")
    let streamed = spokenSoFar("Merge it?\nOptions: merge | hold\n"), early = spokenSoFar("Merge it?\nOpt")
    let mid = "Merge it?\nOptions: merge | hold\nOr ask me later.", saidLive = sentences(spokenSoFar(mid), from: 0)
    check("voice: an Options line in the gist is never said, while it streams or at the end, and the spoken offset holds",
          sentences(streamed, from: 0).said == ["Merge it?"] && sentences(early, from: 0).said == ["Merge it?"]
          && sentences(streamed, from: sentences(early, from: 0).to).said.isEmpty
          && saidLive.said == ["Merge it?"] && sentences(spokenSoFar(splitOptions(mid).0) + "\n", from: saidLive.to).said == ["Or ask me later."])
    check("voice: a commit hash isn't read out, and the sentence still reads; words, numbers, refs and look-alikes stay",
          forEar("The latest commit (abc1234) fixed it.") == "The latest commit fixed it." && forEar("Commit 9f8e7d6c5b4a fails.") == "Commit fails."
          && forEar("Merged in abc1234, then deployed.") == "Merged in a commit, then deployed." && forEar("Fixed in abc1234.") == "Fixed in a commit."
          && forEar("The abc1234 commit fixed it.") == "The commit fixed it." && forEar("The abc1234 Commit fixed it.") == "The Commit fixed it."
          && forEar("abc1234 broke it.") == "a commit broke it."
          && forEar("Facade 1234567 and !42 stay, deadbeef too.") == "Facade 1234567 and !42 stay, deadbeef too."
          && ["It costs 1e10000.", "Color #4f3e2d1 is set.", "Id 0f8fad5b-d9cb-469f is set.", "See a/abc1234def.",
              "See abc1234def/main.swift.", "Check abc1234.log.", "Diff abc1234..def5678 looks good.", "Commit Abc1234 fails."].allSatisfy { forEar($0) == $0 })
    check("voice: \(waitWords.count) ways to say one minute, none twice", waitWords.count > 300 && Set(waitWords).count == waitWords.count && waitWords.allSatisfy { $0.first!.isUppercase && $0.hasSuffix("…") })
    check("chat: replies start with the text, an old mood line is dropped", unmood("😌🔍❤️\n\nAll green.") == "All green." && unmood("#3 is up.\nok") == "#3 is up.\nok" && unmood("👍 Done.") == "👍 Done." && unmood("All green.") == "All green.")
    check("voice: emoji aren't spoken, numbers are", plain("😌🔍❤️\n#3 is 2 of 5.") == "\n#3 is 2 of 5.")
    let ss = sentences(plain("**Two** MRs: [!12](https://x/12) is green. One fails! Fix it"), from: 0)
    check("voice: spoken text is plain, whole sentences only: \(ss)", ss.said == ["Two MRs: !12 is green.", "One fails!"] && sentences("a. b", from: ss.to).said.isEmpty)
    check("voice: cancel chooses a pending launch even while Chief runs", voiceCommand("cancel") == .cancel
          && voiceStopsPending(.cancel, chiefRunning: true, pending: true)
          && !voiceStopsPending(.stop, chiefRunning: true, pending: true)
          && voiceStopsPending(.stop, chiefRunning: false, pending: true))
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
    check("voice: of two Greenlights in voice mode, exactly one keeps it (the later start; the pid breaks a tie)",
          voiceYields(mine: (100, 7), theirs: (101, 3)) && !voiceYields(mine: (101, 3), theirs: (100, 7))
          && voiceYields(mine: (100, 3), theirs: (100, 7)) != voiceYields(mine: (100, 7), theirs: (100, 3)) && !voiceYields(mine: (100, 7), theirs: (200, 7)))
    check("voice: without echo cancellation nothing says talk to interrupt; with it GPT-Live always does, Apple's recognizer by the setting",
          !voiceInterruptible(live: true, vp: false, bargeIn: true) && !voiceInterruptible(live: false, vp: false, bargeIn: true)
          && voiceInterruptible(live: true, vp: true, bargeIn: false) && voiceInterruptible(live: false, vp: true, bargeIn: true)
          && !voiceInterruptible(live: false, vp: true, bargeIn: false))
    check("voice: while voice mode is starting, its button says a press cancels the start",
          voiceButtonText(on: false, starting: true).label == "Cancel starting voice mode" && voiceButtonText(on: false, starting: false).label == "Start voice mode"
          && voiceButtonText(on: true, starting: false).label == "End voice mode" && voiceButtonText(on: false, starting: true).help.contains("cancel"))
    do {  // the header's knobs say what a click does now
        let off = HeaderTips(), live = HeaderTips(on: true, transcript: true, captions: true, out: true, name: "Ada", look: "Phosphor", next: "Default")
        check("chat header: each top-right knob's hint and label follow its state",
              off.voice.label == "Start voice mode" && live.voice.label == "End voice mode"
              && off.view.label == "Show the transcript" && live.view.label == "Show the stage" && live.view.help.contains("Ada")
              && off.caption.label == "Turn captions on" && live.caption.label == "Turn captions off"
              && off.pop.label == "Move to its own window" && live.pop.label == "Put back in the main window"
              && live.style.help == "Style: Phosphor. Click for Default" && live.style.label.contains("Default")
              && [off.attach, off.view, off.caption, off.style, off.voice, off.pop, live.view, live.caption, live.pop].allSatisfy { !$0.help.isEmpty && $0.help.count <= 100 && !$0.label.isEmpty })
        let unmuted = HeaderTips(on: true, name: "Ada"), muted = HeaderTips(on: true, micMuted: true, name: "Ada")
        check("chat header: the mic knob says what a click does now: mute, or unmute",
              unmuted.mic.label == "Mute the microphone" && muted.mic.label == "Unmute the microphone" && muted.mic.help.contains("Ada")
              && [unmuted.mic, muted.mic].allSatisfy { $0.help.contains("⇧⌘M") && $0.help.count <= 100 })
        check("chat header: the muted mic is status.needs in every look, never the accent",
              looks.allSatisfy { mutedFill($0) == dsHex(DS.Palette.statusNeedsSolid, dark: $0.dark) && mutedFill($0) != $0.accent })

        let scr = NSRect(x: 0, y: 0, width: 1000, height: 800), sz = NSSize(width: 200, height: 24)
        let mid = hintFrame(sz, mouse: NSPoint(x: 500, y: 400), screen: scr), low = hintFrame(sz, mouse: NSPoint(x: 500, y: 20), screen: scr)
        let edge = hintFrame(sz, mouse: NSPoint(x: 990, y: 400), screen: scr)
        check("hover hint: by the cursor, below it, flipped above near the bottom, kept on screen at the right edge",
              mid.minX == 510 && mid.maxY < 400 && mid.maxY > 360 && low.minY > 20 && scr.contains(low) && scr.contains(edge) && edge.maxX <= 996)
    }
    check("voice: muted shows when nobody else has the floor, and says so",
          voiceTalk(speaking: false, thinking: false, micMuted: true) == .muted && voiceTalk(speaking: true, thinking: false, micMuted: true) == .speaking
          && voiceTalk(speaking: false, thinking: true, micMuted: true) == .thinking && voiceTalk(speaking: false, thinking: false, micMuted: false) == .listening
          && voiceTalkLabel(.muted) == "Muted" && voiceTalkLabel(.listening) == "Listening" && mutedHint.contains("unmute")
          && voiceTalkLabel(.thinking, micMuted: true) == "Thinking · muted" && voiceTalkLabel(.speaking, micMuted: true) == "Speaking · muted"
          && voiceTalkLabel(.thinking) == "Thinking" && micMenuTitle(muted: false) == "Mute Microphone" && micMenuTitle(muted: true) == "Unmute Microphone")
    do {  // Settings ▸ Thinking motion: on by default, stored, and sent to the stage with every call as data-motion
        let saved = ud.object(forKey: "thinkingMotion"); defer { ud.set(saved, forKey: "thinkingMotion") }
        ud.removeObject(forKey: "thinkingMotion")
        let v = Voice(), byDefault = v.thinkingMotion && v.motionVars.contains("dataset.motion = 'on'")
        v.thinkingMotion = false
        check("voice: thinking motion is on by default, and turning it off is stored and reaches the stage",
              byDefault && ud.object(forKey: "thinkingMotion") as? Bool == false && !Voice().thinkingMotion && v.motionVars.contains("dataset.motion = 'off'"))
    }
    do {  // turned off while the stage page still loads: the call queued for it ends on the new value
        let saved = ud.object(forKey: "thinkingMotion"); defer { ud.set(saved, forKey: "thinkingMotion") }
        let v = Voice(); v.thinkingMotion = true
        v.wait("One minute", "x")
        let carried = v.stageQueued?.contains("dataset.motion = 'on'") ?? false  // every stage call carries the setting
        v.thinkingMotion = false
        check("voice: every stage call carries thinking motion, and turning it off mid-load corrects the queued one",
              carried && v.stageQueued.map { $0.hasSuffix("dataset.motion = 'off';") } ?? false)
    }
    do {  // muting drops the half-heard turn and the recognition request: nothing more reaches the recognizer until unmute
        let v = Voice()
        v.setMicMuted(true)
        let offStaysUnmuted = !v.micMuted  // voice mode off: nothing to mute
        v.on = true; v.isLive = true  // GPT-Live: listen() doesn't start Apple's recognizer here
        let r = SFSpeechAudioBufferRecognitionRequest(), w = DispatchWorkItem {}
        v.request = r; v.heard = "park the"; v.endTurn = w; v.meter.you = v.meter.you.map { _ in 0.5 }
        v.setMicMuted(true)
        let muted = v.micMuted && v.request == nil && v.heard.isEmpty && w.isCancelled && v.meter.you.allSatisfy { $0 == 0 }
        v.isLive = false; v.listen()  // Apple's recognizer: no new request while muted, so nothing is transcribed
        let noRequest = v.request == nil
        v.isLive = true
        v.toggleMic()
        check("voice: mute only while voice mode is on; it drops the turn, the request and the meter; no recognizer while muted; toggling unmutes",
              offStaysUnmuted && muted && noRequest && !v.micMuted)
        v.on = false
    }
    do {  // GPT-Live: what it transcribes or hands off around a mute never reaches Chief (lastHeard is set just before Core.spoken)
        let c = Core(), v = c.voice
        v.on = true; v.isLive = true
        v.onLive(.heard("park the"))
        v.setMicMuted(true)
        v.onLive(.heard(" auth ones"))  // its transcript of audio sent before the mute, arriving after it
        let ignoredWhileMuted = v.heard.isEmpty
        v.handOff("d1")  // a hand-off while muted
        let mutedHandOff = v.lastHeard.isEmpty && v.delegation == nil
        v.setMicMuted(false)
        v.onLive(.heard("hold 42"))
        v.handOff("d2", dropped: true)  // came while muted (or before a mute), run after the unmute: the new turn's words stay
        let keptNewTurn = v.lastHeard.isEmpty && v.heard == "hold 42" && v.delegation == nil
        v.heard = ""
        v.setMicMuted(true); v.onLive(.delegated("d3")); v.setMicMuted(false)  // handed off while muted, unmuted within its 0.4 s
        v.onLive(.delegated("d4")); v.setMicMuted(true); v.setMicMuted(false)  // handed off, then a quick mute within its 0.4 s
        v.onLive(.heard("hold 42"))
        let until = Date() + 0.7; while Date() < until { RunLoop.main.run(until: Date() + 0.05) }
        let droppedBoth = v.lastHeard.isEmpty && v.heard == "hold 42" && v.delegation == nil
        v.heard = ""; v.onLive(.delegated("d5"))  // control: an unmuted hand-off does go through the same delay (nothing heard: no Chief turn)
        let until2 = Date() + 0.7; while Date() < until2 && v.delegation == nil { RunLoop.main.run(until: Date() + 0.05) }
        check("voice: GPT-Live's transcript and hand-offs from a muted turn are dropped, not sent to Chief, and don't eat the next turn",
              ignoredWhileMuted && mutedHandOff && keptNewTurn && droppedBoth && v.delegation == "d5")
        v.on = false
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
    v.vp = false  // forced on above without opening the mic: left on, stopAll() opens inputNode to undo it, minutes in coreaudiod on a headless runner
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
        // Voice.finished and failed clear chiefDrew so subsequent watch/checkIn turns (which skip began()) still update the stage
        v.on = true; v.isLive = false; v.chiefDrew = false
        v.finished(staged("Turn 1.\n[screen]\n- a"), "Turn 1.", detail: "- a")
        let clearedAfterScreen = !v.chiefDrew, seqBeforeWatch = v.drawSeq
        v.finished(staged("Watch turn.\n[screen]\n- b"), "Watch turn.", detail: "- b")
        let watchDrewStage = v.drawSeq > seqBeforeWatch && !v.chiefDrew
        v.chiefDrew = true; v.failed("Stopped."); v.hush()
        check("voice: finished and failed clear chiefDrew so subsequent watch/checkIn turns still draw the stage",
              clearedAfterScreen && watchDrewStage && !v.chiefDrew)
        // issue 208: a turn leaves `stage` events: the wait card (the app's) at its start, then Chief's text, timed from the start
        let tr = Run("trail?"); c.run = tr; v.on = true; v.isLive = false
        v.began(tr); v.finished(staged("Two.\n[screen]\n- a"), "Two.", detail: "- a"); c.run = nil
        Diag.q.sync {}
        let trail = ((try? String(contentsOf: Diag.url, encoding: .utf8)) ?? "").split(separator: "\n")
            .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }.filter { $0["kind"] as? String == "stage" }
        let turnEvents = trail.filter { $0["turn"] as? Int == v.turnNo }
        check("voice: a turn's stage draws go to the trail as tokens and numbers: draw, by, turn, n, at (issue 208)",
              turnEvents.count == 2 && turnEvents[0]["draw"] as? String == "wait" && turnEvents[0]["by"] as? String == "app"
              && turnEvents[0]["n"] as? Int == 1 && turnEvents[1]["draw"] as? String == "text" && turnEvents[1]["by"] as? String == "chief"
              && turnEvents[1]["n"] as? Int == 2 && (turnEvents[1]["at"] as? Double).map { $0 >= 0 && $0 < 5 } == true
              && turnEvents.allSatisfy { $0["card"] == nil && $0["dropped"] == nil } && v.turnAt == nil
              && trail.contains { $0["by"] as? String == "app" && $0["card"] as? String == "list" && $0["turn"] == nil && $0["n"] == nil })  // v.show's Board card above: between turns
        // a card asked between turns stays untimed, even when it lands after a turn begins
        let lateMark = v.turnMark
        v.put("stage.spec", [["kind": "table"]], by: "fast", mark: lateMark)
        c.run = tr; v.began(tr); c.run = nil
        v.put("stage.spec", [["kind": "stat"]], by: "fast", mark: lateMark)
        Diag.q.sync {}
        let late = ((try? String(contentsOf: Diag.url, encoding: .utf8)) ?? "").split(separator: "\n")
            .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }.filter { $0["by"] as? String == "fast" }
        check("voice: a stage card asked between turns stays untimed, even after a turn begins (issue 208)",
              lateMark.at == nil && late.count == 2  // the turn above had ended: turnAt is cleared, so neither carries a turn
              && late.allSatisfy { $0["turn"] == nil && $0["at"] == nil })
        let m2: (turn: Int, at: Date?) = (v.turnNo, Date() - 10)  // asked 10 s into this turn's clock
        v.put("stage.spec", [["kind": "table"]], by: "fast", mark: m2)  // asked in this turn, which is still running
        c.run = tr; v.began(tr); c.run = nil
        v.put("stage.spec", [["kind": "diff"]], by: "fast", mark: m2)  // lands after the next turn began
        Diag.q.sync {}
        let late2 = ((try? String(contentsOf: Diag.url, encoding: .utf8)) ?? "").split(separator: "\n")
            .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }.filter { $0["by"] as? String == "fast" }.suffix(2)
        check("voice: a card asked in one turn and landing in the next stays in its own turn, with no place in the new one (issue 208)",
              late2.count == 2 && late2.allSatisfy { $0["turn"] as? Int == m2.turn && ($0["at"] as? Double ?? 0) >= 10 }
              && late2.first?["n"] != nil && late2.last?["n"] == nil && v.turnNo == m2.turn + 1)
        v.turnAt = nil
        try? FileManager.default.removeItem(at: Diag.url)  // the folder is the test run's support folder too
        // the status line (issue 176): after a quiet stretch of a running turn, a line on the strip (the fallback without a key);
        // the same work again shows nothing new; a clear (speech, the turn's end) takes it down and drops a line on its way
        keyLock.lock(); let savedA = keys["anthropic"]; keys["anthropic"] = .some(nil); keyLock.unlock()
        let sr = Run("status?"); sr.steps = [Step(id: "s1", title: "Running the tests", detail: "")]
        c.run = sr; v.on = true; v.isLive = false; v.hush(); v.statusShown = []; v.statusAsking = false
        v.quietSince = Date() - 5; v.statusTick(sr, v.tickGen)
        let notYet = v.statusShown.isEmpty
        v.quietSince = Date() - statusQuiet - 1; v.statusTick(sr, v.tickGen)
        let firstLine = v.statusShown == ["Running the tests."] && v.statusUp && !v.statusAsking && Date().timeIntervalSince(v.quietSince) < 1
        v.quietSince = Date() - statusQuiet - 1; v.askStatus(sr)
        let noRepeat = v.statusShown.count == 1
        sr.steps.append(Step(id: "s2", title: "Reading the failure", detail: "")); v.statusSeq += 1
        v.statusAsking = true; let seqNow = v.statusSeq; v.clearStatus(); v.statusAsking = false
        let cleared = !v.statusUp && v.statusSeq == seqNow + 1
        v.finished(staged("Done."), "Done."); let afterReply = !v.statusUp
        c.run = nil; v.statusTick(sr, v.tickGen); let staleRun = v.statusShown.count == 1  // a turn that's over gets no line
        // review fixes: the wait word doesn't hold the line back; a cleared chat takes the line down; the reply clears it even muted
        c.run = sr; v.hush(); v.statusShown = []; v.lines = [v.waitWord]; v.quietSince = Date() - statusQuiet - 1; v.statusTick(sr, v.tickGen)
        let fillerQuiet = v.statusShown.count == 1 && v.statusUp
        v.lines = []; c.run = nil; v.statusTick(sr, v.tickGen); let clearedChat = !v.statusUp
        c.run = sr; v.statusShown = []; v.quietSince = Date() - statusQuiet - 1; v.statusTick(sr, v.tickGen)
        v.muted = true; sr.live = "Here's what I found. And more"; v.said = 0; v.live(sr); let mutedReply = !v.statusUp
        v.statusShown = []; v.quietSince = Date() - statusQuiet - 1; v.statusTick(sr, v.tickGen); let upBeforeStop = v.statusUp
        sr.stopping = true; v.statusTick(sr, v.tickGen); let stoppedClears = upBeforeStop && !v.statusUp
        v.quietSince = Date() - statusQuiet - 1; v.statusTick(sr, v.tickGen); let stoppedQuiet = !v.statusUp
        sr.stopping = false; v.muted = false; v.said = 0
        v.statusShown = []; v.quietSince = Date() - statusQuiet - 1; let g0 = v.tickGen; v.statusTick(sr, g0); let upBeforeScreen = v.statusUp
        sr.live = "Found it.\n[screen]\n| a | b |"; sr.textAt = Date(); v.live(sr)
        let screenClearsAtOnce = !v.statusUp  // on the text, not the next tick
        v.statusShown = []; v.quietSince = Date() - statusQuiet - 1; v.statusTick(sr, g0)  // the turn's clock, after [screen]: stopped
        let screenEnds = upBeforeScreen && screenClearsAtOnce && !v.statusUp
        sr.steps.append(Step(id: "s3", title: "Checking CI again", detail: "", start: Date() + 1))  // a tool after it: it was commentary
        v.statusShown = []; v.quietSince = Date() - statusQuiet - 1; v.statusTick(sr, g0)
        let screenResumes = v.statusUp && v.statusShown == ["Checking CI again."]
        c.run = nil
        keyLock.lock(); keys["anthropic"] = savedA; keyLock.unlock(); v.hush()
        check("voice: the status line waits for 20 s of quiet, shows the newest step, doesn't repeat, and clears on speech or the reply",
              notYet && firstLine && noRepeat && cleared && afterReply && staleRun)
        check("voice: the wait word doesn't hold the status line back; a cleared chat and a muted reply take it down (#180 review)",
              fillerQuiet && clearedChat && mutedReply)
        check("voice: a stopped turn's strip clears and gets no new line (#180 review)", stoppedClears && stoppedQuiet)
        check("voice: once the reply's [screen] detail streams, the strip clears; a step after it brings lines back (#180 reviews 2-4)", screenEnds && screenResumes)
    }

    check("voice: Voice state machine handles GPT-Live events, cues, audio silence, stage blocks, fallback dedupe, barge-in, and close (\(heardText) / \(handedOff) / \(awaitingCue) / \(cueCleared) / \(startedSpeaking) / \(endedSpeaking) / \(liveErrCount) / \(fbCount) / \(capOff) / \(closedStopped) / \(bargedIn) / \(turnRanCmd))",
          heardText == "What is status?" && handedOff && awaitingCue && cueCleared
          && startedSpeaking && endedSpeaking && liveErrCount == 1 && fbCount == 1
          && capOff && v.captions && closedStopped && bargedIn && turnRanCmd)
}

/// Dictation keeps every utterance the Apple recognizer hears in a recording, not just the last (#172). The sequences are the
/// results it gave on a Mac for `say` recordings with pauses, shortened to a few partials each.
func runTranscriptTests(check: (String, Bool) -> Void) {
    func heard(_ results: [(String, Bool, Bool)]) -> String {
        var t = Transcript()
        for (s, meta, final) in results { t.add(s, ends: meta || final) }
        return t.text
    }
    check("dictation: partials revise one utterance, even shrinking and changing its first word; the final one is the text",
          heard([("Grin light check", false, false), ("Greenlight", false, false), ("Greenlight check the", false, false), ("Greenlight, check the pipeline.", false, true)])
          == "Greenlight, check the pipeline.")
    check("dictation: a pause ends an utterance (metadata) and the next one is added after it, not in its place (#172)",
          heard([("I want to check", false, false), ("I want to check the pipeline status", false, false), ("I want to check the pipeline status", true, false),
                 ("Then", false, false), ("Then merge it when it is green", false, false), ("Then merge it when it is green", true, true)])
          == "I want to check the pipeline status Then merge it when it is green")
    check("dictation: a one-word utterance before a pause stays (#172)",
          heard([("OK", false, false), ("OK", true, false), ("Check", false, false), ("Check the pipeline, please", true, true)]) == "OK Check the pipeline, please")
    check("dictation: two pauses in one sentence keep all three parts, in order (#172)",
          heard([("First, I paused", false, false), ("First, I paused", true, false), ("In the middle", false, false), ("In the middle of a sentence", true, false),
                 ("And then", false, false), ("And then I kept talking", true, true)]) == "First, I paused In the middle of a sentence And then I kept talking")
    check("dictation: over 60 s of speech, the utterance cut at 60 s stays (#172)",
          heard([("Sentence one", false, false), ("Sentence one … sentence eleven is about", true, false), ("That are waiting", false, false),
                 ("That are waiting on review. Sentence eighteen.", true, true)]) == "Sentence one … sentence eleven is about That are waiting on review. Sentence eighteen.")
    check("dictation: Apple's servers send one growing utterance, ended by the final result: kept once",
          heard([("OK", false, false), ("OK, check the pipeline", false, false), ("OK, check the pipeline", true, true)]) == "OK, check the pipeline")
    check("dictation: a spoken repeat stays; an empty or blank final keeps what was heard",
          heard([("Yes", false, false), ("Yes.", true, false), ("Yes", false, false), ("Yes.", true, true)]) == "Yes. Yes."
          && heard([("Hello there", false, false), ("", false, true)]) == "Hello there" && heard([("Hello", false, false), (" ", true, false)]) == "Hello")
    check("dictation: nothing heard is empty", heard([("", false, false), (" ", false, true)]).isEmpty)
}
