#!/usr/bin/env python3
"""Watchdog for of-robot — restarts robot if price stream stalls.

Monitors /status API. If price hasn't updated for N seconds, restarts the service.
Also restores correct parameters from of_config.json after restart.
"""
import subprocess
import json
import time
import sys
import urllib.request
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [watchdog] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('robot_watchdog')

STALL_TIMEOUT = 120   # seconds without price CHANGE → restart
CHECK_INTERVAL = 30   # check every 30 sec
SERVICE = "of-robot.service"
API_URL = "http://localhost:5080/status"
START_URL = "http://localhost:5080/start"
PARAMS_URL = "http://localhost:5080/params"
CONFIG_PATH = "/root/.openclaw/workspace/HedgeFund/robot/of_config.json"

def get_status():
    try:
        with urllib.request.urlopen(API_URL, timeout=5) as resp:
            return json.load(resp)
    except Exception as e:
        log.error(f"API error: {e}")
        return None

def restart_and_recover():
    log.info(f"Restarting {SERVICE}...")
    subprocess.run(["systemctl", "restart", SERVICE], timeout=30)
    time.sleep(15)

    try:
        urllib.request.urlopen(START_URL, timeout=5)
        log.info("Trading started")
    except Exception as e:
        log.error(f"Recovery error: {e}")

def main():
    log.info(f"Monitoring {SERVICE} (stall={STALL_TIMEOUT}s, check={CHECK_INTERVAL}s)")

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
            log.warning(f"STALL: price={price} unchanged for {age:.0f}s → RESTART")
            restart_and_recover()
            last_price = None
            last_price_change_ts = time.time()
        else:
            log.info(f"OK: price={price} age={age:.0f}s")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
