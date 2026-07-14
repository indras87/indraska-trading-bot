# systemd units

Three components. Edit paths (`/opt/trading-bot`) and `User=` to match your VPS.

```bash
# Copy to systemd
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start
sudo systemctl enable --now futures-executor.service
sudo systemctl enable --now vibe-signal.timer
sudo systemctl enable --now dashboard.service

# Check
systemctl status futures-executor.service
systemctl list-timers vibe-signal.timer
journalctl -u futures-executor.service -f
```

### Dashboard access (localhost-only, via SSH tunnel)

```bash
# On your laptop:
ssh -L 8080:127.0.0.1:8080 user@your-vps
# Then open http://127.0.0.1:8080
```

### Files
- `futures-executor.service` — daemon, polls signals every 30s, testnet-forced.
- `vibe-signal.service` + `vibe-signal.timer` — runs `generate_signal.sh` every 5 min.
- `dashboard.service` — uvicorn bound to `127.0.0.1:8080`.
