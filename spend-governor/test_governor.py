"""Tes invarian untuk spend-governor/governor.py (versi AkTa).

Stdlib murni (unittest). Jalankan:
    python3 -m unittest test_governor -v
atau:
    python3 test_governor.py

Yang diuji = INVARIAN KESELAMATAN, bukan jalur bahagia:
- konfigurasi fail-closed ditolak (semua cap None; session_id hilang);
- intent bernilai dengan usd_value=None di-block;
- cap per-tx / harian / sesi benar-benar menahan akumulasi dari record();
- anomali gas dan rate me-TRIP kill-switch (halt persisten, bukan block);
- berkas HALT menghentikan semua authorize;
- reset HANYA lewat reset_killswitch() manual;
- penghitung unrecorded_authorizations membuat record() yang lupa terlihat.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import governor as G


def _gov(tmp, session_id="t-sess", **limit_kw):
    """Governor terisolasi di folder tmp: DB + berkas HALT sendiri."""
    limits = G.GovernorLimits(**limit_kw)
    return G.SpendGovernor(
        limits=limits,
        db_path=Path(tmp) / "gov.db",
        session_id=session_id,
        killswitch_file=Path(tmp) / "HALT")


def _intent(**kw):
    base = dict(wallet="0xw1", chain_id=56, action="claim",
                usd_value=1.0, simulated_ok=True)
    base.update(kw)
    return G.TxIntent(**base)


class GovTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self._govs = []

    def tearDown(self):
        for g in self._govs:
            g.close()
        self._tmp.cleanup()

    def gov(self, **kw):
        g = _gov(self.tmp, **kw)
        self._govs.append(g)
        return g


# ─────────────────── fail-closed: konfigurasi (perbaikan #1 & #2) ───────────────────
class TestFailClosedConfig(GovTestCase):
    def test_all_caps_none_rejected(self):
        """Ketiga cap USD None = konstruktor MENOLAK (di sumbernya ini default yang lolos)."""
        with self.assertRaises(ValueError):
            _gov(self.tmp)  # GovernorLimits() polos: semua cap None

    def test_from_env_without_caps_rejected(self):
        """from_env() tanpa env cap juga harus ditolak saat konstruksi."""
        for k in list(os.environ):
            if k.startswith("SPEND_GOVERNOR_"):
                os.environ.pop(k, None)
        with self.assertRaises(ValueError):
            G.SpendGovernor(limits=G.GovernorLimits.from_env(),
                            db_path=Path(self.tmp) / "e.db",
                            session_id="t-env",
                            killswitch_file=Path(self.tmp) / "HALT")

    def test_single_cap_is_enough(self):
        """Satu cap saja cukup untuk konstruksi sah."""
        g = self.gov(daily_cap_usd=10.0, require_simulation=False)
        self.assertEqual(g.authorize(_intent(simulated_ok=None)).verdict, "allow")

    def test_nonpositive_cap_rejected(self):
        with self.assertRaises(ValueError):
            _gov(self.tmp, daily_cap_usd=0)
        with self.assertRaises(ValueError):
            _gov(self.tmp, max_tx_usd=-5.0)

    def test_bad_anomaly_params_rejected(self):
        with self.assertRaises(ValueError):
            _gov(self.tmp, daily_cap_usd=10.0, max_tx_per_min=0)
        with self.assertRaises(ValueError):
            _gov(self.tmp, daily_cap_usd=10.0, max_gas_multiple=0)

    def test_session_id_required(self):
        """session_id hilang/None/kosong = ValueError. Tidak ada default timestamp."""
        lim = G.GovernorLimits(daily_cap_usd=10.0)
        for bad in (None, "", "   "):
            with self.assertRaises(ValueError):
                G.SpendGovernor(limits=lim, db_path=Path(self.tmp) / "s.db",
                                session_id=bad,
                                killswitch_file=Path(self.tmp) / "HALT")

    def test_session_id_is_keyword_required(self):
        """Tanpa argumen session_id sama sekali = TypeError (argumen wajib)."""
        lim = G.GovernorLimits(daily_cap_usd=10.0)
        with self.assertRaises(TypeError):
            G.SpendGovernor(limits=lim, db_path=Path(self.tmp) / "s2.db",
                            killswitch_file=Path(self.tmp) / "HALT")


# ─────────────────── usd None untuk intent bernilai (perbaikan #4) ───────────────────
class TestUsdNoneFailClosed(GovTestCase):
    def test_value_bearing_usd_none_blocks(self):
        g = self.gov(daily_cap_usd=100.0, require_simulation=False)
        d = g.authorize(_intent(usd_value=None, simulated_ok=None))
        self.assertEqual(d.verdict, "block")
        self.assertTrue(any("usd_value=None" in r for r in d.reasons))

    def test_value_bearing_false_allows_none(self):
        """value_bearing=False eksplisit = keputusan terlihat; USD section dilewati."""
        g = self.gov(daily_cap_usd=100.0, require_simulation=False)
        d = g.authorize(_intent(usd_value=None, simulated_ok=None, value_bearing=False))
        self.assertEqual(d.verdict, "allow")

    def test_native_value_intent_still_needs_usd(self):
        """Intent dengan native_value > 0 dan usd None tetap block (default value_bearing)."""
        g = self.gov(daily_cap_usd=100.0, require_simulation=False)
        d = g.authorize(_intent(usd_value=None, native_value=0.05, simulated_ok=None))
        self.assertEqual(d.verdict, "block")


# ─────────────────── cek block dasar ───────────────────
class TestBasicBlocks(GovTestCase):
    def test_allow_clean_intent(self):
        g = self.gov(max_tx_usd=500, daily_cap_usd=1000.0)
        d = g.authorize(_intent(usd_value=100.0, slippage_pct=1.0))
        self.assertEqual(d.verdict, "allow")
        self.assertTrue(d.allowed)

    def test_block_when_not_simulated(self):
        g = self.gov(daily_cap_usd=100.0, require_simulation=True)
        d = g.authorize(_intent(simulated_ok=None))
        self.assertEqual(d.verdict, "block")
        self.assertTrue(any("simulasi" in r for r in d.reasons))

    def test_simulated_false_blocks(self):
        g = self.gov(daily_cap_usd=100.0, require_simulation=True)
        self.assertEqual(g.authorize(_intent(simulated_ok=False)).verdict, "block")

    def test_block_over_per_tx_cap(self):
        g = self.gov(max_tx_usd=100, daily_cap_usd=100000.0, require_simulation=False)
        d = g.authorize(_intent(usd_value=1000.0, simulated_ok=None))
        self.assertEqual(d.verdict, "block")

    def test_per_tx_cap_boundary_allows_equal(self):
        g = self.gov(max_tx_usd=100, daily_cap_usd=100000.0, require_simulation=False)
        d = g.authorize(_intent(usd_value=100.0, simulated_ok=None))
        self.assertEqual(d.verdict, "allow")

    def test_block_high_slippage(self):
        g = self.gov(daily_cap_usd=100.0, max_slippage_pct=5.0, require_simulation=False)
        d = g.authorize(_intent(slippage_pct=42.0, simulated_ok=None))
        self.assertEqual(d.verdict, "block")


# ─────────────────── cap akumulasi dari record() ───────────────────
class TestCapsAccumulate(GovTestCase):
    def test_daily_cap_accumulates(self):
        g = self.gov(daily_cap_usd=150, require_simulation=False)
        i1 = _intent(usd_value=100.0, simulated_ok=None)
        self.assertEqual(g.authorize(i1).verdict, "allow")
        g.record(i1, "0xhash1")
        i2 = _intent(usd_value=100.0, simulated_ok=None)
        d = g.authorize(i2)
        self.assertEqual(d.verdict, "block")          # 100 + 100 > 150
        self.assertEqual(d.remaining_daily_usd, 50.0)  # sisa dilaporkan apa adanya

    def test_daily_cap_is_per_wallet(self):
        g = self.gov(daily_cap_usd=150, require_simulation=False)
        g.record(_intent(usd_value=100.0, simulated_ok=None), "0xh")
        d = g.authorize(_intent(wallet="0xw2", usd_value=100.0, simulated_ok=None))
        self.assertEqual(d.verdict, "allow")

    def test_session_cap_spans_wallets(self):
        """Cap sesi menghitung SEMUA wallet dalam session_id yang sama."""
        g = self.gov(session_cap_usd=150, require_simulation=False)
        g.record(_intent(wallet="0xw1", usd_value=100.0, simulated_ok=None), "0xh1")
        d = g.authorize(_intent(wallet="0xw2", usd_value=100.0, simulated_ok=None))
        self.assertEqual(d.verdict, "block")
        self.assertEqual(d.remaining_session_usd, 50.0)

    def test_session_cap_isolated_by_session_id(self):
        """Sesi lain di DB yang sama tidak ikut terhitung cap sesi ini."""
        g1 = self.gov(session_cap_usd=150, require_simulation=False)
        g1.record(_intent(usd_value=140.0, simulated_ok=None), "0xh1")
        g2 = _gov(self.tmp, session_id="t-sess-lain", session_cap_usd=150,
                  require_simulation=False)
        self._govs.append(g2)
        d = g2.authorize(_intent(usd_value=100.0, simulated_ok=None))
        self.assertEqual(d.verdict, "allow")

    def test_report_counts_session_spend(self):
        g = self.gov(session_cap_usd=1000.0, require_simulation=False)
        g.record(_intent(usd_value=25.0, simulated_ok=None), "0xh")
        rep = g.report()
        self.assertEqual(rep["session_spent_usd"], 25.0)
        self.assertEqual(rep["session_id"], "t-sess")
        self.assertIsNone(rep["halted"])


# ─────────────────── anomali gas → TRIP (bukan block) ───────────────────
class TestGasAnomalyTrips(GovTestCase):
    def _seed_baseline(self, g, n=20, gas=100):
        for k in range(n):
            g.record(_intent(usd_value=0.01, gas_price_wei=gas, simulated_ok=None),
                     f"0xseed{k}")

    def test_gas_spike_trips_killswitch(self):
        g = self.gov(daily_cap_usd=100000.0, max_gas_multiple=4.0,
                     max_tx_per_min=1000, require_simulation=False)
        self._seed_baseline(g)  # baseline chain 56 = 100 wei
        d = g.authorize(_intent(usd_value=0.01, gas_price_wei=401, simulated_ok=None))
        self.assertEqual(d.verdict, "halt")
        self.assertIsNotNone(g.is_halted())
        # persisten: authorize berikutnya (gas normal pun) tetap halt
        d2 = g.authorize(_intent(usd_value=0.01, gas_price_wei=100, simulated_ok=None))
        self.assertEqual(d2.verdict, "halt")

    def test_gas_at_exact_multiple_allows(self):
        """Batasnya '>' bukan '>=': tepat 4.0x baseline masih allow."""
        g = self.gov(daily_cap_usd=100000.0, max_gas_multiple=4.0,
                     max_tx_per_min=1000, require_simulation=False)
        self._seed_baseline(g)
        d = g.authorize(_intent(usd_value=0.01, gas_price_wei=400, simulated_ok=None))
        self.assertEqual(d.verdict, "allow")
        self.assertIsNone(g.is_halted())

    def test_first_tx_on_chain_has_no_baseline(self):
        """Belum ada riwayat gas di chain → tidak ada baseline → tidak trip."""
        g = self.gov(daily_cap_usd=100000.0, max_gas_multiple=4.0,
                     require_simulation=False)
        d = g.authorize(_intent(chain_id=999, usd_value=0.01,
                                gas_price_wei=10 ** 12, simulated_ok=None))
        self.assertEqual(d.verdict, "allow")

    def test_baseline_is_per_chain(self):
        """Baseline chain lain tidak memengaruhi chain ini."""
        g = self.gov(daily_cap_usd=100000.0, max_gas_multiple=4.0,
                     max_tx_per_min=1000, require_simulation=False)
        self._seed_baseline(g)  # chain 56 @ 100 wei
        d = g.authorize(_intent(chain_id=1, usd_value=0.01,
                                gas_price_wei=10 ** 9, simulated_ok=None))
        self.assertEqual(d.verdict, "allow")  # chain 1 belum punya baseline


# ─────────────────── rate → TRIP (runaway protection) ───────────────────
class TestRateTrips(GovTestCase):
    def test_rate_limit_trips_killswitch(self):
        g = self.gov(daily_cap_usd=100000.0, max_tx_per_min=3, require_simulation=False)
        for n in range(3):
            g.record(_intent(usd_value=1.0, simulated_ok=None), f"0x{n}")
        d = g.authorize(_intent(simulated_ok=None))
        self.assertEqual(d.verdict, "halt")
        self.assertIsNotNone(g.is_halted())
        # tetap halt sampai reset manual
        self.assertEqual(g.authorize(_intent(simulated_ok=None)).verdict, "halt")
        g.reset_killswitch()
        self.assertIsNone(g.is_halted())


# ─────────────────── kill-switch: flag DB, berkas HALT, reset manual ───────────────────
class TestKillSwitch(GovTestCase):
    def test_db_trip_blocks_then_manual_reset(self):
        g = self.gov(daily_cap_usd=100.0, require_simulation=False)
        g.trip("manual test")
        self.assertEqual(g.authorize(_intent(simulated_ok=None)).verdict, "halt")
        g.reset_killswitch()
        self.assertEqual(g.authorize(_intent(simulated_ok=None)).verdict, "allow")

    def test_halt_file_blocks(self):
        """Berkas HALT di disk (jalur darurat operator dari shell) menghentikan semua."""
        g = self.gov(daily_cap_usd=100.0, require_simulation=False)
        halt_file = Path(self.tmp) / "HALT"
        self.assertFalse(halt_file.exists())
        self.assertEqual(g.authorize(_intent(simulated_ok=None)).verdict, "allow")
        halt_file.write_text("manual halt: liat tx aneh")
        d = g.authorize(_intent(simulated_ok=None))
        self.assertEqual(d.verdict, "halt")
        self.assertIn("liat tx aneh", g.is_halted())

    def test_halt_file_empty_still_halts(self):
        g = self.gov(daily_cap_usd=100.0, require_simulation=False)
        (Path(self.tmp) / "HALT").write_text("")
        self.assertEqual(g.authorize(_intent(simulated_ok=None)).verdict, "halt")
        self.assertEqual(g.is_halted(), "kill-switch file present")

    def test_reset_removes_halt_file_and_flag(self):
        """reset_killswitch() membersihkan KEDUA jalur halt sekaligus."""
        g = self.gov(daily_cap_usd=100.0, require_simulation=False)
        halt_file = Path(self.tmp) / "HALT"
        halt_file.write_text("darurat")
        g.trip("sekaligus flag db")
        g.reset_killswitch()
        self.assertFalse(halt_file.exists())
        self.assertIsNone(g.is_halted())
        self.assertEqual(g.authorize(_intent(simulated_ok=None)).verdict, "allow")

    def test_halt_survives_new_instance(self):
        """State halt persisten di DB: instance baru (proses baru) tetap halt."""
        g1 = self.gov(daily_cap_usd=100.0, require_simulation=False)
        g1.trip("anomali sesi sebelumnya")
        g2 = _gov(self.tmp, daily_cap_usd=100.0, require_simulation=False)
        self._govs.append(g2)
        self.assertEqual(g2.authorize(_intent(simulated_ok=None)).verdict, "halt")


# ─────────────────── record() wajib: penghitung kelalaian (perbaikan #3) ───────────────────
class TestRecordContract(GovTestCase):
    def test_unrecorded_counter_tracks_allow(self):
        g = self.gov(daily_cap_usd=100.0, require_simulation=False)
        self.assertEqual(g.report()["unrecorded_authorizations"], 0)
        i = _intent(simulated_ok=None)
        g.authorize(i)
        self.assertEqual(g.report()["unrecorded_authorizations"], 1)
        g.record(i, "0xh")
        self.assertEqual(g.report()["unrecorded_authorizations"], 0)

    def test_block_does_not_count_as_unrecorded(self):
        g = self.gov(max_tx_usd=1.0, daily_cap_usd=100.0, require_simulation=False)
        g.authorize(_intent(usd_value=50.0, simulated_ok=None))  # block: per-tx cap
        self.assertEqual(g.report()["unrecorded_authorizations"], 0)


# ─────────────────── Decision ───────────────────
class TestDecision(unittest.TestCase):
    def test_summary_and_allowed(self):
        d = G.Decision("allow")
        self.assertTrue(d.allowed)
        self.assertIn("ALLOW", d.summary())
        d2 = G.Decision("block", ["too big"])
        self.assertFalse(d2.allowed)
        self.assertIn("too big", d2.summary())
        d3 = G.Decision("halt", ["tripped"])
        self.assertFalse(d3.allowed)
        self.assertIn("HALT", d3.summary())


if __name__ == "__main__":
    unittest.main()
