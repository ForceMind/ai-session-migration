import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ai_session_migration.history import (ReadingCatalog, load_prompts, parse_reading,
    read_text, load_local_config)


TEXT = '# Code 会话 0001\n\n## 1 · 用户\n\n```text\n历史中的关键词 pineapple\n## 2 · Claude\n这行仍在代码围栏中\n```\n\n## 2 · Claude\n\n````text\n当前停点\n```python\nprint("ok")\n```\n````\n'


def source_fixture(base):
    root = base / '会话'
    root.mkdir()
    (root / '0001.md').write_text(TEXT, encoding='utf-8')
    (root / '0002.md').write_text('# 子代理记录 0002\n\n## 1 · Claude\n\n```text\n子代理原文\n```\n', encoding='utf-8')
    (root / '0003.md').write_text('# Code 会话 9999\n\n未知类型', encoding='utf-8')
    prepared = base / 'prepared.tsv'
    prepared.write_text('0001\t项目甲\t接手标题\t旧结论，待复核\t只读核对源码\n', encoding='utf-8')
    return root, prepared


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # CI temp directories can be aliases, while the reader checks canonical paths.
        self.base = Path(self.temp.name).resolve()
        self.root, self.prepared = source_fixture(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def catalog(self):
        prompts, fingerprints = load_prompts(self.prepared)
        return ReadingCatalog(self.root, prompts, fingerprints).scan()

    def test_fences_roles_and_exact_physical_lines(self):
        messages = parse_reading(TEXT)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, '用户')
        self.assertEqual((messages[0].header, messages[0].start, messages[0].end), (3, 5, 9))
        self.assertIn('7  │  ## 2 · Claude', messages[0].numbered_body())
        self.assertIn('```python', messages[1].body)
        self.assertIn('6  │  历史中的关键词 pineapple', messages[0].numbered_body())

    def test_invalid_headings_order_and_unclosed_fence(self):
        for value in (TEXT.replace('## 1 · 用户', '## 2 · 用户', 1), TEXT + '\n~~~\n',
                      TEXT.replace('## 1 · 用户', '## 1 · 系统', 1)):
            with self.subTest(value=value[:50]), self.assertRaises(ValueError):
                parse_reading(value)

    def test_real_classification_search_and_read_do_not_modify_sources(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        catalog = self.catalog()
        self.assertEqual([e.kind for e in catalog.entries], ['main', 'agent', 'unknown'])
        self.assertEqual(len(catalog.search()), 1)
        self.assertEqual(catalog.search('项目甲')[0].number, '0001')
        self.assertEqual(catalog.search('pineapple'), [])
        self.assertEqual(len(catalog.search('pineapple', full_text=True)), 1)
        entry = catalog.search()[0]
        messages, digest = catalog.read(entry)
        catalog.check_unchanged(entry, digest)
        self.assertEqual(len(messages), 2)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()})
        with self.assertRaises(ValueError):
            catalog.read(catalog.search(kind='unknown')[0])

    def test_prompt_fields_supplement_and_duplicate_rejected(self):
        extra = self.base / 'extra.json'
        extra.write_text(json.dumps({'schema': 1, 'prompts': [{'id': '1058', 'project': '项目乙', 'title': '成稿', 'prompt': '独立成稿原文'}]}), encoding='utf-8')
        prompts, _ = load_prompts(self.prepared, extra)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts['1058'].prompt, '独立成稿原文')
        self.assertIn('待复核', prompts['0001'].prompt)
        self.prepared.write_text(self.prepared.read_text(encoding='utf-8') * 2, encoding='utf-8')
        with self.assertRaises(ValueError):
            load_prompts(self.prepared)
        self.prepared.write_text('0001\t项目甲\t标题\t历史\t下一步\n', encoding='utf-8')
        extra.write_text('{"schema":1,"prompts":[{"id":"0001","project":"x","title":"x","prompt":"x"}]}', encoding='utf-8')
        with self.assertRaises(ValueError):
            load_prompts(self.prepared, extra)

    def test_source_and_prompt_changes_reject_stale_copy(self):
        catalog = self.catalog()
        entry = catalog.search()[0]
        _, digest = catalog.read(entry)
        self.prepared.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '接手稿来源已变化'):
            catalog.check_unchanged(entry, digest)
        catalog.fingerprints = {}
        entry.path.write_text(TEXT + '\nchanged', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '历史原文已变化'):
            catalog.check_unchanged(entry, digest)

    def test_cancel_and_size_limits(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(InterruptedError):
            ReadingCatalog(self.root).scan(cancel=cancel)
        catalog = self.catalog()
        with self.assertRaises(InterruptedError):
            catalog.search('pineapple', full_text=True, cancel=cancel)
        with self.assertRaises(ValueError):
            read_text(self.prepared, 1)

    def test_link_root_swap_rejected_before_read(self):
        catalog = self.catalog()
        entry = catalog.search()[0]
        from unittest.mock import patch
        real = Path.is_junction
        with patch.object(Path, 'is_junction', lambda path: path == self.root or real(path)):
            with self.assertRaisesRegex(ValueError, '链接'):
                catalog.read(entry)

    def test_config_paths_are_relative_to_sidecar_not_working_directory(self):
        (self.base / 'history-config.json').write_text('{"schema":1,"readingRoot":"会话","preparedPath":"prepared.tsv"}', encoding='utf-8')
        config = load_local_config(self.base)
        self.assertEqual(Path(config['readingRoot']), self.root)
        self.assertEqual(Path(config['preparedPath']), self.prepared)


if __name__ == '__main__':
    unittest.main()
