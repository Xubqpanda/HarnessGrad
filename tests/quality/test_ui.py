"""单页控制台的样式与脚本必须自洽 —— 这些是浏览器里没人守着的退化。

`tools/serve_ui.py` 把 HTML、CSS、JS 放在一个文件里,好处是没有构建步骤,代价是没有
任何东西替你检查这三者还对得上。已经真实发生过三次:

  * `view('graph')` 调用 `loadGraph()`,而那个函数从来没写过。后端 `/api/graph` 和
    一整块泳道图 HTML 都在,于是「版本图」点开是空白的,而且只在浏览器控制台里
    留一行 `loadGraph is not defined`。静态检查抓不到"调用了不存在的函数",但
    抓得到"HTML 里的容器 id 没有任何 JS 引用它"。
  * CSS 里有一条全局 `svg{width:100%;height:auto}`,把图例中 12×12 的圆点撑成 93×93、
    把泳道图按比例拉高。全局元素选择器在这里没有存在的理由。
  * 侧栏 logo 的规则写作裸 `.brand{...padding:16px 18px;min-height:60px}`,于是
    `<span class="pill brand">` 也被套上 —— 一个 11.5px 的徽标变成 60px 高,把标题
    行顶成两倍高,两个并排面板的标题因此错位。这是扁平类名空间下的复用碰撞:
    一个声明了盒模型的**块级类**,又出现在别的组件后面当修饰类。

最后一条是这里的主要不变量:`a {}` 里声明盒模型的类,不能被别人当修饰类复用。
`on/off/ok/bad/dim` 这类只改颜色的状态类是允许的 —— 它们本来就是独立工具类。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

#: The console is three files now (`tools/ui/`), not one Python string.
#:
#: Every check below was written against "the page as one blob", and that is still what
#: they want: `SOURCE` is the page **as the browser sees it**, assembled from the three
#: files, so the existing regexes keep working. A test that needs one layer asks for it by
#: name (`HTML` / `CSS` / `JS`).
#:
#: Why the split happened: as a Python raw string the JavaScript could not be syntax
#: checked at all, an edit's failure modes were Python's (a stray `"""`, a kept `\\`),
#: and six UI breakages in one day were nearly all that shape.
UI = ROOT / "tools" / "ui"
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "app.css").read_text(encoding="utf-8")
JS = (UI / "app.js").read_text(encoding="utf-8")
SOURCE = (HTML + "\n<style>\n" + CSS + "\n</style>\n<script>\n" + JS + "\n</script>")

# 会改变布局的声明。只改颜色/透明度的状态类不算 —— 它们被复用是正常的。
BOX_PROPS = ("padding", "margin", "min-height", "max-height", "height", "width",
             "display", "position", "grid", "flex", "gap")


def _css() -> str:
    return SOURCE.split("<style>", 1)[1].split("</style>", 1)[0]


def _rules(css: str) -> list[tuple[str, str]]:
    """(选择器, 声明体) —— 跳过注释,按 `}` 切。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for chunk in css.split("}"):
        if "{" not in chunk:
            continue
        sel, _, body = chunk.partition("{")
        out.append((sel.strip(), body))
    return out


def _bare_box_classes(css: str) -> set[str]:
    """选择器**恰好是一个裸类**、且声明了盒模型的类名。"""
    found = set()
    for sel, body in _rules(css):
        m = re.fullmatch(r"\.([A-Za-z][\w-]*)", sel)
        if not m:
            continue
        if any(re.search(rf"(?:^|;|\s){p}[\s:-]", body) for p in BOX_PROPS):
            found.add(m.group(1))
    return found


def _class_lists() -> list[list[str]]:
    """HTML 里每个 class 属性的类列表。模板字符串里的 `${...}` 是运行时才知道的,
    当成占位丢掉 —— 这里检查的是写死的结构。"""
    out = []
    for raw in re.findall(r'class="([^"]*)"', SOURCE):
        names = [c for c in raw.split() if c and "${" not in c and "'" not in c]
        if names:
            out.append(names)
    return out


def _blocks_and_modifiers() -> tuple[set[str], set[str]]:
    """块级类 = 出现在某个类列表首位的类;修饰类 = 出现在别人后面的类。"""
    blocks, modifiers = set(), set()
    for names in _class_lists():
        blocks.add(names[0])
        modifiers.update(names[1:])
    return blocks, modifiers


# ---------------------------------------------------------------- 类名复用碰撞

def test_box_model_block_classes_are_never_reused_as_modifiers():
    """一个声明了盒模型的块级类,不能再被挂到别的组件后面当修饰类。

    `.brand` 就是踩了这一条:它作为侧栏 logo 的块级类声明了 padding/min-height,
    又被 `<span class="pill brand">` 复用,于是徽标被撑成 60px 高。
    """
    css = _css()
    risky = _bare_box_classes(css)
    blocks, modifiers = _blocks_and_modifiers()
    collisions = sorted(risky & blocks & modifiers)
    assert not collisions, (
        "这些类既作为块级类声明了盒模型,又被当成修饰类复用 —— 复用的地方会继承"
        f"那套盒模型: {collisions}\n"
        "把块级类限定到它的容器里(例如 `.side .brand`),或者给修饰类换个名字。")


def test_every_pill_modifier_is_scoped_to_pill():
    """`.pill X` 的 X 不能是裸类 —— 否则 pill 会捡到 X 的盒模型。"""
    css = _css()
    bare = {m.group(1) for sel, _ in _rules(css)
            if (m := re.fullmatch(r"\.([A-Za-z][\w-]*)", sel))}
    used = {names[1] for names in _class_lists()
            if names[0] == "pill" and len(names) > 1}
    # 只改颜色的状态类本来就是独立工具类,被 pill 复用是设计的一部分
    allowed = {"ok", "warn", "err", "accent", "dim", "on", "off"}
    bad = sorted(used & bare - allowed)
    assert not bad, f"pill 的修饰类里有裸类定义: {bad}"


# ---------------------------------------------------------------- 全局选择器

def test_no_global_element_selector_stretches_every_svg():
    """`svg{}` 这种全局元素选择器会命中页面上每一个 svg。

    曾经有一条 `svg{display:block;width:100%;height:auto}`:图例里 12×12 的圆点被撑成
    93×93,版本图的泳道 SVG 被拉到卡片宽度并按比例变高。需要撑满的只有曲线图,
    它有点名的 id。

    只查 svg/img/canvas/video:这四个自带固有尺寸,全局 `width:100%;height:auto`
    会把每一个都按比例缩放。`table{width:100%}` 不在此列 —— 表格是普通块级容器,
    宽度撑满不会缩放任何东西。
    """
    css = _css()
    offenders = [sel for sel, body in _rules(css)
                 if re.fullmatch(r"(svg|img|canvas|video)", sel.strip())
                 and re.search(r"(?:^|;|\s)width[\s:]", body)]
    assert not offenders, (
        f"这些全局元素选择器会把页面上每一个同类元素都拉伸: {offenders}\n"
        "改成 id 或类选择器,只命中真正要改的那一个。")


# ---------------------------------------------------------------- HTML / JS 对齐

def test_every_view_container_is_referenced_by_script():
    """每个 `<div id="v-xxx" class="view">` 都要有 JS 引用它。

    这条抓的是 `loadGraph` 那种缺口:视图切过去了,渲染函数不存在,页面一片空白,
    只在浏览器控制台留一行错误 —— 服务端测试全绿也照样发生。
    """
    views = re.findall(r'<div id="(v-[a-z]+)" class="view"', SOURCE)
    assert views, "没有解析到任何视图容器 —— 先确认 serve_ui.py 的结构没变"
    script = SOURCE.split("</style>", 1)[1]
    missing = [v for v in views
               if v not in script and v[len("v-"):] not in script]
    assert not missing, (
        f"这些视图容器在 JS 里没有任何引用,点开会是空白: {missing}")


def _script() -> str:
    """页面上**所有**内联脚本拼在一起。

    它以前只取第一块(`split(..., 1)`)。加了一个"先定主题再绘制"的小脚本块之后,
    这个取法就只看得到那三行 —— 于是"每个 inline handler 都有定义"这条检查开始报
    `toggleTheme` 不存在,而它明明定义在第二块里。检查本身是对的,取脚本的方式是错的。
    """
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", SOURCE, re.S)
    return "\n".join(blocks)


def test_every_view_renderer_is_defined():
    """`loadXxx()` / `renderXxx()` 这类视图渲染函数必须真的被定义过。

    `view()` 曾经调用一个从未写过的 `loadGraph()`,而它正好在 `if(id==='graph')`
    分支里,所以静态看没问题、服务端测试全绿,只有点开版本图才会炸,而且只在浏览器
    控制台留一行错误。这个文件里视图渲染器一律用 `load*` / `render*` 命名,所以按
    这个约定查就能精确抓住缺口,不会把 `a.b()`、`Object.keys()` 之类误报进来。
    """
    script = _script()
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", script))
    defined |= set(re.findall(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", script))
    called = set(re.findall(r"\b((?:load|render)[A-Z][\w$]*)\s*\(", script))
    missing = sorted(called - defined)
    assert not missing, (
        f"这些视图渲染函数被调用但从未定义 —— 对应的页面点开会是空白: {missing}")


def test_every_inline_handler_is_defined():
    """HTML 的 `onclick=` / `onchange=` 指向的函数必须在脚本里存在。"""
    script = _script()
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", script))
    defined |= set(re.findall(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", script))
    handlers = set(re.findall(r"on(?:click|change|input)=\"([A-Za-z_$][\w$]*)\(", SOURCE))
    missing = sorted(handlers - defined)
    assert not missing, f"onclick/onchange 指向了不存在的函数: {missing}"


def test_no_stale_element_ids_are_referenced():
    """JS 里 `$('#id')` 引用的元素必须还在 HTML 里。

    表单改版时留下过 `$('#btn-go')`,那是一个已经不存在的按钮 —— `null.disabled`
    会把"运行结束"这条路径整个打断。
    """
    ids = set(re.findall(r'id="([\w-]+)"', SOURCE))
    referenced = set(re.findall(r"\$\('#([\w-]+)'\)", SOURCE))
    missing = sorted(referenced - ids)
    assert not missing, f"JS 引用了 HTML 里不存在的 id: {missing}"


def test_no_selector_targets_a_class_that_does_not_exist():
    """`querySelector('.x')` / `$('.x')` 里的类必须在 CSS 里定义过。

    历史结果那一行曾经是 `view('exp',document.querySelector('.tabs button'))`,
    而 `.tabs` 在整份文件里根本不存在 —— 于是拿到 null,`view()` 在
    `btn.classList.add('on')` 上抛异常,把同一个 onclick 里后面的 `showRun()`
    一起吃掉。点一行历史记录只会切视图,什么也不显示。
    """
    css = _css()
    defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css))
    defined |= set(re.findall(r'class="([^"]*)"', SOURCE) and
                   [c for raw in re.findall(r'class="([^"]*)"', SOURCE)
                    for c in raw.split() if "${" not in c and "'" not in c])
    targets = set(re.findall(r"querySelector(?:All)?\('\.([A-Za-z][\w-]*)", SOURCE))
    targets |= set(re.findall(r"\$\('\.([A-Za-z][\w-]*)'\)", SOURCE))
    missing = sorted(targets - defined)
    assert not missing, (
        f"这些类选择器指向了不存在的类,拿到的会是 null: {missing}")


def test_every_css_variable_used_is_defined():
    """`var(--x)` 里的 x 必须有定义(或者带一个兜底值)。

    `--accent` 曾经被用来给对比条上色,但整份文件里只有 `--brand`。自定义属性没定义时
    `var()` 会让**整条声明**失效,于是那些条子是空的 —— 看起来像"分数都是 0",
    而不是像"样式写错了"。
    """
    css = _css()
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\((--[\w-]+)\)", SOURCE))      # 不带兜底值的才算
    missing = sorted(used - defined)
    assert not missing, f"用了没有定义的 CSS 变量(且没给兜底值): {missing}"


def test_filter_groups_are_buttons_not_dropdowns():
    """筛选是视图,不是配置 —— 它必须是按钮组,不能退回成 `<select>`。

    做成下拉时它和 harness、任务集、模型并排,看起来像第四个配置字段;三个下拉还会
    把栅格撑成三行,和旁边一行的字段对不齐。
    """
    assert 'class="filters"' in SOURCE
    assert 'id="g-hrole"' in SOURCE and 'id="g-henv"' in SOURCE
    for group in ("g-hrole", "g-henv"):
        assert f'<select id="{group}"' not in SOURCE
    # 每个筛选项渲染成按钮
    assert "class=\"seg${FH[key]===v?' on':''}\"" in SOURCE


def test_train_and_eval_are_two_panels_not_one_selector():
    """train 与 eval 是两次独立运行,必须并排两个面板、两个按钮。

    曾经是一个「跑哪一侧」下拉 + 一份随侧别切换的清单,那让两次运行看起来像一次
    运行的一个选项 —— 而它们的产物(进化 vs 一次测量)完全不同。
    """
    assert 'class="duo"' in SOURCE
    assert 'id="btn-train"' in SOURCE and 'id="btn-eval"' in SOURCE
    assert 'id="f-side"' not in SOURCE
    # 两块卡的行必须对齐:靠 subgrid 共享行高,而不是猜 min-height
    assert "grid-template-rows:subgrid" in SOURCE
    assert "grid-row:span 6" in SOURCE


def test_panels_align_on_subgrid_rows():
    """subgrid 的行模板必须和两块卡子元素的数量一致。

    `grid-template-rows` 写了 N 行、卡里却有 N+1 个子元素时,多出来的会变成隐式行,
    高度按内容走 —— 两块卡就又开始错位,而且错得比不用 subgrid 更隐蔽。
    """
    m = re.search(r"\.duo\{[^}]*grid-template-rows:([^;}]+)", SOURCE)
    assert m, "找不到 .duo 的行模板"
    rows = [r for r in m.group(1).split() if r]
    assert len(rows) == 6, f".duo 声明了 {len(rows)} 行,预期 6: {rows}"
    assert rows[-1] == "auto" and "1fr" in rows


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# ------------------------------------------------- 布局:元素不能互相压住
#
# 这一类 bug 只有渲染出来才看得见,而它们连着出了三次(配置面板压住导航轨、抽屉
# 底部压住视图 tab、任务列表溢出到下一个面板上)。所以把它变成一条静态可查的不变量:
# **同一个容器里纵向排列的卡片,边界不能相交**。比"对着截图看"可靠,也不会随主题/
# 视口漂移。
def test_the_setup_is_full_width_not_a_narrow_side_panel():
    """实验设置是整宽的,不是一条窄侧栏。

    它先做过"右侧默认收起的抽屉",再做过"左侧 336px 的组合面板",两次都不对:
    336px 里放"基础 HARNESS / 任务集 / 方法 / 改进器 / 模型"这些要读长文本的下拉,
    标签会换行、选项名被截断(实测下拉宽 411px 是合适的,336 的版本只剩 ~300)。
    这条断言钉住"整宽"这件事,免得下次又滑回窄栏。
    """
    css = _css()
    assert "#setup{display:flex" in css, "设置区是页面里的一个整宽区块"
    assert ".drawer" not in css, \
        "侧栏/抽屉那套样式应该已经完全删掉(它把设置挤窄了)"
    assert "#setup.collapsed .card{display:none}" in css, \
        "收起态必须真的把卡片藏起来,只留一行摘要"


def test_the_setup_collapses_to_a_summary_once_a_run_starts():
    """跑起来之后设置自动收起 —— 但收起时必须留下"这次跑什么"。

    只收起不留摘要,就等于把配置藏了:使用者会以为设置没了(实测用户的第一反应
    正是"我那个选 harness 的界面去哪了")。所以摘要里要有 harness、任务集、方法、
    改进器和步数。
    """
    script = _script()
    assert "function toggleSetup(" in script
    assert "function setupSummary(" in script
    summary = script[script.index("function setupSummary("):]
    for field in ("#f-harness", "#f-dataset", "#f-method", "#f-improver"):
        assert field in summary, f"收起摘要里少了 {field}"
    assert "toggleSetup(false)" in script, "开跑时要自动收起"


def test_every_inline_handler_is_defined_even_across_scripts():
    """页面有多块内联脚本时,`onclick=` 的目标可能定义在第二块里。

    取脚本的方式必须覆盖**全部** `<script>` 块。实测:加了"先定主题再绘制"的三行
    小脚本后,旧取法只看得见那三行,于是这条检查开始报 `toggleTheme` 不存在。
    """
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", SOURCE, re.S)
    assert len(scripts) >= 2, "这个页面现在应该有主题脚本和主脚本两块"
    script = "\n".join(scripts)
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", script))
    handlers = set(re.findall(r"on(?:click|change|input)=\"([A-Za-z_$][\w$]*)\(", SOURCE))
    missing = sorted(handlers - defined)
    assert not missing, f"onclick/onchange 指向了不存在的函数: {missing}"


# ------------------------------- 三个资源必须真的被页面加载
#
# 拆成文件之后多了一个**新的**静默失败点:index.html 里那个路径打错一个字母,页面就
# 变成一个没有样式、没有脚本的空壳 —— 而服务端依然回 200。所以路径本身要有断言。
def test_the_page_loads_its_own_assets():
    assert 'href="/ui/app.css"' in HTML, "index.html 没有引用样式表"
    assert 'src="/ui/app.js"' in HTML, "index.html 没有引用脚本"


def test_the_server_serves_exactly_those_three_files():
    """服务端只认这三个文件;`..` 之类不允许穿过 `tools/ui/`。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ui_mod", ROOT / "tools" / "serve_ui.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("index.html", "app.css", "app.js"):
        got = mod.ui_file(name)
        assert got is not None, f"{name} 取不到"
        body, ctype = got
        assert body and "charset=utf-8" in ctype, (name, ctype)
    assert mod.ui_file("../serve_ui.py") is None, "路径里带 .. 必须被拒"
    assert mod.ui_file("nope.js") is None


def test_the_javascript_parses():
    """搬出 Python 字符串之后,这件事终于可以直接查了。

    以前 JS 是 `PAGE` 这个 raw string 的一部分:一个多余的花括号只有渲染出来才知道
    (`SyntaxError` 只在浏览器控制台里)。现在 `node --check` 对一个文件跑就行 ——
    没有 node 就跳过,并说明,而不是假装通过了。
    """
    import shutil, subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("这台机器上没有 node;JS 语法检查需要它(渲染前就能抓到)")
    proc = subprocess.run([node, "--check", str(UI / "app.js")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"tools/ui/app.js 语法错误:{proc.stderr[-500:]}"
