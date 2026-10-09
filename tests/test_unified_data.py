import csv
import importlib.util
import os
import pathlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor


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

    def test_baby_board_single_multi_and_no_promotion_products(self):
        account = self.app.create_account('baby-board-account', 'baby-board-store')
        date = '2026-10-08'
        sc_headers = ['统计日期', '商品ID', '商品名称', '支付金额', '成功退款金额', '支付件数', '商品访客数', '支付买家数', '商品浏览量', '商品加购人数', '商品加购件数', '商品支付转化率']
        product_rows = [
            [date, '1001', '单计划商品', '100', '0', '2', '50', '5', '80', '4', '6', '0.1'],
            [date, '1002', '多计划商品', '200', '10', '3', '100', '8', '160', '7', '11', '0.08'],
            [date, '1003', '无推广商品', '300', '0', '4', '120', '9', '210', '5', '8', '0.075'],
        ]
        sc_content = ('\n'.join([','.join(sc_headers), *[','.join(row) for row in product_rows]])).encode('utf-8')
        sc_result = self.app.analyse_file('baby-sc.csv', [sc_headers, *product_rows])
        self.app.save_import('shengyicanmou', account['id'], 'baby-board-store', 'baby-sc', 'baby-sc.csv', sc_content, sc_result, 'utf-8')
        wj_headers = ['日期', '场景ID', '场景名字', '计划ID', '计划名字', '主体ID', '主体类型', '主体名称', '展现量', '点击量', '花费', '直接成交金额', '间接成交金额', '总成交金额', '直接成交笔数', '间接成交笔数', '总成交笔数', '成交人数', '总购物车数', '投入产出比']
        wj_rows = [
            [date, '1', '关键词推广', '11', '单计划', '1001', '商品', '单计划商品', '1000', '100', '20', '30', '0', '30', '1', '0', '1', '1', '2', '1.5'],
            [date, '1', '关键词推广', '12', '多计划A', '1002', '商品', '多计划商品', '500', '50', '10', '12', '0', '12', '1', '0', '1', '1', '1', '1.2'],
            [date, '2', '货品全站推广', '13', '多计划B', '1002', '商品', '多计划商品', '300', '30', '5', '8', '0', '8', '1', '0', '1', '1', '1', '1.6'],
        ]
        wj_content = ('\n'.join([','.join(wj_headers), *[','.join(row) for row in wj_rows]])).encode('utf-8')
        wj_result = self.app.analyse_file('baby-wj.csv', [wj_headers, *wj_rows])
        self.app.save_import('wujie', account['id'], 'baby-board-store', 'baby-wj', 'baby-wj.csv', wj_content, wj_result, 'utf-8')
        single = self.app.baby_board(account['id'], '1001', date, date)
        multi = self.app.baby_board(account['id'], '1002', date, date)
        empty = self.app.baby_board(account['id'], '1003', date, date)
        self.assertEqual(single['business']['gmv'], 100.0)
        self.assertEqual(single['promotion']['spend'], 20.0)
        self.assertEqual(len(single['plans']), 1)
        self.assertEqual(multi['business']['gmv'], 200.0)
        self.assertEqual(multi['business']['page_views'], 160.0)
        self.assertEqual(multi['business']['cart_people'], 7.0)
        self.assertEqual(multi['business']['conversion_rate'], 0.08)
        self.assertEqual(multi['promotion']['spend'], 15.0)
        self.assertEqual(multi['promotion']['attributed_deal_amount'], 20.0)
        self.assertEqual(len(multi['plans']), 2)
        self.assertIsNone(empty['promotion'])
        self.assertEqual(empty['quality']['promotion_empty_message'], '无推广记录')
        ranged = self.app.baby_board(account['id'], '1002', '2026-10-07', date)
        self.assertEqual(ranged['quality']['people_scope'], 'daily_sum_not_period_deduplicated')
        self.assertEqual(len(ranged['trend']), 2)
        products = self.app.list_baby_products(account['id'], '多计划')
        self.assertEqual([item['product_id'] for item in products], ['1002'])

    def test_plan_board_filters_multiday_totals_and_account_isolation(self):
        account = self.app.create_account('plan-board-account', 'plan-board-store')
        other = self.app.create_account('plan-board-other', 'plan-board-other-store')
        headers = ['日期', '场景ID', '场景名字', '计划ID', '计划名字', '主体ID', '主体类型', '主体名称', '展现量', '点击量', '花费', '直接成交金额', '间接成交金额', '总成交金额', '直接成交笔数', '间接成交笔数', '总成交笔数', '成交人数', '总购物车数', '投入产出比']
        daily_rows = {
            '2026-10-07': [
                ['2026-10-07', '371', '关键词推广', '81049746646', '秋季主推计划', '919688715573', '商品', '商品甲', '1000', '100', '20', '40', '0', '40', '2', '0', '2', '2', '3', '2'],
                ['2026-10-07', '371', '关键词推广', '81049746646', '秋季主推计划', '1027168958052', '商品', '商品乙', '500', '50', '10', '15', '0', '15', '1', '0', '1', '1', '2', '1.5'],
                ['2026-10-07', '436', '货品全站推广', '83570900419', '全站补量计划', '919688715573', '商品', '商品甲', '300', '30', '15', '30', '0', '30', '1', '0', '1', '1', '1', '2'],
            ],
            '2026-10-08': [
                ['2026-10-08', '371', '关键词推广', '81049746646', '秋季主推计划', '919688715573', '商品', '商品甲', '1200', '120', '24', '48', '0', '48', '2', '0', '2', '2', '3', '2'],
                ['2026-10-08', '371', '关键词推广', '81049746646', '秋季主推计划', '1027168958052', '商品', '商品乙', '600', '60', '12', '18', '0', '18', '1', '0', '1', '1', '2', '1.5'],
                ['2026-10-08', '436', '货品全站推广', '83570900419', '全站补量计划', '919688715573', '商品', '商品甲', '400', '40', '20', '40', '0', '40', '1', '0', '1', '1', '1', '2'],
            ],
        }

        def import_days(target_account, prefix):
            for date, rows in daily_rows.items():
                content = ('\n'.join([','.join(headers), *[','.join(row) for row in rows]])).encode('utf-8')
                result = self.app.analyse_file(f'{prefix}-{date}.csv', [headers, *rows])
                result['attribution_window'] = '7d'
                self.app.save_import('wujie', target_account['id'], target_account['store_name'], f'{prefix}-{date}', f'{prefix}-{date}.csv', content, result, 'utf-8')

        import_days(account, 'plan')
        import_days(other, 'other-plan')
        board = self.app.plan_board(account['id'], '2026-10-07', '2026-10-08')
        self.assertEqual(board['quality']['row_count'], 6)
        self.assertEqual(board['summary']['impressions'], 4000.0)
        self.assertEqual(board['summary']['clicks'], 400.0)
        self.assertEqual(board['summary']['spend'], 101.0)
        self.assertEqual(board['summary']['attributed_deal_amount'], 191.0)
        self.assertEqual(board['summary']['ppc'], 0.25)
        self.assertEqual(board['summary']['roi'], 1.89)
        self.assertTrue(all(isinstance(row['product_id'], str) and isinstance(row['plan_id'], str) for row in board['rows']))

        product = self.app.plan_board(account['id'], '2026-10-07', '2026-10-08', product_id='919688715573')
        scene = self.app.plan_board(account['id'], '2026-10-07', '2026-10-08', scene_name='关键词推广')
        day = self.app.plan_board(account['id'], '2026-10-07', '2026-10-07')
        combined = self.app.plan_board(account['id'], '2026-10-07', '2026-10-08', product_id='919688715573', scene_name='货品全站推广', plan_id='83570900419')
        named = self.app.plan_board(account['id'], '2026-10-07', '2026-10-08', plan_name='主推')
        exact_named = self.app.plan_board(account['id'], '2026-10-07', '2026-10-08', plan_name='秋季主推计划', plan_name_match='exact')
        self.assertEqual((product['quality']['row_count'], product['summary']['spend']), (4, 79.0))
        self.assertEqual((scene['quality']['row_count'], scene['summary']['spend']), (4, 66.0))
        self.assertEqual((day['quality']['row_count'], day['summary']['spend']), (3, 45.0))
        self.assertEqual((combined['quality']['row_count'], combined['summary']['spend']), (2, 35.0))
        self.assertEqual(named['quality']['row_count'], 4)
        self.assertEqual(exact_named['quality']['row_count'], 4)

        plan = next(item for item in board['plan_totals'] if item['plan_id'] == '81049746646')
        self.assertEqual(plan['metrics']['spend'], 66.0)
        self.assertEqual(plan['metrics']['attributed_deal_amount'], 121.0)
        self.assertEqual(plan['metrics']['roi'], 1.83)
        baby = self.app.baby_board(account['id'], '919688715573', '2026-10-07', '2026-10-08')
        self.assertEqual(product['summary']['spend'], baby['promotion']['spend'])
        self.assertEqual(product['summary']['clicks'], baby['promotion']['clicks'])
        self.assertEqual(product['summary']['attributed_deal_amount'], baby['promotion']['attributed_deal_amount'])
        self.assertEqual(product['summary']['roi'], baby['promotion']['roi'])

        options = self.app.plan_board_options(account['id'], '2026-10-07', '2026-10-08')
        self.assertEqual(options['scenes'], ['关键词推广', '货品全站推广'])
        self.assertEqual(len(options['plans']), 2)
        self.assertEqual(len(options['products']), 2)
        other_board = self.app.plan_board(other['id'], '2026-10-07', '2026-10-08')
        self.assertEqual(other_board['quality']['row_count'], 6)
        self.assertEqual(board['quality']['row_count'], 6)

    def test_plan_board_does_not_merge_unknown_attribution_windows(self):
        rows = [
            {'attribution_window': 'unknown', 'batch_id': 1, 'impressions': 100.0, 'clicks': 10.0, 'spend': 5.0, 'total_deal_amount': 8.0, 'total_deal_orders': 1.0},
            {'attribution_window': 'unknown', 'batch_id': 2, 'impressions': 200.0, 'clicks': 20.0, 'spend': 10.0, 'total_deal_amount': 16.0, 'total_deal_orders': 2.0},
        ]
        metrics = self.app.promotion_metrics(rows)
        self.assertEqual(metrics['spend'], 15.0)
        self.assertEqual(metrics['clicks'], 30.0)
        self.assertIsNone(metrics['attributed_deal_amount'])
        self.assertIsNone(metrics['roi'])
        self.assertFalse(metrics['attribution_windows_compatible'])

    def series_fixture(self, name):
        account = self.app.create_account(name, name)
        date = '2026-10-08'
        headers = ['统计日期', '商品ID', '商品名称', '支付金额', '成功退款金额', '支付件数', '商品访客数', '支付买家数']
        rows = [[date, '919688715573', '系列商品A', '100', '5', '2', '50', '5'],
                [date, '1027168958052', '系列商品B', '300', '15', '6', '100', '10']]
        result = self.app.analyse_file('series-sc.csv', [headers, *rows])
        self.app.save_import('shengyicanmou', account['id'], name, name, 'series-sc.csv', repr(rows).encode(), result, 'utf-8')
        result, encoding = self.wujie_result()
        self.app.save_import('wujie', account['id'], name, name, self.sample.name, self.sample.read_bytes(), result, encoding)
        return account, date

    def test_series_single_multi_and_versioned_members(self):
        account, date = self.series_fixture('series-main')
        aid = account['id']
        first, second = '919688715573', '1027168958052'
        saved = self.app.save_series(aid, {'name': '系列一', 'product_ids': [first]}, 1)
        single = self.app.series_board(aid, saved['id'], date, date)
        baby = self.app.baby_board(aid, first, date, date)
        self.assertEqual(single['business'], baby['business'])
        self.assertEqual(single['promotion'], baby['promotion'])
        self.assertEqual(single['rows'][0]['sales_share'], 1)
        updated = self.app.save_series(aid, {'series_id': saved['id'], 'revision': saved['revision'], 'name': '系列一', 'product_ids': [first, second]}, 1)
        self.assertEqual(updated['current_version'], 2)
        multi = self.app.series_board(aid, saved['id'], date, date)
        self.assertEqual(multi['business']['gmv'], 400)
        self.assertEqual(multi['promotion']['spend'], 100)
        self.assertEqual(multi['promotion']['attributed_deal_amount'], 105)
        self.assertEqual(multi['promotion']['roi'], 1.05)
        self.assertEqual(multi['business']['conversion_rate'], .1)
        self.assertEqual([r['sales_share'] for r in multi['rows']], [.25, .75])
        self.assertEqual([r['spend_share'] for r in multi['rows']], [.7, .3])
        self.assertEqual(sum(r['gmv'] for r in multi['rows']), multi['business']['gmv'])
        old = self.app.series_board(aid, saved['id'], date, date, 1)
        self.assertEqual(old['business'], single['business'])
        self.assertEqual(old['product_ids'], [first])
        renamed = self.app.save_series(aid, {'series_id': updated['id'], 'revision': updated['revision'], 'name': '改名', 'product_ids': [second, first]}, 1)
        self.assertEqual(renamed['current_version'], 2)
        removed = self.app.save_series(aid, {'series_id': renamed['id'], 'revision': renamed['revision'], 'name': '改名', 'product_ids': [second]}, 1)
        self.assertEqual(removed['current_version'], 3)
        self.assertEqual(self.app.series_board(aid, saved['id'], date, date)['business']['gmv'], 300)
        self.app.init_databases()
        self.assertEqual(self.app.list_series(aid)[0]['versions'][1]['product_ids'], [first, second])
        self.assertEqual(self.app.series_board(aid, saved['id'], date, date, 2)['business']['gmv'], 400)

    def test_series_validation_isolation_soft_delete_and_conflicts(self):
        account, date = self.series_fixture('series-validation')
        aid, pid = account['id'], '919688715573'
        other = self.app.create_account('series-other', 'series-other')
        for ids in [[], [pid, pid], [123], ['1e12'], ['not-id'], ['999999999999999999'], ['  ']]:
            with self.assertRaises(ValueError):
                self.app.save_series(aid, {'name': '无效', 'product_ids': ids}, 1)
        with self.assertRaises(ValueError):
            self.app.save_series(other['id'], {'name': '跨店铺', 'product_ids': [pid]}, 1)
        saved = self.app.save_series(aid, {'name': '重复归属A', 'product_ids': [pid]}, 1)
        self.app.save_series(aid, {'name': '重复归属B', 'product_ids': [pid]}, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.app.save_series(aid, {'name': '重复归属A', 'product_ids': [pid]}, 1)
        for action in [lambda: self.app.series_board(other['id'], saved['id'], date, date),
                       lambda: self.app.delete_series(other['id'], saved['id'], 1),
                       lambda: self.app.save_series(aid, {'series_id': saved['id'], 'revision': 0, 'name': '过期', 'product_ids': [pid]}, 1),
                       lambda: self.app.series_board(aid, saved['id'], date, date, 999),
                       lambda: self.app.series_board(aid, saved['id'], date, '2026-10-07')]:
            with self.assertRaises(ValueError):
                action()
        self.app.delete_series(aid, saved['id'], saved['revision'])
        with self.assertRaises(ValueError):
            self.app.series_board(aid, saved['id'], date, date)
        with sqlite3.connect(self.app.ACCOUNT_DB) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM series_members WHERE series_id=?', (saved['id'],)).fetchone()[0], 1)
        self.assertEqual(self.app.baby_board(aid, pid, date, date)['business']['gmv'], 100)

    def test_series_missing_data_windows_people_and_data_groups(self):
        account, date = self.series_fixture('series-missing')
        aid = account['id']
        saved = self.app.save_series(aid, {'name': '缺数系列', 'product_ids': ['919688715573', '1066931268876']}, 1)
        data = self.app.series_board(aid, saved['id'], '2026-10-07', date)
        self.assertEqual(data['quality']['missing_business_products'], ['1066931268876'])
        self.assertEqual(data['quality']['expected_product_days'], 4)
        self.assertEqual(data['quality']['business_product_days'], 1)
        self.assertIsNone(data['rows'][1]['sales_share'])
        self.assertIsNone(data['business']['page_views'])
        self.assertIn('非系列', data['quality']['people_note'])
        empty = self.app.series_board(aid, saved['id'], '2026-10-01', '2026-10-02')
        self.assertIsNone(empty['business']['gmv'])
        self.assertIsNone(empty['promotion'])
        result, encoding = self.wujie_result()
        result['date'] = '2026-10-07'
        for row in result['records']:
            row['日期'] = '2026-10-07'
        self.app.save_import('wujie', aid, 'series-missing', 'second-day', 'second.csv', b'second-series-day', result, encoding)
        ranged = self.app.series_board(aid, saved['id'], '2026-10-07', date)
        self.assertEqual(ranged['promotion']['spend'], 220)
        self.assertIsNone(ranged['promotion']['roi'])
        self.assertIsNone(ranged['promotion']['attributed_deal_amount'])
        self.assertTrue(all(row['metrics']['roi'] is not None for row in ranged['trend']))
        state = self.app.data_group_payload(aid, 'series')
        self.assertTrue(next(item for item in state['metrics'] if item['metric_code'] == 'gmv')['available'])
        custom = self.app.save_data_group(aid, {'board': 'series', 'name': '系列核心组', 'metric_codes': ['spend', 'gmv', 'roi']}, 1)
        self.app.select_data_group(aid, 'series', custom['id'])
        self.app.init_databases()
        self.assertEqual(self.app.data_group_payload(aid, 'series')['selected_group_id'], custom['id'])

    def test_data_groups_are_account_scoped_and_board_aware(self):
        account = self.app.create_account('group-account', 'group-store')
        other = self.app.create_account('group-other', 'group-other-store')
        baby = self.app.data_group_payload(account['id'], 'baby')
        plan = self.app.data_group_payload(account['id'], 'plan')
        self.assertEqual({g['name'] for g in baby['groups']}, {'成交数据组', '流量数据组', '互动数据组', '推广数据组'})
        promotion = next(g for g in baby['groups'] if g['name'] == '推广数据组')
        self.assertTrue(any(item['metric_code'] == 'spend' and item['available'] for item in promotion['items']))
        self.assertFalse(any(item['metric_code'] == 'gmv' and item['available'] for item in next(g for g in plan['groups'] if g['name'] == '成交数据组')['items']))
        custom = self.app.save_data_group(account['id'], {'board': 'baby', 'name': '我的核心组', 'metric_codes': ['roi', 'spend', 'clicks']}, 1)
        self.assertEqual([item['metric_code'] for item in custom['items']], ['roi', 'spend', 'clicks'])
        selected = self.app.select_data_group(account['id'], 'baby', custom['id'])
        self.assertEqual(selected['selected_group_id'], custom['id'])
        persisted = self.app.data_group_payload(account['id'], 'baby')
        self.assertEqual(persisted['selected_group_id'], custom['id'])
        self.assertNotIn('我的核心组', {g['name'] for g in self.app.data_group_payload(other['id'], 'baby')['groups']})
        self.app.delete_data_group(account['id'], custom['id'])
        self.assertNotEqual(self.app.data_group_payload(account['id'], 'baby')['selected_group_id'], custom['id'])

    def test_data_groups_concurrent_first_load(self):
        account = self.app.create_account('concurrent-groups', 'concurrent-groups')
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(lambda board: self.app.data_group_payload(account['id'], board), ['store', 'baby', 'plan', 'series'] * 2))
        self.assertTrue(all(len(state['groups']) == 4 for state in states))
        self.assertEqual({tuple(group['id'] for group in state['groups']) for state in states}.__len__(), 1)


if __name__ == '__main__':
    unittest.main()
