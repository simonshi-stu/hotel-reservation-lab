"""Read-only recovery of a synthetic operation from its original round.

A result describes one consistent SQLite snapshot, not network delivery or
causality. It never retries, reserves, cancels, initializes, or repairs a DB.
The surrounding demo supports one managing server process, not hostile local
filesystem writers, production authentication, or cross-host coordination.
"""
import re
import sqlite3

from hotel_sim.scenario import parse_requests
from hotel_sim.sqlite_audit import audit
from hotel_sim.sqlite_store import (SQLiteHotel, IdempotencyConflict, DatabaseBusy,
                                    DataIntegrityError, canonical, digest, request_document, inventory_document)


class RecoveryUnavailable(RuntimeError):
    """The original result must remain unconfirmed."""


def lookup_operation(workspace, body, modes):
    kind, payload = body.get('kind'), body.get('payload')
    if kind not in ('reserve', 'cancel') or not isinstance(payload, dict):
        raise ValueError('lookup requires the original reserve or cancel payload')
    generation, mode = payload.get('generation'), payload.get('mode')
    if not isinstance(generation, str) or not re.fullmatch('[0-9a-f]{32}', generation):
        raise ValueError('invalid original round')
    if mode not in modes:
        raise ValueError('invalid original mode')
    document = None
    if kind == 'reserve':
        raw = payload.get('request')
        if not isinstance(raw, dict):
            raise ValueError('original request required')
        if not isinstance(raw.get('child_ages', []), list) or len(raw.get('child_ages', [])) > 10:
            raise ValueError('too many child ages')
        request = parse_requests({'requests': [raw]})[0]
        key = request.request_id
        document = canonical(request_document(request))
    else:
        key = payload.get('request_id')
    if not isinstance(key, str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}', key):
        raise ValueError('invalid original request ID')

    # The generation is an opaque identifier, never an arbitrary path. Missing
    # archives must not silently create a new DB or turn into NOT_FOUND.
    folder = workspace.path / generation
    path = folder / (mode + '.db')
    if folder.is_symlink() or path.is_symlink() or not folder.is_dir() or not path.is_file():
        raise RecoveryUnavailable('original round database is missing or unsafe')
    if any(path.with_name(path.name + suffix).is_symlink() for suffix in ('-wal', '-shm', '-journal')):
        raise RecoveryUnavailable('unsafe database sidecar')
    store = SQLiteHotel(path, timeout_ms=2000)
    try:
        with store.read_snapshot() as connection:
            config, stored_mode = store._configuration(connection)
            if stored_mode != mode or canonical(config) != canonical(inventory_document(workspace.scenario)):
                raise RecoveryUnavailable('original database does not match this demo')
            report = audit(connection, config, stored_mode, 100)
            if report.get('healthy') is not True or report.get('verification_complete') is not True:
                raise RecoveryUnavailable('original database did not pass complete reconciliation')
            row = connection.execute('SELECT * FROM bookings WHERE request_id=?', (key,)).fetchone()
            if row is not None and document is not None:
                if row['request_json'] != document or row['request_sha256'] != digest(document):
                    raise IdempotencyConflict('original request differs from recorded request')
            record = store._public(row) if row is not None else None
            status = record['status'] if record else 'NOT_FOUND'
            if record is None:
                outcome = 'NOT_RECORDED'
            elif kind == 'reserve':
                outcome = 'RECORDED'
            elif status == 'CANCELLED':
                outcome = 'CANCELLED'
            else:
                outcome = 'NOT_CANCELLED'
            observed_at = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    except sqlite3.OperationalError as exc:
        if (getattr(exc, 'sqlite_errorcode', 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise DatabaseBusy('read snapshot busy; retry this lookup') from None
        raise RecoveryUnavailable('original database could not be verified') from exc
    except IdempotencyConflict:
        raise
    except (DataIntegrityError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise RecoveryUnavailable('original database could not be verified') from exc
    except sqlite3.DatabaseError as exc:
        raise RecoveryUnavailable('original database could not be verified') from exc
    return {'generation': generation, 'current_generation': workspace.generation, 'mode': mode,
            'request_id': key, 'operation': kind, 'status': status, 'outcome': outcome,
            'record': record, 'audit': report, 'read_only': True, 'snapshot_only': True,
            'observed_at': observed_at, 'archived': generation != workspace.generation}
