"""
iff_module.py
GIE-Soliton V2.5 "AWACS" — IFF (Identification Friend or Foe) Feedback Loop

Purpose:
    Distinguish self-induced market impact from external structural collapse.
    Prevents the execution system from entering a feedback loop where its
    own orders degrade AI_log, triggering a self-induced Flash Collapse.

Core formula (per spec §2.3):
    AI_raw      = M_t / (|ΔP_t| + ε)
    AI_adjusted = (M_t − Vol_self) / (|ΔP_t − Î_self| + ε)

    where:
        Vol_self  = volume of own orders in [t-W, t]
        Î_self    = estimated price impact of own volume
                    (linear model: Î_self = VOL_IMPACT_COEF × Vol_self)

Verdict logic:
    ┌─────────────────────────────────┬───────────────────────────────┐
    │ Condition                       │ Verdict  │ Action             │
    ├─────────────────────────────────┼──────────┼────────────────────┤
    │ AI_raw collapsed AND            │ FRIEND   │ Execution Damping  │
    │ AI_adjusted stable              │          │ (reduce own vol)   │
    ├─────────────────────────────────┼──────────┼────────────────────┤
    │ AI_raw collapsed AND            │ FOE      │ Phoenix Standby    │
    │ AI_adjusted also collapsed      │          │ (external attack)  │
    ├─────────────────────────────────┼──────────┼────────────────────┤
    │ Neither collapsed               │ NEUTRAL  │ Normal operation   │
    └─────────────────────────────────┴──────────┴────────────────────┘

    Collapse threshold: AI_log < COLLAPSE_THRESHOLD (default -3.0, log-space)

Damping output:
    damping_factor ∈ [0.0, 1.0]
    Next execution volume should be multiplied by damping_factor.

    Computed as: max(0, 1 − Vol_self_fraction × DAMPING_SENSITIVITY)
    where Vol_self_fraction = Vol_self / max(M_t, ε)

Hard constraints:
    - array.array('d') rolling buffers; zero dynamic allocation in hot path
    - All math functions localised in __init__
    - O(1) per tick (deque-based rolling window with O(1) sum maintenance)

Reference: Ma, C. (2026). The Ma-Chao Equation: Non-Hermitian Geometry and Topological Phase Transitions in Information-Fluid Hydrodynamics.
           ORCID: 0009-0004-2456-9098
"""

import math
import array
from enum import IntEnum


# ══════════════════════════════════════════════════════════════════════════════
# IFF Verdict
# ══════════════════════════════════════════════════════════════════════════════

class IFFVerdict(IntEnum):
    NEUTRAL = 0   # no collapse detected; normal operation
    FRIEND  = 1   # self-induced collapse; initiate Execution Damping
    FOE     = 2   # external collapse; initiate Phoenix Standby


# ══════════════════════════════════════════════════════════════════════════════
# Output container
# ══════════════════════════════════════════════════════════════════════════════

class IFFResult:
    """
    Reusable result object (no per-tick heap allocation).
    """
    __slots__ = (
        "verdict", "damping_factor",
        "ai_raw_log", "ai_adj_log",
        "vol_self_fraction",
        "friend_confidence",       # ∈ [0, 1]: how "friend-like" the event is
    )

    def __init__(self) -> None:
        self.verdict           : IFFVerdict = IFFVerdict.NEUTRAL
        self.damping_factor    : float      = 1.0
        self.ai_raw_log        : float      = 0.0
        self.ai_adj_log        : float      = 0.0
        self.vol_self_fraction : float      = 0.0
        self.friend_confidence : float      = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# IFF Module
# ══════════════════════════════════════════════════════════════════════════════

class IFFModule:
    """
    Self-Impact Sensing & Friend-or-Foe Identification.

    Args:
        window             : rolling window for own-volume accumulation
        collapse_threshold : AI_log below this → "collapsed"
                             (matches ThresholdManager.AI_LOG_COLLAPSE_FLOOR)
        vol_impact_coef    : linear price-impact coefficient Î_self/Vol_self
        damping_sensitivity: multiplier controlling how aggressively vol
                             is cut when FRIEND is declared
        stable_margin      : AI_adjusted must exceed AI_raw by at least this
                             margin to declare FRIEND (prevents false positives)
    """

    EPSILON = 1e-9

    def __init__(
        self,
        window              : int   = 26,
        collapse_threshold  : float = -3.0,
        vol_impact_coef     : float = 1e-5,
        damping_sensitivity : float = 2.5,
        stable_margin       : float = 0.5,
    ) -> None:
        # ── Localise math ─────────────────────────────────────────────────────
        self._log10 = math.log10
        self._fabs  = math.fabs

        self.W                   = window
        self.collapse_threshold  = collapse_threshold
        self.vol_impact_coef     = vol_impact_coef
        self.damping_sensitivity = damping_sensitivity
        self.stable_margin       = stable_margin

        # ── Pre-allocate own-volume rolling buffer ────────────────────────────
        # Circular buffer of length W; O(1) sum via running accumulator
        self._vol_buf  = array.array('d', [0.0] * window)
        self._vol_head = 0        # next write position
        self._vol_sum  = 0.0      # running sum (O(1) maintenance)
        self._vol_cnt  = 0        # fill count (< W during warm-up)

        # Pre-allocate own delta_p estimates (for Î_self rolling average)
        self._dp_buf   = array.array('d', [0.0] * window)
        self._dp_head  = 0
        self._dp_sum   = 0.0
        self._dp_cnt   = 0

        # Reusable result
        self._result = IFFResult()

        # ── State ─────────────────────────────────────────────────────────────
        self.last_verdict   : IFFVerdict = IFFVerdict.NEUTRAL
        self.damping_active : bool       = False

    # ── Hot path ───────────────────────────────────────────────────────────────

    def update(
        self,
        cum_vol   : float,   # M_t: total rolling cumulative volume (from AtomicIntegrityEngine)
        delta_p   : float,   # |ΔP_t|: net price displacement over window
        vol_self  : float,   # own order volume executed in current tick
        ai_raw_log: float,   # AI_log from AtomicIntegrityEngine (log10 space)
    ) -> IFFResult:
        """
        Process one tick and return IFF verdict.

        Args:
            cum_vol    : M_t from AtomicIntegrityEngine (rolling vol sum)
            delta_p    : |ΔP_t| from AtomicIntegrityEngine
            vol_self   : own order volume filled this tick (0 if no fill)
            ai_raw_log : AI_log_t = log10(M_t / (|ΔP_t| + ε))

        Returns:
            Shared IFFResult (do NOT store across calls).

        Complexity: O(1), zero dynamic allocation.
        """
        # ── Local bindings ────────────────────────────────────────────────────
        _log10  = self._log10
        _fabs   = self._fabs
        EPSILON = self.EPSILON
        W       = self.W

        # ── 1. Roll own-volume window (O(1) sum maintenance) ──────────────────
        old_h  = self._vol_head
        old_v  = self._vol_buf[old_h]          # value being evicted
        self._vol_buf[old_h] = vol_self
        self._vol_head = (old_h + 1) % W

        if self._vol_cnt < W:
            self._vol_sum += vol_self
            self._vol_cnt += 1
        else:
            self._vol_sum += vol_self - old_v   # evict oldest

        vol_self_window = self._vol_sum         # Σ own vol over [t-W, t]

        # ── 2. Estimated own price impact: Î_self = k × Vol_self_window ───────
        i_self = self.vol_impact_coef * vol_self_window

        # ── 3. Adjusted AI (own-impact stripped) ──────────────────────────────
        #    AI_adjusted = (M_t − Vol_self) / (|ΔP_t − Î_self| + ε)
        m_adj     = max(cum_vol - vol_self_window, EPSILON)
        dp_adj    = _fabs(delta_p - i_self)
        ai_adj    = m_adj / (dp_adj + EPSILON)
        ai_adj_log = _log10(max(ai_adj, EPSILON))

        # ── 4. Collapse detection ──────────────────────────────────────────────
        thr       = self.collapse_threshold
        raw_coll  = ai_raw_log  < thr
        adj_coll  = ai_adj_log  < thr

        # ── 5. Friend confidence = how much AI_adjusted exceeds AI_raw ────────
        friend_conf = max(0.0, min(1.0,
            (ai_adj_log - ai_raw_log) / max(_fabs(ai_raw_log), EPSILON)
        ))

        # ── 6. Verdict ─────────────────────────────────────────────────────────
        if raw_coll and not adj_coll and (ai_adj_log - ai_raw_log) >= self.stable_margin:
            verdict = IFFVerdict.FRIEND
        elif raw_coll and adj_coll:
            verdict = IFFVerdict.FOE
        else:
            verdict = IFFVerdict.NEUTRAL

        self.last_verdict   = verdict
        self.damping_active = (verdict == IFFVerdict.FRIEND)

        # ── 7. Damping factor ──────────────────────────────────────────────────
        #   For FRIEND: reduce execution volume proportionally to self-fraction
        #   For others: no damping (factor = 1.0)
        vol_frac = vol_self_window / max(cum_vol, EPSILON)
        if verdict == IFFVerdict.FRIEND:
            damping = max(0.0, 1.0 - vol_frac * self.damping_sensitivity)
        else:
            damping = 1.0

        # ── 8. Populate reusable result ───────────────────────────────────────
        r = self._result
        r.verdict            = verdict
        r.damping_factor     = damping
        r.ai_raw_log         = ai_raw_log
        r.ai_adj_log         = ai_adj_log
        r.vol_self_fraction  = vol_frac
        r.friend_confidence  = friend_conf
        return r

    def reset(self) -> None:
        """Full state reset. Called on Phoenix Standby entry."""
        for i in range(self.W):
            self._vol_buf[i] = 0.0
            self._dp_buf[i]  = 0.0
        self._vol_head      = 0; self._vol_sum = 0.0; self._vol_cnt = 0
        self._dp_head       = 0; self._dp_sum  = 0.0; self._dp_cnt  = 0
        self.last_verdict   = IFFVerdict.NEUTRAL
        self.damping_active = False


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import time, statistics as st, random
    random.seed(7)

    iff   = IFFModule(window=26, collapse_threshold=-3.0,
                      vol_impact_coef=1e-5, damping_sensitivity=2.5)
    N     = 2000
    times = []

    # Pre-generate tick data
    cum_vols   = [random.gauss(1500, 200) for _ in range(26*N)]
    delta_ps   = [abs(random.gauss(5, 2)) for _ in range(26*N)]
    vol_selfs  = [max(0, random.gauss(50, 20)) for _ in range(26*N)]

    import math
    ai_raws = [math.log10(max(cum_vols[i]/(delta_ps[i]+1e-9), 1e-9))
               for i in range(26*N)]

    idx = 0
    for _ in range(N):
        t0 = time.perf_counter()
        for _ in range(26):
            iff.update(cum_vols[idx], delta_ps[idx],
                       vol_selfs[idx], ai_raws[idx])
            idx += 1
        times.append((time.perf_counter()-t0)*1000)

    print("=" * 58)
    print("  GIE-Soliton V2.5 — IFF Module Benchmark")
    print("=" * 58)
    print(f"  Median / 26-tick : {st.median(times):.5f} ms")
    print(f"  p99   / 26-tick  : {sorted(times)[int(0.99*N)]:.5f} ms")

    # Scenario: FRIEND test
    iff2 = IFFModule(window=26, collapse_threshold=-3.0,
                     vol_impact_coef=1e-5, stable_margin=0.5)
    # Inject self-impact scenario: own vol is large, adj AI recovers
    r = iff2.update(
        cum_vol    = 1.0,     # very low total vol → AI_raw collapses
        delta_p    = 10.0,
        vol_self   = 0.8,     # 80% is own → after strip, M_adj = 0.2
        ai_raw_log = -4.0,    # collapsed
    )
    print(f"\n  FRIEND test: verdict={r.verdict.name}  "
          f"damping={r.damping_factor:.3f}  "
          f"AI_adj_log={r.ai_adj_log:.3f}")

    # Scenario: FOE test
    r2 = iff2.update(
        cum_vol    = 100.0,
        delta_p    = 5000.0,  # massive external move
        vol_self   = 0.5,     # tiny own vol
        ai_raw_log = -4.5,
    )
    print(f"  FOE   test: verdict={r2.verdict.name}  "
          f"damping={r2.damping_factor:.3f}  "
          f"AI_adj_log={r2.ai_adj_log:.3f}")
    print("=" * 58)
