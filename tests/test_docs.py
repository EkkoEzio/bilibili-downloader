"""文档自身的结构不变量。

文档漂移在本项目是反复发生的问题(REQUIREMENTS 停在 v2.0 状态、COMPLIANCE 的
限频数字比代码松、CHANGELOG 的 [未发布] 占位错位到两个已发布版本之间),
纯人工核对靠不住,这里把能机械检查的部分固定住。
"""
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _changelog() -> str:
    return (BASE / "CHANGELOG.md").read_text(encoding="utf-8")


def _versions(text: str) -> list[str]:
    """按出现顺序取出已发布版本号(跳过 [未发布] 与 [v1.x] 汇总小节)。"""
    out = []
    for m in re.finditer(r"^## \[v(\d+)\.(\d+)\.(\d+)\]", text, re.M):
        out.append(tuple(int(g) for g in m.groups()))
    return out


def test_changelog_versions_unique_and_descending():
    """版本小节不得重复,且必须由新到旧排列。

    回归:一次 str_replace 把 v2.1.0 的标题误写成 v2.0.0,于是文件里出现两个
    v2.0.0、v2.1.0 凭空消失,而 v2.1.0 的正文还挂在错标题下面。
    """
    vs = _versions(_changelog())
    assert vs, "没解析到任何版本小节"
    assert len(vs) == len(set(vs)), (
        "CHANGELOG 有重复的版本小节:"
        + ", ".join("v{}.{}.{}".format(*v) for v in sorted(
            {v for v in vs if vs.count(v) > 1})))
    assert vs == sorted(vs, reverse=True), (
        "版本小节应由新到旧:实际顺序 "
        + " → ".join("v{}.{}.{}".format(*v) for v in vs))


def test_changelog_unreleased_placeholder_is_on_top():
    """[未发布] 占位必须在所有已发布版本之前。

    回归:v2.1.0 发布后占位没上移,卡在 v2.1.0 与 v2.0.0 之间。
    """
    text = _changelog()
    pos_unreleased = text.find("## [未发布]")
    if pos_unreleased < 0:
        return  # 没有占位小节也可以
    first_release = re.search(r"^## \[v\d+\.\d+\.\d+\]", text, re.M)
    assert first_release, "没找到任何已发布版本小节"
    assert pos_unreleased < first_release.start(), \
        "[未发布] 占位应在所有已发布版本之前"


def test_changelog_link_refs_cover_released_versions():
    """每个已发布版本都应有底部链接引用(Keep a Changelog 惯例)。"""
    text = _changelog()
    declared = {tuple(int(g) for g in m.groups())
                for m in re.finditer(r"^\[v(\d+)\.(\d+)\.(\d+)\]:\s*http", text, re.M)}
    missing = sorted(set(_versions(text)) - declared)
    assert not missing, ("缺少底部链接引用:"
                         + ", ".join("v{}.{}.{}".format(*v) for v in missing))


def test_readme_banner_matches_latest_changelog_version():
    """README 顶部的版本横幅应指向 CHANGELOG 里最新的已发布版本。"""
    latest = max(_versions(_changelog()))
    banner = (BASE / "README.md").read_text(encoding="utf-8")[:600]
    m = re.search(r"\*\*v(\d+)\.(\d+)\.(\d+)\*\*", banner)
    assert m, "README 顶部没找到版本横幅"
    assert tuple(int(g) for g in m.groups()) == latest, (
        "README 横幅 v{}.{}.{} 与 CHANGELOG 最新版本 v{}.{}.{} 不一致".format(
            *m.groups(), *latest))
