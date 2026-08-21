import psycopg2
import json
import sys

# ================= Configuration defaults =================
DEFAULT_CONFIG = {
    "host": "localhost",
    "port": 5433,
    "database": "admidio",
    "user": "admidio",
    "password": "password"
}

# Output file names.
JSON_OUTPUT_FILE = "temp_db_config.json"
PHP_OUTPUT_FILE = "temp_db_config.php"
TABLE_CONFIG_FILE = "temp_table_config.json"

# Schema to scan.
TARGET_SCHEMA = "public"
# ====================================================

def get_db_connection(config):
    """Create a database connection."""
    try:
        conn = psycopg2.connect(
            host=config["host"],
            port=config["port"],
            database=config["database"],
            user=config["user"],
            password=config["password"]
        )
        return conn
    except Exception as e:
        print(f"Database connection failed: {e}")
        sys.exit(1)

def get_all_tables(cursor, schema):
    """Return all table names under the given schema."""
    sql = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = %s AND table_type = 'BASE TABLE';
    """
    cursor.execute(sql, (schema,))
    tables = [row[0] for row in cursor.fetchall()]
    return tables

def get_primary_key(cursor, schema, table_name):
    """Return the primary-key column name for the given table."""
    sql = """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tco
        JOIN information_schema.key_column_usage kcu 
          ON kcu.constraint_name = tco.constraint_name
          AND kcu.table_schema = tco.table_schema
        WHERE tco.constraint_type = 'PRIMARY KEY'
          AND tco.table_schema = %s
          AND tco.table_name = %s
        ORDER BY kcu.ordinal_position
        LIMIT 1; 
    """
    cursor.execute(sql, (schema, table_name))
    result = cursor.fetchone()
    return result[0] if result else None

def save_json_config(final_data):
    """Save the JSON config file."""
    with open(JSON_OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_data, f, indent=2, ensure_ascii=False)
    print(f"JSON config file generated: {JSON_OUTPUT_FILE}")

def save_php_config(table_info):
    """
    Save the PHP config file.
    Format:
    const INSTRUMENT_TABLE_CONFIG = [
        ["table" => "users", "pk" => "id"], 
        ...
    ];
    """
    lines = []
    lines.append("<?php")
    lines.append("")
    lines.append("const INSTRUMENT_TABLE_CONFIG = [")
    
    for full_table_name, pk in table_info.items():
        # Strip the schema prefix, e.g. public.dag -> dag.
        # To keep the public. prefix, use pure_table_name = full_table_name.
        if "." in full_table_name:
            pure_table_name = full_table_name.split(".")[1]
        else:
            pure_table_name = full_table_name
            
        # Skip tables without a primary key.
        if not pk:
            continue
        pk_val = pk
        
        # Generate one PHP array row.
        line = f'    ["table" => "{pure_table_name}", "pk" => "{pk_val}"],'
        lines.append(line)

    lines.append("    ")
    lines.append("    // ... add other Craft tables you want to monitor")
    lines.append("];")
    
    # Write the file.
    with open(PHP_OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
        
    print(f"PHP config file generated: {PHP_OUTPUT_FILE}")

def save_table_config(table_info):
    """
    Save the table_config.json file.
    Format:
    [
        {"table": "users", "pk": "id"},
        ...
    ]
    """
    lines = []
    lines.append("[")
    
    
    for full_table_name, pk in table_info.items():
        # Strip the schema prefix, e.g. public.dag -> dag.
        # To keep the public. prefix, use pure_table_name = full_table_name.
        if "." in full_table_name:
            pure_table_name = full_table_name.split(".")[1]
        else:
            pure_table_name = full_table_name

        # Skip tables without a primary key.
        if not pk:
            continue
        pk_val = pk
        
        # Generate one JSON object row.
        line = f'    {{ "table": "{pure_table_name}", "pk": "{pk_val}" }},'
        lines.append(line)

    lines.append("]")
    
    # Write the file.
    with open(TABLE_CONFIG_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
        
    print(f"temp_table_config.json config file generated: {TABLE_CONFIG_FILE}")

def generate_config():
    print(f"Connecting to database {DEFAULT_CONFIG['host']}:{DEFAULT_CONFIG['port']}...")
    conn = get_db_connection(DEFAULT_CONFIG)
    cursor = conn.cursor()

    try:
        # 1. Load all tables.
        print(f"Scanning all tables under schema '{TARGET_SCHEMA}'...")
        tables = get_all_tables(cursor, TARGET_SCHEMA)
        print(f"Found {len(tables)} tables.")

        table_info = {}

        # 2. Collect primary keys for each table.
        for table in tables:
            pk = get_primary_key(cursor, TARGET_SCHEMA, table)
            full_table_name = f"{TARGET_SCHEMA}.{table}"
            table_info[full_table_name] = pk
            
            if not pk:
                print(f"Warning: table {full_table_name} has no primary key and was removed.")
                del table_info[full_table_name]

        # 3. Generate JSON.
        json_data = {
            "connection": DEFAULT_CONFIG,
            "table_info": table_info
        }
        save_json_config(json_data)

        # 4. Generate PHP.
        save_php_config(table_info)

        # 5. Generate table_config.json.
        save_table_config(table_info)

    except Exception as e:
        print(f"Error: {e}")
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    generate_config()
