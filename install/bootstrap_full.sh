#!/usr/bin/env bash
# 一条命令把 DLSS 5 NR (Wine) 从零装到一台干净的 vast/ComfyUI 机器上。
#
#   bash bootstrap_full.sh
#
# 幂等：每一步都先查后装，重跑只会补缺的。任何一步失败都会说清楚缺什么，不会假装成功。
#
# ── 装什么、为什么 ────────────────────────────────────────────────────
#   ① Xorg + dummy 虚拟显示 :99（带真实 60Hz Modeline）
#      去掉 DISPLAY 后 DXVK 连 Vulkan instance 都建不出来：
#        DxvkInstance::createInstance: Failed to create Vulkan instance
#      Modeline 必须给真实刷新率：Xvfb 报 0.00 Hz 会让 DXVK 在 swapchain 创建时整数除零
#      崩溃（EXCEPTION_INT_DIVIDE_BY_ZERO，退出码 148）。见 docs/FINDINGS.md ⑥。
#   ② GE-Proton（只用它的 wine64 与 prefix，不走 proton 脚本 —— Proton 后端会吞掉 worker 输出）
#   ③ NVIDIA NGX 运行时 nvngx_dlssnr.dll + nvngx_dlss.dll（从 Merserk 公开发布包里取）
#   ④ 驱动的 NGX loader nvngx.dll + _nvngx.dll（从**与本机驱动同版本**的官方 .run 解包）
#   ⑤ dxvk-nvapi >= 0.9.2 + vkd3d-proton >= 3.0.1（64-bit CuBIN 需要这两个版本）
#   ⑥ 本仓库的节点 + 上游源码 + 预编译原生二进制
#
# ❌ **不需要** Win10 版 d3dcompiler_47：实测本路径三个二进制里 D3DCompile 命中 0，
#    依赖只有 KERNEL32/d3d12/dxgi/msvcrt/ole32。那个编译器是 RenoDX/ReShade 那条
#    （在 Wine 下已确认走不通的）路才要的。
#
# ⚠️ NVIDIA 的运行时是**专有软件**，本仓库不含也不分发。第 ③④ 步是从官方/公开发布处
#    下到你自己的机器上，与你手动去下是同一件事。
set -uo pipefail

ROOT=${DLSS5NR_ROOT:-/workspace/dlss5nr}
PROTON_ROOT=${DLSS5_PROTON_ROOT:-/workspace/dlss5}
PROTON_TAG=${PROTON_TAG:-GE-Proton10-34}
PFX="$PROTON_ROOT/prefix/pfx"
BRIDGE_DIR="$PROTON_ROOT/ngx_bridge"
CN=${COMFYUI_CUSTOM_NODES:-/workspace/ComfyUI/custom_nodes}
WINE="$PROTON_ROOT/$PROTON_TAG/files/bin/wine64"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

PROTON_URL="https://github.com/GloriousEggroll/proton-ge-custom/releases/download/$PROTON_TAG/$PROTON_TAG.tar.gz"
PROTON_BYTES=516611656
MERSERK_URL="https://github.com/Merserk/dlss5-visual-enhancer/releases/download/v5.0/DLSS.5.Visual.Enhancer.v5.0.zip"
MERSERK_BYTES=481168925
NODE_REPO="https://github.com/LQCCS/ComfyUI-DLSS5NR-Wine"
UPSTREAM="https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux"
RELEASE_TGZ="$NODE_REPO/releases/download/v0.2.0/dlss5nr-native-mingw-x64.tar.gz"

say() { printf '\n== %s\n' "$1"; }
ok()  { printf '   OK   %s\n' "$1"; }
bad() { printf '   FAIL %s\n' "$1"; FAILED=$((FAILED+1)); }
FAILED=0

command -v nvidia-smi >/dev/null 2>&1 || { echo "没有 nvidia-smi，这不是 GPU 机器"; exit 1; }
DRV=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')
GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
say "目标机: $GPU  驱动 $DRV  盘 $(df -h /workspace | awk 'NR==2{print $4}') 可用"

# 正在 provisioning 就别抢带宽——这一步会下 ~1.4 GB
if [ -f /.provisioning ]; then
    echo "   ⚠️ /.provisioning 还在：机器正在下模型。现在装会抢带宽。"
    echo "      要么等它消失，要么设 DLSS5NR_FORCE=1 强行继续。"
    [ "${DLSS5NR_FORCE:-0}" = "1" ] || exit 2
fi

# ── ① 虚拟显示 ────────────────────────────────────────────────────
say "① Xorg + dummy 虚拟显示 :99"
command -v Xorg >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    xserver-xorg-core xserver-xorg-video-dummy x11-utils x11-xserver-utils >/dev/null 2>&1
if command -v Xorg >/dev/null 2>&1; then
    cp -f "$HERE/xorg-dummy.conf" /etc/X11/xorg-dummy.conf
    if [ -d /etc/supervisor/conf.d ]; then
        cat > /etc/supervisor/conf.d/dlss5_display.conf <<'EOF'
[program:dlss5_display]
command=/usr/bin/Xorg :99 -config /etc/X11/xorg-dummy.conf -noreset -nolisten tcp -logfile /var/log/portal/xorg99.log
autostart=true
autorestart=true
priority=1
redirect_stderr=true
EOF
        supervisorctl reread >/dev/null 2>&1; supervisorctl update >/dev/null 2>&1; sleep 6
    else
        pgrep -f 'Xorg :99' >/dev/null || \
          (nohup Xorg :99 -config /etc/X11/xorg-dummy.conf -noreset -nolisten tcp >/tmp/xorg99.log 2>&1 & sleep 5)
    fi
    RATE=$(DISPLAY=:99 xrandr 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\*' | head -1)
    [ -n "$RATE" ] && [ "$RATE" != "0.00*" ] && ok ":99 刷新率 $RATE" || bad ":99 刷新率=${RATE:-无}（0.00 会触发 DXVK 除零）"
else
    bad "Xorg 装不上"
fi

# ── ② GE-Proton + prefix ─────────────────────────────────────────
say "② GE-Proton $PROTON_TAG + wine prefix"
mkdir -p "$PROTON_ROOT"
if [ -x "$WINE" ]; then
    ok "已有 $PROTON_TAG"
else
    echo "   下 $PROTON_TAG（$((PROTON_BYTES/1000000)) MB）…"
    aria2c -x16 -s16 -k1M --console-log-level=warn --summary-interval=0 --allow-overwrite=true \
        --auto-file-renaming=false -d "$PROTON_ROOT" -o proton.tgz "$PROTON_URL" >/dev/null 2>&1
    got=$(stat -c%s "$PROTON_ROOT/proton.tgz" 2>/dev/null || echo 0)
    if [ "$got" = "$PROTON_BYTES" ]; then
        tar xzf "$PROTON_ROOT/proton.tgz" -C "$PROTON_ROOT" && rm -f "$PROTON_ROOT/proton.tgz"
        [ -x "$WINE" ] && ok "解包完成" || bad "解包后没有 $WINE"
    else
        bad "GE-Proton 下载字节不符 got=$got want=$PROTON_BYTES"
    fi
fi
# 🔴 prefix **必须由 Proton 自己建**，不能用裸 wineboot。
#    2026-09-04 在 49837537 上踩：用 `wine64 wineboot -u` 建出来的是最小 prefix，
#    system32 里没有 DXVK 的 dxgi/d3d11、没有 dxvk-nvapi 的 nvapi64/nvofapi64。
#    后果是一路静默降级：
#      · 先是 LoadLibrary(dlss5nr_bridge.dll) failed: 126（ERROR_MOD_NOT_FOUND，
#        因为它依赖的 dxgi 是 Wine 内建的 718854 B 版本，不是 DXVK 的 4435982 B）
#      · 手工补上 dxgi/d3d11 之后又变成 NvAPI_EnumPhysicalGPUs failed with error: -2
#        (NVAPI_LIBRARY_NOT_FOUND)，NGX 认不出显卡 -> Init_ProjectID 返回 0xBAD00001。
#        +loaddll 证实 nvapi64.dll **从头到尾没被加载过**。
#    Proton 的 `proton run` 会把 DXVK / vkd3d-proton / dxvk-nvapi 一整套按它自己的
#    版本矩阵装进 prefix —— 这是能跑的那台（49812079）当初的建法。照抄它。
#
#    Proton 要 STEAM_COMPAT_CLIENT_INSTALL_PATH（缺了直接 KeyError），
#    prefix 落在 $STEAM_COMPAT_DATA_PATH/pfx。
if [ -x "$WINE" ] && [ ! -f "$PFX/drive_c/windows/system32/nvapi64.dll" ]; then
    echo "   用 Proton 建 prefix（不是 wineboot —— 见上面注释）…"
    # Proton 只在 STEAM_COMPAT_DATA_PATH **已存在**时才work：它在里面建 pfx.lock，
    # 但不会创建这个父目录。目录不在就抛
    #   FileNotFoundError: .../prefix/pfx.lock
    mkdir -p /root/.steam/root "${XDG_RUNTIME_DIR:-/tmp/xdg}" "$PROTON_ROOT/prefix" 2>/dev/null || true
    chmod 700 "${XDG_RUNTIME_DIR:-/tmp/xdg}" 2>/dev/null || true
    PROTON_BIN="$PROTON_ROOT/$PROTON_TAG/proton"
    if [ -x "$PROTON_BIN" ]; then
        STEAM_COMPAT_CLIENT_INSTALL_PATH=/root/.steam/root         STEAM_COMPAT_DATA_PATH="$PROTON_ROOT/prefix"         XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/xdg}"         PROTON_DISABLE_XALIA=1 WINEDEBUG=-all DISPLAY=:99             timeout 600 "$PROTON_BIN" run wineboot -u >/dev/null 2>&1 || true
        sleep 3
    else
        echo "   ✗ 找不到 $PROTON_BIN，退回 wineboot（prefix 会不完整）"
        mkdir -p "$PFX"
        WINEPREFIX="$PFX" WINEARCH=win64 WINEDEBUG=-all timeout 300 "$WINE" wineboot -u >/dev/null 2>&1 || true
    fi
fi
# Proton 建完之后校验那几个关键 DLL 的**大小**——大小不对就说明拿到的是 Wine 内建版。
if [ -d "$PFX/drive_c" ]; then
    S0="$PFX/drive_c/windows/system32"
    for chk in "dxgi.dll:2000000" "d3d11.dll:2000000" "nvapi64.dll:500000" "d3d12core.dll:2000000"; do
        f=${chk%%:*}; min=${chk##*:}
        sz=$(stat -c%s "$S0/$f" 2>/dev/null || echo 0)
        if [ "$sz" -lt "$min" ]; then
            echo "   ⚠️ $f 只有 $sz B（<$min）—— 像是 Wine 内建而不是 DXVK/vkd3d/nvapi，从 GE-Proton 树补"
            for sub in dxvk vkd3d-proton nvapi; do
                src="$PROTON_ROOT/$PROTON_TAG/files/lib/wine/$sub/x86_64-windows/$f"
                [ -s "$src" ] && { cp -f "$src" "$S0/$f"; echo "      <- $sub ($(stat -c%s "$S0/$f") B)"; break; }
            done
        fi
    done
    # nvofapi64 是 dxvk-nvapi 的另一半，能跑的那台有、裸 prefix 没有
    [ -s "$S0/nvofapi64.dll" ] || {
        src="$PROTON_ROOT/$PROTON_TAG/files/lib/wine/nvapi/x86_64-windows/nvofapi64.dll"
        [ -s "$src" ] && cp -f "$src" "$S0/" && echo "   补 nvofapi64.dll"
    }
fi
[ -d "$PFX/drive_c" ] && ok "prefix $PFX" || bad "prefix 没建出来"

# ── ③ NVIDIA NGX 运行时（自 Merserk 公开发布包）──────────────────
say "③ nvngx_dlssnr.dll + nvngx_dlss.dll"
mkdir -p "$ROOT/runtime"
if [ -s "$ROOT/runtime/nvngx_dlssnr.dll" ] && [ -s "$ROOT/runtime/nvngx_dlss.dll" ]; then
    ok "已有（$(stat -c%s "$ROOT/runtime/nvngx_dlssnr.dll") / $(stat -c%s "$ROOT/runtime/nvngx_dlss.dll") B）"
else
    T=/workspace/_merserk; rm -rf "$T"; mkdir -p "$T"
    echo "   下 Merserk v5.0（$((MERSERK_BYTES/1000000)) MB，只为取那两个 DLL）…"
    aria2c -x16 -s16 -k1M --console-log-level=warn --summary-interval=0 --allow-overwrite=true \
        --auto-file-renaming=false -d "$T" -o m.zip "$MERSERK_URL" >/dev/null 2>&1
    got=$(stat -c%s "$T/m.zip" 2>/dev/null || echo 0)
    if [ "$got" = "$MERSERK_BYTES" ]; then
        (command -v 7z >/dev/null 2>&1 && 7z x -y -o"$T/x" "$T/m.zip" >/dev/null 2>&1) || unzip -qo "$T/m.zip" -d "$T/x"
        for f in nvngx_dlssnr.dll nvngx_dlss.dll; do
            src=$(find "$T/x" -name "$f" -print -quit)
            [ -n "$src" ] && cp -f "$src" "$ROOT/runtime/$f" || bad "包里没有 $f"
        done
        [ -s "$ROOT/runtime/nvngx_dlssnr.dll" ] && ok "取到运行时" || bad "运行时没取到"
    else
        bad "Merserk 包字节不符 got=$got want=$MERSERK_BYTES"
    fi
    rm -rf "$T"
fi

# ── ④ 驱动的 NGX loader（必须与本机驱动同版本）───────────────────
say "④ NGX loader nvngx.dll / _nvngx.dll（驱动 $DRV）"
if [ -s "$BRIDGE_DIR/_nvngx.dll" ] && [ -s "$BRIDGE_DIR/nvngx.dll" ]; then
    ok "已有桥接件"
else
    T=/workspace/_nvdrv; rm -rf "$T"; mkdir -p "$T"
    U="https://us.download.nvidia.com/XFree86/Linux-x86_64/$DRV/NVIDIA-Linux-x86_64-$DRV.run"
    echo "   下驱动 $DRV 并只解包取两个 PE…"
    if aria2c -x16 -s16 -k1M --console-log-level=warn --summary-interval=0 --allow-overwrite=true \
              --auto-file-renaming=false -d "$T" -o drv.run "$U" >/dev/null 2>&1; then
        sh "$T/drv.run" --extract-only --target "$T/x" >/dev/null 2>&1
        mkdir -p "$BRIDGE_DIR"
        for f in nvngx.dll _nvngx.dll; do
            src=$(find "$T/x" -name "$f" -print -quit)
            [ -n "$src" ] && cp -f "$src" "$BRIDGE_DIR/$f" || bad "驱动包里没有 $f"
        done
        [ -s "$BRIDGE_DIR/_nvngx.dll" ] && ok "取到桥接件" || bad "桥接件没取到"
    else
        bad "驱动 $DRV 的 .run 拿不到（官方源没有这个版本？）"
    fi
    rm -rf "$T"
fi

# ── ⑤ prefix 侧：nvapi/vkd3d 升级 + NGX 注册表 ───────────────────
say "⑤ prefix 组件（dxvk-nvapi >=0.9.2 / vkd3d-proton >=3.0.1 / NGX 注册表）"
if [ -d "$PFX/drive_c" ] && [ -x "$WINE" ]; then
    DLSS5NR_SKIP_DXC=1 bash "$HERE/setup_prefix.sh" "$PFX" "$WINE" "$BRIDGE_DIR" 2>&1 | sed 's/^/   /'
else
    bad "prefix 或 wine64 缺失，跳过"
fi

# ── ⑥ 节点 + 上游源码 + 原生二进制 ───────────────────────────────
say "⑥ ComfyUI 节点 + 上游源码 + 原生二进制"
mkdir -p "$CN"
if [ -d "$CN/ComfyUI-DLSS5NR-Wine/.git" ]; then
    git -C "$CN/ComfyUI-DLSS5NR-Wine" pull --ff-only -q 2>/dev/null && ok "节点已更新" || ok "节点已有"
else
    git clone -q --depth 1 "$NODE_REPO" "$CN/ComfyUI-DLSS5NR-Wine" 2>&1 | tail -2
    [ -f "$CN/ComfyUI-DLSS5NR-Wine/nodes.py" ] && ok "节点 clone 完成" || bad "节点 clone 失败"
fi
if [ -f "$ROOT/tools/dlss5nr_video.py" ]; then
    ok "上游源码已有"
else
    git clone -q --depth 1 "$UPSTREAM" "$ROOT.src" 2>/dev/null && {
        mkdir -p "$ROOT"
        cp -rn "$ROOT.src/." "$ROOT/" 2>/dev/null
        rm -rf "$ROOT.src"
    }
    [ -f "$ROOT/tools/dlss5nr_video.py" ] && ok "上游源码 clone 完成" || bad "上游源码没拿到（节点要 import 它的协议常量）"
fi
if [ -x "$ROOT/native/bin/dlss5nr_host.exe" ] && [ -s "$ROOT/native/bin/dlss5nr_bridge.dll" ]; then
    ok "原生二进制已有"
else
    echo "   下预编译二进制（473 KB，含 DLSS 模型预设补丁）…"
    if curl -sfL "$RELEASE_TGZ" | tar xz -C "$ROOT"; then
        ok "二进制解包完成"
    else
        echo "   release 拿不到，改用 MinGW 现编…"
        bash "$HERE/build_native.sh" "$ROOT" 2>&1 | tail -6 | sed 's/^/   /'
    fi
fi
[ -s "$ROOT/runtime/caller/nvngx.dll_comfy.dll" ] || \
    cp -f "$ROOT/native/bin/nvngx.dll_comfy.dll" "$ROOT/runtime/caller/" 2>/dev/null

# ── 自检 ─────────────────────────────────────────────────────────
say "自检"
for f in "$ROOT/native/bin/dlss5nr_host.exe" "$ROOT/native/bin/dlss5nr_bridge.dll" \
         "$ROOT/runtime/caller/nvngx.dll_comfy.dll" "$ROOT/runtime/nvngx_dlssnr.dll" \
         "$ROOT/runtime/nvngx_dlss.dll" "$WINE" "$PFX/drive_c" \
         "$CN/ComfyUI-DLSS5NR-Wine/nodes.py" "$ROOT/tools/dlss5nr_video.py"; do
    [ -e "$f" ] && printf '   OK   %-52s %s\n' "$(basename "$f")" "$([ -f "$f" ] && stat -c%s "$f" || echo dir)" \
                || bad "缺 $f"
done
echo -n "   预设补丁: "; strings -a "$ROOT/native/bin/dlss5nr_bridge.dll" 2>/dev/null | grep -c DLSS5NR_MODEL_PRESET

if [ "$FAILED" -eq 0 ]; then
    cat <<EOF

== 全部就绪。接下来：
   1) supervisorctl restart comfyui     # 自定义节点只在启动时扫描
   2) 工作流里加 "DLSS 5 NR · 自检 (Wine)" 先跑一遍
   3) 再接 "DLSS 5 神经渲染 · Wine (Linux)"
      推荐档（2026-09-04 按 Blueforcer 单变量实测订正）：
        model_preset=M   auto_mask=开   skin=2.0   structure=1.5~2.0
        只清理不放大 -> scale=1.0x (DLAA)；要放大 -> 1.724x / 2.0x
      关键：skin 只在 auto_mask=开 时生效；关着等于放弃材质重建。
      别拿"整体锐度/高频能量"判好坏 —— 本节点按设计就会降低整体高频，
      它加的是皮肤/头发/织物的材质结构。用眼睛看毛孔、发丝、织物。
      ⚠️ 肤色可能漂移（社区报告），化妆品/人像片必须专门核对这一项。
EOF
else
    echo ""
    echo "== 有 $FAILED 项没成，上面标了 FAIL。修好后重跑本脚本（幂等）。"
    exit 1
fi
