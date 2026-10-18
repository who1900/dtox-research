"""Source-grounded Unicode mappings, on a memory-only derivative PDF.

Valid mappings are never overridden. Unsupported font/CMap contracts remain
unresolved. A glyph must exist in its embedded CharStrings and actual PDF font
encoding; no OCR, language inference, page IDs or Markdown replacements occur.
"""
import hashlib
import json
import re
from pathlib import Path
if __package__:
    from .pdf_cid_unicode import invalid_scalar, repair_identity_h
else:
    from pdf_cid_unicode import invalid_scalar, repair_identity_h

ADAPTER_REVISION = 'source-font-tounicode-v5'
GLYPHLIST_SOURCE = 'https://raw.githubusercontent.com/apache/pdfbox/2.0.14/pdfbox/src/main/resources/org/apache/pdfbox/resources/glyphlist/additional.txt'
GLYPHLIST_SHA256 = 'e70928360e577f03fa4fe354dc353a56526ef675e07790b9131acd27fd38c28f'
MATHML_GLYPH_NAMES = {'mapsto':'\u21a6'}
MATHML_GLYPH_SOURCE = 'https://www.w3.org/TR/1998/PR-math-19980224/chap6/MMALIAS2.html'
BBDING_REFERENCE_SHA256 = 'fd11709c57988d2b790b580e0e8d29dc026490f3b3f245468d615e517b5d4a7b'
BBDING_REFERENCE_SOURCE = 'https://mirrors.ctan.org/fonts/niceframe/type1/bbding10.pfb'
BBDING_SYMBOL_SOURCE = 'https://mirrors.ctan.org/fonts/bbding/bbding.pdf'
BBDING_UNICODE_SOURCE = 'https://mirrors.ctan.org/macros/latex/contrib/hyperref/doc/hyperref.pdf'
BBDING_REFERENCE_GLYPHS = {
    33: ('enc-33', '\u2713', '8a24e322f7a74b54d41379d4559f3f9eaf548bc0dbc810e760d0c4910427b6a4'),
    34: ('enc-34', '\u2714', 'e22502702922356ffce54fe58ca5dfa82343e173ff50430dadd07c87a65abaf9'),
    35: ('enc-35', '\u2715', 'f856aaf9c9a6b1a85ac5928b1f37d2a30d648bc1e9294dcfc570ab0191383785'),
    36: ('enc-36', '\u2716', '10e1e743523eb2c0c989beace3ad8f3f15b513e62ba09227b48d845de09804ea'),
    37: ('enc-37', '\u2717', 'ef9aa05a5c7b0e7ef254ce81683d229e086476a570c21cad8f663dbc0adaf005'),
}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _supplemental():
    raw=Path(__file__).with_name('pdfbox_additional_glyphlist.txt').read_bytes().replace(b'\r\n',b'\n')
    if _sha(raw)!=GLYPHLIST_SHA256: raise ValueError('supplemental glyphlist hash mismatch')
    result={}
    for line in raw.decode('ascii').splitlines():
        if not line.strip() or line.startswith('#'):continue
        name,code=line.split(';')
        result[name]=''.join(chr(int(value,16)) for value in code.split())
    return result


def _type1_outline_sha256(charstring, font_matrix):
    """Hash an expanded Type 1 outline together with its advance and matrix."""
    from fontTools.pens.recordingPen import RecordingPen
    if not isinstance(font_matrix, (list, tuple)) or len(font_matrix) != 6:
        return None
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           for value in font_matrix):
        return None
    pen = RecordingPen()
    charstring.draw(pen)  # expands local/global subroutines through fontTools
    width = getattr(charstring, 'width', None)
    if isinstance(width, bool) or not isinstance(width, (int, float)):
        return None
    def canonical(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        return value
    try:
        payload = json.dumps([canonical(font_matrix), float(width), canonical(pen.value)],
                             ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return _sha(payload.encode('ascii'))


def _bbding_text(name, code, charstring, font_matrix):
    """Use CTAN bbding semantics only after an exact code and outline proof."""
    reference = BBDING_REFERENCE_GLYPHS.get(code)
    if reference is None or name != reference[0] or charstring is None:
        return None
    if _type1_outline_sha256(charstring, font_matrix) != reference[2]:
        return None
    return reference[1]


def _glyph_text(name, fontname, supplemental, *, code=None, charstring=None,
                font_matrix=None):
    from fontTools import agl
    # Icon fonts deliberately use private codes. Represent the verified source
    # glyph's name explicitly rather than inventing a prose Unicode substitute.
    if 'FontAwesome' in fontname and re.fullmatch(r'[a-z][a-z0-9-]{0,79}',name):
        return '[icon:'+name+']','source-icon-glyphname'
    value=agl.toUnicode(name)
    source='adobe-glyph-list'
    if not value: value=supplemental.get(name,'');source='pdfbox-additional-glyphlist'
    if not value: value=MATHML_GLYPH_NAMES.get(name,'');source='w3c-mathml-glyph-name'
    if not value:
        value=_bbding_text(name,code,charstring,font_matrix)
        if value:source='ctan-bbding-outline-match'
    if (not value or any(ord(x)<32 or ord(x)==65533 or 0xD800<=ord(x)<=0xDFFF
            or 0xE000<=ord(x)<=0xF8FF for x in value)):
        return None,None
    return value,source


def _pdf_name(value):
    return re.sub(r'#([0-9a-fA-F]{2})',lambda m:chr(int(m[1],16)),value)


def _encoding(document,xref,font):
    from fontTools.encodings.StandardEncoding import StandardEncoding
    kind,value=document.xref_get_key(xref,'Encoding')
    if kind=='null': return list(font['Encoding'])
    if kind=='name':
        if value!='/StandardEncoding':raise ValueError('unsupported Type1 base encoding')
        return list(StandardEncoding)
    if kind!='xref':raise ValueError('unsupported Type1 encoding object')
    obj=document.xref_object(int(value.split()[0]))
    base=re.search(r'/BaseEncoding\s*/([^\s/<>\[\]()]+)',obj)
    if base and base[1]!='StandardEncoding':raise ValueError('unsupported Type1 base encoding')
    codes=list(StandardEncoding if base else font['Encoding'])
    diff=re.search(r'/Differences\s*\[([^\]]*)\]',obj,re.S)
    if diff:
        body=diff[1];tokens=re.findall(r'\d+|/[^\s/<>\[\]()]+',body)
        if re.sub(r'\d+|/[^\s/<>\[\]()]+','',body).strip():raise ValueError('unsupported encoding Differences syntax')
        cursor=None
        for token in tokens:
            if token.startswith('/'):
                if cursor is None or cursor>255:raise ValueError('invalid encoding Differences code')
                codes[cursor]=_pdf_name(token[1:]);cursor+=1
            else:cursor=int(token)
    if len(codes)!=256:raise ValueError('unsupported Type1 encoding width')
    return codes


def _cmap_entries(raw):
    """Read complete one-byte literal CMaps without treating empty targets as mappings."""
    text=re.sub(r'%[^\n]*','',raw.decode('ascii'))
    if 'usecmap' in text or text.count('endcmap')!=1:raise ValueError('unsupported inherited/malformed ToUnicode')
    spaces=re.findall(r'begincodespacerange(.*?)endcodespacerange',text,re.S)
    if not spaces:raise ValueError('explicit one-byte ToUnicode codespace required')
    ranges=[]
    for body in spaces:
        pairs=re.findall(r'<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{2})>',body)
        if not pairs or re.sub(r'<[0-9a-fA-F]{2}>\s*<[0-9a-fA-F]{2}>','',body).strip():
            raise ValueError('unsupported ToUnicode code width')
        ranges.extend((int(a,16),int(b,16)) for a,b in pairs)
    # Keep insertion within an unambiguous full one-byte codespace; narrower
    # legacy contracts need their own explicit source-code attribution.
    if ranges!=[(0,255)]:raise ValueError('unsupported restricted ToUnicode codespace')
    codes=set();empty={}
    for body in re.findall(r'beginbfchar(.*?)endbfchar',text,re.S):
        pairs=re.findall(r'<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]*)>',body)
        if re.sub(r'<[0-9a-fA-F]{2}>\s*<[0-9a-fA-F]*>','',body).strip():raise ValueError('unsupported ToUnicode bfchar')
        for src,dst in pairs:
            code=int(src,16)
            if dst:codes.add(code)
            else:empty[code]=empty.get(code,0)+1
    for body in re.findall(r'beginbfrange(.*?)endbfrange',text,re.S):
        pattern=r'<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{2})>\s*(?:<[0-9a-fA-F]+>|\[[0-9a-fA-F<>\s]+\])'
        for match in re.finditer(pattern,body):
            first,last=int(match[1],16),int(match[2],16)
            if last<first:raise ValueError('invalid ToUnicode range')
            codes.update(range(first,last+1))
        if re.sub(pattern,'',body).strip():raise ValueError('unsupported ToUnicode bfrange')
    return codes,empty


def _cmap_codes(raw):
    """Return source codes with a non-empty ToUnicode destination."""
    return _cmap_entries(raw)[0]


def _replace_empty_bfchar(raw, replacements):
    """Replace only one-line empty entries whose source glyph was proven."""
    result=raw
    for code,text in replacements.items():
        pattern=rb'(?m)^([ \t]*)<%02X>[ \t]*<[ \t]*>[ \t]*(?:%%[^\r\n]*)?$'%code
        result,count=re.subn(pattern,lambda match:match.group(1)+('<%02X> <%s>'%(code,text.encode('utf-16-be').hex().upper())).encode('ascii'),result)
        if count!=1:raise ValueError('observed empty ToUnicode entry is not uniquely replaceable')
    return result


def _font_key(name):
    return re.sub(r'^[A-Z]{6}\+','',name)


def _used_type1_codes(font, codes, glyph_ids):
    """Resolve observed MuPDF Type 1 GIDs to unique PDF character codes."""
    glyph_order=list(font.getGlyphSet())
    # FreeType follows CharStrings order, but repairs missing or misplaced
    # .notdef entries. Refuse those repairs because they change GID indices.
    if (not glyph_order or glyph_order[0]!='.notdef'
            or glyph_order.count('.notdef')!=1):
        raise ValueError('Type1 CharStrings GID order is not verifiable')
    by_name={}
    for gid,name in enumerate(glyph_order):
        by_name.setdefault(name,[]).append(gid)
    result=[]
    for glyph_id in sorted(glyph_ids):
        if not isinstance(glyph_id,int) or glyph_id<0 or glyph_id>=len(glyph_order):
            raise ValueError('texttrace Type1 glyph id is outside CharStrings')
        glyph_name=glyph_order[glyph_id]
        if len(by_name[glyph_name])!=1:
            raise ValueError('texttrace Type1 glyph name is ambiguous')
        matching=[code for code,name in enumerate(codes) if name==glyph_name]
        if len(matching)!=1:
            raise ValueError('texttrace Type1 glyph has no unique PDF encoding code')
        result.append((matching[0],glyph_id,glyph_name))
    return result


def _new_cmap():
    return b'''/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /DTOXSourceFontUnicode def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
'''


def normalize_pdf_bytes(raw):
    """Return a derivative PDF plus full mapping provenance; never write raw PDF.

    Runs inside the existing bounded native converter child. Only fonts observed
    with missing Unicode in texttrace are considered; only uniquely bound
    encoding codes receive trusted mappings. Unknown semantics reject only when
    observed; unprovable GID/code bindings remain unresolved for the quality gate.
    """
    import fitz
    import fontTools
    from fontTools import t1Lib
    if not isinstance(raw,bytes) or not raw.startswith(b'%PDF-'):raise ValueError('PDF bytes required')
    supplemental=_supplemental()
    diagnostics=dict(adapter_revision=ADAPTER_REVISION,raw_sha256=_sha(raw),derived_sha256=_sha(raw),changed=False,
        mappings=[],unresolved=[],glyphlist_sha256=GLYPHLIST_SHA256,glyphlist_source=GLYPHLIST_SOURCE,fonttools_version=fontTools.__version__)
    document=fitz.open(stream=raw,filetype='pdf')
    try:
        affected={};ambiguous=set();cmap_owners={}
        for page in document:
            missing={}
            for run in page.get_texttrace():
                glyph_ids={ch[1] for ch in run['chars'] if invalid_scalar(ch[0])}
                if glyph_ids:missing.setdefault(_font_key(run['font']),set()).update(glyph_ids)
            fonts=page.get_fonts(full=True)
            for row in fonts:
                # PyMuPDF uses xref 0 when a page font dictionary is direct
                # rather than an indirect object. Such a font has no stable
                # object identity for xref_get_key/extract_font.
                if row[0] > 0:
                    kind,value=document.xref_get_key(row[0],'ToUnicode')
                    if kind=='xref':cmap_owners.setdefault(int(value.split()[0]),set()).add(row[0])
            for name,glyph_ids in missing.items():
                matches={row[0] for row in fonts if _font_key(row[3])==name}
                if len(matches)!=1:
                    ambiguous.update(matches)
                    diagnostics['unresolved'].append(dict(font_xref=None,font_name=name,reason='ambiguous or missing page font resource'))
                else:
                    xref=next(iter(matches))
                    if xref <= 0:
                        diagnostics['unresolved'].append(dict(font_xref=0,font_name=name,
                            reason='direct page font dictionary has no indirect xref for source-grounded mapping'))
                    else:
                        entry=affected.setdefault(xref,dict(font_name=name,glyph_ids=set()))
                        entry['glyph_ids'].update(glyph_ids)
        used_cmaps={}
        for xref,affected_font in affected.items():
            name=affected_font['font_name']
            if xref in ambiguous:continue
            try:
                if document.xref_get_key(xref,'Subtype')==('name','/Type0'):
                    diagnostics['mappings'].extend(repair_identity_h(
                        document,xref,affected_font['glyph_ids'],cmap_owners))
                    continue
                if document.xref_get_key(xref,'Subtype')!=('name','/Type1'):raise ValueError('unsupported missing-Unicode font subtype')
                actual_name,ext,kind,program=document.extract_font(xref)
                if not program or ext!='pfa':raise ValueError('embedded Type1 program required')
                font=t1Lib.T1Font.__new__(t1Lib.T1Font);font.encoding='ascii';font.data=program;font.parse()
                glyphs=set(font.getGlyphSet());codes=_encoding(document,xref,font)
                used_codes=_used_type1_codes(font,codes,affected_font['glyph_ids'])
                kind,value=document.xref_get_key(xref,'ToUnicode')
                created=kind=='null'
                if created:cmap_xref=None;before=_new_cmap()
                elif kind=='xref':
                    cmap_xref=int(value.split()[0]);before=document.xref_stream(cmap_xref)
                    if len(cmap_owners.get(cmap_xref,set()))!=1:raise ValueError('shared ToUnicode owner is ambiguous')
                else:raise ValueError('unsupported explicit ToUnicode object')
                covered,empty_entries=_cmap_entries(before)
                mappings=[]
                observed={code:(glyph_id,glyph) for code,glyph_id,glyph in used_codes}
                for code,glyph in enumerate(codes):
                    if code in covered or glyph=='.notdef' or glyph not in glyphs:continue
                    if code in empty_entries and code not in observed:continue
                    if code in empty_entries and empty_entries[code]!=1:
                        glyph_id,_=observed[code]
                        diagnostics['unresolved'].append(dict(font_xref=xref,font_name=name,code=code,glyph_id=glyph_id,glyph_name=glyph,reason='ambiguous empty ToUnicode entries'))
                        continue
                    parsed_font=getattr(font,'font',{})
                    charstrings=parsed_font.get('CharStrings',{}) if isinstance(parsed_font,dict) else {}
                    charstring=charstrings.get(glyph)
                    font_matrix=parsed_font.get('FontMatrix') if isinstance(parsed_font,dict) else None
                    text,origin=_glyph_text(glyph,actual_name,supplemental,code=code,
                                            charstring=charstring,font_matrix=font_matrix)
                    if text is None:
                        if code in observed:
                            glyph_id,_=observed[code]
                            diagnostics['unresolved'].append(dict(font_xref=xref,font_name=name,code=code,glyph_id=glyph_id,glyph_name=glyph,reason='no trusted Unicode glyph semantics'))
                        continue
                    mapping=dict(font_xref=xref,font_name=name,code=code,glyph_name=glyph,text=text,mapping_source=origin,
                        font_program_sha256=_sha(program),encoding_sha256=_sha('\n'.join(codes).encode()),
                        tounicode_before_sha256=None if created else _sha(before),tounicode_created=created,
                        mapping_reference=MATHML_GLYPH_SOURCE if origin=='w3c-mathml-glyph-name' else None)
                    if origin=='ctan-bbding-outline-match':
                        mapping.update(glyph_outline_sha256=_type1_outline_sha256(charstring,font_matrix),
                            semantic_reference_font_sha256=BBDING_REFERENCE_SHA256,
                            semantic_reference_font_source=BBDING_REFERENCE_SOURCE,
                            mapping_reference=BBDING_UNICODE_SOURCE,
                            symbol_reference=BBDING_SYMBOL_SOURCE)
                    if code in observed:mapping['glyph_id']=observed[code][0]
                    mappings.append(mapping)
                if not mappings:continue
                replacements={m['code']:m['text'] for m in mappings if m['code'] in empty_entries}
                before=_replace_empty_bfchar(before,replacements) if replacements else before
                append_mappings=[m for m in mappings if m['code'] not in empty_entries]
                body=(str(len(append_mappings))+' beginbfchar\n'+''.join('<%02X> <%s>\n'%(m['code'],m['text'].encode('utf-16-be').hex().upper()) for m in append_mappings)+'endbfchar\n').encode('ascii') if append_mappings else b''
                closing=list(re.finditer(rb'(?m)^[ \t]*endcmap[ \t]*(?:%[^\r\n]*)?\r?$',before))
                if len(closing)!=1:raise ValueError('unsupported ToUnicode closing syntax')
                at=closing[0].start();after=before[:at]+body+before[at:] if body else before
                if created:
                    cmap_xref=document.get_new_xref()
                    document.update_object(cmap_xref,'<<>>')
                    document.xref_set_key(xref,'ToUnicode',str(cmap_xref)+' 0 R')
                if cmap_xref in used_cmaps and used_cmaps[cmap_xref]!=after:raise ValueError('shared ToUnicode mapping conflict')
                used_cmaps[cmap_xref]=after
                for mapping in mappings:
                    mapping['tounicode_after_sha256']=_sha(after)
                    mapping['font_tounicode_after']=str(cmap_xref)+' 0 R'
                document.update_stream(cmap_xref,after)
                diagnostics['mappings'].extend(mappings)
            except Exception as exc:
                diagnostics['unresolved'].append(dict(font_xref=xref,font_name=name,reason=str(exc)))
        if diagnostics['mappings']:
            derived=document.tobytes(garbage=0,deflate=False,no_new_id=True)
            diagnostics.update(changed=True,derived_sha256=_sha(derived))
            return derived,diagnostics
        return raw,diagnostics
    finally:document.close()
