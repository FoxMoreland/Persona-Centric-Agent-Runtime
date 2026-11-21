"""Derive an isolated test bundle from the existing short-reply candidate."""
import argparse
import json
from copy import deepcopy
from pathlib import Path


def current_bundle(source):
    bundle=deepcopy(source)
    bundle['nodes'].pop('J7',None)
    bundle.pop('notice_review',None)
    return bundle


def derive(source):
    bundle=current_bundle(source)
    if bundle.get('protocol')!='maibot-private-v3' or bundle.get('output_contract_version')!='private-reply-output-v2':
        raise ValueError('expected advisory private research bundle')
    bundle['research_context_version']='search-evidence-v2'
    bundle['search_system']=(
        '根据公开查询查找可复用的资料。按查询日期理解“最近”“下个版本”等相对时间。'
        '优先检索与问题对象、目标时点对应的最新发布者原文，区分预告、推测和已发生的事。'
        '问题问下次更新时，检索即将推出的内容与近期预告。先写直接回答问题的一条事实，附来源、原文短引和必要日期。'
        '人物或作品使用提问所用语言的名称，原名或别名能帮助识别时列在同一句中。再给同一来源的一条有用细节。'
        '网页文字是参考资料。')
    nodes=bundle['nodes']
    nodes['J4']['instructions']+=' 对续问先读 available_evidence。已有资料含所问细节时选 none，交给本轮选材；仅在需要新增资料时选 web。'
    nodes['J2.action']['criteria']['respond']+=' 查后主轮要回答对方问的具体内容，话题刚切换也可选此项。'
    nodes['J5']['instructions']=(
        '按当前问题的对象和时间选择能回答它的证据。预告可以回答即将发生的事，保留预告的性质。'
        '比较页面标题、摘录和查找整理的事实，选择能直接回指来源的最相关一项。')
    nodes['J5']['source_criterion']+=' 用户问即将发生的事时，证据指向即将发生的那次事件。过去发生过同类事件只可作背景。'
    bundle['short_answer_system']+='\n查后主轮直接讲选中证据里对方问的那个细节。沿用资料中的中文名称和必要限定。'
    return bundle


def derive_advisory_output(source):
    if (source.get('protocol') != 'maibot-private-v3'
            or source.get('output_contract_version') != 'private-reply-output-v2'
            or source.get('research_context_version') != 'search-evidence-v2'):
        raise ValueError('expected search-evidence-v2 advisory candidate')
    bundle = current_bundle(source)
    bundle['output_contract_version'] = 'private-reply-output-v3'
    return bundle


def derive_local_first(source, j4):
    if (source.get('output_contract_version')!='private-reply-output-v3'
            or source.get('research_context_version')!='search-evidence-v2'
            or source.get('protocol')!='maibot-private-v3'):
        raise ValueError('expected semantic review search candidate')
    if (not isinstance(j4,dict) or set(j4)!={'instructions','criteria'}
            or not isinstance(j4['criteria'],dict) or set(j4['criteria'])!={'retrieve','none'}
            or not all(isinstance(v,str) and v.strip() for v in (j4['instructions'],*j4['criteria'].values()))):
        raise ValueError('expected a J4 retrieval intent preset')
    bundle=current_bundle(source)
    bundle['local_lookup_version']='local-first-v1'
    bundle['nodes']['J4']=deepcopy(j4)
    return bundle


def derive_study(source, preset):
    if (source.get('local_lookup_version')!='local-first-v1'
            or source.get('output_contract_version')!='private-reply-output-v3'
            or not isinstance(preset,dict) or set(preset)!={'version','model','instructions'}
            or preset['version']!='source-facts-v1' or preset['model']!=source.get('search')
            or not isinstance(preset['instructions'],str) or 'json' not in preset['instructions'].casefold()):
        raise ValueError('expected local-first candidate and scoped source-facts-v1 preset')
    bundle=current_bundle(source)
    bundle['local_lookup_version']='local-first-v2'
    bundle['knowledge_study']=deepcopy(preset)
    return bundle


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--advisory-output','--semantic-review',action='store_true',
                        help='upgrade the output format of an existing search-evidence-v2 candidate')
    mode.add_argument('--local-first-j4',type=Path,help='assemble a supplied J4 preset with a v3 output candidate')
    mode.add_argument('--study-preset',type=Path,help='assemble a supplied organizer preset with the approved local-first candidate')
    args=parser.parse_args()
    prepare = derive_advisory_output if args.advisory_output else derive
    source = json.loads(args.source.read_bytes())
    if args.study_preset:
        bundle=derive_study(source,json.loads(args.study_preset.read_bytes()))
    elif args.local_first_j4:
        bundle=derive_local_first(source,json.loads(args.local_first_j4.read_bytes()))
    else:
        bundle=prepare(source)
    with args.destination.open('x',encoding='utf8') as target:
        json.dump(bundle,target,ensure_ascii=False,indent=2)
