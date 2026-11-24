"""Dispatch only the accepted turn's decision provider, with a durable send boundary."""

import asyncio
import json
from weakref import WeakValueDictionary

from . import community, community_jev
from .archive import encoded, fingerprint
from .models import RuntimeErrorBase, identifier
from .scene_policy import read, save


TURN = 'community.decision.turn.v1.'
UNSENT = frozenset(('system_proxy_required', 'system_proxy_configuration_unavailable',
                    'provider_network_error', 'jev_credential_unavailable'))


def freeze(db, config, tid, pid, sid):
    if not community.enabled():
        return
    state = {'requested': 'flash', 'provider': 'flash', 'pending': None, 'jev_sent': False,
             'unknown': False, 'binding': None, 'fallback': None, 'last': None}
    from .community_lookup import automatic_consent
    if consent := automatic_consent(db, config, pid, sid):
        state['search_consent'] = consent
    try:
        value = community_jev.settings(config)
    except RuntimeErrorBase:
        value = community_jev.DEFAULT
    if value['enabled']:
        grant = community_jev.session_grant(db, config, pid, sid, value)
        if grant['authorized']:
            state.update(requested='jev', provider='jev', binding=grant['recipient'], name=value['name'])
        else:
            state['unapproved'] = True
    save(db, TURN + tid, state)


def turn_view(db, tid):
    state = read(db, TURN + tid)
    if state is None:
        return None
    return {'selected': state['requested'], 'actual': (state['last'] or {}).get('provider'),
            'fallback': state['fallback'], 'unapproved': state.get('unapproved', False),
            'outcome': 'unknown' if state['pending'] or state['unknown'] else (state['last'] or {}).get('outcome')}


def cross_memory_allowed(db, config, turn, sessions):
    if len(sessions) < 2 or not community.enabled():
        return True
    plan = read(db, TURN + turn['id'])
    if not plan or plan['requested'] != 'jev':
        return True
    try:
        saved = community_jev.settings(config)
        return all((grant := community_jev.session_grant(db, config, turn['persona_id'], sid, saved))['authorized']
                   and grant['recipient'] == plan['binding'] for sid in sessions)
    except RuntimeErrorBase:
        return False


class TurnDecisions:
    def __init__(self, store, config, *, transport=None):
        from .scene_jev import JevTransport
        self.store, self.config = store, config
        self.transport = transport or JevTransport
        self.locks = WeakValueDictionary()
        self.connections = {}

    async def close(self):
        for connection, _ in self.connections.values():
            await connection.close()
        self.connections.clear()

    async def complete(self, tid, node, wire, flash, *, check_fallback=None, **callbacks):
        if not tid or not community.enabled():
            return await flash(wire, **callbacks)
        lock = self.locks.setdefault(tid, asyncio.Lock())
        async with lock:
            return await self._complete(tid, node, wire, flash, check_fallback, **callbacks)

    async def _complete(self, tid, node, wire, flash, check_fallback, *, on_dispatch, on_receipt, diagnostics):
        from .chat import ModelError
        with self.store.connection() as db:
            state = read(db, TURN + tid)
        if state is None:
            return await flash(wire, on_dispatch=on_dispatch, on_receipt=on_receipt, diagnostics=diagnostics)
        if state['provider'] == 'flash':
            return await self._flash(tid, node, wire, flash, on_dispatch, on_receipt, diagnostics)

        aid, model, binding = self._begin(tid)
        body = {**wire, 'model': model['name']}
        request_hash = fingerprint(body)
        if len(encoded(body)) > model['max_input_chars']:
            self._finish(tid, aid, node, model['name'], 'input_limit', False)
            raise ModelError('jev_input_budget_exceeded', diagnostics={'decision_provider': 'jev',
                             'request_headers_sent': False, 'effect_unknown': False})
        sent = False
        detail = {}

        def dispatched(at):
            nonlocal sent
            sent = True
            on_dispatch(at)

        connection = await self._connection(binding, model)
        try:
            try:
                status, raw = await connection.complete(body, on_dispatch=dispatched, on_receipt=on_receipt,
                                                        diagnostics=detail.update)
            except ModelError as error:
                observed = error.diagnostics or {}
                unsent = (not sent and (observed.get('request_headers_sent') is False
                                        or str(error) == 'jev_credential_unavailable'))
                known = unsent or observed.get('effect_unknown') is False
                use_flash = unsent and str(error) in UNSENT and not state['jev_sent']
                if use_flash and check_fallback is not None:
                    try:
                        check_fallback()
                    except RuntimeErrorBase:
                        use_flash = False
                outcome = 'unsent' if unsent else 'invalid' if known else 'unknown'
                fallback = self._finish(tid, aid, node, model['name'], outcome, not unsent,
                                        reason=str(error) if use_flash else None)
                if fallback:
                    return await self._flash(tid, node, wire, flash, on_dispatch, on_receipt, diagnostics,
                                             reason=str(error))
                error.diagnostics = {**detail, **observed, 'decision_provider': 'jev',
                                     'request_headers_sent': not unsent, 'effect_unknown': not known,
                                     'decision_outcome': outcome}
                diagnostics(error.diagnostics)
                raise
            except BaseException:
                self._finish(tid, aid, node, model['name'], 'unknown', True)
                raise
            answer = community_jev.choice_result(raw, body) if status == 200 else None
            outcome = 'received' if answer else 'invalid' if status == 200 else 'http_error'
            self._finish(tid, aid, node, model['name'], outcome, True, status=status)
            receipt = fingerprint(raw)
            diagnostics({**detail, 'decision_provider': 'jev', 'decision_outcome': outcome,
                         'provider_receipt_sha256': receipt, 'request_sha256': request_hash,
                         'request_headers_sent': True,
                         'http_status': status, 'effect_unknown': False})
            if answer is None:
                from .knowledge_web_decision import usage
                try:
                    measured = usage(json.loads(raw))
                except (ValueError, RecursionError):
                    measured = None
                answer = {'answers': {}, 'model': model['name'], 'decision_source': 'jev', 'usage': measured}
            return status, encoded(answer
                                   | {'provider_receipt_sha256': receipt})
        finally:
            self.connections[binding][1] -= 1

    async def _connection(self, binding, model):
        if binding not in self.connections:
            for key, (connection, active) in list(self.connections.items()):
                if not active:
                    del self.connections[key]
                    await connection.close()
            self.connections[binding] = [self.transport(model), 0]
        value = self.connections[binding]
        value[1] += 1
        return value[0]

    def _begin(self, tid):
        from .chat import ModelError
        with self.store.connection(write=True) as db:
            plan = read(db, TURN + tid)
            if plan['pending'] or plan['unknown']:
                raise ModelError('jev_transport_unconfirmed',
                                 diagnostics={'decision_provider': 'jev', 'effect_unknown': True})
            turn = db.execute('SELECT persona_id,session_id FROM turns WHERE id=? AND cancelled_at IS NULL',
                              (tid,)).fetchone()
            try:
                saved = community_jev.settings(self.config)
            except RuntimeErrorBase as error:
                raise ModelError(str(error), diagnostics={'decision_provider': 'jev',
                                 'request_headers_sent': False, 'effect_unknown': False}) from None
            if turn:
                grant = community_jev.session_grant(db, self.config, turn['persona_id'], turn['session_id'], saved)
                if grant['authorized'] and grant['recipient'] == plan['binding']:
                    model = community.decision_model(self.config.load()) | {
                        key: saved[key] for key in ('base_url', 'name', 'encrypted_key')}
                    aid = plan['pending'] = identifier()
                    save(db, TURN + tid, plan)
                    return aid, model, plan['binding']
        raise ModelError('community_jev_grant_changed', diagnostics={'decision_provider': 'jev',
                         'request_headers_sent': False, 'effect_unknown': False})

    def _finish(self, tid, aid, node, model, outcome, sent, *, reason=None, status=None):
        with self.store.connection(write=True) as db:
            state = read(db, TURN + tid)
            if state['pending'] != aid:
                raise RuntimeErrorBase('community_jev_receipt_changed')
            fallback = not state['jev_sent'] and not sent and reason in UNSENT
            state.update(pending=None, jev_sent=state['jev_sent'] or sent, unknown=outcome == 'unknown',
                         last={'provider': 'jev', 'node': node, 'model': model,
                               'outcome': outcome, 'status': status})
            if fallback:
                state.update(provider='flash', fallback=reason)
            save(db, TURN + tid, state)
        return fallback

    async def _flash(self, tid, node, body, flash, on_dispatch, on_receipt, diagnostics, reason=None):
        detail = {}
        outcome = 'unknown'
        try:
            status, raw = await flash(body, on_dispatch=on_dispatch, on_receipt=on_receipt, diagnostics=detail.update)
            outcome = 'received' if status == 200 else 'http_error'
        except BaseException as error:
            observed = getattr(error, 'diagnostics', None) or {}
            outcome = 'unsent' if observed.get('request_headers_sent') is False else 'unknown'
            raise
        finally:
            with self.store.connection(write=True) as db:
                state = read(db, TURN + tid)
                if state:
                    state['last'] = {'provider': 'flash', 'node': node, 'model': body['model'], 'outcome': outcome}
                    save(db, TURN + tid, state)
            diagnostics({**detail, 'decision_provider': 'flash', 'decision_outcome': outcome,
                         **({'fallback_reason': reason} if reason else {})})
        return status, raw
