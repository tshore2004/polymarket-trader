from __future__ import annotations
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
from utils.models import Signal, SignalType, Side, TradeResult, Market, MarketConsensus
from utils.categories import detect_market_category, CATEGORY_EMOJI


BANNER = r"""
 ____       _       __  __            _        _
|  _ \ ___ | |_   _|  \/  | __ _ _ __| | _____| |_
| |_) / _ \| | | | | |\/| |/ _` | '__| |/ / _ \ __|
|  __/ (_) | | |_| | |  | | (_| | |  |   <  __/ |_
|_|   \___/|_|\__, |_|  |_|\__,_|_|  |_|\_\___|\__|
              |___/  Daily Picks + Multi-Factor Signals
"""


def show_banner(console: Console) -> None:
    console.print(f"[bold cyan]{BANNER}[/bold cyan]")
    console.print("[dim]Semi-automated • Conservative $5–$25 per trade[/dim]\n")


def show_balance(console: Console, balance: Optional[float]) -> None:
    if balance is None:
        console.print("[dim]Wallet balance: unavailable (connection issue — will retry next scan)[/dim]")
    else:
        color = "bright_green" if balance >= 10 else "yellow"
        console.print(f"[{color}]Wallet balance: ${balance:,.2f} USDC[/{color}]")


def _score_color(score: float) -> str:
    if score >= 60:
        return "bright_green"
    if score >= 35:
        return "yellow"
    return "cyan"


_TYPE_LABELS = {
    SignalType.LEADERBOARD: "[bold blue]LEADERBOARD[/bold blue]",
    SignalType.FAIR_VALUE: "[bold magenta]FAIR VALUE EDGE[/bold magenta]",
    SignalType.NEWS: "[bold cyan]NEWS SENTIMENT[/bold cyan]",
    SignalType.VOLUME_SPIKE: "[bold yellow]VOLUME SPIKE[/bold yellow]",
    SignalType.MULTI: "[bold bright_green]MULTI-FACTOR[/bold bright_green]",
}


def _bet_label(signal: Signal) -> str:
    """Return the clearest possible bet instruction."""
    market = signal.market
    side = signal.recommended_side
    price = signal.recommended_price

    token = market.yes_token if side == Side.YES else market.no_token

    outcome_label = None
    if token and token.outcome.lower() not in ("yes", "no", "1", "0", ""):
        outcome_label = token.outcome

    if outcome_label:
        return f"Select: {outcome_label} ({side.name} @ {price:.3f})"
    return f"BET {side.name} @ {price:.3f} — {market.question[:60]}"


def show_signal(console: Console, sig: Signal, index: int) -> None:
    score_color = _score_color(sig.combined_score)
    type_label = _TYPE_LABELS.get(sig.signal_type, sig.signal_type.value)

    side_color = "green" if sig.recommended_side == Side.YES else "red"

    title = (
        f"  #{index}  {type_label}  |  "
        f"Score [{score_color}]{sig.combined_score:.0f}/100[/{score_color}]  |  "
        f"Side [{side_color}]{sig.recommended_side.value}[/{side_color}] @ {sig.recommended_price:.3f}"
    )

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Key", style="dim", width=22)
    table.add_column("Value")

    q = sig.market.question

    # Prominent summary line — dominant side, confidence, trader count, score
    if sig.consensus:
        con = sig.consensus
        summary = (
            f"▶ BET {sig.recommended_side.value}"
            f"  |  Conf: {con.confidence*100:.0f}%"
            f"  |  {con.num_traders_dominant} traders"
            f"  |  Score: {sig.combined_score:.0f}"
        )
    else:
        summary = f"▶ BET {sig.recommended_side.value}  |  Score: {sig.combined_score:.0f}"
    table.add_row("", f"[bold {side_color}]{summary}[/bold {side_color}]")

    # Bet action — uses _bet_label for named-outcome (sports) vs binary markets
    action_str = f"[bold {side_color}]{_bet_label(sig)}[/bold {side_color}]"
    table.add_row("Action", action_str)
    table.add_row("Market", q if len(q) <= 110 else q[:107] + "...")

    slug = sig.market.event_slug
    if slug:
        table.add_row("Find on", f"[dim]https://polymarket.com/event/{slug}[/dim]")

    if sig.market.tags:
        table.add_row("Tags", ", ".join(sig.market.tags[:6]))

    # Time category
    tc = sig.market.time_category
    tc_colors = {"tonight": "bright_red", "tomorrow": "yellow", "this_week": "cyan"}
    tc_color = tc_colors.get(tc, "dim")
    table.add_row("Resolves", f"[{tc_color}]{tc}[/{tc_color}]")

    # Score breakdown
    s = sig.scores
    breakdown_parts = []
    if s.leaderboard > 0:
        breakdown_parts.append(f"[blue]LB {s.leaderboard:.0f}/30[/blue]")
    if s.fair_value_edge > 0:
        breakdown_parts.append(f"[magenta]Edge {s.fair_value_edge:.0f}/30[/magenta]")
    if s.line_movement > 0:
        breakdown_parts.append(f"[yellow]Vol {s.line_movement:.0f}/20[/yellow]")
    if s.news_momentum > 0:
        breakdown_parts.append(f"[cyan]News {s.news_momentum:.0f}/10[/cyan]")
    if s.urgency > 0:
        breakdown_parts.append(f"[bright_red]Urg {s.urgency:.0f}/10[/bright_red]")
    if breakdown_parts:
        table.add_row("Score Breakdown", "  ".join(breakdown_parts))

    # Fair value info
    if sig.fair_value is not None:
        edge_color = "bright_green" if sig.edge_pct > 0.02 else ("yellow" if sig.edge_pct > 0 else "red")
        table.add_row(
            "Fair Value",
            f"{sig.fair_value*100:.1f}% implied  |  "
            f"Poly price: {sig.recommended_price*100:.1f}%  |  "
            f"Edge: [{edge_color}]{sig.edge_pct*100:+.1f}%[/{edge_color}]"
        )

    # Leaderboard consensus
    if sig.consensus:
        con = sig.consensus
        copy_color = "bright_green" if con.copy_score >= 60 else ("yellow" if con.copy_score >= 35 else "cyan")
        table.add_row(
            "Copy strength",
            f"[{copy_color}]{con.copy_score:.0f}/100[/{copy_color}]  "
            f"[dim](how strong the smart-money signal is)[/dim]"
        )
        table.add_row(
            "Top bettors backing",
            f"[{side_color}]{con.num_traders_dominant} on {con.dominant_side.value}[/{side_color}]  |  "
            f"${con.dominant_position_value:,.0f} held  |  "
            f"{con.confidence*100:.0f}% agreement  |  "
            f"{con.avg_dominant_win_rate*100:.0f}% avg win rate"
        )
        # Per-trader stakes on the side we're recommending
        stakes = con.dominant_stakes[:5]
        if stakes:
            stake_str = "  ".join(
                f"{s.name} [dim]${s.size:,.0f}[/dim]" for s in stakes
            )
            table.add_row("Who's in", stake_str)

    if sig.explanation:
        table.add_row("Why", f"[dim italic]{sig.explanation}[/dim italic]")

    console.print(Panel(table, title=title, border_style=score_color, expand=False))


def _signal_horizon(sig: Signal) -> str:
    """tonight | short | long for report grouping (prefers copy-consensus horizon)."""
    if sig.consensus and sig.consensus.horizon:
        return sig.consensus.horizon
    tc = sig.market.time_category
    if tc in ("tonight",):
        return "tonight"
    if tc in ("tomorrow", "this_week"):
        return "short"
    return "long"


def show_daily_report(console: Console, signals: list[Signal], limit: int = 12) -> None:
    """Ranked daily report: smart-money copy picks, split by time horizon.

    Sections:
      🔴 TONIGHT / SHORT-TERM  — top bettors' positions resolving soon (e.g. games)
      📅 LONG-TERM HOLDS       — top bettors' longer-dated positions
      📊 OTHER SIGNALS         — volume/news markets with no top-trader backing
    """
    console.print("\n[bold bright_green]===  DAILY PICKS — WHAT THE TOP BETTORS ARE HOLDING  ===[/bold bright_green]\n")

    if not signals:
        console.print("[dim]No qualifying signals found.[/dim]")
        return

    smart = [s for s in signals if s.consensus is not None]
    other = [s for s in signals if s.consensus is None]

    tonight = [s for s in smart if _signal_horizon(s) in ("tonight", "short")]
    longterm = [s for s in smart if _signal_horizon(s) == "long"]

    idx = 1
    if tonight:
        console.print(f"[bold bright_red]🔴 TONIGHT / SHORT-TERM[/bold bright_red] "
                      f"[dim]({len(tonight)} smart-money picks resolving within ~2 days)[/dim]\n")
        for sig in tonight[:limit]:
            show_signal(console, sig, idx)
            idx += 1

    if longterm:
        console.print(f"\n[bold cyan]📅 LONG-TERM HOLDS[/bold cyan] "
                      f"[dim]({len(longterm)} smart-money picks resolving later)[/dim]\n")
        for sig in longterm[:limit]:
            show_signal(console, sig, idx)
            idx += 1

    if not smart:
        console.print("[yellow]No top-trader (smart-money) positions matched live markets this scan.[/yellow]")
        console.print("[dim]Showing other signals below. If this persists, the leaderboard/positions "
                      "API may be rate-limited — re-run in a minute.[/dim]\n")

    if other and idx <= limit:
        console.print(f"\n[bold]📊 OTHER SIGNALS[/bold] [dim](volume/news, no top-trader backing)[/dim]\n")
        for sig in other[: max(0, limit - len(smart))]:
            show_signal(console, sig, idx)
            idx += 1


def show_event_library(console: Console, events: dict[str, list[Market]]) -> list[str]:
    """Display events grouped by category. Returns list of event keys for selection."""
    console.print("\n[bold]===  EVENT LIBRARY  ===[/bold]\n")

    # Group events by category
    categorized: dict[str, list[tuple[str, list[Market]]]] = {}
    for event_key, markets in events.items():
        if markets:
            cat, _ = detect_market_category(markets[0])
            categorized.setdefault(cat, []).append((event_key, markets))

    event_list: list[str] = []
    idx = 1

    for category in ["sports", "politics", "crypto", "entertainment", "other"]:
        if category not in categorized:
            continue

        emoji = CATEGORY_EMOJI.get(category, "")
        console.print(f"\n[bold]{emoji} {category.upper()}[/bold]")

        cat_events = categorized[category]
        # Sort by earliest resolution date
        cat_events.sort(key=lambda x: _earliest_end(x[1]))

        for event_key, markets in cat_events:
            event_list.append(event_key)
            tc = markets[0].time_category
            tc_colors = {"tonight": "bright_red", "tomorrow": "yellow", "this_week": "cyan"}
            tc_color = tc_colors.get(tc, "dim")

            # Show event with market count — prefer the first market question over raw slug
            raw_name = markets[0].question if markets else event_key
            display_name = raw_name[:50] if len(raw_name) <= 50 else raw_name[:47] + "..."
            console.print(
                f"  [cyan]{idx:>3}[/cyan]  [{tc_color}]{tc:<12}[/{tc_color}]  "
                f"{display_name}  [dim]({len(markets)} markets)[/dim]"
            )
            idx += 1

    return event_list


def show_event_detail(console: Console, event_key: str, markets: list[Market]) -> None:
    """Show detailed breakdown of markets within an event."""
    console.print(f"\n[bold]Event: {event_key}[/bold]")
    console.print(f"[dim]{len(markets)} markets[/dim]\n")

    table = Table(box=box.ROUNDED, show_header=True, padding=(0, 1))
    table.add_column("#", width=4, style="dim")
    table.add_column("Market", max_width=55)
    table.add_column("Resolves", width=12)
    table.add_column("Volume", width=12, justify="right")

    for i, m in enumerate(markets, 1):
        q = m.question if len(m.question) <= 55 else m.question[:52] + "..."
        tc = m.time_category
        vol = f"${m.volume:,.0f}" if m.volume > 0 else "—"
        table.add_row(str(i), q, tc, vol)

    console.print(table)


def _earliest_end(markets: list[Market]) -> float:
    """Sort key: earliest end_date in the group, or far future."""
    from datetime import timezone
    dates = []
    for m in markets:
        if m.end_date:
            ed = m.end_date
            if ed.tzinfo is None:
                ed = ed.replace(tzinfo=timezone.utc)
            dates.append(ed.timestamp())
    return min(dates) if dates else float("inf")


def show_scan_status(
    console: Console,
    markets_checked: int,
    found: int,
    elapsed: float,
    mode: str = "all",
    active_tags: Optional[list[str]] = None,
) -> None:
    tag_note = f"  tags: {', '.join(active_tags)}" if active_tags else ""
    console.print(
        f"[dim]Scanned {markets_checked} markets in {elapsed:.1f}s — "
        f"[bold]{found}[/bold] signal{'s' if found != 1 else ''} found  "
        f"[mode: {mode}]{tag_note}[/dim]"
    )


def show_trade_result(console: Console, result: TradeResult) -> None:
    if result.success:
        console.print(
            f"[bold bright_green]OK[/bold bright_green]  "
            f"ID: [dim]{result.order_id}[/dim]  "
            f"{result.side} @ {result.price:.4f}  size ${result.size:.2f}"
        )
    else:
        console.print(f"[bold red]FAIL:[/bold red] {result.error}")


def prompt_trade(console: Console, sig: Signal, size: float) -> Optional[bool]:
    """Return True=execute, False=skip, None=quit."""
    side_label = sig.recommended_side.value
    price = sig.recommended_price
    action = f"BUY ${size:.2f} of {side_label} @ {price:.4f}"

    console.print(f"\n[bold]Action:[/bold] {action}")
    response = console.input("[bold yellow]Execute? [y/skip/q][/bold yellow] > ").strip().lower()
    if response in ("q", "quit"):
        return None
    return response in ("y", "yes")


def show_mode_menu(
    console: Console,
    config_mode: str = "all",
    config_tags: Optional[list[str]] = None,
) -> tuple[str, list[str]]:
    """Interactive startup menu. Returns (selected_mode, selected_tags)."""
    config_tags = config_tags or []

    modes = [
        ("1", "today",       "Daily picks — events resolving today/tonight (recommended)"),
        ("2", "all",         "All signals — Leaderboard + News + Volume + Fair Value"),
        ("3", "leaderboard", "Leaderboard copy-trading only"),
        ("4", "news",        "News sentiment + Volume spikes only"),
        ("5", "volume",      "Volume/price movement detection only"),
    ]

    console.print("\n[bold]-- Scan Mode -----------------------------------------------[/bold]")
    for num, key, desc in modes:
        marker = "  [dim]< current[/dim]" if key == config_mode else ""
        console.print(f"  [cyan]{num}[/cyan]  [bold]{key:<14}[/bold] [dim]{desc}[/dim]{marker}")
    raw = console.input("[bold yellow]Select [1-5, Enter to keep current]:[/bold yellow] > ").strip()
    mode_map = {m[0]: m[1] for m in modes}
    mode = mode_map.get(raw, config_mode)

    tag_options = [
        ("1", "politics"),     ("2", "crypto"),       ("3", "sports"),
        ("4", "entertainment"),("5", "science"),       ("6", "business"),
        ("7", "economics"),    ("8", "technology"),    ("9", "geopolitics"),
    ]

    console.print("\n[bold]-- Market Categories ----------------------------------------[/bold]")
    console.print("[dim]  Leave blank to scan all categories[/dim]")
    for num, tag in tag_options:
        marker = "  [dim]<[/dim]" if tag in config_tags else ""
        console.print(f"  [cyan]{num}[/cyan]  {tag}{marker}")
    raw_tags = console.input(
        "[bold yellow]Filter categories [e.g. 1,3 — Enter for all]:[/bold yellow] > "
    ).strip()

    tags: list[str] = config_tags
    if raw_tags:
        tag_map = {t[0]: t[1] for t in tag_options}
        selected = [tag_map[n.strip()] for n in raw_tags.split(",") if n.strip() in tag_map]
        if selected:
            tags = selected
    elif not raw_tags and config_tags:
        clear = console.input("[dim]Clear existing tag filter? [y/N]:[/dim] > ").strip().lower()
        if clear in ("y", "yes"):
            tags = []

    console.print()
    return mode, tags


def show_run_mode_menu(console: Console) -> str:
    """Choose between daily report and interactive browse."""
    console.print("\n[bold]-- Run Mode ------------------------------------------------[/bold]")
    console.print("  [cyan]1[/cyan]  [bold]report[/bold]      Daily picks report — ranked best bets")
    console.print("  [cyan]2[/cyan]  [bold]browse[/bold]      Interactive event library — pick your events")
    console.print("  [cyan]3[/cyan]  [bold]continuous[/bold]   Continuous scan loop (legacy behavior)")

    raw = console.input("[bold yellow]Select [1-3]:[/bold yellow] > ").strip()
    return {"1": "report", "2": "browse", "3": "continuous"}.get(raw, "report")
