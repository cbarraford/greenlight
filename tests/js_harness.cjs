// Shared Playwright browser launcher and optional Chromium V8 JS coverage recorder.
// Usage:
//   const { launchBrowser, ENGINE, pw } = require("./js_harness.cjs");
// When JS_COV_DIR is set (and ENGINE === "chromium"), every page opened on the launched browser
// automatically collects V8 JS coverage and writes raw JSON chunks to JS_COV_DIR on close.
const fs = require("fs"), path = require("path"), os = require("os");

function loadPlaywright() {
  try { return require("playwright-core"); } catch {}
  try { return require("playwright"); } catch {}
  const candidates = [
    "/tmp/pw/node_modules/playwright-core",
    path.join(os.homedir(), ".cache/ms-playwright/node_modules/playwright-core"),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return require(c);
  }
  console.error("needs playwright-core: npm i playwright-core, then run with NODE_PATH=<that>/node_modules");
  process.exit(2);
}

const pw = loadPlaywright();
const { chromium, webkit } = pw;
const ENGINE = (process.env.BROWSER || "chromium").toLowerCase();

function browserPath() {
  if (process.env.CHROME) return process.env.CHROME;
  const root = path.join(os.homedir(), ".cache", "ms-playwright");
  const dirs = fs.existsSync(root)
    ? fs.readdirSync(root).filter(d => /^chromium(_headless_shell)?-\d+$/.test(d)).sort().reverse()
    : [];
  for (const d of dirs) {
    for (const rel of [
      "chrome-headless-shell-linux64/chrome-headless-shell",
      "chrome-linux/chrome",
      "chrome-headless-shell-mac-arm64/chrome-headless-shell",
      "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    ]) {
      const p = path.join(root, d, rel);
      if (fs.existsSync(p)) return p;
    }
  }
  return undefined;
}

let covSeq = 0;

async function launchBrowser() {
  const browser = ENGINE === "webkit"
    ? await webkit.launch()
    : await chromium.launch({ executablePath: browserPath() });

  const covDir = process.env.JS_COV_DIR;
  if (!covDir || ENGINE !== "chromium") return browser;

  fs.mkdirSync(covDir, { recursive: true });
  const activePages = new Set();
  const origNewPage = browser.newPage.bind(browser);
  const origClose = browser.close.bind(browser);

  async function flushPage(page) {
    if (!activePages.has(page)) return;
    activePages.delete(page);
    try {
      const entries = await page.coverage.stopJSCoverage();
      const out = path.join(covDir, `v8-${process.pid}-${++covSeq}.json`);
      fs.writeFileSync(out, JSON.stringify(entries));
    } catch {}
  }

  browser.newPage = async (...args) => {
    const page = await origNewPage(...args);
    try {
      await page.coverage.startJSCoverage({ resetOnNavigation: false });
      activePages.add(page);
      const origPageClose = page.close.bind(page);
      page.close = async (...cArgs) => {
        await flushPage(page);
        return origPageClose(...cArgs);
      };
    } catch {}
    return page;
  };

  browser.close = async (...args) => {
    for (const page of [...activePages]) {
      await flushPage(page);
    }
    return origClose(...args);
  };

  return browser;
}

module.exports = { pw, chromium, webkit, ENGINE, browserPath, launchBrowser };
