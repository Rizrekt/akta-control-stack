#!/usr/bin/env python3
"""
make_manifest.py — bangun manifes integritas rak skill (SHA-256 per berkas).

Diadaptasi dari tools/skill_integrity.py SUPERAGENT V10. Lihat README.md folder ini untuk kredit + perbedaan penuh.

Pakai:
    python3 make_manifest.py                      # root bawaan: folder custom-skills di workspace
    python3 make_manifest.py --include-system     # + /opt/muse/skills (read-only)
    python3 make_manifest.py --root LABEL=PATH    # root tambahan/ganti (boleh diulang)
    python3 make_manifest.py --manifest PATH      # lokasi manifes lain (bawaan: folder ini)

KAPAN DIJALANKAN (aturan pakai, bukan saran): manifes hanya diperbarui
sesudah adopsi/edit skill yang DISENGAJA dan sudah DISAHKAN — tidak
pernah otomatis, tidak pernah untuk "membersihkan" temuan verify yang
belum ditelaah. Lihat README §Kapan manifes diperbarui.

Kode keluar: 0 = manifes tertulis; 2 = error (root tidak ada, dsb).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from integrity_common import (  # noqa: E402
    DEFAULT_MANIFEST,
    DEFAULT_ROOTS,
    MANIFEST_VERSION,
    OPTIONAL_ROOTS,
    scan_root,
)


def parse_roots(args) -> dict:
    roots = dict(DEFAULT_ROOTS)
    if args.include_system:
        roots.update(OPTIONAL_ROOTS)
    for item in args.root or []:
        if "=" not in item:
            raise SystemExit(f"--root harus LABEL=PATH, dapat: {item!r}")
        label, path = item.split("=", 1)
        roots[label] = Path(path).expanduser()
    return roots


def build_manifest(roots: dict) -> dict:
    sections = {}
    for label, root in roots.items():
        if not root.is_dir():
            raise SystemExit(f"root tidak ditemukan: {label} = {root}")
        files = scan_root(root)
        sections[label] = {
            "root": str(root),
            "file_count": len(files),
            "files": files,
        }
    payload = json.dumps(sections, sort_keys=True, separators=(",", ":")).encode()
    return {
        "version": MANIFEST_VERSION,
        "tool": "skill-integrity (adaptasi SUPERAGENT V10)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": sections,
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Bangun manifes integritas rak skill (SHA-256).")
    ap.add_argument("--include-system", action="store_true",
                    help="ikutkan /opt/muse/skills (read-only) ke manifes")
    ap.add_argument("--root", action="append", metavar="LABEL=PATH",
                    help="root tambahan atau pengganti (boleh diulang)")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                    help=f"lokasi manifes (bawaan: {DEFAULT_MANIFEST})")
    args = ap.parse_args()

    try:
        roots = parse_roots(args)
        manifest = build_manifest(roots)
    except SystemExit as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    total = sum(s["file_count"] for s in manifest["roots"].values())
    for label, s in manifest["roots"].items():
        print(f"  {label}: {s['file_count']} berkas  ({s['root']})")
    print(f"Manifes ditulis: {args.manifest} — total {total} berkas")
    print(f"manifest_sha256: {manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
