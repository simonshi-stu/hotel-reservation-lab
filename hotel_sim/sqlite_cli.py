"""Explicit-file CLI for the synthetic SQLite candidate. No default live database."""
import argparse
import json
from pathlib import Path
import sqlite3

from .scenario import load_scenario, parse_requests
from .sqlite_store import SQLiteHotel, DatabaseBusy, DataIntegrityError


def main():
    parser=argparse.ArgumentParser(description='Synthetic hotel SQLite candidate; not a production booking service')
    parser.add_argument('--db',type=Path,required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init');init.add_argument('--scenario',type=Path,required=True)
    init.add_argument('--policy',choices=('strict','upgrade','protected_upgrade'),default='strict')
    reserve=commands.add_parser('reserve');reserve.add_argument('--request',type=Path,required=True)
    for name in ('get','cancel'):
        command=commands.add_parser(name);command.add_argument('request_id')
    commands.add_parser('reconcile')
    commands.add_parser('snapshot')
    args=parser.parse_args();store=SQLiteHotel(args.db)
    try:
        if args.command=='init':data,_=load_scenario(args.scenario);result=store.initialize(data,args.policy)
        elif args.command=='reserve':
            body=json.loads(args.request.read_text(encoding='utf-8'))
            result=store.reserve(parse_requests({'requests':[body]})[0])
        elif args.command=='get':result=store.get_booking(args.request_id)
        elif args.command=='cancel':result=store.cancel(args.request_id)
        elif args.command=='snapshot':result=store.snapshot()
        else:result=store.reconcile()
        print(json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True))
        return 2 if args.command=='reconcile' and not result['healthy'] else 0
    except DatabaseBusy:
        print(json.dumps({'error':'DATABASE_BUSY','retry':'use the same request ID and body'}));return 3
    except (ValueError,LookupError,DataIntegrityError) as exc:
        print(json.dumps({'error':type(exc).__name__,'message':str(exc)},ensure_ascii=False));return 2
    except sqlite3.DatabaseError:
        print(json.dumps({'error':'DATABASE_ERROR','message':'database operation failed; no success is claimed'}));return 2


if __name__=='__main__':raise SystemExit(main())
