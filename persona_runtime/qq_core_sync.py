"""Paginate authorized core QQ history into the local context journal only."""
import asyncio
import json
from pathlib import Path

import aiohttp

from .config import atomic_json
from .models import RuntimeErrorBase, timestamp


PAGE_SIZE = 50


async def synchronize(journal, peer, fetch):
    old = journal.watermark(peer)
    newest = None
    anchor = 0
    seen = set()
    stored = 0
    while True:
        items = await fetch(peer,anchor,PAGE_SIZE)
        if not isinstance(items,list) or len(items) > PAGE_SIZE:
            raise RuntimeErrorBase('qq_history_invalid_page')
        if not items:
            if anchor:
                raise RuntimeErrorBase('qq_history_anchor_unavailable')
            if old:
                raise RuntimeErrorBase('qq_history_watermark_unavailable')
            raise RuntimeErrorBase('qq_history_empty_unverified')
        if any(not isinstance(row,dict) or type(row.get('time')) is not int
               or type(row.get('message_id')) is not int or not row['message_id']
               or str(row.get('user_id','')) not in (journal.bot,peer) for row in items):
            raise RuntimeErrorBase('qq_history_invalid_page')
        if anchor and all(row['message_id'] != anchor for row in items):
            raise RuntimeErrorBase('qq_history_anchor_unavailable')
        items.sort(key=lambda row: row['time'],reverse=True)
        reached = False
        for item in items:
            mid = str(item['message_id'])
            if newest is None:
                newest = mid
            if mid == old:
                reached = True
                break
            own = str(item['user_id']) == journal.bot
            raw = {**item,'self_id':journal.bot,'post_type':'message_sent' if own else 'message',
                   'message_type':'private','sub_type':'friend'}
            if own:
                raw['target_id'] = peer
            result = journal.capture(raw)
            if result is None:
                raise RuntimeErrorBase('qq_history_invalid_message')
            stored += result != 'duplicate'
        if reached or len(items) < PAGE_SIZE:
            if old and not reached:
                raise RuntimeErrorBase('qq_history_watermark_unavailable')
            journal.watermark(peer,newest)
            return stored
        anchor = items[-1]['message_id']
        if anchor in seen:
            raise RuntimeErrorBase('qq_history_anchor_unavailable')
        seen.add(anchor)


async def http_worker(paths, journal, config_file, port):
    target = paths.home/'runtime/qq-core-sync.json'
    async with aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=20)) as session:
        while True:
            try:
                with Path(config_file).open(encoding='utf-8') as stream:
                    config = json.load(stream)
                servers = [node for node in config['networks']['httpServers']
                           if (node.get('host'),node.get('port')) == ('127.0.0.1',port)
                           and node.get('enabled') is not False]
                if len(servers)!=1 or not servers[0].get('accessToken'):
                    raise RuntimeErrorBase('qq_history_endpoint_unavailable')
                headers = {'Authorization':'Bearer '+servers[0]['accessToken']}

                async def action(name, value):
                    async with session.post(f'http://127.0.0.1:{port}/{name}',json=value,headers=headers,
                                             allow_redirects=False) as response:
                        response.raise_for_status()
                        envelope = await response.json()
                        if not isinstance(envelope,dict) or envelope.get('retcode') != 0 or envelope.get('status') != 'ok':
                            code = envelope.get('retcode') if isinstance(envelope,dict) else None
                            raise RuntimeErrorBase('qq_history_platform_error'+(':'+str(code) if type(code) is int else ''))
                        return envelope['data']

                account = await action('get_login_info',{})
                if str(account.get('user_id','')) != journal.bot:
                    raise RuntimeErrorBase('qq_history_identity_mismatch')

                async def fetch(peer, anchor, count):
                    page = await action('get_friend_msg_history',{'user_id':int(peer),'message_id':anchor,
                                                                 'count':count,'reverse_order':True})
                    return page['messages']

                added = sum([await synchronize(journal,peer,fetch) for peer in journal.core])
                atomic_json(target,{'state':'synced','messages_added':added,'at':timestamp()},durable=True)
                return
            except (RuntimeErrorBase,aiohttp.ClientError,TimeoutError,OSError,ValueError,KeyError,TypeError) as error:
                code = str(error) if isinstance(error,RuntimeErrorBase) else type(error).__name__
                if isinstance(error,RuntimeErrorBase):
                    atomic_json(target,{'state':'unverified','reason':code,'at':timestamp()},durable=True)
                    return
                atomic_json(target,{'state':'waiting','reason':code,'at':timestamp()})
                await asyncio.sleep(15)
