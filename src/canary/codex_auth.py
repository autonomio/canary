"""Service-owned native Codex authentication; never handles OAuth tokens itself."""
from __future__ import annotations

from datetime import datetime, timezone
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
    # The verified account email the native CLI itself reports, for operator
    # identity display only. Null when signed out, on API-key sign-in, or when
    # the CLI does not report one. No other account field is ever relayed.
    email: str | None
    login: Login | None
    error: str | None


class UsageWindow(TypedDict):
    used_percent: float
    window_duration_mins: int | None
    resets_at: str | None
    resets_at_epoch: float | None


class AuthUsage(TypedDict):
    """Provider-reported rate-limit windows for the connected account.

    Only window percentages, durations and reset times, the plan name, and
    the backend's own usage permission are relayed — never credits,
    balances, account identifiers, upsells, or reset-credit detail.
    """
    available: bool
    observed_at: str
    windows: list[UsageWindow]
    plan_type: str | None
    limit_reached: bool | None
    ordinary_usage_allowed: bool | None
    error: str | None


class AuthError(RuntimeError):
    def __init__(self, message: str, code: str = 'codex_unavailable', status: int = 503):
        super().__init__(message)
        self.code, self.status = code, status


def credential_home() -> str:
    return str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve())


def credential_store(value: str) -> str:
    if value not in ('file', 'keyring', 'auto'):
        raise AuthError('Codex needs persistent credentials (file, keyring or auto); ephemeral credentials cannot be shared with research workers.',
                        'unsupported_credential_store')
    return value


class CodexAuth:
    """One lazy app-server; Codex owns storage and refresh.

    Scientific sessions share its multiplexed transport through CodexExecution.
    Login challenges live in memory and are removed at completion/cancellation.
    """
    def __init__(self, home: str | None = None):
        self.home = home or credential_home()
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.messages: queue.Queue = queue.Queue()
        self.sequence = 0
        self.rpc_lock = threading.RLock()
        self.pending: dict = {}
        self.listeners: dict = {}
        self.login: Login | None = None
        self.completions: dict = {}
        self.timeout = 15.0
        self.store: str | None = None
        self.executable: str | None = None
        self._usage_cache: tuple[float, AuthUsage] | None = None

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
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
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
        executable = str(Path(executable).resolve())
        self.messages = messages = queue.Queue()
        self.pending = pending = {}
        self.listeners = listeners = {}
        self.completions.clear()
        try:
            Path(self.home).mkdir(parents=True, exist_ok=True, mode=0o700)
            self.executable = executable
            command = [executable, 'app-server']
            override = os.environ.get('CANARY_CODEX_CREDENTIALS_STORE')
            if override:
                command += ['-c', 'cli_auth_credentials_store=' + json.dumps(credential_store(override))]
            self.proc = subprocess.Popen(
                command, cwd=self.home,
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
                        message = json.loads(line)
                        with self.rpc_lock:
                            response = pending.pop(message.get('id'), None) if 'method' not in message else None
                            params = message.get('params') or {}
                            thread_id = params.get('threadId') or (params.get('thread') or {}).get('id')
                            listener = listeners.get(thread_id)
                        if response is not None:
                            response.put(message)
                        elif listener is not None:
                            listener(message)
                        else:
                            messages.put(message)
                    except ValueError:
                        pass
            finally:
                failure = {'error': {'code': 'connection_closed'}}
                with self.rpc_lock:
                    waiting = list(pending.values())
                    pending.clear()
                    callbacks = list(listeners.values())
                for waiting_request in waiting:
                    waiting_request.put(failure)
                for callback in callbacks:
                    callback({'method': 'canary/connectionClosed', 'params': {}})
                messages.put(None)

        threading.Thread(target=read, daemon=True).start()
        try:
            self._rpc('initialize', {'clientInfo': {'name': 'canary', 'version': '0.1.0'}})
            self._write({'method': 'initialized'})
            config = self._rpc('config/read', {'includeLayers': False})
            self.store = credential_store(config.get('config', {}).get('cli_auth_credentials_store'))
        except AuthError:
            self._stop()
            raise

    def worker_environment(self) -> dict[str, str]:
        """Pin the native runtime and resolved store; never read or move credentials."""
        with self.lock:
            self._start()
            return {'CODEX_HOME': self.home, 'CANARY_CODEX_EXECUTABLE': self.executable,
                    'CANARY_CODEX_CREDENTIALS_STORE': self.store}

    def _write(self, message: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(message) + '\n')
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise AuthError('Codex authentication connection closed.') from None

    def _event(self, message: dict) -> None:
        with self.lock:
            if message.get('method') == 'account/login/completed':
                self._usage_cache = None  # A different account may have signed in.
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

    def _rpc(self, method: str, params: dict | None = None, *, generation=None) -> dict:
        # Only ID assignment and the pipe write serialize; independent scientific
        # requests keep receiving replies and events while an account call waits.
        response = queue.Queue(maxsize=1)
        with self.rpc_lock:
            if generation is not None and self.proc is not generation:
                raise AuthError('The Codex execution connection was replaced.')
            self.sequence += 1
            request_id = self.sequence
            pending = self.pending
            pending[request_id] = response
            try:
                self._write({'id': request_id, 'method': method, 'params': params or {}})
            except Exception:
                pending.pop(request_id, None)
                raise
        try:
            message = response.get(timeout=self.timeout)
        except queue.Empty:
            raise AuthError('Codex request timed out; retry.') from None
        finally:
            with self.rpc_lock:
                pending.pop(request_id, None)
        # Login-completed can precede its start response. Preserve those events
        # without taking another session's replies off the transport.
        while True:
            try:
                event = self.messages.get_nowait()
            except queue.Empty:
                break
            if event is not None:
                self._event(event)
        if 'error' in message:
            # Provider error text can contain credentials or login URLs.
            raise AuthError('Codex rejected the request or its connection closed.',
                            'codex_connection_closed' if message['error'].get('code') == 'connection_closed' else 'codex_request_failed', 502)
        result = message.get('result')
        if not isinstance(result, dict):
            raise AuthError('Codex returned an unsupported response.')
        return result

    def _call(self, method, params=None):
        try:
            self._start()
            return self._rpc(method, params)
        except AuthError as error:
            if error.code != 'codex_request_failed':
                self._stop()
            raise

    def _call_advisory(self, method, params=None):
        """A best-effort read that can never stop the shared app-server.

        Scientific sessions multiplex on this transport, so an advisory
        call must not own its lifecycle: timeouts and transport errors are
        the caller's to report, while the owning status/execution paths
        still stop a genuinely broken process on their own calls.
        """
        self._start()
        return self._rpc(method, params)

    def _drain_events(self) -> None:
        """Apply queued notifications (sign-in completion clears usage)."""
        while True:
            try:
                event = self.messages.get_nowait()
            except queue.Empty:
                return
            if event is None:
                self._usage_cache = None
                return
            self._event(event)

    def status(self) -> AuthStatus:
        with self.lock:
            try:
                was_pending = self.login and self.login['state'] == 'pending'
                result = self._call('account/read', {'refreshToken': False})
                while True:
                    try:
                        message = self.messages.get_nowait()
                    except queue.Empty:
                        break
                    if message is None:
                        raise AuthError('Codex authentication connection closed.')
                    self._event(message)
                if was_pending and self.login['state'] == 'complete':
                    # The completion event can follow the read's old snapshot.
                    result = self._call('account/read', {'refreshToken': False})
                account = result.get('account') or {}
                mode = account.get('type')
                authenticated = mode in ('chatgpt', 'apiKey')
                email = account.get('email') if authenticated and mode == 'chatgpt' else None
                if not isinstance(email, str) or not email or len(email) > 320:
                    email = None
                return dict(available=True, authenticated=authenticated,
                            state='signed_in' if authenticated else 'signed_out',
                            mode=mode if authenticated else None, email=email,
                            login=dict(self.login) if self.login else None, error=None)
            except AuthError as error:
                if error.code != 'codex_request_failed':
                    self._stop()
                return dict(available=False, authenticated=False, state='unavailable', mode=None,
                            email=None,
                            login=dict(self.login) if self.login else None, error=str(error))

    def usage(self) -> AuthUsage:
        """Rate-limit windows the provider backend reports for this account.

        A backend read at most once a minute; callers get the cached
        observation with its original observed_at, never a fresher-looking
        restamp. Unavailable is a successful read, like status.
        """
        with self.lock:
            # A queued sign-in completion belongs to a newer account than
            # the cache; apply it (and any close) before trusting the cache.
            self._drain_events()
            if self._usage_cache and time.monotonic() - self._usage_cache[0] < 60:
                return dict(self._usage_cache[1])
            observed = datetime.now(timezone.utc).isoformat()
            try:
                result = self._call_advisory('account/rateLimits/read',
                                              {'excludeResetCreditDetails': True})
                snapshot = result.get('rateLimits') or {}
                windows = []
                for name in ('primary', 'secondary'):
                    window = snapshot.get(name)
                    if not isinstance(window, dict):
                        continue
                    used = window.get('usedPercent')
                    if isinstance(used, bool) or not isinstance(used, (int, float)):
                        continue
                    resets = window.get('resetsAt')
                    if isinstance(resets, bool) or not isinstance(resets, (int, float)):
                        resets = None
                    duration = window.get('windowDurationMins')
                    if isinstance(duration, bool) or not isinstance(duration, int):
                        duration = None
                    windows.append(dict(
                        used_percent=float(used), window_duration_mins=duration,
                        resets_at=datetime.fromtimestamp(resets, timezone.utc).isoformat()
                        if resets is not None else None,
                        resets_at_epoch=float(resets) if resets is not None else None))
                plan = snapshot.get('planType')
                if not isinstance(plan, str) or not plan or len(plan) > 80:
                    plan = None
                allowed = result.get('ordinaryUsageAllowed')
                usage = dict(available=True, observed_at=observed, windows=windows,
                             plan_type=plan,
                             limit_reached=snapshot.get('rateLimitReachedType') is not None,
                             ordinary_usage_allowed=allowed if isinstance(allowed, bool) else None,
                             error=None)
            except (AuthError, OverflowError, OSError, ValueError):
                # Advisory: never stop the shared app-server over usage.
                usage = dict(available=False, observed_at=observed, windows=[], plan_type=None,
                             limit_reached=None, ordinary_usage_allowed=None,
                             error='Provider usage status is unavailable.')
            self._usage_cache = (time.monotonic(), usage)
            return dict(usage)

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
            self._usage_cache = None
            return self.status()
