"""Explicit ambient environment boundary for the Codex executor."""
from pathlib import Path
import os
import tomllib

from agent.secret_scope import get_secret
from tools.environments.local import hermes_subprocess_env


# No prefix grants: unknown provider, gateway, and infrastructure variables stay
# in Hermes. HOME/CODEX_HOME preserve Codex's own subscription authentication.
_RUNTIME_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "TMP", "TEMP", "TZ", "PYTHONUTF8", "RUST_LOG",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "MSYSTEM", "MSYS", "CHERE_INVOKING",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "CODEX_HOME", "HERMES_HOME", "HERMES_PROFILE", "HERMES_SESSION_ID",
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_ROOT",
    "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_BRANCH", "HERMES_DELEGATED_CHILD_CONTEXT",
})


def codex_subprocess_env(codex_home=None, model_provider=None, overrides=None):
    """Forward runtime paths and only the selected provider's credential.

    A named provider uses Codex's declared env_key, not every provider secret in
    Hermes. Explicit client overrides cannot bypass the same allowlist.
    """
    env = {key: value for key, value in hermes_subprocess_env().items()
           if key.upper() in _RUNTIME_ENV_KEYS}
    home = Path(codex_home or (overrides or {}).get("CODEX_HOME")
                or os.getenv("CODEX_HOME") or Path.home() / ".codex")
    try:
        with (home / "config.toml").open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        config = {}
    provider = model_provider or config.get("model_provider", "openai")
    provider_config = config.get("model_providers", {}).get(provider, {})
    key = provider_config.get("env_key") or ("OPENAI_API_KEY" if provider == "openai" else None)
    if key:
        value = get_secret(key)
        if value is not None:
            env[key] = value
    if overrides:
        allowed = _RUNTIME_ENV_KEYS | ({key.upper()} if key else set())
        env.update({name: value for name, value in overrides.items()
                    if name.upper() in allowed})
    if codex_home:
        env["CODEX_HOME"] = str(codex_home)
    return env
