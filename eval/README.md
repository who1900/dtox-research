# Retrieval evaluation

The tools in this directory evaluate retrieval channels, candidate ranking and source coverage. Versioned query fixtures and unit tests are kept with the code; generated run outputs belong in local `eval/results/`.

Use each script's `--help` for its inputs and endpoint configuration. Live probes target the explicitly configured API or MCP service. Keep access credentials in the environment and retain the source passages used to assess each result.

Offline evaluator tests:

```bash
python -B -m unittest discover -s eval/tests -p 'test_*.py'
```
