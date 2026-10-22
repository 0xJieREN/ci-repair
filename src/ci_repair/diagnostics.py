"""Private failure diagnostics and progress logging.

Reports are shared (draft PR bodies, evaluation artifacts), so they record only an
exception's type. The full traceback goes to a file next to the report, readable only by
the owner, with credentials masked: provider and GitHub errors can echo request headers.
"""

import logging
import os
import re
import traceback
from collections.abc import Mapping
from pathlib import Path

ERROR_LOG = "error.log"
MASK = "[REDACTED]"
# Environment variables whose values are credentials, by name.
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)
# Credentials that need not come from this process's environment: a labelled value
# ("api_key=...", "Authorization: Bearer ...") keeps its label; a known token shape does not.
SECRET_TEXT = re.compile(
    r"(?P<label>(?i:\b(?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?"
    r"(?:bearer\s+)?|\bbearer\s+))[^\s\"',}]{4,}"
    r"|\b(?:sk-[\w-]{16,}|gh[pousr]_\w{20,}|github_pat_\w{20,})"
)

logger = logging.getLogger(__name__)


def redact(text: str, environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    # Longest first, so a value that contains another is masked whole.
    values = sorted(
        (v for k, v in environ.items() if SECRET_NAME.search(k) and len(v) >= 8),
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, MASK)
    return SECRET_TEXT.sub(lambda m: (m["label"] or "") + MASK, text)


def record_error(directory: Path, exc: BaseException) -> dict:
    """Write the masked traceback to `directory/error.log` (0600); return the report fields."""
    fields = {"error_type": type(exc).__name__}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ERROR_LOG
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(redact("".join(traceback.format_exception(exc))))
        os.chmod(path, 0o600)  # also when the file already existed with wider permissions
        fields["error_log"] = ERROR_LOG
    except OSError:
        pass  # diagnostics must never replace the original failure
    logger.error("%s in %s", fields["error_type"], directory)
    return fields


def configure_logging() -> None:
    """Progress on stderr for the command-line entry points; stdout keeps their results."""
    # Only this package's loggers: a root handler would also print mini's DEBUG records.
    package = logging.getLogger("ci_repair")
    if not package.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        package.addHandler(handler)
    package.setLevel(os.getenv("CI_REPAIR_LOG_LEVEL", "INFO").upper())
