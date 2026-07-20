#!/usr/bin/env python3
"""Watchdog for of-robot — restarts robot if price stream stalls.

Monitors /status API. If price hasn't updated for N seconds, restarts the service.
Also restores correct parameters from of_config.json after restart.
"""
import subprocess
import json
import time
import sys
import os
import urllib.request
import logging
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s [watchdog] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('robot_watchdog')

MSK = timezone(timedelta(hours=3))

STALL_TIMEOUT = 120   # 2 min without price change → restart
CHECK_INTERVAL = 30   # check every 30 sec
API_TIMEOUT = 15      # was 5 — too aggressive
RESTART_COOLDOWN = 300 # 5 min cooldown
SERVICE = "of-robot.service"
API_URL = "http://localhost:5080/status"
START_URL = "http://localhost:5080/start"
PARAMS_URL = "http://localhost:5080/params"
CONFIG_PATH = "/root/.openclaw/workspace/HedgeFund/robot/of_config.json"

def _is_night() -> bool:
    """Market closed: 23:50–07:00 MSK."""
    msk = datetime.now(MSK)
    return (msk.hour >= 23 and msk.minute >= 50) or msk.hour < 7

_last_restart_ts = 0.0

def _restart_cooldown_active() -> bool:
    """Prevent rapid restart loops."""
    return (time.time() - _last_restart_ts) < RESTART_COOLDOWN

def _mark_restarted():
    global _last_restart_ts
    _last_restart_ts = time.time()

def get_status():
    try:
        with urllib.request.urlopen(API_URL, timeout=API_TIMEOUT) as resp:
            return json.load(resp)
    except Exception as e:
        log.error(f"API error: {e}")
        return None

def restart_and_recover():
    log.info(f"Restarting {SERVICE}...")
    subprocess.run(["systemctl", "restart", SERVICE], timeout=30)
    time.sleep(15)

    # Restore params from config but do NOT force mode=running
    # — if robot was stopped before stall, keep it stopped
    try:
        # Save correct params but don't change mode
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                config = json.load(f)
            req = urllib.request.Request(
                PARAMS_URL,
                data=json.dumps(config).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
            log.info("Params restored")
    except Exception as e:
        log.warning(f"Param restore skipped: {e}")

    # Check current mode before deciding whether to start
    try:
        status = get_status()
        if status and status.get("mode") != "running":
            log.info(f"Robot is in '{status.get('mode')}' mode — NOT auto-starting")
            return
    except Exception:
        pass

    try:
        urllib.request.urlopen(START_URL, timeout=5)
        log.info("Trading started")
    except Exception as e:
        log.error(f"Recovery error: {e}")

def main():
    log.info(f"Monitoring {SERVICE} (stall={STALL_TIMEOUT}s, check={CHECK_INTERVAL}s, api_timeout={API_TIMEOUT}s)")

    # Track last seen price to detect REAL stalls
    last_price = None
    last_price_change_ts = time.time()

    while True:
        status = get_status()

        if status is None:
            log.warning("API unreachable — restarting")
            restart_and_recover()
            last_price = None
            last_price_change_ts = time.time()
            time.sleep(CHECK_INTERVAL)
            continue

        price = status.get("currentPrice", 0)
        now = time.time()

        # Track when price actually CHANGED
        if price != last_price:
            last_price = price
            last_price_change_ts = now

        age = now - last_price_change_ts

        if age > STALL_TIMEOUT:
            # Skip restart if night (market closed — price won't change)
            if _is_night():
                log.info(f"NIGHT: price stall ignored (market closed)")
                time.sleep(CHECK_INTERVAL)
                continue
            # Skip restart if robot was stopped intentionally
            if status.get("mode") == "stopped":
                log.info(f"STOPPED: robot is stopped — skip restart")
                time.sleep(CHECK_INTERVAL)
                continue
            # Skip restart if cooldown active (prevent infinite loops)
            if _restart_cooldown_active():
                remaining = int(RESTART_COOLDOWN - (time.time() - _last_restart_ts))
                log.warning(f"STALL: cooldown active ({remaining}s remaining)")
                time.sleep(CHECK_INTERVAL)
                continue
            log.warning(f"STALL: price={price} unchanged for {age:.0f}s → RESTART")
            restart_and_recover()
            _mark_restarted()
            last_price = None
            last_price_change_ts = time.time()
        else:
            log.info(f"OK: price={price} age={age:.0f}s")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
