package com.example.sql;

import java.io.File;

public class SqlRewriter {
    
    // Load the native library.
    static {
        // In production, this usually uses System.loadLibrary("sql_rewriter")
        // together with -Djava.library.path.
        // For this demo, load the compiled .so through an absolute path.
        String libPath = "/app/rust_lib/target/release/libsql_rewriter.so";
        File f = new File(libPath);
        if (f.exists()) {
            System.load(libPath);
        } else {
            System.err.println("Native library not found at: " + libPath);
            System.exit(1);
        }
    }

    // Native method declarations.
    public static native void initConfig(String jsonConfig);
    
    public static native RewriteResult rewriteSql(String sql);
}
