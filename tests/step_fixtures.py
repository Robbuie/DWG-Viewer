"""Build small STEP files with OpenCascade, for tests and for trying the
viewer without a real model to hand.

``make_assembly`` writes a coloured assembly: a base plate, four
instances of one bolt (so instancing is exercised), and a bracket with
one face coloured differently from the rest of it.

    python tests/step_fixtures.py out.step
"""
from __future__ import annotations

import sys
from pathlib import Path


def _color(r, g, b):
    from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
    return Quantity_Color(r, g, b, Quantity_TOC_sRGB)


def make_assembly(path, bolts: int = 4) -> Path:
    from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder,
                                 BRepPrimAPI_MakeSphere)
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.gp import gp_Trsf, gp_Vec, gp_Pnt, gp_Ax2, gp_Dir
    from OCP.TopLoc import TopLoc_Location
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.TDataStd import TDataStd_Name
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    surf = XCAFDoc_ColorType.XCAFDoc_ColorSurf

    def name(label, text):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))

    plate = BRepPrimAPI_MakeBox(100.0, 60.0, 8.0).Shape()
    for x, y in ((15, 15), (85, 15), (15, 45), (85, 45))[:bolts]:
        hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)),
                                        4.5, 10).Shape()
        plate = BRepAlgoAPI_Cut(plate, hole).Shape()

    head = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 8), gp_Dir(0, 0, 1)),
                                    7.0, 5.0).Shape()
    shank = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -12), gp_Dir(0, 0, 1)),
                                     4.0, 20.0).Shape()
    bolt = BRepAlgoAPI_Fuse(head, shank).Shape()

    bracket = BRepPrimAPI_MakeBox(gp_Pnt(35, 20, 8), 30.0, 20.0, 40.0).Shape()
    ball = BRepPrimAPI_MakeSphere(gp_Pnt(50, 30, 58), 10.0).Shape()

    asm = st.NewShape()
    name(asm, "Test fixture")

    plate_l = st.AddShape(plate, False)
    name(plate_l, "Base plate")
    ct.SetColor(plate_l, _color(0.55, 0.60, 0.68), surf)
    st.AddComponent(asm, plate_l, TopLoc_Location())

    bolt_l = st.AddShape(bolt, False)
    name(bolt_l, "M8 bolt")
    ct.SetColor(bolt_l, _color(0.85, 0.70, 0.20), surf)
    for x, y in ((15, 15), (85, 15), (15, 45), (85, 45))[:bolts]:
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(x, y, 0))
        st.AddComponent(asm, bolt_l, TopLoc_Location(t))

    br_l = st.AddShape(bracket, False)
    name(br_l, "Bracket")
    ct.SetColor(br_l, _color(0.20, 0.45, 0.80), surf)
    # One face of the bracket in red: a per-face colour.
    exp = TopExp_Explorer(bracket, TopAbs_FACE)
    face = exp.Current()
    sub = st.AddSubShape(br_l, face)
    ct.SetColor(sub, _color(0.85, 0.15, 0.15), surf)
    st.AddComponent(asm, br_l, TopLoc_Location())

    ball_l = st.AddShape(ball, False)
    name(ball_l, "Knob")
    st.AddComponent(asm, ball_l, TopLoc_Location())

    st.UpdateAssemblies()
    writer = STEPCAFControl_Writer()
    writer.SetColorMode(True)
    writer.SetNameMode(True)
    writer.Transfer(doc, STEPControl_AsIs)
    path = Path(path)
    writer.Write(str(path))
    return path


def make_single_solid(path) -> Path:
    """A plain STEP with one unnamed solid and no colour — the minimum a
    CAD export can be."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    w = STEPControl_Writer()
    w.Transfer(BRepPrimAPI_MakeTorus(30.0, 8.0).Shape(), STEPControl_AsIs)
    w.Write(str(path))
    return Path(path)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture.step")
    make_assembly(out)
    print(out)
