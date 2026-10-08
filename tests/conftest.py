import pytest


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Settings are cached; reset around every test so env changes in one test can't leak into another."""
    from emaild import config
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_tracker_schema_probe():
    """trackers caches whether migration 016's closed_reason column exists; each test probes its own fake DB."""
    from emaild import trackers
    trackers._REASON.clear()
    yield
    trackers._REASON.clear()
