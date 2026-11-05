"""OnlineJobs.ph from the command line. Everything but `login` uses the cached session in data/olj_session.json; nothing writes to OLJ unless OLJ_SEND=1.

    .venv/bin/python tools/olj_cli.py login [--email X] [--force]      a local keychain item -> session (the only command that reads the keychain)
    .venv/bin/python tools/olj_cli.py login --cookies cookies.txt      browser-exported Netscape cookie file -> session (the captcha fallback)
    .venv/bin/python tools/olj_cli.py login --token TOK [--cookies f]  an already-captured Bearer token (+ optional cookie file) -> session, no network
    .venv/bin/python tools/olj_cli.py whoami                           name, email, unread count
    .venv/bin/python tools/olj_cli.py inbox [--unread] [--limit N] [--json]
    .venv/bin/python tools/olj_cli.py threads <job_hash> [--limit N] [--json]
    .venv/bin/python tools/olj_cli.py thread <thread_hash> [--json]
    .venv/bin/python tools/olj_cli.py jobs [--json] | job <id> [--json]
    .venv/bin/python tools/olj_cli.py sync [--apply]                   dry-run by default: prints what would be created
    .venv/bin/python tools/olj_cli.py send <thread_hash> <text>        refused unless OLJ_SEND=1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)
os.environ.setdefault("HIRE_NO_WORKER", "1")
if __name__ == "__main__":
    import dotenv_lite
    dotenv_lite.load(os.path.join(HERE, ".env"))

import db  # noqa: E402
import olj  # noqa: E402


def keychain_password() -> str:
    """Read the login password from the keychain. The value is passed straight to olj.login and never printed or stored."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", olj.KEYCHAIN_ITEM, "-w"], capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"keychain read failed: {type(e).__name__}") from None
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit(f"keychain item {olj.KEYCHAIN_ITEM} not readable (exit {r.returncode}); add it with: security add-generic-password -s {olj.KEYCHAIN_ITEM} -a <email> -w")
    return r.stdout.strip()


def keychain_account() -> str:
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", olj.KEYCHAIN_ITEM], capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in r.stdout.splitlines():
        if '"acct"' in line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def make_api() -> olj.Api:
    return olj.Api()


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=1, default=str))
    else:
        print(obj)


def _thread_line(rec: dict) -> str:
    return (f"{rec['thread_hash']:<14} {rec['name'][:24]:<24} {rec['email'][:30]:<30} {rec['job_title'][:28]:<28} "
            f"{rec['created'][:19]:<19} {'unread' if rec['unread'] else '      '} {rec['message'][:60].replace(chr(10), ' ')}")


def cmd_login(args) -> int:
    email_addr = args.email or os.environ.get("OLJ_EMAIL") or keychain_account() or olj.DEFAULT_EMAIL
    items = None
    if args.cookies:
        with open(args.cookies, encoding="utf-8") as f:
            items = olj.parse_netscape_cookies(f.read())
        if not items:
            print("no cookies parsed from that file (Netscape cookies.txt format expected)")
            return 2
    try:
        if args.token:
            token = args.token.strip()
            if token in ("-", "@stdin"):
                token = sys.stdin.readline().strip()
            sess = olj.seed_session(token, cookies=items or [], email_addr=email_addr)
            print(f"session seeded from the given token ({'with' if items else 'no'} cookies); no network call was made. Validate with `whoami`.")
        elif args.cookies:
            sess = olj.import_cookies(items, email_addr=email_addr)
        else:
            sess = olj.login(email_addr, keychain_password(), force_captcha=args.force)
    except olj.CaptchaRequired as e:
        print(f"captcha: {e}")
        return 3
    except olj.OljError as e:
        print(f"login failed: {e}")
        return 1
    u = sess.get("user") or {}
    print(f"session saved to {olj.session_path()} (0600); account {u.get('name') or '?'} <{u.get('email') or email_addr}>; token present; fetched {sess['fetched_at']}")
    return 0


def _api_or_exit():
    try:
        return make_api()
    except olj.SessionExpired as e:
        print(str(e))
        raise SystemExit(2)


def cmd_whoami(args) -> int:
    api = _api_or_exit()
    me = api.me()
    d = me.get("data", me) if isinstance(me, dict) else {}
    unread = api.unread_count()
    n = olj.pick(unread.get("data", unread) if isinstance(unread, dict) else {}, "count", "unread", "total", default="?")
    print(f"{olj.pick(d, 'name', 'full_name', 'business_name', default='?')} <{olj.pick(d, 'email', default='?')}> · unread {n}")
    if args.json:
        _print({"me": me, "unread": unread}, True)
    return 0


def cmd_inbox(args) -> int:
    api = _api_or_exit()
    resp = api.inbox(page=1, per_page=args.limit, unread_only=args.unread)
    if args.json:
        _print(resp, True)
        return 0
    items = olj.items_of(resp)
    for it in items:
        print(_thread_line(olj.map_thread(it)))
    meta = olj.meta_of(resp)
    print(f"-- {len(items)} threads shown; page {meta.get('current_page', 1)} of {meta.get('last_page', '?')}, total {meta.get('total', '?')}")
    return 0


def cmd_threads(args) -> int:
    api = _api_or_exit()
    resp = api.job_threads(args.job_hash, page=1, per_page=args.limit, include_archive=args.archive)
    if args.json:
        _print(resp, True)
        return 0
    for it in olj.items_of(resp):
        print(_thread_line(olj.map_thread(it)))
    meta = olj.meta_of(resp)
    print(f"-- page {meta.get('current_page', 1)} of {meta.get('last_page', '?')}, total {meta.get('total', '?')}")
    return 0


def cmd_thread(args) -> int:
    api = _api_or_exit()
    resp = api.thread(args.thread_hash, page=1, per_page=args.limit)
    if args.json:
        _print(resp, True)
        return 0
    items = olj.items_of(resp)
    for it in reversed(items):
        rec = olj.map_thread(it)
        who = {"in": rec["name"] or "applicant", "out": "us"}.get(rec["direction"] or "", "?")
        print(f"[{rec['created'][:19]}] {who}: {rec['message']}")
    if items:
        rec = olj.map_thread(items[0])
        print(f"-- {rec['name']} <{rec['email']}> {rec['mobile']} {rec['viber']} | {rec['headline']} | rate {rec['rate']} | AP {rec['ap_score']} | {rec['profile_url']}")
    return 0


def cmd_jobs(args) -> int:
    api = _api_or_exit()
    resp = api.my_jobs()
    if args.json:
        _print(resp, True)
        return 0
    for j in olj.items_of(resp):
        r = olj.job_record(j)
        print(f"{r['id']:<10} {r['hash_id']:<14} {'open' if r['is_open'] else 'closed':<6} {r['status']:<10} {r['title']}")
    return 0


def cmd_job(args) -> int:
    api = _api_or_exit()
    _print(api.job(args.job_id), True)
    return 0


def cmd_sync(args) -> int:
    api = _api_or_exit()
    db.init()
    with db.db() as con:
        out = olj.sync_inbox(con, api, dry=not args.apply, unread_only=not args.all, enrich=not args.no_enrich, jobs=not args.no_jobs)
    for p in out["plan"]:
        print(f"{p['status']:<13} {p.get('name') or '':<24} {p.get('email') or '':<30} {p.get('job_title') or '':<28} {p['thread_hash']}" + (f"  -> applicant #{p['applicant_id']}" if p.get("applicant_id") else ""))
    totals = {k: v for k, v in out.items() if k != "plan"}
    print(("DRY RUN, nothing written. " if out["dry"] else "") + json.dumps(totals))
    return 0


def cmd_send(args) -> int:
    if not olj.send_enabled():
        print("refused: OLJ_SEND is not 1. Nothing was sent. Set OLJ_SEND=1 in the environment to allow posting to OnlineJobs.ph.")
        return 2
    api = _api_or_exit()
    _print(api.add_message(args.thread_hash, args.text), True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="olj_cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("login"); s.add_argument("--email"); s.add_argument("--cookies"); s.add_argument("--token", help="an already-captured Bearer token; '-' reads it from stdin"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_login)
    s = sub.add_parser("whoami"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_whoami)
    s = sub.add_parser("inbox"); s.add_argument("--unread", action="store_true"); s.add_argument("--limit", type=int, default=20); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_inbox)
    s = sub.add_parser("threads"); s.add_argument("job_hash"); s.add_argument("--limit", type=int, default=20); s.add_argument("--archive", action="store_true"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_threads)
    s = sub.add_parser("thread"); s.add_argument("thread_hash"); s.add_argument("--limit", type=int, default=50); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_thread)
    s = sub.add_parser("jobs"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_jobs)
    s = sub.add_parser("job"); s.add_argument("job_id"); s.set_defaults(fn=cmd_job)
    s = sub.add_parser("sync"); s.add_argument("--apply", action="store_true"); s.add_argument("--all", action="store_true", help="read threads too, not only unread")
    s.add_argument("--no-enrich", action="store_true"); s.add_argument("--no-jobs", action="store_true"); s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("send"); s.add_argument("thread_hash"); s.add_argument("text"); s.set_defaults(fn=cmd_send)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except olj.ReadOnly as e:
        print(f"refused: {e}")
        return 2
    except olj.OljError as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
