import unittest
from unittest.mock import patch
from pipeline import pdf_unicode as adapter


class SourceFontUnicodeTests(unittest.TestCase):
    def test_trusted_math_icon_and_unknown_semantics(self):
        glyphlist=adapter._supplemental()
        self.assertEqual(adapter._glyph_text('summationtext','ABCDEF+CMEX10',glyphlist),('\u2211','pdfbox-additional-glyphlist'))
        self.assertEqual(adapter._glyph_text('envelope-open','ABCDEF+FontAwesome5Free-Regular',glyphlist),('[icon:envelope-open]','source-icon-glyphname'))
        self.assertEqual(adapter._glyph_text('unrecognizedmath','CMEX10',glyphlist),(None,None))
        self.assertEqual(adapter._glyph_text('uniE123','OtherFont',glyphlist),(None,None))

    def test_existing_cmap_entries_remain_covered(self):
        raw=b'1 begincodespacerange\n<00><FF>\nendcodespacerange\n1 beginbfchar\n<50><2211>\nendbfchar\n1 beginbfrange\n<61><63><0061>\nendbfrange\nendcmap'
        self.assertEqual(adapter._cmap_codes(raw),{80,97,98,99})
        with self.assertRaises(ValueError):adapter._cmap_codes(b'/OtherCmap usecmap\nendcmap')

    def test_empty_cmap_destinations_are_not_usable_coverage(self):
        raw=b'1 begincodespacerange\n<00><FF>\nendcodespacerange\n2 beginbfchar\n<41><0041>\n<42> <>\nendbfchar\nendcmap'
        self.assertEqual(adapter._cmap_codes(raw),{0x41})
        self.assertEqual(adapter._cmap_entries(raw)[1],{0x42:1})
        self.assertEqual(adapter._replace_empty_bfchar(raw,{0x42:'B'}),
            b'1 begincodespacerange\n<00><FF>\nendcodespacerange\n2 beginbfchar\n<41><0041>\n<42> <0042>\nendbfchar\nendcmap')

    def test_pdf_encoding_differences_override_embedded_defaults(self):
        class Document:
            def xref_get_key(self,*args):return 'xref','12 0 R'
            def xref_object(self,*args):return '<< /Differences [36 /envelope#2Dopen 80 /summationtext] >>'
        from fontTools.encodings.StandardEncoding import StandardEncoding
        encoded=adapter._encoding(Document(),1,{'Encoding':list(StandardEncoding)})
        self.assertEqual(encoded[36],'envelope-open')
        self.assertEqual(encoded[80],'summationtext')

    def test_type1_trace_gids_select_only_uniquely_bound_codes(self):
        class Font:
            def __init__(self,names):self.names=names
            def getGlyphSet(self):return {name:None for name in self.names}
        font=Font(['.notdef','A','unknownmath'])
        codes=['.notdef']*256;codes[65]='A';codes[66]='unknownmath';codes[200]='unusedmath'
        self.assertEqual(adapter._used_type1_codes(font,codes,{1}),[(65,1,'A')])
        self.assertEqual(adapter._used_type1_codes(font,codes,{2}),[(66,2,'unknownmath')])
        codes[67]='A'
        with self.assertRaisesRegex(ValueError,'unique PDF encoding code'):
            adapter._used_type1_codes(font,codes,{1})
        with self.assertRaisesRegex(ValueError,'GID order is not verifiable'):
            adapter._used_type1_codes(Font(['A','.notdef']),codes,{1})

    def test_missing_tounicode_maps_only_trace_observed_glyphs(self):
        class Font:
            names=['.notdef','A','summationtext','mapsto','unknownmath','unusedmath']
            def parse(self):pass
            def __getitem__(self,key):
                codes=['.notdef']*256
                codes[65]='A';codes[80]='summationtext';codes[55]='mapsto';codes[66]='unknownmath';codes[77]='unusedmath'
                return codes
            def getGlyphSet(self):return {name:None for name in self.names}
        class Page:
            def get_texttrace(self):return [{'font':'CMEX10','chars':[(65533,1),(65533,4)]}]
            def get_fonts(self,full):return [(1,'pfa','Type1','CMEX10','F1')]
        class Document:
            def __init__(self):self.streams={};self.bindings={}
            def __iter__(self):return iter([Page()])
            def xref_get_key(self,xref,key):return ('name','/Type1') if key=='Subtype' else ('null','null')
            def extract_font(self,xref):return 'CMEX10','pfa','Type1',b'verified embedded font fixture'
            def get_new_xref(self):return 99
            def update_object(self,*args):pass
            def xref_set_key(self,xref,key,value):self.bindings[(xref,key)]=value
            def update_stream(self,xref,data):self.streams[xref]=data
            def tobytes(self,**kwargs):return b'%PDF-derived fixture'
            def close(self):pass
        document=Document();raw=b'%PDF-original fixture'
        with patch('fitz.open',return_value=document),patch('fontTools.t1Lib.T1Font',Font):
            derived,proof=adapter.normalize_pdf_bytes(raw)
        self.assertEqual(raw,b'%PDF-original fixture')
        self.assertNotEqual(raw,derived)
        self.assertEqual(document.bindings[(1,'ToUnicode')],'99 0 R')
        self.assertEqual(adapter._cmap_codes(document.streams[99]),{65,80,55})
        self.assertEqual({m['text'] for m in proof['mappings']},{'A','\u2211','\u21a6'})
        self.assertEqual(next(m['glyph_id'] for m in proof['mappings'] if m['code']==65),1)
        self.assertNotIn('glyph_id',next(m for m in proof['mappings'] if m['code']==80))
        self.assertTrue(all(m['tounicode_created'] and m['tounicode_before_sha256'] is None for m in proof['mappings']))
        self.assertEqual(proof['unresolved'],[{'font_xref':1,'font_name':'CMEX10','code':66,
            'glyph_id':4,'glyph_name':'unknownmath','reason':'no trusted Unicode glyph semantics'}])

    def test_fontawesome_missing_code_with_empty_cmap_entries(self):
        class Font:
            def parse(self):pass
            def __getitem__(self,key):
                codes=['.notdef']*256;codes[66]='B';codes[135]='github';return codes
            def getGlyphSet(self):return {'.notdef':None,'B':None,'github':None}
        cmap=(b'1 begincodespacerange\n<00><FF>\nendcodespacerange\n'
              b'2 beginbfchar\n<41><0041>\n<42> <>\nendbfchar\nendcmap')
        class Page:
            def get_texttrace(self):return [{'font':'ABCDEF+FontAwesome','chars':[(65533,1,(12.0,34.0)),(65533,2,(13.0,34.0))]}]
            def get_fonts(self,full):return [(1,'pfa','Type1','ABCDEF+FontAwesome','F1')]
        class Document:
            def __init__(self):self.streams={};self.bindings={}
            def __iter__(self):return iter([Page()])
            def xref_get_key(self,xref,key):
                if key=='Subtype':return 'name','/Type1'
                if key=='ToUnicode':return 'xref','2 0 R'
                if key=='Encoding':return 'xref','3 0 R'
                raise AssertionError(key)
            def xref_object(self,xref):return '<< /Differences [135 /github] >>'
            def xref_stream(self,xref):return cmap
            def extract_font(self,xref):return 'ABCDEF+FontAwesome','pfa','Type1',b'verified FontAwesome fixture'
            def tobytes(self,**kwargs):return b'%PDF-derived fixture'
            def update_stream(self,xref,data):self.streams[xref]=data
            def close(self):pass
        document=Document();raw=b'%PDF-original fixture'
        with patch('fitz.open',return_value=document),patch('fontTools.t1Lib.T1Font',Font):
            derived,proof=adapter.normalize_pdf_bytes(raw)
        self.assertNotEqual(derived,raw)
        self.assertEqual(proof['unresolved'],[])
        self.assertEqual([(m['code'],m['glyph_id'],m['glyph_name'],m['text'],m['mapping_source'])
                          for m in proof['mappings']],[(66,1,'B','B','adobe-glyph-list'),
                                                       (135,2,'github','[icon:github]','source-icon-glyphname')])
        after=document.streams[2]
        self.assertIn(b'<41><0041>',after)
        self.assertIn(b'<42> <0042>',after)
        self.assertIn(b'<87> <005B00690063006F006E003A006700690074006800750062005D>',after)
        self.assertEqual(adapter._cmap_codes(after),{0x41,0x42,0x87})

    def test_direct_font_xref_zero_is_reported_without_xref_lookup(self):
        class Page:
            def get_texttrace(self):return [{'font':'CMEX10','chars':[(65533,1)]}]
            def get_fonts(self,full):return [(0,'','Type1','CMEX10','F1')]
        class Document:
            def __iter__(self):return iter([Page()])
            def xref_get_key(self,xref,key):
                if xref == 0:raise AssertionError('xref zero is a direct font dictionary')
                raise AssertionError('no indirect font should be inspected')
            def close(self):pass
        raw=b'%PDF-direct-font-fixture'
        with patch('fitz.open',return_value=Document()):
            derived,proof=adapter.normalize_pdf_bytes(raw)
        self.assertEqual(derived,raw)
        self.assertEqual(proof['mappings'],[])
        self.assertEqual(proof['unresolved'][0]['font_xref'],0)
        self.assertIn('direct page font dictionary',proof['unresolved'][0]['reason'])


if __name__=='__main__':unittest.main()
