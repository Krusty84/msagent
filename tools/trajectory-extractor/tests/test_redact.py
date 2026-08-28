from trajectory_extractor.redact import redact_text, redact_value


def test_secret_assignments_are_scrubbed_but_key_names_kept():
    text = "export TAVILY_API_KEY=tvly-abc123def456 && echo done"
    result = redact_text(text)
    assert "tvly-abc123def456" not in result
    assert "TAVILY_API_KEY=<REDACTED>" in result


def test_quoted_secret_value_is_scrubbed():
    assert redact_text('PASSWORD = "hunter2 with spaces"') == "PASSWORD = <REDACTED>"


def test_vendor_tokens_are_scrubbed_without_an_assignment():
    text = "curl -H 'x: sk-0123456789abcdefghij' https://example.org"
    assert "sk-0123456789abcdefghij" not in redact_text(text)


def test_authorization_header_is_scrubbed():
    assert redact_text("Authorization: Bearer abcdef.ghijkl") == "Authorization: Bearer <REDACTED>"


def test_url_credentials_are_scrubbed():
    assert redact_text("https://alice:s3cret@git.example.org/x") == ("https://<REDACTED>@git.example.org/x")


def test_home_directory_keeps_shape_and_loses_account_name():
    assert redact_text("/home/kirill/models/qwen3") == "/home/<USER>/models/qwen3"
    assert redact_text("/Users/kirill/data") == "/Users/<USER>/data"


def test_home_redaction_does_not_create_a_fake_email():
    assert "@" not in redact_text("/home/kirill")


def test_emails_are_scrubbed():
    assert redact_text("ping dev@example.org please") == "ping <EMAIL> please"


def test_redact_value_walks_nested_structures():
    payload = {"cmd": ["echo /home/bob/x", {"API_TOKEN": "ghp_aaaaaaaaaaaaaaaaaaaaaa"}], "n": 3}
    result = redact_value(payload)
    assert result["cmd"][0] == "echo /home/<USER>/x"
    assert "ghp_" not in str(result["cmd"][1])
    assert result["n"] == 3


def test_plain_text_is_untouched():
    text = "run msprof-analyze on the profiling directory"
    assert redact_text(text) == text
