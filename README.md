# 分布式图书馆数据库

Python 3、FastAPI、SQLite、sqlglot 和 httpx 实现的分布式数据库课程设计。主站提供分片目录、跨站图书检索和 SQL 查询计划。三个独立分站各自保存本分馆的数据，并提供本地表浏览和请求事件。

## 架构

- 主站点口 `8000`：解析 SQL、检查只读语法白名单、根据 branch_id 分片目录裁剪目标站点、并发调用分站并展示查询计划。主站不创建业务 SQLite 文件；跨站查询使用一次性内存 SQLite 合并数据。
- 分站 A / B / C：每站独立 FastAPI 进程和 SQLite 文件，只保存 branch_id 分别为 1 / 2 / 3 的业务记录。`/internal/query` 仅接受受控 SELECT；`/events` 通过 SSE 推送请求事件，事件仅保存在有限的进程内缓冲区，不写日志文件。
- `config/sites.json` 集中配置主站和分站监听地址、主站访问 URL、站点 URL、数据库相对路径、超时与最大返回行数。各服务经 HTTP 通信，分站 URL 可改为其他机器地址；跨电脑部署时也应填写浏览器可访问的主站 URL。

## 逻辑数据

```text
books(book_id, branch_id, title, author, category, status)
readers(reader_id, branch_id, name)
loans(loan_id, branch_id, book_id, reader_id, loan_date, return_date)
```

三张表均按 `branch_id` 水平划分到 A / B / C，ID 在演示数据中全局唯一。分站数据库用 branch_id CHECK 约束限制归属；loans 用复合外键约束确保 book、reader 与借阅记录属于同一分馆。

## SQL 支持范围

支持单条只读 SELECT，预定义表和列，列选择、别名、`*`、`DISTINCT`、`WHERE`、`AND` / `OR` / `NOT`、比较运算、`IS NULL`、`LIKE`、字面量 `IN`、带 `ON` 的 INNER JOIN、`ORDER BY`、非负整数 `LIMIT`、`COUNT` / `SUM` / `AVG` / `MIN` / `MAX`、`GROUP BY` 和 `HAVING`。支持数值四则运算和括号表达式。多表 `SELECT *` 按 SQLite 规则展开，重复列名会按 SQLite 原生结果保留。

明确拒绝多条语句、写入和 DDL、CTE、子查询、自连接、外连接、CROSS JOIN、逗号连接、JOIN USING、OFFSET、窗口函数、未知表列和未知函数。只接受字面量 LIMIT；不支持 SQL 参数占位符、字符串函数、日期函数、CASE、集合运算和相关查询。

所有输入先由 sqlglot 解析为语法树，再校验节点、表和列；传给 SQLite 的是验证后生成的 SQL。只在逻辑上安全时将 branch_id 条件下推。若查询可证明完整落在一个站点，则在该站点执行完整 SQL；多站查询先从相关站点取基础表，再由主站内存 SQLite 执行完整 SQL，因此 DISTINCT、聚合、JOIN、全局排序与 LIMIT 按整体结果处理。任一站点失败或超过行数/超时限制时，主站报告失败，不返回部分结果。

## 安装与启动

需要 Python 3.10 或更新版本。

Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

macOS 或 Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

启动器会幂等重建三份演示数据，先启动 A / B / C，再启动主站；按 Ctrl+C 停止所有服务。只初始化数据而不启动服务：

```bash
python run.py --init-only
```

访问地址：

- 主站查询台：http://127.0.0.1:8000/
- 分站 A：http://127.0.0.1:8001/
- 分站 B：http://127.0.0.1:8002/
- 分站 C：http://127.0.0.1:8003/

主站点“单站裁剪 · B”后执行，可在 B 页面看到相同 request_id 的查询事件。再运行“跨站点连接”或“三站汇总”，比较主站计划中的站点、子查询及全局合并过程。配置修改后，`config/sites.json` 的端口、URL 与数据库路径用于服务启动和站点发现；跨电脑部署时将相应分站 URL 改为可访问地址，并修改其监听 host。

## 馆藏检索与浏览

- 主站的分片目录显示各分馆的图书、读者和借阅统计。点击分馆可进入该站页面。
- 跨站图书检索支持书名、作者和分类等关键词。结果按分馆编号、书名和图书编号排序，界面同时显示匹配总数。
- 分站页面支持切换图书、读者和借阅表，按关键字过滤并分页浏览。

## 初始化与测试

重复初始化会清空并重新填充各分站的演示表。每站有 215 本图书、88 位读者和 170 条借阅记录，包含分类、在借状态、已归还借阅和跨分馆查询数据。

```bash
python -m pytest -q
```

主要入口：`app/main.py`（主站 API）、`app/shard.py`（分站 API）、`app/sql_engine.py`（语法校验与分片规划）。
