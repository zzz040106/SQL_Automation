# AI Data Analyst 工作台原型

这是一个本地网页数据分析工作台，前端在 `index.html`，本地 Python 后端在 `server.py`。

## 运行

```powershell
cd your-project-folder
python server.py
```

访问：

```text
http://127.0.0.1:8000
```

## MySQL 连接

左侧填写本地或远程 MySQL 信息：

```text
Host: 数据库地址，例如 127.0.0.1
Port: MySQL 端口，默认通常是 3306
Database: 目标数据库名
User: MySQL 用户名
Password: MySQL 密码
```

`Database` 是目标数据库名称。点击“读取 Schema”后，后端会连接这个数据库，读取表结构、字段和真实行数。

## 导入数据

当前导入模式支持上传 `.sql` 文件，把表结构和数据导入到 MySQL。

步骤：

1. 先在左侧填写 MySQL 连接信息。
2. 点击“导入数据”。
3. 选择单个 SQL 文件，或一次选择多个 SQL 文件。
4. 点击“开始导入”。
5. 导入完成后点击“读取 Schema”查看表结构。

如果 SQL 文件里没有 `CREATE DATABASE` / `USE 数据库名`，请先在左侧填写目标 `Database`。

## DeepSeek

右上角设置中可以填写：

```text
API Key
Base URL: https://api.deepseek.com
Model: deepseek-chat 或你要使用的模型名
```

没有 API Key 时，页面只使用本地规则预览，不会调用 DeepSeek。

填写 API Key 后，点击“分析”会通过本地 Python 后端调用 DeepSeek，根据业务问题和当前 MySQL Schema 生成分类结果与只读 SQL。

## SQL 执行

点击“执行查询”后，前端请求：

```text
POST /api/sql/execute
```

后端只允许执行 `SELECT`，会阻断 `INSERT / UPDATE / DELETE / DROP / ALTER / TRUNCATE / CREATE / REPLACE` 等语句。没有 `LIMIT` 的查询会自动追加 `LIMIT 100`。

## 导出

支持导出：

- CSV：查询结果表格
- Excel：查询结果表格
- HTML：业务问题、执行 SQL、全部查询结果、数据质量、分析摘要与业务建议
