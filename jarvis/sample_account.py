"""Offline stand-in for --sample demos: no network, no orders ever."""
from .config import Settings
from .models import TradePlan


class SampleAccount:
    dry_run = True

    def __init__(self, cfg: Settings):
        self.cfg = cfg

    def equity(self) -> float:
        return self.cfg.sim_equity

    def open_symbols(self) -> set[str]:
        return set()

    def submit(self, plan: TradePlan, today_submitted: set[str]) -> str:
        if not plan.risk_approved:
            raise ValueError("Risk Manager did not approve this plan.")
        return "DRY_RUN"
