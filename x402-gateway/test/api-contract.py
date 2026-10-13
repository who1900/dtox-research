"""Offline contract export: execute only real model AST, never import the API."""
import ast
import hashlib
import json
from pathlib import Path
import sys
from typing import List, Optional, Union

from pydantic import BaseModel, Field, ValidationError


root = Path(__file__).resolve().parents[2]
namespace = dict(BaseModel=BaseModel, Field=Field, List=List, Optional=Optional, Union=Union)
models = {}
handlers = {}
hashes = {}
selected = {"ResearchBundleBody", "ClaimSpec", "ValidateBody", "SignedJudgmentItem",
            "AdjudicateSignedBody", "VerdictMessageBody", "SearchBody"}
for relative in ("api/research_bundle.py", "api/main.py", "mcp/server.py"):
    source = (root / relative).read_bytes()
    hashes[relative] = hashlib.sha256(source).hexdigest()
    tree = ast.parse(source, filename=relative)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in selected:
            exec(compile(ast.Module(body=[node], type_ignores=[]), relative, "exec"), namespace)
            models[node.name] = namespace[node.name].model_json_schema()
        if isinstance(node, ast.FunctionDef):
            handlers[f"{relative}:{node.name}"] = {
                "parameters": [arg.arg for arg in node.args.args],
                "defaults": {arg.arg: ast.unparse(value) for arg, value in
                             zip(node.args.args[-len(node.args.defaults):], node.args.defaults)},
            }

results = []
for request in json.load(sys.stdin):
    try:
        parsed = namespace[request["model"]].model_validate(request["input"])
        results.append({"valid": True, "value": parsed.model_dump()})
    except ValidationError:
        results.append({"valid": False})
print(json.dumps({"models": models, "handlers": handlers, "sha256": hashes, "results": results}))
