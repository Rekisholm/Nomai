# /opt/gitlab/embedded/service/gitlab-rails/config/initializers/hook.rb
require 'securerandom'
require 'json'
require 'time'
require 'digest'

module HpExperimentFlags
  FALSE_VALUES = ['0', 'false', 'no', 'off'].freeze

  def self.enabled?(name, default_value = '1')
    !FALSE_VALUES.include?(ENV.fetch(name, default_value).to_s.strip.downcase)
  end
end

module HookLogger
  LOG_FILE = '/tmp/logs/request.log'.freeze
  def self.write(message)
    # File.open(LOG_FILE, 'a') { |f| f.puts(message.strip) } rescue nil
  end
end

module RequestContext
  def self.request_id; Thread.current[:gitlab_hook_request_id]; end
  def self.request_id=(id); Thread.current[:gitlab_hook_request_id] = id; end
  # The in_pg_patch flags are only used by the patcher, so they are not needed here.
end

module ContextLogger
  CONTEXT_FILE = '/tmp/logs/request_context.log'.freeze
  @seen_rids = {}
  @seen_lock = Mutex.new

  def self.emit(rid, env)
    return unless rid
    # Deduplicate by RID: one GitLab request may pass through middleware multiple times
    @seen_lock.synchronize do
      return if @seen_rids[rid]
      @seen_rids[rid] = true
    end

    record = build_record(rid, env)
    File.open(CONTEXT_FILE, 'a') { |f| f.puts(record.to_json) } rescue nil
  rescue => e
    HookLogger.write("[CTX] error: #{e.message}")
  end

  def self.build_record(rid, env)
    method = env['REQUEST_METHOD'].to_s
    user_id, identity_source = extract_user_id(rid, env)
    api_id, route = extract_api_id(env)
    {
      'time' => Time.now.iso8601(6),
      'rid' => rid,
      'method' => method,
      'route' => route,
      'api_id' => api_id,
      'user_id' => user_id,
      'identity_source' => identity_source
    }
  end

  def self.extract_user_id(rid, env)
    # 1. Warden/Devise authenticated user (session authentication)
    begin
      warden = env['warden']
      if warden
        user = warden.user
        if user
          uid = user.id
          return ["user:#{uid}", 'warden'] if uid
        end
      end
    rescue => e
      HookLogger.write("[CTX] warden error: #{e.message}")
    end

    # 2. current_user on the Grape API endpoint (API token authentication)
    begin
      endpoint = env['api.endpoint']
      if endpoint && endpoint.respond_to?(:current_user)
        user = endpoint.current_user
        if user && user.id
          return ["user:#{user.id}", 'grape_endpoint']
        end
      end
    rescue => e
      # ignore
    end

    # 3. current_user on action_controller.instance
    begin
      ctrl = env['action_controller.instance']
      if ctrl && ctrl.respond_to?(:current_user)
        user = ctrl.current_user
        if user && user.id
          return ["user:#{user.id}", 'controller_current_user']
        end
      end
    rescue => e
      # ignore
    end

    # 4. Session cookie SHA-256 digest
    begin
      cookie = env['HTTP_COOKIE'].to_s
      unless cookie.empty?
        digest = Digest::SHA256.hexdigest(cookie)[0, 24]
        return ["session:#{digest}", 'session_cookie_sha256']
      end
    rescue => e
      # ignore
    end

    # 5. Fall back to request:<rid>
    ["request:#{rid}", 'request_id']
  end

  def self.extract_api_id(env)
    method = env['REQUEST_METHOD'].to_s
    # 1. Rails path_parameters -> controller/action
    begin
      params = env['action_dispatch.request.path_parameters']
      if params
        controller = params[:controller] || params['controller']
        action = params[:action] || params['action']
        if controller && action
          route = "/#{controller}/#{action}"
          return ["#{method} #{route}", route]
        end
      end
    rescue => e
      # ignore
    end

    # 2. Fall back to PATH_INFO
    path = env['PATH_INFO'].to_s
    route = path.empty? ? '/' : path
    ["#{method} #{route}", route]
  end
end

HookLogger.write("[REQ_ID] request_id_injector.rb loaded.")
class RequestIdInjector
  def initialize(app)
    @app = app
  end

  def call(env)

    real_pid = Process.pid
    tid = Thread.current.object_id
    rand_str = SecureRandom.hex(4)

    # Composite ID: process, thread, random suffix
    request_id = "#{real_pid}-#{tid}-#{rand_str}"

    # Set context
    RequestContext.request_id = request_id
    Thread.current[:gitlab_hook_read_sequence] = 0

    path = env['PATH_INFO']
    HookLogger.write("[REQ_ID] gitlab web_request_start #{request_id} #{path}")

    status, headers, body = @app.call(env)

    begin
      headers.delete('X-Request-ID')
      headers.delete('X-Request-Id')
      headers['X-Request-ID'] = request_id
    rescue
      # ignore
    end

    # Collect request context (Priority-v2): authentication and routing are complete after @app.call
    ContextLogger.emit(request_id, env)

    HookLogger.write("[REQ_ID] gitlab web_request_end #{request_id}")

    [status, headers, body]
  ensure

    RequestContext.request_id = nil
    Thread.current[:gitlab_hook_read_sequence] = nil
  end
end

if HpExperimentFlags.enabled?('HP_REQUEST_ID_HOOK') && defined?(Rails)
  Rails.application.config.middleware.insert_before 0, RequestIdInjector
  HookLogger.write("[REQ_ID] [OK] RequestIdInjector middleware injected.")
else
  HookLogger.write("[REQ_ID] WARNING: Rails not defined, middleware not injected.")
end
