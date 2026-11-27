from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import keyring

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STUDY_BUDDY_DATA", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
KEYRING_SERVICE = "StudyVideoSummary"

DEFAULT_CONFIG: dict[str, Any] = {
    "whisper_model": "small",
    "whisper_device": "cpu",
    "whisper_compute_type": "int8",
    "chunk_chars": 14000,
    "frames_enabled": True,
    "frame_interval": 60,
    "max_frames": 24,
    "deep_refine": True,
    "exam_mode": True,
    "api": {"base_url": "", "model": "", "vision_model": "glm-4v-flash", "reasoning_effort": "low"},
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class SettingsManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config = self._load()

    def _load(self) -> dict[str, Any]:
        if not CONFIG_PATH.exists():
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = _merge(DEFAULT_CONFIG, saved)
            # 丢弃历史版本遗留的已移除配置项（如 provider、workbuddy）。
            cleaned = {
                key: value
                for key, value in merged.items()
                if key in DEFAULT_CONFIG
            }
            cleaned["api"] = {
                key: value
                for key, value in merged.get("api", {}).items()
                if key in DEFAULT_CONFIG["api"]
            }
            return cleaned
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(DEFAULT_CONFIG))

    def _save(self) -> None:
        temporary = CONFIG_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(CONFIG_PATH)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._config))

    def update_general(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "whisper_model",
            "whisper_device",
            "whisper_compute_type",
            "chunk_chars",
            "frames_enabled",
            "frame_interval",
            "max_frames",
            "deep_refine",
            "exam_mode",
        }
        with self._lock:
            for key, value in values.items():
                if key in allowed:
                    self._config[key] = value
            self._save()
            return self.get()

    def update_api(
        self,
        base_url: str,
        model: str,
        vision_model: str,
        api_key: str | None,
        reasoning_effort: str = "low",
    ) -> None:
        base_url = base_url.strip()
        effort = (reasoning_effort or "").strip()
        if effort not in {"", "low", "high", "max"}:
            effort = "low"
        new_key = (api_key or "").strip() or None
        with self._lock:
            old_api = dict(self._config["api"])
            old_key = self.get_secret("api_key")
            base_changed = old_api.get("base_url", "") != base_url
            try:
                if new_key:
                    self.set_secret("api_key", new_key)
                elif base_changed:
                    # 换了服务商，旧密钥对新 base_url 没有意义；
                    # 但先留着 old_key 的原文，写配置失败时还能退回去。
                    if not self.delete_secret("api_key"):
                        raise RuntimeError("无法安全移除旧 API 密钥，配置未修改。")
                self._config["api"] = {
                    "base_url": base_url,
                    "model": model.strip(),
                    "vision_model": vision_model.strip(),
                    "reasoning_effort": effort,
                }
                self._save()
            except Exception:
                # 任一步失败就整体回滚：内存态、磁盘配置、密钥都退回改之前，
                # 不留下「界面说保存失败，但配置已经变了」这种半截状态。
                self._config["api"] = old_api
                self._restore_secret("api_key", old_key)
                try:
                    self._save()
                except OSError:
                    pass
                raise

    def _restore_secret(self, name: str, value: str | None) -> None:
        try:
            if value:
                keyring.set_password(KEYRING_SERVICE, name, value)
            else:
                keyring.delete_password(KEYRING_SERVICE, name)
        except keyring.errors.KeyringError:
            pass

    def get_secret(self, name: str) -> str | None:
        try:
            return keyring.get_password(KEYRING_SERVICE, name)
        except keyring.errors.KeyringError:
            return None

    def set_secret(self, name: str, value: str) -> None:
        keyring.set_password(KEYRING_SERVICE, name, value)

    def delete_secret(self, name: str) -> bool:
        try:
            keyring.delete_password(KEYRING_SERVICE, name)
            return True
        except keyring.errors.PasswordDeleteError:
            return True
        except keyring.errors.KeyringError:
            return False

    def public(self) -> dict[str, Any]:
        config = self.get()
        config["api"]["api_configured"] = bool(
            config["api"]["base_url"] and config["api"]["model"] and self.get_secret("api_key")
        )
        return config
