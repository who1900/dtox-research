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

    def test_pdf_encoding_differences_override_embedded_defaults(self):
        class Document:
            def xref_get_key(self,*args):return 'xref','12 0 R'
            def xref_object(self,*args):return '<< /Differences [36 /envelope#2Dopen 80 /summationtext] >>'
        from fontTools.encodings.StandardEncoding import StandardEncoding
        encoded=adapter._encoding(Document(),1,{'Encoding':list(StandardEncoding)})
        self.assertEqual(encoded[36],'envelope-open')
        self.assertEqual(encoded[80],'summationtext')

    def test_missing_tounicode_creates_complete_known_encoding_map(self):
        class Font:
            def parse(self):pass
            def __getitem__(self,key):
                codes=['.notdef']*256
                codes[65]='A';codes[80]='summationtext';codes[55]='mapsto';codes[66]='unknownmath'
                return codes
            def getGlyphSet(self):return {'A':None,'summationtext':None,'mapsto':None,'unknownmath':None}
        class Page:
            def get_texttrace(self):return [{'font':'CMEX10','chars':[(65533,1)]}]
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
        self.assertTrue(all(m['tounicode_created'] and m['tounicode_before_sha256'] is None for m in proof['mappings']))
        self.assertEqual(proof['unresolved'][0]['glyph_name'],'unknownmath')

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
