#!/usr/bin/env python3
"""按分镜表一条条生成 H3 视频，拼成一条长片。只用标准库 + 本机 ffmpeg。

  python3 scripts/storyboard.py examples/storyboard-demo.json --dry-run     # 只算预算，不提交
  python3 scripts/storyboard.py examples/storyboard-demo.json               # 真生成
  python3 scripts/storyboard.py my.json --max-percent 30                    # 预计超过 30% 周额度就拒绝

分镜表（JSON）：
  {
    "defaults": {"model": "MiniMax-H3-Max-Turbo", "resolution": "480P", "ratio": "9:16", "duration": 10},
    "out_dir": "out/demo",
    "shots": [
      {"id": "s01", "prompt": "……", "duration": 10},
      {"id": "s02", "prompt": "接上一个镜头……", "chain": true, "duration": 8},   # 用上一条的最后一帧当首帧
      {"id": "s03", "prompt": "图1是主角……", "ref_images": ["me.png"], "duration": 10}   # 带参考图，每条都带可锁住人物
    ]
  }

行为：
- 顺序执行（续拍要等上一条出来）；已经生成过的镜头（输出文件在）会跳过，所以中断后重跑不会重复扣额度。
- chain=true 的镜头用上一条的末帧做首帧；画幅由那张图决定。
- 开跑前按 RATE 表估算占多少周额度，超过 --max-percent（默认 40）或超过当前剩余就拒绝，除非 --force。
- 最后用 ffmpeg 拼成 final.mp4（先试直接拷贝，不行再重编码）。
"""
import argparse, json, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h3  # noqa: E402


def last_frame(video, jpg):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.1", "-i", video, "-frames:v", "1", "-q:v", "2", jpg], check=True)


def concat(clips, out):
    lst = out + ".list.txt"
    with open(lst, "w") as f:
        for c in clips:
            f.write("file '" + os.path.abspath(c).replace("'", "'\\''") + "'\n")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    if r.returncode != 0:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", out], check=True)
    os.remove(lst)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("storyboard")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-percent", type=float, default=40.0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    sb = json.load(open(a.storyboard))
    d = {"model": "MiniMax-H3-Max-Turbo", "resolution": "480P", "ratio": "9:16", "duration": 10}
    d.update(sb.get("defaults", {}))
    out_dir = sb.get("out_dir", "out/storyboard")
    os.makedirs(out_dir, exist_ok=True)
    shots = []
    for i, s in enumerate(sb["shots"], 1):
        x = {**d, **s}
        x.setdefault("id", f"s{i:02d}")
        x["path"] = os.path.join(out_dir, x["id"] + ".mp4")
        shots.append(x)

    total_s = sum(s["duration"] for s in shots if not os.path.exists(s["path"]))
    est = 0.0
    unknown = []
    for s in shots:
        if os.path.exists(s["path"]):
            continue
        e = h3.estimate_percent(s["model"], s["resolution"], s["duration"])
        if e is None:
            unknown.append(s["id"])
        else:
            est += e
    q = h3.quota()
    print(f"待生成 {total_s} 秒，预计约占周额度 {est:.1f}%（当前周额度剩 {q.get('weekly')}%）"
          + (f"；这些镜头的档位没有估算值：{unknown}" if unknown else ""))
    if not a.force and (est > a.max_percent or (q.get("weekly") is not None and est > q["weekly"])):
        sys.exit(f"预计 {est:.1f}% 超过上限 {a.max_percent}% 或超过剩余，停。要继续请加 --force 或调高 --max-percent。")
    if a.dry_run:
        return

    log = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "quota_before": q, "shots": []}
    prev = None
    for s in shots:
        if os.path.exists(s["path"]):
            print(f"[{s['id']}] 已有，跳过")
            prev = s["path"]
            continue
        first = s.get("first_frame")
        if s.get("chain"):
            if not prev:
                sys.exit(f"[{s['id']}] chain=true 但没有上一条")
            first = os.path.join(out_dir, s["id"] + ".first.jpg")
            last_frame(prev, first)
        content = h3.build_content(s["prompt"], first, s.get("last_frame"), s.get("ref_images", []),
                                   s.get("ref_videos", []), s.get("ref_audios", []))
        print(f"[{s['id']}] {s['model']} {s['resolution']} {s['duration']}s" + ("（续拍）" if s.get("chain") else ""))
        tid = h3.submit(content, s["model"], s["duration"], s["resolution"], s.get("ratio"))
        url = h3.wait(tid)
        h3.download(url, s["path"])
        log["shots"].append({"id": s["id"], "task_id": tid, "path": s["path"]})
        prev = s["path"]

    final = os.path.join(out_dir, "final.mp4")
    concat([s["path"] for s in shots], final)
    time.sleep(30)  # 用量统计滞后约 20 秒
    log["quota_after"] = h3.quota()
    json.dump(log, open(os.path.join(out_dir, "run.json"), "w"), ensure_ascii=False, indent=2)
    print("已拼接:", final)
    print(f"周额度 {q.get('weekly')}% → {log['quota_after'].get('weekly')}%")


if __name__ == "__main__":
    main()
