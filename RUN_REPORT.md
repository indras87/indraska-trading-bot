# RUN_REPORT — Trading Bot Build

**Tanggal:** 2026-07-14 (UTC)
**Mode:** headless / automated
**Testnet:** `BINANCE_TESTNET=true` (dipaksakan di kode; env var menang)

---

## 1. Yang sudah selesai

| Komponen | Status | Bukti |
|---|---|---|
| `.gitignore` (exclude `.env`, `KILL_SWITCH`, state, logs) | ✅ | `git check-ignore executor/.env` → ignored |
| `executor/risk_guard.py` + 19 unit test | ✅ | `pytest executor/test_risk_guard.py` → **19 passed** |
| `executor/executor.py` (loop, validate, order+SL/TP, state) | ✅ | import OK, loop jalan di testnet |
| `executor/config.yaml`, `requirements.txt`, `.env.example` | ✅ | `BINANCE_TESTNET=true` default |
| `vibe-trading/generate_signal.{sh,py}` | ✅ | mock signal tertulis ke `signals/latest_signal.json` |
| `dashboard/app.py` + `index.html` (localhost-only, token kill switch) | ✅ | smoke test: status 200, no-token 401, valid-token 200 |
| `systemd/` (executor, signal timer, dashboard) + README | ✅ | commit `b041a74` |
| Git commits per komponen | ✅ | 6 commit, `.env` tidak pernah ter-stage |

### Arsitektur dipatuhi (CLAUDE.md)
- vibe-trading **tidak** panggil order. Hanya tulis `signals/latest_signal.json`.
- executor **satu-satunya** yang bicara ke Binance Futures. Setiap order lewat `RiskGuard.validate()` dulu — tidak ada jalur bypass.
- Kill switch dicek di `validate()`, pertama, selalu.
- Dashboard bind `127.0.0.1` saja, read-only kecuali toggle kill switch (token-gated).
- Tidak ada secret di kode; `.env` git-ignored; tidak pernah di-print/echo ke file manapun.

---

## 2. Hasil test end-to-end — ✅ SIKLUS PENUH TERVERIFIKASI DI TESTNET

User mengganti key ke **Binance Futures Demo/Testnet** (`https://testnet.binancefuture.com`). Setelah 2 perbaikan kecil (lihat §4), satu siklus penuh terbukti jalan:

```
generate_signal.sh --mock
  → signals/latest_signal.json {symbol:BTCUSDT, action:BUY, confidence:0.75,
                                 run_id:mock-70f05642, generated_at:...}
executor.py --once
  → load executor/.env (testnet=true) ✓
  → init client + PREFLIGHT futures_account() AUTH OK ✓
  → RiskGuard.validate() → ACCEPTED ✓ (fresh, conf 0.75 ≥ 0.60)
  → leverage set BTCUSDT 1x, margin ISOLATED ✓
  → MARKET BUY filled  orderId=21675461475 ✓
  → SL (STOP_MARKET, closePosition) algo order placed  id=1000000135627041 @62453.7 ✓
  → TP (TAKE_PROFIT_MARKET, closePosition) algo order placed id=1000000135627044 @66277.4 ✓
verifikasi:
  → position BTCUSDT 0.0009 @63717.6 lev 1, uPnL +0.004 ✓
  → duplikat SL place → -4130 (konfirmasi algo order benar-benar ada) ✓
dashboard /api/status → menampilkan signal + last_order + state ✓
dashboard kill switch: tanpa token 401, dengan token 200 ✓
```

Log order testnet (`executor/executor.log`, **tanpa isi `.env`**):
```
2026-07-14 19:49:46 INFO Binance Futures client ready + auth OK (testnet=True, url=https://testnet.binancefuture.com/fapi/v1/)
2026-07-14 19:49:46 INFO ACCEPTED run_id=mock-70f05642 — placing order
2026-07-14 19:49:46 INFO leverage set BTCUSDT 1x
2026-07-14 19:49:46 INFO ORDER BUY BTCUSDT qty=0.0009 entry~63728.23824275 SL=62453.7 TP=66277.4
2026-07-14 19:49:47 INFO entry order filled orderId=21675461475 status=NEW
2026-07-14 19:49:47 INFO SL placed id=1000000135627041 stopPrice=62453.7 status=NEW
2026-07-14 19:49:47 INFO TP placed id=1000000135627044 stopPrice=66277.4 status=NEW
2026-07-14 19:49:47 INFO DONE run_id=mock-70f05642 entry=21675461475 SL=1000000135627041 TP=1000000135627044
```

`executor/state.json` `last_order` (ringkasan order terakhir):
```json
{
  "symbol": "BTCUSDT", "side": "BUY", "quantity": 0.0009,
  "entry_price": 63728.24, "sl_price": 62453.7, "tp_price": 66277.4,
  "leverage": 1,
  "entry_order_id": 21675461475,
  "sl_order_id": 1000000135627041, "tp_order_id": 1000000135627044,
  "status": "filled", "run_id": "mock-70f05642"
}
```

> Posisi tes ditutup kembali setelah verifikasi supaya testnet bersih.

---

## 3. Leverage & ukuran posisi — perlu konfirmasi user

`config.yaml`:
```yaml
trading:
  leverage: 1            # 1x, risiko minimal
  position_size_usdt: 60 # lihat catatan di bawah
  sl_percent: 2.0
  tp_percent: 4.0
```

- `leverage: 1` — konservatif. **Tidak dinaikkan** tanpa angka spesifik dari user.
- `position_size_usdt: 60` — **bukan** kenaikan risiko, tapi **floor exchange**. Binance Futures `MIN_NOTIONAL` BTCUSDT = 50 USDT; 20 (default awal) ditolak `-4164`. 60 dipilih supaya di atas floor + aman dari rounding. Kalau cuma trading symbol dengan min lebih kecil (ETH=20, SOL/BNB/XRP=5), boleh turunkan ke angka di atas min symbol tersebut. Beri angka spesifik kalau mau diubah.

---

## 4. Catatan implementasi & perbaikan selama e2e

- **Preflight auth-check** (`init_client`): `futures_ping()` publik → key buruk cuma muncul saat order dengan `-2015` cryptic. Ditambah `futures_account()` read-only di startup; gagal cepat dengan pesan jelas (spot-testnet vs futures-testnet key, izin, IP).
- **Schema algo/conditional order (Binance Futures Demo):** SL/TP (`STOP_MARKET`/`TAKE_PROFIT_MARKET`) di testnet ini dikembalikan sebagai algo order dengan field `algoId`/`triggerPrice`/`algoStatus`, BUKAN `orderId`/`stopPrice`/`status`. Awalnya executor log `id=None` (seolah gagal/silent) padahal order benar-benar dibuat (terbukti `-4130` saat duplikat & `algoId` di response). Fix: helper `_cond_id`/`_cond_status`/`_cond_stop` baca kedua schema. Algo order juga **tidak muncul** di `futures_get_open_orders`/`get_all_orders` di testnet ini — itu quirk testnet, order tetap aktif.
- **Z.ai / vibe-trading-ai:** CLAUDE.md menyebut package `vibe-trading-ai`; dependency tree-nya berat & install hang. `generate_signal.py` memanggil **Z.ai GLM langsung** lewat endpoint OpenAI-compatible (`ZAI_BASE_URL`) + fallback deterministic mock. E2e pakai mock karena `ZAI_API_KEY` belum di-set (tidak boleh saya isi otomatis). Untuk sinyal GLM asli, set `ZAI_API_KEY` di `vibe-trading/.env`.
- **SDK:** pakai `python-binance` (community). Binance juga punya connector resmi (`binance-futures-connector-python`) — tidak dipakai karena `python-binance` sudah cukup & terverifikasi jalan di testnet.
- **Dashboard Binance read-only:** `dashboard/app.py` opsional fetch posisi live jika `DASHBOARD_BINANCE_API_KEY/SECRET` (key terpisah read-only) di-set. Tanpa itu, dashboard jalan dari `executor/state.json` + `signals/latest_signal.json`.
- **`state.json`/log tidak ter-stage:** runtime state & log di-gitignore.

---

## 5. Langkah deploy ke VPS

1. Clone repo ke VPS (mis. `/opt/trading-bot`).
2. Buat venv + install:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r executor/requirements.txt
   .venv/bin/pip install -r dashboard/requirements.txt
   .venv/bin/pip install pyyaml python-dotenv
   ```
3. Isi (di terminal VPS, langsung):
   - `executor/.env` — key testnet 64-char + `BINANCE_TESTNET=true`
   - `dashboard/.env` — `DASHBOARD_TOKEN` (string acak panjang), opsional read-only Binance key
   - `vibe-trading/.env` — `ZAI_API_KEY` (opsional, untuk sinyal GLM asli)
4. Tes lokal dulu: `./vibe-trading/generate_signal.sh --mock` lalu `.venv/bin/python executor/executor.py --once`. Pastikan order testnet sukses.
5. Install systemd (`systemd/README.md`): edit path & `User=`, copy ke `/etc/systemd/system/`, `daemon-reload`, enable.
6. Akses dashboard via SSH tunnel: `ssh -L 8080:127.0.0.1:8080 user@vps` → `http://127.0.0.1:8080`.
7. Uji kill switch dari dashboard (token) → executor harus berhenti menerima order.

---

## 6. Open items / pertanyaan untuk user (dicatat, tidak memblokir)
- [x] **API key testnet** — SUDAH valid (Futures Demo), e2e terverifikasi.
- [ ] Konfirmasi leverage & position size (sekarang 1x / 60 USDT) — beri angka spesifik kalau ingin diubah.
- [ ] Set `DASHBOARD_TOKEN` kuat di `dashboard/.env` (lihat **§7 peringatan**: token saat ini tertulis di `dashboard/.env.example` yang ter-track — pindahkan ke `.env`).
- [ ] Set `ZAI_API_KEY` di `vibe-trading/.env` kalau mau sinyal GLM asli (bukan mock).

---

## 7. ⚠️ Peringatan: token di `dashboard/.env.example`

`dashboard/.env.example` (file yang **ter-track** di git) saat ini berisi nilai `DASHBOARD_TOKEN` yang terlihat seperti token **asli**, bukan placeholder. File `.example` seharusnya cuma berisi template (`change_me_...`), karena akan ter-commit ke repo publik.

**Yang HARUS user lakukan sebelum push/commit:**
1. Pindahkan nilai token asli ke `dashboard/.env` (di-gitignore).
2. Kembalikan `dashboard/.env.example` ke placeholder (`DASHBOARD_TOKEN=change_me_to_a_long_random_string`).
3. Kalau repo ini pernah di-push dengan token asli di `.example`, **regenerate token** itu (anggap sudah bocor).

Saya **tidak** commit perubahan `dashboard/.env.example` dan **tidak** meng-editnya diam-diam — butuh keputusan user karena ini menyentuh kredensial.
