// Headless checks for dashboard.html: New task (the empty page's sheet, files, start), the console opening only when asked, the hover card.
//
//   npm i playwright-core          (anywhere; point NODE_PATH at its node_modules)
//   NODE_PATH=…/node_modules node tests/test_dashboard.cjs
//
// The browser is $CHROME, else the newest Playwright headless shell under ~/.cache/ms-playwright.
const fs = require("fs"), path = require("path"), os = require("os"), assert = require("assert");
const { launchBrowser } = require("./js_harness.cjs");

const PAGE = "file://" + path.join(__dirname, "../app/", "dashboard.html");
// Playwright WebKit doesn't intercept file:// subresources via page.route(), so stage terminal.html + ghostty-web.js side by side.
const TERM_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "dash-term-"));
fs.copyFileSync(path.join(__dirname, "../app/", "terminal.html"), path.join(TERM_DIR, "terminal.html"));
fs.copyFileSync(path.join(__dirname, "../app/", "vendor", "ghostty-web.js"), path.join(TERM_DIR, "ghostty-web.js"));
const TERM_PAGE = "file://" + path.join(TERM_DIR, "terminal.html");
const puck = (name, goal, state) => ({name, path: "/w/" + name, project: "acme/widgets", goal, state: state === "waiting" ? "needs" : "working", why: "",
  progress: 0, steps: [], step_index: 0, step: "freeform", owner: "parked", hidden: false, action: "", changed: null,
  agent: {state, type: "claude", last: state === "waiting" ? "Which one?" : "", since: 1700000000000, task: "", tool: "", input: ""},
  mr: null, branch: "b-" + name, puck: null, sig: name + state, built: true});
const SNAP = {ts: 1, scanned: 1, pucks: [puck("plan-a", "Alpha", "waiting"), puck("plan-b", "Beta", "working")]};
const REPOS = {op: "repos", arg: "-", repos: [{name: "widgets", host: "gitlab", path: "acme/widgets"}, {name: "greenlight", host: "github", path: "octo/greenlight"}]};

const tests = [];
const test = (name, fn) => tests.push({name, fn});

async function boot(browser, store = {}){
  const page = await browser.newPage({viewport: {width: 1300, height: 900}});
  await page.addInitScript(store => {
    for (const [k, v] of Object.entries(store)) localStorage.setItem(k, v);
    window.__posts = [];
    window.webkit = {messageHandlers: {mc: {postMessage: m => window.__posts.push(JSON.parse(JSON.stringify(m)))}}};
  }, store);
  const errs = []; page.on("pageerror", e => errs.push(e.message)); page._errs = errs;
  await page.goto(PAGE);
  await page.evaluate(s => window.mc.update(s), SNAP);
  return page;
}
const posts = (page, cmd) => page.evaluate(c => window.__posts.filter(m => m.cmd === c), cmd);
async function newTask(page){  // the sheet's one way in: the empty page's button (the bar's is gone)
  await page.evaluate(s => window.mc.update({...s, pucks: []}), SNAP);
  await page.click("#main .empty button:has-text('New task')");
  await page.evaluate(r => window.mc.tasked(r), REPOS);
}
const sheet = page => page.$(".sheet[aria-label='New task']");

test("agent avatars use the OpenAI and Claude logos", async page => {
  await page.evaluate(s => {
    s.pucks[0].state = "working";
    s.pucks[0].agent.state = "working";
    s.pucks[1].agent.type = "codex";
    mc.update(s);
  }, JSON.parse(JSON.stringify(SNAP)));
  const logos = await page.$$eval(".acard .av svg", es => es.map(e => ({
    viewBox: e.getAttribute("viewBox"), fill: e.getAttribute("fill"),
    path: e.querySelector("path").getAttribute("d"), circles: e.querySelectorAll("circle").length
  })));
  assert.equal(logos.length, 2);
  assert.ok(logos.every(e => e.viewBox === "0 0 24 24" && e.fill === "currentColor" && !e.circles));
  assert.notEqual(logos[0].path, logos[1].path, "providers must have different logos");
  assert.equal(await page.locator(".acard .av").allTextContents().then(xs => xs.join("")), "");
});

test("the Plan view is gone: Build is the only view; the bar has no New task, the empty page's opens the sheet", async page => {
  assert.equal(await page.$(".mode"), null);
  assert.equal(await page.evaluate(() => document.getElementById("main").innerText.includes("Plan")), false);
  assert.equal(await page.$("#bar button[aria-label='New task'], #bar button:has-text('New task')"), null, "the bar still has New task");
  await newTask(page);
  assert.ok(await sheet(page), "the sheet opened");
  assert.deepEqual((await posts(page, "task")).map(m => m.op), ["repos"]);
  assert.equal(await page.evaluate(() => document.activeElement.id), "ps-repo", "no repository picked: the caret starts on the repository");
});

test("Enter starts the task with its project, the whole text and the files; Shift+Enter is a new line", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "Retry stuck txs");
  await page.press("#ps-task", "Shift+Enter");
  await page.type("#ps-task", "after a churn");
  assert.equal((await posts(page, "task")).filter(m => m.op === "new").length, 0, "Shift+Enter sent it");
  assert.equal(await page.isDisabled("#ps-go"), false, "Start task stayed disabled after typing");
  await page.setInputFiles(".tdrop input[type=file]", {name: "panic.log", mimeType: "text/plain", buffer: Buffer.from("boom")});
  await page.waitForSelector(".tfile");
  await page.waitForFunction(() => !document.getElementById("ps-go").disabled);
  assert.match(await page.textContent("#ps-foot"), /Creates retry-stuck-txs in widgets/);
  await page.press("#ps-task", "Enter");
  const n = (await posts(page, "task")).filter(m => m.op === "new");
  assert.equal(n.length, 1);
  assert.equal(n[0].arg, "widgets");
  const body = JSON.parse(n[0].text);
  assert.equal(body.text, "Retry stuck txs\nafter a churn");
  assert.deepEqual(body.files.map(f => [f.name, f.data]), [["panic.log", "data:text/plain;base64," + Buffer.from("boom").toString("base64")]]);
  assert.equal(await page.evaluate(() => localStorage.getItem("taskRepo")), "widgets");
});

test("no project or no text: nothing starts", async page => {
  await newTask(page);
  await page.fill("#ps-task", "Do a thing");
  await page.press("#ps-task", "Enter");
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "  \n ");
  await page.press("#ps-task", "Enter");
  assert.equal((await posts(page, "task")).filter(m => m.op === "new").length, 0);
});

test("Escape and ✕ close the sheet and keep the draft; a file chip can be removed", async page => {
  await newTask(page);
  await page.fill("#ps-task", "Half a thought");
  await page.keyboard.press("Escape");
  assert.equal(await sheet(page), null);
  await newTask(page);
  assert.equal(await page.inputValue("#ps-task"), "Half a thought");
  await page.setInputFiles(".tdrop input[type=file]", [{name: "a.png", mimeType: "image/png", buffer: Buffer.from("a")}, {name: "b.txt", mimeType: "text/plain", buffer: Buffer.from("b")}]);
  await page.waitForFunction(() => document.querySelectorAll(".tfile").length === 2);
  await page.click("button[aria-label='Remove a.png']");
  assert.deepEqual(await page.$$eval(".tfile .mono", e => e.map(x => x.textContent)), ["b.txt"]);
  await page.click(".shd button[aria-label=Close]");
  assert.equal(await sheet(page), null);
});

test("dragging files over lights the drop zone; dropping attaches them", async page => {
  await newTask(page);
  await page.evaluate(() => {
    const dt = new DataTransfer(); dt.items.add(new File(["x"], "one.log")); dt.items.add(new File(["yy"], "two.png", {type: "image/png"}));
    window.__dt = dt; document.querySelector(".sheet").dispatchEvent(new DragEvent("dragover", {dataTransfer: dt, bubbles: true, cancelable: true}));
  });
  assert.ok(await page.$(".sheetbg.drag"));
  assert.equal(await page.textContent(".tdrop .over"), "Drop to attach 2 files");
  await page.evaluate(() => document.querySelector(".sheet").dispatchEvent(new DragEvent("drop", {dataTransfer: window.__dt, bubbles: true, cancelable: true})));
  await page.waitForFunction(() => document.querySelectorAll(".tfile").length === 2);
  assert.equal(await page.$(".sheetbg.drag"), null);
});

test("started: the sheet closes, the draft clears, a toast opens it in the Basement; an error keeps the sheet", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "Fix the flake");
  await page.press("#ps-task", "Enter");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", error: "no space"}));
  assert.ok(await sheet(page)); assert.match(await page.textContent(".sheet .perr"), /no space/);
  assert.equal(await page.isDisabled("#ps-go"), true, "Start stayed live under the error");
  await page.click("#ps-retry");
  assert.equal((await posts(page, "task")).filter(m => m.op === "new").length, 2, "Retry didn't send the same task again");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", error: "workspace fix-the-flake already exists"}));
  assert.equal(await page.evaluate(() => document.activeElement.id), "ps-task", "a taken name puts the caret back in the title");
  await page.click(".sheet .perr button:has-text('Edit first line')");
  assert.deepEqual(await page.evaluate(() => [document.activeElement.id, document.activeElement.selectionEnd]), ["ps-task", "Fix the flake".length], "Edit first line didn't select the first line");
  const setTask = v => page.evaluate(v => { const t = document.getElementById("ps-task"); t.value = v; t.dispatchEvent(new Event("input")) }, v);
  await setTask("Fix the flake\n\nmore detail");
  assert.ok(await page.$(".sheet .perr"), "editing the details cleared the taken-name error");
  assert.equal(await page.isDisabled("#ps-go"), true, "Start came back with the same taken name");
  await setTask("Fix the flake");
  await page.press("#ps-task", "End");
  await page.type("#ps-task", " now");
  assert.equal(await page.$(".sheet .perr"), null, "editing the title kept the stale error");
  await page.press("#ps-task", "Enter");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", ok: true, name: "plan-b", repo: "widgets"}));
  assert.equal(await sheet(page), null);
  assert.equal(await page.evaluate(() => localStorage.getItem("taskDraft")), "");
  assert.match(await page.textContent(".toast"), /Task started\s*plan-b · widgets/);
  assert.equal((await posts(page, "basement")).length, 0, "it went to the Basement by itself");
  await page.evaluate(s => window.mc.update(s), SNAP);  // the next scan brings the new workspace in: before it, the toast's Open in Basement has no path to send (pre-existing gap)
  await page.click(".toast button:has-text('Open in Basement')");
  assert.deepEqual((await posts(page, "basement")).map(m => m.path), ["/w/plan-b"]);
  assert.equal(await page.$(".toast"), null);
});

test("an ambiguous goal pauses: chips answer preflight's questions, then the same task goes again with them; Start anyway forces it; an edit re-asks", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "make it better");
  await page.press("#ps-task", "Enter");
  const Q = {op: "new", arg: "widgets", paused: true, p_ambiguous: 0.9, questions: [
    {id: "done", q: "What does done look like?", options: ["A draft PR with tests", "A working prototype I can try"]},
    {id: "scope", q: "How far should it go?", options: ["The smallest change that works", "Investigate and report back first"]}]};
  await page.evaluate(q => window.mc.tasked(q), Q);
  assert.ok(await sheet(page), "the sheet closed on a pause");
  assert.match(await page.textContent("#ps-ask"), /Two questions before it starts.*90% sure/);
  assert.equal(await page.isDisabled("#ps-go"), true, "Start was live with nothing answered");
  assert.match(await page.textContent("#ps-go"), /Start with answers/);
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "A draft PR with tests", "the first chip doesn't have the focus");
  await page.click("#ps-ask button:has-text('A working prototype I can try')");
  assert.equal(await page.getAttribute("#ps-ask button:has-text('A working prototype I can try')", "aria-pressed"), "true");
  assert.equal(await page.isDisabled("#ps-go"), true, "one of two answered is enough");
  await page.fill("#ps-ask input[aria-label^='How far']", "Only the retry path");
  await page.click("#ps-go");
  const n = (await posts(page, "task")).filter(m => m.op === "new");
  assert.equal(n.length, 2);
  assert.deepEqual(JSON.parse(n[1].text).answers, {done: "A working prototype I can try", scope: "Only the retry path"});
  await page.evaluate(q => window.mc.tasked(q), Q);  // asked again (say the other window sent it): Start anyway
  await page.click("#ps-anyway");
  const f = (await posts(page, "task")).filter(m => m.op === "new");
  assert.equal(JSON.parse(f[2].text).force, true); assert.equal(JSON.parse(f[2].text).answers, undefined);
  await page.evaluate(q => window.mc.tasked(q), Q);
  await page.type("#ps-task", " now");  // a changed goal: its questions go, and it's judged afresh
  assert.equal(await page.$("#ps-ask"), null); assert.equal(await page.$("#ps-anyway"), null);
  assert.match(await page.textContent("#ps-go"), /^Start task/);
  await page.press("#ps-task", "Enter");
  const g = (await posts(page, "task")).filter(m => m.op === "new");
  assert.equal(g.length, 4); assert.equal(JSON.parse(g[3].text).answers, undefined);
  await page.keyboard.press("Escape");
  await page.evaluate(q => window.mc.tasked(q), Q);  // the sheet closed before the pause came back: the toast says so
  assert.match(await page.textContent(".toast"), /question about the goal first/);
  assert.deepEqual(page._errs, []);
});

test("a preflight reply for an edited task cannot clear the new goal", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "make it better");
  await page.press("#ps-task", "Enter");
  await page.fill("#ps-task", "build something different");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", paused: true, questions: [
    {id: "done", q: "What does done look like?", options: ["A draft PR with tests"]}]}));
  assert.equal(await page.$("#ps-ask"), null);
  assert.equal(await page.isDisabled("#ps-go"), false);
  await page.click("#ps-go");
  const started = (await posts(page, "task")).filter(m => m.op === "new");
  assert.equal(started.length, 2);
  assert.equal(JSON.parse(started[1].text).text, "build something different");
  assert.equal(JSON.parse(started[1].text).answers, undefined);
});

test("a completed earlier task preserves a newly edited draft and keeps its error off the new goal", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "Task A");
  await page.press("#ps-task", "Enter");
  await page.fill("#ps-task", "Task B");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", ok: true, name: "task-a", repo: "widgets"}));
  assert.ok(await sheet(page));
  assert.equal(await page.inputValue("#ps-task"), "Task B");
  assert.equal(await page.evaluate(() => localStorage.getItem("taskDraft")), "Task B");
  assert.match(await page.textContent(".toast"), /Task started.*task-a/);
  await page.press("#ps-task", "Enter");
  await page.fill("#ps-task", "Task C");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", error: "workspace task-b already exists"}));
  assert.ok(await sheet(page));
  assert.equal(await page.inputValue("#ps-task"), "Task C");
  assert.equal(await page.$("#ps-err"), null);
  assert.equal(await page.isDisabled("#ps-go"), false);
});

test("a late reply preserves edited attachments, and Reopen keeps the current sheet", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "Task A");
  await page.press("#ps-task", "Enter");
  await page.setInputFiles(".tdrop input[type=file]", {name: "later.txt", mimeType: "text/plain", buffer: Buffer.from("later")});
  await page.waitForSelector(".tfile");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", ok: true, name: "task-a", repo: "widgets"}));
  assert.ok(await sheet(page));
  assert.deepEqual(await page.$$eval(".tfile .mono", e => e.map(x => x.textContent)), ["later.txt"]);
  await page.waitForFunction(() => !document.getElementById("ps-go").disabled);
  await page.press("#ps-task", "Enter");
  await page.fill("#ps-task", "Task B");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", error: "old task failed"}));
  await page.locator(".toast button:has-text('Reopen')").focus();
  await page.keyboard.press("Enter");
  assert.ok(await sheet(page));
  assert.equal(await page.inputValue("#ps-task"), "Task B");
  assert.deepEqual(await page.$$eval(".tfile .mono", e => e.map(x => x.textContent)), ["later.txt"]);
});

test("a selected file counts as an edit before its reader finishes", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "Task A");
  await page.evaluate(() => { window.__reads = []; window.FileReader = class {
    readAsDataURL(){ this.result = "data:text/plain;base64,bGF0ZXI="; window.__reads.push(this) }
  } });
  await page.setInputFiles(".tdrop input[type=file]", {name: "before.txt", mimeType: "text/plain", buffer: Buffer.from("before")});
  assert.equal(await page.isDisabled("#ps-go"), true, "a pending read was omitted from a submitted task");
  await page.evaluate(() => window.__reads[0].onload());
  assert.equal(await page.isDisabled("#ps-go"), false);
  await page.press("#ps-task", "Enter");
  await page.setInputFiles(".tdrop input[type=file]", {name: "later.txt", mimeType: "text/plain", buffer: Buffer.from("later")});
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", ok: true, name: "task-a", repo: "widgets"}));
  assert.ok(await sheet(page), "success closed a sheet with a selected file still loading");
  await page.evaluate(() => window.__reads[1].onload());
  assert.deepEqual(await page.$$eval(".tfile .mono", e => e.map(x => x.textContent)), ["before.txt", "later.txt"]);
});

test("a failed file read stays visible until removed and cannot trigger task Retry", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "Task A");
  await page.evaluate(() => { window.__reads = []; window.FileReader = class {
    readAsDataURL(){ this.result = "data:text/plain;base64,b2s="; window.__reads.push(this) }
  } });
  await page.setInputFiles(".tdrop input[type=file]", {name: "broken.txt", mimeType: "text/plain", buffer: Buffer.from("bad")});
  await page.evaluate(() => window.__reads[0].onerror());
  assert.match(await page.textContent(".perr"), /Couldn't read broken.txt.*Remove it and choose it again/);
  assert.equal(await page.$("#ps-retry"), null);
  assert.equal(await page.isDisabled("#ps-go"), true);
  await page.setInputFiles(".tdrop input[type=file]", {name: "replacement.txt", mimeType: "text/plain", buffer: Buffer.from("ok")});
  assert.match(await page.textContent(".perr"), /broken.txt/);
  assert.equal(await page.isDisabled("#ps-go"), true);
  await page.evaluate(() => window.__reads[1].onload());
  assert.equal(await page.isDisabled("#ps-go"), true, "one replacement silently cleared the failed entry");
  await page.click("button[aria-label='Remove broken.txt']");
  assert.equal(await page.isDisabled("#ps-go"), false);
  assert.deepEqual(await page.$$eval(".tfile .mono", e => e.map(x => x.textContent)), ["replacement.txt"]);
});

test("multiple failed reads remain visible, and a removed reader cannot revive an error", async page => {
  await newTask(page);
  await page.selectOption("#ps-repo", "widgets");
  await page.fill("#ps-task", "Task A");
  await page.evaluate(() => { window.__reads = []; window.FileReader = class {
    readAsDataURL(){ this.result = "data:text/plain;base64,b2s="; window.__reads.push(this) }
  } });
  await page.setInputFiles(".tdrop input[type=file]", [
    {name: "bad-a.txt", mimeType: "text/plain", buffer: Buffer.from("a")},
    {name: "bad-b.txt", mimeType: "text/plain", buffer: Buffer.from("b")}]);
  await page.evaluate(() => window.__reads[0].onerror());
  await page.setInputFiles(".tdrop input[type=file]", {name: "replacement.txt", mimeType: "text/plain", buffer: Buffer.from("ok")});
  await page.evaluate(() => window.__reads[2].onload());
  assert.equal(await page.isDisabled("#ps-go"), true);
  assert.match(await page.textContent(".perr"), /bad-a.txt/);
  await page.click("button[aria-label='Remove bad-a.txt']");
  assert.equal(await page.isDisabled("#ps-go"), true, "the second unread file was omitted");
  await page.click("button[aria-label='Remove bad-b.txt']");
  await page.evaluate(() => window.__reads[1].onerror());
  assert.equal(await page.$(".perr"), null);
  assert.equal(await page.isDisabled("#ps-go"), false);
  assert.deepEqual(await page.$$eval(".tfile .mono", e => e.map(x => x.textContent)), ["replacement.txt"]);
});

test("with workspaces on the page, a failed start's Reopen still opens the sheet (the bar has no New task)", async page => {
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", error: "no space"}));
  await page.click(".toast button:has-text('Reopen')");
  assert.ok(await sheet(page), "Reopen didn't open the sheet");
  assert.deepEqual((await posts(page, "task")).map(m => m.op), ["repos"]);
});

test("Basement on a workspace page carries its last agent (issue 157): the tooltip says attach or continue", async page => {
  const pk = (name, resume) => ({...SNAP.pucks.find(p => p.name === name), resume});
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [pk("plan-a", {harness: "codex"}), pk("plan-b", {harness: "claude", session: "mc-1a2b3c4d"})]});
  const tip = async n => { await page.evaluate(n => openPage(n), n); return page.getAttribute("#bar button:has-text('Basement')", "title") };
  assert.equal(await tip("plan-a"), "Continue its last Codex session in the Basement (B1)");
  await page.click("#bar button:has-text('Basement')");
  assert.equal(await tip("plan-b"), "Attach to its Claude Code session in the Basement (B1)");
  await page.click("#bar button:has-text('Basement')");
  assert.deepEqual((await posts(page, "basement")).map(m => [m.path, m.resume]), [["/w/plan-a", {harness: "codex"}], ["/w/plan-b", {harness: "claude", session: "mc-1a2b3c4d"}]]);
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [pk("plan-a", {harness: "toString"}), pk("plan-b", undefined)]});
  assert.equal(await tip("plan-a"), "Open this workspace in the Basement (B1)", "only harnesses it knows");
  assert.equal(await tip("plan-b"), "Open this workspace in the Basement (B1)");
  await page.click("#bar button:has-text('Basement')");
  assert.deepEqual((await posts(page, "basement")).pop().resume, {}, "no agent yet: nothing to resume");
});

test("toast: the dismiss button closes it", async page => {
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", ok: true, name: "plan-b", repo: "widgets"}));
  await page.click(".toast button[aria-label='Dismiss']");
  assert.equal(await page.$(".toast"), null);
});

test("Basement: a page's button sends B1 that worktree; ⌘-click on a hex docks it into chat context; no console is left", async page => {
  assert.equal(await page.$("#con"), null);
  await page.evaluate(() => openPage("plan-a"));
  await page.click("#bar button:has-text('Basement')");
  await page.evaluate(() => closePage());
  await page.click("#main .cell[data-name='plan-b']", {modifiers: ["Meta"]});
  assert.deepEqual((await posts(page, "basement")).map(m => m.path), ["/w/plan-a"]);
  const ctx = (await posts(page, "context")).pop();
  assert.ok(ctx && ctx.items.some(i => i.key === "plan-b"), "⌘-click on a hex docks it in chat context");
});

test("hover card button: greyed \"Please wait…\" until the step is written, then says it and runs it; none while the agent works or waits", async page => {
  await page.evaluate(() => document.body.append(hoverCard(puckOf("plan-b"))));
  assert.equal(await page.$("#hcard .btn.nx"), null, "button shown while the agent is working");
  await page.evaluate(() => { hideCard(); document.body.append(hoverCard(puckOf("plan-a"))) });
  assert.equal(await page.$("#hcard .btn.nx"), null, "button shown while the agent waits on an answer");
  await page.evaluate(() => { puckOf("plan-a").agent.state = "idle"; hideCard(); document.body.append(hoverCard(puckOf("plan-a"))) });
  assert.equal(await page.textContent("#hcard .btn.nx"), "Please wait…");
  assert.equal(await page.$eval("#hcard .btn.nx", b => b.disabled), true, "a click before the step is written does nothing");
  assert.equal((await posts(page, "suggest")).length, 1, "the card asks once (the app joins an in-flight background one)");
  await page.evaluate(() => window.mc.suggest({name: "plan-a", prompt: "Fix the gofmt error in x.go and push.", label: "Fix lint job", sig: "plan-awaiting"}));
  assert.equal(await page.textContent("#hcard .btn.nx"), "Fix lint job", "the open card relabels when it lands");
  assert.equal(await page.$eval("#hcard .btn.nx", b => b.disabled), false);
  assert.equal((await posts(page, "run")).length, 0, "relabeling ran it");
  await page.click("#hcard .btn.nx");
  assert.deepEqual((await posts(page, "run")).map(m => m.prompt), ["Fix the gofmt error in x.go and push."]);
});

test("hover card footer: one glyph for the agent (a live one outranks a crash), and the MR link only when there is one", async page => {
  const foot = (agent, state, mr) => page.evaluate(([agent, state, mr]) => { const p = puckOf("plan-a"); p.agent = agent; p.state = state; p.mr = mr;
    hideCard(); document.body.append(hoverCard(p));
    const g = document.querySelector("#hcard .hf .gl"), l = document.querySelector("#hcard .hf .lk");
    return [g && g.getAttribute("class"), l && l.getAttribute("href"), document.querySelector("#hcard .hfx").textContent] }, [agent, state, mr]);
  const mr = {iid: 12, ref: "!12", state: "opened", title: "x", web_url: "https://gl/12"};
  assert.deepEqual(await foot({type: "claude", state: "idle"}, "needs", mr), ["dsi gl idle", "https://gl/12", "Claude is idle"]);
  assert.deepEqual(await foot({type: "claude", state: "working"}, "crashed", mr), ["dsi gl working", "https://gl/12", "Claude is working"]);
  assert.deepEqual(await foot({type: "codex", state: "waiting"}, "needs", null), ["dsi gl waiting", null, "Codex is waiting"]);
  assert.deepEqual(await foot({type: "claude", state: "idle"}, "crashed", null), ["dsi gl crashed", null, "Claude crashed"]);
  assert.deepEqual(await foot({}, "crashed", null), ["dsi gl crashed", null, "Agent crashed"]);  // no type: don't guess Claude
  assert.deepEqual(await foot({}, "needs", null), [null, null, "No agent"]);
});

test("verify: the hover card's Verify move runs its prompt; the page's facts and steps show the verdict and its evidence", async page => {
  await page.evaluate(() => { const p = puckOf("plan-a"); p.agent.state = "idle"; p.steps = ["mr_open", "ci_green", "verified", "ready", "merge"]; p.step_index = 2; p.step = "verified";
    p.mr = {iid: 12, ref: "!12", state: "opened", title: "x", target_branch: "main", web_url: "https://gl/12",
            verify: {verdict: "fail", phase: "fix", round: 1, max_rounds: 3, summary: "cancel kept state", defects: ["dirty dialog"], unproven: ["Safari"], evidence: "/srv/gl/verify/x-12.html"}};
    p.moves = [{kind: "verify", prompt: "verify prompt", own: false, repo: "widgets"}];
    hideCard(); document.body.append(hoverCard(p)) });
  assert.match(await page.textContent("#hcard .mv.bl .go"), /^Verify !12/);
  assert.ok(await page.$("#hcard .mv .go svg"), "the move has its icon");
  await page.click("#hcard .mv .go");
  assert.deepEqual((await posts(page, "run")).map(m => [m.name, m.prompt]), [["plan-a", "verify prompt"]]);
  await page.evaluate(() => openPage("plan-a"));
  assert.match(await page.textContent(".facts"), /Verification\s*failed · round 1\/3 · 1 defect · 1 unproven — cancel kept state · evidence \/srv\/gl\/verify\/x-12\.html/);
  assert.equal(await page.$(".facts a:has-text('Evidence')"), null, "a file on the host isn't a link the app would cancel");
  await page.evaluate(() => { const p = puckOf("plan-a"); p.mr.verify = {verdict: "pass", phase: "pass", round: 0, evidence: "https://ci.example/run/7"}; openPage("plan-a") });
  assert.equal(await page.getAttribute(".facts a:has-text('Evidence')", "href"), "https://ci.example/run/7");
  assert.match(await page.textContent(".facts"), /Verification\s*passed/);
});

test("hover card: the smart button on top replaces the next step; Also runs the rest; Merge asks twice", async page => {
  await page.evaluate(() => { const p = puckOf("plan-a"); p.agent.state = "idle";
    p.mr = {iid: 12, ref: "!12", state: "opened", title: "x", target_branch: "main", web_url: "https://gl/12"};
    p.moves = [{kind: "threads", n: 2, prompt: "fix threads", own: false, repo: "widgets"}, {kind: "review", prompt: "rv", own: true, repo: "widgets"}];
    hideCard(); document.body.append(hoverCard(p)) });
  assert.equal(await page.$("#hcard .btn.nx"), null, "the next step shows beside a move");
  const kids = await page.$$eval("#hcard > *", k => k.map(e => e.className));
  assert.ok(kids.indexOf("mvs") === 2, kids.join(","));
  assert.match(await page.textContent("#hcard .mv.bl .go"), /^Resolve 2 threads/);
  await page.click("#hcard .also button:text-is('Review')");
  assert.deepEqual((await posts(page, "run")).map(m => [m.name, m.prompt]), [["plan-a", "rv"]]);
  await page.evaluate(() => { document.body.append(hoverCard(puckOf("plan-a"))) });
  await page.click("#hcard .mv .ed");
  assert.deepEqual((await posts(page, "editPrompt")).pop(), {cmd: "editPrompt", repo: "widgets", kind: "threads"});
  await page.evaluate(() => { const p = puckOf("plan-a"); p.moves = [{kind: "merge", how: "Squash", target: "main"}]; hideCard(); document.body.append(hoverCard(p)) });
  await page.click("#hcard .mg .go");
  assert.equal((await posts(page, "merge")).length, 0, "one click merged");
  await page.click("#hcard .mg .go");
  assert.deepEqual((await posts(page, "merge")).pop(), {cmd: "merge", path: "/w/plan-a"});
  await page.evaluate(() => window.mc.merged({path: "/w/plan-a", error: "405: Branch cannot be merged"}));
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "Didn't merge !12");
  assert.equal(await page.textContent(".toast.bad .tmsg code"), "405: Branch cannot be merged");
  await page.click(".toast .btn.x");  // a success never covers an error: dismiss it first
  assert.equal(await page.$(".toast"), null);
  await page.evaluate(() => { const p = puckOf("plan-a"); p.moves = [{kind: "review", prompt: "rv", own: true, repo: "widgets"}, {kind: "merge", how: "Squash", target: "main"}]; hideCard(); document.body.append(hoverCard(p)) });
  assert.match(await page.textContent("#hcard .mv .go"), /^Review/); assert.match(await page.textContent("#hcard .mg .go"), /^Merge !12/, "a review doesn't hide Merge");
  await page.evaluate(() => { const p = puckOf("plan-a"); p.moves = [{kind: "ready"}]; hideCard(); document.body.append(hoverCard(p)) });
  await page.click("#hcard .mg .go");  // one click: undrafting is undone by drafting again
  assert.deepEqual((await posts(page, "undraft")).pop(), {cmd: "undraft", path: "/w/plan-a"});
  await page.evaluate(() => window.mc.merged({op: "ready", path: "/w/plan-a", ok: true, ref: "!12"}));
  assert.equal(await page.textContent(".toast:not(.bad) .tmsg b"), "!12 is ready for review");
  await page.evaluate(() => { const p = puckOf("plan-a"); p.mr = {...p.mr, state: "merged"}; p.moves = [{kind: "signoff", step: "deploy", run: "git pull"}]; hideCard(); document.body.append(hoverCard(p)) });
  assert.equal(await page.textContent("#hcard .mg .go b"), "Deploy !12", "a merged MR's step after the merge gets its button");
  await page.click("#hcard .mg .go");
  assert.deepEqual((await posts(page, "signoff")).pop(), {cmd: "signoff", path: "/w/plan-a"});
  await page.evaluate(() => window.mc.merged({op: "signoff", path: "/w/plan-a", ok: true, step: "deploy", ran: "git pull"}));
  assert.equal(await page.textContent(".toast:not(.bad) .tmsg b"), "Deploy signed off");
  assert.equal(await page.textContent(".toast .tmsg span"), "Ran git pull");
});

test("footer: a scan that died says why and how to fix it until a newer scan lands; not while a rescan runs", async page => {
  const now = Math.floor(Date.now() / 1000), snap = {...JSON.parse(JSON.stringify(SNAP)), ts: now, scanned: now - 7200};
  const refreshSel = '#foot button[aria-label="Refresh now"]';
  await page.evaluate(s => window.mc.update(s), snap);
  assert.equal(await page.$("#foot .scanerr"), null, "no failure, no line");
  // fresh failure (< 60s): inline fix + tooltip says "just now" (never "now ago")
  const seFresh = {msg: "GitLab login expired", fix: "run glab auth login --hostname gitlab.com, then ↻", ts: now - 10};
  await page.evaluate(s => window.mc.update(s), {...snap, scan_error: seFresh});
  assert.equal(await page.textContent("#foot .scanerr"), "Scan failed: GitLab login expired · run glab auth login --hostname gitlab.com, then ↻");
  const tipFresh = await page.getAttribute("#foot .scanerr", "title");
  assert.match(tipFresh, /^The last scan stopped just now, so these facts are from the one before\.\nFix: run glab auth login --hostname gitlab\.com, then ↻\.$/);
  // older failure (>= 60s) and empty fix
  await page.evaluate(s => window.mc.update(s), {...snap, scan_error: {msg: "crashed", fix: "", ts: now - 120}});
  assert.equal(await page.textContent("#foot .scanerr"), "Scan failed: crashed");
  assert.equal(await page.getAttribute("#foot .scanerr", "title"), "The last scan stopped 2m ago, so these facts are from the one before.");
  // restore auth failure, click refresh: hidden while rescanning, returns on failure, cleared when snapshot drops it
  await page.evaluate(s => window.mc.update(s), {...snap, scan_error: seFresh});
  await page.click(refreshSel);
  assert.equal(await page.$("#foot .scanerr"), null, "a rescan in flight: the old failure isn't this one's");
  await page.evaluate(() => window.mc.rescanned(1));
  assert.equal(await page.textContent("#foot .scanerr"), "Scan failed: GitLab login expired · run glab auth login --hostname gitlab.com, then ↻", "it failed again: the line is back");
  await page.evaluate(s => window.mc.update(s), {...snap, scanned: now});
  assert.equal(await page.$("#foot .scanerr"), null, "a good scan: the line goes");
  // rescanned(1) without a prior scan_error doesn't flash "exit 1" before the stream delivers scan_error.json
  await page.click(refreshSel);
  await page.evaluate(() => window.mc.rescanned(1));
  assert.equal(await page.$("#foot .scanerr"), null, "exit 1 waits for scan_error.json rather than flashing 'exit 1'");
  // SSH failure (exit 255) shows in footer even over a stale snapshot scan_error, until a newer scan lands
  await page.evaluate(s => window.mc.update(s), {...snap, scanned: now, scan_error: {msg: "GitLab login expired", fix: "run glab auth login, then ↻", ts: now - 300}});
  await page.click(refreshSel);
  await page.evaluate(() => window.mc.rescanned(255));
  assert.equal(await page.textContent("#foot .scanerr"), "Scan failed: host unreachable · check SSH connection, then ↻");
  await page.evaluate(s => window.mc.update(s), {...snap, scanned: now, scan_error: {msg: "GitLab login expired", fix: "run glab auth login, then ↻", ts: now - 300}});
  assert.equal(await page.textContent("#foot .scanerr"), "Scan failed: host unreachable · check SSH connection, then ↻", "stale snapshot error does not override newer SSH failure");
  await page.evaluate(s => window.mc.update(s), {...snap, scanned: now + 60});
  assert.equal(await page.$("#foot .scanerr"), null, "a later good scan clears the SSH rescan error");
  // first-scan failure (scanned: 0): shows Not scanned yet + failure with first-scan tooltip, and clicking refresh still enters Refreshing…
  await page.evaluate(s => window.mc.update(s), {...snap, scanned: 0, pucks: [], scan_error: seFresh});
  assert.equal(await page.textContent("#foot .scan"), "Not scanned yet");
  assert.match(await page.getAttribute("#foot .scanerr", "title"), /^The scan stopped just now, so no workspaces have been scanned yet\./);
  await page.click(refreshSel);
  assert.equal(await page.textContent("#foot .scan"), "Refreshing…");
  assert.equal(await page.$("#foot .scanerr"), null);
  // narrow viewport: footer reaches .tightest (hides .scan and .scanfix) instead of crushing .scanerr first
  await page.setViewportSize({width: 480, height: 640});
  await page.evaluate(s => window.mc.update(s), {...snap, scanned: now - 7200, scan_error: seFresh});
  assert.equal(await page.$eval("#foot", el => el.classList.contains("tightest")), true, "narrow footer activates tightest");
  assert.equal(await page.$eval("#foot .scanfix", el => getComputedStyle(el).display), "none", "inline fix hides at tightest so error reason stays legible");
  await page.setViewportSize({width: 1280, height: 800});
  await page.evaluate(s => window.mc.update(s), {...snap, scanned: now + 120});
  assert.equal(await page.$("#foot .scanerr"), null);
});

test("card actions: the hex leaves on the click, comes back when its state moves or a later scan still has it; a failure restores it", async page => {
  const snap = JSON.parse(JSON.stringify(SNAP)), now = () => Math.floor(Date.now() / 1000);
  Object.assign(snap.pucks[0], {agent: {...snap.pucks[0].agent, state: "idle"}, sig: "a1",
    mr: {iid: 12, ref: "!12", state: "opened", title: "x", target_branch: "main", web_url: "https://gl/12"}, moves: [{kind: "merge", how: "Squash", target: "main"}]});
  Object.assign(snap, {ts: now(), scanned: 100, scan_started: 90});
  await page.evaluate(s => window.mc.update(s), snap);
  const hex = () => page.evaluate(() => !!hexOn("plan-a"));
  const card = () => page.evaluate(() => document.body.append(hoverCard(puckOf("plan-a"))));
  assert.equal(await hex(), true);
  // Merge: gone the moment it's confirmed, before the host answers
  await card(); await page.click("#hcard .mg .go"); await page.click("#hcard .mg .go");
  assert.deepEqual((await posts(page, "merge")).pop(), {cmd: "merge", path: "/w/plan-a"});
  assert.equal(await hex(), false, "the hex lags the merge");
  assert.equal(await page.$("#hcard"), null, "the card goes with its hex");
  assert.equal(await page.evaluate(() => shown().some(p => p.name === "plan-a")), false, "counts and the list skip it too");
  await page.evaluate(() => window.mc.update(ui.raw));  // the 5s stream, before the reply: still away
  assert.equal(await hex(), false);
  // it merged: the outcome toasts, a scan is asked for at once, and a scan that began before the reply doesn't bring it back
  await page.evaluate(() => window.mc.merged({path: "/w/plan-a", ok: true, ref: "!12", target: "main", how: "Squash"}));
  assert.equal(await page.textContent(".toast .tmsg b"), "Merged !12 into main");
  assert.equal((await posts(page, "rescan")).length, 1, "no scan asked for after the merge");
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000), scanned: 101, scan_started: 95}), snap);
  assert.equal(await hex(), false, "a scan already running when it merged put it back unmerged");
  assert.equal((await posts(page, "rescan")).length, 2, "that stale scan should be followed by one that starts after the merge");
  // the merged state lands (its sig moves): back, as it is now
  snap.pucks[0] = {...snap.pucks[0], state: "done", sig: "a2", mr: {...snap.pucks[0].mr, state: "merged"}, moves: []};
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000)}), snap);
  assert.equal(await page.evaluate(() => shown().some(p => p.name === "plan-a" && p.state === "done")), true, "the merged state never brought it back (done: on the shelf)");
  assert.deepEqual(await page.evaluate(() => Object.keys(ui.acting)), []);
  // a move that fails: the hex comes straight back and the toast says why
  snap.pucks[0] = {...snap.pucks[0], state: "needs", sig: "a3", mr: {...snap.pucks[0].mr, state: "opened"}, moves: [{kind: "ci", jobs: ["lint"], prompt: "fix ci", own: true, repo: "widgets"}]};
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000)}), snap);
  await card(); await page.click("#hcard .mv .go");
  assert.deepEqual((await posts(page, "run")).pop(), {cmd: "run", name: "plan-a", prompt: "fix ci", kind: "ci"});
  assert.equal(await hex(), false);
  await page.evaluate(() => window.mc.ran({name: "plan-a", error: "the workspace is gone"}));
  assert.equal(await hex(), true, "a failed action left the hex hidden");
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "The agent didn't start");
  assert.equal(await page.textContent(".toast.bad .tmsg code"), "the workspace is gone");
  // it worked but nothing moved: a scan begun after the reply that still has it on you brings it back
  await card(); await page.click("#hcard .mv .go");
  await page.evaluate(() => window.mc.ran({name: "plan-a", ok: true}));
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000), scanned: 200, scan_started: 150}), snap);
  assert.equal(await hex(), false, "an older scan brought it back");
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000), scanned: 300, scan_started: Math.floor(Date.now() / 1000) + 5}), snap);
  assert.equal(await hex(), true, "it still needs you after a fresh scan, but stayed hidden");
  // gone from the snapshot after the action (merged and cleaned up): it stays gone, and nothing waits on it
  await card(); await page.click("#hcard .mv .go");
  await page.evaluate(() => window.mc.ran({name: "plan-a", ok: true}));
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000), pucks: s.pucks.slice(1)}), snap);
  assert.equal(await hex(), false);
  assert.deepEqual(await page.evaluate(() => Object.keys(ui.acting)), []);
});

test("card actions: errors keep their end, join, and time out at 5s; a late reply still says so; a failing scan isn't re-asked every 5s; an empty move hides nothing", async page => {
  const snap = JSON.parse(JSON.stringify(SNAP));
  Object.assign(snap.pucks[0], {agent: {...snap.pucks[0].agent, state: "idle"}, sig: "e1", mr: {iid: 12, ref: "!12", state: "merged", title: "x"},
    moves: [{kind: "signoff", step: "deploy", run: "make deploy"}]});
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000)}), snap);
  await page.evaluate(() => document.body.append(hoverCard(puckOf("plan-a"))));
  await page.click("#hcard .mg .go");
  const err = "`make deploy` exited 2: " + "x".repeat(700) + "\nError: the real cause";
  await page.evaluate(e => window.mc.merged({op: "signoff", path: "/w/plan-a", step: "deploy", error: e}), err);
  const shown = await page.textContent(".toast.bad .tmsg code");
  assert.ok(shown.endsWith("\nError: the real cause") && shown.startsWith("…"), "the error's tail (stderr's cause) was cut: " + shown.slice(-40));
  await page.evaluate(() => { ui.task.done = null; clearTimeout(ui.task.t); ui.task.t = 0; note("Didn't merge", "y", true) });
  assert.notEqual(await page.evaluate(() => ui.task.t), 0, "an error toast has no timer (DESIGN.md §10.14: toasts are timed)");
  await page.evaluate(() => { const st = window.setTimeout; window.__ms = []; window.setTimeout = (f, ms) => { window.__ms.push(ms); return st(f, ms) }; note("Didn't merge", "z", true); window.setTimeout = st });
  assert.ok((await page.evaluate(() => window.__ms)).includes(5000), "a close-only toast isn't 5s (DESIGN.md §10.14)");
  // due while hovered (or focused): it waits; render() rebuilding the toast mustn't strand it either
  await page.evaluate(() => { ui.task.done = null; note("Didn't merge", "h", true) });
  await page.hover(".toast .tmsg");
  await page.evaluate(() => new Promise(r => { toastTimer(10); render(); setTimeout(r, 120) }));
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "Didn't merge", "the toast went while it was being read");
  await page.mouse.move(5, 5);  // its whole time again once let go: the fake-clock toast test checks that
  await page.waitForFunction(() => !document.querySelector(".toast"), null, {timeout: 3000});
  await page.evaluate(() => { ui.task.done = null; note("Didn't merge", "y", true) });
  await page.evaluate(() => note("Merged", "Squash"));
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "Didn't merge", "a success covered the error");
  await page.evaluate(() => note("Deploy didn't finish", "exit 2", true));
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "2 actions failed", "a second error replaced the first");
  assert.equal(await page.textContent(".toast.bad .tmsg code"), "Didn't merge: y\nDeploy didn't finish: exit 2");
  await page.evaluate(() => { ui.task.done = null; act({name: "plan-b", path: "/w/café-ü", sig: "x"}, "f", {cmd: "merge", path: "/w/café-ü"}) });
  assert.deepEqual(await page.evaluate(() => Object.keys(ui.acting)), ["plan-b"], "a path the app's \\w accepts (Unicode letters) was refused");
  await page.evaluate(() => { delete ui.acting["plan-b"]; render() });
  // a reply after the action was given up on (ACT_LOST): the error still reaches the toast
  await page.evaluate(() => { ui.task.done = null; window.mc.merged({path: "/w/plan-a", error: "409: already merged"}) });
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "Didn't merge");
  // the rescan the action asks for fails: settle() waits a minute before asking again
  await page.evaluate(() => { document.body.append(hoverCard(puckOf("plan-a"))) });
  await page.click("#hcard .mg .go");
  await page.evaluate(() => window.mc.merged({op: "signoff", path: "/w/plan-a", ok: true, step: "deploy", ran: "make deploy"}));
  const asked = (await posts(page, "rescan")).length;
  await page.evaluate(() => window.mc.rescanned(1));
  for (let i = 0; i < 3; i++) await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000)}), snap);
  assert.equal((await posts(page, "rescan")).length, asked, "a failed rescan is re-asked on every stream update");
  // a move whose prompt rendered empty: the app ignores it (no reply), so its hex mustn't wait on one
  await page.evaluate(() => { delete ui.acting["plan-a"]; const p = puckOf("plan-a"); p.moves = [{kind: "ci", prompt: "", own: true, repo: "widgets"}]; document.body.append(hoverCard(p)) });
  await page.evaluate(() => { ui.task.done = null; render() });
  await page.click("#hcard .mv .go");
  assert.deepEqual(await page.evaluate(() => Object.keys(ui.acting)), []);
  assert.equal((await posts(page, "run")).length, 0);
  assert.equal(await page.textContent(".toast.bad .tmsg code"), "This step has no prompt yet.", "a dead button: nothing says why");
  // a New task's success doesn't cover a card's error (the new workspace is ringed on the map anyway)
  await page.evaluate(() => { ui.task.fresh = null; const st = window.setTimeout; window.__ms = []; window.setTimeout = (f, ms) => { window.__ms.push(ms); return st(f, ms) };
    tasked({op: "new", name: "t1", repo: "widgets"}); window.setTimeout = st });
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "The agent didn't start");
  assert.ok((await page.evaluate(() => window.__ms)).includes(5000) && !(await page.evaluate(() => window.__ms)).includes(12000), "the kept card toast went to 12s");
  await page.click(".toast .btn.x");
  assert.equal(await page.evaluate(() => ui.task.fresh), null, "the task's ring outlives the toast it was under");
  await page.evaluate(() => { ui.task.done = {name: "t2", repo: "widgets"}; ui.task.fresh = "t2"; toastTimer(300); render() });
  await page.click(".toast .btn.pri");  // Open in Basement: the toast's timer and ring go with it, so the timer can't clear a later task's ring
  assert.equal(await page.evaluate(() => ui.task.fresh), null, "Open in Basement left the task's ring on");
  await page.mouse.move(5, 5);
  await page.evaluate(() => { ui.task.fresh = "t3" });
  await page.evaluate(() => new Promise(r => setTimeout(r, 1500)));
  assert.equal(await page.evaluate(() => ui.task.fresh), "t3", "a dismissed toast's timer cleared a later task's ring");
  // a New task error up: a card's outcome replaces it (as before), never joins it as "undefined: …"
  await page.evaluate(() => { ui.task.done = {error: "repo gone"}; note("Merged !12 into main", "Squash") });
  assert.equal(await page.textContent(".toast .tmsg b"), "Merged !12 into main", "a New task error swallowed a card's success");
  await page.evaluate(() => { ui.task.done = {error: "repo gone"}; note("Didn't merge !12", "405", true) });
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "Didn't merge !12");
  // joined errors keep each one's title: a long first one is cut to its own end, not cut off by the second
  await page.evaluate(() => { ui.task.done = null; note("Deploy didn't finish", "x".repeat(900) + " real cause", true); note("Didn't merge !12", "405", true) });
  const joined = await page.textContent(".toast.bad .tmsg code");
  assert.ok(joined.startsWith("Deploy didn't finish: …") && joined.includes(" real cause\nDidn't merge !12: 405"), joined.slice(0, 40));
  // its state moves while the action runs (the agent stopped): that isn't the result, so the reply's sig is the baseline
  await page.evaluate(() => { ui.task.done = null; ui.rescanFail = 0; const p = puckOf("plan-a"); p.moves = [{kind: "signoff", step: "deploy", run: "make deploy"}]; hideCard(); document.body.append(hoverCard(p)) });
  await page.click("#hcard .mg .go");
  snap.pucks[0].sig = "e2";
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000)}), snap);
  await page.evaluate(() => window.mc.merged({op: "signoff", path: "/w/plan-a", ok: true, step: "deploy", ran: "make deploy"}));
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000)}), snap);
  assert.deepEqual(await page.evaluate(() => Object.keys(ui.acting)), ["plan-a"], "back with a stale Deploy button before any scan saw the sign-off");
  // an older dash.py (no scan_started): a scan's finish time alone doesn't bring it back; nor does the map offer to clear filters meanwhile
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000), scanned: Math.floor(Date.now() / 1000) + 60, pucks: s.pucks.slice(0, 1)}), snap);
  assert.deepEqual(await page.evaluate(() => Object.keys(ui.acting)), ["plan-a"], "a scan that may predate the reply brought it back");
  assert.equal(await page.textContent("#main .empty h2"), "Working on it", "the last workspace mid-action reads as a filter hiding everything");
  assert.equal(await page.$("#main .empty button"), null);
  // a path the app refuses without a reply: nothing hides, the toast says why
  await page.evaluate(s => { delete ui.acting["plan-a"]; s.pucks[0].path = "/w/My Projects/a"; window.mc.update({...s, ts: Math.floor(Date.now() / 1000)});
    document.body.append(hoverCard(puckOf("plan-a"))) }, snap);
  await page.click("#hcard .mg .go");
  assert.deepEqual(await page.evaluate(() => Object.keys(ui.acting)), []);
  assert.equal((await posts(page, "signoff")).filter(m => m.path.includes(" ")).length, 0);
  assert.equal(await page.textContent(".toast.bad .tmsg b"), "Deploy didn't finish");
});

test("hover card button: a step written ahead (the snapshot's suggestion) is ready the moment the card opens", async page => {
  const snap = await page.evaluate(() => JSON.parse(JSON.stringify(ui.raw)));
  snap.pucks[0].agent.state = "idle"; snap.pucks[0].sig = "plan-aidle";
  snap.pucks[0].suggestion = {label: "Address review threads", prompt: "Reply to the 2 open threads on !12 and push the fixes."};
  await page.evaluate(s => window.mc.update(s), snap);
  await page.evaluate(() => document.body.append(hoverCard(puckOf("plan-a"))));
  assert.equal(await page.textContent("#hcard .btn.nx"), "Address review threads");
  assert.equal((await posts(page, "suggest")).length, 0, "asked again for a step it already has");
  await page.click("#hcard .btn.nx");
  assert.deepEqual((await posts(page, "run")).map(m => m.prompt), ["Reply to the 2 open threads on !12 and push the fixes."]);
  await page.evaluate(() => window.mc.ran({name: "plan-a", ok: true}));  // the agent started: its hex is back once the state moves
  // the state moves on: the old step is stale, so the card waits for the new one, and an open card picks it up from the next snapshot
  snap.pucks[0].sig = "plan-aidle2"; snap.pucks[0].suggestion = null;
  await page.evaluate(s => { window.mc.update(s); hideCard(); document.body.append(hoverCard(puckOf("plan-a"))) }, snap);
  assert.equal(await page.textContent("#hcard .btn.nx"), "Please wait…");
  snap.pucks[0].suggestion = {label: "Fix lint job", prompt: "Fix the lint job."};
  await page.evaluate(s => window.mc.update(s), snap);
  assert.equal(await page.textContent("#hcard .btn.nx"), "Fix lint job");
});

test("hover card: the recommendation shows when it's on you; the steps bar opens a checklist with what's after merge", async page => {
  await page.evaluate(() => { const p = puckOf("plan-a"); Object.assign(p, {agent: {...p.agent, state: "idle"}, owner: "me", step: "deploy", step_index: 2,
    steps: ["mr_open", "merge", "deploy"], advice: "Once it's true, sign off deploy in the chat."}); hideCard(); document.body.append(hoverCard(p)) });
  assert.match(await page.textContent("#hcard .rec"), /sign off deploy/);
  assert.equal(await page.$("#hcard .chk"), null, "checklist open before asked");
  await page.click("#hcard .sbar");
  assert.equal(await page.getAttribute("#hcard .sbar", "aria-expanded"), "true");
  assert.deepEqual(await page.$$eval("#hcard .chk li", l => l.map(e => e.className + ":" + e.textContent)),
    ["ok:✓done: Implement & open MR", "ok:✓done: Merged", "sep:After merge", "cur:●now: Deploy · on you"]);
  assert.equal(await page.evaluate(() => localStorage.getItem("stepsOpen")), "1");
  await page.focus("#hcard .sbar"); await page.keyboard.press("Enter"); await page.waitForTimeout(300);
  assert.ok(await page.$("#hcard") && !(await page.$("#hcard .chk")), "Enter closes the checklist and the card stays");
  await page.keyboard.press("Enter");
  // a step with no rule (advice null): the model's line, from the snapshot's suggestion or when it lands; none from an older step
  await page.evaluate(() => { const p = puckOf("plan-a"); p.advice = null; ui.sug["plan-a"] = {sig: "old", prompt: "x", advice: "Stale."}; hideCard(); document.body.append(hoverCard(p)) });
  assert.equal(await page.textContent("#hcard .rec"), "RecommendedWorking one out…");
  await page.evaluate(() => window.mc.suggest({name: "plan-a", sig: puckOf("plan-a").sig, prompt: "Deploy it.", label: "Deploy", advice: "Run the deploy job, then sign off."}));
  assert.equal(await page.textContent("#hcard .rec"), "RecommendedRun the deploy job, then sign off.");
  await page.evaluate(() => { window.mc.suggest({name: "plan-a", sig: puckOf("plan-a").sig, prompt: "Deploy it.", label: "Deploy", advice: ""}); hideCard(); document.body.append(hoverCard(puckOf("plan-a"))) });
  assert.equal(await page.$("#hcard .rec"), null, "the model had nothing to add: no block");
  await page.evaluate(() => { const p = puckOf("plan-a"); p.advice = ""; p.steps = []; hideCard(); document.body.append(hoverCard(p)) });
  assert.equal(await page.$("#hcard .rec"), null, "a recommendation when it isn't on you");
  assert.equal(await page.$("#hcard .sbar"), null, "a lifecycle bar for a tree with no lifecycle");
});

test("hover card: the lifecycle line says where the change is and who holds it; the button says the move until its prompt is written", async page => {
  const card = life => page.evaluate(life => { const p = puckOf("plan-a"); p.agent.state = "idle"; p.life = life; hideCard(); document.body.append(hoverCard(p)) }, life);
  await page.evaluate(() => { delete ui.sug["plan-a"] });
  await card({stage_name: "Review", sub_name: "Open for review", holder: "you", pile: "yours", suggestion: {label: "Pick a milestone", kind: "you"},
    do: "!12 has no milestone, and no single release stands out. Pick the milestone it ships in."});
  assert.equal(await page.textContent("#hcard .lf"), "Review › Open for review · held by you");
  // greyed out with the lifecycle's move, not "Please wait…"; once written, the label is what the prompt a click sends will do
  assert.equal(await page.textContent("#hcard .btn.nx"), "Pick a milestone");
  assert.equal(await page.$eval("#hcard .btn.nx", b => b.disabled), true);
  await page.evaluate(() => window.mc.suggest({name: "plan-a", prompt: "List the open release milestones.", label: "List milestones", sig: puckOf("plan-a").sig}));
  assert.equal(await page.textContent("#hcard .btn.nx"), "List milestones");
  assert.equal(await page.$eval("#hcard .btn.nx", b => b.disabled), false);
  await page.click("#hcard .btn.nx");
  assert.deepEqual((await posts(page, "run")).map(m => m.prompt), ["List the open release milestones."]);
  // nobody holds a finished change, and one that isn't on the path has no line
  await card({stage_name: "Land", sub_name: "Merged", holder: null, pile: "done", suggestion: null});
  assert.equal(await page.textContent("#hcard .lf"), "Land › Merged");
  await card({stage_name: null, sub_name: null, holder: null, pile: "parked", suggestion: null});
  assert.equal(await page.$("#hcard .lf"), null);
  await card(undefined);  // a snapshot from before lifestage.py
  assert.equal(await page.$("#hcard .lf"), null);
});

test("hover card button: a failed step leaves a button that asks again", async page => {
  await page.evaluate(() => { puckOf("plan-a").agent.state = "idle"; document.body.append(hoverCard(puckOf("plan-a"))) });
  await page.evaluate(() => window.mc.suggest({name: "plan-a", error: "claude timed out"}));
  assert.equal(await page.textContent("#hcard .btn.nx"), "Run next step");
  await page.click("#hcard .btn.nx");
  assert.equal((await posts(page, "suggest")).length, 2, "the click asked again");
  await page.evaluate(() => window.mc.suggest({name: "plan-a", prompt: "rebase and push", sig: "plan-awaiting"}));
  assert.deepEqual((await posts(page, "run")).map(m => m.prompt), ["rebase and push"], "ran once it was written");
});

test("keyboard: a focused hex shows its card, Esc closes it", async page => {
  await page.focus(".cell[data-name='plan-b']");
  await page.waitForSelector("#hcard[data-name='plan-b']");
  await page.keyboard.press("Escape");
  assert.equal(await page.$("#hcard"), null);
});

test("drag: Esc cancels it, nothing docks", async page => {
  const b = await page.locator(".cell[data-name='plan-b']").boundingBox();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2); await page.mouse.down();
  await page.mouse.move(1300 - 30, 400, {steps: 5});
  assert.equal(await page.evaluate(() => document.body.classList.contains("dockhot")), true);
  await page.keyboard.press("Escape"); await page.mouse.up();
  assert.equal(await page.evaluate(() => ui.dock.length + document.querySelectorAll(".ghost").length + document.body.classList.contains("dragging")), 0);
});

test("answer: Send is disabled until there's text, and the reply box grows with it", async page => {
  await page.evaluate(() => openAnswer("plan-a"));
  assert.equal(await page.isDisabled("#asend"), true);
  const h0 = await page.evaluate(() => document.getElementById("areply").offsetHeight);
  await page.fill("#areply", "one\ntwo\nthree\nfour\nfive\nsix");
  assert.equal(await page.isDisabled("#asend"), false);
  assert.ok(await page.evaluate(() => document.getElementById("areply").offsetHeight) > h0, "the textarea didn't grow");
});

test("answer: the chosen option says Sending… until the agent picks it up", async page => {
  await page.evaluate(() => { openAnswer("plan-a"); window.mc.asked({name: "plan-a", question: "Which one?", options: [{label: "Left"}, {label: "Right"}]}) });
  await page.click(".aopts button:has-text('Right')");
  assert.equal((await posts(page, "answer")).pop().text, "Right");
  assert.match(await page.textContent(".aopts button.on"), /Sending…/);
});

test("answer: Chat about this opens a chat in the answer box, keeps the sheet open, and the thread carries on until an option is chosen (#173)", async page => {
  const MENU = {name: "plan-a", question: "Which way?", kind: "menu", chat: "3", options: [{key: "1", label: "Keep it"}, {key: "2", label: "Port it"}]};
  await page.evaluate(m => { openAnswer("plan-a"); window.mc.asked(m) }, MENU);
  assert.deepEqual(await page.$$eval(".aopts button .lbl", es => es.map(e => e.textContent)), ["Keep it", "Port it"], "Chat about this isn't an answer option");
  assert.equal(await page.$("#areply"), null, "a picker has no reply box until you choose to chat");
  await page.click("#achat");
  assert.equal(await page.evaluate(() => document.activeElement.id), "areply", "the composer takes the caret");
  await page.click("#aback");
  assert.equal(await page.$("#areply"), null, "Back to the options closes it");
  await page.click("#achat");
  await page.fill("#areply", "Why not both?");
  await page.press("#areply", "Enter");
  assert.deepEqual((await posts(page, "answer")).pop(), {cmd: "answer", name: "plan-a", text: "Why not both?", chat: true});
  await page.evaluate(() => window.mc.answered({name: "plan-a", ok: true}));
  await page.waitForTimeout(2000);
  assert.ok(await page.$(".sheet .abox"), "a chat keeps the sheet open for the reply");
  assert.deepEqual(await page.$$eval(".athread p", es => es.map(e => [e.className, e.textContent]).reverse()), [["ag", "Which way?"], ["you", "Why not both?"]]);
  // the agent works, then replies and ends its turn at its prompt, which Orca calls "done": the chat goes on, with nothing to ask the host
  const at = (state, last, dt) => page.evaluate(([state, last, dt, s]) => { Object.assign(s.pucks[0].agent, {state, since: s.pucks[0].agent.since + dt}, last ? {last} : {}); s.pucks[0].state = state === "waiting" ? "needs" : "working"; window.mc.update(s) }, [state, last, dt, JSON.parse(JSON.stringify(SNAP))]);
  await at("working", "", 500);
  assert.match(await page.textContent(".sheet"), /The agent is replying…/);
  const asks = (await posts(page, "ask")).length;
  await at("done", "…the review. Port it?", 1000);
  assert.equal((await posts(page, "ask")).length, asks + 1, "a reply at its prompt is fetched in full (the stream cuts it to 400 characters)");
  await page.evaluate(() => window.mc.asked({name: "plan-a", question: "Both would double the review. Port it?", kind: "prompt", options: []}));
  assert.equal(await page.$$eval(".athread p", es => es.length), 2, "the thread carried over");
  assert.match(await page.textContent("#aq"), /double the review/);
  assert.match(await page.textContent(".sheet .shd small"), /replied/);
  assert.match(await page.textContent(".acard[data-name='plan-a'] button.open"), /Answer/, "its card offers the chat again");
  await page.fill("#areply", "And the tests?");
  await page.click("#asend");
  assert.deepEqual((await posts(page, "answer")).pop(), {cmd: "answer", name: "plan-a", text: "And the tests?", chat: true}, "a chat message at its prompt (dash.py sends it as a plain message)");
  await page.evaluate(() => window.mc.answered({name: "plan-a", ok: true}));
  assert.equal(await page.$$eval(".athread p", es => es.length), 4);
  assert.equal(await page.isDisabled("#areply"), true, "no second message before the agent picks up the first");
  // it replies, then asks again with its picker: its reply shows above the question; choosing an option answers it and ends the chat
  await at("working", "", 1500);
  await at("waiting", "Tests stay as they are.", 2000);
  assert.equal(await page.isDisabled("#areply"), true, "in a chat, the composer waits to know whether a picker is up");
  await page.evaluate(m => window.mc.asked({...m, said: "Tests stay as they are."}), MENU);
  assert.equal(await page.$("#achat"), null, "already chatting: the composer is open, no second Chat button");
  assert.match(await page.textContent(".athread p:first-child"), /Tests stay as they are/, "its reply before the picker is in the thread");
  assert.ok(await page.$("#areply") && !(await page.isDisabled("#areply")), "you can keep chatting over the picker");
  await page.click(".aopts button:has-text('Port it')");
  assert.deepEqual((await posts(page, "answer")).pop(), {cmd: "answer", name: "plan-a", text: "2"});
  await page.evaluate(() => window.mc.answered({name: "plan-a", ok: true}));
  assert.equal(await page.evaluate(() => ui.ask["plan-a"].chat), null, "an option ends the chat");
  assert.deepEqual(page._errs, []);
});

test("answer: in a chat, only a 'done' newer than your message is the agent's reply (#173)", async page => {
  await page.evaluate(() => { openAnswer("plan-a"); window.mc.asked({name: "plan-a", question: "Which way?", kind: "menu", chat: "3", options: [{key: "1", label: "Keep it"}]}) });
  await page.click("#achat"); await page.fill("#areply", "Why?"); await page.press("#areply", "Enter");
  const t0 = await page.evaluate(() => { const t = Date.now(); window.mc.answered({name: "plan-a", ok: true, since: t}); return t });
  const done = (since, last) => page.evaluate(([since, last, s]) => { Object.assign(s.pucks[0].agent, {state: "done", since, last}); s.pucks[0].state = "working"; window.mc.update(s) }, [since, last, JSON.parse(JSON.stringify(SNAP))]);
  const asks = (await posts(page, "ask")).length;
  await done(t0, "What would you like to clarify?");  // its own turn after "Chat about this", ended before your message went in
  assert.equal((await posts(page, "ask")).length, asks, "not a reply to your message");
  assert.match(await page.textContent(".sheet"), /Waiting for the agent to pick it up/);
  await done(t0 + 5000, "Because of the release.");
  assert.equal((await posts(page, "ask")).length, asks + 1, "its reply");
  await page.evaluate(() => window.mc.asked({name: "plan-a", question: "Because of the release.", kind: "prompt", options: []}));
  await done(t0 + 9000, "Background task finished.");  // a turn you didn't start (a background task woke it)
  assert.equal((await posts(page, "ask")).length, asks + 1, "not a reply: you sent nothing since");
  assert.deepEqual(page._errs, []);
});

test("answer: mid-send the chat says Sending… while the agent works; a chat you never sent never needs you (#173)", async page => {
  const MENU = {name: "plan-a", question: "Which way?", kind: "menu", chat: "3", options: [{key: "1", label: "Keep it"}]};
  const state = st => page.evaluate(([st, s]) => { s.pucks[0].agent.state = st; s.pucks[0].agent.since += 1000; s.pucks[0].state = "working"; window.mc.update(s) }, [st, JSON.parse(JSON.stringify(SNAP))]);
  await page.evaluate(m => { openAnswer("plan-a"); window.mc.asked(m) }, MENU);
  await page.click("#achat");
  await state("done");  // you opened a chat, sent nothing, and the agent finished another way
  assert.equal(await page.evaluate(() => asking(puckOf("plan-a"))), false, "an unsent chat isn't a question");
  await page.evaluate(([m, s]) => { ui.ask = {}; window.mc.update(s); openAnswer("plan-a"); window.mc.asked(m) }, [MENU, JSON.parse(JSON.stringify(SNAP))]);
  await page.click("#achat"); await page.fill("#areply", "Why?"); await page.press("#areply", "Enter");
  await state("working");  // dash.py is still waiting out the agent's own "Chat about this" turn
  assert.match(await page.textContent(".sheet"), /Sending…/);
  assert.deepEqual(await page.$$eval(".athread p", es => es.map(e => e.textContent).reverse()), ["Which way?", "Why?"], "your message shows while it goes in");
  await page.evaluate(() => window.mc.answered({name: "plan-a", error: "the agent is still busy; send it again in a moment"}));
  assert.match(await page.textContent(".sheet"), /still busy.*Your message is kept/, "a failed first message keeps the chat (its picker may be gone)");
  await state("done");  // its own "Chat about this" turn ended: the chat is there to send it again
  await page.evaluate(() => window.mc.asked({name: "plan-a", question: "What would you like to clarify?", kind: "prompt", options: []}));
  assert.equal(await page.inputValue("#areply"), "Why?", "the draft is still there");
  assert.equal(await page.evaluate(() => asking(puckOf("plan-a"))), true);
  assert.deepEqual(page._errs, []);
});

test("answer: a chat ends on a permission prompt or when the agent goes away, and never spins on 'replying' (#173)", async page => {
  const MENU = {name: "plan-a", question: "Which way?", kind: "menu", chat: "3", options: [{key: "1", label: "Keep it"}, {key: "2", label: "Port it"}]};
  const chat = async () => {
    await page.evaluate(m => { openAnswer("plan-a"); window.mc.asked(m) }, MENU);
    await page.click("#achat"); await page.fill("#areply", "Why?"); await page.press("#areply", "Enter");
    await page.evaluate(() => window.mc.answered({name: "plan-a", ok: true}));
  };
  await chat();
  await page.evaluate(s => { s.pucks[0].agent.since += 1000; window.mc.update(s) }, JSON.parse(JSON.stringify(SNAP)));
  await page.evaluate(m => window.mc.asked({...m, chat: null, question: "Allow Bash: rm -rf build?", options: [{key: "1", label: "Yes"}, {key: "2", label: "No"}]}), MENU);
  assert.equal(await page.evaluate(() => ui.ask["plan-a"].chat), null, "a permission prompt ends the chat");
  assert.equal(await page.$("#areply"), null, "and takes no text");
  await page.evaluate(() => { ui.ask = {}; closeAnswer() });
  await chat();
  await page.evaluate(s => { s.pucks[0].agent.state = "idle"; s.pucks[0].state = "working"; window.mc.update(s) }, JSON.parse(JSON.stringify(SNAP)));
  assert.doesNotMatch(await page.textContent(".sheet"), /replying/);
  assert.equal(await page.evaluate(() => ui.ask["plan-a"].chat), null);
  assert.deepEqual(page._errs, []);
});

test("connecting: after 15s with no update it says so, and Retry says hello to the app again", async page => {
  await page.clock.install();
  await page.evaluate(() => { ui.snap = null; armConn(); render() });
  assert.equal(await page.locator("#retry").count(), 0, "Retry before the 15s are up");
  assert.equal(await page.isVisible("#connset"), false, "Settings before 5s");
  await page.clock.fastForward(5000);
  assert.equal(await page.isVisible("#connset"), true, "no Settings 5s in");
  await page.clock.fastForward(10000);
  assert.match(await page.textContent("#main"), /Workspace update timed out/);
  const n = (await posts(page, "ready")).length;
  await page.click("#retry");
  assert.equal((await posts(page, "ready")).length, n + 1);
  assert.deepEqual(await page.evaluate(() => { const b = document.getElementById("retry"); return [b.disabled, b.textContent] }), [true, "Retrying…"], "Retry left the screen while retrying");
  await page.clock.fastForward(15000);
  assert.equal(await page.evaluate(() => document.activeElement.id), "retry", "a failed retry hands focus back to Retry");
  assert.equal(await page.evaluate(() => CONN.refocus), false, "the refocus flag outlived its retry");
});

test("the bar offers Overview and List only; a stored view that's gone (the old CI view) opens on Overview", async page => {
  assert.deepEqual(await page.$$eval("#bar .seg button", es => es.map(e => e.textContent)), ["Overview", "List"]);
  for (const [view, on] of [["ci", "Overview"], ["list", "List"]]) {
    const p = await boot(page.context().browser(), {view});
    try {
      assert.deepEqual(await p.$$eval("#bar .seg button[aria-pressed=true]", es => es.map(e => e.textContent)), [on], view);
      assert.deepEqual(p._errs, [], view);
    } finally { await p.close() }
  }
});
test("Agents show by default; Hide sticks, and a window that was told to hide them stays hidden", async page => {
  assert.deepEqual(await page.$$eval(".agents .acard", es => es.map(e => e.dataset.name)), ["plan-a", "plan-b"], "a fresh window shows every live agent, the one waiting on a 'needs' workspace too");
  assert.equal(await page.getAttribute(".agents button.fold", "aria-expanded"), "true");
  await page.click(".agents button.fold");
  assert.equal(await page.locator(".agents .acard").count(), 0, "Hide folds them");
  assert.equal(await page.evaluate(() => localStorage.getItem("agentsFolded")), "1");
  const hid = await boot(page.context().browser(), {agentsFolded: "1"});
  try {
    assert.equal(await hid.locator(".agents .acard").count(), 0, "a stored Hide is kept");
    assert.match(await hid.textContent(".agents button.fold"), /^Show 2 agents\b/);
  } finally { await hid.close() }
});

test("Agents strip: working and waiting agents on 'needs' and 'raised' workspaces show, and the counts match the cards", async page => {
  const ws = (name, state, agent, type = "claude") => { const b = puck(name, name, agent); return {...b, state, agent: {...b.agent, type, last: ""}} };
  const pucks = [ws("ask-work", "needs", "working"), ws("ask-wait", "needs", "waiting"), ws("raise-work", "raised", "working"), ws("raise-wait", "raised", "waiting"),
    ws("quiet-work", "quiet", "working"), ws("idle-a", "working", "idle"), ws("idle-b", "done", "idle"), ws("down", "crashed", "idle"), ws("none", "working", "idle", ""), ws("back-up", "crashed", "working"), {...ws("back-ask", "crashed", "waiting"), step: "ci_green"}];  // its own step: three alike would stack into one hex
  await page.evaluate(s => { window.mc.update(s); ui.agentsFolded = false; ui.q = ""; render() }, {...SNAP, pucks});
  const cards = () => page.$$eval(".agents .acard", es => es.map(e => e.dataset.name).sort());
  const pills = () => page.$$eval(".agents .shead .pill", es => es.map(e => e.textContent));
  const live = ["ask-wait", "ask-work", "back-ask", "back-up", "quiet-work", "raise-wait", "raise-work"];
  assert.deepEqual(await cards(), live, "agents on 'needs' and 'raised' workspaces show");
  assert.deepEqual(await pills(), ["Working 4", "3 need you"], "the pills count the cards");
  assert.equal(await page.textContent(".agents .crash"), "3 crashed", "the chip counts crashed workspaces, as the footer does");
  assert.equal(await page.evaluate(() => counts().crashed), 3, "counts(), which feeds the map's legend and the footer, agrees");
  assert.equal(await page.locator(".agents .shead .muted").count(), 0, "no 'No agents working' line while agents work");
  for (const [n, says] of [["ask-work", /Claude is working/], ["raise-work", /Claude is working/], ["ask-wait", /Claude is waiting/], ["raise-wait", /Claude is waiting/], ["down", /Claude crashed/], ["back-up", /Claude is working/], ["back-ask", /Claude is waiting/]]) {  // each hex's hover card reads the state the strip shows
    await page.mouse.move(1, 1); await page.waitForSelector("#hcard", {state: "hidden", timeout: 2000});  // the last card can cover the next hex
    await page.hover(`.cell[data-name='${n}']`);
    assert.match(await page.textContent("#hcard"), says, n);
  }
  await page.click(".agents .crash");
  assert.deepEqual(await page.$$eval("#main .cell:not(.dim)", es => es.map(e => e.dataset.name).sort()), ["back-ask", "back-up", "down"], "the chip lights the workspaces it counted");
  assert.deepEqual(await cards(), ["back-ask", "back-up"], "the ones whose agent is live again keep their cards");
  assert.equal(await page.textContent(".agents .acard[data-name='back-ask'] .open"), "Answer", "a waiting agent on a crashed workspace is asked, not inspected");
  assert.deepEqual(await pills(), ["Working 1", "1 needs you"], "and the pills follow the chip's filter");
  await page.evaluate(() => { ui.q = ""; render() });  // clear the chip's search
  await page.click(".agents button.fold");
  assert.equal(await page.locator(".agents .acard").count(), 0, "Hide folds them");
  assert.match(await page.textContent(".agents button.fold"), /^Show 7 agents\b/, "the fold counts them all");
  await page.evaluate(() => { ui.agentsFolded = false; ui.q = "raise-"; renderMain() });
  assert.deepEqual(await cards(), ["raise-wait", "raise-work"], "a search narrows the cards");
  assert.deepEqual(await pills(), ["Working 1", "1 needs you"], "and the pills with them");
  assert.equal(await page.locator(".agents .shead .muted").count(), 0, "no empty line while some match");
  await page.evaluate(() => { ui.q = "zzz-nothing"; renderMain() });
  assert.equal(await page.textContent(".agents .shead .muted"), "No agents match the search", "a search that hides them isn't 'No agents working'");
  await page.evaluate(() => { ui.q = ""; window.mc.update({...ui.snap, pucks: ui.snap.pucks.filter(p => !["working", "waiting"].includes(p.agent.state))}); render() });
  assert.equal(await page.textContent(".agents .shead .muted"), "No agents working · 2 idle", "idle: agents neither working nor waiting, the crashed one left to its chip");
  const det = await boot(page.context().browser());
  try {  // the detached Agents window: the same agents, plus the crashed ones as cards
    await det.evaluate(s => { window.mc.update(s); ui.only = "agents"; ui.agentsFolded = false; render() }, {...SNAP, pucks});
    assert.deepEqual(await det.$$eval(".agents .acard", es => es.map(e => e.dataset.name).sort()), [...live, "down"].sort());
    assert.deepEqual(await det.$$eval(".agents .shead .pill", es => es.map(e => e.textContent)), ["Working 4", "3 need you"]);
    await det.evaluate(() => { ui.q = "down"; renderMain() });
    assert.deepEqual(await det.$$eval(".agents .acard", es => es.map(e => e.dataset.name)), ["down"], "a search can leave only a crashed card");
    await det.evaluate(() => { ui.q = "zzz-nothing"; renderMain() });
    assert.equal(await det.textContent(".agents .shead .muted"), "No agents match the search", "a search that hides the crashed card says so");
    assert.deepEqual(det._errs, []);
  } finally { await det.close() }
  assert.deepEqual(page._errs, []);
});

test("Agent cards keep one size in a grid; the shelf folds back; no null in the bar", async page => {
  const run = k => ({...SNAP.pucks[1], name: "run-" + k, path: "/w/run-" + k, sig: "r" + k, steps: ["mr_open", "ci_green", "merge"], step_index: 1});
  const wait = k => ({...SNAP.pucks[1], name: "waiting-on-review-" + k, path: "/w/q" + k, sig: "q" + k, state: "quiet", agent: {...SNAP.pucks[1].agent, state: "idle"}});
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [...SNAP.pucks, ...["c", "d", "e"].map(run), ...Array.from({length: 14}, (_, k) => wait(k))]});
  await page.evaluate(() => { ui.agentsFolded = false; render() });
  const box = await page.$$eval(".acard", es => es.map(e => e.getBoundingClientRect()).map(r => [Math.round(r.width), Math.round(r.top)]));
  assert.equal(box.length, 5, "plan-a's waiting agent, plan-b's and the three runs");
  assert.ok(new Set(box.map(b => b[1])).size < 4 && box.every(b => b[0] >= 420), `a wide pane puts cards side by side: ${JSON.stringify(box)}`);
  assert.equal(await page.locator(".acard[data-name='run-c'] .segs span").count(), 3, "the progress bar has a segment per step");
  await page.click(".shrow .chip.more");
  await page.click(".shrow .chip.more:has-text('Show less')");
  assert.match(await page.textContent(".shrow .chip.more"), /^\+\d+ more$/, "Show less didn't fold the shelf");
  await page.evaluate(() => { ui.away = ["console"]; render() });
  assert.equal(await page.evaluate(() => document.getElementById("bar").textContent.includes("null")), false, "an empty bar slot printed null");
});

test("hover card: Answer opens the question's sheet; Tab from the hex enters the card, Esc goes back to the hex", async page => {
  await page.focus(".cell[data-name='plan-a']");
  await page.waitForSelector("#hcard[data-name='plan-a']");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.getElementById("hcard").contains(document.activeElement)), true, "Tab didn't enter the card");
  await page.keyboard.press("Escape");
  assert.deepEqual(await page.evaluate(() => [!!document.getElementById("hcard"), document.activeElement.dataset.name]), [false, "plan-a"]);
  await page.evaluate(() => document.body.append(hoverCard(puckOf("plan-a"))));
  await page.focus("#hcard .hacts button:has-text('Answer')"); await page.keyboard.press("Enter");  // no mouse: a pointer crossing a hex swaps the card under the click (the flake)
  assert.equal(await page.evaluate(() => [!!document.getElementById("hcard"), !!document.getElementById("areply")].join()), "false,true");
});

test("widgets: a small text tile offers More only when its text is cut, and More opens all of it", async page => {
  const [long, short] = await page.evaluateHandle(() => [60, 2].map(n => document.body.appendChild(Object.assign(widget({widget: "text", props: {text: "word ".repeat(n).trim()}}, true), {style: "width:150px"})))).then(async a => [await a.getProperty("0"), await a.getProperty("1")]);
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.equal(await short.evaluate(e => !!e.querySelector(".w-more")), false, "More on a text that fits");
  await long.evaluate(e => e.querySelector(".w-more").click());
  assert.equal(await long.evaluate(e => e.querySelector(".w-more").textContent + e.querySelector(".w-text").classList.contains("open") + e.textContent.includes("word ".repeat(59))), "Lesstruetrue");
});

test("toast: it floats over the map, which keeps its size", async page => {
  const h0 = await page.evaluate(() => document.getElementById("main").clientHeight);
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", ok: true, name: "plan-b", repo: "widgets"}));
  assert.ok(await page.$(".toast"));
  assert.equal(await page.evaluate(() => document.getElementById("main").clientHeight), h0, "the toast took a row from the map");
});

test("drag: the map stays put, and the drop lands where the dock rail is drawn", async page => {
  const b = await page.locator(".cell[data-name='plan-b']").boundingBox();
  const a0 = await page.locator(".cell[data-name='plan-a']").boundingBox();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2); await page.mouse.down();
  await page.mouse.move(b.x + b.width / 2 + 40, b.y + b.height / 2 + 40, {steps: 3});
  assert.deepEqual(await page.locator(".cell[data-name='plan-a']").boundingBox(), a0, "the map moved under the drag");
  assert.equal(await page.textContent(".ghost .gl"), "Drag to chat or Park · Esc cancels", "the caption promised a drop that isn't there");
  assert.ok(await page.evaluate(() => [...document.querySelectorAll("main .btn")].every(b => b.getBoundingClientRect().right <= document.getElementById("dock").getBoundingClientRect().left)), "the rail covers a button");
  const d = await page.evaluate(() => { const r = document.getElementById("dock").getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2} });
  await page.mouse.move(d.x, d.y, {steps: 5});
  assert.match(await page.textContent(".ghost .gl"), /^Release to add .* to chat$/);
  await page.mouse.up();
  assert.deepEqual(await page.evaluate(() => ui.dock.map(x => x.key)), ["plan-b"]);
});

test("toast: hovering it holds the 12s timer, leaving restarts it", async page => {
  await page.clock.install();
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", ok: true, name: "plan-b", repo: "widgets"}));
  await page.hover(".toast");
  await page.clock.fastForward(20000);
  assert.ok(await page.$(".toast"), "the toast left while hovered");
  await page.mouse.move(5, 300);
  await page.clock.runFor(2000);  // the 1s re-check sees it let go: it gets its whole 12s again, not dismissal
  assert.ok(await page.$(".toast"), "let go after reading, the toast went at the next re-check");
  await page.clock.runFor(12000);
  assert.equal(await page.$(".toast"), null);
});

test("connecting failed: Open settings asks the app for its Settings window", async page => {
  await page.clock.install();
  await page.evaluate(() => { ui.snap = null; armConn(); render() });
  await page.clock.fastForward(15000);
  await page.click("#connset");
  assert.equal((await posts(page, "settings")).length, 1);
});

test("dock: clicking a docked workspace opens it, its × takes it out of the chat", async page => {
  await page.evaluate(() => { ui.dock = [{key: "plan-b", item: ui.snap.pucks[1]}]; renderDock() });
  await page.click("#dock .dopen");
  assert.equal(await page.evaluate(() => ui.page && ui.page.name), "plan-b");
  assert.equal(await page.evaluate(() => ui.dock.length), 1);
  await page.hover("#dock .dslot.full"); await page.click("#dock .dx");
  assert.equal(await page.evaluate(() => ui.dock.length), 0);
});

test("list: Enter or Space on a row opens it; Enter on its Answer button doesn't also open the row", async page => {
  await page.evaluate(() => { ui.view = "list"; render() });
  await page.focus('.lrow[data-name="plan-b"]'); await page.keyboard.press(" ");
  assert.equal(await page.evaluate(() => ui.page && ui.page.name), "plan-b");
  await page.evaluate(() => { ui.page = null; ui.view = "list"; render() });
  await page.focus('.lrow[data-name="plan-a"] .lbtn'); await page.keyboard.press("Enter");
  assert.notEqual(await page.evaluate(() => ui.page && ui.page.name), "plan-a");
});

test("empty: no workspaces at all starts a task; a filter that hides them all offers Clear filters", async page => {
  await page.evaluate(s => window.mc.update({...s, pucks: []}), SNAP);
  assert.equal(await page.isDisabled("#bar .seg button >> nth=0"), true, "nothing to switch between");
  assert.equal(await page.isVisible("#main .empty button:has-text('New task')"), true);
  await page.evaluate(s => { window.mc.update(s); ui.paths = ["/nowhere"]; render() }, SNAP);
  await page.click("#main .empty button:has-text('Clear filters')");
  assert.equal(await page.evaluate(() => ui.paths), null);
});
test("L1's pick filters by worktree path, not project name (a puck's project is its forge path)", async page => {
  await page.evaluate(s => { mc.update(s); mc.paths([s.pucks[0].path]) }, SNAP);
  assert.equal(await page.evaluate(s => s.pucks.filter(inView).length, SNAP), 1);
});
test("L1's pick takes in a workspace made after the terminal page's last trees: the snapshot's own trees match the picked repos", async page => {
  const [a, b] = SNAP.pucks, snap = {...SNAP, trees: [{path: a.path, project: "alpha"}, {path: b.path, project: "beta"}]};
  await page.evaluate(s => { mc.update({...s, trees: [s.trees[1]]}); mc.paths([], ["alpha"]) }, snap);  // stale: the app's paths predate plan-a's worktree
  assert.equal(await page.locator("#main .cell, #main .lrow").count(), 0, "not in the snapshot's trees yet: hidden");
  await page.evaluate(s => mc.update(s), snap);
  assert.deepEqual(await page.evaluate(s => s.pucks.filter(inView).map(p => p.name), snap), ["plan-a"], "the fresh trees bring it in; beta stays out");
  assert.equal(await page.locator(`#main .cell[data-name="plan-a"], #main .lrow[data-name="plan-a"]`).count(), 1, "and it's drawn");
  assert.deepEqual(await page.evaluate(() => mc.focus().paths), [a.path], "Chief's window scope sees it too");
  await page.evaluate(s => { mc.update(s); mc.paths([s.pucks[1].path]) }, snap);  // an app without repos: paths only, as before
  assert.deepEqual(await page.evaluate(s => s.pucks.filter(inView).map(p => p.name), snap), ["plan-b"]);
});
test("a workspace `ade worktree create` just started is a puck at once, never in the snapshot's trees: its repo puts it in the picked window", async page => {
  // dash.py snapshot lists a workspace once: as a puck (here, a goal-only one with a working agent), so trees never carry its path
  const fresh = {...puck("openai-live-voice", "Switch voice", "working"), repo: "greenlight", project: "octo/greenlight"};
  const old = {...SNAP.pucks[0], repo: "greenlight"}, other = {...SNAP.pucks[1], repo: "widgets"};
  const snap = {...SNAP, pucks: [old, other, fresh], trees: [{path: "/w/main", project: "greenlight", main: true}]};
  await page.evaluate(s => { mc.update(s); mc.paths([s.pucks[0].path, "/w/main"], ["greenlight"]) }, snap);  // the app's paths predate the new workspace
  assert.deepEqual(await page.evaluate(s => s.pucks.filter(inView).map(p => p.name), snap), ["plan-a", "openai-live-voice"], "in by its repo; widgets stays out");
  assert.equal(await page.locator(`#main .cell[data-name="openai-live-voice"]`).count(), 1, "its hex is drawn");
  await page.evaluate(() => { ui.agentsFolded = false; render() });
  const strip = await page.locator("#main section.agents").innerText();
  assert.ok(strip.includes("Switch voice"), "and its agent is in Agents: " + strip);
  const f = await page.evaluate(() => mc.focus());
  assert.ok(f.paths.includes(fresh.path) && !f.paths.includes(other.path), "Chief's window scope has its path");
  assert.deepEqual(f.repos, ["greenlight"], "and the repos, so the BOARD takes in one made after this focus");
  await page.evaluate(() => mc.paths(null, null));
  assert.equal(await page.evaluate(() => mc.focus().repos), null, "all projects: no scope");
});
test("mc.paths before mc.update keeps Connecting until the snapshot arrives and draws only the picked project on first paint", async page => {
  const fresh = await page.context().browser().newPage({viewport: {width: 1300, height: 900}});
  await fresh.addInitScript(() => { window.__posts = []; window.webkit = {messageHandlers: {mc: {postMessage: m => window.__posts.push(m)}}} });
  await fresh.goto(PAGE);
  const a = {...SNAP.pucks[0], repo: "greenlight"}, b = {...SNAP.pucks[1], repo: "widgets"};
  const snap = {...SNAP, pucks: [a, b], trees: [{path: a.path, project: "greenlight"}, {path: b.path, project: "widgets"}]};
  await fresh.evaluate(p => mc.paths([p], ["greenlight"]), a.path);
  assert.equal(await fresh.locator("#main .empty").count(), 1, "still Connecting before snapshot");
  assert.equal(await fresh.locator("#main .cell, #main .lrow").count(), 0, "no hexes before snapshot");
  await fresh.evaluate(s => mc.update(s), snap);
  assert.deepEqual(await fresh.locator("#main .cell").evaluateAll(els => els.map(e => e.dataset.name)), [a.name], "only greenlight drawn on first paint");
  await fresh.close();
});
test("L1's Open projects: the terminal page's tiles; ticking a project posts it, All projects unticks", async page => {
  const L = [{id: "a", name: "Alpha", color: "#FF9F0A", repos: ["x/a"], busy: 1}, {id: "", name: "No Man's Land", color: "#8E8E93", repos: [], busy: 0}];
  await page.evaluate(L => mc.pickProjects(L, null), L);
  assert.equal(await page.locator("#pp label.tile").count(), 3, "All projects + 2");
  assert.match(await page.textContent("#pp .tile >> nth=1"), /1 repo · 1 agent working/);
  await page.click("#pp .tile >> nth=1"); assert.equal(await page.isChecked("#pp .tile input >> nth=0"), false, "picking one unticks All");
  await page.click("#pp button.pri");
  assert.deepEqual(await page.evaluate(() => __posts.filter(m => m.cmd === "project").pop()), {cmd: "project", only: ["a"]});
  assert.equal(await page.isHidden("#pp"), true);
});
test("L1's Open projects returns to All when the last project is unticked", async page => {
  const L = [{id: "a", name: "Alpha", color: "#FF9F0A", repos: ["x/a"], busy: 0}];
  await page.evaluate(L => mc.pickProjects(L, ["a"]), L);
  await page.click("#pp .tile input >> nth=1");
  assert.equal(await page.isChecked("#pp .tile input >> nth=0"), true);
  await page.click("#pp button.pri");
  assert.deepEqual(await page.evaluate(() => __posts.filter(m => m.cmd === "project").pop()), {cmd: "project", only: null});
});
test("L1's Open projects, New window: posts window with the ticked projects (not project), ⌘⇧N asks the app for it", async page => {
  const L = [{id: "a", name: "Alpha", color: "#FF9F0A", repos: ["x/a"], busy: 0}, {id: "b", name: "Beta", color: "#0969DA", repos: ["x/b"], busy: 0}];
  await page.evaluate(() => { __posts.length = 0 });
  await page.evaluate(L => mc.pickProjects(L, ["a"], false), L);  // ⌘⇧N's dialog: New window pressed, this window's pick ticked
  assert.equal(await page.getAttribute("#pp .seg button >> nth=0", "aria-pressed"), "true", "New window is the mode");
  assert.match(await page.textContent("#pp button.pri"), /Open window/);
  await page.click("#pp .tile input >> nth=1"); await page.click("#pp .tile input >> nth=2");  // untick Alpha, tick Beta
  await page.click("#pp button.pri");
  const posts = await page.evaluate(() => __posts.filter(m => m.cmd === "window" || m.cmd === "project"));
  assert.deepEqual(posts, [{cmd: "window", only: ["b"]}], "a new window with Beta; this window's pick untouched");
  await page.evaluate(L => mc.pickProjects(L, null, true), L);
  await page.click("#pp .seg button >> nth=0"); await page.click("#pp button.pri");
  assert.deepEqual(await page.evaluate(() => __posts.filter(m => m.cmd === "window").pop()), {cmd: "window", only: null}, "All projects: null");
  await page.keyboard.press("Meta+Shift+N");
  assert.ok(await page.evaluate(() => __posts.some(m => m.cmd === "pickNew")), "⌘⇧N posts pickNew");
});
test("terminal: arrows move between tabs, Enter opens the focused one", async page => {
  const tp = await page.context().browser().newPage({viewport: {width: 760, height: 460}});
  await tp.addInitScript(() => { window.webkit = {messageHandlers: {term: {postMessage: m => (window.__term = window.__term || []).push(m)}}} });
  await tp.goto(TERM_PAGE);
  await tp.waitForFunction(() => (window.__term || []).some(m => m.cmd === "ready"), null, {timeout: 8000});
  await tp.evaluate(() => term.restore(null));  // the app's answer to ready: no saved layout
  await tp.evaluate(() => { newTab(); show(tabs[0]) });
  await tp.focus(".tab"); await tp.keyboard.press("ArrowRight");
  assert.equal(await tp.evaluate(() => [...document.querySelectorAll(".tab")].indexOf(document.activeElement)), 1, "focus moved to tab 2");
  assert.equal(await tp.evaluate(() => tabs.indexOf(cur)), 0, "arrows only move focus");
  await tp.keyboard.press("Enter");
  assert.equal(await tp.evaluate(() => tabs.indexOf(cur)), 1, "Enter opened tab 2");
  await tp.close();
});

test("bridge: the app's theme colors restyle surfaces and chrome, never the state colors", async page => {
  const look = () => page.evaluate(() => { const r = getComputedStyle(document.documentElement), b = document.querySelector("#tbtn") || document.body.appendChild(Object.assign(document.createElement("button"), {id: "tbtn", className: "btn go"}));
    return {bg: r.getPropertyValue("--bg").trim(), orange: r.getPropertyValue("--orange").trim(), red: r.getPropertyValue("--red").trim(), blue: r.getPropertyValue("--blue").trim(),
            btn: b && getComputedStyle(b).backgroundColor} });
  const before = await look();
  await page.evaluate(() => { const s = document.documentElement.style; s.setProperty("--mc-bg", "#2E3440"); s.setProperty("--mc-accent", "#FF0000"); s.setProperty("--mc-on-accent", "#111113") });
  const after = await look();
  assert.equal(after.bg, "#2E3440");
  assert.equal(before.btn, "rgb(0, 130, 58)", "no theme: the primary button is Signal green (brand/DESIGN.md accent.solid, light)");
  assert.equal(after.btn, "rgb(255, 0, 0)", "the primary button takes the theme accent");
  assert.deepEqual([after.orange, after.red, after.blue], [before.orange, before.red, before.blue], "a theme accent changed a state color");
});

test("terminal: a tab whose title is just ~ is named by the folder its shell is in", async page => {
  const tp = await page.context().browser().newPage({viewport: {width: 760, height: 460}});
  await tp.addInitScript(() => { window.webkit = {messageHandlers: {term: {postMessage: m => (window.__term = window.__term || []).push(m)}}} });
  await tp.goto(TERM_PAGE);
  await tp.waitForFunction(() => (window.__term || []).some(m => m.cmd === "ready"), null, {timeout: 8000});
  await tp.evaluate(() => term.restore(null));  // the app's answer to ready: no saved layout
  await tp.evaluate(() => { const id = window.__term.find(m => m.cmd === "open").id; term.feed(id, btoa("\x1b]2;me@devbox:~\x07\x1b]7;file://devbox/home/me/w/retry-stuck-outbounds\x07$ ")) });
  assert.equal(await tp.textContent(".tab.on .nm"), "retry-stuck-outbounds");
  await tp.close();
});

test("terminal: keys send what Ghostty sends, and ⌘K / ⌘A / ⌘F / ⌘V / panes act like Ghostty's", async page => {
  const tp = await page.context().browser().newPage({viewport: {width: 1000, height: 600}});
  await tp.addInitScript(() => { window.webkit = {messageHandlers: {term: {postMessage: m => (window.__term = window.__term || []).push(m)}}} });
  await tp.goto(TERM_PAGE);
  await tp.waitForFunction(() => (window.__term || []).some(m => m.cmd === "ready"), null, {timeout: 8000});
  await tp.evaluate(() => term.restore(null));
  await tp.click(".pane canvas");
  const sent = async (keys, fn) => { await tp.evaluate(() => window.__term.length = 0); for (const k of keys) await (fn ? tp.evaluate(fn, k) : tp.keyboard.press(k));
    return tp.evaluate(() => window.__term.filter(m => m.cmd === "in").map(m => m.d).join("")) };
  const want = {"Shift+Tab": "\x1b[Z", "Shift+Enter": "\n", "Alt+ArrowLeft": "\x1bb", "Alt+ArrowRight": "\x1bf", "Alt+Backspace": "\x1b\x7f",
    "Meta+ArrowLeft": "\x01", "Meta+ArrowRight": "\x05", "Meta+Backspace": "\x15", "Meta+Home": "", "Meta+q": "", "Control+c": "\x03", "Control+v": "\x16", "Control+i": "\t", "Control+m": "\r", "Control+BracketLeft": "\x1b", "Enter": "\r", "Tab": "\t"};
  for (const [k, d] of Object.entries(want)) assert.equal(await sent([k]), d, k);
  // ⌥ without macos-option-as-alt types the character macOS makes (CDP can't produce ∫ from ⌥B, so this one is synthetic)
  assert.equal(await sent([0], () => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {key: "∫", code: "KeyB", altKey: true, bubbles: true, cancelable: true}))), "∫");
  await tp.evaluate(() => { let s = ""; for (let i = 0; i < 60; i++) s += `line${i} Needle needle\r\n`; cur.focus.t.write(s) });
  await tp.waitForTimeout(100);
  assert.ok(await tp.evaluate(() => cur.focus.t.getScrollbackLength() > 0));
  assert.equal(await sent(["Meta+k"]), "\x0c", "⌘K has the shell redraw");
  assert.equal(await tp.evaluate(() => cur.focus.t.getScrollbackLength()), 0, "⌘K clears the scrollback");
  await tp.evaluate(() => { let s = ""; for (let i = 0; i < 60; i++) s += `line${i} Needle needle\r\n`; cur.focus.t.write(s) });
  await tp.waitForTimeout(100);
  await tp.keyboard.press("Meta+a");
  assert.ok(await tp.evaluate(() => { const s = cur.focus.t.getSelection(); return s.startsWith("line0 ") && s.includes("line59") }), "⌘A takes the whole history");
  await tp.keyboard.press("Meta+f"); await tp.keyboard.type("Needle");
  assert.equal(await tp.textContent("#fn"), "1 of 60", "a capital matches case");
  await tp.keyboard.press("Enter");
  assert.equal(await tp.evaluate(() => cur.focus.t.getSelection()), "Needle");
  assert.equal(await tp.evaluate(() => cur.focus.t.buffer.active.getLine(fnd.hits[fnd.i].y).translateToString(true)), "line58 Needle needle", "↵ goes older");
  await tp.keyboard.press("Escape");
  assert.equal(await tp.evaluate(() => $("find").hidden), true);
  await tp.evaluate(() => window.__term.length = 0);
  await tp.evaluate(() => { const dt = new DataTransfer(); dt.setData("text/plain", "a\nb"); cur.focus.t.write("\x1b[?2004h");
    setTimeout(() => cur.focus.box.querySelector("textarea").dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true})), 50) });
  await tp.waitForTimeout(150);
  assert.equal(await tp.evaluate(() => window.__term.filter(m => m.cmd === "in").map(m => m.d).join("")), "\x1b[200~a\rb\x1b[201~", "⌘V pastes once, bracketed");
  await tp.keyboard.press("Meta+d"); await tp.waitForTimeout(150);
  const [a, b] = await tp.evaluate(() => paneOf(cur).map(p => p.id));
  await tp.keyboard.press("Meta+Alt+ArrowLeft");
  assert.equal(await tp.evaluate(() => cur.focus.id), a, "⌘⌥← focuses the pane on the left");
  await tp.keyboard.press("Meta+Shift+Enter");
  assert.equal(await tp.evaluate(id => panes.get(id).box.getBoundingClientRect().width, b), 0, "⌘⇧↵ zooms the focused pane");
  await tp.close();
});

test("Chief's picks: the hexes its reply is about get the ring, the rest dims; [] or a click on one clears", async page => {
  const name = await page.evaluate(() => document.querySelector(".cell").dataset.name);
  await page.evaluate(n => window.mc.pick([n, "not-a-workspace"]), name);
  assert.ok(await page.$(".fieldwrap.picking"), "the hive steps back");
  assert.deepEqual(await page.$$eval(".cell.picked", e => e.map(c => c.dataset.name)), [name]);
  assert.equal(await page.$eval("#hcard", e => e.dataset.name), name, "one picked hex on screen: its card opens");
  await page.evaluate(() => window.mc.pick([]));
  assert.ok(!(await page.$(".fieldwrap.picking")) && !(await page.$(".cell.picked")) && !(await page.$("#hcard")), "[] clears, card too");
  await page.evaluate(n => window.mc.pick([n], false), name);
  assert.deepEqual(await page.$$eval(".cell.picked", e => e.map(c => c.dataset.name)), [name]);
  assert.ok(!(await page.$("#hcard")), "lit but not sure enough (open false): no card");
  await page.evaluate(n => window.mc.pick([n], true), name);
  assert.equal(await page.$eval("#hcard", e => e.dataset.name), name, "sure (open true): its card opens");
  await page.evaluate(n => window.mc.pick([n]), name);
  await page.click(`.cell.picked`);
  assert.ok(!(await page.$(".cell.picked")), "clicking a picked hex clears the picks");
});

test("a selected hex whose workspace drops out of a refresh closes its card and leaves the selection", async page => {
  await page.evaluate(() => window.mc.pick(["plan-b"], true));
  assert.equal(await page.$eval("#hcard", e => e.dataset.name), "plan-b", "the picked hex's card is open");
  await page.evaluate(s => window.mc.update({...s, ts: 2}), SNAP);
  assert.equal(await page.$eval("#hcard", e => e.dataset.name), "plan-b", "a refresh that still has it keeps the card");
  await page.evaluate(s => window.mc.update({...s, ts: 3, pucks: s.pucks.filter(p => p.name !== "plan-b")}), SNAP);
  assert.equal(await page.$("#hcard"), null, "its workspace is gone: the card closes on its own");
  assert.deepEqual(await page.evaluate(() => [...ui.picked]), [], "and the selection clears");
  assert.deepEqual((await posts(page, "unpick")).map(m => m.gone), [["plan-b"]], "the app forgets just that one, so a reload doesn't light it again");
  assert.ok(!(await page.$(".fieldwrap.picking")), "the hive no longer dims around a gone pick");
  await page.evaluate(s => window.mc.update({...s, ts: 4}), SNAP);
  assert.ok(!(await page.$(".cell.picked")) && !(await page.$("#hcard")), "it coming back doesn't revive the old pick");
  await page.evaluate(() => window.mc.pick(["plan-a"], true));
  await page.evaluate(s => window.mc.update({...s, ts: 5, pucks: s.pucks.filter(p => p.name !== "plan-b")}), SNAP);
  assert.equal(await page.$eval("#hcard", e => e.dataset.name), "plan-a", "another workspace leaving doesn't close this card");
  assert.equal((await posts(page, "unpick")).length, 1, "nor clear the app's picks");
});

test("two picks, one workspace drops: the other stays lit and the app is told only the gone one", async page => {
  await page.evaluate(() => window.mc.pick(["plan-a", "plan-b"]));
  await page.evaluate(s => window.mc.update({...s, ts: 2, pucks: s.pucks.filter(p => p.name !== "plan-b")}), SNAP);
  assert.deepEqual(await page.$$eval(".cell.picked", e => e.map(c => c.dataset.name)), ["plan-a"]);
  assert.deepEqual((await posts(page, "unpick")).map(m => m.gone), [["plan-b"]]);
});

test("an open card whose hex a refresh hides (parked from another window) closes; the pick stays for when it's shown again", async page => {
  await page.evaluate(() => window.mc.pick(["plan-b"], true));
  assert.equal(await page.$eval("#hcard", e => e.dataset.name), "plan-b");
  await page.evaluate(s => window.mc.update({...s, ts: 2, pucks: s.pucks.map(p => p.name === "plan-b" ? {...p, hidden: true} : p)}), SNAP);
  assert.equal(await page.$("#hcard"), null, "its hex left the hive: the card goes too");
  assert.deepEqual(await page.evaluate(() => [...ui.picked]), ["plan-b"], "still in the data: still picked");
  assert.equal((await posts(page, "unpick")).length, 0);
});

const STAGES = ["mr_open", "ci_green", "threads", "merge"];
const staged = (k, i) => ({...SNAP.pucks[1], name: "stg-" + k, path: "/w/stg-" + k, sig: "s" + k, steps: STAGES, step_index: i, step: STAGES[i] || "merge"});
test("agent card: hovering (or tabbing to) the progress bar lists every stage: done ones ticked, where it is now, what's next", async page => {
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [...SNAP.pucks, staged("a", 1), staged("z", 4)]});
  await page.evaluate(() => { ui.agentsFolded = false; render() });
  const bar = ".acard[data-name='stg-a'] .prog", pop = bar + " .stagepop";
  assert.equal(await page.isVisible(pop), false, "hidden until asked");
  await page.hover(bar);
  await page.waitForSelector(pop, {state: "visible"});
  const rows = await page.$$eval(pop + " li", es => es.map(e => [e.className, e.children[1].textContent, e.children[2].textContent]));
  assert.deepEqual(rows, [["done", "MR open", "Done"], ["cur", "CI green", "Now"], ["todo", "Threads", "Next"], ["todo", "Merge", ""]]);
  assert.match(await page.textContent(pop + " .sph"), /^Step 2 of 4\s*CI green$/);
  assert.equal(await page.getAttribute(pop + " li.cur", "aria-current"), "step");
  assert.equal(await page.$eval(bar, e => document.getElementById(e.getAttribute("aria-describedby")) === e.querySelector(".stagepop")), true, "the bar is described by its list");
  assert.equal(await page.$eval(pop, e => getComputedStyle(e).pointerEvents), "none", "it never takes the click meant for the output line under it");
  assert.equal(await page.$$eval(".acard[data-name='stg-a'] .lbls span[title]", es => es.length), 0, "no native title tips on top of it");
  const [card, next] = await page.$$eval(".acard", es => es.filter(e => e.matches(":hover") || e.dataset.name === "stg-z").map(e => getComputedStyle(e).zIndex));
  assert.ok(+card > +(next === "auto" ? 0 : next), `the hovered card is raised over its neighbours (${card} vs ${next})`);
  await page.mouse.move(1, 1);
  await page.waitForSelector(pop, {state: "hidden"});
  await page.focus(".acard[data-name='stg-a'] .open");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.classList.contains("prog")), true, "Tab reaches the bar");
  await page.waitForSelector(pop, {state: "visible"});
  await page.evaluate(s => mc.update(s), {...SNAP, pucks: [...SNAP.pucks, staged("a", 1), staged("z", 4)]});  // the 5s snapshot rebuilds the cards
  assert.equal(await page.evaluate(() => document.activeElement.matches(".acard[data-name='stg-a'] .prog")), true, "focus on the bar survives a re-render");
  assert.equal(await page.$eval(pop, e => getComputedStyle(e).animationName), "none", "rebuilt while open, it doesn't fade in again (no 5s blink)");
  await page.waitForSelector(pop, {state: "visible"});
  await page.keyboard.press("Escape");
  await page.waitForSelector(pop, {state: "hidden"});
  assert.equal(await page.evaluate(() => document.activeElement.classList.contains("prog")), true, "Esc hides the list and keeps focus on the bar");
  await page.evaluate(s => mc.update(s), {...SNAP, pucks: [...SNAP.pucks, staged("a", 1), staged("z", 4)]});
  assert.equal(await page.isVisible(pop), false, "and it stays put away through a re-render");
  assert.notEqual(await page.evaluate(() => cssId("v1.2")), await page.evaluate(() => cssId("v1_2")), "two names never share a stage list's id");
  await page.hover(".acard[data-name='stg-z'] .prog");
  const z = ".acard[data-name='stg-z'] .stagepop";
  await page.waitForSelector(z, {state: "visible"});
  assert.match(await page.textContent(z + " .sph"), /^All 4 steps done$/);
  assert.deepEqual(await page.$$eval(z + " li", es => es.map(e => e.className)), ["done", "done", "done", "done"]);
});

const E = (n, role, text) => ({id: "s1:" + n, role, text});
const atBottom = page => page.$eval("#aout-log", e => e.scrollHeight - e.scrollTop - e.clientHeight <= 2);
const tposts = async page => (await posts(page, "transcript")).map(m => m.before || null);
const nextPoll = (page, n) => page.waitForFunction(n => window.__posts.filter(m => m.cmd === "transcript").length >= n, n, {timeout: 8000});
test("agent card: clicking its output opens All output over the window, live: follows the bottom, holds when scrolled up, pages back; Esc returns", async page => {
  await page.evaluate(() => { ui.agentsFolded = false; render() });
  const line = ".acard[data-name='plan-b'] button.aline";
  await page.click(line);
  assert.ok(await page.isVisible("#aout"), "the full-window view opened");
  assert.deepEqual(await page.$eval("#aout", e => { const r = e.getBoundingClientRect(); return [r.width, r.height] }), [1300, 900], "it covers the whole window");
  assert.deepEqual(await tposts(page), [null], "asked for the newest page");
  assert.equal(await page.evaluate(() => document.activeElement.id), "aout-log", "the keys scroll it at once");
  assert.match(await page.textContent("#aout-more"), /Loading output/);
  const many = Array.from({length: 40}, (_, k) => E(k + 1, k % 3 ? "assistant" : "tool", `line ${k + 1}` + "\nmore".repeat(k % 4)));
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: "s0:999999999",
    entries: [{id: "s1:start", role: "sep", text: "claude session · Nov 20 10:00"}, E(0, "user", "Fix the flake"), ...many]});
  assert.equal(await page.locator("#aout-list > *").count(), 42);
  assert.deepEqual(await page.$$eval("#aout-list .aoe.you .aow, #aout-list .aoe.said .aow", es => [...new Set(es.map(e => e.textContent))]), ["Prompt", "Claude"]);
  assert.ok(await atBottom(page), "it opens at the newest output");
  const tool = "#aout-list .aoe.tool >> nth=0", tall = () => page.$eval("#aout-list .aoe.tool", e => e.getBoundingClientRect().height);
  await page.evaluate(() => { const e = document.querySelector("#aout-list .aoe.tool"); e.replaceWith(outEntry({id: "s1:900", role: "tool", text: "Bash " + "x ".repeat(2000)})) });
  const folded = await tall();
  await page.focus(tool); await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(() => document.activeElement.id), "aout-log", "unfolded, it hands the keyboard to the log");
  assert.ok(folded < 60 && await tall() > folded * 3, `a long tool line folds to 3 lines and a click unfolds it (${folded} → ${await tall()})`);
  await page.evaluate(() => outBottom());
  await page.evaluate(s => mc.update(s), SNAP);  // the 5s snapshot re-renders the page under it
  assert.ok(await page.isVisible("#aout") && await atBottom(page), "a re-render leaves it open and where it was");
  assert.equal(await page.locator(".acard[data-name='plan-b'] .prog, .acard[data-name='plan-b'] button.aline").count(), 1, "the compact card is still there under it");

  await nextPoll(page, 2);  // a working agent: refreshed every 3s
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: "x:1", entries: [...many.slice(-5), E(41, "assistant", "new one")]});
  assert.equal(await page.locator("#aout-list > *").count(), 43, "only the new entry is added");
  assert.equal(await page.textContent("#aout-list > :last-child .aotx"), "new one");
  assert.ok(await atBottom(page), "at the bottom, it follows new output");

  await page.$eval("#aout-log", e => { e.scrollTop = 0 });
  await page.waitForFunction(() => !ui.out.follow);
  await nextPoll(page, 3);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: "x:1", entries: [E(41, "assistant", "new one"), E(42, "tool", "Bash ls")]});
  assert.equal(await page.$eval("#aout-log", e => e.scrollTop), 0, "scrolled up, new output doesn't move it");
  assert.match(await page.textContent("#aout-new"), /^1 new/);
  await nextPoll(page, 4);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, error: "boom"});
  assert.match(await page.textContent("#aout-now"), /Couldn't refresh: boom/);

  const firstTop = () => page.$eval("#aout-list > :first-child", e => e.getBoundingClientRect().top);
  await page.focus("#aout-new");
  await page.$eval("#aout-log", e => { e.scrollTop = e.scrollHeight });
  await page.waitForFunction(() => document.getElementById("aout-new").hidden);
  assert.equal(await page.evaluate(() => document.activeElement.id), "aout-log", "scrolling to the bottom hides N new without dropping the keyboard");
  await page.$eval("#aout-log", e => { e.scrollTop = 0 });
  await page.waitForFunction(() => !ui.out.follow);
  const was = await firstTop();
  await page.click("#aout-older");
  assert.deepEqual((await tposts(page)).filter(Boolean), ["s0:999999999"], "Load earlier asks for the page before the first one's cursor");
  assert.equal(await page.isDisabled("#aout-older"), true);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: "s0:999999999", more: null,
    entries: [{id: "s0:start", role: "sep", text: "codex session · Nov 19 09:00"}, {id: "s0:0", role: "assistant", text: "older work", kind: "codex"}]});
  assert.equal(await page.textContent("#aout-list > :nth-child(2) .aotx"), "older work", "earlier output goes on top");
  assert.equal(await page.textContent("#aout-list > :nth-child(2) .aow"), "Codex", "labelled by the session that wrote it, not today's agent");
  assert.ok(Math.abs(await page.$eval("#aout-list > [data-id='s1:start']", e => e.getBoundingClientRect().top) - was) < 2, "what you were reading stays put");
  assert.match(await page.textContent("#aout-more"), /Start of this workspace's agent sessions/);

  await page.focus("#aout-new"); await page.keyboard.press("Enter");
  assert.ok(await atBottom(page) && await page.isHidden("#aout-new"), "Latest jumps back down and follows again");
  assert.equal(await page.evaluate(() => document.activeElement.id), "aout-log", "the hidden button hands the keyboard to the log, not to the page behind");
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.evaluate(() => document.getElementById("aout").contains(document.activeElement)), true, "Tab stays inside the dialog");
  await page.keyboard.press("Escape");
  assert.equal(await page.$("#aout"), null, "Esc closes it");
  assert.equal(await page.evaluate(l => document.activeElement === document.querySelector(l), line), true, "and the caret is back on the card's output");
  const n = (await tposts(page)).length;
  await page.waitForTimeout(2500);
  assert.equal((await tposts(page)).length, n, "closed, it stops refreshing");
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, entries: [E(99, "assistant", "late")]});
  assert.equal(await page.$("#aout"), null, "a late answer doesn't reopen it");
});

test("All output: Back closes it; more new output than a page holds is marked as a gap; an empty workspace says so until it isn't", async page => {
  await page.evaluate(() => { ui.agentsFolded = false; render() });
  await page.click(".acard[data-name='plan-b'] button.aline");
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries: []});
  assert.match(await page.textContent("#aout-list"), /No output from this workspace's agents yet/);
  await nextPoll(page, 2);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries: [E(1, "assistant", "a")]});
  assert.equal(await page.$("#aout-list .aonote"), null, "the first output replaces No output yet");
  await page.$eval("#aout-log", e => { e.style.flex = "none"; e.style.height = "60px"; e.scrollTop = 0; e.dispatchEvent(new Event("scroll")) });
  await page.waitForFunction(() => !ui.out.follow);
  await nextPoll(page, 3);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: "s1:5", entries: [E(70, "assistant", "x\n\n\n\nx"), E(71, "assistant", "y\n\n\n\ny")]});
  assert.deepEqual(await page.$$eval("#aout-list .aotx", es => es.map(e => e.textContent[0])), ["a", "x", "y"], "no overlap: kept what was shown, then the newest");
  assert.match(await page.textContent("#aout-list .aosep.gap"), /some may be skipped here/, "and says output was skipped between them");
  assert.equal(await page.$eval("#aout-log", e => e.scrollTop), 0, "a reader scrolled up stays where they were");
  await page.click("#aout-back");
  assert.equal(await page.$("#aout"), null);
});

test("All output: another busy session's lines ride along, a lost reply doesn't stop it, an error isn't re-announced, a closed view refocuses the card", async page => {
  await page.evaluate(() => { ui.agentsFolded = false; render(); OUT_WAIT = 300 });
  await page.click(".acard[data-name='plan-b'] button.aline");
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries: [E(1, "assistant", "main 1")], recent: []});
  await nextPoll(page, 2);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries: [E(1, "assistant", "main 1"), E(2, "assistant", "main 2")],
    recent: [{id: "rv:38", role: "assistant", text: "b38", kind: "codex"}, {id: "rv:39", role: "assistant", text: "LGTM", kind: "codex"}]});
  assert.deepEqual(await page.$$eval("#aout-list > *", es => es.map(e => e.textContent)), ["Claudemain 1", "Claudemain 2", "Codex · another sessionb38", "Codex · another sessionLGTM"]);
  // the review writes once more and is the newest session now: its page carries 40 older lines never shown; only its new one comes in
  await nextPoll(page, 3);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: "s1:0",
    entries: Array.from({length: 41}, (_, k) => ({id: "rv:" + k, role: "assistant", text: "b" + k, kind: "codex"})), recent: [E(2, "assistant", "main 2")]});
  assert.deepEqual((await page.$$eval("#aout-list .aotx", es => es.map(e => e.textContent))).slice(-3), ["b38", "LGTM", "b40"], "no old lines dumped at the bottom");
  assert.equal(await page.$("#aout-list .aosep.gap"), null, "and no false skipped line");
  await nextPoll(page, 4);  // no reply this time: the watchdog says so and keeps polling
  await page.waitForFunction(() => /no answer from the host/.test(document.getElementById("aout-now").textContent));
  const alert = await page.$("#aout-now .aerr");
  await nextPoll(page, 5);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, error: "no answer from the host"});
  await page.evaluate(s => mc.update({...s, pucks: s.pucks.map(p => ({...p, agent: {...p.agent, tool: "Bash", input: "ls " + Math.random()}}))}), SNAP);  // its tool moves meanwhile
  assert.equal(await page.evaluate(a => a === document.querySelector("#aout-now .aerr"), alert), true, "the same alert node: not announced again");
  await page.evaluate(() => { OUT_WAIT = 20000 });
  await nextPoll(page, 6);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries: [{id: "s1:3", role: "tool", text: "Bash " + "y ".repeat(2000)}]});
  await page.$eval("#aout-log", e => { e.style.flex = "none"; e.style.height = "80px"; e.scrollTop = e.scrollHeight; e.dispatchEvent(new Event("scroll")) });
  await page.waitForFunction(() => ui.out.follow);
  await page.click("#aout-list .aoe.tool");
  assert.equal(await page.evaluate(() => ui.out.follow), false, "unfolding the last line grew it under you: no jump on the next refresh");
  // the agent asks a question meanwhile: its card loses the output line; Esc lands on its Answer
  await page.evaluate(s => mc.update({...s, pucks: s.pucks.map(p => p.name === "plan-b" ? {...p, agent: {...p.agent, state: "waiting", last: "Which?"}} : p)}), SNAP);
  await page.keyboard.press("Escape");
  assert.equal(await page.evaluate(() => document.activeElement.matches(".acard[data-name='plan-b'] button.open")), true, "focus goes to the card's Answer");
});

test("All output: another session is labelled from the first paint; a new session takes over as main once the old one goes quiet", async page => {
  await page.evaluate(() => { ui.agentsFolded = false; render() });
  await page.click(".acard[data-name='plan-b'] button.aline");
  const R = (st, n, text, kind = "claude") => ({id: `${st}:${n}`, role: "assistant", text, kind});
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null,
    entries: [{id: "old:start", role: "sep", text: "codex session"}, R("old", 0, "yesterday", "codex"), {id: "m:start", role: "sep", text: "claude session"}, R("m", 0, "m0")],
    recent: [R("rv", 5, "LGTM", "codex")]});
  assert.deepEqual(await page.$$eval("#aout-list .aow", es => es.map(e => e.textContent)), ["Codex", "Claude", "Codex · another session"],
    "a review running alongside is labelled at once; yesterday's session is just history");
  // two sessions both busy, taking turns as newest: the labels hold
  const turn = async (n, entries, recent) => { await nextPoll(page, n); await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries, recent}) };
  await turn(2, [R("rv", 5, "LGTM", "codex"), R("rv", 6, "r6", "codex")], [R("m", 0, "m0")]);  // the review wrote last
  await turn(3, [R("m", 0, "m0"), R("m", 1, "m1")], [R("rv", 6, "r6", "codex")]);
  await turn(4, [R("rv", 6, "r6", "codex"), R("rv", 7, "r7", "codex")], [R("m", 1, "m1")]);
  assert.deepEqual(await page.$$eval("#aout-list .aoe", es => es.map(e => e.textContent)),
    ["Codexyesterday", "Claudem0", "Codex · another sessionLGTM", "Codex · another sessionr6", "Claudem1", "Codex · another sessionr7"],
    "the review stays 'another session' and the main agent stays itself, whoever wrote last");
  // m stops for good (quiet past OUT_QUIET); b's first lines came in through recent (no separator), then b is the newest and only one writing
  await page.evaluate(() => { OUT_QUIET = 0 });
  await nextPoll(page, 5);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries: [R("m", 0, "m0"), R("m", 1, "m1"), R("m", 2, "m2")], recent: [R("b", 0, "b0")]});
  await nextPoll(page, 6);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries: [{id: "b:start", role: "sep", text: "claude session"}, R("b", 0, "b0"), R("b", 1, "b1")], recent: []});
  const tail = await page.$$eval("#aout-list .aoe", es => es.slice(-2).map(e => e.textContent));
  assert.deepEqual(tail, ["Claude · another sessionb0", "Claudeb1"], "b is main once m went quiet: its new lines aren't 'another session'");
});

test("custom lifecycle stages show step_meta requirements and pre-MR signoff button without an MR", async page => {
  const custom = {...puck("pre-rfc", "Write RFC", "idle"), state: "needs", owner: "me", step: "rfc", mr: {},
    steps: ["rfc", "mr_open", "qa", "e2e", "merge"], step_index: 0,
    step_meta: {rfc: {done: "signoff"}, qa: {done: "label=qa-ok"}, e2e: {done: "check=e2e-suite"}},
    moves: [{kind: "signoff", step: "rfc", run: "make rfc"}]};
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [custom]});
  await page.evaluate(() => window.mc.pick(["pre-rfc"], true));
  const btnText = await page.textContent("#hcard .mg .go");
  assert.match(btnText, /Rfc pre-rfc/);
  assert.ok(!btnText.includes("undefined"), `unexpected undefined in signoff button: ${btnText}`);
  assert.deepEqual(await page.evaluate(p => [
    stepNote("rfc", 0, 0, p, {})[0],
    stepNote("qa", 2, 0, p, {})[0],
    stepNote("e2e", 3, 0, p, {})[0],
    stepNote("e2e", 3, 0, p, {jobs: [{name: "e2e-suite", status: "running"}]})[0],
    stepNote("e2e", 3, 0, p, {jobs: [{name: "e2e-suite", status: "failed"}]})[1],
  ], custom), ["needs sign-off", "needs label qa-ok", "needs check e2e-suite", "e2e-suite running", "bad"]);
});

test("the approved step reads 'none required' when the forge asks no approvals, never '0 approvals'", async page => {
  const p = {...puck("no-appr", "No approvals", "idle"), mr: {approvals: 0, approvals_required: 0}};
  assert.deepEqual(await page.evaluate(p => [
    stepNote("approved", 1, 1, p, {})[0],
    stepNote("approved", 1, 1, {...p, mr: {approvals: 1, approvals_required: 2}}, {})[0],
    stepNote("approved", 1, 1, {...p, mr: {approvals: 1, approvals_required: 0}}, {})[0],
  ], p), ["none required", "1 of 2", "1 of 1"]);
});

test("a rejected promise posts {cmd: error} with its name, for the app's diag trail", async page => {
  await page.evaluate(() => { Promise.reject(new TypeError("boom")) });
  await page.waitForFunction(() => window.__posts.some(m => m.cmd === "error" && m.name === "TypeError"));
  page._errs.splice(0);  // the rejection is the point
});

test("workspace page: Branch row shows p.branch (not p.name) and long h1 wraps inside its column", async page => {
  const longName = "ws-" + "x".repeat(120);
  const p = {...puck(longName, "Long title check", "idle"), mr: null, branch: "feat/real-branch-name"};
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [p]});
  await page.evaluate(n => window.mc.openPage(n), longName);
  const branchText = await page.locator(".facts dt:has-text('Branch') + dd").textContent();
  assert.equal(branchText, "feat/real-branch-name", "Branch row should render p.branch, not p.name");
  const box = await page.evaluate(() => {
    const h1 = document.querySelector(".ht h1"), ht = document.querySelector(".ht");
    return {h1Right: h1.getBoundingClientRect().right, htRight: ht.getBoundingClientRect().right};
  });
  assert.ok(box.h1Right <= box.htRight + 2, `long h1 overflowed container: ${JSON.stringify(box)}`);
});

test("workspace page: Host row says where its agent runs, in needs-you ink when it isn't where Chief runs; none when unknown", async page => {
  const here = {...puck("ws-here", "Here", "idle"), mr: null, host: "devbox", away: false};
  const away = {...puck("ws-away", "Away", "idle"), mr: {iid: 4, ref: "!4", title: "x", web_url: "https://x/4"}, host: "laptop", away: true};
  const none = {...puck("ws-none", "Unknown", "idle"), mr: null};
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [here, away, none]});
  const host = async n => { await page.evaluate(n => window.mc.openPage(n), n);
    const dd = page.locator(".facts dt:has-text('Host') + dd");
    return await dd.count() ? [await dd.textContent(), await dd.evaluate(e => (e.querySelector("span") || {}).style?.color || "")] : null };
  assert.deepEqual(await host("ws-here"), ["devbox", ""]);
  const [t, color] = await host("ws-away");
  assert.equal(t, "laptop · not where Chief runs");
  assert.match(color, /--orange-ink/);
  assert.equal(await host("ws-none"), null);
});

test("empty state: .empty clears the Context rail gutter and does not clip at narrow width", async page => {
  await page.setViewportSize({width: 440, height: 520});
  await page.evaluate(s => window.mc.update({...s, pucks: []}), SNAP);
  const geom = await page.evaluate(() => {
    const emp = document.querySelector("#main .empty"), ep = emp.querySelector(".ep"), dock = document.getElementById("dock");
    return {
      dockHidden: dock.hidden,
      dockLeft: dock.getBoundingClientRect().left,
      empRight: emp.getBoundingClientRect().right,
      epRight: ep.getBoundingClientRect().right,
    };
  });
  assert.equal(geom.dockHidden, false, "Context rail should be visible");
  assert.ok(geom.empRight <= geom.dockLeft + 1, `.empty extended under #dock: ${JSON.stringify(geom)}`);
  assert.ok(geom.epRight <= geom.dockLeft + 1, `.empty paragraph clipped under #dock: ${JSON.stringify(geom)}`);
});

test("one hex per MR: workspaces on the same MR fold into one stack that still reaches each of them (#158)", async page => {
  const mr = (iid, project = "acme/widgets") => ({iid, ref: "!" + iid, title: "Solvency", state: "opened", web_url: `https://gitlab.example/${project}/-/merge_requests/${iid}`});
  const ws = (name, state, m, step = "ci_green") => ({...puck(name, name, "idle"), state, step, mr: m, project: m ? m.web_url.split("/").slice(3, 5).join("/") : "acme/widgets"});
  const pucks = [ws("gated-inbounds", "working", mr(5304)), {...ws("solvency-5304", "needs", mr(5304), "threads"), steps: ["mr_open", "ci_green", "threads", "merge"], step_index: 2, agent: {...puck("x", "x", "waiting").agent}}, ws("elsewhere", "working", mr(5304, "acme/other")), ws("no-mr", "working", null)];
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks});
  const hubs = await page.$$eval("#main .fan .hub", es => es.map(e => e.getAttribute("aria-label")));
  assert.deepEqual(hubs, ["!5304, 2 workspaces on the same MR. Hover to fan out, click to open."], "the two workspaces on !5304 are one hex");
  assert.match(await page.textContent("#main .fan .hub"), /!5304/, "the stack goes by its MR's number");
  assert.equal(await page.locator("#main .fan .hub .hx.st-needs").count(), 1, "led by its most urgent workspace");
  assert.match(await page.textContent("#main .fan .hub"), /Answer/, "with its label: its agent waits on an answer");
  assert.equal(await page.getAttribute("#main .fan .hub .prog", "stroke-dasharray"), "0.86 0.14 ".repeat(2) + "0 4", "and its progress: step 3 of 4");
  assert.deepEqual(await page.$$eval("#main .fieldwrap > .cell", es => es.map(e => e.dataset.name).sort()), ["elsewhere", "no-mr"], "the same number in another project is another MR");
  assert.deepEqual(await page.$$eval("#main .fan .fp", es => es.map(e => e.dataset.dock).sort()), ["gated-inbounds", "solvency-5304"], "its fan holds both");
  assert.deepEqual(await page.$$eval("#main .fan .fp .hname", es => es.map(e => e.textContent).sort()), ["gated inbounds", "solvency 5304"], "the number is the hub's: each member says which workspace it is");
  assert.deepEqual(await page.evaluate(() => stackUp(shown()).find(p => p.dup).members.map(p => p.name)), ["solvency-5304", "gated-inbounds"], "most urgent first, whatever the snapshot's order");
  await page.evaluate(() => hl("gated-inbounds", true));
  assert.equal(await page.locator("#main .fan .hub.hl").count(), 1, "its agent card's hover rings the MR's hex");
  await page.evaluate(() => hl("gated-inbounds", false));
  await page.evaluate(() => dockToggle(stackUp(shown()).find(p => p.dup)));
  const ctx = (await posts(page, "context")).pop().items;
  assert.deepEqual(ctx.map(i => [i.label, i.mrs]), [["!5304 · 2", 1]], "docked, it's one MR to the chat");
  await page.evaluate(() => dockToggle(stackUp(shown()).find(p => p.dup)));
  await page.click("#main .fan .hub");
  assert.match(await page.textContent("#main .whead"), /!5304 · Needs you.*2 workspaces on this MR/, "drilled in, the breadcrumb names the MR");
  assert.deepEqual(await page.$$eval("#main .fieldwrap > .cell", es => es.map(e => e.dataset.name).sort()), ["gated-inbounds", "solvency-5304"], "each workspace gets its own hex in there");
  await page.click("#main .cell[data-name='gated-inbounds']");
  assert.equal(await page.evaluate(() => ui.page && ui.page.name), "gated-inbounds", "and its own page");
  await page.evaluate(() => { ui.page = null; drill(null); ui.picked = new Set(["gated-inbounds", "solvency-5304"]); render() });
  assert.equal(await page.locator("#main .hub.picked").count(), 1, "picking the MR lights one hex, not two");
  assert.equal(await page.locator("#main .fieldwrap > .cell.picked").count(), 0);
  await page.evaluate(() => { ui.picked = new Set(); ui.raw = {...ui.raw, pucks: [...ui.raw.pucks].reverse()}; ui.bell = {...ui.bell, asked: [{ws: ["gated-inbounds"], text: "Which one?"}]}; relift(); render() });  // asked one second: it leads by the tie-break alone
  assert.equal(await page.locator("#main .fan .hub .hbell").count(), 1, "greenlight asked about one of them: the MR's hex carries the bell");
  assert.equal(await page.evaluate(() => stackUp(shown()).find(p => p.dup).members[0].name), "gated-inbounds", "and the asked one leads the tie");
});

test("hovering an agent whose hex is in a stack highlights the stack hub and member, and hovering a fanned hex highlights its card", async page => {
  const mk = (name, step) => ({...puck(name, "Goal " + name, "working"), state: "working", step, owner: "working"});
  const pucks = [mk("st-1", "ci_green"), mk("st-2", "ci_green"), mk("st-3", "ci_green"), mk("solo-1", "threads")];
  await page.evaluate(s => { ui.agentsFolded = false; window.mc.update(s) }, {...SNAP, pucks});
  assert.equal(await page.locator(".fan .hub").count(), 1, "three identical workspaces collapse into one stack");
  assert.equal(await page.locator(".cell[data-name='solo-1']").count(), 1, "different step stays unstacked");

  // Hovering an agent card in the stack highlights the stack hub and member, not unrelated cells
  await page.hover(".acard[data-name='st-1']");
  assert.equal(await page.$eval(".acard[data-name='st-1']", e => e.classList.contains("hl")), true);
  assert.equal(await page.$eval(".fan .hub", e => e.classList.contains("hl")), true, "collapsed stack hub gets .hl");
  assert.equal(await page.$eval(".fan .fp[data-name='st-1']", e => e.classList.contains("hl")), true, "stack member gets .hl");
  assert.equal(await page.$eval(".cell[data-name='solo-1']", e => e.classList.contains("hl")), false, "unrelated hex stays unhighlighted");
  await page.waitForFunction(() => getComputedStyle(document.querySelector(".fan .hub .halo")).opacity === "1");
  assert.equal(await page.$eval(".cell[data-name='solo-1'] .halo", e => getComputedStyle(e).opacity), "0");

  // Moving away clears the highlight from card, member, and hub
  await page.mouse.move(1, 1);
  assert.equal(await page.$eval(".fan .hub", e => e.classList.contains("hl")), false, "leaving card clears stack hub .hl");
  assert.equal(await page.$eval(".fan .fp[data-name='st-1']", e => e.classList.contains("hl")), false);
  await page.waitForFunction(() => getComputedStyle(document.querySelector(".fan .hub .halo")).opacity === "0");

  // Overlapping highlight in the same stack keeps the hub highlighted until all members clear
  await page.evaluate(() => hl("st-1", true));
  await page.hover(".acard[data-name='st-2']");
  await page.mouse.move(1, 1);
  assert.equal(await page.$eval(".fan .hub", e => e.classList.contains("hl")), true, "hub stays highlighted while another member still has .hl");
  await page.evaluate(() => hl("st-1", false));
  assert.equal(await page.$eval(".fan .hub", e => e.classList.contains("hl")), false, "hub clears once last highlighted member clears");

  // Fanning the stack open and hovering a member highlights its agent card and member hex without double-highlighting the hub
  await page.hover(".fan .hub");
  await page.waitForFunction(() => {
    const el = document.querySelector(".fan.open .fp[data-name='st-2']");
    if (!el) return false;
    const cs = getComputedStyle(el);
    return cs.opacity === "1"
      && Math.abs(parseFloat(cs.left) - parseFloat(el.style.getPropertyValue("--x"))) < 1
      && Math.abs(parseFloat(cs.top) - parseFloat(el.style.getPropertyValue("--y"))) < 1;
  });
  await page.hover(".fan .fp[data-name='st-2']");
  assert.equal(await page.$eval(".acard[data-name='st-2']", e => e.classList.contains("hl")), true, "hovering a fanned member highlights its agent card");
  assert.equal(await page.$eval(".fan .fp[data-name='st-2']", e => e.classList.contains("hl")), true);
  await page.waitForFunction(() => getComputedStyle(document.querySelector(".fan .fp[data-name='st-2'] .halo")).opacity === "1");
  await page.waitForFunction(() => getComputedStyle(document.querySelector(".fan .hub .halo")).opacity === "0");
  await page.mouse.move(1, 1);
  assert.equal(await page.$eval(".acard[data-name='st-2']", e => e.classList.contains("hl")), false, "leaving fanned member clears its agent card highlight");
});

test("hexes with no MR: needs-you and crashed keep their solid fill, and every label reads at 4.5:1 on its body, light and dark (#155)", async page => {
  const pucks = [{...puck("ask-a", "Alpha", "waiting"), state: "needs"}, {...puck("crash-b", "Beta", "idle"), state: "crashed"},
    {...puck("work-c", "Gamma", "working"), state: "working"}, {...puck("ask-d", "Delta", "waiting"), state: "needs", mr: {iid: 7, ref: "!7", state: "opened", title: "x", web_url: "https://gl/7"}}];
  for (const look of ["dark", "light"]) {
    const got = await page.evaluate(([s, look]) => {
      document.documentElement.dataset.appearance = look; window.mc.update(s);
      const cv = document.createElement("canvas").getContext("2d", {willReadFrequently: true});
      const rgb = c => { cv.clearRect(0, 0, 1, 1); cv.fillStyle = "#000"; cv.fillStyle = c; cv.fillRect(0, 0, 1, 1); return [...cv.getImageData(0, 0, 1, 1).data].slice(0, 3) };
      const lum = c => { const [r, g, b] = rgb(c).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4 }); return .2126 * r + .7152 * g + .0722 * b };
      const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m); return (x + .05) / (y + .05) };
      return [...document.querySelectorAll("#main .hx")].map(hx => {
        const body = getComputedStyle(hx.querySelector(".body")), fill = body.fill;
        return {cls: [...hx.classList].filter(c => /^st-|^nomr$/.test(c)).sort().join(" "), fill: rgb(fill).join(","), dash: body.strokeDasharray,
          worst: Math.min(...[...hx.querySelectorAll(".hin b, .hin span, .hin svg")].map(e => ratio(getComputedStyle(e).color, fill)))};
      });
    }, [{...SNAP, pucks}, look]);
    const by = c => got.filter(g => g.cls === c);
    for (const c of ["nomr st-needs", "nomr st-crashed", "nomr st-working", "st-needs"]) assert.ok(by(c).length, `${look}: no ${c} hex in ${JSON.stringify(got)}`);
    for (const g of got) assert.ok(g.worst >= 4.5, `${look}: ${g.cls} label contrast ${g.worst.toFixed(2)}:1 on its body`);
    const lit = by("st-needs")[0].fill;
    assert.equal(by("nomr st-needs")[0].fill, lit, `${look}: a needs-you hex with no MR keeps the solid fill`);
    assert.notEqual(by("nomr st-crashed")[0].fill, by("nomr st-working")[0].fill, `${look}: a crashed hex with no MR keeps its solid fill`);
    for (const c of ["nomr st-needs", "nomr st-crashed", "nomr st-working"]) assert.notEqual(by(c)[0].dash, "none", `${look}: ${c} keeps the dashed no-MR outline`);
  }
});

test("steward hex badge and popover Log tab show per-MR activity and toggle steward mode", async page => {
  const p = {
    ...puck("st-ws", "Merge #42: Steward mode", "raised"),
    mr: {iid: 42, ref: "#42", state: "opened", title: "Steward mode", target_branch: "main", web_url: "https://github.com/octo/greenlight/pull/42"},
    steward: true,
    steward_drift: {kind: "head_changed", sha: "deadbeef1234", target: "main", old_value: "aaaa1111"},
    activity: [
      {ts: 1700000100, kind: "drift", step: "merge", sha: "deadbeef", target: "main", summary: "Paused: head changed", detail: "aaaa1111 → deadbeef", ok: false},
      {ts: 1700000000, kind: "rereview", step: "ci_green", sha: "aaaa1111", target: "main", summary: "Re-reviewed diff at aaaa1111", detail: "all contracts passed", ok: true},
    ],
  };
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [p, puck("plain-ws", "Plain goal", "working")]});
  assert.equal(await page.$("section.stewards"), null, "no separate stewards lane");
  assert.ok(await page.$(".cell[data-name='st-ws'] .stbadge.drift"), "stewarded hex shows small drift badge");
  assert.equal(await page.$(".cell[data-name='plain-ws'] .stbadge"), null, "non-stewarded hex has no badge");

  // Hovering opens #hcard with Overview + Log (2) tabs, drift banner, and Steward toggle
  await page.hover(".cell[data-name='st-ws']");
  await page.waitForSelector("#hcard");
  assert.ok(await page.$("#hcard .hcdrift"), "drift alert banner shown in popover");
  await page.click("#hcard [role='tab']:has-text('Log (2)')");
  assert.equal(await page.locator("#hcard .hclog li").count(), 2);
  assert.match(await page.locator("#hcard .hclog").textContent(), /Re-reviewed diff/);
  assert.equal(await page.locator("#hcard .hclog li.err").count(), 1);

  // Confirm drift and toggle steward post structured steward commands and update UI via mc.steward
  await page.click("#hcard .hcdrift button:has-text('Confirm')");
  assert.ok((await posts(page, "steward")).some(m => m.op === "confirm" && m.name === "st-ws"));
  await page.evaluate(r => window.mc.steward(r), {op: "confirm", name: "st-ws", ok: true, steward: true, steward_drift: null, activity: p.activity});
  assert.equal(await page.$("#hcard .hcdrift"), null, "drift banner clears after confirmation");
  assert.ok(await page.$(".cell[data-name='st-ws'] .stbadge:not(.drift)"), "badge returns to normal steward tone");

  await page.click("#hcard .sttog");
  assert.ok((await posts(page, "steward")).some(m => m.op === "off" && m.name === "st-ws"));
  await page.evaluate(r => window.mc.steward(r), {op: "off", name: "st-ws", ok: true, steward: false, steward_drift: null, activity: p.activity});
  assert.equal(await page.$(".cell[data-name='st-ws'] .stbadge"), null, "badge disappears when steward mode turns off");
});

test("agent avatars, hover cards, and All output support Claude, Codex, and Antigravity (agy)", async page => {
  await page.evaluate(s => {
    const agyPuck = JSON.parse(JSON.stringify(s.pucks[0]));
    agyPuck.name = "agy-ws"; agyPuck.state = "working"; agyPuck.agent = {state: "working", type: "agy", last: "Building", since: 1700000000000};
    s.pucks[0].state = "working"; s.pucks[0].agent.state = "working"; s.pucks[0].agent.type = "claude";
    s.pucks[1].agent.type = "codex";
    s.pucks.push(agyPuck);
    mc.update(s);
  }, JSON.parse(JSON.stringify(SNAP)));
  const logos = await page.$$eval(".acard .av svg path", es => es.map(e => e.getAttribute("d")));
  assert.equal(logos.length, 3);
  assert.equal(new Set(logos).size, 3, "Claude, Codex, and Antigravity must have distinct logos");
  assert.match(await page.locator(".acard[data-name='agy-ws']").textContent(), /Antigravity/);
  await page.click(".acard[data-name='agy-ws'] button.aline");
  await page.evaluate(() => mc.transcript({name: "agy-ws", before: null, more: null, entries: [
    {id: "c1:0", role: "assistant", text: "from agy", kind: "agy"},
    {id: "c2:0", role: "assistant", text: "from codex", kind: "codex"},
  ], recent: []}));
  assert.deepEqual(await page.$$eval("#aout-list .aow", es => es.map(e => e.textContent)), ["Antigravity", "Codex"]);
  assert.match(await page.textContent("#aout-sub"), /Antigravity is working/);
});

test("Agent log view phase 1: status strip, per-line relative-time gutter, and 'Since you last looked' divider", async page => {
  const now = Date.now();
  const rich = {
    ...puck("log-ws", "Agent log view", "working"),
    state: "working",
    steps: ["mr_open", "ci_green", "approved", "merge"],
    step_index: 1,
    step: "ci_green",
    agent: {state: "working", type: "claude", since: now - 300_000, tool: "Bash", input: "pytest -q", last: "Running tests", last_output_at: now - 180_000},
    mr: {iid: 42, ref: "#42", forge: "github", web_url: "https://github.com/o/r/pull/42", title: "Agent log view", draft: true, state: "opened", pipeline: "running"},
  };
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [rich]});
  await page.evaluate(() => { localStorage.removeItem("gl_last_looked"); ui.agentsFolded = false; render() });

  // Open from the agent card
  await page.click(".acard[data-name='log-ws'] button.aline");
  await page.evaluate(r => mc.transcript(r), {
    name: "log-ws", before: null, more: "mc-s1.g0:0", source: "pane", session: "mc-s1", last_output_ts: now - 180_000,
    tool: "Read", tool_input: "scripts/pane_log.py",
    entries: [
      {id: "mc-s1.g0:1", role: "line", text: "Starting pytest", ts: now - 600_000, kind: "pane"},
      {id: "mc-s1.g0:2", role: "line", text: "2 passed", ts: now - 180_000, kind: "pane"},
    ],
  });

  // Status strip shows working state, time since last output, current tool, PR + Draft, CI, and lifecycle step
  const strip = await page.textContent("#aout-strip");
  assert.match(strip, /Working/, "status pill");
  assert.match(strip, /Last output 3m ago/, "time since last output");
  assert.match(strip, /Bash: pytest -q/, "current tool from agent");
  assert.match(strip, /#42/, "PR ref");
  assert.match(strip, /Draft/, "PR draft tag");
  assert.match(strip, /CI running/, "CI status");
  assert.match(strip, /Step 2 of 4 · CI green/, "lifecycle step");
  assert.equal(await page.getAttribute("#aout-strip a.aos-pr", "href"), "https://github.com/o/r/pull/42");

  // Pane log styling and per-line relative-time gutter
  assert.equal(await page.evaluate(() => document.getElementById("aout-log").classList.contains("pane")), true);
  assert.deepEqual(await page.$$eval("#aout-list .aoe.line .aots", es => es.map(e => e.textContent)), ["10m ago", "3m ago"]);
  assert.equal(await page.$eval("#aout-list .aoe.line .aots", e => getComputedStyle(e).userSelect), "none", "gutter is non-selectable so copy grabs only log lines");
  assert.equal(await page.$("#aout-looked"), null, "first visit has no 'Since you last looked' divider");

  // Basement shortcut posts {cmd: "basement", path}
  await page.click("#aout-basement");
  assert.equal((await posts(page, "basement")).pop().path, rich.path);

  // Close saves lastLooked = 180s ago; reopen with newer lines shows 'Since you last looked' divider in exact spot
  await page.click("#aout-back");
  assert.equal(await page.evaluate(() => getLastLooked("log-ws")), now - 180_000);

  await page.click(".acard[data-name='log-ws'] button.aline");
  await page.evaluate(r => mc.transcript(r), {
    name: "log-ws", before: null, more: null, source: "pane", session: "mc-s1", last_output_ts: now - 5_000,
    tool: "Edit", tool_input: "app/dashboard.html",
    entries: [
      {id: "mc-s1.g0:1", role: "line", text: "Starting pytest", ts: now - 600_000, kind: "pane"},
      {id: "mc-s1.g0:2", role: "line", text: "2 passed", ts: now - 180_000, kind: "pane"},
      {id: "mc-s1.g0:3", role: "line", text: "All green", ts: now - 5_000, kind: "pane"},
    ],
  });
  assert.equal(await page.locator("#aout-looked").count(), 1, "divider appears once");
  assert.match(await page.textContent("#aout-looked"), /Since you last looked/);
  const order = await page.$$eval("#aout-list > *", es => es.map(e => e.id || e.dataset.id));
  assert.deepEqual(order, ["mc-s1.g0:1", "mc-s1.g0:2", "aout-looked", "mc-s1.g0:3"], "divider sits right before first unseen line");

  // Live relative-time gutter update: backdating data-ts ticks within 1.5s without re-rendering list
  await page.$eval("#aout-list .aoe.line[data-id='mc-s1.g0:3'] .aots", e => { e.dataset.ts = String(Date.now() - 120_000) });
  await page.waitForFunction(() => document.querySelector("#aout-list .aoe.line[data-id='mc-s1.g0:3'] .aots").textContent === "2m ago", null, {timeout: 3000});

  // Status strip updates when agent switches to waiting/idle and falls back to pane-detected tool when agent.tool is empty
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [{
    ...rich,
    agent: {...rich.agent, state: "waiting", tool: "", input: ""},
    mr: {...rich.mr, pipeline: "failed"},
  }]});
  const strip2 = await page.textContent("#aout-strip");
  assert.match(strip2, /Waiting/);
  assert.match(strip2, /Edit: app\/dashboard\.html/, "falls back to tool detected from recorded pane log");
  assert.match(strip2, /CI failed/);

  // Close & reopen with no new lines: divider does not appear, and live lines arriving while watching never spawn a stale divider
  await page.click("#aout-back");
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [{...rich, agent: {...rich.agent, state: "idle"}, mr: {...rich.mr, pipeline: null}}]});
  await page.evaluate(() => openPage("log-ws"));
  await page.click("button:has-text('View log')");
  await page.evaluate(r => mc.transcript(r), {
    name: "log-ws", before: null, more: null, source: "pane", session: "mc-s1", last_output_ts: now - 5_000,
    entries: [{id: "mc-s1.g0:3", role: "line", text: "All green", ts: now - 5_000, kind: "pane"}],
  });
  const stripNoPipe = await page.textContent("#aout-strip");
  assert.match(stripNoPipe, /Idle/);
  assert.doesNotMatch(stripNoPipe, /no pipeline/i, "MR without pipeline does not render 'CI no pipeline'");
  assert.equal(await page.$("#aout-looked"), null, "no divider when nothing arrived since last looked");

  // Live partial tail arrives while watching: appears cleanly without spawning a stale divider, then transitions to committed line
  await page.evaluate(() => { ui.out.inflight = true });
  await page.evaluate(r => mc.transcript(r), {
    name: "log-ws", before: null, more: null, source: "pane", session: "mc-s1", last_output_ts: now - 2_000,
    entries: [
      {id: "mc-s1.g0:3", role: "line", text: "All green", ts: now - 5_000, kind: "pane"},
      {id: "mc-s1.g0:tail", role: "line", text: "Half written", ts: now - 2_000, kind: "pane", partial: true},
    ],
  });
  assert.equal(await page.textContent("#aout-list [data-tail='1'] .aotx"), "Half written");
  assert.equal(await page.$("#aout-looked"), null, "live tail while watching never spawns a stale divider");

  await page.evaluate(() => { ui.out.inflight = true });
  await page.evaluate(r => mc.transcript(r), {
    name: "log-ws", before: null, more: null, source: "pane", session: "mc-s1", last_output_ts: now - 1_000,
    entries: [
      {id: "mc-s1.g0:3", role: "line", text: "All green", ts: now - 5_000, kind: "pane"},
      {id: "mc-s1.g0:4", role: "line", text: "Half written completed", ts: now - 1_000, kind: "pane"},
    ],
  });
  assert.equal(await page.$("#aout-list [data-tail='1']"), null, "committed line replaces partial tail");
  assert.equal(await page.textContent("#aout-list [data-id='mc-s1.g0:4'] .aotx"), "Half written completed");
  assert.equal(await page.$("#aout-looked"), null, "live output while watching never spawns a stale 'Since you last looked' divider");

  // Closing while scrolled up (follow=false) preserves seenTs so unseen lines show 'Since you last looked' on reopen
  await page.evaluate(() => { ui.out.follow = false; ui.out.inflight = true });
  await page.evaluate(r => mc.transcript(r), {
    name: "log-ws", before: null, more: null, source: "pane", session: "mc-s1", last_output_ts: now + 5_000,
    entries: [
      {id: "mc-s1.g0:3", role: "line", text: "All green", ts: now - 5_000, kind: "pane"},
      {id: "mc-s1.g0:4", role: "line", text: "Half written completed", ts: now - 1_000, kind: "pane"},
      {id: "mc-s1.g0:5", role: "line", text: "Unseen while scrolled up", ts: now + 5_000, kind: "pane"},
    ],
  });
  await page.click("#aout-back");
  assert.equal(await page.evaluate(() => getLastLooked("log-ws")), now - 1_000, "scrolled-up close keeps seenTs, not unseen ts");

  // Narrow viewport & light appearance sanity
  await page.setViewportSize({width: 480, height: 640});
  await page.evaluate(() => { document.documentElement.dataset.appearance = "light"; openOut("log-ws") });
  await page.evaluate(r => mc.transcript(r), {
    name: "log-ws", before: null, more: null, source: "pane", session: "mc-s1", last_output_ts: now + 5_000,
    entries: [
      {id: "mc-s1.g0:4", role: "line", text: "Half written completed", ts: now - 1_000, kind: "pane"},
      {id: "mc-s1.g0:5", role: "line", text: "Unseen while scrolled up", ts: now + 5_000, kind: "pane"},
    ],
  });
  assert.equal(await page.locator("#aout-looked").count(), 1, "reopening shows divider before line that arrived while scrolled up");
  const overflow = await page.evaluate(() => document.getElementById("aout-strip").scrollWidth > window.innerWidth + 2);
  assert.equal(overflow, false, "status strip wraps cleanly at 480px width");
  await page.click("#aout-back");
});

// the footer's usage meters (issue 193): one per configured agent in config order, its popover with every window
const NOW_S = () => Math.floor(Date.now() / 1000);
const USAGE = () => [
  {id: "claude", name: "Claude", state: "ok", plan: "Claude Max", src: "api", at: NOW_S(),
   windows: [{label: "Week", pct: 64, resets: NOW_S() + 3 * 86400 + 7 * 3600 + 30}, {label: "Fable", pct: 15, resets: NOW_S() + 3 * 86400}]},
  {id: "codex", name: "Codex", state: "ok", plan: "ChatGPT Pro", src: "api", at: NOW_S(),
   windows: [{label: "5h", pct: 92, resets: NOW_S() + 2 * 3600 + 600}, {label: "Week", pct: 30, resets: NOW_S() + 5 * 86400}]},
  {id: "opencode", name: "OpenCode", state: "na", plan: "OpenCode Go", note: "OpenCode publishes no usage API: its web console shows the plan's usage"},
  {id: "pi", name: "Pi", state: "failed", note: "can't reach the usage API"}];
const meters = page => page.$$eval("#foot .um", bs => bs.map(b => ({id: b.dataset.id, state: b.dataset.state, text: b.textContent, title: b.title,
  tone: b.querySelector(".ubar")?.dataset.tone || "", err: !!b.querySelector(".uerr"), stale: b.classList.contains("stale")})));

test("usage meters: one per agent at the footer's left, in config order: normal, near the limit, n/a and failed", async page => {
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), USAGE());
  const m = await meters(page);
  assert.deepEqual(m.map(x => x.id), ["claude", "codex", "opencode", "pi"]);
  assert.equal(await page.$eval("#foot", f => f.firstElementChild.className), "ums", "the meters lead the footer");
  assert.match(m[0].text, /^64%3d 7h$/); assert.equal(m[0].tone, "", "64%: the neutral bar");
  assert.match(m[0].title, /Claude \(Claude Max\)\nWeek: 64% used, resets in 3d 7h\nFable: 15% used/);
  assert.match(m[1].text, /^92%2h 1\dm$/, "the tightest window (5h), not the first"); assert.equal(m[1].tone, "near", "amber near the limit");
  assert.equal(m[2].text, "n/a"); assert.match(m[2].title, /no usage API/);
  assert.equal(m[3].text, "Refresh failed"); assert.match(m[3].title, /refresh failed\ncan't reach the usage API/);
  assert.equal(await page.$eval("#foot .um[data-id=codex] .ubar i", i => i.style.width), "92%");
  assert.equal(await page.$eval("#foot .um[data-id=claude] svg", s => s.dataset.icon), "claude");
  assert.equal(await page.$eval("#foot .um[data-id=codex] svg", s => s.dataset.icon), "openai");
});

test("usage meters: at the limit is red; a failed refresh keeps its last windows with a warning; old data says its age", async page => {
  const u = USAGE(); u[0].windows[0].pct = 100; u[1] = {...u[1], state: "failed", note: "rate limited: trying again in 5 minutes", at: NOW_S() - 1200};
  u[3] = {id: "pi", name: "Pi", state: "ok", at: NOW_S() - 7200, note: "from the last session instead", windows: [{label: "Week", pct: 10, resets: null}]};
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), u);
  const m = await meters(page);
  assert.equal(m[0].tone, "full"); assert.match(m[0].text, /^100%/);
  assert.ok(m[1].err, "the failed one shows the alert glyph"); assert.match(m[1].title, /Refresh failed: rate limited.*These are from 20m ago/s);
  assert.ok(m[3].stale); assert.match(m[3].title, /As of 2h ago \(from the last session instead\)/); assert.equal(m[3].text, "10%", "no reset time: no countdown");
  await page.click("#foot .um[data-id=codex]");
  assert.match(await page.textContent(".urow[data-id=codex] .unote"), /Refresh failed: rate limited.*From 20m ago/);
  assert.match(await page.textContent(".urow[data-id=pi] .unote"), /As of 2h ago: from the last session instead/);
});

test("usage meters: a window whose reset has passed reads — (no new reading) and stops being the tightest; Esc elsewhere isn't taken", async page => {
  const u = USAGE(); u[0] = {...u[0], state: "failed", note: "token expired", at: NOW_S() - 36000,
    windows: [{label: "5h", pct: 95, resets: NOW_S() - 30000}, {label: "Week", pct: 40, resets: NOW_S() + 86400}]};
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), u);
  const m = (await meters(page))[0];
  assert.match(m.text, /^40%1d 0h$/, "the week, not yesterday's 95% 5h"); assert.equal(m.tone, "");
  assert.match(m.title, /5h: reset since, no new reading\nWeek: 40% used, resets in 1d 0h/);
  await page.click("#foot .um[data-id=claude]");
  assert.deepEqual(await page.$$eval("#umpop .urow[data-id=claude] .uwin", ws => ws.map(w => [w.querySelector(".upct").textContent, w.querySelector(".ur").textContent])),
    [["—", "reset"], ["40%", "in 1d 0h"]]);
  await page.evaluate(() => { const i = document.createElement("input"); i.id = "elsewhere"; document.body.append(i); i.focus() });
  const taken = await page.evaluate(() => { const e = new KeyboardEvent("keydown", {key: "Escape", bubbles: true, cancelable: true}); document.activeElement.dispatchEvent(e); return e.defaultPrevented });
  assert.equal(taken, false, "Esc in another field is that field's"); assert.ok(await page.$("#umpop"), "and the popover stays");
  await page.focus("#umpop [data-view=compact]"); await page.keyboard.press("Escape");
  assert.equal(await page.$("#umpop"), null); assert.equal(await page.evaluate(() => document.activeElement?.dataset.id), "claude", "focus back on its meter");
  await page.click("#foot .um[data-id=codex]"); await page.click("#umpop .urow[data-id=codex] b"); await page.keyboard.press("Escape");
  assert.equal(await page.$("#umpop"), null, "Esc after a click on its text still closes it");
  const v = USAGE(); v[0] = {...v[0], windows: [{label: "5h", pct: 95, resets: NOW_S() - 60}]}; v[1] = {...v[1], windows: [{label: "5h", pct: 40, resets: null}]};
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), v);
  const mm = await meters(page);
  assert.equal(mm[0].text, "—", "every window reset since: a dash, not n/a"); assert.equal(mm[1].text, "40%", "no reset time: the %, no countdown");
  await page.click("#foot .um[data-id=codex]");
  assert.deepEqual(await page.$$eval("#umpop .urow[data-id=codex] .uwin", ws => ws.map(w => [w.querySelector(".upct").textContent, w.querySelector(".ur").textContent])), [["40%", ""]]);
  await page.keyboard.press("Escape");
  v[0] = {...v[0], state: "failed", note: "timeout", at: NOW_S() - 7200};
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), v);
  const f0 = (await meters(page))[0];
  assert.equal(f0.text, "—"); assert.ok(f0.err); assert.match(f0.title, /5h: reset since, no new reading\nRefresh failed: timeout\. These are from 2h ago/);
  await page.click("#foot .um[data-id=claude]"); await page.click("#umpop .ut"); await page.evaluate(() => footer());
  assert.notEqual(await page.evaluate(() => document.activeElement?.dataset.view), "detailed", "a redraw doesn't pull focus onto a tab");
  assert.match(await page.textContent("#umpop .urow[data-id=claude] .unote"), /Refresh failed: timeout\. From 2h ago\./);
  // Compact mode preserves failed-refresh alert icon and "reset" label; <1m avoids "Resets in now"; missing/zero `at` avoids "From ."
  await page.click("#umpop .useg [data-view=compact]");
  assert.ok(await page.$("#umpop .urow.c[data-id=claude] .uerr"), "compact mode shows alert icon on failed refresh with kept windows");
  assert.equal(await page.textContent("#umpop .urow.c[data-id=claude] .ur"), "reset", "compact mode shows reset when all windows reset");
  await page.keyboard.press("Escape");
  const w = USAGE(); w[0] = {...w[0], state: "failed", note: "timeout", at: 0, windows: [{label: "5h", pct: 50, resets: NOW_S() + 30}]};
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), w);
  const fw = (await meters(page))[0];
  assert.equal(fw.text, "50%<1m");
  assert.ok(fw.title.includes("resets in <1m") && !fw.title.includes("in now") && !fw.title.includes("From .") && !fw.title.includes("from ."), fw.title);
  await page.click("#foot .um[data-id=claude]"); await page.click("#umpop .useg [data-view=detailed]");
  assert.equal(await page.textContent("#umpop .urow[data-id=claude] .urr"), "Resets in <1m");
  assert.equal(await page.textContent("#umpop .urow[data-id=claude] .unote"), "Refresh failed: timeout");
  // Fresh state:"ok" rollout fallback still surfaces its note; umTone rounds with umPctText; stale near-limit keeps amber in Compact
  const f = USAGE();
  f[0] = {...f[0], at: NOW_S(), note: "rate limited; from the last session instead", windows: [{label: "5h", pct: 99.6, resets: NOW_S() + 3600}]};
  f[1] = {...f[1], at: NOW_S() - 7200, windows: [{label: "5h", pct: 79.6, resets: NOW_S() + 3600}]};
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), f);
  const mf = await meters(page);
  assert.equal(mf[0].tone, "full", "99.6 rounds to 100% and uses full tone");
  assert.equal(mf[1].tone, "near", "79.6 rounds to 80% and uses near tone");
  assert.match(mf[0].title, /from the last session instead/, "fresh ok fallback note shows in tooltip");
  assert.match(await page.textContent("#umpop .urow[data-id=claude] .unote"), /from the last session instead/, "fresh ok fallback note shows in Detailed popover");
  await page.click("#umpop .useg [data-view=compact]");
  assert.equal(await page.$eval("#umpop .urow.c[data-id=codex] .upct", e => getComputedStyle(e).color),
    await page.$eval("#foot .um[data-id=codex] .upct", e => getComputedStyle(e).color), "stale near-limit compact row keeps amber color matching footer");
});

test("usage popover: Detailed lists every window with its reset, Compact one line each; it remembers, Escape and a click outside close it", async page => {
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), USAGE());
  assert.equal(await page.$("#umpop"), null);
  await page.click("#foot .um[data-id=claude]");
  assert.equal(await page.getAttribute("#foot .um[data-id=claude]", "aria-expanded"), "true");
  assert.equal(await page.getAttribute("#foot .um[data-id=codex]", "aria-expanded"), "false", "only the meter it opened from");
  assert.deepEqual(await page.$$eval("#umpop .urow", rs => rs.map(r => r.dataset.id)), ["claude", "codex", "opencode", "pi"]);
  const wins = await page.$$eval("#umpop .urow[data-id=claude] .uwin", ws => ws.map(w => [w.querySelector(".ul").textContent, w.querySelector(".upct").textContent, w.querySelector(".ur").textContent]));
  assert.deepEqual(wins, [["Week", "64%", "in 3d 7h"], ["Fable", "15%", "in 3d 0h"]]);
  assert.equal(await page.textContent("#umpop .urow[data-id=claude] .uplan"), "Claude Max");
  assert.match(await page.textContent("#umpop .urow[data-id=codex] .urr"), /^Resets in 2h 1\dm$/);
  assert.equal(await page.$$eval("#umpop .urow[data-id=opencode] .uwin", w => w.length), 0);
  assert.match(await page.textContent("#umpop .urow[data-id=opencode] .unote"), /^n\/a: OpenCode publishes no usage API/);
  assert.match(await page.textContent("#umpop .urow[data-id=pi] .unote"), /^Refresh failed: can't reach the usage API$/);
  const box = await page.$eval("#umpop", p => { const r = p.getBoundingClientRect(), f = document.getElementById("foot").getBoundingClientRect(); return {bottom: r.bottom, foot: f.top, left: r.left} });
  assert.ok(box.bottom <= box.foot, "above the footer"); assert.ok(box.left >= 8);
  await page.click("#umpop .useg [data-view=compact]");
  assert.equal(await page.$$eval("#umpop .urow.c", r => r.length), 4);
  assert.deepEqual(await page.$$eval("#umpop .urow.c", rs => rs.map(r => r.querySelector(".upct")?.textContent || r.querySelector(".uw").textContent)), ["64%", "92%", "n/a", "Refresh failed"]);
  assert.equal(await page.evaluate(() => localStorage.getItem("usageView")), "compact");
  assert.equal(await page.$eval("#umpop .urow.c[data-id=codex] .upct", e => getComputedStyle(e).color),
    await page.$eval("#foot .um[data-id=codex] .upct", e => getComputedStyle(e).color), "compact rows take the amber too");
  await page.focus("#umpop [data-view=detailed]");
  await page.evaluate(() => mc.update(JSON.parse(JSON.stringify(ui.raw))));
  assert.equal(await page.evaluate(() => document.activeElement?.dataset.view), "detailed", "an unchanged update keeps the focus in the popover");
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), USAGE());  // a fresh snapshot redraws it in place
  assert.equal(await page.$$eval("#umpop .urow.c", r => r.length), 4);
  await page.keyboard.press("Escape"); assert.equal(await page.$("#umpop"), null);
  assert.equal(await page.getAttribute("#foot .um[data-id=claude]", "aria-expanded"), "false");
  await page.click("#foot .um[data-id=pi]"); await page.mouse.click(600, 300); assert.equal(await page.$("#umpop"), null, "a click outside closes it");
  // 1m countdown, focus preserved across footer rebuild, malformed windows guarded, non-identifier id Esc dismissal, narrow viewport clamp
  const edge = [
    {id: 'a"b]c', name: "Odd", state: "ok", at: NOW_S(), windows: [null, {label: "5h", pct: NaN, resets: NOW_S() + 65}, {label: "Week", pct: 120, resets: NOW_S() + 65}]},
    ...USAGE().slice(0, 2),
  ];
  await page.evaluate(u => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: u}), edge);
  const me = await meters(page);
  assert.equal(me[0].text, "100%1m", "clamps >100%, skips null/NaN windows, and formats 1m countdown");
  await page.evaluate(() => document.querySelector("#foot .um").focus());
  await page.evaluate(() => footer());
  assert.equal(await page.evaluate(() => document.activeElement?.dataset.id), 'a"b]c', "footer rebuild keeps focus on the focused meter");
  await page.evaluate(() => document.querySelector("#foot .um").click());
  await page.keyboard.press("Escape");
  assert.equal(await page.$("#umpop"), null, "Esc closes popover even when id has quotes/brackets");
  assert.equal(await page.evaluate(() => document.activeElement?.dataset.id), 'a"b]c', "Esc restores focus to meter with special id");
  await page.setViewportSize({width: 300, height: 700});
  await page.evaluate(() => footer());
  await page.evaluate(() => document.querySelector("#foot .um").click());
  const narrow = await page.$eval("#umpop", p => { const r = p.getBoundingClientRect(); return {left: r.left, right: r.right, vw: innerWidth} });
  assert.ok(narrow.left >= 8 && narrow.right <= narrow.vw - 7, `popover stays within narrow viewport: ${JSON.stringify(narrow)}`);
  await page.keyboard.press("Escape");
  await page.setViewportSize({width: 1200, height: 800});
  await page.evaluate(() => mc.update({ts: Date.now() / 1000, scanned: 1, pucks: [], usage: []}));
  assert.equal(await page.$("#foot .ums"), null, "no agent reports: no meters");
});

// ---------- the agent log view, phases 2-3 (issue 191) ----------
const LOGNOW = Date.now();
function paneLog(){  // a Claude Code pane: two prompts, tool calls with output, two failing lines
  let i = 0; const L = (min, text) => ({id: `mc-s1.g0:${i++}`, role: "line", text, ts: LOGNOW - min * 60e3, kind: "pane"});
  return [
    L(22, "> Fix the flaky retry test"),
    L(21, "⏺ Bash(pytest -q -k retry)"), ...Array.from({length: 34}, (_, k) => L(20.9, `  case_${k} PASSED`)),
    L(20, "⏺ The test waits on a real clock."),
    L(14, "> CI still fails on the retry path"),
    L(13, "⏺ Read(scripts/queue.py)"), L(13, "  ⎿  Read 212 lines"),
    L(3, "⏺ Bash(pytest -q tests/test_queue.py)"), L(3, "  ⎿  ....F...."),
    L(3, "     FAILED tests/test_queue.py::test_retry_after_churn - AssertionError: 64 != 1"), L(3, "     1 failed, 8 passed in 2.41s"),
    L(1, "⏺ Bash(pytest -q tests/test_queue.py)"), L(1, "  ⎿  9 passed in 2.10s"),
  ];
}
async function openLog(page, entries = paneLog(), extra = {}){
  await page.evaluate(() => openOut("plan-b"));
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, source: "pane", last_output_ts: Date.now() - 60e3, entries, ...extra});
}
const visibleTexts = page => page.$$eval("#aout-list > [data-id]:not([hidden]) .aotx", es => es.map(e => e.textContent));

test("log view: tool calls and prompts fold, a long finished call starts folded, Fold all folds every section", async page => {
  await openLog(page);
  assert.equal(await page.locator("#aout-list > .sec").count(), 4, "each tool call is a section header");
  assert.equal(await page.locator("#aout-list > .turn").count(), 2, "each prompt starts a turn");
  assert.match(await page.textContent("#aout-list > .sec >> nth=0"), /34 lines/, "a header says how much it holds");
  assert.match(await page.textContent("#aout-list > .turn >> nth=1"), /3 tool calls/);
  assert.equal(await page.locator("#aout-list > .in:not([hidden])").count(), 1 + 3 + 1, "the 34-line call folded itself; the rest stay open");
  await page.click("#aout-list > .sec >> nth=0 >> .aofold");
  assert.equal(await page.locator("#aout-list > .in:not([hidden])").count(), 34 + 5, "unfolded by hand");
  await page.click("#aout-list > .sec >> nth=0 >> .aofold");
  await page.click("#aout-list > .turn >> nth=1 >> .aofold");
  assert.deepEqual(await visibleTexts(page), ["> Fix the flaky retry test", "⏺ Bash(pytest -q -k retry)", "⏺ The test waits on a real clock.", "> CI still fails on the retry path"], "a folded turn hides its calls and their output");
  assert.equal(await page.getAttribute("#aout-list > .turn >> nth=1 >> .aofold", "aria-expanded"), "false");
  await page.focus("#aout-list > .sec >> nth=1 >> .aofold"); await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(() => document.activeElement.classList.contains("aofold")), true, "a fold toggled from the keyboard keeps it");
  await page.keyboard.press("Enter");
  await page.focus("#aout-log"); await page.keyboard.press("Home"); await page.keyboard.press("]"); await page.keyboard.press("]");
  await page.keyboard.press("f");
  assert.equal(await page.getAttribute("#aout-list > .sec >> nth=1 >> .aofold", "aria-expanded"), "false", "F folds the call you jumped to");
  await page.keyboard.press("f");
  assert.equal(await page.getAttribute("#aout-list > .sec >> nth=1 >> .aofold", "aria-expanded"), "true", "and F again unfolds it");
  await page.focus("#aout-q");
  for (let k = 0; k < 12; k++) { await page.keyboard.press("Tab"); assert.equal(await page.evaluate(() => document.activeElement.classList.contains("aofold")), false, "Tab skips the fold buttons") }
  await page.click("#aout-foldall");
  assert.equal(await page.locator("#aout-list > [data-id]:not([hidden])").count(), 2, "Fold all leaves the prompts");
  assert.match(await page.textContent("#aout-foldall"), /Unfold all/);
  await page.click("#aout-foldall");
  assert.equal(await page.locator("#aout-list > [data-id][hidden]").count(), 0, "Unfold all shows everything");
  // a refresh appends without undoing any of it
  await page.click("#aout-list > .sec >> nth=1 >> .aofold");
  const e = paneLog(); e.push({id: "mc-s1.g0:99", role: "line", text: "  ⎿  pushed", ts: Date.now(), kind: "pane"});
  await nextPoll(page, 2);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, source: "pane", last_output_ts: Date.now(), entries: e});
  assert.equal(await page.locator("#aout-list > .sec >> nth=1 >> .aofold").getAttribute("aria-expanded"), "false", "a fold survives a refresh");
  assert.match(await page.textContent("#aout-list > .sec >> nth=3"), /2 lines/, "new output joins its call");
});

test("log view: search marks every match, steps through them, finds them inside folds; Esc clears, then closes", async page => {
  await openLog(page);
  await page.click("#aout-foldall");
  await page.click("#aout-q"); await page.keyboard.type("PASSED");
  await page.waitForFunction(() => /of 36/.test(document.getElementById("aout-qn").textContent));
  assert.equal(await page.textContent("#aout-qn"), "36 of 36", "the newest match first (case doesn't matter: \"9 passed\" too)");
  assert.equal(await page.locator("#aout-list mark").count(), 36);
  assert.equal(await page.locator("#aout-list mark.cur").count(), 1);
  assert.equal(await page.locator("#aout-list > [data-id]:not([hidden]) mark").count(), 36, "matches show even in a folded call");
  await page.keyboard.press("Enter");
  assert.equal(await page.textContent("#aout-qn"), "1 of 36", "Return wraps to the first");
  await page.keyboard.press("Shift+Enter");
  assert.equal(await page.textContent("#aout-qn"), "36 of 36");
  await page.click("#aout-qprev");
  assert.equal(await page.textContent("#aout-qn"), "35 of 36");
  assert.equal(await page.$eval("#aout-list mark.cur", m => m.textContent), "passed");
  // new output that matches joins the count; the current match stays the current one
  const more = paneLog(); more.push({id: "mc-s1.g0:99", role: "line", text: "  ⎿  2 passed", ts: Date.now(), kind: "pane"});
  await nextPoll(page, 2);
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, source: "pane", last_output_ts: Date.now(), entries: more});
  assert.equal(await page.textContent("#aout-qn"), "35 of 37", "a refresh's matches are counted, the current one kept");
  assert.equal(await page.locator("#aout-list mark").count(), 37);
  await page.click("#aout-filter [data-f=err]");
  assert.equal(await page.textContent("#aout-qn"), "1 of 1", "under the Errors filter, only the error that matches");
  await page.click("#aout-filter [data-f=all]");
  await page.fill("#aout-q", "nothing like it"); await page.waitForFunction(() => /No matches/.test(document.getElementById("aout-qn").textContent));
  await page.focus("#aout-q");
  await page.keyboard.press("Escape");
  assert.equal(await page.inputValue("#aout-q"), "", "Esc clears the search first");
  assert.equal(await page.locator("#aout-list mark").count(), 0);
  assert.ok(await page.isVisible("#aout"), "and keeps the view open");
  await page.keyboard.press("Escape"); await page.keyboard.press("Escape");
  assert.equal(await page.$("#aout"), null, "then Esc closes it");
});

test("log view: the kind filter counts and shows only that kind, with an empty state; Jump to and its keys", async page => {
  await openLog(page);
  assert.deepEqual(await page.$$eval("#aout-filter button", bs => bs.map(b => b.textContent)), ["All46", "Tool calls4", "Errors2", "Prompts2"]);
  await page.click("#aout-filter [data-f=err]");
  assert.deepEqual(await visibleTexts(page), ["     FAILED tests/test_queue.py::test_retry_after_churn - AssertionError: 64 != 1", "     1 failed, 8 passed in 2.41s"]);
  assert.equal(await page.getAttribute("#aout-filter [data-f=err]", "aria-pressed"), "true");
  assert.equal(await page.isDisabled("#aout-foldall"), true, "folding means nothing in a filtered view");
  await page.click("#aout-filter [data-f=prompt]");
  assert.equal((await visibleTexts(page)).length, 2);
  // no errors at all: the empty state, and its way back
  const clean = paneLog().filter(e => !/FAILED|failed/.test(e.text));
  await page.evaluate(() => closeOut(false));
  await openLog(page, clean);
  await page.click("#aout-filter [data-f=err]");
  assert.ok(await page.isVisible("#aout-empty"));
  assert.match(await page.textContent("#aout-empty"), /No errors in what's loaded/);
  await page.click("#aout-empty button");
  assert.equal(await page.isVisible("#aout-empty"), false);
  assert.equal(await page.getAttribute("#aout-filter [data-f=all]", "aria-pressed"), "true");
  // Jump to
  await page.evaluate(() => closeOut(false));
  await openLog(page);
  await page.$eval("#aout-log", e => { e.scrollTop = 0; e.dispatchEvent(new Event("scroll")) });
  await page.click("#aout-jumpbtn");
  assert.equal(await page.getAttribute("#aout-jumpbtn", "aria-expanded"), "true");
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("role")), "menuitem", "the menu takes the keyboard");
  await page.click("#aout-jump [role=menuitem]:has-text('Next error')");
  assert.equal(await page.$("#aout-jump"), null);
  assert.match(await page.textContent("#aout-list > .aoat"), /FAILED/, "it lands on the first error");
  await page.focus("#aout-log");
  await page.keyboard.press("e");
  assert.match(await page.textContent("#aout-list > .aoat"), /1 failed/, "E: the next one");
  await page.keyboard.press("e");
  assert.match(await page.textContent("#aout-note"), /No later error/, "and says when there are no more");
  await page.keyboard.press("Shift+E");
  assert.match(await page.textContent("#aout-list > .aoat"), /FAILED/, "⇧E goes back");
  await page.keyboard.press("[");
  assert.match(await page.textContent("#aout-list > .aoat"), /Bash\(pytest -q tests/, "[ the call before");
  // from a filtered view, a jump starts where you were, not at the bottom
  await page.$eval("#aout-log", e => { e.scrollTop = 0; e.dispatchEvent(new Event("scroll")) });
  await page.evaluate(() => document.querySelectorAll(".aoat").forEach(x => x.classList.remove("aoat")));
  await page.click("#aout-filter [data-f=prompt]"); await page.$eval("#aout-log", e => { e.scrollTop = 0; e.dispatchEvent(new Event("scroll")) });
  await page.focus("#aout-log"); await page.keyboard.press("e");
  assert.match(await page.textContent("#aout-list > .aoat"), /FAILED/, "E from the Prompts filter finds the first error");
  assert.equal(await page.getAttribute("#aout-filter [data-f=all]", "aria-pressed"), "true");
  await page.click("#aout-jumpbtn"); await page.keyboard.press("Escape");
  assert.equal(await page.$("#aout-jump"), null, "Esc closes the menu, not the view");
  assert.ok(await page.isVisible("#aout"));
});

test("log view: a working agent quiet for 10 min gets a stuck banner; a repeating block, a looping one; dismissed, it stays away", async page => {
  const quiet = paneLog().map(e => ({...e, ts: e.ts - 15 * 60e3}));
  await openLog(page, quiet, {last_output_ts: Date.now() - 37 * 60e3});
  await page.waitForSelector("#aout-warn:not([hidden])");
  assert.match(await page.textContent("#aout-warn"), /^No output for 1\dm while the agent is working/);
  assert.ok(await page.$("#aout-warn button:has-text('Open in Basement')"), "the way to look at it");
  await page.click("#aout-warn [aria-label=Dismiss]");
  assert.equal(await page.isVisible("#aout-warn"), false);
  await page.evaluate(() => outWarn());
  assert.equal(await page.isVisible("#aout-warn"), false, "dismissed: stays away while nothing changes");
  // looping: the same two lines, nine times (digits don't count)
  await page.evaluate(() => closeOut(false));
  const loop = paneLog(); let n = 100;
  for (let r = 0; r < 9; r++) loop.push({id: `mc-s1.g0:${n++}`, role: "line", text: "⏺ Bash(curl localhost:8080)", ts: Date.now() - (9 - r) * 40e3, kind: "pane"},
    {id: `mc-s1.g0:${n++}`, role: "line", text: `  ⎿  connection refused after ${r + 1}.0s`, ts: Date.now() - (9 - r) * 40e3 + 1000, kind: "pane"});
  await openLog(page, loop, {last_output_ts: Date.now()});
  await page.waitForSelector("#aout-warn:not([hidden])");
  assert.match(await page.textContent("#aout-warn"), /The same 2 lines came back 9 times in 5m\. The agent may be looping\./);
  await page.$eval("#aout-log", e => { e.scrollTop = 0 });
  await page.click("#aout-warn button:has-text('Show the repeat')");
  assert.ok(await page.$eval("#aout-list > [data-id='mc-s1.g0:100']", e => { const r = e.getBoundingClientRect(), l = document.getElementById("aout-log").getBoundingClientRect(); return r.top >= l.top - 1 && r.top < l.bottom }), "it scrolls to where the repeat starts");
  // working, and not a word since it started 20 min ago
  await page.evaluate(() => closeOut(false));
  await page.evaluate(s => mc.update({...s, pucks: s.pucks.map(p => ({...p, agent: {...p.agent, since: Date.now() - 20 * 60e3}}))}), SNAP);
  await page.evaluate(() => openOut("plan-b"));
  await page.evaluate(() => mc.transcript({name: "plan-b", before: null, more: null, source: "pane", entries: []}));
  await page.waitForSelector("#aout-warn:not([hidden])");
  assert.match(await page.textContent("#aout-warn"), /No output in the 20m since it started/);
  // restarted a minute ago after an older session's output: not stuck yet
  await page.evaluate(() => closeOut(false));
  await page.evaluate(s => mc.update({...s, pucks: s.pucks.map(p => ({...p, agent: {...p.agent, since: Date.now() - 60e3}}))}), SNAP);
  await openLog(page, quiet);
  assert.equal(await page.isVisible("#aout-warn"), false, "silence counts from this run's start");
  // an agent that isn't working is never stuck
  await page.evaluate(() => closeOut(false));
  await page.evaluate(s => mc.update({...s, pucks: s.pucks.map(p => ({...p, agent: {...p.agent, state: "idle"}}))}), SNAP);
  await openLog(page, quiet);
  assert.equal(await page.isVisible("#aout-warn"), false);
});

test("log view: after L, E still finds the next error; Show the repeat opens a folded turn; ⌘F stays out of the share sheet", async page => {
  await page.evaluate(lk => localStorage.setItem("gl_last_looked", JSON.stringify({"plan-b": lk})), Date.now() - 10 * 60e3);
  const loop = paneLog(); let n = 300;
  for (let r = 0; r < 9; r++) loop.push({id: `mc-s1.g0:${n++}`, role: "line", text: "⏺ Bash(curl localhost:8080)", ts: Date.now() - (9 - r) * 40e3, kind: "pane"},
    {id: `mc-s1.g0:${n++}`, role: "line", text: "  ⎿  connection refused", ts: Date.now() - (9 - r) * 40e3 + 1000, kind: "pane"});
  await openLog(page, loop, {last_output_ts: Date.now()});
  assert.ok(await page.$("#aout-looked"), "a divider to jump to");
  await page.focus("#aout-log"); await page.keyboard.press("l");
  await page.keyboard.press("e");
  assert.match(await page.textContent("#aout-list > .aoat"), /FAILED/, "the divider isn't a row to count from");
  await page.click("#aout-foldall");
  await page.click("#aout-warn button:has-text('Show the repeat')");
  assert.equal(await page.$eval("#aout-list > [data-id='mc-s1.g0:300']", e => e.hidden), false, "its turn unfolds");
  await page.click("#aout-share-btn");
  await page.keyboard.press(process.platform === "darwin" ? "Meta+f" : "Control+f");
  assert.notEqual(await page.evaluate(() => document.activeElement.id), "aout-q", "the search behind the sheet stays put");
  assert.match((await posts(page, "logshare")).pop().text, /^\S+ {2}⏺ Bash/m);
});

test("log view: a transcript's shared text labels the agent's replies", async page => {
  await page.evaluate(() => openOut("plan-b"));
  await page.evaluate(r => mc.transcript(r), {name: "plan-b", before: null, more: null, entries: [E(1, "user", "go"), E(2, "assistant", "done it")], recent: []});
  await page.click("#aout-share-btn");
  const t = (await posts(page, "logshare")).pop().text;
  assert.match(t, /Prompt: go/); assert.match(t, /Claude: done it/);
});

test("log view: parametrized tests aren't a loop; Previous jumps skip folded rows; a session's sections survive another one's lines in between", async page => {
  const tests = paneLog(); let n = 200;
  for (let r = 0; r < 12; r++) tests.push({id: `mc-s1.g0:${n++}`, role: "line", text: `  test_retry[${r}] PASSED`, ts: Date.now() - 1000, kind: "pane"});
  await openLog(page, tests, {last_output_ts: Date.now()});
  assert.equal(await page.isVisible("#aout-warn"), false, "test_x[0], test_x[1]… are different lines");
  // ⇧E from the bottom: the visible FAILED lines, never a hidden row, and nothing else unfolds on the way
  await page.click("#aout-list > .sec >> nth=0 >> .aofold");  // open the long call...
  await page.click("#aout-list > .sec >> nth=0 >> .aofold");  // ...and fold it again by hand
  const folded = await page.evaluate(() => [...ui.out.folds]);
  await page.focus("#aout-log"); await page.keyboard.press("End"); await page.$eval("#aout-log", e => { e.scrollTop = e.scrollHeight; e.dispatchEvent(new Event("scroll")) });
  await page.keyboard.press("Shift+E");
  assert.match(await page.textContent("#aout-list > .aoat"), /1 failed/);
  assert.equal(await page.$eval("#aout-list > .aoat", e => e.hidden), false);
  await page.keyboard.press("Shift+E");
  assert.match(await page.textContent("#aout-list > .aoat"), /FAILED/);
  assert.deepEqual(await page.evaluate(() => [...ui.out.folds]), folded, "no fold opened that it didn't land in");
  // two sessions writing at once
  await page.evaluate(() => closeOut(false));
  const A = (k, t) => ({id: `mc-a.g0:${k}`, role: "line", text: t, ts: Date.now(), kind: "pane"}), B = (k, t) => ({id: `mc-b.g0:${k}`, role: "line", text: t, ts: Date.now(), kind: "pane"});
  await openLog(page, [A(0, "> go"), A(1, "⏺ Bash(make test)"), B(0, "⏺ Read(x)"), A(2, "  ⎿  ok 1"), B(1, "  ⎿  Read 3 lines"), A(3, "  ⎿  ok 2")]);
  assert.match(await page.textContent("#aout-list > [data-id='mc-a.g0:1']"), /2 lines/, "session A's call keeps its lines");
  assert.match(await page.textContent("#aout-list > [data-id='mc-a.g0:0']"), /1 tool call/);
});

const EXTRAS = {name: "plan-b", usage: {in_tokens: 1820400, out_tokens: 41200, cache_read: 1700000, sessions: 2, cost_cents: 142.3, models: ["claude-opus-5-5"], estimated: true},
  files: {mode: "branch", base: "main", files: [["scripts/queue.py", "3", "1"], ["tests/{old_test.py => new_test.py}", "2", "0"], ["logo.png", "-", "-"]], status: ["M", "R", "A"], cut: false,
    patch: "diff --git a/scripts/queue.py b/scripts/queue.py\nindex 1..2\n--- a/scripts/queue.py\n+++ b/scripts/queue.py\n@@ -1,3 +1,5 @@\n keep\n-old\n+new\n+newer\n+newest\ndiff --git a/tests/new_test.py b/tests/new_test.py\nnew file\n@@ -0,0 +1,2 @@\n+a\n+b\n"}};

test("log view: the sessions' cost and tokens in the strip; Files shows what the branch changes, with each file's diff", async page => {
  await openLog(page);
  assert.ok((await posts(page, "logextras")).some(m => m.name === "plan-b"), "it asks for the extras on open");
  assert.equal(await page.$("#aout-strip .aos-cost"), null, "nothing until they come");
  await page.evaluate(r => mc.logextras(r), EXTRAS);
  assert.equal(await page.textContent("#aout-strip .aos-cost"), "≈$1.42 · 1.86M tokens");
  assert.match(await page.getAttribute("#aout-strip .aos-cost", "title"), /2 sessions · claude-opus-5-5\nEstimated from list prices/);
  await page.evaluate(r => mc.logextras({...r, usage: {...r.usage, cost_cents: null, estimated: false}}), EXTRAS);
  assert.equal(await page.textContent("#aout-strip .aos-cost"), "1.86M tokens", "no cost reported: tokens only, never a made-up one");
  assert.equal(await page.textContent("#aout-files-n"), "3");
  await page.click("#aout-files-btn");
  assert.equal(await page.getAttribute("#aout-files-btn", "aria-pressed"), "true");
  assert.deepEqual(await page.$$eval("#aout-files .aofrow", rs => rs.map(r => r.textContent)), ["Mscripts/queue.py+3 −1", "Rtests/{old_test.py => new_test.py}+2 ", "Alogo.pngbinary"]);
  assert.deepEqual(await page.$$eval("#aout-files .aodiff > *", ds => ds.map(d => d.className + ":" + d.textContent)),
    ["dfn:scripts/queue.py", "dh:@@ -1,3 +1,5 @@", "dl: keep", "dl r:-old", "dl a:+new", "dl a:+newer", "dl a:+newest"]);
  await page.click("#aout-files .aofrow:has-text('new_test.py')");
  assert.deepEqual(await page.$$eval("#aout-files .aodiff .dl", ds => ds.map(d => d.textContent)), ["+a", "+b"]);
  await page.evaluate(r => mc.logextras({...r, files: {...r.files, patch: r.files.patch.replace("+b", "+c")}}), EXTRAS);
  assert.deepEqual(await page.$$eval("#aout-files .aodiff .dl", ds => ds.map(d => d.textContent)), ["+a", "+c"], "a same-length edit shows");
  assert.match(await page.textContent("#aout-files .aoff"), /since this branch left main/);
  await page.evaluate(r => mc.logextras({...r, files: {error: "not a git checkout"}}), EXTRAS);
  assert.match(await page.textContent("#aout-files"), /Couldn't load the files/);
  const before = (await posts(page, "logextras")).length;
  await page.click("#aout-files button:has-text('Retry')");
  assert.equal((await posts(page, "logextras")).length, before + 1, "Retry asks again");
  assert.ok(await page.evaluate(() => document.getElementById("aout").contains(document.activeElement) && document.activeElement !== document.body), "the keyboard stays in the view");
  await page.keyboard.press("Escape");
  assert.equal(await page.$("#aout"), null, "and Esc still closes it");
  await openLog(page); await page.evaluate(r => mc.logextras(r), EXTRAS); await page.click("#aout-files-btn");
  await page.evaluate(() => mc.logextras({name: "plan-b", error: "the host didn't answer (exit 255)"}));  // the whole request failed
  assert.match(await page.textContent("#aout-files"), /Couldn't load the files.*the host didn't answer/);
  assert.equal(await page.$("#aout-strip .aos-cost"), null, "and no cost from a failed answer");
  await page.evaluate(r => mc.logextras({...r, files: {...r.files, files: [], status: [], patch: ""}}), EXTRAS);
  assert.match(await page.textContent("#aout-files"), /No files changed yet/);
  await page.click("#aout-files [aria-label='Close files']");
  assert.equal(await page.isVisible("#aout-files"), false);
  assert.equal(await page.evaluate(() => document.activeElement.id), "aout-files-btn");
  await page.evaluate(r => mc.logextras({...r, name: "plan-a"}), EXTRAS);  // another workspace's answer is ignored
  assert.equal(await page.textContent("#aout-files-n"), "0");
});

test("log view: Share log… sends nothing until the host has taken private details out; a failed check shares nothing", async page => {
  await openLog(page);
  await page.click("#aout-share-btn");
  assert.ok(await page.isVisible("#aout-share [role=dialog]"));
  assert.equal(await page.isDisabled("#aout-share-copy"), true, "nothing to copy before the check");
  assert.equal(await page.isDisabled("#aout-share-save"), true);
  const ask = (await posts(page, "logshare")).pop();
  assert.equal(ask.name, "plan-b");
  assert.match(ask.text, /^\d\d:\d\d:\d\d {2}> Fix the flaky retry test$/m, "times and lines, oldest first");
  assert.equal(ask.text.split("\n").length, 46);
  await page.evaluate(req => mc.logshared({name: "plan-b", req: req - 1, text: "stale", removed: 0}), ask.req);
  assert.equal(await page.isDisabled("#aout-share-copy"), true, "an answer to another ask is dropped");
  await page.evaluate(req => mc.logshared({name: "plan-b", req, text: "12:00:00  ls [path]", removed: 4}), ask.req);
  assert.match(await page.textContent("#aout-share"), /Private details are taken out: 4 removed/);
  assert.equal(await page.textContent("#aout-share .aopv"), "12:00:00  ls [path]", "the preview is the whole export");
  await page.click("#aout-share-copy");
  assert.deepEqual((await posts(page, "copyText")).pop(), {cmd: "copyText", text: "12:00:00  ls [path]"}, "Copy sends the redacted text only");
  assert.match(await page.textContent("#aout-share-copy"), /Copied/);
  await page.click("#aout-share-save");
  assert.deepEqual((await posts(page, "saveText")).pop(), {cmd: "saveText", text: "12:00:00  ls [path]", filename: `agent-log-${new Date().toISOString().slice(0, 10)}.txt`}, "named by the date, never the workspace");
  await page.evaluate(() => mc.saved({ok: false, error: "disk full"}));
  assert.match(await page.textContent("#aout-share"), /Couldn't save it: disk full/);
  await page.evaluate(() => mc.saved({ok: true}));
  assert.match(await page.textContent("#aout-share"), /Saved/);
  // another range asks again; an error is a dead end, not a leak
  await page.click("#aout-r-looked").catch(() => {});  // no last look in this boot: the option may be absent
  await page.evaluate(() => { ui.out.share.range = "all"; outShareAsk(); outShareRender() });
  await page.evaluate(() => mc.logshared({name: "plan-b", req: ui.out.share.req, error: "leak.re: bad pattern"}));
  assert.match(await page.textContent("#aout-share"), /Couldn't check it for private details/);
  assert.match(await page.textContent("#aout-share"), /So nothing is shared: leak\.re: bad pattern/);
  assert.equal(await page.isDisabled("#aout-share-copy"), true);
  assert.equal(await page.isDisabled("#aout-share-save"), true);
  await page.click("#aout-share-retry");
  assert.equal(await page.isDisabled("#aout-share-copy"), true, "retrying: still nothing to share");
  await page.keyboard.press("Escape");
  assert.equal(await page.$("#aout-share"), null, "Esc closes the sheet first");
  assert.ok(await page.isVisible("#aout"));
  assert.equal(await page.evaluate(() => document.activeElement.id), "aout-share-btn");
  // nothing in the range: nothing to copy (Copy would empty the clipboard)
  await page.click("#aout-share-btn");
  await page.evaluate(() => mc.logshared({name: "plan-b", req: ui.out.share.req, text: "", removed: 0}));
  assert.equal(await page.isDisabled("#aout-share-copy"), true); assert.equal(await page.isDisabled("#aout-share-save"), true);
  await page.keyboard.press("Escape");
  // a filtered view offers what it shows
  await page.click("#aout-filter [data-f=err]");
  await page.click("#aout-share-btn");
  assert.match(await page.textContent("#aout-share fieldset"), /What the filter shows: Errors2 lines/);
  await page.click("#aout-r-view");
  const asked = (await posts(page, "logshare")).length;
  await page.evaluate(() => mc.logshared({name: "plan-b", req: ui.out.share.req, text: "the whole log", removed: 0}));  // the answer for the range it had asked about
  assert.equal((await posts(page, "logshare")).length, asked + 1, "a range changed mid-check is asked again, not shown stale");
  assert.equal(await page.isDisabled("#aout-share-copy"), true);
  const v = (await posts(page, "logshare")).pop();
  assert.equal(v.text.split("\n").length, 2, "only the two errors");
});

test("Hover card: a hex answers at its outline, the next card swaps in fast, a refresh leaves it be, and the way to it is kept", async page => {
  const pucks = ["freeform", "ci_green", "review", "threads", "merge", "milestone", "ready", "verify", "mr_open"].map((step, i) => ({...puck("hx-" + i, "Goal " + i, "idle"), step}));  // each its own step: no stacks
  await page.setViewportSize({width: 1300, height: 900});
  await page.evaluate(s => { window.mc.update(s); ui.agentsFolded = true; render() }, {...SNAP, pucks});
  const geo = () => page.$$eval("#main .cell", cs => cs.map(c => { const r = c.querySelector(".hx").getBoundingClientRect(); return {n: c.dataset.name, x: r.left + r.width / 2, y: r.top + r.height / 2, R: r.width / 2} }));
  const cells = await geo();
  assert.ok(cells.length >= 7, "the hive draws the hexes");
  const owner = (x, y) => page.evaluate(([x, y]) => { const e = document.elementFromPoint(x, y), c = e && e.closest(".cell"); return c ? c.dataset.name : null }, [x, y]);
  for (const c of cells) {
    assert.equal(await owner(c.x, c.y), c.n, `${c.n}: its centre is its own`);
    assert.notEqual(await owner(c.x - c.R + 2, c.y - c.R + 2), c.n, `${c.n}: its square box's corner is not`);  // outside a pointy-top hexagon
  }
  const swaps = () => page.evaluate(() => window.__sw);
  await page.evaluate(() => { window.__sw = []; new MutationObserver(() => { const c = document.getElementById("hcard"); const n = c && c.dataset.name; if (window.__sw[window.__sw.length - 1]?.n !== n) window.__sw.push({n, t: performance.now()}) }).observe(document.body, {childList: true}) });
  const [a, b] = cells, far = cells.reduce((m, c) => Math.hypot(c.x - a.x, c.y - a.y) > Math.hypot(m.x - a.x, m.y - a.y) ? c : m);
  await page.mouse.move(a.x, a.y); const t0 = await page.evaluate(() => performance.now());
  await page.waitForTimeout(120);
  assert.equal(await page.$("#hcard"), null, "a hex crossed in passing doesn't open a card");
  await page.waitForSelector(`#hcard[data-name='${a.n}']`);
  assert.ok((await swaps())[0].t - t0 >= 200, "the first card waits for the pointer to settle");
  // warm: the next hex's card replaces it at once (a hex nowhere near the way to the card)
  const t1 = await page.evaluate(() => performance.now());
  await page.mouse.move(far.x, far.y, {steps: 4});
  await page.waitForSelector(`#hcard[data-name='${far.n}']`, {timeout: 1000});
  const sw = (await swaps()).filter(s => s.t > t1);
  assert.deepEqual(sw.map(s => s.n), [far.n], "one card changing, never none between");
  assert.ok(sw[0].t - t1 < 200, `the next card comes quicker than the first (${Math.round(sw[0].t - t1)}ms)`);
  assert.equal(await page.locator("#hcard").count(), 1);
  // a refresh redraws the hive under a still pointer: the card stays the same card
  await page.evaluate(() => { window.__card = document.getElementById("hcard") });
  await page.evaluate(s => window.mc.update({...s, ts: 2}), {...SNAP, pucks});
  await page.mouse.move(far.x + 1, far.y);
  await page.waitForTimeout(350);
  assert.equal(await page.evaluate(() => document.getElementById("hcard") === window.__card), true, "not rebuilt, so no flicker");
  // on the way to the card, the hexes it crosses don't take it over
  await page.mouse.move(1, 1); await page.waitForSelector("#hcard", {state: "hidden"});
  await page.waitForTimeout(500);  // cold again
  let route = null;
  for (const c of await geo()) {
    await page.mouse.move(c.x, c.y); await page.waitForSelector(`#hcard[data-name='${c.n}']`);
    const r = await page.$eval("#hcard", e => { const b = e.getBoundingClientRect(); return {l: b.left, r: b.right, t: b.top, b: b.bottom} });
    for (const ty of [r.t + 12, r.b - 12]) {  // a corner of its near edge: a diagonal that may cross a neighbour
      const tx = r.l > c.x ? r.l + 12 : r.r - 12, pts = Array.from({length: 24}, (_, k) => [c.x + (tx - c.x) * (k + 1) / 24, c.y + (ty - c.y) * (k + 1) / 24]);
      const by = []; for (const [x, y] of pts) { const o = await owner(x, y); if (o && o !== c.n) by.push(o) }
      if (by.length) { route = {c, pts, by}; break }
    }
    if (route) break;
    await page.mouse.move(1, 1); await page.waitForSelector("#hcard", {state: "hidden"}); await page.waitForTimeout(500);
  }
  assert.ok(route, "some hex's way to its card crosses another hex");
  const t2 = await page.evaluate(() => performance.now());
  for (const [x, y] of route.pts) { await page.mouse.move(x, y); await page.waitForTimeout(12) }
  await page.waitForTimeout(300);
  assert.deepEqual((await swaps()).filter(s => s.t > t2).map(s => s.n), [], `crossing ${route.by[0]} on the way kept ${route.c.n}'s card`);
  assert.equal(await page.getAttribute("#hcard", "data-name"), route.c.n);
  // resting on a crossed hex, off the way, does swap
  const o = (await geo()).find(c => c.n === route.by[0]);
  await page.mouse.move(o.x, o.y, {steps: 3});
  if (await owner(o.x, o.y) === o.n) await page.waitForSelector(`#hcard[data-name='${o.n}']`, {timeout: 1000});
});

test("detached Agents window fills the full height (no 240px cap or empty box beneath), sorts by urgency, filters inline, and keeps output lines inside cards", async page => {
  const det = await page.context().browser().newPage({viewport: {width: 960, height: 860}});
  await det.addInitScript(() => {
    window.__posts = [];
    window.webkit = {messageHandlers: {mc: {postMessage: m => window.__posts.push(JSON.parse(JSON.stringify(m)))}}};
  });
  const errs = []; det.on("pageerror", e => errs.push(e.message));
  try {
    await det.goto(PAGE);
    const mk = (name, state, agentState, tool = "") => ({
      ...puck(name, "Task " + name, agentState),
      state,
      steps: ["mr_open", "ci_green", "merge"],
      step_index: 1,
      step: "ci_green",
      agent: {...puck(name, "Task " + name, agentState).agent, state: agentState, tool, input: tool ? "app/dashboard.html" : "", last: state === "crashed" ? "boom" : agentState === "waiting" ? "Ship it?" : "Running"},
    });
    const pucks = [
      mk("w-1", "working", "working", "Edit"),
      mk("w-2", "working", "working", "Bash"),
      mk("c-1", "crashed", "idle"),
      mk("n-1", "needs", "waiting"),
      mk("w-3", "working", "working", "Read"),
      mk("w-4", "working", "working", "Edit"),
      mk("w-5", "working", "working", "Bash"),
      mk("w-6", "working", "working", "Read"),
    ];
    await det.evaluate(s => { window.mc.update(s); window.mc.mode({only: "agents", away: []}) }, {...SNAP, pucks});
    assert.equal(await det.evaluate(() => document.body.classList.contains("nohex")), true);
    assert.equal(await det.evaluate(() => document.getElementById("bar").style.display), "none");
    // Urgency order: needs you first, then crashed, then working
    const order = await det.$$eval(".agents.detached .acard", es => es.map(e => e.dataset.name));
    assert.deepEqual(order.slice(0, 2), ["n-1", "c-1"], "needs-you then crashed come first: " + order.join(","));
    // Full-window height: .acards fills the space between the header bar and footer (no 240px cap / gray box beneath)
    const geom = await det.evaluate(() => {
      const s = document.querySelector(".agents.detached").getBoundingClientRect();
      const c = document.querySelector(".agents.detached .acards").getBoundingClientRect();
      const f = document.getElementById("foot").getBoundingClientRect();
      const maxH = getComputedStyle(document.querySelector(".agents.detached .acards")).maxHeight;
      const overflow = [...document.querySelectorAll(".acard")].filter(card => {
        const line = card.querySelector("button.aline");
        return line && line.getBoundingClientRect().right > card.getBoundingClientRect().right + 1;
      }).length;
      return {sBottom: Math.round(s.bottom), cBottom: Math.round(c.bottom), cHeight: Math.round(c.height), fTop: Math.round(f.top), maxH, overflow};
    });
    assert.equal(geom.maxH, "none", "detached cards grid must not cap max-height");
    assert.ok(geom.cHeight > 600, `detached cards grid should fill the tall window, got ${geom.cHeight}px`);
    assert.ok(Math.abs(geom.cBottom - geom.fTop) <= 2, `no gray void beneath cards (${geom.cBottom} vs footer ${geom.fTop})`);
    assert.equal(geom.overflow, 0, "button.aline must stay inside its card");
    // Detached empty state + Clear filter button when filtered to no matches
    await det.evaluate(() => { ui.q = "zzz-no-match"; renderMain() });
    assert.equal(await det.locator(".agents.detached .acard").count(), 0);
    assert.equal(await det.isVisible(".agents.detached .aempty"), true);
    assert.equal(await det.locator(".agents.detached .crash").count(), 0, "no stale crashed chip when filter matches nothing");
    await det.click(".agents.detached .aempty button");
    assert.equal(await det.locator(".agents.detached .acard").count(), 8, "Clear filter restores all cards");
    assert.deepEqual(errs, []);
  } finally { await det.close() }
});

test("issue 236: 5s stream updates do not tear down or flicker the answer sheet or workspace page answer box", async page => {
  const snap = JSON.parse(JSON.stringify(SNAP));
  snap.pucks[0].state = "needs";
  snap.pucks[0].agent = {type: "claude", state: "waiting", since: "2026-04-06T12:00:00Z", last: "Which approach should we take?"};
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000)}), snap);
  await page.evaluate(() => openAnswer("plan-a"));
  await page.evaluate(() => window.mc.asked({name: "plan-a", kind: "menu", question: "Which approach should we take?",
    options: [{key: "1", label: "Option A"}, {key: "2", label: "Option B"}]}));
  await page.evaluate(() => {
    window.__bg = document.querySelector(".sheetbg");
    window.__sh = document.querySelector(".sheetbg .sheet");
    window.__opt = document.querySelector(".sheetbg .aopts button");
  });
  await page.hover(".sheetbg .aopts button");
  // Quiet 5s tick (only ts changed) and a tick where a different workspace changed must both keep the sheet & option button untouched.
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000) + 5}), snap);
  const snapOther = JSON.parse(JSON.stringify(snap));
  snapOther.pucks[1].why = "updated elsewhere";
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000) + 10}), snapOther);
  const sheetCheck = await page.evaluate(() => ({
    bgSame: document.querySelector(".sheetbg") === window.__bg && window.__bg.isConnected,
    shSame: document.querySelector(".sheetbg .sheet") === window.__sh && window.__sh.isConnected,
    optSame: document.querySelector(".sheetbg .aopts button") === window.__opt && window.__opt.isConnected,
    optHover: window.__opt.matches(":hover"),
  }));
  assert.equal(sheetCheck.bgSame, true, "answer sheet backdrop should not be recreated on stream updates");
  assert.equal(sheetCheck.shSame, true, "answer sheet panel should not be recreated (replays hcin open animation)");
  assert.equal(sheetCheck.optSame, true, "answer option button should stay connected on stream updates");
  assert.equal(sheetCheck.optHover, true, "answer option button should keep :hover on stream updates");

  // Even when the sheet content updates (e.g. clicking Full message / chat / new asked payload), .sheet suppresses the hcin open animation.
  await page.evaluate(() => window.mc.asked({name: "plan-a", kind: "menu", question: "Updated question?",
    options: [{key: "1", label: "Option A"}, {key: "2", label: "Option B"}], chat: true}));
  assert.equal(await page.$eval(".sheetbg .sheet", e => getComputedStyle(e).animationName), "none", "updating sheet content must suppress hcin open animation");

  // Workspace page answer box must also stay connected across 5s stream ticks (including unrelated workspace updates).
  await page.evaluate(() => { closeAnswer(); openPage("plan-a") });
  await page.evaluate(() => { window.__pageOpt = document.querySelector("#main .aopts button") });
  await page.hover("#main .aopts button");
  const snapOther2 = JSON.parse(JSON.stringify(snapOther));
  snapOther2.pucks[1].why = "changed again";
  await page.evaluate(s => window.mc.update({...s, ts: Math.floor(Date.now() / 1000) + 15}), snapOther2);
  const pageCheck = await page.evaluate(() => ({
    same: document.querySelector("#main .aopts button") === window.__pageOpt && window.__pageOpt.isConnected,
    hover: window.__pageOpt.matches(":hover"),
  }));
  assert.equal(pageCheck.same, true, "workspace page answer box should not rebuild on unrelated 5s stream updates");
  assert.equal(pageCheck.hover, true, "workspace page answer option should keep :hover across 5s stream updates");
});

test("stuck stage escalates smart button to Ask Chief with Retry and Reset in Also", async page => {
  const stuckPuck = {
    ...puck("fix-ci", "Fix broken tests", "idle"),
    state: "needs",
    step: "ci_green",
    owner: "agent",
    advice: "A prior attempt failed without moving this stage (failed on this commit) — ask Chief or inspect the last run before retrying.",
    mr: {iid: 42, ref: "!42", state: "opened", title: "Fix broken tests", pipeline: "failed", target_branch: "main", sha: "aaaa1111"},
    moves: [{kind: "ci", repo: "widgets", prompt: "Fix CI\n\nPrior attempt failed on this stage.", jobs: ["unit"]}],
    attempts: {count: 1, same_sha: 1, stuck: true, step: "ci_green", reason: "Failed on this commit", summary: "pytest still failing", last: {ts: 1700000000, agent: "claude", source: "center", kind: "ci"}},
  };
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [stuckPuck]});
  await page.evaluate(() => { const p = puckOf("fix-ci"), el = document.querySelector('.cell[data-dock="fix-ci"]'); showCard(el, p) });
  await page.waitForSelector("#hcard .mv.stuck");
  assert.equal(await page.textContent("#hcard .mv.stuck .go b"), "Ask Chief");
  assert.match(await page.textContent("#hcard .mv.stuck .go small"), /Fix CI · Failed on this commit/);
  // Clicking Ask Chief posts askChief with workspace + failure context
  await page.click("#hcard .mv.stuck .go");
  const ask = (await posts(page, "askChief")).pop();
  assert.ok(ask, "clicking Ask Chief sends askChief message");
  assert.equal(ask.name, "fix-ci");
  assert.match(ask.shown, /fix-ci.*!42.*stuck/i);
  assert.match(ask.text, /Failed on this commit/);
  // Re-open card: Also row has Retry Fix CI (passing kind) and Reset
  await page.evaluate(() => { const p = puckOf("fix-ci"), el = document.querySelector('.cell[data-dock="fix-ci"]'); showCard(el, p) });
  await page.waitForSelector("#hcard .also");
  assert.match(await page.textContent("#hcard .also"), /Retry Fix CI/);
  assert.match(await page.textContent("#hcard .also"), /Reset/);
  await page.click("#hcard .also button:has-text('Reset')");
  const rst = (await posts(page, "resetAttempts")).pop();
  assert.ok(rst, "clicking Reset sends resetAttempts");
  assert.equal(rst.name, "fix-ci");
});

(async () => {
  const browser = await launchBrowser();
  let failed = 0;
  for (const t of tests) {
    const page = await boot(browser);
    try { await t.fn(page); assert.deepEqual(page._errs, [], "page errors: " + page._errs.join("; ")); console.log("ok  ", t.name) }
    catch (e) { failed++; console.log("FAIL", t.name, "\n    ", e.message.split("\n").join("\n     ")) }
    await page.close();
  }
  await browser.close();
  fs.rmSync(TERM_DIR, {recursive: true, force: true});
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
