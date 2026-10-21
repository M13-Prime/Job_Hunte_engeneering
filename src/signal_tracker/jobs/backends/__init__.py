"""ATS backend adapters. Each implements ``JobsBackend.list_jobs``."""

from signal_tracker.jobs.backends.ashby import AshbyBackend
from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.backends.greenhouse import GreenhouseBackend
from signal_tracker.jobs.backends.lever import LeverBackend
from signal_tracker.jobs.backends.personio import PersonioBackend
from signal_tracker.jobs.backends.recruitee import RecruiteeBackend
from signal_tracker.jobs.backends.smartrecruiters import SmartRecruitersBackend
from signal_tracker.jobs.backends.teamtailor import TeamtailorBackend
from signal_tracker.jobs.backends.workable import WorkableBackend
from signal_tracker.jobs.backends.workday import WorkdayBackend

# Order = order tried when guessing a company's ATS from its name.
DEFAULT_BACKENDS: list[JobsBackend] = [
    GreenhouseBackend(),
    LeverBackend(),
    WorkableBackend(),
    AshbyBackend(),
    SmartRecruitersBackend(),
    RecruiteeBackend(),
    TeamtailorBackend(),
    PersonioBackend(),
    WorkdayBackend(),  # overrides only (guessable = False)
]

__all__ = [
    "DEFAULT_BACKENDS",
    "AshbyBackend",
    "GreenhouseBackend",
    "JobsBackend",
    "LeverBackend",
    "PersonioBackend",
    "RecruiteeBackend",
    "SmartRecruitersBackend",
    "TeamtailorBackend",
    "WorkableBackend",
    "WorkdayBackend",
]
