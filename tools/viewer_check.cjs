// Open the 3D viewer headless, measure sensor-to-screen latency, take screenshots.
// usage: node tools/viewer_check.cjs <playwright-core dir> <out dir> [seconds] [label]
const path = require("path");
const { chromium } = require(path.resolve(process.argv[2]));
const out = process.argv[3], secs = +(process.argv[4] || 30), label = process.argv[5] || "twin";
(async () => {
  const b = await chromium.launch({ args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
  const pg = await b.newPage({ viewport: { width: 1600, height: 900 } });
  const errs = []; pg.on("pageerror", (e) => errs.push(e.message)); pg.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  await pg.goto("http://127.0.0.1:8000/", { waitUntil: "load", timeout: 90000 });
  await pg.waitForFunction(() => window.__twin && window.__twin.worldLoaded > 0, null, { timeout: 120000 });
  if (label.includes("truth")) {
    await pg.waitForFunction(() => window.__twin.truth, null, { timeout: 60000 });   // truth arrives every 10 s
    await pg.click("#truth");
  }
  await pg.waitForTimeout(secs * 1000);
  const r = await pg.evaluate(() => {
    const s = [...window.__twin.lat].sort((a, b) => a - b), q = (p) => s.length ? +s[Math.min(s.length - 1, Math.floor(p * s.length))].toFixed(1) : null;
    return { features: window.__twin.worldLoaded, samples: s.length, p50: q(0.5), p95: q(0.95), p99: q(0.99),
             clock_rtt_ms: +(window.__twin.clockRtt * 1000).toFixed(2), entities: Object.keys(window.__twin.ent).length,
             open_alerts: Object.keys(window.__twin.alerts).length };
  });
  await pg.screenshot({ path: `${out}/${label}.png` });
  console.log(JSON.stringify({ ...r, errors: errs.slice(0, 5) }));
  await b.close();
})();
