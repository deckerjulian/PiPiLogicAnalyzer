def test_settings_live_in_the_openscilab_folder(tmp_path, monkeypatch):
    from openscilab.core import settings as settings_module

    monkeypatch.delenv("OPENSCILAB_SETTINGS_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(settings_module.sys, "platform", "linux")

    assert settings_module.settings_directory() == str(tmp_path / "openSciLab")
    assert (tmp_path / "openSciLab").is_dir()
