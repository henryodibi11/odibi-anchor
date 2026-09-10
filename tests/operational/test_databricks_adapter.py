from __future__ import annotations

import pytest

from odibi_anchor.operational import ContractError
from odibi_anchor.operational._databricks import DatabricksOperationalAdapter, quote_sql_identifier


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_identifier_validation_and_successful_uc_normalization() -> None:
    statements = []

    def sql(statement: str):
        statements.append(statement)
        if statement.startswith("DESCRIBE"):
            return [{"describe_json": '{"type":"MANAGED","location":"s3://bucket/table?token=secret"}'}]
        return [{"principal": "analysts", "action_type": "SELECT"}]

    result = DatabricksOperationalAdapter(sql_executor=sql).collect_uc("main.sales.orders")
    assert result.status == "collected"
    assert statements[0] == "DESCRIBE TABLE EXTENDED `main`.`sales`.`orders` AS JSON"
    assert result.facts["direct_grants"][0]["action_type"] == "SELECT"
    assert result.facts["tags"] is None
    assert any("not queried" in item for item in result.limitations)
    for unsafe in ("main.t; DROP TABLE x", "main.`t`", "main..t", "main.t --"):
        with pytest.raises(ContractError):
            quote_sql_identifier(unsafe)


def test_schema_drift_is_failed() -> None:
    adapter = DatabricksOperationalAdapter(sql_executor=lambda statement: [{"unexpected": 1}])
    result = adapter.collect_delta("main.default.events")
    assert result.status == "failed"
    assert "schema drift" in result.error["message"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [(ProviderError("PERMISSION_DENIED", status_code=403), "denied"),
     (ProviderError("TABLE_OR_VIEW_NOT_FOUND: system.access.audit"), "unavailable")],
)
def test_provider_failures_are_classified(error: Exception, expected: str) -> None:
    def sql(statement: str):
        raise error

    result = DatabricksOperationalAdapter(sql_executor=sql).collect_uc("system.access.audit")
    assert result.status == expected
    assert not result.findings


def test_delta_cdf_executor_and_result_are_aggregate_only() -> None:
    statements = []

    def sql(statement: str):
        statements.append(statement)
        if statement.startswith("DESCRIBE DETAIL"):
            return [{"format": "delta", "version": 999,
                     "properties": {"delta.enableChangeDataFeed": "true"}}]
        if statement.startswith("DESCRIBE HISTORY"):
            return [{"version": 12, "operation": "WRITE"}]
        return [{"_change_type": "insert", "change_count": 7}]

    result = DatabricksOperationalAdapter(sql_executor=sql).collect_delta(
        "main.default.events", start_version=10, end_version=12
    )
    assert result.status == "collected"
    assert result.facts["current_version"] == 12
    assert result.facts["cdf"]["enabled"] is True
    cdf_sql = statements[-1]
    assert "COUNT(*)" in cdf_sql and "GROUP BY _change_type" in cdf_sql
    assert "SELECT *" not in cdf_sql
    assert result.facts["cdf"]["change_counts"] == {"insert": 7, "update_preimage": 0,
                                                     "update_postimage": 0, "delete": 0}
    rendered = str(result.to_dict())
    assert "customer@example.com" not in rendered
    assert "raw_rows" not in rendered


def test_serverless_profile_and_api_environment_are_normalized() -> None:
    def api(operation, parameters):
        if operation == "query_profile":
            return {"plan_text": "Exchange hashpartitioning(id)",
                    "metrics": [{"spill_bytes": 3}], "query_id": parameters["query_id"]}
        return {"execution_mode": "serverless", "runtime": "17.0", "authorization": "Bearer secret"}

    adapter = DatabricksOperationalAdapter(api_executor=api)
    spark = adapter.collect_spark("query-1")
    environment = adapter.collect_environment()
    assert spark.status == "collected"
    assert spark.environment["execution_mode"] == "serverless"
    assert {finding["kind"] for finding in spark.findings} >= {"exchange", "spill"}
    assert environment.facts == {"runtime": "17.0", "execution_mode": "serverless"}


def test_missing_executor_is_unavailable_and_run_schema_drift_fails() -> None:
    assert DatabricksOperationalAdapter().collect_environment().status == "unavailable"
    adapter = DatabricksOperationalAdapter(api_executor=lambda operation, parameters: {"run_id": 4})
    assert adapter.collect_run(4).status == "failed"
