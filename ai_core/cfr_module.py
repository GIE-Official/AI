"""
cfr_module.py
GIE-Soliton V2.5 "AWACS" — Lead-Lag Cross-Field Radar (CFR)

Purpose:
    Detect "Signal Leakage": when auxiliary manifold AI_log begins
    degrading before the primary manifold, pre-empting a Drift event
    before it manifests in the primary stream.

Algorithm:
    Online bivariate Welford (Chan 1979) for per-stream Pearson ρ between
    primary AI_log and each auxiliary AI_log series.

    Key optimisation — Deferred sqrt:
        sqrt (the most expensive operation in ρ) is computed only at the
        END of each 26-tick batch (is_batch_end=True).  Within the batch,
        M2 accumulators are updated every tick at O(1) cost.  Cached ρ
        values are returned for intermediate ticks.  Maximum staleness of
        ρ: 25 ticks (~8 ms at Binance aggTrade rates) — negligible for a
        Pre-Drift early-warning signal.

    Staleness expiry:
        If auxiliary stream k has not been updated for > STALE_TICKS ticks,
        its weight is forced to 0 (dimension weight collapse).

Trigger:
    pre_drift = True  when:
        ρ_LL > threshold
        AND at least one active stream is "leading" (aux declining while
            primary is stable or rising)

Hard constraints:
    - array.array('d') for all numeric state (no list, no numpy)
    - All math functions localised in __init__ (no global lookup in hot path)
    - Zero dynamic allocation after __init__
    - O(N_aux) per tick

Reference: Ma, C. (2026). The Ma-Chao Equation: Non-Hermitian Geometry and Topological Phase Transitions in Information-Fluid Hydrodynamics.
           ORCID: 0009-0004-2456-9098
"""

import math
import array
from dataclasses import dataclass


# ══════════════════════════════════════════════════════════════════════════════
# Output container  (pre-defined; not allocated per-tick)
# ══════════════════════════════════════════════════════════════════════════════

class CFRResult:
    """
    Lightweight result object.  Reused across ticks by __slots__ to avoid
    per-tick heap allocation.
    """
    __slots__ = ("rho_ll", "pre_drift", "active_n", "leading_k")

    def __init__(self) -> None:
        self.rho_ll    : float = 0.0
        self.pre_drift : bool  = False
        self.active_n  : int   = 0
        self.leading_k : int   = -1


# ══════════════════════════════════════════════════════════════════════════════
# Bivariate Welford buffer layout (per auxiliary stream, 6 doubles):
#
#   Slot  Meaning
#   ────  ─────────────────────────────────────────────────
#   0     n          : observation count (float for FP division)
#   1     mean_x     : running mean of primary AI_log (x)
#   2     mean_y     : running mean of auxiliary AI_log (y)
#   3     M2_x       : Σ(x_i - x̄)² accumulator
#   4     M2_y       : Σ(y_i - ȳ)² accumulator
#   5     C_xy       : Σ(x_i - x̄)(y_i - ȳ) cross-product accumulator
#
# Pearson ρ = C_xy / sqrt(M2_x · M2_y)   [computed only at batch end]
# ══════════════════════════════════════════════════════════════════════════════

_F  = 6          # fields per stream
_N  = 0          # slot indices
_MX = 1
_MY = 2
_VX = 3
_VY = 4
_CXY= 5


# ══════════════════════════════════════════════════════════════════════════════
# CFR Module
# ══════════════════════════════════════════════════════════════════════════════

class CFRModule:
    """
    Lead-Lag Cross-Field Radar.

    Args:
        n_aux        : number of auxiliary manifold streams (default 4)
        threshold    : ρ_LL trigger threshold for Pre-Drift flag (default 0.60)
        min_samples  : minimum observations before ρ is considered valid (30)
        stale_ticks  : ticks without update before stream weight → 0 (default 2)
    """

    def __init__(
        self,
        n_aux       : int   = 4,
        threshold   : float = 0.60,
        min_samples : int   = 30,
        stale_ticks : int   = 2,
    ) -> None:
        # ── Localise math (eliminates global dict lookup in hot path) ─────────
        self._sqrt = math.sqrt

        self.n_aux       = n_aux
        self.threshold   = threshold
        self.min_samples = min_samples
        self.stale_ticks = stale_ticks

        # ── Pre-allocate all state as array.array (zero dynamic alloc) ────────

        # Bivariate Welford state: n_aux × 6 doubles
        self._biv = array.array('d', [0.0] * (n_aux * _F))

        # Per-stream cached ρ  (updated at batch end via deferred sqrt)
        self._rho = array.array('d', [0.0] * n_aux)

        # Last-update global tick index per stream (-999 = never seen)
        self._last_upd = array.array('l', [-999] * n_aux)

        # Previous aux AI_log per stream (for lead-lag direction check)
        self._prev_aux = array.array('d', [0.0] * n_aux)

        # Previous primary AI_log (for lead-lag direction check)
        self._prev_primary : float = 0.0

        # Reusable result object
        self._result = CFRResult()

        # Global tick counter
        self._tick : int = 0

        # ── Cached aggregate output ───────────────────────────────────────────
        self.rho_ll    : float = 0.0
        self.pre_drift : bool  = False

    # ── Hot path ───────────────────────────────────────────────────────────────

    def update(
        self,
        primary_ai_log : float,
        aux_ai_logs    : list,      # list[float], len ≤ n_aux
        is_batch_end   : bool = False,
    ) -> CFRResult:
        """
        Ingest one tick.

        Args:
            primary_ai_log : log10(AI_t) of the primary instrument
            aux_ai_logs    : AI_log values for auxiliary streams.
                             Pass fewer than n_aux to mark missing streams.
            is_batch_end   : True on the 26th tick of a batch.  Only then
                             are sqrt (Pearson ρ) and Pre-Drift evaluated.
                             Every other tick: O(1), no sqrt, no output change.

        Returns:
            Shared CFRResult object (do NOT store across calls).

        Complexity: O(N_aux) per tick.  Zero dynamic allocation.
        """
        # ── Localise for hot path ─────────────────────────────────────────────
        _sqrt      = self._sqrt
        _biv       = self._biv
        _rho       = self._rho
        _last_upd  = self._last_upd
        _prev_aux  = self._prev_aux
        n_aux      = self.n_aux
        tick       = self._tick
        stale      = self.stale_ticks
        min_s      = self.min_samples
        thresh     = self.threshold

        self._tick = tick + 1
        n_provided = len(aux_ai_logs)

        # ── Per-stream bivariate Welford update ───────────────────────────────
        for k in range(n_aux):
            # Staleness guard
            age = tick - _last_upd[k]
            if k < n_provided:
                _last_upd[k] = tick
                y = aux_ai_logs[k]
            else:
                if age > stale:
                    continue          # weight = 0, skip entirely
                y = _prev_aux[k]      # carry-forward last known value

            # Final staleness re-check after carry-forward path
            if tick - _last_upd[k] > stale:
                continue

            b  = k * _F
            x  = primary_ai_log

            # ── Welford bivariate update (Chan & Lewis 1979) ──────────────────
            n_prev = _biv[b + _N]
            n_new  = n_prev + 1.0
            _biv[b + _N] = n_new

            # Deltas against OLD means
            dx = x - _biv[b + _MX]
            dy = y - _biv[b + _MY]

            # Update means
            inv_n = 1.0 / n_new
            _biv[b + _MX] += dx * inv_n
            _biv[b + _MY] += dy * inv_n

            # Residuals against NEW means (critical: use updated means)
            dx2 = x - _biv[b + _MX]
            dy2 = y - _biv[b + _MY]

            # Variance & covariance accumulators
            _biv[b + _VX]  += dx * dx2
            _biv[b + _VY]  += dy * dy2
            _biv[b + _CXY] += dx * dy2   # cross term: old dx × new dy

            _prev_aux[k] = y

        self._prev_primary = primary_ai_log

        # ── Deferred evaluation: only at batch end (26th tick) ────────────────
        if not is_batch_end:
            return self._result   # return cached result unchanged

        # ── Compute ρ_k, ρ_LL, Pre-Drift  (once per 26-tick batch) ───────────
        rho_sum    = 0.0
        weight_sum = 0.0
        leading_k  = -1
        active_n   = 0

        for k in range(n_aux):
            if tick - _last_upd[k] > stale:
                _rho[k] = 0.0
                continue

            b   = k * _F
            n_k = _biv[b + _N]
            if n_k < min_s:
                continue

            active_n += 1

            denom = _biv[b + _VX] * _biv[b + _VY]
            if denom > 1e-14:
                rho_k = _biv[b + _CXY] / _sqrt(denom)
                # Clamp to [-1, 1]
                if   rho_k >  1.0: rho_k =  1.0
                elif rho_k < -1.0: rho_k = -1.0
            else:
                rho_k = 0.0

            _rho[k]     = rho_k
            abs_rho     = rho_k if rho_k >= 0.0 else -rho_k
            rho_sum    += abs_rho
            weight_sum += 1.0

            # Lead-lag direction: aux declining while primary stable or rising
            aux_declining  = _prev_aux[k] < _biv[b + _MY] - 0.02
            primary_stable = primary_ai_log >= self._prev_primary - 0.01
            if (rho_k > thresh and aux_declining
                    and primary_stable and leading_k == -1):
                leading_k = k

        rho_ll = rho_sum / weight_sum if weight_sum > 0.0 else 0.0

        self.rho_ll    = rho_ll
        self.pre_drift = (leading_k != -1) and (rho_ll > thresh)

        # Update shared result object (no heap allocation)
        r = self._result
        r.rho_ll    = rho_ll
        r.pre_drift = self.pre_drift
        r.active_n  = active_n
        r.leading_k = leading_k
        return r

    # ── Utility ────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Full state reset.  Called on Phoenix Standby entry."""
        for i in range(len(self._biv)):
            self._biv[i] = 0.0
        for k in range(self.n_aux):
            self._rho[k]     = 0.0
            self._last_upd[k]= -999
            self._prev_aux[k]= 0.0
        self._prev_primary = 0.0
        self._tick         = 0
        self.rho_ll        = 0.0
        self.pre_drift     = False
        r = self._result
        r.rho_ll = 0.0; r.pre_drift = False
        r.active_n = 0;  r.leading_k = -1


# ── Standalone benchmark ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import time, statistics as st, random
    random.seed(42)

    cfr = CFRModule(n_aux=4, threshold=0.60, min_samples=20)
    N   = 2000

    # Pre-generate data (simulates real path: data arrives, we compute)
    prices = [random.gauss(-0.5, 0.4) for _ in range(26*N)]
    auxs   = [[prices[i]+random.gauss(0,0.05) for _ in range(4)]
               for i in range(26*N)]

    times = []
    idx = 0
    for _ in range(N):
        t0 = time.perf_counter()
        for ti in range(26):
            cfr.update(prices[idx], auxs[idx], is_batch_end=(ti==25))
            idx += 1
        times.append((time.perf_counter()-t0)*1000)

    med = st.median(times)
    p99 = sorted(times)[int(0.99*N)]
    print("=" * 58)
    print("  GIE-Soliton V2.5 — CFR Module Benchmark")
    print("=" * 58)
    print(f"  Batches          : {N} × 26 ticks")
    print(f"  Median / 26-tick : {med:.5f} ms")
    print(f"  p99   / 26-tick  : {p99:.5f} ms")
    print(f"  ρ_LL             : {cfr.rho_ll:.4f}")
    print(f"  Pre-Drift flag   : {cfr.pre_drift}")
    print(f"  Deferred sqrt    : only 1× per 26-tick batch")
    print("=" * 58)
