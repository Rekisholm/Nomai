use jni::objects::{JClass, JObject, JString, JValue};
use jni::JNIEnv;
use lazy_static::lazy_static;
use regex::Regex;
use serde::{Deserialize, Serialize};
use sqlparser::ast::{
    Expr, GroupByExpr, Ident, ObjectName, Query, Select, SelectItem, SetExpr, Statement,
    TableFactor,
};
use sqlparser::ast::{FunctionArg, FunctionArgExpr};
use sqlparser::parser::Parser;
use std::collections::HashMap;
use std::collections::HashSet;
use std::ffi::CString;
use std::os::raw::c_char;
use std::sync::RwLock;
// --- 1. Configuration Management ---

const SNAPSHOT_XMIN_ALIAS: &str = "__nomai_xmin";
const SNAPSHOT_XMAX_ALIAS: &str = "__nomai_xmax";
const SNAPSHOT_PROJECTION_SQL: &str = "SELECT \
    (SELECT nomai_snapshot_bounds_xmin()) AS __nomai_xmin, \
    (SELECT nomai_snapshot_bounds_xmax()) AS __nomai_xmax";

lazy_static! {
    static ref RE_PY_PARAM: Regex = Regex::new(r"%\((\w+)\)s").unwrap();
    static ref RE_PY_POSITIONAL_PARAM: Regex = Regex::new(r"%s\b").unwrap();
    static ref RE_RESTORE_PARAM: Regex = Regex::new(r"__py_param_(\w+)").unwrap();
    static ref RE_RESTORE_POSITIONAL_PARAM: Regex =
        Regex::new(r"__py_positional_param\b").unwrap();
    static ref TABLE_CONFIG: RwLock<HashMap<String, String>> = RwLock::new(HashMap::new());
    static ref SNAPSHOT_BOUND_PROJECTION: Vec<SelectItem> =
        build_snapshot_projection().expect("valid snapshot projection SQL");
    // Aggregate function blacklist in uppercase.
    static ref AGGREGATE_FUNCTIONS: HashSet<&'static str> = HashSet::from_iter([
        // 1. Standard and common aggregate functions.
        "COUNT", "SUM", "AVG", "MIN", "MAX", "COUNT_BIG", "ANY_VALUE",
        // 2. Statistical analysis functions, including PostgreSQL native functions.
        "STDDEV", "STDDEV_SAMP", "STDDEV_POP", "VARIANCE", "VAR_SAMP", "VAR_POP",
        "COVAR_POP", "COVAR_SAMP", "CORR", "REGR_SLOPE", "REGR_INTERCEPT", "REGR_COUNT",
        "REGR_R2", "REGR_AVGX", "REGR_AVGY", "REGR_SXX", "REGR_SYY", "REGR_SXY",
        // 3. Ordered-set aggregates with WITHIN GROUP.
        "MODE", "PERCENTILE_CONT", "PERCENTILE_DISC",
        // 4. Collection and text construction aggregates.
        "ARRAY_AGG", "STRING_AGG", "XMLAGG", "JSON_AGG", "JSONB_AGG", "JSON_OBJECT_AGG", "JSONB_OBJECT_AGG",
        // 5. Boolean and bitwise aggregates.
        "BOOL_AND", "BOOL_OR", "EVERY", "BIT_AND", "BIT_OR", "BIT_XOR",
        // 6. Approximate and range aggregates.
        "APPROX_COUNT_DISTINCT", "APPROX_PERCENTILE_CONT", "APPROX_PERCENTILE_DISC", "RANGE_AGG",
        // 7. Compatibility aliases.
        "GROUP_CONCAT",
    ]);
}

#[derive(Deserialize)]
struct ConfigItem {
    table: String,
    pk: String,
}

// --- 2. Data Structures ---

#[repr(C)] // Preserve the C memory layout.
pub struct RewriteResult {
    pub new_sql: *mut c_char,
    pub meta_json: *mut c_char,
    pub error_msg: *mut c_char,
}

impl RewriteResult {
    fn success(sql: String, meta: String) -> Self {
        Self {
            new_sql: CString::new(sql).unwrap().into_raw(),
            meta_json: CString::new(meta).unwrap().into_raw(),
            error_msg: std::ptr::null_mut(),
        }
    }

    fn error(msg: String) -> Self {
        Self {
            new_sql: std::ptr::null_mut(),
            meta_json: std::ptr::null_mut(),
            error_msg: CString::new(msg).unwrap().into_raw(),
        }
    }
}

#[derive(Serialize, Clone)]
struct InjectionMeta {
    table: String,
    pk_col: String,
    alias_used: String,
    snapshot_xmin_alias: &'static str,
    snapshot_xmax_alias: &'static str,
}

// CTE name -> metadata entries injected inside that CTE.
type CteScope = HashMap<String, Vec<InjectionMeta>>;

// --- 3. Core Processing Logic ---

fn process_sql(sql: &str) -> Result<(String, String), String> {
    // Replace Python parameter placeholders so sqlparser can parse %(...)s.
    let masked_named_sql = RE_PY_PARAM.replace_all(sql, "__py_param_$1");
    let masked_sql = RE_PY_POSITIONAL_PARAM.replace_all(&masked_named_sql, "__py_positional_param");
    let dialect = sqlparser::dialect::GenericDialect {};
    let mut ast = Parser::parse_sql(&dialect, &masked_sql).map_err(|e| e.to_string())?;

    let config = TABLE_CONFIG.read().map_err(|_| "Lock error")?;

    let mut all_injected_meta = Vec::new();
    let empty_scope = HashMap::new(); // Root frame of the CTE scope stack.

    // Recursively process each statement.
    for statement in &mut ast {
        if let Statement::Query(query) = statement {
            let metas = process_query(query, &config, &empty_scope, true);
            if !metas.is_empty() {
                append_snapshot_bounds(query)?;
            }
            all_injected_meta.extend(metas);
        }
    }

    // Serialize the AST back to SQL, joining multiple statements.
    let new_sql_masked = ast
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>()
        .join(";\n");
    let restored_named_sql = RE_RESTORE_PARAM.replace_all(&new_sql_masked, "%($1)s");
    let final_sql = RE_RESTORE_POSITIONAL_PARAM
        .replace_all(&restored_named_sql, "%s")
        .to_string();
    let meta_json = serde_json::to_string(&all_injected_meta).unwrap();

    Ok((final_sql, meta_json))
}

fn build_snapshot_projection() -> Result<Vec<SelectItem>, String> {
    let dialect = sqlparser::dialect::GenericDialect {};
    let mut statements = Parser::parse_sql(&dialect, SNAPSHOT_PROJECTION_SQL)
        .map_err(|error| format!("failed to build snapshot projection: {error}"))?;
    let statement = statements
        .pop()
        .ok_or_else(|| "failed to build snapshot projection".to_string())?;
    match statement {
        Statement::Query(snapshot_query) => match *snapshot_query.body {
            SetExpr::Select(snapshot_select) => Ok(snapshot_select.projection),
            _ => Err("snapshot projection is not a SELECT".to_string()),
        },
        _ => Err("snapshot projection is not a query".to_string()),
    }
}

/// Append active statement-snapshot bounds after the injected PK columns.
/// The cached AST keeps rewrite overhead constant and the scalar subqueries
/// allow one-time InitPlan evaluation. Snapshot correctness comes from the C
/// functions reading GetActiveSnapshot(), not from evaluation timing.
fn append_snapshot_bounds(query: &mut Query) -> Result<(), String> {
    let select = match query.body.as_mut() {
        SetExpr::Select(select) => select,
        _ => return Err("cannot append snapshot bounds to non-SELECT query body".to_string()),
    };
    select.projection.extend(SNAPSHOT_BOUND_PROJECTION.iter().cloned());
    Ok(())
}

/// Recursively process a Query node, including WITH/CTE clauses.
fn process_query(
    query: &mut Query,
    config: &HashMap<String, String>,
    parent_scope: &CteScope,
    allow_injection: bool,
) -> Vec<InjectionMeta> {
    let mut collected = Vec::new();
    let mut current_scope = parent_scope.clone();

    // Set operations must be handled as a whole across all branches; otherwise
    // injecting only into CTEs can change branch column counts.
    // For now, skip the full set-operation query, including its CTE definitions.
    if set_expr_contains_set_operation(query.body.as_ref()) {
        return collected;
    }

    // Process each subquery in WITH/CTE clauses first; they may also receive injections.
    if let Some(with) = &mut query.with {
        for cte in &mut with.cte_tables {
            // cte.query is a Box<Query>.
            let inner_metas =
                process_query(&mut cte.query, config, &current_scope, allow_injection);
            let cte_name = cte.alias.name.value.to_lowercase();
            current_scope.insert(cte_name, inner_metas.clone());
        }
    }

    // Then process the current query body.
    match &mut *query.body {
        SetExpr::Select(boxed_select) => {
            let metas = process_select(boxed_select, config, &current_scope, allow_injection);
            collected.extend(metas);
        }
        _ => {
            // Other types, such as UNION and EXCEPT, are not handled for now.
        }
    }

    collected
}

fn set_expr_contains_set_operation(expr: &SetExpr) -> bool {
    match expr {
        SetExpr::SetOperation { .. } => true,
        SetExpr::Query(query) => set_expr_contains_set_operation(query.body.as_ref()),
        _ => false,
    }
}

/// Process a SELECT node and return metadata injected at the current level.
fn process_select(
    select: &mut Select,
    config: &HashMap<String, String>,
    scope_ctes: &CteScope,
    allow_injection: bool,
) -> Vec<InjectionMeta> {
    if select.distinct.is_some() {
        return Vec::new();
    }
    if !matches!(&select.group_by, GroupByExpr::Expressions(exprs) if exprs.is_empty()) {
        return Vec::new();
    }

    // Skip injection if the top-level projection contains an aggregate function
    // that is not inside a subquery.
    for item in &select.projection {
        if select_item_contains_aggregate(item) {
            return Vec::new();
        }
    }

    // Recursively process subqueries in the projection.
    for item in &mut select.projection {
        scan_and_process_subqueries_in_select_item(item, config, scope_ctes);
    }
    if !allow_injection {
        return Vec::new();
    }

    let mut current_level_meta = Vec::new();

    // Traverse FROM and JOIN entries.
    for table_with_joins in &mut select.from {
        // Process the main relation, which may be a table or a derived subquery.
        let metas = process_table_factor(
            &mut table_with_joins.relation,
            config,
            scope_ctes,
            &mut select.projection,
            true,
        );
        current_level_meta.extend(metas);

        // Process JOIN entries.
        for join in &mut table_with_joins.joins {
            let metas = process_table_factor(
                &mut join.relation,
                config,
                scope_ctes,
                &mut select.projection,
                true,
            );
            current_level_meta.extend(metas);
        }
    }

    current_level_meta
}

/// Process a table factor.
fn process_table_factor(
    relation: &mut TableFactor,
    config: &HashMap<String, String>,
    scope_ctes: &CteScope, // Known CTE entries.
    projection: &mut Vec<SelectItem>,
    allow_injection: bool,
) -> Vec<InjectionMeta> {
    if !allow_injection {
        return Vec::new();
    }

    let mut injected_metas = Vec::new();

    match relation {
        // Base table.
        TableFactor::Table { name, alias, .. } => {
            let full_table_name = object_name_to_string(name);
            let pure_table_name = name.0.last().unwrap().value.clone();
            let lower_full = full_table_name.to_lowercase();
            let lower_pure = pure_table_name.to_lowercase();

            // Determine the prefix Ident while preserving quote style.
            // Use the alias Ident when present; otherwise use the final table-name Ident.
            let prefix_ident = if let Some(a) = alias {
                a.name.clone()
            } else {
                name.0.last().unwrap().clone()
            };

            // Get the plain prefix text for building the safe_alias string.
            let prefix_str = prefix_ident.value.clone();

            // CTEs shadow base tables with the same name, so check the current CTE scope first.
            if let Some(cte_metas) = scope_ctes.get(&lower_pure) {
                bridge_injected_columns(cte_metas, &prefix_ident, projection, &mut injected_metas);
            } else if let Some(pk_name) =
                config.get(&lower_full).or_else(|| config.get(&lower_pure))
            {
                // Generate a safe alias. Output column aliases usually do not need quotes.
                let safe_prefix = prefix_str.replace('.', "_");
                let inject_alias = format!("__pk_{}_{}", safe_prefix, pk_name).to_lowercase();

                let needs_quote = pk_name.chars().any(|c| c.is_uppercase());
                // Build CompoundIdentifier: prefix.pk_name.
                let pk_ident = Ident {
                    value: pk_name.clone(),
                    quote_style: if needs_quote { Some('"') } else { None },
                };

                let idents = vec![prefix_ident.clone(), pk_ident];

                projection.push(SelectItem::ExprWithAlias {
                    expr: Expr::CompoundIdentifier(idents),
                    alias: Ident::new(inject_alias.clone()),
                });

                injected_metas.push(InjectionMeta {
                    table: full_table_name,
                    pk_col: pk_name.clone(),
                    alias_used: inject_alias,
                    snapshot_xmin_alias: SNAPSHOT_XMIN_ALIAS,
                    snapshot_xmax_alias: SNAPSHOT_XMAX_ALIAS,
                });
            }
        }

        // Derived table: FROM (SELECT ...) AS alias
        TableFactor::Derived {
            subquery, alias, ..
        } => {
            // Recursively process the subquery and collect its inner injections.
            let inner_metas = process_query(subquery.as_mut(), config, scope_ctes, true);

            // If the inner query injected PKs and the outer relation has an alias,
            // map the injected columns into the outer projection.
            if !inner_metas.is_empty() {
                if let Some(alias_node) = alias {
                    // Get the alias Ident, including quote information.
                    let table_alias_ident = alias_node.name.clone();
                    bridge_injected_columns(
                        &inner_metas,
                        &table_alias_ident,
                        projection,
                        &mut injected_metas,
                    );
                } else {
                    // Without an alias, a derived table cannot be bridged for outer
                    // references under SQL semantics.
                }
            }
        }

        // Other types, such as TableFunction and UNNEST, are skipped for now.
        _ => {}
    }

    injected_metas
}

// CTEs and derived tables may already contain injected PK columns. An outer
// wildcard automatically carries those columns, so do not push them explicitly.
// Explicit projections use the referenced alias to build unique bridged names.
fn bridge_injected_columns(
    inner_metas: &[InjectionMeta],
    relation_ident: &Ident,
    projection: &mut Vec<SelectItem>,
    injected_metas: &mut Vec<InjectionMeta>,
) {
    if projection_wildcard_covers(projection, relation_ident) {
        injected_metas.extend(inner_metas.iter().cloned());
        return;
    }

    for inner_meta in inner_metas {
        let output_alias = bridged_alias(relation_ident, &inner_meta.alias_used);
        projection.push(SelectItem::ExprWithAlias {
            expr: Expr::CompoundIdentifier(vec![
                relation_ident.clone(),
                Ident::new(inner_meta.alias_used.clone()),
            ]),
            alias: Ident::new(output_alias.clone()),
        });

        let mut output_meta = inner_meta.clone();
        output_meta.alias_used = output_alias;
        injected_metas.push(output_meta);
    }
}

fn projection_wildcard_covers(projection: &[SelectItem], relation_ident: &Ident) -> bool {
    projection.iter().any(|item| match item {
        SelectItem::Wildcard(_) => true,
        SelectItem::QualifiedWildcard(name, _) => name
            .0
            .last()
            .map(|ident| identifiers_match(ident, relation_ident))
            .unwrap_or(false),
        _ => false,
    })
}

fn identifiers_match(left: &Ident, right: &Ident) -> bool {
    match (left.quote_style, right.quote_style) {
        (None, None) => left.value.eq_ignore_ascii_case(&right.value),
        _ => left.value == right.value,
    }
}

fn bridged_alias(relation_ident: &Ident, inner_alias: &str) -> String {
    let relation = relation_ident
        .value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || character == '_' {
                character
            } else {
                '_'
            }
        })
        .collect::<String>();
    let inner = inner_alias.strip_prefix("__pk_").unwrap_or(inner_alias);
    format!("__pk_{}_{}", relation, inner).to_lowercase()
}

// Helper: convert ObjectName to a schema.table string.
fn object_name_to_string(name: &ObjectName) -> String {
    name.0
        .iter()
        .map(|i| i.value.clone())
        .collect::<Vec<_>>()
        .join(".")
}

// Detect whether a SelectItem contains an aggregate function.
fn select_item_contains_aggregate(item: &SelectItem) -> bool {
    match item {
        SelectItem::UnnamedExpr(expr) => expr_contains_aggregate(expr),
        SelectItem::ExprWithAlias { expr, alias: _ } => expr_contains_aggregate(expr),
        // Wildcard (*) does not contain aggregates.
        SelectItem::QualifiedWildcard(_, _) | SelectItem::Wildcard(_) => false,
    }
}

// Recursively detect whether an Expr contains an aggregate function.
fn expr_contains_aggregate(expr: &Expr) -> bool {
    match expr {
        Expr::Function(func) => {
            // Check whether the function name matches the blacklist.
            let name = object_name_to_string(&func.name).to_uppercase();
            if AGGREGATE_FUNCTIONS.contains(name.as_str()) {
                return true;
            }

            // If this is not an aggregate function, such as UPPER or ABS, still
            // inspect its arguments to catch cases like ABS(SUM(amount)).
            for arg in &func.args {
                match arg {
                    FunctionArg::Named { arg: arg_expr, .. } => {
                        if function_arg_expr_contains_aggregate(arg_expr) {
                            return true;
                        }
                    }
                    FunctionArg::Unnamed(arg_expr) => {
                        if function_arg_expr_contains_aggregate(arg_expr) {
                            return true;
                        }
                    }
                }
            }
            // Treat functions with an OVER clause as complex queries and skip them.
            if func.over.is_some() {
                return true;
            }

            false
        }

        // Recursively inspect other expression types.
        Expr::BinaryOp { left, right, .. } => {
            expr_contains_aggregate(left) || expr_contains_aggregate(right)
        }
        Expr::UnaryOp { expr, .. } => expr_contains_aggregate(expr),
        Expr::Nested(e) => expr_contains_aggregate(e),
        Expr::Case {
            operand,
            conditions,
            results,
            else_result,
        } => {
            if let Some(op) = operand {
                if expr_contains_aggregate(op) {
                    return true;
                }
            }
            for c in conditions {
                if expr_contains_aggregate(c) {
                    return true;
                }
            }
            for r in results {
                if expr_contains_aggregate(r) {
                    return true;
                }
            }
            if let Some(er) = else_result {
                if expr_contains_aggregate(er) {
                    return true;
                }
            }
            false
        }
        Expr::Cast { expr, .. } => expr_contains_aggregate(expr),
        Expr::Extract { expr, .. } => expr_contains_aggregate(expr),
        Expr::Between {
            expr, low, high, ..
        } => {
            expr_contains_aggregate(expr)
                || expr_contains_aggregate(low)
                || expr_contains_aggregate(high)
        }
        Expr::InList { expr, list, .. } => {
            if expr_contains_aggregate(expr) {
                return true;
            }
            for it in list {
                if expr_contains_aggregate(it) {
                    return true;
                }
            }
            false
        }
        Expr::Like { expr, pattern, .. } => {
            expr_contains_aggregate(expr) || expr_contains_aggregate(pattern)
        }
        // Aggregates inside subqueries do not affect the outer query for this
        // projection check. scan_and_process_subqueries handles subqueries separately.
        Expr::Subquery(_) => false,

        _ => false,
    }
}

// Helper: handle FunctionArgExpr.
fn function_arg_expr_contains_aggregate(arg_expr: &FunctionArgExpr) -> bool {
    match arg_expr {
        FunctionArgExpr::Expr(e) => expr_contains_aggregate(e),
        // Wildcard (*) is usually used with COUNT, which is already caught by name.
        // For custom func(*) calls, assume they are not aggregates.
        FunctionArgExpr::Wildcard | FunctionArgExpr::QualifiedWildcard(_) => false,
    }
}

// Find Expr::Subquery in a SelectItem and process it recursively.
fn scan_and_process_subqueries_in_select_item(
    item: &mut SelectItem,
    config: &HashMap<String, String>,
    scope_ctes: &CteScope,
) {
    match item {
        SelectItem::UnnamedExpr(expr) => {
            scan_and_process_subqueries_in_expr(expr, config, scope_ctes);
        }
        SelectItem::ExprWithAlias { expr, alias: _ } => {
            scan_and_process_subqueries_in_expr(expr, config, scope_ctes);
        }
        _ => {}
    }
}

// Recursively find and process Expr::Subquery without modifying the current expr.
fn scan_and_process_subqueries_in_expr(
    expr: &mut Expr,
    config: &HashMap<String, String>,
    scope_ctes: &CteScope,
) {
    match expr {
        Expr::Subquery(q) => {
            let _ = process_query(q.as_mut(), config, scope_ctes, false);
        }
        Expr::BinaryOp { left, right, .. } => {
            scan_and_process_subqueries_in_expr(left, config, scope_ctes);
            scan_and_process_subqueries_in_expr(right, config, scope_ctes);
        }
        Expr::UnaryOp { expr, .. } => {
            scan_and_process_subqueries_in_expr(expr, config, scope_ctes);
        }
        Expr::Nested(inner) => {
            scan_and_process_subqueries_in_expr(inner, config, scope_ctes);
        }
        Expr::Cast { expr, .. } => {
            scan_and_process_subqueries_in_expr(expr, config, scope_ctes);
        }
        Expr::Extract { expr, .. } => {
            scan_and_process_subqueries_in_expr(expr, config, scope_ctes);
        }
        Expr::Case {
            operand,
            conditions,
            results,
            else_result,
        } => {
            if let Some(op) = operand {
                scan_and_process_subqueries_in_expr(op, config, scope_ctes);
            }
            for c in conditions {
                scan_and_process_subqueries_in_expr(c, config, scope_ctes);
            }
            for r in results {
                scan_and_process_subqueries_in_expr(r, config, scope_ctes);
            }
            if let Some(er) = else_result {
                scan_and_process_subqueries_in_expr(er, config, scope_ctes);
            }
        }
        Expr::InList { expr, list, .. } => {
            scan_and_process_subqueries_in_expr(expr, config, scope_ctes);
            for it in list {
                scan_and_process_subqueries_in_expr(it, config, scope_ctes);
            }
        }
        Expr::Like { expr, pattern, .. } => {
            scan_and_process_subqueries_in_expr(expr, config, scope_ctes);
            scan_and_process_subqueries_in_expr(pattern, config, scope_ctes);
        }
        // Extend this branch if other Expr variants with subexpressions need support.
        _ => {}
    }
}

// --- 4. JNI Interface Implementation ---

// Class: com.example.agent.SqlRewriteJni
// Method: initConfig
#[no_mangle]
pub extern "system" fn Java_com_example_agent_SqlRewriteJni_initConfig<'local>(
    mut env: JNIEnv<'local>,
    _class: JClass<'local>,
    json_str: JString<'local>,
) {
    let input: String = match env.get_string(&json_str) {
        Ok(s) => s.into(),
        Err(_) => return, // Java will throw the exception.
    };

    let configs: Vec<ConfigItem> = match serde_json::from_str(&input) {
        Ok(c) => c,
        Err(e) => {
            let _ = env.throw_new(
                "java/lang/IllegalArgumentException",
                format!("JSON Error: {}", e),
            );
            return;
        }
    };

    // Acquire the exclusive write lock. This blocks all rewrite operations until
    // the update is complete. It is called only during agent startup or config updates.
    let mut map = match TABLE_CONFIG.write() {
        Ok(m) => m,
        Err(_) => {
            let _ = env.throw_new("java/lang/RuntimeException", "Rust RwLock poisoned");
            return;
        }
    };

    map.clear();
    for item in configs {
        map.insert(item.table.to_lowercase(), item.pk);
    }
}

// Class: com.example.agent.SqlRewriteJni
// Method: rewriteSql
// Return: com.example.agent.SqlRewriteJni$RewriteResult
#[no_mangle]
pub extern "system" fn Java_com_example_agent_SqlRewriteJni_rewriteSql<'local>(
    mut env: JNIEnv<'local>,
    _class: JClass<'local>,
    sql_jstr: JString<'local>,
) -> JObject<'local> {
    let sql: String = match env.get_string(&sql_jstr) {
        Ok(s) => s.into(),
        Err(_) => return JObject::null(),
    };

    // Execute the rewrite logic.
    let (new_sql, meta, error_msg) = match process_sql(&sql) {
        Ok((s, m)) => (s, m, String::new()),
        Err(e) => (String::new(), String::new(), e),
    };

    // Construct the Java object.
    // Inner classes use $ in JNI signatures, such as
    // com/example/agent/SqlRewriteJni$RewriteResult.
    let result_cls_name = "com/example/agent/SqlRewriteJni$RewriteResult";
    let result_cls = match env.find_class(result_cls_name) {
        Ok(c) => c,
        Err(_) => {
            let _ = env.throw_new(
                "java/lang/ClassNotFoundException",
                format!("Could not find {}", result_cls_name),
            );
            return JObject::null();
        }
    };

    let j_new_sql = env.new_string(new_sql).unwrap();
    let j_meta = env.new_string(meta).unwrap();
    let j_error = env.new_string(error_msg).unwrap();

    // Call the constructor.
    let result_obj = env.new_object(
        result_cls,
        "(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)V",
        &[
            JValue::Object(&j_new_sql),
            JValue::Object(&j_meta),
            JValue::Object(&j_error),
        ],
    );

    match result_obj {
        Ok(obj) => obj,
        Err(e) => {
            let _ = env.throw_new(
                "java/lang/RuntimeException",
                format!("JNI Object creation failed: {}", e),
            );
            JObject::null()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn active_snapshot_bounds_and_metadata_are_injected() {
        let mut config = TABLE_CONFIG.write().unwrap();
        config.clear();
        config.insert("items".to_string(), "id".to_string());
        drop(config);

        let (rewritten, metadata) = process_sql("SELECT name FROM items").unwrap();
        assert!(rewritten.contains("nomai_snapshot_bounds_xmin()"));
        assert!(rewritten.contains("nomai_snapshot_bounds_xmax()"));
        assert!(!rewritten.contains("txid_current_snapshot"));
        assert_eq!(rewritten.matches(SNAPSHOT_XMIN_ALIAS).count(), 1);
        assert_eq!(rewritten.matches(SNAPSHOT_XMAX_ALIAS).count(), 1);

        let metadata: serde_json::Value = serde_json::from_str(&metadata).unwrap();
        assert_eq!(metadata.as_array().unwrap().len(), 1);
        assert_eq!(metadata[0]["snapshot_xmin_alias"], SNAPSHOT_XMIN_ALIAS);
        assert_eq!(metadata[0]["snapshot_xmax_alias"], SNAPSHOT_XMAX_ALIAS);
    }
}
