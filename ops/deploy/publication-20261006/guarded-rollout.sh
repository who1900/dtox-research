#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../../.." && pwd)"
MANIFEST="$HERE/manifest.json"
BACKUP_ROOT=/opt/dtox-release-20261006
API_DIR=/opt/dtox-research-api
PIPE_DIR=/opt/dtox-research
API_SERVICE=dtox-research-api
WRITER_SERVICE=dtox-research
HEALTH_URL=http://127.0.0.1:8010/v1/health
die(){ echo "BLOCKED: $*" >&2; exit 1; }
sha(){ sha256sum -- "$1" | awk '{print $1}'; }
stage(){
  local out="${1:?usage: guarded-rollout.sh stage OUT.tar.gz}" tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/dtox-rollout.XXXXXXXX")"
  mkdir -p "$tmp/payload"/pipeline/tests "$tmp/payload"/api/tests "$tmp/payload/ops/deploy/publication-20261006"
  python3 - "$MANIFEST" "$ROOT" "$tmp/payload" <<'PY'
import json, pathlib, shutil, sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text()); root=pathlib.Path(sys.argv[2]); out=pathlib.Path(sys.argv[3])
paths=[x['source'] for x in m['files']]+m['tests']+['api/'+x for x in m['api_dependencies']]
for rel in paths:
 p=root/rel
 if not p.is_file(): raise SystemExit('missing package input: '+rel)
 d=out/rel; d.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(p,d)
for rel in ('manifest.json','guarded-rollout.sh'):
 p=pathlib.Path(sys.argv[1]).parent/rel; shutil.copyfile(p,out/'ops/deploy/publication-20261006'/rel)
PY
  tar -C "$tmp/payload" -czf "$out" .
  echo "Staged $out; private working files retained at $tmp (no deploy performed)."
}
load_bundle(){
  local archive="${1:?bundle required}" dest="$2"
  [[ -f "$archive" ]] || die "bundle not found"
  mkdir -p "$dest"
  tar -tzf "$archive" | awk 'BEGIN{ok=1} /^\// || /(^|\/)\.\.($|\/)/ {ok=0} END{exit !ok}' || die "unsafe archive paths"
  tar -xzf "$archive" -C "$dest"
  MANIFEST="$dest/ops/deploy/publication-20261006/manifest.json"
  [[ -f "$MANIFEST" ]] || die "manifest missing"
}
verify_candidate(){
  python3 - "$MANIFEST" "$1" <<'PY'
import hashlib,json,pathlib,sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text()); root=pathlib.Path(sys.argv[2])
for f in m['files']:
 p=root/f['source']; got=hashlib.sha256(p.read_bytes()).hexdigest()
 if got != f['candidate']: raise SystemExit('candidate hash mismatch: '+f['source'])
PY
}
preflight(){
  [[ $EUID -eq 0 ]] || die "run as root on target host"
  command -v systemctl >/dev/null && command -v curl >/dev/null && command -v python3 >/dev/null || die "missing system tools"
  python3 - "$MANIFEST" <<'PY'
import json,pathlib,sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text())
for f in m['files']:
 p=pathlib.Path(f['target'])
 if f['baseline']=='ABSENT':
  if p.exists(): raise SystemExit('expected absent; refusing overwrite: '+str(p))
 else:
  if not p.is_file(): raise SystemExit('baseline target missing: '+str(p))
  import hashlib
  if hashlib.sha256(p.read_bytes()).hexdigest()!=f['baseline']: raise SystemExit('baseline hash mismatch: '+str(p))
for name in m['api_dependencies']:
 if not (pathlib.Path('/opt/dtox-research-api')/name).is_file(): raise SystemExit('API dependency absent; not in whitelist: '+name)
PY
  systemctl is-active --quiet "$API_SERVICE" || die "API is not active"
  systemctl is-active --quiet "$WRITER_SERVICE" || die "writer is not active"
}
deploy(){
  local bundle="${1:?usage: guarded-rollout.sh deploy BUNDLE.tar.gz}" work stamp backup
  work="$(mktemp -d /tmp/dtox-deploy.XXXXXXXX)"
  echo "Private extracted candidate retained at $work"
  load_bundle "$bundle" "$work"; verify_candidate "$work"; preflight
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"; backup="$BACKUP_ROOT/$stamp"
  [[ ! -e "$backup" ]] || die "backup path exists"
  install -d -m 0700 "$backup/pipeline" "$backup/api"
  python3 - "$MANIFEST" "$backup" <<'PY'
import json,pathlib,shutil,sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text()); b=pathlib.Path(sys.argv[2])
for f in m['files']:
 p=pathlib.Path(f['target'])
 if p.exists():
  dest=b/('pipeline' if f['target'].startswith('/opt/dtox-research/') else 'api')/p.name
  shutil.copy2(p,dest)
PY
  # Recheck every baseline after backup and immediately before the first write.
  preflight
  systemctl stop "$WRITER_SERVICE"
  # Keep the writer stopped while the API gate is installed and proven healthy.
  install -m 0644 "$work/api/main.py" "$API_DIR/main.py"
  install -m 0644 "$work/api/evidence.py" "$API_DIR/evidence.py"
  systemctl restart "$API_SERVICE"
  healthy=0
  for _ in {1..10}; do
    if curl --max-time 1 --fail --silent "$HEALTH_URL" >/dev/null; then healthy=1; break; fi
    sleep 1
  done
  [[ $healthy -eq 1 ]] || die "API health failed within ~20s; writer remains STOPPED; do not restore old reader over possibly marked data"
  install -m 0644 "$work/pipeline/service.py" "$PIPE_DIR/service.py"
  install -m 0644 "$work/pipeline/publication.py" "$PIPE_DIR/publication.py"
  install -m 0644 "$work/pipeline/fts_catalog.py" "$PIPE_DIR/fts_catalog.py"
  install -m 0644 "$work/pipeline/build_fts.py" "$PIPE_DIR/build_fts.py"
  systemctl start "$WRITER_SERVICE"
  echo "Rollout complete. Backup: $backup"
}
rollback(){
  local backup="${1:?usage: guarded-rollout.sh rollback BACKUP_DIR}"
  [[ $EUID -eq 0 ]] || die "run as root"
  [[ "$backup" == "$BACKUP_ROOT/"* && -d "$backup" ]] || die "backup must be under $BACKUP_ROOT"
  systemctl stop "$WRITER_SERVICE"
  # Deliberately preserve API visibility gate and publication journal/module;
  # no attempt to infer marked-data presence or erase publication state.
  for f in service.py build_fts.py; do
    [[ -f "$backup/pipeline/$f" ]] || die "backup missing $f"
    install -m 0644 "$backup/pipeline/$f" "$PIPE_DIR/$f"
  done
  systemctl restart "$API_SERVICE"
  for _ in {1..30}; do curl --fail --silent "$HEALTH_URL" >/dev/null && break; sleep 2; done
  curl --fail --silent "$HEALTH_URL" >/dev/null || die "API health failed; writer stays stopped"
  echo "Writer remains stopped. Publication code, gate and journal preserved for manual reconciliation."
}
case "${1:-}" in
  stage) shift; stage "$@" ;;
  deploy) shift; deploy "$@" ;;
  rollback) shift; rollback "$@" ;;
  *) die "usage: $0 {stage OUT.tar.gz|deploy BUNDLE.tar.gz|rollback BACKUP_DIR}" ;;
esac
