import csv
import io
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MYSQL_EXE = os.environ.get(
    "MYSQL_EXE",
    "mysql",
)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def write_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def connection_config(payload):
    required = ["host", "port", "user", "database"]
    missing = [key for key in required if not str(payload.get(key, "")).strip()]
    if missing:
        raise ApiError(f"缺少连接字段: {', '.join(missing)}")

    return {
        "host": str(payload["host"]).strip(),
        "port": str(payload["port"]).strip(),
        "user": str(payload["user"]).strip(),
        "password": str(payload.get("password", "")),
        "database": str(payload["database"]).strip(),
    }


def mysql_query(config, sql, timeout=15, with_column_names=False):
    if not Path(MYSQL_EXE).exists():
        raise ApiError(f"找不到 MySQL 客户端: {MYSQL_EXE}", 500)

    defaults = (
        "[client]\n"
        f"host={config['host']}\n"
        f"port={config['port']}\n"
        f"user={config['user']}\n"
        f"password={config['password']}\n"
        "default-character-set=utf8mb4\n"
    )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".cnf") as tmp:
            tmp.write(defaults)
            tmp_path = tmp.name

        command = [
            MYSQL_EXE,
            f"--defaults-extra-file={tmp_path}",
            "--batch",
            "--raw",
        ]
        if not with_column_names:
            command.append("--skip-column-names")
        command.extend([config["database"], "-e", sql])

        result = subprocess.run(
            command,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "MySQL 执行失败"
            raise ApiError(message, 400)
        return result.stdout.strip()
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def mysql_import_file(config, sql_file, timeout=180):
    if not Path(MYSQL_EXE).exists():
        raise ApiError(f"找不到 MySQL 客户端: {MYSQL_EXE}", 500)
    if not Path(sql_file).exists():
        raise ApiError(f"找不到 SQL 文件: {sql_file}", 404)

    defaults = (
        "[client]\n"
        f"host={config['host']}\n"
        f"port={config['port']}\n"
        f"user={config['user']}\n"
        f"password={config['password']}\n"
        "default-character-set=utf8mb4\n"
    )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".cnf") as tmp:
            tmp.write(defaults)
            tmp_path = tmp.name

        command = (
            f"\"{MYSQL_EXE}\" --defaults-extra-file=\"{tmp_path}\" "
            f"--default-character-set=utf8mb4 < \"{sql_file}\""
        )
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "SQL 文件导入失败"
            raise ApiError(message, 400)
        return {"file": sql_file, "message": "导入成功"}
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def parse_rows(output):
    if not output:
        return []
    return [line.split("\t") for line in output.splitlines()]


def format_sql(sql):
    compact = " ".join(str(sql or "").strip().split())
    if not compact:
        return ""

    compact = re.sub(r"\bselect\b", "SELECT", compact, flags=re.I)
    compact = re.sub(r"\bfrom\b", "\nFROM", compact, flags=re.I)
    compact = re.sub(r"\binner\s+join\b", "\nINNER JOIN", compact, flags=re.I)
    compact = re.sub(r"\bleft\s+join\b", "\nLEFT JOIN", compact, flags=re.I)
    compact = re.sub(r"\bright\s+join\b", "\nRIGHT JOIN", compact, flags=re.I)
    compact = re.sub(r"(?<!inner )(?<!left )(?<!right )\bjoin\b", "\nJOIN", compact, flags=re.I)
    compact = re.sub(r"\bwhere\b", "\nWHERE", compact, flags=re.I)
    compact = re.sub(r"\bgroup\s+by\b", "\nGROUP BY", compact, flags=re.I)
    compact = re.sub(r"\bhaving\b", "\nHAVING", compact, flags=re.I)
    compact = re.sub(r"\border\s+by\b", "\nORDER BY", compact, flags=re.I)
    compact = re.sub(r"\blimit\b", "\nLIMIT", compact, flags=re.I)
    compact = compact.replace("SELECT ", "SELECT\n  ", 1)
    compact = re.sub(r",\s*", ",\n  ", compact)
    compact = re.sub(r"\nFROM\s+", "\nFROM ", compact)
    compact = re.sub(r"\nWHERE\s+", "\nWHERE ", compact)
    compact = re.sub(r"\nGROUP BY\s+", "\nGROUP BY ", compact)
    compact = re.sub(r"\nHAVING\s+", "\nHAVING ", compact)
    compact = re.sub(r"\nORDER BY\s+", "\nORDER BY ", compact)
    compact = re.sub(r"\nLIMIT\s+", "\nLIMIT ", compact)
    return compact.strip()


def is_select_sql(sql):
    normalized = f" {str(sql or '').strip().lower()} "
    forbidden = ["insert", "update", "delete", "drop", "alter", "truncate", "create", "replace"]
    return normalized.strip().startswith("select") and not any(f" {word} " in normalized for word in forbidden)


def handle_test_connection(payload):
    config = connection_config(payload)
    output = mysql_query(config, "SELECT DATABASE(), VERSION();")
    rows = parse_rows(output)
    database, version = rows[0] if rows else [config["database"], "unknown"]
    return {
        "ok": True,
        "database": database,
        "version": version,
        "message": "MySQL 连接成功",
    }


def handle_schema(payload):
    config = connection_config(payload)
    database_name = config["database"].replace("'", "''")
    table_output = mysql_query(
        config,
        "SELECT table_name, table_rows "
        "FROM information_schema.tables "
        f"WHERE table_schema = '{database_name}' "
        "ORDER BY table_name;",
    )
    tables = []
    for table_name, table_rows in parse_rows(table_output):
        safe_table_name = table_name.replace("'", "''")
        count_output = mysql_query(
            config,
            f"SELECT COUNT(*) FROM {quote_identifier(table_name)};",
            timeout=15,
        )
        count_rows = parse_rows(count_output)
        exact_row_count = (
            int(count_rows[0][0])
            if count_rows and count_rows[0] and count_rows[0][0].isdigit()
            else None
        )
        column_output = mysql_query(
            config,
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{database_name}' "
            f"AND table_name = '{safe_table_name}' "
            "ORDER BY ordinal_position;",
        )
        columns = [
            {"name": column[0], "type": column[1]}
            for column in parse_rows(column_output)
            if len(column) >= 2
        ]
        tables.append(
            {
                "name": table_name,
                "row_count": exact_row_count,
                "column_count": len(columns),
                "columns": columns,
            }
        )

    return {
        "ok": True,
        "database": config["database"],
        "tables": tables,
    }


def handle_import_sql(payload):
    config = connection_config(payload)
    confirmed = bool(payload.get("confirmed"))
    if not confirmed:
        raise ApiError("请先确认导入会修改数据库")

    files = [path for path in payload.get("files", []) if str(path).strip()]
    if not files:
        raise ApiError("请至少填写一个 SQL 文件路径")

    imported = [mysql_import_file(config, file_path) for file_path in files]
    schema = handle_schema(payload)
    return {
        "ok": True,
        "mode": "sql_files",
        "imported": imported,
        "schema": schema,
    }


def handle_import_uploaded_sql(payload):
    config = connection_config(payload)
    files = payload.get("files", [])
    confirmed = bool(payload.get("confirmed"))
    if not confirmed:
        raise ApiError("请先确认导入会修改数据库")
    if not files:
        raise ApiError("请至少选择一个 SQL 文件")

    imported = []
    temp_paths = []
    try:
        for item in files:
            name = str(item.get("name", "upload.sql"))
            content = str(item.get("content", ""))
            if not name.lower().endswith(".sql"):
                raise ApiError(f"只支持 .sql 文件: {name}")
            if not content.strip():
                raise ApiError(f"文件内容为空: {name}")
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".sql") as tmp:
                tmp.write(content)
                temp_paths.append(tmp.name)
            imported.append(mysql_import_file(config, temp_paths[-1]))
            imported[-1]["name"] = name
        schema = handle_schema(payload)
        return {
            "ok": True,
            "mode": "uploaded_sql",
            "imported": imported,
            "schema": schema,
        }
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass


def sql_literal(value):
    if value is None or str(value) == "":
        return "NULL"
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def table_column_names(config, table_name):
    database_name = config["database"].replace("'", "''")
    safe_table_name = str(table_name).replace("'", "''")
    output = mysql_query(
        config,
        "SELECT column_name "
        "FROM information_schema.columns "
        f"WHERE table_schema = '{database_name}' "
        f"AND table_name = '{safe_table_name}' "
        "ORDER BY ordinal_position;",
    )
    return [row[0] for row in parse_rows(output) if row]


def handle_import_csv(payload):
    config = connection_config(payload)
    table_name = str(payload.get("table", "")).strip()
    file_name = str(payload.get("fileName", "data.csv")).strip()
    content = str(payload.get("content", ""))
    confirmed = bool(payload.get("confirmed"))
    if not confirmed:
        raise ApiError("请先确认导入会向当前 MySQL 数据库写入数据")
    if not table_name:
        raise ApiError("请选择当前数据库中的目标表")
    if not file_name.lower().endswith(".csv"):
        raise ApiError("当前导入模式只支持 .csv 文件")
    if not content.strip():
        raise ApiError("CSV 文件内容不能为空")

    existing_columns = table_column_names(config, table_name)
    if not existing_columns:
        raise ApiError(f"没有找到目标表: {table_name}", 404)

    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    headers = [header.strip() for header in (reader.fieldnames or []) if header and header.strip()]
    if not headers:
        raise ApiError("CSV 第一行必须是字段名")

    insert_columns = [column for column in headers if column in existing_columns]
    ignored_columns = [column for column in headers if column not in existing_columns]
    if not insert_columns:
        raise ApiError("CSV 字段与目标表字段没有匹配项，请检查表头")

    rows = list(reader)
    if not rows:
        raise ApiError("CSV 没有可导入的数据行")

    quoted_table = quote_identifier(table_name)
    quoted_columns = ", ".join(quote_identifier(column) for column in insert_columns)
    inserted = 0
    batch_size = 300
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        values = []
        for row in batch:
            values.append("(" + ", ".join(sql_literal(row.get(column, "")) for column in insert_columns) + ")")
        mysql_query(
            config,
            f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES {', '.join(values)};",
            timeout=60,
        )
        inserted += len(batch)

    schema = handle_schema(payload)
    return {
        "ok": True,
        "mode": "csv_table_import",
        "table": table_name,
        "file": file_name,
        "inserted_rows": inserted,
        "matched_columns": insert_columns,
        "ignored_columns": ignored_columns,
        "schema": schema,
    }


def handle_execute_sql(payload):
    config = connection_config(payload)
    sql = str(payload.get("sql", "")).strip()
    if not sql:
        raise ApiError("SQL 不能为空")
    if not is_select_sql(sql):
        raise ApiError("出于安全原因，当前只允许执行 SELECT 查询")

    executable_sql = re.sub(r";+\s*$", "", sql.strip())
    if not re.search(r"\blimit\b", executable_sql, flags=re.I):
        executable_sql += " LIMIT 100"
    executable_sql += ";"

    output = mysql_query(config, executable_sql, timeout=30, with_column_names=True)
    parsed = parse_rows(output)
    if not parsed:
        return {
            "ok": True,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "executed_sql": format_sql(executable_sql),
        }

    columns = parsed[0]
    rows = [dict(zip(columns, row)) for row in parsed[1:]]
    return {
        "ok": True,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "executed_sql": format_sql(executable_sql),
    }


def quote_identifier(name):
    return "`" + str(name).replace("`", "``") + "`"


def handle_table_detail(payload):
    config = connection_config(payload)
    table_name = str(payload.get("table", "")).strip()
    if not table_name:
        raise ApiError("表名不能为空")

    database_name = config["database"].replace("'", "''")
    safe_table_name = table_name.replace("'", "''")
    column_output = mysql_query(
        config,
        "SELECT column_name, data_type, is_nullable, column_key, column_default, column_comment "
        "FROM information_schema.columns "
        f"WHERE table_schema = '{database_name}' "
        f"AND table_name = '{safe_table_name}' "
        "ORDER BY ordinal_position;",
    )
    columns = [
        {
            "name": row[0],
            "type": row[1],
            "nullable": row[2],
            "key": row[3],
            "default": row[4],
            "comment": row[5] if len(row) > 5 else "",
        }
        for row in parse_rows(column_output)
        if len(row) >= 5
    ]
    if not columns:
        raise ApiError(f"没有找到表: {table_name}", 404)

    count_sql = f"SELECT COUNT(*) AS row_count FROM {quote_identifier(table_name)};"
    count_output = mysql_query(config, count_sql, timeout=15)
    count_rows = parse_rows(count_output)
    row_count = int(count_rows[0][0]) if count_rows and count_rows[0] and count_rows[0][0].isdigit() else None

    sample_sql = f"SELECT * FROM {quote_identifier(table_name)} LIMIT 5;"
    sample_output = mysql_query(config, sample_sql, timeout=15, with_column_names=True)
    parsed = parse_rows(sample_output)
    sample_columns = parsed[0] if parsed else []
    sample_rows = [dict(zip(sample_columns, row)) for row in parsed[1:]]
    return {
        "ok": True,
        "table": table_name,
        "row_count": row_count,
        "columns": columns,
        "sample_columns": sample_columns,
        "sample_rows": sample_rows,
        "sample_sql": format_sql(sample_sql),
    }


def compact_schema(schema):
    tables = schema.get("tables", []) if isinstance(schema, dict) else []
    compact = []
    for table in tables[:20]:
        compact.append(
            {
                "table": table.get("name"),
                "columns": [
                    {"name": column.get("name"), "type": column.get("type")}
                    for column in table.get("columns", [])[:40]
                ],
            }
        )
    return compact


def build_analysis_prompt(payload):
    question = str(payload.get("question", "")).strip()
    if not question:
        raise ApiError("业务问题不能为空")

    schema = compact_schema(payload.get("schema", {}))
    return [
        {
            "role": "system",
            "content": (
                "你是一个 MySQL 数据分析助手。你必须只返回 JSON，不要返回 Markdown。"
                "请根据用户问题和数据库 schema 完成只读数据分析分类、生成安全 MySQL SQL。"
                "生成的 SQL 必须格式清晰，SELECT、FROM、JOIN、WHERE、GROUP BY、ORDER BY 等子句分行；LIMIT 10 保持一行，不要拆成两行。"
                "对于消费总金额、销售总额、订单总数这类问题，优先返回可分析的明细粒度，例如客户、地区、品类、日期等维度及其指标，"
                "不要只返回一个聚合数字；最终总数由系统根据结果表再汇总。"
                "如果用户说最近 N 天、近 N 天、最近一个月等相对时间，优先基于相关日期字段的 MAX(date) 往前推 N 天，"
                "不要直接使用 NOW() 或 CURRENT_DATE，因为示例/历史数据可能不是当前年份。"
                "是否需要汇总卡由 requires_total 决定：趋势分析通常 false；消费总额、销售额、订单数等汇总问题通常 true。"
                "如果信息不足，不要编造字段，返回 requires_clarification=true。"
                "本应用只服务数据分析，不执行增删改；遇到 INSERT、UPDATE、DELETE、DROP、ALTER、TRUNCATE 等意图时，"
                "sql 必须为空字符串，summary 用中文说明该请求超出只读分析范围。"
                "分类字段可以用英文枚举，但 summary、sql_explanation、understood_task 必须用中文。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_question": question,
                    "database_schema": schema,
                    "output_json_shape": {
                        "analysis_type": "metric_query | trend_analysis | comparison_analysis | ranking_analysis | proportion_analysis | attribution_analysis | anomaly_detection | forecasting | segmentation | detail_query | data_operation",
                        "result_type": "table | chart | table_and_chart | explanation",
                        "requires_sql": True,
                        "requires_python_cleaning": True,
                        "requires_visualization": True,
                        "requires_total": False,
                        "suggested_chart": "line_chart | bar_chart | pie_chart | table | none",
                        "requires_user_confirmation": False,
                        "requires_clarification": False,
                        "understood_task": "中文说明",
                        "sql": "安全 MySQL SQL，只读场景必须是 SELECT",
                        "sql_explanation": "中文解释",
                        "python_steps": ["中文步骤"],
                        "summary": ["中文摘要"],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def call_deepseek(payload):
    api_key = str(payload.get("apiKey", "")).strip()
    if not api_key:
        raise ApiError("DeepSeek API Key 未配置")

    base_url = str(payload.get("baseUrl", "https://api.deepseek.com")).strip().rstrip("/")
    if not base_url:
        base_url = "https://api.deepseek.com"
    if not re.match(r"^https?://", base_url, flags=re.I):
        base_url = "https://" + base_url
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")].rstrip("/")
    parsed_url = urlparse(base_url)
    if not parsed_url.scheme or not parsed_url.netloc:
        raise ApiError(f"DeepSeek Base URL 无效: {base_url}")

    model = str(payload.get("model", "deepseek-chat")).strip() or "deepseek-chat"
    request_body = {
        "model": model,
        "messages": build_analysis_prompt(payload),
        "response_format": {"type": "json_object"},
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 1800,
    }

    def request_deepseek(body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "AI-Data-Analyst/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")

    try:
        raw = request_deepseek(request_body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 400 and "response_format" in detail.lower():
            retry_body = dict(request_body)
            retry_body.pop("response_format", None)
            try:
                raw = request_deepseek(retry_body)
            except urllib.error.HTTPError as retry_exc:
                retry_detail = retry_exc.read().decode("utf-8", errors="replace")
                raise ApiError(f"DeepSeek 调用失败: HTTP {retry_exc.code} {retry_detail}", retry_exc.code)
        else:
            raise ApiError(f"DeepSeek 调用失败: HTTP {exc.code} {detail}", exc.code)
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "10061" in reason:
            reason += "。当前机器拒绝连接该地址，请检查 Base URL 是否为 https://api.deepseek.com，或检查代理/VPN/防火墙设置。"
        raise ApiError(f"DeepSeek 网络请求失败: {reason}", 502)

    result = json.loads(raw)
    content = result["choices"][0]["message"]["content"]
    try:
        analysis = json.loads(content)
    except json.JSONDecodeError:
        raise ApiError(f"DeepSeek 返回了非 JSON 内容: {content}", 502)

    if isinstance(analysis, dict) and analysis.get("sql"):
        analysis["sql"] = format_sql(analysis["sql"])

    return {
        "ok": True,
        "mode": "deepseek",
        "requested_model": model,
        "actual_model": result.get("model", model),
        "analysis": analysis,
        "usage": result.get("usage", {}),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_POST(self):
        try:
            payload = read_json(self)
            if self.path == "/api/mysql/test-connection":
                write_json(self, 200, handle_test_connection(payload))
                return
            if self.path == "/api/mysql/schema":
                write_json(self, 200, handle_schema(payload))
                return
            if self.path == "/api/mysql/import-sql":
                write_json(self, 200, handle_import_sql(payload))
                return
            if self.path == "/api/mysql/import-uploaded-sql":
                write_json(self, 200, handle_import_uploaded_sql(payload))
                return
            if self.path == "/api/mysql/import-csv":
                write_json(self, 200, handle_import_csv(payload))
                return
            if self.path == "/api/sql/execute":
                write_json(self, 200, handle_execute_sql(payload))
                return
            if self.path == "/api/mysql/table-detail":
                write_json(self, 200, handle_table_detail(payload))
                return
            if self.path == "/api/deepseek/analyze":
                write_json(self, 200, call_deepseek(payload))
                return
            raise ApiError("未知接口", 404)
        except ApiError as exc:
            write_json(self, exc.status, {"ok": False, "message": str(exc)})
        except Exception as exc:
            write_json(self, 500, {"ok": False, "message": f"服务器错误: {exc}"})


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"AI Data Analyst server running at http://127.0.0.1:{port}")
    print(f"MySQL client: {MYSQL_EXE}")
    server.serve_forever()


if __name__ == "__main__":
    main()
