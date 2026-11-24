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
  console.log("stage ok"); await b.close();
})().catch(e => { console.error(e); process.exit(1); });
