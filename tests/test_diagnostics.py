import logging
import os
import stat

import pytest

from ci_repair.diagnostics import MASK, configure_logging, has_credential, record_error, redact


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Authorization: Bearer abc.def-123", f"Authorization: Bearer {MASK}"),
        ('{"api_key": "abcd1234"}', f'{{"api_key": "{MASK}"}}'),
        ("token=ghs_" + "a" * 30 + " rest", f"token={MASK} rest"),
        ("clone https://ghp_" + "b" * 36 + "@github.com", f"clone https://{MASK}@github.com"),
        ("key sk-ant-" + "c" * 20, f"key {MASK}"),
        ("max_tokens=4096 tokens: 12", "max_tokens=4096 tokens: 12"),
    ],
)
def test_credential_shapes_are_masked(text, expected):
    assert redact(text, environ={}) == expected


def test_credential_values_from_the_environment_are_masked():
    environ = {"MY_PROVIDER_KEY": "plain-value-123", "HOME": "/home/someone-long"}
    text = "sent plain-value-123 from /home/someone-long"
    assert redact(text, environ) == f"sent {MASK} from /home/someone-long"


def test_the_traceback_is_private_and_masked(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value-xyz")
    (tmp_path / "error.log").write_text("old")
    os.chmod(tmp_path / "error.log", 0o644)
    try:
        raise RuntimeError("request failed with secret-value-xyz")
    except RuntimeError as exc:
        fields = record_error(tmp_path, exc)
    assert fields == {"error_type": "RuntimeError", "error_log": "error.log"}
    text = (tmp_path / "error.log").read_text()
    assert "Traceback" in text and "RuntimeError" in text
    assert "secret-value-xyz" not in text and MASK in text
    assert stat.S_IMODE((tmp_path / "error.log").stat().st_mode) == 0o600


def test_an_unwritable_directory_still_reports_the_type(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert record_error(blocker / "run", ValueError("x")) == {"error_type": "ValueError"}


def test_logging_covers_this_package_only(monkeypatch):
    package = logging.getLogger("ci_repair")
    monkeypatch.setattr(package, "handlers", [])
    monkeypatch.setattr(package, "level", package.level)
    monkeypatch.delenv("CI_REPAIR_LOG_LEVEL", raising=False)
    roots = list(logging.getLogger().handlers)
    configure_logging()
    configure_logging()
    assert len(package.handlers) == 1 and package.level == logging.INFO
    assert logging.getLogger().handlers == roots  # mini's records are not printed twice


def test_a_credential_is_a_host_value_or_a_token_shape_not_a_variable_name():
    environ = {"DEEPSEEK_API_KEY": "host-secret-value"}
    assert has_credential("sent host-secret-value", environ)
    assert has_credential("x = 'ghp_" + "d" * 36 + "'", {})
    assert not has_credential("token = self.config['token']\npassword: seekrit", environ)
