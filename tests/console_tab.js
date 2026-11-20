// node tests/console_tab.js: Chief's [console: …] picks the right Basement tab (terminal.html's openWs)
const src = require("fs").readFileSync(__dirname + "/../app/terminal.html", "utf8"), f = src.slice(src.indexOf("function openWs(ws){"), src.indexOf("// status glyphs"));
let got; const show = t => got = t.name, newTab = w => got = w;
const tabs = [{name: "api", ws: "/w/repo/feat-x", used: 1}, {name: "Codex review", ws: "/w/repo/feat-x", used: 5}, {name: "shell", ws: "/home/me", used: 2}];
const panes = new Map([["mc-aaa", 0], ["mc-bbb", 1], ["mc-ccc", 1], ["mc-ddd", 2]].map(([session, i], id) => [id, {session, tab: tabs[i]}]));  // a split tab has two panes
const paneOf = tab => [...panes.values()].filter(p => p.tab === tab);
eval(f);
const t = (q, want) => { got = null; openWs(q); if (got !== want) { console.log("FAIL", q, "->", got); process.exitCode = 1 } };
t("mc-aaa", "api"); t("mc-ccc", "Codex review"); t("/w/repo/feat-x", "Codex review"); t("feat-x", "Codex review"); t("codex", "Codex review"); t("/w/new", "/w/new"); t("nope", null);
if (!process.exitCode) console.log("ok");
