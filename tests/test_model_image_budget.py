import io
import math
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from docbreaker.model_client import model_image_bytes


class ModelImageBudgetTests(unittest.TestCase):
    def test_closer_look_image_is_bounded_with_rounding_and_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'page.jpg'
            # Real incident: 155 x 200 = 31000 patches; provider limit 30000.
            Image.new('RGB', (4946, 6400), 'white').save(path)
            original_bytes = path.read_bytes()
            data, original, sent = model_image_bytes(path)
            self.assertEqual(original, (4946, 6400))
            self.assertLessEqual(math.ceil(sent[0] / 32) * math.ceil(sent[1] / 32), 28000)
            self.assertAlmostEqual(sent[0] / sent[1], original[0] / original[1], places=3)
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size, sent)
                self.assertEqual(image.format, 'JPEG')
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_normal_page_bytes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'page.jpg'
            Image.new('RGB', (2473, 3200), 'white').save(path)
            data, original, sent = model_image_bytes(path)
            self.assertEqual(original, sent)
            self.assertEqual(data, path.read_bytes())
