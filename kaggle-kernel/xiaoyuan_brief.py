#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小源 Kaggle kernel：一次跑完当期投研简报。

流程（每次 kernel run 都是全新会话，跑完即关，没有常驻服务）：
  1. 下载 Qwen3-8B 去审核版 GGUF（Q8_0，约 8.7GB）与 llama.cpp 发布版
  2. 在会话内起 llama-server（本地 OpenAI 兼容接口，127.0.0.1:8080）
  3. 从 GitHub 拉最新三步管道脚本，跑新闻扫描 → 本地模型选标的 → 拉行情入库
  4. 简报写进 Supabase briefs 表（投资策略部空间可读）

场次按北京时间推断：12:00 前跑是早盘（A股+港股），之后是晚盘（美股）。
密钥：SUPABASE_ANON_KEY 存在 Kaggle Secrets 里（notebook 界面 Add-ons → Secrets），
不要写进代码。GPU 约 10-20 分钟/次，一周约 3 小时（额度 30 小时/周）。
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("xiaoyuan-kernel")

WORK = Path("/kaggle/working")
MODELS_DIR = WORK / "models"
SCRIPTS_DIR = WORK / "scripts"
OUT_DIR = WORK / "out"
LLAMA_PORT = 8080
SUPABASE_URL = "https://jneooqtzquqmvyphigfm.supabase.co"
CST = timezone(timedelta(hours=8))

# 部署笔记（qwen3-8b-abliterated-deploy，2026-11-11 验证通过）核实的准确值：
# 模型 mradermacher/Qwen3-8B-abliterated-GGUF 的 Q8_0.gguf；
# llama.cpp b12273 发布包是 .tar.gz（不是 .zip），来自 ggml-org（项目已从 ggerganov 迁移），
# 必须同时下主包和 cudart 包，后者提供 CUDA 运行时库。
MODEL_REPO = "mradermacher/Qwen3-8B-abliterated-GGUF"
MODEL_FILE = "Qwen3-8B-abliterated.Q8_0.gguf"
MODEL_URL = (
    "https://huggingface.co/mradermacher/Qwen3-8B-abliterated-GGUF/resolve/main/"
    "Qwen3-8B-abliterated.Q8_0.gguf"
)
LLAMA_BASE = "https://github.com/ggml-org/llama.cpp/releases/download/b12273"
LLAMA_TARBALLS = [
    "llama-b12273-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "cudart-llama-b12273-bin-ubuntu-cuda-12.8-x64.tar.gz",
]
# 上下文：部署笔记里短问答用 -c 4096；简报提示词约 4000+ token、输出约 2500，
# 合计超 4096，必须放宽到 8192（KV 约 1.2GB，8.7GB 模型 + KV 仍在 15GB 显存内）。
SERVER_FLAGS = ["-c", "8192", "--n-gpu-layers", "99"]

GITHUB_RAW = "https://raw.githubusercontent.com/mashaozhi/ai-studio/main/scripts"
PIPELINE_SCRIPTS = ["news_scan.py", "pick_candidates.py", "fetch_quotes.py"]


def run_step(cmd, env, timeout, label):
    """跑一步管道脚本（日志直接打到 kernel 日志里），失败抛错让整轮失败。"""
    logger.info("跑 %s：%s", label, " ".join(cmd))
    proc = subprocess.run(cmd, env=env, cwd=str(WORK), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{label} 失败（退出码 {proc.returncode}）")


def ensure_deps():
    """装管道需要的包（Kaggle 环境里 requests 已有，feedparser/yfinance 未必有）。"""
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "feedparser", "yfinance"],
        check=True,
    )


def download_model():
    """用 wget -c 下载模型（部署笔记的原办法，支持断点续传）。"""
    path = MODELS_DIR / MODEL_FILE
    if path.exists() and path.stat().st_size > 1_000_000_000:
        logger.info("模型已存在：%s", path)
        return path
    logger.info("下载模型 %s …", MODEL_URL)
    subprocess.run(["wget", "-c", MODEL_URL, "-O", str(path)], check=True)
    return path


def setup_llama():
    """下载 llama.cpp 两个 .tar.gz（主包 + cudart 包），解压后找出 llama-server。

    返回 (server 二进制路径, 运行时库目录列表)，供启动时设 LD_LIBRARY_PATH。
    """
    llama_dir = WORK / "llama"
    server = next(llama_dir.rglob("llama-server"), None) if llama_dir.exists() else None
    if server:
        return server, lib_dirs(llama_dir, server)
    llama_dir.mkdir(parents=True, exist_ok=True)
    for name in LLAMA_TARBALLS:
        logger.info("下载 %s …", name)
        subprocess.run(
            ["wget", "-c", f"{LLAMA_BASE}/{name}", "-O", str(WORK / name)], check=True
        )
        subprocess.run(["tar", "xzf", str(WORK / name), "-C", str(llama_dir)], check=True)
    server = next(llama_dir.rglob("llama-server"), None)
    if server is None:
        raise RuntimeError("解压后找不到 llama-server")
    server.chmod(0o755)
    return server, lib_dirs(llama_dir, server)


def lib_dirs(llama_dir, server):
    """llama-server 运行时要找的库目录：主包目录 + cudart 包目录 + 系统 CUDA 库。"""
    dirs = [str(server.parent)]
    dirs += [
        str(p)
        for p in llama_dir.iterdir()
        if p.is_dir() and p.name.startswith("cudart")
    ]
    dirs.append("/usr/local/cuda/lib64")
    return dirs


def start_server(server_bin, lib_dirs_list, model_path):
    log = open(WORK / "llama-server.log", "wb")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(
        lib_dirs_list + [env.get("LD_LIBRARY_PATH", "")]
    ).strip(":")
    cmd = [
        str(server_bin), "-m", str(model_path),
        "--port", str(LLAMA_PORT), "--host", "127.0.0.1",
    ] + SERVER_FLAGS
    logger.info("启动 llama-server：%s", " ".join(cmd))
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)


def wait_ready(timeout=300):
    """轮询 /health，等服务就绪（部署笔记的原办法：返回 ok 即就绪）。"""
    url = f"http://127.0.0.1:{LLAMA_PORT}/health"
    deadline = time.time() + timeout
    tries = 0
    while time.time() < deadline:
        tries += 1
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                if "ok" in resp.read().decode("utf-8", "ignore"):
                    logger.info("模型服务就绪（第 %d 次检查，约 %d 秒）", tries, tries * 5)
                    return
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError("llama-server 5 分钟内未就绪，请看 llama-server.log")


def fetch_scripts():
    """从 GitHub 拉最新三步管道脚本（保证 kernel 永远用仓库最新版）。"""
    for name in PIPELINE_SCRIPTS:
        dest = SCRIPTS_DIR / name
        logger.info("拉脚本 %s", name)
        urllib.request.urlretrieve(f"{GITHUB_RAW}/{name}", dest)
        if not dest.exists() or dest.stat().st_size < 100:
            raise RuntimeError(f"拉脚本失败：{name}")


def get_supabase_key():
    try:
        from kaggle_secrets import UserSecretsClient

        key = UserSecretsClient().get_secret("SUPABASE_ANON_KEY")
        if key:
            return key
    except Exception as exc:  # noqa: BLE001 - 读不到就试环境变量
        logger.warning("Kaggle Secrets 读不到 SUPABASE_ANON_KEY：%s", exc)
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if key:
        return key
    raise RuntimeError(
        "缺少 SUPABASE_ANON_KEY：请在 Kaggle notebook 界面 Add-ons → Secrets 里添加"
    )


def session_for_now():
    now = datetime.now(CST)
    return ("morning" if now.hour < 12 else "evening"), now


def main():
    session, now = session_for_now()
    date_str = now.strftime("%Y-%m-%d")
    brief_stamp = now.strftime("%Y%m%d")
    logger.info("本次场次：%s（北京时间 %s）", session, now.strftime("%Y-%m-%d %H:%M"))
    supabase_key = get_supabase_key()
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ensure_deps()
    model_path = download_model()
    server_bin, lib_dirs_list = setup_llama()
    server = start_server(server_bin, lib_dirs_list, model_path)
    try:
        wait_ready()
        fetch_scripts()
        env = dict(os.environ)
        env.update(
            {
                "SUPABASE_URL": SUPABASE_URL,
                "SUPABASE_ANON_KEY": supabase_key,
                "LLM_BASE_URL": f"http://127.0.0.1:{LLAMA_PORT}/v1",
                "LLM_API_KEY": "local",
            }
        )
        market = "CN,HK" if session == "morning" else "US"
        run_step(
            ["python3", str(SCRIPTS_DIR / "news_scan.py"), "--session", session,
             "--out", str(OUT_DIR / "news_scan.json")],
            env, 600, "新闻扫描",
        )
        run_step(
            ["python3", str(SCRIPTS_DIR / "pick_candidates.py"), "--session", session,
             "--news", str(OUT_DIR / "news_scan.json"), "--out-dir", str(OUT_DIR)],
            env, 900, "选标的",
        )
        brief_json = OUT_DIR / f"brief_{brief_stamp}_{session}.json"
        run_step(
            ["python3", str(SCRIPTS_DIR / "fetch_quotes.py"), "--market", market,
             "--symbols-json", str(brief_json)],
            env, 600, "拉行情",
        )
        data = json.loads(brief_json.read_text(encoding="utf-8"))
        logger.info(
            "简报完成（%s %s）：候选 %d 只，自选股评估 %d 只，模型 %s",
            date_str, session,
            len(data.get("candidates") or []),
            len(data.get("watchlist_evals") or []),
            data.get("model"),
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
    logger.info("kernel 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
