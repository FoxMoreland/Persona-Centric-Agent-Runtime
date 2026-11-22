"""Transport author-reviewed choice material through the DeepSeek strict tool API."""

import json
from hashlib import sha256

from . import community
from .archive import encoded
from .models import RuntimeErrorBase
from .scene_jev import JevTransport


PROFILE = 'deepseek-community-choice-v1'
FUNCTION = 'record_reply_decision'


def build(body, max_input_chars):
    if body['model'] != community.FLASH or not body['questions']:
        raise RuntimeErrorBase('community_decision_contract_invalid')
    content = encoded({'state': body['state'], 'questions': body['questions']})
    if len(content) > max_input_chars:
        raise RuntimeErrorBase('community_decision_input_budget_exceeded')
    properties = {}
    for name, question in body['questions'].items():
        if question['type'] != 'choice' or not question['criteria']:
            raise RuntimeErrorBase('community_decision_contract_invalid')
        properties[name] = {'type': 'string', 'enum': list(question['criteria'])}
    schema = {'type': 'object', 'properties': properties, 'required': list(properties),
              'additionalProperties': False}
    return {
        'model': community.FLASH,
        'messages': [{'role': 'user', 'content': content}],
        'thinking': {'type': 'disabled'}, 'stream': False, 'max_tokens': 256,
        'tools': [{'type': 'function', 'function': {
            'name': FUNCTION, 'strict': True, 'parameters': schema}}],
        'tool_choice': {'type': 'function', 'function': {'name': FUNCTION}},
    }


def unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate decision field')
    return result


def normalize(raw, body):
    from .knowledge_web_decision import usage

    packet = json.loads(raw, object_pairs_hook=unique)
    result = {'model': packet.get('model'), 'decision_source': PROFILE, 'usage': usage(packet),
              'answers': {}}
    try:
        choice, = packet['choices']
        message = choice['message']
        call, = message['tool_calls']
        function = call['function']
        if (packet['model'] not in (community.FLASH, 'DeepSeek-V4.1-Flash-1017')
                or choice['finish_reason'] != 'tool_calls' or message['role'] != 'assistant'
                or call['type'] != 'function' or function['name'] != FUNCTION
                or message.get('content') not in (None, '')):
            return result
        answers = json.loads(function['arguments'], object_pairs_hook=unique)
        questions = body['questions']
        if not isinstance(answers, dict) or set(answers) != set(questions):
            return result
        if any(not isinstance(value, str) or value not in questions[name]['criteria']
               for name, value in answers.items()):
            return result
        result['answers'] = {name: {'type': 'choice', 'choice': value} for name, value in answers.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return result


class Transport(JevTransport):
    def __init__(self, model):
        if (model['base_url'], model['name']) != (community.DECISION_BASE, community.FLASH):
            raise RuntimeErrorBase('community_official_key_required')
        super().__init__(model, endpoint=community.DECISION_BASE + '/chat/completions', profile=PROFILE)

    async def complete(self, body, **callbacks):
        wire = build(body, self.model['max_input_chars'])
        status, raw = await super().complete(wire, **callbacks)
        if status != 200:
            return status, raw
        try:
            normalized = normalize(raw, body)
        except (ValueError, AttributeError):
            normalized = {'answers': {}, 'decision_source': PROFILE}
        normalized['provider_receipt_sha256'] = sha256(raw.encode()).hexdigest()
        return status, encoded(normalized)
