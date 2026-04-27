# Canary — Agentic QA Health Monitor

## Purpose

Act as a QA engineer: set up Viam machines from scratch, exercise camera modules via browser and SDK, collect raw health data over ~24 hours, and produce daily analysis reports.

## File Layout

```
canary/
  SKILL.md                          ← you are here
  canary.json                        ← config
  webrtc-stats/                      ← local WebRTC testing app (Vite + @viamrobotics/sdk)
    src/main.ts                      ← main entry point
    index.html                       ← UI shell
    package.json
    vite.config.ts                   ← dev server on port 5199
  profiles/
    __init__.py                      ← profile registry
    base.py                          ← base data collector
    sdk_test.py                      ← raw SDK data collector (full + probe modes)
    config_helper.py                 ← machine config CLI
    realsense/
      setup.md                       ← profile setup playbook
      collect.py                     ← profile data collection
    orbbec/
      setup.md                       ← profile setup playbook
      collect.py                     ← profile data collection
  runs/
    YYYY-MM-DD/                      ← one folder per day
      .lock                          ← run lock (prevents overlapping ticks)
      setup.json                     ← setup trace (once per day)
      report.md                      ← analysis report (generated at rollover)
      machine/                       ← machine-level telemetry
        HHMM_full_telegraf.json
        HHMM_full_logs.json
      <profile>/                     ← one per active profile (realsense, orbbec, etc.)
        webrtc.json                  ← WebRTC samples for this profile's camera
        samples/
          HHMM_full.json             ← full SDK collection (profile cameras only)
          HHMM_probe.json            ← lightweight probes (profile cameras only)
```

## Python Environment

**Always use the skill's own venv for all Python invocations.** Never use system Python.

### Setup (one-time)

```bash
cd <SKILL_DIR>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Usage

All `python3` commands in this skill must use the venv interpreter:

```bash
cd <SKILL_DIR>
.venv/bin/python3 profiles/sdk_test.py --config canary.json ...
.venv/bin/python3 profiles/config_helper.py --config canary.json ...
```

If the venv doesn't exist or is missing dependencies, create/update it before proceeding:

```bash
cd <SKILL_DIR>
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
```

**Why:** System Python may lack `Pillow` (needed for resolution cross-checks in profiles), have wrong `viam-sdk` version, or conflict with other tools. The venv pins exact versions via `requirements.txt` and keeps canary isolated.

---

## Configuration

`canary.json` fields:

- **machines[]**: `name`, `address`, `api_key_id`, `api_key`, `part_id`, `test_profiles[]`, `persistent_resources[]`, `telegraf_sensor`

- **logs**: `enabled`, `lookback_minutes`, `levels`
- **schedule**: `cron_expr`, `timezone`
- **alerts**: `slack_webhook`, `slack_token`, `slack_channel`
- **runs_dir**: output directory (default: `runs`)

## Cron Registration

```
cron(action="add", job={
  "name": "canary",
  "schedule": { "kind": "cron", "expr": "<schedule.cron_expr>", "tz": "<schedule.timezone>" },
  "payload": {
    "kind": "agentTurn",
    "message": "Run a canary tick. Read <skill_dir>/SKILL.md and follow the Tick Execution Flow."
  },
  "sessionTarget": "isolated",
  "enabled": true
})
```

---

## Daily Run Model

Instead of independent runs every 30 minutes, the canary accumulates data within a single daily run folder (`runs/YYYY-MM-DD/`):

- **First tick of the day**: Rollover (analyze previous day → Slack report) → full setup (CLI) → WebRTC (browser) → full SDK collection. Budget ~30 minutes for this.
- **Subsequent ticks**: Lightweight probe only (single get_images per camera + telegraf + logs). Takes ~2 minutes.
- **Lock file**: Prevents overlapping ticks. If a previous tick is still running, the new tick skips gracefully.

---

## Tick Execution Flow

Every cron tick follows this flow:

### 0. Pull latest code

```bash
cd <SKILL_DIR>
git pull --ff-only origin main
```

If the pull fails (e.g. local changes), log a warning and continue with the current code. Do not force-reset.

### 0.1. Determine state

```
SKILL_DIR = directory containing this SKILL.md
PROFILES_DIR = SKILL_DIR/profiles
RUNS_DIR = SKILL_DIR/<runs_dir from canary.json>
TIMEZONE = schedule.timezone from canary.json
TODAY = YYYY-MM-DD in TIMEZONE
NOW = HHMM in TIMEZONE
TODAY_DIR = RUNS_DIR/TODAY
LOCK_FILE = TODAY_DIR/.lock
TICK_START = current UTC timestamp (record this immediately — used for log lookback)
```

**Log lookback rule:** When collecting logs at any point during the tick, compute `lookback_minutes` as the number of minutes elapsed since `TICK_START` (rounded up, minimum 1). Do NOT use the static `lookback_minutes` from `canary.json` — that value is a default for probe ticks only. First ticks can run 10-30 minutes; using a fixed 30-minute window misses early startup logs.

### 1. Check lock

Check if `LOCK_FILE` exists:
- **Lock exists and age < 90 minutes** → Write `TODAY_DIR/machine/NOW_skipped.json` with `{"reason": "locked", "lock_age_minutes": N}`. Exit.
- **Lock exists and age ≥ 90 minutes** → Stale lock. Delete it, log a warning, proceed.
- **No lock** → Proceed.

Create `TODAY_DIR` (and `TODAY_DIR/machine/`) if they don't exist. Write `LOCK_FILE` with current timestamp.

### 2. Determine tick type

Check if `TODAY_DIR/setup.json` exists:
- **No** → This is the **first tick of the day**. Go to step 3 (Rollover + Setup + Full Collection).
- **Yes** → This is a **probe tick**. Go to step 7 (Probe Collection).

### 3. Rollover — analyze previous day

Find the most recent `runs/YYYY-MM-DD/` folder that is NOT today and HAS `setup.json`.

If found:
- Read all files from that folder: `setup.json`, `<profile>/webrtc.json`, `<profile>/samples/*.json`, `machine/*_telegraf.json`, `machine/*_logs.json`
- Run **Step 9: Analysis** on that data
- Write `report.md` into that folder
- Send Slack summary: post compact message via webhook, then upload `report.md` as a file attachment (see Step 9: Slack Summary)

If not found (first ever run), skip rollover.

### 4. General Setup

#### 4.1: Load config

Read `canary.json`. Create `TODAY_DIR` and `TODAY_DIR/samples/` if not already done.

#### 4.2: Clear machine config

Preserve persistent resources, clear everything else:

```bash
cd <PROFILES_DIR>
<SKILL_DIR>/.venv/bin/python3 config_helper.py --config ../canary.json --machine <MACHINE> \
  clear-resources --preserve <comma-separated persistent_resources>
```

#### 4.3: Profile Setup (CLI — no browser)

Setup is done entirely via `config_helper.py`. The browser is NOT used for setup — it's too unreliable with the Viam app's dynamic UI. Browser is only used later for WebRTC testing (step 6).

For each profile in `test_profiles`:

1. Read `profiles/<profile>/setup.md` for the module/discovery model names
2. Use `config_helper.py` to add modules, run discovery, and add cameras:

```bash
cd <PROFILES_DIR>
# Run discovery for each profile
<SKILL_DIR>/.venv/bin/python3 config_helper.py --config ../canary.json --machine <MACHINE> discover
# Add discovered resources
<SKILL_DIR>/.venv/bin/python3 config_helper.py --config ../canary.json --machine <MACHINE> add-resource-from-discovery-result
```

3. After all profiles, verify with `config_helper.py get-config`
4. Capture logs via `config_helper.py get-logs`

**`config_helper.py` commands used during setup:**
- `clear-resources` (step 4.2)
- `discover` (find cameras)
- `add-resource-from-discovery-result` (add cameras to config)
- `get-config` (verification)
- `get-version` (capture viam-server + module semvers)
- `get-logs` (raw log capture)

#### 5.1: Capture versions

After all profiles are set up and config is saved, capture exact versions:

```bash
cd <PROFILES_DIR>
<SKILL_DIR>/.venv/bin/python3 config_helper.py --config ../canary.json --machine <MACHINE> get-version
```

This returns viam-server version/platform/api_version and all module name/module_id/version entries.

#### Write setup dump

Write `TODAY_DIR/setup.json`. The `versions` field is mandatory — it records the exact semvers of viam-server and all modules used in this run:

```json
{
  "schema": "canary.setup.v1",
  "started_at": "ISO-8601",
  "completed_at": "ISO-8601",
  "machine": "name",
  "versions": {
    "viam_server": {
      "version": "v0.x.y",
      "platform": "linux/amd64",
      "api_version": "v0.x.y"
    },
    "modules": [
      { "name": "viam_realsense", "module_id": "viam:realsense", "version": "0.21.0-rc1" }
    ]
  },
  "cleared_config": { ... },
  "profiles": {
    "<profile>": {
      "setup_steps": [
        {
          "step": "description",
          "result": { ... },
          "logs_after": [ ... ],
          "elapsed_ms": 1234,
          "notes": "..."
        }
      ],
      "components_added": ["name"],
      "dev_ux_observations": [ "..." ],
      "errors": [],
      "total_setup_ms": 15000
    }
  },
  "final_config": { ... }
}
```

### 6. WebRTC Testing (local app)

WebRTC testing uses the local `webrtc-stats` app driven by a headless-Puppeteer HTTP server (`canary-server.js`). **No OpenClaw browser tool is needed** — the agent uses plain `curl`.

#### One-time setup (first ever run on the machine)

Install dependencies (includes Puppeteer + its bundled Chromium, ~200 MB one-time download):

```bash
cd <SKILL_DIR>/webrtc-stats
npm install
```

#### 6.1: Ensure the canary server is running

The canary server (`http://127.0.0.1:5200`) manages the Vite dev server and Puppeteer lifecycle. It persists across ticks and is reused automatically.

```bash
# Check if already up
curl -sf http://127.0.0.1:5200/health && echo "running" || echo "not running"
```

If not running, start it in the background:

```bash
cd <SKILL_DIR>/webrtc-stats
node canary-server.js &
sleep 2
curl -sf http://127.0.0.1:5200/health
```

The canary server automatically starts the Vite dev server on `127.0.0.1:5199` if it isn't already running.

#### 6.2: Run the WebRTC test via curl

```bash
curl -sf "http://127.0.0.1:5200/run?host=<ADDRESS>&key=<API_KEY>&keyId=<API_KEY_ID>&cameras=<CAM1>,<CAM2>&duration=60&interval=5" \
  -o /tmp/canary-webrtc-raw.json
```

Where `<ADDRESS>`, `<API_KEY>`, `<API_KEY_ID>` come from `canary.json`, and `<CAM1>,<CAM2>` are the camera component names from the machine config (one per profile).

This call **blocks** until the test finishes (duration × cameras + 90 s buffer). The server returns:
- **HTTP 200** — test JSON (schema `canary.webrtc.v2`)
- **HTTP 409** — a prior tick's test is still running; skip WebRTC for this tick and log a note
- **HTTP 500** — test error; check `{"error":"..."}` field

#### 6.3: Parse and write results

Read `/tmp/canary-webrtc-raw.json`. It contains a single JSON object matching schema `canary.webrtc.v2`:

```json
{
  "schema": "canary.webrtc.v2",
  "collectedAt": "ISO-8601",
  "host": "ADDRESS",
  "durationS": 60,
  "intervalS": 5,
  "cameras": [
    {
      "name": "realsense-348522073801",
      "streamStartTs": 1709654400000,
      "ttffMs": 78,
      "samples": [
        {
          "ts": 1709654405000,
          "elapsedMs": 5000,
          "totalFrames": 100,
          "droppedFrames": 0,
          "videoWidth": 1280,
          "videoHeight": 720,
          "currentTime": 4.5,
          "fps": 20
        }
      ],
      "finalState": "ok"
    }
  ]
}
```

Split the result by camera name and write per-profile files:
- Match each camera entry to its profile (by camera name → profile mapping from setup)
- Write `TODAY_DIR/<profile>/webrtc.json` for each profile

**No browser cleanup needed** — the canary server closes headless Chrome internally after each test.

**Note:** Both the canary server and the Vite dev server can be left running between ticks. They are lightweight and will be reused automatically.

### 6.6. Full SDK Collection

After closing the browser, run a full SDK collection as the first sample of the day:

#### Build runtime config

Read machine config via `get-config`. Match components to profiles by model:
- `viam:camera:realsense` → realsense
- `viam:orbbec:astra2` → orbbec
- `viam:viamrtsp:rtsp` → viamrtsp
- `viam:system-audio:microphone` → system-audio
- `viam:system-audio:speaker` → system-audio

Write `/tmp/canary-runtime.json` with machine credentials + all discovered cameras. **Always include the `model` field** — profiles use it to validate they're running against the correct hardware:
```json
{
  "machines": [{
    "name": "...", "address": "...", "api_key_id": "...", "api_key": "...", "part_id": "...",
    "cameras": [
      { "name": "realsense-348522073801", "profile": "realsense", "model": "viam:camera:realsense", "type": "3d" },
      { "name": "orbbec-astra2", "profile": "orbbec", "model": "viam:orbbec:astra2", "type": "3d" },
      { "name": "rtsp-cam-1", "profile": "viamrtsp", "model": "viam:viamrtsp:rtsp", "type": "2d" }
    ],
    "telegraf_sensor": { "name": "telegraf-sensor" }
  }],
  "logs": { "enabled": true, "lookback_minutes": 30, "levels": [] }
}
```

#### Run full collector

```bash
cd <PROFILES_DIR>
<SKILL_DIR>/.venv/bin/python3 sdk_test.py --config /tmp/canary-runtime.json --output-dir <TODAY_DIR> --tag <NOW>_full
```

This writes profile-scoped output:
- `<TODAY_DIR>/<profile>/samples/<NOW>_full.json` — per-profile camera data (schema `canary.dump.v2`)
- `<TODAY_DIR>/machine/<NOW>_full_telegraf.json` — telegraf readings (schema `canary.telegraf.v1`)
- `<TODAY_DIR>/machine/<NOW>_full_logs.json` — machine logs (schema `canary.logs.v1`):
  ```json
  {
    "schema": "canary.logs.v1",
    "collected_at": "ISO-8601",
    "machine": "name",
    "part_id": "...",
    "timestamp": "ISO-8601",
    "config": { "num_entries": 0, "lookback_minutes": 30, "levels": [] },
    "fetch_error": null,
    "entries": [ { "time": "ISO-8601", "level": "info", "logger": "rdk", "message": "...", "caller": {}, "stack": null } ]
  }
  ```
  Note: `entries` is a **top-level key** — read as `data["entries"]`, not `data["logs"]["entries"]`.

**Backward compat:** The old `-o` flag still works for combined single-file output (schema `canary.dump.v1`). Use `--output-dir`/`--tag` for all new runs.

#### Release lock and exit

Bundle the lock delete into the same exec as the sdk_test.py call:

```bash
<SKILL_DIR>/.venv/bin/python3 sdk_test.py --config /tmp/canary-runtime.json --output-dir <TODAY_DIR> --tag <NOW>_full \
  && rm -f "$LOCK_FILE"
```

This ensures the lock is released atomically with the collection completing, even if the LLM session is aborted after the exec returns.

---

### 7. Probe Collection (subsequent ticks)

Lightweight — no browser, no setup, no point cloud.

#### 7.1: Build runtime config

Same as step 6.6 — read machine config, build `/tmp/canary-runtime.json`.

If the machine config has no cameras (setup failed or config was cleared externally), log an error to `machine/NOW_skipped.json` and release the lock.

#### 7.2: Run probe collector

```bash
cd <PROFILES_DIR>
<SKILL_DIR>/.venv/bin/python3 sdk_test.py --config /tmp/canary-runtime.json --probe --output-dir <TODAY_DIR> --tag <NOW>_probe \
  && rm -f "$LOCK_FILE"
```

#### 7.3: Exit

Probe tick complete. Lock was released in the exec above.

---

## Step 9: Analysis (at rollover or on demand)

### Load data

Read all files from the target run folder (`runs/YYYY-MM-DD/`):
- `setup.json` — setup trace
- `<profile>/webrtc.json` — WebRTC samples (one per profile)
- `<profile>/samples/*_full.json` — full SDK collections (per profile, schema `canary.dump.v2`)
- `<profile>/samples/*_probe.json` — lightweight probes (per profile, schema `canary.dump.v2`)
- `machine/*_telegraf.json` — telegraf readings (schema `canary.telegraf.v1`)
- `machine/*_logs.json` — machine logs (schema `canary.logs.v1`)

**Legacy compat:** If `samples/*_full.json` or `samples/*_probe.json` exist at the run root (v1 layout), read those instead. This handles runs that started before the v2 output migration.

### Analyze

**Setup (from setup.json):**
- Exact viam-server version + all module semvers (from `versions` field)
- Discovery results, setup timing
- Developer UX observations collated across profiles

**WebRTC (from `<profile>/webrtc.json`):**
- Per-profile TTFF (`ttffMs` field, already computed)
- FPS time series (from `fps` field in samples, or compute from frame deltas)
- Dropped frames, resolution consistency
- Compare TTFF across profiles if multiple exist
- Schema v2 (`canary.webrtc.v2`) has `ttffMs` directly; legacy v1 uses `ttff_ts - stream_start_ts`

**SDK — Trend Analysis (from `<profile>/samples/`):**

Analyze each profile separately, then compare:
- **Latency**: p50/p95/p99 of get_images latency across all probes (per profile)
- **FPS stability**: from full collection FPS samples (per profile)
- **Failure rate**: % of probes with SDK call errors (per profile)
- **Frame consistency**: data_bytes variance across probes (sudden drops = concern)
- **Cross-profile comparison**: latency/reliability differences between realsense and orbbec

**Telegraf — Resource Trends (from `machine/*_telegraf.json`):**
- **Memory**: baseline (first sample) vs final, linear regression on RSS. Positive slope = leak candidate. Flag if slope suggests >10% growth per 24h.
- **CPU**: mean, max, sustained high periods
- **Temperature**: max, trend direction, thermal throttle risk
- **Disk**: usage trend
- **Load**: mean load1 vs n_cpus

**Logs — Noise & Severity (from `machine/*_logs.json`):**
Read entries as `data["entries"]` (top-level key per `canary.logs.v1` schema).
- Total error/fatal count across the day
- Top 5 recurring log messages (grouped by message template)
- Error rate per hour (are errors bursty or steady?)
- New error classes (appeared only in later probes, not in first)
- Fatal entries highlighted

**Stability:**
- Any probes that failed to connect (module crash/restart?)
- Lock contention (any skipped ticks?)
- Config state consistency (did components disappear mid-day?)

### Generate report

Write `TODAY_DIR/report.md` with sections for each category above.

### Slack Summary

Compact format for Slack (no markdown tables, no headers):

```
🐤 Canary Report — YYYY-MM-DD

*realsense (D435i)*
get_images p50: XXms  p95: XXms
PCD: XXms avg (N calls)
WebRTC TTFF: XXs
Probes: XX/XX ok

*orbbec (Astra2)*
get_images p50: XXms  p95: XXms
PCD: XXms avg (N calls)
WebRTC TTFF: XXs
Probes: XX/XX ok

*viamrtsp (ONVIF/H264)*
get_images p50: XXms  p95: XXms
WebRTC TTFF: XXs (RTP passthrough)
Probes: XX/XX ok

*Machine*
Memory: baseline XXM → final XXM (slope: +X.X MB/hr)
Errors: XX total (XX unique)

*Top Issues*
1. [ERROR] repeated message (×42)
2. [WARN] another message (×15)

*UX Notes*
- observation from setup
```

---

## Thresholds (analysis only)

- Memory > 80% or slope suggests >10% growth per 24h → ⚠️
- CPU idle < 20% sustained → ⚠️
- Swap > 0 → ⚠️
- Load1 > 2× n_cpus → ⚠️
- Zombies > 0 → ⚠️
- Temp > 80°C → ⚠️
- Disk > 90% → ⚠️
- Errors in logs → ⚠️
- get_images failures → ⚠️
- >2 skipped ticks (lock contention) → ⚠️

---

## Troubleshooting

- **config_helper.py errors**: Check API key and part_id
- **Discovery empty**: Module may not have started — wait, check logs
- **webrtc-stats dev server not running**: `cd <SKILL_DIR>/webrtc-stats && npm run dev &`
- **WebRTC test shows TTFF timeout**: Check viam-server logs — camera module may not be producing frames
- **Telegraf missing after clear**: Check `persistent_resources`
- **Lock stuck**: If `.lock` is older than 90 min, it's stale — delete and proceed
- **First tick too slow**: Setup + browser + WebRTC can take 30-60 min. Lock prevents overlap.
- **Memory climbing across probes**: Check `ps aux --sort=-%mem | head -20` — if Chromium renderers are top consumers, the canary server may have leaked a headless Chrome process. Kill them with `pkill -f "chrome"`. The canary server closes Puppeteer after each test, but a crash may leave orphaned processes.

## Rules

1. **Never add components/modules outside setup.** Setup is intentional and logged.
2. **WebRTC testing uses the canary server (curl), not the OpenClaw browser tool.** The canary server at `127.0.0.1:5200` drives headless Puppeteer internally. Never use `browser(action=...)` for this skill.
3. **Setup uses config_helper CLI only.** No browser involved in setup — only `curl` for WebRTC testing (step 6). This keeps setup fast and reliable.
4. **Steps 1-6.5 are collection only.** No interpretation, no pass/fail, no comparisons.
5. **Developer UX observations during setup.** Error quality, log noise, health visibility, debuggability.
6. **Self-contained.** All config from `canary.json` and this directory.
7. **Cron for recurring work, not subagents.**
8. **Dumps are source of truth.** Scriptable by humans.
9. **persistent_resources survive clears.**
10. **Lock before work, unlock after.** Never leave a lock behind — always clean up, even on error.
11. **First tick gets 30 minutes.** Budget for CLI setup + WebRTC (curl/Puppeteer) + full SDK.
12. **Probe ticks are lightweight.** No WebRTC, no point cloud, no source filters.
13. **No manual browser management needed.** The canary server handles Puppeteer lifecycle. If headless Chrome processes accumulate unexpectedly, `pkill -f chrome` is the cleanup.
