// sudoku_canary.js — CloudWatch Synthetics browser (UI) canary handler.
//
// What this is
// ------------
// A synthetic browser canary for the AWS CloudWatch Synthetics
// `syn-nodejs-puppeteer` runtime (headless Chromium driven by Puppeteer). It
// behaves like a real player against the LIVE Amplify-hosted Sudoku site:
// loads the page, starts a new game, asserts a 9x9 board rendered, clicks
// Solve, and asserts the board reaches the solved state. Any failed assertion
// throws, which fails the step, fails the run, and lowers the canary's
// SuccessPercent metric (Requirement 11.3, 11.4). Synthetics automatically
// captures a screenshot on every named step and on failure (Requirement 11.6).
//
// Not a Python module
// -------------------
// This is a NODE asset INVOKED by the Synthetics runtime, not IMPORTED by any
// Python code. Per the dev-environment steering's import-vs-invoke rule it is
// therefore NOT a uv dependency, does NOT appear in pyproject.toml, and is NOT
// collected by the offline pytest suite. CDK bundles the containing
// `backend/infra/canary/` directory as a Code asset with the handler
// `sudoku_canary.handler`.
//
// Configuration
// -------------
// The target site URL comes from the TARGET_URL environment variable, which the
// CDK canary sets from the operator-supplied `canary_target_url` context (that
// wiring is task 14.2). Nothing is hardcoded here, so this file carries no site
// URL and `cdk synth` stays offline.
//
// Verification
// ------------
// This handler cannot run locally — it needs the Synthetics runtime and a live
// site. It is verified by a Node syntax check (`node --check`) plus confirming
// the selectors below match the real frontend, and it is exercised live once
// deployed (task 14.4), not in the offline pytest suite.
//
// Selectors (confirmed against the real frontend)
// ------------------------------------------------
//   #new-game-btn      — the "New Game" button        (frontend/index.html)
//   #solve-btn         — the "Solve" button           (frontend/index.html)
//   #board             — the board container, class "board" (frontend/index.html)
//   #board .cell       — the 81 <input class="cell"> cells rendered by
//                        app.js buildCell() after New Game (frontend/app.js)
//   #board.board--solved — solved-state class toggled onto #board by app.js
//                        renderBoard() when state.solved is true (frontend/app.js)
//   #message           — the aria-live status/error region (frontend/index.html)

const synthetics = require('Synthetics');
const log = require('SyntheticsLogger');

// A standard Sudoku board has 81 cells (9x9).
const EXPECTED_CELL_COUNT = 81;

/**
 * Resolve the live target site URL from the environment.
 *
 * The CDK canary passes this in as TARGET_URL (task 14.2). We refuse to run
 * without it rather than silently probing a bogus host, so a misconfigured
 * canary fails loudly with a clear reason.
 *
 * @returns {string} the target site URL.
 */
function requireTargetUrl() {
  const url = process.env.TARGET_URL;
  if (!url) {
    throw new Error(
      'TARGET_URL environment variable is not set. The CDK canary must supply ' +
        'the live site URL (from the canary_target_url context) as TARGET_URL.'
    );
  }
  return url;
}

exports.handler = async function () {
  const targetUrl = requireTargetUrl();
  const page = await synthetics.getPage();

  // A desktop-ish viewport so the board renders as it would for a real player
  // and screenshots are legible.
  await page.setViewport({ width: 1280, height: 1024 });

  // 1. load-site — navigate to the live site and wait for the network to settle.
  await synthetics.executeStep('load-site', async () => {
    log.info(`Navigating to target site: ${targetUrl}`);
    await page.goto(targetUrl, { waitUntil: 'networkidle0', timeout: 30000 });
  });

  // 2. new-game — click "New Game" and wait for the board to populate. app.js
  //    renders the 81 cell inputs into #board in response to this click.
  await synthetics.executeStep('new-game', async () => {
    log.info('Clicking the New Game button (#new-game-btn).');
    await page.waitForSelector('#new-game-btn', { timeout: 15000 });
    await page.click('#new-game-btn');
    // Wait for at least the first cell to appear so the next step can count them.
    await page.waitForSelector('#board .cell', { timeout: 15000 });
  });

  // 3. assert-board — a full 9x9 board (exactly 81 cells) must have rendered,
  //    and the status region must not be reporting an error state. A wrong
  //    count or an error message throws, failing the run.
  await synthetics.executeStep('assert-board', async () => {
    const cellCount = await page.$$eval('#board .cell', (els) => els.length);
    log.info(`Rendered board cell count: ${cellCount}`);
    if (cellCount !== EXPECTED_CELL_COUNT) {
      throw new Error(
        `Expected a 9x9 board of ${EXPECTED_CELL_COUNT} cells, but found ${cellCount}.`
      );
    }

    // Guard against a board that "rendered" only because an earlier request
    // failed: the message region should not be showing a network/error state.
    const messageText = await page.$eval('#message', (el) =>
      (el.textContent || '').toLowerCase()
    ).catch(() => '');
    if (
      messageText.includes('error') ||
      messageText.includes('could not be reached')
    ) {
      throw new Error(
        `Board rendered but the status region reports an error: "${messageText}".`
      );
    }
    log.info('Assert-board passed: 81 cells rendered with no error state.');
  });

  // 4. solve — click "Solve" and let the backend complete the board.
  await synthetics.executeStep('solve', async () => {
    log.info('Clicking the Solve button (#solve-btn).');
    await page.waitForSelector('#solve-btn', { timeout: 15000 });
    await page.click('#solve-btn');
  });

  // 5. assert-solved — the board must reach the solved state. app.js toggles the
  //    `board--solved` class onto #board (renderBoard) when solved; we wait for
  //    that, and as a fallback verify every cell input carries a value.
  await synthetics.executeStep('assert-solved', async () => {
    try {
      await page.waitForSelector('#board.board--solved', { timeout: 20000 });
      log.info('Assert-solved passed: #board has the board--solved class.');
      return;
    } catch (err) {
      log.warn(
        'board--solved class not observed in time; falling back to checking ' +
          'that every cell is filled.'
      );
    }

    // Fallback: a solved board has a non-empty value in every one of its cells.
    const filledCount = await page.$$eval(
      '#board .cell',
      (els) => els.filter((el) => el.value && el.value.trim() !== '').length
    );
    log.info(`Filled cell count on fallback check: ${filledCount}`);
    if (filledCount !== EXPECTED_CELL_COUNT) {
      throw new Error(
        `Board did not reach the solved state: ${filledCount} of ` +
          `${EXPECTED_CELL_COUNT} cells are filled and board--solved was not set.`
      );
    }
    log.info('Assert-solved passed via fallback: all 81 cells are filled.');
  });

  log.info('Sudoku canary completed all steps successfully.');
};
