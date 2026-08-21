package com.example.sql;

import java.io.File;

public class SqlRewriter {
    
    static {

        String libPath = "/app/rust_app/target/release/libjni_sql_rewriter.so";
        File f = new File(libPath);
        if (f.exists()) {
            System.load(libPath);
        } else {
            System.err.println("Native library not found at: " + libPath);
            System.exit(1);
        }
    }
    public static native void initConfig(String jsonConfig);
    
    public static native RewriteResult rewriteSql(String sql);
}