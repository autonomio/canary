"""Read-only runtime discovery, distinct from login and scientific readiness."""
from pathlib import Path
import shutil
import subprocess
from typing import TypedDict


class ServiceStatus(TypedDict):
    service: str
    data_directory: str
    dashboard_enabled: bool
    codex: dict
    pdf_extraction: dict
    readiness_note: str


def service_status(root: Path, dashboard_enabled: bool) -> ServiceStatus:
    executable = shutil.which('codex')
    version = None
    if executable:
        try:
            version = subprocess.run([executable, '--version'], capture_output=True,
                                     text=True, check=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {'service': 'available', 'data_directory': str(Path(root).resolve()),
            'dashboard_enabled': dashboard_enabled,
            'codex': {'available': bool(executable and version), 'version': version},
            'pdf_extraction': {'engine': 'pdftotext' if shutil.which('pdftotext') else 'pypdf',
                               'layout_preserved': bool(shutil.which('pdftotext'))},
            'readiness_note': 'Runtime availability only. See /api/auth/status for login; model access and source availability are checked during research.'}
