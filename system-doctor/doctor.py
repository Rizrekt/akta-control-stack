#!/usr/bin/env python3
"""system-doctor — one-command health check for an AkTa Control Stack deployment.

Adapted from the doctor/self-check pattern in the SUPERAGENT V10
agent-tooling package: counts, integrity, links dalam SATU laporan kesehatan,
supaya diagnosis saat ada yang mati (pola reboot VM berulang, wallet sessions drop, local proxies die) tidak lagi dicek per bagian secara manual.

READ-ONLY MUTLAK: tool ini hanya membaca — HTTP GET ke proxy lokal,
`baw wallet status` (perintah baca CLI), stat berkas, dan jam sistem.
Ia TIDAK menyalakan ulang proxy, TIDAK re-pair sesi, TIDAK menulis
berkas apa pun, dan TIDAK membaca isi session.json / kredensial apa pun.

Kode keluar: 0 bila tidak ada FAIL (WARN diperbolehkan), 1 bila ada FAIL.

Pemakaian: python3 doctor.py [--json]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
OK, WARN, FAIL = "OK", "WARN", "FAIL"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}

results: list[dict] = []


def add(check: str, status: str, evidence: str) -> None:
    results.append({"check": check, "status": status, "evidence": evidence})


# ---------------------------------------------------------------- local proxies
def check_proxy(name: str, port: int) -> None:
    """Local proxy menjawab HTTP 404 di root bila HIDUP (itu tanda sehatnya).
    000/kosong (koneksi ditolak/timeout) = MATI."""
    url = f"http://127.0.0.1:{port}/"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # koneksi ditolak / timeout / dll
        add(name, FAIL, f"port {port} tidak menjawab ({type(e).__name__}) — proxy MATI")
        return
    if code == 404:
        add(name, OK, f"HTTP 404 di {url} — proxy hidup (404 = sehat untuk proxy ini)")
    else:
        add(name, WARN, f"HTTP {code} di {url} — menjawab, tapi bukan 404 yang diharapkan")


# ---------------------------------------------------------------- sesi baw
def check_baw(label: str, env_extra: dict | None) -> None:
    """`baw wallet status --json` adalah perintah BACA. Isi session.json
    tidak pernah dibuka/dicetak oleh tool ini."""
    baw = HOME / "workspace" / "tools" / "bin" / "baw"
    if not baw.exists():
        add(f"baw {label}", FAIL, f"CLI baw tidak ditemukan di {baw}")
        return
    env = dict(os.environ)
    env["PATH"] = f"{HOME}/.local/bin:{HOME}/workspace/tools/bin:" + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run(
            [str(baw), "wallet", "status", "--json"],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except subprocess.TimeoutExpired:
        add(f"baw {label}", WARN, "baw wallet status timeout 30 dtk (endpoint baw pernah hang dan pulih sendiri — coba lagi nanti)")
        return
    except Exception as e:
        add(f"baw {label}", FAIL, f"gagal menjalankan baw: {type(e).__name__}")
        return
    try:
        data = json.loads(r.stdout)
        status = data.get("data", {}).get("status", "?")
    except Exception:
        add(f"baw {label}", FAIL, f"keluaran baw tidak bisa dibaca (rc={r.returncode})")
        return
    if status == "CONNECTED":
        add(f"baw {label}", OK, "status CONNECTED")
    else:
        add(f"baw {label}", FAIL, f"status {status} — sesi jatuh, perlu re-pair oleh user saat aktif")


# ---------------------------------------------------------------- disk & /tmp
def check_disk() -> None:
    try:
        u = shutil.disk_usage("/")
        avail_gb = u.free / 1e9
        pct = u.used / u.total * 100
        if avail_gb < 0.5:
            add("Disk /", FAIL, f"sisa {avail_gb:.2f} GB ({pct:.0f}% terpakai) — kritis")
        elif avail_gb < 1.5:
            add("Disk /", WARN, f"sisa {avail_gb:.2f} GB ({pct:.0f}% terpakai)")
        else:
            add("Disk /", OK, f"sisa {avail_gb:.2f} GB ({pct:.0f}% terpakai dari {u.total/1e9:.1f} GB)")
    except Exception as e:
        add("Disk /", WARN, f"tidak bisa dibaca ({type(e).__name__})")
    # /tmp = tmpfs 512 MB di VM ini dan HILANG saat restart; ukur isi sebenarnya.
    try:
        total = 0
        for root, _dirs, files in os.walk("/tmp"):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        mb = total / 1e6
        status = WARN if mb > 400 else OK
        add("Isi /tmp", status, f"{mb:.1f} MB terpakai (tmpfs 512 MB; /tmp hilang saat VM restart — berkas kerja jangan di sini)")
    except Exception as e:
        add("Isi /tmp", WARN, f"tidak bisa diukur ({type(e).__name__})")


# ---------------------------------------------------------------- manifes integritas skill
def check_skill_integrity() -> None:
    """Skill-integrity manifest presence check (SHA-256 manifest pattern).
    Bila tool/manifesnya belum terpasang, itu WARN — bukan FAIL."""
    base = HOME / "workspace" / "tools" / "skill-integrity"
    candidates = []
    if base.exists():
        for pat in ("*.json", "*lock*"):
            candidates += [p for p in base.rglob(pat) if p.is_file()]
    if candidates:
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        age_h = (time.time() - newest.stat().st_mtime) / 3600
        add("Skill-integrity", OK, f"manifes ada: {newest.name} (umur {age_h:.1f} jam) di {base}")
    elif base.exists():
        add("Skill-integrity", WARN, f"tool terpasang di {base} tapi berkas manifes (JSON/lock) belum dibuat")
    else:
        add("Skill-integrity", WARN, "belum terpasang — tidak ada verifikasi integritas rak skill")


# ---------------------------------------------------------------- spend-governor
def check_governor() -> None:
    gdir = Path(os.environ.get("SPEND_GOVERNOR_DIR", str(HOME / ".spend-governor"))).expanduser()
    db = gdir / "governor.db"
    halt = gdir / "HALT"
    if halt.exists():
        add("Spend-governor", WARN, f"berkas HALT ADA di {halt} — governor sedang HALT, semua authorize ditolak sampai reset manual")
    elif db.exists():
        add("Spend-governor", OK, f"state ada ({db}, {db.stat().st_size} byte), tidak sedang halt")
    elif (HOME / "workspace" / "tools" / "spend-governor" / "governor.py").exists():
        add("Spend-governor", WARN, "tool terpasang tapi state belum pernah dibuat (belum ada governor.db) — belum ada runner yang memakainya")
    else:
        add("Spend-governor", WARN, "tool spend-governor tidak ditemukan")


# ---------------------------------------------------------------- jam sistem
def check_clock() -> None:
    now = datetime.now().astimezone()
    tz = now.tzname()
    off = now.strftime("%z")
    add("Jam sistem", OK, f"{now.strftime('%Y-%m-%d %H:%M:%S')} {tz} (UTC{off}) — semua deadline proyek dibaca dari jam ini")


def main() -> int:
    check_proxy("Local proxy A (18712)", 18712)
    check_proxy("Local proxy B (18711)", 18711)
    check_baw("primary", None)
    check_baw("secondary", {"BINANCE_BAW_DIR": str(HOME / ".baw-secondary")})
    check_disk()
    check_skill_integrity()
    check_governor()
    check_clock()

    if "--json" in sys.argv:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        print("SYSTEM DOCTOR — AkTa Control Stack health check (read-only)")
        print("=" * 72)
        for r in results:
            print(f"{ICON[r['status']]} [{r['status']:<4}] {r['check']:<28} {r['evidence']}")
        print("=" * 72)
        n_fail = sum(1 for r in results if r["status"] == FAIL)
        n_warn = sum(1 for r in results if r["status"] == WARN)
        n_ok = sum(1 for r in results if r["status"] == OK)
        print(f"Ringkasan: {n_ok} OK · {n_warn} WARN · {n_fail} FAIL")
        if n_fail:
            print("Ada FAIL — lihat baris ❌ di atas; doctor hanya membaca, perbaikan tetap jalur masing-masing (restart proxies via your own launcher; re-pair wallet sessions manually).")
    return 1 if any(r["status"] == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
