# log-redact — redaksi log dua lapis

Tool redaksi teks (log, keluaran perintah, draf laporan) sebelum disimpan
atau dibaca pihak lain. Stdlib murni, tanpa jaringan, tanpa menulis berkas
(kecuali mode `--in-place` yang diminta eksplisit).

> Kredit pola: diadaptasi dari modul `aa/log.py` proyek **airdrop-agent
> karya setyamickala** — redaksi dua lapis (pola struktural + nilai literal
> env sensitif) yang di sana melewati *semua* keluaran log. Bedah sumber:
> internal study notes on the source package.

## Dua lapis

**Lapis 1 — pola struktural (regex) + literal env:**

| Pola | Contoh bentuk | Topeng |
|---|---|---|
| Private key 64-hex (`0x` opsional) | `0x` + 64 hex | `0x…` + 6 hex terakhir |
| Token bot Telegram | `<6+ digit>:<30+ karakter>` | `bot…` + 6 terakhir |
| Token Discord MFA | `Mf….<segmen>.<segmen>` | `discord…` + 6 terakhir |
| Token Discord / JWT | 3 segmen base64url | `token…` + 6 terakhir |
| StringSession Telethon | `1` + base64 250+ karakter | `session…` + 6 terakhir |
| URL proxy ber-kredensial | `http://user:pass@host:port` | password → `***` (user + host tetap terlihat) |
| Mnemonic | jendela 12/15/18/21/24 kata huruf kecil 3–8 huruf, terkonfirmasi oleh kata petunjuk (`mnemonic`, `seed phrase`, `recovery phrase`, `seed words`) dalam 40 karakter sebelumnya, atau jendela ≥ 18 kata | `mnemonic…[redacted]` (utuh, tanpa menyisakan kata apa pun) |
| Nilai literal env sensitif | nilai persis dari variabel yang dinamai pemanggil | 3 depan…3 belakang (nilai > 12), `***` (≤ 12); nilai < 8 karakter tidak dikejar |

Heuristik mnemonic diselaraskan dengan secret tripwire kita
(skrip tripwire secret pada skill scam-sentinel kami): deretan
kata dikelompokkan dulu, jendela terpanjang menang, jendela yang menang
bisa menelan 1–3 kata biasa di tepinya (bias fail-safe). Kalimat biasa
12 kata tanpa kata petunjuk sengaja **tidak** ditandai.

**Lapis 2 — konteks (yang SENGAJA tidak diredaksi):** alamat wallet
(`0x` + 40 hex) dan path vault tetap terlihat. Ini keputusan sumber yang
kita setujui: yang disembunyikan adalah *nilai rahasianya*, bukan
*identitas operasionalnya* — log yang alamatnya ikut hilang tidak bisa
diaudit, dan membedakan "wallet mana" justru fungsi utama log kita.

## Cara pakai

```bash
# stdin -> stdout
some-command 2>&1 | python3 redact.py > clean.log

# dengan nilai literal env (nilai dibaca dari env proses, tidak pernah dicetak)
MY_TOKEN=... python3 redact.py --sensitive-names MY_TOKEN < raw.log > clean.log

# mode auto: semua env yang namanya mengandung
# PASS/PASSWORD/SECRET/TOKEN/KEY/SEED/MNEMONIC/SESSION/COOKIE/PRIVATE
# (kecuali daftar simpan: VAULT_PASSWORD_FILE, TG_API_ID, IMAP_PORT, IMAP_HOST)
python3 redact.py --sensitive-auto < raw.log > clean.log

# mode berkas (ke stdout), atau tulis balik dengan --in-place
python3 redact.py FILE
python3 redact.py --in-place FILE
```

Sebagai pustaka:

```python
from redact import redact
safe = redact(text, sensitive_names=["MY_TOKEN"], sensitive_auto=False)
```

Kode keluar: 0 beres; 2 argumen salah / berkas tidak terbaca.

## Perbedaan dari sumber (disengaja, tercatat)

1. Segmen tengah pola token 3-segmen: sumber tepat `{6}` (format token
   Discord) sehingga JWT umum lolos; kita longgarkan jadi `{6,}`.
2. Topeng 64-hex sumber bisa menghasilkan `0x0x…` (awalan ganda); kita
   normalisasi jadi satu `0x…`.
3. Sumber juga menutup proxy `IP:port` polos; kita hanya menutup URL
   proxy **ber-kredensial** — `IP:port` polos adalah info operasional
   (endpoint RPC dsb.) yang sejalan lapis 2 tetap terlihat. Password-nya
   yang rahasia, bukan alamatnya.
4. Sumber mengumpulkan literal env secara global saat import; kita
   eksplisit: nama diberikan pemanggil, atau mode auto dinyatakan terang.

## Batasan jujur

- **Redaksi adalah jaring terakhir, bukan pengganti hygiene secret.**
  Jalur yang benar tetap: secret tidak pernah lewat command line, tidak
  pernah ditulis ke log sejak awal. Tool ini menangkap yang terlanjur.
- Pola 64-hex tidak bisa membedakan private key dari **tx hash** —
  keduanya teredaksi (perilaku sumber juga begitu). Bila tx hash perlu
  dibaca dari log, ambil dari sumber primernya, bukan dari log teredaksi.
- Mnemonic 12 kata **tanpa** kata petunjuk di dekatnya akan lolos
  (disengaja — harga dari tidak menandai kalimat biasa). Mnemonic ≥ 18
  kata tertangkap tanpa petunjuk.
- Format token berubah dari waktu ke waktu; pola yang tidak dikenal
  tidak tertangkap. Nilai literal env hanya tertangkap untuk nama yang
  diberikan/di-auto-kan, dan hanya bila panjangnya ≥ 8.
- Topeng menyisakan 6 karakter ekor (3+3 untuk literal) demi
  keterlacakan — cukup untuk mencocokkan "yang mana", dan itu juga
  berarti redaksi ini bukan anonimitas penuh.

## Tes

`python3 test_redact.py` — 20 kasus: satu per pola, kalimat biasa tidak
ditandai, alamat wallet + teks bersih utuh byte-per-byte, literal env
(dinamai / tidak dinamai / pendek / mode auto), idempotensi atas teks
kotor gabungan, dan CLI stdin dengan `--sensitive-names` (termasuk cek
nilai env tidak bocor ke stderr). Semua nilai secret di tes adalah palsu.
