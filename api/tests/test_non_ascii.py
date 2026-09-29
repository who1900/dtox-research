import asyncio
import importlib.util
import json
import pathlib
import unittest

from api.search_core import fts_query, fts_query_any

_spec = importlib.util.spec_from_file_location(
    "utf8_guard", pathlib.Path(__file__).resolve().parents[2] / "mcp" / "utf8_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class FtsNonAsciiTests(unittest.TestCase):
    def test_mixed_keeps_latin_terms(self):
        self.assertEqual(fts_query_any("MEV защита"), '"MEV"')
        self.assertEqual(fts_query("MEV защита"), '"MEV"')

    def test_pure_cyrillic_and_cjk_give_empty_query(self):
        for q in ("защита от фронтраннинга", "MEV防护"[3:], "防护", "🙂", ""):
            self.assertEqual(fts_query_any(q), "")
            self.assertEqual(fts_query(q), "")

    def test_cjk_next_to_latin(self):
        self.assertEqual(fts_query_any("MEV 防护 protection"), '"MEV" OR "protection"')


def run_guard(body, method="POST"):
    seen, out = {}, []

    async def app(scope, receive, send):
        m = await receive()
        seen["body"] = m["body"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def go():
        msgs = [{"type": "http.request", "body": body[:5], "more_body": True},
                {"type": "http.request", "body": body[5:], "more_body": False}]

        async def receive():
            return msgs.pop(0)

        async def send(m):
            out.append(m)

        await guard.Utf8Guard(app)({"type": "http", "method": method}, receive, send)

    asyncio.run(go())
    return seen, out


class Utf8GuardTests(unittest.TestCase):
    def test_utf8_cyrillic_passes_through_intact(self):
        body = json.dumps({"query": "MEV защита"}, ensure_ascii=False).encode("utf-8")
        seen, out = run_guard(body)
        self.assertEqual(seen["body"], body)
        self.assertEqual(out[0]["status"], 200)

    def test_cp1251_body_gets_parse_error_not_500(self):
        body = '{"query": "MEV защита"}'.encode("cp1251")
        seen, out = run_guard(body)
        self.assertNotIn("body", seen)
        self.assertEqual(out[0]["status"], 400)
        err = json.loads(out[1]["body"])
        self.assertEqual(err["error"]["code"], -32700)
        self.assertIn("UTF-8", err["error"]["message"])


if __name__ == "__main__":
    unittest.main()
