from __future__ import annotations
import logging
import sys
from datetime import datetime
from colorama import Fore, Style, init

init(autoreset=True)

# Ensure stdout and stderr support UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class ProfessionalConsoleFormatter(logging.Formatter):
    """Clean, high-contrast, professional CLI formatter."""

    ICONS = {
        "DEBUG": f"{Fore.LIGHTBLACK_EX}[DEBUG]{Style.RESET_ALL}",
        "INFO": f"{Fore.CYAN}[INFO]{Style.RESET_ALL}",
        "WARNING": f"{Fore.YELLOW}[WARN]{Style.RESET_ALL}",
        "ERROR": f"{Fore.RED}[ERROR]{Style.RESET_ALL}",
        "CRITICAL": f"{Fore.RED}{Style.BRIGHT}[CRIT]{Style.RESET_ALL}",
    }

    MODULE_BADGES = {
        "main": f"{Fore.MAGENTA}[SYSTEM]  {Style.RESET_ALL}",
        "tracker": f"{Fore.CYAN}[TRACKER] {Style.RESET_ALL}",
        "telegram": f"{Fore.GREEN}[TELEGRAM]{Style.RESET_ALL}",
        "binance": f"{Fore.YELLOW}[BINANCE] {Style.RESET_ALL}",
        "database": f"{Fore.BLUE}[STORAGE] {Style.RESET_ALL}",
        "signal_engine": f"{Fore.GREEN}{Style.BRIGHT}[SIGNAL]  {Style.RESET_ALL}",
    }

    def format(self, record: logging.LogRecord) -> str:
        time_str = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        badge = self.MODULE_BADGES.get(record.name, f"{Fore.WHITE}[{record.name[:8].upper():<8}]{Style.RESET_ALL}")
        msg = record.getMessage()

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)

        base = f"{Fore.LIGHTBLACK_EX}[{time_str}]{Style.RESET_ALL} {badge} {msg}"
        if record.exc_text:
            base += f"\n{Fore.RED}{record.exc_text}{Style.RESET_ALL}"
        return base


def print_banner(db_path: str, symbols_count: int, timeframes: tuple, is_testnet: bool, active_count: int):
    net_str = f"{Fore.YELLOW}Testnet{Style.RESET_ALL}" if is_testnet else f"{Fore.GREEN}Mainnet{Style.RESET_ALL}"
    tf_str = ", ".join(t.upper() for t in timeframes)
    
    banner = f"""
{Fore.CYAN}+==============================================================================+
|                            {Fore.WHITE}{Style.BRIGHT}NEOELLA TRADE BOT{Style.RESET_ALL}{Fore.CYAN}                                 |
|         {Fore.LIGHTCYAN_EX}Automated Chart Pattern Scanner & Precision Risk Management{Fore.CYAN}          |
+==============================================================================+{Style.RESET_ALL}
  * {Style.BRIGHT}Environment  :{Style.RESET_ALL} Binance Futures ({net_str})
  * {Style.BRIGHT}Storage      :{Style.RESET_ALL} SQLite WAL Database ({db_path})
  * {Style.BRIGHT}Universe     :{Style.RESET_ALL} {symbols_count} Pairs x {len(timeframes)} Timeframes ({tf_str})
  * {Style.BRIGHT}Active Trades:{Style.RESET_ALL} {Fore.GREEN}{active_count}{Style.RESET_ALL} signals currently tracked
{Fore.LIGHTBLACK_EX}--------------------------------------------------------------------------------{Style.RESET_ALL}"""
    print(banner)


def format_signal_card(symbol: str, tf: str, direction: str, pattern: str, entry: float, sl: float, tp1: float, tp2: float, tp3: float) -> str:
    is_long = direction.upper() == "LONG"
    dir_color = Fore.GREEN if is_long else Fore.RED
    dir_icon = "LONG" if is_long else "SHORT"
    
    risk = abs(entry - sl)
    rr = (abs(tp2 - entry) / risk) if risk > 0 else 0.0

    return f"""
{Fore.CYAN}+-- [NEW SIGNAL DETECTED] -----------------------------------------------------+{Style.RESET_ALL}
|  {Style.BRIGHT}Pair & TF  :{Style.RESET_ALL} {Fore.WHITE}{symbol} ({tf.upper()}){Style.RESET_ALL}
|  {Style.BRIGHT}Pattern    :{Style.RESET_ALL} {Fore.YELLOW}{pattern}{Style.RESET_ALL}  |  {Style.BRIGHT}Bias:{Style.RESET_ALL} {dir_color}{dir_icon}{Style.RESET_ALL}
|  {Style.BRIGHT}Entry Price:{Style.RESET_ALL} {entry:.6g}
|  {Style.BRIGHT}Stop Loss  :{Style.RESET_ALL} {Fore.RED}{sl:.6g}{Style.RESET_ALL}
|  {Style.BRIGHT}Targets    :{Style.RESET_ALL} TP1: {Fore.GREEN}{tp1:.6g}{Style.RESET_ALL} | TP2: {Fore.GREEN}{tp2:.6g}{Style.RESET_ALL} | TP3: {Fore.GREEN}{tp3:.6g}{Style.RESET_ALL}
|  {Style.BRIGHT}Risk/Reward:{Style.RESET_ALL} 1 : {rr:.2f} (to TP2)
{Fore.CYAN}+------------------------------------------------------------------------------+{Style.RESET_ALL}"""

