"""
Unit tests for configuration persistence, user profiles, and factory presets.
"""

from vhs_lib.config import (
    _FACTORY_PRESETS,
    delete_profile,
    list_profiles,
    load_profile,
    load_settings,
    save_profile,
    save_settings,
)


def test_factory_presets_integrity():
    assert len(_FACTORY_PRESETS) >= 4
    assert "PAL — Hauppauge USB-Live2 (Optimal)" in _FACTORY_PRESETS
    assert "Archival Master (Lossless FFV1 + PCM)" in _FACTORY_PRESETS

    pal = _FACTORY_PRESETS["PAL — Hauppauge USB-Live2 (Optimal)"]
    assert pal["resolution"] == "PAL (720x576)"
    assert pal["fps"] == 25.0
    assert pal["aspect_ratio"] == "4:3"
    assert pal["deinterlace"] == "bwdif"

    archival = _FACTORY_PRESETS["Archival Master (Lossless FFV1 + PCM)"]
    assert archival["encoder"] == "ffv1"
    assert archival["mask_head_switching"] is False


def test_profile_crud_cycle(tmp_path):
    profiles_dir = tmp_path / "profiles"

    # 1. List factory presets
    all_profiles = list_profiles(profiles_dir=profiles_dir)
    assert "PAL — Hauppauge USB-Live2 (Optimal)" in all_profiles

    # 2. Save custom profile
    custom_name = "My Custom JVC VCR"
    custom_data = {
        "resolution": "PAL (720x576)",
        "fps": 25.0,
        "crf": 21,
        "custom_tag": "test_value",
    }
    save_profile(custom_name, custom_data, profiles_dir=profiles_dir)

    # 3. List should include new profile
    updated = list_profiles(profiles_dir=profiles_dir)
    assert custom_name in updated

    # 4. Load custom profile
    loaded = load_profile(custom_name, profiles_dir=profiles_dir)
    assert loaded["custom_tag"] == "test_value"
    assert loaded["crf"] == 21

    # 5. Delete profile
    deleted = delete_profile(custom_name, profiles_dir=profiles_dir)
    assert deleted is True
    assert custom_name not in list_profiles(profiles_dir=profiles_dir)


def test_settings_save_and_load(tmp_path):
    settings_file = tmp_path / "settings.json"

    # Initial load returns empty dict
    assert load_settings(settings_file) == {}

    # Save and reload
    data = {"output_dir": str(tmp_path), "fps": 25.0, "encoder": "libsvtav1"}
    save_settings(data, settings_file)

    loaded = load_settings(settings_file)
    assert loaded["encoder"] == "libsvtav1"
    assert loaded["fps"] == 25.0

    # Corrupt file fallback
    settings_file.write_text("{corrupt json", encoding="utf-8")
    assert load_settings(settings_file) == {}

