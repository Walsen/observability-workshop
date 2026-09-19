// app.js — client-side application logic for the X-Ray Sudoku frontend.
//
// Script style: plain <script> globals, no build step, no modules, no
// dependencies (see config.js for the rationale). This file is loaded AFTER
// config.js, so `window.apiBaseUrl()` is available here.
//
// ── Scope of this file across tasks ──────────────────────────────────────────
// Task 12.1 (this task) implements only the player-identity concern below:
//   - getPlayerId(): a stable, per-browser player id persisted in localStorage.
//   - a minimal apiUrl() helper that composes the configured API base URL.
// Task 12.2 will EXTEND this file with the board UI rendering and the fetch
// calls to newGame/getGame/submitMove/solve, originating and sending the
// `X-Amzn-Trace-Id` header on every outbound request. To avoid collisions, keep
// 12.2's board/fetch code in its own clearly-marked section below this one; do
// not fold it into the player-identity block.

// ── Player identity (Requirement 7.1, 7.2) ───────────────────────────────────

// Stable localStorage key under which this browser's player id is persisted.
// Namespaced so it will not clash with other apps on the same origin.
var PLAYER_ID_STORAGE_KEY = 'xray-sudoku-playerId';

/**
 * Generate a new random player id.
 *
 * Uses the browser-native `crypto.randomUUID()` when available (all current
 * browsers over HTTPS, which Amplify Hosting provides), and falls back to a
 * simple RFC-4122-shaped v4 UUID built from `crypto.getRandomValues` or, as a
 * last resort, `Math.random`. The id only needs to be unique enough to separate
 * concurrent players; it is not a security token.
 *
 * @returns {string} a freshly generated player id.
 */
function generatePlayerId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }

  // Fallback: build a v4-shaped UUID. Prefer crypto-quality randomness.
  var bytes = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    for (var i = 0; i < 16; i++) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }

  // Set the version (4) and variant (10xx) bits per RFC 4122.
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  var hex = [];
  for (var j = 0; j < 256; j++) {
    hex[j] = (j + 0x100).toString(16).slice(1);
  }

  var b = bytes;
  return (
    hex[b[0]] + hex[b[1]] + hex[b[2]] + hex[b[3]] + '-' +
    hex[b[4]] + hex[b[5]] + '-' +
    hex[b[6]] + hex[b[7]] + '-' +
    hex[b[8]] + hex[b[9]] + '-' +
    hex[b[10]] + hex[b[11]] + hex[b[12]] + hex[b[13]] + hex[b[14]] + hex[b[15]]
  );
}

/**
 * Return this browser's player id, generating and persisting it on first use.
 *
 * The id is generated exactly once per browser (Requirement 7.1) and stored in
 * localStorage so it is stable across reloads and reused on every request
 * (Requirement 7.2). Subsequent calls return the stored value.
 *
 * If localStorage is unavailable (e.g. disabled cookies/storage), this falls
 * back to a per-page-load id held in memory so the app still functions for the
 * current session.
 *
 * @returns {string} the stable player id for this browser.
 */
function getPlayerId() {
  try {
    var stored = window.localStorage.getItem(PLAYER_ID_STORAGE_KEY);
    if (stored) {
      return stored;
    }
    var generated = generatePlayerId();
    window.localStorage.setItem(PLAYER_ID_STORAGE_KEY, generated);
    return generated;
  } catch (err) {
    // localStorage blocked/unavailable: degrade to an in-memory id that is
    // stable for the lifetime of this page load.
    if (!getPlayerId._memoryId) {
      getPlayerId._memoryId = generatePlayerId();
    }
    return getPlayerId._memoryId;
  }
}

// ── API URL composition helper (for task 12.2 to build fetch calls on) ────────

/**
 * Compose an absolute API URL from the configured base URL and a path.
 *
 * Returns null when the API base URL is not configured (see config.js), so the
 * caller (task 12.2) can surface a clear "not configured" state rather than
 * issuing a request to an invalid host.
 *
 * @param {string} [path=''] a path beginning with "/" (e.g. "/games").
 * @returns {string|null} the absolute URL, or null if not configured.
 */
function apiUrl(path) {
  var base = window.apiBaseUrl ? window.apiBaseUrl() : null;
  if (!base) {
    return null;
  }
  return base + (path || '');
}

// Expose the player-identity API as globals for task 12.2 and tests.
window.getPlayerId = getPlayerId;
window.apiUrl = apiUrl;

// ── Board UI and API calls (task 12.2) ────────────────────────────────────────
// This section extends the identity logic above with:
//   - X-Amzn-Trace-Id origination (newTraceHeader + apiFetch),
//   - the four API calls (newGame/getGame/submitMove/solve), each carrying
//     playerId,
//   - board rendering into the 9x9 grid, and move/solve UX + error handling.
// It relies only on the globals defined above (getPlayerId, apiUrl) and in
// config.js (apiBaseUrl); no frameworks, no modules.

// The board size and box size. A standard Sudoku is a 9x9 grid of 3x3 boxes.
var BOARD_SIZE = 9;
var BOX_SIZE = 3;

// LocalStorage key for the current game id, so a reload can re-fetch the board.
var GAME_ID_STORAGE_KEY = 'xray-sudoku-gameId';

// In-memory game state. `board` is the current 9x9 grid (0 = empty). `givens`
// is a 9x9 boolean grid marking cells that were non-zero in the *initial*
// puzzle; those stay fixed for the life of the game and are never editable.
var state = {
  gameId: null,
  board: null,
  givens: null,
  solved: false,
};

// ── Trace origination (Requirement 1.1; design "End-to-End Trace Propagation") ─

/**
 * Build a fresh X-Ray root trace header value.
 *
 * This is the whole point of the demo: the BROWSER originates the trace, so the
 * single coherent trace (Frontend → API Gateway → Lambda → DynamoDB) begins
 * here rather than at the API. The X-Ray "Root" id format is:
 *
 *   Root=1-{8 hex}-{24 hex}
 *
 * where the first 8 hex digits are the request's epoch seconds and the trailing
 * 24 hex digits are random. Example:
 *   Root=1-5759e988-bd862e3fe1be46a994272793
 *
 * A fresh id is generated per user action (per game action) so each click is
 * its own end-to-end trace, which is exactly what the workshop wants to inspect.
 *
 * @returns {string} a value suitable for the `X-Amzn-Trace-Id` request header.
 */
function newTraceHeader() {
  // 8 hex digits: the current time in epoch SECONDS, zero-padded to 8.
  var epochSeconds = Math.floor(Date.now() / 1000);
  var timeHex = epochSeconds.toString(16).padStart(8, '0');

  // 24 hex digits of randomness (96 bits). Prefer crypto-quality randomness,
  // falling back to Math.random only if the Web Crypto API is unavailable.
  var randomHex = randomHexDigits(24);

  return 'Root=1-' + timeHex + '-' + randomHex;
}

/**
 * Return `count` random hexadecimal digits as a lowercase string.
 *
 * @param {number} count how many hex digits to produce (should be even).
 * @returns {string} the hex string of length `count`.
 */
function randomHexDigits(count) {
  var byteCount = Math.ceil(count / 2);
  var bytes = new Uint8Array(byteCount);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    for (var i = 0; i < byteCount; i++) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  var hex = '';
  for (var k = 0; k < byteCount; k++) {
    hex += (bytes[k] + 0x100).toString(16).slice(1);
  }
  return hex.slice(0, count);
}

// ── fetch wrapper ─────────────────────────────────────────────────────────────

/**
 * Error thrown when the API base URL has not been configured yet.
 * Callers surface this as the "not configured" message rather than a crash.
 */
function NotConfiguredError() {
  this.name = 'NotConfiguredError';
  this.message = 'The API endpoint is not configured.';
}
NotConfiguredError.prototype = Object.create(Error.prototype);

/**
 * Perform a JSON fetch against the API, ORIGINATING the X-Ray trace.
 *
 * Every outbound request carries a freshly generated `X-Amzn-Trace-Id` header
 * (see newTraceHeader) so the browser starts the trace — this is the critical
 * behavior of Requirement 1.1. `X-Amzn-Trace-Id` is a custom header the API's
 * CORS preflight already allows (task 10.4), so the browser lets us set it.
 *
 * Content-Type is set to application/json for every request. The URL is built
 * from apiUrl(path); if the API is not configured, a NotConfiguredError is
 * thrown so the caller can show the not-configured message instead of firing a
 * request at a bogus host.
 *
 * @param {string} path an API path beginning with "/" (e.g. "/games").
 * @param {object} [options] fetch options; `body` may be a plain object.
 * @returns {Promise<{status:number, ok:boolean, data:any}>} parsed response.
 */
function apiFetch(path, options) {
  options = options || {};

  var url = apiUrl(path);
  if (!url) {
    return Promise.reject(new NotConfiguredError());
  }

  var headers = {
    'Content-Type': 'application/json',
    // CRITICAL: originate the X-Ray trace from the browser. A fresh root id per
    // request means each user action is its own end-to-end trace.
    'X-Amzn-Trace-Id': newTraceHeader(),
  };

  var fetchOptions = {
    method: options.method || 'GET',
    headers: headers,
  };

  // Serialize a plain-object body to JSON. GET requests carry no body.
  if (options.body !== undefined && options.body !== null) {
    fetchOptions.body = JSON.stringify(options.body);
  }

  return fetch(url, fetchOptions).then(function (response) {
    // Parse the JSON body defensively: error bodies are JSON too, but a network
    // proxy or empty body should not blow up the caller.
    return response
      .json()
      .catch(function () {
        return {};
      })
      .then(function (data) {
        return { status: response.status, ok: response.ok, data: data };
      });
  });
}

// ── The four API calls (each includes playerId from getPlayerId()) ────────────

/**
 * POST /games — create a new game. playerId travels in the JSON body.
 * @returns {Promise<{status,ok,data}>} with data {gameId, board, status}.
 */
function newGame() {
  return apiFetch('/games', {
    method: 'POST',
    body: { playerId: getPlayerId() },
  });
}

/**
 * GET /games/{gameId} — load an existing game. playerId travels in the QUERY
 * string, matching the get_game handler which reads playerId from the query.
 * @param {string} gameId
 * @returns {Promise<{status,ok,data}>} with data {gameId, board, status}.
 */
function getGame(gameId) {
  var query = '?playerId=' + encodeURIComponent(getPlayerId());
  return apiFetch('/games/' + encodeURIComponent(gameId) + query, {
    method: 'GET',
  });
}

/**
 * POST /games/{gameId}/moves — submit a single move. playerId + move in body.
 * @param {string} gameId
 * @param {number} row 0-based row index.
 * @param {number} col 0-based column index.
 * @param {number} value the digit 1-9 to place.
 * @returns {Promise<{status,ok,data}>} data {accepted, solved?, status?, board?}
 *   or {accepted:false, reason:"invalid_move"}.
 */
function submitMove(gameId, row, col, value) {
  return apiFetch('/games/' + encodeURIComponent(gameId) + '/moves', {
    method: 'POST',
    body: {
      playerId: getPlayerId(),
      move: { row: row, col: col, value: value },
    },
  });
}

/**
 * POST /games/{gameId}/solve — ask the backend to complete the board.
 * playerId travels in the JSON body.
 * @param {string} gameId
 * @returns {Promise<{status,ok,data}>} data {solved:true, board} or
 *   {solved:false, reason:"no_solution"}.
 */
function solve(gameId) {
  return apiFetch('/games/' + encodeURIComponent(gameId) + '/solve', {
    method: 'POST',
    body: { playerId: getPlayerId() },
  });
}

// ── DOM references (resolved on DOMContentLoaded) ─────────────────────────────

var els = {
  board: null,
  message: null,
  newGameBtn: null,
  solveBtn: null,
  gameId: null,
};

// ── Message area helpers ──────────────────────────────────────────────────────

/**
 * Show a message in the aria-live region with a visual variant.
 * @param {string} text the message to show.
 * @param {'info'|'error'|'success'} [variant='info'] the visual style.
 */
function showMessage(text, variant) {
  if (!els.message) {
    return;
  }
  els.message.textContent = text;
  els.message.className = 'message';
  if (variant === 'error') {
    els.message.classList.add('message--error');
  } else if (variant === 'success') {
    els.message.classList.add('message--success');
  }
  els.message.hidden = false;
}

/** Clear and hide the message region. */
function clearMessage() {
  if (!els.message) {
    return;
  }
  els.message.textContent = '';
  els.message.hidden = true;
}

/**
 * Translate an API error response (or thrown error) into a readable message.
 * The backend's error bodies are JSON of the form {error, field}; use them when
 * present, otherwise fall back to a status-based message. Never crashes.
 *
 * @param {{status:number, data:any}} response the apiFetch result.
 * @returns {string} a human-readable message.
 */
function describeApiError(response) {
  var data = response && response.data;
  if (data && data.error) {
    return data.field ? data.error + ' (field: ' + data.field + ')' : data.error;
  }
  if (response && response.status === 404) {
    return 'Game not found.';
  }
  if (response && response.status >= 400) {
    return 'Request failed (status ' + response.status + ').';
  }
  return 'Something went wrong. Please try again.';
}

// ── Board rendering ────────────────────────────────────────────────────────────

/**
 * Build a 9x9 boolean grid marking which cells are "givens" (non-zero) in the
 * initial puzzle. Givens stay fixed and are rendered read-only.
 * @param {number[][]} board the initial board.
 * @returns {boolean[][]} the givens mask.
 */
function computeGivens(board) {
  var givens = [];
  for (var r = 0; r < BOARD_SIZE; r++) {
    givens[r] = [];
    for (var c = 0; c < BOARD_SIZE; c++) {
      givens[r][c] = board[r][c] !== 0;
    }
  }
  return givens;
}

/**
 * Render the current state.board into the grid, marking given vs editable cells
 * and the box separators. Editable cells are <input>s that submit a move on
 * change; given cells are read-only.
 */
function renderBoard() {
  if (!els.board || !state.board) {
    return;
  }

  els.board.innerHTML = '';
  els.board.classList.toggle('board--solved', state.solved);

  for (var r = 0; r < BOARD_SIZE; r++) {
    for (var c = 0; c < BOARD_SIZE; c++) {
      els.board.appendChild(buildCell(r, c));
    }
  }
}

/**
 * Build a single cell input for (row, col) reflecting state and givens.
 * @param {number} r 0-based row.
 * @param {number} c 0-based column.
 * @returns {HTMLInputElement} the configured cell input.
 */
function buildCell(r, c) {
  var value = state.board[r][c];
  var isGiven = state.givens && state.givens[r][c];

  var input = document.createElement('input');
  input.type = 'text';
  input.inputMode = 'numeric';
  input.className = 'cell';
  input.maxLength = 1;
  input.value = value === 0 ? '' : String(value);
  input.setAttribute('role', 'gridcell');
  input.setAttribute('aria-label', 'Row ' + (r + 1) + ', column ' + (c + 1));
  input.dataset.row = String(r);
  input.dataset.col = String(c);

  // Thick separators on the right/bottom edges of each 3x3 box (but not the
  // outermost edges, which the board frame already draws).
  if (c % BOX_SIZE === BOX_SIZE - 1 && c !== BOARD_SIZE - 1) {
    input.classList.add('cell--box-right');
  }
  if (r % BOX_SIZE === BOX_SIZE - 1 && r !== BOARD_SIZE - 1) {
    input.classList.add('cell--box-bottom');
  }

  // Given cells are fixed: read-only, visually distinct, and skipped by moves.
  if (isGiven || state.solved) {
    input.readOnly = true;
    if (isGiven) {
      input.classList.add('cell--given');
    }
  } else {
    input.addEventListener('change', onCellChange);
    input.addEventListener('input', sanitizeCellInput);
  }

  return input;
}

/**
 * Keep only a single digit 1-9 in an editable cell as the user types. Empty is
 * allowed (it just means "no move yet").
 * @param {Event} event
 */
function sanitizeCellInput(event) {
  var input = event.target;
  var digitsOnly = input.value.replace(/[^1-9]/g, '');
  input.value = digitsOnly.slice(-1); // keep the last valid digit typed
}

/**
 * Handle a committed edit to an editable cell by submitting the move.
 * @param {Event} event
 */
function onCellChange(event) {
  var input = event.target;
  var row = Number(input.dataset.row);
  var col = Number(input.dataset.col);
  var raw = input.value.trim();

  // An emptied cell is not a move; just leave it blank.
  if (raw === '') {
    return;
  }

  var value = Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > 9) {
    input.value = '';
    showMessage('Enter a digit from 1 to 9.', 'error');
    return;
  }

  handleSubmitMove(row, col, value, input);
}

// ── Action handlers (orchestrate API call → state → render → message) ─────────

/** Create a new game, then render its board. */
function handleNewGame() {
  clearMessage();
  setBusy(true);

  newGame()
    .then(function (response) {
      if (!response.ok) {
        showMessage(describeApiError(response), 'error');
        return;
      }
      adoptGame(response.data.gameId, response.data.board, computeGivens(response.data.board));
      state.solved = false;
      renderBoard();
      showMessage('New game started. Fill the empty cells.', 'info');
    })
    .catch(handleRequestError)
    .then(function () {
      setBusy(false);
    });
}

/** Submit a single move and reconcile the board with the response. */
function handleSubmitMove(row, col, value, input) {
  if (!state.gameId) {
    showMessage('Start a new game first.', 'error');
    return;
  }
  clearMessage();

  submitMove(state.gameId, row, col, value)
    .then(function (response) {
      if (!response.ok) {
        // 400 (bad shape) / 404 (unknown game): revert the cell, show why.
        if (input) {
          input.value = '';
        }
        showMessage(describeApiError(response), 'error');
        return;
      }

      var data = response.data;
      if (data.accepted === false) {
        // A rejected move is normal gameplay: leave the stored board unchanged
        // and clear the cell the player typed into.
        if (input) {
          input.value = '';
        }
        showMessage('Invalid move — it conflicts with the row, column, or box.', 'error');
        return;
      }

      // Accepted: adopt the returned board (givens are unchanged).
      state.board = data.board;
      if (data.solved) {
        state.solved = true;
        renderBoard();
        showMessage('Solved! The whole board is complete and correct.', 'success');
      } else {
        renderBoard();
        showMessage('Move accepted.', 'info');
      }
    })
    .catch(function (err) {
      if (input) {
        input.value = '';
      }
      handleRequestError(err);
    });
}

/** Ask the backend to solve the current board and render the completion. */
function handleSolve() {
  if (!state.gameId) {
    showMessage('Start a new game first.', 'error');
    return;
  }
  clearMessage();
  setBusy(true);

  solve(state.gameId)
    .then(function (response) {
      if (!response.ok) {
        showMessage(describeApiError(response), 'error');
        return;
      }

      var data = response.data;
      if (data.solved) {
        state.board = data.board;
        state.solved = true;
        renderBoard();
        showMessage('Puzzle solved by the backend.', 'success');
      } else {
        // solved:false with reason no_solution — a valid outcome, not an error.
        showMessage('No solution exists for this board.', 'error');
      }
    })
    .catch(handleRequestError)
    .then(function () {
      setBusy(false);
    });
}

/**
 * Central handler for thrown errors (not-configured or network failures).
 * @param {Error} err
 */
function handleRequestError(err) {
  if (err instanceof NotConfiguredError) {
    showNotConfigured();
  } else {
    showMessage('Network error — the API could not be reached.', 'error');
  }
}

// ── State + UI plumbing ────────────────────────────────────────────────────────

/**
 * Adopt a game into state and persist its id so a reload can re-fetch it.
 * @param {string} gameId
 * @param {number[][]} board
 * @param {boolean[][]} givens
 */
function adoptGame(gameId, board, givens) {
  state.gameId = gameId;
  state.board = board;
  state.givens = givens;
  persistGameId(gameId);
  if (els.gameId) {
    els.gameId.textContent = 'Game: ' + gameId;
  }
}

/** Persist the current game id in localStorage (best-effort). */
function persistGameId(gameId) {
  try {
    window.localStorage.setItem(GAME_ID_STORAGE_KEY, gameId);
  } catch (err) {
    // Storage unavailable — the in-memory id still works for this page load.
  }
}

/** Read a previously stored game id, or null. */
function readStoredGameId() {
  try {
    return window.localStorage.getItem(GAME_ID_STORAGE_KEY);
  } catch (err) {
    return null;
  }
}

/**
 * Enable/disable the action buttons while a request is in flight.
 * @param {boolean} busy
 */
function setBusy(busy) {
  if (els.newGameBtn) {
    els.newGameBtn.disabled = busy;
  }
  if (els.solveBtn) {
    // Solve is only meaningful once a game exists.
    els.solveBtn.disabled = busy || !state.gameId;
  }
}

/** Show the "API not configured" notice and disable the controls. */
function showNotConfigured() {
  showMessage(
    'The API endpoint is not configured. Set the deployed API URL in config.js ' +
      '(the API_Endpoint_URL from `just deploy`), then reload.',
    'error'
  );
  if (els.newGameBtn) {
    els.newGameBtn.disabled = true;
  }
  if (els.solveBtn) {
    els.solveBtn.disabled = true;
  }
}

// ── Wire-up on load ────────────────────────────────────────────────────────────

/**
 * Initialize the UI: resolve DOM references, wire buttons, and either show the
 * not-configured message or re-fetch a stored game / prompt for a new one.
 */
function init() {
  els.board = document.getElementById('board');
  els.message = document.getElementById('message');
  els.newGameBtn = document.getElementById('new-game-btn');
  els.solveBtn = document.getElementById('solve-btn');
  els.gameId = document.getElementById('game-id');

  if (els.newGameBtn) {
    els.newGameBtn.addEventListener('click', handleNewGame);
  }
  if (els.solveBtn) {
    els.solveBtn.addEventListener('click', handleSolve);
  }

  // If the API is not configured, show the notice and stop — no requests.
  if (!apiUrl('/games')) {
    showNotConfigured();
    return;
  }

  // If a game id was stored, try to re-fetch its board so a reload resumes play.
  var storedGameId = readStoredGameId();
  if (storedGameId) {
    resumeStoredGame(storedGameId);
  } else {
    showMessage('Press "New Game" to start.', 'info');
  }
}

/**
 * Attempt to reload a previously stored game. If it is gone (404) or fails,
 * fall back to prompting for a new game rather than blocking the UI.
 * @param {string} gameId
 */
function resumeStoredGame(gameId) {
  setBusy(true);
  getGame(gameId)
    .then(function (response) {
      if (response.ok && response.data && response.data.board) {
        adoptGame(gameId, response.data.board, computeGivens(response.data.board));
        state.solved = false;
        renderBoard();
        showMessage('Resumed your game.', 'info');
      } else {
        showMessage('Press "New Game" to start.', 'info');
      }
    })
    .catch(function () {
      // Network/not-configured: just invite a new game; don't crash.
      showMessage('Press "New Game" to start.', 'info');
    })
    .then(function () {
      setBusy(false);
    });
}

// Run init once the DOM is ready. (Scripts are at the end of <body>, but guard
// anyway so ordering changes don't break wire-up.)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

// Expose the task-12.2 API as globals for parity with 12.1 and for tests.
window.newTraceHeader = newTraceHeader;
window.apiFetch = apiFetch;
window.newGame = newGame;
window.getGame = getGame;
window.submitMove = submitMove;
window.solve = solve;
