import com.example.agent.SqlRewriteJni;

public class Main {
    private static int passed = 0;

    public static void main(String[] args) {
        System.out.println("=== Java JNI Rust SQL Rewriter Regression ===");
        SqlRewriteJni.initConfig(
                "[{\"table\":\"users\",\"pk\":\"id\"},"
                        + "{\"table\":\"orders\",\"pk\":\"oid\"}]"
        );

        ordinaryProjection();
        cteWildcard();
        derivedWildcard();
        qualifiedDerivedWildcard();
        cteSelfJoinWildcard();
        cteSelfJoinExplicit();
        setOperationSafeSkip();
        projectionSubqueryCteSafeSkip();
        positionalParameter();
        ordinaryJoinRegression();

        System.out.println("ALL PASS: " + passed + "/10");
    }

    private static void ordinaryProjection() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "ordinary_projection",
                "SELECT name FROM users WHERE id = 1"
        );
        require(result.newSql.contains("users.id AS __pk_users_id"),
                "ordinary projection did not inject users.id");
        require(aliasCount(result.metaJson, "__pk_users_id") == 1,
                "ordinary projection metadata count changed");
        pass("ordinary_projection");
    }

    private static void cteWildcard() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "cte_wildcard",
                "WITH mylist AS (SELECT id, name FROM users WHERE id = 1) "
                        + "SELECT * FROM mylist"
        );
        require(count(result.newSql, " AS __pk_users_id") == 1,
                "CTE wildcard injected the PK more than once");
        require(!result.newSql.contains("mylist.__pk_users_id AS"),
                "CTE wildcard still has an explicit outer bridge");
        require(aliasCount(result.metaJson, "__pk_users_id") == 1,
                "CTE wildcard metadata is duplicated");
        pass("cte_wildcard");
    }

    private static void derivedWildcard() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "derived_wildcard",
                "SELECT * FROM (SELECT id, name FROM users WHERE id = 1) item"
        );
        require(count(result.newSql, " AS __pk_users_id") == 1,
                "derived wildcard injected the PK more than once");
        require(!result.newSql.contains("item.__pk_users_id AS"),
                "derived wildcard still has an explicit outer bridge");
        require(aliasCount(result.metaJson, "__pk_users_id") == 1,
                "derived wildcard metadata is duplicated");
        pass("derived_wildcard");
    }

    private static void qualifiedDerivedWildcard() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "qualified_derived_wildcard",
                "SELECT item.* FROM (SELECT id, name FROM users WHERE id = 1) item"
        );
        require(count(result.newSql, " AS __pk_users_id") == 1,
                "qualified wildcard injected the PK more than once");
        require(!result.newSql.contains("item.__pk_users_id AS"),
                "qualified wildcard still has an explicit outer bridge");
        pass("qualified_derived_wildcard");
    }

    private static void cteSelfJoinWildcard() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "cte_self_join_wildcard",
                "WITH selected AS (SELECT id FROM users) "
                        + "SELECT * FROM selected left_item "
                        + "JOIN selected right_item ON left_item.id = right_item.id"
        );
        require(count(result.newSql, " AS __pk_users_id") == 1,
                "self-join wildcard added explicit duplicate PK columns");
        require(!result.newSql.contains("left_item.__pk_users_id AS")
                        && !result.newSql.contains("right_item.__pk_users_id AS"),
                "self-join wildcard still has explicit bridges");
        require(aliasCount(result.metaJson, "__pk_users_id") == 2,
                "self-join wildcard should expose one PK per CTE reference");
        pass("cte_self_join_wildcard");
    }

    private static void cteSelfJoinExplicit() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "cte_self_join_explicit",
                "WITH selected AS (SELECT id FROM users) "
                        + "SELECT left_item.id, right_item.id FROM selected left_item "
                        + "JOIN selected right_item ON left_item.id = right_item.id"
        );
        require(result.newSql.contains("AS __pk_left_item_users_id"),
                "left CTE reference did not receive a unique alias");
        require(result.newSql.contains("AS __pk_right_item_users_id"),
                "right CTE reference did not receive a unique alias");
        require(aliasCount(result.metaJson, "__pk_left_item_users_id") == 1
                        && aliasCount(result.metaJson, "__pk_right_item_users_id") == 1,
                "explicit self-join metadata aliases are not unique");
        pass("cte_self_join_explicit");
    }

    private static void setOperationSafeSkip() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "set_operation_safe_skip",
                "WITH selected AS (SELECT id FROM users) "
                        + "SELECT * FROM selected UNION ALL SELECT 1"
        );
        require(!result.newSql.contains("__pk_") && "[]".equals(result.metaJson),
                "set operation was partially instrumented");
        pass("set_operation_safe_skip");
    }

    private static void projectionSubqueryCteSafeSkip() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "projection_subquery_cte_safe_skip",
                "SELECT (WITH nested AS (SELECT id FROM users) "
                        + "SELECT count(*) FROM nested) AS nested_count"
        );
        require(!result.newSql.contains("__pk_") && "[]".equals(result.metaJson),
                "allow_injection=false was not propagated into the nested CTE");
        pass("projection_subquery_cte_safe_skip");
    }

    private static void positionalParameter() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "psycopg_positional_parameter",
                "SELECT name FROM users WHERE id = %s"
        );
        require(result.newSql.contains("id = %s"),
                "positional %s parameter was not restored");
        require(result.newSql.contains("users.id AS __pk_users_id"),
                "positional parameter query was not instrumented");
        pass("psycopg_positional_parameter");
    }

    private static void ordinaryJoinRegression() {
        SqlRewriteJni.RewriteResult result = rewrite(
                "ordinary_join_regression",
                "SELECT u.name, o.amount FROM users u "
                        + "JOIN orders o ON u.id = o.user_id WHERE u.age > %(min_age)s"
        );
        require(result.newSql.contains("u.id AS __pk_u_id")
                        && result.newSql.contains("o.oid AS __pk_o_oid"),
                "ordinary JOIN instrumentation regressed");
        require(result.newSql.contains("%(min_age)s"),
                "named parameter restoration regressed");
        pass("ordinary_join_regression");
    }

    private static SqlRewriteJni.RewriteResult rewrite(String name, String sql) {
        SqlRewriteJni.RewriteResult result = SqlRewriteJni.rewriteSql(sql);
        require(result != null, name + " returned null");
        require(result.isSuccess(), name + " failed: " + result.errorMsg);
        return result;
    }

    private static int aliasCount(String metadata, String alias) {
        return count(metadata, "\"alias_used\":\"" + alias + "\"");
    }

    private static int count(String value, String needle) {
        int result = 0;
        int offset = 0;
        while ((offset = value.indexOf(needle, offset)) >= 0) {
            result++;
            offset += needle.length();
        }
        return result;
    }

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    private static void pass(String name) {
        passed++;
        System.out.println("[PASS] " + name);
    }
}
