#!/usr/bin/env python3
import csv
import io
import json
import os
import hashlib
import secrets
import sqlite3
import shutil
import subprocess
import tempfile
import math
from contextlib import nullcontext
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(ROOT / "data"))).expanduser().resolve()
RAW_ROOT = DATA_ROOT / "raw"
DB_PATHS = {
    "shengyicanmou": DATA_ROOT / "shengyicanmou.sqlite3",
    "wujie": DATA_ROOT / "wujie.sqlite3",
}
UNIFIED_DB = DATA_ROOT / "workbench.sqlite3"
ACCOUNT_DB = DATA_ROOT / "accounts.sqlite3"
SESSION_DAYS = 7
LOCAL_SOFFICE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice"
SOFFICE = os.environ.get("SOFFICE") or shutil.which("soffice") or str(LOCAL_SOFFICE)

# Target metadata is centralized here. Numeric defaults remain unset until the
# business confirms them; the comparison direction is explicit and never inferred.
TARGET_DEFINITIONS = {
    "gmv": {"label": "GMV", "unit": "amount", "compare_type": "higher", "default_value": None, "mtd_kind": "cumulative"},
    "gsv": {"label": "GSV", "unit": "amount", "compare_type": "higher", "default_value": None, "mtd_kind": "cumulative"},
    "refund_rate": {"label": "退款率", "unit": "percent", "compare_type": "lower", "default_value": None, "mtd_kind": "ratio"},
    "conversion_rate": {"label": "转化率", "unit": "percent", "compare_type": "higher", "default_value": None, "mtd_kind": "ratio"},
    "average_order_value": {"label": "客单价", "unit": "amount", "compare_type": "higher", "default_value": None, "mtd_kind": "ratio"},
    "paid_units": {"label": "支付件数", "unit": "number", "compare_type": "higher", "default_value": None, "mtd_kind": "cumulative"},
    "paid_buyers": {"label": "成交买家数", "unit": "number", "compare_type": "higher", "default_value": None, "mtd_kind": "dedupe"},
    "spend": {"label": "推广花费", "unit": "amount", "compare_type": "budget", "default_value": None, "mtd_kind": "cumulative"},
    "clicks": {"label": "推广点击量", "unit": "number", "compare_type": "higher", "default_value": None, "mtd_kind": "cumulative"},
    "ppc": {"label": "推广点击单价", "unit": "amount", "compare_type": "lower", "default_value": None, "mtd_kind": "ratio"},
    "roi": {"label": "投入产出比", "unit": "ratio", "compare_type": "higher", "default_value": None, "mtd_kind": "ratio"},
    "fee_ratio": {"label": "推广费比", "unit": "percent", "compare_type": "lower", "default_value": None, "mtd_kind": "ratio"},
}


def source_label(source_type):
    return "生意参谋商品日报" if source_type == "shengyicanmou" else "无界商品报表"


def migrate_import_uniqueness(conn):
    """Scope duplicate-file detection to an account instead of the whole database."""
    unique_indexes = conn.execute("PRAGMA index_list(imports)").fetchall()
    for _, name, is_unique, *_ in unique_indexes:
        if not is_unique:
            continue
        columns = [row[2] for row in conn.execute(f'PRAGMA index_info("{name}")').fetchall()]
        if columns != ["file_sha256"]:
            continue
        conn.execute("ALTER TABLE imports RENAME TO imports_legacy")
        conn.execute(
            """CREATE TABLE imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                store_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                business_date TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                account_id INTEGER,
                import_status TEXT NOT NULL DEFAULT 'succeeded',
                UNIQUE(account_id, file_sha256)
            )"""
        )
        conn.execute(
            """INSERT INTO imports
                (id, batch_id, store_name, source_type, business_date, original_filename,
                 file_path, file_sha256, row_count, result_json, imported_at, account_id, import_status)
               SELECT id, batch_id, store_name, source_type, business_date, original_filename,
                      file_path, file_sha256, row_count, result_json, imported_at, account_id, import_status
                 FROM imports_legacy"""
        )
        conn.execute("DROP TABLE imports_legacy")
        break


def init_databases():
    DATA_ROOT.mkdir(exist_ok=True)
    RAW_ROOT.mkdir(exist_ok=True)
    with sqlite3.connect(ACCOUNT_DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, account_name TEXT NOT NULL UNIQUE, store_name TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS metric_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            store_name TEXT NOT NULL,
            month TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            custom_value REAL NULL,
            updated_by INTEGER,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id, month, metric_id)
        )""")
    with sqlite3.connect(UNIFIED_DB) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS source_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                store_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                original_name TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                encoding TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, sha256)
            );
            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                account_id INTEGER NOT NULL,
                store_id TEXT NOT NULL,
                source_file_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                business_date_start TEXT,
                business_date_end TEXT,
                report_grain TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                calculation_version TEXT NOT NULL,
                status TEXT NOT NULL,
                is_effective INTEGER NOT NULL DEFAULT 0,
                row_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                imported_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_batches_scope ON import_batches(account_id, source_type, business_date_start);
            CREATE TABLE IF NOT EXISTS import_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                row_number INTEGER,
                field_name TEXT,
                raw_value TEXT,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raw_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                row_number INTEGER NOT NULL,
                business_date TEXT,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                raw_payload TEXT NOT NULL,
                row_hash TEXT NOT NULL,
                UNIQUE(batch_id, row_number)
            );
            CREATE TABLE IF NOT EXISTS products (
                account_id INTEGER NOT NULL,
                store_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                product_name TEXT,
                first_seen_date TEXT,
                last_seen_date TEXT,
                PRIMARY KEY(account_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS plans (
                account_id INTEGER NOT NULL,
                store_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                plan_name TEXT,
                scene_id TEXT,
                scene_name TEXT,
                first_seen_date TEXT,
                last_seen_date TEXT,
                PRIMARY KEY(account_id, plan_id, scene_id)
            );
            CREATE TABLE IF NOT EXISTS daily_product_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                store_id TEXT NOT NULL,
                business_date TEXT NOT NULL,
                product_id TEXT NOT NULL,
                batch_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                visitors REAL,
                paid_buyers REAL,
                paid_units REAL,
                gmv REAL,
                successful_refund_amount REAL,
                raw_conversion_rate REAL,
                quality_status TEXT NOT NULL DEFAULT 'valid',
                UNIQUE(account_id, business_date, product_id, batch_id)
            );
            CREATE TABLE IF NOT EXISTS daily_plan_product_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                store_id TEXT NOT NULL,
                business_date TEXT NOT NULL,
                scene_id TEXT NOT NULL,
                scene_name TEXT,
                plan_id TEXT NOT NULL,
                plan_name TEXT,
                product_id TEXT NOT NULL,
                batch_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                impressions REAL,
                clicks REAL,
                spend REAL,
                direct_deal_amount REAL,
                indirect_deal_amount REAL,
                total_deal_amount REAL,
                direct_deal_orders REAL,
                indirect_deal_orders REAL,
                total_deal_orders REAL,
                deal_people REAL,
                total_cart_count REAL,
                raw_roi REAL,
                quality_status TEXT NOT NULL DEFAULT 'valid',
                UNIQUE(account_id, business_date, scene_id, plan_id, product_id, batch_id)
            );
            CREATE TABLE IF NOT EXISTS metric_definitions (
                metric_code TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                formula TEXT NOT NULL,
                aggregation TEXT NOT NULL,
                version TEXT NOT NULL
            );
        """)
        columns = {r[1] for r in conn.execute('PRAGMA table_info(import_batches)')}
        for name, definition in [('attribution_window', "TEXT NOT NULL DEFAULT 'unknown'"), ('headers_json', "TEXT NOT NULL DEFAULT '[]'")]:
            if name not in columns:
                conn.execute(f'ALTER TABLE import_batches ADD COLUMN {name} {definition}')
        product_fact_columns = {r[1] for r in conn.execute('PRAGMA table_info(daily_product_facts)')}
        for name in ('page_views', 'cart_people', 'cart_items'):
            if name not in product_fact_columns:
                conn.execute(f'ALTER TABLE daily_product_facts ADD COLUMN {name} REAL')
        metric_rows = [
            ("gmv", "GMV", "SUM(支付金额)", "sum", "v2.0"),
            ("refund_rate", "退款率", "SUM(成功退款金额)/SUM(支付金额)", "ratio", "v2.0"),
            ("roi", "投入产出比", "SUM(总成交金额)/SUM(花费)", "ratio", "v2.0"),
            ("conversion_rate", "转化率", "SUM(成交买家数)/SUM(商品访客数)", "ratio", "v2.0"),
        ]
        conn.executemany("INSERT OR IGNORE INTO metric_definitions(metric_code,label,formula,aggregation,version) VALUES (?,?,?,?,?)", metric_rows)
    for path in DB_PATHS.values():
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL UNIQUE,
                    row_count INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                )"""
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(imports)").fetchall()}
            if "account_id" not in columns:
                conn.execute("ALTER TABLE imports ADD COLUMN account_id INTEGER")
            if "import_status" not in columns:
                conn.execute("ALTER TABLE imports ADD COLUMN import_status TEXT NOT NULL DEFAULT 'succeeded'")
            migrate_import_uniqueness(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_imports_store_date ON imports(store_name, business_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_imports_account_date ON imports(account_id, business_date)")
    with sqlite3.connect(ACCOUNT_DB) as accounts:
        legacy_names = set()
        for path in DB_PATHS.values():
            with sqlite3.connect(path) as conn:
                legacy_names.update(row[0] for row in conn.execute("SELECT DISTINCT store_name FROM imports WHERE store_name <> ''"))
        for name in legacy_names:
            accounts.execute("INSERT OR IGNORE INTO accounts(account_name, store_name, created_at) VALUES (?, ?, ?)", (name, name, datetime.now().isoformat(timespec="seconds")))
        for path in DB_PATHS.values():
            with sqlite3.connect(path) as conn:
                for account_id, store_name in accounts.execute("SELECT id, store_name FROM accounts").fetchall():
                    conn.execute("UPDATE imports SET account_id = ? WHERE account_id IS NULL AND store_name = ?", (account_id, store_name))


def list_accounts():
    with sqlite3.connect(ACCOUNT_DB) as conn:
        rows = conn.execute("SELECT id, account_name, store_name, created_at FROM accounts ORDER BY id").fetchall()
    return [{"id": r[0], "account_name": r[1], "store_name": r[2], "created_at": r[3]} for r in rows]


def create_account(account_name, store_name=None):
    account_name = (account_name or "").strip()
    store_name = (store_name or account_name).strip()
    if not account_name:
        raise ValueError("账号名称不能为空")
    with sqlite3.connect(ACCOUNT_DB) as conn:
        cur = conn.execute("INSERT INTO accounts(account_name, store_name, created_at) VALUES (?, ?, ?)", (account_name, store_name, datetime.now().isoformat(timespec="seconds")))
        account_id = cur.lastrowid
    return {"id": account_id, "account_name": account_name, "store_name": store_name}


def account_by_id(account_id):
    with sqlite3.connect(ACCOUNT_DB) as conn:
        row = conn.execute("SELECT id, account_name, store_name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return {"id": row[0], "account_name": row[1], "store_name": row[2]} if row else None


def target_rows(account_id, month):
    with sqlite3.connect(ACCOUNT_DB) as conn:
        rows = conn.execute(
            "SELECT metric_id, custom_value, updated_at FROM metric_targets WHERE account_id = ? AND month = ?",
            (account_id, month),
        ).fetchall()
    custom = {r[0]: {"value": r[1], "updated_at": r[2]} for r in rows}
    result = []
    for metric_id, definition in TARGET_DEFINITIONS.items():
        item = {"metric_id": metric_id, **definition}
        item["custom_value"] = custom.get(metric_id, {}).get("value")
        item["effective_value"] = item["custom_value"] if item["custom_value"] is not None else item["default_value"]
        item["value_source"] = "custom" if item["custom_value"] is not None else ("default" if item["default_value"] is not None else "unset")
        result.append(item)
    return result


def aggregate_results(results):
    shop = [f for f in results if "生意参谋" in f.get("source", "")]
    ads = [f for f in results if "无界" in f.get("source", "")]
    def total(items, key):
        return sum(float((f.get("metrics") or {}).get(key) or 0) for f in items)
    gmv = total(shop, "gmv")
    refunds = total(shop, "successful_refund_amount")
    units = float(total(shop, "paid_units"))
    buyers = float(total(shop, "paid_buyers"))
    visitors = float(total(shop, "visitors"))
    spend = total(ads, "spend")
    deals = total(ads, "total_deal_amount")
    clicks = float(total(ads, "clicks"))
    impressions = float(total(ads, "impressions"))
    deal_orders = float(total(ads, "total_deal_orders"))
    deal_people = float(total(ads, "deal_people"))
    total_cart = float(total(ads, "total_cart_count"))
    return {
        "gmv": round(gmv, 2), "gsv": round(gmv - refunds, 2), "successful_refund_amount": round(refunds, 2),
        "paid_units": int(units) if units.is_integer() else units, "spend": round(spend, 2),
        "total_deal_amount": round(deals, 2), "clicks": int(clicks) if clicks.is_integer() else clicks,
        "impressions": int(impressions) if impressions.is_integer() else impressions,
        "total_deal_orders": int(deal_orders) if deal_orders.is_integer() else deal_orders,
        "deal_people": int(deal_people) if deal_people.is_integer() else deal_people,
        "total_cart_count": int(total_cart) if total_cart.is_integer() else total_cart,
        "paid_buyers": int(buyers) if buyers.is_integer() else buyers,
        "visitors": int(visitors) if visitors.is_integer() else visitors,
        "refund_rate": round(refunds / gmv, 8) if gmv else None,
        "conversion_rate": round(buyers / visitors, 8) if visitors else None,
        "average_order_value": round(gmv / units, 2) if units else None,
        "ppc": round(spend / clicks, 2) if clicks else None,
        "click_rate": round(clicks / impressions, 8) if impressions else None,
        "click_conversion_rate": round(deal_orders / clicks, 8) if clicks else None,
        "roi": round(deals / spend, 2) if spend else None,
        "fee_ratio": round(spend / gmv, 8) if gmv else None,
        "total_days": len({f.get("date") for f in results if f.get("date")}),
    }


def target_status(value, target, compare_type, complete=True):
    if not complete or value is None or target is None:
        return {"state": "unknown", "difference": None, "progress": None, "text": "无法判断"}
    difference = value - target
    if compare_type == "higher":
        ok = value >= target
        progress = None if target == 0 else value / target
    elif compare_type == "lower":
        ok = value <= target
        progress = None if target == 0 else target / value if value else 1
    else:
        ok = value <= target
        progress = None if target == 0 else value / target
    text = (f"预算使用 {progress * 100:.1f}%" if compare_type == "budget" and progress is not None else ("达标" if ok else "未达标"))
    return {"state": "good" if ok else "bad", "difference": round(difference, 4), "progress": round(progress, 4) if progress is not None else None, "text": text}


def persist_unified_import(source_type, account_id, store_name, batch_id, filename, content, result, encoding=None):
    """Append a normalized, auditable copy without changing legacy source DBs."""
    digest = hashlib.sha256(content).hexdigest()
    records = result.get("records") or []
    dates = sorted({str(row.get("统计日期") or row.get("日期") or "").strip() for row in records if str(row.get("统计日期") or row.get("日期") or "").strip()})
    business_date = dates[0] if dates else result.get("date") or "unknown"
    store_id = f"account:{account_id}"
    imported_at = datetime.now().isoformat(timespec="seconds")
    with nullcontext(result['_connection']) if '_connection' in result else sqlite3.connect(UNIFIED_DB) as conn:
        source = conn.execute(
            "SELECT id FROM source_files WHERE account_id = ? AND sha256 = ?", (account_id, digest)
        ).fetchone()
        if source:
            return {"status": "duplicate", "batch_db_id": None}
        cur = conn.execute(
            "INSERT INTO source_files(account_id,store_id,source_type,original_name,storage_path,sha256,file_size,encoding,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (account_id, store_id, source_type, Path(filename).name, str(RAW_ROOT / str(account_id) / source_type / business_date / f"{digest}-{Path(filename).name}"), digest, len(content), encoding, imported_at),
        )
        source_file_id = cur.lastrowid
        conflict = conn.execute(
            "SELECT 1 FROM import_batches WHERE account_id = ? AND source_type = ? AND business_date_start = ? AND status IN ('succeeded','conflict') LIMIT 1",
            (account_id, source_type, business_date),
        ).fetchone()
        conflict = conflict or result.get('_legacy_conflict')
        status = "conflict" if conflict else "succeeded"
        cur = conn.execute(
            "INSERT INTO import_batches(batch_id,account_id,store_id,source_file_id,source_type,business_date_start,business_date_end,report_grain,schema_version,calculation_version,status,is_effective,row_count,error_count,imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, account_id, store_id, source_file_id, source_type, business_date, dates[-1] if dates else business_date, "daily", result.get("schema_version", "unknown"), "v2.0", status, 0 if conflict else 1, len(records), 0, imported_at),
        )
        batch_db_id = cur.lastrowid
        conn.execute('UPDATE import_batches SET attribution_window=?, headers_json=? WHERE id=?', (result.get('attribution_window') or 'unknown', json.dumps(result.get('headers', []), ensure_ascii=False), batch_db_id))
        error_count = 0
        for row_number, row in zip(result.get('row_numbers', range(2, len(records) + 2)), records):
            date = str(row.get("统计日期") or row.get("日期") or "").strip() or None
            def number(field):
                nonlocal error_count
                try:
                    value = nullable_number(row.get(field))
                    if value is None:
                        raise ValueError('missing value')
                    return value
                except (TypeError, ValueError):
                    error_count += 1
                    conn.execute("INSERT INTO import_errors(batch_id,row_number,field_name,raw_value,error_type,message,created_at) VALUES (?,?,?,?,?,?,?)", (batch_db_id, row_number, field, str(row.get(field) or ""), "invalid_number", f"{field} 不是有效数字", imported_at))
                    return None
            if source_type == "shengyicanmou":
                entity_type, entity_id = "product", str(row.get("商品ID") or "").strip() or None
                conn.execute("INSERT INTO raw_rows(batch_id,row_number,business_date,entity_type,entity_id,raw_payload,row_hash) VALUES (?,?,?,?,?,?,?)", (batch_db_id, row_number, date, entity_type, entity_id, json.dumps(row, ensure_ascii=False), hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()))
                if not entity_id:
                    error_count += 1
                    conn.execute("INSERT INTO import_errors(batch_id,row_number,field_name,raw_value,error_type,message,created_at) VALUES (?,?,?,?,?,?,?)", (batch_db_id, row_number, "商品ID", "", "missing_identifier", "商品ID不能为空", imported_at))
                if entity_id:
                    name = str(row.get("商品名称") or "").strip()
                    conn.execute("INSERT INTO products(account_id,store_id,product_id,product_name,first_seen_date,last_seen_date) VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,product_id) DO UPDATE SET product_name=excluded.product_name,last_seen_date=excluded.last_seen_date", (account_id, store_id, entity_id, name, date, date))
                    buyer_field = next(name for name in ('成交买家数量', '成交买家数', '支付买家数') if name in row)
                    conn.execute("""INSERT INTO daily_product_facts(account_id,store_id,business_date,product_id,batch_id,source_type,visitors,paid_buyers,paid_units,gmv,successful_refund_amount,raw_conversion_rate,quality_status,page_views,cart_people,cart_items) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (account_id, store_id, date, entity_id, batch_db_id, source_type, number("商品访客数"), number(buyer_field), number("支付件数"), number("支付金额"), number("成功退款金额"), nullable_number(row.get("商品支付转化率")), "valid", nullable_number(row.get("商品浏览量")), nullable_number(row.get("商品加购人数")), nullable_number(row.get("商品加购件数"))))
            else:
                entity_type, entity_id = "plan_product", str(row.get("主体ID") or "").strip() or None
                conn.execute("INSERT INTO raw_rows(batch_id,row_number,business_date,entity_type,entity_id,raw_payload,row_hash) VALUES (?,?,?,?,?,?,?)", (batch_db_id, row_number, date, entity_type, entity_id, json.dumps(row, ensure_ascii=False), hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()))
                product_id = str(row.get("主体ID") or "").strip()
                plan_id = str(row.get("计划ID") or "").strip()
                scene_id = str(row.get("场景ID") or "").strip()
                if not product_id or not plan_id or not scene_id:
                    error_count += 1
                    missing = "、".join(name for name, value in (("主体ID", product_id), ("计划ID", plan_id), ("场景ID", scene_id)) if not value)
                    conn.execute("INSERT INTO import_errors(batch_id,row_number,field_name,raw_value,error_type,message,created_at) VALUES (?,?,?,?,?,?,?)", (batch_db_id, row_number, missing, "", "missing_identifier", f"{missing}不能为空", imported_at))
                    continue
                conn.execute("INSERT INTO products(account_id,store_id,product_id,product_name,first_seen_date,last_seen_date) VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,product_id) DO UPDATE SET product_name=excluded.product_name,last_seen_date=excluded.last_seen_date", (account_id, store_id, product_id, str(row.get("主体名称") or "").strip(), date, date))
                conn.execute("INSERT INTO plans(account_id,store_id,plan_id,plan_name,scene_id,scene_name,first_seen_date,last_seen_date) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(account_id,plan_id,scene_id) DO UPDATE SET plan_name=excluded.plan_name,scene_name=excluded.scene_name,last_seen_date=excluded.last_seen_date", (account_id, store_id, plan_id, str(row.get("计划名字") or "").strip(), scene_id, str(row.get("场景名字") or "").strip(), date, date))
                conn.execute("""INSERT INTO daily_plan_product_facts(account_id,store_id,business_date,scene_id,scene_name,plan_id,plan_name,product_id,batch_id,source_type,impressions,clicks,spend,direct_deal_amount,indirect_deal_amount,total_deal_amount,direct_deal_orders,indirect_deal_orders,total_deal_orders,deal_people,total_cart_count,raw_roi,quality_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (account_id, store_id, date, scene_id, str(row.get("场景名字") or "").strip(), plan_id, str(row.get("计划名字") or "").strip(), product_id, batch_db_id, source_type, number("展现量"), number("点击量"), number("花费"), number("直接成交金额"), number("间接成交金额"), number("总成交金额"), number("直接成交笔数"), number("间接成交笔数"), number("总成交笔数"), number("成交人数"), number("总购物车数"), number("投入产出比"), "valid"))
        if error_count:
            conn.execute("UPDATE import_batches SET status='partial', is_effective=0, error_count=? WHERE id=?", (error_count, batch_db_id))
            for table in ('daily_product_facts', 'daily_plan_product_facts'):
                conn.execute(f"UPDATE {table} SET quality_status='invalid' WHERE batch_id=?", (batch_db_id,))
            status = "partial"
    return {"status": status, "batch_db_id": batch_db_id}


def save_import(source_type, account_id, store_name, batch_id, filename, content, result, encoding=None):
    digest = hashlib.sha256(content).hexdigest()
    db_path = DB_PATHS[source_type]
    with sqlite3.connect(db_path) as conn:
        if conn.execute("SELECT 1 FROM imports WHERE account_id = ? AND file_sha256 = ?", (account_id, digest)).fetchone():
            return {"duplicate": True, "sha256": digest, "result": result}
    date = result.get("date") or "unknown"
    raw_dir = RAW_ROOT / str(account_id) / source_type / date
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{digest}-{Path(filename).name}"
    raw_path.write_bytes(content)
    try:
        with sqlite3.connect(UNIFIED_DB) as conn:
            conn.execute('ATTACH DATABASE ? AS legacy', (str(db_path),))
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM legacy.imports WHERE account_id=? AND file_sha256=?', (account_id, digest)).fetchone():
                return {"duplicate": True, "sha256": digest, "result": result}
            legacy_conflict = conn.execute("SELECT 1 FROM legacy.imports WHERE account_id=? AND business_date=? AND import_status='succeeded'", (account_id, date)).fetchone()
            unified = persist_unified_import(source_type, account_id, store_name, batch_id, filename, content, {**result, '_connection': conn, '_legacy_conflict': bool(legacy_conflict)}, encoding)
            legacy_result = {key: value for key, value in result.items() if key not in ('records', 'row_numbers')}
            legacy_result["unified_status"] = unified.get("status")
            conn.execute(
                "INSERT INTO legacy.imports(batch_id, account_id, store_name, source_type, business_date, original_filename, file_path, file_sha256, row_count, result_json, imported_at, import_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, account_id, store_name, source_type, date, Path(filename).name, str(raw_path), digest, result.get("row_count", 0), json.dumps(legacy_result, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), unified.get("status", "succeeded")),
            )
    except Exception:
        raw_path.unlink(missing_ok=True)
        raise
    return {"duplicate": False, "sha256": digest, "result": result, "unified_status": unified.get("status")}


def query_unified(account_id, start_date, end_date, product_id=None, plan_id=None, scene_id=None):
    """Read the two grains independently; never join business facts to plan rows."""
    start = datetime.strptime(start_date, '%Y-%m-%d').date()
    end = datetime.strptime(end_date, '%Y-%m-%d').date()
    if end < start or (end - start).days >= 366:
        raise ValueError('日期范围无效，最多 366 天')
    result = {}
    with sqlite3.connect(UNIFIED_DB) as conn:
        conn.row_factory = sqlite3.Row
        for name, table in [('products', 'daily_product_facts'), ('plans', 'daily_plan_product_facts')]:
            conditions = ['f.account_id=?', 'f.business_date BETWEEN ? AND ?', 'b.is_effective=1', "b.status='succeeded'"]
            values = [account_id, start_date, end_date]
            for field, value in [('product_id', product_id), ('plan_id', plan_id), ('scene_id', scene_id)]:
                if value is not None and (name == 'plans' or field == 'product_id'):
                    conditions.append(f'f.{field}=?')
                    values.append(str(value))
            result[name] = [dict(r) for r in conn.execute(f"SELECT f.*, b.calculation_version, b.schema_version, b.attribution_window FROM {table} f JOIN import_batches b ON b.id=f.batch_id WHERE {' AND '.join(conditions)} ORDER BY f.business_date, f.id", values)]
        result['batches'] = [dict(r) for r in conn.execute('SELECT id, batch_id, source_type, status, is_effective, error_count, row_count, business_date_start, schema_version, calculation_version, attribution_window FROM import_batches WHERE account_id=? AND business_date_start BETWEEN ? AND ? ORDER BY id', (account_id, start_date, end_date))]
    return result


def unified_metrics(data):
    shop, ads = data['products'], data['plans']
    def total(rows, field):
        if not rows or any(r.get(field) is None for r in rows):
            return None
        return round(sum(r[field] for r in rows), 8)
    def ratio(numerator, denominator, precision=8):
        return round(numerator / denominator, precision) if numerator is not None and denominator else None
    gmv, refunds, units = (total(shop, f) for f in ('gmv', 'successful_refund_amount', 'paid_units'))
    spend, clicks, impressions = (total(ads, f) for f in ('spend', 'clicks', 'impressions'))
    # Unknown windows are safe only within one source batch, not across reports.
    windows = {r['attribution_window'] for r in ads}
    compatible = len(windows) == 1 and ('unknown' not in windows or len({r['batch_id'] for r in ads}) == 1)
    deals = total(ads, 'total_deal_amount') if compatible else None
    people_safe = len(shop) == 1
    buyers, visitors = (total(shop, f) if people_safe else None for f in ('paid_buyers', 'visitors'))
    return {
        'gmv': gmv, 'successful_refund_amount': refunds,
        'gsv': round(gmv - refunds, 2) if gmv is not None and refunds is not None else None,
        'paid_units': units, 'paid_buyers': buyers, 'visitors': visitors,
        'refund_rate': ratio(refunds, gmv), 'average_order_value': ratio(gmv, units, 2),
        'conversion_rate': ratio(buyers, visitors), 'spend': spend, 'clicks': clicks,
        'impressions': impressions, 'ppc': ratio(spend, clicks, 2),
        'click_rate': ratio(clicks, impressions), 'fee_ratio': ratio(spend, gmv),
        'attributed_deal_amount': deals, 'roi': ratio(deals, spend, 2),
        'deal_people': total(ads, 'deal_people') if len(ads) == 1 else None,
        'source_semantics': {'gmv': '生意参谋实际支付', 'attributed_deal_amount': '无界归因成交'},
        'quality': {'people_deduplicated': people_safe, 'attribution_windows_compatible': compatible,
                    'attribution_windows': sorted(windows), 'missing_sources': [source for source, rows in [('shengyicanmou', shop), ('wujie', ads)] if not rows]},
        'calculation_version': 'v2.0',
    }


def query_plan_totals(rows):
    groups = {}
    for row in rows:
        key = (row['business_date'], row['scene_id'], row['plan_id'], row['batch_id'])
        groups.setdefault(key, []).append(row)
    result = []
    for group in groups.values():
        metrics = unified_metrics({'products': [], 'plans': group})
        result.append({**{field: group[0][field] for field in ('business_date', 'scene_id', 'scene_name', 'plan_id', 'plan_name', 'batch_id', 'attribution_window')}, 'product_ids': [r['product_id'] for r in group], 'metrics': metrics})
    return result


def list_baby_products(account_id, keyword="", limit=100):
    keyword = str(keyword or "").strip()
    like = f"%{keyword}%"
    with sqlite3.connect(UNIFIED_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT p.product_id, p.product_name, p.first_seen_date, p.last_seen_date,
                      EXISTS(SELECT 1 FROM daily_product_facts f JOIN import_batches b ON b.id=f.batch_id
                             WHERE f.account_id=p.account_id AND f.product_id=p.product_id
                               AND b.status='succeeded' AND b.is_effective=1) AS has_business,
                      EXISTS(SELECT 1 FROM daily_plan_product_facts f JOIN import_batches b ON b.id=f.batch_id
                             WHERE f.account_id=p.account_id AND f.product_id=p.product_id
                               AND b.status='succeeded' AND b.is_effective=1) AS has_promotion
                 FROM products p
                WHERE p.account_id=? AND (?='' OR p.product_id LIKE ? OR COALESCE(p.product_name,'') LIKE ?)
                ORDER BY p.last_seen_date DESC, p.product_id
                LIMIT ?""",
            (account_id, keyword, like, like, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(row) for row in rows]


def baby_board(account_id, product_id, start_date, end_date):
    data = query_unified(account_id, start_date, end_date, product_id=product_id)
    shop, ads = data['products'], data['plans']
    with sqlite3.connect(UNIFIED_DB) as conn:
        conn.row_factory = sqlite3.Row
        product = conn.execute(
            "SELECT product_id, product_name, first_seen_date, last_seen_date FROM products WHERE account_id=? AND product_id=?",
            (account_id, str(product_id)),
        ).fetchone()
    if not product:
        raise ValueError('当前店铺未找到该商品 ID')

    def total(rows, field):
        values = [row.get(field) for row in rows]
        if not values or any(value is None for value in values):
            return None
        return round(sum(values), 8)

    def ratio(numerator, denominator, precision=8):
        return round(numerator / denominator, precision) if numerator is not None and denominator else None

    visitors = total(shop, 'visitors')
    page_views = total(shop, 'page_views')
    cart_people = total(shop, 'cart_people')
    cart_items = total(shop, 'cart_items')
    paid_buyers = total(shop, 'paid_buyers')
    paid_units = total(shop, 'paid_units')
    gmv = total(shop, 'gmv')
    refunds = total(shop, 'successful_refund_amount')
    business = {
        'visitors': visitors, 'page_views': page_views, 'cart_people': cart_people,
        'cart_items': cart_items, 'paid_buyers': paid_buyers, 'paid_units': paid_units,
        'gmv': gmv, 'successful_refund_amount': refunds,
        'conversion_rate': ratio(paid_buyers, visitors), 'refund_rate': ratio(refunds, gmv),
    }

    windows = {row['attribution_window'] for row in ads}
    attribution_compatible = len(windows) == 1 and ('unknown' not in windows or len({row['batch_id'] for row in ads}) == 1)
    impressions = total(ads, 'impressions')
    clicks = total(ads, 'clicks')
    spend = total(ads, 'spend')
    attributed = total(ads, 'total_deal_amount') if attribution_compatible else None
    promotion = None if not ads else {
        'impressions': impressions, 'clicks': clicks, 'spend': spend,
        'click_rate': ratio(clicks, impressions), 'ppc': ratio(spend, clicks, 2),
        'attributed_deal_amount': attributed, 'roi': ratio(attributed, spend, 2),
        'total_deal_orders': total(ads, 'total_deal_orders'),
    }

    start = datetime.strptime(start_date, '%Y-%m-%d').date()
    end = datetime.strptime(end_date, '%Y-%m-%d').date()
    by_date = {}
    for offset in range((end - start).days + 1):
        date = (start + timedelta(days=offset)).isoformat()
        by_date[date] = {'date': date, 'business': None, 'promotion': None}
    for date in by_date:
        daily_shop = [row for row in shop if row['business_date'] == date]
        daily_ads = [row for row in ads if row['business_date'] == date]
        if daily_shop:
            daily_visitors = total(daily_shop, 'visitors')
            daily_buyers = total(daily_shop, 'paid_buyers')
            by_date[date]['business'] = {
                'visitors': daily_visitors, 'page_views': total(daily_shop, 'page_views'),
                'paid_buyers': daily_buyers, 'gmv': total(daily_shop, 'gmv'),
                'conversion_rate': ratio(daily_buyers, daily_visitors),
            }
        if daily_ads:
            daily_spend = total(daily_ads, 'spend')
            daily_deals = total(daily_ads, 'total_deal_amount')
            by_date[date]['promotion'] = {
                'impressions': total(daily_ads, 'impressions'), 'clicks': total(daily_ads, 'clicks'),
                'spend': daily_spend, 'attributed_deal_amount': daily_deals,
                'roi': ratio(daily_deals, daily_spend, 2),
            }

    plan_groups = {}
    for row in ads:
        key = (row['scene_id'], row['plan_id'], row['attribution_window'])
        plan_groups.setdefault(key, []).append(row)
    plans = []
    for group in plan_groups.values():
        group_spend = total(group, 'spend')
        group_deals = total(group, 'total_deal_amount')
        plans.append({
            'scene_id': group[0]['scene_id'], 'scene_name': group[0]['scene_name'],
            'plan_id': group[0]['plan_id'], 'plan_name': group[0]['plan_name'],
            'attribution_window': group[0]['attribution_window'],
            'start_date': min(row['business_date'] for row in group),
            'end_date': max(row['business_date'] for row in group),
            'impressions': total(group, 'impressions'), 'clicks': total(group, 'clicks'),
            'spend': group_spend, 'attributed_deal_amount': group_deals,
            'click_rate': ratio(total(group, 'clicks'), total(group, 'impressions')),
            'ppc': ratio(group_spend, total(group, 'clicks'), 2), 'roi': ratio(group_deals, group_spend, 2),
        })
    plans.sort(key=lambda row: (-(row['spend'] or 0), row['plan_id']))
    multi_day = start_date != end_date
    return {
        'product': dict(product), 'start_date': start_date, 'end_date': end_date,
        'business': business, 'promotion': promotion, 'trend': list(by_date.values()), 'plans': plans,
        'quality': {
            'has_business': bool(shop), 'has_promotion': bool(ads),
            'people_scope': 'daily_sum_not_period_deduplicated' if multi_day else 'single_day',
            'people_note': '跨日人数为每日数值累加，不等于周期去重人数。' if multi_day else '单日人数口径。',
            'attribution_windows_compatible': attribution_compatible,
            'attribution_windows': sorted(windows),
            'promotion_empty_message': None if ads else '无推广记录',
        },
        'source_semantics': {'gmv': '生意参谋实际支付金额', 'attributed_deal_amount': '无界归因成交金额'},
    }


def stored_results(source_type, account_id, date):
    db_path = DB_PATHS[source_type]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT result_json FROM imports WHERE account_id = ? AND business_date = ? AND import_status = 'succeeded' ORDER BY imported_at DESC",
            (account_id, date),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def stored_results_range(source_type, account_id, start_date, end_date):
    """Return the latest saved batch for each business date in the range."""
    db_path = DB_PATHS[source_type]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT i.result_json
               FROM imports i
              WHERE i.account_id = ?
                AND i.import_status = 'succeeded'
                AND i.business_date >= ?
                AND i.business_date <= ?
                AND NOT EXISTS (
                    SELECT 1 FROM imports newer
                     WHERE newer.account_id = i.account_id
                       AND newer.business_date = i.business_date
                       AND newer.import_status = 'succeeded'
                       AND (newer.imported_at > i.imported_at OR (newer.imported_at = i.imported_at AND newer.id > i.id))
                )
              ORDER BY i.business_date ASC""",
            (account_id, start_date, end_date),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def upload_records(source_type, account_id):
    db_path = DB_PATHS[source_type]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT batch_id, business_date, original_filename, row_count, file_sha256, imported_at, import_status FROM imports WHERE account_id = ? ORDER BY imported_at DESC",
            (account_id,),
        ).fetchall()
    return [
        {"batch_id": r[0], "date": r[1], "filename": r[2], "row_count": r[3], "sha256": r[4], "imported_at": r[5], 'status': r[6]}
        for r in rows
    ]


def delete_import(source_type, account_id, file_sha256):
    """Delete one account-owned import and remove its raw file when unreferenced."""
    if source_type not in DB_PATHS or not file_sha256:
        raise ValueError("来源或文件指纹无效")
    db_path = DB_PATHS[source_type]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT file_path FROM imports WHERE account_id = ? AND file_sha256 = ?",
            (account_id, file_sha256),
        ).fetchone()
        if not row:
            return False
        file_path = row[0]
        conn.execute(
            "DELETE FROM imports WHERE account_id = ? AND file_sha256 = ?",
            (account_id, file_sha256),
        )
        still_used = conn.execute(
            "SELECT 1 FROM imports WHERE file_path = ? LIMIT 1",
            (file_path,),
        ).fetchone()
    if not still_used:
        with sqlite3.connect(UNIFIED_DB) as conn:
            source = conn.execute('SELECT id FROM source_files WHERE account_id=? AND sha256=?', (account_id, file_sha256)).fetchone()
            if source:
                conn.execute("UPDATE import_batches SET status='deleted', is_effective=0 WHERE source_file_id=?", (source[0],))
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass
    return True


init_databases()


def hash_password(password):
    if not password or len(password) < 8:
        raise ValueError("密码至少需要 8 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180000)
    return f"pbkdf2_sha256$180000${salt.hex()}${digest.hex()}"


def hash_password_unrestricted(password):
    """Registration hash: no minimum-length policy at this stage."""
    salt = secrets.token_bytes(16)
    rounds = 180000
    digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def user_count():
    with sqlite3.connect(ACCOUNT_DB) as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE active = 1").fetchone()[0]


def user_public(row):
    return {"id": row[0], "username": row[1], "role": row[2]}


def list_users():
    with sqlite3.connect(ACCOUNT_DB) as conn:
        rows = conn.execute(
            "SELECT id, username, role, active, created_at FROM users ORDER BY id"
        ).fetchall()
    return [
        {"id": r[0], "username": r[1], "role": r[2], "active": bool(r[3]), "created_at": r[4]}
        for r in rows
    ]


def session_user(token):
    if not token:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(ACCOUNT_DB) as conn:
        row = conn.execute("SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires_at > ? AND u.active = 1", (token, now)).fetchone()
    return user_public(row) if row else None


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now.fromtimestamp(now.timestamp() + SESSION_DAYS * 86400)
    with sqlite3.connect(ACCOUNT_DB) as conn:
        conn.execute("INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)", (token, user_id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
    return token


def get_user_from_request(handler):
    from http.cookies import SimpleCookie
    cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    token = cookie.get("well_session")
    return session_user(token.value if token else None)


def require_user(handler):
    user = get_user_from_request(handler)
    if not user:
        handler.send_json({"ok": False, "error": "请先登录"}, 401)
        return None
    return user


def decimal_number(value):
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0.0
    if text.endswith("%"):
        return float(text[:-1]) / 100
    try:
        return float(text)
    except ValueError:
        return 0.0


def nullable_number(value):
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text or text == "-":
        return None
    percent = text.endswith('%')
    number = float(text[:-1] if percent else text)
    if not math.isfinite(number):
        raise ValueError('非有限数值')
    return number / 100 if percent else number


def validate_business_dates(filename, dates):
    if not dates:
        raise ValueError(f"{filename}：报表内容缺少业务日期")
    for value in dates:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{filename}：业务日期格式异常：{value}") from exc
    if len(dates) > 1:
        raise ValueError(f"{filename}：同一文件包含多个业务日期：{', '.join(sorted(dates))}")


def report_records(filename, data, source):
    header = data[0]
    if len(header) != len(set(header)):
        raise ValueError(f'{filename}：报表存在重复表头')
    required = {'统计日期', '商品ID', '商品名称', '支付金额', '成功退款金额', '支付件数', '商品访客数'} if source == 'shengyicanmou' else {'日期', '场景ID', '场景名字', '计划ID', '计划名字', '主体ID', '主体名称', '主体类型', '展现量', '点击量', '花费', '总成交金额', '总成交笔数', '成交人数', '直接成交金额', '间接成交金额', '直接成交笔数', '间接成交笔数', '总购物车数', '投入产出比'}
    missing = required - set(header)
    if missing:
        raise ValueError(f'{filename}：缺少字段：{", ".join(sorted(missing))}')
    records, keys = [], set()
    for index, values in enumerate(data[1:], 2):
        if not any(cell.strip() for cell in values):
            continue
        if len(values) != len(header):
            raise ValueError(f'{filename}：第 {index} 行列数异常（{len(values)}/{len(header)}）')
        row = dict(zip(header, values))
        date = row['统计日期' if source == 'shengyicanmou' else '日期'].strip()
        validate_business_dates(filename, {date})
        if date > datetime.now().date().isoformat():
            raise ValueError(f'{filename}：第 {index} 行业务日期在未来：{date}')
        ids = ['商品ID'] if source == 'shengyicanmou' else ['主体ID', '计划ID', '场景ID']
        for field in ids:
            row[field] = row[field].strip()
            if not row[field].isascii() or not row[field].isdigit():
                raise ValueError(f'{filename}：第 {index} 行 {field} 必须是完整数字字符串，不能为科学计数法或小数')
        if source == 'wujie' and row['主体类型'].strip() != '商品':
            raise ValueError(f'{filename}：第 {index} 行主体类型不是商品')
        key = (date, *(row[field] for field in ids))
        if key in keys:
            raise ValueError(f'{filename}：第 {index} 行业务键重复：{key}')
        keys.add(key)
        records.append(row)
    if not records:
        raise ValueError(f'{filename}：无有效商品数据行')
    return records


def read_csv_file_with_encoding(path):
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = raw.decode(encoding)
            rows = list(csv.reader(io.StringIO(text)))
            if rows and any(any(cell.strip() for cell in row) for row in rows[:20]):
                return rows, encoding
        except (UnicodeDecodeError, csv.Error):
            continue
    raise ValueError("无法识别 CSV 编码")


def read_csv_file(path):
    return read_csv_file_with_encoding(path)[0]


def convert_xls(path, temp_dir):
    if not Path(SOFFICE).exists():
        raise ValueError("未找到 LibreOffice/soffice，暂时无法读取 XLS 文件")
    result = subprocess.run(
        [SOFFICE, "--headless", "--convert-to", "csv", "--outdir", str(temp_dir), str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    converted = temp_dir / (path.stem + ".csv")
    if result.returncode != 0 or not converted.exists():
        raise ValueError("XLS 转换失败：" + (result.stderr or result.stdout).strip())
    return read_csv_file(converted)


def find_header(rows, required, any_of=None):
    for index, row in enumerate(rows[:12]):
        normalized = [cell.strip() for cell in row]
        if all(name in normalized for name in required) and (not any_of or any(name in normalized for name in any_of)):
            return index, normalized
    raise ValueError("未找到可识别的报表表头")


def analyse_file(filename, data):
    buyer_field = next((name for name in ("成交买家数量", "成交买家数", "支付买家数") if name in data[0]), None)
    if buyer_field and {"支付金额", "成功退款金额", "支付件数", "商品访客数"}.issubset(set(data[0])):
        header = data[0]
        rows = report_records(filename, data, 'shengyicanmou')
        gmv = sum(decimal_number(row.get("支付金额")) for row in rows)
        refunds = sum(decimal_number(row.get("成功退款金额")) for row in rows)
        units = sum(decimal_number(row.get("支付件数")) for row in rows)
        buyers = sum(decimal_number(row.get(buyer_field)) for row in rows)
        visitors = sum(decimal_number(row.get("商品访客数")) for row in rows)
        dates = {str(row.get("统计日期", "")).strip() for row in rows if str(row.get("统计日期", "")).strip()}
        validate_business_dates(filename, dates)
        return {
            "source": "生意参谋商品日报",
            "date": next(iter(dates), ""),
            "schema_version": "shengyicanmou-product-v1",
            "row_count": len(rows),
            "headers": header,
            "records": rows,
            "metrics": {
                "gmv": round(gmv, 2),
                "successful_refund_amount": round(refunds, 2),
                "gsv": round(gmv - refunds, 2),
                "refund_rate": round(refunds / gmv, 8) if gmv else None,
                "visitors": int(visitors) if visitors.is_integer() else visitors,
                "paid_buyers": int(buyers) if buyers.is_integer() else buyers,
                "paid_units": int(units) if units.is_integer() else units,
                "conversion_rate": round(buyers / visitors, 8) if visitors else None,
                "average_order_value": round(gmv / units, 2) if units else None,
            },
        }
    if {"花费", "投入产出比", "总成交金额", "计划ID"}.issubset(set(data[0])):
        header = data[0]
        rows = report_records(filename, data, 'wujie')
        # 新版无界商品报表同时提供计划、商品和成交漏斗字段。保留旧报表
        # 的基础字段兼容性，但所有可用汇总指标优先按新表原始字段重算。
        impressions = sum(decimal_number(row.get("展现量")) for row in rows)
        spend = sum(decimal_number(row.get("花费")) for row in rows)
        clicks = sum(decimal_number(row.get("点击量")) for row in rows)
        deals = sum(decimal_number(row.get("总成交金额")) for row in rows)
        direct = sum(decimal_number(row.get("直接成交金额")) for row in rows)
        indirect = sum(decimal_number(row.get("间接成交金额")) for row in rows)
        deal_orders = sum(decimal_number(row.get("总成交笔数")) for row in rows)
        direct_orders = sum(decimal_number(row.get("直接成交笔数")) for row in rows)
        indirect_orders = sum(decimal_number(row.get("间接成交笔数")) for row in rows)
        deal_people = sum(decimal_number(row.get("成交人数")) for row in rows)
        total_cart = sum(decimal_number(row.get("总购物车数")) for row in rows)
        favorites = sum(decimal_number(row.get("总收藏数")) for row in rows)
        dates = {str(row.get("日期", "")).strip() for row in rows if str(row.get("日期", "")).strip()}
        validate_business_dates(filename, dates)
        required_product = {"主体ID", "主体名称"}.issubset(set(header))
        if filename.lower().endswith(".csv") and not required_product:
            # The legacy plan report remains readable, but it cannot populate
            # the product-plan relation required by the unified fact layer.
            raise ValueError(f"{filename}：缺少主体ID/主体名称，无法作为无界商品报表导入")
        return {
            "source": "无界商品报表",
            "date": next(iter(dates), ""),
            "schema_version": "wujie-product-v1",
            "row_count": len(rows),
            "headers": header,
            "records": rows,
            "metrics": {
                "impressions": int(impressions) if impressions.is_integer() else impressions,
                "spend": round(spend, 2),
                "total_deal_amount": round(deals, 2),
                "direct_deal_amount": round(direct, 2),
                "indirect_deal_amount": round(indirect, 2),
                "clicks": int(clicks) if clicks.is_integer() else clicks,
                "click_rate": round(clicks / impressions, 8) if impressions else None,
                "ppc": round(spend / clicks, 2) if clicks else None,
                "cpm": round(spend / impressions * 1000, 2) if impressions else None,
                "total_deal_orders": int(deal_orders) if deal_orders.is_integer() else deal_orders,
                "direct_deal_orders": int(direct_orders) if direct_orders.is_integer() else direct_orders,
                "indirect_deal_orders": int(indirect_orders) if indirect_orders.is_integer() else indirect_orders,
                "deal_people": int(deal_people) if deal_people.is_integer() else deal_people,
                "click_conversion_rate": round(deal_orders / clicks, 8) if clicks else None,
                "total_cart_count": int(total_cart) if total_cart.is_integer() else total_cart,
                "total_favorite_count": int(favorites) if favorites.is_integer() else favorites,
                "roi": round(deals / spend, 2) if spend else None,
                "plan_roi_values": [decimal_number(row.get("投入产出比")) for row in rows],
            },
        }
    raise ValueError(f"{filename}：暂不支持该报表字段结构")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.send_json({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
        if parsed.path == "/api/me":
            user = get_user_from_request(self)
            if not user:
                return self.send_json({"ok": False, "setup_required": user_count() == 0, "error": "未登录"}, 401)
            return self.send_json({"ok": True, "user": user})
        # Static HTML/CSS/JS must remain publicly loadable so the login and
        # registration screen can render before a session exists. API routes
        # below remain protected by the session check.
        if not parsed.path.startswith("/api/"):
            path = STATIC / ("index.html" if parsed.path == "/" else parsed.path.lstrip("/"))
            if not path.exists() or not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            content_type = "text/html; charset=utf-8" if path.suffix.lower() in (".html", ".htm") else "text/plain; charset=utf-8"
            if path.suffix.lower() == ".css":
                content_type = "text/css; charset=utf-8"
            elif path.suffix.lower() == ".js":
                content_type = "application/javascript; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        user = require_user(self)
        if not user:
            return
        if parsed.path == "/api/accounts":
            return self.send_json({"ok": True, "accounts": list_accounts()})
        if parsed.path == "/api/users":
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅管理员可查看登录用户"}, 403)
            return self.send_json({"ok": True, "users": list_users()})
        if parsed.path == "/api/targets":
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query)
            account = account_by_id(query.get("account_id", [""])[0]) if query.get("account_id", [""])[0] else None
            month = query.get("month", [""])[0]
            if not account or len(month) != 7:
                return self.send_json({"ok": False, "error": "需要有效 account_id 和 YYYY-MM 月份"}, 400)
            return self.send_json({"ok": True, "account_id": account["id"], "month": month, "targets": target_rows(account["id"], month)})
        if parsed.path == "/api/baby-products":
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query)
            account = account_by_id(query.get("account_id", [""])[0]) if query.get("account_id", [""])[0] else None
            if not account:
                return self.send_json({"ok": False, "error": "请先选择有效店铺"}, 400)
            try:
                products = list_baby_products(account['id'], query.get('keyword', [''])[0])
                return self.send_json({"ok": True, "account_id": account['id'], "products": products})
            except (ValueError, TypeError) as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/baby-board":
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query)
            account = account_by_id(query.get("account_id", [""])[0]) if query.get("account_id", [""])[0] else None
            product_id = query.get('product_id', [''])[0]
            start_date = query.get('start_date', [''])[0]
            end_date = query.get('end_date', [start_date])[0]
            if not account or not product_id or not start_date or not end_date:
                return self.send_json({"ok": False, "error": "需要有效店铺、商品 ID 和日期范围"}, 400)
            try:
                output = baby_board(account['id'], product_id, start_date, end_date)
                return self.send_json({"ok": True, "account_id": account['id'], "store": account['store_name'], "data": output})
            except (ValueError, TypeError) as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/dashboard":
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query)
            account = account_by_id(query.get("account_id", [""])[0]) if query.get("account_id", [""])[0] else None
            store_name = query.get("store", ["本地测试店铺"])[0]
            if not account:
                account = next((a for a in list_accounts() if a["store_name"] == store_name), None)
            if not account:
                return self.send_json({"ok": False, "error": "请先选择有效账号"}, 400)
            start_date = query.get("start_date", [query.get("date", [""])[0]])[0]
            end_date = query.get("end_date", [start_date])[0]
            if not start_date or not end_date:
                return self.send_json({"ok": False, "error": "缺少 start_date 或 end_date"}, 400)
            try:
                start = datetime.strptime(start_date, "%Y-%m-%d").date()
                end = datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                return self.send_json({"ok": False, "error": "日期格式必须是 YYYY-MM-DD"}, 400)
            if end < start:
                return self.send_json({"ok": False, "error": "结束日期不能早于开始日期"}, 400)
            if (end - start).days + 1 > 31:
                return self.send_json({"ok": False, "error": "日期区间最多选择 31 天"}, 400)
            files = stored_results_range("shengyicanmou", account["id"], start_date, end_date) + stored_results_range("wujie", account["id"], start_date, end_date)
            month_start = end.replace(day=1).isoformat()
            mtd_files = stored_results_range("shengyicanmou", account["id"], month_start, end_date) + stored_results_range("wujie", account["id"], month_start, end_date)
            month_days = (end.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1) - end.replace(day=1)
            mtd = aggregate_results(mtd_files)
            targets = target_rows(account["id"], end.strftime("%Y-%m"))
            target_map = {item["metric_id"]: item for item in targets}
            for metric_id, item in target_map.items():
                value = mtd.get(metric_id)
                complete = True
                if item["mtd_kind"] == "dedupe":
                    complete = False
                if item["mtd_kind"] == "cumulative" and item["effective_value"] is not None:
                    expected = item["effective_value"] * ((end.day) / month_days.days)
                    status = target_status(value, expected, item["compare_type"], complete)
                    status["expected_value"] = round(expected, 4)
                else:
                    status = target_status(value, item["effective_value"], item["compare_type"], complete)
                item["actual_value"] = value
                item["status"] = status
            available_dates = {f.get("date") for f in mtd_files if f.get("date")}
            expected_dates = {(month_start if end.day == 1 else month_start)}
            complete_days = len(available_dates)
            mtd["complete_days"] = complete_days
            mtd["missing_days_possible"] = max(0, end.day - complete_days)
            return self.send_json({"ok": True, "account_id": account["id"], "account_name": account["account_name"], "store": account["store_name"], "start_date": start_date, "end_date": end_date, "files": files, "mtd": {"month": end.strftime("%Y-%m"), "start_date": month_start, "end_date": end_date, "metrics": mtd, "targets": list(target_map.values())}})
        if parsed.path.startswith('/api/unified/'):
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query)
            account = account_by_id(query.get("account_id", [""])[0]) if query.get("account_id", [""])[0] else None
            start_date = query.get("start_date", [""])[0]
            end_date = query.get("end_date", [start_date])[0]
            if not account or not start_date or not end_date:
                return self.send_json({"ok": False, "error": "需要有效 account_id、start_date 和 end_date"}, 400)
            try:
                data = query_unified(account['id'], start_date, end_date, query.get('product_id', [None])[0], query.get('plan_id', [None])[0], query.get('scene_id', [None])[0])
                kind = parsed.path.rsplit('/', 1)[-1]
                output = {'products': data['products'], 'plans': data['plans'], 'plan-totals': query_plan_totals(data['plans']), 'metrics': unified_metrics(data), 'batches': data['batches'], 'linked': data}.get(kind)
                if output is None:
                    return self.send_json({'ok': False, 'error': '未知统一查询接口'}, 404)
                return self.send_json({'ok': True, 'account_id': account['id'], 'store_id': f"account:{account['id']}", 'data': output, 'batches': data['batches']})
            except (ValueError, TypeError) as exc:
                return self.send_json({'ok': False, 'error': str(exc)}, 400)
        if parsed.path == "/api/upload-records":
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query)
            source_type = query.get("source", [""])[0]
            account = account_by_id(query.get("account_id", [""])[0]) if query.get("account_id", [""])[0] else None
            store_name = query.get("store", ["本地测试店铺"])[0]
            if not account:
                account = next((a for a in list_accounts() if a["store_name"] == store_name), None)
            if source_type not in DB_PATHS:
                return self.send_json({"ok": False, "error": "source 必须是 shengyicanmou 或 wujie"}, 400)
            if not account:
                return self.send_json({"ok": False, "error": "请先选择有效账号"}, 400)
            return self.send_json({"ok": True, "source": source_type, "account_id": account["id"], "records": upload_records(source_type, account["id"])})
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/register":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                username = (payload.get("username") or "").strip()
                if not username:
                    return self.send_json({"ok": False, "error": "用户名不能为空"}, 400)
                password = payload.get("password") or ""
                with sqlite3.connect(ACCOUNT_DB) as conn:
                    cur = conn.execute(
                        "INSERT INTO users(username, password_hash, role, active, created_at) VALUES (?, ?, 'user', 1, ?)",
                        (username, hash_password_unrestricted(password), datetime.now().isoformat(timespec="seconds")),
                    )
                    user_id = cur.lastrowid
                token = create_session(user_id)
                return self.send_json({"ok": True, "user": {"id": user_id, "username": username, "role": "user"}}, cookie=f"well_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}")
            except sqlite3.IntegrityError:
                return self.send_json({"ok": False, "error": "用户名已存在"}, 409)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/setup":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if user_count() > 0:
                    return self.send_json({"ok": False, "error": "管理员已经初始化"}, 409)
                username = (payload.get("username") or "").strip()
                if not username:
                    raise ValueError("用户名不能为空")
                with sqlite3.connect(ACCOUNT_DB) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("SELECT COUNT(*) FROM users WHERE active = 1").fetchone()[0] > 0:
                        return self.send_json({"ok": False, "error": "管理员已经初始化"}, 409)
                    cur = conn.execute("INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)", (username, hash_password(payload.get("password", "")), datetime.now().isoformat(timespec="seconds")))
                    user_id = cur.lastrowid
                token = create_session(user_id)
                return self.send_json({"ok": True, "user": {"username": username, "role": "admin"}}, cookie=f"well_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}")
            except sqlite3.IntegrityError:
                return self.send_json({"ok": False, "error": "用户名已存在"}, 409)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                username = (payload.get("username") or "").strip()
                with sqlite3.connect(ACCOUNT_DB) as conn:
                    row = conn.execute("SELECT id, username, password_hash, role FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
                if not row or not verify_password(payload.get("password", ""), row[2]):
                    return self.send_json({"ok": False, "error": "用户名或密码错误"}, 401)
                token = create_session(row[0])
                return self.send_json({"ok": True, "user": {"id": row[0], "username": row[1], "role": row[3]}}, cookie=f"well_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}")
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/logout":
            from http.cookies import SimpleCookie
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie.get("well_session")
            if token:
                with sqlite3.connect(ACCOUNT_DB) as conn:
                    conn.execute("DELETE FROM sessions WHERE token = ?", (token.value,))
            self.send_response(200)
            self.send_header("Set-Cookie", "well_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/change-password":
            user = require_user(self)
            if not user:
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                with sqlite3.connect(ACCOUNT_DB) as conn:
                    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
                    if not row or not verify_password(payload.get("old_password", ""), row[0]):
                        return self.send_json({"ok": False, "error": "原密码错误"}, 400)
                    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(payload.get("new_password", "")), user["id"]))
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
                return self.send_json({"ok": True})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        user = require_user(self)
        if not user:
            return
        if path == "/api/users":
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅管理员可创建登录用户"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                username = (payload.get("username") or "").strip()
                with sqlite3.connect(ACCOUNT_DB) as conn:
                    conn.execute("INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'user', ?)", (username, hash_password(payload.get("password", "")), datetime.now().isoformat(timespec="seconds")))
                return self.send_json({"ok": True, "username": username})
            except sqlite3.IntegrityError:
                return self.send_json({"ok": False, "error": "用户名已存在"}, 409)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/targets":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                account = account_by_id(payload.get("account_id"))
                metric_id = payload.get("metric_id")
                month = payload.get("month")
                if not account or metric_id not in TARGET_DEFINITIONS or not isinstance(month, str) or len(month) != 7:
                    return self.send_json({"ok": False, "error": "账号、月份或指标无效"}, 400)
                raw = payload.get("custom_value")
                value = None if raw is None or raw == "" else float(raw)
                with sqlite3.connect(ACCOUNT_DB) as conn:
                    conn.execute("""INSERT INTO metric_targets(account_id, store_name, month, metric_id, custom_value, updated_by, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(account_id, month, metric_id) DO UPDATE SET custom_value=excluded.custom_value, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                        (account["id"], account["store_name"], month, metric_id, value, user["id"], datetime.now().isoformat(timespec="seconds")))
                return self.send_json({"ok": True, "target": next(x for x in target_rows(account["id"], month) if x["metric_id"] == metric_id)})
            except (TypeError, ValueError):
                return self.send_json({"ok": False, "error": "目标值必须是数字或留空"}, 400)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/imports/delete":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                account = account_by_id(payload.get("account_id"))
                source_type = payload.get("source_type")
                file_sha256 = (payload.get("file_sha256") or "").strip()
                if not account or source_type not in DB_PATHS or len(file_sha256) != 64:
                    return self.send_json({"ok": False, "error": "账号、来源或文件指纹无效"}, 400)
                deleted = delete_import(source_type, account["id"], file_sha256)
                if not deleted:
                    return self.send_json({"ok": False, "error": "记录不存在或已删除"}, 404)
                return self.send_json({"ok": True, "account_id": account["id"], "source_type": source_type, "file_sha256": file_sha256})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path.startswith("/api/users/") and path.endswith("/status"):
            if user["role"] != "admin":
                return self.send_json({"ok": False, "error": "仅管理员可停用登录用户"}, 403)
            try:
                target_id = int(path.split("/")[3])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                active = 1 if payload.get("active") else 0
                if target_id == user["id"] and not active:
                    return self.send_json({"ok": False, "error": "不能停用当前管理员账号"}, 400)
                with sqlite3.connect(ACCOUNT_DB) as conn:
                    cur = conn.execute("UPDATE users SET active = ? WHERE id = ?", (active, target_id))
                    if cur.rowcount == 0:
                        return self.send_json({"ok": False, "error": "用户不存在"}, 404)
                    if not active:
                        conn.execute("DELETE FROM sessions WHERE user_id = ?", (target_id,))
                return self.send_json({"ok": True, "active": bool(active)})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/accounts":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                account = create_account(payload.get("account_name"), payload.get("store_name"))
                return self.send_json({"ok": True, "account": account})
            except sqlite3.IntegrityError:
                return self.send_json({"ok": False, "error": "账号名称已存在"}, 409)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if urlparse(self.path).path != "/api/import":
            self.send_error(404)
            return
        try:
            import cgi
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
            )
            source_type = form.getfirst("source_type") or ""
            account_id = int(form.getfirst("account_id") or 0)
            account = account_by_id(account_id)
            if not account:
                return self.send_json({"ok": False, "error": "请选择有效账号"}, 400)
            store_name = account["store_name"]
            if source_type not in DB_PATHS:
                return self.send_json({"ok": False, "error": "请选择报表来源"}, 400)
            files = form["files"] if "files" in form else []
            if not isinstance(files, list):
                files = [files]
            results = []
            batch_id = datetime.now().strftime("local-%Y%m%d-%H%M%S-") + secrets.token_hex(6)
            with tempfile.TemporaryDirectory(prefix="data-workbench-") as temp:
                temp_dir = Path(temp)
                for item in files:
                    if not getattr(item, "filename", None):
                        continue
                    content = item.file.read()
                    upload = temp_dir / Path(item.filename).name
                    if upload.suffix.lower() != ('.xls' if source_type == 'shengyicanmou' else '.csv'):
                        raise ValueError('生意参谋只接受 .xls，无界商品报表只接受 .csv')
                    upload.write_bytes(content)
                    if upload.suffix.lower() == ".xls":
                        rows = convert_xls(upload, temp_dir)
                        header_index, header = find_header(
                            rows,
                            ["支付金额", "成功退款金额", "支付件数", "商品访客数"],
                            ["成交买家数量", "成交买家数", "支付买家数"],
                        )
                        data = [header] + rows[header_index + 1 :]
                        encoding = "libreoffice-csv"
                    else:
                        rows, encoding = read_csv_file_with_encoding(upload)
                        header_index, header = find_header(rows, ["花费", "投入产出比", "总成交金额", "计划ID"])
                        data = [header] + rows[header_index + 1 :]
                    result = analyse_file(upload.name, data)
                    result['row_numbers'] = [header_index + index + 1 for index, row in enumerate(rows[header_index + 1:], 1) if any(cell.strip() for cell in row)]
                    result['attribution_window'] = (form.getfirst('attribution_window') or 'unknown').strip() if source_type == 'wujie' else 'not_applicable'
                    expected_sources = {"生意参谋商品日报"} if source_type == "shengyicanmou" else {"无界商品报表", "无界计划报表"}
                    if result["source"] not in expected_sources:
                        raise ValueError(f"{upload.name} 与选择的报表来源不匹配")
                    saved = save_import(source_type, account_id, store_name, batch_id, upload.name, content, result, encoding)
                    public_result = {key: value for key, value in result.items() if key not in ('records', 'row_numbers')}
                    public_result["stored"] = not saved["duplicate"]
                    public_result["duplicate"] = saved["duplicate"]
                    public_result["sha256"] = saved["sha256"]
                    public_result["unified_status"] = saved.get("unified_status")
                    results.append(public_result)
            self.send_json({"ok": True, "batch_id": batch_id, "account_id": account_id, "account_name": account["account_name"], "store": store_name, "source_type": source_type, "files": results})
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"电商数据工作台已启动：http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
