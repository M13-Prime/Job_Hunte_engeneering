"""ATS backend adapters. Each implements ``JobsBackend.list_jobs``."""

from signal_tracker.jobs.backends.ashby import AshbyBackend
from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.backends.greenhouse import GreenhouseBackend
from signal_tracker.jobs.backends.lever import LeverBackend
from signal_tracker.jobs.backends.workable import WorkableBackend

DEFAULT_BACKENDS: list[JobsBackend] = [
    GreenhouseBackend(),
    LeverBackend(),
    WorkableBackend(),
    AshbyBackend(),
]

__all__ = [
    "DEFAULT_BACKENDS",
    "AshbyBackend",
    "GreenhouseBackend",
    "JobsBackend",
    "LeverBackend",
    "WorkableBackend",
]
