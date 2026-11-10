"""Durable synthetic bookings: one connection and one explicit transaction per write.

SQLite BEGIN IMMEDIATE serializes writers across objects/processes. This module
has no HTTP server, customer accounts, payment, production deployment or PG proof.
"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .allocation import BookingRequest, Decision
from .scenario import build_allocator
from .sqlite_schema import STATEMENTS, VERSION


class IdempotencyConflict(ValueError):
    pass


class BookingNotFound(LookupError):
    pass


class InvalidBookingState(ValueError):
    pass


class DatabaseBusy(RuntimeError):
    """Transient lock contention; retry with the SAME request ID and body."""


class DataIntegrityError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def request_document(request):
    if not isinstance(request, BookingRequest):
        raise ValueError('reserve requires a validated BookingRequest')
    data = asdict(request)
    data['check_in'], data['check_out'] = request.check_in.isoformat(), request.check_out.isoformat()
    # These lists are unordered business constraints, not priority rankings.
    data['accepted_bed_types'] = sorted(request.accepted_bed_types)
    data['child_ages'] = sorted(request.child_ages)
    return data


def inventory_document(scenario):
    required = ('horizon_start', 'horizon_end', 'room_types', 'rooms', 'daily_resources', 'allowed_upgrades')
    result = {key: scenario[key] for key in required}
    result.update({key: scenario.get(key, default) for key, default in
                   (('daily_rates', []), ('protection', []), ('cot_age_limit', 2))})
    return json.loads(canonical(result))


class SQLiteHotel:
    def __init__(self, path, timeout_ms=5000):
        if str(path) == ':memory:':
            raise ValueError('use a dedicated file database; operations use independent connections')
        if type(timeout_ms) is not int or timeout_ms < 1:
            raise ValueError('timeout_ms must be a positive integer')
        self.path = Path(path).resolve()
        self.timeout_ms = timeout_ms

    def connect(self, read_only=False):
        if read_only:
            uri = 'file:' + quote(self.path.as_posix(), safe='/') + '?mode=ro'
            connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=self.timeout_ms / 1000)
        else:
            connection = sqlite3.connect(self.path, isolation_level=None, timeout=self.timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute(f'PRAGMA busy_timeout={self.timeout_ms}')
        if read_only:
            connection.execute('PRAGMA query_only=ON')
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if (getattr(exc, 'sqlite_errorcode', 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise DatabaseBusy('database busy; retry the same request ID and body') from None
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(self):
        connection = self.connect(read_only=True)
        try:
            connection.execute('BEGIN')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, scenario, mode='strict'):
        config = inventory_document(scenario)
        planner = build_allocator(config, mode)  # Validate before creating a file.
        document = canonical(config)
        identity = digest(canonical({'mode': mode, 'config': config}))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if version == 0 and not tables:
                # Do not use executescript here: it can implicitly commit BEGIN.
                for statement in STATEMENTS:
                    connection.execute(statement)
                connection.execute('INSERT INTO configuration VALUES(1,?,?,?)', (mode, document, identity))
                for room in planner.rooms:
                    connection.executemany('INSERT INTO room_calendar VALUES(?,?,?,?)',
                        [(room.room_id, day.isoformat(), room.room_type, int(day in room.blocked_dates)) for day in planner.days])
                connection.executemany('INSERT INTO auxiliary_inventory(night,cot_capacity,extra_capacity) VALUES(?,?,?)',
                    [(day.isoformat(), planner.capacities['cots'][day], planner.capacities['extra_beds'][day]) for day in planner.days])
                connection.execute(f'PRAGMA user_version={VERSION}')
            else:
                stored_config, stored_mode = self._configuration(connection)
                if canonical(stored_config) != document or stored_mode != mode:
                    raise ValueError('database already initialized with a different inventory or policy')
            # Reopening a recognized version must not bless a missing/corrupt
            # table or inconsistent ledger as a successful initialization.
            from .sqlite_audit import audit
            try:
                report = audit(connection, config, mode, 100)
            except (ValueError, KeyError, IndexError, TypeError, sqlite3.DatabaseError) as exc:
                raise DataIntegrityError('existing database is incomplete or malformed; no repair applied') from exc
            if not report['healthy']:
                raise DataIntegrityError('database does not reconcile; no repair applied')
            self._checkpoint('initialize_before_commit', connection)
        # WAL is optional for correctness; enables readers while a writer commits.
        # Only configure it after a recognized, successfully initialized database.
        self._checkpoint('initialize_after_commit', None)
        connection = self.connect()
        warnings = []
        try:
            journal_mode = connection.execute('PRAGMA journal_mode').fetchone()[0]
            try:
                journal_mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            except sqlite3.OperationalError as exc:
                if (getattr(exc, 'sqlite_errorcode', 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                # Schema/configuration already committed. A reader can prevent
                # this optional mode change; do not falsely report rollback.
                warnings.append('wal_not_enabled_due_to_lock; initialization_committed')
        finally:
            connection.close()
        return {'schema_version': VERSION, 'config_sha256': identity, 'mode': mode,
                'initialized': True, 'journal_mode': journal_mode, 'wal_enabled': journal_mode == 'wal',
                'warnings': warnings}

    def _configuration(self, connection):
        if connection.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            raise DataIntegrityError('database schema is uninitialized or unsupported')
        try:
            row = connection.execute('SELECT * FROM configuration WHERE singleton=1').fetchone()
        except sqlite3.DatabaseError as exc:
            raise DataIntegrityError('missing configuration') from exc
        if row is None:
            raise DataIntegrityError('missing configuration')
        config = json.loads(row['config_json'])
        if digest(canonical({'mode': row['mode'], 'config': config})) != row['config_sha256']:
            raise DataIntegrityError('configuration checksum mismatch')
        return config, row['mode']

    def _planner(self, connection, config, mode):
        planner = build_allocator(config, mode)
        active = connection.execute('''SELECT a.*, b.status, n.cots,n.extra_beds FROM active_room_nights a
            JOIN bookings b USING(request_id)
            JOIN booking_nights n ON n.request_id=a.request_id AND n.night=a.night AND n.room_id=a.room_id''').fetchall()
        sums = {day: [0, 0] for day in planner.days}
        for row in active:
            if row['status'] != 'BOOKED':
                raise DataIntegrityError('non-booked request still holds a room')
            day = date.fromisoformat(row['night'])
            planner._occupied[row['room_id'], day] = row['request_id']
            sums[day][0] += row['cots']
            sums[day][1] += row['extra_beds']
        for row in connection.execute('SELECT * FROM auxiliary_inventory'):
            day = date.fromisoformat(row['night'])
            if sums[day] != [row['cots_used'], row['extras_used']]:
                raise DataIntegrityError('auxiliary counters do not match active reservations; reconcile first')
            planner._used['cots'][day] = row['cots_used']
            planner._used['extra_beds'][day] = row['extras_used']
        return planner

    @staticmethod
    def _public(row):
        return {'request_id': row['request_id'], 'status': row['status'],
                'original_decision': json.loads(row['decision_json']),
                'created_at': row['created_at'], 'cancelled_at': row['cancelled_at']}

    @staticmethod
    def _fetch(connection, request_id):
        row = connection.execute('SELECT * FROM bookings WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            raise BookingNotFound(request_id)
        return row

    def reserve(self, request):
        document = canonical(request_document(request))
        identity = digest(document)
        with self.transaction() as connection:
            config, mode = self._configuration(connection)
            old = connection.execute('SELECT * FROM bookings WHERE request_id=?', (request.request_id,)).fetchone()
            if old is not None:
                if old['request_sha256'] != identity or old['request_json'] != document:
                    raise IdempotencyConflict('request ID already used for different content')
                return self._public(old)
            planner = self._planner(connection, config, mode)
            decision = planner.allocate(request)
            status = 'BOOKED' if decision.accepted else 'REJECTED'
            connection.execute('INSERT INTO bookings(request_id,request_json,request_sha256,decision_json,status) VALUES(?,?,?,?,?)',
                (request.request_id, document, identity, canonical(decision.to_dict()), status))
            self._checkpoint('reserve_after_record', connection)
            if decision.accepted:
                for night in decision.nights:
                    cursor = connection.execute('''UPDATE auxiliary_inventory SET cots_used=cots_used+?,extras_used=extras_used+?
                        WHERE night=? AND cots_used+?<=cot_capacity AND extras_used+?<=extra_capacity''',
                        (decision.cots, decision.extra_beds, night, decision.cots, decision.extra_beds))
                    if cursor.rowcount != 1:
                        raise DataIntegrityError('auxiliary inventory changed or is incomplete')
                    connection.execute('INSERT INTO booking_nights VALUES(?,?,?,?,?)',
                        (request.request_id, decision.room_id, night, decision.cots, decision.extra_beds))
                    connection.execute('INSERT INTO active_room_nights VALUES(?,?,?)', (decision.room_id, night, request.request_id))
                    self._checkpoint('reserve_after_night', connection)
            connection.execute('INSERT INTO reservation_events(request_id,event_type) VALUES(?,?)', (request.request_id, status))
            result = self._public(self._fetch(connection, request.request_id))
            self._checkpoint('reserve_before_commit', connection)
        self._checkpoint('reserve_after_commit', None)
        return result

    def cancel(self, request_id):
        with self.transaction() as connection:
            self._configuration(connection)
            row = self._fetch(connection, request_id)
            if row['status'] == 'CANCELLED':
                return self._public(row)
            if row['status'] != 'BOOKED':
                raise InvalidBookingState('only a BOOKED reservation can be cancelled')
            decision = json.loads(row['decision_json'])
            history = connection.execute('SELECT * FROM booking_nights WHERE request_id=? ORDER BY night', (request_id,)).fetchall()
            expected = {(decision['room_id'], night, decision['cots'], decision['extra_beds']) for night in decision['nights']}
            actual = {(n['room_id'], n['night'], n['cots'], n['extra_beds']) for n in history}
            active = {(n['room_id'], n['night']) for n in connection.execute('SELECT * FROM active_room_nights WHERE request_id=?', (request_id,))}
            if actual != expected or active != {(rid, night) for rid, night, _, _ in expected}:
                raise DataIntegrityError('reservation nights are inconsistent; refusing an unsafe release')
            for night in history:
                connection.execute('DELETE FROM active_room_nights WHERE request_id=? AND night=?', (request_id, night['night']))
                cursor = connection.execute('''UPDATE auxiliary_inventory SET cots_used=cots_used-?,extras_used=extras_used-?
                    WHERE night=? AND cots_used>=? AND extras_used>=?''',
                    (night['cots'], night['extra_beds'], night['night'], night['cots'], night['extra_beds']))
                if cursor.rowcount != 1:
                    raise DataIntegrityError('auxiliary release would underflow or references a missing date')
                self._checkpoint('cancel_after_release', connection)
            connection.execute("UPDATE bookings SET status='CANCELLED',cancelled_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE request_id=?", (request_id,))
            connection.execute("INSERT INTO reservation_events(request_id,event_type) VALUES(?,'CANCELLED')", (request_id,))
            self._checkpoint('cancel_before_commit', connection)
            result = self._public(self._fetch(connection, request_id))
        self._checkpoint('cancel_after_commit', None)
        return result

    def get_booking(self, request_id):
        with self.read_snapshot() as connection:
            self._configuration(connection)
            return self._public(self._fetch(connection, request_id))

    def snapshot(self):
        with self.read_snapshot() as connection:
            config, mode = self._configuration(connection)
            return self._planner(connection, config, mode).snapshot()

    def reconcile(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be 1 to 1000')
        from .sqlite_audit import audit
        try:
            with self.read_snapshot() as connection:
                config, mode = self._configuration(connection)
                self._checkpoint('reconcile_snapshot', connection)
                return audit(connection, config, mode, limit)
        except sqlite3.OperationalError as exc:
            if (getattr(exc, 'sqlite_errorcode', 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise DatabaseBusy('reconciliation temporarily blocked; retry without modifying data') from None
            return {'healthy': False, 'verification_complete': False, 'issue_count': 1,
                    'issues': ['reconciliation_unavailable:missing_or_malformed_storage'], 'truncated': False}
        except (DataIntegrityError, ValueError, KeyError, IndexError, TypeError, sqlite3.DatabaseError):
            return {'healthy': False, 'verification_complete': False, 'issue_count': 1,
                    'issues': ['reconciliation_unavailable:invalid_schema_configuration_or_records'], 'truncated': False}

    def _checkpoint(self, stage, connection):
        """No-op injection point used only by failure/concurrency tests."""
