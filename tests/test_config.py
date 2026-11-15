import keyring

import study_buddy.config as config_module
from study_buddy.config import SettingsManager


def make_settings(tmp_path, monkeypatch):
    store = {}

    def set_password(service, name, value):
        store[name] = value

    def get_password(service, name):
        return store.get(name)

    def delete_password(service, name):
        if name not in store:
            raise keyring.errors.PasswordDeleteError(name)
        del store[name]

    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(keyring, "set_password", set_password)
    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    return SettingsManager(), store


def test_changing_api_host_removes_old_key(tmp_path, monkeypatch):
    settings, store = make_settings(tmp_path, monkeypatch)
    settings.update_api("https://a.example/v1", "model-a", "glm-4v-flash", "secret-a")
    settings.update_api("https://a.example/v1", "model-b", "glm-4v-flash", None)
    assert store["api_key"] == "secret-a"
    settings.update_api("https://b.example/v1", "model-b", "glm-4v-flash", None)
    assert "api_key" not in store
    assert settings.public()["api"]["api_configured"] is False


def test_legacy_workbuddy_keys_are_dropped(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"provider": "workbuddy", "auto_fallback": true, '
        '"workbuddy": {"client_id": "x"}, "whisper_model": "base"}',
        encoding="utf-8",
    )
    store = {}
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(keyring, "get_password", lambda service, name: store.get(name))
    settings = SettingsManager()
    config = settings.public()
    assert "provider" not in config
    assert "workbuddy" not in config
    assert config["whisper_model"] == "base"
    assert config["frames_enabled"] is True


def test_frame_settings_roundtrip(tmp_path, monkeypatch):
    settings, _ = make_settings(tmp_path, monkeypatch)
    settings.update_general({"frames_enabled": False, "frame_interval": 30, "max_frames": 12})
    config = settings.get()
    assert config["frames_enabled"] is False
    assert config["frame_interval"] == 30
    assert config["max_frames"] == 12
    assert config["api"]["vision_model"] == "glm-4v-flash"


def test_quality_settings_default_and_roundtrip(tmp_path, monkeypatch):
    settings, _ = make_settings(tmp_path, monkeypatch)
    config = settings.get()
    assert config["deep_refine"] is True
    assert config["exam_mode"] is True

    settings.update_general({"deep_refine": False, "exam_mode": False})
    config = settings.get()
    assert config["deep_refine"] is False
    assert config["exam_mode"] is False


def test_reasoning_effort_default_and_validation(tmp_path, monkeypatch):
    settings, _ = make_settings(tmp_path, monkeypatch)
    assert settings.get()["api"]["reasoning_effort"] == "low"

    settings.update_api("https://a.example/v1", "glm-5.3-flash", "glm-4v-flash", None, "high")
    assert settings.get()["api"]["reasoning_effort"] == "high"

    settings.update_api("https://a.example/v1", "glm-5.3-flash", "glm-4v-flash", None, "乱填")
    assert settings.get()["api"]["reasoning_effort"] == "low"
