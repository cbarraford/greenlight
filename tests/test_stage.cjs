// Headless checks for stage.html, voice mode's card: each kind renders as text only, and Chief's own HTML can't run script.
//   NODE_PATH=…/node_modules node tests/test_stage.cjs
const { chromium } = require("playwright-core");
const assert = require("assert");
(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  await p.goto("file://" + require("path").join(__dirname, "../app/stage.html"));
  assert.match(await p.textContent("#s"), /Listening/);
  await p.evaluate(() => stage.spec({ title: "3 MRs ready", kind: "steps", eyebrow: "<i>ship</i>", stat: "", items: [{ label: "<b>x</b>", detail: "d" }, { label: "two", detail: "" }] }));
  assert.equal(await p.locator("ol.items.steps li").count(), 2);
  assert.equal(await p.locator("ol li b").first().textContent(), "<b>x</b>");  // text, never markup
  assert.equal(await p.textContent(".eyebrow"), "<i>ship</i>");
  await p.evaluate(() => stage.spec({ title: "t", kind: "bogus", items: [] }));
  assert.equal(await p.locator(".items").count(), 0);
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
  await p.evaluate(() => stage.chart({ chart: "bar", title: "merged", labels: ["mon", "tue"], series: [{ name: "n", values: [3, 6] }] }));
  assert.equal(await p.locator(".bars .bar").count(), 2);  // one series: Ledger rows, not SVG
  assert.equal(await p.getAttribute(".bar:nth-child(2) .track i", "class"), "peak");
  assert.match(await p.textContent(".foot"), /total 9 · avg 4\.5 · peak tue/);
  await p.evaluate(() => stage.chart({ chart: "line", labels: ["a", "b", "c"], series: [{ name: "x", values: [1, 3, 2] }] }));
  assert.equal(await p.locator("svg.chart polyline").count(), 1);
  await p.evaluate(() => stage.chart({ chart: "pie", labels: ["a", "b"], series: [{ values: [3, 1] }] }));
  assert.equal(await p.locator("svg.chart path").count(), 2);
  await p.evaluate(() => stage.wait("One minute", "Scanning"));
  assert.match(await p.textContent("#s"), /One minute\s*Scanning/);
  for (const look of ["conversation", "orb", "onair", "phosphor", "editorial", "hud", "zen"]) {  // every look keeps the text visible
    await p.evaluate(l => { document.documentElement.dataset.look = l; stage.spec({ title: "t", kind: "stat", stat: "3", items: [{ label: "ci", detail: "passed" }] }); }, look);
    assert.ok(await p.isVisible(".items.stat span"), look);
  }
  console.log("stage ok"); await b.close();
})().catch(e => { console.error(e); process.exit(1); });
