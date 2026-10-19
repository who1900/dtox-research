import unittest
from unittest.mock import patch
from pipeline import pdf_unicode as adapter


class SourceFontUnicodeTests(unittest.TestCase):
    def test_stmary_delimiters_have_exact_direction_and_no_name_guessing(self):
        import unicodedata
        glyphlist=adapter._supplemental()
        for name,code,expected in (('llbracket',74,'\u27e6'),('rrbracket',75,'\u27e7')):
            with self.subTest(name=name):
                text,source=adapter._glyph_text(name,'ABCDEF+stmary10',glyphlist,code=code)
                self.assertEqual(text,expected)
                self.assertEqual(source,'ctan-stmaryrd-unicode-delimiter')
                self.assertEqual(unicodedata.name(text),'MATHEMATICAL '+('LEFT' if code==74 else 'RIGHT')+' WHITE SQUARE BRACKET')
        for unknown in ('llbracketbig','llbracket.1','rrbracket.alt','LLbracket'):
            self.assertEqual(adapter._glyph_text(unknown,'stmary10',glyphlist),(None,None))
        # Existing trusted semantics take precedence over the new name source.
        self.assertEqual(adapter._glyph_text('llbracket','stmary10',{'llbracket':'['}),('[','pdfbox-additional-glyphlist'))

    def test_trusted_math_icon_and_unknown_semantics(self):
        glyphlist=adapter._supplemental()
        self.assertEqual(adapter._glyph_text('summationtext','ABCDEF+CMEX10',glyphlist),('\u2211','pdfbox-additional-glyphlist'))
        self.assertEqual(adapter._glyph_text('envelope-open','ABCDEF+FontAwesome5Free-Regular',glyphlist),('[icon:envelope-open]','source-icon-glyphname'))
        self.assertEqual(adapter._glyph_text('unrecognizedmath','CMEX10',glyphlist),(None,None))
        self.assertEqual(adapter._glyph_text('uniE123','OtherFont',glyphlist),(None,None))

    def test_stmary_fixture_repairs_source_encoding_and_preserves_existing_cmap(self):
        class Font:
            def parse(self):pass
            def __getitem__(self,key):
                codes=['.notdef']*256
                codes[74]='llbracket';codes[75]='rrbracket';codes[76]='llbracket.1'
                return codes
            def getGlyphSet(self):return dict.fromkeys(('.notdef','llbracket','rrbracket','llbracket.1'))
        class Page:
            def get_texttrace(self):return [{'font':'stmary10','chars':[(65533,1),(65533,2),(65533,3)]}]
            def get_fonts(self,full):return [(1,'pfa','Type1','stmary10','F1')]
        cmap=b'1 begincodespacerange\n<00><FF>\nendcodespacerange\n1 beginbfchar\n<41><0041>\nendbfchar\nendcmap'
        class Document:
            def __init__(self):self.streams={2:cmap}
            def __iter__(self):return iter([Page()])
            def xref_get_key(self,xref,key):
                if key=='Subtype':return 'name','/Type1'
                if key=='ToUnicode':return 'xref','2 0 R'
                return 'null','null'
            def xref_stream(self,xref):return self.streams[xref]
            def extract_font(self,xref):return 'stmary10','pfa','Type1',b'independent encoded bracket fixture'
            def update_stream(self,xref,data):self.streams[xref]=data
            def tobytes(self,**kwargs):return b'%PDF-derived bracket fixture'
            def close(self):pass
        document=Document();raw=b'%PDF-original bracket fixture'
        with patch('fitz.open',return_value=document),patch('fontTools.t1Lib.T1Font',Font):
            derived,proof=adapter.normalize_pdf_bytes(raw)
        self.assertNotEqual(derived,raw)
        self.assertIn(b'<41><0041>',document.streams[2])
        self.assertIn(b'<4A> <27E6>',document.streams[2])
        self.assertIn(b'<4B> <27E7>',document.streams[2])
        self.assertEqual([m['glyph_name'] for m in proof['mappings']],['llbracket','rrbracket'])
        self.assertTrue(all(m['symbol_reference']==adapter.STMARY_GLYPH_SOURCE for m in proof['mappings']))
        self.assertEqual([u['glyph_name'] for u in proof['unresolved']],['llbracket.1'])

    def test_type1_outline_fingerprint_covers_width_and_expanded_shape(self):
        class CharString:
            width=1000
            endpoint=10
            def draw(self,pen):
                pen.moveTo((0,0));pen.lineTo((self.endpoint,0));pen.closePath()
        font_matrix=[0.001,0,0,0.001,0,0]
        baseline=adapter._type1_outline_sha256(CharString(),font_matrix)
        self.assertEqual(baseline,adapter._type1_outline_sha256(CharString(),font_matrix))
        changed_width=CharString();changed_width.width=999
        changed_shape=CharString();changed_shape.endpoint=11
        self.assertNotEqual(baseline,adapter._type1_outline_sha256(changed_width,font_matrix))
        self.assertNotEqual(baseline,adapter._type1_outline_sha256(changed_shape,font_matrix))

    def test_bbding_requires_exact_reference_shape_and_source_encoding_slot(self):
        glyphlist=adapter._supplemental()
        charstring=object()
        proofs=((33,'enc-33','\u2713','8a24e322f7a74b54d41379d4559f3f9eaf548bc0dbc810e760d0c4910427b6a4'),
                (35,'enc-35','\u2715','f856aaf9c9a6b1a85ac5928b1f37d2a30d648bc1e9294dcfc570ab0191383785'),
                (36,'enc-36','\u2716','10e1e743523eb2c0c989beace3ad8f3f15b513e62ba09227b48d845de09804ea'),
                (34,'enc-34','\u2714','e22502702922356ffce54fe58ca5dfa82343e173ff50430dadd07c87a65abaf9'),
                (37,'enc-37','\u2717','ef9aa05a5c7b0e7ef254ce81683d229e086476a570c21cad8f663dbc0adaf005'))
        for code,name,unicode_value,fingerprint in proofs:
            with patch.object(adapter,'_type1_outline_sha256',return_value=fingerprint):
                self.assertEqual(adapter._reference_text(name,code,charstring,[0.001,0,0,0.001,0,0],adapter.BBDING_REFERENCE_GLYPHS),unicode_value)
                self.assertEqual(adapter._glyph_text(name,'arbitrary-font',glyphlist,code=code,
                    charstring=charstring,font_matrix=[0.001,0,0,0.001,0,0]),
                    (unicode_value,'ctan-bbding-outline-match'))
            with patch.object(adapter,'_type1_outline_sha256',return_value='0'*64):
                self.assertIsNone(adapter._reference_text(name,code,charstring,[0.001,0,0,0.001,0,0],adapter.BBDING_REFERENCE_GLYPHS))
            with patch.object(adapter,'_type1_outline_sha256',return_value=fingerprint):
                self.assertIsNone(adapter._reference_text(name,code+1,charstring,[0.001,0,0,0.001,0,0],adapter.BBDING_REFERENCE_GLYPHS))
                self.assertIsNone(adapter._reference_text('enc-999',code,charstring,[0.001,0,0,0.001,0,0],adapter.BBDING_REFERENCE_GLYPHS))

    def test_marvosym_letter_requires_exact_reference_outline_and_code(self):
        glyphlist=adapter._supplemental()
        fingerprint='dfc0ecf9e21cad018a58240388c4d301817314e87b6be6f4451838f5157b66bb'
        def resolve(name='Letter',code=66):
            return adapter._glyph_text(name,'arbitrary-font',glyphlist,code=code,
                charstring=object(),font_matrix=[0.001,0,0,0.001,0,0])
        with patch.object(adapter,'_type1_outline_sha256',return_value=fingerprint):
            self.assertEqual(resolve(),('\u2709','ctan-marvosym-outline-match'))
            self.assertEqual(resolve(code=65),(None,None))
            self.assertEqual(resolve(name='UnknownLetter'),(None,None))
        with patch.object(adapter,'_type1_outline_sha256',return_value='0'*64):
            self.assertEqual(resolve(),(None,None))

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
