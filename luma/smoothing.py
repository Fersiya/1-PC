"""Сглаживание цвета: время подхода не зависит от FPS, резкая смена сцены догоняется быстрее."""
import math


def smooth_alpha(smoothing: float, dt: float, diff: float) -> float:
    """Доля старого значения (0..1) для одного шага.

    smoothing — ползунок 0..0.9: постоянная времени ~ 0.05 + 1.2*smoothing секунд
    (45% ≈ 0.6 с, 85% ≈ 1.1 с). Для больших скачков (diff — сумма |ΔRGB|, 0..765)
    время сокращается максимум вдвое, чтобы сцена не «тянулась» слишком долго.
    """
    if smoothing <= 0.0:
        return 0.0
    dt = max(0.005, min(0.5, dt))
    tau = 0.05 + 1.2 * smoothing
    boost = min(1.0, max(0.0, (diff - 150.0) / 400.0))
    tau *= 1.0 - 0.5 * boost
    return math.exp(-dt / tau)
