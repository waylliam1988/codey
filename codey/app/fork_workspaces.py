"""Owned Git worktree setup; source index, branch and files are never changed."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast

from codey.storage.local_store import read_json_strict, write_json_atomic


def git(root: Path, *args: str, env: dict[str, str] | None = None, data: bytes | None = None, timeout: int = 120) -> str:
    environment = dict(os.environ)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_PREFIX', 'GIT_NAMESPACE'):
        environment.pop(key, None)
    environment.update(env or {})
    result = subprocess.run(
        ['git', '-C', str(root), *args], input=data, capture_output=True,
        env=environment, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if result.returncode:
        raise ValueError(result.stderr.decode('utf8', 'replace').strip()[:1000] or 'Git worktree setup failed.')
    return result.stdout.decode('utf8', 'replace').strip()


def capabilities(path: str) -> dict[str, object]:
    if not path:
        return {'worktree': False, 'is_worktree': False}
    root = Path(path)
    try:
        git(root, 'rev-parse', '--verify', 'HEAD', timeout=5)
        top = Path(git(root, 'rev-parse', '--show-toplevel', timeout=5)).resolve()
        return {'worktree': True, 'is_worktree': (top / '.git').is_file()}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'worktree': False, 'is_worktree': False}


class ForkWorkspaces:
    def __init__(self, ctx: Any, home: Path) -> None:
        self.ctx = ctx
        self.home = home / 'chat-forks'
        self.lock = threading.Lock()
        self.workers: dict[str, threading.Thread] = {}

    def _file(self, child: str) -> Path:
        if not child.startswith('fork_') or len(child) != 37 or any(c not in '0123456789abcdef' for c in child[5:]):
            raise ValueError('Invalid fork identity.')
        return self.home / f'{child}.json'

    def status(self, child: str) -> dict[str, Any] | None:
        path = self._file(child)
        if not path.exists():
            return None
        with self.lock:
            data = cast(dict[str, Any], read_json_strict(path))
            worker = self.workers.get(child)
            if data['status'] == 'pending' and (worker is None or not worker.is_alive()):
                data = cast(dict[str, Any], read_json_strict(path))
                if data['status'] == 'pending':
                    data.update(status='error', error='Worktree setup was interrupted. Retry to continue.')
                    write_json_atomic(path, data)
        return data

    def start(self, source: str, child: str, entry: str, workspace: str) -> dict[str, Any]:
        if self.ctx._close_requested:
            raise ValueError('The server is shutting down. Retry after restart.')
        with self.lock:
            path = self._file(child)
            data: dict[str, Any] = cast(dict[str, Any], read_json_strict(path)) if path.exists() else {
                'session_id': child, 'source': source, 'entry': entry, 'workspace': workspace,
                'root': str((self.home / child).resolve()),
            }
            if data['source'] != source or data['workspace'] != workspace:
                raise ValueError('Fork workspace changed. Start a new fork request.')
            worker = self.workers.get(child)
            if data.get('status') == 'ready' or (worker and worker.is_alive()):
                return data
            if not data.get('project_path'):
                source_path = Path(workspace).resolve()
                repository = Path(git(source_path, 'rev-parse', '--show-toplevel', timeout=5)).resolve()
                data['project_path'] = str(Path(data['root']) / source_path.relative_to(repository))
            data.update(status='pending', error='')
            write_json_atomic(path, data)
            from codey.app.conversation_routes import register_fork

            register_fork(self.ctx, source, child, workspace=data['project_path'], preserve_title=True)
            worker = threading.Thread(target=self._run, args=(child, data), daemon=True, name=f'worktree-{child}')
            self.workers[child] = worker
            worker.start()
            return dict(data)

    def _run(self, child: str, data: dict[str, Any]) -> None:
        try:
            source = Path(data['workspace']).resolve()
            repository = Path(git(source, 'rev-parse', '--show-toplevel')).resolve()
            target = Path(data['root'])
            if target.parent.resolve() != self.home.resolve():
                raise ValueError('Worktree target is outside its owned directory.')
            relative = source.relative_to(repository)
            if not data.get('snapshot'):
                head = git(repository, 'rev-parse', '--verify', 'HEAD')
                # A separate index captures tracked and non-ignored untracked files,
                # including current edits, without staging or stashing the source.
                with tempfile.TemporaryDirectory(prefix='fork-index-', dir=self.home) as temporary:
                    env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
                    git(repository, 'read-tree', head, env=env)
                    git(repository, 'add', '-A', '--', '.', env=env)
                    tree = git(repository, 'write-tree', env=env)
                    if git(repository, 'rev-parse', 'HEAD') != head:
                        raise ValueError('The source branch changed during setup. Retry to capture its current state.')
                    data['snapshot'] = git(repository, '-c', 'user.name=Codey', '-c', 'user.email=codey@localhost',
                                           'commit-tree', tree, '-p', head, env=env, data=b'Codey chat fork workspace\n')
                    with self.lock:
                        write_json_atomic(self._file(child), data)
            if target.exists():
                # A retry may recover an already-created worktree, but must never
                # overwrite an unrelated directory or remove user files.
                if (not (target / '.git').is_file()
                        or Path(git(target, 'rev-parse', '--show-toplevel')).resolve() != target.resolve()
                        or git(target, 'rev-parse', 'HEAD') != data['snapshot']
                        or Path(git(target, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
                        != Path(git(repository, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()):
                    raise ValueError('The worktree destination is occupied. Its files were left unchanged.')
            else:
                git(repository, 'worktree', 'add', '--detach', str(target), str(data['snapshot']))
            workspace = target / relative
            if not workspace.is_dir():
                raise ValueError('The project directory is missing from the new worktree.')
            from codey.app.conversation_routes import register_fork

            register_fork(self.ctx, data['source'], child, workspace=str(workspace), preserve_title=True)
            data.update(status='ready', project_path=str(workspace), error='')
        except Exception as error:
            data.update(status='error', error=str(error)[:1000])
        with self.lock:
            write_json_atomic(self._file(child), data)

    def close(self) -> bool:
        deadline = time.monotonic() + 2
        for worker in list(self.workers.values()):
            worker.join(max(0, deadline - time.monotonic()))
        return not any(worker.is_alive() for worker in self.workers.values())


def service(ctx: Any) -> ForkWorkspaces:
    with ctx.lock:
        if getattr(ctx, 'fork_workspaces', None) is None:
            home = ctx.state_home
            if home is None:
                raise ValueError('A persistent state directory is required for Git worktrees.')
            ctx.fork_workspaces = ForkWorkspaces(ctx, Path(home))
        return cast(ForkWorkspaces, ctx.fork_workspaces)


def ensure_workspace_ready(ctx: Any, identity: str) -> None:
    if (ctx.state_home is not None and identity.startswith('fork_') and len(identity) == 37
            and all(c in '0123456789abcdef' for c in identity[5:])):
        status = service(ctx).status(identity)
        if status and status['status'] != 'ready':
            raise ValueError('Worktree setup is not ready. Finish or retry setup before sending a message.')
