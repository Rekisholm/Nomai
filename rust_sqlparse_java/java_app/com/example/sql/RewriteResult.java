package com.example.sql;

public class RewriteResult {
    private String newSql;
    private String metaJson;
    private String errorMsg;

    public RewriteResult(String newSql, String metaJson, String errorMsg) {
        this.newSql = newSql;
        this.metaJson = metaJson;
        this.errorMsg = errorMsg;
    }

    public boolean isSuccess() {
        return errorMsg == null || errorMsg.isEmpty();
    }

    public String getNewSql() { return newSql; }
    public String getMetaJson() { return metaJson; }
    public String getErrorMsg() { return errorMsg; }

    @Override
    public String toString() {
        return "RewriteResult{" +
                "success=" + isSuccess() +
                ", newSql='" + newSql + '\'' +
                ", metaJson='" + metaJson + '\'' +
                ", errorMsg='" + errorMsg + '\'' +
                '}';
    }
}