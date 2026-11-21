# skill-integrity — Manifes Integritas Rak Skill

> **Kredit sumber:** diadaptasi dari `tools/skill_integrity.py` + `SKILLS.lock`
> paket **SUPERAGENT V10** — pelajaran P2 di
> internal study notes on the source package.
> AGENTS.md akar paket tidak pernah diimpor; adaptasi ini per-berkas dari
> tool sumber yang sudah terbedah.

## Apa ini dan kenapa ada

Skill adalah kode yang dijalankan agen dengan konteks penuh. Berkas skill
yang berubah diam-diam — karena edit tak tercatat, sinkronisasi yang salah,
atau sisipan pihak ketiga — tidak akan ketahuan sampai kebetulan terbaca
ulang. Tool ini menutup kelas kegagalan itu: setiap berkas di rak skill
kita di-hash SHA-256 ke dalam satu manifes, dan verifikasi berkala
membandingkan rak lawan manifes.

Ancaman yang dijaga persis seperti alasan sumbernya: riset keamanan
OpenClaw mencatat skill jahat lewat jalur distribusi plugin sebagai vektor
serangan nyata (skill bisa menyelipkan dropper di dalam konteks LLM).
Manifes tidak membuat skill jadi aman — dia membuat **perubahan** jadi
terlihat.

## Isi folder

| Berkas | Fungsi |
|---|---|
| `integrity_common.py` | Satu-satunya tempat aturan cakupan (berkas apa yang di-hash) + fungsi hash bersama. Pembuat manifes dan pemeriksa memakai logika yang sama persis. |
| `make_manifest.py` | Bangun/perbarui `skills-manifest.json` dari keadaan rak saat ini. |
| `verify.py` | Bandingkan rak lawan manifes; laporkan per berkas OK/CHANGED/MISSING/NEW. |
| `test_integrity.py` | 8 tes ujung-ke-ujung lewat CLI asli. Jalankan: `python3 test_integrity.py`. |
| `skills-manifest.json` | Manifes rak saat ini, dihasilkan oleh `make_manifest.py` (tidak disertakan dalam repo ini; buat dengan perintah di bawah). |

## Cara pakai

```bash
cd skill-integrity

# Verifikasi (read-only, aman kapan pun):
python3 verify.py            # per berkas OK/CHANGED/MISSING/NEW
python3 verify.py --quiet    # ringkasan + temuan saja

# Sertakan skill sistem read-only /opt/muse/skills (opsional):
python3 make_manifest.py --include-system

# Root lain (label bebas, boleh diulang):
python3 make_manifest.py --root nama=/path/ke/rak
python3 verify.py --manifest /path/manifes-lain.json
```

### Kode keluar verify.py

| Kode | Arti |
|---|---|
| 0 | Semua berkas cocok dengan manifes. |
| 1 | Ada temuan: CHANGED (isi berubah), MISSING (ada di manifes, hilang di rak), NEW (ada di rak, belum di-manifes). |
| 2 | Error: manifes tidak ada, manifes rusak, `manifest_sha256` internal tidak cocok (manifes kemungkinan diubah tangan), atau root hilang. Hasil verifikasi dari manifes yang error **tidak boleh dipercaya** — telaah dulu. |

## Mode degradasi parsial (prinsip mengikat)

`verify.py` **hanya melaporkan**. Mismatch tidak mematikan, menahan,
atau mengubah apa pun: tidak ada proses dihentikan, tidak ada berkas
disentuh, tidak ada skill yang dinonaktifkan otomatis. Ini meniru boot
paket sumber (verifikasi integritas adalah langkah 8 boot; gagal →
temuan ditampilkan, yang ditahan hanya operasi berisiko, agen tetap
hidup) dan vonis P2 studi kita: murah, dan menutup kelas "berkas berubah
tanpa ada yang sadar" tanpa menciptakan kelas kegagalan baru berupa
sistem yang memblokir dirinya sendiri.

Konsekuensi praktisnya: temuan verify selalu berakhir di manusia.
Dua jalan sah sesudah temuan:

1. **Perubahan dikenal** — edit/adopsi yang disengaja dan sudah disahkan
   → perbarui manifes (§di bawah).
2. **Perubahan tidak dikenal** → pulihkan berkas dari sumber tepercaya
   (git/salinan yang disahkan), lalu verify ulang sampai bersih.

## Kapan manifes diperbarui

Manifes adalah *pernyataan keadaan yang disahkan*, bukan foto otomatis.
Aturannya keras:

- **Hanya** sesudah adopsi skill baru atau edit skill yang disengaja
  **dan sudah disahkan** (user atau sesi utama yang menelaah isinya),
  manifes boleh dibangun ulang dengan `make_manifest.py`.
- **Tidak pernah** menjalankan `make_manifest.py` untuk "membersihkan"
  temuan verify yang belum ditelaah. Itu menghapus bukti perubahan —
  persis kegagalan yang tool ini dibuat untuk mencegah.
- **Tidak pernah** otomatis terjadwal. Cron boleh menjalankan
  `verify.py` (read-only, melapor), tidak boleh menjalankan
  `make_manifest.py`.
- Sesudah membangun ulang, jalankan `verify.py` sekali lagi dan pastikan
  keluar 0 sebelum manifes baru dianggap berlaku.
- Tempat wajar untuk verify berkala: audit kendali harian (satu baris
  hasil: bersih / N temuan) — selaras saran P2 studi kita.

## Aturan cakupan (apa yang di-hash)

- **Semua jenis berkas** di-hash: `.md`, `.py`, `.mjs`, `.js`, `.ts`,
  `.sh`, `.yaml`, `.json`, `.txt`, dan apa pun lainnya di rak. Skill di
  rak kita memuat skrip `.mjs`/`.sh` yang bisa dieksekusi — membatasi
  hash ke dokumen saja (seperti sumber, hanya `.md`/`.py`) akan
  meloloskan persis berkas yang paling berbahaya bila disisipi.
- **Berkas hidup/state dikecualikan** berdasarkan sifatnya:
  - ekstensi data runtime: `.jsonl`, `.log`, `.db`, `.sqlite`,
    `.sqlite3`, `.pyc`, dan nama berakhiran `.write-lock`;
  - direktori runtime di level mana pun: `__pycache__`, `.git`,
    `node_modules`, `logs`;
  - berkas manifes itu sendiri (`skills-manifest.json`).
- **Berkas kode yang bernama "state" tetap di-hash.** `state.py` adalah
  kode; yang dikecualikan adalah *data* state, bukan kata "state".
  Pengecualian bekerja per ekstensi/direktori, tidak pernah per kata
  dalam nama berkas kode.
- Simlink diikuti isinya (hash atas isi target); simlink putus membuat
  pemindaian error dan verify keluar 2 — keadaan itu harus ditelaah,
  bukan dilewati diam-diam.

## Perbedaan dari sumber (perbaikan yang disengaja)

1. **Berkas hidup tidak ikut di-hash.** Di SKILLS.lock sumber, 465
   berkas yang ter-hash memuat `logs/treasury.jsonl` dan
   `memory/2026-05.md` — berkas yang berubah karena operasi normal.
   Satu baris log baru akan menggagalkan verifikasi boot berikutnya
   dan melatih operator mengabaikan temuan (alarm fatigue). Versi kita
   mengecualikan seluruh kelas berkas itu (lihat §Aturan cakupan),
   dengan tes regresi khusus (`test_05_living_files_excluded`).
2. **Semua jenis berkas di-hash**, bukan hanya `.md`/`.py` (alasan di
   §Aturan cakupan).
3. **Dua skrip terpisah + modul cakupan bersama** (`make_manifest.py`,
   `verify.py`, `integrity_common.py`) menggantikan satu skrip
   subcommand sumber, supaya aturan cakupan hidup di satu tempat dan
   verify bisa dipanggil cron tanpa membawa jalur tulis apa pun.
4. **Konsistensi internal manifes diperiksa.** Manifes menyimpan
   `manifest_sha256` atas bagian roots-nya; verify menghitung ulang dan
   menolak (exit 2) bila tidak cocok — menangkap manifes yang diedit
   tangan. Sumber menandatangani manifes dengan Ed25519 opsional; di
   konteks kita (satu mesin, manifes di workspace yang sama) tanda
   tangan tidak menambah banyak dan tidak diwarisi — pemeriksaan
   konsistensi internal + aturan "manifes hanya diperbarui sesudah
   pengesahan" adalah penggantinya. Bila suatu saat manifes harus
   dipercaya lintas mesin, tanda tangan Ed25519 sumber adalah pola
   yang harus ditambahkan kembali.
5. **Degradasi parsial ditegaskan sebagai perilaku**, bukan sekadar
   saran boot: verify sumber mengembalikan exit 1 dan dokumen boot
   yang menahan operasi on-chain; di kita tidak ada penahan otomatis
   apa pun dari tool ini — pelaporan ke audit kendali + telaah manusia
   adalah satu-satunya tindak lanjutnya.

## Batasan jujur

- Manifes membuktikan *berkas sama dengan saat disahkan*, bukan
  *berkas aman*. Skill berbahaya yang disahkan tanpa dibaca akan
  ter-hash dengan patuh. Gerbangnya tetap pembacaan sebelum adopsi.
- Hash dihitung dari isi berkas pada saat pemindaian; perlombaan
  edit-di-tengah-pindai tidak ditangani (rak skill kita tidak ditulis
  proses lain secara bersamaan dalam praktiknya).
- Manifes bawaan mencakup folder `custom-skills` di workspace saja. Skill
  sistem `/opt/muse/skills` read-only dan diganti oleh deployment —
  memasukkannya (`--include-system`) berguna untuk deteksi, tapi
  temuannya akan menyala tiap pembaruan sistem dan harus ditafsirkan
  sebagai itu.
