"""Linux offline Docling model initialization only; requires the outer sandbox."""
import argparse
import json
import os
from pathlib import Path
import resource
import time
import traceback

import extraction_benchmark_backend as backend


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    backend.require(output.is_absolute() and output.name not in ("", ".", ".."),
                    "absolute output file required")
    parent = backend.local_path(str(output.parent), directory=True)
    output = parent / output.name
    with output.open("x", encoding="utf-8", newline="") as stream:
        started, cpu = time.monotonic(), time.process_time()
        result = {"schema": "dtox-extraction-model-preflight-1", "status": "ERROR",
                  "pdfs_processed": 0, "quality_measured": False, "execution_identity": None,
                  "config_sha256": None, "errors": [],
                  "metrics_scope": "Linux self process CPU and peak RSS; not concurrent process-tree totals"}
        try:
            os.chdir(parent)
            backend.configure_offline(parent)
            result["execution_identity"] = backend.execution_identity()
            config_path = backend.local_path(args.config)
            raw = config_path.read_bytes()
            result["config_sha256"] = backend.hashlib.sha256(raw).hexdigest()
            config = json.loads(raw)
            backend.require(config.get("schema") == backend.SCHEMA and config.get("backend") == "docling",
                            "Docling backend config required")
            backend.require(type(config.get("execution_identity_uid")) is int
                            and config["execution_identity_uid"] == 65534, "explicit execution_identity_uid=65534 required")
            backend.verify_execution_identity(config, result["execution_identity"])
            result["packages"] = backend.verify_versions(config)
            converter = backend.make_docling_converter(config)
            from docling.datamodel.base_models import InputFormat
            converter.initialize_pipeline(InputFormat.PDF)
            result["status"] = "OK"
        except BaseException as error:
            result["errors"].append({"type": type(error).__name__, "message": str(error),
                                     "traceback": traceback.format_exc()})
        finally:
            result.update(elapsed_seconds=time.monotonic() - started,
                          cpu_seconds=time.process_time() - cpu,
                          peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
