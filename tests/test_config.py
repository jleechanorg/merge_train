import json
import pytest
from pathlib import Path
from merge_train.config import (
    config_path,
    default_config,
    load_config,
    save_config,
    add_repo,
    remove_repo,
    lookup_enforcement,
    get_repo_alias,
)

def test_default_config():
    cfg = default_config()
    assert cfg["default_enforcement"] == "warn"
    assert "/Users/jleechan/projects/merge_train" in cfg["repos"]
    # Honest default: merge_train itself is warn, not block.
    # Claude Code's PreToolUse protocol does not actually prevent the
    # Edit tool from running when the hook returns ``decision: block``
    # — the tool still applies the change. The honest default is
    # warn-only for every repo, so the TUI banner is the visible signal
    # rather than a false promise of enforcement.
    assert cfg["repos"]["/Users/jleechan/projects/merge_train"]["enforcement"] == "warn"


def test_legacy_enforcement_falls_back_to_warn():
    """Every repo (including merge_train) must default to warn.

    See ``test_default_config`` for the rationale — the legacy fallback
    in ``conflict_check_helper._legacy_enforcement`` must also return
    ``warn`` for every repo so a missing or unloadable config file
    does not silently regress to the misleading ``block`` behavior.
    """
    from merge_train.hooks import conflict_check_helper as helper_mod
    # Stub out the package-level config loader so the legacy fallback is
    # exercised — we want to test the hardcoded fallback, not whatever
    # the user's ~/merge_train/config.json happens to say.
    cfg_module_saved = helper_mod._load_config
    lookup_module_saved = helper_mod._lookup_enforcement
    helper_mod._load_config = None
    helper_mod._lookup_enforcement = None
    try:
        assert helper_mod._legacy_enforcement("merge_train") == "warn"
        assert helper_mod._legacy_enforcement("anything_else") == "warn"
    finally:
        helper_mod._load_config = cfg_module_saved
        helper_mod._lookup_enforcement = lookup_module_saved


def test_load_save_config(tmp_path):
    cfg_file = tmp_path / "config.json"
    
    # Missing file returns default_config
    cfg = load_config(path=cfg_file)
    assert cfg == default_config()
    
    # Save config
    custom_cfg = {
        "default_enforcement": "block",
        "repos": {
            "/path/to/repo": {
                "alias": "my_repo",
                "enforcement": "allow"
            }
        }
    }
    save_config(custom_cfg, path=cfg_file)
    
    # Load back
    loaded = load_config(path=cfg_file)
    assert loaded == custom_cfg

def test_add_remove_repo(tmp_path):
    cfg_file = tmp_path / "config.json"
    
    # Add repo
    cfg = add_repo("/path/to/new_repo", enforcement="block", alias="new_alias", path=cfg_file)
    assert "/path/to/new_repo" in cfg["repos"]
    assert cfg["repos"]["/path/to/new_repo"]["enforcement"] == "block"
    assert cfg["repos"]["/path/to/new_repo"]["alias"] == "new_alias"
    
    # Verify file saved
    loaded = load_config(path=cfg_file)
    assert "/path/to/new_repo" in loaded["repos"]
    
    # Add with invalid enforcement raises ValueError
    with pytest.raises(ValueError):
        add_repo("/path/to/new_repo", enforcement="invalid", path=cfg_file)
        
    # Remove repo
    cfg = remove_repo("/path/to/new_repo", path=cfg_file)
    assert "/path/to/new_repo" not in cfg["repos"]
    
    # Verify file saved after removal
    loaded2 = load_config(path=cfg_file)
    assert "/path/to/new_repo" not in loaded2["repos"]

def test_lookup_enforcement():
    cfg = {
        "default_enforcement": "warn",
        "repos": {
            "/path/to/block_repo": {
                "enforcement": "block"
            },
            "/path/to/allow_repo": {
                "enforcement": "allow"
            }
        }
    }
    
    assert lookup_enforcement(cfg, "/path/to/block_repo") == "block"
    assert lookup_enforcement(cfg, "/path/to/allow_repo") == "allow"
    assert lookup_enforcement(cfg, "/path/to/warn_repo") == "warn"
    assert lookup_enforcement(None, "/path/to/any") == "warn"

def test_get_repo_alias():
    cfg = {
        "repos": {
            "/path/to/repo_a": {
                "alias": "alias_a"
            }
        }
    }
    assert get_repo_alias(cfg, "/path/to/repo_a") == "alias_a"
    assert get_repo_alias(cfg, "/path/to/repo_b") == "repo_b"


# --------------------------------------------------------------------------- #
# Edge cases — fail-safe load/save/normalize behaviour
# --------------------------------------------------------------------------- #


def test_malformed_json_returns_default(tmp_path):
    """A corrupt config file must not crash the hook — fall back to defaults."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{not valid json at all")
    cfg = load_config(path=cfg_file)
    assert cfg == default_config()


def test_wrong_shape_normalized(tmp_path):
    """Non-dict repos / unknown default enforcement are normalized, not raised."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "default_enforcement": "NOT_A_MODE",
        "repos": "should-be-a-dict",
    }))
    cfg = load_config(path=cfg_file)
    assert cfg["repos"] == {}  # coerced from string
    assert cfg["default_enforcement"] == "warn"  # fell through to valid mode


def test_tilde_path_resolution(tmp_path):
    """`~/foo` in repo_path is expanded to an absolute path."""
    cfg_file = tmp_path / "config.json"
    cfg = add_repo("~/synthetic_tilde_repo", enforcement="warn", path=cfg_file)
    # Key must be absolute — `~` resolved by Path.expanduser().resolve()
    abs_keys = [k for k in cfg["repos"] if "synthetic_tilde_repo" in k]
    assert len(abs_keys) == 1
    assert "~" not in abs_keys[0]
    assert abs_keys[0].startswith("/")


def test_add_repo_idempotent_update(tmp_path):
    """Re-adding the same path updates values; no duplicate key is created."""
    cfg_file = tmp_path / "config.json"
    add_repo("/path/to/x", enforcement="warn", alias="x_first", path=cfg_file)
    add_repo("/path/to/x", enforcement="block", alias="x_second", path=cfg_file)
    loaded = load_config(path=cfg_file)
    x_entries = [v for k, v in loaded["repos"].items() if k == "/path/to/x"]
    assert len(x_entries) == 1
    assert x_entries[0]["enforcement"] == "block"
    assert x_entries[0]["alias"] == "x_second"


def test_remove_repo_is_noop_when_absent(tmp_path):
    """Removing a path that isn't configured must not create the config file."""
    cfg_file = tmp_path / "config.json"
    assert not cfg_file.exists()
    result = remove_repo("/never/added", path=cfg_file)
    # No-op: file should NOT be created just to record the absent removal.
    assert not cfg_file.exists()
    assert result == default_config()


def test_invalid_default_falls_through(tmp_path):
    """An unknown default_enforcement value is silently coerced to 'warn'."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"default_enforcement": "BOGUS_MODE"}))
    cfg = load_config(path=cfg_file)
    assert cfg["default_enforcement"] == "warn"
    # And lookup uses the normalized default for any unknown repo.
    assert lookup_enforcement(cfg, "/any/repo") == "warn"


def test_invalid_enforcement_raises(tmp_path):
    """add_repo with an unknown enforcement mode raises ValueError, not silently coerce."""
    cfg_file = tmp_path / "config.json"
    with pytest.raises(ValueError):
        add_repo("/path/to/y", enforcement="BOGUS_MODE", path=cfg_file)
    # And the file must remain absent / unchanged.
    assert not cfg_file.exists() or "/path/to/y" not in load_config(path=cfg_file)["repos"]


def test_top_level_reexports():
    """merge_train package must re-export every config helper via __getattr__."""
    import merge_train

    for name in (
        "CONFIG_FILENAME",
        "VALID_ENFORCEMENT",
        "config_path",
        "default_config",
        "load_config",
        "save_config",
        "add_repo",
        "remove_repo",
        "lookup_enforcement",
        "get_repo_alias",
    ):
        assert hasattr(merge_train, name), f"merge_train.{name} not re-exported"


def test_lookup_enforcement_handles_non_dict_cfg():
    """A non-dict cfg is tolerated; returns the default 'warn' fallback."""
    assert lookup_enforcement(None, "/any/repo") == "warn"
    assert lookup_enforcement("not a dict", "/any/repo") == "warn"


def test_get_repo_alias_falls_back_to_basename():
    """If no alias is set on a repo entry, the repo basename is returned."""
    cfg = {"repos": {"/path/to/no_alias_repo": {"enforcement": "warn"}}}
    assert get_repo_alias(cfg, "/path/to/no_alias_repo") == "no_alias_repo"
