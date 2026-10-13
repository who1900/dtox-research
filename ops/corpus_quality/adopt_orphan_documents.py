"""Prepare/review/apply the two source-proven SIMD orphan snapshots.

Run on Oracle with the existing repair environment. Preparation embeds only the
coarse candidates and writes operational journals; corpus writes occur in apply.
Pass the saved coarse source record explicitly; importing restore_coarse is unsafe.
"""
import argparse
import ast
from contextlib import contextmanager, ExitStack
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import urllib.request
import uuid

from ops import replace_published_labels as owner

PAPERS = ('simd:0048', 'simd:0049')
MODEL_SHA256 = 'c9729cc84cbd0e9fecc759505d2be65916c9fe05222d7ea26c65fcb3382af38d'


def state_defaults(schema):
    """Read literal defaults; do not execute arbitrary schema expressions."""
    result={}
    for _,name,kind,required,default,primary in schema:
        if default is None:result[name]=None
        elif default.upper()=='NULL':result[name]=None
        else:
            try:result[name]=ast.literal_eval(default)
            except (ValueError,SyntaxError) as exc:raise RuntimeError('nonliteral state default: '+name) from exc
            if type(result[name]) not in (str,int,float):raise RuntimeError('unsupported state default: '+name)
    return result


def source_metadata(raw,aid,payloads):
    text=raw.decode('utf-8')
    front=re.match(r'\A---\s*\n(.*?)\n---\s*\n',text,re.S)
    if not front:raise RuntimeError('SIMD frontmatter missing')
    fields=dict(re.findall(r'^(simd|title|created):\s*(.*?)\s*$',front[1],re.M))
    for k,v in list(fields.items()):
        if v.startswith(('"',"'")):fields[k]=ast.literal_eval(v)
    if fields.get('simd')!=aid.split(':')[1]:raise RuntimeError('SIMD frontmatter ID differs')
    created=date.fromisoformat(fields['created'])
    summary=re.search(r'^## Summary\s*\n(.*?)(?=^#{1,6}\s|\Z)',text[front.end():],re.M|re.S)
    if not summary or not summary[1].strip():raise RuntimeError('exact Summary section missing')
    for p in payloads:
        if p.get('title')!=fields['title'] or p.get('year')!=created.year or p.get('layers')!=['web3']:
            raise RuntimeError('source/live bibliographic metadata mismatch')
    return fields['title'],created.year,summary[1].strip()


def coarse_helpers(record_path):
    """Compile only the five reviewed functions and their bounded constants."""
    record=json.loads(Path(record_path).read_text())['/opt/dtox-research/coarse_index.py']
    source=record['source']
    if hashlib.sha256(source.encode()).hexdigest()!=record['sha256']:raise RuntimeError('coarse helper source SHA differs')
    tree=ast.parse(source);functions={'coarse_point_id','source_for','sentence_truncate','build_embed_text','build_payload'}
    constants={}
    for node in tree.body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            constants[node.targets[0].id]=node.value
    # Only the SIMD member is used by this explicitly two-document helper.
    members=constants['_ID_PREFIX_SOURCE'].elts
    simd=[x for x in members if isinstance(x,ast.Tuple) and isinstance(x.elts[1],ast.Constant) and x.elts[1].value=='simd']
    if len(simd)!=1 or ast.unparse(simd[0].elts[0])!='service.SIMD_ID_PREFIX':raise RuntimeError('SIMD source mapping changed')
    limit=constants['ABSTRACT_CHAR_LIMIT']
    if not (isinstance(limit,ast.Call) and isinstance(limit.args[0],ast.Call)):
        raise RuntimeError('coarse abstract limit expression changed')
    namespace=dict(service=SimpleNamespace(uuid=uuid,NAMESPACE_URL=uuid.NAMESPACE_URL),
        _ID_PREFIX_SOURCE=(('simd:','simd'),),
        COARSE_ID_PREFIX=ast.literal_eval(constants['COARSE_ID_PREFIX']),
        MIN_ABSTRACT_LEN=ast.literal_eval(constants['MIN_ABSTRACT_LEN']),
        ABSTRACT_CHAR_LIMIT=int(ast.literal_eval(limit.args[0].args[1])))
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in functions]
    if {n.name for n in nodes}!=functions:raise RuntimeError('coarse helper function missing')
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'verified-coarse-helpers','exec'),namespace)
    return namespace,record['sha256']


def paths(root):
    # These aliases are the inspected production paths; resolve to the physical
    # regular files before handing them to the existing publication owner.
    names=dict(state_db='state.db',fts_db='fts.db',paper_fts_db='papers_fts.db',
        cache_dir='chunk_cache',section_cache_dir='section_cache',fulltext_cache_dir='fulltext_cache',
        publication_cache_dir='publication_cache')
    result={k:(root/v).resolve(strict=True) for k,v in names.items()}
    for k,p in result.items():
        if not (p.is_dir() if k.endswith('_dir') else p.is_file()):raise RuntimeError('invalid production path: '+k)
    result['data_dir']=root.resolve(strict=True)
    return result


def prepare(aid,base,root,client,record_path):
    from ops import restore_cached_points
    config=paths(root);slug=aid.replace(':','-');source=base/'source-candidates'/(slug+'.md')
    receipt_path=source.with_suffix('.receipt.json');receipt=json.loads(receipt_path.read_text())
    if (receipt['paper_id']!=aid or receipt['candidate_status']!='source-bound-match-candidate'
            or receipt['exact_text_index_element_matches']!=receipt['existing_points']):raise RuntimeError('source receipt not exact')
    raw=restore_cached_points._read(source,receipt['sha256'])
    live=owner._scroll(client,'papers_fulltext',aid)
    if len(live)!=receipt['existing_points']:raise RuntimeError('orphan existing point count changed since source receipt')
    title,year,summary=source_metadata(raw,aid,[p['payload'] for p in live.values()])
    with sqlite3.connect(config['state_db'].as_uri()+'?mode=ro',uri=True) as state:
        schema=state.execute('PRAGMA table_info(papers)').fetchall();projection=state_defaults(schema)
        citations=state.execute('SELECT COUNT(*) FROM citations WHERE dst=?',(aid,)).fetchone()[0]
    metadata=next(iter(live.values()))['payload']
    projection.update(arxiv_id=aid,title=title,year=year,layers='web3',status='done',abstract=summary,
        source_url=receipt['source_url'],fulltext_source='spec-markdown',extractor_version=5,
        parser_mode='markdown',parser_revision=5,parse_status='ok',
        updated_at=datetime.now(timezone.utc).isoformat(),niche_score=metadata.get('niche_score'),
        citation_count=metadata.get('citation_count'),influential=metadata.get('influential'),
        venue=metadata.get('venue'),matched_terms=json.dumps(metadata.get('terms',[])))
    if set(projection)!={r[1] for r in schema}:raise RuntimeError('state schema differs from inspected deployment')
    helpers,helper_sha=coarse_helpers(record_path)
    embedding_text=helpers['build_embed_text'](title,summary,aid)
    request=urllib.request.Request('http://127.0.0.1:8090/embed_batch',
        data=json.dumps({'texts':[embedding_text]}).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=120) as response:vector=json.load(response)['vectors'][0]
    coarse=dict(id=helpers['coarse_point_id'](aid),payload=helpers['build_payload'](projection,citations),vector=vector)
    binding=dict(url=receipt['source_url'],commit=receipt['commit'],retrieved_at=receipt['created_utc'],
        retrieval_precision='day',receipt_path=str(receipt_path),coarse_helper_sha256=helper_sha,
        coarse_model_sha256=MODEL_SHA256,coarse_embedding_text_sha256=hashlib.sha256(embedding_text.encode()).hexdigest(),
        coarse_recipe='bge-base int8 768D CLS/L2 max384 no prefix; title+sentence-truncated abstract800')
    journal=base/'orphan-journals'/(slug+'.json');journal.parent.mkdir(exist_ok=True)
    plan=owner.prepare_orphan_adoption(aid,**config,journal_path=journal,qdrant=client,
        source_path=source,expected_source_sha256=receipt['sha256'],source_route='spec_markdown',
        source_binding=binding,state_after=projection,coarse_point=coarse,parser_revision=5)
    return dict(paper_id=aid,phase=plan['phase'],journal=str(journal),revision=plan['paper']['revision'],points=len(live))


@contextmanager
def reader_maintenance(base,aid,systemctl,health,unit):
    """Use the worker's service gate, retaining its original active state."""
    marker=base/'orphan-journals'/(aid.replace(':','-')+'-maintenance.json')
    previous=json.loads(marker.read_text()) if marker.exists() else None
    if previous and previous['phase']=='closed':active=previous['previously_active']
    else:active=systemctl('show',unit,'--value','--property=ActiveState')=='active'
    gate=dict(phase='closed',previously_active=active,safe=False)
    owner.labels._atomic(marker,owner._jbytes(gate))
    if systemctl('show',unit,'--value','--property=ActiveState')=='active':systemctl('stop',unit)
    try:
        yield gate
        gate['safe']=True
    finally:
        if gate['safe']:
            if active:systemctl('start',unit);health()
            gate['phase']='restored';owner.labels._atomic(marker,owner._jbytes(gate))


def apply(aid,base,client,worker):
    import fcntl
    # Use the same one-off worker locks; service inactivity alone does not
    # establish that an operational repair process has stopped.
    with ExitStack() as locks:
        for name in ('labels-worker.lock','points-worker.lock','orphan-adoption.lock'):
            handle=locks.enter_context((base/name).open('a'))
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _apply_locked(aid,base,client,worker)


def _apply_locked(aid,base,client,worker):
    journal=base/'orphan-journals'/(aid.replace(':','-')+'.json')
    for unit in ('dtox-research.service','dtox-pipeline.service'):
        if worker.systemctl('show',unit,'--value','--property=ActiveState') not in ('inactive','failed'):
            raise RuntimeError('pipeline writer is active')
    with reader_maintenance(base,aid,worker.systemctl,worker.health,worker.UNIT) as gate:
        plan=owner._load(journal)
        try:
            if plan['phase']=='applied':owner._verify_adoption(plan,client,'published')
            elif plan['phase']=='prepared':owner.apply_plan(journal,qdrant=client,maintenance=True)
            elif plan['phase'] in ('applying','rolling_back'):
                owner.rollback_plan(journal,qdrant=client,maintenance=True)
            elif plan['phase']=='rolled_back':owner._verify_adoption(plan,client,'before')
            else:raise RuntimeError('unknown adoption journal phase')
        except BaseException:
            current=owner._load(journal)
            if current['phase'] in ('applying','applied','rolling_back'):
                owner.rollback_plan(journal,qdrant=client,maintenance=True)
            owner._verify_adoption(owner._load(journal),client,'before')
            gate['safe']=True
            raise
    return dict(paper_id=aid,phase=owner._load(journal)['phase'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('prepare','apply'))
    parser.add_argument('--paper',choices=PAPERS,required=True)
    parser.add_argument('--base',type=Path,default=Path('/opt/dtox-corpus-quality-20261013'))
    parser.add_argument('--root',type=Path,default=Path('/opt/dtox-research'))
    parser.add_argument('--coarse-source-record',type=Path)
    args=parser.parse_args()
    try:from ops.corpus_quality import run_label_worker as worker
    except ImportError:import run_label_worker as worker
    class Client(worker.Qdrant):
        def overwrite_payload(self,*,collection_name,payload,points,wait):
            return self.request('/collections/'+collection_name+'/points/payload?wait=true',
                {'payload':payload,'points':points},method='PUT')
    if args.phase=='prepare':
        if args.coarse_source_record is None:parser.error('prepare requires --coarse-source-record')
        result=prepare(args.paper,args.base.resolve(),args.root.resolve(),Client(),args.coarse_source_record)
    else:result=apply(args.paper,args.base.resolve(),Client(),worker)
    print(json.dumps(result))


if __name__=='__main__':main()
