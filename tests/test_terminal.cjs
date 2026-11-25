// Headless checks for terminal.html: restore, the workspace sidebar, the saved layout, ⌘J, auto names.
//
//   NODE_PATH=…/node_modules node tests/test_terminal.cjs     (playwright-core, as test_dashboard.cjs)
//
// The browser is $CHROME, else Playwright's own. No ptys: the app's side is a stub that records what the page posts.
const fs = require("fs"), path = require("path"), os = require("os"), assert = require("assert");
const { launchBrowser } = require("./js_harness.cjs");

// the page loads ghostty-web.js beside it, as the app bundle has it
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "term-"));
fs.copyFileSync(path.join(__dirname, "../app/", "terminal.html"), path.join(dir, "terminal.html"));
fs.copyFileSync(path.join(__dirname, "../app/", "vendor", "ghostty-web.js"), path.join(dir, "ghostty-web.js"));
const PAGE = "file://" + path.join(dir, "terminal.html");
const TREES = {trees: [
  {ade: "orca", project: "acme/widgets", name: "widgets", path: "/w/widgets", branch: "main", main: true, agent: "none"},
  {ade: "orca", project: "acme/widgets", name: "fix-login", path: "/w/fix-login", branch: "fix-login", main: false, agent: "working"},
  {ade: "cmux", project: "octo/greenlight", name: "sidebar", path: "/w/sidebar", branch: "term-sidebar", main: false, agent: "waiting",
   state: "needs", why: "agent is waiting on your answer", mr: {ref: "!51", state: "opened", pipeline: "failed", threads: 2, rebase: true}},
  {ade: "cmux", project: "octo/greenlight", name: "shipped", path: "/w/shipped", branch: "old", main: false, agent: "none",
   state: "done", mr: {ref: "!40", state: "merged", pipeline: "failed", threads: 3}},
  {ade: "cmux", project: "octo/greenlight", name: "review", path: "/w/review", branch: "review", main: false, agent: "none", state: "quiet", why: "waiting on bob"}]};

async function boot(browser, saved = null, query = "", trees = TREES){
  const page = await browser.newPage({viewport: {width: 1200, height: 800}, colorScheme: "dark"});
  await page.addInitScript(() => {
    window.__posts = [];
    window.webkit = {messageHandlers: {term: {postMessage: m => window.__posts.push(JSON.parse(JSON.stringify(m)))}}};
  });
  const errs = []; page.on("pageerror", e => errs.push(e.message)); page._errs = errs;
  await page.goto(PAGE + query);
  await page.waitForFunction(() => window.__posts.some(m => m.cmd === "ready"));
  await page.evaluate(s => term.restore(s), saved);
  await page.evaluate(t => term.workspaces(t), trees);
  if (await page.evaluate(() => document.body.classList.contains("noside"))) await page.evaluate(() => toggleSide());  // it starts hidden (the Pipe)
  return page;
}
const posts = (page, cmd) => page.evaluate(c => window.__posts.filter(m => m.cmd === c), cmd);
const strip = page => page.$$eval("#tabs .tab", b => b.length);
const lastSave = async page => { await page.waitForTimeout(650); return (await posts(page, "save")).pop()?.state };

const tests = [];
const test = (name, fn, query) => tests.push({name, fn, query});

test("⌘W, ⌘Q, ⌘M… reach the app's menu: the terminal doesn't swallow them", async page => {
  page = await boot(page.context().browser()); await page.evaluate(() => newTab()); await page.waitForTimeout(200);
  const eat = await page.evaluate(() => ["w", "q", "m", ","].filter(k => { const e = new KeyboardEvent("keydown", {key: k, code: k === "," ? "Comma" : "Key" + k.toUpperCase(), metaKey: true, bubbles: true, cancelable: true});
    document.activeElement.dispatchEvent(e); return e.defaultPrevented }));
  assert.deepEqual(eat, []); assert.deepEqual(page._errs, []);
});

test("a workspace's + starts its own set of tabs; ⌘T adds to the set you're in; the layout keeps the sets", async page => {
  page = await boot(page.context().browser());
  const plus = () => page.evaluate(() => [...document.querySelectorAll(".ws")].find(r => r._ws === "/w/fix-login").querySelector('[title="New set of tabs here"]').click());
  await plus(); await page.keyboard.press("Meta+t"); assert.equal(await strip(page), 2, "⌘T joins the set");
  await plus(); assert.equal(await strip(page), 1, "+ starts a new set: the strip shows just it");
  assert.equal(await page.$$eval(".tm.ns", r => r.length), 1, "the sidebar marks where a set starts");
  const st = await lastSave(page), sets = st.tabs.filter(t => t.ws === "/w/fix-login").map(t => t.set);
  assert.equal(sets.length, 3); assert.equal(new Set(sets).size, 2);
  const again = await boot(page.context().browser(), st);
  await again.evaluate(() => show(tabs.find(t => t.ws === "/w/fix-login"))); assert.equal(await strip(again), 2, "restored: still two sets");
  await again.close(); await page.close();
});

test("the Basement's footer: the usage meters and their popover; the Pipe has none, and no agent reporting hides it", async page => {
  page = await boot(page.context().browser());
  const now = Math.floor(Date.now() / 1000);
  const usage = [{id: "claude", name: "Claude", state: "ok", at: now, windows: [{label: "5h", pct: 81, resets: now + 3600}, {label: "Week", pct: 40, resets: now + 86400 * 2}]},
    {id: "codex", name: "Codex", state: "ok", at: now, windows: [{label: "Week", pct: 100, resets: now + 86400}]},
    {id: "opencode", name: "OpenCode", state: "na", note: "no usage API"}, {id: "pi", name: "Pi", state: "failed", note: "signed out or the token expired"}];
  assert.equal(await page.$eval("#bfoot", f => f.hidden), true, "nothing reported yet: no footer");
  await page.evaluate(t => term.workspaces(t), {...TREES, usage});
  const m = () => page.$$eval("#bfoot .um", bs => bs.map(b => [b.dataset.id, b.textContent, b.querySelector(".ubar")?.dataset.tone || ""]));
  assert.deepEqual(await m(), [["claude", "81%1h 0m", "near"], ["codex", "100%1d 0h", "full"], ["opencode", "n/a", ""], ["pi", "Refresh failed", ""]]);
  assert.equal(await page.$eval("#bfoot", f => getComputedStyle(f).display), "flex");
  const stage = await page.$eval("#stage", s => s.getBoundingClientRect().bottom), foot = await page.$eval("#bfoot", f => f.getBoundingClientRect());
  assert.ok(Math.abs(stage - foot.top) < 1 && foot.height === 28, "under the terminals, 28 tall");
  await page.click("#bfoot .um[data-id=claude]");
  assert.deepEqual(await page.$$eval("#umpop .urow[data-id=claude] .uwin .ul", l => l.map(x => x.textContent)), ["5h", "Week"]);
  assert.match(await page.textContent("#umpop .urow[data-id=pi] .unote"), /signed out/);
  await page.keyboard.press("Escape"); assert.equal(await page.$("#umpop"), null);
  await page.click("#bfoot .um[data-id=claude]");
  await page.click("#shead [aria-label='Customize look']");
  assert.equal(await page.$("#umpop"), null, "clicking a stopPropagation toolbar button dismisses #umpop");
  await page.keyboard.press("Escape");
  await page.click("#bfoot .um[data-id=claude]");
  await page.evaluate(() => toggleSide()); await page.waitForTimeout(450);
  assert.equal(await page.$("#umpop"), null, "switching to the Pipe closes an open popover");
  assert.equal(await page.$eval("#bfoot", f => getComputedStyle(f).display), "none", "the Pipe: no footer");
  await page.evaluate(() => toggleSide()); await page.waitForTimeout(450);
  await page.setViewportSize({width: 420, height: 800});
  await page.evaluate(t => term.workspaces(t), {...TREES, usage});
  assert.equal(await page.$eval("#bfoot", f => f.classList.contains("tight")), true, "narrow Basement footer activates tight mode");
  await page.setViewportSize({width: 1200, height: 800});
  await page.evaluate(t => term.workspaces(t), TREES);
  assert.equal(await page.$eval("#bfoot", f => f.hidden), true);
  assert.deepEqual(page._errs, []); await page.close();
});

test("this Mac shows by its hostname; Rename tells the app, and the app's answer renames it", async page => {
  page = await boot(page.context().browser(), null, "?localname=cb-mbp");
  const names = () => page.$$eval("#tree .hchip, #tree .hc .ws .wn", e => e.map(x => x.textContent));
  assert.ok((await names()).includes("cb-mbp"), "the hostname, not Local");
  await page.evaluate(() => renameLocal()); await page.fill("#dlg input", "  studio "); await page.keyboard.press("Enter");
  assert.deepEqual((await posts(page, "localName")).pop(), {cmd: "localName", name: "studio"});
  await page.evaluate(() => term.localName("studio")); assert.ok((await names()).includes("studio"));
  await page.close();
});

test("the sidebar starts hidden (the Pipe); ⌘B opens it (the Basement) and tells the app (the orb up to Greenlight Center shows only while it's open)", async page => {
  page = await boot(page.context().browser(), null, "?nosidebar");
  assert.ok(await page.evaluate(() => document.body.classList.contains("noside")) === false, "boot opened it");
  assert.deepEqual((await posts(page, "side")).pop(), {cmd: "side", open: true});
  await page.keyboard.press("Meta+b"); assert.deepEqual((await posts(page, "side")).pop(), {cmd: "side", open: false});
  const sliding = () => page.evaluate(() => document.body.classList.contains("sliding"));
  assert.equal(await sliding(), true, "it slides out (the 0.32 s drawer by default)"); assert.equal(await page.$eval("#side", e => getComputedStyle(e).display), "flex", "still showing mid-slide");
  await page.waitForTimeout(400); assert.equal(await sliding(), false, "and the slide cleans up");
  assert.equal(await page.$eval("#side", e => getComputedStyle(e).display), "none");
  await page.evaluate(() => term.slide(0)); await page.evaluate(() => toggleSide());
  assert.equal(await sliding(), false, "0 in Settings: no slide, it just appears"); await page.evaluate(() => toggleSide());
  const side = () => page.evaluate(() => !document.body.classList.contains("noside"));
  await page.evaluate(() => term.opens("pipe")); assert.equal(await side(), false, "Windows open to Pipe");
  await page.evaluate(() => term.opens("basement")); assert.equal(await side(), true, "Windows open to Basement");
  await page.evaluate(() => term.opens("basement")); assert.equal(await side(), true, "and stays");
  await page.close();
});

test("no saved layout: one Local shell, on its own tmux session", async page => {
  const [o] = await posts(page, "open");
  assert.match(o.session, /^mc-[\w-]+$/); assert.equal(o.ws, "");
  assert.equal(await strip(page), 1);
});

test("the sidebar: Hosts, then status sections → projects → workspaces; Done starts shut", async page => {
  const rows = () => page.$$eval("#tree .ws, #tree .grp", d => d.map(x => x.className.replace(/ (on|main|shut)/g, "").split(" ").slice(0, 2).join(".") + ":" + x.textContent));
  let r = await rows();
  assert.equal(r[0], "grp.sec:Hosts1"); assert.match(r[1], /^ws:Local/);
  assert.deepEqual(r.filter(x => x.startsWith("grp")).map(x => x.replace(/\d+$/, "")),
    ["grp.sec:Hosts", "grp.sec:Workspaces", "grp.prj:octo/greenlight", "grp.prj:acme/widgets", "grp.sec:Waiting on others", "grp.prj:octo/greenlight", "grp.sec:Done"], "needs-you first; Done shows a count");
  assert.ok(r.indexOf("grp.sec:Waiting on others") < r.findIndex(x => x.startsWith("ws:review")), r.join(" | "));
  assert.ok(r.some(x => x.startsWith("ws:fix-login") && x.includes("Orca")), r.join(" | "));
  assert.ok(!r.some(x => x.startsWith("ws:shipped")), "Done is shut");
  assert.ok(r.some(x => x.startsWith("ws:review") && x.includes("bob")), "a waiting row says who it waits on: " + r.join(" | "));
  assert.deepEqual(await page.$$eval("#tree .box", b => b.map(x => x.className)), ["box s-host", "box s-ws", "box s-quiet", "box s-done shut"]);
  await page.click("#tree .grp.sec:has-text('Done')");
  r = await rows();
  assert.ok(r.some(x => x.startsWith("ws:shipped")), r.join(" | "));
  assert.ok((await lastSave(page)).shut.includes("§done"), "opening Done is remembered");
  await page.click("#tree .grp.sec:has-text('Workspaces')");
  assert.ok(!(await rows()).some(x => x.startsWith("ws:sidebar")), "a section collapses");
});

test("hosts: live hosts are cards, idle ones chips, most used first; × and Close all close shells", async page => {
  page = await boot(page.context().browser(), null, "?host=devbox&hosts=" + encodeURIComponent(JSON.stringify(["mini", "devbox"])));
  const view = () => page.evaluate(() => ({cards: [...document.querySelectorAll("#tree .s-host .hc .ws .wn")].map(x => x.textContent),
    chips: [...document.querySelectorAll("#tree .s-host .hchip")].map(x => x.textContent)}));
  assert.deepEqual(await view(), {cards: ["devbox"], chips: ["Local", "mini"]});
  await page.click("#tree .hchip:has-text('mini')");
  const o = (await posts(page, "open")).pop();
  assert.equal(o.ws, "@mini");
  assert.deepEqual(await view(), {cards: ["devbox", "mini"], chips: ["Local"]}, "a tie keeps the config order");
  for (const r of await page.$$("#tree .ws")) {  // hover controls (+, Close all, ⋯) appear without growing the row
    const h = (await r.boundingBox()).height; await r.hover();
    assert.equal((await r.boundingBox()).height, h, await r.textContent());
  }
  await page.hover("#tree .hc .ws:has-text('mini')"); await page.click("#tree .hc .ws:has-text('mini') .ib");
  assert.deepEqual((await view()).cards, ["mini", "devbox"], "two shells opened on mini: it sorts first");
  assert.equal((await lastSave(page)).uses["@mini"], 2, "the counts are saved with the layout");
  assert.ok(!(await page.$$eval("#tree .grp", d => d.map(x => x.textContent))).some(t => t.startsWith("No Man's Land")), "a host isn't a gone workspace");
  const tm = page.locator("#tree .hc", {hasText: "mini"}).locator(".tm").first();
  await tm.hover(); await tm.locator(".ib").click();
  assert.equal((await posts(page, "close")).pop().ws, "@mini", "a terminal row's × closes it");
  await page.hover("#tree .hc .ws:has-text('mini')"); await page.click("#tree .hc .ws:has-text('mini') .ca");
  assert.deepEqual(await view(), {cards: ["devbox"], chips: ["Local", "mini"]}, "Close all: mini goes back to a chip; Local stays first");
  await page.click("#tree .hchip:has-text('Local')");
  assert.deepEqual((await view()).cards, ["Local", "devbox"], "Local is always the first host, however little it's used");
  assert.deepEqual(page._errs, []); await page.close();
});

const PROJ = [{id: "p1", name: "Platform", repos: ["acme/widgets"], dirs: ["/notes"]}, {id: "p2", name: "Greenlight", repos: [], dirs: ["/w/review"]}];
const groups = page => page.$$eval("#tree .grp.prj", d => d.map(x => x.querySelector(".gn").textContent));
const names = page => page.$$eval("#tree .ws .wn", d => d.map(x => x.textContent));

test("projects: repos and folders group under their project; a repo in none keeps its own header", async page => {
  await page.evaluate(p => term.projects(p), PROJ);
  assert.deepEqual(await groups(page), ["octo/greenlight", "Platform", "Greenlight"]);
  assert.ok((await names(page)).includes("notes"), "a project folder is a row");
  await page.click("#tree .ws:has-text('notes')");
  assert.equal((await posts(page, "open")).pop().ws, "/notes", "it opens a shell in that folder");
  assert.deepEqual(page._errs, []);
});

test("a workspace is in its repo's project wherever its worktree sits, new ones at once", async page => {
  // Platform names no repo: it holds acme/widgets by its main checkout's folder. Orca puts the worktrees somewhere else.
  await page.evaluate(() => term.projects([{id: "p1", name: "Platform", repos: [], dirs: ["/w/widgets"]}, {id: "p2", name: "Elsewhere", repos: [], dirs: ["/orca"]}]));
  const T = [...TREES.trees, {ade: "orca", project: "acme/widgets", name: "far", path: "/orca/acme/far", branch: "far", main: false, agent: "none"}];
  await page.evaluate(t => term.workspaces({trees: t}), T);
  const under = () => page.evaluate(() => Object.fromEntries(sideGroups().flatMap(([, , m]) => [...m].flatMap(([p, ws]) => ws.map(w => [w.name, p])))));
  let u = await under();
  assert.equal(u["fix-login"], "Platform", "a worktree outside the project's folder"); assert.equal(u.far, "Platform", "the repo's project, not the folder's");
  await page.evaluate(() => term.tree({op: "new", path: "/orca/acme/brand-new", repo: "/w/widgets", ok: true}));
  assert.equal((await under())["brand-new"], "Platform", "just made, not listed yet: already under its repo's project");
  assert.deepEqual(page._errs, []);
});
test("new workspace: an ambiguous goal pauses before anything is made; chips answer, the same request goes again with them; Start anyway forces it", async page => {
  await page.click("#shead button[aria-label='New workspace']"); await page.waitForSelector("#dlg:not([hidden]) textarea:focus");
  await page.fill("#dlg textarea", "make it better"); await page.click("#dlg button.pri");
  const sent = async () => (await posts(page, "tree")).filter(m => m.op === "new").map(m => ({path: m.path, ...JSON.parse(m.text)}));
  const [first] = await sent();
  assert.deepEqual([first.path, first.name, first.goal, first.agent], ["/w/widgets", "make-it-better", "make it better", true]);
  const Q = {op: "new", path: "/w/widgets", paused: true, p_ambiguous: 0.9, questions: [
    {id: "done", q: "What does done look like?", options: ["A draft PR with tests", "A working prototype I can try"]},
    {id: "scope", q: "How far should it go?", options: ["The smallest change that works", "Investigate and report back first"]}]};
  await page.evaluate(q => term.tree(q), Q);
  assert.match(await page.textContent("#dlg h3"), /Two questions before Claude starts/);
  assert.match(await page.textContent("#dlg"), /“make it better”, which reads as ambiguous \(90% sure\)/);
  await page.click("#dlg button.pri");
  assert.match(await page.textContent("#dlg .err"), /Pick an answer for each/); assert.equal((await sent()).length, 1, "sent unanswered");
  await page.click("#dlg .opts button:has-text('A draft PR with tests')");
  assert.equal(await page.getAttribute("#dlg .opts button:has-text('A draft PR with tests')", "aria-pressed"), "true");
  await page.fill("#dlg input[aria-label^='How far']", "Only the login page");
  await page.click("#dlg button.pri");
  assert.equal(await page.isHidden("#dlg"), true);
  const again = (await sent())[1];
  assert.deepEqual({...again, answers: undefined}, {...first, answers: undefined}, "not the same request");
  assert.deepEqual(again.answers, {done: "A draft PR with tests", scope: "Only the login page"});
  await page.evaluate(q => term.tree(q), Q);
  await page.click("#dlg button:has-text('Start anyway')");
  assert.equal(await page.isHidden("#dlg"), true); assert.equal((await sent())[2].force, true);
  await page.evaluate(() => term.tree({op: "new", path: "/orca/acme/make-it-better", repo: "/w/widgets", ok: true}));
  await page.evaluate(q => term.tree(q), Q);  // made: a stray late pause asks nothing
  assert.equal(await page.isHidden("#dlg"), true);
  assert.deepEqual(page._errs, []);
});
test("new workspace in a project window: the repo offered first is one of the window's", async page => {
  page = await boot(page.context().browser(), null, "?only=" + encodeURIComponent('["p2"]'));
  await page.evaluate(() => { term.projects([{id: "p2", name: "Widgets", repos: ["acme/widgets"], dirs: []}]);
    term.workspaces({trees: [{ade: "orca", project: "aaa/first", name: "first", path: "/w/first", branch: "main", main: true, agent: "none"}, ...trees]}) });
  await page.click("#shead button[aria-label='New workspace']"); await page.waitForSelector("#dlg:not([hidden]) textarea:focus");
  assert.equal(await page.$eval("#dlg select", s => s.value), "/w/widgets");
  assert.deepEqual(page._errs, []); await page.close();
});
test("a window of some projects shows only theirs (Hosts stay); No Man's Land is the repos in none", async page => {
  const b = page.context().browser();
  page = await boot(b, null, "?only=" + encodeURIComponent('["p1"]')); await page.evaluate(p => term.projects(p), PROJ);
  let n = await names(page);
  assert.ok(n.includes("Local") && n.includes("fix-login") && n.includes("notes") && !n.includes("sidebar") && !n.includes("review"), n.join(","));
  assert.equal(await page.textContent("#shead .ttl"), "Platform");
  // Chief's BOARD scope: the window's worktrees and their repos, so a workspace `ade worktree create` makes before the next trees is in it
  assert.deepEqual(await page.evaluate(() => { const f = term.focusInfo(); return [f.paths.sort(), f.repos] }), [["/w/fix-login", "/w/widgets"], ["acme/widgets"]]);
  await page.close();
  page = await boot(b, null, "?only=" + encodeURIComponent('[""]')); await page.evaluate(p => term.projects(p), PROJ);
  n = await names(page);
  assert.ok(n.includes("sidebar") && !n.includes("review") && !n.includes("fix-login"), n.join(","));
  assert.deepEqual(page._errs, []); await page.close();
});

test("new window: the picker posts All, or the projects you tick", async page => {
  await page.evaluate(p => term.projects(p), PROJ);
  assert.equal(await page.$("#shead [aria-label='New window']"), null, "the sidebar head has no New window button: ⌘⇧N, or the picker");
  await page.keyboard.press("Meta+Shift+N"); await page.click("#dlg button.pri");
  assert.deepEqual((await posts(page, "window")).pop(), {cmd: "window", only: null});
  await page.click("#shead .ttl"); await page.click("#dlg .seg button:has-text('New window')"); await page.click("#dlg label:has-text('Greenlight') input"); await page.click("#dlg label:has-text(\"No Man's Land\") input");
  await page.click("#dlg button.pri");
  assert.deepEqual((await posts(page, "window")).pop().only, ["p2", ""]);
});

test("edit projects: a new project, a repo moved into it, folders checked", async page => {
  await page.evaluate(p => term.projects(p), PROJ);
  await page.click("#shead .ttl"); assert.equal(await page.textContent("#dlg .seg [aria-pressed=true]"), "This window", "the window's title picks its projects");
  await page.fill("#dlg [aria-label='Filter projects']", "plat"); assert.deepEqual(await page.$$eval("#dlg .tile:not([hidden]) b", b => b.map(x => x.textContent)), ["Platform"], "filtered");
  await page.click("#dlg button:has-text('Edit projects…')"); assert.match(await page.textContent("#dlg h3"), /project/i, "…and edits them");
  await page.keyboard.press("Escape");
  await page.click("#shead [aria-label='Edit projects']");
  await page.click("#dlg button:has-text('New project')");
  await page.fill("#dlg [aria-label='Project name']", "Ops");
  const dir = async d => { await page.fill("#dlg [aria-label='Add a folder']", d); await page.press("#dlg [aria-label='Add a folder']", "Enter") };
  await dir("relative/path"); assert.match(await page.textContent("#dlg .err"), /isn't a full path/);
  await dir("/srv/ops/"); await dir("/srv/ops");
  assert.deepEqual(await page.$$eval("#dlg .fl code", c => c.map(x => x.textContent)), ["/srv/ops"], "one folder, no trailing slash");
  await page.click("#dlg .pn button:has-text(\"No Man's Land\")");
  await page.selectOption("#dlg select[aria-label='Project for octo/greenlight']", {label: "Ops"});
  assert.ok(!(await page.$("#dlg select[aria-label='Project for octo/greenlight']")), "it moved out of No Man's Land");
  await page.click("#dlg .pn button:has-text('Ops')");
  await page.fill("#dlg [aria-label='Default folder']", "rel"); await page.click("#dlg button.pri");
  assert.match(await page.textContent("#dlg .err"), /isn't a full path/, "a default folder is checked like a folder");
  await page.fill("#dlg [aria-label='Default folder']", "/srv/ops/app/");
  await page.click("#dlg button.pri");
  const saved = (await posts(page, "projects")).pop().projects;
  assert.equal(saved.length, 3); assert.deepEqual({...saved[2], id: 0}, {id: 0, name: "Ops", repos: ["octo/greenlight"], dirs: ["/srv/ops"], dir: "/srv/ops/app"});
  assert.ok((await groups(page)).includes("Ops"), "the sidebar regroups at once");
});

test("a project window's new tabs open in its default folder", async page => {
  const b = page.context().browser();
  page = await boot(b, null, "?only=" + encodeURIComponent('["p1"]')); await page.evaluate(p => term.projects(p), [{...PROJ[0], dir: "/notes/app"}, PROJ[1]]);
  await page.click("[aria-label='New tab']");
  assert.equal((await posts(page, "open")).pop().ws, "/notes/app");
  assert.deepEqual(page._errs, []); await page.close();
});

test("the title changes this window's projects in place", async page => {
  await page.evaluate(p => term.projects(p), PROJ);
  await page.click("#shead .ttl"); await page.click("#dlg label:has-text('Platform') input"); await page.click("#dlg button.pri");
  assert.deepEqual((await posts(page, "only")).pop(), {cmd: "only", only: ["p1"]});
  assert.equal(await page.textContent("#shead .ttl"), "Platform");
  assert.ok(!(await names(page)).includes("sidebar"), "refiltered");
  assert.equal(await strip(page), 1, "its shells stay");
});

test("clearing the last project selects All projects", async page => {
  await page.evaluate(p => term.projects(p), PROJ);
  await page.click("#shead .ttl");
  assert.equal(await page.isChecked("#dlg .tile input >> nth=0"), true, "All starts checked");
  await page.click("#dlg .tile input >> nth=1");
  await page.click("#dlg .tile input >> nth=1");
  assert.equal(await page.isChecked("#dlg .tile input >> nth=0"), true, "no empty selection");
  await page.click("#dlg button.pri");
  assert.deepEqual((await posts(page, "only")).pop(), {cmd: "only", only: null});
  await page.close();
});

test("a vanished selected project shows All without losing the saved pick", async page => {
  const b = page.context().browser();
  page = await boot(b, null, "?only=" + encodeURIComponent('["gone"]'));
  await page.evaluate(p => term.projects(p), PROJ);
  assert.equal(await page.evaluate(() => term.focusInfo().repos), null, "Chief has all repos in scope");
  assert.ok((await names(page)).includes("sidebar"), "terminal still lists workspaces");
  await page.click("#shead .ttl");
  assert.equal(await page.isChecked("#dlg .tile input >> nth=0"), true, "picker shows All");
  await page.click("#dlg button.pri");
  assert.deepEqual((await posts(page, "only")).pop(), {cmd: "only", only: null});
  await page.close();
});

test("L1's project list: each project's paths are the trees this window shows under it, a no-repo or folder-only one too", async page => {
  await page.evaluate(p => term.projects(p), PROJ);
  await page.evaluate(t => term.workspaces({trees: [...t.trees, {ade: "orca", project: "", name: "scratch", path: "/x/scratch", branch: "", main: false, agent: "none"}]}), TREES);
  const list = (await posts(page, "title")).pop().list, paths = id => list.find(p => p.id === id).paths.sort();
  assert.deepEqual(paths("p1"), ["/w/fix-login", "/w/widgets"], "by its repo");
  assert.deepEqual(paths("p2"), ["/w/review"], "by its folder: no repo of its own");
  assert.deepEqual(paths(""), ["/w/shipped", "/w/sidebar", "/x/scratch"], "No Man's Land: a no-repo workspace is in it");
  const at = id => list.find(p => p.id === id);
  assert.deepEqual([at("p1").repos, at("p2").repos, at("").repos], [["acme/widgets"], [], []], "a repo with a tree filed under another project's folder isn't No Man's Land's");
  assert.deepEqual([at("p1").busy, at("p2").busy, at("").busy], [1, 0, 0], "working agents counted where they're shown");
});
// issue 196: a new window shows only once it has drawn, already in its colours and on its floor; a project window's first shell doesn't wait
async function cold(b, query = ""){
  const fresh = await b.newPage({viewport: {width: 1200, height: 800}, colorScheme: "light"});
  await fresh.addInitScript(() => { window.__posts = []; window.webkit = {messageHandlers: {term: {postMessage: m => window.__posts.push(JSON.parse(JSON.stringify(m)))}}};
    document.addEventListener("readystatechange", () => { if (document.readyState === "interactive" && !window.__early) window.__early = getComputedStyle(document.getElementById("stage")).backgroundColor }) });
  const errs = []; fresh.on("pageerror", e => errs.push(e.message)); fresh._errs = errs;
  await fresh.goto(PAGE + query); await fresh.waitForFunction(() => window.__posts.some(m => m.cmd === "ready")); return fresh;
}
test("a cold window: painted comes once, after the layout is restored (not at ready); the terminal canvas has its colour from the first frame", async page => {
  const look = encodeURIComponent(JSON.stringify({background: "#102030", mode: "dark"}));
  const fresh = await cold(page.context().browser(), "?look=" + look);
  assert.equal((await posts(fresh, "painted")).length, 0, "ready isn't painted: nothing restored yet");
  assert.equal(await fresh.evaluate(() => window.__early), "rgb(16, 32, 48)", "the stage wears the look's background before the body script runs");
  assert.equal(await fresh.evaluate(() => document.documentElement.dataset.appearance), "dark", "and the window's Light/Dark");
  await fresh.evaluate(() => term.restore(null)); await fresh.waitForFunction(() => window.__posts.some(m => m.cmd === "painted"));
  await fresh.evaluate(() => term.restore({})); await fresh.waitForTimeout(150);
  assert.equal((await posts(fresh, "painted")).length, 1, "once");
  assert.deepEqual(fresh._errs, []); await fresh.close();
});
test("a Basement window opens with the sidebar out (?side=1): no slide, and the app is told; term.opens before it shows is a cut", async page => {
  const b = page.context().browser(), fresh = await cold(b, "?side=1");
  assert.equal(await fresh.evaluate(() => document.body.classList.contains("noside")), false);
  assert.deepEqual((await posts(fresh, "side")).pop(), {cmd: "side", open: true, boot: true}, "boot: not an action you took");
  const pipe = await cold(b);
  await pipe.evaluate(() => term.opens("basement"));
  assert.deepEqual(await pipe.evaluate(() => [document.body.classList.contains("noside"), document.body.classList.contains("sliding")]), [false, false], "out at once, not sliding");
  await pipe.evaluate(() => term.restore(null)); await pipe.waitForFunction(() => window.__posts.some(m => m.cmd === "painted"));
  await pipe.evaluate(() => term.opens("pipe"));
  assert.equal(await pipe.evaluate(() => document.body.classList.contains("sliding")), true, "once shown, it slides");
  assert.deepEqual([fresh._errs, pipe._errs], [[], []]); await fresh.close(); await pipe.close();
});
test("a one-project window with a default folder opens its first shell at once, without waiting on the trees", async page => {
  const fresh = await cold(page.context().browser(), "?only=" + encodeURIComponent('["p1"]'));
  await fresh.evaluate(p => term.projects(p), [{...PROJ[0], dir: "/notes/app"}, PROJ[1]]);
  await fresh.evaluate(() => term.restore(null));
  assert.equal((await posts(fresh, "open")).pop()?.ws, "/notes/app", "no 5 s wait");
  const bare = await cold(page.context().browser(), "?only=" + encodeURIComponent('["p1"]'));
  await bare.evaluate(p => term.projects(p), PROJ); await bare.evaluate(() => term.restore(null));
  assert.equal((await posts(bare, "open")).length, 0, "no folder, no host yet: it still waits for the trees");
  await bare.evaluate(t => term.workspaces(t), TREES);
  assert.equal((await posts(bare, "open")).length, 1, "the trees come: it opens");
  assert.deepEqual([fresh._errs, bare._errs], [[], []]); await fresh.close(); await bare.close();
});
test("the app's order at ready (trees, a Basement button's worktree, then the layout): one tab, the worktree's, no stray first shell", async page => {
  const fresh = await cold(page.context().browser(), "?only=" + encodeURIComponent('["p1"]'));
  await fresh.evaluate(p => term.projects(p), PROJ); await fresh.evaluate(t => term.workspaces(t), TREES);
  await fresh.evaluate(() => term.ws("/w/fix-login", {})); await fresh.evaluate(() => term.restore(null));
  await fresh.waitForFunction(() => window.__posts.some(m => m.cmd === "painted"));
  assert.deepEqual((await posts(fresh, "open")).map(m => m.ws), ["/w/fix-login"]);
  assert.ok((await lastSave(fresh))?.tabs?.some(t => t.ws === "/w/fix-login"), "the worktree's tab is saved");
  await fresh.evaluate(() => term.opens("pipe", true));
  assert.equal(await fresh.evaluate(() => document.body.classList.contains("sliding")), false, "out of sight (the app says so): a cut, even after painted");
  const odd = await cold(page.context().browser());  // Chief's [console: …] with a name that matches nothing: the usual first shell
  await odd.evaluate(t => term.workspaces(t), TREES); await odd.evaluate(() => term.ws("no-such-tab", {})); await odd.evaluate(() => term.restore(null));
  assert.equal((await posts(odd, "open")).length, 1, "a first shell, not an empty window");
  assert.deepEqual([fresh._errs, odd._errs], [[], []]); await fresh.close(); await odd.close();
});
test("title omits list before term.workspaces arrives so boot never overwrites L1's project list with empty repos/paths", async page => {
  const b = page.context().browser(), fresh = await b.newPage({viewport: {width: 1200, height: 800}, colorScheme: "dark"});
  await fresh.addInitScript(() => { window.__posts = []; window.webkit = {messageHandlers: {term: {postMessage: m => window.__posts.push(JSON.parse(JSON.stringify(m)))}}} });
  await fresh.goto(PAGE);
  await fresh.waitForFunction(() => window.__posts.some(m => m.cmd === "ready"));
  await fresh.evaluate(p => term.projects(p), PROJ);
  assert.equal((await posts(fresh, "title")).some(m => "list" in m), false, "no partial list before treesIn");
  await fresh.evaluate(t => term.workspaces(t), TREES);
  const last = (await posts(fresh, "title")).pop();
  assert.ok(Array.isArray(last.list) && last.list.some(p => p.id === "p1" && p.repos.includes("acme/widgets")), "full list once treesIn");
  await fresh.close();
});

test("a repo's .greenlight.json: its project, and a window of it wears its look, hosts and host; Save to repo writes it", async page => {
  const cfg = {name: "Acme", look: {strip: "#112233", bg: "#000"}, host: "mini", hosts: ["mini"]};
  const trees = {trees: TREES.trees.map(w => w.name === "widgets" ? {...w, cfg} : w)};
  await page.evaluate(t => term.workspaces(t), trees);
  assert.ok((await groups(page)).includes("Acme"), "the file makes the project");
  const b = page.context().browser();
  page = await boot(b, null, "?host=devbox&hosts=" + encodeURIComponent('["mini","devbox","gpu"]') + "&only=" + encodeURIComponent('["pacme"]'), trees);
  assert.equal(await page.textContent("#shead .ttl"), "Acme");
  const strip = () => page.evaluate(() => document.documentElement.style.getPropertyValue("--strip"));
  assert.notEqual(await strip(), "#112233", "the app's colors until you say yes");
  assert.match(await page.textContent("#adopt"), /Wear the colors widgets checked in/);
  await page.click("#adopt button:has-text('No')");
  assert.ok(await page.$eval("#adopt", a => a.hidden)); assert.notEqual(await strip(), "#112233"); assert.equal((await lastSave(page)).lights, "default");
  await page.click("#shead [aria-label='Customize look']"); await page.click("#dlg button:has-text('Local Lights')");
  assert.equal(await strip(), "#112233", "the picker wears it after all"); await page.keyboard.press("Escape");
  await page.evaluate(() => { lights = null; offer() }); await page.click("#adopt button:has-text('Yes')");
  assert.equal(await strip(), "#112233"); assert.equal((await lastSave(page)).lights, "local");
  assert.equal(await page.evaluate(() => document.documentElement.style.getPropertyValue("--bg")) !== "#000", true, "never the terminal background");
  assert.equal((await posts(page, "open")).pop().ws, "@mini", "the first shell opens on its host");
  const hosts = await page.evaluate(() => ({cards: [...document.querySelectorAll("#tree .s-host .hc > .ws .wn")].map(x => x.textContent),
    chips: [...document.querySelectorAll("#tree .s-host .hchip")].map(x => x.textContent)}));
  assert.deepEqual(hosts, {cards: ["mini"], chips: ["Local"]}, "its hosts only (and Local, first of the idle ones)");
  await page.click("#shead [aria-label='Customize look']"); await page.fill("#dlg #look-ask", "x"); await page.click("#dlg button:has-text('Restyle')");
  await page.evaluate(() => term.looked('{"dark": {"accent": "#ff0000"}, "light": {"strip": "#f5f3ee", "fg": "url(x)"}}')); await page.keyboard.press("Escape");
  await page.click("#shead [aria-label='Customize look']"); await page.click("#dlg button:has-text('Save to widgets')");
  const w = (await posts(page, "tree")).pop();
  assert.equal(w.op, "cfg"); assert.equal(w.path, "/w/widgets");
  assert.deepEqual(JSON.parse(w.text), {...cfg, look: {dark: {strip: "#112233", accent: "#ff0000"}, light: {strip: "#f5f3ee"}}}, "a pair; the old flat look is its dark half");
  const saved = JSON.parse(w.text); await page.evaluate(o => term.tree(o), {op: "cfg", path: "/w/widgets", ok: true, cfg: saved});
  assert.deepEqual((await lastSave(page)).skin, {}, "the file has it now");
  assert.equal(await page.evaluate(() => document.documentElement.style.getPropertyValue("--accent")), "#ff0000");
  assert.deepEqual(page._errs, []); await page.close();
});

test("look: a prompt restyles the chrome (not the terminals), undo walks back, and it's saved", async page => {
  const strip = () => page.evaluate(() => document.documentElement.style.getPropertyValue("--strip"));
  const before = await strip(), theme = await page.evaluate(() => JSON.stringify([...panes.values()][0].t.options.theme));
  await page.click("#shead [aria-label='Customize look']"); await page.fill("#dlg #look-ask", "purple and moody"); await page.click("#dlg button:has-text('Restyle')");
  assert.match((await posts(page, "look")).pop().text, /purple and moody/);
  assert.equal(await page.textContent("#dlg button[type=submit]"), "Restyling…", "the dialog stays while Claude thinks, and says so");
  await page.evaluate(() => term.looked('Sure: ```json\n{"strip": "#2a1840", "accent": "#b48cff", "bg": "#000", "fg": "url(x)"}\n```'));
  assert.equal(await strip(), "#2a1840");
  assert.equal(await page.inputValue("#dlg #look-ask"), "purple and moody", "redrawn with the answer, the prompt kept"); assert.equal(await page.isEnabled("#dlg button:has-text('Restyle')"), true);
  assert.equal(await page.evaluate(() => document.documentElement.style.getPropertyValue("--bg")) !== "#000", true, "never the terminal background");
  assert.equal(await page.evaluate(() => JSON.stringify([...panes.values()][0].t.options.theme)), theme, "the terminals keep their colors");
  assert.deepEqual((await lastSave(page)).skin, {strip: "#2a1840", accent: "#b48cff"});
  await page.click("#toast button:has-text('Undo')");
  assert.equal(await strip(), before);
  assert.deepEqual((await lastSave(page)).skin, {});
  await page.evaluate(() => term.looked("no idea"));
  assert.match(await page.textContent("#toast"), /Couldn't restyle/);
  assert.deepEqual(page._errs, []);
});

test("look: premade looks, worn at once; every color by hand, live, and Undo walks back", async page => {
  const v = k => page.evaluate(k => document.documentElement.style.getPropertyValue("--" + k), k);
  const before = await v("strip");
  await page.click("#shead [aria-label='Customize look']"); await page.fill("#dlg #look-ask", "keep me");
  assert.equal(await page.$$eval("#dlg .dz button", b => b.length), 8, "under the prompt");
  await page.click("#dlg .dz button:has-text('Ember')"); await page.click("#dlg .dz button:has-text('Forest')");
  assert.equal(await v("strip"), "#151d18", "worn at once"); assert.equal((await lastSave(page)).skin.strip, "#151d18"); assert.equal((await lastSave(page)).lights, "own");
  assert.equal(await page.inputValue("#dlg #look-ask"), "keep me", "the dialog stays, the prompt with it");
  assert.equal(await page.getAttribute("#dlg .dz button:has-text('Forest')", "aria-pressed"), "true");
  await page.click("#dlg button:has-text('Undo')"); assert.equal(await v("strip"), "#1f1714", "Undo walks back to Ember"); await page.keyboard.press("Escape");
  await page.click("#shead [aria-label='Customize look']"); await page.click("#dlg .dz button:has-text('Forest')");
  assert.equal(await page.$$eval("#dlg input[type=color]", i => i.length), 13);
  assert.equal(await page.inputValue("#dlg input[aria-label='sidebar and tab strip background']"), "#151d18");
  assert.match(await page.inputValue("#dlg input[aria-label='crashed or failed']"), /^#[0-9a-f]{6}$/, "a color-mix or default reads as hex too");
  await page.fill("#dlg input[aria-label='crashed or failed']", "#ff0000"); assert.equal(await v("r"), "#ff0000", "live, and worn");
  await page.click("#dlg button:has-text('Undo')"); assert.notEqual(await v("r"), "#ff0000", "Undo walks it back");
  await page.fill("#dlg input[aria-label='chrome text']", "#fafafa"); await page.click("#dlg button:has-text('Done')");
  assert.deepEqual((await lastSave(page)).skin, {...(await page.evaluate(() => PRESETS[2].colors)), fg: "#fafafa"});
  assert.notEqual(before, "#151d18"); assert.deepEqual(page._errs, []);
});

test("/design: a few options as cards; choosing one previews the whole window, Cancel puts it back, Use this keeps it", async page => {
  const strip = () => page.evaluate(() => document.documentElement.style.getPropertyValue("--strip"));
  const before = await strip();
  await page.click("#shead [aria-label='Customize look']"); await page.fill("#dlg #look-ask", "/design calm"); await page.click("#dlg button:has-text('Restyle')");
  const ask = (await posts(page, "look")).pop().text;
  assert.match(ask, /request: calm/); assert.match(ask, /JSON array/);
  const opts = [{name: "Fog", colors: {strip: "#1b1f24", fg: "#e6e6e6", accent: "#7aa2f7"}}, {name: "Ember <b>", colors: {strip: "#2a1a14", accent: "#ff8a4c", fg: "url(x)"}}, {name: "junk", colors: {nope: 1}}];
  await page.evaluate(o => term.looked("Here:\n" + JSON.stringify(o)), opts);
  assert.deepEqual(await page.$$eval("#dlg .dz button b", b => b.map(x => x.textContent)), ["Fog", "Ember <b>"], "an option with no usable colors is dropped; names are text");
  assert.equal(await strip(), "#1b1f24", "the first option previews at once");
  assert.ok(await page.$eval("#dlg .dz button", c => c.querySelector(".dots").getBoundingClientRect().bottom <= c.getBoundingClientRect().bottom), "the color dots sit inside the card");
  await page.click("#dlg .dz button:nth-child(2)"); assert.equal(await strip(), "#2a1a14");
  await page.click("#dlg button:has-text('Cancel')"); assert.equal(await strip(), before, "Cancel: back as it was");
  assert.deepEqual((await page.evaluate(() => skin)), {});
  await page.click("#shead [aria-label='Customize look']"); await page.fill("#dlg #look-ask", "/design"); await page.click("#dlg button:has-text('Restyle')");
  await page.evaluate(o => term.looked(JSON.stringify(o)), opts);
  await page.click("#dlg .dz button:nth-child(2)"); await page.click("#dlg button.pri");
  assert.deepEqual((await lastSave(page)).skin, {strip: "#2a1a14", accent: "#ff8a4c"});
  await page.click("#toast button:has-text('Undo')"); assert.equal(await strip(), before);
});

test("closeAll (an extra window's red button): every session back to the app, no close or hide posts", async page => {
  await page.keyboard.press("Meta+t"); await page.waitForFunction(() => window.__posts.filter(m => m.cmd === "open").length === 2);
  const sessions = (await posts(page, "open")).map(o => o.session);
  const before = (await page.evaluate(() => window.__posts.length));
  const out = await page.evaluate(() => term.closeAll());
  assert.deepEqual(out.map(o => o.session).sort(), sessions.sort());
  assert.equal(await page.evaluate(() => tabs.length + panes.size), 0, "no tabs or panes left (the window closes next, so the strip DOM is left as is)");
  await page.waitForTimeout(700);
  assert.deepEqual((await page.evaluate(n => window.__posts.slice(n).map(m => m.cmd), before)).filter(c => ["close", "hide", "save"].includes(c)), [], "Swift kills them; the page must not re-enter");
});

test("rows: the most urgent badge, MR/CI/threads/rebase chips (open MRs only)", async page => {
  const row = n => page.$eval(`#tree .ws:has-text('${n}')`, r => ({badge: r.querySelector(".badge")?.textContent || "",
    chips: [...r.querySelectorAll(".chip")].map(c => c.className.split(" ")[1] + ":" + c.textContent + ":" + c.title.split(":")[0])}));
  assert.deepEqual(await row("sidebar"), {badge: "Question", chips: ["k-g:!51:opened", "k-r::CI failed", "k-o:2:2 unresolved threads", "k-o::Needs a rebase"]});
  await page.click("#tree .grp.sec:has-text('Done')");
  assert.deepEqual(await row("shipped"), {badge: "", chips: ["k-p:!40:merged"]}, "a merged MR shows only its ref");
  assert.equal((await row("fix-login")).badge, "Working");
});

test("a workspace opens a shell in its worktree; the strip shows only that workspace's tabs", async page => {
  await page.click("#tree .ws:has-text('fix-login')");
  const o = (await posts(page, "open")).pop();
  assert.equal(o.ws, "/w/fix-login");
  assert.equal(await strip(page), 1, "Local's tab isn't in fix-login's strip");
  await page.keyboard.press("Meta+t");
  assert.equal(await strip(page), 2);
  await page.click("#tree .ws:has-text('Local')");
  assert.equal(await strip(page), 1);
});

test("the layout saves (splits by session) and a fresh page restores the same sessions", async page => {
  await page.click("#tree .ws:has-text('sidebar')");
  await page.keyboard.press("Meta+d");
  await page.waitForFunction(() => window.__posts.some(m => m.cmd === "save"), {timeout: 3000});
  const st = await lastSave(page);
  assert.equal(st.tabs.length, 2);
  const t = st.tabs[1];
  assert.equal(t.ws, "/w/sidebar"); assert.ok(t.tree.kids && t.tree.kids[0].s && t.tree.kids[1].s, JSON.stringify(t.tree));
  const again = await boot(page.context().browser(), st);
  const opened = (await posts(again, "open")).map(o => o.session).sort();
  assert.deepEqual(opened, [st.tabs[0].tree.s, ...t.tree.kids.map(k => k.s)].sort());
  assert.equal(await again.$$eval("#stage .split", s => s.length), 1);
  const st2 = await lastSave(again);
  assert.equal(st2, undefined, "restoring an unchanged layout doesn't rewrite it");
  await again.close();
});

test("ssh couldn't connect (255): the pane and layout stay, a key reconnects to the same session; a real exit closes it", async page => {
  const [o] = await posts(page, "open"), before = await lastSave(page);
  await page.evaluate(id => term.exited(id, 255), o.id);
  assert.equal(await page.$$eval("#stage .pane", p => p.length), 1);
  await page.click("#stage .t"); await page.keyboard.press("a");
  const again = (await posts(page, "open")).pop();
  assert.equal(again.session, o.session); assert.equal((await posts(page, "in")).length, 0, "the key only reconnects");
  assert.deepEqual(await lastSave(page), before, "the layout is unchanged");
  await page.evaluate(id => term.exited(id, 0), o.id);
  assert.equal((await posts(page, "close")).pop().session, o.session);
});

test("⌘J: fuzzy jump to a workspace", async page => {
  await page.keyboard.press("Meta+j");
  assert.equal(await page.$eval("#pal", p => p.hidden), false);
  await page.keyboard.type("fxlg");
  assert.equal(await page.$eval("#pres .sel .m", m => m.textContent), "fix-login");
  await page.keyboard.press("Enter");
  assert.equal(await page.$eval("#pal", p => p.hidden), true);
  assert.equal((await posts(page, "open")).pop().ws, "/w/fix-login");
});

test("⌘J: MR number, title, description and people match; > lists actions; Esc clears, then closes", async page => {
  await page.evaluate(t => { t.trees[2].mr = {...t.trees[2].mr, title: "Sidebar polish", description: "Fixes the flaky devbox reconnect", by: {name: "odin"}, web_url: "https://x/51"}; term.workspaces(t) }, TREES);
  await page.keyboard.press("Meta+j");
  assert.equal(await page.$eval("#pv", p => p.querySelector(".k").textContent + "|" + !!p.querySelector("dt")), "Workspace|true", "preview: kind and facts");
  assert.equal(await page.$eval("#pres .gh", g => g.firstChild.textContent + "|" + g.lastChild.textContent), "Needs you|1", "empty query: what needs you first");
  const top = async q => { await page.fill("#pq", q); return page.$eval("#pres .hit", c => [c.querySelector(".m").textContent, c.querySelector(".sub").textContent]) };
  for (const q of ["51", "!51", "sidebar polish", "devbox reconnect", "odin"]) assert.equal((await top(q))[0], "sidebar", q);
  assert.match((await top("devbox"))[1], /^Description: .*devbox/, "says which field matched");
  assert.equal(await page.$$eval("#pres .hit button", b => b.map(x => x.textContent.split(" ")[0]).join()), "Jump,New,Open", "the top hit's moves");
  await page.keyboard.press("Escape"); assert.equal(await page.$eval("#pq", i => i.value), ""); assert.equal(await page.$eval("#pal", p => p.hidden), false, "Esc clears first");
  await page.keyboard.type(">"); assert.equal(await page.$eval("#pmode", m => m.hidden), false, "> is actions mode");
  await page.keyboard.type("split dow"); assert.equal(await page.$eval("#pres .sel .m", m => m.textContent), "Split down");
  await page.keyboard.press("Enter"); assert.equal(await page.evaluate(() => paneOf(cur).length), 2, "the action ran");
  await page.keyboard.press("Meta+j");
  assert.deepEqual([...new Set(await page.$$eval("#pres .r-ws .ic svg", d => d.map(x => parseFloat(getComputedStyle(x).height) + (x.getAttribute("aria-label") ? "" : " unlabeled"))))], [15], "every workspace row has a 15px, labelled state glyph (no stretched sidebar dots)");
  assert.equal(await page.$eval("#pv", p => getComputedStyle(p).display !== "none" && p.getBoundingClientRect().width), 290, "the preview pane stays beside the results");
  await page.keyboard.press("Tab");
  assert.deepEqual(await page.$$eval("#pres .r", r => [...new Set(r.map(x => x.classList[1]))]), ["r-tab"], "⇥: Terminals only");
  assert.equal(await page.$eval("#pres .r", r => getComputedStyle(r).flexDirection + getComputedStyle(r).height), "row32px", "rows (32, DESIGN.md §10.15) keep their own layout, not the sidebar's .tab/.ws");
  await page.keyboard.press("Escape"); assert.equal(await page.$eval("#pal", p => p.hidden), true, "Esc on empty closes");
});

test("auto names: quiet output asks once; a name you type stops it", async page => {
  const id = (await posts(page, "open"))[0].id;
  await page.evaluate(id => term.feed(id, btoa("\x1b]0;/Users/me/proj\x07$ ls\r\n")), id);
  await page.waitForTimeout(4300);
  const [n] = await posts(page, "name");
  assert.ok(n && n.auto, "an auto run went out");
  await page.evaluate(k => term.named(k, "proj files"), n.tab);
  assert.equal(await page.$eval("#tabs .tab.on .nm", e => e.textContent), "proj files");
  await page.dblclick("#tabs .tab.on .nm");
  await page.waitForSelector("#tabs input:focus");
  await page.keyboard.type("mine"); await page.keyboard.press("Enter");
  await page.evaluate(id => term.feed(id, btoa("\x1b]0;something else\x07more\r\n")), id);
  await page.waitForTimeout(4300);
  assert.equal((await posts(page, "name")).length, 1, "typed name: no more auto runs");
  assert.equal((await lastSave(page)).tabs[0].manual, true);
});

test("tab icon: Claude's ✳ title, tmux's ⌬ for codex (not shown in the name), else the shell", async page => {
  const id = (await posts(page, "open"))[0].id, icon = () => page.$eval("#tabs .tab.on svg", e => e.outerHTML);
  assert.match(await icon(), /data-icon="terminal"/);
  await page.evaluate(id => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode("\x1b]0;⌬ mr-5162\x07")))), id);
  await page.waitForFunction(() => document.querySelector("#tabs .tab.on .nm").textContent === "mr-5162");
  assert.match(await icon(), /data-icon="openai"/);
  await page.evaluate(id => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode("\x1b]0;✳ fix it\x07")))), id);
  await page.waitForFunction(() => document.querySelector("#tabs .tab.on .nm").textContent === "fix it");
  assert.match(await icon(), /data-icon="claude"[^>]*|ic-claude/);
  // the sidebar row renames too, and a typed name sticks: titles and auto-naming never overwrite it
  await page.dblclick("#tree .tm.on .wn"); await page.waitForSelector("#tree .tm input:focus");
  await page.keyboard.type("Codex review"); await page.keyboard.press("Enter");
  assert.equal(await page.$eval("#tree .tm.on .wn", e => e.textContent), "Codex review");
  await page.evaluate(id => term.feed(id, btoa("\x1b]0;Codex Agent\x07")), id);
  await page.waitForTimeout(4300);
  assert.equal(await page.$eval("#tabs .tab.on .nm", e => e.textContent), "Codex review");
  assert.equal(await page.$eval("#tree .tm.on .wn", e => e.textContent), "Codex review");
  assert.ok(!(await posts(page, "name")).some(n => n.auto), "a typed name: no auto runs");
  assert.equal((await lastSave(page)).tabs[0].manual, true);
});

test("alerts: Claude's spinner line going away in a tab you're not on pings once and badges the Dock", async page => {
  const id = (await posts(page, "open"))[0].id, feed = s => page.evaluate(([id, s]) => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode(s)))), [id, s]);
  await page.evaluate(() => newTab()); await page.waitForTimeout(200);
  await feed("\x1b]0;✳ Fix it\x07\r\n❯ Write a poem… (2 lines)\r\n"); await page.waitForTimeout(700);
  await feed("✶\x1b[CSkedaddling…\r\n"); await page.waitForTimeout(700);  // as tmux draws it: the space skipped, no timer in its first second
  assert.equal((await posts(page, "notify")).length, 0, "the prompt you typed isn't the spinner; working isn't done");
  await page.evaluate(() => { TURN_QUIET = 300 });
  await feed("\x1b[1A\x1b[2K✻ Churned for 3s · done\r\n"); await page.waitForTimeout(1100);
  const [ask] = await posts(page, "turn");
  assert.ok(ask && /^mc-/.test(ask.session), "a quiet spinner: greenlight is asked how the turn ended");
  assert.ok(await page.$eval("#toast", t => t.hidden), "no ping until it answers");
  await page.evaluate(s => term.turn({session: s, state: "finished", prob: 0.9}), ask.session); await page.waitForTimeout(100);
  assert.equal((await posts(page, "notify")).length, 0, "the window has focus: a toast, not a notification");
  assert.equal(await page.$eval("#toast", t => t.hidden ? "" : t.querySelector("b").textContent + "|" + t.querySelector("button").textContent), "Local finished|Show");
  assert.equal((await posts(page, "badge")).pop().n, 1);
});

test("alerts: a spinner that blinks out between tool calls isn't a turn's end", async page => {
  const id = (await posts(page, "open"))[0].id, feed = s => page.evaluate(([id, s]) => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode(s)))), [id, s]);
  await page.evaluate(() => { newTab(); TURN_QUIET = 1500 }); await page.waitForTimeout(200);
  await feed("\x1b]0;✳ Fix it\x07\r\n✶ Skedaddling… (3s)\r\n"); await page.waitForTimeout(700);
  await feed("\x1b[1A\x1b[2K⏺ Bash(make test)\r\n"); await page.waitForTimeout(700);  // idle for a moment
  await feed("✶ Skedaddling… (9s)\r\n"); await page.waitForTimeout(1500);  // and back at work before TURN_QUIET
  assert.equal((await posts(page, "turn")).length, 0, "nothing asked");
  assert.ok(await page.$eval("#toast", t => t.hidden), "and nothing pinged");
});

test("alerts: greenlight's reading names how the turn ended; no answer falls back to Finished; back at work drops it", async page => {
  const id = (await posts(page, "open"))[0].id, feed = s => page.evaluate(([id, s]) => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode(s)))), [id, s]);
  await page.evaluate(() => { newTab(); TURN_QUIET = 200 }); await page.waitForTimeout(200);
  const end = async () => { await feed("✶ Skedaddling… (3s)\r\n"); await page.waitForTimeout(700); await feed("\x1b[1A\x1b[2K❯ \r\n"); await page.waitForTimeout(1000);
    return (await posts(page, "turn")).pop() };
  const badge = () => page.evaluate(() => { const p = [...panes.values()][0]; return p.bell ? p.why : "" });
  const reset = () => page.evaluate(() => { const p = [...panes.values()][0]; p.bell = false; pinged.clear(); render() });
  await feed("\x1b]0;✳ Fix it\x07\r\n");
  for (const [state, why] of [["waiting_for_user", "Asking"], ["rate_limited", "Rate limited"], ["stuck_looping", "Stuck"], ["logged_out", "Logged out"], ["finished", "Finished"]]) {
    const ask = await end(); await page.evaluate(([s, st]) => term.turn({session: s, state: st, prob: 0.8}), [ask.session, state]);
    assert.equal(await badge(), why, `${state} → ${why}`); await reset();
  }
  let ask = await end(); await page.evaluate(s => term.turn({session: s, state: "working", prob: 0.8}), ask.session);
  assert.equal(await badge(), "", "still working on screen: no ping");
  ask = await end(); await feed("✶ Skedaddling… (4s)\r\n"); await page.waitForTimeout(700);
  await page.evaluate(s => term.turn({session: s, state: "finished", prob: 0.8}), ask.session);
  assert.equal(await badge(), "", "an answer for a pane that went back to work is dropped");
  await feed("\x1b[1A\x1b[2K❯ \r\n"); await page.waitForTimeout(1000);
  await page.evaluate(s => term.turn({session: s, error: "exit 255"}), (await posts(page, "turn")).pop().session);
  assert.equal(await badge(), "Finished", "an error answer: the plain reading");
});

test("alerts: restored background tabs fire finished and waiting alerts from backend pane states without duplicates", async page => {
  const b = page.context().browser();
  const saved = {
    cur: 1,
    tabs: [
      {ws: "/w/fix-login", set: 1, name: "", manual: false, sig: "", tree: {s: "mc-bg-work", st: "work", title: "✳ Fix login"}},
      {ws: "/w/sidebar", set: 2, name: "", manual: false, sig: "", tree: {s: "mc-fg"}},
      {ws: "/w/review", set: 3, name: "", manual: false, sig: "", tree: {s: "mc-bg-other"}},
    ],
  };
  page = await boot(b, saved, "", {trees: TREES.trees, panes: {
    "mc-bg-work": {state: "done", title: "✳ Fix login"},
    "mc-bg-other": {state: "working", title: "✳ Review PR"},
  }});
  // mc-bg-work was saved while working and backend reports it done at launch -> asks dash.py turn immediately
  const asks1 = await posts(page, "turn");
  assert.deepEqual(asks1.map(a => a.session), ["mc-bg-work"], "saved working pane that finished triggers turn check at restore");
  await page.evaluate(() => term.turn({session: "mc-bg-work", state: "finished", prob: 0.9}));
  assert.equal(await page.$eval("#toast", t => t.hidden ? "" : t.querySelector("b").textContent), "fix-login finished");
  assert.equal((await posts(page, "badge")).pop().n, 1);
  // repeated done polls do not re-trigger turn or ping
  await page.evaluate(t => term.workspaces({trees: t, panes: {
    "mc-bg-work": {state: "done", title: "✳ Fix login"},
    "mc-bg-other": {state: "done", title: "✳ Review PR"},
  }}), TREES.trees);
  const asks2 = await posts(page, "turn");
  assert.deepEqual(asks2.map(a => a.session), ["mc-bg-work", "mc-bg-other"], "working -> done on background tab triggers turn once; done -> done does not");
  await page.evaluate(() => term.turn({session: "mc-bg-other", state: "finished", prob: 0.9}));
  assert.equal((await posts(page, "badge")).pop().n, 2);
  // waiting state pings once and does not repeat on subsequent polls
  await page.evaluate(t => term.workspaces({trees: t, panes: {"mc-bg-other": {state: "waiting", title: "✳ Review PR"}}}), TREES.trees);
  await page.evaluate(() => { const p = [...panes.values()].find(x => x.session === "mc-bg-other"); p.bell = false; pinged.clear(); render() });
  await page.evaluate(t => term.workspaces({trees: t, panes: {"mc-bg-other": {state: "waiting", title: "✳ Review PR"}}}), TREES.trees);
  assert.equal(await page.evaluate(() => [...panes.values()].find(x => x.session === "mc-bg-other").bell), false, "still waiting on next poll: no repeat ping");
  await page.close();
});

test("alerts: with greenlight on a server, a restored @local background tab is driven by the Mac's pane states; another host's isn't", async page => {
  const b = page.context().browser();
  const saved = {cur: 0, tabs: [
    {ws: "/w/sidebar", set: 1, name: "", manual: false, sig: "", tree: {s: "mc-fg3"}},
    {ws: "@local", set: 2, name: "", manual: false, sig: "", tree: {s: "mc-mac-bg", st: "work", title: "✳ Mac job"}},
    {ws: "@mini", set: 3, name: "", manual: false, sig: "", tree: {s: "mc-mini-bg", st: "work", title: "✳ Mini job"}},
    {ws: "@devbox", set: 4, name: "", manual: false, sig: "", tree: {s: "mc-host-bg", st: "work", title: "✳ Old home tab"}},  // saved before greenlight moved to devbox
  ]};
  // the app's merged payload: the server's panes plus the Mac's (mergeTreesPayload); a stray report for the @mini session is ignored
  page = await boot(b, saved, "?host=devbox&hosts=" + encodeURIComponent(JSON.stringify(["mini", "devbox"])), {trees: TREES.trees, panes: {
    "mc-mac-bg": {state: "done", title: "✳ Mac job"}, "mc-mini-bg": {state: "done", title: "✳ Mini job"}, "mc-host-bg": {state: "done", title: "✳ Old home tab"}}});
  assert.deepEqual((await posts(page, "turn")).map(a => [a.session, a.ws]), [["mc-mac-bg", "@local"], ["mc-host-bg", "@devbox"]],
    "the @local pane that finished asks the Mac's dash.py turn; an @<scripts' host> tab asks the server's");
  await page.evaluate(() => term.turn({session: "mc-mac-bg", state: "waiting_for_user", prob: 0.9}));
  const mac = await page.evaluate(() => { const p = [...panes.values()].find(x => x.session === "mc-mac-bg"); return [p.st, p.bell, p.why] });
  assert.deepEqual(mac, ["idle", true, "Asking"], "the Mac's reading labels the alert");
  assert.equal(await page.evaluate(() => [...panes.values()].find(x => x.session === "mc-mini-bg").st), "work", "another host's pane: no backend state applies");
  await page.evaluate(() => term.turn({session: "mc-host-bg", state: "finished", prob: 0.9}));
  assert.equal((await posts(page, "badge")).pop().n, 2);
  await page.close();
});

test("alerts: split OSC title in a restored background tab still detects Claude; stale working poll after PTY finish doesn't double-ping", async page => {
  const b = page.context().browser();
  const saved = {
    cur: 1,
    tabs: [
      {ws: "/w/fix-login", set: 1, name: "", manual: false, sig: "", tree: {s: "mc-split-osc"}},
      {ws: "/w/sidebar", set: 2, name: "", manual: false, sig: "", tree: {s: "mc-fg2"}},
    ],
  };
  page = await boot(b, saved);
  await page.evaluate(() => { TURN_QUIET = 200 });
  const bgId = (await posts(page, "open")).find(o => o.session === "mc-split-osc").id;
  const feed = s => page.evaluate(([id, s]) => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode(s)))), [bgId, s]);
  await feed("\x1b]0;✳ Split");  // OSC title split across two PTY reads
  await feed(" title\x07\r\n✶ Skedaddling… (3s)\r\n");
  await page.waitForTimeout(700);
  assert.equal(await page.evaluate(id => panes.get(id).st, bgId), "work", "split OSC title recognized as Claude");
  assert.equal((await lastSave(page)).tabs[0].tree.st, "work", "working state persisted in layout");
  await feed("\x1b[1A\x1b[2K✻ Churned for 3s · done\r\n");
  await page.waitForTimeout(1000);
  const [ask] = await posts(page, "turn");
  assert.equal(ask.session, "mc-split-osc");
  await page.evaluate(() => term.turn({session: "mc-split-osc", state: "finished", prob: 0.9}));
  // Stale backend poll captured before PTY saw finish reports working, then next poll reports done -> no second turn check
  await page.evaluate(t => term.workspaces({trees: t, panes: {"mc-split-osc": {state: "working", title: "✳ Split title"}}}), TREES.trees);
  await page.evaluate(t => term.workspaces({trees: t, panes: {"mc-split-osc": {state: "done", title: "✳ Split title"}}}), TREES.trees);
  assert.equal((await posts(page, "turn")).length, 1, "stale working poll after live PTY finish never causes duplicate alert");
  await page.close();
});

test("block elements are drawn to the cell, not by the font: ░▒▓ a seamless 2×2 device-pixel dither, █▌▄ their exact part", async page => {
  const id = (await posts(page, "open"))[0].id;
  await page.evaluate(([id, s]) => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode(s)))), [id, "\x1b[38;2;255;200;0m░░▒▓█▌▄x\x1b[0m"]);
  await page.waitForTimeout(150);
  const cells = await page.evaluate(() => { const r = [...panes.values()][0].t.renderer, d = r.devicePixelRatio, W = r.metrics.width * d, H = r.metrics.height * d;
    return {W, H, lit: [...Array(8)].map((_, i) => { const px = r.ctx.getImageData(i * W, 0, W, H).data, on = [];
      for (let k = 0; k < W * H; k++) if (px[4 * k] === 255 && px[4 * k + 1] === 200 && px[4 * k + 2] === 0) on.push([i * W + k % W, Math.floor(k / W)]);
      return on })} });
  const {W, H, lit} = cells, n = c => lit[c].length, all = (c, ok) => lit[c].every(([x, y]) => ok(x, y, x - c * W));
  assert.ok(Number.isInteger(W) && Number.isInteger(H), "whole device pixels");
  for (const c of [0, 1]) { assert.equal(n(c), Math.ceil(H / 2) * [...Array(W)].filter((_, x) => (c * W + x) % 2 === 0).length, "░: one pixel in four");
    assert.ok(all(c, (x, y) => x % 2 === 0 && y % 2 === 0), "on the canvas's grid, so the next cell continues it") }
  assert.ok(n(2) > 1.8 * n(0) && n(2) < 2.2 * n(0), "▒: two in four"); assert.ok(n(3) > 2.7 * n(0) && n(3) < 3.3 * n(0), "▓: three in four");
  assert.equal(n(4), W * H, "█ fills its cell edge to edge");
  assert.equal(n(5), Math.round(W / 2) * H, "▌: the left half"); assert.ok(all(5, (x, y, dx) => dx < Math.round(W / 2)));
  assert.equal(n(6), W * (H - Math.round(H / 2)), "▄: the lower half"); assert.ok(all(6, (x, y) => y >= Math.round(H / 2)));
  assert.ok(n(7) > 0 && n(7) < W * H / 2, "other text is still the font's");
});

test("links: hovering one offers Open / Copy through the app; a click or ⌘-click opens it", async page => {
  const id = (await posts(page, "open"))[0].id, url = "https://gitlab.com/acme/widgets/-/merge_requests/5131";
  await page.evaluate(([id, s]) => term.feed(id, btoa(s)), [id, `see ${url} now\r\n`]);
  await page.waitForTimeout(100);
  const r = await page.$eval("#stage .pane canvas", c => c.getBoundingClientRect().toJSON());
  const {cw, ch} = await page.evaluate(() => { const t = [...document.querySelectorAll("#stage .pane")][0]._p.t; return {cw: t.renderer.charWidth, ch: t.renderer.charHeight} });
  await page.mouse.move(r.x + 12 * cw, r.y + ch / 2); await page.waitForTimeout(120);
  await page.mouse.move(r.x + 14 * cw, r.y + ch / 2);
  await page.waitForSelector("#lnk:not([hidden])");
  await page.click("#lnkCopy");
  assert.equal((await posts(page, "copy")).pop().text, url);
  assert.equal(await page.$eval("#lnkCopy", b => b.textContent), "Copied");
  await page.click("#lnkOpen");
  assert.equal((await posts(page, "url")).pop().url, url);
  await page.mouse.move(r.x + 14 * cw, r.y + ch / 2); await page.waitForTimeout(80);
  await page.keyboard.down("Meta"); await page.mouse.click(r.x + 14 * cw, r.y + ch / 2); await page.keyboard.up("Meta");
  assert.equal((await posts(page, "url")).length, 2);
  await page.mouse.move(r.x + 13 * cw, r.y + ch / 2); await page.waitForTimeout(80);
  await page.mouse.click(r.x + 13 * cw, r.y + ch / 2);  // a plain click, as the user expects
  assert.deepEqual((await posts(page, "url")).map(m => m.url), [url, url, url]);
  // longer than a row: it wraps, and Copy still gets all of it
  const long = "https://example.com/" + "abcdefghij".repeat(25) + "/end";
  await page.evaluate(([id, s]) => term.feed(id, btoa(s)), [id, `\x1b[2J\x1b[H${long}.\r\n`]);
  await page.mouse.move(r.x + 30 * cw, r.y + 3 * ch); await page.waitForTimeout(450);
  await page.mouse.move(r.x + 4 * cw, r.y + ch / 2); await page.waitForTimeout(80); await page.mouse.move(r.x + 6 * cw, r.y + ch / 2);
  await page.waitForTimeout(120); await page.click("#lnkCopy");
  assert.equal((await posts(page, "copy")).pop().text, long);
});

test("workspace card: the recommendation when it's on you, and the lifecycle bar opens a checklist with what's after merge", async page => {
  const row = n => page.locator(".ws", {hasText: n}).first();
  const tree = (k) => page.evaluate(k => term.workspaces({trees: [{ade: "cmux", project: "octo/greenlight", name: "sidebar", path: "/w/sidebar", branch: "b", main: false,
    agent: "done", goal: "x", puck: "sidebar", state: "raised", steps: ["mr_open", "merge", "deploy"], step_index: 2, step: "deploy", owner: "me", ...k}]}), k);
  await tree({advice: "Once it's true, sign off deploy in the chat."});
  await row("sidebar").click({button: "right"}); await page.waitForSelector("#card:not([hidden])");
  assert.equal(await page.$eval("#card .rec", d => d.textContent), "RecommendedOnce it's true, sign off deploy in the chat.");
  assert.match(await page.$eval("#card .sbar", d => d.textContent), /Step 3 of 3 · Deploy/);
  assert.equal(await page.$eval("#card .chk", d => d.hidden), true);
  await page.click("#card .sbar");
  assert.equal(await page.getAttribute("#card .sbar", "aria-expanded"), "true");
  assert.deepEqual(await page.$$eval("#card .chk li", l => l.map(e => e.className + ":" + e.textContent)),
    ["ok:✓done: Implement & open MR", "ok:✓done: Merged", "sep:After merge", "cur:●now: Deploy · on you"]);
  await page.keyboard.press("Escape");
  // no rule for the step: the suggestion's advice line, asked for on open; none when it's not on you
  await tree({advice: null, step: "qa", steps: ["mr_open", "qa", "merge"], step_index: 1});
  await row("sidebar").click({button: "right"});
  assert.match(await page.$eval("#card .rec", d => d.textContent), /Working one out/);
  await page.evaluate(() => term.suggest({name: "sidebar", label: "QA", prompt: "Run QA.", advice: "Ask QA to test the build, then sign off."}));
  assert.equal(await page.$eval("#card .rec", d => d.textContent), "RecommendedAsk QA to test the build, then sign off.");
  await page.keyboard.press("Escape");
  await tree({advice: "", steps: []});
  await row("sidebar").click({button: "right"});
  assert.equal(await page.$("#card .rec"), null); assert.equal(await page.$("#card .sbar"), null);
});

test("workspace card: facts, agent actions, inline goal, delete asks once (or never) and a refusal always asks", async page => {
  const row = n => page.locator(".ws", {hasText: n}).first();
  await page.evaluate(() => { const t = {trees: [{ade: "cmux", project: "octo/greenlight", name: "sidebar", path: "/w/sidebar", branch: "term-sidebar", main: false,
    agent: "done", since: Date.now() - 1500e3, goal: "Right-click menu", puck: "sidebar", state: "raised", created: Date.now() - 2 * 864e5, active: Date.now() - 300e3,
    mr: {ref: "!51", web_url: "https://gl/mr/51", state: "opened", threads: 2, rebase: true,
      by: {name: "sam", avatar: "nope:sam.png"}, assignees: [{name: "me", avatar: null}, {name: "review-bot"}]}}]}; term.workspaces(t) });
  await row("sidebar").click({button: "right"});
  await page.waitForSelector("#card:not([hidden])");
  const card = await page.$eval("#card", d => d.textContent);
  for (const s of ["Agent done · 25m", "term-sidebar", "Right-click menu", "!51", "Needs rebase", "2 threads", "Created 2d ago"]) assert(card.includes(s), s);
  // byline: forge avatars titled with the username; a dead image leaves the initial
  assert.deepEqual(await page.$$eval("#card .by .av", a => a.map(x => x.title)), ["sam", "me", "review-bot"]);
  assert.match(await page.$eval("#card .by", d => d.textContent), /^by s · assigned mr$/);
  assert.match(await page.$eval("#card .goal", d => d.innerHTML), /^<span><em class="lb">Goal<\/em>/);  // a well: the caps label over the goal
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "info", path: "/w/sidebar"});
  await page.evaluate(() => term.tree({op: "info", path: "/w/sidebar", dirty: 3, unpushed: 0, commit: Date.now() - 7200e3}));
  assert.equal(await page.$eval("#card .st", d => d.textContent), "3uncommitted0unpushed2hlast commit");
  const labels = await page.$$eval("#card button", b => b.map(x => x.textContent.trim()));
  for (const l of ["Terminal", "Claude", "Open !51", "Copy", "Close 0"]) assert(labels.includes(l), l);
  // the goal edits in place: Enter saves, Escape only leaves the edit
  await page.click("#card .goal"); await page.fill("#card textarea", "Popover card");
  assert.match(await page.$eval("#card .hint", d => d.textContent), /12 \/ 500/);
  await page.keyboard.press("Enter");
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "goal", path: "/w/sidebar", text: "Popover card"});
  await page.click("#card .goal"); await page.keyboard.press("Escape");
  assert.equal(await page.$eval("#card", d => d.hidden), false); assert.match(await page.$eval("#card .goal", d => d.textContent), /Popover card/);
  await page.keyboard.press("Escape");
  // greenlight's next step: asked for on open, a click starts Claude with it
  await row("sidebar").click({button: "right"});
  assert.deepEqual((await posts(page, "suggest")).pop(), {cmd: "suggest", name: "sidebar"});
  assert.match(await page.$eval("#card .nx", b => b.textContent), /Suggesting a next step/);
  await page.evaluate(() => term.suggest({name: "sidebar", label: "Fix lint job", prompt: "Fix the failing lint job on !51 and push."}));
  assert.match(await page.$eval("#card .nx", b => b.textContent), /Fix lint job.*Fix the failing lint job/);
  await page.click("#card .nx");
  assert.match((await posts(page, "in")).pop().d, /^claude \$'Fix the failing lint job on !51 and push\.'/);
  // Copy opens a small menu
  await row("sidebar").click({button: "right"}); await page.click("#card button:has-text('Copy')"); await page.click("#menu button:text-is('Branch name')");
  assert.equal((await posts(page, "copy")).pop().text, "term-sidebar");
  // delete: reads the worktree first; dirty lists what's lost and forces
  const del = async info => { await row("sidebar").click({button: "right"}); await page.click("#card button.del");
    assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "info", path: "/w/sidebar"});
    await page.evaluate(info => term.tree({op: "info", path: "/w/sidebar", ...info}), info) };
  await del({dirty: 3, untracked: 1, unpushed: 0, files: [["M", "a.js"], ["M", "b.js"], ["??", "new.txt"]]});
  await page.waitForSelector("#dlg:not([hidden]) .lost");
  assert.match(await page.$eval("#dlg .lost", d => d.textContent), /2 uncommitted changesa\.js\nb\.js1 untracked filenew\.txt/);
  assert.equal(await page.$("#dlg input[type=checkbox]"), null, "dirty: no Don't ask again");
  assert.equal(await page.$eval("#dlg button.danger", b => b.textContent), "Delete anyway");
  await page.keyboard.press("Escape"); assert.equal((await posts(page, "tree")).pop().op, "info", "Escape deletes nothing");
  // clean: confirm with "Don't ask again", then the server refuses (dirty since), and that asks to force
  await del({dirty: 0, unpushed: 0});
  await page.waitForSelector("#dlg:not([hidden]) .note"); await page.check("#dlg input[type=checkbox]"); await page.click("#dlg button.danger");
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "rm", path: "/w/sidebar"});
  assert.equal((await lastSave(page)).noAskDel, true);
  await page.evaluate(() => term.tree({op: "rm", path: "/w/sidebar", risks: ["3 uncommitted files", "1 commit no remote has (the local branch keeps them)"]}));
  await page.waitForSelector("#dlg:not([hidden]) li:has-text('3 uncommitted files')");
  assert.match(await page.$eval("#dlg ul", d => d.textContent), /1 commit no remote hasbranch keeps it/);
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "Keep it");
  await page.click("#dlg button.danger");
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "rm", path: "/w/sidebar", force: true});
  // next time: no confirm; a success closes its terminals
  await row("sidebar").click({button: "right"}); await page.click("#card button:text-is('Terminal')");
  const opened = (await posts(page, "open")).pop(); assert.equal(opened.ws, "/w/sidebar");
  await del({dirty: 0, unpushed: 0});
  assert.equal(await page.$eval("#dlg", d => d.hidden), true);
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "rm", path: "/w/sidebar"});
  await page.evaluate(() => term.tree({op: "rm", path: "/w/sidebar", ok: true}));
  assert.deepEqual((await posts(page, "close")).map(m => m.session).sort(), (await posts(page, "open")).filter(m => m.ws === "/w/sidebar").map(m => m.session).sort());
});

test("workspace card on hover: stays over the sidebar and the card, closes over the terminal, and doesn't flicker between them", async page => {
  await page.evaluate(() => newTab()); await page.waitForTimeout(200);
  const hidden = () => page.$eval("#card", c => c.hidden), box = s => page.$eval(s, e => { const b = e.getBoundingClientRect(); return {x: b.x, y: b.y, w: b.width, h: b.height} });
  const row = await page.locator(".ws", {hasText: "fix-login"}).first().boundingBox(), on = [row.x + 20, row.y + row.height / 2];
  const st = await box("#stage"), term = [st.x + st.w - 80, st.y + st.h - 40], tree = await box("#tree"), gap = [tree.x + 30, tree.y + tree.h - 8];
  let pt0 = on;  // where glide starts from
  const open = async () => { await page.mouse.move(...on, {steps: 4}); pt0 = on; await page.waitForTimeout(450); assert.equal(await hidden(), false, "resting on a row opens its card") };
  // every step of a move is watched, so a close-and-reopen on the way shows up
  const glide = async (to, steps = 10) => { const seen = []; for (let i = 1; i <= steps; i++) { await page.mouse.move(pt0[0] + (to[0] - pt0[0]) * i / steps, pt0[1] + (to[1] - pt0[1]) * i / steps); seen.push(await hidden()) } pt0 = to; return seen };
  await open(); await page.mouse.move(...term, {steps: 8}); await page.waitForTimeout(350);
  assert.equal(await hidden(), true, "row → terminal: the card closes");
  await open(); const c = await box("#card"), inCard = [c.x + 40, c.y + 40];
  assert.deepEqual(await glide(inCard), Array(10).fill(false), "row → card: open the whole way");
  await page.waitForTimeout(350); assert.equal(await hidden(), false, "resting in the card keeps it");
  assert.deepEqual(await glide(on), Array(10).fill(false), "card → row: still open");
  assert.deepEqual(await glide(gap), Array(10).fill(false), "row → the sidebar's empty space: still open");
  await page.waitForTimeout(350); assert.equal(await hidden(), false, "resting on the sidebar keeps it");
  await page.mouse.move(...inCard, {steps: 6}); await page.mouse.move(...term, {steps: 6}); await page.waitForTimeout(350);
  assert.equal(await hidden(), true, "card → terminal: the card closes");
  await open(); await page.mouse.move(st.x + 30, st.y + st.h - 40); await page.mouse.move(...gap); await page.waitForTimeout(350);
  assert.equal(await hidden(), false, "a brush over the terminal and back onto the sidebar doesn't close it");
  await page.mouse.move(...term, {steps: 4}); await page.waitForTimeout(350); assert.equal(await hidden(), true, "sidebar gap → terminal: the card closes");
  await page.locator(".ws", {hasText: "fix-login"}).first().click({button: "right"}); await page.mouse.move(...term, {steps: 4}); await page.waitForTimeout(350);
  assert.equal(await hidden(), false, "a card you opened (right-click) stays until Esc or a click outside");
});

test("workspace card: a long name, branch and next step stay inside the card", async page => {
  const b = "auto-reviewbot-health-check-devbox-run-160-20261111T1100";
  await page.evaluate(b => { term.workspaces({trees: [{ade: "cmux", project: "octo/greenlight", name: b, path: "/w/run160", branch: b, main: false, agent: "done", state: "raised", puck: b}]});
    term.suggest({name: b, label: "Confirm run deletion", prompt: "Check open MR on branch " + b + b}) }, b);
  await page.locator(".ws", {hasText: "auto-reviewbot"}).first().click({button: "right"});
  await page.waitForSelector("#card:not([hidden])");
  const over = await page.$eval("#card", c => [...c.querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > c.getBoundingClientRect().right + 1).map(e => e.className || e.tagName));
  assert.ok(await page.$("#card .nx:not([hidden])"), "the next step shows");
  assert.deepEqual(over, []);
});
test("diff: files and the patch as text; a note on a line goes to the workspace's Claude, pasted not sent", async page => {
  await page.evaluate(() => term.workspaces({trees: [{ade: "basement", project: "octo/g", name: "df", path: "/w/df", branch: "df", main: false, agent: "none"}]}));
  await page.locator(".ws", {hasText: "df"}).first().click({button: "right"});
  await page.waitForSelector("#card:not([hidden])"); await page.click("#card button:has-text('Diff')");
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "diff", path: "/w/df", text: "branch"});
  await page.evaluate(() => term.tree({op: "diff", path: "/w/df", mode: "branch", base: "origin/main", cut: false, files: [["a.js", "1", "1"]],
    patch: "diff --git a/a.js b/a.js\nindex 1..2 100644\n--- a/a.js\n+++ b/a.js\n@@ -1,2 +1,2 @@\n keep\n-old <b>\n+new <img src=x onerror=alert(1)>\n"}));
  await page.waitForSelector("#dlg:not([hidden]) .dfb");
  assert.equal(await page.$eval("#dlg .dsum", d => d.textContent), "1 file · +1 −1");
  assert.equal(await page.$("#dlg .dfv img"), null, "patch text is text");
  await page.click("#dlg .dl.ad"); await page.fill("#dlg .dn textarea", "why?");
  assert.equal(await page.$eval("#dlg button.pri", b => b.textContent), "Copy 1 note");
  await page.click("#dlg button.pri");
  assert.match((await posts(page, "copy")).pop().text, /^Notes on the diff \(this branch vs origin\/main\):\n- a\.js:2: why\?$/, "no Claude open: copied");
});
test("new workspace: the branch is the goal's slug; a taken branch opens Advanced and says so", async page => {
  await page.click("#shead button[aria-label='New workspace']"); await page.waitForSelector("#dlg:not([hidden]) textarea:focus");
  await page.selectOption("#dlg select", "/w/widgets");
  await page.keyboard.type("Fix login");
  assert.match(await page.$eval("#dlg .note2", d => d.textContent), /^Creates fix-login in acme\/widgets from the default branch$/);
  await page.click("#dlg button.pri");
  assert.equal(await page.$eval("#dlg .err", d => d.textContent), "acme/widgets already has a workspace on fix-login");
  assert.equal(await page.$eval("#dlg details", d => d.open), true);
  await page.fill("#dlg textarea", "Login keeps the page you were on!"); await page.click("#dlg button.pri");
  const p = (await posts(page, "tree")).pop(); assert.equal(p.op, "new"); assert.equal(p.path, "/w/widgets");
  assert.deepEqual(JSON.parse(p.text), {name: "login-keeps-the-page-you", base: "", goal: "Login keeps the page you were on!", agent: true, setup: ""});
});
test("new workspace: its row shows at once, and its section and project open if they were shut", async page => {
  const row = () => page.$$eval("#tree .ws", r => r.filter(x => x.querySelector(".wn").textContent === "new-one").map(x => x.querySelector(".br")?.textContent || "bare"));
  await page.click("#tree .grp.prj:has-text('acme/widgets')"); await page.click("#tree .grp.sec:has-text('Workspaces')");
  await page.evaluate(() => term.tree({op: "new", path: "/w/new-one", ok: true}));
  assert.deepEqual(await row(), ["bare"], "before the ADE lists it: a row by its tab, the section opened");
  assert.equal(await page.evaluate(() => curWs), "/w/new-one");
  await page.evaluate(t => term.workspaces({trees: [...t, {ade: "orca", project: "acme/widgets", name: "new-one", path: "/w/new-one", branch: "new-one", main: false, agent: "working"}]}), TREES.trees);
  assert.deepEqual(await row(), ["new-one"], "listed: under its project, which was shut and is opened");
  await page.click("#tree .grp.prj:has-text('acme/widgets')"); await page.evaluate(t => term.workspaces({trees: [...t, {ade: "orca", project: "acme/widgets", name: "new-one", path: "/w/new-one", branch: "new-one", main: false, agent: "working"}]}), TREES.trees);
  assert.deepEqual(await row(), [], "shut it yourself afterwards: it stays shut");
});
test("Hosts is the first section, above Needs you and Working", async page => {
  await page.evaluate(() => { newTab("/w/fix-login"); newTab("/w/sidebar"); const [a, b] = [...panes.values()]; a.title = "✳ asks"; a.bell = true; b.title = "⠋ busy"; b.st = "work"; render() });
  assert.deepEqual(await page.$$eval("#tree .box", b => b.map(x => x.className).slice(0, 4)), ["box s-host", "box s-need", "box s-work", "box s-ws"]);
});
test("ports: a remote port tunnels on click; the app's opening / open / failed relabel the row", async page => {
  await page.evaluate(() => term.workspaces({trees: [{ade: "basement", project: "octo/g", name: "pt", path: "/w/pt", branch: "pt", main: false, agent: "none"}]}));
  await page.locator(".ws", {hasText: "pt"}).first().click({button: "right"}); await page.waitForSelector("#card:not([hidden])");
  await page.evaluate(() => term.tree({op: "info", path: "/w/pt", dirty: 0, unpushed: 0, ports: [5173], procs: {5173: "vite"}}));
  const row = () => page.$eval("#card .pr", r => r.querySelector("i").className + "|" + r.querySelector("span").textContent + "|" + r.querySelector("button").textContent);
  assert.match(await page.$eval("#card .pth", d => d.textContent), /on devbox · tunnels on click/);
  assert.equal(await row(), "|vite|Tunnel & open");
  await page.click("#card .pr button"); assert.deepEqual((await posts(page, "port")).pop(), {cmd: "port", port: 5173, ws: "/w/pt"});
  await page.evaluate(() => term.tunnel("devbox:5173", "opening")); assert.equal(await row(), "opening|vite · tunnel opening…|Cancel");
  await page.click("#card .pr button"); assert.deepEqual((await posts(page, "untunnel")).pop(), {cmd: "untunnel", port: 5173, ws: "/w/pt"});
  await page.evaluate(() => term.tunnel("devbox:5173", "failed")); assert.equal(await row(), "failed|vite · tunnel failed|Retry");
  await page.evaluate(() => term.tunnel("devbox:5173", "open")); assert.equal(await row(), "open|vite · tunnel open|Open");
}, "?host=devbox");
test("workspace card: Review runs Claude with the repo's prompt, in place of the next step", async page => {
  const prompt = "Review !58 at abc123.\nIt's\tyours: don't post.";
  await page.evaluate(prompt => term.workspaces({trees: [{ade: "cmux", project: "octo/greenlight", name: "rv", path: "/w/rv", branch: "rv", main: false, agent: "done", state: "needs", puck: "rv",
    mr: {ref: "!58", state: "opened", web_url: "https://gl/mr/58"}, moves: [{kind: "review", prompt, own: true, repo: "greenlight"}]}]}), prompt);
  await page.locator(".ws", {hasText: "rv"}).first().click({button: "right"});
  await page.waitForSelector("#card:not([hidden])");
  assert.equal(await page.$("#card .nx"), null, "Review replaces the next step");
  assert.equal((await posts(page, "suggest")).length, 0);
  assert.equal(await page.$eval("#card .rv .go", b => b.textContent), "Review !58Claude drafts it; nothing posts until you pick");
  assert.equal(await page.$eval("#card .rv", b => b.className), "rv or", "one move: its own color");
  const over = await page.$eval("#card", c => [...c.querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > c.getBoundingClientRect().right + 1).map(e => e.className || e.tagName));
  assert.deepEqual(over, []);
  await page.click("#card .rv .mn"); await page.click("#menu button:text-is('Show the prompt')");
  assert.equal(await page.$eval("#card .rvp", d => d.textContent), prompt);
  await page.click("#card .rv .mn"); await page.click("#menu button:text-is('Copy prompt')");
  assert.equal((await posts(page, "copy")).pop().text, prompt);
  await page.click("#card .rv .mn"); await page.click("#menu button:has-text('Edit greenlight')");
  assert.deepEqual((await posts(page, "settings")).pop(), {cmd: "settings", repo: "greenlight", kind: "review"});
  await page.locator(".ws", {hasText: "rv"}).first().click({button: "right"}); await page.click("#card .rv .go");
  assert.equal((await posts(page, "in")).pop().d, "claude $'Review !58 at abc123.\\x0aIt\\'s\\x09yours: don\\'t post.'\r");
});
test("workspace card: the most urgent move on top, the rest under it, Terminal and Claude below", async page => {
  await page.evaluate(() => term.workspaces({trees: [{ade: "cmux", project: "octo/greenlight", name: "fx", path: "/w/fx", branch: "fx", main: false, agent: "done", state: "needs", puck: "fx",
    mr: {ref: "!60", state: "opened", target_branch: "main"}, moves: [{kind: "threads", n: 2, prompt: "fix threads", own: false, repo: "greenlight"}, {kind: "ci", prompt: "fix ci", own: false, repo: "greenlight"}]}]}));
  await page.locator(".ws", {hasText: "fx"}).first().click({button: "right"});
  await page.waitForSelector("#card:not([hidden])");
  const kids = await page.$$eval("#card > *", k => k.map(e => e.className));
  assert.ok(kids.indexOf("rv bl") >= 0 && kids.indexOf("rv bl") < kids.indexOf("also") && kids.indexOf("also") < kids.indexOf("acts"), kids.join(","));
  assert.match(await page.$eval("#card .rv .go", b => b.textContent), /^Resolve 2 threads/, "several fixes: blue");
  assert.equal(await page.$("#card .nx"), null);
  await page.click("#card .also button:text-is('Fix CI')");
  assert.equal((await posts(page, "in")).pop().d, "claude $'fix ci'\r");
});
test("workspace card: Merge asks once more, then merges; the forge's refusal shows verbatim", async page => {
  const t = ok => ({trees: [{ade: "cmux", project: "octo/greenlight", name: "mg", path: "/w/mg", branch: "mg", main: false, agent: "done", state: "needs", puck: "mg",
    mr: {ref: "!61", state: "opened"}, moves: [{kind: "merge", how: "Squash", target: "main"}, {kind: "review", prompt: "rv", own: false, repo: "greenlight"}]}]});
  await page.evaluate(t => term.workspaces(t), t());
  await page.locator(".ws", {hasText: "mg"}).first().click({button: "right"});
  await page.waitForSelector("#card:not([hidden])");
  assert.match(await page.$eval("#card .mg .go", b => b.textContent), /Merge !61Squash into main/);
  await page.click("#card .mg .go");
  assert.match(await page.$eval("#card .mg .go", b => b.textContent), /Confirm merge !61/);
  assert.equal((await posts(page, "tree")).filter(p => p.op === "merge").length, 0, "one click doesn't merge");
  await page.click("#card .mg .lnk");
  assert.match(await page.$eval("#card .mg .go", b => b.textContent), /^Merge !61/, "Cancel puts it back");
  await page.click("#card .mg .go"); await page.click("#card .mg .go");
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "merge", path: "/w/mg"});
  assert.match(await page.$eval("#card .mg", b => b.textContent), /Merging !61/);
  await page.evaluate(() => term.tree({op: "merge", path: "/w/mg", error: "405: Branch cannot be merged"}));
  assert.equal(await page.$eval("#card .mg code", b => b.textContent), "405: Branch cannot be merged");
  await page.locator(".ws", {hasText: "mg"}).first().click({button: "right"}); await page.click("#card .mg .go"); await page.click("#card .mg .go");
  await page.evaluate(() => term.tree({op: "merge", path: "/w/mg", ok: true, ref: "!61", target: "main", how: "Squash"}));
  assert.match(await page.$eval("#card .mg.ok", b => b.textContent), /Merged !61 into main/);
});
test("workspace card: Mark ready takes a kept draft out of draft in one click", async page => {
  await page.evaluate(() => term.workspaces({trees: [{ade: "cmux", project: "octo/greenlight", name: "dr", path: "/w/dr", branch: "dr", main: false, agent: "done", state: "raised", puck: "dr",
    mr: {ref: "!62", state: "opened"}, moves: [{kind: "ready"}]}]}));
  await page.locator(".ws", {hasText: "dr"}).first().click({button: "right"}); await page.waitForSelector("#card:not([hidden])");
  assert.match(await page.$eval("#card .mg .go", b => b.textContent), /Mark !62 ready for review/);
  assert.equal(await page.$("#card .nx"), null, "the button replaces the next step");
  await page.click("#card .mg .go");
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "ready", path: "/w/dr"});
  await page.evaluate(() => term.tree({op: "ready", path: "/w/dr", ok: true, ref: "!62"}));
  assert.match(await page.$eval("#card .mg.ok", b => b.textContent), /!62 is ready for review/);
});
test("workspace card: Deploy runs a merged MR's step after the merge, then signs it off", async page => {
  await page.evaluate(() => term.workspaces({trees: [{ade: "cmux", project: "octo/greenlight", name: "dp", path: "/w/dp", branch: "dp", main: false, agent: "done", state: "raised", puck: "dp",
    mr: {ref: "#7", state: "merged"}, moves: [{kind: "signoff", step: "deploy", run: "git pull"}]}]}));
  await page.locator(".ws", {hasText: "dp"}).first().click({button: "right"}); await page.waitForSelector("#card:not([hidden])");
  assert.match(await page.$eval("#card .mg .go", b => b.textContent), /Deploy #7.*Runs git pull, then signs it off/);
  await page.click("#card .mg .go");
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "signoff", path: "/w/dp"});
  await page.evaluate(() => term.tree({op: "signoff", path: "/w/dp", step: "deploy", error: "`git pull` exited 1: no"}));
  assert.match(await page.$eval("#card .mg.no", b => b.textContent), /Deploy didn't finish.*exited 1: no/);
});
test("workspace card: the main checkout can't be deleted; a busy agent disables the agent actions", async page => {
  await page.locator(".ws", {hasText: "widgets"}).first().click({button: "right"});
  assert.equal(await page.$("#card .nx"), null, "not waiting on you: no next-step button");
  assert.equal((await posts(page, "suggest")).length, 0);
  await page.keyboard.press("Escape");
  await page.locator(".ws", {hasText: "widgets"}).first().click({button: "right"});
  assert.equal(await page.$("#card button.del"), null);
  await page.keyboard.press("Escape"); assert.equal(await page.$eval("#card", d => d.hidden), true);
  await page.locator(".ws", {hasText: "fix-login"}).first().click({button: "right"});
  assert.equal(await page.$eval("#card button:text-is('Claude')", b => b.disabled), true);
});

test("workspace card: hovering a row opens it without taking focus; leaving closes it, a click in it keeps it", async page => {
  const row = page.locator(".ws", {hasText: "fix-login"}).first();
  await row.hover(); await page.waitForSelector("#card:not([hidden])");
  assert.equal(await page.evaluate(() => !!document.activeElement.closest("#card")), false);
  await page.hover("#card .st"); await page.waitForTimeout(400);
  assert.equal(await page.$eval("#card", d => d.hidden), false, "moving into the card keeps it");
  assert.equal(await row.getAttribute("title"), null, "no tooltip over the card");
  await page.locator(".ws", {hasText: "widgets"}).first().hover(); await page.waitForTimeout(60);
  assert.equal(await page.$eval("#card", d => d.getAttribute("aria-label")), "widgets", "with a card up, another row switches at once");
  await page.hover("#stage"); await page.waitForSelector("#card[hidden]", {state: "attached"});
  await row.hover(); await page.waitForSelector("#card:not([hidden])");
  await page.click("#card .st"); await page.hover("#stage"); await page.waitForTimeout(400);
  assert.equal(await page.$eval("#card", d => d.hidden), false, "a click in it keeps it");
});

test("auto-run: the main window runs a marked move once per head, in an unfocused bolt tab that closes when the job is gone", async page => {
  const w = (moves, agent = "none", sha = "s1") => ({trees: [{ade: "cmux", project: "octo/greenlight", name: "ar", path: "/w/ar", branch: "ar", main: false, agent,
    mr: {ref: "!7", state: "opened", sha}, moves}]});
  const ci = {kind: "ci", prompt: "fix", auto: "unattended: fix", sha: "s1", jobs: []};
  const was = await page.evaluate(() => cur.key);
  await page.evaluate(t => term.workspaces(t), w([ci], "working"));
  assert.equal(await page.evaluate(() => tabs.filter(t => t.auto).length), 0, "not beside a working agent");
  await page.evaluate(t => term.workspaces(t), w([ci]));
  assert.deepEqual(await page.evaluate(() => tabs.filter(t => t.auto).map(t => [t.name, t.auto.kind, t.ws])), [["Fix CI · !7", "ci", "/w/ar"]]);
  assert.equal(await page.evaluate(() => cur.key), was, "focus stays where it was");
  assert.equal((await posts(page, "in")).pop().d, "claude $'unattended: fix'\r");
  assert.equal(await page.$$eval('#side svg[data-icon="bolt"]', x => x.length), 1, "the bolt");
  await page.evaluate(t => term.workspaces(t), w([ci]));
  assert.equal(await page.evaluate(() => tabs.filter(t => t.auto).length), 1, "once per head");
  await page.evaluate(() => { paneOf(tabs.find(t => t.auto))[0].out = Date.now() });
  await page.evaluate(t => term.workspaces(t), w([ci]));  // working
  await page.evaluate(() => { paneOf(tabs.find(t => t.auto))[0].out = 0 });
  const pushed = (t, h) => (t.trees[0].pushed = h, t);
  await page.evaluate(t => term.workspaces(t), pushed(w([ci]), "s2"));  // idle and pushed, greenlight hasn't scanned it yet: wait
  assert.equal((await posts(page, "notify")).length, 0);
  await page.evaluate(t => term.workspaces(t), pushed(w([ci]), "s1"));  // idle, greenlight has seen it all, still failing: needs you
  assert.match((await posts(page, "notify")).pop().title, /^Fix CI · !7 needs you$/);
  const run = w([], "none", "s2"); run.trees[0].mr.pipeline = "running";
  await page.evaluate(t => term.workspaces(t), run);  // CI running on the fix: not done yet
  assert.equal(await page.evaluate(() => tabs.filter(t => t.auto).length), 1);
  await page.evaluate(t => term.workspaces(t), w([], "none", "s2"));  // CI no longer failing: done, the tab goes
  assert.equal(await page.evaluate(() => tabs.filter(t => t.auto).length), 0);
  assert.match((await posts(page, "notify")).pop().title, /^Done: Fix CI · !7$/);
  await page.evaluate(t => term.workspaces(t), w([{kind: "merge", auto: true, sha: "s2"}], "none", "s2"));
  assert.deepEqual((await posts(page, "tree")).pop(), {cmd: "tree", op: "auto-merge", path: "/w/ar"});
  await page.evaluate(() => term.tree({op: "auto-merge", path: "/w/ar", ok: true, ref: "!7", target: "main", how: "Squash"}));
  assert.equal((await posts(page, "notify")).pop().title, "Merged !7 into main", "a notification, not a dialog");
  assert.deepEqual((await lastSave(page)).autoDone, ["/w/ar|ci|s1", "/w/ar|merge|s2"]);
}, "?main=1");

test("auto-run: never in a dev copy, its main window included", async page => {
  await page.evaluate(() => term.workspaces({trees: [{ade: "cmux", project: "octo/greenlight", name: "ar", path: "/w/ar", branch: "ar", main: false, agent: "none",
    mr: {ref: "!7", state: "opened", sha: "s1"}, moves: [{kind: "ci", prompt: "fix", auto: "x", sha: "s1"}, {kind: "merge", auto: true, sha: "s1"}]}]}));
  assert.equal(await page.evaluate(() => tabs.filter(t => t.auto).length), 0, "no tab of its own");
  assert.equal((await posts(page, "tree")).filter(m => m.op === "merge").length, 0, "no merge");
  assert.deepEqual((await lastSave(page))?.autoDone || [], [], "nothing marked tried");
}, "?main=1&dev=faster-qa");

test("auto-run: only the main window runs it", async page => {
  await page.evaluate(() => term.workspaces({trees: [{ade: "cmux", project: "octo/greenlight", name: "ar", path: "/w/ar", branch: "ar", main: false, agent: "none",
    mr: {ref: "!7", state: "opened"}, moves: [{kind: "ci", prompt: "fix", auto: "x", sha: "s1"}]}]}));
  assert.equal(await page.evaluate(() => tabs.filter(t => t.auto).length), 0);
});

test("the wheel goes to tmux's history when the app asks for the mouse; typing after a wheel up leaves it; ⇧ scrolls the page's own", async page => {
  await page.evaluate(() => newTab()); await page.waitForTimeout(300);
  const id = await page.evaluate(() => [...panes.keys()].pop());
  const feed = s => page.evaluate(([i, b]) => term.feed(i, b), [id, Buffer.from(s).toString("base64")]);
  const ins = async () => (await posts(page, "in")).filter(m => m.id === id).map(m => m.d);
  const wheel = (dy, shift = false) => page.evaluate(([dy, shift]) => { const h = panes.get([...panes.keys()].pop()).box.querySelector(".t"), r = h.getBoundingClientRect();
    h.querySelector("canvas").dispatchEvent(new WheelEvent("wheel", {deltaY: dy, deltaMode: 1, shiftKey: shift, clientX: r.left + 5, clientY: r.top + 5, bubbles: true, cancelable: true})) }, [dy, shift]);
  await feed(Array.from({length: 60}, (_, i) => `line ${i}\r\n`).join(""));
  await wheel(-3); assert.deepEqual(await ins(), [], "no mouse asked for: ghostty-web scrolls its own, nothing to the pty");
  await feed("\x1b]6973;greenlight-tmux\x07\x1b[?1000h\x1b[?1002h\x1b[?1006h");  // paneCmd's mark, then tmux's mouse on
  await wheel(-3); assert.deepEqual(await ins(), ["\x1b[<64;1;1M".repeat(3)], "three lines up: three SGR wheel-up reports");
  await wheel(2); assert.equal((await ins()).pop(), "\x1b[<65;1;1M".repeat(2));
  await wheel(-5, true); assert.equal((await ins()).length, 2, "⇧-wheel: the page's own scrollback");
  await page.evaluate(() => panes.get([...panes.keys()].pop()).t.input("x", true));
  assert.equal((await ins()).pop(), "\x1b[<93;1;1Mx", "the next key leaves copy-mode first");
  await page.evaluate(() => panes.get([...panes.keys()].pop()).t.input("y", true)); assert.equal((await ins()).pop(), "y", "once");
  await page.evaluate(() => clearPane(panes.get([...panes.keys()].pop())));
  assert.ok((await ins()).includes("\x1b[<93;1;1M\x1b[<92;1;1M"), "⌘K clears tmux's history too");
});

test("the wheel without Greenlight's tmux: an app's mouse modes get no ⌃⌥⇧ events; ⌃ scrolls as the plain wheel; ⇧ sideways scrolls the page", async page => {
  await page.evaluate(() => newTab()); await page.waitForTimeout(300);
  const id = await page.evaluate(() => [...panes.keys()].pop());
  const feed = s => page.evaluate(([i, b]) => term.feed(i, b), [id, Buffer.from(s).toString("base64")]);
  const ins = async () => (await posts(page, "in")).filter(m => m.id === id).map(m => m.d);
  const wheel = o => page.evaluate(o => { const h = panes.get([...panes.keys()].pop()).box.querySelector(".t"), r = h.getBoundingClientRect();
    h.querySelector("canvas").dispatchEvent(new WheelEvent("wheel", {deltaMode: 1, clientX: r.left + 5, clientY: r.top + 5, bubbles: true, cancelable: true, ...o})) }, o);
  await feed(Array.from({length: 80}, (_, i) => `line ${i}\r\n`).join("") + "\x1b[?1000h\x1b[?1006h");  // vim's mouse=a in a plain shell
  await page.evaluate(() => panes.get([...panes.keys()].pop()).t.scrollToBottom());
  await wheel({deltaY: 0, deltaX: -4, shiftKey: true});
  assert.equal(await page.evaluate(() => panes.get([...panes.keys()].pop()).t.getViewportY()), 4, "⇧ and a mouse's wheel (sideways on macOS): the page's scrollback");
  await wheel({deltaY: -3});
  await page.evaluate(() => panes.get([...panes.keys()].pop()).t.input("j", true));
  await page.evaluate(() => clearPane(panes.get([...panes.keys()].pop())));
  assert.deepEqual(await ins(), ["j", "\x0c"], "no wheel reports, no exit before the key, no ⌘K pair");
  await feed("\x1b]6973;greenlight-tmux\x07");
  assert.ok(!(await page.evaluate(() => { const b = panes.get([...panes.keys()].pop()).t.buffer.active; return Array.from({length: b.length}, (_, y) => b.getLine(y)?.translateToString(true)).join("\n") })).includes("greenlight-tmux"), "the mark draws nothing");
  await wheel({deltaY: -2, ctrlKey: true});
  assert.equal((await ins()).pop(), "\x1b[<64;1;1M".repeat(2), "⌃-wheel: plain reports, as tmux binds no modified wheel");
  await page.evaluate(i => term.exited(i, 255), id);  // the connection drops, after a wheel up
  await page.evaluate(() => panes.get([...panes.keys()].pop()).t.input("r", true));  // the key that reconnects
  await page.evaluate(() => panes.get([...panes.keys()].pop()).t.input("k", true));
  assert.equal((await ins()).pop(), "k", "after a reconnect, no exit before the key: the host may have no tmux now");
  await wheel({deltaY: -2});
  assert.equal((await ins()).pop(), "k", "and no wheel reports until the new connection prints the mark");
});

test("the tree from the keyboard: one tab stop; ↑↓ walk, ← folds or climbs, → unfolds, ↵ opens; row actions show on focus", async page => {
  const stops = () => page.$$eval("#tree [tabindex='0']", x => x.length);
  const at = () => page.evaluate(() => document.activeElement.dataset.k || "");
  assert.equal(await stops(), 1, "one tab stop in the tree");
  await page.evaluate(() => document.querySelector("#tree .grp.sec[data-k='g§ws']").focus());
  await page.keyboard.press("ArrowLeft");
  assert.equal(await page.getAttribute("#tree .grp.sec[data-k='g§ws']", "aria-expanded"), "false", "← folds a section");
  assert.equal(await at(), "g§ws", "and the keyboard stays on it through the re-render");
  await page.keyboard.press("ArrowRight");
  assert.equal(await page.getAttribute("#tree .grp.sec[data-k='g§ws']", "aria-expanded"), "true", "→ unfolds it");
  await page.keyboard.press("ArrowDown"); assert.match(await at(), /^gws\//, "↓: its first project");
  await page.keyboard.press("ArrowDown"); assert.equal(await at(), "w/w/sidebar", "↓: that project's first workspace");
  assert.equal(await page.$eval("#tree .ws[data-k='w/w/sidebar'] .ib", b => getComputedStyle(b).display), "flex", "its + shows on focus, not only on hover");
  await page.keyboard.press("ArrowLeft"); assert.match(await at(), /^gws\//, "← from a row: up to its project");
  await page.keyboard.press("ArrowDown"); await page.keyboard.press("Enter");
  assert.equal((await posts(page, "open")).pop().ws, "/w/sidebar", "↵ opens the workspace");
  assert.equal(await stops(), 1);
});

test("sidebar states: a skeleton until the workspaces come, then none; no workspaces says so and offers New workspace…", async page => {
  const b = page.context().browser(), fresh = await b.newPage({viewport: {width: 1200, height: 800}, colorScheme: "dark"});
  await fresh.addInitScript(() => { window.__posts = []; window.webkit = {messageHandlers: {term: {postMessage: m => window.__posts.push(JSON.parse(JSON.stringify(m)))}}} });
  await fresh.goto(PAGE); await fresh.waitForFunction(() => window.__posts.some(m => m.cmd === "ready"));
  await fresh.evaluate(() => { term.restore(null); if (document.body.classList.contains("noside")) toggleSide() });
  assert.equal(await fresh.$$eval("#tree .skel i", x => x.length), 6, "3 rows a section, 2 sections");
  await fresh.evaluate(() => term.workspaces({trees: []}));
  assert.equal(await fresh.$("#tree .skel"), null);
  assert.match(await fresh.textContent("#tree .nows"), /No workspaces yet/);
  assert.equal(await fresh.textContent("#tree .nows button"), "New workspace…");
  await fresh.evaluate(t => term.workspaces(t), TREES);
  assert.equal(await fresh.$("#tree .nows"), null, "gone once there are some");
  await fresh.close();
});

test("refresh is busy until the workspaces come back", async page => {
  const busy = () => page.getAttribute("#shead [aria-label='Refresh workspaces']", "aria-busy");
  await page.click("#shead [aria-label='Refresh workspaces']");
  assert.equal((await posts(page, "trees")).length > 0, true);
  assert.equal(await busy(), null, "no flash: it waits 200ms");
  await page.waitForTimeout(260); assert.equal(await busy(), "true");
  await page.evaluate(t => term.workspaces(t), TREES); await page.waitForTimeout(450);
  assert.equal(await busy(), null);
});

test("a host that can't be reached says so on its row; Retry opens its sessions again", async page => {
  page = await boot(page.context().browser(), null, "?host=devbox&hosts=" + encodeURIComponent(JSON.stringify(["mini", "devbox"])));
  await page.click("#tree .hchip:has-text('mini')");
  const o = (await posts(page, "open")).pop();
  await page.evaluate(id => term.exited(id, 255), o.id);
  assert.match(await page.textContent("#tree .hc .lost"), /Can't reach mini/);
  await page.click("#tree .hc .lost button");
  const again = (await posts(page, "open")).pop();
  assert.deepEqual([again.id, again.session, again.ws], [o.id, o.session, "@mini"]);
  assert.equal(await page.$("#tree .lost"), null);
  assert.deepEqual(page._errs, []); await page.close();
});

test("appearance: the chrome follows the system, the window's Light/Dark holds it, and the terminals keep their colors", async page => {
  const look = () => page.evaluate(() => [document.documentElement.dataset.appearance || "", getComputedStyle($("bar")).backgroundColor]);
  const [a0, dark] = await look(); assert.equal(a0, "", "the system's");
  const theme = await page.evaluate(() => JSON.stringify([...panes.values()][0].t.options.theme));
  await page.evaluate(() => term.mode("light")); const [a1, lit] = await look();
  assert.equal(a1, "light"); assert.notEqual(lit, dark, "the strip goes light");
  assert.equal(await page.evaluate(() => JSON.stringify([...panes.values()][0].t.options.theme)), theme, "the terminals keep theirs");
  await page.evaluate(() => term.mode("")); assert.deepEqual(await look(), ["", dark], "Auto: back to the system");
  assert.equal(await page.evaluate(() => document.documentElement.style.getPropertyValue("--strip")), "", "no look worn: the tokens, nothing inline");
});

test("a workspace page's Basement button: its agent's live session, else a tab running it, else the harness continuing (issue 157)", async page => {
  const opens = async () => (await posts(page, "open")).filter(o => o.ws === "/w/review");
  const typed = async id => (await posts(page, "in")).filter(m => m.id === id).map(m => m.d);
  await page.evaluate(() => term.ws("/w/review", {harness: "codex"}));  // exited: a new tab there continuing its last session
  let [o] = await opens(); assert.ok(o, "a tab in the worktree");
  assert.deepEqual(await typed(o.id), ["codex resume --last\r"]);
  await page.evaluate(() => term.ws("/w/review", {harness: "nope; rm -rf ~"}));  // not a harness: the plain button, its latest tab
  assert.equal((await opens()).length, 1, "no command for an unknown harness");
  await page.evaluate(() => term.ws("/w/review", {harness: "claude", session: "mc-1a2b3c4d"}));  // alive: attach to it, type nothing
  const at = (await opens()).pop(); assert.equal(at.session, "mc-1a2b3c4d"); assert.deepEqual(await typed(at.id), []);
  await page.evaluate(() => show(tabs[0]));
  await page.evaluate(() => term.ws("/w/review", {harness: "claude", session: "mc-1a2b3c4d"}));  // its tab is open: show it, no new one
  assert.equal((await opens()).length, 2); assert.equal(await page.evaluate(() => cur.focus.session), "mc-1a2b3c4d");
  await page.evaluate(id => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode("\x1b]0;⌬ review\x07")))), at.id);
  await page.evaluate(() => show(tabs[0]));
  await page.evaluate(() => term.ws("/w/review", {harness: "codex"}));  // a tab there runs codex (tmux's ⌬): that one, not a second copy
  assert.equal((await opens()).length, 2); assert.equal(await page.evaluate(() => cur.focus.session), "mc-1a2b3c4d");
  await page.evaluate(id => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode("\x1b]0;✳ review\x07")))), at.id);
  await page.evaluate(() => show(tabs[0]));
  await page.evaluate(() => term.ws("/w/review", {harness: "claude"}));  // a ✳ title with no live session is a quit Claude: continue it
  const cl = (await opens()).pop(); assert.equal((await opens()).length, 3); assert.deepEqual(await typed(cl.id), ["claude --continue\r"]);
  await page.evaluate(() => show(tabs[0]));
  await page.evaluate(() => term.ws("/w/review", {harness: "claude"}));  // clicked again before the next snapshot: that tab, no second copy
  assert.equal((await opens()).length, 3); assert.equal(await page.evaluate(() => cur.focus.session), cl.session);
  await page.evaluate(() => term.ws("/w/shipped", {harness: "claude", session: "bad session; x"}));  // a bad session name: ignored
  const sh = (await posts(page, "open")).filter(o => o.ws === "/w/shipped");
  assert.equal(sh.length, 1); assert.notEqual(sh[0].session, "bad session; x"); assert.deepEqual(await typed(sh[0].id), ["claude --continue\r"]);
  await page.evaluate(() => term.ws("/w/fix-login", {}));  // never ran an agent: as before, a plain shell
  const pl = (await posts(page, "open")).filter(o => o.ws === "/w/fix-login");
  assert.equal(pl.length, 1); assert.deepEqual(await typed(pl[0].id), []);
  // /w/review has a newer plain tab than its Claude one: nothing recorded keeps the old button (its latest tab)
  await page.evaluate(() => { newTab("/w/review"); show(tabs[0]) });
  const plain = await page.evaluate(() => latestTab(t => t.ws === "/w/review").focus.session);
  await page.evaluate(() => term.ws("/w/review", {}));
  assert.equal(await page.evaluate(() => cur.focus.session), plain, "the latest tab, not the agent's");
  await page.evaluate(() => show(tabs[0]));
  await page.evaluate(id => term.feed(id, btoa("\x1b]0;zsh\x07")), at.id);  // codex quit there
  await page.evaluate(() => tabs.forEach(t => { if (t.resumed) t.resumed.at -= 61e3 }));  // and the ones this started are a minute old
  await page.evaluate(() => term.ws("/w/review", {harness: "codex", bin: "/opt/cx/codex"}));  // no tab runs codex: continue it
  const cx = (await opens()).pop(); assert.deepEqual(await typed(cx.id), ["/opt/cx/codex resume --last\r"]);
  await page.evaluate(() => term.ws("/w/review", {harness: "pi", bin: "/x/../pi; rm"}));  // a bad bin: the PATH's
  assert.deepEqual(await typed((await opens()).pop().id), ["pi --continue\r"]);
});

test("a codex tab's ⌬ title is saved, so after a relaunch the Basement button shows it rather than start a second codex (issue 157)", async page => {
  await page.evaluate(() => term.ws("/w/review", {harness: "codex"}));
  const o = (await posts(page, "open")).filter(x => x.ws === "/w/review").pop();
  await page.evaluate(id => term.feed(id, btoa(String.fromCharCode(...new TextEncoder().encode("\x1b]0;⌬ review\x07")))), o.id);
  const st = await lastSave(page);
  const again = await boot(page.context().browser(), st);
  const before = (await posts(again, "open")).length;
  await again.evaluate(() => term.ws("/w/review", {harness: "codex"}));  // before any output: the saved title says codex runs there
  assert.equal((await posts(again, "open")).length, before, "no new tab");
  assert.equal(await again.evaluate(() => cur.focus.session), o.session);
  assert.deepEqual(again._errs, []); await again.close();
});

test("a rejected promise posts {cmd: error} with its name, for the app's diag trail", async page => {
  await page.evaluate(() => { Promise.reject(new TypeError("boom")) });
  await page.waitForFunction(() => window.__posts.some(m => m.cmd === "error" && m.name === "TypeError"));
  page._errs.splice(0);  // the rejection is the point
});

test("Basement supports Codex and agy: tab icons, configured default agent launch, and diff notes", async page => {
  const b64 = s => Buffer.from(s, "utf8").toString("base64");
  const id = await page.evaluate(() => cur.focus.id);
  await page.evaluate(([id, data]) => term.feed(id, data), [id, b64("\x1b]0;◈ Antigravity task\x07")]);
  assert.equal(await page.$eval("#tabs .tab.on .nm", e => e.textContent), "Antigravity task", "◈ prefix stripped from title");
  assert.ok(await page.$("#tabs .tab.on svg[data-icon='agy'].ic-agy"), "agy icon uses ds-icons mark with ic-agy class");
  await page.evaluate(() => term.workspaces({
    agents: ["agy", "codex", "claude"],
    trees: [{ade: "orca", project: "octo/greenlight", name: "fix-agy", path: "/w/fix-agy", branch: "fix-agy", main: false, agent: "none",
      mr: {ref: "!99", state: "opened", sha: "s1"}, moves: [{kind: "ci", prompt: "Fix CI now", jobs: ["lint"]}]}],
  }));
  await page.locator(".ws", {hasText: "fix-agy"}).first().click({button: "right"});
  assert.ok(await page.$("#card button:text-is('Antigravity')"), "primary agent button follows configured default agent");
  await page.click("#card .rv .go");
  assert.equal((await posts(page, "in")).pop().d, "agy -i $'Fix CI now'\r", "agy launches with -i");
  await page.evaluate(() => term.workspaces({
    agents: ["codex", "claude"],
    trees: [{ade: "orca", project: "octo/greenlight", name: "fix-agy", path: "/w/fix-agy", branch: "fix-agy", main: false, agent: "none",
      mr: {ref: "!99", state: "opened", sha: "s1"}, moves: [{kind: "ci", prompt: "Fix CI now", jobs: ["lint"]}]}],
  }));
  await page.locator(".ws", {hasText: "fix-agy"}).first().click({button: "right"});
  assert.ok(await page.$("#card button:text-is('Codex')"), "primary agent button switches to Codex");
  await page.click("#card .rv .go");
  assert.equal((await posts(page, "in")).pop().d, "codex $'Fix CI now'\r", "codex launches with positional prompt");
  // Diff note pastes directly into an open Codex or agy pane, not only Claude
  const codexId = await page.evaluate(() => cur.focus.id);
  await page.evaluate(([id, data]) => term.feed(id, data), [codexId, b64("\x1b]0;⌬ Codex session\x07")]);
  await page.evaluate(() => { diffs.add("/w/fix-agy"); diffShow({op: "diff", path: "/w/fix-agy", mode: "branch", base: "origin/main", files: [["a.py", "1", "0"]], status: ["M"],
    patch: "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n+x = 1\n"}); });
  await page.click("#dlg .dl.ad");
  await page.fill("#dlg .dn textarea", "check this");
  assert.match(await page.textContent("#dlg button.pri"), /Paste 1 note into Codex/);
  assert.match(await page.textContent("#dlg .dn .lb"), /Note to Codex on a\.py:1/);
  // syncPanes preserves agy/codex identity even when pane_states sends an unprefixed title + agent field
  const sess = await page.evaluate(() => { cur.focus.out = 0; return cur.focus.session; });
  await page.evaluate(s => term.workspaces({agents: ["agy"], trees: [], panes: {[s]: {state: "working", title: "raw-agy", agent: "agy"}}}), sess);
  assert.equal(await page.evaluate(() => agentOf(cur.focus)), "agy", "syncPanes keeps agy identity via prefix");
});

// issue 128: ⌘V of a screenshot, a file dropped on a pane, ⌘⇧P's repo Commands and Open in editor, as the page sees them
const unbracket = s => s.replace(/\x1b\[20[01]~/g, "");
const ins = async (page, id) => (await posts(page, "in")).filter(m => m.id === id).map(m => unbracket(m.d));
const filePosts = async (page, n) => { await page.waitForFunction(n => window.__posts.filter(m => m.cmd === "file").length >= n, n); return posts(page, "file") };
// a paste event as WebKit hands it to the page: text, files, or both, on the pane (or a selector)
const paste = (page, o) => page.evaluate(o => { const dt = new DataTransfer();
  if (o.text != null) dt.setData("text/plain", o.text);
  (o.files || []).forEach(([n, type, size]) => dt.items.add(new File([size ? new Uint8Array(size) : "PNGDATA"], n, {type})));
  const e = new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true});
  (o.sel ? document.querySelector(o.sel) : cur.focus.box.querySelector(".t")).dispatchEvent(e); return e.defaultPrevented }, o);
const drag = (page, type, o) => page.evaluate(([type, o]) => { const dt = new DataTransfer();
  (o.files || []).forEach(n => dt.items.add(new File(["x"], n, {type: "text/plain"}))); if (o.text) dt.setData("text/plain", o.text);
  const e = new DragEvent(type, {dataTransfer: dt, bubbles: true, cancelable: true});
  (o.pane ? panes.get(o.pane).box.querySelector(".t") : document.querySelector(o.sel || "#stage")).dispatchEvent(e); return e.defaultPrevented }, [type, o]);
const toastText = page => page.evaluate(() => $("toast").hidden ? null : {b: $("toast").querySelector("b").textContent, sub: $("toast").querySelector("small").textContent});

test("⌘V of a screenshot: the app gets it as pasted-<tag>.png, then its quoted path is typed and the toast says Screenshot (issue 128)", async page => {
  const {id, ws} = await page.evaluate(() => ({id: cur.focus.id, ws: cur.focus.tab.ws}));
  assert.equal(await paste(page, {files: [["image.png", "image/png"]]}), true, "the page takes it from the terminal");
  const [f] = await filePosts(page, 1);
  assert.match(f.name, /^pasted-\w+\.png$/, "WebKit's image.png goes as pasted-<tag>.png");
  assert.deepEqual({...f, name: ""}, {cmd: "file", id, ws, name: "", data: Buffer.from("PNGDATA").toString("base64")});
  assert.deepEqual(await ins(page, id), [], "nothing typed until the app says where it went");
  const at = `/tmp/greenlight-501/ab12cd34-${f.name}`;
  await page.evaluate(([id, at]) => term.filed(id, at), [id, at]);
  assert.deepEqual(await ins(page, id), [`'${at}' `], "the path, shell-quoted, then a space; not sent");
  assert.deepEqual(await toastText(page), {b: "Screenshot sent to this Mac", sub: at});
  await page.click("#toast button:has-text('Copy path')");
  assert.deepEqual((await posts(page, "copy")).pop(), {cmd: "copy", text: at});
  await page.evaluate(([id]) => term.filed(id, "/tmp/greenlight-501/0000-it's.txt"), [id]);
  assert.equal((await ins(page, id)).pop(), "'/tmp/greenlight-501/0000-it'\\''s.txt' ", "a quote in the path can't end the quoting");
  assert.equal((await toastText(page)).b, "File sent to this Mac", "a file that isn't a paste isn't called a screenshot");
  await page.evaluate(([id, at]) => term.filed(id, at), [id, at]);
  assert.equal((await toastText(page)).b, "File sent to this Mac", "a paste's tag counts once");
  await page.evaluate(([id]) => term.filed(id, "/tmp/greenlight-501/0000-pasted-zz.txt"), [id]);
  assert.equal((await toastText(page)).b, "File sent to this Mac", "a dropped file named like one isn't a screenshot");
  await page.evaluate(([id]) => term.filed(id, ""), [id]);
  assert.equal((await toastText(page)).b, "Couldn't copy the file over"); assert.equal((await ins(page, id)).length, 4, "and nothing typed");
});

test("⌘V: a named image keeps its name, a nameless one its type's extension, at most 5 go, over 10 MB is refused (issue 128)", async page => {
  await paste(page, {files: [["diagram.png", "image/png"], ["", "image/jpeg"], ["image.svg", "image/svg+xml"], ["", "image/svg+xml"], ["s0.png", "image/png"], ["s1.png", "image/png"]]});
  await filePosts(page, 5); await page.waitForTimeout(150);
  const names = (await posts(page, "file")).map(f => f.name.replace(/^pasted-\w+/, "pasted-T"));
  assert.deepEqual(names, ["diagram.png", "pasted-T.jpg", "pasted-T.svg", "pasted-T.svg", "s0.png"], "five at most, in order");
  await paste(page, {files: [["image.png", "image/png", 10e6 + 1]]});
  assert.match((await toastText(page)).b, /over 10 MB/); await page.waitForTimeout(150);
  assert.equal((await posts(page, "file")).length, 5, "the big one never went");
});

test("⌘V of text: one block, newlines as ↵, a stray end-of-paste stripped; text beats an image; outside a pane it's the field's (issue 128)", async page => {
  const id = await page.evaluate(() => cur.focus.id);
  assert.equal(await paste(page, {text: "echo a\nrm -rf x\x1b[201~; echo b"}), true);
  assert.deepEqual(await ins(page, id), ["echo a\rrm -rf x; echo b"], "the \\x1b[201~ can't close the paste early");
  await paste(page, {text: "plain", files: [["image.png", "image/png"]]}); await page.waitForTimeout(150);
  assert.equal((await ins(page, id)).pop(), "plain"); assert.deepEqual(await posts(page, "file"), [], "rich text's picture isn't sent");
  await page.evaluate(() => palette(true));
  assert.equal(await paste(page, {text: "jump to", sel: "#pq"}), false, "the palette's field pastes for itself");
  await paste(page, {files: [["image.png", "image/png"]], sel: "#pq"}); await page.waitForTimeout(150);
  assert.deepEqual(await posts(page, "file"), [], "a screenshot pasted outside a terminal isn't sent anywhere");
  assert.equal((await ins(page, id)).length, 2);
});

test("drop: files over a pane show where they'll land; dropped, each goes for the pane under the pointer (issue 128)", async page => {
  const first = await page.evaluate(() => cur.focus.id); await page.evaluate(() => split(cur.focus));
  const second = await page.evaluate(() => cur.focus.id); assert.notEqual(first, second);
  assert.equal(await drag(page, "dragover", {text: "just text", pane: first}), false, "a text drag is the terminal's own");
  assert.equal(await page.evaluate(() => $("stage").classList.contains("drop")), false);
  assert.equal(await drag(page, "dragover", {files: ["a.log", "b.txt"], pane: first}), true);
  const shown = await page.evaluate(() => ({on: $("stage").classList.contains("drop"), text: $("drop").textContent, n: $("drop").querySelector(".n")?.textContent}));
  assert.ok(shown.on); assert.match(shown.text, /Drop to send to devbox/); assert.match(shown.text, /\/tmp\/greenlight\//); assert.equal(shown.n, "2 files");
  await page.waitForFunction(() => !$("stage").classList.contains("drop"));  // dragging away clears it
  assert.equal(await drag(page, "drop", {files: ["a.log", "b.txt"], pane: first}), true);
  const fs = await filePosts(page, 2);
  assert.deepEqual(fs.map(f => [f.id, f.name, Buffer.from(f.data, "base64").toString()]), [[first, "a.log", "x"], [first, "b.txt", "x"]], "the pane it fell on, not the focused one");
  await page.evaluate(([id]) => term.filed(id, "/tmp/greenlight-1000/9f-a.log"), [first]);
  assert.equal((await ins(page, first)).pop(), "'/tmp/greenlight-1000/9f-a.log' ");
  assert.equal((await toastText(page)).b, "File sent to devbox");
  await drag(page, "drop", {files: Array.from({length: 12}, (_, i) => `f${i}`), sel: "#drop"});
  await filePosts(page, 12); await page.waitForTimeout(150);
  const more = (await posts(page, "file")).slice(2);
  assert.equal(more.length, 10, "ten at most"); assert.ok(more.every(f => f.id === second), "off any pane: the focused one");
}, "?host=devbox");

test("⌘⇧P: the repo's .greenlight.json commands; a shell one runs in a new tab, an agent one is pasted into its agent, not sent (issue 128)", async page => {
  const cfg = {commands: [{name: "Run tests", run: "make test"}, {name: "Ask for review", run: "review this\nplease\x1b[201~\x07", agent: true},
    {name: 5, run: "x"}, {name: "", run: "y"}, {name: "Empty", run: ""}, null, {name: "No run"}]};
  await page.evaluate(t => term.workspaces(t), {trees: TREES.trees.map(w => w.name === "widgets" ? {...w, cfg} : w)});
  await page.evaluate(() => openWs("/w/fix-login"));
  await page.keyboard.press("Meta+Shift+p");
  assert.equal(await page.evaluate(() => !$("pal").hidden && pal.act), true, "it opens on the actions");
  const cmds = await page.evaluate(() => pal.items.filter(x => x.where === "Commands").map(x => [x.main, x.sub, x.side]));
  assert.deepEqual(cmds, [["Run tests", "make test", "Run in new tab"], ["Ask for review", "review this\nplease\x1b[201~\x07", "Paste into Claude"]], "only well-formed entries");
  assert.match(await page.textContent("#pres .gh.g-act:has-text('Commands')"), /from widgets\/\.greenlight\.json/, "the group says where they came from");
  const before = (await posts(page, "open")).length;
  await page.fill("#pq", "run tests"); await page.keyboard.press("Enter");
  const o = (await posts(page, "open")).slice(before);
  assert.equal(o.length, 1); assert.equal(o[0].ws, "/w/fix-login", "a new tab in the workspace you're in");
  assert.deepEqual(await ins(page, o[0].id), ["make test\r"]);
  await page.keyboard.press("Meta+Shift+p"); await page.fill("#pq", "ask for review"); await page.keyboard.press("Enter");
  assert.deepEqual((await posts(page, "copy")).pop(), {cmd: "copy", text: "review this\nplease\x1b[201~\x07"}, "no agent open: the clipboard");
  assert.equal((await toastText(page)).b, "No Claude open in fix-login: copied it instead");
  const agent = await page.evaluate(() => cur.focus.id);
  await page.evaluate(([id, d]) => term.feed(id, d), [agent, Buffer.from("\x1b]0;✳ Claude Code\x07").toString("base64")]);
  await page.evaluate(() => newTab()); const other = await page.evaluate(() => cur.focus.id);
  await page.keyboard.press("Meta+Shift+p"); await page.fill("#pq", "ask for review"); await page.keyboard.press("Enter");
  assert.deepEqual((await ins(page, agent)).pop(), "review this\rplease", "into Claude's pane, one block, no ↵ at the end, no escapes");
  assert.equal(await page.evaluate(() => cur.focus.id), agent, "and you're taken to it"); assert.ok(!(await ins(page, other)).length);
  await page.evaluate(() => openWs("/w/sidebar")); await page.keyboard.press("Meta+Shift+p");
  assert.deepEqual(await page.evaluate(() => pal.items.filter(x => x.where === "Commands")), [], "another repo's workspace: none of these");
});

test("Open in editor: the palette, the workspace card and Diff each ask the app to open the workspace, named after its editor (issue 128)", async page => {
  await page.keyboard.press("Meta+Shift+p");
  assert.deepEqual(await page.evaluate(() => pal.items.filter(x => /^Open in/.test(x.main))), [], "a plain shell's tab has no workspace to open");
  await page.keyboard.press("Escape");
  await page.evaluate(() => openWs("/w/fix-login")); await page.keyboard.press("Meta+Shift+p");
  await page.fill("#pq", "open in"); assert.equal(await page.evaluate(() => pal.items[0]?.main), "Open in Zed");
  await page.keyboard.press("Enter");
  assert.deepEqual((await posts(page, "edit")).pop(), {cmd: "edit", ws: "/w/fix-login"});
  await page.locator(".ws", {hasText: "sidebar"}).first().click({button: "right"}); await page.waitForSelector("#card:not([hidden])");
  await page.click("#card button:has-text('Zed')");
  assert.deepEqual((await posts(page, "edit")).pop(), {cmd: "edit", ws: "/w/sidebar"});
  await page.evaluate(() => { diffs.add("/w/review"); diffShow({op: "diff", path: "/w/review", mode: "branch", base: "origin/main", files: [["a.py", "1", "0"]], status: ["M"], patch: "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n+x = 1\n"}) });
  await page.click("#dlg button:has-text('Open in Zed')");
  assert.deepEqual((await posts(page, "edit")).pop(), {cmd: "edit", ws: "/w/review"});
  assert.equal((await posts(page, "edit")).length, 3);
}, "?editor=Zed");

(async () => {
  const browser = await launchBrowser();
  let failed = 0;
  for (const t of tests.filter(t => t.name.includes(process.env.ONLY || ""))) {
    const page = await boot(browser, null, t.query);
    try { await t.fn(page); assert.deepEqual(page._errs, []); console.log("ok  ", t.name) }
    catch (e) { failed++; console.log("FAIL", t.name, "\n     ", e.message, (e.stack.match(/test_terminal.cjs:\d+/) || [""])[0]) }
    await page.close();
  }
  await browser.close(); fs.rmSync(dir, {recursive: true});
  process.exit(failed ? 1 : 0);
})();
