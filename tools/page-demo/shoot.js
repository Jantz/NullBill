// Screenshots of the page serve.py shows: node shoot.js <out> <name> [cookie]
// [port]. One picture per page and size, with the agent's panel closed, and
// each as tall as the page's own scroll height (main scrolls inside the page).
const {chromium} = require("playwright");

const [out, name, cookie, port = "8443"] = process.argv.slice(2);
const PAGES = (process.env.PAGES || "overview services backups activity alerts setup/hardware setup/choices setup/plan setup/build access/tailscale access/people access/lan access/trust access/ssh agent/chat agent/settings updates").split(" ");
const SIZES = {desktop: [1440, 900], phone: [390, 844]};
const WANTED = (process.env.SIZES || "desktop phone").split(" ");
const DARK = process.env.DARK === "1";

(async () => {
  const browser = await chromium.launch();
  for (const size of WANTED) {
    const [width, height] = SIZES[size];
    const context = await browser.newContext({viewport: {width, height}, ignoreHTTPSErrors: true, deviceScaleFactor: 1, colorScheme: DARK ? "dark" : "light"});
    if (cookie) await context.addCookies([{name: "nb_session", value: cookie, domain: "127.0.0.1", path: "/", secure: true, httpOnly: true}]);
    const page = await context.newPage();
    page.on("pageerror", (e) => console.log("PAGE ERROR", size, e.message));
    page.on("console", (m) => { if (m.type() === "error") console.log("CONSOLE", size, m.text()); });
    await page.goto(`https://127.0.0.1:${port}/`);
    await page.waitForTimeout(1200);
    for (const route of cookie ? PAGES : ["overview"]) {
      await page.evaluate((r) => { location.hash = "#" + r; }, route);
      await page.waitForTimeout(900);
      if (size === "desktop") {
        await page.evaluate(() => { if (document.getElementById("toggle-dock")?.getAttribute("aria-expanded") === "true") document.getElementById("toggle-dock")?.click(); });
        await page.waitForTimeout(150);
      }
      const tall = await page.evaluate(() => Math.max(document.querySelector("main")?.scrollHeight || 0, document.documentElement.scrollHeight));
      await page.setViewportSize({width, height: Math.min(Math.max(height, tall + 120), 6000)});
      await page.waitForTimeout(150);
      await page.screenshot({path: `${out}/${name}-${route.replace("/", "_")}-${size}${DARK ? "-dark" : ""}.png`});
      await page.setViewportSize({width, height});
    }
    await context.close();
  }
  await browser.close();
})();
