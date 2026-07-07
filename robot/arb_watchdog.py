#!/usr/bin/env python3
"""Watchdog for arb-robot — restarts if data stream stalls (FinamPy I/O hang).

Monitors /status API for lastDataTs. If no data for N seconds during market hours,
restarts arb-robot.service. State (PnL, positions) persists via arb_state.json.

Run as systemd service (arb-watchdog.service).
"""
import subprocess
import json
import time
import sys
import os
import urllib.request
import logging
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s [arb-watchdog] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('arb_watchdog')

STALL_TIMEOUT = int(os.environ.get('STALL_TIMEOUT', '120'))
STALL_OUTSIDE = int(os.environ.get('STALL_OUTSIDE', '600'))
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', '30'))
SERVICE = os.environ.get('ARB_WATCHDOG_SERVICE', 'arb-robot.service')
API_URL = os.environ.get('ARB_WATCHDOG_URL', 'http://localhost:5090/status')
START_URL = API_URL.rsplit('/status', 1)[0] + '/start'

MSK = timezone(timedelta(hours=3))


def is_market_hours():
    """Check if MOEX is in main or evening session (MSK time)."""
    now = datetime.now(MSK)
    t = now.hour * 60 + now.minute
    # Main session: 10:00–18:45
    if 600 <= t <= 1125:
        return True
    # Evening session: 19:00–23:50
    if 1140 <= t <= 1430:
        return True
    return False


def get_status():
    try:
        with urllib.request.urlopen(API_URL, timeout=5) as resp:
            return json.load(resp)
    except Exception as e:
        log.error(f"API error: {e}")
        return None


def restart_service():
    log.warning(f"Restarting {SERVICE}...")
    subprocess.run(["systemctl", "restart", SERVICE], timeout=30)
    # Wait for API to come back up
    for i in range(12):
        time.sleep(5)
        try:
            with urllib.request.urlopen(API_URL, timeout=3) as resp:
                break
        except Exception:
            log.debug(f"Waiting for API... ({(i+1)*5}s)")
    # Restore running mode if was running before
    try:
        urllib.request.urlopen(START_URL, timeout=5, data=b'')
        log.info("Trading resumed")
    except Exception as e:
        log.error(f"Resume error: {e}")
    return time.time()  # return timestamp of restart


def main():
    log.info(f"Monitoring {SERVICE} (stall={STALL_TIMEOUT}s market, {STALL_OUTSIDE}s outside)")
    log.info(f"API: {API_URL}")

    last_restart = 0
    GRACE_PERIOD = 120  # 2 min grace after restart

    while True:
        try:
            # Grace period — skip checks right after restart
            if time.time() - last_restart < GRACE_PERIOD:
                log.debug(f"Grace period ({time.time()-last_restart:.0f}s/{GRACE_PERIOD}s)")
                time.sleep(CHECK_INTERVAL)
                continue

            status = get_status()

            if status is None:
                # API unreachable — robot may be completely dead
                log.warning("API unreachable — checking service...")
                result = subprocess.run(
                    ["systemctl", "is-active", SERVICE],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip() != "active":
                    log.warning(f"Service not active: {result.stdout.strip()} — not restarting (systemd handles)")
                else:
                    log.warning("Service active but API dead — restarting")
                    last_restart = restart_service()
                time.sleep(CHECK_INTERVAL)
                continue

            last_ts = status.get("lastDataTs", 0)
            mode = status.get("mode", "stopped")

            now = time.time()
            age = now - last_ts if last_ts > 0 else float('inf')

            # If robot is stopped/paused, no need to check data freshness
            if mode in ("stopped", "paused"):
                log.info(f"Robot {mode} — skipping data check")
                time.sleep(CHECK_INTERVAL)
                continue

            # Determine timeout based on market hours
            timeout = STALL_TIMEOUT if is_market_hours() else STALL_OUTSIDE

            if age > timeout:
                log.warning(f"DATA STALL: lastData={age:.0f}s ago (timeout={timeout}s) → RESTART")
                last_restart = restart_service()
            else:
                log.info(f"OK: dataAge={age:.0f}s mode={mode} market={is_market_hours()}")

        except Exception as e:
            log.error(f"Check error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
