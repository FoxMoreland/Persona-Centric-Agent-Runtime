"""Session consent receipts. All public responses omit credential bindings."""
import hashlib
import hmac
import json
from urllib.parse import urlsplit

from .archive import encoded, fingerprint, scope
from .config import atomic_json
from .knowledge_routing import configured_model
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp
from .security import protect, unprotect
from .sessions import owned
from . import community_lookup

PROTOCOL = 'desktop-session-full-flow-v1'
PUBLIC_SCOPE = 'desktop-public-lookup-scope-v1'
PUBLIC_ATTRIBUTE_SCOPE = 'desktop-public-lookup-scope-v2'
PUBLIC_QUESTION_SCOPE = 'desktop-public-lookup-scope-v3'


def account(config, key):
    return hmac.new(config.token().encode(), key.encode(), hashlib.sha256).hexdigest() if key else None


def destination(config, model):
    secret = model.get('encrypted_key')
    return {'endpoint': model.get('base_url', '').rstrip('/'),
            'account': account(config, unprotect(secret)) if secret else None}


def configured_models(config, cfg):
    background = cfg['model'] if cfg['organization']['reuse_chat'] else cfg['organization']['model']
    models = {'main': cfg['model'], 'organization': background}
    if cfg.get('lookup_target_model'):
        models['lookup_target'] = cfg['lookup_target_model']
    if cfg.get('private_research_account')=='native_web':
        models['native_web']=cfg['native_web_model']
    if cfg['character_selector']['enabled']:
        models['selector'] = cfg['character_selector']
    try:
        models['jev'] = configured_model(config.paths)
    except RuntimeErrorBase as exc:
        if str(exc) != 'jev_configuration_required':
            raise
    return {role:model for role,model in models.items() if model.get('base_url')}


def _search_destinations(result, research_account):
    research = result.get(research_account)
    if research and urlsplit(research['endpoint']).hostname == 'api.deepseek.com':
        result['search_fetch'] = dict(research, endpoint='https://api.deepseek.com/v1/tools')
    else:
        result.pop('search_fetch', None)


def recipients(config, cfg):
    result = {role: destination(config, model) for role,model in configured_models(config,cfg).items()}
    if 'lookup_target' in result:
        result['lookup_target']['model'] = cfg['lookup_target_model']['name']
    _search_destinations(result, cfg.get('private_research_account','main'))
    return result


def _display(recipients):
    return {role: value['endpoint'] for role, value in recipients.items()}


def writer_consent(db, config, cfg, pid, sid, accepted=None):
    from . import main_reply
    owned(db,sid,scope(pid,'desktop'))
    path = _witness(config,cfg,sid).with_suffix('.openrouter.json')
    saved = json.loads(path.read_bytes()) if path.is_file() else {}
    revision = saved.get('revision',0)
    if accepted is None and main_reply.available(config,cfg):
        accepted = main_reply.binding(config,cfg)
    digest = fingerprint([cfg['instance_id'],pid,sid,accepted]) if accepted else None
    enabled = bool(digest and saved.get('recipient_hash')==digest and saved.get('enabled'))
    return {'enabled':enabled,'revision':revision,'recipient_hash':digest}


def update_writer_consent(store, config, persona_id, session_id, enabled, revision, recipient_hash):
    from . import main_reply
    if type(enabled) is not bool or type(revision) is not int:
        raise RuntimeErrorBase('invalid_privacy_operation')
    with store.connection(write=True) as db:
        cfg = config.load()
        current_state = writer_consent(db,config,cfg,persona_id,session_id)
        if current_state['revision'] != revision:
            raise RuntimeErrorBase('version_conflict')
        if enabled and (main_reply.settings(cfg)['active'] != main_reply.HYDROGEN
                        or not current_state['recipient_hash'] or recipient_hash!=current_state['recipient_hash']):
            raise RuntimeErrorBase('session_privacy_recipients_changed')
        atomic_json(_witness(config,cfg,session_id).with_suffix('.openrouter.json'),
                    {'revision':revision+1,'enabled':enabled,'recipient_hash':current_state['recipient_hash']},durable=True)
        return writer_consent(db,config,cfg,persona_id,session_id)


def _latest(db, cfg, sid):
    return db.execute("""SELECT * FROM session_privacy_events WHERE instance_id=? AND session_id=?
        ORDER BY revision DESC LIMIT 1""", (cfg['instance_id'], sid)).fetchone()


def _witness(config, cfg, sid):
    return config.paths.home / 'private/session-privacy' / (fingerprint([cfg['instance_id'],sid])+'.json')


def current(db, config, cfg, pid, sid, destinations):
    owned(db, sid, scope(pid, 'desktop'))
    event = _latest(db, cfg, sid)
    path = _witness(config, cfg, sid)
    witness = json.loads(path.read_bytes()) if path.exists() else {}
    rev = max(event['revision'] if event else 0, witness.get('revision', 0))
    active = bool(event and event['enabled'] and event['protocol'] == PROTOCOL
                  and event['operation_hash'] == witness.get('hash') and event['revision'] == witness.get('revision')
                  and json.loads(event['recipients_json']) == destinations)
    return {'enabled': active, 'revision': rev,
            'registered': bool(event and event['enabled']), 'protocol': PROTOCOL,
            'recipients': _display(destinations), 'recipient_hash': fingerprint(destinations)}


def status(store, config, persona_id, session_id):
    cfg = config.load()
    with store.connection() as db:
        state = current(db, config, cfg, persona_id, session_id, recipients(config, cfg))
        models = {role:model['name'] for role,model in configured_models(config,cfg).items()}
        from .private_reply_entry import retired, selected
        installed = selected(db,cfg['instance_id'],persona_id) and not retired(db,cfg['instance_id'])
        public = public_scope(db,config,cfg['instance_id'],persona_id,session_id,state)
        from .public_lookup import VERSION as PUBLIC_LOOKUP_VERSION
        marker=config.paths.home/'private/ordinary-reply/registration.json'
        try:
            registration=json.loads(marker.read_bytes()) if installed and marker.is_file() else {}
        except (OSError,ValueError):
            registration={}
        public['available']=(isinstance(registration,dict) and registration.get('instance')==cfg['instance_id']
                             and (registration.get('public_lookup_version')==PUBLIC_LOOKUP_VERSION
                                  or registration.get('single_retrieval')=='single-retrieval-v1'))
        if public['available'] and public['enabled'] and public['protocol']==PUBLIC_ATTRIBUTE_SCOPE:
            public['upgrade']=public_scope(db,config,cfg['instance_id'],persona_id,session_id,state,
                                           protocol=PUBLIC_QUESTION_SCOPE)
        result=state | {'models':models, 'candidate':bool(installed), 'public_lookup':public}
        from . import community
        if community.enabled():
            result["community_search"] = community_lookup.state(db, config, cfg, persona_id, session_id)[0]
            result["community_auto_search"] = community_lookup.state(db, config, cfg, persona_id, session_id,
                                                                    community_lookup.AUTOMATIC)[0]
        from . import main_reply
        if main_reply.settings(cfg)['active']==main_reply.HYDROGEN:
            result['main_reply_writer']=writer_consent(db,config,cfg,persona_id,session_id)
        if public['available'] and registration.get('single_retrieval')=='single-retrieval-v1':
            result['retrieval_history_turns']=cfg.get('retrieval_history_turns',50)
            from .retrieval_egress import DIGEST
            public['semantic_review']=registration.get('egress_review_sha256')==DIGEST
        return result


def _public_key(instance, pid, sid):
    return 'private.lookup.consent.'+fingerprint([instance,pid,sid])


def _public_protocols(config, instance):
    marker=config.paths.home/'private/ordinary-reply/registration.json'
    try:
        registration=json.loads(marker.read_bytes()) if marker.is_file() else {}
    except (OSError,ValueError):
        return ()
    if not isinstance(registration,dict):
        return ()
    if registration.get('instance')==instance and (registration.get('current_public_attribute') is True
            or registration.get('single_retrieval')=='single-retrieval-v1'):
        return (PUBLIC_QUESTION_SCOPE,PUBLIC_ATTRIBUTE_SCOPE)
    return (PUBLIC_SCOPE,)


def public_scope(db, config, instance, pid, sid, full, *, protocol=None, historical=False):
    # Checking an earlier receipt never grants or enables its protocol for new requests.
    protocols = ((protocol,) if historical and protocol in (PUBLIC_SCOPE,PUBLIC_ATTRIBUTE_SCOPE,PUBLIC_QUESTION_SCOPE)
                 else _public_protocols(config,instance))
    if protocol is not None and protocol not in protocols:
        raise RuntimeErrorBase('session_privacy_recipients_changed')
    key = _public_key(instance,pid,sid)
    row = db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
    saved = json.loads(row[0]) if row else {}
    path = _witness(config,{'instance_id':instance},sid).with_suffix('.public.json')
    witness = json.loads(path.read_bytes()) if path.is_file() else {}
    active_protocol=None
    if full['enabled'] and saved.get('enabled') and saved==witness:
        active_protocol=next((value for value in protocols if saved.get('scope')==
                              fingerprint([value,instance,pid,sid,full['revision'],full['recipient_hash']])),None)
    protocol=protocol or active_protocol or (protocols[0] if protocols else PUBLIC_SCOPE)
    scope_hash = fingerprint([protocol,instance,pid,sid,full['revision'],full['recipient_hash']])
    return {'protocol':protocol,'enabled':protocol==active_protocol,'revision':max(saved.get('revision',0),witness.get('revision',0)),
            'recipient_hash':scope_hash}


def _update_public(db, config, cfg, pid, sid, full, enabled, revision, scope_hash, op, protocol):
    instance = cfg['instance_id']
    key = _public_key(instance,pid,sid)
    state = public_scope(db,config,instance,pid,sid,full,protocol=protocol)
    signature = fingerprint([state['protocol'],op,enabled,revision,scope_hash])
    replay = db.execute('SELECT value FROM knowledge_control WHERE key=?',(key+'.op.'+op,)).fetchone()
    if replay:
        if json.loads(replay[0])!=signature:
            raise RuntimeErrorBase('session_privacy_operation_conflict')
        return public_scope(db,config,instance,pid,sid,full)
    if state['revision']!=revision:
        raise RuntimeErrorBase('version_conflict')
    if state['recipient_hash']!=scope_hash or enabled and not full['enabled']:
        raise RuntimeErrorBase('session_privacy_recipients_changed')
    saved = {'enabled':enabled,'revision':revision+1,'scope':scope_hash,'signature':signature}
    db.execute('INSERT INTO knowledge_control VALUES(?,?)',(key+'.op.'+op,encoded(signature)))
    db.execute('INSERT OR REPLACE INTO knowledge_control VALUES(?,?)',(key,encoded(saved)))
    atomic_json(_witness(config,cfg,sid).with_suffix('.public.json'),saved,durable=True)
    if enabled:
        db.execute("""UPDATE turns SET status='queued',error_code=NULL,updated_at=? WHERE persona_id=? AND session_id=?
            AND cancelled_at IS NULL AND error_code='private_lookup_confirmation_required' AND id IN
            (SELECT turn_id FROM private_lookup_intents WHERE state='waiting')""",(timestamp(),pid,sid))
    return public_scope(db,config,instance,pid,sid,full)


def _record(db, config, cfg, pid, sid, rev, enabled, destinations, op, signature):
    db.execute("""INSERT INTO session_privacy_events VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (cfg['instance_id'], sid, pid, LOCAL_USER_ID, rev, enabled, PROTOCOL,
                encoded(destinations), op, signature, timestamp()))
    # Write before committing. An interrupted write or an older restored DB cannot revive a grant.
    atomic_json(_witness(config, cfg, sid), {'revision': rev, 'hash': signature}, durable=True)


def update(store, config, persona_id, session_id, enabled, revision, recipient_hash, operation_id, protocol):
    if (type(enabled) is not bool or type(revision) is not int or revision < 0
            or protocol not in (PROTOCOL,PUBLIC_SCOPE,PUBLIC_ATTRIBUTE_SCOPE,PUBLIC_QUESTION_SCOPE,*community_lookup.PROTOCOLS)
            or not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200):
        raise RuntimeErrorBase('invalid_privacy_operation')
    with store.connection(write=True) as db:
        cfg = config.load()
        if protocol in community_lookup.PROTOCOLS:
            return community_lookup.update(db, config, cfg, persona_id, session_id, enabled,
                                           revision, recipient_hash, operation_id, protocol)
        dest = recipients(config, cfg)
        state = current(db, config, cfg, persona_id, session_id, dest)
        if protocol!=PROTOCOL:
            return _update_public(db,config,cfg,persona_id,session_id,state,enabled,revision,recipient_hash,operation_id,protocol)
        signature = fingerprint([operation_id, protocol, enabled, revision, recipient_hash])
        replay = db.execute("""SELECT operation_hash FROM session_privacy_events
            WHERE instance_id=? AND session_id=? AND operation_id=?""",
            (cfg['instance_id'], session_id, operation_id)).fetchone()
        if replay:
            if replay[0] != signature:
                raise RuntimeErrorBase('session_privacy_operation_conflict')
            return state
        if revision != state['revision']:
            raise RuntimeErrorBase('version_conflict')
        if enabled and (not dest or recipient_hash != state['recipient_hash']):
            raise RuntimeErrorBase('session_privacy_recipients_changed')
        _record(db, config, cfg, persona_id, session_id, revision+1, enabled, dest,
                operation_id, signature)
        if enabled:
            # Resume only inputs blocked before any external effect in this ordinary session.
            db.execute("""UPDATE turns SET status='queued',error_code=NULL,updated_at=?
                WHERE session_id=? AND persona_id=? AND status='interrupted' AND cancelled_at IS NULL
                AND error_code IN ('private_cloud_provider_not_authorized','private_reply_reference_unavailable',
                                   'private_import_registration_required')
                AND id IN (SELECT turn_id FROM private_reply_turns WHERE route IN ('maibot-private-v2','maibot-private-v3') AND instance_id=?)
                AND NOT EXISTS(SELECT 1 FROM private_reply_nodes n WHERE n.turn_id=turns.id)
                AND NOT EXISTS(SELECT 1 FROM private_reply_attempts a WHERE a.turn_id=turns.id)""",
                (timestamp(),session_id,persona_id,cfg['instance_id']))
            db.execute("""UPDATE turns SET status='queued',error_code=NULL,updated_at=? WHERE session_id=? AND persona_id=?
                AND cancelled_at IS NULL AND error_code='private_lookup_confirmation_required'
                AND id IN (SELECT turn_id FROM private_lookup_intents WHERE state='waiting')""",
                (timestamp(),session_id,persona_id))
        return current(db, config, cfg, persona_id, session_id, dest)


def _change(db, config, role, base_url, name, key):
    from . import community
    single_key = community.enabled()
    if single_key and (role != 'main' or base_url != community.BASE or name != community.PRO):
        raise RuntimeErrorBase('community_official_key_required')
    parsed = urlsplit(base_url)
    if (role not in ('main','jev','organization','selector','lookup_target') or parsed.scheme != 'https' or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or
            not isinstance(name,str) or not name.strip() or not isinstance(key,str)):
        raise RuntimeErrorBase('invalid_model_connection')
    cfg = config.load()
    before = recipients(config, cfg)
    after = dict(before)
    previous = before.get(role, {})
    if role == 'selector' and role not in before:
        previous = destination(config, cfg['character_selector'])
    if not key and base_url.rstrip('/') != previous.get('endpoint'):
        raise RuntimeErrorBase('credential_required_for_new_endpoint')
    binding = {'endpoint': base_url.rstrip('/'), 'account': account(config, key) if key
               else previous.get('account')}
    if role == 'lookup_target':
        from .provider_policy import require_generation_provider
        require_generation_provider({'base_url':base_url,'name':name})
        binding['model'] = name
    if not binding['account']:
        raise RuntimeErrorBase('model_not_configured')
    if role != 'selector' or cfg['character_selector']['enabled']:
        after[role] = binding
    if role == 'main':
        if cfg['organization']['reuse_chat']:
            after['organization'] = binding
        if single_key:
            for purpose in ('organization', 'native_web', 'selector', 'lookup_target'):
                if purpose in before:
                    after[purpose] = dict(before[purpose], **binding)
            after['jev'] = dict(binding, endpoint=community.DECISION_BASE)
    _search_destinations(after, cfg.get('private_research_account','main'))
    affected = []
    if before != after:
        for row in db.execute("""SELECT e.persona_id,e.session_id FROM session_privacy_events e
                WHERE e.instance_id=? AND e.enabled=1 AND NOT EXISTS(SELECT 1 FROM session_privacy_events later
                WHERE later.instance_id=e.instance_id AND later.session_id=e.session_id AND later.revision>e.revision)
                ORDER BY e.session_id""", (cfg['instance_id'],)):
            state = current(db, config, cfg, row['persona_id'], row['session_id'], before)
            if state['enabled']:
                affected.append([row['persona_id'],row['session_id'],state['revision']])
    signature = fingerprint([cfg['instance_id'], before, after, role, name, binding, affected])
    account_changed = previous.get('account') != binding['account']
    return cfg, after, affected, signature, account_changed


def connection(store, config, role, base_url, name, key='', confirm=None):
    with store.connection(write=confirm is not None) as db:
        cfg, after, affected, signature, account_changed = _change(db,config,role,base_url,name,key)
        result = {'recipients': _display(after), 'affected': [sid for _,sid,_ in affected],
                  'account_changed': account_changed,
                  'confirmation': signature}
        result['sessions'] = [dict(db.execute("""SELECT p.name, s.created_at FROM sessions s
            JOIN personas p ON p.id=s.persona_id WHERE s.id=?""",(sid,)).fetchone()) for _,sid,_ in affected]
        if confirm is None:
            return result
        if not hmac.compare_digest(signature, confirm):
            raise RuntimeErrorBase('session_privacy_recipients_changed')
        if role != 'jev':
            model = {'base_url':base_url,'name':name}
            if key:
                model['api_key'] = key
            if role == 'organization':
                if not key and cfg['organization']['reuse_chat']:
                    model['api_key'] = unprotect(cfg['model']['encrypted_key'])
                changes = {'organization':{'reuse_chat':False,'model':model}}
            else:
                slot = {'main':'model','selector':'character_selector','lookup_target':'lookup_target_model'}[role]
                changes = {slot:model}
            config.update(changes)
        else:
            from .character_embedding import private_directory
            path = config.paths.home/'private/knowledge-router/config.json'
            private_directory(config.paths.home,path.parent)
            encrypted = protect(key) if key else configured_model(config.paths)['encrypted_key']
            atomic_json(path, {'base_url':base_url,'name':name,'encrypted_key':encrypted}, durable=True)
        if recipients(config, config.load()) != after:
            raise RuntimeErrorBase('session_privacy_recipients_changed')
        for pid,sid,rev in affected:
            op = identifier()
            _record(db, config, cfg, pid, sid, rev+1, True, after, op,
                    fingerprint([op, signature, sid, rev]))
        return result
