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
  assert.equal(await p.locator(".doc img, .doc script").count(), 0);  // markup never runs
  assert.deepEqual(await p.$$eval(".doc a", as => as.map(a => [a.getAttribute("href"), a.textContent, a.attributes.length])),
    [["https://x/5013", "!5013", 1]]);  // the one web link is a link (issue 208 phase 5): its href and nothing else
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
  assert.ok(await p.evaluate(() => (s => s.offsetTop + s.offsetHeight)(document.getElementById("s")) <= document.getElementById("status").offsetTop + 0.5));
  await p.evaluate(() => { delete document.documentElement.dataset.look; });
  assert.ok(await p.evaluate(() => (s => s.offsetTop + s.offsetHeight)(document.getElementById("s")) <= document.getElementById("status").offsetTop + 0.5));
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
  // caret stays out of void <hr> and empty <tbody> while streaming; list items patch markup changes; 4-tick fences stay open over 3-tick lines
  await p.evaluate(() => { stage.wait("x", ""); stage.text({ md: "Intro.\n\n---", live: true }); });
  assert.equal(await p.$eval(".doc .caret", c => c.parentElement.id), "s");
  await p.evaluate(() => stage.text({ md: "Intro.\n\n---\n\nAfter.", live: true }));
  assert.equal(await p.$eval(".doc .caret", c => c.parentElement.tagName), "P");
  await p.evaluate(() => { stage.wait("x", ""); stage.text({ md: "| PR | CI |", live: true }); });
  assert.equal(await p.$eval(".doc .caret", c => c.parentElement.tagName), "TH");
  await p.evaluate(() => stage.text({ md: "| PR | CI |\n|---|---|\n| !1 | ok |", live: true }));
  assert.equal(await p.$eval(".doc .caret", c => c.parentElement.tagName), "TD");
  await p.evaluate(() => { stage.wait("x", ""); stage.text({ md: "- alpha", live: true }); stage.text({ md: "- **alpha**" }); });
  assert.equal(await p.$eval(".doc ul li", e => e.innerHTML), "<b>alpha</b>");
  await p.evaluate(() => { stage.wait("x", ""); stage.text({ md: "2. second", live: true }); stage.text({ md: "1. first" }); });
  assert.equal(await p.$eval(".doc ol", e => e.start), 1);
  await p.evaluate(() => { stage.wait("x", ""); stage.text({ md: "````md\n```\n**raw", live: true }); });
  assert.equal(await p.textContent(".doc pre"), "```\n**raw");
  await p.evaluate(() => { stage.wait("x", ""); stage.text({ md: "````md\ncode\n```", live: true }); });
  assert.equal(await p.textContent(".doc pre"), "code");
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
  assert.ok(await p.evaluate(() => document.body.scrollHeight <= document.body.clientHeight && document.scrollingElement.scrollHeight <= innerHeight), "a held page doesn't overflow the stage");
  assert.ok(await p.$eval("#s.held", e => parseFloat(getComputedStyle(e, "::before").paddingLeft)) >= 24, "its label keeps the gutter");
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
  // holding mid-stream ends the old stream so the next turn's stream draws a fresh, unheld card with a visible caret
  await p.evaluate(() => stage.hold("Next turn"));
  assert.equal(await p.locator("#s.doc.held:not(.streaming)").count(), 1);
  assert.equal(await p.locator(".caret").count(), 0);
  await p.evaluate(() => stage.text({ lead: "Next.", md: "- b", live: true }));
  assert.equal(await p.locator("#s.doc.streaming:not(.held)").count(), 1);
  assert.equal(await p.locator(".caret").count(), 1);
  assert.notEqual(await p.$eval(".caret", c => getComputedStyle(c).display), "none");
  for (const scheme of ["light", "dark"]) {  // the skeleton's bars show on the stage in both themes
    await p.emulateMedia({ colorScheme: scheme }); await p.evaluate(() => { stage.wait("Listening", ""); stage.hold("x"); });
    assert.notEqual(await p.$eval(".skel i", e => getComputedStyle(e).backgroundColor), await p.$eval("body", e => getComputedStyle(e).backgroundColor), scheme);
  }
  await p.emulateMedia({ colorScheme: "light" }); await p.evaluate(() => stage.status(""));
  // The thinking field (DESIGN.md §10.18): needles while Chief works, clear of the wait card's words; the card that follows waits
  // for them to land, then enters as ever. Your turn, Reduce Motion and Settings ▸ Thinking motion off all leave the stage still.
  await p.setViewportSize({ width: 800, height: 500 });
  const fxNow = () => p.evaluate(() => document.body.dataset.fx), born = () => p.$eval("#s", e => e.classList.contains("born"));
  const settled = () => p.waitForFunction(() => !document.body.dataset.fx && !document.getElementById("s").classList.contains("born"));
  await p.evaluate(() => { stage.wait("Listening", ""); stage.hold("Working"); });
  assert.equal(await fxNow(), "think", "a held skeleton starts the thinking field");
  await p.waitForTimeout(450);
  await p.evaluate(() => stage.text({ lead: "Answer", md: "First line", live: true }));
  assert.equal(await born(), false, "the first streamed text is not delayed by the thinking field");
  assert.equal(await p.textContent("#s p:not(.lead)"), "First line");
  await p.evaluate(() => stage.hold("Working again"));
  assert.equal(await fxNow(), "think", "a held answer starts the thinking field");
  const ink = title => p.evaluate(title => {  // how many pixels the field has inked: over the stage, or just over the wait card's title
    const c = document.getElementById("fx"), d = c.width / innerWidth; let box = { left: 0, top: 0, width: innerWidth, height: innerHeight };
    if (title) { const r = document.createRange(); r.selectNodeContents(document.querySelector("h1").firstChild); box = r.getBoundingClientRect(); }
    const px = c.getContext("2d").getImageData(Math.round(box.left * d), Math.round(box.top * d), Math.max(1, Math.round(box.width * d)), Math.max(1, Math.round(box.height * d))).data;
    let n = 0; for (let i = 3; i < px.length; i += 4) if (px[i] > 8) n++; return n; }, title);
  await p.evaluate(() => stage.wait("Listening", "")); await settled();
  assert.equal(await ink(), 0);  // your turn: nothing moves
  await p.evaluate(() => stage.wait("One minute", "Reading the diff"));
  assert.equal(await fxNow(), "think");
  await p.waitForTimeout(700);
  assert.ok(await ink() > 2000, "needles are drawn while Chief works");
  assert.equal(await ink(true), 0, "and keep clear of the wait card's words");
  assert.equal(await p.$eval("#fx", e => getComputedStyle(e).pointerEvents + e.getAttribute("aria-hidden")), "nonetrue");  // decoration: not in the way, not read out
  await p.evaluate(() => stage.wait("One minute", "Still reading"));  // a new status line: the field carries on
  assert.equal(await fxNow(), "think");
  await p.evaluate(() => stage.spec({ title: "Three workspaces need you", kind: "list", items: [{ label: "one", detail: "review is in" }, { label: "two", detail: "ready to merge" }] }));
  assert.equal(await fxNow(), "reveal");
  assert.equal(await p.$eval("#s", e => getComputedStyle(e).opacity), "0", "the card waits for the needles to land");
  await p.evaluate(() => stage.spec({ title: "Four workspaces need you", kind: "list", items: [{ label: "one" }] }));  // a newer card mid-reveal takes over
  assert.ok(await born());
  await p.waitForFunction(() => !document.getElementById("s").classList.contains("born"));
  assert.equal(await p.$eval("#s", e => getComputedStyle(e).animationName), "in");  // then it enters as it always has
  assert.match(await p.textContent("h1"), /Four/);
  await settled();
  assert.equal(await ink(), 0, "and the field is gone");
  await p.evaluate(() => stage.spec({ title: "No thinking first", items: [{ label: "a" }] }));  // a card with no wait before it just enters
  assert.ok(!(await born()) && (await fxNow()) === undefined);
  await p.evaluate(() => { stage.wait("One minute", "x"); stage.spec({ title: "Fast answer", items: [{ label: "a" }] }); });  // nor one before the needles show
  assert.ok(!(await born()));
  await settled();
  await p.evaluate(() => { document.documentElement.dataset.motion = "off"; stage.wait("One minute", "x"); });  // Settings ▸ Thinking motion, off
  assert.equal(await fxNow(), undefined);
  await p.evaluate(() => stage.spec({ title: "t", items: [{ label: "a" }] }));
  assert.ok(!(await born()));
  await p.evaluate(() => { document.documentElement.dataset.motion = "on"; stage.wait("One minute", "x"); });
  assert.equal(await fxNow(), "think");
  await p.evaluate(() => { document.documentElement.dataset.motion = "off"; });  // switched off mid-thought: it stops by itself
  await settled();
  // Off mid-thought, then back on during the fade: still one animation loop, not two (each frame's callbacks share its timestamp)
  await p.evaluate(() => { const raf = window.requestAnimationFrame; window.fxTicks = [];
    window.requestAnimationFrame = cb => raf(t => { window.fxTicks.push(t); cb(t); });
    document.documentElement.dataset.motion = "on"; stage.wait("One minute", "x"); });
  await p.waitForTimeout(300);
  await p.evaluate(() => { document.documentElement.dataset.motion = "off"; });
  await p.waitForTimeout(60);
  await p.evaluate(() => { document.documentElement.dataset.motion = "on"; stage.wait("One minute", "y"); window.fxTicks = []; });
  await p.waitForTimeout(300);
  assert.ok(await p.evaluate(() => { const t = window.fxTicks; return t.length > 3 && new Set(t).size === t.length; }), "one frame loop after a switch-off mid-thought");
  await p.evaluate(() => { stage.wait("Listening", ""); }); await settled();
  await p.emulateMedia({ reducedMotion: "reduce" });
  await p.evaluate(() => stage.wait("One minute", "x"));
  assert.equal(await fxNow(), undefined);  // Reduce Motion: no loop, and the card fades in as before
  assert.equal(await p.$eval("#fx", e => getComputedStyle(e).display), "none");
  await p.evaluate(() => stage.spec({ title: "t", items: [{ label: "a" }] }));
  assert.ok(!(await born()));
  await p.emulateMedia({ reducedMotion: "no-preference" });
  await p.evaluate(() => stage.wait("One minute", "x"));
  await p.setViewportSize({ width: 640, height: 400 });  // the window changes size mid-thought: the field is laid out again
  await p.waitForTimeout(300);
  assert.ok((await fxNow()) === "think" && (await ink()) > 1000 && await p.evaluate(() => Math.abs(document.getElementById("fx").width / innerWidth - Math.min(2, devicePixelRatio)) < .01));
  await p.evaluate(() => stage.html("<p>page</p>"));  // Chief's own page has no lines to land on: the needles fall into step and fade
  assert.ok((await fxNow()) === "reveal" && await born());
  await p.setViewportSize({ width: 800, height: 500 });  // a resize mid-reveal shows the card at once
  await settled();
  await p.evaluate(() => { stage.wait("One minute", "x"); stage.wait("Listening", "Go on"); });  // your turn ends the thinking
  await settled();
  const inkWords = () => p.evaluate(() => {  // inked pixels over every line of the wait card's words
    const c = document.getElementById("fx"), g = c.getContext("2d"), d = c.width / innerWidth, r = document.createRange(), tw = document.createTreeWalker(document.getElementById("s"), NodeFilter.SHOW_TEXT);
    let n = 0; for (let t = tw.nextNode(); t; t = tw.nextNode()) { if (!t.nodeValue.trim()) continue; r.selectNodeContents(t);
      for (const b of r.getClientRects()) { const px = g.getImageData(Math.round(b.left * d), Math.round(b.top * d), Math.max(1, Math.round(b.width * d)), Math.max(1, Math.round(b.height * d))).data;
        for (let i = 3; i < px.length; i += 4) if (px[i] > 8) n++; } }
    return n; });
  for (const height of [360, 450, 522, 603, 693]) {  // the status strip lifts the wait card's words: the needles make way again
    await p.setViewportSize({ width: 800, height });
    await p.evaluate(() => stage.wait("One minute", "Reading the diff")); await p.waitForTimeout(500);
    await p.evaluate(() => stage.status("Running the tests"));
    let over = 0; for (let k = 0; k < 8; k++) { await p.waitForTimeout(120); over = Math.max(over, await inkWords()); }
    assert.equal(over, 0, `needles keep clear of the words lifted by the status strip (${height}px)`);
    await p.evaluate(() => { stage.status(""); stage.wait("Listening", ""); }); await settled();
  }
  const needle = () => p.evaluate(() => { const c = document.getElementById("fx"), px = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    for (let i = 3; i < px.length; i += 4) if (px[i] > 8) return px[i - 3] + px[i - 2] + px[i - 1]; return -1; });  // one inked pixel's r+g+b
  await p.emulateMedia({ colorScheme: "light" });
  await p.evaluate(() => stage.wait("One minute", "x")); await p.waitForTimeout(500);
  const lightInk = await needle();
  await p.emulateMedia({ colorScheme: "dark" }); await p.waitForTimeout(150);  // the window turns dark mid-thought: the needles follow
  const darkInk = await needle();
  assert.ok(lightInk >= 0 && lightInk < 300 && darkInk > 400, `needle ink follows the appearance (${lightInk} → ${darkInk})`);
  await p.emulateMedia({ colorScheme: null });
  await p.evaluate(() => stage.wait("Listening", ""));  // a resize during your turn's fade, then Chief works again: laid out anew
  await p.setViewportSize({ width: 1000, height: 600 });
  await p.evaluate(() => stage.wait("One minute", "x"));
  assert.ok(await p.evaluate(() => Math.abs(document.getElementById("fx").width / innerWidth - Math.min(2, devicePixelRatio)) < .01), "the field fits the new window");
  await p.evaluate(() => stage.wait("Listening", "")); await settled();
  await p.setViewportSize({ width: 800, height: 500 });
  for (const o of [{ chart: "bar", title: "Runs", labels: ["a", "b"], series: [{ name: "s", values: [2, 5] }] },  // ledger rows: the needles take the tracks
    { chart: "bar", stacked: true, title: "Runs", labels: ["a", "b"], series: [{ name: "ok", values: [4, 3] }, { name: "failed", values: [1, 2] }] }]) {  // bars: along their tops
    await p.evaluate(() => stage.wait("One minute", "x")); await p.waitForTimeout(450);
    await p.evaluate(o => stage.chart(o), o);
    assert.equal(await fxNow(), "reveal");
    await p.waitForTimeout(500);
    assert.ok(await ink() > 200, "the needles are on their way to the chart");
    await settled();
  }
  // a line that's only bold, opening a paragraph, is a heading (issue 208 decision 3: the [screen] rule forbids ## headings);
  // bold inside a paragraph, a bold line under another line, and bold with emphasis in it stay as they were
  await p.evaluate(() => stage.text({ md: "**Needs you:**\n- !12\n\n**Working**\n\nIntro\n**not a heading**\n\n**a *mixed* line**" }));
  assert.deepEqual(await p.locator(".doc h2").allTextContents(), ["Needs you", "Working"]);
  assert.equal(await p.locator(".doc p b").count(), 2);
  // live MR status (issue 208 phase 4): the snapshot's refs get a glyph-and-label chip beside their first mention (not in code,
  // a path or the spoken lead); a patch changes a chip in place with nothing else redrawn; a card with the same kind and title
  // changes only what differs; a chip that goes, goes; a ref the snapshot only now knows gets one
  await p.evaluate(() => { stage.wait("Listening", ""); stage.refs({ "!12": { tone: "now", label: "CI running" }, "#34": { tone: "bad", label: "CI failed" } }); });
  await p.evaluate(() => stage.text({ lead: "!12 is close.", md: "- !12 needs a review, see !12 again\n- `!12` in code, scripts/!12 and !12/file in paths\n- #34 failed, !99 unknown" }));
  assert.deepEqual(await p.$$eval(".chip", cs => cs.map(c => [c.dataset.ref, c.className, c.textContent, c.querySelector("svg.tone")?.getAttribute("aria-label") ?? ""])),
    [["!12", "chip now", "CI running", "Now"], ["#34", "chip bad", "CI failed", "Failed"]]);
  assert.equal(await p.locator(".doc .lead .chip, .doc code .chip").count(), 0);
  assert.equal(await p.$eval(".chip[data-ref='!12']", c => c.previousSibling.nodeValue.endsWith("!12")), true);  // right after the ref
  await p.evaluate(() => { document.querySelectorAll(".doc li").forEach((l, i) => l.dataset.mark = "li" + i); stage.patch({ "!12": { tone: "ok", label: "CI passed" }, "#34": null, "!99": { tone: "needs", label: "Needs you" } }); });
  assert.deepEqual(await p.$$eval(".doc li", ls => ls.map(l => l.dataset.mark)), ["li0", "li1", "li2"]);  // nothing redrawn
  assert.deepEqual(await p.$$eval(".chip", cs => cs.map(c => c.dataset.ref + ":" + c.textContent)), ["!12:CI passed", "!99:Needs you"]);
  // a ref still being written isn't chipped as a shorter one (!1 on the way to !12); a held card updated in place lands the field
  await p.evaluate(() => { stage.refs({ "!1": { tone: "bad", label: "CI failed" }, "!12": { tone: "ok", label: "CI passed" } }); stage.wait("x", ""); stage.text({ md: "see !1", live: true }); });
  assert.equal(await p.locator(".chip").count(), 0);
  await p.evaluate(() => stage.text({ md: "see !12 now", live: true }));
  assert.deepEqual(await p.$$eval(".chip", cs => cs.map(c => c.dataset.ref)), ["!12"]);
  await p.evaluate(() => { const c = { kind: "list", title: "Same", items: [{ label: "a" }] }; stage.spec(c); stage.hold("x"); stage.spec(c); });
  await p.waitForTimeout(800);  // the field fades out (or lands) over a few hundred ms
  assert.notEqual(await p.evaluate(() => document.body.dataset.fx), "think");
  await p.evaluate(() => stage.refs({ "!12": { tone: "ok", label: "CI passed" }, "!99": { tone: "needs", label: "Needs you" } }));
  // a stream with chips: settled blocks keep their nodes while it grows
  await p.evaluate(() => { stage.wait("x", ""); stage.text({ md: "First !12 here.", live: true }); document.querySelector(".doc p").dataset.mark = "p1";
    stage.text({ md: "First !12 here.\n\n- more", live: true }); stage.text({ md: "First !12 here.\n\n- more\n- and more", live: true }); });
  assert.equal(await p.getAttribute(".doc p", "data-mark"), "p1");
  assert.equal(await p.locator(".doc p .chip").count(), 1);
  // the same card again (the snapshot moved): rows kept, only the changed cell changes
  const tbl = cells => ({ kind: "table", title: "Board", columns: ["MR", "CI"], rows: cells });
  await p.evaluate(t => { stage.spec(t); document.querySelectorAll("table.grid tbody tr").forEach((r, i) => r._mark = "r" + i); }, tbl([["!12", "running"], ["!99", "passed"]]));
  await p.evaluate(t => stage.spec(t), tbl([["!12", "passed"], ["!99", "passed"]]));
  assert.deepEqual(await p.$$eval("table.grid tbody tr", rs => rs.map(r => r._mark)), ["r0", "r1"]);  // a mark that isn't an attribute: the same nodes
  assert.equal(await p.textContent("table.grid tbody tr:nth-child(1) td:nth-child(2)"), "passed");
  assert.equal(await p.locator("table.grid .chip").count(), 2);
  const cols = () => p.$$eval("table.grid th", ths => ths.map(t => t.getBoundingClientRect().left));
  const still = (a, b, msg) => assert.ok(a.length === b.length && a.every((x, i) => Math.abs(x - b[i]) <= 1), `${msg}: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`);  // sub-pixel rounding aside
  // the first chip a table gets, and its last one going, don't move its columns either
  await p.setViewportSize({ width: 800, height: 720 });
  await p.evaluate(() => { stage.refs({}); stage.spec({ kind: "table", title: "B", columns: ["Workspace", "MR", "Next step to take"], rows: [["a-very-long-workspace-name-here", "!12", "x"]] }); });
  const free = await cols(); await p.evaluate(() => stage.patch({ "!12": { tone: "bad", label: "CI failed" } }));
  still(await cols(), free, "a table's first chip doesn't move its columns");
  await p.evaluate(() => stage.patch({ "!12": null })); still(await cols(), free, "nor does its last one going");
  await p.setViewportSize({ width: 640, height: 720 });
  await p.evaluate(t => { stage.refs({ "!12": { tone: "ok", label: "CI passed" }, "!99": { tone: "needs", label: "Needs you" } }); stage.spec(t); }, tbl([["!12", "passed"], ["!99", "passed"]]));
  const colsBefore = await cols(); await p.evaluate(() => stage.patch({ "!12": { tone: "ok", label: "CI passed, 1 of 2 approvals" } }));
  still(await cols(), colsBefore, "a chip that grows doesn't move the table's columns");
  // a long chip in a tight 4-column table wraps inside its cell instead of bleeding into the next column
  await p.setViewportSize({ width: 640, height: 500 });
  await p.evaluate(() => stage.spec({ kind: "table", title: "Board4", columns: ["Workspace", "MR", "CI", "Next"],
    rows: [["stage-catalog", "!12", "passed", "Needs 1 more approval"]] }));
  assert.equal(await p.evaluate(() => {
    const c = document.querySelector("table.grid .chip").getBoundingClientRect();
    const next = document.querySelector("table.grid tbody tr td:nth-child(3)").getBoundingClientRect();
    return c.right <= next.left;
  }), true, "a long chip stays inside its table cell");
  await p.setViewportSize({ width: 800, height: 500 });
  // a held card steps its chips (label and glyph both) down to muted along with the rest of the card
  await p.evaluate(() => { stage.refs({ "!12": { tone: "bad", label: "CI failed" } }); stage.text({ md: "- !12 failed" }); stage.hold("Checking…"); });
  assert.equal(await p.evaluate(() => {
    const s = getComputedStyle(document.getElementById("s")).color;
    return getComputedStyle(document.querySelector(".chip")).color === s && getComputedStyle(document.querySelector(".chip svg.tone")).color === s;
  }), true, "held chip and its glyph step down to the held card's muted colour");
  await p.evaluate(() => stage.status(""));
  await p.evaluate(() => stage.refs({ "!12": { tone: "ok", label: "CI passed" }, "!99": { tone: "needs", label: "Needs you" } }));
  await p.evaluate(t => stage.spec(t), { ...tbl([["!12", "passed"]]), title: "Other" });  // another card: drawn fresh
  assert.deepEqual(await p.$$eval("table.grid tbody tr", rs => rs.map(r => r._mark ?? null)), [null]);
  await p.evaluate(() => stage.refs({}));
  assert.equal(await p.locator(".chip").count(), 0);
  // links and options (issue 208 phase 5): a web link is a real link (nothing else is); a linked ref's chip sits after the link;
  // the options are buttons that carry only their index; a new turn's hold takes them away; nothing inline, nothing executes
  await p.evaluate(() => { stage.refs({ "!12": { tone: "ok", label: "CI passed" } }); stage.text({ md: "See [!12](https://gitlab.com/g/p/-/merge_requests/12), [x](javascript:void0), [y](file:///etc/passwd) and [z](mailto:a@b)." }); });
  assert.deepEqual(await p.$$eval("#s a", as => as.map(a => a.getAttribute("href"))), ["https://gitlab.com/g/p/-/merge_requests/12"]);
  assert.equal(await p.$eval("#s a", a => a.nextSibling?.classList?.contains("chip")), true);
  assert.equal(await p.locator("#s a .chip").count(), 0);
  assert.equal(await p.textContent(".doc p"), "See !12CI passed, x, y and z.");
  await p.evaluate(() => stage.options(["Merge it", "<b>Wait</b>", "", "Ask Sam"]));
  assert.deepEqual(await p.$$eval(".options a.option", as => as.map(a => [a.getAttribute("href"), a.textContent])),
    [["greenlight-option:0", "Merge it"], ["greenlight-option:1", "<b>Wait</b>"], ["greenlight-option:3", "Ask Sam"]]);  // the app's index, a blank skipped
  assert.equal(await p.locator(".options b").count(), 0);
  assert.ok(await p.evaluate(() => [...document.querySelectorAll(".options a, #s a")].every(a => ![...a.attributes].some(x => x.name.startsWith("on")))));
  assert.equal(await p.$eval(".options a.option", a => getComputedStyle(a).cursor), "default");  // a button, not a link (§10.2)
  assert.equal(await p.$eval(".options a.option", a => getComputedStyle(a).height), "28px");
  assert.equal(await p.$eval(".options a.option", a => getComputedStyle(a).textDecorationLine), "none");  // a button, not an underlined link
  assert.equal(await p.$eval(".options a.option", a => getComputedStyle(a).color), await p.$eval(".doc p", e => getComputedStyle(e).color));
  await p.evaluate(() => { document.querySelector("#s a:not(.option)")?.remove(); document.activeElement?.blur(); });
  await p.keyboard.press("Tab");  // keyboard focus shows the §10.2 ring
  assert.equal(await p.evaluate(() => document.activeElement.className), "option");
  assert.notEqual(await p.evaluate(() => getComputedStyle(document.activeElement).boxShadow), "none");
  await p.evaluate(() => stage.options(["Only one"])); assert.equal(await p.locator(".options a.option").count(), 1);  // replaced, not added
  await p.evaluate(() => stage.hold("Thinking")); assert.equal(await p.locator(".options").count(), 0);
  await p.evaluate(() => { stage.text({ md: "a" }); stage.options([]); }); assert.equal(await p.locator(".options").count(), 0);
  // a link naming two known refs: both chips after it; an option naming a ref: no chip in the button; a malformed URL: no link
  await p.evaluate(() => { stage.refs({ "!12": { tone: "ok", label: "CI passed" }, "!13": { tone: "bad", label: "CI failed" } });
    stage.text({ md: "[!12 and !13](https://x/12) and [open](https://%)" }); stage.options(["Review !12"]); });
  assert.deepEqual(await p.$$eval(".doc p .chip", cs => cs.map(c => c.dataset.ref)), ["!12", "!13"]);
  assert.equal(await p.$eval(".doc p a", a => a.nextSibling.dataset.ref), "!12");
  assert.equal(await p.locator(".options .chip").count(), 0);
  assert.deepEqual(await p.$$eval(".doc p a", as => as.map(a => a.getAttribute("href"))), ["https://x/12"]);
  // streaming caret stays outside links (even with trailing text); held links mute; html()+options() keeps gutter;
  // narrow wrapped options grow instead of clipping; Phosphor options respond on hover
  await p.evaluate(() => { stage.refs({}); stage.wait("One minute", ""); stage.text({ md: "See [docs](https://x/docs) and more", live: true }); });
  assert.equal(await p.$eval(".doc .caret", c => c.parentElement.tagName), "P");
  await p.evaluate(() => stage.text({ md: "See [docs](https://x/docs)", live: true }));
  assert.equal(await p.$eval(".doc .caret", c => c.parentElement.tagName), "P");
  await p.evaluate(() => { stage.text({ md: "See [docs](https://x/docs)." }); stage.hold("Thinking"); });
  assert.equal(await p.$eval("#s a", a => getComputedStyle(a).color), await p.$eval("#s", e => getComputedStyle(e).color));
  await p.evaluate(() => { stage.status(""); stage.html("<p>custom</p>"); stage.options(["Approve", "Skip"]); });
  assert.ok(await p.$eval("#s.frame > .options a.option", a => a.getBoundingClientRect().left >= 20 && a.getBoundingClientRect().bottom <= innerHeight - 20));
  await p.setViewportSize({ width: 340, height: 520 });
  await p.evaluate(() => { stage.text({ md: "Pick" }); stage.options(["Merge it now and delete the stale branch afterwards once CI is green"]); });
  assert.ok(await p.$eval(".options a.option", a => a.scrollHeight <= a.clientHeight + 2 && a.getBoundingClientRect().height > 28));
  await p.setViewportSize({ width: 640, height: 720 });
  await p.evaluate(() => { document.documentElement.dataset.look = "phosphor"; stage.text({ md: "Pick" }); stage.options(["Merge"]); });
  const phRest = await p.$eval(".options a.option", a => getComputedStyle(a).backgroundColor);
  await p.hover(".options a.option");
  assert.notEqual(await p.$eval(".options a.option", a => getComputedStyle(a).backgroundColor), phRest);
  await p.evaluate(() => { delete document.documentElement.dataset.look; stage.refs({}); });
  // the idle board and card history (issue 208 phase 6): the board in place of Listening, updated in place; the last cards kept
  // as they were, stepped through by "go back" and the buttons, a new card the latest again; the board isn't a card
  const q = await b.newPage(); await q.setViewportSize({ width: 640, height: 720 });
  await q.goto("file://" + require("path").join(__dirname, "../app/stage.html"));  // a fresh page: its own history
  const brd = rows => ({ kind: "board", title: "", sections: [{ label: "Needs you", tone: "needs", rows }, { label: "Working", tone: "now", rows: [{ ref: "!7", title: "retry", age: "2h", why: "CI running" }] }] });
  await q.evaluate(b => { stage.refs({}); stage.wait("Listening", ""); stage.spec(b); }, brd([{ ref: "!5", title: "decode fix", age: "3h", why: "review it" }]));
  assert.deepEqual(await q.$$eval(".bhead", hs => hs.map(h => [h.textContent, h.querySelector("svg.tone")?.getAttribute("aria-label")])), [["Needs you", "Needs you"], ["Working", "Now"]]);
  assert.deepEqual(await q.$$eval("ul.board li", ls => ls.map(l => l.textContent)), ["!5decode fix3hreview it", "!7retry2hCI running"]);
  assert.equal(await q.locator("#s.board h1").count(), 0);
  await q.evaluate(() => document.querySelectorAll("ul.board li").forEach((l, i) => l._m = i));
  await q.evaluate(b => stage.spec(b), brd([{ ref: "!5", title: "decode fix", age: "4h", why: "review it" }]));  // the next snapshot
  assert.deepEqual(await q.$$eval("ul.board li", ls => ls.map(l => l._m)), [0, 1]);  // in place
  assert.equal(await q.textContent("ul.board li time"), "4h");
  assert.equal(await q.isVisible("#hist"), false);  // the board isn't a card
  // three cards, then back twice and forward: each exactly as it was; a new card returns to the latest
  await q.evaluate(() => { stage.text({ lead: "One.", md: "- a" }); document.getElementById("s")._id = 1;
    stage.text({ lead: "Two.", md: "- b", live: true }); stage.text({ lead: "Two.", md: "- b\n- c" }); document.getElementById("s")._id = 2;
    stage.spec({ kind: "list", title: "Three", items: [{ label: "x" }] }); stage.options(["Yes", "No"]); document.getElementById("s")._id = 3; });
  assert.ok(await q.isVisible("#hist"));
  assert.equal(await q.textContent("#hist .hn"), "3 of 3");
  assert.ok(await q.isVisible("#hist .hh"));  // "say go back": on the latest only
  await q.evaluate(() => stage.back());
  assert.equal(await q.evaluate(() => document.getElementById("s")._id), 2);  // the same element: exactly as it was
  assert.equal(await q.textContent("#s.doc"), "Two.bc");
  assert.equal(await q.locator("#s .caret").count(), 0);
  assert.equal(await q.textContent("#hist .hn"), "2 of 3");
  assert.equal(await q.isVisible("#hist .hh"), false);
  await q.click("#hist [data-go=back]");  // the button does the same
  assert.equal(await q.evaluate(() => document.getElementById("s")._id), 1);
  assert.ok(await q.$eval("#hist [data-go=back]", b => b.disabled));
  await q.click("#hist [data-go=forward]"); await q.click("#hist [data-go=forward]");
  assert.equal(await q.evaluate(() => document.getElementById("s")._id), 3);
  assert.ok(await q.isVisible(".options"));  // the latest card's options are live again
  await q.evaluate(() => stage.back()); assert.equal(await q.isVisible(".options"), false);  // an older card's aren't (body.past)
  assert.equal(await q.$eval("#s", e => getComputedStyle(e, "::before").content), '"Earlier"');  // and it says it's an earlier card
  await q.evaluate(() => stage.spec({ kind: "list", title: "Four", items: [{ label: "y" }] }));  // a new card: the latest
  assert.equal(await q.textContent("#hist .hn"), "4 of 4");
  // from the board, back shows the latest card and forward returns to the board
  await q.evaluate(b => { stage.spec(b); }, brd([{ ref: "!5", title: "decode fix", age: "4h", why: "review it" }]));
  assert.equal(await q.textContent("#s h1"), "Four");  // the board never draws over a card
  await q.evaluate(b => { stage.wait("Listening", ""); stage.spec(b); }, brd([{ ref: "!5", title: "decode fix", age: "4h", why: "review it" }]));  // idle again
  await q.evaluate(() => stage.back()); assert.equal(await q.textContent("#s h1"), "Four");
  await q.evaluate(() => stage.forward()); assert.equal(await q.locator("#s.board").count(), 1);
  // review fixes: a board snapshot while you're on an earlier card updates the board behind it, not the card; a new card with the
  // same kind and title as the earlier card you're on is a new card; going back mid-stream doesn't split the stream; a restored
  // card's chips are today's; a new turn strips every earlier card's options; one card and the board: forward still works
  const q2 = await b.newPage(); await q2.goto("file://" + require("path").join(__dirname, "../app/stage.html"));
  const brd2 = why => ({ kind: "board", title: "", sections: [{ label: "Needs you", tone: "needs", rows: [{ ref: "!5", title: "t", age: "1h", why }] }] });
  await q2.evaluate(b => { stage.text({ lead: "A.", md: "- a" }); stage.options(["Yes", "No"]); stage.wait("Listening", ""); stage.spec(b); }, brd2("first"));
  assert.equal(await q2.isVisible("#hist"), false);  // on the board: no history bar
  await q2.evaluate(() => stage.back());
  assert.equal(await q2.textContent("#s .lead"), "A."); assert.ok(await q2.isVisible("#hist"));
  await q2.evaluate(b => stage.spec(b), brd2("second"));  // the next snapshot
  assert.equal(await q2.textContent("#s .lead"), "A.");  // the card you went back to stays
  await q2.evaluate(() => stage.forward()); assert.equal(await q2.textContent("#s .bwhy"), "second");  // the board, up to date
  await q2.evaluate(() => { stage.spec({ kind: "list", title: "Same", items: [{ label: "old" }] }); stage.text({ lead: "B.", md: "x" }); stage.back(); stage.spec({ kind: "list", title: "Same", items: [{ label: "new" }] }); });
  assert.deepEqual(await q2.evaluate(() => [document.querySelector("#s li").textContent, document.querySelector("#hist .hn").textContent]), ["new", "4 of 4"]);
  await q2.evaluate(() => { stage.back(); stage.back(); });
  assert.equal(await q2.textContent("#s li"), "old");  // the earlier card as it was
  await q2.evaluate(() => { stage.forward(); stage.forward(); stage.text({ lead: "S.", md: "- s1", live: true }); stage.back(); stage.text({ lead: "S.", md: "- s1\n- s2", live: true }); stage.text({ lead: "S.", md: "- s1\n- s2\n- s3" }); stage.forward(); });
  assert.equal(await q2.textContent("#s.doc"), "S.s1s2s3");  // one card, the whole stream
  assert.equal(await q2.evaluate(() => document.querySelector("#hist .hn").textContent), "5 of 5");
  await q2.evaluate(() => { stage.refs({ "!5": { tone: "now", label: "CI running" } }); stage.text({ lead: "C.", md: "- !5" }); stage.text({ lead: "D.", md: "x" });
    stage.patch({ "!5": { tone: "ok", label: "CI passed" } }); stage.back(); });
  assert.equal(await q2.textContent("#s .chip"), "CI passed");  // a restored card shows today's status
  await q2.evaluate(() => { stage.forward(); stage.options(["Merge it"]); stage.hold("x"); stage.back(); });
  assert.equal(await q2.locator(".options").count(), 0);  // a new turn: no earlier card keeps its options
  const q3 = await b.newPage(); await q3.goto("file://" + require("path").join(__dirname, "../app/stage.html"));
  await q3.evaluate(b => { stage.text({ lead: "Only.", md: "x" }); stage.wait("Listening", ""); stage.spec(b); stage.back(); }, brd2("w"));
  assert.ok(await q3.isVisible("#hist")); assert.equal(await q3.$eval("#hist [data-go=forward]", b => b.disabled), false);
  await q3.evaluate(() => stage.forward()); assert.equal(await q3.locator("#s.board").count(), 1);
  for (let i = 0; i < 8; i++) await q.evaluate(n => stage.spec({ kind: "list", title: "C" + n, items: [] }), i);  // only the last few are kept
  assert.equal(await q.textContent("#hist .hn"), "6 of 6");
  assert.ok(await q.evaluate(() => ![...document.querySelectorAll("#hist button")].some(b => [...b.attributes].some(a => a.name.startsWith("on")))));
  console.log("stage ok"); await b.close();
})().catch(e => { console.error(e); process.exit(1); });
