"""
spend-governor/governor.py — Spend Governor / Circuit Breaker (versi AkTa)

Rem belanja TERPROGRAM untuk runner/watcher kita yang menyentuh on-chain:
gerbang di antara simulasi dan broadcast. authorize(intent) dipanggil SEBELUM
broadcast dan mengembalikan Decision(allow|block|halt); record(intent, tx_hash)
WAJIB dipanggil SETELAH broadcast sukses supaya cap akurat — keduanya satu
langkah tak terpisah (lihat "KONTRAK INTEGRASI" di bawah).

Diadaptasi dari SUPERAGENT V10 — skills/hermes/scripts/governor.py v4.0
(paket tooling agen pihak ketiga). Mekanisme inti dipertahankan
utuh dari sumbernya:

- cap per-tx, cap harian per wallet (jendela geser 86.400 detik, BUKAN hari
  kalender), cap sesi per session_id (jendela 10**9 detik);
- max_slippage_pct (default 5.0) → block;
- max_gas_multiple (default 4.0) lawan baseline rata-rata gas_price_wei dari
  20 tx terakhir di chain yang sama → bukan block, tapi TRIP kill-switch;
- max_tx_per_min (default 12) dari record 60 detik terakhir → TRIP kill-switch
  (perlindungan runaway loop);
- require_simulation (default True) → intent yang simulated_ok-nya bukan True
  diblokir;
- kill-switch dua jalur: flag 'halt' di DB ATAU berkas HALT di disk. Sekali
  trip, SEMUA authorize() = halt sampai reset_killswitch() dipanggil MANUAL
  oleh operator sesudah investigasi. Tidak ada auto-reset, tidak ada override.
- State persisten di SQLite (stdlib murni: sqlite3, json, time, pathlib) →
  tahan restart proses/mesin.

PERBAIKAN atas cacat yang terbukti di sumbernya (studi kita: study-4
§4.2 butir 2 & 12, §1.8) — keempatnya FAIL-CLOSED:

1. CAP TIDAK BOLEH MATI SEMUA. Di sumbernya ketiga cap USD default None
   sehingga governor bisa jalan tanpa satu pun cap nilai aktif. Di sini
   konstruktor MENOLAK jalan (ValueError) bila max_tx_usd, daily_cap_usd,
   dan session_cap_usd ketiganya None. Minimal satu cap nilai harus disetel.
2. SESSION_ID EKSPLISIT WAJIB. Di sumbernya session_id default = timestamp
   proses (atau env), dan engine yang meng-instansiasi ulang governor tiap
   panggilan membuat cap sesi ter-reset tiap proses baru. Di sini session_id
   adalah argumen wajib tanpa default dan tanpa fallback env/timestamp;
   kosong/None → ValueError. Satu runner = satu session_id yang stabil dan
   disengaja, ditulis eksplisit di kode pemanggil.
3. record() ADALAH BAGIAN API YANG WAJIB, bukan saran. Di sumbernya record()
   "diserahkan ke caller" dan jalur yang lupa memanggilnya membuat cap tidak
   pernah bertambah (rem tidak bekerja diam-diam). Di sini kontraknya
   dinyatakan eksplisit di docstring authorize()/record() dan README, dan
   governor menghitung `unrecorded_authorizations` (authorize yang allow
   tapi belum di-record) yang tampil di report() supaya kelalaian itu
   TERLIHAT, bukan senyap.
4. NILAI USD TIDAK BOLEH None UNTUK INTENT BERNILAI. Di sumbernya
   usd_value=None melewatkan SEMUA cap USD (dokumen sumbernya sendiri
   mengakuinya). Di sini intent yang value_bearing=True (default) dengan
   usd_value=None → BLOCK. Caller wajib menghitung nilai USD intent-nya
   (untuk konteks gas-only kita: estimasi biaya gas dalam USD). Intent yang
   sungguh tidak memindahkan nilai boleh men-set value_bearing=False secara
   eksplisit — itu keputusan yang terlihat di kode, bukan kelalaian senyap.

KONTRAK INTEGRASI (urutan yang benar, dari dokumen sumbernya):
    safety check → simulate → governor.authorize → broadcast → governor.record
Governor TIDAK menggantikan konfirmasi per-tx user, preview discipline, atau
verifikasi receipt sesudah broadcast. Dia pencatat & rem tambahan. Receipt
tetap diverifikasi caller (status 0x1 + event + state) seperti biasa.

Nilai USD dihitung CALLER (price oracle / estimasi gas caller sendiri) —
governor tidak menebak harga.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Literal

Verdict = Literal["allow", "block", "halt"]

# Path default versi kita. DB dan berkas HALT sengaja satu folder supaya
# operator cukup `touch <folder>/HALT` untuk halt darurat dan state-nya
# terlihat sekilas. Override lewat env atau argumen konstruktor.
_DEFAULT_DIR = Path(os.environ.get("SPEND_GOVERNOR_DIR", "~/.spend-governor")).expanduser()
DEFAULT_DB = Path(os.environ.get("SPEND_GOVERNOR_DB", str(_DEFAULT_DIR / "governor.db"))).expanduser()
DEFAULT_KILLSWITCH_FILE = Path(
    os.environ.get("SPEND_GOVERNOR_HALT", str(_DEFAULT_DIR / "HALT"))).expanduser()

DAILY_WINDOW_S = 86400          # jendela geser cap harian (detik) — dari sumber
SESSION_WINDOW_S = 10 ** 9      # jendela cap sesi — dari sumber (praktis: semua record sesi itu)
GAS_BASELINE_N = 20             # jumlah tx terakhir per chain untuk baseline gas — dari sumber
RATE_WINDOW_S = 60              # jendela hitung rate tx/menit — dari sumber


# ───────────────────────────── config ─────────────────────────────
@dataclass
class GovernorLimits:
    """Batas-batas governor.

    Berbeda dari sumbernya: ketiga cap USD boleh None SATU-SATU, tapi TIDAK
    boleh None ketiganya sekaligus — SpendGovernor menolak konstruksi seperti
    itu (fail-closed, perbaikan #1). Nilai cap yang disetel harus > 0.
    """
    max_tx_usd: Optional[float] = None          # cap nilai 1 tx
    daily_cap_usd: Optional[float] = None        # cap akumulasi 24 jam geser (per wallet)
    session_cap_usd: Optional[float] = None      # cap akumulasi 1 sesi (semua wallet)
    max_slippage_pct: float = 5.0                # tolak swap dengan slippage di atas ini
    max_gas_multiple: float = 4.0                # gas > N x baseline rolling → HALT (trip)
    max_tx_per_min: int = 12                     # rate limit (anti runaway loop) → HALT (trip)
    require_simulation: bool = True              # tolak kalau intent belum disimulasi

    @classmethod
    def from_env(cls) -> "GovernorLimits":
        """Baca limit dari env SPEND_GOVERNOR_*.

        PERHATIAN: from_env() bisa menghasilkan ketiga cap USD = None (bila
        env-nya tidak disetel). Itu BUKAN konfigurasi yang bisa jalan —
        SpendGovernor akan menolaknya dengan ValueError. Setel minimal satu
        dari SPEND_GOVERNOR_MAX_TX_USD / SPEND_GOVERNOR_DAILY_CAP_USD /
        SPEND_GOVERNOR_SESSION_CAP_USD.
        """
        def f(k):
            v = os.environ.get(k)
            return float(v) if v not in (None, "") else None
        return cls(
            max_tx_usd=f("SPEND_GOVERNOR_MAX_TX_USD"),
            daily_cap_usd=f("SPEND_GOVERNOR_DAILY_CAP_USD"),
            session_cap_usd=f("SPEND_GOVERNOR_SESSION_CAP_USD"),
            max_slippage_pct=f("SPEND_GOVERNOR_MAX_SLIPPAGE_PCT") or 5.0,
            max_gas_multiple=f("SPEND_GOVERNOR_MAX_GAS_MULTIPLE") or 4.0,
            max_tx_per_min=int(f("SPEND_GOVERNOR_MAX_TX_PER_MIN") or 12),
            require_simulation=os.environ.get("SPEND_GOVERNOR_REQUIRE_SIM", "1") != "0",
        )


@dataclass
class TxIntent:
    """Apa yang mau dieksekusi. Diisi caller sebelum authorize().

    value_bearing: True (default) = intent ini memindahkan nilai (token,
    native, atau membayar gas untuk aksi bernilai) sehingga usd_value WAJIB
    diisi; None → BLOCK (perbaikan #4). Set False HANYA untuk intent yang
    sungguh tidak memindahkan nilai apa pun, sebagai keputusan eksplisit.
    """
    wallet: str
    chain_id: int
    action: str                       # "swap" | "claim" | "mint" | "send" | ...
    usd_value: Optional[float] = None
    native_value: float = 0.0
    slippage_pct: Optional[float] = None
    gas_price_wei: Optional[int] = None
    simulated_ok: Optional[bool] = None
    recipient: Optional[str] = None
    note: str = ""
    value_bearing: bool = True


@dataclass
class Decision:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    remaining_daily_usd: Optional[float] = None
    remaining_session_usd: Optional[float] = None

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"

    def summary(self) -> str:
        icon = {"allow": "✅", "block": "⛔", "halt": "🛑"}[self.verdict]
        body = "; ".join(self.reasons) if self.reasons else "ok"
        return f"{icon} {self.verdict.upper()}: {body}"


# ─────────────────────────── governor ───────────────────────────
class SpendGovernor:
    def __init__(self, limits: Optional[GovernorLimits] = None,
                 db_path: Path = DEFAULT_DB, *,
                 session_id: str,
                 killswitch_file: Path = DEFAULT_KILLSWITCH_FILE,
                 conn: Optional[sqlite3.Connection] = None):
        """Bangun governor.

        session_id WAJIB eksplisit (perbaikan #2): string non-kosong yang
        dipilih pemanggil dan STABIL untuk satu runner/sesi logis — mis.
        "pxio-claim-watch-rizrekt" atau "polyester-session-84". Tidak ada
        default, tidak ada fallback env, tidak ada timestamp proses: cap sesi
        hanya bermakna bila identitas sesinya disengaja.

        conn: opsional koneksi DBAPI yang di-inject (mis. SQLite di path
        bersama) bila beberapa proses harus berbagi cap yang sama. Default =
        SQLite lokal di db_path. Ingat: SQLite lokal hanya nge-cap per-mesin;
        cap lintas proses di mesin yang sama tercapai karena db_path sama.
        """
        self.limits = limits or GovernorLimits.from_env()
        self._validate_limits(self.limits)
        if session_id is None or not str(session_id).strip():
            raise ValueError(
                "session_id eksplisit WAJIB diisi (perbaikan #2): pilih string "
                "yang stabil untuk runner/sesi ini. Default timestamp proses "
                "dari versi sumber SENGAJA dihapus — cap sesi yang ter-reset "
                "tiap proses baru adalah rem yang tidak bekerja.")
        self.session_id = str(session_id)
        self.db_path = Path(db_path)
        self.killswitch_file = Path(killswitch_file)
        if conn is not None:
            self._db = conn
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS spend (
                ts REAL, session_id TEXT, wallet TEXT, chain_id INTEGER,
                action TEXT, usd_value REAL, native_value REAL,
                gas_price_wei INTEGER, tx_hash TEXT
            )""")
        self._db.execute("CREATE TABLE IF NOT EXISTS flags (k TEXT PRIMARY KEY, v TEXT)")
        self._db.commit()
        # Penghitung authorize-allow yang belum di-record (perbaikan #3).
        # In-memory per proses: tujuannya membuat kelalaian record TERLIHAT
        # di report() selama proses berjalan, bukan audit lintas restart.
        self._unrecorded = 0

    @staticmethod
    def _validate_limits(L: GovernorLimits) -> None:
        """Fail-closed: tolak konfigurasi yang remnya mati atau tak masuk akal."""
        if L.max_tx_usd is None and L.daily_cap_usd is None and L.session_cap_usd is None:
            raise ValueError(
                "Konfigurasi ditolak (fail-closed, perbaikan #1): max_tx_usd, "
                "daily_cap_usd, dan session_cap_usd TIDAK BOLEH None ketiganya. "
                "Di versi sumbernya keadaan ini adalah default dan membuat semua "
                "cap nilai mati senyap. Setel minimal satu cap nilai (untuk "
                "konteks gas-only kita biasanya daily_cap_usd dan/atau "
                "session_cap_usd yang kecil).")
        for name in ("max_tx_usd", "daily_cap_usd", "session_cap_usd"):
            v = getattr(L, name)
            if v is not None and v <= 0:
                raise ValueError(f"{name} harus > 0 bila disetel (terima {v!r}).")
        if L.max_slippage_pct < 0:
            raise ValueError(f"max_slippage_pct tidak boleh negatif (terima {L.max_slippage_pct!r}).")
        if L.max_gas_multiple <= 0:
            raise ValueError(f"max_gas_multiple harus > 0 (terima {L.max_gas_multiple!r}).")
        if L.max_tx_per_min < 1:
            raise ValueError(f"max_tx_per_min harus >= 1 (terima {L.max_tx_per_min!r}).")

    # ---- kill-switch ----
    def is_halted(self) -> Optional[str]:
        """Kembalikan alasan halt, atau None bila tidak halt.

        Dua jalur (dari sumber): berkas HALT di disk (isi berkas = alasan;
        berkas kosong = alasan generik) ATAU flag 'halt' di DB (dipasang
        trip() otomatis atau manual).
        """
        if self.killswitch_file.exists():
            return self.killswitch_file.read_text().strip() or "kill-switch file present"
        row = self._db.execute("SELECT v FROM flags WHERE k='halt'").fetchone()
        return row[0] if row else None

    def trip(self, reason: str):
        """Pasang kill-switch (flag DB) dengan alasan + cap waktu.

        Dipanggil otomatis oleh authorize() pada anomali gas/rate, dan boleh
        dipanggil manual oleh kode operator. Sekali trip, semua authorize()
        berikutnya = halt sampai reset_killswitch() manual.
        """
        self._db.execute("INSERT OR REPLACE INTO flags(k, v) VALUES('halt', ?)",
                         (f"{reason} @ {time.strftime('%Y-%m-%d %H:%M:%S')}",))
        self._db.commit()

    def reset_killswitch(self):
        """Operator-only. Panggil MANUAL hanya setelah investigasi selesai.

        Menghapus flag halt di DB dan berkas HALT di disk. Sengaja berupa
        pemanggilan fungsi eksplisit (bukan env, bukan timeout) supaya reset
        selalu perbuatan sadar operator — friksi ini disengaja, persis
        alasan di dokumen sumbernya.
        """
        self._db.execute("DELETE FROM flags WHERE k='halt'")
        self._db.commit()
        try:
            self.killswitch_file.unlink()
        except FileNotFoundError:
            pass

    # ---- accounting ----
    def _spent_since(self, seconds: float, wallet: Optional[str] = None,
                     session: Optional[str] = None) -> float:
        q = "SELECT COALESCE(SUM(usd_value),0) FROM spend WHERE ts >= ?"
        args: list = [time.time() - seconds]
        if wallet:
            q += " AND wallet = ?"; args.append(wallet)
        if session:
            q += " AND session_id = ?"; args.append(session)
        return float(self._db.execute(q, args).fetchone()[0] or 0.0)

    def _gas_baseline(self, chain_id: int) -> Optional[float]:
        """Rata-rata gas_price_wei dari 20 tx terakhir di chain ini.

        None bila belum ada riwayat gas di chain itu — artinya tx pertama
        di chain baru tidak punya baseline dan cek gas-spike tidak berlaku
        untuknya (perilaku sumber, didokumentasikan apa adanya di README).
        """
        row = self._db.execute(
            "SELECT AVG(gas_price_wei) FROM (SELECT gas_price_wei FROM spend "
            "WHERE chain_id=? AND gas_price_wei IS NOT NULL ORDER BY ts DESC LIMIT ?)",
            (chain_id, GAS_BASELINE_N)).fetchone()
        return float(row[0]) if row and row[0] else None

    def _tx_count_last_minute(self) -> int:
        return int(self._db.execute(
            "SELECT COUNT(*) FROM spend WHERE ts >= ?", (time.time() - RATE_WINDOW_S,)).fetchone()[0])

    # ---- the gate ----
    def authorize(self, intent: TxIntent) -> Decision:
        """Putuskan boleh/tidak intent ini di-broadcast. SEBELUM broadcast.

        Semantik verdict (dari sumber):
        - "allow": lolos semua cek. Caller boleh broadcast, lalu WAJIB
          memanggil record(intent, tx_hash) sesudah broadcast sukses —
          authorize dan record adalah SATU langkah tak terpisah (perbaikan
          #3). Allow yang tidak di-record membuat cap tidak bertambah dan
          rem kehilangan giginya; governor menghitung kelalaian itu di
          report()["unrecorded_authorizations"].
        - "block": tolak intent INI saja; governor tetap jalan. Sebabnya
          spesifik per intent (belum simulasi, slippage, cap, usd None).
        - "halt": kill-switch terpasang (sudah trip sebelumnya, atau trip
          barusan karena anomali gas/rate). SEMUA authorize berikutnya juga
          halt sampai operator reset_killswitch() manual.
        """
        halt = self.is_halted()
        if halt:
            return Decision("halt", [f"governor HALTED: {halt}. Reset manual diperlukan."])

        L, reasons = self.limits, []
        block = False

        if L.require_simulation and intent.simulated_ok is not True:
            block = True
            reasons.append("intent belum lolos simulasi (require_simulation=ON)")

        if intent.slippage_pct is not None and intent.slippage_pct > L.max_slippage_pct:
            block = True
            reasons.append(f"slippage {intent.slippage_pct:.2f}% > batas {L.max_slippage_pct:.2f}%")

        # Perbaikan #4 (fail-closed): intent bernilai tanpa nilai USD tidak
        # boleh melewatkan cap. Di sumbernya keadaan ini men-skip semua cap.
        usd_missing = intent.usd_value is None
        if usd_missing and intent.value_bearing:
            block = True
            reasons.append(
                "usd_value=None untuk intent bernilai (value_bearing=True) — "
                "caller WAJIB mengisi nilai USD intent (fail-closed). "
                "Set value_bearing=False secara eksplisit hanya bila intent "
                "ini sungguh tidak memindahkan nilai.")

        if L.max_tx_usd is not None and intent.usd_value is not None \
                and intent.usd_value > L.max_tx_usd:
            block = True
            reasons.append(f"nilai tx ${intent.usd_value:,.2f} > cap per-tx ${L.max_tx_usd:,.2f}")

        # gas spike → tripping anomaly, bukan sekadar block (dari sumber)
        if intent.gas_price_wei is not None:
            base = self._gas_baseline(intent.chain_id)
            if base and intent.gas_price_wei > base * L.max_gas_multiple:
                self.trip(f"gas spike chain {intent.chain_id}: "
                          f"{intent.gas_price_wei} > {L.max_gas_multiple}x baseline {base:.0f}")
                return Decision("halt", ["gas spike abnormal → kill-switch tripped"])

        # rate limit → tripping (runaway loop protection, dari sumber)
        if self._tx_count_last_minute() >= L.max_tx_per_min:
            self.trip(f"rate limit: >{L.max_tx_per_min} tx/min")
            return Decision("halt", [f"rate >{L.max_tx_per_min} tx/min → runaway dicurigai, kill-switch tripped"])

        rem_daily = rem_session = None
        if intent.usd_value is not None:
            if L.daily_cap_usd is not None:
                spent = self._spent_since(DAILY_WINDOW_S, wallet=intent.wallet)
                rem_daily = L.daily_cap_usd - spent
                if spent + intent.usd_value > L.daily_cap_usd:
                    block = True
                    reasons.append(f"cap harian wallet kelewat: terpakai ${spent:,.2f} "
                                   f"+ ${intent.usd_value:,.2f} > ${L.daily_cap_usd:,.2f}")
            if L.session_cap_usd is not None:
                spent_s = self._spent_since(SESSION_WINDOW_S, session=self.session_id)
                rem_session = L.session_cap_usd - spent_s
                if spent_s + intent.usd_value > L.session_cap_usd:
                    block = True
                    reasons.append(f"cap sesi kelewat: terpakai ${spent_s:,.2f} "
                                   f"+ ${intent.usd_value:,.2f} > ${L.session_cap_usd:,.2f}")

        if block:
            return Decision("block", reasons, rem_daily, rem_session)
        self._unrecorded += 1
        return Decision("allow", [], rem_daily, rem_session)

    def record(self, intent: TxIntent, tx_hash: str):
        """Catat tx yang SUDAH ter-broadcast sukses. WAJIB, bukan opsional.

        Ini separuh kedua dari kontrak authorize(): authorize() tanpa
        record() = cap tidak bertambah = rem tidak bekerja. Panggil tepat
        sesudah broadcast mengembalikan tx_hash, dengan intent yang sama
        persis dengan yang di-authorize. Verifikasi receipt (status 0x1,
        event, state akhir) tetap kewajiban caller sesudah ini — record()
        mencatat bahwa tx terkirim, bukan bahwa tx berhasil; kegagalan
        receipt ditangani alur caller (dan alasan untuk trip() manual bila
        polanya mencurigakan).
        """
        self._db.execute(
            "INSERT INTO spend VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), self.session_id, intent.wallet, intent.chain_id, intent.action,
             intent.usd_value, intent.native_value, intent.gas_price_wei, tx_hash))
        self._db.commit()
        if self._unrecorded > 0:
            self._unrecorded -= 1

    def report(self) -> dict:
        return {
            "session_id": self.session_id,
            "halted": self.is_halted(),
            "session_spent_usd": round(self._spent_since(SESSION_WINDOW_S, session=self.session_id), 2),
            "unrecorded_authorizations": self._unrecorded,
            "limits": asdict(self.limits),
        }

    def close(self) -> None:
        """Tutup koneksi DB. Aman dipanggil berkali-kali."""
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "SpendGovernor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ─────────────────────────── usage example ───────────────────────────
if __name__ == "__main__":
    # Contoh konteks kita: runner klaim gas-only. usd_value per intent =
    # estimasi biaya gas dalam USD yang dihitung caller.
    gov = SpendGovernor(
        GovernorLimits(max_tx_usd=0.50, daily_cap_usd=5.0,
                       session_cap_usd=2.0, require_simulation=True),
        session_id="demo-gas-only")
    intent = TxIntent(wallet="0x0000000000000000000000000000000000000001",
                      chain_id=56, action="claim",
                      usd_value=0.03, slippage_pct=None, simulated_ok=True)
    d = gov.authorize(intent)
    print(d.summary())
    if d.allowed:
        gov.record(intent, "0xdeadbeef")   # WAJIB: tepat setelah broadcast sukses
    print(json.dumps(gov.report(), indent=2, default=str))
    gov.close()
