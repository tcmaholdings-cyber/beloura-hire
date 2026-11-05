"""Accept the Beloura gateway identity inside any Python app. Standard library only; copy next to the app.

The gateway (nginx on the droplet) forwards every signed-in request with Remote-User / Remote-Groups /
Remote-Name / Remote-Email and X-Gateway-Token. Trust those headers only when the token matches.

    from beloura_sso import gateway_identity, role_for
    who = gateway_identity(request.headers, os.environ['BELOURA_GATEWAY_TOKEN'])
    if who:                       # {'user': 'tyler', 'groups': [...], 'name': 'Tyler', 'email': '...'}
        role = role_for(who['groups'], 'outreach')   # 'admin' | 'setter' | 'leadgen' | None
        ...open the app session for who['user']...

Flask:   @app.before_request → if not session.get('user') and (who := gateway_identity(request.headers, TOKEN)): session['user'] = who['user']
FastAPI: a middleware or a dependency that reads request.headers the same way and sets the app's session cookie.
Django:  a middleware before AuthenticationMiddleware's consumers that calls login() with the matched user.

Sign-out: clear the app session, then redirect to https://id.beloura.ai/logout?rd=https://workspace.beloura.ai
"""
from __future__ import annotations

import hmac

ROLE_ORDER = ('admin', 'owner', 'manager', 'operator', 'staff', 'member', 'setter', 'leadgen', 'viewer')


def _get(headers, name: str) -> str:
    try:
        v = headers.get(name)
    except Exception:
        v = None
    if v is None:
        try:
            v = headers.get(name.lower())
        except Exception:
            v = None
    if v is None:
        # WSGI environ style: HTTP_X_GATEWAY_TOKEN
        try:
            v = headers.get('HTTP_' + name.upper().replace('-', '_'))
        except Exception:
            v = None
    return (v or '').strip()


def gateway_identity(headers, token: str) -> dict | None:
    """The person the gateway identified, or None when the request did not come through the gateway."""
    if not token:
        return None
    got = _get(headers, 'X-Gateway-Token')
    if not got or not hmac.compare_digest(got, token):
        return None
    user = _get(headers, 'Remote-User')
    if not user:
        return None
    groups = [g.strip() for g in _get(headers, 'Remote-Groups').split(',') if g.strip()]
    return {'user': user, 'groups': groups, 'name': _get(headers, 'Remote-Name') or user, 'email': _get(headers, 'Remote-Email')}


def role_for(groups: list[str], app_prefix: str) -> str | None:
    """'outreach-setter' -> 'setter' for app_prefix 'outreach'; the strongest role if several; None if no group."""
    roles = [g[len(app_prefix) + 1:] for g in groups if g.startswith(app_prefix + '-')]
    if not roles:
        return None
    return sorted(roles, key=lambda r: ROLE_ORDER.index(r) if r in ROLE_ORDER else len(ROLE_ORDER))[0]
