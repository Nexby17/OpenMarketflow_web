#!/usr/bin/env python3
"""Watchdog for of-collector — restarts collector if data stream stalls.

Monitors of-collector journal logs. If no new trades/OB updates for N seconds,
restarts the service.

Run as systemd service (of-collector-watchdog.service).
"""
import subprocess
import time
import sys

STALL_TIMEOUT = 60   # seconds without new data → restart
CHECK_INTERVAL = 30  # check every 30 sec
MIN_RATE_TRADES = 1   # min new trades per check (market hours)
MIN_RATE_OB = 1      # min new OB updates per check (market hours)
SERVICE = "of-collector.service"

def get_last_stats():
    """Parse last Stats line from journal."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", SERVICE, "--since", "5 minutes ago", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")
        for line in reversed(lines):
            if "Stats:" in line:
                # Extract trades and ob_updates counts
                parts = line.split("Stats:")[1].strip()
                # trades=199 ob_updates=3474
                trades = int(parts.split("trades=")[1].split()[0])
                obs = int(parts.split("ob_updates=")[1].split()[0])
                return trades, obs
        return None, None
    except:
        return None, None

def restart_service():
    """Restart the collector service."""
    print(f"[watchdog] Restarting {SERVICE}...")
    subprocess.run(["systemctl", "restart", SERVICE], timeout=30)
    time.sleep(15)
    print(f"[watchdog] {SERVICE} restarted")

def main():
    print(f"[watchdog] Monitoring {SERVICE} (stall={STALL_TIMEOUT}s, check={CHECK_INTERVAL}s)")
    last_trades = None
    last_obs = None
    last_change = time.time()
    last_ob_change = time.time()

    while True:
        trades, obs = get_last_stats()

        if trades is not None:
            if last_trades is not None and trades == last_trades:
                # No change — check timeout
                stalled = time.time() - last_change
                if stalled > STALL_TIMEOUT:
                    print(f"[watchdog] STALL DETECTED: trades={trades} unchanged for {stalled:.0f}s")
                    restart_service()
                    last_trades = None
                    last_obs = None
                    last_change = time.time()
                    continue
            else:
                if last_trades is not None:
                    d_trades = trades - last_trades
                    d_obs = (obs - last_obs) if last_obs is not None else 999
                    print(f"[watchdog] OK: trades={trades} (+{d_trades}) ob={obs} (+{d_obs})")
                    # Check if OB stalled (trades moving but OB not)
                    if d_trades > 0 and d_obs == 0 and last_obs is not None:
                        ob_stall_time = time.time() - last_ob_change
                        if ob_stall_time > STALL_TIMEOUT:
                            print(f"[watchdog] OB STALL: ob_updates={obs} unchanged for {ob_stall_time:.0f}s")
                            restart_service()
                            last_trades = None
                            last_obs = None
                            last_change = time.time()
                            last_ob_change = time.time()
                            continue
                    else:
                        last_ob_change = time.time()
                else:
                    print(f"[watchdog] OK: trades={trades} ob={obs}")
                last_trades = trades
                last_obs = obs
                last_change = time.time()
        else:
            print(f"[watchdog] No stats found — collector may be starting")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
