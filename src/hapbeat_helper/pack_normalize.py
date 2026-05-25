"""Pack WAV normalization to 16 kHz PCM16.

Device firmware plays Pack WAV files at a fixed 16 kHz I2S clock and does
not resample. All clips in a Pack must therefore be 16 kHz PCM16 before
they are transferred to the device.

Studio (2026-04-21+) already normalizes on export. This module acts as a
safety net for kits built via other routes.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import wave
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

TARGET_RATE = 16000
# Hapbeat SDK の stream session は単一 format で固定 (rate+channels 共通) なので、
# 複数 clip が同時 stream される場合に mono / stereo が混在すると session
# mismatch で reject される。Studio / Helper の Pack normalize で stereo (2ch) に
# 揃えておくことで衝突を未然回避する。mono は ffmpeg の標準 up-mix で L=R に複製される。
TARGET_CHANNELS = 2


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            check=False,
        )
        return True
    except FileNotFoundError:
        return False


def _convert_with_ffmpeg(in_path: Path, out_path: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(in_path),
        "-ar", str(TARGET_RATE),
        "-ac", str(TARGET_CHANNELS),
        "-acodec", "pcm_s16le",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="ignore")[:300]
        raise RuntimeError(f"ffmpeg failed: {stderr}")


def _find_manifest(pack_dir: Path) -> Path | None:
    """Locate the kit manifest inside *pack_dir*.

    Convention (2026-05-17): `<pack_dir.name>-manifest.json` preferred,
    fallback to any `*manifest*.json` (legacy `manifest.json` matches).
    Mirrors the rule in server.py / Studio / Unity SDK.
    """
    preferred = pack_dir / f"{pack_dir.name}-manifest.json"
    if preferred.exists():
        return preferred
    for child in sorted(pack_dir.iterdir()):
        if not child.is_file():
            continue
        name = child.name.lower()
        if name.endswith(".json") and "manifest" in name:
            return child
    return None


def normalize_pack(pack_dir: Path) -> List[str]:
    """Normalize all clips/*.wav in *pack_dir* to 16 kHz PCM16.

    Returns a list of informational messages — one per clip that was
    actually converted. Empty list means no conversion was needed.
    """
    manifest_path = _find_manifest(pack_dir)
    if manifest_path is None:
        return []

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("pack_normalize: cannot read %s: %s", manifest_path.name, exc)
        return []

    clips_map: dict = manifest.get("clips", {})
    if not clips_map:
        return []

    if not _ffmpeg_available():
        logger.warning(
            "pack_normalize: ffmpeg not found — WAV normalization skipped. "
            "Clips may play at wrong speed/pitch on device."
        )
        return []

    messages: List[str] = []

    for fname in list(clips_map.keys()):
        wav_path = pack_dir / "clips" / fname
        if not wav_path.exists():
            continue

        try:
            with wave.open(str(wav_path), "rb") as wf:
                rate = wf.getframerate()
                sampwidth = wf.getsampwidth()
                channels = wf.getnchannels()
                frames = wf.getnframes()
        except Exception as exc:
            logger.warning(
                "pack_normalize: cannot read WAV header for %s: %s",
                fname, exc,
            )
            continue

        if rate == TARGET_RATE and sampwidth == 2 and channels == TARGET_CHANNELS:
            clips_map[fname] = {
                "duration_ms": round(frames / rate * 1000, 2),
                "sample_rate": rate,
                "channels": channels,
                "format": "pcm_s16le",
            }
            continue

        tmp_path = wav_path.with_suffix(".normalize_tmp.wav")
        try:
            _convert_with_ffmpeg(wav_path, tmp_path)
        except Exception as exc:
            messages.append(f"{fname}: 変換失敗 ({exc})")
            tmp_path.unlink(missing_ok=True)
            continue

        shutil.move(str(tmp_path), str(wav_path))

        try:
            with wave.open(str(wav_path), "rb") as wf2:
                new_frames = wf2.getnframes()
                new_channels = wf2.getnchannels()
        except Exception:
            new_frames = frames
            new_channels = channels

        clips_map[fname] = {
            "duration_ms": round(new_frames / TARGET_RATE * 1000, 2),
            "sample_rate": TARGET_RATE,
            "channels": new_channels,
            "format": "pcm_s16le",
        }
        msg = f"{fname}: {rate} Hz → {TARGET_RATE} Hz"
        messages.append(msg)
        logger.info("pack_normalize: %s", msg)

    manifest["clips"] = clips_map
    try:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("pack_normalize: cannot write %s: %s", manifest_path.name, exc)

    return messages
