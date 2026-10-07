import pytest


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Settings are cached; reset around every test so env changes in one test can't leak into another."""
    from emaild import config
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()
