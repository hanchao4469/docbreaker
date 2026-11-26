"""Local recovery must preserve facts, ordering and visual fallback boundaries."""
import unittest
import xml.etree.ElementTree as ET

from docbreaker.layout import local_layout, text_and_rules_only


def layout(rows):
    page = ET.Element("page", width="600", height="800")
    for y, cells in rows:
        for x, right, text in cells:
            line = ET.SubElement(page, "line")
            word = ET.SubElement(line, "word", xMin=str(x), yMin=str(y), xMax=str(right), yMax=str(y+12))
            word.text = text
    return ET.tostring(page), "\n".join(text for _, cells in rows for _, _, text in cells)


def table():
    return layout([(30, [(30, 180, "榜单")]),
        (120, [(30, 55, "排名"), (160, 190, "企业"), (400, 460, "说明")]),
        (145, [(40, 50, "1"), (150, 200, "联想"), (300, 550, "海外收入70%")]),
        (170, [(40, 50, "2"), (150, 200, "小米"), (300, 550, "覆盖180个国家")]),
        (195, [(40, 50, "3"), (150, 200, "华为"), (300, 550, "得分88.00")]),
        (760, [(30, 200, "来源：原始报告"), (530, 570, "5 / 22")])])


def svg(content):
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'+content+'</svg>').encode()


class LocalRoutingTests(unittest.TestCase):
    def test_centered_header_and_split_footer_do_not_disqualify_table(self):
        xml, native = table()
        text, kind = local_layout(xml, native)
        self.assertEqual(kind, "simple_table")
        self.assertIn("| 1 | 联想 | 海外收入70% |", text)
        self.assertIn("| 3 | 华为 | 得分88.00 |", text)
        self.assertIn("来源：原始报告", text)
        self.assertIn("5 / 22", text)
        self.assertLess(text.index("联想"), text.index("小米"))

    def test_missing_native_glyphs_and_merged_cells_still_reject(self):
        xml, native = table()
        self.assertIsNone(local_layout(xml, native+"丢失文字"))
        root = ET.fromstring(xml)
        root.remove(list(root)[6])
        broken = ET.tostring(root)
        native = "".join(root.itertext())
        self.assertIsNone(local_layout(broken, native))

    def test_numbered_directory_keeps_descriptions(self):
        rows = [(60, [(40, 100, "目录")])]
        for n in range(1, 5):
            rows.extend([(100+n*70, [(40, 60, f"0{n}"), (180, 400, f"章节{n}")]),
                         (125+n*70, [(180, 490, f"章节{n}的说明文字")])])
        xml, native = layout(rows)
        text, kind = local_layout(xml, native)
        self.assertEqual(kind, "directory")
        self.assertLess(text.index("章节1的说明"), text.index("02"))
        self.assertIn("章节4的说明文字", text)

    def test_directory_requires_sequential_labels(self):
        xml, native = layout([(40, [(30,100,"目录")]), (100, [(30,50,"01"),(180,400,"第一章")]),
                             (170, [(30,50,"03"),(180,400,"第三章")]), (240, [(30,50,"04"),(180,400,"第四章")])])
        self.assertIsNone(local_layout(xml, native))

    def test_chinese_panels_keep_column_reading_order(self):
        rows = [(60, [(40,350,"风险与展望")]), (150, [(40,150,"三大风险"),(330,450,"未来展望")])]
        for n in range(4):
            rows.append((190+n*35, [(40,280,f"风险{n}：本地合规与经营管理需要关注"),
                                     (330,560,f"展望{n}：技术能力与本地运营持续提升")]))
        rows.append((700, [(40,280,"数据来源：公开资料与原始报告")]))
        xml, native = layout(rows)
        text, kind = local_layout(xml,native)
        self.assertEqual(kind,"separated_columns")
        self.assertLess(text.index("风险3"),text.index("未来展望"))
        self.assertLess(text.index("展望3"),text.index("数据来源"))
        self.assertEqual(sorted(c for c in text if not c.isspace()), sorted(c for c in native if not c.isspace()))

    def test_numeric_grid_is_not_a_text_panel(self):
        xml, native = layout([(100, [(30,200,"行业"),(330,500,"收入")])]+[
            (150+n*30,[(30,100,"汽车"),(330,380,str(n))]) for n in range(6)])
        candidate=local_layout(xml,native)
        self.assertTrue(candidate is None or candidate[1] == "simple_table")
        chart=svg('<path fill="red" d="M 30 150 L 100 150 L 100 170 L 30 170 Z"/>')
        self.assertFalse(text_and_rules_only(chart,xml,candidate[1] if candidate else None))

    def test_white_canvas_and_table_header_background_are_allowed(self):
        xml, _=table()
        drawing=svg('<rect x="0" y="0" width="600" height="800" fill="white"/>'
                    '<path fill="blue" d="M 20 115 L 80 115 L 80 140 L 20 140 Z M 20 115"/>')
        self.assertTrue(text_and_rules_only(drawing,xml,"simple_table"))
        linux_svg = drawing.replace(b'fill="white"', b'style="fill:rgb(100%,100%,100%);stroke:none;"')
        self.assertTrue(text_and_rules_only(linux_svg,xml,"simple_table"))
        self.assertFalse(text_and_rules_only(drawing,xml,"directory"))

    def test_body_bars_curves_and_rotated_shapes_stay_visual(self):
        xml,_=table()
        for drawing in [
            '<path fill="blue" d="M 20 150 L 80 150 L 80 175 L 20 175 Z"/>',
            '<path fill="none" d="M 20 150 C 80 150 80 175 20 175"/>',
            '<path fill="blue" transform="matrix(1, .2, 0, 1, 0, 0)" d="M 20 115 L 80 115 L 80 140 L 20 140 Z"/>',
            '<rect x="0" y="0" width="600" height="800" fill="blue"/>']:
            self.assertFalse(text_and_rules_only(svg(drawing),xml,"simple_table"))

    def test_detached_text_prefixes_preserve_all_glyphs_without_symbol_whitelist(self):
        for marker in ("●", "◇", "※", "①", "a"):
            rows = [(100+n*25, [(50,550,"This is a sufficiently long source sentence retaining the exact meaning.")]) for n in range(10)]
            rows[3] = (175, [(38,44,marker),(50,550,"This is a sufficiently long source sentence retaining the exact meaning.")])
            xml, native = layout(rows)
            text,kind = local_layout(xml,native)
            self.assertEqual(kind,"prose_with_margins")
            self.assertIn(marker+" This is",text)
            self.assertEqual(sorted(c for c in native if not c.isspace()),sorted(c for c in text if not c.isspace()))

    def test_only_small_margin_images_are_allowed_with_transforms(self):
        xml,_=table()
        defs='<defs><image id="logo" width="50" height="20"/></defs>'
        self.assertTrue(text_and_rules_only(svg(defs+'<g transform="matrix(1,0,0,1,500,10)"><use xlink:href="#logo"/></g>'),xml,"simple_table"))
        self.assertFalse(text_and_rules_only(svg(defs+'<use xlink:href="#logo" transform="matrix(1,0,0,1,200,200)"/>'),xml,"simple_table"))
        self.assertFalse(text_and_rules_only(svg(defs+'<use xlink:href="#missing"/>'),xml,"simple_table"))
        self.assertFalse(text_and_rules_only(svg(defs+'<use xlink:href="#logo"/>')))
        big='<defs><image id="logo" width="600" height="20"/></defs><use xlink:href="#logo"/>'
        self.assertFalse(text_and_rules_only(svg(big),xml,"simple_table"))


if __name__ == "__main__":
    unittest.main()
