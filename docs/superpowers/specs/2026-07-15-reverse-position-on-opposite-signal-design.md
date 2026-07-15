# Reverse Position on Opposite Signal — Design

**Tanggal:** 2026-07-15
**Status:** Disetujui (tanpa review lanjutan, sesuai permintaan user)
**Scope:** `executor/` (menyentuh `place_futures_order`, `store.py`, `config.yaml`)

## Ringkasan eksekutif

Saat ini bot hanya reaktif terhadap sinyal: membaca `signals/latest_signal.json`, memvalidasi via `risk_guard`, lalu membuka posisi baru dengan SL/TP otomatis. Bot **tidak** memantau arah market secara aktif dan **tidak** membalik (reverse) posisi yang sedang open. Akibatnya, jika posisi long sedang open dan scanner mengeluarkan sinyal SELL untuk symbol yang sama, executor akan **membuka posisi SELL baru** — menghasilkan posisi hedge (long + short simultan pada symbol sama), bukan reversal.

Spesifikasi ini menambahkan logika **reverse otomatis**: jika sinyal baru berlawanan arah dengan posisi bot yang sedang open pada symbol yang sama, executor menutup posisi lama, lalu membuka posisi lawan. Implementasi satu-arah (one-way position mode).

## Keputusan yang dikunci (klarifikasi user)

| Hal | Keputusan | Alasan |
|------|-----------|--------|
| Position mode | One-way | Satu posisi per symbol; reversal = tutup lalu buka lawan. Paling sederhana dan aman. |
| Sinyal searah (posisi open sudah ada) | Abaikan | Tidak menambah exposure/ukuran, tidak memperbesar risiko. |
| Tutup berhasil, buka lawan gagal | Flat + log | Posisi kosong, aman. Tidak auto-retry/recovery otomatis. |
| Cakupan reverse | Hanya posisi yang bot buka sendiri | Posisi manual user di UI Binance tidak disentuh. |

## Aturan kepatuhan (CLAUDE.md)

Perubahan ini menyentuh `place_futures_order`, `store.py`, dan `config.yaml`. Sesuai aturan repo:

1. Setiap path yang berakhir di `client.futures_create_order(...)` **wajib** lewat `RiskGuard.validate()` terlebih dahulu. **Tidak ada jalur baru yang melewati risk_guard.**
2. Kill switch (`KILL_SWITCH` file) tetap dicek pertama oleh `validate()` — tidak dihapus dari flow reverse.
3. `leverage`, `position_size_usdt`, `sl_percent`, `tp_percent` **tidak diubah**.
4. Default `BINANCE_TESTNET=true` tidak diubah.
5. Verifikasi wajib jalan di **testnet** dengan bukti di `RUN_REPORT.md` (tanpa menyertakan isi `.env`).

## Arsitektur

### Komponen baru di `executor/executor.py`

1. **`get_open_bot_position(client, logger, config, symbol) -> dict | None`**

   - READ-ONLY. Sumber kebenaran posisi = broker: `client.futures_position_information(symbol=symbol)`, filter entri dengan `abs(positionAmt) > 0`.
   - Cross-check DB (`store.py`): pastikan ada order row untuk symbol tersebut dengan status OPEN. Jika broker punya posisi tetapi DB tidak memiliki row OPEN → itu **posisi manual** → return `None` (skip, tidak direverse).
   - Arah ditentukan dari tanda `positionAmt`: `> 0` → LONG, `< 0` → SHORT.
   - Return:
     ```python
     {
       "direction": "LONG" | "SHORT",
       "qty": float(abs(positionAmt)),
       "entry_order_id": ...,
       "sl_id": ...,
       "tp_id": ...,
     }
     ```
     atau `None`.

2. **`close_position(client, logger, config, symbol, qty, side, sl_id=None, tp_id=None) -> dict`**

   - Cancel SL/TP conditional order lama: by id bila diketahui, fallback `futures_cancel_all_open_orders(symbol=symbol)`.
   - MARKET order `reduceOnly=True`, side = lawan dari posisi (long ditutup dengan SELL, short ditutup dengan BUY), qty = posisi penuh.
   - Return hasil: `{close_order_id, avg_fill_price, status, ...}`.
   - Raise exception bila gagal (ditangkap pemanggil).

3. **`reverse_position(client, logger, config, signal, existing) -> dict`**

   - Orkestrasi: `close_position(old)` → `place_futures_order(new)`.
   - Error handling terperinci lihat seksi **Error handling**.
   - Return hasil serupa `place_futures_order` + info close.

### Modifikasi `process_signal`

Flow baru (dipanggil sesudah `validate()` lolos):

```
validate(signal)
  └─ BLOCKED → return {accepted: False}

existing = get_open_bot_position(client, logger, config, symbol)
  ├─ None                 → jalur normal: place_futures_order  (entry baru)
  ├─ existing.direction
  │      == signal.action → BLOCKED reason "same_direction_position_open"
  │                        return {accepted: False}   (tidak order, tidak tambah)
  └─ existing.direction
         != signal.action → reverse_position(client, logger, config, signal, existing)
                            mark old row CLOSED (exit_type=REVERSED) di store
                            state.trades_today += 1
                            processed_run_ids.append(run_id)
```

`reverse_position` memanggil `place_futures_order` di dalamnya — sehingga **seluruh jalur order tetap melalui validasi yang sudah lewat**, dan tidak membuka bypass baru. Risk guard tetap murni (tidak butuh akses client/broker).

## Data flow

```
generate_signal.sh (scanner)
  → signals/latest_signal.json {symbol, action, confidence, run_id, generated_at, ...}

executor.py main loop (poll 30s)
  → read_signals()
  → per signal:
       risk_guard.validate()  → BLOCKED? skip
       get_open_bot_position(symbol)
         ├─ none        → entry baru
         ├─ same dir    → skip (same_direction_position_open)
         └─ opposite    → reverse:
              close_position (cancel SL/TP lama + reduceOnly MARKET)
              place_futures_order (entry baru + SL/TP baru)
              store.mark_closed(old, exit_type="REVERSED")
       state update + save_state
  → reconcile_exits (read-only, sudah ada)
```

## State / DB (`store.py`)

- Method baru: `mark_reversed(symbol, old_entry_order_id, closed_at, realized_pnl=None)` — atau reuse `mark_closed(...)` dengan menambah nilai enum exit_type `REVERSED`.
- Kolom/field `exit_type` yang sudah ada (digunakan oleh `reconcile_exits`) diperluas himpunan nilainya: `{TP, SL, MANUAL, UNKNOWN}` → tambah `REVERSED`.
- **Penghitungan daily trade:** 1 reverse = **1 trade baru** (yang dihitung adalah pembukaan posisi lawan). Penutupan posisi lama tidak dihitung terpisah terhadap `max_daily_trades`.
- `processed_run_ids` tetap dicatat agar sinyal lawan arah yang sama (run_id sama) tidak diproses dua kali.

## Config (`config.yaml`)

```yaml
trading:
  enable_reversal: true   # default ON sesuai permintaan user; kill switch tetap backstop utama
```

- Hanya satu field baru. `leverage` (10), `position_size_usdt` (60), `sl_percent` (2.0), `tp_percent` (4.0) **tidak diubah**.
- Bila `enable_reversal: false`, sinyal lawan arah saat posisi open ditangani seperti perilaku lama (membuka hedge) — namun default true.
  - Catatan: untuk benar-benar menonaktifkan risiko hedge, saat `enable_reversal: false` sinyal lawan arah pada symbol yang punya posisi open bot sebaiknya **diblok** (same pattern dengan same-direction). Detail ini diserahkan ke implementasi: default konservatif = block, bukan hedge.

## Error handling

| Skenario | Perilaku |
|----------|----------|
| Kill switch aktif | `validate()` block duluan — tidak sampai reverse. |
| Close gagal | Posisi lama tetap utuh, return error, log. Tidak lanjut open. Sinyal tetap ditandai processed (atau tidak — lihat catatan bawah). |
| Close berhasil, open gagal | **Flat + log** (keputusan user). Posisi kosong. Tidak auto-retry. |
| Network/API error | Ditangkap oleh `except Exception` di main loop, loop lanjut, tidak crash. |
| Posisi manual terdeteksi | `get_open_bot_position` return `None`, diperlakukan sebagai entry baru. |

**Catatan penanda processed_run_ids pada kegagalan:** bila close gagal, run_id sebaiknya **tidak** ditandai processed agar sinyal bisa dicoba ulang di batch berikutnya. Bila close berhasil tetapi open gagal (flat), run_id **ditandai** processed agar tidak retry berulang (posisi sudah flat, aman). Detail flag return (`placed`, `reversed`, `flat_on_failure`) ditetapkan di implementasi.

## Testing & verifikasi

### Unit test
- `executor/test_reverser.py` dengan client mock. Kasus:
  1. Tidak ada posisi open → entry normal.
  2. Posisi searah dengan sinyal → block (`same_direction_position_open`).
  3. Posisi lawan arah → reverse: verify close dipanggil dengan `reduceOnly=True` + side lawan, lalu open.
  4. Close gagal → posisi lama tetap, open tidak dipanggil.
  5. Close OK, open gagal → flat, error tercatat.
  6. Posisi broker ada tapi tidak ada row OPEN di DB (manual) → diperlakukan entry baru.
- Unit test existing (`test_risk_guard.py`, `test_store.py`) tetap hijau — risk_guard tidak diubah perilakunya.

### Verifikasi testnet (Definition of Done)
1. Set akun testnet ke position mode **one-way**.
2. `BINANCE_TESTNET=true`.
3. Buat skenario: trigger entry LONG via bot (sinyal BUY) → konfirmasi posisi + SL/TP kebuka.
4. Kirim sinyal SELL pada symbol yang sama (run_id baru) → konfirmasi:
   - Posisi long tertutup (order reduceOnly terisi).
   - Posisi short kebuka (entry baru).
   - SL/TP baru terpasang untuk short.
   - Row lama ditandai `exit_type=REVERSED` di DB.
5. Verifikasi sinyal searah saat posisi open → diblok, tidak nambah posisi.
6. Tulis bukti (order id testnet, harga, **tanpa isi `.env`**) ke `RUN_REPORT.md`.

## Yang TIDAK termasuk scope (YAGNI)

- Trailing stop / market-direction monitor aktif. (Reverse hanya reaktif terhadap sinyal baru.)
- Auto-retry / recovery posisi pada kegagalan.
- Partial close / pyramiding. (Same-direction diabaikan.)
- Reverse posisi manual user.
- Dashboard: tidak ada endpoint baru. Reverse hanya terlihat sebagai order di data yang sudah ada.

## File yang berubah

- `executor/executor.py` — tambah `get_open_bot_position`, `close_position`, `reverse_position`; modifikasi `process_signal`.
- `executor/store.py` — tambah method tandai reversed / perluas enum `exit_type`.
- `executor/config.yaml` — tambah `trading.enable_reversal`.
- `executor/test_reverser.py` — baru.
- `RUN_REPORT.md` — bukti verifikasi testnet.
- `README.md` — update cara kerja (bila menyentuh flow pengguna).

## Langkah berikutnya

Spesifikasi ini adalah dokumen perencanaan. Implementasi (rincian langkah kode) akan disusun melalui `writing-plans` skill saat user siap, dengan Plan Mode + approval eksplisit sebelum menulis kode (sesuai CLAUDE.md).
