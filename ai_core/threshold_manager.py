"""
threshold_manager.py
GIE-Soliton V2.4 — Adaptive Threshold & Physical Lock Manager

Upgrade notes (V2.3 → V2.4):
  - All threshold comparisons operate on log10(AI) values
  - Physical Lock: auto-engages on singularity or hard collapse floor
  - Phoenix Standby state machine (6 states, full lifecycle):
      ACTIVE → DRIFT_WARNING → FLASH_COLLAPSE →
      PHOENIX_STANDBY → RECOVERY → ACTIVE
  - ∇AI (pressure gradient) used as early-warning signal before collapse
  - numpy dependency removed — pure Python for deployment portability
  - GateDecision dataclass provides structured, typed gate output

Reference: Ma, C. (2026). The Ma-Chao Equation: Non-Hermitian Geometry and Topological Phase Transitions in Information-Fluid Hydrodynamics.
           ORCID: 0009-0004-2456-9098
"""

import math
import bisect
import time
from collections import deque
from enum import Enum, auto
from dataclasses import dataclass, field


# ══════════════════════════════════════════════════════════════════════════════
# System State Machine
# ══════════════════════════════════════════════════════════════════════════════

class SystemState(Enum):
    """
    Full lifecycle state of the GIE execution system.

    Transition diagram (Ma 2026 §6 — Operational Protocol):

        ACTIVE ──(∇AI sustained negative)──────────► DRIFT_WARNING
        DRIFT_WARNING ──(AI_log < collapse floor)──► FLASH_COLLAPSE
        DRIFT_WARNING ──(∇AI recovers)─────────────► ACTIVE
        FLASH_COLLAPSE ──(auto 1-tick)──────────────► PHOENIX_STANDBY
        PHOENIX_STANDBY ──(liquidity rebuilds)──────► RECOVERY
        RECOVERY ──(stability confirmed N ticks)────► ACTIVE
        RECOVERY ──(relapse)────────────────────────► PHOENIX_STANDBY

        Any state ──(singularity / hard floor)──────► LOCKED
        LOCKED ──(manual_release())─────────────────► ACTIVE
    """
    ACTIVE          = auto()   # normal operation, gate evaluating
    DRIFT_WARNING   = auto()   # ∇AI falling fast — pre-collapse signal
    FLASH_COLLAPSE  = auto()   # AI_log < hard floor — liquidity void
    PHOENIX_STANDBY = auto()   # suspended, awaiting liquidity rebuild
    RECOVERY        = auto()   # liquidity rebuilding, observation mode
    LOCKED          = auto()   # hard physical lock — all activation refused


@dataclass
class GateDecision:
    """Structured, typed output from a single admissibility evaluation."""
    admitted      : bool
    ai_log        : float
    tau           : float | None
    nabla         : float
    state         : SystemState
    locked        : bool
    margin        : float | None = None   # tau - ai_log  (positive ⟹ admitted)
    lock_reason   : str          = ""


# ══════════════════════════════════════════════════════════════════════════════
# Adaptive Threshold Manager  (V2.4)
# ══════════════════════════════════════════════════════════════════════════════

class AdaptiveThresholdManager:
    """
    Manages adaptive gate threshold τ and the Physical Lock / Phoenix Standby
    state machine for GIE-Soliton V2.4.

    Admissibility gate (log-space):
        𝒜 = { t : AI_log_t ≤ τ }
        τ  = percentile(trailing AI_log history, p)   [p = 5% primary spec]

    Physical Lock triggers (auto-engage):
      1. Liquidity singularity : ai_raw < SINGULARITY_FLOOR (1e-6)
         → degenerate tick, effectively zero volume
      2. Hard collapse floor   : ai_log < AI_LOG_COLLAPSE_FLOOR (-3.0)
         → log10(AI) below physically meaningful bound
      3. Sustained ∇AI drop   : nabla < NABLA_THRESHOLD for NABLA_WINDOW ticks
         → state transitions to DRIFT_WARNING (not full lock, early warning)

    Phoenix Standby exit conditions:
      - ai_log > AI_LOG_RECOVERY_FLOOR (-1.0)  AND  nabla ≥ 0
      - Must hold for RECOVERY_TICKS consecutive ticks
    """

    # ── Physical thresholds ────────────────────────────────────────────────
    SINGULARITY_FLOOR       =  1e-6    # raw AI: zero-volume degenerate tick
    AI_LOG_COLLAPSE_FLOOR   = -3.0    # log10(AI) hard collapse (AI < 0.001)
    AI_LOG_RECOVERY_FLOOR   = -1.0    # log10(AI) recovery threshold (AI > 0.1)
    NABLA_COLLAPSE_THRESHOLD= -0.05   # ∇AI/µs: sustained drop rate → warning
    NABLA_WINDOW            =  5      # consecutive ticks of neg ∇AI for warning
    RECOVERY_TICKS          = 20      # consecutive stable ticks to exit standby
    MIN_HISTORY             = 50      # minimum samples before τ is computed

    def __init__(
        self,
        tau_percentile : float = 0.05,
        history_size   : int   = 500,
    ) -> None:
        if not (0 < tau_percentile < 1):
            raise ValueError("tau_percentile must be in (0, 1)")

        self.tau_pct     = tau_percentile
        self._history    : deque[float] = deque(maxlen=history_size)
        self._sorted     : list[float]  = []   # bisect-maintained sorted mirror
        self.current_tau : float | None = None
        self.state       : SystemState  = SystemState.ACTIVE
        self._lock_reason: str          = ""

        # ── State machine counters ─────────────────────────────────────────
        self._neg_nabla_streak  : int = 0
        self._recovery_streak   : int = 0

        # ── Lifetime diagnostics ───────────────────────────────────────────
        self.lock_count    : int = 0
        self.standby_count : int = 0
        self.recover_count : int = 0

    # ── Primary API ────────────────────────────────────────────────────────

    def process(
        self,
        ai_log : float,
        nabla  : float,
        ai_raw : float = 1.0,
    ) -> GateDecision:
        """
        Ingest one log-space AI observation and return a GateDecision.

        Args:
            ai_log : log10(AI_t)  — output of AtomicIntegrityEngine
            nabla  : ∇AI_t        — pressure gradient (per µs)
            ai_raw : raw AI_t     — used for singularity detection only

        Returns:
            GateDecision with full state context and typed admission verdict.
        """
        # ── 1. Physical lock check ─────────────────────────────────────────
        triggered, reason = self._check_physical_lock(ai_log, ai_raw)
        if triggered:
            self._engage_lock(reason)

        # ── 2. State machine advance ───────────────────────────────────────
        self._advance_state(ai_log, nabla)

        # ── 3. Update threshold history (skip during hard lock / standby) ──
        if self.state not in (SystemState.LOCKED, SystemState.FLASH_COLLAPSE):
            self._push_history(ai_log)
            self.current_tau = self._compute_tau()

        # ── 4. Gate decision ───────────────────────────────────────────────
        locked   = self.state in (SystemState.LOCKED,
                                   SystemState.PHOENIX_STANDBY,
                                   SystemState.FLASH_COLLAPSE)
        admitted = (
            not locked
            and self.current_tau is not None
            and ai_log <= self.current_tau
        )
        margin = (self.current_tau - ai_log) \
                 if self.current_tau is not None else None

        return GateDecision(
            admitted    = admitted,
            ai_log      = ai_log,
            tau         = self.current_tau,
            nabla       = nabla,
            state       = self.state,
            locked      = locked,
            margin      = margin,
            lock_reason = self._lock_reason,
        )

    def manual_lock(self, reason: str = "manual") -> None:
        """Engage physical lock via external command."""
        self._engage_lock(reason)

    def manual_release(self) -> None:
        """Release physical lock and return to ACTIVE."""
        if self.state == SystemState.LOCKED:
            self.state              = SystemState.ACTIVE
            self._lock_reason       = ""
            self._recovery_streak   = 0
            self._neg_nabla_streak  = 0

    def reset(self) -> None:
        """Full reset — clears history and returns to ACTIVE."""
        self._history.clear()
        self._sorted.clear()
        self.current_tau            = None
        self.state                  = SystemState.ACTIVE
        self._lock_reason           = ""
        self._neg_nabla_streak      = 0
        self._recovery_streak       = 0

    # ── State machine ──────────────────────────────────────────────────────

    def _advance_state(self, ai_log: float, nabla: float) -> None:
        """
        Drive the 6-state machine based on current AI_log and ∇AI.

        LOCKED states only exit via manual_release().
        """
        if self.state == SystemState.LOCKED:
            return

        # Track sustained negative ∇AI streak
        if nabla < self.NABLA_COLLAPSE_THRESHOLD:
            self._neg_nabla_streak += 1
        else:
            self._neg_nabla_streak = 0

        if self.state == SystemState.ACTIVE:
            if self._neg_nabla_streak >= self.NABLA_WINDOW:
                self.state = SystemState.DRIFT_WARNING

        elif self.state == SystemState.DRIFT_WARNING:
            if ai_log < self.AI_LOG_COLLAPSE_FLOOR:
                self.state = SystemState.FLASH_COLLAPSE
                self.standby_count += 1
            elif self._neg_nabla_streak == 0:
                self.state = SystemState.ACTIVE   # gradient recovered

        elif self.state == SystemState.FLASH_COLLAPSE:
            # Auto-transition: collapse is a 1-tick transient state
            self.state = SystemState.PHOENIX_STANDBY

        elif self.state == SystemState.PHOENIX_STANDBY:
            if ai_log > self.AI_LOG_RECOVERY_FLOOR and nabla >= 0:
                self._recovery_streak += 1
                if self._recovery_streak >= self.RECOVERY_TICKS:
                    self.state = SystemState.RECOVERY
                    self.recover_count += 1
                    self._recovery_streak = 0
            else:
                self._recovery_streak = 0

        elif self.state == SystemState.RECOVERY:
            if ai_log > self.AI_LOG_RECOVERY_FLOOR and nabla >= 0:
                self._recovery_streak += 1
                if self._recovery_streak >= self.RECOVERY_TICKS:
                    self.state = SystemState.ACTIVE
                    self._recovery_streak = 0
            else:
                # Relapse — return to standby
                self.state = SystemState.PHOENIX_STANDBY
                self._recovery_streak = 0

    def _check_physical_lock(
        self, ai_log: float, ai_raw: float
    ) -> tuple[bool, str]:
        """Three physical lock triggers. Returns (should_lock, reason)."""
        if ai_raw < self.SINGULARITY_FLOOR:
            return True, (f"SINGULARITY: raw_ai={ai_raw:.2e} "
                          f"< floor={self.SINGULARITY_FLOOR:.0e}")
        if ai_log < self.AI_LOG_COLLAPSE_FLOOR:
            return True, (f"COLLAPSE_FLOOR: ai_log={ai_log:.3f} "
                          f"< floor={self.AI_LOG_COLLAPSE_FLOOR}")
        return False, ""

    def _engage_lock(self, reason: str) -> None:
        if self.state != SystemState.LOCKED:
            self.state        = SystemState.LOCKED
            self._lock_reason = reason
            self.lock_count  += 1

    # ── Threshold computation ──────────────────────────────────────────────

    def _push_history(self, ai_log: float) -> None:
        """Maintain FIFO deque + parallel bisect-sorted list."""
        if len(self._history) == self._history.maxlen:
            oldest = self._history[0]
            pos    = bisect.bisect_left(self._sorted, oldest)
            if pos < len(self._sorted) and self._sorted[pos] == oldest:
                self._sorted.pop(pos)
        self._history.append(ai_log)
        bisect.insort(self._sorted, ai_log)

    def _compute_tau(self) -> float | None:
        """
        O(1) percentile lookup on the always-sorted list.

        V2.4: list is bisect-maintained — no sort call needed.
        τ reflects the current tick's data with zero lag.
        """
        n = len(self._sorted)
        if n < self.MIN_HISTORY:
            return None
        idx = max(0, int(self.tau_pct * n) - 1)
        return self._sorted[idx]

    # ── Diagnostics ────────────────────────────────────────────────────────

    @property
    def status_str(self) -> str:
        icons = {
            SystemState.ACTIVE          : "✓  ACTIVE",
            SystemState.DRIFT_WARNING   : "⚠  DRIFT_WARNING",
            SystemState.FLASH_COLLAPSE  : "✗  FLASH_COLLAPSE",
            SystemState.PHOENIX_STANDBY : "⏸  PHOENIX_STANDBY",
            SystemState.RECOVERY        : "↑  RECOVERY",
            SystemState.LOCKED          : "🔒 LOCKED",
        }
        s   = icons.get(self.state, self.state.name)
        tau = f"{self.current_tau:.4f}" if self.current_tau else "—"
        return (f"[ThresholdManager] {s}  τ={tau}  "
                f"locks={self.lock_count}  "
                f"standbys={self.standby_count}  "
                f"recoveries={self.recover_count}")


# ── Standalone demo ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import random
    random.seed(2026)

    mgr = AdaptiveThresholdManager(tau_percentile=0.05)

    print("=" * 62)
    print("  GIE-Soliton V2.4 — ThresholdManager State Machine Demo")
    print("=" * 62)

    # Phase 1: normal market
    for _ in range(120):
        ai_log = random.gauss(-0.5, 0.8)
        mgr.process(ai_log, nabla=random.gauss(0, 0.001), ai_raw=10**ai_log)
    print(f"\nPhase 1 (Normal):      {mgr.status_str}")

    # Phase 2: drift warning (sustained negative ∇AI)
    for _ in range(8):
        mgr.process(-0.8, nabla=-0.08, ai_raw=0.16)
    print(f"Phase 2 (Drift):       {mgr.status_str}")

    # Phase 3: collapse
    d = mgr.process(-4.5, nabla=-0.25, ai_raw=3e-5)
    print(f"Phase 3 (Collapse):    {mgr.status_str}")
    print(f"  └─ lock_reason: {d.lock_reason}")

    # Phase 4: manual release + recovery
    mgr.manual_release()
    for _ in range(45):
        d = mgr.process(-0.3, nabla=0.003, ai_raw=0.5)
    print(f"Phase 4 (Recovery):    {mgr.status_str}")

    print(f"\nFinal: admitted={d.admitted}  "
          f"τ={d.tau:.4f}  state={d.state.name}")
    print("=" * 62)
