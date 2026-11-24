"""Read-only process-tree resource sampling; no invented pass/fail thresholds.

Use --include-pid for verified WebKit/WebView helpers reparented by the OS.
Memory definitions differ by OS and are never compared as the same metric.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import time


def cpu_seconds(value):
    days = 0
    if "-" in value:
        raw, value = value.split("-", 1)
        days = int(raw)
    fields = [float(part) for part in value.split(":")]
    total = 0.0
    for field in fields:
        total = total * 60 + field
    return days * 86400 + total


def process_snapshot():
    if platform.system() == "Windows":
        script = r"""$rows = Get-CimInstance Win32_Process | ForEach-Object {
          $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
          if ($p) { [pscustomobject]@{pid=[int]$_.ProcessId;ppid=[int]$_.ParentProcessId;
            residentBytes=[long]$p.WorkingSet64;privateBytes=[long]$p.PrivateMemorySize64;
            cpuSeconds=$p.TotalProcessorTime.TotalSeconds;name=$p.ProcessName} }
        }; ConvertTo-Json -InputObject @($rows) -Compress"""
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                check=True, capture_output=True, text=True)
        rows = json.loads(result.stdout.lstrip("\ufeff"))
        return {row["pid"]: row for row in rows}
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,time=,comm="],
                            check=True, capture_output=True, text=True)
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 4)
        if len(fields) != 5:
            continue
        pid, ppid, rss, clock, name = fields
        rows[int(pid)] = {"pid": int(pid), "ppid": int(ppid), "residentBytes": int(rss) * 1024,
                          "privateBytes": None, "cpuSeconds": cpu_seconds(clock), "name": name}
    return rows


def select_tree(rows, roots):
    selected = {pid for pid in roots if pid in rows}
    while True:
        children = {pid for pid, row in rows.items() if row["ppid"] in selected}
        if children <= selected:
            return [rows[pid] for pid in sorted(selected)]
        selected |= children


def measure(pid, include_pids, duration, interval):
    started = time.monotonic()
    deadline = started + duration
    samples = []
    previous = {}
    previous_time = None
    while True:
        stamp = time.monotonic()
        rows = select_tree(process_snapshot(), [pid, *include_pids])
        cpu = None
        if previous_time is not None:
            delta = sum(max(0.0, row["cpuSeconds"] - previous.get(row["pid"], row["cpuSeconds"])) for row in rows)
            cpu = delta / (stamp - previous_time) * 100
        samples.append({"elapsedSeconds": stamp - started, "processCount": len(rows),
                        "residentBytesSum": sum(row["residentBytes"] for row in rows),
                        "privateBytesSum": sum(row["privateBytes"] for row in rows) if rows and all(row["privateBytes"] is not None for row in rows) else None,
                        "cpuPercentOneCore": cpu, "diskReadBytes": None, "diskWriteBytes": None,
                        "processes": rows})
        previous = {row["pid"]: row["cpuSeconds"] for row in rows}
        previous_time = stamp
        if time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    return samples


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--include-pid", type=int, action="append", default=[])
    parser.add_argument("--label", choices=["first", "cold", "warm", "idle", "long-idle"], required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--version", required=True)
    parser.add_argument("--hardware-note", default="")
    parser.add_argument("--startup-log", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.pid <= 0 or not 0 < args.duration <= 3600 or not 0.1 <= args.interval <= 60:
        parser.error("PID必须为正数；时长为0—3600秒；采样间隔为0.1—60秒。")
    if args.out.exists():
        parser.error("结果文件已存在，请选择新文件名。")
    samples = measure(args.pid, args.include_pid, args.duration, args.interval)
    report = {"formatVersion": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
              "version": args.version, "label": args.label, "platform": platform.platform(),
              "architecture": platform.machine(), "cpuCount": os.cpu_count(), "hardwareNote": args.hardware_note,
              "rootPid": args.pid, "explicitOwnedHelperPids": args.include_pid,
              "coverage": "Root and descendants, plus explicitly verified helper roots. OS-reparented helpers require --include-pid; unrelated shared services excluded.",
              "memoryMetric": "Windows working-set sum and private-byte sum" if platform.system() == "Windows" else "Per-process RSS sum; shared pages may be counted more than once",
              "diskMetric": "Not collected by this portable sampler; use OS tracing separately when investigating I/O.",
              "thresholds": None, "verdict": "measurement_only", "samples": samples}
    if args.startup_log:
        report["startupEvents"] = [json.loads(line) for line in args.startup_log.read_text().splitlines() if line.strip()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
