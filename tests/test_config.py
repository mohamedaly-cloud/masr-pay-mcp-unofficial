import pytest

from fawry_mcp.config import BASE_URLS, ConfigError, load_config

CREDS = {"FAWRY_MERCHANT_CODE": "M1", "FAWRY_SECURE_KEY": "secret-key-value"}


def test_defaults_to_the_mock_so_misconfiguration_never_touches_real_money():
    config = load_config(CREDS)
    assert config.environment == "mock"
    assert config.base_url == BASE_URLS["mock"]


@pytest.mark.parametrize("env", ["staging", "production", "PRODUCTION "])
def test_environment_selects_base_url(env):
    config = load_config({**CREDS, "FAWRY_ENV": env})
    assert config.base_url == BASE_URLS[env.strip().lower()]


def test_base_url_override_wins():
    config = load_config({**CREDS, "FAWRY_ENV": "mock", "FAWRY_BASE_URL": "http://localhost:9999/"})
    assert config.base_url == "http://localhost:9999"


def test_unknown_environment_is_rejected():
    with pytest.raises(ConfigError, match="FAWRY_ENV"):
        load_config({**CREDS, "FAWRY_ENV": "prod"})


@pytest.mark.parametrize("missing", ["FAWRY_MERCHANT_CODE", "FAWRY_SECURE_KEY"])
def test_missing_credentials_are_named(missing):
    env = {k: v for k, v in CREDS.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_secure_key_is_not_in_repr():
    config = load_config(CREDS)
    assert "secret-key-value" not in repr(config)
    assert "secret-key-value" not in str(config.credentials)
