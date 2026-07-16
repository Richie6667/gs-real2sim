#!/usr/bin/env bash
# Repair the NVIDIA/GL runtime shipped in the sam3d_gs base image.
#
# The base image bundles a set of "forward-compatibility" driver files
# (libcuda.so.570.*, libEGL_nvidia.so.570.*, ...) plus a matching
# ld.so.conf.d entry that puts /usr/local/cuda-*/compat first on the
# library search path. On consumer GPUs (e.g. GeForce RTX 4090) that
# forward-compat driver is unsupported and CUDA init fails with:
#   Error 804: forward compatibility was attempted on non supported HW
#
# In addition several vendor files were baked in as 0-byte stubs,
# including /usr/share/glvnd/egl_vendor.d/10_nvidia.json, which breaks
# EGL headless rendering (pyrender) with:
#   GLError err=12289 (EGL_NOT_INITIALIZED) at eglInitialize
#
# This script drops the compat entry (so the host-mounted driver is used)
# and restores the NVIDIA EGL vendor config. It is idempotent and safe to
# run both at image-build time and at container start.
set -eu

# 1. Stop preferring the bundled forward-compat CUDA driver.
rm -f /etc/ld.so.conf.d/*compat*.conf
ldconfig 2>/dev/null || true

# 2. Restore the NVIDIA EGL vendor config if it is missing or empty.
egl_json=/usr/share/glvnd/egl_vendor.d/10_nvidia.json
if [ ! -s "$egl_json" ]; then
    mkdir -p "$(dirname "$egl_json")"
    cat > "$egl_json" <<'JSON'
{
    "file_format_version" : "1.0.0",
    "ICD" : {
        "library_path" : "libEGL_nvidia.so.0"
    }
}
JSON
fi
