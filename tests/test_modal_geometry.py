"""A window stays where it opened while its content changes.

It used to move: centred in a grid, it rose by half of every line added -
a check's result, a campaign's new row, each step of a sign-in - and the
grid's column grew to fit the longest unbroken line inside, so a long
SpamBot reply pushed the whole window off to the right.

Measured in a real browser against web/src/styles.css, with the markup the
Modal component renders. Chrome runs headless and hands back the page
after its script has measured; without Chrome the test is skipped.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "src" / "styles.css"

LONG = ("Ограничения по спаму: Здравствуйте! Очень жаль, что Вы с этим столкнулись. "
        "К сожалению, иногда наша антиспам-система излишне сурово реагирует на "
        "некоторые действия. Если Вы считаете, что это произошло по ошибке, "
        "пожалуйста, сообщите нам. Пока действуют ограничения, Вы не сможете "
        "писать тем, кто не сохранил Ваш номер в своих контактах.")
PATH = "C:\\\\Users\\\\someone\\\\AppData\\\\Roaming\\\\TelegramCenter\\\\media\\\\" + "x" * 240

# What appears in an open window: each one used to move it.
GROWTH = {
    "the result of a check": f'<p class="check-result"><span class="status">'
                             f'<span class="dot err"></span>'
                             f'<span class="status-text">{LONG}</span></span></p>',
    "a long channel name": f'<div class="pick-list"><label class="pick-row">'
                           f'<input type="checkbox"><span class="grow">{LONG}</span>'
                           f'</label></div>',
    "an error with a path in it": f'<div class="form-error">{PATH}</div>',
    "a few more lines": '<p class="field-hint">one</p>' * 6,
}
TALL = '<label class="field"><input class="input"></label>' * 40

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="%CSS%"></head><body>
<div class="overlay"><div class="modal" role="dialog">
  <div class="modal-head"><span class="modal-title">@account</span></div>
  <div class="modal-body" id="body">
    <label class="field"><span class="field-label">Прокси</span>
      <select class="select"><option>Proxy</option></select></label>
    <div class="modal-section"><div id="slot"></div></div>
  </div>
  <div class="modal-foot"><button class="ab primary"><span>OK</span></button></div>
</div></div>
<pre id="out"></pre>
<script>
addEventListener("load", () => {
  const modal = document.querySelector(".modal");
  const body = document.getElementById("body");
  const slot = document.getElementById("slot");
  const at = () => { const r = modal.getBoundingClientRect();
                     return [Math.round(r.left), Math.round(r.top)]; };
  const out = { start: at(), width: body.clientWidth, grown: {} };
  for (const [name, html] of Object.entries(%GROWTH%)) {
    slot.innerHTML = html;
    out.grown[name] = at();
  }
  slot.innerHTML = %TALL%;
  const foot = document.querySelector(".modal-foot").getBoundingClientRect();
  out.tall = { at: at(), footBottom: Math.round(foot.bottom), height: innerHeight,
               scrolls: body.scrollHeight > body.clientHeight, width: body.clientWidth };
  document.getElementById("out").textContent = JSON.stringify(out);
});
</script></body></html>"""


def _chrome() -> str | None:
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("chromium"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local, r"Google\Chrome\Application\chrome.exe") if local else None,
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def _measure(tmp_path: Path, width: int, height: int) -> dict:
    chrome = _chrome()
    if chrome is None:
        pytest.skip("Chrome is not installed")
    page = tmp_path / "window.html"
    page.write_text(PAGE.replace("%CSS%", CSS.as_uri())
                        .replace("%GROWTH%", json.dumps(GROWTH))
                        .replace("%TALL%", json.dumps(TALL)), encoding="utf-8")
    done = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={tmp_path / 'profile'}",
         f"--window-size={width},{height}", "--virtual-time-budget=3000",
         "--dump-dom", page.as_uri()],
        capture_output=True, encoding="utf-8", errors="replace", timeout=90)
    found = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
    assert found and found.group(1), done.stdout[-2000:] + done.stderr[-2000:]
    return json.loads(found.group(1).replace("&amp;", "&"))


@pytest.mark.parametrize("width,height", [(1280, 720), (1920, 1080), (390, 700)])
def test_a_window_stays_where_it_opened(tmp_path, width, height):
    seen = _measure(tmp_path, width, height)

    for name, at in seen["grown"].items():
        assert at == seen["start"], f"{name} moved the window"
    assert seen["tall"]["at"] == seen["start"]


@pytest.mark.parametrize("width,height", [(1280, 720), (1280, 400)])
def test_a_tall_window_scrolls_inside_and_keeps_its_foot_on_screen(tmp_path, width,
                                                                   height):
    seen = _measure(tmp_path, width, height)
    tall = seen["tall"]

    assert tall["scrolls"]
    assert tall["footBottom"] <= tall["height"], "Save stays on screen"
    assert tall["width"] == seen["width"], \
        "a body that starts to scroll does not re-wrap its text"
