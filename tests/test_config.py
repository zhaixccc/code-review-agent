import pytest

from code_review_agent.config import load_settings


def test_load_settings_rejects_non_chinese_review_language(monkeypatch):
    monkeypatch.setenv("REVIEW_LANGUAGE", "English")

    with pytest.raises(ValueError, match="仅支持 Simplified Chinese"):
        load_settings()


def test_load_settings_accepts_chinese_label(monkeypatch):
    monkeypatch.setenv("REVIEW_LANGUAGE", "简体中文")

    assert load_settings().review_language == "Simplified Chinese"
