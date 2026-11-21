#!/usr/bin/env python3
"""
verify.py — verifikasi rak skill lawan manifes integritas.

Diadaptasi dari tools/skill_integrity.py SUPERAGENT V10. Lihat README.md folder ini untuk kredit + perbedaan penuh.

Pakai:
    python3 verify.py                    # cek semua root di manifes
    python3 verify.py --manifest PATH    # manifes lain
    python3 verify.py --quiet            # hanya baris ringkasan + temuan

MODE DEGRADASI PARSIAL (prinsip mengikat, warisan P2 studi kita):
verify ini HANYA melaporkan. Mismatch TIDAK mematikan, menahan, atau
mengubah apa pun — tidak ada proses yang dihentikan, tidak ada berkas
yang disentuh. Temuan diserahkan ke operator untuk ditelaah; keputusan
(telaah lalu perbarui manifes, atau pulihkan berkas) selalu manusia.
Ini meniru boot paket sumber: integritas gagal -> temuan ditampilkan,
yang ditahan hanya operasi berisiko, agen tetap hidup.

Keluaran per berkas: OK / CHANGED / MISSING / NEW.
Kode keluar: 0 = semua cocok; 1 = ada temuan (CHANGED/MISSING/NEW);
2 = error (manifes tidak ada/rusak, root hilang).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from integrity_common import DEFAULT_MANIFEST, scan_root  # noqa: E402


def verify(manifest_path: Path, quiet: bool = False) -> int:
    if not manifest_path.exists():
        print(f"ERROR: manifes tidak ada: {manifest_path} — "
              "jalankan make_manifest.py dulu di keadaan rak yang sudah disahkan.",
              file=sys.stderr)
        return 2
    try:
        manifest = json.loads(manifest_path.read_text())
        sections = manifest["roots"]
    except Exception as e:
        print(f"ERROR: manifes rusak/tidak terbaca: {e}", file=sys.stderr)
        return 2

    # Konsistensi internal manifes: hash atas bagian roots harus cocok.
    payload = json.dumps(sections, sort_keys=True, separators=(",", ":")).encode()
    if manifest.get("manifest_sha256") != hashlib.sha256(payload).hexdigest():
        print("ERROR: manifest_sha256 di manifes tidak cocok dengan isinya — "
              "manifes kemungkinan rusak/diubah tangan. Telaah sebelum percaya "
              "hasil apa pun dari berkas ini.", file=sys.stderr)
        return 2

    findings = 0
    total_ok = 0
    for label, section in sections.items():
        root = Path(section["root"])
        if not root.is_dir():
            print(f"ERROR: root hilang: {label} = {root}", file=sys.stderr)
            return 2
        locked: dict = section["files"]
        current = scan_root(root)

        changed, missing, new = [], [], []
        for rel, h in locked.items():
            if rel not in current:
                missing.append(rel)
            elif current[rel] != h:
                changed.append(rel)
        for rel in current:
            if rel not in locked:
                new.append(rel)
        ok = len(locked) - len(changed) - len(missing)
        total_ok += ok
        findings += len(changed) + len(missing) + len(new)

        print(f"[{label}] {root}")
        if not quiet:
            for rel in sorted(set(locked) & set(current)):
                if current[rel] == locked[rel]:
                    print(f"  OK       {rel}")
        for rel in changed:
            print(f"  CHANGED  {rel}")
        for rel in missing:
            print(f"  MISSING  {rel}")
        for rel in new:
            print(f"  NEW      {rel}  (belum di-manifes — telaah sebelum dipakai)")
        print(f"  -> OK {ok}, CHANGED {len(changed)}, "
              f"MISSING {len(missing)}, NEW {len(new)}")

    if findings == 0:
        print(f"INTEGRITAS OK — {total_ok} berkas cocok dengan manifes "
              f"({manifest_path}). Tidak ada yang diubah/dihentikan.")
        return 0
    print(f"TEMUAN: {findings} berkas menyimpang dari manifes. "
          "Mode degradasi parsial: temuan ini HANYA dilaporkan — tidak ada "
          "yang dimatikan atau diubah. Telaah: (a) perubahan disengaja & "
          "sudah disahkan -> perbarui manifes dengan make_manifest.py; "
          "(b) tidak dikenal -> pulihkan berkas dari sumber tepercaya.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Verifikasi rak skill lawan manifes integritas.")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                    help=f"lokasi manifes (bawaan: {DEFAULT_MANIFEST})")
    ap.add_argument("--quiet", action="store_true",
                    help="sembunyikan baris OK per berkas")
    args = ap.parse_args()
    return verify(args.manifest, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
