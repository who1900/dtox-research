#!/usr/bin/env python3
"""Broad quality eval of dtox research through the eyes of an AI agent.

Pipeline: question generation -> agent (MCP tools via OpenAI /responses) -> judge.
Config from eval/.env (EVAL_API_KEY, EVAL_MODEL, EVAL_BASE_URL), loaded in-process.
The key is never printed, logged or written to the results file.

Usage:
  python eval/broad_eval.py --n 10 --tag pilot [--seed 1] [--concurrency 1] [--dry]
"""
import argparse
import json
import os
import random
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
MCP_URL = os.environ.get("DTOX_MCP_URL", "http://127.0.0.1:8011/mcp")
TOOL_CALL_TIMEOUT = 120
MAX_STEPS = 8
TOOL_RESULT_CAP = 12000
JUDGE_EXCERPT_CAP = 3500
JUDGE_TOTAL_CAP = 40000

QTYPES = [
    ("overview", 'topic overview, e.g. "key papers on X"'),
    ("mechanism", 'fact or mechanism, e.g. "how does X work"'),
    ("implementation", 'implementation detail, e.g. "formula/algorithm for X", how to code it'),
    ("trends", 'trends and ideas, e.g. "what is promising in X in 2026"'),
    ("validate", 'idea check, e.g. "has anyone built X"'),
    ("read_paper", "reading a specific named paper or spec (EIP, SIMD, whitepaper) and asking about its content"),
    ("compare", "comparison of two approaches or protocols"),
    ("edge_offtopic", "edge case: a question clearly outside the corpus scope (e.g. cooking, medicine, sports), but phrased like a developer question"),
    ("edge_typo_short", "edge case: a very short query (1-3 words) and/or with a typo in a key term"),
    ("edge_russian", "edge case: a real developer question written in Russian"),
]
TOPICS = (
    ["web3: Solana (runtime, SIMD, Firedancer, PoH, state)"] * 2
    + ["web3: Ethereum / EIP / ERC standards (4337, 4844, 7702, 1559)"] * 2
    + ["web3: DeFi (AMM, lending, stablecoins, oracles, liquidations)"] * 2
    + ["web3: MEV, PBS, order flow, sandwiching"]
    + ["web3: zero-knowledge proofs, rollups, zkVM, validity proofs"]
    + ["web3: consensus, finality, data availability, restaking"]
    + ["ai-agents: tool use, memory, planning, multi-agent, agent security"]
    + ["llm: small models, quantization, retrieval, long context, evaluation"]
    + ["builder-tech: smart contract security, fuzzing, formal verification, developer tooling"]
)


def load_env():
    p = HERE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- MCP client
class MCP:
    def __init__(self, url):
        self.url = url
        self.s = requests.Session()
        self.sid = None
        self.n = 0
        self.lock = threading.Lock()
        self.headers = {"content-type": "application/json",
                        "accept": "application/json, text/event-stream"}

    def _post(self, payload, timeout):
        h = dict(self.headers)
        if self.sid:
            h["mcp-session-id"] = self.sid
        r = self.s.post(self.url, json=payload, headers=h, timeout=timeout)
        if "mcp-session-id" in r.headers:
            self.sid = r.headers["mcp-session-id"]
        return r

    @staticmethod
    def _parse(r):
        if r.status_code >= 400:
            raise RuntimeError(f"MCP HTTP {r.status_code}: {r.text[:300]}")
        ct = r.headers.get("content-type", "")
        if "text/event-stream" in ct:
            last = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        last = json.loads(line[5:].strip())
                    except Exception:
                        pass
            if last is None:
                raise RuntimeError("MCP: empty SSE response")
            return last
        if not r.text.strip():
            return {}
        return r.json()

    def rpc(self, method, params=None, timeout=TOOL_CALL_TIMEOUT):
        with self.lock:
            self.n += 1
            i = self.n
        msg = {"jsonrpc": "2.0", "id": i, "method": method}
        if params is not None:
            msg["params"] = params
        return self._parse(self._post(msg, timeout))

    def init(self):
        res = self.rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "dtox-broad-eval", "version": "0.1"}}, 30)
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, 30)
        except Exception:
            pass
        return res.get("result", {})

    def tools(self):
        return self.rpc("tools/list", {}, 30).get("result", {}).get("tools", [])


def tool_result_text(resp):
    if "error" in resp:
        return json.dumps(resp["error"], ensure_ascii=False), True
    res = resp.get("result", {})
    parts = []
    for c in res.get("content", []) or []:
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
        else:
            parts.append(json.dumps(c, ensure_ascii=False))
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
    return "\n".join(parts), bool(res.get("isError"))


# ---------------------------------------------------------------- LLM client
class LLM:
    def __init__(self):
        self.base = os.environ["EVAL_BASE_URL"].rstrip("/")
        self.model = os.environ["EVAL_MODEL"]
        self._key = os.environ["EVAL_API_KEY"]
        self.usage = {"input": 0, "output": 0, "reasoning": 0, "calls": 0}
        self.lock = threading.Lock()

    def call(self, **body):
        body["model"] = self.model
        h = {"Authorization": "Bearer " + self._key, "content-type": "application/json"}
        last = None
        for attempt in range(4):
            try:
                r = requests.post(self.base + "/responses", headers=h, json=body, timeout=300)
            except requests.RequestException as e:
                last = f"network: {type(e).__name__}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                d = r.json()
                u = d.get("usage") or {}
                with self.lock:
                    self.usage["input"] += u.get("input_tokens", 0) or 0
                    self.usage["output"] += u.get("output_tokens", 0) or 0
                    self.usage["reasoning"] += ((u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)) or 0
                    self.usage["calls"] += 1
                d["_usage"] = u
                return d
            last = f"HTTP {r.status_code}: {r.text[:600]}"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            break
        raise RuntimeError(last)

    @staticmethod
    def text_of(d):
        out = []
        for it in d.get("output", []):
            if it.get("type") == "message":
                for c in it.get("content", []):
                    if c.get("type") in ("output_text", "text"):
                        out.append(c.get("text", ""))
        return "".join(out)


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        text = text[a:b + 1]
    return json.loads(text)


# ---------------------------------------------------------------- 1. questions
def gen_questions(llm, n, seed):
    rnd = random.Random(seed)
    types = [QTYPES[i % len(QTYPES)] for i in range(n)]
    if n <= len(QTYPES):
        types = rnd.sample(QTYPES, n) if n < len(QTYPES) else list(QTYPES)
    plan = []
    for t in types:
        topic = rnd.choice(TOPICS)
        if t[0] == "edge_offtopic":
            topic = "outside the corpus (not web3/AI/LLM/dev tools)"
        plan.append({"type": t[0], "type_desc": t[1], "topic": topic})
    qs = []
    for start in range(0, n, 10):
        chunk = plan[start:start + 10]
        prev = [q["question"] for q in qs]
        sys_p = ("You write realistic questions that a software developer or researcher would ask an AI "
                 "coding/research agent that has access to a full-text index of research papers, protocol "
                 "specs (EIPs, SIMDs) and whitepapers. Questions must be diverse, specific, and not generic. "
                 "Output strictly JSON.")
        user = (f"Write exactly {len(chunk)} questions, one per item, following this plan (type and topic area). "
                "For edge cases follow the type description literally. Avoid duplicating these earlier questions: "
                f"{json.dumps(prev, ensure_ascii=False)}\n\nPlan:\n"
                + json.dumps([{"i": i, **c} for i, c in enumerate(chunk)], ensure_ascii=False)
                + '\n\nReturn JSON: {"questions":[{"i":0,"question":"..."}, ...]}')
        d = llm.call(instructions=sys_p, input=user, text={"format": {"type": "json_object"}})
        js = parse_json(LLM.text_of(d))
        by_i = {x["i"]: x["question"] for x in js["questions"]}
        for i, c in enumerate(chunk):
            qs.append({"id": len(qs), "type": c["type"], "topic": c["topic"],
                       "question": by_i.get(i, "").strip()})
    return qs



# ---------------------------------------------------------------- citation check (deterministic)
ARXIV_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
PREFIX_RE = re.compile(r"\b(iacr|acl|pmlr|oa|eip|simd|wp|gh):([^\s`\"'<>()\[\],;|*]+)", re.I)


def norm_id(x):
    return x.strip().rstrip(".:/").lower()


def extract_ids(text):
    """All source ids mentioned in text: arXiv NNNN.NNNNN (also inside arxiv.org/abs urls) and prefixed ids."""
    ids = set()
    for m in ARXIV_RE.finditer(text or ""):
        ids.add(m.group(1))
    for m in PREFIX_RE.finditer(text or ""):
        ids.add(norm_id(m.group(1) + ":" + m.group(2)))
    return ids


def citation_check(answer, trace, question=""):
    """Compare ids cited in the final answer with ids actually present in tool results seen by the agent."""
    seen, complete = set(), True
    for t in trace:
        if t.get("seen_ids") is not None:
            seen.update(t["seen_ids"])
        else:  # legacy trace: only the truncated excerpt is stored
            seen.update(extract_ids(t.get("excerpt", "")))
            if t.get("resp_chars", 0) > len(t.get("excerpt", "")):
                complete = False
    seen.update(extract_ids(question))
    cited = extract_ids(answer)
    unseen = sorted(c for c in cited if c not in seen)
    return {"cited_total": len(cited), "cited_unseen": len(unseen), "unseen_ids": unseen,
            "seen_total": len(seen), "seen_complete": complete}


def refetch_seen(mcp, trace):
    """Legacy traces only: re-run the (read-only) tool calls to recover full result ids."""
    for t in trace:
        if t.get("seen_ids") is not None:
            continue
        try:
            text, _ = tool_result_text(mcp.rpc("tools/call", {"name": t["tool"], "arguments": t["args"]},
                                              TOOL_CALL_TIMEOUT))
            t["seen_ids"] = sorted(extract_ids(text[:TOOL_RESULT_CAP]))
            t["refetched"] = True
        except Exception:
            pass


# ---------------------------------------------------------------- 2. agent
def to_fn_tool(t):
    schema = t.get("inputSchema") or {"type": "object", "properties": {}}
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    return {"type": "function", "name": t["name"], "description": t.get("description", ""),
            "parameters": schema}


AGENT_SYS = ("You are a developer's research assistant. Answer the user's question using the provided tools "
             "(a research-paper index). Use tools as needed (at most a handful of calls), then give a concise, "
             "concrete final answer.\n"
             "Rules:\n"
             "1. Cite every claim that comes from the index with the paper/spec id (and url if available). Cite ONLY "
             "ids that appear in the tool results you received in this conversation. Never write an id from memory, "
             "never guess or complete an id.\n"
             "2. If the index does not contain the answer, or the question is outside its scope (research papers, "
             "protocol specs, whitepapers on web3, AI agents, LLMs, developer tooling), say so plainly and STOP: do "
             "not answer from your own knowledge. If you add useful background from your own knowledge anyway, keep "
             "it to a short separate part explicitly labelled \"not from the index\" with no citations.\n"
             "3. Do not fabricate ids, numbers or formulas. Use what the tools returned; do not ignore relevant "
             "sources they surfaced.\n\n"
             "Tool server instructions:\n")


def run_agent(llm, mcp, fn_tools, server_instr, q):
    trace, steps_in = [], None
    tok = {"input": 0, "output": 0}
    t0 = time.time()
    prev = None
    nxt = q["question"]
    final = ""
    err = None
    try:
        for step in range(MAX_STEPS + 1):
            body = dict(instructions=AGENT_SYS + (server_instr or ""), input=nxt, tools=fn_tools)
            if step == MAX_STEPS:
                body["tool_choice"] = "none"
            if prev:
                body["previous_response_id"] = prev
            d = llm.call(**body)
            u = d.get("_usage", {})
            tok["input"] += u.get("input_tokens", 0) or 0
            tok["output"] += u.get("output_tokens", 0) or 0
            prev = d["id"]
            calls = [it for it in d.get("output", []) if it.get("type") == "function_call"]
            if not calls:
                final = LLM.text_of(d)
                break
            outs = []
            for c in calls:
                name = c["name"]
                try:
                    args = json.loads(c.get("arguments") or "{}")
                except Exception:
                    args = {}
                ts = time.time()
                text, is_err, exc = "", False, None
                try:
                    resp = mcp.rpc("tools/call", {"name": name, "arguments": args}, TOOL_CALL_TIMEOUT)
                    text, is_err = tool_result_text(resp)
                except Exception as e:
                    exc = f"{type(e).__name__}: {str(e)[:300]}"
                    text, is_err = f"TOOL ERROR: {exc}", True
                dt = time.time() - ts
                trace.append({"step": step, "tool": name, "args": args, "seconds": round(dt, 2),
                              "resp_chars": len(text), "error": is_err, "exception": exc,
                              "excerpt": text[:JUDGE_EXCERPT_CAP]})
                send = text
                if len(send) > TOOL_RESULT_CAP:
                    send = send[:TOOL_RESULT_CAP] + f"\n[truncated: {len(text) - TOOL_RESULT_CAP} more chars]"
                outs.append({"type": "function_call_output", "call_id": c["call_id"], "output": send})
            nxt = outs
    except Exception as e:
        err = str(e)[:600]
    return {"answer": final, "trace": trace, "tokens": tok, "seconds": round(time.time() - t0, 1),
            "agent_error": err, "steps": len({t["step"] for t in trace})}


# ---------------------------------------------------------------- 3. judge
JUDGE_SYS = """You are a strict, skeptical reviewer of a research-paper search tool used by AI agents. You get the question, the agent's final answer, a DETERMINISTIC CITATION CHECK (ids cited in the answer vs ids that tools actually returned), and raw excerpts of tool results (each excerpt is truncated, so absence from an excerpt alone is not proof of absence; trust the citation check for id provenance). Separate two things:
- PRODUCT = the index and tools (did they surface the right, non-junk sources, fast, without errors).
- AGENT = the model using them (faithful use of results, honest scope handling, no invented ids).
Use your own expert knowledge to name canonical works an expert would expect that never appeared. Be concrete and use the FULL 0-2 range; do not default to 1.

SCORING ANCHORS
grounded (agent faithfulness to tool results):
 2 = every cited id is in tool results AND every specific claim/number/formula is traceable to excerpts; no outside knowledge presented as index content. Example: table cells each cite a returned paper and match its text.
 1 = ids are real but some claims are generalizations, unsupported details, or outside knowledge not labelled as such. Example: correct paper cited, but a number that is not in the excerpt.
 0 = invented ids, or main claims unsupported/contradicted by results, or an answer from own knowledge for an empty/out-of-scope result presented as sourced. Example: cites an arXiv id never returned.
relevance (PRODUCT: did the tools surface fitting sources for THIS question):
 2 = the top results are on-topic and include the core works or direct evidence. Example: query on 4337 returns the EIP text.
 1 = partly on-topic: adjacent or tangential papers, core work missing, or relevant material buried among junk.
 0 = mostly irrelevant, or an existing well-known work was not found, or tools errored. For an out-of-scope question: 2 if tools returned nothing/clearly weak matches and that was signalled, 0 if they returned confident junk.
completeness (PRODUCT: do the returned materials contain enough to fully answer; judge what was retrievable, not the agent's prose):
 2 = all parts of the question can be answered from returned results (for out-of-scope: correctly nothing to answer).
 1 = some parts answerable, others missing or only shallow snippets.
 0 = returned material cannot answer the question.
agent_use (AGENT: quality of use of what was returned):
 2 = used the best returned sources, right scope decision (declined out-of-index parts), searched adequately.
 1 = missed relevant returned sources, weak queries, or partly ignored scope limits.
 0 = ignored results, answered from own knowledge outside the index without labelling, or gave up despite available material.

ERROR LISTS (each item {"type": ..., "severity": "minor"|"major", "detail": short}):
agent_errors types: fabricated_id (cited id not in tool results; use the citation check), own_knowledge (answered from memory when index lacked it or question out of scope, unlabelled), ignored_sources (relevant returned sources not used), weak_queries, misread_source (claim contradicts excerpt), other.
product_errors types: missing_existing (an existing relevant work was not found), junk (irrelevant hits), duplicates, slow (call over ~10s), empty_filter (filters returned nothing where they should not), bad_ranking, tool_error, misleading_output, other.
Put an issue in exactly one list: who caused it.
OUT-OF-SCOPE RULE: if the question is outside the index scope or the index lacks the answer, a substantive answer built from the agent's own knowledge is an own_knowledge error of severity major and caps agent_use at 1 and grounded at 1, EVEN IF the agent admits the index does not cover it. Only a brief part explicitly labelled \"not from the index\" is acceptable (then minor at most).

Output strictly JSON with keys:
{"grounded": 0-2, "relevance": 0-2, "completeness": 0-2, "agent_use": 0-2,
 "canonical_missing": [strings; [] if none],
 "agent_errors": [...], "product_errors": [...],
 "scope_handling": "n/a" or "good" or "poor" plus short reason (out-of-scope, typo, short or non-English queries),
 "main_problem": one sentence, "summary": one sentence}"""


def _sev_count(items, sev="major"):
    return sum(1 for e in (items or []) if isinstance(e, dict) and e.get("severity") == sev)


def apply_scores(j, cc):
    """Deterministic post-processing: citation cap on grounded, product/agent aggregates."""
    j["citation_check"] = cc
    if not isinstance(j.get("grounded"), (int, float)):
        return j
    j["grounded_raw"] = j["grounded"]
    if cc["cited_unseen"] > 0:
        cap = 0 if cc["cited_total"] and cc["cited_unseen"] / cc["cited_total"] >= 0.5 else 1
        j["grounded"] = min(j["grounded"], cap)
        errs = j.setdefault("agent_errors", [])
        if not any(isinstance(e, dict) and e.get("type") == "fabricated_id" for e in errs):
            errs.append({"type": "fabricated_id", "severity": "major",
                         "detail": "cited ids not in tool results: " + ", ".join(cc["unseen_ids"][:8])})
    try:
        pe, ae = j.get("product_errors") or [], j.get("agent_errors") or []
        j["product_score"] = max(0.0, j["relevance"] + j["completeness"] - 0.5 * _sev_count(pe))
        j["agent_score"] = max(0.0, j["grounded"] + j.get("agent_use", 0) - 0.5 * _sev_count(ae))
    except Exception:
        pass
    return j


def judge(llm, q, res):
    cc = citation_check(res["answer"], res["trace"], q.get("question", ""))
    parts, total = [], 0
    for i, t in enumerate(res["trace"]):
        e = f"[call {i + 1}] {t['tool']}({json.dumps(t['args'], ensure_ascii=False)[:300]}) " \
            f"{t['seconds']}s err={t['error']} chars={t.get('resp_chars')}\n{t['excerpt']}"
        if total + len(e) > JUDGE_TOTAL_CAP:
            e = e[:max(0, JUDGE_TOTAL_CAP - total)]
        total += len(e)
        parts.append(e)
    cc_txt = (f"cited_total={cc['cited_total']} cited_unseen={cc['cited_unseen']} "
              f"unseen_ids={cc['unseen_ids']} seen_total={cc['seen_total']} "
              f"(id set complete={cc['seen_complete']})")
    user = (f"QUESTION TYPE: {q['type']}\nQUESTION: {q['question']}\n\nAGENT FINAL ANSWER:\n{res['answer'] or '(none)'}\n\n"
            f"CITATION CHECK (deterministic): {cc_txt}\n\n"
            f"TOOL CALLS AND RAW EXCERPTS:\n" + ("\n\n".join(parts) or "(no tool calls)")
            + "\n\nReturn the verdict as a JSON object.")
    d = llm.call(instructions=JUDGE_SYS, input=user, text={"format": {"type": "json_object"}})
    return apply_scores(parse_json(LLM.text_of(d)), cc), d.get("_usage", {})


# ---------------------------------------------------------------- main
def process(llm, mcp, fn_tools, instr, q):
    res = run_agent(llm, mcp, fn_tools, instr, q)
    try:
        j, ju = judge(llm, q, res)
        res["judge_tokens"] = {"input": ju.get("input_tokens", 0), "output": ju.get("output_tokens", 0)}
    except Exception as e:
        j = {"error": str(e)[:400]}
    q2 = dict(q)
    q2.update(res)
    q2["judge"] = j
    print(f"  [{q['id']}] {q['type']}: {q['question'][:70]!r} -> "
          f"{ {k: j.get(k) for k in ('grounded', 'relevance', 'completeness', 'product_score', 'agent_score')} } ({res['seconds']}s)", flush=True)
    return q2


def avg(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 2) if xs else None


METRICS = ("grounded", "relevance", "completeness", "agent_use", "product_score", "agent_score")


def dist(xs):
    out = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return dict(sorted(out.items()))


def finish(a, llm, results, t_start, out_path, mcp_url):
    wall = round(time.time() - t_start, 1)
    per_type = {}
    for r in results:
        per_type.setdefault(r["type"], []).append(r)
    summary = {
        "wall_seconds": wall,
        "tokens": llm.usage,
        "mean": {k: avg([r["judge"].get(k) for r in results]) for k in METRICS},
        "distribution": {k: dist([r["judge"].get(k) for r in results])
                         for k in ("grounded", "relevance", "completeness", "agent_use")},
        "citations": {"cited_total": sum((r["judge"].get("citation_check") or {}).get("cited_total", 0) for r in results),
                      "cited_unseen": sum((r["judge"].get("citation_check") or {}).get("cited_unseen", 0) for r in results),
                      "answers_with_unseen": sum(1 for r in results
                                                 if (r["judge"].get("citation_check") or {}).get("cited_unseen", 0) > 0)},
        "by_type": {t: {k: avg([r["judge"].get(k) for r in rs]) for k in METRICS}
                    for t, rs in per_type.items()},
        "tool_calls": sum(len(r["trace"]) for r in results),
        "tool_errors": sum(1 for r in results for t in r["trace"] if t["error"]),
        "tool_seconds_mean": avg([t["seconds"] for r in results for t in r["trace"]]),
        "tool_seconds_max": max([t["seconds"] for r in results for t in r["trace"]] or [0]),
    }
    out_path.write_text(json.dumps({"tag": a.tag, "model": llm.model, "seed": a.seed, "mcp": mcp_url,
                                    "summary": summary, "results": results},
                                   ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== SUMMARY ===")
    print("mean:", summary["mean"])
    for t, v in summary["by_type"].items():
        print(f"  {t:16s} {v}")
    print(f"tool calls {summary['tool_calls']}, errors {summary['tool_errors']}, "
          f"mean {summary['tool_seconds_mean']}s, max {summary['tool_seconds_max']}s")
    print("distribution:", summary["distribution"])
    print("citations:", summary["citations"])
    for label, key in (("product errors", "product_errors"), ("agent errors", "agent_errors")):
        cnt = {}
        for r in results:
            for e in (r["judge"].get(key) or []):
                k = (e.get("type"), e.get("severity")) if isinstance(e, dict) else (str(e)[:60], None)
                cnt[k] = cnt.get(k, 0) + 1
        print(f"{label}:", {f"{k[0]}/{k[1]}": v for k, v in sorted(cnt.items(), key=lambda x: -x[1])})
    print(f"wall {wall}s, tokens {llm.usage}")
    print("saved", out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--mcp", default=MCP_URL)
    ap.add_argument("--refetch", action="store_true",
                    help="with --rejudge: re-run read-only tool calls of legacy traces to recover full id sets")
    ap.add_argument("--questions-from", help="reuse the questions of this results file instead of generating")
    ap.add_argument("--rejudge", help="reuse agent traces from this results file, only re-run the judge")
    a = ap.parse_args()

    load_env()
    for k in ("EVAL_API_KEY", "EVAL_MODEL", "EVAL_BASE_URL"):
        if not os.environ.get(k):
            sys.exit(f"missing {k}")
    llm = LLM()
    t_start = time.time()
    out_dir = HERE / "runs"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"broad-{a.tag}.json"

    print(f"model={llm.model} n={a.n} seed={a.seed}", flush=True)
    if a.rejudge:
        old = json.loads(Path(a.rejudge).read_text(encoding="utf-8"))["results"]
        rj_mcp = None
        if a.refetch:
            rj_mcp = MCP(a.mcp)
            rj_mcp.init()

        def rj(r):
            r["judge_prev"] = r.get("judge")
            try:
                if rj_mcp:
                    refetch_seen(rj_mcp, r["trace"])
                r["judge"], ju = judge(llm, r, r)
            except Exception as e:
                r["judge"] = {"error": str(e)[:400]}
            return r
        with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            results = list(ex.map(rj, old))
        finish(a, llm, results, t_start, out_path, a.rejudge)
        return
    if a.questions_from:
        # same questions as an earlier run, so two builds are compared on equal terms
        src = json.loads(Path(a.questions_from).read_text(encoding="utf-8"))
        qs = src.get("questions") or [{k: r[k] for k in ("id", "type", "topic", "question") if k in r}
                                      for r in src.get("results", [])]
    else:
        try:
            qs = gen_questions(llm, a.n, a.seed)
        except Exception as e:
            sys.exit(f"question generation failed: {e}")
    for q in qs:
        print(f"  Q{q['id']} [{q['type']}] {q['question']}")
    if a.dry:
        out_path.write_text(json.dumps({"tag": a.tag, "model": llm.model, "questions": qs, "usage": llm.usage},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
        print("dry run, saved", out_path)
        return

    mcp = MCP(a.mcp)
    init = mcp.init()
    instr = init.get("instructions", "")
    tools = mcp.tools()
    fn_tools = [to_fn_tool(t) for t in tools]
    print(f"MCP tools: {[t['name'] for t in tools]}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
        results = list(ex.map(lambda q: process(llm, mcp, fn_tools, instr, q), qs))

    finish(a, llm, results, t_start, out_path, a.mcp)

if __name__ == "__main__":
    main()
