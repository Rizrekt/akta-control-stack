# Cost Ledger — Buku Kas Belanja API + On-Chain (versi AkTa)

Buku kas tunggal untuk semua pengeluaran operasi: API berbayar/berkuota
(Exa REST, OpenRouter, CrowLLM, DashScope, RPC premium, ...), gas on-chain
(klaim/mint/swap — nilai native yang benar-benar keluar dari wallet), dan
infra (langganan, VPS, domain). Setiap entri membawa **proyek**, **lane/
sumber**, **kategori**, **jumlah + mata uang**, dan **waktu yang di-inject
caller**, sehingga pertanyaan "kita udah bakar berapa, di proyek apa, lewat
lane apa, hari ini berapa" selalu bisa dijawab dari satu berkas SQLite —
bukan dari ingatan atau potongan log.

**Kredit sumber:** diadaptasi dari **SUPERAGENT V10**, paket dari **ketua
umum komunitas Airdrop Finder** — `tools/cost_ledger.py` v4.2 ("Unified
Cost & Usage Ledger") dan pola dedup/cooldown alert dari `tools/alerts.py`
v4.0 ("Conditional Alert Engine"). Mekanisme yang dipertahankan utuh dari
sumbernya: satu tabel SQLite untuk semua jenis belanja, koneksi bisa
di-inject (tes memakai `:memory:`), agregasi dibaca dari baris mentah,
lock internal + `check_same_thread=False` untuk pemanggil multi-thread,
dan alert yang tidak menembak dua kali untuk keadaan yang sama. Bedah
sumbernya ada di internal study notes on the source package
§2.6 (cost_ledger) dan tabel inventaris/keamanan alerts.py (baris 46,
317, 357; vonis cost_ledger: **TIRU-POLA** — "pola observability murah
untuk melacak bakar kuota lane gratis kita").

---

## Pasangan kerja dengan spend-governor (baca dulu)

Dua tool ini adalah dua separuh dari satu disiplin belanja, dan keduanya
diadaptasi dari paket yang sama (governor dari `skills/hermes/scripts/
governor.py`, ledger dari `tools/cost_ledger.py`):

| | spend-governor | cost-ledger |
|---|---|---|
| Kedudukan | **Rem SEBELUM belanja** | **Buku SESUDAH belanja** |
| Pertanyaan yang dijawab | "Boleh tx ini jalan?" | "Kita sudah bakar berapa, di mana?" |
| Momen kerja | Di antara simulasi dan broadcast | Sesudah biaya benar-benar terjadi |
| Keputusan | `authorize()` → allow / block / halt | Tidak memutuskan apa pun — mencatat & melaporkan |
| Cakupan | Hanya belanja on-chain bernilai (yang lewat broadcast) | Semua belanja: api + gas + infra, on-chain maupun bukan |
| State | SQLite cap + kill-switch (`~/.spend-governor/`) | SQLite entri + ambang + riwayat alert (`~/.cost-ledger/`) |
| Kegagalan khas yang ditutup | Runner looping / gas spike menguras wallet diam-diam | Kuota lane gratis terbakar tanpa ada yang sadar totalnya |

Alur satu transaksi on-chain yang lengkap di operasi kita:

1. Preview/simulasi dibaca seperti adversary (disiplin tetap kita).
2. Konfirmasi user untuk transaksi yang material berbeda.
3. `governor.authorize(intent)` — rem: cap per-tx/harian/sesi, anomali
   gas, rate, kill-switch. Tidak allow = tidak broadcast.
4. Broadcast.
5. `governor.record(intent, tx_hash)` — wajib, separuh kedua kontrak
   governor.
6. Verifikasi receipt (status `0x1`, event, gas terpakai) — kewajiban
   caller, bukan kedua tool ini.
7. **`ledger.record(...)`** — catat biaya RIILnya (gas terpakai dari
   receipt, dalam mata uang native + nilai USD-nya bila diketahui) ke
   buku kas, dengan proyek + lane + kategori `gas`.

Untuk belanja non-on-chain tidak ada governor (tidak ada broadcast):
caller langsung `ledger.record(...)` sesudah panggilan selesai — mis.
satu panggilan Exa deep-search selesai → satu entri `api` dengan lane
`search-api`. Governor tidak pernah melihat belanja itu; ledger adalah
satu-satunya catatan totalnya. Karena itu ambang alert harian ledger
(lihat di bawah) adalah jaring untuk total gabungan, sedangkan cap
governor adalah rem keras per-transaksi: keduanya sengaja tumpang tindih
di wilayah gas, karena gas adalah satu-satunya belanja yang bisa bocor
dalam hitungan detik oleh bug.

Yang ledger **bukan**: bukan gerbang, bukan bukti keberhasilan tx, bukan
oracle harga. Dia mencatat angka yang diberikan caller; angka yang salah
dari caller menghasilkan buku yang salah dengan rapi.

---

## Cara kerja

### Skema

Satu berkas SQLite (default `~/.cost-ledger/ledger.db`), tiga tabel:

- `entries(id, ts, project, lane, category, amount, currency, note)` —
  satu baris per pengeluaran. `ts` epoch detik **yang di-inject caller**.
- `daily_thresholds(scope, currency, amount)` — ambang total harian
  operator. `scope = '*'` berarti global (semua proyek digabung); nilai
  lain adalah nama proyek.
- `alert_state(scope, currency, day, fired_ts, total_at_fire, threshold)`
  — riwayat alert yang sudah menyala; kunci (scope, currency, day)
  membuat satu ambang menyala maksimal sekali per hari.

### record() — kontrak pencatatan

```python
led.record(ts=..., project="demo-project", lane="search-api", category="api",
           amount=0.25, currency="USD", note="deep search edisi 4")
```

- **`ts` wajib, tanpa default.** Modul ini tidak pernah memanggil
  `time.time()` untuk entri. Caller yang tahu kapan biaya terjadi yang
  menyatakannya. (Sumbernya menebak sendiri dengan `time.time()` di
  `_insert` — di operasi kita, waktu yang ditebak adalah kelas bug
  deadline yang sudah terbukti membakar slot; maka diperbaiki di sini.)
- **`category` harus salah satu `api | gas | infra`** — selain itu
  ditolak (`ValueError`). Pemetaan dari tiga kind sumbernya:
  - `api` — panggilan API/LLM berbayar atau berkuota: token LLM
    (kind `tokens` sumber), panggilan API eksternal (kind `api` sumber),
    RPC premium per-request. Untuk token LLM, caller menghitung USD-nya
    sendiri dari tarif lane-nya lalu mencatat angka jadinya — ledger ini
    tidak menyimpan peta harga per-token seperti sumbernya, karena tarif
    lane gratis kita berubah-ubah dan angka tebakan di dalam buku lebih
    berbahaya daripada angka yang dihitung caller di titik panggil.
  - `gas` — biaya jaringan on-chain (kind `onchain` sumber): gas klaim,
    mint, swap, deploy. Catat dalam mata uang native (BNB/ETH/SOL) dari
    receipt; bila nilai USD-nya juga mau dilacak, catat sebagai entri
    kedua bermata uang USD dengan note yang merujuk tx yang sama — kedua
    entri itu TIDAK akan pernah dijumlahkan bersama (lihat aturan mata
    uang di bawah).
  - `infra` — belanja penunjang yang bukan per-panggilan: langganan RPC,
    VPS, domain, penyimpanan. Kategori ini tidak ada di sumbernya;
    ditambahkan karena belanja infra kita nyata dan selama ini tidak
    tercatat di mana pun.
- **`amount` harus angka finite.** NaN/inf ditolak. Negatif diizinkan
  HANYA sebagai koreksi/refund atas entri sebelumnya (tulis alasannya
  di `note`); total adalah jumlah aljabar semua entri, jadi koreksi
  benar-benar mengurangi total.
- **`currency`** dinormalisasi huruf besar (`bnb` → `BNB`).
- **`project` dan `lane`** string tidak kosong. `lane` = sumber/lane
  persis yang membakar biaya (`search-api`, `llm-router`, `bsc`,
  `publicnode`, ...), supaya laporan per lane menunjukkan lane mana yang
  mahal — pasangan langsung untuk keputusan lane di operasi kita.
- Mengembalikan `id` baris.

### Aturan mata uang: total tidak pernah dicampur

Semua agregasi dikelompokkan per mata uang. `0.25 USD` dan `0.0004 BNB`
hidup sebagai dua angka terpisah di setiap laporan — tidak ada konversi
terselubung, tidak ada "total" lintas mata uang. Konversi adalah
keputusan caller (dengan harga yang ia nyatakan sendiri di note bila
perlu), bukan tebakan buku.

### Laporan

`summary()` membaca baris mentah dalam jendela `[since, until)` (tanpa
batas = seluruh buku) dengan filter opsional `project` / `lane` /
`category`, dan menghasilkan `LedgerSummary`:

- `totals_by_currency` — **total berjalan** per mata uang.
- `by_project`, `by_lane`, `by_category`, `by_day` — masing-masing
  `{nama: {mata uang: jumlah}}`.
- `entries` — jumlah entri dalam jendela.
- `.report()` — render teks multi-baris (format dipertahankan dari
  sumbernya: diawali `💸 cost ledger`).

`daily_total(day, project=None, currency="USD")` menjawab total satu
hari tertentu — fungsi yang sama yang dipakai pemeriksa alert.

### Hari dan tz_offset_s

Hari untuk laporan per hari + alert harian dihitung dari
`ts + tz_offset_s` yang ditafsirkan sebagai UTC. Default `tz_offset_s=0`
= hari kalender UTC murni. Operasi kita hidup di WIB: konstruksi dengan
`tz_offset_s=7*3600` (25200) membuat "hari ini" berarti hari WIB, tanpa
mengubah `ts` yang tersimpan. Pilihan offset adalah keputusan operator
yang tertulis di kode pemanggil — bukan tebakan modul, dan dua instance
dengan offset berbeda akan mengelompokkan hari secara berbeda atas data
yang sama (itu konsekuensi yang disadari, didokumentasikan di sini).

### Alert ambang harian (teks saja, tanpa kirim)

```python
led.set_daily_threshold(10.0, currency="USD")                    # global
led.set_daily_threshold(3.0, currency="USD", scope="demo-project")    # per proyek
fired = led.check_daily_alerts(now_ts=...)   # -> list[str] pesan teks
```

- `check_daily_alerts(now_ts)` menghitung total **hari dari `now_ts`**
  (menurut `tz_offset_s`) untuk tiap ambang terpasang, dan untuk yang
  totalnya **melewati** ambang menghasilkan satu pesan teks, mis.:
  `⚠️ COST ALERT [2026-11-21] total harian proyek 'demo-project' = 3.5000 USD — melewati ambang 3.0000 USD`.
- **Dedup persisten, maksimal sekali per (scope, mata uang, hari).**
  Pola ini warisan `alerts.py` sumber (di sana: cooldown per rule di
  SQLite supaya alert tidak refire sampai cooldown lewat). Di sini
  disederhanakan menjadi per-hari karena ambangnya harian: keadaan
  tersimpan di tabel `alert_state`, jadi pemeriksaan ulang — bahkan oleh
  proses baru — tidak mengulang alert yang sama, dan hari baru boleh
  menyala lagi. `alerts_fired(day=None)` membaca riwayatnya.
- **Keluaran hanya teks yang dikembalikan ke caller.** Tidak ada
  notifier, tidak ada jaringan, tidak ada pengiriman ke mana pun —
  caller (runner, cron, sesi operator) yang memutuskan menampilkan atau
  meneruskannya. Mesin alert sumbernya yang lengkap (rule harga/gas/
  wallet via fetcher DexScreener httpx + push Telegram/Discord) sengaja
  TIDAK diwarisi: study-3 menandai jalur DexScreener-nya mengirim alamat
  token yang dipantau ke API publik, dan kebutuhan kita dari alerts.py
  hanyalah pola "kabarin kalau total lewat ambang" — yang di sini
  dijawab dari data buku sendiri, tanpa sumber luar.

### Export JSONL

`export_jsonl(path)` menulis semua entri (urut waktu) sebagai JSON
Lines dengan field `id, ts, day, project, lane, category, amount,
currency, note` — untuk portabilitas dan arsip teks. SQLite tetap
sumber kebenaran; JSONL adalah salinan baca-saja sekali tulis.

---

## Perbedaan dari sumber (ringkasan keputusan adaptasi)

1. **Waktu di-inject, bukan ditebak.** Sumber: `_insert` memakai
   `time.time()`. Kita: `ts` argumen wajib tanpa default di `record()`
   dan `now_ts` wajib di `check_daily_alerts()`. (CLI di berkas yang
   sama adalah caller — ia meng-inject `time.time()` secara eksplisit
   bila `--ts` tidak diberikan, dan itu tertulis di help-nya.)
2. **Dimensi proyek + lane eksplisit.** Sumber mengelompokkan per
   `provider`/`chain_id`/`session_id`. Kita: `project` dan `lane`
   adalah kolom kelas-satu karena pertanyaan operasi kita berbentuk
   proyek × lane; `chain_id` sumber terserap ke `lane` (mis. `bsc`).
3. **Kategori api|gas|infra** menggantikan kind tokens|onchain|api
   sumber (pemetaan di atas), dengan validasi fail-closed.
4. **Tanpa peta harga token internal.** Sumber mengestimasi USD token
   dari `PRICE_PER_1K` bawaan + env JSON. Kita membuangnya: caller
   mencatat USD hasil hitungannya sendiri. Buku tidak mengarang angka.
5. **Alert dipersempit jadi ambang total harian, teks saja.** Mesin
   rule umum + fetcher jaringan + notifier sumber tidak diwarisi
   (alasan di bagian alert). Dedup cooldown → dedup per-hari persisten.
6. **Validasi fail-closed ditambahkan.** Sumber menerima hampir semua
   input apa adanya. Kita menolak: kategori tak dikenal, project/lane
   kosong, amount NaN/inf/bukan angka, ts bukan angka/negatif/NaN/inf,
   ambang ≤ 0.
7. **Namespace path dipindah** ke `~/.cost-ledger/` dengan env prefix
   `COST_LEDGER_*` (sumber: `~/.superagent/ledger.db` +
   `SUPERAGENT_LEDGER_DB`).

Yang TIDAK berubah dari sumber: format umum laporan teks, pola injeksi
koneksi untuk tes, pola lock + commit-per-insert, dan gagasan intinya —
satu tabel untuk semua dunia belanja.

---

## API ringkas

- `CostLedger(db_path=DEFAULT_DB, conn=None, tz_offset_s=0.0)` — context
  manager didukung; `close()` aman dipanggil berkali-kali.
- `record(*, ts, project, lane, category, amount, currency="USD", note="") -> int`
- `entries(*, since=None, until=None, project=None, lane=None, category=None) -> list[Entry]`
- `summary(*, since=None, until=None, project=None, lane=None, category=None) -> LedgerSummary`
  — `LedgerSummary.report() -> str`.
- `daily_total(day, *, project=None, currency="USD") -> float`
- `set_daily_threshold(amount, *, currency="USD", scope="*")` /
  `daily_thresholds() -> list[(scope, currency, amount)]` /
  `remove_daily_threshold(*, currency="USD", scope="*")`
- `check_daily_alerts(*, now_ts) -> list[str]` /
  `alerts_fired(day=None) -> list[dict]`
- `export_jsonl(path) -> int` (jumlah baris)

Variabel env: `COST_LEDGER_DIR` (folder default), `COST_LEDGER_DB`
(path DB persis; menang atas DIR).

## CLI

```bash
cd cost-ledger

# catat (ts ISO-8601 atau epoch; tanpa --ts = jam sistem, di-inject CLI)
python3 ledger.py --tz-offset-s 25200 add \
  --ts 2026-11-21T13:00:00+07:00 --project demo-project --lane search-api \
  --category api --amount 0.25 --note "deep search edisi 4"

# laporan total berjalan + per proyek/lane/kategori/hari
python3 ledger.py --tz-offset-s 25200 report

# ambang harian: global dan per proyek
python3 ledger.py threshold --amount 10 --currency USD
python3 ledger.py threshold --amount 3 --currency USD --scope demo-project

# periksa alert (mencetak hanya yang BARU menyala)
python3 ledger.py --tz-offset-s 25200 check-alerts

# ekspor
python3 ledger.py export --out ~/ledger-export.jsonl
```

`--db` mengarahkan ke berkas SQLite lain (mis. per mesin/per konteks);
`--tz-offset-s 25200` membuat batas hari mengikuti WIB.

## Tes

23 tes invarian (stdlib `unittest`, tanpa dependensi, semua timestamp
deterministik — tes tidak menyentuh jam sistem):

```bash
cd cost-ledger
python3 -m unittest test_ledger -v
```

Cakupan: kontrak waktu-di-inject (ts hilang = `TypeError`; ts string/
negatif/NaN/inf ditolak), validasi kategori/project/lane/amount/currency,
koreksi negatif mengurangi total, total tidak pernah mencampur mata
uang, agregasi per proyek/lane/kategori/hari, filter jendela + proyek,
`daily_total`, pergeseran ember hari oleh `tz_offset_s` (18:00 UTC =
hari berikutnya di WIB), ambang (set/list/remove + penolakan ≤ 0),
alert menyala tepat sekali per hari lalu dedup, menyala lagi hari
berikutnya, isolasi scope proyek, agregasi scope global lintas proyek,
isolasi mata uang ambang, persistensi entri + keadaan alert lintas
instance, dan ekspor JSONL. Hasil run terakhir: **23 tests, OK**
(Python 3.12.3). Smoke test CLI ujung-ke-ujung (add → threshold →
check-alerts menyala → check-alerts ulang senyap → report → export)
juga terverifikasi manual pada DB sementara.

## Batasan jujur

- **DB tidak dienkripsi.** Persis catatan keamanan study-3 atas
  sumbernya: field `note` adalah teks bebas — jangan pernah menulis
  secret, API key, atau potongan kredensial ke `note`. Tulis rujukan
  ("tx 0x…", "invoice #…"), bukan rahasianya.
- **Buku hanya sebaik caller-nya.** Biaya yang tidak dicatat tidak ada
  di buku; tidak ada rekonsiliasi otomatis lawan saldo wallet atau
  tagihan provider. Total berjalan adalah total dari yang DICATAT —
  laporkan dengan kata itu, bukan sebagai "total belanja kita" tanpa
  kualifikasi, kecuali caller-nya memang semua jalur belanja.
- **Alert hanya menyala bila ada yang memanggil `check_daily_alerts`.**
  Tidak ada loop/daemon di modul ini (mesin sumbernya punya loop poll
  async — tidak diwarisi). Pasang pemanggilnya di runner/cron yang
  memang jalan; alert yang tidak pernah diperiksa tidak pernah menyala.
- **Ambang adalah per mata uang dan tidak ada konversi.** Ambang USD
  tidak akan pernah dipicu oleh belanja BNB. Setel ambang per mata uang
  yang benar-benar dipakai lane terkait.
- **Satu DB SQLite = satu mesin.** Proses di mesin yang sama berbagi
  buku lewat path yang sama; buku lintas mesin butuh koneksi bersama
  yang di-inject lewat `conn=` — di luar kebutuhan kita saat ini.
- **Agregasi dibaca dari baris mentah di Python** (pola sumber, apa
  adanya): jujur dan portabel untuk skala operasi personal, bukan untuk
  jutaan baris.

## Berkas

- `ledger.py` — implementasi (stdlib murni, tanpa dependensi eksternal,
  tanpa jaringan).
- `test_ledger.py` — tes invarian di atas.
- `README.md` — dokumen ini.
