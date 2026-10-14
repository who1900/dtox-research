"""One frozen real 64-passage workload; read-only host and retrieval comparison."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import time
import urllib.request

PAPERS = ['2408.11826','1603.08648','1605.00724','acl:2021.acl-long.501']


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _post(url,body):
    request=urllib.request.Request(url,data=json.dumps(body,ensure_ascii=False).encode(),
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=180) as response:return json.load(response)


def _endpoint(url,route):
    return url.removesuffix('/embed_batch').rstrip('/')+route


def _validate(vectors,count):
    if not isinstance(vectors,list) or len(vectors)!=count:raise ValueError('embedding count mismatch')
    norms=[]
    for vector in vectors:
        if (not isinstance(vector,list) or len(vector)!=384
                or any(type(x) not in (int,float) or not math.isfinite(x) for x in vector)):
            raise ValueError('finite 384D vectors required')
        norm=math.sqrt(sum(x*x for x in vector))
        if abs(norm-1)>1e-3:raise ValueError('embedding is not L2 normalized')
        norms.append(norm)
    return {'norm_min':min(norms),'norm_max':max(norms)}


def _percentile(values,p):
    values=sorted(values);at=(len(values)-1)*p;lo=int(at);hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(at-lo)


def compare_vectors(old,new):
    old_norms=_validate(old,len(old));new_norms=_validate(new,len(old))
    cosine=[sum(a*b for a,b in zip(x,y))/(math.sqrt(sum(a*a for a in x))*math.sqrt(sum(b*b for b in y))) for x,y in zip(old,new)]
    delta=[abs(a-b) for x,y in zip(old,new) for a,b in zip(x,y)]
    identical=all(struct.pack('!d',float(a))==struct.pack('!d',float(b)) for x,y in zip(old,new) for a,b in zip(x,y))
    return dict(bitwise_identical=identical,cosine_min=min(cosine),cosine_mean=sum(cosine)/len(cosine),
        component_delta_p95=_percentile(delta,.95),component_delta_p99=_percentile(delta,.99),
        component_delta_max=max(delta),old=old_norms,new=new_norms)


def rank_documents(query,vectors,records,paper_id,expected_point_id=None):
    scores=[sum(a*b for a,b in zip(query,vector)) for vector in vectors]
    ordered=sorted(range(len(records)),key=lambda i:(-scores[i],records[i]['point_id']))
    rank=next((at+1 for at,i in enumerate(ordered) if records[i]['paper_id']==paper_id),None)
    result=dict(target_paper_rank=rank,hit_at_5=rank is not None and rank<=5,
        hit_at_10=rank is not None and rank<=10,top_ids=[records[i]['point_id'] for i in ordered[:10]],
        top_11=[dict(point_id=records[i]['point_id'],paper_id=records[i]['paper_id'],score=scores[i]) for i in ordered[:11]],
        top_10_boundary_margin=scores[ordered[9]]-scores[ordered[10]] if len(ordered)>10 else None)
    if expected_point_id is not None:
        expected=next((at+1 for at,i in enumerate(ordered) if records[i]['point_id']==expected_point_id),None)
        result.update(expected_point_rank=expected,expected_point_hit_at_5=expected is not None and expected<=5,
            expected_point_hit_at_10=expected is not None and expected<=10,
            expected_point_score=scores[ordered[expected-1]] if expected is not None else None)
    return result


def _freeze(qdrant,papers):
    if len(papers)!=4 or len(set(papers))!=4:raise ValueError('four distinct target papers required')
    pools=[];queries=[]
    for paper in papers:
        body={'filter':{'must':[{'key':'arxiv_id','match':{'value':paper}}]},'limit':200,
              'with_payload':True,'with_vector':False}
        points=_post(qdrant.rstrip('/')+'/collections/papers_fulltext/points/scroll',body)['result']['points']
        records=[dict(point_id=str(p['id']),paper_id=paper,text=p['payload']['text']) for p in points
                 if isinstance(p['payload'].get('text'),str) and p['payload']['text'].strip()]
        if not records:raise ValueError('target paper has no current indexed passages: '+paper)
        title=next((p['payload'].get('title') for p in points if p['payload'].get('title')),None)
        if not isinstance(title,str) or not title.strip():
            coarse=dict(body,limit=1)
            metadata=_post(qdrant.rstrip('/')+'/collections/papers_coarse_base/points/scroll',coarse)['result']['points']
            title=metadata[0]['payload'].get('title') if metadata else None
        if not isinstance(title,str) or not title.strip():raise ValueError('target paper title unavailable: '+paper)
        queries.append(dict(paper_id=paper,text=title))
        records.sort(key=lambda r:(len(r['text']),r['point_id']))
        # Spread each paper's first selection across length quantiles; subsequent
        # picks fill short-paper deficits without duplicating passages.
        size=min(16,len(records));indices=[round(i*(len(records)-1)/max(1,size-1)) for i in range(size)]
        pools.append([records[i] for i in indices]+[r for i,r in enumerate(records) if i not in indices])
    selected=[]
    while len(selected)<64:
        changed=False
        for pool in pools:
            if pool and len(selected)<64:selected.append(pool.pop(0));changed=True
        if not changed:raise ValueError('four papers contain fewer than 64 indexed passages')
    selected.sort(key=lambda r:(len(r['text']),r['point_id']))
    return {'schema':1,'records':selected,'queries':queries,'query_scope':'paper titles; target-paper relevance only'}


def benchmark(args):
    if args.fixture:raw=Path(args.fixture).read_bytes();fixture=json.loads(raw)
    else:
        fixture=_freeze(args.qdrant,args.paper or PAPERS)
        raw=json.dumps(fixture,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    records=fixture['records'];queries=fixture['queries']
    if (len(records)!=64 or len(queries) not in (4,6) or len({r['point_id'] for r in records})!=64
            or any(not isinstance(r.get('text'),str) or not r['text'].strip() for r in records)):
        raise ValueError('fixture must contain 64 distinct passages and four or six fixed queries')
    for item in queries:
        if item.get('expected_point_id') is not None and not any(
                r['point_id']==item['expected_point_id'] and r['paper_id']==item['paper_id'] for r in records):
            raise ValueError('expected fragment must belong to target paper in frozen workload')
    if args.save_fixture:Path(args.save_fixture).write_bytes(raw)
    texts=[r['text'] for r in records]
    def encode(endpoint):
        started=time.monotonic();vectors=_post(_endpoint(endpoint,'/embed_batch'),{'texts':texts})['vectors']
        elapsed=time.monotonic()-started;_validate(vectors,64)
        return vectors,{'seconds':elapsed,'passages_per_second':64/elapsed}
    old,old_time=encode(args.old);new,new_time=encode(args.new)
    parity=compare_vectors(old,new)
    result=dict(fixture_sha256=_sha(raw),workload_size=64,
        workload=[dict(point_id=r['point_id'],paper_id=r['paper_id'],text_sha256=_sha(r['text'].encode()),characters=len(r['text'])) for r in records],
        old_endpoint=args.old,new_endpoint=args.new,throughput={'old':old_time,'new':new_time},parity=parity,
        scope='Same frozen batch; bounded cross-host comparison, not corpus quality acceptance')
    if parity['bitwise_identical']:
        result['retrieval']={'performed':False,'reason':'identical document vectors imply identical rankings for every fixed query vector'}
        return result
    retrieval=[]
    for item in queries:
        query=_post(_endpoint(args.old,'/embed'),{'text':item['text'],'query':True})['vector'];_validate([query],1)
        expected=item.get('expected_point_id')
        before=rank_documents(query,old,records,item['paper_id'],expected);after=rank_documents(query,new,records,item['paper_id'],expected)
        overlaps={str(k):len(set(before['top_ids'][:k])&set(after['top_ids'][:k]))/k for k in (5,10)}
        retrieval.append(dict(paper_id=item['paper_id'],expected_point_id=expected,query_sha256=_sha(item['text'].encode()),old=before,new=after,top_k_overlap=overlaps))
    result['retrieval']={'performed':True,'scope':fixture.get('query_scope','explicit fixed queries; target-paper relevance only'),'queries':retrieval}
    relevance_loss=any((r['old']['hit_at_5'] and not r['new']['hit_at_5']) or
        (r['old']['hit_at_10'] and not r['new']['hit_at_10']) or
        (r['old'].get('expected_point_hit_at_5') and not r['new'].get('expected_point_hit_at_5')) or
        (r['old'].get('expected_point_hit_at_10') and not r['new'].get('expected_point_hit_at_10')) for r in retrieval)
    # This threshold controls one diagnostic repeat, not a quality acceptance
    # decision. Do not waste another real batch for near-exact numerical drift.
    repeat=parity['cosine_min']<.99999 or relevance_loss
    result['old_repeat']={'performed':repeat,'trigger':'cosine_min < .99999 or loss of baseline target-paper Hit@5/10; diagnostic only'}
    if repeat:
        again,timing=encode(args.old)
        result['old_repeat'].update(throughput=timing,parity=compare_vectors(old,again))
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old',default='http://127.0.0.1:18006')
    parser.add_argument('--new',default='http://127.0.0.1:18007')
    parser.add_argument('--qdrant',default='http://127.0.0.1:6333')
    parser.add_argument('--paper',action='append')
    parser.add_argument('--fixture',type=Path)
    parser.add_argument('--save-fixture',type=Path)
    args=parser.parse_args(argv)
    print(json.dumps(benchmark(args),ensure_ascii=False,allow_nan=False,sort_keys=True))


if __name__=='__main__':main()
