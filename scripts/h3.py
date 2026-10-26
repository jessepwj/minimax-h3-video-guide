#!/usr/bin/env python3
"""MiniMax H3 视频生成的最小命令行：查套餐用量、提交任务、等结果、下载。只用标准库。

key 从环境变量 MINIMAX_API_KEY 读（订阅 key 以 sk-cp- 开头），不写进任何文件。

  python3 scripts/h3.py quota
  python3 scripts/h3.py gen --prompt "描述" --duration 5 --resolution 480P --ratio 9:16
  python3 scripts/h3.py gen --prompt "只写变化" --first-frame photo.jpg --duration 5
  python3 scripts/h3.py gen --prompt "图1是主角……" --ref-image me.png --ref-image room.png --duration 10 --ratio 9:16
  python3 scripts/h3.py gen ... --measure      # 生成后等 30 秒再读一次用量，打印这条用了多少
"""
import argparse, base64, json, mimetypes, os, sys, time, urllib.error, urllib.request

BASE = os.environ.get("MINIMAX_BASE", "https://api.minimax.cn")
QUOTA_URL = os.environ.get("MINIMAX_QUOTA_URL", "https://www.minimax.cn/v1/token_plan/remains")

# 每秒视频大约吃掉多少「周额度」百分比。前三行是实测，后面按官方按量价目折算（100% 周额度 ≈ ¥110）。
# 用来估预算，不是账单；整数百分比有 ±1 的误差。
RATE = {
    ("MiniMax-H3-Max-Turbo", "480P"): 0.15,
    ("MiniMax-H3-Max", "480P"): 0.30,
    ("MiniMax-H3", "768P"): 0.45,
    ("MiniMax-H3-Max", "768P"): 0.45,
    ("MiniMax-H3-Max-Turbo", "768P"): 0.24,
    ("MiniMax-H3-Lite", "480P"): 0.15,
    ("MiniMax-H3-Lite", "768P"): 0.24,
    ("MiniMax-H3-Highspeed", "768P"): 0.36,
    ("MiniMax-H3", "2K"): 0.73,
}


def key():
    k = os.environ.get("MINIMAX_API_KEY")
    if not k:
        sys.exit("缺环境变量 MINIMAX_API_KEY（macOS/Linux: export MINIMAX_API_KEY=你的key）")
    return k


def call(method, url, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:2000]}


def quota():
    """返回 {'interval': 5小时窗口剩余%, 'weekly': 周窗口剩余%, 'weekly_reset_s': 距周重置秒数}。视频只看 weekly。"""
    r = call("GET", QUOTA_URL)
    rows = r.get("model_remains") or []
    if not rows:
        return {"error": r}
    row = rows[0]
    return {"interval": row.get("current_interval_remaining_percent"),
            "weekly": row.get("current_weekly_remaining_percent"),
            "weekly_reset_s": (row.get("weekly_remains_time") or 0) // 1000}


def estimate_percent(model, resolution, seconds):
    rate = RATE.get((model, resolution))
    return None if rate is None else round(rate * seconds, 1)


def to_url(path_or_url):
    """本地文件转 data URI（请求体总共不能超过 64MB，大文件请先上传成公网链接）；网址原样返回。"""
    if path_or_url.startswith(("http://", "https://", "data:", "mm_file://")):
        return path_or_url
    mime = mimetypes.guess_type(path_or_url)[0] or "image/jpeg"
    with open(path_or_url, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def build_content(prompt, first_frame=None, last_frame=None, ref_images=(), ref_videos=(), ref_audios=()):
    c = [{"type": "text", "text": prompt}]
    if first_frame:
        c.append({"type": "image_url", "image_url": {"url": to_url(first_frame)}, "role": "first_frame"})
    if last_frame:
        c.append({"type": "image_url", "image_url": {"url": to_url(last_frame)}, "role": "last_frame"})
    for p in ref_images:
        c.append({"type": "image_url", "image_url": {"url": to_url(p)}, "role": "reference_image"})
    for p in ref_videos:
        c.append({"type": "video_url", "video_url": {"url": to_url(p)}, "role": "reference_video"})
    for p in ref_audios:
        c.append({"type": "audio_url", "audio_url": {"url": to_url(p)}, "role": "reference_audio"})
    return c


def submit(content, model, duration, resolution, ratio=None):
    body = {"model": model, "content": content, "duration": duration, "resolution": resolution}
    has_frame = any(i.get("role") in ("first_frame", "last_frame") for i in content)
    if ratio and not has_frame:  # 首/尾帧模式画幅由图片决定，传了也会被忽略
        body["ratio"] = ratio
    r = call("POST", BASE + "/v2/video_generation", body)
    tid = r.get("task_id")
    if not tid:
        sys.exit("提交失败：" + json.dumps(r, ensure_ascii=False)[:800])
    return tid


def wait(task_id, poll=10, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(poll)
        q = call("GET", f"{BASE}/v2/query/video_generation/{task_id}")
        t = q.get("task") or q
        st = t.get("status")
        print(f"  {int(time.time() - t0)}s {st}", flush=True)
        if st == "succeeded":
            return t["content"]["url"]
        if st in ("failed", "cancelled") or q.get("_http_error"):
            sys.exit("任务没成功：" + json.dumps(q, ensure_ascii=False)[:800])
    sys.exit("等超时了，task_id=" + task_id)


def download(url, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as f:
        f.write(r.read())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("quota")
    g = sub.add_parser("gen")
    g.add_argument("--prompt", required=True)
    g.add_argument("--model", default="MiniMax-H3-Max-Turbo")
    g.add_argument("--duration", type=int, default=5, help="整数秒，4~15（H3-Max 系列最少 5）")
    g.add_argument("--resolution", default="480P", help="480P / 768P / 2K，可选档位随模型而异")
    g.add_argument("--ratio", default="9:16", help="文生/参考模式用；首尾帧模式由图片决定")
    g.add_argument("--first-frame")
    g.add_argument("--last-frame")
    g.add_argument("--ref-image", action="append", default=[])
    g.add_argument("--ref-video", action="append", default=[])
    g.add_argument("--ref-audio", action="append", default=[])
    g.add_argument("--out")
    g.add_argument("--measure", action="store_true", help="生成后等 30 秒读一次用量并打印差值（用量统计滞后约 20 秒）")
    a = p.parse_args()

    if a.cmd == "quota":
        print(json.dumps(quota(), ensure_ascii=False, indent=2))
        return
    before = quota()
    est = estimate_percent(a.model, a.resolution, a.duration)
    print(f"提交前周额度剩 {before.get('weekly')}%，这条预计约占 {est}%")
    tid = submit(build_content(a.prompt, a.first_frame, a.last_frame, a.ref_image, a.ref_video, a.ref_audio),
                 a.model, a.duration, a.resolution, a.ratio)
    print("task_id:", tid)
    url = wait(tid)
    out = a.out or f"out/{tid}.mp4"
    download(url, out)
    print("已保存:", out)
    if a.measure:
        time.sleep(30)
        after = quota()
        print(f"用量：周额度 {before.get('weekly')}% → {after.get('weekly')}%")


if __name__ == "__main__":
    main()
