"""Recover invalid Identity-H Unicode from the same embedded TrueType font.

Only observed bad scalars and uniquely proven CID/GID/Unicode bindings qualify.
Valid PDF mappings remain authoritative. No system font or language inference.
"""
import hashlib
import io
import re


def invalid_scalar(value):
    return value == 0xFFFD or 0xD800 <= value <= 0xDFFF


def _entries(raw):
    """Literal two-byte bfchar CMaps; retain exact destination byte offsets."""
    clean = re.sub(rb'%[^\r\n]*', lambda m: b' ' * len(m[0]), raw)
    if re.search(rb'\b(?:usecmap|beginbfrange|begincidchar|begincidrange)\b', clean):
        raise ValueError('unsupported Identity-H ToUnicode operators')
    spaces = re.findall(rb'begincodespacerange(.*?)endcodespacerange', clean, re.S)
    if len(spaces) != 1 or re.sub(rb'\s', b'', spaces[0]) != b'<0000><FFFF>':
        raise ValueError('full two-byte ToUnicode codespace required')
    entries = {}
    for block in re.finditer(rb'(\d+)\s+beginbfchar\b(.*?)\bendbfchar', clean, re.S):
        pairs = list(re.finditer(rb'<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]*)>', block[2]))
        if len(pairs) != int(block[1]) or re.sub(rb'<[0-9A-Fa-f]{4}>\s*<[0-9A-Fa-f]*>', b'', block[2]).strip():
            raise ValueError('malformed Identity-H bfchar block')
        for pair in pairs:
            cid = int(pair[1], 16)
            if cid in entries:
                raise ValueError('duplicate Identity-H bfchar code')
            dst = bytes.fromhex(pair[2].decode('ascii'))
            try:
                text = dst.decode('utf-16-be', errors='strict')
                bad = not text or any(invalid_scalar(ord(c)) for c in text)
            except UnicodeError:
                bad = True
            entries[cid] = (bad, block.start(2) + pair.start(2), block.start(2) + pair.end(2))
    if not entries or len(re.findall(rb'\bbeginbfchar\b', clean)) != len(re.findall(rb'\bendbfchar\b', clean)):
        raise ValueError('missing or malformed Identity-H bfchar entries')
    return entries


def _unicode_by_gid(font):
    """Require agreement across all Unicode subtables, including aliases."""
    by_code = {}
    for table in font['cmap'].tables if 'cmap' in font else []:
        if table.isUnicode():
            for code, glyph in table.cmap.items():
                by_code.setdefault(code, set()).add(glyph)
    reverse, conflicts = {}, set()
    for code, glyphs in by_code.items():
        if len(glyphs) != 1:
            conflicts.update(glyphs)
        for glyph in glyphs:
            reverse.setdefault(glyph, set()).add(code)
    result = {}
    for gid, glyph in enumerate(font.getGlyphOrder()):
        codes = reverse.get(glyph, set())
        if glyph in conflicts or len(codes) != 1:
            continue
        code = next(iter(codes))
        if (0 <= code <= 0x10FFFF and not invalid_scalar(code)
                and chr(code).isprintable() and not 0xE000 <= code <= 0xF8FF
                and not 0xF0000 <= code <= 0x10FFFD):
            result[gid] = chr(code)
    return result


def repair_identity_h(document, xref, glyph_ids, cmap_owners):
    from fontTools.ttLib import TTFont, TTCollection
    if document.xref_get_key(xref, 'Encoding') != ('name', '/Identity-H'):
        raise ValueError('source-grounded Type0 repair requires Identity-H')
    descendants = re.fullmatch(r'\[\s*(\d+)\s+0\s+R\s*\]', document.xref_get_key(xref, 'DescendantFonts')[1])
    if not descendants:
        raise ValueError('one indirect CID descendant required')
    descendant = int(descendants[1])
    if document.xref_get_key(descendant, 'Subtype') != ('name', '/CIDFontType2'):
        raise ValueError('embedded CIDFontType2 required')
    kind, value = document.xref_get_key(descendant, 'CIDToGIDMap')
    if (kind, value) == ('name', '/Identity'):
        gid_for = lambda cid: cid
        cidmap_sha = 'Identity'
    elif kind == 'xref':
        cidmap = document.xref_stream(int(value.split()[0]))
        if len(cidmap) % 2:
            raise ValueError('invalid CIDToGIDMap length')
        gid_for = lambda cid: int.from_bytes(cidmap[2*cid:2*cid+2], 'big') if 2*cid+2 <= len(cidmap) else 0
        cidmap_sha = hashlib.sha256(cidmap).hexdigest()
    else:
        raise ValueError('explicit CIDToGIDMap required')
    kind, value = document.xref_get_key(xref, 'ToUnicode')
    if kind != 'xref':
        raise ValueError('explicit Identity-H ToUnicode stream required')
    cmap_xref = int(value.split()[0])
    if cmap_owners.get(cmap_xref) != {xref}:
        raise ValueError('shared ToUnicode owner is ambiguous')
    before = document.xref_stream(cmap_xref)
    entries = _entries(before)
    name, ext, _, program = document.extract_font(xref)
    if ext != 'ttf' or not program:
        raise ValueError('embedded TrueType program required')
    collection = None
    if program.startswith(b'ttcf'):
        collection = TTCollection(io.BytesIO(program))
        expected = re.sub(r'^[A-Z]{6}\+', '', name)
        matches = [(i, f) for i, f in enumerate(collection.fonts)
                   if expected in {n.toUnicode() for n in f['name'].names if n.nameID == 6}]
        if len(matches) != 1:
            collection.close()
            raise ValueError('embedded collection member is ambiguous')
        member, font = matches[0]
    else:
        member, font = 0, TTFont(io.BytesIO(program))
    try:
        semantics = _unicode_by_gid(font)
        mappings, edits = [], []
        for cid, (bad, start, end) in entries.items():
            gid = gid_for(cid)
            if not bad or gid not in glyph_ids:
                continue
            text = semantics.get(gid)
            if text is None:
                raise ValueError('observed CID glyph has no unique embedded Unicode')
            edits.append((start, end, text.encode('utf-16-be').hex().upper().encode('ascii')))
            mappings.append(dict(font_xref=xref, font_name=name, code=cid, glyph_id=gid,
                text=text, mapping_source='embedded-truetype-unicode-cmap', font_member=member,
                font_program_sha256=hashlib.sha256(program).hexdigest(),
                cid_to_gid_sha256=cidmap_sha, tounicode_before_sha256=hashlib.sha256(before).hexdigest()))
        if not edits:
            raise ValueError('observed invalid Unicode has no repairable explicit CID mapping')
        after = before
        for start, end, replacement in sorted(edits, reverse=True):
            after = after[:start] + replacement + after[end:]
        if any(_entries(after)[m['code']][0] for m in mappings):
            raise ValueError('Identity-H repair verification failed')
        document.update_stream(cmap_xref, after)
        for mapping in mappings:
            mapping['tounicode_after_sha256'] = hashlib.sha256(after).hexdigest()
        return mappings
    finally:
        collection.close() if collection else font.close()
