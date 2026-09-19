// config.js — API base URL resolution for the X-Ray Sudoku frontend.
//
// Script style: this is a no-build static site (no bundler, no build step), so
// files are plain <script> tags loaded in order and communicate through a small
// number of globals on `window`. config.js is loaded BEFORE app.js so that
// `window.APP_CONFIG` and `apiBaseUrl()` exist by the time app.js runs.
//
// ── How the API URL gets here (the two-step deployment handoff) ──────────────
// The backend is deployed with `just deploy` (cdk deploy), which prints the
// `API_Endpoint_URL` stack output. Because Amplify Hosting serves these files
// as-is with NO build step, there is no environment-variable substitution at
// build time. The operator therefore sets the URL by editing the placeholder
// below to the deployed `API_Endpoint_URL` (see design.md "Deployment Handoff",
// Requirement 10.4).
//
// Leave the placeholder as-is in source control; it is replaced per environment
// at deploy time. When it is left unset, apiBaseUrl() returns null so the UI can
// show a clear "not configured" message instead of firing requests at a bogus
// host.

// The single source of configuration for the frontend. The operator replaces
// `apiBaseUrl` with the CDK `API_Endpoint_URL` output after deploying.
window.APP_CONFIG = window.APP_CONFIG || {
  // OPERATOR: set this to the CDK `API_Endpoint_URL` stack output, e.g.
  //   "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
  // Set to the deployed XraySudokuDemoStack ApiEndpointUrl (us-east-1). The
  // literal "__API_ENDPOINT_URL__" is the not-configured sentinel.
  apiBaseUrl: 'https://bd3vvh5j4d.execute-api.us-east-1.amazonaws.com/prod',
};

// The unset placeholder value. Kept in one place so the check below and any
// future callers agree on what "not configured" looks like.
window.APP_CONFIG_PLACEHOLDER = '__API_ENDPOINT_URL__';

/**
 * Return the configured API base URL with any trailing slash removed, or null
 * when the operator has not set it yet.
 *
 * Trimming the trailing slash lets callers build request paths by simple
 * concatenation (`apiBaseUrl() + "/games"`) without risking a double slash.
 *
 * @returns {string|null} the normalized base URL, or null if not configured.
 */
function apiBaseUrl() {
  var configured = window.APP_CONFIG && window.APP_CONFIG.apiBaseUrl;

  // Treat missing, blank, or the untouched placeholder as "not configured".
  if (!configured || configured === window.APP_CONFIG_PLACEHOLDER) {
    return null;
  }

  // Normalize by stripping a single trailing slash so path concatenation is
  // predictable regardless of how the operator pasted the URL.
  return configured.replace(/\/+$/, '');
}

// Expose the getter as a global for app.js (loaded after this file).
window.apiBaseUrl = apiBaseUrl;
