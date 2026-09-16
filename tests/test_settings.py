

def test_settings_of_the_previous_name_are_taken_over(tmp_path, monkeypatch):
    from pipilogicanalyzer.core import settings as settings_module

    monkeypatch.delenv("PIPILOGICANALYZER_SETTINGS_DIR", raising=False)
    monkeypatch.delenv("LOGICANALYZER_SETTINGS_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(settings_module.sys, "platform", "linux")

    previous = tmp_path / settings_module.PREVIOUS_APP_NAME
    previous.mkdir()
    (previous / "profiles.json").write_text('{"Profiles": []}')

    directory = settings_module.settings_directory()

    assert directory == str(tmp_path / settings_module.APP_NAME)
    assert (tmp_path / settings_module.APP_NAME / "profiles.json").read_text() == '{"Profiles": []}'
