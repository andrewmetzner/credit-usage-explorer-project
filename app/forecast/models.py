from dataclasses import dataclass


@dataclass
class PriceModel:
    cost_per_credit: float = 0.0
    available_credits: float = 0.0
    total_credit_cost: float = 0.0

    def estimated_credit_value(self) -> float:
        return self.available_credits * self.cost_per_credit

    def remaining_credit_cost(self, used_credits: float = 0.0) -> float:
        return max(0.0, self.available_credits - used_credits) * self.cost_per_credit
