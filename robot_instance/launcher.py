#!/usr/bin/env python3
"""Launch independent VP Scalp Grid robot instances.
Each instance: own config, state, port.
Does NOT touch the main robot (port 5070, SiM6).
Usage: python3 launcher.py start <instance_dir> <port>
       python3 launcher.py stop <instance_dir>
"""
import sys, os, json, subprocess, signal

ROBOT_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'robot'))

def start(instance_dir, port):
    os.makedirs(instance_dir, exist_ok=True)

    cfg_path = os.path.join(instance_dir, 'config.json')
    if not os.path.exists(cfg_path):
        print(f"No config at {cfg_path}")
        return False

    with open(cfg_path) as f:
        cfg = json.load(f)

    ticker = cfg.get('ticker', 'SiM6')
    symbol_map = {
        'SiM6': 'SiM6@RTSX', 'SiU6': 'SiU6@RTSX', 'SiH7': 'SiH7@RTSX',
        'MXM6': 'MXM6@RTSX', 'MXU6': 'MXU6@RTSX',
        'GDM6': 'GDM6@RTSX', 'BRK6': 'BRK6@RTSX',
        'RIM6': 'RIM6@RTSX', 'RIU6': 'RIU6@RTSX'
    }
    symbol = symbol_map.get(ticker, ticker + '@RTSX')

    # Write strategy config (robot reads from ROBOT_CONFIG_FILE)
    strat_cfg = {
        'max_levels': cfg.get('max_levels', 100),
        'step_base': cfg.get('step_base', 31),
        'spread_base': cfg.get('spread_base', 31),
        'max_hold_minutes': cfg.get('max_hold_minutes', 99999999999999),
        'min_profit_per_lot': cfg.get('min_profit_per_lot', 35),
        'vp_lookback': cfg.get('vp_lookback', 33),
        'vp_bin_size': cfg.get('vp_bin_size', 50),
        'vp_va_percent': cfg.get('vp_va_percent', 0.70),
        'rv_adaptation': cfg.get('rv_adaptation', False),
    }
    with open(os.path.join(instance_dir, 'strategy.json'), 'w') as f:
        json.dump(strat_cfg, f, indent=2)

    env = os.environ.copy()
    env['ROBOT_SYMBOL'] = symbol
    env['ROBOT_TICKER'] = ticker
    env['ROBOT_TIMEFRAME'] = cfg.get('timeframe', 'M1')
    env['ROBOT_CONFIG_FILE'] = os.path.join(instance_dir, 'strategy.json')
    env['ROBOT_STATE_FILE'] = os.path.join(instance_dir, 'state.json')
    env['ROBOT_PORT'] = str(port)

    # Launch main.py as subprocess with patched env
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROBOT_SRC, 'main.py')],
        cwd=ROBOT_SRC,
        env=env,
        stdout=open(os.path.join(instance_dir, 'stdout.log'), 'a'),
        stderr=open(os.path.join(instance_dir, 'stderr.log'), 'a'),
    )

    pid_file = os.path.join(instance_dir, 'pid')
    with open(pid_file, 'w') as f:
        f.write(str(proc.pid))

    print(f"Started {ticker} robot, pid={proc.pid}, port={port}, dir={instance_dir}")
    # Don't wait — launcher exits, main.py runs independently
    # Detach child process
    return True


def stop(instance_dir):
    pid_file = os.path.join(instance_dir, 'pid')
    if not os.path.exists(pid_file):
        print(f"No pid file at {pid_file}")
        return

    with open(pid_file) as f:
        pid = int(f.read().strip())

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped robot pid={pid}")
    except ProcessLookupError:
        print(f"Process {pid} already stopped")

    os.remove(pid_file)


def status(instance_dir):
    pid_file = os.path.join(instance_dir, 'pid')
    if not os.path.exists(pid_file):
        print("stopped")
        return
    with open(pid_file) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 0)
        print(f"running (pid={pid})")
    except ProcessLookupError:
        print("stopped (stale pid)")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: launcher.py start <dir> <port>")
        print("       launcher.py stop <dir>")
        print("       launcher.py status <dir>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == 'start':
        start(sys.argv[2], int(sys.argv[3]))
    elif cmd == 'stop':
        stop(sys.argv[2])
    elif cmd == 'status':
        status(sys.argv[2])
