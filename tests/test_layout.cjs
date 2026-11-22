// Multi-viewport layout invariant checks across app/dashboard.html, app/terminal.html, and app/stage.html.
//
//   NODE_PATH=…/node_modules node tests/test_layout.cjs
//
// Verifies across 4 viewports (760x520, 1300x900, 1680x1020, 1100x480) and dark/light themes:
//   - zero pageerror exceptions
//   - zero horizontal document overflow (scrollWidth <= clientWidth + 2)
//   - primary chrome / header / controls visible with non-zero bounding boxes
//   - zero broken/0x0 SVGs in visible UI
const fs = require("fs");
const os = require("os");
const path = require("path");
const assert = require("assert");
const { launchBrowser } = require("./js_harness.cjs");

const VIEWPORTS = [
  { name: "compact", width: 760, height: 520 },
  { name: "standard", width: 1300, height: 900 },
  { name: "wide", width: 1680, height: 1020 },
  { name: "short-wide", width: 1100, height: 480 },
];
const THEMES = ["dark", "light"];

const DASH_PAGE = "file://" + path.join(__dirname, "../app/dashboard.html");
const STAGE_PAGE = "file://" + path.join(__dirname, "../app/stage.html");

function makeTermStageDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gl-layout-term-"));
  fs.copyFileSync(path.join(__dirname, "../app/terminal.html"), path.join(dir, "terminal.html"));
  fs.copyFileSync(path.join(__dirname, "../app/vendor/ghostty-web.js"), path.join(dir, "ghostty-web.js"));
  return dir;
}

function makeDashboardSnap() {
  const nowSec = Math.floor(Date.now() / 1000);
  const nowMs = Date.now();
  const base = (name, goal, state, extra = {}) => ({
    name,
    path: "/w/" + name,
    project: extra.project || "acme/widgets",
    goal,
    state,
    why: extra.why || "",
    progress: extra.progress || 0.5,
    steps: extra.steps || [],
    step_index: 1,
    step: extra.step || "ci_green",
    owner: extra.owner || "agent",
    hidden: false,
    action: extra.action || "",
    changed: nowSec - 120,
    agent: {
      state: extra.agentState || "idle",
      type: extra.agentType || "claude",
      last: extra.agentLast || "",
      since: nowMs - 180000,
      task: extra.agentTask || goal,
      tool: extra.agentTool || "",
      input: "",
    },
    mr: extra.mr || null,
    branch: "feat/" + name,
    puck: null,
    sig: name + "-" + state,
    built: extra.built !== undefined ? extra.built : true,
    moves: extra.moves || [],
  });

  return {
    ts: nowSec,
    scanned: nowSec - 30,
    pucks: [
      base("auth-retry", "Retry transient token refresh errors", "needs", {
        why: "Agent is waiting on your choice",
        agentState: "waiting",
        agentType: "claude",
        agentLast: "Should refresh tokens expire after 7 or 30 days?",
        mr: { iid: 101, ref: "!101", state: "opened", pipeline: "failed", threads: 2, web_url: "https://example.invalid/mr/101" },
      }),
      base("billing-cache", "Add read-through cache for invoice lookup", "working", {
        agentState: "working",
        agentType: "codex",
        agentTool: "shell",
        mr: { iid: 104, ref: "!104", state: "opened", pipeline: "running", threads: 0, web_url: "https://example.invalid/mr/104" },
      }),
      base("ci-flaky-spec", "Stabilize retry backoff timing test", "crashed", {
        why: "Pipeline failed on retry assertion",
        mr: { iid: 108, ref: "!108", state: "opened", pipeline: "failed", threads: 1, web_url: "https://example.invalid/mr/108" },
      }),
      base("review-ready", "Ready for maintainer review", "raised", {
        project: "octo/greenlight",
        why: "Waiting for approval",
        mr: { iid: 112, ref: "!112", state: "opened", pipeline: "success", threads: 0, web_url: "https://example.invalid/mr/112" },
      }),
      base("docs-polish", "Clarify CLI flag defaults", "queued", { project: "octo/greenlight" }),
      base("waiting-review", "Awaiting reviewer response", "quiet", { project: "octo/greenlight", why: "waiting on reviewer" }),
      base("shipped-cleanup", "Remove deprecated flag aliases", "done", { project: "acme/widgets" }),
    ],
  };
}

const TERM_TREES = {
  trees: [
    { ade: "orca", project: "acme/widgets", name: "widgets", path: "/w/widgets", branch: "main", main: true, agent: "none" },
    { ade: "orca", project: "acme/widgets", name: "auth-retry", path: "/w/auth-retry", branch: "feat/auth-retry", main: false, agent: "working", state: "working" },
    {
      ade: "cmux",
      project: "octo/greenlight",
      name: "layout-checks",
      path: "/w/layout-checks",
      branch: "feat/layout-checks",
      main: false,
      agent: "waiting",
      state: "needs",
      why: "waiting on answer",
      mr: { ref: "!51", state: "opened", pipeline: "failed", threads: 2, rebase: true },
    },
    { ade: "cmux", project: "octo/greenlight", name: "review-ready", path: "/w/review-ready", branch: "feat/review", main: false, agent: "none", state: "quiet", why: "waiting on reviewer" },
    { ade: "cmux", project: "octo/greenlight", name: "shipped-cleanup", path: "/w/shipped-cleanup", branch: "feat/done", main: false, agent: "none", state: "done", mr: { ref: "!40", state: "merged", pipeline: "success", threads: 0 } },
  ],
};

const TERM_PROJECTS = [
  { id: "p1", name: "Platform", repos: ["acme/widgets"], dirs: [] },
  { id: "p2", name: "Greenlight", repos: ["octo/greenlight"], dirs: [] },
];

async function assertLayoutInvariants(page, label, requiredSelectors) {
  const report = await page.evaluate((selectors) => {
    const doc = document.documentElement;
    const body = document.body;
    const docOverflow = doc.scrollWidth - doc.clientWidth;
    const bodyOverflow = body.scrollWidth - body.clientWidth;

    const missing = [];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) {
        missing.push(`${sel}: not found`);
        continue;
      }
      const st = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) === 0) {
        missing.push(`${sel}: hidden (display=${st.display}, visibility=${st.visibility})`);
      } else if (r.width <= 0 || r.height <= 0) {
        missing.push(`${sel}: zero bounding box (${r.width}x${r.height})`);
      }
    }

    const brokenSvgs = [];
    for (const svg of document.querySelectorAll("svg")) {
      // Skip pure <defs> gradient containers like dashboard's hidden #glass svg
      const hasVisibleShapes = Array.from(svg.children).some((c) => c.tagName.toLowerCase() !== "defs");
      if (!hasVisibleShapes) continue;
      let hidden = false;
      for (let cur = svg; cur && cur.nodeType === 1; cur = cur.parentElement) {
        const cs = getComputedStyle(cur);
        if (cs.display === "none" || cs.visibility === "hidden" || cur.hidden) {
          hidden = true;
          break;
        }
      }
      if (hidden) continue;
      const r = svg.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) {
        brokenSvgs.push(svg.outerHTML.slice(0, 120));
      }
    }

    return {
      docOverflow,
      bodyOverflow,
      docScrollW: doc.scrollWidth,
      docClientW: doc.clientWidth,
      missing,
      brokenSvgs,
    };
  }, requiredSelectors);

  assert.ok(
    report.docOverflow <= 2,
    `${label}: horizontal document overflow (${report.docScrollW} > ${report.docClientW})`
  );
  assert.ok(
    report.bodyOverflow <= 2,
    `${label}: horizontal body overflow (${report.bodyOverflow}px)`
  );
  assert.deepEqual(report.missing, [], `${label}: missing/collapsed primary chrome: ${report.missing.join("; ")}`);
  assert.deepEqual(report.brokenSvgs, [], `${label}: broken 0x0 visible SVG(s): ${report.brokenSvgs.join("; ")}`);
}

async function checkDashboard(browser) {
  const snap = makeDashboardSnap();
  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: VIEWPORTS[1], colorScheme: theme });
    const errs = [];
    page.on("pageerror", (e) => errs.push(e.message));
    await page.addInitScript(() => {
      window.__posts = [];
      window.webkit = { messageHandlers: { mc: { postMessage: (m) => window.__posts.push(m) } } };
    });
    await page.goto(DASH_PAGE);
    await page.evaluate((s) => {
      window.mc.bell(2, false, []);
      window.mc.chat(false, 1);
      window.mc.update(s);
    }, snap);

    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      // Trigger resize re-layout cleanly
      await page.evaluate(() => window.dispatchEvent(new Event("resize")));
      await page.waitForTimeout(90);

      // Overview mode
      await assertLayoutInvariants(
        page,
        `dashboard[overview ${theme} ${vp.name} ${vp.width}x${vp.height}]`,
        ["#bar", "#bar .title", "#bar .seg", "#bar #q", "#main", "#foot", ".agents", ".acard"]
      );

      // List mode
      await page.click("#bar .seg button:has-text('List')");
      await page.waitForTimeout(40);
      await assertLayoutInvariants(
        page,
        `dashboard[list ${theme} ${vp.name} ${vp.width}x${vp.height}]`,
        ["#bar", "#bar .title", "#main", "#foot"]
      );

      // Switch back to Overview for next viewport
      await page.click("#bar .seg button:has-text('Overview')");
      await page.waitForTimeout(40);
    }

    assert.deepEqual(errs, [], `dashboard[${theme}] pageerror(s): ${errs.join("; ")}`);
    await page.close();
  }
}

async function checkTerminal(browser, termPageUrl) {
  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: VIEWPORTS[1], colorScheme: theme });
    const errs = [];
    page.on("pageerror", (e) => errs.push(e.message));
    await page.addInitScript(() => {
      window.__posts = [];
      window.webkit = { messageHandlers: { term: { postMessage: (m) => window.__posts.push(m) } } };
    });
    await page.goto(termPageUrl);
    await page.waitForFunction(() => window.__posts.some((m) => m.cmd === "ready"));
    await page.evaluate(
      ([trees, projects, mode]) => {
        term.slide(0);
        term.restore(null);
        term.projects(projects);
        term.workspaces(trees);
        if (document.body.classList.contains("noside")) toggleSide();
        term.mode(mode, "#FFFFFF", "#1F2328");
      },
      [TERM_TREES, TERM_PROJECTS, theme]
    );
    await page.waitForTimeout(80);

    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.evaluate(
        () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      );
      await page
        .waitForFunction(
          () => document.body.scrollWidth <= document.body.clientWidth + 2,
          null,
          { timeout: 1500 }
        )
        .catch(() => {});

      await assertLayoutInvariants(
        page,
        `terminal[${theme} ${vp.name} ${vp.width}x${vp.height}]`,
        ["#side", "#shead", "#shead .ttl", "#jump", "#tree", "#main", "#bar", "#tabs", "#stage"]
      );
    }

    assert.deepEqual(errs, [], `terminal[${theme}] pageerror(s): ${errs.join("; ")}`);
    await page.close();
  }
}

async function checkStage(browser) {
  const specs = [
    {
      name: "table",
      run: () =>
        stage.spec({
          kind: "table",
          eyebrow: "Board summary",
          title: "4 active workspaces",
          columns: ["Workspace", "MR", "CI", "Next"],
          rows: [
            ["auth-retry", "!101", "failed", "answer question"],
            ["billing-cache", "!104", "running", "wait for CI"],
            ["review-ready", "!112", "passed", "merge"],
          ],
          tones: ["bad", "now", "ok"],
        }),
      selectors: ["#s", "#s h1", "table.grid", "table.grid tbody tr"],
    },
    {
      name: "lifecycle",
      run: () =>
        stage.spec({
          kind: "lifecycle",
          eyebrow: "auth-retry",
          title: "Verify in progress",
          at: "3.2",
          holder: "your agent",
          stages: [
            { label: "Define", state: "done" },
            { label: "Build", state: "done" },
            { label: "Verify", state: "now", detail: "Running layout suite" },
            { label: "Review", state: "todo" },
            { label: "Land", state: "todo" },
          ],
        }),
      selectors: ["#s", "#s h1", "ol.life", "ol.life li.now", ".foot"],
    },
    {
      name: "chart-bar",
      run: () =>
        stage.chart({
          chart: "bar",
          stacked: true,
          title: "CI outcomes",
          sub: "Last 7 days",
          labels: ["Mon", "Tue", "Wed", "Thu"],
          series: [
            { name: "passed", values: [14, 18, 12, 20], color: "green" },
            { name: "failed", values: [2, 1, 3, 1], color: "red" },
          ],
          note: "All repos",
        }),
      selectors: ["#s", "#s h1", "svg.chart", ".legend", ".foot"],
    },
    {
      name: "chart-line",
      run: () =>
        stage.chart({
          chart: "line",
          title: "Turn latency",
          labels: ["10:00", "11:00", "12:00", "13:00"],
          series: [{ name: "p50", values: [120, 95, 110, 88], color: "blue" }],
        }),
      selectors: ["#s", "#s h1", "svg.chart"],
    },
  ];

  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: VIEWPORTS[1], colorScheme: theme });
    const errs = [];
    page.on("pageerror", (e) => errs.push(e.message));
    await page.goto(STAGE_PAGE);

    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      for (const spec of specs) {
        await page.evaluate(spec.run);
        await page.waitForFunction(
          () => {
            const s = document.getElementById("s");
            return Boolean(s && parseFloat(getComputedStyle(s).opacity) >= 0.99);
          },
          null,
          { timeout: 1500 }
        );
        await assertLayoutInvariants(
          page,
          `stage[${spec.name} ${theme} ${vp.name} ${vp.width}x${vp.height}]`,
          spec.selectors
        );
      }
    }

    assert.deepEqual(errs, [], `stage[${theme}] pageerror(s): ${errs.join("; ")}`);
    await page.close();
  }
}

(async () => {
  const termDir = makeTermStageDir();
  const browser = await launchBrowser();
  try {
    await checkDashboard(browser);
    await checkTerminal(browser, "file://" + path.join(termDir, "terminal.html"));
    await checkStage(browser);
    console.log("layout ok (dashboard, terminal, stage x 4 viewports x dark/light)");
  } finally {
    await browser.close();
    fs.rmSync(termDir, { recursive: true, force: true });
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
