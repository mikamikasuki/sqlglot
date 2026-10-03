import json
import os
import subprocess
from pathlib import Path

import sys

sys.path.insert(0, str(Path.cwd()))
import sqlglot

password = os.environ["SQLSERVER_PASSWORD"]


def query(sql, database="HintValidation"):
    completed = subprocess.run(
        ["docker", "exec", "-i", "sqlserver-probes", "/opt/mssql-tools18/bin/sqlcmd",
         "-S", "localhost", "-U", "sa", "-P", password, "-C", "-b", "-W", "-d", database],
        input=("" if sql.startswith("CREATE FUNCTION") else "SET NOCOUNT ON;\n") + sql + "\nGO\n", text=True,
        capture_output=True, timeout=30,
    )
    return {"query": sql, "exit_code": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr}


setup = [
    "CREATE TABLE dbo.t (id INT NOT NULL); INSERT INTO dbo.t VALUES (1), (2);",
    "CREATE TABLE dbo.src (x INT NOT NULL, [NOLOCK] INT NOT NULL); INSERT INTO dbo.src VALUES (7, 11);",
    "CREATE FUNCTION dbo.fn(@value INT) RETURNS TABLE AS RETURN SELECT @value AS result;",
    "CREATE FUNCTION dbo.fn2(@first INT, @second INT) RETURNS TABLE AS RETURN SELECT @first + @second AS result;",
]
created = query("CREATE DATABASE HintValidation;", "master")
if created["exit_code"]:
    raise RuntimeError(created)
for statement in setup:
    result = query(statement)
    if result["exit_code"]:
        raise RuntimeError(result)

fixtures = [
    ("unqualified", "SELECT * FROM t (NOLOCK)", "SELECT * FROM t WITH (NOLOCK)"),
    ("qualified", "SELECT * FROM dbo.t (NOLOCK)", "SELECT * FROM dbo.t WITH (NOLOCK)"),
    ("quoted", "SELECT * FROM [dbo].[t] (NOLOCK)", "SELECT * FROM [dbo].[t] WITH (NOLOCK)"),
    ("rowlock", "SELECT * FROM dbo.t (ROWLOCK)", "SELECT * FROM dbo.t WITH (ROWLOCK)"),
    ("literal_argument", "SELECT * FROM dbo.fn(1)", "SELECT * FROM dbo.FN(1)"),
    ("correlated_argument", "SELECT * FROM dbo.src CROSS APPLY dbo.fn(x)", "SELECT * FROM dbo.src CROSS APPLY dbo.fn(x)"),
    ("multiple_arguments", "SELECT * FROM dbo.src CROSS APPLY dbo.fn2(NOLOCK, 1)", "SELECT * FROM dbo.src CROSS APPLY dbo.fn2(NOLOCK, 1)"),
    ("quoted_hint_argument", "SELECT * FROM dbo.src CROSS APPLY dbo.fn([NOLOCK])", "SELECT * FROM dbo.src CROSS APPLY dbo.fn([NOLOCK])"),
    ("alias_hint", "SELECT * FROM dbo.t AS a (NOLOCK)", "SELECT * FROM dbo.t AS a WITH (NOLOCK)"),
    ("unquoted_hint_argument", "SELECT * FROM dbo.src CROSS APPLY dbo.fn(NOLOCK)", "SELECT * FROM dbo.src CROSS APPLY dbo.fn(NOLOCK)"),
]
results = {"validation_workflow_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
           "parser_source_sha": "bffcdefc16b01527ee252ad2bf322b0bbff33bc8", "candidate_source_sha": None,
           "server_version": query("SELECT @@VERSION;"), "fixtures": []}
for name, original, canonical in fixtures:
    baseline_generated = sqlglot.parse_one(original, read="tsql").sql("tsql")
    record = {"name": name, "original": query(original), "canonical": query(canonical),
              "baseline_generated": query(baseline_generated)}
    print(json.dumps(record), flush=True)
    results["fixtures"].append(record)

temporary_setup = "CREATE TABLE #t (id INT NOT NULL); INSERT INTO #t VALUES (1), (2); "
original = "SELECT * FROM #t (NOLOCK)"
record = {"name": "temporary_table", "original": query(temporary_setup + original),
          "canonical": query(temporary_setup + "SELECT * FROM #t WITH (NOLOCK)"),
          "baseline_generated": query(temporary_setup + sqlglot.parse_one(original, read="tsql").sql("tsql"))}
print(json.dumps(record), flush=True)
results["fixtures"].append(record)
candidate = json.loads(Path(".github/candidate_sql_queries.json").read_text())
results["candidate_patch_sha256"] = candidate["candidate_patch_sha256"]
results["candidate_generated_sql"] = []
failures = []
for fixture in candidate["fixtures"]:
    prefix = temporary_setup if "#t" in fixture["original"] else ""
    record = {"original": query(prefix + fixture["original"]),
              "generated": query(prefix + fixture["generated"])}
    record["both_execute_same_output"] = (
        record["original"]["exit_code"] == record["generated"]["exit_code"] == 0
        and record["original"]["stdout"] == record["generated"]["stdout"]
    )
    if not record["both_execute_same_output"]:
        failures.append(fixture["original"])
    print(json.dumps(record), flush=True)
    results["candidate_generated_sql"].append(record)
Path("sqlserver-probe-results.json").write_text(json.dumps(results, indent=2) + "\n")
if failures:
    raise AssertionError("SQL Server execution/comparison failed: " + repr(failures))
