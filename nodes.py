# -*- coding: utf-8 -*-
"""DLSS 5 Neural Rendering（feature 18）· Wine 路径 · ComfyUI 节点

为什么另起一个节点包，而不是修 ComfyUI-DonutNodes：
  DonutNodes 的 DLSS5 分支走 Merserk worker + ReShade + renodx-dlss5.addon64。
  那条路在 Wine 下必崩：feature 18 create 之后 addon 静默 abort。
  一手依据 NIGos/dlss5-bridge#22（同一个 addon 构建 sha256 D5ADF82E…，
  同为 Blackwell + vkd3d-proton，对方用参考版 nvngx_dlssnr 也一样崩），
  issue 至今 open，维护者没有 Linux 环境无法复现。

  本节点走 kos94ok/ComfyUI-DLSS5-NR-Linux 的 host：它自己持有 D3D12/NGX 会话，
  **完全不加载 ReShade / RenoDX**，所以那个 bug 不在路径上。2026-09-04 在
  49812079（RTX 5090 / 驱动 590.48.01）实测：1.0x 改变像素（平均绝对差
  28.049/255）、2× 480x736→960x1472、1.5× →720x1104，host 的
  CreateFeature(18) failed / DLSSNR EvaluateFeature failed 两条错误路径均未触发。
  吞吐 8.72 帧/秒（2×），15s@24fps 约 41 秒。

协议原语直接 import 上游的 tools/dlss5nr_video.py，不复制粘贴 ——
上游改协议时我们跟着走，不会悄悄对不上。
"""

from __future__ import annotations

import importlib.util
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import torch

# 默认路径：装机脚本把仓库放这里
DEFAULT_REPO = "/workspace/dlss5nr"
DEFAULT_WINE = "/workspace/dlss5/GE-Proton10-34/files/bin/wine64"
DEFAULT_PFX = "/workspace/dlss5/prefix/pfx"
DEFAULT_DISPLAY = ":99"

# 档位 -> (倍率, PerfQualityValue)。取自上游 _PERF_QUALITY，写死在这里只是为了
# 让下拉框有稳定顺序；真正的值仍从上游模块校验。
SCALE_CHOICES = {
    # ⚠️ 1.0x 没有 carrier，feature 18 直接作用在已经清晰的原片上会把它磨软
    #    （实测 -3.9%）。feature 18 是给「放大后变软的画面」补结构的，别用 1.0x 求清晰。
    "1.0x (DLAA / 原尺寸·会变软，别用来求清晰)": 1.0,
    "1.5x (Quality)": 1.5,
    "1.724x (Balanced)": 1.724,
    "2.0x (Performance)": 2.0,
    "3.0x (Ultra Performance)": 3.0,
}
STYLE_CHOICES = {"Default": 0, "Natural": 1, "Cinematic": 2}

# DLSS 模型预设（SR carrier 用的，不是 NR 的 preset）。SDK 值 J=10 K=11 L=12 M=13。
# 🔴 这是清晰度最大的一个杠杆，而上游 bridge 从来没设过 —— 它一直跑在驱动默认档上。
#    实测（RTX 5090，H3 出片 736x1280 -> 1.724x，structure=2.0，高频能量 vs Lanczos）：
#        默认  +1.9%      J  +2.0%      K  +1.9%（与默认逐字节相同 => 默认就是 K）
#        L    +14.3%      M +14.5%
#    与社区口径一致：50 系默认 Quality/Balanced=K、Performance=M；K 偏软、M 最锐。
#    实现方式：给 nvngx_dlss.dll 的六个 DLSS.Hint.Render.Preset.* 键统一写入该值
#    （键名从 DLL 字符串表确认），做法沿用 Merserk v4.0 changelog 的描述。
#    需要打过补丁的 dlss5nr_bridge.dll（读环境变量 DLSS5NR_MODEL_PRESET）。
MODEL_PRESETS = {"Default (驱动默认·实测=K)": 0, "J (10)": 10, "K (11)": 11,
                 "L (12) 锐": 12, "M (13) 最锐": 13}

# 🔴 哪些参数真的起作用（2026-09-04 实测 + 一手来源）
#
#   structure  ✅ 唯一真正的清晰度杠杆。NVIDIA 官方：Structure Intensity 管高频细节
#                （环境光遮蔽/接触阴影/反射/次表面散射），SDK 范围 0-1。
#                Merserk UI 放到 0-2。实测 3.0 -> 高频能量 +9.7%、4.0 -> +12.8%，
#                越过文档上限仍在起效，但已无任何文档背书，自担风险。
#   tone       ✅ 低频：整体光照与色彩响应。设 0 = 完全保留原片配色。效果比 structure 小。
#   skin       ⚠️ 只有 auto_mask=True 时才有效果（实测：mask 关时改它输出逐字节不变）。
#                -1 的语义是「跟随 structure」，不是关闭。
#   auto_mask  ✅ 自动识别皮肤区域并保护它不被过度锐化 —— 因此会**降低**整体锐度。
#                人脸特写建议开，产品/环境建议关。
#   intensity  ❌ 本路径无效。NVIDIA SDK 里根本没有独立的 intensity（只有 Structure/Tone
#                两个滑块）；RenoDX 的 NRIntensity 应是它自己混合层的乘数，而这条 bridge
#                直接调 DLSSNR 快照、没有那层混合。实测改它输出逐字节不变。
#   preset     ❌ inert。出厂 DLL 只含单一网络，没有可切换的对象。
#   style      ❌ inert，同上。
#
# 依据：NVIDIA DLSS 5 新闻稿 / TechPowerUp 技术预览（Structure & Tone 语义、0-1 范围、
#      「环境拉满、人脸保守」的工作室共识）；Merserk README 控制表（0-2 范围与默认值）；
#      nexusmods/site/mods/2224（skin=-1 跟随 structure、preset/style 可能无效）；
#      ThunderRuler/dlss5-installer-skill config-reference.md（从 addon 二进制提取的键表，
#      明确写 NRPreset/NRStyle "Currently inert"）；以及本机 RTX 5090 上的高频能量实测。
#
# ⚠️ 高频能量只是锐度的代理指标，不等于「好看」。社区共识是拉满 = "AI slop look"，
#    人脸过锐会出假毛孔假皱纹。上生产前必须用眼睛在真实素材上确认。


class Dlss5NRWineError(RuntimeError):
    pass


def _resolve_choice(value, table, what):
    """按前缀回退匹配。档位文案改过之后，老工作流里存的旧字符串仍然要能命中。

    model_preset 之前走的是 MODEL_PRESETS.get(value, 0)，认不出来的档位会静默
    退回驱动默认 —— 等于悄悄关掉最大的清晰度杠杆，而且哪里都不报错。
    """
    if value in table:
        return table[value]
    head = str(value).split(" ")[0]
    for key, resolved in table.items():
        if key.split(" ")[0] == head:
            return resolved
    raise Dlss5NRWineError("未知%s %r，可选：%s" % (what, value, list(table)))


def _load_upstream(repo: Path):
    """把上游前端当模块加载，借它的协议常量与时域向导。"""
    tool = repo / "tools" / "dlss5nr_video.py"
    if not tool.is_file():
        raise Dlss5NRWineError(
            "找不到 %s。先 git clone https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux %s"
            % (tool, repo)
        )
    spec = importlib.util.spec_from_file_location("_dlss5nr_video", tool)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_dlss5nr_video"] = mod
    spec.loader.exec_module(mod)
    return mod


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0) * 2))


def _drain(stream, sink, cap=400):
    def run():
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                sink.append(line)
                del sink[:-cap]
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


class DLSS5NRWineUpscale:
    """IMAGE 批 -> DLSS 5 神经渲染 -> IMAGE 批。整批共用一个持久 NGX 会话。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "scale": (list(SCALE_CHOICES), {"default": "1.724x (Balanced)",
                          "tooltip": "求清晰必须 >1x。1.0x 档没有 carrier，只会变软"}),
                "repo_dir": ("STRING", {"default": DEFAULT_REPO, "multiline": False}),
                "wine": ("STRING", {"default": DEFAULT_WINE, "multiline": False}),
                "prefix": ("STRING", {"default": DEFAULT_PFX, "multiline": False}),
                "display": ("STRING", {"default": DEFAULT_DISPLAY, "multiline": False,
                                       "tooltip": "必填。去掉 DISPLAY 后 DXVK 连 Vulkan instance 都建不出来（实测）。"}),
                "warmup_frames": ("INT", {"default": 8, "min": 0, "max": 600}),
                "style": (list(STYLE_CHOICES), {"tooltip": "❌ 无效：出厂 DLL 只含单一网络，没有可切换对象"}),
                "preset": ("INT", {"default": 0, "min": 0, "max": 7,
                                   "tooltip": "❌ 无效，同 style"}),
                "intensity": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05,
                                        "tooltip": "❌ 本路径无效（实测改它输出逐字节不变）。NVIDIA SDK 里没有独立 intensity"}),
                "tone": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05,
                                   "tooltip": "低频：整体光照与色彩响应。0 = 完全保留原片配色"}),
                "structure": ("FLOAT", {"default": 2.0, "min": 0.0, "max": 4.0, "step": 0.05,
                                        "tooltip": "✅ 唯一真正的清晰度杠杆。NVIDIA SDK 0-1、Merserk UI 0-2；实测 3.0=+9.7% 4.0=+12.8% 仍在起效但超出文档。人脸 1.5-2.0，产品/环境 2.0-3.0"}),
                "skin": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 2.0, "step": 0.05,
                                   "tooltip": "-1 = 跟随 structure。⚠️ 只有 auto_mask 开启时才有效果"}),
                "auto_mask": ("BOOLEAN", {"default": False,
                                          "tooltip": "⚠️ 想要清晰就关掉。实测开启把 +1.9% 压回 -0.4%（相对 Lanczos）。画面里皮肤占比越大，它抑制得越多"}),
                "reset_each_frame": ("BOOLEAN", {"default": False,
                                                 "tooltip": "关掉时域复用。静态图批量走这个，视频不要开。"}),
                "channel_order": (["auto", "RGBA", "BGRA"],),
                "gpu_index": ("INT", {"default": 0, "min": 0, "max": 7}),
                # 🔴 新参数一律加在**最后**，绝不插中间。
                #    ComfyUI 的 widgets_values 是**按位置**读的：在中间插一个，
                #    所有老工作流会整体错位一格 —— 2026-09-04 实际发生过，报了 6 个错
                #    （model_preset 缺少连接 / warmup_frames、preset、gpu_index 类型错误 /
                #     style、channel_order 值不适用）。加在末尾则老工作流缺最后一个值，
                #    ComfyUI 自动取 default，完全兼容。
                "model_preset": (list(MODEL_PRESETS), {"default": "M (13) 最锐",
                                  "tooltip": "🔴 清晰度最大杠杆。实测 L/M 比默认(K)高 12 个百分点。需要打过补丁的 bridge；未打补丁时此项无效果"}),
            }
        }

    @classmethod
    def VALIDATE_INPUTS(cls, scale, model_preset):
        """让上面的前缀回退真的有机会跑到。

        ComfyUI 会拿 COMBO 的值去比对**当前**选项列表，对不上就直接以
        "Value not in list" 拒掉整个 prompt —— 节点代码一行都不会执行。所以
        80ff35e 给 1.0x 改名之后，改名前存下的工作流其实是打不开的，
        upscale() 里那段前缀回退永远走不到。

        在这里点名 scale 和 model_preset，这两项的校验就交给我们自己做。
        """
        for value, table, what in ((scale, SCALE_CHOICES, "放大档"),
                                   (model_preset, MODEL_PRESETS, "模型预设")):
            try:
                _resolve_choice(value, table, what)
            except Dlss5NRWineError as exc:
                return str(exc)
        return True

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "report")
    FUNCTION = "upscale"
    CATEGORY = "image/DLSS 5 NR (Wine)"

    def upscale(self, image, scale, repo_dir, wine, prefix, display, warmup_frames,
                style, preset, intensity, tone, structure, skin, auto_mask,
                reset_each_frame, channel_order, gpu_index,
                model_preset="M (13) 最锐"):
        repo = Path(repo_dir).expanduser().resolve()
        V = _load_upstream(repo)

        host = repo / "native" / "bin" / "dlss5nr_host.exe"
        runtime = repo / "runtime"
        shim = runtime / "caller" / "nvngx.dll_comfy.dll"
        for p, what in ((host, "dlss5nr_host.exe（用 native/build_host_mingw.sh 编）"),
                        (runtime / "nvngx_dlssnr.dll", "nvngx_dlssnr.dll（自备）"),
                        (shim, "caller shim nvngx.dll_comfy.dll")):
            if not p.is_file():
                raise Dlss5NRWineError("缺 %s：%s" % (what, p))
        bridge = repo / "native" / "bin" / "dlss5nr_bridge.dll"
        if not bridge.is_file():
            raise Dlss5NRWineError("缺 dlss5nr_bridge.dll：%s" % bridge)

        factor = _resolve_choice(scale, SCALE_CHOICES, "放大档")
        img = image.detach().cpu().float().clamp(0.0, 1.0).numpy()
        if img.ndim != 4 or img.shape[-1] < 3:
            raise Dlss5NRWineError("IMAGE 形状不对：%s" % (tuple(img.shape),))
        img = np.ascontiguousarray(img[..., :3])
        n, in_h, in_w = img.shape[0], img.shape[1], img.shape[2]
        if in_w % 2 or in_h % 2:
            raise Dlss5NRWineError(
                "输入必须是偶数尺寸，当前 %dx%d。H3 出片是偶数，若在中间裁过请对齐到 2 的倍数。"
                % (in_w, in_h))
        out_w, out_h = (_even(in_w * factor), _even(in_h * factor)) if factor != 1.0 else (in_w, in_h)
        # 让上游自己校验倍率合法性，不自己另起一套判断
        perf_quality = V._perf_quality_for_size(in_w, in_h, out_w, out_h)
        if factor > 1.0 and not (runtime / "nvngx_dlss.dll").is_file():
            raise Dlss5NRWineError(
                "倍率 >1x 需要普通 DLSS carrier：把 nvngx_dlss.dll 放进 %s" % runtime)

        env = os.environ.copy()
        env["DISPLAY"] = display
        env.setdefault("XDG_RUNTIME_DIR", "/tmp/xdg")
        env["WINEPREFIX"] = str(Path(prefix).expanduser().resolve())
        env.setdefault("DLSS5NR_DISABLE_OTHER_SINKS", "1")
        env["DLSS5NR_GPU_INDEX"] = str(int(gpu_index))
        env.setdefault("DXVK_ENABLE_NVAPI", "1")
        env.setdefault("DXVK_NVAPI_DRS_NGX_DLSS_NR_OVERRIDE", "on")
        # 只用 builtin(=n) 会让 NGX 起来但 NvAPI 看不到物理显卡 —— 上游注释与我们实测一致
        env.setdefault("WINEDLLOVERRIDES", "d3d12,d3d12core,nvapi64,dxgi=n,b")
        env.setdefault("WINEDEBUG", "-all")
        mp = _resolve_choice(model_preset, MODEL_PRESETS, "模型预设")
        if mp:
            env["DLSS5NR_MODEL_PRESET"] = str(mp)
        else:
            env.pop("DLSS5NR_MODEL_PRESET", None)
        os.makedirs(env["XDG_RUNTIME_DIR"], exist_ok=True)

        log: list[str] = []
        proc = subprocess.Popen([str(Path(wine).expanduser()), str(host), str(runtime)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
        _drain(proc.stderr, log)

        def fail(msg):
            try:
                proc.kill()
            except Exception:
                pass
            tail = "\n".join(log[-25:])
            raise Dlss5NRWineError("%s\n---- host stderr ----\n%s" % (msg, tail))

        try:
            # host 校验 warmup_frames > frame_count 就直接拒收 DNR2 头（退出码 5），
            # 前端拿到的是一句没有上下文的 BrokenPipeError。批量小于 warmup 时
            # （单张图最常见：默认 warmup=8 而 n=1）必须先夹住。
            proc.stdin.write(V.HEADER.pack(
                V.MAGIC_DNR2, in_w, in_h, out_w, out_h,
                min(int(warmup_frames), int(n)), int(n), int(perf_quality),
                0,                              # profile
                int(preset), int(STYLE_CHOICES[style]),
                1 if auto_mask else 0,
                0,                              # ui correction
                float(intensity), float(tone), float(structure), float(skin)))
            proc.stdin.flush()

            guides = V.TemporalGuideGenerator(in_w, in_h)
            motion_bytes = in_w * in_h * 2 * 2
            float_bytes = out_w * out_h * 3 * 4
            chosen = None if channel_order == "auto" else channel_order
            out = np.empty((n, out_h, out_w, 3), dtype=np.float32)

            for i in range(n):
                src = np.ascontiguousarray(img[i])
                u8 = np.ascontiguousarray(np.rint(src * 255.0).astype(np.uint8))
                motion, guide_reset = guides.process(u8)
                if motion.nbytes != motion_bytes:
                    fail("第 %d 帧运动向量 %d 字节，应为 %d" % (i, motion.nbytes, motion_bytes))
                reset = bool(reset_each_frame or guide_reset)
                proc.stdin.write(V.FRAME_HEADER.pack(V.MAGIC_FRM2, i, 1 if reset else 0))
                proc.stdin.write(src.tobytes(order="C"))
                proc.stdin.write(motion.tobytes(order="C"))
                proc.stdin.flush()

                try:
                    magic, idx, ok, count = V.REPLY_HEADER.unpack(
                        V._read_exact(proc.stdout, V.REPLY_HEADER.size))
                except Exception as e:
                    fail("第 %d 帧读回复失败：%s" % (i, e))
                if magic != V.MAGIC_OUT1 or idx != i:
                    fail("第 %d 帧回复错乱：magic=%r index=%d" % (i, magic, idx))
                if not ok:
                    try:
                        ln = struct.unpack("<I", V._read_exact(proc.stdout, 4))[0]
                        detail = V._read_exact(proc.stdout, min(ln, 65535)).decode("utf-8", "replace")
                    except Exception:
                        detail = "(读不到详情)"
                    fail("第 %d 帧 host 报错：%s" % (i, detail))
                if count != out_w * out_h * 3:
                    fail("第 %d 帧返回 %d 个 float，应为 %d" % (i, count, out_w * out_h * 3))
                raw = V._read_exact(proc.stdout, float_bytes)
                frame = np.frombuffer(raw, dtype=np.float32).reshape((out_h, out_w, 3))
                corrected, order = V._channel_choice(frame, src, chosen or "auto")
                if chosen is None:
                    chosen = order
                out[i] = np.clip(corrected, 0.0, 1.0)

            proc.stdin.close()
            proc.stdin = None
            done = V._read_exact(proc.stdout, 4)
            if done != V.MAGIC_END1:
                fail("host 没有发 END1（收到 %r）" % (done,))
        finally:
            if proc.poll() is None:
                # 参考 310.8 运行时在发完 END1 之后可能卡在自己的进程析构里；
                # END1 才是协议完成标志，不拿它的 shutdown 当成败判据。
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        report = "\n".join([
            "DLSS 5 Neural Rendering (feature 18) · Wine 路径",
            "  %d 帧   %dx%d -> %dx%d   %s   PerfQualityValue=%d" % (
                n, in_w, in_h, out_w, out_h, scale, perf_quality),
            "  通道序 %s   warmup=%d   reset_each_frame=%s   auto_mask=%s" % (
                chosen, warmup_frames, reset_each_frame, auto_mask),
            "  DLSS 模型预设 %s%s" % (model_preset,
                "" if mp else "   ← 建议改成 L 或 M，实测高 12 个百分点"),
            "  有效参数 structure=%.2f tone=%.2f%s   （intensity/preset/style 本路径无效）" % (
                structure, tone,
                ("  skin=%.2f" % skin) if auto_mask else "  skin 未生效(auto_mask=off)"),
            "  host=%s" % host,
            "  运行时 nvngx_dlssnr.dll=%d B%s" % (
                (runtime / "nvngx_dlssnr.dll").stat().st_size,
                "   carrier nvngx_dlss.dll=%d B" % (runtime / "nvngx_dlss.dll").stat().st_size
                if (runtime / "nvngx_dlss.dll").is_file() else ""),
            "  host 最后几行：",
        ] + ["    " + l for l in log[-6:]])
        return (torch.from_numpy(out), report)


class DLSS5NRWineStatus:
    """先跑这个：不碰 GPU，只检查文件与环境齐不齐。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "repo_dir": ("STRING", {"default": DEFAULT_REPO, "multiline": False}),
            "wine": ("STRING", {"default": DEFAULT_WINE, "multiline": False}),
            "prefix": ("STRING", {"default": DEFAULT_PFX, "multiline": False}),
            "display": ("STRING", {"default": DEFAULT_DISPLAY, "multiline": False}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "inspect"
    CATEGORY = "image/DLSS 5 NR (Wine)"

    def inspect(self, repo_dir, wine, prefix, display):
        repo = Path(repo_dir).expanduser().resolve()
        lines = ["DLSS 5 NR (Wine) 自检", "  仓库: %s" % repo]
        items = [
            ("host",    repo / "native" / "bin" / "dlss5nr_host.exe"),
            ("bridge",  repo / "native" / "bin" / "dlss5nr_bridge.dll"),
            ("shim",    repo / "runtime" / "caller" / "nvngx.dll_comfy.dll"),
            ("dlssnr",  repo / "runtime" / "nvngx_dlssnr.dll"),
            ("dlss SR", repo / "runtime" / "nvngx_dlss.dll"),
            ("wine",    Path(wine)),
            ("prefix",  Path(prefix)),
        ]
        for name, p in items:
            if p.exists():
                sz = p.stat().st_size if p.is_file() else 0
                lines.append("  ✓ %-8s %s%s" % (name, p, ("  %d B" % sz) if sz else ""))
            else:
                lines.append("  ✗ %-8s 缺失: %s" % (name, p))
        lines.append("  DISPLAY=%s（进程内 %s）" % (display, os.environ.get("DISPLAY", "<未设>")))
        try:
            import cv2  # noqa: F401
            lines.append("  ✓ cv2 %s（光流运动向量可用）" % cv2.__version__)
        except Exception:
            lines.append("  ⚠️ 无 cv2 —— 运动向量退化为零、每帧重置，视频会丢时域收益")
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                                "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
            lines.append("  GPU: %s" % (r.stdout.strip() or "查不到"))
        except Exception as e:
            lines.append("  GPU: 查不到 (%s)" % e)
        return ("\n".join(lines),)


NODE_CLASS_MAPPINGS = {
    "DLSS5NRWineUpscale": DLSS5NRWineUpscale,
    "DLSS5NRWineStatus": DLSS5NRWineStatus,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DLSS5NRWineUpscale": "DLSS 5 神经渲染 · Wine (Linux)",
    "DLSS5NRWineStatus": "DLSS 5 NR · 自检 (Wine)",
}
