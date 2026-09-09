"""
.env parsing rules.

These pin the behaviour a hand-edited `.env` must have: inline comments are
stripped, quoted values keep their content (including '#'), and a '#' glued
onto an unquoted value is data, not a comment. The regression that motivated
the tests: `APP_ENV=development  # development | production` used to load the
comment *into* the value, so switching to `production # ...` would have left
the app silently in development mode.
"""

from __future__ import annotations

from app.config import _load_env_file, _parse_value


def test_parse_unquoted_value_with_inline_comment():
    assert _parse_value("development                 # development | production") == "development"
    assert _parse_value("production  # comment") == "production"
    assert _parse_value("8000   # port") == "8000"
    assert _parse_value("true#no-space") == "true#no-space"  # glued '#' is data
    assert _parse_value("#only-a-comment") == ""


def test_parse_quoted_values():
    assert _parse_value('"Personal Content Tracker"') == "Personal Content Tracker"
    assert _parse_value('"abc#def"  # trailing comment') == "abc#def"
    assert _parse_value("'single # quoted'") == "single # quoted"
    assert _parse_value('"unterminated') == "unterminated"
    assert _parse_value('""') == ""


def test_load_env_file_full_line_shapes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# full-line comment",
                "",
                "export PCT_TEST_EXPORTED=hello",
                "PCT_TEST_APP_ENV=production   # must not leak into the value",
                'PCT_TEST_QUOTED="a # b"  # comment after quotes',
                "PCT_TEST_GLUE=pass#word",
                "NOT_A_LINE",
            ]
        ),
        encoding="utf-8",
    )
    keys = ["PCT_TEST_EXPORTED", "PCT_TEST_APP_ENV", "PCT_TEST_QUOTED", "PCT_TEST_GLUE"]
    for k in keys:
        monkeypatch.delenv(k, raising=False)

    _load_env_file(env_file)
    try:
        import os

        assert os.environ["PCT_TEST_EXPORTED"] == "hello"
        assert os.environ["PCT_TEST_APP_ENV"] == "production"
        assert os.environ["PCT_TEST_QUOTED"] == "a # b"
        assert os.environ["PCT_TEST_GLUE"] == "pass#word"
    finally:
        for k in keys:
            monkeypatch.delenv(k, raising=False)


def test_existing_environment_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PCT_TEST_PRESET=from-file\n", encoding="utf-8")
    monkeypatch.setenv("PCT_TEST_PRESET", "from-process")
    _load_env_file(env_file)
    import os

    assert os.environ["PCT_TEST_PRESET"] == "from-process"


def test_production_flag_survives_a_commented_env_file(tmp_path, monkeypatch):
    """The security-relevant case: APP_ENV=production with a sample comment."""
    from app.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=production   # development | production\n", encoding="utf-8")
    monkeypatch.delenv("APP_ENV", raising=False)
    _load_env_file(env_file)
    try:
        assert Settings().is_production is True
    finally:
        monkeypatch.delenv("APP_ENV", raising=False)
