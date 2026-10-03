#!/usr/bin/env python3
"""给控制台截图,用来确认它真的长成该有的样子。

    python3 tools/screenshot_ui.py                    # 存到 /tmp/hg-ui-shots
    python3 tools/screenshot_ui.py --out docs/ui      # 存到指定目录

为什么需要一个工具而不是"自己打开看看"
----------------------------------------
改前端的时候,读源码能确认"元素在不在",不能确认"它长什么样"。这次重写布局时,
一个**重复的 id**(侧栏的状态点叫 `f-model`,主区的模型下拉也叫 `f-model`)让
`document.querySelector('#f-model')` 拿到了侧栏那个 span,于是模型选项被塞进了
侧栏、主下拉是空的。源码里两处都写着 `id="f-model"`,各自都对;只有渲染出来才
看得出坏了。

同一个工具还顺带做了另外两件读源码做不到的事:
  * 收集 console 错误(JS 抛异常时页面通常还是"看起来正常"的)
  * 断言关键元素真的存在且类型正确(模型下拉必须是 SELECT,不是 SPAN)

需要 playwright 和 chromium;没有就跳过并说明,不让它变成跑测试的前置依赖。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: 零成本预设,用来起一次演示运行。按 label 选,因为下拉的 value 是表单里的键,
#: 那个键会随文案调整而变,而 label 正是人在界面上看到的那一个。
MOCK_PRESET = "mock(零成本冒烟,不调模型)"

#: 每个视图要截什么,以及要断言什么。断言写成 `选择器 -> 期望的标签名`,
#: 因为这次踩的就是"元素在,但类型不对"。
#: (视图 key, 名称, 抽屉要不要打开, 断言)。
#:
#: 这张表以前指向 `button[data-v="mth"]`(一个已经删掉的"方法一览"视图)和一个
#: `#btn-go`(早已改名),所以工具跑到第二屏就超时 —— 一个过时的检查比没有检查更糟,
#: 因为它看起来还在工作。视图 key 现在与顶部 tab 一一对应,并且由脚本**自己发现**
#: 有哪些 tab,少一个多一个都会被报出来,而不是悄悄跳过。
VIEWS = [
    ("exp", "总览", False, [("#home-card", "DIV"), ("#runs-home", "TABLE")]),
    ("exp", "总览(实验设置)", True,
     [("#f-harness", "SELECT"), ("#f-model", "SELECT"), ("#f-dataset", "SELECT"),
      ("#f-method", "SELECT"), ("#f-improver", "SELECT"), ("#f-rounds", "INPUT"),
      ("#setup", "SECTION")]),
    ("hist", "实验", False, [("#runs", "TABLE")]),
    ("graph", "版本图", False, [("#graph", "DIV")]),
    ("set", "设置", False, [("#svc-dir", "SELECT")]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8771")
    ap.add_argument("--out", default="/tmp/hg-ui-shots")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=960)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright 未安装;跳过截图(它只是确认外观,不是平台的一部分)",
              file=sys.stderr)
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:                            # noqa: BLE001
            print(f"启动 chromium 失败({exc});跳过", file=sys.stderr)
            return 0

        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text[:200])
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {str(e)[:200]}"))

        try:
            page.goto(args.url, wait_until="networkidle", timeout=30000)
        except Exception as exc:                            # noqa: BLE001
            print(f"打不开 {args.url}:{exc}", file=sys.stderr)
            print("先启动控制台: python3 tools/serve_ui.py", file=sys.stderr)
            browser.close()
            return 1

        page.wait_for_timeout(1200)

        # 先核对"顶部 tab 与脚本知道的视图"是否一致:多出来或少了都要报。
        tabs = page.eval_on_selector_all(".tabs button", "els => els.map(e => e.dataset.v)")
        known = {v[0] for v in VIEWS}
        if set(tabs) != known:
            problems.append(f"顶部 tab 与脚本的视图列表不一致:tab={tabs} 脚本={sorted(known)}")

        for key, label, open_drawer, assertions in VIEWS:
            page.click(f'.tabs button[data-v="{key}"]')
            collapsed = page.eval_on_selector(
                "#setup", "e => e.classList.contains('collapsed')")
            if open_drawer and collapsed:
                page.click("#btn-setup")
            elif not open_drawer and not collapsed:
                page.click("#btn-setup")
            page.wait_for_timeout(1200 if key in ("hist", "set", "graph") else 800)
            name = ("setup" if open_drawer else key)
            page.screenshot(path=str(out / f"{name}.png"))

            for selector, want in assertions:
                got = page.evaluate(
                    "sel => { const e = document.querySelector(sel);"
                    "         return e ? e.tagName : null; }", selector)
                if got is None:
                    problems.append(f"{label}:找不到 {selector}")
                elif got != want:
                    problems.append(f"{label}:{selector} 是 {got},期望 {want}")

        # 运行日志面板。这是最需要"看一眼"的一屏:它以前是一个装着终端文本的黑框,
        # 现在按 driver 写的事件排版。事件有没有真的排成可读的行,读源码看不出来,
        # 而且它只在有一个 run 在跑的时候才出现 —— 所以这里真的起一次(mock 预设,零成本)。
        page.click('.tabs button[data-v="exp"]'); page.wait_for_timeout(300)
        if page.eval_on_selector("#setup", "e => e.classList.contains('collapsed')"):
            page.click("#btn-setup"); page.wait_for_timeout(700)

        # 「改进方法」下拉里只能有已发表的方法。基线和平台自带的零件(契约自检、
        # 共享编辑器)不该出现在这里 —— 它们出现在同一列会让人以为可以互相比较。
        options = page.eval_on_selector_all(
            "#f-method option", "els => els.map(e => e.value)")
        for disallowed in ("echo_base", "llm_improver"):
            if disallowed in options:
                problems.append(f"改进方法下拉里还有 {disallowed},它不是已发表方法")
        if "noop" not in options:
            problems.append("改进方法下拉里没有基线选项")
        if not any(o not in ("noop", "") for o in options):
            problems.append(f"改进方法下拉里没有任何已发表方法:{options}")

        page.select_option("#f-method", "noop")     # 零模型调用,跑得快
        page.select_option("#f-model", label=MOCK_PRESET)
        page.fill("#f-runid", "shot-log")
        page.click("#btn-train")
        try:
            page.wait_for_selector(".ev .ev-row", timeout=60000)
        except Exception:                                    # noqa: BLE001
            problems.append("运行日志:等不到任何事件行(面板可能还是空的)")

        # 「原始输出」展开后必须留着。面板每 1.5 秒重画一次,而 innerHTML 替换会销毁并
        # 重建 <details> —— 曾经的表现是一点开就自己关掉。两条路径都要测,因为它们是
        # 两个不同的机制,只测一条会漏掉另一条:
        #
        #   1. 没有新事件时不重画(短路)。旧的实现无论如何都重画。
        #   2. 有新事件时必须重画,重画就会重建 <details>;这一条靠重画前记下状态、
        #      重画后还原。短路救不了它,而运行中一直在发生的就是它。
        try:
            page.click(".ev-raw summary")
            page.wait_for_timeout(4200)                       # 过两个轮询周期
            if not page.evaluate(
                    "() => !!document.querySelector('.ev-raw details[open]')"):
                problems.append("原始输出:展开后又被自动关掉了(空闲时面板仍在重画)")

            # 路径 2:直接调 renderEvents 并塞一个多出来的事件,强制一次真正的重画。
            survived = page.evaluate("""async () => {
              const id = (document.querySelector('#prog-title').textContent||'')
                           .replace('实验 ','').trim();
              const s = await (await fetch('/api/job/'+id)).json();
              const ev = (s.events||[]).concat([{seq:999999, kind:'note',
                         level:'info', message:'forced re-render probe'}]);
              renderEvents(ev, s.log, true);
              return !!document.querySelector('.ev-raw details[open]');
            }""")
            # 先确认那次重画真的发生了,否则下一行断言是空的
            if not page.evaluate(
                    "() => document.body.innerText.includes('forced re-render probe')"):
                problems.append("原始输出:强制重画没生效,路径 2 的断言是空的")
            elif not survived:
                problems.append("原始输出:面板重画时把展开状态丢了")
        except Exception as exc:                              # noqa: BLE001
            problems.append(f"原始输出:展开检查失败({exc})")

        # 等到跑完再截:面板在结束后不会消失,而只有跑完才会出现 artifact 行。
        try:
            page.wait_for_function(
                "() => (document.querySelector('#prog-state')?.textContent || '')"
                ".includes('已结束')", timeout=120000)
        except Exception:                                    # noqa: BLE001
            problems.append("运行日志:等不到运行结束")
        page.wait_for_timeout(800)
        page.screenshot(path=str(out / "live.png"))

        kinds = page.eval_on_selector_all(
            ".ev .ev-row .ev-k", "els => els.map(e => e.textContent.trim())")
        if not any(k.startswith("round") for k in kinds):
            problems.append(f"运行日志:没有 round 行,只有 {kinds[:6]}")
        head = page.eval_on_selector_all(
            ".ev-head b", "els => els.map(e => e.textContent.trim())")
        if "sandbox" not in head:
            problems.append(f"运行日志:表头缺字段,只有 {head}")
        if page.evaluate("() => !!document.querySelector('pre.log')"):
            problems.append("运行日志:还在用那个黑色终端框(pre.log)")

        # 折叠态也要看一眼:侧栏收起来时主区不该被压坏
        page.click(".collapse"); page.wait_for_timeout(600)
        page.screenshot(path=str(out / "collapsed.png"))
        browser.close()

    print(f"截图写到 {out}")
    for p in sorted(out.glob("*.png")):
        print(f"  {p.name}")

    if console_errors:
        print(f"\n控制台有 {len(console_errors)} 条错误:")
        for e in console_errors[:6]:
            print("  ", e)
        problems.extend(console_errors[:3])

    if problems:
        print("\n\033[31m有问题:\033[0m")
        for p in problems:
            print("  ✗", p)
        return 1
    print("\n\033[32m四个视图都渲染正常,元素类型也对。\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
