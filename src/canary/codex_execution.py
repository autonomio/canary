"""API-owned Codex sessions: multiplex one native server, retain exact CLI fallback."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Literal, TypedDict

from .codex_auth import AuthError
from .processes import RunCancelled, identity, stop_tree

SHARED = 'shared_codex_app_server'
LEGACY = 'codex_exec'
FALLBACK = ('Personal Codex configuration requires --ignore-user-config, which app-server '
            'does not support. This session uses isolated codex exec.')


class ExecutionRequest(TypedDict):
    role: Literal['research', 'audit']


class ExecutionStatus(TypedDict):
    backend: Literal['shared_codex_app_server']
    running: bool
    active_shared_sessions: int
    active_exec_sessions: int


class ExecutionFinish(TypedDict):
    cancelled: bool


class ExecutionFinished(ExecutionFinish):
    key: str


class ExecutionReceipt(TypedDict, total=False):
    backend: Literal['shared_codex_app_server', 'codex_exec']
    fallback_reason: str | None
    cli_version: str
    thread_id: str | None
    model: str
    reasoning_effort: str
    started_at: str
    ended_at: str
    seconds: float
    exit_code: int
    timed_out: bool
    turn_usage: list[dict]
    usage: dict
    cleanup: dict
    subagents_enabled: bool
    configuration_policy: str


class CodexExecution:
    def __init__(self, auth, can_start=None):
        self.auth, self.can_start = auth, can_start
        self.lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.version = None
        self.closing = False
        self.cancelled_runs: set[str] = set()

    def context(self):
        with self.lock:
            if self.closing:
                raise RuntimeError('The API-owned execution runtime is stopping')
            return self.auth.worker_environment()

    def status(self) -> ExecutionStatus:
        with self.lock:
            return {'backend': SHARED, 'running': bool(self.auth.proc and self.auth.proc.poll() is None),
                    'active_shared_sessions': sum(s.backend == SHARED for s in self.sessions.values()),
                    'active_exec_sessions': sum(s.backend == LEGACY for s in self.sessions.values())}

    def open(self, key, role, workspace, model, effort, shape, output, stderr, command, environment=None):
        self.context()
        with self.auth.lock:
            generation = self.auth.proc
            executable, store = self.auth.executable, self.auth.store
        config = self.auth._rpc('config/read', {'includeLayers': True, 'cwd': str(workspace)}, generation=generation)
        # Native exec can omit the entire personal config layer. App-server 0.162
        # cannot: overriding selected keys would leave other scientific inputs live.
        layers = config.get('layers')
        shared = isinstance(layers, list) and all(
            layer.get('disabledReason') or (isinstance(layer.get('config'), dict) and
            (layer.get('name', {}).get('type') != 'user' or
             not (set(layer['config']) - {'cli_auth_credentials_store'})))
            for layer in layers)
        with self.lock:
            if self.closing:
                raise RuntimeError('The API-owned execution runtime is stopping')
            if key in self.cancelled_runs or self.can_start is not None and not self.can_start(key):
                raise RunCancelled('Research was stopped before scientific execution started')
            if key in self.sessions:
                raise ValueError('A scientific session is already active for this run')
            if self.version is None:
                self.version = subprocess.run([executable, '--version'], capture_output=True,
                    text=True, check=True, timeout=5).stdout.strip()
            session = Session(self, key, role, workspace, model, effort, shape, output,
                              stderr, command, shared, generation, store, environment or {})
            self.sessions[key] = session
        return session

    def cancel(self, key, *, remember=True):
        with self.lock:
            if remember:
                self.cancelled_runs.add(key)
            session = self.sessions.get(key)
        if session:
            session.interrupt()
        return session

    def close(self):
        with self.lock:
            self.closing = True
            sessions = list(self.sessions.values())
        for session in sessions:
            session.cancelled.set()
            session.messages.put({'method': 'canary/connectionClosed', 'params': {}})
        # The service owns this shared process, never an individual run worker.
        self.auth.close()
        for session in sessions:
            if session.process is not None:
                session.interrupt()
        # Let API handlers seal their receipts before the service exits. This is
        # one bounded grace period shared by all sessions, not one wait per run.
        deadline = time.monotonic() + 5
        for session in sessions:
            session.finished.wait(timeout=max(0, deadline - time.monotonic()))
        self.auth.close()


class Session:
    def __init__(self, owner, key, role, workspace, model, effort, shape, output, stderr,
                 command, shared, generation, store, environment):
        self.owner, self.auth, self.key = owner, owner.auth, key
        self.role, self.workspace, self.model, self.effort = role, Path(workspace), model, effort
        self.shape, self.output, self.stderr, self.command = shape, Path(output), stderr, command
        self.backend = SHARED if shared else LEGACY
        self.generation, self.store, self.environment = generation, store, environment
        self.home = self.auth.home
        self.messages = queue.Queue()
        self.cancelled, self.finished = threading.Event(), threading.Event()
        self.thread_id, self.turn_id, self.process = None, None, None
        self.process_receipt = None
        self.exit_code, self.last_message = None, None
        self.cancel_at = None
        self.cleanup = {}

    def invocation(self):
        return {'backend': self.backend, 'fallback_reason': None if self.backend == SHARED else FALLBACK,
                'cli_version': self.owner.version, 'subagents_enabled': False,
                'configuration_policy': 'native_clean_or_credentials_only' if self.backend == SHARED else 'ignore_user_config'}

    def _receive(self, message):
        # Token/reasoning deltas are not operational progress and can be very
        # frequent. Keep complete native receipts without queuing those copies.
        if 'id' in message or message.get('method') in ('item/completed', 'turn/completed', 'canary/connectionClosed'):
            self.messages.put(message)

    def _rpc(self, method, params):
        return self.auth._rpc(method, params, generation=self.generation)

    def start(self, prompt):
        if self.cancelled.is_set():
            self.exit_code = 130
            return
        if self.backend == LEGACY:
            import os
            process = subprocess.Popen(self.command, cwd=self.workspace, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr, text=True, start_new_session=True,
                env=dict(os.environ, CODEX_HOME=self.home, CANARY_CODEX_CREDENTIALS_STORE=self.store, **self.environment))
            self.process_receipt = {'pid': process.pid, 'identity': identity(process.pid)}
            self.process = process
            if self.cancelled.is_set():
                self.interrupt()
                self.exit_code = process.wait()
                return
            self.process.stdin.write(prompt)
            self.process.stdin.close()
        else:
            config = {'cli_auth_credentials_store': self.store, 'model_reasoning_effort': self.effort,
                'agents.enabled': False, 'features.multi_agent': False, 'features.multi_agent_v2': False,
                'features.memories': False, 'features.plugins': False, 'features.apps': False,
                'web_search': 'disabled', 'sandbox_workspace_write.network_access': True,
                'shell_environment_policy.set': self.environment}
            result = self._rpc('thread/start', {'cwd': str(self.workspace), 'model': self.model,
                'approvalPolicy': 'on-request', 'approvalsReviewer': 'auto_review',
                'sandbox': 'workspace-write', 'config': config})
            self.thread_id = result['thread']['id']
            if (result.get('model') != self.model or result.get('reasoningEffort') != self.effort or
                    result.get('cwd') != str(self.workspace)):
                raise RuntimeError('Codex did not preserve the scientific model, effort or workspace')
            with self.auth.rpc_lock:
                self.auth.listeners[self.thread_id] = self._receive
            if self.cancelled.is_set():
                self.exit_code = 130
                return
            result = self._rpc('turn/start', {'threadId': self.thread_id, 'effort': self.effort,
                'input': [{'type': 'text', 'text': prompt}], 'outputSchema': self.shape})
            self.turn_id = result['turn']['id']
        if self.cancelled.is_set():
            self.interrupt()

    def __iter__(self):
        if self.backend == LEGACY and self.process is not None:
            yield from self.process.stdout
            self.exit_code = self.process.wait()
            return
        if self.thread_id:
            yield json.dumps({'type': 'thread.started', 'thread_id': self.thread_id}) + '\n'
        while self.exit_code is None:
            try:
                message = self.messages.get(timeout=.25)
            except queue.Empty:
                if self.cancel_at and time.monotonic() - self.cancel_at > 10:
                    self.exit_code = 130
                continue
            method, params = message.get('method'), message.get('params', {})
            if 'id' in message and method:
                # Auto-review normally resolves approvals natively. Never leave a
                # scientific turn waiting for an unimplemented interactive client.
                with self.auth.rpc_lock:
                    self.auth._write({'id': message['id'], 'error': {
                        'code': -32601, 'message': 'Canary cannot answer this interactive Codex request'}})
                self.interrupt()
                yield json.dumps({'type': 'error', 'message': 'Scientific session requested unsupported interaction'}) + '\n'
            elif method == 'canary/connectionClosed':
                self.exit_code = 1
                yield json.dumps({'type': 'error', 'message': 'Shared Codex execution connection closed; no automatic rerun'}) + '\n'
            elif method == 'item/completed':
                item = params.get('item', {})
                if item.get('type') == 'agentMessage':
                    self.last_message = item.get('text')
                    item = {'type': 'agent_message', 'text': self.last_message}
                yield json.dumps({'type': 'item.completed', 'item': item}, ensure_ascii=False) + '\n'
            elif method == 'turn/completed':
                turn = params.get('turn', {})
                self.exit_code = 0 if turn.get('status') == 'completed' and not self.cancelled.is_set() else 130 if self.cancelled.is_set() else 1
                if self.exit_code == 0 and self.last_message:
                    self.output.write_text(self.last_message, encoding='utf-8')
                yield json.dumps({'type': 'turn.completed' if self.exit_code == 0 else 'turn.failed',
                                  'status': turn.get('status')}) + '\n'

    def interrupt(self):
        self.cancelled.set()
        self.cancel_at = self.cancel_at or time.monotonic()
        if self.process is not None:
            if self.process.poll() is None and self.process_receipt:
                stop_tree(self.process_receipt, grace=5)
        elif self.thread_id and self.turn_id:
            try:
                self._rpc('turn/interrupt', {'threadId': self.thread_id, 'turnId': self.turn_id})
            except AuthError:
                self.messages.put({'method': 'canary/connectionClosed', 'params': {}})

    def wait(self):
        if self.process is not None:
            self.exit_code = self.process.wait()
        return self.exit_code if self.exit_code is not None else 130 if self.cancelled.is_set() else 1

    def archive(self):
        if self.backend == SHARED and self.thread_id:
            try:
                self._rpc('thread/archive', {'threadId': self.thread_id})
                self.cleanup = {'archived': True}
            except AuthError:
                self.cleanup = {'archived': False, 'error': 'Native thread archival unavailable'}
            finally:
                with self.auth.rpc_lock:
                    self.auth.listeners.pop(self.thread_id, None)
        return self.cleanup

    def close(self):
        if self.exit_code is None:
            self.interrupt()
            self.wait()
        if not self.cleanup:
            self.archive()
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        with self.owner.lock:
            if self.owner.sessions.get(self.key) is self:
                self.owner.sessions.pop(self.key)
        self.finished.set()
