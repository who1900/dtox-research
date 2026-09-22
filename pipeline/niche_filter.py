#!/usr/bin/env python3
"""
Deterministic lexical niche gate for dtox-research.

Replaces the failed semantic (cosine) niche_gate.py. Word-boundary regex
matching on title (+ abstract when available), case-insensitive. No
embeddings, no network calls, no threshold tuning. A paper passes iff at
least one bucket's terms match; the matched buckets become its `layers`.

IMPORTANT: matching is word-boundary based (\\b...\\b), not raw substring.
Raw substring on a short token like "rag" would match inside "average",
"storage", "fragment", "paragraph" etc. - verified empirically while
building this gate, so all short/ambiguous tokens ("rag", "llm", "slm",
"moe", "mev", ...) are regex-anchored at word boundaries.

Deliberately excludes over-broad terms ("transformer", "deep learning",
"neural network", bare "agent", bare "multi-agent") because those are
exactly what let speech/vision/tabular/graph/MARL papers leak into
cs.CL/cs.LG/cs.AI/cs.MA harvests. "multi-agent" specifically was verified
to pull in classic MARL/RL papers unrelated to LLMs (e.g. "Learning Reward
Machines in Cooperative Multi-Agent Tasks"), so it is only accepted here
combined with an LLM/language-model signal in the same phrase.

--- Relevance scoring (added on top of the binary gate) ---------------------

Terms are now split into two tiers per layer:
  * BROAD terms (weight 1)  - the original coarse gate terms above.
  * SPECIFIC terms (weight 5) - narrow technical markers that indicate the
    paper actually has real, on-topic technical content (specific systems,
    algorithms, benchmarks, protocols) rather than a passing mention.

niche_score(text) sums the weights of all *unique* terms matched per layer
and also returns which terms fired, so downstream code can persist a
relevance score and facet on the terms themselves. The binary pass/fail
behavior (off_niche iff no layer scores > 0) is unchanged - this is purely
additive on top of niche_match().
"""

import re

# Original coarse-gate terms, weight 1. Unchanged from before.
BROAD_TERMS = {
    "llm-slm": [
        "language model", "llm", "slm", "large language model", "small language model",
        "rag", "retrieval-augmented", "retrieval augmented", "in-context learning",
        "instruction tuning", "chain-of-thought", "chain of thought", "mixture of experts",
        "moe", "prompt compression", "prompt tuning", "context window", "long context",
        "gpt", "chatgpt", "llama", "mistral", "qwen", "perplexity language",
        "text generation model", "decoder-only", "encoder-decoder language",
    ],
    "ai-agents": [
        "llm agent", "language model agent", "llm-based agent", "tool use", "tool-use",
        "function calling", "agentic", "autonomous agent", "react agent",
        "reasoning and acting", "web agent", "browser agent", "agent memory",
        "agent planning", "tool-augmented",
        # "multi-agent" alone matches classic MARL/RL papers unrelated to LLMs
        # (verified empirically), so require it to co-occur with an
        # LLM/language-model signal in the phrase itself.
        "multi-agent llm", "multi-agent language model", "multi-agent large language model",
        "llm-based multi-agent", "llm multi-agent", "multi-llm-agent", "multi agent llm",
    ],
    "web3": [
        "blockchain", "defi", "decentralized finance", "smart contract", "solana",
        "ethereum", "zero-knowledge", "zero knowledge", "zk-proof", "zk proof",
        "zksnark", "zk-rollup", "rollup", "layer-2 blockchain", "dao governance",
        "mev", "maximal extractable", "cryptocurrency", "on-chain", "web3",
        "tokenomics", "nft market", "staking protocol", "consensus protocol",
    ],
    "builder-tech": [
        "mobile application", "mobile app", "mobile wallet", "wallet application",
        "rust programming language", "software engineering", "software testing",
        "program analysis", "distributed system", "developer tooling", "programming language",
        "language runtime", "smart contract language", "mobile development",
    ],
}

# Narrow technical markers, weight 5. Presence of any of these is a strong
# signal the paper has substantive on-topic content, not just a passing
# mention of the broad niche.
SPECIFIC_TERMS = {
    "llm-slm": [
        "kv cache", "paged attention", "vllm", "flash attention", "speculative decoding",
        "medusa", "eagle", "gptq", "awq", "gguf", "smoothquant", "lora", "qlora", "dora",
        "rope scaling", "yarn", "grouped-query attention", "gqa", "mqa",
        "expert parallelism", "continuous batching", "prefix caching",
        "constrained decoding", "dpo", "grpo", "orpo", "kto", "rlaif", "sparsegpt",
        "wanda", "mamba", "state space model", "rwkv", "linear attention",
        "test-time compute", "chain-of-thought", "self-consistency", "hyde", "colbert",
        "late interaction", "bm25", "reranking", "instruction tuning", "in-context learning",
        "mixture of experts", "prompt compression", "long context", "quantization",
    ],
    "ai-agents": [
        "react agent", "reflexion", "toolformer", "function calling",
        "model context protocol", "mcp server", "computer use", "webarena",
        "swe-bench", "gaia benchmark", "agentbench", "tool retrieval",
        "episodic memory", "planner-executor", "multi-agent debate",
        "agent orchestration", "guardrails", "prompt injection", "tool-augmented",
        "autonomous agent",
    ],
    "web3": [
        # applied cryptography that IACR ePrint publishes and blockchains
        # actually run on; without these the gate rejects the ZK/consensus
        # papers that are the whole reason for harvesting IACR, while still
        # (correctly) dropping block ciphers, isogenies and side-channel work
        "snark", "zk-snark", "stark", "zk-stark", "bulletproofs", "halo2",
        "recursive proof", "proof aggregation", "trusted setup",
        "polynomial commitment", "vector commitment", "merkle proof", "merkle tree",
        "threshold signature", "distributed key generation", "verifiable delay function",
        "byzantine fault", "byzantine agreement", "proof of stake", "proof of work",
        "multi-party computation", "secure multiparty computation",
        "payment channel", "state channel", "atomic swap", "ring signature",
        "stealth address", "confidential transaction", "verifiable random function",
        "durable nonce", "x402", "anchor framework", "sealevel", "firedancer",
        "agave validator", "priority fees", "compute units", "jito",
        "proposer-builder separation", "mev-boost", "account abstraction", "erc-4337",
        "erc-7683", "eip-4844", "blob transaction", "danksharding",
        "data availability sampling", "validium", "zkvm", "risc zero", "sp1", "groth16",
        "plonk", "kzg commitment", "verkle", "light client", "ibc protocol", "wormhole",
        "layerzero", "pyth", "hermes", "chainlink", "twap oracle",
        "concentrated liquidity", "jit liquidity", "sandwich attack", "funding rate",
        "restaking", "eigenlayer", "slashing", "distributed validator", "zk-rollup",
        "optimistic rollup", "stablecoin", "liquid staking",
    ],
    "builder-tech": [
        "rust", "c++", "c#", "golang", "go language", "typescript", "javascript",
        "python", "java", "kotlin", "swift", "dart", "objective-c", "scala", "ruby",
        "php", "lua", "elixir", "erlang", "haskell", "ocaml", "f#", "zig", "nim",
        "flutter", "react native", "html", "css", "web frontend", "web application",
        "node.js", "nodejs", "next.js", "vue.js", "angular framework", "svelte",
        "webassembly", "wasm", "android application", "ios application", "walletconnect",
        "solidity", "vyper", "yul", "huff language", "fe language", "cairo language",
        "move language", "move smart contract", "sui move", "aptos move", "sway language",
        "func language", "tact language", "cadence language", "clarity language",
        "michelson language", "plutus", "reach language", "leo language", "noir language",
        "circom", "circuit language",
        "hardware wallet", "secure element", "secure enclave", "passkey", "webauthn",
        "static analysis", "symbolic execution", "fuzzing", "property-based testing",
        "formal verification", "model checking", "compiler optimization",
        "language server protocol", "software supply chain", "dependency resolution",
        "reproducible build", "distributed systems", "database systems",
    ],
}

BROAD_WEIGHT = 1
SPECIFIC_WEIGHT = 5


def _compile_group(terms_dict):
    compiled = {}
    for layer, terms in terms_dict.items():
        entries = []
        for term in terms:
            # \b works fine around internal hyphens/spaces; term is escaped
            # so literal regex metachars in any future term stay literal.
            # Trailing "s?" tolerates plurals ("LLM agent" vs "LLM agents",
            # "smart contract" vs "smart contracts") without new false
            # positives (verified: "rags"/"gpts"/"moes" etc. are harmless).
            # Separators are written inconsistently in practice
            # ("mixture of experts" / "Mixture-of-Experts" / "mixture_of_experts"),
            # so any space or hyphen in a term matches any of them. Without
            # this the canonical MoE paper, titled with hyphens, was missed.
            pattern = r"[\s\-_]+".join(re.escape(part) for part in re.split(r"[\s\-]+", term))
            # \b cannot close after punctuation, so it silently misses C++ and
            # C#.  Non-word lookarounds preserve the old anti-substring safety
            # for terms such as rag while also matching language names.
            entries.append((term, re.compile(r"(?<!\w)" + pattern + r"s?(?!\w)", re.IGNORECASE)))
        compiled[layer] = entries
    return compiled


_COMPILED_BROAD = _compile_group(BROAD_TERMS)
_COMPILED_SPECIFIC = _compile_group(SPECIFIC_TERMS)

# Backward-compat: original flat NICHE_TERMS name, in case anything else
# imports it directly.
NICHE_TERMS = BROAD_TERMS
_COMPILED_TERMS = _COMPILED_BROAD


def niche_score(text):
    """Return {layer: {"score": int, "matched_terms": [term, ...]}} for all
    three niches. score = sum of weights of unique matched terms
    (BROAD_WEIGHT=1, SPECIFIC_WEIGHT=5). matched_terms lists the unique
    terms that fired, broad and specific combined (broad terms checked
    before specific terms per layer)."""
    result = {layer: {"score": 0, "matched_terms": []} for layer in BROAD_TERMS}
    if not text:
        return result
    for layer in BROAD_TERMS:
        score = 0
        matched = []
        for term, pat in _COMPILED_BROAD.get(layer, []):
            if pat.search(text):
                score += BROAD_WEIGHT
                matched.append(term)
        for term, pat in _COMPILED_SPECIFIC.get(layer, []):
            if pat.search(text):
                score += SPECIFIC_WEIGHT
                matched.append(term)
        result[layer]["score"] = score
        result[layer]["matched_terms"] = matched
    return result


# A single broad term is not evidence of topic: "language model" shows up in
# any NLP paper of the last decade, "blockchain" in edge-computing auctions.
# Sampling the harvested base, every score-1 paper was noise of that kind
# (MoE for CNNs, vision-language reward models for RL, RNN loss smoothing),
# while score-2 papers were already on topic. Two broad terms, or one
# specific term, is the cheapest honest signal.
MIN_NICHE_SCORE = 2

# Terms differ in how much they actually say about a paper, so the bar is
# per layer. "language model" appears in a decade of NLP work, so llm-slm and
# ai-agents need corroboration; "blockchain" or "threshold signature" is
# already rare and on-topic, and holding web3 to the same bar dropped IACR
# acceptance from 31% to 9% -- starving exactly the layer we harvest IACR for.
MIN_SCORE_BY_LAYER = {
    "llm-slm": 2,
    "ai-agents": 2,
    "web3": 1,
    "builder-tech": 2,
}


# Some terms mean different things in different fields: a control-theory paper
# proposing a "consensus protocol" for linear multi-agent systems is not
# blockchain research, yet it matched web3 and outranked the Curve whitepaper.
# A layer is refused when its evidence is only the ambiguous term and the text
# carries the other field's markers instead.
NEGATIVE_GUARDS = {
    "web3": {
        "ambiguous": ("consensus protocol", "consensus"),
        "foreign": re.compile(
            r"\b(multi-?agent system|linear system|control (?:law|theory|input)|"
            r"leader-follower|formation control|event-triggered|lyapunov|"
            r"receding horizon|flocking|synchroniz)", re.IGNORECASE),
        "anchors": re.compile(
            r"\b(blockchain|distributed ledger|byzantine|proof.of.(?:work|stake)|"
            r"smart contract|cryptocurrenc|bitcoin|ethereum|validator node|mining)",
            re.IGNORECASE),
    },
}


def _passes_negative_guard(layer, text, matched_terms):
    guard = NEGATIVE_GUARDS.get(layer)
    if not guard:
        return True
    informative = [t for t in matched_terms if t not in guard["ambiguous"]]
    if informative:
        return True  # evidence beyond the ambiguous term
    if guard["foreign"].search(text) and not guard["anchors"].search(text):
        return False
    return True


def niche_match(text, min_score=None):
    """Return sorted list of layer names whose terms appear in `text`
    (case-insensitive, word-boundary match). Empty list = off-niche.

    A layer matches when its combined term score reaches that layer's
    threshold; pass min_score to override for every layer (min_score=1
    restores the old "any single term" behavior)."""
    if not text:
        return []
    scores = niche_score(text)
    out = []
    for layer, d in scores.items():
        bar = min_score if min_score is not None else MIN_SCORE_BY_LAYER.get(layer, MIN_NICHE_SCORE)
        if d["score"] >= bar and _passes_negative_guard(layer, text, d.get("matched_terms", [])):
            out.append(layer)
    return sorted(out)


def primary_layer(scores):
    """Given the dict returned by niche_score(), return (layer_name, score)
    for the highest-scoring layer, or (None, 0) if all layers scored 0."""
    best_layer, best_score = None, 0
    for layer, d in scores.items():
        if d["score"] > best_score:
            best_layer, best_score = layer, d["score"]
    return best_layer, best_score


def all_matched_terms(scores):
    """Flatten unique matched terms across all layers, order-stable."""
    seen = []
    for layer in sorted(scores):
        for term in scores[layer]["matched_terms"]:
            if term not in seen:
                seen.append(term)
    return seen


if __name__ == "__main__":
    import sys
    for line in sys.argv[1:] or [
        "Speaker Verification with Deep Neural Networks",
        "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
        "Tensor-Train Decomposition for Deep Learning Compression",
        "LLM Agents that Browse the Web Autonomously",
        "MEV Extraction in Automated Market Makers on Ethereum",
        "Iris Recognition using Convolutional Neural Networks",
        "Learning Reward Machines in Cooperative Multi-Agent Tasks",
        "Average Storage Fragmentation in Paragraph Retrieval Systems",  # rag-substring trap
        "A Survey of Multi-Agent LLM Frameworks",
        "vLLM: Easy, Fast, and Cheap LLM Serving with PagedAttention and KV Cache",
        "MEV-Boost and Proposer-Builder Separation in Ethereum Block Building",
    ]:
        scores = niche_score(line)
        matched = niche_match(line)
        pl, ps = primary_layer(scores)
        print(matched, f"primary={pl}({ps})", "|", line)
