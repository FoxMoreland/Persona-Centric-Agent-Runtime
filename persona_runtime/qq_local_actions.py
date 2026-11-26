"""Scoped SnowLuma actions for a QQ event-only connection."""
import json
from pathlib import Path

import aiohttp

from .models import RuntimeErrorBase
from .qq_native_reply import full_local_ready,pro_test


def enabled(paths, cfg):
    pid=cfg['runtime']['qq_persona_id']
    return full_local_ready(paths,cfg,pid) or pro_test(paths,cfg,pid)


class SnowLumaLocalActions:
    def __init__(self, paths, cfg):
        if not enabled(paths,cfg):
            raise RuntimeErrorBase('qq_pro_test_scope_changed')
        record = json.loads((paths.home/'private/qq-core-history.json').read_bytes())
        port = record['port']
        if type(port) is not int or not 1 <= port <= 65535:
            raise RuntimeErrorBase('qq_local_actions_unavailable')
        candidates = json.loads(Path(record['config_file']).read_bytes())['networks']['httpServers']
        servers = [item for item in candidates
                   if (item.get('host'),item.get('port')) == ('127.0.0.1',port)
                   and item.get('enabled') is not False]
        if len(servers)!=1 or not servers[0].get('accessToken'):
            raise RuntimeErrorBase('qq_local_actions_unavailable')
        self.endpoint = f'http://127.0.0.1:{port}/'
        self.token = servers[0]['accessToken']
        self.bot,self.peer = int(cfg['bot_qq']),int(cfg['owner_qq'])

    async def _call(self, action, payload):
        async with aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=12)) as session:
            async with session.post(self.endpoint+action,json=payload,
                    headers={'Authorization':'Bearer '+self.token},allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeErrorBase('qq_local_http_error')
                result = await response.json()
                if not isinstance(result,dict) or result.get('retcode')!=0 or result.get('status')!='ok':
                    raise RuntimeErrorBase('qq_local_action_failed')
                return result['data']

    async def get_login_info(self, **routing):
        return await self._call('get_login_info',{})

    async def get_status(self, **routing):
        return await self._call('get_status',{})

    async def get_version_info(self, **routing):
        return await self._call('get_version_info',{})

    async def send_msg(self, *, message_type, self_id, user_id, message):
        if (message_type,self_id,user_id)!=('private',self.bot,self.peer):
            raise RuntimeErrorBase('qq_pro_test_scope_changed')
        return await self._call('send_msg',{'message_type':message_type,'self_id':self_id,
                                            'user_id':user_id,'message':message})
