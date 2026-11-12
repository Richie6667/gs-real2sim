# 用手机网页拍照生成 MuJoCo 资产

## 部署网页

在装有 NVIDIA GPU、Docker 和 NVIDIA Container Toolkit 的 Linux 机器上部署。项目文档建议 GPU 至少有 24 GB 显存。

```bash
git clone --recurse-submodules https://github.com/Richie6667/gs-real2sim.git
cd gs-real2sim
```

在 Hugging Face 申请 [SAM3](https://huggingface.co/facebook/sam3) 和 [SAM-3D-Objects](https://huggingface.co/facebook/sam-3d-objects) 的模型权限。获批后，使用有权限的账号配置权重。 [模型配置说明](README_zh.md#23-huggingface-权限申请)

```bash
hf auth login
bash scripts/download_checkpoints.sh
bash run_web.sh
curl -fsS http://localhost:8000/healthz
```

`/healthz` 返回 `"ready": true` 时，可以提交第一张照片。返回 503 时，用 `curl -s http://localhost:8000/healthz` 查看缺少的文件或 GPU 检查结果。

## 在手机上拍照并生成

1. 手机与部署机器连到同一局域网。在手机浏览器打开 `run_web.sh` 打印的局域网地址，例如 `http://192.168.1.10:8000`。
2. 点 **系统相机 / 相册**，用手机相机拍一张照片，或选一张已有照片。使用清晰的 JPEG 或 PNG，文件小于 20 MB。让桌面和周围背景完整入镜，目标物体约占画面的五分之一以内。
3. 在 **英文物体名称** 中填写目标物体，例如 `ceramic mug`，然后点 **开始生成 3D**。
4. 等待分割、背景重建和物体网格生成。完成后点 **下载全部 ZIP**，或单独下载网格和点云。失败时可从结果页下载日志。

如需在网页内预览后置摄像头，使用手机浏览器信任的 HTTPS 地址。部署机和手机都加入同一 Tailscale 网络时，可在部署机运行 `tailscale serve --bg 8000`，再用 `tailscale serve status` 查看 HTTPS 地址。在该地址打开网页，点 **开启后置摄像头**，允许相机权限，再点 **拍摄照片**。

## 将网格转成 MJCF

ZIP 里的 `3d_assets/` 包含物体网格和背景 Gaussian Splat（`bg.ply`）。解压并保留网格旁的材质和纹理文件。在装好项目 Python 环境的机器上运行下面的命令，把物体网格转换为 MuJoCo 可加载的 MJCF。

```bash
python pipeline/mesh2mjcf.py /path/to/3d_assets/object.obj
```

替换成实际的 OBJ 文件路径。需要可移动物体时，可按转换器文档加上 `--free_joint -cd`。 [MJCF 转换说明](README_zh.md#pipelinemesh2mjcfpy--mesh--mujoco-mjcf-转换器)
