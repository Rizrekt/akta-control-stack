# Spend Governor — Rem Belanja Terprogram (versi AkTa)

Gerbang terprogram di antara **simulasi** dan **broadcast** untuk runner/watcher
kita yang menyentuh on-chain. Setiap transaksi yang akan di-broadcast wajib
melewati `authorize()` dulu; sesudah broadcast sukses, `record()` wajib
dipanggil supaya akumulasi cap-nya akurat. Governor menyimpan seluruh riwayat
belanja di SQLite (persisten, tahan restart) dan punya kill-switch yang
menghentikan SEMUA transaksi berikutnya bila ada anomali — sampai operator
me-resetnya secara manual.

**Kredit sumber:** diadaptasi dari **SUPERAGENT V10** — `skills/hermes/scripts/governor.py`
v4.0 ("Spend Governor / Circuit Breaker") beserta dokumen `skills/hermes/references/governor.md`,
paket tooling agen pihak ketiga. Mekanisme inti (cap, baseline
gas, trip anomali, kill-switch dua jalur) dipertahankan utuh dari sumbernya;
empat cacat yang terbukti di sumbernya diperbaiki fail-closed (lihat
"Perbedaan dari sumber" di bawah). Bedah sumbernya ada di
internal study notes on the source package, and
`study-2-skills.md` §2A butir 1.

---

## Kedudukan governor di operasi kita (baca dulu)

Governor adalah **pencatat & rem**, **BUKAN** pengganti konfirmasi per-tx user
dan bukan pengganti disiplin preview kita. Urutan lengkap satu transaksi di
operasi kita tetap:

1. Baca preview/simulasi seperti adversary (tolak bila ada nilai di luar gas,
   transfer token, approval/allowance change, authority change, external call
   tak terduga, atau risk flag).
2. Konfirmasi user untuk transaksi yang material berbeda — satu "gas" untuk
   satu tx bukan untuk tx lain.
3. `governor.authorize(intent)` — rem terprogram: cap, slippage, anomali gas,
   rate, kill-switch.
4. Broadcast.
5. `governor.record(intent, tx_hash)` — **wajib, satu langkah tak terpisah
   dari broadcast** (lihat kontrak di bawah).
6. Verifikasi receipt: status `0x1`, event, block, gas terpakai, dan state
   on-chain akhir — sebelum apa pun disebut berhasil. Governor TIDAK melakukan
   langkah ini; ini tetap pekerjaan caller.

Yang governor tambahkan di atas disiplin prosedural kita: disiplin itu selama
ini hidup di dokumen kendali dan kebiasaan operator. Governor membuatnya
**ditegakkan kode** — sebuah runner yang bug, looping, atau membaca gas yang
salah akan berhenti sendiri (trip) alih-alih menguras wallet sampai ada yang
menyadari.

**Kapan governor dipakai:** setiap action yang memindahkan dana atau membayar
gas untuk aksi bernilai — claim, mint, swap, send, approve, deposit/withdraw.
**Kapan tidak perlu:** read-only murni (balance, quote, eth_call/simulasi,
monitoring) — itu tidak menyentuh governor sama sekali.

---

## Cara kerja (mekanisme lengkap, dari sumbernya)

### authorize(intent) → Decision(allow | block | halt)

`TxIntent` diisi caller: `wallet`, `chain_id`, `action`, `usd_value`,
`native_value`, `slippage_pct`, `gas_price_wei`, `simulated_ok`, `recipient`,
`note`, `value_bearing`. Cek berjalan dalam urutan ini:

| # | Cek | Default | Bila gagal |
|---|-----|---------|-----------|
| 0 | Kill-switch aktif? (berkas HALT ada, atau flag `halt` di DB) | — | **halt** untuk semua intent, tanpa cek lain |
| 1 | Sudah disimulasi? `require_simulation` dan `simulated_ok is not True` | `True` | **block** |
| 2 | `slippage_pct` > `max_slippage_pct` | 5.0% | **block** |
| 3 | `usd_value is None` padahal `value_bearing=True` | — | **block** (perbaikan #4, fail-closed) |
| 4 | `usd_value` > `max_tx_usd` | disetel caller | **block** |
| 5 | `gas_price_wei` > `max_gas_multiple` × baseline | 4.0× | **HALT** — kill-switch di-trip |
| 6 | Jumlah record 60 detik terakhir ≥ `max_tx_per_min` | 12 | **HALT** — kill-switch di-trip |
| 7 | Akumulasi harian wallet + intent > `daily_cap_usd` | disetel caller | **block** |
| 8 | Akumulasi sesi + intent > `session_cap_usd` | disetel caller | **block** |

Semantik verdict (persis sumbernya):

- **`allow`** — lolos semua cek. Caller boleh broadcast, lalu **wajib**
  `record()`. Decision membawa `remaining_daily_usd` / `remaining_session_usd`
  (sisa cap sebelum intent ini) bila cap terkait disetel.
- **`block`** — tolak intent INI saja; governor tetap jalan. Sebabnya selalu
  spesifik per intent dan tertulis di `Decision.reasons`.
- **`halt`** — kill-switch terpasang. Semua `authorize()` berikutnya juga
  `halt`, untuk semua wallet dan semua chain, sampai operator memanggil
  `reset_killswitch()` secara manual sesudah investigasi. Anomali gas dan
  rate menghasilkan `halt` (bukan `block`) karena keduanya adalah pola
  "sesuatu di luar intent ini sedang salah" — gas spike ekstrem atau loop
  yang lepas kendali — jadi seluruh mesin dihentikan, bukan satu tx saja.

Detail akuntansi (persis sumbernya):

- **Cap harian** = jendela geser 86.400 detik ke belakang dari sekarang,
  dihitung per `wallet` dari tabel `spend`. Bukan hari kalender dan bukan
  reset tengah malam.
- **Cap sesi** = seluruh record dengan `session_id` governor ini (jendela
  10⁹ detik), lintas wallet. Karena itu `session_id` menentukan apa arti
  "sesi" bagi cap ini — lihat kewajiban session_id eksplisit di bawah.
- **Baseline gas** = rata-rata `gas_price_wei` dari 20 record terakhir di
  `chain_id` yang sama. Chain tanpa riwayat gas belum punya baseline: tx
  pertamanya tidak bisa di-trip oleh cek gas (perilaku sumber, apa adanya).
- **Rate** = jumlah record dengan `ts` dalam 60 detik terakhir, global
  (semua wallet/chain) di DB ini.

### record(intent, tx_hash) — separuh kedua kontrak

Di sumbernya, `record()` diserahkan ke kedisiplinan caller dan terbukti ada
jalur yang tidak pernah memanggilnya — akibatnya cap tidak bertambah dan rem
tidak bekerja, diam-diam. Di versi kita kontraknya eksplisit:

- `record()` **wajib** dipanggil tepat sesudah broadcast mengembalikan
  tx_hash, dengan objek intent yang sama persis dengan yang di-authorize.
- Governor menghitung `unrecorded_authorizations` (authorize yang `allow`
  tapi belum di-record dalam proses ini) dan menampilkannya di `report()`.
  Angka > 0 pada akhir run = ada broadcast yang tidak tercatat = temuan
  yang harus dijelaskan, bukan angka yang diabaikan.
- `record()` mencatat bahwa tx **terkirim**, bukan bahwa tx **berhasil**.
  Receipt yang revert/gagal tetap tercatat (gas-nya memang sudah keluar dari
  wallet, jadi memang harus memakan cap) dan verifikasinya tetap kewajiban
  caller di langkah 6 urutan operasi di atas.

### Kill-switch

Dua jalur, salah satu cukup untuk halt:

1. **Berkas HALT di disk** — jalur darurat operator dari shell, tanpa menyentuh
   kode dan tanpa menunggu proses apa pun:
   ```bash
   echo "manual halt: liat tx aneh" > ~/.spend-governor/HALT
   ```
   Isi berkas menjadi alasan halt yang tampil di Decision; berkas kosong pun
   tetap halt (alasan generik "kill-switch file present").
2. **Flag `halt` di DB** — dipasang otomatis oleh `trip()` saat anomali gas /
   rate, atau manual lewat `gov.trip("alasan")` dari kode operator. Alasan
   disimpan bersama cap waktu.

Reset **hanya** lewat `gov.reset_killswitch()` — menghapus flag DB dan berkas
HALT sekaligus. Tidak ada auto-reset, tidak ada timeout, tidak ada env yang
melewatinya. Friksi ini disengaja (persis alasan dokumen sumbernya): halt yang
bisa ke-reset oleh refleks bukan halt.

State halt persisten: instance governor baru (proses baru, mesin restart)
yang membuka DB yang sama tetap melihat halt-nya.

---

## Perbedaan dari sumber (empat perbaikan fail-closed)

Hasil bedah kita (`study-4-hermes-security.md` §4.2 butir 2 dan 12, §1.8)
membuktikan empat cacat di versi sumber. Keempatnya diperbaiki di sini:

1. **Cap tidak boleh mati semua.** Di sumbernya `max_tx_usd`,
   `daily_cap_usd`, `session_cap_usd` default `None` semua — governor bisa
   "jalan" tanpa satu pun cap nilai aktif. Di sini konstruktor melempar
   `ValueError` bila ketiganya `None`. Minimal satu cap nilai harus disetel.
   Cap yang disetel juga divalidasi > 0; `max_gas_multiple` harus > 0;
   `max_tx_per_min` harus ≥ 1; `max_slippage_pct` tidak boleh negatif.
2. **session_id eksplisit wajib.** Di sumbernya `session_id` default ke
   timestamp proses (atau env), dan engine yang membuat instance baru tiap
   panggilan membuat cap sesi ter-reset tiap proses — rem yang diam-diam
   tidak bekerja. Di sini `session_id` adalah argumen keyword **wajib**
   tanpa default dan tanpa fallback: tidak diisi = `TypeError`; `None`/kosong
   = `ValueError`. Satu runner logis = satu session_id yang dipilih sadar
   dan ditulis di kode pemanggil, mis. `"pxio-claim-watch"` atau
   `"polyester-session-runner"`. Konsekuensi yang harus disadari: dua proses
   yang harus berbagi cap sesi wajib memakai session_id yang sama **dan**
   db_path yang sama; dua run yang tidak boleh tercampur wajib session_id
   berbeda.
3. **record() adalah kewajiban terdokumentasi + kelalaiannya terlihat.**
   Lihat kontrak record() di atas dan penghitung `unrecorded_authorizations`
   di `report()`.
4. **usd_value=None untuk intent bernilai = block.** Di sumbernya `None`
   melewatkan semua cap USD (dokumen sumbernya sendiri mengakuinya sebagai
   batasan). Di sini `TxIntent.value_bearing` default `True`, dan intent
   bernilai tanpa `usd_value` di-block dengan alasan eksplisit. Intent yang
   sungguh tidak memindahkan nilai boleh `value_bearing=False` — keputusan
   itu tertulis di kode pemanggil, bisa diaudit, bukan kelalaian senyap.

Perbedaan kecil lain: path default dipindah ke namespace kita
(`~/.spend-governor/` untuk DB + HALT, env prefix `SPEND_GOVERNOR_*`), dan
berkas HALT kini argumen konstruktor (`killswitch_file`) supaya tes dan
runner paralel bisa mengisolasi kill-switch-nya masing-masing.

---

## Konfigurasi untuk konteks kita: cap gas-only

Operasi kita free-only: nilai yang bergerak pada runner FCFS/watcher hampir
selalu **gas saja**. Pola setelnya: caller menghitung estimasi biaya gas
dalam USD untuk intent itu —

```
usd_value = gas_price_wei × gas_limit ÷ 10^18 × harga_native_usd
```

— lalu cap disetel pada skala gas, bukan skala nilai aset:

| Limit | Makna pada konteks gas-only | Contoh setelan awal* |
|-------|------------------------------|----------------------|
| `max_tx_usd` | biaya gas maksimum yang masuk akal untuk SATU tx | $0.50 |
| `daily_cap_usd` | total gas per wallet per 24 jam geser | $5.00 |
| `session_cap_usd` | total gas untuk satu run/sesi runner ini | $2.00 |
| `max_gas_multiple` | gas price > 4× rata-rata 20 tx terakhir chain itu = ada yang salah → halt | 4.0 (default sumber) |
| `max_tx_per_min` | runner klaim yang sehat tidak menembak > 12 tx/menit; lebih dari itu = loop lepas → halt | 12 (default sumber) |
| `require_simulation` | intent tanpa simulasi bersih tidak boleh lewat | `True` (default sumber) |

\* Contoh, bukan ketetapan: angka persisnya mengikuti gas riil chain dan pola
runner-nya, dan perubahan angka adalah keputusan operator. Yang tidak boleh
berubah: minimal satu cap ter-setel, dan cap disetel pada skala yang membuat
trip-nya **bermakna** — cap $10.000 pada runner gas-only sama saja tidak punya
rem.

Catatan khusus FCFS: jendela FCFS memang menuntut tembakan cepat di detik
buka, dan governor tidak boleh menjadi alasan kehilangan jendela. Urutannya
tetap aman untuk FCFS karena semua pekerjaan berat (preview, simulasi,
authorize) terjadi **sebelum** jendela buka pada calldata yang sudah
di-pre-stage; yang tersisa di detik buka hanya broadcast + record. Trip
karena gas spike justru melindungi di momen itu: gas yang melonjak jauh di
atas baseline 20 tx terakhir pada detik buka adalah persis keadaan di mana
otorisasi berdiri FCFS kita sendiri memerintahkan berhenti dan lapor, bukan
menembak membabi buta.

Contoh integrasi minimal (runner klaim BSC, gas-only):

```python
from governor import SpendGovernor, GovernorLimits, TxIntent

gov = SpendGovernor(
    GovernorLimits(max_tx_usd=0.50, daily_cap_usd=5.0, session_cap_usd=2.0,
                   require_simulation=True),
    session_id="pxio-claim-watch",          # eksplisit & stabil — WAJIB
)   # db_path & killswitch_file default: ~/.spend-governor/governor.db & HALT

intent = TxIntent(
    wallet=wallet_address, chain_id=56, action="claim",
    usd_value=gas_cost_usd_estimate,        # dihitung caller — None = block
    gas_price_wei=tx_gas_price_wei,
    simulated_ok=preview_bersih,            # hasil eth_call/preview caller
)

decision = gov.authorize(intent)
if not decision.allowed:
    log(decision.summary())                 # JANGAN broadcast
    raise SystemExit(1)

tx_hash = broadcast(tx)                     # jalur broadcast runner itu sendiri
gov.record(intent, tx_hash)                 # WAJIB — tepat sesudah broadcast
receipt = wait_and_verify_receipt(tx_hash)  # tetap kewajiban caller
```

Untuk watcher yang berbagi DB dengan runner lain (cap harian wallet yang
sama harus terlihat lintas proses), arahkan `db_path` ke berkas yang sama
dan pakai `session_id` yang berbeda per runner logis. SQLite lokal hanya
menge-cap per-mesin; cap lintas mesin butuh koneksi DB bersama yang
di-inject lewat argumen `conn=` — di luar kebutuhan kita saat ini.

### Variabel env (alternatif konstruktor eksplisit)

`GovernorLimits.from_env()` membaca: `SPEND_GOVERNOR_MAX_TX_USD`,
`SPEND_GOVERNOR_DAILY_CAP_USD`, `SPEND_GOVERNOR_SESSION_CAP_USD`,
`SPEND_GOVERNOR_MAX_SLIPPAGE_PCT`, `SPEND_GOVERNOR_MAX_GAS_MULTIPLE`,
`SPEND_GOVERNOR_MAX_TX_PER_MIN`, `SPEND_GOVERNOR_REQUIRE_SIM` (`"0"` =
matikan — tidak disarankan). Path: `SPEND_GOVERNOR_DIR` (folder default),
`SPEND_GOVERNOR_DB`, `SPEND_GOVERNOR_HALT`.

Peringatan: `from_env()` tanpa satu pun env cap akan menghasilkan konfigurasi
yang **ditolak konstruktor** (perbaikan #1). Itu disengaja — governor yang
gagal start dengan pesan jelas lebih baik daripada governor yang jalan tanpa
rem.

---

## API ringkas

- `SpendGovernor(limits=None, db_path=DEFAULT_DB, *, session_id, killswitch_file=DEFAULT_KILLSWITCH_FILE, conn=None)`
  — `limits=None` berarti `GovernorLimits.from_env()`. Context manager
  (`with`) didukung; `close()` aman dipanggil berkali-kali.
- `authorize(intent) -> Decision` — `Decision.verdict` ∈
  `{"allow","block","halt"}`, `Decision.allowed`, `Decision.reasons`,
  `Decision.remaining_daily_usd`, `Decision.remaining_session_usd`,
  `Decision.summary()`.
- `record(intent, tx_hash)` — lihat kontrak di atas.
- `is_halted() -> Optional[str]` — alasan halt atau `None`.
- `trip(reason)` — pasang kill-switch (flag DB) manual/otomatis.
- `reset_killswitch()` — operator-only, manual, sesudah investigasi.
- `report() -> dict` — `session_id`, `halted`, `session_spent_usd`,
  `unrecorded_authorizations`, `limits`.

## Tes

34 tes invarian (stdlib `unittest`, tanpa dependensi):

```bash
cd spend-governor
python3 -m unittest test_governor -v
```

Cakupan: penolakan konfigurasi fail-closed (semua cap None, dari env kosong,
cap ≤ 0, parameter anomali tidak sah, session_id hilang/None/kosong/bukan
keyword), block usd-None untuk intent bernilai + pengecualian
`value_bearing=False` eksplisit, block simulasi/slippage/cap per-tx (termasuk
batas tepat-sama = allow), akumulasi cap harian per wallet + isolasinya,
cap sesi lintas wallet + isolasinya per session_id, trip kill-switch oleh
anomali gas (termasuk batas tepat 4.0× = allow, baseline per chain, chain
tanpa baseline), trip oleh rate, halt dari flag DB dan dari berkas HALT
(isi teks maupun kosong), reset manual membersihkan kedua jalur, halt yang
persisten lintas instance, dan penghitung `unrecorded_authorizations`.
Hasil run terakhir: **34 tests, OK** (Python 3.12.3).

Demo cepat (menulis ke folder state sementara, bukan state asli):

```bash
SPEND_GOVERNOR_DIR=$(mktemp -d) python3 governor.py
```

## Batasan jujur

- Governor hanya sebaik `usd_value` yang diberikan caller. Dia tidak punya
  oracle harga sendiri dan tidak memverifikasi angka itu. Caller yang salah
  hitung memberi rem yang salah kalibrasi — karena itu usd None untuk intent
  bernilai di-block, tapi usd yang **salah** tetap tanggung jawab caller.
- Governor menahan broadcast; dia tidak bisa menarik tx yang sudah terkirim,
  dan tidak memverifikasi receipt. Pre-flight gate, bukan undo, bukan bukti
  keberhasilan.
- Cek gas-spike butuh riwayat: 20 record terakhir per chain menjadi baseline.
  Runner baru di chain baru berjalan tanpa perlindungan gas-spike sampai
  riwayatnya terbentuk (cek lain tetap jalan penuh).
- `unrecorded_authorizations` adalah penghitung in-memory per proses —
  alat visibilitas selama run, bukan audit lintas restart. Audit lintas
  restart hidup di tabel `spend` itu sendiri (bandingkan jumlah broadcast
  yang diketahui caller lawan jumlah baris record).
- Satu DB SQLite = satu mesin. Proses-proses di mesin yang sama berbagi cap
  lewat db_path yang sama; jangan menjalankan dua writer berat bersamaan
  tanpa memahami bahwa SQLite mengunci per tulis (transaksinya pendek —
  satu INSERT/SELECT per operasi — dan pola runner kita jauh di bawah batas
  itu).

## Berkas

- `governor.py` — implementasi (stdlib murni, tanpa dependensi eksternal).
- `test_governor.py` — tes invarian di atas.
- `README.md` — dokumen ini.
