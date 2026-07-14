# indraska-trading-bot

Crypto **futures** trading bot dengan arsitektur terpisah: riset/sinyal (LLM) dan eksekusi order (Binance Futures) dijalankan oleh komponen berbeda yang **tidak berbagi kredensial**. Tujuannya: kalau kredensial LLM bocor atau ada bug di riset, akun trading tetap aman.

Default selalu **Binance Futures Testnet** (`BINANCE_TESTNET=true`).

---

## Daftar isi
- [Gambaran arsitektur](#gambaran-arsitektur)
- [Struktur folder & file](#struktur-folder--file)
- [Fitur & API](#fitur--api)
- [Schema data (file-based)](#schema-data-file-based)
- [Tech stack & library](#tech-stack--library)
- [Setup project](#setup-project)
- [Cara run aplikasi](#cara-run-aplikasi)
- [Cara test aplikasi](#cara-test-aplikasi)
- [Keamanan (atururan keras)](#keamanan-aturan-keras)
- [Status & catatan](#status--catatan)

---

## Gambaran arsitektur

Empat subsistem sengaja dipisah, berkomunikasi lewat **file JSON** (bukan koneksi langsung):

```
┌─────────────────┐     signals/latest_signal.json     ┌─────────────────┐
│  vibe-trading/  │  ───────────────────────────────►  │    executor/    │
│  riset & sinyal │   {symbol,action,confidence,...}    │  Binance order  │
│  (GLM via Z.ai) │                                     │  + risk_guard   │
└─────────────────┘                                     └────────┬────────┘
                                                                 │
                                          state.json + order log │
                                                                 ▼
┌─────────────────┐     read signal/state/Binance       ┌─────────────────┐
│   dashboard/    │  ◄───────────────────────────────── │  (file bridge)  │
│  web UI readonly│   toggle kill switch (token)        │                 │
└─────────────────┘                                     └─────────────────┘
```

| Komponen | Bicara ke Binance? | Bisa kirim order? |
|---|---|---|
| `vibe-trading/` | Tidak | **Tidak pernah** |
| `executor/` | Ya (testnet) | **Satu-satunya** |
| `dashboard/` | Hanya read-only (opsional) | **Tidak pernah** |

---

## Struktur folder & file

```
trading-bot/
├── CLAUDE.md                  # aturan proyek (baca sebelum edit executor/dashboard)
├── README.md                  # dokumen ini
├── RUN_REPORT.md              # status build + hasil testnet + langkah deploy
├── .gitignore                 # exclude .env, KILL_SWITCH, state, logs
├── .venv/                     # virtualenv (lokal, tidak di-commit)
│
├── signals/                   # jembatan komunikasi antar komponen (file JSON)
│   ├── latest_signal.json     # sinyal terbaru (di-gitignore)
│   └── .gitkeep
│
├── vibe-trading/              # SUBSISTEM 1 — riset & sinyal
│   ├── generate_signal.py     # provider: Z.ai GLM → vibe-trading-ai → mock
│   ├── generate_signal.sh     # wrapper (load .env, pilih venv python)
│   ├── requirements.txt
│   └── .env.example           # ZAI_API_KEY, ZAI_BASE_URL, ZAI_MODEL
│
├── executor/                  # SUBSISTEM 2 — eksekusi order (satunya yang trade)
│   ├── risk_guard.py          # GATE wajib sebelum setiap order
│   ├── executor.py            # loop poll → validate → place order + SL/TP
│   ├── config.yaml            # leverage/posisi/SL/TP/allowed symbols
│   ├── test_risk_guard.py     # 19 unit test
│   ├── requirements.txt
│   ├── .env.example           # BINANCE_API_KEY/SECRET, BINANCE_TESTNET=true
│   ├── state.json             # runtime state (di-gitignore)
│   └── executor.log           # log runtime (di-gitignore)
│
├── dashboard/                 # SUBSISTEM 3 — web UI read-only
│   ├── app.py                 # FastAPI, bind 127.0.0.1, token-gated kill switch
│   ├── index.html             # vanilla HTML/JS, polling /api/status tiap 3s
│   ├── requirements.txt
│   └── .env.example           # DASHBOARD_TOKEN, optional read-only Binance key
│
├── systemd/                   # unit systemd untuk VPS
│   ├── futures-executor.service
│   ├── vibe-signal.service
│   ├── vibe-signal.timer
│   ├── dashboard.service
│   └── README.md              # cara install + SSH tunnel
│
└── KILL_SWITCH                # file presence = blok semua trading (di-gitignore)
```

### Penamaan file
- `generate_signal.{sh,py}` — generator sinyal.
- `risk_guard.py` — satu class `RiskGuard` dengan method `validate()`.
- `executor.py` — entrypoint daemon (`place_futures_order` = satu-satunya fungsi order).
- `app.py` — app FastAPI.
- `*.example` — template `.env`, **bukan** kredensial nyata.

---

## Fitur & API

### Executor (`executor.py`) — CLI
```bash
python executor.py            # daemon, poll signal tiap 30s
python executor.py --once     # proses satu sinyal lalu exit (testing)
```
- Membaca `signals/latest_signal.json` (cek `run_id` belum diproses).
- `RiskGuard.validate()` → kalau lolos: `place_futures_order()` (MARKET) + SL (`STOP_MARKET`) + TP (`TAKE_PROFIT_MARKET`, `closePosition=True`).
- Margin type ISOLATED, leverage dari config.
- Menolak jalan jika `BINANCE_TESTNET` bukan `true`.

### Signal generator (`generate_signal.sh`)
```bash
./vibe-trading/generate_signal.sh                      # auto (ZAI_API_KEY atau mock)
./vibe-trading/generate_signal.sh --mock               # paksa mock
./vibe-trading/generate_signal.sh --provider zai       # paksa Z.ai GLM
./vibe-trading/generate_signal.sh --symbol ETHUSDT --action SELL
```

### Dashboard API (`dashboard/app.py`, bind `127.0.0.1:8080`)
| Method | Path | Auth | Fungsi |
|---|---|---|---|
| `GET` | `/` | — | UI HTML (`index.html`) |
| `GET` | `/api/status` | — | signal + executor state + kill switch + (opsional) posisi Binance read-only |
| `POST` | `/api/killswitch?enable=true\|false` | `X-Dashboard-Token` header | toggle kill switch (satu-satunya endpoint tulis) |
| `GET` | `/healthz` | — | health check |

Contoh toggle kill switch:
```bash
curl -X POST "http://127.0.0.1:8080/api/killswitch?enable=true" \
     -H "X-Dashboard-Token: $DASHBOARD_TOKEN"
```

> Dashboard **read-only** kecuali kill switch. Tidak ada endpoint yang memanggil fungsi order executor.

---

## Schema data (file-based)

**Tidak ada database relasional.** Semua state disimpan sebagai file JSON — sengaja, agar komponen terpisah dan mudah diaudit.

### `signals/latest_signal.json` (jembatan sinyal)
```jsonc
{
  "symbol": "BTCUSDT",          // harus ada di config.trading.symbols_allowed
  "action": "BUY",              // BUY | SELL | HOLD
  "confidence": 0.75,           // float 0..1, minimal config.risk_guard.min_confidence
  "reason": "...",              // alasan singkat dari LLM/mock
  "run_id": "mock-e2993a6c",    // id unik, dipakai dedup (sekali proses)
  "generated_at": "2026-07-14T11:34:28.897369+00:00"  // ISO 8601; max age dari config
}
```

### `executor/state.json` (state executor)
```jsonc
{
  "processed_run_ids": ["mock-e2993a6c"],   // dedup sinyal
  "trades_today": 1,                         // counter harian (reset per tanggal UTC)
  "trades_date": "2026-07-14",
  "last_order": {                            // ringkasan order terakhir yang sukses
    "symbol": "BTCUSDT", "side": "BUY", "quantity": 0.0003,
    "entry_price": 62826.78, "sl_price": 61570.2, "tp_price": 65339.9,
    "leverage": 1,
    "entry_order_id": 123, "sl_order_id": 124, "tp_order_id": 125,
    "status": "filled", "executed_at": "2026-07-14T...", "run_id": "mock-..."
  }
}
```

### `config.yaml` (konfigurasi executor)
```yaml
binance:
  testnet: true                 # default; env BINANCE_TESTNET menang
executor:
  poll_interval_seconds: 30
  signal_file: signals/latest_signal.json
  state_file: executor/state.json
risk_guard:
  min_confidence: 0.60
  max_signal_age_seconds: 300
  max_daily_trades: 10
  kill_switch_file: KILL_SWITCH
  allowed_actions: [BUY, SELL]
trading:
  leverage: 1                   # KONSERVATIF — butuh angka spesifik dari user untuk dinaikkan
  position_size_usdt: 20
  sl_percent: 2.0
  tp_percent: 4.0
  symbols_allowed: [BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT]
```

### `KILL_SWITCH` (file presence)
- **Ada** = semua trading diblokir (`risk_guard` menolak di `validate()`).
- **Tidak ada** = trading jalan.
- Dibuat/dihapus dari dashboard (token-gated).

---

## Tech stack & library

| Lapisan | Teknologi |
|---|---|
| Bahasa | Python 3.11+ (dites di 3.12) |
| LLM riset | Z.ai GLM (Coding Plan), endpoint OpenAI-compatible |
| Eksekusi | Binance Futures Testnet |
| Web UI | FastAPI + Uvicorn, HTML/JS vanilla (tanpa build step/npm) |
| Deployment | systemd services + SSH tunnel (dashboard localhost-only) |

### Library (`requirements.txt` per komponen)
| Library | Pemakaian |
|---|---|
| `python-binance` | klien Binance Futures (executor + dashboard read-only) |
| `python-dotenv` | load `.env` |
| `pyyaml` | baca `config.yaml` |
| `requests` | panggil Z.ai GLM |
| `fastapi` + `uvicorn[standard]` | dashboard |
| `pytest` | unit test |

> Catatan: CLAUDE.md menyebut package `vibe-trading-ai`. Dependency tree-nya berat, jadi `generate_signal.py` memanggil **Z.ai GLM langsung** lewat endpoint OpenAI-compatible (`ZAI_BASE_URL`) dengan fallback deterministic mock. Lihat `RUN_REPORT.md`.

---

## Setup project

```bash
git clone git@github.com:indras87/indraska-trading-bot.git
cd indraska-trading-bot

# 1. Virtualenv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip

# 2. Install deps semua komponen
.venv/bin/pip install -r executor/requirements.txt
.venv/bin/pip install -r dashboard/requirements.txt
.venv/bin/pip install pyyaml requests
.venv/bin/pip install pytest      # untuk test

# 3. Isi .env (SALIN dari .env.example, lalu edit di terminal — JANGAN commit)
cp executor/.env.example executor/.env       # isi key TESTNET 64-char + BINANCE_TESTNET=true
cp dashboard/.env.example dashboard/.env     # isi DASHBOARD_TOKEN kuat
cp vibe-trading/.env.example vibe-trading/.env  # opsional: ZAI_API_KEY untuk sinyal GLM asli
```

**Wajib:**
- `executor/.env` → key dari https://testnet.binancefuture.com (64 char alfanumerik, **tanpa** spasi/`-`), `BINANCE_TESTNET=true`.
- `dashboard/.env` → `DASHBOARD_TOKEN` string acak panjang.

---

## Cara run aplikasi

### Jalankan satu siklus penuh (lokal, testnet)
```bash
# 1. Buat sinyal
./vibe-trading/generate_signal.sh --mock

# 2. Eksekusi (proses satu sinyal, lalu exit)
.venv/bin/python executor/executor.py --once

# 3. Lihat hasil
cat executor/state.json          # cek last_order
tail -20 executor/executor.log   # log order
```

### Jalankan sebagai daemon (lokal)
```bash
.venv/bin/python executor/executor.py        # poll tiap 30s
```

### Jalankan dashboard
```bash
DASHBOARD_TOKEN=rahasia .venv/bin/uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
# buka http://127.0.0.1:8080
```

### Deploy di VPS (systemd)
Lihat `systemd/README.md` — install unit, enable, lalu akses dashboard via SSH tunnel:
```bash
ssh -L 8080:127.0.0.1:8080 user@vps   # di laptop, lalu buka http://127.0.0.1:8080
```

---

## Cara test aplikasi

### Unit test `risk_guard.py`
```bash
.venv/bin/python -m pytest executor/test_risk_guard.py -v
```
Menguji semua kondisi blok: kill switch, sinyal stale, confidence rendah, action tidak diizinkan (`HOLD`), symbol tidak diizinkan, `run_id` sudah diproses, batas harian, field invalid — plus kasus lolos & boundary.

### Test dashboard (smoke)
```bash
# status
curl http://127.0.0.1:8080/api/status

# kill switch TANPA token → harus 401
curl -X POST "http://127.0.0.1:8080/api/killswitch?enable=true"

# kill switch DENGAN token → 200
curl -X POST "http://127.0.0.1:8080/api/killswitch?enable=true" \
     -H "X-Dashboard-Token: $DASHBOARD_TOKEN"
```

### Test end-to-end testnet
```bash
./vibe-trading/generate_signal.sh --mock
.venv/bin/python executor/executor.py --once
# sukses = executor.log muncul "entry order filled", "SL placed", "TP placed"
#          dan dashboard /api/status menampilkan last_order + posisi
```

---

## Keamanan (aturan keras)

Ringkas dari `CLAUDE.md`:
1. **Default testnet.** `BINANCE_TESTNET=true` wajib; executor menolak jalan jika bukan true.
2. **`risk_guard.py` = gerbang wajib.** Setiap path yang ke `futures_create_order` harus lewat `validate()`.
3. **Kill switch tidak boleh dihapus** dari validasi.
4. **Leverage tidak dinaikkan** tanpa angka spesifik dari user.
5. **`.env` tidak pernah di-commit** (di-gitignore). Key dashboard **harus read-only**, terpisah dari key executor.
6. **Dashboard bind `127.0.0.1` saja** — akses lewat SSH tunnel. Toggle kill switch butuh token.
7. Dashboard **tidak boleh** memanggil fungsi order executor.

---

## Status & catatan

- Lihat **`RUN_REPORT.md`** untuk status build terbaru, hasil testnet, dan langkah deploy.
- Pemisahan `vibe-trading-ai` (Z.ai) dari `executor` (Binance) adalah keputusan arsitektur disengaja demi isolasi kredensial.
- Sinyal default pakai **mock** sampai `ZAI_API_KEY` di-set di `vibe-trading/.env`.
