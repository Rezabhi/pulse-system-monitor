#!/usr/bin/env python3
"""pulse - a live, colourful system performance monitor for your terminal.

Shows CPU (total and per core), memory, swap, disks, network and the busiest
processes, refreshed in place. The only dependency is psutil.

    python pulse.py              # live dashboard, press q to quit
    python pulse.py -i 0.5       # refresh twice a second
    python pulse.py --once       # print a single snapshot and exit
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import socket
import sys
import time
from collections import deque
from datetime import datetime

try:
    import psutil
except ImportError:  # pragma: no cover
    sys.exit("pulse needs psutil:  pip install psutil")

__version__ = "1.0.0"

HISTORY = 120      # samples kept for the sparklines
TOP_N = 6          # processes shown
MAX_DISKS = 3      # partitions shown
SPARKS = "▁▂▃▄▅▆▇█"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

RESET = "\x1b[0m"
BOLD = "\x1b[1m"


def fg(n: int) -> str:
    return f"\x1b[38;5;{n}m"


GREEN, YELLOW, RED = fg(78), fg(221), fg(203)
BLUE, PURPLE, CYAN = fg(75), fg(141), fg(80)
TEXT, MUTED, TRACK, FRAME = fg(252), fg(245), fg(238), fg(240)
TRACK_CHAR = "█"   # becomes "░" in --no-color mode so empty bars stay visible


def level_color(percent: float) -> str:
    return GREEN if percent < 50 else YELLOW if percent < 80 else RED


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------
def vlen(text: str) -> int:
    """Visible length of a string, ignoring colour codes."""
    return len(ANSI_RE.sub("", text))


def clip(text: str, width: int) -> str:
    """Cut text to `width` visible columns while keeping colour codes intact."""
    out, shown, i = [], 0, 0
    while i < len(text):
        m = ANSI_RE.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
        elif shown < width:
            out.append(text[i])
            shown += 1
            i += 1
        else:
            break
    return "".join(out) + RESET


def pad(text: str, width: int) -> str:
    text = clip(text, width)
    return text + " " * (width - vlen(text))


def bar(percent: float, width: int, color: str | None = None) -> str:
    width = max(width, 1)
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100))
    color = color or level_color(percent)
    return f"{color}{'█' * filled}{TRACK}{TRACK_CHAR * (width - filled)}{RESET}"


def spark(values, width: int, color: str, ceiling: float | None = None) -> str:
    vals = list(values)[-width:]
    top = ceiling if ceiling else max(max(vals, default=0.0), 1e-9)
    cells = "".join(SPARKS[min(7, max(0, int(v / top * 8)))] for v in vals)
    return " " * (width - len(vals)) + f"{color}{cells}{RESET}"


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_uptime(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return (f"{days}d " if days else "") + f"{hours}h {rest // 60:02d}m"


def panel(title: str, rows: list[str], width: int, right: str = "", min_rows: int = 0) -> list[str]:
    inner = width - 4
    rows = rows + [""] * (min_rows - len(rows))
    used = 3 + len(title) + 1
    tail_len = (vlen(right) + 2 if right else 0) + 2
    fill = max(0, width - used - tail_len)
    tail = (f" {right}{FRAME} " if right else "") + "─╮"
    top = f"{FRAME}╭─ {BOLD}{TEXT}{title}{RESET}{FRAME} {'─' * fill}{tail}{RESET}"
    lines = [top]
    for row in rows:
        lines.append(f"{FRAME}│{RESET} {pad(row, inner)} {FRAME}│{RESET}")
    lines.append(f"{FRAME}╰{'─' * (width - 2)}╯{RESET}")
    return lines


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------
class Monitor:
    def __init__(self) -> None:
        self.host = socket.gethostname()
        self.os = f"{platform.system()} {platform.release()}"
        self.boot = psutil.boot_time()
        self.logical = psutil.cpu_count() or 1
        self.physical = psutil.cpu_count(logical=False) or self.logical
        self.hist = {k: deque(maxlen=HISTORY) for k in ("cpu", "mem", "down", "up", "read", "write")}
        self.rates = {"down": 0.0, "up": 0.0, "read": 0.0, "write": 0.0}
        self.disks: list = []
        self._disks_at = 0.0
        self._t = time.monotonic()
        self._net = self._net_counters()
        self._dio = self._disk_counters()
        self.net_total = self._net
        # The first cpu_percent() call only primes the counters.
        self.cores = psutil.cpu_percent(percpu=True) or [0.0]
        for p in psutil.process_iter():
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        self.update(record=False)

    @staticmethod
    def _net_counters():
        try:
            c = psutil.net_io_counters()
            return (c.bytes_recv, c.bytes_sent) if c else (0, 0)
        except Exception:
            return (0, 0)

    @staticmethod
    def _disk_counters():
        try:
            c = psutil.disk_io_counters()
            return (c.read_bytes, c.write_bytes) if c else (0, 0)
        except Exception:
            return (0, 0)

    @staticmethod
    def _read_disks():
        seen, found = set(), []
        for part in psutil.disk_partitions(all=False):
            if part.device in seen or "cdrom" in part.opts:
                continue
            if part.fstype in ("", "squashfs") or part.mountpoint.startswith("/snap"):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            seen.add(part.device)
            found.append((part.mountpoint, usage))
        if not found:  # containers often hide partitions: fall back to the root
            try:
                root = os.path.abspath(os.sep)
                found.append((root, psutil.disk_usage(root)))
            except OSError:
                pass
        return found[:MAX_DISKS]

    @staticmethod
    def _temperature():
        fn = getattr(psutil, "sensors_temperatures", None)
        if not fn:
            return None
        try:
            data = fn()
        except Exception:
            return None
        for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            if data.get(key):
                return max(t.current for t in data[key])
        for entries in data.values():
            if entries:
                return entries[0].current
        return None

    def update(self, record: bool = True) -> None:
        now = time.monotonic()
        dt = max(now - self._t, 1e-3)
        self._t = now

        self.cores = psutil.cpu_percent(percpu=True) or self.cores
        self.cpu = sum(self.cores) / len(self.cores)

        mem = psutil.virtual_memory()
        self.mem_total = mem.total
        self.mem_used = mem.total - mem.available
        self.mem_pct = mem.percent
        try:
            self.swap = psutil.swap_memory()
        except Exception:
            self.swap = None

        net, dio = self._net_counters(), self._disk_counters()
        self.rates["down"] = max(0.0, (net[0] - self._net[0]) / dt)
        self.rates["up"] = max(0.0, (net[1] - self._net[1]) / dt)
        self.rates["read"] = max(0.0, (dio[0] - self._dio[0]) / dt)
        self.rates["write"] = max(0.0, (dio[1] - self._dio[1]) / dt)
        self._net, self._dio, self.net_total = net, dio, net

        try:
            self.load = psutil.getloadavg()
        except (AttributeError, OSError):
            self.load = (0.0, 0.0, 0.0)
        try:
            freq = psutil.cpu_freq()
            self.freq = freq.current if freq else 0.0
        except Exception:
            self.freq = 0.0
        self.temp = self._temperature()
        try:
            self.battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        except Exception:
            self.battery = None

        rows = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            if p.info["pid"] != 0:
                rows.append(p.info)
        rows.sort(key=lambda i: i["cpu_percent"] or 0.0, reverse=True)
        self.nprocs = len(rows)
        self.procs = rows[:TOP_N]

        if now - self._disks_at > 5 or not self.disks:
            self.disks, self._disks_at = self._read_disks(), now

        if record:
            self.hist["cpu"].append(self.cpu)
            self.hist["mem"].append(self.mem_pct)
            for key, value in self.rates.items():
                self.hist[key].append(value)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def render(m: Monitor, width: int, interval: float) -> list[str]:
    W = max(64, min(width, 110))
    inner = W - 4
    out: list[str] = []

    clock = f"{MUTED}{datetime.now():%H:%M:%S}{RESET}"
    left = f"{BOLD}{CYAN}◆ pulse{RESET}  {MUTED}{m.host} · {m.os} · up {fmt_uptime(time.time() - m.boot)}{RESET}"
    left = clip(left, W - vlen(clock) - 1)
    out.append(left + " " * (W - vlen(left) - vlen(clock)) + clock)

    # ---- CPU --------------------------------------------------------------
    sw = min(40, inner // 3)
    bw = inner - 6 - 1 - 6 - 2 - sw
    cpu_color = level_color(m.cpu)
    rows = [
        f"{MUTED}Total{RESET} {bar(m.cpu, bw)} {cpu_color}{m.cpu:5.1f}%{RESET}  "
        f"{spark(m.hist['cpu'], sw, BLUE, 100)}"
    ]
    cores = m.cores[:64]
    cols = min(len(cores), max(1, inner // 22))
    colw = inner // cols
    cbw = max(2, colw - 11)
    for start in range(0, len(cores), cols):
        cells = []
        for i in range(start, min(start + cols, len(cores))):
            p = cores[i]
            cells.append(pad(f"{MUTED}C{i:<2d}{RESET} {bar(p, cbw)} {level_color(p)}{p:3.0f}%{RESET}", colw))
        rows.append("".join(cells))
    info = [f"load {m.load[0]:.2f} {m.load[1]:.2f} {m.load[2]:.2f}",
            f"{m.physical} cores / {m.logical} threads"]
    if m.freq:
        info.append(f"{m.freq / 1000:.2f} GHz")
    if m.temp is not None:
        info.append(f"{level_color(m.temp)}{m.temp:.0f}°C{MUTED}")
    if m.battery is not None:
        info.append(f"battery {m.battery.percent:.0f}%" + (" (charging)" if m.battery.power_plugged else ""))
    rows.append(f"{MUTED}{f'  {FRAME}·{MUTED}  '.join(info)}{RESET}")
    out += panel("CPU", rows, W, right=f"{cpu_color}{m.cpu:.0f}%{RESET}")

    # ---- Memory + Network -------------------------------------------------
    def memory_rows(iw: int) -> list[str]:
        mbw = iw - 12
        color = level_color(m.mem_pct)
        res = [
            f"{MUTED}RAM{RESET}  {bar(m.mem_pct, mbw)} {color}{m.mem_pct:5.1f}%{RESET}",
            f"     {MUTED}{human(m.mem_used)} of {human(m.mem_total)}{RESET}",
            f"     {spark(m.hist['mem'], iw - 5, PURPLE, 100)}",
        ]
        if m.swap and m.swap.total:
            res += [f"{MUTED}Swap{RESET} {bar(m.swap.percent, mbw)} {level_color(m.swap.percent)}{m.swap.percent:5.1f}%{RESET}",
                    f"     {MUTED}{human(m.swap.used)} of {human(m.swap.total)}{RESET}"]
        else:
            res += [f"{MUTED}Swap{RESET} {MUTED}not configured{RESET}"]
        return res

    def network_rows(iw: int) -> list[str]:
        def head(arrow: str, label: str, color: str, value: float) -> str:
            lft = f"{color}{arrow}{RESET} {MUTED}{label}{RESET}"
            rgt = f"{TEXT}{human(value)}/s{RESET}"
            return lft + " " * max(1, iw - vlen(lft) - vlen(rgt)) + rgt

        def line(key: str, color: str) -> str:
            ceil = max(max(m.hist[key], default=0.0), 1024.0)
            return spark(m.hist[key], iw, color, ceil)

        return [head("↓", "Download", CYAN, m.rates["down"]), line("down", CYAN),
                head("↑", "Upload", PURPLE, m.rates["up"]), line("up", PURPLE),
                f"{MUTED}total  ↓ {human(m.net_total[0])}   ↑ {human(m.net_total[1])}{RESET}"]

    mem_color = level_color(m.mem_pct)
    if W >= 92:
        wl = (W - 1) // 2
        wr = W - 1 - wl
        mem_rows, net_rows = memory_rows(wl - 4), network_rows(wr - 4)
        height = max(len(mem_rows), len(net_rows))
        left_p = panel("Memory", mem_rows, wl, right=f"{mem_color}{m.mem_pct:.0f}%{RESET}", min_rows=height)
        right_p = panel("Network", net_rows, wr, min_rows=height)
        out += [a + " " + b for a, b in zip(left_p, right_p)]
    else:
        out += panel("Memory", memory_rows(inner), W, right=f"{mem_color}{m.mem_pct:.0f}%{RESET}")
        out += panel("Network", network_rows(inner), W)

    # ---- Disks ------------------------------------------------------------
    show_detail = inner >= 70
    rows = []
    for mount, usage in m.disks:
        name = mount if len(mount) <= 14 else "…" + mount[-13:]
        dbw = inner - 14 - 1 - 1 - 6 - (21 if show_detail else 0)
        tail = f"  {MUTED}{f'{human(usage.used)} / {human(usage.total)}':>19}{RESET}" if show_detail else ""
        rows.append(f"{TEXT}{name:<14}{RESET} {bar(usage.percent, dbw)} "
                    f"{level_color(usage.percent)}{usage.percent:5.1f}%{RESET}{tail}")
    for label, key, color in (("Read ", "read", BLUE), ("Write", "write", YELLOW)):
        ceil = max(max(m.hist[key], default=0.0), 1024.0)
        rows.append(f"{MUTED}{label}{RESET} {TEXT}{f'{human(m.rates[key])}/s':>10}{RESET} "
                    f"{spark(m.hist[key], inner - 17, color, ceil)}")
    out += panel("Disks", rows, W)

    # ---- Processes --------------------------------------------------------
    nw = inner - 33
    rows = [f"{MUTED}{'PID':>7}  {'NAME':<{nw}} {'':8} {'CPU':>6}  {'MEM':>6}{RESET}"]
    for p in m.procs:
        cpu = (p["cpu_percent"] or 0.0) / m.logical
        mem = p["memory_percent"] or 0.0
        name = p["name"] or "?"
        rows.append(f"{MUTED}{p['pid']:>7}{RESET}  {TEXT}{name:<{nw}.{nw}}{RESET} {bar(cpu, 8)} "
                    f"{level_color(cpu)}{cpu:5.1f}%{RESET}  {MUTED}{mem:5.1f}%{RESET}")
    out += panel("Top processes", rows, W, right=f"{MUTED}{m.nprocs} running{RESET}")

    out.append(f"{MUTED} q quit  ·  refresh {interval:g}s  ·  v{__version__}{RESET}")
    return out


# --------------------------------------------------------------------------
# Terminal handling
# --------------------------------------------------------------------------
class Keys:
    """Tiny cross-platform non-blocking key reader."""

    fd = None

    def __enter__(self):
        if os.name != "nt" and sys.stdin.isatty():
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def wait(self, timeout: float):
        """Sleep up to `timeout` seconds; return a key if one was pressed."""
        if os.name == "nt":
            import msvcrt
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                time.sleep(0.05)
            return None
        if self.fd is None:
            time.sleep(timeout)
            return None
        import select
        ready, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 1).decode(errors="ignore") if ready else None


def run(interval: float) -> None:
    if os.name == "nt":
        os.system("")  # switches on ANSI colours in the Windows console
    monitor = Monitor()
    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    try:
        with Keys() as keys:
            key = keys.wait(min(interval, 0.5))
            while True:
                if key and (key.lower() == "q" or key == "\x03"):
                    break
                monitor.update()
                size = shutil.get_terminal_size((100, 40))
                frame = render(monitor, size.columns, interval)[: size.lines]
                out.write("\x1b[H" + "\x1b[K\n".join(frame) + "\x1b[K\x1b[J")
                out.flush()
                key = keys.wait(interval)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live system performance monitor for your terminal.")
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="refresh interval in seconds (default 1)")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--width", type=int, help="width for --once (default: terminal width)")
    parser.add_argument("--no-color", action="store_true", help="strip colours (with --once)")
    parser.add_argument("--version", action="version", version=f"pulse {__version__}")
    args = parser.parse_args()
    interval = max(0.2, args.interval)

    if args.once:
        if args.no_color:
            global TRACK_CHAR
            TRACK_CHAR = "░"
        monitor = Monitor()
        time.sleep(max(interval, 0.5))
        monitor.update()
        width = args.width or shutil.get_terminal_size((100, 40)).columns
        text = "\n".join(render(monitor, width, interval))
        print(ANSI_RE.sub("", text) if args.no_color else text)
    else:
        run(interval)


if __name__ == "__main__":
    main()
