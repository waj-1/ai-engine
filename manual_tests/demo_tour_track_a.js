// The public demo's guided tour, track A (Build) end to end, as a visitor
// would walk it: Try the demo, Welcome, Start, then each step's coach cards
// in turn, moving on with the toast's "Next step". Screenshots at 1440 and at
// 390 (phone), light theme; a video too with VIDEO=1.
//
// Needs a running demo (DEMO_MODE=true, DEMO_TOUR on) and Node 18+ with the
// playwright package, which is not a dependency of this repository:
//
//   npm install --no-save playwright && npx playwright install chromium
//   DEMO_URL=http://localhost:8000 node manual_tests/demo_tour_track_a.js
//
// Options (environment): DEMO_URL (required), OUT_DIR (default
// ./demo-tour-shots), WIDTHS (default "1440,390"), THEME (light or dark),
// VIDEO=1 (a .webm per width in OUT_DIR), CHROME_PATH (a Chromium binary to
// use instead of Playwright's own). Exits 1 when a step does not tick
// "via walkthrough", a card is off screen or the page logs an error.
//
// It creates one draft agent per width on the demo, as a visitor would.
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const BASE = (process.env.DEMO_URL || "").replace(/\/+$/, "");
if (!BASE) { console.error("Set DEMO_URL, for example DEMO_URL=http://localhost:8000"); process.exit(2); }
const OUT = path.resolve(process.env.OUT_DIR || "demo-tour-shots");
const WIDTHS = (process.env.WIDTHS || "1440,390").split(",").map(Number).filter(Boolean);
const THEME = process.env.THEME === "dark" ? "dark" : "light";
fs.mkdirSync(OUT, { recursive: true });

async function walk(browser, width) {
  const phone = width < 640;
  const ctx = await browser.newContext({
    viewport: { width, height: phone ? 844 : 900 }, hasTouch: phone, deviceScaleFactor: 1,
    recordVideo: process.env.VIDEO === "1" ? { dir: OUT, size: { width, height: phone ? 844 : 900 } } : undefined
  });
  await ctx.addInitScript((theme) => { try { localStorage.setItem("waj-admin-theme", theme); } catch (e) { /* blocked */ } }, THEME);
  const page = await ctx.newPage();
  const problems = [];
  page.on("console", (m) => { if (m.type() === "error") problems.push("console: " + m.text()); });
  page.on("pageerror", (e) => problems.push("page error: " + e.message));
  let n = 0;
  const shot = async (name) => {
    await page.waitForTimeout(500);
    await page.screenshot({ path: path.join(OUT, `track-a-${width}-${THEME}-${String(++n).padStart(2, "0")}-${name}.png`) });
  };
  const state = () => page.evaluate(() => window.__demoTour.state());
  // Wait for the coach card, check it is on screen, screenshot it.
  const card = async (name) => {
    await page.waitForLoadState("domcontentloaded");
    await page.waitForSelector(".dt-coach", { timeout: 20000 });
    await page.waitForTimeout(600);
    const g = await page.evaluate(() => {
      const r = document.querySelector(".dt-coach").getBoundingClientRect();
      return { r: [r.left, r.top, r.right, r.bottom], w: document.documentElement.clientWidth, h: innerHeight,
               title: document.querySelector(".dt-coach-title").textContent };
    });
    if (g.r[0] < -1 || g.r[1] < -1 || g.r[2] > g.w + 1 || g.r[3] > g.h + 1) problems.push(`${name}: card off screen ${JSON.stringify(g)}`);
    console.log(`  ${width} ${name}: ${g.title}`);
    await shot(name);
  };
  const ticked = async (id, ms) => {
    await page.waitForFunction((id) => window.__demoTour.state().steps[id], id, { timeout: ms || 20000, polling: 500 }).catch(() => {});
    const s = (await state()).steps[id];
    console.log(`  ${width} ${id}: ${s ? s.via : "not done"}`);
    if (!s || s.via !== "walkthrough") problems.push(`${id} did not tick via walkthrough`);
  };
  // The toast's Next step, as a visitor moves on.
  const nextStep = async () => {
    await page.waitForSelector(".dt-toast .dt-toast-next", { timeout: 20000 });
    await shot("toast");
    await page.click(".dt-toast .dt-toast-next");
  };

  await page.goto(BASE + "/admin/login");
  await Promise.all([page.waitForNavigation(), page.click("[data-demo-enter]")]);
  await page.waitForSelector(".dt-welcome");
  await shot("welcome");
  await page.click(".dt-start");
  await page.waitForSelector(".dt-drawer.dt-in");
  await shot("checklist");
  await page.click(".dt-step.dt-expanded .dt-step-actions .primary");  // Show me: A1

  await card("A1");
  await page.click('.dt-coach .dt-btn.primary:has-text("Next")');
  await ticked("A1");
  await nextStep();

  await card("A2-test");
  await page.click('[data-tour="agent-test"]');
  await card("A2-send");
  await page.fill('[data-tour="test-input"]', "My internet is slow tonight");
  await page.keyboard.press("Enter");
  await ticked("A2", 120000);
  await shot("A2-reply");
  await nextStep();

  await card("A3-new");
  await Promise.all([page.waitForNavigation(), page.click('[data-tour="agents-new"]:visible')]);
  await card("A3-pick");
  await page.click('[data-tour="template-pick"] input[type=radio] >> nth=0');
  await page.waitForTimeout(500);
  if (await page.isVisible('[data-tour="template-next"]')) { await card("A3-next"); await page.click('[data-tour="template-next"]'); }
  await card("A3-name");
  await page.fill('[data-tour="template-name"]', `Outage helper ${width} ${Date.now() % 10000}`);
  await Promise.all([page.waitForNavigation(), page.click('[data-tour="agent-create"]')]);
  await ticked("A3");
  await nextStep();

  await card("A4-toolset");
  await page.click('[data-tour="tools-add-toolset"]');
  await page.waitForTimeout(300);
  await shot("A4-pick-list");
  await page.click('[data-combobox="toolset"] .combobox-option >> nth=0');
  await card("A4-knowledge");
  await page.click('[data-tour="tools-add-knowledge"]');
  await page.waitForTimeout(300);
  await page.click('[data-combobox="knowledge"] .combobox-option >> nth=0');
  await card("A4-save");
  await Promise.all([page.waitForNavigation({ timeout: 20000 }).catch(() => {}), page.click('[data-tour="agent-save"]')]);
  await ticked("A4");
  await nextStep();

  await card("A5-add");
  await page.click('[data-tour="subagents-add"]');
  await page.waitForTimeout(300);
  await page.click("[data-combobox] .combobox-panel:not([hidden]) .combobox-option >> nth=0");
  await card("A5-save");
  await Promise.all([page.waitForNavigation({ timeout: 20000 }).catch(() => {}), page.click('[data-tour="agent-save"]')]);
  await ticked("A5");
  await page.waitForSelector(".dt-toast", { timeout: 20000 }).catch(() => {});
  await shot("track-a-done");

  const s = await state();
  console.log(`  ${width} made: ${JSON.stringify(s.made.agents)}`);
  const video = page.video();
  await ctx.close();
  if (video) {
    const to = path.join(OUT, `track-a-${width}-${THEME}.webm`);
    fs.renameSync(await video.path(), to);
    console.log(`  ${width} video: ${to}`);
  }
  return problems;
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--disable-gpu"] });
  let problems = [];
  try {
    for (const w of WIDTHS) {
      console.log(`Track A at ${w}`);
      problems = problems.concat((await walk(browser, w)).map((p) => `${w}: ${p}`));
    }
  } finally {
    await browser.close();
  }
  console.log(problems.length ? "PROBLEMS:\n" + problems.join("\n") : "Track A walked with no problems.");
  console.log("Screenshots in " + OUT);
  process.exit(problems.length ? 1 : 0);
})();
