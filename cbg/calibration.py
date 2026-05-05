"""CBG++ calibration: LP-based bestline (Weinberg et al., IMC 2018).

Unit conventions (matching our pipeline):
  - Input distances : kilometers
  - Input RTTs      : milliseconds
  - _curve['max']   : callable, returns maximum plausible distance in **meters**
                      given an RTT in milliseconds
"""

import collections
import warnings

import numpy as np
from scipy import optimize

# Half of the equatorial circumference of the Earth, in meters.
# No target can be farther away than this.
DISTANCE_LIMIT_M = 20037508


class _Line(collections.namedtuple("_Line", ("m", "b"))):
    """Line y = mx + b."""

    def __call__(self, x):
        return self.m * x + self.b


class MinimizationFailedWarning(UserWarning):
    def __init__(self, label, optresult):
        UserWarning.__init__(
            self, label + ": minimization failed: " + optresult.message
        )
        self.details = optresult


def _warn_if_failed(label, result):
    if not result.success:
        warnings.warn(MinimizationFailedWarning(label, result), stacklevel=2)


def discard_infeasible_measurements(src_pid: int, measurements: list):
    """ Filters out observations that are physically impossible, where RTT falls outside
    the bounds of expected distances based on the speed of light or known satellite delays.
    """ 
    filtered_measurements = [m for m in measurements 
                           if (m['rtt'] >= m['distance']/100) and (m['rtt'] <= 480)]

    return filtered_measurements


def discard_infeasible(dists_m, rtts_ms):
    """Filter observations that violate physical propagation speed limits.

    An observation is infeasible if it implies:
      - propagation faster than 200,000 km/s  (upper physical limit)
      - propagation slower than 110,000 km/s after a 55 ms fixed delay
 
    Args:
        dists_m:  distances in meters (1-D numpy array)
        rtts_ms:  RTTs in milliseconds (1-D numpy array)

    Returns:
        Tuple (filtered_dists_m, filtered_rtts_ms).
    """
    feasible = (
        (rtts_ms * 100_000 >= dists_m) &            # <= 200,000 km/s
        ((rtts_ms - 55) * 55_000 <= dists_m)        # >= 110,000 km/s minus 55 ms delay
    )
    return dists_m[feasible], rtts_ms[feasible]


class CBGLinProg:
    """LP-based CBG calibration for a single landmark (source node).

    Computes the "bestline": the line rtt = m*dist + b that is as close as
    possible to — but still *below* — all calibration observations, with a
    non-negative RTT intercept b.  The inverse of this line gives the maximum
    plausible distance to a target for any observed RTT.

    After construction, ``_curve['max']`` is a callable ``_Line(m, b)`` that
    maps RTT (ms) → maximum plausible distance (meters).  ``_curve['min']``
    is always ``_Line(0, 0)`` (CBG provides no data-driven lower bound).

    Args:
        dists_km: distances in km to calibration landmarks (list or array)
        rtts_ms:  corresponding RTTs in milliseconds (list or array)
    """

    def __init__(self, dists_km, rtts_ms):
        dists_m = np.asarray(dists_km, dtype=float) * 1000.0
        rtts_ms = np.asarray(rtts_ms, dtype=float)

        # 1. Filter physically infeasible observations
        dists_m, rtts_ms = discard_infeasible(dists_m, rtts_ms)
        if len(dists_m) == 0:
            raise ValueError("CBGLinProg: no feasible observations after filtering")

        # CBG cannot use distance-0 observations
        mask = dists_m > 0
        dists_m, rtts_ms = dists_m[mask], rtts_ms[mask]
        if len(dists_m) == 0:
            raise ValueError("CBGLinProg: all observations have zero distance")

        # Sort by distance (required for binning)
        order = np.argsort(dists_m)
        dists_m, rtts_ms = dists_m[order], rtts_ms[order]

        # 2. Bin into ~25 km intervals, keep minimum RTT per bin
        edges = np.linspace(dists_m[0], dists_m[-1], 804)
        binds = np.digitize(dists_m, edges)
        nbins = int(binds.max()) - 1

        bin_dists = np.zeros(nbins)
        bin_rtts  = np.zeros(nbins)
        for i in reversed(range(nbins)):
            bin_dists[i] = (edges[i] + edges[i + 1]) / 2.0
            sel = binds == i + 1
            if sel.any():
                bin_rtts[i] = rtts_ms[sel].min()
            elif i < nbins - 1:
                # Propagate from the next higher bin so linprog doesn't see NaN
                bin_rtts[i] = bin_rtts[i + 1]
            else:
                # Empirical maximum: 237.16 ms to traverse half the Earth
                bin_rtts[i] = 237.16
            assert bin_rtts[i] > 0

        # 3. Add artificial constraint at Earth's half-circumference
        cx = np.append(bin_dists, DISTANCE_LIMIT_M)
        cy = np.append(bin_rtts,  237.16)

        # 4. Solve the LP
        coef     = np.array([np.sum(bin_rtts), -np.sum(bin_dists), -float(nbins)])
        constr_A = np.column_stack((np.zeros_like(cx), cx, np.ones_like(cx)))
        constr_B = cy[:, None]
        bounds   = [(1, 1), (1.0 / 100_000, None), (0.0, float(cy.min()))]

        fit = optimize.linprog(coef, A_ub=constr_A, b_ub=constr_B, bounds=bounds)
        _warn_if_failed("CBGLinProg", fit)

        if not fit.success:
            raise RuntimeError(f"CBGLinProg LP did not converge: {fit.message}")

        # Bestline (forward): rtt = m_fwd * dist + b_fwd
        # Inverse  (max dist): dist = (1/m_fwd) * rtt - b_fwd/m_fwd
        m_fwd = fit.x[1]
        b_fwd = fit.x[2]
        m_inv = 1.0 / m_fwd
        b_inv = -m_inv * b_fwd

        self._curve = {
            'max': _Line(m_inv, b_inv),   # RTT (ms) → max distance (m)
            'min': _Line(0.0, 0.0),
        }

 
    def max_distance(self, rtt_ms):
        """Maximum plausible distance in **meters** for a given RTT (ms)."""
        return max(float(self._curve['max'](rtt_ms)), 0.0)

    def distance_range(self, rtts_ms):
        """Return (min_dist_m, max_dist_m) for a collection of RTTs (ms)."""
        min_rtt = float(np.amin(rtts_ms))
        return (0.0, self.max_distance(min_rtt))

    @property
    def slope(self):
        """Slope of the max-distance line (m per ms)."""
        return self._curve['max'].m

    @property
    def intercept(self):
        """Intercept of the max-distance line (meters)."""
        return self._curve['max'].b
