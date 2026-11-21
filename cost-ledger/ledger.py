#!/usr/bin/env python3
"""
tools/cost-ledger/ledger.py — Buku Kas Belanja API + On-Chain  (versi AkTa)

Satu tempat buat menjawab "kita udah bakar berapa, di proyek apa, lewat lane
apa": pengeluaran API berbayar/kuota (Exa, OpenRouter, CrowLLM, DashScope,
RPC premium, ...), gas on-chain (klaim/mint/swap — nilai native yang keluar
dari wallet), dan infra (langganan RPC, VPS, domain, ...). Buku ini adalah
pasangan spend-governor: governor menahan SEBELUM belanja, ledger mencatat
SESUDAH belanja. Lihat README.md untuk peta pasangan kerjanya.

Diadaptasi dari SUPERAGENT V10:
tools/cost_ledger.py v4.2 + pola dedup alert dari tools/alerts.py v4.0.
Mekanisme yang dipertahankan dari sumber: satu tabel SQLite untuk semua
jenis belanja, koneksi bisa di-inject (tes pakai :memory:), agregasi
dibaca dari baris mentah, nol dependensi eksternal.

Perbedaan yang disengaja dari sumber (detail di README):
- WAKTU DI-INJECT. record() mewajibkan argumen ts dari caller; modul ini
  tidak pernah memanggil time.time() untuk entri. (Sumber menebak sendiri
  — kelas bug deadline yang tidak kita warisi.)
- Dimensi proyek + lane/sumber eksplisit, kategori tetap api|gas|infra.
- Jumlah + mata uang: total TIDAK PERNAH mencampur mata uang berbeda.
- Alert = total harian melewati ambang operator, keluaran TEKS saja.
  Mesin alert sumber (fetcher harga DexScreener via httpx + notifier
  Telegram/Discord) TIDAK diwarisi: modul ini tanpa jaringan sama sekali.

Zero dependency eksternal (sqlite3, json, math, threading, datetime,
pathlib, os). Aman dipanggil multi-thread (check_same_thread=False + lock
internal, pola yang sama dengan sumbernya).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_DIR = Path(os.environ.get("COST_LEDGER_DIR", "~/.cost-ledger")).expanduser()
DEFAULT_DB = Path(os.environ.get("COST_LEDGER_DB", str(DEFAULT_DIR / "ledger.db"))).expanduser()

#: Kategori belanja yang sah. Selain ini record() menolak (fail-closed).
CATEGORIES = ("api", "gas", "infra")

#: Scope ambang alert global (seluruh proyek). Scope lain = nama proyek.
GLOBAL_SCOPE = "*"


def _day_of(ts: float, tz_offset_s: float = 0.0) -> str:
    """String hari 'YYYY-MM-DD' untuk timestamp epoch, digeser tz_offset_s.

    Hari selalu dihitung dari waktu yang di-inject + offset yang dipilih
    operator saat konstruksi (default 0 = hari kalender UTC; 7*3600 untuk
    pembukuan hari WIB). Fungsi ini murni — tidak membaca jam sistem.
    """
    return datetime.fromtimestamp(ts + tz_offset_s, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Entry:
    id: int
    ts: float
    project: str
    lane: str
    category: str
    amount: float
    currency: str
    note: str


@dataclass
class LedgerSummary:
    """Agregasi atas satu jendela [since, until). Semua total per mata uang."""
    since: Optional[float]
    until: Optional[float]
    entries: int
    totals_by_currency: dict = field(default_factory=dict)     # currency -> amount
    by_project: dict = field(default_factory=dict)             # project -> {currency -> amount}
    by_lane: dict = field(default_factory=dict)                # lane -> {currency -> amount}
    by_category: dict = field(default_factory=dict)            # category -> {currency -> amount}
    by_day: dict = field(default_factory=dict)                 # 'YYYY-MM-DD' -> {currency -> amount}

    def report(self) -> str:
        def _fmt(bucket: dict) -> str:
            parts = []
            for name in sorted(bucket):
                cur = ", ".join(f"{amt:,.4f} {c}" for c, amt in sorted(bucket[name].items()))
                parts.append(f"{name}: {cur}")
            return " | ".join(parts) or "—"

        tot = ", ".join(f"{amt:,.4f} {c}" for c, amt in sorted(self.totals_by_currency.items())) or "—"
        return (
            f"💸 cost ledger ({self.entries} entries)\n"
            f"   total berjalan: {tot}\n"
            f"   per proyek:     {_fmt(self.by_project)}\n"
            f"   per lane:       {_fmt(self.by_lane)}\n"
            f"   per kategori:   {_fmt(self.by_category)}\n"
            f"   per hari:       {_fmt(self.by_day)}"
        )


class CostLedger:
    """Buku kas belanja. Satu instance = satu berkas SQLite (atau koneksi inject).

    tz_offset_s hanya memengaruhi pengelompokan HARI (laporan per hari +
    alert harian), tidak mengubah ts yang tersimpan. Default 0 (UTC);
    set 7*3600 untuk hari WIB.
    """

    def __init__(self, db_path: Path = DEFAULT_DB, conn: Optional[sqlite3.Connection] = None,
                 tz_offset_s: float = 0.0):
        self._lock = threading.Lock()
        self.tz_offset_s = float(tz_offset_s)
        if conn is not None:
            self._db = conn
        else:
            db_path = Path(db_path).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS entries (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL NOT NULL, project TEXT NOT NULL, lane TEXT NOT NULL,
                   category TEXT NOT NULL, amount REAL NOT NULL,
                   currency TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
               )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS daily_thresholds (
                   scope TEXT NOT NULL, currency TEXT NOT NULL,
                   amount REAL NOT NULL,
                   PRIMARY KEY (scope, currency)
               )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS alert_state (
                   scope TEXT NOT NULL, currency TEXT NOT NULL, day TEXT NOT NULL,
                   fired_ts REAL NOT NULL, total_at_fire REAL NOT NULL,
                   threshold REAL NOT NULL,
                   PRIMARY KEY (scope, currency, day)
               )"""
        )
        self._db.commit()

    # ---- writer ----
    def record(self, *, ts: float, project: str, lane: str, category: str,
               amount: float, currency: str = "USD", note: str = "") -> int:
        """Catat satu pengeluaran. Mengembalikan id baris.

        ts WAJIB di-inject caller (epoch detik) — tidak ada default jam
        sistem, supaya buku ini tidak pernah menebak waktu. amount boleh
        negatif HANYA sebagai koreksi/refund atas entri sebelumnya (catat
        alasannya di note); total adalah jumlah aljabar semua entri.
        """
        ts = self._check_ts(ts)
        project = self._check_text(project, "project")
        lane = self._check_text(lane, "lane")
        if category not in CATEGORIES:
            raise ValueError(f"category tidak dikenal: {category!r}. Pilihan: {list(CATEGORIES)}")
        amount = self._check_amount(amount)
        currency = self._check_currency(currency)
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO entries (ts, project, lane, category, amount, currency, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, project, lane, category, amount, currency, str(note)),
            )
            self._db.commit()
            return int(cur.lastrowid)

    # ---- readers ----
    def entries(self, *, since: Optional[float] = None, until: Optional[float] = None,
                project: Optional[str] = None, lane: Optional[str] = None,
                category: Optional[str] = None) -> list[Entry]:
        """Baca entri mentah, urut waktu naik. Filter None = tanpa filter."""
        q = "SELECT id, ts, project, lane, category, amount, currency, note FROM entries WHERE 1=1"
        args: list = []
        if since is not None:
            q += " AND ts >= ?"
            args.append(float(since))
        if until is not None:
            q += " AND ts < ?"
            args.append(float(until))
        if project is not None:
            q += " AND project = ?"
            args.append(project)
        if lane is not None:
            q += " AND lane = ?"
            args.append(lane)
        if category is not None:
            if category not in CATEGORIES:
                raise ValueError(f"category tidak dikenal: {category!r}. Pilihan: {list(CATEGORIES)}")
            q += " AND category = ?"
            args.append(category)
        q += " ORDER BY ts ASC, id ASC"
        rows = self._db.execute(q, args).fetchall()
        return [Entry(*r) for r in rows]

    def summary(self, *, since: Optional[float] = None, until: Optional[float] = None,
                project: Optional[str] = None, lane: Optional[str] = None,
                category: Optional[str] = None) -> LedgerSummary:
        """Agregasi: total berjalan + per proyek + per lane + per kategori + per hari.

        Tanpa since/until = seluruh buku (total berjalan sejak entri
        pertama). Hari dihitung dengan tz_offset_s instance ini.
        """
        rows = self.entries(since=since, until=until, project=project,
                            lane=lane, category=category)
        s = LedgerSummary(since=since, until=until, entries=len(rows))

        def _add(bucket: dict, key: str, currency: str, amount: float) -> None:
            inner = bucket.setdefault(key, {})
            inner[currency] = inner.get(currency, 0.0) + amount

        for e in rows:
            s.totals_by_currency[e.currency] = s.totals_by_currency.get(e.currency, 0.0) + e.amount
            _add(s.by_project, e.project, e.currency, e.amount)
            _add(s.by_lane, e.lane, e.currency, e.amount)
            _add(s.by_category, e.category, e.currency, e.amount)
            _add(s.by_day, _day_of(e.ts, self.tz_offset_s), e.currency, e.amount)
        # rapikan float (hindari 0.30000000000000004 di laporan)
        def _round(bucket: dict) -> dict:
            return {k: {c: round(v, 6) for c, v in inner.items()} for k, inner in bucket.items()}

        s.totals_by_currency = {c: round(v, 6) for c, v in s.totals_by_currency.items()}
        s.by_project = _round(s.by_project)
        s.by_lane = _round(s.by_lane)
        s.by_category = _round(s.by_category)
        s.by_day = _round(s.by_day)
        return s

    def daily_total(self, day: str, *, project: Optional[str] = None,
                    currency: str = "USD") -> float:
        """Total satu hari ('YYYY-MM-DD', menurut tz_offset_s) dalam satu mata uang."""
        currency = self._check_currency(currency)
        total = 0.0
        for e in self.entries(project=project):
            if e.currency == currency and _day_of(e.ts, self.tz_offset_s) == day:
                total += e.amount
        return round(total, 6)

    # ---- alert ambang harian (keluaran teks saja; tidak dikirim ke mana pun) ----
    def set_daily_threshold(self, amount: float, *, currency: str = "USD",
                            scope: str = GLOBAL_SCOPE) -> None:
        """Setel ambang total harian operator. scope='*' = global (semua proyek
        digabung); scope lain = nama proyek (total proyek itu saja)."""
        amount = self._check_amount(amount)
        if amount <= 0:
            raise ValueError("ambang harian harus > 0")
        currency = self._check_currency(currency)
        scope = self._check_text(scope, "scope")
        with self._lock:
            self._db.execute(
                "INSERT INTO daily_thresholds (scope, currency, amount) VALUES (?,?,?)"
                " ON CONFLICT(scope, currency) DO UPDATE SET amount = excluded.amount",
                (scope, currency, amount),
            )
            self._db.commit()

    def daily_thresholds(self) -> list[tuple[str, str, float]]:
        """Daftar (scope, currency, amount) ambang yang terpasang."""
        rows = self._db.execute(
            "SELECT scope, currency, amount FROM daily_thresholds ORDER BY scope, currency"
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def remove_daily_threshold(self, *, currency: str = "USD", scope: str = GLOBAL_SCOPE) -> None:
        currency = self._check_currency(currency)
        with self._lock:
            self._db.execute(
                "DELETE FROM daily_thresholds WHERE scope = ? AND currency = ?",
                (scope, currency),
            )
            self._db.commit()

    def check_daily_alerts(self, *, now_ts: float) -> list[str]:
        """Periksa total hari ini (hari dari now_ts, menurut tz_offset_s)
        lawan semua ambang terpasang. Mengembalikan daftar PESAN TEKS alert
        yang BARU menyala pada pemeriksaan ini.

        Dedup persisten (pola cooldown alerts.py sumber, disederhanakan):
        satu (scope, mata uang) menyala MAKSIMAL SEKALI PER HARI — keadaan
        tersimpan di tabel alert_state, jadi pemeriksaan ulang di hari yang
        sama tidak mengulang alert yang sama, dan proses baru pun melihat
        riwayat yang sama. Hari baru = kunci baru = boleh menyala lagi.
        Pesan hanya dikembalikan sebagai teks; modul ini tidak mengirim
        apa pun ke mana pun — caller yang memutuskan menampilkannya.
        """
        now_ts = self._check_ts(now_ts)
        day = _day_of(now_ts, self.tz_offset_s)
        fired: list[str] = []
        for scope, currency, threshold in self.daily_thresholds():
            project = None if scope == GLOBAL_SCOPE else scope
            total = self.daily_total(day, project=project, currency=currency)
            if total <= threshold:
                continue
            with self._lock:
                already = self._db.execute(
                    "SELECT 1 FROM alert_state WHERE scope = ? AND currency = ? AND day = ?",
                    (scope, currency, day),
                ).fetchone()
                if already:
                    continue
                self._db.execute(
                    "INSERT INTO alert_state (scope, currency, day, fired_ts, total_at_fire, threshold)"
                    " VALUES (?,?,?,?,?,?)",
                    (scope, currency, day, now_ts, total, threshold),
                )
                self._db.commit()
            label = "SEMUA PROYEK" if scope == GLOBAL_SCOPE else f"proyek '{scope}'"
            fired.append(
                f"⚠️ COST ALERT [{day}] total harian {label} = {total:,.4f} {currency}"
                f" — melewati ambang {threshold:,.4f} {currency}"
            )
        return fired

    def alerts_fired(self, day: Optional[str] = None) -> list[dict]:
        """Riwayat alert yang sudah menyala (semua hari, atau satu hari)."""
        q = ("SELECT scope, currency, day, fired_ts, total_at_fire, threshold"
             " FROM alert_state")
        args: list = []
        if day is not None:
            q += " WHERE day = ?"
            args.append(day)
        q += " ORDER BY fired_ts ASC"
        return [
            {"scope": r[0], "currency": r[1], "day": r[2], "fired_ts": r[3],
             "total_at_fire": r[4], "threshold": r[5]}
            for r in self._db.execute(q, args).fetchall()
        ]

    # ---- export ----
    def export_jsonl(self, path) -> int:
        """Tulis semua entri sebagai JSON Lines (satu objek per baris, urut
        waktu). Mengembalikan jumlah baris. Untuk portabilitas/arsip —
        SQLite tetap sumber kebenaran."""
        rows = self.entries()
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for e in rows:
                f.write(json.dumps({
                    "id": e.id, "ts": e.ts, "day": _day_of(e.ts, self.tz_offset_s),
                    "project": e.project, "lane": e.lane, "category": e.category,
                    "amount": e.amount, "currency": e.currency, "note": e.note,
                }, ensure_ascii=False) + "\n")
        return len(rows)

    # ---- validasi ----
    @staticmethod
    def _check_ts(ts) -> float:
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            raise TypeError(f"ts harus angka epoch (di-inject caller), dapat: {type(ts).__name__}")
        ts = float(ts)
        if not math.isfinite(ts) or ts < 0:
            raise ValueError(f"ts tidak sah: {ts!r}")
        return ts

    @staticmethod
    def _check_amount(amount) -> float:
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise TypeError(f"amount harus angka, dapat: {type(amount).__name__}")
        amount = float(amount)
        if not math.isfinite(amount):
            raise ValueError(f"amount harus finite (bukan NaN/inf): {amount!r}")
        return amount

    @staticmethod
    def _check_text(value, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} harus string tidak kosong")
        return value.strip()

    @staticmethod
    def _check_currency(currency) -> str:
        if not isinstance(currency, str) or not currency.strip():
            raise ValueError("currency harus string tidak kosong (mis. 'USD', 'BNB', 'ETH')")
        return currency.strip().upper()

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "CostLedger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# CLI tipis — caller di sini adalah operator; CLI meng-inject waktu secara
# eksplisit (library-nya sendiri tidak pernah menebak waktu).
# ---------------------------------------------------------------------------
def _parse_ts(raw: str) -> float:
    """Terima epoch detik atau ISO-8601 (mis. 2026-11-21T20:00:00+07:00)."""
    try:
        return float(raw)
    except ValueError:
        pass
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def main(argv: Optional[list] = None) -> int:
    import argparse
    import time

    ap = argparse.ArgumentParser(prog="ledger.py", description="Buku kas belanja API + on-chain (versi AkTa)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path SQLite (default: %(default)s)")
    ap.add_argument("--tz-offset-s", type=float, default=0.0,
                    help="offset detik untuk batas hari (25200 = WIB). Default 0 (UTC).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="catat satu pengeluaran")
    p_add.add_argument("--ts", default=None,
                       help="epoch detik atau ISO-8601. Default: jam sistem SAAT INI, di-inject CLI secara eksplisit.")
    p_add.add_argument("--project", required=True)
    p_add.add_argument("--lane", required=True)
    p_add.add_argument("--category", required=True, choices=list(CATEGORIES))
    p_add.add_argument("--amount", required=True, type=float)
    p_add.add_argument("--currency", default="USD")
    p_add.add_argument("--note", default="")

    sub.add_parser("report", help="laporan total berjalan + per proyek/lane/kategori/hari")

    p_thr = sub.add_parser("threshold", help="setel ambang total harian")
    p_thr.add_argument("--amount", required=True, type=float)
    p_thr.add_argument("--currency", default="USD")
    p_thr.add_argument("--scope", default=GLOBAL_SCOPE, help="'*' = global, atau nama proyek")

    p_chk = sub.add_parser("check-alerts", help="periksa ambang harian; cetak alert yang baru menyala")
    p_chk.add_argument("--ts", default=None, help="epoch/ISO untuk 'sekarang'. Default: jam sistem, di-inject CLI.")

    p_exp = sub.add_parser("export", help="ekspor semua entri ke JSONL")
    p_exp.add_argument("--out", required=True)

    args = ap.parse_args(argv)
    led = CostLedger(db_path=Path(args.db), tz_offset_s=args.tz_offset_s)
    try:
        if args.cmd == "add":
            ts = _parse_ts(args.ts) if args.ts is not None else time.time()
            rid = led.record(ts=ts, project=args.project, lane=args.lane,
                             category=args.category, amount=args.amount,
                             currency=args.currency, note=args.note)
            print(f"recorded id={rid}")
        elif args.cmd == "report":
            print(led.summary().report())
        elif args.cmd == "threshold":
            led.set_daily_threshold(args.amount, currency=args.currency, scope=args.scope)
            print(f"threshold set: scope={args.scope} {args.amount} {args.currency.upper()}")
        elif args.cmd == "check-alerts":
            now_ts = _parse_ts(args.ts) if args.ts is not None else time.time()
            fired = led.check_daily_alerts(now_ts=now_ts)
            for msg in fired:
                print(msg)
            if not fired:
                print("(tidak ada alert baru)")
        elif args.cmd == "export":
            n = led.export_jsonl(args.out)
            print(f"exported {n} entries -> {args.out}")
    finally:
        led.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
