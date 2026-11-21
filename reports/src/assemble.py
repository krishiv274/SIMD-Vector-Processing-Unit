"""
assemble.py - builds <name>.tex for the Phase 2 report and the combined paper.

Both papers share the Phase 1 report's preamble (fonts, colours, diagram
styles), so they look like one series, and its reference list. This script
takes the preamble from main.tex, adds the paper's body (<name>_body.tex),
and appends exactly the references the body cites, in the order it first
cites them (IEEE style), taken from main.tex and extra_refs.tex.

Usage (make report runs it):  python3 assemble.py phase2
                              python3 assemble.py combined
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
TITLES = {"phase2": "Phase II report", "combined": "Combined Phase I and II paper"}


def bibitems(text):
    """Every \\bibitem in a file, by key."""
    return {m.group(1): m.group(0).rstrip() + "\n\n"
            for m in re.finditer(r"\\bibitem\{([^}]*)\}.*?(?=\\bibitem|\\end\{thebibliography\}|\Z)", text, re.S)}


def main(name):
    main_tex = (SRC / "main.tex").read_text()
    preamble = main_tex.split("\\begin{document}")[0]
    preamble = preamble.replace("% Phase I report - SIMD Vector Processing Unit",
                                f"% {TITLES[name]} - SIMD Vector Processing Unit (made by assemble.py)")
    preamble = preamble.replace("(runs reports/src/make_figures.py for the result plots, then tectonic)",
                                "(runs reports/src/make_figures_p2.py for the result plots, then tectonic)")
    refs = {**bibitems(main_tex), **bibitems((SRC / "extra_refs.tex").read_text())}
    body = (SRC / f"{name}_body.tex").read_text()

    cited = []
    for m in re.finditer(r"\\cite\{([^}]*)\}", body):
        for key in (k.strip() for k in m.group(1).split(",")):
            if key not in cited:
                cited.append(key)
    missing = [k for k in cited if k not in refs]
    if missing:
        sys.exit(f"{name}: no reference for {missing}")

    (SRC / f"{name}.tex").write_text(
        preamble + "\\begin{document}\n\n" + body
        + "\n% " + "=" * 77 + "\n\\begin{thebibliography}{99}\n\n"
        + "".join(refs[k] for k in cited) + "\\end{thebibliography}\n\n\\end{document}\n")
    print(f"{name}.tex: {len(cited)} references")


if __name__ == "__main__":
    main(sys.argv[1])
