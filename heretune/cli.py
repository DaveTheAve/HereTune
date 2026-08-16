from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import socket
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from . import __version__
from .accel import gpu_telemetry, resolve_execution_mode, resource_worker_plan
from .analyzer import CorrectionProfile, OPENCV_NOTE, cv2
from .config import load_config, library_roots, scheduler_library_root
from .db import DB
from .hsp import build, inspect
from .library import Library
from .server import create_app
from .processes import run_capture, terminate_all_processes


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="heretune")
    command.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = command.add_subparsers(dest="cmd", required=True)

    serve = subcommands.add_parser("serve")
    serve.add_argument("--config", default="config.toml")
    serve.add_argument(
        "--verbose", action="store_true",
        help="Show Uvicorn startup/access logs in addition to the HereTune banner",
    )
    for name in ("scan", "doctor"):
        item = subcommands.add_parser(name)
        item.add_argument("--config", default="config.toml")
    inspect_command = subcommands.add_parser("inspect-hsp")
    inspect_command.add_argument("path")
    return command


def _doctor(cfg) -> int:
    print(f"HereTune {__version__} doctor")
    print("Python:", sys.version.split()[0], f"({sys.executable})")
    roots = library_roots(cfg)
    if not roots:
        print("Library folders: NOT CONFIGURED")
    else:
        print(f"Library folders: {len(roots)} configured")
        for root in roots:
            try:
                reachable = Path(root).is_dir()
            except OSError:
                reachable = False
            print("  -", root, "OK" if reachable else "NOT REACHABLE")
    print("FFmpeg:", shutil.which(cfg.paths.ffmpeg) or "NOT FOUND")
    print("FFprobe:", shutil.which(cfg.paths.ffprobe) or "NOT FOUND")
    try:
        resolved_mode, cuda = resolve_execution_mode(cfg.analysis, cfg.paths.ffmpeg)
        print(
            "Analysis mode:",
            f"{cfg.analysis.execution_mode} -> {resolved_mode}",
            f"(Auto prefers {cfg.analysis.auto_preference})",
        )
        if cuda is not None:
            print("CUDA sampling:", "OK" if cuda.usable else "unavailable", "-", cuda.detail)
            if cuda.usable:
                telemetry = gpu_telemetry(int(cfg.analysis.gpu_device), max_age=0.0)
                if telemetry:
                    print("GPU telemetry:", telemetry)
        print(
            "Sampling:",
            f"{cfg.analysis.samples} base frames @ {cfg.analysis.analysis_width}px",
            f"(adaptive max {'unlimited' if int(cfg.analysis.max_samples) == 0 else cfg.analysis.max_samples})",
        )
    except Exception as exc:
        print("Analysis acceleration:", f"ERROR: {exc}")
    print("OpenCV:", getattr(cv2, "__version__", "unavailable"))
    if OPENCV_NOTE:
        print("OpenCV note:", OPENCV_NOTE)
    print("Data:", cfg.data_dir)

    blob = build("doctor", "HereTune Doctor", 60.0, CorrectionProfile())
    parsed = inspect(blob)
    image = parsed.image_keyframes[0]
    exact = (
        parsed.version == 7
        and image.sharpen == 2.0
        and image.white_temp == 6500.0
        and image.white_tint == 0.0
        and image.shadows == 0.0
        and image.midtones == 1.0
        and image.highlights == 1.0
    )
    print("Current-build HSP v7 self-test:", "OK" if exact else "FAILED")
    return 0 if exact else 1


def _decode_bytes(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _print_gpu_telemetry(device: int) -> None:
    data = gpu_telemetry(device, max_age=0.0)
    if not data:
        return
    name = data.get("name") or f"GPU {device}"
    gpu = data.get("gpu_utilization")
    dec = data.get("decoder_utilization")
    used = data.get("memory_used_mb")
    total = data.get("memory_total_mb")
    temp = data.get("temperature_c")
    parts = []
    if gpu is not None: parts.append(f"GPU {gpu:.0f}%")
    if dec is not None: parts.append(f"NVDEC {dec:.0f}%")
    if used is not None and total is not None: parts.append(f"VRAM {used/1024:.1f}/{total/1024:.1f} GB")
    if temp is not None: parts.append(f"{temp:.0f}°C")
    print(f"  Telemetry   {name}" + (f" · {' · '.join(parts)}" if parts else ""))


def _windows_addresses() -> list[tuple[str, str]]:
    """Return (IPv4, interface alias) using structured PowerShell output."""
    if sys.platform != "win32":
        return []
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object {$_.AddressState -eq 'Preferred' -and $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*'} | "
        "Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress"
    )
    try:
        result = run_capture(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=8,
        )
        if result.returncode:
            return []
        payload = json.loads(_decode_bytes(result.stdout) or "[]")
        if isinstance(payload, dict):
            payload = [payload]
        return [
            (str(item.get("IPAddress") or ""), str(item.get("InterfaceAlias") or "Network"))
            for item in payload
            if item.get("IPAddress")
        ]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
        return []


def _socket_addresses() -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if address and address != "127.0.0.1":
                results.append((address, "Network"))
    except OSError:
        pass
    return results


def _network_kind(address: str, alias: str) -> tuple[int, str]:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return 99, "Other"
    lowered = alias.casefold()
    virtual_words = ("vethernet", "wsl", "virtual", "vmware", "virtualbox", "hyper-v", "bluetooth", "docker")
    if "tailscale" in lowered or ip in ipaddress.ip_network("100.64.0.0/10"):
        return 2, "Tailscale"
    if ip.is_private and not any(word in lowered for word in virtual_words):
        return 0, "LAN / Quest"
    if any(word in lowered for word in virtual_words):
        return 3, "Virtual"
    if ip.is_private:
        return 1, "Private network"
    return 4, "Other"


def _discover_addresses() -> list[tuple[str, str, str]]:
    raw = _windows_addresses() or _socket_addresses()
    dedup: dict[str, str] = {}
    for address, alias in raw:
        dedup.setdefault(address, alias)
    items = []
    for address, alias in dedup.items():
        priority, kind = _network_kind(address, alias)
        items.append((priority, address, alias, kind))
    items.sort(key=lambda item: (item[0], tuple(int(part) for part in item[1].split("."))))
    return [(address, alias, kind) for _, address, alias, kind in items]


def _print_endpoint(label: str, base: str) -> None:
    print(f"  {label:<11} {base}/ui")
    print(f"  {'HereSphere':<11} {base}/heresphere")


def _print_startup_banner(cfg) -> None:
    port = int(cfg.server.port)
    width = 72
    print()
    print(f"HereTune {__version__}")
    print("=" * width)
    print("HereTune is awake. The pixels have formed an orderly line.")
    print()
    print("THIS PC")
    _print_endpoint("Dashboard", f"http://127.0.0.1:{port}")

    addresses = _discover_addresses()
    recommended = next((item for item in addresses if item[2] == "LAN / Quest"), None)
    if recommended:
        address, alias, _ = recommended
        print()
        print(f"QUEST / LAN  [recommended]  {alias}")
        _print_endpoint("Dashboard", f"http://{address}:{port}")

    others = [item for item in addresses if item != recommended]
    if others:
        print()
        print("OTHER NETWORKS")
        for address, alias, kind in others:
            base = f"http://{address}:{port}"
            print(f"  {kind:<14} {address:<15} {alias}")
            print(f"    Dashboard  {base}/ui")
            print(f"    HereSphere {base}/heresphere")

    print()
    print("ANALYSIS")
    try:
        resolved_mode, cuda = resolve_execution_mode(cfg.analysis, cfg.paths.ffmpeg)
        requested = str(cfg.analysis.execution_mode).upper()
        active = str(resolved_mode).upper()
        print(f"  Mode        {requested} -> {active}")
        if cuda is not None:
            if cuda.usable:
                print(f"  GPU         CUDA/NVDEC ready on device {cfg.analysis.gpu_device}")
            else:
                print(f"  GPU         unavailable; using CPU ({cuda.detail})")
        else:
            print("  GPU         disabled by CPU mode")
        if resolved_mode in {"hybrid", "gpu"} and cuda is not None and cuda.usable:
            _print_gpu_telemetry(int(cfg.analysis.gpu_device))
        if resolved_mode == "hybrid":
            print(f"  Pipeline    GPU frame decode/resize + CPU correction math ({cfg.analysis.cpu_analysis_workers} CPU workers)")
        elif resolved_mode == "gpu":
            print("  Pipeline    GPU frame decode/resize + CPU correction math")
        else:
            print(f"  Pipeline    CPU frame decode + correction math ({cfg.analysis.cpu_analysis_workers} workers)")
        max_samples_text = "unlimited" if int(cfg.analysis.max_samples) == 0 else str(cfg.analysis.max_samples)
        print(f"  Sampling    {cfg.analysis.samples} base -> up to {max_samples_text} frames @ {cfg.analysis.analysis_width}px")
        try:
            plan = resource_worker_plan(cfg.analysis, scheduler_library_root(cfg), max_age=0.0)
            scheduler = f"{plan.gpu_workers} GPU / {plan.cpu_workers} CPU / {plan.probe_workers} probe"
            if plan.nas_latency_ms is not None:
                scheduler += f" · NAS {plan.nas_latency_ms:.0f}ms"
            scheduler += " · adaptive" if plan.adaptive else " · fixed"
            print(f"  Scheduler   {scheduler}")
        except Exception:
            pass
        if getattr(cfg.analysis, "smart_sampling", False):
            print(f"  Selection   smart rejection on · {float(getattr(cfg.analysis, 'candidate_multiplier', 1.35)):.2f}x candidate pool")
    except Exception as exc:
        print(f"  Acceleration check failed: {exc}")

    print()
    print("Tip: Steam HereSphere on this PC can use 127.0.0.1. Quest should use the")
    print("     LAN address above. In HereSphere, enable 'Always Load HSP'.")
    print("-" * width)
    print(f"Listening on {cfg.server.host}:{port}  |  Ctrl+C to stop")
    print()




def _write_crash_log(cfg, heading: str, text: str) -> None:
    """Persist fatal/background failures even when the console disappears."""
    try:
        path = cfg.data_dir / "logs" / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        with path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"\n[{stamp}] {heading}\n{text.rstrip()}\n")
    except Exception:
        pass


def _install_thread_crash_logging(cfg) -> None:
    previous = threading.excepthook

    def hook(args):
        formatted = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        _write_crash_log(cfg, f"Unhandled thread exception: {args.thread.name}", formatted)
        try:
            print(f"[HereTune FATAL] Background thread {args.thread.name} crashed. See data/logs/crash.log", file=sys.stderr)
        except Exception:
            pass
        previous(args)

    threading.excepthook = hook

def main() -> int:
    args = parser().parse_args()
    if args.cmd == "inspect-hsp":
        result = inspect(Path(args.path).read_bytes(), require_complete=False)
        print(json.dumps(result.dict(), indent=2, ensure_ascii=False))
        return 0

    cfg = load_config(args.config)
    _install_thread_crash_logging(cfg)
    if args.cmd == "doctor":
        return _doctor(cfg)
    if args.cmd == "scan":
        database = DB(cfg.db_path)
        print(json.dumps(Library(database, cfg).scan(), indent=2))
        return 0

    _print_startup_banner(cfg)
    import uvicorn
    try:
        uvicorn.run(
            create_app(cfg),
            host=cfg.server.host,
            port=cfg.server.port,
            log_level="info" if args.verbose else "warning",
            access_log=bool(args.verbose),
        )
    except KeyboardInterrupt:
        # Older Uvicorn releases may bubble the signal instead of swallowing it.
        pass
    except BaseException as exc:
        formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _write_crash_log(cfg, "Fatal server exception", formatted)
        try:
            print("[HereTune FATAL] Server terminated unexpectedly. Crash details were saved to data/logs/crash.log", file=sys.stderr)
        except Exception:
            pass
        raise
    finally:
        stopped = terminate_all_processes()
        print(f"HereTune stopped{f' ({stopped} child process(es) terminated)' if stopped else ''}.")
    return 0
