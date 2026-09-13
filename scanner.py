"""
scanner.py
----------
Main orchestrator. Runs one full pass of the strategy for a given universe
of symbols using whatever candle + OI data you feed it (CSV now, live
broker feed later — same functions, different data source).

Usage:
    python scanner.py --oi path/to/oi.csv --candles path/to/candles.csv
"""
import argparse
from collections import defaultdict

import config
from data_loader import load_oi_csv, load_candles_csv
from oi_analysis import load_oi_snapshot, get_high_oi_strikes, get_significant_oi_changes
from sr_levels import get_important_levels
from watchlist import Watchlist
from confirmation import evaluate


def run_pass(symbols, candles_by_symbol, oi_rows, watchlist: Watchlist, is_first_pass=False):
    """Note: with the rolling_15min OI baseline, every snapshot is both a
    'record' and a 'check' — there's no separate baseline-only pass anymore.
    Just feed snapshots in every 1-5 min (or per historical row) and call
    this each time; the rolling window builds up on its own."""
    strikes = load_oi_snapshot(oi_rows)  # = oi_analysis.record_snapshot()

    new_entries_total = []
    setups = []

    for symbol in symbols:
        candles = candles_by_symbol.get(symbol, [])
        if not candles:
            continue
        current_price = candles[-1].close

        sr_levels = get_important_levels(symbol, candles)

        symbol_strikes = [s for s in strikes if s.symbol == symbol]
        if not symbol_strikes:
            continue
        expiries = set(s.expiry for s in symbol_strikes)

        for expiry in expiries:
            high_ce = get_high_oi_strikes(symbol_strikes, symbol, expiry, "CE")
            high_pe = get_high_oi_strikes(symbol_strikes, symbol, expiry, "PE")

            new_entries = watchlist.scan_symbol(symbol, current_price, sr_levels, high_ce, high_pe)
            new_entries_total.extend(new_entries)

        # Run confirmation on every active entry for this symbol whose
        # matched strike currently shows a significant OI change (fresh trigger)
        sig_changes = get_significant_oi_changes(symbol_strikes)
        sig_keys = {(sc.strike, sc.option_type) for sc in sig_changes}
        for entry in watchlist.get_active():
            if entry.symbol != symbol:
                continue
            if (entry.matched_strike.strike, entry.matched_strike.option_type) in sig_keys:
                setup = evaluate(entry, candles)
                if setup:
                    watchlist.mark_confirmed(entry)
                    setups.append(setup)

    return new_entries_total, setups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oi", help="Path to a single OI CSV (one snapshot, no rolling history yet)")
    parser.add_argument("--oi-dir", help="Directory of OI CSV snapshots, one per pull "
                                          "(e.g. oi_0915.csv, oi_0920.csv...), filenames "
                                          "sorted = chronological order. Needed to build "
                                          "the rolling 15-min OI-change history.")
    parser.add_argument("--candles", required=True, help="Path to candles CSV (intraday, any timeframe "
                                                           "<= config.SR_TIMEFRAME_MIN; will be resampled)")
    args = parser.parse_args()

    if not args.oi and not args.oi_dir:
        parser.error("Provide --oi (single snapshot) or --oi-dir (sequence of snapshots)")

    if args.oi_dir:
        import glob
        snapshot_paths = sorted(glob.glob(f"{args.oi_dir}/*.csv"))
    else:
        snapshot_paths = [args.oi]

    first_rows = load_oi_csv(snapshot_paths[0])
    symbols = sorted(set(r["symbol"] for r in first_rows))

    candles_by_symbol = defaultdict(list)
    for sym in symbols:
        candles_by_symbol[sym] = load_candles_csv(args.candles, symbol=sym)

    watchlist = Watchlist()
    all_new_entries, all_setups = [], []

    for i, path in enumerate(snapshot_paths):
        oi_rows = load_oi_csv(path)
        new_entries, setups = run_pass(symbols, candles_by_symbol, oi_rows, watchlist)
        all_new_entries.extend(new_entries)
        all_setups.extend(setups)

    print(f"\n=== Watchlist additions: {len(all_new_entries)} ===")
    for e in all_new_entries:
        print(f"  {e.symbol}: {e.reason}")

    print(f"\n=== Trade setups generated: {len(all_setups)} ===")
    for s in all_setups:
        print(f"  {s.symbol} | {s.direction.upper()} | entry_zone={s.entry_zone} "
              f"| confirmations={s.confirmations}")


if __name__ == "__main__":
    main()
