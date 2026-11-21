#!/usr/bin/env python3
"""
Tes process-watchdog — logika cap restart + deteksi hidup berbasis CPU-time.

Semua tabel proses adalah data `ps` contoh (string), bukan proses live,
kecuali perintah restart uji yang memang no-op (`python3 -c pass`).
Jalankan: python3 test_watchdog.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from watchdog import (LivenessTracker, PersistentState, ProcInfo, Watchdog,
                      WatchedProcess, find_matches, parse_ps_output,
                      parse_ps_time)

# ---------------------------------------------------------------------------
# Data ps contoh: format `ps -eo pid=,time=,args=`
# ---------------------------------------------------------------------------
PS_V1 = """\
  101  00:01:30 python3 /srv/example/worker-a.py
  102  00:00:45 python3 /srv/example/worker-b.py
  103  00:00:05 python3 /srv/example/watchdog.py --config config.json
  999  12:34 bash
"""

PS_V2_CPU_NAIK = """\
  101  00:01:47 python3 /srv/example/worker-a.py
  102  00:00:45 python3 /srv/example/worker-b.py
"""

PS_V2_CPU_DIAM = """\
  101  00:01:30 python3 /srv/example/worker-a.py
"""


def proc(name, pattern, cmd, **kw):
    return WatchedProcess(name=name, pattern=pattern, restart_command=cmd, **kw)


NOOP_CMD = [sys.executable, "-c", "pass"]


class TestParsePsTime(unittest.TestCase):
    def test_menit_detik(self):
        self.assertEqual(parse_ps_time("03:25"), 205)

    def test_jam_menit_detik(self):
        self.assertEqual(parse_ps_time("01:02:03"), 3723)

    def test_hari(self):
        self.assertEqual(parse_ps_time("2-00:00:10"), 2 * 86400 + 10)


class TestParsePsOutput(unittest.TestCase):
    def test_parse_dasar(self):
        procs = parse_ps_output(PS_V1)
        self.assertEqual(len(procs), 4)
        seek = [p for p in procs if p.pid == 101][0]
        self.assertEqual(seek.cpu_seconds, 90)
        self.assertIn("worker-a.py", seek.args)

    def test_baris_cacat_dilewati(self):
        procs = parse_ps_output("bukan baris ps\n\n  7  00:00:01 init\n")
        self.assertEqual([p.pid for p in procs], [7])


class TestFindMatches(unittest.TestCase):
    def test_bracket_pattern_cocok(self):
        procs = parse_ps_output(PS_V1)
        hits = find_matches(procs, "[w]orker-a.py", exclude_pids=set())
        self.assertEqual([p.pid for p in hits], [101])

    def test_watchdog_sendiri_dikecualikan(self):
        # Pola lebar yang juga mengenai baris watchdog sendiri (pid 103):
        # baris itu tetap tidak boleh terhitung sebagai proses terdaftar.
        procs = parse_ps_output(PS_V1)
        hits = find_matches(procs, "python3", exclude_pids=set())
        self.assertNotIn(103, [p.pid for p in hits])

    def test_pid_leluhur_dikecualikan(self):
        procs = parse_ps_output(PS_V1)
        hits = find_matches(procs, "[w]orker-a.py", exclude_pids={101})
        self.assertEqual(hits, [])


class TestLivenessTracker(unittest.TestCase):
    def test_tidak_ada_match_mati(self):
        t = LivenessTracker()
        self.assertEqual(t.update("x", []), "dead")

    def test_pid_baru_provisional_hidup(self):
        t = LivenessTracker()
        m = find_matches(parse_ps_output(PS_V1), "[w]orker-a.py", set())
        self.assertEqual(t.update("seek", m), "alive")

    def test_cpu_naik_hidup(self):
        t = LivenessTracker()
        t.update("seek", find_matches(parse_ps_output(PS_V1), "[w]orker-a.py", set()))
        m2 = find_matches(parse_ps_output(PS_V2_CPU_NAIK), "[w]orker-a.py", set())
        self.assertEqual(t.update("seek", m2), "alive")

    def test_cpu_diam_stalled_dianggap_mati(self):
        # Inti pelajaran dari insiden pgrep: proses TERLIHAT ada di daftar,
        # tapi CPU-time-nya tidak bergerak → bukan hidup.
        t = LivenessTracker()
        t.update("seek", find_matches(parse_ps_output(PS_V1), "[w]orker-a.py", set()))
        m2 = find_matches(parse_ps_output(PS_V2_CPU_DIAM), "[w]orker-a.py", set())
        self.assertEqual(t.update("seek", m2), "stalled")

    def test_tanpa_syarat_cpu_cukup_ada(self):
        t = LivenessTracker()
        m = find_matches(parse_ps_output(PS_V1), "[w]orker-a.py", set())
        t.update("seek", m, require_cpu_progress=False)
        self.assertEqual(t.update("seek", m, require_cpu_progress=False), "alive")


class WatchdogHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state_path = self.tmp / "state.json"
        self.status_path = self.tmp / "status.json"
        self.now = [1_000_000.0]
        self.table = [[]]  # tabel proses yang bisa diganti antar tick

    def make_wd(self, processes):
        state = PersistentState.load(self.state_path)
        return Watchdog(processes, state, self.state_path, self.status_path,
                        table_reader=lambda: self.table[0],
                        exclude_pids=set(), clock=lambda: self.now[0])


class TestRestartCapDanHalt(WatchdogHarness):
    def test_cap_5_per_jam_lalu_halted_dan_berhenti(self):
        p = proc("mati-total", "[t]idak-pernah-ada.py", NOOP_CMD,
                 max_restarts_per_hour=5)
        wd = self.make_wd([p])
        for i in range(5):
            ev = wd.tick()
            self.assertEqual(ev[0]["status"], "restarted", f"tick {i + 1}")
        # Tick ke-6: cap tercapai → HALTED, tidak ada restart lagi.
        ev = wd.tick()
        self.assertEqual(ev[0]["status"], "halted")
        self.assertIn("mati-total", wd.state.halted)
        # Tick ke-7: senyap (sudah halted), state tetap tercatat di status.json.
        self.assertEqual(wd.tick(), [])
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["processes"]["mati-total"]["state"], "halted")
        self.assertEqual(status["processes"]["mati-total"]["restarts_last_hour"], 5)
        # State persisten: watchdog baru dari berkas yang sama tetap HALTED.
        wd2 = self.make_wd([p])
        self.assertIn("mati-total", wd2.state.halted)
        self.assertEqual(wd2.tick(), [])

    def test_jendela_geser_melepas_cap(self):
        p = proc("mati-total", "[t]idak-pernah-ada.py", NOOP_CMD,
                 max_restarts_per_hour=2)
        wd = self.make_wd([p])
        wd.tick(); wd.tick()
        self.assertEqual(wd.tick()[0]["status"], "halted")
        # Operator membersihkan HALTED → boleh jalan lagi.
        self.assertEqual(wd.clear_halt("mati-total"), ["mati-total"])
        ev = wd.tick()
        self.assertEqual(ev[0]["status"], "restarted")

    def test_restart_lama_kedaluwarsa_dari_jendela(self):
        p = proc("mati-total", "[t]idak-pernah-ada.py", NOOP_CMD,
                 max_restarts_per_hour=2)
        wd = self.make_wd([p])
        wd.tick(); wd.tick()
        # Lompat 1 jam + 1 detik: dua restart lama keluar jendela,
        # jadi tick berikut restart lagi, BUKAN halted.
        self.now[0] += 3601
        ev = wd.tick()
        self.assertEqual(ev[0]["status"], "restarted")
        self.assertNotIn("mati-total", wd.state.halted)


class TestPantauSajaDanStalled(WatchdogHarness):
    def test_monitor_only_tidak_pernah_restart(self):
        p = proc("dipantau", "[t]idak-pernah-ada.py", None)
        wd = self.make_wd([p])
        ev = wd.tick()
        self.assertEqual(ev[0]["status"], "monitor_down")
        self.assertEqual(wd.state.restart_times.get("dipantau", []), [])
        self.assertNotIn("dipantau", wd.state.halted)

    def test_proses_stalled_direstart(self):
        # Proses muncul di tabel ps (seperti "ALIVE" palsu versi pgrep),
        # tapi CPU-time membeku antar tick → dianggap mati → direstart.
        frozen = [ProcInfo(pid=555, cpu_seconds=42, args="python3 /x/worker-a.py")]
        self.table[0] = frozen
        p = proc("seek", "[w]orker-a.py", NOOP_CMD)
        wd = self.make_wd([p])
        self.assertEqual(wd.tick(), [])  # tick 1: pid baru → provisional hidup
        ev = wd.tick()                   # tick 2: CPU diam → stalled → restart
        self.assertEqual(ev[0]["status"], "restarted")
        self.assertIn("stalled", ev[0]["detail"])

    def test_proses_hidup_cpu_bergerak_tidak_disentuh(self):
        self.table[0] = [ProcInfo(pid=555, cpu_seconds=42, args="python3 /x/worker-a.py")]
        p = proc("seek", "[w]orker-a.py", NOOP_CMD)
        wd = self.make_wd([p])
        wd.tick()
        self.table[0] = [ProcInfo(pid=555, cpu_seconds=50, args="python3 /x/worker-a.py")]
        self.assertEqual(wd.tick(), [])
        self.assertEqual(wd.state.restart_times.get("seek", []), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
