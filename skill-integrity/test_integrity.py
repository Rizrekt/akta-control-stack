#!/usr/bin/env python3
"""
test_integrity.py — tes ujung-ke-ujung make_manifest.py + verify.py.

Menjalankan kedua skrip sebagai proses terpisah (CLI asli, bukan impor)
di rak skill buatan sementara:
  1. buat manifes -> verify bersih (exit 0)
  2. ubah isi berkas -> CHANGED (exit 1)
  3. hapus berkas -> MISSING (exit 1)
  4. tambah berkas baru -> NEW (exit 1)
  5. berkas HIDUP (.jsonl log + berkas di logs/) berubah -> tetap bersih
     (exit 0) — regresi untuk cacat sumber yang meng-hash berkas hidup
  6. berkas kode bernama state.py berubah -> CHANGED (kode tetap di-hash)
  7. manifes hilang -> exit 2
  8. manifes dirusak tangan (isi diubah tanpa hash) -> exit 2

Jalankan: python3 test_integrity.py  (unittest, exit 0 bila semua lulus)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
MAKE = TOOL_DIR / "make_manifest.py"
VERIFY = TOOL_DIR / "verify.py"


class TestIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.shelf = base / "shelf"
        (self.shelf / "skill-a").mkdir(parents=True)
        (self.shelf / "skill-a" / "SKILL.md").write_text("# Skill A\nisi awal\n")
        (self.shelf / "skill-a" / "run.py").write_text("print('a')\n")
        (self.shelf / "skill-a" / "state.py").write_text("STATE = 1\n")
        self.manifest = base / "manifest.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_make(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(MAKE),
             "--root", f"test-shelf={self.shelf}",
             "--manifest", str(self.manifest)],
            capture_output=True, text=True)

    def run_verify(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(VERIFY), "--manifest", str(self.manifest)],
            capture_output=True, text=True)

    def make_clean(self):
        r = self.run_make()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.manifest.exists())

    def test_01_build_then_verify_clean(self):
        self.make_clean()
        m = json.loads(self.manifest.read_text())
        self.assertEqual(m["roots"]["test-shelf"]["file_count"], 3)
        r = self.run_verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("INTEGRITAS OK", r.stdout)

    def test_02_changed_file_detected(self):
        self.make_clean()
        (self.shelf / "skill-a" / "SKILL.md").write_text("# Skill A\nISI DIUBAH\n")
        r = self.run_verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("CHANGED  skill-a/SKILL.md", r.stdout)

    def test_03_missing_file_detected(self):
        self.make_clean()
        (self.shelf / "skill-a" / "run.py").unlink()
        r = self.run_verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING  skill-a/run.py", r.stdout)

    def test_04_new_file_detected(self):
        self.make_clean()
        (self.shelf / "skill-a" / "extra.md").write_text("baru\n")
        r = self.run_verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("NEW      skill-a/extra.md", r.stdout)

    def test_05_living_files_excluded(self):
        # Log runtime berubah tiap operasi normal: tidak boleh mematahkan
        # verifikasi (cacat sumber: logs/treasury.jsonl ikut ter-hash).
        log = self.shelf / "skill-a" / "runs.jsonl"
        log.write_text('{"run": 1}\n')
        logsdir = self.shelf / "skill-a" / "logs"
        logsdir.mkdir()
        (logsdir / "boot.log").write_text("boot 1\n")
        self.make_clean()
        log.write_text('{"run": 1}\n{"run": 2}\n')
        (logsdir / "boot.log").write_text("boot 1\nboot 2\n")
        r = self.run_verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_06_code_named_state_still_hashed(self):
        # state.py adalah KODE, bukan state — perubahan padanya wajib ketahuan.
        self.make_clean()
        (self.shelf / "skill-a" / "state.py").write_text("STATE = 2\n")
        r = self.run_verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("CHANGED  skill-a/state.py", r.stdout)

    def test_07_missing_manifest_is_error(self):
        r = self.run_verify()
        self.assertEqual(r.returncode, 2)

    def test_08_tampered_manifest_is_error(self):
        self.make_clean()
        m = json.loads(self.manifest.read_text())
        m["roots"]["test-shelf"]["files"]["skill-a/SKILL.md"] = "0" * 64
        self.manifest.write_text(json.dumps(m, indent=2) + "\n")
        r = self.run_verify()
        self.assertEqual(r.returncode, 2)
        self.assertIn("manifest_sha256", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
