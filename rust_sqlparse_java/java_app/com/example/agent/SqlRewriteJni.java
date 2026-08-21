package com.example.agent;

import java.io.File;

/** Minimal Java app binding for the production JNI class and symbols. */
public final class SqlRewriteJni {
    public static final class RewriteResult {
        public final String newSql;
        public final String metaJson;
        public final String errorMsg;

        public RewriteResult(String newSql, String metaJson, String errorMsg) {
            this.newSql = newSql;
            this.metaJson = metaJson;
            this.errorMsg = errorMsg;
        }

        public boolean isSuccess() {
            return errorMsg == null || errorMsg.isEmpty();
        }
    }

    static {
        String defaultPath = "/app/rust_app/target/release/libjni_sql_rewriter.so";
        String libraryPath = System.getProperty("sql.rewriter.library", defaultPath);
        File library = new File(libraryPath).getAbsoluteFile();
        if (!library.isFile()) {
            throw new ExceptionInInitializerError(
                    "Native library not found: " + library.getAbsolutePath()
            );
        }
        System.load(library.getAbsolutePath());
        System.out.println("Loaded JNI library: " + library.getAbsolutePath());
    }

    private SqlRewriteJni() {}

    public static native void initConfig(String configJson);

    public static native RewriteResult rewriteSql(String sql);
}
