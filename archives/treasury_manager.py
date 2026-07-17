from dataclasses import dataclass
from typing import List, Optional

@dataclass(frozen=True)
class AllocationRequest:
    symbol: str
    current_capital: float
    desired_capital: float
    minimum_capital: float = 0.0
    maximum_capital: float = float('inf')
    priority: Optional[float] = None  # unused in V1, reserved

@dataclass(frozen=True)
class AllocationResult:
    symbol: str
    current_capital: float
    allocated_capital: float
    delta: float
    fulfilled_ratio: float  # 0..1, 1 if fully fulfilled

class TreasuryManager:
    @staticmethod
    def allocate(requests: List[AllocationRequest], available_cash: float) -> List[AllocationResult]:
        # validate available_cash
        if available_cash < 0:
            raise ValueError("available_cash must be non-negative")
        # process each request
        results = []
        # step 1: clamp desired_capital to [min, max]
        clamped_requests = []
        for req in requests:
            if req.minimum_capital > req.maximum_capital:
                raise ValueError(f"min_capital {req.minimum_capital} > max_capital {req.maximum_capital} for {req.symbol}")
            desired = max(req.minimum_capital, min(req.maximum_capital, req.desired_capital))
            # also ensure current is within bounds? we can clamp current too for safety, but we assume it's valid.
            current = max(req.minimum_capital, min(req.maximum_capital, req.current_capital))
            clamped_requests.append((req, current, desired))
        # step 2: compute deltas and separate
        deltas = [(req, current, desired, desired - current) for req, current, desired in clamped_requests]
        # step 3: sum reductions and needs
        freed_cash = 0.0
        total_need = 0.0
        for _, _, _, delta in deltas:
            if delta < 0:
                freed_cash += -delta
            elif delta > 0:
                total_need += delta
        total_available = available_cash + freed_cash
        # step 4: decide allocation
        if total_available >= total_need:
            # fully satisfy all desired
            allocated_list = [(req, current, desired) for req, current, desired, delta in deltas]
        else:
            # ration positive needs proportionally
            allocated_list = []
            for req, current, desired, delta in deltas:
                if delta > 0:
                    # allocate proportionally
                    allocated = current + (delta / total_need) * total_available
                else:
                    # full reduction
                    allocated = desired
                allocated_list.append((req, current, allocated))
        # step 5: build results
        results = []
        for req, current, allocated in allocated_list:
            # clamp allocated to [min, max] just in case
            allocated = max(req.minimum_capital, min(req.maximum_capital, allocated))
            delta = allocated - current
            # fulfilled ratio: if desired == current, ratio 1; else ratio = (allocated - current)/(desired - current) clamped 0..1
            desired = max(req.minimum_capital, min(req.maximum_capital, req.desired_capital))
            if desired == current:
                ratio = 1.0
            else:
                ratio = (allocated - current) / (desired - current)
                ratio = max(0.0, min(1.0, ratio))
            results.append(AllocationResult(
                symbol=req.symbol,
                current_capital=current,
                allocated_capital=allocated,
                delta=delta,
                fulfilled_ratio=ratio
            ))
        return results
