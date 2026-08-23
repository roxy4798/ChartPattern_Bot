from __future__ import annotations
from dataclasses import dataclass, field
from bot.models import Signal
from bot.database import Database


@dataclass
class Bucket:
    signals: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    win_pnls: list = field(default_factory=list)
    loss_pnls: list = field(default_factory=list)
    tp1: int = 0
    tp2: int = 0
    tp3: int = 0
    sl: int = 0
    durations: list = field(default_factory=list)

    @property
    def winrate(self) -> float:
        closed = self.wins + self.losses
        return (self.wins / closed * 100) if closed else 0.0

    @property
    def avg_pnl(self) -> float:
        n = self.wins + self.losses
        return (self.total_pnl / n) if n else 0.0

    @property
    def avg_win(self) -> float:
        return sum(self.win_pnls) / len(self.win_pnls) if self.win_pnls else 0.0

    @property
    def avg_loss(self) -> float:
        return sum(self.loss_pnls) / len(self.loss_pnls) if self.loss_pnls else 0.0

    @property
    def avg_duration_sec(self) -> float:
        return sum(self.durations) / len(self.durations) if self.durations else 0.0


def _add(bucket: Bucket, s: Signal):
    bucket.signals += 1
    if s.result == "WIN":
        bucket.wins += 1
        bucket.win_pnls.append(s.pnl_pct or 0.0)
    elif s.result == "LOSS":
        bucket.losses += 1
        bucket.loss_pnls.append(s.pnl_pct or 0.0)
    if s.pnl_pct is not None:
        bucket.total_pnl += s.pnl_pct
    if s.status.value == "TP1_HIT":
        bucket.tp1 += 1
    elif s.status.value == "TP2_HIT":
        bucket.tp2 += 1
    elif s.status.value in ("TP3_HIT", "CLOSED") and s.result == "WIN":
        bucket.tp3 += 1
    elif s.status.value == "SL_HIT":
        bucket.sl += 1
    if s.duration_sec:
        bucket.durations.append(s.duration_sec)


@dataclass
class PerformanceReport:
    overall: Bucket
    by_timeframe: dict
    by_pattern: dict
    best_trade: Signal | None
    worst_trade: Signal | None
    max_consecutive_wins: int
    max_consecutive_losses: int


async def build_report(db: Database) -> PerformanceReport:
    closed = await db.get_all_closed()
    overall = Bucket()
    by_tf: dict[str, Bucket] = {}
    by_pat: dict[str, Bucket] = {}
    best, worst = None, None
    streak = 0
    max_win_streak = 0
    max_loss_streak = 0

    for s in closed:
        _add(overall, s)
        _add(by_tf.setdefault(s.timeframe, Bucket()), s)
        _add(by_pat.setdefault(s.pattern, Bucket()), s)
        if s.pnl_pct is not None:
            if best is None or s.pnl_pct > (best.pnl_pct or float("-inf")):
                best = s
            if worst is None or s.pnl_pct < (worst.pnl_pct or float("inf")):
                worst = s
        if s.result == "WIN":
            streak = streak + 1 if streak >= 0 else 1
            max_win_streak = max(max_win_streak, streak)
        elif s.result == "LOSS":
            streak = streak - 1 if streak <= 0 else -1
            max_loss_streak = max(max_loss_streak, abs(streak))

    return PerformanceReport(
        overall=overall, by_timeframe=by_tf, by_pattern=by_pat,
        best_trade=best, worst_trade=worst,
        max_consecutive_wins=max_win_streak, max_consecutive_losses=max_loss_streak,
    )
