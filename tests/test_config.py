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
