"""Read-only receipt of the service's native research connection."""
import subprocess
from typing import TypedDict

from .codex_auth import AuthStatus, CodexAuth


class ConnectionStatus(TypedDict):
    auth: AuthStatus
    cli_version: str | None
    credential_store: str | None
    execution: str
    model_access: str


def connection_status(auth: CodexAuth) -> ConnectionStatus:
    status = dict(auth.status(), login=None)  # Challenges stay on the same-origin auth routes.
    version = None
    if status['available'] and auth.executable:
        try:
            version = subprocess.run([auth.executable, '--version'], capture_output=True,
                                     text=True, check=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    return {'auth': status, 'cli_version': version,
            'credential_store': auth.store if status['available'] else None,
            'execution': 'service_host',
            'model_access': 'Checked when research starts; sign-in alone does not establish model access.'}
