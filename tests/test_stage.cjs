// Headless checks for stage.html, voice mode's card: each kind renders as text only, and Chief's own HTML can't run script.
//   NODE_PATH=…/node_modules node tests/test_stage.cjs
const assert = require("assert");
const { launchBrowser } = require("./js_harness.cjs");
(async () => {
  const b = await launchBrowser(); const p = await b.newPage();
  await p.goto("file://" + require("path").join(__dirname, "../app/stage.html"));
  assert.match(await p.textContent("#s"), /Listening/);
  assert.notEqual(await p.$eval(".cursor", e => getComputedStyle(e).animationName), "none");  // the cursor blinks while listening
  await p.emulateMedia({ reducedMotion: "reduce" });
  assert.equal(await p.$eval(".cursor", e => getComputedStyle(e).animationName), "none");  // and stops under reduced motion
  assert.equal(await p.$eval("#s", e => getComputedStyle(e).animationName), "fade");  // content fades in instead of rising
  await p.emulateMedia({ reducedMotion: "no-preference" });
  await p.evaluate(() => stage.wait("One minute", "Scanning"));
  assert.equal(await p.$eval(".cursor", e => getComputedStyle(e).animationName), "none");  // thinking: a still cursor
  assert.equal(await p.$eval("h1", e => getComputedStyle(e).textTransform), "none");
  await p.evaluate(() => stage.spec({ title: "3 MRs ready", kind: "steps", eyebrow: "<i>ship</i>", stat: "", items: [{ label: "<b>x</b>", detail: "d" }, { label: "two", detail: "" }] }));
  assert.equal(await p.locator("ol.items.steps li").count(), 2);
  assert.equal(await p.locator("ol li b").first().textContent(), "<b>x</b>");  // text, never markup
  assert.equal(await p.textContent(".eyebrow"), "<i>ship</i>");
  await p.evaluate(() => stage.spec({ title: "t", kind: "bogus", items: [] }));
  assert.equal(await p.locator(".items").count(), 0);
  await p.evaluate(() => stage.spec({ title: "prototype fallback", kind: "constructor", items: [{ label: "safe" }] }));
  assert.equal(await p.locator("ul.items.list li b").textContent(), "safe");
  await p.evaluate(() => stage.spec({ title: "t", kind: "list", items: [{ label: "a", tone: "needs" }, { label: "b", tone: "bad" }, { label: "c", tone: "<x>" }] }));
  assert.deepEqual(await p.$$eval("ul.items li", ls => ls.map(l => l.querySelector("svg.tone")?.getAttribute("aria-label") ?? "")), ["Needs you", "Failed", ""]);
  await p.evaluate(() => stage.html('<h2 id="h">Hi</h2><script>parent.document.body.dataset.pwned=1</script><img src=x onerror="parent.document.body.dataset.pwned=2">'));
  await p.waitForTimeout(300);
  const f = p.frameLocator("iframe");
  assert.equal(await f.locator("#h").textContent(), "Hi");
  assert.equal(await p.evaluate(() => document.body.dataset.pwned), undefined);
  await p.evaluate(() => stage.chart({ chart: "bar", title: "CI runs", stacked: true, labels: ["Oct 20", "<i>x</i>"], note: "n",
    series: [{ name: "success", values: [44, 30], color: "green" }, { name: "failed", values: [31, "nope"], color: "red" }] }));
  assert.equal(await p.locator("svg.chart rect").count(), 4);
  assert.deepEqual(await p.locator("svg.chart text.v").allTextContents(), ["75", "30"]);  // stack totals; a bad value is 0
  assert.match(await p.textContent(".legend"), /74 success.*31 failed/);
  assert.equal(await p.locator("svg.chart i").count(), 0);  // labels are text
  // colours are tokens: green (success) is neutral, red keeps its one meaning (failed); no literal hex reaches the chart
  assert.deepEqual(await p.$$eval("svg.chart rect", rs => [...new Set(rs.map(r => r.getAttribute("fill")))]), ["var(--c2)", "var(--c-red)"]);  // success is done: neutral
  await p.evaluate(() => stage.chart({ chart: "line", labels: ["a", "b"], series: [1, 2, 3, 4, 5, 6].map(n => ({ name: "s" + n, values: [n, n] })) }));
  assert.deepEqual(await p.$$eval("svg.chart polyline", ls => ls.map(l => l.getAttribute("stroke"))),
    ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c-rest)", "var(--c-rest)"]);  // fixed order, never cycled
  await p.evaluate(() => stage.chart({ chart: "bar", title: "merged", labels: ["mon", "tue"], series: [{ name: "n", values: [3, 6] }] }));
  assert.equal(await p.locator(".bars .bar").count(), 2);  // one series: Ledger rows, not SVG
  assert.equal(await p.getAttribute(".bar:nth-child(2) .track i", "class"), "peak");
  assert.match(await p.textContent(".foot"), /total 9 · avg 4\.5 · peak tue/);
  await p.evaluate(() => stage.chart({ chart: "line", labels: ["a", "b", "c"], series: [{ name: "x", values: [1, 3, 2] }] }));
  assert.equal(await p.locator("svg.chart polyline").count(), 1);
  await p.evaluate(() => stage.chart({ chart: "pie", labels: ["a", "b"], series: [{ values: [3, 1] }] }));
  assert.equal(await p.locator("svg.chart path").count(), 2);
  // the native templates: facts in, text only out, in every look
  await p.evaluate(() => stage.spec({ kind: "table", eyebrow: "the board", title: "2 workspaces", columns: ["Workspace", "MR", "CI", "<b>Next</b>"],
    rows: [["auth-login", "#42", "failed", "<img src=x onerror=alert(1)>"], ["billing", "#44", "passed", "merge"]], tones: ["bad", "nope"] }));
  assert.equal(await p.locator("table.grid tbody tr").count(), 2);
  assert.equal(await p.locator("table.grid th").nth(3).textContent(), "<b>Next</b>");
  assert.equal(await p.locator("table.grid img").count(), 0);
  assert.equal(await p.getAttribute("table.grid tbody tr:nth-child(1)", "class"), "bad");
  assert.equal(await p.getAttribute("table.grid tbody tr:nth-child(2)", "class"), null);  // an unknown tone is none
  assert.equal(await p.getAttribute("table.grid tbody tr.bad svg.tone", "aria-label"), "Failed");  // tone is a glyph, not colour alone
  assert.equal(await p.locator("table.grid tbody tr:nth-child(2) svg.tone").count(), 0);
  assert.equal(await p.textContent("table.grid tbody tr.bad td"), "auth-login");
  await p.evaluate(() => stage.spec({ kind: "table", title: "empty", rows: "nope" }));
  assert.equal(await p.locator("table").count(), 0);
  assert.equal(await p.textContent("h1"), "empty");
  await p.evaluate(() => stage.spec({ kind: "timeline", title: "Lately", items: [{ at: "11-20 14:02", label: "parked from voice", detail: "d" }, { at: "11-19", label: "CI failed" }] }));
  assert.equal(await p.locator("ol.timeline li").count(), 2);
  assert.equal(await p.locator("ol.timeline time").first().textContent(), "11-20 14:02");
  await p.evaluate(() => stage.spec({ kind: "diff", title: "2 files changed", files: [{ path: "scripts/dwim.py", add: 300, del: 2 }, { path: "app/stage.html", add: "x", del: 10 }],
    lines: [{ t: "@", s: "@@ -1 +1 @@" }, { t: "+", s: "<script>x</script>" }, { t: "-", s: "old" }] }));
  assert.equal(await p.locator(".diff .f").count(), 2);
  assert.equal(await p.textContent(".diff-sum"), "+300 −12");  // a bad count is 0
  assert.equal(await p.locator("pre.hunk span.a").textContent(), "+<script>x</script>\n");
  assert.equal(await p.locator("pre.hunk script").count(), 0);
  await p.evaluate(() => stage.spec({ kind: "lifecycle", eyebrow: "auth-login", title: "CI failed", at: "3.2", holder: "your agent",
    stages: [{ label: "Define", state: "done" }, { label: "Build", state: "done" }, { label: "Verify", state: "now", detail: "Pipeline green" }, { label: "Review", state: "bogus" }, { label: "Land" }] }));
  assert.equal(await p.locator("ol.life li").count(), 5);
  assert.equal(await p.locator("ol.life li.now").textContent(), "VerifyPipeline green");
  assert.equal(await p.locator("ol.life li.todo").count(), 2);
  assert.match(await p.textContent(".foot"), /at 3\.2 · with your agent/);
  await p.evaluate(() => stage.spec({ kind: "lifecycle", title: "t", stages: [{ label: "Verify", state: "now", tone: "bad" }] }));
  assert.equal(await p.getAttribute("ol.life li.now svg.tone", "aria-label"), "Failed");  // a failure carries the crashed glyph
  await p.evaluate(() => stage.spec({ kind: "timeline", title: "t", items: [{ at: "1", label: "CI failed", tone: "bad" }, { at: "2", label: "x", tone: "nope" }] }));
  assert.equal(await p.locator("ol.timeline svg.tone").count(), 1);
  const KINDS = { table: { columns: ["a"], rows: [["x"]] }, timeline: { items: [{ at: "1", label: "x" }] }, diff: { files: [{ path: "x", add: 1, del: 1 }] },
    lifecycle: { stages: [{ label: "x", state: "now" }] } };
  for (const look of ["default", "phosphor", "orb"]) for (const [kind, o] of Object.entries(KINDS)) {
    await p.evaluate(([l, o]) => { document.documentElement.dataset.look = l; stage.spec(o); }, [look, { kind, title: "t", ...o }]);
    const sel = { table: "table.grid td", timeline: "ol.timeline b", diff: ".diff .p", lifecycle: "ol.life b" }[kind];
    assert.ok(await p.isVisible(sel), `${look} ${kind}`);
    assert.equal(await p.locator(sel).first().textContent(), "x", `${look} ${kind}`);
  }
  await p.evaluate(() => { delete document.documentElement.dataset.look; });
  await p.evaluate(() => stage.wait("One minute", "Scanning"));
  assert.match(await p.textContent("#s"), /One minute\s*Scanning/);
  for (const look of ["default", "phosphor", "orb"]) {  // every look keeps the text visible
    await p.evaluate(l => { document.documentElement.dataset.look = l; stage.spec({ title: "t", kind: "stat", stat: "3", items: [{ label: "ci", detail: "passed" }] }); }, look);
    assert.ok(await p.isVisible(".items.stat span"), look);
  }
  // the [screen] text: the spoken gist as a lead, the detail's markdown read as structure and drawn as text only
  await p.evaluate(() => { delete document.documentElement.dataset.look; stage.text({ lead: "Two reviews need you.",
    md: "## Waiting\n- **!4834** fixes `ws_state.py`\n- [!5013](https://x/5013) adds tests\n  and docs\n\n1. merge\n2. deploy\n\nLatest commit abc1234.\n<img src=x onerror=alert(1)>\n```\n<script>x</script>\n```" }); });
  assert.equal(await p.textContent("#s.doc .lead"), "Two reviews need you.");
  assert.equal(await p.textContent(".doc h2"), "Waiting");
  assert.deepEqual(await p.locator(".doc ul li").allTextContents(), ["!4834 fixes ws_state.py", "!5013 adds tests and docs"]);
  assert.equal(await p.textContent(".doc ul li b"), "!4834");
  assert.equal(await p.textContent(".doc ul li code"), "ws_state.py");
  assert.deepEqual(await p.locator(".doc ol li").allTextContents(), ["merge", "deploy"]);
  assert.equal(await p.locator(".doc a, .doc img, .doc script").count(), 0);  // links as text; markup never runs
  assert.equal(await p.textContent(".doc pre"), "<script>x</script>");
  assert.match(await p.textContent(".doc"), /Latest commit abc1234\.<img src=x onerror=alert\(1\)>/);  // the screen keeps the hash (and the line break)
  // a pipe table becomes the grid, its cells text; numbered steps split by code keep counting
  await p.evaluate(() => stage.text({ md: "| MR | CI |\n|---|:--:|\n| **!1** | <b>ok</b> |\n| !2 | failed |\n\n1. build\n```\nmake\n```\n2. deploy" }));
  assert.deepEqual(await p.locator(".doc table.grid th").allTextContents(), ["MR", "CI"]);
  assert.deepEqual(await p.locator(".doc table.grid tbody td").allTextContents(), ["!1", "<b>ok</b>", "!2", "failed"]);
  assert.equal(await p.locator(".doc table.grid td b").textContent(), "!1");
  assert.deepEqual(await p.$$eval(".doc ol", ls => ls.map(l => l.start)), [1, 2]);
  await p.evaluate(() => stage.text({ md: "1. **[!5355](https://x/5355): decode fix.** Review it, `now`." }));  // Chief bolds its links
  assert.equal(await p.textContent(".doc ol li"), "!5355: decode fix. Review it, now.");
  assert.equal(await p.textContent(".doc ol li b"), "!5355: decode fix.");
  // lines keep their breaks; emphasis, a quote and a rule are drawn, not shown as marks; snake_case stays as written
  await p.evaluate(() => stage.text({ md: "CI: green\nReview: *waiting* on _Sam_\n\n> held by its goal\n> until Friday\n\n---\nsee my_var_name" }));
  assert.equal(await p.locator(".doc p").first().locator("br").count(), 1);
  assert.deepEqual(await p.locator(".doc em").allTextContents(), ["waiting", "Sam"]);
  assert.equal(await p.textContent(".doc p.quote"), "held by its goaluntil Friday");
  assert.equal(await p.locator(".doc hr").count(), 1);
  assert.equal(await p.textContent(".doc p:last-child"), "see my_var_name");
  await p.evaluate(() => stage.text({ md: "**!12 — *blocked* on review**\n\nTouches *.py and *.js files" }));  // em in bold; globs aren't em
  assert.equal(await p.textContent(".doc p b"), "!12 — blocked on review");
  assert.equal(await p.textContent(".doc p b em"), "blocked");
  assert.equal(await p.textContent(".doc p:last-child"), "Touches *.py and *.js files");
  assert.equal(await p.locator(".doc p:last-child em").count(), 0);
  // bold+em, trailing/leading em in bold, inline code inside emphasis, snake_case in _em_, formatted links
  await p.evaluate(() => stage.text({ md: "**!12 — *blocked*** and ***urgent*** and ***first* rest**\n\n**Use `**kwargs` here** · *Touches `*.py` files* · _fix ws_state.py_\n\n[**!5013**](https://x/5013) in [`ws_state.py`](https://x/ws)" }));
  assert.deepEqual(await p.locator(".doc p").nth(0).locator("b").allTextContents(), ["!12 — blocked", "urgent", "first rest"]);
  assert.deepEqual(await p.locator(".doc p").nth(0).locator("b em").allTextContents(), ["blocked", "urgent", "first"]);
  assert.equal(await p.locator(".doc p").nth(1).locator("b code").textContent(), "**kwargs");
  assert.equal(await p.locator(".doc p").nth(1).locator("em code").textContent(), "*.py");
  assert.deepEqual(await p.locator(".doc p").nth(1).locator("em").allTextContents(), ["Touches *.py files", "fix ws_state.py"]);
  assert.equal(await p.locator(".doc p").nth(2).locator("b").textContent(), "!5013");
  assert.equal(await p.locator(".doc p").nth(2).locator("code").textContent(), "ws_state.py");
  // unmatched * / ` runs stay literal (never recurse/stack-overflow), unclosed brackets stay linear, inline triple backticks aren't fences
  const t0 = Date.now();
  await p.evaluate(() => stage.text({ md: "- *****\n- " + "*".repeat(10000) + "\n\nUse ``` for fences\n\n```stage``` blocks draw charts.\n\n" + "[a](".repeat(16000) }));
  assert.ok(Date.now() - t0 < 2000, "inline stayed linear on unclosed brackets and long star runs");
  assert.equal(await p.locator(".doc ul li").nth(0).textContent(), "*****");
  assert.equal(await p.locator(".doc ul li b").count(), 0);
  assert.equal(await p.locator(".doc pre").count(), 0);
  assert.equal(await p.locator(".doc p").nth(0).locator("code").count(), 0);
  assert.equal(await p.locator(".doc p").nth(1).textContent(), "stage blocks draw charts.");
  assert.equal(await p.locator(".doc p").nth(1).locator("code").textContent(), "stage");
  // pipe tables keep pipes inside inline code and escaped pipes, and keep `-` data rows after the header divider
  await p.evaluate(() => stage.text({ md: "| Command | Ok |\n|---|---|\n| `a | b` | a \\| b |\n| - | - |" }));
  assert.deepEqual(await p.locator(".doc table.grid tbody td").allTextContents(), ["a | b", "a | b", "-", "-"]);
  assert.equal(await p.locator(".doc table.grid tbody td code").textContent(), "a | b");
  await p.evaluate(() => stage.text({ md: "just text" }));
  assert.equal(await p.locator(".doc .lead").count(), 0);
  assert.equal(await p.textContent(".doc p"), "just text");
  await p.evaluate(() => stage.text(null));
  assert.equal(await p.locator("#s.doc").count(), 1);
  for (const look of ["default", "phosphor"]) {
    await p.evaluate(l => { document.documentElement.dataset.look = l; stage.text({ lead: "Gist.", md: "- one" }); }, look);
    assert.ok(await p.isVisible(".doc li"), look);
  }
  await p.evaluate(() => { delete document.documentElement.dataset.look; });
  // the status line (issue 176): hidden until there's one, text only, updated in place, kept across cards, cleared by ""
  assert.equal(await p.isVisible("#status"), false);
  await p.evaluate(() => stage.status("Checking <b>PR 153</b>'s CI."));
  assert.equal(await p.textContent("#status span"), "Checking <b>PR 153</b>'s CI.");
  assert.equal(await p.locator("#status b").count(), 0);
  assert.equal(await p.getAttribute("#status svg.tone", "aria-label"), "Now");  // the working glyph, not colour alone
  assert.ok(await p.evaluate(() => document.body.classList.contains("status")));  // the card makes room above it
  await p.evaluate(() => { document.querySelector("#status span").dataset.mark = "same"; stage.status("Still waiting on the test run."); });
  assert.equal(await p.getAttribute("#status span", "data-mark"), "same");  // in place, no re-entry
  assert.equal(await p.textContent("#status span"), "Still waiting on the test run.");
  await p.evaluate(() => stage.spec({ title: "new card", kind: "list", items: [] }));
  assert.ok(await p.isVisible("#status"));  // a new card doesn't take it down
  assert.equal(await p.$eval("#status", e => getComputedStyle(e).position), "fixed");
  await p.evaluate(() => stage.status(""));
  assert.equal(await p.isVisible("#status"), false);
  assert.ok(await p.evaluate(() => !document.body.classList.contains("status")));
  await p.evaluate(() => stage.status(null));
  assert.equal(await p.isVisible("#status"), false);
  await p.evaluate(() => { stage.status("Reading the logs."); document.documentElement.dataset.look = "phosphor"; });
  assert.equal(await p.isVisible("#status svg.tone"), false);  // Phosphor: a $ prompt instead of the glyph
  assert.equal(await p.$eval("#status span", e => getComputedStyle(e, "::before").content), '"$ "');
  await p.evaluate(() => { stage.html("<p>page</p>"); });  // Chief's own page ends above the strip, in every look
  assert.ok(await p.evaluate(() => document.getElementById("s").getBoundingClientRect().bottom <= document.getElementById("status").getBoundingClientRect().top + 0.5));
  await p.evaluate(() => { delete document.documentElement.dataset.look; });
  assert.ok(await p.evaluate(() => document.getElementById("s").getBoundingClientRect().bottom <= document.getElementById("status").getBoundingClientRect().top + 0.5));
  await p.evaluate(() => stage.status(""));
  assert.ok(await p.evaluate(() => document.getElementById("s").getBoundingClientRect().bottom >= innerHeight - 0.5));  // and fills it again without one
  // narrow stage wraps long inline code paths/hashes without horizontal overflow, and replacing a scrolled doc resets scroll to top
  await p.setViewportSize({ width: 340, height: 320 });
  await p.evaluate(() => stage.text({ lead: "Long items.",
    md: Array.from({ length: 24 }, (_, i) => `- \`scripts/deeply/nested/module_${i}.py\` at \`0123456789abcdef0123456789abcdef01234567\``).join("\n") }));
  assert.ok(await p.evaluate(() => document.body.scrollWidth <= window.innerWidth + 1 && document.documentElement.scrollWidth <= window.innerWidth + 1), "narrow .doc wraps long inline code");
  await p.evaluate(() => { document.body.scrollTop = 300; window.scrollTo(0, 300); });
  assert.ok(await p.evaluate(() => Math.max(window.scrollY, document.body.scrollTop, document.documentElement.scrollTop) > 0), "scrolled before replacement");
  await p.evaluate(() => stage.text({ lead: "Next long reply.", md: Array.from({ length: 24 }, (_, i) => `- next item ${i}`).join("\n") }));
  assert.equal(await p.evaluate(() => Math.max(window.scrollY, document.body.scrollTop, document.documentElement.scrollTop)), 0, "new stage render resets scroll to top");
  // streaming (issue 208): the [screen] text drawn as it's written. Settled blocks keep their nodes, the last is patched in
  // place, new ones are appended and rise in; the scroll stays; a caret on the block being written; half-written markdown waits.
  await p.setViewportSize({ width: 640, height: 720 });
  await p.evaluate(() => stage.text({ lead: "Two reviews need you.", md: "First para", live: true }));
  assert.equal(await p.locator("#s.doc.streaming").count(), 1);
  assert.equal(await p.locator(".doc .caret").count(), 1);
  assert.equal(await p.$eval(".doc .caret", c => c.parentElement.tagName), "P");
  await p.evaluate(() => { document.querySelector(".doc .lead").dataset.mark = "lead"; document.querySelector(".doc p:not(.lead)").dataset.mark = "p1"; });
  await p.evaluate(() => stage.text({ lead: "Two reviews need you.", md: "First paragraph.\n\n- one\n- tw", live: true }));
  assert.equal(await p.getAttribute(".doc .lead", "data-mark"), "lead");
  assert.equal(await p.getAttribute(".doc p:not(.lead)", "data-mark"), "p1");  // patched in place, not redrawn
  assert.equal(await p.textContent(".doc p:not(.lead)"), "First paragraph.");
  assert.equal(await p.locator(".doc p.fresh:not(.lead)").count(), 0);  // the patched block doesn't rise again
  assert.equal(await p.locator(".doc ul.fresh").count(), 1);  // the new list rises in
  assert.equal(await p.$eval(".doc .caret", c => c.parentElement.textContent), "tw");  // on the item being written
  await p.evaluate(() => { document.querySelector(".doc li").dataset.mark = "li1"; });
  await p.evaluate(() => stage.text({ lead: "Two reviews need you.", md: "First paragraph.\n\n- one\n- two\n- three **bo", live: true }));
  assert.equal(await p.getAttribute(".doc li", "data-mark"), "li1");  // earlier items keep their nodes
  assert.deepEqual(await p.locator(".doc li").allTextContents(), ["one", "two", "three"]);  // the unclosed ** waits
  assert.equal(await p.locator(".doc li.fresh").count(), 1);
  for (const [md, want] of [["see `scr", "see"], ["open [!12](https://x/1", "open"], ["open [!12", "open"], ["a [!12](u) b", "a !12 b"],
                            ["**done** and `x`", "done and x"], ["```\nmake **", "make **"],
                            // the reviewers' repros: a marker at an item's or heading's start, ** in code, single emphasis, bare markers
                            ["Intro.\n\n- one\n- **two", "Intro.one"], ["1. **merge", ""], ["## **Bold", ""], ["hello\n-", "hello"],
                            ["Para\n``", "Para"], ["use `**` for bold", "use ** for bold"], ["`arr[`", "arr["], ["a *em", "a"],
                            ["a *em* b", "a em b"], ["x ***both**", "x"], ["see _Sam", "see"], ["a b _", "a b"], ["see ``foo", "see"], ["see ``ab`` ok", "see ab ok"], ["Totals: 3 * 4 = 12 and *don", "Totals: 3 * 4 = 12 and"], ["x\n\n1", "x"], ["my_var_name", "my_var_name"],
                            ["- [ ] task", "[ ] task"], ["```\nmake\n``", "make"], ["* a bullet", "a bullet"], ["```x``` inline", "x inline"]]) {
    await p.evaluate(m => stage.text({ md: "", live: false }) || stage.text({ md: m, live: true }), md);
    assert.equal((await p.textContent("#s")).trim(), want, md);
  }
  // a long stream keeps its scroll: no jump back to the top while it grows
  await p.evaluate(() => stage.text({ lead: "Long.", md: Array.from({ length: 40 }, (_, i) => `- item ${i}`).join("\n"), live: true }));
  await p.evaluate(() => { document.body.scrollTop = 200; window.scrollTo(0, 200); });
  const before = await p.evaluate(() => Math.max(window.scrollY, document.body.scrollTop));
  await p.evaluate(() => stage.text({ lead: "Long.", md: Array.from({ length: 44 }, (_, i) => `- item ${i}`).join("\n"), live: true }));
  assert.ok(before > 0 && await p.evaluate(() => Math.max(window.scrollY, document.body.scrollTop)) === before, "streaming keeps the scroll");
  // the end: same page as a one-shot draw of the whole text, the caret gone, the stream closed
  const md = "Intro.\n\n## Next\n\n1. **merge** !12\n2. rebase `main`\n\n| a | b |\n|---|---|\n| 1 | 2 |";
  await p.evaluate(m => { stage.wait("One minute", ""); for (let i = 1; i <= m.length; i += 7) stage.text({ lead: "Gist.", md: m.slice(0, i), live: true });
                          stage.text({ lead: "Gist.", md: m }); }, md);
  const streamed = await p.$eval("#s", e => [e.className, e.innerHTML.replace(/ class="fresh"| fresh/g, "")]);
  await p.evaluate(m => { stage.wait("One minute", ""); stage.text({ lead: "Gist.", md: m }); }, md);
  const oneShot = await p.$eval("#s", e => [e.className, e.innerHTML]);
  assert.deepEqual(streamed, oneShot, "a finished stream leaves the card a one-shot draw does");
  // a table whose header row gains an empty cell: same text, another column, which the finished card has too
  const tmd = "| one |\n|---|\n| 1 |";
  await p.evaluate(m => { stage.wait("x", ""); stage.text({ md: "| one |", live: true }); stage.text({ md: "| one ||", live: true }); stage.text({ md: m.replace("| one |", "| one ||") }); }, tmd);
  const tStreamed = await p.$eval("#s", e => e.innerHTML.replace(/ class="fresh"| fresh/g, ""));
  await p.evaluate(m => { stage.wait("x", ""); stage.text({ md: m.replace("| one |", "| one ||") }); }, tmd);
  assert.equal(tStreamed, await p.$eval("#s", e => e.innerHTML), "a table that gains a column ends as a one-shot draw");
  assert.equal(await p.locator(".caret").count(), 0);
  // cards are top-aligned, and new lines rise in once (opacity and transform), with no motion under reduced motion
  await p.evaluate(() => stage.spec({ title: "short", kind: "list", items: [] }));  // a short card sits at the top, not centred
  assert.ok(await p.$eval("#s h1", e => e.getBoundingClientRect().top) < 40, "top-aligned");
  await p.evaluate(() => { stage.wait("x", ""); stage.text({ md: "a", live: true }); stage.text({ md: "a\n\nb", live: true }); });
  assert.equal(await p.$eval(".doc p.fresh", e => getComputedStyle(e).animationName), "rise");
  assert.equal(await p.$eval(".doc p.fresh", e => getComputedStyle(e).animationIterationCount), "1");
  await p.emulateMedia({ reducedMotion: "reduce" });
  assert.equal(await p.$eval(".doc p.fresh", e => getComputedStyle(e).animationName), "none");
  await p.emulateMedia({ reducedMotion: "no-preference" });
  // markup in a streamed reply is text, as in a one-shot draw
  await p.evaluate(() => stage.text({ md: "<img src=x onerror=alert(1)> and **<b>x</b>**", live: true }));
  assert.equal(await p.locator(".doc img, .doc p > b > b").count(), 0);
  // thinking (issue 208 phase 2): a card already up stays, muted, its nodes untouched; with none, a still skeleton; the status
  // strip says what the turn is doing, in the working colour after its glyph; the next drawing replaces either
  await p.evaluate(() => { stage.status(""); stage.text({ lead: "Last answer.", md: "- one\n- two" }); document.querySelector(".doc li").dataset.mark = "kept"; stage.hold("Looking up !12"); });
  assert.equal(await p.locator("#s.doc.held").count(), 1);
  assert.equal(await p.getAttribute(".doc li", "data-mark"), "kept");
  const muted = await p.evaluate(() => { const c = getComputedStyle(document.documentElement).getPropertyValue("--ds-fg-muted").trim(), t = document.createElement("i");
    t.style.color = c; document.body.append(t); const want = getComputedStyle(t).color; t.remove(); return [getComputedStyle(document.querySelector(".doc .lead")).color, want]; });
  assert.equal(muted[0], muted[1], "the held card is muted");
  assert.equal(await p.$eval("#s.held", e => getComputedStyle(e, "::before").content), '"Last answer"');  // labelled, not colour alone
  assert.equal(await p.textContent("#status span"), "Looking up !12");
  assert.equal(await p.getAttribute("#status svg.tone", "aria-label"), "Now");  // the glyph with the colour
  const nowCol = await p.evaluate(() => { const t = document.createElement("i"); t.style.color = "var(--now)"; document.body.append(t); const c = getComputedStyle(t).color; t.remove(); return c; });
  assert.equal(await p.$eval("#status span", e => getComputedStyle(e).color), nowCol);
  await p.evaluate(() => { stage.chart({ chart: "bar", title: "t", labels: ["a", "b"], series: [{ name: "n", values: [1, 3] }] }); stage.hold("x"); });
  assert.equal(await p.$eval(".track i.peak", e => getComputedStyle(e).backgroundColor), await p.$eval(".track i:not(.peak)", e => getComputedStyle(e).backgroundColor));
  await p.evaluate(() => { stage.text({ lead: "Last answer.", md: "- one\n- two" }); document.querySelector(".doc li").dataset.mark = "kept"; stage.hold("Looking up !12"); });
  await p.evaluate(() => { stage.html("<p style='color:red'>page</p>"); stage.hold("x"); });  // Chief's own page can't take the colour: dimmed
  assert.equal(await p.$eval("#s.held iframe", e => getComputedStyle(e).opacity), "0.6");
  await p.evaluate(() => { stage.text({ lead: "Last answer.", md: "- one\n- two" }); document.querySelector(".doc li").dataset.mark = "kept"; stage.hold("Looking up !12"); });
  await p.evaluate(() => stage.hold("Still looking"));  // a second hold keeps the same held card
  assert.equal(await p.getAttribute(".doc li", "data-mark"), "kept");
  await p.evaluate(() => stage.spec({ kind: "table", title: "Board", columns: ["MR"], rows: [["!12"]] }));  // the instant card wins
  assert.equal(await p.locator("#s.held").count(), 0);
  assert.ok(await p.isVisible("#status"));  // the strip stays until Chief speaks
  for (const before of [() => stage.wait("Listening", "Say something."), () => stage.status("")]) {
    await p.evaluate(before); await p.evaluate(() => { stage.wait("Listening", "Say something."); stage.hold("Thinking"); });
    assert.equal(await p.locator("#s.skeleton .skel i").count(), 5);
    assert.equal(await p.locator("#s.skeleton .skel i.lead").count(), 1);
    assert.equal(await p.$eval(".skel i.lead", e => e.getBoundingClientRect().height), 12);
    assert.equal(await p.$eval(".skel i:not(.lead)", e => [e.getBoundingClientRect().height, getComputedStyle(e).borderTopLeftRadius].join()), "8,4px");
    assert.equal(await p.$eval(".skel i", e => getComputedStyle(e).animationName), "none");  // no shimmer
    assert.equal(await p.locator("#s.skeleton h1, #s.skeleton .cursor").count(), 0);  // no wait card
  }
  await p.evaluate(() => stage.hold(null));
  assert.equal(await p.locator("#s.skeleton").count(), 1);
  await p.evaluate(() => stage.text({ lead: "Gist.", md: "- a", live: true }));  // the stream replaces the skeleton
  assert.equal(await p.locator("#s.skeleton, .skel").count(), 0);
  for (const scheme of ["light", "dark"]) {  // the skeleton's bars show on the stage in both themes
    await p.emulateMedia({ colorScheme: scheme }); await p.evaluate(() => { stage.wait("Listening", ""); stage.hold("x"); });
    assert.notEqual(await p.$eval(".skel i", e => getComputedStyle(e).backgroundColor), await p.$eval("body", e => getComputedStyle(e).backgroundColor), scheme);
  }
  await p.emulateMedia({ colorScheme: "light" }); await p.evaluate(() => stage.status(""));
  console.log("stage ok"); await b.close();
})().catch(e => { console.error(e); process.exit(1); });
