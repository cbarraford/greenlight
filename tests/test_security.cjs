// Adversarial XSS, CSP, and real Python->HTML contract tests for dashboard.html, terminal.html, and stage.html.
// Runs on Chromium (default) or WebKit (BROWSER=webkit).
const fs = require("fs"), path = require("path"), os = require("os"), assert = require("assert");
const {execFileSync} = require("child_process");
let pw;
try { pw = require("playwright-core") } catch { try { pw = require("playwright") } catch {
  console.error("needs playwright-core"); process.exit(2) } }
const {chromium, webkit} = pw;
const ENGINE = (process.env.BROWSER || "chromium").toLowerCase();

const ROOT = path.join(__dirname, "..");
const XSS_IMG = '"><img src=x onerror="window.__pwned=1"><svg onload="window.__pwned=2">';
const XSS_SCRIPT = '</script><script>window.__pwned=3</script>';
const XSS_ATTR = '" autofocus onfocus="window.__pwned=4" style="background:url(javascript:window.__pwned=5)';
const XSS_URL = 'javascript:window.__pwned=6';

(async () => {
  const browser = ENGINE === "webkit" ? await webkit.launch() : await chromium.launch(process.env.CHROME ? {executablePath: process.env.CHROME} : {});
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gl-sec-"));
  fs.copyFileSync(path.join(ROOT, "app/terminal.html"), path.join(dir, "terminal.html"));
  fs.copyFileSync(path.join(ROOT, "app/vendor/ghostty-web.js"), path.join(dir, "ghostty-web.js"));

  try {
    // 1. dashboard.html: adversarial snapshot + pickProjects + hover card + real dash.py contract
    {
      const page = await browser.newPage({viewport: {width: 1300, height: 900}});
      const errs = [];
      page.on("pageerror", e => errs.push(e.message));
      await page.addInitScript(() => {
        window.__posts = [];
        window.webkit = {messageHandlers: {mc: {postMessage: m => window.__posts.push(m)}}};
      });
      await page.goto("file://" + path.join(ROOT, "app/dashboard.html"));

      // Real dash.py snapshot contract test against fixture GREENLIGHT_HOME + stub orca
      const home = path.join(dir, "home"), bindir = path.join(dir, "bin");
      fs.mkdirSync(home, {recursive: true}); fs.mkdirSync(bindir, {recursive: true});
      const orcaBin = path.join(bindir, "orca");
      const orcaData = path.join(dir, "orca.json");
      fs.writeFileSync(orcaData, JSON.stringify({
        ok: true,
        result: {
          worktrees: [{
            worktreeId: "wt-xss", displayName: "xss-ws-" + XSS_IMG, path: "/w/xss-ws",
            repo: "widgets", branch: "feat/" + XSS_IMG, comment: "Ship " + XSS_IMG + " " + XSS_ATTR,
            agents: [{state: "waiting", agentType: "claude", lastAssistantMessage: XSS_IMG, taskTitle: XSS_SCRIPT, toolName: XSS_IMG, toolInput: XSS_ATTR}],
          }],
        },
      }));
      fs.writeFileSync(orcaBin, `#!/bin/sh\ncat ${JSON.stringify(orcaData)}\n`);
      fs.chmodSync(orcaBin, 0o755);
      fs.writeFileSync(path.join(home, "last.json"), JSON.stringify({
        ts: 1700000000,
        rows: [{
          id: "wt-xss",
          name: "xss-ws-" + XSS_IMG,
          path: "/w/xss-ws",
          project: "acme/" + XSS_SCRIPT,
          goal: "Ship " + XSS_IMG + " " + XSS_ATTR,
          branch: "feat/" + XSS_IMG,
          agent: {state: "waiting", type: "claude", last: XSS_IMG, since: 1700000000000, task: XSS_SCRIPT, tool: XSS_IMG, input: XSS_ATTR},
          mr: {ref: "!77", title: XSS_IMG, state: "opened", web_url: XSS_URL},
          next: {step: "review", owner: "me", action: XSS_IMG, cmd: []},
        }],
      }));
      const rawSnap = execFileSync("python3", [path.join(ROOT, "scripts/dash.py"), "snapshot"], {
        env: {...process.env, GREENLIGHT_HOME: home, GREENLIGHT_ADE: "orca", ORCA_CLI_COMMAND: orcaBin, PATH: `${bindir}:${process.env.PATH || ""}`}, encoding: "utf8",
      });
      const snap = JSON.parse(rawSnap);
      await page.evaluate(s => window.mc.update(s), snap);
      await page.evaluate((x) => {
        window.mc.pickProjects([{id: x, name: x, repos: ["a"], busy: 1, color: 'red;background:url(x)'}], null, true);
      }, XSS_IMG);
      await page.waitForTimeout(150);
      assert.equal(await page.evaluate(() => window.__pwned), undefined, "dashboard.html executed injected XSS!");
      assert.equal(await page.locator('img[src="x"]').count(), 0, "dashboard.html rendered injected <img>");
      assert.deepEqual(errs, [], "dashboard.html page errors: " + errs.join("; "));
      await page.close();
      console.log(`ok  dashboard.html adversarial XSS + dash.py contract (${ENGINE})`);
    }

    // 2. terminal.html: adversarial workspaces, tree info, ask() modal, pickProjects, hostile avatar URL
    {
      const page = await browser.newPage({viewport: {width: 1200, height: 800}});
      const errs = [];
      page.on("pageerror", e => errs.push(e.message));
      await page.addInitScript(() => {
        window.__posts = [];
        window.webkit = {messageHandlers: {term: {postMessage: m => window.__posts.push(m)}}};
      });
      await page.goto("file://" + path.join(dir, "terminal.html"));
      await page.waitForFunction(() => window.__posts.some(m => m.cmd === "ready"));
      await page.evaluate(() => { term.restore(null); if (document.body.classList.contains("noside")) toggleSide(); });
      await page.evaluate(({img, scr, attr, url}) => {
        term.workspaces({trees: [{
          ade: "orca", project: "acme/" + img, name: img, path: "/w/xss", branch: scr, main: false,
          agent: "waiting", state: "needs", why: img,
          mr: {ref: "!9" + img, state: "opened", title: img, author: {name: img, avatar: url}, pipeline: "failed", threads: 2},
          moves: [{kind: "ci", prompt: img, auto: false}],
        }]});
        term.tree({op: "info", path: "/w/xss", dirty: img, unpushed: scr, error: img, teardown: img});
        ask({icon: "warn", title: img, text: esc(img), rows: [["r", img, img]], cancel: "No", ok: "Yes"});
      }, {img: XSS_IMG, scr: XSS_SCRIPT, attr: XSS_ATTR, url: XSS_URL});
      await page.waitForTimeout(150);
      assert.equal(await page.evaluate(() => window.__pwned), undefined, "terminal.html executed injected XSS!");
      assert.equal(await page.locator('img[src="x"], img[src^="javascript:"]').count(), 0, "terminal.html rendered injected <img>");
      assert.deepEqual(errs, [], "terminal.html page errors: " + errs.join("; "));
      await page.close();
      console.log(`ok  terminal.html adversarial XSS & CSP (${ENGINE})`);
    }

    // 3. stage.html: adversarial HTML & spec payloads remain inert inside sandboxed iframe/text nodes
    {
      const page = await browser.newPage({viewport: {width: 800, height: 600}});
      const errs = [];
      page.on("pageerror", e => errs.push(e.message));
      await page.goto("file://" + path.join(ROOT, "app/stage.html"));
      await page.evaluate(({img, scr}) => {
        stage.html(`<h1>Hello</h1>${img}${scr}`);
        stage.spec({kind: "table", title: img, columns: [img], rows: [[img, scr]]});
      }, {img: XSS_IMG, scr: XSS_SCRIPT});
      await page.waitForTimeout(200);
      assert.equal(await page.evaluate(() => window.__pwned), undefined, "stage.html executed injected XSS!");
      assert.equal(await page.locator('img[src="x"]').count(), 0, "stage.html rendered injected <img>");
      assert.deepEqual(errs, [], "stage.html page errors: " + errs.join("; "));
      await page.close();
      console.log(`ok  stage.html adversarial XSS & CSP (${ENGINE})`);
    }
  } finally {
    await browser.close();
    fs.rmSync(dir, {recursive: true, force: true});
  }
})().catch(e => { console.error(e); process.exit(1); });
