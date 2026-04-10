"""Юнит-тесты CapitalGuard (без сети)."""

from capital import CapitalGuard


def test_session_loss_blocks() -> None:
    g = CapitalGuard(
        max_session_loss_quote=10.0,
        cooldown_after_loss_seconds=0.0,
        max_consecutive_losses=0,
    )
    ok, _ = g.can_open(realized_pnl_quote=-9.0, now_mono=0.0, paper=True)
    assert ok
    ok2, r = g.can_open(realized_pnl_quote=-10.0, now_mono=0.0, paper=True)
    assert not ok2 and "лимит сессии" in r


def test_session_loss_ignored_when_not_paper() -> None:
    g = CapitalGuard(
        max_session_loss_quote=1.0,
        cooldown_after_loss_seconds=0.0,
        max_consecutive_losses=0,
    )
    ok, _ = g.can_open(realized_pnl_quote=-100.0, now_mono=0.0, paper=False)
    assert ok


def test_cooldown() -> None:
    g = CapitalGuard(
        max_session_loss_quote=0.0,
        cooldown_after_loss_seconds=60.0,
        max_consecutive_losses=0,
    )
    g.on_sell_realized_delta(-1.0, now_mono=0.0)
    ok, r = g.can_open(realized_pnl_quote=0.0, now_mono=1.0, paper=True)
    assert not ok and "cooldown" in r
    ok2, _ = g.can_open(realized_pnl_quote=0.0, now_mono=70.0, paper=True)
    assert ok2


def test_streak_without_cooldown() -> None:
    g = CapitalGuard(
        max_session_loss_quote=0.0,
        cooldown_after_loss_seconds=0.0,
        max_consecutive_losses=2,
    )
    g.on_sell_realized_delta(-1.0, now_mono=0.0)
    g.on_sell_realized_delta(-1.0, now_mono=1.0)
    ok, r = g.can_open(realized_pnl_quote=0.0, now_mono=2.0, paper=True)
    assert not ok and "серия" in r


def test_win_resets_streak() -> None:
    g = CapitalGuard(
        max_session_loss_quote=0.0,
        cooldown_after_loss_seconds=0.0,
        max_consecutive_losses=3,
    )
    g.on_sell_realized_delta(-1.0, now_mono=0.0)
    g.on_sell_realized_delta(0.5, now_mono=1.0)
    g.on_sell_realized_delta(-1.0, now_mono=2.0)
    ok, _ = g.can_open(realized_pnl_quote=0.0, now_mono=3.0, paper=True)
    assert ok


if __name__ == "__main__":
    test_session_loss_blocks()
    test_session_loss_ignored_when_not_paper()
    test_cooldown()
    test_streak_without_cooldown()
    test_win_resets_streak()
    print("OK")
