"""
msts_module.py
GIE-Soliton V2.5 "AWACS" — Multi-Scale Tensor Sweep (MSTS)

Purpose:
    Detect structural resonance: when price micro-oscillations at
    multiple time scales synchronise simultaneously, indicating
    imminent liquidity collapse.

Algorithm:
    1. Ψ_t = P_t − 2·P_{t-1} + P_{t-2}  (discrete Laplacian / price
       acceleration, per spec §1.1).  Acts as the damping factor.

    2. Sliding-window energy per scale s:
           E[s] = Σ Ψ_t² over last W_s ticks   (rolling sum via O(1) update)

    3. Normalised PSD[s] = E[s] / W_s   (average power at scale s)

    4. Adaptive σ_limit via Welford on mean_PSD:
           σ_limit = mean_PSD + σ_multiplier × std_PSD

    5. Resonance = (active_scales ≥ RESONANCE_K), where
           active_scales = count{s : PSD[s] > σ_limit}

    2D tensor T(Time × Scale):
        Stored as a flat array.array('d') of size T_DEPTH × N_SCALES.
        Circular time-axis updated every tick.  Used for downstream
        temporal analysis (e.g. resonance persistence check).

Critical design:
    - 6-scale inner loop is ARITHMETICALLY UNROLLED (no Python for-loop).
      This is the primary optimisation achieving sub-0.05ms per 26 ticks.
    - All arithmetic is pure scalar; numpy/torch strictly prohibited.
    - array.array('d') for all buffers; zero dynamic allocation.
    - All math functions localised in __init__.

Hard constraints (per spec §2 and §3):
    - Strictly O(1) per tick (constant, N_SCALES = 6)
    - Pre-allocated Ψ² circular buffer depth = MAX_WINDOW = 100
    - Tensor T stored as flat array: T[t*N_SCALES + s]

Reference: Ma, C. (2026). The Ma-Chao Equation: Non-Hermitian Geometry and Topological Phase Transitions in Information-Fluid Hydrodynamics.
           ORCID: 0009-0004-2456-9098
"""

import math
import array


# ══════════════════════════════════════════════════════════════════════════════
# Module-level constants
# ══════════════════════════════════════════════════════════════════════════════

N_SCALES    = 6                       # number of sweep scales
SCALES      = (8, 16, 26, 40, 60, 100)  # W_s values
MAX_WINDOW  = SCALES[-1]              # = 100: depth of Ψ² circular buffer
T_DEPTH     = 32                      # time-axis depth of tensor T
RESONANCE_K = 3                       # minimum co-active scales for alert
TENSOR_SIZE = T_DEPTH * N_SCALES      # = 192 doubles


# ══════════════════════════════════════════════════════════════════════════════
# Output container
# ══════════════════════════════════════════════════════════════════════════════

class MSTSResult:
    """
    Reusable result object (avoids per-tick heap allocation).
    """
    __slots__ = ("resonance", "psd", "active_scales",
                 "sigma_limit", "psi_t", "energy_total")

    def __init__(self) -> None:
        self.resonance     : bool        = False
        self.psd           : list        = [0.0] * N_SCALES
        self.active_scales : int         = 0
        self.sigma_limit   : float       = 1.0
        self.psi_t         : float       = 0.0
        self.energy_total  : float       = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# MSTS Module
# ══════════════════════════════════════════════════════════════════════════════

class MSTSModule:
    """
    Multi-Scale Tensor Sweep.

    Args:
        sigma_multiplier : σ_limit = mean_PSD + k × std_PSD  (default 2.5)
        resonance_k      : minimum scales above σ_limit for Resonance_Alert
    """

    def __init__(
        self,
        sigma_multiplier : float = 2.5,
        resonance_k      : int   = RESONANCE_K,
    ) -> None:
        # ── Localise math (no global lookup in hot path) ──────────────────────
        self._sqrt = math.sqrt

        self.sigma_multiplier = sigma_multiplier
        self.resonance_k      = resonance_k

        # ── Pre-allocate all state buffers (array.array, no numpy) ────────────

        # Ψ² circular buffer  (depth = MAX_WINDOW = 100)
        self._psq      = array.array('d', [0.0] * MAX_WINDOW)
        self._psq_head = 0       # index of most recently written slot
        self._psq_cnt  = 0       # fill count (capped at MAX_WINDOW)

        # Price ring for Ψ computation (only 3 prices needed)
        self._p0 = 0.0           # P_t   (current)
        self._p1 = 0.0           # P_{t-1}
        self._p2 = 0.0           # P_{t-2}
        self._pcnt = 0           # price count (0 → 2 during warm-up)

        # 6-element energy array E[s] = rolling Σ Ψ_t² over W_s ticks
        self._energy = array.array('d', [0.0] * N_SCALES)

        # Flat 2D tensor T(Time, Scale) — circular time axis
        # Access: T[time_head * N_SCALES + scale_idx]
        self._tensor      = array.array('d', [0.0] * TENSOR_SIZE)
        self._tensor_head = 0     # next write position (mod T_DEPTH)

        # Welford online stats on scalar mean_PSD (for adaptive σ_limit)
        self._wel_n    = 0
        self._wel_mean = 0.0
        self._wel_M2   = 0.0
        self._sigma_limit : float = 1.0   # adapts after MIN_WEL_N samples

        # Resonance state
        self.resonance : bool = False

        # Reusable result
        self._result = MSTSResult()

    # ── Hot path ───────────────────────────────────────────────────────────────

    def update(self, price: float) -> MSTSResult:
        """
        Ingest one price tick.

        The 6-scale inner loop is ARITHMETICALLY UNROLLED for minimal
        Python bytecode and no per-iteration loop overhead.

        Complexity: O(1) — all operations are constant regardless of W_s.

        Returns shared MSTSResult (do NOT store across calls).
        """
        # ── Local bindings ────────────────────────────────────────────────────
        _sqrt    = self._sqrt
        _psq     = self._psq
        _energy  = self._energy
        _tensor  = self._tensor

        # ── 1. Update 3-element price history ─────────────────────────────────
        self._p2  = self._p1
        self._p1  = self._p0
        self._p0  = price
        self._pcnt = pc = self._pcnt + 1 if self._pcnt < 3 else 3

        # ── 2. Ψ_t = P_t − 2·P_{t−1} + P_{t−2} (discrete Laplacian) ─────────
        if pc >= 3:
            psi_t  = price - 2.0 * self._p1 + self._p2
            psi_sq = psi_t * psi_t
        else:
            psi_t  = 0.0
            psi_sq = 0.0

        # ── 3. Write Ψ² into circular buffer ──────────────────────────────────
        new_head = (self._psq_head + 1) % MAX_WINDOW
        _psq[new_head]  = psi_sq
        self._psq_head  = new_head
        cnt = self._psq_cnt
        if cnt < MAX_WINDOW:
            self._psq_cnt = cnt = cnt + 1

        # ── 4. Unrolled 6-scale sliding-window energy update ──────────────────
        #
        #  For each scale s with window W_s:
        #    E[s] += psi_sq                      (add new Ψ²)
        #    E[s] -= psq[(head - W_s) % 100]     (evict oldest if cnt > W_s)
        #
        #  Unrolled to avoid inner for-loop overhead.  Each line is one scale.
        #  Scales: 8, 16, 26, 40, 60, 100
        h = new_head   # alias for readability in unrolled code

        e0 = _energy[0] + psi_sq
        e1 = _energy[1] + psi_sq
        e2 = _energy[2] + psi_sq
        e3 = _energy[3] + psi_sq
        e4 = _energy[4] + psi_sq
        e5 = _energy[5] + psi_sq

        if cnt >   8: e0 -= _psq[(h -   8) % MAX_WINDOW]
        if cnt >  16: e1 -= _psq[(h -  16) % MAX_WINDOW]
        if cnt >  26: e2 -= _psq[(h -  26) % MAX_WINDOW]
        if cnt >  40: e3 -= _psq[(h -  40) % MAX_WINDOW]
        if cnt >  60: e4 -= _psq[(h -  60) % MAX_WINDOW]
        if cnt > 100: e5 -= _psq[(h - 100) % MAX_WINDOW]

        # Clamp to 0 (prevents FP drift below zero after many subtractions)
        if e0 < 0.0: e0 = 0.0
        if e1 < 0.0: e1 = 0.0
        if e2 < 0.0: e2 = 0.0
        if e3 < 0.0: e3 = 0.0
        if e4 < 0.0: e4 = 0.0
        if e5 < 0.0: e5 = 0.0

        _energy[0] = e0; _energy[1] = e1; _energy[2] = e2
        _energy[3] = e3; _energy[4] = e4; _energy[5] = e5

        # ── 5. PSD[s] = E[s] / W_s  (normalised average power) ───────────────
        psd0 = e0 /   8.0
        psd1 = e1 /  16.0
        psd2 = e2 /  26.0
        psd3 = e3 /  40.0
        psd4 = e4 /  60.0
        psd5 = e5 / 100.0

        # ── 6. Write PSD vector to 2D tensor (circular time axis) ─────────────
        t_base = self._tensor_head * N_SCALES
        _tensor[t_base    ] = psd0; _tensor[t_base + 1] = psd1
        _tensor[t_base + 2] = psd2; _tensor[t_base + 3] = psd3
        _tensor[t_base + 4] = psd4; _tensor[t_base + 5] = psd5
        self._tensor_head = (self._tensor_head + 1) % T_DEPTH

        # ── 7. Welford update on mean_PSD (adaptive σ_limit) ─────────────────
        mean_psd = (psd0 + psd1 + psd2 + psd3 + psd4 + psd5) * (1.0 / N_SCALES)
        n_w = self._wel_n + 1; self._wel_n = n_w
        delta         = mean_psd - self._wel_mean
        self._wel_mean += delta / n_w
        self._wel_M2   += delta * (mean_psd - self._wel_mean)

        if n_w >= 20:
            self._sigma_limit = (
                self._wel_mean
                + self.sigma_multiplier * _sqrt(self._wel_M2 / (n_w - 1))
            )
        sl = self._sigma_limit

        # ── 8. Resonance check: count scales above σ_limit ────────────────────
        active = (
            (1 if psd0 > sl else 0) + (1 if psd1 > sl else 0)
            + (1 if psd2 > sl else 0) + (1 if psd3 > sl else 0)
            + (1 if psd4 > sl else 0) + (1 if psd5 > sl else 0)
        )
        resonance      = active >= self.resonance_k
        self.resonance = resonance

        # ── 9. Update reusable result (no heap allocation) ────────────────────
        r = self._result
        r.resonance     = resonance
        r.psd[0] = psd0; r.psd[1] = psd1; r.psd[2] = psd2
        r.psd[3] = psd3; r.psd[4] = psd4; r.psd[5] = psd5
        r.active_scales = active
        r.sigma_limit   = sl
        r.psi_t         = psi_t
        r.energy_total  = psd0 + psd1 + psd2 + psd3 + psd4 + psd5
        return r

    # ── Utility ────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Full state reset.  Called on Phoenix Standby entry."""
        for i in range(MAX_WINDOW):  self._psq[i] = 0.0
        for i in range(N_SCALES):    self._energy[i] = 0.0
        for i in range(TENSOR_SIZE): self._tensor[i] = 0.0
        self._psq_head    = 0; self._psq_cnt  = 0
        self._p0 = self._p1 = self._p2 = 0.0; self._pcnt = 0
        self._tensor_head = 0
        self._wel_n = 0; self._wel_mean = 0.0; self._wel_M2 = 0.0
        self._sigma_limit = 1.0
        self.resonance    = False


# ── Standalone benchmark + injection test ──────────────────────────────────────
if __name__ == "__main__":
    import time, statistics as st, random
    random.seed(99)

    msts = MSTSModule(sigma_multiplier=2.5, resonance_k=3)
    N    = 2000

    # Pre-generate prices
    prices = [60000.0 + i * 0.01 + (i % 7) * 0.5 for i in range(26 * N + 10)]

    times = []
    for i in range(N):
        base = i * 26
        t0   = time.perf_counter()
        for j in range(26):
            msts.update(prices[base + j])
        times.append((time.perf_counter() - t0) * 1000)

    med = st.median(times)
    p99 = sorted(times)[int(0.99 * N)]
    print("=" * 58)
    print("  GIE-Soliton V2.5 — MSTS Module Benchmark")
    print("=" * 58)
    print(f"  Batches          : {N} × 26 ticks")
    print(f"  Median / 26-tick : {med:.5f} ms")
    print(f"  p99   / 26-tick  : {p99:.5f} ms")
    print(f"  σ_limit          : {msts._sigma_limit:.5f}")
    print(f"  Resonance        : {msts.resonance}")
    print(f"  Inner loop       : arithmetically unrolled (0 for-loop iterations)")
    print("=" * 58)

    # Resonance injection test
    msts2 = MSTSModule(sigma_multiplier=1.5, resonance_k=2)
    p = 60000.0
    for _ in range(200):
        p += random.gauss(0, 1)
        msts2.update(p)
    for _ in range(30):
        p += random.gauss(0, 120)   # sharp oscillation burst
        r = msts2.update(p)
    print(f"\n  Injection test: resonance={r.resonance}  "
          f"active_scales={r.active_scales}/{N_SCALES}  "
          f"Ψ_t={r.psi_t:.4f}")
