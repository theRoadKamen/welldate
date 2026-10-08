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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA_ROOT = ROOT / "data"
RAW_ROOT = DATA_ROOT / "raw"
DB_PATHS = {
    "shengyicanmou": DATA_ROOT / "shengyicanmou.sqlite3",
    "wujie": DATA_ROOT / "wujie.sqlite3",
}
ACCOUNT_DB = DATA_ROOT / "accounts.sqlite3"
SESSION_DAYS = 7
SOFFICE = os.environ.get(
    "SOFFICE",
    "/Users/well/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice",
)

# Target metadata is centralized here. Numeric defaults remain unset until the
# business confirms them; the comparison direction is explicit and never inferred.
TARGET_DEFINITIONS = {
    "gmv": {"label": "GMV", "unit": "amount", "compare_type": "higher", "default_value": None, "mtd_kind": "cumulative"},
    "gsv": {"label": "GSV", "unit": "amount", "compare_type": "higher", "default_value": None, "mtd_kind": "cumulative"},
    "refund_rate": {"label": "退款率", "unit": "percent", "compare_type": "lower", "default_value": None, "mtd_kind": "ratio"},
    "conversion_rate": {"label": "转化率", "unit": "percent", "compare_type": "higher", "default_value": None, "mtd_kind": "ratio"},
    "average_order_value": {"label": "客单价", "unit": "amount", "compare_type": "higher", "default_value": None, "mtd_kind": "ratio"},
    "paid_units": {"label": "支付件数", "unit": "number", "compare_type": "higher", "default_value": None, "mtd_kind": "cumulative"},
    "paid_buyers": {"label": "支付买家数", "unit": "number", "compare_type": "higher", "default_value": None, "mtd_kind": "dedupe"},
    "spend": {"label": "推广花费", "unit": "amount", "compare_type": "budget", "default_value": None, "mtd_kind": "cumulative"},
    "clicks": {"label": "推广点击量", "unit": "number", "compare_type": "higher", "default_value": None, "mtd_kind": "cumulative"},
    "ppc": {"label": "推广点击单价", "unit": "amount", "compare_type": "lower", "default_value": None, "mtd_kind": "ratio"},
    "roi": {"label": "投入产出比", "unit": "ratio", "compare_type": "higher", "default_value": None, "mtd_kind": "ratio"},
    "fee_ratio": {"label": "推广费比", "unit": "percent", "compare_type": "lower", "default_value": None, "mtd_kind": "ratio"},
}


def source_label(source_type):
    return "生意参谋商品日报" if source_type == "shengyicanmou" else "无界计划报表"


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
    units = total(shop, "paid_units")
    spend = total(ads, "spend")
    deals = total(ads, "total_deal_amount")
    clicks = total(ads, "clicks")
    return {
        "gmv": round(gmv, 2), "gsv": round(gmv - refunds, 2), "successful_refund_amount": round(refunds, 2),
        "paid_units": int(units) if units.is_integer() else units, "spend": round(spend, 2),
        "total_deal_amount": round(deals, 2), "clicks": int(clicks) if clicks.is_integer() else clicks,
        "paid_buyers": None, "visitors": None,
        "refund_rate": round(refunds / gmv, 8) if gmv else None,
        "conversion_rate": None, "average_order_value": round(gmv / units, 2) if units else None,
        "ppc": round(spend / clicks, 2) if clicks else None, "roi": round(deals / spend, 2) if spend else None,
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


def save_import(source_type, account_id, store_name, batch_id, filename, content, result):
    digest = hashlib.sha256(content).hexdigest()
    date = result.get("date") or "unknown"
    raw_dir = RAW_ROOT / source_type / date
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{digest[:16]}-{Path(filename).name}"
    raw_path.write_bytes(content)
    db_path = DB_PATHS[source_type]
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO imports(batch_id, account_id, store_name, source_type, business_date, original_filename, file_path, file_sha256, row_count, result_json, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, account_id, store_name, source_type, date, Path(filename).name, str(raw_path), digest, result.get("row_count", 0), json.dumps(result, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")),
            )
    except sqlite3.IntegrityError:
        return {"duplicate": True, "sha256": digest, "result": result}
    return {"duplicate": False, "sha256": digest, "result": result}


def stored_results(source_type, account_id, date):
    db_path = DB_PATHS[source_type]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT result_json FROM imports WHERE account_id = ? AND business_date = ? ORDER BY imported_at DESC",
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
                AND i.business_date >= ?
                AND i.business_date <= ?
                AND NOT EXISTS (
                    SELECT 1 FROM imports newer
                     WHERE newer.account_id = i.account_id
                       AND newer.business_date = i.business_date
                       AND newer.imported_at > i.imported_at
                )
              ORDER BY i.business_date ASC""",
            (account_id, start_date, end_date),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def upload_records(source_type, account_id):
    db_path = DB_PATHS[source_type]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT batch_id, business_date, original_filename, row_count, file_sha256, imported_at FROM imports WHERE account_id = ? ORDER BY imported_at DESC",
            (account_id,),
        ).fetchall()
    return [
        {"batch_id": r[0], "date": r[1], "filename": r[2], "row_count": r[3], "sha256": r[4], "imported_at": r[5]}
        for r in rows
    ]


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


def read_csv_file(path):
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = raw.decode(encoding)
            rows = list(csv.reader(io.StringIO(text)))
            if rows and any(any(cell.strip() for cell in row) for row in rows[:20]):
                return rows
        except (UnicodeDecodeError, csv.Error):
            continue
    raise ValueError("无法识别 CSV 编码")


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


def find_header(rows, required):
    for index, row in enumerate(rows[:12]):
        normalized = [cell.strip() for cell in row]
        if all(name in normalized for name in required):
            return index, normalized
    raise ValueError("未找到可识别的报表表头")


def analyse_file(filename, data):
    if {"支付金额", "成功退款金额", "支付件数", "支付买家数", "商品访客数"}.issubset(set(data[0])):
        header = data[0]
        rows = [dict(zip(header, row)) for row in data[1:] if any(cell.strip() for cell in row)]
        gmv = sum(decimal_number(row.get("支付金额")) for row in rows)
        refunds = sum(decimal_number(row.get("成功退款金额")) for row in rows)
        units = sum(decimal_number(row.get("支付件数")) for row in rows)
        buyers = sum(decimal_number(row.get("支付买家数")) for row in rows)
        visitors = sum(decimal_number(row.get("商品访客数")) for row in rows)
        return {
            "source": "生意参谋商品日报",
            "date": rows[0].get("统计日期", "") if rows else "",
            "row_count": len(rows),
            "headers": header,
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
        rows = [dict(zip(header, row)) for row in data[1:] if any(cell.strip() for cell in row)]
        spend = sum(decimal_number(row.get("花费")) for row in rows)
        deals = sum(decimal_number(row.get("总成交金额")) for row in rows)
        direct = sum(decimal_number(row.get("直接成交金额")) for row in rows)
        indirect = sum(decimal_number(row.get("间接成交金额")) for row in rows)
        clicks = sum(decimal_number(row.get("点击量")) for row in rows)
        return {
            "source": "无界计划报表",
            "date": rows[0].get("日期", "") if rows else "",
            "row_count": len(rows),
            "headers": header,
            "metrics": {
                "spend": round(spend, 2),
                "total_deal_amount": round(deals, 2),
                "direct_deal_amount": round(direct, 2),
                "indirect_deal_amount": round(indirect, 2),
                "clicks": int(clicks) if clicks.is_integer() else clicks,
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
            batch_id = datetime.now().strftime("local-%Y%m%d-%H%M%S")
            with tempfile.TemporaryDirectory(prefix="data-workbench-") as temp:
                temp_dir = Path(temp)
                for item in files:
                    if not getattr(item, "filename", None):
                        continue
                    content = item.file.read()
                    upload = temp_dir / Path(item.filename).name
                    upload.write_bytes(content)
                    if upload.suffix.lower() == ".xls":
                        rows = convert_xls(upload, temp_dir)
                        header_index, header = find_header(rows, ["支付金额", "成功退款金额", "支付件数", "支付买家数", "商品访客数"])
                        data = [header] + rows[header_index + 1 :]
                    else:
                        rows = read_csv_file(upload)
                        header_index, header = find_header(rows, ["花费", "投入产出比", "总成交金额", "计划ID"])
                        data = [header] + rows[header_index + 1 :]
                    result = analyse_file(upload.name, data)
                    expected_source = "生意参谋商品日报" if source_type == "shengyicanmou" else "无界计划报表"
                    if result["source"] != expected_source:
                        raise ValueError(f"{upload.name} 与选择的报表来源不匹配")
                    saved = save_import(source_type, account_id, store_name, batch_id, upload.name, content, result)
                    result["stored"] = not saved["duplicate"]
                    result["duplicate"] = saved["duplicate"]
                    results.append(result)
            self.send_json({"ok": True, "batch_id": batch_id, "account_id": account_id, "account_name": account["account_name"], "store": store_name, "source_type": source_type, "files": results})
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"电商数据工作台已启动：http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
