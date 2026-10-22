"""Process-lifetime lock shared by library repair commands."""
from contextlib import contextmanager
import os
import json
from datetime import datetime,timezone


def tag_state(root):
    path=root/'.navidrome-scripts-repair.lock'
    if not path.exists():
        return {}
    with path.open('rb') as stream:
        stream.seek(1);payload=stream.read()
    if not payload:
        return {}
    try:
        state=json.loads(payload)
        if state.get('schema')!=1 or state.get('status') not in ('writing','complete'):
            raise ValueError('invalid state')
        return state
    except (ValueError,AttributeError):
        raise ValueError('repair state is incomplete; inspect the last tag journal before renaming') from None


def _save_tag_state(root,state):
    # Call only while library_lock(root) is held. Leave byte zero and its inode
    # intact: Windows locks that byte and POSIX locks the same file.
    with (root/'.navidrome-scripts-repair.lock').open('r+b') as stream:
        stream.seek(1);stream.write(json.dumps(state).encode('utf8'));stream.truncate()
        stream.flush();os.fsync(stream.fileno())


def begin_tag_write(root,journal,operation):
    previous=tag_state(root);identity=str(journal.resolve())
    if previous.get('status')=='writing' and previous.get('journal')!=identity:
        raise ValueError('finish or roll back the unfinished tag journal first: '+str(previous.get('journal')))
    _save_tag_state(root,dict(schema=1,status='writing',journal=identity,operation=operation,
        started=datetime.now(timezone.utc).isoformat()))


def finish_tag_write(root,journal):
    state=tag_state(root)
    if state.get('status')!='writing' or state.get('journal')!=str(journal.resolve()):
        return
    state.update(status='complete',finished=datetime.now(timezone.utc).isoformat())
    _save_tag_state(root,state)


def require_tag_scan(root,scan):
    state=tag_state(root)
    if not state:
        return state
    if state['status']=='writing':
        raise ValueError('finish or roll back the interrupted tag repair before renaming: '+state['journal'])
    try:
        completed=datetime.fromisoformat(state['finished'].replace('Z','+00:00'))
        scanned=datetime.fromisoformat(str(scan.get('lastScan') or '').replace('Z','+00:00'))
        if completed.tzinfo is None or scanned.tzinfo is None:
            raise ValueError('timestamps need time zones')
    except (ValueError,KeyError):
        raise ValueError('cannot verify a full Navidrome scan after the last tag repair') from None
    if scan.get('scanType')!='full' or scanned<=completed:
        raise ValueError('complete a new full Navidrome scan after the last tag repair before renaming')
    return state


@contextmanager
def library_lock(root):
    # Keep the lock inode: deleting it could let a third process bypass a held lock.
    with (root / '.navidrome-scripts-repair.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
