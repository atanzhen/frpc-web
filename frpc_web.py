#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FRP Client Manager - 高兼容性增强版
支持：Python 3.6 ~ 3.12+ | 多架构 (amd64, arm64, armv7) | 自动版本检测
"""
import os
import sys
import json
import shutil
import subprocess
import hashlib
import time
import socket
import platform
import glob
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

# ================== 全局配置与自适应检测 ==================

def get_arch_suffix():
    """自动检测系统架构并返回 frp 文件名对应的后缀"""
    machine = platform.machine().lower()
    if machine in ['x86_64', 'amd64']:
        return 'linux_amd64'
    elif machine in ['aarch64', 'arm64']:
        return 'linux_arm64'
    elif machine.startswith('arm'):
        return 'linux_arm'
    elif machine in ['i386', 'i686']:
        return 'linux_386'
    return 'linux_amd64' # 默认回退

def find_frpc_dir():
    """自动查找 frp 目录，优先级：环境变量 > 同级目录 > 常见路径"""
    # 1. 优先使用环境变量
    env_dir = os.environ.get('FRPC_DIR')
    if env_dir and os.path.isdir(env_dir):
        return env_dir

    # 2. 查找同级目录下的 frp_* 文件夹
    base_dir = os.path.dirname(os.path.abspath(__file__))
    arch_suffix = get_arch_suffix()
    
    # 匹配模式：frp_版本号_架构
    pattern = os.path.join(base_dir, f"frp_*_{arch_suffix}")
    matches = glob.glob(pattern)
    if matches:
        return matches[0]
        
    # 3. 尝试查找 /home/ubuntu 或 /home/frp 下的目录
    common_paths = [
        "/home/ubuntu", "/home/frp", "/opt", "/usr/local"
    ]
    for path in common_paths:
        if os.path.exists(path):
            for d in os.listdir(path):
                if d.startswith("frp_") and arch_suffix in d:
                    return os.path.join(path, d)
                    
    # 4. 最后回退到硬编码路径（保持原有逻辑）
    return f"/home/frp/frp_0.52.3_{arch_suffix}"

FRPC_DIR    = find_frpc_dir()
FRPC_BIN    = os.path.join(FRPC_DIR, "frpc")
FRPC_CONF   = os.path.join(FRPC_DIR, "frpc.toml")
FRPC_BAK    = FRPC_CONF + ".bak"
SERVICE_NAME = os.environ.get('SERVICE_NAME', "frpc")  # 支持环境变量定义服务名
WEB_PORT    = int(os.environ.get('WEB_PORT', 7600))

# 【重要】设置后台登录密码
ADMIN_PASSWORD = os.environ.get('FRP_ADMIN_PASSWORD', "8787387Ww")

# 简单的 Session 存储 (内存中，重启失效)
ACTIVE_SESSIONS = {}
SESSION_TIMEOUT = 3600  # 1小时过期

# 确保目录存在 (处理权限问题)
try:
    os.makedirs(FRPC_DIR, exist_ok=True)
except OSError as e:
    print(f"警告: 无法创建目录 {FRPC_DIR}，请检查权限。错误: {e}")

# 获取本机局域网IP用于自动填充
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

LOCAL_IP = get_local_ip()

# ================== TOML 简易解析与生成 ==================

def parse_toml_value(v):
    """解析 TOML 值"""
    v = v.strip()
    if not v: return ""
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v.startswith('[') and v.endswith(']'):
        inner = v[1:-1].strip()
        if not inner: return []
        return [parse_toml_value(x) for x in inner.split(',')]
    if v.lower() == 'true': return True
    if v.lower() == 'false': return False
    try: return int(v)
    except ValueError: pass
    try: return float(v)
    except ValueError: pass
    return v

def parse_toml_file(filepath):
    """解析 frpc.toml 为字典结构"""
    result = {"server": {}, "proxies": []}
    if not os.path.exists(filepath):
        return result
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        return result

    current_proxy = None
    in_proxy_block = False
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
            
        if line == '[[proxies]]':
            if current_proxy is not None:
                result["proxies"].append(current_proxy)
            current_proxy = {}
            in_proxy_block = True
            continue
        
        if line.startswith('[') and line != '[[proxies]]':
            if current_proxy is not None:
                result["proxies"].append(current_proxy)
                current_proxy = None
            in_proxy_block = False
            continue
            
        if '=' not in line:
            continue
            
        key, val = line.split('=', 1)
        key = key.strip()
        val = parse_toml_value(val)
        
        if in_proxy_block and current_proxy is not None:
            current_proxy[key] = val
        else:
            if key == 'serverAddr': result["server"]["addr"] = val
            elif key == 'serverPort': result["server"]["port"] = val
            elif key == 'auth.token': result["server"]["token"] = val
            elif key == 'user': result["server"]["user"] = val
            
    if current_proxy is not None:
        result["proxies"].append(current_proxy)
        
    return result

def generate_toml(data):
    """根据字典生成 TOML 内容"""
    lines = []
    s = data.get("server", {})
    
    if s.get("addr"):
        lines.append(f'serverAddr = "{s["addr"]}"')
    if s.get("port"):
        lines.append(f'serverPort = {s["port"]}')
    if s.get("token"):
        lines.append(f'auth.token = "{s["token"]}"')
    if s.get("user"):
        lines.append(f'user = "{s["user"]}"')
        
    lines.append("")
    
    for p in data.get("proxies", []):
        lines.append("[[proxies]]")
        lines.append(f'name = "{p["name"]}"')
        lines.append(f'type = "{p["type"]}"')
        
        t = p["type"]
        if t in ("tcp", "udp", "http", "https", "stcp", "xtcp", "tcpmux"):
            lines.append(f'localIP = "{p.get("localIP", "127.0.0.1")}"')
            lines.append(f'localPort = {p["localPort"]}')
            
        if t in ("tcp", "udp"):
            lines.append(f'remotePort = {p["remotePort"]}')
            
        if t in ("http", "https", "tcpmux"):
            if p.get("customDomains"):
                domains = p["customDomains"] if isinstance(p["customDomains"], list) else [p["customDomains"]]
                lines.append(f'customDomains = {json.dumps(domains)}')
            if p.get("subdomain"):
                lines.append(f'subdomain = "{p["subdomain"]}"')
                
        if t in ("stcp", "xtcp"):
            lines.append(f'secretKey = "{p.get("secretKey", "")}"')
            if p.get("allowUsers"):
                 users = p["allowUsers"] if isinstance(p["allowUsers"], list) else [p["allowUsers"]]
                 lines.append(f'allowUsers = {json.dumps(users)}')
                 
        if p.get("bandwidthLimit"):
            lines.append(f'bandwidthLimit = "{p["bandwidthLimit"]}"')
        if p.get("useEncryption"):
            lines.append("useEncryption = true")
        if p.get("useCompression"):
            lines.append("useCompression = true")
            
        lines.append("") 
        
    return "\n".join(lines)

# ================== 系统操作 ==================

def run_cmd(cmd, timeout=10):
    """执行 shell 命令，支持自定义超时时间"""
    try:
        # Python 3.6 兼容：capture_output 是在 3.7 加入的，这里用 stdout/stderr=PIPE 替代
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, f"命令执行超时 ({timeout}秒)"
    except Exception as e:
        return False, str(e)

def verify_config():
    """校验配置文件语法"""
    if not os.path.exists(FRPC_BIN):
        return False, f"找不到 frpc 二进制文件: {FRPC_BIN}"
    cmd = f"{FRPC_BIN} verify -c {FRPC_CONF}"
    success, output = run_cmd(cmd)
    return success, output.strip()

def reload_service():
    """重载或重启服务"""
    success, msg = run_cmd(f"systemctl reload {SERVICE_NAME}")
    if success:
        return True, "配置已重载 (Reload)"
    
    # restart 给足超时时间，避免 ExecStartPre=sleep 导致超时
    success, msg = run_cmd(f"systemctl restart {SERVICE_NAME}", timeout=30)
    if success:
        return True, "配置已应用 (Restart)"
        
    return False, f"服务操作失败: {msg}"

def save_and_apply(data):
    """保存配置并应用"""
    if os.path.exists(FRPC_CONF):
        shutil.copy2(FRPC_CONF, FRPC_BAK)
        
    content = generate_toml(data)
    with open(FRPC_CONF, 'w', encoding='utf-8') as f:
        f.write(content)
        
    valid, msg = verify_config()
    if not valid:
        if os.path.exists(FRPC_BAK):
            shutil.copy2(FRPC_BAK, FRPC_CONF)
        return False, f"配置语法错误，已回滚: {msg}"
        
    success, msg = reload_service()
    return success, msg

# ================== HTTP 处理器 ==================

class FrpHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _get_session_id(self):
        cookie = self.headers.get('Cookie', '')
        if 'session_id=' in cookie:
            return cookie.split('session_id=')[1].split(';')[0]
        return None

    def _check_auth(self):
        session_id = self._get_session_id()
        if session_id and session_id in ACTIVE_SESSIONS:
            last_time = ACTIVE_SESSIONS[session_id]
            if time.time() - last_time < SESSION_TIMEOUT:
                ACTIVE_SESSIONS[session_id] = time.time()
                return True
        return False

    def _send_json(self, code, data):
        response = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        try:
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, code, html):
        response = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        try:
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 0:
            body = self.rfile.read(length)
            return json.loads(body.decode('utf-8'))
        return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/login':
            return self._send_html(200, LOGIN_HTML)

        if path == '/api/check_auth':
            if self._check_auth():
                return self._send_json(200, {"authorized": True})
            else:
                return self._send_json(401, {"authorized": False})

        if not self._check_auth():
            if path == '/' or path == '/index.html':
                return self._send_html(200, LOGIN_HTML)
            return self._send_json(401, {"error": "Unauthorized"})

        if path == '/' or path == '/index.html':
            return self._send_html(200, DASHBOARD_HTML)

        elif path == '/api/status':
            success, output = run_cmd(f"systemctl is-active {SERVICE_NAME}")
            is_active = output.strip() == "active"
            config_data = parse_toml_file(FRPC_CONF)
            return self._send_json(200, {
                "active": is_active,
                "config_exists": os.path.exists(FRPC_CONF),
                "server_info": config_data.get("server", {}),
                "proxy_count": len(config_data.get("proxies", [])),
                "local_ip": LOCAL_IP
            })

        elif path == '/api/config':
            data = parse_toml_file(FRPC_CONF)
            return self._send_json(200, data)

        elif path == '/api/logs':
            success, output = run_cmd(f"journalctl -u {SERVICE_NAME} -n 50 --no-pager")
            return self._send_json(200, {"logs": output})

        # 【新增】轻量级状态查询接口，供前端轮询
        elif path == '/api/service/status':
            success, output = run_cmd(f"systemctl is-active {SERVICE_NAME}", timeout=5)
            is_active = output.strip() == "active"
            return self._send_json(200, {"active": is_active})

        else:
            self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == '/api/login':
            password = body.get('password', '')
            if password == ADMIN_PASSWORD:
                session_id = hashlib.md5(str(time.time()).encode()).hexdigest()
                ACTIVE_SESSIONS[session_id] = time.time()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Set-Cookie', f'session_id={session_id}; Path=/; HttpOnly')
                self.end_headers()
                try:
                    self.wfile.write(json.dumps({"success": True}).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self._send_json(401, {"success": False, "message": "密码错误"})
            return

        if not self._check_auth():
            return self._send_json(401, {"error": "Unauthorized"})

        if path == '/api/save':
            success, msg = save_and_apply(body)
            if success:
                return self._send_json(200, {"success": True, "message": f"✅ 配置保存成功，{msg}"})
            else:
                return self._send_json(200, {"success": False, "message": f"❌ {msg}"})

        elif path == '/api/service/restart':
            # 给足超时时间，等待 ExecStartPre=sleep 完成
            success, msg = run_cmd(f"systemctl restart {SERVICE_NAME}", timeout=30)
            if success:
                return self._send_json(200, {"success": True, "message": "✅ FRP 服务重启成功"})
            else:
                return self._send_json(200, {"success": False, "message": f"❌ 重启失败: {msg}"})
            
        elif path == '/api/service/stop':
            success, msg = run_cmd(f"systemctl stop {SERVICE_NAME}", timeout=15)
            if success:
                return self._send_json(200, {"success": True, "message": "✅ FRP 服务已停止"})
            else:
                return self._send_json(200, {"success": False, "message": f"❌ 停止失败: {msg}"})

        # 【新增】POST 方式的状态查询兼容
        elif path == '/api/service/status':
            success, output = run_cmd(f"systemctl is-active {SERVICE_NAME}", timeout=5)
            is_active = output.strip() == "active"
            return self._send_json(200, {"active": is_active})

        else:
            self._send_json(404, {"error": "Not Found"})

# ================== 前端 HTML 模板 ==================

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FRP 管理 - 登录</title>
    <style>
        body { background: #1a1b26; color: #c0caf5; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-box { background: #24283b; padding: 40px; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); width: 300px; text-align: center; }
        h2 { color: #7aa2f7; margin-bottom: 20px; }
        input { width: 100%; padding: 10px; margin-bottom: 15px; background: #1f2335; border: 1px solid #414868; color: white; border-radius: 4px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background: #7aa2f7; color: #1a1b26; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; white-space: nowrap; }
        button:hover { opacity: 0.9; }
        .error { color: #f7768e; margin-top: 10px; font-size: 0.9em; display: none; }
    </style>
</head>
<body>
    <div class="login-box">
        <h2>FRP 管理后台</h2>
        <input type="password" id="password" placeholder="请输入管理员密码" onkeypress="if(event.keyCode==13) login()">
        <button onclick="login()">登 录</button>
        <div id="errorMsg" class="error">密码错误</div>
    </div>
    <script>
        async function login() {
            const pwd = document.getElementById('password').value;
            const btn = document.querySelector('button');
            const err = document.getElementById('errorMsg');
            
            btn.textContent = "验证中...";
            btn.disabled = true;
            
            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: pwd})
                });
                const data = await res.json();
                if (data.success) {
                    window.location.href = '/';
                } else {
                    err.style.display = 'block';
                    err.textContent = data.message || "登录失败";
                    btn.textContent = "登 录";
                    btn.disabled = false;
                }
            } catch (e) {
                err.style.display = 'block';
                err.textContent = "网络错误";
                btn.textContent = "登 录";
                btn.disabled = false;
            }
        }
    </script>
</body>
</html>"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FRP 客户端管理</title>
    <style>
        :root {
            --bg: #1a1b26; --card: #24283b; --text: #c0caf5; --accent: #7aa2f7;
            --success: #9ece6a; --danger: #f7768e; --border: #414868;
            --warning-bg: #3d3328; --warning-text: #e0af68;
        }
        body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; min-height: 100vh; display: flex; flex-direction: column; }
        .container { max-width: 1200px; margin: 0 auto; flex: 1; width: 100%; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
        .status-badge { padding: 5px 12px; border-radius: 12px; font-size: 0.9em; font-weight: bold; white-space: nowrap; }
        .status-active { background: rgba(158, 206, 106, 0.2); color: var(--success); }
        .status-inactive { background: rgba(247, 118, 142, 0.2); color: var(--danger); }
        
        .card { background: var(--card); border-radius: 8px; padding: 20px; margin-bottom: 20px; border: 1px solid var(--border); overflow-x: auto; }
        .card h2 { margin-top: 0; color: var(--accent); font-size: 1.2em; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
        
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }
        .form-group { margin-bottom: 10px; }
        label { display: block; margin-bottom: 5px; font-size: 0.9em; color: #a9b1d6; }
        
        /* 优化输入框样式 */
        input, select { 
            width: 100%; padding: 8px; background: #1f2335; border: 1px solid var(--border); 
            color: var(--text); border-radius: 4px; box-sizing: border-box; 
        }
        
        /* 移除 number 输入框的上下箭头 */
        input[type=number]::-webkit-inner-spin-button, 
        input[type=number]::-webkit-outer-spin-button { 
            -webkit-appearance: none; 
            margin: 0; 
        }
        input[type=number] { -moz-appearance: textfield; }

        input:focus { outline: none; border-color: var(--accent); }
        
        /* 按钮全局样式：禁止换行 */
        .btn { 
            padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; 
            transition: opacity 0.2s; white-space: nowrap; 
        }
        .btn:hover { opacity: 0.8; }
        .btn-primary { background: var(--accent); color: #1a1b26; }
        .btn-danger { background: var(--danger); color: #1a1b26; }
        .btn-success { background: var(--success); color: #1a1b26; }
        .btn-sm { padding: 4px 8px; font-size: 0.8em; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        
        table { width: 100%; border-collapse: collapse; margin-top: 10px; min-width: 600px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
        th { color: var(--accent); font-size: 0.9em; white-space: nowrap; }
        tr:hover { background: rgba(255,255,255,0.02); }
        
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 100; justify-content: center; align-items: center; }
        .modal-content { background: var(--card); padding: 25px; border-radius: 8px; width: 90%; max-width: 500px; max-height: 90vh; overflow-y: auto; position: relative; }
        
        .close-btn-circle {
            position: absolute; top: 15px; right: 15px; width: 24px; height: 24px;
            background-color: #ff4d4f; border-radius: 50%; display: flex; align-items: center;
            justify-content: center; cursor: pointer; color: white; font-size: 12px;
            border: none; outline: none; transition: transform 0.2s;
        }
        .close-btn-circle:hover { transform: scale(1.1); background-color: #ff7875; }

        .modal-header { margin-bottom: 15px; padding-right: 30px; }
        
        .toast-container { position: fixed; top: 20px; right: 20px; z-index: 9999; display: flex; flex-direction: column; gap: 10px; pointer-events: none; }
        .toast { padding: 12px 20px; border-radius: 4px; color: white; box-shadow: 0 4px 12px rgba(0,0,0,0.3); font-size: 14px; opacity: 0; transform: translateX(100%); transition: all 0.3s ease; display: flex; align-items: center; min-width: 200px; pointer-events: auto; white-space: nowrap; }
        .toast.show { opacity: 1; transform: translateX(0); }
        .toast-success { background: var(--success); color: #1a1b26; }
        .toast-error { background: var(--danger); }
        .toast-info { background: var(--accent); color: #1a1b26; }
        
        .logs-box { background: #13141f; padding: 10px; border-radius: 4px; font-family: monospace; font-size: 0.85em; height: 200px; overflow-y: auto; white-space: pre-wrap; color: #a9b1d6; }

        .pagination-container { display: flex; justify-content: flex-end; align-items: center; margin-top: 15px; gap: 10px; font-size: 0.9em; flex-wrap: wrap; }
        .page-info { color: #a9b1d6; white-space: nowrap; }
        .page-btn { background: #1f2335; border: 1px solid var(--border); color: var(--text); padding: 4px 10px; border-radius: 4px; cursor: pointer; white-space: nowrap; }
        .page-btn.active { background: var(--accent); color: #1a1b26; border-color: var(--accent); }
        .page-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        
        .copy-link { color: var(--accent); cursor: pointer; display: inline-flex; align-items: center; gap: 5px; padding: 2px 6px; border-radius: 4px; background: rgba(122, 162, 247, 0.1); white-space: nowrap; }
        .copy-link:hover { background: rgba(122, 162, 247, 0.2); }
        .copy-icon { font-size: 0.8em; opacity: 0.7; }

        .port-mapping { font-family: monospace; background: rgba(255, 255, 255, 0.05); padding: 2px 6px; border-radius: 4px; color: #bb9af7; font-weight: bold; white-space: nowrap; }

        .confirm-modal .modal-content { max-width: 400px; text-align: center; }
        .confirm-actions { margin-top: 20px; display: flex; justify-content: center; gap: 15px; }

        /* 操作日志样式 */
        .log-list {
            max-height: 200px;
            overflow-y: auto;
            padding: 0;
            margin: 0;
            list-style: none;
        }
        .log-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid #2f354b;
            font-size: 0.9em;
        }
        .log-item:last-child { border-bottom: none; }
        
        .log-desc {
            flex: 1;
            margin-right: 15px;
            color: #c0caf5;
            word-break: break-all;
            line-height: 1.4;
        }
        .log-desc strong { color: #7aa2f7; font-weight: 600; }
        .log-detail { color: #a9b1d6; font-size: 0.95em; margin-left: 5px; }
        
        .log-time {
            flex-shrink: 0;
            color: #565f89;
            font-family: "SFMono-Regular", Consolas, monospace;
            font-size: 0.85em;
            background: rgba(86, 95, 137, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
            white-space: nowrap;
        }

        .reload-notice {
            background: var(--warning-bg);
            border: 1px solid rgba(224, 175, 104, 0.3);
            color: var(--warning-text);
            padding: 12px 16px;
            border-radius: 6px;
            margin-top: 15px;
            font-size: 0.9em;
            display: flex; align-items: flex-start; gap: 10px; line-height: 1.5;
        }
        .notice-icon { font-size: 1.2em; flex-shrink: 0; }

        .footer {
            text-align: center; margin-top: 30px; padding: 20px 0; border-top: 1px solid var(--border);
            font-size: 0.85em; color: #565f89;
        }
        .footer a { color: var(--accent); text-decoration: none; margin-left: 5px; }
        .footer a:hover { text-decoration: underline; }

        /* ================= 持久化进度遮罩层 ================= */
        .progress-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.65);
            z-index: 10000;
            justify-content: center;
            align-items: center;
            backdrop-filter: blur(3px);
        }
        .progress-overlay.active {
            display: flex;
        }
        .progress-box {
            background: #24283b;
            border: 1px solid #414868;
            border-radius: 12px;
            padding: 35px 45px;
            text-align: center;
            box-shadow: 0 8px 40px rgba(0,0,0,0.5);
            min-width: 320px;
            max-width: 90vw;
        }
        .progress-spinner {
            width: 44px; height: 44px;
            border: 4px solid #414868;
            border-top: 4px solid #7aa2f7;
            border-radius: 50%;
            animation: spin 0.9s linear infinite;
            margin: 0 auto 20px auto;
        }
        .progress-spinner.success {
            border: 4px solid #9ece6a;
            border-top: 4px solid #9ece6a;
            animation: none;
        }
        .progress-spinner.error {
            border: 4px solid #f7768e;
            border-top: 4px solid #f7768e;
            animation: none;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .progress-title {
            font-size: 1.15em;
            font-weight: bold;
            color: #c0caf5;
            margin-bottom: 10px;
        }
        .progress-step {
            font-size: 0.95em;
            color: #a9b1d6;
            margin-bottom: 8px;
            min-height: 1.4em;
        }
        .progress-elapsed {
            font-size: 0.8em;
            color: #565f89;
            font-family: "SFMono-Regular", Consolas, monospace;
            margin-top: 8px;
        }
        .progress-bar-track {
            width: 100%;
            height: 4px;
            background: #1f2335;
            border-radius: 2px;
            margin-top: 15px;
            overflow: hidden;
        }
        .progress-bar-fill {
            height: 100%;
            background: #7aa2f7;
            border-radius: 2px;
            width: 0%;
            transition: width 0.4s ease;
        }
        .progress-bar-fill.success { background: #9ece6a; }
        .progress-bar-fill.error { background: #f7768e; }

        /* 进度条不确定动画 */
        .progress-bar-fill.indeterminate {
            width: 30% !important;
            animation: indeterminate 1.5s ease-in-out infinite;
        }
        @keyframes indeterminate {
            0% { margin-left: -30%; }
            100% { margin-left: 100%; }
        }

        .progress-close-btn {
            display: none;
            margin-top: 20px;
            padding: 8px 30px;
            background: #7aa2f7;
            color: #1a1b26;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-weight: bold;
            font-size: 0.95em;
        }
        .progress-close-btn:hover { opacity: 0.85; }
        .progress-close-btn.visible { display: inline-block; }

        /* ================= 手机端适配核心 CSS ================= */
        @media (max-width: 768px) {
            body { padding: 10px; }
            .container { width: 100%; }
            .card { padding: 15px; }
            
            /* 隐藏远程地址列 */
            .hide-on-mobile {
                display: none !important;
            }

            /* 将 Table 转换为卡片式布局 */
            table, thead, tbody, th, td, tr { 
                display: block; 
            }
            
            thead tr { 
                position: absolute;
                top: -9999px;
                left: -9999px;
            }
            
            tr { 
                margin-bottom: 15px; 
                border: 1px solid var(--border); 
                border-radius: 8px; 
                background: rgba(255,255,255,0.02);
                padding: 10px;
            }
            
            td { 
                border: none; 
                position: relative; 
                padding-left: 0; 
                padding-right: 0;
                padding-top: 5px;
                padding-bottom: 5px;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            
            /* 针对特定列的微调 */
            td:nth-of-type(1) { /* 名称 */
                font-size: 1.1em;
                font-weight: bold;
                color: var(--accent);
                border-bottom: 1px dashed var(--border);
                margin-bottom: 5px;
                justify-content: flex-start;
            }
            
            td:nth-of-type(2) { /* 类型 */
                font-size: 0.9em;
                color: #a9b1d6;
            }
            
            td:nth-of-type(3) { /* 本地地址 */
                font-family: monospace;
            }

            td:nth-of-type(4) { /* 端口映射 */
                font-weight: bold;
            }
            
            /* 操作按钮列：显示在对应配置下面 */
            td:last-child {
                margin-top: 10px;
                justify-content: flex-end;
                border-top: 1px solid var(--border);
                padding-top: 10px;
            }
            
            /* 按钮在手机上稍微大一点方便点击 */
            .btn-sm {
                padding: 6px 12px;
                font-size: 0.9em;
            }

            /* 手机端进度框适配 */
            .progress-box {
                padding: 25px 20px;
                min-width: 280px;
            }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1 style="margin:0; font-size:1.5em;">FRP 客户端管理</h1>
        <div>
            <span id="serviceStatus" class="status-badge status-inactive">检测中...</span>
            <button class="btn btn-primary btn-sm" onclick="loadData()" style="margin-left: 10px;">刷新</button>
        </div>
    </div>

    <div class="card">
        <h2>服务端配置</h2>
        <div class="form-grid">
            <div class="form-group">
                <label>服务器地址 (serverAddr)</label>
                <input type="text" id="cfg_addr" placeholder="例如: 1.2.3.4">
            </div>
            <div class="form-group">
                <label>服务器端口 (serverPort)</label>
                <input type="text" inputmode="numeric" id="cfg_port" placeholder="例如: 7000" oninput="this.value=this.value.replace(/[^0-9]/g,'')">
            </div>
            <div class="form-group">
                <label>认证 Token 配置 (auth.token)</label>
                <input type="text" id="cfg_token" placeholder="留空则无认证">
            </div>
        </div>
    </div>

    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
            <h2 style="margin:0; border:none; padding:0;">端口映射</h2>
            <button class="btn btn-success btn-sm" onclick="openProxyModal()">+ 新增映射</button>
        </div>
        <table id="proxyTable">
            <thead>
                <tr>
                    <th>名称</th>
                    <th>类型</th>
                    <th>本地地址</th>
                    <th>端口映射</th>
                    <!-- 添加类名以便手机端隐藏 -->
                    <th class="hide-on-mobile">远程地址/域名 (点击复制)</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody></tbody>
        </table>
        
        <div id="paginationControls" class="pagination-container" style="display:none;">
            <span class="page-info" id="pageInfoText"></span>
            <div id="pageButtons"></div>
        </div>
    </div>

    <div class="card">
        <h2>服务控制 & 日志</h2>
        <div style="margin-bottom: 15px; display:flex; gap:10px; flex-wrap:wrap;">
            <button class="btn btn-success" id="btnRestart" onclick="confirmAction('restart')">重启服务</button>
            <button class="btn btn-danger" id="btnStop" onclick="confirmAction('stop')">停止服务</button>
            <button class="btn btn-primary" id="btnSave" onclick="saveAllConfig()">💾 保存配置 (自动重载)</button>
        </div>
        
        <div class="reload-notice">
            <span class="notice-icon">⚠️</span>
            <span><strong>重要提示：</strong>修改配置需要点击"保存配置"后才会生效，保存配置后需要等待配置完成，系统会自动重载服务以使新配置生效。如果自动重载失败，请手动点击"重启服务"。</span>
        </div>

        <label style="display:block; margin-top:20px; margin-bottom:5px;">系统日志:</label>
        <div class="logs-box" id="logBox">加载中...</div>
    </div>

    <div class="card">
        <h2>操作记录(浏览器本地记录)</h2>
        <div style="text-align:right; margin-bottom:10px;">
             <button class="btn btn-danger btn-sm" onclick="clearLogs()">清空记录</button>
        </div>
        <ul class="log-list" id="operationLogs">
            <li class="log-item" style="justify-content:center; color:#565f89;">暂无操作记录</li>
        </ul>
    </div>
</div>

<!-- 新增/编辑代理弹窗 -->
<div id="proxyModal" class="modal">
    <div class="modal-content">
        <button class="close-btn-circle" onclick="closeModal()">✕</button>
        <div class="modal-header">
            <h3 id="modalTitle">新增映射</h3>
        </div>
        <div class="form-group">
            <label>名称 (Name)</label>
            <input type="text" id="p_name">
        </div>
        <div class="form-group">
            <label>类型 (Type)</label>
            <select id="p_type" onchange="toggleFields()">
                <option value="tcp">TCP (端口转发)</option>
                <option value="udp">UDP</option>
                <option value="http">HTTP (Web)</option>
                <option value="https">HTTPS</option>
                <option value="stcp">STCP (秘密TCP)</option>
            </select>
        </div>
        <div class="form-grid">
            <div class="form-group">
                <label>本地 IP</label>
                <input type="text" id="p_localIp" value="127.0.0.1">
            </div>
            <div class="form-group">
                <label>本地端口</label>
                <input type="text" inputmode="numeric" id="p_localPort" oninput="this.value=this.value.replace(/[^0-9]/g,'')">
            </div>
        </div>
        <div class="form-group" id="remotePortGroup">
            <label>远程端口 (Remote Port)</label>
            <input type="text" inputmode="numeric" id="p_remotePort" oninput="this.value=this.value.replace(/[^0-9]/g,'')">
        </div>
        <div class="form-group" id="domainGroup" style="display:none;">
            <label>自定义域名 (Custom Domains)</label>
            <input type="text" id="p_domains" placeholder="example.com">
        </div>
        <div class="form-group" id="secretGroup" style="display:none;">
            <label>密钥 (Secret Key)</label>
            <input type="text" id="p_secretKey">
        </div>
        <div style="margin-top: 20px; text-align: right;">
            <button class="btn btn-primary" onclick="saveProxy()">保存</button>
        </div>
    </div>
</div>

<!-- 自定义 Confirm 弹窗 -->
<div id="confirmModal" class="modal confirm-modal">
    <div class="modal-content">
        <button class="close-btn-circle" onclick="closeConfirm()">✕</button>
        <div class="modal-header">
            <h3>操作确认</h3>
        </div>
        <div id="confirmMessage" style="margin: 20px 0; color: #a9b1d6;"></div>
        <div class="confirm-actions">
            <button class="btn" onclick="closeConfirm()" style="background:#414868; color:white;">取消</button>
            <button class="btn btn-danger" id="confirmBtn" onclick="handleConfirmOk()">确定</button>
        </div>
    </div>
</div>

<!-- ==================== 持久化进度遮罩层 ==================== -->
<div id="progressOverlay" class="progress-overlay">
    <div class="progress-box">
        <div class="progress-spinner" id="progressSpinner"></div>
        <div class="progress-title" id="progressTitle">正在处理</div>
        <div class="progress-step" id="progressStep">准备中...</div>
        <div class="progress-bar-track">
            <div class="progress-bar-fill indeterminate" id="progressBarFill"></div>
        </div>
        <div class="progress-elapsed" id="progressElapsed">已用时: 0 秒</div>
        <button class="progress-close-btn" id="progressCloseBtn" onclick="hideProgress()">确 定</button>
    </div>
</div>

<div class="toast-container" id="toastContainer"></div>

<div class="footer">
    <p>© 2023 FRP Client Manager. All rights reserved. | <a href="https://github.com/fatedier/frp" target="_blank" rel="noopener noreferrer">View Project on GitHub</a></p>
</div>

<script>
    let currentConfig = { server: {}, proxies: [] };
    let editingIndex = -1;
    let localIp = "";
    
    const PAGE_SIZE = 10;
    let currentPage = 1;

    // 操作日志存储 - 从 LocalStorage 加载以实现永久保存
    let operationLogs = [];

    // ================== 进度遮罩层控制 ==================
    let progressTimer = null;
    let progressStartTime = 0;

    function showProgress(title, stepText) {
        var overlay = document.getElementById('progressOverlay');
        var spinner = document.getElementById('progressSpinner');
        var titleEl = document.getElementById('progressTitle');
        var stepEl = document.getElementById('progressStep');
        var barFill = document.getElementById('progressBarFill');
        var closeBtn = document.getElementById('progressCloseBtn');

        spinner.className = 'progress-spinner';
        barFill.className = 'progress-bar-fill indeterminate';
        barFill.style.width = '';
        closeBtn.className = 'progress-close-btn';

        titleEl.textContent = title || '正在处理';
        stepEl.textContent = stepText || '准备中...';

        progressStartTime = Date.now();
        document.getElementById('progressElapsed').textContent = '已用时: 0 秒';

        if (progressTimer) clearInterval(progressTimer);
        progressTimer = setInterval(function() {
            var elapsed = Math.floor((Date.now() - progressStartTime) / 1000);
            document.getElementById('progressElapsed').textContent = '已用时: ' + elapsed + ' 秒';
        }, 1000);

        overlay.classList.add('active');

        // 禁用操作按钮防止重复点击
        setActionButtonsDisabled(true);
    }

    function updateProgress(stepText, percent) {
        var stepEl = document.getElementById('progressStep');
        var barFill = document.getElementById('progressBarFill');
        stepEl.textContent = stepText;
        if (typeof percent === 'number' && percent >= 0 && percent <= 100) {
            barFill.className = 'progress-bar-fill';
            barFill.style.width = percent + '%';
        }
    }

    function finishProgress(success, titleText, detailText) {
        var spinner = document.getElementById('progressSpinner');
        var titleEl = document.getElementById('progressTitle');
        var stepEl = document.getElementById('progressStep');
        var barFill = document.getElementById('progressBarFill');
        var closeBtn = document.getElementById('progressCloseBtn');

        if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }

        var elapsed = Math.floor((Date.now() - progressStartTime) / 1000);
        document.getElementById('progressElapsed').textContent = '总耗时: ' + elapsed + ' 秒';

        if (success) {
            spinner.className = 'progress-spinner success';
            barFill.className = 'progress-bar-fill success';
            barFill.style.width = '100%';
            titleEl.textContent = titleText || '✅ 操作成功';
            stepEl.textContent = detailText || '配置已生效';
        } else {
            spinner.className = 'progress-spinner error';
            barFill.className = 'progress-bar-fill error';
            barFill.style.width = '100%';
            titleEl.textContent = titleText || '❌ 操作失败';
            stepEl.textContent = detailText || '请检查配置后重试';
        }

        closeBtn.className = 'progress-close-btn visible';

        // 恢复按钮
        setActionButtonsDisabled(false);
    }

    function hideProgress() {
        var overlay = document.getElementById('progressOverlay');
        overlay.classList.remove('active');
        if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
        setActionButtonsDisabled(false);
    }

    function setActionButtonsDisabled(disabled) {
        var btns = ['btnRestart', 'btnStop', 'btnSave'];
        for (var i = 0; i < btns.length; i++) {
            var el = document.getElementById(btns[i]);
            if (el) el.disabled = disabled;
        }
    }

    // ================== 初始化 ==================

    window.onload = function() {
        loadLogsFromStorage();
        checkAuthAndLoad();
    };

    // 从 LocalStorage 加载日志
    function loadLogsFromStorage() {
        const savedLogs = localStorage.getItem('frp_operation_logs');
        if (savedLogs) {
            try {
                operationLogs = JSON.parse(savedLogs);
                renderOperationLogs();
            } catch (e) {
                console.error("Failed to parse logs", e);
            }
        }
    }

    // 保存日志到 LocalStorage
    function saveLogsToStorage() {
        localStorage.setItem('frp_operation_logs', JSON.stringify(operationLogs));
    }

    // 清空日志
    function clearLogs() {
        if(confirm("确定要清空所有操作记录吗？")) {
            operationLogs = [];
            localStorage.removeItem('frp_operation_logs');
            renderOperationLogs();
            showToast("记录已清空", "info");
        }
    }

    async function checkAuthAndLoad() {
        try {
            const res = await fetch('/api/check_auth');
            const data = await res.json();
            if (!data.authorized) {
                window.location.href = '/login';
                return;
            }
            loadData();
        } catch (e) {
            console.error("Auth check failed", e);
        }
    }

    // 添加操作日志
    function addLog(action, detail = "", type = 'info') {
        const now = new Date();
        const timeStr = now.getFullYear() + '-' + 
                        String(now.getMonth() + 1).padStart(2, '0') + '-' + 
                        String(now.getDate()).padStart(2, '0') + ' ' + 
                        String(now.getHours()).padStart(2, '0') + ':' + 
                        String(now.getMinutes()).padStart(2, '0') + ':' + 
                        String(now.getSeconds()).padStart(2, '0');
                        
        const logEntry = { time: timeStr, action: action, detail: detail, type: type };
        operationLogs.unshift(logEntry);
        if (operationLogs.length > 100) operationLogs.pop(); // 限制最多100条
        
        saveLogsToStorage(); // 永久保存
        renderOperationLogs();
    }

    function renderOperationLogs() {
        const container = document.getElementById('operationLogs');
        if (operationLogs.length === 0) {
            container.innerHTML = '<li class="log-item" style="justify-content:center; color:#565f89;">暂无操作记录</li>';
            return;
        }
        
        let html = '';
        operationLogs.forEach(log => {
            let typeClass = '';
            if (log.type === 'add') typeClass = 'color: #9ece6a;';
            if (log.type === 'del') typeClass = 'color: #f7768e;';
            if (log.type === 'edit') typeClass = 'color: #7aa2f7;';
            if (log.type === 'save') typeClass = 'color: #e0af68;';
            
            let descHtml = `<strong>${log.action}</strong>`;
            if (log.detail) {
                descHtml += `<span class="log-detail">${log.detail}</span>`;
            }
            
            html += `
                <li class="log-item">
                    <div class="log-desc" style="${typeClass}">${descHtml}</div>
                    <div class="log-time">${log.time}</div>
                </li>
            `;
        });
        container.innerHTML = html;
    }

    async function loadData() {
        try {
            const statusRes = await fetch('/api/status');
            const statusData = await statusRes.json();
            const badge = document.getElementById('serviceStatus');
            if (statusData.active) {
                badge.textContent = "● 运行中";
                badge.className = "status-badge status-active";
            } else {
                badge.textContent = "○ 已停止";
                badge.className = "status-badge status-inactive";
            }
            
            if(statusData.local_ip) localIp = statusData.local_ip;

            const configRes = await fetch('/api/config');
            currentConfig = await configRes.json();
            
            document.getElementById('cfg_addr').value = currentConfig.server.addr || '';
            document.getElementById('cfg_port').value = currentConfig.server.port || '';
            document.getElementById('cfg_token').value = currentConfig.server.token || '';

            currentPage = 1;
            renderProxies();
            
            const logRes = await fetch('/api/logs');
            const logData = await logRes.json();
            document.getElementById('logBox').textContent = logData.logs || "无日志";

        } catch (e) {
            showToast("加载数据失败: " + e.message, 'error');
        }
    }

    // 操作后轮询刷新服务状态，最多等待 maxWait 毫秒，期间更新进度文字
    async function pollServiceStatus(expectedActive, maxWait) {
        if (maxWait === undefined) maxWait = 35000;
        var interval = 2000;
        var startTime = Date.now();
        var attempt = 0;
        while (Date.now() - startTime < maxWait) {
            await new Promise(function(resolve) { setTimeout(resolve, interval); });
            attempt++;
            var elapsed = Math.floor((Date.now() - startTime) / 1000);
            updateProgress(
                '等待服务' + (expectedActive ? '启动' : '停止') + '中... (第' + attempt + '次检测, 已等待' + elapsed + '秒)',
                Math.min(90, 30 + attempt * 8)
            );
            try {
                var res = await fetch('/api/service/status');
                var data = await res.json();
                if (data.active === expectedActive) {
                    loadData();
                    return true;
                }
            } catch (e) { /* 忽略单次请求失败 */ }
        }
        loadData();
        return false;
    }

    function renderProxies() {
        const tbody = document.querySelector('#proxyTable tbody');
        tbody.innerHTML = '';
        
        const proxies = currentConfig.proxies || [];
        const total = proxies.length;
        const totalPages = Math.ceil(total / PAGE_SIZE);
        
        const startIndex = (currentPage - 1) * PAGE_SIZE;
        const endIndex = Math.min(startIndex + PAGE_SIZE, total);
        const pageData = proxies.slice(startIndex, endIndex);

        if (total === 0) {
             tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:#666; padding:20px;">暂无映射配置</td></tr>';
             document.getElementById('paginationControls').style.display = 'none';
             return;
        }

        const serverAddr = currentConfig.server.addr || localIp || "127.0.0.1";

        pageData.forEach((p, idx) => {
            const originalIndex = startIndex + idx;
            const localAddrDisplay = `${p.localIP || '127.0.0.1'}:${p.localPort}`;
            
            let remoteDisplay = '-';
            let copyText = '';
            let portMappingDisplay = '-';
            
            if (p.type === 'tcp' || p.type === 'udp') {
                const rPort = p.remotePort || '?';
                const lPort = p.localPort || '?';
                portMappingDisplay = `<span class="port-mapping">${lPort} &gt; ${rPort}</span>`;
                copyText = `${serverAddr}:${rPort}`;
                // 添加 hide-on-mobile 类
                remoteDisplay = `<span class="copy-link hide-on-mobile" onclick="copyToClipboard('${copyText}')">${copyText} <span class="copy-icon">📋</span></span>`;
            } else if (p.type === 'http' || p.type === 'https') {
                const domains = Array.isArray(p.customDomains) ? p.customDomains.join(',') : (p.customDomains || '-');
                const lPort = p.localPort || '?';
                portMappingDisplay = `<span class="port-mapping">${lPort} &gt; Web</span>`;
                copyText = domains;
                remoteDisplay = `<span class="copy-link hide-on-mobile" onclick="copyToClipboard('${copyText}')">${domains} <span class="copy-icon">📋</span></span>`;
            } else {
                const lPort = p.localPort || '?';
                portMappingDisplay = `<span class="port-mapping">${lPort} &gt; STCP</span>`;
                remoteDisplay = p.type.toUpperCase();
            }

            let row = `
                <tr>
                    <td>${p.name}</td>
                    <td><span style="color:var(--accent)">${p.type.toUpperCase()}</span></td>
                    <td>${localAddrDisplay}</td>
                    <td>${portMappingDisplay}</td>
                    <td class="hide-on-mobile">${remoteDisplay}</td>
                    <td>
                        <button class="btn btn-primary btn-sm" onclick="editProxy(${originalIndex})">编辑</button>
                        <button class="btn btn-danger btn-sm" onclick="confirmDelete(${originalIndex})">删除</button>
                    </td>
                </tr>
            `;
            tbody.innerHTML += row;
        });

        renderPagination(total, totalPages);
    }
    
    function renderPagination(total, totalPages) {
        const container = document.getElementById('paginationControls');
        const infoText = document.getElementById('pageInfoText');
        const btnContainer = document.getElementById('pageButtons');
        
        if (total <= PAGE_SIZE) {
            container.style.display = 'none';
            return;
        }
        
        container.style.display = 'flex';
        infoText.textContent = `共 ${total} 条, 第 ${currentPage}/${totalPages} 页`;
        
        btnContainer.innerHTML = '';
        
        const prevBtn = document.createElement('button');
        prevBtn.className = 'page-btn';
        prevBtn.innerText = '<';
        prevBtn.disabled = currentPage === 1;
        prevBtn.onclick = () => changePage(currentPage - 1);
        btnContainer.appendChild(prevBtn);
        
        let startPage = Math.max(1, currentPage - 1);
        let endPage = Math.min(totalPages, startPage + 2);
        if (endPage - startPage < 2) {
            startPage = Math.max(1, endPage - 2);
        }

        for (let i = startPage; i <= endPage; i++) {
            const btn = document.createElement('button');
            btn.className = `page-btn ${i === currentPage ? 'active' : ''}`;
            btn.innerText = i;
            btn.onclick = () => changePage(i);
            btnContainer.appendChild(btn);
        }
        
        const nextBtn = document.createElement('button');
        nextBtn.className = 'page-btn';
        nextBtn.innerText = '>';
        nextBtn.disabled = currentPage === totalPages;
        nextBtn.onclick = () => changePage(currentPage + 1);
        btnContainer.appendChild(nextBtn);
    }
    
    function changePage(page) {
        currentPage = page;
        renderProxies();
    }

    function copyToClipboard(text) {
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(() => {
                showToast(`已复制: ${text}`, 'success');
            }).catch(err => {
                fallbackCopy(text);
            });
        } else {
            fallbackCopy(text);
        }
    }

    function fallbackCopy(text) {
        const textArea = document.createElement("textarea");
        textArea.value = text;
        textArea.style.top = "0";
        textArea.style.left = "0";
        textArea.style.position = "fixed";
        textArea.style.opacity = "0";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        try {
            const successful = document.execCommand('copy');
            if (successful) {
                showToast(`已复制: ${text}`, 'success');
            } else {
                showToast('复制失败，请手动复制', 'error');
            }
        } catch (err) {
            showToast('浏览器不支持自动复制', 'error');
        }
        document.body.removeChild(textArea);
    }

    function openProxyModal(index = -1) {
        editingIndex = index;
        document.getElementById('proxyModal').style.display = 'flex';
        
        if (index >= 0) {
            const p = currentConfig.proxies[index];
            document.getElementById('modalTitle').textContent = "编辑映射";
            document.getElementById('p_name').value = p.name;
            document.getElementById('p_type').value = p.type;
            document.getElementById('p_localIp').value = p.localIP;
            document.getElementById('p_localPort').value = p.localPort;
            document.getElementById('p_remotePort').value = p.remotePort || '';
            
            let domVal = '';
            if (p.customDomains) {
                if (Array.isArray(p.customDomains)) domVal = p.customDomains.join(',');
                else domVal = p.customDomains;
            }
            document.getElementById('p_domains').value = domVal;
            document.getElementById('p_secretKey').value = p.secretKey || '';
        } else {
            document.getElementById('modalTitle').textContent = "新增映射";
            document.getElementById('p_name').value = '';
            document.getElementById('p_localPort').value = '';
            document.getElementById('p_remotePort').value = '';
            document.getElementById('p_domains').value = '';
            document.getElementById('p_secretKey').value = '';
            document.getElementById('p_localIp').value = '127.0.0.1';
        }
        toggleFields();
    }

    function closeModal() {
        document.getElementById('proxyModal').style.display = 'none';
    }

    function toggleFields() {
        const type = document.getElementById('p_type').value;
        document.getElementById('remotePortGroup').style.display = (type === 'tcp' || type === 'udp') ? 'block' : 'none';
        document.getElementById('domainGroup').style.display = (type === 'http' || type === 'https') ? 'block' : 'none';
        document.getElementById('secretGroup').style.display = (type === 'stcp') ? 'block' : 'none';
    }
    
    function editProxy(index) {
        openProxyModal(index);
    }
    
    function confirmDelete(index) {
        const proxyName = currentConfig.proxies[index].name;
        showConfirm(`确定要删除映射 "${proxyName}" 吗？删除后需点击"保存配置"才会生效。`, function() {
            currentConfig.proxies.splice(index, 1);
            
            const total = currentConfig.proxies.length;
            const maxPage = Math.ceil(total / PAGE_SIZE) || 1;
            if (currentPage > maxPage) currentPage = maxPage;
            
            renderProxies();
            addLog(`删除映射`, `名称: ${proxyName}`, 'del');
            showToast("已从列表中移除，请点击'保存配置'生效", 'info');
        });
    }

    function saveProxy() {
        const type = document.getElementById('p_type').value;
        const name = document.getElementById('p_name').value;
        const localPort = document.getElementById('p_localPort').value;
        
        if (!name || !localPort) {
            showToast("名称和本地端口不能为空", 'error');
            return;
        }

        const newProxy = {
            name: name,
            type: type,
            localIP: document.getElementById('p_localIp').value,
            localPort: parseInt(localPort)
        };

        if (type === 'tcp' || type === 'udp') {
            const rp = document.getElementById('p_remotePort').value;
            if(!rp) {
                showToast("TCP/UDP类型必须填写远程端口", 'error');
                return;
            }
            newProxy.remotePort = parseInt(rp);
        } else if (type === 'http' || type === 'https') {
            const dom = document.getElementById('p_domains').value;
            if (dom) newProxy.customDomains = dom.split(',').map(s=>s.trim());
        } else if (type === 'stcp') {
            newProxy.secretKey = document.getElementById('p_secretKey').value;
        }

        if (editingIndex >= 0) {
            currentConfig.proxies[editingIndex] = newProxy;
            addLog(`编辑映射`, `名称: ${name}, 类型: ${type}, 本地端口: ${localPort}`, 'edit');
        } else {
            currentConfig.proxies.unshift(newProxy);
            currentPage = 1;
            addLog(`新增映射`, `名称: ${name}, 类型: ${type}, 本地端口: ${localPort}`, 'add');
        }
        
        closeModal();
        renderProxies();
        showToast("配置已存储到内存中，请点击'保存配置'生效配置", 'success');
    }

    // ==================== 保存配置 - 带完整进度显示 ====================
    async function saveAllConfig() {
        const payload = {
            server: {
                addr: document.getElementById('cfg_addr').value,
                port: parseInt(document.getElementById('cfg_port').value) || 7000,
                token: document.getElementById('cfg_token').value
            },
            proxies: currentConfig.proxies
        };

        // 显示持久化进度遮罩
        showProgress('💾 保存配置并重载服务', '正在提交配置到服务器...');
        updateProgress('正在提交配置到服务器...', 5);

        try {
            // 第一步：发送保存请求
            updateProgress('正在写入配置文件并校验语法...', 15);

            const res = await fetch('/api/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            const data = await res.json();

            if (!data.success) {
                // 保存/校验失败
                finishProgress(false, '❌ 保存失败', data.message || '配置写入或校验出错，请检查后重试');
                addLog("保存配置失败", data.message || '', 'info');
                return;
            }

            // 第二步：后端已保存+重载成功，现在轮询等待服务就绪
            updateProgress('配置已写入，服务正在重载中...', 30);

            const proxyCount = payload.proxies.length;
            const serverAddr = payload.server.addr;
            addLog("保存配置并重载服务", `服务器: ${serverAddr}, 映射数量: ${proxyCount}`, 'save');

            // 轮询等待服务变为 active
            var ready = await pollServiceStatus(true, 35000);

            if (ready) {
                finishProgress(true, '✅ 配置保存成功，服务已就绪', data.message + ' — 所有映射已生效');
            } else {
                finishProgress(false, '⚠️ 配置已保存，但服务启动较慢', '配置已写入成功，但服务未在预期时间内就绪。请手动点击"刷新"确认状态，或点击"重启服务"。');
            }

        } catch (e) {
            finishProgress(false, '❌ 保存失败', '网络错误: ' + e.message);
        }
    }

    // ==================== 服务控制 - 带完整进度显示 ====================
    function confirmAction(action) {
        const msg = action === 'restart' ? '确定要重启 FRP 服务吗？' : '确定要停止 FRP 服务吗？';
        showConfirm(msg, async function() {
            var actionLabel = action === 'restart' ? '重启' : '停止';
            var expectedActive = (action === 'restart');

            // 显示持久化进度遮罩
            showProgress(
                (action === 'restart' ? '🔄 重启服务' : '⏹ 停止服务'),
                '正在发送' + actionLabel + '指令...'
            );
            updateProgress('正在发送' + actionLabel + '指令...', 10);

            try {
                const res = await fetch(`/api/service/${action}`, { method: 'POST' });
                const data = await res.json();

                if (!data.success) {
                    finishProgress(false, '❌ ' + actionLabel + '失败', data.message || '操作未成功');
                    addLog(actionLabel + ' FRP 服务', '操作结果: 失败', 'info');
                    loadData();
                    return;
                }

                // 指令发送成功，轮询等待状态变更
                updateProgress(actionLabel + '指令已发送，等待服务状态变更...', 30);
                addLog(actionLabel + ' FRP 服务', '操作结果: ' + data.message, 'info');

                var ready = await pollServiceStatus(expectedActive, 35000);

                if (ready) {
                    if (expectedActive) {
                        finishProgress(true, '✅ 服务重启成功', 'FRP 服务已重新运行，所有映射已生效');
                    } else {
                        finishProgress(true, '✅ 服务已停止', 'FRP 服务已成功停止');
                    }
                } else {
                    if (expectedActive) {
                        finishProgress(false, '⚠️ 重启指令已发送，但服务启动较慢', '请手动点击"刷新"确认服务状态');
                    } else {
                        finishProgress(false, '⚠️ 停止指令已发送，但服务停止较慢', '请手动点击"刷新"确认服务状态');
                    }
                }

            } catch (e) {
                finishProgress(false, '❌ 操作失败', '网络错误: ' + e.message);
                loadData();
            }
        });
    }

    let confirmCallback = null;

    function showConfirm(message, callback) {
        const modal = document.getElementById('confirmModal');
        const msgDiv = document.getElementById('confirmMessage');
        
        msgDiv.innerText = message;
        confirmCallback = callback;
        
        modal.style.display = 'flex';
    }

    function handleConfirmOk() {
        const cb = confirmCallback;
        closeConfirm();
        if (typeof cb === 'function') {
            cb();
        }
    }

    function closeConfirm() {
        document.getElementById('confirmModal').style.display = 'none';
        confirmCallback = null;
    }

    function showToast(msg, type = 'info') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerText = msg;
        container.appendChild(toast);
        
        requestAnimationFrame(() => {
            toast.classList.add('show');
        });

        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => {
                if (toast.parentNode) {
                    toast.parentNode.removeChild(toast);
                }
            }, 300);
        }, 3000);
    }

</script>
</body>
</html>"""

# ================== 主程序入口 ==================

if __name__ == '__main__':
    print(f"Starting FRP Manager on port {WEB_PORT}...")
    print(f"Detected Architecture: {platform.machine()}")
    print(f"Using FRP Directory: {FRPC_DIR}")
    print(f"Config file: {FRPC_CONF}")
    print(f"Admin Password: {ADMIN_PASSWORD}")
    print(f"Detected Local IP: {LOCAL_IP}")
    try:
        server = HTTPServer(('0.0.0.0', WEB_PORT), FrpHandler)
        print("Server is running. Access via http://<your-ip>:" + str(WEB_PORT))
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()
    except Exception as e:
        print(f"Error starting server: {e}")