"""Opt-in transports for an externally reviewed private-chat assembly."""
import json
import math
import os
from contextlib import closing, nullcontext
from tempfile import NamedTemporaryFile
from hashlib import sha256
from functools import partial
from pathlib import Path
import re

from .archive import encoded
from .chat import ChatCompletionsClient
from .knowledge_routing import configured_model, binding as jev_binding
from .knowledge_web_decision import usage
from .models import RuntimeErrorBase
from .provider_compat import build_request
from .scene_jev import JevTransport
from .provider_policy import DEEPSEEK_TEXT_RELAY, is_official_deepseek


OFFICIAL_DEEPSEEK_ROUTE = 'deepseek-official-jev-ef-v1'


class ReviewedPrivateChatProviders:
    """The invoking candidate must supply the hash of the exact author-reviewed bundle."""
    generation_route = None
    background_lookup = True
    def __init__(self,config,path,reviewed_sha256,*,writer=None,jev_factory=JevTransport,organizer=None,target_client=None):
        raw=Path(path).read_bytes()
        self.version=sha256(raw).hexdigest()
        if self.version!=reviewed_sha256:
            raise RuntimeErrorBase('private_chat_review_version_changed')
        document=json.loads(raw)
        has_source_assessment='source_assessment' in document
        source_assessment=document.pop('source_assessment',None)
        assessment_parent=dict(document)
        self.source_assessment_sha256=None
        has_translation_range='translation_range' in document
        translation_range=document.pop('translation_range',None)
        base_bundle=dict(document)
        self.translation_range_sha256=None
        self.conversation_systems = {}
        self.conversation_mode = None
        if 'conversation_parent' in document:
            if set(document) - {'conversation_parent','conversation_systems','conversation_mode'} or 'conversation_systems' not in document:
                raise RuntimeErrorBase('private_chat_review_incomplete')
            parent_hash = document['conversation_parent']
            if not isinstance(parent_hash,str) or len(parent_hash)!=64 or any(c not in '0123456789abcdef' for c in parent_hash):
                raise RuntimeErrorBase('private_chat_review_version_changed')
            required = {'reply_system','short_answer_system','post_search_system','partwise_post_search_system'}
            systems = document['conversation_systems']
            if not isinstance(systems,dict) or set(systems)!=required or not all(isinstance(v,str) and v.strip() for v in systems.values()):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            parent = (config.paths.home/'private/ordinary-reply'/(parent_hash+'.json')).read_bytes()
            if sha256(parent).hexdigest()!=parent_hash:
                raise RuntimeErrorBase('private_chat_review_version_changed')
            self.conversation_systems = systems
            self.conversation_mode = document.get('conversation_mode')
            if self.conversation_mode is not None:
                mode=self.conversation_mode
                if (not isinstance(mode,dict) or set(mode)!={'instructions','criteria'} or
                    not isinstance(mode['instructions'],str) or not mode['instructions'].strip() or
                    not isinstance(mode['criteria'],dict) or set(mode['criteria'])!={'chat','short_answer','long_detail'} or
                    not all(isinstance(v,str) and v.strip() for v in mode['criteria'].values())):
                    raise RuntimeErrorBase('private_chat_review_incomplete')
            document = json.loads(parent)
        from .private_reply_output import (
            ADVISORY_VERSIONS, FACT_REVIEW_VERSION, SEARCH_SPEECH_V1, SEARCH_SPEECH_VERSION, SEMANTIC_REVIEW_VERSION, VERSION,
        )
        self.output_contract_version = document.get('output_contract_version')
        if self.output_contract_version not in (None, VERSION, *ADVISORY_VERSIONS):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.output_contract_version and not (
            isinstance(document.get('short_answer_system'), str) and document['short_answer_system'].strip()
        ):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        nodes={'J1','J2.action','J2.stance','J2.task','J3','J6.mode','J6.coverage','J6.ending'}
        # Old reviewed bundles and their hashes remain readable. Retired review
        # material is excluded from the executable assembly.
        from . import reply_provenance
        self.provenance_version=document.get('provenance_version')
        if 'provenance_version' in document:
            if (self.provenance_version!=reply_provenance.VERSION or not document.get('public_lookup_version')
                    or document.get('lookup_target',{}).get('version')!='lookup-target-v4'):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            nodes.add(reply_provenance.NODE)
            audit_node=document.get('nodes',{}).get(reply_provenance.NODE,{})
            if (set(audit_node.get('criteria',{}))!=set(reply_provenance.OPTIONS)
                    or not all(isinstance(v,str) and v.strip() for v in audit_node['criteria'].values())):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        if isinstance(document.get('nodes'),dict):
            document['nodes'].pop('J7',None)
        document.pop('notice_review',None)
        self.research=document.get('protocol')=='maibot-private-v3'
        self.grounded_search = document.get('research_context_version') == 'search-evidence-v2'
        from .local_knowledge import VERSION as LOCAL_LOOKUP_VERSION
        local_version=document.get('local_lookup_version')
        from .knowledge_revisions import VERSION as REVISION_VERSION
        from . import knowledge_coverage
        self.structured_study = local_version==knowledge_coverage.VERSION
        dependency_version=document.get('requirement_dependency_version')
        self.conditional_coverage = dependency_version in ('conditional-v1','conditional-v2')
        self.relation_judge = dependency_version=='conditional-v2'
        if self.relation_judge:
            nodes.add('J5.relation')
        if 'requirement_dependency_version' in document and not (self.structured_study and self.conditional_coverage):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        self.knowledge_revisions = local_version==REVISION_VERSION or self.structured_study
        self.local_first = local_version in (LOCAL_LOOKUP_VERSION,'local-first-v2') or self.knowledge_revisions
        self.semantic_lookup = self.local_first
        self.fact_review = self.output_contract_version == FACT_REVIEW_VERSION
        search_speech_version = document.get('search_speech_version')
        self.phase_scoped_search = search_speech_version in (SEARCH_SPEECH_V1, SEARCH_SPEECH_VERSION)
        self.plain_speech_output = search_speech_version == SEARCH_SPEECH_VERSION
        continuation_templates = ('post_search_system', 'post_search_long_system')
        if 'search_speech_version' in document or any(k in document for k in continuation_templates):
            if not (self.phase_scoped_search and self.research and self.grounded_search and self.fact_review
                    and document.get('notice') and all(isinstance(document.get(k), str) and document[k].strip()
                                                      for k in continuation_templates)):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.fact_review and local_version!='local-first-v2' and not self.knowledge_revisions:
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.knowledge_revisions and not self.fact_review:
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if 'local_lookup_version' in document and not (self.local_first and self.grounded_search
                and self.output_contract_version in (SEMANTIC_REVIEW_VERSION, FACT_REVIEW_VERSION)):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if 'research_context_version' in document and not (self.grounded_search and self.research):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.research:
            nodes.update(('J4','J5'))
            if not all(isinstance(document.get(k),str) and document[k].strip()
                       for k in ('notice_system','long_system','search_system')):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        self.styles=document.get('styles',{})
        if not isinstance(self.styles,dict) or any(not isinstance(values,dict) for values in self.styles.values()):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if any(self.styles.values()):
            nodes.add('J2.style')
        if (document.get('protocol') not in ('maibot-private-v2','maibot-private-v3') or set(document.get('nodes',{}))!=nodes
                or not isinstance(document.get('reply_system'),str) or not document['reply_system'].strip()):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        scope=document.get('scope',{})
        if (scope.get('instance')!=config.load()['instance_id'] or not isinstance(scope.get('personas'),list)
                or not scope['personas'] or not all(isinstance(i,str) and i for i in scope['personas'])):
            raise RuntimeErrorBase('private_chat_review_scope_changed')
        self.bundle=document
        self.retrieval_spec=None
        self.egress_spec=None
        if 'retrieval_manifest' in document:
            from .single_retrieval import load_manifest
            self.retrieval_spec=load_manifest(document,config)
            self.retrieval_history_turns=config.load().get('retrieval_history_turns',50)
            if type(self.retrieval_history_turns) is not int or not 0<=self.retrieval_history_turns<=50:
                raise RuntimeErrorBase('retrieval_history_scope_invalid')
        if 'egress_manifest' in document:
            from .retrieval_egress import load_manifest
            self.egress_spec=load_manifest(document,config,self.retrieval_spec)
        if 'translation_manifest' in document:
            from .single_retrieval import apply_translation_manifest
            self.retrieval_spec=apply_translation_manifest(document,config,self.retrieval_spec)
        if has_translation_range:
            from .single_retrieval import apply_translation_range,TRANSLATION_RANGE_V3_DIGEST
            self.retrieval_spec=apply_translation_range(translation_range,base_bundle,config,self.retrieval_spec)
            self.translation_range_sha256=TRANSLATION_RANGE_V3_DIGEST
        if has_source_assessment:
            from .single_retrieval import apply_source_assessment,SOURCE_ASSESSMENT_V2_DIGEST
            self.retrieval_spec=apply_source_assessment(source_assessment,assessment_parent,config,self.retrieval_spec)
            self.source_assessment_sha256=SOURCE_ASSESSMENT_V2_DIGEST
        self.generation_route=document.get('generation_route')
        if 'generation_route' in document and self.generation_route!=OFFICIAL_DEEPSEEK_ROUTE:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        self.text_flash_account=document.get('text_flash_account')
        if self.text_flash_account not in (None,'organization'):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if 'notice' in document and (not self.research or
                                     not self.text_flash_account and document['notice']!=document.get('search')):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        for preset in document['nodes'].values():
            if (not isinstance(preset,dict) or not isinstance(preset.get('instructions'),str)
                    or not preset['instructions'].strip() or not isinstance(preset.get('criteria'),dict)):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.local_first:
            choices=document['nodes']['J4']['criteria']
            if set(choices)!={'retrieve','none'} or not all(isinstance(v,str) and v.strip() for v in choices.values()):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        self.study_enabled='knowledge_study' in document
        if self.study_enabled!=(local_version=='local-first-v2' or self.knowledge_revisions):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if self.study_enabled:
            study=document['knowledge_study']
            if (not self.local_first or not isinstance(study,dict) or set(study)!={'version','model','instructions'}
                    or study['version']!=(knowledge_coverage.STUDY_VERSION if self.structured_study else 'source-facts-v1')
                    or study['model']!=document.get('notice' if self.text_flash_account else 'search')
                    or not isinstance(study['instructions'],str) or 'json' not in study['instructions'].casefold()):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            self.study_instructions=study['instructions']
            self.organizer=organizer or ChatCompletionsClient()
        if self.structured_study:
            selector=document['nodes']['J5']
            if (set(selector['criteria'])!={'complete','insufficient'} or
                    not all(isinstance(v,str) and v.strip() for v in [*selector['criteria'].values(),selector.get('source_criterion')])):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            if self.conditional_coverage and not (isinstance(selector.get('correction_criterion'),str)
                                                   and selector['correction_criterion'].strip()):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            if self.relation_judge:
                relation=document['nodes']['J5.relation']
                if (set(relation['criteria'])!={'independent'} or not all(isinstance(v,str) and v.strip()
                    for v in (relation['criteria']['independent'],relation.get('source_criterion')))):
                    raise RuntimeErrorBase('private_chat_review_incomplete')
        self.config=config
        from . import research_contract
        self.research_contract_version=None
        if 'research_coverage' in document:
            candidate=document['research_coverage']
            if (not isinstance(candidate,dict) or set(candidate)!={'version','model','system'}
                    or candidate['version']!=research_contract.VERSION or not self.provenance_version
                    or not isinstance(candidate['system'],str) or not candidate['system'].strip()
                    or self.text_flash_account!='organization' or candidate['model']!=document.get('notice')):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            self.research_contract_version=candidate['version']
        self.target_resolution = 'lookup_target' in document
        if self.target_resolution:
            from .lookup_target import VERSIONS as TARGET_VERSIONS
            preset = document['lookup_target']
            if (not self.semantic_lookup or not isinstance(preset,dict)
                    or not all(isinstance(document.get(role),dict) and document[role].get('name')
                               for role in ('main','jev'))
                    or set(preset)!={'version','model','system'} or preset['version'] not in TARGET_VERSIONS
                    or not isinstance(preset['system'],str) or not preset['system'].strip()):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            self.lookup_target_version=preset['version']
            from .lookup_target import QUOTED_TARGET_VERSION
            if self.lookup_target_version==QUOTED_TARGET_VERSION and not (
                    isinstance(document.get('lookup_delivery_system'),str) and document['lookup_delivery_system'].strip()
                    and document.get('public_lookup_version')):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            build_request(self.target_model() | {'_json_output':True},[{'role':'system','content':preset['system']}])
            self.target_client = target_client or ChatCompletionsClient()
        from .public_lookup import VERSION as PUBLIC_LOOKUP_VERSION
        self.public_lookup_version = document.get('public_lookup_version')
        self.notice_context_profiles=None
        if 'notice_context' in document:
            from .lookup_target import QUOTED_TARGET_VERSION
            from .search_notice_context import valid_profiles
            notice_context=document['notice_context']
            if not (self.plain_speech_output and self.public_lookup_version==PUBLIC_LOOKUP_VERSION
                    and getattr(self,'lookup_target_version',None)==QUOTED_TARGET_VERSION
                    and valid_profiles(notice_context,scope['personas'])):
                raise RuntimeErrorBase('private_chat_review_incomplete')
            self.notice_context_profiles=notice_context['profiles']
        from .public_lookup_study import SOURCE_OPTIONS
        source_review=document.get('public_lookup_j5')
        if self.public_lookup_version or 'public_lookup_j5' in document:
            if (not self.public_lookup_version or not isinstance(source_review,dict)
                    or set(source_review)!={'instructions','criteria'} or not isinstance(source_review['criteria'],dict)
                    or set(source_review['criteria'])!=set(SOURCE_OPTIONS) or not all(isinstance(v,str) and v.strip()
                        for v in (source_review['instructions'],*source_review['criteria'].values()))):
                raise RuntimeErrorBase('private_chat_review_incomplete')
        if 'public_lookup_version' in document and not (self.public_lookup_version==PUBLIC_LOOKUP_VERSION
                and self.target_resolution and self.structured_study and self.grounded_search and self.fact_review):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        self.compatible_writer = document.get('writer_transport')=='openai-chat-completions-v1'
        settings = config.load()
        from .provider_policy import is_deepseek_text_relay,is_private_compatible_writer,require_generation_provider
        main_target=document.get('main') or {}
        gateway_writer=is_private_compatible_writer(main_target)
        deepseek_writer=is_deepseek_text_relay(main_target)
        if not deepseek_writer and main_target.get('name')=='deepseek-v4-pro':
            try:
                require_generation_provider(main_target)
            except ValueError:
                pass
            else:
                deepseek_writer=True
        research_account=settings.get('private_research_account','main')
        if 'writer_transport' in document and not (
            self.compatible_writer and self.research and
            (gateway_writer and research_account=='organization' and not self.text_flash_account or
             deepseek_writer and research_account=='native_web' and self.text_flash_account=='organization')
        ):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if document.get('search_account','main')!=research_account:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if self.research:
            self.search_model()
        if self.text_flash_account:
            if not deepseek_writer or not self.compatible_writer or 'notice' not in document:
                raise RuntimeErrorBase('private_chat_review_recipient_changed')
            self.notice_model()
        if self.generation_route:
            if (self.text_flash_account!='organization' or research_account!='native_web'
                    or document.get('jev')!={'base_url':DEEPSEEK_TEXT_RELAY,'name':'jev-1.14'}
                    or main_target.get('name')!='deepseek-v4-pro' or not is_official_deepseek(main_target)):
                raise RuntimeErrorBase('private_chat_review_recipient_changed')
            self._model('main')
            self._model('jev')
            self._check_selector(settings)
        self.writer=writer or ChatCompletionsClient(allow_private_compatible_writer=self.compatible_writer)
        self.jev_factory=jev_factory
        self.connections={}

    async def close(self):
        for transport in self.connections.values():
            await transport.close()
        self.connections.clear()

    def check_scope(self,base):
        if base['instance']!=self.bundle['scope']['instance'] or base['persona'] not in self.bundle['scope']['personas']:
            raise RuntimeErrorBase('private_chat_review_scope_changed')
        if self.compatible_writer and not base['consent']['enabled']:
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
        if self.generation_route:
            self._check_selector(self.config.load())
        self._model('jev')
        self._model('main')

    def _model(self,role):
        current=configured_model(self.config.paths) if role=='jev' else self.config.load()['model']
        expected=self.bundle.get(role)
        if expected!={k:current[k] for k in ('base_url','name')}:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        return current

    def _check_selector(self, settings):
        selector=settings['character_selector']
        if selector['enabled'] and not is_official_deepseek(selector):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')

    def bind_choose(self):
        return partial(self._choose,self._model('jev'))

    def bind_public_source(self):
        if not self.public_lookup_version:
            raise RuntimeErrorBase('private_chat_review_incomplete')
        return partial(self._choose,self._model('jev'),source_review=self.bundle['public_lookup_j5'])

    def target_model(self, settings=None):
        model = (self.config.load() if settings is None else settings).get('lookup_target_model')
        if (not model or self.bundle['lookup_target']['model']!={k:model[k] for k in ('base_url','name')}
                or model['name'] in (self.bundle['main']['name'],self.bundle['jev']['name'])
                or (self.generation_route and (not is_official_deepseek(model)
                    or model['name'] not in ('deepseek-v4-flash','deepseek-flash')))):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        return model

    def lookup_target_budget(self):
        return self.target_model()['max_input_chars']-len(self.bundle['lookup_target']['system'])

    def bind_target(self, base, state):
        from .session_privacy import recipients
        from .archive import fingerprint
        settings = self.config.load()
        model = self.target_model(settings) | {'_json_output':True}
        if (not base['consent']['enabled'] or fingerprint(recipients(self.config,settings))
                !=base['consent']['recipient_hash']):
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
        messages = [{'role':'system','content':self.bundle['lookup_target']['system']},
                    {'role':'user','content':encoded(state)}]
        if sum(len(m['content']) for m in messages)>model['max_input_chars']:
            raise RuntimeErrorBase('lookup_target_input_budget_exceeded')
        request,_ = build_request(model,messages)
        audit = {'model':model['name'],'endpoint':model['base_url'],'preset_version':self.version,
                 'request_sha256':fingerprint(request),'recipient_hash':base['consent']['recipient_hash']}
        return partial(self._structured_reply,self.target_client,model,messages),audit

    def bind_coverage(self,base,state):
        from .archive import fingerprint
        from .session_privacy import recipients
        preset=self.bundle.get('research_coverage')
        settings=self.config.load()
        if not self.research_contract_version or not preset or state.get('version')!=self.research_contract_version:
            raise RuntimeErrorBase('private_chat_review_incomplete')
        if (not base['consent']['enabled'] or fingerprint(recipients(self.config,settings))
                !=base['consent']['recipient_hash']):
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
        model=self.study_model()|{'_json_output':True}
        if preset['model']!={k:model[k] for k in ('name','base_url')}:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        messages=[{'role':'system','content':preset['system']},{'role':'user','content':encoded(state)}]
        if sum(len(m['content']) for m in messages)>model['max_input_chars']:
            raise RuntimeErrorBase('research_coverage_input_budget')
        packet,_=build_request(model,messages)
        if packet.get('tools') or packet.get('tool_choice'):
            raise RuntimeErrorBase('private_reply_tools_forbidden')
        audit={'model':model['name'],'endpoint':model['base_url'],'purpose':'lookup_coverage',
               'request_sha256':fingerprint(packet)}
        return partial(self._structured_reply,self.organizer,model,messages),audit

    def bind_retrieval(self,base,state,phase):
        from .single_retrieval import check_recipient,validate_fields,validate_packet
        spec=self.retrieval_spec
        if phase=='egress':
            if not self.egress_spec:
                raise RuntimeErrorBase('private_chat_review_incomplete')
            validate_fields(self.egress_spec['user_fields'],state)
            system=self.egress_spec['system']
        else:
            validate_packet(spec,phase,state)
            system=spec['phases'][phase]['system']
        model=check_recipient(self.config,spec,base)|{'_json_output':True}
        messages=[{'role':'system','content':system},
                  {'role':'user','content':encoded(state)}]
        if sum(len(m['content']) for m in messages)>model['max_input_chars']:
            raise RuntimeErrorBase('retrieval_input_budget')
        packet,_=build_request(model,messages)
        from .archive import fingerprint
        if packet.get('tools') or packet.get('tool_choice') or packet.get('response_format')!={'type':'json_object'}:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        return partial(self._structured_reply,self.organizer,model,messages),{
            'model':model['name'],'endpoint':model['base_url'],'purpose':phase,'request_sha256':fingerprint(packet)}

    async def _structured_reply(self,client,model,messages,attempt):
        response = await client.complete(model, messages)
        raw,usage = response
        audit = {'usage':usage,'raw_sha256':sha256(raw.encode()).hexdigest()}
        def unique(fields):
            if len(dict(fields))!=len(fields):
                raise ValueError('duplicate_field')
            return dict(fields)
        try:
            result = json.loads(raw,object_pairs_hook=unique)
        except (ValueError,RecursionError):
            result = None
        if (getattr(response,'diagnostics',None) or {}).get('finish_reason') not in (None,'stop'):
            result = None
        return result,audit

    async def choose(self,node,attempt,state,options):
        return await self.bind_choose()(node,attempt,state,options)

    async def _choose(self,model,node,attempt,state,options,*,source_review=None):
        if self.knowledge_revisions:
            from .knowledge_revisions import model_material
            state=model_material(state)
        preset=source_review if source_review is not None else self.bundle['nodes'].get(node)
        if node=='J6.mode' and self.conversation_mode and state.get('phase')!='search_notice':
            preset=self.conversation_mode
        if source_review is not None:
            from .public_lookup_study import SOURCE_OPTIONS
            from .lookup_target import QUOTED_TARGET_VERSION
            keys={'question','source'}
            if self.lookup_target_version==QUOTED_TARGET_VERSION:
                keys.update(('as_of','candidate_scopes'))
            if node!='J5' or set(state)!=keys or tuple(options)!=SOURCE_OPTIONS:
                raise RuntimeErrorBase('private_reply_invalid_choices')
        if preset is None:
            raise RuntimeErrorBase('private_reply_decision_unavailable')
        fixed=preset['criteria']
        criteria={k:fixed.get(k,preset.get('source_criterion')) for k in options}
        if node=='J5' and getattr(self,'conditional_coverage',False):
            criteria.update((k,preset['correction_criterion']) for k in options if k.startswith('correct/'))
        if (not isinstance(preset.get('instructions'),str) or not preset['instructions'].strip()
                or not all(isinstance(v,str) and v.strip() for v in criteria.values())):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        body={'model':model['name'],'state':state,
              'questions':{node:{'type':'choice','instructions':preset['instructions'],'criteria':criteria}}}
        if len(encoded(body))>model['max_input_chars']:
            raise RuntimeErrorBase('jev_input_budget_exceeded')
        key=jev_binding(model)
        if key not in self.connections:
            self.connections[key]=self.jev_factory(model)
        timing={}
        status,raw=await self.connections[key].complete(body,on_dispatch=lambda _:None,on_receipt=lambda _:None,
                                                        diagnostics=timing.update)
        audit={'model':model['name'],'status':status,'timing':timing,'raw_sha256':sha256(raw.encode()).hexdigest(),
               'options':list(options),'preset_version':self.version}
        if source_review is not None:
            from .archive import fingerprint
            audit.update(purpose='public_lookup_j5',request_sha256=fingerprint(body))
        elif node=='lookup.attribution':
            from .archive import fingerprint
            audit.update(purpose=node,request_sha256=fingerprint(body))
        try:
            result=json.loads(raw)
        except ValueError:
            return None,audit
        answers=result.get('answers') if isinstance(result,dict) else None
        if status!=200 or not isinstance(answers,dict) or set(answers)!={node} or not isinstance(answers[node],dict):
            return None,audit
        answer=answers[node]
        choice=answer.get('choice')
        weights=answer.get('probabilities',{})
        if (answer.get('type')!='choice' or choice not in options or not isinstance(weights,dict) or set(weights)!=set(options)
                or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in weights.values())
                or not math.isclose(sum(weights.values()),1,abs_tol=.001)):
            return None,audit
        return choice,audit|{'usage':usage(result),'probabilities':weights,'confidence':answer.get('confidence')}

    def notice_model(self):
        if 'notice' not in self.bundle:
            return None
        return self.study_model()

    def study_model(self):
        if not self.text_flash_account:
            return self.search_model()
        settings=self.config.load()
        model=settings['organization']['model']
        from .provider_policy import is_deepseek_text_relay
        allowed=is_official_deepseek(model) if self.generation_route else is_deepseek_text_relay(model)
        if (settings['organization']['reuse_chat'] or model['name']!='deepseek-flash' or not allowed
                or self.bundle.get('notice')!={k:model[k] for k in ('base_url','name')}):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        return model

    def search_model(self):
        from .deepseek_native_contract import official_endpoint,private_research_model
        model=private_research_model(self.config)
        if self.bundle.get('search_account','main')!=self.config.load().get('private_research_account','main'):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if self.bundle.get('search')!={k:model[k] for k in ('base_url','name')}:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        official_endpoint(model)
        return model

    def bind_phase_reply(self,phase):
        model=(self.notice_model() if phase=='search_notice' else None) or self._model('main')
        return partial(self._reply,model)

    def bind_reply(self):
        return self.bind_phase_reply('main')

    async def reply(self,attempt,packet):
        return await self.bind_phase_reply(packet.get('phase','main'))(attempt,packet)

    async def _reply(self,model,attempt,packet):
        if self.knowledge_revisions:
            from .knowledge_revisions import model_material
            packet=model_material(packet)
        if self.output_contract_version:
            from .private_reply_output import limits
            if packet.get('output_constraints') != limits(packet['phase'], packet['mode'],
                                                          compact_notice=self.phase_scoped_search,conversation=bool(self.conversation_systems)):
                raise RuntimeErrorBase('private_reply_output_contract_missing')
        long=packet.get('mode')=='long_detail'
        template='reply_system'
        research=packet.get('research') or {}
        lookup_finished=(packet.get('phase')=='main' and
                         research.get('retrieval',{}).get('tool') in ('acquired','empty','failed'))
        if self.phase_scoped_search and packet.get('phase')=='search_notice':
            from .search_notice_context import valid_packet
            valid=valid_packet(packet) if self.notice_context_profiles else (
                set(packet)=={'phase','mode','output_constraints'} and packet['mode']=='short_answer')
            if not valid:
                raise RuntimeErrorBase('private_search_notice_input_invalid')
        if packet.get('phase')=='search_notice' and not long:
            template='notice_system'
        elif self.phase_scoped_search and (packet.get('search_notice_presented') is True or lookup_finished):
            template='post_search_long_system' if long else 'post_search_system'
        elif long:
            template='long_system'
        elif self.output_contract_version and packet.get('mode')=='short_answer':
            template='short_answer_system'
        if self.plain_speech_output and not long:
            # Plain text already has a single output slot. Field names are only needed for the JSON form.
            packet=packet|{'output_constraints':{key:value for key,value in packet['output_constraints'].items()
                                                if key!='output_fields'}}
        system=self.conversation_systems.get(template) or self.bundle[template]
        from .lookup_target import QUOTED_TARGET_VERSION
        attributed=bool(packet.get('provenance') and getattr(self,'provenance_version',None))
        if attributed:
            system=self.bundle['lookup_delivery_system']
            model=model|{'_json_output':True}
        elif (not getattr(self,'provenance_version',None) and getattr(self,'lookup_target_version',None)==QUOTED_TARGET_VERSION
                and packet.get('phase')=='main'
                and research.get('status') not in (None,'ordinary')):
            system+='\n'+self.bundle['lookup_delivery_system']
            if not research.get('facts'):
                packet=dict(packet,dialogue=[],evidence=[])
        partwise=None
        items=(research.get('retrieval') or {}).get('items',[])
        if (self.conversation_systems and not attributed and template=='post_search_system'
                and packet.get('action')=='respond' and research.get('status')=='local_hit'
                and research.get('lookup_outcome')=='studied' and len(items)>1
                and all(i['result'] in ('partial','complete') and i['facts'] for i in items)):
            from .partwise_reply import prepare
            partwise=prepare(packet)
            packet=partwise.packet
            system=self.conversation_systems['partwise_post_search_system']
            model=model|{'_json_output':True}
        messages=[{'role':'system','content':system},
                  {'role':'user','content':encoded(packet)}]
        if sum(len(m['content']) for m in messages)>model['max_input_chars']:
            raise RuntimeErrorBase('private_reply_input_budget_exceeded')
        from .provider_policy import is_private_compatible_writer
        if self.compatible_writer and is_private_compatible_writer(model):
            model=model|{'_private_writer_stream':True}
        request,_=build_request(model,messages)
        if request.get('tools') or request.get('tool_choice'):
            raise RuntimeErrorBase('private_reply_tools_forbidden')
        response=await self.writer.complete(model,messages)
        raw,usage=response
        usage=dict(usage or {},requested_model=model['name'])
        if self.notice_context_profiles and packet.get('phase')=='search_notice':
            from .archive import fingerprint
            usage['request_sha256']=fingerprint(request)
        echoed=(getattr(response,'diagnostics',None) or {}).get('requested_model_echoed')
        if self.compatible_writer and type(echoed) is bool:
            usage['requested_model_echoed']=echoed
        if partwise:
            from .partwise_reply import assemble
            try:
                combined=assemble(raw,partwise)
            except (ValueError,TypeError,RecursionError) as error:
                return raw or '{}',usage|{'partwise_error':type(error).__name__}
            return combined.body,usage|{'partwise_sources':combined.bindings,'raw_sha256':sha256(raw.encode()).hexdigest()}
        if attributed:
            from .reply_provenance import unpack
            slots,annotation=unpack(raw,long,action=packet.get('action'),mode=packet.get('mode'))
            return slots,usage|{'lookup_attribution':annotation,'raw_sha256':sha256(raw.encode()).hexdigest()}
        result=raw,usage
        if not long:return result
        raw,usage=result
        def unique(items):
            obj=dict(items)
            if len(obj)!=len(items):raise ValueError('duplicate_long_reply_field')
            return obj
        try:slots=json.loads(raw,object_pairs_hook=unique)
        except ValueError:slots=raw
        return slots,dict(usage or {},long_receipt_sha256=sha256(raw.encode()).hexdigest())

    async def organize(self,attempt,packet,model):
        from .deepseek_native_contract import MODEL
        search=self.study_model()
        if (not self.study_enabled or model['name']!=MODEL or model['base_url']!=search['base_url']
                or model.get('encrypted_key')!=search.get('encrypted_key')
                or self.bundle['knowledge_study']['model']!={k:model[k] for k in ('name','base_url')}):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        expanded=model.get('_study_length_retry',False)
        model=search|{'_json_output':True}
        if getattr(self,'structured_study',False):
            from . import knowledge_coverage
            minimum=knowledge_coverage.RETRY_OUTPUT_TOKENS if expanded else knowledge_coverage.OUTPUT_TOKENS
            model['max_tokens']=max(model.get('max_tokens',0),minimum)
        messages=[{'role':'system','content':self.study_instructions},
                  {'role':'user','content':encoded(packet)}]
        if sum(len(m['content']) for m in messages)>model['max_input_chars']:
            raise RuntimeErrorBase('knowledge_study_input_too_large')
        raw,usage=await self.organizer.complete(model,messages)
        return {'raw':raw},usage

    def bind_retrieve(self,step,attempt,query,model):
        from .deepseek_native_contract import VERSION,MODEL,official_endpoint,OUTPUT_TOKENS
        from .deepseek_native_transport import DeepSeekNativeClient
        from .knowledge_general import IndependentWebTools
        from .knowledge_native import NativeRetrieval
        if step not in ('native','search','fetch'):
            raise RuntimeErrorBase('private_lookup_invalid_step')
        if not self.research or self.bundle.get('search')!={'base_url':model['base_url'],'name':MODEL}:
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        if model.get('encrypted_key')!=self.search_model().get('encrypted_key'):
            raise RuntimeErrorBase('private_chat_review_recipient_changed')
        official_endpoint(model)
        if step=='native':
            actual=model|{'max_tokens':max(model['max_tokens'],OUTPUT_TOKENS['deep']),
                          '_native_web_profile':{'version':VERSION,'mode':'deep','tools':'search','stream':True}}
            question=query['question']
            if self.grounded_search and query.get('as_of'):
                question=f"查询日期：{query['as_of']}\n{question}"
            if query.get('url') and query['url'] not in question:
                question=question+'\n'+query['url']
            messages=[{'role':'system','content':self.bundle['search_system']},{'role':'user','content':question}]
            if sum(len(m['content']) for m in messages)>actual['max_input_chars']:
                raise RuntimeErrorBase('native_input_budget_exceeded')
            page=query.get('url') if self.study_enabled and query.get('intent')=='read_link' else None
            async def native():
                received=await DeepSeekNativeClient().complete(actual,messages)
                body,usage=received
                return NativeRetrieval.result_from_receipt(received.native,body,attempt,prefer_text=self.study_enabled,
                        requested_page=page),{'usage':usage,'native':received.native,'transport':received.diagnostics}
            payload,_=build_request(actual,messages)
            return native,official_endpoint(actual),payload
        from .knowledge_web_transport import request
        args=query|{'count':8,'need_content':True} if step=='search' else query
        if step=='fetch':
            from .knowledge_general import public_url
            args=dict(args,url=public_url(args['url']))
        async def independent():
            received=await request(step,args,model)
            raw,usage=received
            result=IndependentWebTools.accept({'knowledge_processing':{'stage':'conversation_'+step,'input':args}},raw)
            return result,usage
        return independent,'https://api.deepseek.com/v1/tools/'+step,args

    async def retrieve(self,step,attempt,query,model):
        send,_,_=self.bind_retrieve(step,attempt,query,model)
        return await send()


def registered(config):
    root=config.paths.home/'private/ordinary-reply'
    suspension=root/'retired.json'
    if suspension.is_file():
        from .private_reply_entry import ORDINARY, RESEARCH, ROUTE
        from .storage import Store
        record=json.loads(suspension.read_bytes())
        instance=config.load()['instance_id']
        if record.get('instance')==instance:
            if record.get('route') not in (ROUTE,ORDINARY,RESEARCH):
                raise RuntimeErrorBase('private_reply_route_changed')
            with Store(config.paths).connection(write=True) as db:
                db.execute("INSERT OR IGNORE INTO private_reply_instances VALUES(?,?)",(instance,record['route']))
                db.execute("INSERT OR IGNORE INTO knowledge_control VALUES(?,?)",('private.reply.retired.'+instance,'true'))
            return None
    marker=root/'registration.json'
    if not marker.is_file():
        return None
    grant=json.loads(marker.read_bytes())
    if grant.get('instance')!=config.load()['instance_id']:
        return None
    digest=grant.get('reviewed_sha256','')
    if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
        raise RuntimeErrorBase('private_chat_review_version_changed')
    return ReviewedPrivateChatProviders(config,root/(digest+'.json'),digest)


def _registration_lock(config):
    from .instance import InstanceLock
    return closing(InstanceLock(config.paths.home/'runtime/private-reply-registration.lock'))


def retire(config):
    """Stop candidate dispatch on a closed instance while retaining its frozen routes and history."""
    from .migration import _offline
    from .character_embedding import private_directory
    from .config import atomic_json
    from .private_reply_entry import selected
    from .storage import Store
    with _registration_lock(config), _offline(config.paths), Store(config.paths).connection(write=True) as db:
        instance=config.load()['instance_id']
        route=selected(db,instance)
        if not route:
            raise RuntimeErrorBase('private_reply_route_changed')
        root=private_directory(config.paths.home,config.paths.home/'private/ordinary-reply')
        atomic_json(root/'retired.json',{'instance':instance,'route':route},durable=True)
        db.execute("INSERT OR IGNORE INTO knowledge_control VALUES(?,?)",
                   ('private.reply.retired.'+instance,'true'))


def register(config,path,reviewed_sha256):
    from .migration import _offline
    with _registration_lock(config):
        retired=(config.paths.home/'private/ordinary-reply/retired.json').is_file()
        with _offline(config.paths) if retired else nullcontext():
            _register(config,path,reviewed_sha256)


def _register(config,path,reviewed_sha256):
    from .config import atomic_json
    from .character_embedding import private_directory
    from .private_personas import safe
    providers=ReviewedPrivateChatProviders(config,path,reviewed_sha256)
    providers._model('jev')
    providers._model('main')
    root=private_directory(config.paths.home,config.paths.home/'private/ordinary-reply')
    target=root/(reviewed_sha256+'.json')
    raw=Path(path).read_bytes()
    if sha256(raw).hexdigest()!=reviewed_sha256:
        raise RuntimeErrorBase('private_chat_review_version_changed')
    safe(target,root)
    if not target.exists() or target.read_bytes()!=raw:
        with NamedTemporaryFile(dir=root,delete=False) as output:
            temporary=Path(output.name)
        try:
            with temporary.open('wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
    if target.read_bytes()!=raw:
        raise RuntimeErrorBase('private_chat_review_version_changed')
    marker={'instance':config.load()['instance_id'],'reviewed_sha256':reviewed_sha256}
    if providers.conversation_systems:
        marker['conversation_bubble_personas']=providers.bundle['scope']['personas']
    if providers.retrieval_spec:
        from .single_retrieval import VERSION
        marker['single_retrieval']=VERSION
        if 'translation_manifest' in providers.bundle:
            from .single_retrieval import TRANSLATION_V2_DIGEST
            marker['translation_review_sha256']=TRANSLATION_V2_DIGEST
        if providers.translation_range_sha256:
            marker['translation_range_review_sha256']=providers.translation_range_sha256
        if providers.source_assessment_sha256:
            marker['source_assessment_review_sha256']=providers.source_assessment_sha256
        if providers.egress_spec:
            from .retrieval_egress import DIGEST
            marker['egress_review_sha256']=DIGEST
    if providers.public_lookup_version:
        marker['public_lookup_version']=providers.public_lookup_version
        from .lookup_target import QUOTED_TARGET_VERSION
        if providers.lookup_target_version==QUOTED_TARGET_VERSION:
            marker['current_public_attribute']=True
    atomic_json(root/'registration.json',marker,durable=True)
    from .storage import Store
    store=Store(config.paths)
    if (root/'retired.json').is_file() and store.path.is_file():
        store.initialize()
        with store.connection(write=True) as db:
            db.execute("DELETE FROM knowledge_control WHERE key=?",('private.reply.retired.'+config.load()['instance_id'],))
    (root/'retired.json').unlink(missing_ok=True)


if __name__=='__main__':
    import argparse
    from .config import ConfigStore
    from .paths import RuntimePaths
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',required=True,type=Path)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--bundle',type=Path)
    action.add_argument('--retire',action='store_true')
    parser.add_argument('--reviewed-sha256')
    args=parser.parse_args()
    config=ConfigStore(RuntimePaths(args.home.resolve()))
    if args.retire:
        retire(config)
    elif args.reviewed_sha256:
        register(config,args.bundle,args.reviewed_sha256)
    else:
        parser.error('--bundle requires --reviewed-sha256')
