"""Backtester for the EMA-bounce swing strategy."""

from .engine import Trade, aggregate, backtest_symbol

__all__ = ["Trade", "aggregate", "backtest_symbol"]
