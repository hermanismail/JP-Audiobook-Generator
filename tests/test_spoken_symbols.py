r"""
test_spoken_symbols.py - symbols the seiyuu cannot read are left out of what
it is sent (2026-11-18); the ones that are read stay.

    C:\JP-Audiobook-Generator\.venv\Scripts\python.exe tests/test_spoken_symbols.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import text_pipeline as tp  # noqa: E402

results = []


def check(name, got, want):
    results.append(got == want)
    print(("PASS " if got == want else "FAIL ") + name + ("" if got == want else f"  [got {got!r}, want {want!r}]"))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    t = tp.prepare_tts_text_dynamic
    check("a ☆ heading is sent as the name", t("☆ペチカ"), "ペチカ")
    check("★ and ☆ bullets go", t("★無限に増え続けるアイテム！☆リアルの極致"), "無限に増え続けるアイテム！リアルの極致")
    for ch in "♪♫♡♥◆◇※〒©＊":
        check(f"{ch} goes", t(f"あ{ch}い"), "あい")
    check("＋ stays (武器＋７)", t("武器＋７"), "武器＋７")
    check("～ stays (８～12)", t("８～12"), "８～12")
    check("＝ stays", t("地形調査＝壁の探索"), "地形調査＝壁の探索")
    check("○ stays (a censored word)", t("魔女っ子○作戦"), "魔女っ子○作戦")
    check("a Part of symbols alone is sent unchanged, never empty", t("☆☆☆"), "☆☆☆")
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
