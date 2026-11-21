#!/usr/bin/env python3
"""log-redact — redaksi log dua lapis (stdlib murni).

Pola diadaptasi dari `aa/log.py` proyek airdrop-agent karya setyamickala
(lihat README.md): semua keluaran yang akan dibaca pihak lain / disimpan
melewati redaksi ini dulu.

Lapis 1 — pola struktural (regex):
  * private key 64-hex (dengan atau tanpa awalan 0x)
  * token bot Telegram            (<id>:<35-an karakter>)
  * token Discord (MFA + format 3 segmen) dan JWT generik 3 segmen
  * string sesi Telethon (StringSession: "1" + base64 panjang)
  * URL proxy ber-kredensial      (skema://user:password@host)
  * mnemonic — heuristik jendela kata yang sama dengan secret tripwire
    kita: jendela 12/15/18/21/24 kata huruf kecil 3-8 huruf; terkonfirmasi
    bila ada kata petunjuk (mnemonic / seed phrase / recovery phrase /
    seed words) dalam 40 karakter sebelum jendela, ATAU jendela >= 18 kata.
  * nilai literal variabel env sensitif: nama variabel diberikan pemanggil
    (argumen `sensitive_names` / flag CLI `--sensitive-names`, atau mode
    `--sensitive-auto` yang meniru daftar nama sumber). Nilai dibaca dari
    env proses dan TIDAK PERNAH dicetak — hanya diganti topengnya.

Lapis 2 — konteks (keputusan sumber yang kita setujui):
  alamat wallet (0x + 40 hex) dan path vault SENGAJA tetap terlihat.
  Yang disembunyikan adalah nilai rahasianya, bukan identitas
  operasionalnya — log yang alamatnya ikut hilang tidak bisa diaudit.

Semua fungsi murni teks; modul ini tidak membuka jaringan dan tidak
menulis berkas apa pun. `redact()` idempoten: redact(redact(x)) == redact(x).

CATATAN BATAS: redaksi adalah jaring terakhir, bukan pengganti hygiene
secret. Pola 64-hex tidak bisa membedakan private key dari tx hash —
keduanya teredaksi (persis perilaku sumber).
"""

from __future__ import annotations

import os
import re
import sys

# ---------------------------------------------------------------------------
# Lapis 1a — nilai literal env sensitif
# ---------------------------------------------------------------------------

# Nama yang, dalam mode --sensitive-auto, dianggap sensitif (meniru sumber).
_SENSITIVE_NAME = re.compile(
    r"(PASS|PASSWORD|SECRET|TOKEN|KEY|SEED|MNEMONIC|SESSION|COOKIE|PRIVATE)", re.I
)
# Nama yang nilainya berguna di log dan tidak ikut diredaksi pada mode auto
# (meniru KEEP_ENV sumber).
_KEEP_NAMES = {"VAULT_PASSWORD_FILE", "TG_API_ID", "IMAP_PORT", "IMAP_HOST"}

_MIN_LITERAL_LEN = 8  # nilai lebih pendek dari ini tidak dikejar (meniru sumber)


def _mask_literal(value: str) -> str:
    if len(value) > 12:
        return value[:3] + "…" + value[-3:]
    return "***"


def _env_literals(names) -> list[str]:
    vals = []
    for name in names:
        v = os.environ.get(name)
        if v and len(v) >= _MIN_LITERAL_LEN and v not in vals:
            vals.append(v)
    return vals


def _auto_env_literals() -> list[str]:
    return _env_literals(
        k for k in os.environ if _SENSITIVE_NAME.search(k) and k not in _KEEP_NAMES
    )


# ---------------------------------------------------------------------------
# Lapis 1b — pola struktural
# ---------------------------------------------------------------------------

def _mask_tail(prefix: str, keep: int):
    def fn(m: re.Match) -> str:
        return prefix + "…" + m.group(0)[-keep:]
    return fn


_PATTERNS = [
    # private key / 64-hex (0x opsional) — topeng menyisakan 6 hex terakhir
    # (awalan dinormalisasi jadi satu "0x…"; sumber menghasilkan "0x0x…")
    (re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b"),
     lambda m: "0x…" + m.group(0)[-6:]),
    # token bot Telegram
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), _mask_tail("bot", 6)),
    # token Discord MFA
    (re.compile(r"\b(MFK|Mf)[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}\b"),
     _mask_tail("discord", 6)),
    # token Discord biasa / JWT generik (3 segmen base64url).
    # Segmen tengah sumber tepat {6} (format Discord); kita longgarkan
    # jadi {6,} supaya JWT umum (segmen payload panjang) ikut tertangkap.
    (re.compile(r"\b[A-Za-z0-9_-]{22,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{25,}\b"),
     _mask_tail("token", 6)),
    # StringSession Telethon: "1" diikuti base64 sangat panjang
    (re.compile(r"\b1[A-Za-z0-9_+/=-]{250,}\b"), _mask_tail("session", 6)),
    # URL proxy ber-kredensial: skema://user:password@host — password saja
    # yang ditutup; skema, user, dan host tetap terlihat untuk debugging.
    (re.compile(r"\b((?:https?|socks5)://[^:\s/@]+):([^@\s/]+)@"),
     lambda m: m.group(1) + ":***@"),
]

# ---------------------------------------------------------------------------
# Lapis 1c — mnemonic (heuristik jendela, selaras secret tripwire kita)
# ---------------------------------------------------------------------------

_HINT = re.compile(r"(mnemonic|seed\s*phrase|recovery\s*phrase|seed\s*words)", re.I)
_HINT_RANGE = 40
_WINDOW_SIZES = (24, 21, 18, 15, 12)
_WORD_RUN = re.compile(r"[a-z]{3,8}(?: [a-z]{3,8})+")
_TOKEN = re.compile(r"[a-z]{3,8}")


def _find_mnemonic_spans(text: str) -> list[tuple[int, int]]:
    """Kembalikan span (start, end) deretan kata yang dinilai mnemonic.

    Deretan kata huruf-kecil dikelompokkan dulu, lalu tiap jendela
    24/21/18/15/12 kata diuji: lolos bila ada kata petunjuk dalam
    40 karakter sebelum awal jendela, atau panjang jendela >= 18.
    Jendela terpanjang menang; span yang tumpang tindih digabung.
    """
    spans: list[tuple[int, int]] = []
    for run in _WORD_RUN.finditer(text):
        words = [(m.start(), m.end()) for m in _TOKEN.finditer(run.group(0))]
        base = run.start()
        words = [(base + s, base + e) for s, e in words]
        n = len(words)
        i = 0
        while i < n:
            hit = None
            for size in _WINDOW_SIZES:
                if i + size > n:
                    continue
                start = words[i][0]
                before = text[max(0, start - _HINT_RANGE):start]
                if size >= 18 or _HINT.search(before):
                    hit = (start, words[i + size - 1][1])
                    i += size
                    break
            if hit:
                spans.append(hit)
            else:
                i += 1
    return spans


def _redact_mnemonics(text: str) -> str:
    spans = _find_mnemonic_spans(text)
    if not spans:
        return text
    out = []
    prev = 0
    for start, end in spans:
        out.append(text[prev:start])
        out.append("mnemonic…[redacted]")
        prev = end
    out.append(text[prev:])
    return "".join(out)


# ---------------------------------------------------------------------------
# API utama
# ---------------------------------------------------------------------------

def redact(text: str, sensitive_names=(), sensitive_auto: bool = False) -> str:
    """Redaksi `text`. Nilai env untuk `sensitive_names` dibaca dari proses
    ini dan tidak pernah dikembalikan/dicetak. Idempoten."""
    if not text:
        return text
    out = str(text)

    literals = _env_literals(sensitive_names)
    if sensitive_auto:
        for v in _auto_env_literals():
            if v not in literals:
                literals.append(v)
    for value in sorted(literals, key=len, reverse=True):
        out = out.replace(value, _mask_literal(value))

    out = _redact_mnemonics(out)
    for rx, fn in _PATTERNS:
        out = rx.sub(fn, out)
    return out


# ---------------------------------------------------------------------------
# CLI: stdin -> stdout, atau mode berkas
# ---------------------------------------------------------------------------

_USAGE = """pakai:
  python3 redact.py [--sensitive-names A,B,C] [--sensitive-auto] < input > output
  python3 redact.py [--sensitive-names A,B,C] [--sensitive-auto] FILE
  python3 redact.py [--sensitive-names A,B,C] [--sensitive-auto] --in-place FILE
"""


def main(argv: list[str]) -> int:
    names: list[str] = []
    auto = False
    in_place = False
    files: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--sensitive-names":
            i += 1
            if i >= len(argv):
                sys.stderr.write(_USAGE)
                return 2
            names.extend(x.strip() for x in argv[i].split(",") if x.strip())
        elif a == "--sensitive-auto":
            auto = True
        elif a == "--in-place":
            in_place = True
        elif a in ("-h", "--help"):
            sys.stdout.write(_USAGE)
            return 0
        else:
            files.append(a)
        i += 1

    if len(files) > 1 or (in_place and not files):
        sys.stderr.write(_USAGE)
        return 2

    if files:
        try:
            with open(files[0], "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError as e:
            sys.stderr.write(f"log-redact: tidak bisa membaca berkas: {e}\n")
            return 2
        result = redact(data, names, auto)
        if in_place:
            with open(files[0], "w", encoding="utf-8") as f:
                f.write(result)
        else:
            sys.stdout.write(result)
        return 0

    data = sys.stdin.read()
    sys.stdout.write(redact(data, names, auto))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
