#!/usr/bin/env python3
"""Build an iPhone-frame to Nexus-time lookup from an OpenCap capture."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable


def build_rows(
    sync_document: dict[str, Any],
    odometry_rows: Iterable[dict[str, str]],
    *,
    vicon_rate_hz: float | None = None,
    vicon_pretrigger_seconds: float = 0,
    vicon_first_frame: int = 0,
) -> list[dict[str, str]]:
    delta_ms = sync_document.get("firstCameraFrameDeltaFromScheduledStartMilliseconds")
    if not isinstance(delta_ms, (int, float)):
        raise ValueError(
            "vicon_sync.json has no first-camera-frame delta; run a trial after the badge reaches VICON READY"
        )
    if vicon_rate_hz is not None and vicon_rate_hz <= 0:
        raise ValueError("Vicon rate must be greater than zero")

    first_frame_nexus_seconds = float(delta_ms) / 1000
    result: list[dict[str, str]] = []
    for row in odometry_rows:
        phone_relative = float(row["timestamp"])
        nexus_seconds = first_frame_nexus_seconds + phone_relative
        mapped = {
            "iphone_frame": row["frame"],
            "iphone_relative_seconds": f"{phone_relative:.9f}",
            "nexus_capture_seconds": f"{nexus_seconds:.9f}",
            "vicon_frame_float": "",
            "vicon_frame_nearest": "",
        }
        if vicon_rate_hz is not None:
            vicon_frame = (
                (nexus_seconds + vicon_pretrigger_seconds) * vicon_rate_hz
                + vicon_first_frame
            )
            mapped["vicon_frame_float"] = f"{vicon_frame:.6f}"
            mapped["vicon_frame_nearest"] = str(round(vicon_frame))
        result.append(mapped)
    return result


def write_frame_sync(
    sync_path: Path,
    odometry_path: Path,
    output_path: Path,
    *,
    vicon_rate_hz: float | None,
    vicon_pretrigger_seconds: float,
    vicon_first_frame: int,
) -> int:
    sync_document = json.loads(sync_path.read_text(encoding="utf-8"))
    with odometry_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = build_rows(
            sync_document,
            csv.DictReader(stream),
            vicon_rate_hz=vicon_rate_hz,
            vicon_pretrigger_seconds=vicon_pretrigger_seconds,
            vicon_first_frame=vicon_first_frame,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "iphone_frame",
        "iphone_relative_seconds",
        "nexus_capture_seconds",
        "vicon_frame_float",
        "vicon_frame_nearest",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vicon-sync", required=True, type=Path)
    parser.add_argument("--odometry", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--vicon-rate", type=float, help="Vicon capture rate in Hz")
    parser.add_argument(
        "--vicon-pretrigger-seconds",
        type=float,
        default=0,
        help="Nexus Capture Before Start value; normally zero",
    )
    parser.add_argument(
        "--vicon-first-frame",
        type=int,
        default=0,
        help="First Vicon/C3D frame number; default zero",
    )
    args = parser.parse_args()
    try:
        count = write_frame_sync(
            args.vicon_sync,
            args.odometry,
            args.out,
            vicon_rate_hz=args.vicon_rate,
            vicon_pretrigger_seconds=args.vicon_pretrigger_seconds,
            vicon_first_frame=args.vicon_first_frame,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(f"wrote {count} synchronized frame rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
