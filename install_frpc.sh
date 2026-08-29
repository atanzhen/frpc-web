#!/bin/bash

# ================== 全局配置 ==================
DEFAULT_FRP_VERSION="0.52.3"
WEB_PANEL_URL="http://107.149.212.83/frp/frpc_web.py"
INSTALL_DIR="/opt/frp"
WEB_PORT=7600
SERVICE_NAME="frpc"

# 内置可用版本列表 (按版本号升序排列)
AVAILABLE_VERSIONS=(
    "0.52.3"
    "0.54.0" "0.56.0" "0.57.0" "0.58.0" "0.58.1"
    "0.59.0" "0.60.0" "0.61.0" "0.61.1" "0.61.2" "0.62.0"
    "0.62.1" "0.64.0" "0.65.0" "0.66.0" "0.67.0" "0.68.0"
    "0.68.1" "0.69.0" "0.69.1" "0.70.0" "0.70.1"
)

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ================== 环境校验 ==================

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "请使用 root 权限运行此脚本"
        log_error "正确用法: curl -sSL <url> | sudo bash"
        exit 1
    fi
}

check_tty() {
    if [ ! -e /dev/tty ]; then
        log_error "未检测到终端设备 (/dev/tty)，无法进行交互式配置。"
        log_error "请确保您在交互式终端 (SSH) 中运行，而非后台任务或 cron。"
        exit 1
    fi
}

detect_arch() {
    local machine
    machine=$(uname -m)
    case "$machine" in
        x86_64|amd64)   ARCH="amd64" ;;
        aarch64|arm64)  ARCH="arm64" ;;
        armv7l|armv6l)  ARCH="arm" ;;
        i386|i686)      ARCH="386" ;;
        *)
            log_error "不支持的系统架构: $machine"
            exit 1
            ;;
    esac
    log_info "检测到系统架构: $machine -> ${CYAN}linux_${ARCH}${NC}"
}

install_dependencies() {
    log_info "正在检查并安装必要的依赖..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq >/dev/null 2>&1
        apt-get install -y wget curl tar python3 >/dev/null 2>&1
    elif command -v yum &>/dev/null; then
        yum install -y wget curl tar python3 >/dev/null 2>&1
    elif command -v dnf &>/dev/null; then
        dnf install -y wget curl tar python3 >/dev/null 2>&1
    else
        log_warn "无法识别包管理器，请确保已安装 wget, curl, tar, python3"
    fi
    log_info "依赖检查完成"
}

# ================== 交互逻辑 ==================

select_frp_version() {
    FRP_VERSION="$DEFAULT_FRP_VERSION"
    local total=${#AVAILABLE_VERSIONS[@]}

    echo ""
    echo -e "${CYAN}=========================================="
    echo -e "       选择 FRP 版本"
    echo -e "==========================================${NC}"
    echo ""

    for i in "${!AVAILABLE_VERSIONS[@]}"; do
        local ver="${AVAILABLE_VERSIONS[$i]}"
        local num=$((i + 1))
        if [ "$ver" = "$DEFAULT_FRP_VERSION" ]; then
            echo -e "  ${GREEN}$(printf '%3d' $num)) v${ver} ← 默认推荐${NC}"
        else
            echo "  $(printf '%3d' $num)) v${ver}"
        fi
    done

    echo ""
    while true; do
        echo -n "请输入编号 [1-${total}] (直接回车使用默认 v${DEFAULT_FRP_VERSION}): "
        read -r choice < /dev/tty

        if [ -z "$choice" ]; then
            log_info "使用默认版本: ${GREEN}v${FRP_VERSION}${NC}"
            return
        fi

        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "$total" ]; then
            FRP_VERSION="${AVAILABLE_VERSIONS[$((choice - 1))]}"
            log_info "已选择版本: ${GREEN}v${FRP_VERSION}${NC}"
            return
        fi

        log_warn "输入无效，请输入 1 到 ${total} 之间的数字"
    done
}

read_password() {
    echo ""
    echo -e "${CYAN}=========================================="
    echo -e "       配置 Web 管理面板密码"
    echo -e "==========================================${NC}"
    echo ""

    while true; do
        echo -n "请输入密码 (直接回车使用默认 admin123): "
        read -r USER_PASSWORD < /dev/tty
        
        # 强制换行，防止某些终端下提示语与日志重叠
        echo "" 

        if [ -z "$USER_PASSWORD" ]; then
            ADMIN_PASSWORD="admin123"
            log_info "使用默认密码: ${YELLOW}${ADMIN_PASSWORD}${NC}"
            return
        fi

        if [ ${#USER_PASSWORD} -lt 4 ]; then
            log_warn "密码太短，请至少输入 4 个字符"
            continue
        fi

        ADMIN_PASSWORD="$USER_PASSWORD"
        log_info "已设置自定义密码: ${YELLOW}******${NC}"
        return
    done
}

# ================== 部署逻辑 ==================

install_frp() {
    local frp_name="frp_${FRP_VERSION}_linux_${ARCH}"
    local download_url="https://cc.guluyun.cn/https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${frp_name}.tar.gz"
    local temp_dir="/tmp/frp_install_$$"

    mkdir -p "$INSTALL_DIR" "$temp_dir"
    log_info "正在从github下载 FRP v${FRP_VERSION} (linux_${ARCH})..."

    if ! wget -q --show-progress -O "${temp_dir}/frp.tar.gz" "$download_url" 2>&1; then
        log_warn "主镜像下载失败，尝试备用镜像..."
        download_url="http://117.55.229.75/frp/frp_releases/v${FRP_VERSION}/${frp_name}.tar.gz"
        if ! wget -q --show-progress -O "${temp_dir}/frp.tar.gz" "$download_url" 2>&1; then
            log_error "备用镜像均下载失败，请检查网络连接或尝试其他版本进行下载"
            rm -rf "$temp_dir"
            exit 1
        fi
    fi

    log_info "正在解压并安装到 ${INSTALL_DIR}..."
    tar -xzf "${temp_dir}/frp.tar.gz" -C "$temp_dir"

    cp -f "${temp_dir}/${frp_name}/frpc" "$INSTALL_DIR/"
    cp -f "${temp_dir}/${frp_name}/frps" "$INSTALL_DIR/" 2>/dev/null || true
    chmod +x "${INSTALL_DIR}/frpc"
    chmod +x "${INSTALL_DIR}/frps" 2>/dev/null || true

    if [ ! -f "${INSTALL_DIR}/frpc.toml" ]; then
        cat > "${INSTALL_DIR}/frpc.toml" << 'EOF'
# FRP 客户端配置文件 - 请通过 Web 管理面板进行配置
serverAddr = "127.0.0.1"
serverPort = 7000
EOF
    fi

    rm -rf "$temp_dir"
    log_info "FRP v${FRP_VERSION} 安装完成"
}

deploy_web_panel() {
    log_info "正在下载 Web 管理面板..."
    local web_panel_path="${INSTALL_DIR}/frpc_web.py"

    if ! wget -q --show-progress -O "$web_panel_path" "$WEB_PANEL_URL" 2>&1; then
        log_error "Web 面板下载失败: $WEB_PANEL_URL"
        exit 1
    fi

    chmod +x "$web_panel_path"
    log_info "Web 管理面板下载完成"
}

setup_systemd() {
    log_info "正在配置 Systemd 服务..."

    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl stop "${SERVICE_NAME}-web" 2>/dev/null || true

    # frpc.service: 不带 ExecStartPre=sleep，由脚本智能控制启动时机
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Frp Client Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Restart=on-failure
RestartSec=5s
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/frpc -c ${INSTALL_DIR}/frpc.toml
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

    # frpc-web.service: 无延迟，立即启动
    cat > "/etc/systemd/system/${SERVICE_NAME}-web.service" << EOF
[Unit]
Description=FRP Client Web Manager
After=network.target ${SERVICE_NAME}.service
Wants=${SERVICE_NAME}.service

[Service]
Type=simple
User=root
Restart=on-failure
RestartSec=5s
WorkingDirectory=${INSTALL_DIR}
Environment="FRPC_DIR=${INSTALL_DIR}"
Environment="FRP_ADMIN_PASSWORD=${ADMIN_PASSWORD}"
Environment="WEB_PORT=${WEB_PORT}"
Environment="SERVICE_NAME=${SERVICE_NAME}"
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/frpc_web.py
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}-web

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1
    systemctl enable "${SERVICE_NAME}-web" >/dev/null 2>&1

    log_info "Systemd 服务配置完成，已启用开机自启"
}

# ================== 智能网络检测与分级启动 ==================

check_network() {
    # 检测是否能连通外网 (DNS解析 + HTTP连通性)
    # 尝试多个目标，任一成功即视为网络正常
    local targets=(
        "https://www.baidu.com"
        "https://connect.rom.miui.com/generate_204"
        "https://www.cloudflare.com/cdn-cgi/trace"
        "https://8.8.8.8"
    )
    
    for target in "${targets[@]}"; do
        if curl -s --connect-timeout 3 --max-time 5 -o /dev/null -w "%{http_code}" "$target" 2>/dev/null | grep -qE "^[23]"; then
            return 0  # 网络正常
        fi
    done
    
    # 最后尝试 ping
    if ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
        return 0
    fi
    
    return 1  # 网络异常
}

check_frpc_running() {
    # 检查 frpc 是否正常运行
    if ! systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        return 1
    fi
    # 进一步检查日志中是否有成功标识
    if journalctl -u "${SERVICE_NAME}" --since "1 min ago" --no-pager 2>/dev/null | \
       grep -qE "(login to server success|start proxy success|connected to server)"; then
        return 0
    fi
    # 进程在运行但没有成功日志（可能是配置问题），也视为已启动
    return 0
}

smart_start_frpc() {
    """
    智能分级启动策略：
    第1阶段: 检测网络 → 正常 → 立即启动 frpc
    第2阶段: 网络异常 → 等待15秒 → 重新检测 → 正常则启动
    第3阶段: 仍异常 → 输出安装结果 → 后台守护进程1分钟后自动重试启动
    """
    
    log_info "🔍 正在检测网络连通性..."
    
    # ====== 第1阶段：快速检测 ======
    if check_network; then
        log_info "✅ 网络连通正常，立即启动 frpc 服务..."
        systemctl start "${SERVICE_NAME}" 2>/dev/null
        
        # 快速验证 (等待最多10秒)
        local waited=0
        while [ $waited -lt 10 ]; do
            sleep 2
            waited=$((waited + 2))
            if check_frpc_running; then
                log_info "✅ frpc 已成功启动并连接 (${waited}s)"
                return 0
            fi
            printf "\r   验证 frpc 状态中... %ds" "$waited"
        done
        echo ""
        
        # 启动了但可能还没连上
        if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
            log_info "🔄 frpc 进程已启动，正在连接服务端..."
            return 0
        fi
        
        log_warn "⚠️  frpc 已启动但未成功连接，进入第2阶段..."
    else
        log_warn "⚠️  网络连通性检测失败"
    fi
    
    # ====== 第2阶段：延迟15秒重试 ======
    log_info "⏳ 等待 15 秒后重试 (等待网络恢复)..."
    
    # 动态显示倒计时
    for i in $(seq 15 -1 1); do
        printf "\r   倒计时: %ds " "$i"
        sleep 1
    done
    echo ""
    
    log_info "🔍 重新检测网络连通性..."
    if check_network; then
        log_info "✅ 网络已恢复，启动 frpc 服务..."
        systemctl restart "${SERVICE_NAME}" 2>/dev/null
        
        # 验证 (等待最多15秒)
        local waited=0
        while [ $waited -lt 15 ]; do
            sleep 3
            waited=$((waited + 3))
            if check_frpc_running; then
                log_info "✅ frpc 已成功启动并连接 (${waited}s)"
                return 0
            fi
            printf "\r   验证 frpc 状态中... %ds" "$waited"
        done
        echo ""
        
        if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
            log_info "🔄 frpc 进程已启动，正在连接服务端..."
            return 0
        fi
    else
        log_warn "⚠️  网络仍然不通"
    fi
    
    # ====== 第3阶段：后台守护启动 ======
    log_warn "⚠️  网络暂不可用，将创建后台守护任务自动重试启动"
    log_info "📌 frpc 将在后台自动重试，不影响安装结果输出"
    
    # 创建后台守护脚本
    local guardian_script="${INSTALL_DIR}/.frpc_guardian.sh"
    cat > "$guardian_script" << 'GUARDIAN_EOF'
#!/bin/bash
# FRP 客户端后台守护启动脚本
# 自动在网络恢复后启动 frpc，最多重试 10 次

SERVICE_NAME="frpc"
MAX_RETRIES=10
RETRY_INTERVAL=60  # 每次间隔 60 秒
LOG_FILE="/tmp/frpc_guardian.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

log "=== 守护进程启动 ==="

for attempt in $(seq 1 $MAX_RETRIES); do
    log "第 ${attempt}/${MAX_RETRIES} 次尝试启动 frpc..."
    
    # 检测网络
    network_ok=false
    
    # 尝试 curl
    for target in "https://www.baidu.com" "https://connect.rom.miui.com/generate_204" "https://8.8.8.8"; do
        if curl -s --connect-timeout 3 --max-time 5 -o /dev/null "$target" 2>/dev/null; then
            network_ok=true
            break
        fi
    done
    
    # 尝试 ping
    if [ "$network_ok" = false ]; then
        if ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
            network_ok=true
        fi
    fi
    
    if [ "$network_ok" = true ]; then
        log "网络已恢复，尝试启动 frpc..."
        systemctl restart "$SERVICE_NAME" 2>/dev/null
        
        sleep 10
        
        if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
            log "✅ frpc 启动成功！守护进程退出。"
            # 清理自身
            rm -f "$0"
            exit 0
        else
            log "frpc 启动后未保持运行，${RETRY_INTERVAL}s 后重试..."
        fi
    else
        log "网络仍不可用，${RETRY_INTERVAL}s 后重试..."
    fi
    
    if [ $attempt -lt $MAX_RETRIES ]; then
        sleep $RETRY_INTERVAL
    fi
done

log "⚠️ 已达最大重试次数 (${MAX_RETRIES})，守护进程退出"
log "请手动检查网络后执行: systemctl restart frpc"

# 最终清理
rm -f "$0"
GUARDIAN_EOF
    
    chmod +x "$guardian_script"
    
    # 后台启动守护进程（完全脱离当前终端）
    nohup bash "$guardian_script" >/dev/null 2>&1 &
    local guardian_pid=$!
    disown $guardian_pid 2>/dev/null
    
    log_info "🛡️  后台守护进程已启动 (PID: ${guardian_pid})"
    log_info "   每 60 秒自动检测网络并重试，最多 10 次 (约 10 分钟)"
    log_info "   日志位置: /tmp/frpc_guardian.log"
    
    return 1  # 表示未立即启动成功
}

start_services() {
    log_info "正在启动服务..."

    # Web 面板优先启动（无延迟，立即可用）
    systemctl start "${SERVICE_NAME}-web"
    sleep 2

    if systemctl is-active --quiet "${SERVICE_NAME}-web"; then
        log_info "✅ Web 管理面板启动成功！"
    else
        log_error "❌ Web 管理面板启动失败"
        log_error "查看日志: journalctl -u ${SERVICE_NAME}-web -n 20 --no-pager"
    fi

    # frpc 智能分级启动
    local frpc_started=false
    if [ -f "${INSTALL_DIR}/frpc.toml" ] && \
       ! grep -qE '^\s*serverAddr\s*=\s*"127\.0\.0\.1"' "${INSTALL_DIR}/frpc.toml" 2>/dev/null; then
        smart_start_frpc
        if [ $? -eq 0 ]; then
            frpc_started=true
        fi
    else
        log_warn "frpc 启动跳过 (请先通过 Web 面板配置服务端信息)"
    fi
    
    # 返回启动状态供后续使用
    if [ "$frpc_started" = true ]; then
        FRPC_START_STATUS="running"
    else
        FRPC_START_STATUS="pending"
    fi
}

get_local_ip() {
    local ip
    ip=$(ip route get 8.8.8.8 2>/dev/null | awk '{print $7; exit}')
    if [ -z "$ip" ]; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    echo "${ip:-127.0.0.1}"
}

show_info() {
    local local_ip
    local_ip=$(get_local_ip)
    
    # frpc 状态描述
    local frpc_status_text=""
    local frpc_status_color=""
    if [ "${FRPC_START_STATUS}" = "running" ]; then
        frpc_status_text="✅ 已启动运行中"
        frpc_status_color="${GREEN}"
    else
        frpc_status_text="⏳ 后台守护启动中 (网络恢复后自动连接)"
        frpc_status_color="${YELLOW}"
    fi

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║            🎉 FRP 客户端部署完成！                       ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  📁 ${CYAN}安装目录:${NC}      ${INSTALL_DIR}"
    echo -e "  📄 ${CYAN}配置文件:${NC}      ${INSTALL_DIR}/frpc.toml"
    echo -e "  🐍 ${CYAN}Web 面板:${NC}      ${INSTALL_DIR}/frpc_web.py"
    echo -e "  🏗️  ${CYAN}FRP 版本:${NC}      v${FRP_VERSION} (linux_${ARCH})"
    echo -e "  📡 ${CYAN}frpc 状态:${NC}     ${frpc_status_color}${frpc_status_text}${NC}"
    echo ""
    echo -e "  🌐 ${YELLOW}Web 管理面板访问地址:${NC}"
    echo -e "     ➜ 局域网: ${GREEN}http://${local_ip}:${WEB_PORT}${NC}"
    echo -e "     ➜ 本  机: ${GREEN}http://127.0.0.1:${WEB_PORT}${NC}"
    echo -e "     ➜ 密  码: ${YELLOW}${ADMIN_PASSWORD}${NC}"
    echo ""
    echo -e "  ${RED}======================================================${NC}"
    echo -e "  ${RED}👉 下一步操作指南 (必读):${NC}"
    echo -e "  ${RED}======================================================${NC}"
    echo -e "  1. 浏览器打开上述 ${GREEN}Web 管理面板地址${NC}"
    echo -e "  2. 使用密码 ${YELLOW}${ADMIN_PASSWORD}${NC} 登录"
    echo -e "  3. 在【服务端配置】填写 FRP 服务器地址、端口、Token"
    echo -e "  4. 点击【💾 保存配置】自动重载连接服务器"
    echo -e "  5. 在【端口映射】中添加内网穿透规则即可使用"
    
    if [ "${FRPC_START_STATUS}" = "pending" ]; then
        echo ""
        echo -e "  ${YELLOW}======================================================${NC}"
        echo -e "  ${YELLOW}📌 后台守护任务说明:${NC}"
        echo -e "  ${YELLOW}======================================================${NC}"
        echo -e "  • 守护进程正在后台运行，每 60 秒自动检测网络"
        echo -e "  • 网络恢复后将自动启动 frpc 并连接服务端"
        echo -e "  • 查看守护日志: ${CYAN}cat /tmp/frpc_guardian.log${NC}"
        echo -e "  • 手动启动 frpc: ${CYAN}systemctl restart frpc${NC}"
        echo -e "  • 守护进程最多重试 10 次 (约 10 分钟)"
    fi
    
    echo -e "  ${RED}======================================================${NC}"
    echo ""
    echo -e "  ⚠️  ${YELLOW}提示: 请确保防火墙/安全组已放行端口 ${WEB_PORT}${NC}"
    echo ""
    
    # 查看守护日志（如果存在）
    if [ -f "/tmp/frpc_guardian.log" ]; then
        echo -e "  ${CYAN}[后台守护最新日志]:${NC}"
        tail -3 /tmp/frpc_guardian.log 2>/dev/null | while read -r line; do
            echo -e "    $line"
        done
        echo ""
    fi
}

# ================== 主执行流程 ==================
main() {
    # 仅在支持清屏的终端中清屏
    clear 2>/dev/null || true
    
    echo -e "${CYAN}=========================================="
    echo -e "    FRP 客户端 + Web 面板 智能部署脚本"
    echo -e "==========================================${NC}"
    echo ""

    check_root
    check_tty
    detect_arch
    install_dependencies
    select_frp_version
    read_password
    install_frp
    deploy_web_panel
    setup_systemd
    start_services
    show_info
}

main "$@"