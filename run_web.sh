#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${REPO}/docker-compose.web.yml"
IMAGE="crpi-3nfi31esiwp28zns.cn-hangzhou.personal.cr.aliyuncs.com/open_projects_yuchi/sam3d_gs:v0.1@sha256:81522e9bc03f7fc01d952ed1b8d64aada0634041a731cc0af7402a9a8c828ba8"
CONTAINER_NAME="gs-real2sim-web"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: 未找到 Docker。请先安装 Docker 和 NVIDIA Container Toolkit。" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: 无法连接 Docker daemon。请启动 Docker，并确认当前用户有权限使用它。" >&2
    exit 1
fi

if [[ ! -f "${REPO}/web/app.py" ]]; then
    echo "ERROR: 缺少 Web 入口 ${REPO}/web/app.py。" >&2
    exit 1
fi

mkdir -p \
    "${REPO}/data/jobs" \
    "${REPO}/submodule/Sam-3d-objects/checkpoints" \
    "${REPO}/submodule/Prompt-Inpaint/checkpoints" \
    "${HOME}/.cache/huggingface" \
    "${HOME}/.cache/torch"

missing_checkpoints=()
[[ -f "${REPO}/submodule/Sam-3d-objects/checkpoints/hf/pipeline.yaml" ]] || \
    missing_checkpoints+=("submodule/Sam-3d-objects/checkpoints/hf/pipeline.yaml")
[[ -f "${REPO}/submodule/Prompt-Inpaint/checkpoints/sam3.pt" ]] || \
    missing_checkpoints+=("submodule/Prompt-Inpaint/checkpoints/sam3.pt")

if (( ${#missing_checkpoints[@]} > 0 )); then
    echo "WARNING: 下列 checkpoint 尚未找到，/healthz 会报告未就绪：" >&2
    printf '  - %s\n' "${missing_checkpoints[@]}" >&2
    echo "请先按照 README_zh.md 执行 bash scripts/download_checkpoints.sh。" >&2
fi

if docker compose version >/dev/null 2>&1; then
    docker compose -f "${COMPOSE_FILE}" up -d --pull missing
    logs_command="docker compose -f ${COMPOSE_FILE} logs -f web"
    stop_command="docker compose -f ${COMPOSE_FILE} down"
else
    echo "INFO: 未找到 Docker Compose v2，使用 docker run 启动。"
    if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
        if [[ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}")" != "true" ]]; then
            docker start "${CONTAINER_NAME}" >/dev/null
        fi
    else
        docker run -d \
            --name "${CONTAINER_NAME}" \
            --restart unless-stopped \
            --init \
            --gpus all \
            --shm-size=8g \
            -p 8000:8000 \
            -v "${REPO}/web:/opt/sam3d_gs/web:ro" \
            -v "${REPO}/data/jobs:/opt/sam3d_gs/data/jobs" \
            -v "${REPO}/submodule/Sam-3d-objects/checkpoints:/opt/sam3d_gs/submodule/Sam-3d-objects/checkpoints" \
            -v "${REPO}/submodule/Prompt-Inpaint/checkpoints:/opt/sam3d_gs/submodule/Prompt-Inpaint/checkpoints" \
            -v "${HOME}/.cache/huggingface:/root/.cache/huggingface" \
            -v "${HOME}/.cache/torch:/root/.cache/torch" \
            -e SAM3D_PROJECT_ROOT=/opt/sam3d_gs \
            -e SAM3D_JOBS_ROOT=/opt/sam3d_gs/data/jobs \
            -e HF_HOME=/root/.cache/huggingface \
            -e TORCH_HOME=/root/.cache/torch \
            -e PYTHONUNBUFFERED=1 \
            "${IMAGE}" \
            python -m uvicorn web.app:app --host 0.0.0.0 --port 8000 --workers 1 \
            >/dev/null
    fi
    logs_command="docker logs -f ${CONTAINER_NAME}"
    stop_command="docker stop ${CONTAINER_NAME}"
fi

echo
echo "手机 Web 已在后台启动。"
echo "本机访问: http://localhost:8000"

lan_ip=""
for candidate in $(hostname -I 2>/dev/null || true); do
    if [[ "${candidate}" != *:* && "${candidate}" != 127.* ]]; then
        lan_ip="${candidate}"
        break
    fi
done

if [[ -n "${lan_ip}" ]]; then
    echo "局域网访问: http://${lan_ip}:8000"
else
    echo "局域网访问: http://<这台电脑的局域网 IP>:8000"
fi

echo
echo "局域网 HTTP 页面会自动使用系统相机/相册上传。"
echo "若要在网页内实时调用后置摄像头，请在本机执行："
echo "  tailscale serve --bg 8000"
echo "  tailscale serve status"
echo "然后在手机上打开 status 输出的 https://... 地址（手机需加入同一 Tailnet）。"
echo
echo "查看日志: ${logs_command}"
echo "停止服务: ${stop_command}"
