"""Bounded, non-executing HTML structure extraction. Not a publication/edition gate."""
import hashlib
from html.parser import HTMLParser


class HTMLLimitError(ValueError):
    pass


_VOID = set("area base br col embed hr img input link meta param source track wbr".split())
_SKIP = set("script style head template noscript nav".split())
_BLOCK = set("p pre li blockquote h1 h2 h3 h4 h5 h6".split())
_TEX = {"application/x-tex", "application/x-latex", "text/x-tex"}


class _Node:
    def __init__(self, tag, attrs, start, parent=None):
        self.tag, self.attrs, self.start = tag, dict(attrs), start
        self.end, self.parent, self.children = start, parent, []


class _Parser(HTMLParser):
    def __init__(self, text, max_nodes, max_depth):
        super().__init__(convert_charrefs=True)
        self.text, self.max_nodes, self.max_depth = text, max_nodes, max_depth
        self.lines = [0]
        for i, char in enumerate(text):
            if char == "\n":
                self.lines.append(i + 1)
        self.root = _Node("root", [], 0)
        self.stack, self.count, self.errors = [self.root], 0, []

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def count_node(self):
        self.count += 1
        if self.count > self.max_nodes:
            raise HTMLLimitError("HTML node cap exceeded")

    def handle_starttag(self, tag, attrs):
        self.count_node()
        if len(self.stack) > self.max_depth:
            raise HTMLLimitError("HTML depth cap exceeded")
        node = _Node(tag, attrs, self.position(), self.stack[-1])
        node.end = node.start + len(self.get_starttag_text())
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, 0, -1)
                      if self.stack[i].tag == tag), None)
        if index is None:
            self.errors.append({"kind": "unmatched_end_tag", "tag": tag, "offset": self.position()})
            return
        end = self.text.find(">", self.position()) + 1
        if index != len(self.stack) - 1:
            self.errors.append({"kind": "implicit_close", "tag": tag, "offset": self.position()})
        for node in self.stack[index:]:
            node.end = end
        del self.stack[index:]

    def handle_data(self, data):
        self.count_node()
        self.stack[-1].children.append(data)

    def finish(self):
        self.feed(self.text)
        self.close()
        for node in self.stack[1:]:
            node.end = len(self.text)
            self.errors.append({"kind": "unclosed_tag", "tag": node.tag, "offset": node.start})


def _nodes(node, stop=()):
    for child in node.children:
        if isinstance(child, _Node) and child.tag not in _SKIP:
            yield child
            if child.tag not in stop:
                yield from _nodes(child, stop)


def _plain(node):
    return "".join(child if isinstance(child, str) else _plain(child)
                   for child in node.children if isinstance(child, str) or child.tag not in _SKIP)


def _project(content):
    parts = []
    for item in content:
        if item["kind"] == "text":
            parts.append(item["text"])
        elif item["kind"] in ("math", "table"):
            parts.append("[[" + item["kind"] + ":" + item["id"] + "]]")
        elif item["tag"] == "br":
            parts.append("\n")
        else:
            value = _project(item["children"])
            parts.append("[[" + item["tag"] + ":" + value + "]]" if item["tag"] in ("sub", "sup") else value)
    return "".join(parts)


class _Extractor:
    def __init__(self, parser, grid_cap, raw_cap):
        self.parser, self.grid_left, self.raw_left = parser, grid_cap, raw_cap
        self.header_left = grid_cap * 4
        self.math, self.tables, self.blocks, self.outline = [], [], [], []
        self.math_ids, self.table_ids = {}, {}
        self.math_records, self.table_records = {}, {}
        for node in _nodes(parser.root, stop={"math"}):
            if node.tag == "math":
                self.math_ids[node] = "math-" + str(len(self.math_ids) + 1)
            elif node.tag == "table":
                self.table_ids[node] = "table-" + str(len(self.table_ids) + 1)

    def raw(self, node):
        size = node.end - node.start
        if size > self.raw_left:
            raise HTMLLimitError("aggregate raw structure cap exceeded")
        self.raw_left -= size
        return self.parser.text[node.start:node.end]

    def location(self, node):
        ancestors, parent = [], node.parent
        while parent is not None:
            if parent.attrs.get("id"):
                ancestors.append(parent.attrs["id"])
            parent = parent.parent
        return {"source_dom_id": node.attrs.get("id"), "ancestor_dom_ids": ancestors[::-1],
                "source_span": {"start": node.start, "end": node.end, "unit": "unicode_codepoints"}}

    def math_ref(self, node, context):
        identity = self.math_ids[node]
        if node not in self.math_records:
            tex = [{"encoding": child.attrs["encoding"], "value": _plain(child), **self.location(child)}
                   for child in _nodes(node) if child.tag == "annotation" and
                   child.attrs.get("encoding", "").lower() in _TEX]
            unsupported = []
            if not tex:
                unsupported.append("mathml_semantics_not_interpreted")
            if self.parser.errors:
                unsupported.append("malformed_html")
            record = {"id": identity, **self.location(node), "context": dict(context),
                      "tex_annotations": tex, "alttext": node.attrs.get("alttext"),
                      "mathml": self.raw(node), "status": "EXPLICIT_TEX" if tex else "MATHML_ONLY",
                      "unsupported": unsupported}
            self.math.append(record)
            self.math_records[node] = record
        return {"kind": "math", "id": identity}

    def content(self, node, context):
        result = []
        for child in node.children:
            if isinstance(child, str):
                result.append({"kind": "text", "text": child})
            elif child.tag in _SKIP:
                continue
            elif child.tag == "math":
                result.append(self.math_ref(child, context))
            elif child.tag == "table":
                result.append(self.table_ref(child, context))
            else:
                result.append({"kind": "element", "tag": child.tag,
                               "source_dom_id": child.attrs.get("id"),
                               "alttext": child.attrs.get("alt") if child.tag == "img" else None,
                               "unsupported": ["nontext_content_not_transcribed"] if child.tag in
                               ("img", "svg", "canvas", "object", "iframe") else [],
                               "children": self.content(child, context)})
        return result

    def table_ref(self, node, context):
        identity = self.table_ids[node]
        if node in self.table_records:
            return {"kind": "table", "id": identity}
        record = {"id": identity, **self.location(node), "context": dict(context),
                  "raw_html": self.raw(node), "cells": [], "grid": None,
                  "header_binding_policy": "explicit_headers_scope_and_thead_only", "unsupported": []}
        self.table_records[node] = record
        self.tables.append(record)
        rows = [child for child in _nodes(node, stop={"table"}) if child.tag == "tr"]
        groups = []
        for row in rows:
            parent = row.parent
            while parent is not node and parent.tag not in ("thead", "tbody", "tfoot"):
                parent = parent.parent
            groups.append(parent)
        group_starts = {}
        for r, group in enumerate(groups):
            group_starts.setdefault(group, r)
        occupied = {}
        errors = record["unsupported"]
        for r, row in enumerate(rows):
            column = 0
            for cell in _nodes(row, stop={"table", "td", "th"}):
                if cell.tag not in ("td", "th"):
                    continue
                while (r, column) in occupied:
                    column += 1
                item = {"id": identity + "-cell-" + str(len(record["cells"]) + 1),
                        **self.location(cell), "row": r, "column": column,
                        "row_group": groups[r].tag, "row_group_start": group_starts[groups[r]],
                        "is_header": cell.tag == "th", "scope": cell.attrs.get("scope"),
                        "headers_attribute": cell.attrs.get("headers", "").split(),
                        "span_attributes": {k: cell.attrs.get(k) for k in ("rowspan", "colspan")}}
                item["content"] = self.content(cell, {**context, "table_id": identity, "cell_id": item["id"]})
                item["text"] = _project(item["content"]).strip()
                record["cells"].append(item)
                try:
                    rs, cs = int(cell.attrs.get("rowspan", "1")), int(cell.attrs.get("colspan", "1"))
                    if rs == 0:
                        rs = sum(group is groups[r] for group in groups[r:])
                    if rs < 1 or cs < 1 or r + rs > len(rows) or any(groups[i] is not groups[r] for i in range(r, r + rs)):
                        raise ValueError()
                except (ValueError, TypeError):
                    item.update(row_span=None, column_span=None)
                    errors.append("invalid_or_cross_group_span")
                    column += 1
                    continue
                item.update(row_span=rs, column_span=cs)
                if rs * cs > self.grid_left:
                    raise HTMLLimitError("aggregate table grid cap exceeded")
                self.grid_left -= rs * cs
                for rr in range(r, r + rs):
                    for cc in range(column, column + cs):
                        if (rr, cc) in occupied:
                            errors.append("overlapping_span")
                        occupied[rr, cc] = item["id"]
                column += cs
        if self.parser.errors:
            errors.append("malformed_html")
        if not errors:
            width = max((c for _, c in occupied), default=-1) + 1
            slots = len(rows) * width
            if slots > self.grid_left + len(occupied):
                raise HTMLLimitError("rectangular table grid cap exceeded")
            self.grid_left -= slots - len(occupied)
            record["grid"] = [[occupied.get((r, c)) for c in range(width)] for r in range(len(rows))]
        headers = [h for h in record["cells"] if h["is_header"]]
        work = len(record["cells"]) * len(headers)
        if work > self.header_left:
            raise HTMLLimitError("aggregate header association work cap exceeded")
        self.header_left -= work
        for item in record["cells"]:
            item["header_cell_ids"] = [h["id"] for h in headers if h is not item and (
                h["source_dom_id"] in item["headers_attribute"] or
                h["scope"] in ("col", "colgroup") and h["column"] <= item["column"] < h["column"] + (h["column_span"] or 1) or
                h["scope"] == "row" and h["row"] <= item["row"] < h["row"] + (h["row_span"] or 1) or
                h["scope"] == "rowgroup" and h["row_group_start"] == item["row_group_start"] or
                h["row_group"] == "thead" and h["row"] < item["row"] and h["column"] <= item["column"] < h["column"] + (h["column_span"] or 1))]
            item["header_binding_status"] = "HEADER" if item["is_header"] else "EXPLICIT" if item["header_cell_ids"] else "UNRESOLVED"
        if not any(c["is_header"] for c in record["cells"]):
            errors.append("no_explicit_th_headers")
        captions = [child for child in node.children if isinstance(child, _Node) and child.tag == "caption"]
        record["captions"] = [self.content(child, {**context, "table_id": identity}) for child in captions]
        return {"kind": "table", "id": identity}

    def emit(self, node, kind):
        identity = "block-" + str(len(self.blocks) + 1)
        if kind == "heading":
            level = int(node.tag[1])
            self.outline = [h for h in self.outline if h["level"] < level]
            self.outline.append({"id": identity, "level": level, "source_dom_id": node.attrs.get("id")})
        context = {"block_id": identity, "section_ids": [h["id"] for h in self.outline]}
        content = ([self.table_ref(node, context)] if node.tag == "table" else
                   [self.math_ref(node, context)] if node.tag == "math" else self.content(node, context))
        text = _project(content)
        self.blocks.append({"id": identity, "kind": kind, **self.location(node), **context,
                            "content": content, "text": text if node.tag == "pre" else " ".join(text.split())})

    def walk(self, node):
        pending = []
        def flush():
            if pending:
                proxy = _Node(node.tag, list(node.attrs.items()), node.start, node.parent)
                proxy.end, proxy.children = node.end, list(pending)
                if _plain(proxy).strip() or any(isinstance(c, _Node) for c in pending):
                    self.emit(proxy, "text")
                pending.clear()
        for child in node.children:
            if isinstance(child, str):
                pending.append(child)
            elif child.tag in _SKIP:
                continue
            elif child.tag in ("table", "math") or child.tag in _BLOCK:
                flush()
                if child.tag in ("li", "blockquote") and any(n.tag in _BLOCK for n in _nodes(child)):
                    self.walk(child)
                else:
                    self.emit(child, "heading" if child.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} else child.tag)
            elif any(n.tag in _BLOCK or n.tag == "table" for n in _nodes(child, stop={"math"})):
                flush()
                self.walk(child)
            else:
                pending.append(child)
        flush()


def project_search_text(ir, *, max_chars=16 * 1024 * 1024, max_segments=200000):
    """Opt-in diagnostic projection; does not mutate IR or verify editions."""
    if ir.get("schema") != "dtox-structured-html-1":
        raise ValueError("structured HTML IR required")
    if any(type(n) is not int or n < 1 for n in (max_chars, max_segments)):
        raise ValueError("positive integer projection limits required")
    indexes = {}
    for kind, key in (("math", "math"), ("table", "tables")):
        records = ir[key]
        indexes[kind] = {r["id"]: r for r in records}
        if len(indexes[kind]) != len(records):
            raise ValueError("duplicate structure ID")
    parts, segments, issues, seen = [], [], [], set()
    offset = 0

    def issue(code, identity):
        value = {"code": code, "structure_id": identity}
        if value not in issues:
            issues.append(value)

    def emit(text, record, context, kind, **binding):
        nonlocal offset
        if not text:
            return
        if offset + len(text) > max_chars or len(segments) >= max_segments:
            raise HTMLLimitError("search projection cap exceeded")
        segments.append({"kind": kind, "start": offset, "end": offset + len(text),
                         "raw_sha256": ir["raw_sha256"],
                         "source_dom_id": record.get("source_dom_id"),
                         "ancestor_dom_ids": list(record.get("ancestor_dom_ids", [])),
                         "source_span": dict(record["source_span"]),
                         "context": dict(context), **binding})
        parts.append(text)
        offset += len(text)

    def content(items, record, context, **binding):
        for item in items:
            kind = item["kind"]
            if kind == "text":
                emit(item["text"], record, context, "text", **binding)
            elif kind in ("math", "table"):
                structure(kind, item["id"], context, **binding)
            elif kind == "element":
                for error in item.get("unsupported", []):
                    issue(error, binding.get("cell_id", context.get("block_id")))
                tag = item["tag"]
                if tag == "br":
                    emit("\n", record, context, "separator", **binding)
                else:
                    if tag in ("sub", "sup"):
                        emit("_{" if tag == "sub" else "^{", record, context, "notation", **binding)
                    content(item.get("children", []), record, context, **binding)
                    if tag in ("sub", "sup"):
                        emit("}", record, context, "notation", **binding)
            else:
                issue("unknown_content_kind", context.get("block_id"))

    def structure(kind, identity, context, **binding):
        token = (kind, identity)
        if token in seen:
            issue("repeated_structure_reference", identity)
            return
        seen.add(token)
        record = indexes[kind].get(identity)
        if record is None:
            issue("missing_structure", identity)
            return
        binding = {**binding, kind + "_id": identity}
        for error in record.get("unsupported", []):
            issue(error, identity)
        if kind == "math":
            annotations = record.get("tex_annotations", [])
            values = list(dict.fromkeys(a["value"] for a in annotations if a["value"].strip()))
            if len(values) != 1:
                code = "ambiguous_tex" if values else "unsupported_math_without_tex"
                issue(code, identity)
                emit("[UNSUPPORTED MATH]", record, context, "unsupported", **binding)
            else:
                annotation = next(a for a in annotations if a["value"] == values[0])
                emit(values[0], record, context, "math", annotation_source_span=dict(annotation["source_span"]),
                     **binding)
            return
        grid = record.get("grid")
        if grid is None:
            issue("unsupported_table_grid", identity)
        elif any(cell is None for row in grid for cell in row):
            issue("table_grid_holes", identity)
        emit("\nTable " + identity + ":\n", record, context, "table", **binding)
        for caption in record.get("captions", []):
            emit("Caption: ", record, context, "caption", **binding)
            content(caption, record, context, **binding)
            emit("\n", record, context, "separator", **binding)
        for cell in record["cells"]:
            cell_binding = {**binding, "cell_id": cell["id"], "row": cell["row"],
                            "column": cell["column"], "row_span": cell["row_span"],
                            "column_span": cell["column_span"],
                            "header_cell_ids": list(cell["header_cell_ids"])}
            if cell["header_binding_status"] == "UNRESOLVED":
                issue("unresolved_cell_headers", cell["id"])
            prefix = (f"Row {cell['row'] + 1}, column {cell['column'] + 1}; "
                      f"span {cell['row_span']}x{cell['column_span']}; "
                      f"{'header' if cell['is_header'] else 'cell'}; "
                      f"headers {','.join(cell['header_cell_ids']) or 'undeclared'}: ")
            emit(prefix, cell, context, "cell", **cell_binding)
            content(cell["content"], cell, context, **cell_binding)
            emit("\n", cell, context, "separator", **cell_binding)

    for block in ir["blocks"]:
        context = {"block_id": block["id"], "section_ids": list(block["section_ids"])}
        content(block["content"], block, context)
        emit("\n\n", block, context, "separator")
    for kind, records in indexes.items():
        for identity in records:
            if (kind, identity) not in seen:
                issue("unreferenced_structure", identity)
    for diagnostic in ir.get("diagnostics", []):
        issue("source_parse_diagnostic", str(diagnostic))
    return {"schema": "dtox-structured-html-search-projection-1", "raw_sha256": ir["raw_sha256"],
            "text": "".join(parts), "segments": segments, "issues": issues,
            "quality_candidate": not issues, "quality_measured": False,
            "status": "DIAGNOSTIC_ONLY", "publication_authorized": False,
            "exact_edition_binding": dict(ir["exact_edition_binding"])}


def extract_html(raw, *, max_bytes=8 * 1024 * 1024, max_nodes=100000, max_depth=128,
                 max_grid_slots=200000, max_raw_chars=16 * 1024 * 1024):
    """Return diagnostic data only; consumers must separately verify exact edition binding."""
    limits = (max_bytes, max_nodes, max_depth, max_grid_slots, max_raw_chars)
    if any(type(value) is not int or value < 1 for value in limits):
        raise ValueError("positive integer extraction limits required")
    if not isinstance(raw, bytes):
        raise TypeError("raw HTML bytes required")
    if len(raw) > max_bytes:
        raise HTMLLimitError("HTML byte cap exceeded")
    text = raw.decode("utf-8", errors="strict")
    parser = _Parser(text, max_nodes, max_depth)
    parser.finish()
    extractor = _Extractor(parser, max_grid_slots, max_raw_chars)
    extractor.walk(parser.root)
    return {"schema": "dtox-structured-html-1", "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "status": "DIAGNOSTIC_ONLY", "publication_authorized": False,
            "exact_edition_binding": {"required_external_gate": True, "status": "NOT_VERIFIED",
                                      "document_id_equality_sufficient": False},
            "source_offsets": "decoded UTF-8 Unicode codepoints, not byte offsets",
            "text": "\n\n".join(block["text"] for block in extractor.blocks),
            "blocks": extractor.blocks, "math": extractor.math, "tables": extractor.tables,
            "diagnostics": parser.errors, "raw_markup_is_untrusted_data": True}
