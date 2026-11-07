from app.services.browser import constants


def test_every_pinned_hash_has_a_known_platform_tag():
    known_tags = set(constants.CFT_PLATFORMS.values())

    assert set(constants.CFT_SHA256) <= known_tags


def test_pinned_hashes_are_sha256_hex_digests():
    for tag, digest in constants.CFT_SHA256.items():
        assert len(digest) == 64, tag
        int(digest, 16)


def test_candidates_cover_each_supported_os():
    assert {"Darwin", "Linux", "Windows"} <= set(constants.CANDIDATES)


def test_profile_prefix_ends_with_separator():
    assert constants.PROFILE_PREFIX.endswith("-")


def test_engine_home_is_inside_the_kit_directory():
    assert constants.HOME_PATH.endswith(".engine")
    assert constants.HOME_PATH.startswith(constants.KIT_PATH)


def test_python_command_is_py_or_python3():
    assert constants.PY in {"py", "python3"}
