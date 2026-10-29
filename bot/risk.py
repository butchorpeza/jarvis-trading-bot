"""Hard-coded risk rules. The decision layer can NOT override these."""


class RiskManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.day = None
        self.day_start_equity = cfg["start_balance"]
        self.killed = False

    def new_bar(self, ts, equity):
        d = ts.date()
        if d != self.day:
            self.day, self.day_start_equity = d, equity

    def can_open(self, equity, n_open) -> bool:
        if self.killed or n_open >= self.cfg["max_open_positions"]:
            return False
        return equity > self.day_start_equity * (1 - self.cfg["max_daily_loss"])

    def lots(self, equity, sl_distance, contract) -> float:
        risk_money = equity * self.cfg["risk_per_trade"]
        lots = risk_money / (sl_distance * contract)
        return max(0.01, round(lots, 2))
