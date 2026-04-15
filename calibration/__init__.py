"""Calibration Layer — EV-based scoring for reward farming.

Produces reliable estimates for:
  P_fill            — probability of getting filled
  E_loss_given_fill — expected dollar loss per fill
  E_time_on_book    — expected time on book (survival)
  reward_rate       — per-market reward attribution

These feed into: EV = (reward × time) - (P_fill × loss)
"""

from .manager import CalibrationManager, CalibrationPredictions
from .attribution import compute_attribution, get_attribution_error

__all__ = [
    "CalibrationManager", "CalibrationPredictions",
    "compute_attribution", "get_attribution_error",
]
