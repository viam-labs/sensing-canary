# viamrtsp ONVIF H264 Setup Playbook

## Rule
Camera components MUST come from the ONVIF discovery service. Never manually add an rtsp camera component.

## Method
Setup is done entirely via `config_helper.py` CLI. No browser needed for setup.

## Steps

### 1. Add the viamrtsp module
Use `config_helper.py` to add the `viam:viamrtsp` module (version: latest-with-prerelease).

### 2. Add the ONVIF discovery service
Read `onvif_credentials` from `canary.json` (under the machine entry). Pass them as the discovery service attributes:

```bash
python3 config_helper.py --config canary.json --machine <MACHINE> \
  add-resource --kind service --api rdk:service:discovery \
  --model viam:viamrtsp:onvif --resource-name onvif-discovery \
  --attributes '{"credentials": <onvif_credentials array from canary.json>}'
```

The attributes must have the shape `{"credentials": [{"user": "...", "pass": "..."}]}`. The `onvif_credentials` field in `canary.json` is already an array of `{user, pass}` objects, so pass it directly as the `credentials` value.

### 3. Wait for startup
Wait 10 seconds for the module and discovery service to start. Check logs: `get-logs --num 50 --lookback 2`.

The ONVIF discovery worker scans the network in the background. The first scan starts when `DiscoverResources` is called, not on a timer, so step 4 will trigger it.

**Observe:**
- Did any errors fire during startup? Are they real failures or benign noise?
- Are there credential errors? Wrong user/pass should log clearly.
- Can you tell from the logs alone whether the module started successfully?
- Is there a clear "ready" signal, or do you have to guess?
- Are error levels accurate?

### 4. Discover and add H264 cameras
Run discovery and selectively add H264 cameras:

```bash
python3 config_helper.py --config canary.json --machine <MACHINE> discover --service onvif-discovery
```

Parse the JSON output. The `results` array contains discovery entries. Each entry has:
```json
{
  "name": "...",
  "api": "rdk:component:camera",
  "model": "viam:viamrtsp:rtsp",
  "attributes": {
    "rtsp_address": "rtsp://user:pass@host/path",
    "rtp_passthrough": true,
    "codec": "H264",
    "frame_rate": 30,
    "resolution": {"width": 1920, "height": 1080},
    "discovery_dep": "onvif-discovery"
  }
}
```

For each entry where `attributes.codec` is `"H264"`, add it:
```bash
python3 config_helper.py --config canary.json --machine <MACHINE> \
  add-resource-from-discovery-result --json '<entry as JSON string>'
```

Skip entries with other codecs (H265, MJPEG, etc.).

**If `count` is 0:** The network scan may still be running. Sleep 30 seconds, then retry `discover`. Retry up to 3 times total. If still empty after 3 retries, log the failure in setup_steps and move to the next profile.

### 5. Verify cameras running
Check via SDK (get_images) or logs that cameras are producing frames.

Check logs: `get-logs --num 50 --lookback 2`.

**Observe:**
- Are there RTSP connection errors? Do they explain what went wrong?
- Any errors that look scary but are actually harmless? Note the false alarm.
- Could you diagnose a real failure from these logs without source code access?

## After Setup
Run `get-config` to verify: camera has `rtsp_address`, `rtp_passthrough: true`, `codec` in attributes. Model is `viam:viamrtsp:rtsp`.

## Developer UX Observations

At every step, evaluate from the perspective of a developer debugging or setting up this module:

- **Error quality** — Do error messages explain what went wrong AND what to do about it? Or are they opaque?
- **Log noise** — Are logs cluttered with benign errors/warnings that drown out real issues?
- **Health visibility** — Can you tell at a glance whether the module/camera/service is healthy?
- **Debuggability** — If something broke, would the logs + UI give you enough to file a bug report without reading source code?
- **Accuracy** — Do error levels match severity?
- **Credential clarity** — If credentials are wrong, does the error message say so explicitly?
- **Failure modes** — If the camera is unreachable or the RTSP stream drops, do you get a useful error?

## Expected Outcome
- 1 module: `viam:viamrtsp` (latest-with-prerelease)
- 1 discovery service: `viam:viamrtsp:onvif` (named `onvif-discovery`)
- N cameras from discovery (model `viam:viamrtsp:rtsp`, with `rtp_passthrough: true`)
- Frames producing via SDK
