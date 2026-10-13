"""Read-only full Qdrant/FTS identity reconciliation, with a compact seen bitset."""
import json
from pathlib import Path
import sqlite3
import time
import urllib.request

BASE=Path('/opt/dtox-corpus-quality-20261013')
def request(path,body=None):
    req=urllib.request.Request('http://127.0.0.1:6333'+path,
        data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=120) as r:answer=json.load(r)
    if answer['status']!='ok':raise RuntimeError('Qdrant returned non-ok status')
    return answer['result']

def main():
    start=time.time();rows=sqlite3.connect((BASE/'fts-rowmap.sqlite').as_uri()+'?mode=ro',uri=True)
    if dict(rows.execute('SELECT key,value FROM meta')).get('complete')!='1':raise RuntimeError('incomplete frozen map')
    ceiling=rows.execute('SELECT MAX(rowid) FROM rows').fetchone()[0]
    seen=bytearray((ceiling+8)//8)
    output=sqlite3.connect(BASE/'identity-coverage.sqlite')
    output.executescript('CREATE TABLE IF NOT EXISTS anomalies(kind TEXT,point_id TEXT,arxiv_id TEXT,chunk_index INTEGER,fts_rowid INTEGER);DELETE FROM anomalies;')
    initial=request('/collections/papers_fulltext')['points_count']
    total=0;offset=None
    while True:
        body={'limit':4096,'with_payload':False,'with_vector':False}
        if offset is not None:body['offset']=offset
        page=request('/collections/papers_fulltext/points/scroll',body)
        pts=page['points']
        for pos in range(0,len(pts),800):
            part=pts[pos:pos+800];by_id={str(p['id']):p for p in part}
            found=set()
            for rid,pid,aid in rows.execute('SELECT rowid,point_id,arxiv_id FROM rows WHERE point_id IN('+','.join('?' for _ in by_id)+')',list(by_id)):
                found.add(pid);seen[rid//8]|=1<<(rid%8)
            extra=set(by_id)-found
            missing_payloads=request('/collections/papers_fulltext/points',{'ids':list(extra),'with_payload':['arxiv_id','chunk_index'],'with_vector':False}) if extra else []
            for p in missing_payloads:
                pid=str(p['id'])
                output.execute('INSERT INTO anomalies VALUES(?,?,?,?,NULL)',('qdrant_without_fts',pid,p['payload'].get('arxiv_id'),p['payload'].get('chunk_index')))
        total+=len(pts);offset=page.get('next_page_offset')
        output.commit()
        if total//100000!=(total-len(pts))//100000:print(json.dumps({'phase':'fulltext_ids','scanned':total}),flush=True)
        if offset is None:break
    final=request('/collections/papers_fulltext')['points_count']
    if initial!=final or total!=final:raise RuntimeError('Qdrant identity universe changed during scan')
    for rid,pid,aid in rows.execute('SELECT rowid,point_id,arxiv_id FROM rows'):
        if not seen[rid//8]&(1<<(rid%8)):
            output.execute('INSERT INTO anomalies VALUES(?,?,?,?,?)',('fts_without_qdrant',pid,aid,None,rid))
    output.commit()
    state=sqlite3.connect(Path('/opt/dtox-research/state.db').resolve().as_uri()+'?mode=ro',uri=True)
    done={r[0] for r in state.execute("SELECT arxiv_id FROM papers WHERE status='done'")}
    coarse=set();offset=None
    while True:
        body={'limit':2048,'with_payload':['arxiv_id'],'with_vector':False}
        if offset is not None:body['offset']=offset
        page=request('/collections/papers_coarse_base/points/scroll',body)
        for p in page['points']:coarse.add(p['payload'].get('arxiv_id'))
        offset=page.get('next_page_offset')
        if offset is None:break
    report={'complete':True,'fulltext_qdrant_points':total,'fts_rows':rows.execute('SELECT COUNT(*) FROM rows').fetchone()[0],
        'anomalies':dict(output.execute('SELECT kind,COUNT(*) FROM anomalies GROUP BY kind')),
        'coarse_paper_ids':len(coarse),'done_paper_ids':len(done),'done_without_coarse':sorted(done-coarse),
        'coarse_without_done':sorted(coarse-done,key=str),'elapsed_seconds':round(time.time()-start,3),'corpus_writes':False,
        'fulltext_check_scope':'all point IDs; full payload/text/vector equivalence not globally checked'}
    (BASE/'identity-coverage.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)

if __name__=='__main__':main()
