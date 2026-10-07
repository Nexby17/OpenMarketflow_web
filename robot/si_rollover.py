"""F-061: auto-rollover for the eternal Si future (si_perp robot).

MOEX has no perpetual futures: the "eternal" Si is a chain of quarterly
contracts SiZ6 -> SiH6 -> SiM6 -> SiU6 -> ... The robot is a 1:1 copy of
OF-Si; this module switches its SYMBOL to the actual (front) contract
before expiry, using the robot's own POST /instrument endpoint
(works only when stopped + FLAT — same safety as manual switch).

Usage (manual/scheduled, robot must be running):
    python si_rollover.py --check                  # report only
    python si_rollover.py --apply                  # switch if due
    python si_rollover.py --apply --port 5084      # explicit robot port
    python si_rollover.py --force --ticker SiH6    # manual switch

Contract series (RTSX): SiZ6, SiH6, SiM6, SiU6, then next year letters.
Month letters: H=Mar, M=Jun, U=Sep, Z=Dec. Expiry ~15th of expiry month.
Switch window: DAYS_BEFORE days before expiry (default 5), only if FLAT.
"""
import argparse
import json
import sys
import urllib.request
from datetime import date

# Si quarterly series: (month_letter, month_number) for 2026-2028+
_SERIES = [("Z", 12), ("H", 3), ("M", 6), ("U", 9)]

def _contracts_for(year: int):
    # order by expiry within the year: Mar(H), Jun(M), Sep(U), Dec(Z)
    return [(f"Si{letter}{year % 10}", year, month)
            for letter, month in [("H", 3), ("M", 6), ("U", 9), ("Z", 12)]]

def build_chain(years=(2026, 2027, 2028)):
    """Ordered list of (ticker, expiry_date). Expiry assumed ~15th of month."""
    chain = []
    for y in years:
        for t, y_, m in _contracts_for(y):
            # roll over: Z6 expires Dec 2026 -> after that SiH7 etc.
            chain.append((f"Si{t[2]}{y % 10}" if False else t, date(y_, m, 15)))
    # fix tickers: build explicitly
    chain = []
    for y in years:
        for letter, m in [("H", 3), ("M", 6), ("U", 9), ("Z", 12)]:
            chain.append((f"Si{letter}{y % 10}", date(y, m, 15)))
    # sort by expiry
    chain.sort(key=lambda x: x[1])
    return chain

def front_contract(today: date, years=(2026, 2027, 2028)):
    """First contract whose expiry is still >= today."""
    for ticker, exp in build_chain(years):
        if exp >= today:
            return ticker, exp
    return None, None

def robot_status(port: int):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def switch_instrument(port: int, ticker: str):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/instrument",
        data=json.dumps({"instrument": ticker}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return {"http_error": e.code, "body": e.read().decode()[:300]}
        except Exception:
            return {"http_error": e.code}
    except Exception as e:
        return {"error": str(e)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5084)
    ap.add_argument("--check", action="store_true", help="report only, no switch")
    ap.add_argument("--apply", action="store_true", help="switch if due")
    ap.add_argument("--force", action="store_true", help="manual switch (with --ticker)")
    ap.add_argument("--ticker", type=str, default="", help="explicit ticker for --force")
    ap.add_argument("--days-before", type=int, default=5)
    args = ap.parse_args()

    today = date.today()
    front, exp = front_contract(today)
    st = robot_status(args.port)
    cur = str(st.get("symbol", "?")).split("@")[0]
    mode = st.get("mode", "?")
    lots = st.get("totalLots", "?")

    print(f"today={today} front={front} (expiry {exp}) days_left={(exp - today).days if exp else '?'}")
    print(f"robot :{args.port} symbol={cur} mode={mode} lots={lots}")

    if args.force and args.ticker:
        print("switch ->", switch_instrument(args.port, args.ticker))
        return

    if front is None:
        print("ERROR: contract chain exhausted (extend years in si_rollover.py)")
        sys.exit(1)

    if front == cur:
        print("OK: already on front contract")
        return

    due = (exp - today).days <= args.days_before
    if not due:
        print(f"no switch needed (>= {args.days_before} days to expiry)")
        return

    print(f"SWITCH DUE: {cur} -> {front} (expiry in {(exp - today).days} days)")
    if args.check or not args.apply:
        print("dry-run (use --apply to switch)")
        return

    if mode != "stopped" or lots not in (0, "0"):
        print(f"REFUSED: robot must be stopped+FLAT to roll (mode={mode} lots={lots}). Stop it first.")
        sys.exit(2)

    print("switch ->", switch_instrument(args.port, front))

if __name__ == "__main__":
    main()
