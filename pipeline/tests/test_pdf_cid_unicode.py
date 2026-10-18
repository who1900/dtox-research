import io
import unittest
from pipeline import pdf_cid_unicode as adapter


def embedded_font(alias=False):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    builder = FontBuilder(1000, isTTF=True)
    order = ['.notdef', 'A', 'math']
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({65: 'A', 0x1D43B: 'math', **({0x1D43C: 'math'} if alias else {})})
    builder.setupGlyf({name: TTGlyphPen(None).glyph() for name in order})
    builder.setupHorizontalMetrics({name: (500, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'TestMath', 'styleName': 'Regular', 'psName': 'TestMath'})
    builder.setupOS2()
    builder.setupPost()
    builder.setupMaxp()
    stream = io.BytesIO()
    builder.font.save(stream)
    return stream.getvalue()


CMAP = (b'1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n'
        b'2 beginbfchar\n<0001> <0042>\n<0002> <D835>\nendbfchar\nendcmap')


class Document:
    def __init__(self, program, cidmap=('name', '/Identity')):
        self.program, self.cidmap, self.after = program, cidmap, None

    def xref_get_key(self, xref, key):
        return {'Encoding': ('name', '/Identity-H'), 'DescendantFonts': ('array', '[3 0 R]'),
                'Subtype': ('name', '/CIDFontType2'), 'CIDToGIDMap': self.cidmap,
                'ToUnicode': ('xref', '2 0 R')}[key]

    def xref_stream(self, xref):
        return CMAP if xref == 2 else b'\0\0\0\2\0\1'

    def extract_font(self, xref):
        return 'ABCDEF+TestMath', 'ttf', 'Type0', self.program

    def update_stream(self, xref, raw):
        self.after = raw


class CIDUnicodeTests(unittest.TestCase):
    def test_invalid_utf16_is_detected_without_reclassifying_valid_pairs(self):
        self.assertTrue(adapter._entries(CMAP)[2][0])
        self.assertFalse(adapter._entries(CMAP.replace(b'<D835>', b'<D835DC3B>'))[2][0])
        self.assertTrue(adapter.invalid_scalar(0xD835))
        self.assertTrue(adapter.invalid_scalar(0xFFFD))
        self.assertFalse(adapter.invalid_scalar(0x1D43B))

    def test_only_observed_bad_mapping_changes_using_embedded_unicode(self):
        d = Document(embedded_font())
        proof = adapter.repair_identity_h(d, 1, {2}, {2: {1}})
        self.assertEqual(d.after, CMAP.replace(b'<D835>', b'<D835DC3B>'))
        # Existing valid PDF mapping B stays B, despite embedded font saying A.
        self.assertIn(b'<0001> <0042>', d.after)
        self.assertEqual([(m['code'], m['glyph_id'], m['text']) for m in proof], [(2, 2, '\U0001D43B')])
        self.assertEqual(len(proof[0]['font_program_sha256']), 64)

    def test_ambiguous_unicode_and_unobserved_gid_leave_pdf_unchanged(self):
        for program, observed, message in [(embedded_font(True), {2}, 'unique embedded Unicode'),
                                           (embedded_font(), {1}, 'no repairable explicit')]:
            d = Document(program)
            with self.assertRaisesRegex(ValueError, message):
                adapter.repair_identity_h(d, 1, observed, {2: {1}})
            self.assertIsNone(d.after)

    def test_explicit_cid_to_gid_map_is_used(self):
        d = Document(embedded_font(), ('xref', '4 0 R'))
        with self.assertRaisesRegex(ValueError, 'no repairable explicit'):
            adapter.repair_identity_h(d, 1, {2}, {2: {1}})
        self.assertIsNone(d.after)

    def test_shared_cmap_and_unsupported_ranges_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'shared ToUnicode'):
            adapter.repair_identity_h(Document(embedded_font()), 1, {2}, {2: {1, 7}})
        with self.assertRaisesRegex(ValueError, 'unsupported Identity-H'):
            adapter._entries(CMAP + b'\n1 beginbfrange\n<0010><0020><0041>\nendbfrange')

    def test_collection_uses_unique_postscript_member_and_rejects_duplicates(self):
        from fontTools.ttLib import TTFont, TTCollection
        matching = TTFont(io.BytesIO(embedded_font()))
        other = TTFont(io.BytesIO(embedded_font(True)))
        for record in other['name'].names:
            if record.nameID == 6:
                record.string = 'OtherFont'.encode(record.getEncoding())
        collection = TTCollection()
        collection.fonts = [other, matching]
        stream = io.BytesIO()
        collection.save(stream)
        d = Document(stream.getvalue())
        proof = adapter.repair_identity_h(d, 1, {2}, {2: {1}})
        self.assertEqual(proof[0]['font_member'], 1)
        collection.fonts = [matching, matching]
        stream = io.BytesIO()
        collection.save(stream)
        d = Document(stream.getvalue())
        with self.assertRaisesRegex(ValueError, 'collection member is ambiguous'):
            adapter.repair_identity_h(d, 1, {2}, {2: {1}})
        self.assertIsNone(d.after)


if __name__ == '__main__':
    unittest.main()
