"""Reviewed, turn-local context for the short speech before public lookup."""
from .archive import fingerprint
from .models import RuntimeErrorBase

VERSION = 'search-notice-context-v1'
CURRENT_LIMIT = 3
CURRENT_CHARACTERS = 80
REFERENCE_CHARACTERS = 100
PROFILE_FIELDS = {'core_revision','expression_revision','trait_indices','style_range'}
PACKET_FIELDS = {'phase','mode','persona','context','situation','output_constraints'}
SITUATION = {'user_request_received':True,'public_lookup':'ready_to_start','results_available':False}


def valid_profiles(preset, persona_ids):
    if not isinstance(preset,dict) or set(preset)!={'version','profiles'} or preset['version']!=VERSION:
        return False
    profiles=preset['profiles']
    if not isinstance(profiles,dict) or set(profiles)!=set(persona_ids):
        return False
    for profile in profiles.values():
        if not isinstance(profile,dict) or set(profile)!=PROFILE_FIELDS:
            return False
        if not all(isinstance(profile[k],str) and profile[k] for k in ('core_revision','expression_revision')):
            return False
        traits,start_end=profile['trait_indices'],profile['style_range']
        if (not isinstance(traits,list) or not 1<=len(traits)<=3
                or any(type(i) is not int or i<0 for i in traits) or len(set(traits))!=len(traits)
                or not isinstance(start_end,list) or len(start_end)!=2 or any(type(i) is not int for i in start_end)
                or not 0<=start_end[0]<start_end[1] or start_end[1]-start_end[0]>240):
            return False
    return True


def _valid_excerpt(row, roles, limit):
    return (isinstance(row,dict) and set(row)=={'role','text'} and row['role'] in roles
            and isinstance(row['text'],str) and row['text'].strip() and len(row['text'])<=limit)


def valid_packet(packet):
    if set(packet)!=PACKET_FIELDS or packet['phase']!='search_notice' or packet['mode']!='short_answer':
        return False
    situation=packet['situation']
    if (not isinstance(situation,dict) or set(situation)!=set(SITUATION)
            or situation['user_request_received'] is not True or situation['results_available'] is not False
            or situation['public_lookup']!=SITUATION['public_lookup']):
        return False
    actor,context=packet['persona'],packet['context']
    if not isinstance(actor,dict) or set(actor)!={'name','core_traits','expression_style'}:
        return False
    if (not all(isinstance(actor[k],str) and actor[k].strip() for k in ('name','expression_style'))
            or len(actor['expression_style'])>240):
        return False
    traits=actor['core_traits']
    if not isinstance(traits,list) or not 1<=len(traits)<=3 or any(not isinstance(t,str) or not t.strip() for t in traits):
        return False
    if not isinstance(context,dict) or set(context)!={'current','reference','public_query'}:
        return False
    current,reference,query=context['current'],context['reference'],context['public_query']
    return (isinstance(current,list) and len(current)<=CURRENT_LIMIT
            and all(_valid_excerpt(row,('user',),CURRENT_CHARACTERS) for row in current)
            and (reference is None or _valid_excerpt(reference,('user','assistant','persona'),REFERENCE_CHARACTERS))
            and isinstance(query,str) and bool(query.strip()))


def _actor(material, profile):
    persona=material['persona']
    versions=material['profile_versions']
    if versions.get('core')!=profile['core_revision'] or versions.get('expression')!=profile['expression_revision']:
        raise RuntimeErrorBase('private_reply_persona_changed')
    indices=profile['trait_indices']
    raw_traits=persona['core']['core_traits']
    raw_style=persona['expression']['style']
    start,end=profile['style_range']
    if any(i>=len(raw_traits) for i in indices) or end>len(raw_style):
        raise RuntimeErrorBase('private_reply_persona_changed')
    return {'name':persona['core']['name'],'core_traits':[raw_traits[i] for i in indices],
            'expression_style':raw_style[start:end]}


def _excerpt(row, intervals, limit, *, whole):
    text=row['text']
    if whole and len(text)<=limit and text.strip():
        return (0,len(text),text)
    for start,end in intervals:
        if 0<=start<end<=len(text) and end-start<=limit and text[start:end].strip():
            return (start,end,text[start:end])
    return None


def _context(material, target, order):
    catalog=material['catalog']
    intervals={}
    for pin in order['pins']:
        if 'start' in pin and 'end' in pin:
            intervals.setdefault(pin['ref'],[]).append((pin['start'],pin['end']))
    current=material['current']
    preferred=[key for key in current if key in intervals or key==target]
    if target=='batch':
        preferred.extend(key for key in current if key not in preferred)
    selected={}
    for key in preferred:
        row=catalog.get(key)
        if row and row['role']=='user':
            part=_excerpt(row,intervals.get(key,()),CURRENT_CHARACTERS,whole=True)
            if part:
                selected[key]=part
            if len(selected)==CURRENT_LIMIT:
                break
    snippets=[]
    bindings=[]
    for key in current:
        if key in selected:
            start,end,text=selected[key]
            snippets.append({'role':'user','text':text})
            bindings.append({'event':key,'span':[start,end]})
    reference=None
    reference_binding=None
    if len(snippets)<CURRENT_LIMIT:
        visible=dict.fromkeys([*material['references'],*reversed(material['recent'])])
        for key in visible:
            row=catalog.get(key)
            if key not in current and row and row['role'] in ('user','persona','assistant'):
                part=_excerpt(row,intervals.get(key,()),REFERENCE_CHARACTERS,whole=False)
                if part:
                    start,end,text=part
                    reference={'role':row['role'],'text':text}
                    reference_binding={'event':key,'span':[start,end]}
                    break
    return {'current':snippets,'reference':reference,'public_query':order['query']['question']},{
        'current':bindings,'reference':reference_binding}


def prepare(db, config, turn, base, material, proof, profiles):
    from . import private_lookup, public_lookup
    if not base['consent']['enabled']:
        raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    profile=profiles.get(turn['persona_id'])
    if not profile:
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    order=public_lookup.require_order(db,config,turn,base,private_lookup.intent(db,turn['id']))
    context,bindings=_context(material,proof['research']['target'],order)
    packet={'phase':'search_notice','mode':'short_answer','persona':_actor(material,profile),'context':context,
            'situation':dict(SITUATION),
            'output_constraints':proof['output_constraints']}
    return packet, {'version':VERSION,'order_hash':fingerprint(order),'packet_hash':fingerprint(packet),
                    'core_revision':profile['core_revision'],'expression_revision':profile['expression_revision'],
                    'inputs':bindings}
