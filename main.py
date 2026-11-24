"""工程助手 entry point.

GUI:        工程助手.exe
Self-test:  工程助手.exe --selftest <drawing> <outdir> [NX,SolidWorks] [page]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def selftest(argv):
    from app.config import Settings
    from app.pipeline import Pipeline

    if len(argv) < 4:
        return 2
    drawing, out = argv[2], argv[3]
    targets = (argv[4].split(",") if len(argv) > 4 else ["SolidWorks"])
    page = (int(argv[5]) - 1) if len(argv) > 5 and str(argv[5]).strip() else None
    log_path = Path(out) / "selftest_log.txt"
    Path(out).mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "w", encoding="utf-8")

    def log(m):
        fh.write(str(m) + "\n")
        fh.flush()

    s = Settings.load()
    p = Pipeline(s, log)
    res = p.run([drawing], targets, out, progress=log, page=page)
    data = {
        "ok": bool(res.ok),
        "analysis": res.analysis.drawing_type if res.analysis else "",
        "part": res.analysis.spec.to_dict() if res.analysis else None,
        "builds": [b.to_dict() for b in res.builds],
        "message": res.message,
    }
    (Path(out) / "selftest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    fh.close()
    return 0 if res.ok else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv))
    from PySide6.QtWidgets import QApplication
    from app.config import APP_TITLE
    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()