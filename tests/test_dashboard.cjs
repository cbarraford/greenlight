// Headless checks for dashboard.html: New task (the empty page's sheet, files, start), the console opening only when asked, the hover card.
//
//   npm i playwright-core          (anywhere; point NODE_PATH at its node_modules)
//   NODE_PATH=…/node_modules node tests/test_dashboard.cjs
//
// The browser is $CHROME, else the newest Playwright headless shell under ~/.cache/ms-playwright.
const fs = require("fs"), path = require("path"), os = require("os"), assert = require("assert");
let chromium;
try { ({chromium} = require("playwright-core")) } catch { try { ({chromium} = require("playwright")) } catch {
  console.error("needs playwright-core: npm i playwright-core, then run with NODE_PATH=<that>/node_modules"); process.exit(2) } }

function browserPath(){
  if (process.env.CHROME) return process.env.CHROME;
  const root = path.join(os.homedir(), ".cache", "ms-playwright");
  const dirs = fs.existsSync(root) ? fs.readdirSync(root).filter(d => /^chromium(_headless_shell)?-\d+$/.test(d)).sort().reverse() : [];
  for (const d of dirs) for (const rel of ["chrome-headless-shell-linux64/chrome-headless-shell", "chrome-linux/chrome",
      "chrome-headless-shell-mac-arm64/chrome-headless-shell", "chrome-mac/Chromium.app/Contents/MacOS/Chromium"]) {
    const p = path.join(root, d, rel); if (fs.existsSync(p)) return p;
  }
  return undefined;  // let Playwright find its own
}

const PAGE = "file://" + path.join(__dirname, "../app/", "dashboard.html");
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
  await page.press("#ps-task", "Enter");
  await page.fill("#ps-task", "Task B");
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", error: "old task failed"}));
  await page.locator(".toast button:has-text('Reopen')").focus();
  await page.keyboard.press("Enter");
  assert.ok(await sheet(page));
  assert.equal(await page.inputValue("#ps-task"), "Task B");
  assert.deepEqual(await page.$$eval(".tfile .mono", e => e.map(x => x.textContent)), ["later.txt"]);
});

test("with workspaces on the page, a failed start's Reopen still opens the sheet (the bar has no New task)", async page => {
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", error: "no space"}));
  await page.click(".toast button:has-text('Reopen')");
  assert.ok(await sheet(page), "Reopen didn't open the sheet");
  assert.deepEqual((await posts(page, "task")).map(m => m.op), ["repos"]);
});

test("toast: the dismiss button closes it", async page => {
  await page.evaluate(() => window.mc.tasked({op: "new", arg: "widgets", ok: true, name: "plan-b", repo: "widgets"}));
  await page.click(".toast button[aria-label='Dismiss']");
  assert.equal(await page.$(".toast"), null);
});

test("Basement: a page's button and a ⌘-click on a hex send B1 that worktree; no console is left", async page => {
  assert.equal(await page.$("#con"), null);
  await page.evaluate(() => openPage("plan-a"));
  await page.click("#bar button:has-text('Basement')");
  await page.evaluate(() => closePage());
  await page.click("#main .cell[data-name='plan-b']", {modifiers: ["Meta"]});
  assert.deepEqual((await posts(page, "basement")).map(m => m.path), ["/w/plan-a", "/w/plan-b"]);
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
  assert.equal(await page.textContent("#hcard .mg code"), "405: Branch cannot be merged");
  await page.evaluate(() => { const p = puckOf("plan-a"); p.moves = [{kind: "review", prompt: "rv", own: true, repo: "widgets"}, {kind: "merge", how: "Squash", target: "main"}]; hideCard(); document.body.append(hoverCard(p)) });
  assert.match(await page.textContent("#hcard .mv .go"), /^Review/); assert.match(await page.textContent("#hcard .mg .go"), /^Merge !12/, "a review doesn't hide Merge");
  await page.evaluate(() => { const p = puckOf("plan-a"); p.moves = [{kind: "ready"}]; hideCard(); document.body.append(hoverCard(p)) });
  await page.click("#hcard .mg .go");  // one click: undrafting is undone by drafting again
  assert.deepEqual((await posts(page, "undraft")).pop(), {cmd: "undraft", path: "/w/plan-a"});
  await page.evaluate(() => window.mc.merged({op: "ready", path: "/w/plan-a", ok: true, ref: "!12"}));
  assert.equal(await page.textContent("#hcard .mg.ok b"), "!12 is ready for review");
  await page.evaluate(() => { const p = puckOf("plan-a"); p.mr = {...p.mr, state: "merged"}; p.moves = [{kind: "signoff", step: "deploy", run: "git pull"}]; hideCard(); document.body.append(hoverCard(p)) });
  assert.equal(await page.textContent("#hcard .mg .go b"), "Deploy !12", "a merged MR's step after the merge gets its button");
  await page.click("#hcard .mg .go");
  assert.deepEqual((await posts(page, "signoff")).pop(), {cmd: "signoff", path: "/w/plan-a"});
  await page.evaluate(() => window.mc.merged({op: "signoff", path: "/w/plan-a", ok: true, step: "deploy", ran: "git pull"}));
  assert.equal(await page.textContent("#hcard .mg.ok b"), "Deploy signed off");
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
  assert.deepEqual(await page.$$eval(".agents .acard", es => es.map(e => e.dataset.name)), ["plan-b"], "a fresh window shows its working agent (plan-a's question is listed above it)");
  assert.equal(await page.getAttribute(".agents button.fold", "aria-expanded"), "true");
  await page.click(".agents button.fold");
  assert.equal(await page.locator(".agents .acard").count(), 0, "Hide folds them");
  assert.equal(await page.evaluate(() => localStorage.getItem("agentsFolded")), "1");
  const hid = await boot(page.context().browser(), {agentsFolded: "1"});
  try {
    assert.equal(await hid.locator(".agents .acard").count(), 0, "a stored Hide is kept");
    assert.match(await hid.textContent(".agents button.fold"), /^Show 1 agent\b/);
  } finally { await hid.close() }
});

test("Agent cards keep one size in a grid; the shelf folds back; no null in the bar", async page => {
  const run = k => ({...SNAP.pucks[1], name: "run-" + k, path: "/w/run-" + k, sig: "r" + k, steps: ["mr_open", "ci_green", "merge"], step_index: 1});
  const wait = k => ({...SNAP.pucks[1], name: "waiting-on-review-" + k, path: "/w/q" + k, sig: "q" + k, state: "quiet", agent: {...SNAP.pucks[1].agent, state: "idle"}});
  await page.evaluate(s => window.mc.update(s), {...SNAP, pucks: [...SNAP.pucks, ...["c", "d", "e"].map(run), ...Array.from({length: 14}, (_, k) => wait(k))]});
  await page.evaluate(() => { ui.agentsFolded = false; render() });
  const box = await page.$$eval(".acard", es => es.map(e => e.getBoundingClientRect()).map(r => [Math.round(r.width), Math.round(r.top)]));
  assert.equal(box.length, 4);
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
  await page.clock.fastForward(13000);
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
  assert.equal(await page.locator(`#main [data-name="plan-a"]`).count(), 1, "and it's drawn");
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
test("terminal: arrows move between tabs, Enter opens the focused one", async page => {
  const tp = await page.context().browser().newPage({viewport: {width: 760, height: 460}});
  await tp.addInitScript(() => { window.webkit = {messageHandlers: {term: {postMessage: m => (window.__term = window.__term || []).push(m)}}} });
  await tp.route("**/ghostty-web.js", r => r.fulfill({path: path.join(__dirname, "../app/", "vendor", "ghostty-web.js")}));
  await tp.goto("file://" + path.join(__dirname, "../app/", "terminal.html"));
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
  assert.equal(before.btn, "rgb(46, 111, 67)", "no theme: the primary button is Sage green");
  assert.equal(after.btn, "rgb(255, 0, 0)", "the primary button takes the theme accent");
  assert.deepEqual([after.orange, after.red, after.blue], [before.orange, before.red, before.blue], "a theme accent changed a state color");
});

test("terminal: a tab whose title is just ~ is named by the folder its shell is in", async page => {
  const tp = await page.context().browser().newPage({viewport: {width: 760, height: 460}});
  await tp.addInitScript(() => { window.webkit = {messageHandlers: {term: {postMessage: m => (window.__term = window.__term || []).push(m)}}} });
  await tp.route("**/ghostty-web.js", r => r.fulfill({path: path.join(__dirname, "../app/", "vendor", "ghostty-web.js")}));
  await tp.goto("file://" + path.join(__dirname, "../app/", "terminal.html"));
  await tp.waitForFunction(() => (window.__term || []).some(m => m.cmd === "ready"), null, {timeout: 8000});
  await tp.evaluate(() => term.restore(null));  // the app's answer to ready: no saved layout
  await tp.evaluate(() => { const id = window.__term.find(m => m.cmd === "open").id; term.feed(id, btoa("\x1b]2;me@devbox:~\x07\x1b]7;file://devbox/home/me/w/retry-stuck-outbounds\x07$ ")) });
  assert.equal(await tp.textContent(".tab.on .nm"), "retry-stuck-outbounds");
  await tp.close();
});

test("terminal: keys send what Ghostty sends, and ⌘K / ⌘A / ⌘F / ⌘V / panes act like Ghostty's", async page => {
  const tp = await page.context().browser().newPage({viewport: {width: 1000, height: 600}});
  await tp.addInitScript(() => { window.webkit = {messageHandlers: {term: {postMessage: m => (window.__term = window.__term || []).push(m)}}} });
  await tp.route("**/ghostty-web.js", r => r.fulfill({path: path.join(__dirname, "../app/", "vendor", "ghostty-web.js")}));
  await tp.goto("file://" + path.join(__dirname, "../app/", "terminal.html"));
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
  await page.evaluate(n => window.mc.pick([n]), name);
  await page.click(`.cell.picked`);
  assert.ok(!(await page.$(".cell.picked")), "clicking a picked hex clears the picks");
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

(async () => {
  const browser = await chromium.launch({executablePath: browserPath()});
  let failed = 0;
  for (const t of tests) {
    const page = await boot(browser);
    try { await t.fn(page); assert.deepEqual(page._errs, [], "page errors: " + page._errs.join("; ")); console.log("ok  ", t.name) }
    catch (e) { failed++; console.log("FAIL", t.name, "\n    ", e.message.split("\n").join("\n     ")) }
    await page.close();
  }
  await browser.close();
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
