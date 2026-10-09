import csv
import importlib.util
import os
import pathlib
import sqlite3
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class UnifiedDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ['DATA_ROOT'] = cls.temp.name
        spec = importlib.util.spec_from_file_location('workbench_test_app', ROOT / 'app.py')
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)
        cls.account = cls.app.create_account('test-account', 'test-store')
        cls.sample = pathlib.Path(cls.temp.name) / 'sanitized-wujie.csv'
        cls._write_sanitized_wujie_sample()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def _write_sanitized_wujie_sample(cls):
        headers = [
            '日期', '场景ID', '场景名字', '计划ID', '计划名字', '主体ID', '主体类型', '主体名称',
            '展现量', '点击量', '花费', '直接成交金额', '间接成交金额', '总成交金额',
            '直接成交笔数', '间接成交笔数', '总成交笔数', '成交人数', '总购物车数',
            '投入产出比', '总收藏数',
        ]
        rows = []
        products = ['919688715573', '1027168958052', '1066931268876', '820567382270']
        for product_id, spend, deal in zip(products, [20, 30, 40, 110], [10, 20, 30, 164.32]):
            rows.append(['2026-10-08', '371', '关键词推广', '81049746646', '测试计划A', product_id, '商品', f'脱敏商品{product_id[-2:]}', 100, 10, spend, deal, 0, deal, 1, 0, 1, 1, 2, deal / spend, 0])
        rows.extend([
            ['2026-10-08', '371', '关键词推广', '83376728266', '测试计划B', products[0], '商品', '脱敏商品73', 80, 8, 25, 50, 0, 50, 1, 0, 1, 1, 1, 2, 0],
            ['2026-10-08', '436', '货品全站推广', '83570900419', '测试计划C', products[0], '商品', '脱敏商品73', 60, 6, 25, 25, 0, 25, 1, 0, 1, 1, 1, 1, 0],
        ])
        with cls.sample.open('w', encoding='gb18030', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows(rows)

    def wujie_result(self):
        rows, encoding = self.app.read_csv_file_with_encoding(self.sample)
        index, header = self.app.find_header(rows, ['花费', '投入产出比', '总成交金额', '计划ID'])
        result = self.app.analyse_file(self.sample.name, [header] + rows[index + 1:])
        result['row_numbers'] = list(range(index + 2, index + 2 + result['row_count']))
        result['attribution_window'] = 'unknown'
        return result, encoding

    def test_wujie_encoding_grain_and_string_ids(self):
        result, encoding = self.wujie_result()
        self.assertEqual(encoding, 'gb18030')
        self.assertEqual((result['date'], result['row_count']), ('2026-10-08', 6))
        self.assertTrue(all(isinstance(row['主体ID'], str) and isinstance(row['计划ID'], str) for row in result['records']))
        self.assertEqual(sum(row['计划ID'] == '81049746646' for row in result['records']), 4)
        self.assertEqual(sum(row['主体ID'] == '919688715573' for row in result['records']), 3)
        self.assertEqual({row['场景名字'] for row in result['records']}, {'关键词推广', '货品全站推广'})

    def test_import_persists_and_deduplicates(self):
        result, encoding = self.wujie_result()
        saved = self.app.save_import('wujie', self.account['id'], 'test-store', 'batch-1', self.sample.name, self.sample.read_bytes(), result, encoding)
        self.assertFalse(saved['duplicate'])
        duplicate = self.app.save_import('wujie', self.account['id'], 'test-store', 'batch-2', self.sample.name, self.sample.read_bytes(), result, encoding)
        self.assertTrue(duplicate['duplicate'])
        with sqlite3.connect(self.app.UNIFIED_DB) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_rows r JOIN import_batches b ON b.id=r.batch_id WHERE b.account_id=?', (self.account['id'],)).fetchone()[0], 6)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_plan_product_facts WHERE account_id=?', (self.account['id'],)).fetchone()[0], 6)
            self.assertEqual(conn.execute('SELECT typeof(product_id), typeof(plan_id) FROM daily_plan_product_facts LIMIT 1').fetchone(), ('text', 'text'))

    def test_same_day_revision_is_conflict_and_not_effective(self):
        result, encoding = self.wujie_result()
        changed = self.sample.read_bytes() + b'\n'
        saved = self.app.save_import('wujie', self.account['id'], 'test-store', 'batch-conflict', 'changed.csv', changed, result, encoding)
        self.assertEqual(saved['unified_status'], 'conflict')
        data = self.app.query_unified(self.account['id'], '2026-10-08', '2026-10-08')
        self.assertEqual(len(data['plans']), 6)
        self.assertAlmostEqual(sum(row['spend'] for row in data['plans']), 250, places=2)

    def test_account_isolation(self):
        other = self.app.create_account('other-account', 'other-store')
        result, encoding = self.wujie_result()
        saved = self.app.save_import('wujie', other['id'], 'other-store', 'other-batch', self.sample.name, self.sample.read_bytes(), result, encoding)
        self.assertFalse(saved['duplicate'])
        self.assertEqual(len(self.app.query_unified(other['id'], '2026-10-08', '2026-10-08')['plans']), 6)

    def test_plan_totals_do_not_duplicate_shop_metrics(self):
        data = self.app.query_unified(self.account['id'], '2026-10-08', '2026-10-08')
        plan = next(row for row in self.app.query_plan_totals(data['plans']) if row['plan_id'] == '81049746646')
        self.assertEqual(len(plan['product_ids']), 4)
        self.assertEqual(plan['metrics']['spend'], 200.0)
        self.assertEqual(plan['metrics']['attributed_deal_amount'], 224.32)

    def test_sycm_xls_and_product_id_when_local_sample_exists(self):
        sample = next((ROOT / 'data/raw/shengyicanmou').rglob('*.xls'), None)
        if sample is None:
            self.skipTest('未提供本地生意参谋 XLS 样本')
        with tempfile.TemporaryDirectory() as directory:
            rows = self.app.convert_xls(sample, pathlib.Path(directory))
        index, header = self.app.find_header(rows, ['支付金额', '成功退款金额', '支付件数', '商品访客数'], ['支付买家数', '成交买家数', '成交买家数量'])
        result = self.app.analyse_file(sample.name, [header] + rows[index + 1:])
        self.assertTrue(result['records'][0]['商品ID'].isdigit())
        self.assertIsInstance(result['records'][0]['商品ID'], str)

    def test_rejects_invalid_encoding_id_and_mixed_dates(self):
        bad = pathlib.Path(self.temp.name) / 'bad.csv'
        bad.write_bytes(b'\xff\xfe\x00')
        with self.assertRaisesRegex(ValueError, '无法识别 CSV 编码'):
            self.app.read_csv_file_with_encoding(bad)
        result, _ = self.wujie_result()
        header = result['headers']
        rows = [dict(row) for row in result['records'][:2]]
        rows[0]['主体ID'] = '1.048939072726E12'
        with self.assertRaisesRegex(ValueError, '完整数字字符串'):
            self.app.analyse_file('invalid.csv', [header] + [[row.get(field, '') for field in header] for row in rows])
        rows[0]['主体ID'] = result['records'][0]['主体ID']
        rows[1]['日期'] = '2026-10-07'
        with self.assertRaisesRegex(ValueError, '多个业务日期'):
            self.app.analyse_file('mixed.csv', [header] + [[row.get(field, '') for field in header] for row in rows])

    def test_delete_marks_batch_and_removes_raw_file(self):
        result, encoding = self.wujie_result()
        other = self.app.create_account('delete-account', 'delete-store')
        saved = self.app.save_import('wujie', other['id'], 'delete-store', 'delete-batch', self.sample.name, self.sample.read_bytes(), result, encoding)
        with sqlite3.connect(self.app.DB_PATHS['wujie']) as conn:
            path = pathlib.Path(conn.execute('SELECT file_path FROM imports WHERE account_id=?', (other['id'],)).fetchone()[0])
        self.assertTrue(path.exists())
        self.assertTrue(self.app.delete_import('wujie', other['id'], saved['sha256']))
        self.assertFalse(path.exists())
        with sqlite3.connect(self.app.UNIFIED_DB) as conn:
            self.assertEqual(conn.execute('SELECT status, is_effective FROM import_batches WHERE account_id=?', (other['id'],)).fetchone(), ('deleted', 0))


if __name__ == '__main__':
    unittest.main()
