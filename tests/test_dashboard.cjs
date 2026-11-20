// Headless checks for dashboard.html: New task (sheet, files, start), the console opening only when asked, the hover card.
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
async function newTask(page){
  await page.click("#bar button:has-text('New task')");
  await page.evaluate(r => window.mc.tasked(r), REPOS);
}
const sheet = page => page.$(".sheet[aria-label='New task']");

test("the Plan view is gone: Build is the only view, and New task sits in its bar", async page => {
  assert.equal(await page.$(".mode"), null);
  assert.equal(await page.evaluate(() => document.getElementById("main").innerText.includes("Plan")), false);
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
  await page.click(".toast button:has-text('Open in Basement')");
  assert.deepEqual((await posts(page, "basement")).map(m => m.path), ["/w/plan-b"]);
  assert.equal(await page.$(".toast"), null);
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

test("CI view: asks dash.py ci, draws runs per day and minutes, filters by project, refreshes fresh", async page => {
  await page.click("#bar .seg button:has-text('CI')");
  assert.deepEqual((await posts(page, "ci")).map(m => [m.days, m.fresh]), [[30, false]], "entering the view asks once");
  const day = (d, sc, f, c, ms) => ({day: d, success: sc, failed: f, canceled: c, running: 0, median_success: ms, p90_success: ms && ms + 5, median_failed: f ? 3 : null});
  const agg = days => ({start: days[0].day, end: days.at(-1).day, days, totals: {success: 3, failed: 2, canceled: 1, running: 0, runs: 6, median_success: 20, p90_success: 25, median_failed: 3}});
  const mr = (ws, project, ref, st) => ({ws, project, ref, web_url: "https://x/" + ref, success: 1, failed: 1, canceled: 0, running: 0, median: 10, durs: [3, 20], statuses: ["failed", st],
    last: {ts: "2026-11-19T10:00:00Z", status: st, dur: 20, url: "https://x/p"}});
  await page.evaluate(r => window.mc.ci(r), {ts: Math.floor(Date.now() / 1000), days_n: 30, errors: [],
    all: {...agg([day("2026-11-18", 2, 1, 1, 20), day("2026-11-19", 1, 1, 0, 30)]), mrs: [mr("plan-a", "acme/widgets", "!7", "success"), mr("other", "acme/tools", "!9", "failed")]},
    projects: {"acme/widgets": agg([day("2026-11-18", 2, 0, 0, 20), day("2026-11-19", 1, 1, 0, 30)]), "acme/tools": agg([day("2026-11-18", 0, 1, 1, null), day("2026-11-19", 0, 0, 0, null)])}});
  const text = await page.evaluate(() => document.querySelector(".ci").innerText);
  assert.ok(/3\s*passed/.test(text) && /2\s*failed/.test(text) && /1\s*canceled/.test(text), text);
  assert.ok(text.includes("2026-11-18 → 2026-11-19"), "the range is named");
  assert.equal(await page.$$eval(".ci-card svg", g => g.length), 2, "runs per day and minutes per run");
  assert.ok((await page.$eval(".ci-card svg", g => g.getAttribute("aria-label"))).includes("2026-11-18 2 passed 1 failed 1 canceled"));
  assert.equal(await page.$$eval(".ci .w-table tr", r => r.length), 3, "header + one row per MR");
  await page.setViewportSize({width: 840, height: 800});  // the app's pane beside the chat: MR refs and latest runs stay on their lines
  await page.evaluate(() => renderMain());
  assert.deepEqual(await page.$$eval(".ci .w-table td:nth-child(2) a", as => as.map(a => a.getClientRects().length)), [1, 1], "an MR ref never breaks");
  assert.deepEqual(await page.$$eval(".ci .w-table td:last-child .sub", ss => ss.map(e => e.getClientRects().length)), [1, 1], "latest: status over one unbroken line of time");
  await page.setViewportSize({width: 1300, height: 900});
  await page.click(".ci-top .seg button:has-text('tools')");
  assert.equal(await page.$$eval(".ci .w-table tr", r => r.length), 2, "the project filter keeps its MRs");
  await page.click(".ci-top button:has-text('Refresh')");
  assert.deepEqual((await posts(page, "ci")).at(-1), {cmd: "ci", days: 30, fresh: true});
  await page.click(".ci-top .seg button:has-text('7 days')");
  assert.deepEqual((await posts(page, "ci")).at(-1), {cmd: "ci", days: 7, fresh: false});
  await page.evaluate(() => window.mc.ci({error: "gitlab: 401"}));
  assert.ok((await page.$eval(".ci-err", e => e.textContent)).includes("401"));
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
