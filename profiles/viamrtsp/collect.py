"""
viamrtsp ONVIF H264 camera test profile — raw data collection.

Collects get_images frame metadata, frame staleness detection, camera properties
validation, and ONVIF discovery service validation. Designed for H264 RTSP cameras
with RTP passthrough enabled.

All validations assert EXACT expected values where the spec is fixed (MIME type,
SupportsPCD, model string). Resolution and frame rate are recorded but not asserted
against hardcoded values since RTSP camera specs vary by model.
"""

import asyncio
import hashlib
import io
import time
from typing import Optional
from datetime import datetime, timezone

from google.protobuf.json_format import MessageToDict
from PIL import Image
from viam.components.camera import Camera
from viam.services.discovery import DiscoveryClient

from profiles.base import BaseProfile
from profiles import register

# ---------------------------------------------------------------------------
# Expected constants for viamrtsp H264 RTSP cameras
# These are fixed by the viamrtsp module implementation, not by camera hardware.
# ---------------------------------------------------------------------------

# get_images
EXPECTED_FRAME_COUNT = 1  # single color stream, no depth
EXPECTED_MIME_TYPE = "image/jpeg"  # viamrtsp always returns JPEG from get_images
EXPECTED_IMAGE_NAME = ""  # viamrtsp passes empty string to NamedImageFromBytes

# get_properties (hardcoded in rtsp.go Properties method)
EXPECTED_SUPPORTS_PCD = False
EXPECTED_MIME_TYPES = ["image/jpeg"]

# discovery
EXPECTED_DISCOVERY_MODEL = "viam:viamrtsp:rtsp"
EXPECTED_DISCOVERY_API = "rdk:component:camera"


class ViamrtspProfile(BaseProfile):
    """viamrtsp ONVIF H264 RTSP camera raw data collection."""

    name = "viamrtsp"
    ACCEPTED_MODELS = {"viam:viamrtsp:rtsp"}

    def _check_model(self, robot) -> Optional[str]:
        """Validate camera model matches this profile. Returns error string or None."""
        model = self.config.get("model")
        if model and model not in self.ACCEPTED_MODELS:
            return (
                f"Model mismatch: camera '{self.cam_name}' has model '{model}' "
                f"but {self.name} profile only accepts {self.ACCEPTED_MODELS}"
            )
        return None

    async def run(self, robot) -> dict:
        """Collect all raw data for the RTSP camera."""
        result = {
            "camera": self.cam_name,
            "profile": self.config.get("profile", self.name),
            "config": self.config,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "get_images": None,
            "fps_samples": None,
            "profile_data": {},
            "errors": [],
        }

        model_mismatch = self._check_model(robot)
        if model_mismatch:
            result["errors"].append(model_mismatch)
            return result

        try:
            cam = Camera.from_robot(robot, self.cam_name)
        except Exception as e:
            result["errors"].append(f"Camera not found: {e}")
            return result

        # Single get_images call for frame metadata
        result["get_images"] = await self._collect_get_images(cam)

        # FPS samples and frame staleness can run concurrently
        fps_task = asyncio.create_task(self._collect_fps_samples(cam))
        staleness_task = asyncio.create_task(self._collect_frame_staleness(cam))
        result["fps_samples"], frame_staleness = await asyncio.gather(
            fps_task, staleness_task
        )

        # Profile-specific data
        result["profile_data"] = await self._collect_profile_data(cam, robot)
        result["profile_data"]["frame_staleness"] = frame_staleness

        return result

    # ------------------------------------------------------------------
    # Profile data aggregation
    # ------------------------------------------------------------------

    async def _collect_profile_data(self, cam, robot) -> dict:
        """Collect viamrtsp-specific data."""
        data = {}

        try:
            resp = await cam.get_images()
            imgs = resp[0] if isinstance(resp, tuple) else resp
        except Exception as e:
            return {"error": f"get_images failed for profile data: {e}"}

        data["color_frame"] = self._collect_color_info(imgs)
        data["get_properties"] = await self._collect_get_properties(cam)
        data["discovery"] = await self._test_discovery(robot, data.get("color_frame"))

        return data

    # ------------------------------------------------------------------
    # Frame metadata
    # ------------------------------------------------------------------

    def _collect_color_info(self, imgs) -> dict:
        """Extract frame metadata from the single color frame."""
        if not imgs:
            return {"found": False, "frame_count": 0}

        img = imgs[0]
        name = getattr(img, "name", None)
        data_bytes = len(img.data) if hasattr(img, "data") else None
        mime = getattr(img, "mime_type", None)

        info = {
            "found": True,
            "frame_count": len(imgs),
            "frame_count_exact": len(imgs) == EXPECTED_FRAME_COUNT,
            "name": name,
            "name_exact": name == EXPECTED_IMAGE_NAME,
            "data_bytes": data_bytes,
            "mime_type": mime,
            "mime_type_exact": mime == EXPECTED_MIME_TYPE,
        }

        try:
            pil_img = Image.open(io.BytesIO(img.data))
            info["width"] = pil_img.size[0]
            info["height"] = pil_img.size[1]
        except Exception as e:
            info["pil_error"] = str(e)

        return info

    # ------------------------------------------------------------------
    # Frame staleness detection
    # ------------------------------------------------------------------

    async def _collect_frame_staleness(self, cam, n=10) -> dict:
        """Hash consecutive get_images frames to detect frozen streams.

        A live RTSP stream should never produce byte-identical consecutive
        JPEG frames. Identical hashes indicate a frozen or stale source.
        """
        samples = []
        hashes = []

        for i in range(n):
            sample = {"index": i}
            t0 = time.monotonic()
            try:
                resp = await cam.get_images()
                elapsed_ms = (time.monotonic() - t0) * 1000
                imgs = resp[0] if isinstance(resp, tuple) else resp

                if imgs:
                    frame_data = imgs[0].data if hasattr(imgs[0], "data") else b""
                    h = hashlib.sha256(frame_data).hexdigest()
                    sample["latency_ms"] = round(elapsed_ms, 1)
                    sample["data_bytes"] = len(frame_data)
                    sample["sha256"] = h
                    hashes.append(h)
                else:
                    sample["latency_ms"] = round(elapsed_ms, 1)
                    sample["error"] = "get_images returned empty list"
                    hashes.append(None)

            except Exception as e:
                sample["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                sample["error"] = str(e)
                hashes.append(None)

            samples.append(sample)

        # Staleness analysis (same structure as orbbec/realsense PCD staleness)
        stale_pairs = 0
        total_pairs = 0
        for i in range(1, len(hashes)):
            if hashes[i] is not None and hashes[i - 1] is not None:
                total_pairs += 1
                if hashes[i] == hashes[i - 1]:
                    stale_pairs += 1

        successful = [s for s in samples if "error" not in s]
        total_ms = sum(s["latency_ms"] for s in samples)

        return {
            "num_calls": n,
            "total_ms": round(total_ms, 1),
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

    # ------------------------------------------------------------------
    # get_properties validation
    # ------------------------------------------------------------------

    async def _collect_get_properties(self, cam) -> dict:
        """Call get_properties and validate against viamrtsp expected values.

        viamrtsp returns SupportsPCD=false, MimeTypes=["image/jpeg"], and
        nil intrinsics. This is hardcoded in rtsp.go, not camera-dependent.
        """
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw": None,
            "validations": {},
            "error": None,
        }

        t0 = time.monotonic()
        try:
            props = await cam.get_properties()
            elapsed_ms = (time.monotonic() - t0) * 1000
        except Exception as e:
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["error"] = str(e)
            return result

        result["latency_ms"] = round(elapsed_ms, 1)

        supports_pcd = getattr(props, "supports_pcd", None)
        mime_types = list(getattr(props, "mime_types", None) or [])
        intrinsics = getattr(props, "intrinsic_parameters", None)
        distortion = getattr(props, "distortion_parameters", None)

        result["raw"] = {
            "supports_pcd": supports_pcd,
            "mime_types": mime_types,
            "has_intrinsics": intrinsics is not None,
            "has_distortion": distortion is not None,
        }

        v = {}
        v["supports_pcd_is_false"] = supports_pcd is False
        v["mime_types_exact"] = mime_types == EXPECTED_MIME_TYPES
        v["intrinsics_is_none"] = intrinsics is None
        v["distortion_is_none"] = distortion is None

        result["validations"] = v
        return result

    # ------------------------------------------------------------------
    # Discovery service validation
    # ------------------------------------------------------------------

    async def _test_discovery(self, robot, color_frame_info=None) -> dict:
        """Test ONVIF discovery service and validate discovered camera attributes."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "discovery_services_found": [],
            "results": [],
            "validations": {},
            "error": None,
        }

        # Find all discovery services on the robot (same approach as realsense)
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

        # Filter to viamrtsp discoveries
        rtsp_devices = [
            d for d in discovered
            if d.get("model") == EXPECTED_DISCOVERY_MODEL
        ]
        v["rtsp_device_count"] = len(rtsp_devices)
        v["has_rtsp_device"] = len(rtsp_devices) >= 1

        per_device = []
        for d in rtsp_devices:
            dv = {"name": d.get("name")}
            attrs = d.get("attributes") or {}

            dv["model_exact"] = d.get("model") == EXPECTED_DISCOVERY_MODEL
            dv["api_exact"] = d.get("api") == EXPECTED_DISCOVERY_API

            # rtp_passthrough should be true (set by discovery)
            dv["rtp_passthrough"] = attrs.get("rtp_passthrough") or attrs.get("rtpPassthrough")
            dv["rtp_passthrough_is_true"] = dv["rtp_passthrough"] is True

            # Codec should be H264 for this profile
            dv["codec"] = attrs.get("codec")
            dv["codec_is_h264"] = dv["codec"] == "H264"

            # Resolution from discovery (nested object)
            resolution = attrs.get("resolution")
            if isinstance(resolution, dict):
                dv["discovery_width"] = resolution.get("width")
                dv["discovery_height"] = resolution.get("height")
            else:
                dv["discovery_width"] = None
                dv["discovery_height"] = None

            # Frame rate from discovery
            dv["frame_rate"] = attrs.get("frame_rate") or attrs.get("frameRate")

            # Cross-check: discovery-reported resolution vs actual frame resolution
            if color_frame_info and isinstance(resolution, dict):
                actual_w = color_frame_info.get("width")
                actual_h = color_frame_info.get("height")
                disc_w = resolution.get("width")
                disc_h = resolution.get("height")
                if actual_w is not None and disc_w is not None:
                    dv["resolution_width_match"] = actual_w == disc_w
                    dv["resolution_height_match"] = actual_h == disc_h
                    dv["resolution_match"] = (
                        actual_w == disc_w and actual_h == disc_h
                    )

            per_device.append(dv)

        v["per_device"] = per_device

        # Check if our camera name appears in discovery
        our_name = self.cam_name
        discovered_names = [d.get("name") for d in rtsp_devices]
        v["our_camera_in_discovery"] = our_name in discovered_names

        result["validations"] = v
        return result


register(ViamrtspProfile)
