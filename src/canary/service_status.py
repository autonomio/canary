"""Read-only runtime discovery, distinct from login and scientific readiness."""
from pathlib import Path
import shutil
import subprocess
from typing import TypedDict

from .codex_execution import ExecutionStatus


class PdfExtractionStatus(TypedDict):
    engine: str
    layout_preserved: bool
    concurrent_pdf_conversions: int
    capacity_scope: str


class ServiceStatus(TypedDict):
    service: str
    data_directory: str
    dashboard_enabled: bool
    codex: dict
    pdf_extraction: PdfExtractionStatus
    execution: ExecutionStatus
    readiness_note: str


def service_status(root: Path, dashboard_enabled: bool, *, execution=None) -> ServiceStatus:
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
            'execution': execution or {'backend': 'shared_codex_app_server', 'running': False,
                                       'active_shared_sessions': 0, 'active_exec_sessions': 0},
            'pdf_extraction': {'engine': 'pdftotext' if shutil.which('pdftotext') else 'pypdf',
                               'layout_preserved': bool(shutil.which('pdftotext')),
                               'concurrent_pdf_conversions': 1, 'capacity_scope': 'service_data_directory'},
            'readiness_note': 'Runtime availability only. See /api/auth/status for login; model access and source availability are checked during research.'}
