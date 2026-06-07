"""
Polymarket Trading Bot — multi-factor daily picks + interactive event browser.
Run: python main.py
"""
from __future__ import annotations
import logging
import signal
import time

from rich.console import Console
from rich.rule import Rule

from config import Config
from core.signal import SignalEngine
from core.executor import TradeExecutor
from utils.display import (
    show_banner, show_scan_status, show_balance, show_mode_menu,
    show_run_mode_menu, show_daily_report, show_event_library,
    show_event_detail, show_signal, prompt_trade, show_starred_traders,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

console = Console()


def run() -> None:
    try:
        config = Config.load()
    except EnvironmentError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        raise SystemExit(1)

    show_banner(console)
    console.print(
        f"[dim]Max bet: ${config.max_bet_size:.0f}  "
        f"Min bet: ${config.min_bet_size:.0f}  "
        f"Leaderboard window: {config.leaderboard_window}[/dim]"
    )

    executor = TradeExecutor(config)
    show_balance(console, executor.get_balance())
    console.print()

    # Startup mode / category selection
    scan_mode, active_tags = show_mode_menu(console, config.scan_mode, config.market_tags_filter)
    config.market_tags_filter = active_tags

    # Run mode selection
    run_mode = show_run_mode_menu(console)

    engine = SignalEngine(config)

    tag_note = f"  categories: {', '.join(active_tags)}" if active_tags else "  categories: all"
    console.print(f"[dim]Scan mode: [bold]{scan_mode}[/bold]{tag_note}  |  Run mode: [bold]{run_mode}[/bold][/dim]\n")

    if run_mode == "report":
        _run_daily_report(engine, executor, config, scan_mode)
    elif run_mode == "browse":
        _run_interactive_browser(engine, executor, config, scan_mode)
    else:
        _run_continuous(engine, executor, config, scan_mode, active_tags)


def _run_daily_report(
    engine: SignalEngine,
    executor: TradeExecutor,
    config: Config,
    scan_mode: str,
) -> None:
    """One-shot daily report: scan, rank, display, optionally execute."""
    console.print(Rule("[bold]Running daily scan...[/bold]", style="cyan"))
    t0 = time.monotonic()

    try:
        signals = engine.scan(mode=scan_mode)
    except Exception as exc:
        console.print(f"[red]Scan error: {exc}[/red]")
        return

    # Smart-money copy picks span every horizon (tonight + long-dated). Merge them
    # in so the daily report always leads with what the top bettors are holding,
    # regardless of the scan-mode time window.
    picks = engine.pick_signals()
    by_id = {s.market.condition_id: s for s in picks}
    for s in signals:
        by_id.setdefault(s.market.condition_id, s)
    signals = sorted(by_id.values(), key=lambda s: s.combined_score, reverse=True)

    elapsed = time.monotonic() - t0
    show_scan_status(console, engine.markets_loaded, len(signals), elapsed, mode=scan_mode)

    show_daily_report(console, signals, limit=config.daily_report_top_n)

    if not signals:
        if scan_mode == "today":
            _show_today_fallback(console, engine)
        return

    # Offer to execute top picks
    console.print("\n[bold]Commands:[/bold]")
    console.print("  [dim]#        Execute pick by number[/dim]")
    console.print("  [dim]all      Execute top 5[/dim]")
    console.print("  [dim]star #   Star the traders backing pick #[/dim]")
    console.print("  [dim]stars    View starred traders & positions[/dim]")
    console.print("  [dim]q        Quit[/dim]")

    while True:
        raw = console.input("[yellow]> [/yellow]").strip()
        raw_lower = raw.lower()

        if raw_lower in ("q", "quit", ""):
            break

        if raw_lower == "stars":
            show_starred_traders(console, engine._lb.traders, engine._lb._positions)
            continue

        if raw_lower.startswith("star "):
            _handle_star_command(console, raw, signals, engine)
            continue

        if raw_lower.startswith("unstar "):
            addr = raw[7:].strip()
            if engine._lb.starred.unstar(addr):
                console.print(f"[dim]Unstarred {addr[:12]}...[/dim]")
            else:
                console.print("[dim]Address not found in starred list.[/dim]")
            continue

        if raw_lower == "all":
            for sig in signals[:5]:
                confirmed = executor.present(console, sig, signals.index(sig) + 1)
                if confirmed is None:
                    break
                elif confirmed:
                    executor.execute(sig, console)
            break

        try:
            idx = int(raw) - 1
            if 0 <= idx < len(signals):
                sig = signals[idx]
                confirmed = executor.present(console, sig, idx + 1)
                if confirmed is None:
                    break
                elif confirmed:
                    executor.execute(sig, console)
            else:
                console.print("[dim]Invalid pick number.[/dim]")
        except ValueError:
            console.print("[dim]Enter a number, 'all', 'star #', 'stars', or 'q'.[/dim]")

    console.print("\n[bold yellow]Done. Goodbye.[/bold yellow]")


def _handle_star_command(console: Console, raw: str, signals: list, engine: SignalEngine) -> None:
    """Star all traders backing a specific pick number."""
    try:
        pick_num = int(raw.split()[1]) - 1
        if 0 <= pick_num < len(signals):
            sig = signals[pick_num]
            if sig.consensus:
                starred_count = 0
                for t in sig.consensus.traders:
                    engine._lb.starred.star(t.address, t.name)
                    starred_count += 1
                console.print(
                    f"[bright_green]Starred {starred_count} trader(s) "
                    f"from pick #{pick_num+1}[/bright_green]"
                )
            else:
                console.print("[dim]No trader data for this pick.[/dim]")
        else:
            console.print("[dim]Invalid pick number.[/dim]")
    except (ValueError, IndexError):
        console.print("[dim]Usage: star <pick_number>[/dim]")


def _show_today_fallback(console: Console, engine: SignalEngine) -> None:
    """When no signals qualify today, show highest-volume today's markets as reference."""
    todays = engine.get_todays_events()
    if not todays:
        return
    all_today = [m for markets in todays.values() for m in markets]
    top = sorted(all_today, key=lambda m: m.volume, reverse=True)[:10]
    console.print("\n[bold yellow]Today's Expiring Markets[/bold yellow] [dim](no strong signal yet — shown for reference)[/dim]")
    for i, m in enumerate(top, 1):
        tc = m.time_category
        tc_color = "red" if tc == "tonight" else "yellow"
        console.print(
            f"  [dim]{i:>2}.[/dim] [{tc_color}]{tc}[/{tc_color}]  "
            f"[bold]{m.question[:80]}[/bold]  "
            f"[dim]vol ${m.volume:,.0f}[/dim]"
        )


def _run_interactive_browser(
    engine: SignalEngine,
    executor: TradeExecutor,
    config: Config,
    scan_mode: str,
) -> None:
    """Interactive event browser: scan, show events, let user drill in."""
    console.print(Rule("[bold]Loading events...[/bold]", style="cyan"))
    t0 = time.monotonic()

    try:
        signals = engine.scan(mode=scan_mode)
    except Exception as exc:
        console.print(f"[red]Scan error: {exc}[/red]")
        return

    elapsed = time.monotonic() - t0
    show_scan_status(console, engine.markets_loaded, len(signals), elapsed, mode=scan_mode)

    # Build signal lookup
    signal_by_mid = {s.market.condition_id: s for s in signals}

    while True:
        # Show event library
        if scan_mode == "today":
            events = engine.get_todays_events()
            console.print(f"\n[bold]Events resolving in the next 24 hours ({len(events)} events):[/bold]")
        else:
            events = engine.get_all_events()

        event_keys = show_event_library(console, events)

        if not event_keys:
            console.print("[dim]No events found.[/dim]")
            break

        raw = console.input(
            "\n[yellow]Enter event # to analyze, 'r' to rescan, or 'q' to quit:[/yellow] > "
        ).strip().lower()

        if raw in ("q", "quit", ""):
            break

        if raw == "r":
            console.print("[dim]Rescanning...[/dim]")
            try:
                signals = engine.scan(mode=scan_mode)
                signal_by_mid = {s.market.condition_id: s for s in signals}
            except Exception as exc:
                console.print(f"[red]Scan error: {exc}[/red]")
            continue

        try:
            idx = int(raw) - 1
            if 0 <= idx < len(event_keys):
                event_key = event_keys[idx]
                event_markets = events[event_key]

                show_event_detail(console, event_key, event_markets)

                # Show signals for markets in this event
                event_signals = [
                    signal_by_mid[m.condition_id]
                    for m in event_markets
                    if m.condition_id in signal_by_mid
                ]
                event_signals.sort(key=lambda s: s.combined_score, reverse=True)

                if event_signals:
                    console.print(f"\n[bold]Picks for this event ({len(event_signals)} signals):[/bold]\n")
                    for i, sig in enumerate(event_signals, 1):
                        show_signal(console, sig, i)

                    # Offer execution
                    while True:
                        raw2 = console.input(
                            "[yellow]Execute pick # or 'b' to go back:[/yellow] > "
                        ).strip().lower()
                        if raw2 in ("b", "back", ""):
                            break
                        try:
                            pidx = int(raw2) - 1
                            if 0 <= pidx < len(event_signals):
                                sig = event_signals[pidx]
                                confirmed = executor.present(console, sig, pidx + 1)
                                if confirmed:
                                    executor.execute(sig, console)
                            else:
                                console.print("[dim]Invalid pick #.[/dim]")
                        except ValueError:
                            console.print("[dim]Enter a number or 'b'.[/dim]")
                else:
                    console.print("[dim]No qualifying signals for this event.[/dim]")
            else:
                console.print("[dim]Invalid event number.[/dim]")
        except ValueError:
            console.print("[dim]Enter a number, 'r', or 'q'.[/dim]")

    console.print("\n[bold yellow]Done. Goodbye.[/bold yellow]")


def _run_continuous(
    engine: SignalEngine,
    executor: TradeExecutor,
    config: Config,
    scan_mode: str,
    active_tags: list[str],
) -> None:
    """Legacy continuous scan loop."""
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False
        console.print("\n[yellow]Stopping after this scan...[/yellow]")

    signal.signal(signal.SIGINT, _stop)
    scan_count = 0

    while running:
        scan_count += 1
        console.print(Rule(f"[dim]Scan #{scan_count}  [{scan_mode}][/dim]", style="dim"))
        t0 = time.monotonic()

        show_balance(console, executor.get_balance())

        try:
            signals = engine.scan(mode=scan_mode)
        except Exception as exc:
            console.print(f"[red]Scan error: {exc}[/red]")
            time.sleep(10)
            continue

        elapsed = time.monotonic() - t0
        show_scan_status(
            console, engine.markets_loaded, len(signals), elapsed,
            mode=scan_mode, active_tags=active_tags or None,
        )

        if not signals:
            console.print(f"[dim]Next scan in {config.scan_interval}s (Ctrl+C to stop)[/dim]")
        else:
            for i, sig in enumerate(signals, start=1):
                if not running:
                    break
                confirmed = executor.present(console, sig, i)
                if confirmed is None:
                    running = False
                    break
                elif confirmed:
                    executor.execute(sig, console)
                else:
                    console.print("[dim]  Skipped.[/dim]")

        if running:
            _sleep_interruptible(config.scan_interval)

    console.print("\n[bold yellow]Bot stopped. Goodbye.[/bold yellow]")


def _sleep_interruptible(seconds: int) -> None:
    for _ in range(seconds):
        time.sleep(1)


if __name__ == "__main__":
    run()
