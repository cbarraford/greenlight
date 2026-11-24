// Render sheet.html to sheet.png (1x, so 16 px is 16 real pixels) with headless Chromium through playwright-core.
//   NODE_PATH=<dir with playwright-core> CHROME=<chrome-headless-shell binary> node brand/icon/concepts/render.js
const path = require('path');
const { chromium } = require('playwright-core');

(async () => {
  const dir = __dirname;
  const browser = await chromium.launch({ executablePath: process.env.CHROME });
  const page = await browser.newPage({ viewport: { width: 1784, height: 900 }, deviceScaleFactor: 1 });
  await page.goto('file://' + path.join(dir, 'sheet.html'));
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(dir, 'sheet.png'), fullPage: true });
  await browser.close();
})();
