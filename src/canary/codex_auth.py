"""Service-owned native Codex authentication; never handles OAuth tokens itself."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time
from typing import Literal, TypedDict
from urllib.parse import urlparse


class LoginRequest(TypedDict):
    method: Literal['browser', 'device']


class AuthFailure(TypedDict):
    error: str
    code: str


class Login(TypedDict):
    id: str
    method: Literal['browser', 'device']
    state: Literal['pending', 'complete', 'failed', 'cancelled']
    url: str | None
    user_code: str | None
    error: str | None


class AuthStatus(TypedDict):
    available: bool
    authenticated: bool
    state: Literal['signed_in', 'signed_out', 'unavailable']
    mode: str | None
    login: Login | None
    error: str | None


class AuthError(RuntimeError):
    def __init__(self, message: str, code: str = 'codex_unavailable', status: int = 503):
        super().__init__(message)
        self.code, self.status = code, status


def credential_home() -> str:
    return str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve())


class CodexAuth:
    """One lazy app-server, serialized RPCs; Codex owns storage and refresh.

    The process only receives account RPCs, never scientific prompts or threads.
    Login challenges live in memory and are removed at completion/cancellation.
    """
    def __init__(self, home: str | None = None):
        self.home = home or credential_home()
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.messages: queue.Queue = queue.Queue()
        self.sequence = 0
        self.login: Login | None = None
        self.completions: dict = {}
        self.timeout = 15.0

    def _stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    stream.close()
        if self.login and self.login['state'] == 'pending':
            self._finish('failed', 'Login stopped with the authentication service; start again.')

    def close(self) -> None:
        with self.lock:
            self._stop()  # stopping a login process does not log out the account

    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self._stop()
        executable = shutil.which('codex')
        if not executable:
            raise AuthError('Codex CLI is not installed on the service host.')
        self.messages = messages = queue.Queue()
        self.completions.clear()
        try:
            Path(self.home).mkdir(parents=True, exist_ok=True, mode=0o700)
            self.proc = subprocess.Popen(
                [executable, 'app-server', '-c', 'cli_auth_credentials_store="file"'],
                env=dict(os.environ, CODEX_HOME=self.home),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', start_new_session=True)
        except OSError:
            raise AuthError('Codex authentication could not start.') from None
        proc = self.proc

        def read():
            try:
                for line in proc.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        pass
            finally:
                messages.put(None)

        threading.Thread(target=read, daemon=True).start()
        try:
            self._rpc('initialize', {'clientInfo': {'name': 'canary', 'version': '0.1.0'}})
            self._write({'method': 'initialized'})
        except AuthError:
            self._stop()
            raise

    def _write(self, message: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(message) + '\n')
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise AuthError('Codex authentication connection closed.') from None

    def _event(self, message: dict) -> None:
        if message.get('method') == 'account/login/completed':
            event = message.get('params', {})
            login_id = event.get('loginId')
            if isinstance(login_id, str):
                self.completions[login_id] = bool(event.get('success'))
            self._apply_completion()

    def _finish(self, state, error=None):
        self.login.update(state=state, error=error, url=None, user_code=None)

    def _apply_completion(self):
        if self.login and self.login['state'] == 'pending' and self.login['id'] in self.completions:
            success = self.completions.pop(self.login['id'])
            self._finish('complete' if success else 'failed',
                         None if success else 'Codex login failed. Retry, or use device login.')

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self.sequence += 1
        request_id = self.sequence
        self._write({'id': request_id, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise AuthError('Codex authentication timed out; retry.') from None
            if message is None:
                raise AuthError('Codex authentication connection closed.')
            if message.get('id') == request_id:
                if 'error' in message:
                    # Never forward provider error text: it can contain auth URLs or secrets.
                    raise AuthError('Codex rejected the authentication action. Retry or use device login.',
                                    'codex_auth_failed', 502)
                result = message.get('result')
                if not isinstance(result, dict):
                    raise AuthError('Codex returned an unsupported authentication response.')
                return result
            self._event(message)

    def _call(self, method, params=None):
        try:
            self._start()
            return self._rpc(method, params)
        except AuthError:
            self._stop()
            raise

    def status(self) -> AuthStatus:
        with self.lock:
            try:
                was_pending = self.login and self.login['state'] == 'pending'
                result = self._call('account/read', {'refreshToken': False})
                while not self.messages.empty():
                    message = self.messages.get_nowait()
                    if message is None:
                        raise AuthError('Codex authentication connection closed.')
                    self._event(message)
                if was_pending and self.login['state'] == 'complete':
                    # The completion event can follow the read's old snapshot.
                    result = self._call('account/read', {'refreshToken': False})
                account = result.get('account') or {}
                mode = account.get('type')
                authenticated = mode in ('chatgpt', 'apiKey')
                return dict(available=True, authenticated=authenticated,
                            state='signed_in' if authenticated else 'signed_out',
                            mode=mode if authenticated else None,
                            login=dict(self.login) if self.login else None, error=None)
            except AuthError as error:
                self._stop()
                return dict(available=False, authenticated=False, state='unavailable', mode=None,
                            login=dict(self.login) if self.login else None, error=str(error))

    def start_login(self, method: str = 'browser') -> Login:
        if method not in ('browser', 'device'):
            raise AuthError('method must be browser or device', 'invalid_request', 400)
        with self.lock:
            if self.login and self.login['state'] == 'pending':
                self.status()  # consume completion before reusing the pending flow
                if self.login['state'] == 'pending':
                    return dict(self.login)
            result = self._call('account/login/start',
                                {'type': 'chatgptDeviceCode' if method == 'device' else 'chatgpt'})
            url = result.get('verificationUrl' if method == 'device' else 'authUrl', '')
            parsed = urlparse(url)
            login_id = result.get('loginId')
            code = result.get('userCode') if method == 'device' else None
            if (parsed.scheme != 'https' or parsed.hostname not in ('auth.openai.com', 'chatgpt.com')
                    or parsed.username or parsed.password or not isinstance(login_id, str)
                    or not login_id or (method == 'device' and not isinstance(code, str))):
                self._stop()
                raise AuthError('Codex returned an unsupported login challenge.')
            self.login = dict(id=login_id, method=method, state='pending', url=url, user_code=code, error=None)
            self._apply_completion()
            return dict(self.login)

    def progress(self, login_id: str) -> Login:
        with self.lock:
            self.status()
            if not self.login or self.login['id'] != login_id:
                raise AuthError('Unknown login; start a new login.', 'login_not_found', 404)
            return dict(self.login)

    def cancel(self, login_id: str) -> Login:
        with self.lock:
            self.progress(login_id)
            if self.login['state'] == 'pending':
                result = self._call('account/login/cancel', {'loginId': login_id})
                if self.login['state'] == 'pending':
                    if result.get('status') == 'canceled':
                        self._finish('cancelled')
                    elif result.get('status') == 'notFound':
                        self.status()  # completion can race cancellation
                        if self.login['state'] == 'pending':
                            self._finish('failed', 'Login is no longer active; check account status or start again.')
                    else:
                        self._stop()
                        raise AuthError('Codex returned an unsupported cancellation response.')
            return dict(self.login)

    def logout(self) -> AuthStatus:
        with self.lock:
            if self.login and self.login['state'] == 'pending':
                self.cancel(self.login['id'])
            self._call('account/logout')
            self.login = None
            return self.status()
