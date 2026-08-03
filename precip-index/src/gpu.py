"""
GPU acceleration module for SPI/SPEI computation.

Provides CuPy-based GPU-accelerated replacements for the vectorized CPU
functions in compute.py. Falls back gracefully to CPU if CuPy is unavailable.

Key accelerated operations (all three scale linearly with n_lat * n_lon):
  - rolling_sum_3d   : cumsum-based rolling window along time axis
  - compute_gamma_params : method-of-moments gamma fitting per calendar period
  - transform_to_normal  : gamma CDF + inverse-normal transform

GPU memory note:
  For TerraClimate global (4320 x 8640), one float32 time-slice is ~150 MB.
  A full 67-year monthly dataset is ~4 GB. Process in spatial chunks (via
  ChunkedProcessor) so each chunk fits in GPU VRAM.

Usage:
    from gpu import GPU_AVAILABLE
    if GPU_AVAILABLE:
        from gpu import rolling_sum_3d_gpu, compute_gamma_params_gpu, transform_to_normal_gpu

---
Author: Benny Istanto, GOST/DEC Data Group/The World Bank
"""

import contextlib
import numpy as np

# ---------------------------------------------------------------------------
# GPU availability detection
# ---------------------------------------------------------------------------

GPU_AVAILABLE: bool = False
_cp = None          # cupy module reference
_cpspecial = None   # cupyx.scipy.special module reference

try:
    import cupy as cp
    import cupyx.scipy.special as cpspecial

    # Quick sanity-check: allocate a tiny array to confirm a device exists
    _test = cp.array([1.0], dtype=cp.float32)
    del _test

    GPU_AVAILABLE = True
    _cp = cp
    _cpspecial = cpspecial
except Exception:
    pass


def gpu_info() -> str:
    """Return a one-line description of GPU status."""
    if not GPU_AVAILABLE:
        return "GPU not available (CuPy not installed or no CUDA device found)"
    try:
        dev = _cp.cuda.Device()
        props = _cp.cuda.runtime.getDeviceProperties(dev.id)
        name = props["name"].decode()
        mem_gb = props["totalGlobalMem"] / 1024**3
        return f"GPU available: {name} ({mem_gb:.1f} GB VRAM)"
    except Exception as exc:
        return f"GPU available but could not query properties: {exc}"


# ---------------------------------------------------------------------------
# Rolling sum  (replaces _rolling_sum_3d in compute.py)
# ---------------------------------------------------------------------------

def rolling_sum_3d_gpu(
    data: np.ndarray,
    scale: int,
    dtype
) -> np.ndarray:
    """
    GPU-accelerated rolling sum for a 3-D array (time, lat, lon).

    Uses a cumulative-sum approach (O(n) in time) identical to the CPU version
    in compute._rolling_sum_3d, but executed on the GPU.

    :param data: 3-D numpy array (time, lat, lon)
    :param scale: rolling window size (number of time steps)
    :param dtype: output dtype (np.float32 or np.float64)
    :return: 3-D numpy array with rolling sums; first (scale-1) steps are NaN
    """
    cp = _cp
    n_time, n_lat, n_lon = data.shape

    data_gpu = cp.asarray(data, dtype=dtype)

    # Replace NaN with 0 for cumsum; track validity separately
    data_filled = cp.where(cp.isnan(data_gpu), dtype(0), data_gpu)
    valid_mask = (~cp.isnan(data_gpu)).astype(cp.int16)

    cumsum_data = cp.cumsum(data_filled, axis=0, dtype=dtype)
    cumsum_valid = cp.cumsum(valid_mask, axis=0, dtype=cp.int16)
    del data_filled, valid_mask, data_gpu

    # Pad with zeros so index [t+1] - [t+1-scale] works for t = scale-1 onward
    zeros_2d = cp.zeros((1, n_lat, n_lon), dtype=dtype)
    zeros_2d_i16 = cp.zeros((1, n_lat, n_lon), dtype=cp.int16)
    padded_data = cp.concatenate([zeros_2d, cumsum_data], axis=0)   # (n_time+1,)
    padded_valid = cp.concatenate([zeros_2d_i16, cumsum_valid], axis=0)
    del cumsum_data, cumsum_valid, zeros_2d, zeros_2d_i16

    # window_sum[t] = padded[t+1] - padded[t+1-scale]  for t in [scale-1, n_time)
    # => indices: padded[scale:] - padded[:n_time-scale+1]
    window_sums = padded_data[scale:] - padded_data[:n_time - scale + 1]
    window_valid = padded_valid[scale:] - padded_valid[:n_time - scale + 1]
    del padded_data, padded_valid

    # Build result: NaN for first (scale-1) steps, rolling sum elsewhere
    nan_prefix = cp.full((scale - 1, n_lat, n_lon), cp.nan, dtype=dtype)
    valid_sums = cp.where(window_valid == scale, window_sums, cp.nan)
    result_gpu = cp.concatenate([nan_prefix, valid_sums], axis=0)
    del nan_prefix, valid_sums, window_sums, window_valid

    result = cp.asnumpy(result_gpu)
    del result_gpu
    return result


# ---------------------------------------------------------------------------
# Gamma parameter fitting  (replaces _compute_gamma_params_vectorized)
# ---------------------------------------------------------------------------

def compute_gamma_params_gpu(
    scaled_data: np.ndarray,
    n_years: int,
    periods_per_year: int,
    n_lat: int,
    n_lon: int,
    cal_start_idx: int,
    cal_end_idx: int,
    dtype,
    min_values_for_fit: int = 10
) -> tuple:
    """
    GPU-accelerated gamma parameter estimation via method of moments.

    Processes all grid cells simultaneously for each calendar period.

    :param scaled_data: 3-D array (time, lat, lon)
    :param n_years: number of years
    :param periods_per_year: 12 for monthly, 366 for daily
    :param n_lat: latitude grid size
    :param n_lon: longitude grid size
    :param cal_start_idx: calibration start index (year offset)
    :param cal_end_idx: calibration end index (exclusive)
    :param dtype: working dtype (np.float32 or np.float64)
    :param min_values_for_fit: minimum non-zero samples required
    :return: (alphas, betas, probs_zero) as CPU numpy arrays,
             each with shape (periods_per_year, n_lat, n_lon)
    """
    cp = _cp

    data_gpu = cp.asarray(scaled_data, dtype=dtype)
    scaled_4d = data_gpu.reshape(n_years, periods_per_year, n_lat, n_lon)

    alphas_gpu = cp.full((periods_per_year, n_lat, n_lon), cp.nan, dtype=dtype)
    betas_gpu = cp.full((periods_per_year, n_lat, n_lon), cp.nan, dtype=dtype)
    probs_zero_gpu = cp.full((periods_per_year, n_lat, n_lon), cp.nan, dtype=dtype)

    for period_idx in range(periods_per_year):
        calib_data = scaled_4d[cal_start_idx:cal_end_idx, period_idx, :, :]

        valid_mask = ~cp.isnan(calib_data)
        n_valid = cp.sum(valid_mask, axis=0)
        n_zeros = cp.sum((calib_data == 0) & valid_mask, axis=0)

        with contextlib.nullcontext():
            probs_zero_gpu[period_idx] = cp.where(
                n_valid > 0, n_zeros / n_valid.astype(dtype), cp.nan
            )

        nonzero_mask = (calib_data > 0) & valid_mask
        n_nonzero = cp.sum(nonzero_mask, axis=0)

        calib_positive = cp.where(nonzero_mask, calib_data, cp.nan)

        with contextlib.nullcontext():
            mean_vals = cp.nanmean(calib_positive, axis=0)
            log_vals = cp.log(calib_positive)
            mean_log = cp.nanmean(log_vals, axis=0)

            # A = ln(mean) - mean(ln(x))  (Wilson-Hilferty approximation)
            a = cp.log(mean_vals) - mean_log

            alpha = cp.where(
                a > 0,
                (1.0 + cp.sqrt(1.0 + 4.0 * a / 3.0)) / (4.0 * a),
                cp.nan
            )
            beta = mean_vals / alpha

            valid_fit = n_nonzero >= min_values_for_fit
            alphas_gpu[period_idx] = cp.where(valid_fit, alpha, cp.nan)
            betas_gpu[period_idx] = cp.where(valid_fit, beta, cp.nan)

        del calib_data, valid_mask, nonzero_mask, calib_positive, log_vals

    del data_gpu

    alphas = cp.asnumpy(alphas_gpu)
    betas = cp.asnumpy(betas_gpu)
    probs_zero = cp.asnumpy(probs_zero_gpu)
    del alphas_gpu, betas_gpu, probs_zero_gpu

    return alphas, betas, probs_zero


# ---------------------------------------------------------------------------
# Normal transformation  (replaces _transform_to_normal_vectorized)
# ---------------------------------------------------------------------------

def transform_to_normal_gpu(
    scaled_data: np.ndarray,
    alphas: np.ndarray,
    betas: np.ndarray,
    probs_zero: np.ndarray,
    n_years: int,
    periods_per_year: int,
    n_lat: int,
    n_lon: int,
    dtype,
    valid_min: float = -3.09,
    valid_max: float = 3.09
) -> np.ndarray:
    """
    GPU-accelerated transformation to standard normal via gamma CDF.

    Uses cupyx.scipy.special.gammainc for the regularized incomplete gamma
    (= gamma CDF with loc=0) and ndtri for the inverse-normal transform.

    Equivalent to scipy.stats.gamma.cdf + scipy.stats.norm.ppf, but runs
    entirely on the GPU for all grid cells simultaneously.

    :param scaled_data: 3-D array (time, lat, lon)
    :param alphas: shape parameters, shape (periods_per_year, lat, lon)
    :param betas: scale parameters, shape (periods_per_year, lat, lon)
    :param probs_zero: zero-probability, shape (periods_per_year, lat, lon)
    :param n_years: number of years
    :param periods_per_year: 12 for monthly, 366 for daily
    :param n_lat: latitude grid size
    :param n_lon: longitude grid size
    :param dtype: working dtype
    :param valid_min: lower clip value for output
    :param valid_max: upper clip value for output
    :return: transformed array (time, lat, lon) as CPU numpy array
    """
    cp = _cp
    cpsp = _cpspecial
    n_time = n_years * periods_per_year

    data_gpu = cp.asarray(scaled_data, dtype=dtype)
    alphas_gpu = cp.asarray(alphas, dtype=dtype)
    betas_gpu = cp.asarray(betas, dtype=dtype)
    probs_zero_gpu = cp.asarray(probs_zero, dtype=dtype)

    result_gpu = cp.full((n_time, n_lat, n_lon), cp.nan, dtype=dtype)

    scaled_4d = data_gpu.reshape(n_years, periods_per_year, n_lat, n_lon)
    result_4d = result_gpu.reshape(n_years, periods_per_year, n_lat, n_lon)

    for period_idx in range(periods_per_year):
        alpha = alphas_gpu[period_idx]        # (lat, lon)
        beta = betas_gpu[period_idx]
        prob_zero = probs_zero_gpu[period_idx]

        period_vals = scaled_4d[:, period_idx, :, :]  # (n_years, lat, lon)

        valid_params = (
            ~cp.isnan(alpha) & ~cp.isnan(beta) & (alpha > 0) & (beta > 0)
        )
        if not cp.any(valid_params):
            continue

        with contextlib.nullcontext():
            # Gamma CDF via regularised lower incomplete gamma:
            #   Gamma.cdf(x; a, scale=b) = gammainc(a, x/b)
            x_scaled = cp.where(period_vals > 0, period_vals / beta[cp.newaxis], 0.0)
            gamma_probs = cpsp.gammainc(alpha[cp.newaxis], x_scaled)

            adjusted = (
                prob_zero[cp.newaxis]
                + (1.0 - prob_zero[cp.newaxis]) * gamma_probs
            )
            cp.clip(adjusted, 1e-10, 1.0 - 1e-10, out=adjusted)

            # Inverse-normal transform
            transformed = cpsp.ndtri(adjusted)

            valid_expanded = cp.broadcast_to(
                valid_params[cp.newaxis], period_vals.shape
            )
            result_4d[:, period_idx, :, :] = cp.where(
                valid_expanded & ~cp.isnan(period_vals),
                transformed,
                cp.nan
            )

        del x_scaled, gamma_probs, adjusted, transformed

    del data_gpu, alphas_gpu, betas_gpu, probs_zero_gpu

    result = cp.asnumpy(result_gpu)
    del result_gpu
    return result
