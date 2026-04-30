"""
system-audio test profile — raw data collection for microphones and speakers.

Collects get_properties, get_audio stream metrics (microphone), play/do_command
latency (speaker), and discovery validation. Handles both audio_in and audio_out
components in a single profile based on the component's model.

All data is raw — no pass/fail judgments.
"""

import asyncio
import hashlib
import math
import struct
import time
from typing import Optional
from datetime import datetime, timezone

from google.protobuf.json_format import MessageToDict
from viam.components.audio_in import AudioIn
from viam.components.audio_out import AudioOut
from viam.media.audio import AudioCodec
from viam.proto.common import AudioInfo
from viam.services.discovery import DiscoveryClient

from profiles import register

# ---------------------------------------------------------------------------
# Expected constants for viam:system-audio module
# ---------------------------------------------------------------------------

ACCEPTED_MODELS = {
    "viam:system-audio:microphone",
    "viam:system-audio:speaker",
}

MICROPHONE_MODELS = {"viam:system-audio:microphone"}
SPEAKER_MODELS = {"viam:system-audio:speaker"}

# Discovery
EXPECTED_DISCOVERY_MODEL_MIC = "viam:system-audio:microphone"
EXPECTED_DISCOVERY_MODEL_SPK = "viam:system-audio:speaker"
EXPECTED_DISCOVERY_API_MIC = "rdk:component:audio_in"
EXPECTED_DISCOVERY_API_SPK = "rdk:component:audio_out"

# Audio stream test defaults
DEFAULT_STREAM_CODEC = AudioCodec.PCM16
DEFAULT_STREAM_DURATION_S = 3.0
CODEC_TEST_DURATION_S = 1.0
TEST_CODECS = [AudioCodec.PCM16, AudioCodec.PCM32, AudioCodec.PCM32_FLOAT, AudioCodec.MP3]
DEFAULT_LATENCY_SAMPLE_DURATION_S = 1.0
DEFAULT_LATENCY_SAMPLES = 5

# Infinite-duration stream test
INFINITE_STREAM_LISTEN_S = 3.0

# Historical audio test
HISTORICAL_WAIT_S = 3.0
HISTORICAL_DURATION_S = 2.0

# Sine wave for speaker play() test
SINE_SAMPLE_RATE = 16000
SINE_DURATION_S = 0.5
SINE_FREQ_HZ = 440
SINE_NUM_CHANNELS = 1


def _generate_sine_pcm16(sample_rate=SINE_SAMPLE_RATE, duration_s=SINE_DURATION_S,
                          freq_hz=SINE_FREQ_HZ, num_channels=SINE_NUM_CHANNELS) -> bytes:
    """Generate a short PCM16 sine wave for speaker testing."""
    num_samples = int(sample_rate * duration_s)
    samples = []
    for i in range(num_samples):
        value = int(32767 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        for _ in range(num_channels):
            samples.append(struct.pack("<h", value))
    return b"".join(samples)


class SystemAudioProfile:
    """system-audio raw data collection for microphones and speakers."""

    name = "system-audio"

    def __init__(self, camera_config):
        self.config = camera_config
        self.cam_name = camera_config["name"]
        self.profile_config = camera_config.get("profile_config", {})

    def _check_model(self, robot) -> Optional[str]:
        """Validate component model matches this profile. Returns error string or None."""
        model = self.config.get("model")
        if model and model not in ACCEPTED_MODELS:
            return (
                f"Model mismatch: component '{self.cam_name}' has model '{model}' "
                f"but {self.name} profile only accepts {ACCEPTED_MODELS}"
            )
        return None

    def _is_microphone(self) -> bool:
        """Check if this component is a microphone based on its model."""
        return self.config.get("model", "") in MICROPHONE_MODELS

    def _is_speaker(self) -> bool:
        """Check if this component is a speaker based on its model."""
        return self.config.get("model", "") in SPEAKER_MODELS

    async def run(self, robot) -> dict:
        """Collect all raw data for the audio component."""
        result = {
            "component": self.cam_name,
            "profile": self.config.get("profile", self.name),
            "config": self.config,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component_type": None,
            "get_properties": None,
            "profile_data": {},
            "errors": [],
        }

        model_mismatch = self._check_model(robot)
        if model_mismatch:
            result["errors"].append(model_mismatch)
            return result

        if self._is_microphone():
            result["component_type"] = "microphone"
            await self._run_microphone_tests(robot, result)
        elif self._is_speaker():
            result["component_type"] = "speaker"
            await self._run_speaker_tests(robot, result)
        else:
            result["errors"].append(
                f"Cannot determine component type from model '{self.config.get('model')}'. "
                f"Expected one of {ACCEPTED_MODELS}"
            )
            return result

        # Discovery validation (runs for both mic and speaker)
        result["profile_data"]["discovery"] = await self._test_discovery(robot)

        return result

    # ------------------------------------------------------------------
    # Microphone tests
    # ------------------------------------------------------------------

    async def _run_microphone_tests(self, robot, result):
        """Run all microphone test surfaces."""
        try:
            mic = AudioIn.from_robot(robot, self.cam_name)
        except Exception as e:
            result["errors"].append(f"AudioIn not found: {e}")
            return

        result["get_properties"] = await self._collect_mic_properties(mic)

        # Stream test and latency samples can run concurrently
        stream_task = asyncio.create_task(self._collect_audio_stream(mic))
        staleness_task = asyncio.create_task(self._collect_audio_staleness(mic))
        stream_data, staleness_data = await asyncio.gather(stream_task, staleness_task)

        result["profile_data"]["audio_stream"] = stream_data
        result["profile_data"]["audio_staleness"] = staleness_data
        result["profile_data"]["latency_samples"] = await self._collect_mic_latency_samples(mic)

        # Multi-codec test
        result["profile_data"]["codec_test"] = await self._test_codecs(mic)

        # Streaming mode tests: infinite-with-stop and historical replay
        result["profile_data"]["infinite_stream"] = await self._test_infinite_stream(mic)
        result["profile_data"]["historical_audio"] = await self._test_historical_audio(mic)

    async def _collect_mic_properties(self, mic) -> dict:
        """Call get_properties on a microphone and record raw data."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw": None,
            "error": None,
        }

        t0 = time.monotonic()
        try:
            props = await mic.get_properties()
            elapsed_ms = (time.monotonic() - t0) * 1000
        except Exception as e:
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["error"] = str(e)
            return result

        result["latency_ms"] = round(elapsed_ms, 1)
        result["raw"] = {
            "sample_rate_hz": getattr(props, "sample_rate_hz", None),
            "num_channels": getattr(props, "num_channels", None),
        }

        return result

    async def _collect_audio_stream(self, mic) -> dict:
        """Request a short audio stream and measure delivery metrics.

        Analogous to camera get_images — measures time to first chunk (TTFC),
        chunk delivery rate, total bytes, and audio info consistency.
        """
        codec = self.profile_config.get("stream_codec", DEFAULT_STREAM_CODEC)
        duration_s = self.profile_config.get("stream_duration_s", DEFAULT_STREAM_DURATION_S)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "codec_requested": codec,
            "duration_requested_s": duration_s,
            "ttfc_ms": None,
            "chunk_count": 0,
            "total_bytes": 0,
            "total_elapsed_ms": None,
            "audio_info_samples": [],
            "chunk_sizes": [],
            "error": None,
        }

        t0 = time.monotonic()
        try:
            stream = await mic.get_audio(
                codec=codec,
                duration_seconds=duration_s,
                previous_timestamp_ns=0,
            )
        except Exception as e:
            result["total_elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["error"] = f"get_audio call failed: {e}"
            return result

        first_chunk_received = False
        chunk_count = 0
        total_bytes = 0
        chunk_sizes = []
        audio_info_samples = []

        try:
            async for chunk in stream:
                now = time.monotonic()
                if not first_chunk_received:
                    result["ttfc_ms"] = round((now - t0) * 1000, 1)
                    first_chunk_received = True

                chunk_count += 1
                chunk_data = chunk.audio.audio_data
                chunk_len = len(chunk_data) if isinstance(chunk_data, (bytes, bytearray)) else 0
                total_bytes += chunk_len
                chunk_sizes.append(chunk_len)

                # Record audio_info from first few chunks for consistency check
                if chunk_count <= 5:
                    info = chunk.audio.audio_info
                    if info is not None:
                        audio_info_samples.append({
                            "chunk_index": chunk_count - 1,
                            "codec": getattr(info, "codec", None),
                            "sample_rate_hz": getattr(info, "sample_rate_hz", None),
                            "num_channels": getattr(info, "num_channels", None),
                        })
        except Exception as e:
            result["error"] = f"stream iteration error after {chunk_count} chunks: {e}"

        result["total_elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["chunk_count"] = chunk_count
        result["total_bytes"] = total_bytes
        result["chunk_sizes"] = chunk_sizes
        result["audio_info_samples"] = audio_info_samples

        # Audio info consistency: check if all sampled infos match
        if len(audio_info_samples) >= 2:
            first = audio_info_samples[0]
            result["audio_info_consistent"] = all(
                s["codec"] == first["codec"]
                and s["sample_rate_hz"] == first["sample_rate_hz"]
                and s["num_channels"] == first["num_channels"]
                for s in audio_info_samples[1:]
            )
        elif len(audio_info_samples) == 1:
            result["audio_info_consistent"] = True
        else:
            result["audio_info_consistent"] = None

        return result

    async def _collect_audio_staleness(self, mic, n=5) -> dict:
        """Hash consecutive short audio chunks to detect frozen/silent streams.

        Analogous to camera frame staleness — identical audio hashes indicate
        a frozen or silent source.
        """
        codec = self.profile_config.get("stream_codec", DEFAULT_STREAM_CODEC)
        samples = []
        hashes = []

        for i in range(n):
            sample = {"index": i}
            t0 = time.monotonic()
            try:
                stream = await mic.get_audio(
                    codec=codec,
                    duration_seconds=0.5,
                    previous_timestamp_ns=0,
                )
                # Collect all chunks from the short stream
                audio_bytes = bytearray()
                async for chunk in stream:
                    chunk_data = chunk.audio.audio_data
                    if isinstance(chunk_data, (bytes, bytearray)):
                        audio_bytes.extend(chunk_data)

                elapsed_ms = (time.monotonic() - t0) * 1000
                h = hashlib.sha256(bytes(audio_bytes)).hexdigest()
                sample["latency_ms"] = round(elapsed_ms, 1)
                sample["data_bytes"] = len(audio_bytes)
                sample["sha256"] = h
                hashes.append(h)

            except Exception as e:
                sample["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                sample["error"] = str(e)
                hashes.append(None)

            samples.append(sample)

        # Staleness analysis (same structure as viamrtsp frame staleness)
        stale_pairs = 0
        total_pairs = 0
        for i in range(1, len(hashes)):
            if hashes[i] is not None and hashes[i - 1] is not None:
                total_pairs += 1
                if hashes[i] == hashes[i - 1]:
                    stale_pairs += 1

        successful = [s for s in samples if "error" not in s]

        return {
            "num_calls": n,
            "successful": len(successful),
            "all_succeeded": len(successful) == n,
            "samples": samples,
            "staleness": {
                "consecutive_identical_pairs": stale_pairs,
                "total_comparable_pairs": total_pairs,
                "all_unique": stale_pairs == 0 and total_pairs > 0,
                "zero_stale_pairs": stale_pairs == 0,
                "unique_hashes": len(set(h for h in hashes if h is not None)),
            },
        }

    async def _collect_mic_latency_samples(self, mic) -> dict:
        """Repeated short get_audio calls to measure per-call latency.

        Analogous to camera FPS samples — N short calls recording latency.
        """
        n = self.profile_config.get("latency_samples", DEFAULT_LATENCY_SAMPLES)
        codec = self.profile_config.get("stream_codec", DEFAULT_STREAM_CODEC)
        duration_s = self.profile_config.get("latency_sample_duration_s", DEFAULT_LATENCY_SAMPLE_DURATION_S)

        samples = []
        for i in range(n):
            t0 = time.monotonic()
            try:
                stream = await mic.get_audio(
                    codec=codec,
                    duration_seconds=duration_s,
                    previous_timestamp_ns=0,
                )
                # Drain the stream to measure full round-trip
                byte_count = 0
                async for chunk in stream:
                    chunk_data = chunk.audio.audio_data
                    if isinstance(chunk_data, (bytes, bytearray)):
                        byte_count += len(chunk_data)

                elapsed_ms = (time.monotonic() - t0) * 1000
                samples.append({
                    "index": i,
                    "latency_ms": round(elapsed_ms, 1),
                    "bytes_received": byte_count,
                })
            except Exception as e:
                elapsed_ms = (time.monotonic() - t0) * 1000
                samples.append({
                    "index": i,
                    "latency_ms": round(elapsed_ms, 1),
                    "error": str(e),
                })

        total_ms = sum(s["latency_ms"] for s in samples)
        successful = [s for s in samples if "error" not in s]
        return {
            "num_calls": n,
            "total_ms": round(total_ms, 1),
            "successful": len(successful),
            "samples": samples,
        }

    # ------------------------------------------------------------------
    # Codec tests
    # ------------------------------------------------------------------

    async def _test_codecs(self, mic) -> dict:
        """Test get_audio with each codec (PCM16, PCM32, PCM32_FLOAT, MP3).

        Runs a short stream per codec and records whether it succeeded,
        chunk count, total bytes, and audio_info from the first chunk.
        """
        duration_s = self.profile_config.get("codec_test_duration_s", CODEC_TEST_DURATION_S)
        codecs = self.profile_config.get("test_codecs", TEST_CODECS)
        results = []

        for codec in codecs:
            entry = {
                "codec": codec,
                "ttfc_ms": None,
                "chunk_count": 0,
                "total_bytes": 0,
                "total_elapsed_ms": None,
                "audio_info": None,
                "error": None,
            }

            t0 = time.monotonic()
            try:
                stream = await mic.get_audio(
                    codec=codec,
                    duration_seconds=duration_s,
                    previous_timestamp_ns=0,
                )
            except Exception as e:
                entry["total_elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
                entry["error"] = f"get_audio failed: {e}"
                results.append(entry)
                continue

            first_chunk = True
            chunk_count = 0
            total_bytes = 0

            try:
                async for chunk in stream:
                    now = time.monotonic()
                    if first_chunk:
                        entry["ttfc_ms"] = round((now - t0) * 1000, 1)
                        first_chunk = False
                        info = chunk.audio.audio_info
                        if info is not None:
                            entry["audio_info"] = {
                                "codec": getattr(info, "codec", None),
                                "sample_rate_hz": getattr(info, "sample_rate_hz", None),
                                "num_channels": getattr(info, "num_channels", None),
                            }

                    chunk_count += 1
                    chunk_data = chunk.audio.audio_data
                    if isinstance(chunk_data, (bytes, bytearray)):
                        total_bytes += len(chunk_data)
            except Exception as e:
                entry["error"] = f"stream error after {chunk_count} chunks: {e}"

            entry["total_elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
            entry["chunk_count"] = chunk_count
            entry["total_bytes"] = total_bytes
            results.append(entry)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_per_codec_s": duration_s,
            "codecs_tested": len(results),
            "results": results,
        }

    # ------------------------------------------------------------------
    # Streaming mode tests
    # ------------------------------------------------------------------

    async def _test_infinite_stream(self, mic) -> dict:
        """Test get_audio with duration_seconds=0 (infinite), then cancel after a few seconds.

        Verifies the module can start an open-ended stream and that client-side
        cancellation is clean (no errors, no hangs).
        """
        codec = self.profile_config.get("stream_codec", DEFAULT_STREAM_CODEC)
        listen_s = self.profile_config.get("infinite_listen_s", INFINITE_STREAM_LISTEN_S)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "codec_requested": codec,
            "duration_requested_s": 0,
            "listen_s": listen_s,
            "ttfc_ms": None,
            "chunk_count": 0,
            "total_bytes": 0,
            "total_elapsed_ms": None,
            "cancelled_cleanly": None,
            "error": None,
        }

        t0 = time.monotonic()
        try:
            stream = await mic.get_audio(
                codec=codec,
                duration_seconds=0,
                previous_timestamp_ns=0,
            )
        except Exception as e:
            result["total_elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["error"] = f"get_audio call failed: {e}"
            return result

        first_chunk_received = False
        chunk_count = 0
        total_bytes = 0
        cancel_error = None

        try:
            async for chunk in stream:
                now = time.monotonic()
                if not first_chunk_received:
                    result["ttfc_ms"] = round((now - t0) * 1000, 1)
                    first_chunk_received = True

                chunk_count += 1
                chunk_data = chunk.audio.audio_data
                if isinstance(chunk_data, (bytes, bytearray)):
                    total_bytes += len(chunk_data)

                # Stop after listen_s seconds
                if (now - t0) >= listen_s:
                    break
        except Exception as e:
            cancel_error = str(e)

        # Try to cleanly close the stream
        try:
            close_fn = getattr(stream, "close", None) or getattr(stream, "cancel", None)
            if close_fn and callable(close_fn):
                maybe_coro = close_fn()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            result["cancelled_cleanly"] = True
        except Exception as e:
            result["cancelled_cleanly"] = False
            cancel_error = (cancel_error or "") + f"; close error: {e}"

        result["total_elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["chunk_count"] = chunk_count
        result["total_bytes"] = total_bytes
        if cancel_error:
            result["error"] = cancel_error

        return result

    async def _test_historical_audio(self, mic) -> dict:
        """Test get_audio with previous_timestamp_ns set to a recent past timestamp.

        Records the current time, waits a few seconds for audio to buffer,
        then requests audio starting from the recorded timestamp. The module
        throttles historical delivery with historical_throttle_ms (default 50ms).
        """
        codec = self.profile_config.get("stream_codec", DEFAULT_STREAM_CODEC)
        wait_s = self.profile_config.get("historical_wait_s", HISTORICAL_WAIT_S)
        duration_s = self.profile_config.get("historical_duration_s", HISTORICAL_DURATION_S)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "codec_requested": codec,
            "duration_requested_s": duration_s,
            "wait_before_request_s": wait_s,
            "previous_timestamp_ns": None,
            "live_capture": None,
            "historical_replay": None,
            "data_match": None,
            "error": None,
        }

        # Step 1: Capture live audio and record the timestamp
        recorded_ns = time.time_ns()
        result["previous_timestamp_ns"] = recorded_ns

        live_bytes = bytearray()
        try:
            live_stream = await mic.get_audio(
                codec=codec,
                duration_seconds=duration_s,
                previous_timestamp_ns=0,
            )
            async for chunk in live_stream:
                chunk_data = chunk.audio.audio_data
                if isinstance(chunk_data, (bytes, bytearray)):
                    live_bytes.extend(chunk_data)
            result["live_capture"] = {
                "total_bytes": len(live_bytes),
                "sha256": hashlib.sha256(bytes(live_bytes)).hexdigest(),
            }
        except Exception as e:
            result["live_capture"] = {"error": str(e)}

        # Step 2: Wait for buffer to accumulate, then request historical audio
        await asyncio.sleep(wait_s)

        t0 = time.monotonic()
        try:
            stream = await mic.get_audio(
                codec=codec,
                duration_seconds=duration_s,
                previous_timestamp_ns=recorded_ns,
            )
        except Exception as e:
            result["historical_replay"] = {
                "total_elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "error": f"get_audio call failed: {e}",
            }
            return result

        first_chunk_received = False
        chunk_count = 0
        hist_bytes = bytearray()

        try:
            async for chunk in stream:
                now = time.monotonic()
                if not first_chunk_received:
                    ttfc_ms = round((now - t0) * 1000, 1)
                    first_chunk_received = True

                chunk_count += 1
                chunk_data = chunk.audio.audio_data
                if isinstance(chunk_data, (bytes, bytearray)):
                    hist_bytes.extend(chunk_data)
        except Exception as e:
            result["error"] = f"stream iteration error after {chunk_count} chunks: {e}"

        result["historical_replay"] = {
            "ttfc_ms": ttfc_ms if first_chunk_received else None,
            "chunk_count": chunk_count,
            "total_bytes": len(hist_bytes),
            "total_elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "sha256": hashlib.sha256(bytes(hist_bytes)).hexdigest(),
        }

        # Step 3: Compare live vs historical
        if live_bytes and hist_bytes:
            live_hash = result["live_capture"].get("sha256")
            hist_hash = result["historical_replay"]["sha256"]
            # Check if historical data is a prefix/subset of live data
            overlap = min(len(live_bytes), len(hist_bytes))
            matching_bytes = sum(
                1 for a, b in zip(live_bytes[:overlap], hist_bytes[:overlap]) if a == b
            )
            result["data_match"] = {
                "hashes_equal": live_hash == hist_hash,
                "live_bytes": len(live_bytes),
                "historical_bytes": len(hist_bytes),
                "overlap_bytes": overlap,
                "matching_bytes": matching_bytes,
                "match_ratio": round(matching_bytes / overlap, 4) if overlap > 0 else None,
            }

        return result

    # ------------------------------------------------------------------
    # Speaker tests
    # ------------------------------------------------------------------

    async def _run_speaker_tests(self, robot, result):
        """Run all speaker test surfaces."""
        try:
            speaker = AudioOut.from_robot(robot, self.cam_name)
        except Exception as e:
            result["errors"].append(f"AudioOut not found: {e}")
            return

        result["get_properties"] = await self._collect_speaker_properties(speaker)
        result["profile_data"]["do_commands"] = await self._test_speaker_do_commands(speaker)
        result["profile_data"]["play"] = await self._test_speaker_play(speaker)

    async def _collect_speaker_properties(self, speaker) -> dict:
        """Call get_properties on a speaker and record raw data."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw": None,
            "error": None,
        }

        t0 = time.monotonic()
        try:
            props = await speaker.get_properties()
            elapsed_ms = (time.monotonic() - t0) * 1000
        except Exception as e:
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["error"] = str(e)
            return result

        result["latency_ms"] = round(elapsed_ms, 1)
        result["raw"] = {
            "sample_rate_hz": getattr(props, "sample_rate_hz", None),
            "num_channels": getattr(props, "num_channels", None),
        }

        return result

    async def _test_speaker_do_commands(self, speaker) -> dict:
        """Test speaker DoCommands: set_volume and stop."""
        results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "set_volume": None,
            "stop": None,
        }

        # set_volume
        t0 = time.monotonic()
        try:
            resp = await speaker.do_command({"set_volume": 50})
            elapsed_ms = (time.monotonic() - t0) * 1000
            results["set_volume"] = {
                "latency_ms": round(elapsed_ms, 1),
                "response": dict(resp) if resp else None,
                "error": None,
            }
        except Exception as e:
            results["set_volume"] = {
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "response": None,
                "error": str(e),
            }

        # stop
        t0 = time.monotonic()
        try:
            resp = await speaker.do_command({"stop": True})
            elapsed_ms = (time.monotonic() - t0) * 1000
            results["stop"] = {
                "latency_ms": round(elapsed_ms, 1),
                "response": dict(resp) if resp else None,
                "error": None,
            }
        except Exception as e:
            results["stop"] = {
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "response": None,
                "error": str(e),
            }

        return results

    async def _test_speaker_play(self, speaker) -> dict:
        """Send a short PCM16 sine wave to the speaker and measure latency."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "latency_ms": None,
            "audio_bytes": None,
            "audio_info": {
                "codec": AudioCodec.PCM16,
                "sample_rate_hz": SINE_SAMPLE_RATE,
                "num_channels": SINE_NUM_CHANNELS,
            },
            "error": None,
        }

        pcm_data = _generate_sine_pcm16()
        result["audio_bytes"] = len(pcm_data)

        info = AudioInfo(
            codec=AudioCodec.PCM16,
            sample_rate_hz=SINE_SAMPLE_RATE,
            num_channels=SINE_NUM_CHANNELS,
        )

        t0 = time.monotonic()
        try:
            await speaker.play(data=pcm_data, info=info)
            elapsed_ms = (time.monotonic() - t0) * 1000
            result["latency_ms"] = round(elapsed_ms, 1)
        except Exception as e:
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["error"] = str(e)

        return result

    # ------------------------------------------------------------------
    # Discovery validation
    # ------------------------------------------------------------------

    async def _test_discovery(self, robot) -> dict:
        """Test audio discovery service and validate discovered device attributes."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "discovery_services_found": [],
            "results": [],
            "validations": {},
            "error": None,
        }

        # Find all discovery services on the robot
        resource_names = robot.resource_names
        discovery_names = []
        for rn in resource_names:
            if getattr(rn, "subtype", None) == "discovery":
                name = getattr(rn, "name", None)
                if name:
                    discovery_names.append(name)

        result["discovery_services_found"] = discovery_names

        if not discovery_names:
            result["error"] = "No discovery services found on robot"
            return result

        discovered = []
        errors_per_service = {}
        total_latency_ms = 0

        for disc_name in discovery_names:
            t0 = time.monotonic()
            last_err = None
            resources = None
            for attempt in range(2):
                try:
                    disc = DiscoveryClient.from_robot(robot, disc_name)
                    resources = await disc.discover_resources()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        await asyncio.sleep(0.5)
            elapsed_ms = (time.monotonic() - t0) * 1000
            total_latency_ms += elapsed_ms
            if last_err is not None:
                errors_per_service[disc_name] = str(last_err)
                continue

            for rc in resources:
                d = {}
                d["discovery_service"] = disc_name
                d["name"] = getattr(rc, "name", None)
                d["api"] = getattr(rc, "api", "")
                d["model"] = getattr(rc, "model", "")

                attrs_proto = getattr(rc, "attributes", None)
                if attrs_proto is not None:
                    d["attributes"] = MessageToDict(attrs_proto)
                else:
                    d["attributes"] = None

                discovered.append(d)

        result["latency_ms"] = round(total_latency_ms, 1)
        if errors_per_service:
            result["per_service_errors"] = errors_per_service

        result["results"] = discovered
        result["discovered_count"] = len(discovered)

        # --- Validations ---
        v = {}

        # Filter to audio discoveries
        mic_devices = [
            d for d in discovered
            if d.get("model") == EXPECTED_DISCOVERY_MODEL_MIC
        ]
        spk_devices = [
            d for d in discovered
            if d.get("model") == EXPECTED_DISCOVERY_MODEL_SPK
        ]
        v["microphone_count"] = len(mic_devices)
        v["speaker_count"] = len(spk_devices)
        v["has_microphone"] = len(mic_devices) >= 1
        v["has_speaker"] = len(spk_devices) >= 1

        per_device = []
        for d in mic_devices + spk_devices:
            dv = {"name": d.get("name")}
            attrs = d.get("attributes") or {}

            dv["model"] = d.get("model")
            dv["api"] = d.get("api")

            if d.get("model") == EXPECTED_DISCOVERY_MODEL_MIC:
                dv["model_exact"] = True
                dv["api_exact"] = d.get("api") == EXPECTED_DISCOVERY_API_MIC
            else:
                dv["model_exact"] = d.get("model") == EXPECTED_DISCOVERY_MODEL_SPK
                dv["api_exact"] = d.get("api") == EXPECTED_DISCOVERY_API_SPK

            dv["device_id"] = attrs.get("device_id") or attrs.get("deviceId")
            dv["has_device_id"] = dv["device_id"] is not None and dv["device_id"] != ""
            dv["device_name"] = attrs.get("device_name") or attrs.get("deviceName")
            dv["sample_rate"] = attrs.get("sample_rate") or attrs.get("sampleRate")
            dv["num_channels"] = attrs.get("num_channels") or attrs.get("numChannels")

            per_device.append(dv)

        v["per_device"] = per_device

        # Check if our component name appears in discovery
        discovered_names = [d.get("name") for d in mic_devices + spk_devices]
        v["our_component_in_discovery"] = self.cam_name in discovered_names

        result["validations"] = v
        return result


register(SystemAudioProfile)
