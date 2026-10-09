"""Run a disposable series acceptance server; never uses the workspace data directory."""
import pathlib
import sys
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_unified_data import UnifiedDataTest

UnifiedDataTest.setUpClass()
try:
    fixture = UnifiedDataTest()
    account, date = fixture.series_fixture('浏览器验收店铺')
    app = fixture.app
    app.create_account('隔离验收店铺', '隔离验收店铺')
    app.save_series(account['id'], {'name': '单商品系列', 'product_ids': ['919688715573']}, 1)
    app.save_series(account['id'], {'name': '多商品系列', 'product_ids': ['919688715573', '1027168958052']}, 1)
    print('Disposable fixture server: http://127.0.0.1:8874', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8874), app.Handler).serve_forever()
finally:
    UnifiedDataTest.tearDownClass()
