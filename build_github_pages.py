"""Write the previous app's server-rendered shell as a GitHub Pages entrypoint."""

from pathlib import Path

from server import app_shell


ROOT = Path(__file__).resolve().parent
(ROOT / "index.html").write_text(app_shell(), encoding="utf-8")
(ROOT / "test").mkdir(exist_ok=True)
(ROOT / "test" / "index.html").write_text(
    (ROOT / "showcase.html").read_text(encoding="utf-8"),
    encoding="utf-8",
)
print(f"Wrote {ROOT / 'index.html'}")
print(f"Wrote {ROOT / 'test' / 'index.html'}")
