#!/usr/bin/env python3
"""
integrity_common.py — logika bersama make_manifest.py + verify.py.

Diadaptasi dari tools/skill_integrity.py SUPERAGENT V10 — lihat README.md di folder ini untuk kredit + perbedaan.

Satu-satunya tempat aturan "berkas apa yang ikut di-hash" hidup, supaya
pembuat manifes dan pemeriksa tidak pernah bisa berbeda pendapat.

ATURAN CAKUPAN (perbaikan atas sumber — baca README §Perbedaan):
- SEMUA jenis berkas di-hash (sumber hanya .md/.py — di rak kita itu
  meloloskan .mjs/.js/.sh yang justru bisa dieksekusi).
- Berkas HIDUP/STATE dikecualikan berdasarkan sifatnya, bukan namanya:
  ekstensi data runtime (.jsonl, .log, .db, .sqlite, .sqlite3, .pyc)
  dan direktori runtime (__pycache__, .git, node_modules, logs).
  Berkas KODE yang kebetulan bernama "state" (mis. state.py) TETAP
  di-hash — dia kode, bukan state. Di sumbernya, logs/treasury.jsonl
  dan memory/*.md ikut ter-hash sehingga operasi normal (satu baris
  log baru) menggagalkan verifikasi boot berikutnya; itu cacat yang
  diperbaiki di sini.
- Manifes itu sendiri tidak pernah ikut di-hash.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = TOOL_DIR / "skills-manifest.json"
MANIFEST_NAME = "skills-manifest.json"
MANIFEST_VERSION = "1.0"

# Root bawaan: label -> path. /opt/muse/skills bersifat opsional
# (read-only, milik sistem) dan hanya masuk bila diminta eksplisit.
DEFAULT_ROOTS = {
    "custom-skills": Path.home() / "workspace" / "custom-skills",
}
OPTIONAL_ROOTS = {
    "system-skills": Path("/opt/muse/skills"),
}

# Direktori yang tidak pernah berisi kode skill: isinya artefak runtime.
EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", "logs"}

# Ekstensi berkas hidup/state: berubah karena operasi normal, bukan
# karena edit yang disengaja. Meng-hash-nya = verifikasi gagal palsu.
EXCLUDE_SUFFIXES = {".jsonl", ".log", ".db", ".sqlite", ".sqlite3", ".pyc"}

# Nama berkas spesifik yang dikecualikan (manifes sendiri + kunci tulis).
EXCLUDE_NAMES = {MANIFEST_NAME}
EXCLUDE_SUFFIXES_FULL = {".write-lock"}


def is_excluded(rel: Path) -> bool:
    """True bila berkas (path relatif terhadap root-nya) tidak di-hash."""
    if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
        return True
    if rel.name in EXCLUDE_NAMES:
        return True
    name = rel.name.lower()
    if any(name.endswith(suf) for suf in EXCLUDE_SUFFIXES_FULL):
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def iter_skill_files(root: Path):
    """Yield Path absolut semua berkas yang masuk cakupan, terurut."""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if is_excluded(rel):
            continue
        yield p


def sha256_file(path: Path) -> str:
    """SHA-256 isi berkas, dibaca per blok (berkas besar aman)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_root(root: Path) -> dict:
    """Pindai satu root -> {relpath_posix: sha256}."""
    return {
        p.relative_to(root).as_posix(): sha256_file(p)
        for p in iter_skill_files(root)
    }
