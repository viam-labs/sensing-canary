# system-audio Setup Playbook

## Rule
Audio components MUST come from the discovery service. Never manually add a microphone or speaker component.

## Method
Setup is done entirely via `config_helper.py` CLI. No browser needed for setup.

## Steps

### 1. Add the system-audio module
Use `config_helper.py` to add the `viam:system-audio` module (version: latest-with-prerelease).

### 2. Add the discovery service
Add service: model `viam:system-audio:discovery`, API `rdk:service:discovery`.

```bash
python3 config_helper.py --config canary.json --machine <MACHINE> \
  add-resource --kind service --api rdk:service:discovery \
  --model viam:system-audio:discovery --resource-name audio-discovery
```

### 3. Wait for startup
Wait 10 seconds for the module and discovery service to start. Check logs: `get-logs --num 50 --lookback 2`.

**Observe:**
- Did any errors fire during startup? Are they real failures or benign noise?
- Are there PortAudio initialization errors? Do they explain what went wrong?
- Can you tell from the logs alone whether the module started successfully?
- Is there a clear "ready" signal, or do you have to guess?
- Are error levels accurate?

### 4. Discover and add audio devices
Run discovery and add discovered microphones and speakers:

```bash
python3 config_helper.py --config canary.json --machine <MACHINE> discover --service audio-discovery
```

Parse the JSON output. The `results` array contains discovery entries. Each entry has:
```json
{
  "name": "microphone-1",
  "api": "rdk:component:audio_in",
  "model": "viam:system-audio:microphone",
  "attributes": {
    "device_name": "Built-in Microphone",
    "device_id": "BuiltInMicrophoneDevice",
    "sample_rate": 44100,
    "num_channels": 1
  }
}
```

For speakers, entries look like:
```json
{
  "name": "speaker-1",
  "api": "rdk:component:audio_out",
  "model": "viam:system-audio:speaker",
  "attributes": {
    "device_name": "Built-in Output",
    "device_id": "BuiltInSpeakerDevice",
    "sample_rate": 44100,
    "num_channels": 2
  }
}
```

For each discovered device, add it:
```bash
python3 config_helper.py --config canary.json --machine <MACHINE> \
  add-resource-from-discovery-result --json '<entry as JSON string>'
```

**If `count` is 0:** The PortAudio enumeration may have failed. Check logs for device access errors. Sleep 10 seconds, then retry `discover`. Retry up to 3 times total. If still empty after 3 retries, log the failure in setup_steps and move to the next profile.

### 5. Verify audio devices running
Check via SDK (`get_properties` for microphones and speakers, short `get_audio` for microphones) or logs that devices are accessible.

Check logs: `get-logs --num 50 --lookback 2`.

**Observe:**
- Are there device access errors? Do they explain what went wrong?
- Any errors that look scary but are actually harmless? Note the false alarm.
- Could you diagnose a real failure from these logs without source code access?

### 6. Test get_audio streaming modes (microphones only)
For each discovered microphone, test the three `get_audio` streaming modes. These exercise different code paths in the module and verify that duration control and historical replay work correctly.

#### 6a. Set duration
Call `get_audio` with a fixed duration (e.g. 2 seconds). The stream should deliver audio chunks and then end naturally when the duration elapses.

```
get_audio(codec="pcm16", duration_seconds=2.0, previous_timestamp_ns=0)
```

**Observe:**
- Does the stream terminate cleanly after the requested duration?
- Is the total audio received roughly consistent with `duration × sample_rate × sample_size × channels`?
- Are there any errors or hangs at stream end?

#### 6b. Infinite duration with stop
Call `get_audio` with `duration_seconds=0` (infinite stream). Read chunks for ~3 seconds, then cancel/close the stream from the client side.

```
get_audio(codec="pcm16", duration_seconds=0, previous_timestamp_ns=0)
# ... read chunks for ~3 seconds ...
# cancel the stream
```

**Observe:**
- Does the infinite stream start delivering chunks immediately?
- Does client-side cancellation work cleanly, or does it produce errors/warnings in the logs?
- Are there any resource leaks or zombie goroutines after cancellation? (Check logs for unusual activity.)
- Is TTFC (time to first chunk) comparable to the set-duration case?

#### 6c. Historical audio (previous_timestamp_ns)
First, record the current time in nanoseconds. Wait a few seconds (so there's buffered audio), then call `get_audio` with `previous_timestamp_ns` set to the recorded timestamp. This requests audio starting from that past point in time.

```
# Record timestamp, wait, then request historical audio
get_audio(codec="pcm16", duration_seconds=2.0, previous_timestamp_ns=<recorded_ns>)
```

**Observe:**
- Does historical audio arrive? Or does the module error/return empty?
- Is TTFC faster than live streaming? (Historical data is already buffered, so delivery should be near-instant.)
- Is the total audio data consistent with the requested duration?
- If the timestamp is too far in the past (beyond the circular buffer), does the module return a useful error or silently return partial data?

## After Setup
Run `get-config` to verify: audio components have `device_id` in attributes, model is `viam:system-audio:microphone` or `viam:system-audio:speaker`.

## Developer UX Observations

At every step, evaluate from the perspective of a developer debugging or setting up this module:

- **Error quality** — Do error messages explain what went wrong AND what to do about it? Or are they opaque?
- **Log noise** — Are logs cluttered with benign errors/warnings that drown out real issues?
- **Health visibility** — Can you tell at a glance whether the module/audio device/service is healthy?
- **Debuggability** — If something broke, would the logs + UI give you enough to file a bug report without reading source code?
- **Accuracy** — Do error levels match severity?
- **Device clarity** — Is it obvious which physical device maps to which component? Are device IDs stable across reboots?
- **Failure modes** — If an audio device is disconnected or busy, do you get a useful error or a cryptic crash?

## Expected Outcome
- 1 module: `viam:system-audio` (latest-with-prerelease)
- 1 discovery service: `viam:system-audio:discovery` (named `audio-discovery`)
- N microphones from discovery (model `viam:system-audio:microphone`, with `device_id` in attributes)
- N speakers from discovery (model `viam:system-audio:speaker`, with `device_id` in attributes)
- Microphones accessible via `get_audio`, speakers accessible via `play`
