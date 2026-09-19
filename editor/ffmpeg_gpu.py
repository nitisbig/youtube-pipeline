"""
ffmpeg_gpu.py - Hardware acceleration detection and FFmpeg command builder.

Supports NVIDIA NVENC, AMD/Intel VAAPI, Apple VideoToolbox, and Intel QSV,
with automatic graceful fallback to CPU (libx264).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

_CACHED_GPU_ENCODER: Optional[Tuple[Optional[str], Optional[str]]] = None


def str2bool(val) -> bool:
    """Parse boolean from CLI arguments or configuration."""
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    v = str(val).strip().lower()
    if v in ("yes", "true", "t", "y", "1"):
        return True
    if v in ("no", "false", "f", "n", "0", ""):
        return False
    raise argparse.ArgumentTypeError(f"Boolean value expected, got '{val}'.")


def probe_gpu_encoder() -> Tuple[Optional[str], Optional[str]]:
    """
    Probe for available and functional FFmpeg hardware H.264 encoders.
    Returns (encoder_type, device_path_or_info) or (None, None).
    encoder_type is one of: 'nvenc', 'vaapi', 'videotoolbox', 'qsv'.
    """
    global _CACHED_GPU_ENCODER
    if _CACHED_GPU_ENCODER is not None:
        return _CACHED_GPU_ENCODER

    if shutil.which("ffmpeg") is None:
        _CACHED_GPU_ENCODER = (None, None)
        return _CACHED_GPU_ENCODER

    # 1. Probe NVIDIA NVENC (h264_nvenc)
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04",
            "-c:v", "h264_nvenc", "-f", "null", "-"
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0:
            _CACHED_GPU_ENCODER = ("nvenc", None)
            return _CACHED_GPU_ENCODER
    except Exception:
        pass

    # 2. Probe VAAPI (AMD / Intel on Linux)
    # Check common render nodes: /dev/dri/renderD128, etc.
    dri_nodes = [f"/dev/dri/renderD{i}" for i in range(128, 136)]
    for node in dri_nodes:
        if os.path.exists(node):
            try:
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-vaapi_device", node,
                    "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04",
                    "-vf", "format=nv12,hwupload",
                    "-c:v", "h264_vaapi", "-f", "null", "-"
                ]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0:
                    _CACHED_GPU_ENCODER = ("vaapi", node)
                    return _CACHED_GPU_ENCODER
            except Exception:
                pass

    # 3. Probe Apple VideoToolbox (macOS)
    if sys.platform == "darwin":
        try:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04",
                "-c:v", "h264_videotoolbox", "-f", "null", "-"
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0:
                _CACHED_GPU_ENCODER = ("videotoolbox", None)
                return _CACHED_GPU_ENCODER
        except Exception:
            pass

    # 4. Probe Intel QSV
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04",
            "-c:v", "h264_qsv", "-f", "null", "-"
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0:
            _CACHED_GPU_ENCODER = ("qsv", None)
            return _CACHED_GPU_ENCODER
    except Exception:
        pass

    _CACHED_GPU_ENCODER = (None, None)
    return _CACHED_GPU_ENCODER


def get_hardware_status_summary() -> str:
    """Return a short human-readable summary of available video hardware acceleration."""
    enc_type, dev = probe_gpu_encoder()
    if enc_type == "nvenc":
        return "NVIDIA GPU (NVENC)"
    elif enc_type == "vaapi":
        return f"AMD/Intel GPU (VAAPI: {dev})"
    elif enc_type == "videotoolbox":
        return "Apple Silicon / GPU (VideoToolbox)"
    elif enc_type == "qsv":
        return "Intel QuickSync (QSV)"
    return "CPU (libx264)"


@dataclass
class VideoEncoderConfig:
    encoder_type: str  # 'nvenc', 'vaapi', 'videotoolbox', 'qsv', or 'cpu'
    codec: str         # 'h264_nvenc', 'h264_vaapi', 'libx264', etc.
    args: List[str]    # command-line arguments (e.g. ['-c:v', ...])
    needs_hwupload: bool = False
    vaapi_device: Optional[str] = None


def get_video_encoder_config(
    use_gpu: bool = False,
    preset: str = "medium",
    crf: int = 18,
) -> VideoEncoderConfig:
    """
    Produce the encoder configuration for FFmpeg based on GPU preference and availability.
    """
    if use_gpu:
        enc_type, dev = probe_gpu_encoder()
        if enc_type == "nvenc":
            # Map x264 preset to nvenc preset
            nvenc_preset = "p4"
            if preset in ("ultrafast", "superfast", "veryfast", "faster", "fast"):
                nvenc_preset = "p3"
            elif preset in ("slow", "slower", "veryslow"):
                nvenc_preset = "p6"
            return VideoEncoderConfig(
                encoder_type="nvenc",
                codec="h264_nvenc",
                args=[
                    "-c:v", "h264_nvenc",
                    "-preset", nvenc_preset,
                    "-cq", str(crf),
                    "-pix_fmt", "yuv420p",
                ],
                needs_hwupload=False,
            )
        elif enc_type == "vaapi":
            return VideoEncoderConfig(
                encoder_type="vaapi",
                codec="h264_vaapi",
                args=[
                    "-c:v", "h264_vaapi",
                    "-qp", str(crf),
                ],
                needs_hwupload=True,
                vaapi_device=dev,
            )
        elif enc_type == "videotoolbox":
            return VideoEncoderConfig(
                encoder_type="videotoolbox",
                codec="h264_videotoolbox",
                args=[
                    "-c:v", "h264_videotoolbox",
                    "-q:v", "65",
                    "-pix_fmt", "yuv420p",
                ],
                needs_hwupload=False,
            )
        elif enc_type == "qsv":
            return VideoEncoderConfig(
                encoder_type="qsv",
                codec="h264_qsv",
                args=[
                    "-c:v", "h264_qsv",
                    "-global_quality", str(crf),
                    "-pix_fmt", "nv12",
                ],
                needs_hwupload=False,
            )
        else:
            print(
                "[ffmpeg_gpu] Notice: GPU acceleration requested (--gpu true), "
                "but no compatible GPU encoder found. Using CPU (libx264).",
                file=sys.stderr,
            )

    # CPU fallback / default
    return VideoEncoderConfig(
        encoder_type="cpu",
        codec="libx264",
        args=[
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
        ],
        needs_hwupload=False,
    )


def run_ffmpeg_with_cpu_fallback(
    gpu_cmd: List[str],
    cpu_cmd: List[str],
    desc: str = "FFmpeg",
) -> None:
    """
    Run the GPU command. If it fails with CalledProcessError, print a warning
    and re-run using cpu_cmd.
    """
    print(f"\nRunning {desc} (GPU-accelerated)...")
    try:
        subprocess.run(gpu_cmd, check=True)
        return
    except subprocess.CalledProcessError as exc:
        print(
            f"\n[WARNING] {desc} GPU encoding failed with exit code {exc.returncode}. "
            "Falling back to CPU encoding (libx264)...",
            file=sys.stderr,
        )
    except KeyboardInterrupt:
        raise

    print(f"Running {desc} (CPU fallback)...")
    subprocess.run(cpu_cmd, check=True)
