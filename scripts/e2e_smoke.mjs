// e2e_smoke.mjs — in-browser UI smoke test for the deployed X-Ray Sudoku demo.
//
// Why this exists: the `playwright` MCP server (mcp.json) and this script both
// drive a real headless Chromium, but Playwright's own browser auto-discovery
// does NOT find the browser this environment provides. devbox pins the browsers
// via the Nix `playwright-driver.browsers` bundle, gc-rooted at
// `.devbox-gcroots/playwright-browsers`. That bundle is an aarch64 build laid
// out as `chromium_headless_shell-<rev>/chrome-headless-shell-linux-arm64/
// chrome-headless-shell`, whereas playwright-core scans for a different
// revision and the x64-style `chrome-linux/headless_shell` path. The mismatch
// is why auto-launch fails with "Executable doesn't exist … run npx playwright
// install" even though a working browser is present.
//
// The fix, applied here, is to stop relying on auto-discovery: resolve the real
// binary out of the gc-root and pass it as `executablePath`. The binary itself
// launches and renders the live site fine (verified). Resolving by glob (not a
// hard-coded revision) means a browsers-bundle upgrade does not break this.
//
// What it verifies (the in-browser flow the HTTPS-only QA check could not):
//   1. The Amplify-hosted UI loads and renders (title + board grid present).
//   2. "New Game" produces a 9x9 board of 81 cells via the live API.
//   3. "Solve" completes the board — the success message and solved state show.
//   4. A fresh screenshot is written to the MCP screenshots dir.
//
// Run it through the documented command surface: `just e2e`. It needs the
// network and talks to the deployed stack, so it is NOT part of the offline
// `just test` suite — same boundary as `just verify-trace`.

import { chromium } from "playwright-core";
import { readdirSync, existsSync, realpathSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = dirname(HERE);

// The Amplify UI under test. Overridable via --url or UI_URL for a different
// environment; defaults to the deployed demo frontend.
function resolveUiUrl() {
  const flagIdx = process.argv.indexOf("--url");
  if (flagIdx !== -1 && process.argv[flagIdx + 1]) {
    return process.argv[flagIdx + 1];
  }
  if (process.env.UI_URL) {
    return process.env.UI_URL;
  }
  return "https://main.de4qj9oj2by93.amplifyapp.com";
}

// Resolve the Nix-provided headless Chromium out of the gc-root, so a
// browsers-bundle revision bump (chromium_headless_shell-1243 -> next) keeps
// working without editing this path. Built from readdir rather than a deep
// glob: the Nix store dirs are read-only (dr-xr-xr-x), under which Node's
// globSync does not match a trailing filename segment, so we walk the two
// revision/platform directory levels and append the known binary name.
function resolveChromiumExecutable() {
  const gcroot = join(REPO, ".devbox-gcroots", "playwright-browsers");
  if (!existsSync(gcroot)) {
    throw new Error(
      "Playwright browsers gc-root missing: " +
        gcroot +
        ". Create it with:\n" +
        "  nix build --out-link .devbox-gcroots/playwright-browsers " +
        "nixpkgs#playwright-driver.browsers"
    );
  }
  const root = realpathSync(gcroot);

  // Pick the highest chromium_headless_shell-<rev> so an upgrade wins over a
  // leftover older revision.
  const revDirs = readdirSync(root)
    .filter((n) => n.startsWith("chromium_headless_shell-"))
    .sort();
  for (const rev of revDirs.reverse()) {
    const platformDir = readdirSync(join(root, rev)).find((n) =>
      n.startsWith("chrome-headless-shell-linux-")
    );
    if (!platformDir) continue;
    const exe = join(root, rev, platformDir, "chrome-headless-shell");
    if (existsSync(exe)) return exe;
  }

  throw new Error(
    "No chrome-headless-shell found under " +
      root +
      ". Rebuild the gc-root with:\n" +
      "  nix build --out-link .devbox-gcroots/playwright-browsers " +
      "nixpkgs#playwright-driver.browsers"
  );
}

function fail(msg) {
  console.error("E2E_SMOKE_FAIL: " + msg);
  process.exitCode = 1;
}

async function main() {
  const url = resolveUiUrl();
  const executablePath = resolveChromiumExecutable();
  const screenshotDir = join(REPO, ".agents", "workflow", "screenshots");
  mkdirSync(screenshotDir, { recursive: true });

  console.log("UI_URL      " + url);
  console.log("CHROMIUM    " + executablePath);

  const browser = await chromium.launch({ headless: true, executablePath });
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });

  try {
    // 1. Load and render.
    const resp = await page.goto(url, {
      waitUntil: "domcontentloaded",
      timeout: 30000,
    });
    const status = resp ? resp.status() : 0;
    console.log("LOAD_STATUS " + status);
    if (status !== 200) {
      fail("UI did not return 200 (got " + status + ")");
    }
    const title = await page.title();
    console.log("TITLE       " + title);

    // 2. New Game -> 81-cell board via the live API.
    await page.locator("#new-game-btn").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#board .cell").length === 81,
      { timeout: 30000 }
    );
    const cellCount = await page.locator("#board .cell").count();
    console.log("CELL_COUNT  " + cellCount);
    if (cellCount !== 81) {
      fail("expected 81 board cells, got " + cellCount);
    }

    // 3. Solve -> board completes. The Solve button enables once a game exists;
    //    on success app.js adds .board--solved and shows a success message.
    await page.locator("#solve-btn").click();
    await page.waitForSelector("#board.board--solved", { timeout: 30000 });
    const message = (await page.locator("#message").textContent()) || "";
    console.log("MESSAGE     " + message.trim());

    const emptyAfterSolve = await page.evaluate(() => {
      const cells = Array.from(document.querySelectorAll("#board .cell"));
      return cells.filter((c) => !c.value || c.value.trim() === "").length;
    });
    console.log("EMPTY_CELLS " + emptyAfterSolve);
    if (emptyAfterSolve !== 0) {
      fail("board still has " + emptyAfterSolve + " empty cells after solve");
    }

    // 4. Fresh screenshot (current, not the stale prior-run PNGs).
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const shot = join(screenshotDir, "e2e-solved-" + stamp + ".png");
    await page.screenshot({ path: shot, fullPage: true });
    console.log("SCREENSHOT  " + shot);

    if (consoleErrors.length > 0) {
      console.log("CONSOLE_ERRORS " + consoleErrors.length);
      for (const e of consoleErrors.slice(0, 5)) console.log("  " + e);
    }

    if (process.exitCode === 1) {
      console.error("E2E smoke FAILED — see failures above.");
    } else {
      console.log("E2E_SMOKE_OK");
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  fail(err && err.stack ? err.stack : String(err));
});
