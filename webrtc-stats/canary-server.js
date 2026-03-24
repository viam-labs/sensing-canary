#!/usr/bin/env node
/**
 * canary-server.js — HTTP shim for the canary WebRTC stats app.
 *
 * Exposes two endpoints so the canary skill can collect WebRTC stats with
 * plain curl instead of the OpenClaw browser tool:
 *
 *   GET /health
 *       Returns {"ok":true} — use to check whether the server is up.
 *
 *   GET /run?host=HOST&key=KEY&keyId=KEY_ID&cameras=cam1,cam2[&duration=60][&interval=5][&ttffTimeout=30000]
 *       Runs the WebRTC test via headless Puppeteer against the Vite dev app.
 *       Blocks until complete (or timeout).
 *       Returns the canary.webrtc.v2 JSON on success (200) or an error JSON on failure (500).
 *       Only one test can run at a time; concurrent requests receive 409.
 *
 * Usage:
 *   node canary-server.js [--port 5200] [--vite-port 5199]
 *   # or via npm:
 *   npm run server
 *
 * The server manages the Vite dev server lifecycle — it will start it
 * automatically if not already running on VITE_PORT.
 */

import http from 'http';
import { URL } from 'url';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';
import { dirname } from 'path';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const __dirname = dirname(fileURLToPath(import.meta.url));

const args = process.argv.slice(2);
const getArg = (flag, def) => {
  const i = args.indexOf(flag);
  return i !== -1 && args[i + 1] ? args[i + 1] : def;
};

const SERVER_PORT = parseInt(getArg('--port', '5200'), 10);
const VITE_PORT   = parseInt(getArg('--vite-port', '5199'), 10);
const VITE_URL    = `http://127.0.0.1:${VITE_PORT}`;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Resolve after ms milliseconds. */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Returns true if the given HTTP URL responds with a non-5xx status. */
async function pingServer(url) {
  return new Promise((resolve) => {
    http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode < 500);
    }).on('error', () => resolve(false));
  });
}

/** Start the Vite dev server and wait until it is ready (max 30 s). */
async function ensureViteRunning() {
  if (await pingServer(VITE_URL)) return;

  console.error('[canary-server] Starting Vite dev server...');
  const child = spawn('npm', ['run', 'dev'], {
    cwd: __dirname,
    detached: true,
    stdio: 'ignore',
  });
  child.unref();

  for (let i = 0; i < 60; i++) {
    await sleep(500);
    if (await pingServer(VITE_URL)) {
      console.error('[canary-server] Vite dev server ready.');
      return;
    }
  }
  throw new Error(`Vite dev server on port ${VITE_PORT} did not start within 30 s`);
}

/** Send a JSON response. */
function jsonResponse(res, status, body) {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

// ---------------------------------------------------------------------------
// In-flight guard (one test at a time)
// ---------------------------------------------------------------------------

let running = false;

// ---------------------------------------------------------------------------
// WebRTC test runner
// ---------------------------------------------------------------------------

async function runWebrtcTest(params) {
  // Lazy-import puppeteer so startup is fast even if puppeteer isn't installed yet.
  const puppeteer = (await import('puppeteer')).default;

  await ensureViteRunning();

  const {
    host, key, keyId, cameras,
    duration = '60',
    interval = '5',
    ttffTimeout = '30000',
  } = params;

  const pageUrl = new URL(VITE_URL);
  pageUrl.searchParams.set('host',        host);
  pageUrl.searchParams.set('key',         key);
  pageUrl.searchParams.set('keyId',       keyId);
  pageUrl.searchParams.set('cameras',     cameras);
  pageUrl.searchParams.set('duration',    duration);
  pageUrl.searchParams.set('interval',    interval);
  pageUrl.searchParams.set('ttffTimeout', ttffTimeout);

  const camCount   = cameras.split(',').filter(Boolean).length;
  const durationS  = parseInt(duration, 10);
  // Allow duration-per-camera + 90 s buffer for connection / TTFF
  const testTimeoutMs = (durationS * camCount + 90) * 1000;

  console.error(`[canary-server] Launching headless Chrome → ${pageUrl}`);

  const browser = await puppeteer.launch({
    headless: true,
    // Raise the CDP protocol timeout to comfortably exceed the test duration.
    // The default 180s is too short for multi-camera runs (e.g. 3 cams × 60s = 180s + buffer).
    protocolTimeout: testTimeoutMs + 60_000,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--use-fake-ui-for-media-stream',
      '--disable-web-security',          // allow localhost SDK WS connections
      '--disable-features=IsolateOrigins,site-per-process',
    ],
  });

  try {
    const page = await browser.newPage();

    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        console.error(`[page error] ${msg.text()}`);
      }
    });
    page.on('pageerror', (err) => console.error(`[page exception] ${err.message}`));

    await page.goto(pageUrl.toString(), { waitUntil: 'networkidle0', timeout: 30_000 });

    console.error(`[canary-server] Waiting up to ${Math.round(testTimeoutMs / 1000)} s for test completion...`);
    await page.waitForFunction(
      () => document.querySelector('#json-output')?.getAttribute('data-complete') === 'true',
      { timeout: testTimeoutMs }
    );

    const result = await page.evaluate(() => window.__canaryWebrtcResult);
    console.error('[canary-server] Test complete.');
    return result;
  } finally {
    await browser.close().catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${SERVER_PORT}`);

  // ── GET /health ──────────────────────────────────────────────────────────
  if (req.method === 'GET' && url.pathname === '/health') {
    return jsonResponse(res, 200, { ok: true });
  }

  // ── GET /run ─────────────────────────────────────────────────────────────
  if (req.method === 'GET' && url.pathname === '/run') {
    const required = ['host', 'key', 'keyId', 'cameras'];
    const missing  = required.filter((k) => !url.searchParams.get(k));
    if (missing.length) {
      return jsonResponse(res, 400, {
        error: `Missing required params: ${missing.join(', ')}`,
        usage: '/run?host=HOST&key=KEY&keyId=KEY_ID&cameras=cam1,cam2[&duration=60][&interval=5]',
      });
    }

    if (running) {
      return jsonResponse(res, 409, { error: 'A test is already running. Retry later.' });
    }

    running = true;
    try {
      const params = Object.fromEntries(url.searchParams.entries());
      const result = await runWebrtcTest(params);
      return jsonResponse(res, 200, result);
    } catch (err) {
      console.error(`[canary-server] Test failed: ${err.message}`);
      return jsonResponse(res, 500, { error: err.message });
    } finally {
      running = false;
    }
  }

  // ── 404 ──────────────────────────────────────────────────────────────────
  jsonResponse(res, 404, { error: 'Not found', endpoints: ['/health', '/run'] });
});

server.listen(SERVER_PORT, '127.0.0.1', () => {
  console.error(`[canary-server] Listening on http://127.0.0.1:${SERVER_PORT}`);
});

process.on('SIGTERM', () => { server.close(); process.exit(0); });
process.on('SIGINT',  () => { server.close(); process.exit(0); });
