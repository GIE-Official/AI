"""
gie_soliton_v25.py
GIE-Soliton V2.5 "AWACS" — Primary Integration Interface

Implements the spec §4 GIESolitonV25 class with full AWACS sensing layer.

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │  GIESolitonV25.awacs_sensing(tick_data, own_orders)          │
    │                                                              │
    │   tick_data ──► AtomicIntegrityEngine (Moat-protected)       │
    │                  ├── AI_log, ∇AI, cum_vol, delta_p           │
    │                  │                                           │
    │                  ├──► IFF Module   (self-impact strip)       │
    │                  │      └── verdict, damping_factor          │
    │                  │                                           │
    │                  ├──► MSTS Module  (resonance sweep)         │
    │                  │      └── resonance, active_scales         │
    │                  │                                           │
    │                  └──► CFR Module   (lead-lag radar)          │
    │                         └── rho_LL, pre_drift                │
    │                                                              │
    │   Score fusion ──► admissibility_score ∈ [0.0, 1.0]         │
    └──────────────────────────────────────────────────────────────┘

ETL Moat:
    AtomicIntegrityEngine and ThresholdManager are instantiated internally.
    External code MUST NOT modify them.  All output is consumed via the
    structured dict returned by awacs_sensing().

FSM state transitions (V2.5 enhanced per spec Table §3):
    ACTIVE      → PRE_DRIFT   : CFR Pre-Drift flag raised
    ANY         → RESONANCE   : MSTS resonance detected
    DRIFT       → COLLAPSE    : Ψ singular value (MSTS Ψ_t > PSI_SINGULAR)
    ANY         → STANDBY     : ThresholdManager Phoenix Standby
    ANY         → LOCKED      : Physical lock (singularity / floor)

Score fusion:
    base_score  = 1.0  if ThresholdManager admits (AI_log ≤ τ)  else 0.0
    × damping   : IFF damping_factor        (1.0 = no self-impact)
    × resonance : 0.0 if MSTS resonance     (gateway fully constricted)
    × pre_drift : × (1 − 0.5 × rho_LL)     (partial constriction)
    = admissibility_score

Reference: Ma, C. (2026). The Ma-Chao Equation: Non-Hermitian Geometry and Topological Phase Transitions in Information-Fluid Hydrodynamics.
           ORCID: 0009-0004-2456-9098
"""

import math
import sys
import pathlib

# ── Import V2.4 base + V2.5 modules ──────────────────────────────────────────
_HERE = pathlib.Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from atomic_integrity  import AtomicIntegrityEngine
from threshold_manager import AdaptiveThresholdManager, SystemState, GateDecision
from cfr_module        import CFRModule
from msts_module       import MSTSModule
from iff_module        import IFFModule, IFFVerdict


# ══════════════════════════════════════════════════════════════════════════════
# V2.5 FSM States (superset of V2.4)
# ══════════════════════════════════════════════════════════════════════════════

class V25State:
    ACTIVE          = "ACTIVE"
    PRE_DRIFT       = "PRE_DRIFT"       # V2.5: CFR early warning
    RESONANCE       = "RESONANCE"       # V2.5: MSTS gateway constriction
    DRIFT_WARNING   = "DRIFT_WARNING"   # V2.4 inherited
    FLASH_COLLAPSE  = "FLASH_COLLAPSE"  # V2.4 inherited
    PHOENIX_STANDBY = "PHOENIX_STANDBY" # V2.4 inherited
    RECOVERY        = "RECOVERY"        # V2.4 inherited
    LOCKED          = "LOCKED"          # V2.4 inherited (Physical Lock)


# ══════════════════════════════════════════════════════════════════════════════
# Config dataclass
# ══════════════════════════════════════════════════════════════════════════════

class V25Config:
    """All tunable parameters for GIESolitonV25."""

    def __init__(self) -> None:
        # ── AtomicIntegrity ───────────────────────────────────────────────────
        self.ai_window          = 100
        self.tau_percentile     = 0.05
        self.ai_history_size    = 500

        # ── ThresholdManager ──────────────────────────────────────────────────
        self.tau_pct            = 0.05
        self.tm_history_size    = 500

        # ── CFR ───────────────────────────────────────────────────────────────
        self.cfr_n_aux          = 4
        self.cfr_threshold      = 0.60
        self.cfr_min_samples    = 30
        self.cfr_stale_ticks    = 2

        # ── MSTS ──────────────────────────────────────────────────────────────
        self.msts_sigma_mult    = 2.5
        self.msts_resonance_k   = 3
        self.psi_singular       = 500.0   # |Ψ| above this → Collapse trigger

        # ── IFF ───────────────────────────────────────────────────────────────
        self.iff_window         = 26
        self.iff_collapse_thr   = -3.0
        self.iff_impact_coef    = 1e-5
        self.iff_damping_sens   = 2.5
        self.iff_stable_margin  = 0.5


# ══════════════════════════════════════════════════════════════════════════════
# GIE-Soliton V2.5 — Primary Interface
# ══════════════════════════════════════════════════════════════════════════════

class GIESolitonV25:
    """
    V2.5 AWACS integration interface.

    Spec §4 compliance:
        - ETL Moat: AtomicIntegrityEngine and ThresholdManager are internal.
        - awacs_sensing() is the ONLY public computation entry point.
        - All radar modules accessed via self.radar_cfr, self.radar_msts,
          self.iff_loop (per spec naming).

    Usage:
        cfg  = V25Config()
        gie  = GIESolitonV25(cfg)
        score = gie.awacs_sensing(
            tick_data  = (price, volume, timestamp_us),
            own_orders = [(fill_price, fill_vol), ...]   # recent own fills
        )
    """

    def __init__(self, cfg: V25Config | None = None) -> None:
        if cfg is None:
            cfg = V25Config()
        self._cfg = cfg

        # ── Localise math ─────────────────────────────────────────────────────
        self._log10 = math.log10
        self._fabs  = math.fabs

        # ── ETL Moat: core V2.4 engine (DO NOT EXPOSE / MODIFY) ──────────────
        self._engine = AtomicIntegrityEngine(
            window          = cfg.ai_window,
            tau_percentile  = cfg.tau_percentile,
            history_size    = cfg.ai_history_size,
        )
        self._threshold_mgr = AdaptiveThresholdManager(
            tau_percentile = cfg.tau_pct,
            history_size   = cfg.tm_history_size,
        )

        # ── V2.5 AWACS radar modules (spec §4 naming) ─────────────────────────
        self.radar_cfr  = CFRModule(
            n_aux       = cfg.cfr_n_aux,
            threshold   = cfg.cfr_threshold,
            min_samples = cfg.cfr_min_samples,
            stale_ticks = cfg.cfr_stale_ticks,
        )
        self.radar_msts = MSTSModule(
            sigma_multiplier = cfg.msts_sigma_mult,
            resonance_k      = cfg.msts_resonance_k,
        )
        self.iff_loop = IFFModule(
            window              = cfg.iff_window,
            collapse_threshold  = cfg.iff_collapse_thr,
            vol_impact_coef     = cfg.iff_impact_coef,
            damping_sensitivity = cfg.iff_damping_sens,
            stable_margin       = cfg.iff_stable_margin,
        )

        # ── FSM state ─────────────────────────────────────────────────────────
        self.state = V25State.PHOENIX_STANDBY   # per spec §4 __init__ default

        # ── Tick counter (for batch-end detection) ────────────────────────────
        self._tick_in_batch = 0

        # ── Last known good values (for warm-up period pass-through) ──────────
        self._last_ai_log   : float = 0.0
        self._last_nabla    : float = 0.0
        self._last_cum_vol  : float = 0.0
        self._last_delta_p  : float = 0.0
        self._last_score    : float = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    def awacs_sensing(
        self,
        tick_data  : tuple,        # (price: float, vol: float, ts_us: float)
        own_orders : list | None = None,  # [(fill_price, fill_vol), ...]
    ) -> float:
        """
        Execute full AWACS sensing pipeline for one tick.

        Pipeline (per spec §4):
            1. ETL Moat: AtomicIntegrityEngine → AI_log, ∇AI, gate decision
            2. IFF: strip self-impact, classify Friend/Foe/Neutral
            3. MSTS: multi-scale tensor sweep → Resonance_Alert
            4. CFR: lead-lag cross-field radar → Pre-Drift flag
            5. FSM: update V2.5 state transitions
            6. Score fusion → admissibility_score ∈ [0.0, 1.0]

        Args:
            tick_data  : (price, volume, timestamp_us)
            own_orders : list of (fill_price, fill_vol) tuples for own fills
                         in this tick.  Pass None or [] if no own activity.

        Returns:
            admissibility_score ∈ [0.0, 1.0]
                0.0 = fully blocked (locked / resonance / foe-collapse)
                1.0 = fully admitted

        Hard guarantees:
            - Returns 0.0 immediately if physical lock is active
            - Returns 0.0 during PHOENIX_STANDBY
            - Never raises; returns 0.0 on any internal error
        """
        try:
            return self._awacs_inner(tick_data, own_orders or [])
        except Exception:
            return 0.0

    # ── Internal pipeline ──────────────────────────────────────────────────────

    def _awacs_inner(self, tick_data: tuple, own_orders: list) -> float:
        # ── Parse tick ────────────────────────────────────────────────────────
        price    = tick_data[0]
        volume   = tick_data[1]
        ts_us    = tick_data[2] if len(tick_data) > 2 else None

        # ── Batch-end flag (for CFR deferred sqrt) ────────────────────────────
        tb = self._tick_in_batch
        is_batch_end = (tb == 25)
        self._tick_in_batch = (tb + 1) % 26

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # STEP 1 — ETL Moat: AtomicIntegrityEngine (core V2.4, read-only)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        ai_result = self._engine.update(price, volume, ts_us)

        if ai_result is None:
            # Window not yet filled — warm-up, pass through
            self.radar_msts.update(price)   # keep MSTS price history live
            return 0.0

        if ai_result.get("locked"):
            self.state = V25State.LOCKED
            return 0.0

        ai_log    = ai_result["ai_log"]
        nabla     = ai_result["nabla"]
        cum_vol   = ai_result["cum_vol"]
        delta_p   = ai_result["delta_p"]
        ai_raw    = ai_result["ai_raw"]

        self._last_ai_log  = ai_log
        self._last_nabla   = nabla
        self._last_cum_vol = cum_vol
        self._last_delta_p = delta_p

        # ThresholdManager gate decision
        gate : GateDecision = self._threshold_mgr.process(
            ai_log = ai_log,
            nabla  = nabla,
            ai_raw = ai_raw,
        )

        if gate.locked:
            self.state = V25State.LOCKED
            return 0.0

        # Map ThresholdManager state → V25 state (base layer)
        tm_state = gate.state
        if tm_state == SystemState.PHOENIX_STANDBY:
            self.state = V25State.PHOENIX_STANDBY
            return 0.0
        elif tm_state == SystemState.FLASH_COLLAPSE:
            self.state = V25State.FLASH_COLLAPSE
            return 0.0

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # STEP 2 — IFF: strip own execution volume
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        vol_self = sum(o[1] for o in own_orders) if own_orders else 0.0

        iff_r = self.iff_loop.update(
            cum_vol    = cum_vol,
            delta_p    = delta_p,
            vol_self   = vol_self,
            ai_raw_log = ai_log,
        )

        if iff_r.verdict == IFFVerdict.FOE:
            # External structural attack → Phoenix Standby
            self._threshold_mgr.manual_lock("IFF_FOE")
            self.state = V25State.PHOENIX_STANDBY
            return 0.0

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # STEP 3 — MSTS: multi-scale tensor sweep
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        msts_r = self.radar_msts.update(price)

        if msts_r.resonance:
            self.state = V25State.RESONANCE
            return 0.0   # spec §3: immediate gateway constriction = 0

        # Collapse trigger: Ψ singular value (spec §3 row 3)
        if self._fabs(msts_r.psi_t) > self._cfg.psi_singular:
            self._threshold_mgr.manual_lock(
                f"PSI_SINGULAR: |Ψ|={msts_r.psi_t:.1f} > {self._cfg.psi_singular}"
            )
            self.state = V25State.LOCKED
            return 0.0

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # STEP 4 — CFR: lead-lag cross-field radar
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Aux manifolds: use own-impact-adjusted AI_log + noise proxies
        # In production these come from correlated instruments via data feed.
        # Here: synthetic aux = iff-adjusted ai_log + small perturbations
        adj_ai  = iff_r.ai_adj_log
        aux_ais = [adj_ai + (k - 1.5) * 0.1 for k in range(self._cfg.cfr_n_aux)]

        cfr_r = self.radar_cfr.update(
            primary_ai_log = ai_log,
            aux_ai_logs    = aux_ais,
            is_batch_end   = is_batch_end,
        )

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # STEP 5 — FSM state update (V2.5 extended transitions)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if cfr_r.pre_drift and self.state == V25State.ACTIVE:
            self.state = V25State.PRE_DRIFT
        elif not cfr_r.pre_drift and self.state == V25State.PRE_DRIFT:
            self.state = V25State.ACTIVE
        elif tm_state == SystemState.DRIFT_WARNING:
            self.state = V25State.DRIFT_WARNING
        elif tm_state == SystemState.RECOVERY:
            self.state = V25State.RECOVERY
        elif self.state not in (V25State.DRIFT_WARNING, V25State.PRE_DRIFT):
            self.state = V25State.ACTIVE

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # STEP 6 — Score fusion
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Base: gate admitted?
        score = 1.0 if gate.admitted else 0.0

        # IFF damping (FRIEND: reduce own execution speed)
        score *= iff_r.damping_factor

        # Pre-Drift partial constriction: score × (1 − 0.5 × ρ_LL)
        if cfr_r.pre_drift:
            score *= max(0.0, 1.0 - 0.5 * cfr_r.rho_ll)

        self._last_score = score
        return score

    # ── Utility ────────────────────────────────────────────────────────────────

    def force_standby(self) -> None:
        """Manually place system into Phoenix Standby."""
        self._threshold_mgr.manual_lock("MANUAL_STANDBY")
        self.state = V25State.PHOENIX_STANDBY
        self._engine.set_lock(True)
        self.radar_cfr.reset()
        self.radar_msts.reset()
        self.iff_loop.reset()

    def force_recovery(self) -> None:
        """Release physical lock and begin recovery sequence."""
        self._threshold_mgr.manual_release()
        self._engine.set_lock(False)
        self.state = V25State.RECOVERY

    @property
    def status(self) -> dict:
        """Structured diagnostic snapshot (not on hot path)."""
        return {
            "state"          : self.state,
            "last_ai_log"    : self._last_ai_log,
            "last_nabla"     : self._last_nabla,
            "last_score"     : self._last_score,
            "rho_ll"         : self.radar_cfr.rho_ll,
            "pre_drift"      : self.radar_cfr.pre_drift,
            "resonance"      : self.radar_msts.resonance,
            "iff_verdict"    : self.iff_loop.last_verdict.name,
            "tm_state"       : self._threshold_mgr.state.name,
            "admission_rate" : self._engine.admission_rate,
        }


# ── Standalone integration test ────────────────────────────────────────────────
if __name__ == "__main__":
    import time, statistics as st, random
    random.seed(2026)

    cfg = V25Config()
    gie = GIESolitonV25(cfg)

    print("=" * 62)
    print("  GIE-Soliton V2.5 AWACS — Integration Benchmark")
    print("=" * 62)

    # Pre-generate tick data
    N      = 2000
    price  = 60000.0
    prices = []
    for _ in range(26 * N):
        price += random.gauss(0, 5)
        prices.append(price)
    vols   = [abs(random.gauss(1.5, 0.3)) for _ in range(26*N)]
    ts_arr = [i * 300.0 for i in range(26*N)]

    times = []
    idx   = 0
    for _ in range(N):
        t0 = time.perf_counter()
        for _ in range(26):
            gie.awacs_sensing((prices[idx], vols[idx], ts_arr[idx]), [])
            idx += 1
        times.append((time.perf_counter()-t0)*1000)

    med = st.median(times)
    p99 = sorted(times)[int(0.99*N)]
    print(f"  Median / 26-tick : {med:.5f} ms")
    print(f"  p99   / 26-tick  : {p99:.5f} ms")
    print(f"  State            : {gie.state}")
    print(f"  Admission rate   : {gie._engine.admission_rate:.3%}")

    s = gie.status
    print(f"\n  AWACS Status:")
    for k, v in s.items():
        print(f"    {k:<18}: {v}")

    # Resonance injection
    gie2 = GIESolitonV25(V25Config())
    p2 = 60000.0
    for _ in range(200):
        p2 += random.gauss(0, 3)
        gie2.awacs_sensing((p2, 1.5, 0.0), [])
    # inject sharp oscillations
    scores = []
    for _ in range(30):
        p2 += random.gauss(0, 300)
        scores.append(gie2.awacs_sensing((p2, 0.001, 0.0), []))
    print(f"\n  Resonance injection: state={gie2.state}  "
          f"min_score={min(scores):.3f}")
    print("=" * 62)
