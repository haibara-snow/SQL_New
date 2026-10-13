from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import sqlglot
from sqlglot import exp

from app.config import Settings
from app.schema import TABLE_COLUMNS, TABLES


class QueryValidationError(ValueError):
    pass


@dataclass
class ValidatedQuery:
    tree: exp.Select
    sql: str
    tables: list[str]
    aliases: dict[str, str]
    table_domains: dict[str, set[int] | None]
    colocated: bool


ALLOWED_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Select", "From", "Table", "TableAlias", "Identifier", "Column", "Star", "Alias", "Where", "Having", "Join",
        "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "And", "Or", "Not", "Like", "In", "Paren",
        "Literal", "Null", "Boolean", "Order", "Ordered", "Limit", "Group", "Distinct", "Count",
        "Sum", "Avg", "Min", "Max", "Add", "Sub", "Mul", "Div", "Neg", "Is",
    )
    if hasattr(exp, name)
)


def _table_name(table: exp.Table) -> str:
    if table.args.get("db") or table.args.get("catalog"):
        raise QueryValidationError("Qualified database names are not supported")
    name = table.name.casefold()
    if name not in TABLE_COLUMNS:
        raise QueryValidationError(f"Unknown logical table: {table.name}")
    return name


def _literal_branch_values(node: exp.Expression) -> set[int] | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            value = int(node.this)
        except (TypeError, ValueError):
            return None
        if str(value) == str(node.this):
            return {value}
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column):
        values: set[int] = set()
        for item in node.expressions:
            result = _literal_branch_values(item)
            if result is None or len(result) != 1:
                return None
            values.update(result)
        return values
    return None


def _is_branch_column(node: exp.Expression, alias: str, aliases: dict[str, str], table_count: int) -> bool:
    if not isinstance(node, exp.Column) or node.name.casefold() != "branch_id":
        return False
    qualifier = node.table.casefold()
    if qualifier:
        return qualifier == alias and qualifier in aliases
    return table_count == 1


def _domain_for(expression: exp.Expression | None, alias: str, aliases: dict[str, str]) -> set[int] | None:
    if expression is None:
        return None
    count = len(aliases)
    if isinstance(expression, exp.Paren):
        return _domain_for(expression.this, alias, aliases)
    if isinstance(expression, exp.And):
        left = _domain_for(expression.this, alias, aliases)
        right = _domain_for(expression.expression, alias, aliases)
        if left is None:
            return right
        if right is None:
            return left
        return left & right
    if isinstance(expression, exp.Or):
        left = _domain_for(expression.this, alias, aliases)
        right = _domain_for(expression.expression, alias, aliases)
        if left is None or right is None:
            return None
        return left | right
    if isinstance(expression, (exp.EQ, exp.In)):
        column: exp.Expression = expression.this
        value_expr: exp.Expression = expression.expression if isinstance(expression, exp.EQ) else expression
        if _is_branch_column(column, alias, aliases, count):
            values = _literal_branch_values(value_expr)
            if values is not None:
                return values
        if isinstance(expression, exp.EQ) and _is_branch_column(expression.expression, alias, aliases, count):
            values = _literal_branch_values(expression.this)
            if values is not None:
                return values
    return None


def _conjuncts(expression: exp.Expression | None) -> Iterable[exp.Expression]:
    if expression is None:
        return
    if isinstance(expression, exp.And):
        yield from _conjuncts(expression.this)
        yield from _conjuncts(expression.expression)
    else:
        yield expression


def _branch_equality(expression: exp.Expression) -> tuple[str, str] | None:
    if not isinstance(expression, exp.EQ):
        return None
    left, right = expression.this, expression.expression
    if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
        return None
    if left.name.casefold() != "branch_id" or right.name.casefold() != "branch_id":
        return None
    if not left.table or not right.table:
        return None
    return left.table.casefold(), right.table.casefold()


def _tables_are_colocated(tree: exp.Select, aliases: dict[str, str]) -> bool:
    if len(aliases) <= 1:
        return True
    graph = {alias: set() for alias in aliases}
    predicates: list[exp.Expression] = []
    for join in tree.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None:
            predicates.extend(_conjuncts(on))
    for predicate in predicates:
        pair = _branch_equality(predicate)
        if pair and pair[0] in graph and pair[1] in graph:
            graph[pair[0]].add(pair[1])
            graph[pair[1]].add(pair[0])
    reached = set()
    pending = [next(iter(graph))]
    while pending:
        alias = pending.pop()
        if alias in reached:
            continue
        reached.add(alias)
        pending.extend(graph[alias] - reached)
    return len(reached) == len(graph)


def validate_query(sql: str) -> ValidatedQuery:
    if not sql or not sql.strip():
        raise QueryValidationError("SQL must not be empty")
    if len(sql) > 12000:
        raise QueryValidationError("SQL exceeds the 12,000 character limit")
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError as exc:
        raise QueryValidationError(f"SQL syntax error: {exc.errors[0]['description']}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise QueryValidationError("Exactly one SQL statement is allowed")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise QueryValidationError("Only SELECT queries are allowed")
    if tree.args.get("with"):
        raise QueryValidationError("CTEs are not supported")
    if tree.args.get("offset"):
        raise QueryValidationError("OFFSET is not supported")
    if tree.args.get("qualify") or tree.args.get("windows") or tree.args.get("locks"):
        raise QueryValidationError("This SELECT clause is not supported")

    from_clause = tree.args.get("from_")
    if not from_clause or not isinstance(from_clause.this, exp.Table):
        raise QueryValidationError("FROM must name a logical table")
    table_nodes = [from_clause.this]
    joins = tree.args.get("joins") or []
    for join in joins:
        if not isinstance(join.this, exp.Table):
            raise QueryValidationError("Only joins between logical tables are supported")
        if join.args.get("side") or join.args.get("method") or join.args.get("kind") not in (None, "INNER"):
            raise QueryValidationError("Only INNER JOIN is supported")
        if join.args.get("using") or join.args.get("on") is None:
            raise QueryValidationError("JOIN must use an ON condition")
        table_nodes.append(join.this)
    aliases: dict[str, str] = {}
    for table in table_nodes:
        name = _table_name(table)
        alias = table.alias_or_name.casefold()
        if alias in aliases:
            raise QueryValidationError(f"Duplicate table alias: {alias}")
        aliases[alias] = name
    if len(set(aliases.values())) != len(aliases):
        raise QueryValidationError("Self joins are not supported")

    unsupported = [node for node in tree.walk() if not isinstance(node, ALLOWED_NODES)]
    if unsupported:
        raise QueryValidationError(f"Unsupported SQL syntax: {unsupported[0].key}")

    projection_aliases = {
        item.alias.casefold()
        for item in tree.expressions
        if isinstance(item, exp.Alias) and item.alias
    }
    for node in tree.walk():
        if isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            continue
        if isinstance(node, exp.Anonymous):
            raise QueryValidationError(f"Unsupported function: {node.name}")
        if isinstance(node, exp.Column):
            if isinstance(node.this, exp.Star):
                if node.table and node.table.casefold() not in aliases:
                    raise QueryValidationError(f"Unknown table qualifier: {node.table}")
                continue
            name = node.name.casefold()
            if node.table:
                table_name = aliases.get(node.table.casefold())
                if table_name is None:
                    raise QueryValidationError(f"Unknown table qualifier: {node.table}")
                if name not in TABLE_COLUMNS[table_name]:
                    raise QueryValidationError(f"Unknown column: {node.table}.{node.name}")
            elif name in projection_aliases:
                continue
            elif name not in {column for table_name in aliases.values() for column in TABLE_COLUMNS[table_name]}:
                raise QueryValidationError(f"Unknown column: {node.name}")
            elif sum(name in TABLE_COLUMNS[table_name] for table_name in aliases.values()) > 1:
                raise QueryValidationError(f"Ambiguous column: {node.name}; qualify it with a table alias")
    if tree.args.get("group") and not tree.args["group"].expressions:
        raise QueryValidationError("GROUP BY must contain at least one expression")
    if tree.args.get("limit"):
        limit = tree.args["limit"].expression
        try:
            valid_limit = isinstance(limit, exp.Literal) and not limit.is_string and str(int(limit.this)) == str(limit.this)
        except (TypeError, ValueError):
            valid_limit = False
        if not valid_limit or int(limit.this) < 0:
            raise QueryValidationError("LIMIT must be a non-negative integer literal")

    where = tree.args.get("where")
    table_domains = {
        alias: _domain_for(where.this if where else None, alias, aliases)
        for alias in aliases
    }
    colocated = _tables_are_colocated(tree, aliases)
    if colocated and len(aliases) > 1:
        constrained = [domain for domain in table_domains.values() if domain is not None]
        if constrained:
            common = set.intersection(*constrained)
            table_domains = {alias: common for alias in aliases}

    return ValidatedQuery(
        tree=tree,
        sql=tree.sql(dialect="sqlite"),
        tables=list(dict.fromkeys(aliases.values())),
        aliases=aliases,
        table_domains=table_domains,
        colocated=colocated,
    )


def branch_domain_for_alias(query: ValidatedQuery, alias: str) -> set[int] | None:
    return query.table_domains[alias]


def plan_sites(query: ValidatedQuery, settings: Settings) -> tuple[list[str], list[str], str | None]:
    branch_to_site = {site.branch_id: site.site_id for site in settings.sites}
    all_site_ids = [site.site_id for site in settings.sites]
    candidate_sites = list(all_site_ids)
    effective_domains = list(query.table_domains.values())
    if effective_domains and all(domain is not None for domain in effective_domains):
        possible = set().union(*(domain or set() for domain in effective_domains))
        candidate_sites = [branch_to_site[value] for value in sorted(possible) if value in branch_to_site]
    active_domains = list(query.table_domains.values())
    if query.tables and all(domain is not None and len(domain) == 1 for domain in active_domains):
        branches = {next(iter(domain)) for domain in active_domains if domain is not None}
        if len(branches) == 1:
            branch_id = next(iter(branches))
            if branch_id in branch_to_site:
                return candidate_sites, [branch_to_site[branch_id]], branch_to_site[branch_id]
    return candidate_sites, candidate_sites, None


def build_table_query(table_name: str, domain: set[int] | None) -> str:
    table = exp.Table(this=exp.Identifier(this=table_name, quoted=True))
    query = exp.Select(expressions=[exp.Star()]).from_(table)
    if domain is not None:
        if not domain:
            query = query.where(exp.false())
        else:
            values = [exp.Literal.number(value) for value in sorted(domain)]
            query = query.where(exp.In(this=exp.column("branch_id"), expressions=values))
    return query.sql(dialect="sqlite")


def make_branch_predicate(site_branch_id: int) -> str:
    return f"branch_id = {site_branch_id}"
