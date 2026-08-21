# /opt/gitlab/embedded/service/gitlab-rails/config/initializers/hook_pg.rb
require 'ffi'
require 'json'
require 'time'
require 'set'

module HpExperimentFlags
  FALSE_VALUES = ['0', 'false', 'no', 'off'].freeze

  def self.enabled?(name, default_value = '1')
    !FALSE_VALUES.include?(ENV.fetch(name, default_value).to_s.strip.downcase)
  end
end

# ==========================================
# 1. Logging module
# ==========================================
module PgPatchLogger
  LOG_FILE = '/tmp/logs/db_read.log'.freeze
  ERROR_FILE = '/tmp/logs/db_error.log'.freeze
  
  def self.write(message)
    File.open(LOG_FILE, 'a') { |f| f.puts(message.strip) } rescue nil
  end

  def self.error(message)
    # File.open(ERROR_FILE, 'a') { |f| f.puts("[#{Time.now}] #{message.strip}") } rescue nil
    # Skip directly and do not write logs
  end
end

# ==========================================
# 2. Rust SQL rewriter (FFI)
# ==========================================
module RustSqlRewriter
  extend FFI::Library

  SO_PATH = ENV.fetch('SQL_REWRITER_SO_PATH', '/tmp/librust_sql_rewriter.so')
  CONFIG_PATH = '/tmp/table_config.json'.freeze
  ENABLED = HpExperimentFlags.enabled?('HP_PG_HOOK') && HpExperimentFlags.enabled?('HP_SQL_REWRITE')

  class RewriteResult < FFI::Struct
    layout :new_sql,    :string,
           :meta_json,  :string,
           :error_msg,  :string
  end

  @lib_loaded = false

  begin
    if ENABLED && File.exist?(SO_PATH)
      ffi_lib SO_PATH
      attach_function :init_config, [:string], :int
      attach_function :rewrite_sql, [:string], RewriteResult.by_value
      attach_function :free_rewrite_result, [RewriteResult.by_value], :void
      
      json_config_str = nil

      begin
        if File.exist?(CONFIG_PATH)
          file_content = File.read(CONFIG_PATH)
          parsed_config = JSON.parse(file_content)
          json_config_str = parsed_config.to_json
          # PgPatchLogger.write("[RustRewriter] Loaded config from #{CONFIG_PATH}")
        else
          PgPatchLogger.error("[RustRewriter] Config file not found at #{CONFIG_PATH}, using default.")
        end
      rescue => e
        PgPatchLogger.error("[RustRewriter] Failed to load config file: #{e.message}")
      end

      if init_config(json_config_str) == 0
        @lib_loaded = true
      else
        PgPatchLogger.error("Rust init_config failed.")
      end
    else
      PgPatchLogger.error("Rust .so file not found.")
    end
  rescue => e
    PgPatchLogger.error("FFI Load Error: #{e.message}")
  end

  def self.loaded?
    ENABLED && @lib_loaded
  end

  def self.rewrite(sql)
    return [nil, []] unless loaded?
    # Ruby 2.3 compatibility
    #return [nil, []] unless sql =~ /^\s*SELECT/i

    begin
      result = rewrite_sql(sql)
      new_sql = nil
      meta = []

      if result[:new_sql] && !result[:new_sql].empty?
        new_sql = result[:new_sql]
        if result[:meta_json] && !result[:meta_json].empty?
          meta = JSON.parse(result[:meta_json]) rescue []
        end
      end
      
      free_rewrite_result(result)
      return [new_sql, meta]
    rescue => e
      PgPatchLogger.error("Rewrite Exception: #{e.message}")
      return [nil, []]
    end
  end
end

# ==========================================
# 3. Result auditing and cleanup module
# ==========================================
module PgResultAuditor
  SNAPSHOT_COLUMN_COUNT = 2
  READ_LOG_ENABLED = HpExperimentFlags.enabled?('HP_READ_LOG')
  attr_accessor :_audit_meta, :_audit_req_id, :_audit_seq

  def values
    rows = super
    return rows unless should_audit? && !rows.empty?
    process_rows_and_strip!(rows, :array)
    rows
  end

  def each(&block)
    return super unless should_audit?
    return super unless block_given?

    super do |row|
      row_wrapper = [row]
      process_rows_and_strip!(row_wrapper, row.is_a?(Array) ? :array : :hash)
      yield row_wrapper.first
    end
  end

  def fields
    f = super
    return f unless should_audit?
    f[0...((@_audit_meta.size + SNAPSHOT_COLUMN_COUNT) * -1)]
  end

  def nfields
    n = super
    return n unless should_audit?
    n - @_audit_meta.size - SNAPSHOT_COLUMN_COUNT
  end

  private

  def should_audit?
    @_audit_meta && !@_audit_meta.empty?
  end

  def process_rows_and_strip!(rows, type)
    audit_buffer = Hash.new { |h, k| h[k] = Set.new }
    meta_count = @_audit_meta.size
    snapshot_bounds = Set.new

    rows.each do |row|
      if type == :array
        xmax = row.pop
        xmin = row.pop
      elsif type == :hash
        xmax = row.delete('__nomai_xmax')
        xmin = row.delete('__nomai_xmin')
      end
      snapshot_bounds << [Integer(xmin), Integer(xmax)]

      (meta_count - 1).downto(0) do |k|
        val = nil
        meta_item = @_audit_meta[k]

        if type == :array
          val = row.pop
        elsif type == :hash
          val = row.delete(meta_item['alias_used'])
        end

        if val
          table_name = meta_item['table'] || 'unknown'
          audit_buffer[table_name] << val
        end
      end
    end

    raise "inconsistent snapshot bounds: #{snapshot_bounds.to_a}" unless snapshot_bounds.size == 1
    xmin, xmax = snapshot_bounds.first
    flush_audit_log(audit_buffer, xmin, xmax) unless audit_buffer.empty?
  rescue => e
    PgPatchLogger.error("Audit Processing Error: #{e.message}")
  end

  def flush_audit_log(audit_buffer, xmin, xmax)
    return unless READ_LOG_ENABLED

    log_time = Time.now.iso8601(6)
    audit_buffer.each do |table, pks|
      log_entry = {
        :time => log_time,
        :event => 'select',
        :rid => @_audit_req_id,
        :tbn => table,
        :pks => pks.to_a,
        :seq => @_audit_seq,
        :xmin => xmin,
        :xmax => xmax
      }
      PgPatchLogger.write(log_entry.to_json)
    end
  end
end

# ==========================================
# 4. Connection hook (driver-level patch)
# ==========================================
module PgConnectionPatcher
  attr_accessor :_in_recursion
  
  # Stores the mapping from prepared statement names to metadata
  # Format: { "a1" => [meta_array] }
  def _stmt_registry
    @_stmt_registry ||= {}
  end

  def _set_request_id_logic(req_id)
    return if !req_id || @_in_recursion || @_last_request_id == req_id

    begin
      @_in_recursion = true
      self.async_exec("SET request_id = '#{escape_string(req_id)}'")
      @_last_request_id = req_id
    rescue => e
      # ignore
    ensure
      @_in_recursion = false
    end
  end

  def _audit_result_logic(result, meta, req_id)
    if result.is_a?(PG::Result) && meta && !meta.empty?
      result.extend(PgResultAuditor)
      result._audit_meta = meta
      result._audit_req_id = req_id
      result._audit_seq = Thread.current[:gitlab_hook_read_sequence] || 0
      Thread.current[:gitlab_hook_read_sequence] = result._audit_seq + 1
    end
  end

  # --- Direct execution methods ---

  def exec(*args, &block)
    sql = args.first.to_s
    req_id = Thread.current[:gitlab_hook_request_id]

    _set_request_id_logic(req_id)
    
    new_sql = nil
    meta = []

    # Change: rewrite only when req_id exists and execution is not recursive
    if req_id && !@_in_recursion && RustSqlRewriter.loaded?
      # Print the original SQL and request ID
      PgPatchLogger.error("[EXEC] #{req_id} #{sql}")
      
      new_sql, meta = RustSqlRewriter.rewrite(sql)
      args[0] = new_sql if new_sql
    end
    
    result = super(*args, &block)
    
    _audit_result_logic(result, meta, req_id)
    result
  end

  def async_exec(*args, &block)
    sql = args.first.to_s
    req_id = Thread.current[:gitlab_hook_request_id]

    _set_request_id_logic(req_id)

    new_sql = nil
    meta = []

    # Change: rewrite only when req_id exists and execution is not recursive
    if req_id && !@_in_recursion && RustSqlRewriter.loaded?
      PgPatchLogger.error("[ASYNC_EXEC] #{req_id} #{sql}")
      
      new_sql, meta = RustSqlRewriter.rewrite(sql)
      args[0] = new_sql if new_sql
    end

    result = super(*args, &block)

    _audit_result_logic(result, meta, req_id)
    result
  end

  def exec_params(*args, &block)
    sql = args.first.to_s
    req_id = Thread.current[:gitlab_hook_request_id]

    _set_request_id_logic(req_id)

    new_sql = nil
    meta = []

    # Change: rewrite only when req_id exists and execution is not recursive
    if req_id && !@_in_recursion && RustSqlRewriter.loaded?
      PgPatchLogger.error("[EXEC_PARAMS] #{req_id} #{sql}")
      
      new_sql, meta = RustSqlRewriter.rewrite(sql)
      args[0] = new_sql if new_sql
    end

    result = super(*args, &block)

    _audit_result_logic(result, meta, req_id)
    result
  end

  # --- Prepared statement methods ---

  def prepare(*args, &block)
    stmt_name = args[0].to_s
    sql = args[1].to_s
    
    # Get the current context request_id only to decide whether rewrite is needed
    req_id = Thread.current[:gitlab_hook_request_id]

    new_sql = nil
    meta = []
    
    # Change: rewrite only when req_id exists and execution is not recursive
    if req_id && !@_in_recursion && RustSqlRewriter.loaded?
      PgPatchLogger.error("[PREPARE] #{req_id} #{sql}")
      
      new_sql, meta = RustSqlRewriter.rewrite(sql)
    end

    if new_sql
      args[1] = new_sql
      # Register metadata for exec_prepared
      _stmt_registry[stmt_name] = meta
    else
      # If there was no rewrite
      _stmt_registry.delete(stmt_name)
    end

    super(*args, &block)
  end

  def exec_prepared(*args, &block)
    stmt_name = args[0].to_s
    req_id = Thread.current[:gitlab_hook_request_id]

    # Set Request ID
    _set_request_id_logic(req_id)

    # Execution (possibly rewritten or native)
    result = super(*args, &block)

    # Try to get metadata
    # If prepare skipped rewriting because req_id was absent, meta is nil here and auditing will not run
    meta = _stmt_registry[stmt_name]

    _audit_result_logic(result, meta, req_id)
    result
  end
end

# ==========================================
# 5. Install patches
# ==========================================

if HpExperimentFlags.enabled?('HP_PG_HOOK')
Rails.application.config.to_prepare do
  # Ensure the PG library is loaded
  require 'pg' unless defined?(PG::Connection)

  unless PG::Connection.ancestors.include?(PgConnectionPatcher)
    PG::Connection.prepend(PgConnectionPatcher)
    PgPatchLogger.error("PgConnectionPatcher prepended to PG::Connection.")
  end
  
  # Try to clear connections at startup to force reconnects; works with Spring/Puma preload
  if defined?(ActiveRecord::Base)
    ActiveRecord::Base.clear_all_connections!
    PgPatchLogger.error("ActiveRecord connections cleared.")
  end
end
end
