"""Source-grounded missing Type1 ToUnicode mappings, on a memory-only PDF.

Existing mappings are never overridden. Unsupported font/CMap contracts remain
unresolved. A glyph must exist in its embedded CharStrings and actual PDF font
encoding; no OCR, language inference, page IDs or Markdown replacements occur.
"""
import hashlib
import re
from pathlib import Path

ADAPTER_REVISION = 'source-font-tounicode-v2'
GLYPHLIST_SOURCE = 'https://raw.githubusercontent.com/apache/pdfbox/2.0.14/pdfbox/src/main/resources/org/apache/pdfbox/resources/glyphlist/additional.txt'
GLYPHLIST_SHA256 = 'e70928360e577f03fa4fe354dc353a56526ef675e07790b9131acd27fd38c28f'
MATHML_GLYPH_NAMES = {'mapsto':'\u21a6'}
MATHML_GLYPH_SOURCE = 'https://www.w3.org/TR/1998/PR-math-19980224/chap6/MMALIAS2.html'


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


def _glyph_text(name, fontname, supplemental):
    from fontTools import agl
    # Icon fonts deliberately use private codes. Represent the verified source
    # glyph's name explicitly rather than inventing a prose Unicode substitute.
    if 'FontAwesome' in fontname and re.fullmatch(r'[a-z][a-z0-9-]{0,79}',name):
        return '[icon:'+name+']','source-icon-glyphname'
    value=agl.toUnicode(name)
    source='adobe-glyph-list'
    if not value: value=supplemental.get(name,'');source='pdfbox-additional-glyphlist'
    if not value: value=MATHML_GLYPH_NAMES.get(name,'');source='w3c-mathml-glyph-name'
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


def _cmap_codes(raw):
    """Recognize only complete one-byte literal CMaps; never infer inheritance."""
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
    codes=set()
    for body in re.findall(r'beginbfchar(.*?)endbfchar',text,re.S):
        pairs=re.findall(r'<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]+)>',body)
        if re.sub(r'<[0-9a-fA-F]{2}>\s*<[0-9a-fA-F]+>','',body).strip():raise ValueError('unsupported ToUnicode bfchar')
        codes.update(int(src,16) for src,_ in pairs)
    for body in re.findall(r'beginbfrange(.*?)endbfrange',text,re.S):
        pattern=r'<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{2})>\s*(?:<[0-9a-fA-F]+>|\[[0-9a-fA-F<>\s]+\])'
        for match in re.finditer(pattern,body):
            first,last=int(match[1],16),int(match[2],16)
            if last<first:raise ValueError('invalid ToUnicode range')
            codes.update(range(first,last+1))
        if re.sub(pattern,'',body).strip():raise ValueError('unsupported ToUnicode bfrange')
    return codes


def _font_key(name):
    return re.sub(r'^[A-Z]{6}\+','',name)


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
    with missing Unicode in texttrace are considered; every missing encoding entry
    receives a mapping solely when embedded glyph semantics are independently
    supported. Unknown symbols remain unresolved for the caller's quality gate.
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
            missing={_font_key(run['font']) for run in page.get_texttrace()
                     if any(ch[0]==65533 for ch in run['chars'])}
            fonts=page.get_fonts(full=True)
            for row in fonts:
                kind,value=document.xref_get_key(row[0],'ToUnicode')
                if kind=='xref':cmap_owners.setdefault(int(value.split()[0]),set()).add(row[0])
            for name in missing:
                matches={row[0] for row in fonts if _font_key(row[3])==name}
                if len(matches)!=1:
                    ambiguous.update(matches)
                    diagnostics['unresolved'].append(dict(font_xref=None,font_name=name,reason='ambiguous or missing page font resource'))
                else:affected[next(iter(matches))]=name
        used_cmaps={}
        for xref,name in affected.items():
            if xref in ambiguous:continue
            try:
                if document.xref_get_key(xref,'Subtype')!=('name','/Type1'):raise ValueError('unsupported missing-Unicode font subtype')
                actual_name,ext,kind,program=document.extract_font(xref)
                if not program or ext!='pfa':raise ValueError('embedded Type1 program required')
                font=t1Lib.T1Font.__new__(t1Lib.T1Font);font.encoding='ascii';font.data=program;font.parse()
                glyphs=set(font.getGlyphSet());codes=_encoding(document,xref,font)
                kind,value=document.xref_get_key(xref,'ToUnicode')
                created=kind=='null'
                if created:cmap_xref=None;before=_new_cmap()
                elif kind=='xref':
                    cmap_xref=int(value.split()[0]);before=document.xref_stream(cmap_xref)
                    if len(cmap_owners.get(cmap_xref,set()))!=1:raise ValueError('shared ToUnicode owner is ambiguous')
                else:raise ValueError('unsupported explicit ToUnicode object')
                covered=_cmap_codes(before)
                mappings=[]
                for code,glyph in enumerate(codes):
                    if code in covered or glyph=='.notdef' or glyph not in glyphs:continue
                    text,origin=_glyph_text(glyph,actual_name,supplemental)
                    if text is None:
                        diagnostics['unresolved'].append(dict(font_xref=xref,font_name=name,code=code,glyph_name=glyph,reason='no trusted Unicode glyph semantics'))
                        continue
                    mappings.append(dict(font_xref=xref,font_name=name,code=code,glyph_name=glyph,text=text,mapping_source=origin,
                        font_program_sha256=_sha(program),encoding_sha256=_sha('\n'.join(codes).encode()),
                        tounicode_before_sha256=None if created else _sha(before),tounicode_created=created,
                        mapping_reference=MATHML_GLYPH_SOURCE if origin=='w3c-mathml-glyph-name' else None))
                if not mappings:continue
                body=(str(len(mappings))+' beginbfchar\n'+''.join('<%02X> <%s>\n'%(m['code'],m['text'].encode('utf-16-be').hex().upper()) for m in mappings)+'endbfchar\n').encode('ascii')
                closing=list(re.finditer(rb'(?m)^[ \t]*endcmap[ \t]*(?:%[^\r\n]*)?\r?$',before))
                if len(closing)!=1:raise ValueError('unsupported ToUnicode closing syntax')
                at=closing[0].start();after=before[:at]+body+before[at:]
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
