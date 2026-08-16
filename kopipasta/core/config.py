"""Backend selection is configuration, not conversation — spec §6.

Which model answers is an operator decision, made once. A caller asking "where
does token expiry live?" has no basis for choosing between Gemini and Opus:
that choice is about cost, context ceiling and which keys you hold, and it does
not change between questions. So it is not an argument.

    --backend gemini:gemini-3.7-flash    escape hatch: debugging, A/B, CI pinning
    KOPIPASTA_BACKEND                    per-shell or per-job override
    ~/.config/kopipasta/config.toml      the normal place
    built-in default                     before anything is configured

Every resolved value carries where it came from. Precedence chains are exactly
the thing that silently does the wrong thing, and "which model actually
answered?" is the first question when an answer looks off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from kopipasta.config import get_global_profile_path
from kopipasta.core.errors import (
    ConfigInvalid,
    MissingApiKey,
    NoBackendConfigured,
    UnknownProvider,
    UsageError,
)

# provider -> the env var holding its key. `exec` and `claude-cli` are absent
# deliberately: they borrow whatever the surrounding CLI is authenticated as,
# which is the whole reason to reach for them.
PROVIDER_KEY_ENV: Dict[str, Optional[str]] = {
    # `none` calls no model at all: it hands the assembled payload back as the
    # answer. It is how the pipeline is exercised without a key, a network or
    # a bill, and it needs no credential for the same reason.
    "none": None,
    "exec": None,
    "claude-cli": None,
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "OPENAI_API_KEY",
    "gemini-compat": "GEMINI_API_KEY",
}

VALID_PROVIDERS = tuple(PROVIDER_KEY_ENV)

DEFAULTS: Dict[str, Any] = {
    "cache_ttl_s": 300,
    "max_tokens": 8192,
    "timeout_s": 900,
}

FALLBACK_SECTION = "ask"

SRC_FLAG = "--backend"
SRC_ENV = "KOPIPASTA_BACKEND"
SRC_DEFAULT = "built-in default"


def config_path() -> Path:
    """Beside prompt_template.j2 and ai_profile.md, via the same XDG logic."""
    return get_global_profile_path().parent / "config.toml"


DEFAULT_CONFIG = """# Which model answers, and how. Edited once, not argued per call.
#
# API keys are NOT here: they stay in the environment (GEMINI_API_KEY,
# ANTHROPIC_API_KEY, OPENAI_API_KEY), out of a file on disk and out of any
# session record. Sections are per verb; [ask] is the fallback for the rest.

[ask]
provider    = "gemini"
model       = "gemini-3.7-flash"
cache_ttl_s = 300          # a provider-side prefix cache is rented until this expires
max_tokens  = 8192         # reasoning tokens spend this budget too
timeout_s   = 900

# [patch]
# provider = "anthropic"
# model    = "claude-opus-5"
"""


def open_config_in_editor() -> None:
    """`kopipasta --edit-config`, with the interaction guard — spec §6/§12.

    Creating the file first means a headless caller that cannot open an editor
    is still left with something it can edit directly, which is why the guard
    is consulted after the write and not before it.
    """
    import os as _os
    import shutil
    import subprocess
    import sys

    from kopipasta.interaction import require_human

    path = config_path()
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(DEFAULT_CONFIG)
            print(f"Created a starter config at: {path}")
        except OSError as exc:
            print(f"Error creating {path}: {exc}")
            return

    require_human(
        f"Opening {path} in an editor",
        "The file exists and can be edited directly.",
    )
    editor = _os.environ.get("EDITOR", "code" if shutil.which("code") else "vim")
    if sys.platform == "win32":
        _os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.call(("open", str(path)))
    else:
        subprocess.call((editor, str(path)))


def _load_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            raise ConfigInvalid(
                str(path),
                "Reading TOML needs Python 3.11+ or the 'tomli' package on 3.10.",
            ) from None
    try:
        with open(path, "rb") as fh:
            return dict(tomllib.load(fh))
    except OSError as exc:
        raise ConfigInvalid(str(path), f"{exc}") from exc
    except Exception as exc:  # tomllib.TOMLDecodeError, named loosely for the 3.10 path
        raise ConfigInvalid(str(path), f"Not valid TOML: {exc}") from exc


@dataclass
class BackendConfig:
    provider: str
    model: str
    cache_ttl_s: int
    max_tokens: int
    timeout_s: int
    #: field name -> human-readable provenance, for `config --show`
    sources: Dict[str, str] = field(default_factory=dict)

    @property
    def spec(self) -> str:
        """The `kind:model` string the backend factory takes."""
        return f"{self.provider}:{self.model}" if self.model else self.provider

    @property
    def api_key_env(self) -> Optional[str]:
        return PROVIDER_KEY_ENV.get(self.provider)

    def require_api_key(self, env: Optional[Dict[str, str]] = None) -> None:
        """Raise MissingApiKey naming the provider's provenance, not just the var."""
        env = os.environ if env is None else env
        var = self.api_key_env
        if var is None:
            return
        if not (env.get(var) or "").strip():
            raise MissingApiKey(
                self.provider, var, self.sources.get("provider", "unknown")
            )


def _split_spec(spec: str) -> Tuple[str, str]:
    provider, _, model = spec.partition(":")
    return provider.strip(), model.strip()


def resolve_backend(
    verb: str = FALLBACK_SECTION,
    *,
    flag: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    path: Optional[Path] = None,
) -> BackendConfig:
    """Resolve the backend for `verb`, recording where each value came from.

    Sections are per verb, not per call: triage over a 400k frontload wants a
    large cheap context window while a coordinated patch wants the strongest
    model, and that is still an operator decision. `[ask]` is the fallback for
    any verb without its own section.
    """
    env = os.environ if env is None else env
    path = config_path() if path is None else path

    data = _load_toml(path)

    # Provenance is tracked per key, not per section. [patch] inheriting
    # max_tokens from [ask] must still say [ask] -- attributing an inherited
    # value to the overriding section is exactly the mis-report that
    # `config --show` exists to prevent.
    section: Dict[str, Any] = {}
    key_source: Dict[str, str] = {}
    for name in (
        (FALLBACK_SECTION, verb) if verb != FALLBACK_SECTION else (FALLBACK_SECTION,)
    ):
        block = data.get(name)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            section[key] = value
            key_source[key] = f"{path} [{name}]"

    provider = model = None
    source = None

    if flag is not None and not flag.strip():
        # An explicitly empty --backend is a mistake, not a request to fall
        # back: `--backend "$MODEL"` with MODEL unset would otherwise silently
        # use the config file's model and report it as configured. An empty
        # KOPIPASTA_BACKEND is treated as unset below, because for an
        # environment variable that idiom is genuinely common.
        raise UsageError(
            "--backend was given an empty value.",
            detail="It takes provider:model, e.g. gemini:gemini-3.7-flash.",
            hint="Drop the flag entirely to use the configured backend:\n"
            "  kopipasta config --show",
        )
    if flag:
        provider, model = _split_spec(flag)
        source = SRC_FLAG
    elif (env.get(SRC_ENV) or "").strip():
        provider, model = _split_spec(env[SRC_ENV].strip())
        source = SRC_ENV
    elif section.get("provider"):
        provider = str(section["provider"]).strip()
        model = str(section.get("model") or "").strip()
        source = key_source["provider"]

    if not provider:
        raise NoBackendConfigured(str(path))
    if provider not in PROVIDER_KEY_ENV:
        raise UnknownProvider(provider, VALID_PROVIDERS, source or SRC_DEFAULT)

    sources = {
        "provider": source,
        "model": key_source.get("model", source)
        if source not in (SRC_FLAG, SRC_ENV)
        else source,
    }

    def scalar(name: str) -> Any:
        if name in section:
            sources[name] = key_source[name]
            return section[name]
        sources[name] = SRC_DEFAULT
        return DEFAULTS[name]

    return BackendConfig(
        provider=provider,
        model=model,
        cache_ttl_s=int(scalar("cache_ttl_s")),
        max_tokens=int(scalar("max_tokens")),
        timeout_s=int(scalar("timeout_s")),
        sources=sources,
    )


def render_show(cfg: BackendConfig, env: Optional[Dict[str, str]] = None) -> str:
    """`kopipasta config --show` — resolved values and where each came from.

    Never prints the key. Presence and length are enough to tell "unset" from
    "empty" from "the wrong one", and printing it invites pasting a secret into
    a bug report.
    """
    env = os.environ if env is None else env
    rows = [
        ("provider", cfg.provider, cfg.sources.get("provider", "")),
        ("model", cfg.model or "(provider default)", cfg.sources.get("model", "")),
    ]

    var = cfg.api_key_env
    if var is None:
        rows.append(
            ("api key", "not needed", f"{cfg.provider} borrows its host CLI's auth")
        )
    else:
        raw = env.get(var)
        if raw is None:
            state = "UNSET"
        elif not raw.strip():
            state = "set but empty"
        else:
            state = f"set ({len(raw)} chars)"
        rows.append(("api key", var, state))

    for name in ("cache_ttl_s", "max_tokens", "timeout_s"):
        rows.append((name, str(getattr(cfg, name)), cfg.sources.get(name, "")))

    w1 = max(len(r[0]) for r in rows)
    w2 = max(len(str(r[1])) for r in rows)
    return "\n".join(f"{n:<{w1}}  {v:<{w2}}  {s}" for n, v, s in rows)
