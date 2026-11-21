#!/usr/bin/env python3
"""
process-watchdog — penjaga proses yang HANYA menyentuh daftar eksplisit operator.

Diadaptasi dari tools/watchdog.py SUPERAGENT V10,
dengan dua perbaikan wajib dari pelajaran operasi kita sendiri:

1. DETEKSI HIDUP TIDAK PAKAI pgrep MENTAH.
   `pgrep -f <pola>` mencocokkan shell pemeriksa itu sendiri (string pola ikut
   di command line pemeriksa) → laporan "ALIVE" palsu (kejadian 2026-11-14:
   VM reboot membunuh proxy, pgrep tetap bilang hidup dua kali).
   Di sini tabel proses dibaca dari `ps -eo pid=,time=,args=` dan dicocokkan
   DI DALAM Python (tidak ada pipeline grep), PID watchdog sendiri + leluhurnya
   dikecualikan, dan pola config ditulis gaya bracket ("[s]eek-proxy.py")
   sehingga tidak akan pernah mencocokkan baris perintah pemeriksa mana pun.
   Lebih dari itu: proses dinyatakan HIDUP hanya bila CPU-time-nya BERTAMBAH
   antar dua cek. Proses yang CPU-time-nya diam = dianggap mati/hang,
   apa pun kata daftar proses.

2. WATCHDOG TIDAK PERNAH MEMBUNUH PROSES.
   Tidak ada kill/signal di berkas ini. Satu-satunya aksi tulis adalah
   menjalankan perintah restart yang ditulis operator di config, dan itu pun
   hanya untuk proses yang sudah terdeteksi mati. Proses yang masih hidup
   tidak pernah disentuh.

Aturan lain yang dipertahankan dari sumbernya:
- Hanya proses yang DIDAFTARKAN EKSPLISIT operator di config.json yang
  dipantau. Tidak ada penemuan proses otomatis, tidak ada input lain.
- Restart di-rate-limit: maks `max_restarts_per_hour` (bawaan 5) per proses
  dalam jendela geser 1 jam. Sesudah cap tercapai, proses ditandai HALTED
  (persisten di state.json + terlihat di status.json) dan TIDAK direstart
  lagi sampai operator membersihkannya (`--clear-halt <nama>` atau "all").
- Perintah restart berupa LIST argv (bukan string shell) — anti-injeksi,
  sama seperti sumbernya.
- `restart_command: null` di config = mode pantau-saja: kematian dilaporkan
  sebagai event, tidak pernah ada restart otomatis.

State persisten (state.json): cap restart + flag HALTED selamat dari
restart watchdog sendiri — mengulang watchdog tidak mereset jatah restart.
Status terakhir (status.json) ditulis ulang tiap tick: per proses berisi
state (alive/dead/stalled/halted/monitor_down), pid yang cocok, CPU-time
terakhir, jumlah restart dalam jendela, dan event terakhir.

Stdlib murni. Python 3.10+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

DEFAULT_MAX_RESTARTS_PER_HOUR = 5
WINDOW_S = 3600.0


# --------------------------------------------------------------------------
# Parsing tabel proses (`ps -eo pid=,time=,args=`)
# --------------------------------------------------------------------------

def parse_ps_time(text: str) -> int:
    """Ubah kolom TIME ps ([[DD-]hh:]mm:ss) menjadi detik CPU kumulatif."""
    text = text.strip()
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        days = int(day_part)
    parts = [int(p) for p in text.split(":")]
    if len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"format TIME ps tidak dikenal: {text!r}")
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


@dataclass
class ProcInfo:
    pid: int
    cpu_seconds: int
    args: str


def parse_ps_output(text: str) -> list[ProcInfo]:
    """Parse keluaran `ps -eo pid=,time=,args=`. Baris cacat dilewati."""
    procs: list[ProcInfo] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid = int(fields[0])
            cpu = parse_ps_time(fields[1])
        except ValueError:
            continue
        procs.append(ProcInfo(pid=pid, cpu_seconds=cpu, args=fields[2]))
    return procs


def read_process_table() -> list[ProcInfo]:
    """Baca tabel proses live. Satu-satunya pembacaan proses di tool ini."""
    r = subprocess.run(
        ["ps", "-eo", "pid=,time=,args="],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ps gagal: {r.stderr.strip()}")
    return parse_ps_output(r.stdout)


def ancestor_pids() -> set[int]:
    """PID watchdog ini + semua leluhurnya (untuk pengecualian self-match)."""
    pids: set[int] = set()
    pid = os.getpid()
    while pid and pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                # field ke-4 (ppid) sesudah comm dalam kurung — comm bisa
                # mengandung spasi/kurung, jadi potong dari ')' terakhir.
                stat = fh.read().decode("utf-8", "replace")
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except Exception:
            break
    pids.add(os.getpid())
    return pids


def find_matches(procs: list[ProcInfo], pattern: str,
                 exclude_pids: set[int]) -> list[ProcInfo]:
    """Cocokkan pola (regex; gaya bracket didukung) lawan args proses.

    Proses yang args-nya menyebut berkas watchdog ini sendiri selalu
    dikecualikan — pemeriksa tidak boleh mencocokkan dirinya.
    """
    rx = re.compile(pattern)
    self_marker = Path(__file__).name
    out: list[ProcInfo] = []
    for p in procs:
        if p.pid in exclude_pids:
            continue
        if self_marker in p.args:
            continue
        if rx.search(p.args):
            out.append(p)
    return out


# --------------------------------------------------------------------------
# Penilaian hidup: CPU-time wajib bertambah antar dua cek
# --------------------------------------------------------------------------

class LivenessTracker:
    """Mengingat snapshot CPU per (nama, pid) antar tick.

    Vonis per proses terdaftar:
    - "dead"    : tidak ada pid yang cocok sama sekali.
    - "alive"   : ada pid cocok yang CPU-time-nya bertambah sejak cek lalu,
                  ATAU pid baru pertama kali terlihat (provisional — baseline
                  dicatat; pada cek berikutnya CPU-nya wajib sudah bergerak).
    - "stalled" : pid cocok ada, tapi semuanya pid lama dengan CPU-time diam.
                  Diperlakukan sebagai MATI (proses hang / zombie), sesuai
                  pelajaran: CPU-time tidak nambah = mati, apa pun kata pgrep.
    """

    def __init__(self) -> None:
        self._prev: dict[str, dict[int, int]] = {}

    def update(self, name: str, matches: list[ProcInfo],
               require_cpu_progress: bool = True) -> str:
        current = {p.pid: p.cpu_seconds for p in matches}
        prev = self._prev.get(name, {})
        self._prev[name] = current
        if not current:
            return "dead"
        if not require_cpu_progress:
            return "alive"
        for pid, cpu in current.items():
            if pid not in prev:
                return "alive"  # pid baru → provisional, baseline tercatat
            if cpu > prev[pid]:
                return "alive"
        return "stalled"


# --------------------------------------------------------------------------
# Konfigurasi (daftar eksplisit operator — satu-satunya sumber kebenaran)
# --------------------------------------------------------------------------

@dataclass
class WatchedProcess:
    name: str
    pattern: str                       # regex gaya bracket, mis. "[s]eek-proxy.py"
    restart_command: Optional[list]    # list argv, atau None = pantau-saja
    cwd: Optional[str] = None
    max_restarts_per_hour: int = DEFAULT_MAX_RESTARTS_PER_HOUR
    require_cpu_progress: bool = True
    restart_timeout_s: int = 120


def load_config(path: Path) -> list[WatchedProcess]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw["processes"] if isinstance(raw, dict) else raw
    procs: list[WatchedProcess] = []
    seen: set[str] = set()
    for e in entries:
        name = e["name"]
        if name in seen:
            raise ValueError(f"nama proses duplikat di config: {name}")
        seen.add(name)
        cmd = e.get("restart_command")
        if cmd is not None and not (isinstance(cmd, list) and cmd
                                    and all(isinstance(x, str) for x in cmd)):
            raise ValueError(
                f"{name}: restart_command harus list argv non-kosong atau null")
        procs.append(WatchedProcess(
            name=name,
            pattern=e["pattern"],
            restart_command=cmd,
            cwd=e.get("cwd"),
            max_restarts_per_hour=int(e.get("max_restarts_per_hour",
                                            DEFAULT_MAX_RESTARTS_PER_HOUR)),
            require_cpu_progress=bool(e.get("require_cpu_progress", True)),
            restart_timeout_s=int(e.get("restart_timeout_s", 120)),
        ))
    if not procs:
        raise ValueError("config kosong — tidak ada proses terdaftar")
    return procs


# --------------------------------------------------------------------------
# State persisten (cap restart + HALTED) dan status terakhir
# --------------------------------------------------------------------------

@dataclass
class PersistentState:
    restart_times: dict[str, list] = field(default_factory=dict)
    halted: dict[str, str] = field(default_factory=dict)  # nama → alasan

    @classmethod
    def load(cls, path: Path) -> "PersistentState":
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return cls(restart_times=raw.get("restart_times", {}),
                           halted=raw.get("halted", {}))
            except Exception:
                pass  # state rusak → mulai bersih, jangan crash
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"restart_times": self.restart_times, "halted": self.halted},
            indent=2), encoding="utf-8")
        tmp.replace(path)

    def recent_restarts(self, name: str, now: float) -> list:
        times = [t for t in self.restart_times.get(name, [])
                 if now - t < WINDOW_S]
        self.restart_times[name] = times
        return times


# --------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------

class Watchdog:
    def __init__(self, processes: list[WatchedProcess], state: PersistentState,
                 state_path: Path, status_path: Path,
                 table_reader: Callable[[], list[ProcInfo]] = read_process_table,
                 exclude_pids: Optional[set[int]] = None,
                 clock: Callable[[], float] = time.time):
        self.processes = processes
        self.state = state
        self.state_path = state_path
        self.status_path = status_path
        self._read_table = table_reader
        self._exclude = exclude_pids if exclude_pids is not None else ancestor_pids()
        self._clock = clock
        self.tracker = LivenessTracker()
        self._status: dict[str, dict] = {}

    # -- restart: SATU-SATUNYA aksi tulis; hanya untuk proses yang mati ----
    def _do_restart(self, proc: WatchedProcess) -> None:
        subprocess.run(proc.restart_command, cwd=proc.cwd,
                       capture_output=True, timeout=proc.restart_timeout_s)

    def _record_status(self, proc: WatchedProcess, state: str,
                       matches: list[ProcInfo], now: float,
                       event: Optional[dict]) -> None:
        self._status[proc.name] = {
            "state": state,
            "pids": [p.pid for p in matches],
            "cpu_seconds": {str(p.pid): p.cpu_seconds for p in matches},
            "restarts_last_hour": len(self.state.recent_restarts(proc.name, now)),
            "max_restarts_per_hour": proc.max_restarts_per_hour,
            "halt_reason": self.state.halted.get(proc.name),
            "last_event": event,
            "checked_at": now,
        }

    def tick(self) -> list[dict]:
        """Satu siklus cek untuk semua proses terdaftar. Return daftar event."""
        now = self._clock()
        try:
            table = self._read_table()
        except Exception as e:
            return [{"proc": None, "status": "table_error", "detail": repr(e)}]
        events: list[dict] = []
        for proc in self.processes:
            matches = find_matches(table, proc.pattern, self._exclude)
            verdict = self.tracker.update(proc.name, matches,
                                          proc.require_cpu_progress)
            event: Optional[dict] = None

            if proc.name in self.state.halted:
                self._record_status(proc, "halted", matches, now, None)
                continue
            if verdict == "alive":
                self._record_status(proc, "alive", matches, now, None)
                continue

            # verdict "dead" / "stalled" → proses dianggap mati.
            if proc.restart_command is None:
                event = {"proc": proc.name, "status": "monitor_down",
                         "detail": f"{verdict} — pantau-saja, tidak ada restart otomatis"}
                self._record_status(proc, "monitor_down", matches, now, event)
                events.append(event)
                continue

            recent = self.state.recent_restarts(proc.name, now)
            if len(recent) >= proc.max_restarts_per_hour:
                reason = (f"cap restart tercapai: {len(recent)}x dalam 1 jam "
                          f"(maks {proc.max_restarts_per_hour}) — berhenti, butuh operator")
                self.state.halted[proc.name] = reason
                self.state.save(self.state_path)
                event = {"proc": proc.name, "status": "halted", "detail": reason}
                self._record_status(proc, "halted", matches, now, event)
                events.append(event)
                continue

            try:
                self._do_restart(proc)
                recent.append(now)
                self.state.restart_times[proc.name] = recent
                self.state.save(self.state_path)
                event = {"proc": proc.name, "status": "restarted",
                         "detail": f"terdeteksi {verdict}; restart ke-{len(recent)} dalam 1 jam"}
                self._record_status(proc, "restarted", matches, now, event)
            except Exception as e:
                event = {"proc": proc.name, "status": "restart_failed",
                         "detail": repr(e)}
                self._record_status(proc, "restart_failed", matches, now, event)
            events.append(event)

        self._write_status(now)
        return events

    def _write_status(self, now: float) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"updated_at": now, "processes": self._status}, indent=2),
            encoding="utf-8")
        tmp.replace(self.status_path)

    def clear_halt(self, name: Optional[str]) -> list[str]:
        """Bersihkan flag HALTED (satu nama atau semua). Return yang dibersihkan."""
        targets = list(self.state.halted) if name in (None, "all") else [name]
        cleared = [t for t in targets if t in self.state.halted]
        for t in cleared:
            del self.state.halted[t]
            self.state.restart_times[t] = []
        if cleared:
            self.state.save(self.state_path)
        return cleared

    def run(self, interval_s: int = 60) -> None:
        while True:
            for ev in self.tick():
                print(f"watchdog: {ev['proc']} → {ev['status']}"
                      + (f" ({ev.get('detail')})" if ev.get("detail") else ""),
                      flush=True)
            time.sleep(interval_s)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Watchdog proses terdaftar-eksplisit (tanpa pgrep, tanpa kill).")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")),
                    help="path config.json (daftar proses operator)")
    ap.add_argument("--state-dir", default=str(Path.home() / ".process-watchdog"),
                    help="folder state.json + status.json")
    ap.add_argument("--once", action="store_true", help="satu tick lalu keluar")
    ap.add_argument("--interval", type=int, default=60, help="jeda antar tick (detik)")
    ap.add_argument("--clear-halt", metavar="NAMA|all",
                    help="bersihkan flag HALTED lalu keluar")
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    status_path = state_dir / "status.json"
    state = PersistentState.load(state_path)

    if args.clear_halt:
        wd = Watchdog([], state, state_path, status_path)
        cleared = wd.clear_halt(args.clear_halt)
        print(f"HALTED dibersihkan: {cleared or '(tidak ada)'}")
        return 0

    processes = load_config(Path(args.config))
    wd = Watchdog(processes, state, state_path, status_path)
    if args.once:
        for ev in wd.tick():
            print(f"watchdog: {ev['proc']} → {ev['status']}"
                  + (f" ({ev.get('detail')})" if ev.get("detail") else ""))
        return 0
    wd.run(interval_s=args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
