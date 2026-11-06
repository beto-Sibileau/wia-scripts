"""MOZ adm2 P-code bridge: MSNA version (April 2026 download, 159 units) ->
current OCHA COD-AB on HDX (161 units).

Builds and compares four mappings, old (MSNA) P-codes -> new P-codes:
  1. reach : the bridge file shared by REACH (as given)
  2. name  : label matching (exact, then fuzzy) within the matching province
  3. shape : best possible 1-to-1 assignment on shapes (max total IoU)
  4. hybrid: per NEW unit, how to obtain an MSNA value (copy one old unit,
             or a weighted mean of several), combining shapes and names

Run from the wia-scripts folder:
    python moz_pcode_bridge/build_bridge.py
    python moz_pcode_bridge/build_bridge.py --piece-pop pieces_pop.csv

Shapes are compared at full resolution with the overlap logic of
cod_ab_geosight.compare (same edge and boundary-shift thresholds). Weights
of composite units are area shares by default; pass --piece-pop (columns
old_pcode, new_pcode, pop) with population per piece (pieces.gpkg) to use
population shares instead.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tempfile
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from cod_ab_geosight.compare import overlap_table  # noqa: E402

D_IN = REPO / "data_in/geo_sight/cod_shapes"
DEFAULTS = {
    "old_zip": D_IN / "moz_v2025_v2026/moz_admin_boundaries_april_2026_hdx.shp.zip",
    "new_zip": D_IN / "hdx_shp_zip/moz_admin_boundaries.shp.zip",
    "reach": D_IN / "moz_v2025_v2026/bridges_pcodes.xlsx",
    "out": REPO / "data_out/geo_sight/moz_pcode_bridge",
}
CRS_AREA = "EPSG:6933"

# thresholds
EQUAL_IOU, EQUAL_AREA = 0.90, 0.10   # "same shape" (as in cod_ab_geosight)
CLOSE = 0.80          # both units >= 80% inside each other ("within 20%")
DOMINANT = 0.80       # new unit >= 80% inside one old unit
EDGE_MIN = 0.05       # overlap kept if >= 5% of the smaller unit
SHIFT = 0.15          # overlaps < 15% of both units = boundary shift (ignored)
FUZZY = 0.80          # name similarity for a fuzzy match


# ----------------------------------------------------------------------
def read_adm2(zip_path: Path) -> gpd.GeoDataFrame:
    g = gpd.read_file(f"zip://{zip_path}!moz_admin2.shp")
    g = g[["adm2_pcode", "adm2_name", "adm1_pcode", "adm1_name", "geometry"]].copy()
    g["geometry"] = g.make_valid()
    return g


def read_reach(path: Path) -> pd.DataFrame:
    """REACH bridge (xlsx with one comma-separated column, or a plain table)."""
    df = pd.read_excel(path, header=None, dtype=str)
    if df.shape[1] == 1 and "," in str(df.iloc[0, 0]):
        df = pd.read_csv(io.StringIO("\n".join(df[0].astype(str))), dtype=str)
    else:
        df.columns = df.iloc[0]
        df = df.iloc[1:]
    df = df.rename(columns={"2025_ADM2_PCODE": "old_pcode", "ADM2_PCODE": "new_pcode",
                            "2025_ADM1_PCODE": "old_adm1", "ADM1_PCODE": "new_adm1",
                            "ADM2_PT": "reach_name"})
    return df[["old_pcode", "new_pcode", "reach_name"]]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def core(s: str) -> str:
    return re.sub(r"^cidade(de|da|do)?", "", norm(s))


# ----------------------------------------------------------------------
def province_crosswalk(old, new) -> dict:
    """Old adm1 -> new adm1 by largest shared area."""
    a = old.dissolve("adm1_pcode").to_crs(CRS_AREA)
    b = new.dissolve("adm1_pcode").to_crs(CRS_AREA)
    out = {}
    for p, geom in a.geometry.items():
        inter = b.geometry.intersection(geom).area
        out[p] = inter.idxmax()
    return out


def name_mapping(old, new, prov) -> pd.DataFrame:
    """Old -> new by label, within the matching province, one to one."""
    rows = []
    for p_old, grp in old.groupby("adm1_pcode"):
        cand = new[new["adm1_pcode"] == prov[p_old]]
        pairs = []
        for _, o in grp.iterrows():
            for _, n in cand.iterrows():
                if norm(o.adm2_name) == norm(n.adm2_name):
                    score, kind = 1.0, "exact"
                else:
                    score = SequenceMatcher(None, core(o.adm2_name), core(n.adm2_name)).ratio()
                    kind = "fuzzy"
                if score >= FUZZY:
                    pairs.append((score, kind == "exact", o.adm2_pcode, n.adm2_pcode, kind))
        used_o, used_n = set(), set()
        for score, _, po, pn, kind in sorted(pairs, reverse=True):
            if po in used_o or pn in used_n:
                continue
            used_o.add(po); used_n.add(pn)
            rows.append({"old_pcode": po, "new_pcode": pn, "name_match": kind,
                         "name_score": round(score, 3)})
    m = pd.DataFrame(rows)
    return old[["adm2_pcode"]].rename(columns={"adm2_pcode": "old_pcode"}).merge(
        m, on="old_pcode", how="left")


def overlaps(old, new) -> pd.DataFrame:
    """Old x new overlaps (full resolution), with shares and areas."""
    cfg = {"equal_area_crs": CRS_AREA, "edge_min_frac": EDGE_MIN}
    e, _, _ = overlap_table(old, new, "adm2_pcode", "adm2_pcode", cfg)
    e = e.rename(columns={"pcode_ref": "old_pcode", "pcode_hdx": "new_pcode",
                          "frac_ref": "share_of_old", "frac_hdx": "share_of_new"})
    e["boundary_shift"] = e[["share_of_old", "share_of_new"]].max(axis=1) < SHIFT
    return e


def shape_mapping(old, new, e) -> pd.DataFrame:
    """Best 1-to-1 assignment maximizing the total IoU."""
    po, pn = list(old.adm2_pcode), list(new.adm2_pcode)
    iou = np.zeros((len(po), len(pn)))
    io_, in_ = {p: i for i, p in enumerate(po)}, {p: i for i, p in enumerate(pn)}
    for r in e.itertuples():
        iou[io_[r.old_pcode], in_[r.new_pcode]] = r.iou
    r, c = linear_sum_assignment(-iou)
    m = pd.DataFrame({"old_pcode": [po[i] for i in r], "new_pcode": [pn[j] for j in c],
                      "iou": iou[r, c]})
    m = m[m.iou > 0]
    return old[["adm2_pcode"]].rename(columns={"adm2_pcode": "old_pcode"}).merge(
        m, on="old_pcode", how="left")


# ----------------------------------------------------------------------
def hybrid(old, new, e, names, reach, pop=None) -> pd.DataFrame:
    """Per new unit: category, method and the old sources with weights.

    Categories (first that applies; s = share of the new unit covered by an
    old unit, c = share of that old unit inside the new unit):
      equal          1 old unit, IoU >= 0.90 and area within 10%     -> copy
      close          s >= 0.80 and c >= 0.80 (within 20% both ways)   -> copy
      shrunk_copy    s >= 0.80, 0.50 <= c < 0.80, same unit by name /
                     REACH (the unit lost territory to a neighbour)    -> copy
      split_copy     s >= 0.80 otherwise (one old unit split in several) -> copy
      merge_weighted several old units, all wholly inside (c >= 0.80) -> weighted
      expanded       several old units; one of them lies wholly in the
                     new unit (c >= 0.80) and is the same unit by name /
                     REACH (the anchor): the unit gained territory     -> copy
                     the anchor, or population-weighted mean if given
      complex_weighted parts of several old units, no anchor          -> weighted
    """
    pair = set(zip(names.dropna().old_pcode, names.dropna().new_pcode))
    pair_reach = set(zip(reach.dropna().old_pcode, reach.dropna().new_pcode))
    use = e[~e.boundary_shift].copy()
    if pop is not None:
        use = use.merge(pop, on=["old_pcode", "new_pcode"], how="left")
    rows = []
    for pn in new.adm2_pcode:
        src = use[use.new_pcode == pn].sort_values("share_of_new", ascending=False)
        base = {"new_pcode": pn}
        if src.empty:
            rows.append({**base, "category": "no_source", "method": "none",
                         "confidence": "none"})
            continue

        def same(po):
            return (po, pn) in pair or (po, pn) in pair_reach

        top = src.iloc[0]
        s, c = top.share_of_new, top.share_of_old
        one = None
        if len(src) == 1 and top.iou >= EQUAL_IOU and abs(top.area_ratio - 1) <= EQUAL_AREA:
            one = ("equal", "high" if same(top.old_pcode) else "high (renamed)")
        elif s >= CLOSE and c >= CLOSE:
            one = ("close", "high" if same(top.old_pcode) else "medium (renamed)")
        elif s >= DOMINANT:
            one = (("shrunk_copy", "high") if same(top.old_pcode) and c >= 0.5
                   else ("split_copy", "medium"))
        if one:
            rows.append({**base, "category": one[0], "method": "copy",
                         "confidence": one[1], "old_pcode": top.old_pcode,
                         "weight": 1.0, "share_of_new": s, "share_of_old": c,
                         "iou": top.iou, "same_unit": same(top.old_pcode)})
            continue

        # composite: the new unit is made of pieces of several old units
        anchors = [r.old_pcode for r in src.itertuples()
                   if r.share_of_old >= CLOSE and same(r.old_pcode)]
        whole = (src.share_of_old >= CLOSE).all()
        has_pop = pop is not None and src["pop"].notna().all() and src["pop"].sum() > 0
        if has_pop:
            w, basis = src["pop"] / src["pop"].sum(), "population"
        else:
            w, basis = src.share_of_new / src.share_of_new.sum(), "area"
        if whole:
            cat, conf, method = "merge_weighted", "medium", f"weighted mean ({basis})"
        elif anchors and not has_pop:
            cat, conf, method = "expanded", "medium", f"copy anchor {anchors[0]}"
        elif anchors:
            cat, conf, method = "expanded", "medium", "weighted mean (population)"
        else:
            cat, conf, method = "complex_weighted", "low", f"weighted mean ({basis})"
        for (_, r), wi in zip(src.iterrows(), w):
            rows.append({**base, "category": cat, "method": method, "confidence": conf,
                         "old_pcode": r.old_pcode, "weight": wi, "weight_basis": basis,
                         "share_of_new": r.share_of_new, "share_of_old": r.share_of_old,
                         "iou": r.iou, "same_unit": same(r.old_pcode),
                         "name_anchor": anchors[0] if anchors else None})
    h = pd.DataFrame(rows)
    on = old.set_index("adm2_pcode").adm2_name
    nn = new.set_index("adm2_pcode").adm2_name
    h.insert(1, "new_name", h.new_pcode.map(nn))
    h["old_name"] = h.old_pcode.map(on)
    cov = use.groupby("new_pcode").share_of_new.sum()
    h["new_area_covered"] = h.new_pcode.map(cov).fillna(0)
    for col in ("weight", "share_of_new", "share_of_old", "iou", "new_area_covered"):
        h[col] = h[col].astype(float).round(4)
    return h


# ----------------------------------------------------------------------
def compare_old_view(old, reach, names, shape, h, e) -> pd.DataFrame:
    v = old[["adm2_pcode", "adm2_name", "adm1_name"]].rename(
        columns={"adm2_pcode": "old_pcode", "adm2_name": "old_name"})
    v = v.merge(reach[["old_pcode", "new_pcode"]].rename(columns={"new_pcode": "reach_new"}),
                on="old_pcode", how="left")
    v["in_reach_file"] = v.old_pcode.isin(reach.old_pcode)
    v = v.merge(names.rename(columns={"new_pcode": "name_new"}), on="old_pcode", how="left")
    v = v.merge(shape.rename(columns={"new_pcode": "shape_new", "iou": "shape_iou"}),
                on="old_pcode", how="left")
    role = (h.dropna(subset=["old_pcode"]).groupby("old_pcode")
            .apply(lambda d: "; ".join(f"{r.new_pcode}:{r.category}" for r in d.itertuples()), include_groups=False)
            .rename("hybrid_use"))
    v = v.merge(role, left_on="old_pcode", right_index=True, how="left")
    used_share = (h.dropna(subset=["old_pcode"]).groupby("old_pcode").share_of_old.sum())
    v["hybrid_share_used"] = v.old_pcode.map(used_share).fillna(0).round(3)

    def agree(r):
        vals = {"reach": r.reach_new, "name": r.name_new, "shape": r.shape_new}
        vals = {k: (x if isinstance(x, str) else None) for k, x in vals.items()}
        if len(set(vals.values())) == 1:
            return "all agree" if vals["reach"] else "all blank"
        pairs = [f"{a}={b}" for a, b in (("reach", "name"), ("reach", "shape"), ("name", "shape"))
                 if vals[a] and vals[a] == vals[b]]
        return ", ".join(pairs) if pairs else "all differ"
    v["agreement"] = v.apply(agree, axis=1)
    return v


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", default=str(v))
    ap.add_argument("--piece-pop", default=None,
                    help="csv old_pcode,new_pcode,pop per piece (population weights)")
    ap.add_argument("--no-map", action="store_true")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    old, new = read_adm2(Path(a.old_zip)), read_adm2(Path(a.new_zip))
    reach = read_reach(Path(a.reach))
    pop = pd.read_csv(a.piece_pop, dtype={"old_pcode": str, "new_pcode": str}) if a.piece_pop else None
    print(f"old (MSNA) {len(old)} units, new {len(new)} units, REACH rows {len(reach)}")

    prov = province_crosswalk(old, new)
    names = name_mapping(old, new, prov)
    e = overlaps(old, new)
    shape = shape_mapping(old, new, e)
    h = hybrid(old, new, e, names, reach, pop)
    v = compare_old_view(old, reach, names, shape, h, e)

    on = old.set_index("adm2_pcode").adm2_name; nn = new.set_index("adm2_pcode").adm2_name
    e.insert(1, "old_name", e.old_pcode.map(on)); e.insert(3, "new_name", e.new_pcode.map(nn))
    a_old = old.to_crs(CRS_AREA).set_index("adm2_pcode").area
    e["piece_km2"] = (e.share_of_old * e.old_pcode.map(a_old) / 1e6).round(3)

    # outputs
    e.round(4).to_csv(out / "overlaps.csv", index=False)
    v.to_csv(out / "comparison_old_view.csv", index=False)
    h.to_csv(out / "bridge_hybrid_long.csv", index=False)
    wide = (h.groupby(["new_pcode", "new_name", "category", "method", "confidence"], dropna=False,
                      sort=False)
            .apply(lambda d: "; ".join(f"{r.old_pcode} {r.old_name} ({r.weight:.2f})"
                                       for r in d.itertuples() if isinstance(r.old_pcode, str)),
                   include_groups=False)
            .rename("sources").reset_index())
    wide["anchor"] = wide.new_pcode.map(
        h.dropna(subset=["name_anchor"]).groupby("new_pcode").name_anchor.first()
        if "name_anchor" in h else {})
    rmap = reach.dropna(subset=["new_pcode"]).groupby("new_pcode").old_pcode.apply(";".join)
    wide["reach_old"] = wide.new_pcode.map(rmap)
    single = wide.sources.str.count(";") == 0
    src1 = wide.sources.str.split(" ").str[0]
    wide["reach_vs_hybrid"] = np.select(
        [wide.reach_old.isna(),
         single & (wide.reach_old == src1),
         wide.anchor.notna() & (wide.reach_old == wide.anchor)],
        ["no value in REACH", "same", "REACH = anchor only"], default="differs")
    order = ["equal", "close", "shrunk_copy", "split_copy", "expanded",
             "merge_weighted", "complex_weighted", "no_source"]
    wide["category"] = pd.Categorical(wide.category, order, ordered=True)
    wide = wide.sort_values(["category", "new_pcode"])
    wide.to_csv(out / "bridge_hybrid.csv", index=False)
    pieces = gpd.overlay(old[["adm2_pcode", "geometry"]].rename(columns={"adm2_pcode": "old_pcode"}),
                         new[["adm2_pcode", "geometry"]].rename(columns={"adm2_pcode": "new_pcode"}),
                         how="intersection", keep_geom_type=True)
    pieces["km2"] = (pieces.to_crs(CRS_AREA).area / 1e6).round(4)
    pieces = pieces[pieces.km2 > 0.01]
    # written in a temp folder then copied (SQLite fails on synced/mounted folders)
    with tempfile.TemporaryDirectory() as tmp:
        pieces.to_file(Path(tmp) / "pieces.gpkg", driver="GPKG", layer="pieces")
        shutil.copyfile(Path(tmp) / "pieces.gpkg", out / "pieces.gpkg")

    summarize(v, h, wide, reach, out)
    if not a.no_map:
        make_map(old, new, wide, out / "bridge_map.html")
    print(f"outputs in {out}")


def summarize(v, h, wide, reach, out):
    lines = ["# MOZ adm2 P-code bridge: summary", ""]
    lines += [f"- Old (MSNA) units: {len(v)}; new units: {wide.new_pcode.nunique()}",
              f"- REACH rows: {len(reach)}; old units missing from REACH: "
              f"{', '.join(v[~v.in_reach_file].old_pcode) or 'none'}", ""]
    lines += ["## Agreement of the three 1-to-1 mappings (per old unit)", "",
              v.agreement.value_counts().to_frame("old units").to_markdown(), ""]
    lines += ["## Name matching", "",
              v.name_match.fillna("no match").value_counts().to_frame("old units").to_markdown(), ""]
    cat = wide.groupby(["category", "confidence"], observed=True).size().rename("new units").reset_index()
    lines += ["## Hybrid bridge (per new unit)", "", cat.to_markdown(index=False), ""]
    rv = wide.groupby(["category", "reach_vs_hybrid"], observed=True).size().rename(
        "new units").reset_index()
    lines += ["## Hybrid vs REACH (per new unit)", "", rv.to_markdown(index=False), ""]
    lines += ["## Composite and split units", ""]
    sub = wide[~wide.category.isin(["equal", "close"])][["new_pcode", "new_name", "category",
                                                        "method", "sources", "anchor"]]
    lines += [sub.to_markdown(index=False), ""]
    unused = v[v.hybrid_share_used < 0.5][["old_pcode", "old_name", "hybrid_share_used",
                                           "hybrid_use"]]
    lines += ["## Old units with little or no use in the hybrid bridge", "",
              unused.to_markdown(index=False) if len(unused) else "none", ""]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def make_map(old, new, wide, path):
    import plotly.graph_objects as go
    colors = {"equal": "#cfe2d4", "close": "#7fbf8e", "shrunk_copy": "#6fa8dc",
              "split_copy": "#1c4f8c", "expanded": "#f6b26b",
              "merge_weighted": "#f1c232", "complex_weighted": "#cc0000",
              "no_source": "#999999"}
    labels = {"equal": "equal shape (copy)", "close": "close, within 20% (copy)",
              "shrunk_copy": "shrunk: lost area to a neighbour (copy)",
              "split_copy": "split of one old unit (copy)",
              "expanded": "expanded: gained area (copy anchor / pop. weights)",
              "merge_weighted": "merge of whole old units (weighted)",
              "complex_weighted": "complex: parts of old units (weighted)",
              "no_source": "no source"}
    g = new.merge(wide, left_on="adm2_pcode", right_on="new_pcode")
    g["geometry"] = g.geometry.simplify(0.002, preserve_topology=True)
    fig = go.Figure()
    for cat, col in colors.items():
        d = g[g.category == cat].reset_index(drop=True)
        if d.empty:
            continue
        d["id"] = d.index.astype(str)
        fig.add_trace(go.Choropleth(
            geojson=json.loads(d[["id", "geometry"]].to_json()), locations=d["id"],
            z=[1] * len(d), featureidkey="properties.id", showscale=False,
            colorscale=[[0, col], [1, col]], name=f"{labels[cat]} ({len(d)})",
            marker=dict(line=dict(color="rgba(30,30,30,0.6)", width=0.6)),
            customdata=d[["new_pcode", "new_name", "confidence", "sources"]].to_numpy(),
            hovertemplate=("<b>%{customdata[1]}</b> (%{customdata[0]})<br>"
                           "confidence: %{customdata[2]}<br>from: %{customdata[3]}"
                           "<extra>" + labels[cat] + "</extra>"),
            showlegend=True))
    o = old.copy()
    o["geometry"] = o.geometry.simplify(0.002, preserve_topology=True)
    o = o.reset_index(drop=True); o["id"] = o.index.astype(str)
    fig.add_trace(go.Choropleth(
        geojson=json.loads(o[["id", "geometry"]].to_json()), locations=o["id"],
        z=[1] * len(o), featureidkey="properties.id", showscale=False,
        colorscale=[[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,0)"]],
        name="old (MSNA) boundaries", visible="legendonly",
        marker=dict(line=dict(color="rgba(150,0,120,0.9)", width=1.2)), hoverinfo="skip",
        showlegend=True))
    fig.update_layout(
        title="MOZ adm2: how each new P-code gets its MSNA value (hybrid bridge)",
        geo=dict(fitbounds="locations", visible=False, bgcolor="#f6f7f9"),
        paper_bgcolor="#f6f7f9", height=900, margin=dict(l=0, r=0, t=50, b=0),
        legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.85)"))
    fig.write_html(path, include_plotlyjs="cdn")


if __name__ == "__main__":
    main()
