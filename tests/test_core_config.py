"""Backend resolution and its provenance — spec §6."""

import pytest

from kopipasta.core import config as cfgmod
from kopipasta.core.config import BackendConfig, render_show, resolve_backend
from kopipasta.core.errors import (
    ConfigInvalid,
    MissingApiKey,
    NoBackendConfigured,
    UnknownProvider,
)

TWO_SECTIONS = """
[ask]
provider = "gemini"
model = "gemini-3.7-flash"
max_tokens = 4096

[patch]
provider = "anthropic"
model = "claude-opus-5"
"""


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_config_file_is_read(tmp_path):
    cfg = resolve_backend("ask", env={}, path=write(tmp_path, TWO_SECTIONS))
    assert cfg.spec == "gemini:gemini-3.7-flash"
    assert cfg.max_tokens == 4096


def test_env_beats_config(tmp_path):
    cfg = resolve_backend(
        "ask",
        env={"KOPIPASTA_BACKEND": "openai:gpt-5"},
        path=write(tmp_path, TWO_SECTIONS),
    )
    assert cfg.spec == "openai:gpt-5"
    assert cfg.sources["provider"] == "KOPIPASTA_BACKEND"


def test_flag_beats_env(tmp_path):
    cfg = resolve_backend(
        "ask",
        flag="anthropic:claude-opus-5",
        env={"KOPIPASTA_BACKEND": "openai:gpt-5"},
        path=write(tmp_path, TWO_SECTIONS),
    )
    assert cfg.spec == "anthropic:claude-opus-5"
    assert cfg.sources["provider"] == "--backend"


def test_verb_section_overrides_ask(tmp_path):
    """Per verb, not per call: triage and patch want different models."""
    cfg = resolve_backend("patch", env={}, path=write(tmp_path, TWO_SECTIONS))
    assert cfg.spec == "anthropic:claude-opus-5"


def test_unknown_verb_falls_back_to_ask(tmp_path):
    cfg = resolve_backend("map", env={}, path=write(tmp_path, TWO_SECTIONS))
    assert cfg.spec == "gemini:gemini-3.7-flash"


def test_inherited_value_is_attributed_to_the_section_it_came_from(tmp_path):
    """[patch] inherits max_tokens from [ask] and must say so.

    Reporting it as [patch] would send someone editing the wrong section --
    the exact mis-attribution `config --show` exists to prevent.
    """
    cfg = resolve_backend("patch", env={}, path=write(tmp_path, TWO_SECTIONS))
    assert cfg.max_tokens == 4096
    assert cfg.sources["max_tokens"].endswith("[ask]")
    assert cfg.sources["provider"].endswith("[patch]")


def test_defaults_are_labelled_as_defaults(tmp_path):
    cfg = resolve_backend("ask", env={}, path=write(tmp_path, TWO_SECTIONS))
    assert cfg.timeout_s == 900
    assert cfg.sources["timeout_s"] == "built-in default"


def test_no_backend_anywhere_is_an_onboarding_error(tmp_path):
    with pytest.raises(NoBackendConfigured) as exc:
        resolve_backend("ask", env={}, path=tmp_path / "absent.toml")
    rendered = exc.value.render()
    assert "--backend" in rendered and "KOPIPASTA_BACKEND" in rendered
    assert "--edit-config" in rendered
    assert exc.value.exit_code == 2
    assert exc.value.retryable is False


def test_unknown_provider_lists_valid_ones_and_suggests(tmp_path):
    with pytest.raises(UnknownProvider) as exc:
        resolve_backend(
            "ask", env={}, path=write(tmp_path, '[ask]\nprovider = "gemni"\n')
        )
    rendered = exc.value.render()
    assert "gemini" in rendered
    assert exc.value.exit_code == 1


def test_malformed_toml_names_the_file(tmp_path):
    with pytest.raises(ConfigInvalid) as exc:
        resolve_backend("ask", env={}, path=write(tmp_path, "[ask\nprovider = "))
    assert "config.toml" in exc.value.render()


def test_missing_key_reports_where_the_provider_resolved_from(tmp_path):
    """Naming only the env var sends you to fix the key when the real bug is
    that you did not expect to be talking to this provider at all."""
    cfg = resolve_backend("ask", env={}, path=write(tmp_path, TWO_SECTIONS))
    with pytest.raises(MissingApiKey) as exc:
        cfg.require_api_key(env={})
    rendered = exc.value.render()
    assert "GEMINI_API_KEY" in rendered
    assert "[ask]" in rendered
    assert exc.value.to_json()["resolved_from"].endswith("[ask]")


def test_blank_key_counts_as_missing(tmp_path):
    cfg = resolve_backend("ask", env={}, path=write(tmp_path, TWO_SECTIONS))
    with pytest.raises(MissingApiKey):
        cfg.require_api_key(env={"GEMINI_API_KEY": "   "})


@pytest.mark.parametrize("provider", ["exec", "claude-cli"])
def test_host_authenticated_backends_need_no_key(provider, tmp_path):
    """exec: and claude-cli: borrow the surrounding CLI's auth. Demanding a
    key would defeat the reason to reach for them."""
    cfg = resolve_backend(
        "ask", flag=f"{provider}:claude -p", env={}, path=tmp_path / "absent.toml"
    )
    cfg.require_api_key(env={})  # must not raise
    assert cfg.api_key_env is None


def test_show_never_prints_the_key(tmp_path):
    secret = "sk-ant-SUPERSECRET-abcdefghijklmnop"
    cfg = resolve_backend("patch", env={}, path=write(tmp_path, TWO_SECTIONS))
    out = render_show(cfg, env={"ANTHROPIC_API_KEY": secret})
    assert secret not in out
    assert "ANTHROPIC_API_KEY" in out
    assert f"set ({len(secret)} chars)" in out


def test_show_distinguishes_unset_from_empty(tmp_path):
    cfg = resolve_backend("ask", env={}, path=write(tmp_path, TWO_SECTIONS))
    assert "UNSET" in render_show(cfg, env={})
    assert "set but empty" in render_show(cfg, env={"GEMINI_API_KEY": "  "})


def test_show_lists_every_value_with_a_source(tmp_path):
    cfg = resolve_backend("ask", env={}, path=write(tmp_path, TWO_SECTIONS))
    out = render_show(cfg, env={"GEMINI_API_KEY": "k"})
    for field in ("provider", "model", "cache_ttl_s", "max_tokens", "timeout_s"):
        assert field in out
    assert "built-in default" in out


def test_config_path_sits_beside_the_other_config_files(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cfgmod.config_path().name == "config.toml"
    assert cfgmod.config_path().parent.name == "kopipasta"


def test_spec_omits_the_colon_when_no_model_is_named():
    cfg = BackendConfig("gemini", "", 300, 8192, 900)
    assert cfg.spec == "gemini"
